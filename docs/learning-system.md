# Learning system — how the agent improves across runs

The model's weights never change. Improvement is a **system property**: a growing, embedded, *scored* memory plus an offline coach that distills each run. This is the Voyager skill-library / Claude-Plays-Pokémon pattern, specialized to Timberborn micro-management (best water-storage layouts, feeding setups, needs management) at a bounded scope.

## Four memory tiers
| Tier | Mutable? | Written by | Read when |
|---|---|---|---|
| Knowledge base (KB) | static | authored once (see `docs/kb/`) | on-demand `kb_lookup` |
| Playbook (lessons) | grows | offline coach | before deciding (retrieved top-k) |
| Design library | grows + re-scored | coach proposes, evaluator scores | when building |
| Run journal (episodic) | append-only per run | the loop | source for the coach; not read live |

Strict separation: the KB is timeless ground truth; a bad lesson can never corrupt it. Lessons and designs carry confidence + evidence so wrong ones decay.

## Retrieval (the embedder's job)
Every KB chunk, lesson, and design is embedded (`nomic-embed-text` / `bge-m3`). At decision time the agent embeds a short query built from the current alerts + phase (e.g. `"drought in 3 days, water understocked, 14 beavers"`) and pulls the top-k most similar lessons and designs. This is how the agent *remembers the right thing at the right moment* instead of holding everything in context.

## Playbook lesson schema
```json
{
  "id": "L-0042",
  "trigger": "forecast drought AND water_days_remaining < drought_len+2",
  "situation": "cycle<=3, pop 10-20, single river map",
  "action": "pause discretionary builds; add Large Tanks until stored >= (D+2)*2.13*P; raise pump worker priority",
  "outcome": "survived drought with 1.5-day water buffer",
  "evidence": {"runs": 7, "wins": 6, "losses": 1},
  "confidence": 0.82,
  "created_run": 12, "last_seen_run": 31,
  "supersedes": ["L-0007"]
}
```
Reconciliation of contradictory lessons: prefer **higher confidence**, then **more evidence (runs)**, then **recency**. The loser is marked `superseded`, not deleted (keeps the history the coach reasons over).

## Design library — the "find the best layout" engine
A design is a **parametric, anchor-relative build recipe** plus a measured score distribution. Not a fixed blueprint — it scales with population `P` and drought length `D`.
```json
{
  "id": "D-water-03",
  "kind": "water_storage",
  "goal": "hold >= (D+2)*2.13*P water through a drought",
  "preconditions": ["river <= 6 tiles wide", "bedrock banks"],
  "recipe": [
    {"op":"dam","from":"anchor","across":"river"},
    {"op":"levee_wall","height":"H = ceil(needed_volume / reservoir_area)"},
    {"op":"place","spec":"LargeWaterTank","count":"ceil(P/8)","behind":"dam"}
  ],
  "params": {"P":"population","D":"forecast_drought_len"},
  "score": {"mean": 0.78, "n": 9, "metric": "water_buffer_days_end_of_drought"},
  "cost": {"logs": "~", "planks": "~", "science": "~"},
  "risk": "under-sized levee overtops during long wet season"
}
```
Designs start seeded from the KB `designs/` files (known-good community patterns), then the agent discovers variants and the evaluator ranks them.

## Evaluation harness — how "best or near-best" is measured
The only trustworthy signal is *outcome under controlled conditions*. Use `save`/`load` to make comparisons near-deterministic:

1. From a **checkpoint save** (e.g. "day 8, 3 days to first drought"), apply design variant A.
2. `set_speed` high, fast-forward N days, read `/state` metrics.
3. `load` the same checkpoint, apply variant B, repeat.
4. Score each on the design's metric (water buffer days, food buffer, wellbeing, beaver-days survived, build cost/footprint efficiency).
5. Update each design's score distribution; **promote** the incumbent only when a challenger beats it with enough samples (avoid promoting on one lucky run).

This A/B-from-a-fixed-save loop is the core research tool and doubles as the regression test suite (a library of hard checkpoint saves the agent must keep surviving).

## The improvement loop (per run → coach → next run)
```
run N:  play using KB + retrieved lessons + best designs; append every
        decision + outcome to the run journal
        │
        ▼
coach (offline, bigger/slower model or Claude API):
        read journal + final metrics →
          • what killed or saved the colony?
          • append/update playbook lessons (adjust confidence from evidence)
          • propose new design variants to try
          • flag KB numbers that reality contradicted (→ verify vs blueprints)
        │
        ▼
run N+1: retrieves the improved lessons/designs; evaluator A/B-tests the
         new variants against incumbents from checkpoint saves
```

## The learning curve (what "getting better" means, and how we prove it)
A curriculum of escalating goals, each unlocking finer micro-optimization once the prior is reliable:

| Stage | Goal | Unlocks |
|---|---|---|
| 1 | survive first drought | water/food storage sizing lessons |
| 2 | survive to cycle 5 | pump/farm placement designs, worker priorities |
| 3 | stable positive well-being | wellbeing building order, housing clusters |
| 4 | thrive on harder maps / badtide | badtide isolation designs, reservoir engineering |

Tracked metrics per run (plotted over runs = the curve): droughts survived, cycle reached, peak/final population, final well-being, water/food buffer days at each drought, build-cost efficiency. Improvement is real if these trend up at **flat inference cost** — no bigger model, just better memory.

## Guardrails against false learning
- Promote designs/lessons only with `n >= 3` samples and a margin, so noise isn't mistaken for skill.
- Log every silent cap (top-k truncation, dropped variants) so "we tried everything" is never assumed.
- Keep superseded entries; the coach needs the failure history to avoid re-proposing dead ends.
- The evaluator's checkpoints are the regression suite — a change that improves stage 3 must not regress stage 1.
