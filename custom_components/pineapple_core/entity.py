"""Shared base entity for Pineapple Core diagnostics."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PineappleCoreCoordinator


class PineappleCoreEntity(CoordinatorEntity[PineappleCoreCoordinator]):
    """A diagnostic entity attached to the single Core service device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PineappleCoreCoordinator, key: str) -> None:
        """Bind to the coordinator and the service device."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry_id)},
            name="Pineapple Core",
            manufacturer="Pineapple Core",
            entry_type=DeviceEntryType.SERVICE,
        )
