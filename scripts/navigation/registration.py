"""Conservative wall-only SE(2) registration of independently framed robot maps.

Coordinates are pixel centres: x=column, y=row (image coordinates, y down).
The returned matrix maps moving pixels into reference pixels. No dock position,
shared origin, or prior run transform is assumed. SciPy/NumPy are offline tools.
"""

from __future__ import annotations

import math

import numpy as np
from scipy.ndimage import distance_transform_edt, map_coordinates, maximum_filter
from scipy.optimize import minimize
from scipy.signal import fftconvolve


def _walls(grid):
    array = np.asarray(grid)
    if array.ndim != 2:
        raise ValueError("Expected a two-dimensional wall mask or 0/1/2 floor grid")
    return array if array.dtype == bool else array == 2


def _rotation(angle):
    cosine, sine = math.cos(angle), math.sin(angle)
    return np.array([[cosine, -sine], [sine, cosine]])


def _points(mask, limit=4000):
    rows, columns = np.nonzero(mask)
    result = np.column_stack((columns, rows)).astype(float)
    if len(result) > limit:
        result = result[np.linspace(0, len(result) - 1, limit, dtype=int)]
    return result


def register_grids(reference, moving, *, angular_step_degrees=10, tolerance_pixels=3.0,
                   minimum_score=0.70, ambiguity_margin=0.025):
    """Return a rigid moving→reference transform, rejecting weak/ambiguous fits.

    Inputs accept boolean wall masks or floor grids (2=wall, 1=free, 0=unknown).
    Score tolerates up to 15% unmatched moving-wall points (mobile obstacles).
    ``accepted`` is deliberately conservative: repeated/symmetric rooms can fit
    well but must not become confident room-cleaning evidence. ``confidence`` is
    a diagnostic quality score, not a calibrated probability.
    """
    reference_mask, moving_mask = _walls(reference), _walls(moving)
    reference_points, moving_points = _points(reference_mask), _points(moving_mask)
    rejected = {"accepted": False, "confidence": 0.0, "transform": None}
    if min(len(reference_points), len(moving_points)) < 30:
        return {**rejected, "diagnostics": {"reason": "insufficient_walls"}}
    if tolerance_pixels <= 0 or not 0 < angular_step_degrees <= 45:
        raise ValueError("Positive tolerance and angular step in (0,45] required")

    distance = distance_transform_edt(~reference_mask)
    sigma = tolerance_pixels
    field = np.exp(-0.5 * (distance / sigma) ** 2)
    # Bound coarse FFT memory without assuming equal map sizes or origins.
    scale = max(1.0, max(*reference_mask.shape, *moving_mask.shape) / 180.0)
    coarse_shape = tuple(int(math.ceil(v / scale)) + 2 for v in reference_mask.shape)
    coarse_reference = np.zeros(coarse_shape)
    indices = np.rint(reference_points / scale).astype(int)
    coarse_reference[indices[:, 1], indices[:, 0]] = 1
    coarse_distance = distance_transform_edt(coarse_reference == 0)
    coarse_field = np.exp(-0.5 * (coarse_distance / max(1.0, sigma / scale)) ** 2)

    def quality(parameters):
        angle, tx, ty = parameters
        transformed = moving_points @ _rotation(angle).T + (tx, ty)
        values = map_coordinates(field, [transformed[:, 1], transformed[:, 0]],
                                 order=1, mode="constant", cval=0)
        # The worst residuals may be chairs or doors that have moved.
        keep = max(1, int(len(values) * 0.85))
        return float(np.partition(values, len(values) - keep)[-keep:].mean())

    candidates = []
    for angle_degrees in np.arange(-180, 180, angular_step_degrees):
        angle = math.radians(float(angle_degrees))
        rotated = moving_points @ _rotation(angle).T / scale
        offset = np.floor(rotated.min(axis=0))
        pixels = np.rint(rotated - offset).astype(int)
        shape = (int(pixels[:, 1].max()) + 1, int(pixels[:, 0].max()) + 1)
        raster = np.zeros(shape)
        raster[pixels[:, 1], pixels[:, 0]] = 1
        correlation = fftconvolve(coarse_field, raster[::-1, ::-1], mode="full")
        peaks = correlation == maximum_filter(correlation, size=9)
        peak_indices = np.flatnonzero(peaks)
        best = peak_indices[np.argsort(correlation.ravel()[peak_indices])[-3:]]
        for index in best:
            row, column = np.unravel_index(index, correlation.shape)
            translation = (np.array([column - shape[1] + 1, row - shape[0] + 1]) - offset) * scale
            parameters = np.array([angle, *translation])
            candidates.append((quality(parameters), parameters))

    candidates.sort(key=lambda item: item[0], reverse=True)
    refined = []
    # Keep diverse basins so symmetric alternatives survive refinement.
    for _, parameters in candidates[:36]:
        angle, tx, ty = parameters
        angle_radius = math.radians(angular_step_degrees * 1.2)
        shift_radius = max(scale * 5, sigma * 3)
        bounds = [(angle - angle_radius, angle + angle_radius),
                  (tx - shift_radius, tx + shift_radius), (ty - shift_radius, ty + shift_radius)]
        result = minimize(lambda p: -quality(p), parameters, method="Powell", bounds=bounds,
                          options={"maxiter": 65, "xtol": 0.0002, "ftol": 0.00002})
        refined.append((quality(result.x), result.x))
    refined.sort(key=lambda item: item[0], reverse=True)
    score, parameters = refined[0]
    angle, tx, ty = parameters
    transformed = moving_points @ _rotation(angle).T + (tx, ty)
    residuals = map_coordinates(distance, [transformed[:, 1], transformed[:, 0]],
                               order=1, mode="constant", cval=1e6)
    inlier_fraction = float(np.mean(residuals <= sigma * 2))
    # Compare transforms by their displacement of actual wall points, avoiding
    # misleading translation differences caused by rotations around origin.
    alternative_score = 0.0
    for other_score, other in refined[1:]:
        other_points = moving_points @ _rotation(other[0]).T + other[1:]
        displacement = np.median(np.linalg.norm(other_points - transformed, axis=1))
        if displacement > max(8.0, sigma * 3):
            alternative_score = other_score
            break
    margin = score - alternative_score
    accepted = bool(score >= minimum_score and inlier_fraction >= 0.65 and margin >= ambiguity_margin)
    reason = "accepted" if accepted else ("ambiguous" if margin < ambiguity_margin else "weak_fit")
    confidence = min(score, inlier_fraction, max(0.0, margin) / max(ambiguity_margin * 3, 1e-9))
    rotation = _rotation(angle)
    matrix = np.eye(3)
    matrix[:2, :2], matrix[:2, 2] = rotation, (tx, ty)
    return {
        "accepted": accepted,
        "confidence": float(confidence),
        "transform": {"angle_degrees": float((math.degrees(angle) + 180) % 360 - 180),
                      "tx": float(tx), "ty": float(ty), "matrix3x3": matrix.tolist()},
        "diagnostics": {"reason": reason, "score": score, "inlier_fraction": inlier_fraction,
                        "alternative_score": alternative_score, "margin": margin,
                        "reference_wall_points": len(reference_points), "moving_wall_points": len(moving_points),
                        "tolerance_pixels": sigma, "coarse_scale": scale,
                        "confidence_is_probability": False},
    }
