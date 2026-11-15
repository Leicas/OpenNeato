"""Conservative candidate plan from observations aligned to the original map.

This does not re-register maps, fill holes, close passages, or establish ground
truth. Dates are UTC calendar dates supplied by the caller. Repeated observations
on the same date cannot accumulate extra voting power. The reference sets the
output shape only: its pixels do not vote.
"""

from datetime import date

import numpy as np


def build_consensus(reference_grid, aligned_observations, *, stable_days=2, stable_ratio=0.8):
    """Return NumPy arrays and a JSON-ready summary for a candidate floor plan.

    Each observation is ``{grid: 2D 0/1/2, date: 'YYYY-MM-DD', quality: 0..1}``.
    Grids must already be warped to the reference shape; 0=unknown, 1=free,
    2=wall. Unknown pixels and zero-quality observations abstain.

    Per date/cell we retain maximum wall and free quality, then split that day's
    bounded vote between those classes. This makes exact duplicate runs inert
    and preserves within-day contradictions. Across dates votes are summed.
    A stable class needs at least ``stable_days`` decisive supporting dates and
    ``stable_ratio`` of the quality-weighted votes. Coexistence conflict remains
    visible even when the dominant class is stable. Recent counts cover the last
    three distinct positive-quality observation dates, relative to the dataset.

    ``wall_probability`` is an evidence ratio, not a calibrated probability;
    unobserved pixels use 0.5 and must be interpreted with the ``unknown`` mask.
    Independence between dates is only a heuristic, not a statistical guarantee.
    """
    reference = np.asarray(reference_grid)
    if reference.ndim != 2 or not reference.size:
        raise ValueError("reference_grid must be a nonempty two-dimensional grid")
    if type(stable_days) is not int or stable_days < 2:
        raise ValueError("stable_days must be an integer of at least two")
    if not 0.5 < stable_ratio <= 1:
        raise ValueError("stable_ratio must be greater than 0.5 and at most one")

    daily = {}
    for observation in aligned_observations:
        observed_date = observation["date"]
        if not isinstance(observed_date, str) or date.fromisoformat(observed_date).isoformat() != observed_date:
            raise ValueError("Observation dates must be canonical UTC YYYY-MM-DD strings")
        quality = float(observation["quality"])
        if not np.isfinite(quality) or not 0 <= quality <= 1:
            raise ValueError("Observation quality must be finite and between zero and one")
        grid = np.asarray(observation["grid"])
        if grid.shape != reference.shape or not np.isin(grid, [0, 1, 2]).all():
            raise ValueError("Each observation must match reference shape and contain only 0/1/2")
        if quality == 0 or not np.any(grid):
            continue
        if observed_date not in daily:
            daily[observed_date] = (np.zeros(reference.shape), np.zeros(reference.shape))
        wall, free = daily[observed_date]
        np.maximum(wall, np.where(grid == 2, quality, 0), out=wall)
        np.maximum(free, np.where(grid == 1, quality, 0), out=free)

    shape = reference.shape
    wall_weight = np.zeros(shape)
    total_weight = np.zeros(shape)
    observation_days = np.zeros(shape, dtype=np.int32)
    wall_days = np.zeros(shape, dtype=np.int32)
    free_days = np.zeros(shape, dtype=np.int32)
    decisive_wall_days = np.zeros(shape, dtype=np.int32)
    decisive_free_days = np.zeros(shape, dtype=np.int32)
    recent_wall_days = np.zeros(shape, dtype=np.int32)
    recent_free_days = np.zeros(shape, dtype=np.int32)
    recent_dates = sorted(daily)[-3:]
    for observed_date, (wall, free) in daily.items():
        evidence = wall + free
        weight = np.maximum(wall, free)
        fraction = np.divide(wall, evidence, out=np.full(shape, 0.5), where=evidence > 0)
        wall_weight += fraction * weight
        total_weight += weight
        observation_days += evidence > 0
        wall_days += wall > 0
        free_days += free > 0
        decisive_wall_days += (evidence > 0) & (fraction >= stable_ratio)
        decisive_free_days += (evidence > 0) & (fraction <= 1 - stable_ratio + 1e-12)
        if observed_date in recent_dates:
            recent_wall_days += wall > 0
            recent_free_days += free > 0

    wall_probability = np.divide(wall_weight, total_weight, out=np.full(shape, 0.5), where=total_weight > 0)
    unknown = observation_days == 0
    stable_wall = (decisive_wall_days >= stable_days) & (wall_probability >= stable_ratio - 1e-12)
    stable_free = (decisive_free_days >= stable_days) & (wall_probability <= 1 - stable_ratio + 1e-12)
    uncertain = ~(stable_wall | stable_free | unknown)
    conflict = (wall_days > 0) & (free_days > 0)
    balanced_conflict = conflict & (wall_probability > 0.2) & (wall_probability < 0.8)
    summary = {
        "kind": "candidate_consensus_not_ground_truth",
        "date_basis": "UTC_calendar_date_supplied_by_caller",
        "distinct_observation_dates": sorted(daily),
        "recent_dates": recent_dates,
        "stable_days": stable_days,
        "stable_ratio": stable_ratio,
        "reference_votes": False,
        "units": "reference_grid_cells",
        "cell_count": int(reference.size),
        "observed_cells": int(np.count_nonzero(~unknown)),
        "unknown_cells": int(np.count_nonzero(unknown)),
        "stable_wall_cells": int(np.count_nonzero(stable_wall)),
        "stable_free_cells": int(np.count_nonzero(stable_free)),
        "uncertain_cells": int(np.count_nonzero(uncertain)),
        "conflict_cells": int(np.count_nonzero(conflict)),
        "balanced_conflict_cells": int(np.count_nonzero(balanced_conflict)),
        "limitations": [
            "All observations must be aligned to the original reference, never this candidate.",
            "Distinct dates only approximate independent evidence.",
            "Wall ratios are not calibrated probabilities; unknown cells carry no evidence.",
            "No metric scale or guaranteed physical plan is inferred.",
        ],
    }
    return {
        "wall_probability": wall_probability,
        "observation_days": observation_days,
        "wall_days": wall_days,
        "free_days": free_days,
        "recent_wall_days": recent_wall_days,
        "recent_free_days": recent_free_days,
        "stable_wall": stable_wall,
        "stable_free": stable_free,
        "conflict": conflict,
        "balanced_conflict": balanced_conflict,
        "unknown": unknown,
        "uncertain": uncertain,
        "summary": summary,
    }
