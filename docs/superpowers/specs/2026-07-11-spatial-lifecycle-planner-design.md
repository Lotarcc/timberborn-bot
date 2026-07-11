# Spatial + Lifecycle Planner — Design

date: 2026-07-11 · status: proposed (autonomous; user to review) · supersedes the
placement parts of the 2026-07-07 controller design (which stands for the loop).

## Problem (from a live screenshot)

The controller plays mechanically but is spatially blind: it places buildings at the
"nearest reachable flat tile", ignoring where trees/water/moist-soil actually are,
never clustering or zoning, and with no lifecycle plan (wild lumberjack now →
forester plantation later). A capable player reasons about the map geometrically and
across time.

## What 5 research fronts unanimously concluded

1. **Division of labor** (architecture front, strongest evidence — the Factorio
   Learning Environment showed top LLMs fail at base-building precisely because they
   had a rich API but NO deterministic spatial solver): put ~90% of spatial work in
   DETERMINISTIC code. The LLM never emits coordinates — it sets intent and ranks
   pre-scored options via structured JSON. LLMs are documented to fail at generating
   geometry, and it does not improve with scale/tokens/a code interpreter.
2. **Spatial primitives** (influence/distance + zoning/layout + how-to-code fronts):
   multi-source BFS distance fields, weighted influence maps, BFS-Voronoi zoning,
   greedy+hill-climb layout, MST/Steiner path networks. All clean in pure Python; our
   grids are small (~30×256, usually less), so no numpy/scipy needed.
3. **Placement** = utility scoring over stacked influence layers → ranked candidates.
4. **Macros** (Voyager lesson applied as code): parameterized layout templates place
   whole sub-bases atomically — one decision, many buildings, zero LLM in the hot path.
5. **Lifecycle** (lifecycle front): DON'T adopt HTN/GOAP/PDDL engines. Extend the
   phased checklist with lifecycle-tagged goals + a plan-REPAIR executor (OPEN /
   SATISFY / RETIRE / RELOCATE). Relocate = build-successor-first, then demolish.
   Retire a temporary only once its successor is actually live.

## Architecture (three deterministic layers + LLM arbiter)

```
/state /map /resources ── spatial.py ──> feature layers (distance/influence/zones)
                              │
                     placement.py (utility scoring) ──> ranked candidate tiles/zones
                              │
                     macros.py (layout templates) ──> atomic multi-building blocks
                              │
   planner.py (phased+lifecycle goals) ─── controller.py (executes; LLM only at forks
                                            with structured JSON + pre-scored options)
```

### spatial.py (NEW, pure stdlib) — the missing "map as a map" layer
- `distance_field(sources, passable, terrain_h, max_step=1)` — multi-source BFS,
  walk-distance to nearest source over reachable tiles (dist_to_tree/water/moist/road/DC).
- `influence(mask_or_field, scale, amplitude, kind='decay')` — exponential/linear
  falloff from sources; `stack(layers, weights)` weighted-sum with negatives=repel;
  `norm()` 0..1. Optional momentum update `I += m*(target-I)` to avoid thrash.
- `label_regions(mask)` flood-fill connected components → centroids/sizes;
  `voronoi_districts(seeds, buildable)` multi-source BFS zoning (terrain-aware).
- `plantable_mask`, `badwater_reach_mask`, `water_depth_column`, `path_dist_from_dc`
  — Timberborn-specific derived layers (moist+flat+unobstructed; badwater BFS with
  height-drop ≤ contamination reach; pump-depth check; DC 70-tile green zone).
- `SpatialCache`: static layers (terrain slope, buildable, walk-graph) computed once
  and versioned; dynamic layers (occupied, dist_to_tree as trees are cut,
  contamination) recomputed per cycle. Perf rule: never rebuild the walk-graph per cycle.
- `path_network(terminals, walk_graph)` — MST/greedy-Steiner over cluster centroids +
  DC to place SHARED path spines (hub-and-spoke), not one path per building.

### placement.py (NEW) — replaces "nearest reachable flat tile"
`score_tiles(features, weights, buildable, occupied, buffers, adjacency)` → ranked
candidates. Per-spec weight profiles encode the Timberborn rules, e.g.:
- WaterPump: +deep-clean-water-edge, −badwater-reach, must pass pump-depth gate.
- Forester/Farmhouse: +moisture, +contiguous-moist-cluster, −contamination, inside lip.
- LumberjackFlag: near-DC + near wild-tree cluster (global cutting), off town-hall buffer.
- Housing: near DC; Storage: between workplace and its consumer; all: town-hall buffer,
  reachability, flat footprint, tile-overlap avoidance.

### macros.py (NEW) — parameterized layout templates
`forester_lumberjack_plantation(moist_cluster)`, `waterfront_pump_tank_bank(shore)`,
`housing_cluster(near_dc, n)`, `farm_block(moist_zone, WxH)`, `drought_reservoir(river
chokepoint, volume)`. Each returns an ordered list of placements+followups its own
utility scoring positioned. The planner emits a macro goal; the controller executes it
as one batch.

### planner.py — phased + lifecycle goals (extend, don't rewrite)
Goals gain `zone` (policy, resolved by placement/macros at exec time), `lifecycle`
(temporary|permanent|scaffold), `replaced_by`, `retire_trigger`, `retire_action`
(keep|demolish|relocate), `priority`, `invariant`. Phases: survival → sustainability →
industry → wellbeing with preconditions. The wild-lumberjack→forester handoff is the
flagship lifecycle rule; forester:lumberjack 1:4 is a self-healing invariant.

### controller.py — plan-repair + LLM arbiter
Add the four repair operators over a PERSISTED goal queue (OPEN/SATISFY/RETIRE/
RELOCATE) so lifecycle transitions patch the plan instead of rebuilding it (avoids
thrash). LLM calls (already fork-only) switch to structured JSON: named entities +
coords + derived features + a short list of code-scored candidate ZONES/templates;
the LLM picks intent + option id, never a tile.

## Staged implementation (each stage: dry-tested, then one live run)
1. **spatial.py primitives** + unit tests (distance fields, influence, zoning, plantable/
   badwater masks) against fixtures. Highest ROI; foundation for everything.
2. **placement.py** utility scoring wired into `candidates_for` (replace nearest-flat-
   tile). Live check: lumberjack near wild trees, pump on deep clean edge, forester on
   moist cluster — visibly sensible placements.
3. **macros.py** + planner macro goals for the forester-plantation and pump-tank-bank.
4. **Lifecycle** goal schema + plan-repair executor; wild→forester handoff.
5. **path_network** hub-and-spoke shared spines; **LLM structured-JSON grounding** at forks.

## Success criteria
Placements are spatially sensible (lumberjack at the forest, pump at deep clean water,
forester on a moist grid, housing clustered by the DC, shared path spines), the colony
sustains wood via the wild→forester transition, survives drought with a sized reservoir,
and all of it runs deterministically (<100ms/cycle) with the LLM only at strategic forks.

## Dependencies
Stdlib-only (pure-Python BFS/flood-fill/greedy; grids are small). No numpy/scipy —
keeps deployment frictionless on the game machine.

## Risks
- Utility weights need tuning → expose them as a config table; start from the researched
  Timberborn rules; let the learning loop nudge them later.
- Macro placement can fail on cramped/irregular maps → fall back to single-goal utility
  placement; report no_land_route/no_valid_zone as a fork.
- Lifecycle relocation mid-drought → gate retire_trigger on successor-live AND not-in-hazard.
