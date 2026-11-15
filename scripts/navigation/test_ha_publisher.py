"""Offline HA publisher tests: no HA changes and no notifications are sent."""

import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from . import ha_publisher as publisher
except ImportError:
    import ha_publisher as publisher


class PublisherTests(unittest.TestCase):
    def status(self, **changes):
        status = {"state": "ready", "event_id": "event-1", "image_run_id": "run-1",
                  "notifications_allowed": True, "cycle_status": "canceled"}
        status.update(changes)
        return status

    def test_backfill_mirrors_without_notifying(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(publisher, "snapshot", return_value=(self.status(event_id="", notifications_allowed=False),
                              {"latest.png": b"image", "eink.png": b"eink"})), patch.object(publisher, "publish") as send:
                result = publisher.sync(Path(directory), "http://localhost:8765")
                self.assertEqual(result["action"], "mirrored_backfill")
                self.assertTrue((Path(directory) / "www/neato-navigation/eink.png").exists())
                send.assert_not_called()

    def test_event_sent_once_and_persisted_across_invocations(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(publisher, "snapshot", return_value=(self.status(), {"latest.png": b"image"})), \
                    patch.object(publisher, "publish", return_value="ntfy-message") as send:
                first = publisher.sync(Path(directory), "http://localhost:8765")
                second = publisher.sync(Path(directory), "http://localhost:8765")
                self.assertEqual(first["action"], "sent")
                self.assertEqual(second["action"], "already_attempted")
                self.assertEqual(second["delivered_event_id"], "event-1")
                send.assert_called_once()

    def test_ambiguous_http_failure_is_not_automatically_resent(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(publisher, "snapshot", return_value=(self.status(), {"latest.png": b"image"})), \
                    patch.object(publisher, "publish", side_effect=TimeoutError) as send:
                with self.assertRaises(TimeoutError):
                    publisher.sync(Path(directory), "http://localhost:8765")
                result = publisher.sync(Path(directory), "http://localhost:8765")
                self.assertEqual(result["action"], "already_attempted")
                self.assertEqual(result["delivered_event_id"], "")
                send.assert_called_once()
                state = json.loads((Path(directory) / ".storage/neato_navigation_publisher.json").read_text())
                self.assertEqual(state["delivery_state"], "pending")

    def test_generation_change_refuses_snapshot(self):
        with patch.object(publisher, "get_json", side_effect=[self.status(), self.status(event_id="event-2")]), \
                patch.object(publisher, "get_png", return_value=publisher.PNG_SIGNATURE):
            with self.assertRaisesRegex(ValueError, "generation"):
                publisher.snapshot("http://localhost:8765")

    def test_cross_origin_image_refused(self):
        with self.assertRaisesRegex(ValueError, "origin"):
            publisher.image_url("http://localhost:8765", "http://another-host/latest.png")

    def test_processing_does_not_fetch_images(self):
        with patch.object(publisher, "get_json", return_value=self.status(state="processing")), \
                patch.object(publisher, "get_png") as download:
            _, images = publisher.snapshot("http://localhost:8765")
            self.assertIsNone(images)
            download.assert_not_called()

    def test_binary_notification_uses_unfolded_utf8_headers(self):
        response = io.BytesIO(json.dumps({"id": "test", "attachment": {"url": "https://ntfy/file/test.png"}}).encode())
        with patch.object(publisher, "load_ntfy", return_value=("https://ntfy/topic", {})), \
                patch.object(publisher, "urlopen", return_value=response) as send:
            publisher.publish(Path("/config"), self.status(), b"image", test=True)
            request = send.call_args.args[0]
            self.assertEqual(request.data, b"image")
            self.assertIn("OpenNeatoNavigation/", request.get_header("User-agent"))
            self.assertTrue(all("\n" not in value and "\r" not in value for _, value in request.header_items()))


if __name__ == "__main__":
    unittest.main()
