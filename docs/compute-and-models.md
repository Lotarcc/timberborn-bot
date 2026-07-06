# Compute & model stack — local inference on the Windows box

The player agent runs on **local Ollama**, not the Claude API. Cheaper for an always-on game loop, private, and no per-call billing. The Mac reaches the Ollama server over the SSH tunnel (or the agent runs on the Windows box itself).

## Hardware (measured)
- GPU: **NVIDIA RTX 4060 Ti, 16 GB** (≈14.8 GB free at idle), driver 610.62.
- CPU: Intel i5-9600**KF** (6c/6t, **no integrated GPU**) · RAM: 32 GB · Ollama models on `F:\Ollama\models` (217 GB free).
- **The game renders on the same 4060 Ti** — the KF has no iGPU to offload display to, so Timberborn and any model on this box share one 16 GB pool. This is the binding constraint (see "Sharing the GPU").
- **Second node: M1 Pro MacBook** — Apple M1 Pro, 16 GB unified, 14-core GPU (Metal). Hosts the small auxiliary models (embedder, optional reflex) so they don't touch the contended Windows GPU. On the LAN (192.168.88.x); Ollama not yet installed there (setup step).
- Ollama `0.24.0`, API at `127.0.0.1:11434`, runs as the tray app (no Windows service).

## The critical config finding
The server shipped with `OLLAMA_NUM_PARALLEL=4`. Ollama reserves KV cache for *every* parallel slot, so a request at `num_ctx=8192` really allocates ~4×8192 of KV. That pushed a 14B off the GPU onto the CPU → **4 tok/s** (the "14B is too slow" mirage). Flash attention and KV quantization were both off.

**Target server config for a single agent:**
```
OLLAMA_NUM_PARALLEL=1          # one agent → reclaim 4× KV cache
OLLAMA_FLASH_ATTENTION=1       # required for KV cache quantization to apply
OLLAMA_KV_CACHE_TYPE=q8_0      # ~½ KV memory, negligible quality loss
OLLAMA_KEEP_ALIVE=30m          # keep the planner + support models hot
```
Restart the server after setting these (kill `ollama app.exe`/`ollama.exe`, relaunch with the env applied). This is what lets a 14B hold 32k context fully on-GPU.

## Context requirement
Per-decision prompt budget:

| Component | Tokens |
|---|---|
| System prompt + cheat sheet (loop rules, tool list, guardrails) | ~2.5k |
| Tool/function schemas | ~0.8k |
| Digested state JSON | ~1.2k |
| Recent events | ~0.4k |
| Retrieved playbook lessons (top-k) | ~1.5k |
| Retrieved KB entries (1–3) | ~2.5k |
| Retrieved design(s) | ~1.0k |
| Compacted episodic history | ~3–5k |
| Generation | ~0.5–1k |

Typical ≈10–13k, spikes ≈16k. **Floor = 16k, target = 32k** (`num_ctx=32768`). Above 32k is wasted KV for this task.

## Latency budget
Quality-first: **up to ~30–60 s per hard decision is acceptable.** At 32k context this is comfortably met by anything ≥15 tok/s that stays on-GPU. Routine ticks route to the reflex model and return in ~1 s.

## The model ensemble (two nodes — see topology below)
| Role | Model | ~Size | Node | Purpose |
|---|---|---|---|---|
| Planner | `qwen2.5:7b` @ 32k | ~7.3 GB | Windows GPU (shares with game) | reason over state, pick actions & designs |
| Reflex / router | code-based (or small `qwen2.5:3b`) | ~0–2 GB | Mac / none | triage each tick; wake planner only when needed |
| Embedder | `bge-m3` / `nomic-embed-text` | 0.3–1.2 GB | Mac (Metal) | semantic retrieval over KB + playbook + design library |
| Offline coach | **Claude Code (Fable 5 / Opus 4.8, high–max effort) via Max subscription**; `qwen2.5:14b` fallback | — | between runs only | distill run logs → new lessons/designs (latency-irrelevant, quality-critical) |

Windows coexistence with `num_parallel=1` + q8 KV: game (5–6 GB reserved) + planner `qwen2.5:7b` (~7.3 GB) + desktop (~1 GB) ≈ 14 GB of 16. The aux models live on the Mac, so the Windows GPU only holds the game + planner.

**Reliability layer:** Ollama's schema-constrained decoding (`format` = JSON schema) forces every action to be valid JSON matching the `act` command enum — the planner physically cannot emit a malformed command. This is what makes even mid-size local models dependable in the loop.

## Final benchmark (2026-07-06, after the fix)
All at `num_ctx=32768`, `NUM_PARALLEL=1` + flash attention + q8 KV. gen tok/s measured server-side (nanosecond timers, SSH-latency-immune).

| model @ 32k | gen tok/s | prompt tok/s | VRAM | on GPU | native JSON |
|---|---|---|---|---|---|
| `qwen2.5:14b` | 18.1 | 3723 | 14.6 GB | 96% | slightly malformed |
| `mistral-nemo:12b` | **39.5** | 9858 | 11.6 GB | **100%** | clean |
| `qwen2.5:7b` | 59.7 | 13875 | 7.3 GB | 100% | clean |
| `llama3.1:8b` | 58.7 | 13209 | 9.0 GB | 100% | clean |

The fix moved a 14B from 4 → 18 tok/s. Those numbers assume the whole card is ours — but during play it isn't (see below).

## Sharing the GPU with the game (the real budget)
VRAM is split three ways, concurrently: Windows desktop, Timberborn rendering, and the models. Rough budget on the 16 GB card:

| Consumer | VRAM | Notes |
|---|---|---|
| Windows desktop / overhead | ~0.7–1 GB | dedicated box, keep it lean |
| Timberborn rendering | **5–6 GB reserved** | per operator; already at "Low" preset, resolution lowered to 1280×720 windowed. Measure actual and adjust |
| Models on Windows (must fit the rest) | **≤ ~9 GB** | leaves the game its reservation so it never gets starved → offloaded/stuttering |

Two things make this workable: (1) the agent runs the game at **minimal graphics** (no fidelity needed), shrinking its footprint; (2) the loop **pauses the game to think**, so while the planner uses GPU *compute*, the game is a static capped frame using almost none — the pausable design eases compute contention even though a paused frame still holds its VRAM.

Ordering matters: keep the Windows model footprint small enough that even if it loads first, the game can still claim its 2–3.5 GB.

## Distributed topology (two nodes)
Offload the small, always-on auxiliary models to the MacBook so the Windows GPU holds only the game + the planner.

```
Windows (4060 Ti 16GB)                 M1 Pro Mac (16GB unified)
  Timberborn (minimal graphics)          Ollama/Metal: embedder (+ optional reflex 3B)
  TimberBridge mod  (HTTP :7744)         Claude Code = offline coach
  Ollama: PLANNER  (HTTP :11434)         agent orchestrator (dev) — or run it on Windows
        └── game + planner share VRAM          │
                    ▲                           │  LAN 192.168.88.x
                    └──── SSH tunnels ──────────┘  (bridge :7744, Windows Ollama → :11435;
                                                    Mac Ollama local :11434)
```
The hot path (read state → planner → act) is colocated on Windows; only embedding retrieval crosses the LAN. Fallback if the Mac is off: run the embedder on the Windows CPU.

## Decision (roles)
| Role | Model | Where | Why |
|---|---|---|---|
| **Planner (default)** | `qwen2.5:7b` @ 32k (~7.3 GB) | Windows GPU | fits alongside the game with room; 59 tok/s; clean JSON |
| **Embedder** | `bge-m3` / `nomic-embed-text` | **Mac (Metal)** | constant retrieval, off the contended GPU |
| **Reflex / router** | code-based triage (or small `qwen2.5:3b` on Mac) | Mac / none | "no alerts + healthy buffers → advance" is a rule; a Mac model only if judgment needed |
| **Hard-call escalation** | frontier Claude (pause game, consult) | cloud | a bigger *local* model can't coexist with the game; the game is pausable |
| **Offline coach** | Claude Code (Fable 5 / Opus 4.8, high–max) via Max sub | Mac (cloud) | between-run retrospectives; `qwen2.5:14b` on Windows as unattended fallback |

All action outputs use Ollama schema-constrained decoding (`format`) so the 7B can't emit an invalid command. With 5–6 GB reserved for the game, the Windows planner budget is ~9 GB, so `qwen2.5:7b` is the fit; a 12B only if the game measures well under its reservation. Decide after measuring actual game VRAM in-play.

## Server ops (how Ollama is run on the box)
Config persisted via `setx` (`OLLAMA_NUM_PARALLEL=1`, `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`, `OLLAMA_KEEP_ALIVE=30m`). The tray app won't launch into the interactive desktop over SSH; restart the server detached with `Win32_Process.Create("cmd /c \"set OLLAMA_*=...&& ollama.exe serve\"")` (survives the SSH session, runs headless in session 0, GPU compute unaffected). For permanent unattended use, promote to a scheduled task / service.

Sources: [Ollama KV cache quantization](https://smcleod.net/2024/12/bringing-k/v-context-quantisation-to-ollama/), [local LLM tool-calling eval 2026](https://www.jdhodges.com/blog/local-llms-on-tool-calling-2026-pt1-local-lm/).
