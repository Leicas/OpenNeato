"""Render manual HA room controls from validated, private map/boundary IDs."""

import argparse
from datetime import datetime
import json
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID


ROOMS = ("cuisine", "chambre", "entree")


def render_rooms(config):
    """Return UTF-8 YAML text; no network, HA actions, or credential handling."""
    if set(config) != {"map_id", "maps_url", "boundaries"}:
        raise ValueError("config requires exactly map_id, maps_url, boundaries")
    map_id = config["map_id"]
    if not isinstance(map_id, str) or not map_id.endswith("Z"):
        raise ValueError("map_id must be a UTC native map timestamp")
    try:
        datetime.fromisoformat(map_id.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid map timestamp") from exc
    url = urlsplit(config["maps_url"])
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username
            or url.password or url.query or url.fragment or not url.path.startswith("/api/robot/")
            or not url.path.endswith("/maps")):
        raise ValueError("maps_url must be a credential-free Vacuula robot maps URL")
    boundaries = config["boundaries"]
    if not isinstance(boundaries, dict) or set(boundaries) != set(ROOMS):
        raise ValueError("exactly cuisine, chambre, entree boundaries are required")
    try:
        canonical = {room: str(UUID(boundaries[room])) for room in ROOMS}
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("boundary IDs must be UUIDs") from exc
    if len(set(canonical.values())) != len(ROOMS):
        raise ValueError("each room must target a different boundary")
    text = Path(__file__).with_name("home_assistant_rooms.yaml").read_text(encoding="utf-8")
    replacements = {"__MAP_ID__": map_id, "__MAPS_URL__": config["maps_url"],
                    **{"__BOUNDARY_" + key.upper() + "__": value for key, value in canonical.items()}}
    for placeholder, value in replacements.items():
        text = text.replace(placeholder, json.dumps(value, ensure_ascii=True))
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="Private JSON IDs outside the repository")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = render_rooms(json.loads(args.config.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(result, encoding="utf-8")
    print("Rendered manual room controls; no Home Assistant action performed.")


if __name__ == "__main__":
    main()
