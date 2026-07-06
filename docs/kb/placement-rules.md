# Placement Rules (keyed to /map fields)
keys: placement, where to build, adjacency, footprint, water_depth, contamination, moist, occupied, terrain_height, badwater, pump placement, farm placement, path adjacency, tile validity
v: Timberborn 1.0.13.1 · (v?) = unverified vs game blueprints

/map per-tile fields used below: water_depth (0=land), contamination (0=clean, >0=badwater), moist (1=irrigated soil), occupied (1=taken), terrain_height. Footprint = W×H tiles the building covers; ALL covered tiles must be occupied==0 and (unless noted) same terrain_height (flat). A tile is "clean water" iff water_depth>0 AND contamination==0.

## Universal placement gate (apply to EVERY building)
- RULE: A tile is placeable only if occupied==0 for the whole footprint. Never place two buildings on the same tile.
- RULE: Footprint must be FLAT — every covered tile shares one terrain_height (except pumps/farms which straddle a water edge). Reject a spot where covered tiles differ in height.
- RULE: Every building needs ≥1 footprint-edge tile that is Path-adjacent (a Path tile orthogonally next to it) AND that Path chains back to the District Center. No path chain = beavers can't reach it = it never works. (see pathing KB)
- RULE: Do NOT place on water_depth>0 unless the building's spec explicitly allows it (pumps intake, aquatic farm, DoubleLodge on 1-deep). Buildings on badwater (contamination>0) risk contaminating workers.

## Per-building placement (Folktails spec ids)
| spec | footprint WxH,h | workers | WHERE (against /map) |
|---|---|---|---|
| WaterPump | 2x3 h2 | 1 | body on LAND (all 6 tiles water_depth==0), one SHORT edge facing a CLEAN water tile within depth ≤2 (water_depth in 1..2 AND contamination==0). Intake side must still sit over ≥1-deep water at drought's END. |
| DeepWaterPump (IT) | 2x3 h2 | 1 | like WaterPump but source depth ≤6; put intake edge over the DEEPEST clean tile. |
| MediumTank | 2x2 h3 | 0 | flat LAND, path-adjacent, near pump for short haul. water_depth==0. |
| SmallTank | 1x1 h2 | 0 | flat LAND, path-adjacent, cluster beside pump. water_depth==0. |
| Lodge | 2x2 h1 | 0 | flat LAND, path-adjacent, cluster with other Lodges + Campfire. |
| MiniLodge | 2x1 | 0 | fills odd 2x1 LAND gaps in the housing cluster. |
| EfficientFarmhouse | 3x2 h2 | 3 | flat LAND, path-adjacent; place so its field rectangle lands on MOIST tiles (moist==1). The FIELD (not the house) is what needs moist soil. |
| GathererFlag | 1x1 h2 | 1 | on LAND within ~20 tiles of wild berry/food bushes; path-adjacent. Range ~40x40 around flag. |
| Forester | 2x2 (v?) h1 | 1..3 | on LAND on/next to plantable soil (moist or dry-plantable), path-adjacent, near LumberjackFlag. |
| LumberjackFlag | 1x1 (v?) | 1..3 | on LAND within range of standing trees; path-adjacent. |
| SmallWarehouse | (v?) | 0 | flat LAND, CENTRAL to producers+consumers to cut haul distance; path-adjacent. |
| Inventor | 2x2 h3 | 1 | flat LAND, path-adjacent; location non-critical, keep near housing. |
| Path | 1x1 | 0 | on any solid surface (LAND or on top of Levee/Platform/Dam); free, instant. |
| Dam | 1x1 h1 | 0 | IN the river channel (water_depth>0) spanning a cross-section to back water up. |
| Levee | 1x1 h1 | 0 | IN channel or as reservoir wall/floor; buildable-on (put Path on top). |
| Floodgate | 1x1 h2 | 0 | IN a channel gap where you need an openable seal (colony intake / bypass mouth). |

## Placement heuristics keyed to fields
- RULE: WaterPump target = a LAND tile whose orthogonal neighbor is CLEAN water (water_depth 1..2, contamination==0). Scan the clean-water shoreline nearest the DC; pick the closest such land tile. NEVER place a pump facing a tile with contamination>0 (badwater) — it will pump nothing usable and can poison the intake.
- RULE: Farms (EfficientFarmhouse) — first find a contiguous block of moist==1 LAND tiles, THEN place the farmhouse on flat land path-adjacent to that block so its range covers it. If no moist tiles exist, irrigate first (water within irrigation range) or the field grows nothing.
- RULE: Do not scatter. Housing + Inventor + workplaces cluster within a few tiles of the DC on one path spine; water infra hugs the nearest clean shoreline. Scattered buildings inflate walk time and break the path chain.
- RULE: Reject any candidate tile where terrain_height differs across the footprint, or occupied==1 anywhere in it, or (for land buildings) water_depth>0 anywhere in it.
- RULE: Keep all buildings within 70 path-tiles of the DC (green range). Beyond 70 the path turns red and workers waste time walking. (District pathing KB)
- RULE: If the only clean water is far from good building land, run a Path/Levee spine to the shoreline, put pumps+tanks there, and haul stored water back — do not scatter housing out to the water.

sources:
- https://timberborn.wiki.gg/wiki/Water_Pump
- https://timberborn.wiki.gg/wiki/Deep_Water_Pump
- https://timberborn.wiki.gg/wiki/Efficient_Farmhouse
- https://timberborn.wiki.gg/wiki/Gatherer_Flag
- https://timberborn.wiki.gg/wiki/Path
- https://timberborn.wiki.gg/wiki/District_Center
- https://timberborn.wiki.gg/wiki/Irrigation
- https://timberborn.wiki.gg/wiki/Badwater
