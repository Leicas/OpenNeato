"""Data update coordinator for OpenNeato."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import OpenNeatoApiClient, OpenNeatoApiError, OpenNeatoConnectionError
from .const import (
    ACTIVE_UISTATE_SUBSTRINGS,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    FAST_POLL_KEYS,
    LIVE_POLL_EVERY,
    LIVE_POLL_KEYS,
    SLOW_POLL_EVERY,
    SLOW_POLL_KEYS,
)

_LOGGER = logging.getLogger(__name__)

# Every section an entity may read from coordinator.data. All of them are
# present on every cycle -- sections not polled this time round carry the
# previous value forward -- so platforms can keep using `.data.get(section)`.
ALL_KEYS: tuple[str, ...] = (
    "state", "charger", "error", "user_settings",
    "system", "settings", "motors", "history", "sensors",
    "analog", "warranty", "schedule_next",
)
# If every one of these that was polled fails, the robot is unreachable and
# the whole update fails. Non-critical endpoints (like /api/error, which can
# hang if the robot's serial interface is stuck) fail individually without
# breaking the integration. `system` is on the slow cadence now, so it no
# longer takes part.
CRITICAL_KEYS = frozenset({"state", "charger"})


def _empty(key: str) -> Any:
    """The placeholder value for a section that has never been fetched."""
    return [] if key == "history" else {}


def _rank_session_ts(session: dict[str, Any]) -> float:
    """Shared ranking key: prefer summary.time, fall back to filename epoch.

    Firmware directory iteration order isn't guaranteed, so we can't
    trust the list order. Summary.time is the clean's end timestamp;
    filenames are epoch seconds at session start.
    """
    summary = session.get("summary")
    if isinstance(summary, dict):
        raw = summary.get("time", 0)
        if isinstance(raw, (int, float)) and raw > 0:
            return float(raw)
    name = session.get("name") or ""
    try:
        return float(name.split(".", 1)[0])
    except ValueError:
        return 0.0


def latest_completed_session(history: Any) -> dict[str, Any] | None:
    """Return the most recent completed (non-recording) session entry."""
    if not isinstance(history, list):
        return None
    best: dict[str, Any] | None = None
    best_key = -1.0
    for session in history:
        if not isinstance(session, dict) or session.get("recording"):
            continue
        if not session.get("name"):
            continue
        key = _rank_session_ts(session)
        if key > best_key:
            best_key = key
            best = session
    return best


class OpenNeatoCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Single coordinator for all OpenNeato data.

    One 5 s tick drives three cadences (issue #13): the fast keys that make
    the vacuum entity responsive go out every tick, the rest once a minute,
    and while the robot is out cleaning -- or a session file is still being
    written -- history and motors go out every 15 s so the live map and the
    last-clean sensors do not lag a minute behind. Sections not polled this
    tick carry their previous value forward, so `coordinator.data` always
    holds every section.
    """

    def __init__(self, hass: HomeAssistant, api: OpenNeatoApiClient) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_POLL_INTERVAL),
        )
        self.api = api
        self._cycle = 0
        self._force_full = False
        self._was_active = False
        # Endpoints this firmware answered 404 for. Older/upstream builds
        # lack some of them; polling a missing route every minute would only
        # fill the log, so each is dropped after its first 404 until the
        # integration reloads (a firmware OTA needs a reload to be noticed).
        self.unsupported: set[str] = set()
        self._fetchers: dict[str, Callable[[], Awaitable[Any]]] = {
            "state": api.get_state,
            "charger": api.get_charger,
            "error": api.get_error,
            "user_settings": api.get_user_settings,
            "system": api.get_system,
            "settings": api.get_settings,
            "motors": api.get_motors,
            "history": api.get_history,
            "sensors": api.get_sensors,
            "analog": api.get_battery_analog,
            "warranty": api.get_battery_warranty,
            "schedule_next": api.get_schedule_next,
        }

    async def async_request_refresh(self) -> None:
        """Request a refresh that re-reads every section.

        Entities call this after a write (and the card after a delete) and
        expect the section they changed to be re-read straight away, which
        the cadence split would otherwise defer by up to a minute.
        """
        self._force_full = True
        await super().async_request_refresh()

    @staticmethod
    def _is_active(state: Any) -> bool:
        """True while the robot is cleaning, paused mid-clean or docking."""
        ui_state = (state or {}).get("uiState", "") if isinstance(state, dict) else ""
        return any(s in ui_state for s in ACTIVE_UISTATE_SUBSTRINGS)

    @staticmethod
    def _has_recording(history: Any) -> bool:
        """True while the firmware is still appending to a session file."""
        if not isinstance(history, list):
            return False
        return any(isinstance(item, dict) and item.get("recording") for item in history)

    def _select_keys(self, full: bool) -> list[str]:
        """Pick which sections to fetch this cycle."""
        if full:
            keys = list(ALL_KEYS)
        else:
            keys = list(FAST_POLL_KEYS)
            previous = self.data or {}
            if self._cycle % SLOW_POLL_EVERY == 0:
                keys.extend(SLOW_POLL_KEYS)
            elif (
                self._is_active(previous.get("state"))
                or self._has_recording(previous.get("history"))
            ) and self._cycle % LIVE_POLL_EVERY == 0:
                keys.extend(LIVE_POLL_KEYS)
        # De-duplicate, keep order, drop what this firmware cannot serve.
        return [k for k in dict.fromkeys(keys) if k not in self.unsupported]

    def _resolve(self, key: str, result: Any, data: dict[str, Any]) -> bool:
        """Store one fetch result in `data`; return True if it failed."""
        if not isinstance(result, Exception):
            data[key] = result
            return False

        # A critical key is never marked unsupported: every firmware serves
        # /api/state and /api/charger, and dropping them would leave the
        # entities available forever on empty data with UpdateFailed unable
        # to fire. A 404 there is treated as an ordinary failure.
        if (
            isinstance(result, OpenNeatoApiError)
            and result.status == 404
            and key not in CRITICAL_KEYS
        ):
            self.unsupported.add(key)
            _LOGGER.warning(
                "Endpoint %s returned 404 — firmware does not support it; "
                "it will not be polled again until the integration reloads",
                key,
            )
            data[key] = _empty(key)
            return True

        if isinstance(result, OpenNeatoConnectionError):
            _LOGGER.warning("Timeout/connection error on %s: %s", key, result)
        else:
            _LOGGER.warning("Failed to fetch %s: %s", key, result)
        # Fall back to the previous value if we have one.
        if self.data and key in self.data:
            data[key] = self.data[key]
        else:
            data[key] = _empty(key)
        return True

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch this cycle's sections and merge them over the previous data."""
        self._cycle += 1
        # `_force_full` is only cleared once this cycle succeeds: if it
        # raises UpdateFailed the forced re-read must survive to the next
        # tick rather than wait for the slow cadence.
        full = self.data is None or self._force_full

        keys = self._select_keys(full)
        # The API client's semaphore bounds how many of these are really in
        # flight against the bridge at once.
        results = await asyncio.gather(
            *(self._fetchers[k]() for k in keys), return_exceptions=True
        )

        data: dict[str, Any] = {}
        failures: list[str] = []
        for key, result in zip(keys, results):
            if self._resolve(key, result, data):
                failures.append(key)

        # Carry forward everything not polled this cycle so every section is
        # always present (unsupported ones keep their empty placeholder).
        previous = self.data or {}
        for key in ALL_KEYS:
            if key not in data:
                data[key] = previous.get(key, _empty(key))

        # Only fail the whole coordinator if every polled critical endpoint
        # failed, so a single hung endpoint does not take the rest down.
        polled_critical = [k for k in keys if k in CRITICAL_KEYS]
        critical_failures = [k for k in failures if k in CRITICAL_KEYS]
        if polled_critical and len(critical_failures) == len(polled_critical):
            raise UpdateFailed(
                f"All critical endpoints failed: {', '.join(critical_failures)}"
            )

        # Idle <-> active transition: fetch history once right now so the
        # new `recording` entry (or the finished session's summary) reaches
        # the lidar runner, the sensors and the card without waiting for
        # the next slow or live tick.
        active = self._is_active(data["state"])
        if (
            active != self._was_active
            and "history" not in keys
            and "history" not in self.unsupported
        ):
            try:
                history = await self._fetchers["history"]()
            except Exception as err:  # noqa: BLE001 -- same handling as the gather above
                history = err
            if self._resolve("history", history, data):
                failures.append("history")
        self._was_active = active

        if failures:
            _LOGGER.debug(
                "Coordinator update succeeded with %d failed endpoints: %s",
                len(failures), ", ".join(failures),
            )

        self._force_full = False
        return data
