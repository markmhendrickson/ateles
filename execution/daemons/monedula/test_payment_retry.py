"""Operator-command retry of one definitely failed Monedula payment.

All payment rails and Neotoma are fakes. No test calls a wallet, Wise, the
network, or the production journal.
"""

from __future__ import annotations

import copy
import io
import json
import time
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

import chain_reconcile  # noqa: E402
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
    explorer=None,
) -> payment_retry.RetryResult:
    kwargs = {} if explorer is None else {"explorer": explorer}
    return payment_retry.run_retry(
        original,
        journal_path=journal_path,
        handlers=[handler],
        execute=True,
        stdin=FakeTTY("10 EUR Synthetic Payee\n"),
        stdout=io.StringIO(),
        **kwargs,
    )


PAYEE_ADDRESS = "bc1qsyntheticpayee0000000000000000000000"
OTHER_ADDRESS = "bc1qsyntheticother0000000000000000000000"


def _make_btc(handler: FakeHandler) -> None:
    handler.profile.payment_type = "btc"
    handler.profile.btc_address = PAYEE_ADDRESS


def _txid(n: int) -> str:
    return f"{n:064x}"


def _tx(n: int, *, to: str, block_time: int | None = None) -> dict:
    status = (
        {"confirmed": True, "block_time": block_time}
        if block_time is not None
        else {"confirmed": False}
    )
    return {
        "txid": _txid(n),
        "status": status,
        "vout": [
            {"scriptpubkey_address": to, "value": 1000},
            {"scriptpubkey": "6a", "value": 0},
        ],
    }


class FakeChain:
    """An Esplora explorer served from memory; records every URL asked for."""

    BASE = "https://explorer.invalid/api"

    def __init__(self) -> None:
        self.mempool: list = []
        self.chain: list = []
        self.urls: list[str] = []
        self.fail: Exception | None = None
        self.raw_mempool = None

    def fetch(self, url: str):
        self.urls.append(url)
        if self.fail is not None:
            raise self.fail
        prefix = f"{self.BASE}/address/{PAYEE_ADDRESS}/txs"
        assert url.startswith(prefix), url
        rest = url[len(prefix):]
        if rest == "/mempool":
            return self.raw_mempool if self.raw_mempool is not None else list(self.mempool)
        if rest == "/chain":
            return self.chain[:25]
        if rest.startswith("/chain/"):
            last = rest[len("/chain/"):]
            ids = [tx["txid"] for tx in self.chain]
            i = ids.index(last) + 1
            return self.chain[i:i + 25]
        raise AssertionError(url)

    def explorer(self) -> chain_reconcile.EsploraExplorer:
        return chain_reconcile.EsploraExplorer(self.BASE, fetch=self.fetch)


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
            admission="pre_submission",
            approval_revision="rev-1",
            approval_check=_same_revision,
            reconciliation=None,
        )
    except payment_journal.JournalError:
        return
    ProcessHandler(counter_path).execute()


def _same_revision() -> str:
    return "rev-1"


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


def _journal_with_class(monkeypatch, journal_path, handler, failure_class):
    """Journal a failed original with ``failure_class``; ``unclassified``
    reproduces a row written before failure classes existed."""
    if failure_class == "unclassified":
        real = payment_journal._outcome_fields
        monkeypatch.setattr(
            payment_journal, "_outcome_fields", lambda result: ("failed", None)
        )
        original = _journal(journal_path, handler, failure_class="")
        monkeypatch.setattr(payment_journal, "_outcome_fields", real)
        return original
    return _journal(journal_path, handler, failure_class=failure_class)


@pytest.mark.parametrize("failure_class", ["unknown", "unclassified"])
@pytest.mark.parametrize("answer", ["attended yoga", "attended yoga; not paid"])
def test_wise_unconfirmed_failure_is_refused_for_manual_reconciliation(
    retry_env, monkeypatch, failure_class, answer
):
    handler, journal_path, _original, neo = retry_env
    journal_path.unlink()
    original = _journal_with_class(monkeypatch, journal_path, handler, failure_class)

    with pytest.raises(payment_retry.RetryRefused, match="manual reconciliation"):
        payment_retry.run_retry(
            original, journal_path=journal_path, handlers=[handler]
        )
    with pytest.raises(payment_retry.RetryRefused, match="manual reconciliation"):
        _execute(original, journal_path, handler)

    assert neo.writes == 0
    assert handler.execute_calls == []
    assert payment_journal.retry_state(
        payment_journal.load(journal_path), original
    ) is None


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
    _make_btc(handler)
    handler.profile.wise_reference = "must-not-be-used"
    original = _original_key(handler)
    journal_path.unlink()
    _journal(journal_path, handler)
    chain = FakeChain()
    raised = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler],
        explorer=chain.explorer(),
    )
    assert neo.only["rail"] == "btc"
    assert neo.only["reference"] == ""
    assert "memo" not in neo.only
    neo.resolve(raised.retry_key, "approved", "attended yoga")

    _execute(original, journal_path, handler, explorer=chain.explorer())

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


# ── Finding 1: approval withdrawal races ─────────────────────────────────────


def _count_judgments(monkeypatch, after_call: int, action) -> dict:
    """Run ``action`` right after the ``after_call``-th approval judgment
    returns, i.e. between that judgment and whatever the code does next."""
    real = payment_approval.judge
    calls = {"count": 0}

    def judge(*args, **kwargs):
        verdict = real(*args, **kwargs)
        calls["count"] += 1
        if calls["count"] == after_call:
            action()
        return verdict

    monkeypatch.setattr(payment_approval, "judge", judge)
    return calls


@pytest.mark.parametrize(
    ("status", "answer"),
    [("rejected", "no"), ("approved", "attended yoga, but hold on")],
)
def test_retry_refuses_withdrawal_between_final_judgment_and_reservation(
    retry_env, monkeypatch, status, answer
):
    handler, journal_path, original, neo = retry_env
    raised = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler]
    )
    neo.resolve(raised.retry_key, "approved", "attended yoga")
    # Judgment 1 admits the run; judgment 2 is the final one before the
    # reservation. The operator's rejection or overwrite lands right after it.
    _count_judgments(
        monkeypatch, 2, lambda: neo.resolve(raised.retry_key, status, answer)
    )

    with pytest.raises(payment_retry.RetryRefused):
        _execute(original, journal_path, handler)

    assert handler.execute_calls == []
    journal = payment_journal.load(journal_path)
    assert payment_journal.retry_state(journal, original) is None
    assert "retry" not in journal["obligations"][original]


@pytest.mark.parametrize(
    ("status", "answer"),
    [("rejected", "no"), ("approved", "attended yoga again")],
)
def test_retry_aborts_when_approval_changes_between_reservation_and_execute(
    retry_env, monkeypatch, status, answer
):
    handler, journal_path, original, neo = retry_env
    raised = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler]
    )
    neo.resolve(raised.retry_key, "approved", "attended yoga")
    real_reserve = payment_journal.reserve_retry

    def reserve_then_change(*args, **kwargs):
        real_reserve(*args, **kwargs)
        neo.resolve(raised.retry_key, status, answer)

    monkeypatch.setattr(payment_journal, "reserve_retry", reserve_then_change)

    result = _execute(original, journal_path, handler)

    assert result.state == "aborted"
    assert handler.execute_calls == []
    journal = payment_journal.load(journal_path)
    assert payment_journal.retry_state(journal, original) == "aborted"
    assert "outcome_status" not in journal["obligations"][original]["retry"]
    with pytest.raises(payment_retry.RetryRefused, match="aborted"):
        _execute(original, journal_path, handler)
    assert handler.execute_calls == []


def test_reservation_records_the_approval_revision_it_was_bound_to(retry_env):
    handler, journal_path, original, neo = retry_env
    raised = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler]
    )
    neo.resolve(raised.retry_key, "approved", "attended yoga")

    _execute(original, journal_path, handler)

    retry = payment_journal.load(journal_path)["obligations"][original]["retry"]
    entity = payment_approval.find_entity(raised.retry_key)
    assert retry["approval_revision"] == payment_approval.approval_revision(entity)
    assert retry["admission"] == "pre_submission"


# ── Finding 3: the approval is bound to the original attempt ────────────────


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("outcome_at", 12345.0),
        ("intent_at", 12340.0),
        ("failure_class", "unknown"),
    ],
)
def test_retry_refused_when_original_mutated_after_approval(
    retry_env, field, value
):
    handler, journal_path, original, neo = retry_env
    raised = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler]
    )
    neo.resolve(raised.retry_key, "approved", "attended yoga")

    data = json.loads(journal_path.read_text())
    data["obligations"][original][field] = value
    journal_path.write_text(json.dumps(data))

    try:
        result = _execute(original, journal_path, handler)
    except payment_retry.RetryRefused:
        result = None
    assert result is None or result.state == "blocked"
    assert handler.execute_calls == []
    assert payment_journal.retry_state(
        payment_journal.load(journal_path), original
    ) is None


def test_completed_original_outcome_is_immutable(tmp_path):
    path = tmp_path / payment_journal.JOURNAL_NAME
    payment_journal.record_intent(path, "obligation", "token", generation=1)
    payment_journal.record_outcome(
        path, "obligation", {"status": "failed", "failure_class": "pre_submission"}
    )
    before = payment_journal.load(path)["obligations"]["obligation"]

    with pytest.raises(payment_journal.JournalError, match="immutable"):
        payment_journal.record_outcome(path, "obligation", {"status": "sent"})

    assert payment_journal.load(path)["obligations"]["obligation"] == before


def test_approval_record_carries_structured_retry_fields_not_title_text(retry_env):
    handler, journal_path, original, neo = retry_env
    entry = payment_journal.load(journal_path)["obligations"][original]

    payment_retry.run_retry(original, journal_path=journal_path, handlers=[handler])

    row = neo.only
    assert row["retry_of"] == original
    assert row["original_failure_class"] == "pre_submission"
    assert row["retry_admission"] == "pre_submission"
    assert row["reconciliation_evidence"] == ""
    assert row["original_intent_at"] == repr(float(entry["intent_at"]))
    assert row["original_outcome_at"] == repr(float(entry["outcome_at"]))
    assert set(payment_approval.RETRY_BOUND_FIELDS) <= set(payment_approval.FIELD_SPECS)


# ── Finding 2: BTC admission is a recorded chain reconciliation ──────────────


@pytest.fixture
def btc_env(retry_env, monkeypatch):
    handler, journal_path, _original, neo = retry_env
    _make_btc(handler)
    journal_path.unlink()
    return handler, journal_path, neo, monkeypatch


@pytest.mark.parametrize("failure_class", ["unknown", "unclassified", "pre_submission"])
def test_btc_retry_pays_after_clear_chain_reconciliation_and_attendance(
    btc_env, failure_class
):
    handler, journal_path, neo, monkeypatch = btc_env
    original = _journal_with_class(monkeypatch, journal_path, handler, failure_class)
    chain = FakeChain()
    old = int(time.time()) - 7 * 86400
    chain.chain = [_tx(1, to=PAYEE_ADDRESS, block_time=old)]  # last week's payment
    chain.mempool = [_tx(2, to=OTHER_ADDRESS)]

    raised = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler],
        explorer=chain.explorer(),
    )
    row = neo.only
    evidence = json.loads(row["reconciliation_evidence"])
    assert row["retry_admission"] == "btc_chain_reconciled"
    assert evidence["result"] == chain_reconcile.RESULT_CLEAR
    assert evidence["explorer"] == FakeChain.BASE
    assert evidence["txids_seen"] == [_txid(2)]
    assert evidence["payee_address_sha256"] == chain_reconcile.address_digest(PAYEE_ADDRESS)
    assert PAYEE_ADDRESS not in json.dumps(row)
    queried = len(chain.urls)
    assert queried >= 2

    neo.resolve(raised.retry_key, "approved", "attended yoga")
    result = _execute(original, journal_path, handler, explorer=chain.explorer())

    assert result.state == "paid"
    assert len(handler.execute_calls) == 1
    assert len(chain.urls) > queried, "reconciled again before reserving"
    retry = payment_journal.load(journal_path)["obligations"][original]["retry"]
    assert retry["admission"] == "btc_chain_reconciled"
    assert retry["reconciliation"]["result"] == chain_reconcile.RESULT_CLEAR
    assert retry["reconciliation"]["payee_address_sha256"] == evidence["payee_address_sha256"]
    assert retry["reconciliation"]["window_start_epoch"] == evidence["window_start_epoch"]
    assert PAYEE_ADDRESS not in journal_path.read_text()


@pytest.mark.parametrize("where", ["mempool", "confirmed", "confirmed_just_before_intent"])
def test_btc_retry_refuses_when_chain_shows_any_payment_to_payee(btc_env, where):
    handler, journal_path, neo, monkeypatch = btc_env
    original = _journal_with_class(monkeypatch, journal_path, handler, "unknown")
    intent_at = payment_journal.load(journal_path)["obligations"][original]["intent_at"]
    chain = FakeChain()
    if where == "mempool":
        chain.mempool = [_tx(7, to=PAYEE_ADDRESS)]
    elif where == "confirmed":
        chain.chain = [_tx(7, to=PAYEE_ADDRESS, block_time=int(intent_at) + 60)]
    else:  # block timestamps may lag wall time
        chain.chain = [_tx(7, to=PAYEE_ADDRESS, block_time=int(intent_at) - 600)]

    with pytest.raises(payment_retry.RetryRefused, match=_txid(7)):
        payment_retry.run_retry(
            original, journal_path=journal_path, handlers=[handler],
            explorer=chain.explorer(),
        )
    assert neo.writes == 0
    assert handler.execute_calls == []


@pytest.mark.parametrize(
    "failure",
    ["error", "timeout", "not_a_list", "malformed_tx", "full_mempool", "unbounded_history"],
)
def test_btc_retry_refuses_when_chain_lookup_cannot_be_trusted(btc_env, failure):
    handler, journal_path, neo, monkeypatch = btc_env
    original = _journal_with_class(monkeypatch, journal_path, handler, "unknown")
    chain = FakeChain()
    now = int(time.time())
    if failure == "error":
        chain.fail = OSError("synthetic network down")
    elif failure == "timeout":
        chain.fail = TimeoutError("synthetic timeout")
    elif failure == "not_a_list":
        chain.raw_mempool = {"error": "rate limited"}
    elif failure == "malformed_tx":
        chain.mempool = [{"txid": "zz", "status": {}, "vout": []}]
    elif failure == "full_mempool":
        chain.mempool = [_tx(100 + i, to=OTHER_ADDRESS) for i in range(50)]
    else:
        chain.chain = [
            _tx(1000 + i, to=OTHER_ADDRESS, block_time=now) for i in range(25 * 21)
        ]

    with pytest.raises(payment_retry.RetryRefused, match="chain reconciliation failed"):
        payment_retry.run_retry(
            original, journal_path=journal_path, handlers=[handler],
            explorer=chain.explorer(),
        )
    assert neo.writes == 0
    assert handler.execute_calls == []


def test_btc_chain_history_is_paged_until_before_the_window(btc_env):
    handler, journal_path, neo, monkeypatch = btc_env
    original = _journal_with_class(monkeypatch, journal_path, handler, "unknown")
    now = int(time.time())
    chain = FakeChain()
    chain.chain = [_tx(10 + i, to=OTHER_ADDRESS, block_time=now) for i in range(25)]
    chain.chain.append(_tx(99, to=PAYEE_ADDRESS, block_time=now))

    with pytest.raises(payment_retry.RetryRefused, match=_txid(99)):
        payment_retry.run_retry(
            original, journal_path=journal_path, handlers=[handler],
            explorer=chain.explorer(),
        )
    assert any("/chain/" in url for url in chain.urls)
    assert neo.writes == 0


def test_btc_payment_seen_after_approval_refuses_before_reservation(btc_env):
    handler, journal_path, neo, monkeypatch = btc_env
    original = _journal_with_class(monkeypatch, journal_path, handler, "unknown")
    chain = FakeChain()
    raised = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler],
        explorer=chain.explorer(),
    )
    neo.resolve(raised.retry_key, "approved", "attended yoga")
    chain.mempool = [_tx(5, to=PAYEE_ADDRESS)]  # the timed-out child broadcast late

    with pytest.raises(payment_retry.RetryRefused, match=_txid(5)):
        _execute(original, journal_path, handler, explorer=chain.explorer())

    assert handler.execute_calls == []
    assert payment_journal.retry_state(
        payment_journal.load(journal_path), original
    ) is None


@pytest.mark.parametrize("answer", ["attended yoga", "attended yoga; not paid", "not paid attended yoga"])
def test_not_paid_in_the_answer_is_not_evidence_for_btc(btc_env, answer):
    handler, journal_path, neo, monkeypatch = btc_env
    original = _journal_with_class(monkeypatch, journal_path, handler, "unknown")
    chain = FakeChain()
    raised = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler],
        explorer=chain.explorer(),
    )
    neo.resolve(raised.retry_key, "approved", answer)
    chain.fail = OSError("synthetic explorer outage")

    try:
        result = _execute(original, journal_path, handler, explorer=chain.explorer())
    except payment_retry.RetryRefused as exc:
        assert "chain reconciliation failed" in str(exc)
        result = None
    assert result is None or result.state == "blocked"
    assert handler.execute_calls == []
    assert payment_journal.retry_state(
        payment_journal.load(journal_path), original
    ) is None


@pytest.mark.parametrize(
    "tamper",
    ["paid_result", "other_payee", "other_window", "other_explorer", "empty"],
)
def test_btc_approval_must_carry_matching_clear_evidence(btc_env, tamper):
    handler, journal_path, neo, monkeypatch = btc_env
    original = _journal_with_class(monkeypatch, journal_path, handler, "unknown")
    chain = FakeChain()
    raised = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler],
        explorer=chain.explorer(),
    )
    evidence = json.loads(neo.rows[raised.retry_key]["reconciliation_evidence"])
    if tamper == "paid_result":
        evidence["result"] = chain_reconcile.RESULT_PAID
        evidence["paying_txids"] = [_txid(3)]
    elif tamper == "other_payee":
        evidence["payee_address_sha256"] = chain_reconcile.address_digest(OTHER_ADDRESS)
    elif tamper == "other_window":
        evidence["window_start_epoch"] += 86400
    elif tamper == "other_explorer":
        evidence["explorer"] = "https://elsewhere.invalid/api"
    raw = "" if tamper == "empty" else chain_reconcile.canonical(evidence)
    neo.rows[raised.retry_key]["reconciliation_evidence"] = raw
    neo.resolve(raised.retry_key, "approved", "attended yoga")

    result = _execute(original, journal_path, handler, explorer=chain.explorer())

    assert result.state == "blocked"
    assert handler.execute_calls == []


def test_btc_without_payee_address_is_refused(btc_env):
    handler, journal_path, neo, monkeypatch = btc_env
    original = _journal_with_class(monkeypatch, journal_path, handler, "unknown")
    handler.profile.btc_address = ""
    with pytest.raises(payment_retry.RetryRefused):
        payment_retry.run_retry(
            original, journal_path=journal_path, handlers=[handler],
            explorer=FakeChain().explorer(),
        )
    assert neo.writes == 0


# ── Tests never touch the daemon directory's state files ─────────────────────


def test_tests_use_a_temporary_journal_not_the_daemon_directory(tmp_path):
    daemon_dir = Path(payment_retry.__file__).resolve().parent
    assert consent_email.journal_path().parent != daemon_dir
    assert tmp_path in consent_email.journal_path().parents


def test_retry_raise_fails_closed_when_schema_drops_retry_fields(retry_env):
    handler, journal_path, original, neo = retry_env
    neo.drop_fields = {"retry_of", "reconciliation_evidence"}

    with pytest.raises(payment_approval.ApprovalError, match="did not persist"):
        payment_retry.run_retry(
            original, journal_path=journal_path, handlers=[handler]
        )
    assert handler.execute_calls == []


# ── Self-review fixes ────────────────────────────────────────────────────────


def test_btc_payee_address_matches_case_insensitively(btc_env):
    handler, journal_path, neo, monkeypatch = btc_env
    handler.profile.btc_address = PAYEE_ADDRESS.upper()
    original = _journal_with_class(monkeypatch, journal_path, handler, "unknown")
    chain = FakeChain()
    chain.mempool = [_tx(8, to=PAYEE_ADDRESS)]

    with pytest.raises(payment_retry.RetryRefused, match=_txid(8)):
        payment_retry.run_retry(
            original, journal_path=journal_path, handlers=[handler],
            explorer=chain.explorer(),
        )
    assert neo.writes == 0


def test_btc_paging_does_not_stop_on_one_old_looking_block(btc_env):
    handler, journal_path, neo, monkeypatch = btc_env
    original = _journal_with_class(monkeypatch, journal_path, handler, "unknown")
    now = int(time.time())
    chain = FakeChain()
    page = [_tx(10 + i, to=OTHER_ADDRESS, block_time=now) for i in range(24)]
    page.append(_tx(40, to=OTHER_ADDRESS, block_time=now - 3 * 86400))
    chain.chain = page + [_tx(99, to=PAYEE_ADDRESS, block_time=now)]

    with pytest.raises(payment_retry.RetryRefused, match=_txid(99)):
        payment_retry.run_retry(
            original, journal_path=journal_path, handlers=[handler],
            explorer=chain.explorer(),
        )
    assert neo.writes == 0


def test_btc_explorer_must_use_https(btc_env):
    handler, journal_path, neo, monkeypatch = btc_env
    original = _journal_with_class(monkeypatch, journal_path, handler, "unknown")
    chain = FakeChain()
    explorer = chain_reconcile.EsploraExplorer("http://explorer.invalid/api", fetch=chain.fetch)

    with pytest.raises(payment_retry.RetryRefused, match="https"):
        payment_retry.run_retry(
            original, journal_path=journal_path, handlers=[handler], explorer=explorer
        )
    assert chain.urls == []
    assert neo.writes == 0


def test_btc_late_broadcast_after_reservation_aborts_before_execute(btc_env):
    handler, journal_path, neo, monkeypatch = btc_env
    original = _journal_with_class(monkeypatch, journal_path, handler, "unknown")
    chain = FakeChain()
    raised = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler],
        explorer=chain.explorer(),
    )
    neo.resolve(raised.retry_key, "approved", "attended yoga")
    real_reserve = payment_journal.reserve_retry

    def reserve_then_broadcast(*args, **kwargs):
        real_reserve(*args, **kwargs)
        chain.mempool = [_tx(6, to=PAYEE_ADDRESS)]

    monkeypatch.setattr(payment_journal, "reserve_retry", reserve_then_broadcast)

    result = _execute(original, journal_path, handler, explorer=chain.explorer())

    assert result.state == "aborted"
    assert handler.execute_calls == []
    assert payment_journal.retry_state(
        payment_journal.load(journal_path), original
    ) == "aborted"


def test_non_btc_retry_record_carrying_evidence_is_blocked(retry_env):
    handler, journal_path, original, neo = retry_env
    raised = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler]
    )
    neo.rows[raised.retry_key]["reconciliation_evidence"] = '{"result":"x"}'
    neo.resolve(raised.retry_key, "approved", "attended yoga")

    result = _execute(original, journal_path, handler)

    assert result.state == "blocked"
    assert handler.execute_calls == []
