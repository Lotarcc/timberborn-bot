"""Run-to-run learning loop: turn a bad run's credit-assignment corrections into
augmented training rows for the decision heads.

`play_policy`/`controller` play a run and `replay.record_step` logs each cycle; when
a run ends badly, `replay.credit_assignment` looks back over the steps leading up to
the failure and proposes a better action for each ("you let water hit zero for 3
steps with no WaterPump -> you should have built one"). This module turns those
regret windows into labeled training rows and MERGES them into the base dataset
that train_cart.py/train_lidsnet.py already consume: a corrected label for a
feature vector OVERRIDES whatever the base (Oracle behavioral-cloning) dataset said
for that exact vector, since the outcome-derived correction reflects what actually
happened in a real run, not just what the deterministic planner would propose in
the abstract. A feature vector the base dataset never saw is appended as a new row.

Pipeline (see agent/run_loop.py, Task 7b):
    play a run -> replay.record_step (now also stores feature vectors, see the
    Task 7a coupling fix in agent/replay.py) -> replay.summarize_run classifies
    the outcome -> on failure, build_augmented_dataset(...) folds the run's
    corrections into agent/data/decision_dataset.json -> retrain_command() gives
    the shell sequence to re-run train_cart.py/train_lidsnet.py against it.

Run:  .venv/bin/python -m unittest agent.nlp.test_learn -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, Union

from agent import game_schema, replay

_DATA = Path(__file__).resolve().parent.parent / "data"
_DEFAULT_DATASET = _DATA / "decision_dataset.json"

# Corrections are outcome-verified (a real run actually died/stalled and this is
# the specific fix) whereas base rows are Oracle behavioral-cloning from a
# deterministic planner - weigh corrections higher so retraining favors them
# without discarding the (much larger) base dataset. >1 per the Task 7a spec.
CORRECTION_WEIGHT = 3.0
CORRECTION_SOURCE = "credit_assignment"

# host=None (default): local MPS retrain - both heads train directly on this box.
# docs/superpowers/plans/2026-07-11-timberborn-full-game-completion.md Part 0: the
# Mac's .venv has torch w/ the MPS backend, so this is now the fast/preferred path
# (no scp round-trip per run). System python3 is 3.14 with none of these deps -
# always .venv/bin/python.
_LOCAL_RETRAIN_CMD = (
    ".venv/bin/python -m agent.nlp.train_cart && "
    ".venv/bin/python -m agent.nlp.train_lidsnet"
)

# host="cka-win": the RTX 4060 Ti Windows box's Part B sequence, reproduced
# verbatim from the plan doc above (the `cka-win` ssh alias, its torch venv at
# C:\Users\semyo\tb_ml and work dir C:\Users\semyo\tb_ml_work are established
# elsewhere in the repo/operator ssh config, not invented here) - scp the dataset
# over, train remotely, scp the exported models back.
_CKA_WIN_RETRAIN_CMD = "\n".join([
    "scp agent/data/decision_dataset.json agent/data/decision_vocab.json "
    "agent/data/decision_labels.json cka-win:C:/Users/semyo/tb_ml_work/data/",
    "ssh cka-win \"C:\\Users\\semyo\\tb_ml\\Scripts\\python.exe "
    "C:\\Users\\semyo\\tb_ml_work\\nlp\\train_cart.py & "
    "C:\\Users\\semyo\\tb_ml\\Scripts\\python.exe "
    "C:\\Users\\semyo\\tb_ml_work\\nlp\\train_lidsnet.py\"",
    "scp cka-win:C:/Users/semyo/tb_ml_work/data/decision_cart.json "
    "cka-win:C:/Users/semyo/tb_ml_work/data/decision_mlp.json agent/data/",
])


def examples_from_run(run_id: str) -> List[dict]:
    """[{features, label, source, weight}] distilled from run_id's regret windows.

    Each of replay.credit_assignment(run_id)'s window entries becomes one training
    row: `features` is that step's recorded feature vector (agent/replay.py's
    record_step stores it via game_schema.feature_strings at write time), `label`
    is the window's `better_action`. A window is skipped (defensively) when
    `better_action` is not a real game_schema action - e.g. a stalled run where
    WaterPump/GathererFlag/LumberjackFlag are already all built and
    credit_assignment has nothing left to recommend (better_action is None then) -
    or when `features` is empty (a run recorded before the record_step/features
    coupling fix, or any other malformed row). Empty list for a run with nothing
    to learn from (survived cleanly, or has no salvageable windows).
    """
    valid_actions = set(game_schema.actions())
    examples: List[dict] = []
    for window in replay.credit_assignment(run_id):
        better_action = window.get("better_action")
        features = window.get("features") or []
        if better_action not in valid_actions or not features:
            continue
        examples.append({
            "features": list(features),
            "label": better_action,
            "source": CORRECTION_SOURCE,
            "weight": CORRECTION_WEIGHT,
        })
    return examples


def _feature_key(features: Iterable[str]) -> Tuple[str, ...]:
    """The merge key for a feature vector: order-independent (feature_strings'
    output order is deterministic per state but irrelevant to the multi-hot
    vectorizers in train_cart.py/train_lidsnet.py, which only read the bag)."""
    return tuple(sorted(features))


def build_augmented_dataset(
    run_ids: Iterable[str],
    base: Optional[Union[str, Path]] = None,
    out: Optional[Union[str, Path]] = None,
) -> dict:
    """Merge run_ids' outcome corrections into a base decision_dataset.json.

    A corrected example OVERRIDES the base row with the same feature key (see
    _feature_key) - the corrected row (features/label/source/weight) replaces the
    base row wholesale, so its label wins. A feature key the base dataset never
    saw is appended as a new row. When multiple corrections land on the same key
    (across run_ids, or within one run's regret windows), the last one applied
    wins - `overridden` still counts that base row exactly once (it tracks base
    rows whose FINAL label differs from their ORIGINAL label, not the number of
    times a key was touched); `added_rows` counts each genuinely-new key once.

    base defaults to agent/data/decision_dataset.json; out defaults to base (i.e.
    in-place augmentation) but can be pointed elsewhere for a dry run. Writes the
    merged `[{features, label, ...}]` dataset (same shape train_cart.py/
    train_lidsnet.py already read - they only look at row["features"]/
    row["label"], so the extra source/weight keys on corrected rows are harmless)
    and returns {base_rows, added_rows, overridden, total_rows, out_path}.
    """
    base_path = Path(base) if base is not None else _DEFAULT_DATASET
    out_path = Path(out) if out is not None else base_path

    base_rows = json.loads(base_path.read_text(encoding="utf-8"))

    merged: Dict[Tuple[str, ...], dict] = {}
    order: List[Tuple[str, ...]] = []
    original_labels: Dict[Tuple[str, ...], object] = {}
    for row in base_rows:
        key = _feature_key(row["features"])
        merged[key] = dict(row)
        order.append(key)
        original_labels[key] = row.get("label")

    added_rows = 0
    for run_id in run_ids:
        for example in examples_from_run(run_id):
            key = _feature_key(example["features"])
            if key not in merged:
                order.append(key)
                added_rows += 1
            merged[key] = dict(example)

    overridden = sum(
        1 for key, orig_label in original_labels.items()
        if merged[key].get("label") != orig_label
    )

    out_rows = [merged[key] for key in order]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out_rows, indent=2), encoding="utf-8")

    return {
        "base_rows": len(base_rows),
        "added_rows": added_rows,
        "overridden": overridden,
        "total_rows": len(out_rows),
        "out_path": str(out_path),
    }


def retrain_command(host: Optional[str] = None) -> str:
    """The shell command sequence to retrain both decision heads after
    build_augmented_dataset has written a new decision_dataset.json. Returns a
    string only - never executes anything.

    host=None (default) - local MPS retrain on this box (preferred: fast, no
    network round-trip). host="cka-win" - the RTX 4060 Ti Windows box's
    scp-dataset-there / ssh-train / scp-models-back sequence (Part B).
    """
    if host is None:
        return _LOCAL_RETRAIN_CMD
    if host == "cka-win":
        return _CKA_WIN_RETRAIN_CMD
    raise ValueError("unknown retrain host: %r (expected None or 'cka-win')" % (host,))


__all__ = [
    "examples_from_run", "build_augmented_dataset", "retrain_command",
    "CORRECTION_WEIGHT", "CORRECTION_SOURCE",
]
