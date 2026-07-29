"""The Pineapple Core integration — local delivery of Core's reminders."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform

from .coordinator import PineappleCoreCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

type PineappleCoreConfigEntry = ConfigEntry[PineappleCoreCoordinator]

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: PineappleCoreConfigEntry) -> bool:
    """Set up Pineapple Core from a config entry."""
    coordinator = PineappleCoreCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PineappleCoreConfigEntry) -> bool:
    """Unload a config entry, cancelling every armed fire callback first."""
    entry.runtime_data.async_cancel_scheduled()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(hass: HomeAssistant, entry: PineappleCoreConfigEntry) -> None:
    """Reload the entry when its options change (poll interval, window)."""
    await hass.config_entries.async_reload(entry.entry_id)
