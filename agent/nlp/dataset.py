"""Generate the labeled training dataset for the decision heads.

Analog of NLP_2.0's `intent_classifier_test.py` (which built final_dataset_raw.json
from a seed + augmentation). Here we sweep a realistic grid of game states across
the bootstrap trajectory, label each with the expert Oracle (behavioral cloning),
harvest any real states recorded in journals, dedup by feature vector, and write:

  data/decision_dataset.json  - [{features:[str], label:goal_id}]
  data/decision_vocab.json    - {vocab:[str]}  (the fixed feature vocabulary)
  data/decision_labels.json   - [goal_id, ...] (the label index order)

Run:  python -m agent.nlp.dataset
Pure-stdlib + planner; no torch/sklearn needed to build the dataset.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from agent.nlp import features as feat
from agent.nlp.labeler import Oracle

_ROOT = Path(__file__).resolve().parent.parent
_FIXTURES = _ROOT / "fixtures"
_DATA = _ROOT / "data"

# ---------------------------------------------------------------------------
# state construction helpers
# ---------------------------------------------------------------------------

def _base_state() -> dict:
    with (_FIXTURES / "state_fresh.json").open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _set_resource(state: dict, good: str, stored: float, days: float) -> None:
    resources = state.setdefault("resources", [])
    for item in resources:
        if str(item.get("good", "")).lower() == good.lower():
            item["stored"] = stored
            item["all_stock"] = stored
            item["days_remaining"] = days
            return
    resources.append({"good": good, "stored": stored, "all_stock": stored, "days_remaining": days})


def _set_pop(state: dict, total: int, homeless: int) -> None:
    pop = state.setdefault("population", {})
    pop["total"] = total
    pop["homeless"] = homeless


def _set_counts(state: dict, counts: Dict[str, int]) -> None:
    state.setdefault("buildings", {})["counts"] = dict(counts)


def _set_drought(state: dict, duration_days: float) -> None:
    weather = state.setdefault("weather", {})
    weather["next"] = {"duration_days": duration_days}


def _inject_unreachable(state: dict) -> None:
    buildings = state.setdefault("buildings", {})
    listing = buildings.setdefault("list", [])
    listing.append({"spec": "WaterPump", "status": "finished", "reachable": False,
                    "x": 4, "y": 5, "z": 6})


# Realistic bootstrap building configurations (bare spec keys; planner matches prefix).
_STAGES: List[Dict[str, int]] = [
    {},
    {"LumberjackFlag": 1},
    {"LumberjackFlag": 1, "GathererFlag": 1},
    {"LumberjackFlag": 1, "GathererFlag": 1, "WaterPump": 1},
    {"LumberjackFlag": 1, "GathererFlag": 1, "WaterPump": 1, "SmallTank": 2},
    {"LumberjackFlag": 1, "GathererFlag": 1, "WaterPump": 1, "SmallTank": 4},
    {"LumberjackFlag": 1, "GathererFlag": 1, "WaterPump": 1, "SmallTank": 4, "Lodge": 1},
    {"LumberjackFlag": 1, "GathererFlag": 1, "WaterPump": 1, "SmallTank": 4, "Lodge": 1,
     "EfficientFarmhouse": 1},
    {"LumberjackFlag": 1, "GathererFlag": 1, "WaterPump": 1, "SmallTank": 4, "Lodge": 1,
     "EfficientFarmhouse": 1, "SmallWarehouse": 1},
    {"LumberjackFlag": 1, "GathererFlag": 1, "WaterPump": 1, "SmallTank": 4, "Lodge": 1,
     "EfficientFarmhouse": 1, "SmallWarehouse": 1, "Inventor": 1},
    {"LumberjackFlag": 1, "GathererFlag": 1, "WaterPump": 1, "SmallTank": 5, "Lodge": 2,
     "EfficientFarmhouse": 1, "SmallWarehouse": 1, "Inventor": 1, "ForesterFlag": 1},
]

_LOGS = [0, 6, 12, 20, 40]
_WATER_DAYS = [0.5, 2.0, 5.0, 15.0]
_FOOD_DAYS = [0.5, 2.0, 5.0, 15.0]
_POP = [(5, 0), (5, 4), (12, 0), (12, 4), (20, 0)]
_DROUGHT = [1.0, 3.0, 6.0]


def _iter_states() -> Iterable[dict]:
    for counts in _STAGES:
        for logs in _LOGS:
            for wd in _WATER_DAYS:
                for fd in _FOOD_DAYS:
                    for total, homeless in _POP:
                        for drought in _DROUGHT:
                            s = _base_state()
                            _set_counts(s, counts)
                            _set_resource(s, "Log", logs, logs / 3.0)
                            _set_resource(s, "Water", wd * 5, wd)
                            _set_resource(s, "Food", fd * 5, fd)
                            _set_pop(s, total, homeless)
                            _set_drought(s, drought)
                            yield s
    # a few explicit unreachable-building states so demolish_unreachable is learnable
    for counts in _STAGES[3:6]:
        s = _base_state()
        _set_counts(s, counts)
        _set_resource(s, "Log", 20, 6)
        _set_resource(s, "Water", 10, 2)
        _set_resource(s, "Food", 10, 2)
        _set_pop(s, 8, 0)
        _set_drought(s, 3.0)
        _inject_unreachable(s)
        yield s


def _harvest_journal_states() -> List[dict]:
    """Pull any full state snapshots recorded in journal/*.jsonl (best-effort)."""
    out: List[dict] = []
    jdir = _ROOT / "journal"
    if not jdir.is_dir():
        return out
    for path in sorted(jdir.glob("*.jsonl")):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or '"resources"' not in line:
                    continue
                rec = json.loads(line)
                state = rec.get("state") or rec.get("after_state") or rec.get("before_state")
                if isinstance(state, dict) and state.get("resources"):
                    out.append(state)
        except Exception:
            continue
    return out


def build(balance_cap: int = 0) -> Tuple[List[dict], List[str], List[str]]:
    oracle = Oracle()
    seen: set = set()
    rows: List[dict] = []

    states = list(_iter_states()) + _harvest_journal_states()
    for state in states:
        strings = feat.feature_strings(state)
        key = tuple(sorted(strings))
        if key in seen:
            continue
        seen.add(key)
        try:
            label = oracle.label(state)
        except Exception:
            continue
        rows.append({"features": strings, "label": label})

    # optional per-class cap so no single label dominates (keeps CART/MLP honest)
    if balance_cap:
        by_label: Dict[str, List[dict]] = {}
        for r in rows:
            by_label.setdefault(r["label"], []).append(r)
        capped: List[dict] = []
        for label, group in by_label.items():
            capped.extend(group[:balance_cap])
        rows = capped

    vocab = sorted({s for r in rows for s in r["features"]})
    labels = [a for a in feat.ACTIONS if any(r["label"] == a for r in rows)]
    return rows, vocab, labels


def main() -> None:
    _DATA.mkdir(parents=True, exist_ok=True)
    rows, vocab, labels = build()

    (_DATA / "decision_dataset.json").write_text(
        json.dumps(rows, indent=2), encoding="utf-8")
    (_DATA / "decision_vocab.json").write_text(
        json.dumps({"vocab": vocab}, indent=2), encoding="utf-8")
    (_DATA / "decision_labels.json").write_text(
        json.dumps(labels, indent=2), encoding="utf-8")

    dist: Dict[str, int] = {}
    for r in rows:
        dist[r["label"]] = dist.get(r["label"], 0) + 1
    print(f"rows={len(rows)}  vocab={len(vocab)}  labels={len(labels)}")
    for label in sorted(dist, key=lambda k: -dist[k]):
        print(f"  {label:24s} {dist[label]}")


if __name__ == "__main__":
    main()
