"""Run-to-run learning-loop orchestrator (Task 7b; stall-driven routing per
docs/kb/learning-loop-design.md).

Plays the trained decision policy against the live bridge for N iterations and
makes the colony's play improve iteration-over-iteration: after each run it
CLASSIFIES why the run ended the way it did, routes to the response that class
actually supports (relabel+retrain vs. flag-a-structural-gap), and lets the next
run pick up whatever changed. The metric trend (days_survived / peak_pop per
iteration) is the observable "the model improves run-to-run" signal that Task 8
consumes; the trend log's `stall_class`/`gap_flagged` fields are what let a human
(or a later analysis pass) tell "flat because we need more data" apart from
"flat because of a structural ceiling" without re-deriving it.

One iteration i (run_id = f"{base_run_id}_{i}", deterministic - no timestamps):

  1. PLAY       play_policy.run(cfg, run_id, max_cycles) - plays one colony,
                recording every cycle to agent/runs/<run_id>.jsonl via
                replay.record_step (which now also ends the run EARLY, in-run,
                on its own stall/death streak - see replay.progress_signal).
                DecisionPolicy.load() is called INSIDE run() every call, so a
                model retrained in a previous iteration is picked up
                automatically here - reload is IMPLICIT (see "Reload" below).
  2. SUMMARIZE  replay.summarize_run(run_id) -> {days_survived, peak_pop,
                final_pop, ended, ..., reached_30_pop}; for a run that didn't
                end 'alive', replay.classify_stall(run_id) additionally
                diagnoses WHY: RESOURCE_STARVED / POLICY_GAP (relabel-able) or
                STRUCTURAL_GAP (the real expert also had nothing to propose -
                no correct label exists).
  3. ROUTE      STRUCTURAL_GAP -> NO relabel; instead
                learn.gap_lesson_from_diagnosis + coach.update_playbook write/
                merge a lesson into the playbook (evidence-weighted, persists
                and escalates across recurrences - see coach.py). Anything else
                -> the pre-existing relabel trigger (failed or regressed vs the
                best prior iteration).
  4. RELABEL    (when routed to it) learn.build_augmented_dataset(window) folds
                the recent runs' outcome corrections into the live
                decision_dataset.json, OVERRIDING mislabeled feature vectors
                with the credit-assignment fix - which now itself prefers the
                real expert's simultaneous opinion (meta.expert_top, true
                DAgger) over the older hand-written heuristic.
  5. LOG        append {iter, run_id, days_survived, peak_pop, final_pop, ended,
                overridden, added_rows, stall_class, gap_flagged} to the
                in-memory trend AND to a JSONL log (default
                agent/runs/loop_trend.jsonl); print a one-line summary. Logged
                BEFORE retraining, so a failing retrain still leaves this
                iteration's outcome persisted on disk and in the returned list.
  6. RETRAIN    only if the relabel actually changed the dataset: run
                learn.retrain_command() as a subprocess and CHECK the exit
                status (raise on non-zero). This overwrites decision_cart/
                mlp.json.
  7. STOP       break early when metrics["reached_30_pop"] (goal reached), when
                the trend has PLATEAUED (best score hasn't improved for
                `plateau_patience` iterations) AND every one of those recent
                iterations was STRUCTURAL_GAP (retraining can't fix that -
                grinding more iterations is pure waste), else after
                `iterations` iterations.

Returns the trend list.

DESIGN DECISIONS
----------------
* CLASS-BASED ROUTING (replaces the old binary "failed or regressed" gate) -
  a run that ended badly is classified by replay.classify_stall before
  deciding what to do about it:
    - RESOURCE_STARVED (died of thirst/hunger) or POLICY_GAP (stalled/regressed
      AND the real expert still had a concrete move somewhere in the trailing
      window) -> relabel-able, same trigger condition as before (failed OR
      regressed vs. best prior iteration).
    - STRUCTURAL_GAP (stalled/regressed AND the expert's OWN telemetry says
      "advance_time" throughout the window - it also had nothing to do) ->
      relabeling would teach the clone a falsehood (there is no correct label),
      so this NEVER relabels/retrains. Instead it writes an evidence-weighted
      lesson to the playbook via coach.py's existing reconcile/confidence/prune
      machinery, so the gap is surfaced (and escalates the more it recurs)
      instead of silently discarded. See docs/kb/learning-loop-design.md SS4/SS5.

* RETRAIN GATE (unchanged) - a relabel that produced no actual change to the
  dataset (added_rows == 0 and overridden == 0 - e.g. a flat-but-alive run whose
  window holds no failures, so credit_assignment yields nothing) does NOT
  retrain: retraining identical data reproduces an identical model, so it is
  skipped as wasteful. Only a dataset that actually changed triggers a retrain.

* RUN_ID WINDOW (unchanged) - build_augmented_dataset is fed a bounded SLIDING
  WINDOW of the most recent `relabel_window` run_ids (default 5), in
  chronological order, NOT just the current run and NOT the full history.
  Chronological order means the most recent run's correction wins on a
  conflicting feature key. Only failing runs actually contribute corrections
  (credit_assignment returns [] for survivors AND for STRUCTURAL_GAP runs), so
  a window of recent runs = "recent relabel-able failures still worth
  remembering."

* BASE-DATASET STRATEGY (bounded growth, unchanged) - every iteration augments
  from a FROZEN PRISTINE snapshot of the synthetic dataset, NEVER from the
  previous iteration's augmented output. The snapshot
  (decision_dataset.pristine.json) is captured once, the first time the loop
  runs, by copying the synthetic decision_dataset.json; thereafter it is reused
  verbatim. Each iteration REBUILDS the live decision_dataset.json = pristine +
  (this window's corrections). Because the window is bounded, the live dataset
  size is bounded - it can never grow without bound. Corrections from runs that
  age out of the window are naturally forgotten - a bounded replay buffer, not
  an ever-growing log.

* RELOAD IS IMPLICIT (unchanged) - play_policy.run() calls DecisionPolicy.load()
  at the top of every run, and DecisionPolicy.load() re-reads decision_cart.json/
  decision_mlp.json from disk each call. So the retrain in iteration i (which
  overwrites those files) is automatically in force for iteration i+1's play -
  no explicit reload needed, and nothing is cached across iterations. The
  playbook (agent/playbook.json by default) is likewise just read fresh by
  whatever consumes it next - this loop only writes it.

* PLATEAU STOP (new) - a flat trend caused by RESOURCE_STARVED/POLICY_GAP runs
  still has a plausible next-iteration payoff (more corrected data), so the loop
  keeps spending iterations on those. A flat trend where EVERY ONE of the last
  `plateau_patience` iterations was STRUCTURAL_GAP cannot be fixed by another
  retrain (there was never a label to learn from), so the loop stops instead of
  grinding - see run_stderr for the "see the playbook" pointer.

Deliberately NOT built here (see docs/kb/learning-loop-design.md, separate
follow-up): bounded exploration retry before conceding STRUCTURAL_GAP (SS5.4),
a champion/challenger retrain gate (SS5.6), curriculum ranking-penalty feedback
(SS5.7).

Run:  .venv/bin/python -m agent.run_loop --iterations 10 --base-run-id loop
Tests (mocks only - no live game, no real training, no network):
      .venv/bin/python -m unittest agent.test_run_loop -v
      .venv/bin/python -m unittest agent.test_learning_loop -v
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple, Union

from agent import play, replay
# `coach` does a BARE `import metrics as metrics_mod` internally (it's designed to
# run standalone too, see agent/coach.py's header) - that only resolves once
# agent/ itself is on sys.path, which `agent.play`'s own import (line above)
# already guarantees (play.py inserts its own AGENT_DIR at import time). Import
# order here matters: coach must be imported AFTER play, not combined into one
# `from agent import ...` statement (fromlist imports left-to-right - listing
# coach first would import it before play.py's sys.path insert has run).
from agent import coach
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
_DEFAULT_PLAYBOOK = coach.DEFAULT_PLAYBOOK

# ended-classifications (replay.summarize_run) that count as a failed run.
FAILURE_ENDINGS = frozenset({"dead_thirst", "dead_hunger", "stalled"})

# Sliding window of recent run_ids fed to build_augmented_dataset (bounded growth).
RELABEL_WINDOW = 5

# Iterations without a best-score improvement before the plateau check considers
# stopping (only actually stops if every one of those iterations was
# STRUCTURAL_GAP - see run_learning_loop's "PLATEAU STOP" design note above).
PLATEAU_PATIENCE = 5


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
    playbook_path: Optional[Union[str, Path]] = None,
    plateau_patience: int = PLATEAU_PATIENCE,
) -> List[dict]:
    """Run the play -> summarize -> classify -> route(relabel+retrain, or flag a
    structural gap) loop for `iterations` iterations - or until reached_30_pop,
    or a structural-gap plateau (see the module docstring's PLATEAU STOP note) -
    returning the metric-trend list."""
    dataset_path = Path(dataset_path) if dataset_path else _DEFAULT_DATASET
    pristine_path = Path(pristine_path) if pristine_path else _DEFAULT_PRISTINE
    trend_log_path = Path(trend_log_path) if trend_log_path else _DEFAULT_TREND_LOG
    playbook_path = Path(playbook_path) if playbook_path else _DEFAULT_PLAYBOOK

    _ensure_pristine_base(dataset_path, pristine_path)

    trend: List[dict] = []
    all_run_ids: List[str] = []
    best_score: Optional[Tuple[float, float]] = None
    stall_streak = 0  # iterations since best_score last improved (plateau check)

    for i in range(max(0, iterations)):
        run_id = "%s_%d" % (base_run_id, i)
        all_run_ids.append(run_id)

        # 1. PLAY. Any retrain from a prior iteration is already on disk; the reload
        # is implicit inside play_policy.run (DecisionPolicy.load re-reads the files).
        play_policy.run(cfg, run_id, max_cycles)

        # 2. SUMMARIZE + CLASSIFY. classify_stall is only meaningful (and only
        # worth the extra file rescan) for a run that didn't end 'alive' - it
        # returns None for a living run anyway, so skip the call entirely then.
        metrics = replay.summarize_run(run_id)
        ended = metrics.get("ended")
        # classify_stall's OWN lookback default (6 rows within THIS run) and
        # relabel_window (default 5 run_ids across THE LOOP) are different
        # windows over different things that happen to default to similar
        # sizes - deliberately NOT passing relabel_window through here so they
        # stay independently tunable instead of silently conflated.
        diagnosis = replay.classify_stall(run_id) if ended != "alive" else None
        score = _score(metrics)
        # flat/regressing vs the best PRIOR iteration (best_score not yet updated
        # with this run's score, so this compares against strictly-earlier runs).
        regressed = best_score is not None and score <= best_score

        # 3. ROUTE: STRUCTURAL_GAP (the real expert also proposed nothing at
        # every window row - no correct label exists) never relabels; flag it
        # into the playbook instead (coach.py's existing reconcile/confidence/
        # prune machinery - evidence accumulates and escalates across
        # recurrences of the SAME gap, see coach._STRUCTURAL_GAP_TRIGGER_PREFIX).
        # Everything else keeps the original failed-or-regressed relabel trigger.
        gap_flagged = False
        if diagnosis is not None and diagnosis["class"] == replay.STRUCTURAL_GAP:
            lesson = learn.gap_lesson_from_diagnosis(diagnosis, run_id)
            coach.update_playbook(playbook_path, [lesson], run_id)
            gap_flagged = True
            should_relabel = False
        else:
            should_relabel = (ended in FAILURE_ENDINGS) or regressed

        # 4. RELABEL: fold the recent window's outcome corrections into the live
        # dataset, always augmenting FROM the frozen pristine base (bounded growth).
        overridden = added_rows = 0
        if should_relabel:
            window = all_run_ids[-relabel_window:]
            stats = learn.build_augmented_dataset(window, base=pristine_path, out=dataset_path)
            overridden = int(stats.get("overridden") or 0)
            added_rows = int(stats.get("added_rows") or 0)

        # 5. LOG the trend BEFORE retraining, so a failing retrain still leaves this
        # iteration's outcome persisted on disk and in the returned list.
        stall_class = diagnosis["class"] if diagnosis is not None else None
        entry = {
            "iter": i,
            "run_id": run_id,
            "days_survived": metrics.get("days_survived"),
            "peak_pop": metrics.get("peak_pop"),
            "final_pop": metrics.get("final_pop"),
            "ended": ended,
            "overridden": overridden,
            "added_rows": added_rows,
            "stall_class": stall_class,
            "gap_flagged": gap_flagged,
        }
        trend.append(entry)
        play.journal_append(str(trend_log_path), entry)
        play.log_stderr(
            "run_loop iter %d | run=%s ended=%s class=%s days=%s peak=%s | relabel=%s "
            "(+%d rows, %d overridden)%s"
            % (i, run_id, entry["ended"], stall_class, entry["days_survived"], entry["peak_pop"],
               should_relabel, added_rows, overridden, " [gap flagged]" if gap_flagged else ""))

        # 6. RETRAIN - only when the relabel actually changed the dataset (else the
        # model would come out identical). Reload is implicit next iteration.
        if should_relabel and (added_rows > 0 or overridden > 0):
            _run_retrain(retrain_host, _REPO_ROOT)

        # book-keeping: best score over iterations seen so far, and the plateau
        # streak (iterations since it last improved - reset to 0 on improvement).
        improved = best_score is None or score > best_score
        stall_streak = 0 if improved else stall_streak + 1
        best_score = score if best_score is None else max(best_score, score)

        # 7a. STOP early once the colony hits the pop>=30 milestone.
        if metrics.get("reached_30_pop"):
            play.log_stderr("run_loop: reached_30_pop at iter %d - stopping" % i)
            break

        # 7b. STOP if the trend has plateaued (no score improvement for
        # plateau_patience iterations) AND every one of those recent iterations
        # was STRUCTURAL_GAP: retraining cannot fix that (there was never a
        # label to learn from), so grinding more iterations is pure waste. A
        # plateau caused by RESOURCE_STARVED/POLICY_GAP runs keeps going - more
        # corrected data still has a plausible payoff there.
        if stall_streak >= plateau_patience:
            recent = trend[-plateau_patience:]
            if all(e.get("stall_class") == replay.STRUCTURAL_GAP for e in recent):
                play.log_stderr(
                    "run_loop: plateaued %d iterations, all structural_gap - stopping "
                    "(retraining cannot fix this; see %s)" % (plateau_patience, playbook_path))
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
    parser.add_argument("--playbook", default=str(_DEFAULT_PLAYBOOK),
                        help="where structural-gap lessons are written (default agent/playbook.json)")
    parser.add_argument("--plateau-patience", type=int, default=PLATEAU_PATIENCE,
                        help="stop after this many score-flat iterations IF all of them "
                             "were structural_gap (retraining can't fix that)")
    args = parser.parse_args(argv)

    trend = run_learning_loop(
        {"BRIDGE_URL": args.bridge}, args.base_run_id, args.iterations,
        max_cycles=args.cycles, relabel_window=args.window, retrain_host=args.retrain_host,
        playbook_path=args.playbook, plateau_patience=args.plateau_patience,
    )
    play.log_stderr("run_loop: done, %d iterations" % len(trend))
    for entry in trend:
        play.log_stderr("  iter %(iter)s: ended=%(ended)s class=%(stall_class)s "
                        "days=%(days_survived)s peak=%(peak_pop)s (+%(added_rows)s rows)" % entry)
    gap_lessons = sum(1 for e in trend if e.get("gap_flagged"))
    if gap_lessons:
        play.log_stderr(
            "run_loop: %d iteration(s) flagged a structural-gap lesson - see %s"
            % (gap_lessons, args.playbook))


if __name__ == "__main__":
    main()
