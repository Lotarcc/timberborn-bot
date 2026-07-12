"""Replay recorder + credit assignment for the Timberborn learning loop.

`record_step` appends one JSON line per game-cycle to `agent/runs/<run_id>.jsonl`,
capturing just enough of the bridge `/state` payload to reconstruct the colony's
economic trajectory (population, water/food buffer days, log/plank stock, building
counts) alongside whatever action the controller/LLM chose that step.

After a run ends (or mid-run, via `progress_signal` on the rows recorded so far),
`summarize_run`/`classify_stall` classify how it ended (survived / died of thirst /
died of hunger / stalled with no progress) and, for a bad ending, WHY it's worth a
relabel or not: `classify_stall` distinguishes RESOURCE_STARVED/POLICY_GAP (the
colony died, or the real deterministic expert still saw a move the policy missed -
correctable) from STRUCTURAL_GAP (the expert also had nothing to propose - no
correct label exists). `credit_assignment` then looks back over the steps leading
up to a correctable bad ending and proposes what should have been done instead,
preferring the expert's own simultaneous opinion (`meta.expert_top`, true DAgger)
over the older hand-written heuristic (e.g. "you let water hit zero for 3 steps
with no WaterPump -> you should have built one"). This is the run-to-run learning
signal: future runs can consult past regret windows instead of repeating the same
mistake. See docs/kb/learning-loop-design.md for the full design.

Python 3 standard library only - no third-party packages, no network calls.
Runs its own tests: `python3 agent/replay.py`.
"""

from __future__ import annotations

import json
import os
import unittest
import uuid
from collections import Counter

# `agent/` has no __init__.py, and this module is designed to run standalone
# (`python3 agent/replay.py`) as well as via package-qualified imports, so try the
# package-relative import first and fall back to the bare sibling import that
# resolves when sys.path[0] is agent/ itself (prefer `.venv/bin/python` either way).
try:
    from agent import game_schema
except ImportError:
    import game_schema

_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_AGENT_DIR, "data")
_RUNS_DIR = os.path.join(_AGENT_DIR, "runs")
_GOODS_PATH = os.path.join(_DATA_DIR, "goods.json")

# --- credit-assignment tuning -------------------------------------------------
_NEAR_ZERO_DAYS = 0.05   # "days_remaining ~= 0"
_DEATH_STREAK = 3        # >=3 consecutive near-zero steps -> starvation/dehydration
_STALL_STREAK = 8        # >=8 consecutive no-progress advance_time steps -> stalled

# Building specs (bare, pre-faction-suffix) used by the credit heuristics.
_SPEC_WATER_PUMP = "WaterPump"
_SPEC_GATHERER = "GathererFlag"
_SPEC_FARMHOUSE = "EfficientFarmHouse"
_SPEC_LUMBERJACK = "LumberjackFlag"

# Real goal_id strings from game_schema.actions() - verified against the DB-driven
# action space, NOT the plan-doc placeholder names.
GOAL_WATER_PUMP = "build_water_pump"
GOAL_SMALL_TANK = "build_small_tank"
GOAL_GATHERER = "build_gatherer_flag"
GOAL_FARMHOUSE = "build_efficient_farm_house"
GOAL_LUMBERJACK = "build_lumberjack_flag"


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _num(value, default=0):
    """Best-effort numeric coercion that never raises."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


_FOOD_GOODS_CACHE = None


def _food_goods():
    """Set of good ids where goods.json marks is_food true (read directly, no
    dependency on game_schema so this module stays self-contained)."""
    global _FOOD_GOODS_CACHE
    if _FOOD_GOODS_CACHE is None:
        with open(_GOODS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        _FOOD_GOODS_CACHE = {
            g["id"] for g in data.get("goods", []) if isinstance(g, dict) and g.get("is_food")
        }
    return _FOOD_GOODS_CACHE


def _resources(state):
    value = (state or {}).get("resources")
    return value if isinstance(value, list) else []


def _resource(state, good):
    for item in _resources(state):
        if isinstance(item, dict) and item.get("good") == good:
            return item
    return None


def _resource_field(state, good, field, default=0.0):
    item = _resource(state, good)
    if not item:
        return default
    return _num(item.get(field), default)


def _food_days(state):
    """max days_remaining over resources whose good is_food (0.0 if none present)."""
    foods = _food_goods()
    best = 0.0
    for item in _resources(state):
        if isinstance(item, dict) and item.get("good") in foods:
            best = max(best, _num(item.get("days_remaining"), 0.0))
    return best


def _building_counts(state):
    counts = ((state or {}).get("buildings") or {}).get("counts")
    return dict(counts) if isinstance(counts, dict) else {}


def _has_building(building_counts, spec):
    """True if building_counts has a key whose bare (pre-faction) prefix == spec
    with a positive count. Keys are faction-suffixed, e.g. 'WaterPump.Folktails'."""
    for key, value in (building_counts or {}).items():
        if str(key).split(".")[0] == spec and _num(value, 0) > 0:
            return True
    return False


def _building_total(building_counts):
    return sum(_num(v, 0) for v in (building_counts or {}).values())


def _action_id(action):
    """Normalize a recorded `action` (str or dict) down to a goal_id/verb string."""
    if isinstance(action, str):
        return action
    if isinstance(action, dict):
        for key in ("action", "id", "goal_id", "tool", "name"):
            value = action.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _meta_expert_top(row):
    """row["meta"]["expert_top"] if row.meta is a dict, else None. play_policy.py's
    record_step calls always carry this (the schema-id the deterministic expert -
    controller.build_safe_ready_frontier - would pick this exact cycle); older/
    synthetic rows without a meta dict at all just read as None here, same as a
    row whose meta explicitly has no expert_top key."""
    meta = row.get("meta")
    return meta.get("expert_top") if isinstance(meta, dict) else None


def _failed_action_id(row):
    """For a row recorded with meta.executed is False, the goal_id worth blaming:
    meta.policy_top (what the policy actually ranked top and tried, before falling
    through to advance_time - see play_policy._execute_intent's contract, where
    executed=False always means the recorded row["action"] itself is "advance_time",
    so that field alone can't tell two different failed goals apart) if present,
    else row["action"]/_action_id as a fallback for rows recorded without
    policy_top. None for a row that isn't an executed=False row at all."""
    meta = row.get("meta")
    if not (isinstance(meta, dict) and meta.get("executed") is False):
        return None
    policy_top = meta.get("policy_top")
    if isinstance(policy_top, str) and policy_top:
        return policy_top
    return _action_id(row.get("action"))


def _run_path(run_id):
    return os.path.join(_RUNS_DIR, "%s.jsonl" % run_id)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def record_step(run_id, step, state, action, meta=None):
    """Append one JSONL record for this cycle to agent/runs/<run_id>.jsonl."""
    state = state if isinstance(state, dict) else {}
    time_block = state.get("time") or {}
    population = state.get("population") or {}
    record = {
        "step": step,
        "day": time_block.get("day"),
        "hour": time_block.get("hour"),
        "pop_total": population.get("total"),
        "homeless": population.get("homeless"),
        "water_days": _resource_field(state, "Water", "days_remaining", 0.0),
        "food_days": _food_days(state),
        "log_stored": _resource_field(state, "Log", "stored", 0.0),
        "plank_stored": _resource_field(state, "Plank", "stored", 0.0),
        "building_counts": _building_counts(state),
        "features": game_schema.feature_strings(state),
        "action": action,
        "meta": meta,
    }
    os.makedirs(_RUNS_DIR, exist_ok=True)
    with open(_run_path(run_id), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record))
        fh.write("\n")
    return record


def load_run(run_id):
    """Return the list of step records for run_id, in the order they were written.
    Missing/unreadable files and malformed lines are treated as absent, not errors."""
    rows = []
    try:
        fh = open(_run_path(run_id), "r", encoding="utf-8")
    except OSError:
        return rows
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows


def progress_signal(rows, state_ctx=None):
    """Single pass over a run's rows - or a PREFIX of them - classifying how the
    run has gone SO FAR: the running peak/final/min stats plus, if it already
    ended badly, the classification and the row-index where that classification
    first became true (the "trigger" the lookback window in credit_assignment/
    classify_stall anchors on).

    This is the same logic summarize_run/credit_assignment/classify_stall already
    relied on via `_scan` (which is now a thin alias for this function - see
    below), extracted so it is also incrementally callable: play_policy.run's
    in-run stall check calls this every cycle with the rows recorded so far, and
    gets the identical classification summarize_run would compute post-hoc on the
    finished file, without duplicating the _STALL_STREAK/_DEATH_STREAK thresholds.

    `state_ctx` is currently unused - reserved for a future caller that wants to
    thread incremental streak state across calls instead of rescanning `rows`
    from scratch every cycle. Not needed yet: this is O(len(rows)) per call, and
    _STALL_STREAK=8/_DEATH_STREAK=3 mean the classification-relevant streaks
    resolve within single-digit rows of whatever prefix is passed in, so an
    in-run caller re-scanning the (short, bounded-by-when-it-stops) rows-so-far
    list every cycle is cheap in practice."""
    result = {
        "days_survived": 0,
        "peak_pop": 0,
        "final_pop": 0,
        "ended": "alive",
        "death_cause": None,
        "min_water_days": 0.0,
        "min_food_days": 0.0,
        "reached_pump": False,
        "reached_30_pop": False,
        "trigger_index": None,
    }
    if not rows:
        return result

    min_water = None
    min_food = None
    water_streak = 0
    food_streak = 0
    advance_streak = 0
    prev_log = None
    prev_building_total = None

    for i, row in enumerate(rows):
        pop = _num(row.get("pop_total"), 0)
        water_days = _num(row.get("water_days"), 0.0)
        food_days = _num(row.get("food_days"), 0.0)
        log_stored = _num(row.get("log_stored"), 0.0)
        building_counts = row.get("building_counts") or {}
        building_total = _building_total(building_counts)

        result["peak_pop"] = max(result["peak_pop"], pop)
        result["final_pop"] = pop
        min_water = water_days if min_water is None else min(min_water, water_days)
        min_food = food_days if min_food is None else min(min_food, food_days)
        if result["peak_pop"] >= 30:
            result["reached_30_pop"] = True
        if _has_building(building_counts, _SPEC_WATER_PUMP):
            result["reached_pump"] = True
        if row.get("day") is not None:
            result["days_survived"] = row.get("day")

        if result["ended"] == "alive":
            water_streak = water_streak + 1 if water_days <= _NEAR_ZERO_DAYS else 0
            food_streak = food_streak + 1 if food_days <= _NEAR_ZERO_DAYS else 0

            action_id = _action_id(row.get("action"))
            no_progress = (
                action_id == "advance_time"
                and prev_log is not None and log_stored <= prev_log
                and prev_building_total is not None and building_total <= prev_building_total
            )
            advance_streak = advance_streak + 1 if no_progress else 0

            if pop <= 0:
                if water_days <= food_days:
                    result["ended"], result["death_cause"] = "dead_thirst", "thirst"
                else:
                    result["ended"], result["death_cause"] = "dead_hunger", "hunger"
                result["trigger_index"] = i
            elif water_streak >= _DEATH_STREAK:
                result["ended"], result["death_cause"], result["trigger_index"] = (
                    "dead_thirst", "thirst", i
                )
            elif food_streak >= _DEATH_STREAK:
                result["ended"], result["death_cause"], result["trigger_index"] = (
                    "dead_hunger", "hunger", i
                )
            elif advance_streak >= _STALL_STREAK:
                result["ended"], result["death_cause"], result["trigger_index"] = (
                    "stalled", "stall", i
                )

        prev_log, prev_building_total = log_stored, building_total

    result["min_water_days"] = min_water if min_water is not None else 0.0
    result["min_food_days"] = min_food if min_food is not None else 0.0
    return result


def _scan(rows):
    """Thin alias kept for the existing internal call sites (summarize_run,
    classify_stall) - the streak-tracking body now lives in progress_signal so it
    is also callable incrementally (see its docstring). Behavior-preserving: same
    function, same result, just extracted under a name that reflects it also
    being usable on a run-in-progress, not only a finished one."""
    return progress_signal(rows)


def summarize_run(run_id):
    """{days_survived, peak_pop, final_pop, ended, death_cause, min_water_days,
    min_food_days, reached_pump, reached_30_pop} for the whole run."""
    scan = _scan(load_run(run_id))
    return {
        "days_survived": scan["days_survived"],
        "peak_pop": scan["peak_pop"],
        "final_pop": scan["final_pop"],
        "ended": scan["ended"],
        "death_cause": scan["death_cause"],
        "min_water_days": scan["min_water_days"],
        "min_food_days": scan["min_food_days"],
        "reached_pump": scan["reached_pump"],
        "reached_30_pop": scan["reached_30_pop"],
    }


_SNAPSHOT_FIELDS = (
    "day", "hour", "pop_total", "homeless", "water_days", "food_days",
    "log_stored", "plank_stored", "building_counts",
)


def _stall_better_action(building_counts):
    for spec, goal_id in (
        (_SPEC_WATER_PUMP, GOAL_WATER_PUMP),
        (_SPEC_GATHERER, GOAL_GATHERER),
        (_SPEC_LUMBERJACK, GOAL_LUMBERJACK),
    ):
        if not _has_building(building_counts, spec):
            return goal_id
    return None


# ---------------------------------------------------------------------------
# failure classification (docs/kb/learning-loop-design.md SS4/SS5.2)
# ---------------------------------------------------------------------------
#
# RESOURCE_STARVED / POLICY_GAP -> credit_assignment has a correction to offer
# (relabel-able). STRUCTURAL_GAP -> the real deterministic expert also had
# nothing to propose, so no correct label exists; credit_assignment returns []
# and run_loop.py routes these to a playbook lesson instead of a relabel.
RESOURCE_STARVED = "resource_starved"
POLICY_GAP = "policy_gap"
STRUCTURAL_GAP = "structural_gap"


def classify_stall(run_id, lookback=6):
    """For a run that has already ended badly, {class, window, repeated_action,
    expert_had_option, ended} - the routing signal run_loop.py uses in place of
    the old binary failed-or-regressed gate. None if the run is still 'alive'
    (nothing to classify yet).

    `class` is one of RESOURCE_STARVED / POLICY_GAP / STRUCTURAL_GAP:

      * RESOURCE_STARVED - the colony actually died (thirst/hunger). Checked
        first, unconditionally on expert telemetry: a resource death is always
        worth a relabel attempt regardless of what the expert's opinion was.
      * POLICY_GAP - not a resource death, and at least one window row's
        meta.expert_top is a concrete game_schema goal other than "advance_time":
        the real expert (controller.build_safe_ready_frontier, as recorded live
        by play_policy.run every cycle) saw a move here that the policy didn't
        take. This is true DAgger territory - query the expert on the state the
        learner itself visited.
      * STRUCTURAL_GAP - not a resource death, and every window row whose
        meta.expert_top was actually RECORDED says "advance_time": the expert
        itself had nothing to propose either. No correct label exists.
        NOTE (deliberate refinement over a literal "not policy_gap => structural
        gap" reading): a window with NO expert_top telemetry recorded at all
        (every row's meta is missing/None, or lacks an "expert_top" key - i.e. a
        run predating play_policy passing meta={"expert_top": ...}, which is
        every existing replay.py/learn.py fixture) is routed to POLICY_GAP, not
        STRUCTURAL_GAP. Absence of evidence isn't evidence the expert also had
        nothing - only a *recorded* "advance_time" proves that. This is what
        keeps every pre-DAgger-telemetry run going through credit_assignment's
        heuristic fallback unchanged, instead of newly returning [] for runs
        that used to get a (heuristic) correction.

    `repeated_action` (the REPEATED_FAILED_ACTION auxiliary signal) is the
    dominant goal_id behind this window's executed=False rows - see
    _failed_action_id - when it accounts for at least len(window)-1 of them,
    else None. Feeds gap_lesson_from_diagnosis's playbook trigger tag.
    """
    rows = load_run(run_id)
    scan = _scan(rows)
    if scan["ended"] == "alive" or scan["trigger_index"] is None:
        return None

    start = max(0, scan["trigger_index"] - lookback + 1)
    window = rows[start:scan["trigger_index"] + 1]

    expert_tops = [_meta_expert_top(r) for r in window]
    expert_had_option = any(et not in (None, "advance_time") for et in expert_tops)
    has_expert_telemetry = any(
        isinstance(r.get("meta"), dict) and "expert_top" in r["meta"] for r in window
    )

    failed_actions = [fa for fa in (_failed_action_id(r) for r in window) if fa is not None]
    repeated = None
    if failed_actions:
        top, count = Counter(failed_actions).most_common(1)[0]
        if count >= max(1, len(window) - 1):
            repeated = top

    if scan["ended"] in ("dead_thirst", "dead_hunger"):
        cls = RESOURCE_STARVED
    elif expert_had_option:
        cls = POLICY_GAP
    elif has_expert_telemetry:
        cls = STRUCTURAL_GAP
    else:
        cls = POLICY_GAP  # no expert telemetry recorded at all - see NOTE above

    return {
        "class": cls,
        "window": window,
        "repeated_action": repeated,
        "expert_had_option": expert_had_option,
        "ended": scan["ended"],
    }


def _legacy_better_action(diagnosis, row=None):
    """The pre-DAgger heuristic (thirst/hunger producer checks, `_stall_better_
    action`), kept verbatim - just re-sourced from a classify_stall diagnosis
    dict (diagnosis["ended"]/diagnosis["window"]) instead of re-scanning the run
    itself. Returns (better_action, reason).

    `row` is accepted only for call-site symmetry with credit_assignment's
    per-row DAgger-or-fallback decision; it's unused here because this heuristic
    has always looked at the whole window (or its last row), never per-row -
    every row in a window got the exact same correction before this refactor
    too, so computing it once per window (not once per row) is behavior-
    preserving, not a simplification."""
    ended = diagnosis["ended"]
    window_rows = diagnosis["window"]
    if ended == "dead_thirst":
        pump_in_window = any(
            _has_building(r.get("building_counts") or {}, _SPEC_WATER_PUMP) for r in window_rows
        )
        better_action = GOAL_SMALL_TANK if pump_in_window else GOAL_WATER_PUMP
        reason = (
            "colony died of thirst (water_days ~0 for %d+ steps); " % _DEATH_STREAK
            + (
                "a WaterPump already existed so more storage was needed"
                if pump_in_window else
                "no WaterPump existed in the preceding steps"
            )
        )
    elif ended == "dead_hunger":
        producer_in_window = any(
            _has_building(r.get("building_counts") or {}, _SPEC_GATHERER)
            or _has_building(r.get("building_counts") or {}, _SPEC_FARMHOUSE)
            for r in window_rows
        )
        better_action = GOAL_FARMHOUSE if producer_in_window else GOAL_GATHERER
        reason = (
            "colony died of hunger (food_days ~0 for %d+ steps); " % _DEATH_STREAK
            + (
                "a food producer already existed so more production was needed"
                if producer_in_window else
                "no food producer existed in the preceding steps"
            )
        )
    else:  # stalled
        last_counts = window_rows[-1].get("building_counts") or {} if window_rows else {}
        better_action = _stall_better_action(last_counts)
        reason = (
            "no increase in log_stored or building count for %d+ consecutive "
            "advance_time steps; " % _STALL_STREAK
            + (
                "missing %s" % better_action if better_action else
                "WaterPump/GathererFlag/LumberjackFlag all present, cause unclear"
            )
        )
    return better_action, reason


def credit_assignment(run_id, lookback=6):
    """For a run that ended badly, the `lookback` steps leading up to the failure,
    each annotated with the action that should have been chosen instead.

    Delegates classification to classify_stall. Empty list when there's nothing
    to blame: the run is still 'alive', or its class is STRUCTURAL_GAP (the real
    expert also proposed nothing at every one of these states - no correct label
    exists to clone toward, see docs/kb/learning-loop-design.md SS2/SS5.2).

    RESOURCE_STARVED/POLICY_GAP windows get a per-row correction: prefer the
    real expert's simultaneous opinion (that row's meta.expert_top) when it is a
    concrete game_schema goal other than "advance_time" - true DAgger, querying
    the expert on the exact state the learner itself visited - else fall back to
    the pre-DAgger heuristic (_legacy_better_action). A run recorded before
    meta.expert_top existed (every pre-existing replay.py/learn.py fixture) has
    no expert_top on any row, so every one of its entries takes the fallback
    path - unchanged from this function's behavior before classify_stall existed.
    """
    diagnosis = classify_stall(run_id, lookback=lookback)
    if diagnosis is None or diagnosis["class"] == STRUCTURAL_GAP:
        return []

    legacy_action, legacy_reason = _legacy_better_action(diagnosis)

    entries = []
    for row in diagnosis["window"]:
        expert_top = _meta_expert_top(row)
        if expert_top not in (None, "advance_time"):
            better_action = expert_top
            reason = (
                "DAgger: the expert planner proposed %r at this exact state; the "
                "policy diverged from a still-capable expert" % (expert_top,)
            )
        else:
            better_action, reason = legacy_action, legacy_reason
        entries.append({
            "step": row.get("step"),
            "state_snapshot": {key: row.get(key) for key in _SNAPSHOT_FIELDS},
            "features": row.get("features") or [],
            "chosen_action": row.get("action"),
            "better_action": better_action,
            "reason": reason,
        })
    return entries


# ---------------------------------------------------------------------------
# inline tests
# ---------------------------------------------------------------------------

class ReplayTests(unittest.TestCase):
    def setUp(self):
        self._run_ids = []

    def tearDown(self):
        for run_id in self._run_ids:
            try:
                os.remove(_run_path(run_id))
            except OSError:
                pass

    def _new_run_id(self, label):
        run_id = "ut_%s_%s" % (label, uuid.uuid4().hex[:8])
        self._run_ids.append(run_id)
        return run_id

    @staticmethod
    def _state(day, hour, pop, homeless, water_days, food_days, log, plank, counts,
               berries_days=None):
        resources = [
            {"good": "Water", "stored": max(water_days, 0) * 2, "days_remaining": water_days},
            {"good": "Log", "stored": log, "days_remaining": 0},
            {"good": "Plank", "stored": plank, "days_remaining": 0},
            {"good": "Berries", "stored": max(food_days, 0) * 2,
             "days_remaining": food_days if berries_days is None else berries_days},
            {"good": "Bread", "stored": 0, "days_remaining": 0},
        ]
        return {
            "time": {"cycle": 1, "day": day, "hour": hour, "daytime": "day"},
            "population": {"total": pop, "homeless": homeless},
            "resources": resources,
            "buildings": {"counts": dict(counts), "under_construction": 0},
        }

    def _write_trace(self, run_id, steps):
        """steps: list of (state_kwargs_dict, action) tuples."""
        for i, (state_kwargs, action) in enumerate(steps, start=1):
            record_step(run_id, i, self._state(**state_kwargs), action)

    # -- record_step / load_run -------------------------------------------------

    def test_record_step_writes_required_fields_and_creates_dir(self):
        run_id = self._new_run_id("schema")
        state = self._state(1, 8, 10, 10, 5.0, 5.0, 0, 0, {"DistrictCenter.Folktails": 1})

        record_step(run_id, 1, state, "advance_time", meta={"note": "boot"})

        self.assertTrue(os.path.isdir(_RUNS_DIR))
        rows = load_run(run_id)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            set(row.keys()),
            {
                "step", "day", "hour", "pop_total", "homeless", "water_days",
                "food_days", "log_stored", "plank_stored", "building_counts",
                "features", "action", "meta",
            },
        )
        self.assertEqual(row["step"], 1)
        self.assertEqual(row["day"], 1)
        self.assertEqual(row["hour"], 8)
        self.assertEqual(row["pop_total"], 10)
        self.assertEqual(row["homeless"], 10)
        self.assertEqual(row["water_days"], 5.0)
        self.assertEqual(row["food_days"], 5.0)
        self.assertEqual(row["log_stored"], 0)
        self.assertEqual(row["plank_stored"], 0)
        self.assertEqual(row["building_counts"], {"DistrictCenter.Folktails": 1})
        self.assertEqual(row["features"], game_schema.feature_strings(state))
        self.assertTrue(row["features"])
        self.assertEqual(row["action"], "advance_time")
        self.assertEqual(row["meta"], {"note": "boot"})

    def test_load_run_missing_run_id_returns_empty_list(self):
        self.assertEqual(load_run("does_not_exist_%s" % uuid.uuid4().hex), [])

    # -- goal_id / game_schema consistency ---------------------------------
    #
    # The five GOAL_* constants are hand-copied goal_id strings. If game_schema's
    # DB-driven action space ever renames/removes one of the underlying buildings,
    # credit_assignment would silently recommend an action that no controller/LLM
    # consumer can ever match. Guard the drift here rather than discovering it at
    # runtime.

    def test_goal_id_constants_are_valid_game_schema_actions(self):
        # `agent/` has no __init__.py, and this module is designed to run standalone
        # (`python3 agent/replay.py`) as well as via package-qualified imports, so
        # try the package-relative import first and fall back to the bare sibling
        # import that resolves when sys.path[0] is agent/ itself.
        try:
            from agent import game_schema
        except ImportError:
            import game_schema
        self.assertLessEqual(
            {GOAL_WATER_PUMP, GOAL_SMALL_TANK, GOAL_GATHERER, GOAL_FARMHOUSE, GOAL_LUMBERJACK},
            set(game_schema.actions()),
        )

    # -- shared fixtures ------------------------------------------------------
    #
    # Trace shapes are plain data (not test methods) so classification tests and
    # credit-assignment tests can each build their own fresh run_id from the same
    # scenario without one test invoking another.

    _THIRST_DEATH_STEPS = [
        (dict(day=1, hour=8, pop=10, homeless=10, water_days=5.0, food_days=5.0,
              log=0, plank=0, counts={"DistrictCenter.Folktails": 1}), "advance_time"),
        (dict(day=1, hour=14, pop=10, homeless=10, water_days=3.0, food_days=5.0,
              log=6, plank=0, counts={"DistrictCenter.Folktails": 1,
                                       "LumberjackFlag.Folktails": 1}), "build_lumberjack_flag"),
        (dict(day=2, hour=8, pop=10, homeless=10, water_days=1.0, food_days=4.5,
              log=6, plank=0, counts={"DistrictCenter.Folktails": 1,
                                       "LumberjackFlag.Folktails": 1}), "advance_time"),
        (dict(day=2, hour=14, pop=10, homeless=10, water_days=0.0, food_days=4.0,
              log=6, plank=0, counts={"DistrictCenter.Folktails": 1,
                                       "LumberjackFlag.Folktails": 1}), "advance_time"),
        (dict(day=3, hour=8, pop=10, homeless=10, water_days=0.0, food_days=3.5,
              log=6, plank=0, counts={"DistrictCenter.Folktails": 1,
                                       "LumberjackFlag.Folktails": 1}), "advance_time"),
        (dict(day=3, hour=14, pop=10, homeless=10, water_days=0.0, food_days=3.0,
              log=6, plank=0, counts={"DistrictCenter.Folktails": 1,
                                       "LumberjackFlag.Folktails": 1}), "advance_time"),
        (dict(day=4, hour=8, pop=8, homeless=8, water_days=0.0, food_days=2.5,
              log=6, plank=0, counts={"DistrictCenter.Folktails": 1,
                                       "LumberjackFlag.Folktails": 1}), "advance_time"),
        (dict(day=4, hour=14, pop=0, homeless=0, water_days=0.0, food_days=2.0,
              log=6, plank=0, counts={"DistrictCenter.Folktails": 1,
                                       "LumberjackFlag.Folktails": 1}), "advance_time"),
    ]

    _SURVIVOR_STEPS = [
        (dict(day=1, hour=8, pop=10, homeless=10, water_days=5.0, food_days=5.0,
              log=0, plank=0, counts={"DistrictCenter.Folktails": 1}), "advance_time"),
        (dict(day=2, hour=8, pop=12, homeless=2, water_days=6.0, food_days=6.0,
              log=10, plank=4, counts={"DistrictCenter.Folktails": 1,
                                        "LumberjackFlag.Folktails": 1}), "build_lumberjack_flag"),
        (dict(day=3, hour=8, pop=14, homeless=0, water_days=8.0, food_days=7.0,
              log=14, plank=6, counts={"DistrictCenter.Folktails": 1,
                                        "LumberjackFlag.Folktails": 1,
                                        "WaterPump.Folktails": 1}), "build_water_pump"),
        (dict(day=5, hour=8, pop=20, homeless=0, water_days=10.0, food_days=9.0,
              log=20, plank=10, counts={"DistrictCenter.Folktails": 1,
                                         "LumberjackFlag.Folktails": 1,
                                         "WaterPump.Folktails": 1,
                                         "GathererFlag.Folktails": 1}), "build_gatherer_flag"),
        (dict(day=10, hour=8, pop=32, homeless=0, water_days=12.0, food_days=11.0,
              log=25, plank=15, counts={"DistrictCenter.Folktails": 1,
                                         "LumberjackFlag.Folktails": 1,
                                         "WaterPump.Folktails": 1,
                                         "GathererFlag.Folktails": 1,
                                         "SmallTank.Folktails": 2}), "build_small_tank"),
    ]

    def _build_thirst_run(self):
        run_id = self._new_run_id("thirst")
        self._write_trace(run_id, self._THIRST_DEATH_STEPS)
        return run_id

    def _build_survivor_run(self):
        run_id = self._new_run_id("survivor")
        self._write_trace(run_id, self._SURVIVOR_STEPS)
        return run_id

    # -- summarize_run classification -------------------------------------------

    def test_summarize_run_classifies_thirst_death_and_survivor(self):
        thirst_run = self._build_thirst_run()

        summary = summarize_run(thirst_run)
        self.assertEqual(summary["ended"], "dead_thirst")
        self.assertEqual(summary["death_cause"], "thirst")
        self.assertEqual(summary["days_survived"], 4)
        self.assertEqual(summary["peak_pop"], 10)
        self.assertEqual(summary["final_pop"], 0)
        self.assertEqual(summary["min_water_days"], 0.0)
        self.assertEqual(summary["min_food_days"], 2.0)
        self.assertFalse(summary["reached_pump"])
        self.assertFalse(summary["reached_30_pop"])

        survivor_run = self._build_survivor_run()

        summary = summarize_run(survivor_run)
        self.assertEqual(summary["ended"], "alive")
        self.assertIsNone(summary["death_cause"])
        self.assertEqual(summary["days_survived"], 10)
        self.assertEqual(summary["peak_pop"], 32)
        self.assertEqual(summary["final_pop"], 32)
        self.assertEqual(summary["min_water_days"], 5.0)
        self.assertEqual(summary["min_food_days"], 5.0)
        self.assertTrue(summary["reached_pump"])
        self.assertTrue(summary["reached_30_pop"])

    def test_summarize_run_classifies_hunger_death_no_food_producer(self):
        run_id = self._new_run_id("hunger")
        self._write_trace(run_id, [
            (dict(day=1, hour=8, pop=10, homeless=10, water_days=5.0, food_days=5.0,
                  log=0, plank=0, counts={"DistrictCenter.Folktails": 1}), "advance_time"),
            (dict(day=2, hour=8, pop=10, homeless=10, water_days=5.0, food_days=3.0,
                  log=6, plank=0, counts={"DistrictCenter.Folktails": 1,
                                           "LumberjackFlag.Folktails": 1}), "build_lumberjack_flag"),
            (dict(day=3, hour=8, pop=10, homeless=10, water_days=5.0, food_days=0.0,
                  log=6, plank=0, counts={"DistrictCenter.Folktails": 1,
                                           "LumberjackFlag.Folktails": 1}), "advance_time"),
            (dict(day=4, hour=8, pop=10, homeless=10, water_days=5.0, food_days=0.0,
                  log=6, plank=0, counts={"DistrictCenter.Folktails": 1,
                                           "LumberjackFlag.Folktails": 1}), "advance_time"),
            (dict(day=5, hour=8, pop=10, homeless=10, water_days=5.0, food_days=0.0,
                  log=6, plank=0, counts={"DistrictCenter.Folktails": 1,
                                           "LumberjackFlag.Folktails": 1}), "advance_time"),
        ])

        summary = summarize_run(run_id)
        self.assertEqual(summary["ended"], "dead_hunger")
        self.assertEqual(summary["death_cause"], "hunger")

        entries = credit_assignment(run_id, lookback=6)
        self.assertTrue(entries)
        for entry in entries:
            self.assertEqual(entry["better_action"], GOAL_GATHERER)

    def test_summarize_run_classifies_stall_and_prioritizes_water_pump(self):
        run_id = self._new_run_id("stall")
        counts = {"DistrictCenter.Folktails": 1, "LumberjackFlag.Folktails": 1}
        steps = [
            (dict(day=1, hour=8, pop=5, homeless=5, water_days=5.0, food_days=5.0,
                  log=5, plank=0, counts=counts), "build_lumberjack_flag"),
        ]
        for i in range(8):
            steps.append(
                (dict(day=1 + i, hour=8, pop=5, homeless=5, water_days=5.0, food_days=5.0,
                      log=5, plank=0, counts=counts), "advance_time")
            )
        self._write_trace(run_id, steps)

        summary = summarize_run(run_id)
        self.assertEqual(summary["ended"], "stalled")
        self.assertEqual(summary["death_cause"], "stall")

        entries = credit_assignment(run_id, lookback=6)
        self.assertEqual(len(entries), 6)
        for entry in entries:
            self.assertEqual(entry["better_action"], GOAL_WATER_PUMP)
            self.assertEqual(entry["chosen_action"], "advance_time")

    # -- credit_assignment --------------------------------------------------

    def test_credit_assignment_flags_water_regret_for_thirst_death(self):
        thirst_run = self._build_thirst_run()
        survivor_run = self._build_survivor_run()

        entries = credit_assignment(thirst_run, lookback=6)
        self.assertTrue(entries)
        for entry in entries:
            self.assertIn(entry["better_action"], {GOAL_WATER_PUMP, GOAL_SMALL_TANK})
            self.assertIn("step", entry)
            self.assertIn("state_snapshot", entry)
            self.assertIn("chosen_action", entry)
            self.assertIn("reason", entry)
        # No WaterPump ever existed -> the recommendation must be to build one.
        self.assertTrue(all(e["better_action"] == GOAL_WATER_PUMP for e in entries))

        self.assertEqual(credit_assignment(survivor_run), [])

    def test_credit_assignment_recommends_small_tank_when_pump_already_exists(self):
        run_id = self._new_run_id("thirst_with_pump")
        counts = {"DistrictCenter.Folktails": 1, "WaterPump.Folktails": 1}
        self._write_trace(run_id, [
            (dict(day=1, hour=8, pop=10, homeless=10, water_days=1.0, food_days=5.0,
                  log=6, plank=0, counts=counts), "advance_time"),
            (dict(day=1, hour=14, pop=10, homeless=10, water_days=0.0, food_days=5.0,
                  log=6, plank=0, counts=counts), "advance_time"),
            (dict(day=2, hour=8, pop=10, homeless=10, water_days=0.0, food_days=5.0,
                  log=6, plank=0, counts=counts), "advance_time"),
            (dict(day=2, hour=14, pop=10, homeless=10, water_days=0.0, food_days=5.0,
                  log=6, plank=0, counts=counts), "advance_time"),
        ])

        summary = summarize_run(run_id)
        self.assertEqual(summary["ended"], "dead_thirst")

        entries = credit_assignment(run_id, lookback=6)
        self.assertTrue(entries)
        self.assertTrue(all(e["better_action"] == GOAL_SMALL_TANK for e in entries))

    def test_credit_assignment_stall_has_no_better_action_when_survival_buildings_built(self):
        # Reachable stall state: WaterPump, GathererFlag and LumberjackFlag are ALL
        # already built, so _stall_better_action has nothing left to recommend and
        # returns None (credit_assignment then reports that on every window entry).
        run_id = self._new_run_id("stall_all_built")
        counts = {
            "DistrictCenter.Folktails": 1,
            "WaterPump.Folktails": 1,
            "GathererFlag.Folktails": 1,
            "LumberjackFlag.Folktails": 1,
        }
        steps = [
            (dict(day=1, hour=8, pop=5, homeless=5, water_days=5.0, food_days=5.0,
                  log=5, plank=0, counts=counts), "build_lumberjack_flag"),
        ]
        for i in range(8):
            steps.append(
                (dict(day=1 + i, hour=8, pop=5, homeless=5, water_days=5.0, food_days=5.0,
                      log=5, plank=0, counts=counts), "advance_time")
            )
        self._write_trace(run_id, steps)

        summary = summarize_run(run_id)
        self.assertEqual(summary["ended"], "stalled")
        self.assertEqual(summary["death_cause"], "stall")

        entries = credit_assignment(run_id, lookback=6)
        self.assertEqual(len(entries), 6)
        for entry in entries:
            self.assertIsNone(entry["better_action"])
            self.assertIn("all present", entry["reason"])

    # -- progress_signal / _scan parity (behavior-preserving refactor) --------

    def test_progress_signal_matches_scan_on_fixture_traces(self):
        for label, steps in (
            ("thirst", self._THIRST_DEATH_STEPS),
            ("survivor", self._SURVIVOR_STEPS),
        ):
            run_id = self._new_run_id("parity_%s" % label)
            self._write_trace(run_id, steps)
            rows = load_run(run_id)
            self.assertEqual(progress_signal(rows), _scan(rows))

    def test_progress_signal_matches_scan_on_every_growing_prefix(self):
        # play_policy.py's in-run caller calls this every cycle on a GROWING
        # prefix of the run-so-far, not just the whole finished run - prove
        # parity holds at every prefix length, not only the final one.
        run_id = self._new_run_id("parity_prefixes")
        self._write_trace(run_id, self._THIRST_DEATH_STEPS)
        rows = load_run(run_id)
        for n in range(1, len(rows) + 1):
            prefix = rows[:n]
            self.assertEqual(progress_signal(prefix), _scan(prefix))

    # -- classify_stall / DAgger credit_assignment ----------------------------
    #
    # docs/kb/learning-loop-design.md SS4/SS5.2: RESOURCE_STARVED/POLICY_GAP are
    # relabel-able (credit_assignment has a correction); STRUCTURAL_GAP means the
    # real expert also proposed nothing, so credit_assignment must return [].

    def _write_meta_trace(self, run_id, steps):
        """Like _write_trace but each step is (state_kwargs, action, meta)."""
        for i, (state_kwargs, action, meta) in enumerate(steps, start=1):
            record_step(run_id, i, self._state(**state_kwargs), action, meta=meta)

    def test_classify_stall_resource_starved_for_thirst_death(self):
        run_id = self._build_thirst_run()

        diagnosis = classify_stall(run_id, lookback=6)

        self.assertIsNotNone(diagnosis)
        self.assertEqual(diagnosis["class"], RESOURCE_STARVED)
        self.assertEqual(diagnosis["ended"], "dead_thirst")
        self.assertEqual(len(diagnosis["window"]), 6)

    def test_classify_stall_returns_none_for_a_living_run(self):
        run_id = self._build_survivor_run()
        self.assertIsNone(classify_stall(run_id))

    def test_classify_stall_policy_gap_when_expert_still_sees_a_concrete_goal(self):
        # A stall where the REAL expert (meta.expert_top, as play_policy.run
        # records it every cycle) still proposes a concrete goal throughout the
        # window - the policy diverged from a still-capable expert, classic
        # DAgger territory, not a dead end.
        run_id = self._new_run_id("policy_gap")
        counts = {"DistrictCenter.Folktails": 1, "LumberjackFlag.Folktails": 1}
        steps = [
            (dict(day=1, hour=8, pop=5, homeless=5, water_days=5.0, food_days=5.0,
                  log=5, plank=0, counts=counts), "build_lumberjack_flag", None),
        ]
        for i in range(8):
            steps.append((
                dict(day=1 + i, hour=8, pop=5, homeless=5, water_days=5.0, food_days=5.0,
                     log=5, plank=0, counts=counts), "advance_time",
                {"expert_top": "build_lumber_mill", "executed": False},
            ))
        self._write_meta_trace(run_id, steps)

        summary = summarize_run(run_id)
        self.assertEqual(summary["ended"], "stalled")

        diagnosis = classify_stall(run_id, lookback=6)
        self.assertEqual(diagnosis["class"], POLICY_GAP)
        self.assertTrue(diagnosis["expert_had_option"])

        entries = credit_assignment(run_id, lookback=6)
        self.assertEqual(len(entries), 6)
        for entry in entries:
            self.assertEqual(entry["better_action"], "build_lumber_mill")
            self.assertIn("DAgger", entry["reason"])

    def test_classify_stall_structural_gap_when_expert_also_advances_time(self):
        # Same shape stall, but the expert's OWN telemetry says advance_time at
        # every window row - the real planner also had nothing to propose. No
        # correct label exists; credit_assignment must not fabricate one.
        run_id = self._new_run_id("structural_gap")
        counts = {
            "DistrictCenter.Folktails": 1,
            "WaterPump.Folktails": 1,
            "GathererFlag.Folktails": 1,
            "LumberjackFlag.Folktails": 1,
        }
        steps = [
            (dict(day=1, hour=8, pop=5, homeless=5, water_days=5.0, food_days=5.0,
                  log=5, plank=0, counts=counts), "build_lumberjack_flag", None),
        ]
        for i in range(8):
            steps.append((
                dict(day=1 + i, hour=8, pop=5, homeless=5, water_days=5.0, food_days=5.0,
                     log=5, plank=0, counts=counts), "advance_time",
                {"expert_top": "advance_time", "executed": False, "policy_top": "build_dam"},
            ))
        self._write_meta_trace(run_id, steps)

        diagnosis = classify_stall(run_id, lookback=6)
        self.assertEqual(diagnosis["class"], STRUCTURAL_GAP)
        self.assertFalse(diagnosis["expert_had_option"])
        # REPEATED_FAILED_ACTION auxiliary signal: the goal the policy actually
        # kept wanting (meta.policy_top), not the recorded "advance_time" action
        # every executed=False row shares regardless of which goal was tried.
        self.assertEqual(diagnosis["repeated_action"], "build_dam")

        self.assertEqual(credit_assignment(run_id, lookback=6), [])

    def test_classify_stall_legacy_run_without_expert_telemetry_falls_back_to_policy_gap(self):
        # A run recorded with NO meta at all (every existing replay.py/learn.py
        # fixture, and every run predating meta.expert_top) must NOT be
        # classified STRUCTURAL_GAP - there is no evidence the expert also had
        # nothing, only absence of evidence. It routes through POLICY_GAP so
        # credit_assignment still applies the pre-DAgger heuristic fallback.
        run_id = self._new_run_id("no_telemetry")
        counts = {"DistrictCenter.Folktails": 1, "LumberjackFlag.Folktails": 1}
        steps = [
            (dict(day=1, hour=8, pop=5, homeless=5, water_days=5.0, food_days=5.0,
                  log=5, plank=0, counts=counts), "build_lumberjack_flag"),
        ]
        for i in range(8):
            steps.append(
                (dict(day=1 + i, hour=8, pop=5, homeless=5, water_days=5.0, food_days=5.0,
                      log=5, plank=0, counts=counts), "advance_time")
            )
        self._write_trace(run_id, steps)

        diagnosis = classify_stall(run_id, lookback=6)
        self.assertEqual(diagnosis["class"], POLICY_GAP)
        self.assertFalse(diagnosis["expert_had_option"])

        entries = credit_assignment(run_id, lookback=6)
        self.assertEqual(len(entries), 6)
        for entry in entries:
            self.assertEqual(entry["better_action"], GOAL_WATER_PUMP)


if __name__ == "__main__":
    unittest.main()
