#!/usr/bin/env python3
"""Controller-first Timberborn player loop.

The deterministic controller executes planner-enumerated work and advances time.
The LLM is reserved for explicit planner forks and never chooses coordinates.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from collections import Counter


AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
if AGENT_DIR not in sys.path:
    sys.path.insert(0, AGENT_DIR)

try:
    import planner
    import play
except Exception:  # pragma: no cover - import path depends on invocation style
    from agent import planner, play  # type: ignore

try:
    import discovery as discovery_mod
except Exception:  # pragma: no cover - learning is best-effort
    try:
        from agent import discovery as discovery_mod  # type: ignore
    except Exception:  # pragma: no cover
        discovery_mod = None


MAX_BATCH_ACTIONS = 16
MAX_ACTIVE_SITES = 6
DEFAULT_RUN_SPEED = 3
DEFAULT_MAX_POLLS = 24
DEFAULT_POLL_INTERVAL = 0.25
DEFAULT_MAX_ADVANCE_DAYS = 1.0
DEFAULT_HAZARD_MARGIN_DAYS = 0.25

KNOWN_ALERT_IDS = {
    "no_log_production",
    "no_water_pump",
    "water_understocked",
    "water_understocked_for_forecast",
    "no_food_production",
    "homeless",
    "logs_zero_sites_waiting",
    "sites_in_progress",
    "building_unreachable",
}

DISCRETIONARY_GOALS = {
    "build_warehouse",
    "build_inventor",
    "build_forester",
}

WORKPLACE_GOALS = {
    "build_lumberjack",
    "build_water_pump",
    "build_gatherer",
    "build_farm",
    "build_inventor",
    "build_forester",
}

DISCRETIONARY_WORKPLACE_GOALS = {"build_inventor", "build_forester"}


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


def _resource_items(state):
    return (state.get("resources", []) if isinstance(state, dict) else []) or []


def _resource_item(state, good):
    wanted = str(good).lower()
    aliases = {wanted, wanted.rstrip("s"), wanted + "s"}
    for item in _resource_items(state):
        if not isinstance(item, dict):
            continue
        name = str(item.get("good", "")).lower()
        if name in aliases or name.rstrip("s") == wanted.rstrip("s"):
            return item
    return {}


def _resource_stock(state, good):
    item = _resource_item(state, good)
    if item.get("stored") is not None:
        return _as_int(item.get("stored"), 0)
    return _as_int(item.get("all_stock"), 0)


def _resource_days(state, good):
    return _as_float(_resource_item(state, good).get("days_remaining"), -1.0)


def _goal_satisfied(goal):
    if goal.get("satisfied") is True:
        return True
    if goal.get("current_count") is not None and goal.get("target_count") is not None:
        return _as_int(goal.get("current_count")) >= _as_int(goal.get("target_count"))
    return False


def _sites_under_construction(state):
    value = ((state.get("buildings") or {}).get("under_construction") if isinstance(state, dict) else 0)
    if isinstance(value, (list, dict)):
        return len(value)
    return max(_as_int(value, 0), 0)


def _dependency_ready(goal_id, goals_by_id, selected):
    for dependency in getattr(planner, "GOAL_DEPENDENCIES", {}).get(goal_id, ()):
        if dependency in goals_by_id and dependency not in selected:
            return False
    return True


def _candidate_for_goal(candidates, reserved_tiles):
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        tile = (
            _as_int(candidate.get("x")),
            _as_int(candidate.get("y", candidate.get("z"))),
            _as_int(candidate.get("z")),
        )
        overlaps = any(
            abs(tile[0] - reserved[0]) <= 2
            and abs(tile[1] - reserved[1]) <= 2
            and abs(tile[2] - reserved[2]) <= 1
            for reserved in reserved_tiles
        )
        if not overlaps:
            return candidate, tile
    return None, None


def build_safe_ready_frontier(report, state, selected_goal_ids=None, max_actions=MAX_BATCH_ACTIONS):
    """Build one deterministic, cumulatively affordable planner action batch."""
    report = report if isinstance(report, dict) else {}
    goals = [goal for goal in report.get("goals", []) or [] if isinstance(goal, dict)]
    goals_by_id = {goal.get("id"): goal for goal in goals if goal.get("id")}
    chosen_by_arbiter = set(selected_goal_ids) if selected_goal_ids is not None else None
    candidates_by_goal = report.get("candidates_by_goal", {}) or {}
    followups_by_goal = report.get("followups", {}) or {}

    budgets = {
        "Log": _resource_stock(state, "Log"),
        "Plank": _resource_stock(state, "Plank"),
    }
    reserved = {"Log": 0, "Plank": 0}
    reserved_tiles = set()
    selected = []
    actions = []
    skipped = []
    existing_sites = _sites_under_construction(state)
    placements_reserved = 0
    workers_reserved = 0
    population = (state.get("population", {}) if isinstance(state, dict) else {}) or {}
    unemployed = population.get("unemployed")
    unemployed = _as_int(unemployed, 0) if unemployed is not None else None
    critical_unstaffed = _critical_unstaffed(state)

    for goal in goals:
        goal_id = goal.get("id")
        if not goal_id or _goal_satisfied(goal):
            skipped.append({"goal_id": goal_id, "reason": "satisfied"})
            continue
        spec = goal.get("spec")
        candidates = candidates_by_goal.get(goal_id) or []
        if not spec or not candidates:
            skipped.append({"goal_id": goal_id, "reason": "no_candidate"})
            continue
        cost_logs = max(_as_int(goal.get("cost_logs"), 0), 0)
        cost_planks = max(_as_int(goal.get("cost_planks"), 0), 0)
        is_free = goal.get("free") is True or (cost_logs == 0 and cost_planks == 0)
        if chosen_by_arbiter is not None and not is_free and goal_id not in chosen_by_arbiter:
            skipped.append({"goal_id": goal_id, "reason": "not_selected_at_fork"})
            continue
        if existing_sites > 0 and goal_id in DISCRETIONARY_GOALS:
            skipped.append({"goal_id": goal_id, "reason": "builder_throughput_reserved"})
            continue
        if existing_sites + placements_reserved >= MAX_ACTIVE_SITES:
            skipped.append({"goal_id": goal_id, "reason": "active_site_cap"})
            continue
        if goal_id in WORKPLACE_GOALS and (
            (unemployed is not None and workers_reserved >= unemployed)
            or (goal_id in DISCRETIONARY_WORKPLACE_GOALS and critical_unstaffed)
        ):
            skipped.append({"goal_id": goal_id, "reason": "worker_capacity_reserved"})
            continue
        if goal.get("affordable") is False and not is_free:
            skipped.append({"goal_id": goal_id, "reason": "individually_unaffordable"})
            continue
        if not _dependency_ready(goal_id, goals_by_id, set(selected)):
            skipped.append({"goal_id": goal_id, "reason": "dependency_not_ready"})
            continue
        if reserved["Log"] + cost_logs > budgets["Log"]:
            skipped.append({"goal_id": goal_id, "reason": "cumulative_log_budget"})
            continue
        if reserved["Plank"] + cost_planks > budgets["Plank"]:
            skipped.append({"goal_id": goal_id, "reason": "cumulative_plank_budget"})
            continue

        candidate, tile = _candidate_for_goal(candidates, reserved_tiles)
        if candidate is None:
            skipped.append({"goal_id": goal_id, "reason": "candidate_overlap"})
            continue
        followups = [
            item for item in followups_by_goal.get(goal_id, []) or []
            if isinstance(item, dict) and item.get("action")
        ]
        needed_slots = 1 + len(followups)
        if len(actions) + needed_slots > min(_as_int(max_actions, MAX_BATCH_ACTIONS), MAX_BATCH_ACTIONS):
            skipped.append({"goal_id": goal_id, "reason": "batch_cap"})
            continue

        args = {
            "spec": spec,
            "x": candidate.get("x"),
            "y": candidate.get("y", candidate.get("z")),
            "z": candidate.get("z"),
        }
        if candidate.get("orientation"):
            args["orientation"] = candidate["orientation"]
        actions.append(
            {
                "action": "place_building",
                "args": args,
                "goal_id": goal_id,
            }
        )
        for followup in followups:
            actions.append(
                {
                    "action": followup["action"],
                    "args": dict(followup.get("args") or {}),
                    "goal_id": goal_id,
                }
            )
        selected.append(goal_id)
        reserved["Log"] += cost_logs
        reserved["Plank"] += cost_planks
        reserved_tiles.add(tile)
        placements_reserved += 1
        if goal_id in WORKPLACE_GOALS:
            workers_reserved += 1

    return {
        "actions": actions,
        "goal_ids": selected,
        "reserved": reserved,
        "available": budgets,
        "skipped": skipped,
    }


def _novel_alerts(state):
    novel = []
    for alert in (state.get("alerts", []) if isinstance(state, dict) else []) or []:
        alert_id = alert.get("id") if isinstance(alert, dict) else str(alert)
        if alert_id and str(alert_id).lower() not in KNOWN_ALERT_IDS:
            novel.append(str(alert_id))
    return novel


def _risk_tradeoff(report, state):
    weather = (state.get("weather", {}) if isinstance(state, dict) else {}) or {}
    next_weather = weather.get("next", {}) or {}
    duration = _as_float(next_weather.get("duration_days"), 0.0)
    in_days = _as_float(next_weather.get("in_days"), 999.0)
    water_days = _resource_days(state, "Water")
    if water_days < 0 or water_days >= duration + 2.0 or in_days > duration + 2.0:
        return False
    candidates = report.get("candidates_by_goal", {}) if isinstance(report, dict) else {}
    return any(
        goal.get("id") in DISCRETIONARY_GOALS
        and goal.get("affordable") is True
        and candidates.get(goal.get("id"))
        for goal in report.get("goals", []) or []
        if isinstance(goal, dict)
    )


def needs_llm(report, state=None, pending_forks=None, handled_alert_ids=None):
    """True only for an explicit fork, risk trade-off, or unhandled alert."""
    if pending_forks:
        return True
    if isinstance(report, dict) and report.get("decision_fork"):
        return True
    if any(
        isinstance(goal, dict)
        and str(goal.get("id", "")).startswith("demolish_unreachable")
        for goal in (report.get("goals", []) if isinstance(report, dict) else []) or []
    ):
        return True
    novel = set(_novel_alerts(state or {})) - set(handled_alert_ids or ())
    if novel:
        return True
    return _risk_tradeoff(report or {}, state or {})


def _alert_signature(state):
    signature = Counter()
    for alert in (state.get("alerts", []) if isinstance(state, dict) else []) or []:
        if isinstance(alert, dict):
            key = (
                str(alert.get("id", "")),
                str(alert.get("severity", "")),
                str(alert.get("message", "")),
            )
        else:
            key = (str(alert), "", "")
        signature[key] += 1
    return signature


def _population_signature(state):
    population = (state.get("population", {}) if isinstance(state, dict) else {}) or {}
    return tuple(
        _as_int(population.get(key), 0)
        for key in ("total", "adults", "kits", "homeless", "free_beds", "unemployed")
    )


def _critical_unstaffed(state):
    buildings = (state.get("buildings", {}) if isinstance(state, dict) else {}) or {}
    explicit = buildings.get("unstaffed") or []
    result = {str(item) for item in explicit}
    critical_specs = {"WaterPump", "GathererFlag", "EfficientFarmHouse", "Farmhouse"}
    for building in buildings.get("list", []) or []:
        if not isinstance(building, dict):
            continue
        spec = str(building.get("spec") or "").split(".")[0]
        if spec not in critical_specs:
            continue
        if str(building.get("status", "finished")).lower() in ("paused", "site"):
            continue
        if _as_int(building.get("max_workers"), -1) > 0 and _as_int(building.get("workers"), -1) == 0:
            result.add("%s@%s,%s,%s" % (spec, building.get("x"), building.get("y"), building.get("z")))
    return result


def wake_reason(initial_state, current_state, thresholds=None, hazard_margin_days=DEFAULT_HAZARD_MARGIN_DAYS,
                coast=False):
    """Why the bulk-advance should pause. When coast=True (idle fast-forward: the play
    loop has nothing to build and is only waiting for logs/breeding), the minor-churn
    wakes (alert flicker, staffing shuffles) are SKIPPED so the game covers multiple
    game-days per cycle instead of stopping every few game-minutes; the crisis wakes
    (resource/buffer thresholds, weather + hazard, population change) are ALWAYS kept so
    coasting can never run through a thirst/hunger death or into a drought unprepared."""
    if not coast and _alert_signature(initial_state) != _alert_signature(current_state):
        return "alerts_changed"

    for good, threshold in (thresholds or {}).items():
        before = _resource_stock(initial_state, good)
        after = _resource_stock(current_state, good)
        target = _as_int(threshold, 0)
        if before < target <= after:
            return "resource_threshold:%s" % good

    current_weather = (current_state.get("weather", {}) or {}) if isinstance(current_state, dict) else {}
    initial_weather = (initial_state.get("weather", {}) or {}) if isinstance(initial_state, dict) else {}
    if str(current_weather.get("current")) != str(initial_weather.get("current")):
        return "weather_transition"
    current_next = current_weather.get("next", {}) or {}
    initial_next = initial_weather.get("next", {}) or {}
    next_in = _as_float(current_next.get("in_days"), 999.0)
    initial_in = _as_float(initial_next.get("in_days"), 999.0)
    if next_in <= hazard_margin_days < initial_in or initial_in <= hazard_margin_days:
        return "hazard_imminent"

    hazard_buffer = _as_float(current_next.get("duration_days"), 0.0) + 2.0
    for good in ("Water", "Food"):
        before_days = _resource_days(initial_state, good)
        after_days = _resource_days(current_state, good)
        if before_days < 0 or after_days < 0:
            continue
        if (before_days < hazard_buffer <= after_days) or (
            before_days >= hazard_buffer > after_days
        ):
            return "buffer_threshold:%s" % good

    if _population_signature(initial_state) != _population_signature(current_state):
        return "population_transition"
    if not coast and _critical_unstaffed(initial_state) != _critical_unstaffed(current_state):
        return "staffing_transition"
    return None


def bulk_advance_until_wake(
    bridge,
    initial_state,
    thresholds=None,
    run_speed=DEFAULT_RUN_SPEED,
    poll_interval=DEFAULT_POLL_INTERVAL,
    max_polls=DEFAULT_MAX_POLLS,
    max_advance_days=DEFAULT_MAX_ADVANCE_DAYS,
    hazard_margin_days=DEFAULT_HAZARD_MARGIN_DAYS,
    coast=False,
):
    """Run time in bulk, pause on a decision threshold, then read stable state.

    coast=True is the idle fast-forward: the play loop has nothing to build and is
    only waiting for logs to accrue / beavers to breed, so we run faster (game speed
    up to 12) and pass coast through to wake_reason, which then ignores minor churn
    (alerts/staffing) and only stops for a real crisis or a population change. The
    hazard-boundary guard below (which uses max_advance_days) still fires first, so a
    longer coast never advances toward an imminent drought."""
    initial_next_in = _as_float(
        ((((initial_state.get("weather") or {}).get("next") or {}).get("in_days")))
        if isinstance(initial_state, dict)
        else None,
        999.0,
    )
    if initial_next_in <= hazard_margin_days + max_advance_days:
        pause_status, pause_body = bridge.act("set_speed", {"speed": 0})
        return {
            "state": initial_state,
            "reason": "hazard_imminent",
            "polls": 0,
            "paused": pause_status == 200
            and isinstance(pause_body, dict)
            and pause_body.get("ok") is True,
        }

    immediate = wake_reason(
        initial_state, initial_state, thresholds, hazard_margin_days=hazard_margin_days, coast=coast
    )
    if immediate:
        return {"state": initial_state, "reason": immediate, "polls": 0, "paused": True}

    reason = "poll_cap"
    polls = 0
    last_state = initial_state
    started = False
    pause_result = None
    safe_run_speed = min(max(_as_int(run_speed, DEFAULT_RUN_SPEED), 1), 12 if coast else DEFAULT_RUN_SPEED)
    safe_poll_interval = min(max(_as_float(poll_interval, DEFAULT_POLL_INTERVAL), 0.0), 0.25)
    status, body = bridge.act("set_speed", {"speed": safe_run_speed})
    if status != 200 or not isinstance(body, dict) or body.get("ok") is not True:
        return {"state": initial_state, "reason": "set_speed_failed", "polls": 0, "paused": False}
    started = True
    try:
        for polls in range(1, max(_as_int(max_polls, DEFAULT_MAX_POLLS), 1) + 1):
            if safe_poll_interval > 0:
                time.sleep(safe_poll_interval)
            state_status, current = bridge.state()
            if state_status != 200 or not isinstance(current, dict):
                reason = "state_poll_failed"
                break
            last_state = current
            reason = wake_reason(
                initial_state,
                current,
                thresholds,
                hazard_margin_days=hazard_margin_days,
                coast=coast,
            )
            if reason:
                break
            initial_in = _as_float((((initial_state.get("weather") or {}).get("next") or {}).get("in_days")), 999.0)
            current_in = _as_float((((current.get("weather") or {}).get("next") or {}).get("in_days")), initial_in)
            if initial_in - current_in >= max_advance_days:
                reason = "advance_cap"
                break
        else:
            reason = "poll_cap"
    finally:
        if started:
            pause_result = bridge.act("set_speed", {"speed": 0})

    stable_status, stable_state = bridge.state()
    if stable_status == 200 and isinstance(stable_state, dict):
        last_state = stable_state
    paused = bool(
        pause_result
        and pause_result[0] == 200
        and isinstance(pause_result[1], dict)
        and pause_result[1].get("ok") is True
    )
    return {"state": last_state, "reason": reason, "polls": polls, "paused": paused}


def _building_at(state, coords):
    buildings = ((state.get("buildings") or {}).get("list") if isinstance(state, dict) else None) or []
    for building in buildings:
        if not isinstance(building, dict):
            continue
        actual = {
            "x": _as_int(building.get("x")),
            "y": _as_int(building.get("y", building.get("z"))),
            "z": _as_int(building.get("z")),
        }
        if actual == coords:
            return building
    return None


def _placement_forks(actions, execution_results, after_state):
    forks = []
    seen = set()
    for item in execution_results or []:
        index = _as_int(item.get("index"), -1)
        action = actions[index] if 0 <= index < len(actions) else {}
        if action.get("action") != "place_building":
            continue
        body = item.get("result") if isinstance(item, dict) else {}
        applied = body.get("applied") if isinstance(body, dict) else {}
        if not isinstance(applied, dict):
            continue
        actual = {
            "x": _as_int(applied.get("x")),
            "y": _as_int(applied.get("y", applied.get("z"))),
            "z": _as_int(applied.get("z")),
        }
        auto_connect = applied.get("auto_connect")
        reason = None
        if isinstance(auto_connect, dict) and auto_connect.get("connected") is False:
            reason = str(auto_connect.get("reason") or "unconnected_placement")
        placed = _building_at(after_state, actual)
        if reason is None and isinstance(placed, dict) and placed.get("reachable") is False:
            reason = "building_unreachable"
        if reason is None:
            continue
        key = (action.get("goal_id"), reason, tuple(actual.values()))
        if key in seen:
            continue
        seen.add(key)
        forks.append(
            {
                "type": reason,
                "goal_id": action.get("goal_id"),
                "spec": applied.get("spec") or action.get("args", {}).get("spec"),
                "actual": actual,
                "requested": applied.get("requested"),
            }
        )
    return forks


def _merge_forks(*groups):
    merged = []
    seen = set()
    for group in groups:
        for fork in group or []:
            key = (
                fork.get("type"),
                fork.get("goal_id"),
                json.dumps(fork.get("actual"), sort_keys=True, default=str),
            )
            if key not in seen:
                seen.add(key)
                merged.append(fork)
    return merged


def execute_frontier(bridge, actions, after_state=None):
    execution = play.execute_action_queue(bridge, actions)
    if after_state is None:
        state_status, observed_state = bridge.state()
        after_state = observed_state if state_status == 200 and isinstance(observed_state, dict) else {}
    forks = _placement_forks(actions, execution.get("results") or [], after_state or {})
    execution["forks"] = forks
    execution["after_state"] = after_state
    return execution


def compute_state_deltas(before, after):
    """Return deterministic resource/population/housing deltas for journaling."""
    deltas = {"resources": {}, "population": {}}
    goods = {
        str(item.get("good"))
        for state in (before, after)
        for item in _resource_items(state)
        if isinstance(item, dict) and item.get("good") is not None
    }
    for good in sorted(goods):
        delta = _as_float(_resource_item(after, good).get("stored"), 0.0) - _as_float(
            _resource_item(before, good).get("stored"), 0.0
        )
        if delta:
            deltas["resources"][good] = delta
    before_pop = (before.get("population", {}) if isinstance(before, dict) else {}) or {}
    after_pop = (after.get("population", {}) if isinstance(after, dict) else {}) or {}
    for key in ("total", "adults", "kits", "homeless", "free_beds", "unemployed"):
        delta = _as_int(after_pop.get(key), 0) - _as_int(before_pop.get(key), 0)
        if delta:
            deltas["population"][key] = delta
    return deltas


def thresholds_for_report(report, state):
    """Wake when the next materially blocked ready goal becomes affordable."""
    thresholds = {}
    candidates = report.get("candidates_by_goal", {}) if isinstance(report, dict) else {}
    for material, field in (("Log", "cost_logs"), ("Plank", "cost_planks")):
        have = _resource_stock(state, material)
        blocked_costs = [
            _as_int(goal.get(field), 0)
            for goal in (report.get("goals", []) if isinstance(report, dict) else []) or []
            if isinstance(goal, dict)
            and goal.get("id")
            and candidates.get(goal.get("id"))
            and _as_int(goal.get(field), 0) > have
        ]
        if blocked_costs:
            thresholds[material] = min(blocked_costs)
    return thresholds


def _selected_nonplacement_actions(report, selected_goal_ids):
    selected = set(selected_goal_ids or [])
    actions = []
    for goal in (report.get("goals", []) if isinstance(report, dict) else []) or []:
        if not isinstance(goal, dict) or goal.get("id") not in selected:
            continue
        if str(goal.get("id", "")).startswith("demolish_unreachable") and isinstance(goal.get("coords"), dict):
            actions.append(
                {
                    "action": "demolish",
                    "args": dict(goal["coords"]),
                    "goal_id": goal["id"],
                }
            )
    return actions


def _journal_action_results(journal_path, run_id, cycle, execution):
    for item in execution.get("results") or []:
        play.journal_append(
            journal_path,
            {
                "run_id": run_id,
                "step": cycle,
                "event": "action_result",
                "action": {
                    "name": item.get("action"),
                    "command": item.get("command"),
                },
                "result": {
                    "http_status": item.get("http_status", execution.get("http_status")),
                    "body": item.get("result"),
                },
            },
        )


def _read_cycle_inputs(bridge, cycle):
    state_status, state = bridge.state()
    if state_status != 200 or not isinstance(state, dict):
        raise RuntimeError("/state failed status=%s body=%s" % (state_status, play._short(state)))
    map_status, map_data = bridge.map()
    if map_status != 200 or not isinstance(map_data, dict):
        play.log_stderr("cycle %d: /map unavailable (status=%s)" % (cycle, map_status))
        map_data = {}
    resources_status, resources = bridge.resources()
    if resources_status != 200 or not isinstance(resources, dict) or resources.get("ok") is False:
        play.log_stderr("cycle %d: /resources unavailable (status=%s)" % (cycle, resources_status))
        resources = None
    return state, map_data, resources, map_status, resources_status


def _vision_at_fork(cfg, state):
    if _as_int(cfg.get("VISION_EVERY"), 0) <= 0 or play.vision_look is None:
        return None
    try:
        return play.vision_look(
            cfg["BRIDGE_URL"],
            cfg["OLLAMA_URL"],
            cfg.get("VISION_MODEL"),
            width=768,
            state_hint=play.compact_state(state),
        )
    except Exception as error:
        play.log_stderr("vision at fork failed: %s" % error)
        return None


def run_controller(cfg, run_id, max_cycles, bridge=None, ollama=None):
    """Run the controller-first loop without launching or screen-controlling the game."""
    bridge = bridge or play.Bridge(cfg["BRIDGE_URL"])
    ollama = ollama or play.Ollama(cfg["OLLAMA_URL"], cfg["MODEL"])
    journal_dir = os.path.join(AGENT_DIR, "journal")
    os.makedirs(journal_dir, exist_ok=True)
    journal_path = os.path.join(journal_dir, "%s.jsonl" % run_id)
    play.log_stderr("controller journal: %s" % journal_path)

    ping_status, ping_body = bridge.ping()
    pause_status, pause_body = bridge.act("set_speed", {"speed": 0})
    play.journal_append(
        journal_path,
        {
            "run_id": run_id,
            "event": "run_start",
            "mode": "controller",
            "config": dict(cfg),
            "max_cycles": max_cycles,
            "ping": {"status": ping_status, "body": ping_body},
            "initial_pause": {"status": pause_status, "body": pause_body},
        },
    )
    if (
        pause_status != 200
        or not isinstance(pause_body, dict)
        or pause_body.get("ok") is not True
    ):
        play.journal_append(
            journal_path,
            {"run_id": run_id, "event": "run_end", "reason": "initial_pause_failed"},
        )
        raise RuntimeError("controller could not pause the game before reading state")

    pending_forks = []
    handled_alert_ids = set()
    consecutive_errors = 0
    try:
        for cycle in range(1, max_cycles + 1):
            try:
                state, map_data, resources, map_status, resources_status = _read_cycle_inputs(
                    bridge, cycle
                )
                buildings_detail = ((state.get("buildings") or {}).get("list") or [])
                report = planner.plan_report(
                    state,
                    map_data,
                    buildings_detail,
                    resources=resources,
                )

                # The deterministic controller can play a full game on its own: at a
                # fork it funds goals in the planner's survival priority order and
                # advances time when blocked. The LLM is an OPTIONAL optimizer, off by
                # default (USE_LLM) — it was being called every cycle and just churned
                # without unblocking anything, making runs crawl.
                use_llm = bool(cfg.get("USE_LLM"))
                llm_required = use_llm and needs_llm(
                    report,
                    state,
                    pending_forks=pending_forks,
                    handled_alert_ids=handled_alert_ids,
                )
                choice = None
                selected_goal_ids = None
                forks_at_start = list(pending_forks)
                if llm_required:
                    vision = _vision_at_fork(cfg, state)
                    choice = play.arbitrate_planner_fork(
                        ollama,
                        report,
                        state,
                        pending_forks=forks_at_start,
                        vision=vision,
                    )
                    selected_goal_ids = choice.get("goal_ids") or []
                    handled_alert_ids.update(_novel_alerts(state))
                    play.journal_append(
                        journal_path,
                        {
                            "run_id": run_id,
                            "step": cycle,
                            "event": "arbiter_choice",
                            "fork": report.get("decision_fork"),
                            "pending_forks": forks_at_start,
                            "choice": choice,
                        },
                    )

                frontier = build_safe_ready_frontier(
                    report,
                    state,
                    selected_goal_ids=selected_goal_ids,
                )
                actions = list(frontier["actions"])
                for action in _selected_nonplacement_actions(report, selected_goal_ids):
                    if len(actions) < MAX_BATCH_ACTIONS:
                        actions.append(action)

                if actions:
                    execution = execute_frontier(bridge, actions)
                    after_state = execution.get("after_state") or state
                    deltas = compute_state_deltas(state, after_state)
                    action_names = [action.get("action") for action in actions if action.get("action")]
                    effects = []
                    if discovery_mod is not None:
                        try:
                            effects = discovery_mod.observe_step(state, action_names, after_state)
                        except Exception as error:
                            play.log_stderr("cycle %d: discovery observe failed: %s" % (cycle, error))
                    resolved_route_fork = any(
                        str(action.get("goal_id", "")).startswith("demolish_unreachable")
                        for action in actions
                    )
                    pending_forks = _merge_forks(
                        [] if resolved_route_fork else forks_at_start,
                        execution.get("forks") or [],
                    )
                    _journal_action_results(journal_path, run_id, cycle, execution)
                    play.journal_append(
                        journal_path,
                        {
                            "run_id": run_id,
                            "step": cycle,
                            "event": "step",
                            "mode": "controller",
                            "state": play.state_summary_for_journal(state),
                            "map": {"http_status": map_status},
                            "resources_http_status": resources_status,
                            "planner": report.get("text"),
                            "frontier": frontier,
                            "action": {"actions": actions, "arbiter": choice},
                            "result": {
                                "http_status": execution.get("http_status"),
                                "body": execution.get("body"),
                                "fallback": execution.get("fallback", False),
                                "forks": pending_forks,
                            },
                            "deltas": deltas,
                            "observed_effects": effects,
                        },
                    )
                    print(
                        "cycle %02d/%d | frontier=%d actions=%d forks=%d llm=%s"
                        % (
                            cycle,
                            max_cycles,
                            len(frontier["goal_ids"]),
                            len(actions),
                            len(pending_forks),
                            "yes" if llm_required else "no",
                        ),
                        flush=True,
                    )
                else:
                    # No executable frontier action this cycle. The safe default is
                    # ALWAYS to advance time — never stall paused. A fork whose chosen
                    # goal is unaffordable (e.g. WaterPump needs 12 logs, have 0)
                    # resolves itself once builders/cutters produce; a pending
                    # unreachable-building fork keeps its alert and is retried. Stalling
                    # paused would make logs impossible to ever accumulate.
                    thresholds = thresholds_for_report(report, state)
                    advance = bulk_advance_until_wake(
                        bridge,
                        state,
                        thresholds=thresholds,
                        run_speed=_as_int(cfg.get("RUN_SPEED"), DEFAULT_RUN_SPEED),
                        poll_interval=_as_float(cfg.get("POLL_INTERVAL"), DEFAULT_POLL_INTERVAL),
                        max_polls=_as_int(cfg.get("MAX_POLLS"), DEFAULT_MAX_POLLS),
                        max_advance_days=_as_float(
                            cfg.get("MAX_ADVANCE_DAYS"), DEFAULT_MAX_ADVANCE_DAYS
                        ),
                    )
                    if advance.get("paused") is not True:
                        raise RuntimeError("event watcher could not pause the game")
                    play.journal_append(
                        journal_path,
                        {
                            "run_id": run_id,
                            "step": cycle,
                            "event": "advance",
                            "state": play.state_summary_for_journal(state),
                            "thresholds": thresholds,
                            "wake": {
                                "reason": advance.get("reason"),
                                "polls": advance.get("polls"),
                                "paused": advance.get("paused"),
                            },
                            "after": play.state_summary_for_journal(advance.get("state") or state),
                        },
                    )
                    reason = advance.get("reason")
                    if pending_forks:
                        reason = "%s (fork pending)" % reason
                    print(
                        "cycle %02d/%d | advance wake=%s polls=%s llm=%s"
                        % (cycle, max_cycles, reason, advance.get("polls"),
                           "yes" if llm_required else "no"),
                        flush=True,
                    )
                consecutive_errors = 0
            except KeyboardInterrupt:
                play.journal_append(
                    journal_path,
                    {"run_id": run_id, "step": cycle, "event": "interrupted"},
                )
                break
            except Exception as error:
                consecutive_errors += 1
                play.log_stderr(
                    "cycle %d: UNEXPECTED %s\n%s" % (cycle, error, traceback.format_exc())
                )
                play.journal_append(
                    journal_path,
                    {
                        "run_id": run_id,
                        "step": cycle,
                        "event": "exception",
                        "detail": str(error),
                    },
                )
                if consecutive_errors >= play.MAX_CONSECUTIVE_ERRORS:
                    break
    finally:
        final_pause_status, final_pause_body = bridge.act("set_speed", {"speed": 0})
        play.journal_append(
            journal_path,
            {
                "run_id": run_id,
                "event": "final_pause",
                "status": final_pause_status,
                "ok": final_pause_status == 200
                and isinstance(final_pause_body, dict)
                and final_pause_body.get("ok") is True,
            },
        )
        play.journal_append(journal_path, {"run_id": run_id, "event": "run_end"})
        play.run_learning_loop(journal_path, run_id)
        play.log_stderr("controller run complete. journal: %s" % journal_path)
    return journal_path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Timberborn controller-first agent loop.")
    parser.add_argument("--bridge-url", default=play.DEFAULTS["BRIDGE_URL"])
    parser.add_argument("--ollama-url", default=play.DEFAULTS["OLLAMA_URL"])
    parser.add_argument("--model", default=play.DEFAULTS["MODEL"])
    parser.add_argument("--max-cycles", type=int, default=play.DEFAULTS["MAX_STEPS"])
    parser.add_argument("--run-id", default=os.environ.get("RUN_ID", "controller-run"))
    parser.add_argument("--run-speed", type=int, default=DEFAULT_RUN_SPEED)
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument("--max-polls", type=int, default=DEFAULT_MAX_POLLS)
    parser.add_argument("--max-advance-days", type=float, default=DEFAULT_MAX_ADVANCE_DAYS)
    parser.add_argument("--vision-model", default=play.DEFAULTS["VISION_MODEL"])
    parser.add_argument("--vision-every", type=int, default=play.DEFAULTS["VISION_EVERY"])
    parser.add_argument("--use-llm", action="store_true",
                        help="Consult the LLM at genuine forks (default: fully deterministic).")
    args = parser.parse_args(argv)
    cfg = {
        "BRIDGE_URL": args.bridge_url,
        "OLLAMA_URL": args.ollama_url,
        "MODEL": args.model,
        "VISION_MODEL": args.vision_model,
        "VISION_EVERY": args.vision_every if args.use_llm else 0,
        "RUN_SPEED": args.run_speed,
        "POLL_INTERVAL": args.poll_interval,
        "MAX_POLLS": args.max_polls,
        "MAX_ADVANCE_DAYS": args.max_advance_days,
        "USE_LLM": args.use_llm,
    }
    run_controller(cfg, args.run_id, args.max_cycles)


if __name__ == "__main__":
    main()
