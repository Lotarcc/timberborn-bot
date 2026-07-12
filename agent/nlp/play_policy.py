"""Play Timberborn with the TRAINED decision policy in the driver's seat.

Each cycle: read state -> the learned heads (CART + LIDSNet) rank intents -> we execute
the top executable intent via the existing planner.candidates_for (WHERE) + bridge
primitives, then bulk-advance time. This is the AI-Player loop with our trained model
replacing the per-turn LLM: the model decides WHAT in milliseconds; proven code does the
rest; the LLM is only a tie-break hook (policy.disagreement).

It also logs, every cycle, whether the learned policy AGREES with the deterministic
expert (planner frontier) - the fidelity signal, and the hook where outcome-based
relabeling will later push the policy BEYOND the planner.

Run:  python -m agent.nlp.play_policy --cycles 40
"""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import List, Optional, Tuple

from agent import auto_path, controller, curriculum, game_schema, planner, play, replay, time_manager
from agent.nlp import labeler
from agent.nlp.policy import DecisionPolicy

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Max buildings under construction at once. Beyond this, stop placing new buildings so
# beavers finish current sites first (prevents the build-spam / path-sprawl loop).
MAX_ACTIVE_SITES = 3


def _int(v, d=0):
    try:
        return int(v)
    except Exception:
        return d


def _alive(state) -> int:
    pop = (state.get("population") or {}) if isinstance(state, dict) else {}
    return _int(pop.get("total"), 0)


def _find_unreachable(state) -> Optional[dict]:
    listing = ((state.get("buildings") or {}).get("list")) if isinstance(state, dict) else None
    if isinstance(listing, list):
        for b in listing:
            if isinstance(b, dict) and b.get("status") == "finished" and b.get("reachable") is False:
                return b
    return None


def _execute_intent(bridge, ranked: List[Tuple[str, float]], report: dict,
                    state: dict, map_data: dict, resources) -> Tuple[str, float, bool]:
    """Walk ranked intents; execute the first one that is actionable. Returns
    (intent, confidence, executed).

    NAMESPACE BRIDGE: `ranked` carries game_schema ids (agent/nlp/game_schema.py::
    actions(), e.g. "build_lumberjack_flag", "build_lumber_mill") - that is the
    model's whole label space. `report["goals"]` (from planner.plan_report) carries
    a MIXED namespace: legacy bootstrap goals predate game_schema and use
    planner-only ids ("build_lumberjack", "build_water_pump", "build_gatherer",
    "build_farm", "build_warehouse", ...), while the Task-3 economy/amenity/power/
    storage goals already use game_schema ids (planner builds them FROM
    game_schema.spec_to_action). Keying goals by id (as this used to) means every
    bootstrap intent the model proposes finds nothing and silently falls through to
    advance_time - the colony never bootstraps. Resolving BY SPEC instead
    (action_to_spec(goal_id) -> spec -> the planner goal whose goal["spec"]
    matches) bridges both namespaces uniformly, the same way agent/nlp/labeler.py's
    _to_schema_id resolves the training labels.
    """
    goals_by_spec = {
        g["spec"]: g for g in report.get("goals", []) if isinstance(g, dict) and g.get("spec")
    }
    followups = report.get("followups", {}) or {}

    # Don't pile up buildings the colony can't build yet. If enough sites are already under
    # construction, stop placing more (they'd sit unbuilt and drag redundant paths) - let
    # beavers finish current work first. This kills the build_lodge/pump spam loop.
    sites = sum(1 for b in ((state.get("buildings") or {}).get("list") or [])
                if isinstance(b, dict) and b.get("status") == "site")
    at_site_cap = sites >= MAX_ACTIVE_SITES

    advance_conf = 0.0
    for goal_id, conf in ranked:
        if goal_id == "advance_time":
            # advance_time is the LAST resort, not a short-circuit: remember it and keep
            # walking so an affordable, useful build ranked BELOW it still gets placed
            # (the model over-weights advance_time from the training imbalance). If nothing
            # ranked is executable, the loop falls through to the advance_time return below.
            advance_conf = conf
            continue
        if at_site_cap and goal_id.startswith("build_"):
            continue  # too many sites pending; skip builds (fall through to advance_time)
        if goal_id == "demolish_unreachable":
            target = _find_unreachable(state)
            if target is None:
                continue
            bridge.act("demolish", {"x": target.get("x"), "y": target.get("y"), "z": target.get("z")})
            return "demolish_unreachable", conf, True

        spec = game_schema.action_to_spec(goal_id)
        if spec is None:
            continue  # not a buildable action (unknown id, or a verb we don't special-case)
        goal = goals_by_spec.get(spec)
        if goal is None:
            continue  # the expert isn't proposing this spec right now; try the next ranked intent
        if goal.get("satisfied") is True:
            continue
        if goal.get("affordable") is False and goal.get("free") is not True:
            continue
        # candidates_for takes the resolved GOAL DICT (carries the real spec), not the raw
        # model id - the raw id is not a key of planner.GOAL_SPECS and would mis-resolve.
        cands = planner.candidates_for(goal, state, map_data, k=6, resources=resources)
        if not cands:
            continue
        resolved_id = goal.get("id")
        # Try each candidate, and if the bridge rejects a tile as invalid it usually returns a
        # `suggestion.nearest_valid` - retry there once. CRITICAL: only report executed=True when
        # the bridge actually ACCEPTS (body.ok is True). The old code ignored the response and
        # returned executed=True on a rejected placement, which froze the loop: it "built" a
        # phantom lodge every cycle, never advanced time, and never housed anyone.
        placed = False
        for c in cands:
            # auto_connect:false -> the AGENT owns pathing (one DC-rooted trunk via auto_path).
            attempt = c
            for _retry in range(2):  # the candidate tile, then the bridge's nearest_valid suggestion
                args = {"spec": spec, "x": attempt.get("x"), "y": attempt.get("y", attempt.get("z")),
                        "z": attempt.get("z"), "auto_connect": False}
                if attempt.get("orientation"):
                    args["orientation"] = attempt["orientation"]
                status, body = bridge.act("place_building", args)
                if status == 200 and isinstance(body, dict) and body.get("ok") is True:
                    placed = True
                    break
                sugg = ((body.get("suggestion") or {}).get("nearest_valid")
                        if isinstance(body, dict) else None)
                if not isinstance(sugg, dict):
                    break
                attempt = sugg  # retry once at the bridge-suggested valid tile
            if placed:
                break
        if not placed:
            continue  # no valid tile for this spec right now; fall through to the next ranked intent
        for fu in followups.get(resolved_id, []) or []:
            if isinstance(fu, dict) and fu.get("action"):
                bridge.act(fu["action"], fu.get("args") or {})
        return resolved_id, conf, True

    return "advance_time", advance_conf, False


def run(cfg: dict, run_id: str, max_cycles: int = 40) -> dict:
    bridge = play.Bridge(cfg["BRIDGE_URL"])
    policy = DecisionPolicy.load()
    journal_dir = os.path.join(AGENT_DIR, "journal")
    os.makedirs(journal_dir, exist_ok=True)
    journal_path = os.path.join(journal_dir, "%s.jsonl" % run_id)

    ping_status, ping_body = bridge.ping()
    bridge.act("set_speed", {"speed": 0})
    play.journal_append(journal_path, {"run_id": run_id, "event": "run_start",
                                       "policy": "trained-lidsnet+cart", "ping": ping_body})
    play.log_stderr("policy play journal: %s" % journal_path)

    agree = 0
    total = 0
    last_work_hours = None
    actions_set = set(game_schema.actions())
    # In-memory mirror of this run's recorded rows (docs/kb/learning-loop-design.md
    # SS5.1): the in-run stall check below needs exactly what replay.load_run(run_id)
    # would return, but re-reading the jsonl file from disk every cycle is wasted
    # work when record_step already hands back each row as it's written.
    run_rows: List[dict] = []
    for cycle in range(1, max_cycles + 1):
        try:
            state, map_data, resources, _, _ = controller._read_cycle_inputs(bridge, cycle)
        except RuntimeError as err:
            play.log_stderr("cycle %d: %s" % (cycle, err))
            break

        # Curriculum stop condition: the colony reached the terminal STABLE goal (pop>=30,
        # water/food buffers clear the longest forecast drought, nobody homeless). Checked
        # first - before any ranking/planning work - so a reached goal ends the run cleanly.
        if curriculum.is_goal_reached(state):
            phase = curriculum.current_phase(state)
            play.journal_append(journal_path, {
                "run_id": run_id, "cycle": cycle, "event": "goal_reached", "phase": phase,
            })
            replay.record_step(run_id, cycle, state, "goal_reached", meta={"phase": phase})
            play.log_stderr("cycle %d: goal reached (STABLE) - stopping" % cycle)
            break

        if _alive(state) <= 0 and cycle > 1:
            play.journal_append(journal_path, {"run_id": run_id, "cycle": cycle, "event": "colony_dead"})
            play.log_stderr("cycle %d: colony dead - stopping" % cycle)
            break

        # Keep production flowing (idempotent): mark mature trees for cutting so a placed
        # lumberjack actually yields logs. Without this the colony starves for wood and the
        # policy stalls on advance_time forever.
        try:
            if planner._building_count(state, "LumberjackFlag") > 0:
                bridge.act("designate_cutting", {"all": True})
        except Exception:
            pass

        # Work-hours strategy: run the work day long early to bootstrap, step it down as the
        # colony grows so beavers rest. Only push updates when the target changes.
        try:
            wh = round(time_manager.work_hours_for(state), 1)
            if wh != last_work_hours:
                bridge.act("set_working_hours", {"hours": wh})
                last_work_hours = wh
                play.log_stderr("  work hours -> %.0fh (pop %d)" % (wh, _alive(state)))
        except Exception:
            pass

        ranked = policy.rank(state)
        raw_top = ranked[0][0]  # the RAW model pick, before biasing - the true fidelity signal
        # Curriculum biasing: promote the current phase's priority goals to the front of
        # the ranked list (stable sort - within-phase confidence order is preserved).
        ranked = curriculum.bias_ranking(state, ranked)
        report = planner.plan_report(state, map_data, resources=resources)

        # fidelity telemetry: does the learned policy agree with the deterministic expert?
        # NAMESPACE FIX: build_safe_ready_frontier's goal_ids are planner ids (the same
        # mixed bootstrap/economy namespace _execute_intent bridges above); translate the
        # expert's pick to a game_schema id via the same _to_schema_id the training labeler
        # uses so this compares like-for-like against policy_top (always a game_schema id).
        expert = controller.build_safe_ready_frontier(report, state)
        expert_top_id = (expert.get("goal_ids") or ["advance_time"])[0]
        goals_by_id = {g.get("id"): g for g in report.get("goals", []) if isinstance(g, dict)}
        expert_goal = goals_by_id.get(expert_top_id)
        expert_top = labeler._to_schema_id(expert_goal, actions_set) if expert_goal else expert_top_id
        policy_top = ranked[0][0]  # biased top (what execution starts from)
        total += 1
        # Fidelity = does the MODEL agree with the expert? Compare the RAW model pick, not the
        # biased top - curriculum biasing is an intentional override, not a model prediction.
        if raw_top == expert_top:
            agree += 1

        intent, conf, executed = _execute_intent(bridge, ranked, report, state, map_data, resources)

        play.journal_append(journal_path, {
            "run_id": run_id, "cycle": cycle, "event": "decision",
            "raw_top": raw_top, "policy_top": policy_top, "confidence": round(float(conf), 3),
            "expert_top": expert_top, "agrees": raw_top == expert_top,
            "intent_executed": intent, "executed": executed,
            "pop": _alive(state),
            "logs": planner._logs_available(state),
            "water_days": round(planner._resource_days(state, "Water"), 1),
        })
        record = replay.record_step(run_id, cycle, state, intent, meta={
            "phase": curriculum.current_phase(state),
            "confidence": round(float(conf), 3),
            "expert_top": expert_top,
            "policy_top": policy_top,
            "executed": executed,
        })
        run_rows.append(record)
        play.log_stderr("cycle %02d | policy=%s(%.2f) expert=%s %s | %s" % (
            cycle, policy_top, conf, expert_top,
            "OK" if policy_top == expert_top else "DIFF",
            "did " + intent if executed else "advance"))

        # In-run stall stop (docs/kb/learning-loop-design.md SS5.1): reuse the SAME
        # streak logic summarize_run applies post-hoc, but incrementally, on the
        # rows recorded so far - so a colony that's alive-but-stuck (or already
        # dying) ends the run within ~_STALL_STREAK/_DEATH_STREAK cycles of the
        # streak actually starting, instead of grinding through the rest of
        # max_cycles' bulk_advance_until_wake calls on an already-decided run.
        sig = replay.progress_signal(run_rows)
        if sig["ended"] != "alive":
            play.journal_append(journal_path, {
                "run_id": run_id, "cycle": cycle, "event": "stall_detected",
                "ended": sig["ended"], "death_cause": sig["death_cause"],
            })
            play.log_stderr("cycle %d: in-run stop (%s) - ending run early" % (cycle, sig["ended"]))
            break

        # Agent-owned pathing: after any placement, plan/repave ONE trunk from the DC
        # entrance connecting every building (auto_path), not per-building shortest hops.
        if executed and intent not in ("advance_time", "demolish_unreachable"):
            try:
                pstatus, pstate = bridge.state()
                if pstatus == 200 and isinstance(pstate, dict):
                    conn = auto_path.connect_all(bridge, pstate, map_data)
                    if conn["laid"] or conn["unreachable"]:
                        play.log_stderr("  trunk: +%d path tiles, %d unreachable" % (
                            len(conn["laid"]), len(conn["unreachable"])))
            except Exception as exc:
                play.log_stderr("  trunk pathing failed: %s" % exc)

        if not executed:
            # Idle fast-forward (COAST): nothing was built this cycle, so run time in a big
            # chunk at high speed THROUGH minor churn (alert/staffing flicker) to accrue logs
            # and let beavers breed - instead of stopping every few game-minutes. Crisis wakes
            # (water/food buffer, hazard, population change) still pause it, so it never coasts
            # through a thirst/hunger death or into an unprepared drought.
            controller.bulk_advance_until_wake(
                bridge, state, run_speed=12, coast=True, max_advance_days=3.0, max_polls=120)

    summary = {"run_id": run_id, "event": "run_end", "cycles": total,
               "policy_expert_agreement": round(agree / total, 3) if total else None}
    play.journal_append(journal_path, summary)
    play.log_stderr("run end: %d cycles, policy/expert agreement %.1f%%" % (
        total, 100 * agree / total if total else 0))
    return summary


def main(argv=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge", default=os.environ.get("BRIDGE_URL", "http://127.0.0.1:7744"))
    parser.add_argument("--cycles", type=int, default=40)
    parser.add_argument("--run-id", default="policy%d" % int(time.time()) if False else "policy_run")
    args = parser.parse_args(argv)
    run({"BRIDGE_URL": args.bridge}, args.run_id, args.cycles)


if __name__ == "__main__":
    main()
