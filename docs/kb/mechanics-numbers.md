# Hard Numbers: Consumption, Growth, Irrigation, Districts, Water Physics
keys: numbers, consumption, water per day, food per day, tree growth, crop growth, irrigation range, moisture, district range, carry capacity, work hours, breeding, tank capacity, dam height, evaporation, drought length
v: Timberborn 1.0.13.1 · (v?) = unverified vs game blueprints

## Per-beaver consumption & body stats
stat | value
Water drunk | 2.12 /day (plan with 2.25)
Food eaten | 2.67 /day (plan with 2.75)
Carry capacity | 14 units (Hauling Post / District Crossing haulers: 28)
Move speed | 2.67 tiles/s base (≈1233 tiles/day)
Work day | 16 h work + 8 h night (24 h day; hours adjustable 0–24)
Lifespan | ~50 days ±10%; kit matures in 5 days
Bots | work 24 h, no food/water; need biofuel (F)

RULE: stockpile before a hazard: water ≥ (D+2)*2.25*P, food ≥ (D+2)*2.75*P (D = predicted event days, P = population).

## Trees (Folktails Forester plants all below)
tree | grow d | logs | extra yield | note
Birch | 7 | 1 | — | fastest; first plantings
Pine | 12 | 2 | 2 pine resin/day (Tapper) | resin → treated planks
Chestnut | 23 | 4 | 3 chestnuts/day (Gatherer) | F-only food tree
Maple | 28 | 6 | 3 maple syrup/day (Tapper) | F-only; syrup → pastries/catalyst
Oak | 30 | 8 | — | best logs/tile long-term
DandelionBush | 3 | — | 1 dandelion | decorative food (v? food value)
BlueberryBush | 12 | — | 3 berries | Gatherer food

RULE: plant Birch for the first log loop; convert to Oak groves once a 30-day buffer exists (Oak = 0.27 logs/day/tile vs Birch 0.14).
RULE: keep living Pines + Maples near a Tapper's Shack (do NOT chop them) — resin/syrup come from standing mature trees.

## Crops (Folktails; grown by Efficient/Aquatic Farmhouse on irrigated/submerged tiles)
crop | grow d | yield | where | chain
Carrot | 4 | 3 | irrigated land | raw — first food
Sunflower | 5 | 2 | irrigated land | raw
Potato | 6 | 1 | irrigated land | Grill → 4 grilled
Cattail | 8 | 3 | shallow water | Gristmill flour → Bakery crackers
Wheat | 10 | 3 | irrigated land | Gristmill → Bakery bread
Spadderdock | 12 | 3 | shallow water | Grill → 3 grilled

- Beehive: ~30% shorter growth time for ≤39 crops nearby (boost pulses every 2 h, 24 h cooldown per crop).
- Crops die within ~0.3–3.5 days on dry soil during drought (wheat/spadderdock most fragile) (v? exact per-crop).

## Irrigation / soil moisture
fact | value
Contamination gate | water <50% polluted irrigates; ≥50% contaminates soil instead
Elevation penalty | each +1 block of terrain rise costs 6 tiles of range; downhill costs nothing
Range vs body size | spread scales with water body size/adjacency; max ≈16 tiles from a ≥3-wide body (v?)
Channel width | 3-wide channels: best evaporation ratio (0.053 m³/day/tile vs 0.200 for 1-wide)
Best pattern | 3x3 puddle fed by Fluid Dump ≈ cheapest irrigation per farmland area (wiki-recommended)
Dry-out | soil dries and crops/trees wither once moisture source gone; brief grace (v? exact hours)

RULE: farmland above river level is nearly un-irrigable (−6 tiles/level): pump water UP with Fluid Dump puddles instead of hoping for spread.
RULE: keep every farm tile within ~10 tiles of water on the SAME level (safe margin under the ≈16 max).

## Districts & logistics
fact | value
District Center | 3x3x5, free, from start; 4 builder/hauler staff
Range | no hard cap; path range indicator green ≤70 path-tiles then red (long walks kill efficiency); stairs count 1 tile
Off-path reach | builders/workers reach ~10 tiles off a connected path (v?)
Between districts | District Crossing exchanges goods; Migrate population via settlement UI

RULE: keep production loops (farm→storage→housing→work) within ~40 path-tiles of the DC; treat 70 as hard budget.

## Water physics, storage & structures
fact | value
Source strength | N m³/s per source tile; stops entirely during drought/badtide
Pump depths | WaterPump ≤2 · LargeWaterPump ≤4 · BadwaterPump ≤2 · MechanicalPump deep (v?)
Tanks | Small 30 (1x1x2) · Medium 300 (2x2x3) · Large 1200 (3x3x3); tanks don't evaporate & stay clean
Dam | 1-high, holds ~0.65 block, excess spills over top; not stackable
Levee | 1-high, seals 100%, stackable to any wall height, buildable-on
Floodgate | body 1x1x2; threshold settable 0.00–1.00 in 0.05 steps; adjacent gates sync
Sluice | auto-closes on contamination >5% or by depth rule; badtide automation
Fluid Dump | 3 water→flow, stops when outlet ~0.5 deep
Evaporation | ~0.05–0.30 m³/day per surface tile depending on exposure (wider/deeper = less per m³); 3-wide channel ≈0.053/tile/day

## Hazards
fact | value
Drought length | grows each cycle; Normal ≈2–3 d early → 10–24 d late (v? late values); Hard up to 30 d (v?)
Badtide | sources emit badwater: contamination ramps 50→100% (12 h), holds, ramps down last 12 h, 0% at temperate
Contaminated beaver | >5% polluted water contact → Unwell → Contaminated (−70% move, no work); cure = 8 antidotes over ~4 d (Herbalist); no natural recovery
Contaminated soil | ≥50% polluted water contaminates ground; crops die in ~0.2–0.3 d; ContaminationBarrier blocks spread

## Breeding (Folktails)
condition | requirement
Adults | ≥2 adults living in the SAME dwelling
Needs | no critical needs (thirst/hunger/etc.) among those adults
Beds | a spare (free) bed must exist for the kit
Housing type | any Lodge except Mini Lodge (1 bed — pairs can't form)
Rate lever | population grows only while free beds exist → build lodges ahead of demand

RULE: to grow population, keep ≥2 free beds per 10 beavers and everyone watered/fed; to FREEZE population (drought rationing), fill all beds exactly.

## Quick planning constants
- 1 beaver-day of survival = 2.25 water + 2.75 food.
- 10 beavers for 10-day drought ≈ 270 water (9 Small Tanks or 1 Medium Tank) + 330 food.
- 1 Water Pump ≈ up to 16 water/16 h day (v? ~1/cycle) → 1 pump per ~6–7 beavers plus refill margin.
- Carrot field: 1 tile ≈ 0.75 food/day → ~3.6 tiles per beaver incl. buffer.

sources:
- https://timberborn.wiki.gg/wiki/Beaver · /wiki/Time
- /wiki/Trees · /wiki/Birch · /wiki/Pine · /wiki/Maple · /wiki/Oak · /wiki/Chestnut_Tree
- /wiki/Crops · /wiki/Beehive
- /wiki/Irrigation · /wiki/Evaporation
- /wiki/District_Center · /wiki/Hauling_Post · /wiki/Update_2 (carry capacity)
- /wiki/Water · /wiki/Water_Source · /wiki/Water_Pump · /wiki/Large_Water_Pump
- /wiki/Dam · /wiki/Levee · /wiki/Floodgate · /wiki/Sluice · /wiki/Fluid_Dump
- /wiki/Drought · /wiki/Badtide · /wiki/Badwater · /wiki/Contamination
- /wiki/Breeding · /wiki/Folktails
- /wiki/Small_Tank · /wiki/Medium_Tank · /wiki/Large_Tank
