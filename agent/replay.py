"""Replay recorder + credit assignment for the Timberborn learning loop.

`record_step` appends one JSON line per game-cycle to `agent/runs/<run_id>.jsonl`,
capturing just enough of the bridge `/state` payload to reconstruct the colony's
economic trajectory (population, water/food buffer days, log/plank stock, building
counts) alongside whatever action the controller/LLM chose that step.

After a run ends, `summarize_run` classifies how it ended (survived / died of
thirst / died of hunger / stalled with no progress) and `credit_assignment` looks
back over the steps leading up to a bad ending and proposes what should have been
built instead (e.g. "you let water hit zero for 3 steps with no WaterPump -> you
should have built one"). This is the run-to-run learning signal: future runs can
consult past regret windows instead of repeating the same mistake.

Python 3 standard library only - no third-party packages, no network calls.
Runs its own tests: `python3 agent/replay.py`.
"""

from __future__ import annotations

import json
import os
import unittest
import uuid

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


def _scan(rows):
    """Single pass over a run's rows shared by summarize_run and credit_assignment.

    Returns the running peak/final/min stats plus, if the run ended badly, the
    classification and the row-index where that classification first became true
    (the "trigger" the lookback window in credit_assignment anchors on)."""
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


def credit_assignment(run_id, lookback=6):
    """For a run that ended badly, the `lookback` steps leading up to the failure,
    each annotated with the action that should have been chosen instead. Empty list
    for a run that ended 'alive' (nothing to blame)."""
    rows = load_run(run_id)
    scan = _scan(rows)
    ended = scan["ended"]
    trigger_index = scan["trigger_index"]
    if ended == "alive" or trigger_index is None:
        return []

    start = max(0, trigger_index - lookback + 1)
    window_rows = rows[start:trigger_index + 1]

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

    entries = []
    for row in window_rows:
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


if __name__ == "__main__":
    unittest.main()
