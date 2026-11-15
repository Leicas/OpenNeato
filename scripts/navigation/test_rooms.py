"""Private-ID validation and safe offline room-template rendering."""

from copy import deepcopy
import unittest

try:
    from .rooms import render_rooms
except ImportError:
    from rooms import render_rooms


class RoomTemplateTests(unittest.TestCase):
    def config(self):
        return {"map_id": "2020-01-02T03:04:05Z",
                "maps_url": "http://localhost/api/robot/test-robot/maps",
                "boundaries": {"cuisine": "00000000-0000-4000-8000-000000000001",
                               "chambre": "00000000-0000-4000-8000-000000000002",
                               "entree": "00000000-0000-4000-8000-000000000003"}}

    def test_render_keeps_utf8_labels_and_resolves_private_placeholders(self):
        rendered = render_rooms(self.config())
        self.assertIn("Entrée avec carte", rendered)
        self.assertIn("Chambre avec carte", rendered)
        self.assertNotIn("__MAP_ID__", rendered)
        self.assertNotIn("__BOUNDARY_", rendered)
        self.assertNotIn("Ã", rendered)

    def test_duplicate_room_ids_rejected(self):
        config = self.config()
        config["boundaries"]["chambre"] = config["boundaries"]["cuisine"]
        with self.assertRaisesRegex(ValueError, "different"):
            render_rooms(config)

    def test_credentials_are_not_accepted_in_maps_url(self):
        config = self.config()
        config["maps_url"] = "http://user:password@localhost/api/robot/test/maps"
        with self.assertRaisesRegex(ValueError, "credential-free"):
            render_rooms(config)

    def test_missing_or_extra_room_rejected(self):
        config = self.config()
        del config["boundaries"]["entree"]
        with self.assertRaises(ValueError):
            render_rooms(config)

    def test_unknown_config_fields_are_not_copied_to_output(self):
        config = self.config()
        config["unrelated_secret"] = "not-to-be-rendered"
        with self.assertRaises(ValueError):
            render_rooms(config)

    def test_uuid_and_timestamp_template_injection_rejected(self):
        for key in ("uuid", "map"):
            config = deepcopy(self.config())
            if key == "uuid":
                config["boundaries"]["cuisine"] = "{{ state_attr('x', 'y') }}"
            else:
                config["map_id"] = "{{ now() }}Z"
            with self.assertRaises(ValueError):
                render_rooms(config)


if __name__ == "__main__":
    unittest.main()
