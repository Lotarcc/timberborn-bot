"""Tests for agent/run_loop.py - the run-to-run learning-loop orchestrator.

Fully hermetic: NO live game, NO real training, NO network.

  * play_policy.run is MONKEYPATCHED with a scripted fake that writes a canned
    trace (thirst-death / improving-survivor / etc.) via the REAL
    replay.record_step and returns a summary - so replay.summarize_run reads a
    REAL recorded run and classifies it for real (the outcome logic is not
    stubbed, only the "play a live colony" part is).
  * subprocess.run is MONKEYPATCHED to a no-op returning success, so the retrain
    command is never actually executed.
  * learn.build_augmented_dataset is spied (wraps the real function) against
    temp base/out paths, so the override-merge really runs but agent/data is
    never touched.

Runnable BOTH ways:
    .venv/bin/python -m unittest agent.test_run_loop -v
    .venv/bin/python -m unittest discover -s agent
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent import replay, run_loop

# Reuse replay.py's own shared fixture shapes (plain data, documented reusable).
_state = replay.ReplayTests._state
_THIRST_DEATH_STEPS = replay.ReplayTests._THIRST_DEATH_STEPS
_SURVIVOR_STEPS = replay.ReplayTests._SURVIVOR_STEPS  # peaks pop 32 -> reached_30_pop


def _write_trace(run_id, steps):
    for i, (state_kwargs, action) in enumerate(steps, start=1):
        replay.record_step(run_id, i, _state(**state_kwargs), action)


def _alive_trace(peak, days):
    """A run that ends 'alive' with the given peak population and days_survived:
    water/food stay healthy, population never hits zero, and log stock rises every
    step so the stall detector never trips. peak stays < 30 (no reached_30_pop)."""
    counts = {"DistrictCenter.Folktails": 1}
    steps = []
    log = 0
    for d in range(1, days + 1):
        if d == 2:
            counts = dict(counts, **{"LumberjackFlag.Folktails": 1})
            action = "build_lumberjack_flag"
        elif d == 3:
            counts = dict(counts, **{"WaterPump.Folktails": 1})
            action = "build_water_pump"
        else:
            action = "advance_time"
        log += 5
        pop = min(peak, 10 + d)
        steps.append((dict(day=d, hour=8, pop=pop, homeless=max(0, peak - pop),
                           water_days=6.0, food_days=6.0, log=log, plank=0,
                           counts=dict(counts)), action))
    steps[-1][0]["pop"] = peak
    steps[-1][0]["homeless"] = 0
    return steps


class _ScriptedPlay:
    """Stands in for play_policy.run. Consumes one canned trace per call (in order),
    writes it under the run_id the loop passes, and returns a minimal summary.
    Records every (run_id, max_cycles) call so tests can assert determinism, and
    appends each run_id to `sink` for tearDown cleanup."""

    def __init__(self, traces, sink):
        self._traces = list(traces)
        self._sink = sink
        self.calls = []

    def __call__(self, cfg, run_id, max_cycles=40):
        self.calls.append((run_id, max_cycles))
        self._sink.append(run_id)
        steps = self._traces.pop(0)
        _write_trace(run_id, steps)
        return {"run_id": run_id, "event": "run_end", "cycles": len(steps)}


class _LoopTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="run_loop_test_")
        self._created_run_ids = []
        self.dataset_path = Path(self._tmp) / "decision_dataset.json"
        self.pristine_path = Path(self._tmp) / "decision_dataset.pristine.json"
        self.trend_path = Path(self._tmp) / "loop_trend.jsonl"
        self.base_run_id = "ut_loop_%s" % uuid.uuid4().hex[:8]

    def tearDown(self):
        for run_id in self._created_run_ids:
            try:
                os.remove(replay._run_path(run_id))
            except OSError:
                pass
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_pristine(self, rows=None):
        """A tiny pristine synthetic base whose single row can never collide with
        any feature vector feature_strings() emits (closed vocabulary)."""
        if rows is None:
            rows = [{"features": ["zzz_never=collides"], "label": "advance_time"}]
        self.pristine_path.write_text(json.dumps(rows), encoding="utf-8")

    def _ok_subprocess(self):
        return mock.MagicMock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))

    def _scripted(self, traces):
        return _ScriptedPlay(traces, self._created_run_ids)

    def _run(self, scripted, subproc, iterations, **kwargs):
        with mock.patch.object(run_loop.play_policy, "run", scripted), \
             mock.patch.object(run_loop.subprocess, "run", subproc), \
             mock.patch.object(run_loop.learn, "build_augmented_dataset",
                               wraps=run_loop.learn.build_augmented_dataset) as build_spy:
            trend = run_loop.run_learning_loop(
                {"BRIDGE_URL": "http://test"}, self.base_run_id, iterations,
                max_cycles=7, dataset_path=self.dataset_path,
                pristine_path=self.pristine_path, trend_log_path=self.trend_path,
                **kwargs)
        return trend, build_spy


class DeterministicIdsTests(_LoopTestBase):
    def test_run_ids_are_base_indexed_and_summarize_runs_each_iter(self):
        scripted = self._scripted([_alive_trace(12, 4), _alive_trace(14, 5), _alive_trace(16, 6)])
        subproc = self._ok_subprocess()

        with mock.patch.object(replay, "summarize_run",
                               wraps=replay.summarize_run) as summ_spy:
            trend, build_spy = self._run(scripted, subproc, iterations=3)

        # (e-prereq) deterministic ids: base_0, base_1, base_2 - no timestamps/random.
        self.assertEqual([rid for rid, _ in scripted.calls],
                         ["%s_%d" % (self.base_run_id, i) for i in range(3)])
        # (a) summarize called once per iteration.
        self.assertEqual(summ_spy.call_count, 3)
        # (d) trend recorded and returned, one entry per iter, with the metric fields.
        self.assertEqual(len(trend), 3)
        self.assertEqual([e["peak_pop"] for e in trend], [12, 14, 16])
        self.assertEqual([e["days_survived"] for e in trend], [4, 5, 6])
        for e in trend:
            self.assertEqual(e["ended"], "alive")
        # improving survivors never fail and never regress -> no relabel, no retrain.
        build_spy.assert_not_called()
        subproc.assert_not_called()


class FailureTriggersRetrainTests(_LoopTestBase):
    def test_failed_run_relabels_window_and_retrains(self):
        self._write_pristine()
        scripted = self._scripted([_THIRST_DEATH_STEPS])
        subproc = self._ok_subprocess()

        trend, build_spy = self._run(scripted, subproc, iterations=1)

        rid0 = "%s_0" % self.base_run_id
        # (b) build_augmented_dataset called once, with the current run's window,
        # augmenting FROM the frozen pristine base INTO the live dataset.
        build_spy.assert_called_once()
        call = build_spy.call_args
        self.assertEqual(list(call.args[0]), [rid0])
        self.assertEqual(call.kwargs["base"], self.pristine_path)
        self.assertEqual(call.kwargs["out"], self.dataset_path)

        # (b) the retrain subprocess ran once, with the local train_cart+train_lidsnet
        # command, as a shell command.
        subproc.assert_called_once()
        cmd = subproc.call_args.args[0]
        self.assertIn("agent.nlp.train_cart", cmd)
        self.assertIn("agent.nlp.train_lidsnet", cmd)
        self.assertTrue(subproc.call_args.kwargs.get("shell"))

        # (d) trend records the failure + how many rows the relabel produced.
        self.assertEqual(len(trend), 1)
        self.assertEqual(trend[0]["ended"], "dead_thirst")
        self.assertGreater(trend[0]["added_rows"], 0)
        # the augmented live dataset was actually written (pristine + corrections).
        self.assertTrue(self.dataset_path.exists())
        out_rows = json.loads(self.dataset_path.read_text(encoding="utf-8"))
        self.assertGreater(len(out_rows), 1)

        # trend was persisted to JSONL too.
        self.assertTrue(self.trend_path.exists())
        logged = [json.loads(l) for l in self.trend_path.read_text().splitlines() if l.strip()]
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0]["ended"], "dead_thirst")

    def test_retrain_nonzero_exit_raises(self):
        self._write_pristine()
        scripted = self._scripted([_THIRST_DEATH_STEPS])
        subproc = mock.MagicMock(return_value=SimpleNamespace(returncode=1, stdout="", stderr="boom"))

        with self.assertRaises(RuntimeError):
            self._run(scripted, subproc, iterations=1)
        subproc.assert_called_once()


class SuccessSkipsRetrainTests(_LoopTestBase):
    def test_improving_survivor_after_a_failure_does_not_retrain_again(self):
        self._write_pristine()
        # iter0 fails (thirst) -> relabel + retrain; iter1 is alive AND beats iter0's
        # score -> succeeded + improved -> NO relabel, NO retrain.
        scripted = self._scripted([_THIRST_DEATH_STEPS, _alive_trace(14, 6)])
        subproc = self._ok_subprocess()

        trend, build_spy = self._run(scripted, subproc, iterations=2)

        # (c) exactly one relabel + one retrain across both iters - only iter0.
        build_spy.assert_called_once()
        self.assertEqual(list(build_spy.call_args.args[0]), ["%s_0" % self.base_run_id])
        subproc.assert_called_once()

        self.assertEqual(len(trend), 2)
        self.assertEqual(trend[0]["ended"], "dead_thirst")
        self.assertEqual(trend[1]["ended"], "alive")
        # the improving survivor's iteration relabeled nothing.
        self.assertEqual(trend[1]["added_rows"], 0)
        self.assertEqual(trend[1]["overridden"], 0)
        # (d) days_survived / peak_pop trend is UP from the failure to the survivor.
        self.assertGreater(trend[1]["peak_pop"], trend[0]["peak_pop"])
        self.assertGreater(trend[1]["days_survived"], trend[0]["days_survived"])


class FlatTrendTriggersRelabelTests(_LoopTestBase):
    def test_regressing_alive_run_relabels_but_skips_empty_retrain(self):
        self._write_pristine()
        # iter0: strong survivor (peak14, 5 days) -> sets best, no relabel (alive,
        # no prior best). iter1: WORSE survivor (peak12, 3 days) -> alive but its
        # score regresses vs best -> the flat/regress branch fires the relabel...
        scripted = self._scripted([_alive_trace(14, 5), _alive_trace(12, 3)])
        subproc = self._ok_subprocess()

        trend, build_spy = self._run(scripted, subproc, iterations=2)

        # relabel attempted exactly once (on the regressing iter1), over the recent
        # window [base_0, base_1].
        build_spy.assert_called_once()
        self.assertEqual(list(build_spy.call_args.args[0]),
                         ["%s_0" % self.base_run_id, "%s_1" % self.base_run_id])
        # ...but the window holds only survivors (no credit-assignment corrections),
        # so nothing changed -> the retrain is correctly skipped.
        self.assertEqual(trend[1]["added_rows"], 0)
        self.assertEqual(trend[1]["overridden"], 0)
        subproc.assert_not_called()


class StopEarlyOnGoalTests(_LoopTestBase):
    def test_reached_30_pop_stops_before_remaining_iterations(self):
        # iter0 reaches pop 32 (reached_30_pop) -> stop, even though 5 were requested.
        scripted = self._scripted([_SURVIVOR_STEPS, _alive_trace(12, 3), _alive_trace(12, 3)])
        subproc = self._ok_subprocess()

        trend, build_spy = self._run(scripted, subproc, iterations=5)

        # (e) only the first iteration ran.
        self.assertEqual(len(scripted.calls), 1)
        self.assertEqual(len(trend), 1)
        self.assertEqual(trend[0]["peak_pop"], 32)
        # a clean 30-pop survivor neither failed nor regressed -> no relabel/retrain.
        build_spy.assert_not_called()
        subproc.assert_not_called()


class WindowIsBoundedTests(_LoopTestBase):
    def test_relabel_window_slices_to_the_most_recent_run_ids(self):
        self._write_pristine()
        # Three consecutive thirst deaths; a window of 2 must feed only the two most
        # recent run_ids to build_augmented_dataset on the third iteration (bounded
        # growth: old runs age out of the window).
        scripted = self._scripted([_THIRST_DEATH_STEPS, _THIRST_DEATH_STEPS, _THIRST_DEATH_STEPS])
        subproc = self._ok_subprocess()

        trend, build_spy = self._run(scripted, subproc, iterations=3, relabel_window=2)

        self.assertEqual(build_spy.call_count, 3)
        windows = [list(c.args[0]) for c in build_spy.call_args_list]
        b = self.base_run_id
        self.assertEqual(windows[0], ["%s_0" % b])
        self.assertEqual(windows[1], ["%s_0" % b, "%s_1" % b])
        self.assertEqual(windows[2], ["%s_1" % b, "%s_2" % b])  # base_0 aged out


if __name__ == "__main__":
    unittest.main()
