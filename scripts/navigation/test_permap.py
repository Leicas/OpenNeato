"""Malformed native containers must never be accepted as valid snapshots."""

import struct
import unittest

from scripts.navigation.permap import Container, FLOOR_LOOKUP, Section, floor_rows, parse


def fixture(sections=((0, b"MAPS\x00\xff"), (3, b"\xff\x00\x80"))):
    body = b"".join(struct.pack("<4sII", b"TCES", len(payload), section_id) + payload
                    for section_id, payload in sections)
    return b"TSAM\xff\x80\x01\x00" + struct.pack("<I", 12 + len(body)) + body


class PermapTests(unittest.TestCase):
    def test_floor_classifier_thresholds_and_special_values(self):
        values = bytes((0, 59, 60, 99, 100, 101, 128, 199, 200, 201, 220, 221, 255))
        self.assertEqual(values.translate(FLOOR_LOOKUP), bytes((1, 1, 0, 0, 1, 0, 0, 0, 1, 0, 0, 2, 2)))

    def test_floor_extent_is_cropped_before_row_flip(self):
        grid = bytearray([128]) * 1_000_000
        grid[:3] = bytes((0, 255, 128))
        grid[1000:1003] = bytes((255, 128, 0))
        container = Container(b"\x00" * 4, (Section(2, bytes(grid)),))
        self.assertEqual(floor_rows(container, 3, 2), (bytes((2, 0, 1)), bytes((1, 2, 0))))

    def test_floor_refuses_missing_grid_or_unsupplied_extent(self):
        container = Container(b"\x00" * 4, (Section(2, b"short"),))
        for width, height in ((0, 1), (1001, 1), (1, True), (1, 1)):
            with self.subTest(width=width, height=height), self.assertRaises(ValueError):
                floor_rows(container, width, height)

    def test_preserves_unknown_header_bytes_and_binary_payloads(self):
        original = fixture()
        self.assertEqual(parse(original).serialize(), original)

    def test_rejects_truncation_and_trailing_bytes(self):
        original = fixture()
        for broken in (original[:11], original[:-1], original + b"\x00"):
            with self.subTest(size=len(broken)), self.assertRaises(ValueError):
                parse(broken)

    def test_rejects_payload_length_overrun_even_with_valid_total(self):
        broken = bytearray(fixture())
        struct.pack_into("<I", broken, 16, 0xFFFFFFFF)
        with self.assertRaisesRegex(ValueError, "exceeds"):
            parse(bytes(broken))

    def test_rejects_duplicates_unknown_ids_and_missing_metadata(self):
        cases = (((0, b"MAPS"), (0, b"MAPS")), ((0, b"MAPS"), (99, b"")),
                 ((3, b"MAPS"),), ((0, b"not metadata"),))
        for sections in cases:
            with self.subTest(sections=sections), self.assertRaises(ValueError):
                parse(fixture(sections))

    def test_rejects_bad_section_magic_and_partial_header(self):
        broken = bytearray(fixture())
        broken[12:16] = b"NOPE"
        partial = fixture() + b"TCES"
        partial = partial[:8] + struct.pack("<I", len(partial)) + partial[12:]
        for data in (bytes(broken), partial):
            with self.assertRaises(ValueError):
                parse(data)


if __name__ == "__main__":
    unittest.main()
