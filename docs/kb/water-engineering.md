# Water Engineering (clean vs badwater, forecast-driven)
keys: water engineering, clean water, badwater, contamination, badtide, drought, forecast, tanks, reservoir, dam, levee, floodgate, isolate, seal intake, fill before drought, pump fails badtide
v: Timberborn 1.0.13.1 · (v?) = unverified vs game blueprints
notation: P=population, D=next drought/badtide length (days). Daily water use ≈2.13·P. Target stored ≈(D+2)·2.13·P.

## The two water states (from /map)
- CLEAN water: water_depth>0 AND contamination==0 → pumpable, drinkable, irrigates soil (moist spreads).
- BADWATER: contamination>0 → pumps draw it at falling efficiency and it is NOT drinkable; >~50% contamination poisons ground and kills crops; touching it can contaminate beavers.
- RULE: Pumps (WaterPump / DeepWaterPump) draw ONLY clean water; as contamination rises during a badtide their output falls toward zero. A pump is not a badtide water source.

## Forecast-driven actions (read the weather forecast each cycle)
| forecast says | action |
|---|---|
| Temperate now, DROUGHT in N days | Top ALL tanks to 100% with clean water before it hits. Bank (D+2)·2.13·P in TANKS. Ensure reservoir deep enough that intake stays ≥1-deep after evaporation+draw. |
| Temperate now, BADTIDE in N days | Top tanks to 100% clean NOW. Prepare to SEAL colony intake (close Floodgate / drop Levee) and OPEN the bypass at the first contaminated tick. |
| DROUGHT active | Sources dead; drink from tanks+reservoir only. Do not expand water use. Pumps only help if reservoir still clean+deep. |
| BADTIDE active | Intake SEALED, bypass OPEN, pumps into drinking tanks HALTED. Drink tanks only (sealed = still clean). Keep beavers off badwater tiles. |
| Temperate returns | contamination snaps to 0. Re-open intake, resume pumping, refill tanks/reservoir for the NEXT hazard. |

## Storage that survives hazards
- TANKS (SmallTank 30, MediumTank 300): zero evaporation, sealed from contamination. The ONLY guaranteed drinking reserve through badtide. This is the survival insurance — always fill first.
- RESERVOIR (dammed river): large + cheap but EVAPORATES each drought day and goes toxic in badtide if connected to a badwater source. A supplement, never the sole drinking safety.
- RULE: Fill tanks with clean water BEFORE every drought/badtide. Enter every hazard with tanks at 100%.
- RULE: Size storage to the FORECAST D, not today's D — droughts lengthen across a run. Recompute (D+2)·2.13·P every cycle and EXPAND tanks; never just maintain.

## Building a drought reservoir (Dam + Levee + Floodgate)
1. Find the narrowest river cross-section (fewest tiles to span) from /map.
2. Wall it: Dam across the channel backs water UPSTREAM into a reservoir. Overflows above ~0.65 block (leaky).
3. Use LEVEE for reservoir walls/floor that must NOT leak (100% seal); use Dam only for the overflow lip.
4. Put one Floodgate in the wall as a drain/flush control.
5. Put the pump intake over the DEEPEST reservoir tile so it stays submerged after drawdown.
- RULE: Build reservoir seals from LEVEES (100%), not Dams (leak >0.65). Floodgates only where you need to open/close.
- RULE: Make the reservoir deep, not wide — a deep narrow basin loses a smaller FRACTION to evaporation than a shallow wide one of equal volume, and keeps the pump intake submerged longer.

## Keeping badwater OUT of drinking storage
- Contamination spreads ONLY through CONNECTED water. A body severed from the badwater source (by Levee or closed Floodgate) stays clean.
- RULE: Split flow upstream: channel A = colony intake→reservoir/tanks (sealable), channel B = bypass "garbage chute" past the colony. Badtide: SLAM A shut, OPEN B so contaminated water runs away from drinking storage.
- RULE: A tank fed by a running pump is NOT auto-safe — if the pump keeps drawing a contaminating river, it pumps badwater in. Halt pumps / seal intake at contamination>0.
- RULE: Isolate drinking storage physically before badtide. Any channel that touches a badwater-connected body can go toxic — wall it off.
- RULE (auto, 400 sci): put a Sluice on intake A set "close above 5% contamination" and a Sluice on bypass B set to open. Below that tech, switch Floodgates by hand each badtide.

sources:
- https://timberborn.wiki.gg/wiki/Drought
- https://timberborn.wiki.gg/wiki/Badtide
- https://timberborn.wiki.gg/wiki/Contamination
- https://timberborn.wiki.gg/wiki/Badwater
- https://timberborn.wiki.gg/wiki/Water_Pump
- https://timberborn.wiki.gg/wiki/Dam
- https://timberborn.wiki.gg/wiki/Levee
- https://timberborn.wiki.gg/wiki/Floodgate
- https://timberborn.wiki.gg/wiki/Sluice
- https://timberborn.wiki.gg/wiki/Small_Tank
