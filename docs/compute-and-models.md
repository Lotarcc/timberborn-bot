# Compute & model stack — local inference on the Windows box

The player agent runs on **local Ollama**, not the Claude API. Cheaper for an always-on game loop, private, and no per-call billing. The Mac reaches the Ollama server over the SSH tunnel (or the agent runs on the Windows box itself).

## Hardware (measured)
- GPU: **NVIDIA RTX 4060 Ti, 16 GB** (≈14.8 GB free at idle), driver 610.62.
- CPU: Intel i5-9600KF (6c/6t) · RAM: 32 GB · Ollama models on `F:\Ollama\models` (217 GB free).
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

## The model ensemble
| Role | Model | ~Size | Residency | Purpose |
|---|---|---|---|---|
| Planner | benchmark winner (candidate `qwen2.5:14b`) | ~9 GB | hot during hard decisions | reason over state, pick actions & designs |
| Reflex / router | `qwen2.5:3b` (or pure code) | ~2 GB | always hot | triage each tick; wake planner only when needed |
| Embedder | `nomic-embed-text` (have) / `bge-m3` (A/B) | 0.3–1.2 GB | always hot | semantic retrieval over KB + playbook + design library |
| Offline coach | **Claude Code (Fable 5 / Opus 4.8, high–max effort) via Max subscription**; `qwen2.5:14b` fallback | — | between runs only | distill run logs → new lessons/designs (latency-irrelevant, quality-critical) |

Coexistence in 16 GB with `num_parallel=1` + q8 KV: planner ~11–12 GB (weights + 32k KV) + embedder + 3B ≈ 14 GB. Keep the embedder + reflex resident; planner is the big consumer.

**Reliability layer:** Ollama's schema-constrained decoding (`format` = JSON schema) forces every action to be valid JSON matching the `act` command enum — the planner physically cannot emit a malformed command. This is what makes even mid-size local models dependable in the loop.

## Final benchmark (2026-07-06, after the fix)
All at `num_ctx=32768`, `NUM_PARALLEL=1` + flash attention + q8 KV. gen tok/s measured server-side (nanosecond timers, SSH-latency-immune).

| model @ 32k | gen tok/s | prompt tok/s | VRAM | on GPU | native JSON |
|---|---|---|---|---|---|
| `qwen2.5:14b` | 18.1 | 3723 | 14.6 GB | 96% | slightly malformed |
| `mistral-nemo:12b` | **39.5** | 9858 | 11.6 GB | **100%** | clean |
| `qwen2.5:7b` | 59.7 | 13875 | 7.3 GB | 100% | clean |
| `llama3.1:8b` | 58.7 | 13209 | 9.0 GB | 100% | clean |

The fix moved a 14B from 4 → 18 tok/s, but at 32k it sits at 14.6 GB (96% GPU) — no room for the embedder + reflex to stay resident, and its raw JSON was slightly off.

## Decision (roles)
| Role | Model | Why |
|---|---|---|
| **Planner (default)** | `mistral-nemo:12b` | 39.5 tok/s, 100% on-GPU at 32k, clean JSON, 4.4 GB headroom keeps the ensemble hot |
| **Escalation planner** | `qwen2.5:14b` | smartest *local* model; hard/novel mid-run decisions; also the unattended coach fallback |
| **Offline coach** | Claude Code (Fable 5 / Opus 4.8, high–max effort) via Max subscription | between-run retrospectives — frontier quality, no per-token cost, latency irrelevant |
| **Reflex / router** | `qwen2.5:3b` | routine ticks in ~1 s |
| **Embedder** | `bge-m3` (primary), `nomic-embed-text` (light fallback) | retrieval quality drives "recall the right lesson" |

All action outputs use Ollama schema-constrained decoding (`format`), so even the mid-size planner can't emit an invalid command. Fast fallback if headroom is ever needed: `qwen2.5:7b`.

## Server ops (how Ollama is run on the box)
Config persisted via `setx` (`OLLAMA_NUM_PARALLEL=1`, `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`, `OLLAMA_KEEP_ALIVE=30m`). The tray app won't launch into the interactive desktop over SSH; restart the server detached with `Win32_Process.Create("cmd /c \"set OLLAMA_*=...&& ollama.exe serve\"")` (survives the SSH session, runs headless in session 0, GPU compute unaffected). For permanent unattended use, promote to a scheduled task / service.

Sources: [Ollama KV cache quantization](https://smcleod.net/2024/12/bringing-k/v-context-quantisation-to-ollama/), [local LLM tool-calling eval 2026](https://www.jdhodges.com/blog/local-llms-on-tool-calling-2026-pt1-local-lm/).
