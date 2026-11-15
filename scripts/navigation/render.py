"""Readable notification and e-paper maps from the read-only analysis bundle."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from matplotlib import font_manager
from scipy.ndimage import label

try:
    from .analyze import decode_rle, warp_mask
except ImportError:
    from analyze import decode_rle, warp_mask


def _stamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def map_layers(dataset, analysis):
    """Return actual latest trace plus previous reliable passages in image pixels."""
    runs = dataset.get("runs", [])
    latest = max(runs, key=lambda run: run.get("ended_at") or run.get("started_at") or run["id"], default=None)
    reference = dataset["reference"]
    floor = decode_rle(reference["floor_rle"], reference["width"], reference["height"])
    records = {record["id"]: record for record in analysis.get("runs", [])}
    record = records.get(latest["id"], {}) if latest else {}
    registered = bool(record.get("coverage_eligible") and record.get("registration", {}).get("accepted"))
    if latest and not registered:
        grid = latest.get("grid", latest)
        floor = decode_rle(grid["floor_rle"], grid["width"], grid["height"])
    latest_mask = np.zeros(floor.shape, dtype=bool)
    age_days = np.full(floor.shape, np.nan)
    anchor = _stamp(latest.get("ended_at") or latest.get("started_at")) if latest else None
    for run in runs:
        grid = run.get("grid", run)
        if not grid.get("run_rle"):
            continue
        path = decode_rle(grid["run_rle"], grid["width"], grid["height"])
        path_values = grid.get("path_values")
        mask = np.isin(path, path_values) if path_values is not None else ~np.isin(path, [128, 100, 200])
        item = records.get(run["id"], {})
        if registered:
            registration = item.get("registration", {})
            if not item.get("coverage_eligible") or not registration.get("accepted"):
                continue
            matrix = registration.get("transform", {}).get("matrix3x3")
            if matrix is None:
                continue
            mask = warp_mask(mask, matrix, floor.shape)
        elif latest is None or run["id"] != latest["id"]:
            continue
        if latest and run["id"] == latest["id"]:
            latest_mask = mask
        ended = _stamp(run.get("ended_at"))
        if anchor and ended and run.get("status") in {"complete", "incomplete", "canceled"}:
            age = max(0, (anchor - ended).total_seconds() / 86400)
            age_days[mask] = np.fmin(age_days[mask], age)
    return floor, latest_mask, age_days, latest, record, registered


def _font(size):
    return ImageFont.truetype(font_manager.findfont("DejaVu Sans"), size=size)


def _fit_text(draw, text, position, width, font, fill=0):
    """Keep metadata within its card, using an explicit ellipsis when needed."""
    while text and draw.textlength(text, font=font) > width:
        text = text[:-2].rstrip() + "…" if not text.endswith("…") else text[:-2] + "…"
    draw.text(position, text, font=font, fill=fill)


def _draw_map(floor, latest_mask, age_days, box_size, monochrome=True):
    """Crop the useful footprint and scale it up as well as down."""
    height, width = floor.shape
    rgb = np.full((height, width, 3), 255, dtype=np.uint8)
    yy, xx = np.indices(floor.shape)
    recent = np.isfinite(age_days) & ~latest_mask
    hatch = ((xx + yy) % 7 <= 1)
    rgb[floor == 1] = [248, 249, 247] if not monochrome else [255, 255, 255]
    rgb[floor == 3] = [231, 234, 230] if not monochrome else [255, 255, 255]
    rgb[recent] = [235, 239, 241] if not monochrome else [219, 219, 219]
    if not monochrome:
        rgb[recent & hatch] = [167, 181, 191]
    rgb[latest_mask] = [35, 150, 139] if not monochrome else [146, 146, 146]
    rgb[floor == 2] = [24, 44, 49] if not monochrome else [0, 0, 0]
    occupied = np.isin(floor, [1, 2]) | latest_mask
    if not occupied.any():
        occupied = np.isfinite(age_days)
    # Stray uncertain pixels must not shrink the actual house into a tiny icon.
    # This only selects the display viewport; evidence masks are unchanged.
    components, count = label(occupied, structure=np.ones((3, 3), dtype=int))
    if count:
        counts = np.bincount(components.ravel())
        counts[0] = 0
        supported = counts >= min(12, max(1, int(counts.max())))
        occupied = supported[components]
    image = Image.fromarray(rgb)
    if np.any(occupied):
        ys, xs = np.where(occupied)
        bounds = (max(0, xs.min() - 4), max(0, ys.min() - 4), min(width, xs.max() + 5), min(height, ys.max() + 5))
        image = image.crop(bounds)
    factor = min(box_size[0] / image.width, box_size[1] / image.height)
    image = image.resize((max(1, round(image.width * factor)), max(1, round(image.height * factor))),
                         Image.Resampling.NEAREST)
    return image.convert("L") if monochrome else image


def _render_card(layers, size, monochrome=True):
    width, height = size
    if width < 320 or height < 220:
        raise ValueError("Map cards require at least 320×220 pixels")
    floor, latest_mask, age_days, latest, record, registered = layers
    image = Image.new("L" if monochrome else "RGB", size, 255 if monochrome else "white")
    draw = ImageDraw.Draw(image)
    scale = min(width / 680, height / 420)
    margin = max(10, round(16 * scale))
    title_font = _font(max(16, round(25 * scale)))
    text_font = _font(max(12, round(18 * scale)))
    small_font = _font(max(11, round(15 * scale)))
    status = {"complete": "terminé", "incomplete": "incomplet", "canceled": "interrompu"}.get(
        latest.get("status") if latest else None, "état inconnu")
    _fit_text(draw, "Cycle " + status, (margin, margin), width - 2 * margin, title_font)
    date = _stamp(latest.get("ended_at") or latest.get("started_at")) if latest else None
    display_zone = ZoneInfo(os.environ.get("TZ", "America/Toronto"))
    date_label = date.astimezone(display_zone).strftime("%d/%m %H:%M %Z") if date else "Aucun cycle disponible"
    label = date_label if registered else f"{date_label} · localisation incertaine"
    _fit_text(draw, label, (margin, margin + round(35 * scale)), width - 2 * margin, text_font)
    map_top = margin + round(67 * scale)
    footer_height = round(60 * scale)
    map_height = max(70, height - map_top - footer_height - margin)
    map_image = _draw_map(floor, latest_mask, age_days, (width - 2 * margin, map_height), monochrome)
    image.paste(map_image, ((width - map_image.width) // 2, map_top + (map_height - map_image.height) // 2))
    footer_top = height - footer_height
    legend = "Gris foncé : ce cycle · clair : avant" if monochrome else "Vert : ce cycle · hachures : passages antérieurs"
    _fit_text(draw, legend, (margin, footer_top),
              width - 2 * margin, small_font)
    warning = "Passages observés" if registered else "Tracé seul, exclu du cumul"
    _fit_text(draw, warning, (margin, footer_top + round(23 * scale)), width - 2 * margin, text_font)
    return image


def render(dataset, analysis, output_dir, *, eink_size=(680, 300), latest_size=(1200, 800)):
    """Write latest.png and eink.png atomically into the service generation."""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    layers = map_layers(dataset, analysis)
    consensus_path = output / "consensus-map.json"
    if layers[-1] and consensus_path.exists():
        consensus = json.loads(consensus_path.read_text(encoding="utf-8"))
        plan = decode_rle(consensus["label_rle"], consensus["width"], consensus["height"])
        if plan.shape != layers[0].shape:
            raise ValueError("Consensus and reference shapes differ")
        layers = (plan, *layers[1:])
    result = {}
    for name, size in [("latest", latest_size), ("eink", eink_size)]:
        path = output / (name + ".png")
        temporary = output / (name + ".png.tmp")
        _render_card(layers, size, monochrome=name == "eink").save(temporary, format="PNG")
        temporary.replace(path)
        result[name] = path
    return result
