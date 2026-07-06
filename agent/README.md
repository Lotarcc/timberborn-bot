# Agent Learning Loop

These files are the first local scaffold for the learning loop described in `docs/learning-system.md`. They do not drive TimberBridge or change `agent/play.py`; they prepare memory that the player agent can retrieve before future decisions.

## `kb.py`

`kb.py` is the static `KB` tier. It loads `docs/kb/*.md` plus `docs/knowledge/survival-basics.md`, splits each markdown file into heading-sized concept chunks, and ranks chunks for a short query.

Default lookup tries the local Ollama embedder (`bge-m3` at `http://127.0.0.1:11434/api/embeddings`). If Ollama is not reachable or does not return embeddings, it falls back to dependency-free TF-IDF and rule-keyword scoring.

```sh
python3 agent/kb.py "how much water for a drought"
python3 agent/kb.py --no-embeddings -k 5 "badtide tank storage"
```

## `metrics.py`

`metrics.py` is the learning-curve summarizer for a `run journal`. It reads `agent/journal/<id>.jsonl`, tolerates missing files and malformed lines, prints a run summary, and appends the core curve fields to `agent/metrics.csv`.

Tracked fields include final cycle, peak/final population, buildings built, actions, errors, and final water/food snapshots when the journal contains those values.

```sh
python3 agent/metrics.py firstlife
python3 agent/metrics.py agent/journal/firstlife.jsonl
```

## `coach.py`

`coach.py` is the offline `coach` tier. Version 1 is intentionally rule-based: it reads a run journal plus metrics, detects simple failure patterns such as empty water storage, no completed construction, and bridge teaching errors, then writes reconciled lessons to `agent/playbook.json`.

The public seam is `analyze(journal, metrics) -> list[lesson]`, so a frontier or local LLM analyzer can replace the rule engine later without changing the playbook writer.

```sh
python3 agent/coach.py --run-id firstlife
python3 agent/coach.py --run-id firstlife --dry-run
```

## How They Fit

- `KB`: stable authored facts from `docs/kb/` and survival strategy seeds.
- `run journal`: append-only episodic record emitted by the player loop.
- `metrics.csv`: compact learning-curve rows for comparing runs.
- `playbook.json`: mutable lessons with evidence and confidence, retrieved before future decisions.

This keeps the project rule intact: the model weights do not change. Improvement comes from better retrieval, scored evidence, and offline retrospectives between runs.
