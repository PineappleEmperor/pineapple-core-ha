"""Inbound webhook: event routing + registration lifecycle.

Only the aiohttp transport is mocked; the real setup → webhook-register path runs,
and dispatch is exercised against actually-registered mock HA services.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pineapple_core.const import CONF_WEBHOOK_ID, DOMAIN
from custom_components.pineapple_core.coordinator import PineappleCoreCoordinator
from custom_components.pineapple_core.webhook import async_dispatch

from .conftest import BASE_URL, NOTIFY_TARGET

if TYPE_CHECKING:
    from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker


def _coordinator(
    hass: HomeAssistant, entry_data: dict, *, base_url: str = BASE_URL
) -> PineappleCoreCoordinator:
    """A real coordinator for dispatch tests — constructing one performs no I/O."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="Core", data={**entry_data, "base_url": base_url}
    )
    entry.add_to_hass(hass)
    return PineappleCoreCoordinator(hass, entry)


def _capture(hass: HomeAssistant, domain: str, service: str) -> list[ServiceCall]:
    """Register a mock service that records the calls it receives."""
    calls: list[ServiceCall] = []

    def _record(call: ServiceCall) -> None:
        calls.append(call)

    hass.services.async_register(domain, service, _record)
    return calls


async def test_dispatch_helper_sets_input_number(
    hass: HomeAssistant, entry_data: dict
) -> None:
    """A `helper` push calls input_number.set_value with the entity + value."""
    calls = _capture(hass, "input_number", "set_value")
    coord = _coordinator(hass, entry_data)
    await async_dispatch(
        hass, coord, {"event": "helper", "entity": "input_number.bin", "value": 1}
    )
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert calls[0].data == {"entity_id": "input_number.bin", "value": 1}


async def test_dispatch_reminder_and_clear_and_digest_notify(
    hass: HomeAssistant, entry_data: dict
) -> None:
    """clear/reminder/digest each fire the notify target; a clear also cancels nags."""
    calls = _capture(hass, "notify", NOTIFY_TARGET)
    coord = _coordinator(hass, entry_data)
    await async_dispatch(
        hass, coord, {"event": "reminder", "title": "T", "message": "M", "data": {"tag": "x"}}
    )
    await async_dispatch(
        hass,
        coord,
        {"event": "clear", "title": "", "message": "clear_notification", "data": {"tag": "rem-9"}},
    )
    await async_dispatch(
        hass, coord, {"event": "digest", "title": "Today", "message": "3 due", "data": {}}
    )
    await hass.async_block_till_done()
    # blocking=False notify calls complete as tasks, so assert contents, not order.
    assert sorted(c.data["message"] for c in calls) == sorted(["M", "clear_notification", "3 due"])
    # Pushed tags are namespaced too, so the clear dismisses the tag we sent under.
    assert sorted(c.data["data"].get("tag", "") for c in calls) == ["", "core_rem-9", "core_x"]
    assert "rem-9" in coord._s.handled  # the clear stopped that tag's nag chain


async def test_dispatch_unknown_event_is_ignored(
    hass: HomeAssistant, entry_data: dict
) -> None:
    """An unrecognised event triggers no service call."""
    notify = _capture(hass, "notify", NOTIFY_TARGET)
    helper = _capture(hass, "input_number", "set_value")
    await async_dispatch(hass, _coordinator(hass, entry_data), {"event": "mystery"})
    await hass.async_block_till_done()
    assert not notify
    assert not helper


async def test_pushed_reminder_actions_are_claimed_by_this_entry(
    hass: HomeAssistant, entry_data: dict
) -> None:
    """A Core-pushed reminder's action tokens become forwardable by this entry only."""
    _capture(hass, "notify", NOTIFY_TARGET)
    coord = _coordinator(hass, entry_data)
    await async_dispatch(
        hass,
        coord,
        {
            "event": "reminder",
            "title": "T",
            "message": "M",
            "data": {"tag": "push-1", "actions": [{"action": "PTOK", "title": "Done"}]},
        },
    )
    await hass.async_block_till_done()
    assert coord._s.action_tags.get("PTOK") == "push-1"


async def test_setup_registers_webhook_and_stores_id(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    mock_upcoming,
) -> None:
    """Setup mints + stores a webhook_id and unload tears the handler down."""
    mock_upcoming([])
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED

    webhook_id = mock_config_entry.data.get(CONF_WEBHOOK_ID)
    assert webhook_id  # minted + persisted
    assert webhook_id in hass.data.get("webhook", {})  # handler registered

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert webhook_id not in hass.data.get("webhook", {})  # torn down on unload
