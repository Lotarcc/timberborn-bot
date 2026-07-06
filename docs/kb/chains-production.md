# Production Chains (Materials & Food)
keys: chain, production, logs, planks, gears, lumber, sawmill, forester, lumberjack, tree, wood, refined, gristmill, bakery, grill, dependency, bottleneck, hamsterpower, power, rate
v: Timberborn 1.0.13.1 · (v?) = unverified vs game blueprints

## Core material buildings
spec | name | fac | sci | cost | size | workers | rate | use
LumberjackFlag | Lumberjack Flag | both | start | free | 1x1 h2 | 1 | fells 1 tree/~1.2h -> logs (yield by tree) | stores 20 logs
Forester | Forester | both | 30 | 10 logs+7 planks | 2x2 h4 | 1 | replants trees in ~21x20 range | sustain log supply
LumberMill | Lumber Mill | F | start | 15 logs | 3x2 h3 | 1 | 1 log -> 1 plank / 1.3h | needs 50hp power; buf 13 in/13 out
IndustrialLumberMill | Industrial Lumber Mill | IT | (v?) | (v?) | (v?) | (v?) | logs -> planks, higher throughput (v?) | IT plank source
GearWorkshop | Gear Workshop | both | 100 | 15 logs+25 planks | 3x2 h3 | 1 | 1 plank -> 1 gear / 3h | needs 120hp power; buf 10 in/10 out

## Tree log yield & grow time
tree | logs | grow_days | notes
Birch | 1 | 7 | fastest cycle, low yield — best for steady early logs
Pine | 2 | 12 | balanced
Mangrove | 2 | 10 | IT / water-adjacent
Chestnut | 4 | 23 | also yields chestnuts (food)
Maple | 6 | 28 | high log yield + maple syrup (F pastries); slow
Oak | 8 | 30 | highest log yield, slowest
- Fell time ~1.2h base (faster with well-being). RULE-of-thumb throughput per lumberjack ~ logs_per_tree / ~0.2day fell ≈ steady but tree regrowth is the real limiter — match Forester replant rate to cut rate.

## Material chains (end to end)
- tree -> [Lumberjack Flag] -> logs   (Birch 1 / Pine 2 / Maple 6 / Oak 8 per tree; ~1 tree/1.2h/worker)
- logs -> [Lumber Mill, 50hp] -> planks @ 1 log -> 1 plank / 1.3h (~0.77 plank/h/mill)
- planks -> [Gear Workshop, 120hp] -> gears @ 1 plank -> 1 gear / 3h (~0.33 gear/h/workshop)
- logs -> (direct) -> pumps, tanks, dams, housing, most early builds (logs = master resource)
- power: Power Wheel / Water Wheel / (later) Power Shaft feed hp to Lumber Mill(50), Gristmill(60), Bakery(transmit), Gear Workshop(120). Unpowered mill = 0 output.

## Food chains (raw -> edible)
- carrot / sunflower / kohlrabi / berries -> (eaten raw, no processing)
- potato -> [Grill +0.1 log] -> 4 grilled potatoes / 0.52h
- chestnut -> [Grill +0.1 log] -> 2 grilled chestnuts / 0.33h
- spadderdock -> [Grill +0.1 log] -> 3 grilled spadderdock / 0.4h
- wheat -> [Gristmill, 60hp] -> wheat flour (1:1 /0.5h) -> [Bakery +0.1 log] -> 5 bread / 1h
- cattail root -> [Gristmill] -> cattail flour (1:1 /0.25h) -> [Bakery +0.1 log] -> 4 cattail crackers / 0.5h
- wheat flour + maple syrup -> [Bakery +0.1 log] -> 3 maple pastries / 1.5h  (best F well-being +3)
- IT: corn/soy/cassava/eggplant -> [IT ration/processing bldg] -> rations/fermented food (rates v?)
- balance note: 1 Gristmill feeds ~2 bread-Bakeries or ~3 pastry-Bakeries (v?).

## What needs what (dependency list)
- logs: nothing (raw) — from trees. EVERYTHING downstream needs logs.
- planks: need logs + Lumber Mill + power.
- gears: need planks (-> logs) + Gear Workshop + power.
- power (hamsterpower): needed for Lumber Mill, Gristmill, Bakery, Gear Workshop, most refineries.
- Medium Tank / advanced storage: needs planks + gears.
- Bakery/Gristmill/Gear Workshop: cost planks+gears -> so you must have a plank & gear line BEFORE them.
- cooked food: needs raw crop + logs (fuel) + processing building + power.
- science: unlocks Forester(30), Gear Workshop(100), Aquatic FH(150), Bakery(160), Gristmill(180).

## RULEs
- RULE: NEVER let logs hit zero — logs gate pumps, tanks, housing, planks, and all builds. Keep a Lumberjack cutting every turn and a reserve buffer.
- RULE: Build a Forester BEFORE clear-cutting. Match replant rate to cut rate or the forest (and log supply) collapses. Prefer Birch (7-day) for fast renewal; add Oak/Maple only for bulk once Forester keeps up.
- RULE: Planks are the mid-game bottleneck. Stand up a Lumber Mill (+power) as soon as any build needs planks; expect ~0.77 plank/h/mill. Add mills before a big build spree, not during it.
- RULE: Gears are slow (3h each) — build the Gear Workshop early if you want Medium Tanks / advanced buildings, and never rely on a single workshop for a large order.
- RULE: No power = no planks/gears/cooked food. Verify a working power source (wheel/water wheel) is wired before staffing powered workshops.
- RULE: Order of standing up the economy: logs (Lumberjack+Forester) -> planks (Lumber Mill+power) -> gears (Gear Workshop) -> then powered food (Gristmill/Bakery) and Medium Tanks.
- RULE: Keep raw materials flowing to buffers via Log Pile / storage so mills/workshops aren't idle waiting on haulers.

sources:
- https://timberborn.wiki.gg/wiki/Lumberjack_Flag
- https://timberborn.wiki.gg/wiki/Forester
- https://timberborn.wiki.gg/wiki/Lumber_Mill
- https://timberborn.wiki.gg/wiki/Gear_Workshop
- https://timberborn.wiki.gg/wiki/Grill
- https://timberborn.wiki.gg/wiki/Gristmill
- https://timberborn.wiki.gg/wiki/Bakery
- https://timberborn.wiki.gg/wiki/Iron_Teeth
- https://timberborn.wiki.gg/wiki/Food
