"""Analyze saved robot maps and serve a read-only local report."""

from __future__ import annotations

import argparse
import functools
import html
import json
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from scipy.ndimage import affine_transform

try:
    from .registration import register_grids
    from .coverage import analyze_coverage
    from .consensus import build_consensus
except ImportError:
    from registration import register_grids
    from coverage import analyze_coverage
    from consensus import build_consensus


def decode_rle(rle, width, height):
    """Decode a bounded grid without silently accepting truncated input."""
    if not isinstance(width, int) or not isinstance(height, int) or not 0 < width * height <= 4_000_000:
        raise ValueError("Invalid grid dimensions")
    if any(len(pair) != 2 or not isinstance(pair[1], int) or pair[1] < 0 for pair in rle):
        raise ValueError("Invalid RLE count")
    if sum(pair[1] for pair in rle) != width * height:
        raise ValueError("RLE size does not match grid dimensions")
    return np.repeat([p[0] for p in rle], [p[1] for p in rle]).reshape(height, width)


def warp_mask(mask, matrix, output_shape):
    """Apply a source-to-reference homogeneous transform in (x, y) pixels."""
    inverse = np.linalg.inv(np.asarray(matrix, dtype=float))
    swap = np.array([[0, 1], [1, 0]])
    return affine_transform(
        mask.astype(np.uint8), swap @ inverse[:2, :2] @ swap,
        offset=swap @ inverse[:2, 2], output_shape=output_shape,
        order=0, mode="constant", cval=0, prefilter=False,
    ).astype(bool)


def build_report(dataset, output, zones=None):
    output.mkdir(parents=True, exist_ok=True)
    reference = dataset["reference"]
    ref = decode_rle(reference["floor_rle"], reference["width"], reference["height"])
    records = []
    plots = []
    map_observations = []
    for run in dataset["runs"]:
        grid = run.get("grid", run)
        record = {key: run.get(key) for key in (
            "id", "status", "category", "started_at", "ended_at", "error", "reference_map_id",
        )}
        record["coverage_semantics"] = run.get("coverage_semantics", "unknown")
        if not grid or not grid.get("floor_rle"):
            record["registration"] = {"accepted": False, "confidence": 0, "reason": "missing_grid"}
            records.append(record)
            continue
        moving = decode_rle(grid["floor_rle"], grid["width"], grid["height"])
        result = register_grids(ref, moving)
        record["registration"] = result
        aligned = None
        record["coverage_eligible"] = bool(result.get("accepted") and result.get("confidence", 0) >= 0.8)
        if record["coverage_eligible"]:
            matrix = result["transform"]["matrix3x3"]
            aligned_floor = np.zeros(ref.shape, dtype=np.uint8)
            aligned_floor[warp_mask(moving == 1, matrix, ref.shape)] = 1
            aligned_floor[warp_mask(moving == 2, matrix, ref.shape)] = 2
            started = run.get("started_at") or run.get("ended_at")
            if started:
                date = datetime.fromisoformat(started.replace("Z", "+00:00")).astimezone(timezone.utc).date()
                map_observations.append({"id": run["id"], "grid": aligned_floor,
                                         "date": date.isoformat(), "quality": result["confidence"]})
        if record["coverage_eligible"] and grid.get("run_rle"):
            path = decode_rle(grid["run_rle"], grid["width"], grid["height"])
            path_values = grid.get("path_values")
            if path_values is not None:
                path_mask = np.isin(path, path_values)
            else:
                path_mask = ~np.isin(path, [128, 100, 200])
            transform = result.get("transform", result)
            matrix = transform.get("matrix3x3", transform.get("matrix"))
            if matrix is None:
                raise ValueError("Registration returned no transform matrix")
            aligned = warp_mask(path_mask, matrix, ref.shape)
            record["coverage_mask"] = aligned
        records.append(record)
        plots.append((record, moving, aligned))

    coverage = analyze_coverage(records, zones=zones, reference_shape=ref.shape)
    accepted_ids = {run["run_id"] for run in coverage["runs"]}
    write_multiday_map(output, ref, records, accepted_ids)
    candidates = [item for item in map_observations if item["id"] in accepted_ids and item["id"] != reference["id"]]
    independent, correlated = deduplicate_observations(ref, candidates)
    consensus = build_consensus(ref, independent)
    consensus["summary"]["excluded_reference_like_or_duplicate_runs"] = correlated
    write_consensus(output, ref, consensus)
    serializable = []
    for record in records:
        serializable.append({key: value for key, value in record.items() if key != "coverage_mask"})
    summary = {
        "schema_version": 1, "reference_id": reference["id"],
        "source_assumptions": dataset.get("assumptions", []),
        "semantics_source": dataset.get("semantics_source"),
        "limitations": [
            "Rigid alignment in pixels; metric scale is not verified.",
            "RunMap is a passage trace, not proof of active brushes or completed room cleaning.",
            "Registration confidence is a heuristic, not a calibrated probability.",
            "Passage ages use cycle end times; the exact time of each passage is unavailable.",
            "No robot commands are issued; no Home Assistant state is modified.",
            "Consensus is clipped to the reference frame and remains a server-side candidate.",
            "Persistent furniture cannot be distinguished from structural walls by frequency alone.",
        ],
        "runs": serializable, "coverage": coverage, "consensus": consensus["summary"],
    }
    (output / "analysis.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8",
    )

    images = []
    for index, (record, moving, aligned) in enumerate(plots):
        fig, axes = plt.subplots(1, 3, figsize=(13, 4))
        axes[0].imshow(ref, cmap="gray", interpolation="nearest")
        axes[0].set_title("Carte de référence")
        axes[1].imshow(moving, cmap="gray", interpolation="nearest")
        axes[1].set_title("Carte du cycle")
        axes[2].imshow(ref, cmap="gray", interpolation="nearest")
        transform = record["registration"].get("transform")
        if transform is not None:
            matrix = transform["matrix3x3"]
            aligned_walls = warp_mask(moving == 2, matrix, ref.shape)
            wall_overlay = np.zeros((*ref.shape, 4))
            wall_overlay[aligned_walls] = [0.9, 0.4, 0.0, 0.8]
            axes[2].imshow(wall_overlay, interpolation="nearest")
        if aligned is not None:
            overlay = np.zeros((*ref.shape, 4))
            overlay[aligned] = [0.0, 0.65, 0.8, 0.75]
            axes[2].imshow(overlay, interpolation="nearest")
        if transform is not None:
            label = "Recalage retenu" if record["coverage_eligible"] else "Candidat exclu du consensus"
            axes[2].set_title(f"Rotation {transform['angle_degrees']:+.1f}°\n{label}")
        else:
            axes[2].set_title("Aucun recalage trouvé")
        for axis in axes:
            axis.axis("off")
        fig.suptitle(str(record["id"]))
        fig.tight_layout()
        name = f"cycle-{index:02d}.png"
        fig.savefig(output / name, dpi=130)
        plt.close(fig)
        images.append((record, name))
    write_html(output, summary, images)
    return summary


def last_passages(records, shape, accepted_ids):
    """Use the same accepted evidence as the JSON, including finality checks."""
    last_seen = np.full(shape, np.nan)
    latest = None
    for record in records:
        if record["id"] not in accepted_ids or "coverage_mask" not in record:
            continue
        stamp = datetime.fromisoformat(record["ended_at"].replace("Z", "+00:00")).timestamp()
        previous = last_seen[record["coverage_mask"]]
        last_seen[record["coverage_mask"]] = np.fmax(previous, stamp)
        latest = max(latest or stamp, stamp)
    return last_seen, latest


def write_multiday_map(output, reference, records, accepted_ids):
    last_seen, latest = last_passages(records, reference.shape, accepted_ids)
    fig, axis = plt.subplots(figsize=(11, 7))
    axis.imshow(reference, cmap="gray", interpolation="nearest")
    if latest is not None:
        days = np.ma.masked_invalid((latest - last_seen) / 86400)
        plot = axis.imshow(days, cmap="viridis_r", interpolation="nearest", vmin=0)
        fig.colorbar(plot, ax=axis, label="Jours avant le dernier cycle disponible")
        day = datetime.fromtimestamp(latest, timezone.utc).date().isoformat()
        axis.set_title(f"Cycle le plus récent ayant observé un passage · référence {day}")
    else:
        axis.set_title("Aucun passage suffisamment fiable")
    axis.axis("off")
    fig.tight_layout()
    fig.savefig(output / "passages-multijours.png", dpi=140)
    plt.close(fig)


def json_default(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def encode_rle(array):
    flat = np.asarray(array).ravel()
    if not flat.size:
        return []
    changes = np.flatnonzero(flat[1:] != flat[:-1]) + 1
    starts = np.r_[0, changes]
    ends = np.r_[changes, len(flat)]
    return [[int(flat[start]), int(end - start)] for start, end in zip(starts, ends)]


def deduplicate_observations(reference, observations, similarity=0.995):
    """Conservatively avoid counting reused persistent maps as new evidence."""
    seen = [reference]
    retained, excluded = [], []
    for item in observations:
        grid = item["grid"]
        repeated = False
        for previous in seen:
            known = (grid != 0) | (previous != 0)
            if known.any() and np.mean(grid[known] == previous[known]) >= similarity:
                repeated = True
                break
        if repeated:
            excluded.append(item["id"])
        else:
            retained.append(item)
            seen.append(grid)
    return retained, excluded


def write_consensus(output, reference, consensus):
    labels = np.zeros(reference.shape, dtype=np.uint8)
    labels[np.asarray(consensus["stable_free"], dtype=bool)] = 1
    labels[np.asarray(consensus["stable_wall"], dtype=bool)] = 2
    uncertain = ~(np.asarray(consensus["stable_free"], dtype=bool)
                  | np.asarray(consensus["stable_wall"], dtype=bool)
                  | np.asarray(consensus["unknown"], dtype=bool))
    labels[uncertain] = 3
    exported = {"schema_version": 1, "width": reference.shape[1], "height": reference.shape[0],
                "label_rle": encode_rle(labels),
                "labels": {"0": "unobserved", "1": "stable_free", "2": "persistent_obstacle",
                           "3": "uncertain_or_changing"},
                "observation_days_rle": encode_rle(consensus["observation_days"]),
                "summary": consensus["summary"]}
    (output / "consensus-map.json").write_text(json.dumps(exported, ensure_ascii=False, indent=2), encoding="utf-8")
    from matplotlib.colors import ListedColormap
    fig, axes = plt.subplots(1, 3, figsize=(14, 5))
    axes[0].imshow(reference, cmap="gray", interpolation="nearest")
    axes[0].set_title("Plan de référence")
    axes[1].imshow(labels, cmap=ListedColormap(["#cbd1d2", "#f2f4f0", "#203d45", "#e9aa42"]),
                   vmin=0, vmax=3, interpolation="nearest")
    axes[1].set_title("Consensus : obstacles / libre / incertain")
    evidence = axes[2].imshow(consensus["observation_days"], cmap="Blues", interpolation="nearest", vmin=0)
    axes[2].set_title("Nombre de jours d’observation")
    fig.colorbar(evidence, ax=axes[2], shrink=0.7)
    for axis in axes:
        axis.axis("off")
    fig.suptitle("Fusion candidate · sombre : obstacle persistant · clair : libre · orange : incertain · gris : inconnu")
    fig.tight_layout()
    fig.savefig(output / "consensus.png", dpi=140)
    plt.close(fig)


def write_html(output, summary, images):
    cards = []
    for run, name in images:
        reg = run["registration"]
        accepted = bool(run.get("coverage_eligible"))
        confidence = float(reg.get("confidence", 0))
        transform = reg.get("transform")
        angle = f" · rotation {transform['angle_degrees']:+.1f}°" if transform else ""
        label = "Recalage retenu" if accepted else "Recalage incertain — couverture inconnue"
        cards.append(
            f'<article><h2>{html.escape(str(run["id"]))}</h2>'
            f'<p>{html.escape(str(run.get("status")))} · {label} · indice {confidence:.2f}{angle}</p>'
            f'<img src="{name}" alt="Comparaison du cycle avec la carte de référence"></article>'
        )
    document = '''<!doctype html><html lang="fr"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenNeato · Analyse des passages</title><style>
body{font:17px system-ui,sans-serif;background:#f4f5f1;color:#202b2b;max-width:1200px;margin:40px auto;padding:0 20px}
h1{font-size:36px;letter-spacing:-1px}p{line-height:1.6}article{background:white;border:1px solid #dce1d9;border-radius:8px;padding:20px;margin:24px 0}
img{width:100%;height:auto}a{color:#006675}.notice{border-left:4px solid #b7791f;padding:12px 20px;background:#fff6de}
</style><h1>Navigation · passages observés</h1>
<p class="notice">Prototype en lecture seule. Le cyan représente une trace de passage, pas une preuve de nettoyage.
Les cycles dont le recalage est incertain ne valident aucune pièce. Les indices sont heuristiques.</p>
<p>Orange : murs du cycle après rotation et translation proposées, y compris pour les candidats exclus du consensus.</p>
<p><a href="analysis.json">Résultats et diagnostics JSON</a></p>'''
    document += '<article><h2>Plan de consensus</h2><p>Votes par jour ; les cartes inconnues ne votent pas. '
    document += 'Les objets mobiles et les désaccords restent incertains. Plan candidat, sans écriture dans le robot.</p>'
    document += '<img src="consensus.png" alt="Plan de consensus et nombre de jours observés">'
    document += '<p><a href="consensus-map.json">Carte fusionnée en JSON</a></p></article>'
    document += '<article><h2>Passages sur plusieurs jours</h2><p>Âge de fin du cycle, relatif au dernier cycle disponible. '
    document += 'Les cycles incertains sont exclus ; les passages partiels restent visibles.</p>'
    document += '<img src="passages-multijours.png" alt="Ancienneté du dernier passage par cellule"></article>'
    document += "".join(cards)
    document += "<h2>Couverture et zones</h2><pre>" + html.escape(
        json.dumps(summary["coverage"], ensure_ascii=False, indent=2, default=json_default)
    ) + "</pre></html>"
    (output / "index.html").write_text(document, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--zones", type=Path, help="Optional polygons in reference pixel coordinates")
    parser.add_argument("--serve", action="store_true", help="Serve the report on localhost only")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    zones = json.loads(args.zones.read_text(encoding="utf-8")) if args.zones else None
    summary = build_report(dataset, args.output, zones)
    print(json.dumps({"runs": len(summary["runs"]), "report": str(args.output.resolve() / "index.html")}))
    if args.serve:
        handler = functools.partial(SimpleHTTPRequestHandler, directory=str(args.output.resolve()))
        with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
            print(f"Report: http://127.0.0.1:{args.port}", flush=True)
            server.serve_forever()


if __name__ == "__main__":
    main()
