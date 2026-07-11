import unittest

from agent import placement


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


class PlacementTests(unittest.TestCase):
    def test_water_pump_only_allows_clean_deep_safe_edge(self):
        arrays = grid(
            6,
            2,
            water_depth=[0] * 6 + [2, 0, 0.5, 2, 2, 0],
            contamination=[0] * 6 + [0, 0, 0, 0, 1, 0],
        )

        score, allowed = placement.score_for_spec(
            "WaterPump", arrays, {}, (15, 20)
        )

        self.assertEqual(allowed[:6], [True, False, False, False, False, False])
        self.assertTrue(allowed[7])  # land beside the same isolated deep water
        self.assertFalse(any(allowed[index] for index in (2, 3, 4)))
        self.assertEqual(len(score), 12)

    def test_forester_only_allows_plantable_tiles_and_prefers_large_region(self):
        arrays = grid(
            7,
            3,
            moist=[
                1, 1, 0, 0, 1, 0, 0,
                1, 1, 0, 0, 0, 0, 0,
                0, 0, 0, 0, 0, 0, 0,
            ],
            occupied=[0, 1, 0, 0, 0, 0, 0] + [0] * 14,
        )

        score, allowed = placement.score_for_spec(
            "ForesterFlag", arrays, {}, (16, 22)
        )

        self.assertTrue(allowed[0])
        self.assertFalse(allowed[1])
        self.assertTrue(allowed[4])
        self.assertFalse(allowed[2])
        self.assertGreater(score[0], score[4])

    def test_lumberjack_scores_clear_land_near_mature_tree_cluster(self):
        arrays = grid(8, 3)
        resources = {
            "trees": [
                {"x": 16, "y": 20, "mature": True},
                {"x": 17, "y": 20, "mature": True},
                {"x": 16, "y": 21, "mature": True},
            ]
        }

        score, allowed = placement.score_for_spec(
            "LumberjackFlag", arrays, resources, (10, 22)
        )

        self.assertTrue(any(allowed))
        self.assertGreater(score[6], score[0])

    def test_ranked_candidates_respect_townhall_buffer_and_coordinates(self):
        arrays = grid(9, 9)

        candidates = placement.ranked_candidates(
            "Lodge", arrays, {}, (14, 24), k=8
        )

        self.assertEqual(len(candidates), 8)
        for candidate in candidates:
            self.assertGreater(
                max(abs(candidate["x"] - 14), abs(candidate["y"] - 24)),
                placement.TOWNHALL_BUFFER,
            )
            self.assertEqual(candidate["z"], 0)
            self.assertIn("near district center", candidate["why"])

    def test_occupied_extra_blocks_tiles_and_rewards_related_adjacency(self):
        arrays = grid(8, 3)
        extras = [{"spec": "WaterPump", "x": 16, "y": 21}]

        score, allowed = placement.score_for_spec(
            "SmallTank", arrays, {}, (10, 21), occupied_extra=extras
        )

        pump_index = 1 * 8 + 6
        adjacent_index = 1 * 8 + 5
        far_index = 1 * 8 + 1
        self.assertFalse(allowed[pump_index])
        self.assertGreater(score[adjacent_index], score[far_index])


if __name__ == "__main__":
    unittest.main()
