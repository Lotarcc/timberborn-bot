"""Deterministic utility-scored building placement for Timberborn maps."""

from agent import spatial


TOWNHALL_BUFFER = 2

# Weights are intentionally public and simple: the learning loop can tune policy
# without changing the scoring algorithms. Distances are normalized influences.
PROFILES = {
    "WaterPump": {
        "dc": 3.0,
        "badwater": -8.0,
        "dc_scale": 20,
        "badwater_scale": 7,
    },
    "Forester": {
        "moist_cluster": 6.0,
        "contamination": -10.0,
        "dc": 1.5,
        "dc_scale": 30,
    },
    "EfficientFarmHouse": {
        "moist_cluster": 6.0,
        "contamination": -10.0,
        "dc": 1.5,
        "dc_scale": 30,
    },
    "LumberjackFlag": {
        "resource": 7.0,
        "dc": 2.0,
        "resource_scale": 20,
        "dc_scale": 24,
        "cluster_radius": 3,
    },
    "GathererFlag": {
        "resource": 8.0,
        "dc": 1.0,
        "resource_scale": 21,
        "resource_kind": "linear",
        "dc_scale": 30,
        "cluster_radius": 3,
    },
    "SmallTank": {
        "dc": 4.0,
        "adjacency": 5.0,
        "dc_scale": 20,
        "adjacency_scale": 4,
        "related": ("WaterPump", "DeepWaterPump"),
    },
    "Lodge": {
        "dc": 4.0,
        "adjacency": 3.0,
        "dc_scale": 20,
        "adjacency_scale": 4,
        "related": ("Lodge", "MiniLodge", "Campfire"),
    },
    "SmallWarehouse": {
        "dc": 3.0,
        "adjacency": 4.0,
        "dc_scale": 24,
        "adjacency_scale": 5,
        "related": (
            "LumberjackFlag",
            "Forester",
            "LumberMill",
            "GearWorkshop",
            "EfficientFarmHouse",
        ),
    },
    "Inventor": {
        "dc": 4.0,
        "adjacency": 2.0,
        "dc_scale": 20,
        "adjacency_scale": 4,
        "related": ("Lodge", "MiniLodge"),
    },
    "Path": {"dc": 1.0, "dc_scale": 30},
}


def score_for_spec(spec, arrays, resources, dc_xy, occupied_extra=None):
    """Return ``(score, allowed)`` for one spec using deterministic layers.

    Layers are normalized before their profile weights are applied. Resource
    proximity uses BFS from the densest mature-tree or ready-gatherable cluster;
    moisture desirability uses flood-filled plantable-region sizes.
    """
    if spec not in PROFILES:
        raise ValueError("unsupported placement spec: %s" % spec)
    grid = spatial._arrays(arrays)
    width, height = grid["width"], grid["height"]
    total = width * height
    profile = PROFILES[spec]
    resources = resources if isinstance(resources, dict) else {}
    extra_mask, extra_records = _occupied_extra(occupied_extra, grid)
    resource_mask = _resource_occupied(resources, grid)
    walkable = _walkable_mask(grid)
    # The DC-proximity gradient must span the whole LAND area, not the narrow
    # reachable-spill mask: the DC tile is occupied and can sit at the map-window
    # edge, so a reachable-only field gets trapped (reaches 0 tiles). Occupancy is
    # enforced later by `allowed`; here we only want a distance-to-DC gradient.
    land = _land_mask(grid)
    dc_colrow = spatial.xy_to_colrow(_xy_pair(dc_xy), grid)
    dc_distance = spatial.distance_field(
        [dc_colrow], width, height, passable=land,
        terrain=grid["terrain"], max_step=1,
    )
    dc_layer = spatial.influence(dc_distance, profile.get("dc_scale", 24))
    layers = [(dc_layer, profile.get("dc", 0.0))]

    if spec == "WaterPump":
        allowed = spatial.deep_clean_water_edges(grid)
        badwater = spatial.badwater_reach_mask(grid)
        bad_distance = spatial.distance_field(badwater, width, height)
        layers.append(
            (spatial.influence(bad_distance, profile["badwater_scale"]), profile["badwater"])
        )
    elif spec in ("Forester", "EfficientFarmHouse"):
        allowed = spatial.plantable_mask(grid)
        labels, regions = spatial.label_regions(allowed, width, height)
        sizes = {region["id"]: region["size"] for region in regions}
        region_size = [sizes.get(label, 0) for label in labels]
        contamination = [
            float(_value(grid["contamination"], index, 0)) for index in range(total)
        ]
        layers.extend(
            [
                (region_size, profile["moist_cluster"]),
                (contamination, profile["contamination"]),
            ]
        )
    elif spec == "LumberjackFlag":
        allowed = _flat_dry_mask(grid)
        mature = [
            item for item in resources.get("trees", []) or []
            if isinstance(item, dict) and item.get("mature") is True
        ]
        resource_layer, _count = _resource_layer(mature, profile, grid, walkable)
        layers.append((resource_layer, profile["resource"]))
    elif spec == "GathererFlag":
        allowed = _safe_reachable_land(grid)
        ready = [
            item for item in resources.get("gatherables", []) or []
            if isinstance(item, dict) and item.get("ready") is True
        ]
        resource_layer, _count = _resource_layer(ready, profile, grid, walkable)
        layers.append((resource_layer, profile["resource"]))
    elif spec == "Path":
        allowed = walkable[:]
    else:
        allowed = _flat_dry_mask(grid)
        related_points = _related_points(extra_records, profile.get("related", ()))
        if related_points:
            related_distance = spatial.distance_field(related_points, width, height)
            layers.append(
                (
                    spatial.influence(related_distance, profile["adjacency_scale"]),
                    profile["adjacency"],
                )
            )

    for index in range(total):
        if extra_mask[index] or resource_mask[index]:
            allowed[index] = False
        if spec != "Path" and _value(grid["on_road"], index, 0):
            allowed[index] = False
        if spec not in ("Forester", "EfficientFarmHouse", "Path"):
            if not walkable[index]:
                allowed[index] = False
        if spec == "WaterPump":
            if (
                _value(grid["occupied"], index, 0)
                or float(_value(grid["contamination"], index, 0)) > 0
            ):
                allowed[index] = False

    return spatial.stack(layers), allowed


def ranked_candidates(spec, arrays, resources, dc_xy, k=6, occupied_extra=None):
    """Return top-k world-coordinate candidates, reserving the DC approaches."""
    grid = spatial._arrays(arrays)
    score, allowed = score_for_spec(
        spec, grid, resources, dc_xy, occupied_extra=occupied_extra
    )
    dc_x, dc_y = _xy_pair(dc_xy)
    if spec != "Path":
        for row in range(grid["height"]):
            for col in range(grid["width"]):
                x, y = spatial.colrow_to_xy((col, row), grid)
                if max(abs(x - dc_x), abs(y - dc_y)) <= TOWNHALL_BUFFER:
                    allowed[row * grid["width"] + col] = False

    why = _why_for_spec(spec, resources, occupied_extra)
    result = []
    for value, (col, row) in spatial.top_k(
        score, grid["width"], grid["height"], allowed, max(int(k), 0)
    ):
        index = row * grid["width"] + col
        x, y = spatial.colrow_to_xy((col, row), grid)
        result.append(
            {
                "x": x,
                "y": y,
                "z": _value(grid["terrain"], index, 0),
                "why": why,
                "score": value,
            }
        )
    return result


def _resource_layer(items, profile, grid, walkable):
    points = []
    for item in items:
        point = spatial.xy_to_colrow(_xy_pair(item), grid)
        if 0 <= point[0] < grid["width"] and 0 <= point[1] < grid["height"]:
            points.append(point)
    groups = spatial.clusters(points, profile.get("cluster_radius", 3))
    if not groups:
        return [0.0] * (grid["width"] * grid["height"]), 0
    group = groups[0]
    centroid = (round(group["centroid"][0]), round(group["centroid"][1]))
    distances = spatial.distance_field(
        [centroid], grid["width"], grid["height"], passable=walkable,
        terrain=grid["terrain"], max_step=1,
    )
    return spatial.influence(
        distances,
        profile["resource_scale"],
        kind=profile.get("resource_kind", "decay"),
    ), group["count"]


def _walkable_mask(grid):
    total = grid["width"] * grid["height"]
    has_reachable = len(grid["reachable"]) >= total
    return [
        float(_value(grid["water"], index, 0)) <= 0
        and (not has_reachable or bool(_value(grid["reachable"], index, 0)))
        for index in range(total)
    ]


def _land_mask(grid):
    """Land tiles (no water), ignoring occupancy/reachability — used to spread the
    DC-proximity distance gradient across the whole map."""
    total = grid["width"] * grid["height"]
    return [float(_value(grid["water"], index, 0)) <= 0 for index in range(total)]


def _safe_reachable_land(grid):
    total = grid["width"] * grid["height"]
    walkable = _walkable_mask(grid)
    return [
        walkable[index]
        and not bool(_value(grid["occupied"], index, 0))
        and float(_value(grid["contamination"], index, 0)) <= 0
        for index in range(total)
    ]


def _flat_dry_mask(grid):
    width, height = grid["width"], grid["height"]
    safe = _safe_reachable_land(grid)
    result = [False] * (width * height)
    for row in range(height):
        for col in range(width):
            index = row * width + col
            if not safe[index]:
                continue
            height_here = _value(grid["terrain"], index, 0)
            same_height_neighbors = 0
            for dcol, drow in spatial.DIRECTIONS:
                other_col, other_row = col + dcol, row + drow
                if not (0 <= other_col < width and 0 <= other_row < height):
                    continue
                other = other_row * width + other_col
                if safe[other] and _value(grid["terrain"], other, 0) == height_here:
                    same_height_neighbors += 1
            result[index] = same_height_neighbors >= 2
    return result


def _occupied_extra(value, grid):
    total = grid["width"] * grid["height"]
    mask = [False] * total
    records = []
    values = list(value or [])
    if len(values) == total and all(
        not isinstance(item, (tuple, list, dict)) for item in values
    ):
        return [bool(item) for item in values], records
    for item in values:
        spec = None
        if isinstance(item, dict):
            spec = item.get("spec") or item.get("spec_id")
            point = spatial.xy_to_colrow(_xy_pair(item), grid)
        else:
            point = tuple(item[:2])
            if not (0 <= point[0] < grid["width"] and 0 <= point[1] < grid["height"]):
                point = spatial.xy_to_colrow(point, grid)
        col, row = point
        if 0 <= col < grid["width"] and 0 <= row < grid["height"]:
            mask[row * grid["width"] + col] = True
            records.append({"spec": spec, "point": (col, row)})
    return mask, records


def _resource_occupied(resources, grid):
    total = grid["width"] * grid["height"]
    mask = [False] * total
    for key in ("trees", "gatherables"):
        for item in resources.get(key, []) or []:
            if not isinstance(item, dict):
                continue
            col, row = spatial.xy_to_colrow(_xy_pair(item), grid)
            if 0 <= col < grid["width"] and 0 <= row < grid["height"]:
                mask[row * grid["width"] + col] = True
    return mask


def _related_points(records, related_specs):
    return [
        record["point"] for record in records if record["spec"] in related_specs
    ]


def _why_for_spec(spec, resources, occupied_extra):
    if spec == "WaterPump":
        return "clean water depth 1..2 outside badwater reach; near district center"
    if spec in ("Forester", "EfficientFarmHouse"):
        return "plantable moist cluster; uncontaminated; near district center"
    if spec == "LumberjackFlag":
        return "near densest mature-tree cluster; near district center"
    if spec == "GathererFlag":
        return "ready bushes within ~20 walkable tiles; near district center"
    if spec == "Path":
        return "reachable solid tile near district center"
    why = "flat dry reachable land; near district center"
    if occupied_extra:
        why += "; related-building adjacency when available"
    return why


def _xy_pair(value):
    if isinstance(value, dict):
        return value.get("x", 0), value.get("y", value.get("z", 0))
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        return value[0], value[1]
    return 0, 0


def _value(values, index, default):
    try:
        return values[index]
    except (IndexError, KeyError, TypeError):
        return default
