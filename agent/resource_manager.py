"""Resource management helpers for the Timberborn agent.

PURE functions only. No network / bridge / HTTP calls, no torch. The module
reads the static JSON knowledge files under ``agent/data`` (lazily, cached) and
reasons over a game-state dict provided by the caller.

Public API
----------
    analyze(state) -> dict
        Resource report: per-good status + ranked shortage list.
    production_chain_for(good) -> list[dict]
        Ordered buildings (raw -> target) that produce ``good``.
    next_production_building(state) -> str | None
        The single most valuable bare building spec to add next.
    drought_prep(state) -> dict
        Water-buffer sizing and how many tanks to build for the next drought.

Game-state schema (all fields optional; helpers degrade gracefully):
    state["resources"]:   [{"good": str, "stored": num, "days_remaining": num}]
    state["population"]:   {"total": int, "homeless": int, "unemployed": int}
    state["buildings"]["counts"]: {"SpecName.Faction": int, ...}
    state["weather"]["next"]:     {"duration_days": num}
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any, Dict, List, Optional

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# --- status vocabulary --------------------------------------------------------
STATUS_NONE = "none"
STATUS_CRITICAL = "critical"
STATUS_LOW = "low"
STATUS_OK = "ok"
STATUS_SURPLUS = "surplus"

# Ordering for ranking shortages (most urgent first).
_STATUS_RANK = {
    STATUS_NONE: 0,
    STATUS_CRITICAL: 1,
    STATUS_LOW: 2,
    STATUS_OK: 3,
    STATUS_SURPLUS: 4,
}
_SHORTAGE_STATUSES = {STATUS_NONE, STATUS_CRITICAL, STATUS_LOW}

# Critical goods we track. "Food" is a synthetic aggregate over is_food goods.
_CRITICAL_GOODS = ["Log", "Plank", "Water", "Food", "Science"]

# Which resource-list good ids feed the synthetic "Science" bucket.
_SCIENCE_GOODS = {"SciencePoints", "Science"}

# Fallback drought length (days) if state carries no weather info.
_DEFAULT_DROUGHT_DAYS = 3
# Safety margin (days) added on top of the drought when judging water/food.
_HAZARD_MARGIN_DAYS = 2
# Extra margin before a good counts as merely "low".
_LOW_MARGIN_DAYS = 5


# =============================================================================
# Data loading (cached)
# =============================================================================
@lru_cache(maxsize=None)
def _load(name: str) -> Any:
    path = os.path.join(_DATA_DIR, name)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=None)
def _building_name_to_spec() -> Dict[str, str]:
    """Map building display_name -> bare spec id (faction suffix stripped)."""
    out: Dict[str, str] = {}
    for b in _load("buildings.json").get("buildings", []):
        dn = b.get("display_name")
        bare = _strip_faction(b.get("id", ""))
        if dn and bare:
            out[dn] = bare
    return out


@lru_cache(maxsize=None)
def _food_good_ids() -> frozenset:
    ids = {
        g["id"]
        for g in _load("goods.json").get("goods", [])
        if g.get("is_food")
    }
    return frozenset(ids)


@lru_cache(maxsize=None)
def _chains_by_good() -> Dict[str, dict]:
    return {c["good"]: c for c in _load("chains.json").get("chains", [])}


def _strip_faction(spec: str) -> str:
    """'LumberMill.Folktails' -> 'LumberMill'; leave bare names untouched."""
    return spec.split(".", 1)[0] if spec else spec


# =============================================================================
# State accessors
# =============================================================================
def _resource_index(state: Dict) -> Dict[str, dict]:
    """good id -> {stored, days_remaining} (case-insensitive keys added too)."""
    out: Dict[str, dict] = {}
    for r in state.get("resources", []) or []:
        good = r.get("good")
        if good is None:
            continue
        out[good] = r
        out.setdefault(good.lower(), r)
    return out


def _building_count(counts: Dict[str, int], bare_spec: str) -> int:
    """Sum counts across any faction-suffixed keys matching ``bare_spec``.

    ``counts`` keys look like 'LumberMill.Folktails'; ``bare_spec`` is
    'LumberMill'. Also tolerates already-bare keys.
    """
    if not counts:
        return 0
    total = 0
    for key, n in counts.items():
        if _strip_faction(key) == bare_spec:
            try:
                total += int(n)
            except (TypeError, ValueError):
                continue
    return total


def _drought_days(state: Dict) -> float:
    nxt = (state.get("weather") or {}).get("next") or {}
    dur = nxt.get("duration_days")
    if dur is None:
        return float(_DEFAULT_DROUGHT_DAYS)
    try:
        return float(dur)
    except (TypeError, ValueError):
        return float(_DEFAULT_DROUGHT_DAYS)


def _num(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# =============================================================================
# 1. analyze
# =============================================================================
def _aggregate_food(ridx: Dict[str, dict]) -> Optional[dict]:
    """Sum all is_food goods present in the resource list into one bucket."""
    food_ids = _food_good_ids()
    stored = 0.0
    days_vals: List[float] = []
    found = False
    for gid in food_ids:
        r = ridx.get(gid) or ridx.get(gid.lower())
        if r is None:
            continue
        found = True
        stored += _num(r.get("stored"))
        dr = r.get("days_remaining")
        if dr is not None:
            days_vals.append(_num(dr))
    if not found:
        return None
    # Days remaining for the aggregate = worst (soonest) contributor.
    days_remaining = min(days_vals) if days_vals else None
    return {"good": "Food", "stored": stored, "days_remaining": days_remaining}


def _lookup_good(good: str, ridx: Dict[str, dict]) -> Optional[dict]:
    """Resolve a critical-good name to a resource record (with synonyms)."""
    if good == "Food":
        return _aggregate_food(ridx)
    if good == "Science":
        for gid in _SCIENCE_GOODS:
            r = ridx.get(gid) or ridx.get(gid.lower())
            if r is not None:
                return {
                    "good": "Science",
                    "stored": _num(r.get("stored")),
                    "days_remaining": r.get("days_remaining"),
                }
        return None
    r = ridx.get(good) or ridx.get(good.lower())
    if r is None:
        return None
    return {
        "good": good,
        "stored": _num(r.get("stored")),
        "days_remaining": r.get("days_remaining"),
    }


def _classify(good: str, record: Optional[dict], drought_days: float) -> dict:
    """Return {good, status, stored, days_remaining, present} for a good.

    ``present`` is False when the good is absent from the state resource list;
    such goods still get a "none" status in the report but do NOT count as
    shortages (they are simply untracked, not depleted).
    """
    if record is None:
        return {
            "good": good,
            "status": STATUS_NONE,
            "stored": 0.0,
            "days_remaining": 0.0,
            "present": False,
        }
    if _num(record.get("stored")) <= 0:
        return {
            "good": good,
            "status": STATUS_NONE,
            "stored": 0.0,
            "days_remaining": 0.0,
            "present": True,
        }

    stored = _num(record.get("stored"))
    dr_raw = record.get("days_remaining")
    days = _num(dr_raw, default=float("inf")) if dr_raw is not None else float("inf")

    is_hazard_sensitive = good in ("Water", "Food")
    if is_hazard_sensitive:
        crit_threshold = drought_days + _HAZARD_MARGIN_DAYS
        low_threshold = drought_days + _HAZARD_MARGIN_DAYS + _LOW_MARGIN_DAYS
        surplus_threshold = low_threshold + 10
    else:
        crit_threshold = 2.0
        low_threshold = 5.0
        surplus_threshold = 15.0

    if days < crit_threshold:
        status = STATUS_CRITICAL
    elif days < low_threshold:
        status = STATUS_LOW
    elif days < surplus_threshold:
        status = STATUS_OK
    else:
        status = STATUS_SURPLUS

    days_out = None if days == float("inf") else round(days, 2)
    return {
        "good": good,
        "status": status,
        "stored": stored,
        "days_remaining": days_out,
        "present": True,
    }


def analyze(state: Dict) -> Dict[str, Any]:
    """Resource report for the current game state.

    Returns::

        {
          "drought_days": float,
          "goods": {good: {good, status, stored, days_remaining}},
          "shortages": [ {good, status, ...}, ... ]   # most urgent first
        }
    """
    ridx = _resource_index(state)
    drought = _drought_days(state)

    goods: Dict[str, dict] = {}
    for good in _CRITICAL_GOODS:
        record = _lookup_good(good, ridx)
        goods[good] = _classify(good, record, drought)

    shortages = [
        g
        for g in goods.values()
        if g.get("present") and g["status"] in _SHORTAGE_STATUSES
    ]

    def _key(g: dict):
        dr = g["days_remaining"]
        dr = float("inf") if dr is None else dr
        return (_STATUS_RANK[g["status"]], dr)

    shortages.sort(key=_key)

    return {"drought_days": drought, "goods": goods, "shortages": shortages}


# =============================================================================
# 2. production_chain_for
# =============================================================================
def _pick_producer(entry: dict) -> Optional[dict]:
    """Choose the simplest producer (prefer raw / fewest inputs)."""
    producers = entry.get("produced_by") or []
    if not producers:
        return None
    return min(producers, key=lambda p: len(p.get("inputs") or []))


def production_chain_for(good: str) -> List[Dict[str, Any]]:
    """Walk chains.json backward from ``good`` to raw resources.

    Returns an ordered list (dependencies first, target last) of::

        {"good": <good id>, "building": <display name>, "spec": <bare spec|None>}

    Example: production_chain_for("Plank") ->
        [ {"good": "Log",   "building": "Lumberjack Flag", "spec": "LumberjackFlag"},
          {"good": "Plank", "building": "Lumber Mill",     "spec": "LumberMill"} ]
    """
    chains = _chains_by_good()
    name_to_spec = _building_name_to_spec()
    ordered: List[Dict[str, Any]] = []
    seen_goods: set = set()

    def visit(g: str, stack: frozenset) -> None:
        if g in seen_goods or g in stack:
            return
        entry = chains.get(g)
        if entry is None:
            return
        producer = _pick_producer(entry)
        if producer is None:
            return
        # Recurse into inputs first (dependencies before dependents).
        for inp in producer.get("inputs") or []:
            visit(inp, stack | {g})
        if g in seen_goods:
            return
        seen_goods.add(g)
        building = producer.get("building")
        ordered.append(
            {
                "good": g,
                "building": building,
                "spec": name_to_spec.get(building),
            }
        )

    visit(good, frozenset())
    return ordered


# =============================================================================
# 3. next_production_building
# =============================================================================
# Direct fallback: bare building spec for a raw / bootstrap good when the
# chain graph offers no faction-mapped producer.
_GOOD_FALLBACK_SPEC = {
    "Log": "LumberjackFlag",
    "Water": "WaterPump",
    "Food": "GathererFlag",
    "Berries": "GathererFlag",
    "Science": "Inventor",
    "SciencePoints": "Inventor",
    "Plank": "LumberMill",
}


def next_production_building(state: Dict) -> Optional[str]:
    """Return the single most valuable bare building spec to add next.

    Strategy: take the top-ranked shortage, walk its production chain from raw
    resources upward, and return the first building that has zero count (build
    the deepest missing input first). If every building in the chain already
    exists, return the target's own producer so it can be scaled up.
    """
    report = analyze(state)
    shortages = report["shortages"]
    if not shortages:
        return None

    counts = (state.get("buildings") or {}).get("counts") or {}

    for shortage in shortages:
        good = shortage["good"]
        spec = _first_missing_spec(good, counts)
        if spec is not None:
            return spec
    return None


def _first_missing_spec(good: str, counts: Dict[str, int]) -> Optional[str]:
    """First not-yet-built building spec in ``good``'s chain (raw -> target)."""
    chain = production_chain_for(good)

    if not chain:
        # No chain graph entry (e.g. synthetic "Food"): use direct fallback.
        fallback = _GOOD_FALLBACK_SPEC.get(good)
        if fallback and _building_count(counts, fallback) == 0:
            return fallback
        return fallback  # allow scale-up even if one exists

    last_spec: Optional[str] = None
    for step in chain:
        spec = step["spec"] or _GOOD_FALLBACK_SPEC.get(step["good"])
        if spec is None:
            continue
        last_spec = spec
        if _building_count(counts, spec) == 0:
            return spec

    # Everything already built: scale up the target's own producer.
    return last_spec


# =============================================================================
# 4. drought_prep
# =============================================================================
def _tank_capacity(spec_id: str) -> float:
    """Water capacity for a tank spec id, from mechanics or building notes."""
    caps = {
        "SmallTank": 30.0,
        "MediumTank": 300.0,
        "LargeTank": 1200.0,
    }
    return caps.get(_strip_faction(spec_id), 0.0)


def drought_prep(state: Dict) -> Dict[str, Any]:
    """Compute water-buffer needs for the upcoming drought.

    Uses mechanics_water.json for daily water use per beaver and tank
    capacities. Returns the required buffer, current tank buffer, the deficit,
    and how many additional SmallTank / LargeTank to build.
    """
    water = _load("mechanics_water.json")
    per_day = (
        (water.get("water_consumption") or {})
        .get("beaver_water_per_day", {})
        .get("value")
    )
    if per_day is None:
        # buffer_rule_of_thumb fallback
        per_day = (
            (water.get("water_storage") or {})
            .get("buffer_rule_of_thumb", {})
            .get("water_per_beaver_per_drought_day", 2.25)
        )
    per_day = _num(per_day, 2.25)

    population = _num((state.get("population") or {}).get("total"), 0.0)
    drought_days = _drought_days(state)
    margin_days = _HAZARD_MARGIN_DAYS

    required_buffer = population * per_day * (drought_days + margin_days)

    counts = (state.get("buildings") or {}).get("counts") or {}
    small_n = _building_count(counts, "SmallTank")
    medium_n = _building_count(counts, "MediumTank")
    large_n = _building_count(counts, "LargeTank")

    current_buffer = (
        small_n * _tank_capacity("SmallTank")
        + medium_n * _tank_capacity("MediumTank")
        + large_n * _tank_capacity("LargeTank")
    )

    # Any stored water already counts toward the buffer.
    ridx = _resource_index(state)
    stored_water = _num((ridx.get("Water") or ridx.get("water") or {}).get("stored"), 0.0)

    deficit = required_buffer - current_buffer
    if deficit < 0:
        deficit = 0.0

    small_cap = _tank_capacity("SmallTank")
    large_cap = _tank_capacity("LargeTank")

    # Prefer a few SmallTanks for a small deficit, else LargeTanks.
    build_small = 0
    build_large = 0
    if deficit > 0:
        if deficit <= small_cap * 4:
            import math

            build_small = int(math.ceil(deficit / small_cap)) if small_cap else 0
        else:
            import math

            build_large = int(math.ceil(deficit / large_cap)) if large_cap else 0

    return {
        "population": population,
        "daily_water_use_per_beaver": per_day,
        "drought_days": drought_days,
        "margin_days": margin_days,
        "required_buffer": round(required_buffer, 2),
        "current_buffer": round(current_buffer, 2),
        "stored_water": round(stored_water, 2),
        "deficit": round(deficit, 2),
        "tanks_present": {"SmallTank": small_n, "MediumTank": medium_n, "LargeTank": large_n},
        "build": {"SmallTank": build_small, "LargeTank": build_large},
    }


# =============================================================================
# Tests
# =============================================================================
import unittest  # noqa: E402


class ResourceManagerTests(unittest.TestCase):
    def _state(self, resources, counts=None, pop=10, drought=3):
        return {
            "resources": resources,
            "population": {"total": pop, "homeless": 0, "unemployed": 0},
            "buildings": {"counts": counts or {}},
            "weather": {"next": {"duration_days": drought}},
        }

    def test_no_logs_no_lumberjack_suggests_lumberjack(self):
        state = self._state(
            resources=[{"good": "Log", "stored": 0, "days_remaining": 0}],
            counts={},
        )
        self.assertEqual(next_production_building(state), "LumberjackFlag")

    def test_plank_shortage_lumberjack_built_suggests_lumbermill(self):
        state = self._state(
            resources=[
                {"good": "Log", "stored": 50, "days_remaining": 30},
                {"good": "Plank", "stored": 1, "days_remaining": 0.5},
            ],
            counts={"LumberjackFlag.Folktails": 1},
        )
        self.assertEqual(next_production_building(state), "LumberMill")

    def test_analyze_flags_none_and_critical(self):
        state = self._state(
            resources=[
                {"good": "Log", "stored": 0, "days_remaining": 0},
                {"good": "Water", "stored": 20, "days_remaining": 1},
                {"good": "Berries", "stored": 200, "days_remaining": 40},
            ],
            drought=3,
        )
        report = analyze(state)
        self.assertEqual(report["goods"]["Log"]["status"], STATUS_NONE)
        # Water days_remaining 1 < drought(3)+2 => critical
        self.assertEqual(report["goods"]["Water"]["status"], STATUS_CRITICAL)
        # Food aggregated from Berries, healthy
        self.assertIn(report["goods"]["Food"]["status"], (STATUS_OK, STATUS_SURPLUS))
        # Shortages ranked: Log (none) before Water (critical)
        self.assertEqual(report["shortages"][0]["good"], "Log")

    def test_water_critical_relative_to_drought_length(self):
        # Same stored/days, but a longer drought makes water critical.
        res = [{"good": "Water", "stored": 100, "days_remaining": 6}]
        short = analyze(self._state(res, drought=2))
        long = analyze(self._state(res, drought=8))
        self.assertIn(short["goods"]["Water"]["status"], (STATUS_OK, STATUS_LOW, STATUS_SURPLUS))
        self.assertEqual(long["goods"]["Water"]["status"], STATUS_CRITICAL)

    def test_production_chain_for_plank(self):
        chain = production_chain_for("Plank")
        goods = [c["good"] for c in chain]
        self.assertEqual(goods, ["Log", "Plank"])
        specs = [c["spec"] for c in chain]
        self.assertEqual(specs, ["LumberjackFlag", "LumberMill"])

    def test_production_chain_for_bread_is_ordered(self):
        chain = production_chain_for("Bread")
        goods = [c["good"] for c in chain]
        # Bread depends on WheatFlour (<-Wheat) and Log; target is last.
        self.assertEqual(goods[-1], "Bread")
        self.assertLess(goods.index("Wheat"), goods.index("WheatFlour"))
        self.assertLess(goods.index("WheatFlour"), goods.index("Bread"))

    def test_drought_prep_water_poor_builds_tanks(self):
        # 10 beavers, 8-day drought, no tanks -> must build storage.
        state = self._state(
            resources=[{"good": "Water", "stored": 10, "days_remaining": 2}],
            counts={},
            pop=10,
            drought=8,
        )
        prep = drought_prep(state)
        self.assertGreater(prep["required_buffer"], 0)
        self.assertEqual(prep["current_buffer"], 0)
        self.assertGreater(prep["deficit"], 0)
        total_new = prep["build"]["SmallTank"] + prep["build"]["LargeTank"]
        self.assertGreater(total_new, 0)

    def test_drought_prep_well_stocked_builds_nothing(self):
        state = self._state(
            resources=[{"good": "Water", "stored": 500, "days_remaining": 30}],
            counts={"LargeTank.Folktails": 2},
            pop=10,
            drought=3,
        )
        prep = drought_prep(state)
        self.assertEqual(prep["deficit"], 0)
        self.assertEqual(prep["build"]["SmallTank"], 0)
        self.assertEqual(prep["build"]["LargeTank"], 0)

    def test_building_count_matches_faction_suffix(self):
        counts = {"LumberMill.Folktails": 2, "LumberMill.Ironteeth": 1}
        self.assertEqual(_building_count(counts, "LumberMill"), 3)

    def test_empty_state_is_safe(self):
        self.assertIsNone(next_production_building({}))
        report = analyze({})
        self.assertEqual(report["goods"]["Log"]["status"], STATUS_NONE)


def _demo() -> None:
    state = {
        "resources": [
            {"good": "Log", "stored": 4, "days_remaining": 1.0},
            {"good": "Plank", "stored": 0, "days_remaining": 0},
            {"good": "Water", "stored": 60, "days_remaining": 5},
            {"good": "Berries", "stored": 120, "days_remaining": 12},
            {"good": "SciencePoints", "stored": 0, "days_remaining": 0},
        ],
        "population": {"total": 12, "homeless": 0, "unemployed": 2},
        "buildings": {"counts": {"LumberjackFlag.Folktails": 1, "SmallTank.Folktails": 1}},
        "weather": {"next": {"duration_days": 6}},
    }
    print("== analyze ==")
    rpt = analyze(state)
    for g in rpt["goods"].values():
        print(f"  {g['good']:8} {g['status']:8} stored={g['stored']} days={g['days_remaining']}")
    print("  shortages:", [s["good"] for s in rpt["shortages"]])
    print("== production_chain_for('Plank') ==")
    for step in production_chain_for("Plank"):
        print("  ", step)
    print("== next_production_building ==")
    print("  ", next_production_building(state))
    print("== drought_prep ==")
    for k, v in drought_prep(state).items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        _demo()
    else:
        _demo()
        print("\n== running tests ==")
        unittest.main(argv=[sys.argv[0], "-v"], exit=False)
