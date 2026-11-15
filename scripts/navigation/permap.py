"""Offline reader for the observed D6 TSAM/TCES map container.

Only framing is established. Payload semantics and robot import are unknown.
The CLI reads local files and reports hashes; it has no robot/network access.
"""

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import struct


MAX_BYTES = 128 * 1024 * 1024
KNOWN_SECTION_IDS = frozenset(range(6))
GRID_WIDTH = 1000
FLOOR_LOOKUP = bytes(1 if value < 60 or value in (100, 200) else 2 if value > 220 else 0
                     for value in range(256))


@dataclass(frozen=True)
class Section:
    section_id: int
    payload: bytes


@dataclass(frozen=True)
class Container:
    field_4_raw: bytes
    sections: tuple[Section, ...]

    def serialize(self):
        if len(self.field_4_raw) != 4:
            raise ValueError("Header field must contain four raw bytes")
        body = b"".join(
            struct.pack("<4sII", b"TCES", len(section.payload), section.section_id) + section.payload
            for section in self.sections
        )
        return b"TSAM" + self.field_4_raw + struct.pack("<I", 12 + len(body)) + body


def parse(data):
    """Parse the observed framing strictly; preserve every uninterpreted byte."""
    if not isinstance(data, bytes) or not 12 <= len(data) <= MAX_BYTES:
        raise ValueError("Container size is outside the supported bound")
    if data[:4] != b"TSAM":
        raise ValueError("Missing TSAM header")
    if struct.unpack_from("<I", data, 8)[0] != len(data):
        raise ValueError("Declared length differs from actual length")
    sections = []
    seen = set()
    offset = 12
    while offset < len(data):
        if len(sections) >= 64 or offset + 12 > len(data):
            raise ValueError("Incomplete section header or too many sections")
        magic, length, section_id = struct.unpack_from("<4sII", data, offset)
        if magic != b"TCES":
            raise ValueError("Missing TCES section header")
        if section_id not in KNOWN_SECTION_IDS:
            raise ValueError("Unknown section ID; payload model not established")
        if section_id in seen:
            raise ValueError("Repeated section ID")
        end = offset + 12 + length
        if end > len(data):
            raise ValueError("Section payload exceeds container length")
        sections.append(Section(section_id, data[offset + 12:end]))
        seen.add(section_id)
        offset = end
    if not sections or sections[0].section_id != 0 or not sections[0].payload.startswith(b"MAPS"):
        raise ValueError("Expected leading MAPS section with ID 0")
    return Container(data[4:8], tuple(sections))


def inspect_file(path):
    # Bound the read itself, including if the file grows after it is opened.
    with Path(path).open("rb") as source:
        data = source.read(MAX_BYTES + 1)
    container = parse(data)
    rebuilt = container.serialize()
    if rebuilt != data:
        raise ValueError("Lossless reconstruction failed")
    return {
        "file": Path(path).name,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "roundtrip_sha256": hashlib.sha256(rebuilt).hexdigest(),
        "roundtrip_equal": True,
        "sections": [
            {"id": section.section_id, "bytes": len(section.payload),
             "sha256": hashlib.sha256(section.payload).hexdigest()}
            for section in container.sections
        ],
        "semantics": "framing only; geometry, payload meaning and robot import unverified",
    }


def floor_rows(container, width, height):
    """Reproduce Vacuula's observed FloorMap classes in a supplied image extent.

    Proven pixel-exact for acquired map 14 versus the active 438x328 FloorMap.
    The native file does not yet supply a decoded extent: callers must provide
    one independently. Rows are flipped within that extent, not the whole grid.
    No metric resolution, room labels or cleaning status is inferred.
    """
    if type(width) is not int or type(height) is not int or not (
        0 < width <= GRID_WIDTH and 0 < height <= GRID_WIDTH
    ):
        raise ValueError("Explicit image extent must fit the observed 1000x1000 grid")
    payloads = [section.payload for section in container.sections if section.section_id == 2]
    if len(payloads) != 1 or len(payloads[0]) != GRID_WIDTH * GRID_WIDTH:
        raise ValueError("Expected one million-byte ID2 section")
    grid = payloads[0]
    return tuple(grid[row * GRID_WIDTH:row * GRID_WIDTH + width].translate(FLOOR_LOOKUP)
                 for row in range(height - 1, -1, -1))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs="+")
    args = parser.parse_args()
    try:
        reports = [inspect_file(path) for path in args.inputs]
    except (OSError, ValueError) as error:
        parser.exit(1, f"Inspection failed: {error}\n")
    print(json.dumps(reports, indent=2))


if __name__ == "__main__":
    main()
