"""Run-to-run learning-loop orchestrator (Task 7b).

Plays the trained decision policy against the live bridge for N iterations and
makes the colony's play improve iteration-over-iteration: after each run it
classifies the outcome, folds the failing runs' credit-assignment corrections
back into the training set, retrains both decision heads, and lets the next run
pick up the new model. The metric trend (days_survived / peak_pop per iteration)
is the observable "the model improves run-to-run" signal that Task 8 consumes.

One iteration i (run_id = f"{base_run_id}_{i}", deterministic - no timestamps):

  1. PLAY      play_policy.run(cfg, run_id, max_cycles) - plays one colony,
               recording every cycle to agent/runs/<run_id>.jsonl via
               replay.record_step. DecisionPolicy.load() is called INSIDE run()
               every call, so a model retrained in a previous iteration is picked
               up automatically here - reload is IMPLICIT (see "Reload" below).
  2. SUMMARIZE replay.summarize_run(run_id) -> {days_survived, peak_pop,
               final_pop, ended, ..., reached_30_pop}.
  3. RELABEL   (trigger below) learn.build_augmented_dataset(window) folds the
               recent runs' outcome corrections into the live decision_dataset.json,
               OVERRIDING mislabeled feature vectors with the credit-assignment
               fix (a real run died/stalled here; this is what it should have done).
  4. RETRAIN   only if the relabel actually changed the dataset: run
               learn.retrain_command() as a subprocess and CHECK the exit status
               (raise on non-zero). This overwrites decision_cart/mlp.json.
  5. LOG       append {iter, run_id, days_survived, peak_pop, final_pop, ended,
               overridden, added_rows} to the in-memory trend AND to a JSONL log
               (default agent/runs/loop_trend.jsonl); print a one-line summary.
  6. STOP      break early when metrics["reached_30_pop"] (goal reached), else
               after `iterations` iterations.

Returns the trend list.

DESIGN DECISIONS
----------------
* RELABEL TRIGGER - relabel when the run FAILED (ended in {dead_thirst,
  dead_hunger, stalled}) OR its score is flat/regressing vs the best PRIOR
  iteration. Score = (peak_pop, days_survived), compared lexicographically:
  peak_pop leads because the terminal goal is pop>=30, with days_survived as the
  survival tiebreak. On iteration 0 there is no prior best, so only the failure
  branch can fire. A survivor that also IMPROVED on the best score is left alone
  (no relabel, no retrain) - that is the "getting better" path we must not perturb.

* RETRAIN GATE - a relabel that produced no actual change to the dataset
  (added_rows == 0 and overridden == 0 - e.g. a flat-but-alive run whose window
  holds no failures, so credit_assignment yields nothing) does NOT retrain:
  retraining identical data reproduces an identical model, so it is skipped as
  wasteful. Only a dataset that actually changed triggers a retrain.

* RUN_ID WINDOW - build_augmented_dataset is fed a bounded SLIDING WINDOW of the
  most recent `relabel_window` run_ids (default 5), in chronological order, NOT
  just the current run and NOT the full history. Chronological order means the
  most recent run's correction wins on a conflicting feature key (this closes the
  Task 7a "feed runs in chronological order so last == most recent" follow-up).
  Only failing runs actually contribute corrections (credit_assignment returns []
  for survivors), so a window of recent runs = "recent failures still worth
  remembering."

* BASE-DATASET STRATEGY (bounded growth) - every iteration augments from a FROZEN
  PRISTINE snapshot of the synthetic dataset, NEVER from the previous iteration's
  augmented output. The snapshot (decision_dataset.pristine.json) is captured
  once, the first time the loop runs, by copying the synthetic decision_dataset.json;
  thereafter it is reused verbatim. Each iteration REBUILDS the live
  decision_dataset.json = pristine + (this window's corrections). Because the window
  is bounded, the live dataset size is bounded (pristine + at most `relabel_window`
  runs' worth of <=lookback corrections) - it can never grow without bound the way
  "augment last iteration's already-augmented output again" would (Task 7a flagged
  exactly that unbounded growth). Corrections from runs that age out of the window
  are naturally forgotten - a bounded replay buffer, not an ever-growing log.

* RELOAD IS IMPLICIT - play_policy.run() calls DecisionPolicy.load() at the top of
  every run, and DecisionPolicy.load() re-reads decision_cart.json/decision_mlp.json
  from disk each call. So the retrain in iteration i (which overwrites those files)
  is automatically in force for iteration i+1's play - no explicit reload needed,
  and nothing is cached across iterations.

Run:  .venv/bin/python -m agent.run_loop --iterations 10 --base-run-id loop
Tests (mocks only - no live game, no real training, no network):
      .venv/bin/python -m unittest agent.test_run_loop -v
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple, Union

from agent import play, replay
from agent.nlp import learn, play_policy

_AGENT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _AGENT_DIR.parent
_DATA_DIR = _AGENT_DIR / "data"
_RUNS_DIR = _AGENT_DIR / "runs"

# In the REAL loop these MUST live in agent/data: train_cart.py/train_lidsnet.py
# read agent/data/decision_dataset.json (hardcoded), so the augmentation is only
# seen by training if the live dataset is written there.
_DEFAULT_DATASET = _DATA_DIR / "decision_dataset.json"
_DEFAULT_PRISTINE = _DATA_DIR / "decision_dataset.pristine.json"
_DEFAULT_TREND_LOG = _RUNS_DIR / "loop_trend.jsonl"

# ended-classifications (replay.summarize_run) that count as a failed run.
FAILURE_ENDINGS = frozenset({"dead_thirst", "dead_hunger", "stalled"})

# Sliding window of recent run_ids fed to build_augmented_dataset (bounded growth).
RELABEL_WINDOW = 5


def _score(metrics: dict) -> Tuple[float, float]:
    """Lexicographic run score: peak_pop leads (terminal goal is pop>=30),
    days_survived breaks ties (survive longer at equal peak = strictly better)."""
    return (float(metrics.get("peak_pop") or 0.0),
            float(metrics.get("days_survived") or 0.0))


def _ensure_pristine_base(dataset_path: Path, pristine_path: Path) -> None:
    """Freeze the synthetic dataset once so every iteration augments from a stable
    pristine base (the bounded-growth strategy). No-op if the snapshot already
    exists, or if there is no synthetic dataset yet to snapshot."""
    if pristine_path.exists():
        return
    if dataset_path.exists():
        pristine_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(dataset_path, pristine_path)
        play.log_stderr("run_loop: froze pristine base %s -> %s"
                        % (dataset_path.name, pristine_path.name))


def _run_retrain(host: Optional[str], cwd: Path) -> None:
    """Execute learn.retrain_command(host) as a shell subprocess and raise on a
    non-zero exit (a broken retrain must halt the loop loudly, not silently keep
    playing the old model)."""
    cmd = learn.retrain_command(host)
    play.log_stderr("run_loop: retraining (%s)" % ("local-MPS" if host is None else host))
    result = subprocess.run(cmd, shell=True, cwd=str(cwd),
                            capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip()[-800:]
        msg = "run_loop: retrain FAILED (exit %d): %s" % (result.returncode, tail)
        play.log_stderr(msg)
        raise RuntimeError(msg)


def run_learning_loop(
    cfg: dict,
    base_run_id: str,
    iterations: int,
    max_cycles: int = 40,
    relabel_window: int = RELABEL_WINDOW,
    retrain_host: Optional[str] = None,
    dataset_path: Optional[Union[str, Path]] = None,
    pristine_path: Optional[Union[str, Path]] = None,
    trend_log_path: Optional[Union[str, Path]] = None,
) -> List[dict]:
    """Run the play -> summarize -> relabel -> retrain loop for `iterations`
    iterations (or until reached_30_pop), returning the metric-trend list."""
    dataset_path = Path(dataset_path) if dataset_path else _DEFAULT_DATASET
    pristine_path = Path(pristine_path) if pristine_path else _DEFAULT_PRISTINE
    trend_log_path = Path(trend_log_path) if trend_log_path else _DEFAULT_TREND_LOG

    _ensure_pristine_base(dataset_path, pristine_path)

    trend: List[dict] = []
    all_run_ids: List[str] = []
    best_score: Optional[Tuple[float, float]] = None

    for i in range(max(0, iterations)):
        run_id = "%s_%d" % (base_run_id, i)
        all_run_ids.append(run_id)

        # 1. PLAY. Any retrain from a prior iteration is already on disk; the reload
        # is implicit inside play_policy.run (DecisionPolicy.load re-reads the files).
        play_policy.run(cfg, run_id, max_cycles)

        # 2. SUMMARIZE the recorded run.
        metrics = replay.summarize_run(run_id)
        score = _score(metrics)
        failed = metrics.get("ended") in FAILURE_ENDINGS
        # flat/regressing vs the best PRIOR iteration (best_score not yet updated
        # with this run's score, so this compares against strictly-earlier runs).
        regressed = best_score is not None and score <= best_score
        should_relabel = failed or regressed

        # 3. RELABEL: fold the recent window's outcome corrections into the live
        # dataset, always augmenting FROM the frozen pristine base (bounded growth).
        overridden = added_rows = 0
        if should_relabel:
            window = all_run_ids[-relabel_window:]
            stats = learn.build_augmented_dataset(window, base=pristine_path, out=dataset_path)
            overridden = int(stats.get("overridden") or 0)
            added_rows = int(stats.get("added_rows") or 0)

        # 5. LOG the trend BEFORE retraining, so a failing retrain still leaves this
        # iteration's outcome persisted on disk and in the returned list.
        entry = {
            "iter": i,
            "run_id": run_id,
            "days_survived": metrics.get("days_survived"),
            "peak_pop": metrics.get("peak_pop"),
            "final_pop": metrics.get("final_pop"),
            "ended": metrics.get("ended"),
            "overridden": overridden,
            "added_rows": added_rows,
        }
        trend.append(entry)
        play.journal_append(str(trend_log_path), entry)
        play.log_stderr(
            "run_loop iter %d | run=%s ended=%s days=%s peak=%s | relabel=%s (+%d rows, %d overridden)"
            % (i, run_id, entry["ended"], entry["days_survived"], entry["peak_pop"],
               should_relabel, added_rows, overridden))

        # 4. RETRAIN - only when the relabel actually changed the dataset (else the
        # model would come out identical). Reload is implicit next iteration.
        if should_relabel and (added_rows > 0 or overridden > 0):
            _run_retrain(retrain_host, _REPO_ROOT)

        # book-keeping: best score over iterations seen so far.
        best_score = score if best_score is None else max(best_score, score)

        # 6. STOP early once the colony hits the pop>=30 milestone.
        if metrics.get("reached_30_pop"):
            play.log_stderr("run_loop: reached_30_pop at iter %d - stopping" % i)
            break

    return trend


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Run-to-run learning loop orchestrator")
    parser.add_argument("--bridge", default=os.environ.get("BRIDGE_URL", "http://127.0.0.1:7744"))
    parser.add_argument("--base-run-id", default="loop")
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--cycles", type=int, default=40)
    parser.add_argument("--window", type=int, default=RELABEL_WINDOW)
    parser.add_argument("--retrain-host", default=None,
                        help="None -> local MPS retrain (default); 'cka-win' -> remote GPU box")
    args = parser.parse_args(argv)

    trend = run_learning_loop(
        {"BRIDGE_URL": args.bridge}, args.base_run_id, args.iterations,
        max_cycles=args.cycles, relabel_window=args.window, retrain_host=args.retrain_host,
    )
    play.log_stderr("run_loop: done, %d iterations" % len(trend))
    for entry in trend:
        play.log_stderr("  iter %(iter)s: ended=%(ended)s days=%(days_survived)s "
                        "peak=%(peak_pop)s (+%(added_rows)s rows)" % entry)


if __name__ == "__main__":
    main()
