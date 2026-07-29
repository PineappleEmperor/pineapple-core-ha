"""Config, options, and reauth flows for Pineapple Core."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PineappleCoreAuthError, PineappleCoreClient, PineappleCoreError
from .const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    CONF_NOTIFY_TARGET,
    CONF_POLL_INTERVAL,
    CONF_WINDOW_HOURS,
    DEFAULT_NOTIFY_TARGET,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_WINDOW_HOURS,
    DOMAIN,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BASE_URL): str,
        vol.Required(CONF_API_TOKEN): str,
        vol.Required(CONF_NOTIFY_TARGET, default=DEFAULT_NOTIFY_TARGET): str,
    }
)


async def _validate(hass: HomeAssistant, base_url: str, token: str) -> None:
    """Prove the base URL + token reach Core before saving the entry."""
    client = PineappleCoreClient(async_get_clientsession(hass), base_url, token)
    await client.async_get_upcoming(1)


class PineappleCoreConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the setup, reauth, and options entry points."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the Core URL, token, and notify target."""
        errors: dict[str, str] = {}
        if user_input is not None:
            base_url = user_input[CONF_BASE_URL].rstrip("/")
            await self.async_set_unique_id(base_url)
            self._abort_if_unique_id_configured()
            errors = await self._probe(base_url, user_input[CONF_API_TOKEN])
            if not errors:
                return self.async_create_entry(
                    title="Pineapple Core",
                    data={**user_input, CONF_BASE_URL: base_url},
                )
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]  # noqa: ARG002 — HA reauth signature
    ) -> ConfigFlowResult:
        """Start reauth when Core rejects the stored token."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take a fresh token and update the existing entry."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            errors = await self._probe(entry.data[CONF_BASE_URL], user_input[CONF_API_TOKEN])
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_TOKEN: user_input[CONF_API_TOKEN]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_TOKEN): str}),
            errors=errors,
        )

    async def _probe(self, base_url: str, token: str) -> dict[str, str]:
        """Return a form-error dict for a bad URL/token, or empty on success."""
        try:
            await _validate(self.hass, base_url, token)
        except PineappleCoreAuthError:
            return {"base": "invalid_auth"}
        except PineappleCoreError:
            return {"base": "cannot_connect"}
        return {}

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:  # noqa: ARG004
        """Return the options flow for tuning poll cadence + window."""
        return PineappleCoreOptionsFlow()


class PineappleCoreOptionsFlow(OptionsFlow):
    """Tune the poll interval and how far ahead Core is queried."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show + persist the tunable options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=options.get(
                        CONF_POLL_INTERVAL, int(DEFAULT_POLL_INTERVAL.total_seconds() // 60)
                    ),
                ): vol.All(int, vol.Range(min=1, max=60)),
                vol.Required(
                    CONF_WINDOW_HOURS,
                    default=options.get(CONF_WINDOW_HOURS, DEFAULT_WINDOW_HOURS),
                ): vol.All(int, vol.Range(min=1, max=24)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
