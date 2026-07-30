"""Diagnostic sensors for the Pineapple Core delivery queue."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.util import dt as dt_util

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
    """Set up the diagnostic sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            UpcomingCountSensor(coordinator),
            NextReminderSensor(coordinator),
            WebhookUrlSensor(coordinator),
        ]
    )


class UpcomingCountSensor(PineappleCoreEntity, SensorEntity):
    """How many reminders are currently queued for delivery."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: PineappleCoreCoordinator) -> None:
        """Register under the `upcoming_count` translation key."""
        super().__init__(coordinator, "upcoming_count")
        self._update_value()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Refresh the count when the queue re-syncs."""
        self._update_value()
        super()._handle_coordinator_update()

    def _update_value(self) -> None:
        """Recompute the queue size from the coordinator data."""
        self._attr_native_value = len(self.coordinator.data or [])


class NextReminderSensor(PineappleCoreEntity, SensorEntity):
    """When the soonest queued reminder is due to fire."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: PineappleCoreCoordinator) -> None:
        """Register under the `next_reminder` translation key."""
        super().__init__(coordinator, "next_reminder")
        self._update_value()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Refresh the soonest fire time when the queue re-syncs."""
        self._update_value()
        super()._handle_coordinator_update()

    def _update_value(self) -> None:
        """Pick the earliest fire_at across the queue, or None when empty."""
        times = [dt_util.parse_datetime(r.fire_at) for r in (self.coordinator.data or [])]
        valid = [t for t in times if t is not None]
        self._attr_native_value = min(valid) if valid else None


class WebhookUrlSensor(PineappleCoreEntity, SensorEntity):
    """The inbound webhook URL to paste into Core's HA_WEBHOOK_URL.

    Static (resolved once at setup), so it just reads the coordinator's stored URL.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: PineappleCoreCoordinator) -> None:
        """Register under the `webhook_url` translation key."""
        super().__init__(coordinator, "webhook_url")
        self._attr_native_value = coordinator.webhook_url
