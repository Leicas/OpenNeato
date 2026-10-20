"""Base entity and devices for the OpenNeato integration.

Each robot is two devices: the robot itself, and the OpenNeato bridge (the
ESP32 wired to it) that it is connected via. Each carries its own firmware:
the robot runs Neato's last official release, the bridge runs OpenNeato.
Entities sit on the device they act on.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo, format_mac
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import DOMAIN


@dataclass(frozen=True)
class OpenNeatoDevices:
    """The robot's device and its bridge's device."""

    robot: DeviceInfo
    bridge: DeviceInfo


def readable_model(raw: str | None) -> str | None:
    """Turn the robot's model code into its product name.

    "BotVacD7Connected" -> "Botvac D7 Connected", "BotVacD5" -> "Botvac D5".
    Neato spells the product line "Botvac".
    """
    if not raw:
        return raw
    name = re.sub(r"^BotVac", "Botvac", raw)
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)


def build_devices(
    serial: str,
    robot_info: dict[str, Any],
    firmware_info: dict[str, Any],
    host: str,
) -> OpenNeatoDevices:
    """Describe the robot and bridge devices from what the bridge reports.

    The bridge's hostname is the name the user gave the robot in the bridge's
    web UI, so it names both devices. Firmware older than the board fields
    leaves the bridge's maker, model and MAC empty. The robot's link to the
    bridge is set by async_register_devices, not here: Home Assistant has
    deprecated via_device in device info.
    """
    hostname = str(firmware_info.get("hostname") or "").strip()
    raw_model = robot_info.get("modelName") or None
    url = f"http://{host}"
    mac = firmware_info.get("mac")

    bridge = DeviceInfo(
        identifiers={(DOMAIN, f"{serial}_bridge")},
        name=f"{hostname} bridge" if hostname else "OpenNeato bridge",
        manufacturer=firmware_info.get("boardMaker") or None,
        model=firmware_info.get("boardName") or None,
        model_id=firmware_info.get("board") or None,
        hw_version=firmware_info.get("chip") or None,
        sw_version=firmware_info.get("version") or None,
        configuration_url=url,
    )
    if mac:
        bridge["connections"] = {(CONNECTION_NETWORK_MAC, format_mac(mac))}

    robot = DeviceInfo(
        identifiers={(DOMAIN, serial)},
        name=hostname or readable_model(raw_model) or "Neato robot",
        manufacturer="Neato Robotics",
        model=readable_model(raw_model),
        model_id=raw_model,
        sw_version=robot_info.get("softwareVersion") or None,
        serial_number=serial,
        configuration_url=url,
    )
    return OpenNeatoDevices(robot=robot, bridge=bridge)


def async_register_devices(hass: HomeAssistant, entry: ConfigEntry, devices: OpenNeatoDevices) -> None:
    """Register the bridge's device, then the robot's, connected via the bridge's."""
    registry = dr.async_get(hass)
    bridge = registry.async_get_or_create(config_entry_id=entry.entry_id, **devices.bridge)
    registry.async_get_or_create(config_entry_id=entry.entry_id, via_device_id=bridge.id, **devices.robot)


def device_for(devices: OpenNeatoDevices, description: Any) -> DeviceInfo:
    """Return the bridge's device for a description marked bridge=True, else the robot's."""
    return devices.bridge if description.bridge else devices.robot


class OpenNeatoEntity(CoordinatorEntity):
    """Base class for OpenNeato entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        serial: str,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the entity on the robot's or the bridge's device."""
        super().__init__(coordinator)
        self._serial = serial
        self._attr_device_info = device_info
