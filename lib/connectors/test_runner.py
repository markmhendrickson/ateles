"""Tests for the connector runner.

Three properties carry the weight, each mapped to a real incident:

  - isolation      one source's outage must not stop the others;
  - write budget   the 2026 runaway wrote 520+ records reporting success;
  - status         last_success_at must survive a failure, or the one fact an
                   operator needs ("last worked 3 days ago") is destroyed.
"""

from __future__ import annotations

from lib.connectors.base import ConnectorResult, ConnectorStatus
from lib.connectors.runner import run_all, run_connector


class FakeStore:
    """In-memory stand-in for ConnectorStore. No network."""

    def __init__(self, prior: "dict[str, ConnectorStatus] | None" = None) -> None:
        self.statuses: dict[str, ConnectorStatus] = dict(prior or {})
        self.writes: list[ConnectorStatus] = []
        self.raise_on_write = False

    def read_status(self, name: str) -> ConnectorStatus | None:
        return self.statuses.get(name)

    def write_status(self, status: ConnectorStatus) -> None:
        if self.raise_on_write:
            raise RuntimeError("neotoma down")
        self.writes.append(status)
        self.statuses[status.connector_name] = status


class StubConnector:
    def __init__(self, name="stub", result=None, exc=None, interval=900):
        self.name = name
        self.poll_interval_seconds = interval
        self._result = result or ConnectorResult.success(records_written=3)
        self._exc = exc
        self.calls = 0

    def observe(self) -> ConnectorResult:
        self.calls += 1
        if self._exc:
            raise self._exc
        return self._result


# ── the happy path ──────────────────────────────────────────────────────────


def test_success_records_ok_status_and_both_timestamps():
    store = FakeStore()
    result = run_connector(StubConnector(), store)

    assert result.ok
    written = store.writes[-1]
    assert written.status == "ok"
    assert written.last_attempt_at is not None
    # On success the two timestamps coincide — the run both happened and worked.
    assert written.last_success_at == written.last_attempt_at
    assert written.consecutive_failures == 0
    assert written.stale_after_seconds == 2700  # 3 x 900


def test_skipped_result_does_not_advance_success_or_failures():
    """Unbound skip is idle, not a verify success and not a failure streak."""
    prior = ConnectorStatus(
        connector_name="stub",
        status="ok",
        last_success_at="2026-08-29T09:00:00+00:00",
        consecutive_failures=2,
        records_written=7,
    )
    store = FakeStore({"stub": prior})
    result = run_connector(
        StubConnector(
            result=ConnectorResult.skipped("set FLY_APP or DEPLOYMENT_CONFIGURATION_ID")
        ),
        store,
    )
    assert result.ok and result.detail["skipped"] is True
    written = store.writes[-1]
    assert written.consecutive_failures == 2
    assert written.last_success_at == "2026-08-29T09:00:00+00:00"
    assert written.status == "ok"
    assert written.last_attempt_at is not None
    assert written.last_attempt_at != written.last_success_at


def test_skipped_never_run_stays_never_run():
    store = FakeStore()
    run_connector(StubConnector(result=ConnectorResult.skipped("unbound")), store)
    written = store.writes[-1]
    assert written.status == "never_run"
    assert written.last_success_at is None
    assert written.consecutive_failures == 0


# ── failure handling ────────────────────────────────────────────────────────


def test_failure_preserves_the_prior_last_success_at():
    """The whole point of the field: 'last worked 3 days ago' must survive."""
    prior = ConnectorStatus(
        connector_name="stub",
        status="ok",
        last_success_at="2026-08-29T09:00:00+00:00",
        records_written=7,
    )
    store = FakeStore({"stub": prior})

    run_connector(StubConnector(result=ConnectorResult.failure("upstream 502")), store)

    written = store.writes[-1]
    assert written.status == "failing"
    assert written.last_success_at == "2026-08-29T09:00:00+00:00"
    assert written.last_attempt_at != written.last_success_at
    assert written.last_error == "upstream 502"


def test_consecutive_failures_accumulate_then_reset():
    store = FakeStore()
    failing = StubConnector(result=ConnectorResult.failure("nope"))

    run_connector(failing, store)
    assert store.writes[-1].consecutive_failures == 1
    run_connector(failing, store)
    assert store.writes[-1].consecutive_failures == 2

    run_connector(StubConnector(), store)  # a success
    assert store.writes[-1].consecutive_failures == 0


def test_observe_raising_is_caught_not_propagated():
    """The contract says observe() must not raise; the runner cannot trust it."""
    store = FakeStore()
    result = run_connector(StubConnector(exc=ValueError("kaboom")), store)

    assert not result.ok
    assert "ValueError" in result.error and "kaboom" in result.error
    assert store.writes[-1].status == "failing"


def test_non_result_return_is_treated_as_failure():
    class Broken:
        name = "broken"
        poll_interval_seconds = 900

        def observe(self):
            return "not a ConnectorResult"

    result = run_connector(Broken(), FakeStore())
    assert not result.ok
    assert "expected ConnectorResult" in result.error


def test_status_write_failure_does_not_fail_the_run():
    """Observations may have landed; losing the status write must not undo that."""
    store = FakeStore()
    store.raise_on_write = True
    result = run_connector(StubConnector(), store)
    assert result.ok  # the run itself still succeeded


# ── the write budget ────────────────────────────────────────────────────────


def test_exceeding_the_write_budget_is_a_failure_despite_self_reported_success():
    """The runaway reported success while looping — volume overrides the claim."""
    store = FakeStore()
    greedy = StubConnector(result=ConnectorResult.success(records_written=500))

    result = run_connector(greedy, store, max_writes=200)

    assert not result.ok
    assert "budget exceeded" in result.error
    assert result.detail["records_attempted"] == 500
    assert store.writes[-1].status == "failing"


def test_writes_within_budget_pass():
    result = run_connector(
        StubConnector(result=ConnectorResult.success(records_written=16)),
        FakeStore(),
        max_writes=200,
    )
    assert result.ok and result.records_written == 16


# ── isolation ───────────────────────────────────────────────────────────────


def test_one_connector_failing_does_not_stop_the_others():
    """Fly being unreachable must not stop GitHub, and vice versa."""
    store = FakeStore()
    good_a = StubConnector(name="a")
    bad = StubConnector(name="b", exc=RuntimeError("down"))
    good_c = StubConnector(name="c")

    results = run_all([good_a, bad, good_c], store)

    assert results["a"].ok
    assert not results["b"].ok
    assert results["c"].ok
    assert good_c.calls == 1  # ran despite b blowing up first


# ── hybrid: the verify loop must not disturb the push clock ────────────────


def test_verify_preserves_last_push_at():
    """The push path runs outside this loop; a verify must not clear its clock.

    'Verified, events also arriving' and 'verified, nothing pushed in a week'
    are different situations — the second is how a silently-dead webhook
    becomes visible at all.
    """
    prior = ConnectorStatus(
        connector_name="stub",
        ingestion_mode="hybrid",
        last_push_at="2026-09-01T10:00:00+00:00",
    )
    store = FakeStore({"stub": prior})

    run_connector(StubConnector(), store)

    assert store.writes[-1].last_push_at == "2026-09-01T10:00:00+00:00"


def test_ingestion_mode_is_carried_from_the_connector():
    class Hybrid(StubConnector):
        ingestion_mode = "hybrid"

    store = FakeStore()
    run_connector(Hybrid(), store)
    assert store.writes[-1].ingestion_mode == "hybrid"


def test_connector_without_a_declared_mode_records_poll():
    store = FakeStore()
    run_connector(StubConnector(), store)
    assert store.writes[-1].ingestion_mode == "poll"
