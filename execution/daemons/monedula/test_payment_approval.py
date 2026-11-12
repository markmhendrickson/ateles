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
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import payment_approval  # noqa: E402
import payment_journal  # noqa: E402
from test_session_payments import _event, _Handler, _World  # noqa: E402,F401


OWNER = "11111111-2222-3333-4444-555555555555"
APPROVER_SUB = "coordinator@ateles-swarm"
APPROVER_JKT = "A" * 43
OTHER_SUB = "intruder@ateles-swarm"


def _validated_payment_key_filter(body: dict):
    """Validate the /entities/query request the way the Neotoma API does.

    Contract (openapi ``snapshot_filters``): a map of field to
    ``{"op": <operator>, "value": ...}``. A bare value is a 400 on the real
    server, so it must fail here too; the fake once accepted it silently and
    every real lookup would have been held.
    """
    assert body.get("entity_type") == payment_approval.ENTITY_TYPE
    assert isinstance(body.get("limit"), int) and not isinstance(body["limit"], bool)
    filters = body.get("snapshot_filters")
    if filters is None:
        return None
    if not isinstance(filters, dict):
        raise payment_approval.ApprovalError("HTTP 400: snapshot_filters must be an object")
    for name, spec in filters.items():
        if (
            not isinstance(spec, dict)
            or spec.get("op") not in {"eq", "in", "gt", "gte", "lt", "lte", "ne", "contains"}
            or "value" not in spec
            or set(spec) - {"op", "value"}
        ):
            raise payment_approval.ApprovalError(
                f"HTTP 400: snapshot_filters.{name} must be {{op, value}}"
            )
    spec = filters.get("payment_key")
    if spec is None:
        return None
    assert spec["op"] == "eq", "payment_key lookup must be an exact match"
    return spec["value"]


class FakeNeotoma:
    """In-memory Neotoma: entities, per-field provenance and observations."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}  # payment_key -> snapshot
        self.ids: dict[str, str] = {}
        self.prov: dict[str, dict] = {}  # payment_key -> {field: observation id}
        self.obs: dict[str, list] = {}  # entity_id -> observations
        self.down = False
        self.writes = 0
        self.queries: list[dict] = []
        self.drop_fields: set[str] = set()  # simulate undeclared fields
        self.fail_query_for: set[str] = set()
        self._n = 0

    def _observe(self, key, fields, sub, jkt, tier="software"):
        self._n += 1
        oid = f"obs_{self._n}"
        eid = self.ids[key]
        self.obs.setdefault(eid, []).append(
            {
                "id": oid,
                "fields": dict(fields),
                "user_id": OWNER,
                "provenance": {
                    "agent_sub": sub,
                    "agent_thumbprint": jkt,
                    "attribution_tier": tier,
                },
            }
        )
        for name in fields:
            self.prov.setdefault(key, {})[name] = oid

    def request(self, method, path, body=None):
        if self.down:
            raise payment_approval.ApprovalError("Neotoma unreachable")
        if path == "/store":
            self.writes += 1
            for ent in body["entities"]:
                key = ent["payment_key"]
                if key not in self.rows:
                    fields = {
                        k: v
                        for k, v in ent.items()
                        if k not in self.drop_fields and k != "entity_type"
                    }
                    self.rows[key] = fields
                    self.ids[key] = f"ent_{len(self.ids)}"
                    self._observe(key, fields, "monedula@ateles-swarm", "M" * 43)
            return {"ok": True}
        if path == "/entities/query":
            self.queries.append(body)
            want = _validated_payment_key_filter(body)
            if want in self.fail_query_for:
                raise payment_approval.ApprovalError("query failed")
            return {
                "entities": [
                    {"entity_id": self.ids[k], "snapshot": dict(r)}
                    for k, r in self.rows.items()
                    if want is None or k == want
                ][: body.get("limit", 100)]
            }
        if path.startswith("/entities/"):
            eid = path.rsplit("/", 1)[1]
            for key, ident in self.ids.items():
                if ident == eid:
                    return {
                        "entity_id": eid,
                        "user_id": OWNER,
                        "snapshot": {"snapshot": dict(self.rows[key])},
                        "provenance": dict(self.prov.get(key, {})),
                    }
            raise payment_approval.ApprovalError("not found")
        raise AssertionError(path)

    def resolve(self, key, status, answer, sub=APPROVER_SUB, jkt=APPROVER_JKT, tier="software"):
        """What the coordinator does: answer first, then status."""
        self.rows[key]["operator_answer"] = answer
        self._observe(key, {"operator_answer": answer}, sub, jkt, tier)
        self.rows[key]["status"] = status
        self._observe(key, {"status": status}, sub, jkt, tier)

    @property
    def only(self) -> dict:
        (row,) = self.rows.values()
        return row


@pytest.fixture
def env(monkeypatch, tmp_path):
    from lib.daemon_runtime import gating

    w = _World(monkeypatch, tmp_path, _Handler())
    w.handler.profile.payment_type = "wise"
    w.handler.profile.wise_reference = "ref"
    w.handler.profile.wise_recipient_name = "Test Payee"
    monkeypatch.setenv("MONEDULA_CONSENT_CHANNEL", "payment_approval")
    monkeypatch.setenv("MONEDULA_PAYMENT_APPROVER_SUB", APPROVER_SUB)
    monkeypatch.setenv("MONEDULA_PAYMENT_APPROVER_JKT", APPROVER_JKT)
    neo = FakeNeotoma()
    monkeypatch.setattr(payment_approval, "_request", neo.request)
    monkeypatch.setattr(
        gating, "_fetch_entity_observations", lambda eid, **k: list(neo.obs.get(eid, []))
    )
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
    assert set(subscribed) == {"task", "checkpoint_brief"}  # vocab-ok: live type Apis subscribes to; pins that payment_approval is not among them


def test_record_has_no_task_or_dispatch_fields(env):
    w, neo = env
    w.tick()
    assert not {"task_entity_id", "gate_action", "blast_radius"} & set(neo.only)
