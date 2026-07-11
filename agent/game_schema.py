"""Database-driven action + feature space for the decision model.

The bootstrap-only hand-coded space (14 actions, ~47 features) can't play past the first
few buildings. This module DERIVES the action space and the feature vocabulary from the
game database (agent/data/{buildings,goods,recipes,chains,needs,tech_tree}.json) so every
building, good, production chain, need, power and tech tier is represented - and it stays
in sync with the DB as we add knowledge. Features are introduced iteratively by PROGRESSION
TIER (science gating), so the model sees mid/late-game structure only once it's relevant.

Public:
  actions()                -> list[goal_id]   (build_<snake> for every gameplay building + verbs)
  action_to_spec(goal_id)  -> spec name or None
  spec_to_action(spec)     -> goal_id
  building_tier(spec)      -> 'start'|'early'|'mid'|'late'   (by science cost / prereqs)
  feature_strings(state)   -> list[str]        (DB-grounded symbolic features)
  GOODS, PRODUCERS, CATEGORIES, WELLBEING_NEEDS
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from typing import Dict, List, Optional

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Categories that are actual gameplay decisions (exclude pure cosmetic/wiring/paths and the
# reserve/dev pseudo-buildings). Paths are handled by the trunk pather, not the policy.
_GAMEPLAY_CATEGORIES = {
    "district", "food", "forestry", "housing", "industry",
    "power", "science", "storage", "water", "monuments",
}
_SKIP_IDS = {"DistrictCenter", "DevPowerGenerator", "ReservePile", "ReserveTank",
             "ReserveWarehouse", "AncientAquiferDrill"}

_VERB_ACTIONS = ["designate_cutting", "designate_planting", "demolish_unreachable",
                 "advance_time"]


def _load(name: str) -> dict:
    with open(os.path.join(_DATA, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _snake(spec: str) -> str:
    s = spec.split(".")[0]
    s = re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()
    return s


@lru_cache(maxsize=1)
def _buildings() -> List[dict]:
    return _load("buildings.json")["buildings"]


@lru_cache(maxsize=1)
def _goods() -> List[dict]:
    return _load("goods.json")["goods"]


@lru_cache(maxsize=1)
def _chains() -> List[dict]:
    return _load("chains.json")["chains"]


@lru_cache(maxsize=1)
def _by_spec() -> Dict[str, dict]:
    out = {}
    for b in _buildings():
        out[b["id"].split(".")[0]] = b
    return out


@lru_cache(maxsize=1)
def _gameplay_specs() -> List[str]:
    specs = []
    for b in _buildings():
        spec = b["id"].split(".")[0]
        if spec in _SKIP_IDS:
            continue
        if b.get("category") in _GAMEPLAY_CATEGORIES:
            specs.append(spec)
    return specs


def building_tier(spec: str) -> str:
    """Progression tier from science cost (0=start, <=150 early, <=600 mid, else late)."""
    b = _by_spec().get(spec.split(".")[0])
    sci = 0
    if b:
        try:
            sci = int(b.get("science_cost") or 0)
        except Exception:
            sci = 0
    if sci <= 0:
        return "start"
    if sci <= 150:
        return "early"
    if sci <= 600:
        return "mid"
    return "late"


@lru_cache(maxsize=1)
def _action_specs() -> Dict[str, str]:
    """goal_id -> spec for every gameplay building."""
    m = {}
    for spec in _gameplay_specs():
        m["build_" + _snake(spec)] = spec
    return m


@lru_cache(maxsize=1)
def actions() -> List[str]:
    """The full action space: build_<x> for each gameplay building + verb actions."""
    return list(_action_specs().keys()) + list(_VERB_ACTIONS)


def action_to_spec(goal_id: str) -> Optional[str]:
    return _action_specs().get(goal_id)


@lru_cache(maxsize=1)
def _spec_to_action() -> Dict[str, str]:
    return {spec: gid for gid, spec in _action_specs().items()}


def spec_to_action(spec: str) -> Optional[str]:
    return _spec_to_action().get(spec.split(".")[0])


# Goods that are consumables we track days_remaining for.
_FOOD_GOODS = None
_PRODUCERS = None


@lru_cache(maxsize=1)
def _food_goods() -> List[str]:
    return [g["id"] for g in _goods() if g.get("is_food")]


@lru_cache(maxsize=1)
def _producers() -> Dict[str, List[str]]:
    """good -> list of building specs that produce it (from chains)."""
    out: Dict[str, List[str]] = {}
    name_to_spec = {b.get("display_name", ""): b["id"].split(".")[0] for b in _buildings()}
    for c in _chains():
        good = c.get("good")
        specs = []
        for p in c.get("produced_by", []) or []:
            bname = p.get("building")
            specs.append(name_to_spec.get(bname, bname))
        if good:
            out[good] = specs
    return out


@lru_cache(maxsize=1)
def _wellbeing_needs() -> List[str]:
    n = _load("needs.json").get("wellbeing_needs") or []
    return [x.get("need") if isinstance(x, dict) else str(x) for x in n]


# ---------------------------------------------------------------------------
# state helpers (mirror planner accessors, kept local so this module is standalone)
# ---------------------------------------------------------------------------

def _count(state: dict, spec: str) -> int:
    counts = (((state or {}).get("buildings") or {}).get("counts") or {})
    low = spec.lower()
    total = 0
    for k, v in counts.items():
        name = str(k).lower()
        if name == low or name.startswith(low + "."):
            try:
                total += int(v)
            except Exception:
                pass
    return total


def _resource(state: dict, good: str) -> Optional[dict]:
    for item in (state.get("resources") or []) if isinstance(state, dict) else []:
        if isinstance(item, dict) and str(item.get("good", "")).lower() == good.lower():
            return item
    return None


def _bucket(v: float, edges, names) -> str:
    for e, n in zip(edges, names):
        if v < e:
            return n
    return names[-1]


def feature_strings(state: dict) -> List[str]:
    """DB-grounded symbolic features covering the whole economy. Features for a good/
    building/need are emitted regardless of tier; a fresh colony simply reports 'none'/'no'
    for the mid/late ones, and they light up as the colony progresses."""
    f: List[str] = []

    # --- population / drought context ---
    pop = (state.get("population") or {}) if isinstance(state, dict) else {}
    total = int(pop.get("total") or 0)
    f.append("pop=" + _bucket(total, (6, 15, 25, 35), ("tiny", "small", "mid", "big", "huge")))
    f.append("homeless=" + ("yes" if int(pop.get("homeless") or 0) > 0 else "no"))
    free_beds = pop.get("free_beds")
    if free_beds is not None:
        f.append("free_beds=" + ("yes" if int(free_beds or 0) > 0 else "no"))
    weather = (state.get("weather") or {}) if isinstance(state, dict) else {}
    drought = float((weather.get("next") or {}).get("duration_days") or 0)
    f.append("drought_len=" + _bucket(drought, (3, 6), ("short", "mid", "long")))

    # --- per key good: stock + days_remaining + has-producer ---
    key_goods = ["Log", "Plank", "Gear", "Water", "SciencePoints", "MetalBlock", "Paper"]
    for good in key_goods:
        r = _resource(state, good) or {}
        stock = float(r.get("stored") or 0)
        f.append("stock_%s=" % good.lower() + _bucket(stock, (1, 15, 60), ("none", "low", "ok", "high")))
    # consumables tracked by days_remaining
    water_days = float((_resource(state, "Water") or {}).get("days_remaining") or 0)
    f.append("water_days=" + _bucket(water_days, (2, 4, 10), ("crit", "low", "ok", "high")))
    food_days = 0.0
    for g in _food_goods():
        r = _resource(state, g)
        if r and r.get("days_remaining") is not None:
            food_days = max(food_days, float(r.get("days_remaining") or 0))
    f.append("food_days=" + _bucket(food_days, (2, 4, 10), ("crit", "low", "ok", "high")))

    # --- production capacity: is a producer built for each manufactured good? ---
    producers = _producers()
    for good in ["Plank", "Gear", "SciencePoints", "Bread", "MetalBlock", "Paper"]:
        built = any(_count(state, spec) > 0 for spec in producers.get(good, []))
        f.append("makes_%s=" % good.lower() + ("yes" if built else "no"))

    # --- per gameplay category: how many buildings do we have ---
    cat_counts: Dict[str, int] = {}
    for spec in _gameplay_specs():
        b = _by_spec().get(spec)
        cat = b.get("category") if b else None
        if cat:
            cat_counts[cat] = cat_counts.get(cat, 0) + (_count(state, spec) > 0)
    for cat in sorted(_GAMEPLAY_CATEGORIES):
        c = cat_counts.get(cat, 0)
        f.append("cat_%s=" % cat + _bucket(c, (1, 3), ("none", "some", "many")))

    # --- power balance (from building power specs) ---
    produced = consumed = 0.0
    for spec in _gameplay_specs():
        b = _by_spec().get(spec) or {}
        n = _count(state, spec)
        if not n:
            continue
        power = b.get("power") or {}
        try:
            produced += float(power.get("produced_hp") or 0) * n
            consumed += float(power.get("consumed_hp") or 0) * n
        except Exception:
            pass
    if produced or consumed:
        f.append("power=" + ("ok" if produced >= consumed else "deficit"))
    else:
        f.append("power=none")

    # --- well-being coverage: any source building for each need (coarse: campfire/decor/etc) ---
    f.append("has_science=" + ("yes" if any(_count(state, s) > 0 for s in producers.get("SciencePoints", [])) else "no"))
    f.append("has_storage=" + ("yes" if cat_counts.get("storage", 0) > 0 else "no"))
    f.append("has_power=" + ("yes" if cat_counts.get("power", 0) > 0 else "no"))

    return f


__all__ = [
    "actions", "action_to_spec", "spec_to_action", "building_tier",
    "feature_strings",
]
