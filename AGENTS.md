# AGENTS.md — working guidelines

Guidelines for any AI agent (or human) working in this repo. Read `README.md` and `docs/ARCHITECTURE.md` first for what the project is.

## What this project is
A local, cheaper LLM that autonomously plays Timberborn (v1.0.13.1) and improves across runs via scaffolded memory — not weight updates. Four subsystems: the `TimberBridge` C# mod (observe/act HTTP API inside the game), the player-agent loop, a tiered local-model stack on Ollama, and a four-tier learning memory. `docs/PROJECT.md` is the source of truth for phases and status.

## Current mode (2026-07, autonomous build)
Phase 0 done; Phase 1 `/state` returns real digested game data. **Iteration happens on the Mac**: Timberborn runs locally on the M1 Pro (shell launch/kill via `open`/`pkill`, no RDP/SSH friction), while the models run on the Windows box (its 4060 Ti is now free of the game). The `TimberBridge` DLL is identical on both hosts (`netstandard2.1`, `-p:TimberbornManaged=<host Managed path>`).

Working principles for this phase — **follow these**:
- **Don't disturb the operator's screen.** Run the game windowed and, where possible, backgrounded / on a separate Space so they can work while I do. Interact through the bridge over HTTP; never screen-control for routine work. Use computer-use only as a genuine last resort (e.g. one dialog click).
- **Batch mod changes; reload sparingly.** A reload closes/relaunches the game — group changes so it happens rarely.
- **Commit + push after every meaningful step.** Keep the public repo current. Keep infra/secrets (paths, account names, IPs) in local agent memory, never in the repo.
- **Parallelize with subagents** for decomposable research/drafting.
- **Prefer the game's own APIs** (reflected/decompiled, called in-process) over synthesized input or pixel-pushing — reliable and non-intrusive.

Dev reload loop: edit mod → `mac-reload.sh` (build → deploy → autoload marker → `open` launch → poll → `/state`). New colonies start via `GameSceneLoader.StartNewGameInstantly(factionId, mapRef, settlementName)`; existing saves via `StartMostRecentSaveInstantly()`.

## Doc map (keep these current)
- `docs/PROJECT.md` — phased plan, tests, risks, **current status**. Update status when a phase moves.
- `docs/ARCHITECTURE.md` — system overview.
- `docs/api-contract.md` — the mod↔agent HTTP/JSON seam. Changing `/state` or `/act` means editing this first.
- `docs/learning-system.md` — memory tiers, design library, coach loop.
- `docs/compute-and-models.md` — local model stack + Ollama settings.
- `docs/reference/confirmed-api.md` — real game service signatures (reflected). `docs/reference/modding-api.md` — modding framework.
- `docs/kb/` — the compact knowledge base the agent queries. `docs/knowledge/survival-basics.md` — strategy seed.

## Conventions
- **Terminology (use exactly):** `TimberBridge` (the mod), `player agent` (the loop), `planner`/`reflex`/`embedder`/`coach` (the model roles), `digested state` (pre-computed snapshot), and the memory tiers: `KB`, `playbook`, `design library`, `run journal`.
- **Digested over raw.** The bridge computes days-remaining, alerts, and teaching errors in C#. Never make the model do spatial math or parse raw entity dumps.
- **Pin and self-check.** Internal `Timberborn.*` types are not a stable contract. Target v1.0.13.1; verify names against `confirmed-api.md`; add a startup self-check that logs loudly on a missing service.
- **Single-agent Ollama.** Serve with `OLLAMA_NUM_PARALLEL=1` + `OLLAMA_FLASH_ATTENTION=1` + `OLLAMA_KV_CACHE_TYPE=q8_0`. Actions use schema-constrained decoding so they always parse.
- **KB style:** one concept per file, tables + imperative rules, token-cheap. Mark unverified numbers `(v?)` until reconciled against the `/blueprints` dump.

## Testing philosophy
- **Checkpoint-save replay** is the core tool: save at known-hard moments and replay the agent from them for near-deterministic comparison. This set is both the design evaluator and the regression suite.
- The save file (`.timber` = zip of world JSON) is the oracle for `/state` correctness.
- **No silent caps** — log every truncation/dropped variant so "we covered everything" is never assumed.

## Git discipline
- **Commit often, in small logical units.** Push after each meaningful step so progress is trackable.
- Present tense, imperative subject lines (e.g. `add /state schema`, `benchmark 14B at 32k`).
- End commit messages with the `Co-Authored-By` trailer for the assisting agent.
- **Never commit secrets or infra.** No IPs, hostnames, SSH keys/paths, tokens, or account identifiers. Connection details for the Windows box live in the operator's local agent memory, outside this repo. `.env` and key files are git-ignored — keep it that way.

## Safety
- The bridge binds localhost only; the SSH tunnel is the trust boundary.
- Hard-to-reverse or outward-facing actions (publishing, deleting, remote launches) need explicit human authorization unless already durably granted.
