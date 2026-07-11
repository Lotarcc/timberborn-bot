"""Agent-owned pathing.

The bridge's per-building auto-connect computes an independent SHORTEST path from each
building to the nearest road tile - myopic, producing a tangle of parallel hops. This
module instead plans ONE coherent path network rooted at the District Center's single
entrance and grows it as a SHARED TRUNK: every building attaches to the nearest point of
the network built so far, so the result is an efficient spine that makes sense long-term
when many buildings sit at different places/altitudes (greedy-Steiner via path_network).

Usage in the play loop:
  - place buildings with {"auto_connect": false} so the bridge lays NO paths
  - after placements, call connect_all(bridge, state, map_data) to plan + pave the trunk
Paths are placed as construction sites (built by beavers). Tiles the planner can't reach
by land (across water) are returned in 'unreachable' for a crossing pass.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from agent import path_network as PN

Tile = Tuple[int, int, int]


def _dc_entrance(state: dict) -> Optional[Tile]:
    """The DC's single access/entrance tile - the root the trunk grows from."""
    for b in ((state.get("buildings") or {}).get("list") or []):
        if str(b.get("spec", "")).startswith("DistrictCenter"):
            acc = b.get("access")
            if acc:
                a = acc[0]
                return (a["x"], a["y"], a["z"])
    d = state.get("district_center") or {}
    if d.get("x") is not None:
        return (d["x"], d["y"], d.get("z", 4))
    return None


def _building_accesses(state: dict) -> List[Tile]:
    """Access tile of every real building (not the DC, not paths/platforms)."""
    out: List[Tile] = []
    for b in ((state.get("buildings") or {}).get("list") or []):
        sp = str(b.get("spec", ""))
        if sp.startswith(("DistrictCenter", "Path", "Platform")):
            continue
        acc = b.get("access")
        if acc:
            a = acc[0]
            out.append((a["x"], a["y"], a["z"]))
    return out


def _onroad_tiles(map_data: dict) -> List[Tile]:
    """Every tile already on the district-road network (DC road + built paths). The trunk
    reuses these so we never re-pave, and the network stays rooted at the DC."""
    width = map_data.get("width", 0)
    origin = map_data.get("origin") or {}
    ox = origin.get("x", 0)
    oy = origin.get("z", origin.get("y", 0))
    onroad = map_data.get("on_road") or []
    heights = map_data.get("terrain_height") or []
    out: List[Tile] = []
    for i, v in enumerate(onroad):
        if v:
            r, c = divmod(i, width)
            z = heights[i] if i < len(heights) else 4
            out.append((ox + c, oy + r, z))
    return out


def _existing_path_tiles(state: dict) -> List[Tile]:
    """Every Path/Platform already placed - built OR still a construction site. These must
    seed the network so the planner REUSES them; otherwise unbuilt path sites (not yet on
    the road) are invisible to it and it re-routes each cycle, paving redundant parallel
    paths that pile into a grid (the 'too many paths' bug)."""
    out: List[Tile] = []
    for b in ((state.get("buildings") or {}).get("list") or []):
        if str(b.get("spec", "")).startswith(("Path", "Platform")):
            out.append((b["x"], b["y"], b["z"]))
    return out


def plan(state: dict, map_data: dict) -> dict:
    """Plan the trunk network rooted at the DC entrance (+ existing road + existing paths),
    covering every building access tile. Returns path_network.plan_network's dict."""
    entrance = _dc_entrance(state)
    accesses = _building_accesses(state)
    seed: List[Tile] = []
    if entrance:
        seed.append(entrance)
    seed.extend(_onroad_tiles(map_data))
    seed.extend(_existing_path_tiles(state))  # reuse built AND unbuilt paths
    if not seed:
        return {"paths": [], "unreachable": accesses, "total_tiles": 0}
    return PN.plan_network(map_data, seed, accesses)


def connect_all(bridge, state: dict, map_data: dict) -> dict:
    """Plan the trunk and pave every missing tile as a Path construction site."""
    p = plan(state, map_data)
    laid: List[Tile] = []
    failed: List[dict] = []
    for tile in p.get("paths", []):
        x, y, z = tile[0], tile[1], tile[2]
        status, body = bridge.act("place_building",
                                  {"spec": "Path", "x": x, "y": y, "z": z, "auto_connect": False})
        if isinstance(body, dict) and body.get("ok"):
            laid.append((x, y, z))
        else:
            failed.append({"tile": (x, y, z),
                           "error": body.get("error") if isinstance(body, dict) else status})
    return {"plan": p, "laid": laid, "failed": failed,
            "unreachable": p.get("unreachable", []), "trunk_tiles": p.get("total_tiles", 0)}


__all__ = ["plan", "connect_all"]
