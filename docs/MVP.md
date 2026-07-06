# Timberborn Bot — MVP Contract

v: 2026-07-06 · game 1.0.13.1 · faction Folktails · map: default new-game map

This is the definition of DONE for the MVP and the design rules that get us there.
Nothing gets play-tested until its milestone is feature-complete (test-once policy).

## 1. Definition of done (measurable)

The bot, started with one command against a FRESH colony (real construction, real
resource costs, no instant/god placement), unattended:

- **D1 — survives cycle 1 drought**: no beaver dies of thirst or hunger through
  the first drought.
- **D2 — survives through cycle 3** (including a badtide if rolled): population
  at cycle 3 end >= starting population.
- **D3 — zero stranded buildings**: at every checkpoint, every placed building is
  path-connected to the district center — or gets demolished by the bot within
  2 decision turns of becoming unreachable.
- **D4 — economy bootstrapped**: by end of cycle 1: >=1 Lumberjack working,
  >=1 water pump pumping, >=1 food source working, everyone housed by cycle 2.
- **D5 — learns across runs**: run N+1 starts with lessons extracted from run N
  (playbook injected into the prompt); score trend across 3 runs is non-decreasing.
- **D6 — throughput**: an average decision turn commits >= 3 game-actions
  (plan queue), and a full bootstrap (D4 state) takes <= 15 decision turns.

## 2. Architecture: who is responsible for what

**The LLM is never responsible for remembering rules. Rules live in code.**

| Layer | Owns | Never does |
|---|---|---|
| Bridge (C# mod) | Facts (state/map/buildings/weather), validation (teaching errors), ALERTS (deterministic triage), actions incl. batch + priority | Strategy |
| Planner (Python, deterministic) | Goal checklist (water→wood→food→housing→storage→science), candidate tiles (incl. reachability BFS), affordability check, demolish-unreachable policy | Guessing coordinates |
| LLM (qwen2.5:14b) | CHOICE among validated options: which goal variant, which candidate tile, what to queue this turn | Free-form coordinates, rule recall |
| Learning loop (metrics/coach) | Score + penalties per run, lessons → playbook → prompt injection | — |

Rule-enforcement principle (feedback #8): every game rule the bot must follow is
encoded as (a) a bridge validator/teaching error, (b) a bridge alert, or (c) a
planner constraint. If a rule exists only in the prompt, it is NOT enforced.

## 3. Data contract (bridge)

`/state` (have): time, weather forecast, population, resources (Water/Log/Plank
always present, days_remaining), buildings.counts, district_center.
`/state` (MVP adds):
- `alerts[]` — deterministic triage, each `{id, severity, message, suggestion}`.
  Initial rule set: no_water_pump, no_log_production, no_food_production,
  homeless>0, water_days < next_hazard+2, food_days < next_hazard+2,
  logs_zero_and_sites_waiting, building_unreachable, sites_stalled.
- `buildings[]` detail — per building: spec, coords, finished|site(progress,
  missing materials)|paused, workers filled/slots, **reachable** (game-truth
  navigation check to district center).
`/map` (have): terrain_height, water_depth, contamination, moist, occupied.
`/screenshot` (have): binary PNG.
`/act` (MVP adds): `batch` (ordered list, per-action results, stop-on-error flag),
`set_priority` (construction priority), existing set_speed/pause/place/demolish/save.

## 4. Decision loop (MVP shape)

1. Bridge: state+map+buildings (+vision every Nth turn).
2. Planner (deterministic): compute goal checklist status from alerts + state;
   compute candidate tiles per goal (map fields + BFS reachability from DC +
   affordability from Log stock); attach relevant KB chunks (embedded search).
3. LLM: one call → `{plan: string, actions: [up to 8]}` — chooses among
   presented candidates/goals; may include demolish for unreachable buildings and
   set_speed to advance time.
4. Executor: run actions in order via /act batch; on teaching error, remaining
   actions still run unless dependent; results journaled.
5. Advance time (set_speed) is mandatory when sites are queued and unaffordable
   work remains — the planner enforces it if the LLM forgets.
6. Journal → metrics → coach lessons → next run's prompt.

## 5. Knowledge base

- Big + deep: complete Folktails building catalog (footprint WxDxH, cost, workers,
  science, power, stacking rules), mechanics with numbers (water/food consumption,
  tree growth, crop cycles, irrigation radius, district range), production chains
  with layouts, bootstrap orders, water engineering.
- Embedded: precomputed bge-m3 index (cached vectors, hash-invalidated); warm
  lookup < 100 ms; keyword fallback if Ollama is down.
- Retrieval is planner-driven (per active goal), not one generic query.

## 6. Learning loop (feedback #5: learner > smarter model)

- Score per run = weighted: population Δ, cycles survived, water/food buffer at
  hazard start, buildings completed; penalties: deaths, stranded buildings,
  actions rejected, sites never finished.
- coach.py extracts lessons (exists) → playbook.json → injected as PLAYBOOK block
  into the system prompt (to wire).
- Model stays cheap (qwen2.5:14b now; try 7b when loop is solid). We buy skill
  with scaffold + memory, not parameters.

## 7. Milestones (test-once each)

- **M1 Bridge data**: buildings[] + reachable + alerts + /act batch + set_priority.
  Accept: curl shows alerts on fresh colony; unreachable test building flagged.
- **M2 Planner**: goal checklist + candidates + BFS + affordability, unit-tested
  against recorded /state//map fixtures (no game needed).
- **M3 Agent rework**: plan-queue prompt + executor + playbook injection.
  Accept: dry-run against fixtures produces >=3 sensible actions/turn.
- **M4 Live run**: fresh colony, D1–D6 measured. Only after M1–M3 are complete.

## 8. Open items

- Navigation/reachability API: exact game service (research in flight).
- set_priority: correct component access (prior GetComponentFast failure).
- Batch size cap and stop-on-error semantics.
- Whether qwen2.5:7b holds up once planner does the heavy lifting (post-MVP).
