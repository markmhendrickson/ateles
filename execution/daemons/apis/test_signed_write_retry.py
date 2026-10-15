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


def test_the_total_time_cap_stops_further_attempts(monkeypatch):
    clock = {"now": 0.0}

    async def sleep(delay):
        clock["now"] += delay

    monkeypatch.setattr(gate_waive, "_retry_sleep", sleep, raising=False)
    monkeypatch.setattr(gate_waive, "_monotonic", lambda: clock["now"], raising=False)
    monkeypatch.setattr(gate_waive, "SIGNED_WRITE_TOTAL_CAP_S", 2.0, raising=False)
    rec = _Record({"pm": "pending"}, idempotent=True, script={GATE_STATUS_FIELD: ["transport"] * 10})
    store = _wire(monkeypatch, rec)

    outcome = _sign(store)

    assert not outcome.ok
    assert len(_gate_keys(rec)) == 2, "the cap (2 s) allowed one 1 s retry and no more"


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
