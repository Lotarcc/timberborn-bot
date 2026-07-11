import copy
import json
import unittest
from pathlib import Path

from agent import controller, planner, play


FIXTURES = Path(__file__).with_name("fixtures")


def load_fixture(name):
    with (FIXTURES / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def set_stock(state, good, amount):
    for resource in state.get("resources", []):
        if str(resource.get("good", "")).lower() == good.lower():
            resource["stored"] = amount
            resource["all_stock"] = amount
            return
    state.setdefault("resources", []).append(
        {"good": good, "stored": amount, "all_stock": amount}
    )


def map_with_reserved_townhall_buffer(map_data, state):
    result = copy.deepcopy(map_data)
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


class ScriptedBridge:
    def __init__(self, states, batch_body=None):
        self.states = [copy.deepcopy(state) for state in states]
        self.batch_body = copy.deepcopy(batch_body)
        self.state_index = 0
        self.act_calls = []

    def state(self):
        index = min(self.state_index, len(self.states) - 1)
        self.state_index += 1
        return 200, copy.deepcopy(self.states[index])

    def act(self, command, args):
        self.act_calls.append((command, copy.deepcopy(args)))
        if command == "batch" and self.batch_body is not None:
            return 200, copy.deepcopy(self.batch_body)
        return 200, {
            "ok": True,
            "applied": {"command": command, **copy.deepcopy(args)},
        }


class FakeArbiterOllama:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def chat(self, messages, schema=None):
        self.calls.append((copy.deepcopy(messages), copy.deepcopy(schema)))
        return {"content": json.dumps(self.response)}


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.state = load_fixture("state_fresh.json")
        self.map_data = load_fixture("map_fresh.json")
        self.resources = load_fixture("resources_fresh.json")

    def test_frontier_batches_free_lumberjack_and_other_free_goal(self):
        map_data = map_with_reserved_townhall_buffer(self.map_data, self.state)
        report = planner.plan_report(
            self.state, map_data, resources=self.resources
        )

        frontier = controller.build_safe_ready_frontier(report, self.state)

        self.assertEqual(
            frontier["goal_ids"][:2],
            ["build_lumberjack", "build_gatherer"],
        )
        self.assertEqual(
            [action["action"] for action in frontier["actions"][:3]],
            ["place_building", "designate_cutting", "place_building"],
        )
        self.assertEqual(
            [
                action["args"].get("spec")
                for action in frontier["actions"]
                if action["action"] == "place_building"
            ][:2],
            ["LumberjackFlag", "GathererFlag"],
        )

    def test_frontier_reserves_cumulative_log_budget(self):
        state = {"resources": [{"good": "Log", "stored": 12, "all_stock": 24}]}
        report = {
            "goals": [
                {
                    "id": "build_water_pump",
                    "spec": "WaterPump",
                    "cost_logs": 12,
                    "free": False,
                    "affordable": True,
                    "satisfied": False,
                },
                {
                    "id": "build_lodge",
                    "spec": "Lodge",
                    "cost_logs": 12,
                    "free": False,
                    "affordable": True,
                    "satisfied": False,
                },
            ],
            "candidates_by_goal": {
                "build_water_pump": [
                    {"x": 1, "y": 1, "z": 4, "orientation": "North"}
                ],
                "build_lodge": [{"x": 2, "y": 2, "z": 4}],
            },
            "followups": {},
        }

        frontier = controller.build_safe_ready_frontier(report, state)

        self.assertEqual(frontier["goal_ids"], ["build_water_pump"])
        self.assertEqual(frontier["reserved"]["Log"], 12)

    def test_frontier_does_not_oversubscribe_workplaces(self):
        state = copy.deepcopy(self.state)
        state["population"]["unemployed"] = 1
        map_data = map_with_reserved_townhall_buffer(self.map_data, state)
        report = planner.plan_report(state, map_data, resources=self.resources)

        frontier = controller.build_safe_ready_frontier(report, state)

        workplace_goals = [
            goal_id for goal_id in frontier["goal_ids"]
            if goal_id in controller.WORKPLACE_GOALS
        ]
        self.assertEqual(workplace_goals, ["build_lumberjack"])

    def test_needs_llm_only_for_real_contention(self):
        fresh_report = planner.plan_report(
            self.state, self.map_data, resources=self.resources
        )
        self.assertFalse(controller.needs_llm(fresh_report, self.state))

        contended_state = copy.deepcopy(self.state)
        set_stock(contended_state, "Log", 12)
        contended_report = planner.plan_report(
            contended_state, self.map_data, resources=self.resources
        )
        self.assertIsNotNone(contended_report["decision_fork"])
        self.assertTrue(controller.needs_llm(contended_report, contended_state))

        unreachable_report = {
            "goals": [{"id": "demolish_unreachable", "coords": {"x": 1, "y": 2, "z": 3}}]
        }
        self.assertTrue(controller.needs_llm(unreachable_report, self.state))

    def test_bulk_advance_pauses_on_resource_threshold(self):
        initial = copy.deepcopy(self.state)
        initial["alerts"] = []
        initial["weather"]["next"]["in_days"] = 2.0
        middle = copy.deepcopy(initial)
        set_stock(middle, "Log", 6)
        middle["weather"]["next"]["in_days"] = 1.5
        wake = copy.deepcopy(middle)
        set_stock(wake, "Log", 12)
        wake["weather"]["next"]["in_days"] = 1.0
        bridge = ScriptedBridge([middle, wake])

        result = controller.bulk_advance_until_wake(
            bridge,
            initial,
            thresholds={"Log": 12},
            poll_interval=0,
            max_polls=4,
            hazard_margin_days=0.25,
        )

        self.assertEqual(result["reason"], "resource_threshold:Log")
        self.assertEqual(result["state"], wake)
        self.assertEqual(
            bridge.act_calls,
            [("set_speed", {"speed": 3}), ("set_speed", {"speed": 0})],
        )

    def test_bulk_advance_does_not_start_near_hazard_boundary(self):
        initial = copy.deepcopy(self.state)
        initial["alerts"] = []
        initial["weather"]["next"]["in_days"] = 0.3
        bridge = ScriptedBridge([initial])

        result = controller.bulk_advance_until_wake(
            bridge,
            initial,
            poll_interval=0,
            max_advance_days=1.0,
            hazard_margin_days=0.25,
        )

        self.assertEqual(result["reason"], "hazard_imminent")
        self.assertTrue(result["paused"])
        self.assertEqual(bridge.act_calls, [("set_speed", {"speed": 0})])

    def test_no_land_route_is_returned_as_pending_fork(self):
        actions = [
            {
                "action": "place_building",
                "args": {"spec": "LumberjackFlag", "x": 13, "y": 18, "z": 4},
                "goal_id": "build_lumberjack",
            }
        ]
        batch_body = {
            "ok": True,
            "executed": 1,
            "total": 1,
            "results": [
                {
                    "ok": True,
                    "command": "place_building",
                    "applied": {
                        "command": "place_building",
                        "spec": "LumberjackFlag",
                        "x": 14,
                        "y": 18,
                        "z": 4,
                        "requested": {"x": 13, "y": 18, "z": 4},
                        "auto_connect": {
                            "connected": False,
                            "reason": "no_land_route",
                            "paths_laid": 0,
                            "path_tiles": [],
                            "access_tiles": [],
                        },
                    },
                }
            ],
        }
        bridge = ScriptedBridge([self.state], batch_body=batch_body)

        execution = controller.execute_frontier(bridge, actions, after_state=self.state)

        self.assertEqual(len(execution["forks"]), 1)
        self.assertEqual(execution["forks"][0]["type"], "no_land_route")
        self.assertEqual(execution["forks"][0]["goal_id"], "build_lumberjack")
        self.assertEqual(
            execution["forks"][0]["actual"], {"x": 14, "y": 18, "z": 4}
        )

    def test_arbiter_chooses_only_an_enumerated_goal_subset(self):
        report = {
            "goals": [
                {"id": "build_water_pump", "why": "produce water"},
                {"id": "build_lodge", "why": "house beavers"},
            ],
            "decision_fork": {
                "type": "resource_contention",
                "resource": "Log",
                "available": 12,
                "options": [
                    {"id": "logs-1", "goal_ids": ["build_water_pump"]},
                    {"id": "logs-2", "goal_ids": ["build_lodge"]},
                ],
            },
        }
        ollama = FakeArbiterOllama(
            {
                "option_id": "logs-1",
                "goal_ids": ["build_water_pump"],
                "why": "water is the immediate survival gate",
            }
        )

        choice = play.arbitrate_planner_fork(ollama, report, self.state)

        self.assertEqual(choice["goal_ids"], ["build_water_pump"])
        schema_text = json.dumps(ollama.calls[0][1])
        self.assertNotIn('"x"', schema_text)
        self.assertNotIn('"coordinates"', schema_text)


if __name__ == "__main__":
    unittest.main()
