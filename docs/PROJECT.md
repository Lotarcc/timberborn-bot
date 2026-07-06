# Project plan — timberborn-bot

Make a local, cheaper LLM autonomously play Timberborn v1.0.13.1 and measurably improve across runs. Read `docs/ARCHITECTURE.md` first for the system; this file is the build order, tests, and risks.

## Guiding principles
- **Push intelligence down.** The bridge computes days-remaining, alerts, and teaching errors in C# so a cheap model reads dashboards and applies rules.
- **Vertical slices.** Every phase is independently testable and useful on its own.
- **Outcome-grounded.** Learning is proven by metrics trending up at flat inference cost, tested from fixed checkpoint saves.
- **Pin and self-check.** Internal game types aren't stable; hard-pin v1.0.13.1 and fail loudly on missing services.

## Phases

### Phase 0 — Foundations & spike
Goal: prove the whole chain end-to-end with the thinnest possible mod.
- Dev env: modding template building a "hello world" mod that loads in v1.0.13.1; decompile the GAP services (`docs/reference/confirmed-api.md`).
- Stand up the in-process HTTP server (`ILoadableSingleton` + `IUpdatableSingleton` main-thread queue) returning `/ping`.
- Open the SSH tunnel; confirm the Mac can reach it.
- Reconfigure Ollama (`num_parallel=1`, flash attn, q8 KV); finish the benchmark matrix and lock the planner model.
- **Exit (MET 2026-07-06):** ✅ `curl` through the SSH tunnel (Mac → Windows game) returns `{"ok":true,"bridge_version":"0.1.0","game_version":"1.0.13.1","in_game":true}`; 404s handled. Planner benchmarked (`qwen2.5:7b`, 59 tok/s on-GPU at 32k).
- Learnings: server uses `TcpListener` (HttpListener needs URL-ACL). Drain the request + graceful `Shutdown` before close, else some clients see an RST. Mod is a plain DLL + manifest in the user's `Documents/Timberborn/Mods/` (auto-loaded).

### Phase 1 — Observation (read-only)
Goal: the full colony situation readable from JSON alone.
- Implement `/state` (digested: time, weather forecast, population distress, resources as days-remaining, water sites, buildings, alerts), `/map`, `/events`, `/blueprints`.
- Reconcile KB `(v?)` numbers against the `/blueprints` dump.
- **Exit:** side-by-side, `/state` matches what's on screen across a wet season and a drought; `/blueprints` exported and KB numbers corrected. Delivers a read-only "coach mode" already.
- Test: scripted `curl` snapshots at known moments vs manual observation; the save file as an oracle.

### Phase 2 — Action
Goal: drive a base entirely via JSON.
- Implement `/act`: place/demolish, area designations, priorities, pause building, speed, `advance`, save/load — each validated with teaching errors.
- Verify `load` reconnect path (scene reload → bridge rebind → `/ping`).
- **Exit:** build a working starter base (pump, tanks, housing, farm) by hand-issued `curl` commands and survive one drought; every invalid command returns an actionable suggestion.
- Test: per-command idempotence scripts; a "build the starter base" scripted run.

### Phase 3 — Player agent (the MVP)
Goal: the loop survives the first drought unaided.
- Agent SDK loop against the tools (`docs/agent/agent-design.md`); planner + reflex; KB v1 + a seeded playbook; schema-constrained actions.
- Target a forgiving start (Folktails, temperate, short first drought).
- **Exit / MVP definition:** on an easy map, the agent survives its first drought without human help in ≥60% of runs, with no malformed commands reaching the game.
- Test: N automated runs; survival-to-cycle-2 rate; decision-log review.

### Phase 4 — Learning loop
Goal: the agent gets better run over run.
- Run journal; offline coach distills lessons; design library with the save/reload A/B evaluator; embedded retrieval of lessons/designs.
- Map-aware construction via `/map`; badtide/contamination handling.
- **Exit:** across ≥20 runs, survival-to-cycle-N and water/food buffer days trend up at flat inference cost; the evaluator promotes at least one water-storage and one farm design that beat the seeds.
- Test: the curriculum ladder (`docs/learning-system.md`); checkpoint-save regression suite (a stage-3 gain must not regress stage 1).

### Phase 5 — Robustness & autonomy
Goal: long unattended play.
- Checkpointing across sessions, remote launch (scheduled task on the logged-in session), a metrics dashboard, harder maps/factions (Iron Teeth, badtide maps).
- **Exit:** an unattended multi-cycle run completes and self-recovers from a mid-run save/load; dashboard shows the learning curve.

## Testing strategy (cross-cutting)
- **Checkpoint-save replay is the core tool.** Save at known-hard moments ("3 days to drought, understocked"; "badtide incoming"); replay the agent from them repeatedly for near-deterministic comparison. This set is both the design evaluator and the regression suite.
- **Save file as oracle** for `/state` correctness (the `.timber` is a zip of world JSON).
- **Bridge**: `curl` scripts per endpoint; a golden `/state` diff; startup self-check asserting every required service resolved.
- **Agent**: run-based metrics (survival to cycle N, population, well-being, buffer days), plus human review of decision logs on failures.
- **No silent caps**: log every truncation/dropped-variant so "covered everything" is never assumed.

## Metrics (the learning curve)
Droughts survived · cycle reached · peak/final population · final well-being · water & food buffer-days at each drought · build-cost efficiency · malformed-command rate (must stay ~0) · tokens & seconds per decision.

## Risk register
| Risk | Impact | Mitigation |
|---|---|---|
| Internal types not a stable contract | mod breaks on game update | pin v1.0.13.1; startup self-check; isolate access behind adapters |
| `load` tears down bridge singleton | lost connection mid-run | stop/rebind listener on scene change; agent `/ping` reconnect |
| Main-thread stalls/deadlocks | sim hitches / hangs | cap work per frame; bound every wait with a timeout; fail pending jobs on teardown |
| Inventory/district services still GAP | `/state` incomplete | Phase 0 decompile; fall back to component enumeration |
| Water-physics planning is the deepest skill | slow to get good at dams | seed designs from KB; let the evaluator improve them; start on easy maps |
| False learning from noise | promotes bad designs | require n≥3 samples + margin; keep superseded history |

## Current status (2026-07-06)
- **Phase 0 complete.** TimberBridge mod loads in Timberborn v1.0.13.1, serves `/ping` over the tunnel end-to-end. Docs + KB written; models pulled; Ollama reconfigured.
- Compute: two-node split (Windows planner `qwen2.5:7b` + game; Mac aux models). Game measured at **~2.3 GB VRAM** in a loaded save (well under the 5–6 GB reservation) → a larger planner may be reconsidered later.
- Confirmed most game services via reflection; inventory + districts remain GAP (Phase 1 decompile).
- Next: **Phase 1 — Observation**: main-thread request marshalling (`IUpdatableSingleton`) + `/state`, `/map`, `/events`, `/blueprints`. Requires a game restart to load the rebuilt DLL.
