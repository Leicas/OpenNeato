"""Input integrity and coordinate convention tests for the report pipeline."""

import unittest

import numpy as np

from scripts.navigation.analyze import decode_rle, deduplicate_observations, encode_rle, last_passages, warp_mask


class ReportTests(unittest.TestCase):
    def test_reference_and_reused_map_do_not_strengthen_consensus(self):
        ref = np.array([[2, 2], [1, 1]])
        other = np.array([[2, 1], [1, 1]])
        items = [{"id": "ref-copy", "grid": ref.copy()}, {"id": "new", "grid": other},
                 {"id": "new-copy", "grid": other.copy()}]
        retained, excluded = deduplicate_observations(ref, items)
        self.assertEqual([item["id"] for item in retained], ["new"])
        self.assertEqual(excluded, ["ref-copy", "new-copy"])

    def test_consensus_export_round_trip_preserves_unknown_and_conflict(self):
        grid = np.array([[0, 0, 1], [2, 3, 3]], dtype=np.uint8)
        np.testing.assert_array_equal(decode_rle(encode_rle(grid), 3, 2), grid)

    def test_multiday_map_excludes_evidence_rejected_by_coverage(self):
        mask = np.ones((2, 2), dtype=bool)
        records = [{"id": "future", "ended_at": "2099-01-01T00:00:00Z", "coverage_mask": mask}]
        grid, latest = last_passages(records, (2, 2), set())
        self.assertIsNone(latest)
        self.assertTrue(np.isnan(grid).all())

    def test_translation_uses_columns_for_x_and_rows_for_y(self):
        mask = np.zeros((8, 9), dtype=bool)
        mask[2, 3] = True
        matrix = [[1, 0, 2], [0, 1, 3], [0, 0, 1]]
        result = warp_mask(mask, matrix, (12, 12))
        self.assertEqual(np.argwhere(result).tolist(), [[5, 5]])

    def test_rotation_uses_source_to_reference_transform(self):
        mask = np.zeros((6, 6), dtype=bool)
        mask[1, 3] = True
        matrix = [[0, -1, 5], [1, 0, 0], [0, 0, 1]]
        self.assertEqual(np.argwhere(warp_mask(mask, matrix, (6, 6))).tolist(), [[3, 4]])

    def test_corrupt_or_oversized_grid_rejected(self):
        for rle, width, height in [([[2, 3]], 2, 2), ([[2, -4]], 2, 2), ([[0, 1]], 99999, 99999)]:
            with self.assertRaises(ValueError):
                decode_rle(rle, width, height)


if __name__ == "__main__":
    unittest.main()
