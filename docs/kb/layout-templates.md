# Layout Templates — stampable blocks with (dx,dy) offsets
keys: layout template, block, offsets, stamp, starter block, water block, farm block, forestry block, spacing, footprint, expansion direction, spine
v: Timberborn 1.0.13.1 · (v?) = unverified vs game blueprints
Convention (same as pathing-and-layout.md): origin = DC near corner. +x = along the bank, +y = inland. Water occupies y ≤ -1. A building "at (x,y)" = its min-corner; entrance must touch a path tile. **Verify each footprint against the live blueprint via the API before stamping — adjust, don't skip.**

## Footprints used
| Building | WxD | | Building | WxD |
|---|---|---|---|---|
| DistrictCenter | 3x3 | | EfficientFarmhouse | 3x2 |
| LumberjackFlag / GathererFlag | 1x1 | | Forester | 2x2 |
| WaterPump | 2x3 (v?) | | Inventor | 2x2 |
| SmallTank / SmallWarehouse | 1x1 | | LumberMill / GearWorkshop | 3x2 |
| Lodge | 2x2 | | PowerWheel | 3x1 |

## (a) STARTER BLOCK (DC core) — stamp first
```
y=0 : shoreline row (reserved for water block)
y=1 : PATH spine A, x = -8 .. +14   (the trunk; free)
y=2..4 : DC at (0,2)                (3x3, entrance to spine A)
y=2..3 : Lodge (4,2) (7,2) (10,2)   (2x2 each, 1-tile gaps)
y=2   : SmallWarehouse (13,2) (14,2)
y=5 : PATH spine B, x = -8 .. +14
y=6..7 : Inventor (0,6) · Campfire (3,6) · spare industry slots (6,6)+(10,6) for LumberMill+PowerWheel later
```
RULE: Stamp spine A and B complete BEFORE any building of the block; every later building snaps to a spine — never invent ad-hoc paths.

## (b) WATER BLOCK — at the nearest clean shoreline (shift the whole block along x so pumps face water depth ≤2, contamination 0)
```
y=0  : WaterPump (-6,0) and (-3,0)  (intake edge over water at y=-1; entrance to spine A)
y=2  : SmallTank row (-6,2)(-5,2)(-4,2)(-3,2)(-2,2)... extend +x as needed, all on spine A/B
```
RULE: Tanks always in one contiguous row directly behind the pumps, count = ceil((D+2)·2.13·P/30); extend the row, never scatter tanks.

## (c) FARM BLOCK — on nearest moist==1 soil, off spine B via a 1-wide branch
```
FarmBranch : path from spine B straight to block, +1 ring path around the field
Farmhouse  : EfficientFarmhouse at block corner, long side on the branch
Field      : N x M contiguous tiles adjacent to the ring path
```
Field sizing rule (farmers × 8 × growth_days, and demand tiles = P·2.67/yield):
| Crop | Tiles/farmer max | Demand tiles for P | Module |
|---|---|---|---|
| Carrot | 32 | 3.6·P | 7x7 for P≈13 (1.5 farmers) |
| Potato | 48 | 3.9·P | 8x8 |
| Wheat | 80 | ~53 tiles per Bakery | 8x7 |
RULE: Field = min(demand tiles rounded up to a rectangle, farmers×cap). One crop per farmhouse. Full EfficientFarmhouse (3 farmers, carrots) maxes at ~96 tiles = 10x10; don't exceed it.
RULE: Keep every field tile within ~15 tiles of its farmhouse (v?) — long walks, not planting speed, are the real farm bottleneck.

## (d) FORESTRY BLOCK — at/into the tree line, off a spine branch
LumberjackFlag work radius ≈20; Forester planting range ≈20; Gatherer ≈20.
```
Branch path along the forest edge
LumberjackFlag at (f, 0rel) and (f+15, 0rel)   – flags every ~15 tiles along the edge (radii overlap ~5)
Forester (2x2) centered between two flags, 1 per 4 flags (cycle 2+)
Plantation: mark a 15x15 planting area per forester; birch first, then oak rows
GathererFlag dropped at any wild-berry cluster inside the branch
```
RULE: Space LumberjackFlags ~15 tiles apart along the tree line; place the Forester so its range covers ALL flags' cut zones (1 forester : 4 flags).

## (e) CONNECTING BLOCKS + EXPANSION
- Spines are horizontal (constant y) every 4 rows: y=1, y=5, y=9... Vertical 1-wide connectors every ~10 tiles of x. Blocks are ~12 wide × 8 deep and always border a spine.
- RULE: Expand along the shoreline (±x) first — water access is the scarce resource; expand inland (+y) only for farms (moist soil) and forestry blocks.
- RULE: Before stamping any block, check path distance of its far corner to DC ≤ 70; at >60 stop expanding and plan a 2nd DC instead.
- RULE: Reserve rows y=6..7 industry slots for LumberMill(3x2)+PowerWheel(3x1) glued side-by-side (power touches) — power buildings must be footprint-adjacent to consumers or linked by shafts.

sources:
- https://timberborn.wiki.gg/wiki/District_Center · /wiki/Lumberjack_Flag · /wiki/Forester · /wiki/Gatherer_Flag
- https://timberborn.wiki.gg/wiki/Efficient_Farmhouse · /wiki/Lodge · /wiki/Small_Tank · /wiki/Water_Pump · /wiki/Path
- https://steamcommunity.com/app/1062090/discussions/0/3825287950905045134/ (tiles per farmer)
- https://timberborn.org/articles/early-game-strategy
