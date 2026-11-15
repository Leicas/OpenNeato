import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from scripts.navigation.render import _draw_map, map_layers, render


def fixture(accepted=True):
    reference = {"id": "ref", "width": 3, "height": 2, "floor_rle": [[2, 1], [1, 5]]}
    run = {"id": "run", "ended_at": "2026-01-02T12:00:00Z", "status": "canceled", "category": 2,
           "width": 3, "height": 2, "floor_rle": [[1, 5], [2, 1]], "run_rle": [[3, 1], [128, 5]]}
    record = {"id": "run", "coverage_eligible": accepted, "registration": {
        "accepted": accepted, "confidence": 0.9, "transform": {"matrix3x3": np.eye(3).tolist()}}}
    return {"reference": reference, "runs": [run]}, {"runs": [record]}


class RenderTests(unittest.TestCase):
    def test_small_map_is_enlarged_to_fill_available_area(self):
        floor = np.ones((40, 60), dtype=np.uint8)
        image = _draw_map(floor, np.zeros_like(floor, dtype=bool), np.full(floor.shape, np.nan), (600, 400))
        self.assertEqual(image.size, (600, 400))

    def test_eink_uses_distinct_solid_grays_without_hatching(self):
        floor = np.ones((3, 4), dtype=np.uint8)
        latest = np.zeros(floor.shape, dtype=bool)
        latest[1, 1] = True
        ages = np.full(floor.shape, 2.0)
        image = _draw_map(floor, latest, ages, (40, 30))
        self.assertEqual(image.mode, "L")
        self.assertEqual(image.getpixel((15, 15)), 146)
        self.assertEqual(image.getpixel((25, 15)), 219)

    def test_rejected_registration_uses_source_not_reference(self):
        dataset, analysis = fixture(False)
        floor, trace, _, _, _, accepted = map_layers(dataset, analysis)
        self.assertFalse(accepted)
        self.assertEqual(floor.tolist(), [[1, 1, 1], [1, 1, 2]])
        self.assertEqual(trace.tolist(), [[True, False, False], [False, False, False]])

    def test_accepted_registration_warps_actual_trace(self):
        dataset, analysis = fixture()
        analysis["runs"][0]["registration"]["transform"]["matrix3x3"][0][2] = 1
        floor, trace, _, _, _, accepted = map_layers(dataset, analysis)
        self.assertTrue(accepted)
        self.assertEqual(floor[0, 0], 2)
        self.assertEqual(trace.tolist(), [[False, True, False], [False, False, False]])

    def test_writes_native_card_sizes_and_valid_pngs(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = render(*fixture(), temporary)
            with Image.open(result["eink"]) as image:
                self.assertEqual(image.size, (680, 300))
                self.assertEqual(image.mode, "L")
            with Image.open(result["latest"]) as image:
                self.assertEqual(image.size, (1200, 800))
                self.assertEqual(image.mode, "RGB")
            self.assertFalse(list(Path(temporary).glob("*.tmp")))

    def test_compact_eink_card_keeps_requested_height(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = render(*fixture(), temporary, eink_size=(680, 220))
            with Image.open(result["eink"]) as image:
                self.assertEqual(image.size, (680, 220))


if __name__ == "__main__":
    unittest.main()
