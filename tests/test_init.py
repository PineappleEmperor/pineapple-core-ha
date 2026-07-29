"""Setup / unload lifecycle tests — a real config-entry setup to LOADED.

Only the aiohttp transport is mocked; `async_setup_entry` runs end to end
(credential reads, first refresh, runtime_data, platform forward, entity
creation), which is what `test-before-setup` requires.
"""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pineapple_core.coordinator import PineappleCoreCoordinator

from .conftest import UPCOMING_URL


async def test_setup_entry_reaches_loaded(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_upcoming: Callable[[list], None],
) -> None:
    """A reachable Core takes the entry to LOADED with its entities created."""
    mock_upcoming([])
    mock_config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert isinstance(mock_config_entry.runtime_data, PineappleCoreCoordinator)

    entities = er.async_entries_for_config_entry(
        er.async_get(hass), mock_config_entry.entry_id
    )
    # 2 sensors (upcoming_count, next_reminder) + 1 binary_sensor (core_reachable)
    assert len(entities) == 3


async def test_unload_entry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_upcoming: Callable[[list], None],
) -> None:
    """Unloading a loaded entry returns it to NOT_LOADED and cancels schedules."""
    mock_upcoming(
        [
            {
                "tag": "bins-2026-08-01",
                "fire_at": "2999-01-01T08:00:00+00:00",
                "title": "Bins",
                "message": "Put the bins out",
                "data": {"tag": "bins"},
            }
        ]
    )
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = mock_config_entry.runtime_data
    assert coordinator._scheduled  # a future reminder is armed

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    assert not coordinator._scheduled  # every armed callback cancelled


async def test_setup_retry_on_connection_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A transport failure on first refresh puts the entry into SETUP_RETRY."""
    aioclient_mock.get(UPCOMING_URL, exc=TimeoutError())
    mock_config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_auth_error_triggers_reauth(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A 401 on first refresh fails setup and starts a reauth flow."""
    aioclient_mock.get(UPCOMING_URL, status=401)
    mock_config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"].get("source") == "reauth" for f in flows)


async def test_reload_on_options_update(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_upcoming: Callable[[list], None],
) -> None:
    """Updating options reloads the entry and it comes back LOADED."""
    mock_upcoming([])
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    first_coordinator = mock_config_entry.runtime_data

    hass.config_entries.async_update_entry(
        mock_config_entry, options={"poll_interval": 7, "window_hours": 4}
    )
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    # a reload builds a fresh coordinator
    assert mock_config_entry.runtime_data is not first_coordinator
