"""Per-reminder recipients: a reminder names who it is for, the entry maps that to phones.

Core sends `recipients` as labels (`["dan", "sian"]`); the entry's *recipients* option maps
each label to a notify service. No recipients means the entry's default notify target, so
every reminder that predates the field is delivered exactly as before.
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

from custom_components.pineapple_core.api import _recipients
from custom_components.pineapple_core.const import ACTION_EVENT, CONF_RECIPIENTS, DOMAIN
from custom_components.pineapple_core.coordinator import PineappleCoreCoordinator
from custom_components.pineapple_core.webhook import async_dispatch

from .conftest import (
    ACK_URL,
    ACTION_URL,
    BASE_URL,
    JSON_HEADERS,
    NOTIFY_TARGET,
    UPCOMING_URL,
)

DAN = "mobile_app_dan_phone"
SIAN = "mobile_app_sian_phone"
RECIPIENTS = {"dan": DAN, "sian": SIAN}


def _reminder(
    tag: str,
    fire_at,
    *,
    recipients: list | None = None,
    nag: dict | None = None,
    action: str = "TOK",
) -> dict:
    """One Core upcoming-queue entry, optionally addressed to recipients."""
    entry: dict = {
        "tag": tag,
        "fire_at": fire_at.isoformat(),
        "title": "Invoice due",
        "message": "Stone Barn £600",
        "nag": nag,
        "ack": {"row": tag},
        "data": {"tag": tag, "actions": [{"action": action, "title": "Done"}]},
    }
    if recipients is not None:
        entry["recipients"] = recipients
    return entry


def _stub(aioclient_mock: AiohttpClientMocker, reminders: list[dict]) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(UPCOMING_URL, json={"data": {"reminders": reminders}}, headers=JSON_HEADERS)
    aioclient_mock.post(ACK_URL, json={"ok": True})
    aioclient_mock.post(ACTION_URL, text="ok")


def _ack_count(aioclient_mock: AiohttpClientMocker) -> int:
    return sum(
        1
        for method, url, *_ in aioclient_mock.mock_calls
        if method == "POST" and str(url) == ACK_URL
    )


class _Phones:
    """Records every notify call per service, so a test can ask who got what."""

    def __init__(self, hass: HomeAssistant, *services: str, failing: tuple[str, ...] = ()) -> None:
        self.calls: dict[str, list[ServiceCall]] = {s: [] for s in services}
        for service in services:
            hass.services.async_register(
                "notify", service, self._handler(service, fail=service in failing)
            )

    def _handler(self, service: str, *, fail: bool):
        async def _handle(call: ServiceCall) -> None:
            self.calls[service].append(call)
            if fail:
                msg = f"{service} is offline"
                raise HomeAssistantError(msg)

        return _handle

    def messages(self, service: str) -> list[str]:
        return [c.data["message"] for c in self.calls[service]]

    def cleared(self, service: str) -> list[str]:
        return [
            c.data["data"]["tag"]
            for c in self.calls[service]
            if c.data["message"] == "clear_notification"
        ]


@pytest.fixture
def recipient_entry(entry_data: dict) -> MockConfigEntry:
    """An entry with Dan and Siân mapped to their phones, default target unchanged."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Pineapple Core",
        data=entry_data,
        options={CONF_RECIPIENTS: RECIPIENTS},
        unique_id=BASE_URL,
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> PineappleCoreCoordinator:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry.runtime_data


async def _fire(hass: HomeAssistant, fire_at) -> None:
    async_fire_time_changed(hass, fire_at + timedelta(seconds=1))
    await hass.async_block_till_done()


async def test_two_recipients_are_both_notified(
    hass: HomeAssistant, recipient_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A reminder for dan + sian reaches both phones, not the default target, and acks once."""
    fire_at = dt_util.utcnow() + timedelta(seconds=30)
    _stub(aioclient_mock, [_reminder("inv-1", fire_at, recipients=["dan", "sian"])])
    phones = _Phones(hass, NOTIFY_TARGET, DAN, SIAN)

    await _setup(hass, recipient_entry)
    await _fire(hass, fire_at)

    assert phones.messages(DAN) == ["Stone Barn £600"]
    assert phones.messages(SIAN) == ["Stone Barn £600"]
    assert phones.messages(NOTIFY_TARGET) == []
    # Both phones carry the same namespaced tag, so one clear dismisses it everywhere.
    assert phones.calls[DAN][0].data["data"]["tag"] == "core_inv-1"
    assert phones.calls[SIAN][0].data["data"]["tag"] == "core_inv-1"
    assert _ack_count(aioclient_mock) == 1


@pytest.mark.no_fail_on_log_exception
async def test_one_failing_recipient_still_acks_and_never_resends(
    hass: HomeAssistant,
    recipient_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One accepting phone is enough to ack; the next poll sends to nobody again."""
    fire_at = dt_util.utcnow() + timedelta(seconds=30)
    reminder = _reminder("inv-2", fire_at, recipients=["dan", "sian"])
    _stub(aioclient_mock, [reminder])
    phones = _Phones(hass, NOTIFY_TARGET, DAN, SIAN, failing=(SIAN,))

    coordinator = await _setup(hass, recipient_entry)
    await _fire(hass, fire_at)

    assert len(phones.calls[DAN]) == 1
    assert len(phones.calls[SIAN]) == 1  # attempted, raised
    assert f"notify.{SIAN} failed" in caplog.text  # the failed phone is named
    assert _ack_count(aioclient_mock) == 1
    assert coordinator._s.targets["inv-2"] == (DAN,)  # only the phone that accepted

    # Core hasn't processed the ack yet and still lists it: nothing is re-sent.
    _stub(aioclient_mock, [reminder])
    await coordinator.async_refresh()
    await _fire(hass, fire_at)
    assert len(phones.calls[DAN]) == 1
    assert len(phones.calls[SIAN]) == 1


@pytest.mark.no_fail_on_log_exception
async def test_every_recipient_failing_releases_for_retry(
    hass: HomeAssistant, recipient_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """With no phone accepting, nothing is acked and the next poll re-arms it."""
    fire_at = dt_util.utcnow() + timedelta(seconds=30)
    _stub(aioclient_mock, [_reminder("inv-3", fire_at, recipients=["dan", "sian"])])
    _Phones(hass, NOTIFY_TARGET, DAN, SIAN, failing=(DAN, SIAN))

    coordinator = await _setup(hass, recipient_entry)
    await _fire(hass, fire_at)

    assert "inv-3" not in coordinator._fired
    assert "inv-3" not in coordinator._s.targets
    assert _ack_count(aioclient_mock) == 0
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert "inv-3" in coordinator._scheduled


async def test_unknown_label_falls_back_to_the_default_target(
    hass: HomeAssistant,
    recipient_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A typo'd label warns by name and still delivers, to the default target."""
    fire_at = dt_util.utcnow() + timedelta(seconds=30)
    _stub(aioclient_mock, [_reminder("inv-4", fire_at, recipients=["dan", "sain"])])
    phones = _Phones(hass, NOTIFY_TARGET, DAN, SIAN)

    await _setup(hass, recipient_entry)
    await _fire(hass, fire_at)

    assert "'sain'" in caplog.text
    assert len(phones.calls[DAN]) == 1
    assert len(phones.calls[NOTIFY_TARGET]) == 1  # the fallback
    assert phones.calls[SIAN] == []
    assert _ack_count(aioclient_mock) == 1


async def test_labels_resolving_to_one_service_notify_it_once(
    hass: HomeAssistant, entry_data: dict, aioclient_mock: AiohttpClientMocker
) -> None:
    """Two labels on one phone, or a label on the default phone, de-duplicate."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Pineapple Core",
        data=entry_data,
        options={CONF_RECIPIENTS: {"dan": NOTIFY_TARGET, "both": NOTIFY_TARGET}},
        unique_id=BASE_URL,
    )
    fire_at = dt_util.utcnow() + timedelta(seconds=30)
    _stub(aioclient_mock, [_reminder("inv-5", fire_at, recipients=["dan", "Both", "nobody"])])
    phones = _Phones(hass, NOTIFY_TARGET)

    await _setup(hass, entry)
    await _fire(hass, fire_at)

    assert len(phones.calls[NOTIFY_TARGET]) == 1


async def test_no_recipients_is_delivered_exactly_as_before(
    hass: HomeAssistant, recipient_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Without `recipients` only the default target is notified, map or no map."""
    fire_at = dt_util.utcnow() + timedelta(seconds=30)
    _stub(
        aioclient_mock,
        [
            _reminder("meds-1", fire_at),
            _reminder("meds-2", fire_at, recipients=[], action="TOK2"),
        ],
    )
    phones = _Phones(hass, NOTIFY_TARGET, DAN, SIAN)

    coordinator = await _setup(hass, recipient_entry)
    await _fire(hass, fire_at)

    assert len(phones.calls[NOTIFY_TARGET]) == 2
    assert phones.calls[DAN] == []
    assert phones.calls[SIAN] == []
    assert coordinator._s.targets["meds-1"] == (NOTIFY_TARGET,)


async def test_nag_repeats_to_both_recipients(
    hass: HomeAssistant, recipient_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Each nag goes to every phone that accepted the first delivery."""
    fire_at = dt_util.utcnow() + timedelta(seconds=30)
    _stub(
        aioclient_mock,
        [
            _reminder(
                "inv-6",
                fire_at,
                recipients=["dan", "sian"],
                nag={"interval": "15m", "max": 2, "escalate": False},
            )
        ],
    )
    phones = _Phones(hass, NOTIFY_TARGET, DAN, SIAN)

    coordinator = await _setup(hass, recipient_entry)
    await _fire(hass, fire_at)
    coordinator._nag_fire("inv-6", dt_util.utcnow())
    await hass.async_block_till_done()

    assert len(phones.calls[DAN]) == 2
    assert len(phones.calls[SIAN]) == 2
    assert phones.calls[NOTIFY_TARGET] == []
    coordinator.async_cancel_scheduled()


async def test_core_clear_reaches_both_recipients(
    hass: HomeAssistant, recipient_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Core's `clear` push dismisses the tag on every phone that got it — after the feed drop."""
    fire_at = dt_util.utcnow() + timedelta(seconds=30)
    _stub(aioclient_mock, [_reminder("inv-7", fire_at, recipients=["dan", "sian"])])
    phones = _Phones(hass, NOTIFY_TARGET, DAN, SIAN)

    coordinator = await _setup(hass, recipient_entry)
    await _fire(hass, fire_at)

    # Acked, so Core drops it — the invoice is then paid days later.
    _stub(aioclient_mock, [])
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    await async_dispatch(
        hass,
        coordinator,
        {"event": "clear", "title": "", "message": "clear_notification", "data": {"tag": "inv-7"}},
    )
    await hass.async_block_till_done()

    assert phones.cleared(DAN) == ["core_inv-7"]
    assert phones.cleared(SIAN) == ["core_inv-7"]
    assert phones.cleared(NOTIFY_TARGET) == []


async def test_tap_on_second_phone_clears_the_first(
    hass: HomeAssistant, recipient_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Siân's tap forwards the token once and dismisses the notification on both phones."""
    fire_at = dt_util.utcnow() + timedelta(seconds=30)
    _stub(
        aioclient_mock,
        [
            _reminder(
                "inv-8",
                fire_at,
                recipients=["dan", "sian"],
                action="TOK-8",
                nag={"interval": "15m", "max": 3, "escalate": False},
            )
        ],
    )
    phones = _Phones(hass, NOTIFY_TARGET, DAN, SIAN)

    coordinator = await _setup(hass, recipient_entry)
    await _fire(hass, fire_at)

    # The companion app's event is global; the token alone identifies the reminder.
    hass.bus.async_fire(ACTION_EVENT, {"action": "TOK-8", "device_id": "sian-phone"})
    await hass.async_block_till_done()

    forwarded = [
        c for c in aioclient_mock.mock_calls if c[0] == "POST" and str(c[1]).startswith(ACTION_URL)
    ]
    assert len(forwarded) == 1
    assert phones.cleared(DAN) == ["core_inv-8"]
    assert phones.cleared(SIAN) == ["core_inv-8"]
    assert "inv-8" not in coordinator._s.nags


async def test_clear_for_an_unrecorded_tag_reaches_every_mapped_phone(
    hass: HomeAssistant, recipient_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """After a restart the record is gone, so a clear goes to the default and every mapped phone."""
    _stub(aioclient_mock, [])
    phones = _Phones(hass, NOTIFY_TARGET, DAN, SIAN)
    coordinator = await _setup(hass, recipient_entry)

    await async_dispatch(
        hass,
        coordinator,
        {"event": "clear", "title": "", "message": "clear_notification", "data": {"tag": "old"}},
    )
    await hass.async_block_till_done()

    assert phones.cleared(NOTIFY_TARGET) == ["core_old"]
    assert phones.cleared(DAN) == ["core_old"]
    assert phones.cleared(SIAN) == ["core_old"]


def test_recipients_parse_keeps_only_label_strings() -> None:
    """Core's field is read defensively: anything but a list of labels means default."""
    assert _recipients(["dan", "sian"]) == ("dan", "sian")
    assert _recipients(["dan", 3, "", "  "]) == ("dan",)
    assert _recipients([]) is None
    assert _recipients(None) is None
    assert _recipients("dan") is None
    assert _recipients({"dan": True}) is None
