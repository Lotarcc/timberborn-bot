# Placement & Verticality — what the agent MISSES (gap analysis)

> 2026-07-12. Synthesis of four investigations: game **decompile** (ilspycmd on the 1.0.13.1
> Managed DLLs), **wiki/community** research (timberborn.wiki.gg + Steam + gamedeveloper.com),
> a **code audit** of our placement/planner/model, and a **live experiment** against the running
> bridge. Full source notes: `scratchpad/{decompile-stacking,wiki-strategies,code-audit}.md`.

## TL;DR

**The docs describe a 3-D, footprint-aware, water-engineering base builder. The code is a 2-D,
single-tile, flat-dry-land point placer.** Our knowledge (`docs/kb/*`) already documents most of
this — but almost none of it is *implemented*. The single biggest miss is the **entire vertical
axis**: platforms, stacking, and building up when flat ground runs out. This is very likely the
root cause of the live "placed → boxed in → demolished as unreachable" loop: once a flat, reachable
level fills, the agent has **no way up or out**.

## Live-verified: stacking WORKS, the agent just never does it

Against the running colony (bridge `place_building`):

| Placement | Result |
|---|---|
| `Platform` on ground (z=4) | ✅ accepted |
| `SmallPile` on top of that platform (z=5) | ✅ accepted — **build-on-top works** |
| `Lodge` directly on top of a finished `Lodge` (z+1) | ✅ accepted — **housing-on-housing works** |

Meanwhile `placement.py:206` sets `z = terrain height` for *every* candidate, and Platforms/Stairs
aren't even actions (see below). The agent is structurally incapable of stacking.

## The stacking mechanism (decompiled ground truth)

Stacking is **per-block blueprint data**, not a per-building whitelist. Every footprint cell has:
- `MatterBelow ∈ {Ground, GroundOrStackable, Air, Any, Stackable}` — what must be in the cell below it.
- `Stackable ∈ {None, BlockObject, UnfinishedGround}` — whether something can be built on top of it.

`Timberborn.BlockSystem.MatterBelowValidator.Validate` is the authority. Building **B** can be placed
at world Z on top of whatever is at Z-1 iff B's base cell `MatterBelow` is `Stackable`/`GroundOrStackable`
(or `Air`/`Any`) **and** the cell below has `Stackable = BlockObject`. Platforms are just buildings whose
top is `Stackable=BlockObject`; "supported from center only" (metal platforms) = only the center cell is
`MatterBelow=Ground`, the rest `Any`. Consequences the agent must respect:

1. **Candidate Z is "one above the highest stackable surface,"** not terrain height. A platform/stackable
   building top at `topZ` makes `topZ+1` a legal base — mirror `MatterBelowValidator` before committing.
2. **Reachability is checked at EVERY Z level the building spans** (`BlockObjectAccessGenerator` /
   `HighBlockObjectAccessesAdder`): a stacked building needs a walkable adjacent navmesh tile *at its own
   height* — a platform top (`GenerateFloorsOnStackable`) or a stairs edge. Support alone is not enough.
3. **Stairs/SpiralStairs have no special class** — vertical traversal is generic `BlockObjectNavMeshEdgeSpec`
   Start→End edges with different Z. To go up a level you must place a stair/platform that declares that edge.
4. **Validation is 3-tier**: hard gate (`MatterBelow`/overlap/bounds) → **soft** reachability warning
   (placement *allowed*, but the site never gets a builder → shows "unreachable", which is exactly our
   demolish loop) → construction-site gate (a stacked site **stalls** until its support below finishes).
   So: sequence the support building first; and treat "unreachable" as *don't place yet*, not *demolish after*.
5. **Overlap is per-occupation-flag**, not per-tile: a path (`Path`/`Middle` occupation) and a `Top`-only
   object can legally share one (x,y,z) cell. Treating a tile as binary occupied/free is too coarse.
6. **Power is not wireless and not free vertically** — connectors are 6-way (`Direction3D`); vertical shafts
   are the vanilla way up/down. A stacked workshop needs a shaft path back to a generator.

## Gap catalog (ranked by impact on play)

1. **Verticality / stacking — entirely absent (highest impact).** `z` is always terrain height
   (`placement.py:206`, `planner.py:1203,1245`). Platform/DoublePlatform/TriplePlatform/Stairs/SpiralStairs/
   Overhang/MetalPlatform (13 specs, `category:"paths"`) are excluded by `game_schema.py:31`
   `_GAMEPLAY_CATEGORIES` → **never actions** → the 87-build model space can't choose to go up. `auto_path`
   builds only `Path`, never a platform/ramp. `footprint.z` is in the data but unread. `layout_macros.py`
   (has vertical-gap-column templates) is imported by **nobody**.
2. **Water-infrastructure placement is BROKEN.** Dam/Levee/Floodgate/DoubleFloodgate/Sluice/Valve are actions
   (3e emits them) but fall through to **generic flat-DRY-land** placement (`planner.py:811-818`, requires
   `_is_land`, i.e. `water_depth<=0`). They must sit on `water_depth>0` — so a reservoir wall can **never** be
   placed. Drought/badtide engineering is undeliverable today.
3. **Per-building placement conditions unmodeled for 78 of 87 specs.** Only 5 are resource/water/moisture-aware
   (WaterPump, Forester, EfficientFarmHouse, LumberjackFlag, GathererFlag); 4 more get an adjacency bonus on
   flat-dry-land (SmallTank, Lodge, SmallWarehouse, Inventor). Everything else — all industry, all power, all
   storage tiers, all extra housing — gets a generic flat-dry tile. Missing conditions incl.: **WaterWheel needs
   double-width FLOWING current** (still water = 0 power), **WindTurbine wants height/unobstructed (≥30% wind)**,
   storage should sit **next to the producers it serves** (haul distance).
4. **Irrigation range gates farming/forestry, unmodeled.** Soil is irrigated ~15–16 tiles from water *at the same
   height*, **−6/−7 per block of elevation**, 3-wide canal = 16 max, >50% pollution = none. We site farms on
   `moist` tiles (good, partial) but don't model the range/elevation penalty, so farm siting will still fail as
   the colony spreads uphill. Forester range = 21 ahead / 20 around; ratio ≈ 1 forester : 4 lumberjacks.
5. **The model has ZERO spatial features.** `game_schema.feature_strings` emits pop/goods/producers/power/
   wellbeing — nothing for flat-ground remaining, elevation, water frontage, or "boxed in". The trained policy
   is **blind by construction** — it can never learn "build a platform when flat ground runs out."
6. **Boxing/reachability is reactive, not preventive.** The candidate scorer filters *currently*-reachable land
   with no post-placement lookahead (does this placement leave a free path-adjacent access tile? does it box a
   neighbor?). Boxing is cured *after the fact* by `demolish_unreachable`. With no vertical escape, a full flat
   level is a dead end.
7. **Multi-tile footprint is ignored.** Every building is treated as a 1×1 point; `footprint{x,y,z}` (in the
   data for every building) is never read for flatness/overlap over the whole footprint or for rotation/flip.
8. **Layout/efficiency mechanics unmodeled**: walk distance (the dominant cost), District range (~70 blocks),
   entrance orientation as a hard constraint, cross-district haul caps (10/side), power-shaft routing, well-being
   building coverage radius, terrain manipulation (dynamite to level, platforms over water/gaps).
9. **Bridge blindness.** `/map` is a single-Z heightmap (one terrain height + one occupied flag per tile). The
   agent can't *see* multi-level occupation, so even a stacking-capable planner couldn't plan a stack from
   `/map` alone — the bridge would need to expose per-(x,y,z) occupation + per-building `Size`/`MatterBelow`/
   `Stackable`/`Occupations` (none of which are pollable today).

## Recommended fixes (prioritized)

**P0 — make verticality possible at all (biggest ROI, unblocks compact bases + escapes boxing):**
- **Bridge**: add endpoints to expose (a) per-building `Size` + per-cell `MatterBelow`/`Stackable`/`Occupations`
  (dump `BlockObjectSpec`/`BlockSpec` per template once), and (b) multi-Z occupation / stackable-surface tops
  around the DC (extend `/map` with a `stack_top`/`buildable_z` layer). Without this the agent is blind upward.
- **Action space**: include `platform`, `double_platform`, `stairs`, `spiral_stairs` (add `paths` — or a curated
  subset — to `_GAMEPLAY_CATEGORIES`, same pattern as the 3c amenity expansion). Ripples into a retrain.
- **Placement**: compute candidate Z as "highest stackable surface + 1"; mirror `MatterBelowValidator`; verify a
  walkable adjacent tile *at that Z* (platform top / stairs edge) before committing; sequence supports first.
- **Planner**: emit a "build a platform deck + stairs" goal when flat reachable ground is scarce; stack housing/
  storage on platforms; one shared spiral-stair column per stack.

**P1 — fix broken/naive placement:**
- Route Dam/Levee/Floodgate/Sluice to **water tiles** (`water_depth>0`), not flat-dry-land (fixes 3e).
- Add per-building placement profiles: WaterWheel→flowing-current span; WindTurbine→height/open; storage→near
  served producers; power consumers→shaft-reachable.
- Model irrigation range (flat ~16, −6/−7 per elevation, 3-wide canal cap) for farm/forester siting.

**P2 — perception + prevention:**
- Add **spatial features** to `game_schema.feature_strings`: flat-reachable-tiles-remaining bucket, elevation
  relief, water-frontage-available, "boxed-in" flag — so the model can learn to go vertical.
- Make placement **preventive**: score down tiles that would box a neighbor or leave no free access tile; prefer
  tiles that keep the frontier open.
- Wire up `layout_macros.py` (or replace it) for multi-building + vertical stamps.
- Model walk-distance / District range / entrance orientation in the candidate scorer.

## Cross-references
Our existing (mostly-unimplemented) knowledge: `buildings-catalog.md` (§ "Paths, platforms & transport"),
`placement-rules.md`, `layout-templates.md`, `pathing-and-layout.md`, `designs-*`, `water-engineering.md`,
`timberborn-constants-layout.md`. The gap is **implementation**, not knowledge, for most of these.
