# Folktails Building Catalog (sizing & affordability master table)
keys: catalog, all buildings, footprint, size, dimensions, cost, science, unlock, power, workers, stackable, placement, overlap
v: Timberborn 1.0.13.1 · (v?) = unverified vs game blueprints · spec = bare id, append ".Folktails" for full spec
Size = WxDxH tiles (footprint W×D on the ground grid; H = blocks of height occupied). Never overlap footprints.
power: `t` = transmits power through itself · `→N` = produces N hp · `N` = consumes N hp · — = no power role
sci — = available at start. Costs: l=log, p=plank, g=gear, tp=treated plank, m=metal block, s=scrap.

## District & labor
spec | name | size | cost | wk | sci | power | notes
DistrictCenter | District Center | 3x3x5 | free | 4 | — | t | colony hub; builders/haulers; paths turn red >70 tiles (efficiency, no hard cap)
BuildersHut | Builders' Hut | 3x2x2 | 20l+10p | 4 | 400 | t | +4 builders; reach ~10 tiles off path
HaulingPost | Hauling Post | 3x2x4 | 20l+10p | 10 | 250 (v?) | t | haulers carry 2x (28); frees workers from hauling
DistrictCrossing | District Crossing | 2x3x2 | 30l+15p | ≤10/side | 600 | — | goods exchange between 2 districts; place on border

## Paths, platforms & transport (stacking backbone)
spec | name | size | cost | wk | sci | power | notes
Path | Path | 1x1x1 | free | 0 | — | — | instant, no builder; everything needs path access
Stairs | Stairs | 1x1x2 (v? h) | 1l+4p | 0 | 70 | — | ±1 level; counts 1 tile for range; works underwater
SpiralStairs | Spiral Stairs | 1x1x2 (v? h) | 1l+4tp | 0 | 350 | — | compact vertical climb
Platform | Platform | 1x1x1 | 6p | 0 | 100 | — | BUILD ON TOP: most buildings can sit on platforms
DoublePlatform | Double Platform | 1x1x2 | 8p | 0 | 150 | — | 2-high platform; stairs fit underneath
TriplePlatform | Triple Platform | 1x1x3 | 10p | 0 | 200 | — | 3-high platform
MetalPlatform3x3 | Large Metal Platform 3x3 | 3x3x1 | 30m | 0 | 1000 | — | supported from center 1x1 only (v?)
MetalPlatform5x5 | Large Metal Platform 5x5 | 5x5x1 | 90m | 0 | 2000 | — | supported from center 1x1 only (v?)
SuspensionBridge1x1..6x1 | Suspension Bridge 1–6 | Nx1x1, N=1..6 | 4l+4p per N | 0 | 150–1800 by N (v?) | — | spans N+1 blocks; nothing can be built on top; anchor both ends
ZiplineStation | Zipline Station | 2x3x4 | 20l+40p+20m | 0 | 700 | — | beaver fast travel ~2.5x speed; link stations via pylons
ZiplinePylon | Zipline Pylon | 1x1x4 | 20p+10g+10m | 0 | 600 | — | line support; can stand in water
ZiplineBeam | Zipline Beam | 1x1x1 | 20p+10g+10m | 0 | 600 | — | horizontal zipline connector
Tunnel | Tunnel | 1x1x1 | 6p+1 explosive+1 extract | 0 | 2000 | — | bores walkway through terrain (v? present in 1.0.13)
DirtExcavator | Dirt Excavator | 5x6x3 | 100g+100tp+50m | 4 | 2000 | 200 | digs dirt from 5x5 area, needs water access (v? present in 1.0.13)

## Water & hydro (details in buildings-water.md)
spec | name | size | cost | wk | sci | power | notes
WaterPump | Water Pump | 2x3x2 | 12l | 1 | — | — | water-end must overhang water ≤2 deep; buffer 15
LargeWaterPump | Large Water Pump | 3x4x3 | 20l+5g+10tp | 3 | 400 | — | =5 Water Pumps; depth ≤4; buffer 60
BadwaterPump | Badwater Pump | 2x3x2 | 20l+10g+5m | 1 | 250 | — | pumps badwater (for Explosives/injection), depth ≤2
BadwaterRig | Badwater Rig | 3x3x4 | 400g+200tp+150m | 10 | 4000 | — | must sit ON a Badwater Source; seals it; 40 badwater/h
MechanicalPump | Mechanical Water Pump | 3x3x3 | 50g+25tp+25m | 0 | 2500 | 700 | powered bulk pump; base acts as 3-block levee
FluidDump | Fluid Dump | 2x1x2 | 10l+10p | 1 | 250 | — | releases stored fluid as flow; irrigation puddles
SmallTank | Small Tank | 1x1x2 | 15l | 0 | — | — | 30 water; evaporation-proof
MediumTank | Medium Tank | 2x2x3 | 30p+20g | 0 | 120 | — | 300 water
LargeTank | Large Tank | 3x3x3 | 80p+60g+30m | 0 | 600 | — | 1200 water
Dam | Dam | 1x1x1 | 20l (v?) | 0 | — | — | holds ~0.65 block then spills; NOT stackable
Levee | Levee | 1x1x1 | 12l | 0 | 120 (v?) | — | 100% seal; STACKABLE; buildable-on
Floodgate | Floodgate | 1x1x2 | 10l+5p | 0 | 150 | — | settable 0–1 in 0.05 steps (taller variants exist v?)
Sluice | Sluice | 1x1x1 | 5l+5m | 0 | 400 | — | auto gate by depth/contamination
ContaminationBarrier | Contamination Barrier | 1x1x1 | 5p+1m | 0 | 400 | — | blocks soil contamination spread; must be on ground

## Food
spec | name | size | cost | wk | sci | power | notes
GathererFlag | Gatherer Flag | 1x1x2 | free | 1 | — | — | picks bushes/tree food in 40x41 area; day-1 food
EfficientFarmhouse | Efficient Farmhouse | 3x2x2 | 25l | 3 | — | — | plants/harvests carrot/potato/wheat/sunflower on irrigated land
AquaticFarmhouse | Aquatic Farmhouse | 2x3x2 | 30l+10p | 2 | 150 | — | cattail/spadderdock in shallow water; place at water edge
Beehive | Beehive | 1x1x1 | 10l+15p+20 paper | 0 | 400 | — | ~30% faster crop growth nearby (≤39 crops); bee-sting risk
Forester | Forester | 2x2x4 | 10l+7p | 1 | 30 | — | plants trees/bushes, range ~20 tiles; F plants birch/pine/oak/maple/chestnut/dandelion
Grill | Grill | 2x2x3 | 25l | 1 | — (bot 750) | t | 1 potato+0.1l→4 grilled (0.52h); chestnut, spadderdock recipes; 1 recipe at a time
Gristmill | Gristmill | 3x2x3 | 40l+20p+20g | 1 | 180 | 60 | wheat/cattail root → flour
Bakery | Bakery | 3x2x4 | 15l+15p+10g | 1 | 160 | — (v?) | flour+0.1l→bread/crackers; +maple syrup→pastries

## Wood, industry & metal
spec | name | size | cost | wk | sci | power | notes
LumberjackFlag | Lumberjack Flag | 1x1x2 | free | 1 | — | — | fells marked trees, range ~20/21/18 tiles from entrance
LumberMill | Lumber Mill | 3x2x3 | 15l | 1 | — | 50 | 1 log→1 plank (1.3h)
GearWorkshop | Gear Workshop | 3x2x3 | 15l+25p | 1 | 100 | 120 | 1 plank→1 gear (3h)
PaperMill | Paper Mill | 3x2x2 | 40l+40p+15g | 1 | 250 | 80 | 1 log→2 paper (1.6h)
WoodWorkshop | Wood Workshop | 4x2x3 | 20l+40p+40g | 1 | 800 | 250 | 1 pine resin+1 plank→1 treated plank (3h)
TappersShack | Tapper's Shack | 2x2x2 | 20l+20p+10g | 1 | 500 | — | taps mature pines (resin) & maples (syrup) in range
ScavengerFlag | Scavenger Flag | 1x1x2 | free | 1 | 250 | — | collects scrap metal from surface ruins, 40x41 range
Mine | Mine | 5x5x3 | 250l+350g+200tp | 10 | 4000 | — | ONLY on Underground Ruins tile; 1 tp→5 scrap (1.8h)
Smelter | Smelter | 2x4x3 | 50p+20g+30s | 1 | 300 | 200 | 2 scrap+0.2 log→1 metal block (2h)
Refinery | Refinery | 2x3x4 | 30p+10g+10m | 2 | 400 | t | food+water→biofuel (potato best: 2+2→30); syrup+extract→catalyst
PrintingPress | Printing Press | 4x2x2 | 50l+30g+30m | 2 | 400 | 150 | paper→books; paper+plank→punch cards
ExplosivesFactory | Explosives Factory | 4x2x2 | 30p+30g+30m (v?) | 1 | 400 | 150 | badwater→explosives
Dynamite | Dynamite | 1x1x1 | 1 explosive | 0 | 600 | — | consumable: destroys 1 terrain block below; chains sideways

## Power
spec | name | size | cost | wk | sci | power | notes
PowerWheel | Power Wheel | 3x1x2 | 20l | 1 | — | →35–85 | beaver-powered; day-1 power
WaterWheel | Water Wheel | 3x2x4 | 50l | 0 | — | →270/(m³/s) | needs flowing water under it; output scales with current
WindTurbine | Wind Turbine | 3x3x4 | 20l+20p | 0 | 120 | →0–150 (avg ~68) | base is 1x1, only 3x3 at heights 2–4; does NOT transmit; wind ≥30%
LargeWindTurbine | Large Wind Turbine | 3x3x5 | 40p+20g+30 paper | 0 | 1400 | →0–300 (avg ~144) | 1x1 base; wind ≥20%
GravityBattery | Gravity Battery | 1x2x4 | 40p+40g+10m | 0 | 400 | store 4000 hph flat | +2000 hph per empty block below weight (max 62000 at 29-deep drop); place on cliff/platform edge
PowerShaft | Power Shaft | 1x1x1 | 1l | 0 | — | t | horizontal power link; stack for walls of shafts
VerticalPowerShaft | Vertical Power Shaft | 1x1x1 | 2l+2p+1g | 0 | 40 | t | ONLY way to route power straight up/down

## Science & bots
spec | name | size | cost | wk | sci | power | notes
Inventor | Inventor | 2x2x3 | 12l | 1 | — | — | 1 sci/h; only start-game science
Observatory | Observatory | 3x3x4 | 80p+30g+10 pine resin | 4 | 1000 | 200 (v?) | 13.3 sci/h; best F science
BotPartFactory | Bot Part Factory | 3x3x2 | 50p+25g+15m | 1 | 500 | 150 | makes bot heads/chassis/limbs
BotAssembler | Bot Assembler | 3x3x1 (v? h) | 100p+50g+50m | 2 | 750 | 250 | assembles bots (~36h each)
BiofuelTank | Biofuel Tank | 1x1x2 (v?) | 50p+25g+15m (v?) | 0 | 200 (v?) | — | bots refuel here
CatalystTank | Catalyst Tank | 1x1x2 (v?) | ? (v?) | 0 | 600 (v?) | — | stores catalyst

## Housing (Folktails breed in dwellings)
spec | name | size | cost | wk | sci | power | notes
Lodge | Lodge | 2x2x1 | 12l | 0 | — | t | 3 beds; STACKABLE (build housing on housing/platforms)
MiniLodge | Mini Lodge | 2x1x1 | 5l | 0 | 50 | t | 1 bed; NO breeding possible inside; gap filler
DoubleLodge | Double Lodge | 2x2x2 | 20l | 0 | 150 | t | 6 beds; entrance 1 block UP — needs path/stairs at +1; can stand in 1-deep water
TripleLodge | Triple Lodge | 2x3x2 | 35l | 0 | 250 | t | 9 beds; entrance 1 block up; can stand in 1-deep water; densest F housing

## Storage
spec | name | size | cost | wk | sci | power | notes
SmallPile | Small Pile | 1x1x1 (v? h) | 2l | 0 | — | — | 20 cap, raw goods (logs etc.); ground only
LargePile | Large Pile | 3x3x1 (v? h) | 6l | 0 | — | — | 180 cap raw goods
UndergroundPile | Underground Pile | 3x3x1 top (v?) | 20l+40p+20g | 0 | 1000 | — | 1000 cap; needs ≥2 solid terrain layers below; top stays buildable (v?)
SmallWarehouse | Small Warehouse | 1x1x1 | 3l | 0 | — | — | 30 cap, 1 good type; buildable-on
MediumWarehouse | Medium Warehouse | 2x3x1 | 15l | 0 | — | — | 200 cap; buildable-on
LargeWarehouse | Large Warehouse | 3x3x2 | 60l+80p | 0 | 250 | — | 1200 cap; buildable-on

## Wellbeing, health & decor
spec | name | size | cost | wk | sci | power | notes
Campfire | Campfire | 3x3x1 | 15l | 0 | — | — | social, 5 visitors; must be on terrain
RooftopTerrace | Rooftop Terrace | 3x2x1 | 15l | 0 | — | — | social, 8 visitors; MUST be placed on top of another building
MedicalBed | Medical Bed | 1x1x1 | 5l+1p | 0 | 80 | — | heals Injury; 1 patient; place near housing
Herbalist | Herbalist | 2x2x3 | 10p+20g (v?) | 1 | 300 | — | makes antidotes → cures Contamination
Lido | Lido | 4x3x2 | 40l+30p | 0 | 250 | — | fun + wet fur, 7 visitors; must border water (≥0.2 deep)
Agora | Agora | 5x5x4 (v?) | 120l+40p (v?) | 0 | 300 (v?) | — | social, 15 visitors
Carousel | Carousel | 6x5x2 (v?) | 50p+50g+40m (v?) | 0 | 700 | 400 (v?) | fun, 12 visitors; POWERED attraction; entrance 1 up (v?)
DanceHall | Dance Hall | 5x5x2 (v?) | 100l+50tp+20m | 0 | 1200 | — | best social (+5), 20 visitors
MudPit | Mud Pit | 3x3x1 | 60l+40tp (v?) | 0 | 1800 | — | fun, 8 visitors; consumes dirt (v?)
Bench | Bench | 1x1x1 | 6l+2p | 0 | 80 | — | 2 seats; fits under platforms
Hedge | Hedge | 1x1x1 | 3l | 0 | 150 | — | aesthetics decor
Scarecrow | Scarecrow | 1x1x2 | 5l+10 paper | 0 | 200 | — | aesthetics + ~30% crop growth boost nearby (v?)
BeaverStatue | Beaver Statue | 1x1x2 | 50l+10p | 0 | 500 | — | aesthetics, range 3
FarmerMonument | Farmer Monument | 2x2x3 | 200l | 0 | 1000 | — | awe, range 7
BrazierOfBonding | Brazier of Bonding | 2x2x4 | 400p | 0 | 3000 | — | awe, range 10
FountainOfJoy | Fountain of Joy | 5x5x4 (v?) | 400p+100tp+300m | 0 | 12000 | — | wonder-tier awe; base must be submerged in water

RULE: reserve the FULL WxD footprint plus a path tile at the entrance; a building without path access never operates.
RULE: stack vertically with Platforms (1/2/3-high) — build platform, then path + building on top; Lodges/warehouses/most workplaces fit on platforms. Suspension bridges cannot carry buildings.
RULE: entrance-at-+1 buildings (Double/Triple Lodge, Carousel) need a path at entrance level, not ground level.
RULE: power does NOT flow through paths — chain buildings marked `t` or add Power Shafts; going up a level requires Vertical Power Shaft.
RULE: check `sci` before planning: with 0 science only the `—` rows are available (DistrictCenter, Path, WaterPump, EfficientFarmhouse, GathererFlag, LumberjackFlag, Lodge, Campfire, Inventor, LumberMill, PowerWheel, WaterWheel, piles/warehouses/SmallTank, Dam, Grill).
RULE: Mine only on Underground Ruins; BadwaterRig only on a Badwater Source; RooftopTerrace only on a building; Campfire only on terrain.

sources:
- https://timberborn.wiki.gg/wiki/Housing · /wiki/Storage · /wiki/Power · /wiki/Paths_and_Structures · /wiki/Science
- /wiki/Water_Pump · /wiki/Large_Water_Pump · /wiki/Badwater_Pump · /wiki/Badwater_Rig · /wiki/Gatherer_Flag · /wiki/Efficient_Farmhouse · /wiki/Aquatic_Farmhouse · /wiki/Beehive · /wiki/Grill · /wiki/Gristmill · /wiki/Bakery
- /wiki/Lumberjack_Flag · /wiki/Forester · /wiki/Lumber_Mill · /wiki/Gear_Workshop · /wiki/Paper_Mill · /wiki/Wood_Workshop · /wiki/Tapper%27s_Shack · /wiki/Scavenger_Flag · /wiki/Mine · /wiki/Smelter · /wiki/Refinery · /wiki/Printing_Press · /wiki/Explosives_Factory · /wiki/Dynamite
- /wiki/Power_Wheel · /wiki/Water_Wheel · /wiki/Wind_Turbine · /wiki/Large_Wind_Turbine · /wiki/Gravity_Battery · /wiki/Power_Shaft · /wiki/Vertical_Power_Shaft
- /wiki/Inventor · /wiki/Observatory · /wiki/Bot_Part_Factory · /wiki/Bot_Assembler
- /wiki/Lodge · /wiki/Mini_Lodge · /wiki/Double_Lodge · /wiki/Triple_Lodge
- /wiki/District_Center · /wiki/District_Crossing · /wiki/Hauling_Post · /wiki/Builders_Hut · /wiki/Contamination_Barrier
- /wiki/Small_Pile · /wiki/Large_Pile · /wiki/Underground_Pile · /wiki/Small_Warehouse · /wiki/Medium_Warehouse · /wiki/Large_Warehouse
- /wiki/Campfire · /wiki/Rooftop_Terrace · /wiki/Medical_Bed · /wiki/Herbalist · /wiki/Lido · /wiki/Agora · /wiki/Carousel · /wiki/Dance_Hall · /wiki/Mud_Pit · /wiki/Bench · /wiki/Hedge · /wiki/Scarecrow · /wiki/Beaver_Statue · /wiki/Farmer_Monument · /wiki/Brazier_of_Bonding · /wiki/Fountain_of_Joy
- /wiki/Zipline_Station · /wiki/Zipline_Pylon · /wiki/Zipline_Beam · /wiki/Suspension_Bridge · /wiki/Platform · /wiki/Stairs
