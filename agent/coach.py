#!/usr/bin/env python3
"""Offline retrospective coach for Timberborn run journals."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import metrics as metrics_mod


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = REPO_ROOT / "agent"
DEFAULT_PLAYBOOK = AGENT_DIR / "playbook.json"


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _run_sort_value(run_id: str) -> int:
    match = re.search(r"(\d+)$", run_id)
    return int(match.group(1)) if match else 0


def _state_rows(journal: metrics_mod.Journal) -> list[dict[str, Any]]:
    return [row for row in journal.rows if isinstance(row.get("state"), dict)]


def _resource_stored(state: dict[str, Any], names: tuple[str, ...]) -> float:
    resources = state.get("resources")
    if not isinstance(resources, dict):
        return 0.0
    lowered = {name.lower() for name in names}
    for name, value in resources.items():
        if str(name).lower() not in lowered:
            continue
        if isinstance(value, dict):
            return _number(value.get("stored"))
        return _number(value)
    return 0.0


def _errors_by_type(journal: metrics_mod.Journal) -> dict[str, int]:
    errors: dict[str, int] = {}
    for row in journal.rows:
        result = row.get("result")
        if not isinstance(result, dict):
            continue
        body = result.get("body")
        error: str | None = None
        if isinstance(body, dict) and body.get("ok") is False:
            error = str(body.get("error") or "not_ok")
        elif result.get("error"):
            error = str(result["error"])
        if error:
            errors[error] = errors.get(error, 0) + 1
    if journal.invalid_lines:
        errors["invalid_jsonl"] = journal.invalid_lines
    return errors


def _lesson(
    *,
    trigger: str,
    situation: str,
    action: str,
    outcome: str,
    run_id: str,
    confidence: float = 0.55,
    win: bool = False,
) -> dict[str, Any]:
    return {
        "trigger": trigger,
        "situation": situation,
        "action": action,
        "outcome": outcome,
        "evidence": {"runs": 1, "wins": 1 if win else 0, "losses": 0 if win else 1},
        "confidence": round(confidence, 3),
        "created_run": run_id,
        "last_seen_run": run_id,
    }


def analyze(journal: metrics_mod.Journal, metrics: dict[str, Any]) -> list[dict[str, Any]]:
    """Rule-based retrospective. Swappable later for a smarter LLM analyzer."""

    run_id = str(metrics.get("run_id") or journal.path.stem)
    states = _state_rows(journal)
    final_state = states[-1] if states else {}
    lessons: list[dict[str, Any]] = []

    final_water = _number(metrics.get("final_water_stored"))
    if states:
        final_water = max(final_water, _resource_stored(final_state, ("Water", "water")))
    if states and final_water <= 0:
        lessons.append(
            _lesson(
                trigger="forecast drought OR water_stored == 0",
                situation=f"cycle<={metrics.get('final_cycle', 0)}, pop={metrics.get('final_population', 0)}, tank reserve absent",
                action="pause discretionary builds; place pumps and tanks until stored_water >= (D+2)*2.13*P before advancing",
                outcome="prevents first-drought thirst spiral by making tank water the survival gate",
                run_id=run_id,
                confidence=0.62,
            )
        )

    final_food = _number(metrics.get("final_food_stored"))
    if states:
        final_food = max(
            final_food,
            sum(
                _resource_stored(final_state, names)
                for names in (
                    ("Food", "food"),
                    ("Berries", "berries"),
                    ("Carrots", "carrots"),
                    ("Potatoes", "potatoes"),
                )
            ),
        )
    if states and final_food <= 0:
        lessons.append(
            _lesson(
                trigger="forecast drought OR food_stored == 0",
                situation=f"cycle<={metrics.get('final_cycle', 0)}, pop={metrics.get('final_population', 0)}, no food bank",
                action="build GathererFlag immediately, then EfficientFarmhouse with carrots; target stored_food >= (D+2)*2.67*P",
                outcome="keeps hunger from disabling pump and farm workers during the first hazard",
                run_id=run_id,
                confidence=0.56,
            )
        )

    if int(_number(metrics.get("buildings_built"))) == 0 and int(_number(metrics.get("actions"))) > 0:
        lessons.append(
            _lesson(
                trigger="early run AND buildings_built == 0",
                situation=f"cycle<={metrics.get('final_cycle', 0)}, actions={metrics.get('actions', 0)}, no completed construction",
                action="prioritize a minimal starter-base action sequence: Path, WaterPump or DeepWaterPump, SmallTank, GathererFlag, Farmhouse",
                outcome="turns early decisions into survival infrastructure instead of idle speed changes",
                run_id=run_id,
                confidence=0.58,
            )
        )

    errors = _errors_by_type(journal)
    if errors:
        # One deduped lesson for all teaching errors this run (was one-per-error-name,
        # which proliferated near-identical lessons in the playbook). A STABLE trigger
        # so reconcile collapses it across runs; the specific errors go in the situation.
        summary = ", ".join("%s x%d" % (name, count) for name, count in sorted(errors.items()))
        lessons.append(
            _lesson(
                trigger="bridge teaching errors occurred",
                situation="run %s: %s" % (run_id, summary),
                action="treat bridge teaching errors as hard constraints; use only implemented tools and planner-provided candidate tiles",
                outcome="stops malformed or unsupported commands from wasting turns",
                run_id=run_id,
                confidence=0.52,
            )
        )

    if not lessons and not journal.missing:
        lessons.append(
            _lesson(
                trigger="run completes with no detected survival failure",
                situation=f"cycle={metrics.get('final_cycle', 0)}, pop={metrics.get('final_population', 0)}",
                action="keep using the current survival priorities and compare against the next checkpoint replay",
                outcome="preserves successful behavior until more evidence contradicts it",
                run_id=run_id,
                confidence=0.5,
                win=True,
            )
        )
    return lessons


def load_playbook(path: Path = DEFAULT_PLAYBOOK) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"lessons": []}
    if isinstance(data, list):
        return {"lessons": [item for item in data if isinstance(item, dict)]}
    if isinstance(data, dict):
        lessons = data.get("lessons")
        if isinstance(lessons, list):
            data["lessons"] = [item for item in lessons if isinstance(item, dict)]
        else:
            data["lessons"] = []
        return data
    return {"lessons": []}


def _lesson_key(lesson: dict[str, Any]) -> str:
    trigger = str(lesson.get("trigger", "")).strip().lower()
    action = str(lesson.get("action", "")).strip().lower()
    return re.sub(r"\s+", " ", f"{trigger}|{action}")


def _evidence(lesson: dict[str, Any]) -> dict[str, int]:
    evidence = lesson.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    return {
        "runs": int(_number(evidence.get("runs"))),
        "wins": int(_number(evidence.get("wins"))),
        "losses": int(_number(evidence.get("losses"))),
    }


def _confidence_from_evidence(confidence: float, evidence: dict[str, int]) -> float:
    runs = max(evidence["runs"], 1)
    win_rate = evidence["wins"] / runs
    loss_rate = evidence["losses"] / runs
    adjusted = 0.7 * confidence + 0.3 * (0.5 + win_rate * 0.35 - loss_rate * 0.15)
    if runs >= 3:
        adjusted += min(0.08, 0.01 * runs)
    adjusted = round(max(0.05, min(0.95, adjusted)), 3)
    # An all-loss lesson (never once helped) is noise or an anti-pattern — never let
    # it float near the top of the injected playbook. Hard-cap its confidence.
    if evidence["wins"] == 0 and evidence["losses"] >= 1:
        adjusted = min(adjusted, 0.2)
    return adjusted


# Keep the injected playbook small and honest.
MAX_PLAYBOOK_LESSONS = 12


def _prune(lessons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop persistent losers and cap the pool. A lesson seen in >=2 runs that has
    never once been a 'win' is noise (or a spurious cause->effect) — remove it so it
    stops polluting the prompt and teaching the model falsehoods."""
    kept = []
    for lesson in lessons:
        ev = _evidence(lesson)
        if ev["runs"] >= 2 and ev["wins"] == 0:
            continue  # persistent all-loss => drop
        kept.append(lesson)
    return kept[:MAX_PLAYBOOK_LESSONS]


def reconcile(existing: list[dict[str, Any]], proposed: list[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for lesson in existing + proposed:
        grouped.setdefault(_lesson_key(lesson), []).append(dict(lesson))

    reconciled: list[dict[str, Any]] = []
    for lessons in grouped.values():
        lessons.sort(
            key=lambda item: (
                _number(item.get("confidence")),
                _evidence(item)["runs"],
                _run_sort_value(str(item.get("last_seen_run") or item.get("created_run") or "")),
            ),
            reverse=True,
        )
        winner = lessons[0]
        evidence = _evidence(winner)
        created_run = str(winner.get("created_run") or run_id)
        last_seen_run = str(winner.get("last_seen_run") or run_id)

        for duplicate in lessons[1:]:
            duplicate_evidence = _evidence(duplicate)
            evidence["runs"] += duplicate_evidence["runs"]
            evidence["wins"] += duplicate_evidence["wins"]
            evidence["losses"] += duplicate_evidence["losses"]
            duplicate_created = str(duplicate.get("created_run") or created_run)
            duplicate_seen = str(duplicate.get("last_seen_run") or last_seen_run)
            if _run_sort_value(duplicate_created) and (
                not _run_sort_value(created_run) or _run_sort_value(duplicate_created) < _run_sort_value(created_run)
            ):
                created_run = duplicate_created
            if _run_sort_value(duplicate_seen) >= _run_sort_value(last_seen_run):
                last_seen_run = duplicate_seen
            elif duplicate_seen == run_id:
                last_seen_run = run_id

        winner["evidence"] = evidence
        winner["created_run"] = created_run
        winner["last_seen_run"] = run_id if any(item.get("last_seen_run") == run_id for item in lessons) else last_seen_run
        winner["confidence"] = _confidence_from_evidence(_number(winner.get("confidence"), 0.5), evidence)
        reconciled.append(winner)

    reconciled.sort(
        key=lambda item: (
            _number(item.get("confidence")),
            _evidence(item)["runs"],
            _run_sort_value(str(item.get("last_seen_run") or "")),
        ),
        reverse=True,
    )
    return _assign_ids(_prune(reconciled))


def _assign_ids(lessons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    used = {str(lesson.get("id")) for lesson in lessons if lesson.get("id")}
    next_id = 1
    for lesson in lessons:
        if lesson.get("id"):
            continue
        while f"L-{next_id:04d}" in used:
            next_id += 1
        lesson["id"] = f"L-{next_id:04d}"
        used.add(lesson["id"])
        next_id += 1
    return lessons


def update_playbook(playbook_path: Path, proposed: list[dict[str, Any]], run_id: str) -> dict[str, Any]:
    playbook = load_playbook(playbook_path)
    playbook["lessons"] = reconcile(playbook.get("lessons", []), proposed, run_id)
    playbook["schema"] = "timberborn-bot-playbook-v1"
    playbook_path.parent.mkdir(parents=True, exist_ok=True)
    playbook_path.write_text(json.dumps(playbook, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return playbook


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Distill a run journal into playbook lessons.")
    parser.add_argument("--run-id", default="firstlife", help="Run id under agent/journal/<id>.jsonl")
    parser.add_argument("--journal", help="Override journal path")
    parser.add_argument("--metrics-csv", default=str(metrics_mod.DEFAULT_CSV), help="Metrics CSV to read")
    parser.add_argument("--playbook", default=str(DEFAULT_PLAYBOOK), help="Playbook JSON to update")
    parser.add_argument("--dry-run", action="store_true", help="Print proposed lessons without writing playbook")
    args = parser.parse_args(argv)

    journal = metrics_mod.read_journal(metrics_mod.journal_path(args.journal or args.run_id))
    run_metrics = metrics_mod.load_metrics_for_run(args.run_id, Path(args.metrics_csv))
    if run_metrics is None:
        run_metrics = metrics_mod.compute_metrics(journal)
    run_metrics["run_id"] = args.run_id

    proposed = analyze(journal, run_metrics)
    if args.dry_run:
        print(json.dumps({"lessons": proposed}, indent=2, sort_keys=True))
        return 0

    playbook = update_playbook(Path(args.playbook), proposed, args.run_id)
    print(
        f"run_id: {args.run_id}\n"
        f"proposed_lessons: {len(proposed)}\n"
        f"playbook_lessons: {len(playbook.get('lessons', []))}\n"
        f"playbook: {args.playbook}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
