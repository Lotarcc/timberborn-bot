# Bootstrap Economy — the WOOD problem & day-1 arithmetic
keys: bootstrap, wood problem, zero logs, first builds, build order day 1, lumberjack flag, free buildings, log budget, first drought food, berries, carrots timing, starting resources
v: Timberborn 1.0.13.1 · (v?) = unverified vs game blueprints

## Starting state (Folktails, Normal)
| Item | Value |
|---|---|
| Beavers | 12–13 (8–9 adults + 4 children) |
| Food | 130 (≈4 days at 2.67/beaver/day for P=12) |
| Water | 0 stored (v?) — pump is life-or-death on day 1 |
| Logs | 0 — **this is the WOOD problem** |

Everything except flags/paths costs logs. With 0 logs, any log-costing building queued first just sits at 0% forever. The ONLY log source is cutting existing wild trees via a LumberjackFlag.

## Free buildings (cost 0 logs, no science)
Path · District Center · **LumberjackFlag** (1x1, 1 worker) · **GathererFlag** (1x1, 1 worker). The "mark trees for cutting" tool is also free. Everything else costs logs.

- RULE: A fresh colony's FIRST or SECOND action is always LumberjackFlag + mark EVERY mature wild tree in its radius (~20 tiles). Never queue a log-costing building before a LumberjackFlag exists and trees are marked.
- RULE: Never queue more log-cost than (logs on hand + ~1 day of cutting income). Check /stock before each build.

## Log income math
Cutting takes 1.2h/tree; 16h workday ⇒ ≤13 trees/day/lumberjack, ~8–10 realistic with walking. Wild yield: birch=1, pine=2, chestnut=4, maple=6, oak=8 logs/tree. Mixed wild forest ⇒ **~10–25 logs/day per lumberjack**. Plan with 12/day conservative.

| Pop | LumberjackFlags | Why |
|---|---|---|
| ≤10 | 2 | ~24 logs/day funds pump+lodges+farm queue |
| 11–20 | 2–3 | keep ~25–35 logs/day while building out |
| 20+ | 3–4 + Forester loop | cap at 1 forester : 4 lumberjacks |

- RULE: Run 2 LumberjackFlags from day 1 (2 of ~8 adult workers). Add a 3rd only if construction queue is starved AND marked mature trees remain.

## Forester & replanting
Forester costs 10 logs + 7 planks + 30 science (v?) — it is NOT a day-1 build (needs planks ⇒ Power Wheel + Lumber Mill, and Inventor SP). Cycle 1 runs on wild trees only.
- RULE: Unlock+build Forester in cycle 2–3, before wild trees within 70 path-tiles run out. Plant **birch first** (7-day grow, fastest restock), switch/mix to **pine** (12d, 2 logs, resin later) and **oak** (30d, 8 logs, best logs/day/tile 0.27) once one birch rotation is banked.

## First builds IN ORDER with log ledger (income ~12–24/day)
| # | Build | Cost | Cum. logs | Day | Why |
|---|---|---|---|---|---|
| 1 | LumberjackFlag ×2 + mark trees | 0 | 0 | 0 | only log source |
| 2 | Path spine (shore + tree line) | 0 | 0 | 0 | nothing works unpathed |
| 3 | GathererFlag ×2 at berry bushes | 0 | 0 | 0 | food income before farms |
| 4 | WaterPump at clean shore | 12 | 12 | 1 | beavers die of thirst in ~4.3d; pump stores 15 drinkable |
| 5 | SmallWarehouse (berries) | 3 | 15 | 1–2 | uncap gatherer output |
| 6 | Lodge ×2 (3 beds each) | 24 | 39 | 2–3 | sleep+shelter for adults first; rest later |
| 7 | EfficientFarmhouse + carrot field | 25 | 64 | 3–4 | must plant carrots by ~day 4–5 |
| 8 | SmallTank ×2–3 (30 water each, 15 logs) | 30–45 | ~100 | 4–6 | drought buffer; fill to 100% |
Then: Inventor (12), Campfire (15), more Lodges to beds≥P, tanks to (D+2)·2.13·P.

- RULE: WaterPump (12 logs) is the first log-costing build. Do not queue Lodge/Farmhouse ahead of it — 12 logs ≈ 1 lumberjack-day; it must land first.
- RULE: Queue in the ledger order above; a queued building silently reserves haul priority, so over-queueing early starves the pump.

## Food bootstrap (130 food ≈ 4 days)
Berries: bushes yield 3 per 12 days (0.25/bush/day) — a GathererFlag over 40+ bushes ≈ 10 food/day: a bridge, not a diet. Carrots: 4-day growth, 3/tile, edible raw, 0.75/tile/day — the real staple.
- RULE: Gatherers day 0; EfficientFarmhouse queued by day 3 planting CARROTS only; field size ≥ ceil(3.6 × P) tiles (2.67/0.75) — for P=13 plant ~48 tiles (7x7). First harvest ~day 8–9, safely before a cycle-1 drought (typically day 10+). Bank one full harvest before the drought hits.
- RULE: Do not plant potatoes/wheat in cycle 1 — both need processing buildings you can't afford yet; carrots feed raw.

sources:
- https://timberborn.wiki.gg/wiki/Lumberjack_Flag · /wiki/Gatherer_Flag · /wiki/Forester · /wiki/Trees
- https://timberborn.wiki.gg/wiki/Water_Pump · /wiki/Small_Tank · /wiki/Lodge · /wiki/Small_Warehouse · /wiki/Efficient_Farmhouse
- https://timberborn.wiki.gg/wiki/Game_Mode · /wiki/District_Center · /wiki/Crops
- https://timberborn.org/articles/early-game-strategy · https://finalboss.io/timberborn-how-to-survive-the-first-drought-starter
