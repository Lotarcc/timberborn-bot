"""Expert oracle: given a game state, return the goal_id the deterministic
planner+controller would pick. This is the behavioral-cloning label source - the
learned heads are trained to imitate (and later, via LLM/outcome relabeling,
improve on) this policy.

The planner needs a map and a /resources payload to test placement feasibility.
For synthetic training states we hold those fixed to the fresh-map fixtures: the
WHAT decision is driven by resources/buildings/needs in `state`, which is exactly
what we vary. WHERE (which the map affects) is not what we are training here.

NAMESPACE TRANSLATION: planner.analyze() emits a MIXED namespace. Bootstrap goals
(build_lumberjack, build_water_pump, build_water_storage, build_gatherer,
build_farm, build_warehouse, ...) predate game_schema and use planner-only ids that
are NOT members of game_schema.actions(); the Task-3 economy/amenity/power/storage
goals already use game_schema ids (the planner builds them FROM
game_schema.spec_to_action). Since the model's label space is game_schema.actions()
(see features.py), every goal this oracle selects must be translated to a schema id
before it is returned - _to_schema_id below is that translation, and Oracle.label
asserts the result so a future planner change can never silently emit an
untranslatable goal into the training data.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from agent import controller, game_schema, planner

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

# Historical note: planner.GOAL_SPECS used to carry "build_farm" -> spec
# "EfficientFarmhouse" (lowercase "h") while the real spec in buildings.json is
# "EfficientFarmHouse", so game_schema.spec_to_action returned None for the
# planner's spelling and this alias was the only way to translate build_farm to a
# schema id. planner.py now uses the real buildings.json spec names
# ("EfficientFarmHouse", "Forester") throughout, so spec_to_action resolves
# build_farm and build_forester directly like every other bootstrap goal
# (build_lumberjack, build_water_pump, build_water_storage, build_gatherer,
# build_lodge, build_warehouse, build_inventor) - this alias is no longer
# exercised by the real planner. Kept as a defensive fallback (belt-and-suspenders)
# for any goal dict that still carries the old misspelling; see
# ToSchemaIdHelperTests.test_alias_used_only_when_spec_is_unresolvable in
# agent/nlp/test_labeler_schema.py for direct coverage of the fallback path.
_ALIAS = {"build_farm": "build_efficient_farm_house"}


def _to_schema_id(goal: dict, actions_set: set) -> str:
    """Translate one planner goal dict to a valid game_schema.actions() id.

    Order matters: demolish first (it never carries a game_schema spec), then
    spec_to_action (resolves every real-spec goal, bootstrap or economy - this
    includes build_farm and build_forester now that planner.py emits their real
    buildings.json spec names), then verbatim membership (catches verb actions),
    and finally the hand-verified _ALIAS as a last-resort fallback for a goal
    whose spec doesn't resolve.
    """
    gid = str(goal.get("id", ""))
    if gid.startswith("demolish_unreachable"):
        return "demolish_unreachable"          # a valid verb action
    spec = goal.get("spec")
    if spec:
        canon = game_schema.spec_to_action(spec)
        if canon:
            return canon                        # resolves all real-spec goals
    if gid in actions_set:
        return gid                              # verbs + build_forester pass through
    return _ALIAS.get(gid, gid)


def _load(name: str) -> dict:
    with (_FIXTURES / name).open("r", encoding="utf-8") as fh:
        return json.load(fh)


class Oracle:
    """Wraps the planner/controller expert policy behind label(state)."""

    def __init__(self, map_data: Optional[dict] = None, resources: Optional[dict] = None):
        self.map_data = map_data if map_data is not None else _load("map_fresh.json")
        self.resources = resources if resources is not None else _load("resources_fresh.json")

    def label(self, state: dict) -> str:
        """Primary next-action goal_id for this state - PLACEMENT-INDEPENDENT.

        The learned policy decides WHAT; placement.py decides WHERE downstream and the
        runtime falls to the next ranked intent if the top one has nowhere to go. So the
        label must be a pure function of resources/buildings/needs, not of whether a tile
        happens to be free on the fixture map. We reuse the real controller selection
        (affordability, dependencies, site/worker caps) but hand every goal a unique,
        non-overlapping fake candidate so the placement gates always pass. This makes the
        label deterministic per feature-relevant state - no first-seen label conflicts.

        The selected goal is always translated to a game_schema.actions() id via
        _to_schema_id (see module docstring) before it is returned; a translation
        that still lands outside the action space raises rather than silently
        corrupting the training data.

        PERF: this calls planner.analyze() directly instead of planner.plan_report().
        plan_report is exactly analyze() plus, per goal, a real candidates_for() map
        scan to build candidates_by_goal/followups/alerts_local/decision_fork/text -
        none of which this method reads (candidates_by_goal is overwritten with
        fake_candidates below regardless, and the rest are never touched). Skipping
        that scan is a pure speedup: analyze() is a deterministic function of
        (state, map_data, buildings_detail), and both call sites pass the same
        arguments (buildings_detail defaulting to None), so the `goals` list is
        identical either way. Verified byte-identical against the plan_report-based
        implementation across the full targeted-recipe battery, the existing
        test_labeler_schema.py battery, and a large random-state fuzz sample before
        landing (see the Task 5a report).
        """
        goals = planner.analyze(state, self.map_data)
        fake_candidates = {
            goal["id"]: [{"x": i * 10, "y": 0, "z": 0}]
            for i, goal in enumerate(goals)
            if isinstance(goal, dict) and goal.get("id") and goal.get("spec")
        }
        stable_report = {"goals": goals, "candidates_by_goal": fake_candidates}
        selected = set(controller.build_safe_ready_frontier(stable_report, state).get("goal_ids") or [])

        actions_set = set(game_schema.actions())

        # Walk goals in planner priority order; first actionable one wins. A demolish
        # (no spec, so never in the frontier) is surfaced explicitly at its priority slot.
        result = "advance_time"
        for goal in goals:
            goal_id = str(goal.get("id", ""))
            if goal_id.startswith("demolish_unreachable") or goal_id in selected:
                result = _to_schema_id(goal, actions_set)
                break

        if result not in actions_set:
            raise ValueError(
                "Oracle.label produced %r, which is not a member of "
                "game_schema.actions() - a planner goal id/spec changed without a "
                "matching _to_schema_id/_ALIAS update in agent/nlp/labeler.py"
                % (result,)
            )
        return result


__all__ = ["Oracle"]
