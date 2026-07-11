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
        # goals read as affordable. SciencePoints stocked to 150 so the 3b tech
        # gate treats the science-locked GearWorkshop (100 SP) as unlockable and
        # the raw->refined ordering assertions below still exercise it. (A fresh
        # 0-science colony now SUPPRESSES GearWorkshop; that is covered by the
        # dedicated TechGatingTests.)
        return {
            "buildings": {"counts": {"LumberjackFlag.Folktails": 1}},
            "resources": [
                {"good": "Log", "stored": 100, "all_stock": 100},
                {"good": "SciencePoints", "stored": 150, "all_stock": 150},
            ],
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


class UnlockableNowTests(unittest.TestCase):
    """3b helper: which specs the colony can build NOW given tech."""

    def _sci_state(self, science=0, counts=None):
        return {
            "buildings": {"counts": dict(counts or {})},
            "resources": [
                {"good": "SciencePoints", "stored": science, "all_stock": science}
            ],
            "population": {"total": 10, "homeless": 0},
        }

    def test_zero_science_only_free_specs(self):
        u = economy.unlockable_now(self._sci_state(0))
        self.assertIn("LumberMill", u)         # science_cost 0 -> always available
        self.assertIn("Inventor", u)           # science_cost 0
        self.assertNotIn("GearWorkshop", u)    # needs 100 SP
        self.assertNotIn("Smelter", u)         # needs 300 SP
        self.assertNotIn("Observatory", u)     # needs 1000 SP
        # With nothing science-locked built, EVERY unlockable spec is free.
        self.assertTrue(u)
        self.assertTrue(all(economy.science_cost(s) == 0 for s in u))

    def test_enough_science_includes_gated_spec(self):
        u = economy.unlockable_now(self._sci_state(150))
        self.assertIn("GearWorkshop", u)       # 100 <= 150, no prereq
        self.assertIn("LumberMill", u)         # free
        self.assertNotIn("Smelter", u)         # 300 > 150

    def test_recursive_building_prereq_resolves(self):
        # GravityBattery (400 SP) requires Lumber Mill (start) + Gear Workshop
        # (100). At 400 SP both prereqs are recursively satisfiable, so it unlocks.
        u = economy.unlockable_now(self._sci_state(400))
        self.assertIn("GearWorkshop", u)
        self.assertIn("GravityBattery", u)

    def test_prereq_unmet_excludes_even_with_enough_science(self):
        # GeothermalEngine costs only 160 SP but requires a 'Geothermal Field
        # tile' -- a terrain prerequisite that never resolves to a building. Even
        # with 500 SP (>> 160) it must NOT be unlockable, while a same-tier purely
        # science-gated building (PrintingPress, 400) IS. This isolates the
        # prerequisite gate from the science gate.
        u = economy.unlockable_now(self._sci_state(500))
        self.assertNotIn("GeothermalEngine", u)
        self.assertIn("PrintingPress", u)

    def test_built_spec_is_available_even_without_science(self):
        # A built instance implies the spec is unlocked (behavioral-cloning rule).
        u = economy.unlockable_now(
            self._sci_state(0, {"GearWorkshop.Folktails": 1})
        )
        self.assertIn("GearWorkshop", u)

    def test_empty_state_is_safe(self):
        u = economy.unlockable_now({})
        self.assertIsInstance(u, list)
        self.assertIn("LumberMill", u)
        self.assertNotIn("GearWorkshop", u)

    def test_result_is_deduped_and_sorted(self):
        u = economy.unlockable_now(self._sci_state(400))
        self.assertEqual(len(u), len(set(u)))
        self.assertEqual(u, sorted(u))


class TechGatingTests(unittest.TestCase):
    """3b wiring: analyze() gates 3a producers on tech + drives/scales science."""

    def _state(self, science=0, counts=None, logs=100):
        c = {"LumberjackFlag.Folktails": 1}
        c.update(counts or {})
        return {
            "buildings": {"counts": c},
            "resources": [
                {"good": "Log", "stored": logs, "all_stock": logs},
                {"good": "SciencePoints", "stored": science, "all_stock": science},
            ],
            "population": {"total": 10, "homeless": 0},
        }

    def test_fresh_colony_suppresses_science_locked_producer(self):
        goals = planner.analyze(self._state(science=0), None)
        ids = [g["id"] for g in goals]
        self.assertIn("build_lumber_mill", ids)       # free producer still emitted
        self.assertNotIn("build_gear_workshop", ids)  # 100 SP -> suppressed
        self.assertNotIn("build_smelter", ids)        # 300 SP -> suppressed
        # science IS driven (Inventor==0 -> bootstrap already emits build_inventor)
        self.assertIn("build_inventor", ids)

    def test_no_duplicate_inventor_when_bootstrap_emits_it(self):
        # Inventor==0: bootstrap emits build_inventor exactly once; science scaling
        # must NOT append a second build_inventor goal.
        goals = planner.analyze(self._state(science=0), None)
        ids = [g["id"] for g in goals]
        self.assertEqual(ids.count("build_inventor"), 1)

    def test_science_scaling_adds_second_inventor_when_inventor_exists(self):
        # Inventor already built => bootstrap does NOT emit build_inventor, yet
        # science still gates wanted producers, so scaling adds a 2nd Inventor.
        goals = planner.analyze(
            self._state(science=0, counts={"Inventor.Folktails": 1}), None
        )
        ids = [g["id"] for g in goals]
        self.assertNotIn("build_gear_workshop", ids)  # still science-locked
        self.assertIn("build_inventor", ids)          # scaling goal (2nd Inventor)
        self.assertNotIn("build_observatory", ids)    # not unlockable at 0 SP

    def test_no_science_scaling_when_no_locked_producers(self):
        # With ample science every wanted producer is unlockable, so there is no
        # science bottleneck and scaling must not fire (no 2nd Inventor).
        goals = planner.analyze(
            self._state(science=2000, counts={"Inventor.Folktails": 1}), None
        )
        ids = [g["id"] for g in goals]
        self.assertNotIn("build_inventor", ids)       # no scaling: nothing locked

    def test_gated_producer_appears_with_enough_science(self):
        goals = planner.analyze(self._state(science=150), None)
        ids = [g["id"] for g in goals]
        self.assertIn("build_gear_workshop", ids)     # 100 <= 150 -> unlockable
        self.assertIn("build_lumber_mill", ids)
        self.assertLess(
            ids.index("build_lumber_mill"), ids.index("build_gear_workshop")
        )

    def test_every_emitted_goal_id_is_a_valid_action(self):
        # Regression guard across several science levels / inventor states: every
        # spec-bearing goal whose spec round-trips to its own id must be a real
        # action, or the decision model can never emit it.
        actions = set(game_schema.actions())
        for sci in (0, 150, 400, 2000):
            for counts in (None, {"Inventor.Folktails": 1}):
                goals = planner.analyze(
                    self._state(science=sci, counts=counts), None
                )
                for g in goals:
                    spec = g.get("spec")
                    if spec and game_schema.spec_to_action(spec) == g["id"]:
                        self.assertIn(g["id"], actions)


class WellbeingActionSpaceTests(unittest.TestCase):
    """3c Step 1: the curated well-being amenities are real gameplay actions."""

    CURATED = [
        "Campfire", "Shrub", "Shower", "ContemplationSpot",
        "Lantern", "Lido", "Agora", "MudPit",
    ]

    def test_curated_amenities_are_actions(self):
        actions = set(game_schema.actions())
        for spec in self.CURATED:
            goal_id = game_schema.spec_to_action(spec)
            self.assertIsNotNone(goal_id, "amenity %r has no gameplay action" % spec)
            self.assertIn(goal_id, actions)

    def test_campfire_resolves_to_expected_id(self):
        self.assertEqual(game_schema.spec_to_action("Campfire"), "build_campfire")
        self.assertEqual(game_schema.spec_to_action("MudPit"), "build_mud_pit")

    def test_action_space_grew_past_bootstrap_83(self):
        # The 83-action space excluded the whole 'decoration' category; the curated
        # amenities must have been added on top (not all 29 decorations).
        self.assertGreater(len(game_schema.actions()), 83)
        self.assertLess(len(game_schema.actions()), 83 + 29)


class UncoveredWellbeingNeedsTests(unittest.TestCase):
    """3c Step 2: cheapest curated source per uncovered decoration-only need."""

    def _state(self, counts=None, science=0):
        return {
            "buildings": {"counts": dict(counts or {})},
            "resources": [
                {"good": "SciencePoints", "stored": science, "all_stock": science}
            ],
            "population": {"total": 10, "homeless": 0},
        }

    def test_no_amenity_built_returns_cheapest_per_need(self):
        by = {r["need"]: r["spec"] for r in economy.uncovered_wellbeing_needs(self._state())}
        self.assertEqual(by.get("Social Life"), "Campfire")   # sci 0
        self.assertEqual(by.get("Aesthetics"), "Shrub")       # sci 0
        self.assertEqual(by.get("Wet Fur"), "Shower")         # sci 50 (cheapest source)
        self.assertEqual(by.get("Fun"), "Lido")               # sci 250 (cheapest source)

    def test_campfire_built_drops_social_life(self):
        by = {
            r["need"]: r["spec"]
            for r in economy.uncovered_wellbeing_needs(self._state({"Campfire.Folktails": 1}))
        }
        self.assertNotIn("Social Life", by)   # a curated source is built -> covered
        self.assertIn("Aesthetics", by)       # the others are still uncovered

    def test_every_source_spec_maps_to_a_valid_action(self):
        actions = set(game_schema.actions())
        for row in economy.uncovered_wellbeing_needs(self._state()):
            goal_id = game_schema.spec_to_action(row["spec"])
            self.assertIsNotNone(goal_id, "source %r has no action" % row["spec"])
            self.assertIn(goal_id, actions)

    def test_empty_state_is_safe(self):
        self.assertIsInstance(economy.uncovered_wellbeing_needs({}), list)


class WellbeingEmissionTests(unittest.TestCase):
    """3c Step 3: analyze() emits housing headroom + gated amenities in GROW phase."""

    def _growth_state(self, science=0, free_beds=0, counts=None):
        c = {
            "LumberjackFlag.Folktails": 1,
            "WaterPump.Folktails": 1,
            "GathererFlag.Folktails": 1,
        }
        c.update(counts or {})
        return {
            "buildings": {"counts": c},
            "resources": [
                {"good": "Log", "stored": 300, "all_stock": 300},
                {"good": "Water", "stored": 100, "days_remaining": 99},
                {"good": "Food", "stored": 100, "days_remaining": 99},
                {"good": "SciencePoints", "stored": science, "all_stock": science},
            ],
            "population": {"total": 10, "homeless": 0, "free_beds": free_beds},
        }

    def test_growth_state_emits_housing_and_free_amenities(self):
        goals = planner.analyze(self._growth_state(science=0, free_beds=0), None)
        ids = [g["id"] for g in goals]
        # Free-bed breeding headroom: a lodge tier so a kit has room to be born.
        self.assertIn("build_lodge", ids)
        # 0-science amenities emitted (Campfire=Social Life, Shrub=Aesthetics).
        self.assertIn("build_campfire", ids)
        self.assertIn("build_shrub", ids)
        # Science-locked amenities are NOT emitted at 0 science (gated by 3b).
        self.assertNotIn("build_mud_pit", ids)   # 1800 SP
        self.assertNotIn("build_shower", ids)    # 50 SP

    def test_free_beds_available_suppresses_housing_goal(self):
        ids = [g["id"] for g in planner.analyze(self._growth_state(free_beds=3), None)]
        self.assertNotIn("build_lodge", ids)     # room already exists; no homeless

    def test_crisis_state_emits_no_amenities(self):
        state = {
            "buildings": {"counts": {"LumberjackFlag.Folktails": 1}},
            "resources": [
                {"good": "Log", "stored": 300, "all_stock": 300},
                {"good": "Water", "stored": 0, "days_remaining": 0},
                {"good": "Food", "stored": 0, "days_remaining": 0},
            ],
            "population": {"total": 10, "homeless": 0, "free_beds": 0},
        }
        ids = [g["id"] for g in planner.analyze(state, None)]
        self.assertNotIn("build_campfire", ids)
        self.assertNotIn("build_shrub", ids)
        self.assertNotIn("build_lodge", ids)     # survival not secure -> no growth goals

    def test_enough_science_unlocks_next_amenity(self):
        ids = [g["id"] for g in planner.analyze(self._growth_state(science=60), None)]
        self.assertIn("build_shower", ids)       # 50 <= 60 -> now unlockable
        self.assertIn("build_campfire", ids)     # still emitted

    def test_every_emitted_goal_id_is_a_valid_action(self):
        actions = set(game_schema.actions())
        for sci in (0, 60, 300, 2000):
            for fb in (0, 5):
                goals = planner.analyze(self._growth_state(science=sci, free_beds=fb), None)
                for g in goals:
                    spec = g.get("spec")
                    if spec and game_schema.spec_to_action(spec) == g["id"]:
                        self.assertIn(g["id"], actions)


if __name__ == "__main__":
    unittest.main()
