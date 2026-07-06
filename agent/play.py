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
import sys
import time
import traceback
import urllib.error
import urllib.request

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
paths, and future amenities go) and execute toward it ONE JSON action per turn. The
bridge validates every action and returns teaching errors (with a nearest valid
tile) you correct next turn. When unsure of a tile, use the suggested one.

DECISION LOOP (each turn):
1. You are given the current digested colony state (population, resources with
   days_remaining, weather forecast, buildings), a compact map summary, candidate
   placement tiles, and KB rules retrieved for the current situation.
2. Keep/refresh a short layout plan: the next 3-4 buildings AND roughly where they
   go, chosen so survival needs are met AND the colony stays connected and
   expandable (see COLONY LAYOUT).
3. Make exactly ONE JSON action that advances that plan (usually the next building
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

PLACEMENT + PATHING (use the compact map summary; never guess blindly):
  - Always place with z = terrain_height for that exact map tile. Coordinates are
    place_building x=<map x>, y=<map y>, z=<terrain_height>.
  - WaterPump: place ONLY on a clean-water edge: a free LAND tile adjacent to
    water_depth>0 AND contamination==0. NEVER place a pump on/next to badwater
    contamination>0, and never place a pump on an occupied tile.
  - SmallTank, Lodge, SmallWarehouse, LogPile, Inventor: place on flat, dry,
    uncontaminated, unoccupied land near the WaterPump and district center.
  - EfficientFarmhouse and crop support: place near moist/farmable soil; farms
    belong on moist soil with nearby path access.
  - Path: every WaterPump, tank, lodge, storage, farmhouse, gatherer, lumberjack,
    and inventor must connect back to the District Center by Path. If a new
    building is not connected, place Path toward it before unrelated work.
  - Prefer compact rows and short path spines near the district center. Leave
    expansion slots for more tanks and more lodges; do not scatter buildings.
  - If /act returns invalid_placement with suggestion.nearest_valid, your NEXT
    place_building action should reuse exactly that x,y,z,orientation unless it
    would violate clean-water/badwater rules.
  - Prefer the listed candidate tiles from the map summary. If candidates exist
    for the needed building, use one of them exactly.

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
  - The map gives tiles as x=origin.x+col and y=origin.z+row; z is
    terrain_height[index]. BUILD NEAR the district center but use the tile's own
    terrain_height — do NOT use z=0 and do NOT blindly reuse district_center.z.
  - A bad placement returns error "invalid_placement" with
    suggestion.nearest_valid {x,y,z,orientation}. On your NEXT turn, retry
    place_building with exactly that suggested tile.
  - Spec ids are simple names (WaterPump, SmallTank, Lodge, Path, ...) — no faction
    suffix needed.

OUTPUT:
  - Output ONLY one JSON object: {"reasoning": string, "action": string, "args": object}.
  - Make exactly ONE action per turn — do not narrate a plan without acting.
  - If a command returns "not_implemented", pick a different action; that command
    is not live yet in this bridge build.
  - If a command returns "invalid_placement" with suggestion.nearest_valid
    {x,y,z,orientation}, reuse exactly that suggested x,y,z,orientation next turn.
"""


# ---------------------------------------------------------------------------
# Ollama structured-output schema. The model must choose one action object; the
# bridge still validates command-specific args and returns teaching errors.
# ---------------------------------------------------------------------------
ACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reasoning": {"type": "string"},
        "action": {
            "type": "string",
            "enum": ["set_speed", "place_building", "demolish", "save", "noop"],
        },
        "args": {"type": "object"},
    },
    "required": ["reasoning", "action", "args"],
}

# Map each action name -> the /act command string the bridge expects, plus how to
# turn the model's args into the command's args object.
ACTION_TO_ACT = {
    "set_speed": ("set_speed", lambda a: {"speed": a.get("speed", 0)}),
    # Forward the model's raw args; the bridge normalizes spec/spec_id, flat vs
    # nested position{x,y,z}, and nulls, and returns a nearest-valid suggestion.
    "place_building": ("place_building", lambda a: dict(a)),
    "demolish": ("demolish", lambda a: dict(a)),
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

    def chat(self, messages):
        body = {
            "model": self.model,
            "messages": messages,
            "format": ACTION_SCHEMA,
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
    resources = _resource_by_good(state)
    water_days = _as_float((resources.get("water") or {}).get("days_remaining"), 999.0)
    if tanks < max(required_tanks, 1) or water_days < _next_hazard_days(state) + 2.0:
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


def kb_query_for_state(state, next_spec):
    if not isinstance(state, dict):
        return "starter base water food housing path planning"
    alerts = []
    for alert in state.get("alerts", []) or []:
        if isinstance(alert, dict):
            alerts.append(str(alert.get("message") or alert.get("id") or ""))
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
            "next building %s" % next_spec,
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
    """Parse Ollama message.content into {"reasoning", "action", "args"}.

    If the model somehow violates the schema, fall back to the first JSON object
    embedded in content. If that also fails, return a local noop.
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
            "reasoning": "Model response was not valid JSON; defaulting to noop.",
            "action": "noop",
            "args": {},
            "raw": raw,
            "parse_error": True,
        }

    reasoning = parsed.get("reasoning", "")
    action = parsed.get("action")
    args = parsed.get("args", {})
    if not isinstance(reasoning, str):
        reasoning = json.dumps(reasoning, default=str)
    if action not in ACTION_SCHEMA["properties"]["action"]["enum"]:
        return {
            "reasoning": "Model returned an unknown action; defaulting to noop.",
            "action": "noop",
            "args": {},
            "raw": raw,
            "parse_error": True,
        }
    if not isinstance(args, dict):
        args = {}
    return {"reasoning": reasoning, "action": action, "args": args, "raw": raw}


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
            next_spec = infer_next_building_type(state)

            mstatus, map_data = bridge.map()
            if mstatus == 200:
                map_info = compact_map_summary(map_data, state, next_spec)
                map_block = map_info["text"]
            else:
                map_info = {"text": "MAP: <unavailable status=%s>" % mstatus, "candidates": []}
                map_block = map_info["text"]
                log_stderr("step %d: /map unavailable (status=%s); continuing" % (step, mstatus))

            kb_query = kb_query_for_state(state, next_spec)
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
                    + map_block
                    + "\n\n"
                    + kb_block
                    + (("\n\n" + last_vision) if last_vision else "")
                    + "\n\nNEXT_BUILDING_HINT=%s" % next_spec
                    + "\nUse map candidates when placing. Connect placed buildings to "
                    "the district center with Path. Choose the single most urgent "
                    "action now. Return ONLY the JSON object matching the schema."
                ),
            }
            messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history[-HISTORY_LIMIT:] + [user_msg]

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

            # --- 3. Execute the chosen action -------------------------------
            action = chosen["action"]
            args = chosen["args"]
            if chosen.get("parse_error"):
                log_stderr(
                    "step %d: invalid JSON action; defaulting to noop. content=%s"
                    % (step, _short(chosen.get("raw", ""), 120))
                )

            if action == "noop":
                act_status = 200
                act_result = {"ok": True, "noop": True}
            else:
                mapping = ACTION_TO_ACT.get(action)
                if mapping is None:
                    # Should be unreachable after parse_action_message validation.
                    log_stderr("step %d: unknown action '%s' — treating as malformed." % (step, action))
                    act_status, act_result = 0, {"ok": False, "error": "unknown_action", "action": action}
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
            elif act_status == 0 and (isinstance(act_result, dict) and act_result.get("error") == "unknown_action"):
                # Malformed action from the model — count softly, re-prompt via history.
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
                "step %02d/%d | %s(%s) -> %s | %s"
                % (
                    step,
                    max_steps,
                    action,
                    _short(args, 80),
                    status_word,
                    _short(chosen.get("reasoning", ""), 120),
                ),
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
                    "map": {
                        "http_status": mstatus,
                        "summary": map_block,
                        "candidate_count": len(map_info.get("candidates", [])),
                    },
                    "kb_query": kb_query,
                    "action": {
                        "name": action,
                        "args": args,
                        "reasoning": chosen.get("reasoning", ""),
                        "raw": chosen.get("raw", ""),
                    },
                    "result": {"http_status": act_status, "body": act_result},
                },
            )

            # --- 7. Update rolling history (bounded) ------------------------
            # Feed the action + result back so the model has short-term memory.
            history.append(user_msg)
            history.append({
                "role": "assistant",
                "content": json.dumps(
                    {
                        "reasoning": chosen.get("reasoning", ""),
                        "action": action,
                        "args": args,
                    },
                    default=str,
                ),
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
