"""Tests for the stall-driven routing added to agent/run_loop.py + agent/coach.py
(docs/kb/learning-loop-design.md SS4/SS5.3/SS5.5/SS5.8/SS5.9):

  * a STRUCTURAL_GAP run (the real deterministic expert also proposed nothing at
    every window row) must NOT relabel/retrain - instead it writes/merges a
    "structural_gap:<tag>" lesson into the playbook via coach.update_playbook.
  * a POLICY_GAP run (the expert still saw a concrete move) keeps relabeling and
    retraining, same as before this design.
  * a run of the SAME structural gap recurring across iterations accumulates
    evidence on ONE playbook lesson (coach.reconcile's dedup), not many.
  * a plateau (score not improving) where every recent iteration was
    STRUCTURAL_GAP stops the loop early; the same plateau shape with
    POLICY_GAP/RESOURCE_STARVED runs does NOT stop early (more corrected data
    still has a plausible payoff there).

Fully hermetic - reuses agent/test_run_loop.py's _LoopTestBase (temp dataset/
pristine/trend paths, scripted play_policy.run, mocked subprocess, run_id
cleanup) and adds a temp playbook path + a meta-carrying scripted-play variant:
the existing _ScriptedPlay/_write_trace helpers don't thread per-step `meta`,
which classify_stall needs to tell POLICY_GAP apart from STRUCTURAL_GAP.

Runnable BOTH ways:
    .venv/bin/python -m unittest agent.test_learning_loop -v
    .venv/bin/python -m unittest discover -s agent
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from agent import replay
from agent.test_run_loop import _LoopTestBase, _state

# One build step (establishes buildings/log so the trailing advance_time streak
# reads as "no progress" from the next row onward - see replay.py's _scan/
# progress_signal no_progress condition) followed by 8 IDENTICAL advance_time
# steps -> _STALL_STREAK(8) trips at the 9th recorded row, ended == "stalled".
# WaterPump/GathererFlag/LumberjackFlag are all already built so the OLD (pre-
# DAgger) heuristic would have nothing to recommend either - this trace is only
# relabel-able at all because of what meta.expert_top says, which is exactly
# the thing under test here.
_STALL_COUNTS = {
    "DistrictCenter.Folktails": 1,
    "WaterPump.Folktails": 1,
    "GathererFlag.Folktails": 1,
    "LumberjackFlag.Folktails": 1,
}


def _stall_trace(expert_top, policy_top=None):
    """A stalled-run trace where every advance_time row carries the given
    meta.expert_top - the DAgger signal classify_stall keys its class on.
    `policy_top`, when given, is what the policy actually kept trying (feeds
    classify_stall's repeated_action -> gap_lesson_from_diagnosis's tag)."""
    steps = [
        (dict(day=1, hour=8, pop=5, homeless=5, water_days=5.0, food_days=5.0,
              log=5, plank=0, counts=_STALL_COUNTS), "build_lumberjack_flag", None),
    ]
    for i in range(8):
        meta = {"expert_top": expert_top, "executed": False}
        if policy_top:
            meta["policy_top"] = policy_top
        steps.append((
            dict(day=1 + i, hour=8, pop=5, homeless=5, water_days=5.0, food_days=5.0,
                 log=5, plank=0, counts=_STALL_COUNTS), "advance_time", meta,
        ))
    return steps


def _write_meta_trace(run_id, steps):
    """Like agent.test_run_loop's module-level _write_trace, but each step is
    (state_kwargs, action, meta) - needed to control meta.expert_top per row."""
    for i, (state_kwargs, action, meta) in enumerate(steps, start=1):
        replay.record_step(run_id, i, _state(**state_kwargs), action, meta=meta)


class _MetaScriptedPlay:
    """Like agent.test_run_loop._ScriptedPlay, but writes meta-carrying traces
    (via _write_meta_trace) so classify_stall sees the meta.expert_top/
    policy_top each test controls. Same call contract as _ScriptedPlay so it
    drops straight into _LoopTestBase._run's play_policy.run monkeypatch."""

    def __init__(self, traces, sink):
        self._traces = list(traces)
        self._sink = sink
        self.calls = []

    def __call__(self, cfg, run_id, max_cycles=40):
        self.calls.append((run_id, max_cycles))
        self._sink.append(run_id)
        steps = self._traces.pop(0)
        _write_meta_trace(run_id, steps)
        return {"run_id": run_id, "event": "run_end", "cycles": len(steps)}


class _GapLoopTestBase(_LoopTestBase):
    def setUp(self):
        super().setUp()
        self.playbook_path = Path(self._tmp) / "playbook.json"

    def _meta_scripted(self, traces):
        return _MetaScriptedPlay(traces, self._created_run_ids)


class StructuralGapRoutingTests(_GapLoopTestBase):
    def test_structural_gap_run_flags_a_lesson_instead_of_relabeling(self):
        scripted = self._meta_scripted([_stall_trace("advance_time", policy_top="build_dam")])
        subproc = self._ok_subprocess()

        trend, build_spy = self._run(
            scripted, subproc, iterations=1, playbook_path=self.playbook_path)

        self.assertEqual(len(trend), 1)
        self.assertEqual(trend[0]["ended"], "stalled")
        self.assertEqual(trend[0]["stall_class"], replay.STRUCTURAL_GAP)
        self.assertTrue(trend[0]["gap_flagged"])
        # No correct label exists for this class - the pre-existing
        # failed-ending relabel trigger must NOT fire, even though "stalled"
        # is a FAILURE_ENDINGS member.
        self.assertEqual(trend[0]["added_rows"], 0)
        self.assertEqual(trend[0]["overridden"], 0)
        build_spy.assert_not_called()
        subproc.assert_not_called()

        self.assertTrue(self.playbook_path.exists())
        playbook = json.loads(self.playbook_path.read_text(encoding="utf-8"))
        gap_lessons = [
            l for l in playbook["lessons"] if l["trigger"].startswith("structural_gap:")
        ]
        self.assertEqual(len(gap_lessons), 1)
        self.assertEqual(gap_lessons[0]["trigger"], "structural_gap:build_dam")
        self.assertEqual(gap_lessons[0]["evidence"], {"runs": 1, "wins": 0, "losses": 1})

    def test_policy_gap_run_still_relabels_and_retrains(self):
        self._write_pristine()
        scripted = self._meta_scripted([_stall_trace("build_lumber_mill")])
        subproc = self._ok_subprocess()

        trend, build_spy = self._run(
            scripted, subproc, iterations=1, playbook_path=self.playbook_path)

        self.assertEqual(trend[0]["stall_class"], replay.POLICY_GAP)
        self.assertFalse(trend[0]["gap_flagged"])
        build_spy.assert_called_once()
        subproc.assert_called_once()
        self.assertGreater(trend[0]["added_rows"], 0)

        # A relabel-able class never touches the playbook.
        self.assertFalse(self.playbook_path.exists())

    def test_structural_gap_lesson_accumulates_evidence_across_recurrences(self):
        # Two SEPARATE iterations that both hit the SAME structural gap
        # (policy_top="build_dam" both times) must accumulate into ONE lesson
        # with evidence.runs == 2, not two separate lessons - coach.reconcile's
        # dedup-by-(trigger, action) key, exercised end to end through run_loop.
        scripted = self._meta_scripted([
            _stall_trace("advance_time", policy_top="build_dam"),
            _stall_trace("advance_time", policy_top="build_dam"),
        ])
        subproc = self._ok_subprocess()

        trend, _ = self._run(
            scripted, subproc, iterations=2, playbook_path=self.playbook_path)

        self.assertEqual(len(trend), 2)
        self.assertTrue(all(e["gap_flagged"] for e in trend))

        playbook = json.loads(self.playbook_path.read_text(encoding="utf-8"))
        gap_lessons = [
            l for l in playbook["lessons"] if l["trigger"] == "structural_gap:build_dam"
        ]
        self.assertEqual(len(gap_lessons), 1)
        self.assertEqual(gap_lessons[0]["evidence"]["runs"], 2)
        self.assertEqual(gap_lessons[0]["evidence"]["wins"], 0)
        # Without the structural_gap:* exemption, coach._confidence_from_evidence's
        # all-loss hard cap would clamp this at 0.2 FOREVER (a structural-gap
        # lesson is all-loss by construction - it never "wins" until the code is
        # fixed, so an ordinary lesson's noise-suppression cap would apply on
        # every single reconcile). The exemption instead runs the plain evidence-
        # weighted formula: 0.7*0.5 (this lesson's own prior confidence) +
        # 0.3*(0.5 - loss_rate(1.0)*0.15) = 0.455 at 2 recurrences (the run-count
        # bonus only starts at runs>=3) - comfortably above the old 0.2 ceiling,
        # and (see the len(gap_lessons)==1 assert above) not pruned either.
        self.assertAlmostEqual(gap_lessons[0]["confidence"], 0.455, places=3)
        self.assertGreater(gap_lessons[0]["confidence"], 0.2)


class PlateauStopTests(_GapLoopTestBase):
    def test_stops_when_all_recent_iterations_are_structural_gap(self):
        # 4 IDENTICAL structural-gap traces (same score every time) with
        # plateau_patience=2: iter0 sets best_score trivially (no prior best),
        # iter1/iter2 tie it (never improve) -> stall_streak reaches 2 right
        # after iter2, and both of the last 2 trend entries are structural_gap
        # -> stop. Only 3 of the 4 requested iterations should actually run.
        trace = _stall_trace("advance_time", policy_top="build_dam")
        scripted = self._meta_scripted([trace, trace, trace, trace])
        subproc = self._ok_subprocess()

        trend, build_spy = self._run(
            scripted, subproc, iterations=4, playbook_path=self.playbook_path,
            plateau_patience=2)

        self.assertEqual(len(trend), 3)
        self.assertEqual(len(scripted.calls), 3)  # only 3 of the 4 requested iterations ran
        self.assertEqual(len(scripted._traces), 1)  # ...proof: one canned trace never consumed
        self.assertTrue(all(e["stall_class"] == replay.STRUCTURAL_GAP for e in trend))
        build_spy.assert_not_called()
        subproc.assert_not_called()

    def test_plateau_does_not_stop_when_relabel_able_failures_recur(self):
        # Same shape plateau (score never improves), but every iteration is
        # POLICY_GAP (relabel-able) instead of structural_gap - more corrected
        # data still has a plausible payoff, so the loop must run to the
        # requested iteration count instead of stopping early.
        self._write_pristine()
        trace = _stall_trace("build_lumber_mill")
        scripted = self._meta_scripted([trace, trace, trace])
        subproc = self._ok_subprocess()

        trend, build_spy = self._run(
            scripted, subproc, iterations=3, playbook_path=self.playbook_path,
            plateau_patience=2)

        self.assertEqual(len(trend), 3)
        self.assertTrue(all(e["stall_class"] == replay.POLICY_GAP for e in trend))
        self.assertEqual(build_spy.call_count, 3)


if __name__ == "__main__":
    unittest.main()
