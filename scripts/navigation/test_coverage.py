"""Synthetic evidence tests; no network, robot, or Home Assistant interaction."""

import unittest

import numpy as np

from coverage import analyze_coverage


NOW = "2026-11-14T18:00:00Z"


def run(identifier, mask, *, status="complete", date=NOW, semantics="cleaned", confidence=0.95):
    return {"id": identifier, "status": status, "ended_at": date,
            "registration": {"accepted": True, "confidence": confidence},
            "coverage_mask": np.asarray(mask, dtype=bool), "coverage_semantics": semantics}


class CoverageTests(unittest.TestCase):
    def analyze(self, runs, zones=None, shape=(2, 4), **kwargs):
        return analyze_coverage(runs, zones, shape, now=NOW, **kwargs)

    def test_traversed_map_never_confirms_cleaning(self):
        zone = self.analyze([run("path", np.ones((2, 4)), semantics="traversed")])["zones"][0]
        self.assertEqual(zone["state"], "traversed")
        self.assertEqual(zone["traversed_fraction"], 1)
        self.assertIsNone(zone["cleaned_fraction"])
        self.assertIsNone(zone["last_confirmed_complete_at"])
        self.assertIsNone(zone["suggested_priority"])

    def test_free_space_does_not_count(self):
        result = self.analyze([run("floor", np.ones((2, 4)), semantics="free_space")])
        self.assertEqual(result["zones"][0]["state"], "unknown")
        self.assertEqual(result["rejected_runs"][0]["reason"], "mask_semantics_unverified")

    def test_unreliable_registration_does_not_count(self):
        result = self.analyze([run("weak", np.ones((2, 4)), confidence=0.4)])
        self.assertEqual(result["zones"][0]["state"], "unknown")
        self.assertEqual(result["rejected_runs"][0]["reason"], "registration_unreliable")

    def test_canceled_full_coverage_remains_partial(self):
        zone = self.analyze([run("cancel", np.ones((2, 4)), status="canceled")])["zones"][0]
        self.assertEqual(zone["state"], "partial")
        self.assertEqual(zone["cleaned_fraction"], 1)
        self.assertIsNone(zone["last_confirmed_complete_at"])

    def test_canceled_traversal_has_no_cleaned_fraction(self):
        zone = self.analyze([run("cancel", np.ones((2, 4)), status="canceled", semantics="traversed")])["zones"][0]
        self.assertEqual(zone["state"], "partial")
        self.assertIsNone(zone["cleaned_fraction"])

    def test_multiple_days_union_and_expiry_without_double_count(self):
        left = [[1, 1, 0, 0], [1, 1, 0, 0]]
        right = [[0, 0, 1, 1], [0, 0, 1, 1]]
        result = self.analyze([run("old", left, date="2026-11-01T18:00:00Z"),
                               run("new", right), run("overlap", right)])
        zone = result["zones"][0]
        self.assertEqual(zone["cleaned_fraction"], 1)
        self.assertEqual(zone["recent_cleaned_fraction"], 0.5)
        self.assertEqual(zone["suggested_priority"], 0.5)
        self.assertIsNone(zone["last_confirmed_complete_at"])

    def test_named_polygons_do_not_credit_neighbor(self):
        zones = [{"id": "left", "name": "Kitchen", "polygon": [[0, 0], [2, 0], [2, 2], [0, 2]]},
                 {"id": "right", "name": "Hall", "polygon": [[2, 0], [4, 0], [4, 2], [2, 2]]}]
        result = self.analyze([run("left", [[1, 1, 0, 0], [1, 1, 0, 0]])], zones)
        self.assertEqual(result["zones"][0]["state"], "confirmed")
        self.assertEqual(result["zones"][1]["state"], "unknown")
        self.assertIsNone(result["zones"][1]["suggested_priority"])

    def test_unnamed_extent_does_not_invent_rooms(self):
        result = self.analyze([])
        self.assertFalse(result["rooms_inferred"])
        self.assertIsNone(result["zones"][0]["name"])

    def test_future_and_live_run_not_accepted(self):
        result = self.analyze([run("future", np.ones((2, 4)), date="2026-11-15T18:00:00Z"),
                               run("live", np.ones((2, 4)), status="running")])
        self.assertEqual([r["reason"] for r in result["rejected_runs"]], ["future_timestamp", "run_not_final"])

    def test_trajectory_requires_cleaning_and_continuity_proof(self):
        item = run("trajectory", np.ones((2, 4)))
        item.pop("coverage_mask")
        item.update(trajectory=[[0.5, 0.5], [3.5, 0.5]], brush_radius_cells=0.6)
        result = self.analyze([item])
        self.assertEqual(result["rejected_runs"][0]["reason"], "trajectory_cleaning_unverified")
        item["trajectory_semantics"] = "cleaning"
        result = self.analyze([item])
        self.assertEqual(result["rejected_runs"][0]["reason"], "trajectory_continuity_unverified")
        item["trajectory_continuous"] = True
        self.assertEqual(self.analyze([item])["zones"][0]["cleaned_fraction"], 0.5)

    def test_duplicate_run_rejected(self):
        item = run("same", np.ones((2, 4)))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.analyze([item, item])

    def test_numeric_occupancy_mask_cannot_be_silently_coerced(self):
        item = run("map", np.ones((2, 4)))
        item["coverage_mask"] = np.full((2, 4), 128)
        with self.assertRaisesRegex(ValueError, "boolean"):
            self.analyze([item])

    def test_shape_mismatch_rejected(self):
        with self.assertRaisesRegex(ValueError, "reference_shape"):
            self.analyze([run("map", np.ones((4, 4)))])

    def test_outside_polygon_rejected(self):
        with self.assertRaisesRegex(ValueError, "no reference"):
            self.analyze([], [{"id": "outside", "polygon": [[10, 10], [12, 10], [12, 12]]}])


if __name__ == "__main__":
    unittest.main()
