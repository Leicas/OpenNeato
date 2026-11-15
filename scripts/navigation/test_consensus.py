import unittest

import numpy as np

from scripts.navigation.consensus import build_consensus


def observation(day, grid, quality=1):
    return {"date": f"2026-01-{day:02d}", "grid": grid, "quality": quality}


class ConsensusTests(unittest.TestCase):
    def test_mobile_obstacle_not_stable_wall_but_repeated_wall_is(self):
        result = build_consensus([[0, 0]], [
            observation(1, [[2, 2]]),
            observation(2, [[1, 2]]),
            observation(3, [[1, 2]]),
        ])
        self.assertEqual(result["stable_wall"].tolist(), [[False, True]])
        self.assertEqual(result["uncertain"].tolist(), [[True, False]])
        self.assertEqual(result["conflict"].tolist(), [[True, False]])

    def test_unknown_and_reference_abstain(self):
        result = build_consensus([[2, 2]], [observation(1, [[0, 1]]), observation(2, [[0, 1]])])
        self.assertEqual(result["unknown"].tolist(), [[True, False]])
        self.assertEqual(result["stable_free"].tolist(), [[False, True]])
        self.assertEqual(result["observation_days"].tolist(), [[0, 2]])

    def test_same_day_repetition_does_not_add_independence_or_weight(self):
        observations = [observation(1, [[2]])] * 20 + [observation(2, [[1]])]
        result = build_consensus([[0]], observations)
        self.assertEqual(result["observation_days"].item(), 2)
        self.assertAlmostEqual(result["wall_probability"].item(), 0.5)
        self.assertFalse(result["stable_wall"].item())
        self.assertFalse(result["stable_free"].item())

    def test_balanced_evidence_is_conflict_not_stable(self):
        result = build_consensus([[0]], [
            observation(1, [[2]]), observation(2, [[2]]),
            observation(3, [[1]]), observation(4, [[1]]),
        ])
        self.assertTrue(result["balanced_conflict"].item())
        self.assertTrue(result["uncertain"].item())
        self.assertEqual(result["recent_wall_days"].item(), 1)
        self.assertEqual(result["recent_free_days"].item(), 2)

    def test_same_day_conflict_retained_and_duplicate_inert(self):
        observations = [observation(1, [[2]])] * 100 + [observation(1, [[1]])]
        result = build_consensus([[0]], observations)
        self.assertAlmostEqual(result["wall_probability"].item(), 0.5)
        self.assertEqual(result["observation_days"].item(), 1)
        self.assertTrue(result["conflict"].item())

    def test_weak_second_date_cannot_make_balanced_map_stable(self):
        result = build_consensus([[0]], [
            observation(1, [[2]]), observation(2, [[2]], quality=0.1), observation(3, [[1]]),
        ])
        self.assertTrue(result["uncertain"].item())
        self.assertFalse(result["stable_wall"].item())

    def test_invalid_quality_shape_and_date_rejected(self):
        for item in [observation(1, [[2]], quality=float("nan")),
                     observation(1, [[2]], quality=2), observation(1, [[2, 2]]),
                     {"grid": [[2]], "date": "20260101", "quality": 1}]:
            with self.subTest(item=item), self.assertRaises(ValueError):
                build_consensus([[0]], [item])


if __name__ == "__main__":
    unittest.main()
