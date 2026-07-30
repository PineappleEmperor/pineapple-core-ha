"""HA → Core helper mirror: watched entities push their numeric state to Core."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pineapple_core.const import CONF_MIRROR_ENTITIES, DOMAIN

from .conftest import ACK_URL, BASE_URL, HELPER_URL, JSON_HEADERS, UPCOMING_URL

if TYPE_CHECKING:
    from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker


def _stub(aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(UPCOMING_URL, json={"data": {"reminders": []}}, headers=JSON_HEADERS)
    aioclient_mock.post(ACK_URL, json={"data": {"ok": True}}, headers=JSON_HEADERS)
    aioclient_mock.post(
        HELPER_URL, json={"data": {"ok": True, "matched": True}}, headers=JSON_HEADERS
    )


def _helper_posts(aioclient_mock: AiohttpClientMocker) -> list[Any]:
    return [
        c
        for c in aioclient_mock.mock_calls
        if c[0] == "POST" and str(c[1]) == HELPER_URL
    ]


async def _setup_with_mirror(
    hass: HomeAssistant, entry_data: dict[str, Any], entities: list[str]
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="Pineapple Core", data=entry_data,
        options={CONF_MIRROR_ENTITIES: entities}, unique_id=BASE_URL,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def test_numeric_state_change_pushes_helper(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A watched entity going numeric POSTs {entity, value} (integral → int)."""
    _stub(aioclient_mock)
    await _setup_with_mirror(hass, entry_data, ["input_number.rubbish_alert"])

    hass.states.async_set("input_number.rubbish_alert", "1")
    await hass.async_block_till_done()

    posts = _helper_posts(aioclient_mock)
    assert len(posts) == 1
    assert posts[0][2] == {"entity": "input_number.rubbish_alert", "value": 1}


async def test_non_numeric_state_is_ignored(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A non-numeric state (unavailable/unknown) mirrors nothing."""
    _stub(aioclient_mock)
    await _setup_with_mirror(hass, entry_data, ["input_number.rubbish_alert"])

    hass.states.async_set("input_number.rubbish_alert", "unavailable")
    await hass.async_block_till_done()

    assert not _helper_posts(aioclient_mock)


async def test_unwatched_entity_is_ignored(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Only the configured entities are mirrored."""
    _stub(aioclient_mock)
    await _setup_with_mirror(hass, entry_data, ["input_number.rubbish_alert"])

    hass.states.async_set("input_number.something_else", "1")
    await hass.async_block_till_done()

    assert not _helper_posts(aioclient_mock)
