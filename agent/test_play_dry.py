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
    map_data = None
    batch_transport_error = False

    def __init__(self, base_url):
        self.base_url = base_url
        self.act_calls = []
        FakeBridge.instances.append(self)

    def ping(self):
        return 200, {"ok": True}

    def state(self):
        return 200, copy.deepcopy(FakeBridge.state_data)

    def map(self):
        return 200, copy.deepcopy(FakeBridge.map_data)

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
        FakeBridge.map_data = load_fixture("map_fresh.json")
        FakeBridge.batch_transport_error = False
        FakeOllama.response = {"plan": "noop", "actions": [{"action": "noop", "args": {}}]}

    def _run_once(self, tmpdir):
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
            play.run(cfg, "dry", 1)
        return FakeBridge.instances[0], FakeOllama.instances[0]

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
            ["place_building", "set_speed"],
        )
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


if __name__ == "__main__":
    unittest.main()
