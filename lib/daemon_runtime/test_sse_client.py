"""
Tests for lib/daemon_runtime/sse_client.py — specifically hydrate_snapshot,
which fills an SSE event's snapshot by fetching the entity (the stream itself
delivers only metadata).

hydrate_snapshot is async (it must not block the daemon event loop), so each
test drives it via asyncio.run — the repo has no pytest-asyncio dependency.
"""

from __future__ import annotations

import asyncio

import lib.daemon_runtime.sse_client as sc
from lib.daemon_runtime.sse_client import NeotomaEvent, hydrate_snapshot


class _FakeResp:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            raise RuntimeError(f"HTTP {self._status}")

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient as an async context manager."""

    def __init__(self, payload=None, *, capture=None, raises=None, **kwargs):
        self._payload = payload
        self._capture = capture
        self._raises = raises

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        if self._capture is not None:
            self._capture["url"] = url
            self._capture["headers"] = headers or {}
        if self._raises is not None:
            raise self._raises
        return _FakeResp(self._payload or {})


def _install_fake_client(monkeypatch, payload=None, *, capture=None, raises=None):
    def factory(**kwargs):
        return _FakeAsyncClient(payload, capture=capture, raises=raises, **kwargs)

    monkeypatch.setattr(sc.httpx, "AsyncClient", factory)


def test_empty_snapshot_is_hydrated_from_entity(monkeypatch):
    _install_fake_client(
        monkeypatch,
        payload={"snapshot": {"title": "T", "tags": ["neotoma"], "assigned_to": "cicada"}},
    )
    ev = NeotomaEvent(entity_type="task", entity_id="ent_x", action="created")
    asyncio.run(hydrate_snapshot(ev))
    assert ev.snapshot["tags"] == ["neotoma"]
    assert ev.snapshot["assigned_to"] == "cicada"


def test_populated_snapshot_is_left_alone_no_fetch(monkeypatch):
    def factory(**kwargs):
        raise AssertionError("should not construct a client when snapshot present")

    monkeypatch.setattr(sc.httpx, "AsyncClient", factory)
    ev = NeotomaEvent(
        entity_type="task", entity_id="ent_x", action="created",
        snapshot={"title": "already here"},
    )
    asyncio.run(hydrate_snapshot(ev))
    assert ev.snapshot["title"] == "already here"


def test_missing_entity_id_is_noop(monkeypatch):
    def factory(**kwargs):
        raise AssertionError("should not fetch without an entity_id")

    monkeypatch.setattr(sc.httpx, "AsyncClient", factory)
    ev = NeotomaEvent(entity_type="task", entity_id="", action="created")
    asyncio.run(hydrate_snapshot(ev))
    assert ev.snapshot == {}


def test_fetch_error_fails_soft(monkeypatch):
    _install_fake_client(monkeypatch, raises=RuntimeError("neotoma down"))
    ev = NeotomaEvent(entity_type="task", entity_id="ent_x", action="created")
    # must not raise — dispatch loop should survive a transient blip
    asyncio.run(hydrate_snapshot(ev))
    assert ev.snapshot == {}


def test_open_mode_sends_no_auth_header(monkeypatch):
    cap: dict = {}
    monkeypatch.setattr(sc, "NEOTOMA_BEARER_TOKEN", "")
    _install_fake_client(monkeypatch, payload={"snapshot": {"title": "T"}}, capture=cap)
    ev = NeotomaEvent(entity_type="task", entity_id="ent_x", action="created")
    asyncio.run(hydrate_snapshot(ev))
    assert "Authorization" not in cap["headers"]


def test_bearer_mode_sends_auth_header(monkeypatch):
    cap: dict = {}
    monkeypatch.setattr(sc, "NEOTOMA_BEARER_TOKEN", "tok-123")
    _install_fake_client(monkeypatch, payload={"snapshot": {"title": "T"}}, capture=cap)
    ev = NeotomaEvent(entity_type="task", entity_id="ent_x", action="created")
    asyncio.run(hydrate_snapshot(ev))
    assert cap["headers"].get("Authorization") == "Bearer tok-123"


def test_non_dict_snapshot_falls_back_to_empty(monkeypatch):
    _install_fake_client(monkeypatch, payload={"snapshot": None})
    ev = NeotomaEvent(entity_type="task", entity_id="ent_x", action="created")
    asyncio.run(hydrate_snapshot(ev))
    assert ev.snapshot == {}


# ── hydrated flag: a FAILED read must not read as an empty entity ────────────
#
# The no-owner loop's routing half. hydrate_snapshot used to leave snapshot={}
# on any fetch error, which callers could not tell apart from a task that
# genuinely has no tags — so a transient 502 made a fully-tagged task look
# unroutable. ent_c192afd8760fd9f3fbd3c08c (title + description + five tags)
# was escalated three times; its real tags were logged at 16:22:05 and `tags=[]`
# at 16:25:25, right after a 502 on this exact GET.


def test_successful_hydration_marks_hydrated(monkeypatch):
    _install_fake_client(monkeypatch, payload={"snapshot": {"title": "T", "tags": ["ops"]}})
    ev = NeotomaEvent(entity_type="task", entity_id="ent_x", action="created")
    asyncio.run(hydrate_snapshot(ev))
    assert ev.hydrated is True


def test_failed_hydration_leaves_hydrated_false(monkeypatch):
    _install_fake_client(monkeypatch, raises=RuntimeError("502 Bad Gateway"))
    ev = NeotomaEvent(entity_type="task", entity_id="ent_x", action="created")
    asyncio.run(hydrate_snapshot(ev))
    assert ev.snapshot == {}
    assert ev.hydrated is False, "a failed read must not look like an empty entity"


def test_genuinely_empty_entity_is_hydrated_true(monkeypatch):
    """The distinction that matters: read OK but the entity really has no fields."""
    _install_fake_client(monkeypatch, payload={"snapshot": {}})
    ev = NeotomaEvent(entity_type="task", entity_id="ent_x", action="created")
    asyncio.run(hydrate_snapshot(ev))
    assert ev.snapshot == {}
    assert ev.hydrated is True


def test_embedded_snapshot_counts_as_hydrated():
    ev = NeotomaEvent.from_raw(
        {"entity_type": "task", "entity_id": "ent_x", "action": "created",
         "snapshot": {"title": "T"}}
    )
    assert ev.hydrated is True


def test_event_without_snapshot_starts_unhydrated():
    ev = NeotomaEvent.from_raw(
        {"entity_type": "task", "entity_id": "ent_x", "action": "created"}
    )
    assert ev.hydrated is False


def test_prehydrated_event_short_circuits_as_hydrated(monkeypatch):
    def factory(**kwargs):
        raise AssertionError("should not fetch when the snapshot is present")

    monkeypatch.setattr(sc.httpx, "AsyncClient", factory)
    ev = NeotomaEvent(
        entity_type="task", entity_id="ent_x", action="created",
        snapshot={"title": "already here"},
    )
    asyncio.run(hydrate_snapshot(ev))
    assert ev.hydrated is True


# ── Stream liveness ──────────────────────────────────────────────────────────
#
# These drive SSEClient against a real local HTTP server (aiohttp), because the
# read timeout is enforced by httpx's network layer: a fake client or a mock
# transport would never raise ReadTimeout and so could not go red. Limits are
# shrunk to fractions of a second via monkeypatch.

import pytest  # noqa: E402

aiohttp_web = pytest.importorskip("aiohttp.web")

_PING = 'event: ping\ndata: {"t": 1}\n\n'


def _task_event(entity_id: str) -> str:
    return (
        "id: 1\nevent: entity_created\n"
        f'data: {{"event_type": "entity_created", "entity_type": "task", '
        f'"entity_id": "{entity_id}", "action": "created"}}\n\n'
    )


async def _serve(behaviours):
    """Start a server whose Nth connection runs ``behaviours[N]`` (the last
    one repeats). Returns (runner, base_url, connection_counter, closing);
    setting ``closing`` ends every open stream so cleanup is immediate."""
    count = {"n": 0}
    closing = asyncio.Event()

    async def handler(request):
        idx = min(count["n"], len(behaviours) - 1)
        count["n"] += 1
        resp = aiohttp_web.StreamResponse(
            headers={"Content-Type": "text/event-stream"}
        )
        await resp.prepare(request)
        body = asyncio.ensure_future(behaviours[idx](resp))
        stop = asyncio.ensure_future(closing.wait())
        await asyncio.wait({body, stop}, return_when=asyncio.FIRST_COMPLETED)
        for t in (body, stop):
            t.cancel()
        await asyncio.gather(body, stop, return_exceptions=True)
        return resp

    app = aiohttp_web.Application()
    app.router.add_get("/events/stream", handler)
    runner = aiohttp_web.AppRunner(app)
    await runner.setup()
    site = aiohttp_web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}", count, closing


async def _run_until(behaviours, want: int, *, slow_first=0.0, guard=3.0):
    """Run SSEClient until ``want`` task events arrive (or ``guard`` seconds
    pass). Returns (entity ids received in order, connections opened)."""
    runner, base, count, closing = await _serve(behaviours)
    got: list[str] = []
    done = asyncio.Event()

    async def on_event(ev: NeotomaEvent):
        if not ev.entity_id:  # the server's ping heartbeat parses as an empty event
            return
        got.append(ev.entity_id)
        if len(got) == 1 and slow_first:
            await asyncio.sleep(slow_first)
        if len(got) >= want:
            done.set()

    client = sc.SSEClient(base_url=base, subscription_id="sub-test", bearer_token="t")
    task = asyncio.create_task(client.stream(on_event))
    try:
        await asyncio.wait_for(done.wait(), guard)
    except asyncio.TimeoutError:
        pass
    finally:
        client.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        closing.set()
        await runner.cleanup()
    return got, count["n"]


@pytest.fixture
def short_limits(monkeypatch):
    monkeypatch.setattr(sc, "SSE_READ_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(sc, "SSE_RECONNECT_DELAY_BASE", 0)


def test_normal_event_reaches_its_handler(short_limits):
    async def one_event(resp):
        await resp.write(_task_event("ent_a").encode())
        await asyncio.sleep(5)

    got, conns = asyncio.run(_run_until([one_event], want=1))
    assert got == ["ent_a"]
    assert conns == 1


def test_silent_stream_reconnects(short_limits):
    async def then_silent(resp):
        await resp.write(_task_event("ent_a").encode())
        await asyncio.sleep(5)  # no bytes, no heartbeat: a dead stream

    async def after_reconnect(resp):
        await resp.write(_task_event("ent_b").encode())
        await asyncio.sleep(5)

    got, conns = asyncio.run(_run_until([then_silent, after_reconnect], want=2))
    assert got == ["ent_a", "ent_b"]
    assert conns == 2


def test_heartbeats_keep_a_quiet_stream_open(short_limits):
    async def quiet_with_pings(resp):
        # 0.6s of nothing but heartbeats — three times the read limit —
        # then a real event on the same connection.
        for _ in range(12):
            await resp.write(_PING.encode())
            await asyncio.sleep(0.05)
        await resp.write(_task_event("ent_a").encode())
        await asyncio.sleep(5)

    got, conns = asyncio.run(_run_until([quiet_with_pings], want=1))
    assert got == ["ent_a"]
    assert conns == 1


def test_slow_handler_is_not_mistaken_for_a_silent_stream(short_limits):
    # The read limit is per socket read, so time spent inside a handler must
    # not trigger a reconnect: the next event is already buffered when it ends.
    async def gap_while_handler_runs(resp):
        await resp.write(_task_event("ent_a").encode())
        # Longer than the read limit with no bytes at all, but spent while the
        # handler is still busy with ent_a.
        await asyncio.sleep(0.3)
        await resp.write(_task_event("ent_b").encode())
        while True:
            await asyncio.sleep(0.05)
            await resp.write(_PING.encode())

    got, conns = asyncio.run(
        _run_until([gap_while_handler_runs], want=2, slow_first=0.4)
    )
    assert got == ["ent_a", "ent_b"]
    assert conns == 1
