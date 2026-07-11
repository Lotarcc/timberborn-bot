"""Production-chain reasoning for the full-economy expert planner (Task 3a).

This is the pure "WHAT to build next in the production graph" helper. It keeps
`planner.py` readable: `planner.analyze` stays a thin emit loop while the chain
walk, demand detection and raw->refined ordering live here.

Public API
----------
    needed_producers(state) -> [spec]
        For every good the colony CONSUMES but cannot currently PRODUCE, the bare
        producer building spec to add. Ordered raw->refined (by chain depth) and
        de-duped. A good is "demanded" when something BUILT consumes it, or when
        it feeds "construction" (the colony's standing demand for build
        materials). A good is "produced" when some built building makes it.
    producer_plan(state) -> [ {spec, good, depth, cost_logs, why} ]
        The same result with the reasoning attached, consumed by the planner
        wiring so it can emit fully-formed goals without re-deriving anything.
    log_cost(spec) -> int
        Log build-cost of a spec, straight from buildings.json (the ground truth).
        The planner's COST_LOGS table is bootstrap-only and omits/misnames the new
        economy buildings, so goals MUST source their cost here instead.

Ground truth: agent/data/chains.json (production graph) + agent/data/buildings.json
(costs). Chain walking and the display-name->spec mapping are reused from
resource_manager; producer/count lookups from game_schema. Python 3 stdlib only.

Runs its own tests: `python3 -m unittest agent.test_economy`.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Dict, List, Optional

# Import-fallback so the module resolves whether it is imported as a loose module
# (sys.path has agent/) or as the package module agent.economy (sys.path has the
# repo root). Mirrors the pattern at the top of replay.py / placement.py.
try:  # pragma: no cover - import path depends on invocation
    import resource_manager
    import game_schema
except ImportError:  # pragma: no cover
    from agent import resource_manager  # type: ignore
    from agent import game_schema  # type: ignore

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# consumed_by tokens that are TERMINAL (not a factory building) yet still count as
# a standing demand for Task 3a. "construction" == the colony always needs build
# materials. The beaver-need / late-game terminals ("eaten", "drunk", "research",
# "bots", "well-being", "demolition", "landscaping") are deliberately EXCLUDED
# here; food/well-being/tech demand is owned by later subtasks (3b/3c), so 3a
# stays scoped to the construction-material + built-consumer production chains.
_ACTIVE_TERMINALS = frozenset({"construction"})


# =============================================================================
# Data loading (cached)
# =============================================================================
@lru_cache(maxsize=None)
def _load(name: str) -> dict:
    with open(os.path.join(_DATA_DIR, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def _chains() -> Dict[str, dict]:
    """good id -> chain entry {good, produced_by, consumed_by}."""
    return {c["good"]: c for c in _load("chains.json").get("chains", [])}


@lru_cache(maxsize=1)
def _building_by_spec() -> Dict[str, dict]:
    """bare spec id -> building record (faction suffix stripped)."""
    out: Dict[str, dict] = {}
    for b in _load("buildings.json").get("buildings", []):
        out[str(b.get("id", "")).split(".", 1)[0]] = b
    return out


def _bare(spec: str) -> str:
    return str(spec).split(".", 1)[0] if spec else spec


# =============================================================================
# Cost lookup (buildings.json is the only ground truth)
# =============================================================================
def log_cost(spec: str) -> int:
    """Log amount in ``spec``'s build_cost, or 0 if it costs no logs / is unknown.

    buildings.json stores build_cost goods lower-snake ("log"); the amount is the
    faction-agnostic build cost. Returns 0 for specs with no log line (e.g. a
    Smelter, paid in planks/gear/scrap) and for unknown specs.
    """
    building = _building_by_spec().get(_bare(spec))
    if not building:
        return 0
    for cost in building.get("build_cost") or []:
        if str(cost.get("good", "")).lower() == "log":
            try:
                return int(cost.get("amount") or 0)
            except (TypeError, ValueError):
                return 0
    return 0


# =============================================================================
# Tech progression (Task 3b): what is unlockable given science + prerequisites
# =============================================================================
@lru_cache(maxsize=1)
def _tech() -> dict:
    return _load("tech_tree.json")


def science_cost(spec: str) -> int:
    """Science-point unlock cost of ``spec`` from buildings.json (0 if free/unknown).

    buildings.json carries the authoritative per-spec ``science_cost`` (an int),
    keyed by spec so there is no display-name ambiguity. tech_tree.json's
    unlocks[].science_cost is display-name keyed and marked uncertain in places, so
    it is used only for the prerequisite GRAPH, never for the cost.
    """
    building = _building_by_spec().get(_bare(spec))
    if not building:
        return 0
    try:
        return int(building.get("science_cost") or 0)
    except (TypeError, ValueError):
        return 0


def stored_science(state: dict) -> float:
    """Currently stored SciencePoints from ``state["resources"]`` (0 if absent)."""
    for item in (state.get("resources") or []) if isinstance(state, dict) else []:
        if not isinstance(item, dict):
            continue
        if str(item.get("good", "")).lower() in ("sciencepoints", "science"):
            try:
                return float(item.get("stored") or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


@lru_cache(maxsize=1)
def _prereq_tokens() -> Dict[str, List[str]]:
    """bare spec -> raw prerequisite tokens.

    tech_tree.json 'unlocks' is the authoritative prereq graph (keyed by display
    name); buildings.json 'prerequisites' (mostly empty, but carrying terrain-gate
    tokens like 'Aquifer tile' for a few special buildings) is merged in. Tokens
    stay raw here; _tech_graph / _externally_gated resolve them.
    """
    name_to_spec = resource_manager._building_name_to_spec()
    out: Dict[str, List[str]] = {}
    for unlock in _tech().get("unlocks", []) or []:
        spec = name_to_spec.get(unlock.get("name"))
        if not spec:
            continue
        bucket = out.setdefault(spec, [])
        for pre in unlock.get("prerequisites") or []:
            if pre not in bucket:
                bucket.append(pre)
    for spec, building in _building_by_spec().items():
        for pre in building.get("prerequisites") or []:
            bucket = out.setdefault(spec, [])
            if pre not in bucket:
                bucket.append(pre)
    return out


@lru_cache(maxsize=1)
def _externally_gated() -> frozenset:
    """Specs with a prereq token that does NOT resolve to a building.

    Those are terrain/map gates (an 'Aquifer tile', a 'Geothermal Field tile') the
    tech reasoner cannot satisfy or verify, so such a spec is treated as not
    unlockable-from-tech unless a built instance already exists.
    """
    name_to_spec = resource_manager._building_name_to_spec()
    gated = set()
    for spec, tokens in _prereq_tokens().items():
        if any(tok not in name_to_spec for tok in tokens):
            gated.add(spec)
    return frozenset(gated)


@lru_cache(maxsize=1)
def _tech_graph():
    """networkx DiGraph of building->building prereq edges (prereq -> dependent).

    Only edges whose prereq token resolves to a real building are added (terrain
    gates are handled by _externally_gated). networkx gives clean predecessor
    lookup and robust cycle detection over this small DAG.
    """
    import networkx as nx

    name_to_spec = resource_manager._building_name_to_spec()
    graph = nx.DiGraph()
    for spec, tokens in _prereq_tokens().items():
        graph.add_node(spec)
        for tok in tokens:
            pre_spec = name_to_spec.get(tok)
            if pre_spec:
                graph.add_node(pre_spec)
                graph.add_edge(pre_spec, spec)
    return graph


def _building_prereqs(spec: str) -> List[str]:
    """Building-spec prerequisites of ``spec`` (resolved; terrain gates excluded)."""
    graph = _tech_graph()
    bare = _bare(spec)
    if bare not in graph:
        return []
    return list(graph.predecessors(bare))


@lru_cache(maxsize=1)
def _buildable_specs() -> List[str]:
    """Bare specs with a real build_<x> gameplay action (the unlock universe), sorted."""
    return sorted(s for s in _building_by_spec() if game_schema.spec_to_action(s))


def _is_unlockable(spec: str, state: dict, stored: float, visited: frozenset) -> bool:
    """Recursive unlockability under the 3b rule (see unlockable_now)."""
    bare = _bare(spec)
    if game_schema._count(state, bare) > 0:
        return True  # a built instance implies the spec is already unlocked
    if bare in visited:
        return False  # cycle guard (conservative); the tech graph is a DAG anyway
    if bare in _externally_gated():
        return False  # needs a special map tile we cannot satisfy from tech
    visited = visited | {bare}
    for prereq in _building_prereqs(bare):
        if not _is_unlockable(prereq, state, stored, visited):
            return False
    cost = science_cost(bare)
    if cost <= 0:
        return True  # start-tier: always available
    return stored >= cost


def unlockable_now(state: dict) -> List[str]:
    """Specs the colony can build NOW given tech (sorted, de-duped).

    Rule (a behavioral-cloning heuristic): a spec is unlockable iff
      * a built instance already exists (built => unlocked), OR
      * its ``science_cost`` is 0 (start-tier, always available), OR
      * ALL its prerequisites are met AND stored SciencePoints >= ``science_cost``.
    A prerequisite is "met" when the prereq building is built, is itself start-tier,
    or is itself recursively unlockable (cycle-guarded). Prereqs come from
    tech_tree.json's unlock graph (authoritative, display-name keyed); science_cost
    from buildings.json. Buildings gated on a terrain feature (e.g. a Geothermal
    Field tile) are excluded unless already built. Costs are NOT summed across the
    prereq chain -- each level is judged against current stored science.
    """
    state = state if isinstance(state, dict) else {}
    stored = stored_science(state)
    return [
        spec
        for spec in _buildable_specs()
        if _is_unlockable(spec, state, stored, frozenset())
    ]


@lru_cache(maxsize=1)
def recommended_order() -> List[str]:
    """Bare specs in tech_tree.recommended_order (resolved; unknowns dropped)."""
    name_to_spec = resource_manager._building_name_to_spec()
    order: List[str] = []
    for row in _tech().get("recommended_order", []) or []:
        spec = name_to_spec.get(row.get("name"))
        if spec and spec not in order:
            order.append(spec)
    return order


def recommended_index(spec: str) -> int:
    """Tiebreak rank of ``spec`` in recommended_order (len(order) if unlisted)."""
    order = recommended_order()
    bare = _bare(spec)
    return order.index(bare) if bare in order else len(order)


# =============================================================================
# Demand / supply over the current game state
# =============================================================================
def _is_produced(good: str, state: dict) -> bool:
    """True if some currently-built building produces ``good``."""
    for spec in game_schema._producers().get(good, []) or []:
        if game_schema._count(state, spec) > 0:
            return True
    return False


def _demanders(good: str, state: dict) -> List[str]:
    """Active consumers of ``good``: "construction" and any BUILT consumer name."""
    entry = _chains().get(good) or {}
    name_to_spec = resource_manager._building_name_to_spec()
    out: List[str] = []
    for consumer in entry.get("consumed_by") or []:
        if consumer in _ACTIVE_TERMINALS:
            out.append(consumer)
            continue
        spec = name_to_spec.get(consumer)
        if spec and game_schema._count(state, spec) > 0:
            out.append(consumer)
    return out


def _is_demanded(good: str, state: dict) -> bool:
    return bool(_demanders(good, state))


def _good_depth(good: str, _stack: Optional[frozenset] = None) -> int:
    """Chain depth of ``good``: 0 for raw/gathered/grown, else 1 + deepest input.

    Uses the same simplest-producer choice as resource_manager.production_chain_for
    so the depth matches the chain that is actually walked. Cycle-guarded.
    """
    stack = _stack or frozenset()
    entry = _chains().get(good)
    if not entry or good in stack:
        return 0
    producer = resource_manager._pick_producer(entry)
    inputs = (producer or {}).get("inputs") or []
    if not inputs:
        return 0
    return 1 + max(_good_depth(inp, stack | {good}) for inp in inputs)


# =============================================================================
# The 3a planner interface
# =============================================================================
def producer_plan(state: dict) -> List[Dict[str, object]]:
    """Ordered producers to add so every demanded good gets a supplier.

    For each good that is demanded-but-unproduced, walk its production chain from
    raw resources upward (resource_manager.production_chain_for) and collect every
    step whose producer is not yet built. Walking the chain means an input's
    producer is always collected before the refined producer that needs it (you
    never suggest a Lumber Mill's downstream before the Lumber Mill). The combined
    list is then globally ordered raw->refined by chain depth and de-duped.

    Each item: {spec, good, depth, cost_logs, why}.
    """
    state = state if isinstance(state, dict) else {}
    plan: List[Dict[str, object]] = []
    seen_specs = set()

    for good in _chains():
        if not _is_demanded(good, state) or _is_produced(good, state):
            continue
        for step in resource_manager.production_chain_for(good):
            spec = step.get("spec")
            step_good = step.get("good")
            if not spec or spec in seen_specs:
                continue  # unmapped producer (e.g. "unknown") or already collected
            if game_schema._count(state, spec) > 0:
                continue  # producer already exists
            seen_specs.add(spec)
            plan.append(
                {
                    "spec": spec,
                    "good": step_good,
                    "depth": _good_depth(step_good),
                    "cost_logs": log_cost(spec),
                    "why": _why(spec, step_good, state),
                }
            )

    # Global raw->refined order; spec breaks depth ties for determinism.
    plan.sort(key=lambda item: (item["depth"], item["spec"]))
    return plan


def needed_producers(state: dict) -> List[str]:
    """Bare producer specs to add, raw->refined, de-duped (see producer_plan)."""
    return [str(item["spec"]) for item in producer_plan(state)]


def _why(spec: str, good: str, state: dict) -> str:
    demanders = _demanders(good, state)
    if demanders:
        who = ", ".join(demanders)
        return "%s is consumed by %s but has no producer; build %s" % (good, who, spec)
    # Reached as a deeper input of a demanded chain (its own good is not yet
    # independently demanded, but the refined good above it is).
    return "%s feeds a demanded production chain; build %s" % (good, spec)


__all__ = [
    "needed_producers",
    "producer_plan",
    "log_cost",
    "unlockable_now",
    "science_cost",
    "stored_science",
    "recommended_order",
    "recommended_index",
]


if __name__ == "__main__":  # pragma: no cover - manual smoke check
    demo = {"buildings": {"counts": {"LumberjackFlag.Folktails": 1}}}
    print("needed_producers:", needed_producers(demo))
    for row in producer_plan(demo):
        print(" ", row["spec"], "d=%s" % row["depth"], "logs=%s" % row["cost_logs"], "-", row["why"])
