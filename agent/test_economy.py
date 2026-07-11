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
    import resource_manager
except ImportError:  # pragma: no cover
    from agent import economy, game_schema, planner, resource_manager


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


class PowerDeficitTests(unittest.TestCase):
    """3d helper: net power demand of the BUILT colony (consumed - produced)."""

    def _state(self, counts):
        return {
            "buildings": {"counts": dict(counts)},
            "resources": [{"good": "SciencePoints", "stored": 0}],
            "population": {"total": 10, "homeless": 0},
        }

    def test_no_buildings_zero_deficit(self):
        self.assertEqual(economy.power_deficit(self._state({})), 0)

    def test_empty_state_is_safe(self):
        self.assertEqual(economy.power_deficit({}), 0)

    def test_built_consumer_without_producer_is_positive(self):
        # LumberMill draws 50hp; nothing produces power -> deficit 50 (under-powered).
        self.assertEqual(economy.power_deficit(self._state({"LumberMill.Folktails": 1})), 50)

    def test_producer_covers_consumer_is_negative_surplus(self):
        # LumberMill(-50) + WaterWheel(+270) -> 50 - 270 = -220 (surplus, signed).
        d = economy.power_deficit(
            self._state({"LumberMill.Folktails": 1, "WaterWheel.Folktails": 1})
        )
        self.assertEqual(d, -220)
        self.assertLess(d, 0)

    def test_deficit_scales_with_counts(self):
        self.assertEqual(economy.power_deficit(self._state({"LumberMill.Folktails": 3})), 150)

    def test_pure_producer_is_negative(self):
        # A lone PowerWheel (+50) with no consumer is pure surplus.
        self.assertEqual(economy.power_deficit(self._state({"PowerWheel.Folktails": 1})), -50)


class PowerSuggestionTests(unittest.TestCase):
    """3d helper: the map-blind "power building to add by availability"."""

    def _state(self, science=0, counts=None):
        return {
            "buildings": {"counts": dict(counts or {})},
            "resources": [{"good": "SciencePoints", "stored": science}],
            "population": {"total": 10, "homeless": 0},
        }

    def test_fresh_land_only_colony_suggests_power_wheel(self):
        # sci 0, no map: PowerWheel is the only guaranteed land-placeable producer.
        self.assertEqual(economy.power_building_suggestion(self._state(science=0)), "PowerWheel")

    def test_empty_state_is_safe(self):
        self.assertEqual(economy.power_building_suggestion({}), "PowerWheel")

    def test_suggestion_maps_to_a_valid_action(self):
        actions = set(game_schema.actions())
        for sci in (0, 120, 400, 1400, 3000):
            spec = economy.power_building_suggestion(self._state(science=sci))
            goal_id = game_schema.spec_to_action(spec)
            self.assertIsNotNone(goal_id, "suggestion %r has no action" % spec)
            self.assertIn(goal_id, actions)

    def test_suggestion_is_always_unlockable_now(self):
        for sci in (0, 120, 400, 1400, 3000):
            state = self._state(science=sci)
            self.assertIn(
                economy.power_building_suggestion(state),
                set(economy.unlockable_now(state)),
            )

    def test_never_suggests_a_map_gated_producer(self):
        # WaterWheel (flowing water) + GeothermalEngine (terrain) are placement facts
        # economy.py can't see, so it never defaults to them.
        for sci in (0, 160, 1400, 3000):
            spec = economy.power_building_suggestion(self._state(science=sci))
            self.assertNotIn(spec, ("WaterWheel", "GeothermalEngine"))

    def test_higher_science_prefers_higher_output_land_producer(self):
        # "Highest useful output among AVAILABLE options": once unlockable, a wind
        # producer with more hp than PowerWheel(50) wins.
        self.assertEqual(
            economy.power_building_suggestion(self._state(science=120)), "WindTurbine"
        )  # +68 > +50
        self.assertEqual(
            economy.power_building_suggestion(self._state(science=1400)), "LargeWindTurbine"
        )  # +144 > +68


class PowerEmissionTests(unittest.TestCase):
    """3d wiring: analyze() emits a power goal when built/planned load needs it."""

    POWER_BUILD_IDS = {
        "build_power_wheel", "build_water_wheel", "build_wind_turbine",
        "build_large_wind_turbine", "build_geothermal_engine",
    }

    def _state(self, counts, science=0, logs=100):
        return {
            "buildings": {"counts": dict(counts)},
            "resources": [
                {"good": "Log", "stored": logs, "all_stock": logs},
                {"good": "SciencePoints", "stored": science},
            ],
            "population": {"total": 10, "homeless": 0},
        }

    def test_built_underpowered_consumer_emits_power_goal(self):
        goals = planner.analyze(
            self._state({"LumberjackFlag.Folktails": 1, "LumberMill.Folktails": 1}), None
        )
        ids = [g["id"] for g in goals]
        self.assertIn("build_power_wheel", ids)
        goal = next(g for g in goals if g["id"] == "build_power_wheel")
        self.assertEqual(goal["spec"], "PowerWheel")
        self.assertEqual(goal["cost_logs"], 20)   # sourced from buildings.json
        self.assertTrue(goal["affordable"])        # have 100 logs

    def test_planned_powered_producer_pulls_power_with_it(self):
        # Fresh-ish colony: 3a plans build_lumber_mill (a -50hp consumer). No power
        # exists, so power is emitted WITH it (anticipatory), not a cycle late.
        goals = planner.analyze(self._state({"LumberjackFlag.Folktails": 1}), None)
        ids = [g["id"] for g in goals]
        self.assertIn("build_lumber_mill", ids)    # the powered consumer being planned
        self.assertIn("build_power_wheel", ids)    # power lands alongside it

    def test_well_powered_state_emits_no_power_goal(self):
        # LumberMill(-50) already covered by WaterWheel(+270); no new consumer planned
        # (LumberMill built, sci 0) -> surplus, so no power goal at all.
        goals = planner.analyze(
            self._state({
                "LumberjackFlag.Folktails": 1,
                "LumberMill.Folktails": 1,
                "WaterWheel.Folktails": 1,
            }),
            None,
        )
        ids = [g["id"] for g in goals]
        for pid in self.POWER_BUILD_IDS:
            self.assertNotIn(pid, ids)

    def test_science_locked_power_option_not_emitted_before_unlockable(self):
        # At 0 science the emitted power goal is the sci-0 PowerWheel, never a
        # science-locked producer (WindTurbine 120 / LargeWindTurbine 1400).
        goals = planner.analyze(
            self._state({"LumberjackFlag.Folktails": 1, "LumberMill.Folktails": 1}, science=0),
            None,
        )
        ids = [g["id"] for g in goals]
        self.assertIn("build_power_wheel", ids)
        self.assertNotIn("build_wind_turbine", ids)
        self.assertNotIn("build_large_wind_turbine", ids)

    def test_every_emitted_power_goal_is_valid_and_unlockable(self):
        # Regression guard: any emitted power goal id is a real action AND its spec
        # is unlockable now (never emitted ahead of its tech gate).
        actions = set(game_schema.actions())
        counts_variants = (
            {"LumberjackFlag.Folktails": 1},
            {"LumberjackFlag.Folktails": 1, "LumberMill.Folktails": 1},
            {"LumberMill.Folktails": 1, "WaterWheel.Folktails": 1},
        )
        for sci in (0, 120, 400, 1400, 3000):
            for counts in counts_variants:
                state = self._state(counts, science=sci)
                goals = planner.analyze(state, None)
                unlockable = set(economy.unlockable_now(state))
                for g in goals:
                    if g["id"] in self.POWER_BUILD_IDS:
                        self.assertIn(g["id"], actions)
                        self.assertIn(g["spec"], unlockable)


class StoragePressureTests(unittest.TestCase):
    """3e helper: goods near their storage capacity (stored/capacity >= 0.85)."""

    def test_good_near_capacity_is_flagged(self):
        state = {"resources": [{"good": "Log", "stored": 90, "capacity": 100}]}
        self.assertIn("Log", economy.storage_pressure(state))

    def test_good_at_threshold_is_flagged(self):
        # Exactly 0.85 counts as pressure (>= boundary).
        state = {"resources": [{"good": "Log", "stored": 85, "capacity": 100}]}
        self.assertIn("Log", economy.storage_pressure(state))

    def test_good_below_threshold_not_flagged(self):
        state = {"resources": [{"good": "Log", "stored": 30, "capacity": 100}]}
        self.assertNotIn("Log", economy.storage_pressure(state))

    def test_good_without_capacity_not_flagged(self):
        # No capacity key -> excluded (no signal to reason about).
        self.assertEqual(economy.storage_pressure({"resources": [{"good": "Log", "stored": 90}]}), [])
        # Zero/None capacity -> excluded (avoid div-by-zero + meaningless ratio).
        self.assertEqual(economy.storage_pressure({"resources": [{"good": "Log", "stored": 90, "capacity": 0}]}), [])
        self.assertEqual(economy.storage_pressure({"resources": [{"good": "Log", "stored": 90, "capacity": None}]}), [])

    def test_ordered_most_pressured_first(self):
        state = {
            "resources": [
                {"good": "Log", "stored": 86, "capacity": 100},
                {"good": "Plank", "stored": 99, "capacity": 100},
            ]
        }
        self.assertEqual(economy.storage_pressure(state)[0], "Plank")

    def test_empty_state_is_safe(self):
        self.assertEqual(economy.storage_pressure({}), [])


class StorageSpecsForTests(unittest.TestCase):
    """3e helper: the real storage building specs for a good, largest-first."""

    def test_raw_bulk_good_maps_to_pile_family(self):
        specs = economy.storage_specs_for("Log")
        self.assertEqual(specs[0], "UndergroundPile")   # largest-first
        self.assertIn("LargePile", specs)
        self.assertIn("SmallPile", specs)
        self.assertNotIn("SmallWarehouse", specs)       # a pile good, not a warehouse good

    def test_manufactured_good_maps_to_warehouse_family(self):
        specs = economy.storage_specs_for("Gear")
        self.assertEqual(specs[0], "LargeWarehouse")    # largest-first
        self.assertIn("SmallWarehouse", specs)
        self.assertNotIn("SmallPile", specs)

    def test_good_with_no_storage_building_returns_empty(self):
        # SciencePoints has no storage_building in goods.json.
        self.assertEqual(economy.storage_specs_for("SciencePoints"), [])

    def test_unknown_good_returns_empty(self):
        self.assertEqual(economy.storage_specs_for("NotAGood"), [])

    def test_every_storage_spec_maps_to_a_valid_action(self):
        actions = set(game_schema.actions())
        for good in ("Log", "Plank", "Gear", "Bread", "MetalBlock"):
            for spec in economy.storage_specs_for(good):
                self.assertIsNotNone(game_schema.spec_to_action(spec), "%r has no action" % spec)
                self.assertIn(game_schema.spec_to_action(spec), actions)


class StorageEmissionTests(unittest.TestCase):
    """3e wiring: analyze() emits a pile/warehouse goal under storage pressure.

    Storage-pressure is a GROWTH/economy concern, gated behind _survival_secure
    (like the 3c amenities) so no logs are spent on warehouses during a
    thirst/hunger/homeless crisis. The base state is deliberately survival-secure
    and water-buffered (SmallTank x5 zeroes the drought deficit) so these tests
    isolate the storage path from the drought path.
    """

    def _secure_state(self, extra_resources, counts=None, science=0):
        c = {
            "LumberjackFlag.Folktails": 1,
            "WaterPump.Folktails": 1,
            "GathererFlag.Folktails": 1,
            "SmallTank.Folktails": 5,   # 150 water capacity -> drought deficit 0
        }
        c.update(counts or {})
        resources = [
            {"good": "Water", "stored": 100, "days_remaining": 99},
            {"good": "Food", "stored": 100, "days_remaining": 99},
            {"good": "SciencePoints", "stored": science, "all_stock": science},
        ]
        resources.extend(extra_resources)
        return {
            "buildings": {"counts": c},
            "resources": resources,
            "population": {"total": 10, "homeless": 0, "free_beds": 5},
            "weather": {"next": {"duration_days": 3}},
        }

    def test_log_pressure_emits_pile_goal(self):
        state = self._secure_state([{"good": "Log", "stored": 95, "all_stock": 95, "capacity": 100}])
        ids = [g["id"] for g in planner.analyze(state, None)]
        self.assertIn("build_large_pile", ids)          # largest sci-0 pile
        self.assertNotIn("build_small_warehouse", ids)  # a pile good never routes to a warehouse

    def test_manufactured_pressure_emits_warehouse_goal(self):
        # Pre-build a SmallWarehouse so the bootstrap storage goal is satisfied and the
        # only warehouse goal comes from storage-pressure. Gear near cap -> warehouse.
        state = self._secure_state(
            [
                {"good": "Log", "stored": 300, "all_stock": 300},
                {"good": "Gear", "stored": 95, "capacity": 100},
            ],
            counts={"SmallWarehouse.Folktails": 1},
        )
        ids = [g["id"] for g in planner.analyze(state, None)]
        self.assertIn("build_medium_warehouse", ids)    # largest sci-0 warehouse
        self.assertNotIn("build_large_pile", ids)       # a warehouse good never routes to a pile

    def test_no_pressure_emits_no_storage_goal(self):
        state = self._secure_state([{"good": "Log", "stored": 30, "all_stock": 30, "capacity": 100}])
        ids = [g["id"] for g in planner.analyze(state, None)]
        for pid in ("build_small_pile", "build_large_pile", "build_underground_pile"):
            self.assertNotIn(pid, ids)

    def test_storage_suppressed_during_survival_crisis(self):
        # Not survival-secure (no pump, water/food at 0) -> storage goals suppressed
        # even though Log is at capacity.
        state = {
            "buildings": {"counts": {"LumberjackFlag.Folktails": 1}},
            "resources": [
                {"good": "Log", "stored": 95, "all_stock": 95, "capacity": 100},
                {"good": "Water", "stored": 0, "days_remaining": 0},
                {"good": "Food", "stored": 0, "days_remaining": 0},
            ],
            "population": {"total": 10, "homeless": 0},
        }
        ids = [g["id"] for g in planner.analyze(state, None)]
        for pid in ("build_small_pile", "build_large_pile", "build_underground_pile"):
            self.assertNotIn(pid, ids)

    def test_science_gated_tier_appears_when_unlockable(self):
        # 1000 science -> Underground Pile (sci 1000) unlockable -> it becomes the
        # largest-first pile pick, exercising the unlockable_now gate.
        state = self._secure_state(
            [{"good": "Log", "stored": 300, "all_stock": 300, "capacity": 320}], science=1000
        )
        ids = [g["id"] for g in planner.analyze(state, None)]
        self.assertIn("build_underground_pile", ids)

    def test_emitted_storage_goal_is_well_formed(self):
        state = self._secure_state([{"good": "Log", "stored": 95, "all_stock": 95, "capacity": 100}])
        goal = next(g for g in planner.analyze(state, None) if g["id"] == "build_large_pile")
        self.assertEqual(goal["spec"], "LargePile")
        self.assertEqual(goal["cost_logs"], economy.log_cost("LargePile"))   # from buildings.json
        self.assertTrue(goal["affordable"])                                  # 95 logs >> 6


class DroughtTankEmissionTests(unittest.TestCase):
    """3e wiring: analyze() emits tank goals per resource_manager.drought_prep."""

    def _drought_state(self, pop=10, drought=8, water_stored=200, water_days=99,
                       counts=None, science=0, logs=300):
        # water_days defaults HIGH so the bootstrap water-storage goal (which keys on
        # days_remaining) does NOT fire; that isolates the drought-buffer path, whose
        # deficit keys on TANK CAPACITY vs drought length, not on current days. The
        # dedup test overrides water_days low to make the bootstrap fire on purpose.
        c = {"LumberjackFlag.Folktails": 1}
        c.update(counts or {})
        return {
            "buildings": {"counts": c},
            "resources": [
                {"good": "Log", "stored": logs, "all_stock": logs},
                {"good": "Water", "stored": water_stored, "days_remaining": water_days},
                {"good": "SciencePoints", "stored": science, "all_stock": science},
            ],
            "population": {"total": pop, "homeless": 0},
            "weather": {"next": {"duration_days": drought}},
        }

    def test_deficit_emits_a_tank_goal(self):
        state = self._drought_state()
        prep = resource_manager.drought_prep(state)
        self.assertGreater(prep["deficit"], 0)          # precondition: buffer needed
        ids = [g["id"] for g in planner.analyze(state, None)]
        self.assertTrue({"build_small_tank", "build_large_tank"} & set(ids))

    def test_tank_goal_matches_recommendation(self):
        # pop10 / 8-day drought -> deficit ~225 (>120) -> LargeTank recommended, but
        # LargeTank (sci 600) is NOT unlockable at 0 science, so we FALL BACK to the
        # always-available SmallTank rather than emitting nothing.
        state = self._drought_state(science=0)
        prep = resource_manager.drought_prep(state)
        self.assertGreater(prep["build"]["LargeTank"], 0)
        ids = [g["id"] for g in planner.analyze(state, None)]
        self.assertIn("build_small_tank", ids)          # fallback: LargeTank locked
        self.assertNotIn("build_large_tank", ids)

    def test_large_tank_emitted_when_unlockable(self):
        state = self._drought_state(science=600)
        prep = resource_manager.drought_prep(state)
        self.assertGreater(prep["build"]["LargeTank"], 0)
        self.assertIn("LargeTank", set(economy.unlockable_now(state)))
        ids = [g["id"] for g in planner.analyze(state, None)]
        self.assertIn("build_large_tank", ids)

    def test_no_deficit_emits_no_tank_goal(self):
        # Well-buffered + tiny drought + high water days -> deficit 0 and the bootstrap
        # water-storage goal does not fire either -> no tank goal at all.
        state = self._drought_state(
            drought=1, water_stored=500, water_days=99, counts={"LargeTank.Folktails": 2}
        )
        self.assertEqual(resource_manager.drought_prep(state)["deficit"], 0)
        ids = [g["id"] for g in planner.analyze(state, None)]
        self.assertNotIn("build_small_tank", ids)
        self.assertNotIn("build_large_tank", ids)

    def test_no_duplicate_of_bootstrap_water_storage(self):
        # Water days LOW so the bootstrap emits build_water_storage (== a SmallTank),
        # AND drought_prep also wants a SmallTank. The drought path must DEDUP against
        # the bootstrap goal (same building) and not add a second build_small_tank.
        state = self._drought_state(pop=5, drought=3, water_stored=5, water_days=1)
        prep = resource_manager.drought_prep(state)
        self.assertGreater(prep["build"]["SmallTank"], 0)     # deficit small -> SmallTank
        ids = [g["id"] for g in planner.analyze(state, None)]
        self.assertIn("build_water_storage", ids)             # bootstrap fired
        self.assertNotIn("build_small_tank", ids)             # deduped, not duplicated


class ReservoirEngineeringTests(unittest.TestCase):
    """3e wiring: a BOUNDED reservoir-engineering goal (Dam/Levee/Floodgate).

    The reservoir suggestion is capped: at most ONE per cycle, emitted only while
    the drought is long, the colony still has a water deficit, and NO reservoir-
    engineering building exists yet. Because a Dam/Levee does NOT reduce the tank
    deficit, this is gated on building EXISTENCE (not the deficit magnitude), so it
    cannot loop -- once one exists it is never re-suggested.
    """

    RESERVOIR_IDS = {"build_dam", "build_levee", "build_floodgate", "build_double_floodgate"}

    def _long_drought_state(self, drought=8, counts=None, pop=15, science=0, logs=300):
        c = {"LumberjackFlag.Folktails": 1}
        c.update(counts or {})
        return {
            "buildings": {"counts": c},
            "resources": [
                {"good": "Log", "stored": logs, "all_stock": logs},
                {"good": "Water", "stored": 10, "days_remaining": 2},
                {"good": "SciencePoints", "stored": science, "all_stock": science},
            ],
            "population": {"total": pop, "homeless": 0},
            "weather": {"next": {"duration_days": drought}},
        }

    def test_long_drought_emits_one_reservoir_goal(self):
        ids = [g["id"] for g in planner.analyze(self._long_drought_state(), None)]
        reservoir = [i for i in ids if i in self.RESERVOIR_IDS]
        self.assertEqual(len(reservoir), 1)             # bounded: exactly one
        self.assertEqual(reservoir[0], "build_dam")     # Dam (sci 0) is the default

    def test_reservoir_suppressed_when_one_already_exists(self):
        # A built Dam means the bound is hit -> no further reservoir suggestion.
        state = self._long_drought_state(counts={"Dam.Folktails": 1})
        ids = [g["id"] for g in planner.analyze(state, None)]
        self.assertFalse(self.RESERVOIR_IDS & set(ids))

    def test_short_drought_emits_no_reservoir_goal(self):
        state = self._long_drought_state(drought=3)      # short (< long threshold)
        ids = [g["id"] for g in planner.analyze(state, None)]
        self.assertFalse(self.RESERVOIR_IDS & set(ids))

    def test_reservoir_does_not_loop_on_a_large_persistent_deficit(self):
        # A huge deficit must NOT scale reservoir emission (that would never terminate,
        # since a dam doesn't cut the tank deficit): still at most ONE reservoir goal.
        state = self._long_drought_state(pop=60, drought=10)
        ids = [g["id"] for g in planner.analyze(state, None)]
        self.assertLessEqual(len([i for i in ids if i in self.RESERVOIR_IDS]), 1)


class StorageDroughtActionSpaceTests(unittest.TestCase):
    """3e regression guard: every emitted storage/drought goal id is a real action."""

    STORAGE_DROUGHT_IDS = {
        "build_small_pile", "build_large_pile", "build_underground_pile",
        "build_small_warehouse", "build_medium_warehouse", "build_large_warehouse",
        "build_small_tank", "build_large_tank",
        "build_dam", "build_levee", "build_floodgate", "build_double_floodgate",
    }

    def _states(self):
        base_counts = {
            "LumberjackFlag.Folktails": 1,
            "WaterPump.Folktails": 1,
            "GathererFlag.Folktails": 1,
        }
        states = []
        for science in (0, 250, 600, 1000, 3000):
            # storage-pressured, survival-secure
            states.append({
                "buildings": {"counts": dict(base_counts)},
                "resources": [
                    {"good": "Log", "stored": 300, "all_stock": 300, "capacity": 320},
                    {"good": "Gear", "stored": 95, "capacity": 100},
                    {"good": "Water", "stored": 100, "days_remaining": 99},
                    {"good": "Food", "stored": 100, "days_remaining": 99},
                    {"good": "SciencePoints", "stored": science, "all_stock": science},
                ],
                "population": {"total": 12, "homeless": 0, "free_beds": 5},
                "weather": {"next": {"duration_days": 8}},
            })
            # drought-stressed
            states.append({
                "buildings": {"counts": {"LumberjackFlag.Folktails": 1}},
                "resources": [
                    {"good": "Log", "stored": 300, "all_stock": 300},
                    {"good": "Water", "stored": 5, "days_remaining": 1},
                    {"good": "SciencePoints", "stored": science, "all_stock": science},
                ],
                "population": {"total": 20, "homeless": 0},
                "weather": {"next": {"duration_days": 9}},
            })
        return states

    def test_every_storage_drought_goal_id_is_a_valid_action(self):
        actions = set(game_schema.actions())
        seen_any = False
        for state in self._states():
            for g in planner.analyze(state, None):
                if g["id"] in self.STORAGE_DROUGHT_IDS:
                    seen_any = True
                    self.assertIn(g["id"], actions)
                    # spec must round-trip back to this exact id
                    self.assertEqual(game_schema.spec_to_action(g["spec"]), g["id"])
        self.assertTrue(seen_any, "expected at least one storage/drought goal across states")


if __name__ == "__main__":
    unittest.main()
