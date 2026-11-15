"""Worker lifecycle regression checks using fake read-only inputs and renderers."""

import json
import logging
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

try:
    from .service import NavigationWorker
except ImportError:
    from service import NavigationWorker


def row(run_id="2026-11-14T10:00:00Z", ended="2026-11-14T11:00:00Z", status="complete"):
    return {"id": run_id, "run": {"status": status, "end_at": ended, "cleaned_area": 10}}


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.rows = [row()]
        self.now = 1794654121  # 2026-11-14 11:02:01 UTC; initial result is backfill.
        self.collect_calls = 0
        self.analyze_calls = 0
        self.fail_collection = False
        self.fail_render = False
        self.fail_read = False
        self.worker = self.new_worker()

    def read(self, base, path):
        if self.fail_read:
            raise OSError("Vacuula unreachable")
        if path == "/api/robots":
            return {"robot": {"connected": False}}
        self.assertEqual(path, "/api/robot/robot/runs")
        return self.rows

    def collect(self, base, robot, reference):
        self.collect_calls += 1
        if self.fail_collection:
            return {"runs": [], "collection_errors": [{"id": self.rows[-1]["id"], "error": "upload pending"}]}
        return {"runs": [{"id": item["id"], "status": item["run"]["status"],
                           "ended_at": item["run"]["end_at"]} for item in self.rows]}

    def analyze(self, dataset, output):
        self.analyze_calls += 1
        (output / "index.html").write_text("report", encoding="utf-8")
        return {"runs": [{**item, "registration": {"confidence": 0.91}, "coverage_eligible": True}
                          for item in dataset["runs"]]}

    def render(self, dataset, analysis, output):
        (output / "latest.png").write_bytes(b"latest")
        if self.fail_render:
            raise ValueError("render failed")
        (output / "eink.png").write_bytes(b"eink")

    def new_worker(self):
        return NavigationWorker(self.temporary.name, "http://fake", read=self.read,
                                collector=self.collect, analyzer=self.analyze, renderer=self.render,
                                clock=lambda: self.now)

    def complete_baseline(self):
        self.worker.poll_once()
        self.worker.poll_once()
        self.assertEqual(self.worker.snapshot()["state"], "ready")

    def test_initial_backfill_is_silent_and_metadata_is_deduplicated(self):
        self.complete_baseline()
        initial = self.worker.snapshot()
        self.assertEqual(initial["event_id"], "")
        self.assertFalse(initial["notifications_allowed"])
        self.worker.poll_once()
        self.assertEqual(self.analyze_calls, 1)
        self.assertEqual(self.collect_calls, 1)

    def test_render_size_change_rebuilds_without_backfill_notification(self):
        self.complete_baseline()
        with patch.dict(os.environ, {"EINK_WIDTH": "340", "EINK_HEIGHT": "220"}):
            restarted = self.new_worker()
            restarted.poll_once()
            restarted.poll_once()
            self.assertEqual(self.analyze_calls, 2)
            self.assertEqual(restarted.snapshot()["state"], "ready")
            self.assertEqual(restarted.snapshot()["event_id"], "")
            self.assertFalse(restarted.snapshot()["notifications_allowed"])

    def test_new_final_cycle_waits_two_observations_then_notifies(self):
        self.complete_baseline()
        self.rows.append(row("2026-11-14T10:30:00Z", ended="2026-11-14T11:02:01Z", status="canceled"))
        self.worker.poll_once()
        self.assertEqual(self.worker.snapshot()["state"], "processing")
        self.assertEqual(self.analyze_calls, 1)
        self.worker.poll_once()
        status = self.worker.snapshot()
        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["cycle_status"], "canceled")
        self.assertTrue(status["notifications_allowed"])
        self.assertTrue(status["event_id"])
        self.assertEqual(status["image_run_id"], self.rows[-1]["id"])
        event_id = status["event_id"]
        restarted = self.new_worker()
        restarted.poll_once()
        self.assertEqual(restarted.snapshot()["event_id"], event_id)
        self.assertEqual(self.analyze_calls, 2)

    def test_upload_error_keeps_last_generation_and_retries(self):
        self.complete_baseline()
        previous_image = self.worker.artifact("latest.png")
        self.rows.append(row("2026-11-14T10:30:00Z", ended="2026-11-14T11:02:01Z"))
        self.fail_collection = True
        self.worker.poll_once()
        with self.assertLogs(level=logging.ERROR):
            self.worker.poll_once()
        status = self.worker.snapshot()
        self.assertEqual(status["state"], "error")
        self.assertEqual(status["image_run_id"], self.rows[0]["id"])
        self.assertEqual(self.worker.artifact("latest.png"), previous_image)
        self.assertEqual(self.analyze_calls, 1)
        self.fail_collection = False
        self.worker.poll_once()
        self.assertEqual(self.worker.snapshot()["state"], "ready")

    def test_failed_render_never_publishes_partial_generation(self):
        self.complete_baseline()
        previous = self.worker.current.copy()
        self.rows.append(row("2026-11-14T10:30:00Z", ended="2026-11-14T11:02:01Z"))
        self.fail_render = True
        self.worker.poll_once()
        with self.assertLogs(level=logging.ERROR):
            self.worker.poll_once()
        self.assertEqual(self.worker.current, previous)
        self.assertFalse(list(self.worker.generations.glob(".building-*")))
        pointer = json.loads((Path(self.temporary.name) / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer, previous)

    def test_old_final_cycle_can_publish_without_second_poll(self):
        self.now += 121
        self.worker.poll_once()
        self.assertEqual(self.worker.snapshot()["state"], "ready")
        self.assertFalse(self.worker.snapshot()["notifications_allowed"])

    def test_running_cycle_does_not_claim_final_result(self):
        self.rows = [row(status="running", ended=None)]
        self.worker.poll_once()
        self.assertEqual(self.worker.snapshot()["state"], "processing")
        self.assertEqual(self.analyze_calls, 0)
        self.assertIsNone(self.worker.artifact("latest.png"))

    def test_cycle_already_running_at_startup_notifies_when_it_finishes(self):
        self.rows = [row(status="running", ended=None)]
        self.worker.poll_once()
        self.rows = [row()]
        self.worker.poll_once()
        self.worker.poll_once()
        self.assertEqual(self.worker.snapshot()["state"], "ready")
        self.assertTrue(self.worker.snapshot()["notifications_allowed"])

    def test_network_error_is_explicit(self):
        self.complete_baseline()
        self.fail_read = True
        with self.assertLogs(level=logging.ERROR):
            self.worker.poll_once()
        self.assertEqual(self.worker.snapshot()["state"], "error")
        self.assertIn("unreachable", self.worker.snapshot()["error"])
        self.fail_read = False
        self.worker.poll_once()
        self.assertEqual(self.worker.snapshot()["state"], "ready")
        self.assertEqual(self.analyze_calls, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
