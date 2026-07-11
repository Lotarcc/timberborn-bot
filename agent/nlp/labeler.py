"""Expert oracle: given a game state, return the goal_id the deterministic
planner+controller would pick. This is the behavioral-cloning label source - the
learned heads are trained to imitate (and later, via LLM/outcome relabeling,
improve on) this policy.

The planner needs a map and a /resources payload to test placement feasibility.
For synthetic training states we hold those fixed to the fresh-map fixtures: the
WHAT decision is driven by resources/buildings/needs in `state`, which is exactly
what we vary. WHERE (which the map affects) is not what we are training here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from agent import controller, planner

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


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
        """
        report = planner.plan_report(state, self.map_data, resources=self.resources)
        goals = report.get("goals") or []
        fake_candidates = {
            goal["id"]: [{"x": i * 10, "y": 0, "z": 0}]
            for i, goal in enumerate(goals)
            if isinstance(goal, dict) and goal.get("id") and goal.get("spec")
        }
        stable_report = dict(report)
        stable_report["candidates_by_goal"] = fake_candidates
        selected = set(controller.build_safe_ready_frontier(stable_report, state).get("goal_ids") or [])

        # Walk goals in planner priority order; first actionable one wins. A demolish
        # (no spec, so never in the frontier) is surfaced explicitly at its priority slot.
        for goal in goals:
            goal_id = str(goal.get("id", ""))
            if goal_id.startswith("demolish_unreachable"):
                return "demolish_unreachable"
            if goal_id in selected:
                return goal_id
        return "advance_time"


__all__ = ["Oracle"]
