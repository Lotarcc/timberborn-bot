import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent import play


FIXTURES = AGENT_DIR / "fixtures"


def load_fixture(name):
    with (FIXTURES / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


class FakeBridge:
    instances = []
    state_data = None
    state_sequence = None
    map_data = None
    resources_data = None
    batch_transport_error = False

    def __init__(self, base_url):
        self.base_url = base_url
        self.act_calls = []
        self.state_index = 0
        FakeBridge.instances.append(self)

    def ping(self):
        return 200, {"ok": True}

    def state(self):
        if FakeBridge.state_sequence:
            index = min(self.state_index, len(FakeBridge.state_sequence) - 1)
            self.state_index += 1
            return 200, copy.deepcopy(FakeBridge.state_sequence[index])
        return 200, copy.deepcopy(FakeBridge.state_data)

    def map(self):
        return 200, copy.deepcopy(FakeBridge.map_data)

    def resources(self):
        if FakeBridge.resources_data is None:
            return 404, {"ok": False, "error": "not_found"}
        return 200, copy.deepcopy(FakeBridge.resources_data)

    def act(self, command, args):
        self.act_calls.append((command, copy.deepcopy(args)))
        if command == "batch":
            if FakeBridge.batch_transport_error:
                return 0, {"error": "transport_error", "detail": "boom"}
            results = [
                {"ok": True, "command": item["command"]}
                for item in args.get("actions", [])
            ]
            return 200, {
                "ok": True,
                "executed": len(results),
                "total": len(results),
                "results": results,
            }
        return 200, {"ok": True, "command": command}


class FakeOllama:
    instances = []
    response = {"plan": "noop", "actions": [{"action": "noop", "args": {}}]}

    def __init__(self, base_url, model):
        self.base_url = base_url
        self.model = model
        self.messages = []
        FakeOllama.instances.append(self)

    def chat(self, messages):
        self.messages.append(copy.deepcopy(messages))
        return {"content": json.dumps(FakeOllama.response)}


class PlayDryRunTests(unittest.TestCase):
    def setUp(self):
        FakeBridge.instances = []
        FakeOllama.instances = []
        FakeBridge.state_data = load_fixture("state_fresh.json")
        FakeBridge.state_sequence = None
        FakeBridge.map_data = load_fixture("map_fresh.json")
        FakeBridge.resources_data = load_fixture("resources_fresh.json")
        FakeBridge.batch_transport_error = False
        FakeOllama.response = {"plan": "noop", "actions": [{"action": "noop", "args": {}}]}

    def _run_steps(self, tmpdir, max_steps=1):
        playbook_path = Path(tmpdir) / "playbook.json"
        metrics_path = Path(tmpdir) / "metrics.csv"
        playbook_path.write_text(
            json.dumps(
                {
                    "lessons": [
                        {
                            "trigger": "early run",
                            "action": "place free lumberjack first",
                            "outcome": "logs start",
                            "last_seen_run": "run-002",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        cfg = {
            "BRIDGE_URL": "http://bridge.test",
            "OLLAMA_URL": "http://ollama.test",
            "MODEL": "fake",
            "MAX_STEPS": 1,
            "VISION_MODEL": "fake-v",
            "VISION_EVERY": 0,
        }
        with (
            mock.patch.object(play, "Bridge", FakeBridge),
            mock.patch.object(play, "Ollama", FakeOllama),
            mock.patch.object(play, "PLAYBOOK_PATH", str(playbook_path)),
            mock.patch.object(play, "METRICS_CSV_PATH", str(metrics_path)),
            mock.patch.object(play, "__file__", str(Path(tmpdir) / "play.py")),
            mock.patch.object(play, "kb_lookup", lambda query, k=3: []),
        ):
            play.run(cfg, "dry", max_steps)
        return FakeBridge.instances[0], FakeOllama.instances[0], playbook_path

    def _run_once(self, tmpdir):
        bridge, ollama, _playbook_path = self._run_steps(tmpdir, max_steps=1)
        return bridge, ollama

    def test_batch_payload_enforces_advance_time_and_injects_playbook(self):
        state = copy.deepcopy(load_fixture("state_fresh.json"))
        state["buildings"]["under_construction"] = 1
        FakeBridge.state_data = state
        FakeOllama.response = {
            "plan": "start free log production, then let builders work",
            "actions": [
                {
                    "action": "place_building",
                    "args": {"spec": "LumberjackFlag", "x": 12, "y": 18, "z": 4},
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            bridge, ollama = self._run_once(tmpdir)

        self.assertEqual(bridge.act_calls[0][0], "batch")
        batch_args = bridge.act_calls[0][1]
        self.assertEqual(
            [item["command"] for item in batch_args["actions"]],
            ["place_building", "designate_cutting", "set_speed"],
        )
        self.assertEqual(batch_args["actions"][1]["args"], {"all": True})
        self.assertEqual(batch_args["actions"][-1]["args"], {"speed": 3})

        system_prompt = ollama.messages[0][0]["content"]
        self.assertIn("PLAYBOOK lessons from prior runs", system_prompt)
        self.assertIn("place free lumberjack first", system_prompt)

    def test_executor_falls_back_to_single_actions_on_batch_transport_error(self):
        FakeBridge.batch_transport_error = True
        FakeOllama.response = {
            "plan": "advance existing construction",
            "actions": [{"action": "set_speed", "args": {"speed": 2}}],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            bridge, _ollama = self._run_once(tmpdir)

        self.assertEqual(
            [command for command, _args in bridge.act_calls],
            ["batch", "set_speed"],
        )
        self.assertEqual(bridge.act_calls[1][1], {"speed": 2})

    def test_observed_effects_are_added_to_history_and_discovery_distills(self):
        before = copy.deepcopy(load_fixture("state_fresh.json"))
        after = copy.deepcopy(before)
        for resource in after["resources"]:
            if resource["good"] == "Log":
                resource["stored"] = 6
                resource["all_stock"] = 6
        FakeBridge.state_sequence = [before, after]
        FakeOllama.response = {
            "plan": "start wood production",
            "actions": [
                {
                    "action": "place_building",
                    "args": {"spec": "LumberjackFlag", "x": 12, "y": 18, "z": 4},
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            _bridge, ollama, playbook_path = self._run_steps(tmpdir, max_steps=2)

            second_prompt = json.dumps(ollama.messages[1])
            self.assertIn("OBSERVED last action: Log +6 after", second_prompt)

            playbook = json.loads(playbook_path.read_text(encoding="utf-8"))
            actions = [lesson.get("action", "") for lesson in playbook.get("lessons", [])]
            self.assertTrue(
                any("designate_cutting -> raises Log" in action for action in actions),
                actions,
            )


if __name__ == "__main__":
    unittest.main()
