"""Coverage test (Task 5a): the regenerated dataset must span the FULL economy,
not just the bare-survival bootstrap trajectory. Before this task,
agent/nlp/dataset.py only ever synthesized bootstrap states (no SciencePoints
dimension at all, so every science-gated producer/amenity/power tier was
permanently locked), even though the expert planner (agent/planner.py Task-3
emitters) can select from all of production chains, well-being amenities,
power, storage pressure and drought tank sizing once bootstrap survival is
satisfied.

This test runs ONLY the fast targeted generators (dataset._economy_family_states
+ a small bootstrap/unreachable slice), NOT the full ~13k-state
dataset._bootstrap_grid_states sweep that `python -m agent.nlp.dataset`
regenerates from - that grid is unchanged from before this task and takes
minutes; this test must stay at normal unit-test speed.

Runnable BOTH ways:
    .venv/bin/python -m unittest agent.nlp.test_dataset_coverage
    .venv/bin/python -m pytest agent/nlp/test_dataset_coverage.py -q
"""

from __future__ import annotations

import unittest

from agent import game_schema
from agent.nlp import dataset


class FullEconomyLabelCoverageTests(unittest.TestCase):
    """At minimum, one example of each major goal family the expert planner
    can emit must survive into the dataset. Not all 91 game_schema.actions()
    build-goals are asserted here - several are provably never emitted by the
    current planner (see the Task 5a report's blind-spot list: e.g.
    build_observatory, build_dam, build_water_wheel, build_small_pile,
    build_mini_lodge and the ContemplationSpot/Lantern/Agora/MudPit amenities
    are always dominated by a cheaper/lower-tier sibling in their own
    emitter)."""

    @classmethod
    def setUpClass(cls):
        states = (
            list(dataset._bootstrap_smoke_states())
            + list(dataset._unreachable_states())
            + list(dataset._economy_family_states())
        )
        rows, vocab, labels = dataset.build(synthetic_states=states)
        cls.rows = rows
        cls.vocab = vocab
        cls.labels = labels
        cls.labels_seen = {r["label"] for r in rows}

    def test_every_row_label_is_a_valid_schema_action(self):
        actions = set(game_schema.actions())
        for row in self.rows:
            self.assertIn(row["label"], actions)

    def test_dataset_is_not_trivially_small(self):
        # Sanity: the fast targeted slice alone should yield several dozen
        # deduped rows spanning a couple dozen distinct labels - if this drops
        # near zero, a recipe (or the dedup key) broke silently.
        self.assertGreater(len(self.rows), 40)
        self.assertGreater(len(self.labels_seen), 15)

    def test_production_chain_goal_covered(self):
        self.assertIn("build_lumber_mill", self.labels_seen)

    def test_production_chain_goal_covered_beyond_the_first_tier(self):
        # Not just the cheapest/first producer - the sweep must reach further
        # down the raw->refined chain too (see _production_chain_states).
        deeper_tier = {"build_gear_workshop", "build_smelter", "build_paper_mill",
                       "build_scavenger_flag", "build_tappers_shack",
                       "build_wood_workshop", "build_printing_press"}
        self.assertTrue(
            deeper_tier.issubset(self.labels_seen),
            "missing: %r" % (deeper_tier - self.labels_seen),
        )

    def test_power_goal_covered(self):
        self.assertIn("build_power_wheel", self.labels_seen)
        self.assertTrue({"build_wind_turbine", "build_large_wind_turbine"} <= self.labels_seen)

    def test_storage_goal_covered(self):
        storage_labels = {
            "build_large_pile", "build_medium_warehouse", "build_large_warehouse",
            "build_medium_tank", "build_large_tank",
        }
        self.assertTrue(storage_labels <= self.labels_seen, self.labels_seen)

    def test_drought_tank_goal_covered(self):
        self.assertTrue({"build_small_tank", "build_large_tank"} <= self.labels_seen)

    def test_wellbeing_goal_covered(self):
        self.assertIn("build_campfire", self.labels_seen)
        self.assertTrue({"build_shrub", "build_shower", "build_lido"} <= self.labels_seen)

    def test_science_scaling_goal_covered(self):
        self.assertIn("build_inventor", self.labels_seen)

    def test_demolish_unreachable_covered(self):
        self.assertIn("demolish_unreachable", self.labels_seen)

    def test_bootstrap_goals_covered(self):
        bootstrap = {
            "build_lumberjack_flag", "build_water_pump", "build_gatherer_flag",
            "build_efficient_farm_house", "build_lodge", "build_small_warehouse",
            "build_inventor", "build_forester",
        }
        overlap = bootstrap & self.labels_seen
        self.assertGreaterEqual(
            len(overlap), 2,
            "expected >=2 bootstrap goals among the smoke states, got: %r" % overlap,
        )


class InjectUnreachableFixTests(unittest.TestCase):
    """Direct regression coverage for Task 5a bug fix #1: _inject_unreachable
    used to write only to buildings.list, a key planner._building_details
    never reads when Oracle.label calls plan_report without an explicit
    buildings_detail argument (it falls back to buildings.detail) - so no
    synthetic state ever actually produced demolish_unreachable."""

    def test_inject_unreachable_state_labels_as_demolish(self):
        from agent.nlp.labeler import Oracle

        state = dataset._base_state()
        dataset._set_counts(state, {"LumberjackFlag": 1, "WaterPump": 1})
        dataset._set_resource(state, "Log", 40, 13.0)
        dataset._set_resource(state, "Water", 100, 20.0)
        dataset._inject_unreachable(state)

        label = Oracle().label(state)
        self.assertEqual(label, "demolish_unreachable")

    def test_inject_unreachable_writes_both_detail_and_list_keys(self):
        state = dataset._base_state()
        dataset._inject_unreachable(state)
        self.assertTrue(state["buildings"]["detail"])
        self.assertTrue(state["buildings"]["list"])
        self.assertFalse(state["buildings"]["detail"][0]["reachable"])


class BuildFailureHandlingTests(unittest.TestCase):
    """Task 5a bug fix #2: a synthetic state that Oracle.label cannot
    translate must raise (loudly), never be silently dropped. Journal-harvested
    states remain best-effort (they are external, possibly malformed)."""

    def test_synthetic_label_failure_raises_instead_of_being_skipped(self):
        class _ExplodingOracle:
            def label(self, state):
                raise ValueError("boom")

        import agent.nlp.dataset as ds_mod

        original = ds_mod.Oracle
        ds_mod.Oracle = _ExplodingOracle
        try:
            with self.assertRaises(RuntimeError):
                dataset.build(synthetic_states=[dataset._base_state()])
        finally:
            ds_mod.Oracle = original


if __name__ == "__main__":
    unittest.main()
