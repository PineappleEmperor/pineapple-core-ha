"""HA → Core helper mirror: watched entities push their whole state to Core."""

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


async def test_state_change_pushes_whole_state(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A watched entity pushes state, attributes, unit and both timestamps."""
    _stub(aioclient_mock)
    await _setup_with_mirror(hass, entry_data, ["sensor.settle_up_balance"])

    hass.states.async_set(
        "sensor.settle_up_balance",
        "12.5",
        {"unit_of_measurement": "GBP", "counterparty": "flatmate", "friendly_name": "Balance"},
    )
    await hass.async_block_till_done()

    posts = _helper_posts(aioclient_mock)
    assert len(posts) == 1
    body = posts[0][2]
    assert body["entity"] == "sensor.settle_up_balance"
    assert body["state"] == "12.5"
    assert body["unit"] == "GBP"
    assert body["attributes"]["counterparty"] == "flatmate"
    assert body["value"] == 12.5  # non-integral stays a float
    assert body["last_changed"] and body["last_updated"]


async def test_integral_numeric_state_is_sent_as_int(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Core's helper contract expects 0|1|2 for a done-state, so 1.0 goes as 1."""
    _stub(aioclient_mock)
    await _setup_with_mirror(hass, entry_data, ["input_number.rubbish_alert"])

    hass.states.async_set("input_number.rubbish_alert", "1")
    await hass.async_block_till_done()

    body = _helper_posts(aioclient_mock)[0][2]
    assert body["value"] == 1
    assert not isinstance(body["value"], float)


async def test_text_state_is_mirrored_not_dropped(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A non-numeric, non-date state still reaches Core — it just carries no value."""
    _stub(aioclient_mock)
    await _setup_with_mirror(hass, entry_data, ["sensor.budget_period"])

    hass.states.async_set("sensor.budget_period", "august", {"remaining": 240})
    await hass.async_block_till_done()

    posts = _helper_posts(aioclient_mock)
    assert len(posts) == 1
    body = posts[0][2]
    assert body["state"] == "august"
    assert body["attributes"]["remaining"] == 240
    assert "value" not in body
    assert "next_at" not in body


async def test_unserialisable_attribute_is_skipped(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """HA's encoder coerces what it can; only a truly opaque value is dropped."""
    _stub(aioclient_mock)
    await _setup_with_mirror(hass, entry_data, ["sensor.settle_up_balance"])

    hass.states.async_set(
        "sensor.settle_up_balance",
        "3",
        {"ok": 1, "coerced": {2, 1}, "opaque": object()},
    )
    await hass.async_block_till_done()

    body = _helper_posts(aioclient_mock)[0][2]
    assert body["attributes"]["ok"] == 1
    assert sorted(body["attributes"]["coerced"]) == [1, 2]  # a set survives as a list
    assert "opaque" not in body["attributes"]  # dropped, and the push still went
    assert body["value"] == 3


async def test_initial_sync_pushes_current_state_on_setup(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A watched entity's CURRENT state is mirrored at setup, not only on change —
    so an input_number that hasn't moved since setup is still reflected in Core."""
    _stub(aioclient_mock)
    hass.states.async_set("input_number.rubbish_alert", "1")  # already set BEFORE setup
    await _setup_with_mirror(hass, entry_data, ["input_number.rubbish_alert"])
    await hass.async_block_till_done()

    posts = _helper_posts(aioclient_mock)
    assert len(posts) == 1
    assert posts[0][2]["entity"] == "input_number.rubbish_alert"
    assert posts[0][2]["value"] == 1


async def test_unknown_state_is_ignored(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """`unavailable`/`unknown` mirror nothing — they would blank a good value."""
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


async def test_iso_datetime_state_pushes_next_at(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A date-ish state (Ocado's ISO deadline) is pushed as next_at, not value."""
    _stub(aioclient_mock)
    await _setup_with_mirror(hass, entry_data, ["sensor.ocado_next_edit_deadline"])

    hass.states.async_set("sensor.ocado_next_edit_deadline", "2026-07-20T18:00:00+00:00")
    await hass.async_block_till_done()

    posts = _helper_posts(aioclient_mock)
    assert len(posts) == 1
    assert posts[0][2]["next_at"] == "2026-07-20T18:00:00+00:00"
    assert posts[0][2]["state"] == "2026-07-20T18:00:00+00:00"


async def test_bins_next_collection_attribute_pushes_iso_next_at(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """UKBinCollectionData: state is 'In N days', the DD/MM/YYYY date is in the
    next_collection attribute → mirrored as an ISO next_at."""
    _stub(aioclient_mock)
    await _setup_with_mirror(hass, entry_data, ["sensor.home_refuse_bin"])

    hass.states.async_set(
        "sensor.home_refuse_bin", "In 3 days", {"next_collection": "20/07/2026"}
    )
    await hass.async_block_till_done()

    posts = _helper_posts(aioclient_mock)
    assert len(posts) == 1
    body = posts[0][2]
    assert body["next_at"] == "2026-07-20"
    assert body["state"] == "In 3 days"
    assert body["attributes"]["next_collection"] == "20/07/2026"
