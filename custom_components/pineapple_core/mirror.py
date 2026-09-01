"""HA → Core mirror: push watched entities' state to Core.

Replaces the hand-written `callback - bins status to core` AND `core_helper_schedule`
automations. The user picks entities in the options flow; when one changes, the
integration POSTs it to Core's `/api/integrations/ha/helper` as the whole state:

  {entity, state, attributes, unit, last_changed, last_updated}

Core owns the interpretation — a consumer (habits, settle-up, budgeting) reads
whichever part it needs. Two derived keys are added when they apply, because
Core's existing helper contract reads them directly:

  value   ← a numeric state (integral → int; Core expects 0|1|2 for done-states)
  next_at ← a date-ish state (the Ocado ISO deadline), or the `next_collection`
            attribute (UKBinCollectionData: state is "In N days", the date is a
            DD/MM/YYYY attribute — normalized to ISO here)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.json import JSONEncoder
from homeassistant.util import dt as dt_util

from .const import CONF_MIRROR_ENTITIES

if TYPE_CHECKING:
    from homeassistant.core import (
        CALLBACK_TYPE,
        Event,
        EventStateChangedData,
        HomeAssistant,
        State,
    )

    from . import PineappleCoreConfigEntry
    from .api import PineappleCoreClient

_LOGGER = logging.getLogger(__name__)

_IGNORE = {None, "", "unknown", "unavailable"}


def _to_iso(raw: str) -> str | None:
    """Normalize a date/datetime string to ISO, or None if it isn't one."""
    # Accepts ISO 8601 (Ocado's deadline) and DD/MM/YYYY (UKBinCollectionData's
    # `next_collection`). Core parses `next_at` with `new Date(...)`, which handles
    # ISO datetimes and `YYYY-MM-DD` but NOT `DD/MM/YYYY`.
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


def _json_safe(attributes: dict[str, Any]) -> dict[str, Any]:
    """Drop attribute values that cannot survive a JSON round-trip."""
    # Attributes are arbitrary Python — datetimes, enums, dataclasses. HA's own
    # encoder handles most of them; anything it still refuses is skipped rather
    # than failing the whole push.
    out: dict[str, Any] = {}
    for key, value in attributes.items():
        try:
            out[key] = json.loads(json.dumps(value, cls=JSONEncoder))
        except (TypeError, ValueError):
            _LOGGER.debug("Skipping unserialisable attribute %s", key)
    return out


def _payload(state: State) -> dict[str, Any]:
    """Build the full mirror payload for one entity's state."""
    payload: dict[str, Any] = {
        "entity": state.entity_id,
        "state": state.state,
        "attributes": _json_safe(dict(state.attributes)),
        "last_changed": state.last_changed.isoformat(),
        "last_updated": state.last_updated.isoformat(),
    }
    if (unit := state.attributes.get("unit_of_measurement")) is not None:
        payload["unit"] = unit
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        value = None
    if value is not None:
        payload["value"] = int(value) if value.is_integer() else value
    iso = _to_iso(state.state)
    if iso is None:
        raw = state.attributes.get("next_collection")
        iso = _to_iso(str(raw)) if raw not in _IGNORE else None
    if iso is not None:
        payload["next_at"] = iso
    return payload


@callback
def async_setup_mirror(
    hass: HomeAssistant, entry: PineappleCoreConfigEntry, client: PineappleCoreClient
) -> CALLBACK_TYPE:
    """Watch the configured entities; return an unsubscribe callback."""
    # A no-op unsubscribe when no entities are configured, so the caller can
    # always register it with `entry.async_on_unload`.
    entities: list[str] = entry.options.get(CONF_MIRROR_ENTITIES, [])
    if not entities:
        return lambda: None

    @callback
    def _sync(state: State | None) -> None:
        """Push one entity's whole state to Core under its own id."""
        # `unknown`/`unavailable` are skipped: they say nothing about the entity,
        # and mirroring them would overwrite a good value in Core with a gap.
        if state is None or state.state in _IGNORE:
            return
        hass.async_create_task(_push(client, _payload(state)))

    @callback
    def _on_change(event: Event[EventStateChangedData]) -> None:
        _sync(event.data.get("new_state"))

    unsub = async_track_state_change_event(hass, entities, _on_change)
    # Initial sync: reflect each watched entity's CURRENT state in Core right away,
    # not only on its next change — so an input_number that hasn't moved since setup
    # (or a sensor that won't change for a while) is already mirrored.
    for entity_id in entities:
        _sync(hass.states.get(entity_id))
    return unsub


async def _push(client: PineappleCoreClient, payload: dict[str, Any]) -> None:
    """Send one mirrored update to Core; a failure is logged, never raised."""
    from .api import PineappleCoreError  # noqa: PLC0415 — avoid a module import cycle

    try:
        await client.async_send_helper(payload)
    except PineappleCoreError as err:
        _LOGGER.warning("Could not mirror %s to Core: %s", payload["entity"], err)
