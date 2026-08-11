"""Two Core instances loaded side by side must not bleed into each other.

Both entries share one HA event bus and one notify target, so anything global —
the tapped-action event, the companion app's notification tag, the device name —
has to be namespaced per entry.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pineapple_core.const import (
    ACTION_EVENT,
    CONF_API_TOKEN,
    CONF_BASE_URL,
    CONF_NOTIFY_TARGET,
    DOMAIN,
)

from .conftest import API_TOKEN, BASE_URL, JSON_HEADERS, NOTIFY_TARGET

OTHER_BASE_URL = "https://budgets.test"


def _entry(base_url: str, title: str) -> MockConfigEntry:
    """A config entry pointed at one Core deployment."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data={
            CONF_BASE_URL: base_url,
            CONF_API_TOKEN: API_TOKEN,
            CONF_NOTIFY_TARGET: NOTIFY_TARGET,
        },
        options={},
        unique_id=base_url,
    )


def _stub_core(mock: AiohttpClientMocker, base_url: str, reminders: list) -> None:
    """Stub one Core's upcoming/ack/action endpoints."""
    mock.get(
        f"{base_url}/api/reminders/upcoming",
        json={"data": {"reminders": reminders}},
        headers=JSON_HEADERS,
    )
    mock.post(f"{base_url}/api/reminders/ack", json={"ok": True})
    mock.post(f"{base_url}/api/webhook/action", text="ok")


def _reminder(tag: str, fire_at, token: str) -> dict:
    """One upcoming reminder carrying a single tappable action."""
    return {
        "tag": tag,
        "fire_at": fire_at.isoformat(),
        "title": "Bins",
        "message": "Out",
        "priority": 3,
        "nag": {"interval": "15m", "max": 3, "escalate": False},
        "ack": {"row": tag},
        "data": {"tag": tag, "actions": [{"action": token, "title": "Done"}]},
    }


def _posts(mock: AiohttpClientMocker, url_prefix: str) -> list:
    """Every POST recorded against a URL prefix."""
    return [c for c in mock.mock_calls if c[0] == "POST" and str(c[1]).startswith(url_prefix)]


async def _setup_both(
    hass: HomeAssistant, mock: AiohttpClientMocker, reminders: list
) -> tuple[MockConfigEntry, MockConfigEntry]:
    """Load a `core` entry (with reminders) and a `budgets` entry (idle)."""
    _stub_core(mock, BASE_URL, reminders)
    _stub_core(mock, OTHER_BASE_URL, [])
    core, budgets = _entry(BASE_URL, "Core"), _entry(OTHER_BASE_URL, "Budgets")
    for entry in (core, budgets):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert core.state is ConfigEntryState.LOADED
    assert budgets.state is ConfigEntryState.LOADED
    return core, budgets


async def test_two_entries_get_distinctly_named_devices(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Each entry owns its own service device, named for its Core instance."""
    core, budgets = await _setup_both(hass, aioclient_mock, [])

    registry = dr.async_get(hass)
    names = {
        entry.entry_id: dr.async_entries_for_config_entry(registry, entry.entry_id)[0].name
        for entry in (core, budgets)
    }
    assert sorted(names.values()) == ["Budgets", "Core"]


async def test_tapped_action_is_forwarded_only_by_the_entry_that_sent_it(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The idle entry must not relay another Core's action token."""
    calls: list[ServiceCall] = []
    hass.services.async_register("notify", NOTIFY_TARGET, calls.append)

    fire_at = dt_util.utcnow() + timedelta(seconds=30)
    await _setup_both(hass, aioclient_mock, [_reminder("bins-1", fire_at, "TOK-A")])
    async_fire_time_changed(hass, fire_at + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert calls[0].data["data"]["tag"] == "core_bins-1"  # namespaced away from budgets_*

    hass.bus.async_fire(ACTION_EVENT, {"action": "TOK-A"})
    await hass.async_block_till_done()

    assert len(_posts(aioclient_mock, f"{BASE_URL}/api/webhook/action")) == 1
    assert not _posts(aioclient_mock, f"{OTHER_BASE_URL}/api/webhook/action")


async def test_unknown_action_token_is_forwarded_by_nobody(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A token neither entry issued (another integration's tap) reaches no Core."""
    await _setup_both(hass, aioclient_mock, [])

    hass.bus.async_fire(ACTION_EVENT, {"action": "SOMEONE-ELSES"})
    await hass.async_block_till_done()

    assert not _posts(aioclient_mock, f"{BASE_URL}/api/webhook/action")
    assert not _posts(aioclient_mock, f"{OTHER_BASE_URL}/api/webhook/action")
