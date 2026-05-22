"""
lib/daemon_runtime/sse_client.py — Neotoma SSE subscription for T3 daemons.

Subscribes to the Neotoma entity event stream. Daemons use this to react
to entity changes (tasks due, issues created, etc.) without polling.

Usage:

    async def handle_event(event: NeotomaEvent):
        if event.entity_type == "task" and event.action == "updated":
            ...

    sse = SSEClient(entity_types=["task", "event"])
    await sse.stream(handle_event)

The stream auto-reconnects with exponential backoff on disconnect.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")

SSE_RECONNECT_DELAY_BASE = 2  # seconds
SSE_RECONNECT_DELAY_MAX = 60  # seconds


@dataclass
class NeotomaEvent:
    """A single event from the Neotoma SSE stream."""

    event_type: str = ""  # e.g. "entity_updated", "entity_created"
    entity_type: str = ""  # e.g. "task", "event"
    entity_id: str = ""
    action: str = ""  # created | updated | deleted
    snapshot: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_raw(cls, data: dict) -> NeotomaEvent:
        return cls(
            event_type=data.get("event_type", ""),
            entity_type=data.get("entity_type", ""),
            entity_id=data.get("entity_id", ""),
            action=data.get("action", ""),
            snapshot=data.get("snapshot") or {},
            raw=data,
        )


EventHandler = Callable[[NeotomaEvent], Awaitable[None]]


class SSEClient:
    """
    Async SSE client for the Neotoma entity event stream.

    entity_types: subscribe only to events for these entity types (empty = all)
    handler_name: used in log messages
    """

    def __init__(
        self,
        entity_types: list[str] | None = None,
        handler_name: str = "daemon",
        bearer_token: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.entity_types = entity_types or []
        self.handler_name = handler_name
        self._token = bearer_token or NEOTOMA_BEARER_TOKEN
        self._base_url = base_url or NEOTOMA_BASE_URL
        self._running = False

    async def stream(
        self,
        handler: EventHandler,
        reconnect: bool = True,
    ) -> None:
        """
        Subscribe to Neotoma SSE stream and call handler for each event.
        Runs until stop() is called or reconnect=False and stream ends.
        """
        self._running = True
        delay = SSE_RECONNECT_DELAY_BASE

        while self._running:
            try:
                await self._connect_and_stream(handler)
                delay = SSE_RECONNECT_DELAY_BASE  # reset on clean disconnect
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning(
                    f"[{self.handler_name}] SSE stream error: {exc} — "
                    f"reconnecting in {delay}s"
                )
                if not reconnect:
                    break
                await asyncio.sleep(delay)
                delay = min(delay * 2, SSE_RECONNECT_DELAY_MAX)

    def stop(self) -> None:
        self._running = False

    async def _connect_and_stream(self, handler: EventHandler) -> None:
        if not self._token:
            log.warning(
                f"[{self.handler_name}] NEOTOMA_BEARER_TOKEN not set — "
                "SSE subscription skipped"
            )
            self._running = False
            return

        url = f"{self._base_url}/events/stream"
        params: dict[str, str] = {}
        if self.entity_types:
            params["entity_types"] = ",".join(self.entity_types)

        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        }

        log.info(
            f"[{self.handler_name}] Connecting to SSE stream "
            f"(entity_types={self.entity_types or 'all'})"
        )

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "GET", url, headers=headers, params=params
            ) as resp:
                resp.raise_for_status()
                log.info(f"[{self.handler_name}] SSE stream connected.")
                async for event in _parse_sse(resp):
                    if not self._running:
                        break
                    if event:
                        try:
                            await handler(event)
                        except Exception as exc:
                            log.error(
                                f"[{self.handler_name}] Handler error for "
                                f"{event.entity_type}/{event.entity_id}: {exc}"
                            )


async def _parse_sse(resp: httpx.Response) -> AsyncIterator[NeotomaEvent | None]:
    """Parse SSE lines into NeotomaEvent objects."""
    data_lines: list[str] = []
    async for line in resp.aiter_lines():
        line = line.strip()
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif line == "" and data_lines:
            raw_str = "\n".join(data_lines)
            data_lines = []
            if raw_str in ("", "ping", ":ping"):
                yield None
                continue
            try:
                data = json.loads(raw_str)
                yield NeotomaEvent.from_raw(data)
            except json.JSONDecodeError:
                log.debug(f"[sse] Non-JSON event data: {raw_str[:100]!r}")
                yield None
        elif line.startswith(":"):
            # SSE comment / heartbeat
            yield None
