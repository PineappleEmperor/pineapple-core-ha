"""Thin client for the Pineapple Core reminder-delivery API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientResponseError, ClientSession

from .const import API_ACK, API_ACTION, API_HELPER, API_UPCOMING, REQUEST_TIMEOUT


class PineappleCoreError(Exception):
    """A Core request failed for a non-auth reason (network, 5xx, bad body)."""


class PineappleCoreAuthError(PineappleCoreError):
    """Core rejected the service token (401/403)."""


@dataclass(slots=True)
class Reminder:
    """One upcoming notification Core wants delivered."""

    tag: str
    fire_at: str
    title: str
    message: str
    # The ready-to-use companion-app payload block (tag, actions, push, …),
    # relayed verbatim — Core stays the single source of truth for how a
    # notification is shaped.
    data: dict[str, Any]
    priority: int = 3
    # Core's repeat policy, scheduled locally: {interval, max, escalate}, or None
    # to fire once.
    nag: dict[str, Any] | None = None
    # The opaque retire-token echoed back on delivery ({"row": id} or
    # {"marker": {...}}) — never interpreted here, only relayed.
    ack: dict[str, Any] | None = None


class PineappleCoreClient:
    """Reads the upcoming queue and acknowledges deliveries."""

    def __init__(self, session: ClientSession, base_url: str, token: str) -> None:
        """Store the injected HA aiohttp session and Core credentials."""
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._headers = {"authorization": f"Bearer {token}"}

    async def async_get_upcoming(self, window_hours: int) -> list[Reminder]:
        """Fetch the reminders Core wants delivered within the window."""
        body = await self._request(
            "GET", API_UPCOMING, params={"window_hours": window_hours}
        )
        # Core wraps every response in a {"data": …} envelope.
        data = body.get("data") if isinstance(body, dict) else None
        reminders = data.get("reminders", []) if isinstance(data, dict) else []
        return [
            Reminder(
                tag=r["tag"],
                fire_at=r["fire_at"],
                title=r.get("title", ""),
                message=r.get("message", ""),
                data=r.get("data", {}),
                priority=r.get("priority", 3),
                nag=r.get("nag"),
                ack=r.get("ack"),
            )
            for r in reminders
        ]

    async def async_ack(self, acks: list[dict[str, Any]]) -> None:
        """Tell Core these entries were delivered so it retires them."""
        # Each ack is the entry's own `ack` object relayed verbatim — Core marks
        # the backing row sent, or writes the med fire-once marker.
        if not acks:
            return
        await self._request("POST", API_ACK, json={"acks": acks})

    async def async_send_action(self, token: str) -> None:
        """Forward a tapped notification action to Core's capability webhook."""
        # Core verifies the single-use `tok` and applies the domain action (log
        # dose / complete todo / …). The `/api/webhook/` route self-authenticates
        # via that token, so no bearer is sent. Core's own clear is left to run —
        # it dismisses the entity's sibling notifications too, which the local
        # single-tag clear does not.
        try:
            async with (
                asyncio.timeout(REQUEST_TIMEOUT),
                self._session.post(f"{self._base_url}{API_ACTION}", params={"tok": token}) as resp,
            ):
                resp.raise_for_status()
        except (TimeoutError, ClientError) as err:
            msg = f"Could not forward action to Core: {err}"
            raise PineappleCoreError(msg) from err

    async def async_send_helper(
        self, entity: str, *, value: float | None = None, next_at: str | None = None
    ) -> None:
        """Mirror a watched entity back to Core (HA → Core helper)."""
        # Sends `value` (an input_number done-state → logs the linked habit/todo)
        # or `next_at` (an externally-scheduled date → drives the item's
        # reminder), matching Core's helper contract. An integral value goes as an
        # int, since Core expects 0|1|2.
        payload: dict[str, Any] = {"entity": entity}
        if value is not None:
            payload["value"] = int(value) if float(value).is_integer() else value
        if next_at is not None:
            payload["next_at"] = next_at
        await self._request("POST", API_HELPER, json=payload)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """One authed Core call, mapping failures onto our error types."""
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with self._session.request(
                    method,
                    f"{self._base_url}{path}",
                    headers=self._headers,
                    params=params,
                    json=json,
                ) as resp:
                    if resp.status in (401, 403):
                        msg = f"Core rejected the token ({resp.status})"
                        # Raised here so callers can distinguish auth from other failures.
                        raise PineappleCoreAuthError(msg)  # noqa: TRY301
                    resp.raise_for_status()
                    if resp.content_type == "application/json":
                        return await resp.json()
                    return None
        except PineappleCoreAuthError:
            raise
        except ClientResponseError as err:
            msg = f"Core returned {err.status}"
            raise PineappleCoreError(msg) from err
        except (TimeoutError, ClientError) as err:
            msg = f"Could not reach Core: {err}"
            raise PineappleCoreError(msg) from err
