"""Colony curriculum / phase manager for the Timberborn agent.

Classifies the current colony into a coarse play phase - "what should the
colony be working on right now" - and biases a trained model's ranked
intents (`DecisionPolicy.rank(state)` output) so the phase-appropriate goals
lead the list. This module does not execute anything and does not talk to
the bridge; it is pure planning support that plugs into the play loop
(Task 6): the loop asks `current_phase(state)` for telemetry/logging, then
calls `bias_ranking(state, ranked)` before picking the top intent.

Phase order (`current_phase` returns the FIRST one whose exit criterion is
not yet met; STABLE is terminal, meaning every criterion is met):

    SURVIVE_WATER -> SECURE_FOOD -> HOUSE -> DROUGHT_PROOF -> GROW -> STABLE

Exit criteria:
    SURVIVE_WATER  a WaterPump exists AND >=2 SmallTank AND
                   water_days > drought_len + 2
    SECURE_FOOD    a GathererFlag exists AND an EfficientFarmHouse exists AND
                   food_days > drought_len + 2
    HOUSE          homeless == 0 AND free_beds > 0
    DROUGHT_PROOF  resource_manager.drought_prep(state)["deficit"] <= 0
    GROW           population.total >= 30
    STABLE         terminal (all of the above met)

Public API
----------
    current_phase(state) -> str
    phase_priorities(phase) -> list[goal_id]
    is_goal_reached(state) -> bool
    bias_ranking(state, ranked) -> list[(goal_id, confidence)]

Python 3 standard library only - no third-party packages, no network calls.
Runs its own tests: `python3 agent/curriculum.py`.
"""

from __future__ import annotations

import json
import os
import unittest

# `agent/` has no __init__.py, and this module is designed to run BOTH
# standalone (`python3 agent/curriculum.py`, where sys.path[0] is agent/
# itself so the bare sibling import resolves) and package-qualified
# (`python3 -m unittest agent.curriculum` from the repo root, where the
# package-relative import resolves instead). Same fallback shape validated
# by agent/replay.py's goal_id regression test.
try:
    from agent import game_schema, resource_manager
except ImportError:
    import game_schema
    import resource_manager

_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_AGENT_DIR, "data")
_GOODS_PATH = os.path.join(_DATA_DIR, "goods.json")

# --- phase vocabulary ---------------------------------------------------------
SURVIVE_WATER = "SURVIVE_WATER"
SECURE_FOOD = "SECURE_FOOD"
HOUSE = "HOUSE"
DROUGHT_PROOF = "DROUGHT_PROOF"
GROW = "GROW"
STABLE = "STABLE"

# Evaluation order for current_phase: first unmet criterion wins.
PHASES = (SURVIVE_WATER, SECURE_FOOD, HOUSE, DROUGHT_PROOF, GROW, STABLE)

_GROW_POP_TARGET = 30
# Water/food buffer must clear (drought length + this margin) days for
# SURVIVE_WATER / SECURE_FOOD to be considered exited.
_EXIT_MARGIN_DAYS = 2.0

# Bare (pre-faction-suffix) building specs used by the exit-criteria checks.
# Resolved through game_schema.action_to_spec so they can never silently
# drift from the DB-driven action space (mirrors phase_priorities below,
# which must stay real goal_ids too).
_SPEC_WATER_PUMP = game_schema.action_to_spec("build_water_pump")
_SPEC_SMALL_TANK = game_schema.action_to_spec("build_small_tank")
_SPEC_GATHERER = game_schema.action_to_spec("build_gatherer_flag")
_SPEC_FARMHOUSE = game_schema.action_to_spec("build_efficient_farm_house")

# Real goal_id strings from game_schema.actions() - verified live against the
# DB-driven action space, NOT the plan-doc placeholder names (build_gatherer/
# build_farm/build_water_storage do not exist). See .superpowers/sdd/progress.md.
_PHASE_PRIORITIES = {
    SURVIVE_WATER: ["build_water_pump", "build_small_tank"],
    SECURE_FOOD: ["build_gatherer_flag", "build_efficient_farm_house"],
    HOUSE: ["build_mini_lodge", "build_lodge", "build_double_lodge"],
    DROUGHT_PROOF: [
        "build_small_tank", "build_large_tank", "build_levee", "build_dam", "build_floodgate",
    ],
    GROW: ["build_lodge", "build_double_lodge", "build_triple_lodge"],
    STABLE: [],
}


# ---------------------------------------------------------------------------
# small state helpers - self-contained (mirrors agent/replay.py's pattern) so
# this module doesn't reach into resource_manager's private/underscored
# helpers, only its four public functions.
# ---------------------------------------------------------------------------

def _num(value, default=0.0):
    """Best-effort numeric coercion that never raises."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


_FOOD_GOODS_CACHE = None


def _food_goods():
    """Set of good ids where goods.json marks is_food true."""
    global _FOOD_GOODS_CACHE
    if _FOOD_GOODS_CACHE is None:
        with open(_GOODS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        _FOOD_GOODS_CACHE = {
            g["id"] for g in data.get("goods", []) if isinstance(g, dict) and g.get("is_food")
        }
    return _FOOD_GOODS_CACHE


def _resources(state):
    value = (state or {}).get("resources")
    return value if isinstance(value, list) else []


def _resource_field(state, good, field, default=0.0):
    for item in _resources(state):
        if isinstance(item, dict) and item.get("good") == good:
            return _num(item.get(field), default)
    return default


def _water_days(state):
    return _resource_field(state, "Water", "days_remaining", 0.0)


def _food_days(state):
    """max days_remaining over resources whose good is_food (0.0 if none present)."""
    foods = _food_goods()
    best = 0.0
    for item in _resources(state):
        if isinstance(item, dict) and item.get("good") in foods:
            best = max(best, _num(item.get("days_remaining"), 0.0))
    return best


def _population(state):
    value = (state or {}).get("population")
    return value if isinstance(value, dict) else {}


def _pop_total(state):
    return _num(_population(state).get("total"), 0.0)


def _homeless(state):
    return _num(_population(state).get("homeless"), 0.0)


def _free_beds(state):
    return _num(_population(state).get("free_beds"), 0.0)


def _building_counts(state):
    counts = ((state or {}).get("buildings") or {}).get("counts")
    return counts if isinstance(counts, dict) else {}


def _has_building(building_counts, spec, minimum=1):
    """True if building_counts has key(s) whose bare (pre-faction) prefix ==
    spec, summing to >= minimum. Keys are faction-suffixed e.g.
    'WaterPump.Folktails'."""
    total = 0.0
    for key, value in (building_counts or {}).items():
        if str(key).split(".")[0] == spec:
            total += _num(value, 0.0)
    return total >= minimum


def _drought_len(state):
    """Longest forecast drought (days), via resource_manager.analyze."""
    return _num(resource_manager.analyze(state or {}).get("drought_days"), 0.0)


# ---------------------------------------------------------------------------
# phase exit criteria
# ---------------------------------------------------------------------------

def _survive_water_met(state, counts, drought_len):
    return (
        _has_building(counts, _SPEC_WATER_PUMP, 1)
        and _has_building(counts, _SPEC_SMALL_TANK, 2)
        and _water_days(state) > drought_len + _EXIT_MARGIN_DAYS
    )


def _secure_food_met(state, counts, drought_len):
    return (
        _has_building(counts, _SPEC_GATHERER, 1)
        and _has_building(counts, _SPEC_FARMHOUSE, 1)
        and _food_days(state) > drought_len + _EXIT_MARGIN_DAYS
    )


def _house_met(state):
    return _homeless(state) == 0 and _free_beds(state) > 0


def _drought_proof_met(state):
    return _num(resource_manager.drought_prep(state or {}).get("deficit"), 0.0) <= 0


def _grow_met(state):
    return _pop_total(state) >= _GROW_POP_TARGET


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def current_phase(state):
    """First phase (SURVIVE_WATER..GROW, in order) whose exit criterion is
    not yet met; STABLE once every criterion is met."""
    state = state if isinstance(state, dict) else {}
    counts = _building_counts(state)
    drought_len = _drought_len(state)

    if not _survive_water_met(state, counts, drought_len):
        return SURVIVE_WATER
    if not _secure_food_met(state, counts, drought_len):
        return SECURE_FOOD
    if not _house_met(state):
        return HOUSE
    if not _drought_proof_met(state):
        return DROUGHT_PROOF
    if not _grow_met(state):
        return GROW
    return STABLE


def phase_priorities(phase):
    """Ordered goal_ids (members of game_schema.actions()) this phase should
    bias toward the front of a ranked intent list. Unknown phase -> []."""
    return list(_PHASE_PRIORITIES.get(phase, []))


def is_goal_reached(state):
    """True iff the colony has reached the terminal STABLE goal: population
    >= 30, water_days and food_days both exceed the longest forecast
    drought, and nobody is homeless."""
    state = state if isinstance(state, dict) else {}
    drought_len = _drought_len(state)
    return (
        _pop_total(state) >= _GROW_POP_TARGET
        and _water_days(state) > drought_len
        and _food_days(state) > drought_len
        and _homeless(state) == 0
    )


def bias_ranking(state, ranked):
    """Stable-sort `ranked` (`[(goal_id, confidence), ...]`, best-first, as
    returned by `DecisionPolicy.rank`) so the current phase's
    `phase_priorities` goal_ids lead, preserving each group's original
    relative (confidence) order. Never drops or duplicates entries."""
    items = list(ranked) if ranked else []
    priority_ids = set(phase_priorities(current_phase(state)))
    return sorted(items, key=lambda entry: 0 if entry[0] in priority_ids else 1)


__all__ = [
    "SURVIVE_WATER", "SECURE_FOOD", "HOUSE", "DROUGHT_PROOF", "GROW", "STABLE", "PHASES",
    "current_phase", "phase_priorities", "is_goal_reached", "bias_ranking",
]


# ---------------------------------------------------------------------------
# inline tests
# ---------------------------------------------------------------------------

class CurriculumTests(unittest.TestCase):

    # -- state synthesis helpers ---------------------------------------------

    @staticmethod
    def _state(pop_total, homeless, free_beds, water_days, food_days, counts, drought=3.0):
        return {
            "population": {"total": pop_total, "homeless": homeless, "free_beds": free_beds},
            "resources": [
                {"good": "Water", "stored": max(water_days, 0) * 10, "days_remaining": water_days},
                {"good": "Berries", "stored": max(food_days, 0) * 10, "days_remaining": food_days},
            ],
            "buildings": {"counts": dict(counts)},
            "weather": {"next": {"duration_days": drought}},
        }

    @classmethod
    def _drought_proofed(cls, pop, base_counts, drought=3.0):
        """base_counts (already SURVIVE_WATER/SECURE_FOOD/HOUSE-sufficient)
        plus however many extra tanks resource_manager.drought_prep
        recommends so deficit <= 0. Computed dynamically (not hand-derived)
        so this stays correct if the water-buffer constants ever change."""
        counts = dict(base_counts)
        probe = cls._state(pop, 0, 3, 100.0, 100.0, counts, drought)
        prep = resource_manager.drought_prep(probe)
        if prep["build"]["SmallTank"]:
            counts["SmallTank.Folktails"] = counts.get("SmallTank.Folktails", 0) + prep["build"]["SmallTank"]
        if prep["build"]["LargeTank"]:
            counts["LargeTank.Folktails"] = counts.get("LargeTank.Folktails", 0) + prep["build"]["LargeTank"]
        return counts

    # Base building set once SURVIVE_WATER + SECURE_FOOD + HOUSE are cleared.
    _BASE_COUNTS = {
        "DistrictCenter.Folktails": 1,
        "WaterPump.Folktails": 1,
        "SmallTank.Folktails": 2,
        "GathererFlag.Folktails": 1,
        "EfficientFarmHouse.Folktails": 1,
        "MiniLodge.Folktails": 1,
    }

    def _phase_states(self):
        """One representative state per phase, each satisfying every EARLIER
        phase's exit criterion but not its own (STABLE satisfies all six)."""
        states = {}

        # SURVIVE_WATER: nothing built yet.
        states[SURVIVE_WATER] = self._state(
            pop_total=8, homeless=8, free_beds=0,
            water_days=2.0, food_days=1.0,
            counts={"DistrictCenter.Folktails": 1},
        )

        # SECURE_FOOD: water solved, no food producers yet.
        states[SECURE_FOOD] = self._state(
            pop_total=10, homeless=10, free_beds=0,
            water_days=10.0, food_days=2.0,
            counts={"DistrictCenter.Folktails": 1, "WaterPump.Folktails": 1,
                    "SmallTank.Folktails": 2},
        )

        # HOUSE: water + food solved, colony still homeless.
        states[HOUSE] = self._state(
            pop_total=15, homeless=5, free_beds=0,
            water_days=10.0, food_days=10.0,
            counts={"DistrictCenter.Folktails": 1, "WaterPump.Folktails": 1,
                    "SmallTank.Folktails": 2, "GathererFlag.Folktails": 1,
                    "EfficientFarmHouse.Folktails": 1},
        )

        # DROUGHT_PROOF: everyone housed, but buffer capacity is way under
        # what drought_prep requires for this population (only the 2 starter
        # SmallTanks - no scale-up yet).
        states[DROUGHT_PROOF] = self._state(
            pop_total=20, homeless=0, free_beds=3,
            water_days=10.0, food_days=10.0,
            counts=self._BASE_COUNTS,
        )

        # GROW: drought-proofed, population still under the 30 target.
        states[GROW] = self._state(
            pop_total=25, homeless=0, free_beds=3,
            water_days=10.0, food_days=10.0,
            counts=self._drought_proofed(25, self._BASE_COUNTS),
        )

        # STABLE: every criterion met, including pop >= 30.
        states[STABLE] = self._state(
            pop_total=30, homeless=0, free_beds=3,
            water_days=10.0, food_days=10.0,
            counts=self._drought_proofed(30, self._BASE_COUNTS),
        )

        return states

    # -- current_phase --------------------------------------------------------

    def test_current_phase_matches_each_synthesized_state(self):
        states = self._phase_states()
        for phase in PHASES:
            with self.subTest(phase=phase):
                self.assertEqual(current_phase(states[phase]), phase)

    def test_current_phase_empty_state_defaults_to_survive_water(self):
        self.assertEqual(current_phase({}), SURVIVE_WATER)
        self.assertEqual(current_phase(None), SURVIVE_WATER)

    # -- is_goal_reached --------------------------------------------------------

    def test_is_goal_reached_true_only_for_stable_state(self):
        states = self._phase_states()
        for phase in PHASES:
            with self.subTest(phase=phase):
                self.assertEqual(is_goal_reached(states[phase]), phase == STABLE)

    # -- phase_priorities / regression guard -----------------------------------

    def test_phase_priorities_are_valid_game_schema_actions(self):
        valid = set(game_schema.actions())
        for phase in PHASES:
            goals = phase_priorities(phase)
            self.assertIsInstance(goals, list)
            for goal_id in goals:
                self.assertIn(
                    goal_id, valid,
                    "phase_priorities(%r) returned %r, not in game_schema.actions()"
                    % (phase, goal_id),
                )

    def test_phase_priorities_stable_is_empty(self):
        self.assertEqual(phase_priorities(STABLE), [])

    def test_phase_priorities_unknown_phase_is_empty(self):
        self.assertEqual(phase_priorities("NOT_A_PHASE"), [])

    def test_exit_criteria_spec_constants_resolve_to_real_specs(self):
        # If game_schema.action_to_spec ever failed to resolve one of these
        # (e.g. a DB rename), _has_building would silently never match and
        # current_phase would get permanently stuck at SURVIVE_WATER instead
        # of raising - guard the resolution loudly here instead.
        for spec in (_SPEC_WATER_PUMP, _SPEC_SMALL_TANK, _SPEC_GATHERER, _SPEC_FARMHOUSE):
            self.assertIsInstance(spec, str)
            self.assertTrue(spec)

    # -- bias_ranking -------------------------------------------------------

    def test_bias_ranking_promotes_survive_water_priorities_and_keeps_order(self):
        state = self._phase_states()[SURVIVE_WATER]
        ranked = [
            ("build_lodge", 0.9),
            ("build_gatherer_flag", 0.8),
            ("build_small_tank", 0.7),
            ("advance_time", 0.6),
            ("build_water_pump", 0.5),
            ("build_lumberjack_flag", 0.4),
        ]

        biased = bias_ranking(state, ranked)

        self.assertEqual(
            biased,
            [
                ("build_small_tank", 0.7),
                ("build_water_pump", 0.5),
                ("build_lodge", 0.9),
                ("build_gatherer_flag", 0.8),
                ("advance_time", 0.6),
                ("build_lumberjack_flag", 0.4),
            ],
        )
        # no drop / duplicate: same multiset of entries, same length.
        self.assertEqual(len(biased), len(ranked))
        self.assertEqual(set(biased), set(ranked))

    def test_bias_ranking_uses_the_current_phase_not_a_fixed_one(self):
        # Same kind of list, different phase (HOUSE) -> different promotion,
        # and priority items keep THEIR OWN original relative order (which
        # here deliberately differs from confidence order and from
        # phase_priorities' own list order) to prove the sort is stable.
        state = self._phase_states()[HOUSE]
        ranked = [
            ("build_water_pump", 0.9),
            ("build_lodge", 0.7),
            ("advance_time", 0.6),
            ("build_double_lodge", 0.5),
            ("build_mini_lodge", 0.4),
        ]

        biased = bias_ranking(state, ranked)

        self.assertEqual(
            biased,
            [
                ("build_lodge", 0.7),
                ("build_double_lodge", 0.5),
                ("build_mini_lodge", 0.4),
                ("build_water_pump", 0.9),
                ("advance_time", 0.6),
            ],
        )
        self.assertEqual(len(biased), len(ranked))
        self.assertEqual(set(biased), set(ranked))

    def test_bias_ranking_stable_phase_is_a_no_op_reorder(self):
        state = self._phase_states()[STABLE]
        ranked = [("build_lodge", 0.5), ("advance_time", 0.4), ("build_dam", 0.3)]
        self.assertEqual(bias_ranking(state, ranked), ranked)

    def test_bias_ranking_handles_empty_inputs(self):
        self.assertEqual(bias_ranking({}, []), [])
        self.assertEqual(bias_ranking({}, None), [])


if __name__ == "__main__":
    unittest.main()
