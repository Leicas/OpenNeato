"""Synthetic geometry checks; no robot or server changes."""

import unittest

import numpy as np
from scipy.ndimage import affine_transform

try:
    from .registration import register_grids
except ImportError:
    from registration import register_grids


def asymmetric_map():
    walls = np.zeros((140, 170), dtype=bool)
    walls[15:120, 20] = True
    walls[15, 20:145] = True
    walls[119, 20:145] = True
    walls[15:120, 144] = True
    walls[35:100, 75] = True
    walls[99, 75:110] = True
    walls[45, 100:144] = True
    return walls


def move_grid(reference, angle_degrees, tx, ty):
    angle = np.deg2rad(angle_degrees)
    rotation = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
    # Output moving pixel p samples reference R*p+t; expected moving→ref R,t.
    swap = np.array([[0, 1], [1, 0]])
    return affine_transform(reference.astype(float), swap @ rotation @ swap,
                            offset=swap @ np.array([tx, ty]), output_shape=(190, 200),
                            order=0, mode="constant", cval=0) > 0.5


class RegistrationTests(unittest.TestCase):
    def test_rotation_translation_and_mobile_obstacles(self):
        reference = asymmetric_map()
        moving = move_grid(reference, 23, 17, -24)
        moving[65:76, 45] = True
        moving[75, 45:59] = True
        result = register_grids(reference, moving, tolerance_pixels=1.8)
        self.assertTrue(result["accepted"], result)
        transform = result["transform"]
        self.assertAlmostEqual(transform["angle_degrees"], 23, delta=1.5)
        self.assertAlmostEqual(transform["tx"], 17, delta=2)
        self.assertAlmostEqual(transform["ty"], -24, delta=2)

    def test_symmetric_room_is_ambiguous(self):
        walls = np.zeros((120, 120), dtype=bool)
        walls[20:100, 20] = walls[20:100, 99] = True
        walls[20, 20:100] = walls[99, 20:100] = True
        result = register_grids(walls, walls, tolerance_pixels=1.8)
        self.assertFalse(result["accepted"], result)
        self.assertEqual(result["diagnostics"]["reason"], "ambiguous")

    def test_empty_grid_has_no_transform(self):
        result = register_grids(np.zeros((40, 40)), np.zeros((40, 40)))
        self.assertFalse(result["accepted"])
        self.assertIsNone(result["transform"])

    def test_repeated_room_rejects_translation_ambiguity(self):
        room = asymmetric_map()
        reference = np.zeros((140, 360), dtype=bool)
        reference[:, :170] = room
        reference[:, 190:] = room
        result = register_grids(reference, room, tolerance_pixels=1.8)
        self.assertFalse(result["accepted"], result)
        self.assertEqual(result["diagnostics"]["reason"], "ambiguous")


if __name__ == "__main__":
    unittest.main(verbosity=2)
