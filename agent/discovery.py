#!/usr/bin/env python3
"""
discovery.py — autonomous mechanics discovery for the Timberborn agent.

The agent should not only follow rules we hand it; it should DISCOVER how the game
works from its own play and remember it. This module reads a run journal, lines up
each action with the state change that followed it, and distills empirical
cause->effect lessons ("after designate_cutting, Log stock rose"; "placing a Lodge
dropped homeless"; "a GathererFlag alone did NOT raise food"). Those lessons are
emitted in the SAME shape coach.py uses, so they flow into the existing playbook
and get injected into future prompts — the agent teaches itself.

Two products:
  - observe_step(before, action, after)  -> live per-step effect strings, so the
    running agent sees "Log +6 after designate_cutting" in its own history and can
    adapt within a run.
  - distill(journal_rows)                -> accumulated mechanic lessons (playbook
    format) for cross-run memory.

Stdlib only. Tolerant of journal-shape drift: it reads whatever resource/population/
building snapshots and action records are present.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# A resource must move at least this much (absolute) in one step to count as an effect,
# so we don't learn from rounding noise.
RESOURCE_EPS = 1.0
# Goods we care about learning production/consumption for.
TRACKED_GOODS = ("Log", "Water", "Plank", "Berries", "Food", "Carrots", "Potatoes")


# ---------------------------------------------------------------------------
# Snapshot extraction — tolerant of the journal's evolving shape.
# ---------------------------------------------------------------------------
def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _resources(state: dict) -> dict:
    """good -> stored, from either a list[{good,stored}] or a dict{good:{stored}}."""
    out: dict[str, float] = {}
    if not isinstance(state, dict):
        return out
    res = state.get("resources")
    if isinstance(res, list):
        for r in res:
            if isinstance(r, dict) and r.get("good") is not None:
                out[str(r["good"])] = _num(r.get("stored"))
    elif isinstance(res, dict):
        for good, v in res.items():
            if isinstance(v, dict):
                out[str(good)] = _num(v.get("stored"))
            else:
                out[str(good)] = _num(v)
    return out


def _population_total(state: dict) -> float:
    if not isinstance(state, dict):
        return 0.0
    p = state.get("population")
    if isinstance(p, dict):
        return _num(p.get("total"))
    return _num(state.get("population_total"))


def _homeless(state: dict) -> float:
    p = state.get("population") if isinstance(state, dict) else None
    if isinstance(p, dict):
        return _num(p.get("homeless"))
    return _num((state or {}).get("homeless"))


def _building_total(state: dict) -> float:
    b = state.get("buildings") if isinstance(state, dict) else None
    if isinstance(b, dict):
        counts = b.get("counts")
        if isinstance(counts, dict):
            return sum(_num(v) for v in counts.values())
    return 0.0


def _action_names(record: dict) -> list[str]:
    """Every action name in a step record — supports single action or a batch queue."""
    names: list[str] = []
    act = record.get("action")
    if isinstance(act, dict):
        # could be {name/tool/command} OR {plan, actions:[...]}
        if isinstance(act.get("actions"), list):
            for a in act["actions"]:
                if isinstance(a, dict):
                    n = a.get("action") or a.get("tool") or a.get("command")
                    if n:
                        names.append(str(n))
        else:
            n = act.get("name") or act.get("tool") or act.get("command")
            if n:
                names.append(str(n))
    elif isinstance(act, str):
        names.append(act)
    # normalize place_building to include the spec, so we learn per-building effects
    specs = _placed_specs(record)
    for s in specs:
        names.append("place:" + s)
    return names


def _placed_specs(record: dict) -> list[str]:
    specs = []
    act = record.get("action")
    queue = act.get("actions") if isinstance(act, dict) else None
    items = queue if isinstance(queue, list) else ([act] if isinstance(act, dict) else [])
    for a in items:
        if not isinstance(a, dict):
            continue
        if (a.get("action") or a.get("command")) == "place_building":
            args = a.get("args") or {}
            spec = args.get("spec") or args.get("spec_id") or args.get("building") or args.get("building_type")
            if spec:
                specs.append(str(spec).split(".")[0])
    return specs


# ---------------------------------------------------------------------------
# Live, within-run observation.
# ---------------------------------------------------------------------------
def observe_step(before: dict, action_names: list[str], after: dict) -> list[str]:
    """Human/LLM-readable effect strings for one step, e.g. 'Log +6', 'homeless -3'.
    Fed back into the agent's own history so it sees cause->effect as it plays."""
    effects: list[str] = []
    rb, ra = _resources(before), _resources(after)
    for good in set(list(rb.keys()) + list(ra.keys())):
        d = ra.get(good, 0.0) - rb.get(good, 0.0)
        if abs(d) >= RESOURCE_EPS:
            effects.append("%s %+d" % (good, int(round(d))))
    dh = _homeless(after) - _homeless(before)
    if abs(dh) >= 1:
        effects.append("homeless %+d" % int(round(dh)))
    dp = _population_total(after) - _population_total(before)
    if abs(dp) >= 1:
        effects.append("pop %+d" % int(round(dp)))
    db = _building_total(after) - _building_total(before)
    if db >= 1:
        effects.append("buildings %+d" % int(round(db)))
    return effects


# ---------------------------------------------------------------------------
# Cross-run distillation into playbook-format lessons.
# ---------------------------------------------------------------------------
def _rows(journal) -> list[dict]:
    if isinstance(journal, (str, Path)):
        rows = []
        try:
            with open(journal, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
        except OSError:
            return []
        return rows
    return list(journal or [])


def _step_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("event") == "step" and isinstance(r.get("state"), dict)]


def distill(journal, run_id: str | None = None) -> list[dict]:
    """Return empirical mechanic lessons (playbook format) from one run's journal."""
    rows = _rows(journal)
    if run_id is None:
        run_id = str(next((r.get("run_id") for r in rows if r.get("run_id")), "run"))
    steps = _step_rows(rows)
    if len(steps) < 2:
        return []

    # Per step: which actions fired, and which signed effects were observed. We then
    # attribute an effect to an action only if the effect is SPECIFIC to it — much
    # more likely in steps WITH the action than WITHOUT (lift). This is what stops a
    # background flow (e.g. logs rising every step from an already-running lumberjack)
    # from being falsely credited to whatever unrelated action shared that step.
    transitions = []  # list of (set(actions), dict(effect_key -> signed delta))
    for a, b in zip(steps, steps[1:]):
        names = set(_action_names(a))
        if not names:
            continue
        rb, ra = _resources(a["state"]), _resources(b["state"])
        eff: dict[str, float] = {}
        for good in TRACKED_GOODS:
            d = ra.get(good, 0.0) - rb.get(good, 0.0)
            if abs(d) >= RESOURCE_EPS:
                eff["good:" + good + (":up" if d > 0 else ":down")] = d
        dh = _homeless(b["state"]) - _homeless(a["state"])
        if abs(dh) >= 1:
            eff["homeless:" + ("up" if dh > 0 else "down")] = dh
        transitions.append((names, eff))

    if len(transitions) < 1:
        return []
    total = len(transitions)
    all_actions = set().union(*[t[0] for t in transitions]) if transitions else set()
    all_effects = set().union(*[set(t[1].keys()) for t in transitions]) if transitions else set()

    lessons: list[dict] = []
    for effect_key in all_effects:
        base_rate = sum(1 for _, e in transitions if effect_key in e) / total
        for action in all_actions:
            with_a = [e for names, e in transitions if action in names]
            if len(with_a) < 1:
                continue
            hits = [e[effect_key] for e in with_a if effect_key in e]
            if len(hits) < 1:
                continue
            precision = len(hits) / len(with_a)          # how often the effect follows this action
            lift = precision / base_rate if base_rate > 0 else 999.0
            # Specificity: the effect should follow this action at least as often as
            # its background rate. The stronger correctness guard is the plausibility
            # gate in _lesson_for (a pump can't be credited with producing logs).
            if precision < 0.5 or lift < 1.0:
                continue
            net = sum(hits)
            lesson = _lesson_for(action, effect_key, net, len(hits), run_id)
            if lesson:
                lessons.append(lesson)
    return lessons


# Which action families can PRODUCE each good — a raise is only credited to a
# plausible producer, so a coincidental co-occurrence (pump placed while the
# lumberjack's logs happened to tick up) is never learned as "pump makes logs".
_PRODUCERS = {
    "Log": ("designate_cutting", "lumberjack", "forester"),
    "Water": ("pump",),
    "Berries": ("gather", "farm"),
    "Food": ("gather", "farm"),
    "Carrots": ("gather", "farm"),
    "Potatoes": ("gather", "farm"),
    "Plank": ("mill", "lumbermill"),
}


def _plausible_producer(action: str, good: str) -> bool:
    keys = _PRODUCERS.get(good)
    if not keys:
        return False
    a = action.lower()
    return any(k in a for k in keys)


def _lesson_for(action, effect_key, net, n, run_id):
    if effect_key.startswith("good:"):
        _, good, direction = effect_key.split(":")
        if direction == "up":
            if not _plausible_producer(action, good):
                return None  # correctness: don't credit an implausible producer
            return _mech_lesson(action, good, "raises", net, n, run_id,
                outcome="%s reliably raises %s (+%d over %d obs) — use it to produce %s"
                        % (action, good, int(round(net)), n, good))
        if action.startswith("place:"):
            return _mech_lesson(action, good, "consumes", net, n, run_id,
                outcome="%s costs %s (%d over %d obs) — keep %s in stock before placing it"
                        % (action, good, int(round(net)), n, good))
        return None
    if effect_key == "homeless:down":
        return _mech_lesson(action, "homeless", "reduces", net, n, run_id,
            outcome="%s reduces homelessness (%d over %d obs) — build it to house beavers"
                    % (action, int(round(net)), n))
    return None


def _mech_lesson(action, target, verb, net, n, run_id, outcome):
    """A discovered-mechanic lesson in coach.py's lesson shape (so reconcile/merge works)."""
    conf = min(0.85, 0.5 + 0.08 * n)  # more observations => more confident
    return {
        "trigger": "you need %s" % (target if verb in ("raises", "reduces") else "to place %s" % action[6:]),
        "situation": "discovered from own play (run %s, %d obs)" % (run_id, int(n)),
        "action": "%s -> %s %s" % (action, verb, target),
        "outcome": outcome,
        "evidence": {"runs": 1, "wins": 1, "losses": 0},
        "confidence": round(conf, 3),
        "created_run": run_id,
        "last_seen_run": run_id,
        "kind": "mechanic",
    }


if __name__ == "__main__":
    import sys
    jp = sys.argv[1] if len(sys.argv) > 1 else None
    if not jp:
        print("usage: discovery.py <journal.jsonl>")
        raise SystemExit(1)
    for lesson in distill(jp):
        print("- [%s] %s => %s" % (lesson["confidence"], lesson["action"], lesson["outcome"]))
