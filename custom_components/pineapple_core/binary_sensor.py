"""Connectivity binary sensor for Pineapple Core."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import callback

from .entity import PineappleCoreEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import PineappleCoreConfigEntry
    from .coordinator import PineappleCoreCoordinator


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 — HA platform setup signature
    entry: PineappleCoreConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the connectivity sensor."""
    async_add_entities([CoreReachableSensor(entry.runtime_data)])


class CoreReachableSensor(PineappleCoreEntity, BinarySensorEntity):
    """Whether Core is currently being polled successfully."""

    # Delivery does not depend on this — reminders fire from the cached queue —
    # but it surfaces whether the queue is being kept fresh.
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: PineappleCoreCoordinator) -> None:
        """Register under the `core_reachable` translation key."""
        super().__init__(coordinator, "core_reachable")
        self._attr_is_on = coordinator.last_update_success

    @callback
    def _handle_coordinator_update(self) -> None:
        """Reflect the latest poll outcome."""
        self._attr_is_on = self.coordinator.last_update_success
        super()._handle_coordinator_update()
