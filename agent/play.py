#!/usr/bin/env python3
"""
play.py — first-version agent loop that plays Timberborn through the TimberBridge
HTTP API using a local LLM (Ollama with schema-constrained JSON output).

Architecture (see docs/agent/agent-design.md, docs/api-contract.md):
  - TimberBridge exposes the live game over localhost HTTP: GET /ping, GET /state
    (a *digested* colony snapshot — the bridge does the survival math), POST /act
    (one validated command; returns teaching errors on failure).
  - The LLM is the cheap "operator": every step it reads the digested state, picks
    ONE JSON action, we forward it to the bridge, feed the result back, and advance.
  - We keep the game paused between decisions (set_speed 0) so every read is a
    consistent snapshot and there is no wall-clock pressure on the model.

Design bias for this first version: robustness over cleverness. Everything degrades
gracefully — missing bridge, missing Ollama, malformed JSON actions, unimplemented
commands — and every step is logged to a JSONL run-journal for later retrospective.

Runtime deps: Python 3.8+ stdlib. Uses `requests` if importable, else falls back to
urllib. No third-party requirement.
"""

import argparse
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
if AGENT_DIR not in sys.path:
    sys.path.insert(0, AGENT_DIR)

try:
    import planner
except Exception:  # pragma: no cover - import path depends on invocation style
    from agent import planner  # type: ignore

try:
    import metrics as metrics_mod
    import coach as coach_mod
except Exception:  # pragma: no cover - end-of-run learning is best-effort
    metrics_mod = None
    coach_mod = None

try:
    import discovery as discovery_mod
except Exception:  # pragma: no cover - mechanics discovery is best-effort
    try:
        from agent import discovery as discovery_mod  # type: ignore
    except Exception:  # pragma: no cover
        discovery_mod = None

try:
    from kb import lookup as kb_lookup
except Exception:  # pragma: no cover - import path depends on invocation style
    try:
        from agent.kb import lookup as kb_lookup
    except Exception:  # pragma: no cover - agent can still run without KB
        kb_lookup = None

# Optional visual layer (screenshot -> VLM critique). Degrades to no-op if absent.
try:
    from vision import look as vision_look
except Exception:  # pragma: no cover
    try:
        from agent.vision import look as vision_look
    except Exception:  # pragma: no cover
        vision_look = None

# ---------------------------------------------------------------------------
# HTTP shim: prefer requests, fall back to urllib so the script has zero hard deps.
# Both paths expose one helper: http_json(method, url, body, timeout) -> (status, dict).
# ---------------------------------------------------------------------------
try:
    import requests  # type: ignore

    _HAVE_REQUESTS = True
except Exception:  # pragma: no cover - environment dependent
    _HAVE_REQUESTS = False


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
    # Visual layer: a VLM critiques a screenshot every N steps (0 = disabled).
    # Kept infrequent because swapping the text<->vision model on the GPU is costly.
    "VISION_MODEL": os.environ.get("VISION_MODEL", "qwen2.5vl:7b"),
    "VISION_EVERY": int(os.environ.get("VISION_EVERY", "5")),
}

PLAYBOOK_PATH = os.path.join(AGENT_DIR, "playbook.json")
METRICS_CSV_PATH = os.path.join(AGENT_DIR, "metrics.csv")

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
You operate a Timberborn colony through the bridge. Each turn you receive STATE,
RESOURCES, PLANNER, KB rules, optional VISION, recent Action results, and OBSERVED
effects from the last action.

Rules:
- Survival priority is thirst > hunger > shelter; use weather forecast tank math
  from STATE/PLANNER before droughts or badtides.
- Buildings auto-connect and auto-orient. Never place Path yourself for routine
  construction; choose PLANNER building tiles and let the bridge connect them.
- To get logs: place LumberjackFlag near mature trees, then MUST
  designate_cutting, then advance time. A lumberjack alone yields 0 logs.
- Gathering is automatic when a staffed GathererFlag is near ready bushes.
- Put production on/next to resources shown in RESOURCES and PLANNER. Do not
  invent coordinates when planner candidates exist.
- Advance time after queuing construction or production designations so beavers
  build, haul, cut, pump, and gather.
- Use OBSERVED effects to learn what worked last turn and adapt.

Actions: set_speed, place_building, demolish, set_priority, designate_cutting,
designate_planting, save, noop. Return only JSON matching the schema:
{"plan": string, "actions": [1 to 8 {"action": string, "args": object}]}.
"""


# ---------------------------------------------------------------------------
# Ollama structured-output schema. The model chooses an ordered queue; the bridge
# still validates command-specific args and returns teaching errors.
# ---------------------------------------------------------------------------
ACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "plan": {"type": "string"},
        "actions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "set_speed",
                            "place_building",
                            "demolish",
                            "set_priority",
                            "designate_cutting",
                            "designate_planting",
                            "save",
                            "noop",
                        ],
                    },
                    "args": {"type": "object"},
                },
                "required": ["action", "args"],
            },
        },
    },
    "required": ["plan", "actions"],
}

ARBITER_SYSTEM_PROMPT = """\
You arbitrate a Timberborn planner fork. Choose exactly one enumerated option.
Return its ordered goal_ids and a brief reason. Never propose actions or coordinates.
"""

# Map each action name -> the /act command string the bridge expects, plus how to
# turn the model's args into the command's args object.
def _normalize_coords(a):
    """Coerce coordinates the model emitted into flat x,y,z. Accepts position as
    [x,y,z], {x,y,z}, or already-flat keys."""
    a = dict(a or {})
    out = dict(a)
    pos = a.get("position") or a.get("pos") or a.get("coordinates") or a.get("coord")
    if isinstance(pos, (list, tuple)) and len(pos) >= 3:
        out["x"], out["y"], out["z"] = pos[0], pos[1], pos[2]
    elif isinstance(pos, dict):
        out["x"] = pos.get("x", a.get("x"))
        out["y"] = pos.get("y", a.get("y"))
        out["z"] = pos.get("z", a.get("z"))
    return out


_SPEC_KEYS = (
    "spec", "spec_id", "building", "building_type", "buildingtype", "building_name",
    "name", "type", "blueprint", "blueprint_name",
)
_NON_SPEC_KEYS = {
    "x", "y", "z", "position", "pos", "coord", "coordinates", "orientation",
    "instant", "speed", "priority",
}


def _normalize_place_args(a):
    """place_building: normalize spec id AND coordinates into the bridge's contract.
    Models invent endless key names for the building id (building/building_type/
    blueprint/...), so try the known set, then fall back to the first plausible
    string value that isn't a coordinate/orientation."""
    a = a or {}
    out = _normalize_coords(a)
    spec = None
    for key in _SPEC_KEYS:
        if a.get(key):
            spec = a[key]
            break
    if spec is None:  # last resort: any leftover string value that looks like an id
        for key, val in a.items():
            if key.lower() in _NON_SPEC_KEYS:
                continue
            if isinstance(val, str) and val.strip():
                spec = val.strip()
                break
    if spec is not None:
        out["spec"] = spec
    return out


ACTION_TO_ACT = {
    # An omitted-speed set_speed almost always means "advance time", never pause;
    # the planner appends speed=3 for advancing, so default to 3 not 0.
    "set_speed": ("set_speed", lambda a: {"speed": a.get("speed", 3)}),
    # Normalize spec/coord shapes; the bridge still validates + suggests tiles.
    "place_building": ("place_building", _normalize_place_args),
    "demolish": ("demolish", _normalize_coords),
    "set_priority": ("set_priority", _normalize_coords),
    "designate_cutting": ("designate_cutting", lambda a: dict(a or {}) or {"all": True}),
    "designate_planting": ("designate_planting", lambda a: dict(a or {})),
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

    def map(self):
        return http_json("GET", self.base + "/map", timeout=BRIDGE_TIMEOUT)

    def resources(self):
        return http_json("GET", self.base + "/resources", timeout=BRIDGE_TIMEOUT)

    def act(self, command, args):
        body = {"command": command, "args": args}
        return http_json("POST", self.base + "/act", body=body, timeout=BRIDGE_TIMEOUT)


# ---------------------------------------------------------------------------
# Ollama client — POST /api/chat with schema-constrained JSON, non-streaming.
# ---------------------------------------------------------------------------
class Ollama:
    def __init__(self, base_url, model):
        self.base = base_url.rstrip("/")
        self.model = model

    def chat(self, messages, schema=None):
        body = {
            "model": self.model,
            "messages": messages,
            "format": schema or ACTION_SCHEMA,
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


def compact_resources_summary(resources, state=None):
    if not isinstance(resources, dict) or resources.get("ok") is False:
        return "RESOURCES DETAIL: <unavailable>"

    lines = ["RESOURCES DETAIL:"]
    counts = resources.get("counts")
    if isinstance(counts, dict) and counts:
        lines.append(
            "COUNTS " + ", ".join("%s=%s" % (key, counts[key]) for key in sorted(counts))
        )

    dc = (state.get("district_center") if isinstance(state, dict) else None) or {}
    mature = [
        item for item in resources.get("trees", []) or []
        if isinstance(item, dict) and item.get("mature") is True
    ]
    ready = [
        item for item in resources.get("gatherables", []) or []
        if isinstance(item, dict) and item.get("ready") is True
    ]
    lines.append("MATURE TREE CLUSTER " + _resource_cluster_summary(mature, dc, "mature trees"))
    lines.append("READY BUSH CLUSTER " + _resource_cluster_summary(ready, dc, "ready gatherables"))
    if resources.get("truncated"):
        lines.append("TRUNCATED=true; planner used a partial resource list")
    return "\n".join(lines)


def _resource_cluster_summary(items, dc, label):
    if not items:
        return "none"
    clusters = []
    for anchor in items:
        cluster = [item for item in items if _resource_distance(anchor, item) <= 8]
        cx = round(sum(_as_float(item.get("x")) for item in cluster) / len(cluster), 1)
        cy = round(sum(_as_float(item.get("y", item.get("z"))) for item in cluster) / len(cluster), 1)
        species = _dominant_resource_name(cluster)
        dist = abs(_as_float(cx) - _as_float(dc.get("x"))) + abs(
            _as_float(cy) - _as_float(dc.get("y", dc.get("z")))
        )
        clusters.append((-len(cluster), dist, cx, cy, species))
    clusters.sort()
    count = -clusters[0][0]
    _dist, cx, cy, species = clusters[0][1], clusters[0][2], clusters[0][3], clusters[0][4]
    return "%s %s near x=%s y=%s (%s)" % (count, species or label, cx, cy, label)


def _dominant_resource_name(items):
    counts = {}
    for item in items:
        name = str(item.get("species") or item.get("good") or "").strip()
        if not name:
            continue
        counts[name] = counts.get(name, 0) + 1
    if not counts:
        return ""
    name, count = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[0]
    if count != 1 and not name.endswith("s"):
        name += "s"
    return name


def _resource_distance(a, b):
    return abs(_as_float(a.get("x")) - _as_float(b.get("x"))) + abs(
        _as_float(a.get("y", a.get("z"))) - _as_float(b.get("y", b.get("z")))
    )


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _resource_by_good(state):
    resources = {}
    for item in (state.get("resources", []) if isinstance(state, dict) else []) or []:
        if isinstance(item, dict) and item.get("good") is not None:
            resources[str(item.get("good")).lower()] = item
    return resources


def _building_count(state, spec):
    counts = (((state or {}).get("buildings", {}) or {}).get("counts", {}) or {})
    if spec in counts:
        return _as_int(counts.get(spec), 0)
    lowered = spec.lower()
    for key, value in counts.items():
        if str(key).lower() == lowered:
            return _as_int(value, 0)
    return 0


def _next_hazard_days(state):
    weather = (state.get("weather", {}) if isinstance(state, dict) else {}) or {}
    nxt = weather.get("next", {}) or {}
    return _as_float(nxt.get("duration_days"), 0.0)


def infer_next_building_type(state):
    """Small deterministic hint used for map candidates and KB query construction."""
    if not isinstance(state, dict):
        return "WaterPump"

    if _building_count(state, "WaterPump") <= 0:
        return "WaterPump"

    population = (state.get("population", {}) or {}).get("total")
    p = max(_as_int(population, 0), 1)
    required_water = (_next_hazard_days(state) + 2.0) * 2.13 * p
    required_tanks = int((required_water + 29.999) // 30) if required_water > 0 else 1
    tanks = (
        _building_count(state, "SmallTank")
        + _building_count(state, "MediumTank") * 10
        + _building_count(state, "LargeWaterTank") * 10
    )
    # Gate on tank CAPACITY vs the target, not on water_days: while the game is
    # paused (and before beavers finish building), tanks read empty, so a
    # days-based gate would demand tanks forever. Enough tanks placed -> move on;
    # filling them is a matter of advancing time, not building more.
    if tanks < max(required_tanks, 1):
        return "SmallTank"

    pop = state.get("population", {}) or {}
    if _as_int(pop.get("homeless"), 0) > 0 or _as_int(pop.get("free_beds"), 0) < 0:
        return "Lodge"

    if _building_count(state, "GathererFlag") <= 0:
        return "GathererFlag"
    if _building_count(state, "EfficientFarmhouse") <= 0:
        return "EfficientFarmhouse"
    if _building_count(state, "SmallWarehouse") <= 0:
        return "SmallWarehouse"
    return "Path"


def _map_arrays(map_data):
    if not isinstance(map_data, dict):
        return None
    width = _as_int(map_data.get("width"), 0)
    height = _as_int(map_data.get("height"), 0)
    origin = map_data.get("origin", {}) or {}
    if width <= 0 or height <= 0:
        return None
    total = width * height
    terrain = map_data.get("terrain_height") or []
    water = map_data.get("water_depth") or []
    contamination = map_data.get("contamination") or []
    moist = map_data.get("moist")
    if moist is None:
        moist = map_data.get("moisture")
    occupied = map_data.get("occupied") or []
    if len(terrain) < total:
        return None
    return {
        "origin_x": _as_int(origin.get("x"), 0),
        "origin_y": _as_int(origin.get("z", origin.get("y", 0)), 0),
        "width": width,
        "height": height,
        "terrain": terrain,
        "water": water,
        "contamination": contamination,
        "moist": moist or [],
        "occupied": occupied,
    }


def _array_value(values, index, default=0):
    if not isinstance(values, list) or index < 0 or index >= len(values):
        return default
    return values[index]


def _tile(arrays, col, row):
    width = arrays["width"]
    height = arrays["height"]
    if col < 0 or row < 0 or col >= width or row >= height:
        return None
    index = row * width + col
    return {
        "x": arrays["origin_x"] + col,
        "y": arrays["origin_y"] + row,
        "z": _array_value(arrays["terrain"], index, 0),
        "water": _as_float(_array_value(arrays["water"], index, 0), 0.0),
        "contamination": _as_float(
            _array_value(arrays["contamination"], index, 0), 0.0
        ),
        "moist": _as_int(_array_value(arrays["moist"], index, 0), 0),
        "occupied": _as_int(_array_value(arrays["occupied"], index, 0), 0),
        "col": col,
        "row": row,
    }


def _district_center_xy(map_data, arrays):
    dc = (map_data.get("district_center", {}) if isinstance(map_data, dict) else {}) or {}
    if "x" in dc:
        cx = _as_int(dc.get("x"), arrays["origin_x"] + arrays["width"] // 2)
    else:
        cx = arrays["origin_x"] + arrays["width"] // 2
    cy = _as_int(dc.get("z", dc.get("y", arrays["origin_y"] + arrays["height"] // 2)))
    return cx, cy


def _tile_distance(tile, cx, cy):
    return abs(tile["x"] - cx) + abs(tile["y"] - cy)


def _same_height_dry_neighbors(arrays, tile):
    same = 0
    for dx, dy, _ in ((0, -1, "North"), (1, 0, "East"), (0, 1, "South"), (-1, 0, "West")):
        other = _tile(arrays, tile["col"] + dx, tile["row"] + dy)
        if not other:
            continue
        if (
            _as_float(other["z"]) == _as_float(tile["z"])
            and other["water"] <= 0
            and other["contamination"] <= 0
            and other["occupied"] == 0
        ):
            same += 1
    return same


def _format_tile(tile, extra=None):
    parts = ["x=%s" % tile["x"], "y=%s" % tile["y"], "z=%s" % tile["z"]]
    if extra:
        parts.extend(extra)
    return "(" + ",".join(parts) + ")"


def _format_tile_list(tiles, limit=6):
    if not tiles:
        return "none"
    return "; ".join(_format_tile(tile, tile.get("extra")) for tile in tiles[:limit])


def compact_map_summary(map_data, state, next_spec):
    """Summarize /map into placement-relevant facts and candidate tiles only."""
    arrays = _map_arrays(map_data)
    if arrays is None:
        return {
            "text": "MAP: <unavailable or malformed>",
            "candidates": [],
            "error": "malformed_map",
        }

    width = arrays["width"]
    height = arrays["height"]
    cx, cy = _district_center_xy(map_data, arrays)
    center_col = cx - arrays["origin_x"]
    center_row = cy - arrays["origin_y"]
    center_tile = _tile(arrays, center_col, center_row)

    clean_edges = []
    flat_dry = []
    moist_tiles = []
    path_tiles = []
    areas = {
        "NW": {"free": 0, "occupied": 0},
        "NE": {"free": 0, "occupied": 0},
        "SW": {"free": 0, "occupied": 0},
        "SE": {"free": 0, "occupied": 0},
    }
    directions = ((0, -1, "North"), (1, 0, "East"), (0, 1, "South"), (-1, 0, "West"))

    for row in range(height):
        for col in range(width):
            tile = _tile(arrays, col, row)
            if tile is None:
                continue

            east_west = "W" if tile["x"] < cx else "E"
            north_south = "N" if tile["y"] < cy else "S"
            area = areas[north_south + east_west]
            if tile["occupied"]:
                area["occupied"] += 1
            elif tile["water"] <= 0 and tile["contamination"] <= 0:
                area["free"] += 1

            if tile["occupied"] or tile["contamination"] > 0:
                continue

            is_land = tile["water"] <= 0
            if not is_land:
                continue

            clean_water_dirs = []
            badwater_dirs = []
            for dx, dy, direction in directions:
                other = _tile(arrays, col + dx, row + dy)
                if not other or other["water"] <= 0:
                    continue
                if other["contamination"] > 0:
                    badwater_dirs.append(direction)
                else:
                    clean_water_dirs.append(direction)

            if clean_water_dirs and not badwater_dirs:
                candidate = dict(tile)
                candidate["extra"] = ["water=%s" % clean_water_dirs[0], "orientation=%s" % clean_water_dirs[0]]
                clean_edges.append(candidate)

            same_height = _same_height_dry_neighbors(arrays, tile)
            if same_height >= 2 and not clean_water_dirs and not badwater_dirs:
                candidate = dict(tile)
                candidate["extra"] = ["flat_neighbors=%s" % same_height]
                flat_dry.append(candidate)

            if tile["moist"] == 1:
                candidate = dict(tile)
                candidate["extra"] = ["moist=1"]
                moist_tiles.append(candidate)

            if _tile_distance(tile, cx, cy) <= 6 and same_height >= 1:
                path_tiles.append(dict(tile))

    for items in (clean_edges, flat_dry, moist_tiles, path_tiles):
        items.sort(key=lambda t: (_tile_distance(t, cx, cy), t["y"], t["x"]))

    if next_spec == "WaterPump":
        candidates = clean_edges[:6]
    elif next_spec == "EfficientFarmhouse":
        candidates = moist_tiles[:6] or flat_dry[:6]
    elif next_spec == "Path":
        candidates = path_tiles[:6] or flat_dry[:6]
    else:
        candidates = flat_dry[:6]

    area_text = ", ".join(
        "%s free_dry=%s occ=%s" % (name, value["free"], value["occupied"])
        for name, value in sorted(areas.items())
    )
    center_text = (
        _format_tile(center_tile) if center_tile is not None else "(x=%s,y=%s,z=?)" % (cx, cy)
    )
    map_size = map_data.get("map_size", {}) if isinstance(map_data, dict) else {}
    lines = [
        "MAP window origin=(x=%s,y=%s) size=%sx%s map_size=%s center=%s"
        % (arrays["origin_x"], arrays["origin_y"], width, height, map_size, center_text),
        "FREE/OCCUPIED by area: " + area_text,
        "CLEAN WATER EDGES for WaterPump (free land adjacent to clean water, no badwater): "
        + _format_tile_list(clean_edges),
        "FLAT DRY LAND for tanks/lodges/storage near center: " + _format_tile_list(flat_dry),
        "MOIST FARMABLE SOIL for farms/fields: " + _format_tile_list(moist_tiles),
        "CANDIDATES for next %s: %s" % (next_spec, _format_tile_list(candidates)),
    ]
    return {"text": "COMPACT MAP:\n" + "\n".join(lines), "candidates": candidates}


def kb_query_for_state(state, top_goal_id):
    if not isinstance(state, dict):
        return "starter base water food housing path planning"
    alerts = []
    for alert in state.get("alerts", []) or []:
        if isinstance(alert, dict):
            alerts.append(str(alert.get("id") or alert.get("message") or ""))
        else:
            alerts.append(str(alert))
    weather = (state.get("weather", {}) or {})
    nxt = weather.get("next", {}) or {}
    resources = _resource_by_good(state)
    water = resources.get("water", {})
    food = resources.get("food", {}) or resources.get("berries", {}) or {}
    return " ".join(
        [
            "current situation",
            "top planner goal %s" % (top_goal_id or "bootstrap"),
            "alerts %s" % " ".join(alerts[:4]),
            "weather current %s next %s duration %s"
            % (weather.get("current", ""), nxt.get("type", ""), nxt.get("duration_days", "")),
            "water days %s food days %s"
            % (water.get("days_remaining", ""), food.get("days_remaining", "")),
            "placement pathing water pump tank lodge farm storage starter base",
        ]
    )


def compact_kb_block(query, k=3):
    if kb_lookup is None:
        return "KB: <unavailable: agent/kb.py could not be imported>"
    try:
        chunks = kb_lookup(query, k=k)
    except Exception as e:
        return "KB: <lookup failed: %s>" % e
    if not chunks:
        return "KB: <no relevant chunks>"

    lines = ["KB RULES for query: %s" % query[:220]]
    for index, chunk in enumerate(chunks[:k], start=1):
        title = chunk.get("heading_path") or chunk.get("title") or chunk.get("source")
        text = str(chunk.get("text", "")).strip().replace("\r\n", "\n")
        if len(text) > 900:
            text = text[:897].rstrip() + "..."
        lines.append("%d. %s [%s]\n%s" % (index, title, chunk.get("source", "?"), text))
    return "\n".join(lines)


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
    buildings = state.get("buildings", {}) or {}
    building_list = []
    if isinstance(buildings.get("list"), list):
        for item in buildings.get("list") or []:
            if not isinstance(item, dict):
                continue
            building_list.append(
                {
                    "spec": item.get("spec"),
                    "status": item.get("status"),
                    "reachable": item.get("reachable"),
                }
            )
    return {
        "time": state.get("time"),
        "weather_current": w.get("current"),
        "weather_next": w.get("next"),
        "population_total": p.get("total"),
        "resources": res,
        "buildings": {
            "counts": buildings.get("counts") if isinstance(buildings, dict) else {},
            "list": building_list,
            "under_construction": buildings.get("under_construction") if isinstance(buildings, dict) else None,
        },
        "under_construction": buildings.get("under_construction") if isinstance(buildings, dict) else None,
        "alerts": state.get("alerts", []) if isinstance(state.get("alerts"), list) else [],
    }


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------
def log_stderr(msg):
    print("[agent] " + msg, file=sys.stderr, flush=True)


def _short(obj, n=300):
    s = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    return s if len(s) <= n else s[:n] + "..."


def first_json_object_block(text):
    """Return the first balanced {...} block from text, or None."""
    if not isinstance(text, str):
        return None
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        start = text.find("{", start + 1)
    return None


def parse_action_message(message):
    """Parse Ollama message.content into {"plan", "actions"}.

    If the model somehow violates the schema, fall back to the first JSON object
    embedded in content. If that also fails, return a local noop queue.
    """
    content = (message or {}).get("content", "")
    raw = content if isinstance(content, str) else json.dumps(content, default=str)

    parsed = None
    try:
        parsed = json.loads(raw)
    except Exception:
        block = first_json_object_block(raw)
        if block is not None:
            try:
                parsed = json.loads(block)
            except Exception:
                parsed = None

    if not isinstance(parsed, dict):
        return {
            "plan": "Model response was not valid JSON; defaulting to noop.",
            "actions": [{"action": "noop", "args": {}}],
            "raw": raw,
            "parse_error": True,
        }

    plan = parsed.get("plan", "")
    actions = parsed.get("actions")
    if not isinstance(plan, str):
        plan = json.dumps(plan, default=str)
    if not isinstance(actions, list):
        return {
            "plan": "Model returned no action queue; defaulting to noop.",
            "actions": [{"action": "noop", "args": {}}],
            "raw": raw,
            "parse_error": True,
        }

    allowed = set(ACTION_SCHEMA["properties"]["actions"]["items"]["properties"]["action"]["enum"])
    normalized = []
    for item in actions:
        if not isinstance(item, dict):
            continue
        action = item.get("action")
        args = item.get("args", {})
        if action not in allowed:
            continue
        if not isinstance(args, dict):
            args = {}
        normalized.append({"action": action, "args": args})

    if not normalized:
        return {
            "plan": "Model returned no valid actions; defaulting to noop.",
            "actions": [{"action": "noop", "args": {}}],
            "raw": raw,
            "parse_error": True,
        }
    return {"plan": plan, "actions": normalized, "raw": raw}


def _arbiter_options(report, pending_forks=None):
    if pending_forks:
        route_goals = [
            str(goal.get("id"))
            for goal in (report.get("goals", []) if isinstance(report, dict) else []) or []
            if isinstance(goal, dict)
            and str(goal.get("id", "")).startswith("demolish_unreachable")
        ]
        options = [{"id": "route-hold", "goal_ids": []}]
        for index, goal_id in enumerate(route_goals[:15], start=1):
            options.append({"id": "route-%s" % index, "goal_ids": [goal_id]})
        return options

    fork = report.get("decision_fork") if isinstance(report, dict) else None
    options = fork.get("options") if isinstance(fork, dict) else None
    if isinstance(options, list) and options:
        return [
            {
                "id": str(option.get("id")),
                "goal_ids": [str(goal_id) for goal_id in option.get("goal_ids", [])],
                **({"cost_logs": option.get("cost_logs")} if option.get("cost_logs") is not None else {}),
            }
            for option in options[:16]
            if isinstance(option, dict) and option.get("id")
        ]

    goal_ids = [
        str(goal.get("id"))
        for goal in (report.get("goals", []) if isinstance(report, dict) else []) or []
        if isinstance(goal, dict) and goal.get("id") and goal.get("id") != "advance_time"
    ]
    fallback = [{"id": "hold", "goal_ids": []}]
    for index, goal_id in enumerate(goal_ids[:15], start=1):
        fallback.append({"id": "goal-%s" % index, "goal_ids": [goal_id]})
    return fallback


def _arbiter_schema(options):
    option_ids = [option["id"] for option in options]
    goal_ids = sorted({goal_id for option in options for goal_id in option["goal_ids"]})
    goal_items = {"type": "string"}
    if goal_ids:
        goal_items["enum"] = goal_ids
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "option_id": {"type": "string", "enum": option_ids},
            "goal_ids": {
                "type": "array",
                "items": goal_items,
                "maxItems": max(len(goal_ids), 1),
            },
            "why": {"type": "string"},
        },
        "required": ["option_id", "goal_ids", "why"],
    }


def _parse_arbiter_message(message):
    content = (message or {}).get("content", "")
    raw = content if isinstance(content, str) else json.dumps(content, default=str)
    try:
        parsed = json.loads(raw)
    except Exception:
        block = first_json_object_block(raw)
        try:
            parsed = json.loads(block) if block else None
        except Exception:
            parsed = None
    return parsed if isinstance(parsed, dict) else {}


def arbitrate_planner_fork(ollama, report, state, pending_forks=None, vision=None):
    """Return one validated choice among planner-enumerated goal subsets."""
    options = _arbiter_options(report, pending_forks=pending_forks)
    by_id = {option["id"]: option for option in options}
    weather = (state.get("weather", {}) if isinstance(state, dict) else {}) or {}
    resources = {}
    for item in (state.get("resources", []) if isinstance(state, dict) else []) or []:
        if isinstance(item, dict) and item.get("good") in ("Log", "Plank", "Water", "Food"):
            resources[item["good"]] = {
                "stored": item.get("stored"),
                "all_stock": item.get("all_stock"),
                "days_remaining": item.get("days_remaining"),
            }
    prompt = {
        "fork": report.get("decision_fork") if isinstance(report, dict) else None,
        "pending_escalations": pending_forks or [],
        "weather": weather,
        "resources": resources,
        "options": options,
    }
    if vision:
        prompt["vision"] = str(vision)[:1200]
    schema = _arbiter_schema(options)
    message = ollama.chat(
        [
            {"role": "system", "content": ARBITER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(prompt, separators=(",", ":"), default=str),
            },
        ],
        schema=schema,
    )
    parsed = _parse_arbiter_message(message)
    option = by_id.get(str(parsed.get("option_id")))
    valid = option is not None and parsed.get("goal_ids") == option.get("goal_ids")
    if not valid:
        option = options[0]
    return {
        "option_id": option["id"],
        "goal_ids": list(option["goal_ids"]),
        "why": str(parsed.get("why") or "deterministic fallback to first option"),
        "valid": valid,
        "raw": (message or {}).get("content", ""),
    }


def _logs_available_for_enforcement(state):
    resources = _resource_by_good(state if isinstance(state, dict) else {})
    logs = resources.get("log", {})
    if not isinstance(logs, dict):
        return None
    if logs.get("stored") is not None:
        return _as_int(logs.get("stored"), 0)
    if logs.get("all_stock") is not None:
        return _as_int(logs.get("all_stock"), 0)
    return None


def _action_spec(action):
    args = action.get("args") if isinstance(action, dict) else {}
    if not isinstance(args, dict):
        return None
    return args.get("spec") or args.get("spec_id") or args.get("building") or args.get("building_type")


def _is_lumberjack_place(action):
    if not isinstance(action, dict) or action.get("action") != "place_building":
        return False
    spec = str(_action_spec(action) or "").split(".")[-1]
    return spec == "LumberjackFlag"


def enforce_actions(actions, report, state, journal_path=None, run_id=None, step=None):
    """Apply deterministic MVP rules to the model queue."""
    enforced = []
    working = list(actions or [])

    if report.get("advance_time_recommended") and not any(a.get("action") == "set_speed" for a in working):
        working.append({"action": "set_speed", "args": {"speed": 3}})
        enforced.append({"rule": "append_set_speed", "reason": "planner_recommended_advance_time"})

    has_lumberjack = any(_is_lumberjack_place(action) for action in working)
    has_cutting = any(action.get("action") == "designate_cutting" for action in working if isinstance(action, dict))
    if has_lumberjack and not has_cutting:
        for index, action in enumerate(working):
            if _is_lumberjack_place(action):
                working.insert(index + 1, {"action": "designate_cutting", "args": {"all": True}})
                enforced.append({"rule": "append_designate_cutting", "reason": "lumberjack_requires_cutting_designation"})
                break

    costs = getattr(planner, "COST_LOGS", None)
    logs_have = _logs_available_for_enforcement(state)
    filtered = []
    if isinstance(costs, dict) and logs_have is not None:
        for action in working:
            if action.get("action") == "place_building":
                spec = _action_spec(action)
                if spec in costs and _as_int(costs.get(spec), 0) > logs_have:
                    enforced.append(
                        {
                            "rule": "drop_unaffordable_place_building",
                            "spec": spec,
                            "cost_logs": _as_int(costs.get(spec), 0),
                            "logs_have": logs_have,
                        }
                    )
                    continue
            filtered.append(action)
    else:
        filtered = working

    if len(filtered) > 8:
        kept = filtered[:8]
        if filtered[-1].get("action") == "set_speed" and not any(a.get("action") == "set_speed" for a in kept):
            kept = filtered[:7] + [filtered[-1]]
        enforced.append({"rule": "cap_actions", "from": len(filtered), "to": len(kept)})
        filtered = kept

    if not filtered:
        filtered = [{"action": "noop", "args": {}}]
        enforced.append({"rule": "empty_queue_to_noop"})

    for item in enforced:
        if journal_path:
            journal_append(journal_path, {"run_id": run_id, "step": step, "event": "enforced", **item})
    return filtered, enforced


def journal_append(path, record):
    """Append one JSON line. Journal failures must never crash the run."""
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        log_stderr("journal write failed: %s" % e)


def bridge_actions_from_model(actions):
    mapped = []
    local_results = []
    for index, action in enumerate(actions or []):
        name = action.get("action") if isinstance(action, dict) else None
        args = action.get("args", {}) if isinstance(action, dict) else {}
        if not isinstance(args, dict):
            args = {}
        if name == "noop":
            local_results.append(
                {
                    "index": index,
                    "action": name,
                    "command": "noop",
                    "ok": True,
                    "result": {"ok": True, "noop": True, "command": "noop"},
                }
            )
            continue
        mapping = ACTION_TO_ACT.get(name)
        if mapping is None:
            local_results.append(
                {
                    "index": index,
                    "action": name,
                    "command": str(name or "unknown"),
                    "ok": False,
                    "result": {"ok": False, "error": "unknown_action", "command": name},
                }
            )
            continue
        command, arg_fn = mapping
        mapped.append(
            {
                "index": index,
                "action": name,
                "command": command,
                "args": arg_fn(args),
            }
        )
    return mapped, local_results


def _single_action_result(item, status, body):
    result = body if isinstance(body, dict) else {"ok": False, "body": body}
    if "command" not in result:
        result = dict(result)
        result["command"] = item["command"]
    return {
        "index": item["index"],
        "action": item["action"],
        "command": item["command"],
        "http_status": status,
        "ok": status == 200 and isinstance(result, dict) and result.get("ok") is True,
        "result": result,
    }


def execute_action_queue(bridge, actions):
    bridge_items, local_results = bridge_actions_from_model(actions)
    if not bridge_items:
        return {"http_status": 200, "body": {"ok": True, "noop": True}, "results": local_results}

    payload_actions = [{"command": item["command"], "args": item["args"]} for item in bridge_items]
    batch_status, batch_body = bridge.act(
        "batch", {"actions": payload_actions, "stop_on_error": False}
    )

    batch_ok = (
        batch_status == 200
        and isinstance(batch_body, dict)
        and isinstance(batch_body.get("results"), list)
    )
    if batch_ok:
        results = list(local_results)
        for item, result in zip(bridge_items, batch_body.get("results") or []):
            body = result if isinstance(result, dict) else {"ok": False, "body": result}
            if "command" not in body:
                body = dict(body)
                body["command"] = item["command"]
            results.append(
                {
                    "index": item["index"],
                    "action": item["action"],
                    "command": item["command"],
                    "http_status": batch_status,
                    "ok": body.get("ok") is True,
                    "result": body,
                }
            )
        results.sort(key=lambda item: item["index"])
        return {"http_status": batch_status, "body": batch_body, "results": results}

    fallback_results = list(local_results)
    for item in bridge_items:
        status, body = bridge.act(item["command"], item["args"])
        fallback_results.append(_single_action_result(item, status, body))
    fallback_results.sort(key=lambda item: item["index"])
    return {
        "http_status": batch_status,
        "body": batch_body,
        "fallback": True,
        "results": fallback_results,
    }


def compact_action_results(results):
    lines = []
    for item in results or []:
        body = item.get("result") if isinstance(item, dict) else {}
        if not isinstance(body, dict):
            body = {}
        command = body.get("command") or item.get("command") or item.get("action") or "?"
        if item.get("ok") is True or body.get("ok") is True:
            status = "ok"
        else:
            error = body.get("error") or body.get("reason") or "error"
            status = "error:%s" % error
        suggestion = body.get("suggestion")
        if suggestion:
            status += " suggestion=%s" % _short(suggestion, 180)
        lines.append("%s -> %s" % (command, status))
    return "Action results:\n" + ("\n".join(lines) if lines else "none")


def _format_observation_line(effects, action_names):
    names = [str(name) for name in action_names or [] if name]
    if "designate_cutting" in names:
        cause = "designate_cutting"
    elif names:
        cause = ", ".join(names[:4])
    else:
        cause = "last action"
    return "OBSERVED last action: %s after %s" % (", ".join(effects), cause)


def _playbook_sort_key(lesson):
    if not isinstance(lesson, dict):
        return (0, "")
    value = str(lesson.get("last_seen_run") or lesson.get("created_run") or lesson.get("id") or "")
    match = re.search(r"(\d+)$", value)
    return (int(match.group(1)) if match else 0, value)


def compact_playbook_block(path=PLAYBOOK_PATH, limit=5):
    if coach_mod is None:
        return ""
    try:
        playbook = coach_mod.load_playbook(Path(path))
    except Exception as e:
        log_stderr("playbook load failed: %s" % e)
        return ""
    lessons = [item for item in playbook.get("lessons", []) if isinstance(item, dict)]
    lessons.sort(key=_playbook_sort_key, reverse=True)
    lessons = lessons[:limit]
    if not lessons:
        return ""
    lines = ["PLAYBOOK lessons from prior runs:"]
    for index, lesson in enumerate(lessons, 1):
        action = str(lesson.get("action") or "").strip()
        trigger = str(lesson.get("trigger") or "").strip()
        outcome = str(lesson.get("outcome") or "").strip()
        line = "%d. trigger=%s | action=%s" % (index, trigger[:140], action[:240])
        if outcome:
            line += " | outcome=%s" % outcome[:160]
        lines.append(line)
    return "\n".join(lines)


def run_learning_loop(journal_path, run_id):
    if metrics_mod is None or coach_mod is None:
        log_stderr("learning loop skipped: metrics or coach module unavailable")
        return
    try:
        journal = metrics_mod.read_journal(Path(journal_path))
        run_metrics = metrics_mod.compute_metrics(journal)
        run_metrics["run_id"] = run_id
        metrics_mod.append_metrics_csv(run_metrics, Path(METRICS_CSV_PATH))
        proposed = coach_mod.analyze(journal, run_metrics)
        if discovery_mod is not None:
            try:
                discovered = discovery_mod.distill(journal_path, run_id)
                proposed.extend(discovered)
                if discovered:
                    log_stderr("discovery distilled lessons=%s" % len(discovered))
            except Exception as e:
                log_stderr("discovery distill failed: %s" % e)
        coach_mod.update_playbook(Path(PLAYBOOK_PATH), proposed, run_id)
        log_stderr(
            "learning loop complete: score=%s lessons=%s"
            % (run_metrics.get("score"), len(proposed))
        )
    except Exception as e:
        log_stderr("learning loop failed: %s" % e)


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
    playbook_block = compact_playbook_block(PLAYBOOK_PATH)
    system_prompt = SYSTEM_PROMPT + (("\n\n" + playbook_block) if playbook_block else "")

    # Preflight: bridge and ollama liveness (warn but continue — they may come up).
    pstatus, pdata = bridge.ping()
    if pstatus == 200:
        log_stderr("bridge OK: %s" % _short(pdata))
    else:
        log_stderr("WARNING: bridge /ping failed (status=%s) — will retry per step." % pstatus)

    journal_append(
        journal_path,
        {"run_id": run_id, "event": "run_start", "config": {k: cfg[k] for k in cfg},
         "max_steps": max_steps, "ping": pdata, "playbook_loaded": bool(playbook_block)},
    )

    # Rolling chat history (after the system prompt). We keep it short.
    history = []
    pending_observation = None
    consecutive_errors = 0
    # Most recent screenshot critique; refreshed every VISION_EVERY steps and
    # reused on the steps in between (a VLM call is slow + swaps the GPU model).
    last_vision = ""
    vision_every = _as_int(cfg.get("VISION_EVERY"), 0)
    vision_model = cfg.get("VISION_MODEL")

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

            if pending_observation and discovery_mod is not None:
                try:
                    effects = discovery_mod.observe_step(
                        pending_observation.get("before") or {},
                        pending_observation.get("action_names") or [],
                        state_summary_for_journal(state),
                    )
                    if effects:
                        observation = _format_observation_line(
                            effects, pending_observation.get("action_names") or []
                        )
                        history.append({"role": "user", "content": observation})
                        journal_append(
                            journal_path,
                            {
                                "run_id": run_id,
                                "step": step,
                                "event": "observed",
                                "effects": effects,
                                "action_names": pending_observation.get("action_names") or [],
                            },
                        )
                    pending_observation = None
                except Exception as e:
                    log_stderr("step %d: discovery observe failed: %s" % (step, e))
                    pending_observation = None

            mstatus, map_data = bridge.map()
            if mstatus != 200:
                map_data = {}
                log_stderr("step %d: /map unavailable (status=%s); continuing" % (step, mstatus))

            rstatus, resources_data = bridge.resources()
            if rstatus != 200 or not isinstance(resources_data, dict) or resources_data.get("ok") is False:
                resources_data = None
                log_stderr("step %d: /resources unavailable (status=%s); continuing" % (step, rstatus))
            resources_block = compact_resources_summary(resources_data, state)

            buildings_detail = ((state.get("buildings") or {}).get("list") if isinstance(state, dict) else None)
            report = planner.plan_report(state, map_data, buildings_detail, resources=resources_data)
            goals = report.get("goals") or []
            top_goal_id = goals[0].get("id") if goals and isinstance(goals[0], dict) else None
            planner_block = report.get("text") or "PLANNER: <unavailable>"

            kb_query = kb_query_for_state(state, top_goal_id)
            kb_block = compact_kb_block(kb_query, k=3)

            # --- 1b. Optional visual critique (every VISION_EVERY steps) ----
            if vision_every > 0 and vision_look is not None and ((step - 1) % vision_every == 0):
                try:
                    v = vision_look(
                        cfg["BRIDGE_URL"], cfg["OLLAMA_URL"], vision_model,
                        width=768, state_hint=state_block,
                    )
                    if v:
                        last_vision = v
                        log_stderr("step %d: vision refreshed (%d chars)" % (step, len(v)))
                except Exception as e:
                    log_stderr("step %d: vision failed: %s" % (step, e))

            # --- 2. Compose user message & call the LLM --------------------
            user_msg = {
                "role": "user",
                "content": (
                    state_block
                    + "\n\n"
                    + resources_block
                    + "\n\n"
                    + planner_block
                    + "\n\n"
                    + kb_block
                    + (("\n\n" + last_vision) if last_vision else "")
                    + "\n\nChoose an ordered queue from the PLANNER goals/candidates. "
                    "Return ONLY the JSON object matching the schema."
                ),
            }
            messages = [{"role": "system", "content": system_prompt}] + history[-HISTORY_LIMIT:] + [user_msg]

            try:
                assistant_msg = ollama.chat(messages)
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

            chosen = parse_action_message(assistant_msg)
            actions, enforced = enforce_actions(
                chosen.get("actions", []),
                report,
                state,
                journal_path=journal_path,
                run_id=run_id,
                step=step,
            )

            # --- 3. Execute the chosen action queue -------------------------
            if chosen.get("parse_error"):
                log_stderr(
                    "step %d: invalid JSON action queue; defaulting to noop. content=%s"
                    % (step, _short(chosen.get("raw", ""), 120))
                )

            exec_result = execute_action_queue(bridge, actions)
            action_results = exec_result.get("results") or []
            pending_observation = {
                "before": state_summary_for_journal(state),
                "action_names": [
                    str(action.get("action"))
                    for action in actions
                    if isinstance(action, dict) and action.get("action")
                ],
            }

            # --- 4. Interpret the action result ----------------------------
            ok_count = sum(1 for item in action_results if item.get("ok") is True)
            err_count = len(action_results) - ok_count
            if action_results:
                consecutive_errors = 0
            else:
                consecutive_errors += 1

            # --- 5. Per-step stdout line -----------------------------------
            status_word = "OK:%s ERR:%s" % (ok_count, err_count)
            print(
                "step %02d/%d | actions=%d -> %s | %s"
                % (
                    step,
                    max_steps,
                    len(actions),
                    status_word,
                    _short(chosen.get("plan", ""), 120),
                ),
                flush=True,
            )

            # --- 6. Journal the full step ----------------------------------
            for result_item in action_results:
                journal_append(
                    journal_path,
                    {
                        "run_id": run_id,
                        "step": step,
                        "event": "action_result",
                        "action": {
                            "name": result_item.get("action"),
                            "command": result_item.get("command"),
                        },
                        "result": {
                            "http_status": result_item.get("http_status", exec_result.get("http_status")),
                            "body": result_item.get("result"),
                        },
                    },
                )

            journal_append(
                journal_path,
                {
                    "run_id": run_id,
                    "step": step,
                    "event": "step",
                    "state": state_summary_for_journal(state),
                    "map": {
                        "http_status": mstatus,
                        "planner": planner_block,
                    },
                    "kb_query": kb_query,
                    "action": {
                        "plan": chosen.get("plan", ""),
                        "actions": actions,
                        "enforced": enforced,
                        "raw": chosen.get("raw", ""),
                    },
                    "result": {
                        "http_status": exec_result.get("http_status"),
                        "body": exec_result.get("body"),
                        "fallback": exec_result.get("fallback", False),
                    },
                },
            )

            # --- 7. Update rolling history (bounded) ------------------------
            # Feed the action + result back so the model has short-term memory.
            history.append(user_msg)
            history.append({
                "role": "assistant",
                "content": json.dumps(
                    {
                        "plan": chosen.get("plan", ""),
                        "actions": actions,
                    },
                    default=str,
                ),
            })
            history.append({
                "role": "user",
                "content": compact_action_results(action_results),
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
    run_learning_loop(journal_path, run_id)
    log_stderr("run complete. journal: %s" % journal_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Timberborn LLM agent loop (v1).")
    parser.add_argument("--bridge-url", default=DEFAULTS["BRIDGE_URL"])
    parser.add_argument("--ollama-url", default=DEFAULTS["OLLAMA_URL"])
    parser.add_argument("--model", default=DEFAULTS["MODEL"])
    parser.add_argument("--max-steps", type=int, default=DEFAULTS["MAX_STEPS"])
    parser.add_argument("--vision-model", default=DEFAULTS["VISION_MODEL"])
    parser.add_argument(
        "--vision-every", type=int, default=DEFAULTS["VISION_EVERY"],
        help="Run a VLM screenshot critique every N steps (0 disables the visual layer).",
    )
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
        "VISION_MODEL": args.vision_model,
        "VISION_EVERY": args.vision_every,
    }
    log_stderr("config: %s run_id=%s" % (cfg, args.run_id))
    run(cfg, args.run_id, args.max_steps)


if __name__ == "__main__":
    main()
