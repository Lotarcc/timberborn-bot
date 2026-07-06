# Needs & Wellbeing Model
keys: needs thirst hunger sleep shelter wellbeing wet fur social aesthetics awe fun nutrition injury contamination decay work speed lifespan threshold priority
v: Timberborn 1.0.13.1 · (v?) = unverified vs game blueprints

## Basic needs (lethal / survival) — bar range, decay/day
need | use/day | range | decay/day | warn (bar<0) | unmet effect | death grace
Thirst | 2.13 water | -300..100 | 70 | "Thirsty" | move -25% | 5.71d from full / 4.29d if already thirsty (fastest killer)
Hunger | 2.67 food | -300..100 | 80 | "Starving" | work -50%, growth -40% | 5.0d from full / 3.75d if starving
Sleep | 3–10h | -20..80 | 60 | "Exhausted" | move -10%, sleep-restore -33%, unemployable→seeks shelter/DC | not directly lethal
Shelter | housing | -20..80 | 30 | — | sleep-restore -25%; F breeding disabled | not lethal

- Sleep restore: +15/h unsheltered, +20/h sheltered. Rested beaver can work ~32h straight.
- Shelter restore: +30/h in housing. Each satisfied basic need = +1 wellbeing point.

## Wellbeing needs (non-lethal, boost stats) — +bonus / -decay per building
need | how satisfied | example +bonus / -decay
Social | visit multi-visitor bldg | Campfire +1/-20 · Agora +3/-10 · Dance Hall +5/-10
Aesthetics | proximity to decoration (passive) | Shrub/Lantern/Bust +1–2 / -40
Awe | proximity to monument (passive) | basic +3 · advanced +5 · Wonder +10 / -30 (Wonder -1)
Fun | attractions/books | Lido/Carousel/Mud Pit +1–3 / -10..-20 · Detailer +1/-5
Wet Fur | dip in water +50/h · Shower +100/h | +1 wellbeing; no critical effect (Lido gives it)
Nutrition | eating varied foods | +1 wellbeing per satisfied (see food KB)

## Ailments (subtract wellbeing)
ailment | fac | effect | cure | WB penalty
Injury | both | refuses work | ~10d at medical bed | -2
Chipped Teeth | both | tree-cut chance -75% | Teeth Grindstone (2h→0) | -1
Bee Sting | F | minor | time 1.33d | -1
Contamination | F(v?) | move -70%, refuses work, non-basic needs locked 0 | Antidote 8×, -25/d, ~4d | -10
Wet Fur high | both | becomes priority activity | dip/shower | (need, not ailment)

## Wellbeing → stat bonuses (tiered, ever-increasing)
- Score sums points from all satisfied needs. Higher score = more bonus. Applies: Work Speed (adults), Growth Speed (kits), Move Speed (all), Life Expectancy.
- Example scaling: score 72+ ≈ move +70%, lifespan +75%; work speed up to +260% at max wellbeing (v?).
- IT breeding rate scales with average wellbeing → wellbeing is IT population lever, not just productivity.

RULE: satisfy order Thirst > Hunger > Sleep/Shelter FIRST; never let a wellbeing building steal labor from an unmet basic need.
RULE: Thirst is the emergency — empty thirst kills in ~4.3d and empties any time no water is reachable. Water outage in drought = #1 death cause.
RULE: build wellbeing only AFTER survival buffer exists (stored water+food cover next hazard D for pop P).
RULE: buy cheapest wellbeing first — Campfire (social) + Shrubs/Lanterns (passive aesthetics). Passive Aesthetics/Awe need no worker or visit: free stat gain.
RULE: keep Shelter met — losing it disables Folktails breeding and hurts sleep.
RULE: contamination (F) is catastrophic (-10 WB, -70% move, needs locked): keep beavers off badwater entirely.

sources:
- https://timberborn.wiki.gg/wiki/Needs
- https://timberborn.wiki.gg/wiki/Wellbeing
