# Stall-driven self-improving learning loop — design

Scope: extend the existing play → summarize → relabel → retrain loop
(`agent/run_loop.py`, `agent/replay.py`, `agent/nlp/learn.py`) so that (a) a run detects
its own stall **in-run** and ends early with a **diagnosed cause**, (b) the cause routes
to the **right** learning response instead of always "relabel the clone," (c) genuinely
structural failures (the kind `docs/kb/placement-verticality-gaps.md` catalogs) get
**surfaced**, not silently discarded, and (d) we can tell, from the trend log alone,
whether the loop is actually getting better or has hit a ceiling that needs a code change.

Everything below is additive to the current modules. No repo files were changed while
writing this — it's a design doc only.

---

## 1. What we have today (grounded in the actual code)

`agent/run_loop.py::run_learning_loop` (per-iteration): `play_policy.run` plays one
colony to `agent/runs/<run_id>.jsonl` via `replay.record_step` → `replay.summarize_run`
classifies `ended ∈ {alive, dead_thirst, dead_hunger, stalled}` → if `failed or regressed`
(score ≤ best-prior `(peak_pop, days_survived)`), `learn.build_augmented_dataset` folds a
bounded sliding window (`RELABEL_WINDOW=5`) of recent runs' `replay.credit_assignment`
corrections into a **frozen-pristine + window** dataset (bounded growth — good, already
solves "unbounded replay buffer") → retrain only if rows actually changed → reload is
implicit (`DecisionPolicy.load()` re-reads JSON every `play_policy.run` call) → stop at
`reached_30_pop` or `iterations` exhausted.

`agent/replay.py::_scan` walks a run's rows once and trips `ended="stalled"` after
`_STALL_STREAK=8` consecutive `advance_time` steps with no `log_stored`/building-count
increase. `credit_assignment` then looks at the trailing `lookback` window and calls
`_stall_better_action`, a **hard-coded 3-item priority list**
(WaterPump → GathererFlag → LumberjackFlag): if all three already exist it returns
`better_action=None`, reason `"...all present, cause unclear"`, and
`learn.examples_from_run` silently **drops** that window (`better_action not in
valid_actions` guard). Today that's the *only* outcome available for a stall whose real
cause is something the deterministic expert (`planner.py`) never had a goal for at all.

**Two things already computed and thrown away, load-bearing for this design:**

1. `agent/nlp/play_policy.py::run` computes `expert_top` **every single cycle** (the
   schema-id `controller.build_safe_ready_frontier` would pick right now, translated via
   `labeler._to_schema_id`) purely for fidelity telemetry (`agrees` bool in the journal),
   and passes it into `replay.record_step(..., meta={"expert_top": expert_top, ...})`.
   `replay.py` never reads `row["meta"]["expert_top"]` back out. This is a **DAgger label
   sitting unused in every run file** — the real expert's opinion at the exact state the
   policy visited, which is strictly better than `_stall_better_action`'s 3-item heuristic
   and, unlike that heuristic, generalizes to all 83 `game_schema.actions()` goals
   automatically as `planner.py` grows (Task 3a–3e in
   `docs/superpowers/plans/2026-07-11-timberborn-full-game-completion.md`).
2. `game_schema._VERB_ACTIONS` includes `"advance_time"` as a *valid* `game_schema.actions()`
   member — so naively relabeling toward `meta.expert_top` whenever present, without
   excluding `"advance_time"`, would teach the model "wait here," compounding the
   over-prediction of `advance_time` that `play_policy._execute_intent` already has to
   work around. When `expert_top == "advance_time"`, that isn't a label — it's the
   **structural-gap signal**: the real planner also has nothing to do.

There is also a second, currently-orphaned self-improvement mechanism:
`agent/coach.py` (`analyze` → rule-based lessons, `reconcile`/`update_playbook` →
confidence+evidence-weighted dedup into `agent/playbook.json`, with noise-pruning for
persistent-loss lessons) and `agent/discovery.py` (`distill` → empirical cause→effect
lessons in the same shape). Both are wired into the **old** LLM-per-turn loop
(`agent/play.py` injects `compact_playbook_block` into the system prompt and calls
`coach.update_playbook` at run end) but **neither is called from `run_loop.py` or
`play_policy.py`** — the new trained-policy loop has no LLM prompt to inject a playbook
into, so this Reflexion-shaped memory system went dark when the policy replaced the LLM.
It's tested, schema-stable infrastructure for exactly the "remember a lesson across runs
with confidence that decays if it doesn't keep helping" problem — reusing it for
structural-gap lessons (§6.4) is cheaper than inventing a parallel store.

`agent/curriculum.py` (`current_phase`, `phase_priorities`, `bias_ranking`,
`is_goal_reached`) is wired into `play_policy.py` per-cycle, but `run_loop.py` never
feeds run **outcomes** back into it — a run that keeps failing in `DROUGHT_PROOF` doesn't
change what the next run's curriculum emphasizes.

---

## 2. Why relabeling alone is structurally incomplete

The model is behaviorally cloned from `planner.py`. Relabeling (`build_augmented_dataset`)
can only push the clone **toward the expert's existing opinion**. When the expert itself
has no answer — `docs/kb/placement-verticality-gaps.md`'s catalog: no vertical/stacking
action space at all (`z` is always terrain height, platforms/stairs excluded from
`_GAMEPLAY_CATEGORIES`), Dam/Levee/Floodgate routed to flat-dry-land candidate search so
they can *never* place, boxing handled reactively (`demolish_unreachable`) instead of
prevented, 78/87 specs get a generic flat-dry-tile placement profile — there is no correct
label to clone toward. Every run this session's proposed loop reads
`meta.expert_top == "advance_time"` in the stall window is (mechanically) exactly this
case: the deterministic planner, given the real map and real state, also picked
`advance_time`. Retraining on a fabricated label here doesn't fix the colony; it teaches
the clone a falsehood and burns a retrain cycle. This is the crux the taxonomy below is
built around.

---

## 3. Research: how others detect stalls, diagnose causes, and route the fix

### 3.1 Stall / plateau detection

- **In-run "stuck but not obviously dead"** is a known, distinctly awkward failure shape
  in autonomous agents: "stuck agents look normal from the outside — the heartbeat fires
  and logs show activity, but the task never completes," with named patterns like "The
  Repeater" (same action, state should change but doesn't) and "The Wanderer" (busywork
  disconnected from the goal) — checkpoint heartbeats + an independent watchdog with
  externalized state are the standard mitigation ([How to Detect When Your AI Agent Is
  Stuck](https://dev.to/clawgenesis/how-to-detect-when-your-ai-agent-is-stuck-and-what-to-do-about-it-ce9);
  [production stall/timeout lessons](https://dev.to/bobrenze/how-ai-agents-handle-stalled-tasks-and-timeouts-lessons-from-my-production-failure-1jj9)).
  Our `_STALL_STREAK` counter is already a heartbeat-style detector; it just runs
  **post-hoc** only, over the whole file, instead of live.
- **Across-run plateau / regret-based prioritization**: Prioritized Level Replay resamples
  training scenarios proportional to *regret* — a principled estimate of a scenario's
  remaining learning potential — rather than uniformly, which "induces an emergent
  curriculum" and focuses compute on informative cases
  ([Jiang et al. 2021, arXiv:2010.03934](https://arxiv.org/abs/2010.03934)). Our
  `credit_assignment`'s "regret windows" are already regret in this sense (steps where the
  chosen action was worse than the alternative); PLR's contribution we're *not* yet taking
  is prioritizing which **past runs/scenarios** to keep re-relabeling from, vs. treating
  the last N runs uniformly (our `RELABEL_WINDOW` today is a flat sliding window, not
  regret-weighted).
- **AlphaZero's loop shape** — self-play → train → **evaluate (challenger vs. champion,
  promote only if better)** — is the closest macro-analogy to ours (self-play ~ "play
  against the live game," reward ~ our score). AlphaGo Zero gated promotion on a 55%
  win-rate match; AlphaZero itself dropped the gate and always replaces
  ([Wikipedia: AlphaZero](https://en.wikipedia.org/wiki/AlphaZero);
  [OpenSpiel AlphaZero docs](https://openspiel.readthedocs.io/en/stable/alpha_zero.html)).
  We currently have **neither** — `run_loop.py` retrains and the reload is unconditional
  and implicit — which is the oscillation risk called out in §6.5.

### 3.2 Failure diagnosis / credit assignment beyond relabeling

- **Reflexion**: an LLM verbally self-reflects on a failure and stores the reflection in an
  episodic memory buffer consulted on the next attempt — "verbal reinforcement learning,"
  no weight updates
  ([Shinn et al. 2023, arXiv:2303.11366](https://arxiv.org/abs/2303.11366)). Structurally
  this *is* `agent/coach.py` + `playbook.json` (rule-based today, LLM-swappable per its own
  docstring: "Rule-based retrospective. Swappable later for a smarter LLM analyzer").
- **Voyager**: three parts — an **automatic curriculum** (propose the next task that
  matches current capability, not too easy/hard), an **ever-growing skill library**, and
  **iterative prompting with environment feedback + self-verification**, retrying a bounded
  number of times before moving on
  ([Wang et al. 2023, arXiv:2305.16291](https://arxiv.org/abs/2305.16291);
  [project site](https://voyager.minedojo.org/)). Maps onto us as: `curriculum.py` is
  already the automatic-curriculum piece; `planner.py`'s goal catalog is the (currently
  human/LLM-extended, not self-extended) skill library; our proposed **bounded exploration
  retry before declaring a structural gap** (§6.3) is the iterative-prompting-with-retry
  analogue, minus an LLM (ours retries a *different affordable action*, not a rewritten
  program).
- **DAgger**: the actual algorithm our relabel step approximates. DAgger's contribution
  over plain behavioral cloning is querying the expert **on states the learner itself
  visits**, closing the distribution-shift gap that makes cloned policies drift and
  compound errors, with a regret bound linear (not quadratic) in horizon
  ([Ross, Gordon & Bagnell 2011](https://www.roboticscenter.ai/glossary/dagger)). Our
  `credit_assignment` today only queries a hand-written 3-item heuristic on the *lookback
  window before a death/stall trigger* — a narrower, weaker version of DAgger than what
  `meta.expert_top` already lets us do "for free": query the **real** expert
  (`controller.build_safe_ready_frontier`) on literally every visited state, every cycle.
- **Hindsight relabeling**: HER turns a failed trajectory into a success by relabeling the
  goal to whatever *was* achieved
  ([Andrychowicz et al. 2017](https://www.emergentmind.com/topics/hindsight-experience-replay));
  AgentHER extends this to LLM-agent trajectories via a four-stage pipeline — **failure
  classification → outcome extraction → LLM-guided relabeling with confidence gating →
  data packaging**
  ([arXiv:2603.21357](https://arxiv.org/html/2603.21357v3)). That four-stage shape is
  almost exactly §6.2–6.3's `classify_stall` → evidence extraction → routed
  relabel-or-flag → dataset/playbook write, just with our routing decision made by
  deterministic rules instead of an LLM (see caveat: HER's own literature flags "hindsight
  bias" — relabeling can make bad outcomes look retroactively fine in stochastic domains;
  our mitigation is the same one DAgger already gives us — we relabel toward the *real
  expert's simultaneous opinion*, not toward "whatever happened," so a lucky-but-bad
  outcome doesn't get taught as correct).
- **LLM failure-taxonomy work**: "Where LLM Agents Fail and How They Can Learn From
  Failures" introduces `AgentErrorTaxonomy`/`AgentErrorBench`/`AgentDebug`, tracing errors
  to root cause and applying a *targeted* correction rather than a blanket retrain —
  explicitly framed as "debugging as a foundation for agents that continuously learn from
  their mistakes"
  ([arXiv:2509.25370](https://arxiv.org/pdf/2509.25370)). This is the direct precedent for
  "classify the failure class, then pick the response *for that class*" rather than one
  relabel-everything response, which is the core move this design makes.
- **Quality-diversity / novelty search**: maintain diverse intermediate solutions as
  stepping-stones rather than collapsing to one optimum, explicitly to escape deceptive
  fitness landscapes / local optima
  ([QD overview](https://www.emergentmind.com/topics/quality-diversity-algorithm); MAP-Elites
  lineage). POET goes further and **co-evolves the environment with the agent**, subject to
  a minimal-criterion (not too easy, not too hard)
  ([Wang et al. 2019, arXiv:1901.01753](https://arxiv.org/abs/1901.01753)). We are not
  proposing full QD/POET (no population, no environment mutation) — but the **bounded
  exploration retry** in §6.3 (try a different affordable action before giving up) is the
  minimal, single-agent version of "don't collapse to the one path that's stuck," and
  POET's minimal-criterion is a good frame for *why* `curriculum.py`'s phase gates exist
  (each phase's exit criterion is a minimal-criterion check).

### 3.3 Taxonomy of "update itself"

Concretely, five kinds of update, in increasing order of how much they touch:

| # | Update | What changes | Automatable? |
|---|---|---|---|
| a | Relabel + retrain the clone | `decision_dataset.json` rows, then `decision_cart/mlp.json` | Yes — pure code (have it) |
| b | Adjust curriculum / phase priorities | `curriculum._PHASE_PRIORITIES` bias, or a runtime penalty table | Yes — pure code (proposed, §6.7) |
| c | Fix/extend the expert (new goals, new placement, new action space) | `planner.py`, `game_schema.py`, `agent/economy.py`, the bridge | No — needs an LLM/human coding session (this *is* what produced `docs/kb/placement-verticality-gaps.md`) |
| d | LLM reflection proposing a concrete change | A written diagnosis + patch proposal | Diagnosis: yes, pure code, if scoped to "which spec/phase/tag." Root-cause **narrative** + patch: LLM |
| e | Exploration bonus / try-something-different when plateaued | Temporarily perturb ranking/site caps for K cycles | Yes — pure code (proposed, §6.3) |

(a), (b), (e) are pure-code and belong **inside** the hot play/retrain loop. (c) is a real
code-authorship task against game mechanics knowledge — Darwin Gödel Machine and STOP both
show an LLM *can* do this kind of self-directed code editing, validated empirically by
rerunning the benchmark/loop after the patch
([DGM, Zhang et al. 2025, arXiv:2505.22954](https://arxiv.org/abs/2505.22954);
[STOP, Zelikman et al. 2023, arXiv:2310.02304](https://arxiv.org/abs/2310.02304)) — but
both papers run that as a deliberately separate, reviewed, sandboxed step, not an unbounded
inline call from the training loop. (d)'s diagnosis half is cheap and automatable now
(§6.4's aggregation is literally "which tag, which phase, how often, show me the evidence");
its narrative/patch half is exactly what this agent's own `docs/kb/placement-verticality-gaps.md`
was — a one-off investigation — and the design below turns that from a manual "someone
noticed the agent keeps getting boxed in" into an automatic backlog with evidence attached,
still requiring a human/LLM session to act on it.

---

## 4. Failure-class → learning-response taxonomy

This is the routing table `run_loop.py` should apply after every run, in place of today's
binary `should_relabel = failed or regressed`.

| Class | Detection signal | Learning response | Module |
|---|---|---|---|
| **RESOURCE_STARVED** | `ended ∈ {dead_thirst, dead_hunger}` (existing `_scan` streaks) | Relabel: `better_action` = `meta.expert_top` at each window row if it's a concrete build goal (not `advance_time`), else fall back to the existing thirst/hunger heuristic. Retrain. | `replay.credit_assignment` (extend), `learn.build_augmented_dataset` (unchanged) |
| **POLICY_GAP** | `ended == "stalled"` or score regressed, AND at least one window row has `meta.expert_top` that is a concrete goal ≠ `advance_time` | Relabel toward that `expert_top` (true DAgger — the model diverged from a still-capable expert). Retrain. | same |
| **STRUCTURAL_GAP** | `ended == "stalled"` or score regressed, AND **every** window row's `meta.expert_top` is `advance_time`/absent (the real planner also has nothing to do) | **No relabel** (no correct label exists). Try bounded exploration retry first (§6.3); if that doesn't unblock it, write/merge a `coach.py`-shaped lesson (`trigger="structural_gap:<tag>"`) into `playbook.json` with evidence (spec/goal_id repeatedly requested-but-never-executed, phase, run_ids). No retrain. | new `replay.classify_stall`, new `learn.gap_lesson_from_diagnosis`, `coach.update_playbook` |
| **REPEATED_FAILED_ACTION** (auxiliary tag, attaches to any of the above) | Same `policy_top`/`action` id appears in ≥5 of the last 6 recorded cycles with `meta.executed is False` | Attach as evidence on whichever primary-class response fires; if paired with POLICY_GAP, also demote that goal_id in `curriculum`'s next-run ranking bias (§6.7) so the model stops re-proposing a placement that structurally never lands, even before a code fix ships. | `replay.classify_stall`, `curriculum.apply_gap_penalties` (new) |
| **BAD_EXPERT / EXPERT_BLIND** (stretch; flagged, not built in v1) | `ended ∈ {dead_thirst, dead_hunger}` but `meta.expert_top` was *also* `advance_time` right up to the death — i.e. the deterministic planner itself failed to see the danger coming (e.g. `resource_manager.drought_prep` under-forecasting) | Same as STRUCTURAL_GAP (no correct label — the "expert" is the bug) but tag distinctly (`structural_gap:expert_blind:<cause>`) so a human fixing it knows to look at `resource_manager`/`planner.py` forecasting, not placement | future extension of `classify_stall` |
| **Improved / flat-but-alive** | `ended == "alive"` and score ≥ best-prior | No-op (current correct behavior — don't perturb a colony that's improving) | unchanged |

---

## 5. The enhanced loop — concrete deltas

### 5.1 In-run stall detection (ends the run early, with a cause)

Today `play_policy.run`'s cycle loop only breaks early on `curriculum.is_goal_reached`
or `_alive(state) <= 0`; a colony that's alive-but-stuck runs the full `max_cycles`,
burning wall-clock on `bulk_advance_until_wake(..., max_advance_days=3.0, max_polls=120)`
every idle cycle. Fix: reuse the **same** streak logic `replay._scan` already has, but
make it incrementally callable so `play_policy.py` can consult it every cycle instead of
only at post-hoc `summarize_run` time.

```python
# agent/replay.py — refactor _scan's streak tracking into a reusable, incremental function.
# Behavior-preserving: _scan becomes a thin wrapper that calls this in its existing loop.

def progress_signal(rows, state_ctx=None):
    """Incrementally reproduces _scan's death/stall streak logic over `rows` (a prefix
    of a run, or the whole run). Returns the same {ended, death_cause, trigger_index}
    shape _scan already produces, computed over just the given rows — so play_policy.py
    can call this every cycle with rows-so-far and get the identical classification
    summarize_run would compute post-hoc, instead of duplicating the thresholds."""
    ...  # body = the existing streak-tracking loop, extracted verbatim from _scan
```

```python
# agent/nlp/play_policy.py — inside the `for cycle in range(1, max_cycles + 1):` loop,
# right after replay.record_step (which already returns the record dict):

record = replay.record_step(run_id, cycle, state, intent, meta={...})
sig = replay.progress_signal(replay.load_run(run_id))   # or an in-loop accumulator, avoid re-reading the file every cycle
if sig["ended"] != "alive":
    play.journal_append(journal_path, {
        "run_id": run_id, "cycle": cycle, "event": "stall_detected",
        "ended": sig["ended"], "death_cause": sig["death_cause"],
    })
    play.log_stderr("cycle %d: in-run stop (%s) - ending run early" % (cycle, sig["ended"]))
    break
```

Cheap: `_STALL_STREAK=8` and `_DEATH_STREAK=3` are already small windows, so this adds no
real overhead, and it turns "grind to `max_cycles` on a dead colony" into "stop within
~8 cycles of the streak actually starting" — the wall-clock saving `run_loop.py` needs to
spend its iteration budget on runs that can produce learning signal, not on runs that are
already-decided losses. (Read `replay.load_run` fresh each cycle only if cheap enough in
practice; alternatively thread an in-memory list of the cycle's own recorded dicts through
the loop to avoid the file re-read — either is a small implementation choice, not a design
fork.)

### 5.2 Failure classification (the taxonomy, made code)

```python
# agent/replay.py — new function, sits next to credit_assignment and calls the same
# _scan/window machinery. This is what run_loop.py routes on.

RESOURCE_STARVED = "resource_starved"
POLICY_GAP = "policy_gap"
STRUCTURAL_GAP = "structural_gap"

def classify_stall(run_id, lookback=6):
    """{class, window, repeated_action, expert_had_option} for a run that ended badly.
    class is one of RESOURCE_STARVED / POLICY_GAP / STRUCTURAL_GAP, or None if the run
    is 'alive' (nothing to classify).

    expert_had_option = True iff any window row's meta.expert_top is a concrete
    game_schema goal (not None/"advance_time") - the DAgger signal that the real planner
    still saw a move. repeated_action = the most common (action id) among window rows
    where meta.executed is False, if it appears in >= 5 of the last 6 - else None.
    """
    rows = load_run(run_id)
    scan = _scan(rows)
    if scan["ended"] == "alive" or scan["trigger_index"] is None:
        return None
    start = max(0, scan["trigger_index"] - lookback + 1)
    window = rows[start:scan["trigger_index"] + 1]

    def _expert_top(row):
        meta = row.get("meta") or {}
        return meta.get("expert_top")

    expert_had_option = any(
        et not in (None, "advance_time") for et in (_expert_top(r) for r in window)
    )
    failed_actions = [
        _action_id(r.get("action")) for r in window
        if isinstance(r.get("meta"), dict) and r["meta"].get("executed") is False
    ]
    repeated = None
    if failed_actions:
        top, count = Counter(failed_actions).most_common(1)[0]
        if count >= max(1, len(window) - 1):
            repeated = top

    if scan["ended"] in ("dead_thirst", "dead_hunger"):
        cls = RESOURCE_STARVED
    elif expert_had_option:
        cls = POLICY_GAP
    else:
        cls = STRUCTURAL_GAP

    return {"class": cls, "window": window, "repeated_action": repeated,
            "expert_had_option": expert_had_option, "ended": scan["ended"]}
```

`credit_assignment` changes to *use* this instead of guessing independently:

```python
def credit_assignment(run_id, lookback=6):
    diagnosis = classify_stall(run_id, lookback=lookback)
    if diagnosis is None:
        return []
    if diagnosis["class"] == STRUCTURAL_GAP:
        return []   # no correct label exists - nothing for learn.examples_from_run to use
    # RESOURCE_STARVED / POLICY_GAP: prefer the real expert's per-row opinion (true DAgger)
    # over the hand-written thirst/hunger/stall heuristic; fall back to the heuristic only
    # for rows recorded before meta.expert_top existed, or where it's itself advance_time.
    entries = []
    for row in diagnosis["window"]:
        expert_top = (row.get("meta") or {}).get("expert_top")
        better_action = expert_top if expert_top not in (None, "advance_time") else _legacy_better_action(diagnosis, row)
        ...
    return entries
```

(`_legacy_better_action` = today's thirst/hunger/`_stall_better_action` logic, kept
verbatim as the fallback path — this is additive, not a rewrite, and every existing
`replay.py` unit test keeps passing unchanged since old runs / runs without `meta` still
hit the fallback.)

### 5.3 Routing in `run_loop.py`

```python
# agent/run_loop.py — replace the single should_relabel gate with class-based routing.

metrics = replay.summarize_run(run_id)
diagnosis = replay.classify_stall(run_id, lookback=relabel_window) if metrics["ended"] != "alive" else None
score = _score(metrics)
regressed = best_score is not None and score <= best_score

if diagnosis and diagnosis["class"] == replay.STRUCTURAL_GAP:
    resolved = _try_exploration_retry(cfg, run_id, i, max_cycles)   # §6.3, bounded, cheap
    if not resolved:
        lesson = learn.gap_lesson_from_diagnosis(diagnosis, run_id)  # new, coach.py-shaped
        playbook = coach.update_playbook(_PLAYBOOK_PATH, [lesson], run_id)
        gap_flagged = True
    should_relabel = False
else:
    should_relabel = (metrics["ended"] in FAILURE_ENDINGS) or regressed
    gap_flagged = False
```

### 5.4 Bounded exploration retry (the "e" response, automatable)

Before conceding a run to STRUCTURAL_GAP, try the cheap thing: replay the same colony a
short distance further with the ranking perturbed, so a transient/local cause (e.g. the
placement scorer being one candidate too conservative, or `MAX_ACTIVE_SITES` blocking a
different building that would have unblocked progress) gets a chance to resolve itself
without waiting for a planner code change. This is the single-agent, no-LLM version of
Voyager's bounded iterative retry and QD's "keep a diverse stepping stone instead of
collapsing to the one stuck path":

```python
def _try_exploration_retry(cfg, run_id, iteration, max_cycles, tries=1, extra_cycles=10):
    """On a STRUCTURAL_GAP stall, retry up to `tries` times with the ranking perturbed
    (skip the top-ranked-but-never-executes goal; temporarily relax MAX_ACTIVE_SITES by
    1) for `extra_cycles` more cycles. Returns True if the retry made progress (peak_pop
    or building count increased vs the stalled snapshot) - the caller then treats this
    run as ordinary POLICY_GAP/alive instead of flagging a gap. Bounded and cheap: this
    is NOT a general search, just "did skipping the one thing that's obviously not
    working help," logged as its own run_id suffix (e.g. "<run_id>_retry1") so it doesn't
    corrupt the original run's trace.
    """
```

If retries never help (the common case for a *real* structural gap like "no vertical
placement exists"), it fails fast and falls through to gap-flagging — bounded cost either
way.

### 5.5 Structural-gap aggregation (reusing `coach.py`, not a new store)

```python
# agent/nlp/learn.py — new adapter, coach.py-shaped so it flows through the EXISTING,
# tested reconcile/confidence/prune machinery instead of a new schema.

def gap_lesson_from_diagnosis(diagnosis: dict, run_id: str) -> dict:
    tag = diagnosis.get("repeated_action") or "unknown"
    return {
        "trigger": "structural_gap:%s" % tag,
        "situation": "phase=%s ended=%s expert_had_option=False" % (
            diagnosis["window"][-1].get("meta", {}).get("phase"), diagnosis["ended"]),
        "action": "extend planner.py/game_schema.py (or the bridge) so %s has a real "
                  "candidate; see docs/kb/placement-verticality-gaps.md for the known "
                  "verticality/water-infra gaps" % tag,
        "outcome": "unblocks colonies that stall with the real expert also proposing "
                   "advance_time - relabeling cannot fix this class",
        "evidence": {"runs": 1, "wins": 0, "losses": 1},
        "confidence": 0.5,
        "created_run": run_id, "last_seen_run": run_id,
    }
```

`coach.reconcile`/`update_playbook` already do exactly the right thing with this: dedup by
`(trigger, action)` key, accumulate `evidence.runs` across recurrences, and — this is the
part worth calling out — a lesson that's recurring (seen across many runs, `losses` only
since a structural gap by definition never "wins") is *exactly* the signal that should
escalate loudest. `coach._confidence_from_evidence` currently caps all-loss lessons at
`0.2` (designed to suppress noise) — for `structural_gap:*` lessons specifically this
should probably be inverted (persistence across runs = higher priority, not lower
confidence) since "still true every run" is the opposite of noise here; call this out as a
one-line tweak (a `trigger.startswith("structural_gap:")` branch in
`_confidence_from_evidence`) rather than reusing the win/loss-shaped default verbatim.

`run_loop.py` surfaces this at the end of `run_learning_loop` (or every K iterations):

```python
play.log_stderr(
    "run_loop: %d structural-gap lesson(s) at confidence>=0.5 unresolved - see agent/playbook.json"
    % sum(1 for l in playbook["lessons"] if l["trigger"].startswith("structural_gap:") and l["confidence"] >= 0.5)
)
```

— the same escalation shape this very working session used to produce
`docs/kb/placement-verticality-gaps.md`, just triggered by accumulated evidence instead of
a person noticing the agent "placed → boxed in → demolished" loop by eye.

### 5.6 Champion/challenger retrain gate (new — prevents oscillation/regression)

Nothing today stops a relabel+retrain from producing a *worse* model — `should_relabel`
fires on `regressed` (this run scored ≤ best-prior), which is exactly the situation where
a bad correction is most likely, and the reload next iteration is unconditional. Borrow
AlphaZero's evaluate-before-promote gate, minimally:

```python
def _promote_or_revert(cka_dataset_path, cart_path, mlp_path, validation_run):
    """After retrain, before the NEXT iteration's play_policy.run reads the new model:
    back up the pre-retrain cart/mlp JSON; if a bounded validation replay (a fixed short
    fixture state batch, or the just-played run replayed against the NEW model in
    dry-run/no-op-bridge mode) scores worse than the champion, restore the backup and log
    'retrain rejected (regressed)' instead of silently deploying a worse policy."""
```

This is the direct, minimal answer to "avoid oscillation": today the loop can only get
monotonically noisier data (bounded, fine) but has **no floor** stopping a bad retrain from
being played next; a cheap champion/challenger check gives it one. (Full live-game
validation is expensive; even a lightweight proxy — re-score the *last few* recorded runs'
feature vectors against old vs. new model and require non-regression on `raw_top ==
expert_top` fidelity — is enough to catch the worst case cheaply, without a second live
playthrough per iteration.)

### 5.7 Curriculum feedback loop (the "b" response)

```python
# agent/curriculum.py — new, additive function (bias_ranking unchanged/still used alone
# when there's no gap evidence).

def apply_gap_penalties(ranked, gap_lessons, penalty=0.15):
    """Demote goal_ids that appear as a structural_gap:<goal_id> trigger in gap_lessons
    (confidence-weighted: penalty * confidence) - so the policy stops re-proposing (and
    re-recording as REPEATED_FAILED_ACTION) a placement that structurally never lands,
    while a code fix is pending. Never removes an entry, only reorders - same contract as
    bias_ranking (stable, no drop/duplicate)."""
```

`play_policy.py` calls `curriculum.apply_gap_penalties(ranked, playbook["lessons"])`
right after `curriculum.bias_ranking`, reading `agent/playbook.json` once per run (not
per-cycle — cache it like `DecisionPolicy.load()`). This is how "seed the next run with
what was learned" works for the structural-gap class specifically: the *code* isn't fixed
yet, but the policy stops wasting cycles proposing the thing that's known-broken, which
also tightens how fast the in-run stall detector (§6.1) resolves future stalls of the same
kind (fewer wasted `REPEATED_FAILED_ACTION` cycles before falling through to something
executable or to a clean `advance_time`).

### 5.8 Across-run plateau detection + stop condition

`run_learning_loop` gains:

```python
def run_learning_loop(..., plateau_patience: int = 5, ...):
    ...
    stall_streak = 0  # iterations since best_score last improved
    for i in range(...):
        ...
        improved = best_score is None or score > best_score
        stall_streak = 0 if improved else stall_streak + 1
        entry["stall_class"] = diagnosis["class"] if diagnosis else None
        ...
        if stall_streak >= plateau_patience:
            recent = trend[-plateau_patience:]
            if all(e.get("stall_class") == replay.STRUCTURAL_GAP for e in recent):
                play.log_stderr(
                    "run_loop: plateaued %d iterations, all structural_gap - stopping "
                    "(retraining cannot fix this; see agent/playbook.json)" % plateau_patience)
                break
            # else: plateaued but on relabel-able failures - keep going, more data may help
```

This is the "know when to stop" answer, split correctly: a flat trend caused by
`POLICY_GAP`/`RESOURCE_STARVED` runs still has a plausible next-iteration payoff (more
corrected data), so the loop keeps spending iterations; a flat trend where every recent
run is `STRUCTURAL_GAP` **cannot** be fixed by another retrain, so grinding more
iterations is pure waste — stop and point at the playbook instead. (`reached_30_pop`
remains the success stop condition, unchanged.)

### 5.9 Trend log additions

Add `"stall_class"` and `"gap_flagged"` to the entry dict already appended to
`loop_trend.jsonl` (§6.8's `entry["stall_class"]` above) — this alone is what lets a human
(or a future analysis pass) distinguish "flat because we need more data" from "flat
because of a structural ceiling" just by reading the log, without re-deriving it.

---

## 6. Automatable now vs. needs an LLM/human

**Pure code, buildable directly on top of existing modules (no LLM in the hot loop):**
- In-run stall detection (§6.1) — reuses `replay._scan`'s existing thresholds, just called
  incrementally instead of post-hoc.
- Failure classification into RESOURCE_STARVED / POLICY_GAP / STRUCTURAL_GAP (§6.2) — a
  deterministic read of `meta.expert_top`, which is **already recorded** and merely unused
  today.
- True-DAgger relabeling (prefer `meta.expert_top` over the hand-written heuristic; exclude
  `"advance_time"` as a label) — strictly simpler than today's heuristic, not more code.
- Bounded exploration retry (§6.3) — a ranking perturbation + short replay, no model.
- Structural-gap aggregation into `playbook.json` via `coach.py`'s existing
  `reconcile`/`update_playbook` (§6.4) — reuses tested infra, one new adapter function.
- Champion/challenger retrain gate (§6.5) — arithmetic comparison + file backup/restore.
- Curriculum gap-penalty demotion (§6.6) — same shape as the existing `bias_ranking`.
- Across-run plateau detection + stop (§6.7) — arithmetic over the trend log.

**Needs an LLM or a human in the loop:**
- Actually **fixing** a structural gap: writing the new `planner.py` candidate logic, new
  `game_schema.py`/bridge action-space entries (verticality, water-infra placement
  routing, spatial features) that `docs/kb/placement-verticality-gaps.md` already
  catalogs. This is code authorship against game-mechanics knowledge; DGM/STOP show an LLM
  *can* do this class of self-directed patch, validated empirically by rerunning the loop
  afterward, but both keep it a separate, reviewed step rather than an inline call from the
  training loop, and this design follows that precedent deliberately (cost/safety, and
  `run_loop.py`'s current value as a hermetic, mock-tested, no-network unit is worth
  protecting).
- Turning an aggregated `structural_gap:*` playbook entry into a written root-cause
  hypothesis (Reflexion-style narrative) is a nice-to-have batch job over
  `playbook.json`, not a per-iteration requirement — the pure-code tag + evidence is
  already enough to route correctly and to prioritize (highest-`evidence.runs` first).
- Judging whether a proposed planner/placement patch is actually correct beyond what unit
  tests cover — ordinary code review, human or LLM, same as any change to this repo.

---

## Sources

- Reflexion — Shinn et al., [arXiv:2303.11366](https://arxiv.org/abs/2303.11366)
- Voyager — Wang et al., [arXiv:2305.16291](https://arxiv.org/abs/2305.16291) / [project site](https://voyager.minedojo.org/)
- DAgger — Ross, Gordon & Bagnell, summarized at [roboticscenter.ai/glossary/dagger](https://www.roboticscenter.ai/glossary/dagger)
- Hindsight Experience Replay — [emergentmind.com/topics/hindsight-experience-replay](https://www.emergentmind.com/topics/hindsight-experience-replay); AgentHER, [arXiv:2603.21357](https://arxiv.org/html/2603.21357v3)
- Prioritized Level Replay — Jiang et al., [arXiv:2010.03934](https://arxiv.org/abs/2010.03934)
- POET — Wang et al., [arXiv:1901.01753](https://arxiv.org/abs/1901.01753)
- Quality-Diversity overview — [emergentmind.com/topics/quality-diversity-algorithm](https://www.emergentmind.com/topics/quality-diversity-algorithm)
- AlphaZero self-play/evaluate loop — [Wikipedia](https://en.wikipedia.org/wiki/AlphaZero); [OpenSpiel docs](https://openspiel.readthedocs.io/en/stable/alpha_zero.html)
- "Where LLM Agents Fail and How They Can Learn From Failures" — [arXiv:2509.25370](https://arxiv.org/pdf/2509.25370)
- Darwin Gödel Machine — Zhang et al., [arXiv:2505.22954](https://arxiv.org/abs/2505.22954) / [sakana.ai/dgm](https://sakana.ai/dgm/)
- STOP (Self-Taught Optimizer) — Zelikman et al., [arXiv:2310.02304](https://arxiv.org/abs/2310.02304)
- Stuck-agent detection patterns — [dev.to: How to Detect When Your AI Agent Is Stuck](https://dev.to/clawgenesis/how-to-detect-when-your-ai-agent-is-stuck-and-what-to-do-about-it-ce9); [dev.to: stalled tasks and timeouts](https://dev.to/bobrenze/how-ai-agents-handle-stalled-tasks-and-timeouts-lessons-from-my-production-failure-1jj9)

**Code read (this repo, read-only):** `agent/run_loop.py`, `agent/replay.py`,
`agent/nlp/learn.py`, `agent/nlp/play_policy.py`, `agent/nlp/policy.py`,
`agent/curriculum.py`, `agent/coach.py`, `agent/metrics.py`, `agent/discovery.py`,
`agent/game_schema.py`, `agent/economy.py`, `agent/controller.py` (signatures),
`agent/nlp/labeler.py` (`_to_schema_id`), `agent/playbook.json`,
`docs/kb/placement-verticality-gaps.md`, `docs/kb/aiplayer-architecture.md`,
`docs/superpowers/plans/2026-07-11-timberborn-full-game-completion.md`.
