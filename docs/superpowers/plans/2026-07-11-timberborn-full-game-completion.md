# Timberborn Full-Game Completion — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An autonomous agent that plays a full game of Timberborn (v1.0.13.1, Folktails) from a fresh colony to a **stable ~30-beaver colony that survives droughts** (water + food buffered past the longest drought, homeless=0), driven by a **trained decision model** (LIDSNet MLP + CART) that clones a **deterministic full-game expert planner**, and **improves run-to-run** via a replay → credit-assignment → relabel → retrain loop.

**Architecture:** The bridge (C# mod inside the game) exposes state + actions over HTTP. A Python agent loop each cycle: reads state → the trained model picks an intent (WHAT) over an 83-action DB-driven space → the planner picks placement (WHERE) → the agent's own trunk pather lays paths → time advances (night-fast-forwarded). The model is behaviorally cloned from `planner.py`; **the model can only be as good as that expert**, so the core remaining work is making the expert play the whole economy, then regenerating the dataset and retraining on the Windows GPU. Runs are recorded; failures relabel the dataset so the next model is better.

**Tech Stack:** Python 3.14 (Mac, agent, no torch), C#/.NET netstandard2.1 (bridge mod, built with `dotnet` 10 against the game's Managed DLLs), PyTorch 2.6+cu124 + scikit-learn on the Windows GPU box (training only), pure-JSON model export for dependency-free inference.

## Global Constraints

- **Target game:** Timberborn v1.0.13.1 (build 23107127), faction **Folktails**. Internal `Timberborn.*` types are not a stable API — verify against decompile before use.
- **Game runs LOCALLY on the Mac** (M1 Pro), process `Timberborn.app/Contents/MacOS/Timberborn`, bridge on `http://127.0.0.1:7744`. DLLs at `~/Library/Application Support/Steam/steamapps/common/Timberborn/Timberborn.app/Contents/Resources/Data/Managed`.
- **The Mac has a torch venv now** (`./.venv`, Python 3.13, macOS arm64 torch **with the MPS/Metal backend** — `torch.backends.mps.is_available()` → True), plus numpy/scikit-learn/pandas/networkx/pytest. So the **M1 Pro can train AND infer locally** (fast iteration; no scp-to-Windows per run). The RTX 4060 Ti Windows box stays the heavy-training option. **Run everything via `.venv/bin/python`** — the system `python3` is 3.14 and has none of these deps. Pure-Python JSON inference (`agent/nlp/model.py`) still works and stays valid (keeps the deployed agent torch-optional), but is no longer forced. `requirements.txt` reproduces the venv. **Dependencies are allowed** — use numpy/networkx/pytest/etc. where they make the code clearer.
- **GPU box:** `ssh cka-win` (192.168.88.72, RTX 4060 Ti 16 GB). torch venv at `C:\Users\semyo\tb_ml`, work dir `C:\Users\semyo\tb_ml_work`. Alias lives in `~/.ssh/cka-lab-config` (Included from `~/.ssh/config`).
- **Placement decides WHERE, the model decides WHAT.** Never train the model to output coordinates.
- **Paths are agent-owned** (`auto_path.py`), placed with `auto_connect:false`; the bridge no longer paves per building in the play loop.
- **Commit after every meaningful step.** `git push` is currently BLOCKED (session creds lack access to `Lotarcc/timberborn-bot`); commit locally, the operator pushes.
- **Keep secrets/infra out of the repo** (IPs, hostnames, SSH paths, account names live in operator agent-memory only).

---

## Part A — Current State (what you inherit)

### The bridge mod (`mod/TimberBridge/`, C#)
Builds clean; `dotnet build TimberBridge.csproj -c Release -p:TimberbornManaged=<Managed path>`. Endpoints on `:7744`:
- `GET /ping` → `{ok,in_game,game_version}`
- `GET /state` → `{time:{cycle,day,hour,daytime}, population:{total,adults,kits,homeless,unemployed,free_beds,...}, resources:[{good,stored,all_stock,days_remaining,capacity,fill_rate}], buildings:{counts:{Spec.Faction:int}, list:[{spec,x,y,z,status,reachable,workers,max_workers,access:[{x,y,z}],access_diag}]}, weather:{current,current_ends_in_days,next:{type,in_days,duration_days}}, district_center, alerts}`
- `GET /map` → per-tile row-major arrays over a window around the DC: `origin{x,z}, width, height, terrain_height, water_depth, contamination, moist, occupied, reachable, on_road, resource` (resource: 0 none / 1 tree / 2 gatherable). index = row*width+col; tile x=origin.x+col, y=origin.z+row.
- `GET /resources` → `{trees:[{x,y,z,species,good,mature}], gatherables:[{x,y,z,species,good,ready}], counts}`
- `GET /blueprints` → 201 spec ids (`{id, building}`), the ground-truth key set.
- `GET /screenshot` → binary PNG.
- `POST /act` body `{command, args}`. Commands: `place_building{spec,x,y,z,orientation?,instant?,auto_connect?}` (returns `applied.auto_connect{connected,paths_laid,path_tiles,access_tiles,reason}`), `designate_cutting{all:true}`, `undesignate_cutting`, `designate_planting{species,tiles}`, `demolish{x,y,z}`, `set_priority`, `set_speed{speed}` (accepts >3, e.g. 12), `set_camera`, `set_working_hours{hours|fraction}` (WorkedPartOfDay; 22h→end_hours 22), `save{name}`, `batch{...}`.

Key bridge internals (all verified via decompile, documented inline):
- Auto-connect resolves a building's **access tile from the just-created entity** (not a coord re-lookup, which returned terrain for relocated/sloped buildings). Access tile itself must be a road node.
- Connector paths are **construction sites** (`CreateUnfinished`), built by beavers; deep-water tiles get a **Platform** instead of a Path.
- `ReachabilityReader.AccessDiag` is a live diagnostic (why access is null) — leave it, it's cheap.

### The trained decision model (`agent/nlp/`) — WORKS but BOOTSTRAP-ONLY
- `features.py` — the OLD hand-coded featurizer (14 `ACTIONS`, ~47 features). **Superseded by `game_schema.py`** but still what the current model was trained on. `StateFeaturizer.fit/transform`, `feature_strings(state)`.
- `labeler.py` — `Oracle.label(state)`: placement-independent expert label (feeds `planner` fake candidates so labels don't depend on map geometry). **This is the behavioral-cloning oracle.**
- `dataset.py` — synthetic state sweep + journal harvest → `data/decision_dataset.json` (9,869 rows).
- `train_lidsnet.py` / `train_cart.py` — GPU trainers (torch MLP faithful to NLP_2.0; sklearn CART max_depth 6) → JSON export.
- `model.py` — dependency-free `CartModel`, `MlpModel` (pure-Python forward).
- `policy.py` — `DecisionPolicy.load()`, `.rank(state)→[(goal_id,conf)]`, `.decide(state)`, `.disagreement(state)`.
- `play_policy.py` — the live play loop: each cycle sets work hours, designates cutting, ranks intents, places top executable one (`auto_connect:false`), lays the trunk via `auto_path`, advances time night-aware. Caps concurrent sites (`MAX_ACTIVE_SITES=3`).
- **Current model quality:** LIDSNet val 99.95%, reproduces the (bootstrap) expert 99.99%; runs on Mac in ms. It plays the bootstrap (lumberjack, gatherer, pump) but **cannot build anything past the ~10 bootstrap buildings** — see the gap below.

### The DB-driven schema (`agent/game_schema.py`) — NEW, the foundation for the full game
Derives from `agent/data/{buildings,goods,recipes,chains,needs,tech_tree}.json`:
- `actions()` → **83 actions** (`build_<snake>` for every gameplay building + verbs). `action_to_spec` / `spec_to_action` / `building_tier(spec)` (start/early/mid/late by science cost).
- `feature_strings(state)` → ~32 DB-grounded features: per-good stock + days, `makes_<good>` production capacity, `cat_<category>` counts, power balance, science/storage/power presence.
- **NOT yet wired into the model** — the model still uses the old `features.py`. Wiring it in + retraining is Task 3–5.

### Planning modules (pure Python, unit-tested)
- `agent/auto_path.py` — **agent-owned trunk pathing.** `plan(state,map)`, `connect_all(bridge,state,map)`. Seeds the network with the DC entrance + every existing Path/Platform (built or site) so it reuses the trunk (idempotent; killed the 73-path sprawl → 15). Uses `path_network`.
- `agent/path_network.py` — greedy-Steiner shared spine. `plan_network(map_data, dc_road_tiles, building_access_tiles)`. **Paves the access tile itself** (fixed the one-tile-short bug).
- `agent/resource_manager.py` — `analyze(state)`, `production_chain_for(good)`, `next_production_building(state)`, `drought_prep(state)`.
- `agent/layout_macros.py` — overlap-free multi-building templates (`forester_plantation`, `pump_and_storage`, `housing_cluster`, `bakery_chain`).
- `agent/time_manager.py` — `work_hours_for(state)` (22h ≤16 pop → 18/16/14h as it grows), `is_night(state)`, `speed_for(state)` (day 6 / night 12).
- `agent/planner.py` — the deterministic expert. **Only emits ~10 bootstrap goals.** `analyze`, `plan_report`, `candidates_for(goal,state,map,resources)`, `_building_count`, `_resource_days`, etc. **This is the file the full-game work extends.**
- `agent/controller.py` — `build_safe_ready_frontier(report,state)` (the expert selection with affordability/caps), `bulk_advance_until_wake(bridge,state,run_speed)`, `_read_cycle_inputs`.

### Game database (`agent/data/*.json`) — COMPLETE
157 buildings (26 with production), 37 goods, 26 production chains, needs (survival + 8 well-being), tech tree (science-gated unlocks + recommended order), flora, water/weather numbers. Assembled + reconciled against `/blueprints`.

### Knowledge docs
`docs/kb/` (mechanics, layouts, nlp-pipeline-replication.md, aiplayer-architecture.md), `docs/superpowers/specs/2026-07-11-spatial-lifecycle-planner-design.md`.

### The gap (why the goal isn't reached)
The model's action/feature space is bootstrap-only, AND the expert (`planner.py`) only knows the bootstrap. **Behavioral cloning can't teach what the expert doesn't know.** To play the full economy the expert must reason about production chains, science-gated tech, well-being (for breeding), power, storage, and drought engineering — then the model is retrained over the 83-action DB space. That is Parts D below.

---

## Part B — Operational Runbook (critical tacit knowledge)

### Build the mod (Mac)
```bash
export DOTNET_ROOT=/opt/homebrew/Cellar/dotnet/10.0.301/libexec
MG="$HOME/Library/Application Support/Steam/steamapps/common/Timberborn/Timberborn.app/Contents/Resources/Data/Managed"
cd mod/TimberBridge && /opt/homebrew/bin/dotnet build TimberBridge.csproj -c Release -p:TimberbornManaged="$MG"
```

### Reload the game with the fresh DLL (Mac) — needed after any mod change
```bash
MODDIR="$HOME/Documents/Timberborn/Mods/TimberBridge"
MARKER="$HOME/Library/Application Support/Mechanistry/Timberborn/timberbridge_autoload.flag"
cp mod/TimberBridge/bin/Release/TimberBridge.dll "$MODDIR/TimberBridge.dll"
pkill -f "Timberborn.app/Contents/MacOS/Timberborn"; sleep 3
printf "new"  > "$MARKER"   # "new" = fresh Folktails colony; "" = load most recent save
open "$HOME/Library/Application Support/Steam/steamapps/common/Timberborn/Timberborn.app"
# A "modded game" warning dialog blocks the AutoLoader. Dismiss it while polling:
for i in $(seq 1 45); do
  curl -s --max-time 3 http://127.0.0.1:7744/ping | grep -q '"in_game":true' && break
  osascript -e 'tell application "Timberborn" to activate' -e 'delay 0.2' -e 'tell application "System Events" to key code 36' >/dev/null 2>&1
  sleep 4
done
```
`AutoLoader.cs` reads the marker at the main menu (`""`/`recent` → most-recent save, `new` → new game). Decompile with `DOTNET_ROOT=... ~/.dotnet/tools/ilspycmd -t <FullTypeName> "$MG/Timberborn.<Asm>.dll"`.

### Train — LOCAL on the Mac (preferred for iteration) or on the GPU box
Local (M1 Pro MPS, no network round-trip):
```bash
.venv/bin/python -m agent.nlp.train_cart      # sklearn CART -> JSON
.venv/bin/python -m agent.nlp.train_lidsnet   # torch MLP (device='mps') -> JSON export
```
Heavy runs on the RTX 4060 Ti box (still available):
```bash
scp agent/data/decision_dataset.json agent/data/decision_vocab.json agent/data/decision_labels.json cka-win:C:/Users/semyo/tb_ml_work/data/
ssh cka-win "C:\Users\semyo\tb_ml\Scripts\python.exe C:\Users\semyo\tb_ml_work\nlp\train_cart.py & C:\Users\semyo\tb_ml\Scripts\python.exe C:\Users\semyo\tb_ml_work\nlp\train_lidsnet.py"
scp cka-win:C:/Users/semyo/tb_ml_work/data/decision_cart.json cka-win:C:/Users/semyo/tb_ml_work/data/decision_mlp.json agent/data/
```
(GPU-box path: sync `agent/nlp/train_*.py` to `tb_ml_work/nlp/` if changed. `train_lidsnet.py` should select `mps` on Mac / `cuda` on Windows / `cpu` fallback. Trainers read `../data/decision_*.json` relative to their own path.)

### Run the agent
```bash
.venv/bin/python -m agent.nlp.play_policy --cycles 30 --run-id <name>   # journal → agent/journal/<name>.jsonl
```

### Gotchas learned the hard way
- Background `sleep`+`cat` chains are blocked by the harness; poll with `until <cond>; do sleep N; done` in a `run_in_background` bash call, or read the output file.
- Subagents occasionally return skill-ceremony text and do nothing — prefix subagent prompts with "You are a subagent executing a specific coding task. Do NOT invoke skills."
- `_building_count` must match faction-suffixed keys (`LumberjackFlag.Folktails`) by bare-prefix.
- A building is staffed only when its **access tile is on the road** (tight network), distinct from builder road-spill reachability.

---

## Part C — Architecture (target)

```
/state,/map,/resources ─► play loop (agent/nlp/play_policy.py)
                             │  each cycle:
   game_schema.feature_strings(state) ─► DecisionPolicy.rank ─► curriculum.bias_ranking
                             │        (trained model = WHAT, over 83 DB actions)
                             ▼
   planner.candidates_for(goal) ─► placement (WHERE) ─► bridge place_building{auto_connect:false}
                             ▼
   auto_path.connect_all  ─► one DC-rooted trunk (WHERE for paths)
                             ▼
   time_manager ─► set_working_hours + night-fast-forward advance
                             ▼
   replay.record_step ─► agent/runs/<run>.jsonl
   ── end of run ──► replay.summarize_run + credit_assignment
                     ─► learn.build_augmented_dataset ─► retrain on GPU ─► better model next run
```

**The expert (`planner.py`) is the teacher.** Everything the model can do, the expert must first do deterministically. The learning loop lets outcomes (deaths, stalls) correct the expert's blind spots over runs.

---

## Part D — Ordered Implementation Tasks

Each task ends with a runnable test and a commit. Modules under `agent/` are pure stdlib (no torch, no network in unit tests) unless noted. State/map schemas are in Part A.

### Task 1: Land the learning-loop modules (replay + credit assignment)

**Files:**
- Create: `agent/replay.py`
- Test: `agent/replay.py` (inline `unittest`, run `python3 agent/replay.py`)

**Interfaces — Produces:**
- `record_step(run_id, step, state, action, meta=None)` → appends a JSON line to `agent/runs/<run_id>.jsonl` capturing `{step, day, hour, pop_total, homeless, water_days, food_days, log_stored, plank_stored, building_counts, action, meta}`.
- `load_run(run_id) -> list[dict]`
- `summarize_run(run_id) -> {days_survived, peak_pop, final_pop, ended∈{alive,dead_thirst,dead_hunger,stalled}, death_cause, min_water_days, min_food_days, reached_pump:bool, reached_30_pop:bool}`
- `credit_assignment(run_id, lookback=6) -> [{step, state_snapshot, chosen_action, better_action, reason}]`

**Detail:** `food_days` = max `days_remaining` over goods where `goods.json` `is_food`. `stalled` = ≥8 consecutive `advance_time` with no increase in `log_stored` or total building count. `dead_thirst`/`dead_hunger` = water/food days ≈0 for ≥3 steps or pop→0. Credit heuristic: thirst → `better_action="build_water_pump"` (or `build_water_storage` if a pump exists) for the lookback steps; hunger → `build_gatherer`/`build_farm`; stall → the highest-priority unbuilt survival building.

- [ ] **Step 1:** Write `agent/replay.py` with the four functions + a `unittest.TestCase` that synthesizes one thirst-death trace and one survivor trace via `record_step`, then asserts `summarize_run` classifies each and `credit_assignment` returns water-related regret windows for the death.
- [ ] **Step 2:** Run `python3 agent/replay.py` — expect OK.
- [ ] **Step 3:** Commit `feat: replay recorder + credit assignment`.

### Task 2: Land the curriculum (phase manager)

**Files:**
- Create: `agent/curriculum.py`
- Test: inline `unittest`, `python3 agent/curriculum.py`

**Interfaces — Produces:**
- `current_phase(state) -> str` in order: `SURVIVE_WATER, SECURE_FOOD, HOUSE, DROUGHT_PROOF, GROW, STABLE`.
- `phase_priorities(phase) -> [goal_id]` (goal_ids from `game_schema.actions()`).
- `is_goal_reached(state) -> bool` (STABLE: pop≥30, water_days & food_days > longest drought, homeless=0).
- `bias_ranking(state, ranked) -> ranked` — stable-sort so the current phase's priority goals lead.

**Detail:** exit criteria per phase — SURVIVE_WATER: a WaterPump + ≥2 SmallTank and water_days > drought+2; SECURE_FOOD: gatherer + a farm and food_days > drought+2; HOUSE: homeless=0 with free beds; DROUGHT_PROOF: `resource_manager.drought_prep` deficit ≤0; GROW: pop≥30. Use `resource_manager` for buffer math.

- [ ] **Step 1:** Write `agent/curriculum.py` + `unittest` with synthesized states for each phase asserting `current_phase`, `is_goal_reached`, and that `bias_ranking` promotes phase-appropriate goals.
- [ ] **Step 2:** `python3 agent/curriculum.py` — expect OK.
- [ ] **Step 3:** Commit `feat: colony curriculum / phase manager`.

### Task 3: Extend the expert planner to the full production economy

This is the crux and the largest task. Split into 3a–3e; each adds goal emitters to `planner.analyze` and candidate logic to `planner.candidates_for`, keyed on `game_schema`.

**Files:** Modify `agent/planner.py`; Create `agent/economy.py` (pure helper for chain/tech reasoning so `planner.py` stays readable); Test: `agent/test_economy.py`.

**Interfaces — `agent/economy.py` Produces:**
- `needed_producers(state) -> [spec]` — for each good the colony consumes but can't produce (walk `chains.json` from demand back to raw), the producer building to add (e.g. planks demanded by construction/gears → `LumberMill`).
- `unlockable_now(state) -> [spec]` — buildings whose `science_cost` ≤ stored SciencePoints and prerequisites are met (`tech_tree.json`).
- `power_deficit(state) -> float` and `power_building_suggestion(state) -> spec` (WaterWheel/PowerWheel/WindTurbine by availability).
- `storage_pressure(state) -> [good]` (goods near capacity) → pile/warehouse suggestion.

**Detail per subtask (each its own commit):**
- **3a Production chains:** emit `build_<producer>` goals when a downstream good is demanded and its producer is missing/insufficient. Order by chain depth (raw→refined). Candidates: cluster near existing related industry (use `layout_macros.bakery_chain` style adjacency).
- **3b Tech progression:** emit goals to build/scale `Inventor` (then `Observatory`) when science is the gate; only emit science-gated `build_*` goals once `unlockable_now` includes them. Encode the `tech_tree.recommended_order` as a tiebreak.
- **3c Well-being for growth:** in GROW phase, emit well-being building goals (per `needs.json` wellbeing_needs: Social Life→Campfire, Aesthetics→decor, etc.) and housing tiers until pop climbs; ensure free beds > 0 (breeding requires an empty bed).
- **3d Power:** emit a power-building goal when `power_deficit>0` before/with any powered production building.
- **3e Storage & drought:** emit pile/warehouse goals on `storage_pressure`; in DROUGHT_PROOF emit tank/levee/floodgate goals until `drought_prep` deficit ≤0.

- [ ] **Step 1 (per 3a–3e):** Write the failing test in `agent/test_economy.py` for that subsystem's helper (e.g. a state consuming planks with no LumberMill → `needed_producers` includes `LumberMill`).
- [ ] **Step 2:** Run it, see it fail.
- [ ] **Step 3:** Implement the helper in `economy.py` + wire the goal into `planner.analyze`/`candidates_for`.
- [ ] **Step 4:** Run `python3 -m pytest agent/test_economy.py -q` (or unittest) — expect PASS, and `python3 -m unittest agent.test_planner` still green.
- [ ] **Step 5:** Commit `feat(planner): <subsystem> goals`.

### Task 4: Point the model at the DB-driven schema

**Files:** Modify `agent/nlp/features.py` (or `labeler.py`/`dataset.py`) to source `ACTIONS` and `feature_strings` from `agent/game_schema.py`; Modify `agent/nlp/labeler.py` so the oracle can return any of the 83 goal_ids.

**Interfaces — Consumes:** `game_schema.actions()`, `game_schema.feature_strings`, extended `planner`.

- [ ] **Step 1:** Replace `features.ACTIONS` with `game_schema.actions()` and `features.feature_strings` with `game_schema.feature_strings` (keep the `StateFeaturizer` wrapper API). Update `labeler.Oracle.label` to select over the full goal set via the extended `build_safe_ready_frontier`.
- [ ] **Step 2:** `python3 -c "from agent.nlp import features; print(len(features.ACTIONS))"` → 83. Existing `agent/test_planner.py` still green.
- [ ] **Step 3:** Commit `refactor(nlp): model uses DB-driven schema`.

### Task 5: Regenerate the dataset over the full economy and retrain

**Files:** Modify `agent/nlp/dataset.py` (broaden the synthetic state sweep to cover mid/late-game: production buildings present, science levels, well-being, power, drought states). Run on Mac; train on GPU (Part B).

- [ ] **Step 1:** Broaden `dataset._iter_states` and `_STAGES` to include economy stages (LumberMill/GearWorkshop/Inventor/farm-chain/power/storage present at varying resource levels). Regenerate: `python3 -m agent.nlp.dataset` → new `decision_dataset.json` (expect ≫9,869 rows across ≫10 labels).
- [ ] **Step 2:** Sanity: every `game_schema.actions()` build goal that the expert can emit appears in the dataset label distribution (log any with 0 examples — those are expert blind spots to fix in Task 3).
- [ ] **Step 3:** Train on GPU (Part B), pull models back, verify `DecisionPolicy.load()` + per-class recall (`python3` fidelity script pattern from `agent/journal`/session).
- [ ] **Step 4:** Commit `feat: full-economy dataset + retrained model`.

### Task 6: Wire curriculum + replay into the play loop

**Files:** Modify `agent/nlp/play_policy.py`.

**Interfaces — Consumes:** `curriculum.bias_ranking`, `curriculum.is_goal_reached`, `replay.record_step`.

- [ ] **Step 1:** In the cycle: after `policy.rank`, apply `curriculum.bias_ranking(state, ranked)`; call `replay.record_step(run_id, cycle, state, chosen_intent, meta)`; stop the run early when `curriculum.is_goal_reached(state)`.
- [ ] **Step 2:** Dry-run against the live game for ~10 cycles; confirm records land in `agent/runs/<id>.jsonl` and phase biasing changes selections vs raw policy.
- [ ] **Step 3:** Commit `feat(play): curriculum biasing + run recording`.

### Task 7: The run-to-run learning loop

**Files:** Create `agent/nlp/learn.py`; Create `agent/run_loop.py` (orchestrator: play → summarize → relabel → retrain → repeat).

**Interfaces — `learn.py` Produces:**
- `examples_from_run(run_id) -> [{features,label,source,weight}]` (via `replay.credit_assignment` + `game_schema.feature_strings`).
- `build_augmented_dataset(run_ids, base=..., out=...) -> {base_rows,added_rows,overridden,total_rows,out_path}` — corrected labels OVERRIDE base labels for the same feature vector.
- `retrain_command(host="cka-win") -> str` (the Part B sync+train+pull sequence).

`run_loop.py`: N iterations of `play_policy.run` → `replay.summarize_run` → if failure, `learn.build_augmented_dataset` → retrain (Part B) → reload model → next run; log the metric trend (`days_survived`, `peak_pop`).

- [ ] **Step 1:** Write `agent/nlp/learn.py` + `unittest` (stub `replay.credit_assignment`; assert corrected examples override conflicting base labels). `python3 agent/nlp/learn.py` — OK.
- [ ] **Step 2:** Write `agent/run_loop.py`; do ONE real iteration (play a bootstrap, record, relabel, retrain, reload) and confirm the model file changed and loads.
- [ ] **Step 3:** Commit `feat: run-to-run learning loop`.

### Task 8: Drive to the goal + tune

Not code — an execution/verification task. Run `agent/run_loop.py` for many iterations, watching `curriculum` phases advance. Expected failure modes to fix as they appear (each becomes a small planner/placement/economy patch + commit):
- Population won't grow → check free beds + well-being coverage (Task 3c); breeding needs an empty bed + positive well-being.
- Drought death → `drought_prep` under-buffered or pump intake dries out; add reservoir engineering (dam/levee) goals.
- Placement unreachable → building boxed by resources/water; the trunk reports `unreachable`, fall to next candidate (already supported) — improve the candidate scorer to avoid boxed tiles.
- Real-time too slow to breed to 30 → raise `time_manager` speeds / advance larger chunks when only waiting.

**Definition of done:** a run reaches `curriculum.is_goal_reached` (pop≥30, water_days & food_days > longest drought, homeless=0) and `run_loop` shows `days_survived`/`peak_pop` trending UP across iterations (the model measurably improves run-to-run).

---

## Self-Review notes
- Tasks 1–2 recover work three subagents attempted but didn't land this session (prompts + interfaces are specified here so they're reproducible).
- Task 3 is the true critical path and is intentionally the biggest; do 3a–3e as separate commits.
- The model (Tasks 4–5) is worthless ahead of the expert (Task 3) — **do Task 3 before Task 5's retrain**, or the dataset will have no mid/late-game labels.
- Everything the model outputs is validated at execution (placement feasibility, affordability, site caps), so a wrong model prediction degrades gracefully to the next ranked intent or advance_time — safe to iterate live.
