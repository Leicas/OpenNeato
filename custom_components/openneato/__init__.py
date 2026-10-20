"""The OpenNeato integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from . import websocket
from .api import OpenNeatoApiClient, OpenNeatoConnectionError
from .card import async_register_card, async_remove_card_resource
from .const import (
    CONF_HOST,
    CONF_MAP_ENABLED,
    DOMAIN,
    FORK_MIN_VERSION,
    FORK_RELEASES_URL,
    MAP_DEFAULT_ENABLED,
)
from .coordinator import OpenNeatoCoordinator
from .entity import async_register_devices, build_devices
from .firmware_check import FIRMWARE_UNSUPPORTED, classify_firmware
from .http import OpenNeatoMapView
from .lidar_runner import LidarMapRunner

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_FRONTEND_REGISTERED = "frontend_registered"

PLATFORMS: list[Platform] = [
    Platform.VACUUM,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.BUTTON,
    # Platform.CAMERA is deliberately absent. The "LIDAR map" and "Cleaning
    # replay" cameras were server-rendered PNG/GIF snapshots; the canvas card
    # in www/openneato-replay-card.js supersedes both, drawing from the
    # openneato/session(s) websocket API with pan, zoom and scrubbing.
    #
    # camera.py, lidar_renderer.py and history_renderer.py were deleted with
    # it (1347 lines). The one piece still needed, _try_repair_pose, now lives
    # in replay.py.
    Platform.SELECT,
    Platform.TEXT,
    Platform.TIME,
    Platform.UPDATE,
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up OpenNeato: serve the replay card to the dashboards.

    The card ships inside the integration rather than as a separate HACS
    resource, so the card and the WebSocket API it talks to can never fall out
    of version sync. It is registered here, not per config entry, so it is on
    the dashboards at every start even while the robot is offline.
    """
    await async_register_card(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up OpenNeato from a config entry."""
    host = entry.data[CONF_HOST]
    session = async_get_clientsession(hass)
    api = OpenNeatoApiClient(host, session)

    _LOGGER.debug("Connecting to OpenNeato at %s", host)
    try:
        firmware_info = await api.get_firmware_version()
        robot_info = await api.get_robot_version()
    except OpenNeatoConnectionError as err:
        _LOGGER.warning("Cannot connect to OpenNeato at %s: %s", host, err)
        raise ConfigEntryNotReady(
            f"Cannot connect to OpenNeato at {host}: {err}"
        ) from err
    except Exception as err:
        _LOGGER.exception("Unexpected error connecting to OpenNeato at %s", host)
        raise ConfigEntryNotReady(
            f"Unexpected error connecting to OpenNeato at {host}: {err}"
        ) from err

    _LOGGER.info(
        "Connected to OpenNeato at %s — %s (%s) firmware %s",
        host,
        robot_info.get("modelName"),
        robot_info.get("serialNumber"),
        firmware_info.get("version"),
    )

    # Upstream or pre-fork firmware keeps working (the coordinator drops the
    # endpoints it 404s on) but gets a repair issue pointing at the fork
    # build, since that is what the entities and the card are written for.
    issue_id = f"unsupported_firmware_{entry.entry_id}"
    if classify_firmware(firmware_info) == FIRMWARE_UNSUPPORTED:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="unsupported_firmware",
            translation_placeholders={
                "host": host,
                "version": str(firmware_info.get("version", "?")),
                "repository": str(firmware_info.get("repositoryUrl", "?")),
                "min_version": ".".join(str(part) for part in FORK_MIN_VERSION),
                "releases_url": FORK_RELEASES_URL,
            },
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id)

    serial = robot_info.get("serialNumber", entry.data.get("serial", "unknown"))
    # What the config flow stored fills in anything the bridge did not report.
    devices = build_devices(
        serial,
        {
            "modelName": entry.data.get("model"),
            "softwareVersion": entry.data.get("software_version"),
            **robot_info,
        },
        {"version": entry.data.get("firmware_version"), **firmware_info},
        host,
    )
    async_register_devices(hass, entry, devices)

    coordinator = OpenNeatoCoordinator(hass, api)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    await _async_register_frontend(hass)

    # Builds a floor plan from the robot's own LIDAR, one cleaning at a time.
    # The accumulated map lives in its own store so it survives restarts and
    # keeps improving instead of being rebuilt from a single run.
    mapper = None
    if entry.options.get(CONF_MAP_ENABLED, MAP_DEFAULT_ENABLED):
        mapper = LidarMapRunner(hass, entry.entry_id, api, coordinator)
        await mapper.async_load()

    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
        "mapper": mapper,
        "serial": serial,
        "devices": devices,
        "firmware_info": firmware_info,
        "host": host,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload the entry when its options change (e.g. floorplan background
    # calibration) so camera entities pick up the new config without a
    # full HA restart.
    entry.async_on_unload(entry.add_update_listener(async_reload_options))

    return True


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Register the API the replay card calls.

    Runs once per HA start, not once per config entry — the WebSocket commands
    and the map view are global.
    """
    if hass.data[DOMAIN].get(_FRONTEND_REGISTERED):
        return
    hass.data[DOMAIN][_FRONTEND_REGISTERED] = True

    websocket.async_register(hass)
    hass.http.register_view(OpenNeatoMapView())


async def async_reload_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry to apply updated options."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an OpenNeato config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        stored = hass.data[DOMAIN].pop(entry.entry_id)
        if stored.get("mapper"):
            stored["mapper"].async_unload()
        # Drop this entry's parsed replay sessions so a reload re-fetches
        # instead of serving a stale map from before a recalibration.
        for cache_key in (websocket.CACHE_KEY, websocket.LIVE_CACHE_KEY):
            cache = hass.data[DOMAIN].get(cache_key)
            if isinstance(cache, dict):
                for key in [k for k in cache if k[0] == entry.entry_id]:
                    cache.pop(key, None)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the card from the dashboards with the last OpenNeato robot."""
    others = [
        other
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id
    ]
    if not others:
        await async_remove_card_resource(hass)
