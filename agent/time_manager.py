"""Time & work-hours management.

Two levers the agent should drive (per user strategy):
1. WORK HOURS - how much of the day beavers work (WorkingHoursManager.WorkedPartOfDay).
   Run it HIGH early to bootstrap fast, then step DOWN as the colony grows so beavers
   rest properly (well-being, lifespan). ~22h bootstrap -> 18 -> 16 -> 14h mature.
2. NIGHT FAST-FORWARD - beavers sleep at night; nothing productive happens, so blast the
   game speed up when it's not daytime and run normal speed during the day.

Pure functions over the /state dict (state["time"] = {cycle, day, hour, daytime}).
"""

from __future__ import annotations


def _pop(state: dict) -> int:
    p = (state.get("population") or {}) if isinstance(state, dict) else {}
    try:
        return int(p.get("total") or 0)
    except Exception:
        return 0


def work_hours_for(state: dict) -> float:
    """Desired length of the work day (hours). High to bootstrap, lower as we grow so the
    colony can meet its work in less time and beavers rest."""
    pop = _pop(state)
    # Colonies start at ~13 beavers, so the bootstrap tier must cover them: push the work
    # day hard while infrastructure is thin, then step down as the colony grows so beavers
    # rest (better well-being/lifespan).
    if pop <= 16:
        return 22.0   # bootstrap: little infrastructure, race to water/food security
    if pop <= 25:
        return 18.0
    if pop <= 35:
        return 16.0
    return 14.0       # mature colony: comfortable rest


def is_night(state: dict) -> bool:
    t = (state.get("time") or {}) if isinstance(state, dict) else {}
    if "daytime" in t:
        return not bool(t.get("daytime"))
    # Fallback by hour if daytime flag missing.
    hour = t.get("hour")
    try:
        return not (6.0 <= float(hour) <= 22.0)
    except Exception:
        return False


def speed_for(state: dict, day_speed: float = 3.0, night_speed: float = 10.0) -> float:
    """Game speed to run: blast through the (unproductive) night, normal during the day."""
    return night_speed if is_night(state) else day_speed


__all__ = ["work_hours_for", "is_night", "speed_for"]
