# Progression (first ~10 cycles, cycle-by-cycle)
keys: progression, build order, first cycles, opening, tech order, science order, when to expand, population gating, wellbeing timing, survive first drought badtide
v: Timberborn 1.0.13.1 · (v?) = unverified vs game blueprints
notation: P=pop, D=next hazard length (days). water use 2.13·P/day, food 2.67·P/day, target stored (D+2)·2.13·P water & (D+2)·2.67·P food. A "cycle" = one temperate→hazard→temperate loop. Default start P≈6–8. Read the FORECAST each cycle; hazards lengthen over time.

## Guiding order of concerns (never invert)
Water > Food > Housing/Shelter > Logs loop > Science > Wellbeing > Expansion/pop growth.
- RULE: Never build a lower-priority thing while a higher one is unmet for the coming hazard. Thirst is the #1 killer (empties in ~4.3d).

## Cycle-by-cycle (Folktails, spec ids)
Cycle 1 — establish survival before FIRST drought (read D from forecast):
1. Path spine from DC along the clean shoreline + one inland branch (free, first).
2. LumberjackFlag + Forester near trees → logs (the master resource).
3. GathererFlag near wild bushes → instant berries while a farm grows.
4. WaterPump on land facing clean water (depth ≤2). Start filling.
5. SmallTank ×ceil((D+2)·2.13·P / 30) beside the pump. Fill to 100% before the drought.
6. Lodge ×ceil(P/3) clustered inland (covers Sleep+Shelter, enables breeding).
7. EfficientFarmhouse planting CARROTS (4-day, raw) on moist==1 soil; bank ≥1 harvest before drought.
8. SmallWarehouse central so output isn't buffer-capped.
Checkpoint: tanks 100%, ≥1 carrot harvest banked, beds≥P, logs loop running. Then coast into drought drinking tanks.

Cycle 2 — survive drought, add science + reservoir:
9. Inventor (12 log, 1 SP/h) ONLY after 1–8 secure. Start banking SP.
10. Dam across the narrowest river gap → reservoir as cheap secondary reserve (supplements tanks, discount for evaporation).
11. Expand SmallTanks / add a 2nd WaterPump if 1 can't refill tanks in the wet window.
12. Campfire (15 log, Social) once survival buffered — cheapest wellbeing, lifts work/move speed.
Checkpoint: survived drought with no death; SP accumulating; forecast next D.

Cycle 3 — prepare for BADTIDE + first tech spend:
13. Spend SP (~120) on Levee/MediumTank/Floodgate line.
14. Build badtide bypass: split flow upstream, Floodgate on colony intake (seal), Floodgate on bypass (open in badtide).
15. Convert/expand storage to MediumTank(300) once its tech is up — 1 Medium = 10 Small at less footprint.
16. Enter badtide with tanks 100%, intake sealed, bypass open, pumps halted. Drink tanks only.
Checkpoint: badtide passed clean, 0 contaminated beavers.

Cycles 4–6 — stabilize + wellbeing + pop growth:
17. Add a 2nd food type (Sunflower/Potato) for Nutrition wellbeing bonus once carrots cover survival.
18. Add Shrubs/Lanterns (passive Aesthetics, free) near housing; then Lido/Agora when affordable.
19. Grow pop deliberately: keep Shelter met (F breeds in Lodges); add Lodges AHEAD of pop so beds≥P always.
20. Observatory (1000 sci) for real science throughput once basics are automated.
Checkpoint: wellbeing positive, pop rising with beds+water+food scaling ahead of it.

Cycles 7–10 — expand + industrialize:
21. Power (Water Wheel / Power Wheel) → Gristmill+Bakery for bread (variety + density), or hold if food is fine.
22. Bigger reservoir / more MediumTanks sized to the now-longer forecast D.
23. Sluice (400 sci) to AUTOMATE badtide intake (close >5% contamination) so it's hands-free.
24. Consider a 2nd District Center if the base pushes past 70 path-tiles.
Checkpoint: each hazard entered with storage sized to the growing forecast D, not last cycle's.

## Population-growth gating
- RULE: Only let pop grow when beds≥P AND stored water/food already cover the NEXT hazard for the NEW P. Growth without scaled storage causes thirst deaths.
- RULE: F breeding needs Shelter met (housing) + spare housing capacity. Keep beds a step ahead of pop; pause growth (fill beds) if storage lags.

## When science / when wellbeing
- RULE: Build Inventor only after water+food+housing for the first drought are secured. 1 SP/h is slow — start it early-ish but never before survival.
- RULE: Spend first SP on water-engineering tech (Levee, MediumTank, Floodgate) — it hardens survival — before cosmetic/wellbeing tech.
- RULE: Build wellbeing (Campfire, then passive Shrubs) only once a survival buffer exists. Wellbeing multiplies work/move/growth/lifespan, so it pays off — but never at the cost of an unmet basic need.

sources:
- https://timberborn.wiki.gg/wiki/Needs
- https://timberborn.wiki.gg/wiki/Drought
- https://timberborn.wiki.gg/wiki/Badtide
- https://timberborn.wiki.gg/wiki/Science
- https://timberborn.wiki.gg/wiki/Wellbeing
- https://timberborn.wiki.gg/wiki/Water_Pump
- https://timberborn.wiki.gg/wiki/Small_Tank
- https://timberborn.wiki.gg/wiki/Efficient_Farmhouse
