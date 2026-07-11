"""Generate the labeled training dataset for the decision heads.

Analog of NLP_2.0's `intent_classifier_test.py` (which built final_dataset_raw.json
from a seed + augmentation). Here we sweep a realistic grid of game states across
the bootstrap trajectory PLUS a set of targeted full-economy states (Task 5a),
label each with the expert Oracle (behavioral cloning), harvest any real states
recorded in journals, dedup by feature vector, and write:

  data/decision_dataset.json  - [{features:[str], label:goal_id}]
  data/decision_vocab.json    - {vocab:[str]}  (the fixed feature vocabulary)
  data/decision_labels.json   - [goal_id, ...] (the label index order)

Run:  python -m agent.nlp.dataset
Pure-stdlib + planner; no torch/sklearn needed to build the dataset.

STATE SOURCES (see _iter_states):
  * _bootstrap_grid_states  - the original LOGS x WATER x FOOD x POP x DROUGHT
    sweep over `_STAGES` (bare survival bootstrap trajectory: lumberjack ->
    water pump -> tanks -> gatherer -> farm -> lodge -> warehouse -> inventor ->
    forester). ~13k states; this is the slow part of a full regenerate.
  * _unreachable_states     - a few states with one unreachable building, so
    demolish_unreachable is learnable (see the buildings.detail note below).
  * _economy_family_states  - Task 5a: targeted, hand-verified states that each
    trigger ONE specific full-economy goal family the expert planner can emit
    once bootstrap is satisfied: production chains (economy.producer_plan),
    science scaling, well-being amenities, power, storage pressure, and
    drought tank sizing. Each state was verified against the live Oracle
    (see docs/kb or the Task 5a report) rather than guessed, because the
    planner's tie-break rules (chain depth, then recommended_order, then spec
    name) are not obvious from reading a single emitter in isolation.

A note on `buildings.detail` vs `buildings.list`: the REAL bridge/controller
path (agent/controller.py, agent/play.py) always reads unreachable-building
info from `state["buildings"]["list"]` and passes it to planner.plan_report
explicitly as `buildings_detail`. But Oracle.label calls
`planner.plan_report(state, map_data, resources=...)` WITHOUT that explicit
argument (deliberately - see labeler.py), so `planner._building_details` falls
back to reading `state["buildings"]["detail"]` (or top-level
`buildings_detail`) instead. `_inject_unreachable` below writes to BOTH keys:
`detail` because that is what Oracle.label's code path actually reads, and
`list` so the synthetic state also looks like a realistic bridge snapshot.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from agent.nlp import features as feat
from agent.nlp.labeler import Oracle

_ROOT = Path(__file__).resolve().parent.parent
_FIXTURES = _ROOT / "fixtures"
_DATA = _ROOT / "data"

# ---------------------------------------------------------------------------
# state construction helpers
# ---------------------------------------------------------------------------

def _base_state() -> dict:
    with (_FIXTURES / "state_fresh.json").open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _set_resource(state: dict, good: str, stored: float, days: float,
                   capacity: Optional[float] = None) -> None:
    """Set a resource's stored amount + days_remaining, and optionally its
    storage `capacity` (needed for economy.storage_pressure - see
    _set_capacity_pressure below; state_fresh.json's own resource entries
    already carry a "capacity" key, matching the real bridge payload)."""
    resources = state.setdefault("resources", [])
    for item in resources:
        if str(item.get("good", "")).lower() == good.lower():
            item["stored"] = stored
            item["all_stock"] = stored
            item["days_remaining"] = days
            if capacity is not None:
                item["capacity"] = capacity
            return
    entry = {"good": good, "stored": stored, "all_stock": stored, "days_remaining": days}
    if capacity is not None:
        entry["capacity"] = capacity
    resources.append(entry)


def _set_capacity_pressure(state: dict, good: str, ratio: float, capacity: float = 100.0,
                            days: float = 30.0) -> None:
    """Set ``good`` to ``ratio`` of ``capacity`` stored (economy.storage_pressure
    fires at >= STORAGE_PRESSURE_THRESHOLD, currently 0.85). ``days`` defaults
    to a comfortably-high 30: the storage family is gated behind
    _survival_secure, which for Water/Food reads THIS SAME resource entry's
    days_remaining - overwriting it to 0 here would silently break survival_secure
    for exactly the "Water is near capacity" case and starve every _survival_secure
    -gated emitter (storage included) of a goal, so the label degrades to the
    Oracle's advance_time fallback instead of the intended storage goal."""
    _set_resource(state, good, ratio * capacity, days, capacity=capacity)


def _set_science(state: dict, stored: float) -> None:
    """SciencePoints stock (economy.stored_science / unlockable_now's science
    gate). Absent from the base fixture entirely, so every pre-Task-5a
    synthetic state read as stored=0 - meaning every science-gated producer,
    amenity or power tier was permanently locked. This is the dimension that
    lets the sweep actually reach them."""
    _set_resource(state, "SciencePoints", stored, 0.0)


def _set_pop(state: dict, total: int, homeless: int, free_beds: Optional[int] = None) -> None:
    """Population total/homeless, and optionally free_beds (population_growth
    housing headroom - planner._append_wellbeing_goals's free-bed Lodge check).
    state_fresh.json already carries free_beds=0; when free_beds is left at 0
    across a whole "survival secure, growing" state, that stale 0 makes the
    free-bed Lodge goal fire ahead of every other Task-3c growth goal, so the
    Task 5a well-being/power/storage/drought states must set free_beds > 0
    explicitly to let the OTHER growth goals surface."""
    pop = state.setdefault("population", {})
    pop["total"] = total
    pop["homeless"] = homeless
    if free_beds is not None:
        pop["free_beds"] = free_beds


def _set_counts(state: dict, counts: Dict[str, int]) -> None:
    state.setdefault("buildings", {})["counts"] = dict(counts)


def _set_drought(state: dict, duration_days: float) -> None:
    weather = state.setdefault("weather", {})
    weather["next"] = {"duration_days": duration_days}


def _inject_unreachable(state: dict) -> None:
    """Add one unreachable building. Written to BOTH `buildings.detail` (what
    Oracle.label's plan_report call actually reads - see the module docstring)
    and `buildings.list` (what the real bridge/controller path uses), so the
    state is simultaneously realistic AND genuinely produces demolish_unreachable
    through Oracle.label. Verified: Oracle().label(state) == "demolish_unreachable"
    after this fix (see agent/nlp/test_dataset_coverage.py)."""
    building = {"spec": "WaterPump", "status": "finished", "reachable": False,
                "x": 4, "y": 5, "z": 6}
    buildings = state.setdefault("buildings", {})
    buildings.setdefault("list", []).append(dict(building))
    buildings.setdefault("detail", []).append(dict(building))


# Realistic bootstrap building configurations (bare spec keys; planner matches prefix).
_STAGES: List[Dict[str, int]] = [
    {},
    {"LumberjackFlag": 1},
    {"LumberjackFlag": 1, "GathererFlag": 1},
    {"LumberjackFlag": 1, "GathererFlag": 1, "WaterPump": 1},
    {"LumberjackFlag": 1, "GathererFlag": 1, "WaterPump": 1, "SmallTank": 2},
    {"LumberjackFlag": 1, "GathererFlag": 1, "WaterPump": 1, "SmallTank": 4},
    {"LumberjackFlag": 1, "GathererFlag": 1, "WaterPump": 1, "SmallTank": 4, "Lodge": 1},
    {"LumberjackFlag": 1, "GathererFlag": 1, "WaterPump": 1, "SmallTank": 4, "Lodge": 1,
     "EfficientFarmHouse": 1},
    {"LumberjackFlag": 1, "GathererFlag": 1, "WaterPump": 1, "SmallTank": 4, "Lodge": 1,
     "EfficientFarmHouse": 1, "SmallWarehouse": 1},
    {"LumberjackFlag": 1, "GathererFlag": 1, "WaterPump": 1, "SmallTank": 4, "Lodge": 1,
     "EfficientFarmHouse": 1, "SmallWarehouse": 1, "Inventor": 1},
    {"LumberjackFlag": 1, "GathererFlag": 1, "WaterPump": 1, "SmallTank": 5, "Lodge": 2,
     "EfficientFarmHouse": 1, "SmallWarehouse": 1, "Inventor": 1, "Forester": 1},
]

_LOGS = [0, 6, 12, 20, 40]
_WATER_DAYS = [0.5, 2.0, 5.0, 15.0]
_FOOD_DAYS = [0.5, 2.0, 5.0, 15.0]
_POP = [(5, 0), (5, 4), (12, 0), (12, 4), (20, 0)]
_DROUGHT = [1.0, 3.0, 6.0]


def _bootstrap_grid_states() -> Iterable[dict]:
    """The original LOGS x WATER x FOOD x POP x DROUGHT sweep over _STAGES -
    unchanged from pre-Task-5a. Every state here reads SciencePoints=0 (absent
    from the fixture, _set_science never called), so it stays confined to the
    bare-survival bootstrap trajectory; this is intentional - full-economy
    coverage comes from _economy_family_states instead of blowing up this
    already-~13k-state grid with an extra science dimension."""
    for counts in _STAGES:
        for logs in _LOGS:
            for wd in _WATER_DAYS:
                for fd in _FOOD_DAYS:
                    for total, homeless in _POP:
                        for drought in _DROUGHT:
                            s = _base_state()
                            _set_counts(s, counts)
                            _set_resource(s, "Log", logs, logs / 3.0)
                            _set_resource(s, "Water", wd * 5, wd)
                            _set_resource(s, "Food", fd * 5, fd)
                            _set_pop(s, total, homeless)
                            _set_drought(s, drought)
                            yield s


def _bootstrap_smoke_states() -> Iterable[dict]:
    """One representative state per _STAGES entry at fixed mid-range resource
    levels - a FAST slice of the bootstrap trajectory (11 states) used by the
    coverage test, which needs a couple of bootstrap-goal examples without
    paying for the full _bootstrap_grid_states cross product."""
    for counts in _STAGES:
        s = _base_state()
        _set_counts(s, counts)
        _set_resource(s, "Log", 20, 6.0)
        _set_resource(s, "Water", 30, 6.0)
        _set_resource(s, "Food", 30, 6.0)
        _set_pop(s, 8, 2)
        _set_drought(s, 3.0)
        yield s


def _unreachable_states() -> Iterable[dict]:
    """A few explicit unreachable-building states so demolish_unreachable is
    learnable - both against the bare bootstrap trajectory (as before) and
    against a full-economy backdrop, so the label is not confounded with "no
    economy built yet" (demolish always wins regardless: it is checked first
    in planner.analyze and short-circuits Oracle.label's selection loop)."""
    for counts in _STAGES[3:6]:
        s = _base_state()
        _set_counts(s, counts)
        _set_resource(s, "Log", 20, 6)
        _set_resource(s, "Water", 10, 2)
        _set_resource(s, "Food", 10, 2)
        _set_pop(s, 8, 0)
        _set_drought(s, 3.0)
        _inject_unreachable(s)
        yield s

    # One more against a full-economy backdrop, so the label is not confounded
    # with "no economy built yet" (demolish is checked first in analyze() and
    # always wins regardless). A fully-built economy has no feature signature
    # of its own beyond resource/pop buckets (decoration/amenity counts are not
    # tracked by game_schema.feature_strings at all - see the Task 5a report),
    # so this collides with several _economy_family_states targets unless it
    # is pushed into a water_days bucket ("ok", [4,10)) none of them use - they
    # all default to a comfortable 30 ("high", >=10).
    s = _base_state()
    _set_counts(s, _mix(_BOOTSTRAP_DONE, _INDUSTRY_ALL, _AMENITIES_ALL, {"PowerWheel": 20}))
    _set_resource(s, "Log", 120, 30.0)
    _set_resource(s, "Water", 150, 8.0)
    _set_resource(s, "Food", 150, 30.0)
    _set_science(s, 999)
    _set_pop(s, 15, 0, free_beds=5)
    _set_drought(s, 3.0)
    _inject_unreachable(s)
    yield s


# ---------------------------------------------------------------------------
# Task 5a: targeted full-economy states, one (or a small sweep) per goal
# family the expert planner can emit once bootstrap survival is satisfied.
#
# Every recipe below was empirically verified against the live Oracle (not just
# reasoned about) because economy.producer_plan's ordering - chain depth, then
# recommended_order, then spec name - and the well-being/storage "cheapest
# curated source" logic both have non-obvious tie-breaks. See the Task 5a
# report for the verification transcript.
# ---------------------------------------------------------------------------

# Bootstrap fully satisfied: no bootstrap goal (lumberjack/pump/tanks/gatherer/
# farm/lodge/warehouse/inventor/forester) is outstanding, so analyze() falls
# straight through to the Task-3 economy emitters.
_BOOTSTRAP_DONE: Dict[str, int] = {
    "LumberjackFlag": 1, "GathererFlag": 1, "WaterPump": 1, "SmallTank": 6,
    "Lodge": 2, "EfficientFarmHouse": 1, "SmallWarehouse": 1, "Inventor": 1,
    "Forester": 1,
}

# The full "always-on construction demand" industry chain (Log/Plank/Gear/
# MetalBlock/TreatedPlank/PunchCards are demanded by the standing "construction"
# terminal regardless of what is built - see economy._ACTIVE_TERMINALS). With
# all 8 built, economy.producer_plan has nothing left to propose, which is what
# lets well-being/power/storage/drought states isolate their OWN family.
_INDUSTRY_ALL: Dict[str, int] = {
    "LumberMill": 1, "GearWorkshop": 1, "Smelter": 1, "PaperMill": 1,
    "ScavengerFlag": 1, "TappersShack": 1, "WoodWorkshop": 1, "PrintingPress": 1,
}

# The cheapest curated source per well-being need (economy._WELLBEING_SOURCES);
# ContemplationSpot/Lantern/Agora/MudPit are never the curated "cheapest" pick
# for their need (see the Task 5a report's blind-spot list), so are not needed
# here to fully cover the well-being family.
_AMENITIES_ALL: Dict[str, int] = {"Shrub": 1, "Campfire": 1, "Shower": 1, "Lido": 1}


def _mix(*counts: Dict[str, int]) -> Dict[str, int]:
    merged: Dict[str, int] = {}
    for c in counts:
        merged.update(c)
    return merged


def _economy_state(counts, sci=0, logs=120, water_days=30.0, food_days=30.0,
                    pop=15, homeless=0, free_beds=5, drought_days=3.0) -> dict:
    s = _base_state()
    _set_counts(s, counts)
    _set_resource(s, "Log", logs, logs / 3.0 if logs else 0.0)
    _set_resource(s, "Water", water_days * 5, water_days)
    _set_resource(s, "Food", food_days * 5, food_days)
    _set_science(s, sci)
    _set_pop(s, pop, homeless, free_beds=free_beds)
    _set_drought(s, drought_days)
    return s


def _production_chain_states() -> Iterable[dict]:
    """economy.producer_plan's raw->refined tiers, isolated one spec at a time
    by pre-building every lower-tier-or-alphabetically-earlier competitor
    (chain depth: {ScavengerFlag,TappersShack} < {LumberMill,PaperMill,Smelter}
    < {GearWorkshop,PrintingPress,WoodWorkshop}; ties break on spec name).

    NOTE on `logs=`: game_schema.feature_strings only tracks SciencePoints in 4
    coarse buckets (none/low/ok/high at 1/15/60), and every target past
    lumber_mill needs sci>=100 - all land in the SAME "high" bucket. Two of
    these targets (gear_workshop, paper_mill) are otherwise feature-identical
    (same industry-category bucket, same makes_* flags), so without a second
    lever they collide under dataset.build()'s feature-vector dedup and one
    silently loses its row. `logs=` (also bucketed, and functionally inert
    here since every target's own cost_logs is well under any value used) is
    that second lever - see the Task 5a report for the full collision map."""
    for pop in (12, 22):
        yield _economy_state(_BOOTSTRAP_DONE, sci=0, pop=pop)  # -> build_lumber_mill

        c = _mix(_BOOTSTRAP_DONE, {"LumberMill": 1})
        yield _economy_state(c, sci=150, pop=pop, logs=20)  # -> build_gear_workshop

        c = _mix(_BOOTSTRAP_DONE, {"LumberMill": 1, "PaperMill": 1, "ScavengerFlag": 1})
        yield _economy_state(c, sci=400, pop=pop)  # -> build_smelter

        c = _mix(_BOOTSTRAP_DONE, {"LumberMill": 1, "ScavengerFlag": 1})
        yield _economy_state(c, sci=260, pop=pop)  # -> build_paper_mill (logs=120 default,
        # deliberately different stock_log bucket from gear_workshop's logs=20 above)

        yield _economy_state(_BOOTSTRAP_DONE, sci=250, pop=pop)  # -> build_scavenger_flag

        c = _mix(_BOOTSTRAP_DONE, {"ScavengerFlag": 1})
        yield _economy_state(c, sci=500, pop=pop)  # -> build_tappers_shack

        c = _mix(_BOOTSTRAP_DONE, {"ScavengerFlag": 1, "LumberMill": 1, "PaperMill": 1,
                                    "Smelter": 1, "GearWorkshop": 1})
        yield _economy_state(c, sci=400, pop=pop)  # -> build_printing_press

        c = _mix(_BOOTSTRAP_DONE, {"ScavengerFlag": 1, "TappersShack": 1, "LumberMill": 1,
                                    "PaperMill": 1, "Smelter": 1, "GearWorkshop": 1,
                                    "PrintingPress": 1})
        yield _economy_state(c, sci=1000, pop=pop, logs=25)  # -> build_wood_workshop
        # (logs=25 - see _power_states' wind/large-wind-turbine states, which are
        # otherwise feature-identical to this one at the default logs=120)

    # science-scaling: Inventor already built, the one always-free producer
    # (LumberMill) cleared, everything else still locked and stored science
    # below the cheapest locked item -> "2nd Inventor" (same build_inventor id).
    c = _mix(_BOOTSTRAP_DONE, {"LumberMill": 1})
    yield _economy_state(c, sci=0, pop=15)  # -> build_inventor


def _wellbeing_states() -> Iterable[dict]:
    """Task-3c growth goals: gated behind _survival_secure (all these states
    satisfy it) and, for the free-bed case, free_beds<=0. Growing population
    (12-28) per the task brief; industry pre-cleared via _INDUSTRY_ALL so a
    production-chain goal never preempts."""
    base = _mix(_BOOTSTRAP_DONE, _INDUSTRY_ALL, {"PowerWheel": 20})
    for pop in (12, 16, 20, 24, 28):
        yield _economy_state(base, sci=0, pop=pop, free_beds=5)  # -> build_shrub

        c = _mix(base, {"Shrub": 1})
        # sci=5 (not 0): Campfire's own gate is sci0 like Shrub's, so nothing
        # about the LABEL needs this, but Shrub's state above is otherwise
        # feature-identical (decoration/amenity counts aren't tracked by
        # game_schema.feature_strings at all - see the Task 5a report) and
        # would collide with it under dataset.build()'s dedup without a
        # distinguishing science bucket (0="none" vs 5="low").
        yield _economy_state(c, sci=5, pop=pop, free_beds=5)  # -> build_campfire

        c = _mix(base, {"Shrub": 1, "Campfire": 1})
        # sci=100, not the minimum 50: still comfortably < Lido's 250 gate, but
        # 50-59 shares Shower's stock_sciencepoints bucket ("ok", [15,60)) with
        # _storage_states' pile-pressure states at sci=20 - 100 lands in "high"
        # instead, where cat_power (PowerWheel is built here, unlike the pile
        # states) already keeps it distinguishable from every other "high" user.
        yield _economy_state(c, sci=100, pop=pop, free_beds=5)  # -> build_shower

        c = _mix(base, {"Shrub": 1, "Campfire": 1, "Shower": 1})
        # logs=45 ("ok" bucket, >= Lido's own 40-log cost): sci alone can't
        # distinguish this from build_shower above (both pinned to the "high"
        # bucket by their >=100/>=250 gates with an unbounded top edge), and
        # the building profiles are otherwise identical (decoration counts
        # untracked - only the invisible presence/absence of the Shower
        # building differs between the two states).
        yield _economy_state(c, sci=250, pop=pop, free_beds=5, logs=45)  # -> build_lido

        c = _mix(base, _AMENITIES_ALL)
        yield _economy_state(c, sci=0, pop=pop, free_beds=0)  # -> build_lodge (free-bed)


def _power_states() -> Iterable[dict]:
    """economy.power_building_suggestion picks the highest-hp unlockable,
    land-placeable producer (WaterWheel/GeothermalEngine are map-gated and
    deliberately excluded - see the Task 5a report's blind-spot list). Industry
    +amenities pre-cleared (minus PowerWheel) so only power is under-supplied."""
    no_power = _mix(_BOOTSTRAP_DONE, _INDUSTRY_ALL, _AMENITIES_ALL)  # no PowerWheel: real deficit

    yield _economy_state(no_power, sci=0)      # -> build_power_wheel (only sci-0 producer)
    yield _economy_state(no_power, sci=150)    # -> build_wind_turbine (68hp > PowerWheel's 50)
    # logs=5 ("low" stock_log bucket): sci=1500 alone does not distinguish this
    # from build_wood_workshop's state above (both "high" science, "many"
    # industry, all makes_* flags identical) - LargeWindTurbine has no "log"
    # build-cost line at all (free regardless of logs), so this is a free lever.
    yield _economy_state(no_power, sci=1500, logs=5)   # -> build_large_wind_turbine (144hp, highest)


def _storage_states() -> Iterable[dict]:
    """economy.storage_pressure (ratio >= 0.85 of a resource's "capacity")
    picks the LARGEST unlockable storage tier for the good's kind - largest
    always wins ties at equal science, which is why Large Pile/Medium Warehouse
    dominate Small Pile/Small Warehouse here (see the Task 5a report)."""
    base = _mix(_BOOTSTRAP_DONE, _INDUSTRY_ALL, _AMENITIES_ALL, {"PowerWheel": 20})

    # sci=20 ("ok" bucket): the pile tests don't need >0 science (everything is
    # already pre-built), but at sci=0 ("none" bucket) these would be
    # feature-identical to _wellbeing_states' shrub/campfire states (same
    # industry-category bucket, decoration/amenity counts untracked) and lose
    # to them in dataset.build()'s dedup - see the Task 5a report.
    for good in ("Log", "Plank", "MetalBlock"):
        s = _economy_state(base, sci=20)
        _set_capacity_pressure(s, good, 0.95)
        yield s  # -> build_large_pile

    s = _economy_state(base, sci=0)
    _set_capacity_pressure(s, "Gear", 0.95)
    yield s  # -> build_medium_warehouse (MediumWarehouse is sci0, larger than Small;
    # the Gear stock itself - untouched by any other family's states - already
    # keeps this distinguishable at sci=0)

    s = _economy_state(base, sci=260)
    _set_capacity_pressure(s, "Gear", 0.95)
    yield s  # -> build_large_warehouse (sci>=250)

    # logs=10/25 ("low"/"ok" buckets): MediumTank/LargeTank have no "log" cost
    # line (free regardless of logs), and sci is pinned >=120/>=600 respectively
    # ("high" bucket either way) - same collision risk as the production/power
    # fixes above, this time against _wellbeing_states' build_lido state.
    s = _economy_state(base, sci=150, logs=10)
    _set_capacity_pressure(s, "Water", 0.95)
    yield s  # -> build_medium_tank (sci 120-599; the ONLY path to this label -
    # resource_manager.drought_prep never recommends MediumTank)

    # logs=0 ("none" bucket, LargeTank has no "log" build-cost line so this is
    # free): 25 ("ok") collided with build_lido's logs=45 (also "ok") - same
    # collision pattern as medium_tank above, against a different neighbor.
    c = _mix(base, {"SmallTank": 10})
    s = _economy_state(c, sci=650, logs=0)
    _set_capacity_pressure(s, "Water", 0.95, capacity=1000.0)
    yield s  # -> build_large_tank (sci>=600)

    # UndergroundPile (sci>=1000) is the TOP pile tier - it dominates Large
    # Pile once unlockable, the same way Large Pile dominates Small Pile.
    # Pressure PLANK, not Log: Plank also lists the Small/Large/Underground
    # Pile family in goods.json, and pressuring it leaves "Log" fully
    # controllable via the plain `logs=` kwarg - UndergroundPile costs 20 logs,
    # and _set_capacity_pressure's overwrite of whichever good it targets
    # doubles as that good's affordability check (see build_shower's and this
    # comment's history in the Task 5a report), so pressuring the SAME good
    # the build costs logs in creates exactly this kind of bind. Pressuring a
    # different good sidesteps it entirely, and "stock_plank" is untouched by
    # every other targeted state (always "none"), so this can't collide.
    s = _economy_state(base, sci=1000, logs=120)
    _set_capacity_pressure(s, "Plank", 0.95)
    yield s  # -> build_underground_pile


def _drought_states() -> Iterable[dict]:
    """resource_manager.drought_prep's buffer deficit (population x per-beaver
    use x (drought_days+2), vs tank capacity) - deliberately UNGATED by
    _survival_secure (surviving a drought IS survival) but industry/amenities/
    power still pre-cleared so THIS family's goal is the one that surfaces.
    Note: build_dam is unreachable here on purpose (see the Task 5a report) -
    a tank goal always co-occurs with deficit>0 and, since tank build costs
    have no "log" cost line for the relevant sizes, is always selected first."""
    base = _mix(_BOOTSTRAP_DONE, _INDUSTRY_ALL, _AMENITIES_ALL, {"PowerWheel": 20, "SmallTank": 0})
    for pop, dd in ((20, 6), (28, 10)):
        yield _economy_state(base, sci=0, pop=pop, drought_days=dd)    # -> build_small_tank
        yield _economy_state(base, sci=650, pop=pop, drought_days=dd)  # -> build_large_tank


def _economy_family_states() -> Iterable[dict]:
    yield from _production_chain_states()
    yield from _wellbeing_states()
    yield from _power_states()
    yield from _storage_states()
    yield from _drought_states()


def _deep_bootstrap_states() -> Iterable[dict]:
    """Two bootstrap goals that _bootstrap_grid_states's own cross product,
    empirically, never lets survive dataset.build()'s dedup:

    * build_efficient_farm_house - reachable (its condition is a pure
      food_days-vs-hazard-buffer check, independent of any dependency), but
      every grid combo where it wins turns out to share a feature vector with
      an EARLIER-generated, differently-labeled grid state (bucketed pop x
      water/food-days x drought leaves few distinct combinations, and _STAGES
      is the outer loop so earlier stages claim them first). Added explicitly
      here rather than debugged further into the grid, since the resulting
      state is trivial (GathererFlag built, WaterPump/tanks satisfied, food
      days below buffer).
    * build_forester - a genuine PLANNER QUIRK, not just a dedup collision:
      GOAL_DEPENDENCIES["build_forester"] = ("build_warehouse", "build_inventor")
      is checked by id STRING against build_safe_ready_frontier's sequential,
      order-dependent `selected` set. Once Inventor is actually built, the
      bootstrap "build_inventor" goal disappears - but
      _append_science_scaling_goals (Task 3b) can independently append A GOAL
      WITH THE SAME ID "build_inventor" (its "2nd Inventor" scaling signal)
      whenever any wanted producer is still science-locked. Since Forester is
      processed before that scaling goal in goals list order,
      _dependency_ready sees "build_inventor" present-but-not-yet-selected and
      blocks Forester - EVERY TIME, across the WHOLE bootstrap grid (which
      never finishes the industry tree, so science-scaling always has
      something suppressed). Forester only clears once SmallWarehouse AND
      Inventor are built AND nothing is left industry-wise for science-scaling
      to latch onto - i.e. the full economy is saturated. Not a Task 5a fix
      (out of scope - this is a live agent/planner.py bug); worked around here
      so the label is still learnable. See the Task 5a report."""
    c = {"LumberjackFlag": 1, "GathererFlag": 1, "WaterPump": 1, "SmallTank": 8}
    yield _economy_state(c, sci=0, logs=120, water_days=30, food_days=1.0, pop=10, homeless=0)
    # -> build_efficient_farm_house

    c = _mix({k: v for k, v in _BOOTSTRAP_DONE.items() if k != "Forester"}, _INDUSTRY_ALL)
    yield _economy_state(c, sci=0, pop=15)  # -> build_forester


def _iter_states() -> Iterable[dict]:
    yield from _bootstrap_grid_states()
    yield from _unreachable_states()
    yield from _economy_family_states()
    yield from _deep_bootstrap_states()


def _harvest_journal_states() -> List[dict]:
    """Pull any full state snapshots recorded in journal/*.jsonl (best-effort)."""
    out: List[dict] = []
    jdir = _ROOT / "journal"
    if not jdir.is_dir():
        return out
    for path in sorted(jdir.glob("*.jsonl")):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or '"resources"' not in line:
                    continue
                rec = json.loads(line)
                state = rec.get("state") or rec.get("after_state") or rec.get("before_state")
                if isinstance(state, dict) and state.get("resources"):
                    out.append(state)
        except Exception:
            continue
    return out


def build(
    balance_cap: int = 0, synthetic_states: Optional[Iterable[dict]] = None
) -> Tuple[List[dict], List[str], List[str]]:
    """Build (rows, vocab, labels).

    ``synthetic_states`` defaults to the full ``_iter_states()`` sweep (what
    ``python -m agent.nlp.dataset`` regenerates from); pass a smaller iterable
    to build a fast subset (e.g. the coverage test only needs
    ``_economy_family_states()`` + a couple of bootstrap examples, not the full
    ~13k-state grid).

    Label-failure handling is asymmetric on purpose: a SYNTHETIC state (one we
    constructed above) that Oracle.label can't translate is a real
    label-coverage bug - the planner emitted a goal id/spec this dataset
    builder doesn't know how to handle - so those failures are collected and
    raise a RuntimeError (loud, not swallowed) once every state has been
    tried. Journal-harvested states are external, possibly-stale snapshots
    recorded by real runs; those alone are still allowed to be skipped
    best-effort, since a malformed/outdated journal record is not a
    label-coverage signal about the CURRENT planner.
    """
    oracle = Oracle()
    seen: set = set()
    rows: List[dict] = []
    failures: List[str] = []

    states = list(synthetic_states) if synthetic_states is not None else list(_iter_states())
    for state in states:
        strings = feat.feature_strings(state)
        key = tuple(sorted(strings))
        if key in seen:
            continue
        seen.add(key)
        try:
            label = oracle.label(state)
        except Exception as exc:
            failures.append("counts=%r sci=%r: %s" % (
                ((state.get("buildings") or {}).get("counts")),
                next((r.get("stored") for r in state.get("resources", [])
                      if str(r.get("good", "")).lower() == "sciencepoints"), None),
                exc,
            ))
            continue
        rows.append({"features": strings, "label": label})

    if failures:
        print("Oracle.label failed on %d synthetic state(s):" % len(failures), file=sys.stderr)
        for line in failures[:50]:
            print("  " + line, file=sys.stderr)
        if len(failures) > 50:
            print("  ... and %d more" % (len(failures) - 50), file=sys.stderr)
        raise RuntimeError(
            "%d synthetic state(s) failed Oracle.label - this is a label-coverage "
            "bug (a planner goal id/spec this dataset builder cannot translate), "
            "not something to silently skip; see stderr for the failing states."
            % len(failures)
        )

    # journal-harvested states are external/possibly malformed - best-effort only,
    # and only pulled in for the default (full) build, never for an explicit
    # synthetic_states subset (e.g. the fast coverage-test path).
    if synthetic_states is None:
        for state in _harvest_journal_states():
            strings = feat.feature_strings(state)
            key = tuple(sorted(strings))
            if key in seen:
                continue
            seen.add(key)
            try:
                label = oracle.label(state)
            except Exception:
                continue
            rows.append({"features": strings, "label": label})

    # optional per-class cap so no single label dominates (keeps CART/MLP honest)
    if balance_cap:
        by_label: Dict[str, List[dict]] = {}
        for r in rows:
            by_label.setdefault(r["label"], []).append(r)
        capped: List[dict] = []
        for label, group in by_label.items():
            capped.extend(group[:balance_cap])
        rows = capped

    vocab = sorted({s for r in rows for s in r["features"]})
    labels = [a for a in feat.ACTIONS if any(r["label"] == a for r in rows)]
    return rows, vocab, labels


def main() -> None:
    _DATA.mkdir(parents=True, exist_ok=True)
    rows, vocab, labels = build()

    (_DATA / "decision_dataset.json").write_text(
        json.dumps(rows, indent=2), encoding="utf-8")
    (_DATA / "decision_vocab.json").write_text(
        json.dumps({"vocab": vocab}, indent=2), encoding="utf-8")
    (_DATA / "decision_labels.json").write_text(
        json.dumps(labels, indent=2), encoding="utf-8")

    dist: Dict[str, int] = {}
    for r in rows:
        dist[r["label"]] = dist.get(r["label"], 0) + 1
    print(f"rows={len(rows)}  vocab={len(vocab)}  labels={len(labels)}")
    for label in sorted(dist, key=lambda k: -dist[k]):
        print(f"  {label:24s} {dist[label]}")


if __name__ == "__main__":
    main()
