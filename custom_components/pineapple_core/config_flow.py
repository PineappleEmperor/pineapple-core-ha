"""Config, options, reauth, and reconfigure flows for Pineapple Core."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PineappleCoreAuthError, PineappleCoreClient, PineappleCoreError
from .const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    CONF_MIRROR_ENTITIES,
    CONF_NOTIFY_TARGET,
    CONF_POLL_INTERVAL,
    CONF_RECIPIENTS,
    CONF_WINDOW_HOURS,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_WINDOW_HOURS,
    DOMAIN,
)
from .helpers import instance_title

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

# The keys of one recipient row in the options form.
_LABEL = "label"
_SERVICE = "service"


def _notify_services(hass: HomeAssistant) -> list[str]:
    """List the notify services HA has registered right now, for the dropdowns."""
    return sorted(hass.services.async_services().get("notify", {}))


def _notify_selector(hass: HomeAssistant) -> selector.SelectSelector:
    """Build a dropdown of notify services that still accepts one not registered yet."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=_notify_services(hass),
            custom_value=True,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _user_schema(
    hass: HomeAssistant, defaults: Mapping[str, Any] | None = None
) -> vol.Schema:
    """Build the setup form, offering the installed notify services as a dropdown."""
    # `notify_target` is a per-install device (e.g. the companion app on your
    # phone), so it's picked from the notify services HA actually has — with
    # `custom_value` on, so a service that hasn't registered yet can still be typed.
    current = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_BASE_URL,
                description={"suggested_value": current.get(CONF_BASE_URL)},
            ): str,
            vol.Required(
                CONF_API_TOKEN,
                description={"suggested_value": current.get(CONF_API_TOKEN)},
            ): str,
            vol.Required(
                CONF_NOTIFY_TARGET,
                description={"suggested_value": current.get(CONF_NOTIFY_TARGET)},
            ): _notify_selector(hass),
        }
    )


def _recipient_rows(recipients: Mapping[str, str]) -> list[dict[str, str]]:
    """Turn the stored label → service map into the options form's editable rows."""
    return [{_LABEL: label, _SERVICE: service} for label, service in recipients.items()]


def _recipient_map(rows: list[dict[str, str]]) -> dict[str, str]:
    """Turn the form's rows into the stored map, or raise ValueError with the error key."""
    recipients: dict[str, str] = {}
    for row in rows:
        label = row.get(_LABEL, "").strip().casefold()
        service = row.get(_SERVICE, "").strip()
        if not label or not service:
            error = "invalid_recipient"
            raise ValueError(error)
        if label in recipients:
            error = "duplicate_recipient"
            raise ValueError(error)
        recipients[label] = service
    return recipients


async def _validate(hass: HomeAssistant, base_url: str, token: str) -> None:
    """Prove the base URL + token reach Core before saving the entry."""
    client = PineappleCoreClient(async_get_clientsession(hass), base_url, token)
    await client.async_get_upcoming(1)


class PineappleCoreConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the setup, reconfigure, reauth, and options entry points."""

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
                    title=instance_title(base_url),
                    data={**user_input, CONF_BASE_URL: base_url},
                )
        return self.async_show_form(
            step_id="user", data_schema=_user_schema(self.hass), errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change an existing entry's URL, token, or notify target in place."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            base_url = user_input[CONF_BASE_URL].rstrip("/")
            if base_url != entry.data[CONF_BASE_URL]:
                # The base URL is the unique id, so pointing this entry at a
                # different Core is fine — but not at one a second entry owns.
                await self.async_set_unique_id(base_url)
                self._abort_if_unique_id_configured()
            errors = await self._probe(base_url, user_input[CONF_API_TOKEN])
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=base_url,
                    title=instance_title(base_url),
                    data_updates={**user_input, CONF_BASE_URL: base_url},
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_user_schema(self.hass, entry.data),
            errors=errors,
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


class PineappleCoreOptionsFlow(OptionsFlowWithReload):
    """Tune polling, the mirror list, and who reminders can be addressed to."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show + persist the tunable options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                recipients = _recipient_map(user_input.get(CONF_RECIPIENTS, []))
            except ValueError as err:
                errors[CONF_RECIPIENTS] = str(err)
            else:
                return self.async_create_entry(
                    data={**user_input, CONF_RECIPIENTS: recipients}
                )
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
                vol.Optional(
                    CONF_MIRROR_ENTITIES,
                    default=options.get(CONF_MIRROR_ENTITIES, []),
                ): selector.EntitySelector(selector.EntitySelectorConfig(multiple=True)),
                vol.Optional(
                    CONF_RECIPIENTS,
                    default=_recipient_rows(options.get(CONF_RECIPIENTS, {})),
                ): selector.ObjectSelector(
                    selector.ObjectSelectorConfig(
                        fields={
                            _LABEL: {"required": True, "selector": selector.TextSelector()},
                            _SERVICE: {"required": True, "selector": _notify_selector(self.hass)},
                        },
                        multiple=True,
                        label_field=_LABEL,
                        description_field=_SERVICE,
                        translation_key="recipient",
                    )
                ),
            }
        )
        if user_input is not None:
            # Keep what was typed when the form comes back with an error.
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
