"""Classify the bridge firmware reported by /api/firmware/version.

The integration is written against the Leicas/OpenNeato fork firmware. The
upstream renjfk builds it descends from ship two-part `0.x` versions and lack
several endpoints the integration polls; a user who installs the HACS
integration against one gets missing entities and, worse, enough polling to
make the ESP32 unresponsive. Rather than refuse to load (the coordinator
degrades gracefully by dropping 404'd endpoints), the result of this check
raises a repair issue that tells them which firmware to flash.
"""

from __future__ import annotations

import re
from typing import Any

from .const import FORK_MIN_VERSION

# Accepts "1.12.0", "v1.11", "0.15", "0.0-abc123" -- a leading `v`, one to
# three dotted numbers, anything after that ignored.
_VERSION_RE = re.compile(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")

FIRMWARE_FORK = "fork"
FIRMWARE_DEV = "dev"
FIRMWARE_UNSUPPORTED = "unsupported"


def parse_version(raw: Any) -> tuple[int, int, int] | None:
    """Parse a firmware version string into a 3-tuple, padding with zeros.

    "1.12.0" -> (1, 12, 0); "1.11" -> (1, 11, 0); "0.0-abc" -> (0, 0, 0);
    anything that does not start with a number -> None.
    """
    if raw is None:
        return None
    match = _VERSION_RE.match(str(raw))
    if match is None:
        return None
    major, minor, patch = (int(part) if part is not None else 0 for part in match.groups())
    return (major, minor, patch)


def classify_firmware(info: dict[str, Any]) -> str:
    """Return "fork", "dev" or "unsupported" for a /api/firmware/version payload.

    Dev builds report 0.0 (optionally with a hash suffix) and are trusted:
    whoever flashed one knows what they are running. Anything that names the
    fork repository or meets FORK_MIN_VERSION is the fork; the rest -- upstream
    0.x releases and unparsable strings -- is unsupported.
    """
    parsed = parse_version(info.get("version"))
    if parsed == (0, 0, 0):
        return FIRMWARE_DEV
    repository = str(info.get("repositoryUrl", "")).lower()
    if "leicas/openneato" in repository:
        return FIRMWARE_FORK
    if parsed is not None and parsed >= FORK_MIN_VERSION:
        return FIRMWARE_FORK
    return FIRMWARE_UNSUPPORTED
