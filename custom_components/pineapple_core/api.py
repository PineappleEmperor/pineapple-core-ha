"""Thin client for the Pineapple Core reminder-delivery API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientResponseError, ClientSession

from .const import API_ACK, API_UPCOMING, REQUEST_TIMEOUT


class PineappleCoreError(Exception):
    """A Core request failed for a non-auth reason (network, 5xx, bad body)."""


class PineappleCoreAuthError(PineappleCoreError):
    """Core rejected the service token (401/403)."""


@dataclass(slots=True)
class Reminder:
    """One upcoming notification Core wants delivered.

    `data` is the ready-to-use companion-app payload block (tag, actions, push,
    …) — the integration relays it verbatim to the notify service, so Core stays
    the single source of truth for how a notification is shaped.
    """

    tag: str
    fire_at: str
    title: str
    message: str
    data: dict[str, Any]


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
            )
            for r in reminders
        ]

    async def async_ack(self, tags: list[str]) -> None:
        """Tell Core these tags were delivered so it doesn't re-queue them."""
        if not tags:
            return
        await self._request("POST", API_ACK, json={"tags": tags})

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
