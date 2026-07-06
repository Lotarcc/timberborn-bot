# Food Buildings & Crops
keys: food, farm, farmhouse, crop, carrot, potato, wheat, sunflower, cattail, spadderdock, kohlrabi, corn, soybean, cassava, eggplant, canola, berries, gatherer, grill, bakery, gristmill, aquatic, hydroponic, nutrition, silo, food storage
v: Timberborn 1.0.13.1 · (v?) = unverified vs game blueprints

## Consumption (from survival-basics)
- Eat ~2.67 food/day/beaver. Need = 2.67 * P/day. Stockpile = (D+2)*2.67*P before drought (D = next drought days).
- Nutrition variety = well-being: each distinct food eaten gives a Nutrition bonus. Cooked foods also carry +1..+3 well-being.

## Food-producing buildings
spec | name | fac | sci | cost | size | workers | rate | use
GathererFlag | Gatherer Flag | both | start | free | 1x1 h2 | 1 | picks wild bush/tree food in ~40x41 range | instant early food (berries/chestnuts/dandelion/mangrove/coffee)
EfficientFarmhouse | Efficient Farmhouse | F | start | 25 logs | 3x2 h2 | 3 | plants+harvests land crops in range | carrot/potato/wheat/sunflower; stores 50 each
AquaticFarmhouse | Aquatic Farmhouse | F | 150 | 30 logs+10 planks | 2x3 h2 | 2 | grows underwater crops | cattail + spadderdock; place above water, workers use stairs
Farmhouse | Farmhouse (Iron Teeth) | IT | start | 20 logs | 2x2 h2 | 2 | plants+harvests land crops | kohlrabi/cassava/soybean/canola/corn/eggplant
HydroponicGarden | Hydroponic Garden | IT | (v?) | (v?) | (v?) | (v?) | high-density, no irrigated land, needs water+power | stackable; feeds mushroom/algae rations (very high output)
Grill | Grill | F(+IT v?) | 750(bot) | 25 logs | 2x2 h3 | 1 | cooks 1 raw+0.1 log -> food | potato->4 grilled(0.52h); chestnut->2(0.33h); spadderdock->3(0.4h)
Gristmill | Gristmill | F | 180 | 40 logs+20 planks+20 gears | 3x2 h3 | 1 | 1 wheat->1 flour(0.5h); 1 cattail root->1 flour(0.25h) | needs 60hp power
Bakery | Bakery | F | 160 | 15 logs+15 planks+10 gears | 3x2 h4 | 1 | 1 flour+0.1 log-> bread/crackers/pastry | wheat flour->5 bread(1h); cattail flour->4 crackers(0.5h); +1 maple syrup->3 pastries(1.5h)
RationGrill | Ration/processing bldg (IT) | IT | (v?) | (v?) | (v?) | 1 | ferments/cooks IT crops -> rations | corn->corn rations, soy->fermented soybeans, cassava->fermented cassava, eggplant->eggplant rations (v?)
LogPile | Log Pile / food storage | both | start | (v? cheap) | (v?) | 0 | bulk-stores logs/food off building buffers | uncap production
GrainSilo | Food storage (silo/warehouse) | both | (v?) | (v?) | (v?) | 0 | dedicated food stockpile | needed to bank drought food (v?)

## Crops
crop | grow_days | yield | plant_where | notes
Carrot | 4 (2.8 w/Beehive) | 3 | F irrigated land | F, raw, 0.75 food/day/tile; fastest first-farm food
Sunflower | 5 (3.5) | 2 | F irrigated land | F, raw seeds, 0.4/day
Potato | 6 (4.2) | 1 | F irrigated land | F, must Grill (+0.1 log) -> 4 grilled potatoes, 0.667/day
Wheat | 10 (7.0) | 3 | F irrigated land | F, Gristmill->flour->Bakery->Bread; late-game
Cattail | 8 (5.6) | 3 | F underwater (Aquatic FH) | F, roots -> Gristmill flour -> Bakery Cattail Crackers
Spadderdock | 12 (8.4) | 3 | F underwater (Aquatic FH) | F, Grill -> Grilled Spadderdock, 0.75/day
Kohlrabi | 3 | 2 | IT irrigated land | IT, raw, fastest IT crop, 0.667/day
Cassava | 5 | 1 | IT irrigated land | IT, process -> Fermented Cassava
Soybean | 8 | 2 | IT irrigated land | IT, process -> Fermented Soybeans
Canola | 9 | 3 | IT irrigated land | IT, oil crop (Oil Press) not direct food (v?)
Corn | 10 | 2 | IT irrigated land | IT, process -> Corn Rations, 1/day
Eggplant | 12 | 3 | IT irrigated land | IT, process -> Eggplant Rations, 1.5/day
Blueberry(wild) | 12 mature | 3 | wild bush (Gatherer) | both, raw berries 0.25/day, finite wild supply

## Tree-food (via Gatherer/Grill)
- Chestnut Tree: 4 logs + chestnuts; Grill -> Grilled Chestnuts 0.75/day (F). Mangrove Tree: mangrove fruits raw (IT) 0.4/day.

## RULEs
- RULE: FORAGE first — place a Gatherer Flag turn 1 for instant berries before any farm is standing.
- RULE: Keep the Gatherer AND Farmhouse fully staffed; an unstaffed farm grows nothing and a starving farmer cascades to thirst deaths.
- RULE: Size fields to population: land crop tiles needed ~= ceil( 2.67*P / food_per_day_of_that_crop ). Carrot gives 0.75/tile/day, so ~1 tile per 0.28 beavers; plant extra for drought buffer.
- RULE: Only irrigated (moist) soil grows land crops. During drought unirrigated tiles die — irrigate fields (keep water within ~ a few tiles) or bank harvests before the dry season.
- RULE: Cattail/Spadderdock need submerged tiles + Aquatic Farmhouse (F only); do not plant them on dry land.
- RULE: Raw food (carrot/sunflower/kohlrabi/berries) needs NO worker beyond the farm — prefer it for first drought. Cooked food (potato/wheat/cattail/corn/soy) needs an extra powered building + logs; only add once raw food is secure.
- RULE: First-drought food plan (F): carrots (raw, 4-day) + wild berries. Skip Grill/Bakery until survival is buffered.
- RULE: Cooked/varied foods raise well-being & growth — add a second food type early for the Nutrition bonus once basics are safe.

sources:
- https://timberborn.wiki.gg/wiki/Food
- https://timberborn.wiki.gg/wiki/Crops
- https://timberborn.wiki.gg/wiki/Carrot
- https://timberborn.wiki.gg/wiki/Gatherer_Flag
- https://timberborn.wiki.gg/wiki/Efficient_Farmhouse
- https://timberborn.wiki.gg/wiki/Aquatic_Farmhouse
- https://timberborn.wiki.gg/wiki/Farmhouse
- https://timberborn.wiki.gg/wiki/Grill
- https://timberborn.wiki.gg/wiki/Gristmill
- https://timberborn.wiki.gg/wiki/Bakery
- https://timberborn.wiki.gg/wiki/Iron_Teeth
