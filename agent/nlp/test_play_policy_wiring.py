"""Tests: Task 6a - wire the trained model + curriculum + replay into play_policy.run().

Covers, with a FakeBridge (no live game, no network):

1. THE NAMESPACE BRIDGE (the crux). `policy.rank(state)` returns game_schema ids
   ("build_lumberjack_flag", "build_lumber_mill", ...) but `planner.plan_report`'s
   bootstrap goals still use planner-only ids ("build_lumberjack", "build_water_pump",
   ...). Before this fix, `_execute_intent` keyed `report["goals"]` by id, so every
   bootstrap intent the model proposed found nothing and silently fell through to
   advance_time - the colony could never bootstrap. `_execute_intent` now resolves
   by SPEC (action_to_spec(model_id) -> spec -> the planner goal whose goal["spec"]
   matches), which bridges bootstrap ids (spec-mismatched) and economy ids
   (already spec-matched) uniformly.
2. Curriculum biasing (`curriculum.bias_ranking`) is actually invoked inside
   `run()`, between `policy.rank` and `_execute_intent`, and its output - not the
   raw ranked list - is what gets executed.
3. Replay recording (`replay.record_step`) fires once per cycle and lands a
   correctly-shaped row in agent/runs/<run_id>.jsonl.
4. The goal-reached stop condition (`curriculum.is_goal_reached`) ends the run
   before any ranking/execution work happens on that cycle.

Runnable BOTH ways:
    .venv/bin/python -m unittest agent.nlp.test_play_policy_wiring -v
    .venv/bin/python -m unittest discover -s agent
"""

from __future__ import annotations

import copy
import json
import unittest
import uuid
from pathlib import Path
from unittest import mock

from agent import curriculum, planner, play, replay
from agent.nlp import play_policy

_AGENT_DIR = Path(__file__).resolve().parent.parent
_FIXTURES = _AGENT_DIR / "fixtures"
_JOURNAL_DIR = _AGENT_DIR / "journal"
_RUNS_DIR = _AGENT_DIR / "runs"


def _load_fixture(name: str) -> dict:
    with (_FIXTURES / name).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _stable_state() -> dict:
    """A colony state that satisfies curriculum.is_goal_reached: pop>=30, water/food
    days both clear the forecast drought, nobody homeless. Deliberately minimal
    (no buildings/district_center) - the goal-reached check must fire BEFORE any
    map/planner-dependent work happens, so this is all run() should ever touch."""
    return {
        "population": {"total": 30, "homeless": 0, "free_beds": 3},
        "resources": [
            {"good": "Water", "stored": 1000, "days_remaining": 100.0},
            {"good": "Berries", "stored": 1000, "days_remaining": 100.0},
        ],
        "buildings": {"counts": {}, "under_construction": 0},
        "weather": {"next": {"duration_days": 3, "in_days": 10}},
        "time": {"cycle": 1, "day": 1, "daytime": "day", "hour": 8},
    }


class FakeBridge:
    """Stands in for play.Bridge. Canned ping/state/map/resources; records every
    .act(command, args) call so tests can assert on what was actually issued."""

    instances: list = []
    state_data = None
    map_data = None
    resources_data = None

    def __init__(self, base_url):
        self.base_url = base_url
        self.act_calls = []
        FakeBridge.instances.append(self)

    def ping(self):
        return 200, {"ok": True}

    def state(self):
        return 200, copy.deepcopy(FakeBridge.state_data)

    def map(self):
        return 200, copy.deepcopy(FakeBridge.map_data)

    def resources(self):
        return 200, copy.deepcopy(FakeBridge.resources_data)

    def act(self, command, args):
        self.act_calls.append((command, copy.deepcopy(args)))
        return 200, {"ok": True}


class _FixedPolicy:
    """Stands in for DecisionPolicy - .rank always returns the same canned list,
    regardless of state, so a test can control the RAW ranking precisely and prove
    curriculum.bias_ranking (not the raw order) drove what got executed."""

    def __init__(self, ranked):
        self._ranked = list(ranked)

    def rank(self, state):
        return list(self._ranked)


class _FixedPolicyLoader:
    """Stands in for agent.nlp.policy.DecisionPolicy - run() only ever calls .load()."""

    ranked = [("build_lumberjack_flag", 0.9), ("build_gatherer_flag", 0.1)]

    @classmethod
    def load(cls):
        return _FixedPolicy(cls.ranked)


class _AdvanceOnlyPolicyLoader:
    """A policy that only ever ranks advance_time - _execute_intent then has
    nothing else to try and always reports executed=False (see _execute_intent:
    the "advance_time" entry is remembered and returned only once the loop runs
    out of other ranked intents to attempt)."""

    ranked = [("advance_time", 0.5)]

    @classmethod
    def load(cls):
        return _FixedPolicy(cls.ranked)


class PlayPolicyWiringTests(unittest.TestCase):
    def setUp(self):
        FakeBridge.instances = []
        FakeBridge.state_data = None
        FakeBridge.map_data = None
        FakeBridge.resources_data = None
        self._run_ids = []

    def tearDown(self):
        for run_id in self._run_ids:
            journal_path = _JOURNAL_DIR / ("%s.jsonl" % run_id)
            run_path = _RUNS_DIR / ("%s.jsonl" % run_id)
            if journal_path.exists():
                journal_path.unlink()
            if run_path.exists():
                run_path.unlink()

    def _run_id(self, tag: str) -> str:
        run_id = "test_play_policy_wiring_%s_%s" % (tag, uuid.uuid4().hex[:8])
        self._run_ids.append(run_id)
        return run_id

    # -----------------------------------------------------------------------
    # 1. Namespace bridge - the crux fix, tested directly against _execute_intent.
    # -----------------------------------------------------------------------

    def test_execute_intent_resolves_bootstrap_model_id_by_spec(self):
        state = _load_fixture("state_fresh.json")
        map_data = _load_fixture("map_fresh.json")
        resources = _load_fixture("resources_fresh.json")
        report = planner.plan_report(state, map_data, resources=resources)

        # Prove the premise: the OLD id-keyed lookup would never have found this
        # goal - the model's id is not a planner goal id.
        goals_by_id = {g.get("id"): g for g in report.get("goals", []) if isinstance(g, dict)}
        self.assertNotIn("build_lumberjack_flag", goals_by_id)

        bridge = FakeBridge("http://test")
        ranked = [("build_lumberjack_flag", 0.9)]  # the model's game_schema namespace
        intent, conf, executed = play_policy._execute_intent(
            bridge, ranked, report, state, map_data, resources
        )

        self.assertTrue(executed)
        self.assertEqual(conf, 0.9)
        self.assertEqual(intent, "build_lumberjack")  # resolved to the PLANNER's bootstrap id

        place_calls = [args for cmd, args in bridge.act_calls if cmd == "place_building"]
        self.assertEqual(len(place_calls), 1)
        self.assertEqual(place_calls[0]["spec"], "LumberjackFlag")

    def test_execute_intent_resolves_economy_model_id_when_affordable(self):
        state = copy.deepcopy(_load_fixture("state_fresh.json"))
        for item in state["resources"]:
            if item.get("good") == "Log":
                item["stored"] = 200
                item["all_stock"] = 200
        map_data = _load_fixture("map_fresh.json")
        resources = _load_fixture("resources_fresh.json")
        report = planner.plan_report(state, map_data, resources=resources)

        bridge = FakeBridge("http://test")
        ranked = [("build_lumber_mill", 0.77)]  # already a game_schema id AND a planner id
        intent, conf, executed = play_policy._execute_intent(
            bridge, ranked, report, state, map_data, resources
        )

        self.assertTrue(executed)
        self.assertEqual(intent, "build_lumber_mill")
        place_calls = [args for cmd, args in bridge.act_calls if cmd == "place_building"]
        self.assertEqual(len(place_calls), 1)
        self.assertEqual(place_calls[0]["spec"], "LumberMill")

    def test_execute_intent_falls_through_when_expert_has_no_matching_goal(self):
        state = _load_fixture("state_fresh.json")
        map_data = _load_fixture("map_fresh.json")
        resources = _load_fixture("resources_fresh.json")
        report = planner.plan_report(state, map_data, resources=resources)

        # Guard the premise: nothing in a fresh colony's report proposes a Dam.
        proposed_specs = {g.get("spec") for g in report.get("goals", []) if isinstance(g, dict)}
        self.assertNotIn("Dam", proposed_specs)

        bridge = FakeBridge("http://test")
        ranked = [("build_dam", 0.95), ("build_lumberjack_flag", 0.4)]
        intent, conf, executed = play_policy._execute_intent(
            bridge, ranked, report, state, map_data, resources
        )

        # build_dam resolves to a real spec but has no live planner goal right now -
        # skipped gracefully, falls through to the next ranked intent.
        self.assertTrue(executed)
        self.assertEqual(intent, "build_lumberjack")
        self.assertEqual(conf, 0.4)

    # -----------------------------------------------------------------------
    # 2. Curriculum biasing is wired into run() between policy.rank and execution.
    # -----------------------------------------------------------------------

    def test_run_applies_curriculum_bias_ranking_before_executing(self):
        FakeBridge.state_data = _load_fixture("state_fresh.json")
        FakeBridge.map_data = _load_fixture("map_fresh.json")
        FakeBridge.resources_data = _load_fixture("resources_fresh.json")

        def reversing_bias(state, ranked):
            return list(reversed(ranked))

        run_id = self._run_id("bias")
        with mock.patch.object(play, "Bridge", FakeBridge), \
             mock.patch.object(play_policy, "DecisionPolicy", _FixedPolicyLoader), \
             mock.patch.object(curriculum, "bias_ranking", side_effect=reversing_bias) as bias_mock:
            play_policy.run({"BRIDGE_URL": "http://test"}, run_id, max_cycles=1)

        # bias_ranking was actually called, with the RAW policy.rank output.
        self.assertEqual(bias_mock.call_count, 1)
        raw_state, raw_ranked = bias_mock.call_args[0]
        self.assertEqual(raw_ranked, _FixedPolicyLoader.ranked)

        # The BIASED (reversed) order drove execution, not the raw top
        # ("build_lumberjack_flag"): GathererFlag was placed, and placed first.
        bridge = FakeBridge.instances[0]
        place_specs = [args["spec"] for cmd, args in bridge.act_calls if cmd == "place_building"]
        self.assertIn("GathererFlag", place_specs)
        self.assertEqual(place_specs[0], "GathererFlag")

    # -----------------------------------------------------------------------
    # 3. Replay recording fires once per cycle with the documented meta shape.
    # -----------------------------------------------------------------------

    def test_run_records_replay_step_each_cycle(self):
        FakeBridge.state_data = _load_fixture("state_fresh.json")
        FakeBridge.map_data = _load_fixture("map_fresh.json")
        FakeBridge.resources_data = _load_fixture("resources_fresh.json")

        run_id = self._run_id("record_step")
        with mock.patch.object(play, "Bridge", FakeBridge):
            play_policy.run({"BRIDGE_URL": "http://test"}, run_id, max_cycles=1)

        run_path = _RUNS_DIR / ("%s.jsonl" % run_id)
        self.assertTrue(run_path.exists())

        rows = replay.load_run(run_id)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["step"], 1)
        self.assertIsInstance(row.get("action"), str)
        self.assertTrue(row["action"])

        meta = row.get("meta") or {}
        for key in ("phase", "confidence", "expert_top", "policy_top", "executed"):
            self.assertIn(key, meta)
        self.assertEqual(meta["phase"], "SURVIVE_WATER")
        self.assertIsInstance(meta["executed"], bool)

    # -----------------------------------------------------------------------
    # 4. is_goal_reached stops the loop before any ranking/execution work.
    # -----------------------------------------------------------------------

    def test_run_stops_when_goal_reached(self):
        FakeBridge.state_data = _stable_state()
        FakeBridge.map_data = {}
        FakeBridge.resources_data = {"ok": True}

        run_id = self._run_id("goal_reached")
        with mock.patch.object(play, "Bridge", FakeBridge):
            summary = play_policy.run({"BRIDGE_URL": "http://test"}, run_id, max_cycles=5)

        # Broke out before the per-cycle ranking block ever incremented `total`.
        self.assertEqual(summary["cycles"], 0)

        # Only the run-prologue action (set_speed) fired; no ranking/placement calls.
        bridge = FakeBridge.instances[0]
        self.assertEqual([cmd for cmd, _args in bridge.act_calls], ["set_speed"])

        journal_events = _read_jsonl(_JOURNAL_DIR / ("%s.jsonl" % run_id))
        self.assertTrue(any(e.get("event") == "goal_reached" for e in journal_events))

        rows = replay.load_run(run_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "goal_reached")
        self.assertEqual(rows[0]["meta"]["phase"], curriculum.current_phase(_stable_state()))


class InRunStallDetectionTests(unittest.TestCase):
    """docs/kb/learning-loop-design.md SS5.1: run() must end itself within a few
    cycles of a stall/death streak starting, instead of grinding to max_cycles."""

    def setUp(self):
        FakeBridge.instances = []
        FakeBridge.state_data = None
        FakeBridge.map_data = None
        FakeBridge.resources_data = None
        self._run_ids = []

    def tearDown(self):
        for run_id in self._run_ids:
            journal_path = _JOURNAL_DIR / ("%s.jsonl" % run_id)
            run_path = _RUNS_DIR / ("%s.jsonl" % run_id)
            if journal_path.exists():
                journal_path.unlink()
            if run_path.exists():
                run_path.unlink()

    def _run_id(self, tag: str) -> str:
        run_id = "test_play_policy_wiring_%s_%s" % (tag, uuid.uuid4().hex[:8])
        self._run_ids.append(run_id)
        return run_id

    def _frozen_stall_state(self) -> dict:
        """state_fresh.json with comfortable, UNCHANGING water/food buffers (so
        only the stall path - not a thirst/hunger death - can trip) and a
        FakeBridge that returns this exact snapshot every cycle, so log_stored/
        building_counts genuinely never change: the "no progress" condition
        _scan/progress_signal look for."""
        state = copy.deepcopy(_load_fixture("state_fresh.json"))
        for item in state["resources"]:
            if item.get("good") == "Water":
                item["days_remaining"] = 10.0
                item["stored"] = 100
        state["resources"].append({
            "good": "Berries", "stored": 100, "days_remaining": 10.0,
            "all_stock": 100, "capacity": 100, "fill_rate": 0,
        })
        return state

    def test_run_breaks_early_on_in_run_stall_instead_of_grinding_to_max_cycles(self):
        FakeBridge.state_data = self._frozen_stall_state()
        FakeBridge.map_data = _load_fixture("map_fresh.json")
        FakeBridge.resources_data = _load_fixture("resources_fresh.json")

        run_id = self._run_id("stall_early_stop")
        with mock.patch.object(play, "Bridge", FakeBridge), \
             mock.patch.object(play_policy, "DecisionPolicy", _AdvanceOnlyPolicyLoader), \
             mock.patch.object(play_policy.controller, "bulk_advance_until_wake") as bulk_mock:
            summary = play_policy.run({"BRIDGE_URL": "http://test"}, run_id, max_cycles=15)

        # Every cycle ranks advance_time only, against a perfectly frozen state
        # (log_stored/building_counts never change) -> replay's stall streak
        # (_STALL_STREAK=8, one warmup row before the streak can start counting)
        # trips exactly at the 9th recorded row. The in-run check must end the
        # run THEN, not grind through the remaining 6 of the 15 requested cycles.
        rows = replay.load_run(run_id)
        self.assertEqual(len(rows), 9)
        self.assertEqual(summary["cycles"], 9)
        # bulk_advance_until_wake still ran for the 8 cycles before the stall was
        # detected (executed=False every time) but never for a 9th/10th/... -
        # proof the loop actually broke instead of merely coincidentally ending.
        self.assertEqual(bulk_mock.call_count, 8)

        journal_events = _read_jsonl(_JOURNAL_DIR / ("%s.jsonl" % run_id))
        stall_events = [e for e in journal_events if e.get("event") == "stall_detected"]
        self.assertEqual(len(stall_events), 1)
        self.assertEqual(stall_events[0]["cycle"], 9)
        self.assertEqual(stall_events[0]["ended"], "stalled")
        self.assertEqual(stall_events[0]["death_cause"], "stall")


if __name__ == "__main__":
    unittest.main()
