# Timberborn constants + layout heuristics (Folktails)
keys: pump depth forester ratio plantation reservoir drought water budget hub spoke adjacency layout constants
v: Timberborn 1.0.13.1 (wiki.gg + community, researched 2026-07-11)

## Hard constants (drive the spatial rules)
- Water Pump: max intake depth 2 tiles; internal store 15; degrades in badwater. Place on the DEEP clean edge, not shallow shore.
- Forester: 2x2 h4; plants ONLY on moist tiles; work area ~21 ahead / 20 other dirs from entrance; 1 forester keeps ~4 lumberjacks supplied.
- Lumberjack Flag: 1 worker; cut range ~20 L/R, 21 behind, 18 ahead (THIS build treats cutting as GLOBAL — see reachability memory; encode as config flag). ~1.2 h/tree.
- District Center: path-distance based; workplaces must be within ~70 path-tiles (green->red at 70); stairs = 1 tile.
- Tanks: Small 1x1 = 30 units (15 logs); Medium 2x2 = 300; Large 3x3 = 1200.
- Evaporation: 0.045 units per exposed water surface tile per day.
- Dam blocks 0.5 height (2 logs); Levee blocks 1.0, walkable/buildable on top; Floodgate 0.5 steps, 1-3 wide.
- Contamination: badwater spreads up to 7 tiles horizontally, -5 tiles reach per ascending step (a 2-tile levee lip stops it). Plants on contaminated soil die.
- Water use per beaver/day: 2.13 clean baseline; use 2.5-3.0 for drought sizing (buffer).

## Trees (Folktails): growth days / logs / logs-per-day
Birch 7d/1/0.14 (fastest cash-flow incl. replant) · Mangrove 10d/2/0.20 (+4 fruit) ·
Pine 12d/2/0.17 (+2 resin) · Chestnut 23d/4/0.17 (+3 chestnut) · Maple 28d/6/0.21 (+3 syrup) ·
Oak 30d/8/0.27 (best steady logs/day). Ring plantation: birch inner, pine/mangrove mid, oak/maple outer.

## Top encodable layout rules
RULE drought water budget: stored = beavers * 3.0 * D_drought * 1.25. Reservoir volume first (cheap), tanks second.
RULE deep-narrow reservoir: for volume V pick depth (<= pump reach) then footprint = ceil(V/depth); evaporation = surface_tiles*0.045/day — minimize surface.
RULE pump depth gate: water column under intake >=2 deep AND still >=1 deep at drought END (river drops as it drains).
RULE clean source only: reject a pump tile reachable by badwater flow (BFS from badwater over water, height-drop <= contamination reach).
RULE badtide lip: protect drinking reservoir + plantation + fields behind a levee/terrain lip >=2 tiles above surrounding badwater.
RULE lumberjack early: place over densest WILD-tree cluster; Log Pile <=5 tiles away.
RULE forester plantation: contiguous R×C block of MOIST flat plantable tiles fully inside one forester's area; birch inner ring.
RULE plantation triangle: plantation + lumberjack + log storage all within ~20 walkable tiles pairwise.
RULE forester:lumberjack = 1:4 (self-healing invariant; add 5th lumberjack only with a 2nd forester).
RULE plantation sizing: trees ~= beavers*(1 birch + 2 pine + 1 oak); expand if log storage trends empty.
RULE DC central, approaches CLEAR (no buildings on the town-hall spine — user rule); all workplaces within 70 path-tiles.
RULE hub-and-spoke: one main path spine from DC, short spurs to clusters; minimize turns+length (each counts to 70).
RULE spoke order: DC -> housing -> storage -> workplace -> resource (short commute AND haul).
RULE storage co-located with consumer: Log Pile next to lumberjack/forester and next to each log-consumer (Lumber Mill, Gear Workshop).
RULE production-chain adjacency: logs->planks->gears physically chained; food: farmhouse on moist soil adjacent to field, grill/bakery next to crop storage.

## Wild -> forester transition (phased)
A wild harvest (lumberjack on natural forest, no forester). B overlap ~day 4-8: build forester on moist grid overlapping the cut zone; plant birch. C relocate cut zone onto maturing plantation as wild thins (start forester by ~day 6-8 so first birch ring matures before wild stock is exhausted ~day 20-30). D steady state 1:4, diversify rings.

Sources: timberborn.wiki.gg (Forester, Lumberjack_Flag, Trees, District_Center, Water_Pump, Tanks, Contaminated_Terrain, Water); switchbladegaming reservoir math; Steam community tree-planting thread. (timberborn.org is an AI fan site — not authoritative.)
