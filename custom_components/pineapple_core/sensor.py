"""Diagnostic sensors for the Pineapple Core delivery queue."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import PineappleCoreConfigEntry
from .entity import PineappleCoreEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PineappleCoreConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the diagnostic sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            UpcomingCountSensor(coordinator),
            NextReminderSensor(coordinator),
        ]
    )


class UpcomingCountSensor(PineappleCoreEntity, SensorEntity):
    """How many reminders are currently queued for delivery."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator) -> None:  # noqa: ANN001
        """Register under the `upcoming_count` translation key."""
        super().__init__(coordinator, "upcoming_count")

    @property
    def native_value(self) -> int:
        """The size of the current queue."""
        return len(self.coordinator.data or [])


class NextReminderSensor(PineappleCoreEntity, SensorEntity):
    """When the soonest queued reminder is due to fire."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator) -> None:  # noqa: ANN001
        """Register under the `next_reminder` translation key."""
        super().__init__(coordinator, "next_reminder")

    @property
    @callback
    def native_value(self):  # noqa: ANN201
        """The earliest fire_at across the queue, or None when empty."""
        times = [
            dt_util.parse_datetime(r.fire_at)
            for r in (self.coordinator.data or [])
        ]
        valid = [t for t in times if t is not None]
        return min(valid) if valid else None
