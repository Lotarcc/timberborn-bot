#!/usr/bin/env python3
"""Run-journal metrics for the Timberborn learning curve."""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = REPO_ROOT / "agent"
JOURNAL_DIR = AGENT_DIR / "journal"
DEFAULT_CSV = AGENT_DIR / "metrics.csv"


@dataclass
class Journal:
    path: Path
    rows: list[dict[str, Any]] = field(default_factory=list)
    invalid_lines: int = 0
    missing: bool = False


def journal_path(value: str | None) -> Path:
    if not value:
        return JOURNAL_DIR / "firstlife.jsonl"
    candidate = Path(value)
    if candidate.suffix == ".jsonl" or candidate.parent != Path("."):
        return candidate if candidate.is_absolute() else REPO_ROOT / candidate
    return JOURNAL_DIR / f"{value}.jsonl"


def read_journal(path: Path) -> Journal:
    journal = Journal(path=path)
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    journal.invalid_lines += 1
                    continue
                if isinstance(item, dict):
                    journal.rows.append(item)
                else:
                    journal.invalid_lines += 1
    except OSError:
        journal.missing = True
    return journal


def _nested(data: Any, *path: str) -> Any:
    cursor = data
    for key in path:
        if not isinstance(cursor, dict) or key not in cursor:
            return None
        cursor = cursor[key]
    return cursor


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


def _population(state: dict[str, Any]) -> int | None:
    candidates = [
        state.get("population_total"),
        _nested(state, "population", "total"),
        _nested(state, "population", "beavers"),
        state.get("beavers"),
    ]
    for candidate in candidates:
        if candidate is not None:
            return int(_number(candidate))
    return None


def _cycle(state: dict[str, Any]) -> int | None:
    candidates = [_nested(state, "time", "cycle"), state.get("cycle")]
    for candidate in candidates:
        if candidate is not None:
            return int(_number(candidate))
    return None


def _resource_value(state: dict[str, Any], names: tuple[str, ...], key: str) -> float:
    resources = state.get("resources")
    if not isinstance(resources, dict):
        return 0.0
    lowered_names = {name.lower() for name in names}
    for name, value in resources.items():
        if str(name).lower() not in lowered_names:
            continue
        if isinstance(value, dict):
            return _number(value.get(key))
        if key == "stored":
            return _number(value)
    return 0.0


def _resource_days(state: dict[str, Any], names: tuple[str, ...]) -> float:
    return _resource_value(state, names, "days_remaining")


def _resource_stored(state: dict[str, Any], names: tuple[str, ...]) -> float:
    return _resource_value(state, names, "stored")


def _building_count(state: dict[str, Any]) -> int:
    for key in ("buildings", "built_buildings", "completed_buildings"):
        value = state.get(key)
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            return len(value)
        if isinstance(value, int):
            return value
    return 0


def _result_error(row: dict[str, Any]) -> str | None:
    result = row.get("result")
    if not isinstance(result, dict):
        return None
    status = _number(result.get("http_status"), 200)
    body = result.get("body")
    if status >= 400:
        return f"http_{int(status)}"
    if isinstance(body, dict):
        if body.get("ok") is False:
            return str(body.get("error") or "not_ok")
        if body.get("error"):
            return str(body["error"])
    if result.get("error"):
        return str(result["error"])
    return None


def _action_tool(row: dict[str, Any]) -> str:
    action = row.get("action")
    if isinstance(action, dict):
        return str(action.get("tool") or action.get("command") or "")
    return ""


def _successful_build_action(row: dict[str, Any]) -> bool:
    tool = _action_tool(row)
    if not any(word in tool.lower() for word in ("place", "build")):
        return False
    result = row.get("result")
    if not isinstance(result, dict):
        return False
    body = result.get("body")
    if isinstance(body, dict):
        return body.get("ok") is True
    return _number(result.get("http_status"), 500) < 400


def compute_metrics(journal: Journal) -> dict[str, Any]:
    rows = journal.rows
    run_id = journal.path.stem
    for row in rows:
        if row.get("run_id"):
            run_id = str(row["run_id"])
            break

    states = [row.get("state") for row in rows if isinstance(row.get("state"), dict)]
    populations = [pop for pop in (_population(state) for state in states) if pop is not None]
    cycles = [cycle for cycle in (_cycle(state) for state in states) if cycle is not None]
    final_state = states[-1] if states else {}

    errors_by_type: dict[str, int] = {}
    for row in rows:
        error = _result_error(row)
        if error:
            errors_by_type[error] = errors_by_type.get(error, 0) + 1

    action_rows = [row for row in rows if row.get("action") is not None]
    state_building_peak = max([_building_count(state) for state in states] or [0])
    successful_builds = sum(1 for row in rows if _successful_build_action(row))

    return {
        "run_id": run_id,
        "journal_path": str(journal.path),
        "missing_journal": int(journal.missing),
        "invalid_lines": journal.invalid_lines,
        "events": len(rows),
        "actions": len(action_rows),
        "errors": sum(errors_by_type.values()) + journal.invalid_lines,
        "errors_by_type": errors_by_type,
        "final_cycle": max(cycles) if cycles else 0,
        "peak_population": max(populations) if populations else 0,
        "final_population": populations[-1] if populations else 0,
        "buildings_built": max(state_building_peak, successful_builds),
        "final_water_stored": round(_resource_stored(final_state, ("Water", "water")), 3),
        "final_water_days": round(_resource_days(final_state, ("Water", "water")), 3),
        "final_food_stored": round(
            sum(
                _resource_stored(final_state, names)
                for names in (
                    ("Food", "food"),
                    ("Berries", "berries"),
                    ("Carrots", "carrots"),
                    ("Potatoes", "potatoes"),
                    ("Bread", "bread"),
                )
            ),
            3,
        ),
        "final_food_days": round(_resource_days(final_state, ("Food", "food")), 3),
    }


def append_metrics_csv(metrics: dict[str, Any], path: Path = DEFAULT_CSV) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "run_id",
        "journal_path",
        "missing_journal",
        "invalid_lines",
        "events",
        "actions",
        "errors",
        "final_cycle",
        "peak_population",
        "final_population",
        "buildings_built",
        "final_water_stored",
        "final_water_days",
        "final_food_stored",
        "final_food_days",
    ]
    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow({name: metrics.get(name, "") for name in fieldnames})


def format_summary(metrics: dict[str, Any]) -> str:
    errors_by_type = metrics.get("errors_by_type") or {}
    error_detail = ", ".join(f"{key}={value}" for key, value in sorted(errors_by_type.items()))
    if not error_detail:
        error_detail = "none"
    return "\n".join(
        [
            f"run_id: {metrics['run_id']}",
            f"journal: {metrics['journal_path']}",
            f"missing_journal: {metrics['missing_journal']}",
            f"final_cycle: {metrics['final_cycle']}",
            f"population: peak={metrics['peak_population']} final={metrics['final_population']}",
            f"buildings_built: {metrics['buildings_built']}",
            f"actions: {metrics['actions']}",
            f"errors: {metrics['errors']} ({error_detail})",
            f"final_water: stored={metrics['final_water_stored']} days={metrics['final_water_days']}",
            f"final_food: stored={metrics['final_food_stored']} days={metrics['final_food_days']}",
        ]
    )


def load_metrics_for_run(run_id: str, csv_path: Path = DEFAULT_CSV) -> dict[str, Any] | None:
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            matches = [row for row in csv.DictReader(handle) if row.get("run_id") == run_id]
    except OSError:
        return None
    if not matches:
        return None
    row = matches[-1]
    for key in (
        "missing_journal",
        "invalid_lines",
        "events",
        "actions",
        "errors",
        "final_cycle",
        "peak_population",
        "final_population",
        "buildings_built",
    ):
        row[key] = int(_number(row.get(key)))
    for key in ("final_water_stored", "final_water_days", "final_food_stored", "final_food_days"):
        row[key] = _number(row.get(key))
    row["errors_by_type"] = {}
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize a Timberborn run journal.")
    parser.add_argument(
        "run",
        nargs="?",
        default="firstlife",
        help="Run id or path to agent/journal/<id>.jsonl",
    )
    parser.add_argument("--csv", default=str(DEFAULT_CSV), help="CSV path to append to")
    parser.add_argument("--no-append", action="store_true", help="Print only; do not append CSV")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text")
    args = parser.parse_args(argv)

    journal = read_journal(journal_path(args.run))
    metrics = compute_metrics(journal)
    if args.json:
        print(json.dumps(metrics, indent=2, sort_keys=True))
    else:
        print(format_summary(metrics))
    if not args.no_append:
        append_metrics_csv(metrics, Path(args.csv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
