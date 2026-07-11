"""Deterministic MVP planner for Timberborn bootstrap decisions.

This module is intentionally standalone: stdlib only, no imports from play.py.
It turns bridge state/map facts into an ordered rule checklist plus reachable
placement candidates so the LLM chooses among valid options instead of guessing.
"""

from collections import deque
from itertools import combinations

# Spatial, utility-scored placement (influence/distance/zoning). Optional: the
# planner still works with the legacy nearest-flat-tile scan if it can't import.
try:
    import placement
except Exception:  # pragma: no cover - import path depends on invocation
    try:
        from agent import placement  # type: ignore
    except Exception:  # pragma: no cover
        placement = None

# Full-economy production-chain reasoning (Task 3a). Optional: the bootstrap
# planner still works without it; when present, analyze() appends producer goals
# for demanded-but-unproduced goods. game_schema maps a producer spec to its
# gameplay goal id (build_<snake>) and validates the action space.
try:
    import economy
except Exception:  # pragma: no cover - import path depends on invocation
    try:
        from agent import economy  # type: ignore
    except Exception:  # pragma: no cover
        economy = None

try:
    import game_schema
except Exception:  # pragma: no cover - import path depends on invocation
    try:
        from agent import game_schema  # type: ignore
    except Exception:  # pragma: no cover
        game_schema = None

# Drought-buffer sizing (Task 3e) reuses resource_manager.drought_prep verbatim.
# Optional, like the economy import: the planner degrades gracefully without it.
try:
    import resource_manager
except Exception:  # pragma: no cover - import path depends on invocation
    try:
        from agent import resource_manager  # type: ignore
    except Exception:  # pragma: no cover
        resource_manager = None


def _existing_buildings(state):
    """Placed buildings as [{spec,x,y,z}] for adjacency scoring (from buildings.list)."""
    out = []
    blist = (((state or {}).get("buildings") or {}).get("list")) if isinstance(state, dict) else None
    for b in blist or []:
        if isinstance(b, dict) and b.get("x") is not None and b.get("y") is not None:
            out.append({"spec": b.get("spec") or b.get("spec_id"),
                        "x": b.get("x"), "y": b.get("y"), "z": b.get("z")})
    return out


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
    "build_forester": "ForesterFlag",
    "build_path": "Path",
}


# Simple curriculum dependencies used by the controller. A dependency is satisfied
# when its goal is absent (the producer already exists) or was selected earlier in
# the same frontier.
GOAL_DEPENDENCIES = {
    "build_water_storage": ("build_water_pump",),
    "build_inventor": ("build_warehouse",),
    "build_forester": ("build_warehouse", "build_inventor"),
}


DIRECTIONS = ((0, -1, "North"), (1, 0, "East"), (0, 1, "South"), (-1, 0, "West"))

# Keep the tiles around the District Center (town hall) clear for the PATH network —
# no buildings may sit on the town-hall approaches. Exclude building candidates within
# this Chebyshev distance of the DC centre (covers the ~3x3 DC footprint + a 1-tile
# road ring); auto-connect paves paths through the reserved buffer. Paths themselves
# are exempt.
TOWNHALL_BUFFER = 2


def _blocks_townhall(tile, dc, buffer=TOWNHALL_BUFFER):
    """True if placing a BUILDING here would sit on/against the town-hall approaches."""
    if not isinstance(tile, dict) or not isinstance(dc, dict):
        return False
    try:
        return max(abs(int(tile["x"]) - int(dc["x"])), abs(int(tile["y"]) - int(dc["y"]))) <= buffer
    except (KeyError, TypeError, ValueError):
        return False


def analyze(state, map_data, buildings_detail=None):
    """Return an ordered deterministic goal checklist.

    Missing fields are treated as absent/zero so partial bridge snapshots still
    produce a useful checklist instead of raising.
    """
    state = state if isinstance(state, dict) else {}
    goals = []

    unreachable_index = 0
    for building in _building_details(state, buildings_detail):
        if isinstance(building, dict) and building.get("reachable") is False:
            unreachable_index += 1
            coords = _coords(building)
            goals.append(
                _goal(
                    "demolish_unreachable"
                    if unreachable_index == 1
                    else "demolish_unreachable_%s" % unreachable_index,
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

    water_tanks_have = _water_storage_units(state)
    water_tanks_target = _water_storage_target(state)
    if (
        _resource_days(state, "Water") < _hazard_buffer_days(state)
        and water_tanks_have < water_tanks_target
    ):
        goals.append(
            _goal(
                "build_water_storage",
                "water days remaining is below next hazard duration plus 2-day buffer",
                spec="SmallTank",
                logs_have=_logs_available(state),
                current_count=water_tanks_have,
                target_count=water_tanks_target,
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

    if _building_count(state, "ForesterFlag") <= 0:
        goals.append(
            _goal(
                "build_forester",
                "wood sustain comes after warehouse and science; Forester replants moist empty tiles",
                spec="ForesterFlag",
                logs_have=_logs_available(state),
            )
        )

    _append_production_chain_goals(goals, state)
    _append_wellbeing_goals(goals, state)
    _append_power_goals(goals, state)
    # Drought before storage: tank goals (and the bootstrap water-storage goal) land
    # in the covered set first, so a water storage-pressure signal can't double-emit a
    # tank the drought path already asked for.
    _append_drought_goals(goals, state)
    _append_storage_goals(goals, state)

    if _sites_under_construction(state) and _logs_available(state) > 0 and not _has_urgent_unblocked_goal(goals):
        goals.append(
            {
                "id": "advance_time",
                "why": "construction sites exist and logs are available; let haulers/builders work",
                "spec": "set_speed",
            }
        )

    return goals


def _append_production_chain_goals(goals, state):
    """Append Task-3a producer goals, GATED by 3b tech progression.

    3a is intentionally eager: economy.producer_plan wants the whole
    construction-material industry even when most of it is still science-locked.
    3b filters those goals through economy.unlockable_now, so a science-gated
    producer is emitted only once it is actually unlockable (enough stored science
    + prerequisites met). Non-science-locked producers (e.g. the start-tier Lumber
    Mill) pass straight through, preserving 3a behavior. The still-locked-but-wanted
    producers instead drive science (see _append_science_scaling_goals).

    economy.producer_plan still does the chain reasoning and raw->refined ordering;
    here we just turn each unlockable producer spec into a goal whose id is the real
    game_schema action (build_<snake>) with the log cost sourced from buildings.json.
    De-dups against goals already emitted, including bootstrap goals that target the
    same building under a different id (e.g. build_lumberjack -> LumberjackFlag).
    """
    if economy is None or game_schema is None:
        return

    covered = {goal.get("id") for goal in goals}
    for goal in goals:
        spec = goal.get("spec")
        if spec:
            action = game_schema.spec_to_action(spec)
            if action:
                covered.add(action)

    logs_have = _logs_available(state)
    unlockable = set(economy.unlockable_now(state))

    emittable = []      # (goal_id, item) for producers buildable NOW given tech
    suppressed = []     # items wanted but still science-locked
    for item in economy.producer_plan(state):
        goal_id = game_schema.spec_to_action(item["spec"])
        if not goal_id:
            continue  # not a gameplay action
        if item["spec"] in unlockable:
            emittable.append((goal_id, item))
        else:
            suppressed.append(item)

    # Raw->refined (chain depth) primary; recommended_order is the tiebreak among
    # available science-gated goals; spec breaks any remaining tie for determinism.
    emittable.sort(
        key=lambda gi: (
            gi[1]["depth"],
            economy.recommended_index(gi[1]["spec"]),
            gi[1]["spec"],
        )
    )
    for goal_id, item in emittable:
        if goal_id in covered:
            continue  # already covered by another goal
        covered.add(goal_id)
        goals.append(
            _goal(
                goal_id,
                item["why"],
                spec=item["spec"],
                logs_have=logs_have,
                cost_logs=item["cost_logs"],
            )
        )

    _append_science_scaling_goals(goals, state, covered, suppressed, logs_have)


def _append_science_scaling_goals(goals, state, covered, suppressed, logs_have):
    """Drive/scale science when it is the bottleneck for wanted producers.

    "Bottleneck" == there exist wanted-but-science-locked producers AND current
    stored science is below the cheapest one's unlock cost. The bootstrap already
    emits build_inventor when Inventor==0 (that IS the science driver), so we never
    duplicate it. Once an Inventor exists we SCALE science: build_observatory when
    it is itself unlockable (the high-throughput science building), else a 2nd
    Inventor. Additive: emits nothing when science is not the gate.
    """
    if economy is None or game_schema is None or not suppressed:
        return

    costs = [economy.science_cost(item["spec"]) for item in suppressed]
    costs = [c for c in costs if c > 0]
    if not costs:
        return  # suppressed for a non-science reason; more science won't help

    needed = min(costs)
    stored = economy.stored_science(state)
    if stored >= needed:
        return  # cheapest wanted unlock already affordable; science isn't the gate

    inventor_id = game_schema.spec_to_action("Inventor")
    if not inventor_id or inventor_id in covered:
        return  # bootstrap already emits build_inventor (Inventor==0); don't duplicate

    why = (
        "science gates %d wanted producer(s) (need >= %d SP, have %d); scale science"
        % (len(suppressed), int(needed), int(stored))
    )

    unlockable = set(economy.unlockable_now(state))
    observatory_id = game_schema.spec_to_action("Observatory")
    if (
        observatory_id
        and observatory_id not in covered
        and "Observatory" in unlockable
        and _building_count(state, "Observatory") <= 0
    ):
        covered.add(observatory_id)
        goals.append(
            _goal(
                observatory_id,
                why + " (Observatory: high-throughput science)",
                spec="Observatory",
                logs_have=logs_have,
                cost_logs=economy.log_cost("Observatory"),
            )
        )
        return

    covered.add(inventor_id)
    goals.append(
        _goal(
            inventor_id,
            why + " (2nd Inventor)",
            spec="Inventor",
            logs_have=logs_have,
            cost_logs=economy.log_cost("Inventor"),
        )
    )


def _survival_secure(state):
    """True when survival needs are met, so growth investments are appropriate.

    Requires a water pump AND a food source built, water AND food days-remaining at
    or above the hazard buffer (next weather duration + 2), and nobody homeless.
    This gates the Task-3c growth goals: well-being amenities and breeding headroom
    are never emitted while thirst, hunger or homelessness is still unresolved.
    Unknown/missing days read as 0, i.e. NOT secure (conservative).
    """
    if _building_count(state, "WaterPump") <= 0:
        return False
    if not _has_food_production(state):
        return False
    buffer = _hazard_buffer_days(state)
    if _resource_days(state, "Water") < buffer:
        return False
    if _resource_days(state, "Food", fallback_goods=("Berries", "Carrot", "GrilledPotato")) < buffer:
        return False
    if _as_int(((state.get("population") or {}).get("homeless")), 0) > 0:
        return False
    return True


def _append_wellbeing_goals(goals, state):
    """Append Task-3c housing-headroom + well-being amenity goals (GROW phase).

    Only fires when survival is SECURE (see _survival_secure): amenities and
    breeding headroom are growth investments, never emitted during a thirst/hunger/
    homeless crisis. Two additive emissions:

    * FREE-BED HEADROOM: Folktails breeding halts with no empty bed
      (needs.json.population_growth). The bootstrap only builds a Lodge when
      homeless>0; here, when free_beds<=0 even with nobody homeless, we emit a lodge
      tier so a new kit has ROOM to be born. Skipped when free_beds is unknown.
    * WELL-BEING AMENITIES: for each uncovered decoration-only well-being need
      (economy.uncovered_wellbeing_needs -> cheapest curated source), emit a
      build_<amenity> goal, GATED through economy.unlockable_now exactly like the
      3a/3b producers, so a science-locked source (Shower 50 SP, Mud Pit 1800 SP)
      is emitted only once it is actually unlockable. The 3b science driver scales
      science toward the still-locked ones.

    De-dups against goals already emitted (including the bootstrap Lodge).
    """
    if economy is None or game_schema is None:
        return
    if not _survival_secure(state):
        return

    covered = {goal.get("id") for goal in goals}
    logs_have = _logs_available(state)

    population = (state.get("population") or {}) if isinstance(state, dict) else {}
    free_beds = population.get("free_beds")
    if free_beds is not None and _as_int(free_beds, 0) <= 0:
        lodge_id = game_schema.spec_to_action("Lodge") or "build_lodge"
        if lodge_id not in covered:
            covered.add(lodge_id)
            goals.append(
                _goal(
                    lodge_id,
                    "no free beds: Folktails breeding needs an empty bed for a kit; add housing",
                    spec="Lodge",
                    logs_have=logs_have,
                    cost_logs=economy.log_cost("Lodge"),
                )
            )

    unlockable = set(economy.unlockable_now(state))
    for row in economy.uncovered_wellbeing_needs(state):
        spec = row["spec"]
        goal_id = game_schema.spec_to_action(spec)
        if not goal_id or goal_id in covered:
            continue
        if spec not in unlockable:
            continue  # science-locked; the 3b science driver scales toward it
        covered.add(goal_id)
        goals.append(
            _goal(
                goal_id,
                "well-being: %s has no source; %s raises well-being to speed breeding"
                % (row["need"], spec),
                spec=spec,
                logs_have=logs_have,
                cost_logs=economy.log_cost(spec),
            )
        )


def _append_power_goals(goals, state):
    """Append a Task-3d power-building goal when the colony is (or is about to be)
    under-powered.

    Powered production buildings (LumberMill -50hp, GearWorkshop -120hp, Smelter
    -200hp, ...) sit IDLE until a power source feeds them, so power must land WITH --
    not a cycle behind -- production. Two additive triggers, both keyed on the signed
    economy.power_deficit (consumed - produced over BUILT buildings):

    * REACTIVE: power_deficit > 0 -> a built powered building already outruns built
      production; add power to actually run it.
    * ANTICIPATORY: a powered production building is being PLANNED this cycle (an
      already-emitted goal whose spec is a power consumer) AND there is no existing
      surplus (power_deficit >= 0) -> emit power alongside the production so the new
      consumer is not stranded idle for a cycle. An existing surplus (deficit < 0)
      is left to absorb the new load, so power is not over-built.

    The building is economy.power_building_suggestion (map-blind: PowerWheel by
    default, upgrading to wind producers as science allows; WaterWheel/Geothermal are
    left to the map-aware placement layer). It is routed through the SAME
    unlockable_now science gate as 3a/3b/3c, with cost sourced from buildings.json via
    economy.log_cost. De-dups against goals already emitted. Emits at most ONE power
    goal per cycle; multi-building scaling happens across cycles as built counts (and
    thus the deficit) change.
    """
    if economy is None or game_schema is None:
        return

    deficit = economy.power_deficit(state)
    planning_powered = any(
        goal.get("spec") and economy.is_power_consumer(goal["spec"]) for goal in goals
    )
    if not (deficit > 0 or (planning_powered and deficit >= 0)):
        return

    spec = economy.power_building_suggestion(state)
    if not spec:
        return
    goal_id = game_schema.spec_to_action(spec)
    if not goal_id:
        return
    if spec not in set(economy.unlockable_now(state)):
        return  # science-locked; the suggestion already guarantees this, but be safe
    covered = {goal.get("id") for goal in goals}
    if goal_id in covered:
        return

    if deficit > 0:
        why = (
            "built powered buildings draw %d hp beyond production; add %s to run them"
            % (int(deficit), spec)
        )
    else:
        why = (
            "a powered production building is being built with no spare power; "
            "add %s so it runs immediately" % spec
        )

    goals.append(
        _goal(
            goal_id,
            why,
            spec=spec,
            logs_have=_logs_available(state),
            cost_logs=economy.log_cost(spec),
        )
    )


def _covered_ids(goals):
    """Goal ids already present, PLUS the build action of each goal's spec.

    Lets a later emitter dedup both against explicit ids and against a bootstrap goal
    that targets the same building under a different id (e.g. build_water_storage ->
    SmallTank -> build_small_tank). Mirrors the covered-set the 3a/3b/3c/3d emitters
    build inline.
    """
    covered = {goal.get("id") for goal in goals}
    if game_schema is not None:
        for goal in goals:
            spec = goal.get("spec")
            if spec:
                action = game_schema.spec_to_action(spec)
                if action:
                    covered.add(action)
    return covered


def _emit_spec_goal(goals, covered, spec, why, logs_have):
    """Append one build_<spec> goal (log cost from buildings.json) unless covered.

    Returns True when a goal was appended. Mutates ``covered`` with the new id so
    repeated calls in one cycle self-dedup. The caller is responsible for the science
    gate (only unlockable specs should be passed); this is the shared emit tail for
    the 3e storage/drought goals, matching how the 3a-3d emitters build a goal.
    """
    if game_schema is None or economy is None:
        return False
    goal_id = game_schema.spec_to_action(spec)
    if not goal_id or goal_id in covered:
        return False
    covered.add(goal_id)
    goals.append(
        _goal(goal_id, why, spec=spec, logs_have=logs_have, cost_logs=economy.log_cost(spec))
    )
    return True


def _append_drought_goals(goals, state):
    """Append Task-3e drought goals: the measurable tank buffer + a bounded reservoir.

    TANKS (survival-adjacent -> emitted UNGATED by _survival_secure, since surviving a
    drought IS survival): resource_manager.drought_prep sizes the water buffer for the
    next drought and recommends SmallTank/LargeTank counts; while its ``deficit`` > 0 we
    emit the matching tank goal. This is the MEASURABLE drought buffer -- each tank built
    raises current_buffer and shrinks the deficit, so it terminates at deficit 0.
      * LargeTank (600 SP) is routed through the unlockable_now science gate.
      * If a LargeTank is recommended but not yet unlockable we FALL BACK to the always-
        available SmallTank, so a big deficit on a low-science colony still gets buffer
        instead of emitting nothing.
      * De-dups against the bootstrap build_water_storage goal (also a SmallTank) via
        the covered set, so the two never double-emit.

    RESERVOIR ENGINEERING (Levee/Dam/Floodgate -- BOUNDED, at most one): drought_prep's
    deficit models ONLY tanks; a Dam/Levee/Floodgate does NOT reduce it. So reservoir
    emission is deliberately NOT keyed on the deficit magnitude (looping levee/floodgate
    on a deficit they can't shrink would never terminate). Instead we emit a SINGLE
    reservoir goal (economy.reservoir_suggestion -> Dam by default) only while the
    drought is long (>= economy.DROUGHT_LONG_DAYS), a deficit still exists, AND no
    reservoir-engineering building exists yet. That "none built yet" guard is the hard
    bound: once one Dam/Levee/Floodgate exists the suggestion is never repeated. Water/
    river placement is a Task 8 refinement; here the goal just needs a valid spec (the
    bridge validates placement and falls to the next candidate if unreachable).
    """
    if economy is None or game_schema is None or resource_manager is None:
        return

    prep = resource_manager.drought_prep(state)
    deficit = _as_float(prep.get("deficit"), 0.0)
    if deficit <= 0:
        return

    covered = _covered_ids(goals)
    logs_have = _logs_available(state)
    unlockable = set(economy.unlockable_now(state))
    build = prep.get("build") or {}

    want_small = _as_int(build.get("SmallTank"), 0) > 0
    want_large = _as_int(build.get("LargeTank"), 0) > 0
    large_ok = want_large and "LargeTank" in unlockable
    # LargeTank recommended but still science-locked -> keep buffering with SmallTank.
    small_needed = want_small or (want_large and not large_ok)

    if large_ok:
        _emit_spec_goal(
            goals, covered, "LargeTank",
            "drought buffer short by %d water; add a Large Tank" % int(deficit),
            logs_have,
        )
    if small_needed:
        _emit_spec_goal(
            goals, covered, "SmallTank",
            "drought buffer short by %d water; add a Small Tank" % int(deficit),
            logs_have,
        )

    # Bounded reservoir engineering: long drought, still in deficit, none built yet.
    drought_days = _as_float(prep.get("drought_days"), 0.0)
    reservoir_built = any(_building_count(state, spec) > 0 for spec in economy.RESERVOIR_SPECS)
    if drought_days >= economy.DROUGHT_LONG_DAYS and not reservoir_built:
        spec = economy.reservoir_suggestion(state)
        if spec:
            _emit_spec_goal(
                goals, covered, spec,
                "long %d-day droughts with a water deficit; %s starts reservoir "
                "engineering (build one, then expand manually)" % (int(drought_days), spec),
                logs_have,
            )


def _append_storage_goals(goals, state):
    """Append Task-3e pile/warehouse goals for goods near their storage capacity.

    Storage-pressure is an economy/GROWTH concern -- a full pile stalls production but
    is not itself a survival crisis -- so, like the 3c amenities, it is GATED behind
    _survival_secure: no logs are diverted to warehouses while thirst, hunger or
    homelessness is unresolved. For each pressured good (economy.storage_pressure) we
    pick the largest storage building of the good's OWN kind that is unlockable_now
    (economy.storage_specs_for is largest-first; the science gate down-selects Large
    Warehouse / Underground Pile to when they are affordable), and emit one build goal
    per distinct storage building. Goods with no storage building (SciencePoints) are
    skipped. De-dups against goals already emitted (incl. the bootstrap warehouse).
    """
    if economy is None or game_schema is None:
        return
    if not _survival_secure(state):
        return

    covered = _covered_ids(goals)
    logs_have = _logs_available(state)
    unlockable = set(economy.unlockable_now(state))

    for good in economy.storage_pressure(state):
        spec = next((s for s in economy.storage_specs_for(good) if s in unlockable), None)
        if not spec:
            continue  # good has no storage building, or none of its tiers is unlockable
        _emit_spec_goal(
            goals, covered, spec,
            "storage: %s is near capacity; add a %s" % (good, spec),
            logs_have,
        )


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


def candidates_for(goal, state, map_data, k=6, resources=None):
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
    if str(goal_id).startswith("demolish_unreachable"):
        return []

    # SPATIAL PLACEMENT: for building specs the placement layer supports, use
    # utility-scored candidates (influence maps + distance fields + the researched
    # Timberborn weight profiles) instead of "nearest reachable flat tile". Falls
    # through to the legacy scan for Path / unsupported specs or on any error.
    if placement is not None and spec in getattr(placement, "PROFILES", {}) and spec != "Path":
        try:
            dc_xy = (dc["x"], dc["y"])
            occupied_extra = _existing_buildings(state)
            ranked = placement.ranked_candidates(
                spec, map_data, resources or {}, dc_xy, k=max(_as_int(k, 6), 0),
                occupied_extra=occupied_extra,
            )
            if ranked:
                return ranked
        except Exception:
            pass  # fall through to the legacy candidate scan

    # Buildings (everything except a Path) must stay off the town-hall approaches.
    keep_townhall_clear = spec != "Path"

    resource_candidates = _resource_aware_candidates(
        goal_id, spec, state, map_data, arrays, dc, reachable, resources, k
    )
    if resource_candidates and keep_townhall_clear:
        resource_candidates = [c for c in resource_candidates if not _blocks_townhall(c, dc)]
    # Only short-circuit on resource-aware candidates if any SURVIVED the town-hall
    # filter; otherwise fall through to the generic scan (which also honors the buffer
    # but searches the whole map, so it finds valid tiles just outside it).
    if resource_candidates:
        return resource_candidates

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
            if keep_townhall_clear and _blocks_townhall(tile, dc):
                continue  # reserve the town-hall approaches for paths

            candidate = None
            if spec == "WaterPump":
                candidate = _water_pump_candidate(arrays, tile)
            elif spec == "EfficientFarmhouse":
                if tile["moist"] == 1:
                    candidate = _candidate(tile, "moist=1")
            elif spec == "ForesterFlag":
                if tile["moist"] == 1:
                    candidate = _candidate(tile, "moist replanting tile")
                    candidate["planting_tiles"] = _nearby_moist_tiles(arrays, tile, set(), limit=12)
            elif spec == "Path":
                if _distance(tile, dc) <= 6:
                    candidate = _candidate(tile, "near district center")
            elif spec in ("SmallTank", "Lodge", "SmallWarehouse", "Inventor", "LumberjackFlag", "GathererFlag") or _is_buildable_spec(spec):
                # Whitelisted bootstrap specs plus any real buildable gameplay spec
                # (e.g. the Task-3a industry producers) get generic flat-dry-land
                # candidates. Verb "specs" like set_speed resolve to no action and
                # are skipped so they never receive build tiles.
                same_height = _same_height_dry_neighbors(arrays, tile)
                if same_height >= 2:
                    candidate = _candidate(tile, "flat dry land; %s same-height dry neighbors" % same_height)
            if candidate is not None:
                tiles.append(candidate)

    tiles.sort(key=lambda item: (_distance(item, dc), item["y"], item["x"]))
    return tiles[: max(_as_int(k, 6), 0)]


def plan_report(state, map_data, buildings_detail=None, resources=None):
    """Return planner data plus a compact prompt block for the LLM."""
    goals = analyze(state, map_data, buildings_detail=buildings_detail)
    candidates_by_goal = {}
    followups = {}
    alerts_local = []
    advance_time = False

    for goal in goals:
        goal_id = goal.get("id")
        if goal_id:
            candidates_by_goal[goal_id] = candidates_for(goal, state, map_data, k=6, resources=resources)
            followup = _followups_for_goal(goal_id, candidates_by_goal[goal_id], resources)
            if followup:
                followups[goal_id] = followup
        if goal.get("blocked_by"):
            alerts_local.append({"id": "blocked_by_resources", "goal": goal_id, "message": goal["blocked_by"]})
            if "advance_time" in goal["blocked_by"]:
                advance_time = True
        if str(goal_id).startswith("demolish_unreachable"):
            alerts_local.append({"id": "building_unreachable", "message": goal.get("why", "")})
        if goal_id == "advance_time":
            advance_time = True

    if _sites_under_construction(state):
        alerts_local.append({"id": "sites_under_construction", "message": "construction sites exist"})
        advance_time = True

    decision_fork = _decision_fork(goals, candidates_by_goal, state)
    text = _report_text(goals, candidates_by_goal, advance_time, followups)
    return {
        "goals": goals,
        "candidates_by_goal": candidates_by_goal,
        "followups": followups,
        "alerts_local": alerts_local,
        "advance_time_recommended": advance_time,
        "decision_fork": decision_fork,
        "text": text,
    }


def _goal(
    goal_id,
    why,
    spec=None,
    logs_have=None,
    coords=None,
    current_count=None,
    target_count=None,
    cost_logs=None,
):
    item = {"id": goal_id, "why": why, "satisfied": False}
    if spec:
        item["spec"] = spec
        # COST_LOGS is the bootstrap table; it omits/misnames the economy
        # buildings, so callers with a real cost (sourced from buildings.json via
        # economy.log_cost) pass it explicitly and override the lookup.
        item["cost_logs"] = COST_LOGS.get(spec, 0) if cost_logs is None else cost_logs
        item["free"] = item["cost_logs"] == 0
        item["affordable"] = item["free"] or (
            logs_have is not None and item["cost_logs"] <= logs_have
        )
        if logs_have is not None and item["cost_logs"] > logs_have:
            item["blocked_by"] = "need %s logs, have %s -> advance_time" % (item["cost_logs"], logs_have)
    if current_count is not None and target_count is not None:
        item["current_count"] = current_count
        item["target_count"] = target_count
        item["satisfied"] = current_count >= target_count
    if coords:
        item["coords"] = coords
    return item


def _decision_fork(goals, candidates_by_goal, state):
    """Describe Log contention among individually affordable, ready goals."""
    available = _logs_available(state)
    contenders = []
    for goal in goals:
        goal_id = goal.get("id")
        cost = _as_int(goal.get("cost_logs"), 0)
        if (
            goal_id
            and cost > 0
            and goal.get("affordable") is True
            and goal.get("satisfied") is not True
            and candidates_by_goal.get(goal_id)
        ):
            contenders.append(goal)
    required = sum(_as_int(goal.get("cost_logs"), 0) for goal in contenders)
    if len(contenders) < 2 or required <= available:
        return None

    goal_ids_present = {goal.get("id") for goal in goals}
    always_selected = {
        goal.get("id")
        for goal in goals
        if goal.get("free") is True and candidates_by_goal.get(goal.get("id"))
    }
    feasible = []
    for size in range(1, len(contenders) + 1):
        for indexes in combinations(range(len(contenders)), size):
            cost = sum(_as_int(contenders[index].get("cost_logs"), 0) for index in indexes)
            option_goal_ids = {contenders[index]["id"] for index in indexes}
            chosen_ids = always_selected.union(option_goal_ids)
            dependencies_ready = all(
                dependency not in goal_ids_present or dependency in chosen_ids
                for goal_id in option_goal_ids
                for dependency in GOAL_DEPENDENCIES.get(goal_id, ())
            )
            if cost <= available and dependencies_ready:
                feasible.append((indexes, cost))

    maximal = []
    for indexes, cost in feasible:
        chosen = set(indexes)
        if any(chosen < set(other) for other, _other_cost in feasible):
            continue
        maximal.append((indexes, cost))
    maximal.sort(key=lambda item: (item[0], item[1]))

    options = []
    for number, (indexes, cost) in enumerate(maximal[:16], start=1):
        options.append(
            {
                "id": "logs-%s" % number,
                "goal_ids": [contenders[index]["id"] for index in indexes],
                "cost_logs": cost,
            }
        )
    return {
        "type": "resource_contention",
        "resource": "Log",
        "available": available,
        "required_total": required,
        "goal_ids": [goal["id"] for goal in contenders],
        "options": options,
        "options_truncated": len(maximal) > len(options),
    }


def _report_text(goals, candidates_by_goal, advance_time, followups=None):
    followups = followups or {}
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
        goal_followups = followups.get(goal.get("id")) or []
        if goal_followups:
            bits.append("followup=%s" % ", ".join(item.get("action", "?") for item in goal_followups))
        lines.append(" | ".join(bits))
    if advance_time:
        lines.append("ADVANCE TIME (set_speed 3, then re-check)")
    return "\n".join(lines[:25])


def _format_candidates(candidates):
    parts = []
    for item in candidates[:6]:
        coord = "(%s,%s,%s%s)" % (
            item["x"],
            item["y"],
            item["z"],
            ("," + item["orientation"]) if item.get("orientation") else "",
        )
        why = item.get("why")
        parts.append("%s %s" % (coord, why) if why else coord)
    return "; ".join(parts)


def _format_coords(coords):
    if isinstance(coords, dict):
        return "(%s,%s,%s)" % (coords.get("x", "?"), coords.get("y", coords.get("z", "?")), coords.get("z", "?"))
    return str(coords)


def _is_buildable_spec(spec):
    """True if ``spec`` is a real buildable gameplay spec (has a build_<x> action).

    Lets candidates_for hand generic land tiles to economy producer specs without
    hard-coding them, while excluding verb pseudo-specs like set_speed.
    """
    if not spec or game_schema is None:
        return False
    try:
        return game_schema.spec_to_action(spec) is not None
    except Exception:  # pragma: no cover - defensive
        return False


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
        if (
            goal.get("id") in urgent
            or str(goal.get("id", "")).startswith("demolish_unreachable")
        ) and not goal.get("blocked_by"):
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
    # Building count keys are faction-suffixed ("LumberjackFlag.Folktails"); match
    # the bare prefix so "LumberjackFlag" finds it. Counts include construction
    # sites, so a just-placed (still building) flag correctly reads as present and
    # the planner stops demanding another.
    counts = (((state or {}).get("buildings") or {}).get("counts") or {})
    lowered = str(spec).lower()
    total = 0
    for key, value in counts.items():
        name = str(key).lower()
        if name == lowered or name.startswith(lowered + "."):
            total += _as_int(value, 0)
    return total


def _logs_available(state):
    resource = _resource(state, "Log")
    if not resource:
        return 0
    if resource.get("stored") is not None:
        return _as_int(resource.get("stored"), 0)
    return _as_int(resource.get("all_stock"), 0)


def _resource_days(state, good, fallback_goods=()):
    goods = (good,) + tuple(fallback_goods)
    for name in goods:
        resource = _resource(state, name)
        if resource:
            return _as_float(resource.get("days_remaining"), 0.0)
    return 0.0


def _water_storage_target(state):
    population = max(
        _as_int(((state.get("population") or {}).get("total")), 0)
        if isinstance(state, dict)
        else 0,
        1,
    )
    required_water = _hazard_buffer_days(state) * 2.13 * population
    return max(int((required_water + 29.999) // 30), 1)


def _water_storage_units(state):
    return (
        _building_count(state, "SmallTank")
        + _building_count(state, "MediumTank") * 10
        + _building_count(state, "LargeWaterTank") * 10
    )


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
        "reachable": map_data.get("reachable") or [],
        "on_road": map_data.get("on_road") or [],
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


def _resource_aware_candidates(goal_id, spec, state, map_data, arrays, dc, fallback_reachable, resources, k):
    if not isinstance(resources, dict) or resources.get("ok") is False:
        return []
    limit = max(_as_int(k, 6), 0)
    if limit <= 0:
        return []

    blocked = _resource_coords(resources)
    road_reachable = _road_reachable_tiles(map_data, arrays, fallback_reachable)
    if not road_reachable:
        return []

    if goal_id == "build_lumberjack" or spec == "LumberjackFlag":
        return _lumberjack_resource_candidates(arrays, dc, resources, road_reachable, blocked, limit)
    if goal_id == "build_gatherer" or spec == "GathererFlag":
        return _gatherer_resource_candidates(arrays, dc, resources, road_reachable, blocked, limit)
    if goal_id == "build_water_pump" or spec == "WaterPump":
        return _water_resource_candidates(arrays, dc, road_reachable, blocked, limit)
    if goal_id == "build_farm" or spec == "EfficientFarmhouse":
        return _moist_cluster_candidates(arrays, dc, road_reachable, blocked, limit, "moist farm cluster")
    if goal_id == "build_forester" or spec == "ForesterFlag":
        return _moist_cluster_candidates(arrays, dc, road_reachable, blocked, limit, "moist replanting cluster")
    return []


def _lumberjack_resource_candidates(arrays, dc, resources, road_reachable, blocked, limit):
    # Cutting is GLOBAL: a staffed Lumberjack fells any REACHABLE designated tree,
    # so the flag does NOT need to be near the forest. Placing it in the forest is
    # actually harmful — surrounded by tree-occupied tiles it is hard to connect and
    # often reads unreachable. So we place it on clear reachable land NEAREST the
    # district center (trivial to connect + staff); the designate_cutting followup
    # marks the trees and the beaver walks out to cut them.
    mature = [
        item for item in resources.get("trees", []) or []
        if isinstance(item, dict) and item.get("mature") is True
    ]
    if not mature:
        return []  # nothing to cut; fall back to generic candidates
    species, total = _dominant_species(mature, "Tree")
    # Cutting is GLOBAL and the flag has a large harvest radius, so the flag does NOT need to
    # sit by the forest - the worker walking out to cut is expected and fine. Keep it on
    # clear reachable land nearest the DC (trivial to connect + staff).
    candidates = []
    for tile in _candidate_land_tiles(arrays, road_reachable, blocked):
        candidate = _candidate(
            tile,
            "clear near-DC land; cuts %d designated %s globally"
            % (total, _plural_species(species, total)),
        )
        candidates.append((_distance(tile, dc), tile["y"], tile["x"], candidate))
    candidates.sort()
    return [item[-1] for item in candidates[:limit]]


def _gatherer_resource_candidates(arrays, dc, resources, road_reachable, blocked, limit):
    ready = [
        item for item in resources.get("gatherables", []) or []
        if isinstance(item, dict) and item.get("ready") is True
    ]
    if not ready:
        return []
    candidates = []
    for tile in _candidate_land_tiles(arrays, road_reachable, blocked):
        nearby = [item for item in ready if _manhattan_xy(tile, item) <= 20]
        if not nearby:
            continue
        good, count = _dominant_good(nearby, "gatherables")
        total_distance = sum(_manhattan_xy(tile, item) for item in nearby)
        candidate = _candidate(tile, "central to %s ready %s within 20 tiles" % (len(nearby), good))
        candidates.append((-len(nearby), total_distance, _distance(tile, dc), tile["y"], tile["x"], candidate))
    candidates.sort()
    return [item[-1] for item in candidates[:limit]]


def _water_resource_candidates(arrays, dc, road_reachable, blocked, limit):
    candidates = []
    for tile in _candidate_land_tiles(arrays, road_reachable, blocked):
        candidate = _water_pump_candidate(arrays, tile)
        if candidate is not None:
            candidates.append((_distance(tile, dc), tile["y"], tile["x"], candidate))
    candidates.sort()
    return [item[-1] for item in candidates[:limit]]


def _moist_cluster_candidates(arrays, dc, road_reachable, blocked, limit, label):
    candidates = []
    for tile in _candidate_land_tiles(arrays, road_reachable, blocked):
        if tile["moist"] != 1:
            continue
        moist_nearby = 0
        for row in range(max(0, tile["row"] - 4), min(arrays["height"], tile["row"] + 5)):
            for col in range(max(0, tile["col"] - 4), min(arrays["width"], tile["col"] + 5)):
                other = _tile(arrays, col, row)
                if other is not None and other["moist"] == 1 and _is_land(other):
                    moist_nearby += 1
        candidate = _candidate(tile, "%s; %s moist tiles nearby" % (label, moist_nearby))
        candidate["planting_tiles"] = _nearby_moist_tiles(arrays, tile, blocked, limit=12)
        candidates.append((-moist_nearby, _distance(tile, dc), tile["y"], tile["x"], candidate))
    candidates.sort()
    return [item[-1] for item in candidates[:limit]]


def _candidate_land_tiles(arrays, road_reachable, blocked):
    for row in range(arrays["height"]):
        for col in range(arrays["width"]):
            tile = _tile(arrays, col, row)
            if tile is None:
                continue
            key = (tile["x"], tile["y"])
            if key not in road_reachable or key in blocked:
                continue
            if _array_value(arrays["on_road"], row * arrays["width"] + col, 0):
                continue
            if tile["occupied"] or tile["contamination"] > 0 or not _is_land(tile):
                continue
            yield tile


def _clear_land_tiles(arrays, blocked):
    """Clear, buildable land tiles WITHOUT the road-reachable restriction - used when the
    agent trunk will connect a far placement (e.g. a lumberjack at the forest edge). Same
    clear-land test as _candidate_land_tiles minus the road_reachable membership."""
    for row in range(arrays["height"]):
        for col in range(arrays["width"]):
            tile = _tile(arrays, col, row)
            if tile is None:
                continue
            key = (tile["x"], tile["y"])
            if key in blocked:
                continue
            if _array_value(arrays["on_road"], row * arrays["width"] + col, 0):
                continue
            if tile["occupied"] or tile["contamination"] > 0 or not _is_land(tile):
                continue
            yield tile


def _road_reachable_tiles(map_data, arrays, fallback_reachable):
    total = arrays["width"] * arrays["height"]
    on_road = arrays.get("on_road") or []
    bridge_reachable = arrays.get("reachable") or []
    if len(on_road) < total:
        return set(fallback_reachable)

    starts = []
    for row in range(arrays["height"]):
        for col in range(arrays["width"]):
            index = row * arrays["width"] + col
            if not _array_value(on_road, index, 0):
                continue
            tile = _tile(arrays, col, row)
            if tile is not None and _is_land(tile):
                starts.append(tile)
    if not starts:
        return set(fallback_reachable)

    seen = {(tile["x"], tile["y"]) for tile in starts}
    queue = deque(starts)
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

    if len(bridge_reachable) >= total:
        allowed = set()
        for row in range(arrays["height"]):
            for col in range(arrays["width"]):
                index = row * arrays["width"] + col
                if _array_value(bridge_reachable, index, 0):
                    tile = _tile(arrays, col, row)
                    if tile is not None:
                        allowed.add((tile["x"], tile["y"]))
        if allowed:
            seen = seen.intersection(allowed)
    return seen


def _resource_coords(resources):
    coords = set()
    for key in ("trees", "gatherables"):
        for item in resources.get(key, []) or []:
            if isinstance(item, dict) and "x" in item:
                coords.add((_as_int(item.get("x")), _as_int(item.get("y", item.get("z")))))
    return coords


def _dominant_species(items, fallback):
    counts = {}
    for item in items:
        species = str(item.get("species") or fallback)
        counts[species] = counts.get(species, 0) + 1
    if not counts:
        return fallback, 0
    species, count = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[0]
    return species, count


def _dominant_good(items, fallback):
    counts = {}
    for item in items:
        good = str(item.get("good") or item.get("species") or fallback)
        counts[good] = counts.get(good, 0) + 1
    if not counts:
        return fallback, 0
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[0]


def _plural_species(species, count):
    species = str(species or "trees")
    if count == 1 or species.endswith("s"):
        return species
    return species + "s"


def _manhattan_xy(a, b):
    return abs(_as_int(a.get("x")) - _as_int(b.get("x"))) + abs(
        _as_int(a.get("y", a.get("z"))) - _as_int(b.get("y", b.get("z")))
    )


def _followups_for_goal(goal_id, candidates, resources):
    if goal_id == "build_lumberjack":
        return [{"action": "designate_cutting", "args": {"all": True}}]
    if goal_id == "build_forester":
        tiles = _planting_tiles_near(candidates[0], resources) if candidates else []
        if tiles:
            return [{"action": "designate_planting", "args": {"tiles": tiles, "species": "Pine"}}]
    return []


def _planting_tiles_near(candidate, resources):
    if not isinstance(candidate, dict):
        return []
    # /resources does not list empty moist tiles, so use the planner candidates'
    # neighborhood from /map-derived followup context when available.
    tiles = candidate.get("planting_tiles")
    if isinstance(tiles, list) and tiles:
        return tiles[:12]
    return [{"x": candidate["x"], "y": candidate["y"], "z": candidate["z"]}]


def _nearby_moist_tiles(arrays, tile, blocked, limit=12):
    tiles = []
    for row in range(max(0, tile["row"] - 5), min(arrays["height"], tile["row"] + 6)):
        for col in range(max(0, tile["col"] - 5), min(arrays["width"], tile["col"] + 6)):
            other = _tile(arrays, col, row)
            if other is None:
                continue
            key = (other["x"], other["y"])
            if key in blocked:
                continue
            if other["moist"] != 1 or other["occupied"] or other["contamination"] > 0 or not _is_land(other):
                continue
            tiles.append((_manhattan_xy(tile, other), other["y"], other["x"], {"x": other["x"], "y": other["y"], "z": other["z"]}))
    tiles.sort()
    return [item[-1] for item in tiles[: max(_as_int(limit, 12), 0)]]


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
