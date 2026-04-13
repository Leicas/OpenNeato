"""Camera entity exposing the LIDAR map for OpenNeato.

Provides a standard HA camera entity that renders the robot's map as a PNG
image.  Compatible with vacuum-card, picture-entity, and other Lovelace cards
that consume camera entities.

Adaptive content based on robot state:
  - Cleaning / manual mode: live 360-degree LIDAR scan (polled every 2 s).
  - Docked / idle: most recent completed cleaning session map (path + coverage).
  - No history: placeholder grid image.

Rendering runs in the executor to avoid blocking the HA event loop.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .api import OpenNeatoApiClient
from .const import DOMAIN, HISTORY_IMAGE_SIZE, LIDAR_POLL_INTERVAL, UISTATE_SUBSTRINGS
from .entity import OpenNeatoEntity
from .history_renderer import parse_session_jsonl, render_history_map
from .lidar_renderer import ScanAccumulator, render_idle_image, render_lidar_scan

_LOGGER = logging.getLogger(__name__)

# uiState substrings that indicate the LDS motor is spinning
_ACTIVE_SUBSTRINGS = {sub for sub, _ in UISTATE_SUBSTRINGS}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the OpenNeato LIDAR map camera from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            OpenNeatoLidarCamera(
                coordinator=data["coordinator"],
                api=data["api"],
                serial=data["serial"],
                model=data["model"],
                sw_version=data["sw_version"],
                fw_version=data["fw_version"],
                host=data["host"],
            )
        ]
    )


class OpenNeatoLidarCamera(OpenNeatoEntity, Camera):
    """Camera entity showing the live LIDAR scan or last cleaning session map."""

    _attr_translation_key = "lidar_map"
    _attr_frame_interval = 5.0
    _attr_content_type = "image/png"

    def __init__(
        self,
        coordinator,
        api: OpenNeatoApiClient,
        serial: str,
        model: str | None = None,
        sw_version: str | None = None,
        fw_version: str | None = None,
        host: str | None = None,
    ) -> None:
        """Initialize the LIDAR map camera."""
        OpenNeatoEntity.__init__(
            self, coordinator, serial,
            model=model, sw_version=sw_version, fw_version=fw_version, host=host,
        )
        Camera.__init__(self)
        self._api = api
        self._attr_unique_id = f"{serial}_lidar_map"

        # LIDAR scan state
        self._accumulator = ScanAccumulator()
        self._lidar_image: bytes | None = None
        self._poll_unsub: CALLBACK_TYPE | None = None
        self._polling_active = False

        # History map state
        self._history_image: bytes | None = None
        self._history_session_name: str | None = None  # track which session is cached

        # Idle placeholder
        self._idle_image: bytes | None = None

        # Current display mode
        self._map_source: str = "idle"  # "lidar" | "history" | "idle"

        # LIDAR diagnostics exposed as attributes
        self._rotation_speed: float | None = None
        self._valid_points: int | None = None

        # History diagnostics
        self._session_mode: str | None = None
        self._session_duration: int | None = None
        self._session_area: float | None = None

    # ── Properties ───────────────────────────────────────────────────

    @property
    def is_on(self) -> bool:
        """Return True when a map image is available."""
        return self._lidar_image is not None or self._history_image is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return map diagnostics."""
        attrs: dict[str, Any] = {"map_source": self._map_source}
        if self._map_source == "lidar":
            if self._rotation_speed is not None:
                attrs["rotation_speed"] = round(self._rotation_speed, 1)
            if self._valid_points is not None:
                attrs["valid_points"] = self._valid_points
                attrs["scan_quality"] = round(self._valid_points / 360 * 100)
        elif self._map_source == "history":
            if self._history_session_name:
                attrs["session_name"] = self._history_session_name
            if self._session_mode:
                attrs["session_mode"] = self._session_mode
            if self._session_duration is not None:
                attrs["session_duration"] = self._session_duration
            if self._session_area is not None:
                attrs["session_area"] = round(self._session_area, 2)
        return attrs

    # ── Camera interface ─────────────────────────────────────────────

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the current map image based on robot state."""
        # Active LIDAR takes priority
        if self._polling_active and self._lidar_image is not None:
            return self._lidar_image

        # Fall back to history map
        if self._history_image is not None:
            return self._history_image

        # Last resort: LIDAR image from before robot stopped
        if self._lidar_image is not None:
            return self._lidar_image

        # Placeholder
        if self._idle_image is None:
            self._idle_image = await self.hass.async_add_executor_job(
                render_idle_image
            )
        return self._idle_image

    # ── Lifecycle ────────────────────────────────────────────────────

    async def async_added_to_hass(self) -> None:
        """Start listening for coordinator updates."""
        await super().async_added_to_hass()
        self._check_polling_state()
        # Try to load the most recent history map on startup
        self.hass.async_create_task(self._async_update_history_map())

    async def async_will_remove_from_hass(self) -> None:
        """Clean up the poll timer."""
        self._stop_polling()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        """React to coordinator data changes — manage LIDAR polling and history."""
        was_active = self._polling_active
        self._check_polling_state()

        # When robot transitions from active → idle, load the history map
        if was_active and not self._polling_active:
            self._map_source = "history"
            self.hass.async_create_task(self._async_update_history_map())

        super()._handle_coordinator_update()

    # ── LIDAR polling lifecycle ──────────────────────────────────────

    def _is_robot_active(self) -> bool:
        """Return True if the robot is in a state where the LDS is spinning."""
        if not self.coordinator.data:
            return False
        ui_state = self.coordinator.data.get("state", {}).get("uiState", "")
        return any(sub in ui_state for sub in _ACTIVE_SUBSTRINGS)

    @callback
    def _check_polling_state(self) -> None:
        """Start or stop LIDAR polling based on robot state."""
        should_poll = self._is_robot_active()
        if should_poll and not self._polling_active:
            self._start_polling()
        elif not should_poll and self._polling_active:
            self._stop_polling()

    @callback
    def _start_polling(self) -> None:
        """Start the periodic LIDAR fetch timer."""
        _LOGGER.debug("Starting LIDAR polling (robot is active)")
        self._polling_active = True
        self._map_source = "lidar"
        self._poll_unsub = async_track_time_interval(
            self.hass,
            self._async_poll_lidar,
            timedelta(seconds=LIDAR_POLL_INTERVAL),
        )
        # Fire immediately for first frame
        self.hass.async_create_task(self._async_poll_lidar())

    @callback
    def _stop_polling(self) -> None:
        """Cancel the periodic LIDAR fetch timer."""
        if self._poll_unsub is not None:
            _LOGGER.debug("Stopping LIDAR polling (robot is idle)")
            self._poll_unsub()
            self._poll_unsub = None
        self._polling_active = False

    async def _async_poll_lidar(self, _now=None) -> None:
        """Fetch LIDAR data from the ESP32 and re-render the image."""
        try:
            data = await self._api.get_lidar()
        except Exception:
            _LOGGER.debug("LIDAR fetch failed, keeping previous image", exc_info=True)
            return

        points = data.get("points", [])
        self._rotation_speed = data.get("rotationSpeed")
        self._valid_points = data.get("validPoints")
        self._map_source = "lidar"

        # Determine if the robot is physically moving (cleaning vs stationary)
        moving = self._is_robot_moving()

        # Merge into accumulator and render
        self._accumulator.merge(points, moving)
        snapshot = self._accumulator.snapshot()

        self._lidar_image = await self.hass.async_add_executor_job(
            render_lidar_scan, snapshot
        )
        self.async_write_ha_state()

    def _is_robot_moving(self) -> bool:
        """Return True if the robot is physically moving (not just paused)."""
        if not self.coordinator.data:
            return False
        ui_state = self.coordinator.data.get("state", {}).get("uiState", "")
        return "CLEANINGRUNNING" in ui_state or "MANUALCLEANING" in ui_state

    # ── History map ──────────────────────────────────────────────────

    def _get_latest_session(self) -> dict[str, Any] | None:
        """Find the most recent completed (non-recording) session from coordinator data."""
        if not self.coordinator.data:
            return None
        history = self.coordinator.data.get("history", [])
        if not isinstance(history, list):
            return None
        for session in history:
            # Skip sessions that are still being recorded
            if session.get("recording"):
                continue
            # Need a valid filename to download
            if session.get("name"):
                return session
        return None

    async def _async_update_history_map(self) -> None:
        """Fetch and render the most recent cleaning session map."""
        session_info = self._get_latest_session()
        if not session_info:
            if not self._polling_active:
                self._map_source = "idle"
                self.async_write_ha_state()
            return

        session_name = session_info["name"]

        # Don't re-fetch if we already have this session cached
        if session_name == self._history_session_name and self._history_image is not None:
            if not self._polling_active:
                self._map_source = "history"
                self.async_write_ha_state()
            return

        _LOGGER.debug("Fetching history session %s for map rendering", session_name)
        try:
            raw_jsonl = await self._api.get_history_session(session_name)
        except Exception:
            _LOGGER.debug(
                "Failed to fetch history session %s", session_name, exc_info=True
            )
            return

        # Parse and render in the executor
        parsed = await self.hass.async_add_executor_job(parse_session_jsonl, raw_jsonl)

        if not parsed.get("path"):
            _LOGGER.debug("Session %s has no path data", session_name)
            return

        # Check if this session is currently being recorded
        recording = session_info.get("recording", False)

        self._history_image = await self.hass.async_add_executor_job(
            render_history_map, parsed, HISTORY_IMAGE_SIZE, recording
        )

        # Cache session identity and extract metadata
        self._history_session_name = session_name
        summary = session_info.get("summary") or parsed.get("summary") or {}
        session_meta = session_info.get("session") or parsed.get("session") or {}
        self._session_mode = session_meta.get("mode") or summary.get("mode")
        self._session_duration = summary.get("duration")
        self._session_area = summary.get("areaCovered")

        if not self._polling_active:
            self._map_source = "history"
        self.async_write_ha_state()
