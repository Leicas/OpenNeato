"""Async HTTP client for the OpenNeato robot API."""

from __future__ import annotations

import json
import logging
import re
from asyncio import Semaphore, Task, ensure_future
from typing import Any

import aiohttp
from asyncio import timeout

from homeassistant.exceptions import HomeAssistantError

from .const import (
    MAX_CONCURRENT_REQUESTS,
    MAX_HISTORY_RESPONSE_BYTES,
    SESSION_NAME_PATTERN,
)

_LOGGER = logging.getLogger(__name__)

TIMEOUT = 30  # seconds — ESP32 can be slow when serial queue is busy

_SESSION_NAME_RE = re.compile(SESSION_NAME_PATTERN)


class OpenNeatoConnectionError(HomeAssistantError):
    """Error to indicate we cannot connect to the robot."""


class OpenNeatoApiError(HomeAssistantError):
    """Error to indicate a non-connection API failure.

    `status` carries the HTTP status when the failure came from the bridge's
    response (None for client-side refusals such as an unsafe filename), so
    the coordinator can tell "endpoint missing on this firmware" (404) from
    a transient failure.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        """Initialize with a message and optional HTTP status."""
        super().__init__(message)
        self.status = status


async def _read_json(response: aiohttp.ClientResponse) -> Any:
    """Read a response body and parse it as JSON, tolerating stray non-UTF-8 bytes.

    The firmware's jsonEscape() passes bytes >= 0x20 through verbatim, so the
    /api/version response can contain raw bytes from the robot's smart-battery
    memory (manufacturer name, serial, etc.) that aren't valid UTF-8. Replace
    those bytes rather than raising — losing one glyph is better than failing
    the whole config-entry setup.
    """
    raw = await response.read()
    return json.loads(raw.decode("utf-8", errors="replace"))


class OpenNeatoApiClient:
    """Async HTTP client for OpenNeato."""

    def __init__(self, host: str, session: aiohttp.ClientSession) -> None:
        """Initialize the API client."""
        self._host = host.rstrip("/")
        self._session = session
        self._base_url = f"http://{self._host}"
        # Coalesces concurrent get_history_session() calls for the same
        # filename into a single in-flight request. Both camera entities
        # (LIDAR map + Cleaning replay) independently fetch the same
        # completed session right after the first coordinator refresh, and
        # the ESP32 bridge — which reads history off a blocking serial link
        # to the robot — can stall indefinitely when hit with two
        # overlapping requests for the same file rather than erroring.
        # Entries are removed once the fetch completes, so this only
        # de-duplicates true concurrency, not stale caching (important for
        # in-progress "recording" sessions whose data keeps growing).
        self._history_inflight: dict[str, Task] = {}
        # Caps requests in flight against the bridge. The ESP32 serves
        # everything off one blocking serial link, and a dozen concurrent
        # GETs were enough to make it unreachable (issue #13). Waiting for
        # a slot sits outside the per-request timeout so queueing never
        # counts against the 30 s the bridge is given to answer.
        self._sem = Semaphore(MAX_CONCURRENT_REQUESTS)

    @property
    def base_url(self) -> str:
        """Return the base URL."""
        return self._base_url

    @property
    def session(self) -> aiohttp.ClientSession:
        """Return the aiohttp session."""
        return self._session

    async def _get(self, path: str) -> dict[str, Any]:
        """Perform a GET request and return parsed JSON."""
        url = f"{self._base_url}{path}"
        _LOGGER.debug("GET %s", url)
        try:
            async with self._sem:
                async with timeout(TIMEOUT):
                    async with self._session.get(url) as response:
                        _LOGGER.debug(
                            "GET %s -> %s (%s)",
                            path, response.status, response.content_type,
                        )
                        response.raise_for_status()
                        return await _read_json(response)
        except aiohttp.ClientConnectionError as err:
            _LOGGER.warning("Connection error on GET %s: %s", path, err)
            raise OpenNeatoConnectionError(
                f"Unable to connect to OpenNeato at {self._host}: {err}"
            ) from err
        except aiohttp.ClientResponseError as err:
            _LOGGER.warning("HTTP %s on GET %s: %s", err.status, path, err.message)
            raise OpenNeatoApiError(
                f"API error from {path}: {err.status} {err.message}",
                status=err.status,
            ) from err
        except TimeoutError as err:
            _LOGGER.warning("Timeout on GET %s (limit %ss)", path, TIMEOUT)
            raise OpenNeatoConnectionError(
                f"Timeout connecting to OpenNeato at {self._host}"
            ) from err

    async def _post(
        self, path: str, params: dict[str, str] | None = None
    ) -> dict[str, Any] | str:
        """Perform a POST request with optional query params."""
        url = f"{self._base_url}{path}"
        _LOGGER.debug("POST %s params=%s", url, params)
        try:
            async with self._sem:
                async with timeout(TIMEOUT):
                    async with self._session.post(url, params=params) as response:
                        _LOGGER.debug(
                            "POST %s -> %s (%s)",
                            path, response.status, response.content_type,
                        )
                        response.raise_for_status()
                        content_type = response.content_type or ""
                        if "json" in content_type:
                            return await _read_json(response)
                        return await response.text()
        except aiohttp.ClientConnectionError as err:
            _LOGGER.warning("Connection error on POST %s: %s", path, err)
            raise OpenNeatoConnectionError(
                f"Unable to connect to OpenNeato at {self._host}: {err}"
            ) from err
        except aiohttp.ClientResponseError as err:
            _LOGGER.warning("HTTP %s on POST %s: %s", err.status, path, err.message)
            raise OpenNeatoApiError(
                f"API error from POST {path}: {err.status} {err.message}",
                status=err.status,
            ) from err
        except TimeoutError as err:
            _LOGGER.warning("Timeout on POST %s (limit %ss)", path, TIMEOUT)
            raise OpenNeatoConnectionError(
                f"Timeout connecting to OpenNeato at {self._host}"
            ) from err

    async def delete_history_session(self, filename: str) -> None:
        """Delete one recorded cleaning session from the bridge.

        The firmware exposes DELETE /api/history/<name>. `filename` comes
        from the /api/history listing and is concatenated into the URL, so
        it is validated against the same strict pattern the download path
        uses — a rogue or MITM'd peer could otherwise aim the delete at an
        unrelated endpoint.
        """
        if not _SESSION_NAME_RE.match(filename):
            raise OpenNeatoApiError(f"Refusing to delete unsafe filename: {filename!r}")

        url = f"{self._base_url}/api/history/{filename}"
        _LOGGER.debug("DELETE %s", url)
        try:
            async with self._sem:
                async with timeout(TIMEOUT):
                    async with self._session.delete(url) as response:
                        _LOGGER.debug("DELETE %s -> %s", url, response.status)
                        response.raise_for_status()
        except aiohttp.ClientConnectionError as err:
            _LOGGER.warning("Connection error deleting %s: %s", filename, err)
            raise OpenNeatoConnectionError(
                f"Unable to connect to OpenNeato at {self._host}: {err}"
            ) from err
        except aiohttp.ClientResponseError as err:
            _LOGGER.warning("HTTP %s deleting %s: %s", err.status, filename, err.message)
            raise OpenNeatoApiError(
                f"API error deleting /api/history/{filename}: "
                f"{err.status} {err.message}",
                status=err.status,
            ) from err
        except TimeoutError as err:
            _LOGGER.warning("Timeout deleting %s (limit %ss)", filename, TIMEOUT)
            raise OpenNeatoConnectionError(
                f"Timeout connecting to OpenNeato at {self._host}"
            ) from err

    async def _put(self, path: str, json_data: dict[str, Any]) -> dict[str, Any]:
        """Perform a PUT request with a JSON body."""
        url = f"{self._base_url}{path}"
        _LOGGER.debug("PUT %s body=%s", url, json_data)
        try:
            async with self._sem:
                async with timeout(TIMEOUT):
                    async with self._session.put(url, json=json_data) as response:
                        _LOGGER.debug(
                            "PUT %s -> %s (%s)",
                            path, response.status, response.content_type,
                        )
                        response.raise_for_status()
                        return await _read_json(response)
        except aiohttp.ClientConnectionError as err:
            _LOGGER.warning("Connection error on PUT %s: %s", path, err)
            raise OpenNeatoConnectionError(
                f"Unable to connect to OpenNeato at {self._host}: {err}"
            ) from err
        except aiohttp.ClientResponseError as err:
            _LOGGER.warning("HTTP %s on PUT %s: %s", err.status, path, err.message)
            raise OpenNeatoApiError(
                f"API error from PUT {path}: {err.status} {err.message}",
                status=err.status,
            ) from err
        except TimeoutError as err:
            _LOGGER.warning("Timeout on PUT %s (limit %ss)", path, TIMEOUT)
            raise OpenNeatoConnectionError(
                f"Timeout connecting to OpenNeato at {self._host}"
            ) from err

    async def _delete(self, path: str) -> dict[str, Any] | str:
        """Perform a DELETE request."""
        url = f"{self._base_url}{path}"
        _LOGGER.debug("DELETE %s", url)
        try:
            async with self._sem:
                async with timeout(TIMEOUT):
                    async with self._session.delete(url) as response:
                        _LOGGER.debug(
                            "DELETE %s -> %s (%s)",
                            path, response.status, response.content_type,
                        )
                        response.raise_for_status()
                        content_type = response.content_type or ""
                        if "json" in content_type:
                            return await _read_json(response)
                        return await response.text()
        except aiohttp.ClientConnectionError as err:
            _LOGGER.warning("Connection error on DELETE %s: %s", path, err)
            raise OpenNeatoConnectionError(
                f"Unable to connect to OpenNeato at {self._host}: {err}"
            ) from err
        except aiohttp.ClientResponseError as err:
            _LOGGER.warning("HTTP %s on DELETE %s: %s", err.status, path, err.message)
            raise OpenNeatoApiError(
                f"API error from DELETE {path}: {err.status} {err.message}",
                status=err.status,
            ) from err
        except TimeoutError as err:
            _LOGGER.warning("Timeout on DELETE %s (limit %ss)", path, TIMEOUT)
            raise OpenNeatoConnectionError(
                f"Timeout connecting to OpenNeato at {self._host}"
            ) from err

    # ── GET endpoints ────────────────────────────────────────────────

    async def get_state(self) -> dict[str, Any]:
        """Get the robot's current state."""
        return await self._get("/api/state")

    async def get_schedule_next(self) -> dict[str, Any]:
        """Get the next scheduled cleaning slot and skip state."""
        return await self._get("/api/schedule/next")

    async def get_charger(self) -> dict[str, Any]:
        """Get charger / battery information."""
        return await self._get("/api/charger")

    async def get_battery_analog(self) -> dict[str, Any]:
        """Get analog battery readings (voltage, current, temperature)."""
        return await self._get("/api/analog")

    async def get_battery_warranty(self) -> dict[str, Any]:
        """Get battery warranty data (cumulative cycles, runtime)."""
        return await self._get("/api/warranty")

    async def get_error(self) -> dict[str, Any]:
        """Get current error information."""
        return await self._get("/api/error")

    async def get_firmware_version(self) -> dict[str, Any]:
        """Get firmware version info (chip, model, etc.)."""
        return await self._get("/api/firmware/version")

    async def get_robot_version(self) -> dict[str, Any]:
        """Get robot version info (serial, model name, etc.)."""
        return await self._get("/api/version")

    async def get_motors(self) -> dict[str, Any]:
        """Get motor RPM and current readings."""
        return await self._get("/api/motors")

    async def get_system(self) -> dict[str, Any]:
        """Get system information (heap, uptime, RSSI, etc.)."""
        return await self._get("/api/system")

    async def get_user_settings(self) -> dict[str, Any]:
        """Get user-facing settings (eco mode, intense clean, etc.)."""
        return await self._get("/api/user-settings")

    async def get_sensors(self) -> dict[str, Any]:
        """Get digital sensor data (dustbin, bumpers, wheel lift)."""
        return await self._get("/api/sensors")

    async def get_settings(self) -> dict[str, Any]:
        """Get full device settings."""
        return await self._get("/api/settings")

    async def get_history(self) -> list[dict[str, Any]]:
        """Get cleaning history sessions."""
        return await self._get("/api/history")  # type: ignore[return-value]

    async def get_lidar(self) -> dict[str, Any]:
        """Get the latest LDS LIDAR scan (360 points)."""
        return await self._get("/api/lidar")

    async def get_history_session(self, filename: str) -> str:
        """Download the raw JSONL data for a specific cleaning session.

        Coalesces concurrent requests for the same filename into a single
        in-flight fetch (see `_history_inflight` in __init__) — both camera
        entities can request the same session within milliseconds of each
        other at startup, and the bridge can't reliably serve two
        overlapping requests for the same file.
        """
        task = self._history_inflight.get(filename)
        if task is not None:
            _LOGGER.debug(
                "History fetch for %s already in flight, awaiting it", filename
            )
            return await task

        task = ensure_future(self._fetch_history_session(filename))
        self._history_inflight[filename] = task
        try:
            return await task
        finally:
            self._history_inflight.pop(filename, None)

    async def _fetch_history_session(self, filename: str) -> str:
        """Perform the actual HTTP fetch for a session's raw JSONL data.

        `filename` originates from the ESP32's /api/history listing and
        is concatenated into the URL, so we validate it against a strict
        pattern first — a rogue or MITM'd peer could otherwise redirect
        the request to an unrelated endpoint. The response is capped to
        MAX_HISTORY_RESPONSE_BYTES to stop a misbehaving peer from
        OOM'ing HA Core with an unbounded stream.
        """
        if not _SESSION_NAME_RE.match(filename):
            raise OpenNeatoApiError(
                f"Invalid session filename: {filename!r}"
            )
        url = f"{self._base_url}/api/history/{filename}"
        _LOGGER.debug("GET %s", url)
        try:
            async with self._sem:
                async with timeout(TIMEOUT):
                    async with self._session.get(url) as response:
                        response.raise_for_status()
                        # The firmware serves this endpoint as an HTTP chunked
                        # transfer with no Content-Length (beginChunkedResponse
                        # in web_server.cpp). On a chunked aiohttp response,
                        # content.read(n) returns as soon as ANY buffered data is
                        # available — it does NOT block until n bytes or EOF — so
                        # a single bounded read silently truncates the JSONL to
                        # the first chunk, producing a PARTIAL map. Drain the full
                        # stream to EOF (matching the frontend's res.text()),
                        # enforcing the size cap incrementally as we accumulate.
                        buf = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            buf.extend(chunk)
                            if len(buf) > MAX_HISTORY_RESPONSE_BYTES:
                                raise OpenNeatoApiError(
                                    f"Session {filename} exceeds size cap "
                                    f"({MAX_HISTORY_RESPONSE_BYTES} bytes)"
                                )
                        # Firmware emits UTF-8 JSONL; hardcode rather than
                        # call response.get_encoding(), which raises in
                        # modern aiohttp when content was streamed via
                        # response.content (the streaming path doesn't
                        # populate the response's _body buffer that
                        # get_encoding's chardet fallback needs).
                        return bytes(buf).decode("utf-8", errors="replace")
        except aiohttp.ClientConnectionError as err:
            raise OpenNeatoConnectionError(
                f"Unable to connect to OpenNeato at {self._host}: {err}"
            ) from err
        except aiohttp.ClientResponseError as err:
            raise OpenNeatoApiError(
                f"API error from /api/history/{filename}: {err.status} {err.message}",
                status=err.status,
            ) from err
        except TimeoutError as err:
            _LOGGER.warning(
                "Timeout on GET /api/history/%s (limit %ss)", filename, TIMEOUT
            )
            raise OpenNeatoConnectionError(
                f"Timeout connecting to OpenNeato at {self._host}"
            ) from err

    # ── POST endpoints ───────────────────────────────────────────────

    async def clean(self, action: str) -> dict[str, Any] | str:
        """Send a clean command.

        NeatoSerial::clean() recognises "dock", "pause", "stop" and "spot";
        every other value falls through to EVT_START_HOUSE. There is no
        "resume" action -- the robot's own state machine treats a house-clean
        event while paused as a resume, which is why async_start() sends
        "house" in both cases. Passing an unrecognised string here would
        silently start a fresh house clean instead of erroring.
        """
        return await self._post("/api/clean", params={"action": action})

    async def play_sound(self, sound_id: int) -> dict[str, Any] | str:
        """Play a sound by ID (0-20)."""
        return await self._post("/api/sound", params={"id": str(sound_id)})

    async def power(self, action: str) -> dict[str, Any] | str:
        """Send a power command (on, off, standby, shutdown)."""
        return await self._post("/api/power", params={"action": action})

    async def set_user_setting(
        self, key: str, value: str
    ) -> dict[str, Any] | str:
        """Set a single user setting via query params."""
        return await self._post(
            "/api/user-settings", params={"key": key, "value": value}
        )

    async def send_serial_command(self, cmd: str) -> str:
        """Send a raw serial command. Returns plain text."""
        result = await self._post("/api/serial", params={"cmd": cmd})
        return str(result)

    async def clear_errors(self) -> dict[str, Any] | str:
        """Clear all UI errors and alerts."""
        return await self._post("/api/clear-errors")

    async def restart(self) -> dict[str, Any] | str:
        """Restart the robot controller."""
        return await self._post("/api/system/restart")

    async def new_battery(self) -> dict[str, Any] | str:
        """Reset battery fuel-gauge calibration after physically replacing the pack."""
        return await self._post("/api/battery/new")

    async def format_fs(self) -> dict[str, Any] | str:
        """Format the filesystem."""
        return await self._post("/api/system/format-fs")

    async def skip_next_clean(self) -> dict[str, Any] | str:
        """Skip the next enabled scheduled cleaning slot."""
        return await self._post("/api/schedule/next")

    async def cancel_skip_next_clean(self) -> dict[str, Any] | str:
        """Cancel the pending skip for the next scheduled cleaning slot."""
        return await self._delete("/api/schedule/next")

    # ── PUT endpoints ────────────────────────────────────────────────

    async def update_settings(
        self, settings: dict[str, Any]
    ) -> dict[str, Any]:
        """Update device settings (JSON body). Returns full settings."""
        return await self._put("/api/settings", json_data=settings)
