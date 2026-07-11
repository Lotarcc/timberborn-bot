"""Pure-stdlib spatial primitives for Timberborn's small row-major maps.

Internal coordinates are ``(col, row)``. Bridge/world coordinates are ``(x, y)``,
where bridge ``y`` is the map's z-axis. Grids are flat row-major sequences.
"""

from collections import deque
import heapq
import math


DIRECTIONS = ((0, -1), (1, 0), (0, 1), (-1, 0))


def distance_field(sources, width, height, passable=None, terrain=None, max_step=None):
    """Return shortest 4-neighbor distances using multi-source BFS in O(N).

    ``sources`` may be an iterable of ``(col, row)`` pairs or a flat boolean
    mask. Impassable cells and cells separated by a terrain change greater than
    ``max_step`` remain ``-1``.
    """
    total = _grid_size(width, height)
    distances = [-1] * total
    queue = deque()
    for col, row in _source_points(sources, width, height):
        index = row * width + col
        # Seed the source at distance 0 EVEN IF it is impassable: a source tile is
        # a distance origin (a district center, a tree) whose own tile is occupied,
        # but its passable neighbours must still get distance 1, 2, ... Only the
        # EXPANSION below respects `passable`, not the seeding.
        if distances[index] == 0:
            continue
        distances[index] = 0
        queue.append((col, row))

    while queue:
        col, row = queue.popleft()
        index = row * width + col
        for dcol, drow in DIRECTIONS:
            other_col, other_row = col + dcol, row + drow
            if not _inside(other_col, other_row, width, height):
                continue
            other_index = other_row * width + other_col
            if distances[other_index] >= 0 or not _value(passable, other_index, True):
                continue
            if terrain is not None and max_step is not None:
                if abs(_number(_value(terrain, other_index, 0)) - _number(_value(terrain, index, 0))) > max_step:
                    continue
            distances[other_index] = distances[index] + 1
            queue.append((other_col, other_row))
    return distances


def influence(dist, scale, amplitude=1.0, kind="decay"):
    """Convert a distance field to exponential or linear influence in O(N)."""
    if scale <= 0:
        raise ValueError("scale must be positive")
    if kind not in ("decay", "linear"):
        raise ValueError("kind must be 'decay' or 'linear'")
    result = []
    for distance in dist:
        if distance is None or distance < 0:
            result.append(0.0)
        elif kind == "decay":
            result.append(float(amplitude) * math.exp(-distance / scale))
        else:
            result.append(float(amplitude) * max(0.0, 1.0 - distance / scale))
    return result


def norm(field):
    """Min-max normalize a field to 0..1; a flat or empty field maps to zero."""
    values = list(field)
    if not values:
        return []
    low, high = min(values), max(values)
    if high == low:
        return [0.0] * len(values)
    span = high - low
    return [(value - low) / span for value in values]


def stack(layers_weights):
    """Return a weighted sum of normalized layers; negative weights repel."""
    layers_weights = list(layers_weights)
    if not layers_weights:
        return []
    length = len(layers_weights[0][0])
    result = [0.0] * length
    for field, weight in layers_weights:
        if len(field) != length:
            raise ValueError("all stacked fields must have the same length")
        for index, value in enumerate(norm(field)):
            result[index] += value * weight
    return result


def label_regions(mask, width, height, diagonal=True):
    """Flood-fill connected true cells in O(N), returning labels and metadata."""
    total = _grid_size(width, height)
    labels = [-1] * total
    regions = []
    neighbors = DIRECTIONS
    if diagonal:
        neighbors = DIRECTIONS + ((-1, -1), (1, -1), (1, 1), (-1, 1))

    for start in range(total):
        if labels[start] >= 0 or not _value(mask, start, False):
            continue
        region_id = len(regions)
        start_col, start_row = start % width, start // width
        labels[start] = region_id
        queue = deque([(start_col, start_row)])
        cells = []
        col_sum = row_sum = 0
        while queue:
            col, row = queue.popleft()
            cells.append((col, row))
            col_sum += col
            row_sum += row
            for dcol, drow in neighbors:
                other_col, other_row = col + dcol, row + drow
                if not _inside(other_col, other_row, width, height):
                    continue
                other = other_row * width + other_col
                if labels[other] >= 0 or not _value(mask, other, False):
                    continue
                labels[other] = region_id
                queue.append((other_col, other_row))
        size = len(cells)
        regions.append(
            {
                "id": region_id,
                "size": size,
                "centroid": (col_sum / size, row_sum / size),
                "cells": cells,
            }
        )
    return labels, regions


def voronoi_districts(seeds, width, height, passable):
    """Label cells by nearest passable seed with multi-source BFS in O(N).

    Seeds may be ``(col, row)`` pairs (ids are their input positions) or
    ``(id, col, row)`` triples. Equal-distance ties go to the earlier seed.
    """
    total = _grid_size(width, height)
    labels = [-1] * total
    queue = deque()
    for position, seed in enumerate(seeds):
        if len(seed) == 2:
            seed_id, col, row = position, seed[0], seed[1]
        else:
            seed_id, col, row = seed[0], seed[1], seed[2]
        if not _inside(col, row, width, height):
            continue
        index = row * width + col
        if labels[index] >= 0 or not _value(passable, index, False):
            continue
        labels[index] = seed_id
        queue.append((col, row))

    while queue:
        col, row = queue.popleft()
        index = row * width + col
        for dcol, drow in DIRECTIONS:
            other_col, other_row = col + dcol, row + drow
            if not _inside(other_col, other_row, width, height):
                continue
            other = other_row * width + other_col
            if labels[other] >= 0 or not _value(passable, other, False):
                continue
            labels[other] = labels[index]
            queue.append((other_col, other_row))
    return labels


def clusters(points, radius):
    """Greedily cluster points within Chebyshev ``radius`` of each seed.

    A spatial hash limits each seed to neighboring buckets, avoiding a pairwise
    distance matrix. Input order breaks greedy ties; counting buckets order the
    result by descending count in O(N).
    """
    if radius < 0:
        raise ValueError("radius must be non-negative")
    ordered = list(dict.fromkeys(tuple(point) for point in points))
    remaining = set(ordered)
    bucket_size = max(float(radius), 1.0)
    buckets = {}
    for point in ordered:
        buckets.setdefault(_point_bucket(point, bucket_size), set()).add(point)
    result = []
    for seed in ordered:
        if seed not in remaining:
            continue
        seed_bucket = _point_bucket(seed, bucket_size)
        members = []
        for bucket_col in range(seed_bucket[0] - 1, seed_bucket[0] + 2):
            for bucket_row in range(seed_bucket[1] - 1, seed_bucket[1] + 2):
                for point in tuple(buckets.get((bucket_col, bucket_row), ())):
                    if max(abs(point[0] - seed[0]), abs(point[1] - seed[1])) <= radius:
                        members.append(point)
        for point in members:
            remaining.remove(point)
            buckets[_point_bucket(point, bucket_size)].remove(point)
        count = len(members)
        result.append(
            {
                "centroid": (
                    sum(point[0] for point in members) / count,
                    sum(point[1] for point in members) / count,
                ),
                "count": count,
                "points": members,
            }
        )
    by_size = {}
    for group in result:
        by_size.setdefault(group["count"], []).append(group)
    return [
        group
        for size in range(len(ordered), 0, -1)
        for group in by_size.get(size, ())
    ]


def plantable_mask(arrays):
    """Return land that is moist, empty, and uncontaminated in O(N)."""
    grid = _arrays(arrays)
    total = grid["width"] * grid["height"]
    return [
        _number(_value(grid["water"], index, 0)) <= 0
        and bool(_value(grid["moist"], index, 0))
        and not bool(_value(grid["occupied"], index, 0))
        and _number(_value(grid["contamination"], index, 0)) <= 0
        for index in range(total)
    ]


def badwater_reach_mask(arrays, max_reach=7, step_falloff=5):
    """Flood badwater across water while tracking finite horizontal reach.

    Every horizontal move costs one reach. Each ascending terrain level costs an
    additional ``step_falloff``. A cell is revisited only when reached with a
    larger remaining budget; with Timberborn's fixed small reach this is O(N).
    """
    grid = _arrays(arrays)
    width, height = grid["width"], grid["height"]
    total = width * height
    remaining = [-1.0] * total
    queue = deque()
    for index in range(total):
        if _number(_value(grid["water"], index, 0)) > 0 and _number(_value(grid["contamination"], index, 0)) > 0:
            remaining[index] = float(max_reach)
            queue.append(index)

    while queue:
        index = queue.popleft()
        col, row = index % width, index // width
        for dcol, drow in DIRECTIONS:
            other_col, other_row = col + dcol, row + drow
            if not _inside(other_col, other_row, width, height):
                continue
            other = other_row * width + other_col
            if _number(_value(grid["water"], other, 0)) <= 0:
                continue
            ascent = max(
                0.0,
                _number(_value(grid["terrain"], other, 0))
                - _number(_value(grid["terrain"], index, 0)),
            )
            budget = remaining[index] - 1.0 - ascent * step_falloff
            if budget < 0 or budget <= remaining[other]:
                continue
            remaining[other] = budget
            queue.append(other)
    return [budget >= 0 for budget in remaining]


def deep_clean_water_edges(arrays, min_depth=1, max_depth=2):
    """Return land orthogonally adjacent to safe clean water in the depth gate."""
    grid = _arrays(arrays)
    width, height = grid["width"], grid["height"]
    badwater = badwater_reach_mask(grid)
    result = [False] * (width * height)
    for row in range(height):
        for col in range(width):
            index = row * width + col
            if (
                _number(_value(grid["water"], index, 0)) > 0
                or _number(_value(grid["contamination"], index, 0)) > 0
            ):
                continue
            for dcol, drow in DIRECTIONS:
                other_col, other_row = col + dcol, row + drow
                if not _inside(other_col, other_row, width, height):
                    continue
                other = other_row * width + other_col
                depth = _number(_value(grid["water"], other, 0))
                if (
                    min_depth <= depth <= max_depth
                    and _number(_value(grid["contamination"], other, 0)) <= 0
                    and not badwater[other]
                ):
                    result[index] = True
                    break
    return result


def argmax_tile(score, width, height, allowed_mask):
    """Return the highest-scoring allowed ``(col, row)`` in row-major ties."""
    _grid_size(width, height)
    best_index = None
    best_score = None
    for index, value in enumerate(score[: width * height]):
        if not _value(allowed_mask, index, False):
            continue
        if best_index is None or value > best_score:
            best_index, best_score = index, value
    if best_index is None:
        return None
    return best_index % width, best_index // width


def top_k(score, width, height, allowed_mask, k):
    """Return up to k scored allowed cells using ``heapq.nlargest``."""
    _grid_size(width, height)
    if k <= 0:
        return []
    candidates = (
        (value, (index % width, index // width))
        for index, value in enumerate(score[: width * height])
        if _value(allowed_mask, index, False)
    )
    return heapq.nlargest(k, candidates)


def colrow_to_xy(point, arrays):
    """Convert internal ``(col, row)`` to bridge ``(x, y)`` using map origin."""
    grid = _arrays(arrays)
    return grid["origin_x"] + point[0], grid["origin_y"] + point[1]


def xy_to_colrow(point, arrays):
    """Convert bridge ``(x, y)`` to internal ``(col, row)`` using map origin."""
    grid = _arrays(arrays)
    return point[0] - grid["origin_x"], point[1] - grid["origin_y"]


def _arrays(arrays):
    """Normalize raw /map data or planner-style arrays without copying grids."""
    if not isinstance(arrays, dict):
        raise ValueError("arrays must be a map dictionary")
    width = int(arrays.get("width", 0))
    height = int(arrays.get("height", 0))
    _grid_size(width, height)
    origin = arrays.get("origin") or {}
    moist = arrays.get("moist")
    if moist is None:
        moist = arrays.get("moisture")
    return {
        "origin_x": int(arrays.get("origin_x", origin.get("x", 0))),
        "origin_y": int(arrays.get("origin_y", origin.get("z", origin.get("y", 0)))),
        "width": width,
        "height": height,
        "terrain": arrays.get("terrain", arrays.get("terrain_height", [])) or [],
        "water": arrays.get("water", arrays.get("water_depth", [])) or [],
        "contamination": arrays.get("contamination") or [],
        "moist": moist or [],
        "occupied": arrays.get("occupied") or [],
        "reachable": arrays.get("reachable") or [],
        "on_road": arrays.get("on_road") or [],
    }


def _source_points(sources, width, height):
    values = list(sources or [])
    total = width * height
    is_mask = len(values) == total and all(
        not isinstance(value, (tuple, list, dict)) for value in values
    )
    if is_mask:
        return [(index % width, index // width) for index, value in enumerate(values) if value]
    result = []
    for point in values:
        if isinstance(point, dict):
            col, row = point.get("col"), point.get("row")
        else:
            col, row = point[0], point[1]
        if _inside(col, row, width, height):
            result.append((col, row))
    return result


def _grid_size(width, height):
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        raise ValueError("width and height must be positive integers")
    return width * height


def _inside(col, row, width, height):
    return 0 <= col < width and 0 <= row < height


def _value(values, index, default):
    if values is None:
        return default
    try:
        return values[index]
    except (IndexError, KeyError, TypeError):
        return default


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _point_bucket(point, bucket_size):
    return math.floor(point[0] / bucket_size), math.floor(point[1] / bucket_size)
