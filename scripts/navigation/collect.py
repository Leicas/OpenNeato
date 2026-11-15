"""Collect an offline navigation dataset using read-only Vacuula HTTP routes.

No robot commands are sent. Coordinates are image pixels: x right, y down.
RunMap means traversed space, not verified cleaning. Resolution is unknown.
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import urlopen


def read_json(base, path, timeout=20):
    """Use GET exclusively, with a bounded request timeout."""
    with urlopen(base.rstrip("/") + path, timeout=timeout) as response:
        return json.load(response)


def decode_rle(rle, width, height):
    """Validate and decode row-major RLE into rows without third-party packages."""
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise ValueError("Grid dimensions must be positive integers")
    if width * height > 16_000_000:
        raise ValueError("Grid exceeds the 16 million pixel collection limit")
    count = 0
    for pair in rle:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError("RLE entries must be [value, count] pairs")
        value, length = pair
        if type(value) is not int or not 0 <= value <= 255 or type(length) is not int or length <= 0:
            raise ValueError("Invalid RLE pixel value or count")
        count += length
        if count > width * height:
            raise ValueError("RLE exceeds grid dimensions")
    if count != width * height:
        raise ValueError("RLE does not fill grid dimensions")
    pixels = [value for value, length in rle for _ in range(length)]
    return [pixels[offset:offset + width] for offset in range(0, len(pixels), width)]


def normalise_grid(floor, run=None):
    width, height = floor["width"], floor["height"]
    decode_rle(floor["rle"], width, height)
    if set(value for value, _ in floor["rle"]) - {0, 1, 2}:
        raise ValueError("Unsupported FloorMap legend")
    result = {
        "width": width,
        "height": height,
        "floor_rle": floor["rle"],
        "dock": floor.get("dock"),
        "resolution_m": None,
        "downsample": floor.get("downsample", 1),
        "coordinates": "image_x_right_y_down",
        "wall_value": 2,
        "free_value": 1,
        "unknown_value": 0,
    }
    if run is not None:
        if (run["width"], run["height"]) != (width, height):
            raise ValueError("FloorMap and RunMap dimensions differ")
        decode_rle(run["rle"], width, height)
        result.update({
            "run_rle": run["rle"],
            "path_values": sorted({v for v, _ in run["rle"]} - {128, 100, 200}),
            "background_value": 128,
            "dock_values": [100, 200],
            "coverage_semantics": "traversed",
        })
    return result


def collect(base, robot=None, reference_id=None):
    """Return the schema-v1 bundle; unavailable cycles are explicit failures."""
    robots = read_json(base, "/api/robots")
    if robot is None:
        if len(robots) != 1:
            raise ValueError("Specify --robot when server does not contain exactly one robot")
        robot = next(iter(robots))
    root = "/api/robot/" + quote(robot, safe="")
    maps = read_json(base, root + "/maps")
    if reference_id is None:
        enabled = [item for item in maps if item.get("enabled") and item.get("in_maps_list")]
        if len(enabled) != 1:
            raise ValueError("Specify --reference when there is not exactly one active map")
        reference_id = enabled[0]["id"]
    # Vacuula's current router matches the raw URL without percent-decoding IDs.
    reference = normalise_grid(read_json(base, root + "/maps/" + quote(reference_id, safe=":")))
    reference["id"] = reference_id
    result = {
        "schema_version": 1,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "reference": reference,
        "runs": [],
        "collection_errors": [],
        "assumptions": [
            "FloorMap values 0=unknown, 1=free, 2=wall; RLE is row-major.",
            "RunMap records traversal, not confirmed brush or suction operation.",
            "Physical resolution and origin are unavailable; compare in pixels only.",
            "Equal pixel scale across cycles is an unverified registration assumption.",
            "Dock metadata is preserved without treating it as a reliable alignment anchor.",
        ],
        "semantics_source": "Vacuula installed gen3/map_utils.py: path parser and floor classifier",
    }
    rows = read_json(base, root + "/runs")
    for row in sorted(rows, key=lambda item: item["id"]):
        run_id = row["id"]
        meta = row["run"]
        try:
            encoded_id = quote(run_id, safe=":")
            floor = read_json(base, root + "/maps/" + encoded_id)
            run = read_json(base, root + "/run/" + encoded_id)
            item = normalise_grid(floor, run)
            item.update({
                "id": run_id,
                "reference_map_id": meta.get("persistent_map_id"),
                "started_at": meta.get("start_at"),
                "ended_at": meta.get("end_at"),
                "status": meta.get("status"),
                "category": meta.get("category"),
                "error": meta.get("error"),
                "cleaned_area_m2": meta.get("cleaned_area"),
                "time_in_error_s": meta.get("time_in_error"),
                "picked_up_count": meta.get("picked_up_count"),
                "localization_lost": meta.get("localization_lost"),
                "valid_as_persistent_map": meta.get("valid_as_persistent_map"),
            })
            result["runs"].append(item)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result["collection_errors"].append({"id": run_id, "error": type(exc).__name__ + ": " + str(exc)})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Vacuula origin, e.g. http://localhost:8080")
    parser.add_argument("--robot", help="Robot serial; inferred only when exactly one exists")
    parser.add_argument("--reference", help="Reference map id; defaults to the only active map")
    parser.add_argument("--output", type=Path, required=True, help="Private output path outside the repository")
    args = parser.parse_args()
    url = urlsplit(args.base_url)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
        parser.error("--base-url must be an HTTP(S) origin without credentials, query, or fragment")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    bundle = collect(args.base_url, args.robot, args.reference)
    args.output.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    print(f"Collected {len(bundle['runs'])} runs; {len(bundle['collection_errors'])} errors; output: {args.output}")


if __name__ == "__main__":
    main()
