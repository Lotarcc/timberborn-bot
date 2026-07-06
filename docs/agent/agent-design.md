# Player Agent Design — timberborn-bot

> Design for the player agent (Claude Agent SDK). Default model `claude-haiku-4-5-20251001`, low effort; escalate to `claude-sonnet-5` when flagged.

## 0. Scope & premise

The player agent is a Claude loop running on a Mac that plays Timberborn by talking to the **TimberBridge** C# mod (an HTTP+JSON server inside the game on a Windows box, reached over an SSH tunnel at `localhost:PORT`). The expensive reasoning was done once by a big model that authored the **knowledge base (KB)**, the **playbook** structure, and the bridge. The player agent does the cheap part every turn: read a **digested state**, check alerts, consult a rule, issue a few **validated actions**, advance a bounded amount of game time, repeat.

Design principle throughout: **push intelligence into the bridge and the KB** so the cheap model reasons minimally. The bridge validates actions and returns *teaching* errors; the KB holds distilled rules; the agent is a dispatcher, not a strategist.

Timberborn is a colony-survival builder. A **run** is one colony playthrough; a **cycle** is one wet/dry weather period. Beavers need water, food, and shelter; droughts (dry cycles) are the recurring survival threat. This maps cleanly onto a turn-based control loop because the game can be paused between every decision.

---

## 1. The decision loop

The agent operates the game as a **turn-based** problem, not a real-time one. Between decisions the game is **paused**, so there is no wall-clock pressure: the model can take 10 seconds or 3 minutes to think and the colony state is frozen. This is what makes a cheap model viable — Haiku doesn't have to react fast, it has to react *correctly* to a static snapshot.

### Loop steps

```
                        ┌─────────────────────────────────────┐
                        │  (run start) playbook_read + load KB │
                        │            cheat sheet               │
                        └──────────────────┬───────────────────┘
                                           ▼
   ┌──► 1. ensure paused ─────────► act{pause}  (idempotent; no-op if paused)
   │    2. GET digested state ────► get_state
   │    3. check alerts/events ───► get_events(since=cursor)
   │    4. any alert / novel? ────► kb_lookup(topic)   (only if needed)
   │    5. decide batch ──────────► (model picks 1–5 actions from rules)
   │    6. POST actions ──────────► act{...} × N   (each validated)
   │    7. handle teaching errors ► retry corrected action, or skip
   │    8. advance bounded time ──► act{set_speed} + act{resume},
   │                                then act{pause} after Δ days
   └────  9. loop to 1
```

1. **Ensure paused.** `act{command:"pause"}` is idempotent. Guarantees the state read in step 2 is consistent with the actions issued in step 6.
2. **GET digested state.** `get_state` returns a decision-ready snapshot (aggregates, days-remaining, alerts) — never raw entity dumps.
3. **Check events.** `get_events(since=cursor)` returns what changed (births, deaths, drought start, building completed) since the last decision. Cheap delta, keeps the model oriented without re-reading everything.
4. **Consult rules only when needed.** If an alert fired or the situation is novel, `kb_lookup(topic)` fetches one small rule file. Routine turns skip this — the always-in-context cheat sheet covers the common cases.
5. **Decide a small batch.** The model chooses **1–5 actions**. Small batches bound blast radius (§7) and keep the model from over-planning.
6. **POST actions.** Each `act` call is validated by the bridge. A bad action returns a teaching error (§2), not a silent failure.
7. **Handle teaching errors.** On error, the model reads the suggested fix ("nearest free tile Y") and retries once, or skips the action and moves on.
8. **Advance bounded game time.** `act{set_speed:2}` + `act{resume}`, then the bridge auto-pauses after a bounded Δ (default **1 in-game day**, tightened to a fraction of a day near a drought boundary or after a risky move). The agent then loops.

### Why pausing makes this tractable for a cheap model

- **No real-time pressure.** The model's latency (seconds) is decoupled from game time. A slow, cheap model plays exactly as well as a fast one.
- **Consistent reads.** State can't drift between observe and act — the snapshot the model reasons over is the snapshot its actions apply to.
- **Bounded lookahead.** Advancing one day at a time means the model only ever reasons about the *next* short horizon, not a full cycle. Errors surface within one day and are cheap to correct.
- **Determinism for learning.** A decision → outcome pair is cleanly attributable when nothing else happened in between, which is what makes the retrospective loop (§5) able to assign credit.

---

## 2. The tool surface

Every tool maps to one TimberBridge HTTP endpoint. Schemas are designed **for a cheap model**: small enumerated command sets, validated args, no big free-form fields, and error returns that teach the next action. Tools are declared with `strict: true` (guarantees `input` validates) and stable ordering (preserves prompt cache — see §3).

| Tool | HTTP endpoint | Purpose (one line) |
|---|---|---|
| `get_state` | `GET /state` | Digested, decision-ready colony snapshot. |
| `get_map` | `GET /map?bbox=...` | Terrain/water grid for a bounding box — construction planning only. |
| `get_events` | `GET /events?since=cursor` | What changed since a cursor (births/deaths/drought/etc). |
| `act` | `POST /act` | One validated command from a small enum. |
| `kb_lookup` | `GET /kb?topic=...` | Query the static knowledge base. |
| `playbook_read` | `GET /playbook` | Read evolving cross-run strategy notes. |
| `playbook_append` | `POST /playbook` | Append a structured lesson after a run/cycle. |

> `kb_lookup` / `playbook_read` / `playbook_append` may be served by the bridge or by a local file server on the Mac — the agent doesn't care. Keeping them as tools (not raw file reads) lets the bridge index/validate them and keeps the tool surface uniform.

### 2.1 `get_state` — digested snapshot

`GET /state`. No args. Returns aggregates and alerts, **not** entity lists.

```json
{
  "day": 34, "cycle": 3, "weather": "dry",
  "days_until_weather_change": 3,
  "population": { "adults": 12, "children": 4, "total": 16 },
  "wellbeing": { "avg": 11.2, "housing_free": 2 },
  "food": { "stored": 210, "days_remaining": 6, "net_per_day": -4 },
  "water": { "stored_days": 4, "net_per_day": -2, "sources": ["reservoir_a"] },
  "power": { "produced": 40, "demand": 32 },
  "storage": { "food_pct": 0.7, "water_pct": 0.55, "logs_pct": 0.4 },
  "buildings_paused": 1,
  "alerts": [
    { "id": "water_understocked", "severity": "high",
      "detail": "water 4 days stored, drought in 3 days" }
  ],
  "cursor": "evt_10432"
}
```

The `alerts` array is the bridge doing the model's triage for it. `cursor` is the opaque event cursor to pass to `get_events` next turn.

### 2.2 `get_map` — terrain/water grid (construction only)

`GET /map?bbox=x0,y0,x1,y1`. Used **only** when the model is about to place a building and needs geometry. Bounded bbox keeps the payload small; the model asks for the neighborhood around a proposed tile, not the whole map.

```json
// request: get_map { "bbox": [40, 40, 48, 48] }
{
  "bbox": [40,40,48,48],
  "legend": { "G":"ground", "W":"water", "B":"building", "R":"ramp", ".":"unbuildable" },
  "grid": [
    "GGGGWWWW",
    "GGGBWWWW",
    "GGG.WWWW",
    "GGGGGWWW"
  ],
  "z_hint": "row 0 = y=40 (north)"
}
```

### 2.3 `get_events` — delta since cursor

`GET /events?since=evt_10432`. Returns discrete events, newest-relevant first, plus the new cursor.

```json
{
  "events": [
    { "id": "evt_10440", "type": "drought_started", "day": 34 },
    { "id": "evt_10437", "type": "building_completed", "what": "water_pump", "at": [44,41] },
    { "id": "evt_10435", "type": "beaver_died", "cause": "thirst" }
  ],
  "cursor": "evt_10440"
}
```

### 2.4 `act` — one validated command

`POST /act`. The heart of the design. **One `command` field from a small enum**, with validated args. The bridge validates against live game state and returns a **teaching error** on failure.

Command enum (closed set — the model cannot invent commands):

| `command` | Args | Notes |
|---|---|---|
| `place_building` | `{ type, x, y, rotation? }` | `type` from a KB-known enum; bridge checks footprint/overlap/terrain. |
| `demolish` | `{ x, y }` | |
| `designate_area` | `{ action: "cut"\|"plant"\|"clear"\|"dig", bbox, resource? }` | e.g. plant `oak` in a bbox. |
| `set_priority` | `{ x, y, priority: 1-5 }` | Building work priority. |
| `pause_building` | `{ x, y, paused: bool }` | Halt/resume construction or operation. |
| `set_speed` | `{ speed: 0\|1\|2\|3 }` | 0 = paused. |
| `pause` / `resume` | `{}` | Idempotent convenience aliases for `set_speed`. |
| `save` | `{ slot? }` | Checkpoint before risky moves (§7). |
| `load` | `{ slot? }` | Roll back a bad decision. |

Success return:

```json
{ "ok": true, "command": "place_building",
  "result": { "placed": "water_pump", "at": [44,41], "cost": {"logs": 6} } }
```

**Teaching error return** — the single most important design element for a cheap model. The error states *why* and gives the *next action*:

```json
{ "ok": false, "command": "place_building",
  "error": {
    "code": "overlap",
    "message": "cannot place water_pump at [43,41]: overlaps Lodge footprint",
    "suggestion": { "nearest_free": [44,41], "reason": "adjacent, on water edge" }
  } }
```

Other teaching errors follow the same shape: `not_enough_resources` (returns shortfall), `unbuildable_terrain` (returns nearest buildable), `needs_water_adjacency` (returns nearest valid edge), `unknown_building_type` (returns closest valid enum values). The model reads `suggestion` and retries — it doesn't have to *reason out* the fix, the bridge hands it over.

### 2.5 `kb_lookup`, `playbook_read`, `playbook_append`

```json
// kb_lookup { "topic": "water_storage_sizing" }  -> returns one KB file (§4)
// playbook_read {}                               -> returns lessons array (§5)
// playbook_append { "lesson": { ...structured lesson... } }  -> { "ok": true, "id": "L-042" }
```

`kb_lookup` accepts a topic id or a short query the bridge matches against the KB index. `playbook_append` validates the lesson schema before writing.

---

## 3. Context budget

### Always-in-context cheat sheet (~2–3k tokens)

Placed in the **system prompt** (frozen, cached — see §7 and prompt-caching rules). It contains only what's needed every turn:

- **The core loop** (the 9 steps of §1, compressed to a numbered list).
- **Survival rules** — the ~15 imperative one-liners that prevent death: *"If water `stored_days` < `days_until_weather_change` + 2, build/expand water storage before anything else." "Never let food `days_remaining` drop below 3." "Keep ≥1 free housing slot per 6 adults."* etc.
- **The tool list** — names, one-line purposes, and the `act` command enum.
- **Escalation & guardrail rules** (§7) — when to `kb_lookup`, when to advance less time, when to `save`, when to flag for Sonnet.

Everything else is **on-demand via `kb_lookup`**. The KB itself is *not* in context; only the cheat sheet plus whatever 1–2 KB files the current turn pulled.

### Per-decision token budget

| Segment | Tokens (typical) |
|---|---|
| System: cheat sheet (cached) | ~2,500 |
| Tool schemas (cached) | ~800 |
| `get_state` result | ~400 |
| `get_events` result | ~150 |
| 0–2 `kb_lookup` results | 0–2,500 |
| Model reasoning + tool calls (output) | ~300–800 |
| **Per-turn new input (uncached)** | **~600–3,000** |
| **Per-turn output** | **~300–800** |

With prompt caching, the ~3,300-token cheat-sheet + tools prefix is written once and read at ~0.1× thereafter. Volatile content (state, events) goes *after* the last cache breakpoint, so the cache survives every turn.

### Cost / latency estimate

Using cached-prefix economics (Haiku $1/$5 per M in/out; Sonnet $3/$15):

| | Haiku, low effort | Sonnet, low/med effort |
|---|---|---|
| New input / turn | ~1,500 tok | ~1,500 tok |
| Output / turn | ~500 tok | ~600 tok |
| Cost / turn (rough) | ~$0.004 | ~$0.014 |
| Latency / turn | ~1–3 s | ~3–6 s |

### Decisions per run — order of magnitude

A run spans ~5–15 cycles. At ~1 decision/day and ~12 days/cycle plus extra decisions around drought boundaries and construction, expect **~10²–10³ decisions per run** (a few hundred is typical). At Haiku's ~$0.004/turn that's **~$1–4 per run** — cheap enough to run many playthroughs for the learning loop. Sonnet-only would be ~$4–15/run, which is why Sonnet is reserved for flagged turns (§7), not the default.

---

## 4. Knowledge base format

**One concept per small file (~1–2k tokens).** Compact tables + imperative rules. An index maps topics/aliases → files; the agent searches it via `kb_lookup`. The KB is *static* — authored once by the big model, versioned, never written to by the player agent (that's the playbook's job, §5).

### Index (excerpt)

```json
{
  "water_storage_sizing":   { "aliases": ["water buffer","drought water"], "file": "kb/water_storage_sizing.md" },
  "food_production_ratios":  { "aliases": ["farms","food per beaver"], "file": "kb/food_production_ratios.md" },
  "drought_prep_checklist":  { "aliases": ["dry cycle","prep"], "file": "kb/drought_prep_checklist.md" },
  "power_wheels_vs_dams":     { "aliases": ["power","energy"], "file": "kb/power.md" }
}
```

### Example KB entry — `kb/water_storage_sizing.md`

```markdown
# Water storage sizing

## Rule (imperative)
- Target water buffer = daily_consumption × (longest_expected_drought_days + 3).
- If projected buffer < that target, expand storage BEFORE the drought starts.
- One Water Tank = 500 water. One Large Tank = 2500.

## Consumption table
| Population | Water / day | Drought 6d target | Drought 10d target |
|-----------|-------------|-------------------|--------------------|
| 8         | ~8          | 72                | 104                |
| 16        | ~16         | 144               | 208                |
| 24        | ~24         | 216               | 312                |

(daily ≈ 1.0 × total beavers; +10% if any building consumes water)

## Sizing shortcut
tanks_needed = ceil(target / 500)

## Placement
- Tanks need adjacency to a water source or a pump route.
- Prefer filling ALL storage before a drought; a full small tank beats an empty large one.

## Common failure
- Building the tank during the drought: pumps can't fill it (no flowing water).
  Build + fill in the wet cycle.
```

Files are terse and mechanical on purpose — a cheap model applies a table lookup or a formula, it does not derive strategy.

---

## 5. Playbook & retrospective loop

The **playbook** is *evolving cross-run* strategy, kept strictly separate from the static KB:

- **KB** = timeless game facts (how much water a tank holds). Authored once, read-only for the player agent.
- **Playbook** = lessons learned across *these* runs (what worked/failed for this colony style). Appended to after every run/cycle.

Keeping them separate means a wrong lesson never corrupts ground-truth facts, and the KB can be re-versioned without losing accumulated experience.

### Structured lesson format

```json
{
  "id": "L-042",
  "trigger": "drought approaching AND water stored_days < days_until_weather_change + 2",
  "situation": "cycle>=2, population>=12, single reservoir source",
  "action": "expand water tanks to cover drought+3 days DURING the wet cycle; set pump priority to 5",
  "outcome": "survived drought with 1.5 days water margin; 0 deaths",
  "confidence": 0.8,
  "runs_seen": 4,
  "last_updated_run": 17,
  "supersedes": ["L-031"]
}
```

### Example lessons

```json
[
  { "id": "L-042",
    "trigger": "water stored_days < days_until_weather_change + 2 and weather=wet",
    "situation": "any cycle, single water source",
    "action": "build+fill tanks to drought+3 target now; do not wait for dry cycle",
    "outcome": "survived; avg 1.5d margin over 4 runs",
    "confidence": 0.85, "runs_seen": 4 },

  { "id": "L-039",
    "trigger": "food net_per_day < 0 for 2+ consecutive days",
    "situation": "population growing, <2 farms",
    "action": "add 1 farm + designate_area plant before adding any decorative/wellbeing building",
    "outcome": "starvation avoided in 3/3 runs where applied",
    "confidence": 0.75, "runs_seen": 3 },

  { "id": "L-051",
    "trigger": "housing_free == 0 and children > 0",
    "situation": "early cycles",
    "action": "build 1 lodge immediately; population stalls otherwise",
    "outcome": "faster growth; but caused log shortage once (see L-052)",
    "confidence": 0.6, "runs_seen": 2 }
]
```

### Retrospective mechanism

After each **run** (and optionally each **cycle** for faster feedback), a **summarizer** — a *slightly bigger model, `claude-sonnet-5` at medium effort*, since this is the reflective step and runs once per run, not per turn — reviews:

- the **decision log** (state → chosen actions → resulting events, per turn), and
- **final metrics** (survival cycle, peak population, avg wellbeing, droughts survived, deaths).

It then:

1. Proposes new lessons or updates to existing ones (bumps `runs_seen`, adjusts `confidence`).
2. Detects contradictions with existing lessons.
3. Writes via `playbook_append` (schema-validated).

The player agent reads the playbook (`playbook_read`) at run start and folds high-confidence lessons into its working context — treating them as extra survival rules alongside the cheat sheet.

### Reconciling contradictory lessons

Two lessons whose `trigger`/`situation` overlap but whose `action` conflicts are reconciled by **confidence, then recency**:

- **Confidence** is a function of `runs_seen` and observed success rate. Higher wins.
- **Recency** breaks ties (later `last_updated_run` wins) — the game or strategy may have shifted.
- The winner records `supersedes: [loser_id]`; the loser is demoted (kept for audit, but not surfaced to the player agent).
- Low-confidence lessons (`confidence < 0.5`, `runs_seen < 2`) are marked **provisional** and only surfaced when no higher-confidence lesson matches — they influence but don't override.

---

## 6. Learning across runs (no weight updates)

"Improvement" is achieved entirely through **scaffolded memory**, not fine-tuning:

- **Growing playbook** — each run appends/refines lessons; the next run starts smarter.
- **Refined KB** — when the summarizer finds a *factual* gap (not a strategy lesson), it flags it for the big model to fix the KB out-of-band. The KB thus sharpens over time too, but through a human/big-model gate, not the player agent.

The model weights never change. The agent gets better because the *context it reads* gets better — the same cheap model, given better rules and better memory, makes better decisions.

### Metrics that prove improvement

Tracked per run, compared across runs:

| Metric | What it shows |
|---|---|
| **Survival to cycle N** | Primary — how long the colony lasts. |
| **Peak / final population** | Growth capability. |
| **Avg wellbeing** | Quality of the colony, not just survival. |
| **Droughts survived** | Handling the core threat. |
| **Deaths (by cause)** | Specific failure modes to target with lessons. |
| **$ per run / decisions per run** | Efficiency — is the cheap model staying cheap? |

Improvement = these trending up (survival, population, wellbeing, droughts) across the run sequence while cost stays flat.

### Prior art

This is the **Claude Plays Pokémon** / **Voyager** pattern:
- **Claude Plays Pokémon** — a single model playing a game turn-by-turn over a long horizon, using external memory/notes to maintain coherence across a context that far exceeds the window.
- **Voyager (Minecraft)** — an LLM agent that improves without weight updates by growing a *skill library* (reusable, verified behaviors) and iterating via environment feedback.

Our playbook is Voyager's skill library specialized to survival lessons; our digested-state + teaching-error bridge is the environment-feedback channel; our pause-between-turns loop is the Claude-Plays-Pokémon turn structure. The novelty here is pushing validation and triage into the bridge so a *cheap* model suffices.

---

## 7. Keeping a cheap model on-rails

### System prompt structure

```
[ROLE]   You play Timberborn by issuing validated tool calls. You are a careful
         operator, not a strategist. Follow the rules; when unsure, look it up.
[LOOP]   <the 9 steps of §1>
[RULES]  <~15 imperative survival rules>
[TOOLS]  <tool list + act command enum>
[GUARDRAILS] <the list below>
[PLAYBOOK] <high-confidence lessons, injected at run start>
```

Frozen prefix (ROLE/LOOP/RULES/TOOLS/GUARDRAILS) is cached; the per-run PLAYBOOK block sits at the end of the cached region and only changes between runs.

### Guardrails

- **Only issue validated actions.** The `act` enum is closed; the bridge rejects anything malformed with a teaching error. The model never free-forms a command.
- **When unsure, don't guess — `kb_lookup` or advance less time.** Uncertainty is a signal to *gather*, not to gamble. If a rule doesn't clearly apply, look it up; if still unclear, advance a fraction of a day and re-observe.
- **Small time steps.** Default Δ = 1 day; near a drought boundary or right after a construction change, Δ = 0.25 day. Errors surface fast and cheap.
- **Save before risky moves.** Before demolishing, terraforming (`designate_area dig`), or any large multi-building change, issue `act{save}`. If the next few observations show a metric cratering, `act{load}` to roll back. This bounds the damage of a bad decision to a fraction of a day.
- **Batch cap.** Max 5 actions per turn — prevents the model from committing a large plan it can't verify.

### When to escalate to Sonnet / Opus

The loop runs on **Haiku, low effort** by default. Escalate the *next turn* to `claude-sonnet-5` (low→medium effort) when a **flag** fires:

- **Novel situation** — no cheat-sheet rule and no playbook lesson matches the current alerts (the model reports "no applicable rule").
- **Repeated failure** — the same alert persists for N turns (e.g. water still understocked 3 turns after acting), or the same `act` teaching-error recurs after a corrected retry.
- **High-stakes irreversible move** — a large terraform or demolition affecting the colony's core.

Escalation is a **model swap on the loop**, not a sub-agent, to keep it simple; the summarizer already uses Sonnet. **Opus** (`claude-opus-4-8`) is reserved for the rare case where Sonnet also stalls repeatedly on the same situation — effectively a "call the expert" backstop, logged for later KB/playbook authoring by the big model. Escalations are counted; a high escalation rate is itself a signal that the KB/playbook has a gap to fill.

> Model-switch note: swapping models mid-run invalidates the prompt cache (caches are model-scoped). Escalation is infrequent by design, so the occasional cold cache-write is acceptable. Keep tools and the frozen prefix byte-identical across models so only the model dimension changes.

### Bounding damage from a bad decision

Three overlapping nets: **small time steps** (a bad action costs ≤ 1 day), **save/load** (roll back the last risky move), and **teaching errors** (most bad actions are rejected before they ever apply). Together these mean a single wrong decision by the cheap model is nearly always recoverable within one game day.

---

## 8. Worked micro-example — one decision cycle end to end

**Situation:** dry cycle approaching; water is understocked. The agent's turn:

**1. Ensure paused**
```json
// → act
{ "command": "pause" }
// ← { "ok": true, "result": { "speed": 0 } }
```

**2. Get state**
```json
// → get_state {}
// ←
{ "day": 40, "cycle": 3, "weather": "wet", "days_until_weather_change": 3,
  "population": { "total": 16 },
  "water": { "stored_days": 4, "net_per_day": -2, "sources": ["reservoir_a"] },
  "storage": { "water_pct": 0.55 },
  "alerts": [
    { "id": "water_understocked", "severity": "high",
      "detail": "water 4 days stored, drought in 3 days" }
  ],
  "cursor": "evt_20510" }
```

**3. Check events**
```json
// → get_events { "since": "evt_20510" }
// ← { "events": [], "cursor": "evt_20510" }
```

**4. Consult the rule** (alert fired → look it up)
```json
// → kb_lookup { "topic": "water_storage_sizing" }
// ← (returns kb/water_storage_sizing.md — see §4)
```
Model applies the rule: target buffer = 16 beavers × (drought 6d + 3) = **144 water**. Current ≈ 4 days × 16 ≈ 64 stored, storage 55% full. It's the wet cycle → build + fill **now** (KB "common failure": don't build during drought). `tanks_needed = ceil(144/500) = 1` extra tank, plus raise pump priority to fill fast.

**5–6. Decide batch + POST actions.** First it needs geometry for the tank:
```json
// → get_map { "bbox": [40,40,48,48] }   (near reservoir_a)
// ← { "grid": ["GGGGWWWW","GGGBWWWW", ...] }   (ground beside water)

// → act (attempt 1)
{ "command": "place_building", "type": "water_tank", "x": 43, "y": 41 }
// ← teaching error
{ "ok": false, "error": {
    "code": "overlap",
    "message": "cannot place water_tank at [43,41]: overlaps Lodge footprint",
    "suggestion": { "nearest_free": [44,41], "reason": "adjacent, on water edge" } } }
```

**7. Handle the teaching error** — retry at the suggested tile:
```json
// → act (attempt 2, using suggestion)
{ "command": "place_building", "type": "water_tank", "x": 44, "y": 41 }
// ← { "ok": true, "result": { "placed": "water_tank", "at": [44,41], "cost": {"logs": 8} } }

// → act (raise pump priority so the new tank fills before the drought)
{ "command": "set_priority", "x": 45, "y": 41, "priority": 5 }
// ← { "ok": true, "result": { "priority": 5 } }
```

**8. Advance bounded time** — drought is near, so advance a fraction of a day and re-check that the tank is filling:
```json
// → act { "command": "set_speed", "speed": 2 }   // ← ok
// → act { "command": "resume" }                    // ← ok
// (bridge auto-pauses after Δ = 0.5 day given proximity to weather change)
```

**9. Loop.** Next turn: `get_state` should show `water.stored_days` rising and the alert clearing. If it *doesn't* clear after 3 turns → the repeated-failure flag fires → next turn escalates to `claude-sonnet-5`. After the run, the summarizer sees "acted on `water_understocked` in wet cycle → survived drought" and bumps **L-042**'s `runs_seen` and `confidence`.

---

## Appendix — Agent SDK sketch (Python, illustrative)

The loop is a manual agentic loop (fine-grained control over stepping/escalation), tools declared `strict: true`, cheat sheet cached in `system`.

```python
import anthropic
client = anthropic.Anthropic()

HAIKU  = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-5"

SYSTEM = [{ "type": "text", "text": CHEAT_SHEET,          # ~2.5k tok, frozen
            "cache_control": {"type": "ephemeral"} }]

def decide_turn(history, model=HAIKU):
    return client.messages.create(
        model=model,
        max_tokens=1024,
        output_config={"effort": "low"},   # low by default; medium on Sonnet escalation
        system=SYSTEM,
        tools=TOOLS,                        # get_state, get_map, get_events, act, kb_lookup, ...
        messages=history,                   # volatile state/events go here, after the cached prefix
    )
# Escalate by calling decide_turn(history, model=SONNET) when a flag fires (§7).
```

`TOOLS` are ordered deterministically and never change mid-run (preserves the tools+system cache). Volatile `get_state`/`get_events` results live in `messages`, after the last cache breakpoint, so the cache is read every turn.
