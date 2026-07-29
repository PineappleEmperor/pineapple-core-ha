"""Connectivity binary sensor for Pineapple Core."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PineappleCoreConfigEntry
from .entity import PineappleCoreEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PineappleCoreConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the connectivity sensor."""
    async_add_entities([CoreReachableSensor(entry.runtime_data)])


class CoreReachableSensor(PineappleCoreEntity, BinarySensorEntity):
    """Whether the last poll of Core succeeded.

    Delivery does not depend on this — reminders fire from the cached queue — but
    it surfaces whether the queue is being kept fresh.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator) -> None:  # noqa: ANN001
        """Register under the `core_reachable` translation key."""
        super().__init__(coordinator, "core_reachable")

    @property
    def is_on(self) -> bool:
        """True while Core is being polled successfully."""
        return self.coordinator.last_update_success

    @property
    def available(self) -> bool:
        """Always available — it reports reachability itself."""
        return True
