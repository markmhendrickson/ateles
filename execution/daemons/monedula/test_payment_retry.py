"""Operator-command retry of one definitely failed Monedula payment.

All payment rails and Neotoma are fakes. No test calls a wallet, Wise, the
network, or the production journal.
"""

from __future__ import annotations

import copy
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
import payment_approval  # noqa: E402
import payment_journal  # noqa: E402
import payment_retry  # noqa: E402
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
) -> str:
    original = _original_key(handler)
    payment_journal.record_intent(path, original, "original-token", generation=1)
    if outcome is not None:
        payment_journal.record_outcome(path, original, outcome)
    payment_journal.note_pending(path, SESSION_KEY, ended_at=ENDED_AT)
    payment_journal.set_session_state(
        path, SESSION_KEY, payment_journal.SESSION_SETTLED
    )
    return original


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

    result = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler]
    )

    journal = payment_journal.load(journal_path)
    assert result.state == "paid"
    assert len(handler.execute_calls) == 1
    assert payment_journal.obligation_state(journal, raised.retry_key) == "done"
    assert journal["obligations"][raised.retry_key]["outcome_status"] == "sent"
    assert journal["obligations"][original] == before

    with pytest.raises(payment_retry.RetryRefused, match="already has an outcome"):
        payment_retry.run_retry(
            original, journal_path=journal_path, handlers=[handler]
        )
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
    assert raised.retry_key not in payment_journal.load(journal_path)["obligations"]


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
    assert raised.retry_key not in payment_journal.load(journal_path)["obligations"]


def test_crash_after_retry_intent_never_executes_a_second_time(retry_env):
    handler, journal_path, original, neo = retry_env
    raised = payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler]
    )
    neo.resolve(raised.retry_key, "approved", "attended yoga")
    handler.crash = True

    with pytest.raises(RuntimeError, match="synthetic crash"):
        payment_retry.run_retry(
            original, journal_path=journal_path, handlers=[handler]
        )
    assert len(handler.execute_calls) == 1
    assert (
        payment_journal.obligation_state(
            payment_journal.load(journal_path), raised.retry_key
        )
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

    payment_retry.run_retry(
        original, journal_path=journal_path, handlers=[handler]
    )

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
