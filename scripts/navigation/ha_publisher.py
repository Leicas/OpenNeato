"""Mirror navigation images into HA and publish final-cycle pictures to ntfy.

Runs inside the existing Home Assistant container. Credentials are read from its
existing notify.ntfy configuration, never printed or copied into this module.
The command takes no templated shell arguments. --test sends one explicit test.
"""

import argparse
import base64
from email.header import Header
import json
from pathlib import Path
import sys
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import Request, urlopen


MAX_IMAGE_BYTES = 5_000_000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == content:
        return
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def get_json(url):
    with urlopen(url, timeout=10) as response:
        return json.loads(response.read(500_000))


def image_url(base, relative):
    url = urljoin(base.rstrip("/") + "/", relative)
    if urlsplit(url)[:2] != urlsplit(base)[:2]:
        raise ValueError("image origin differs from navigation service")
    return url


def get_png(url):
    with urlopen(url, timeout=10) as response:
        image = response.read(MAX_IMAGE_BYTES + 1)
    if not image.startswith(PNG_SIGNATURE) or len(image) > MAX_IMAGE_BYTES:
        raise ValueError("invalid or oversized PNG")
    return image


def snapshot(base):
    """Refuse images that span different service generations."""
    before = get_json(base.rstrip("/") + "/api/status.json")
    if before.get("state") != "ready" or not before.get("image_run_id"):
        return before, None
    latest = get_png(image_url(base, before.get("image_url", "/latest.png")))
    eink = get_png(image_url(base, before.get("eink_url", "/eink.png")))
    after = get_json(base.rstrip("/") + "/api/status.json")
    keys = ("state", "event_id", "image_run_id")
    if any(before.get(key) != after.get(key) for key in keys) or after.get("state") != "ready":
        raise ValueError("navigation generation changed during image fetch")
    return after, {"latest.png": latest, "eink.png": eink}


def load_ntfy(config_dir):
    # PyYAML is provided by Home Assistant; this is not a firmware dependency.
    import yaml

    secrets_path = config_dir / "secrets.yaml"
    secrets = yaml.safe_load(secrets_path.read_text()) if secrets_path.exists() else {}

    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor("!secret", lambda loader, node: secrets[loader.construct_scalar(node)])
    Loader.add_multi_constructor("!", lambda loader, tag, node: None)
    config = yaml.load((config_dir / "configuration.yaml").read_text(), Loader=Loader)
    matches = [item for item in config.get("notify", []) if item.get("name") == "ntfy"]
    if len(matches) != 1:
        raise ValueError("exactly one existing notify.ntfy configuration is required")
    existing = matches[0]
    topic = (existing.get("data") or {}).get("topic")
    resource = existing.get("resource")
    if not topic or not resource:
        raise ValueError("notify.ntfy resource/topic missing")
    parsed = urlsplit(resource)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.query or parsed.fragment:
        raise ValueError("invalid ntfy resource")
    headers = dict(existing.get("headers") or {})
    if not any(key.lower() == "authorization" for key in headers):
        username, password = existing.get("username"), existing.get("password")
        if username is not None and password is not None:
            credentials = base64.b64encode((str(username) + ":" + str(password)).encode()).decode()
            headers["Authorization"] = "Basic " + credentials
    return resource.rstrip("/") + "/" + quote(str(topic), safe=""), headers


def publish(config_dir, status, image, test=False):
    target, headers = load_ntfy(config_dir)
    title = "TEST carte analyse — OpenNeato" if test else "OpenNeato — fin de cycle"
    labels = {"complete": "terminé", "incomplete": "incomplet", "canceled": "interrompu", "cancelled": "interrompu"}
    cycle_status = labels.get(status.get("cycle_status"), str(status.get("cycle_status", "inconnu")))
    message = ("Test de notification avec image. Aucun nettoyage déclenché." if test else
               "Cycle " + cycle_status + ". Passages observés, nettoyage des pièces non confirmé.")
    headers.update({"Content-Type": "image/png", "X-Filename": "openneato-navigation.png",
                    "User-Agent": "OpenNeatoNavigation/1.0 (Home Assistant image publisher)",
                    "X-Title": Header(title, "utf-8", maxlinelen=1000).encode(),
                    "X-Message": Header(message, "utf-8", maxlinelen=1000).encode(), "X-Tags": "robot"})
    request = Request(target, data=image, headers=headers, method="POST")
    with urlopen(request, timeout=20) as response:
        result = json.loads(response.read(500_000))
    if not isinstance(result.get("id"), str) or not result.get("attachment", {}).get("url"):
        raise ValueError("ntfy did not confirm an image attachment")
    return result["id"]


def sync(config_dir, service_base, test=False):
    status, images = snapshot(service_base)
    if images is None:
        return {"action": "not_ready", "state": status.get("state")}
    destination = config_dir / "www" / "neato-navigation"
    for name, content in images.items():
        atomic_write(destination / name, content)
    public_status = {key: status.get(key) for key in
                     ("state", "event_id", "image_run_id", "cycle_status", "ended_at")}
    atomic_write(destination / "status.json", json.dumps(public_status).encode())
    state_path = config_dir / ".storage" / "neato_navigation_publisher.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    event_id = str(status.get("event_id") or "")
    if test:
        identifier = publish(config_dir, status, images["latest.png"], test=True)
        return {"action": "test_sent", "notification_id": identifier}
    if not event_id or status.get("notifications_allowed") is not True:
        return {"action": "mirrored_backfill", "event_id": "", "delivered_event_id": state.get("delivered_event_id", "")}
    if event_id == state.get("attempted_event_id"):
        return {"action": "already_attempted", "event_id": event_id,
                "delivered_event_id": state.get("delivered_event_id", "")}
    # At-most-once attempt: record before the HTTP request. If its response is lost,
    # leave the event pending rather than silently duplicating a push notification.
    state["attempted_event_id"] = event_id
    state["delivery_state"] = "pending"
    atomic_write(state_path, json.dumps(state).encode())
    identifier = publish(config_dir, status, images["latest.png"])
    state.update(delivered_event_id=event_id, notification_id=identifier, delivery_state="delivered")
    atomic_write(state_path, json.dumps(state).encode())
    return {"action": "sent", "event_id": event_id, "delivered_event_id": event_id,
            "notification_id": identifier}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, default=Path("/config"))
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()
    try:
        options = json.loads((args.config_dir / "neato_navigation_options.json").read_text())
        result = sync(args.config_dir, options["service_base"], test=args.test)
        print(json.dumps(result))
    except Exception as exc:
        # HTTP errors/config loaders may contain secrets; emit the class only.
        print(json.dumps({"action": "error", "error_type": type(exc).__name__}))
        sys.exit(1)


if __name__ == "__main__":
    main()
