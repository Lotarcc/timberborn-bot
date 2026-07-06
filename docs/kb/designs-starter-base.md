# Design: Starter Base (survive first drought)
keys: starter base design build order first drought survival layout farm block reusable template spacing checkpoints scales anchor
v: Timberborn 1.0.13.1 · (v?) = unverified vs game blueprints

goal: from spawn, survive the FIRST drought of length D with population P (default P≈8, D≈3 on Normal) without a thirst/hunger death.
assumes: Folktails, flat-ish riverbank, District Center (DistrictCenter) pre-placed = ANCHOR. River depth ≤2 next to anchor. Water use 2.13/beaver/day, food 2.67/beaver/day.

## build order (anchor-relative, spec ids)
1. Path — lay a spine from DistrictCenter along the bank; branch to every future building. Free, instant. Nothing works off-path.
2. LumberjackFlag + Forester near trees → keep logs flowing (master resource).
3. WaterPump (F) on river edge ≤2 deep, adjacent to path. 12 logs. Start filling.
4. SmallTank ×ceil((D+2)·2.13·P / 30) beside the pump. 15 logs each, cap 30, no evaporation. This is drought insurance.
5. Lodge ×ceil(P/3) next to path. 12 logs, cap 3. Covers Sleep+Shelter (+enables breeding).
6. GathererFlag — immediate wild berries while farm grows. Free, 1 worker.
7. Efficient Farmhouse (farm-block below) planting CARROTS (4-day cycle) — bank ≥1 harvest before drought.
8. LogPile + food storage so output isn't buffer-capped.
9. Inventor — 12 logs, 1 SP/h. ONLY after 1–8 done. Unlocks Medium Tank (300)/floodgates/levees.
10. Dam ×N across river (20 logs, spills at 0.65) → reservoir as cheap secondary reserve, if logs+time remain.
11. Campfire (15 logs, Social +1) once survival has buffer — cheapest wellbeing.

## spacing / notes
- Keep everything inside the green district range (≤70 path tiles from anchor) or workers walk too far.
- Tanks adjacent to pump = short haul; pump output → tank → beavers.
- Stack Lodges near Campfire + a couple Shrubs (passive Aesthetics) for free wellbeing later.
- Enter drought with tanks FULL and a carrot harvest banked. Farms don't grow in unirrigated drought.
- Badtide variant: physically seal colony intake (levee/closed floodgate), drink tanks only.

## checkpoints (by end of first wet season)
- WATER: stored_water ≥ (D+2)·2.13·P in TANKS (not just river). e.g. P=8,D=3 → ≥85 water → 3 Small Tanks (90).
- FOOD: stored_food ≥ (D+2)·2.67·P. e.g. P=8,D=3 → ≥107 food → berries + 1 carrot harvest.
- HOUSING: beds ≥ P (ceil(P/3) Lodges).
- LOGS: >0 buffer, Lumberjack+Forester loop running.

## scales (parametric in P, D)
- tanks_small = ceil((D+2)·2.13·P / 30); or 1 MediumTank(300) per ~140 water once SP≥ unlock.
- pumps = ceil(colony_water_use / pump_output); add a 2nd WaterPump if 1 can't refill tanks in the wet season.
- lodges = ceil(P/3) (F). IT: barracks = ceil(P/10).
- re-run checkpoints every cycle with the NEW forecast D (droughts lengthen); expand tanks each cycle, never just maintain.

## farm-block (reusable pattern)
- 1× Efficient Farmhouse (25 logs, 3 farmers) + a rectangle of CARROT fields it can reach.
- size fields so daily yield ≥ 2.67·P: carrots ~3 units/plant over 4-day growth. Plant enough plots that a full harvest ≥ (D+2)·2.67·P to bank a drought's food.
- place fields on IRRIGATED ground (near water) so they keep growing longer; adjacent to the path spine.
- swap CARROT→Sunflower/Potato only after carrot base covers survival; wheat is late-game (needs Gristmill+Bakery power).
- IT variant: replace Farmhouse+fields with Hydroponic Gardens (no irrigation, stackable) + food-processing.

sources:
- https://timberborn.wiki.gg/wiki/Needs
- https://timberborn.wiki.gg/wiki/Housing
- https://timberborn.wiki.gg/wiki/Water_Pump
- https://timberborn.wiki.gg/wiki/Small_Tank
- https://timberborn.wiki.gg/wiki/Efficient_Farmhouse
- https://timberborn.wiki.gg/wiki/District_Center
- https://timberborn.wiki.gg/wiki/Path
