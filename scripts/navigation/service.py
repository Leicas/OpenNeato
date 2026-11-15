"""Read-only Vacuula analysis worker and HTTP image service.

Run with ``python -m scripts.navigation.service``. Only GET requests are made
to Vacuula; notifications and Home Assistant integrations are separate clients.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

try:
    from .collect import collect, read_json
except ImportError:
    from collect import collect, read_json


LOGGER = logging.getLogger(__name__)
FINAL_STATES = {"complete", "completed", "canceled", "cancelled", "incomplete", "failed", "error"}


def utc_timestamp():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def signature(row):
    """Identity includes final metadata, not just the cycle's start timestamp."""
    return hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def default_analyze(dataset, output):
    try:
        from .analyze import build_report
    except ImportError:
        from analyze import build_report
    return build_report(dataset, output)


def default_render(dataset, analysis, output):
    try:
        from .render import render
    except ImportError:
        from render import render
    eink_size = (int(os.environ.get("EINK_WIDTH", "680")), int(os.environ.get("EINK_HEIGHT", "300")))
    return render(dataset, analysis, output, eink_size=eink_size)


class NavigationWorker:
    """One producer publishes immutable generations; HTTP only reads snapshots."""

    def __init__(self, data_dir, base_url, *, robot=None, reference=None, public_url="",
                 poll_interval=60, read=read_json, collector=collect,
                 analyzer=default_analyze, renderer=default_render, clock=time.time):
        self.data_dir = Path(data_dir)
        self.generations = self.data_dir / "generations"
        self.generations.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url.rstrip("/")
        self.robot = robot
        self.reference = reference
        self.public_url = public_url.rstrip("/")
        self.poll_interval = max(1, float(poll_interval))
        self.read, self.collector = read, collector
        self.analyzer, self.renderer, self.clock = analyzer, renderer, clock
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.pending_signature = None
        self.pending_count = 0
        self.state_file = self.data_dir / "worker-state.json"
        self.current_file = self.data_dir / "current.json"
        self.status_file = self.data_dir / "status.json"
        self.state = self._load(self.state_file, {"initialized": False, "published_signature": None})
        revision = ":".join(os.environ.get(key, default) for key, default in (
            ("PIPELINE_REVISION", "1"), ("EINK_WIDTH", "680"), ("EINK_HEIGHT", "300")))
        if self.state.get("pipeline_revision") != revision:
            self.state.update(pipeline_revision=revision, published_signature=None)
            atomic_json(self.state_file, self.state)
        self.current = self._load(self.current_file, {})
        self.status = self._load(self.status_file, self.empty_status())
        if self.current.get("generation") and not (self.generations / self.current["generation"]).is_dir():
            self.current = {}
            self.state["published_signature"] = None
        self.status.update({"state": "initializing", "error": None, "notifications_allowed": False})

    @staticmethod
    def _load(path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default.copy()

    def empty_status(self):
        return {
            "state": "initializing", "run_id": None, "ended_at": None, "cycle_status": None,
            "registration_confidence": None, "coverage_semantics": "traversed",
            "coverage_eligible": False, "updated_at": utc_timestamp(),
            "image_url": "/latest.png", "eink_url": "/eink.png", "image_run_id": None,
            "error": None, "summary_text": "En attente de données Vacuula.",
            "event_id": "", "notifications_allowed": False,
        }

    def _set_status(self, **updates):
        with self.lock:
            self.status.update(updates)
            self.status["updated_at"] = utc_timestamp()
            atomic_json(self.status_file, self.status)

    def snapshot(self):
        with self.lock:
            return self.status.copy()

    def artifact(self, name):
        with self.lock:
            generation = self.current.get("generation")
        return self.generations / generation / name if generation else None

    def _rows(self):
        robots = self.read(self.base_url, "/api/robots")
        if not self.robot:
            if len(robots) != 1:
                raise ValueError("VACUULA_ROBOT is required unless exactly one robot exists")
            self.robot = next(iter(robots))
        rows = self.read(self.base_url, "/api/robot/" + quote(self.robot, safe="") + "/runs")
        return sorted(rows, key=lambda item: item["id"])

    def _old_enough(self, row):
        ended = row.get("run", {}).get("end_at")
        if not ended:
            return False
        try:
            ended_time = datetime.fromisoformat(ended.replace("Z", "+00:00"))
            if ended_time.tzinfo is None:
                return False
            return self.clock() - ended_time.timestamp() >= 120
        except (ValueError, TypeError):
            return False

    def poll_once(self):
        """Handle one observation; errors are visible while last images survive."""
        row = None
        try:
            rows = self._rows()
            if not self.state["initialized"]:
                # Record the initial newest cycle before any costly work. This
                # baseline survives restarts and must never generate backfill alerts.
                latest = rows[-1] if rows else None
                baseline = latest["id"] if (latest and latest.get("run", {}).get("status") in FINAL_STATES
                                             and self._old_enough(latest)) else None
                self.state.update({"initialized": True, "baseline_id": baseline})
                atomic_json(self.state_file, self.state)
            if not rows:
                self._set_status(state="initializing", error=None, notifications_allowed=False,
                                 summary_text="Aucun cycle enregistré par Vacuula.")
                return
            row = rows[-1]
            meta = row.get("run", {})
            common = {"run_id": row["id"], "ended_at": meta.get("end_at"),
                      "cycle_status": meta.get("status"), "error": None}
            if meta.get("status") not in FINAL_STATES:
                self.pending_signature, self.pending_count = None, 0
                self._set_status(**common, state="processing", notifications_allowed=False,
                                 summary_text="Cycle en cours ; résultat final non disponible.")
                return
            current_signature = signature(row)
            if current_signature == self.state.get("published_signature") and self.current:
                # Restore the original publication after a transient fetch error
                # or process restart without minting another notification event.
                publication = self.current["status"].copy()
                publication.pop("updated_at", None)
                self._set_status(**publication)
                return
            if current_signature == self.pending_signature:
                self.pending_count += 1
            else:
                self.pending_signature, self.pending_count = current_signature, 1
            self._set_status(**common, state="processing", notifications_allowed=False,
                             summary_text="Cycle terminé ; attente du téléchargement stable de la carte.")
            if self.pending_count < 2 and not self._old_enough(row):
                return
            dataset = self.collector(self.base_url, self.robot, self.reference)
            if any(error.get("id") == row["id"] for error in dataset.get("collection_errors", [])):
                raise ValueError("Dernière carte pas encore disponible dans Vacuula")
            matches = [run for run in dataset["runs"] if run["id"] == row["id"]]
            if not matches:
                raise ValueError("Dernier cycle absent du jeu de cartes collecté")
            latest = matches[0]
            if latest.get("status") != meta.get("status") or latest.get("ended_at") != meta.get("end_at"):
                raise ValueError("Métadonnées du cycle modifiées pendant la collecte ; nouvel essai au prochain passage")
            self._publish(dataset, row, current_signature)
        except Exception as exc:
            LOGGER.exception("Navigation observation failed")
            self._set_status(state="error", error=type(exc).__name__ + ": " + str(exc),
                             notifications_allowed=False,
                             summary_text="Analyse indisponible ; la dernière image publiée peut être ancienne.")

    def _publish(self, dataset, row, current_signature):
        generation_name = current_signature[:20] + "-" + uuid.uuid4().hex[:8]
        staging = self.generations / (".building-" + generation_name)
        final = self.generations / generation_name
        staging.mkdir()
        try:
            analysis = self.analyzer(dataset, staging)
            self.renderer(dataset, analysis, staging)
            for required in ["index.html", "latest.png", "eink.png"]:
                if not (staging / required).is_file():
                    raise ValueError("Renderer did not produce " + required)
            record = next(run for run in analysis["runs"] if run["id"] == row["id"])
            meta = row["run"]
            confidence = record.get("registration", {}).get("confidence")
            eligible = bool(record.get("coverage_eligible", False))
            notify = row["id"] != self.state.get("baseline_id")
            status = {
                **self.empty_status(), "state": "ready", "run_id": row["id"],
                "ended_at": meta.get("end_at"), "cycle_status": meta.get("status"),
                "registration_confidence": confidence, "coverage_eligible": eligible,
                "image_run_id": row["id"], "error": None,
                "event_id": current_signature if notify else "", "notifications_allowed": notify,
                "summary_text": self._summary(meta.get("status"), confidence, eligible),
                "report_url": self.public_url + "/index.html" if self.public_url else "/index.html",
            }
            atomic_json(staging / "publication.json", status)
            os.replace(staging, final)
            # Publish the pointer last. Readers cannot see partially written images.
            with self.lock:
                current = {"generation": generation_name, "signature": current_signature, "status": status}
                atomic_json(self.current_file, current)
                self.current = current
                self._set_status(**status)
                self.state["published_signature"] = current_signature
                atomic_json(self.state_file, self.state)
            self._prune_generations()
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    @staticmethod
    def _summary(cycle_status, confidence, eligible):
        labels = {"complete": "terminé", "completed": "terminé", "canceled": "annulé",
                  "cancelled": "annulé", "incomplete": "incomplet", "failed": "échoué", "error": "en erreur"}
        label = labels.get(cycle_status, str(cycle_status))
        fit = f"Recalage {confidence:.0%}." if isinstance(confidence, (int, float)) else "Recalage indisponible."
        passage = "Passages exploitables." if eligible else "Passages exclus du suivi par pièce."
        return f"Cycle {label}. {fit} {passage} Le tracé indique des passages, pas un nettoyage confirmé."

    def _prune_generations(self):
        # Keep recent immutable files for requests that already resolved a path.
        candidates = sorted((p for p in self.generations.iterdir() if p.is_dir() and not p.name.startswith(".")),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        for old in candidates[5:]:
            if old.name != self.current.get("generation"):
                shutil.rmtree(old, ignore_errors=True)

    def run(self):
        while not self.stop_event.is_set():
            self.poll_once()
            self.stop_event.wait(self.poll_interval)


def handler_for(worker):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            if path in {"/health", "/api/status.json"}:
                body = json.dumps(worker.snapshot(), ensure_ascii=False).encode()
                return self._respond(200, body, "application/json; charset=utf-8")
            name = "index.html" if path == "/" else path.lstrip("/")
            permitted = name in {"index.html", "analysis.json", "consensus-map.json", "latest.png", "eink.png"}
            permitted = permitted or ("/" not in name and name.endswith(".png") and not name.startswith("."))
            artifact = worker.artifact(name) if permitted else None
            if artifact is None or not artifact.is_file():
                return self._respond(404, b"Artifact not available", "text/plain")
            content_type = "image/png" if name.endswith(".png") else (
                "application/json" if name.endswith(".json") else "text/html; charset=utf-8")
            try:
                self._respond(200, artifact.read_bytes(), content_type)
            except FileNotFoundError:
                self._respond(404, b"Artifact expired", "text/plain")

        def _respond(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, message, *args):
            LOGGER.debug(message, *args)

    return Handler


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    base = os.environ.get("VACUULA_URL", "http://vacuula")
    url = urlsplit(base)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
        raise ValueError("VACUULA_URL must be an HTTP(S) origin without credentials")
    worker = NavigationWorker(os.environ.get("DATA_DIR", "/data"), base,
                              robot=os.environ.get("VACUULA_ROBOT"), reference=os.environ.get("VACUULA_REFERENCE"),
                              public_url=os.environ.get("ANALYSIS_PUBLIC_URL", ""),
                              poll_interval=float(os.environ.get("POLL_INTERVAL_SECONDS", "60")))
    thread = threading.Thread(target=worker.run, name="navigation-worker", daemon=True)
    thread.start()
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8765"))), handler_for(worker))
    try:
        server.serve_forever()
    finally:
        worker.stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
