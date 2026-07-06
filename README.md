# timberborn-bot

A local, cheaper LLM that autonomously plays [Timberborn](https://timberborn.com) and improves across runs — not by retraining, but by growing an embedded, scored memory of lessons and build designs.

The agent perceives and acts through **TimberBridge**, a C# mod that hosts a digested HTTP/JSON API inside the game; a tiered local model stack (planner + reflex + embedder + offline coach) runs on Ollama; and a four-tier memory system turns each colony run into better play in the next.

## Status
Design + planning phase. Game installed on the Windows box; no code yet. Next concrete step is the Phase 0 spike (see below).

## Docs
| Doc | What |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | system overview — start here |
| [docs/PROJECT.md](docs/PROJECT.md) | phased build order, tests, risks, status |
| [docs/api-contract.md](docs/api-contract.md) | the mod↔agent HTTP/JSON seam |
| [docs/learning-system.md](docs/learning-system.md) | how it learns across runs (memory + design library + coach) |
| [docs/compute-and-models.md](docs/compute-and-models.md) | local Ollama stack, model choice, context/latency budget |
| [docs/agent/agent-design.md](docs/agent/agent-design.md) | the player-agent loop & tools |
| [docs/reference/modding-api.md](docs/reference/modding-api.md) | Timberborn modding framework reference |
| [docs/reference/confirmed-api.md](docs/reference/confirmed-api.md) | real game service signatures (v1.0.13.1, reflected) |
| [docs/knowledge/survival-basics.md](docs/knowledge/survival-basics.md) | survival strategy seed |
| [docs/kb/](docs/kb) | compact, token-cheap knowledge base the agent queries |

## Target environment
Windows 11, SSH `cka-win` · RTX 4060 Ti 16 GB · Ollama 0.24.0 · Timberborn v1.0.13.1 at `F:\SteamLibrary\steamapps\common\Timberborn`.
