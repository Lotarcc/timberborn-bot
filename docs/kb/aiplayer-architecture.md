# AI-Player runtime architecture (reference for our Timberborn agent)

Source: https://github.com/shasankp000/AI-Player — a Fabric (Java) Minecraft mod that
drives a Carpet-style fake player from natural-language chat. Analysed from a shallow
clone; ~202 `.java` files. Paths below are under `src/main/java/net/shasankp000/`.

This doc documents **their working loop** and maps each piece onto **our** Timberborn
stack (HTTP bridge + Python planner/controller + `kb.py` RAG + local Ollama), then lists
concrete gaps we should close.

---

## 1. End-to-end loop: chat → intent → task chain → primitives → feedback → next

```
player types in chat
  │  (AIPlayerClient.java: ClientSendMessageEvents.CHAT, only if a bot name is mentioned)
  ▼
runFromChat → routeIntent            (LLMServiceHandler.java / ollamaClient.java)
  │
  ▼
NLP intent classification            (ChatUtils/NLPProcessor.getIntention)
  │   4-model ensemble → LLM "Decision Resolver" → one of:
  │   REQUEST_ACTION · ASK_INFORMATION · GENERAL_CONVERSATION · UNSPECIFIED
  ▼
switch(intent):
  ASK_INFORMATION / GENERAL_CONVERSATION ─► RAG2.run(...)      (talk only; retrieval-augmented)
  REQUEST_ACTION                          ─► FunctionCallerV2.run(...) / handleUserGoal(...)
  UNSPECIFIED                             ─► re-classify via pure-LLM fallback, then route
```

For an action request the task-chaining engine (`FunctionCallerV2`) runs this cascade
(`handleUserGoal`, FunctionCallerV2.java:180):

1. **Goal parse** — `GoalMapper.parseGoal(text)` → a numeric goal id (1–9) via a
   weighted-token/synonym scorer (<1 ms); on `GOAL_UNKNOWN` an async **edge-LLM**
   (default `smollm2:135m`, 3 s timeout) does single-label classification
   (GoalMapper.java:64–120).
2. **Symbolic planner (hybrid)** — `HybridPlanner.buildPlan` runs **A\*** over an action
   graph, scoring nodes by semantic distance to the goal using `GoalVector`, a
   hand-written **64-dim keyword embedding** (GoalVector.java:13–60), plus risk.
3. **Symbolic planner (Markov)** — fallback `Planner.buildPlan`: beam search
   (`INITIAL_DRAFTS=4`, `BEAM_WIDTH=3`, `MAX_REFINEMENT_ITERS=6`, plan length 3–12) over
   sequences sampled from a `MarkovChain2` action-transition model, each candidate scored
   by `SequenceRiskAnalyzer` which wraps the RL agent (Planner.java:19–88).
4. **LLM fallback** — only if both planners return null or score worse than
   `SAFE_THRESHOLD*4`: `fallbackToLLM` asks the main Ollama model to emit a JSON
   **pipeline** (FunctionCallerV2.java:248).

**Primitive execution — the pipeline loop** (`runPipelineLoop`, FunctionCallerV2.java:1150):

- A plan is a JSON array of steps `{functionName, parameters:[{parameterName,parameterValue}]}`,
  loaded into a FIFO `ArrayDeque` and popped one at a time.
- **Data flow between steps is by placeholders.** A `parameterValue` of `"$foundBlock.x"`
  is resolved from a shared blackboard (`resolvePlaceholder`, :1660) before the call.
  Each tool, after running, writes its outputs back into that blackboard via a
  side-effect lambda declared in `ToolRegistry` (e.g. `searchBlocks` writes
  `foundBlock.{x,y,z,type}`; `detectBlocks` writes `lastDetectedBlock.*`). This is how a
  high-level "fetch wood" becomes `searchBlocks → goTo($foundBlock) → mineBlock($foundBlock)`.
- **Environment feedback / verification.** After each `callFunction`, `parseOutputValues`
  updates the blackboard, then a per-tool `ToolVerifiers.StateVerifier` re-reads live game
  state to confirm the effect actually happened (e.g. bot really moved / block gone).
- **Replan on failure.** If a placeholder is `__UNRESOLVED__` or the verifier fails, the
  loop re-prompts the LLM with the list of successfully executed steps + the failure
  reason, and the LLM either returns a **new pipeline** (the deque is cleared and
  refilled) or a `clarification` question relayed to the player. Hard cap `maxRetries=3`.

So the "next decision" comes from two places: the deterministic verifier→LLM-replan loop
(per action request) and, orthogonally, the RL agent that continuously scores survival
actions on world events (below).

**Available primitive tools** (`ToolRegistry.TOOLS`): `goTo`, `detectBlocks`, `turn`,
`look`, `mineBlock`, `placeBlock`, `searchBlocks`, `getOxygenLevel`, `getHungerLevel`,
`getHealthLevel`, `webSearch`. Each has a name, natural-language description (fed to the
LLM), a typed parameter list, the set of blackboard keys it produces, and the writer lambda.

---

## 2. NLP intent model

Not a single OpenNLP categorizer — a **4-member ensemble fused by an LLM** (`NLPProcessor.getIntention`, NLPProcessor.java:438):

| Member | Tech | Role | Labels |
|---|---|---|---|
| DistilBERT (fine-tuned, TorchScript) | DJL / PyTorch | primary neural classifier | ASK_INFORMATION, GENERAL_CONVERSATION, REQUEST_ACTION (BertModelManager.java:67) |
| CART decision tree | JSON tree + BoW vector | secondary vote | same 3 |
| LIDSNet (TorchScript) | DJL, symbolic POS/lemma features | secondary vote | same 3 |
| Apache OpenNLP | sentence/token/POS/lemma | **preprocessing** feeding LIDSNet | — |

The three votes (label + confidence) go to `DecisionResolver.resolveIntent`, which prompts
an LLM (Ollama by default) to pick the final label; the reply is normalized by substring
match to the enum `Intent {REQUEST_ACTION, ASK_INFORMATION, GENERAL_CONVERSATION, UNSPECIFIED}`
(NLPProcessor.java:44; DecisionResolver.java:114). A pure-LLM path
(`getIntentionFromLLM`) is the retry when the ensemble says `UNSPECIFIED`.

**Model provenance:** none of the model artifacts (`.bin/.pt/.json`) or training `.txt`
are in the repo. `NLPProcessor.ensureLocalNLPModel()` downloads + SHA-verifies + unzips
them at first run from the project's GitHub Releases into
`<configDir>/ai-player/NLPModels/...`. Training was done offline.

**How intent output is consumed** (`routeIntent`, LLMServiceHandler.java:208 / ollamaClient.java:163):

```java
switch (intent) {
  case GENERAL_CONVERSATION, ASK_INFORMATION -> RAG2.run(message, botSource, intent, client);
  case REQUEST_ACTION                        -> { new FunctionCallerV2(...); FunctionCallerV2.run(message, client); }
  default /* UNSPECIFIED */                  -> { retry = getIntentionFromLLM(); route retry or throw intentMisclassification; }
}
```

The intent switch chooses **talk (RAG2) vs act (FunctionCallerV2)**. It does *not* pick a
specific task — that is the `GoalMapper`+planner cascade inside the action branch.

---

## 3. RAG system

- **What is embedded:** whole runtime strings, **one vector per row, no chunking**. Three
  writers: conversation turns (`type=conversation`, prompt+response stored, vector = embedding
  of the *user prompt*), tool executions (`type=function_call`, vector = embedding of the
  command), and a first-boot greeting.
- **Model:** provider-selected via JVM prop `aiplayer.llmMode`. **Default = Ollama
  `nomic-embed-text`, 768-dim** (OllamaEmbeddingClient.java). OpenAI `text-embedding-3-small`
  1536; Gemini `text-embedding-004`; Anthropic has no embedding endpoint and falls back to
  Ollama/Voyage. No dimension enforcement on the column (mixing providers corrupts recall).
- **Store & query:** single SQLite table (below). Vectors are written as **TEXT literals**
  `"[0.1,0.2,...]"`. Retrieval = **brute-force full-table cosine scan** filtered by `type`,
  `ORDER BY similarity DESC LIMIT ?` (SQLiteDB.findRelevantMemories:102). **`TOP_K = 5`**
  (RAG2.java:28); the `ASK_INFORMATION` path uses top-1 local recall blended with a live
  web search (trusts web unless empty; else local only if similarity ≥ 0.8).
- **Injection:** retrieved rows are formatted into a plain-text `Context:` block
  ("Relevant conversations: - Prompt/Response/Similarity ...") prepended to the user prompt
  under a system prompt telling the model to treat them as past events and never mention
  "memories."
- **Known bug (matters for us):** `cosine_distance` is only registered (Java UDF) on
  **Windows**; on macOS/Linux the query connection never defines it, so local vector recall
  throws and silently returns empty. A `type=event` recall bucket is queried but never
  written. Takeaway: their persistent-RAG recall is fragile; our static, tested `kb.py`
  cosine path is actually more robust.

---

## 4. Q-table / RL navigation-survival learner

Tabular Q-learning (`GameAI/RLAgent.java`), separate from the chat loop. It is the bot's
reactive survival/combat controller, updated on world events (damage, entity proximity,
death), not chosen by the intent switch.

- **State** (`GameAI/State.java`, `Serializable`): a rich feature vector — bot x/y/z,
  health, hunger, oxygen, frost, distance-to-nearest-hostile, distance-to-danger-zone,
  time of day, dimension, hotbar/offhand/armor item names, selected item, nearby entities,
  nearby blocks, `inDangerousStructure`, plus per-action `riskMap`/`podMap`. State equality
  is **fuzzy** (`isStateConsistent`): categorical fields must match exactly; distances within
  a tolerance of 8; entity/block-name overlap ≥ 0.5. Q-lookup buckets spatially (±2) before
  the fuzzy compare.
- **Action** (`GameAI/StateActions.Action`, 24 values): MOVE_FORWARD/BACKWARD, TURN_LEFT/RIGHT,
  JUMP, STAY, SNEAK, SPRINT, STOP_* , USE_ITEM, ATTACK, SHOOT_ARROW, EQUIP_ARMOR, HOTBAR_1..9,
  EVADE.
- **Reward** (`calculateReward`, RLAgent.java:1569): hand-engineered integer sum — reward for
  safe distance / healthy HP / weapon+shield equipped / daytime / full oxygen; penalties for
  being too close to hostiles, low HP, low hunger/oxygen, night, dangerous dimensions;
  multiplied by a risk weight and reduced by probability-of-death (PoD).
- **Update:** standard Bellman
  `Q ← Q + α(reward + γ·maxₐ' Q(s',a') − Q)` with **α = 0.1, γ = 0.9**
  (RLAgent.java:1805). Action selection is **ε-greedy** with ε: 1.0 → decays ×0.99 per
  episode → floor 0.1, biased against high-PoD/death-risk actions.
- **Credit assignment on death** (`handleDeath`/`LookaheadLearning`): the last ≤10
  transitions get a retroactive penalty `DEATH_PENALTY_BASE=-100 × 0.8^(steps-before-death)`,
  and specific mistake patterns (attacking at low HP, staying amid multiple hostiles) are
  penalized further — so the bot "learns from mistakes."

---

## 5. Memory / persistence (what survives a restart)

Three independent stores:

| Store | Path | Format | Contents |
|---|---|---|---|
| RAG memory | `<gameDir>/sqlite_databases/memory_agent.db` | SQLite (WAL) | conversation history + function-call log |
| RL Q-table | `<gameDir>/qtable_storage/qtable.bin` | Java `ObjectOutputStream` | serialized `QTable` |
| RL aux | same dir: `lastKnownState.bin`, epsilon file | serialized `State` / raw `double` | resume RL across respawns/sessions |

RAG table (SQLiteDB.java:54):
```sql
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,               -- 'conversation' | 'function_call'
  timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
  prompt TEXT, response TEXT,
  embedding VECTOR                  -- actually a TEXT literal "[...]"
);
```
No index on `embedding` (brute force), no eviction (grows unbounded). Q-table is saved on
bot death/respawn and shutdown; conversation memory is written every turn. In-memory only:
cached embedding client, planner objects, transition history (last 100).

---

## 6. Component mapping: AI-Player → our Timberborn stack

| AI-Player component | Their tech | Our equivalent |
|---|---|---|
| Carpet fake-player + in-process tool methods (`Tools.goTo/mineBlock/placeBlock/searchBlocks`) | Direct Fabric/MC API calls | **HTTP bridge** action layer: `place`/`designate`/`demolish`/`advance_time`; readers `state`/`map`/`resources` |
| `ToolRegistry.TOOLS` (typed tool schema + blackboard writer per tool) | Java records + lambdas | Our bridge endpoint set + `agent/playbook.json` action vocabulary |
| Task-chaining engine `FunctionCallerV2` (pipeline stack, placeholder resolution, verify→replan) | Java + Ollama | **Python planner/controller**: `agent/planner.py` (enumerate work), `agent/controller.py` (deterministic execution + advance_time) |
| `GoalMapper` (NL → goal id) | token scorer + edge-LLM | our goal/curriculum selection in `planner.py` / `playbook.json` |
| Symbolic planners `HybridPlanner` (A\*+GoalVector) / `Planner` (beam+Markov) | Java | `agent/spatial.py` + `agent/placement.py` (utility-scored placement) + `planner.py` scoring |
| `ToolVerifiers.StateVerifier` (post-action live-state check) | Java | our bridge `state`/`map` re-reads + `agent/discovery.py` (effect verification) |
| RAG `SQLiteDB.memories` + `RAG2` (nomic-embed-text, cosine top-5) | SQLite + Ollama embed | **`agent/kb.py` + `agent/kb_index.json`** (bge-m3, 1024-dim, 128 chunks, hybrid 0.75·cosine+0.25·keyword) |
| Embedding model (`nomic-embed-text` 768) | Ollama | `bge-m3` (1024) via Ollama |
| Chat/decision LLM (`OllamaAPIHelper.smartChat`, selectable model) | Ollama / hosted | **local Ollama**: `qwen2.5:14b` (text), `qwen2.5vl:7b` (vision) |
| NLP intent ensemble (DistilBERT+CART+LIDSNet+resolver) | DJL + LLM | *(no equivalent — we route by explicit command/curriculum, not free chat)* |
| RL survival controller (`RLAgent` Q-table) | tabular Q-learning | *(no equivalent — our controller is deterministic; `agent/coach.py`/`metrics.py` are analytics, not RL)* |
| Persistence: `qtable.bin`, `lastKnownState.bin`, `memory_agent.db` | serialized + SQLite | `kb_index.json` (static), `agent/journal/`, `agent/metrics.csv`, `playbook.json` |

---

## 7. WHAT WE ARE MISSING

Concrete gaps between AI-Player's working loop and our current agent:

1. **A typed tool/blackboard registry with placeholder chaining.** Their killer primitive is
   `ToolRegistry`: every tool declares the shared-state keys it *produces*, and later steps
   reference them as `$foundBlock.x`. This lets a plan be a flat data-flow graph the executor
   resolves at runtime. Our controller wires outputs to inputs implicitly in Python. Adopting
   an explicit produced-keys blackboard would make multi-step chains (find tree → place
   lumberjack near it → route path) declarative and inspectable.

2. **Per-action post-execution verifiers with automatic replan.** `runPipelineLoop` re-reads
   live game state after every primitive and, on mismatch, re-prompts the LLM to rebuild the
   *remaining* pipeline (bounded to 3 retries) or ask the player a clarifying question. We
   have discovery/state readers but no standard "verify effect → replan tail" contract. This
   is the single most valuable pattern to port: it closes the loop between action and feedback.

3. **A clarification channel back to the user.** When a plan can't be resolved they emit a
   `clarification` string and pause, resuming when the player answers
   (`ChatContextManager.pendingClarification`). Our controller either proceeds or stalls; we
   have no structured "ask one question, wait, continue" mechanism.

4. **A fast NL→goal front door.** `GoalMapper`'s token scorer + tiny edge-LLM turns free text
   into a bounded goal id in <1 ms without waking the 14b model. We currently drive from
   explicit curriculum/commands; a cheap intent/goal parser would let a human type
   "get food going" and have it map to a known goal.

5. **Escalation ladder (symbolic-first, LLM-last).** Their order is deterministic planner →
   second deterministic planner → LLM only when both fail or are unsafe. Our controller is
   deterministic and our LLM is reserved for forks, which is close — but we lack an explicit
   *score gate* ("if planner confidence/score is bad, escalate to LLM") like their
   `SAFE_THRESHOLD*4` check. Worth formalizing.

6. **Cross-session experiential memory that actually influences decisions.** They persist and
   retrieve past conversations *and* function-call outcomes. Our `kb.py` RAG is **static
   documentation only** — it never records what happened in *this* game (what got built,
   what failed, what a site's terrain was). A lightweight run journal that is embedded and
   retrievable would give the planner episodic memory. (Note: keep our tested cosine path —
   their SQLite recall is broken on macOS/Linux, so this is a pattern to borrow, not their code.)

7. **A learned/adaptive layer for repeated failures.** Their RL agent + death-lookahead
   retroactively penalizes action sequences that led to bad outcomes. We have none — a
   repeated bad placement is repeated forever unless a human edits the planner. Even a
   non-RL "outcome memory" (down-rank action templates that failed here before, via
   `metrics.csv`/`coach.py`) would capture most of the benefit without a Q-table.

8. **Effect-scoped shared state / world blackboard.** Their `sharedState` map is the single
   source of truth threaded through a whole request. Our state is re-fetched per step from
   the bridge. A per-request cached blackboard (seeded from `state`/`map`, updated by action
   results) would cut bridge round-trips and make plans reproducible/testable offline.

Non-gaps (where we are already better): our RAG is chunked, tested, and dimension-stable
(bge-m3 1024) vs their unchunked, prompt-only, platform-broken SQLite recall; our
deterministic controller avoids their heavy reliance on the LLM to hand-author JSON pipelines.
