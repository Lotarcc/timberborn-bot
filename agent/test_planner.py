import json
import unittest
from pathlib import Path

from agent import planner


FIXTURES = Path(__file__).with_name("fixtures")


def load_fixture(name):
    with (FIXTURES / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def map_with_reserved_townhall_buffer(map_data, state):
    result = json.loads(json.dumps(map_data))
    dc = state["district_center"]
    origin = result["origin"]
    origin_y = origin.get("z", origin.get("y", 0))
    for y in range(
        dc["y"] - planner.TOWNHALL_BUFFER,
        dc["y"] + planner.TOWNHALL_BUFFER + 1,
    ):
        for x in range(
            dc["x"] - planner.TOWNHALL_BUFFER,
            dc["x"] + planner.TOWNHALL_BUFFER + 1,
        ):
            index = (y - origin_y) * result["width"] + (x - origin["x"])
            result["occupied"][index] = 1
    return result


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
        self.assertFalse(pump_goal["free"])
        self.assertFalse(pump_goal["affordable"])
        self.assertFalse(pump_goal["satisfied"])
        self.assertIn("need 12 logs, have 0", pump_goal["blocked_by"])
        self.assertTrue(report["advance_time_recommended"])
        self.assertIn("ADVANCE TIME (set_speed 3, then re-check)", report["text"])

    def test_pump_candidates_exclude_unreachable_far_side_lake(self):
        candidates = planner.candidates_for("build_water_pump", self.state, self.map_data, k=20)
        coords = {(candidate["x"], candidate["y"]) for candidate in candidates}

        # The real property: valid candidates exist, and NONE are on the far side of
        # the unreachable lake (utility placement scores clean reachable water edges).
        self.assertTrue(candidates)
        self.assertNotIn((25, 8), coords)
        self.assertNotIn((25, 12), coords)

    def test_report_text_is_prompt_sized(self):
        report = planner.plan_report(self.state, self.map_data)

        self.assertLessEqual(len(report["text"].splitlines()), 25)

    def test_lumberjack_placed_on_reachable_land_with_followup_cutting(self):
        # Spatial placement puts the lumberjack on CLEAR REACHABLE land scored toward
        # the mature-tree cluster (cutting is global, so it need not be in the forest),
        # off the town-hall buffer, with the designate_cutting followup.
        map_data = map_with_reserved_townhall_buffer(self.map_data, self.state)
        report = planner.plan_report(self.state, map_data, resources=self.resources)
        candidates = report["candidates_by_goal"]["build_lumberjack"]
        self.assertTrue(candidates)
        first = candidates[0]
        dc = self.state["district_center"]

        # off the town-hall approaches, and on reachable land
        self.assertGreater(
            max(abs(first["x"] - dc["x"]), abs(first["y"] - dc["y"])),
            planner.TOWNHALL_BUFFER,
        )
        self.assertIn(
            (first["x"], first["y"]),
            planner.reachable_tiles(map_data, (dc["x"], dc["y"])),
        )
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
                "EfficientFarmHouse": 1,
                "SmallWarehouse": 1,
                "Inventor": 1,
            }
        )
        state["population"]["homeless"] = 0
        for resource in state["resources"]:
            if resource["good"] in ("Water", "Food", "Berries"):
                resource["days_remaining"] = 99

        map_data = map_with_reserved_townhall_buffer(self.map_data, state)
        report = planner.plan_report(state, map_data, resources=self.resources)

        self.assertIn("build_forester", [goal["id"] for goal in report["goals"]])
        self.assertIn("build_forester", report["followups"])
        followup = report["followups"]["build_forester"][0]
        self.assertEqual(followup["action"], "designate_planting")
        self.assertEqual(followup["args"]["species"], "Pine")
        self.assertGreater(len(followup["args"]["tiles"]), 0)

    def test_log_contention_is_exposed_as_enumerated_decision_fork(self):
        state = json.loads(json.dumps(self.state))
        for resource in state["resources"]:
            if resource["good"] == "Log":
                resource["stored"] = 12
                resource["all_stock"] = 12

        report = planner.plan_report(
            state, self.map_data, resources=self.resources
        )

        fork = report["decision_fork"]
        self.assertEqual(fork["type"], "resource_contention")
        self.assertEqual(fork["resource"], "Log")
        self.assertEqual(fork["available"], 12)
        self.assertIn("build_water_pump", fork["goal_ids"])
        self.assertIn("build_lodge", fork["goal_ids"])
        self.assertTrue(fork["options"])

    def test_water_storage_goal_is_satisfied_by_target_tank_count(self):
        state = json.loads(json.dumps(self.state))
        state["buildings"]["counts"]["SmallTank.Folktails"] = 5

        goals = planner.analyze(state, self.map_data)

        self.assertNotIn("build_water_storage", [goal["id"] for goal in goals])

    def test_multiple_unreachable_buildings_have_distinct_goal_ids(self):
        buildings = [
            {"spec": "Lodge", "x": 1, "y": 2, "z": 3, "reachable": False},
            {"spec": "WaterPump", "x": 4, "y": 5, "z": 6, "reachable": False},
        ]

        report = planner.plan_report(
            self.state, self.map_data, buildings_detail=buildings
        )
        ids = [
            goal["id"]
            for goal in report["goals"]
            if goal["id"].startswith("demolish_unreachable")
        ]

        self.assertEqual(ids, ["demolish_unreachable", "demolish_unreachable_2"])
        self.assertEqual(report["candidates_by_goal"][ids[0]], [])
        self.assertEqual(report["candidates_by_goal"][ids[1]], [])


if __name__ == "__main__":
    unittest.main()
