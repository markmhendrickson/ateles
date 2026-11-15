"""Operator-command retry of one definitely failed Monedula payment.

All payment rails and Neotoma are fakes. No test calls a wallet, Wise, the
network, or the production journal.
"""

from __future__ import annotations

import copy
import io
import multiprocessing
import os
import subprocess
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import consent_email  # noqa: E402
import monedula  # noqa: E402
import payment_approval  # noqa: E402
import payment_journal  # noqa: E402
import payment_retry  # noqa: E402
from handlers import btc_transfer, wise_transfer  # noqa: E402
from handlers.payment_profile import PaymentProfile  # noqa: E402
from lib.daemon_runtime import gating  # noqa: E402
from test_payment_approval import (  # noqa: E402
    APPROVER_JKT,
    APPROVER_SUB,
    OTHER_SUB,
    FakeNeotoma,
)

SESSION_DATE = "2026-11-11"
EVENT_ID = "evt-retry"
SESSION_KEY = f"yoga:{EVENT_ID}"
ENDED_AT = datetime(2026, 11, 11, 21, 0, tzinfo=timezone.utc).timestamp()


class FakeHandler:
    name = "yoga"

    def __init__(self) -> None:
        self.profile = types.SimpleNamespace(
            label="Yoga Example",
            amount_eur=10,
            calendar_keywords=["yoga"],
            payment_type="wise",
            wise_reference="synthetic-ref",
            wise_recipient_name="Synthetic Payee",
        )
        self.execute_calls: list[dict] = []
        self.preflight_calls: list[dict] = []
        self.crash = False

    def payee_identity(self) -> str:
        return "wise|recipient|synthetic"

    def payee_display_name(self) -> str:
        return "Synthetic Payee"

    def preflight(self, match: dict) -> None:
        self.preflight_calls.append(match)

    def execute(self, match: dict) -> dict:
        self.execute_calls.append(match)
        if self.crash:
            raise RuntimeError("synthetic crash after intent")
        return {"status": "sent", "handler": self.name}


def _original_key(handler: FakeHandler) -> str:
    return consent_email.obligation_key(handler, SESSION_DATE, EVENT_ID)


def _journal(
    path: Path,
    handler: FakeHandler,
    *,
    outcome: str | None = "failed",
    failure_class: str = "pre_submission",
    session_date: str = SESSION_DATE,
    session_key: str = SESSION_KEY,
    ended_at: float = ENDED_AT,
) -> str:
    original = consent_email.obligation_key(handler, session_date, EVENT_ID)
    payment_journal.record_intent(path, original, "original-token", generation=1)
    if outcome is not None:
        result = (
            {"status": outcome, "failure_class": failure_class}
            if hasattr(payment_journal, "FAILURE_UNKNOWN")
            else outcome
        )
        payment_journal.record_outcome(path, original, result)
    payment_journal.note_pending(path, session_key, ended_at=ended_at)
    if hasattr(payment_journal, "FAILURE_UNKNOWN"):
        payment_journal.note_pending(
            path,
            session_key,
            ended_at=ended_at,
            started_on=session_date,
            obligation=original,
        )
    payment_journal.set_session_state(
        path, session_key, payment_journal.SESSION_SETTLED
    )
    return original


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def _execute(
    original: str,
    journal_path: Path,
    handler: FakeHandler,
) -> payment_retry.RetryResult:
    return payment_retry.run_retry(
        original,
        journal_path=journal_path,
        handlers=[handler],
        execute=True,
        stdin=FakeTTY("10 EUR Synthetic Payee\n"),
        stdout=io.StringIO(),
    )


class ProcessHandler:
    def __init__(self, counter_path: str) -> None:
        self.counter_path = counter_path

    def execute(self) -> None:
        fd = os.open(self.counter_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, b"execute\n")
        finally:
            os.close(fd)


def _reserve_then_execute(
    start,
    journal_path: str,
    original: str,
    token: str,
    intent_at: float,
    outcome_at: float,
    failure_class: str,
    counter_path: str,
) -> None:
    start.wait()
    try:
        payment_journal.reserve_retry(
            Path(journal_path),
            original,
            SESSION_KEY,
            token,
            generation=2,
            expected_intent_at=intent_at,
            expected_outcome_at=outcome_at,
            expected_failure_class=failure_class,
            not_paid_confirmed=False,
        )
    except payment_journal.JournalError:
        return
    ProcessHandler(counter_path).execute()


def _advance_generation(start, journal_path: str) -> None:
    start.wait()
    payment_journal.next_generation(Path(journal_path))


@pytest.fixture
def retry_env(monkeypatch, tmp_path):
    handler = FakeHandler()
    journal_path = tmp_path / payment_journal.JOURNAL_NAME
    original = _journal(journal_path, handler)
    neo = FakeNeotoma()
    monkeypatch.setenv("MONEDULA_PAYMENT_APPROVER_SUB", APPROVER_SUB)
    monkeypatch.setenv("MONEDULA_PAYMENT_APPROVER_JKT", APPROVER_JKT)
    monkeypatch.setattr(payment_approval, "_request", neo.request)
    monkeypatch.setattr(
        gating,
        "_fetch_entity_observations",
        lambda entity_id, **kwargs: list(neo.obs.get(entity_id, [])),
    )
    return handler, journal_path, original, neo


@pytest.mark.parametrize(
    "outcome",
    [
        "sent",
        "succeeded",
        "success",
        "completed",
        "unknown",
        "pending",
        "processing",
        "in_progress",
        "awaiting",
        "skipped",
        "rejected",
        "declined",
        "manual_required",
        "unreported",
    ],
)
def test_retry_refuses_every_known_non_failed_outcome(tmp_path, outcome):
    handler = FakeHandler()
    journal_path = tmp_path / payment_journal.JOURNAL_NAME
    original = _journal(journal_path, handler, outcome=outcome)

    with pytest.raises(payment_retry.RetryRefused, match="not a definite failure"):
        payment_retry.run_retry(
            original, journal_path=journal_path, handlers=[handler]
        )
    assert handler.execute_calls == []


def test_retry_refuses_intent_only_unknown_outcome(tmp_path):
    handler = FakeHandler()
    journal_path = tmp_path / payment_journal.JOURNAL_NAME
    original = _journal(journal_path, handler, outcome=None)

    with pytest.raises(payment_retry.RetryRefused, match="outcome is unknown"):
        payment_retry.run_retry(
            original, journal_path=journal_path, handlers=[handler]
        )


def test_retry_refuses_missing_and_unrecognised_entries(tmp_path):
    handler = FakeHandler()
    journal_path = tmp_path / payment_journal.JOURNAL_NAME
    original = _journal(journal_path, handler, outcome="mystery")
    with pytest.raises(payment_retry.RetryRefused, match="unrecognised"):
        payment_retry.run_retry(
            original, journal_path=journal_path, handlers=[handler]
        )
    with pytest.raises(payment_retry.RetryRefused, match="not recorded"):
        payment_retry.run_retry(
            "0" * 32, journal_path=journal_path, handlers=[handler]
        )


@pytest.mark.parametrize(
    "missing",
    ["token", "generation", "intent_at", "outcome_at"],
)
def test_retry_refuses_malformed_original_journal_entry(missing):
    entry = {
        "state": payment_journal.STATE_DONE,
        "token": "original-token",
        "generation": 1,
        "intent_at": 1.0,
        "outcome_status": "failed",
        "outcome_at": 2.0,
    }
    entry.pop(missing)
    journal = {"obligations": {"original": entry}}

    with pytest.raises(payment_retry.RetryRefused, match="malformed"):
        payment_retry._recorded_outcome(journal, "original")


def test_retry_raises_one_fresh_idempotent_approval(retry_env):
    handler, journal_path, original, neo = retry_env

    first = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler]
    )
    second = payment_retry.run_retry(
        SESSION_KEY, journal_path=journal_path, handlers=[handler]
    )

    assert first.state == second.state == "awaiting_approval"
    assert first.retry_key == second.retry_key == payment_retry.retry_key(original)
    assert neo.writes == 1
    assert neo.only["payment_key"] == first.retry_key
    assert neo.only["status"] == payment_approval.STATUS_AWAITING
    assert handler.execute_calls == []


def test_retry_pays_only_after_authenticated_yes_and_preserves_original(retry_env):
    handler, journal_path, original, neo = retry_env
    before = copy.deepcopy(payment_journal.load(journal_path)["obligations"][original])
    raised = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler]
    )
    neo.resolve(raised.retry_key, "approved", "attended yoga")

    result = _execute(original, journal_path, handler)

    journal = payment_journal.load(journal_path)
    assert result.state == "paid"
    assert len(handler.execute_calls) == 1
    assert payment_journal.retry_state(journal, original) == "done"
    assert journal["obligations"][original]["retry"]["outcome_status"] == "sent"
    assert {
        key: value
        for key, value in journal["obligations"][original].items()
        if key != "retry"
    } == before

    with pytest.raises(payment_retry.RetryRefused, match="already has an outcome"):
        payment_retry.run_retry(
            original, journal_path=journal_path, handlers=[handler]
        )
    assert len(handler.execute_calls) == 1


def test_approved_retry_without_execute_flag_never_calls_handler(retry_env):
    handler, journal_path, original, neo = retry_env
    raised = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler]
    )
    neo.resolve(raised.retry_key, "approved", "attended yoga")

    result = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler]
    )

    assert result.state == "approved"
    assert handler.execute_calls == []
    assert payment_journal.retry_state(
        payment_journal.load(journal_path), original
    ) is None


def test_execute_refuses_without_interactive_tty(retry_env):
    handler, journal_path, original, neo = retry_env
    raised = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler]
    )
    neo.resolve(raised.retry_key, "approved", "attended yoga")

    with pytest.raises(payment_retry.RetryRefused, match="interactive TTY"):
        payment_retry.run_retry(
            original,
            journal_path=journal_path,
            handlers=[handler],
            execute=True,
            stdin=io.StringIO("10 EUR Synthetic Payee\n"),
            stdout=io.StringIO(),
        )

    assert handler.execute_calls == []


def test_execute_refuses_wrong_typed_amount_or_payee(retry_env):
    handler, journal_path, original, neo = retry_env
    raised = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler]
    )
    neo.resolve(raised.retry_key, "approved", "attended yoga")

    with pytest.raises(payment_retry.RetryRefused, match="did not match"):
        payment_retry.run_retry(
            original,
            journal_path=journal_path,
            handlers=[handler],
            execute=True,
            stdin=FakeTTY("11 EUR Different Payee\n"),
            stdout=io.StringIO(),
        )

    assert handler.execute_calls == []


@pytest.mark.parametrize("failure_class", ["unknown", "unclassified"])
def test_unconfirmed_original_requires_authenticated_not_paid_confirmation(
    monkeypatch, tmp_path, failure_class
):
    handler = FakeHandler()
    journal_path = tmp_path / payment_journal.JOURNAL_NAME
    stored_class = "" if failure_class == "unclassified" else failure_class
    real_outcome_fields = getattr(payment_journal, "_outcome_fields", None)
    if failure_class == "unclassified" and real_outcome_fields is not None:
        monkeypatch.setattr(
            payment_journal,
            "_outcome_fields",
            lambda result: ("failed", None),
        )
    original = _journal(
        journal_path,
        handler,
        failure_class=stored_class,
    )
    if real_outcome_fields is not None:
        monkeypatch.setattr(payment_journal, "_outcome_fields", real_outcome_fields)
    neo = FakeNeotoma()
    monkeypatch.setenv("MONEDULA_PAYMENT_APPROVER_SUB", APPROVER_SUB)
    monkeypatch.setenv("MONEDULA_PAYMENT_APPROVER_JKT", APPROVER_JKT)
    monkeypatch.setattr(payment_approval, "_request", neo.request)
    monkeypatch.setattr(
        gating,
        "_fetch_entity_observations",
        lambda entity_id, **kwargs: list(neo.obs.get(entity_id, [])),
    )

    raised = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler]
    )
    row = neo.only
    assert row["title"].startswith("Retry of failed payment")
    assert f"original_obligation={original}" in row["title"]
    assert f"failure_class={failure_class}" in row["title"]
    assert "FIRST ATTEMPT OUTCOME UNCONFIRMED" in row["title"]
    assert "payee=Synthetic Payee" in row["title"]
    assert "amount=10 EUR" in row["title"]

    neo.resolve(raised.retry_key, "approved", "attended yoga")
    result = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler], execute=False
    )
    assert result.state == "blocked"
    assert handler.execute_calls == []

    neo.resolve(
        raised.retry_key,
        "approved",
        "attended yoga; not paid",
    )
    result = _execute(original, journal_path, handler)
    assert result.state == "paid"
    assert len(handler.execute_calls) == 1


def test_retry_does_not_pay_on_rejection(retry_env):
    handler, journal_path, original, neo = retry_env
    raised = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler]
    )
    neo.resolve(raised.retry_key, "rejected", "no")

    result = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler]
    )

    assert result.state == "rejected"
    assert handler.execute_calls == []
    assert payment_journal.retry_state(payment_journal.load(journal_path), original) is None


def test_retry_does_not_pay_on_unauthenticated_answer(retry_env):
    handler, journal_path, original, neo = retry_env
    raised = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler]
    )
    neo.resolve(
        raised.retry_key,
        "approved",
        "attended yoga",
        sub=OTHER_SUB,
    )

    result = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler]
    )

    assert result.state == "blocked"
    assert handler.execute_calls == []
    assert payment_journal.retry_state(payment_journal.load(journal_path), original) is None


def test_approval_is_refetched_and_rejudged_immediately_before_reservation(
    retry_env, monkeypatch
):
    handler, journal_path, original, neo = retry_env
    raised = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler]
    )
    neo.resolve(raised.retry_key, "approved", "attended yoga")
    real_find = payment_approval.find_entity
    calls = {"count": 0}

    def withdraw_on_second_read(key):
        calls["count"] += 1
        if calls["count"] == 2:
            neo.resolve(key, "rejected", "no")
        return real_find(key)

    monkeypatch.setattr(payment_approval, "find_entity", withdraw_on_second_read)

    with pytest.raises(payment_retry.RetryRefused, match="no longer permits"):
        _execute(original, journal_path, handler)

    assert calls["count"] == 2
    assert handler.execute_calls == []
    assert payment_journal.retry_state(
        payment_journal.load(journal_path), original
    ) is None


def test_retry_refuses_nonterminal_source_session(retry_env):
    handler, journal_path, original, _neo = retry_env
    payment_journal.set_session_state(
        journal_path, SESSION_KEY, payment_journal.SESSION_PENDING
    )

    with pytest.raises(payment_retry.RetryRefused, match="not terminal"):
        payment_retry.run_retry(
            original, journal_path=journal_path, handlers=[handler]
        )

    assert handler.execute_calls == []


def test_two_processes_reserve_one_retry_and_call_execute_once(tmp_path):
    handler = FakeHandler()
    journal_path = tmp_path / payment_journal.JOURNAL_NAME
    original = _journal(journal_path, handler)
    entry = payment_journal.load(journal_path)["obligations"][original]
    counter = tmp_path / "execute-count"
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    args = (
        start,
        str(journal_path),
        original,
        "retry-token",
        entry["intent_at"],
        entry["outcome_at"],
        entry["failure_class"],
        str(counter),
    )
    processes = [ctx.Process(target=_reserve_then_execute, args=args) for _ in range(2)]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0

    assert counter.read_text().splitlines() == ["execute"]
    assert payment_journal.retry_state(
        payment_journal.load(journal_path), original
    ) == payment_journal.STATE_INTENT


def test_normal_journal_write_during_retry_reservation_is_not_lost(tmp_path):
    handler = FakeHandler()
    journal_path = tmp_path / payment_journal.JOURNAL_NAME
    original = _journal(journal_path, handler)
    entry = payment_journal.load(journal_path)["obligations"][original]
    counter = tmp_path / "execute-count"
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    retry_process = ctx.Process(
        target=_reserve_then_execute,
        args=(
            start,
            str(journal_path),
            original,
            "retry-token",
            entry["intent_at"],
            entry["outcome_at"],
            entry["failure_class"],
            str(counter),
        ),
    )
    normal_process = ctx.Process(
        target=_advance_generation,
        args=(start, str(journal_path)),
    )
    retry_process.start()
    normal_process.start()
    start.set()
    retry_process.join(10)
    normal_process.join(10)
    assert retry_process.exitcode == normal_process.exitcode == 0

    journal = payment_journal.load(journal_path)
    assert journal["generation"] == 1
    assert payment_journal.retry_state(journal, original) == payment_journal.STATE_INTENT
    assert counter.read_text().splitlines() == ["execute"]


def test_crash_after_retry_intent_never_executes_a_second_time(retry_env):
    handler, journal_path, original, neo = retry_env
    raised = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler]
    )
    neo.resolve(raised.retry_key, "approved", "attended yoga")
    handler.crash = True

    with pytest.raises(RuntimeError, match="synthetic crash"):
        _execute(original, journal_path, handler)
    assert len(handler.execute_calls) == 1
    assert (
        payment_journal.retry_state(payment_journal.load(journal_path), original)
        == "intent"
    )

    handler.crash = False
    with pytest.raises(payment_retry.RetryRefused, match="outcome is unknown"):
        payment_retry.run_retry(
            original, journal_path=journal_path, handlers=[handler]
        )
    assert len(handler.execute_calls) == 1


def test_dry_run_changes_neither_journal_nor_approval_store(retry_env):
    handler, journal_path, original, neo = retry_env
    before = journal_path.read_bytes()

    result = payment_retry.run_retry(
        original,
        journal_path=journal_path,
        handlers=[handler],
        dry_run=True,
    )

    assert result.state == "dry_run"
    assert journal_path.read_bytes() == before
    assert neo.writes == 0
    assert handler.preflight_calls == []
    assert handler.execute_calls == []


@pytest.mark.parametrize(
    ("start", "end", "expected_date"),
    [
        (
            {"dateTime": "2026-11-11T23:30:00+01:00"},
            {"dateTime": "2026-11-12T00:30:00+01:00"},
            "2026-11-11",
        ),
        (
            {"date": "2026-11-11"},
            {"date": "2026-11-12"},
            "2026-11-11",
        ),
    ],
)
def test_retry_uses_journaled_session_start_date_not_end_date(
    monkeypatch, tmp_path, start, end, expected_date
):
    handler = FakeHandler()
    event = {"id": EVENT_ID, "start": start, "end": end}
    match = {"event": event, "summary": "synthetic"}
    monkeypatch.setattr(consent_email, "STATE_FILE", tmp_path / "consent.json")

    monedula._apply_session_journal([(handler, [match])], reconcile=False)
    journal_path = consent_email.journal_path()
    journal = payment_journal.load(journal_path)
    session = journal["sessions"][SESSION_KEY]
    original = consent_email.obligation_key(handler, expected_date, EVENT_ID)
    assert session["started_on"] == expected_date
    assert session["obligation"] == original

    payment_journal.record_intent(
        journal_path, original, "original-token", generation=1
    )
    payment_journal.record_outcome(
        journal_path,
        original,
        {"status": "failed", "failure_class": "pre_submission"},
    )
    payment_journal.set_session_state(
        journal_path, SESSION_KEY, payment_journal.SESSION_SETTLED
    )
    resolved = payment_retry._resolve_original(
        SESSION_KEY, payment_journal.load(journal_path), [handler]
    )
    assert resolved[0] == original
    assert resolved[3] == expected_date


def test_journal_defaults_unclassified_handler_failure_to_unknown(tmp_path):
    path = tmp_path / payment_journal.JOURNAL_NAME
    payment_journal.record_intent(path, "obligation", "token", generation=1)
    payment_journal.record_outcome(path, "obligation", {"status": "failed"})
    entry = payment_journal.load(path)["obligations"]["obligation"]
    assert entry["failure_class"] == payment_journal.FAILURE_UNKNOWN


def test_btc_failures_distinguish_pre_submission_from_unknown(monkeypatch):
    profile = PaymentProfile(
        prefix="TEST",
        label="Synthetic",
        calendar_keywords=["synthetic"],
        payment_type="btc",
        amount_eur=10,
    )
    handler = btc_transfer.BtcTransferHandler(profile)
    assert handler.execute({})["failure_class"] == "pre_submission"

    profile.btc_address = "bc1synthetic"
    monkeypatch.setattr(btc_transfer, "_find_claude", lambda: None)
    assert handler.execute({})["failure_class"] == "pre_submission"

    monkeypatch.setattr(btc_transfer, "_find_claude", lambda: "/synthetic/claude")
    monkeypatch.setattr(
        btc_transfer.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("claude", 300)
        ),
    )
    assert handler.execute({})["failure_class"] == "unknown"


def test_wise_failures_distinguish_pre_submission_from_unknown(monkeypatch):
    profile = PaymentProfile(
        prefix="TEST",
        label="Synthetic",
        calendar_keywords=["synthetic"],
        payment_type="wise",
        amount_eur=10,
        wise_recipient_id=1,
        wise_recipient_name="Synthetic Payee",
    )
    handler = wise_transfer.WiseTransferHandler(profile)
    monkeypatch.delenv("WISE_API_TOKEN", raising=False)
    assert handler.execute({})["failure_class"] == "pre_submission"

    monkeypatch.setenv("WISE_API_TOKEN", "synthetic-token")
    monkeypatch.setattr(
        wise_transfer,
        "_execute_wise_transfer",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("synthetic uncertain rail failure")
        ),
    )
    assert handler.execute({})["failure_class"] == "unknown"


def test_retry_uses_handler_execute_without_memo_or_op_return(retry_env):
    handler, journal_path, original, neo = retry_env
    handler.profile.payment_type = "btc"
    handler.profile.wise_reference = "must-not-be-used"
    original = _original_key(handler)
    journal_path.unlink()
    _journal(journal_path, handler)
    raised = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler]
    )
    assert neo.only["rail"] == "btc"
    assert neo.only["reference"] == ""
    assert "memo" not in neo.only
    neo.resolve(raised.retry_key, "approved", "attended yoga")

    _execute(original, journal_path, handler)

    (match,) = handler.execute_calls
    assert "memo" not in match
    assert "op_return" not in match


def test_documented_script_runs_from_repository_root(tmp_path):
    script = Path(payment_retry.__file__).resolve()
    repo_root = script.parents[3]
    env = dict(os.environ, HOME=str(tmp_path))

    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "original obligation key or handler:event-id" in result.stdout
