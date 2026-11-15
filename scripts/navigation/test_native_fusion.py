"""Native-map fusion must confirm, not invent, and must keep doubts visible."""

import unittest
from pathlib import Path
import tempfile

import numpy as np

from scripts.navigation.permap import Container, Section
from scripts.navigation.native_fusion import FLOOR_CLASS, candidate_container, cycle_grids, encode_rle, fuse_aligned, warp_classes, write_candidate


def room(wall_column=None):
    grid = np.ones((7, 9), dtype=np.uint8)
    grid[0], grid[-1], grid[:, 0], grid[:, -1] = 2, 2, 2, 2
    if wall_column is not None:
        grid[2:5, wall_column] = 2
    return grid


class NativeFusionTests(unittest.TestCase):
    def test_candidate_writer_preserves_source_and_existing_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "original.bin"
            source.write_bytes(b"original evidence")
            with self.assertRaisesRegex(ValueError, "source map"):
                write_candidate(source, {}, source)
            target = Path(folder) / "candidate.bin"
            for existing in (target, Path(str(target) + ".json")):
                existing.write_bytes(b"previous evidence")
                with self.assertRaisesRegex(ValueError, "existing artifacts"):
                    write_candidate(source, {}, target)
                self.assertEqual(existing.read_bytes(), b"previous evidence")
                existing.unlink()
            self.assertEqual(source.read_bytes(), b"original evidence")

    def test_agreement_is_stable_and_single_maps_stay_provisional(self):
        labels = fuse_aligned([room(), room()])["labels"]
        self.assertTrue(np.array_equal(labels, room()))
        alone = fuse_aligned([room()])["labels"]
        self.assertEqual(set(np.unique(alone)), {4, 5})
        self.assertTrue(np.array_equal(FLOOR_CLASS[alone], room()))

    def test_unknown_reference_cells_are_filled_from_other_maps(self):
        partial = room()
        partial[:, 5:] = 0
        fused = fuse_aligned([partial, room(), room()])
        self.assertEqual(fused["labels"][3, 6], 1)
        self.assertEqual(fused["labels"][3, 8], 2)
        self.assertEqual(fused["first_source"][3, 6], 1)
        self.assertEqual(fused["observations"][3, 6], 2)

    def test_obstacle_seen_once_among_several_maps_is_changing(self):
        labels = fuse_aligned([room(4), room(), room()])["labels"]
        self.assertEqual(labels[3, 4], 3)
        labels = fuse_aligned([room(), room(4), room(4)])["labels"]
        self.assertEqual(labels[3, 4], 3)
        self.assertEqual(labels[3, 2], 1)

    def test_one_cell_wall_jitter_is_not_a_conflict(self):
        labels = fuse_aligned([room(4), room(5)])["labels"]
        self.assertEqual(labels[3, 4], 2)
        # The shifted wall is not a dispute; that cell is free on one map's word.
        self.assertEqual(labels[3, 5], 4)

    def test_unobserved_stays_unobserved_and_input_is_validated(self):
        empty = np.zeros((3, 3), dtype=np.uint8)
        self.assertFalse(fuse_aligned([empty, empty])["labels"].any())
        with self.assertRaises(ValueError):
            fuse_aligned([np.full((3, 3), 7)])
        with self.assertRaises(ValueError):
            fuse_aligned([])

    def test_cycle_grids_restore_native_row_order_and_reject_bad_sizes(self):
        run = {"id": "r1", "ended_at": "2026-11-12T13:23:29Z", "width": 2, "height": 2,
               "floor_rle": [[2, 2], [1, 1], [0, 1]]}
        ((name, day, grid),) = cycle_grids({"runs": [run, {"id": "no-map", "ended_at": "2026-11-12T00:00:00Z"}]})
        self.assertEqual((name, day), ("r1", "2026-11-12"))
        self.assertEqual(grid.tolist(), [[1, 0], [2, 2]])
        with self.assertRaises(ValueError):
            list(cycle_grids({"runs": [{**run, "width": 3}]}))

    def test_strict_candidate_uses_exact_votes_and_preserves_raw_uncertainty(self):
        grid = bytearray([128]) * 1_000_000
        grid[:8] = bytes((0, 255, 60, 127, 129, 220, 128, 128))
        reference = Container(b"\x01\x02\x03\x04", (Section(0, b"MAPS-meta"),
                              Section(2, bytes(grid)), Section(4, b"runs")))
        labels = np.ones((1, 8), dtype=np.uint8)
        evidence = {"exact_free_votes": np.array([[2, 2, 2, 2, 2, 2, 2, 0]]),
                    "exact_wall_votes": np.array([[0, 0, 0, 0, 0, 0, 0, 2]]),
                    "near_wall_votes": np.array([[0, 0, 0, 0, 0, 0, 0, 2]])}
        candidate, changes = candidate_container(reference, labels, (0, 0), evidence=evidence)
        self.assertEqual(candidate.sections[1].payload[:8], bytes((0, 255, 60, 127, 129, 220, 0, 255)))
        self.assertEqual(changes, {"unknown_to_free": 1, "unknown_to_wall": 1})
        self.assertEqual((candidate.sections[0], candidate.sections[2]),
                         (reference.sections[0], reference.sections[2]))
        self.assertEqual(len(candidate.serialize()), len(reference.serialize()))
        with self.assertRaises(ValueError):
            candidate_container(reference, labels, (0, 0))
        with self.assertRaises(ValueError):
            candidate_container(reference, labels, (0, 0), evidence=evidence, clear_changing=True)
        for origin in ((-1, 0), (999, 0), (0, -1), (0.5, 0), (True, 0)):
            with self.assertRaises(ValueError):
                candidate_container(reference, labels, origin, evidence=evidence)
        with self.assertRaises(ValueError):
            candidate_container(reference, labels, (0, 0), evidence={})

    def test_strict_candidate_rejects_dilated_support_and_opposing_votes(self):
        reference = Container(b"1234", (Section(0, b"MAPS-meta"),
                              Section(2, bytes([128]) * 1_000_000)))
        # Display labels alone would accept all five cells. Exact evidence rejects
        # one-wall-plus-neighbour, opposite votes, and a wall beside proposed free.
        labels = np.array([[2, 1, 2, 1, 2]], dtype=np.uint8)
        evidence = {"exact_free_votes": np.array([[0, 3, 1, 2, 0]]),
                    "exact_wall_votes": np.array([[1, 1, 3, 0, 2]]),
                    "near_wall_votes": np.array([[2, 1, 3, 1, 2]])}
        candidate, changes = candidate_container(reference, labels, (0, 0), evidence=evidence)
        self.assertEqual(candidate.sections[1].payload[:5], bytes((128, 128, 128, 128, 255)))
        self.assertEqual(changes, {"unknown_to_free": 0, "unknown_to_wall": 1})

    def test_exact_vote_evidence_distinguishes_jitter_from_two_observations(self):
        left, right = np.zeros((5, 5), dtype=np.uint8), np.zeros((5, 5), dtype=np.uint8)
        left[2, 2], right[2, 3] = 2, 2
        result = fuse_aligned([left, right])
        self.assertEqual(result["labels"][2, 2], 2)
        self.assertEqual(result["exact_wall_votes"][2, 2], 1)
        self.assertEqual(result["near_wall_votes"][2, 2], 2)

    def test_warp_applies_xy_translation_and_rle_round_trips(self):
        grid = np.zeros((4, 5), dtype=np.uint8)
        grid[1, 1] = 2
        matrix = [[1, 0, 2], [0, 1, 1], [0, 0, 1]]
        warped = warp_classes(grid, matrix, grid.shape)
        self.assertEqual(warped[2, 3], 2)
        self.assertEqual(int(warped.sum()), 2)
        self.assertEqual(encode_rle(np.array([[0, 0], [2, 2]])), [[0, 2], [2, 2]])


if __name__ == "__main__":
    unittest.main()
