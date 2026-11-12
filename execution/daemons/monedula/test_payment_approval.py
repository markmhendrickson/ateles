"""Per-payment approval records (operator ruling 2026-11-12).

Neotoma is faked by an in-memory store behind ``payment_approval._request``.
Synthetic fixtures only.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import payment_approval  # noqa: E402
import payment_journal  # noqa: E402
from test_session_payments import _event, _Handler, _World  # noqa: E402,F401


class FakeNeotoma:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.down = False
        self.writes = 0
        self.drop_fields: set[str] = set()  # simulate undeclared fields

    def request(self, method, path, body=None):
        if self.down:
            raise payment_approval.ApprovalError("Neotoma unreachable")
        if path == "/store":
            self.writes += 1
            for ent in body["entities"]:
                key = ent["payment_key"]
                if key not in self.rows:
                    self.rows[key] = {
                        k: v for k, v in ent.items() if k not in self.drop_fields
                    }
            return {"ok": True}
        if path == "/entities/query":
            return {
                "entities": [
                    {"entity_id": f"ent_{i}", "snapshot": dict(r)}
                    for i, r in enumerate(self.rows.values())
                ]
            }
        raise AssertionError(path)

    def resolve(self, key, status, answer):
        """What the coordinator does: answer first, then status."""
        self.rows[key]["operator_answer"] = answer
        self.rows[key]["status"] = status

    @property
    def only(self) -> dict:
        (row,) = self.rows.values()
        return row


@pytest.fixture
def env(monkeypatch, tmp_path):
    w = _World(monkeypatch, tmp_path, _Handler())
    w.handler.profile.payment_type = "wise"
    w.handler.profile.wise_reference = "ref"
    w.handler.profile.wise_recipient_name = "Test Payee"
    monkeypatch.setenv("MONEDULA_CONSENT_CHANNEL", "checkpoint")
    neo = FakeNeotoma()
    monkeypatch.setattr(payment_approval, "_request", neo.request)
    w.calendars["primary"] = [_event("evt-1")]
    return w, neo


def test_no_checkpoint_means_no_payment_and_one_is_raised(env):
    w, neo = env
    w.tick()
    w.tick(minutes=15)
    assert w.handler.execute_calls == []
    assert neo.writes == 1, "raised once, not per tick"
    assert neo.only["status"] == payment_approval.STATUS_AWAITING


def test_record_carries_session_amount_payee_rail_and_no_payee_secrets(env):
    w, neo = env
    w.tick()
    row = neo.only
    assert row["handler"] == "yoga"
    assert row["session"] == "evt-1" and row["session_date"] == "2026-11-11"
    assert (row["amount"], row["currency"], row["rail"]) == (10, "EUR", "wise")
    assert row["payee_display_name"] == "Test Payee" and row["reference"] == "ref"
    assert row["blocking"] is True and row["confidence_threshold"] == 1.0
    assert not (payment_approval.FORBIDDEN_FIELD_NAMES & set(row))
    blob = repr(row)
    assert not re.search(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,}\b|\b(bc1|1|3)[a-zA-Z0-9]{25,}\b", blob)


def test_unresolved_checkpoint_means_no_payment(env):
    w, neo = env
    w.tick()
    w.tick(minutes=15)
    assert w.handler.execute_calls == []


def test_rejected_checkpoint_means_no_payment_and_is_not_raised_again(env):
    w, neo = env
    w.tick()
    neo.resolve(neo.only["payment_key"], "rejected", "no")
    w.tick(minutes=15)
    w.tick(minutes=15)
    assert w.handler.execute_calls == []
    assert neo.writes == 1
    assert payment_journal.session_state(w.journal, "yoga:evt-1") == "skipped"


@pytest.mark.parametrize("answer", ["yes", "y", "yes all", "ok", "", "no"])
def test_approved_with_a_bare_yes_or_unparseable_answer_does_not_pay(env, answer):
    w, neo = env
    w.tick()
    neo.resolve(neo.only["payment_key"], "approved", answer)
    w.tick(minutes=15)
    assert w.handler.execute_calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("amount", 99),
        ("payee_display_name", "Someone Else"),
        ("session", "evt-other"),
        ("session_date", "2026-11-01"),
        ("currency", "USD"),
        ("rail", "btc"),
        ("reference", "other"),
        ("payment_key", "deadbeef"),
    ],
)
def test_approval_for_a_different_payment_does_not_pay(env, field, value):
    w, neo = env
    w.tick()
    key = neo.only["payment_key"]
    neo.resolve(key, "approved", "attended yoga")
    neo.rows[key][field] = value
    if field == "payment_key":
        # the lookup is by payment_key, so a mismatched key simply finds nothing
        neo.rows["deadbeef"] = neo.rows.pop(key)
    w.tick(minutes=15)
    assert w.handler.execute_calls == []


def test_attended_session_pays_exactly_once(env):
    w, neo = env
    w.tick()
    neo.resolve(neo.only["payment_key"], "approved", "attended yoga")
    w.tick(minutes=15)
    assert len(w.handler.execute_calls) == 1
    w.tick(minutes=15)
    w.tick(minutes=15)
    assert len(w.handler.execute_calls) == 1


def test_yes_with_the_name_also_approves(env):
    w, neo = env
    w.tick()
    neo.resolve(neo.only["payment_key"], "approved", "yes yoga")
    w.tick(minutes=15)
    assert len(w.handler.execute_calls) == 1


def test_neotoma_unreachable_means_no_payment_and_pending_is_kept(env):
    w, neo = env
    w.tick()
    neo.resolve(neo.only["payment_key"], "approved", "attended yoga")
    neo.down = True
    ok = w.tick(minutes=15)
    assert ok is False
    assert w.handler.execute_calls == []
    assert "yoga:evt-1" in payment_journal.pending_sessions(w.journal)
    # Neotoma back: the standing approval is honoured, once.
    neo.down = False
    w.tick(minutes=15)
    assert len(w.handler.execute_calls) == 1


def test_neotoma_down_at_first_raise_keeps_session_pending_and_retries(env):
    w, neo = env
    neo.down = True
    w.tick()
    assert "yoga:evt-1" in payment_journal.pending_sessions(w.journal)
    neo.down = False
    w.tick(minutes=15)
    assert neo.writes == 1 and w.handler.execute_calls == []


def test_a_write_that_does_not_read_back_is_an_error_not_a_pending_request(env):
    w, neo = env
    neo.drop_fields = {"amount"}  # schema silently drops an undeclared field
    ok = w.tick()
    assert ok is False
    assert w.handler.execute_calls == []


def test_telegram_credentials_are_not_required_for_payment(env, monkeypatch):
    w, neo = env
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ALLOWED_USER_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("MONEDULA_CONSENT_CHANNEL")
    w.tick()
    neo.resolve(neo.only["payment_key"], "approved", "attended yoga")
    w.tick(minutes=15)
    assert len(w.handler.execute_calls) == 1


# ── Double-pay: approval is consumed by Monedula alone ──────────────────────


def test_apis_never_subscribes_to_payment_approval():
    """Apis reacts only to the entity types it subscribes to. payment_approval
    is not among them, and the record carries no task reference."""
    src = (Path(__file__).parents[1] / "apis" / "apis.py").read_text()
    tree = ast.parse(src)
    subscribed = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "SUBSCRIBE_ENTITY_TYPES" for t in node.targets
        ):
            subscribed = ast.literal_eval(node.value)
    assert subscribed is not None
    assert payment_approval.ENTITY_TYPE not in subscribed
    assert set(subscribed) == {"task", "checkpoint_brief"}


def test_record_has_no_task_or_dispatch_fields(env):
    w, neo = env
    w.tick()
    assert not {"task_entity_id", "gate_action", "blast_radius"} & set(neo.only)
