# Coordinated spatial layout planner — design

> 2026-07-12. Answers two user asks: (1) VERTICALITY — build up with platforms/stacking when
> flat ground is scarce; (3) MULTI-STEP SPATIAL AWARENESS — "do placements for multiple things
> at once, aware of where it'll be placed, blockers, other buildings — a memory of where you
> want what." These are one thing: replace greedy, one-tile, flat-ground placement with a
> **persistent, coordinated colony blueprint**. Grounded in `docs/kb/placement-verticality-gaps.md`.

## Problem with today's placement
`planner.candidates_for(goal, ...)` is called per-goal, greedily, every cycle: it scores flat-dry
ground tiles (z = terrain height) with no memory, no footprint, no reservation of future slots,
and no check that a placement boxes in a neighbor. Result: the colony sprawls, walls itself off,
and buildings get placed → become unreachable → demolished (observed live). There is no plan.

## The design: a persistent `LayoutPlanner`

A single object, held by the play loop and persisted across cycles (not re-derived greedily).

### Data structures (`agent/layout.py`)
- **`Reservation`** — a sparse 3-D occupancy map `{(x,y,z): owner}` where owner ∈ {built, reserved(spec), path, platform}. Seeded each cycle from `/map` (`terrain_height`+`occupied`) and `/state buildings.list` (built footprints), plus the plan's own not-yet-built reservations. This is the "aware of where it will be placed / blockers" substrate — footprint-aware, multi-Z.
- **`Zones`** — `category -> Region` (a set of tiles or a bounding box) reserved per building category: `water` (river frontage), `farm/forestry` (moist soil), `housing` (flat, near DC + amenities), `industry` (flat, near storage), `storage` (between producers), `power` (flowing water / height), `science`, `wellbeing` (inside housing reach). Assigned ONCE at init by scoring the reachable area around the DC for each category's tile needs (reuse `spatial.distance_field` / `label_regions`), then persisted. This is the "memory of where you want what."
- **`Slots`** — `spec -> reserved footprint (x,y,z,orientation)`: intended future placements, filled by the batch planner, cleared as buildings get built. Lets the agent reserve room for the whole plan, not just the next building.
- **`placed`** — reconciled from `/state` each cycle (coords + footprint of what's actually built), so the plan tracks reality.

### Footprint + stacking data
From `agent/data/building_stacking.json` (already captured): per spec `size{x,y,z}`, `stackable`
(has a buildable roof), `can_stack_on` (base allows a stackable surface). A placement reserves the
FULL rotated footprint; a stacked placement requires the support below to be `stackable` and a
walkable access tile at that Z (platform top / stairs), per the decompiled `MatterBelow` rules
(`docs/kb/placement-verticality-gaps.md`).

## Algorithm (per cycle)
1. **Reconcile** — refresh `Reservation` from `/map`+`/state`; mark built footprints; drop
   satisfied slots; re-derive per-zone free-space.
2. **Wanted queue** — take the next K specs the curriculum+expert want (from
   `planner.analyze`/`build_safe_ready_frontier`), not just the single current goal.
3. **Coordinated batch placement** — for each wanted spec, pick a tile IN ITS ZONE that: fits the
   full footprint (rotated so the entrance faces a path/resource), overlaps nothing built/reserved,
   has a reachable access tile at its Z, and passes the **boxing check** (after reserving it, every
   existing building's entrance + the path frontier stay connected — a flood-fill on the free/road
   graph). Reserve it as a slot. Placing several at once with reservation is what stops them
   competing for the same tiles or sealing each other off.
4. **Verticality fallback** — if a zone has no valid GROUND placement left for the spec: plan a
   **platform deck** — reserve a platform block + a single spiral-stair access column, place the
   building on the platform top at `z = surface+1` (spec must be `can_stack_on`; platform is
   `stackable`). Emit the platform + stairs as prerequisite placements (sequence support first —
   the game stalls a stacked site until its support finishes). Housing/warehouses (also `stackable`)
   can stack directly without a platform.
5. **Serve WHERE** — return the reserved placement for the current goal; `auto_path` connects it to
   the DC trunk (unchanged). The reservation persists to the next cycle.

## Integration decision: **wrap, don't replace** `candidates_for`
`agent/layout.py::LayoutPlanner.place(spec, state, map_data, resources) -> placement|None` becomes
the primary WHERE oracle, consulted by `play_policy._execute_intent` instead of the raw
`planner.candidates_for`. Internally it may still call `candidates_for` as a fallback tile-scorer
within a zone, but it owns the reservation/zone/boxing/verticality logic. The trained model +
planner keep deciding WHAT (goal_ids); the LayoutPlanner owns WHERE. `auto_path` unchanged. Per-
building placement CONDITIONS (water-edge pump, moist farm, flowing-current wheel, height turbine,
storage-near-producer) move into ZONE assignment (step 1) so the 78 generic fall-throughs get a
right-shaped region instead of any flat tile. Reuse: `spatial.py` (zones), `layout_macros.py` (the
dead multi-building stamp templates → wire in for clusters), `path_network`/`auto_path` (trunk).

## Bridge: no change needed for v1
The single-Z `/map` heightmap suffices for ground placement; the agent TRACKS its own platform
placements in the `Reservation` grid to compute stacking Z. `/blueprints` already exposes the
stacking data. (The P1 `/map` multi-Z stackable-surface layer stays an optional later refinement.)

## Ordered implementation tasks (each testable)
- **LP1** `agent/layout.py`: `Reservation` (sparse 3-D occupancy, footprint reserve/free/`fits`) +
  reconcile-from-map/state. Tests: footprint overlap, multi-Z, reconcile.
- **LP2** Zone assignment: partition reachable area around DC per category via `spatial.py`
  scoring (water/moist/flat/DC-distance). Tests: water zone on frontage, farm zone on moist.
- **LP3** Coordinated batch placement + **boxing check** (flood-fill connectivity after reservation).
  Tests: two specs don't overlap; a placement that would box a neighbor is rejected; frontier stays open.
- **LP4** Verticality fallback: platform-deck + stairs + stacked placement when a zone's flat ground
  is exhausted; sequence support-first. Tests: flat-full zone → platform+stack; stacking z=surface+1.
- **LP5** Add platforms/stairs to the action space (`game_schema` allowlist) so the model/planner can
  emit them; planner emits a platform-deck goal on scarcity. Ripples to the retrain.
- **LP6** Wire `LayoutPlanner` into `play_policy` (persist across cycles) + move per-building
  conditions into zones. Live dry-run: colony builds a coordinated, non-boxed, multi-zone base.
- **LP7** Regenerate dataset + retrain (new platform actions + spatial features) on MPS; validate.
  Ties to the stacking-unit V4 spatial features + V5 retrain.

## How this closes the loop with the other two asks
- Verticality (ask #1) = LP4 + LP5 (the stacking unit V1-V3 land inside here).
- The stall-learning loop (ask #2) DETECTS a boxed-in STRUCTURAL_GAP; this planner is the code fix
  that RESOLVES it — exactly the "structural gap → human/LLM writes the fix" path in
  `docs/kb/learning-loop-design.md`. Once LP ships, that gap class should stop recurring.
