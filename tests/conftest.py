"""Shared fixtures for the Pineapple Core test suite.

Everything is mocked at the aiohttp transport boundary (`aioclient_mock`), never
at the integration's own functions — so the real config-read → coordinator →
schedule → notify wiring runs under test.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pineapple_core.const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    CONF_NOTIFY_TARGET,
    DOMAIN,
)

BASE_URL = "https://core.test"
API_TOKEN = "svc-token-abc"
NOTIFY_TARGET = "mobile_app_test"

UPCOMING_URL = f"{BASE_URL}/api/reminders/upcoming"
ACK_URL = f"{BASE_URL}/api/reminders/ack"
ACTION_URL = f"{BASE_URL}/api/webhook/action"
HELPER_URL = f"{BASE_URL}/api/integrations/ha/helper"

# The real Core sends this; the aiohttp mocker won't set it from `json=` alone,
# and api.py only parses a body when content_type is application/json.
JSON_HEADERS = {"content-type": "application/json"}


@pytest.fixture
def entry_data() -> dict[str, Any]:
    """The `entry.data` a fully configured Pineapple Core entry stores."""
    return {
        CONF_BASE_URL: BASE_URL,
        CONF_API_TOKEN: API_TOKEN,
        CONF_NOTIFY_TARGET: NOTIFY_TARGET,
    }


@pytest.fixture
def mock_config_entry(entry_data: dict[str, Any]) -> MockConfigEntry:
    """A ready-to-load config entry for this domain.

    `unique_id` mirrors the flow — it is the base URL — so an
    `already_configured` abort test can collide with it.
    """
    return MockConfigEntry(
        domain=DOMAIN,
        title="Pineapple Core",
        data=entry_data,
        options={},
        unique_id=BASE_URL,
    )


@pytest.fixture
def mock_upcoming(
    aioclient_mock: AiohttpClientMocker,
) -> Callable[[list[dict[str, Any]]], None]:
    """Return a helper that stubs the GET upcoming + POST ack endpoints.

    Call it with the list of reminder dicts Core should return; the ack endpoint
    is always stubbed to 200 so a successful delivery can be acknowledged.
    """

    def _set(reminders: list[dict[str, Any]] | None = None) -> None:
        aioclient_mock.clear_requests()
        # Core wraps responses in a {"data": …} envelope.
        aioclient_mock.get(
            UPCOMING_URL, json={"data": {"reminders": reminders or []}}, headers=JSON_HEADERS
        )
        aioclient_mock.post(ACK_URL, json={"data": {"ok": True}}, headers=JSON_HEADERS)

    return _set
