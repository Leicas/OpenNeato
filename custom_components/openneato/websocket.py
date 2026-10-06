"""WebSocket API backing the interactive replay card.

Two commands: one to list cleaning sessions, one to fetch a parsed session.
The card renders the returned data on a canvas at display refresh rate, so
all the CPU work (downloading, decompressing, building the coverage grid)
stays here on the server and happens exactly once per session.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_MAP_ROTATION_OFFSET,
    DOMAIN,
    LIVE_SESSION_TTL,
    MAP_DEFAULT_ROTATION_OFFSET,
)
from .replay import build_replay_session

_LOGGER = logging.getLogger(__name__)

# Parsed sessions are a few hundred KB each; keeping a handful means flipping
# back and forth in the session picker is instant instead of re-downloading
# from the robot every time.
CACHE_KEY = "replay_cache"
_CACHE_MAX = 4
# A session still being recorded grows, so it cannot sit in the cache above.
# It does get a short-lived copy: every dashboard with the card open polls
# the same live session, and without this each one would pull the whole
# JSONL off the bridge. Entries are (monotonic timestamp, parsed session),
# keyed like the main cache, and served while younger than LIVE_SESSION_TTL.
LIVE_CACHE_KEY = "replay_live_cache"


@callback
def async_register(hass: HomeAssistant) -> None:
    """Register the replay WebSocket commands (idempotent)."""
    websocket_api.async_register_command(hass, ws_list_sessions)
    websocket_api.async_register_command(hass, ws_get_session)
    websocket_api.async_register_command(hass, ws_delete_session)


def _resolve_entry(hass: HomeAssistant, entry_id: str | None) -> tuple[str, dict[str, Any]] | None:
    """Return (entry_id, entry_data) for the requested or only OpenNeato entry."""
    entries: dict[str, Any] = hass.data.get(DOMAIN, {})
    # hass.data[DOMAIN] also holds our own module-level scratch keys.
    candidates = {
        key: value
        for key, value in entries.items()
        if isinstance(value, dict) and "api" in value
    }
    if entry_id:
        data = candidates.get(entry_id)
        return (entry_id, data) if data else None
    if len(candidates) == 1:
        return next(iter(candidates.items()))
    return None


def _floorplan_payload(hass: HomeAssistant, entry_id: str) -> dict[str, Any] | None:
    """Expose the background so the card draws the same plan the cameras do.

    A LIDAR-built map wins over a hand-calibrated image: it is drawn in the
    robot's own frame, so its origin and scale are exact rather than fitted,
    and it carries a view rotation derived from the walls, which stands it
    upright however the robot's frame happens to be oriented.

    Either way the stored image is a server-side path the browser can't load,
    so the card fetches it through one of our HTTP views.
    """
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return None

    stored = hass.data.get(DOMAIN, {}).get(entry_id)
    mapper = stored.get("mapper") if isinstance(stored, dict) else None
    if mapper is not None and mapper.sessions:
        cal = mapper.calibration()
        if cal:
            offset = float(
                entry.options.get(CONF_MAP_ROTATION_OFFSET, MAP_DEFAULT_ROTATION_OFFSET)
            )
            return {
                # The session count busts the browser cache as the map improves.
                "url": f"/api/openneato/map/{entry_id}?v={mapper.sessions}",
                "originX": cal["origin_x"],
                "originY": cal["origin_y"],
                "rotation": 0.0,
                "scale": cal["scale"],
                "viewRotation": mapper.view_rotation(offset),
                "generated": True,
                "sessions": mapper.sessions,
            }

    # No hand-calibrated fallback any more: the plan the robot builds for
    # itself is the only one. A static image had to be aligned by hand and
    # went stale the moment the furniture moved.
    return None


@websocket_api.websocket_command(
    {
        vol.Required("type"): "openneato/sessions",
        vol.Optional("entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_list_sessions(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """List the robot's cleaning sessions, newest first."""
    resolved = _resolve_entry(hass, msg.get("entry_id"))
    if resolved is None:
        connection.send_error(msg["id"], "not_found", "No OpenNeato config entry found")
        return
    entry_id, data = resolved

    coordinator = data["coordinator"]
    history = (coordinator.data or {}).get("history")
    if not isinstance(history, list):
        connection.send_error(msg["id"], "unavailable", "No cleaning history available")
        return

    sessions = [
        {
            "name": item.get("name"),
            "size": item.get("size"),
            "recording": bool(item.get("recording")),
            "session": item.get("session"),
            "summary": item.get("summary"),
        }
        for item in history
        if isinstance(item, dict) and item.get("name")
    ]
    # The firmware returns files in directory order; sort by session start so
    # the picker reads chronologically regardless of filesystem layout.
    sessions.sort(key=lambda s: _session_start(s), reverse=True)

    connection.send_result(
        msg["id"],
        {
            "entry_id": entry_id,
            "sessions": sessions,
            "floorplan": _floorplan_payload(hass, entry_id),
        },
    )


def _session_start(session: dict[str, Any]) -> float:
    """Session start epoch, falling back to the numeric filename prefix."""
    info = session.get("session")
    if isinstance(info, dict) and info.get("time"):
        return float(info["time"])
    try:
        return float(str(session.get("name", "")).split(".", 1)[0])
    except ValueError:
        return 0.0


@websocket_api.websocket_command(
    {
        vol.Required("type"): "openneato/session",
        vol.Required("name"): str,
        vol.Optional("entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_get_session(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Download and parse one cleaning session for playback."""
    resolved = _resolve_entry(hass, msg.get("entry_id"))
    if resolved is None:
        connection.send_error(msg["id"], "not_found", "No OpenNeato config entry found")
        return
    entry_id, data = resolved
    name = msg["name"]
    coordinator = data["coordinator"]

    domain_data = hass.data.setdefault(DOMAIN, {})
    cache: dict[tuple[str, str], dict[str, Any]] = domain_data.setdefault(CACHE_KEY, {})
    cached = cache.get((entry_id, name))
    if cached is not None:
        connection.send_result(
            msg["id"],
            {**cached, "floorplan": _floorplan_payload(hass, entry_id), "recording": False},
        )
        return

    live_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = domain_data.setdefault(
        LIVE_CACHE_KEY, {}
    )
    was_recording = _is_recording(coordinator, name)
    if was_recording:
        live = live_cache.get((entry_id, name))
        if live is not None and time.monotonic() - live[0] < LIVE_SESSION_TTL:
            connection.send_result(
                msg["id"],
                {**live[1], "floorplan": _floorplan_payload(hass, entry_id), "recording": True},
            )
            return

    try:
        raw = await data["api"].get_history_session(name)
    except Exception as err:  # noqa: BLE001 -- surface any fetch failure to the card
        _LOGGER.warning("Replay: failed to fetch session %s: %s", name, err)
        connection.send_error(msg["id"], "fetch_failed", str(err))
        return

    # Serve the run in the map's frame, not the robot's frame of the day.
    runner = data.get("mapper")
    align = runner.alignment(name) if runner is not None else None

    # Coverage-grid construction is CPU-bound; keep it off the event loop.
    try:
        parsed = await hass.async_add_executor_job(
            build_replay_session, raw, name, align
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("Replay: failed to parse session %s", name)
        connection.send_error(msg["id"], "parse_failed", str(err))
        return

    if not parsed.get("path"):
        connection.send_error(msg["id"], "empty_session", "Session contains no pose data")
        return

    # Evaluated now rather than before the fetch: the session may have
    # completed while we were downloading it, and the flag the card gets
    # must describe the data it is being handed.
    recording = _is_recording(coordinator, name)
    if recording:
        live_cache[(entry_id, name)] = (time.monotonic(), parsed)
    else:
        live_cache.pop((entry_id, name), None)
        # Only completed sessions are worth keeping -- a recording one grows.
        # If it completed *during* this download the bytes we hold predate
        # the firmware closing the file (no summary), so cache nothing: the
        # card's final fetch re-downloads the closed file.
        if not was_recording:
            if len(cache) >= _CACHE_MAX:
                cache.pop(next(iter(cache)))
            cache[(entry_id, name)] = parsed

    connection.send_result(
        msg["id"],
        {**parsed, "floorplan": _floorplan_payload(hass, entry_id), "recording": recording},
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "openneato/delete_session",
        vol.Required("name"): str,
        vol.Optional("entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_delete_session(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Delete one recorded session from the robot.

    For a run that went wrong -- the robot was picked up, the LIDAR was
    blocked, the map came out half-drawn -- so the bad replay stops cluttering
    the picker.

    Note this does NOT unpick the session's contribution to the accumulated
    wall map: `LidarMap` merges hit counts, and once merged a session's cells
    are indistinguishable from every other session's. Deleting a bad session
    stops it being replayed; it does not un-draw its walls.
    """
    resolved = _resolve_entry(hass, msg.get("entry_id"))
    if resolved is None:
        connection.send_error(msg["id"], "not_found", "No OpenNeato config entry found")
        return
    entry_id, data = resolved
    name = msg["name"]

    # Refuse while the robot is still writing to it: the firmware would be
    # appending to a file we just unlinked.
    if _is_recording(data["coordinator"], name):
        connection.send_error(
            msg["id"], "recording", "That session is still being recorded"
        )
        return

    try:
        await data["api"].delete_history_session(name)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Replay: failed to delete session %s: %s", name, err)
        connection.send_error(msg["id"], "delete_failed", str(err))
        return

    # Drop it from both session caches so a later request cannot serve a
    # session the robot no longer has.
    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data.setdefault(CACHE_KEY, {}).pop((entry_id, name), None)
    domain_data.setdefault(LIVE_CACHE_KEY, {}).pop((entry_id, name), None)

    # Refresh so the picker's next listing no longer offers it. The
    # coordinator treats this as a full fetch, so history is re-read now
    # rather than on its next slow tick.
    await data["coordinator"].async_request_refresh()

    _LOGGER.info("Replay: deleted session %s", name)
    connection.send_result(msg["id"], {"deleted": name})


def _is_recording(coordinator: Any, name: str) -> bool:
    """True while the robot is still appending to this session file."""
    history = (coordinator.data or {}).get("history")
    if not isinstance(history, list):
        return False
    return any(
        isinstance(item, dict) and item.get("name") == name and item.get("recording")
        for item in history
    )
