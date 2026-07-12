"""Footprint-aware 3-D reservation grid -- the substrate for the coordinated
spatial layout planner (LP1 of `docs/kb/layout-planner-design.md`).

`Reservation` is a sparse `{(x, y, z): owner}` map over world tile coordinates,
where owner is one of the short tags in the owner vocabulary below. It answers
the two questions today's greedy, 1x1-at-ground-z placer (`agent/placement.py`)
cannot: "does this building's FULL rotated footprint overlap anything else" and
"is the base of this footprint actually supported" (on terrain, or on a
stackable roof). Later LPs (zones, batch placement, verticality fallback -- see
the design doc) sit on top of this grid; LP1 only builds the grid itself plus
reconciliation from the bridge's `/map` and `/state`.

## Rotation convention (v1, documented simplification)
A building's footprint size `{x, y, z}` (from `agent/data/building_stacking.json`)
is defined for orientation "N". "S" keeps the same `(size_x, size_y)` horizontal
footprint as "N" -- this module does not model the point-mirroring a true compass
rotation would apply, only the axis swap that matters for overlap/support math.
"E" and "W" both SWAP the horizontal footprint to `(size_y, size_x)`. That is
enough to reserve the right set of cells for any of the 4 placement orientations
the bridge accepts; it does not track *which* edge is the entrance. `z` (vertical
size) is never swapped. Orientation input accepts both the "N"/"S"/"E"/"W" short
codes and the bridge's full compass words ("North"/"South"/"East"/"West");
anything else (including missing/None) defaults to "N".

## Support convention (v1, documented simplification)
Ground truth (decompiled, see `docs/kb/placement-verticality-gaps.md`): a
building can occupy world Z on top of Z-1 iff its base cell's `MatterBelow`
allows it AND the cell below has `Stackable = BlockObject`. We approximate
`MatterBelow` with `building_stacking.json`'s per-spec (not per-cell) `base_matter`
and `Stackable = BlockObject` with a spec's `stackable` flag:
  - "ground": every base cell must sit exactly on the terrain surface, i.e.
    `terrain_height_at(x, y) == z`.
  - "stackable": every base cell must have a cell owned "built", "platform" or
    "reserved" at `(x, y, z-1)` belonging to a spec whose `stackable` flag is
    True. ("path" does not count -- a Path tile is not a buildable roof.)
  - "ground_or_stackable" / "any" / "air": EITHER of the above satisfies the
    cell (a v1 simplification -- true `Air` needs no support at all; there is no
    real case of it in the current data, so it is folded into this bucket
    instead of an always-true rule. See the LP1 report for the rationale.)
A spec missing from `building_stacking.json` defaults to size 1x1x1, base_matter
"ground", not stackable.

## Zones (LP2)
`assign_zones(map_data, state=None) -> Zones` partitions the reachable LAND
around the District Center into per-category regions -- the "memory of where
you want what" from the design doc's step 1 (`docs/kb/layout-planner-design.md`).
It is a separate, persisted structure from `Reservation`: `Reservation` tracks
what IS built/claimed; `Zones` tracks what AREA each building CATEGORY should
draw from when a batch placer (LP3, not built here) looks for a tile. Compute
it once per colony and hold onto the result -- `Zones.reconcile(map_data,
state)` drops tiles the colony has since built on without re-scoring the
partition. See the `Zones`/`assign_zones` docstrings below for the
per-category heuristics.

Pure-stdlib, no network/torch (this module's own code; `agent.spatial`, its one
sibling dependency, is pure-stdlib too). Runs standalone: `python3 agent/layout.py`.
"""

from __future__ import annotations

import json
import math
import os
import sys
import unittest
from collections import deque

# `agent/` has no __init__.py, and this module is designed to run standalone
# (`python3 agent/layout.py`) as well as via package-qualified imports (`python3
# -m unittest agent.layout`), so try the package-relative import first and fall
# back to the bare sibling import that resolves when sys.path[0] is agent/
# itself. Mirrors the identical fallback at the top of replay.py.
try:
    from agent import spatial
except ImportError:
    import spatial

_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_AGENT_DIR, "data")
_STACKING_PATH = os.path.join(_DATA_DIR, "building_stacking.json")
_BUILDINGS_PATH = os.path.join(_DATA_DIR, "buildings.json")

# --- owner vocabulary ---------------------------------------------------------
BUILT = "built"
RESERVED = "reserved"
PATH = "path"
PLATFORM = "platform"
# Documented, but never written into `Reservation.cells` -- see
# `Reservation.reconcile_from_map`. Ground support is derived on demand from a
# `terrain_height_at` callable instead, keeping the grid sparse.
TERRAIN = "terrain"

_STACKABLE_SUPPORT_OWNERS = (BUILT, PLATFORM, RESERVED)
_RECONCILABLE_STATUSES = ("finished", "site")
_DEFAULT_BASE_MATTER = "ground"

_ORIENTATION_ALIASES = {
    "N": "N", "NORTH": "N",
    "S": "S", "SOUTH": "S",
    "E": "E", "EAST": "E",
    "W": "W", "WEST": "W",
}


def _normalize_orientation(value):
    """Map "N"/"South"/"east"/... to the canonical "N"/"S"/"E"/"W"; missing or
    unrecognized values default to "N" (the bridge's default facing)."""
    if not value:
        return "N"
    return _ORIENTATION_ALIASES.get(str(value).strip().upper(), "N")


def _as_int(value, default=0):
    """Best-effort int coercion that never raises (booleans excluded)."""
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_STACKING_CACHE = None


def load_stacking_data(path=None):
    """Load `building_stacking.json` defensively: a missing/unreadable file or a
    malformed top-level value yields `{}`, so every spec lookup falls back to the
    1x1x1/"ground" default rather than raising. Results for the default path are
    cached in-process (the file is static per game version)."""
    global _STACKING_CACHE
    if path is None and _STACKING_CACHE is not None:
        return _STACKING_CACHE
    target = path or _STACKING_PATH
    try:
        with open(target, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data = {key: value for key, value in data.items() if key != "_meta"}
    if path is None:
        _STACKING_CACHE = data
    return data


def terrain_height_lookup(map_data):
    """Build a `terrain_height_at(x, y) -> int|None` callable from a `/map`
    payload (row-major `terrain_height`, `origin{x,z}`, `width`/`height`) for use
    with `Reservation.supported`/`fits`. Out-of-bounds or missing data -> None."""
    if not isinstance(map_data, dict):
        return lambda x, y: None
    width = _as_int(map_data.get("width"), 0)
    height = _as_int(map_data.get("height"), 0)
    origin = map_data.get("origin") or {}
    origin_x = _as_int(origin.get("x", map_data.get("origin_x", 0)), 0)
    origin_y = _as_int(origin.get("z", origin.get("y", map_data.get("origin_y", 0))), 0)
    terrain = map_data.get("terrain_height") or map_data.get("terrain") or []

    def lookup(x, y):
        col, row = _as_int(x) - origin_x, _as_int(y) - origin_y
        if width <= 0 or height <= 0 or not (0 <= col < width and 0 <= row < height):
            return None
        index = row * width + col
        if index >= len(terrain):
            return None
        return _as_int(terrain[index], None)

    return lookup


def _buildings_from_state(state):
    buildings = (state or {}).get("buildings") if isinstance(state, dict) else None
    blist = (buildings or {}).get("list") if isinstance(buildings, dict) else None
    return blist if isinstance(blist, list) else []


class Reservation:
    """Sparse 3-D occupancy: `self.cells` is `{(x, y, z): owner}` with `owner` one
    of `BUILT`/`RESERVED`/`PATH`/`PLATFORM` (see module docstring re: `TERRAIN`).

    A second, private index `self._cell_spec` remembers which spec occupies each
    written cell. This is NOT part of the spec'd `{(x,y,z): owner}` structure --
    it exists only so `supported()`'s "stackable" branch can ask "is the spec
    below actually stackable" instead of just "is *something* below" (see module
    docstring, Support convention). `reserve`/`reconcile_from_state` keep both
    dicts in sync; treat `cells` as the public structure and `_cell_spec` as an
    implementation detail a caller should not need to touch directly.
    """

    def __init__(self, stacking=None):
        self.cells = {}
        self._cell_spec = {}
        self.stacking = stacking if isinstance(stacking, dict) else load_stacking_data()
        # Cells THIS instance's last reconcile_from_{state,map} call wrote, so the
        # next call can clear exactly its own stale marks without touching the
        # other layer or the agent's own "reserved" slots (see reconcile docs).
        self._state_built = set()
        self._map_built = set()

    # -- spec metadata -------------------------------------------------------

    def spec_info(self, spec):
        """`building_stacking.json` entry for `spec`, tried exact then bare-prefix
        (buildings in `/state` may be faction-suffixed, e.g. "Lodge.Folktails",
        matching the `spec.split(".")[0]` convention used elsewhere, e.g.
        `agent/controller.py::_critical_unstaffed`)."""
        spec = str(spec or "")
        info = self.stacking.get(spec)
        if not isinstance(info, dict) and "." in spec:
            info = self.stacking.get(spec.split(".")[0])
        return info if isinstance(info, dict) else {}

    def size_of(self, spec):
        """`(size_x, size_y, size_z)` for `spec` at orientation "N"; missing spec
        or malformed size defaults to `(1, 1, 1)`."""
        size = self.spec_info(spec).get("size")
        size = size if isinstance(size, dict) else {}
        return (
            max(1, _as_int(size.get("x", 1), 1)),
            max(1, _as_int(size.get("y", 1), 1)),
            max(1, _as_int(size.get("z", 1), 1)),
        )

    def base_matter_of(self, spec):
        value = self.spec_info(spec).get("base_matter")
        return value if isinstance(value, str) and value else _DEFAULT_BASE_MATTER

    def is_stackable(self, spec):
        return bool(self.spec_info(spec).get("stackable"))

    # -- footprint -------------------------------------------------------------

    def footprint_cells(self, spec, x, y, z, orientation="N"):
        """All `(x, y, z)` cells `spec`'s rotated footprint occupies with its
        base at `(x, y, z)`. See module docstring for the rotation convention."""
        size_x, size_y, size_z = self.size_of(spec)
        if _normalize_orientation(orientation) in ("E", "W"):
            size_x, size_y = size_y, size_x
        x, y, z = _as_int(x), _as_int(y), _as_int(z)
        return [
            (x + dx, y + dy, z + dz)
            for dz in range(size_z)
            for dy in range(size_y)
            for dx in range(size_x)
        ]

    def is_free(self, cells):
        """True iff none of `cells` are already claimed by any owner."""
        return all(cell not in self.cells for cell in cells)

    def reserve(self, spec, x, y, z, orientation="N", owner=RESERVED):
        """Mark `spec`'s full footprint at `(x, y, z)` with `owner`. Returns the
        cells written. Does not check `is_free`/`fits` first -- callers that need
        that guarantee should check before reserving."""
        cells = self.footprint_cells(spec, x, y, z, orientation)
        for cell in cells:
            self.cells[cell] = owner
            self._cell_spec[cell] = spec
        return cells

    def free(self, spec, x, y, z, orientation="N"):
        """Clear `spec`'s full footprint at `(x, y, z)`. Returns the cells
        cleared. Assumes the caller passes the same args used to `reserve`."""
        cells = self.footprint_cells(spec, x, y, z, orientation)
        for cell in cells:
            self.cells.pop(cell, None)
            self._cell_spec.pop(cell, None)
        return cells

    # -- support ---------------------------------------------------------------

    def supported(self, spec, x, y, z, orientation="N", terrain_height_at=None):
        """True if every BASE-level (z) cell of `spec`'s footprint at (x, y, z)
        has valid support per its `base_matter` (see module docstring). Only the
        base level is checked -- a multi-Z building's upper cells rest on its own
        lower cells by construction, not on an external support."""
        size_x, size_y, _size_z = self.size_of(spec)
        if _normalize_orientation(orientation) in ("E", "W"):
            size_x, size_y = size_y, size_x
        x, y, z = _as_int(x), _as_int(y), _as_int(z)
        base_matter = self.base_matter_of(spec)
        for dy in range(size_y):
            for dx in range(size_x):
                if not self._cell_supported(base_matter, x + dx, y + dy, z, terrain_height_at):
                    return False
        return True

    def _cell_supported(self, base_matter, x, y, z, terrain_height_at):
        if base_matter == "ground":
            return self._on_ground(x, y, z, terrain_height_at)
        if base_matter == "stackable":
            return self._on_stackable(x, y, z)
        # "ground_or_stackable" / "any" / "air" -- v1 simplification, see the
        # module docstring's Support convention section.
        return self._on_ground(x, y, z, terrain_height_at) or self._on_stackable(x, y, z)

    def _on_ground(self, x, y, z, terrain_height_at):
        if terrain_height_at is None:
            return False
        try:
            surface = terrain_height_at(x, y)
        except Exception:
            return False
        return surface is not None and _as_int(surface) == z

    def _on_stackable(self, x, y, z):
        below = (x, y, z - 1)
        if self.cells.get(below) not in _STACKABLE_SUPPORT_OWNERS:
            return False
        spec_below = self._cell_spec.get(below)
        return spec_below is not None and self.is_stackable(spec_below)

    def fits(self, spec, x, y, z, orientation="N", terrain_height_at=None):
        """`is_free` AND `supported` for `spec`'s full footprint at (x, y, z)."""
        cells = self.footprint_cells(spec, x, y, z, orientation)
        return self.is_free(cells) and self.supported(
            spec, x, y, z, orientation, terrain_height_at
        )

    # -- reconciliation ----------------------------------------------------

    def reconcile_from_state(self, state):
        """Rebuild the state-derived BUILT layer from `state.buildings.list`:
        every "finished" or "site" building's full footprint becomes owner=BUILT
        ("site" == an active construction site -- it still occupies its
        footprint even though incomplete; any other/missing status, e.g. a
        demolished or ghost entry, is ignored). Cells THIS method wrote on a
        prior call that are no longer justified are cleared; cells it never
        wrote (an agent's "reserved" future slot, a "path"/"platform", or a cell
        only `reconcile_from_map` marked) are left untouched -- so persisted
        plan state survives repeated reconciliation. Returns the fresh built-cell
        set."""
        fresh = {}
        for building in _buildings_from_state(state):
            if not isinstance(building, dict):
                continue
            status = str(building.get("status", "finished") or "finished").lower()
            if status not in _RECONCILABLE_STATUSES:
                continue
            spec = building.get("spec") or building.get("spec_id")
            x, y, z = building.get("x"), building.get("y"), building.get("z")
            if not spec or x is None or y is None or z is None:
                continue
            orientation = _normalize_orientation(building.get("orientation"))
            for cell in self.footprint_cells(spec, x, y, z, orientation):
                fresh[cell] = spec

        for cell in self._state_built - fresh.keys():
            if self.cells.get(cell) == BUILT:
                del self.cells[cell]
            self._cell_spec.pop(cell, None)

        for cell, spec in fresh.items():
            self.cells[cell] = BUILT
            self._cell_spec[cell] = spec

        self._state_built = set(fresh.keys())
        return self._state_built

    def reconcile_from_map(self, map_data):
        """Coarse BUILT fallback from `/map`'s single-Z `occupied` array (1x1
        granularity, no spec/orientation info) at each occupied tile's terrain
        surface height. A cell already claimed by anything else (a precise
        `reconcile_from_state` footprint, an agent "reserved" slot, a "path"/
        "platform") is left alone -- `/map` occupancy is a fallback, never an
        override; prefer calling `reconcile_from_state` too/first each cycle.
        Terrain itself is NOT written into `cells` (stays sparse); ground support
        is derived on demand via `terrain_height_lookup`/`terrain_height_at`
        instead. Returns the fresh map-derived built-cell set."""
        if not isinstance(map_data, dict):
            map_data = {}
        width = _as_int(map_data.get("width"), 0)
        height = _as_int(map_data.get("height"), 0)
        origin = map_data.get("origin") or {}
        origin_x = _as_int(origin.get("x", map_data.get("origin_x", 0)), 0)
        origin_y = _as_int(origin.get("z", origin.get("y", map_data.get("origin_y", 0))), 0)
        terrain = map_data.get("terrain_height") or map_data.get("terrain") or []
        occupied = map_data.get("occupied") or []

        fresh = set()
        if width > 0 and height > 0:
            total = width * height
            for index in range(min(total, len(occupied))):
                if not occupied[index]:
                    continue
                col, row = index % width, index // width
                z = _as_int(terrain[index], 0) if index < len(terrain) else 0
                cell = (origin_x + col, origin_y + row, z)
                if cell in self.cells and cell not in self._map_built:
                    continue  # a more precise layer already claims this cell
                fresh.add(cell)

        for cell in self._map_built - fresh:
            if self.cells.get(cell) == BUILT:
                del self.cells[cell]
                self._cell_spec.pop(cell, None)

        for cell in fresh:
            self.cells[cell] = BUILT

        self._map_built = fresh
        return self._map_built


# ---------------------------------------------------------------------------
# LP2 -- per-category zone assignment
# ---------------------------------------------------------------------------
#
# `Zones` partitions the reachable LAND around the District Center into
# REGIONS reserved per building CATEGORY: the "memory of where you want
# what" from the design doc. `assign_zones(map_data, state=None)` computes
# the partition ONCE from a `/map` snapshot (+ `/state` for the DC position
# if `/map` doesn't carry one); hold the returned `Zones` across cycles like
# `Reservation` and call `.reconcile(map_data, state=None)` to refresh
# availability -- it never re-scores or re-partitions.
#
# Scoring heuristics per category (kept pragmatic -- see the LP2 report for
# the rationale):
#   water              -- land orthogonally adjacent to clean, non-badwater-
#                         reachable water (`spatial.deep_clean_water_edges`):
#                         a pump's placement condition.
#   food, forestry     -- moist, dry, unoccupied land
#                         (`spatial.plantable_mask`). Both categories share
#                         the same tile pool: farms and foresters both want
#                         moist soil, and nothing else here competes for it.
#   power              -- the SAME water-edge tiles as `water` (river
#                         frontage for water wheels) UNION "locally high"
#                         tiles: land strictly above the average height of
#                         its orthogonal neighbors (a local rise, for wind
#                         turbines). The overlap with `water` is intentional
#                         -- both a pump and a wheel want river frontage;
#                         arbitrating a shared tile is LP3's job, not LP2's.
#   housing, storage,  -- FLAT (uniform-height small neighborhood),
#   science,              reachable, dry, unclaimed land (i.e. land not
#   monuments,            already in the water/food/forestry/power pools
#   industry              above), ringed out from the District Center by BFS
#                         distance and sliced into contiguous, equal-sized
#                         chunks in that order -- housing nearest the DC,
#                         industry farthest, storage keeping its traditional
#                         spot between consumers and producers. A coarse but
#                         testable stand-in for real contiguous-district
#                         partitioning; `spatial.label_regions`/
#                         `voronoi_districts` are natural upgrades if the
#                         quantile-ring split proves too coarse in practice.
#   general            -- fallback: reachable land claimed by none of the
#                         above (non-flat leftover land, or any land beyond
#                         what the ring categories consumed), AND the target
#                         `zone_for`/`zone_for_spec` resolve to for any
#                         category this module doesn't specialize (e.g. a
#                         "decoration"/"logic"/"paths"/"district" building,
#                         or an unrecognized spec).
#
# A tile with `water_depth > 0` is NEVER itself a region member -- every
# region is land a building sits ON, not the water tile itself (a pump sits
# beside the river, not in it).

GENERAL_ZONE = "general"
_LAND_RING_ORDER = ("housing", "storage", "science", "monuments", "industry")
_SPECIALIZED_CATEGORIES = ("water", "food", "forestry", "power")
_ALL_ZONE_CATEGORIES = _LAND_RING_ORDER + _SPECIALIZED_CATEGORIES + (GENERAL_ZONE,)
_ORTHOGONAL = ((0, -1), (1, 0), (0, 1), (-1, 0))

_CATEGORY_CACHE = None


def load_building_categories(path=None):
    """`{bare_spec: category}` from `agent/data/buildings.json` (faction
    suffix stripped, mirroring `economy.py::_building_by_spec`). Defensive
    like `load_stacking_data`: a missing/unreadable/malformed file yields
    `{}`, so `category_of_spec` degrades to the general fallback rather than
    raising. Cached in-process for the default path."""
    global _CATEGORY_CACHE
    if path is None and _CATEGORY_CACHE is not None:
        return _CATEGORY_CACHE
    target = path or _BUILDINGS_PATH
    try:
        with open(target, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    buildings = data.get("buildings") if isinstance(data, dict) else None
    out = {}
    for building in buildings if isinstance(buildings, list) else []:
        if not isinstance(building, dict):
            continue
        spec = str(building.get("id") or "").split(".")[0]
        category = building.get("category")
        if spec and isinstance(category, str):
            out[spec] = category
    if path is None:
        _CATEGORY_CACHE = out
    return out


def category_of_spec(spec, categories=None):
    """`spec`'s building category (faction suffix stripped either way), or
    `None` if `spec` is unknown/uncategorized."""
    categories = categories if categories is not None else load_building_categories()
    return categories.get(str(spec or "").split(".")[0])


def _map_grid(map_data):
    """Normalize a `/map` payload into the plain row-major arrays LP2 scores
    against (mirrors `terrain_height_lookup`'s origin/width/height parsing;
    kept separate from `spatial`'s private `_arrays` so this module doesn't
    reach across another module's underscore boundary)."""
    if not isinstance(map_data, dict):
        map_data = {}
    width = _as_int(map_data.get("width"), 0)
    height = _as_int(map_data.get("height"), 0)
    origin = map_data.get("origin") or {}
    origin_x = _as_int(origin.get("x", map_data.get("origin_x", 0)), 0)
    origin_y = _as_int(origin.get("z", origin.get("y", map_data.get("origin_y", 0))), 0)

    def arr(*keys):
        for key in keys:
            value = map_data.get(key)
            if isinstance(value, list):
                return value
        return []

    return {
        "width": width,
        "height": height,
        "origin_x": origin_x,
        "origin_y": origin_y,
        "terrain": arr("terrain_height", "terrain"),
        "water": arr("water_depth", "water"),
        "occupied": arr("occupied"),
        "reachable": arr("reachable"),
        "on_road": arr("on_road"),
        "total": max(0, width * height),
    }


def _num_at(values, index, default=0.0):
    try:
        return float(values[index])
    except (IndexError, TypeError, ValueError):
        return default


def _bool_at(values, index, default=False):
    try:
        return bool(values[index])
    except (IndexError, TypeError):
        return default


def _and_masks(a, b):
    return [x and y for x, y in zip(a, b)]


def _or_masks(a, b):
    return [x or y for x, y in zip(a, b)]


def _mask_to_xy(mask, grid):
    """Bridge `(x, y)` for every truthy entry of a row-major `mask`."""
    width, origin_x, origin_y = grid["width"], grid["origin_x"], grid["origin_y"]
    return {
        (origin_x + index % width, origin_y + index // width)
        for index, value in enumerate(mask)
        if value
    }


def _reachable_land_mask(grid):
    """Dry, reachable, unoccupied tiles -- the substrate every zone draws
    from. `reachable` defaults to passable when the array is missing/short
    (mirrors `spatial.distance_field`'s `passable` default); `occupied`
    defaults to free. A tile with `water_depth > 0` is never land."""
    reachable, water, occupied = grid["reachable"], grid["water"], grid["occupied"]
    return [
        _bool_at(reachable, i, True)
        and _num_at(water, i, 0.0) <= 0
        and not _bool_at(occupied, i, False)
        for i in range(grid["total"])
    ]


def _flat_mask(grid):
    """True where a tile's terrain height matches every in-bounds orthogonal
    neighbor's height: a small flat neighborhood, per the design doc."""
    width, height, terrain = grid["width"], grid["height"], grid["terrain"]
    result = [False] * grid["total"]
    for row in range(height):
        for col in range(width):
            index = row * width + col
            own = _num_at(terrain, index)
            flat = True
            for dcol, drow in _ORTHOGONAL:
                ocol, orow = col + dcol, row + drow
                if not (0 <= ocol < width and 0 <= orow < height):
                    continue
                if _num_at(terrain, orow * width + ocol) != own:
                    flat = False
                    break
            result[index] = flat
    return result


def _locally_high_mask(grid):
    """True where a tile sits STRICTLY above the average height of its
    in-bounds orthogonal neighbors: a local rise, for turbine siting. A tile
    with no in-bounds neighbor is never "high" (nothing to compare against);
    a perfectly flat map has none either (every tile equals its neighbors'
    average), so this never swallows ordinary flat land."""
    width, height, terrain = grid["width"], grid["height"], grid["terrain"]
    result = [False] * grid["total"]
    for row in range(height):
        for col in range(width):
            index = row * width + col
            own = _num_at(terrain, index)
            neighbors = []
            for dcol, drow in _ORTHOGONAL:
                ocol, orow = col + dcol, row + drow
                if 0 <= ocol < width and 0 <= orow < height:
                    neighbors.append(_num_at(terrain, orow * width + ocol))
            if neighbors:
                result[index] = own > (sum(neighbors) / len(neighbors))
    return result


def _district_center_xy(map_data, state, grid):
    """Bridge `(x, y)` of the District Center: `state.district_center` first,
    then `map_data.district_center`, else the map's own center (mirrors
    `planner._district_center`/`play._district_center_xy`)."""
    dc = {}
    if isinstance(state, dict):
        dc = state.get("district_center") or {}
    if not dc and isinstance(map_data, dict):
        dc = map_data.get("district_center") or {}
    cx = _as_int(dc.get("x"), grid["origin_x"] + grid["width"] // 2)
    cy = _as_int(dc.get("y", dc.get("z")), grid["origin_y"] + grid["height"] // 2)
    return cx, cy


def _distance_at(distances, grid, xy):
    col, row = xy[0] - grid["origin_x"], xy[1] - grid["origin_y"]
    index = row * grid["width"] + col
    return distances[index] if 0 <= index < len(distances) else -1


def _built_xy(map_data, state):
    """Bare `{(x, y)}` of every currently-BUILT cell, per a throwaway
    `Reservation` seeded from `/map` (and `/state`, if given) -- reused by
    `Zones.reconcile` instead of re-parsing `occupied[]` here."""
    reservation = Reservation()
    reservation.reconcile_from_map(map_data)
    if state is not None:
        reservation.reconcile_from_state(state)
    return {
        (x, y) for (x, y, _z), owner in reservation.cells.items() if owner == BUILT
    }


class Zones:
    """Per-category tile regions: LP2's persisted "memory of where you want
    what". Build with `assign_zones`; consult with `.zone_for`/
    `.zone_for_spec`; call `.reconcile` each cycle to drop tiles the colony
    has since built on. The partition -- which category owns which tile --
    never changes after construction; only per-tile availability does."""

    def __init__(self, regions, categories=None):
        self.regions = {
            category: set(cells) for category, cells in (regions or {}).items()
        }
        self._categories = (
            categories if categories is not None else load_building_categories()
        )

    def zone_for(self, category):
        """Region for `category`. An unrecognized/falsy category, or one
        this module doesn't specialize (e.g. "decoration"/"logic"/"paths"/
        "district"), falls back to the general flat-land region."""
        if category and category in self.regions:
            return self.regions[category]
        return self.regions.get(GENERAL_ZONE, set())

    def zone_for_spec(self, spec):
        """`zone_for` resolved through `spec`'s building category."""
        return self.zone_for(category_of_spec(spec, self._categories))

    def reconcile(self, map_data, state=None):
        """Refresh free-space in place: drop any tile now BUILT (per a fresh
        `/map` snapshot, and `/state` buildings if given) from every region.
        Does NOT re-score tiles or move them between categories -- the zone
        ASSIGNMENT is stable; only per-tile availability changes as the
        colony grows. Returns `self.regions`."""
        built = _built_xy(map_data, state)
        if built:
            for category in self.regions:
                self.regions[category] -= built
        return self.regions


def assign_zones(map_data, state=None, categories=None):
    """Partition the reachable land around the District Center into
    per-category regions (LP2). Computed ONCE from a `/map` (+ `/state` for
    the DC position) snapshot -- hold the returned `Zones` across cycles and
    call `.reconcile(...)` on it to refresh availability instead of calling
    this again (re-running this would reshuffle which tiles belong to which
    category as the colony's own buildings change the map's occupied[])."""
    grid = _map_grid(map_data)
    if grid["total"] <= 0:
        return Zones({category: set() for category in _ALL_ZONE_CATEGORIES}, categories)

    reachable_land = _reachable_land_mask(grid)
    water_edge = _and_masks(spatial.deep_clean_water_edges(map_data), reachable_land)
    moist = _and_masks(spatial.plantable_mask(map_data), reachable_land)
    locally_high = _and_masks(_locally_high_mask(grid), reachable_land)
    power = _or_masks(water_edge, locally_high)

    water_xy = _mask_to_xy(water_edge, grid)
    food_xy = _mask_to_xy(moist, grid)
    power_xy = _mask_to_xy(power, grid)
    reachable_land_xy = _mask_to_xy(reachable_land, grid)

    claimed = water_xy | food_xy | power_xy
    land_xy = reachable_land_xy - claimed
    flat_xy = _mask_to_xy(_flat_mask(grid), grid)
    flat_land_xy = land_xy & flat_xy
    ring_source = flat_land_xy if flat_land_xy else land_xy
    leftover_xy = land_xy - ring_source

    dc_x, dc_y = _district_center_xy(map_data, state, grid)
    distances = spatial.distance_field(
        [(dc_x - grid["origin_x"], dc_y - grid["origin_y"])],
        grid["width"], grid["height"],
        passable=reachable_land,
    )

    ranked = sorted(
        ring_source, key=lambda xy: (_distance_at(distances, grid, xy), xy[1], xy[0])
    )
    reached = [xy for xy in ranked if _distance_at(distances, grid, xy) >= 0]
    unreached_xy = set(ranked) - set(reached)

    regions = {
        "water": water_xy,
        "food": set(food_xy),
        "forestry": set(food_xy),
        "power": power_xy,
    }
    ring_count = len(_LAND_RING_ORDER)
    chunk = math.ceil(len(reached) / ring_count) if reached else 0
    for i, category in enumerate(_LAND_RING_ORDER):
        regions[category] = set(reached[i * chunk:(i + 1) * chunk]) if chunk else set()
    regions[GENERAL_ZONE] = leftover_xy | unreached_xy

    return Zones(regions, categories)


# ---------------------------------------------------------------------------
# LP3 -- coordinated batch placement + boxing/connectivity check
# ---------------------------------------------------------------------------
#
# `plan_placements` is the design doc's Algorithm steps 2-3: given the next
# few WANTED specs (in priority order), place them one at a time against a
# SHARED `Reservation`/`Zones` pair so each later spec already sees the
# earlier ones' footprints -- the "coordination" that stops a batch from
# claiming the same tile twice or sealing a neighbor in. Verticality (LP4)
# is out of scope here: every placement sits at the ground surface
# (`terrain_height_lookup`); a spec with no valid ground tile is SKIPPED,
# not silently escalated to a platform.
#
# The BOXING CHECK is the crux (step 4 of the design doc): a tile that fits
# and has a doorstep can STILL be wrong to reserve if doing so would sever
# the walkable network -- cutting an existing building off from the
# District Center, or filling in the last scrap of buildable frontier. Each
# candidate is therefore tentatively reserved, flood-filled from the DC over
# the WALKABLE graph (`_walkable_flood`), and un-reserved again; only the
# tile that is ultimately kept stays reserved.
#
# Bounded, not exhaustive: only the `_LP3_MAX_CANDIDATE_TILES` tiles closest
# to the DC (Manhattan distance) are ever flood-fill-checked per spec. A
# real zone can hold hundreds of tiles and the flood-fill is O(map size), so
# checking every candidate in a big zone would be O(zone x map) per spec --
# fine for the small synthetic maps in the tests, not for a real colony. See
# the LP3 report for the tradeoff this bound makes.

_LP3_MAX_CANDIDATE_TILES = 24
_LP3_ORIENTATIONS = ("N", "E", "S", "W")


def _spec_of(entry):
    """A bare spec id string from either a plain spec string or a goal-like
    dict carrying a `"spec"` key (the shape `planner.analyze`/
    `controller.build_safe_ready_frontier` goals use). Does NOT resolve a
    bare goal_id through `planner.GOAL_SPECS` -- `layout.py` stays
    independent of `planner.py` like the rest of this module; a caller that
    only has a goal_id should resolve it to a spec first. Returns `None` for
    anything without a usable spec."""
    spec = entry.get("spec") if isinstance(entry, dict) else entry
    return str(spec) if spec else None


def _note_skip(spec, reason):
    """Best-effort stderr breadcrumb for a spec `plan_placements` could not
    place. The function's return value is a plain `list[dict]` of
    successful placements only -- this is the only trace of a skip a caller
    not otherwise tracking goal state will see."""
    print("layout.plan_placements: skipping %r -- %s" % (spec, reason), file=sys.stderr)


def _manhattan(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _walkable_predicate(reservation, grid, terrain_at):
    """Build an `(x, y) -> bool` WALKABLE test: dry land (water_depth <= 0)
    whose terrain-surface cell isn't claimed by a BUILT/RESERVED/PLATFORM
    footprint, OR any `on_road` tile (a road is always walkable). Water is
    an absolute blocker -- checked before `on_road` -- and out-of-bounds is
    never walkable. Shared by `_walkable_flood`, the per-candidate access-
    tile check, and the boxing check's frontier count, so all three agree on
    what "walkable" means."""

    def walkable(x, y):
        col, row = x - grid["origin_x"], y - grid["origin_y"]
        if not (0 <= col < grid["width"] and 0 <= row < grid["height"]):
            return False
        index = row * grid["width"] + col
        if _num_at(grid["water"], index, 0.0) > 0:
            return False
        if _bool_at(grid["on_road"], index, False):
            return True
        z = terrain_at(x, y)
        if z is None:
            return False
        return reservation.cells.get((x, y, z)) not in (BUILT, RESERVED, PLATFORM)

    return walkable


def _walkable_flood(reservation, map_data, start):
    """BFS over WALKABLE tiles (free land + `on_road`; blocked by BUILT/
    RESERVED/PLATFORM footprints and water) from bridge `(x, y)` `start`,
    4-neighbour. `start` is always added to the returned set -- and its
    walkable neighbours are always explored -- even if `start` is itself
    blocked (e.g. the District Center's own footprint); mirrors
    `spatial.distance_field`'s "seed the source even if impassable"
    convention."""
    grid = _map_grid(map_data)
    if grid["total"] <= 0:
        return set()
    terrain_at = terrain_height_lookup(map_data)
    walkable = _walkable_predicate(reservation, grid, terrain_at)

    start = (_as_int(start[0]), _as_int(start[1]))
    visited = {start}
    queue = deque([start])
    while queue:
        x, y = queue.popleft()
        for dx, dy in _ORTHOGONAL:
            nxt = (x + dx, y + dy)
            if nxt in visited or not walkable(*nxt):
                continue
            visited.add(nxt)
            queue.append(nxt)
    return visited


def _frontier_size(reservation, grid, terrain_at, reached):
    """Count of `reached` tiles that are still free (unclaimed) land -- the
    open frontier a further placement could still use. Doubles as the
    boxing check's "didn't shrink to nothing" gate and the tie-break when
    ranking surviving candidates."""
    count = 0
    for (x, y) in reached:
        col, row = x - grid["origin_x"], y - grid["origin_y"]
        if not (0 <= col < grid["width"] and 0 <= row < grid["height"]):
            continue
        index = row * grid["width"] + col
        if _num_at(grid["water"], index, 0.0) > 0:
            continue
        z = terrain_at(x, y)
        if z is None:
            continue
        if reservation.cells.get((x, y, z)) in (BUILT, RESERVED, PLATFORM):
            continue
        count += 1
    return count


def _access_tiles(reservation, building):
    """Bridge `(x, y)` tiles orthogonally adjacent to `building`'s full
    footprint -- its candidate doorstep ring. `building` is a
    `{"spec","x","y","z","orientation"}` dict: a `/state buildings.list`
    entry (already BUILT) or an LP3 in-batch placement (RESERVED earlier
    this same call) -- both shapes work. Malformed/incomplete input yields
    `[]` rather than raising."""
    if not isinstance(building, dict):
        return []
    spec = building.get("spec") or building.get("spec_id")
    x, y, z = building.get("x"), building.get("y"), building.get("z")
    if not spec or x is None or y is None or z is None:
        return []
    orientation = _normalize_orientation(building.get("orientation"))
    footprint_xy = {
        (fx, fy) for fx, fy, _fz in reservation.footprint_cells(spec, x, y, z, orientation)
    }
    access = set()
    for (fx, fy) in footprint_xy:
        for dx, dy in _ORTHOGONAL:
            neighbor = (fx + dx, fy + dy)
            if neighbor not in footprint_xy:
                access.add(neighbor)
    return list(access)


def _has_access_tile(reservation, grid, terrain_at, spec, x, y, z, orientation):
    """True if `spec`'s footprint at (x, y, z, orientation) has at least one
    orthogonally-adjacent WALKABLE tile -- the building's doorstep (design
    doc step 3). This approximates the entrance as "some adjacent free
    tile"; the exact orientation-correct entrance is LP6's job."""
    walkable = _walkable_predicate(reservation, grid, terrain_at)
    footprint_xy = {
        (fx, fy) for fx, fy, _fz in reservation.footprint_cells(spec, x, y, z, orientation)
    }
    for (fx, fy) in footprint_xy:
        for dx, dy in _ORTHOGONAL:
            neighbor = (fx + dx, fy + dy)
            if neighbor not in footprint_xy and walkable(*neighbor):
                return True
    return False


def _existing_buildings_for_boxing(state):
    """`/state buildings.list` entries the boxing check must keep reachable
    -- the same "finished"/"site" status filter as `Reservation.
    reconcile_from_state` (a demolished/ghost entry has no doorstep worth
    protecting)."""
    out = []
    for building in _buildings_from_state(state):
        if not isinstance(building, dict):
            continue
        status = str(building.get("status", "finished") or "finished").lower()
        if status in _RECONCILABLE_STATUSES:
            out.append(building)
    return out


def _boxing_check(reservation, map_data, grid, terrain_at, dc_xy, protected_buildings):
    """After a candidate footprint has been TENTATIVELY reserved: flood the
    walkable network from the District Center and require (a) every
    building in `protected_buildings` still has a reached access tile, and
    (b) the reached free frontier hasn't shrunk to nothing. Returns
    `(ok, frontier_size)`. Never mutates `reservation` -- the caller is
    responsible for undoing its own tentative reservation either way."""
    reached = _walkable_flood(reservation, map_data, dc_xy)
    frontier = _frontier_size(reservation, grid, terrain_at, reached)
    if frontier <= 0:
        return False, frontier
    for building in protected_buildings:
        access = _access_tiles(reservation, building)
        if access and not (reached & set(access)):
            return False, frontier
    return True, frontier


def _best_placement(spec, candidates, reservation, map_data, grid, terrain_at, dc_xy,
                     protected_buildings):
    """Rank `candidates` by Manhattan distance to `dc_xy` (row-major
    tie-break for determinism), keep only the nearest
    `_LP3_MAX_CANDIDATE_TILES`, and return the best surviving `(x, y, z,
    orientation)` -- fits, has an access tile, and passes the boxing check
    -- or `None` if nothing in the bounded pool survives all three."""
    ranked = sorted(candidates, key=lambda xy: (_manhattan(xy, dc_xy), xy[1], xy[0]))
    ranked = ranked[:_LP3_MAX_CANDIDATE_TILES]

    best = None
    best_key = None
    for (x, y) in ranked:
        z = terrain_at(x, y)
        if z is None:
            continue
        for orientation in _LP3_ORIENTATIONS:
            if not reservation.fits(spec, x, y, z, orientation, terrain_at):
                continue
            if not _has_access_tile(reservation, grid, terrain_at, spec, x, y, z, orientation):
                continue
            reservation.reserve(spec, x, y, z, orientation, owner=RESERVED)
            ok, frontier = _boxing_check(
                reservation, map_data, grid, terrain_at, dc_xy, protected_buildings
            )
            reservation.free(spec, x, y, z, orientation)
            if not ok:
                continue
            key = (_manhattan((x, y), dc_xy), -frontier, y, x)
            if best_key is None or key < best_key:
                best_key, best = key, (x, y, z, orientation)
    return best


def plan_placements(specs, map_data, reservation, zones, state=None):
    """LP3 -- coordinated batch placement (`docs/kb/layout-planner-design.md`
    Algorithm steps 2-3). Places `specs` (an ordered list of building spec
    ids, or goal-like dicts carrying a `"spec"` key) ONE AT A TIME, IN
    ORDER, against the SAME `reservation`/`zones` -- so spec #2 already sees
    spec #1's tentative reservation from this same call and can't collide
    with or box it in. Ground-only: every placement sits at `z =
    terrain_height(x, y)`; verticality is LP4.

    For each spec, in order:
      1. Candidate tiles = `zones.zone_for_spec(spec)`, falling back to any
         reachable land (`_reachable_land_mask`) if the zone is empty.
      2. `reservation.fits(...)` -- free footprint + supported, tried at
         each of the 4 orientations -- so it can't overlap anything built/
         reserved, INCLUDING earlier placements from this same batch.
      3. An access tile: some orthogonally-adjacent WALKABLE tile (the
         building's doorstep; `_has_access_tile`).
      4. The BOXING CHECK (`_boxing_check`): tentatively reserve the
         footprint and flood-fill the walkable network from the District
         Center; reject the tile if that flood no longer reaches every
         existing/already-placed building's access tile, or has no free
         frontier left. The tentative reservation is undone regardless.
      5. Keep the surviving tile closest to the DC (frontier size breaks
         ties), and PERMANENTLY reserve it (`owner=RESERVED`) before moving
         on to the next spec -- so it also becomes protected for the boxing
         check of every spec still to come.

    A spec that finds no valid tile is SKIPPED, never raised (see
    `_note_skip`) -- it is simply absent from the returned list.

    Returns `[{"spec","x","y","z","orientation"}, ...]`, at most one entry
    per input spec, in `specs` order.
    """
    grid = _map_grid(map_data)
    if grid["total"] <= 0 or not specs:
        return []

    terrain_at = terrain_height_lookup(map_data)
    dc_xy = _district_center_xy(map_data, state, grid)
    protected_buildings = _existing_buildings_for_boxing(state)
    reachable_land_xy = None  # computed lazily -- only if a zone ever comes up empty

    placements = []
    for entry in specs:
        spec = _spec_of(entry)
        if not spec:
            _note_skip(entry, "no spec")
            continue

        candidates = zones.zone_for_spec(spec)
        if not candidates:
            if reachable_land_xy is None:
                reachable_land_xy = _mask_to_xy(_reachable_land_mask(grid), grid)
            candidates = reachable_land_xy

        winner = _best_placement(
            spec, candidates, reservation, map_data, grid, terrain_at, dc_xy,
            protected_buildings,
        )
        if winner is None:
            # LP4: no GROUND tile worked in this zone -> try to build UP.
            vertical = plan_vertical_placement(spec, map_data, reservation, zones, state)
            if vertical:
                placements.extend(vertical)
                protected_buildings = protected_buildings + vertical
                continue
            _note_skip(spec, "no ground tile fit/access/boxing, and no vertical option")
            continue

        x, y, z, orientation = winner
        reservation.reserve(spec, x, y, z, orientation, owner=RESERVED)
        placement = {"spec": spec, "x": x, "y": y, "z": z, "orientation": orientation,
                     "role": "ground"}
        placements.append(placement)
        protected_buildings = protected_buildings + [placement]

    return placements


# ---------------------------------------------------------------------------
# LP4 -- verticality fallback (platform deck + stairs + stacking)
# ---------------------------------------------------------------------------

_PLATFORM_SPEC = "Platform"
_STAIRS_SPEC = "SpiralStairs"


def _stackable_top_cells(reservation):
    """Occupied cells that are a stackable building/platform surface -- a
    building can sit at (x, y, z+1) supported by them. Returns [(x, y, z)]."""
    out = []
    for cell, owner in reservation.cells.items():
        if owner not in _STACKABLE_SUPPORT_OWNERS:
            continue
        spec_here = reservation._cell_spec.get(cell)
        if spec_here is not None and reservation.is_stackable(spec_here):
            out.append(cell)
    return out


def plan_vertical_placement(spec, map_data, reservation, zones, state=None):
    """LP4 -- when `spec` cannot go on the GROUND in its zone (LP3 found no
    tile), build UP. Returns an ORDERED sequence of placements
    `[{spec,x,y,z,orientation,role}]` -- **support first** (the play loop must
    build the platform/stairs before the stacked building; the game stalls a
    stacked construction site until its support below is finished) -- or `[]`
    if `spec` can't be stacked (its base needs bare ground) or no room exists.
    Placements in the returned sequence are already RESERVED in `reservation`.

    Strategies, in order:
      1. DIRECT STACK -- place `spec` one Z above an existing stackable
         building/platform whose whole top footprint is free and supports it
         (housing-on-housing, a building on an existing platform deck).
      2. PLATFORM DECK -- reserve a `Platform` under each of `spec`'s base cells
         on free ground in its zone + a `SpiralStairs` access column adjacent,
         then `spec` on the platform tops at `z = surface + 1`.
    `fits()` enforces the real support rule (a "ground"-only base_matter spec
    simply never fits at z+1, so it correctly returns [] here)."""
    grid = _map_grid(map_data)
    if grid["total"] <= 0 or not spec:
        return []
    terrain_at = terrain_height_lookup(map_data)
    dc_xy = _district_center_xy(map_data, state, grid)
    zone = zones.zone_for_spec(spec) if zones is not None else set()

    def _dc_key(xy):
        return abs(xy[0] - dc_xy[0]) + abs(xy[1] - dc_xy[1])

    orientations = ("N", "E", "S", "W")

    # 1. DIRECT STACK on an existing stackable top (nearest the DC first).
    for (sx, sy, sz) in sorted(_stackable_top_cells(reservation),
                               key=lambda c: _dc_key((c[0], c[1]))):
        if zone and (sx, sy) not in zone:
            continue  # keep the stack inside the spec's zone when we have one
        for orient in orientations:
            cz = sz + 1
            if reservation.fits(spec, sx, sy, cz, orient, terrain_at):
                reservation.reserve(spec, sx, sy, cz, orient, owner=RESERVED)
                return [{"spec": spec, "x": sx, "y": sy, "z": cz,
                         "orientation": orient, "role": "stacked"}]

    # 2. PLATFORM DECK on free, flat ground in the zone.
    sx_n, sy_n, _sz_n = reservation.size_of(spec)
    tiles = sorted(zone, key=_dc_key) if zone else sorted(
        _mask_to_xy(_reachable_land_mask(grid), grid), key=_dc_key)
    for (px, py) in tiles:
        surface = terrain_at(px, py)
        if surface is None:
            continue
        surface = _as_int(surface)
        deck = [(px + dx, py + dy) for dy in range(sy_n) for dx in range(sx_n)]
        # every deck cell must be free, flat ground at the same surface height.
        flat_free = all(
            terrain_at(cx, cy) is not None
            and _as_int(terrain_at(cx, cy)) == surface
            and reservation.is_free([(cx, cy, surface)])
            for (cx, cy) in deck
        )
        if not flat_free:
            continue
        # lay the platform deck, then verify the target fits on top.
        for (cx, cy) in deck:
            reservation.reserve(_PLATFORM_SPEC, cx, cy, surface, "N", owner=PLATFORM)
        top_z = surface + 1
        if not reservation.fits(spec, px, py, top_z, "N", terrain_at):
            for (cx, cy) in deck:
                reservation.free(_PLATFORM_SPEC, cx, cy, surface, "N")
            continue
        seq = [{"spec": _PLATFORM_SPEC, "x": cx, "y": cy, "z": surface,
                "orientation": "N", "role": "support"} for (cx, cy) in deck]
        # a stair access column on a free-ground tile orthogonally adjacent to the deck.
        for (ax, ay) in ((px - 1, py), (px + sx_n, py), (px, py - 1), (px, py + sy_n)):
            s = terrain_at(ax, ay)
            if s is not None and _as_int(s) == surface and reservation.is_free([(ax, ay, surface)]):
                reservation.reserve(_STAIRS_SPEC, ax, ay, surface, "N", owner=PATH)
                seq.append({"spec": _STAIRS_SPEC, "x": ax, "y": ay, "z": surface,
                            "orientation": "N", "role": "access"})
                break
        reservation.reserve(spec, px, py, top_z, "N", owner=RESERVED)
        seq.append({"spec": spec, "x": px, "y": py, "z": top_z,
                    "orientation": "N", "role": "stacked"})
        return seq

    return []


# ---------------------------------------------------------------------------
# inline tests
# ---------------------------------------------------------------------------

_TEST_STACKING = {
    "Big": {"size": {"x": 2, "y": 3, "z": 1}, "stackable": False,
            "can_stack_on": True, "base_matter": "ground"},
    "Tower": {"size": {"x": 1, "y": 1, "z": 2}, "stackable": False,
              "can_stack_on": True, "base_matter": "ground"},
    "Hut": {"size": {"x": 1, "y": 1, "z": 1}, "stackable": False,
            "can_stack_on": True, "base_matter": "ground"},
    "Lodge": {"size": {"x": 2, "y": 2, "z": 1}, "stackable": True,
              "can_stack_on": True, "base_matter": "ground_or_stackable"},
    "Platform": {"size": {"x": 1, "y": 1, "z": 1}, "stackable": True,
                 "can_stack_on": True, "base_matter": "ground_or_stackable"},
    "BigPlatform": {"size": {"x": 2, "y": 2, "z": 1}, "stackable": True,
                     "can_stack_on": True, "base_matter": "ground_or_stackable"},
    "Path": {"size": {"x": 1, "y": 1, "z": 1}, "stackable": False,
             "can_stack_on": True, "base_matter": "ground_or_stackable"},
    "Shed": {"size": {"x": 1, "y": 1, "z": 1}, "stackable": False,
             "can_stack_on": True, "base_matter": "ground"},
    "BigShed": {"size": {"x": 2, "y": 2, "z": 1}, "stackable": False,
                "can_stack_on": True, "base_matter": "ground"},
    "Rooftop": {"size": {"x": 1, "y": 1, "z": 1}, "stackable": False,
                "can_stack_on": True, "base_matter": "stackable"},
}


class FootprintCellsTests(unittest.TestCase):
    def setUp(self):
        self.res = Reservation(stacking=_TEST_STACKING)

    def test_2x3x1_north_occupies_six_cells(self):
        cells = self.res.footprint_cells("Big", 5, 5, 4, "N")
        self.assertEqual(
            set(cells),
            {(5, 5, 4), (6, 5, 4), (5, 6, 4), (6, 6, 4), (5, 7, 4), (6, 7, 4)},
        )
        self.assertEqual(len(cells), 6)

    def test_rotated_east_swaps_footprint_to_3x2(self):
        cells = self.res.footprint_cells("Big", 5, 5, 4, "E")
        self.assertEqual(
            set(cells),
            {(5, 5, 4), (6, 5, 4), (7, 5, 4), (5, 6, 4), (6, 6, 4), (7, 6, 4)},
        )
        self.assertEqual(len(cells), 6)

    def test_vertical_stack_occupies_two_z_levels(self):
        cells = self.res.footprint_cells("Tower", 2, 2, 4, "N")
        self.assertEqual(set(cells), {(2, 2, 4), (2, 2, 5)})

    def test_unknown_spec_defaults_to_1x1x1(self):
        cells = self.res.footprint_cells("TotallyUnknownSpec", 0, 0, 0, "N")
        self.assertEqual(set(cells), {(0, 0, 0)})

    def test_south_and_west_orientation_conventions(self):
        # S keeps (sx, sy) like N; W swaps like E (see module docstring).
        self.assertEqual(
            set(self.res.footprint_cells("Big", 0, 0, 0, "S")),
            set(self.res.footprint_cells("Big", 0, 0, 0, "N")),
        )
        self.assertEqual(
            set(self.res.footprint_cells("Big", 0, 0, 0, "W")),
            set(self.res.footprint_cells("Big", 0, 0, 0, "E")),
        )

    def test_orientation_aliases_full_compass_words(self):
        self.assertEqual(
            self.res.footprint_cells("Big", 5, 5, 4, "East"),
            self.res.footprint_cells("Big", 5, 5, 4, "E"),
        )


class OverlapAndMultiZTests(unittest.TestCase):
    def setUp(self):
        self.res = Reservation(stacking=_TEST_STACKING)

    def test_overlapping_footprint_is_not_free(self):
        self.res.reserve("Big", 5, 5, 4, "N", owner=BUILT)
        overlapping = self.res.footprint_cells("Hut", 6, 6, 4, "N")
        self.assertFalse(self.res.is_free(overlapping))

    def test_disjoint_footprint_is_free(self):
        self.res.reserve("Big", 5, 5, 4, "N", owner=BUILT)
        disjoint = self.res.footprint_cells("Hut", 20, 20, 4, "N")
        self.assertTrue(self.res.is_free(disjoint))

    def test_different_z_does_not_conflict(self):
        self.res.reserve("Hut", 3, 3, 4, "N", owner=BUILT)
        self.assertFalse(self.res.is_free([(3, 3, 4)]))
        self.assertTrue(self.res.is_free([(3, 3, 5)]))

    def test_multi_z_building_conflicts_only_where_z_ranges_overlap(self):
        self.res.reserve("Tower", 8, 8, 4, "N", owner=BUILT)  # occupies z=4,5
        self.assertTrue(
            self.res.is_free(self.res.footprint_cells("Hut", 8, 8, 6, "N"))
        )
        self.assertFalse(
            self.res.is_free(self.res.footprint_cells("Hut", 8, 8, 5, "N"))
        )


class SupportedTests(unittest.TestCase):
    def setUp(self):
        self.res = Reservation(stacking=_TEST_STACKING)
        self.terrain_at = lambda x, y: 4

    def test_ground_building_fits_only_at_terrain_surface(self):
        self.assertTrue(self.res.supported("Hut", 1, 1, 4, "N", self.terrain_at))
        self.assertFalse(self.res.supported("Hut", 1, 1, 5, "N", self.terrain_at))

    def test_stackable_building_needs_stackable_support_below(self):
        # Lodge (2x2, ground_or_stackable) at z=5 (one above the surface):
        # nothing below yet -> can't stack.
        self.assertFalse(self.res.supported("Lodge", 2, 2, 5, "N", self.terrain_at))
        # A BigPlatform (2x2, stackable=True) fully below -> stacking allowed.
        self.res.reserve("BigPlatform", 2, 2, 4, "N", owner=PLATFORM)
        self.assertTrue(self.res.supported("Lodge", 2, 2, 5, "N", self.terrain_at))

    def test_partial_support_below_is_not_enough(self):
        # Only ONE of Lodge's 4 base cells has stackable support below (a bare
        # 1x1 Platform, not the full 2x2 footprint) -> every base cell must be
        # supported, so this must still fail.
        self.res.reserve("Platform", 2, 2, 4, "N", owner=PLATFORM)
        self.assertFalse(self.res.supported("Lodge", 2, 2, 5, "N", self.terrain_at))

    def test_non_stackable_support_below_does_not_count(self):
        self.res.reserve("BigShed", 6, 6, 4, "N", owner=BUILT)  # stackable == False
        self.assertFalse(self.res.supported("Lodge", 6, 6, 5, "N", self.terrain_at))

    def test_path_owner_does_not_count_as_stackable_support(self):
        self.res.reserve("BigPlatform", 9, 9, 4, "N", owner=PATH)  # wrong tag on purpose
        self.assertFalse(self.res.supported("Lodge", 9, 9, 5, "N", self.terrain_at))

    def test_ground_or_stackable_also_accepts_ground(self):
        self.assertTrue(self.res.supported("Lodge", 3, 3, 4, "N", self.terrain_at))

    def test_pure_stackable_base_matter_rejects_ground(self):
        self.assertFalse(self.res.supported("Rooftop", 4, 4, 4, "N", self.terrain_at))


class FitsTests(unittest.TestCase):
    def setUp(self):
        self.res = Reservation(stacking=_TEST_STACKING)
        self.terrain_at = lambda x, y: 4

    def test_fits_requires_free_and_supported(self):
        self.assertTrue(self.res.fits("Hut", 1, 1, 4, "N", self.terrain_at))
        self.res.reserve("Hut", 1, 1, 4, "N", owner=BUILT)
        self.assertFalse(self.res.fits("Hut", 1, 1, 4, "N", self.terrain_at))
        self.assertFalse(self.res.fits("Hut", 5, 5, 5, "N", self.terrain_at))


class ReserveFreeTests(unittest.TestCase):
    def setUp(self):
        self.res = Reservation(stacking=_TEST_STACKING)

    def test_reserve_then_free_round_trips(self):
        cells = self.res.reserve("Big", 0, 0, 4, "N", owner=RESERVED)
        self.assertTrue(all(self.res.cells.get(c) == RESERVED for c in cells))
        self.res.free("Big", 0, 0, 4, "N")
        self.assertTrue(self.res.is_free(cells))


class ReconcileFromStateTests(unittest.TestCase):
    def setUp(self):
        self.res = Reservation(stacking=_TEST_STACKING)

    @staticmethod
    def _state(buildings):
        return {"buildings": {"list": buildings}}

    def test_finished_and_site_buildings_become_built(self):
        state = self._state([
            {"spec": "Hut", "x": 1, "y": 1, "z": 4, "status": "finished"},
            {"spec": "Big", "x": 10, "y": 10, "z": 4, "orientation": "N", "status": "site"},
        ])
        self.res.reconcile_from_state(state)
        self.assertEqual(self.res.cells.get((1, 1, 4)), BUILT)
        for cell in self.res.footprint_cells("Big", 10, 10, 4, "N"):
            self.assertEqual(self.res.cells.get(cell), BUILT)

    def test_stale_building_cleared_on_next_reconcile(self):
        state_a = self._state([
            {"spec": "Hut", "x": 1, "y": 1, "z": 4, "status": "finished"},
            {"spec": "Hut", "x": 2, "y": 2, "z": 4, "status": "finished"},
        ])
        self.res.reconcile_from_state(state_a)
        self.assertEqual(self.res.cells.get((2, 2, 4)), BUILT)

        state_b = self._state([
            {"spec": "Hut", "x": 1, "y": 1, "z": 4, "status": "finished"},
        ])
        self.res.reconcile_from_state(state_b)
        self.assertEqual(self.res.cells.get((1, 1, 4)), BUILT)
        self.assertIsNone(self.res.cells.get((2, 2, 4)))

    def test_agent_reserved_slots_survive_reconcile(self):
        self.res.reserve("Hut", 5, 5, 4, "N", owner=RESERVED)
        state = self._state([
            {"spec": "Hut", "x": 1, "y": 1, "z": 4, "status": "finished"},
        ])
        self.res.reconcile_from_state(state)
        self.assertEqual(self.res.cells.get((5, 5, 4)), RESERVED)

    def test_ignores_non_finished_non_site_statuses(self):
        state = self._state([
            {"spec": "Hut", "x": 7, "y": 7, "z": 4, "status": "demolished"},
        ])
        self.res.reconcile_from_state(state)
        self.assertIsNone(self.res.cells.get((7, 7, 4)))

    def test_faction_suffixed_spec_resolves_via_bare_prefix(self):
        state = self._state([
            {"spec": "Big.Folktails", "x": 0, "y": 0, "z": 4, "orientation": "N",
             "status": "finished"},
        ])
        self.res.reconcile_from_state(state)
        for cell in self.res.footprint_cells("Big", 0, 0, 4, "N"):
            self.assertEqual(self.res.cells.get(cell), BUILT)


class ReconcileFromMapTests(unittest.TestCase):
    def setUp(self):
        self.res = Reservation(stacking=_TEST_STACKING)

    def test_occupied_tile_marked_built_at_terrain_height(self):
        map_data = {
            "origin": {"x": 0, "z": 0},
            "width": 2,
            "height": 2,
            "terrain_height": [4, 4, 4, 4],
            "occupied": [0, 1, 0, 0],
        }
        self.res.reconcile_from_map(map_data)
        self.assertEqual(self.res.cells.get((1, 0, 4)), BUILT)
        self.assertIsNone(self.res.cells.get((0, 0, 4)))

    def test_does_not_override_precise_state_reservation(self):
        self.res.reserve("Hut", 0, 0, 4, "N", owner=RESERVED)
        map_data = {
            "origin": {"x": 0, "z": 0},
            "width": 1,
            "height": 1,
            "terrain_height": [4],
            "occupied": [1],
        }
        self.res.reconcile_from_map(map_data)
        self.assertEqual(self.res.cells.get((0, 0, 4)), RESERVED)

    def test_stale_map_mark_cleared_when_no_longer_occupied(self):
        map_a = {
            "origin": {"x": 0, "z": 0}, "width": 1, "height": 1,
            "terrain_height": [4], "occupied": [1],
        }
        self.res.reconcile_from_map(map_a)
        self.assertEqual(self.res.cells.get((0, 0, 4)), BUILT)

        map_b = {
            "origin": {"x": 0, "z": 0}, "width": 1, "height": 1,
            "terrain_height": [4], "occupied": [0],
        }
        self.res.reconcile_from_map(map_b)
        self.assertIsNone(self.res.cells.get((0, 0, 4)))


class LoadStackingDataTests(unittest.TestCase):
    def test_real_data_file_loads_lodge(self):
        res = Reservation()  # default stacking = real building_stacking.json
        self.assertEqual(res.size_of("Lodge"), (2, 2, 1))
        self.assertEqual(res.base_matter_of("Lodge"), "ground_or_stackable")
        self.assertTrue(res.is_stackable("Lodge"))

    def test_missing_spec_defaults(self):
        res = Reservation(stacking={})
        self.assertEqual(res.size_of("NoSuchSpec"), (1, 1, 1))
        self.assertEqual(res.base_matter_of("NoSuchSpec"), "ground")
        self.assertFalse(res.is_stackable("NoSuchSpec"))


class TerrainHeightLookupTests(unittest.TestCase):
    def test_builds_callable_from_map_payload(self):
        map_data = {
            "origin": {"x": 10, "z": 20},
            "width": 2,
            "height": 2,
            "terrain_height": [4, 5, 6, 7],
        }
        lookup = terrain_height_lookup(map_data)
        self.assertEqual(lookup(10, 20), 4)
        self.assertEqual(lookup(11, 20), 5)
        self.assertEqual(lookup(10, 21), 6)
        self.assertIsNone(lookup(999, 999))


# ---------------------------------------------------------------------------
# LP2 inline tests
# ---------------------------------------------------------------------------

class LoadBuildingCategoriesTests(unittest.TestCase):
    def test_real_data_file_loads_known_categories(self):
        categories = load_building_categories()
        self.assertEqual(categories.get("WaterPump"), "water")
        self.assertEqual(categories.get("EfficientFarmHouse"), "food")
        self.assertEqual(categories.get("Lodge"), "housing")

    def test_category_of_spec_strips_faction_suffix(self):
        self.assertEqual(category_of_spec("Lodge.Folktails"), "housing")

    def test_unknown_spec_has_no_category(self):
        self.assertIsNone(category_of_spec("TotallyUnknownSpec"))

    def test_missing_file_degrades_to_empty(self):
        categories = load_building_categories(path="/no/such/buildings.json")
        self.assertEqual(categories, {})


class FlatMaskTests(unittest.TestCase):
    def test_uniform_terrain_is_all_flat(self):
        grid = _map_grid({"width": 2, "height": 2, "terrain_height": [4, 4, 4, 4]})
        self.assertEqual(_flat_mask(grid), [True, True, True, True])

    def test_a_raised_tile_breaks_flatness_for_itself_and_its_neighbors(self):
        # 3x3, center raised (row-major: index 4 is the center).
        terrain = [4, 4, 4, 4, 6, 4, 4, 4, 4]
        grid = _map_grid({"width": 3, "height": 3, "terrain_height": terrain})
        mask = _flat_mask(grid)
        self.assertFalse(mask[4])  # the raised tile itself
        self.assertFalse(mask[1])  # north neighbor
        self.assertFalse(mask[3])  # west neighbor
        self.assertFalse(mask[5])  # east neighbor
        self.assertFalse(mask[7])  # south neighbor
        self.assertTrue(mask[0])   # corner: not orthogonally adjacent to center


class LocallyHighMaskTests(unittest.TestCase):
    def test_flat_map_has_no_locally_high_tiles(self):
        grid = _map_grid({"width": 3, "height": 3, "terrain_height": [4] * 9})
        self.assertEqual(_locally_high_mask(grid), [False] * 9)

    def test_bump_is_locally_high_its_flat_neighbors_are_not(self):
        terrain = [4, 4, 4, 4, 6, 4, 4, 4, 4]  # 3x3, center bumped up
        grid = _map_grid({"width": 3, "height": 3, "terrain_height": terrain})
        mask = _locally_high_mask(grid)
        self.assertEqual([i for i, v in enumerate(mask) if v], [4])


def _zone_test_map(width=8, height=6, origin=(0, 0), extra_occupied=()):
    """A small synthetic `/map` fixture for LP2 zone tests: a locally-high
    bump at (0, 0), a 2-tile-wide clean water body along the east edge
    (cols 6-7, all rows), moist farmland along the south edge (row 5, cols
    0-3), and the District Center at (2, 2). `extra_occupied` marks
    additional bridge `(x, y)` tiles as occupied (for reconcile tests)."""
    total = width * height
    origin_x, origin_y = origin
    terrain = [4] * total
    terrain[0] = 6  # locally-high bump, north/west of the map
    water = [0] * total
    for row in range(height):
        for col in (6, 7):
            water[row * width + col] = 3
    moist = [0] * total
    for col in range(4):
        moist[5 * width + col] = 1
    occupied = [0] * total
    occupied[2 * width + 2] = 1  # District Center footprint
    for x, y in extra_occupied:
        occupied[(y - origin_y) * width + (x - origin_x)] = 1
    return {
        "origin": {"x": origin_x, "z": origin_y},
        "width": width,
        "height": height,
        "terrain_height": terrain,
        "water_depth": water,
        "contamination": [0] * total,
        "moist": moist,
        "occupied": occupied,
        "reachable": [1] * total,
        "on_road": [0] * total,
        "district_center": {"x": origin_x + 2, "y": origin_y + 2},
    }


class ZoneAssignmentTests(unittest.TestCase):
    WIDTH, HEIGHT = 8, 6

    def setUp(self):
        self.map_data = _zone_test_map(self.WIDTH, self.HEIGHT)
        self.zones = assign_zones(self.map_data)

    def _index(self, x, y):
        return y * self.WIDTH + x

    def _water_depth(self, x, y):
        if not (0 <= x < self.WIDTH and 0 <= y < self.HEIGHT):
            return 0
        return self.map_data["water_depth"][self._index(x, y)]

    def _moist(self, x, y):
        return bool(self.map_data["moist"][self._index(x, y)])

    def _occupied(self, x, y):
        return bool(self.map_data["occupied"][self._index(x, y)])

    def _flat(self, x, y):
        own = self.map_data["terrain_height"][self._index(x, y)]
        for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < self.WIDTH and 0 <= ny < self.HEIGHT:
                if self.map_data["terrain_height"][self._index(nx, ny)] != own:
                    return False
        return True

    @staticmethod
    def _dist(xy, dc):
        return abs(xy[0] - dc[0]) + abs(xy[1] - dc[1])

    def test_water_zone_tiles_are_adjacent_to_clean_water(self):
        water_tiles = self.zones.zone_for("water")
        self.assertTrue(water_tiles)
        for x, y in water_tiles:
            neighbors = ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1))
            self.assertTrue(
                any(self._water_depth(nx, ny) > 0 for nx, ny in neighbors),
                "%r not adjacent to water" % ((x, y),),
            )

    def test_food_and_forestry_zones_are_all_moist(self):
        for category in ("food", "forestry"):
            tiles = self.zones.zone_for(category)
            self.assertTrue(tiles, category)
            for x, y in tiles:
                self.assertTrue(self._moist(x, y), "%r not moist" % ((x, y),))

    def test_power_zone_includes_water_edge_and_locally_high_tiles(self):
        power = self.zones.zone_for("power")
        water = self.zones.zone_for("water")
        self.assertTrue(water.issubset(power))
        self.assertIn((0, 0), power)          # the locally-high bump
        self.assertNotIn((0, 0), water)       # not itself adjacent to water

    def test_housing_zone_is_flat_reachable_and_not_on_water(self):
        housing = self.zones.zone_for("housing")
        industry = self.zones.zone_for("industry")
        water_tiles = self.zones.zone_for("water")
        self.assertTrue(housing)
        self.assertTrue(industry)
        for x, y in housing:
            self.assertLessEqual(self._water_depth(x, y), 0)
            self.assertNotIn((x, y), water_tiles)
            self.assertTrue(self._flat(x, y), "%r not flat" % ((x, y),))
            self.assertFalse(self._occupied(x, y))
        # Ring order: housing sits no farther from the DC than industry.
        dc = (2, 2)
        self.assertLessEqual(
            max(self._dist(t, dc) for t in housing),
            min(self._dist(t, dc) for t in industry),
        )

    def test_regions_are_subsets_of_reachable_land(self):
        reachable_land = {
            (x, y)
            for y in range(self.HEIGHT)
            for x in range(self.WIDTH)
            if self._water_depth(x, y) <= 0 and not self._occupied(x, y)
        }
        for category, tiles in self.zones.regions.items():
            self.assertTrue(
                tiles <= reachable_land,
                "%s has tiles outside reachable land: %r"
                % (category, tiles - reachable_land),
            )

    def test_zone_for_spec_resolves_via_category(self):
        self.assertEqual(self.zones.zone_for_spec("WaterPump"), self.zones.zone_for("water"))
        self.assertEqual(
            self.zones.zone_for_spec("EfficientFarmHouse"), self.zones.zone_for("food")
        )
        self.assertEqual(self.zones.zone_for_spec("Lodge"), self.zones.zone_for("housing"))

    def test_zone_for_unknown_category_falls_back_to_general(self):
        self.assertEqual(self.zones.zone_for("nonsense"), self.zones.zone_for(GENERAL_ZONE))
        self.assertEqual(self.zones.zone_for(None), self.zones.zone_for(GENERAL_ZONE))

    def test_reconcile_drops_a_tile_that_becomes_occupied(self):
        housing = self.zones.zone_for("housing")
        tile = next(iter(housing))
        occupied_map = _zone_test_map(self.WIDTH, self.HEIGHT, extra_occupied=[tile])

        result = self.zones.reconcile(occupied_map)

        self.assertIs(result, self.zones.regions)
        for tiles in self.zones.regions.values():
            self.assertNotIn(tile, tiles)

    def test_reconcile_does_not_reassign_categories(self):
        before = {category: set(tiles) for category, tiles in self.zones.regions.items()}
        self.zones.reconcile(self.map_data)  # nothing new occupied
        after = self.zones.regions
        self.assertEqual(before, after)


class AssignZonesDegenerateInputTests(unittest.TestCase):
    def test_empty_map_returns_empty_zones_for_every_category(self):
        zones = assign_zones({})
        for category in _ALL_ZONE_CATEGORIES:
            self.assertEqual(zones.zone_for(category), set())

    def test_non_dict_map_does_not_raise(self):
        zones = assign_zones(None)
        self.assertEqual(zones.zone_for("water"), set())


# ---------------------------------------------------------------------------
# LP3 inline tests
# ---------------------------------------------------------------------------

def _open_map(width=6, height=6, origin=(0, 0), z=4):
    """A fully flat, dry, unoccupied `/map` fixture for LP3 tests: every
    tile is buildable land at terrain height `z`, all reachable, no roads.
    The District Center defaults to the map's center tile."""
    total = width * height
    origin_x, origin_y = origin
    return {
        "origin": {"x": origin_x, "z": origin_y},
        "width": width,
        "height": height,
        "terrain_height": [z] * total,
        "water_depth": [0] * total,
        "occupied": [0] * total,
        "reachable": [1] * total,
        "on_road": [0] * total,
        "district_center": {"x": origin_x + width // 2, "y": origin_y + height // 2},
    }


def _corridor_map():
    """A 5x3 fixture with a single 1-wide dry corridor at row y=1 connecting
    a District Center stand-in at (0, 1) to an existing building at (4, 1);
    rows y=0 and y=2 are water except a single land tile at (1, 2) -- a
    dead-end alcove reachable ONLY through the corridor tile (1, 1), never
    through the through-corridor tile (2, 1). Used by `BoxingCheckTests`:
    reserving (2, 1) severs the corridor (boxing); reserving (1, 2) does
    not."""
    width, height = 5, 3
    total = width * height

    def index(x, y):
        return y * width + x

    terrain = [4] * total
    water = [0] * total
    for x in range(width):
        water[index(x, 0)] = 3  # row 0: all water
        water[index(x, 2)] = 3  # row 2: all water...
    water[index(1, 2)] = 0      # ...except the (1, 2) alcove

    occupied = [0] * total
    occupied[index(0, 1)] = 1  # DC stand-in
    occupied[index(4, 1)] = 1  # existing building

    return {
        "origin": {"x": 0, "z": 0},
        "width": width,
        "height": height,
        "terrain_height": terrain,
        "water_depth": water,
        "occupied": occupied,
        "reachable": [1] * total,
        "on_road": [0] * total,
        "district_center": {"x": 0, "y": 1},
    }


class WalkableFloodTests(unittest.TestCase):
    def setUp(self):
        self.map_data = _open_map(4, 4)
        for row in range(4):
            self.map_data["water_depth"][row * 4 + 2] = 3  # column x=2 is all water
        self.reservation = Reservation(stacking=_TEST_STACKING)

    def test_water_blocks_the_flood(self):
        reached = _walkable_flood(self.reservation, self.map_data, (0, 0))
        self.assertIn((1, 0), reached)
        self.assertNotIn((3, 0), reached)

    def test_reserved_footprint_blocks_the_flood(self):
        self.map_data["water_depth"][1 * 4 + 2] = 0  # punch a gap at (2, 1)
        reached = _walkable_flood(self.reservation, self.map_data, (0, 0))
        self.assertIn((3, 0), reached)  # now reachable through the gap

        self.reservation.reserve("Hut", 2, 1, 4, "N", owner=RESERVED)
        blocked = _walkable_flood(self.reservation, self.map_data, (0, 0))
        self.assertNotIn((3, 0), blocked)  # the gap itself is now claimed

    def test_start_is_always_seeded_even_if_blocked(self):
        self.reservation.reserve("Hut", 0, 0, 4, "N", owner=BUILT)
        reached = _walkable_flood(self.reservation, self.map_data, (0, 0))
        self.assertIn((0, 0), reached)  # the seed itself, even though BUILT
        self.assertIn((1, 0), reached)  # its walkable neighbour is still explored


class AccessTilesTests(unittest.TestCase):
    def setUp(self):
        self.reservation = Reservation(stacking=_TEST_STACKING)

    def test_returns_ring_around_footprint_excluding_footprint_itself(self):
        building = {"spec": "Big", "x": 5, "y": 5, "z": 4, "orientation": "N"}  # 2x3
        access = set(_access_tiles(self.reservation, building))
        footprint_xy = {(5, 5), (6, 5), (5, 6), (6, 6), (5, 7), (6, 7)}
        self.assertTrue(footprint_xy.isdisjoint(access))
        for tile in ((4, 5), (4, 6), (4, 7), (7, 5), (7, 6), (7, 7), (5, 4), (6, 4), (5, 8), (6, 8)):
            self.assertIn(tile, access)

    def test_malformed_building_yields_empty_list(self):
        self.assertEqual(_access_tiles(self.reservation, {}), [])
        self.assertEqual(_access_tiles(self.reservation, None), [])


class PlanPlacementsBasicTests(unittest.TestCase):
    def test_two_specs_in_the_same_zone_do_not_overlap_and_stay_reachable(self):
        map_data = _open_map(6, 6)
        reservation = Reservation(stacking=_TEST_STACKING)
        zones = Zones({"housing": {(0, 0), (1, 0), (0, 1), (1, 1)}},
                       categories={"Hut": "housing"})

        placements = plan_placements(["Hut", "Hut"], map_data, reservation, zones)

        self.assertEqual(len(placements), 2)
        cells = [
            set(reservation.footprint_cells(p["spec"], p["x"], p["y"], p["z"], p["orientation"]))
            for p in placements
        ]
        self.assertTrue(cells[0].isdisjoint(cells[1]))
        dc = (map_data["district_center"]["x"], map_data["district_center"]["y"])
        reached = _walkable_flood(reservation, map_data, dc)
        for p in placements:
            self.assertTrue(set(_access_tiles(reservation, p)) & reached)

    def test_batch_of_three_all_placed_disjoint_and_reachable(self):
        map_data = _open_map(6, 6)
        reservation = Reservation(stacking=_TEST_STACKING)
        zones = Zones({"housing": {(0, 0), (1, 0), (0, 1), (1, 1)}},
                       categories={"Hut": "housing"})

        placements = plan_placements(["Hut", "Hut", "Hut"], map_data, reservation, zones)

        self.assertEqual(len(placements), 3)
        dc = (map_data["district_center"]["x"], map_data["district_center"]["y"])
        reached = _walkable_flood(reservation, map_data, dc)
        seen = set()
        for p in placements:
            cells = set(reservation.footprint_cells(p["spec"], p["x"], p["y"], p["z"], p["orientation"]))
            self.assertTrue(cells.isdisjoint(seen))
            seen |= cells
            self.assertTrue(set(_access_tiles(reservation, p)) & reached)

    def test_unplaceable_spec_is_skipped_not_crashed(self):
        map_data = _open_map(6, 6)
        map_data["occupied"] = [1] * 36  # kill the "any reachable land" fallback
        reservation = Reservation(stacking=_TEST_STACKING)
        zones = Zones({"housing": {(2, 2), (3, 2)}},
                       categories={"Hut": "housing"})  # "Ghost" left uncategorized

        placements = plan_placements(["Hut", "Hut", "Ghost"], map_data, reservation, zones)

        self.assertEqual(len(placements), 2)
        self.assertEqual({p["spec"] for p in placements}, {"Hut"})

    def test_empty_specs_list_returns_empty(self):
        map_data = _open_map(4, 4)
        reservation = Reservation(stacking=_TEST_STACKING)
        zones = Zones({})
        self.assertEqual(plan_placements([], map_data, reservation, zones), [])

    def test_goal_like_dict_entries_are_accepted(self):
        map_data = _open_map(6, 6)
        reservation = Reservation(stacking=_TEST_STACKING)
        zones = Zones({"housing": {(0, 0)}}, categories={"Hut": "housing"})

        placements = plan_placements(
            [{"id": "build_hut", "spec": "Hut"}], map_data, reservation, zones
        )

        self.assertEqual(len(placements), 1)
        self.assertEqual(placements[0]["spec"], "Hut")


class BoxingCheckTests(unittest.TestCase):
    def setUp(self):
        self.map_data = _corridor_map()
        self.reservation = Reservation(stacking=_TEST_STACKING)
        self.reservation.reserve("Hut", 0, 1, 4, "N", owner=BUILT)  # DC stand-in
        self.reservation.reserve("Hut", 4, 1, 4, "N", owner=BUILT)  # existing building
        self.state = {"buildings": {"list": [
            {"spec": "Hut", "x": 4, "y": 1, "z": 4, "orientation": "N", "status": "finished"},
        ]}}
        # The zone offers BOTH the through-corridor tile (2, 1) [boxes the
        # existing building] and the dead-end alcove (1, 2) [safe]. (2, 1)
        # ranks closer to the DC by the planner's own tie-break (see the
        # sanity check below), so picking the alcove instead proves the
        # boxing check -- not luck -- decided it.
        self.zones = Zones({"z": {(2, 1), (1, 2)}}, categories={"Hut": "z"})

    def test_boxing_tile_rejected_alcove_chosen_instead(self):
        placements = plan_placements(
            ["Hut"], self.map_data, self.reservation, self.zones, state=self.state
        )
        self.assertEqual(len(placements), 1)
        self.assertEqual((placements[0]["x"], placements[0]["y"]), (1, 2))

    def test_corridor_tile_alone_would_have_ranked_first(self):
        # Sanity check on the fixture itself: without the boxing check,
        # naive nearest-tile-first ranking WOULD pick (2, 1) -- so the
        # (1, 2) result above is the boxing check's doing, not the
        # ranking's.
        dc_xy = (0, 1)
        ranked = sorted(self.zones.zone_for("z"), key=lambda xy: (_manhattan(xy, dc_xy), xy[1], xy[0]))
        self.assertEqual(ranked[0], (2, 1))


class BoxingFrontierExhaustionTests(unittest.TestCase):
    def test_candidate_that_would_zero_the_frontier_is_rejected_and_spec_skipped(self):
        # (0,0) DC, (1,0) candidate, (2,0) the only other tile -- reserving
        # (1,0) would cut the DC off from (2,0), leaving no free frontier.
        map_data = _open_map(3, 1)
        map_data["district_center"] = {"x": 0, "y": 0}
        reservation = Reservation(stacking=_TEST_STACKING)
        reservation.reserve("Hut", 0, 0, 4, "N", owner=BUILT)  # DC stand-in
        zones = Zones({"z": {(1, 0)}}, categories={"Hut": "z"})

        placements = plan_placements(["Hut"], map_data, reservation, zones)

        self.assertEqual(placements, [])


def _flat_map(width=12, height=12, surface=4, dc=(5, 5)):
    total = width * height
    return {"origin": {"x": 0, "z": 0}, "width": width, "height": height,
            "terrain_height": [surface] * total, "occupied": [0] * total,
            "water_depth": [0.0] * total, "reachable": [1] * total,
            "on_road": [1] * total, "district_center": {"x": dc[0], "z": dc[1]}}


class VerticalPlacementTests(unittest.TestCase):
    """LP4 -- plan_vertical_placement direct-stack + platform-deck."""

    def test_direct_stack_on_existing_stackable_building(self):
        # A finished Lodge (stackable) -> a new Lodge stacks one Z above it.
        res = Reservation(stacking=_TEST_STACKING)
        res.reserve("Lodge", 5, 5, 4, "N", owner=BUILT)
        m = _flat_map(dc=(5, 5))
        seq = plan_vertical_placement("Lodge", m, res, None)
        self.assertTrue(seq)
        top = seq[-1]
        self.assertEqual(top["spec"], "Lodge")
        self.assertEqual(top["z"], 5)  # lodge_top(4) + 1
        self.assertEqual(top["role"], "stacked")
        # and it is actually supported by the lodge below (reserved by the call).
        self.assertTrue(res.supported("Lodge", 5, 5, 5, "N", terrain_height_lookup(m)))

    def test_platform_deck_when_no_stackable_top(self):
        # No existing stackable surface -> platform deck + stairs + target on top.
        res = Reservation(stacking=_TEST_STACKING)
        m = _flat_map(dc=(5, 5))
        # "Rooftop" (base_matter="stackable") can ONLY sit on a stackable surface,
        # so it forces the platform-deck path.
        seq = plan_vertical_placement("Rooftop", m, res, None)
        self.assertTrue(seq)
        self.assertEqual(seq[0]["spec"], _PLATFORM_SPEC)
        self.assertEqual(seq[0]["role"], "support")
        roles = [p["role"] for p in seq]
        # support(s) come BEFORE the stacked building (build order matters).
        self.assertEqual(roles[-1], "stacked")
        self.assertLess(roles.index("support"), len(roles) - 1)
        top = seq[-1]
        self.assertEqual(top["spec"], "Rooftop")
        self.assertEqual(top["z"], 5)  # surface(4) + 1
        # an access stair column was added.
        self.assertIn(_STAIRS_SPEC, [p["spec"] for p in seq])

    def test_ground_only_spec_cannot_stack(self):
        # base_matter "ground" -> never fits at z+1 -> no vertical option.
        res = Reservation(stacking=_TEST_STACKING)
        res.reserve("Lodge", 5, 5, 4, "N", owner=BUILT)  # a stackable top exists
        m = _flat_map(dc=(5, 5))
        seq = plan_vertical_placement("Shed", m, res, None)  # Shed base_matter=ground
        self.assertEqual(seq, [])

    def test_plan_placements_falls_back_to_vertical_when_ground_full(self):
        # Fill the whole map with built cells so no ground tile fits, but leave a
        # stackable Lodge to stack on -> plan_placements returns a stacked placement.
        res = Reservation(stacking=_TEST_STACKING)
        m = _flat_map(width=6, height=6, surface=4, dc=(2, 2))
        # occupy every ground cell at surface, except the lodge footprint.
        for y in range(6):
            for x in range(6):
                res.cells[(x, y, 4)] = BUILT
                res._cell_spec[(x, y, 4)] = "Big"
        # place a stackable lodge (overwrite its cells as a stackable surface)
        for cell in res.footprint_cells("Lodge", 2, 2, 4, "N"):
            res.cells[cell] = BUILT
            res._cell_spec[cell] = "Lodge"
        zones = assign_zones(m)
        placements = plan_placements(["Lodge"], m, res, zones)
        self.assertTrue(placements)
        self.assertEqual(placements[-1]["spec"], "Lodge")
        self.assertEqual(placements[-1].get("role"), "stacked")
        self.assertEqual(placements[-1]["z"], 5)


if __name__ == "__main__":
    unittest.main()
