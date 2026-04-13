"""Cleaning-session map renderer — converts JSONL pose data to a PNG image.

Algorithm ported from:
  - frontend/src/history-data.ts  (JSONL parsing, coverage grid, bounds)
  - frontend/src/views/history/helpers.ts  (canvas rendering)

Rendering order (bottom to top):
  1. Dark background
  2. Grid lines (0.5 m spacing)
  3. Coverage cells (semi-transparent green)
  4. Path line (amber/gold)
  5. Start marker (green dot)
  6. End marker (red dot)
  7. Recharge bolt icons (gold with dark outline)
"""

from __future__ import annotations

import io
import json
import math
import re
from typing import Any

from PIL import Image, ImageDraw

from .const import (
    HISTORY_BG_COLOR,
    HISTORY_CELL_SIZE_M,
    HISTORY_COVERAGE_COLOR,
    HISTORY_END_COLOR,
    HISTORY_GRID_COLOR,
    HISTORY_GRID_STEP_M,
    HISTORY_IMAGE_SIZE,
    HISTORY_PAD_PX,
    HISTORY_PATH_COLOR,
    HISTORY_RECHARGE_COLOR,
    HISTORY_ROBOT_DIAMETER_M,
    HISTORY_START_COLOR,
)


# ── JSONL parsing (ported from history-data.ts) ─────────────────────

# Heatshrink decompression can corrupt bytes in numeric tokens.
# Match the structural skeleton permissively so we can repair numbers.
_POSE_RE = re.compile(
    r'^\{.x.:\s*(-?[\d.eE:"\w-]+)\s*,.y.:\s*(-?[\d.eE:"\w-]+)'
    r'\s*,.t.:\s*([\d.eE:"\w-]+)\s*,.ts.:\s*([\d.eE:"\w-]+)\s*\}$'
)


def _repair_number(raw: str) -> float | None:
    """Replace non-numeric garbage with dots, collapse, parse."""
    cleaned = re.sub(r"[^0-9.eE-]", ".", raw)
    parts = cleaned.split(".")
    if len(parts) > 2:
        cleaned = parts[0] + "." + "".join(parts[1:])
    try:
        n = float(cleaned)
        return n if math.isfinite(n) else None
    except ValueError:
        return None


def _try_repair_pose(line: str) -> dict[str, float] | None:
    """Attempt regex-based recovery of a corrupted pose line."""
    m = _POSE_RE.match(line)
    if not m:
        return None
    vals = [_repair_number(m.group(i)) for i in range(1, 5)]
    if any(v is None for v in vals):
        return None
    return {"x": vals[0], "y": vals[1], "t": vals[2], "ts": vals[3]}


def parse_session_jsonl(raw: str) -> dict[str, Any]:
    """Parse raw JSONL text into structured session data.

    Returns dict with keys: session, summary, path, coverage, recharges, bounds.
    """
    lines = [l for l in raw.strip().split("\n") if l.strip()]

    session: dict | None = None
    summary: dict | None = None
    poses: list[dict[str, float]] = []
    recharges: list[tuple[float, float]] = []

    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            repaired = _try_repair_pose(line)
            if repaired:
                poses.append(repaired)
            continue

        obj_type = obj.get("type")
        if obj_type == "session":
            session = obj
        elif obj_type == "summary":
            summary = obj
        elif obj_type == "recharge":
            recharges.append((obj.get("x", 0), obj.get("y", 0)))
        elif "x" in obj and "y" in obj and "type" not in obj:
            # Pose point — skip origin (all zeros)
            if obj.get("x") != 0 or obj.get("y") != 0 or obj.get("t") != 0:
                poses.append(obj)

    if not poses:
        return {
            "session": session, "summary": summary,
            "path": [], "coverage": [], "recharges": recharges, "bounds": None,
        }

    # Build coverage grid — stamp robot footprint circle at each pose
    cell_size = HISTORY_CELL_SIZE_M
    radius_cells = math.ceil(HISTORY_ROBOT_DIAMETER_M / 2 / cell_size)
    covered: set[tuple[int, int]] = set()

    for p in poses:
        cx = round(p["x"] / cell_size)
        cy = round(p["y"] / cell_size)
        for dx in range(-radius_cells, radius_cells + 1):
            for dy in range(-radius_cells, radius_cells + 1):
                if dx * dx + dy * dy <= radius_cells * radius_cells:
                    covered.add((cx + dx, cy + dy))

    # Bounding box padded by robot radius
    pad = HISTORY_ROBOT_DIAMETER_M / 2 + 0.1
    xs = [p["x"] for p in poses]
    ys = [p["y"] for p in poses]
    bounds = {
        "minX": min(xs) - pad,
        "maxX": max(xs) + pad,
        "minY": min(ys) - pad,
        "maxY": max(ys) + pad,
    }

    return {
        "session": session,
        "summary": summary,
        "path": poses,
        "coverage": list(covered),
        "recharges": recharges,
        "bounds": bounds,
    }


# ── Map rendering (ported from helpers.ts::renderMap) ────────────────

def render_history_map(
    data: dict[str, Any],
    image_size: int = HISTORY_IMAGE_SIZE,
    recording: bool = False,
) -> bytes:
    """Render a cleaning session map as a PNG image. Returns PNG bytes.

    Parameters
    ----------
    data : dict
        Parsed session data from parse_session_jsonl().
    image_size : int
        Output image width and height in pixels.
    recording : bool
        True if the session is still being recorded (affects end marker style).
    """
    bounds = data.get("bounds")
    path = data.get("path", [])

    if not bounds or not path:
        # No data — return a minimal placeholder
        from .lidar_renderer import render_idle_image
        return render_idle_image(image_size)

    img = Image.new("RGBA", (image_size, image_size), HISTORY_BG_COLOR + (255,))
    draw = ImageDraw.Draw(img)

    min_x, max_x = bounds["minX"], bounds["maxX"]
    min_y, max_y = bounds["minY"], bounds["maxY"]
    world_w = max_x - min_x
    world_h = max_y - min_y

    if world_w <= 0 or world_h <= 0:
        from .lidar_renderer import render_idle_image
        return render_idle_image(image_size)

    pad = HISTORY_PAD_PX
    avail_w = image_size - pad * 2
    avail_h = image_size - pad * 2
    scale = min(avail_w / world_w, avail_h / world_h)

    rendered_w = world_w * scale
    rendered_h = world_h * scale
    off_x = pad + (avail_w - rendered_w) / 2
    off_y = pad + (avail_h - rendered_h) / 2

    def to_x(x: float) -> float:
        return off_x + (x - min_x) * scale

    def to_y(y: float) -> float:
        return off_y + (max_y - y) * scale  # Y is inverted

    # ── Grid lines ───────────────────────────────────────────────────
    grid_step = HISTORY_GRID_STEP_M
    grid_min_x = math.floor(min_x / grid_step) * grid_step
    grid_min_y = math.floor(min_y / grid_step) * grid_step

    gx = grid_min_x
    while gx <= max_x:
        x_px = to_x(gx)
        draw.line([(x_px, to_y(min_y)), (x_px, to_y(max_y))], fill=HISTORY_GRID_COLOR, width=1)
        gx += grid_step

    gy = grid_min_y
    while gy <= max_y:
        y_px = to_y(gy)
        draw.line([(to_x(min_x), y_px), (to_x(max_x), y_px)], fill=HISTORY_GRID_COLOR, width=1)
        gy += grid_step

    # ── Coverage cells ───────────────────────────────────────────────
    cell_size = HISTORY_CELL_SIZE_M
    cell_px = cell_size * scale
    for cx, cy in data.get("coverage", []):
        wx = cx * cell_size
        wy = cy * cell_size
        x0 = to_x(wx) - cell_px / 2
        y0 = to_y(wy) - cell_px / 2
        draw.rectangle([x0, y0, x0 + cell_px, y0 + cell_px], fill=HISTORY_COVERAGE_COLOR)

    # ── Path line ────────────────────────────────────────────────────
    if len(path) > 1:
        coords = [(to_x(p["x"]), to_y(p["y"])) for p in path]
        draw.line(coords, fill=HISTORY_PATH_COLOR, width=2, joint="curve")

    # ── Start marker (green dot) ─────────────────────────────────────
    if path:
        sx, sy = to_x(path[0]["x"]), to_y(path[0]["y"])
        r = 5
        draw.ellipse([sx - r, sy - r, sx + r, sy + r], fill=HISTORY_START_COLOR)

    # ── End marker ───────────────────────────────────────────────────
    if len(path) > 1:
        ex, ey = to_x(path[-1]["x"]), to_y(path[-1]["y"])
        if recording:
            # Open green ring
            r = 6
            draw.ellipse(
                [ex - r, ey - r, ex + r, ey + r],
                outline=HISTORY_START_COLOR, width=3,
            )
        else:
            # Solid red dot
            r = 5
            draw.ellipse([ex - r, ey - r, ex + r, ey + r], fill=HISTORY_END_COLOR)

    # ── Recharge bolt icons ──────────────────────────────────────────
    for rx_world, ry_world in data.get("recharges", []):
        rx, ry = to_x(rx_world), to_y(ry_world)
        s = 10
        bolt = [
            (rx + s * 0.15, ry - s),
            (rx - s * 0.55, ry + s * 0.05),
            (rx - s * 0.05, ry + s * 0.05),
            (rx - s * 0.15, ry + s),
            (rx + s * 0.55, ry - s * 0.05),
            (rx + s * 0.05, ry - s * 0.05),
        ]
        draw.polygon(bolt, fill=HISTORY_RECHARGE_COLOR + (255,), outline=(0, 0, 0, 128))

    # ── Encode PNG ───────────────────────────────────────────────────
    # Convert RGBA to RGB for consistency with LIDAR camera
    rgb_img = Image.new("RGB", img.size, HISTORY_BG_COLOR)
    rgb_img.paste(img, mask=img.split()[3])
    buf = io.BytesIO()
    rgb_img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
