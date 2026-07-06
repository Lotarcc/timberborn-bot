# Pathing & Starter Layout
keys: path, pathing, connectivity, district center, walkable, path range 70, layout, base plan, tile plan, cluster, spine, path-adjacent, reachability, starter layout
v: Timberborn 1.0.13.1 · (v?) = unverified vs game blueprints

## Pathing facts
- Beavers reach a building only if a chain of Path tiles connects the building to the District Center (DC). No path chain → construction is UNUSABLE (won't be worked, stocked, or built).
- Path is FREE, no materials, built instantly, no builder. There is no reason to under-build path — connect everything.
- Path range: green ≤70 path-tiles from the DC; beyond 70 the line turns RED = workers walk too long = efficiency drop. Keep the whole base inside 70.
- Stairs / Slopes / Suspension Bridges act as paths automatically (use them to cross elevation or span water). A Levee/Platform can carry a Path on top to bridge water.
- Farmers/gatherers cut the shortest route from their building to their target tile, but the building itself still must be path-connected to the DC.

RULE: Lay the Path SPINE first, before any workplace. A building with no adjacent path never functions — path is the skeleton.
RULE: Every building must have ≥1 footprint-edge tile orthogonally touching a Path tile that chains to the DC. Verify reachability before/after each placement.
RULE: Cluster tightly. One main spine off the DC with short branch stubs to each building beats long scattered spurs. Minimize total path length and walk distance.
RULE: Never place a building "floating" (no path neighbor) expecting to add path later and forgetting — add its path stub in the same step as the building.
RULE: If a building sits >70 path-tiles from the DC, either move it closer or (later game) plan a second District Center — don't leave it in the red.

## Concrete STARTER LAYOUT (relative to DC anchor)
Coordinates are (dx,dy) offsets from the DC's near corner; +x = along the bank, +y = inland away from water. Water is at the DC's water-facing edge. Adjust to the actual clean shoreline from /map.

```
  WATER (clean: water_depth 1..2, contamination 0)
  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
  [Pump][Pump]            <- on land, intake edge over clean water
  --- PATH (shoreline spine) --------------------
  [Tank][Tank][Tank][Warehouse]   <- storage row, 1 tile inland
  --- PATH (spine, parallel) --------------------
  [Lodge][Lodge][Inventor][Campfire]  <- housing/science cluster
  --- PATH branch inland ------------------------
  [ Farmhouse ] + [field field field]  <- field on moist==1 tiles
  [GathererFlag]  ...berry bushes...    [LumberjackFlag][Forester] near trees
```

Ordering rules for the plan:
- Water infra (WaterPump ×N, then SmallTank/MediumTank ×N) sits at the NEAREST clean-water shoreline edge, all on a short path spine right at the bank.
- Storage (SmallWarehouse) goes CENTRAL between producers and housing to shorten hauls.
- Housing (Lodge cluster) + Inventor + Campfire cluster a few tiles inland from the DC, off the same spine — tight, not spread.
- Farms go on the nearest block of moist==1 soil; run a path branch to the Farmhouse; keep fields within its range and adjacent to path.
- Gatherer near wild bushes; Lumberjack+Forester near the tree line — both off short path branches, still ≤70 from DC.

## Tile-plan pattern (repeatable)
1. Read /map: find nearest clean shoreline tile to DC, nearest moist block, nearest tree cluster.
2. Draw the shoreline path spine along the bank at the clean-water edge.
3. Place pumps on land tiles whose neighbor is clean water; tanks directly behind them on the spine.
4. Draw a second parallel path spine 1–2 tiles inland; place housing + Inventor + Campfire + Warehouse on it, clustered.
5. Branch a path to the moist block; place Farmhouse + fields there.
6. Branch short stubs to Gatherer / Lumberjack / Forester at their resource.
7. Re-verify: every building touches path; every path chains to DC; nothing beyond 70 tiles.

RULE: Storage central, water at the water, housing+workplaces clustered inland, farms on moist soil — all strung on 1–2 short parallel path spines off the DC.

sources:
- https://timberborn.wiki.gg/wiki/Path
- https://timberborn.wiki.gg/wiki/District_Center
- https://timberborn.wiki.gg/wiki/Stairs
