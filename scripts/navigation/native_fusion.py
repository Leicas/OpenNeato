"""Candidate floor plan fused from several native D6 persistent maps.

Acquired ``permap`` files may share evidence; independent exploration is not proven. Those that register
rigidly onto one chosen reference map are warped into its grid and compared
cell by cell. The result is an offline candidate plan: it is never written to
the robot, never used as a registration reference, and proves no metric scale.

Coordinates are native grid cells of the reference map (x=column, y=row). The
Vacuula FloorMap of that map uses the same x and reversed rows.
"""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from scipy.ndimage import affine_transform, binary_dilation

from scripts.navigation.permap import FLOOR_LOOKUP, GRID_WIDTH, parse
from scripts.navigation.registration import register_grids

LABELS = {
    0: "unobserved",
    1: "stable_free",
    2: "persistent_obstacle",
    3: "uncertain_or_changing",
    4: "free_single_observation",
    5: "obstacle_single_observation",
}
# Collapsed 0/1/2 floor classes: a cell that is only sometimes occupied is floor.
FLOOR_CLASS = np.array([0, 1, 2, 1, 1, 2], dtype=np.uint8)
PALETTE = np.array([[203, 209, 210], [242, 244, 240], [32, 61, 69], [233, 170, 66],
                    [214, 228, 240], [110, 140, 160]], dtype=np.uint8)
FLOOR_LOOKUP_ARRAY = np.frombuffer(FLOOR_LOOKUP, dtype=np.uint8)
MINIMUM_CONFIDENCE = 0.8
_NEIGHBOURHOOD = np.ones((3, 3), dtype=bool)


def native_classes(container):
    """Return the 1000x1000 ID2 grid as 0=unknown, 1=free, 2=wall (native rows)."""
    payloads = [section.payload for section in container.sections if section.section_id == 2]
    if len(payloads) != 1 or len(payloads[0]) != GRID_WIDTH * GRID_WIDTH:
        raise ValueError("Expected one million-byte ID2 section")
    return np.frombuffer(payloads[0].translate(FLOOR_LOOKUP), dtype=np.uint8).reshape(GRID_WIDTH, GRID_WIDTH)


def warp_classes(grid, matrix, output_shape):
    """Warp a 0/1/2 grid with a moving-to-reference (x, y) matrix, nearest cell."""
    inverse = np.linalg.inv(np.asarray(matrix, dtype=float))
    swap = np.array([[0, 1], [1, 0]])
    return affine_transform(grid, swap @ inverse[:2, :2] @ swap, offset=swap @ inverse[:2, 2],
                            output_shape=output_shape, order=0, mode="constant", cval=0, prefilter=False)


def fuse_aligned(grids):
    """Fuse aligned 0/1/2 grids, listed by decreasing trust (reference first).

    The first map that observed a cell proposes its class, which keeps walls as
    thin as that map drew them. Other maps then confirm or contradict it. A wall
    counts as present within one cell, so sub-cell registration residue is not
    read as a moved obstacle. Unknown abstains. Every map has one vote: these
    are separate explorations, but their independence is not proven.
    """
    stack = np.asarray(grids)
    if stack.ndim != 3 or not stack.size or not np.isin(stack, (0, 1, 2)).all():
        raise ValueError("Expected a nonempty list of equally shaped 0/1/2 grids")
    wall = stack == 2
    free = stack == 1
    near = np.array([binary_dilation(layer, structure=_NEIGHBOURHOOD) for layer in wall])
    observed = wall | free
    first = np.argmax(observed, axis=0)
    seen = observed.any(axis=0)

    def proposer(layers):
        return np.take_along_axis(layers, first[None], axis=0)[0] & seen

    proposes_wall = proposer(wall)
    wall_support = near.sum(axis=0)
    clear_votes = (free & ~near).sum(axis=0)
    free_votes = free.sum(axis=0)
    # A wall elsewhere only contradicts free space away from the proposer's own walls.
    wall_votes = np.where(proposer(near), 0, wall.sum(axis=0))

    labels = np.zeros(stack.shape[1:], dtype=np.uint8)
    labels[proposes_wall & (wall_support >= 2) & (wall_support >= clear_votes)] = 2
    labels[proposes_wall & (wall_support == 1) & (clear_votes == 0)] = 5
    labels[proposes_wall & (labels == 0)] = 3
    proposes_free = seen & ~proposes_wall
    disputed = proposes_free & (wall_votes * 2 >= free_votes)
    labels[disputed] = 3
    labels[proposes_free & ~disputed & (free_votes >= 2)] = 1
    labels[proposes_free & ~disputed & (free_votes == 1)] = 4
    return {"labels": labels, "observations": observed.sum(axis=0).astype(np.uint8),
            "first_source": np.where(seen, first, -1).astype(np.int8),
            "exact_free_votes": free.sum(axis=0), "exact_wall_votes": wall.sum(axis=0),
            "near_wall_votes": near.sum(axis=0)}


def encode_rle(array):
    flat = np.asarray(array).ravel()
    changes = np.flatnonzero(flat[1:] != flat[:-1]) + 1
    starts, ends = np.r_[0, changes], np.r_[changes, len(flat)]
    return [[int(flat[start]), int(end - start)] for start, end in zip(starts, ends)]


def cycle_grids(dataset):
    """Yield ``(run id, UTC date, grid in native row order)`` from a collected dataset.

    Vacuula FloorMaps are stored with rows reversed relative to the native grid.
    A reflection cannot be recovered by rigid registration, so undo it here.
    """
    for run in dataset["runs"]:
        grid = run.get("grid", run)
        stamp = run.get("ended_at") or run.get("started_at") or ""
        if "floor_rle" not in grid or len(stamp) < 10:
            continue
        values = np.repeat([pair[0] for pair in grid["floor_rle"]], [pair[1] for pair in grid["floor_rle"]])
        if values.size != grid["width"] * grid["height"] or not np.isin(values, (0, 1, 2)).all():
            raise ValueError("Cycle floor grid does not match its declared size or classes")
        yield str(run["id"]), stamp[:10], values.reshape(grid["height"], grid["width"])[::-1].astype(np.uint8)


def _repeats(grid, retained, similarity):
    """True when a grid restates an already retained map (a reused persistent map)."""
    for previous in retained:
        known = (grid != 0) | (previous != 0)
        if known.any() and np.mean(grid[known] == previous[known]) >= similarity:
            return True
    return False


def fuse_permaps(reference_path, other_paths, *, dataset=None, minimum_confidence=MINIMUM_CONFIDENCE,
                 duplicate_similarity=0.995, margin=8):
    """Register other maps onto the reference native map and fuse the accepted ones.

    Candidates are the other acquired native maps and, optionally, the per-cycle
    FloorMaps of a collected Vacuula dataset. Maps from another home are
    expected among the inputs: anything the wall registration rejects, or scores
    below ``minimum_confidence``, does not vote. A cycle map that restates a map
    already retained does not vote either, and each UTC date contributes at most
    its best-registered cycle map.
    """
    reference = native_classes(parse(Path(reference_path).read_bytes()))
    sources = [{"file": Path(reference_path).name, "role": "reference", "used": True, "confidence": 1.0}]
    candidates = [(Path(path).name, "native_map", None, native_classes(parse(Path(path).read_bytes())))
                  for path in other_paths]
    candidates += [(name, "cycle_map", day, grid) for name, day, grid in cycle_grids(dataset or {"runs": []})]
    warped = {}
    for name, role, day, grid in candidates:
        rows, columns = np.nonzero(grid)
        entry = {"file": name, "role": role, "used": False, "confidence": 0.0}
        if day:
            entry["date"] = day
        sources.append(entry)
        if not len(rows):
            entry["reason"] = "empty"
            continue
        top, left = int(rows.min()), int(columns.min())
        result = register_grids(reference, grid[top:rows.max() + 1, left:columns.max() + 1])
        entry["confidence"] = result["confidence"]
        entry["reason"] = result["diagnostics"]["reason"]
        if not result["accepted"] or result["confidence"] < minimum_confidence:
            continue
        shift = np.eye(3)
        shift[:2, 2] = (-left, -top)
        matrix = np.asarray(result["transform"]["matrix3x3"]) @ shift
        entry.update(angle_degrees=result["transform"]["angle_degrees"], matrix3x3=matrix.tolist())
        warped[name] = warp_classes(grid, matrix, reference.shape)
    # Trust order: the reference, native maps by registration quality, then cycle maps.
    ranked, retained, dates = [], [reference], set()
    for entry in sorted((entry for entry in sources[1:] if entry["file"] in warped),
                        key=lambda entry: (entry["role"] != "native_map", -entry["confidence"])):
        grid = warped[entry["file"]]
        if entry["role"] == "cycle_map" and entry["date"] in dates:
            entry["reason"] = "same_date_as_better_cycle"
        elif entry["role"] == "cycle_map" and _repeats(grid, retained, duplicate_similarity):
            entry["reason"] = "repeats_retained_map"
        else:
            entry["used"] = True
            dates.add(entry.get("date"))
            retained.append(grid)
            ranked.append(entry)
    order = [sources[0]["file"]] + [entry["file"] for entry in ranked]

    fused = fuse_aligned(retained)
    rows, columns = np.nonzero(fused["labels"])
    top, bottom = max(0, rows.min() - margin), min(GRID_WIDTH, rows.max() + margin + 1)
    left, right = max(0, columns.min() - margin), min(GRID_WIDTH, columns.max() + margin + 1)
    window = (slice(top, bottom), slice(left, right))
    labels, before = fused["labels"][window], reference[window]
    floor = FLOOR_CLASS[labels]
    summary = {
        "kind": "candidate_native_fusion_not_ground_truth",
        "units": "reference_native_grid_cells",
        "origin_xy": [int(left), int(top)],
        "row_order": "native; the Vacuula FloorMap of the reference reverses rows",
        "minimum_confidence": minimum_confidence,
        "trust_order": order,
        "sources": sources,
        "label_counts": {name: int(np.count_nonzero(labels == value)) for value, name in LABELS.items()},
        "reference_known_cells": int(np.count_nonzero(before)),
        "fused_known_cells": int(np.count_nonzero(floor)),
        "cells_added_beyond_reference": int(np.count_nonzero((before == 0) & (floor != 0))),
        "reference_walls_not_confirmed": int(np.count_nonzero((before == 2) & (labels == 3))),
        "reference_free_disputed": int(np.count_nonzero((before == 1) & (labels == 3))),
        "limitations": [
            "Offline candidate only: not a native map and not importable into the robot.",
            "Each map votes once; independence between explorations is assumed, not proven.",
            "A never-moved piece of furniture remains a persistent obstacle.",
            "Single-observation cells come from one exploration and are unconfirmed.",
            "No metric scale is established; cells are native grid cells.",
        ],
    }
    return {"labels": labels, "floor": floor, "reference": before,
            "observations": fused["observations"][window], "summary": summary,
            "candidate_evidence": {key: fused[key][window] for key in
                                   ("exact_free_votes", "exact_wall_votes", "near_wall_votes")}}


def candidate_container(reference, labels, origin_xy, *, evidence=None, clear_changing=False):
    """Build an offline ID2 candidate from exact votes, never from display labels.

    Only raw 128 cells can change. Two exact same-class votes and zero opposing
    votes are required; free proposals also exclude any source wall within one
    cell. Separate source files are not proof of independent acquisition. These
    checks do not validate native SLAM consistency or permit robot import.
    """
    if clear_changing:
        raise ValueError("Clearing existing walls is unsupported by strict candidate generation")
    labels = np.asarray(labels)
    if labels.ndim != 2 or not labels.size:
        raise ValueError("Expected a nonempty two-dimensional label window")
    if (len(origin_xy) != 2 or any(isinstance(value, (bool, np.bool_)) or
            not isinstance(value, (int, np.integer)) for value in origin_xy)):
        raise ValueError("Candidate origin must contain two integer grid coordinates")
    left, top = origin_xy
    if left < 0 or top < 0 or left + labels.shape[1] > GRID_WIDTH or top + labels.shape[0] > GRID_WIDTH:
        raise ValueError("Fused labels do not fit the native grid at this origin")
    if evidence is None:
        raise ValueError("Strict candidate generation requires exact source-vote evidence")
    counts = {}
    for key in ("exact_free_votes", "exact_wall_votes", "near_wall_votes"):
        values = np.asarray(evidence.get(key))
        if values.shape != labels.shape or values.dtype.kind not in "iu" or np.any(values < 0):
            raise ValueError("Candidate evidence must contain equally shaped nonnegative integer counts")
        counts[key] = values
    free, wall, near = (counts[key] for key in
                        ("exact_free_votes", "exact_wall_votes", "near_wall_votes"))
    if np.any(near < wall):
        raise ValueError("Neighbourhood wall counts cannot be below exact wall counts")
    grid = np.frombuffer(next(s.payload for s in reference.sections if s.section_id == 2), dtype=np.uint8)
    grid = grid.reshape(GRID_WIDTH, GRID_WIDTH).copy()
    window = grid[top:top + labels.shape[0], left:left + labels.shape[1]]
    unknown = window == 128
    changes = {"unknown_to_free": unknown & (free >= 2) & (wall == 0) & (near == 0),
               "unknown_to_wall": unknown & (wall >= 2) & (free == 0)}
    window[changes["unknown_to_free"]] = 0
    window[changes["unknown_to_wall"]] = 255
    sections = tuple(type(s)(2, grid.tobytes()) if s.section_id == 2 else s for s in reference.sections)
    return type(reference)(reference.field_4_raw, sections), {name: int(mask.sum()) for name, mask in changes.items()}


def write_candidate(reference_path, result, path, *, clear_changing=False):
    path = Path(path)
    report_path = Path(str(path) + ".json")
    if path.resolve() == Path(reference_path).resolve():
        raise ValueError("Candidate must not replace its source map")
    if path.exists() or report_path.exists():
        raise ValueError("Candidate and report paths must be new; existing artifacts are preserved")
    source = Path(reference_path).read_bytes()
    reference = parse(source)
    candidate, changes = candidate_container(reference, result["labels"], result["summary"]["origin_xy"],
                                             evidence=result.get("candidate_evidence"), clear_changing=clear_changing)
    data = candidate.serialize()
    reparsed = parse(data)
    untouched = all(a.payload == b.payload for a, b in zip(reference.sections, reparsed.sections)
                    if a.section_id != 2)
    if len(data) != len(source) or not untouched or len(reparsed.sections) != len(reference.sections):
        raise ValueError("Candidate changed more than the ID2 payload")
    report = {"kind": "offline_candidate_never_loaded_on_a_robot", "based_on": Path(reference_path).name,
              "based_on_sha256": hashlib.sha256(source).hexdigest(), "sha256": hashlib.sha256(data).hexdigest(),
              "bytes": len(data), "id2_cells_changed": changes, "other_sections_identical": True,
              "clear_changing": clear_changing,
              "candidate_policy": "raw128_only_two_exact_votes_zero_opposition_free_no_neighbour_walls",
              "independent_acquisition_proven": False,
              "limitations": ["Exact votes may reuse correlated observations.",
                              "Ray-like artifacts may remain outside physical floor boundaries.",
                              "Native SLAM consistency and robot acceptance are unverified."]}
    with path.open("xb") as candidate_file:
        candidate_file.write(data)
    with report_path.open("x", encoding="utf-8") as report_file:
        report_file.write(json.dumps(report, indent=2))
    return report


def write_outputs(result, output, scale=4):
    from matplotlib.image import imsave
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    labels = result["labels"]
    exported = {"schema_version": 1, "width": labels.shape[1], "height": labels.shape[0],
                "labels": {str(value): name for value, name in LABELS.items()},
                "label_rle": encode_rle(labels), "floor_rle": encode_rle(result["floor"]),
                "observations_rle": encode_rle(result["observations"]), "summary": result["summary"]}
    (output / "native-fusion.json").write_text(json.dumps(exported, indent=2), encoding="utf-8")
    block = np.ones((scale, scale, 1), dtype=np.uint8)
    # Images use FloorMap row order (native rows reversed) for direct comparison.
    for name, grid in (("native-fusion.png", labels), ("native-fusion-floor.png", result["floor"]),
                       ("native-reference.png", result["reference"])):
        imsave(output / name, np.kron(PALETTE[grid[::-1]], block))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path, help="Native map of the current home (alignment reference)")
    parser.add_argument("others", type=Path, nargs="+", help="Other acquired native maps; mismatches are skipped")
    parser.add_argument("--output", type=Path, required=True, help="Private folder outside the repository")
    parser.add_argument("--dataset", type=Path, help="Collected Vacuula dataset: its cycle FloorMaps also vote")
    parser.add_argument("--minimum-confidence", type=float, default=MINIMUM_CONFIDENCE)
    parser.add_argument("--candidate", help="Also write a candidate native map with this file name (offline only)")
    parser.add_argument("--clear-changing", action="store_true",
                        help="Unsupported legacy option: strict candidate generation rejects wall clearing")
    args = parser.parse_args()
    if args.clear_changing:
        parser.error("--clear-changing is disabled: strict generation preserves all observed cells")
    try:
        dataset = json.loads(args.dataset.read_text(encoding="utf-8")) if args.dataset else None
        result = fuse_permaps(args.reference, args.others, dataset=dataset,
                              minimum_confidence=args.minimum_confidence)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Fusion failed: {error}\n")
    write_outputs(result, args.output)
    if args.candidate:
        report = write_candidate(args.reference, result, args.output / args.candidate,
                                 clear_changing=args.clear_changing)
        print(json.dumps(report, indent=2))
    summary = result["summary"]
    print(json.dumps({key: summary[key] for key in (
        "trust_order", "label_counts", "reference_known_cells", "fused_known_cells",
        "cells_added_beyond_reference", "reference_walls_not_confirmed", "reference_free_disputed")}, indent=2))


if __name__ == "__main__":
    main()
