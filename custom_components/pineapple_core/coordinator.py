"""Polls Core's upcoming queue and delivers each reminder locally on time."""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import partial
import logging
from typing import TYPE_CHECKING

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
    DEFAULT_NOTIFY_TARGET,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_WINDOW_HOURS,
    DOMAIN,
)

if TYPE_CHECKING:
    from . import PineappleCoreConfigEntry

_LOGGER = logging.getLogger(__name__)


class PineappleCoreCoordinator(DataUpdateCoordinator[list[Reminder]]):
    """Owns the local schedule of Core's upcoming reminders.

    Each poll re-syncs the queue and (re)arms a native ``async_track_point_in_time``
    callback per reminder, so notifications fire on time from the cached queue even
    if Core is unreachable at the firing instant. A locally-fired tag is remembered
    (`_fired`) so a failed ack can't cause a double-fire; the ack is retried each
    poll until Core confirms and drops it from the feed.
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
        self.client = PineappleCoreClient(
            async_get_clientsession(hass),
            entry.data[CONF_BASE_URL],
            entry.data[CONF_API_TOKEN],
        )
        self._notify_target: str = entry.data.get(CONF_NOTIFY_TARGET, DEFAULT_NOTIFY_TARGET)
        self._window_hours: int = entry.options.get(CONF_WINDOW_HOURS, DEFAULT_WINDOW_HOURS)
        self._scheduled: dict[str, CALLBACK_TYPE] = {}
        self._fired: set[str] = set()
        self._pending_acks: set[str] = set()

    async def _async_update_data(self) -> list[Reminder]:
        """Re-sync the queue, re-arm the schedule, and retry pending acks."""
        try:
            reminders = await self.client.async_get_upcoming(self._window_hours)
        except PineappleCoreAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except PineappleCoreError as err:
            raise UpdateFailed(str(err)) from err

        feed = {r.tag: r for r in reminders}
        # Drop schedules for reminders Core has cancelled since the last poll.
        for tag in [t for t in self._scheduled if t not in feed]:
            self._scheduled.pop(tag)()
        # (Re)arm every reminder we haven't already delivered.
        for tag, reminder in feed.items():
            if tag in self._fired:
                continue
            self._arm(reminder)
        # Retry acks Core hasn't confirmed yet (it drops confirmed ones from the feed).
        await self._retry_acks()
        # Forget delivered tags Core has dropped — they won't come back.
        self._fired = {t for t in self._fired if t in feed or t in self._pending_acks}
        return reminders

    @callback
    def _arm(self, reminder: Reminder) -> None:
        """Schedule (or reschedule) a reminder's local fire callback."""
        when = dt_util.parse_datetime(reminder.fire_at)
        if when is None:
            _LOGGER.warning("Skipping reminder %s: unparseable fire_at %r", reminder.tag, reminder.fire_at)
            return
        if reminder.tag in self._scheduled:
            self._scheduled.pop(reminder.tag)()
        self._scheduled[reminder.tag] = async_track_point_in_time(
            self.hass, partial(self._fire, reminder), when
        )

    @callback
    def _fire(self, reminder: Reminder, _now: datetime) -> None:
        """Native scheduler hit this reminder's time — deliver it."""
        self._scheduled.pop(reminder.tag, None)
        self.config_entry.async_create_task(
            self.hass, self._deliver(reminder), eager_start=False
        )

    async def _deliver(self, reminder: Reminder) -> None:
        """Fire the notification, then ack Core. Dedup + failure-retry safe."""
        if reminder.tag in self._fired:
            return
        self._fired.add(reminder.tag)  # claim before awaiting → no concurrent double-fire
        try:
            await self.hass.services.async_call(
                "notify",
                self._notify_target,
                {"title": reminder.title, "message": reminder.message, "data": reminder.data},
                blocking=True,
            )
        except Exception:  # noqa: BLE001 — a notify failure must retry, not vanish
            self._fired.discard(reminder.tag)  # let the next poll re-arm it
            _LOGGER.exception("notify.%s failed for %s — will retry", self._notify_target, reminder.tag)
            return
        self._pending_acks.add(reminder.tag)
        await self._retry_acks()

    async def _retry_acks(self) -> None:
        """Confirm delivered tags to Core; keep unconfirmed ones for the next try."""
        if not self._pending_acks:
            return
        try:
            await self.client.async_ack(list(self._pending_acks))
        except PineappleCoreError as err:
            _LOGGER.debug("Ack deferred (%s tags): %s", len(self._pending_acks), err)
            return
        self._pending_acks.clear()

    @callback
    def async_cancel_scheduled(self) -> None:
        """Cancel every armed fire callback — called on unload/shutdown."""
        for cancel in self._scheduled.values():
            cancel()
        self._scheduled.clear()
