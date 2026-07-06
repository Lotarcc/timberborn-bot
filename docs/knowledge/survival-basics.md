Seed knowledge for the player agent KB/playbook. Timberborn v1.0.13.1. Numbers marked (verify) must be checked against installed game blueprint files.

# Timberborn Survival Basics (KB seed)

Purpose: concrete survival rules for a small model playing Timberborn autonomously. Read this as a rulebook, not prose. Every "RULE:" line is an action to take. Numbers are from the Timberborn wiki (timberborn.wiki.gg) plus community guides; several are version-sensitive and flagged (verify).

Convention:
- `P` = current population (number of beavers).
- `D` = length in days of the NEXT hazard (drought or badtide) shown in the weather forecast.
- "day" = one in-game day. A cycle = one temperate season + one hazard (drought or badtide).

---

## 1. The Clock — Weather & Cycle System

Three weather types. The game ALWAYS starts in temperate. Each cycle = temperate season, then a hazard.

| Weather   | Water sources | Effect on colony |
|-----------|---------------|------------------|
| Temperate | Flow normally (clean + badwater flow normally) | Safe prep phase. Refill tanks, farm, build. |
| Drought   | STOP producing. No river flow. | Only stored water in tanks/reservoirs is drinkable. Reservoirs evaporate. Crops on unirrigated land die. |
| Badtide   | Emit CONTAMINATED badwater instead of clean water. | Water supply becomes toxic. Beavers touching badwater get contaminated. Crops on contaminated ground die in ~0.2–0.3 days. |

### First-event durations by difficulty (days)

| Difficulty | First drought | First badtide | Notes |
|-----------|---------------|---------------|-------|
| Easy   | 1     | 1     | Badtide may be disabled/minimal on easiest setting (verify vs v1.0.13.1). |
| Normal | 2–3   | 1     | Default assumption for runs. |
| Hard   | 3–6   | 6–12  | Both hazards long from the start. |

- Hazards LENGTHEN over successive cycles. Early durations get a handicap multiplier that ramps toward 100% over several cycles (exact curve depends on game mode + RNG) (verify vs v1.0.13.1).
- RULE: Never assume the next hazard equals the last one. Read `D` from the in-game forecast each cycle and size storage to `D`, not to history.
- RULE: Treat drought length as monotonically increasing across a run. Every cycle, expand water storage; do not just maintain it.

### Badtide specifics
- Contamination ramps 50% → 100% over the first ~12 hours of the badtide, and 100% → 50% during the final ~12 hours; snaps to 0% the instant temperate returns.
- Badtide replaces the water in your river with contaminated water. Beavers that drink it get contaminated (a debuff), and it kills crops.
- Badtide presence: badtide is a hazard type that appears on Normal/Hard and on maps with badwater sources; it is reduced or off on the easiest settings (verify vs v1.0.13.1 map/difficulty config).
- RULE: During badtide, drink ONLY from tanks filled during temperate weather. Do not let pumps feed the drinking supply from a contaminated river.

---

## 2. Beaver Needs — Priority Order for Survival

Survival order (satisfy top-down; never let a lower need's building compete for labor with an unmet top need):

1. THIRST (water) — fastest killer.
2. HUNGER (food).
3. SLEEP / shelter.
4. Well-being needs (non-lethal, boost productivity/growth): Social Life, Aesthetics/Decoration, Fun/Recreation, Awe/Monuments, Wet Fur, Nutrition variety.

### Thresholds & timing (verify vs v1.0.13.1)

| Need   | Daily use        | Slow-down effect            | Death grace (from full) | Death grace (already deprived) |
|--------|------------------|-----------------------------|-------------------------|--------------------------------|
| Thirst | ~2.13 water/day  | −25% move speed when thirsty | ~5.71 days              | ~4.29 days (fastest to kill)   |
| Hunger | ~2.67 food/day   | −50% work speed when starving; becomes unemployable | ~5.0 days | ~3.75 days |
| Sleep  | 3–10 hrs/day     | −10% move speed; seeks shelter when exhausted; can fall asleep involuntarily | not directly lethal | — |

- THIRST is the emergency: a beaver with empty thirst dies in ~4.3 days, and thirst empties whenever no drinkable water is reachable. Water outage during a long drought is the #1 cause of colony death.
- Hunger is slower but a starving beaver stops working, which cascades (no pumping/farming → thirst deaths).
- Sleep does not kill directly but tanks productivity; unhoused beavers still function but slower.
- RULE: If any beaver's thirst OR hunger bar is dropping with no reachable supply, that is a CODE-RED. Fix water/food access before building anything else.
- RULE: Keep well-being buildings (campfire, decorations) OFF the critical path. Build them only after thirst+hunger+sleep are secured with a buffer.

---

## 3. Critical Resources as "Days Remaining"

Frame every survival decision as: "How many days of water/food do I have stored for population `P`?"

### 3a. WATER

- Beavers drink ~2.13 water/day each → colony daily water use `W = 2.13 * P` (round up to ~2.5*P for safety margin).
- Water is only "safe" once it is INSIDE a tank (tanks do not evaporate). Water sitting in a dammed reservoir counts too but evaporates during drought and can be contaminated during badtide.

Storage capacities (verify vs v1.0.13.1 blueprints):

| Store             | Faction | Capacity (water units) | Cost        | Science | Footprint |
|-------------------|---------|------------------------|-------------|---------|-----------|
| Small Tank        | Both    | 30                     | 15 logs     | start   | 1x1 h2    |
| Medium Tank       | Both    | 300                    | 30 planks + 20 gears | 120 | 2x2 h3 |
| (Large Tank)      | Both    | larger (verify)        | verify      | verify  | verify    |
| Water Pump internal | Folktails | 15 (buffer, not storage) | 12 logs | start | 2x3 h2 |

Sizing rule (the core water math):

```
Required stored water  =  (D + 2) * 2.13 * P
                          rounded UP, where D = next drought length in days,
                          +2 days buffer for forecast error / refill lag.
Tanks needed (Small)   =  ceil( Required / 30 )
Tanks needed (Medium)  =  ceil( Required / 300 )
```

- Example: P=10, next drought D=4 → Required = (4+2)*2.13*10 ≈ 128 water. That is ceil(128/30)=5 Small Tanks, OR 1 Medium Tank (300) with margin.
- Community rule of thumb agrees: "~2 water per beaver per day of drought" plus buffer; the 2.13 figure is the precise consumption.
- RULE: Before EVERY dry season, ensure `stored_water >= (D + 2) * 2.13 * P`. If short, pause expansion and build tanks/pumps until met.
- RULE: Pumped water in the river is NOT safety. It only counts once it reaches a tank. Prioritize tanks over more pumps once pump throughput is adequate.
- RULE: A dammed reservoir is a cheap early water reserve, but discount its volume for evaporation during long droughts and never rely on it during badtide (it gets contaminated).

Pumps (fill the tanks):

| Pump             | Faction    | Cost    | Science | Footprint | Workers | Max depth | Output |
|------------------|------------|---------|---------|-----------|---------|-----------|--------|
| Water Pump       | Folktails  | 12 logs | start   | 2x3 h2    | 1       | 2 blocks  | ~0.33–1 water/tick (verify) |
| Deep Water Pump  | Iron Teeth | 12 logs | start   | 2x3 h2    | 1       | 6 blocks  | ~0.33–1 water/tick (verify) |

- Both pumps pump ONLY clean water and work less efficiently as badwater concentration rises → useless for drinking during badtide.

### 3b. FOOD

- Beavers eat ~2.67 food/day each → colony daily food use `F = 2.67 * P`.
- Two supply modes: FORAGING (Gatherer Flag picks wild berries — instant, no growth wait, low yield) and FARMING (Farmhouse plants + harvests crops — higher yield, needs growth time + irrigated land).

Early crops (Folktails; verify vs v1.0.13.1):

| Food     | Source            | Growth time | Yield/plant | Prep needed        | Survival note |
|----------|-------------------|-------------|-------------|--------------------|---------------|
| Berries  | Gatherer Flag (wild bushes) | none (pick when ripe) | low (~0.25/day/source) | none | Instant early food; finite wild supply. |
| Carrots  | Efficient Farmhouse | 4 days    | ~3 units    | edible raw         | Fastest crop; best first farm food. |
| Sunflower| Efficient Farmhouse | 5 days    | ~2 seeds    | edible raw         | Backup raw food. |
| Potatoes | Efficient Farmhouse | 6 days    | ~1 unit     | cook at Grill (logs+worker) | Higher effort. |
| Wheat    | Efficient Farmhouse | 10 days   | ~3 units    | Gristmill + Bakery (power) | Late; not for first drought. |

Food storage sizing:

```
Required stored food = (D + 2) * 2.67 * P   (round up)
```

- Farms DON'T grow during drought unless the plot is irrigated (near water); harvests halt, so you must have stockpiled food before the drought like water.
- RULE: Start with a Gatherer Flag for immediate berries, then stand up a Farmhouse with carrots (4-day cycle) before the first drought.
- RULE: Before every dry season, ensure `stored_food >= (D + 2) * 2.67 * P`. Berries + a full carrot harvest should cover it early.
- RULE: Keep at least one full food-storage buffer at all times; a starving beaver stops working and the colony spirals.

---

## 4. Minimal Early Build Order (First Wet Season → Survive First Drought)

Ordered checklist. Do these top-to-bottom; do not skip ahead to well-being until step 8.

1. District Center is already placed (holds starting resources, 4 builder/hauler workers). Build the colony around it.
2. Paths — connect every building to the District Center. Beavers can't reach unconnected buildings. Lay paths first/continuously.
3. Water Pump (Folktails) / Deep Water Pump (Iron Teeth) on the river → gets clean water flowing to storage. 12 logs, no science.
4. Water storage — build Small Tanks (30 each, 15 logs) NOW and keep them filling. Target the Section-3 water math for the first forecast drought.
5. Housing — Lodge (Folktails, 12 logs, houses 3) or faction equivalent, enough beds for `P`. Satisfies sleep + shelter.
6. Food gathering — Gatherer Flag (free, 1 worker) for immediate berries.
7. Food farming — Efficient Farmhouse (25 logs, 3 farmers) planting carrots (4-day cycle). Get a harvest banked before drought.
8. Storage buildings — Log Pile and a Warehouse/food storage so resources aren't capped in building buffers. (verify building names/caps vs v1.0.13.1)
9. Science / Inventor — build the science building (Inventor for Folktails) only AFTER water+food+housing are secured. Science unlocks tanks (Medium=120), floodgates (150), levees (120), etc. It is important but NOT before basic survival.
10. Dam the river (20 logs each, no science) to raise a reservoir as a cheap secondary water reserve before the first drought if time/logs allow.

- RULE: The instant a drought is forecast, STOP discretionary building and verify water+food day-counts (Section 3). Top up tanks and food first.
- RULE: Logs are the master early resource (pumps, tanks, housing, dams all cost logs). Keep a Lumberjack cutting and a Forester replanting so you never run dry on logs.

---

## 5. Buildings Reference Table (Survival-Critical)

All costs/sizes verify vs v1.0.13.1 blueprints. "start" science = available from game start.

| Building | Faction | Science | Material cost | Footprint | Workers | Purpose (one line) |
|----------|---------|---------|---------------|-----------|---------|--------------------|
| District Center | Both | start | free (placed) | 3x3 h5 | 4 | Colony hub; holds starting resources; builders/haulers. |
| Path | Both | start | ~ (cheap) | 1x1 | 0 | Connects buildings; beavers can't reach off-path structures. |
| Water Pump | Folktails | start | 12 logs | 2x3 h2 | 1 | Pumps clean water (depth ≤2) to fill tanks. |
| Deep Water Pump | Iron Teeth | start | 12 logs | 2x3 h2 | 1 | Iron Teeth water pump, depth ≤6, no power. |
| Small Tank | Both | start | 15 logs | 1x1 h2 | 0 | Stores 30 water; drought insurance (no evaporation). |
| Medium Tank | Both | 120 | 30 planks + 20 gears | 2x2 h3 | 0 | Stores 300 water; bulk drought reserve. |
| Lodge | Folktails | start | 12 logs | 2x2 h1 | 0 | Houses 3 beavers; satisfies sleep + shelter. |
| Gatherer Flag | Both | start | free | 1x1 h2 | 1 | Gathers wild berries/food; instant early food. |
| Efficient Farmhouse | Folktails | start | 25 logs | 3x2 h2 | 3 | Plants/harvests carrots, potatoes, sunflowers, wheat. |
| Lumberjack Flag | Both | start | (verify) | 1x1 | 1 | Fells trees for logs (master resource). |
| Forester | Both | start | (verify) | (verify) | 1 | Replants trees to sustain log supply. |
| Log Pile | Both | start | (verify, cheap) | (verify) | 0 | Stores logs so production isn't buffer-capped. |
| Inventor (science) | Folktails | start | (verify) | (verify) | 1 | Generates science to unlock tanks/floodgates/etc. |
| Dam | Both | start | 20 logs | 1x1 h1 | 0 | Blocks flow at 0.65 blocks; builds reservoirs. |
| Levee | Both | 120 | 12 logs | 1x1 h1 | 0 | Blocks water completely; stackable waterproof wall. |
| Floodgate | Both | 150 | 10 logs + 5 planks | 1x1 h2 | 0 | Adjustable-height gate (0–1, 0.05 steps) to route/hold water. |
| Grill | Folktails | (verify) | (verify) | (verify) | 1 | Cooks potatoes into edible food. |

- RULE: If playing Iron Teeth, substitute faction housing and use the Deep Water Pump; Folktails buildings above are cheaper (mostly logs) and are the recommended beginner faction.

---

## 6. Water Engineering Basics (store wet-season water, keep badtide out)

Goal: capture temperate-season water so it survives the drought, and divert contaminated badtide water away from the drinking supply. Four primitives:

| Structure | What it does | Survival use |
|-----------|--------------|--------------|
| Dam       | Blocks flow but spills over the top at ~0.65 blocks. | Raise river level → create a reservoir upstream that persists into drought. |
| Levee     | Blocks water 100% (stackable). | Build solid walls of a reservoir/canal; wall off a badwater channel. |
| Floodgate | Adjustable-height gate (0–1). Water above the set height spills; at/below is held. | Manually open/close to let water in during temperate, seal it in for drought, or block badwater. |
| (Sluice)  | Gate that can auto-open/close on contamination level (verify availability/name in v1.0.13.1). | Automate: close drinking channel when contamination high, open a "garbage chute" to flush badwater. |

Storing wet-season water:
- Place dams/levees across the river to form a reservoir; the deeper/wider the reservoir, the more water banked. Use floodgates to seal the reservoir before drought so it doesn't drain downstream.
- Still fill TANKS as primary insurance — reservoir water evaporates during long droughts and is exposed to badtide; tanks don't.

Keeping badtide water out of the drinking supply:
- PRINCIPLE: Isolation. Route contaminated water AWAY from the colony, as far UPSTREAM (and as close to the source) as possible.
- Build a diversion channel that carries river water past/around the colony. At the junction, use floodgates/levees:
  - Temperate: open the channel INTO the colony reservoir (clean water fills tanks/reservoir).
  - Badtide: SLAM the colony intake shut (levee/closed floodgate) and open a bypass ("garbage chute") that dumps the contaminated water downstream, away from drinking storage.
- If sluices/automation are available: set colony-intake sluices to close when contamination > ~5%, and bypass sluices to open when contamination > ~5% (invert for the clean path). This survives badtide without micromanagement (verify sluice/automation exists in v1.0.13.1; otherwise do it manually each badtide).
- RULE: Enter every badtide with tanks already full of clean water and the colony intake physically sealed. Drink from tanks only until temperate returns.

---

## 7. How Colonies Die + Prevention Rules

Top failure modes, each with an imperative prevention rule.

| # | Failure mode | Why it kills | PREVENTION RULE |
|---|--------------|--------------|-----------------|
| 1 | Ran out of water in drought | Thirst empties, beavers die in ~4.3 days; no pumping possible (river dry). | Before each dry season, store `>= (D + 2) * 2.13 * P` water IN TANKS. |
| 2 | Food collapse | Starving beavers (−50% work, then unemployable) stop pumping/farming → thirst deaths cascade. | Before each dry season, store `>= (D + 2) * 2.67 * P` food; keep a Gatherer + carrot Farmhouse running. |
| 3 | Drank contaminated badtide water | Pumps/reservoir feed toxic water to beavers; contamination debuff + crop death. | Seal colony intake during badtide; drink only from temperate-filled tanks; divert badwater upstream. |
| 4 | Overexpanded population | More beavers than stored water/food can cover for `D` days → mass die-off. | Cap growth: only let `P` rise when stored water AND food both still satisfy the Section-3 formulas for the forecast `D`. Check before allowing breeding/immigration. |
| 5 | No / too little storage | Water stuck in river or building buffers, not banked → nothing to draw on in drought. | Build tanks + log/food storage early; treat "water in a tank" as the only water that counts. |
| 6 | Buildings unreachable / no workers | Path not connected, or all workers starving/thirsty → pump & farm idle. | Keep every survival building path-connected to the District Center; keep pump/farm workers fed+hydrated first. |
| 7 | Ignored the forecast | Prep started after the river dropped → too late to fill tanks. | On every forecast, immediately recompute water/food day-counts and top up BEFORE the hazard begins. |
| 8 | Ran out of logs | Can't build pumps/tanks/dams; expansion stalls mid-drought-prep. | Keep a Lumberjack + Forester loop running so logs never hit zero. |

### Turn-loop survival check (run every cycle / on every forecast)
1. Read next hazard type (drought/badtide) and length `D` from forecast.
2. Compute `need_water = (D+2)*2.13*P`, `need_food = (D+2)*2.67*P`.
3. If `stored_water < need_water` → build/fill tanks, add pumps; PAUSE non-survival builds.
4. If `stored_food < need_food` → add gatherers/farms, bank harvests.
5. If hazard is badtide → verify colony intake can be sealed and tanks are full & clean.
6. Only if 3–5 satisfied with buffer: allow population growth, science, well-being, expansion.

---

## Sources

- https://timberborn.wiki.gg/wiki/Weather — weather types, first-event durations, badtide contamination ramp.
- https://timberborn.wiki.gg/wiki/Needs — thirst/hunger/sleep consumption, slow-down thresholds, death grace times.
- https://timberborn.wiki.gg/wiki/Water_Pump — Water Pump (Folktails) stats.
- https://timberborn.wiki.gg/wiki/Deep_Water_Pump — Deep Water Pump (Iron Teeth) stats.
- https://timberborn.wiki.gg/wiki/Small_Tank — Small Tank capacity/cost.
- https://timberborn.wiki.gg/wiki/Medium_Tank — Medium Tank capacity/cost/science.
- https://timberborn.wiki.gg/wiki/Food — daily food consumption, early food comparison.
- https://timberborn.wiki.gg/wiki/Efficient_Farmhouse — farmhouse stats + crop growth times/yields.
- https://timberborn.wiki.gg/wiki/Carrot — carrot as food/faction.
- https://timberborn.wiki.gg/wiki/District_Center — district center role/stats.
- https://timberborn.wiki.gg/wiki/Lodge — Lodge housing stats.
- https://timberborn.wiki.gg/wiki/Gatherer_Flag — gatherer flag stats/function.
- https://timberborn.wiki.gg/wiki/Dam — dam spillway height/cost.
- https://timberborn.wiki.gg/wiki/Levee — levee (blocks water fully)/cost/science.
- https://timberborn.wiki.gg/wiki/Floodgate — floodgate adjustable height/cost/science.
- Community: slashskill.com, whisperofthehouse.com, finalboss.io, timberborn.org (beginner/drought guides); Steam Community discussions + neonlightsmedia.com (badtide diversion / sluice T-valve automation).
