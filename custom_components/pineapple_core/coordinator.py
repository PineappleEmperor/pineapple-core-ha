"""Polls Core's upcoming queue and delivers each reminder locally on time."""

from __future__ import annotations

import logging
import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    PineappleCoreAuthError,
    PineappleCoreClient,
    PineappleCoreError,
    Reminder,
)
from .const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    CONF_NOTIFY_TARGET,
    CONF_POLL_INTERVAL,
    CONF_WINDOW_HOURS,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_WINDOW_HOURS,
    DOMAIN,
)
from .helpers import instance_label

if TYPE_CHECKING:
    from homeassistant.core import Event

    from . import PineappleCoreConfigEntry

_LOGGER = logging.getLogger(__name__)

# iOS interruption levels, quietest → loudest — the escalation ladder a nagging
# reminder climbs one rung per repeat when its policy is `escalate`.
_LEVELS = ("passive", "active", "time-sensitive", "critical")

# Cap on remembered action token → tag pairs. The map is what proves a tapped
# action belongs to THIS entry, so it must outlive the feed (a tap can land long
# after Core dropped the reminder); a FIFO cap bounds it instead.
_MAX_ACTION_TOKENS = 200


def _interval_seconds(interval: str | None) -> int | None:
    """Parse a Core nag interval (`"15m"`, `"1h"`) into seconds, or None."""
    if not interval:
        return None
    m = re.fullmatch(r"(\d+)([mh])", interval.strip())
    if not m:
        return None
    n = int(m.group(1))
    return n * (3600 if m.group(2) == "h" else 60) or None


def _payload(reminder: Reminder, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a reminder's notification data, guaranteed to carry its dedup tag.

    Dismissal (`clear_notification`) matches on `data.tag`, so a reminder whose
    Core payload omits it could never be dismissed.
    """
    out = dict(reminder.data if data is None else data)
    out.setdefault("tag", reminder.tag)
    return out


def _escalated(data: dict[str, Any], steps: int) -> dict[str, Any]:
    """Return a copy of the notification data with its level raised `steps` rungs."""
    out = deepcopy(data)
    push = out.setdefault("push", {})
    base = push.get("interruption-level", "active")
    idx = _LEVELS.index(base) if base in _LEVELS else 1
    push["interruption-level"] = _LEVELS[min(idx + steps, len(_LEVELS) - 1)]
    return out


@dataclass
class _Nag:
    """The live nag chain for one delivered reminder."""

    reminder: Reminder
    interval: int
    count: int = 0  # repeats sent so far
    cancel: CALLBACK_TYPE | None = None


@dataclass
class _State:
    """All per-reminder delivery state, keyed by tag.

    Kept together so pruning a tag can never leave a dangling schedule, nag, or
    ack behind.
    """

    scheduled: dict[str, CALLBACK_TYPE] = field(default_factory=dict)
    fired: set[str] = field(default_factory=set)
    handled: set[str] = field(default_factory=set)
    nags: dict[str, _Nag] = field(default_factory=dict)
    pending_acks: dict[str, dict[str, Any]] = field(default_factory=dict)
    action_tags: dict[str, str] = field(default_factory=dict)  # action token → tag


class PineappleCoreCoordinator(DataUpdateCoordinator[list[Reminder]]):
    """Owns the local schedule of Core's upcoming reminders.

    Each poll re-syncs the queue and (re)arms a native ``async_track_point_in_time``
    callback per reminder, so notifications fire on time from the cached queue even
    if Core is unreachable at the firing instant. A delivered reminder repeats its
    own nag chain locally (Core hands over the policy but not the timing), and a
    tapped action is forwarded straight to Core's capability webhook while its local
    nags are cancelled at once. A locally-fired tag is remembered so a failed ack
    can't double-fire; the ack is retried each poll until Core drops it.
    """

    def __init__(self, hass: HomeAssistant, entry: PineappleCoreConfigEntry) -> None:
        """Build the client from the entry and set the poll cadence."""
        interval = entry.options.get(CONF_POLL_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(minutes=interval) if interval else DEFAULT_POLL_INTERVAL,
            always_update=False,
        )
        self.base_url: str = entry.data[CONF_BASE_URL]
        self.client = PineappleCoreClient(
            async_get_clientsession(hass),
            self.base_url,
            entry.data[CONF_API_TOKEN],
        )
        self.entry_id: str = entry.entry_id
        self.device_name: str = entry.title
        self._tag_prefix: str = f"{instance_label(self.base_url)}_"
        self._notify_target: str = entry.data[CONF_NOTIFY_TARGET]
        self._window_hours: int = entry.options.get(CONF_WINDOW_HOURS, DEFAULT_WINDOW_HOURS)
        # The integration's own inbound webhook URL (set at setup) — surfaced on a
        # diagnostic sensor so the user can copy it into Core's HA_WEBHOOK_URL.
        self.webhook_url: str | None = None
        self._s = _State()

    # --- backwards-compatible views the tests (and diagnostics) read ---
    @property
    def notify_target(self) -> str:
        """The notify service reminders + Core pushes are delivered to."""
        return self._notify_target

    @property
    def _scheduled(self) -> dict[str, CALLBACK_TYPE]:
        return self._s.scheduled

    @property
    def _fired(self) -> set[str]:
        return self._s.fired

    @property
    def _pending_acks(self) -> dict[str, dict[str, Any]]:
        return self._s.pending_acks

    async def _async_update_data(self) -> list[Reminder]:
        """Re-sync the queue, re-arm the schedule, and retry pending acks."""
        try:
            reminders = await self.client.async_get_upcoming(self._window_hours)
        except PineappleCoreAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except PineappleCoreError as err:
            raise UpdateFailed(str(err)) from err

        feed = {r.tag: r for r in reminders}
        # Drop only PRE-fire schedules for reminders Core cancelled since the last poll.
        # A nag chain is NOT cancelled here: once a reminder is delivered we ack it, so
        # Core drops it from the feed by design — the local nag chain must outlive that
        # (it's stopped by a tap, a Core `clear` push, or reaching max, never by the
        # feed simply no longer listing an already-delivered reminder).
        for tag in [t for t in self._s.scheduled if t not in feed]:
            self._s.scheduled.pop(tag)()
        # (Re)arm every reminder we haven't already delivered or had acted on.
        for tag, reminder in feed.items():
            if tag in self._s.fired or tag in self._s.handled:
                continue
            self._arm(reminder)
        # Retry acks Core hasn't confirmed yet (it drops confirmed ones from the feed).
        await self._retry_acks()
        # Forget state for tags Core has dropped — they won't come back.
        self._prune(feed)
        return reminders

    @callback
    def _prune(self, feed: dict[str, Reminder]) -> None:
        """Drop remembered state for tags gone from the feed and not still owed.

        A tag with a live nag chain is kept regardless of the feed — the chain runs
        locally after we've acked the reminder away, and it still needs its dedup
        memory until it's stopped. `action_tags` is deliberately not pruned here:
        it is the proof a tapped token is ours, and a tap can land long after Core
        dropped the reminder, so it is FIFO-capped instead.
        """
        keep = set(feed) | set(self._s.pending_acks) | set(self._s.nags)
        self._s.fired = {t for t in self._s.fired if t in keep}
        self._s.handled = {t for t in self._s.handled if t in feed}

    @callback
    def _arm(self, reminder: Reminder) -> None:
        """Schedule (or reschedule) a reminder's local fire callback."""
        when = dt_util.parse_datetime(reminder.fire_at)
        if when is None:
            _LOGGER.warning(
                "Skipping reminder %s: unparseable fire_at %r", reminder.tag, reminder.fire_at
            )
            return
        if reminder.tag in self._s.scheduled:
            self._s.scheduled.pop(reminder.tag)()
        self._s.scheduled[reminder.tag] = async_track_point_in_time(
            self.hass, partial(self._fire, reminder), when
        )

    @callback
    def _fire(self, reminder: Reminder, _now: datetime) -> None:
        """Native scheduler hit this reminder's time — deliver it."""
        self._s.scheduled.pop(reminder.tag, None)
        self.hass.async_create_task(self._deliver(reminder))

    async def _deliver(self, reminder: Reminder) -> None:
        """Fire the notification, ack Core, and start its nag chain."""
        if reminder.tag in self._s.fired:
            return
        self._s.fired.add(reminder.tag)  # claim before awaiting → no concurrent double-fire
        self.note_actions(reminder.tag, reminder.data)
        if not await self.async_notify(
            reminder.title, reminder.message, _payload(reminder)
        ):
            self._s.fired.discard(reminder.tag)  # let the next poll re-arm it
            for token in [t for t, tag in self._s.action_tags.items() if tag == reminder.tag]:
                self._s.action_tags.pop(token, None)
            return
        if reminder.ack:
            self._s.pending_acks[reminder.tag] = reminder.ack
            await self._retry_acks()
        self._start_nag(reminder)

    def namespaced_tag(self, tag: str) -> str:
        """Prefix a Core tag with this instance's label.

        The companion app dedups notifications by tag across the whole phone, so
        two Core instances sharing a notify target would overwrite each other's
        notifications on any tag they both use.
        """
        return f"{self._tag_prefix}{tag}"

    async def async_notify(
        self, title: str, message: str, data: dict[str, Any], *, blocking: bool = True
    ) -> bool:
        """Call the notify service with this instance's tag namespace applied."""
        payload = dict(data)
        if tag := payload.get("tag"):
            payload["tag"] = self.namespaced_tag(str(tag))
        try:
            await self.hass.services.async_call(
                "notify",
                self._notify_target,
                {"title": title, "message": message, "data": payload},
                blocking=blocking,
            )
        except Exception:
            _LOGGER.exception("notify.%s failed", self._notify_target)
            return False
        return True

    # --- nag chain (slice 4) ---
    @callback
    def _start_nag(self, reminder: Reminder) -> None:
        """Arm the first repeat if Core gave this reminder a nag policy."""
        nag = reminder.nag or {}
        seconds = _interval_seconds(nag.get("interval"))
        if seconds is None or nag.get("max", 0) <= 0:
            return
        self._cancel_nag(reminder.tag)
        self._s.nags[reminder.tag] = _Nag(reminder=reminder, interval=seconds)
        self._schedule_nag(reminder.tag)

    @callback
    def _schedule_nag(self, tag: str) -> None:
        """Arm the next repeat for a live nag chain."""
        nag = self._s.nags.get(tag)
        if nag is None:
            return
        when = dt_util.utcnow() + timedelta(seconds=nag.interval)
        nag.cancel = async_track_point_in_time(self.hass, partial(self._nag_fire, tag), when)

    @callback
    def _nag_fire(self, tag: str, _now: datetime) -> None:
        """Re-send the notification (escalated) when a nag interval elapses, if still due."""
        nag = self._s.nags.get(tag)
        if nag is None or tag in self._s.handled:
            self._cancel_nag(tag)
            return
        # Detach the handle that fired us (a no-op when the native timer already
        # consumed it; cancels a still-armed one when driven directly).
        if nag.cancel:
            nag.cancel()
        nag.cancel = None
        nag.count += 1
        escalate = bool((nag.reminder.nag or {}).get("escalate"))
        data = _escalated(nag.reminder.data, nag.count) if escalate else nag.reminder.data
        self.hass.async_create_task(
            self.async_notify(
                nag.reminder.title, nag.reminder.message, _payload(nag.reminder, data)
            )
        )
        if nag.count < (nag.reminder.nag or {}).get("max", 0):
            self._schedule_nag(tag)
        else:
            self._s.nags.pop(tag, None)

    @callback
    def _cancel_nag(self, tag: str) -> None:
        """Cancel and forget a reminder's nag chain."""
        nag = self._s.nags.pop(tag, None)
        if nag and nag.cancel:
            nag.cancel()

    @callback
    def note_external_clear(self, tag: str) -> None:
        """Stop nagging a reminder cleared elsewhere (Core's `clear` webhook push).

        When the item is completed in the app, Core clears the notification; that
        clear now also ends the local nag chain, so a handled reminder stops nagging
        without waiting for a tap on the phone.
        """
        if not tag:
            return
        self._s.handled.add(tag)
        self._cancel_nag(tag)
        if tag in self._s.scheduled:
            self._s.scheduled.pop(tag)()

    # --- action forwarding (slice 5) ---
    @callback
    def note_actions(self, tag: str, data: dict[str, Any]) -> None:
        """Record which action tokens belong to a notification this entry sent."""
        for act in data.get("actions", []):
            if isinstance(act, dict) and (token := act.get("action")):
                self._s.action_tags.pop(token, None)
                self._s.action_tags[token] = tag
        while len(self._s.action_tags) > _MAX_ACTION_TOKENS:
            self._s.action_tags.pop(next(iter(self._s.action_tags)))

    @callback
    def handle_action_event(self, event: Event) -> None:
        """Forward a tapped companion-app action to Core and stop its nag chain.

        The event only carries the compact `action` token, and the event bus is
        global: with two Core entries loaded, both see every tap. Only the entry
        that sent the notification may forward the token — otherwise the other
        Core is handed an action it never issued. If the token also belongs to a
        reminder we delivered, cancel its local nag chain and dismiss the
        notification right away — no waiting for the next poll.
        """
        token = event.data.get("action")
        if not token:
            return
        tag = self._s.action_tags.get(token)
        if tag is None:
            return  # another entry's notification (or another integration's)
        self.hass.async_create_task(self._forward_action(token))
        if not tag:
            return  # ours, but it carried no tag — nothing local to cancel
        self._s.handled.add(tag)
        self._cancel_nag(tag)
        if tag in self._s.scheduled:
            self._s.scheduled.pop(tag)()
        self.hass.async_create_task(self._clear(tag))

    async def _forward_action(self, token: str) -> None:
        """Relay a tapped action token to Core; best-effort (the user can re-tap)."""
        try:
            await self.client.async_send_action(token)
        except PineappleCoreError as err:
            _LOGGER.warning("Could not forward tapped action to Core: %s", err)

    async def _clear(self, tag: str) -> None:
        """Dismiss the delivered notification off the phone by its tag."""
        await self.async_notify("", "clear_notification", {"tag": tag})

    async def _retry_acks(self) -> None:
        """Confirm delivered entries to Core; keep unconfirmed ones for the next try."""
        if not self._s.pending_acks:
            return
        try:
            await self.client.async_ack(list(self._s.pending_acks.values()))
        except PineappleCoreError as err:
            _LOGGER.debug("Ack deferred (%s entries): %s", len(self._s.pending_acks), err)
            return
        self._s.pending_acks.clear()

    @callback
    def async_cancel_scheduled(self) -> None:
        """Cancel every armed fire + nag callback — called on unload/shutdown."""
        for cancel in self._s.scheduled.values():
            cancel()
        self._s.scheduled.clear()
        for tag in list(self._s.nags):
            self._cancel_nag(tag)
