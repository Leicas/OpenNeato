"""Camera entity exposing the LIDAR map for OpenNeato.

Provides a standard HA camera entity that renders the robot's 360-degree LDS
scan as a PNG image.  Compatible with vacuum-card, picture-entity, and other
Lovelace cards that consume camera entities.

The entity manages its own LIDAR polling lifecycle:
  - When the robot is cleaning or in manual mode, polls /api/lidar every 2 s.
  - When idle / docked, stops polling and serves the last rendered image
    (or a placeholder if no scan has ever been captured).

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
from .const import DOMAIN, LIDAR_POLL_INTERVAL, UISTATE_SUBSTRINGS
from .entity import OpenNeatoEntity
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
    """Camera entity showing the live LIDAR scan map."""

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

        self._accumulator = ScanAccumulator()
        self._image_bytes: bytes | None = None
        self._idle_image: bytes | None = None
        self._poll_unsub: CALLBACK_TYPE | None = None
        self._polling_active = False

        # LIDAR diagnostics exposed as attributes
        self._rotation_speed: float | None = None
        self._valid_points: int | None = None

    # ── Properties ───────────────────────────────────────────────────

    @property
    def is_on(self) -> bool:
        """Return True when a map image is available."""
        return self._image_bytes is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return LIDAR diagnostics."""
        attrs: dict[str, Any] = {}
        if self._rotation_speed is not None:
            attrs["rotation_speed"] = round(self._rotation_speed, 1)
        if self._valid_points is not None:
            attrs["valid_points"] = self._valid_points
            attrs["scan_quality"] = round(self._valid_points / 360 * 100)
        return attrs

    # ── Camera interface ─────────────────────────────────────────────

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the most recent rendered LIDAR scan PNG."""
        if self._image_bytes is not None:
            return self._image_bytes
        # Return a placeholder when no scan has ever been captured
        if self._idle_image is None:
            self._idle_image = await self.hass.async_add_executor_job(
                render_idle_image
            )
        return self._idle_image

    # ── Lifecycle ────────────────────────────────────────────────────

    async def async_added_to_hass(self) -> None:
        """Start listening for coordinator updates."""
        await super().async_added_to_hass()
        # Evaluate initial state
        self._check_polling_state()

    async def async_will_remove_from_hass(self) -> None:
        """Clean up the poll timer."""
        self._stop_polling()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        """React to coordinator data changes — start/stop LIDAR polling."""
        self._check_polling_state()
        super()._handle_coordinator_update()

    # ── Polling lifecycle ────────────────────────────────────────────

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

        # Determine if the robot is physically moving (cleaning vs stationary)
        moving = self._is_robot_moving()

        # Merge into accumulator and render
        self._accumulator.merge(points, moving)
        snapshot = self._accumulator.snapshot()

        self._image_bytes = await self.hass.async_add_executor_job(
            render_lidar_scan, snapshot
        )
        self.async_write_ha_state()

    def _is_robot_moving(self) -> bool:
        """Return True if the robot is physically moving (not just paused)."""
        if not self.coordinator.data:
            return False
        ui_state = self.coordinator.data.get("state", {}).get("uiState", "")
        # Moving = actively cleaning. Paused/suspended = stationary, accumulate.
        return "CLEANINGRUNNING" in ui_state or "MANUALCLEANING" in ui_state
