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

from custom_components.pineapple_core.coordinator import (
    PineappleCoreCoordinator,
    _escalated,
    _interval_seconds,
)

from .conftest import ACK_URL, ACTION_URL, JSON_HEADERS, NOTIFY_TARGET, UPCOMING_URL


def _reminder(
    tag: str,
    fire_at,
    *,
    action: str = "DONE",
    nag: dict | None = None,
) -> dict:
    """Build one Core upcoming-queue reminder payload."""
    return {
        "tag": tag,
        "fire_at": fire_at.isoformat(),
        "title": "Bins",
        "message": "Out",
        "priority": 3,
        "nag": nag,
        "ack": {"row": tag},
        "data": {
            "tag": tag,
            "actions": [{"action": action, "title": "Done"}],
            "push": {"interruption-level": "active"},
        },
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
        json={"data": {"reminders": [_reminder("bins-1", fire_at)]}},
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
    aioclient_mock.get(UPCOMING_URL, json={"data": {"reminders": [reminder]}}, headers=JSON_HEADERS)
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
    aioclient_mock.get(UPCOMING_URL, json={"data": {"reminders": [reminder]}}, headers=JSON_HEADERS)
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
    aioclient_mock.get(UPCOMING_URL, json={"data": {"reminders": [reminder]}}, headers=JSON_HEADERS)
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
    aioclient_mock.get(UPCOMING_URL, json={"data": {"reminders": [reminder]}}, headers=JSON_HEADERS)
    aioclient_mock.post(ACK_URL, json={"ok": True})
    calls = _register_notify(hass)

    coordinator = await _setup(hass, mock_config_entry)
    assert "bins-4" in coordinator._scheduled

    # Core cancels it — next poll returns an empty queue.
    aioclient_mock.clear_requests()
    aioclient_mock.get(UPCOMING_URL, json={"data": {"reminders": []}}, headers=JSON_HEADERS)
    aioclient_mock.post(ACK_URL, json={"ok": True})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert "bins-4" not in coordinator._scheduled

    async_fire_time_changed(hass, fire_at + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert not calls  # the cancelled reminder never fired


def test_interval_seconds_parses_and_rejects() -> None:
    """Nag intervals map to seconds; junk / non-repeat values give None."""
    assert _interval_seconds("15m") == 900
    assert _interval_seconds("1h") == 3600
    assert _interval_seconds("0m") is None
    assert _interval_seconds("soon") is None
    assert _interval_seconds(None) is None


def test_escalated_raises_interruption_level_capped() -> None:
    """Escalation climbs the level ladder one rung per step, capped at critical."""
    base = {"push": {"interruption-level": "active"}}
    assert _escalated(base, 1)["push"]["interruption-level"] == "time-sensitive"
    assert _escalated(base, 2)["push"]["interruption-level"] == "critical"
    assert _escalated(base, 9)["push"]["interruption-level"] == "critical"
    # The original is untouched (deep-copied).
    assert base["push"]["interruption-level"] == "active"


async def test_nag_repeats_until_max_then_stops(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A delivered reminder re-fires its notification up to `max` times, then stops."""
    fire_at = dt_util.utcnow() + timedelta(seconds=30)
    reminder = _reminder("nag-1", fire_at, nag={"interval": "15m", "max": 2, "escalate": False})
    aioclient_mock.get(UPCOMING_URL, json={"data": {"reminders": [reminder]}}, headers=JSON_HEADERS)
    aioclient_mock.post(ACK_URL, json={"ok": True})
    calls = _register_notify(hass)

    coordinator = await _setup(hass, mock_config_entry)
    async_fire_time_changed(hass, fire_at + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert len(calls) == 1  # initial delivery
    assert "nag-1" in coordinator._s.nags  # chain armed

    # Drive the chain directly — deterministic, no timer wall-clock races.
    coordinator._nag_fire("nag-1", dt_util.utcnow())
    await hass.async_block_till_done()
    assert len(calls) == 2
    assert coordinator._s.nags["nag-1"].count == 1

    coordinator._nag_fire("nag-1", dt_util.utcnow())
    await hass.async_block_till_done()
    assert len(calls) == 3  # max reached
    assert "nag-1" not in coordinator._s.nags  # chain retired

    coordinator._nag_fire("nag-1", dt_util.utcnow())  # no chain → no-op
    await hass.async_block_till_done()
    assert len(calls) == 3


async def test_escalating_nag_raises_level(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An `escalate` nag re-sends the notification at a higher interruption level."""
    fire_at = dt_util.utcnow() + timedelta(seconds=30)
    reminder = _reminder("nag-2", fire_at, nag={"interval": "15m", "max": 3, "escalate": True})
    aioclient_mock.get(UPCOMING_URL, json={"data": {"reminders": [reminder]}}, headers=JSON_HEADERS)
    aioclient_mock.post(ACK_URL, json={"ok": True})
    calls = _register_notify(hass)

    coordinator = await _setup(hass, mock_config_entry)
    async_fire_time_changed(hass, fire_at + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert calls[0].data["data"]["push"]["interruption-level"] == "active"

    coordinator._nag_fire("nag-2", dt_util.utcnow())
    await hass.async_block_till_done()
    assert calls[1].data["data"]["push"]["interruption-level"] == "time-sensitive"

    coordinator.async_cancel_scheduled()  # the chain's next timer is still armed


async def test_tapped_action_forwards_to_core_and_cancels_nag(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A companion-app action tap forwards the token to Core and stops its nags."""
    fire_at = dt_util.utcnow() + timedelta(seconds=30)
    reminder = _reminder(
        "act-1", fire_at, action="TOK-1", nag={"interval": "15m", "max": 3, "escalate": False}
    )
    aioclient_mock.get(UPCOMING_URL, json={"data": {"reminders": [reminder]}}, headers=JSON_HEADERS)
    aioclient_mock.post(ACK_URL, json={"ok": True})
    aioclient_mock.post(ACTION_URL, text="ok")
    _register_notify(hass)

    coordinator = await _setup(hass, mock_config_entry)
    async_fire_time_changed(hass, fire_at + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert "act-1" in coordinator._s.nags
    assert coordinator._s.action_tags.get("TOK-1") == "act-1"

    hass.bus.async_fire("mobile_app_notification_action", {"action": "TOK-1"})
    await hass.async_block_till_done()

    forwarded = [
        c for c in aioclient_mock.mock_calls
        if c[0] == "POST" and str(c[1]).startswith(ACTION_URL)
    ]
    assert len(forwarded) == 1  # the token reached Core's webhook
    assert "TOK-1" in str(forwarded[0][1])  # ?tok= carries it
    assert "act-1" in coordinator._s.handled
    assert "act-1" not in coordinator._s.nags  # nag chain cancelled at once


async def test_auth_error_raises_config_entry_auth_failed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A 401 mid-run turns into a reauth trigger on the next poll."""
    aioclient_mock.get(UPCOMING_URL, json={"data": {"reminders": []}}, headers=JSON_HEADERS)
    aioclient_mock.post(ACK_URL, json={"ok": True})
    coordinator = await _setup(hass, mock_config_entry)

    aioclient_mock.clear_requests()
    aioclient_mock.get(UPCOMING_URL, status=401)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"].get("source") == "reauth" for f in flows)
