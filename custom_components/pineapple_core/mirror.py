"""HA → Core mirror: push watched entities' state to Core.

Replaces the hand-written `callback - bins status to core` AND `core_helper_schedule`
automations. The user picks entities in the options flow; when one changes, the
integration routes it to Core's `/api/integrations/ha/helper`:

  numeric state          → {entity, value}    (an input_number done-state)
  date-ish state         → {entity, next_at}  (e.g. the Ocado ISO deadline)
  `next_collection` attr → {entity, next_at}  (UKBinCollectionData: the state is
                                               "In N days", the date is a DD/MM/YYYY
                                               attribute — normalized to ISO here)

Core maps the entity to its linked habit/todo and applies the value/date.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import CONF_MIRROR_ALIASES, CONF_MIRROR_ENTITIES

if TYPE_CHECKING:
    from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant

    from . import PineappleCoreConfigEntry
    from .api import PineappleCoreClient

_LOGGER = logging.getLogger(__name__)

_IGNORE = {None, "", "unknown", "unavailable"}


def _parse_aliases(text: str) -> dict[str, str]:
    """Parse `source=target` lines into a {source_entity: core_key} map."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        source, sep, target = line.partition("=")
        if sep and (s := source.strip()) and (t := target.strip()):
            out[s] = t
    return out


def _to_iso(raw: str) -> str | None:
    """Normalize a date/datetime string to ISO, or None if it isn't one.

    Accepts ISO 8601 (Ocado's deadline) and DD/MM/YYYY (UKBinCollectionData's
    `next_collection`). Core parses `next_at` with `new Date(...)`, which handles
    ISO datetimes and `YYYY-MM-DD` but NOT `DD/MM/YYYY` — hence the normalization.
    """
    raw = raw.strip()
    if not raw:
        return None
    if (dtv := dt_util.parse_datetime(raw)) is not None:
        return dtv.isoformat()
    if (dov := dt_util.parse_date(raw)) is not None:
        return dov.isoformat()
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()  # noqa: DTZ007 — a plain date
        except ValueError:
            continue
    return None


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
    aliases = _parse_aliases(entry.options.get(CONF_MIRROR_ALIASES, ""))

    @callback
    def _on_change(event: Event[EventStateChangedData]) -> None:
        new = event.data.get("new_state")
        if new is None or new.state in _IGNORE:
            return
        # Push under the aliased Core key when configured, else the entity's own id —
        # so a bin date sensor can feed the same helper as its done-state input_number.
        key = aliases.get(new.entity_id, new.entity_id)
        # Numeric → a done-state value.
        try:
            value = float(new.state)
        except (TypeError, ValueError):
            value = None
        if value is not None:
            hass.async_create_task(_push(client, key, value=value))
            return
        # Date-ish state (Ocado) → next_at; else the bins date lives in an attribute.
        iso = _to_iso(new.state)
        if iso is None:
            raw = new.attributes.get("next_collection")
            iso = _to_iso(str(raw)) if raw not in _IGNORE else None
        if iso is not None:
            hass.async_create_task(_push(client, key, next_at=iso))

    return async_track_state_change_event(hass, entities, _on_change)


async def _push(
    client: PineappleCoreClient,
    entity: str,
    *,
    value: float | None = None,
    next_at: str | None = None,
) -> None:
    """Send one mirrored update to Core; a failure is logged, never raised."""
    from .api import PineappleCoreError  # noqa: PLC0415 — avoid a module import cycle

    try:
        await client.async_send_helper(entity, value=value, next_at=next_at)
    except PineappleCoreError as err:
        _LOGGER.warning("Could not mirror %s to Core: %s", entity, err)
