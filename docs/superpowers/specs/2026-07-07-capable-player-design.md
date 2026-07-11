# Capable Timberborn Player — Design

date: 2026-07-07 · status: proposed (autonomous; user to review later)

## Goal

A local-LLM agent that plays Timberborn *well* and improves across runs — survives
droughts/badtides, bootstraps the economy, keeps a connected/functioning colony —
without burning slow LLM calls on mechanical decisions or learning falsehoods.

## What we learned (why the current loop underperforms)

The bridge API and `planner.py` are already strong. Live runs and a literature
review (Voyager, Reflexion/ReAct, ExpeL, ReflAct, hierarchical LLM+controller,
planning-failure analyses) converge on one conclusion: **the failures are not
architectural — they are about what the LLM is allowed to decide, a learning loop
that manufactures false causality, and calling the LLM at non-forks.**

Three concrete failure modes, each with a code-level (not prompt-level) fix:
1. **Idling**: after placing the free lumberjack the LLM advances time one tick at a
   time waiting for logs, instead of placing the other free/affordable buildings.
   LLMs cannot be prompted out of local-optimum idling — it must be a code invariant.
2. **False causality**: `discovery.py` (pre-fix) taught the playbook "WaterPump →
   raises Log", "demolish → raises Log", "LumberjackFlag → consumes Berries" — pure
   correlation. Injecting these *causes* the model to state falsehoods. The playbook
   also has 5 duplicate all-loss "bridge error" lessons.
3. **Latency waste**: a slow tunneled qwen2.5:14b is called every turn, including
   pure advance-time turns where no decision exists.

## Architecture: deterministic controller + LLM arbiter (3 tiers)

Demote the LLM from per-turn actor to **arbiter at genuine forks**. Every cycle:

**Tier 1 — Deterministic executor (no LLM).** The planner already emits an ordered
goal checklist, reachable+affordable candidate tiles, follow-ups, and
`advance_time_recommended`. The controller executes, in code, every goal that is
*unambiguous*: place all free + affordable goals that have ≥1 reachable candidate
(with follow-ups like `designate_cutting`), in priority order, as one `/act batch`.
This is the curriculum — the planner's survival-ordered checklist *is* the plan; no
LLM needed to generate it.

**Tier 2 — LLM arbiter (only at real forks).** A `needs_llm(report)` gate calls the
model ONLY when the planner surfaces a choice it cannot rank deterministically:
- resource contention: several goals affordable, not enough logs to fund all → which
  subset, given the weather forecast;
- risk trade-off: gamble a discretionary build vs. bank water before a forecast
  drought/badtide;
- novel alert / stuck state (a goal unreachable across water → dam/bridge?).
The LLM returns a **choice among planner-enumerated options** (ordered subset of
goal ids + brief why) — never free-form coordinates or invented actions. Most turns
are zero-LLM.

**Tier 3 — Deterministic verifier (no LLM).** After the batch, compute
`after - before` deltas in code and feed *that* back as OBSERVED effects (not the
model's narration). Grounds the model and gives discovery clean, attributable
evidence.

### Control invariant (fixes idling) — refined per Codex critique
Before any `set_speed`/advance: queue every goal in the **safe ready frontier** —
free/affordable goals with a reachable candidate — but the frontier is NOT "every
incomplete checklist item". It must respect:
- **Cumulative material reservation**: budget logs/planks across the whole batch
  (don't queue 3 builds each needing 12 logs when only 12 exist).
- Builder/hauler throughput and workplace staffing (don't flood sites).
- Sites already at target capacity (enough tanks) → stop.
- Dependencies (a producer before its consumer) and candidate footprint/path overlap.

**Immediate reachability check**: after each placement inspect `auto_connect.connected`
+ the actual (possibly relocated) coords + next `/state` reachability. A
`no_land_route` result means unreachable — connect/rebuild/escalate THIS cycle, never
wait two turns (the old demolish-churn came from waiting).

### Bulk time-advance = event watcher (not one tick)
Loop: `advance (chunk) → cheap /state poll → wake on a decision threshold → PAUSE →
read stable state → plan/advise → execute`. Wake conditions: a new/cleared alert, a
tracked resource crossing a threshold, a hazard becoming imminent (never advance past
`weather.next.in_days`), a blocked goal becoming affordable, or a
population/staffing transition (birth/death, homeless/free-bed change, a critical
workplace going unstaffed, unemployment crossing a threshold). **Pause before any LLM
call** so the state described to the model isn't stale by the time it replies.

### Population/consumption forecasting
Size water/food targets from current population PLUS expected near-term growth (kits
maturing), not just current pop. Don't build workplaces merely because they're free —
reserve workers for survival-critical roles. Respect `/resources.truncated` (don't
assume the full resource set was seen at the 400-cap).

### Skill/plan library (next tier, Voyager-adapted)
We play via a structured API, so a "skill" = a **named, parameterized deterministic
macro** with a precondition + success check: `bootstrap_lumber_chain`,
`establish_water_security(days)`, `prep_for_drought(days)`, `secure_food`. The LLM
(at a fork) picks a macro by one cheap classification; code runs it across turns with
no further LLM calls. Promote repeatedly-successful discovered sequences into macros
(progressive crystallization); a circuit-breaker demotes a macro back to LLM control
if it starts failing.

## Learning loop (honest, contrastive)
- **Mechanism gate** (already added to discovery.py): only credit a "raises <good>"
  lesson to a plausible producer (pump→Water, cutting→Log). Keep it.
- **Purge + dedupe the playbook**: reset the polluted `playbook.json`; in coach,
  dedupe by (action, outcome) and never raise confidence on an all-loss lesson —
  store persistent-loss patterns as "avoid" or drop them.
- **Goal-state reflection** (ReflAct): end-of-run reflection formatted as
  "goal was X; reached Y; gap caused by Z", not free narration.
- Cap the injected playbook to ~10–15 high-confidence, deduped rules.

## Components & boundaries
- `planner.py` — unchanged role (goals, candidates, followups, affordability);
  the curriculum + option enumerator. Small additions: expose per-goal
  `affordable`/`free` flags and a `decision_fork` descriptor when goals contend.
- `controller.py` (NEW) — the deterministic Tier-1/Tier-3 loop + control invariant +
  bulk time advance + `needs_llm` gate. Owns the run loop.
- `play.py` — becomes the LLM-arbiter adapter (constrained multiple-choice prompt +
  parse) called by the controller, plus wiring (bridge client, journal, vision).
- `discovery.py` — keep mechanism gate; feed it the deterministic deltas.
- `coach.py` — add dedupe + all-loss handling; ReflAct-format reflection.
- `macros.py` (NEW, next tier) — the skill library.

## Migration path (incremental, test-once per stage)
1. Playbook purge + coach dedupe/all-loss gate (independent, safe).
2. Control invariant + bulk advance in the existing loop (fixes idling without a
   rewrite). Verify: a run bootstraps lumber+water+food+housing without idling.
3. `needs_llm` gate + deterministic verifier deltas (cuts LLM calls; grounds model).
4. Constrained multiple-choice fork prompt.
5. Macros/skill library.
Each stage keeps the dry test suite green and is verified on one live fresh-colony run.

## Success criteria (from MVP.md D1–D6)
Survives cycle-1 drought; pop stable through cycle 3; zero stranded buildings;
economy bootstrapped by end of cycle 1; lessons learned are TRUE (no spurious);
avg decision turn commits ≥3 actions and most turns are zero-LLM.

## Risks
- Bulk time-advance overshooting a hazard onset → cap advance by the weather forecast
  (never advance past `next hazard in_days`).
- Deterministic controller mis-ranking under contention → that's exactly when it
  defers to the LLM; keep the fork detection conservative (defer when unsure).
- Resource unreachable across water (no_land_route) → surface as a fork; macro for
  dam/bridge is future work.
