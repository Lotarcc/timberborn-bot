# Water Storage Design Patterns
keys: water design, reservoir, dam pattern, bottleneck, tank farm, badtide bypass, drought storage, design library, water blueprint
v: Timberborn 1.0.13.1 · (v?) = unverified vs game blueprints
notation: P=population, D=next drought/badtide length (days). Daily water use ≈ 2.13*P. Target stored = (D+2)*2.13*P.

## dam-and-reservoir behind a bottleneck
goal: bank a large temperate-water reserve that persists into drought, cheap early.
when: early game, river has a natural narrow point or canyon; before first long drought; low science.
build:
1. Find the narrowest cross-section of the river = the anchor line.
2. Wall the whole cross-section with Dams (start-tech, 20 log ea) — fewer tiles because it's narrow. Upgrade sides to Levees (120 sci) once available for a true seal.
3. Water backs up UPSTREAM into a reservoir; the narrower the gap, the fewer blocks to span.
4. Put a pump (Water/Deep Water) on the reservoir side, intake block over the DEEPEST part.
5. Optional: one Floodgate in the wall = drain/flush control.
cost: ~20 log * (channel width in tiles); e.g. 3-wide gap ≈ 60 log. +1 pump (12 log).
expected: reservoir volume ≈ width * upstream_length * depth (blocks) of water units; a modest 3x8x2 basin ~ enough raw buffer for ~10–15 beavers through a short drought, but DISCOUNT for evaporation — pair with tanks.
risk: dams overflow above 0.65 block (leaky); evaporation shrinks it each drought day; goes toxic in badtide if connected to source. Not standalone drinking safety — feed tanks.

## tank farm fed by pumps
goal: evaporation-proof, contamination-proof guaranteed drinking reserve = the real survival insurance.
when: always, before every dry season; the number you can't skip.
build:
1. Anchor = flat ground next to the district center / near the pump, path-connected.
2. Compute need = ceil((D+2)*2.13*P). Tanks: Small=30, Medium=300.
3. Place ceil(need/30) Small Tanks (start) OR ceil(need/300) Medium Tanks (120 sci) in a grid, all path-adjacent.
4. Run pump(s) from river/reservoir; ensure fill-rate tops all tanks during temperate before the hazard.
5. Keep tanks TOPPED at 100% entering every hazard.
cost: Small path: ceil(need/30)*15 log. Medium path: ceil(need/300)*(30 plank+20 gear). +pumps 12 log ea.
expected: holds exactly (D+2) days of water for P beavers with a 2-day buffer; loses ZERO to evaporation/badtide. Ex: P=10, D=4 → need=128 → 5 Small (150 log) or 1 Medium.
risk: under-sizing vs the FORECAST D (recompute each cycle, D grows); pump too slow to refill between hazards; tanks not path-connected → beavers can't drink.

## badtide bypass channel
goal: keep contaminated badwater out of the drinking supply automatically/manually.
when: map has a badwater source or badtide is enabled; before first badtide.
build:
1. Anchor = the fork point upstream of the colony where you split flow.
2. Dig/route TWO channels from the fork: (A) colony intake → reservoir/tanks, (B) bypass "garbage chute" running past the colony downstream.
3. At A's mouth put a Floodgate or Levee (the seal); at B's mouth a Floodgate.
4. Temperate: A open (clean water fills reservoir/tanks), B closed or partial.
5. Badtide: SLAM A shut, OPEN B — contaminated water runs down the bypass, away from drinking storage.
6. Automate (400 sci): Sluice on A "close above 5% contamination"; Sluice on B opens — hands-free.
cost: ~2 Floodgates (20 log+10 plank) or Levees; +Sluices (5 log+5 metalblock ea) if automating; + channel walls (Levees).
expected: drinking reservoir/tanks stay 0% contaminated through badtide; beavers drink clean tank water the whole event; zero contaminated-crop/beaver incidents.
risk: forgetting to switch gates (use Sluice automation); A not fully sealed (use Levee not Dam — dam leaks over 0.65); bypass not actually disconnected from the drinking body → contamination bleeds back.

RULE: Layer these — bypass channel protects a reservoir (bottleneck dam) that pumps into a tank farm. Tanks are the last line; never rely on the reservoir alone.
RULE: Every cycle, re-read forecast D and re-run the tank-farm math; D trends up across a run, so expand storage, don't just maintain.

sources:
- https://timberborn.wiki.gg/wiki/Dam
- https://timberborn.wiki.gg/wiki/Levee
- https://timberborn.wiki.gg/wiki/Floodgate
- https://timberborn.wiki.gg/wiki/Sluice
- https://timberborn.wiki.gg/wiki/Small_Tank
- https://timberborn.wiki.gg/wiki/Medium_Tank
- https://timberborn.wiki.gg/wiki/Drought
- https://timberborn.wiki.gg/wiki/Badtide
