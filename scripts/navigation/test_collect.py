"""Synthetic collector tests: validation and read-only route contract."""

import unittest
from unittest.mock import patch

from scripts.navigation.collect import collect, decode_rle, normalise_grid


class CollectTests(unittest.TestCase):
    def test_rejects_truncated_or_excessive_rle(self):
        for rle in ([[2, 3]], [[2, 5]], [[2, -1]], [[2, True]], [[256, 4]]):
            with self.subTest(rle=rle), self.assertRaises(ValueError):
                decode_rle(rle, 2, 2)

    def test_preserves_row_order(self):
        self.assertEqual(decode_rle([[0, 1], [2, 2], [1, 1]], 2, 2), [[0, 2], [2, 1]])

    def test_rejects_misaligned_maps(self):
        floor = {"width": 2, "height": 2, "rle": [[1, 4]]}
        path = {"width": 4, "height": 1, "rle": [[3, 4]]}
        with self.assertRaisesRegex(ValueError, "dimensions differ"):
            normalise_grid(floor, path)

    def test_collects_only_read_routes_and_keeps_failed_cycle(self):
        floor = {"width": 2, "height": 2, "rle": [[2, 1], [1, 3]]}
        path = {"width": 2, "height": 2, "rle": [[3, 2], [128, 2]]}
        routes = {
            "/api/robots": {"example-robot": {}},
            "/api/robot/example-robot/maps": [{"id": "reference", "enabled": True, "in_maps_list": True}],
            "/api/robot/example-robot/maps/reference": floor,
            "/api/robot/example-robot/runs": [
                {"id": "2026-01-01T12:00:00Z", "run": {"status": "canceled", "category": 2}},
                {"id": "missing", "run": {"status": "incomplete", "category": 4}},
            ],
            "/api/robot/example-robot/maps/2026-01-01T12:00:00Z": floor,
            "/api/robot/example-robot/run/2026-01-01T12:00:00Z": path,
        }
        visited = []

        def get(_base, route):
            visited.append(route)
            if route not in routes:
                raise OSError("not found")
            return routes[route]

        with patch("scripts.navigation.collect.read_json", side_effect=get):
            data = collect("http://example.invalid")
        self.assertEqual(len(data["runs"]), 1)
        self.assertEqual(data["runs"][0]["status"], "canceled")
        self.assertEqual(data["runs"][0]["coverage_semantics"], "traversed")
        self.assertIsNone(data["runs"][0]["reference_map_id"])
        self.assertEqual(data["collection_errors"][0]["id"], "missing")
        self.assertTrue(all("/command" not in route and "/connect" not in route for route in visited))


if __name__ == "__main__":
    unittest.main()
