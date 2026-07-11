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

from agent import auto_path, controller, planner, play
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
    (intent, confidence, executed)."""
    goals_by_id = {g.get("id"): g for g in report.get("goals", []) if isinstance(g, dict)}
    followups = report.get("followups", {}) or {}

    # Don't pile up buildings the colony can't build yet. If enough sites are already under
    # construction, stop placing more (they'd sit unbuilt and drag redundant paths) - let
    # beavers finish current work first. This kills the build_lodge/pump spam loop.
    sites = sum(1 for b in ((state.get("buildings") or {}).get("list") or [])
                if isinstance(b, dict) and b.get("status") == "site")
    at_site_cap = sites >= MAX_ACTIVE_SITES

    for goal_id, conf in ranked:
        if goal_id == "advance_time":
            return "advance_time", conf, False
        if at_site_cap and goal_id.startswith("build_"):
            continue  # too many sites pending; skip builds (fall through to advance_time)
        if goal_id == "demolish_unreachable":
            target = _find_unreachable(state)
            if target is None:
                continue
            bridge.act("demolish", {"x": target.get("x"), "y": target.get("y"), "z": target.get("z")})
            return "demolish_unreachable", conf, True

        goal = goals_by_id.get(goal_id)
        if not goal or goal.get("satisfied") is True:
            continue
        if goal.get("affordable") is False and goal.get("free") is not True:
            continue
        spec = goal.get("spec")
        if not spec:
            continue
        cands = planner.candidates_for(goal_id, state, map_data, k=6, resources=resources)
        if not cands:
            continue
        c = cands[0]
        # auto_connect:false -> the AGENT owns pathing (one DC-rooted trunk via auto_path),
        # not the bridge's per-building shortest hop.
        args = {"spec": spec, "x": c.get("x"), "y": c.get("y", c.get("z")), "z": c.get("z"),
                "auto_connect": False}
        if c.get("orientation"):
            args["orientation"] = c["orientation"]
        bridge.act("place_building", args)
        for fu in followups.get(goal_id, []) or []:
            if isinstance(fu, dict) and fu.get("action"):
                bridge.act(fu["action"], fu.get("args") or {})
        return goal_id, conf, True

    return "advance_time", 0.0, False


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
    for cycle in range(1, max_cycles + 1):
        try:
            state, map_data, resources, _, _ = controller._read_cycle_inputs(bridge, cycle)
        except RuntimeError as err:
            play.log_stderr("cycle %d: %s" % (cycle, err))
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

        ranked = policy.rank(state)
        report = planner.plan_report(state, map_data, resources=resources)

        # fidelity telemetry: does the learned policy agree with the deterministic expert?
        expert = controller.build_safe_ready_frontier(report, state)
        expert_top = (expert.get("goal_ids") or ["advance_time"])[0]
        policy_top = ranked[0][0]
        total += 1
        if policy_top == expert_top:
            agree += 1

        intent, conf, executed = _execute_intent(bridge, ranked, report, state, map_data, resources)

        play.journal_append(journal_path, {
            "run_id": run_id, "cycle": cycle, "event": "decision",
            "policy_top": policy_top, "confidence": round(float(conf), 3),
            "expert_top": expert_top, "agrees": policy_top == expert_top,
            "intent_executed": intent, "executed": executed,
            "pop": _alive(state),
            "logs": planner._logs_available(state),
            "water_days": round(planner._resource_days(state, "Water"), 1),
        })
        play.log_stderr("cycle %02d | policy=%s(%.2f) expert=%s %s | %s" % (
            cycle, policy_top, conf, expert_top,
            "OK" if policy_top == expert_top else "DIFF",
            "did " + intent if executed else "advance"))

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
            controller.bulk_advance_until_wake(bridge, state)

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
