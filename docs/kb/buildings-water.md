# Water & Hydro Buildings
keys: water, pump, deep water pump, mechanical pump, tank, small tank, medium tank, large tank, fluid dump, water dump, dam, levee, floodgate, sluice, reservoir, storage, capacity
v: Timberborn 1.0.13.1 · (v?) = unverified vs game blueprints

spec | name | fac | sci | cost | size | workers | rate | use
WaterPump | Water Pump | F | — | 12 log | 2x3 h2 | 1 | ~0.33–1 water/cycle (v?); 15 internal buffer | pumps clean water, depth ≤2, unpowered
DeepWaterPump | Deep Water Pump | IT | — | 12 log | 2x3 h2 | 1 | ~0.33–1 water/cycle (v?) | clean water, depth ≤6, unpowered; far block must sit over water
MechanicalPump | Mechanical Pump | F | 2500 | 50 gear+25 treatedplank+25 metalblock | 3x3 h3 | 0 | ~0.25 m³/s (v?), needs 700 hp | high-volume powered pump; lower part = 3-block levee
SmallTank | Small Tank | both | — | 15 log | 1x1 h2 | 0 | cap 30 | cheap drought insurance; no evaporation
MediumTank | Medium Tank | both | 120 | 30 plank+20 gear | 2x2 h3 | 0 | cap 300 | bulk reserve; supersedes obsolete "Large Water Tank"
FluidDump | Fluid Dump | both | 250 | 10 log+10 plank | 2x1 h2 | 1 | 3 water→flowing; 15 buffer; stops at 0.5m below outlet | discharge stored water as flow; irrigate/refill lower terrain
Dam | Dam | both | — | 20 log | 1x1 h1 | 0 | spillover ~0.65 block | raise river → form reservoir; overflows at top
Levee | Levee | both | 120 | 12 log | 1x1 h1 | 0 | blocks 100% | stackable waterproof wall; buildable-on
Floodgate | Floodgate | both | 150 | 10 log+5 plank | 1x1 h2 | 0 | height 0–1, 0.05 steps | adjustable gate; ≤set-height held, >set-height spills
Sluice | Sluice | both | 400 | 5 log+5 metalblock | 1x1 h1 | 0 | auto by depth/contamination | auto-close on contamination >5% or downstream depth; badtide automation

Notes:
- Water Pump = F only; Deep Water Pump = IT only. Both pump ONLY clean water; efficiency drops as badwater % rises → useless mid-badtide.
- Only tank & reservoir water survives drought/badtide; tanks alone are evaporation-proof AND contamination-proof.
- Double/Triple Floodgate variants exist for wider channels (span, not height) (v?).

RULE: Place a pump so its water-facing block sits over a spot ≥1 block deep at drought's END, not just now — river drops as it drains. Deep Water Pump (depth 6) survives far deeper drawdown than Water Pump (depth 2).
RULE: F early game = Water Pump; IT early game = Deep Water Pump. Never assume the other faction's pump is available.
RULE: Pump into TANKS, not just a reservoir. Prioritize enough tank capacity for (D+2)*2.13*P water BEFORE adding pumps beyond fill-rate need.
RULE: Fill Small Tanks first (start-tech, 15 log ea, 30 cap). Switch to Medium Tanks (300 cap) once 120 sci unlocked — 1 Medium = 10 Small at ~⅔ the log-equivalent footprint.
RULE: Mechanical Pump only worth it late (2500 sci + 700 hp power). Its base doubles as a 3-block levee — use it as part of a dam wall.
RULE: Sluice (400 sci) automates badtide: set colony intake to close above 5% contamination; needs metal blocks so it's a mid/late unlock — do it manually with floodgates/levees before then.
RULE: Fluid Dump lifts stored water to higher/dry ground; use to irrigate crop plots or top a raised reservoir. It stops at ~0.5m depth below its outlet, so it won't overflow.

sources:
- https://timberborn.wiki.gg/wiki/Water_Pump
- https://timberborn.wiki.gg/wiki/Deep_Water_Pump
- https://timberborn.wiki.gg/wiki/Mechanical_Water_Pump
- https://timberborn.wiki.gg/wiki/Small_Tank
- https://timberborn.wiki.gg/wiki/Medium_Tank
- https://timberborn.wiki.gg/wiki/Fluid_Dump
- https://timberborn.wiki.gg/wiki/Dam
- https://timberborn.wiki.gg/wiki/Levee
- https://timberborn.wiki.gg/wiki/Floodgate
- https://timberborn.wiki.gg/wiki/Sluice
