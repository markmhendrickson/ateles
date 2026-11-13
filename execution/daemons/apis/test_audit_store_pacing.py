"""Apis audit stores when Neotoma is slow to accept writes.

Four behaviours, each with a test that fails without its change:

(a) every store that names no limit of its own gets 60s, not 30s;
(b) harness_event audit stores run one at a time, a minimum gap apart;
(c) a slow audit store never delays the dispatch or webhook it records;
(d) a timed-out audit store is retried once, with the same body and key.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

import audit_queue
import skill_runner
import swarm_dispatch
from swarm_dispatch import SwarmDispatcher
from test_swarm_dispatch import _StubNotifier, _config, _trigger


@pytest.fixture(autouse=True)
def _pacing_off_after_each_test():
    yield
    audit_queue.disable()


class _FakeClock:
    """A monotonic clock that only moves when a store or a pause moves it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.pauses: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


def _issue_trigger(number: int):
    return _trigger(
        kind="issue_opened",
        number=number,
        delivery_id=f"delivery-{number}",
        html_url=f"https://github.com/owner/repo/issues/{number}",
        head_sha="",
    )


# ── (a) default single-attempt limit ─────────────────────────────────────────


def test_default_store_limit_is_sixty_seconds(monkeypatch):
    limits: list[float] = []

    async def fake_once(self, entities, idempotency_key, time_limit, relationships=None):
        limits.append(time_limit)
        return {"entities": []}

    monkeypatch.setattr(SwarmDispatcher, "_store_entities_once", fake_once)
    dispatcher = SwarmDispatcher(_StubNotifier(), _config(neotoma_token="t"))

    asyncio.run(dispatcher._store_entities([{"entity_type": "task"}], "plain-store"))

    assert swarm_dispatch.NEOTOMA_STORE_ATTEMPT_TIMEOUT_SECONDS == 60.0
    assert limits == [60.0]


# ── (b) one at a time, a minimum gap apart ───────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_audit_stores_run_serially_with_the_minimum_gap(monkeypatch):
    clock = _FakeClock()
    queue = audit_queue.enable(
        audit_queue.PacedAuditQueue(clock=clock, sleep=clock.sleep)
    )
    spans: list[tuple[float, float]] = []
    in_flight = 0
    most_in_flight = 0

    async def fake_store(self, entities, idempotency_key, **kwargs):
        nonlocal in_flight, most_in_flight
        assert kwargs.get("retry_once") is True
        in_flight += 1
        most_in_flight = max(most_in_flight, in_flight)
        start = clock.now
        for _ in range(3):  # a 4s write, yielding so any overlap would show
            clock.now += 4.0 / 3
            await asyncio.sleep(0)
        in_flight -= 1
        spans.append((start, clock.now))
        return {"entities": []}

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    dispatcher = SwarmDispatcher(_StubNotifier(), _config(neotoma_token="t"))

    # Five webhook deliveries arriving together: the burst that was lost.
    await asyncio.gather(
        *(dispatcher._log_harness_event(_issue_trigger(n)) for n in range(1, 6))
    )
    assert spans == []  # nothing was written on the callers' time
    await queue.join()

    assert most_in_flight == 1
    assert len(spans) == 5
    gaps = [later[0] - earlier[1] for earlier, later in zip(spans, spans[1:])]
    assert all(gap >= audit_queue.MIN_GAP_SECONDS for gap in gaps), gaps
    assert clock.pauses == [audit_queue.MIN_GAP_SECONDS] * 4


@pytest.mark.asyncio
async def test_skill_runner_harness_events_use_the_same_queue(monkeypatch):
    clock = _FakeClock()
    queue = audit_queue.enable(
        audit_queue.PacedAuditQueue(clock=clock, sleep=clock.sleep)
    )
    written: list[dict] = []
    monkeypatch.setattr(
        skill_runner, "_write_harness_event", lambda **fields: written.append(fields)
    )

    await skill_runner._emit_harness_event(
        task_entity_id="ent_task", role="cicada", agent_sub="", event_type="subprocess",
        tool_name="claude:cicada", success="true",
    )
    await skill_runner._emit_harness_event(
        task_entity_id="ent_task", role="cicada", agent_sub="", event_type="subprocess",
        tool_name="claude:cicada", success="false",
    )
    assert written == []  # queued, not written on the dispatch's time
    await queue.join()

    assert [row["success"] for row in written] == ["true", "false"]
    assert clock.pauses == [audit_queue.MIN_GAP_SECONDS]


@pytest.mark.asyncio
async def test_a_full_queue_drops_the_row_with_a_warning(caplog):
    queue = audit_queue.PacedAuditQueue(max_pending=2)
    release = asyncio.Event()
    ran: list[str] = []

    def job(name):
        async def run():
            if name == "first":
                await release.wait()
            ran.append(name)
        return run

    assert queue.submit(job("first"), label="first")
    await asyncio.sleep(0)  # the worker takes "first" and waits on it
    with caplog.at_level(logging.WARNING, logger="apis.audit_queue"):
        accepted = [queue.submit(job(name), label=name) for name in ("b", "c", "d")]

    assert accepted == [True, True, False]
    assert queue.dropped == 1
    assert "dropped d" in caplog.text
    queue.min_gap_seconds = 0
    release.set()
    await queue.join()
    assert ran == ["first", "b", "c"]


def test_without_pacing_enabled_audit_stores_stay_inline(monkeypatch):
    """Short-lived processes (the dispatch_role CLI, evals) must not exit with
    rows still queued, so pacing is off unless the daemon turns it on."""
    stored: list[str] = []

    async def fake_store(self, entities, idempotency_key, **kwargs):
        stored.append(idempotency_key)

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    dispatcher = SwarmDispatcher(_StubNotifier(), _config(neotoma_token="t"))

    asyncio.run(dispatcher._log_harness_event(_issue_trigger(7)))

    assert audit_queue.active() is None
    assert len(stored) == 1 and stored[0].startswith("harness-event-")


# ── (c) a slow audit store does not delay the work it records ───────────────


@pytest.mark.asyncio
async def test_slow_audit_store_does_not_delay_webhook_dispatch(monkeypatch):
    queue = audit_queue.enable()
    never = asyncio.Event()
    audit_started = asyncio.Event()
    handled: list[int] = []

    async def slow_store(self, entities, idempotency_key, **kwargs):
        audit_started.set()
        await never.wait()  # Neotoma holding its write lock

    async def ready(self, repositories):
        return None

    async def gate_allows(self, trigger, **kwargs):
        return True

    async def handle_issue(self, trigger):
        handled.append(trigger.number)

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", slow_store)
    monkeypatch.setattr(SwarmDispatcher, "_await_github_app_ready", ready)
    monkeypatch.setattr(SwarmDispatcher, "_label_gate_allows", gate_allows)
    monkeypatch.setattr(SwarmDispatcher, "_handle_issue_opened", handle_issue)
    dispatcher = SwarmDispatcher(_StubNotifier(), _config(neotoma_token="t"))
    dispatcher.config.dry_run = False

    await asyncio.wait_for(dispatcher.handle_trigger(_issue_trigger(11)), timeout=2)

    assert handled == [11]
    await asyncio.wait_for(audit_started.wait(), timeout=2)
    assert queue._worker is not None and not queue._worker.done()
    queue._worker.cancel()


@pytest.mark.asyncio
async def test_slow_panel_audit_store_does_not_delay_the_panel(monkeypatch):
    queue = audit_queue.enable()
    never = asyncio.Event()
    keys: list[str] = []

    async def slow_store(self, entities, idempotency_key, **kwargs):
        keys.append(idempotency_key)
        await never.wait()

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", slow_store)
    dispatcher = SwarmDispatcher(_StubNotifier(), _config(neotoma_token="t"))

    # No verified head: no review rows are written, only the audit rows.
    confirmed = await asyncio.wait_for(
        dispatcher._persist_panel_reviews(
            _trigger(), [("pm", "Verdict: approve")], reviewed_head=""
        ),
        timeout=2,
    )

    assert confirmed is False
    await asyncio.sleep(0)
    assert len(keys) == 1 and keys[0].startswith("panel-reviews-")
    queue._worker.cancel()


# ── (d) a timeout is an unknown outcome: one same-key retry ─────────────────


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://neotoma.invalid/store")
    return httpx.HTTPStatusError(
        "status", request=request, response=httpx.Response(code, request=request)
    )


def _run_audit_store(monkeypatch, outcomes):
    calls: list[tuple[list[dict], str, float]] = []
    pauses: list[float] = []

    async def fake_once(self, entities, idempotency_key, time_limit, relationships=None):
        calls.append((entities, idempotency_key, time_limit))
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def no_sleep(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(SwarmDispatcher, "_store_entities_once", fake_once)
    monkeypatch.setattr(swarm_dispatch._neotoma_transient, "_asleep", no_sleep)
    dispatcher = SwarmDispatcher(_StubNotifier(), _config(neotoma_token="t"))
    asyncio.run(dispatcher._log_harness_event(_issue_trigger(21)))
    return calls, pauses


def test_timed_out_audit_store_is_retried_once_with_the_same_key(monkeypatch, caplog):
    with caplog.at_level(logging.INFO):
        calls, pauses = _run_audit_store(
            monkeypatch, [httpx.ReadTimeout(""), {"replayed": True, "entities": []}]
        )

    assert len(calls) == 2
    (first_body, first_key, _), (second_body, second_key, _) = calls
    assert first_key == second_key and first_key.startswith("harness-event-")
    assert second_body == first_body
    assert [limit for _, _, limit in calls] == [60.0, 60.0]
    assert pauses == [2.0]
    assert "idempotency replay" in caplog.text


def test_audit_store_retries_a_timeout_at_most_once(monkeypatch, caplog):
    with caplog.at_level(logging.ERROR):
        calls, pauses = _run_audit_store(
            monkeypatch,
            [httpx.ReadTimeout(""), httpx.ReadTimeout(""), {"entities": []}],
        )

    assert len(calls) == 2
    assert pauses == [2.0]
    assert "Neotoma store failed (harness-event-" in caplog.text


def test_audit_store_does_not_retry_a_rejection(monkeypatch):
    calls, pauses = _run_audit_store(monkeypatch, [_status_error(400), {}])

    assert len(calls) == 1
    assert pauses == []


@pytest.mark.parametrize("max_retries,expected_attempts", [(0, 1), (1, 2), (None, 3)])
def test_shared_retry_helper_honours_max_retries(monkeypatch, max_retries, expected_attempts):
    attempts: list[float] = []

    async def always_times_out(limit):
        attempts.append(limit)
        raise httpx.ReadTimeout("")

    async def no_sleep(seconds):
        return None

    monkeypatch.setattr(swarm_dispatch._neotoma_transient, "_asleep", no_sleep)
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(
            swarm_dispatch._neotoma_transient.acall_with_transient_retry(
                always_times_out,
                label="probe",
                attempt_timeout=60.0,
                total_seconds=1000.0,
                max_retries=max_retries,
            )
        )
    assert len(attempts) == expected_attempts
