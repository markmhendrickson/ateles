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


async def hydrate_snapshot(event: NeotomaEvent) -> NeotomaEvent:
    """
    Ensure ``event.snapshot`` is populated by fetching the entity if needed.

    The Neotoma SSE stream delivers only event metadata (entity_id, entity_type,
    action) — it does NOT embed the entity snapshot. Daemons that route on
    snapshot fields (tags, assigned_to, labels, title) therefore see an empty
    dict and drop every event unless they re-fetch. This helper performs that
    fetch in place: it GETs ``/entities/{entity_id}`` and fills
    ``event.snapshot`` from the response.

    Async so the fetch never blocks the daemon event loop: a slow Neotoma
    response yields to other tasks instead of stalling the whole dispatch loop.
    ``await`` it at the top of each handler.

    Open-mode aware: sends a Bearer header only when NEOTOMA_BEARER_TOKEN is set,
    matching how the stream itself connects to an open-mode Neotoma.

    Idempotent and fail-soft: a no-op when the snapshot is already present or the
    event has no entity_id; on any fetch error it logs and returns the event
    unchanged (with an empty snapshot) rather than raising, so a transient
    Neotoma blip never crashes the dispatch loop.
    """
    if event.snapshot or not event.entity_id:
        return event

    headers: dict = {}
    if NEOTOMA_BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {NEOTOMA_BEARER_TOKEN}"

    url = f"{NEOTOMA_BASE_URL}/entities/{event.entity_id}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        # GET /entities/{id} returns the computed snapshot fields directly under
        # the top-level "snapshot" key (title, tags, assigned_to, ...).
        snapshot = data.get("snapshot")
        event.snapshot = snapshot if isinstance(snapshot, dict) else {}
    except Exception as exc:  # noqa: BLE001 — never crash the dispatch loop
        log.warning(
            f"[sse] could not hydrate snapshot for {event.entity_type}/"
            f"{event.entity_id}: {exc}"
        )
    return event


EventHandler = Callable[[NeotomaEvent], Awaitable[None]]


class MissingSubscriptionError(RuntimeError):
    """Raised when a daemon's SSE subscription id resolves from nowhere.

    Replaces the previous behaviour — a single WARNING and a silent return —
    which made "subscribed and idle" indistinguishable from "consuming nothing".
    Apis sat in the second state for 88 days (67,450 skipped events, ~100
    stranded tasks) while looking healthy.
    """


def resolve_subscription_id(handler_name: str) -> str | None:
    """Resolve this daemon's SSE subscription id.

    Order: env var (operator override) → Neotoma `daemon_configuration` →
    local last-known-good cache. Returns None when all three miss, which the
    caller turns into a hard, named failure.

    Neotoma is time-boxed and cache-backed inside config_resolver, so a slow or
    down Neotoma costs a few seconds and falls back to cache — it never prevents
    a daemon from starting on config it already knows.
    """
    # Historical env keys, both consulted so nothing that works today breaks.
    env_key = f"NEOTOMA_SSE_SUBSCRIPTION_ID_{handler_name.upper()}"
    direct = os.environ.get(env_key) or os.environ.get(
        "NEOTOMA_SSE_SUBSCRIPTION_ID"
    )
    if direct and not (direct.startswith("__") and direct.endswith("__")):
        return direct

    try:
        from .config_resolver import ConfigSpec, resolve

        resolved = resolve(
            handler_name,
            [
                ConfigSpec(
                    key="sse_subscription_id",
                    env_var=env_key,
                    required=False,  # we raise our own, richer error below
                    secret_name="NEOTOMA_BEARER_TOKEN",
                    remedy=(
                        "create the subscription via the Neotoma `subscribe` tool "
                        "with delivery_method=sse, then record its id on the "
                        "daemon_configuration entity"
                    ),
                )
            ],
        )
        value = resolved.get("sse_subscription_id")
        if value:
            log.info(
                f"[{handler_name}] SSE subscription id resolved from "
                f"{resolved.source_of('sse_subscription_id')}"
            )
        return value or None
    except Exception as exc:  # noqa: BLE001 — resolution must not crash import
        log.warning(
            f"[{handler_name}] config resolution for sse_subscription_id "
            f"failed: {exc}"
        )
        return None


class SSEClient:
    """
    Async SSE client for the Neotoma entity event stream.

    entity_types: subscribe only to events for these entity types (empty = all)
    handler_name: used in log messages
    subscription_id: Neotoma SSE subscription UUID (required by the server).
        Pass the ID returned by POST /events/subscriptions (or the MCP
        `subscribe` tool with delivery_method="sse"). Set the env var
        NEOTOMA_SSE_SUBSCRIPTION_ID_<HANDLER_NAME_UPPER> to inject it at
        runtime without code changes.
    """

    def __init__(
        self,
        entity_types: list[str] | None = None,
        handler_name: str = "daemon",
        bearer_token: str | None = None,
        base_url: str | None = None,
        subscription_id: str | None = None,
    ) -> None:
        self.entity_types = entity_types or []
        self.handler_name = handler_name
        self._token = bearer_token or NEOTOMA_BEARER_TOKEN
        self._base_url = base_url or NEOTOMA_BASE_URL
        self._running = False
        self._subscription_id = subscription_id or resolve_subscription_id(
            handler_name
        )

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
            except MissingSubscriptionError:
                # Must propagate. Retrying cannot help — no amount of backoff
                # invents a subscription id — and catching it here would convert
                # the loud failure this class exists to raise back into a
                # warning in a quiet retry loop, which is the original defect.
                self._running = False
                raise
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
            # Open-mode Neotoma instances (no NEOTOMA_BEARER_TOKEN configured
            # server-side) accept unauthenticated requests and reject any bearer
            # token. Connect without an Authorization header rather than skipping.
            log.info(
                f"[{self.handler_name}] NEOTOMA_BEARER_TOKEN not set — "
                "connecting to SSE stream without auth (open-mode Neotoma)"
            )

        if not self._subscription_id:
            # LOUD FAILURE, deliberately. This previously logged one warning and
            # returned, leaving the daemon running and consuming nothing — the
            # exact shape of the 88-day Apis outage. A daemon whose entire job
            # is reacting to events, and which cannot subscribe, is not degraded
            # but healthy-looking; it is broken and must say so.
            env_key = f"NEOTOMA_SSE_SUBSCRIPTION_ID_{self.handler_name.upper()}"
            raise MissingSubscriptionError(
                f"[{self.handler_name}] SSE SUBSCRIPTION ID UNRESOLVED — this "
                f"daemon consumes events and cannot subscribe, so it would "
                f"process NOTHING while appearing healthy. Refusing to run.\n"
                f"  Tried: env {env_key}, env NEOTOMA_SSE_SUBSCRIPTION_ID, "
                f"Neotoma daemon_configuration(daemon_name="
                f"{self.handler_name!r}).config.sse_subscription_id, local cache.\n"
                f"  Fix: create a subscription (Neotoma `subscribe` tool, "
                f"delivery_method=sse), then record its id as "
                f"`sse_subscription_id` on the daemon_configuration entity for "
                f"{self.handler_name!r} (preferred — versioned and queryable), "
                f"or set {env_key} as a stopgap."
            )

        url = f"{self._base_url}/events/stream"
        params: dict[str, str] = {"subscription_id": self._subscription_id}
        if self.entity_types:
            params["entity_types"] = ",".join(self.entity_types)

        headers = {
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

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
                            # exc_info: without the traceback this logs only
                            # the exception text, which names neither the file
                            # nor the line — a handler fault then has to be
                            # re-reproduced by hand before it can be read.
                            log.error(
                                f"[{self.handler_name}] Handler error for "
                                f"{event.entity_type}/{event.entity_id}: {exc}",
                                exc_info=True,
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
