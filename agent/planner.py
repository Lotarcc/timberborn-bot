"""Deterministic MVP planner for Timberborn bootstrap decisions.

This module is intentionally standalone: stdlib only, no imports from play.py.
It turns bridge state/map facts into an ordered rule checklist plus reachable
placement candidates so the LLM chooses among valid options instead of guessing.
"""

from collections import deque


# Log costs used by the deterministic affordability gate.
# Path is free for the MVP planner. Verify against the live blueprint dump if
# Timberborn changes faction/building costs.
COST_LOGS = {
    "Path": 0,
    "LumberjackFlag": 0,
    "GathererFlag": 0,
    "WaterPump": 12,
    "SmallTank": 15,
    "Lodge": 12,
    "EfficientFarmhouse": 25,
    "SmallWarehouse": 18,  # v-check: verify against /blueprints.
    "Inventor": 30,  # v-check: verify against /blueprints.
    "Dam": 20,  # Per tile.
    "ForesterFlag": 0,  # v-check: verify against /blueprints.
}


GOAL_SPECS = {
    "build_lumberjack": "LumberjackFlag",
    "build_water_pump": "WaterPump",
    "build_water_storage": "SmallTank",
    "build_gatherer": "GathererFlag",
    "build_farm": "EfficientFarmhouse",
    "build_lodge": "Lodge",
    "build_warehouse": "SmallWarehouse",
    "build_inventor": "Inventor",
    "build_path": "Path",
}


DIRECTIONS = ((0, -1, "North"), (1, 0, "East"), (0, 1, "South"), (-1, 0, "West"))


def analyze(state, map_data, buildings_detail=None):
    """Return an ordered deterministic goal checklist.

    Missing fields are treated as absent/zero so partial bridge snapshots still
    produce a useful checklist instead of raising.
    """
    state = state if isinstance(state, dict) else {}
    goals = []

    for building in _building_details(state, buildings_detail):
        if isinstance(building, dict) and building.get("reachable") is False:
            coords = _coords(building)
            goals.append(
                _goal(
                    "demolish_unreachable",
                    "building is not path-connected to the district center",
                    spec=building.get("spec") or building.get("spec_id"),
                    coords=coords,
                )
            )

    if _building_count(state, "LumberjackFlag") <= 0:
        goals.append(
            _goal(
                "build_lumberjack",
                "no LumberjackFlag placed; it is free and starts wild-tree log production",
                spec="LumberjackFlag",
            )
        )

    if _building_count(state, "WaterPump") <= 0:
        goals.append(
            _goal(
                "build_water_pump",
                "no WaterPump; drinkable water cannot be produced",
                spec="WaterPump",
                logs_have=_logs_available(state),
            )
        )

    if _resource_days(state, "Water") < _hazard_buffer_days(state):
        goals.append(
            _goal(
                "build_water_storage",
                "water days remaining is below next hazard duration plus 2-day buffer",
                spec="SmallTank",
                logs_have=_logs_available(state),
            )
        )

    if not _has_food_production(state):
        goals.append(
            _goal(
                "build_gatherer",
                "no food production; GathererFlag is free immediate wild food",
                spec="GathererFlag",
            )
        )

    if _resource_days(state, "Food", fallback_goods=("Berries", "Carrot", "GrilledPotato")) < _hazard_buffer_days(state):
        goals.append(
            _goal(
                "build_farm",
                "food days remaining is below next hazard duration plus 2-day buffer",
                spec="EfficientFarmhouse",
                logs_have=_logs_available(state),
            )
        )

    homeless = _as_int(((state.get("population") or {}).get("homeless")), 0)
    if homeless > 0:
        goals.append(
            _goal(
                "build_lodge",
                "%s homeless beavers need beds" % homeless,
                spec="Lodge",
                logs_have=_logs_available(state),
            )
        )

    if not _has_storage(state):
        goals.append(
            _goal(
                "build_warehouse",
                "no storage building; goods need central buffer space",
                spec="SmallWarehouse",
                logs_have=_logs_available(state),
            )
        )

    if _building_count(state, "Inventor") <= 0:
        goals.append(
            _goal(
                "build_inventor",
                "science comes after storage and survival bootstrap",
                spec="Inventor",
                logs_have=_logs_available(state),
            )
        )

    if _sites_under_construction(state) and _logs_available(state) > 0 and not _has_urgent_unblocked_goal(goals):
        goals.append(
            {
                "id": "advance_time",
                "why": "construction sites exist and logs are available; let haulers/builders work",
                "spec": "set_speed",
            }
        )

    return goals


def reachable_tiles(map_data, start_xy):
    """Return land tiles reachable by a simple 4-neighbor BFS from start_xy.

    This approximates stairs by allowing movement only between dry land tiles
    whose terrain heights differ by at most 1. The bridge/game validator remains
    authoritative for real pathing.
    """
    arrays = _map_arrays(map_data)
    if arrays is None:
        return set()

    sx, sy = _xy_pair(start_xy)
    start = _tile_at_xy(arrays, sx, sy)
    if start is None or not _is_land(start):
        return set()

    seen = {(start["x"], start["y"])}
    queue = deque([start])

    while queue:
        tile = queue.popleft()
        for dx, dy, _direction in DIRECTIONS:
            other = _tile(arrays, tile["col"] + dx, tile["row"] + dy)
            if other is None or not _is_land(other):
                continue
            key = (other["x"], other["y"])
            if key in seen:
                continue
            if abs(_as_float(other["z"]) - _as_float(tile["z"])) > 1:
                continue
            seen.add(key)
            queue.append(other)

    return seen


def candidates_for(goal, state, map_data, k=6):
    """Return reachable candidate build tiles for a planner goal or spec id."""
    arrays = _map_arrays(map_data)
    if arrays is None:
        return []

    state = state if isinstance(state, dict) else {}
    dc = _district_center(state, map_data, arrays)
    reachable = reachable_tiles(map_data, (dc["x"], dc["y"]))
    if not reachable:
        return []

    goal_id = goal.get("id") if isinstance(goal, dict) else str(goal)
    spec = _goal_spec(goal)
    tiles = []

    for row in range(arrays["height"]):
        for col in range(arrays["width"]):
            tile = _tile(arrays, col, row)
            if tile is None:
                continue
            if (tile["x"], tile["y"]) not in reachable:
                continue
            if tile["occupied"] or tile["contamination"] > 0 or not _is_land(tile):
                continue

            candidate = None
            if spec == "WaterPump":
                candidate = _water_pump_candidate(arrays, tile)
            elif spec == "EfficientFarmhouse":
                if tile["moist"] == 1:
                    candidate = _candidate(tile, "moist=1")
            elif spec == "Path":
                if _distance(tile, dc) <= 6:
                    candidate = _candidate(tile, "near district center")
            elif spec in ("SmallTank", "Lodge", "SmallWarehouse", "Inventor", "LumberjackFlag", "GathererFlag"):
                same_height = _same_height_dry_neighbors(arrays, tile)
                if same_height >= 2:
                    candidate = _candidate(tile, "flat dry land; %s same-height dry neighbors" % same_height)
            elif goal_id == "demolish_unreachable":
                candidate = None

            if candidate is not None:
                tiles.append(candidate)

    tiles.sort(key=lambda item: (_distance(item, dc), item["y"], item["x"]))
    return tiles[: max(_as_int(k, 6), 0)]


def plan_report(state, map_data, buildings_detail=None):
    """Return planner data plus a compact prompt block for the LLM."""
    goals = analyze(state, map_data, buildings_detail=buildings_detail)
    candidates_by_goal = {}
    alerts_local = []
    advance_time = False

    for goal in goals:
        goal_id = goal.get("id")
        if goal_id:
            candidates_by_goal[goal_id] = candidates_for(goal, state, map_data, k=6)
        if goal.get("blocked_by"):
            alerts_local.append({"id": "blocked_by_resources", "goal": goal_id, "message": goal["blocked_by"]})
            if "advance_time" in goal["blocked_by"]:
                advance_time = True
        if goal_id == "demolish_unreachable":
            alerts_local.append({"id": "building_unreachable", "message": goal.get("why", "")})
        if goal_id == "advance_time":
            advance_time = True

    if _sites_under_construction(state):
        alerts_local.append({"id": "sites_under_construction", "message": "construction sites exist"})
        advance_time = True

    text = _report_text(goals, candidates_by_goal, advance_time)
    return {
        "goals": goals,
        "candidates_by_goal": candidates_by_goal,
        "alerts_local": alerts_local,
        "advance_time_recommended": advance_time,
        "text": text,
    }


def _goal(goal_id, why, spec=None, logs_have=None, coords=None):
    item = {"id": goal_id, "why": why}
    if spec:
        item["spec"] = spec
        item["cost_logs"] = COST_LOGS.get(spec, 0)
        if logs_have is not None and item["cost_logs"] > logs_have:
            item["blocked_by"] = "need %s logs, have %s -> advance_time" % (item["cost_logs"], logs_have)
    if coords:
        item["coords"] = coords
    return item


def _report_text(goals, candidates_by_goal, advance_time):
    lines = ["PLANNER"]
    for index, goal in enumerate(goals, 1):
        bits = ["%s. %s: %s" % (index, goal.get("id", "?"), goal.get("why", ""))]
        if goal.get("blocked_by"):
            bits.append("blocked_by=%s" % goal["blocked_by"])
        if goal.get("coords"):
            bits.append("coords=%s" % _format_coords(goal["coords"]))
        candidates = candidates_by_goal.get(goal.get("id")) or []
        if candidates:
            bits.append("candidates=%s" % _format_candidates(candidates))
        lines.append(" | ".join(bits))
    if advance_time:
        lines.append("ADVANCE TIME (set_speed 3, then re-check)")
    return "\n".join(lines[:25])


def _format_candidates(candidates):
    return "; ".join("(%s,%s,%s%s)" % (item["x"], item["y"], item["z"], ("," + item["orientation"]) if item.get("orientation") else "") for item in candidates[:6])


def _format_coords(coords):
    if isinstance(coords, dict):
        return "(%s,%s,%s)" % (coords.get("x", "?"), coords.get("y", coords.get("z", "?")), coords.get("z", "?"))
    return str(coords)


def _goal_spec(goal):
    if isinstance(goal, dict):
        if goal.get("spec"):
            return goal.get("spec")
        return GOAL_SPECS.get(goal.get("id"), goal.get("id"))
    return GOAL_SPECS.get(str(goal), str(goal))


def _has_urgent_unblocked_goal(goals):
    urgent = {
        "demolish_unreachable",
        "build_lumberjack",
        "build_water_pump",
        "build_water_storage",
        "build_gatherer",
        "build_farm",
        "build_lodge",
    }
    for goal in goals:
        if goal.get("id") in urgent and not goal.get("blocked_by"):
            return True
    return False


def _building_details(state, buildings_detail):
    if buildings_detail is not None:
        return buildings_detail or []
    buildings = (state.get("buildings") or {}) if isinstance(state, dict) else {}
    details = buildings.get("detail")
    if details is None:
        details = buildings.get("details")
    if details is None:
        details = state.get("buildings_detail")
    return details or []


def _coords(building):
    if not isinstance(building, dict):
        return None
    for key in ("coords", "position", "tile"):
        value = building.get(key)
        if isinstance(value, dict):
            return {"x": value.get("x"), "y": value.get("y", value.get("z")), "z": value.get("z")}
    if "x" in building:
        return {"x": building.get("x"), "y": building.get("y", building.get("z")), "z": building.get("z")}
    return None


def _has_food_production(state):
    return any(_building_count(state, spec) > 0 for spec in ("GathererFlag", "EfficientFarmhouse", "Farmhouse"))


def _has_storage(state):
    return any(_building_count(state, spec) > 0 for spec in ("SmallWarehouse", "MediumWarehouse", "LargeWarehouse"))


def _sites_under_construction(state):
    value = ((state.get("buildings") or {}) if isinstance(state, dict) else {}).get("under_construction")
    if isinstance(value, list):
        return len(value) > 0
    if isinstance(value, dict):
        return bool(value)
    return _as_int(value, 0) > 0


def _building_count(state, spec):
    counts = (((state or {}).get("buildings") or {}).get("counts") or {})
    lowered = str(spec).lower()
    for key, value in counts.items():
        if str(key).lower() == lowered:
            return _as_int(value, 0)
    return 0


def _logs_available(state):
    resource = _resource(state, "Log")
    if not resource:
        return 0
    if resource.get("all_stock") is not None:
        return _as_int(resource.get("all_stock"), 0)
    return _as_int(resource.get("stored"), 0)


def _resource_days(state, good, fallback_goods=()):
    goods = (good,) + tuple(fallback_goods)
    for name in goods:
        resource = _resource(state, name)
        if resource:
            return _as_float(resource.get("days_remaining"), 0.0)
    return 0.0


def _resource(state, good):
    target = str(good).lower()
    for item in (state.get("resources") or []) if isinstance(state, dict) else []:
        if isinstance(item, dict) and str(item.get("good", "")).lower() == target:
            return item
    return None


def _hazard_buffer_days(state):
    weather = (state.get("weather") or {}) if isinstance(state, dict) else {}
    next_weather = weather.get("next") or {}
    return _as_float(next_weather.get("duration_days"), 0.0) + 2.0


def _map_arrays(map_data):
    if not isinstance(map_data, dict):
        return None
    width = _as_int(map_data.get("width"), 0)
    height = _as_int(map_data.get("height"), 0)
    if width <= 0 or height <= 0:
        return None
    total = width * height
    terrain = map_data.get("terrain_height") or []
    if len(terrain) < total:
        return None
    origin = map_data.get("origin") or {}
    moist = map_data.get("moist")
    if moist is None:
        moist = map_data.get("moisture")
    return {
        "origin_x": _as_int(origin.get("x"), 0),
        "origin_y": _as_int(origin.get("z", origin.get("y", 0)), 0),
        "width": width,
        "height": height,
        "terrain": terrain,
        "water": map_data.get("water_depth") or [],
        "contamination": map_data.get("contamination") or [],
        "moist": moist or [],
        "occupied": map_data.get("occupied") or [],
    }


def _district_center(state, map_data, arrays):
    dc = (state.get("district_center") or {}) if isinstance(state, dict) else {}
    if not dc:
        dc = (map_data.get("district_center") or {}) if isinstance(map_data, dict) else {}
    return {
        "x": _as_int(dc.get("x"), arrays["origin_x"] + arrays["width"] // 2),
        "y": _as_int(dc.get("y", dc.get("z")), arrays["origin_y"] + arrays["height"] // 2),
        "z": _as_int(dc.get("z"), 0),
    }


def _tile(arrays, col, row):
    if col < 0 or row < 0 or col >= arrays["width"] or row >= arrays["height"]:
        return None
    index = row * arrays["width"] + col
    return {
        "x": arrays["origin_x"] + col,
        "y": arrays["origin_y"] + row,
        "z": _array_value(arrays["terrain"], index, 0),
        "water": _as_float(_array_value(arrays["water"], index, 0), 0.0),
        "contamination": _as_float(_array_value(arrays["contamination"], index, 0), 0.0),
        "moist": _as_int(_array_value(arrays["moist"], index, 0), 0),
        "occupied": _as_int(_array_value(arrays["occupied"], index, 0), 0),
        "col": col,
        "row": row,
    }


def _tile_at_xy(arrays, x, y):
    return _tile(arrays, _as_int(x) - arrays["origin_x"], _as_int(y) - arrays["origin_y"])


def _water_pump_candidate(arrays, tile):
    clean = []
    badwater = []
    for dx, dy, direction in DIRECTIONS:
        other = _tile(arrays, tile["col"] + dx, tile["row"] + dy)
        if other is None or other["water"] <= 0:
            continue
        if other["contamination"] > 0:
            badwater.append(direction)
        else:
            clean.append(direction)
    if clean and not badwater:
        return _candidate(tile, "clean water edge", orientation=clean[0])
    return None


def _same_height_dry_neighbors(arrays, tile):
    count = 0
    for dx, dy, _direction in DIRECTIONS:
        other = _tile(arrays, tile["col"] + dx, tile["row"] + dy)
        if other is None:
            continue
        if other["z"] == tile["z"] and _is_land(other) and other["contamination"] <= 0 and other["occupied"] == 0:
            count += 1
    return count


def _candidate(tile, why, orientation=None):
    result = {"x": tile["x"], "y": tile["y"], "z": tile["z"], "why": why}
    if orientation:
        result["orientation"] = orientation
    return result


def _is_land(tile):
    return _as_float(tile.get("water"), 0.0) <= 0


def _distance(tile, dc):
    return abs(_as_int(tile["x"]) - _as_int(dc["x"])) + abs(_as_int(tile["y"]) - _as_int(dc["y"]))


def _xy_pair(value):
    if isinstance(value, dict):
        return value.get("x", 0), value.get("y", value.get("z", 0))
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        return value[0], value[1]
    return 0, 0


def _array_value(values, index, default=0):
    if isinstance(values, list) and 0 <= index < len(values):
        return values[index]
    return default


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
