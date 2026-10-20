"""Binary sensor platform for the OpenNeato integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .entity import OpenNeatoEntity, device_for


@dataclass(frozen=True, kw_only=True)
class OpenNeatoBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describe an OpenNeato binary sensor."""

    section: str = ""
    field: str = ""
    # The bridge's own setting or reading: shown on the bridge's device.
    bridge: bool = False


BINARY_SENSOR_DESCRIPTIONS: tuple[OpenNeatoBinarySensorEntityDescription, ...] = (
    # ── Charger ─────────────────────────────────────────────────────────
    OpenNeatoBinarySensorEntityDescription(
        key="charger_charging_active",
        translation_key="charging",
        name="Charging",
        section="charger",
        field="chargingActive",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
    ),
    OpenNeatoBinarySensorEntityDescription(
        key="charger_ext_pwr_present",
        translation_key="external_power",
        name="External power",
        section="charger",
        field="extPwrPresent",
        device_class=BinarySensorDeviceClass.PLUG,
    ),
    OpenNeatoBinarySensorEntityDescription(
        key="charger_battery_over_temp",
        translation_key="battery_over_temp",
        name="Battery over temp",
        section="charger",
        field="batteryOverTemp",
        device_class=BinarySensorDeviceClass.HEAT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    OpenNeatoBinarySensorEntityDescription(
        key="charger_battery_failure",
        translation_key="battery_failure",
        name="Battery failure",
        section="charger",
        field="batteryFailure",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    OpenNeatoBinarySensorEntityDescription(
        key="charger_empty_fuel",
        translation_key="empty_fuel",
        name="Empty fuel",
        section="charger",
        field="emptyFuel",
        device_class=BinarySensorDeviceClass.BATTERY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # ── Error ───────────────────────────────────────────────────────────
    OpenNeatoBinarySensorEntityDescription(
        key="error_has_error",
        translation_key="error",
        name="Error",
        section="error",
        field="hasError",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    # ── System ──────────────────────────────────────────────────────────
    OpenNeatoBinarySensorEntityDescription(
        key="system_ntp_synced",
        bridge=True,
        translation_key="ntp_synced",
        name="NTP synced",
        section="system",
        field="ntpSynced",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # ── Digital sensors ────────────────────────────────────────────────
    OpenNeatoBinarySensorEntityDescription(
        key="sensors_dustbin_in",
        translation_key="dustbin",
        name="Dust bin",
        section="sensors",
        field="dustbinIn",
        device_class=BinarySensorDeviceClass.PRESENCE,
        icon="mdi:delete-variant",
    ),
    OpenNeatoBinarySensorEntityDescription(
        key="sensors_left_wheel_extended",
        translation_key="left_wheel_lifted",
        name="Left wheel lifted",
        section="sensors",
        field="leftWheelExtended",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:tire",
    ),
    OpenNeatoBinarySensorEntityDescription(
        key="sensors_right_wheel_extended",
        translation_key="right_wheel_lifted",
        name="Right wheel lifted",
        section="sensors",
        field="rightWheelExtended",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:tire",
    ),
    OpenNeatoBinarySensorEntityDescription(
        key="sensors_dc_jack_in",
        translation_key="dock_contact",
        # dcJackIn is the robot's own DC barrel jack, NOT the charging dock:
        # measured False while docked with 18.9 V of external power present.
        # "On dock" is extPwrPresent, which drives the External power sensor.
        name="DC jack",
        section="sensors",
        field="dcJackIn",
        device_class=BinarySensorDeviceClass.PLUG,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # ── Bumper contacts ─────────────────────────────────────────────────
    # /api/sensors already carries these six bits on every 5 s poll, so they
    # cost nothing extra to expose. The firmware itself reads them as bumpers
    # (manual_clean_manager.cpp: bumperFrontLeft = lFrontBit || lLdsBit), and
    # a stuck bumper is a common reason a robot refuses to start.
    #
    # Off by default: they toggle on every contact during a run, and the
    # recorder database is already large. Enable the ones you want to watch.
    *(
        OpenNeatoBinarySensorEntityDescription(
            key=f"sensors_{key}",
            translation_key=key,
            name=label,
            section="sensors",
            field=field,
            icon="mdi:bumper-car",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
        )
        for key, field, label in (
            ("bumper_front_left", "lFrontBit", "Bumper front left"),
            ("bumper_front_right", "rFrontBit", "Bumper front right"),
            ("bumper_side_left", "lSideBit", "Bumper side left"),
            ("bumper_side_right", "rSideBit", "Bumper side right"),
            ("bumper_lds_left", "lLdsBit", "Bumper LDS left"),
            ("bumper_lds_right", "rLdsBit", "Bumper LDS right"),
        )
    ),
)


class OpenNeatoBinarySensor(OpenNeatoEntity, BinarySensorEntity):
    """Representation of an OpenNeato binary sensor."""

    entity_description: OpenNeatoBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        serial: str,
        description: OpenNeatoBinarySensorEntityDescription,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(
            coordinator,
            serial,
            device_info,
        )
        self.entity_description = description
        self._attr_unique_id = f"{serial}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        if self.coordinator.data is None:
            return None
        section_data = self.coordinator.data.get(
            self.entity_description.section, {}
        )
        value = section_data.get(self.entity_description.field)
        if value is None:
            return None
        # The bridge reports the robot's alerts (UI_ALERT 201-242: returning to
        # base, recovering location...) as hasError with kind "warning". They
        # say what the robot is doing, not that something is wrong.
        if self.entity_description.key == "error_has_error" and section_data.get("kind") == "warning":
            return False
        return bool(value)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OpenNeato binary sensors from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    serial = data["serial"]
    devices = data["devices"]
    coordinator = data["coordinator"]

    entities: list[OpenNeatoBinarySensor] = []
    for description in BINARY_SENSOR_DESCRIPTIONS:
        entities.append(
            OpenNeatoBinarySensor(
                coordinator,
                serial,
                description,
                device_for(devices, description),
            )
        )

    async_add_entities(entities)
