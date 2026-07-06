# Housing, Science & Wellbeing Buildings
keys: housing lodge barrack rowhouse breeding pod district center path stairs platform science inventor observatory numbercruncher campfire lido decoration temple grove wellbeing capacity
v: Timberborn 1.0.13.1 · (v?) = unverified vs game blueprints

## District & connectivity
spec | name | fac | sci | cost | size | workers | rate | use
DistrictCenter | District Center | both | start | placed | 3x3 h5 | 4 | — | colony hub; holds start goods; employs 4 builders/haulers. Path range green ≤70 tiles then red (slow).
Path | Path | both | start | free | 1x1 | 0 | instant | connects buildings; built instantly, no builder. Off-path = unreachable.
Stairs | Stairs | both | start | 1 log (v?) | 1x1 | 0 | — | vertical movement; counts as 1 tile in range.
Platform | Platform | both | start | 2 logs (v?) | 1x1 h_var | 0 | — | solid raised deck; build paths on top separately.

## Housing (capacity = beds)
spec | name | fac | sci | cost | size | workers | rate | use
Lodge | Lodge | F | start | 12 logs | 2x2 h1 | 0 | cap 3 | base house; F breeds inside (2 adults, no critical need).
MiniLodge | Mini Lodge | F | 50 | 5 logs | 2x1 | 0 | cap 1 | fill odd corners.
DoubleLodge | Double Lodge | F | 150 | 20 logs | 2x2 | 0 | cap 6 | dense; can sit on 1-deep water.
TripleLodge | Triple Lodge | F | 250 | 35 logs | 2x3 | 0 | cap 9 | densest F house.
Barrack | Barrack | IT | start | 40 logs | 3x2 | 0 | cap 10 | IT base house, huge cap; no breeding here.
LargeBarrack | Large Barrack | IT | 400 | 70 logs | 3x3 | 0 | cap 16 | IT bulk housing.
Rowhouse | Rowhouse | IT | 180 | 20 logs | 2x1 | 0 | cap 5 | compact; helps passive Aesthetics.
LargeRowhouse | Large Rowhouse | IT | 600 | 35 logs | 2x1 | 0 | cap 8 | dense compact.
BreedingPod | Breeding Pod | IT | start | 10 logs | 1x1 | 0 | breeds | IT reproduction; pop scales w/ #running pods × avg wellbeing.
AdvBreedingPod | Advanced Breeding Pod | IT | 1000 | 5 treated plank+2 metal blk | 1x1 | 0 | breeds | faster IT breeding.

## Science (Science Points = SP)
spec | name | fac | sci | cost | size | workers | rate | use
Inventor | Inventor | both | start | 12 logs | 2x2 h3 | 1 | 1 SP/h | only start-game science; low output, build early.
Observatory | Observatory | F | 1000 | (v?) | (v?) | 4 | 13.3 SP/h (3.3/worker) | mid-game F science, best per-worker.
Numbercruncher | Numbercruncher | IT | 1500 | (v?) | (v?) | 0 | 10 SP/h | automated IT science, no workers.

## Wellbeing buildings (need satisfied · +bonus/-decay)
spec | name | fac | sci | cost | size | workers | rate | use
Campfire | Campfire | both | start | 15 logs | 3x3 h1 | 0 | cap 5 | Social +1 / -20. Cheapest social; build first.
Lido | Lido | F | 250 | 40 logs+30 plank | 4x3 h2 | 0 | cap 7 | Fun +? & Wet Fur; needs ≥0.2m water beside.
Shrub | Shrub/Lantern/Brazier | both | start(v?) | few logs (v?) | 1x1 | 0 | — | Aesthetics +1–2 / -40; cheap radius decor.
BeaverBust | Beaver Bust/Statue | both | (v?) | logs+plank (v?) | 1x1 | 0 | — | Aesthetics +1–2, stronger decoration.
Agora | Agora | both(v?) | (v?) | (v?) | large | 0 | — | Social +3 / -10; big social upgrade.
DanceHall | Dance Hall | both(v?) | (v?) | (v?) | large | 0 | — | Social +5 / -10; best social.
Carousel | Carousel/Mud Pit | both(v?) | (v?) | (v?) | med | 0 | — | Fun +1–3 / -10..-20.
Monument | Monument (basic/adv) | both | (v?) | plank+metal (v?) | large | 0 | — | Awe +3 (basic) / +5 (adv) / -30.
Wonder | Wonder | both | (v?) | huge | huge | 0 | — | Awe +10 / -1 (near-permanent).

RULE: only start-game housing = Lodge (F) / Barrack (IT); everything denser needs SP.
RULE: build Inventor early but never before water+food+housing secured; 1 SP/h is slow.
RULE: cheapest wellbeing = Campfire (social) then Shrubs/Lanterns (aesthetics); add Lido/Agora later.
RULE: Aesthetics & Awe are passive (proximity, no worker/visit) — place decor near housing for free wellbeing.

sources:
- https://timberborn.wiki.gg/wiki/Housing
- https://timberborn.wiki.gg/wiki/Science
- https://timberborn.wiki.gg/wiki/Inventor
- https://timberborn.wiki.gg/wiki/District_Center
- https://timberborn.wiki.gg/wiki/Path
- https://timberborn.wiki.gg/wiki/Campfire
- https://timberborn.wiki.gg/wiki/Lido
- https://timberborn.wiki.gg/wiki/Needs
- https://timberborn.wiki.gg/wiki/Wellbeing
