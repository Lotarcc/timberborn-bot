import math
import unittest

from agent import spatial


def grid(width, height, **overrides):
    total = width * height
    data = {
        "origin": {"x": 10, "z": 20},
        "width": width,
        "height": height,
        "terrain_height": [0] * total,
        "water_depth": [0] * total,
        "contamination": [0] * total,
        "moist": [0] * total,
        "occupied": [0] * total,
        "reachable": [1] * total,
        "on_road": [0] * total,
    }
    data.update(overrides)
    return data


class SpatialPrimitiveTests(unittest.TestCase):
    def test_distance_field_respects_obstacles_and_height_steps(self):
        passable = [True] * 9
        passable[1] = False
        terrain = [0, 0, 2, 0, 0, 2, 0, 0, 2]

        distances = spatial.distance_field(
            [(0, 0)], 3, 3, passable=passable, terrain=terrain, max_step=1
        )

        self.assertEqual(distances, [0, -1, -1, 1, 2, -1, 2, 3, -1])

    def test_distance_field_accepts_boolean_source_mask(self):
        distances = spatial.distance_field(
            [False, True, True, False], 2, 2
        )

        self.assertEqual(distances, [1, 0, 0, 1])

    def test_influence_decay_is_monotonic_and_unreachable_is_zero(self):
        values = spatial.influence([0, 1, 2, -1], scale=2)

        self.assertEqual(values[-1], 0.0)
        self.assertGreater(values[0], values[1])
        self.assertGreater(values[1], values[2])
        self.assertAlmostEqual(values[1], math.exp(-0.5))

    def test_norm_and_stack_handle_flat_and_negative_layers(self):
        self.assertEqual(spatial.norm([4, 4]), [0.0, 0.0])
        self.assertEqual(
            spatial.stack([([0, 10], 2), ([5, 0], -1)]),
            [-1.0, 2.0],
        )

    def test_label_regions_reports_sizes_and_centroids(self):
        mask = [1, 0, 0, 1, 1, 0, 0, 0, 1]

        labels, regions = spatial.label_regions(mask, 3, 3, diagonal=False)

        self.assertEqual([region["size"] for region in regions], [3, 1])
        self.assertEqual(regions[0]["centroid"], (1 / 3, 2 / 3))
        self.assertEqual(set(regions[0]["cells"]), {(0, 0), (0, 1), (1, 1)})
        self.assertEqual(labels[8], regions[1]["id"])

    def test_voronoi_districts_split_passable_grid(self):
        labels = spatial.voronoi_districts(
            [(0, 0), (4, 0)], 5, 1, [True] * 5
        )

        self.assertEqual(labels, [0, 0, 0, 1, 1])

    def test_clusters_finds_densest_group(self):
        groups = spatial.clusters(
            [(0, 0), (1, 0), (1, 1), (8, 8), (9, 8)], radius=1
        )

        self.assertEqual([group["count"] for group in groups], [3, 2])
        self.assertEqual(groups[0]["centroid"], (2 / 3, 1 / 3))

    def test_coordinate_helpers_apply_map_origin(self):
        arrays = grid(3, 2)

        self.assertEqual(spatial.colrow_to_xy((2, 1), arrays), (12, 21))
        self.assertEqual(spatial.xy_to_colrow((12, 21), arrays), (2, 1))


class TimberbornMaskTests(unittest.TestCase):
    def test_plantable_mask_requires_clean_moist_empty_land(self):
        arrays = grid(
            5,
            1,
            water_depth=[0, 0, 0, 0, 1],
            contamination=[0, 0, 1, 0, 0],
            moist=[1, 1, 1, 0, 1],
            occupied=[0, 1, 0, 0, 0],
        )

        self.assertEqual(
            spatial.plantable_mask(arrays), [True, False, False, False, False]
        )

    def test_badwater_reach_spends_extra_reach_ascending(self):
        arrays = grid(
            5,
            1,
            terrain_height=[0, 0, 1, 1, 1],
            water_depth=[1, 1, 1, 1, 1],
            contamination=[1, 0, 0, 0, 0],
        )

        reached = spatial.badwater_reach_mask(arrays, max_reach=7, step_falloff=5)

        self.assertEqual(reached, [True, True, True, False, False])

    def test_deep_clean_water_edges_reject_badwater_reachable_water(self):
        # Top row is land. Bottom row has isolated deep clean water at col 0,
        # shallow water at col 2, and badwater-connected deep water at cols 3-4.
        arrays = grid(
            5,
            2,
            water_depth=[0] * 5 + [2, 0, 0.5, 2, 2],
            contamination=[0] * 5 + [0, 0, 0, 0, 1],
        )

        edges = spatial.deep_clean_water_edges(arrays)

        self.assertEqual(edges[:5], [True, False, False, False, False])
        self.assertEqual(edges[5:], [False, True, False, False, False])

    def test_argmax_and_top_k_only_consider_allowed_tiles(self):
        score = [1, 5, 3, 4]
        allowed = [True, False, True, True]

        self.assertEqual(spatial.argmax_tile(score, 2, 2, allowed), (1, 1))
        self.assertEqual(
            spatial.top_k(score, 2, 2, allowed, 2),
            [(4, (1, 1)), (3, (0, 1))],
        )


if __name__ == "__main__":
    unittest.main()
