#!/usr/bin/env python3
"""
vision_player.py — a Timberborn agent that SEES the game and plays it.

Every turn it captures a screenshot of the colony, reads the digested state
(resources, weather forecast, population, alerts) and the deterministic planner's
suggestions (which goals are needed + exact valid candidate tiles), and asks a
vision LLM (Ollama VLM, e.g. qwen2.5vl:7b) to choose the single most urgent action.

Division of labor (what actually works):
  - The VLM SEES and DECIDES *what* to do — it can spot the river, the forest, an
    "Unconnected building" warning, low water — the things a coordinate feed misses.
  - The deterministic planner/placement decides *where* (a valid, reachable tile) and
    executes via the bridge (auto-connect + auto-orient). The VLM never emits raw
    coordinates (LLMs are bad at that); it picks an action + a building type.

Loop: screenshot + state + planner options -> VLM decision -> execute -> advance
time -> repeat, until the run ends. Everything degrades gracefully.
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
if AGENT_DIR not in sys.path:
    sys.path.insert(0, AGENT_DIR)

import play  # Bridge, journal, helpers
import planner

try:
    from kb import lookup as kb_lookup
except Exception:
    kb_lookup = None


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://127.0.0.1:7744")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
VISION_MODEL = os.environ.get("VISION_MODEL", "qwen2.5vl:7b")
SHOT_WIDTH = 1100

# The action menu the VLM chooses from. WHAT, not WHERE.
ACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "observation": {"type": "string"},   # what it sees on screen
        "priority": {"type": "string"},       # the most urgent need right now
        "action": {
            "type": "string",
            "enum": [
                "build",                # build a building (placement picks the tile)
                "designate_cutting",    # mark mature trees so lumberjacks produce logs
                "demolish_unconnected", # remove a building the game flags as unreachable
                "advance_time",         # let beavers work / time pass
            ],
        },
        "building": {"type": "string"},       # spec when action == build
        "reason": {"type": "string"},
    },
    "required": ["observation", "priority", "action", "reason"],
}

# Buildings the VLM may ask for (the placement layer knows where each goes).
BUILDABLE = [
    "LumberjackFlag", "WaterPump", "SmallTank", "GathererFlag", "Lodge",
    "EfficientFarmHouse", "SmallWarehouse", "Forester", "Inventor",
]

SYSTEM_PROMPT_FALLBACK = """\
You are an expert Timberborn (Folktails) player. You SEE the colony in a screenshot
and read its digested state. Each turn choose the SINGLE most urgent action.

Survival priority: WATER > FOOD > SHELTER > expansion. Beavers die of thirst first.

What to look for on screen: the round District Center (town hall); the blue river
(water); green forest (trees, top/side); berry bushes (small green dots); red/cracked
ground (contaminated badwater — avoid); grey lines (paths); a red "Unconnected
building" warning (a building not on a path — it does NOTHING until fixed).

Rules you must follow:
- To get LOGS: build a LumberjackFlag, then designate_cutting (trees must be marked!).
- To get WATER: build a WaterPump on the clean river edge, then SmallTanks to store
  water before a drought. River water alone dries up in a drought.
- To get FOOD: build a GathererFlag covering berry bushes (must be connected).
- Housing: Lodges near the DC for homeless beavers.
- If the state shows an "Unconnected building" / building_unreachable alert, FIX IT:
  choose demolish_unconnected before anything else.
- If nothing is affordable yet (not enough logs) or you just placed something, choose
  advance_time to let beavers build and produce.
- You do NOT choose coordinates — just the action and building type; the game places
  it on a valid reachable tile automatically.

Output ONE JSON object: {observation, priority, action, building?, reason}.
"""


def _load_system_prompt():
    guide = os.path.join(os.path.dirname(AGENT_DIR), "docs", "kb", "play-guide-vision.md")
    try:
        with open(guide, "r", encoding="utf-8") as f:
            text = f.read().strip()
        if text:
            return text + "\n\nOutput ONE JSON object: {observation, priority, action, building?, reason}."
    except OSError:
        pass
    return SYSTEM_PROMPT_FALLBACK


# --------------------------------------------------------------------------- #
# Bridge helpers
# --------------------------------------------------------------------------- #
def screenshot(bridge_url, width=SHOT_WIDTH, timeout=15):
    url = "%s/screenshot?w=%d" % (bridge_url.rstrip("/"), int(width))
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        return data if data[:4] == b"\x89PNG" else None
    except Exception:
        return None


def vlm_decide(ollama_url, model, system_prompt, png_bytes, context_text, timeout=180):
    """Ask the VLM for a structured decision. Returns dict or None."""
    b64 = base64.b64encode(png_bytes).decode("ascii") if png_bytes else None
    user = {"role": "user", "content": context_text}
    if b64:
        user["images"] = [b64]
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt}, user],
        "format": ACTION_SCHEMA,
        "stream": False,
        "options": {"temperature": 0.2, "num_ctx": 8192},
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        ollama_url.rstrip("/") + "/api/chat", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8"))
        content = (resp.get("message") or {}).get("content")
        if isinstance(content, str):
            return json.loads(content)
    except Exception as e:
        play.log_stderr("VLM call failed: %s" % e)
    return None


# --------------------------------------------------------------------------- #
# Context: what the VLM reads alongside the screenshot
# --------------------------------------------------------------------------- #
def build_context(state, resources, report):
    lines = ["WHAT THE STATE SAYS (cross-check against the screenshot):"]
    lines.append(play.compact_state(state))
    lines.append("")
    lines.append(play.compact_resources_summary(resources, state))
    lines.append("")
    alerts = state.get("alerts") or []
    if alerts:
        lines.append("ALERTS (fix critical ones first):")
        for a in alerts:
            if isinstance(a, dict):
                lines.append("  [%s] %s -> %s" % (a.get("severity"), a.get("message"), a.get("suggestion")))
    # planner: what's needed + that a valid tile EXISTS for each (the game will place it)
    goals = report.get("goals") or []
    if goals:
        lines.append("")
        lines.append("NEEDED NEXT (a valid tile exists unless noted):")
        cbg = report.get("candidates_by_goal") or {}
        for g in goals[:8]:
            gid = g.get("id")
            spec = g.get("spec")
            has = bool(cbg.get(gid))
            blocked = g.get("blocked_by")
            note = ("" if has else " [no valid tile yet]") + ((" [%s]" % blocked) if blocked else "")
            lines.append("  - %s (%s)%s" % (gid, spec, note))
    lines.append("")
    lines.append("Choose the single most urgent action now. If a building is "
                 "unreachable/unconnected, demolish_unconnected. If you can't afford "
                 "anything or just built something, advance_time.")
    return "\n".join(lines)


def _unreachable_building(state):
    """Return coords of a finished unreachable building to demolish, or None."""
    for b in ((state.get("buildings") or {}).get("list") or []):
        if isinstance(b, dict) and b.get("reachable") is False and b.get("status") == "finished":
            spec = str(b.get("spec") or "")
            if "DistrictCenter" in spec:
                continue
            return b
    return None


# --------------------------------------------------------------------------- #
# Execute the VLM's chosen action deterministically
# --------------------------------------------------------------------------- #
def execute(bridge, decision, state, map_data, resources, report):
    action = (decision or {}).get("action")
    if action == "designate_cutting":
        return "designate_cutting", bridge.act("designate_cutting", {"all": True})

    if action == "demolish_unconnected":
        b = _unreachable_building(state)
        if b:
            return "demolish", bridge.act("demolish", {"x": b["x"], "y": b["y"], "z": b.get("z", 0)})
        return "noop", {"ok": True, "note": "no unreachable building"}

    if action == "build":
        spec = (decision.get("building") or "").strip()
        # normalize a few common names
        spec = {"Farmhouse": "EfficientFarmHouse", "Forester": "Forester",
                "Tank": "SmallTank", "Warehouse": "SmallWarehouse"}.get(spec, spec)
        if spec not in BUILDABLE:
            # fall back to the planner's top affordable goal spec
            for g in report.get("goals") or []:
                if g.get("spec") in BUILDABLE and (report.get("candidates_by_goal") or {}).get(g.get("id")):
                    spec = g["spec"]
                    break
        dc = planner._district_center(state, map_data, planner._map_arrays(map_data))
        cands = planner.candidates_for({"id": "build", "spec": spec}, state, map_data, k=1, resources=resources)
        if not cands:
            return "advance_time", bridge.act("set_speed", {"speed": 5})  # nowhere to build -> advance
        t = cands[0]
        batch = [{"command": "place_building", "args": {"spec": spec, "x": t["x"], "y": t["y"], "z": t["z"]}}]
        if spec == "LumberjackFlag":
            batch.append({"command": "designate_cutting", "args": {"all": True}})
        return "build:%s@(%s,%s)" % (spec, t["x"], t["y"]), bridge.act("batch", {"actions": batch, "stop_on_error": False})

    # advance_time (default)
    return "advance_time", bridge.act("set_speed", {"speed": 6})


def advance(bridge, seconds=6.0):
    """Let the game run a bit, then pause for a stable read."""
    bridge.act("set_speed", {"speed": 6})
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(2)
    bridge.act("set_speed", {"speed": 0})


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
def run(bridge_url, ollama_url, model, max_turns, run_id):
    bridge = play.Bridge(bridge_url)
    system_prompt = _load_system_prompt()
    here = os.path.dirname(os.path.abspath(__file__))
    jdir = os.path.join(here, "journal")
    os.makedirs(jdir, exist_ok=True)
    jpath = os.path.join(jdir, "%s.jsonl" % run_id)
    play.log_stderr("vision journal: %s" % jpath)

    status, ping = bridge.ping()
    play.log_stderr("bridge %s" % ("OK" if status == 200 else "DOWN status=%s" % status))

    for turn in range(1, max_turns + 1):
        try:
            _, state = bridge.state()
            _, map_data = bridge.map()
            rstat, resources = bridge.resources()
            if not isinstance(resources, dict):
                resources = {}
            buildings_detail = ((state.get("buildings") or {}).get("list")) if isinstance(state, dict) else None
            report = planner.plan_report(state, map_data, buildings_detail, resources=resources)

            png = screenshot(bridge_url)
            context = build_context(state, resources, report)
            decision = vlm_decide(ollama_url, model, system_prompt, png, context) or {}

            label, result = execute(bridge, decision, state, map_data, resources, report)
            ok = isinstance(result, dict) and result.get("ok") is not False

            res = {r.get("good"): r.get("stored") for r in (state.get("resources") or []) if isinstance(r, dict)}
            t = state.get("time", {})
            print("turn %02d/%d | day%s h%.0f | Log=%s Water=%s Berries=%s pop=%s homeless=%s | %s -> %s | obs: %s"
                  % (turn, max_turns, t.get("day"), t.get("hour") or 0, res.get("Log"), res.get("Water"),
                     res.get("Berries"), (state.get("population") or {}).get("total"),
                     (state.get("population") or {}).get("homeless"),
                     decision.get("action"), "ok" if ok else "ERR", str(decision.get("observation"))[:70]),
                  flush=True)

            play.journal_append(jpath, {
                "run_id": run_id, "turn": turn, "event": "turn",
                "state": play.state_summary_for_journal(state),
                "alerts": [a.get("id") if isinstance(a, dict) else a for a in (state.get("alerts") or [])],
                "decision": decision, "executed": label,
                "result_ok": ok,
                "has_screenshot": png is not None,
            })

            advance(bridge, seconds=6.0)
        except KeyboardInterrupt:
            play.log_stderr("interrupted")
            break
        except Exception as e:
            play.log_stderr("turn %d error: %s" % (turn, e))
            time.sleep(1)

    play.journal_append(jpath, {"run_id": run_id, "event": "run_end"})
    play.log_stderr("vision run complete: %s" % jpath)


def main(argv=None):
    p = argparse.ArgumentParser(description="Vision-driven Timberborn player.")
    p.add_argument("--bridge-url", default=BRIDGE_URL)
    p.add_argument("--ollama-url", default=OLLAMA_URL)
    p.add_argument("--model", default=VISION_MODEL)
    p.add_argument("--max-turns", type=int, default=30)
    p.add_argument("--run-id", default=os.environ.get("RUN_ID", "vision"))
    a = p.parse_args(argv)
    run(a.bridge_url, a.ollama_url, a.model, a.max_turns, a.run_id)


if __name__ == "__main__":
    main()
