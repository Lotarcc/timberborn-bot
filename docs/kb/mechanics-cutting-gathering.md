# Cutting, Gathering & Planting — how work actually gets assigned
keys: lumberjack cutting designation trees logs gatherer berries forester planting how to get logs food
v: Timberborn 1.0.13.1 (verified by decompile + live test)

These are the ACTUAL rules (confirmed in-engine), not the intuitive ones. A worker
being assigned to a flag is NOT enough — most production needs a designation or a
correctly-ranged flag.

## Trees / Logs (CUTTING) — needs designation
RULE: A Lumberjack does NOT auto-cut trees near its flag. Trees must be DESIGNATED
for cutting. Cutting is GLOBAL: any staffed Lumberjack fells any REACHABLE
designated tree, wherever it is. The LumberjackFlag is only a workplace (job slots +
log output), not a cutting radius.
RULE: To get logs: (1) place a LumberjackFlag on clear reachable land (bridge
auto-connects it), (2) call action `designate_cutting` with the mature-tree tiles
(or {all:true} to mark every mature tree), (3) advance time. Logs then rise.
RULE: Read tree positions from /resources (`trees:[{x,y,z,species,good,mature}]`).
Only `mature:true` trees yield logs. Designate mature trees.
RULE: Cutting DEPLETES the forest. Without replanting (Forester) logs run out. Plan
a Forester once the bootstrap is stable.
RULE: `undesignate_cutting` cancels. Designation persists on a tile even after the
tree is felled (a regrown tree there gets cut again).

## Berry bushes / Food (GATHERING) — automatic, range-based
RULE: Gathering needs NO per-plant designation. A GathererFlag automatically
harvests READY gatherables within its terrain RANGE. Place the flag so its range
covers a cluster of ready bushes (see /resources `gatherables:[{x,y,z,ready}]`).
RULE: A gatherer may need the target SPECIES enabled/prioritized on the flag (check
the gather action if berries don't rise after placing a flag on ready bushes).
RULE: Wild bushes have limited/again-growing yield; a few gatherers on a big bush
cluster feed the early colony, but plan a Farmhouse (carrots) for reliable food
before the population grows. Berries alone will decline if consumption > gather rate.

## Trees replanting (FORESTER / PLANTING)
RULE: A Forester replants trees so cutting is sustainable. Like cutting, planting
may be area/species-based (a planting designation or a ranged flag) — set the
planting area/species if trees don't appear. Foresters cost planks/science (not a
day-1 build); run wild trees first, add a Forester by cycle 2.

## The bootstrap consequence
RULE: The very first productive act is: LumberjackFlag (free) + designate_cutting
{all:true}, then advance time to bank logs. Water (pump, 12 logs) and everything
else waits on that first log income. Do NOT expect logs from a flag alone.
