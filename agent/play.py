#!/usr/bin/env python3
"""
play.py — first-version agent loop that plays Timberborn through the TimberBridge
HTTP API using a local LLM (Ollama running mistral-nemo:12b via function calling).

Architecture (see docs/agent/agent-design.md, docs/api-contract.md):
  - TimberBridge exposes the live game over localhost HTTP: GET /ping, GET /state
    (a *digested* colony snapshot — the bridge does the survival math), POST /act
    (one validated command; returns teaching errors on failure).
  - The LLM is the cheap "operator": every step it reads the digested state, picks
    ONE tool call, we forward it to the bridge, feed the result back, and advance.
  - We keep the game paused between decisions (set_speed 0) so every read is a
    consistent snapshot and there is no wall-clock pressure on the model.

Design bias for this first version: robustness over cleverness. Everything degrades
gracefully — missing bridge, missing Ollama, malformed tool calls, unimplemented
commands — and every step is logged to a JSONL run-journal for later retrospective.

Runtime deps: Python 3.8+ stdlib. Uses `requests` if importable, else falls back to
urllib. No third-party requirement.
"""

import argparse
import json
import os
import sys
import time
import traceback

# ---------------------------------------------------------------------------
# HTTP shim: prefer requests, fall back to urllib so the script has zero hard deps.
# Both paths expose one helper: http_json(method, url, body, timeout) -> (status, dict).
# ---------------------------------------------------------------------------
try:
    import requests  # type: ignore

    _HAVE_REQUESTS = True
except Exception:  # pragma: no cover - environment dependent
    _HAVE_REQUESTS = False
    import urllib.error
    import urllib.request


def http_json(method, url, body=None, timeout=(5, 120)):
    """POST/GET JSON. Returns (status_code, parsed_json_or_text).

    `timeout` is (connect, read) seconds when requests is available; with urllib
    only the total is used. On transport error returns (0, {"error": ...}) so the
    caller never sees an exception from the network layer.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    try:
        if _HAVE_REQUESTS:
            resp = requests.request(
                method, url, data=data, headers=headers, timeout=timeout
            )
            status = resp.status_code
            try:
                return status, resp.json()
            except Exception:
                return status, {"_raw": resp.text}
        else:
            # urllib path: single total timeout (use the read side of the tuple).
            total = timeout[1] if isinstance(timeout, (tuple, list)) else timeout
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=total) as r:
                raw = r.read().decode("utf-8")
                status = r.getcode()
                try:
                    return status, json.loads(raw)
                except Exception:
                    return status, {"_raw": raw}
    except urllib.error.HTTPError as e:  # type: ignore[name-defined]
        # HTTP error still carries a JSON body we want (e.g. {"ok":false,...}).
        try:
            raw = e.read().decode("utf-8")
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"error": "http_error", "status": e.code}
    except Exception as e:
        return 0, {"error": "transport_error", "detail": str(e)}


# ---------------------------------------------------------------------------
# Config — defaults, overridable by env then argv (argv wins).
# ---------------------------------------------------------------------------
DEFAULTS = {
    "BRIDGE_URL": os.environ.get("BRIDGE_URL", "http://127.0.0.1:7744"),
    "OLLAMA_URL": os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434"),
    "MODEL": os.environ.get("MODEL", "qwen2.5:14b"),
    "MAX_STEPS": int(os.environ.get("MAX_STEPS", "40")),
}

# Bounded network timeouts (connect, read) seconds.
BRIDGE_TIMEOUT = (5, 30)
OLLAMA_TIMEOUT = (10, 300)  # local LLM inference can be slow; generous read timeout.

# How many prior turn messages to keep in the rolling chat history (bounds context).
HISTORY_LIMIT = 6
# Consecutive hard errors before we bail out of the loop.
MAX_CONSECUTIVE_ERRORS = 4


# ---------------------------------------------------------------------------
# System prompt: the decision loop + survival rules + tool usage. Distilled from
# docs/knowledge/survival-basics.md and docs/kb/*.md — rules, not prose. Kept to
# ~1.5-2k tokens so it fits comfortably in the 16k context.
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """\
You are the operator for a Timberborn beaver colony. You are a PLANFUL operator:
you hold a simple layout plan a few buildings ahead (where water, housing, farms,
paths, and future amenities go) and execute toward it ONE tool call per turn. The
bridge validates every action and returns teaching errors (with a nearest valid
tile) you correct next turn. When unsure of a tile, use the suggested one.

DECISION LOOP (each turn):
1. You are given the current digested colony state (population, resources with
   days_remaining, weather forecast, buildings).
2. Keep/refresh a short layout plan: the next 3-4 buildings AND roughly where they
   go, chosen so survival needs are met AND the colony stays connected and
   expandable (see COLONY LAYOUT).
3. Make exactly ONE tool call that advances that plan (usually the next building
   or a Path to connect it). If prep is done and nothing is understocked, call
   set_speed to advance time and re-check.
4. You will see the action result next turn; iterate.

COLONY LAYOUT (plan ahead - do NOT just react to the immediate gap):
  - Think a few steps ahead like a town plan: decide roughly where water
    infrastructure, housing, farms, storage, and later amenities will sit BEFORE
    placing, so you don't box yourself in.
  - Cluster by function and CONNECT with Paths as you go (nothing works off-path):
    water (WaterPump + SmallTanks) on the river edge; housing (Lodges) clustered
    near the district center and workplaces; farms on moist soil near the water;
    storage central.
  - Leave room to EXPAND: droughts lengthen, so reserve space for more tanks each
    cycle, and a spot for wellbeing/amenity buildings to add once thirst+hunger+
    shelter are secured.
  - Minimize beaver travel: put workplaces near housing and the goods they use.
  - Lay the Path that connects a new building in the same few turns you place it.

SURVIVAL PRIORITY (satisfy top-down; never let a lower need steal labor from an
unmet higher one):
  THIRST (water) > HUNGER (food) > SLEEP/SHELTER (housing) > wellbeing/expansion.
Thirst is the emergency: an empty thirst bar kills a beaver in ~4.3 days and
empties whenever no drinkable water is reachable. Water outage during drought is
the #1 cause of colony death.

CORE NUMBERS (per beaver/day): water 2.13, food 2.67.
Let P = population.total and D = duration_days of the NEXT hazard from the forecast.
  needed_water = (D + 2) * 2.13 * P   (in TANKS — river/pump water does NOT count)
  needed_food  = (D + 2) * 2.67 * P
The bridge already computes each resource's days_remaining; compare it to
(D + 2). If a resource's days_remaining < D + 2, that resource is UNDERSTOCKED —
fix it before any expansion or wellbeing building.

WEATHER: game starts temperate. Each cycle = temperate then a hazard.
  - DROUGHT: water sources stop; only water already in TANKS is safe (tanks never
    evaporate; reservoirs do). Crops stop growing unless irrigated.
  - BADTIDE: river turns contaminated; pumps can't supply drinkable water. Enter
    with tanks FULL of clean temperate water; do not pump into drinking storage
    while contaminated. Contamination is catastrophic for beavers.
Droughts LENGTHEN across a run — read D from the forecast every cycle; expand
water storage each cycle, never just maintain it.

BUILD ORDER (early game, until first drought is survivable). Use these spec ids
with place_building. Only build the NEXT missing survival item:
  1. Path            — connect buildings; nothing works off-path.
  2. WaterPump       — 12 logs, on river edge depth<=2. Gets clean water flowing.
  3. SmallTank       — 15 logs, cap 30, no evaporation. Drought insurance. Build
                       ceil(needed_water / 30) of them.
  4. Lodge           — 12 logs, houses 3. One per 3 beavers (satisfies sleep).
  5. GathererFlag    — free, 1 worker. Immediate wild-berry food.
  6. EfficientFarmhouse — 25 logs, 3 farmers. Plant carrots (4-day cycle); bank a
                       harvest before drought.
  7. SmallWarehouse  — stores logs/goods so production isn't buffer-capped.
  8. Inventor        — science; ONLY after water+food+housing are secured.
  9. Dam (20 logs) / MediumTank (needs science, cap 300) — secondary water reserve.
Keep a Lumberjack + Forester loop so logs (the master resource) never hit zero.

RULES OF THUMB:
  - If any resource days_remaining < D + 2 while temperate: build/fill storage NOW.
    You cannot fill tanks during a drought (no flowing water).
  - Keep beds >= population; a homeless beaver loses sleep and (Folktails) stops
    breeding.
  - Only allow population growth when BOTH water and food cover (D + 2) days for P.
  - Keep the game paused (speed 0) while you build; use set_speed 2-4 to advance a
    bit when prep is done, then it re-pauses and you re-check.

COORDINATES (important):
  - Tiles are (x, y, z) where x,y are the HORIZONTAL plane and z is HEIGHT.
  - The state gives district_center {x,y,z}. BUILD NEAR IT: start placements within
    a few tiles of district_center.x / .y, and use z = district_center.z (the
    ground height) — do NOT use z=0.
  - A bad placement returns error "invalid_placement" with
    suggestion.nearest_valid {x,y,z,orientation}. On your NEXT turn, retry
    place_building with exactly that suggested tile.
  - Spec ids are simple names (WaterPump, SmallTank, Lodge, Path, ...) — no faction
    suffix needed.

TOOL USE:
  - Make exactly ONE tool call per turn — do not narrate a plan without acting.
  - If a command returns "not_implemented", pick a different action; that command
    is not live yet in this bridge build.
"""


# ---------------------------------------------------------------------------
# Tool schema (Ollama / OpenAI-style function-calling format). Mirrors the
# POST /act command enum from docs/api-contract.md. We include commands that
# aren't live yet (place_building, save) so the model has the full surface; the
# bridge returns a "not_implemented" error we handle gracefully.
#
# We expose /act commands as INDIVIDUAL tools (one per command) rather than a
# single generic act(command, args): small models pick a named tool with a tight
# arg schema far more reliably than they fill a free-form {command, args} blob.
# ---------------------------------------------------------------------------
BUILDING_SPECS = [
    "Path",
    "WaterPump",
    "SmallTank",
    "MediumTank",
    "MiniLodge",
    "Lodge",
    "GathererFlag",
    "EfficientFarmHouse",
    "SmallWarehouse",
    "Forester",
    "LumberjackFlag",
    "Inventor",
    "Dam",
    "Levee",
    "Floodgate",
    "LumberjackFlag",
    "Forester",
]
ORIENTATIONS = ["North", "East", "South", "West"]

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "set_speed",
            "description": (
                "Set game speed. 0 = pause (default operating state). 1-10 advances "
                "game time so the colony acts; use 2-4 to advance a bit after building, "
                "then re-observe. Higher = faster/riskier."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "speed": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 10,
                        "description": "0 pauses; 1-10 runs the game.",
                    }
                },
                "required": ["speed"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "place_building",
            "description": (
                "Place one building at a map tile. On invalid placement the bridge "
                "returns a teaching error with a suggested valid tile to retry."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "spec": {
                        "type": "string",
                        "enum": BUILDING_SPECS,
                        "description": "Building spec id to place.",
                    },
                    "x": {"type": "integer", "description": "Map X coordinate."},
                    "y": {
                        "type": "integer",
                        "description": "Vertical layer / height (usually 0 at ground).",
                    },
                    "z": {"type": "integer", "description": "Map Z coordinate."},
                    "orientation": {
                        "type": "string",
                        "enum": ORIENTATIONS,
                        "description": "Facing direction.",
                    },
                },
                "required": ["spec", "x", "y", "z", "orientation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save",
            "description": "Save a rollback checkpoint before a risky change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Save slot / name.",
                    }
                },
                "required": ["name"],
            },
        },
    },
]

# Map each tool name -> the /act command string the bridge expects, plus how to
# turn the tool's flat args into the command's args object.
TOOL_TO_ACT = {
    "set_speed": ("set_speed", lambda a: {"speed": a.get("speed", 0)}),
    "place_building": (
        "place_building",
        lambda a: {
            "spec": a.get("spec"),
            "x": a.get("x"),
            "y": a.get("y", 0),
            "z": a.get("z"),
            "orientation": a.get("orientation", "North"),
        },
    ),
    "save": ("save", lambda a: {"name": a.get("name", "agent")}),
}


# ---------------------------------------------------------------------------
# Bridge client
# ---------------------------------------------------------------------------
class Bridge:
    def __init__(self, base_url):
        self.base = base_url.rstrip("/")

    def ping(self):
        return http_json("GET", self.base + "/ping", timeout=BRIDGE_TIMEOUT)

    def state(self):
        return http_json("GET", self.base + "/state", timeout=BRIDGE_TIMEOUT)

    def act(self, command, args):
        body = {"command": command, "args": args}
        return http_json("POST", self.base + "/act", body=body, timeout=BRIDGE_TIMEOUT)


# ---------------------------------------------------------------------------
# Ollama client — POST /api/chat with tools, non-streaming.
# ---------------------------------------------------------------------------
class Ollama:
    def __init__(self, base_url, model):
        self.base = base_url.rstrip("/")
        self.model = model

    def chat(self, messages, tools):
        body = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "options": {"temperature": 0.2, "num_ctx": 16384},
        }
        # Retry once on transport/5xx failure — the tunnel can hiccup.
        for attempt in (1, 2):
            status, data = http_json(
                "POST", self.base + "/api/chat", body=body, timeout=OLLAMA_TIMEOUT
            )
            if status == 200 and isinstance(data, dict) and "message" in data:
                return data["message"]
            if attempt == 1:
                log_stderr(
                    "ollama call failed (status=%s), retrying once..." % status
                )
                time.sleep(2)
        # Both attempts failed.
        raise RuntimeError("ollama chat failed: status=%s body=%s" % (status, _short(data)))


# ---------------------------------------------------------------------------
# State digest -> compact user message. We do NOT dump the full JSON; we render
# the survival-relevant fields tersely to keep context small.
# ---------------------------------------------------------------------------
def compact_state(state):
    """Turn the digested /state JSON into a compact human/LLM-readable block.

    Tolerant of missing keys — the bridge schema may evolve; we render what's there.
    """
    if not isinstance(state, dict):
        return "STATE: <unavailable>"

    lines = []

    t = state.get("time", {}) or {}
    lines.append(
        "TIME cycle=%s day=%s hour=%s %s"
        % (
            t.get("cycle", "?"),
            t.get("day", "?"),
            t.get("hour", "?"),
            t.get("daytime", t.get("time_of_day", "")),
        )
    )

    # Weather forecast (shape may be flat or nested per api-contract).
    w = state.get("weather")
    if isinstance(w, dict):
        nxt = w.get("next", {}) or {}
        lines.append(
            "WEATHER current=%s ends_in=%s NEXT=%s in=%sd duration=%sd"
            % (
                w.get("current", "?"),
                w.get("current_ends_in_days", "?"),
                nxt.get("type", "?"),
                nxt.get("in_days", "?"),
                nxt.get("duration_days", "?"),
            )
        )

    p = state.get("population", {}) or {}
    lines.append(
        "POP total=%s adults=%s kits=%s free_workslots=%s unemployed=%s "
        "free_beds=%s homeless=%s"
        % (
            p.get("total", "?"),
            p.get("adults", "?"),
            p.get("kits", "?"),
            p.get("free_workslots", "?"),
            p.get("unemployed", "?"),
            p.get("free_beds", "?"),
            p.get("homeless", "?"),
        )
    )

    res = state.get("resources", []) or []
    if res:
        lines.append("RESOURCES:")
        for r in res:
            if not isinstance(r, dict):
                continue
            parts = ["  %s stored=%s" % (r.get("good", "?"), r.get("stored", "?"))]
            if "all_stock" in r:
                parts.append("all=%s" % r.get("all_stock"))
            if "capacity" in r:
                parts.append("cap=%s" % r.get("capacity"))
            if "fill_rate" in r:
                parts.append("fill=%s" % r.get("fill_rate"))
            if "net_per_day" in r:
                parts.append("net/d=%s" % r.get("net_per_day"))
            if "days_remaining" in r:
                parts.append("days_left=%s" % r.get("days_remaining"))
            lines.append(" ".join(parts))

    b = state.get("buildings", {}) or {}
    counts = b.get("counts", {}) or {}
    if counts:
        lines.append(
            "BUILDINGS " + ", ".join("%s=%s" % (k, v) for k, v in counts.items())
        )
    uc = b.get("under_construction")
    if uc is not None:
        lines.append("UNDER_CONSTRUCTION=%s" % uc)
    for extra in ("unstaffed", "paused"):
        if b.get(extra):
            lines.append("%s=%s" % (extra.upper(), b.get(extra)))

    # Bridge-computed alerts are the model's triage — surface them prominently.
    alerts = state.get("alerts", []) or []
    if alerts:
        lines.append("ALERTS:")
        for a in alerts:
            if isinstance(a, dict):
                lines.append(
                    "  [%s] %s%s"
                    % (
                        a.get("severity", "?"),
                        a.get("message", a.get("id", "?")),
                        (" -> " + a["suggestion"]) if a.get("suggestion") else "",
                    )
                )

    return "CURRENT STATE:\n" + "\n".join(lines)


def state_summary_for_journal(state):
    """Small structured summary of state for the journal (not the whole blob)."""
    if not isinstance(state, dict):
        return {"ok": False}
    p = state.get("population", {}) or {}
    res = {}
    for r in state.get("resources", []) or []:
        if isinstance(r, dict) and "good" in r:
            res[r["good"]] = {
                "stored": r.get("stored"),
                "days_remaining": r.get("days_remaining"),
            }
    w = state.get("weather", {}) or {}
    return {
        "time": state.get("time"),
        "weather_current": w.get("current"),
        "weather_next": w.get("next"),
        "population_total": p.get("total"),
        "resources": res,
        "under_construction": (state.get("buildings", {}) or {}).get(
            "under_construction"
        ),
    }


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------
def log_stderr(msg):
    print("[agent] " + msg, file=sys.stderr, flush=True)


def _short(obj, n=300):
    s = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    return s if len(s) <= n else s[:n] + "..."


def parse_tool_calls(message):
    """Extract normalized tool calls from an Ollama chat message.

    Returns a list of {"name": str, "args": dict}. Ollama returns tool calls under
    message["tool_calls"][i]["function"] with "name" and "arguments" (already a
    dict, but tolerate a JSON string). Returns [] if none / malformed.
    """
    calls = []
    raw = (message or {}).get("tool_calls") or []
    for c in raw:
        fn = (c or {}).get("function") or {}
        name = fn.get("name")
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        if not isinstance(args, dict):
            args = {}
        if name:
            calls.append({"name": name, "args": args})
    return calls


def journal_append(path, record):
    """Append one JSON line. Journal failures must never crash the run."""
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        log_stderr("journal write failed: %s" % e)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run(cfg, run_id, max_steps):
    bridge = Bridge(cfg["BRIDGE_URL"])
    ollama = Ollama(cfg["OLLAMA_URL"], cfg["MODEL"])

    # Journal path: agent/journal/<run_id>.jsonl next to this script.
    here = os.path.dirname(os.path.abspath(__file__))
    journal_dir = os.path.join(here, "journal")
    os.makedirs(journal_dir, exist_ok=True)
    journal_path = os.path.join(journal_dir, "%s.jsonl" % run_id)
    log_stderr("journal: %s" % journal_path)

    # Preflight: bridge and ollama liveness (warn but continue — they may come up).
    pstatus, pdata = bridge.ping()
    if pstatus == 200:
        log_stderr("bridge OK: %s" % _short(pdata))
    else:
        log_stderr("WARNING: bridge /ping failed (status=%s) — will retry per step." % pstatus)

    journal_append(
        journal_path,
        {"run_id": run_id, "event": "run_start", "config": {k: cfg[k] for k in cfg},
         "max_steps": max_steps, "ping": pdata},
    )

    # Rolling chat history (after the system prompt). We keep it short.
    history = []
    consecutive_errors = 0

    for step in range(1, max_steps + 1):
        try:
            # --- 1. Read digested state ------------------------------------
            sstatus, state = bridge.state()
            if sstatus != 200:
                consecutive_errors += 1
                log_stderr(
                    "step %d: /state failed (status=%s) err#%d"
                    % (step, sstatus, consecutive_errors)
                )
                journal_append(
                    journal_path,
                    {"run_id": run_id, "step": step, "event": "state_error",
                     "status": sstatus, "body": state},
                )
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    log_stderr("too many consecutive errors — exiting.")
                    break
                time.sleep(2)
                continue

            state_block = compact_state(state)

            # --- 2. Compose user message & call the LLM --------------------
            user_msg = {"role": "user", "content": state_block +
                        "\n\nChoose the single most urgent action now. Make exactly "
                        "ONE tool call."}
            messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history[-HISTORY_LIMIT:] + [user_msg]

            try:
                assistant_msg = ollama.chat(messages, TOOLS)
            except Exception as e:
                consecutive_errors += 1
                log_stderr("step %d: ollama failed: %s err#%d" % (step, e, consecutive_errors))
                journal_append(
                    journal_path,
                    {"run_id": run_id, "step": step, "event": "llm_error", "detail": str(e)},
                )
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    log_stderr("too many consecutive errors — exiting.")
                    break
                time.sleep(2)
                continue

            calls = parse_tool_calls(assistant_msg)

            # --- 3. No tool call -> nudge once, then move on ---------------
            if not calls:
                content = (assistant_msg or {}).get("content", "")
                log_stderr("step %d: no tool call. Nudging. (model said: %s)"
                           % (step, _short(content, 120)))
                # Keep the assistant turn, add a nudge, re-ask ONCE.
                nudge_messages = messages + [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content":
                     "You did not call a tool. Respond with exactly one tool call "
                     "(set_speed if nothing is urgent)."},
                ]
                try:
                    assistant_msg = ollama.chat(nudge_messages, TOOLS)
                    calls = parse_tool_calls(assistant_msg)
                except Exception as e:
                    log_stderr("step %d: nudge ollama failed: %s" % (step, e))
                    calls = []

            if not calls:
                # Still nothing — record it, advance time ourselves so the run
                # doesn't stall, and continue.
                log_stderr("step %d: still no tool call after nudge; defaulting to set_speed 0." % step)
                chosen = {"name": "set_speed", "args": {"speed": 0}}
                act_status, act_result = bridge.act("set_speed", {"speed": 0})
            else:
                # Take the FIRST tool call (one action per turn by design).
                chosen = calls[0]
                name = chosen["name"]
                args = chosen["args"]

                mapping = TOOL_TO_ACT.get(name)
                if mapping is None:
                    # Model invented / hallucinated a tool name.
                    log_stderr("step %d: unknown tool '%s' — treating as malformed." % (step, name))
                    act_status, act_result = 0, {"ok": False, "error": "unknown_tool", "tool": name}
                else:
                    command, arg_fn = mapping
                    act_args = arg_fn(args)
                    act_status, act_result = bridge.act(command, act_args)

            # --- 4. Interpret the action result ----------------------------
            ok = isinstance(act_result, dict) and act_result.get("ok") is True
            err = None
            if not ok and isinstance(act_result, dict):
                err = act_result.get("error") or act_result.get("reason") or "unknown"
                # A "not_implemented" command is expected for not-yet-live commands;
                # not a hard failure — the model should pick something else next turn.

            if act_status == 200 and (ok or err == "not_implemented"):
                consecutive_errors = 0
            elif act_status == 0 and (isinstance(act_result, dict) and act_result.get("error") == "unknown_tool"):
                # Malformed tool call from the model — count softly, re-prompt via history.
                consecutive_errors += 1
            elif not ok:
                # Teaching error (invalid placement, etc.) — NOT a transport failure;
                # this is normal feedback the model corrects from. Don't count it.
                consecutive_errors = 0
            else:
                consecutive_errors += 1

            # --- 5. Per-step stdout line -----------------------------------
            status_word = "OK" if ok else ("ERR:%s" % err if err else "?")
            print(
                "step %02d/%d | %s(%s) -> %s"
                % (step, max_steps, chosen["name"], _short(chosen["args"], 80), status_word),
                flush=True,
            )

            # --- 6. Journal the full step ----------------------------------
            journal_append(
                journal_path,
                {
                    "run_id": run_id,
                    "step": step,
                    "event": "step",
                    "state": state_summary_for_journal(state),
                    "action": {"tool": chosen["name"], "args": chosen["args"]},
                    "result": {"http_status": act_status, "body": act_result},
                },
            )

            # --- 7. Update rolling history (bounded) ------------------------
            # Feed the action + result back so the model has short-term memory.
            history.append(user_msg)
            history.append({
                "role": "assistant",
                "content": "Called %s with %s" % (chosen["name"], json.dumps(chosen["args"], default=str)),
            })
            history.append({
                "role": "user",
                "content": "Action result: " + _short(act_result, 400),
            })
            # Trim to the last HISTORY_LIMIT messages.
            history = history[-HISTORY_LIMIT:]

        except KeyboardInterrupt:
            log_stderr("interrupted by user (Ctrl-C) — exiting cleanly.")
            journal_append(journal_path, {"run_id": run_id, "step": step, "event": "interrupted"})
            break
        except Exception as e:
            # Any unexpected error: log, journal, count, keep going unless it recurs.
            consecutive_errors += 1
            log_stderr("step %d: UNEXPECTED %s\n%s" % (step, e, traceback.format_exc()))
            journal_append(
                journal_path,
                {"run_id": run_id, "step": step, "event": "exception", "detail": str(e)},
            )
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                log_stderr("too many consecutive errors — exiting.")
                break
            time.sleep(2)

    journal_append(journal_path, {"run_id": run_id, "event": "run_end"})
    log_stderr("run complete. journal: %s" % journal_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Timberborn LLM agent loop (v1).")
    parser.add_argument("--bridge-url", default=DEFAULTS["BRIDGE_URL"])
    parser.add_argument("--ollama-url", default=DEFAULTS["OLLAMA_URL"])
    parser.add_argument("--model", default=DEFAULTS["MODEL"])
    parser.add_argument("--max-steps", type=int, default=DEFAULTS["MAX_STEPS"])
    parser.add_argument(
        "--run-id",
        default=os.environ.get("RUN_ID", "run"),
        help="Journal filename stem (agent/journal/<run-id>.jsonl). "
             "Pass a timestamp for uniqueness; sandbox may block auto-timestamping.",
    )
    args = parser.parse_args(argv)

    cfg = {
        "BRIDGE_URL": args.bridge_url,
        "OLLAMA_URL": args.ollama_url,
        "MODEL": args.model,
        "MAX_STEPS": args.max_steps,
    }
    log_stderr("config: %s run_id=%s" % (cfg, args.run_id))
    run(cfg, args.run_id, args.max_steps)


if __name__ == "__main__":
    main()
