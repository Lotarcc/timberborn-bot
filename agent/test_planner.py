import json
import unittest
from pathlib import Path

from agent import planner


FIXTURES = Path(__file__).with_name("fixtures")


def load_fixture(name):
    with (FIXTURES / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.state = load_fixture("state_fresh.json")
        self.map_data = load_fixture("map_fresh.json")

    def test_fresh_colony_goal_order_starts_with_lumberjack(self):
        goals = planner.analyze(self.state, self.map_data)

        self.assertGreaterEqual(len(goals), 4)
        self.assertEqual(
            [goal["id"] for goal in goals[:4]],
            [
                "build_lumberjack",
                "build_water_pump",
                "build_water_storage",
                "build_gatherer",
            ],
        )

    def test_blocked_by_logs_recommends_advancing_time(self):
        report = planner.plan_report(self.state, self.map_data)
        pump_goal = next(goal for goal in report["goals"] if goal["id"] == "build_water_pump")

        self.assertEqual(pump_goal["cost_logs"], 12)
        self.assertIn("need 12 logs, have 0", pump_goal["blocked_by"])
        self.assertTrue(report["advance_time_recommended"])
        self.assertIn("ADVANCE TIME (set_speed 3, then re-check)", report["text"])

    def test_pump_candidates_exclude_unreachable_far_side_lake(self):
        candidates = planner.candidates_for("build_water_pump", self.state, self.map_data, k=20)
        coords = {(candidate["x"], candidate["y"]) for candidate in candidates}

        self.assertIn((13, 17), coords)
        self.assertNotIn((25, 8), coords)
        self.assertNotIn((25, 12), coords)

    def test_report_text_is_prompt_sized(self):
        report = planner.plan_report(self.state, self.map_data)

        self.assertLessEqual(len(report["text"].splitlines()), 25)


if __name__ == "__main__":
    unittest.main()
