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
        self.resources = load_fixture("resources_fresh.json")

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

    def test_lumberjack_placed_near_dc_with_followup_cutting(self):
        # Cutting is global, so the flag goes on clear reachable land NEAREST the
        # district center (not in the forest), and the trees are handled by the
        # designate_cutting followup.
        report = planner.plan_report(self.state, self.map_data, resources=self.resources)
        candidates = report["candidates_by_goal"]["build_lumberjack"]
        first = candidates[0]
        dc = self.state["district_center"]

        # nearest candidate should be close to the DC, not out at the tree cluster
        self.assertLessEqual(abs(first["x"] - dc["x"]) + abs(first["y"] - dc["y"]), 4)
        self.assertIn("cuts", first["why"])
        self.assertEqual(
            report["followups"]["build_lumberjack"],
            [{"action": "designate_cutting", "args": {"all": True}}],
        )
        self.assertIn("followup=designate_cutting", report["text"])

    def test_forester_goal_has_planting_followup_when_unlocked(self):
        state = json.loads(json.dumps(self.state))
        state["buildings"]["counts"].update(
            {
                "LumberjackFlag": 1,
                "WaterPump": 1,
                "SmallTank": 4,
                "GathererFlag": 1,
                "EfficientFarmhouse": 1,
                "SmallWarehouse": 1,
                "Inventor": 1,
            }
        )
        state["population"]["homeless"] = 0
        for resource in state["resources"]:
            if resource["good"] in ("Water", "Food", "Berries"):
                resource["days_remaining"] = 99

        report = planner.plan_report(state, self.map_data, resources=self.resources)

        self.assertIn("build_forester", [goal["id"] for goal in report["goals"]])
        self.assertIn("build_forester", report["followups"])
        followup = report["followups"]["build_forester"][0]
        self.assertEqual(followup["action"], "designate_planting")
        self.assertEqual(followup["args"]["species"], "Pine")
        self.assertGreater(len(followup["args"]["tiles"]), 0)


if __name__ == "__main__":
    unittest.main()
