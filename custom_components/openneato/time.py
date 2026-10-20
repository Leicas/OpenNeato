"""Time platform for the OpenNeato integration — the cleaning schedule.

The firmware keeps a fortnight-shaped little table in its settings: seven days,
two slots each, every slot an hour, a minute and an on/off flag.

    sched{d}Hour      sched{d}Min      sched{d}On          <- slot 1
    sched{d}Slot1Hour sched{d}Slot1Min sched{d}Slot1On     <- slot 2

`d` runs Monday=0 .. Sunday=6 — the firmware's own convention, not C's, which
starts on Sunday (see Scheduler::toSchedDay in scheduler.cpp). Getting that
wrong would silently clean on the wrong day, so it is spelled out in SCHED_DAYS
rather than derived.

Home Assistant already had the master switch for `scheduleEnabled`, which is
why the schedule could be turned on but never actually set. Each slot is
exposed here as a `time` entity for when it runs, alongside a switch in
switch.py for whether it runs at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time as dt_time
import logging

from homeassistant.components.time import TimeEntity, TimeEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import OpenNeatoApiClient
from .const import DOMAIN
from .entity import OpenNeatoEntity
from .schedule import SCHED_DAYS, SCHED_SLOTS, slot_fields

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class OpenNeatoTimeEntityDescription(TimeEntityDescription):
    """Describe an OpenNeato time entity."""

    hour_field: str
    minute_field: str


def _descriptions() -> tuple[OpenNeatoTimeEntityDescription, ...]:
    """One entity per day and slot."""
    out: list[OpenNeatoTimeEntityDescription] = []
    for day_index, day_key, day_name in SCHED_DAYS:
        for slot in range(SCHED_SLOTS):
            fields = slot_fields(day_index, slot)
            out.append(
                OpenNeatoTimeEntityDescription(
                    key=f"schedule_{day_key}_{slot + 1}",
                    translation_key=f"schedule_{day_key}_{slot + 1}",
                    name=f"Schedule {day_name} {slot + 1}",
                    hour_field=fields["hour"],
                    minute_field=fields["minute"],
                    icon="mdi:clock-outline",
                    entity_category=EntityCategory.CONFIG,
                )
            )
    return tuple(out)


TIME_DESCRIPTIONS: tuple[OpenNeatoTimeEntityDescription, ...] = _descriptions()


class OpenNeatoScheduleTime(OpenNeatoEntity, TimeEntity):
    """One slot of the robot's weekly cleaning schedule."""

    entity_description: OpenNeatoTimeEntityDescription

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        serial: str,
        description: OpenNeatoTimeEntityDescription,
        api: OpenNeatoApiClient,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the time entity."""
        super().__init__(
            coordinator,
            serial,
            device_info,
        )
        self.entity_description = description
        self._attr_unique_id = f"{serial}_{description.key}"
        self._api = api

    @property
    def native_value(self) -> dt_time | None:
        """Return the slot's time of day."""
        if self.coordinator.data is None:
            return None
        settings = self.coordinator.data.get("settings", {})
        hour = settings.get(self.entity_description.hour_field)
        minute = settings.get(self.entity_description.minute_field)
        if hour is None or minute is None:
            return None
        try:
            return dt_time(hour=int(hour), minute=int(minute))
        except (TypeError, ValueError):
            # A firmware that grew a field we cannot read is not worth an
            # unavailable entity; fall back to unknown.
            return None

    async def async_set_value(self, value: dt_time) -> None:
        """Write the slot's time of day."""
        await self._api.update_settings(
            {
                self.entity_description.hour_field: value.hour,
                self.entity_description.minute_field: value.minute,
            }
        )
        await self.coordinator.async_request_refresh()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the OpenNeato schedule times from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        OpenNeatoScheduleTime(
            data["coordinator"],
            data["serial"],
            description,
            data["api"],
            data["devices"].robot,
        )
        for description in TIME_DESCRIPTIONS
    )
