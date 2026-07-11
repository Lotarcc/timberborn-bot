"""Shared path-network planner for the Timberborn agent.

PURE functions only. No network / bridge / HTTP calls, no torch, no numpy.
stdlib only.

Purpose
-------
Plan an efficient SHARED path network (hub-and-spoke / greedy-Steiner-ish)
connecting the District Center road to many building access tiles while
minimizing total pavement and avoiding redundant parallel paths.

The naive approach routes every building independently to the DC road, which
produces many overlapping / parallel paths. Instead we grow a single network:
route each building to the CURRENT network (DC road plus every tile chosen so
far), then fold that new path into the network so later buildings can branch
off the shared spine.

Map schema (dict) -- see module docstring in the task:
- map_data["origin"] = {"x": int, "z": int}, plus "width", "height"
- row-major arrays (index = row * width + col;
  tile x = origin.x + col, tile y = origin.z + row):
    "terrain_height" (int surface z per tile)
    "water_depth"    (float; > 0 means water)
    "occupied"       (0/1)
    "on_road"        (0/1 already district road)
    "reachable"      (0/1)

Pavement rules
--------------
A tile is PAVABLE (a walkable path candidate) if:
  - it is not occupied, OR it is already on_road, and
  - water_depth == 0, and
  - it is in-bounds.
Movement is 4-neighbour, cost 1 per step, only between tiles whose terrain
heights differ by <= 1 (beavers ramp a single level).

Tiles are represented as (x, y, z) tuples where z is terrain_height at (x, y).
"""

from __future__ import annotations

import heapq
import unittest


# ---------------------------------------------------------------------------
# Internal helpers: turn the raw map_data dict into a compact "map_arrays"
# view with O(1) tile lookups keyed by (x, y).
# ---------------------------------------------------------------------------

def build_map_arrays(map_data: dict) -> dict:
    """Build a lookup-friendly view of the raw map_data.

    Returns a dict with:
      origin (x, z), width, height, and per-(x, y) dicts:
      height[(x, y)], water[(x, y)], occupied[(x, y)], on_road[(x, y)],
      reachable[(x, y)].
    """
    origin = map_data["origin"]
    ox, oz = origin["x"], origin["z"]
    width = map_data["width"]
    height = map_data["height"]

    terrain = map_data["terrain_height"]
    water = map_data.get("water_depth", [0] * (width * height))
    occupied = map_data.get("occupied", [0] * (width * height))
    on_road = map_data.get("on_road", [0] * (width * height))
    reachable = map_data.get("reachable", [1] * (width * height))

    h_map = {}
    w_map = {}
    occ_map = {}
    road_map = {}
    reach_map = {}

    for row in range(height):
        for col in range(width):
            idx = row * width + col
            x = ox + col
            y = oz + row
            key = (x, y)
            h_map[key] = terrain[idx]
            w_map[key] = water[idx]
            occ_map[key] = occupied[idx]
            road_map[key] = on_road[idx]
            reach_map[key] = reachable[idx]

    return {
        "origin": (ox, oz),
        "width": width,
        "height": height,
        "height_map": h_map,
        "water": w_map,
        "occupied": occ_map,
        "on_road": road_map,
        "reachable": reach_map,
    }


def _in_bounds(map_arrays: dict, x: int, y: int) -> bool:
    return (x, y) in map_arrays["height_map"]


def _tile_z(map_arrays: dict, x: int, y: int):
    return map_arrays["height_map"].get((x, y))


def is_pavable(map_arrays: dict, x: int, y: int) -> bool:
    """A tile can hold a path if in-bounds, dry, and free (or already road)."""
    if not _in_bounds(map_arrays, x, y):
        return False
    if map_arrays["water"][(x, y)] > 0:
        return False
    if map_arrays["occupied"][(x, y)] and not map_arrays["on_road"][(x, y)]:
        return False
    return True


def _as_tile(map_arrays: dict, x: int, y: int):
    """Return (x, y, z) tuple for a tile using its terrain height as z."""
    return (x, y, _tile_z(map_arrays, x, y))


# ---------------------------------------------------------------------------
# 1. walkable_neighbors
# ---------------------------------------------------------------------------

_DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1))


def walkable_neighbors(map_arrays: dict, tile) -> list:
    """4-neighbour walkable tiles respecting pavable + height-<=1 rules.

    `tile` may be (x, y) or (x, y, z); returns a list of (x, y, z) tiles.
    The origin tile itself must be pavable for its neighbours to matter, but
    we do not enforce that here (route() controls starts/goals).
    """
    x, y = tile[0], tile[1]
    z = _tile_z(map_arrays, x, y)
    if z is None:
        return []

    out = []
    for dx, dy in _DIRS:
        nx, ny = x + dx, y + dy
        if not is_pavable(map_arrays, nx, ny):
            continue
        nz = _tile_z(map_arrays, nx, ny)
        if abs(nz - z) > 1:
            continue
        out.append((nx, ny, nz))
    return out


# ---------------------------------------------------------------------------
# 2. route -- Dijkstra (uniform cost => BFS) to the NEAREST target tile.
# ---------------------------------------------------------------------------

def route(map_arrays: dict, start_tile, targets) -> list:
    """Shortest walkable path from start to the nearest tile in `targets`.

    - `start_tile` is (x, y) or (x, y, z).
    - `targets` is an iterable of tiles; matching is done on (x, y).
    Returns a list of (x, y, z) tiles from start to the reached target
    inclusive, or [] if no target is reachable over land.
    If the start is already a target, returns just the start tile.
    """
    target_xy = {(t[0], t[1]) for t in targets}
    if not target_xy:
        return []

    sx, sy = start_tile[0], start_tile[1]
    if not _in_bounds(map_arrays, sx, sy):
        return []

    start = _as_tile(map_arrays, sx, sy)
    if (sx, sy) in target_xy:
        return [start]

    # Uniform-cost search (Dijkstra with unit edges == BFS), but we keep the
    # heap form so the design generalizes if edge costs are added later.
    dist = {(sx, sy): 0}
    prev = {(sx, sy): None}
    pq = [(0, (sx, sy))]

    reached = None
    while pq:
        d, (cx, cy) = heapq.heappop(pq)
        if d > dist.get((cx, cy), float("inf")):
            continue
        if (cx, cy) in target_xy:
            reached = (cx, cy)
            break
        for nx, ny, _nz in walkable_neighbors(map_arrays, (cx, cy)):
            nd = d + 1
            if nd < dist.get((nx, ny), float("inf")):
                dist[(nx, ny)] = nd
                prev[(nx, ny)] = (cx, cy)
                heapq.heappush(pq, (nd, (nx, ny)))

    if reached is None:
        return []

    # Reconstruct.
    path = []
    node = reached
    while node is not None:
        path.append(_as_tile(map_arrays, node[0], node[1]))
        node = prev[node]
    path.reverse()
    return path


# ---------------------------------------------------------------------------
# 3. plan_network -- greedy Steiner shared spine.
# ---------------------------------------------------------------------------

def _xy(tile):
    return (tile[0], tile[1])


def plan_network(map_data: dict, dc_road_tiles: list,
                 building_access_tiles: list) -> dict:
    """Grow a shared path network from the DC road to every building tile.

    Greedy Steiner heuristic:
      1. Build the current network = set of DC road tile (x, y).
      2. Sort buildings by shortest distance to the current network.
      3. Route the nearest building to the network; add every tile of that
         path (excluding the building access tile itself and existing
         network tiles) to `paths`, and add all path tiles to the network so
         later buildings can branch off the freshly-paved spine.
      4. Re-sort remaining buildings against the grown network and repeat.
      Buildings with no land route (e.g. across water) go to `unreachable`.

    Returns dict:
      "paths"       -> sorted list of (x, y, z) tiles to pave (de-duplicated,
                       excludes tiles already part of the DC road)
      "unreachable" -> list of building access tiles with no land route
      "total_tiles" -> len(paths)
    """
    map_arrays = build_map_arrays(map_data)

    # network_xy: (x, y) of everything a beaver can already walk on
    # (DC road plus paths chosen so far).
    network_xy = set()
    for t in dc_road_tiles:
        network_xy.add(_xy(t))

    # paths_xy: tiles WE decide to pave (excludes pre-existing DC road).
    paths_xy = set()

    # Normalize buildings to (x, y) and remember insertion order for stable
    # output / tie-breaking.
    pending = []
    for b in building_access_tiles:
        pending.append((b[0], b[1]))

    unreachable = []

    # Iteratively attach the nearest pending building to the current network.
    while pending:
        best = None  # (path, building_xy, index)
        still_pending = []

        # For each pending building, route to the current network. Pick the
        # one with the shortest route this round (greedy nearest-first). This
        # re-routes every round because the network grows, which is what makes
        # the spine shared rather than parallel.
        best_len = None
        best_path = None
        best_bxy = None
        best_idx = None

        for idx, bxy in enumerate(pending):
            # A building already sitting on the network needs no path.
            if bxy in network_xy or bxy in paths_xy:
                best_len = 0
                best_path = [_as_tile(map_arrays, bxy[0], bxy[1])]
                best_bxy = bxy
                best_idx = idx
                break

            targets = set()
            for nxy in network_xy:
                targets.add((nxy[0], nxy[1]))
            for pxy in paths_xy:
                targets.add((pxy[0], pxy[1]))

            path = route(map_arrays, bxy, targets)
            if not path:
                continue
            plen = len(path)
            if best_len is None or plen < best_len:
                best_len = plen
                best_path = path
                best_bxy = bxy
                best_idx = idx

        if best_path is None:
            # Nobody left can reach the network over land.
            for bxy in pending:
                unreachable.append(_as_tile(map_arrays, bxy[0], bxy[1]))
            break

        # Commit the chosen building's path into the network.
        for tile in best_path:
            txy = _xy(tile)
            # The building access tile itself is a destination, not pavement
            # we own; but intermediate tiles become shared road. We add every
            # tile to the walkable network so future routes can branch here.
            network_xy.add(txy)
            # Only record as "to pave" if it isn't pre-existing DC road.
            if txy not in {_xy(t) for t in dc_road_tiles}:
                # Exclude the building access endpoint from paths: it's the
                # building's own access tile, not a path tile we lay down.
                if txy != best_bxy:
                    paths_xy.add(txy)

        # Drop the committed building from pending.
        for idx, bxy in enumerate(pending):
            if idx == best_idx:
                continue
            still_pending.append(bxy)
        pending = still_pending

    paths = sorted(_as_tile(map_arrays, x, y) for (x, y) in paths_xy)
    return {
        "paths": paths,
        "unreachable": unreachable,
        "total_tiles": len(paths),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _make_flat_map(width=12, height=12, base_z=4, origin=(0, 0)):
    """All-flat map, no water, nothing occupied, everything reachable."""
    n = width * height
    return {
        "origin": {"x": origin[0], "z": origin[1]},
        "width": width,
        "height": height,
        "terrain_height": [base_z] * n,
        "water_depth": [0.0] * n,
        "occupied": [0] * n,
        "on_road": [0] * n,
        "reachable": [1] * n,
    }


def _set_tile(map_data, x, y, **kwargs):
    ox = map_data["origin"]["x"]
    oz = map_data["origin"]["z"]
    col = x - ox
    row = y - oz
    idx = row * map_data["width"] + col
    for k, v in kwargs.items():
        map_data[k][idx] = v


class PathNetworkTests(unittest.TestCase):

    def setUp(self):
        # 12x12 flat map at height 4.
        self.map_data = _make_flat_map(12, 12, base_z=4)

        # A vertical strip of water at x == 8 (rows 0..11) splits the map.
        for y in range(12):
            _set_tile(self.map_data, 8, y, water_depth=1.0)

        # A couple of occupied blocks (buildings sitting on the map).
        for (bx, by) in [(3, 3), (3, 4), (4, 3), (4, 4)]:
            _set_tile(self.map_data, bx, by, occupied=1)
        for (bx, by) in [(5, 8), (6, 8)]:
            _set_tile(self.map_data, bx, by, occupied=1)

        # DC road: a small stub near the left edge at row y=0.
        for x in range(0, 2):
            _set_tile(self.map_data, x, 0, on_road=1)
        self.dc_road = [(0, 0, 4), (1, 0, 4)]

    def test_walkable_neighbors_flat(self):
        arrays = build_map_arrays(self.map_data)
        nbrs = walkable_neighbors(arrays, (5, 5))
        self.assertEqual(len(nbrs), 4)
        for t in nbrs:
            self.assertEqual(len(t), 3)
            self.assertEqual(t[2], 4)

    def test_walkable_neighbors_blocks_water_and_occupied(self):
        arrays = build_map_arrays(self.map_data)
        # Tile (7,5) has water to its east (8,5) -> only 3 walkable neighbours.
        nbrs = walkable_neighbors(arrays, (7, 5))
        xs = {(t[0], t[1]) for t in nbrs}
        self.assertNotIn((8, 5), xs)
        self.assertEqual(len(nbrs), 3)

    def test_walkable_neighbors_height_step(self):
        # Build a tiny map with a 2-height cliff.
        m = _make_flat_map(3, 1, base_z=4)
        _set_tile(m, 2, 0, terrain_height=6)  # cliff (diff 2 from x=1)
        arrays = build_map_arrays(m)
        nbrs = walkable_neighbors(arrays, (1, 0))
        xs = {(t[0], t[1]) for t in nbrs}
        self.assertIn((0, 0), xs)      # diff 0 ok
        self.assertNotIn((2, 0), xs)   # diff 2 too steep

    def test_route_reaches_nearest_target(self):
        arrays = build_map_arrays(self.map_data)
        targets = {(0, 0), (1, 0)}
        path = route(arrays, (5, 5), targets)
        self.assertTrue(path)
        self.assertIn((path[-1][0], path[-1][1]), targets)
        self.assertEqual((path[0][0], path[0][1]), (5, 5))

    def test_route_across_water_unreachable(self):
        arrays = build_map_arrays(self.map_data)
        # Start on the RIGHT of the water strip, target on the LEFT road.
        targets = {(0, 0)}
        path = route(arrays, (10, 5), targets)
        self.assertEqual(path, [])

    def test_plan_network_connects_reachable(self):
        # Two buildings on the left (reachable) side of the water.
        buildings = [(5, 5, 4), (7, 5, 4)]
        result = plan_network(self.map_data, self.dc_road, buildings)
        self.assertEqual(result["unreachable"], [])
        self.assertGreater(result["total_tiles"], 0)

        # Every building must be adjacent to (or on) the paved network.
        paved = {(t[0], t[1]) for t in result["paths"]}
        road = {(t[0], t[1]) for t in self.dc_road}
        network = paved | road
        for (bx, by, _bz) in buildings:
            adj = {(bx + dx, by + dy) for dx, dy in _DIRS}
            self.assertTrue(
                (bx, by) in network or adj & network,
                msg=f"building {(bx, by)} not connected to network",
            )

    def test_plan_network_shares_spine(self):
        # Two NEARBY buildings: a shared spine should cost less than routing
        # each independently to the DC road.
        b1 = (5, 5, 4)
        b2 = (7, 5, 4)
        arrays = build_map_arrays(self.map_data)
        road_targets = {(t[0], t[1]) for t in self.dc_road}

        # Independent routing (what the current per-building planner does):
        # each building routes to the DC road on its own, with no knowledge of
        # the other, so their corridors are paved SEPARATELY. The cost is the
        # SUM of per-building path tiles (redundant parallel paving).
        ends = {(b1[0], b1[1]), (b2[0], b2[1])}
        indep_count = 0
        for b in (b1, b2):
            for t in route(arrays, b, road_targets):
                if (t[0], t[1]) not in road_targets and (t[0], t[1]) not in ends:
                    indep_count += 1

        shared = plan_network(self.map_data, self.dc_road, [b1, b2])
        shared_count = shared["total_tiles"]

        self.assertLess(
            shared_count, indep_count,
            msg=f"shared spine ({shared_count}) not cheaper than "
                f"independent ({indep_count})",
        )
        self.shared_savings = (indep_count, shared_count)

    def test_plan_network_reports_across_water(self):
        # One reachable, one on the far side of the water strip.
        b_ok = (5, 5, 4)
        b_far = (10, 5, 4)
        result = plan_network(self.map_data, self.dc_road, [b_ok, b_far])
        unreachable_xy = {(t[0], t[1]) for t in result["unreachable"]}
        self.assertIn((10, 5), unreachable_xy)
        self.assertNotIn((5, 5), unreachable_xy)


def _report_savings():
    """Print a one-line savings comparison for the shared-spine test case."""
    map_data = _make_flat_map(12, 12, base_z=4)
    for y in range(12):
        _set_tile(map_data, 8, y, water_depth=1.0)
    for x in range(0, 2):
        _set_tile(map_data, x, 0, on_road=1)
    dc_road = [(0, 0, 4), (1, 0, 4)]

    arrays = build_map_arrays(map_data)
    b1, b2 = (5, 5, 4), (7, 5, 4)
    road_targets = {(t[0], t[1]) for t in dc_road}
    ends = {(b1[0], b1[1]), (b2[0], b2[1])}

    indep = 0
    for b in (b1, b2):
        for t in route(arrays, b, road_targets):
            if (t[0], t[1]) not in road_targets and (t[0], t[1]) not in ends:
                indep += 1

    shared = plan_network(map_data, dc_road, [b1, b2])["total_tiles"]
    print(f"[savings] independent routing = {indep} tiles, "
          f"shared spine = {shared} tiles "
          f"({indep - shared} fewer, "
          f"{100.0 * (indep - shared) / indep:.0f}% less pavement).")


if __name__ == "__main__":
    _report_savings()
    unittest.main(verbosity=2)
