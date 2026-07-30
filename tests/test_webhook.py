"""Inbound webhook: event routing + registration lifecycle.

Only the aiohttp transport is mocked; the real setup → webhook-register path runs,
and dispatch is exercised against actually-registered mock HA services.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pineapple_core.const import CONF_WEBHOOK_ID
from custom_components.pineapple_core.webhook import async_dispatch

from .conftest import NOTIFY_TARGET

if TYPE_CHECKING:
    from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker


class _StubCoordinator:
    """Just what async_dispatch touches: a notify target + clear→nag-cancel hook."""

    def __init__(self, target: str) -> None:
        self.notify_target = target
        self.cleared: list[str] = []

    def note_external_clear(self, tag: str) -> None:
        self.cleared.append(tag)


def _capture(hass: HomeAssistant, domain: str, service: str) -> list[ServiceCall]:
    """Register a mock service that records the calls it receives."""
    calls: list[ServiceCall] = []

    def _record(call: ServiceCall) -> None:
        calls.append(call)

    hass.services.async_register(domain, service, _record)
    return calls


async def test_dispatch_helper_sets_input_number(hass: HomeAssistant) -> None:
    """A `helper` push calls input_number.set_value with the entity + value."""
    calls = _capture(hass, "input_number", "set_value")
    coord = _StubCoordinator(NOTIFY_TARGET)
    await async_dispatch(
        hass, coord, {"event": "helper", "entity": "input_number.bin", "value": 1}
    )
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert calls[0].data == {"entity_id": "input_number.bin", "value": 1}


async def test_dispatch_reminder_and_clear_and_digest_notify(hass: HomeAssistant) -> None:
    """clear/reminder/digest each fire the notify target; a clear also cancels nags."""
    calls = _capture(hass, "notify", NOTIFY_TARGET)
    coord = _StubCoordinator(NOTIFY_TARGET)
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
    assert [c.data["message"] for c in calls] == ["M", "clear_notification", "3 due"]
    assert calls[0].data["data"] == {"tag": "x"}
    assert coord.cleared == ["rem-9"]  # the clear stopped that tag's nag chain


async def test_dispatch_unknown_event_is_ignored(hass: HomeAssistant) -> None:
    """An unrecognised event triggers no service call."""
    notify = _capture(hass, "notify", NOTIFY_TARGET)
    helper = _capture(hass, "input_number", "set_value")
    await async_dispatch(hass, _StubCoordinator(NOTIFY_TARGET), {"event": "mystery"})
    await hass.async_block_till_done()
    assert not notify
    assert not helper


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
