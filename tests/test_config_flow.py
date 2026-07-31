"""Config, reauth, and options flow tests.

The transport is mocked at the aiohttp boundary; the flow's own `_validate` /
`_probe` wiring runs for real so a refactor that drops the URL/token read fails
loudly.
"""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.pineapple_core.const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    CONF_MIRROR_ALIASES,
    CONF_MIRROR_ENTITIES,
    CONF_NOTIFY_TARGET,
    CONF_POLL_INTERVAL,
    CONF_WINDOW_HOURS,
    DOMAIN,
)

from .conftest import API_TOKEN, BASE_URL, NOTIFY_TARGET, UPCOMING_URL

USER_INPUT = {
    CONF_BASE_URL: BASE_URL,
    CONF_API_TOKEN: API_TOKEN,
    CONF_NOTIFY_TARGET: NOTIFY_TARGET,
}


async def test_user_step_success_creates_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A reachable URL + accepted token creates the config entry."""
    aioclient_mock.get(UPCOMING_URL, json={"reminders": []})

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result.get("type") is FlowResultType.FORM
    assert result.get("step_id") == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()

    assert result.get("type") is FlowResultType.CREATE_ENTRY
    assert result.get("title") == "Pineapple Core"
    data = result["data"]
    assert data[CONF_BASE_URL] == BASE_URL
    assert data[CONF_API_TOKEN] == API_TOKEN
    assert data[CONF_NOTIFY_TARGET] == NOTIFY_TARGET


async def test_user_step_lists_notify_services_in_dropdown(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The notify_target field offers the installed notify services as options."""
    aioclient_mock.get(UPCOMING_URL, json={"reminders": []})
    hass.services.async_register("notify", "mobile_app_pixel", lambda _call: None)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    # Walk the selector's option list out of the shown schema.
    schema = result["data_schema"].schema
    field = next(k for k in schema if str(k) == CONF_NOTIFY_TARGET)
    options = schema[field].config["options"]
    assert "mobile_app_pixel" in options
    assert schema[field].config["custom_value"] is True


async def test_user_step_trailing_slash_stripped(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The stored base URL is normalised (no trailing slash) and is the unique id."""
    aioclient_mock.get(UPCOMING_URL, json={"reminders": []})

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_BASE_URL: f"{BASE_URL}/"}
    )
    await hass.async_block_till_done()

    assert result.get("type") is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_BASE_URL] == BASE_URL
    assert result["result"].unique_id == BASE_URL


async def test_user_step_cannot_connect(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A transport error surfaces as the cannot_connect form error."""
    aioclient_mock.get(UPCOMING_URL, exc=TimeoutError())

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result.get("type") is FlowResultType.FORM
    assert result.get("errors") == {"base": "cannot_connect"}


async def test_user_step_invalid_auth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 401 from Core surfaces as the invalid_auth form error."""
    aioclient_mock.get(UPCOMING_URL, status=401)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result.get("type") is FlowResultType.FORM
    assert result.get("errors") == {"base": "invalid_auth"}


async def test_user_step_recovers_after_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """After a cannot_connect the same flow succeeds once Core is reachable."""
    aioclient_mock.get(UPCOMING_URL, exc=TimeoutError())
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result.get("errors") == {"base": "cannot_connect"}

    aioclient_mock.clear_requests()
    aioclient_mock.get(UPCOMING_URL, json={"reminders": []})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()
    assert result.get("type") is FlowResultType.CREATE_ENTRY


async def test_already_configured_aborts(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A second entry for the same base URL aborts as already_configured."""
    mock_config_entry.add_to_hass(hass)
    aioclient_mock.get(UPCOMING_URL, json={"reminders": []})

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result.get("type") is FlowResultType.ABORT
    assert result.get("reason") == "already_configured"


async def test_reauth_flow_updates_token(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Reauth accepts a fresh token, revalidates it, and updates the entry."""
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result.get("type") is FlowResultType.FORM
    assert result.get("step_id") == "reauth_confirm"

    aioclient_mock.get(UPCOMING_URL, json={"reminders": []})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: "fresh-token"}
    )
    await hass.async_block_till_done()

    assert result.get("type") is FlowResultType.ABORT
    assert result.get("reason") == "reauth_successful"
    assert mock_config_entry.data[CONF_API_TOKEN] == "fresh-token"


async def test_reauth_flow_rejects_bad_token(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A still-bad token keeps the reauth form open with invalid_auth."""
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reauth_flow(hass)
    aioclient_mock.get(UPCOMING_URL, status=403)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: "still-bad"}
    )

    assert result.get("type") is FlowResultType.FORM
    assert result.get("errors") == {"base": "invalid_auth"}
    assert mock_config_entry.data[CONF_API_TOKEN] == API_TOKEN


async def test_options_flow_persists_tunables(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    mock_upcoming,
) -> None:
    """The options flow stores the poll interval and window hours."""
    mock_upcoming([])
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(
        mock_config_entry.entry_id
    )
    assert result.get("type") is FlowResultType.FORM
    assert result.get("step_id") == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_POLL_INTERVAL: 10, CONF_WINDOW_HOURS: 6},
    )
    await hass.async_block_till_done()

    assert result.get("type") is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options == {
        CONF_POLL_INTERVAL: 10,
        CONF_WINDOW_HOURS: 6,
        CONF_MIRROR_ENTITIES: [],  # optional, defaults to none
        CONF_MIRROR_ALIASES: "",  # optional, defaults to empty
    }
