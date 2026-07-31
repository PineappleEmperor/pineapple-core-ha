"""The Pineapple Core integration — local delivery of Core's reminders."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.helpers import entity_registry as er

from .const import ACTION_EVENT, DOMAIN
from .coordinator import PineappleCoreCoordinator
from .mirror import async_setup_mirror
from .webhook import (
    async_register_webhook,
    async_remove_cloudhook,
    async_unregister_webhook,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

type PineappleCoreConfigEntry = ConfigEntry[PineappleCoreCoordinator]

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: PineappleCoreConfigEntry) -> bool:
    """Set up Pineapple Core from a config entry."""
    # One-time cleanup: the old "Inbound webhook URL" sensor was removed (it exposed
    # a secret cloudhook), but HA keeps its registry entry as an orphaned
    # 'unavailable' entity the user can't easily delete. Drop it here.
    registry = er.async_get(hass)
    stale = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_webhook_url")
    if stale:
        registry.async_remove(stale)

    coordinator = PineappleCoreCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    # Inbound webhook: Core pushes clears/helpers/digest here (replaces `notify - core`).
    coordinator.webhook_url = await async_register_webhook(hass, entry)
    entry.async_on_unload(lambda: async_unregister_webhook(hass, entry))
    _LOGGER.info("Pineapple Core inbound webhook registered")
    # The URL is a Nabu Casa cloudhook — a secret. Emit it at DEBUG only (never a
    # visible sensor), so it can be retrieved from logs when needed without exposing it.
    _LOGGER.debug("Inbound webhook URL (for Core's HA_WEBHOOK_URL): %s", coordinator.webhook_url)

    # A tapped notification button forwards to Core + cancels the local nag chain.
    entry.async_on_unload(
        hass.bus.async_listen(ACTION_EVENT, coordinator.handle_action_event)
    )
    # Mirror the configured HA entities' numeric state back to Core (replaces
    # `callback - bins status to core`). Re-armed on options change via reload.
    entry.async_on_unload(async_setup_mirror(hass, entry, coordinator.client))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PineappleCoreConfigEntry) -> bool:
    """Unload a config entry, cancelling every armed fire callback first."""
    entry.runtime_data.async_cancel_scheduled()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: PineappleCoreConfigEntry) -> None:
    """On full removal, delete the cloudhook we created (reload keeps it)."""
    await async_remove_cloudhook(hass, entry)


async def _async_reload_entry(hass: HomeAssistant, entry: PineappleCoreConfigEntry) -> None:
    """Reload the entry when its options change (poll interval, window)."""
    await hass.config_entries.async_reload(entry.entry_id)
