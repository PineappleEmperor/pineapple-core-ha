"""Inbound webhook: the integration's own Core→HA push channel.

Replaces the hand-written `notify - core` automation. On setup the integration
registers its own `webhook_id` (and, when Nabu Casa cloud is active, a cloudhook),
so Core keeps POSTing to `HA_WEBHOOK_URL` — that URL now points here instead of at
a YAML automation. The handler routes Core's existing payloads by their `event`:

  clear    → notify.<target> message "clear_notification" (dismiss by tag)
  helper   → input_number.set_value(entity, value)
  reminder → notify.<target> (also the digest's `event`)
  digest   → notify.<target>

Reminders/meds do NOT arrive here — those stay on the hardened `/upcoming` poll.
Only the low-stakes pushes ride the webhook, where an edge-200 is tolerable.
"""

from __future__ import annotations

import logging
from functools import partial
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp.web import Response
from homeassistant.components.webhook import (
    async_generate_id,
    async_generate_url,
    async_register,
    async_unregister,
)
from homeassistant.helpers.network import NoURLAvailableError

from .const import (
    CONF_CLOUDHOOK_URL,
    CONF_WEBHOOK_ID,
    DOMAIN,
)

if TYPE_CHECKING:
    from aiohttp.web import Request
    from homeassistant.core import HomeAssistant

    from . import PineappleCoreConfigEntry
    from .coordinator import PineappleCoreCoordinator

_LOGGER = logging.getLogger(__name__)


def _cloud() -> Any:  # noqa: ANN401 — the cloud module's surface is accessed dynamically
    """Lazily import the optional `cloud` component; None if unavailable."""
    try:
        from homeassistant.components import cloud  # noqa: PLC0415 — optional soft dep
    except ImportError:
        return None
    return cloud


async def async_dispatch(
    hass: HomeAssistant,
    coordinator: PineappleCoreCoordinator,
    data: dict[str, Any],
) -> None:
    """Route one decoded Core push to the right HA service call."""
    event = data.get("event")
    if event == "helper":
        entity, value = data.get("entity"), data.get("value")
        if entity is None or value is None:
            _LOGGER.warning("Ignoring helper push with no entity/value: %s", data)
            return
        await hass.services.async_call(
            "input_number", "set_value", {"entity_id": entity, "value": value}, blocking=False
        )
        return
    if event in ("clear", "reminder", "digest"):
        payload: dict[str, Any] = data.get("data") or {}
        # A clear is an app-side "handled" signal — also end the local nag chain for
        # that tag, so completing an item in the app stops its nagging.
        if event == "clear":
            coordinator.note_external_clear(payload.get("tag", ""))
        else:
            # Claim this push's action tokens: a tap is a global event, so the
            # entry that sent the notification must be the one that forwards it.
            coordinator.note_actions(payload.get("tag", ""), payload)
        await coordinator.async_notify(
            data.get("title", ""), data.get("message", ""), payload, blocking=False
        )
        return
    _LOGGER.debug("Ignoring webhook with unknown event %r", event)


async def _handle(
    entry: PineappleCoreConfigEntry,
    hass: HomeAssistant,
    webhook_id: str,  # noqa: ARG001 — HA webhook handler signature
    request: Request,
) -> Response:
    """Decode a Core push and dispatch it (webhook handler signature)."""
    try:
        data = await request.json()
    except ValueError:
        _LOGGER.warning("Pineapple Core webhook received invalid JSON")
        return Response(status=HTTPStatus.BAD_REQUEST)
    if not isinstance(data, dict):
        return Response(status=HTTPStatus.BAD_REQUEST)
    await async_dispatch(hass, entry.runtime_data, data)
    return Response(status=HTTPStatus.OK)


async def async_register_webhook(
    hass: HomeAssistant, entry: PineappleCoreConfigEntry
) -> str | None:
    """Register the inbound webhook (minting an id if needed) and return its URL."""
    # Prefers a Nabu Casa cloudhook when a cloud subscription is active (so Core
    # can reach it off-LAN), else the local webhook URL. Returns None when HA has
    # no URL configured yet — the webhook still works on-LAN, the URL just isn't
    # resolvable. The resolved URL is what the user copies into `HA_WEBHOOK_URL`.
    webhook_id = entry.data.get(CONF_WEBHOOK_ID)
    if not webhook_id:
        webhook_id = async_generate_id()
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_WEBHOOK_ID: webhook_id}
        )
    async_register(
        hass,
        DOMAIN,
        "Pineapple Core",
        webhook_id,
        partial(_handle, entry),
        allowed_methods=["POST"],
    )
    return await _async_resolve_url(hass, entry, webhook_id)


async def _async_resolve_url(
    hass: HomeAssistant, entry: PineappleCoreConfigEntry, webhook_id: str
) -> str | None:
    """Cloudhook URL when cloud is active (created + cached once), else the local URL."""
    # `cloud` is imported lazily — it is only a soft (`after_`) dependency, and a
    # top-level import would drag its whole optional stack into every load/test.
    # Returns None when no local URL is resolvable yet (HA base URL unset).
    cloud = _cloud()
    try:
        cloud_active = cloud is not None and cloud.async_active_subscription(hass)
    except (KeyError, AttributeError):  # cloud not set up
        cloud_active = False
    if cloud and cloud_active:
        url = entry.data.get(CONF_CLOUDHOOK_URL)
        if not url:
            create = (
                getattr(cloud, "async_get_or_create_cloudhook", None)
                or cloud.async_create_cloudhook
            )
            url = await create(hass, webhook_id)
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_CLOUDHOOK_URL: url}
            )
        return url
    try:
        return async_generate_url(hass, webhook_id)
    except NoURLAvailableError:
        _LOGGER.warning("No Home Assistant URL configured yet — webhook URL unavailable")
        return None


def async_unregister_webhook(hass: HomeAssistant, entry: PineappleCoreConfigEntry) -> None:
    """Unregister the local handler on unload."""
    # The cloudhook is deliberately NOT deleted here — a reload unloads then
    # re-sets-up, and recreating a cloudhook mints a NEW URL, which would silently
    # break Core's stored `HA_WEBHOOK_URL`. Teardown belongs to entry removal.
    webhook_id = entry.data.get(CONF_WEBHOOK_ID)
    if webhook_id:
        async_unregister(hass, webhook_id)


async def async_remove_cloudhook(hass: HomeAssistant, entry: PineappleCoreConfigEntry) -> None:
    """Delete the cloudhook — only on full entry removal, not on reload."""
    cloud = _cloud()
    webhook_id = entry.data.get(CONF_WEBHOOK_ID)
    if cloud and webhook_id and entry.data.get(CONF_CLOUDHOOK_URL):
        try:
            await cloud.async_delete_cloudhook(hass, webhook_id)
        except (KeyError, AttributeError, ValueError):
            _LOGGER.debug("Cloudhook already gone for %s", webhook_id)
