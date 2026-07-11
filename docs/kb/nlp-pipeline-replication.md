# NLP_2.0_pipeline — Reverse-Engineered Replication Spec

Source: https://github.com/shasankp000/NLP_2.0_pipeline (trains the intent models used by
https://github.com/shasankp000/AI-Player, a Minecraft "second player" mod).

This document is a precise, replicate-from-scratch spec of that training pipeline, followed by a
mapping to our **Timberborn autonomous agent** domain.

---

## 0. What the pipeline actually is

It is a **3-class text intent classifier**, trained **three different ways** on the *same* tiny
seed dataset, then combined at inference by an LLM "decision resolver". The three learners:

1. **LIDSNet** — a small PyTorch MLP over **symbolic one-hot features** (POS tags, lemmas, WH-words,
   NER, sentiment). Exported to **TorchScript** and run inside the Java mod via DJL.
2. **CART** — an sklearn `DecisionTreeClassifier` over the *same* symbolic feature space, exported as
   a hand-rolled JSON tree (`cart_tree.json` + vocab + labels) and re-implemented in Java.
3. **(In AI-Player only)** a fine-tuned **distilBERT** — *not* trained in this repo; trained elsewhere.

The 3 intent classes: **`REQUEST_ACTION`**, **`ASK_INFORMATION`**, **`GENERAL_CONVERSATION`**
(the Java side adds a 4th sentinel `UNSPECIFIED` used only by the LLM resolver, never a train label).

The whole thing is small-data + symbolic on purpose: the models are cheap "context" votes, and a
local LLM makes the final call.

### File map of the repo

| File | Role |
|---|---|
| `intent_classifier_test.py` | **Stage 1** – builds `final_dataset_raw.json` (raw text+label) from a hand-written seed + CLINC150 + synthetic augmentation. CART block at bottom is commented out. |
| `fp-growth-tree-symbolic-features.py` | **Stage 2** – converts text → symbolic features → `fp_symbolic_features.json` (text, label, features[]). |
| `fp-growth-tree-miner.py` | **Stage 2b** – FP-Growth pattern mining per class → `fp_growth_patterns.json` (analysis/inspection only; not wired into training). |
| `preprocessing_layer.py` | The symbolic feature extractor (spaCy + VADER). Shared by CART + LIDSNet inference. |
| `lidsnet_trainer.py` | **Stage 3a** – trains the LIDSNet MLP on `fp_symbolic_features.json` → `lidsnet_model.pt` + `lidsnet_feature_map.json`. |
| `lidsnet_export_torchscript.py` | **Stage 3b** – loads `.pt` state dict, `torch.jit.trace` → `LIDSNet_torchscript/LIDSNet_intent_detect.pt`. |
| `lidsnet_test.py` | LIDSNet inference sanity check (text → features → vector → softmax). |
| `train_cart.py` | **Stage 3c** – trains CART on symbolic features (reads `test.json`), exports `cart_tree.json`, `cart_vectorizer_vocab.json`, `cart_class_labels.json`. |
| `test_cart.py` | Pure-Python re-implementation of tree traversal for inference (mirrors the Java `CartClassifier`). |
| `final_dataset_raw.json` | `{label: [text,...]}` dict, 3 keys. **410 rows** (REQUEST_ACTION 132, ASK_INFORMATION 151, GENERAL_CONVERSATION 127). |
| `test.json` | `{label: [text,...]}`, the actual input to `train_cart.py`. 552 rows (RA 132 / AI 293 / GC 127). A hand-edited superset of `final_dataset_raw.json`. |
| `fp_symbolic_features.json` | 600 rows `{text,label,features[]}`, input to LIDSNet + FP-Growth. |
| `dum.py` | Throwaway: a literal dump of the 268 feature names. |
| `dfa.py`, `turing_machine.py` | Unrelated toy automata — **ignore**. |

> **Important inconsistency to know before replicating:** the three stages were run at different
> times and are *not* perfectly consistent.
> - `preprocessing_layer.py` (current) returns a **flat `List[str]`** of features
>   (`POS=`, `lemma=`, `WH=`, `sentiment=`). It does **not** emit NER.
> - `fp-growth-tree-symbolic-features.py` calls `preprocessor.process(text)` and then indexes
>   `data["lemmas"]`, `data["pos_tags"]`, `data["entities"]` — i.e. it expects an **older dict-returning
>   version** of the preprocessor that emitted NER but no sentiment. The committed
>   `fp_symbolic_features.json` / `lidsnet_feature_map.json` (268 features incl. `NER=*`, no `sentiment=*`)
>   were produced by that older version.
> When you replicate, **pick one feature contract and make all three stages use it.** See §6.

---

## 1. LIDSNet architecture (exact)

From `lidsnet_trainer.py` / `lidsnet_export_torchscript.py` / `lidsnet_test.py` — identical class in all three:

```python
class LIDSNet(nn.Module):
    def __init__(self, input_dim, hidden=256, num_classes=3):
        super().__init__()
        self.model = nn.Sequential(
            nn.Linear(input_dim, hidden),   # input_dim = #features (268 as committed)
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(hidden, hidden // 2), # 256 -> 128
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(hidden // 2, num_classes)  # 128 -> 3
        )
    def forward(self, x):
        return self.model(x)
```

- **Type:** plain feed-forward MLP. "LIDSNet" is just the author's name for it; there is **no
  embedding layer, no tokenizer, no RNN/attention**. The "embedding" is the **one-hot symbolic
  feature vector** built from the feature map.
- **Input:** dense float32 vector of length `input_dim` = number of distinct symbolic features
  (`len(feature_names)`; **268** in the committed artifact). Values are 0.0/1.0 (multi-hot).
- **Layers:** `Linear(D,256) → ReLU → Dropout(0.3) → Linear(256,128) → ReLU → Dropout(0.3) → Linear(128,3)`.
- **Output:** raw logits `[batch, 3]`; softmax applied at inference only.
- **Loss:** `nn.CrossEntropyLoss()` (expects integer class indices).
- **Optimizer:** `Adam`, `lr=0.001`, default betas/eps, no weight decay, no scheduler.
- **Hyperparameters (module constants):** `MAX_FEATURES = 2000` (declared, **unused**), `BATCH_SIZE = 32`,
  `EPOCHS = 25`, device = cuda if available else cpu.
- **Train/test split:** `train_test_split(X, y, test_size=0.2, stratify=y)` — no fixed seed, no val set.
- **Training loop:** vanilla; `zero_grad → forward → CrossEntropy → backward → step`; sums `loss.item()`
  per epoch; prints `classification_report` on the held-out 20% at the end.
- **Label encoding:** `label2idx = {label: i for i,label in enumerate(sorted(set(labels)))}` →
  alphabetical: `ASK_INFORMATION=0, GENERAL_CONVERSATION=1, REQUEST_ACTION=2`.
- **Feature encoding:** `MultiLabelBinarizer().fit_transform(features)` — one column per distinct feature
  string; `mlb.classes_` (sorted) is the canonical feature order saved to the feature map.

### Saved artifacts
```python
torch.save(model.state_dict(), "lidsnet_model.pt")   # weights only
json.dump({"label2idx", "idx2label", "features": mlb.classes_.tolist()}, "lidsnet_feature_map.json")
```

### TorchScript export mechanics (`lidsnet_export_torchscript.py`)
1. Re-declare the identical `LIDSNet` class.
2. Read `lidsnet_feature_map.json` → `input_size = len(features)`, `num_classes = len(label2idx)`,
   `hidden=256`.
3. `model = LIDSNet(...)`; `model.load_state_dict(torch.load("lidsnet_model.pt", map_location=...))`;
   `model.eval()`.
4. `dummy_input = torch.zeros(1, input_size)`.
5. `traced = torch.jit.trace(model, dummy_input)` — **tracing**, not scripting (safe: no data-dependent
   control flow; dropout is inert in eval).
6. `traced.save("./LIDSNet_torchscript/LIDSNet_intent_detect.pt")` — this `.pt` + a copy of
   `lidsnet_feature_map.json` are the two files shipped to the Java/DJL side.

### Inference (`lidsnet_test.py`, and mirrored in Java `LIDSNetTranslator`)
- `text → PreprocessingLayer.process(text) → set of feature strings`.
- Build vector in **feature-map order**: `vec[i]=1.0 if feature_names[i] in features`.
- `unsqueeze(0)` → `[1, D]`; `softmax(logits, dim=1)`; `argmax` → `idx2label`; confidence = max prob.
- Java side (`LIDSNetTranslator`): `NDArray.create(input).reshape(1, input.length)` →
  `output.softmax(1)` → `new Classifications(classNames, prob)`. Feature extraction there uses **OpenNLP**
  (POS + lemma) not spaCy, so the Java feature strings must match the Python `feature_map` vocabulary.

---

## 2. Symbolic feature extractor — `preprocessing_layer.py` (every step, in order)

```python
class PreprocessingLayer:
    def __init__(self, model="en_core_web_sm"):
        self.nlp = spacy.load(model)                    # spaCy small English
        self.vader = SentimentIntensityAnalyzer()       # VADER
```

`process(message) ->` sorted-unique `List[str]`:
1. `doc = self.nlp(message)` — tokenize + POS + lemma.
2. For each token **where `token.is_alpha`** (drops punctuation/numbers):
   - `tokens.append(token.text)`
   - `pos_tags.append("POS=" + token.tag_)`   ← fine-grained Penn Treebank tag (`token.tag_`, not `pos_`).
   - `lemmas.append("lemma=" + token.lemma_)`
   - if `token.tag_ in {WDT, WP, WRB, WP$, MD}`: `wh_tags.append("WH=" + token.text.lower())`.
3. **VADER** on the *raw message*: `compound >= 0.05 → sentiment=positive`; `<= -0.05 → negative`;
   else `neutral`. Exactly one sentiment tag.
4. `features = pos_tags + lemmas + wh_tags + sentiment_tags`; return `sorted(set(features))`.

**Feature vocabulary namespaces:** `POS=<tag>`, `lemma=<lemma>`, `WH=<word>`, `sentiment=<pos|neg|neutral>`
(current version). The older committed artifacts additionally have `NER=<entitylabel>` and **no** `sentiment=`.

---

## 3. FP-Growth symbolic miner — `fp-growth-tree-miner.py`

- Reads `fp_symbolic_features.json`; groups the `features[]` transactions **per class**.
- `mlxtend.TransactionEncoder` → one-hot DataFrame per class.
- `mlxtend.frequent_patterns.fpgrowth(df, min_support=0.3, use_colnames=True)`.
- Sorts by support desc, keeps `TOP_N=20`; frozensets → sorted lists; writes `fp_growth_patterns.json`
  (`{label: [{support, itemsets[]}]}`).
- **What it mines:** frequent *co-occurring symbolic feature itemsets* per intent (e.g. REQUEST_ACTION
  top pattern is `{POS=VB}` at support 0.9, then `{POS=NN}`, then `{POS=VB, POS=NN}`).
- **How it feeds CART:** it does **not** feed CART programmatically in the committed code. It is an
  *interpretability / feature-discovery* artifact confirming which symbolic features discriminate each
  class. CART then learns splits over the full one-hot feature space directly. (The repo name implies an
  intended "FP-Growth → CART" coupling; in practice the coupling is conceptual, not code.)

---

## 4. CART classifier — `train_cart.py`

- **Library:** `sklearn.tree.DecisionTreeClassifier`.
- **Input:** `test.json` (`{label:[text]}`) flattened to `(text,label)` pairs.
- **Features:** `PreprocessingLayer().process(text)` per row → `MultiLabelBinarizer().fit_transform(...)`
  (same symbolic multi-hot space as LIDSNet).
- **Labels:** the 3 class strings directly (sklearn handles string classes; `clf.classes_` is sorted
  alphabetical).
- **Params:** `DecisionTreeClassifier(max_depth=6)` — everything else default (Gini criterion, no
  `random_state`, no class weights). Split `test_size=0.2, stratify=y_cart`.
- **Exports (custom JSON, not pickle):**
  - `cart_tree.json` — recursive `export_cart_tree()`: each node is either
    `{"type":"split","feature":<name>,"threshold":<float ~0.5>,"left":...,"right":...}` or
    `{"type":"leaf","class":<int argmax>,"class_counts":[...],"confidence":<max/total>}`.
  - `cart_vectorizer_vocab.json` — `{feature_name: column_index}` (the multi-hot vocabulary).
  - `cart_class_labels.json` — `clf.classes_.tolist()` (index → label; `["ASK_INFORMATION",
    "GENERAL_CONVERSATION","REQUEST_ACTION"]`).
- **Inference (`test_cart.py` / Java `CartClassifier`):** build multi-hot vector in vocab order; walk the
  JSON tree: at each split, if `vec[feat_idx] <= threshold` go left else right; missing feature → default
  left; leaf returns `class_labels[class]` + `confidence`.
- `per_class_cart/` (`cart_action.json`, `cart_information.json`, `cart_general.json`) and `cart_main/`
  are alternate one-vs-rest / bundled exports produced by unshown variants; the mainline is the single
  3-class `cart_tree.json`.

---

## 5. Dataset schema, size, and label provenance

### `final_dataset_raw.json` (Stage-1 output)
Committed form is a **dict** `{label: [text, ...]}` with keys `REQUEST_ACTION`, `ASK_INFORMATION`,
`GENERAL_CONVERSATION` (410 texts total: 132 / 151 / 127). Note the *generating script* writes a **list
of `{"text","label"}` dicts** — the committed file was later reshaped by hand into the dict form
(`test.json` is the dict form actually consumed by `train_cart.py`, hand-expanded to 552 rows).

### `fp_symbolic_features.json` (Stage-2 output, LIDSNet/FP-Growth input) — 600 rows
```json
{ "text": "Could you build a house near the village?",
  "label": "REQUEST_ACTION",
  "features": ["POS=.","POS=DT","POS=IN","POS=MD","POS=NN","POS=PRP","POS=VB",
               "WH=could","lemma=?","lemma=a","lemma=build","lemma=could",
               "lemma=house","lemma=near","lemma=the","lemma=village","lemma=you"] }
```
Fields: `text` (str), `label` (one of 3), `features` (sorted-unique symbolic strings).
200 rows per class (`MAX_PER_CLASS=200`).

### `lidsnet_feature_map.json` — the label + feature contract
`{"label2idx":{ASK_INFORMATION:0,GENERAL_CONVERSATION:1,REQUEST_ACTION:2}, "idx2label":{...},
"features":[...268 sorted feature strings incl. NER=*, POS=*, WH=*, lemma=* ...]}`.

### Label space
3 mutually-exclusive classes. `UNSPECIFIED` exists only as an inference-time escape hatch in AI-Player.

### How labels were generated/collected (reverse-engineered)
1. **Hand-authored seed** (`intent_classifier_test.py` `examples` dict): ~50 REQUEST_ACTION, ~20
   ASK_INFORMATION, ~20 GENERAL_CONVERSATION Minecraft-flavored sentences, each *manually* assigned to a class.
2. **CLINC150 mapping:** loads HuggingFace `clinc_oos/small`; a hand-written `intent_mapping` maps CLINC
   intents (`set_alarm`, `transfer_money`, `time`, `greeting`, …) into the 3 classes; ≤100 dedup samples/class merged in.
3. **Emotional augmentation:** prepend fixed per-class phrases (`"Please"`, `"Could you"`, `"I'm wondering,"`,
   `"Honestly,"`, …) to each seed → new labeled rows (label inherited).
4. **Synthetic combination:** pick random pairs of same-class sentences, extract WH/verb/noun chunks with
   spaCy, splice into `"{wh} {verb} the {noun}?"`, then produce a `language_tool_python` grammar-corrected
   variant; both keep the source class label.
Labels are therefore **weak/programmatic**: human-seeded, then propagated by rule through augmentation and
external-dataset mapping. No manual per-row annotation of the augmented data.

---

## 6. End-to-end train → export → inference (actual commands)

```bash
# ---- environment ----
python -m venv .venv && source .venv/bin/activate
pip install torch scikit-learn spacy vaderSentiment mlxtend pandas numpy \
            datasets language_tool_python tqdm
python -m spacy download en_core_web_sm

# ---- Stage 1: raw text dataset ----
python intent_classifier_test.py          # -> final_dataset_raw.json  (downloads clinc_oos/small)

# ---- Stage 2: symbolic features ----
#   (fix the preprocessor/feature contract mismatch first — see note below)
python fp-growth-tree-symbolic-features.py  # -> fp_symbolic_features.json
python fp-growth-tree-miner.py              # -> fp_growth_patterns.json (optional, analysis)

# ---- Stage 3a/3b: LIDSNet ----
python lidsnet_trainer.py                 # -> lidsnet_model.pt + lidsnet_feature_map.json
python lidsnet_export_torchscript.py      # -> LIDSNet_torchscript/LIDSNet_intent_detect.pt
python lidsnet_test.py                     # sanity check

# ---- Stage 3c: CART ----
python train_cart.py                      # -> cart_tree.json, cart_vectorizer_vocab.json, cart_class_labels.json
python test_cart.py                        # sanity check
```

**Consistency fix required to actually reproduce:** either (a) restore the dict-returning preprocessor
that `fp-growth-tree-symbolic-features.py` expects (`{"lemmas":[(w,l)],"pos_tags":[(w,t)],"entities":[{label}]}`),
or (b) rewrite that script to consume the current flat-list preprocessor. Also make `train_cart.py` read
from the same feature contract so CART and LIDSNet share vocabulary. Pin scikit-learn (tree JSON export is
version-tolerant; the model pickle is not — this repo avoids pickle for exactly that reason).

### Inference-time consumption in AI-Player (Java/DJL)
`NLPProcessor.getIntention()` runs **BERT (distilBERT) + CART + LIDSNet** in parallel, each yielding
`(label, confidence)`. A `DecisionResolver` formats all three as text and asks a **local Ollama LLM** to
pick the final class — explicitly told *"DO NOT TRUST THE CLASSIFIERS"* and to output exactly one of
`REQUEST_ACTION | ASK_INFORMATION | GENERAL_CONVERSATION | UNSPECIFIED`. If local inference fails,
`getIntentionFromLLM()` classifies from scratch via the LLM. There are **no numeric confidence
thresholds** — the classifiers are advisory context, the LLM arbitrates. The resolved intent then routes
to the mod's action/tool layer (function-calling) vs. a Q&A/RAG path vs. plain chat.

---

## 7. Python dependencies (no requirements.txt exists — reconstructed)

```
torch                 # LIDSNet MLP + TorchScript trace
scikit-learn          # DecisionTreeClassifier, MultiLabelBinarizer, train_test_split, metrics
spacy                 # tokenize/POS/lemma; model en_core_web_sm
vaderSentiment        # sentiment tags
mlxtend               # TransactionEncoder + fpgrowth
pandas, numpy         # FP-Growth dataframes, tree export
datasets              # HuggingFace clinc_oos/small
language_tool_python  # grammar correction of synthetic sentences (needs Java runtime)
tqdm
```
Plus `python -m spacy download en_core_web_sm`. `language_tool_python` pulls a Java-based LanguageTool
server on first run.

---

# MAPPING TO TIMBERBORN AUTONOMOUS AGENT

Our agent plays Timberborn via an HTTP bridge exposing **STATE** (resources, buildings, population,
per-tile map grids: reachable/on_road/moist/height) and **ACTIONS** (`place_building`,
`designate_cutting`, `demolish`, `advance_time`, `set_priority`). The NLP_2.0 pattern maps cleanly onto
**"classify the current game situation, then emit the next action."** We are replacing *"utterance →
intent+slots"* with *"game STATE → ACTION (verb) + slots (building type / location / target)."*

### Component-by-component mapping

| NLP_2.0 | Timberborn analogue |
|---|---|
| Input utterance (raw text) | Current **game STATE** snapshot from the bridge (`/state` + `/map` grids). |
| `PreprocessingLayer` (spaCy POS/lemma/WH/VADER → symbolic strings) | **StateFeaturizer**: turn STATE into a bag of discrete symbolic feature strings (see below). |
| One-hot `MultiLabelBinarizer` vocab (`feature_map`) | Fixed **feature vocabulary** of all possible state-feature strings, saved as `state_feature_map.json`. |
| Intent label (`REQUEST_ACTION`/…) | **Action verb** label: `PLACE_BUILDING` / `DESIGNATE_CUTTING` / `DEMOLISH` / `ADVANCE_TIME` / `SET_PRIORITY` / `NO_OP`. |
| Slots (implicit; WH/verb/noun chunks) | **Action arguments**: which building type, which tile/region, which priority — a second head/model. |
| FP-Growth per-class patterns | Mine which STATE features co-occur with each chosen action (interpretability + feature pruning). |
| CART tree (`max_depth=6`, JSON export) | A **decision-tree policy** over state features → action verb; JSON-exportable, auditable, fast, embeddable with no runtime deps. |
| LIDSNet MLP over multi-hot | A **small MLP policy** over the multi-hot state vector → action verb logits; TorchScript-exportable if we want it in-process. |
| distilBERT third voter | Optional: an LLM/embedding voter over a *textual serialization* of the state. |
| DecisionResolver (LLM arbitrates 3 votes) | **Our existing "LLM only at forks"** controller: deterministic policies (CART/MLP) propose; LLM breaks ties only on hard/low-agreement states. This matches our MVP architecture memory exactly. |

### Concrete feature vector (StateFeaturizer output — multi-hot strings)

Emit sorted-unique discrete tokens so the exact same one-hot machinery applies. **Bucketize
continuous quantities** (this is the key move — decision trees and multi-hot MLPs want categorical, not raw
floats):

```
# resources (bucketed: none/low/med/high by threshold)
food=low  water=high  logs=med  planks=none  science=low
food_days=<3  food_days=3-10  food_days=10+
# population
pop=12  pop_bucket=small  beavers_idle=yes  housing_free=0
# buildings present (existence + count buckets)
has=Lumberjack  has=WaterPump  count=House>=2  missing=Warehouse
# map / geometry facts from grids
flat_tiles_near_dc=high  moist_tiles=high  forest_reachable=yes
river_adjacent=yes  drought_active=no  season=dry
# lifecycle / progression
tick_bucket=early  no_food_production=yes  unreached_goal=housing
```

Namespaces mirror NLP's `POS=`/`lemma=`/`WH=`: use `resource=`, `has=`, `count=`, `map=`, `need=`,
`season=`. Vocabulary is closed and saved to `state_feature_map.json` exactly like `lidsnet_feature_map.json`.

### Label space (two-level, to capture "intent + slots")

- **Head 1 — action verb** (the "intent", 6 classes incl. `NO_OP`): `PLACE_BUILDING`,
  `DESIGNATE_CUTTING`, `DEMOLISH`, `ADVANCE_TIME`, `SET_PRIORITY`, `NO_OP`.
- **Head 2 — slots** (conditioned on verb):
  - for `PLACE_BUILDING`: **building-type** class (Lumberjack, House, WaterPump, Farmhouse, …) — a second
    classifier over the same features. Location can stay **deterministic** (existing spatial/placement
    utility scorer picks the tile) so the learned part only chooses *what*, not *where*.
  - for `DESIGNATE_CUTTING`/`DEMOLISH`/`SET_PRIORITY`: target selected by existing heuristics.
Keeping location out of the label space keeps the classification problem small (mirrors NLP_2.0's tiny
3-class setup) while our proven spatial planner handles geometry.

### How to GENERATE the training dataset (behavioral cloning)

1. **Expert rollouts (primary signal):** run our existing **deterministic planner** (the controller
   curriculum that already emits good actions). At every decision point log
   `(STATE snapshot, chosen ACTION verb, chosen building-type slot)`. This is the analogue of the
   hand-authored seed — but *auto-labeled by a working expert*, so far higher quality than NLP_2.0's
   weak labels. A few hundred–thousand `(state, action)` pairs across varied maps/seeds.
2. **Perturbations (augmentation, = NLP's synthetic combos):** jitter the logged states — nudge resource
   counts across bucket boundaries, drop/add a building, toggle drought/season, resample a map region —
   and **re-query the deterministic planner** for the correct action on the perturbed state. This grows
   coverage and teaches bucket-boundary behavior. (Only relabel via the expert; never inherit a stale label
   through a perturbation that changes the right answer — an improvement over NLP_2.0's label-inheriting augmentation.)
3. **LLM-labeled hard states (= NLP's DecisionResolver, but at *train* time):** collect states where the
   deterministic planner is uncertain / low-margin / hits a fork, and have the **LLM label the correct
   action** with a rationale. These become high-value training rows for exactly the situations the cheap
   policy will later defer on. Dedup + balance per action class (mirror `MAX_PER_CLASS`).

Serialize as `{ "state_features": [...], "action_verb": "...", "building_type": "..." }` — structurally
identical to `fp_symbolic_features.json`. Then train **CART (`max_depth`≈6–10, JSON export)** and a
**small MLP (Linear→ReLU→Dropout→…→verbs)** on it, export CART to JSON and MLP to TorchScript, and at
runtime let both vote with the LLM arbitrating only low-agreement states.

### The single most important design decision

**Bucketize/discretize the continuous game STATE into a closed vocabulary of symbolic feature strings,
and keep spatial *where*-decisions in the existing deterministic planner — let the learned models decide
only the *what* (action verb + building type).** Everything else in NLP_2.0 (one-hot vocab, CART JSON
tree, tiny MLP, TorchScript, LLM tie-break) then transfers almost verbatim. This choice is what makes a
tiny, auditable, dependency-light behavioral-cloning classifier viable for a continuous-state colony sim,
and it aligns with our standing "rules/geometry in code, LLM only at forks" architecture.
