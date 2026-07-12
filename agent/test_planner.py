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


def _reservoir_channel_map(width=10, height=6, channel_col=6, dc=(2, 3)):
    """A small synthetic /map: flat dry land with a vertical water channel.

    Terrain is flat (height 4 everywhere). Column ``channel_col`` is a
    water_depth=2 channel bisecting the map north-south; everything else is
    dry land. The district center sits on the dry (west) bank, so land-BFS
    reachability only spans the west side -- the east bank is intentionally
    unreachable, mirroring a real map where a river cuts the colony off from
    the far shore until a dam/levee/floodgate is built.
    """
    total = width * height
    terrain = [4] * total
    water = [0] * total
    for row in range(height):
        water[row * width + channel_col] = 2
    return {
        "width": width,
        "height": height,
        "origin": {"x": 0, "z": 0},
        "district_center": {"x": dc[0], "y": dc[1], "z": 4},
        "terrain_height": terrain,
        "water_depth": water,
        "contamination": [0] * total,
        "moist": [0] * total,
        "occupied": [0] * total,
        # Deliberately omitted: "on_road" / "reachable". Both helpers fall back
        # to the land-BFS reachable set when the bridge hasn't supplied them
        # (see planner._road_reachable_tiles), which is exactly what a partial
        # bridge snapshot looks like -- and keeps this fixture minimal.
    }


class ReservoirPlacementTests(unittest.TestCase):
    """Task: reservoir-engineering (Dam/Levee/Floodgate/...) must be placed ON
    water (water_depth > 0), never on generic flat-dry-land. See
    docs/kb/placement-verticality-gaps.md gap #2.
    """

    def setUp(self):
        self.map_data = _reservoir_channel_map()
        self.width = self.map_data["width"]

    def _water_depth_at(self, x, y):
        index = y * self.width + x
        return self.map_data["water_depth"][index]

    def test_reservoir_specs_get_on_water_candidates_by_bare_goal_id(self):
        # These arrive as bare "build_x" strings when emitted straight from
        # economy.RESERVOIR_SPECS / game_schema.spec_to_action, with no goal
        # dict wrapping -- candidates_for must resolve them without a "spec".
        for goal_id in (
            "build_dam",
            "build_levee",
            "build_floodgate",
            "build_double_floodgate",
            "build_sluice",
        ):
            with self.subTest(goal_id=goal_id):
                candidates = planner.candidates_for(goal_id, {}, self.map_data, k=6)
                self.assertTrue(candidates, "%s got no candidates" % goal_id)
                for candidate in candidates:
                    self.assertGreater(
                        self._water_depth_at(candidate["x"], candidate["y"]), 0,
                        "%s candidate %s is not on water" % (goal_id, candidate),
                    )

    def test_reservoir_spec_gets_on_water_candidates_by_goal_dict_with_spec(self):
        # The real planner path (planner._append_drought_goals -> _emit_spec_goal)
        # emits a goal dict carrying an explicit "spec", not just a bare id.
        goal = {"id": "build_dam", "spec": "Dam", "why": "test"}
        candidates = planner.candidates_for(goal, {}, self.map_data, k=6)

        self.assertTrue(candidates)
        for candidate in candidates:
            self.assertGreater(self._water_depth_at(candidate["x"], candidate["y"]), 0)

    def test_reservoir_candidates_are_nearest_reachable_shoreline_to_dc(self):
        candidates = planner.candidates_for("build_dam", {}, self.map_data, k=20)

        self.assertTrue(candidates)
        # Every water column cell (col=6) neighbors the reachable west bank
        # (col=5), so every row of the channel should qualify.
        self.assertEqual({c["x"] for c in candidates}, {6})
        self.assertEqual({c["y"] for c in candidates}, set(range(self.map_data["height"])))
        # Nearest to the DC (2,3) comes first: (6,3) is Manhattan-distance 4.
        self.assertEqual((candidates[0]["x"], candidates[0]["y"]), (6, 3))

    def test_reservoir_candidate_z_is_terrain_height_not_invented(self):
        candidates = planner.candidates_for("build_dam", {}, self.map_data, k=6)

        self.assertTrue(candidates)
        for candidate in candidates:
            self.assertEqual(candidate["z"], 4)

    def test_normal_building_still_gets_dry_land_on_the_same_map(self):
        # Non-regression: a generic buildable spec (not water-infrastructure,
        # not one of the specialized resource-aware profiles) must still land
        # on dry ground, on the SAME map that now serves reservoir specs water.
        # LumberMill isn't in the small bootstrap GOAL_SPECS table, so (exactly
        # like the real planner via plan_report) it is passed as a goal dict
        # carrying an explicit "spec" rather than a bare goal-id string.
        goal = {"id": "build_lumber_mill", "spec": "LumberMill"}
        candidates = planner.candidates_for(goal, {}, self.map_data, k=10)

        self.assertTrue(candidates)
        for candidate in candidates:
            self.assertEqual(
                self._water_depth_at(candidate["x"], candidate["y"]), 0,
                "build_lumber_mill candidate %s is on water" % candidate,
            )


if __name__ == "__main__":
    unittest.main()
