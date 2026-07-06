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
| Offline coach | reuse planner, or Claude API | — | between runs only | distill run logs → new lessons/designs (latency-irrelevant) |

Coexistence in 16 GB with `num_parallel=1` + q8 KV: planner ~11–12 GB (weights + 32k KV) + embedder + 3B ≈ 14 GB. Keep the embedder + reflex resident; planner is the big consumer.

**Reliability layer:** Ollama's schema-constrained decoding (`format` = JSON schema) forces every action to be valid JSON matching the `act` command enum — the planner physically cannot emit a malformed command. This is what makes even mid-size local models dependable in the loop.

## Candidates & benchmark status
Installed/pulling: `llama3.1:8b` (have), `qwen2.5-coder:14b` (have), `qwen2.5:7b`, `mistral-nemo:12b` (128k-capable), `qwen2.5:14b`, `qwen2.5:3b`, `bge-m3`, `nomic-embed-text` (have).

Preliminary (default config, **pre-fix** — not final):
| Model | max ctx | gen tok/s | prompt tok/s | note |
|---|---|---|---|---|
| `llama3.1:8b` | 131k | **56** | 9776 | 100% GPU at 8k |
| `qwen2.5-coder:14b` | 32k | 4 | 2527 | CPU-offloaded by NUM_PARALLEL=4 |

Final matrix (after server reconfig) benchmarks every candidate at 16k & 32k for gen tok/s, GPU residency, and JSON-schema adherence, then picks the planner (+ fast fallback for the tiered loop). Expectation: `qwen2.5:14b` becomes viable on-GPU within the 30–60 s budget; `qwen2.5:7b`/`llama3.1:8b` remain the fast fallback.

Sources: [Ollama KV cache quantization](https://smcleod.net/2024/12/bringing-k/v-context-quantisation-to-ollama/), [local LLM tool-calling eval 2026](https://www.jdhodges.com/blog/local-llms-on-tool-calling-2026-pt1-local-lm/).
