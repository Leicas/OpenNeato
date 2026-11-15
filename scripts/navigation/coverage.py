"""Conservative coverage bookkeeping in an already registered reference frame.

Free space is never cleaning evidence. Callers must supply a mask whose cleaned
semantics are established, or a robot trajectory and an explicit brush radius.
All coordinates/radii are expressed in reference-grid cells (x right, y down).
"""

from datetime import datetime, timezone
import math

import numpy as np


def _timestamp(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.timestamp()


def _iso(value):
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat().replace("+00:00", "Z")


def _polygon_mask(vertices, shape):
    points = np.asarray(vertices, dtype=float)
    if points.ndim != 2 or points.shape[1] != 2 or len(points) < 3 or not np.isfinite(points).all():
        raise ValueError("zone polygon requires at least three finite [x, y] vertices")
    yy, xx = np.indices(shape, dtype=float)
    xx += 0.5
    yy += 0.5
    inside = np.zeros(shape, dtype=bool)
    previous = points[-1]
    for current in points:
        x0, y0 = previous
        x1, y1 = current
        if y0 != y1:
            inside ^= ((y0 > yy) != (y1 > yy)) & (xx < (x1 - x0) * (yy - y0) / (y1 - y0) + x0)
        previous = current
    return inside


def _trajectory_mask(trajectory, radius, shape):
    points = np.asarray(trajectory, dtype=float)
    if points.ndim != 2 or points.shape[1] != 2 or not len(points) or not np.isfinite(points).all():
        raise ValueError("trajectory requires finite [x, y] points")
    if not isinstance(radius, (int, float)) or not math.isfinite(radius) or radius <= 0:
        raise ValueError("brush_radius_cells must be positive and finite")
    covered = np.zeros(shape, dtype=bool)
    # Rasterize capsules using exact distance to each segment, bounded to its box.
    # A caller must split gaps/teleports into separate trajectories, not interpolate them.
    pairs = zip(points[:-1], points[1:]) if len(points) > 1 else [(points[0], points[0])]
    for first, second in pairs:
        low = np.maximum(np.floor(np.minimum(first, second) - radius).astype(int), [0, 0])
        high = np.minimum(np.ceil(np.maximum(first, second) + radius).astype(int), [shape[1], shape[0]])
        if np.any(high <= low):
            continue
        yy, xx = np.mgrid[low[1]:high[1], low[0]:high[0]]
        locations = np.stack([xx + 0.5, yy + 0.5], axis=-1)
        vector = second - first
        length_squared = float(vector @ vector)
        along = np.clip(((locations - first) @ vector) / length_squared, 0, 1) if length_squared else 0
        nearest = first + np.asarray(along)[..., None] * vector
        distance_squared = np.sum((locations - nearest) ** 2, axis=-1)
        covered[low[1]:high[1], low[0]:high[0]] |= distance_squared <= radius ** 2
    return covered


def _evidence_mask(run, shape):
    if "coverage_mask" in run:
        if run.get("coverage_semantics") not in {"cleaned", "traversed"}:
            return None, "mask_semantics_unverified"
        raw = np.asarray(run["coverage_mask"])
        if raw.shape != shape or raw.dtype != np.bool_:
            raise ValueError("coverage_mask must be boolean and match reference_shape")
        return raw, "cleaned_mask" if run["coverage_semantics"] == "cleaned" else "traversed_mask"
    if "trajectory" in run:
        if run.get("trajectory_semantics") != "cleaning":
            return None, "trajectory_cleaning_unverified"
        if run.get("trajectory_continuous") is not True:
            return None, "trajectory_continuity_unverified"
        return _trajectory_mask(run["trajectory"], run.get("brush_radius_cells"), shape), "brush_trajectory"
    return None, "no_cleaning_geometry"


def analyze_coverage(runs, zones=None, reference_shape=None, *, now=None,
                     min_confidence=0.8, completion_fraction=0.9, target_days=7):
    """Return observations and suggested priorities; never execute a HA action.

    Each run supplies id, status, end_at (or ended_at),
    registration={accepted: bool, confidence: float}, and evidence accepted
    by _evidence_mask. Zones are {id, name, polygon, target_days?}; polygons use
    reference cells. No zones means one unnamed map extent, never inferred rooms.

    A completed cycle plus sufficient observed geometry can confirm a zone.
    Canceled/incomplete cycles contribute partial observations only. Priority is
    the fraction of zone cells without an observation in its target interval;
    it is withheld when there is no usable geometry. This is an advisory metric.
    """
    if reference_shape is None:
        raise ValueError("reference_shape=(height, width) is required")
    shape = tuple(reference_shape)
    if len(shape) != 2 or any(not isinstance(v, (int, np.integer)) or v <= 0 for v in shape):
        raise ValueError("reference_shape must contain two positive integers")
    if not 0 <= min_confidence <= 1 or not 0 < completion_fraction <= 1 or target_days <= 0:
        raise ValueError("invalid confidence, completion_fraction, or target_days")
    now_stamp = _timestamp(now) if now is not None else datetime.now(timezone.utc).timestamp()
    zone_specs = zones if zones else [{"id": "map_extent", "name": None}]
    zone_masks = []
    for zone in zone_specs:
        mask = _polygon_mask(zone["polygon"], shape) if "polygon" in zone else np.ones(shape, dtype=bool)
        if not mask.any():
            raise ValueError("zone polygon contains no reference cell centers")
        days = float(zone.get("target_days", target_days))
        if not math.isfinite(days) or days <= 0:
            raise ValueError("zone target_days must be positive and finite")
        zone_masks.append((zone, mask, days))

    observations = []
    rejected = []
    accepted = []
    identifiers = set()
    for run in runs:
        identifier = str(run["id"])
        if identifier in identifiers:
            raise ValueError("duplicate run id: " + identifier)
        identifiers.add(identifier)
        registration = run.get("registration") or {}
        confidence = registration.get("confidence", 0)
        if (registration.get("accepted") is not True or not isinstance(confidence, (int, float))
                or not math.isfinite(confidence) or confidence < min_confidence or confidence > 1):
            rejected.append({"run_id": identifier, "reason": "registration_unreliable"})
            continue
        stamp = _timestamp(run.get("end_at") or run.get("ended_at"))
        if stamp is None:
            rejected.append({"run_id": identifier, "reason": "missing_final_timestamp"})
            continue
        if stamp > now_stamp:
            rejected.append({"run_id": identifier, "reason": "future_timestamp"})
            continue
        mask, source = _evidence_mask(run, shape)
        if mask is None:
            rejected.append({"run_id": identifier, "reason": source})
            continue
        status = run.get("status", "unknown")
        if status not in {"complete", "canceled", "cancelled", "incomplete"}:
            rejected.append({"run_id": identifier, "reason": "run_not_final"})
            continue
        accepted.append((stamp, identifier, mask, status, float(confidence), source))

    accepted.sort(key=lambda item: (item[0], item[1]))
    last_seen = np.full(shape, -np.inf)
    cumulative = np.zeros(shape, dtype=bool)
    traversed = np.zeros(shape, dtype=bool)
    zone_results = []
    for stamp, identifier, mask, status, confidence, source in accepted:
        traversed |= mask
        if source != "traversed_mask":
            last_seen[mask] = stamp
            cumulative |= mask
        observations.append({"run_id": identifier, "end_at": _iso(stamp), "cycle_status": status,
                             "evidence": source, "confidence": confidence, "observed_cells": int(mask.sum()),
                             "interpretation": ("traversed" if source == "traversed_mask" else "observed")
                             if status == "complete" else "partial"})
    for zone, mask, days in zone_masks:
        cells = int(mask.sum())
        relevant = [(stamp, identifier, coverage, status, confidence, source)
                    for stamp, identifier, coverage, status, confidence, source in accepted if np.any(coverage & mask)]
        zone_observations = []
        last_complete = None
        for stamp, identifier, coverage, status, confidence, source in relevant:
            fraction = float(np.count_nonzero(coverage & mask) / cells)
            confirmed = source != "traversed_mask" and status == "complete" and fraction >= completion_fraction
            if confirmed:
                last_complete = stamp
            zone_observations.append({"run_id": identifier, "end_at": _iso(stamp),
                                      "observed_fraction": round(fraction, 6),
                                      "semantics": "traversed" if source == "traversed_mask" else "cleaned",
                                      "result": "confirmed" if confirmed else
                                      ("traversed" if source == "traversed_mask" and status == "complete" else "partial")})
        known = bool(relevant)
        cleaning_known = any(source != "traversed_mask" for _, _, _, _, _, source in relevant)
        recent = last_seen[mask] >= now_stamp - days * 86400
        latest = relevant[-1][0] if known else None
        zone_results.append({"id": str(zone["id"]), "name": zone.get("name"),
                             "state": (zone_observations[-1]["result"] if known else "unknown"),
                             "last_observed_at": _iso(latest), "last_confirmed_complete_at": _iso(last_complete),
                             "traversed_fraction": round(float(np.count_nonzero(traversed & mask) / cells), 6),
                             "cleaned_fraction": round(float(np.count_nonzero(cumulative & mask) / cells), 6) if cleaning_known else None,
                             "recent_cleaned_fraction": round(float(recent.sum() / cells), 6) if cleaning_known else None,
                             "target_days": days,
                             "suggested_priority": round(float(1 - recent.sum() / cells), 6) if cleaning_known else None,
                             "priority_basis": "cleaning_observation_age" if cleaning_known else "cleaning_state_unknown",
                             "observations": zone_observations})
    return {"schema_version": 1, "generated_at": _iso(now_stamp), "reference_shape": list(shape),
            "coordinate_system": "reference_grid_cells_xy", "rooms_inferred": False,
            "priority_policy": "fraction_without_recent_cleaning_observation; unknown withheld",
            "zones": zone_results, "runs": observations, "rejected_runs": rejected,
            "limitations": ["geometry is observed coverage, not proof of dirt removal",
                            "free space and aggregate area never count as cleaned",
                            "unnamed map extent is not a room"]}
