# Water Mechanics & Physics
keys: water, spillover, evaporation, drought, badtide, contamination, badwater, flow, reservoir depth, isolation, floodgate physics, dam physics, decay
v: Timberborn 1.0.13.1 · (v?) = unverified vs game blueprints

## Flow & spillover
| behavior | value | note |
|---|---|---|
| Dam spillover | holds ~0.65 block, overflows above | water climbs above dam top; not a perfect seal |
| Levee | blocks 100%, stackable | true watertight wall; build reservoir sides/floor with these |
| Floodgate | holds ≤ set height (0–1, 0.05 step); spills above | manual valve; physical body 2 blocks tall |
| Water flows | downhill toward lower level; seeks equilibrium | current is stronger through narrow gaps (bottlenecks) |
| Fluid Dump | outputs "flowing water" below outlet, stops ~0.5m | moves water UP/across without a river connection |

## Drought
- Water + badwater SOURCES stop producing; no natural refill until temperate returns.
- Evaporation: any open fluid on the map slowly dries up over the drought — shallow water first. Exact rate not published (v?); deeper reservoirs lose a smaller FRACTION.
- Only water in TANKS or RESERVOIRS survives. Tanks: zero evaporation. Reservoirs: evaporate + shrink, so overbuild depth.
- RULE: Size a reservoir by DEPTH not area — a deep narrow basin loses less to evaporation than a wide shallow one holding the same volume.

## Badtide / contamination
| phase | contamination | effect |
|---|---|---|
| Badtide start | 50% → 100% over first 12h | ramps up |
| Badtide mid | 100% | full toxicity |
| Badtide last 12h | 100% → 50% | ramps down |
| Temperate returns | snaps to 0% instantly | clears |

- Contamination spreads through CONNECTED water — any body linked to the badwater source turns toxic; isolated (walled-off) bodies stay clean.
- At ≤50% water neither irrigates nor contaminates terrain; >50% contaminates ground (spreading range) and kills plants in ~0.2–0.3 days.
- Beavers touching badwater: >5% contamination → chance to become Unwell → Contaminated after 3 days (−70% move, refuses work, needs frozen, still eats/drinks/houses). NOT contagious. Bots immune. No natural recovery — needs Herbalist antidotes (F) or Decontamination Pod (IT).
- Pumps (Water/Deep Water) pump only clean water and lose efficiency as badwater % rises → cannot supply drinkable water during badtide.
- Tanks filled during temperate stay clean through badtide (sealed) — the ONLY safe drinking source mid-event.

## Keeping badwater OUT of drinking storage
- Isolation is physical: contamination only reaches water that is CONNECTED to the toxic body. Sever the connection (levee/closed floodgate) and stored water stays clean.
- A tank fed by a pump is NOT auto-safe: if the pump keeps running into a contaminated river, it feeds toxic water in. Stop/seal the intake before contamination climbs.

RULE: Enter every badtide with tanks already FULL of clean temperate water and the colony intake physically sealed (closed floodgate or levee). Drink from tanks only until temperate returns.
RULE: Isolate drinking storage from any water body connected to a badwater source. If a channel touches the river, it can go toxic — wall it off during badtide.
RULE: Never pump into drinking tanks while contamination >0%. Halt pumps or divert intake at the first badtide tick.
RULE: Divert badwater as far UPSTREAM / close to source as possible via a bypass channel; keep the colony reservoir on a separate, sealable branch.
RULE: Auto option (400+ sci): Sluice on colony intake set "close above 5% contamination"; bypass Sluice set to open. Below that tech, do it by hand each badtide.
RULE: Build reservoir walls/floor from LEVEES (100% seal), not dams (dams leak above 0.65 and overflow). Use floodgates only where you need to open/close.
RULE: Make the reservoir deep enough that after (D days of evaporation) + the colony's (D+2)*2.13*P draw, water is still ≥1 block over the pump intake.

sources:
- https://timberborn.wiki.gg/wiki/Drought
- https://timberborn.wiki.gg/wiki/Badtide
- https://timberborn.wiki.gg/wiki/Contamination
- https://timberborn.wiki.gg/wiki/Badwater
- https://timberborn.wiki.gg/wiki/Water
- https://timberborn.wiki.gg/wiki/Dam
- https://timberborn.wiki.gg/wiki/Floodgate
- https://timberborn.wiki.gg/wiki/Levee
