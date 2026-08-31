"""
Settlement-state tests for the Wise payment handler (ateles#575).

The defect: a Wise transfer that had been SUBMITTED but not DELIVERED produced
the same `sent` result as a delivered one, and `sent` drives the task to
`status: done` and the profile to `status: archived`. The record then asserted a
completed payment that had not happened — and because an archived profile is
skipped by the loader, the payment that never landed also stopped being visible.
That is the self-concealing shape of ateles#552.

Every assertion here is on the EFFECT — the argv actually handed to the neotoma
CLI — not on the status string the handler returned. A handler can return a new
status and still close the task; only the argv proves it did not.

No live Wise or Neotoma: subprocess is captured, shutil.which is stubbed, and
Wise is reached only through monkeypatched helpers. Synthetic IBANs and ids
throughout.

Run with: pytest execution/daemons/monedula/test_wise_settlement_state.py -v
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from handlers.payment_profile import PaymentProfile
from handlers.wise_transfer import (
    RESULT_AWAITING_SETTLEMENT,
    WiseTransferHandler,
    _execute_wise_transfer,
    _update_task,
    classify_transfer_state,
)

TODAY = date.today()

# Assembled in parts so the repo's PII scanner sees no IBAN-shaped literal
# (rule pii-iban in .gitleaks.toml), matching test_payment_amount_precision.py
# and test_wise_legal_type.py. Nothing here validates an IBAN — it only has to
# be a non-empty payee identifier.
IBAN = " ".join(["XX00", "0000", "0000", "0000", "0000", "00"])


# ── Harness ──────────────────────────────────────────────────────────────────


def _profile(**kw) -> PaymentProfile:
    base = dict(
        prefix="INVOICE",
        label="Invoice",
        calendar_keywords=[],
        payment_type="wise",
        amount_eur=Decimal("100.00"),
        due_date=TODAY.isoformat(),
        one_off=True,
        entity_id="prof_x",
        wise_reference="ref-1",
    )
    base.update(kw)
    return PaymentProfile(**base)  # type: ignore[arg-type]


def _recurring(**kw) -> PaymentProfile:
    return _profile(
        prefix="THERAPY",
        label="Therapy",
        calendar_keywords=["therapy"],
        due_date="",
        one_off=False,
        entity_id="prof_r",
        **kw,
    )


class _Ok:
    returncode = 0
    stderr = ""
    stdout = "{}"


@pytest.fixture
def calls(monkeypatch) -> list[list[str]]:
    """Capture every subprocess argv the handler emits."""
    import subprocess as _sp

    captured: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        captured.append(list(cmd))
        return _Ok()

    monkeypatch.setattr(_sp, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda _n: "/usr/bin/neotoma")
    monkeypatch.setattr("handlers.wise_transfer._find_task_id", lambda _p: "task_x")
    return captured


def status_updates(calls: list[list[str]]) -> list[tuple[str, str]]:
    """(entity_id, status) for every `entities update … --status <v>` argv."""
    out = []
    for c in calls:
        if "entities" in c and "update" in c and "--status" in c:
            idx = c.index("update")
            out.append((c[idx + 1], c[c.index("--status") + 1]))
    return out


def corrections(calls: list[list[str]]) -> list[tuple[str, str, str]]:
    """(entity_id, field_name, corrected_value) for every corrections argv."""
    out = []
    for c in calls:
        if "corrections" in c and "create" in c:
            out.append(
                (
                    c[c.index("--entity-id") + 1],
                    c[c.index("--field-name") + 1],
                    c[c.index("--corrected-value") + 1],
                )
            )
    return out


def notes(calls: list[list[str]]) -> list[str]:
    return [c[c.index("--notes") + 1] for c in calls if "--notes" in c]


def due_dates(calls: list[list[str]]) -> list[str]:
    return [c[c.index("--due-date") + 1] for c in calls if "--due-date" in c]


def _wire_wise(
    monkeypatch,
    funding_status: str,
    transfer_status: str | None = None,
    *,
    fetch_returns_none: bool = False,
) -> dict:
    """Stub the whole Wise flow. Returns a call-count dict."""
    counts = {"post_transfers": 0, "fetch": 0}
    record: dict = {"id": 7, "sourceValue": "100.00"}
    if transfer_status is not None:
        record["status"] = transfer_status

    def _create(*a, **k):
        counts["post_transfers"] += 1
        return (7, dict(record))

    def _fetch(*a, **k):
        counts["fetch"] += 1
        return None if fetch_returns_none else dict(record)

    monkeypatch.setattr("handlers.wise_transfer._get_wise_profile_id", lambda t: 1)
    monkeypatch.setattr(
        "handlers.wise_transfer._get_or_create_recipient", lambda *a, **k: 99
    )
    monkeypatch.setattr("handlers.wise_transfer._create_quote", lambda *a, **k: "q")
    monkeypatch.setattr("handlers.wise_transfer._create_transfer", _create)
    monkeypatch.setattr(
        "handlers.wise_transfer._fund_transfer",
        lambda *a, **k: {"status": funding_status},
    )
    monkeypatch.setattr("handlers.wise_transfer._fetch_transfer", _fetch)
    return counts


def _run(funding: str, transfer_status: str | None, monkeypatch, **kw) -> dict:
    _wire_wise(monkeypatch, funding, transfer_status, **kw)
    return _execute_wise_transfer(
        "tok", IBAN, "Payee", Decimal("100.00"), "ref-1", label="t"
    )


# ── AC-1 / AC-2: an unsettled transfer never reaches done ────────────────────


@pytest.mark.parametrize("funding", ["PENDING", "PROCESSING"])
def test_unsettled_funding_does_not_close_task(funding, monkeypatch, calls) -> None:
    """The core defect: PENDING/PROCESSING must not mark the task done."""
    result = _run(funding, "processing", monkeypatch)
    assert result["status"] == RESULT_AWAITING_SETTLEMENT

    _update_task(_profile(), result)

    assert status_updates(calls) == [], (
        f"{funding} must not write any status; got {status_updates(calls)}"
    )
    assert ("prof_x", "status", "awaiting_settlement") in corrections(calls)
    assert notes(calls), "the in-flight transfer must still be noted on the task"


def test_completed_funding_with_delivered_transfer_still_closes(
    monkeypatch, calls
) -> None:
    """AC-3 no-regression: a genuinely delivered payment still closes."""
    result = _run("COMPLETED", "outgoing_payment_sent", monkeypatch)
    assert result["status"] == "sent"

    _update_task(_profile(), result)

    assert ("task_x", "done") in status_updates(calls)
    assert ("prof_x", "archived") in status_updates(calls)
    assert not [c for c in corrections(calls) if c[2] == "awaiting_settlement"]


def test_completed_funding_on_unsettled_transfer_does_not_close(
    monkeypatch, calls
) -> None:
    """Funding COMPLETED is NOT delivery.

    _fund_transfer reads status off the PAYMENT object: COMPLETED there means
    the money left the balance, not that the transfer settled. The operator's
    own hand-run ledger records exactly this — payment status COMPLETED,
    transfer status "processing". A fix that only rejected PENDING/PROCESSING
    would leave the defect intact on this, the most common path.
    """
    result = _run("COMPLETED", "processing", monkeypatch)
    assert result["status"] == RESULT_AWAITING_SETTLEMENT

    _update_task(_profile(), result)
    assert status_updates(calls) == []


def test_unknown_funding_status_raises_and_writes_nothing(monkeypatch, calls) -> None:
    """AC-4: an unrecognised funding status still raises, closing nothing."""
    for bad in ("", "FAILED", "REJECTED"):
        with pytest.raises(RuntimeError):
            _run(bad, "processing", monkeypatch)
    assert status_updates(calls) == []
    assert corrections(calls) == []


def test_note_carries_transfer_id_and_raw_status(monkeypatch, calls) -> None:
    """AC-5: the in-flight transfer must be traceable from the task."""
    result = _run("PENDING", "processing", monkeypatch)
    _update_task(_profile(), result)

    (note,) = notes(calls)
    assert "AWAITING SETTLEMENT" in note
    assert "transfer_id=7" in note
    assert "PENDING" in note
    assert "Payment sent" not in note


def test_recurring_unsettled_does_not_roll_due_date(monkeypatch, calls) -> None:
    """AC-8: the due_date roll is a claim the payment happened. Defer it."""
    rolled = {"called": False}

    def _next(*a, **k):
        rolled["called"] = True
        return "2026-09-30"

    monkeypatch.setattr("handlers.wise_transfer._find_next_event_due_date", _next)

    result = _run("PENDING", "processing", monkeypatch)
    _update_task(_recurring(), result)

    assert due_dates(calls) == []
    assert rolled["called"] is False


def test_pending_writes_all_three_profile_fields(monkeypatch, calls) -> None:
    result = _run("PENDING", "processing", monkeypatch)
    _update_task(_profile(), result)

    got = {(f, v) for _e, f, v in corrections(calls)}
    assert ("status", "awaiting_settlement") in got
    assert ("pending_transfer_id", "7") in got
    assert ("pending_transfer_at", TODAY.isoformat()) in got


# ── classify_transfer_state ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "record,expected",
    [
        ({"status": "outgoing_payment_sent"}, "settled"),
        ({"status": "OUTGOING_PAYMENT_SENT"}, "settled"),
        ({"status": "  outgoing_payment_sent  "}, "settled"),
        ({"status": "cancelled"}, "failed"),
        ({"status": "funds_refunded"}, "failed"),
        ({"status": "bounced_back"}, "failed"),
        ({"status": "charged_back"}, "failed"),
        ({"status": "processing"}, "in_flight"),
        ({"status": "funds_converted"}, "in_flight"),
        ({"status": "incoming_payment_waiting"}, "in_flight"),
        ({"status": "incoming_payment_initiated"}, "in_flight"),
        ({"status": "unknown"}, "in_flight"),
        ({"status": ""}, "in_flight"),
        ({}, "unreadable"),
        ({"status": None}, "unreadable"),
        (None, "unreadable"),
    ],
)
def test_classify_transfer_state_table(record, expected) -> None:
    assert classify_transfer_state(record) == expected


def test_only_the_explicit_settled_set_ever_classifies_settled() -> None:
    """A property over the whole space, not a row-by-row check.

    Nothing may be read as delivered except a status Wise explicitly uses for
    delivery. This is the assertion that keeps a future status value, a typo,
    or an empty response from becoming "the money arrived".
    """
    for status in (
        "processing",
        "unknown",
        "",
        "cancelled",
        "bounced_back",
        "outgoing_payment_sent_maybe",
        "sent",
        "completed",
        None,
    ):
        record = {"status": status} if status is not None else {}
        assert classify_transfer_state(record) != "settled", status
    assert classify_transfer_state(None) != "settled"


# ── The funding × transfer-record matrix ─────────────────────────────────────


def test_pending_funding_with_delivered_record_returns_sent(monkeypatch, calls) -> None:
    """A stale funding response does not hold back a delivered transfer."""
    result = _run("PENDING", "outgoing_payment_sent", monkeypatch)
    assert result["status"] == "sent"

    _update_task(_profile(), result)
    assert ("task_x", "done") in status_updates(calls)


def test_pending_funding_with_failed_record_raises(monkeypatch) -> None:
    """A dead transfer is a failure, not a park: it must not sit in limbo."""
    with pytest.raises(RuntimeError) as exc:
        _run("PENDING", "bounced_back", monkeypatch)
    assert "bounced_back" in str(exc.value)


def test_unreadable_record_parks_rather_than_closes(monkeypatch, calls) -> None:
    """The highest-consequence branch: a Wise read failure is not delivery.

    _fetch_transfer returns None and the creation-time record carries no
    status, so nothing has told us the transfer arrived. The only safe reading
    of "we could not check" is "we do not know" — never "it landed".
    """
    result = _run("PENDING", None, monkeypatch, fetch_returns_none=True)
    assert result["status"] == RESULT_AWAITING_SETTLEMENT

    _update_task(_profile(), result)
    assert status_updates(calls) == []


def test_amount_mismatch_wins_over_pending(monkeypatch, calls) -> None:
    """#574's reconciliation runs before any settlement decision."""
    from handlers.wise_transfer import TransferAmountMismatch

    monkeypatch.setattr("handlers.wise_transfer._get_wise_profile_id", lambda t: 1)
    monkeypatch.setattr(
        "handlers.wise_transfer._get_or_create_recipient", lambda *a, **k: 99
    )
    monkeypatch.setattr("handlers.wise_transfer._create_quote", lambda *a, **k: "q")
    monkeypatch.setattr(
        "handlers.wise_transfer._create_transfer",
        lambda *a, **k: (7, {"id": 7, "sourceValue": "100.00"}),
    )
    monkeypatch.setattr(
        "handlers.wise_transfer._fund_transfer", lambda *a, **k: {"status": "PENDING"}
    )
    monkeypatch.setattr(
        "handlers.wise_transfer._fetch_transfer",
        lambda *a, **k: {"id": 7, "sourceValue": "99.00", "status": "processing"},
    )

    with pytest.raises(TransferAmountMismatch):
        _execute_wise_transfer(
            "tok", IBAN, "Payee", Decimal("100.00"), "ref-1", label="t"
        )
    assert corrections(calls) == []


def test_transfer_is_fetched_exactly_once(monkeypatch) -> None:
    """Reconciliation and classification must judge the SAME record."""
    counts = _wire_wise(monkeypatch, "PENDING", "processing")
    _execute_wise_transfer("tok", IBAN, "Payee", Decimal("100.00"), "ref-1", label="t")
    assert counts["fetch"] == 1


# ── AC-6: the double-payment guard ───────────────────────────────────────────


def test_parked_profile_does_not_load_for_payment(monkeypatch) -> None:
    """A parked profile cannot re-match, re-preview, or re-pay."""
    payload = {
        "entities": [
            {
                "entity_id": "prof_x",
                "snapshot": {
                    "label": "Invoice",
                    "payment_type": "wise",
                    "amount_eur": "100.00",
                    "due_date": TODAY.isoformat(),
                    "status": "awaiting_settlement",
                    "pending_transfer_id": "7",
                },
            }
        ]
    }
    # Default load (the payment leg) sees nothing…
    assert _load_with(monkeypatch, payload) == []
    # …while the sweep's loader sees exactly this profile.
    parked = _load_with(monkeypatch, payload, statuses=("awaiting_settlement",))
    assert [p.entity_id for p in parked] == ["prof_x"]
    assert parked[0].pending_transfer_id == "7"


def _load_with(monkeypatch, payload, statuses=None):
    """Drive load_profiles_from_neotoma over a stubbed HTTP response."""
    import io
    import json as _json
    import urllib.request

    import handlers.payment_profile as pp

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://example.invalid")
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *a, **k: _Resp(_json.dumps(payload).encode()),
    )
    if statuses is None:
        return pp.load_profiles_from_neotoma()
    return pp.load_profiles_from_neotoma(statuses=statuses)


@pytest.mark.parametrize(
    "status", ["paused", "archived", "awaiting_settlement", "payment_failed", "weird"]
)
def test_default_load_excludes_every_non_active_status(status, monkeypatch) -> None:
    """The status filter fails CLOSED: an unanticipated value cannot pay."""
    payload = {
        "entities": [
            {
                "entity_id": "prof_x",
                "snapshot": {
                    "label": "Invoice",
                    "payment_type": "wise",
                    "amount_eur": "100.00",
                    "due_date": TODAY.isoformat(),
                    "status": status,
                },
            }
        ]
    }
    assert _load_with(monkeypatch, payload) == []


def test_preview_discloses_in_flight_transfer(monkeypatch) -> None:
    """Defence in depth: if a parked profile reaches a preview, say so."""
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.setattr("handlers.wise_transfer._find_task_id", lambda _p: "task_x")
    profile = _profile(
        wise_iban=IBAN,
        wise_recipient_name="ACME, S.L.",
        pending_transfer_id="7",
        pending_transfer_at="2026-08-20",
    )
    h = WiseTransferHandler(profile)
    text = h.preview(h.matches([])[0])
    assert "ALREADY IN FLIGHT" in text
    assert "7" in text
    assert "2026-08-20" in text
    assert "do NOT approve" in text


# ── Blocking gaps: parking must not depend on the task ───────────────────────


def test_parks_profile_even_when_task_id_unresolvable(monkeypatch, calls) -> None:
    """Money in flight parks the profile whether or not a task is found.

    The task lookup returning "" is the least observable path there is. If
    parking rode behind it, an unresolvable task would leave the profile active
    with a transfer on its way — the double-payment case, invisible.
    """
    monkeypatch.setattr("handlers.wise_transfer._find_task_id", lambda _p: "")

    result = _run("PENDING", "processing", monkeypatch)
    _update_task(_profile(), result)

    assert ("prof_x", "status", "awaiting_settlement") in corrections(calls)
    assert status_updates(calls) == []


def test_park_failure_escalates_to_operator(monkeypatch) -> None:
    """A failed park is not a log line — both defences fail together.

    If the status correction does not land, the profile stays active AND the
    preview's in-flight warning is missing, because it reads the field the same
    failed call was meant to write. The operator's Telegram approval becomes
    the only guard, so the operator has to be told.
    """
    import subprocess as _sp

    from handlers.wise_transfer import _mark_awaiting_settlement

    class _Fail:
        returncode = 1
        stderr = "boom"
        stdout = ""

    monkeypatch.setattr(_sp, "run", lambda *a, **k: _Fail())

    escalations: list[str] = []
    monkeypatch.setattr(
        "handlers.wise_transfer._escalate", lambda msg: escalations.append(msg)
    )

    _mark_awaiting_settlement(
        _profile(), {"transfer_id": 7}, "/usr/bin/neotoma"
    )  # must not raise

    assert escalations, "a failed park must reach the operator"
    assert "7" in escalations[0]


def test_park_without_entity_id_escalates(monkeypatch) -> None:
    from handlers.wise_transfer import _mark_awaiting_settlement

    escalations: list[str] = []
    monkeypatch.setattr(
        "handlers.wise_transfer._escalate", lambda msg: escalations.append(msg)
    )
    _mark_awaiting_settlement(
        _profile(entity_id=""), {"transfer_id": 7}, "/usr/bin/neotoma"
    )
    assert escalations


# ── Consumer / surface parity ────────────────────────────────────────────────


def test_execute_gate_admits_awaiting_settlement(monkeypatch, calls) -> None:
    """Driven through execute(), not _update_task — the gate must admit it.

    execute() only calls _update_task for statuses in its gate list. If the new
    status were not admitted there, the note would never be written and the
    profile would never be parked, and every assertion made against
    _update_task directly would still pass.
    """
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.setenv("WISE_API_TOKEN", "tok-synthetic")
    _wire_wise(monkeypatch, "PENDING", "processing")

    h = WiseTransferHandler(_profile(wise_iban=IBAN, wise_recipient_name="ACME, S.L."))
    result = h.execute(h.matches([])[0])

    assert result["status"] == RESULT_AWAITING_SETTLEMENT
    assert ("prof_x", "status", "awaiting_settlement") in corrections(calls)
    assert status_updates(calls) == []


def test_format_confirmation_awaiting_settlement() -> None:
    """AC-7: neither the success nor the failure string."""
    h = WiseTransferHandler(_profile())
    text = h.format_confirmation(
        {
            "status": RESULT_AWAITING_SETTLEMENT,
            "transfer_id": 7,
            "recipient_name": "ACME, S.L.",
            "wise_transfer_status": "processing",
        }
    )
    assert "✅" not in text
    assert "❌" not in text
    assert "awaiting settlement" in text.lower()
    assert "7" in text
    assert "manual action" not in text


def test_format_confirmation_sent_and_manual_unchanged() -> None:
    """The new branch was inserted without reordering the fallthrough."""
    h = WiseTransferHandler(_profile())
    sent = h.format_confirmation({"status": "sent", "transfer_id": 7})
    assert sent.startswith("✅")
    manual = h.format_confirmation({"status": "manual_required", "error": "x"})
    assert manual.startswith("⚠️")
    failed = h.format_confirmation({"status": "failed", "error": "x"})
    assert failed.startswith("❌")


def test_result_payload_shape_is_identical_across_branches(monkeypatch) -> None:
    """A consumer reading a field on one branch must find it on the other."""
    settled = _run("COMPLETED", "outgoing_payment_sent", monkeypatch)
    unsettled = _run("PENDING", "processing", monkeypatch)
    assert set(settled) == set(unsettled)
    assert settled["status"] != unsettled["status"]
