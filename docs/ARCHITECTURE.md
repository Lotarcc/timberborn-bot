# Architecture

Make a local, cheaper LLM autonomously play Timberborn and get better across runs. Four subsystems, one seam.

```
   Windows box (cka-win, RTX 4060 Ti 16GB)          │  reachable from the Mac over SSH
                                                     │
  ┌───────────────────────────────────────────┐     │
  │ Timberborn v1.0.13.1                        │     │
  │   └─ TimberBridge mod (C#, in-process)      │     │
  │        HTTP/JSON on localhost:PORT          │◄────┼──── SSH tunnel ────┐
  │        /state /map /events /act /ping       │     │                    │
  └───────────────────────────────────────────┘     │                    │
                                                     │            ┌───────┴─────────────┐
  ┌───────────────────────────────────────────┐     │            │ Player agent (loop) │
  │ Ollama server (localhost:11434)             │◄────┼────────────┤  planner + reflex   │
  │   planner 14B · reflex 3B · embedder        │     │            │  + memory + coach   │
  └───────────────────────────────────────────┘     │            └─────────────────────┘
                                                     │            agent may run on either host
  Memory (files + embeddings): KB · playbook · design library · run journal
```

## 1. TimberBridge — the mod (observe/act surface)
A C# mod inside the game hosting an HTTP/JSON server. Built on official mod support: `IModStarter` entry, Bindito DI (`IConfigurator` in `[Context("Game")]`), a singleton that is `ILoadableSingleton` (starts the listener) + `IUpdatableSingleton` (drains a request queue on the Unity main thread each frame). Reads via `EntityComponentRegistry.GetEnabled()` + the confirmed water/weather/needs/time services; writes via `BlockObjectFactory` (validated by `BlockValidator`). Returns **digested** state and **teaching errors**, never raw dumps. Details: `docs/reference/modding-api.md`, `docs/reference/confirmed-api.md`, `docs/api-contract.md`.

## 2. Player agent — the loop
Paused-between-turns loop: read digested `/state` → check alerts/events → (reflex triages; planner decides when it matters) → retrieve lessons/designs from memory → emit validated `/act` commands → `advance` a bounded time → repeat. Pausing removes real-time pressure, making the task tractable (and cheap) for a local model. Tool surface and prompt structure: `docs/agent/agent-design.md`.

## 3. Model stack — local inference
Tiered ensemble on Ollama: a smart **planner** (14B-class, 30–60 s budget), a fast **reflex/router** (3B or code) for routine ticks, an **embedder** for memory retrieval, and an offline **coach** (reuse planner or Claude API) that runs between runs. Schema-constrained decoding guarantees valid actions. Config, VRAM math, context budget, and benchmarks: `docs/compute-and-models.md`.

## 4. Memory & learning — improvement without weight updates
Four tiers — static **KB** (`docs/kb/`), growing **playbook** of lessons, a scored **design library**, and an append-only **run journal**. The coach distills each run into new lessons and design variants; an evaluator A/B-tests designs from checkpoint saves to find best-or-near-best layouts. The curve (droughts survived, cycle reached, buffers) trends up at flat inference cost. Full design: `docs/learning-system.md`.

## Data flow, one decision
1. Agent `GET /state` → digested snapshot + alerts.
2. Reflex model (or code) triages: no alerts + healthy buffers → `advance` and loop; else wake planner.
3. Planner embeds a query from the alerts → retrieves top-k lessons + best designs from memory.
4. Planner emits `act` commands (schema-constrained); bridge validates, applies, or returns a teaching error to retry.
5. Loop appends the decision + outcome to the run journal.
6. Between runs, the coach turns the journal + final metrics into memory updates.

## Key constraints carried across the design
- Internal `Timberborn.*` types aren't a stable contract → pin to v1.0.13.1, startup self-check.
- `load` reloads the scene → tears down the bridge singleton → agent reconnects via `/ping`.
- Unity main-thread rule → background listener enqueues; frame hook executes and bounds work per frame.
- 16 GB VRAM → `num_parallel=1` + q8 KV cache is what makes a 14B viable at 32k context.
