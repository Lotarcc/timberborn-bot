"""Tests: the decision model (features.py) and the behavioral-cloning oracle
(labeler.py) are wired to the DB-driven game_schema action space (Task 4).

Before this task, features.ACTIONS was a hand-coded 14-id planner-namespace list
and labeler.Oracle.label returned raw planner goal ids verbatim - neither is a
member of game_schema.actions() (91 ids after Task 3c's amenity expansion). These
tests pin: (1) features.ACTIONS == game_schema.actions() exactly, and (2)
Oracle.label ALWAYS returns a real game_schema action id, even though
planner.analyze() emits a MIXED namespace (bootstrap goals use planner-only ids
like "build_lumberjack"; Task-3 economy/amenity goals already use schema ids).

Runnable BOTH ways:
    .venv/bin/python -m unittest agent.nlp.test_labeler_schema
    .venv/bin/python -m pytest agent/nlp/test_labeler_schema.py -q
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from agent import game_schema
from agent.nlp import features as feat
from agent.nlp.labeler import Oracle

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _load_fixture(name: str) -> dict:
    with (_FIXTURES / name).open("r", encoding="utf-8") as fh:
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


def _make_state(counts=None, log=0, water_stored=0, water_days=0, food_stored=0,
                 food_days=0, pop_total=13, homeless=0, free_beds=None, drought=3.0,
                 science=0) -> dict:
    """A state_fresh.json-derived fixture with the given knobs overridden. Mirrors
    the state-construction helpers in test_economy.py / nlp/dataset.py so the
    battery below exercises planner.analyze the same way real training data does.
    """
    state = copy.deepcopy(_load_fixture("state_fresh.json"))
    state.setdefault("buildings", {})["counts"] = dict(counts or {})
    _set_resource(state, "Log", log, log / 3.0 if log else 0)
    _set_resource(state, "Water", water_stored, water_days)
    _set_resource(state, "Food", food_stored, food_days)
    _set_resource(state, "SciencePoints", science, 0)
    population = {"total": pop_total, "homeless": homeless}
    if free_beds is not None:
        population["free_beds"] = free_beds
    state["population"] = population
    state.setdefault("weather", {})["next"] = {"duration_days": drought}
    return state


class ActionSpaceTests(unittest.TestCase):
    """features.ACTIONS must be exactly game_schema.actions(): same ids, same
    order, same count (91 after Task 3c's amenity expansion; the Task-4 brief's
    "83" predates that expansion)."""

    def test_actions_count_is_91(self):
        self.assertEqual(len(game_schema.actions()), 91)
        self.assertEqual(len(feat.ACTIONS), 91)

    def test_actions_matches_game_schema_exactly(self):
        self.assertEqual(feat.ACTIONS, list(game_schema.actions()))

    def test_action_index_covers_every_action_exactly_once(self):
        self.assertEqual(set(feat.ACTION_INDEX), set(feat.ACTIONS))
        self.assertEqual(len(feat.ACTION_INDEX), len(feat.ACTIONS))


class OracleSchemaTranslationTests(unittest.TestCase):
    """Oracle.label must ALWAYS return a game_schema.actions() id, even though
    planner.analyze() emits a mixed planner-id/game_schema-id namespace for
    bootstrap goals. Each case below was probed directly against the (unfixed)
    planner/controller to confirm exactly which goal gets selected, so the
    assertions pin the SPECIFIC translated id, not just set membership."""

    def setUp(self):
        self.oracle = Oracle()
        self.actions = set(game_schema.actions())

    def test_fresh_colony_translates_bootstrap_lumberjack(self):
        # No buildings at all: the planner's free bootstrap goal is
        # {"id": "build_lumberjack", "spec": "LumberjackFlag"} - a planner-only id.
        # spec_to_action("LumberjackFlag") resolves it to the schema id.
        state = _make_state()
        label = self.oracle.label(state)
        self.assertEqual(label, "build_lumberjack_flag")
        self.assertIn(label, self.actions)

    def test_no_food_translates_bootstrap_gatherer(self):
        # Lumberjack + pump exist and are well-stocked; no food production yet ->
        # {"id": "build_gatherer", "spec": "GathererFlag"} -> build_gatherer_flag.
        state = _make_state(
            counts={"LumberjackFlag": 1, "WaterPump": 1},
            log=40, water_stored=100, water_days=20, food_stored=0, food_days=0,
        )
        label = self.oracle.label(state)
        self.assertEqual(label, "build_gatherer_flag")
        self.assertIn(label, self.actions)

    def test_farm_needed_resolves_via_spec_to_action(self):
        # Gatherer exists but food is running low ->
        # {"id": "build_farm", "spec": "EfficientFarmHouse"}. planner.GOAL_SPECS
        # now carries the real buildings.json spelling (capital "H"), so
        # game_schema.spec_to_action("EfficientFarmHouse") resolves directly to
        # build_efficient_farm_house - the labeler's _ALIAS fallback (still
        # covered in isolation by
        # ToSchemaIdHelperTests.test_alias_used_only_when_spec_is_unresolvable)
        # is no longer needed for this real, planner-driven scenario.
        state = _make_state(
            counts={"LumberjackFlag": 1, "WaterPump": 1, "GathererFlag": 1},
            log=40, water_stored=100, water_days=20, food_stored=2, food_days=1.0,
        )
        self.assertEqual(
            game_schema.spec_to_action("EfficientFarmHouse"),
            "build_efficient_farm_house",
        )  # precondition
        label = self.oracle.label(state)
        self.assertEqual(label, "build_efficient_farm_house")
        self.assertIn(label, self.actions)

    def test_mid_economy_returns_native_schema_id(self):
        # Full bootstrap satisfied, resources ample: a Task-3 economy goal is
        # selected next, and its id is ALREADY a game_schema id (spec_to_action
        # round-trips to the same id the planner emitted) - translation is a no-op
        # here, which the assertion below exercises just as much as the aliasing
        # cases above.
        state = _make_state(
            counts={
                "LumberjackFlag": 1, "WaterPump": 1, "GathererFlag": 1,
                "SmallTank": 5, "Lodge": 1, "EfficientFarmHouse": 1,
                "SmallWarehouse": 1, "Inventor": 1, "Forester": 1,
            },
            log=200, water_stored=100, water_days=20, food_stored=100, food_days=20,
            science=150, free_beds=5,
        )
        label = self.oracle.label(state)
        self.assertEqual(label, "build_lumber_mill")
        self.assertIn(label, self.actions)

    def test_unreachable_building_translates_to_demolish_verb(self):
        state = _make_state(
            counts={"LumberjackFlag": 1, "WaterPump": 1}, log=40,
            water_stored=100, water_days=20,
        )
        # planner._building_details reads buildings.detail (or the top-level
        # buildings_detail key) - NOT buildings.list - when plan_report is called
        # without an explicit buildings_detail argument, exactly as Oracle does.
        state["buildings"]["detail"] = [
            {"spec": "WaterPump", "status": "finished", "reachable": False,
             "x": 4, "y": 5, "z": 6}
        ]
        label = self.oracle.label(state)
        self.assertEqual(label, "demolish_unreachable")
        self.assertIn(label, self.actions)

    def test_battery_every_label_is_a_valid_schema_action(self):
        """Broad sweep across the bootstrap-to-economy trajectory - every state's
        label must land in game_schema.actions(), regardless of which
        planner-namespace goal was selected underneath. A translation gap would
        show up here even if it isn't one of the specific cases pinned above."""
        counts_progression = [
            {},
            {"LumberjackFlag": 1},
            {"LumberjackFlag": 1, "GathererFlag": 1},
            {"LumberjackFlag": 1, "GathererFlag": 1, "WaterPump": 1},
            {"LumberjackFlag": 1, "GathererFlag": 1, "WaterPump": 1, "SmallTank": 4},
            {"LumberjackFlag": 1, "GathererFlag": 1, "WaterPump": 1, "SmallTank": 4,
             "Lodge": 1},
            {"LumberjackFlag": 1, "GathererFlag": 1, "WaterPump": 1, "SmallTank": 4,
             "Lodge": 1, "EfficientFarmHouse": 1},
            {"LumberjackFlag": 1, "GathererFlag": 1, "WaterPump": 1, "SmallTank": 4,
             "Lodge": 1, "EfficientFarmHouse": 1, "SmallWarehouse": 1},
            {"LumberjackFlag": 1, "GathererFlag": 1, "WaterPump": 1, "SmallTank": 4,
             "Lodge": 1, "EfficientFarmHouse": 1, "SmallWarehouse": 1, "Inventor": 1},
            {"LumberjackFlag": 1, "GathererFlag": 1, "WaterPump": 1, "SmallTank": 5,
             "Lodge": 2, "EfficientFarmHouse": 1, "SmallWarehouse": 1, "Inventor": 1,
             "Forester": 1},
        ]
        logs = (0, 6, 20, 200)
        seen_labels = set()
        for counts in counts_progression:
            for log in logs:
                state = _make_state(
                    counts=counts, log=log,
                    water_stored=log * 2, water_days=5.0,
                    food_stored=log * 2, food_days=5.0,
                    science=150, free_beds=5,
                )
                label = self.oracle.label(state)
                seen_labels.add(label)
                self.assertIn(
                    label, self.actions,
                    "Oracle.label(counts=%r, log=%r) -> %r is not a member of "
                    "game_schema.actions()" % (counts, log, label),
                )
        # Sanity: the sweep must actually traverse several different decisions,
        # not degenerate into always returning e.g. "advance_time".
        self.assertGreater(len(seen_labels), 3)


class ToSchemaIdHelperTests(unittest.TestCase):
    """White-box tests directly on the translation helper. The numbered
    demolish_unreachable_N variant can never actually be RETURNED by
    Oracle.label (the unnumbered "demolish_unreachable" goal is always inserted
    first and the label loop returns on the first match), so it is exercised here
    against the helper directly instead."""

    def setUp(self):
        self.actions = set(game_schema.actions())

    def test_numbered_demolish_variant_collapses_to_the_verb(self):
        from agent.nlp.labeler import _to_schema_id
        goal = {"id": "demolish_unreachable_3", "spec": "WaterPump"}
        self.assertEqual(_to_schema_id(goal, self.actions), "demolish_unreachable")

    def test_spec_resolves_before_alias_is_consulted(self):
        from agent.nlp.labeler import _to_schema_id
        goal = {"id": "build_lumberjack", "spec": "LumberjackFlag"}
        self.assertEqual(_to_schema_id(goal, self.actions), "build_lumberjack_flag")

    def test_alias_used_only_when_spec_is_unresolvable(self):
        from agent.nlp.labeler import _to_schema_id
        goal = {"id": "build_farm", "spec": "EfficientFarmhouse"}
        self.assertEqual(_to_schema_id(goal, self.actions), "build_efficient_farm_house")

    def test_verb_ids_pass_through_unchanged(self):
        from agent.nlp.labeler import _to_schema_id
        for goal_id in ("advance_time", "designate_cutting", "designate_planting"):
            goal = {"id": goal_id}
            self.assertEqual(_to_schema_id(goal, self.actions), goal_id)

    def test_build_forester_resolves_via_spec_to_action(self):
        # planner.GOAL_SPECS now carries "build_forester" -> "Forester" (the real
        # buildings.json spec), so spec_to_action resolves it directly - the same
        # branch every other real-spec goal uses. This is what the planner
        # actually emits today (see the synthetic membership-fallback test below
        # for the historical/edge-case path this coincided with pre-rename).
        from agent.nlp.labeler import _to_schema_id
        self.assertEqual(
            game_schema.spec_to_action("Forester"), "build_forester"
        )  # precondition
        goal = {"id": "build_forester", "spec": "Forester"}
        self.assertEqual(_to_schema_id(goal, self.actions), "build_forester")

    def test_membership_fallback_when_spec_unresolvable_but_id_already_valid(self):
        # Synthetic edge case, not something the real planner emits anymore: if a
        # goal's spec is unresolvable (spec_to_action returns None) but its id
        # already happens to be a member of game_schema.actions(), membership
        # passes it through WITHOUT ever consulting _ALIAS. Before the buildings-
        # spec-name fix this was planner.py's ACTUAL behavior for build_forester
        # (whose GOAL_SPECS entry was misspelled "ForesterFlag", which
        # coincidentally still resolved by id membership); kept here as direct
        # coverage of the membership branch itself now that build_forester's real
        # path is spec_to_action (see the test above).
        from agent.nlp.labeler import _to_schema_id
        self.assertIsNone(game_schema.spec_to_action("ForesterFlag"))  # precondition
        goal = {"id": "build_forester", "spec": "ForesterFlag"}
        self.assertEqual(_to_schema_id(goal, self.actions), "build_forester")


class StateFeaturizerTests(unittest.TestCase):
    """StateFeaturizer keeps its fit/transform/to_dict/from_dict API but now
    sources feature strings from game_schema.feature_strings."""

    def test_feature_strings_delegates_to_game_schema(self):
        state = _make_state(counts={"LumberjackFlag": 1}, log=10)
        self.assertEqual(feat.feature_strings(state), game_schema.feature_strings(state))

    def test_fit_transform_round_trips_and_produces_a_multi_hot_vector(self):
        states = [
            _make_state(),
            _make_state(counts={"LumberjackFlag": 1}, log=10),
            _make_state(counts={"LumberjackFlag": 1, "WaterPump": 1}, log=40,
                        water_stored=100, water_days=20, science=150),
        ]
        featurizer = feat.StateFeaturizer.fit(states)
        vec = featurizer.transform(states[-1])
        self.assertIsInstance(vec, list)
        self.assertEqual(len(vec), len(featurizer.vocab))
        self.assertTrue(all(v in (0, 1) for v in vec))
        self.assertGreater(sum(vec), 0)

        restored = feat.StateFeaturizer.from_dict(featurizer.to_dict())
        self.assertEqual(restored.vocab, featurizer.vocab)
        self.assertEqual(restored.transform(states[-1]), vec)


if __name__ == "__main__":
    unittest.main()
