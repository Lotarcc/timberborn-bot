"""Layout macros for the Timberborn agent.

Pure, self-contained helpers that emit multi-building *layout templates* as
relative placement offsets. The point is to let the agent drop coherent
functional clusters (a pump with its tanks, a housing block, a full
production chain) in one shot instead of scattering one building at a time.

A macro returns a ``list`` of offset dicts, each shaped like::

    {"spec": str, "dx": int, "dy": int, "orientation": str | None, "role": str}

Offsets are relative to an *anchor* tile ``(ax, ay)`` chosen by the caller.
The caller adds the anchor to every ``(dx, dy)`` and validates / places each
item through the bridge. Some items are not real buildings:

* ``role == "path"``  -> a reserved 1x1 path tile (spec ``"Path"``) that keeps
  an access tile free next to a building's entrance side.
* ``role == "planting_area"`` -> a descriptor block (carries an explicit
  ``footprint``) marking the tile region a Forester should be told to plant.

Coordinate convention: tiles are ``(x, y)``; a building's ``(dx, dy)`` is the
*min corner* of its footprint, which extends ``+x`` (width) and ``+y`` (depth).

This module is PURE: no network / bridge / HTTP calls, no torch. It only
optionally reads ``data/buildings.json`` (stdlib ``json``) to size templates
from the real game footprints, and falls back to baked-in footprints if that
file is unavailable.
"""

from __future__ import annotations

import json
import os
import unittest

# --------------------------------------------------------------------------- #
# Footprint resolution
# --------------------------------------------------------------------------- #

_DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "buildings.json")

# Fallback footprints (x=width, y=depth) keyed by normalized spec, used when
# buildings.json cannot be read. Values verified against buildings.json.
_FALLBACK_FOOTPRINTS = {
    "waterpump": (2, 3),
    "smalltank": (1, 1),
    "mediumtank": (2, 2),
    "largetank": (3, 3),
    "lodge": (2, 2),
    "minilodge": (2, 1),
    "doublelodge": (2, 2),
    "triplelodge": (2, 3),
    "forester": (2, 2),
    "efficientfarmhouse": (3, 2),
    "aquaticfarmhouse": (2, 3),
    "gristmill": (3, 2),
    "bakery": (3, 2),
    "gathererflag": (1, 1),
    "lumberjackflag": (1, 1),
    "path": (1, 1),
}

# Spec aliases -> canonical normalized key used to look up a footprint.
_SPEC_ALIASES = {
    "foresterflag": "forester",
    "farmhouse": "efficientfarmhouse",
}


def _normalize(spec):
    """Normalize a spec / building id to a lookup key.

    Strips a faction suffix (``.Folktails``), lowercases, and applies aliases.
    """
    base = str(spec).split(".", 1)[0].strip().lower()
    return _SPEC_ALIASES.get(base, base)


def _load_footprints():
    """Build ``{normalized_key: (w, h)}`` from buildings.json, else fallback."""
    footprints = dict(_FALLBACK_FOOTPRINTS)
    try:
        with open(_DATA_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return footprints
    for building in data.get("buildings", []):
        fp = building.get("footprint") or {}
        try:
            w = int(fp["x"])
            h = int(fp["y"])
        except (KeyError, TypeError, ValueError):
            continue
        key = _normalize(building.get("id", ""))
        if key:
            footprints[key] = (w, h)
    return footprints


_FOOTPRINTS = _load_footprints()


def footprint(spec):
    """Return ``(width, depth)`` in tiles for a spec. Raises on unknown spec."""
    key = _normalize(spec)
    if key not in _FOOTPRINTS:
        raise KeyError("no footprint known for spec %r" % (spec,))
    return _FOOTPRINTS[key]


# --------------------------------------------------------------------------- #
# Occupancy helpers
# --------------------------------------------------------------------------- #

def _item_footprint(item):
    """Footprint for one offset item: explicit ``footprint`` wins over spec."""
    fp = item.get("footprint")
    if fp:
        return int(fp["x"]), int(fp["y"])
    return footprint(item["spec"])


def occupied_tiles(item):
    """Return the set of ``(dx, dy)`` tiles one offset item covers."""
    w, h = _item_footprint(item)
    dx, dy = int(item["dx"]), int(item["dy"])
    return {(dx + i, dy + j) for i in range(w) for j in range(h)}


def find_overlap(items):
    """Return the first pair ``(a, b)`` of items whose tiles overlap, else None."""
    tiles = []
    for item in items:
        tiles.append((item, occupied_tiles(item)))
    for i in range(len(tiles)):
        for j in range(i + 1, len(tiles)):
            if tiles[i][1] & tiles[j][1]:
                return (tiles[i][0], tiles[j][0])
    return None


def _offset(spec, dx, dy, role, orientation=None, footprint=None):
    item = {
        "spec": spec,
        "dx": int(dx),
        "dy": int(dy),
        "orientation": orientation,
        "role": role,
    }
    if footprint is not None:
        item["footprint"] = footprint
    return item


# --------------------------------------------------------------------------- #
# Macros
# --------------------------------------------------------------------------- #

def forester_plantation(rows=4, cols=4):
    """Forester + an implied ``rows`` x ``cols`` planting grid.

    Anchor ``(0, 0)`` is the min corner of the planting block, which spans
    ``cols`` tiles in ``x`` and ``rows`` tiles in ``y``. The Forester (2x2)
    sits to the left of the block with a 1-tile path gap between them, so the
    forester never overlaps the tiles it plants on and stays path-reachable.

    Returns the Forester placement, a ``planting_area`` descriptor covering the
    grid (so the caller can issue a designate_planting over it), and the
    reserved path tiles.
    """
    if rows < 1 or cols < 1:
        raise ValueError("rows and cols must be >= 1")

    fw, fh = footprint("ForesterFlag")  # 2 x 2
    items = []

    # Path gap column immediately left of the grid (x = -1), spanning the grid.
    for j in range(rows):
        items.append(_offset("Path", -1, j, role="path"))

    # Forester sits left of the path gap: occupies x in [-1-fw, -2].
    forester_dx = -1 - fw
    items.append(
        _offset("ForesterFlag", forester_dx, 0, role="forester", orientation="E")
    )
    # Access path on the forester's own far (left) side.
    for j in range(fh):
        items.append(_offset("Path", forester_dx - 1, j, role="path"))

    # The planting grid itself, as a single descriptor block at the anchor.
    items.append(
        _offset(
            "planting_area",
            0,
            0,
            role="planting_area",
            footprint={"x": cols, "y": rows, "z": 1},
        )
    )
    return items


def pump_and_storage(tanks=3):
    """Water Pump plus ``tanks`` (clamped 2..4) Small Tanks clustered behind it.

    The pump (2x3) faces water on its ``-y`` (front, "south") side. A shared
    access path runs along ``y = 3`` behind it; the tanks sit one row further
    back at ``y = 4`` so each tank is path-adjacent and close to the source.
    """
    tanks = max(2, min(4, int(tanks)))
    pw, ph = footprint("WaterPump")  # 2 x 3
    items = []

    # Pump at anchor; front (-y) overhangs water, so orientation faces south.
    items.append(_offset("WaterPump", 0, 0, role="pump", orientation="S"))

    path_y = ph  # row directly behind the pump
    tank_y = ph + 1
    span = max(pw, tanks)

    # Access path row behind the pump, wide enough for pump + all tanks.
    for i in range(span):
        items.append(_offset("Path", i, path_y, role="path"))

    # Small tanks in a row one step behind the path, hugging the source.
    for i in range(tanks):
        items.append(_offset("SmallTank", i, tank_y, role="storage"))

    return items


def housing_cluster(count=3):
    """``count`` Lodges packed in a row with 1-tile path gaps between them.

    Each Lodge is 2x2 with a stride of 3 (2 wide + 1 gap), so beavers can path
    through the vertical gap columns. A shared front path row at ``y = -1``
    gives every Lodge an access tile on its entrance side.
    """
    if count < 1:
        raise ValueError("count must be >= 1")

    lw, lh = footprint("Lodge")  # 2 x 2
    stride = lw + 1  # building width + 1-tile gap
    items = []

    for i in range(count):
        dx = i * stride
        items.append(_offset("Lodge", dx, 0, role="housing"))
        # 1-tile path gap column between this lodge and the next.
        if i < count - 1:
            for j in range(lh):
                items.append(_offset("Path", dx + lw, j, role="path"))

    # Front access path row spanning the whole cluster (entrance side).
    total_w = (count - 1) * stride + lw
    for i in range(total_w):
        items.append(_offset("Path", i, -1, role="path"))

    return items


def bakery_chain():
    """The bread production chain, placed adjacent in flow order.

    Farmhouse (grows wheat) -> Gristmill (wheat -> wheat_flour) ->
    Bakery (wheat_flour -> bread). Each building is 3x2; they sit directly
    edge-to-edge along ``x`` to minimize hauling distance, sharing a front
    access path row at ``y = -1``.
    """
    chain = [
        ("EfficientFarmhouse", "farm"),
        ("Gristmill", "mill"),
        ("Bakery", "bakery"),
    ]
    items = []
    x = 0
    for spec, role in chain:
        w, _h = footprint(spec)
        items.append(_offset(spec, x, 0, role=role))
        x += w  # next building butts directly against this one

    # Shared front access path along the whole chain (entrance side, y = -1).
    for i in range(x):
        items.append(_offset("Path", i, -1, role="path"))

    return items


MACROS = {
    "forester_plantation": forester_plantation,
    "pump_and_storage": pump_and_storage,
    "housing_cluster": housing_cluster,
    "bakery_chain": bakery_chain,
}


def building_specs(items):
    """Return the list of real building specs in an offset list.

    Excludes reserved path tiles and non-building descriptors.
    """
    skip_roles = {"path", "planting_area"}
    return [item["spec"] for item in items if item["role"] not in skip_roles]


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

class LayoutMacroTests(unittest.TestCase):
    def _assert_no_overlap(self, items):
        overlap = find_overlap(items)
        if overlap is not None:
            a, b = overlap
            self.fail(
                "overlap between %s@(%d,%d) and %s@(%d,%d): shared %s"
                % (
                    a["spec"], a["dx"], a["dy"],
                    b["spec"], b["dx"], b["dy"],
                    sorted(occupied_tiles(a) & occupied_tiles(b)),
                )
            )

    def test_footprints_available(self):
        # Every spec our macros emit must resolve to a footprint.
        for spec in ("ForesterFlag", "WaterPump", "SmallTank", "Lodge",
                     "EfficientFarmhouse", "Gristmill", "Bakery", "Path"):
            w, h = footprint(spec)
            self.assertGreaterEqual(w, 1)
            self.assertGreaterEqual(h, 1)

    def test_faction_suffix_stripped(self):
        self.assertEqual(footprint("Lodge.Folktails"), footprint("Lodge"))

    def test_forester_plantation(self):
        items = forester_plantation(rows=4, cols=4)
        self._assert_no_overlap(items)
        self.assertEqual(building_specs(items), ["ForesterFlag"])
        # Exactly one planting_area descriptor, sized rows x cols.
        areas = [it for it in items if it["role"] == "planting_area"]
        self.assertEqual(len(areas), 1)
        self.assertEqual(occupied_tiles(areas[0]), {
            (i, j) for i in range(4) for j in range(4)
        })
        # Forester tiles must be disjoint from the planting tiles.
        forester = next(it for it in items if it["role"] == "forester")
        self.assertFalse(occupied_tiles(forester) & occupied_tiles(areas[0]))

    def test_forester_plantation_sizes(self):
        for rows, cols in ((1, 1), (2, 6), (5, 3), (8, 8)):
            items = forester_plantation(rows=rows, cols=cols)
            self._assert_no_overlap(items)
            area = next(it for it in items if it["role"] == "planting_area")
            self.assertEqual(len(occupied_tiles(area)), rows * cols)

    def test_pump_and_storage(self):
        for n in (2, 3, 4):
            items = pump_and_storage(tanks=n)
            self._assert_no_overlap(items)
            specs = building_specs(items)
            self.assertEqual(specs.count("WaterPump"), 1)
            self.assertEqual(specs.count("SmallTank"), n)
        # clamp
        self.assertEqual(building_specs(pump_and_storage(tanks=99)).count("SmallTank"), 4)
        self.assertEqual(building_specs(pump_and_storage(tanks=0)).count("SmallTank"), 2)

    def test_pump_and_storage_default(self):
        items = pump_and_storage()
        self._assert_no_overlap(items)
        self.assertIn("WaterPump", building_specs(items))

    def test_housing_cluster(self):
        for n in (1, 3, 5):
            items = housing_cluster(count=n)
            self._assert_no_overlap(items)
            self.assertEqual(building_specs(items), ["Lodge"] * n)

    def test_housing_cluster_has_gaps(self):
        # Between two adjacent lodges there must be a free (path) column.
        items = housing_cluster(count=2)
        lodges = [it for it in items if it["role"] == "housing"]
        lw, _ = footprint("Lodge")
        gap_x = lodges[0]["dx"] + lw
        lodge_tiles = set()
        for it in lodges:
            lodge_tiles |= occupied_tiles(it)
        # The gap column is not occupied by any lodge.
        self.assertFalse(any((gap_x, y) in lodge_tiles for y in range(-1, 3)))

    def test_bakery_chain(self):
        items = bakery_chain()
        self._assert_no_overlap(items)
        self.assertEqual(
            building_specs(items),
            ["EfficientFarmhouse", "Gristmill", "Bakery"],
        )
        # Chain must be laid out in flow order along +x, edge-to-edge.
        buildings = [it for it in items if it["role"] not in ("path",)]
        xs = [it["dx"] for it in buildings]
        self.assertEqual(xs, sorted(xs))

    def test_all_macros_overlap_free(self):
        for name, fn in MACROS.items():
            items = fn()
            with self.subTest(macro=name):
                self._assert_no_overlap(items)
                self.assertTrue(building_specs(items))

    def test_offset_schema(self):
        for fn in MACROS.values():
            for item in fn():
                for key in ("spec", "dx", "dy", "orientation", "role"):
                    self.assertIn(key, item)
                self.assertIsInstance(item["dx"], int)
                self.assertIsInstance(item["dy"], int)


if __name__ == "__main__":
    unittest.main(verbosity=2)
