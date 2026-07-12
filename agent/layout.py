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

Pure-stdlib, no network/torch. Runs standalone: `python3 agent/layout.py`.
"""

from __future__ import annotations

import json
import os
import unittest

_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_AGENT_DIR, "data")
_STACKING_PATH = os.path.join(_DATA_DIR, "building_stacking.json")

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


if __name__ == "__main__":
    unittest.main()
