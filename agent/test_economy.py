"""Tests for the 3a production-chain economy helper + its planner wiring.

Runnable BOTH ways:
    python3 agent/test_economy.py
    python3 -m unittest agent.test_economy
"""

import unittest

# Import-fallback so the module resolves whether the test is run as a loose
# script (sys.path has agent/) or as the package module agent.test_economy
# (sys.path has the repo root). Mirrors the pattern in replay.py / placement.py.
try:  # pragma: no cover - import path depends on invocation
    import economy
    import game_schema
    import planner
except ImportError:  # pragma: no cover
    from agent import economy, game_schema, planner


def _counts_state(counts, logs=0):
    """Minimal game-state dict with the given faction-suffixed building counts."""
    return {
        "buildings": {"counts": dict(counts)},
        "resources": [{"good": "Log", "stored": logs, "all_stock": logs}],
        "population": {"total": 10, "homeless": 0},
    }


class NeededProducersTests(unittest.TestCase):
    def test_plank_consumer_without_lumbermill_needs_lumbermill(self):
        # A built Gear Workshop consumes Plank; construction also consumes it.
        # Log is produced (Lumberjack Flag built) but Plank has no producer.
        state = _counts_state(
            {"LumberjackFlag.Folktails": 1, "GearWorkshop.Folktails": 1}
        )
        needed = economy.needed_producers(state)
        self.assertIn("LumberMill", needed)
        # Already-built producers are never re-suggested.
        self.assertNotIn("GearWorkshop", needed)
        self.assertNotIn("LumberjackFlag", needed)

    def test_lumbermill_present_is_not_needed(self):
        state = _counts_state(
            {
                "LumberjackFlag.Folktails": 1,
                "GearWorkshop.Folktails": 1,
                "LumberMill.Folktails": 1,
            }
        )
        self.assertNotIn("LumberMill", economy.needed_producers(state))

    def test_specs_are_ordered_raw_before_refined(self):
        # Construction demands both Plank (depth 1) and Gear (depth 2). With only
        # a Lumberjack Flag built, both LumberMill and GearWorkshop are missing,
        # and the Plank producer must be listed before the Gear producer.
        state = _counts_state({"LumberjackFlag.Folktails": 1})
        needed = economy.needed_producers(state)
        self.assertIn("LumberMill", needed)
        self.assertIn("GearWorkshop", needed)
        self.assertLess(needed.index("LumberMill"), needed.index("GearWorkshop"))

    def test_result_is_deduped(self):
        state = _counts_state({"LumberjackFlag.Folktails": 1})
        needed = economy.needed_producers(state)
        self.assertEqual(len(needed), len(set(needed)))

    def test_empty_state_is_safe(self):
        needed = economy.needed_producers({})
        self.assertIsInstance(needed, list)
        # Construction demand still applies even with nothing built.
        self.assertIn("LumberMill", needed)

    def test_every_suggested_spec_maps_to_a_gameplay_action(self):
        state = _counts_state({"LumberjackFlag.Folktails": 1})
        actions = set(game_schema.actions())
        for spec in economy.needed_producers(state):
            self.assertIsNotNone(
                game_schema.spec_to_action(spec),
                "producer spec %r has no gameplay action" % spec,
            )
            self.assertIn(game_schema.spec_to_action(spec), actions)


class LogCostTests(unittest.TestCase):
    def test_log_cost_reads_buildings_json(self):
        self.assertEqual(economy.log_cost("LumberMill"), 15)
        self.assertEqual(economy.log_cost("GearWorkshop"), 15)

    def test_log_cost_tolerates_faction_suffix(self):
        self.assertEqual(economy.log_cost("LumberMill.Folktails"), 15)

    def test_log_cost_zero_when_no_log_in_cost(self):
        # A Smelter costs planks/gear/scrap but no logs.
        self.assertEqual(economy.log_cost("Smelter"), 0)

    def test_log_cost_zero_for_unknown_spec(self):
        self.assertEqual(economy.log_cost("NotARealBuilding"), 0)


class PlannerWiringTests(unittest.TestCase):
    def _state(self):
        # Lumberjack Flag built (Log produced); plenty of logs so the economy
        # goals read as affordable.
        return {
            "buildings": {"counts": {"LumberjackFlag.Folktails": 1}},
            "resources": [{"good": "Log", "stored": 100, "all_stock": 100}],
            "population": {"total": 10, "homeless": 0},
        }

    def test_analyze_emits_lumber_mill_goal(self):
        goals = planner.analyze(self._state(), None)
        ids = [g["id"] for g in goals]
        self.assertIn("build_lumber_mill", ids)

        goal = next(g for g in goals if g["id"] == "build_lumber_mill")
        self.assertEqual(goal["spec"], "LumberMill")
        self.assertEqual(goal["cost_logs"], 15)          # sourced from buildings.json
        self.assertFalse(goal["free"])                    # 15 logs is not free
        self.assertTrue(goal["affordable"])               # have 100 logs

    def test_analyze_emits_producers_raw_before_refined(self):
        goals = planner.analyze(self._state(), None)
        ids = [g["id"] for g in goals]
        self.assertIn("build_gear_workshop", ids)
        self.assertLess(
            ids.index("build_lumber_mill"), ids.index("build_gear_workshop")
        )

    def test_every_emitted_producer_goal_id_is_a_valid_action(self):
        # Regression guard (cf. curriculum/replay): a producer goal id is one
        # whose spec round-trips to itself via game_schema; all such ids must be
        # real actions or the decision model can never emit them.
        goals = planner.analyze(self._state(), None)
        actions = set(game_schema.actions())
        producer_ids = [
            g["id"]
            for g in goals
            if g.get("spec") and game_schema.spec_to_action(g["spec"]) == g["id"]
        ]
        self.assertTrue(producer_ids, "expected at least one production-chain goal")
        for goal_id in producer_ids:
            self.assertIn(goal_id, actions)

    def test_bootstrap_goal_order_is_preserved(self):
        # The economy goals must be APPENDED after bootstrap goals, never spliced
        # ahead of them.
        state = {
            "buildings": {"counts": {"DistrictCenter.Folktails": 1}},
            "resources": [
                {"good": "Log", "stored": 0, "all_stock": 0},
                {"good": "Water", "stored": 0, "days_remaining": 0},
                {"good": "Food", "stored": 0, "days_remaining": 0},
            ],
            "population": {"total": 13, "homeless": 13},
        }
        goals = planner.analyze(state, None)
        ids = [g["id"] for g in goals]
        self.assertEqual(ids[0], "build_lumberjack")
        # Any production-chain goal appears strictly after the free lumberjack.
        self.assertIn("build_lumber_mill", ids)
        self.assertGreater(ids.index("build_lumber_mill"), ids.index("build_lumberjack"))


if __name__ == "__main__":
    unittest.main()
