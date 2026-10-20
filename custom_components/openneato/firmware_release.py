"""Find the bridge firmware in the fork's GitHub releases.

Every fork release that changes more than the integration ships one firmware
file per board, named openneato-<board>-firmware.bin, and a GoReleaser
checksums.txt. Integration-only releases ship no firmware file and are
skipped, the same way the bridge's own web UI skips them (frontend/src/update.ts).
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

CHECKSUMS_ASSET = "checksums.txt"

_FIRMWARE_ASSET_RE = re.compile(r"^openneato-.+-firmware\.bin$")
_CHECKSUM_LINE_RE = re.compile(r"^([0-9a-fA-F]{64})\s+(\S+)$")


@dataclass(frozen=True)
class FirmwareRelease:
    """The newest release that carries firmware."""

    version: str
    release_url: str
    notes: str
    # URLs of this board's firmware file and of checksums.txt. None when the
    # bridge does not report its board, or the release lacks either file.
    firmware_url: str | None
    firmware_name: str | None
    checksums_url: str | None


def firmware_asset_name(board: str) -> str:
    """Return the release file name for a board, e.g. openneato-esp32-c3-firmware.bin."""
    return f"openneato-{board.lower()}-firmware.bin"


def pick_release(releases: list[dict[str, Any]], board: str | None) -> FirmwareRelease | None:
    """Return the newest published release that carries firmware, or None.

    GitHub lists releases newest first. Drafts and prereleases are skipped,
    as are integration-only releases, which carry no firmware file.
    """
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        assets = {
            asset.get("name"): asset.get("browser_download_url")
            for asset in release.get("assets", [])
        }
        if not any(name and _FIRMWARE_ASSET_RE.match(name) for name in assets):
            continue
        name = firmware_asset_name(board) if board else None
        firmware_url = assets.get(name) if name else None
        checksums_url = assets.get(CHECKSUMS_ASSET)
        installable = firmware_url is not None and checksums_url is not None
        return FirmwareRelease(
            version=str(release.get("tag_name", "")).lstrip("v"),
            release_url=str(release.get("html_url", "")),
            notes=str(release.get("body") or ""),
            firmware_url=firmware_url if installable else None,
            firmware_name=name if installable else None,
            checksums_url=checksums_url if installable else None,
        )
    return None


def parse_checksums(text: str) -> dict[str, str]:
    """Parse GoReleaser checksums.txt ("<sha256>  <file>" per line) into {file: sha256}."""
    checksums: dict[str, str] = {}
    for line in text.splitlines():
        match = _CHECKSUM_LINE_RE.match(line.strip())
        if match:
            checksums[match.group(2)] = match.group(1).lower()
    return checksums
