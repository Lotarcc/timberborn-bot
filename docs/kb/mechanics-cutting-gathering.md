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

## Berry bushes / Food (GATHERING) — automatic in range, needs a WORKER
RULE: Gathering needs NO designation and NO species selection — a GathererFlag
auto-harvests ANY ready gatherable in range. But TWO things must hold or nothing
is gathered: (1) the flag must have an ASSIGNED WORKER (it is a workplace), and
(2) bushes must be within WALKABLE range: navmesh path distance <=20 from the
flag's access tile (NOT a straight-line radius — a bush across water/off-path is
out of range even if close). Place the flag central to a reachable bush cluster.
RULE: Bushes REGROW their yield after harvest (not one-shot), so gathering is
self-sustaining as long as bushes stay alive and aren't over-eaten. If berries
still fall: check the flag is staffed and bushes are walkably within 20.
RULE: A few gatherers on a big cluster feed the early colony; add a Farmhouse
(carrots) for reliable food before population grows.

## Trees replanting (FORESTER / PLANTING) — needs designate_planting
RULE: A Forester does NOT auto-plant. You must DESIGNATE planting spots, exactly
like cutting: action `designate_planting` with tiles + species (template name like
"Pine"/"Birch"). A tile is plantable only if MARKED + soil is MOIST (see /map
moist[]) + not contaminated + empty + walkably in the forester's range (<=20).
RULE: Sustainable wood loop = Lumberjack cuts designated trees (depletes forest) +
Forester replants designated moist tiles. Add the Forester by cycle 2 (it costs
planks/science); run wild trees first.

## The bootstrap consequence
RULE: The very first productive act is: LumberjackFlag (free) + designate_cutting
{all:true}, then advance time to bank logs. Water (pump, 12 logs) and everything
else waits on that first log income. Do NOT expect logs from a flag alone.
