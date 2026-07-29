"""Coordinator delivery behaviour: fire-on-time, dedup, ack retry.

Time-based firing is driven with `async_fire_time_changed` (advancing to the
reminder's `fire_at`) rather than freezing the clock, so the coordinator's own
poll debouncer/timers stay live. Only the aiohttp transport is mocked; the real
schedule → notify → ack path runs.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pineapple_core.coordinator import PineappleCoreCoordinator

from .conftest import ACK_URL, JSON_HEADERS, NOTIFY_TARGET, UPCOMING_URL


def _reminder(tag: str, fire_at, *, title: str = "Bins", message: str = "Out") -> dict:
    """Build one Core upcoming-queue reminder payload."""
    return {
        "tag": tag,
        "fire_at": fire_at.isoformat(),
        "title": title,
        "message": message,
        "data": {"tag": tag, "actions": [{"action": "DONE", "title": "Done"}]},
    }


def _register_notify(hass: HomeAssistant) -> list[ServiceCall]:
    """Register notify.<target> capturing every call it receives."""
    calls: list[ServiceCall] = []

    async def _handler(call: ServiceCall) -> None:
        calls.append(call)

    hass.services.async_register("notify", NOTIFY_TARGET, _handler)
    return calls


def _ack_count(aioclient_mock: AiohttpClientMocker) -> int:
    """How many POSTs hit the ack endpoint so far."""
    return sum(
        1
        for method, url, *_ in aioclient_mock.mock_calls
        if method == "POST" and str(url) == ACK_URL
    )


async def _setup(
    hass: HomeAssistant, entry: MockConfigEntry
) -> PineappleCoreCoordinator:
    """Load the entry and return its coordinator."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry.runtime_data


async def test_fires_notify_at_fire_at_and_acks(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """At the scheduled instant the reminder fires once and is acked."""
    fire_at = dt_util.utcnow() + timedelta(seconds=30)
    aioclient_mock.get(
        UPCOMING_URL,
        json={"reminders": [_reminder("bins-1", fire_at)]},
        headers=JSON_HEADERS,
    )
    aioclient_mock.post(ACK_URL, json={"ok": True})
    calls = _register_notify(hass)

    await _setup(hass, mock_config_entry)
    assert not calls  # nothing fires before its time

    async_fire_time_changed(hass, fire_at + timedelta(seconds=1))
    await hass.async_block_till_done()

    assert len(calls) == 1
    assert calls[0].data["title"] == "Bins"
    assert calls[0].data["message"] == "Out"
    assert calls[0].data["data"]["tag"] == "bins-1"
    assert _ack_count(aioclient_mock) == 1


async def test_failed_ack_does_not_double_fire(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A failed ack keeps the tag pending but never re-delivers it."""
    fire_at = dt_util.utcnow() + timedelta(seconds=30)
    reminder = _reminder("bins-2", fire_at)
    aioclient_mock.get(UPCOMING_URL, json={"reminders": [reminder]}, headers=JSON_HEADERS)
    aioclient_mock.post(ACK_URL, status=500)  # Core can't confirm the delivery
    calls = _register_notify(hass)

    coordinator = await _setup(hass, mock_config_entry)

    async_fire_time_changed(hass, fire_at + timedelta(seconds=1))
    await hass.async_block_till_done()

    assert len(calls) == 1  # delivered once
    assert "bins-2" in coordinator._pending_acks  # ack still owed
    assert "bins-2" in coordinator._fired  # remembered so it can't re-fire

    # Next poll still sees the same reminder (Core never got the ack) — the
    # fired-tag dedup must keep it from being re-armed and re-delivered, while
    # the ack is retried and now succeeds.
    aioclient_mock.clear_requests()
    aioclient_mock.get(UPCOMING_URL, json={"reminders": [reminder]}, headers=JSON_HEADERS)
    aioclient_mock.post(ACK_URL, json={"ok": True})

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert len(calls) == 1  # STILL one delivery — no double-fire
    assert _ack_count(aioclient_mock) == 1  # the retry landed
    assert not coordinator._pending_acks  # cleared once Core confirmed
    assert "bins-2" not in coordinator._scheduled  # not re-armed


@pytest.mark.no_fail_on_log_exception
async def test_notify_failure_leaves_reminder_to_retry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """If the notify call fails the tag is released and not acked, so it retries."""
    fire_at = dt_util.utcnow() + timedelta(seconds=30)
    reminder = _reminder("bins-3", fire_at)
    aioclient_mock.get(UPCOMING_URL, json={"reminders": [reminder]}, headers=JSON_HEADERS)
    aioclient_mock.post(ACK_URL, json={"ok": True})

    attempts: list[ServiceCall] = []

    async def _boom(call: ServiceCall) -> None:
        attempts.append(call)
        raise HomeAssistantError("notify target unavailable")

    hass.services.async_register("notify", NOTIFY_TARGET, _boom)

    coordinator = await _setup(hass, mock_config_entry)

    async_fire_time_changed(hass, fire_at + timedelta(seconds=1))
    await hass.async_block_till_done()

    assert len(attempts) == 1  # notify was attempted
    assert "bins-3" not in coordinator._fired  # released for retry
    assert not coordinator._pending_acks  # a failed delivery is never acked
    assert _ack_count(aioclient_mock) == 0

    # The next poll re-arms the still-undelivered reminder.
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert "bins-3" in coordinator._scheduled


async def test_cancelled_reminder_is_disarmed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A reminder Core drops from the feed is unscheduled and never fires."""
    fire_at = dt_util.utcnow() + timedelta(seconds=30)
    reminder = _reminder("bins-4", fire_at)
    aioclient_mock.get(UPCOMING_URL, json={"reminders": [reminder]}, headers=JSON_HEADERS)
    aioclient_mock.post(ACK_URL, json={"ok": True})
    calls = _register_notify(hass)

    coordinator = await _setup(hass, mock_config_entry)
    assert "bins-4" in coordinator._scheduled

    # Core cancels it — next poll returns an empty queue.
    aioclient_mock.clear_requests()
    aioclient_mock.get(UPCOMING_URL, json={"reminders": []}, headers=JSON_HEADERS)
    aioclient_mock.post(ACK_URL, json={"ok": True})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert "bins-4" not in coordinator._scheduled

    async_fire_time_changed(hass, fire_at + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert not calls  # the cancelled reminder never fired


async def test_auth_error_raises_config_entry_auth_failed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A 401 mid-run turns into a reauth trigger on the next poll."""
    aioclient_mock.get(UPCOMING_URL, json={"reminders": []}, headers=JSON_HEADERS)
    aioclient_mock.post(ACK_URL, json={"ok": True})
    coordinator = await _setup(hass, mock_config_entry)

    aioclient_mock.clear_requests()
    aioclient_mock.get(UPCOMING_URL, status=401)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"].get("source") == "reauth" for f in flows)
