"""A transient failure of a lens-signed gate write is retried, bounded and idempotent.

Observed on the canary markmhendrickson/ateles#1448: a signed `gate_status` write for
a gate that already read signed_off failed once with a transport error (the value was
169 bytes, so not a size problem), the dispatcher logged "gate may read cleared
without a verified sign-off", treated the gates as not green, and the build handoff
did not start; minutes earlier the same write had succeeded.

The writes carry an idempotency key, so repeating one is safe: if the first attempt
landed and only its response was lost, the repeat is answered as a REPLAY of the same
observation. These tests run the real sign-off path against a fake store that
models that (`_Record(idempotent=True, script=...)`), with the backoff and the clock
replaced so nothing waits.

Policy under test (`gate_waive.SIGNED_WRITE_*`): transport errors, helper timeouts,
and HTTP 5xx get 2 retries (1 s then 3 s); HTTP 401 gets one; every
other 4xx and every other reason code gets none; a wall-clock cap bounds the lot; the
final failure stays fail-closed.

Run: pytest execution/daemons/apis/test_signed_write_retry.py -v
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

import gate_waive
from gate_waive import GATE_STATUS_FIELD, IssueGateStore, SIGN_OFF_SIGNING_FAILED
from test_gate_sign_off_fail_closed import HEAD, _Record, _wire


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def waits(monkeypatch):
    """Record the backoff delays instead of sleeping; a fake clock the tests advance."""
    delays: list[float] = []
    clock = {"now": 1000.0}

    async def sleep(delay):
        delays.append(delay)
        clock["now"] += delay

    monkeypatch.setattr(gate_waive, "_retry_sleep", sleep, raising=False)
    monkeypatch.setattr(gate_waive, "_monotonic", lambda: clock["now"], raising=False)
    return delays, clock


def _sign(store, head=HEAD, gate="pm", lens="pavo"):
    return _run(store.sign_off("o/r", 795, gate, lens, head))


def _gate_keys(rec):
    return [k for f, k in rec.calls if f == GATE_STATUS_FIELD]


# ── the failure that blocked the canary ──────────────────────────────────────


def test_one_transport_error_then_success_is_verified_not_failed(monkeypatch, waits):
    """A gate that already reads signed_off, whose re-sign fails once in transit."""
    delays, _ = waits
    rec = _Record({"pm": "signed_off"}, idempotent=True, script={GATE_STATUS_FIELD: ["transport"]})
    store = _wire(monkeypatch, rec)

    outcome = _sign(store)

    assert outcome.ok and outcome.verified, outcome
    assert rec.gate_status["pm"] == "signed_off"
    assert delays == [1.0]
    keys = _gate_keys(rec)
    assert len(keys) == 2 and len(set(keys)) == 1, "the retry must reuse the idempotency key"


def test_two_failures_then_success_uses_both_retries_with_the_stated_backoff(monkeypatch, waits):
    delays, _ = waits
    rec = _Record({"pm": "pending"}, idempotent=True,
                  script={GATE_STATUS_FIELD: ["transport", "http:503"]})
    store = _wire(monkeypatch, rec)

    outcome = _sign(store)

    assert outcome.ok and outcome.verified
    assert delays == [1.0, 3.0]
    assert len(_gate_keys(rec)) == 3 and len(set(_gate_keys(rec))) == 1


@pytest.mark.parametrize("first", ["transport", "timeout", "http:500", "http:502", "http:503", "http:504"])
def test_each_transient_failure_is_retried(monkeypatch, waits, first):
    rec = _Record({"pm": "pending"}, idempotent=True, script={GATE_STATUS_FIELD: [first]})
    store = _wire(monkeypatch, rec)
    assert _sign(store).ok
    assert len(_gate_keys(rec)) == 2


# ── a write that landed but whose response was lost ──────────────────────────


def test_a_landed_gate_write_with_a_lost_response_is_replayed_not_doubled(monkeypatch, waits):
    rec = _Record({"pm": "pending"}, idempotent=True, script={GATE_STATUS_FIELD: ["lost"]})
    store = _wire(monkeypatch, rec)

    outcome = _sign(store)

    assert outcome.ok and outcome.verified
    assert rec.gate_writes()[0][0] and len(rec.gate_writes()) == 1, "the gate was applied twice"
    assert sum(1 for o in rec.observations if GATE_STATUS_FIELD in o["fields"]) == 1


def test_a_landed_history_write_with_a_lost_response_adds_no_second_entry(monkeypatch, waits):
    rec = _Record({"pm": "pending"}, idempotent=True, script={"owner_history": ["lost"]})
    store = _wire(monkeypatch, rec)

    outcome = _sign(store)

    assert outcome.ok and outcome.verified
    assert [e["action"] for e in rec.owner_history] == ["signed_off"], rec.owner_history
    assert sum(1 for f, _, _ in rec.writes if f == "owner_history") == 1


def test_a_landed_failure_note_with_a_lost_response_adds_no_second_note(monkeypatch, waits):
    """The gate write is refused for good, so the failure note is written; its first
    attempt lands and loses the response; the retry must not add another note."""
    rec = _Record(
        {"pm": "pending"},
        idempotent=True,
        fail_fields=frozenset({GATE_STATUS_FIELD}),
        script={"owner_history": ["ok", "lost"]},
    )
    store = _wire(monkeypatch, rec)

    outcome = _sign(store)

    assert not outcome.ok
    notes = [e for e in rec.owner_history if e["action"] == "sign_off_failed"]
    assert len(notes) == 1, notes


# ── persistent failure: bounded, and fail-closed exactly as before ───────────


def test_a_persistent_transport_failure_stays_fail_closed_in_bounded_time(monkeypatch, waits):
    delays, clock = waits
    rec = _Record({"pm": "pending"}, idempotent=True,
                  script={GATE_STATUS_FIELD: ["transport"] * 10})
    store = _wire(monkeypatch, rec)
    start = clock["now"]

    outcome = _sign(store)

    assert not outcome.ok
    assert outcome.error, "a persistent failure must carry an error"
    assert rec.gate_status["pm"] == "pending", "the gate must not read cleared"
    assert len(_gate_keys(rec)) == 3, "exactly two retries"
    assert delays == [1.0, 3.0]
    assert clock["now"] - start <= 45.0


# ── what is NOT retried ──────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [400, 403, 404, 409, 413, 422])
def test_another_4xx_is_never_retried(monkeypatch, waits, status):
    delays, _ = waits
    rec = _Record({"pm": "pending"}, idempotent=True,
                  script={GATE_STATUS_FIELD: [f"http:{status}"] * 5})
    store = _wire(monkeypatch, rec)

    outcome = _sign(store)

    assert not outcome.ok
    assert len(_gate_keys(rec)) == 1 and delays == []


def test_a_401_is_retried_exactly_once(monkeypatch, waits):
    delays, _ = waits
    rec = _Record({"pm": "pending"}, idempotent=True, script={GATE_STATUS_FIELD: ["http:401"] * 5})
    store = _wire(monkeypatch, rec)

    assert not _sign(store).ok
    assert len(_gate_keys(rec)) == 2 and delays == [1.0]


def test_a_401_that_clears_on_the_retry_succeeds(monkeypatch, waits):
    rec = _Record({"pm": "pending"}, idempotent=True, script={GATE_STATUS_FIELD: ["http:401"]})
    store = _wire(monkeypatch, rec)
    assert _sign(store).ok


@pytest.mark.parametrize("code_exc", ["no_key", "helper_error", "helper_non_json", "unexpected"])
def test_other_reason_codes_are_not_retried(monkeypatch, waits, code_exc):
    from lib.daemon_runtime.neotoma_signed import SignedRequestError

    delays, _ = waits
    calls = []

    def boom(*a, **k):
        calls.append(1)
        if code_exc == "unexpected":
            raise ValueError("odd")
        raise SignedRequestError("detail", code_exc)

    rec = _Record({"pm": "pending"})
    store = _wire(monkeypatch, rec)
    monkeypatch.setattr("gate_waive._ns.signed_request", boom)

    assert not _sign(store).ok
    assert delays == [], "a non-transient failure must not wait and retry"
    # The first write fails for good; the sign-off then tries its failure note and
    # the restore of the prior value, each once. Never three attempts of any one.
    assert len(calls) <= 3, calls


# ── every attempt is logged with its number and reason code, and nothing else ─


def test_each_attempt_is_logged_with_number_and_code_and_no_text(monkeypatch, waits, caplog):
    rec = _Record({"pm": "pending"}, idempotent=True,
                  script={GATE_STATUS_FIELD: ["transport", "transport", "transport", "transport"]})
    store = _wire(monkeypatch, rec)

    with caplog.at_level(logging.INFO, logger="apis.gate_waive"):
        _sign(store)

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "attempt 1 failed" in text and "attempt 2 failed" in text
    assert "attempt=3/3" in text, text
    assert text.count("reason=transport_error") >= 3
    assert "retrying in 1.0s" in text and "retrying in 3.0s" in text
    for forbidden in ("fetch failed", "Bearer", ".jwk.json", "idempotency_key="):
        assert forbidden not in text


def test_a_success_after_a_retry_is_logged(monkeypatch, waits, caplog):
    rec = _Record({"pm": "pending"}, idempotent=True, script={GATE_STATUS_FIELD: ["transport"]})
    store = _wire(monkeypatch, rec)
    with caplog.at_level(logging.INFO, logger="apis.gate_waive"):
        _sign(store)
    assert "succeeded on attempt 2" in "\n".join(r.getMessage() for r in caplog.records)


# ── planted red ──────────────────────────────────────────────────────────────


def test_planted_red_without_the_retry_one_transient_failure_blocks_the_gate(monkeypatch, waits):
    monkeypatch.setattr(gate_waive, "retries_for_reason", lambda code: 0)
    monkeypatch.setattr(gate_waive, "retries_for_status", lambda status: 0)
    rec = _Record({"pm": "signed_off"}, idempotent=True, script={GATE_STATUS_FIELD: ["transport"]})
    store = _wire(monkeypatch, rec)

    outcome = _sign(store)

    assert not outcome.ok, "the planted no-retry behaviour should fail the sign-off"
    assert len(_gate_keys(rec)) == 1


def test_planted_red_retrying_a_4xx_would_repeat_a_refused_write(monkeypatch, waits):
    monkeypatch.setattr(gate_waive, "retries_for_status", lambda status: 2)
    rec = _Record({"pm": "pending"}, idempotent=True, script={GATE_STATUS_FIELD: ["http:403"] * 5})
    store = _wire(monkeypatch, rec)

    _sign(store)

    assert len(_gate_keys(rec)) == 3, "with the planted rule a 403 is repeated"


# ── the waive path's bearer writes get the same policy ───────────────────────


class _Resp:
    def __init__(self, status=200, body=b"{}"):
        self.status_code = status
        self.content = body
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("x", request=httpx.Request("POST", "http://x"), response=self)

    def json(self):
        return {}


class _Client:
    script: list = []
    calls: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        _Client.calls.append(json)
        outcome = _Client.script.pop(0) if _Client.script else "ok"
        if outcome == "timeout":
            raise httpx.ReadTimeout("timed out")
        if outcome == "connect":
            raise httpx.ConnectError("refused")
        if outcome.startswith("http:"):
            return _Resp(int(outcome[5:]))
        return _Resp()


@pytest.fixture
def client(monkeypatch):
    _Client.script, _Client.calls = [], []
    monkeypatch.setattr(gate_waive.httpx, "AsyncClient", _Client)
    return _Client


def test_a_waive_write_is_retried_with_the_same_payload(monkeypatch, waits, client):
    delays, _ = waits
    client.script = ["timeout", "http:503"]
    store = IssueGateStore("http://x", "tok")
    payload = {"entity_id": "e", "field": GATE_STATUS_FIELD, "value": "{}", "idempotency_key": "k"}

    result = _run(store._post("correct", payload))

    assert result == {}
    assert client.calls == [payload] * 3
    assert delays == [1.0, 3.0]


def test_a_waive_write_that_keeps_failing_returns_none_in_bounded_time(monkeypatch, waits, client):
    client.script = ["connect"] * 10
    store = IssueGateStore("http://x", "tok")
    assert _run(store._post("correct", {"k": 1})) is None
    assert len(client.calls) == 3


@pytest.mark.parametrize("status", [400, 403, 404, 409, 413])
def test_a_waive_4xx_is_not_retried(monkeypatch, waits, client, status):
    client.script = [f"http:{status}"] * 5
    store = IssueGateStore("http://x", "tok")
    assert _run(store._post("correct", {"k": 1})) is None
    assert len(client.calls) == 1


def test_a_waive_401_is_retried_once(monkeypatch, waits, client):
    client.script = ["http:401"] * 5
    store = IssueGateStore("http://x", "tok")
    assert _run(store._post("correct", {"k": 1})) is None
    assert len(client.calls) == 2


def test_a_waive_write_that_succeeds_first_time_is_not_repeated(monkeypatch, waits, client):
    store = IssueGateStore("http://x", "tok")
    assert _run(store._post("correct", {"k": 1})) == {}
    assert len(client.calls) == 1


# ── the cap is a real deadline: attempts are bounded, not only the waits ─────
#
# The clock below advances on REQUEST DURATION as well as on sleep. A request is
# given the timeout the code passes it; the fake runs for the pattern's duration
# or for that timeout, whichever is shorter, then fails the way a timed-out
# request fails. A cap that counted only waits would pass a test whose clock
# moves only on sleep; this one does not.

import itertools

from lib.daemon_runtime.neotoma_signed import SignedRequestError  # noqa: E402

CAP = 45.0
HANG = 10_000.0  # "never answers": runs until its timeout


class _Clock:
    def __init__(self):
        self.now = 1000.0
        self.started = self.now
        self.launched: list[tuple[float, float, float]] = []  # (elapsed, timeout, left)

    @property
    def elapsed(self):
        return self.now - self.started


def _virtual(monkeypatch, clock):
    async def sleep(delay):
        clock.now += delay

    monkeypatch.setattr(gate_waive, "_retry_sleep", sleep, raising=False)
    monkeypatch.setattr(gate_waive, "_monotonic", lambda: clock.now, raising=False)


def _signed_fake(clock, durations):
    """Each call takes durations[i] (the last repeats) or its timeout, then fails."""
    calls = {"n": 0}

    def signed(method, url, body=None, agent_name="", timeout=20, *, sub=None):
        i = min(calls["n"], len(durations) - 1)
        calls["n"] += 1
        clock.launched.append((clock.elapsed, timeout, CAP - clock.elapsed))
        took = min(durations[i], timeout)
        clock.now += took
        raise SignedRequestError("fetch failed", "transport_error")

    return signed, calls


def _write(store):
    return _run(
        store._sign_off_write(
            "ent_1", GATE_STATUS_FIELD, "v", "key-1",
            lens_agent="pavo", lens_sub="pavo@ateles-swarm", context=("o/r", 1, "pm"),
        )
    )


PATTERNS = {
    "first hangs": [HANG, 0.1],
    "second hangs": [0.1, HANG, 0.1],
    "all hang": [HANG],
    "slow then hang": [30.0, HANG],
    "long first leaves little": [43.0, HANG],
    "just fits one retry": [41.0, 0.1],
    "fast failures": [0.1],
}


def _store():
    return IssueGateStore("http://x", "tok")


@pytest.mark.parametrize("name", sorted(PATTERNS))
def test_a_signed_write_never_outlasts_the_cap(monkeypatch, name):
    clock = _Clock()
    _virtual(monkeypatch, clock)
    signed, _ = _signed_fake(clock, PATTERNS[name])
    monkeypatch.setattr(gate_waive._ns, "signed_request", signed)

    assert _write(_store()) == SIGN_OFF_SIGNING_FAILED

    assert clock.elapsed <= CAP, f"{name}: {clock.elapsed}s of waits plus attempts"
    for elapsed, timeout, left in clock.launched:
        assert timeout <= left + 1e-9, "an attempt was launched with more time than was left"
        assert timeout >= gate_waive.MIN_ATTEMPT_TIMEOUT_S - 1e-9 or elapsed == 0


def test_every_pair_of_attempt_durations_stays_inside_the_cap(monkeypatch):
    for d1, d2, d3 in itertools.product([0.1, 5.0, 19.0, 30.0, 43.0, HANG], repeat=3):
        clock = _Clock()
        _virtual(monkeypatch, clock)
        signed, _ = _signed_fake(clock, [d1, d2, d3])
        monkeypatch.setattr(gate_waive._ns, "signed_request", signed)
        _write(_store())
        assert clock.elapsed <= CAP, (d1, d2, d3, clock.elapsed)
        assert all(t <= left + 1e-9 for _, t, left in clock.launched), (d1, d2, d3)


def test_all_hang_makes_two_attempts_and_the_true_worst_case_is_41_seconds(monkeypatch):
    clock = _Clock()
    _virtual(monkeypatch, clock)
    signed, calls = _signed_fake(clock, [HANG])
    monkeypatch.setattr(gate_waive._ns, "signed_request", signed)

    _write(_store())

    assert calls["n"] == 2, "a third attempt needs 41 + 3 + 2 s, which does not fit in 45"
    assert clock.elapsed == pytest.approx(41.0)
    assert [t for _, t, _ in clock.launched] == [20.0, 20.0]


def test_a_retry_that_cannot_fit_is_not_started(monkeypatch):
    """The first attempt ends at 43 s: 43 + 1 s backoff + 2 s minimum is over 45."""
    clock = _Clock()
    _virtual(monkeypatch, clock)
    signed, calls = _signed_fake(clock, [43.0])
    # an attempt can't really take 43 s with a 20 s timeout; model a slow helper
    # whose timeout the code did not (yet) shorten by lifting the default
    monkeypatch.setattr(gate_waive, "SIGNED_ATTEMPT_TIMEOUT_S", 44.0)
    monkeypatch.setattr(gate_waive.IssueGateStore, "_signed_correct", _passthrough(signed))

    _write(_store())

    assert calls["n"] == 1
    assert clock.elapsed == pytest.approx(43.0)


def _passthrough(signed):
    async def _signed_correct(self, entity_id, field_name, value, key, *, lens_agent, lens_sub,
                              deadline=None):
        timeout = min(gate_waive.SIGNED_ATTEMPT_TIMEOUT_S, deadline - gate_waive._monotonic())
        return signed("POST", "u", {}, agent_name=lens_agent, timeout=timeout, sub=lens_sub)

    return _signed_correct


def test_a_retry_that_just_fits_is_started_with_the_time_that_is_left(monkeypatch):
    clock = _Clock()
    _virtual(monkeypatch, clock)
    signed, calls = _signed_fake(clock, [20.0, 20.0])  # ends at 20; retry at 21 gets 20
    monkeypatch.setattr(gate_waive._ns, "signed_request", signed)

    _write(_store())

    # attempt 1: 0-20, wait 1, attempt 2: 21-41; a third would need 41+3+2 > 45
    assert calls["n"] == 2


def test_the_timeout_passed_shrinks_to_the_time_left(monkeypatch):
    clock = _Clock()
    _virtual(monkeypatch, clock)
    monkeypatch.setattr(gate_waive, "SIGNED_WRITE_TOTAL_CAP_S", 30.0)
    signed, calls = _signed_fake(clock, [HANG])
    monkeypatch.setattr(gate_waive._ns, "signed_request", signed)

    _write(_store())

    # 20 s, wait 1, then only 9 s left: the retry is launched with 9, not 20
    assert [t for _, t, _ in clock.launched] == [20.0, 9.0]
    assert clock.elapsed == pytest.approx(30.0)


def test_the_failure_note_and_restore_writes_have_their_own_deadline(monkeypatch):
    """Both go through the same loop: each is bounded by its own cap."""
    clock = _Clock()
    _virtual(monkeypatch, clock)
    signed, _ = _signed_fake(clock, [HANG])
    monkeypatch.setattr(gate_waive._ns, "signed_request", signed)
    store = _store()

    for field in ("owner_history", GATE_STATUS_FIELD):
        clock.started = clock.now
        clock.launched.clear()
        _run(store._sign_off_write(
            "ent_1", field, "v", f"key-{field}",
            lens_agent="pavo", lens_sub="pavo@ateles-swarm", context=("o/r", 1, "pm"),
        ))
        assert clock.elapsed <= CAP


def test_the_logs_state_no_cap_that_is_not_kept(monkeypatch, caplog):
    clock = _Clock()
    _virtual(monkeypatch, clock)
    signed, _ = _signed_fake(clock, [HANG])
    monkeypatch.setattr(gate_waive._ns, "signed_request", signed)

    with caplog.at_level(logging.INFO, logger="apis.gate_waive"):
        _write(_store())

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "deadline reached, no further attempt fits" in text
    assert "time cap reached" not in text


# ── the bearer path ──────────────────────────────────────────────────────────


class _TimedClient:
    """AsyncClient fake on the virtual clock: records each attempt's timeout."""

    clock: _Clock
    durations: list = []
    timeouts: list = []

    def __init__(self, *a, timeout=None, **k):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        i = min(len(_TimedClient.timeouts), len(_TimedClient.durations) - 1)
        d = _TimedClient.durations[i]
        _TimedClient.timeouts.append(self.timeout)
        _TimedClient.clock.launched.append(
            (_TimedClient.clock.elapsed, self.timeout, CAP - _TimedClient.clock.elapsed)
        )
        _TimedClient.clock.now += min(d, self.timeout)
        raise httpx.ReadTimeout("timed out")


@pytest.mark.parametrize("name", sorted(PATTERNS))
def test_a_waive_write_never_outlasts_the_cap(monkeypatch, name):
    clock = _Clock()
    _virtual(monkeypatch, clock)
    _TimedClient.clock, _TimedClient.durations, _TimedClient.timeouts = clock, PATTERNS[name], []
    monkeypatch.setattr(gate_waive.httpx, "AsyncClient", _TimedClient)

    assert _run(IssueGateStore("http://x", "tok")._post("correct", {"k": 1})) is None

    assert clock.elapsed <= CAP, f"{name}: {clock.elapsed}s"
    assert all(t <= left + 1e-9 for _, t, left in clock.launched)


def test_a_waive_write_that_hangs_for_real_is_cut_at_its_attempt_timeout(monkeypatch):
    """httpx's timeout is per operation; the whole request must be bounded too."""
    import time as _time

    monkeypatch.setattr(gate_waive, "BEARER_ATTEMPT_TIMEOUT_S", 0.2)
    monkeypatch.setattr(gate_waive, "SIGNED_WRITE_TOTAL_CAP_S", 1.0)
    monkeypatch.setattr(gate_waive, "MIN_ATTEMPT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(gate_waive, "SIGNED_WRITE_BACKOFFS_S", (0.05, 0.05))
    monkeypatch.setattr(gate_waive, "_retry_sleep", asyncio.sleep, raising=False)
    monkeypatch.setattr(gate_waive, "_monotonic", _time.monotonic, raising=False)

    class _Hangs(_TimedClient):
        async def post(self, url, json=None, headers=None):
            await asyncio.sleep(30)

    monkeypatch.setattr(gate_waive.httpx, "AsyncClient", _Hangs)
    t0 = _time.monotonic()

    assert _run(IssueGateStore("http://x", "tok")._post("correct", {"k": 1})) is None

    assert _time.monotonic() - t0 < 1.5


# ── planted red: the old "elapsed + next wait" check ─────────────────────────


def _old_rule(monkeypatch):
    """Reinstate the previous check: it counted elapsed + the next wait only, and
    launched every attempt with its full default timeout."""
    monkeypatch.setattr(
        gate_waive, "_retry_fits",
        lambda started, done: (gate_waive._monotonic() - started + gate_waive._next_backoff(done)
                               <= gate_waive.SIGNED_WRITE_TOTAL_CAP_S),
        raising=False,
    )

    async def old_signed_correct(self, entity_id, field_name, value, key, *, lens_agent,
                                 lens_sub, deadline=None):
        return gate_waive._ns.signed_request(
            "POST", "u", {}, agent_name=lens_agent,
            timeout=gate_waive.SIGNED_ATTEMPT_TIMEOUT_S, sub=lens_sub)

    async def old_post_once(self, path, payload, deadline):
        async with httpx.AsyncClient(timeout=gate_waive.BEARER_ATTEMPT_TIMEOUT_S) as client:
            return await client.post(f"{self.base_url}/{path}", json=payload, headers={})

    async def unbounded(awaitable, timeout=None):
        return await awaitable

    monkeypatch.setattr(IssueGateStore, "_signed_correct", old_signed_correct)
    monkeypatch.setattr(IssueGateStore, "_post_once", old_post_once)
    monkeypatch.setattr(asyncio, "wait_for", unbounded)


def test_planted_red_the_old_check_lets_a_signed_write_run_past_the_cap(monkeypatch):
    _old_rule(monkeypatch)
    clock = _Clock()
    _virtual(monkeypatch, clock)
    signed, _ = _signed_fake(clock, [HANG])
    monkeypatch.setattr(gate_waive._ns, "signed_request", signed)

    _write(_store())

    assert clock.elapsed > CAP, "the old rule is the defect: it overran the cap"


def test_planted_red_the_old_check_lets_a_waive_write_run_past_the_cap(monkeypatch):
    _old_rule(monkeypatch)
    clock = _Clock()
    _virtual(monkeypatch, clock)
    _TimedClient.clock, _TimedClient.durations, _TimedClient.timeouts = clock, [HANG], []
    monkeypatch.setattr(gate_waive.httpx, "AsyncClient", _TimedClient)

    _run(IssueGateStore("http://x", "tok")._post("correct", {"k": 1}))

    assert clock.elapsed > CAP


# ── the deadline holds through executor queueing and mid-flight abandonment ──
#
# The per-attempt timeout bounds the helper process only once a worker THREAD runs
# it. A busy shared executor can delay that start past the deadline; the attempt
# must then issue no write at all. These tests use the real clock and a real,
# saturated executor, with the deadline scaled down so they take a fraction of a
# second.

import concurrent.futures
import threading
import time as _real_time


def _tiny_real_deadline(monkeypatch):
    monkeypatch.setattr(gate_waive, "SIGNED_WRITE_TOTAL_CAP_S", 0.05)
    monkeypatch.setattr(gate_waive, "MIN_ATTEMPT_TIMEOUT_S", 0.01)
    monkeypatch.setattr(gate_waive, "SIGNED_ATTEMPT_TIMEOUT_S", 0.02)
    monkeypatch.setattr(gate_waive, "SIGNED_WRITE_BACKOFFS_S", (0.01, 0.01))
    monkeypatch.setattr(gate_waive, "_monotonic", _real_time.monotonic, raising=False)
    monkeypatch.setattr(gate_waive, "_retry_sleep", asyncio.sleep, raising=False)


def _counting_fake(monkeypatch, *, block=0.0):
    calls = []

    def signed(method, url, body=None, agent_name="", timeout=20, *, sub=None):
        calls.append(timeout)
        if block:
            _real_time.sleep(block)
        return 200, {}

    monkeypatch.setattr(gate_waive._ns, "signed_request", signed)
    return calls


async def _with_executor(workers, occupy_for=0.0):
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    asyncio.get_running_loop().set_default_executor(ex)
    if occupy_for:
        for _ in range(workers):
            ex.submit(_real_time.sleep, occupy_for)
    return ex


def test_a_queued_attempt_that_starts_after_the_deadline_issues_no_write(monkeypatch):
    """Arch's reproduction: the sole worker is busy for 0.15 s, the deadline is 0.05 s,
    and the transport would answer 200 at once. No write may be issued, and the
    caller is not held past the deadline."""
    _tiny_real_deadline(monkeypatch)
    calls = _counting_fake(monkeypatch)
    store = _store()

    async def go():
        await _with_executor(1, occupy_for=0.15)
        t0 = _real_time.monotonic()
        out = await store._sign_off_write(
            "ent_1", GATE_STATUS_FIELD, "v", "key-1",
            lens_agent="pavo", lens_sub="pavo@ateles-swarm", context=("o/r", 1, "pm"),
        )
        held = _real_time.monotonic() - t0
        await asyncio.sleep(0.3)  # the queued worker now gets its turn
        return out, held

    out, held = _run(go())

    assert out == SIGN_OFF_SIGNING_FAILED
    assert held < 0.12, f"the caller was held {held}s past a 0.05 s deadline"
    assert calls == [], "an expired queued attempt issued a write"


def test_the_expired_outcome_is_the_fail_closed_reason_and_is_not_retried(monkeypatch, caplog):
    _tiny_real_deadline(monkeypatch)
    _counting_fake(monkeypatch)

    async def go():
        await _with_executor(1, occupy_for=0.15)
        with caplog.at_level(logging.INFO, logger="apis.gate_waive"):
            return await _store()._sign_off_write(
                "ent_1", GATE_STATUS_FIELD, "v", "key-1",
                lens_agent="pavo", lens_sub="pavo@ateles-swarm", context=("o/r", 1, "pm"),
            )

    assert _run(go()) == SIGN_OFF_SIGNING_FAILED
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "reason=expired" in text and "attempt=1/1" in text, text


def test_a_worker_that_is_mid_flight_when_the_await_gives_up_has_its_result_discarded(monkeypatch):
    _tiny_real_deadline(monkeypatch)
    calls = _counting_fake(monkeypatch, block=0.15)  # a stuck helper that ignores its timeout

    async def go():
        await _with_executor(2)
        out = await _store()._sign_off_write(
            "ent_1", GATE_STATUS_FIELD, "v", "key-1",
            lens_agent="pavo", lens_sub="pavo@ateles-swarm", context=("o/r", 1, "pm"),
        )
        await asyncio.sleep(0.3)  # the worker finishes and tries to report 200
        return out

    assert _run(go()) == SIGN_OFF_SIGNING_FAILED, "a late 200 was treated as success"
    assert len(calls) == 1


def test_repeated_expirations_do_not_grow_the_worker_pool(monkeypatch):
    """50 attempts that each outlast the deadline leave no more live threads than the
    executor's fixed size, and every one of them is reclaimed once it finishes."""
    _tiny_real_deadline(monkeypatch)
    _counting_fake(monkeypatch, block=0.06)
    store = _store()
    before = threading.active_count()

    async def go():
        ex = await _with_executor(3)
        for i in range(50):
            await store._sign_off_write(
                "ent_1", GATE_STATUS_FIELD, "v", f"key-{i}",
                lens_agent="pavo", lens_sub="pavo@ateles-swarm", context=("o/r", 1, "pm"),
            )
        peak = threading.active_count()
        await asyncio.sleep(0.5)
        settled = threading.active_count()
        ex.shutdown(wait=True)
        return peak, settled

    peak, settled = _run(go())
    assert peak <= before + 3, (before, peak)
    assert settled <= before + 3
    assert threading.active_count() <= before + 1, "worker threads were not reclaimed"


def test_a_write_that_landed_after_the_caller_gave_up_is_not_applied_twice(monkeypatch):
    """Landed-but-lost under expiry: the gate write reaches the store, then the helper
    stalls past the deadline. The caller fails closed; nothing is written a second time."""
    _tiny_real_deadline(monkeypatch)
    rec = _Record({"pm": "pending"}, idempotent=True)
    store = _wire(monkeypatch, rec)
    landed = gate_waive._ns.signed_request

    async def lands_then_stalls(method, url, body=None, agent_name="", timeout=20, *, sub=None):
        out = await landed(method, url, body, agent_name, timeout, sub=sub)
        if body and body.get("field") == GATE_STATUS_FIELD:
            await asyncio.sleep(0.15)  # the response is still in flight at the deadline
        return out

    monkeypatch.setattr(gate_waive._ns, "signed_request", lands_then_stalls)

    async def go():
        await _with_executor(4)
        outcome = await store.sign_off("o/r", 795, "pm", "pavo", HEAD)
        await asyncio.sleep(0.4)
        return outcome

    outcome = _run(go())

    assert not outcome.ok, "a response that missed the deadline must not count as success"
    signed_off = [w for w in rec.gate_writes() if w[0].get("pm") == "signed_off"]
    assert len(signed_off) == 1, "the stalled write must not be repeated into a second apply"


def test_a_waive_write_whose_deadline_passed_before_it_began_sends_nothing(monkeypatch):
    _tiny_real_deadline(monkeypatch)
    sent = []

    class _Client(_TimedClient):
        async def post(self, url, json=None, headers=None):
            sent.append(json)
            return _Resp()

    monkeypatch.setattr(gate_waive.httpx, "AsyncClient", _Client)
    store = IssueGateStore("http://x", "tok")

    async def go():
        # the deadline has passed by the time the request would start
        return await store._post_once("correct", {"k": 1}, gate_waive._monotonic() - 1.0)

    with pytest.raises(asyncio.TimeoutError):
        _run(go())
    assert sent == []


def test_planted_red_computing_the_timeout_before_the_thread_only_issues_the_late_write(monkeypatch):
    """The previous design: compute the helper timeout, hand the call to the executor
    and await it with no deadline of its own."""
    _tiny_real_deadline(monkeypatch)
    calls = _counting_fake(monkeypatch)

    async def old(self, entity_id, field_name, value, key, *, lens_agent, lens_sub, deadline=None):
        timeout = min(gate_waive.SIGNED_ATTEMPT_TIMEOUT_S, deadline - gate_waive._monotonic())
        return await asyncio.to_thread(
            gate_waive._ns.signed_request, "POST", "u", {}, agent_name=lens_agent,
            timeout=timeout, sub=lens_sub)

    monkeypatch.setattr(IssueGateStore, "_signed_correct", old)

    async def go():
        await _with_executor(1, occupy_for=0.15)
        return await _store()._sign_off_write(
            "ent_1", GATE_STATUS_FIELD, "v", "key-1",
            lens_agent="pavo", lens_sub="pavo@ateles-swarm", context=("o/r", 1, "pm"),
        )

    out = _run(go())
    assert out == "", "the old design returned SUCCESS after the deadline, having issued the write"
    assert len(calls) == 1
