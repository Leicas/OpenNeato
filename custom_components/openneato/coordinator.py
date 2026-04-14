"""Data update coordinator for OpenNeato."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import OpenNeatoApiClient, OpenNeatoConnectionError
from .const import DEFAULT_POLL_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class OpenNeatoCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Single coordinator for all OpenNeato data."""

    def __init__(self, hass: HomeAssistant, api: OpenNeatoApiClient) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_POLL_INTERVAL),
        )
        self.api = api

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch all data concurrently."""
        results = await asyncio.gather(
            self.api.get_state(),
            self.api.get_charger(),
            self.api.get_error(),
            self.api.get_user_settings(),
            self.api.get_system(),
            self.api.get_settings(),
            self.api.get_motors(),
            self.api.get_history(),
            self.api.get_sensors(),
            return_exceptions=True,
        )

        keys = (
            "state", "charger", "error", "user_settings",
            "system", "settings", "motors", "history", "sensors",
        )
        # Critical endpoints — if ALL of these fail we consider the robot
        # unreachable. Non-critical endpoints (like /api/error, which can hang
        # if the robot's serial interface is stuck) are allowed to fail
        # individually without breaking the integration.
        critical_keys = {"state", "charger", "system"}

        data: dict[str, Any] = {}
        failures: list[str] = []
        critical_failures: list[str] = []

        for key, result in zip(keys, results):
            if isinstance(result, Exception):
                if isinstance(result, OpenNeatoConnectionError):
                    _LOGGER.warning("Timeout/connection error on %s: %s", key, result)
                else:
                    _LOGGER.warning("Failed to fetch %s: %s", key, result)
                failures.append(key)
                if key in critical_keys:
                    critical_failures.append(key)
                # Fall back to previous value if we have one
                if self.data and key in self.data:
                    data[key] = self.data[key]
                else:
                    data[key] = {} if key != "history" else []
            else:
                data[key] = result

        # Only fail the whole coordinator if ALL critical endpoints failed.
        # This means a single hung endpoint (e.g. /api/error when the robot's
        # serial interface gets stuck) doesn't break the rest of the
        # integration.
        if critical_failures and len(critical_failures) == len(critical_keys):
            raise UpdateFailed(
                f"All critical endpoints failed: {', '.join(critical_failures)}"
            )

        if failures:
            _LOGGER.debug(
                "Coordinator update succeeded with %d failed endpoints: %s",
                len(failures), ", ".join(failures),
            )

        return data
