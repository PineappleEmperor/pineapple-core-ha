"""HA → Core helper mirror: push watched entities' numeric state to Core.

Replaces the hand-written `callback - bins status to core` automation. The user
picks entities in the options flow; when one changes to a numeric state, its value
is POSTed to Core's `/api/integrations/ha/helper`, which maps the entity to its
linked habit/todo (e.g. a bin `input_number` flipping to 1 logs the habit period).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event

from .const import CONF_MIRROR_ENTITIES

if TYPE_CHECKING:
    from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant

    from . import PineappleCoreConfigEntry
    from .api import PineappleCoreClient

_LOGGER = logging.getLogger(__name__)


@callback
def async_setup_mirror(
    hass: HomeAssistant, entry: PineappleCoreConfigEntry, client: PineappleCoreClient
) -> CALLBACK_TYPE:
    """Watch the configured entities; return an unsubscribe callback.

    A no-op unsubscribe when no entities are configured, so the caller can always
    register it with `entry.async_on_unload`.
    """
    entities: list[str] = entry.options.get(CONF_MIRROR_ENTITIES, [])
    if not entities:
        return lambda: None

    @callback
    def _on_change(event: Event[EventStateChangedData]) -> None:
        new = event.data.get("new_state")
        if new is None:
            return
        try:
            value = float(new.state)
        except (TypeError, ValueError):
            return  # unavailable/unknown/non-numeric — nothing to mirror
        hass.async_create_task(_push(client, new.entity_id, value))

    return async_track_state_change_event(hass, entities, _on_change)


async def _push(client: PineappleCoreClient, entity: str, value: float) -> None:
    """Send one mirrored value to Core; a failure is logged, never raised."""
    from .api import PineappleCoreError  # noqa: PLC0415 — avoid a module import cycle

    try:
        await client.async_send_helper(entity, value)
    except PineappleCoreError as err:
        _LOGGER.warning("Could not mirror %s=%s to Core: %s", entity, value, err)
