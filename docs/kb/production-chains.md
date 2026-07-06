# Production Chains — rates, ratios, when to start
keys: production chain, planks, gears, treated planks, lumber mill, bread, gristmill, bakery, grill, ratios, throughput per day, power, science rate, when to build
v: Timberborn 1.0.13.1 · (v?) = unverified vs game blueprints · rates assume 16h workday, 100% wellbeing
Demand anchors: water 2.13/beaver/day · food 2.67/beaver/day.

## WOOD chain: wild trees → logs → planks → gears → treated planks
| Stage | Building (cost) | Workers/Power | Rate/day | Science |
|---|---|---|---|---|
| Logs | LumberjackFlag (free) | 1 | ~10–25 logs (1.2h/tree) | 0 |
| Replant | Forester (10L+7P) | 1 | ~9 birch or ~17 oak saplings | 30 |
| Planks | LumberMill (15L) | 1 + 50hp | ~12 planks (1.3h ea, 1 log→1 plank) | 0 |
| Gears | GearWorkshop (15L+25P) | 1 + 120hp | ~8 gears (3h ea, 1 plank→1 gear) | 100 |
| Resin | Tapper's Shack (20L+20P+10G) | 1 | 0.286 resin per tapped pine | 500 |
| TreatedPlank | WoodWorkshop (20L+40P+40G) | 1 + 250hp | ~5.3 (3h ea, 1 plank+1 resin→1) | 800 |

Ratios — RULE: keep **1 Forester per 4 LumberjackFlags**; **1 LumberMill per ~2 working lumberjacks** (12 log/day intake ≈ half their cut; the rest stays as raw logs — you always need raw logs too); **2 LumberMills : 3 GearWorkshops** if gears run full-time; **~19 tapped pines per WoodWorkshop**.
Logs/day/tile planted: birch 0.14 · pine 0.17 · maple 0.21(+syrup) · oak 0.27. RULE: long-term plantations = oak; quick restock = birch.
WHEN: LumberMill+power cycle 2 (after first drought secured) — planks gate Forester, GearWorkshop and most tech buildings. GearWorkshop cycle 3. Tapper/WoodWorkshop cycles 6+ only when science is spare.

## POWER
| Building | Cost | Output | Note |
|---|---|---|---|
| PowerWheel | 20L | 50hp | 1 worker treadmill; output scales with speed |
| WaterWheel | 50L | ~270hp per 1 m³/s flow | no worker; needs flowing 2-wide channel; STOPS in drought |
RULE: First power = 1 PowerWheel glued to the LumberMill (50hp exactly). Switch to WaterWheel when a reliable flow exists, but keep a PowerWheel for droughts.

## WATER chain
Pump (12L, 1 worker): 1 water/0.33h ⇒ **~48/day**; net tank fill = 48 − 2.13·P.
RULE: pumps = ceil(P·2.13/48) + 1 extra for tank-filling before each hazard; tanks sized to (D+2)·2.13·P (SmallTank 30 cap/15L; MediumTank 300 after tech).

## FOOD chains (per-tile yields incl. growth)
| Chain | Buildings | Growth | Yield/tile/day | Feeds formula | WHEN |
|---|---|---|---|---|---|
| Berries (raw) | GathererFlag free | 12d regrow | 0.25/bush | bridge food only | day 0 |
| Carrots (raw) | EfficientFarmhouse 25L (3 farmers) | 4d | 0.75 | tiles = 3.6·P | day 3–4 |
| Potatoes→Grill | +Grill 25L (1 worker) | 6d | 0.17 raw ×4 = 0.68 grilled | tiles = 3.9·P | cycle 4+ (variety) |
| Wheat→Flour→Bread | Gristmill 40L+20P+20G, 60hp, sci180 + Bakery 15L+15P+10G, sci160 | 10d | 0.3 wheat ×5 bread/flour = 1.5 food/tile/day | see ratios | cycle 6+ |
| Sunflowers (raw) | farmhouse | 5d | 0.4 | variety bonus | cycle 4+ |

Bread ratios: Gristmill 2 flour/h (32/day) : Bakery 1 flour/h → 5 bread (80/day). **1 Gristmill (part-time) : 1 Bakery : ~53 wheat tiles**; 1 full Bakery feeds ~30 beavers. Bread is the densest food per farm tile (1.5 vs carrot 0.75) — but never before the gear/power base exists.
Grill: 1 potato→4 grilled in 0.52h — one part-time Grill covers any early colony; farm tiles are the bottleneck, not the Grill.
RULE: Carrots stay the staple until P>25; add 2nd food type for Nutrition wellbeing at cycle 4; start bread only when P>20 AND gears+power exist AND carrots already cover survival.
RULE: Farmer capacity = ~8 tiles maintained/farmer/day ⇒ max tiles per farmer = 8 × growth_days (carrot 32, potato 48, wheat 80). Never plant a field bigger than farmers × that cap.

## SCIENCE chain
Inventor (12L, 1 worker) = 1 SP/h ⇒ **~16 SP/day**. Observatory (sci 1000) later.
Spend order & cost: Forester 30 → GearWorkshop 100 → water tech Levee/MediumTank/Floodgate (~120) → Bakery 160 + Gristmill 180 → Sluice 400 → Tapper 500 → WoodWorkshop 800.
RULE: 1 Inventor from cycle 2; add a 2nd when a >300-SP target is queued and survival is automated.

sources:
- https://timberborn.wiki.gg/wiki/Lumber_Mill · /wiki/Gear_Workshop · /wiki/Wood_Workshop · /wiki/Tappers_Shack · /wiki/Treated_Plank · /wiki/Trees
- https://timberborn.wiki.gg/wiki/Water_Pump · /wiki/Power_Wheel · /wiki/Water_Wheel · /wiki/Inventor
- https://timberborn.wiki.gg/wiki/Crops · /wiki/Gristmill · /wiki/Bakery · /wiki/Grill · /wiki/Gatherer_Flag
