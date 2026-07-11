# Timberborn v1.0.x — Water, Weather & Terrain Mechanics (numbers)

Companion to `agent/data/mechanics_water.json`. Numbers marked _(uncertain)_ come from
community guides or vary by patch; everything else is from the official wiki (timberborn.wiki.gg).

A game **day = 24h**, but a worker shift is ~**16h**, so "per day" production figures are
really per-shift. 1 water unit = 1 tile x 1 tile x 1.0 depth.

## 1. Weather cycle

A **cycle** = one **Temperate** season + one **Hazardous** season (a **Drought**, or a
**Badtide**). Folktails campaigns show droughts as the default hazard; Ironteeth see
badtides. Both mechanics exist on every map — badtide frequency is a dice roll (below).

### Season lengths (days)

| Difficulty | Temperate _(uncertain)_ | First Drought | First Badtide |
|-----------|--------------------------|---------------|---------------|
| Easy      | 16–19 | 1 | 1 |
| Normal    | 13–17 | 2–3 | 1 |
| Hard      | 5–8 | 3–6 | 6–12 |

First-event drought/badtide lengths are from the official wiki table. Temperate ranges are
community estimates.

Typical **later** drought lengths after the handicap ramps _(uncertain)_: Easy 2–4, Normal
5–9, Hard 15–30 days.

### Scaling over time

Hazards lengthen each cycle via a **duration handicap**:

- **Hard:** starts at **20%**, expands over a **15-cycle** window, +~**5%/cycle** to 100%.
- Per-cycle extra days _(uncertain)_: Easy +0.5–1, Normal +1–2, Hard +2–4.

### Which hazard? (drought vs badtide)

- Default **badtide chance = 40%**.
- After **5** consecutive droughts → badtide chance rises to **70%**; after **7** → **100%**.
- Base chances <5% are halved; <2.5% become 0%.

### Badtide contamination timeline

- Ramps **50% → 100% over the first 12h**.
- Ramps back **down starting 12h before the end**, ending at **50%**.
- On temperate return, the residual **50% drops to 0% instantly**.

## 2. Water pump

- **1 worker**, output **0.33–1.0 water per pumping action** (higher when water under the
  intake is deeper) → roughly **48 water/day** in ideal deep water _(uncertain; 3/h × 16h)_.
- **Internal storage 15**, **max pump depth 2 blocks**, min usable depth ~**0.3** _(uncertain)_.
- Footprint 2×3, height 2. **Cost 12 logs.** Pumps regular water only; efficiency falls as
  badwater concentration rises.
- **Deeper water = faster pumping.** This is why reservoirs are built **deep, not wide**.
- Later/variant pumps (Large, Mechanical/powered, Ironteeth Deep pump) have higher throughput
  _(uncertain specifics)_.

**Consumption:** a beaver drinks **~2.1/day** (≈**2.25** on a full 16h shift). One pump in
ideal conditions covers ~**21 beavers** _(uncertain)_.

## 3. Water storage

| Tank | Capacity (water) | Cost | Notes |
|------|------------------|------|-------|
| Small Water Tank | 30 | 20 logs | starter |
| Medium Water Tank | 300 _(uncertain)_ | — | |
| Large Water Tank | **1200** | 30 planks + 20 gears, 120 science | ~143 beaver-days |

**Buffer math:** days-of-water = `total_tank_capacity / (beavers × 2.25)`. Fluid Dumps can
hold water but are not efficient day-buffers — tanks are the intended store.

## 4. Terrain / water-control structures

| Structure | Footprint | Blocks height | Behavior |
|-----------|-----------|---------------|----------|
| **Dam** | 1 tile | ~0.5 | Weir — holds water then **overflows over the top**. Raises river level. |
| **Levee** | 1 tile | **1.0** | Solid wall; **stackable** (3 = 3.0 wall). Seals until overtopped. |
| **Floodgate** | 1-wide | adjustable | Raise/lower to hold or release; single/double/triple = 1.0/2.0/3.0 controllable height. |
| **Sluice** | — | — | Auto gate on water-level / contamination threshold (e.g. auto-dump badwater) _(uncertain)_. |
| **Dynamite / dig** | — | — | Removes terrain to **deepen reservoirs** / carve channels _(uncertain)_. |

## 5. Contamination (badwater)

- **Spreads** through the normal fluid sim — badwater flows and mixes like water. **Terrain**
  starts getting contaminated once local water contamination ≥ **50%**; range grows with
  concentration.
- **Kills plants:** crops/trees on contaminated ground survive only **0.2–0.3 days**.
- **Beavers:** >**5%** contamination → risk of becoming Unwell → **full contamination after 3
  days**. Contaminated beavers move **-70%** speed, **refuse to work**, non-basic needs frozen,
  wellbeing −10→0, and **do not recover on their own**. **Bots are immune.**
- **Cleanup:** Folktails **antidotes** restore **25%/day** (8 antidotes, ~4 days); Ironteeth
  **decontamination pods** ~**2 days** (uses extract + power). Soil clears once badwater recedes
  _(timing not officially numeric)_.
- **Barriers:** Irrigation/Contamination barriers block moisture & badwater seep between tiles
  — pair with levees/sluices to keep a clean reservoir _(uncertain specifics)_.

## 6. Irrigation (moisture)

- Every tile has a **moisture level**; crops and trees only grow on **moist soil**.
- Moisture spreads ~**7 tiles** (≈6–8) horizontally from a source on flat ground _(uncertain)_.
- **+1 block of elevation** above the water reduces range by ~**6 blocks**.
- A tile needs ≥**0.15 depth** to act as a moisture source.
- Water must be **<50% pollution** to irrigate; full effect at **0%** pollution. Above 50% it
  contaminates instead.
- During drought, water recedes and moisture **decays edge-inward** — unirrigated crops/trees
  die. **3-wide channels** minimize evaporation while maximizing reach.

## 7. Evaporation (the drought lever)

- Base: **0.04608 water/day × evaporation_value**.
- `evaporation_value` drops as a tile is surrounded by more water (adjacency 1→8 gives
  6.45 → 1.16). Exposed **narrow** water evaporates fastest; **deep, low-surface** reservoirs
  lose the least.
- Examples: 1-wide channel loses **0.209 m/day** of level; 3-wide loses **0.063 m/day**.
- **Roofs do not stop evaporation.** So: store water **deep with minimal surface area**.

## Sources

Official wiki: [Weather](https://timberborn.wiki.gg/wiki/Weather),
[Water Pump](https://timberborn.wiki.gg/wiki/Water_Pump),
[Evaporation](https://timberborn.wiki.gg/wiki/Evaporation),
[Irrigation](https://timberborn.wiki.gg/wiki/Irrigation),
[Contamination](https://timberborn.wiki.gg/wiki/Contamination),
[Badwater](https://timberborn.wiki.gg/wiki/Badwater),
[Levee](https://timberborn.wiki.gg/wiki/Levee).
Community: [gamerblurb cycles guide](https://gamerblurb.com/articles/timberborn-cycles-guide-seasons-droughts-and-badtides),
[Fandom Large Water Tank](https://timberborn.fandom.com/wiki/Large_Water_Tank).
