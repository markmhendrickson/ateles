"""
Unit tests for payment amount precision across the Monedula money path.

Regression cover for ateles#552. `PaymentProfile.amount_eur` was typed `int`
and both loaders parsed with `int()`, so a profile for €133.60 was silently
truncated to €133 before the Wise quote was built. Two live transfers funded
short (€0.60 and €0.30) and both linked tasks were then marked `done`, so the
record asserted a complete payment that had not happened.

These tests lock three properties:

  * cents survive the load, from either source, and reach Wise unchanged
  * an amount that cannot be represented exactly is REFUSED, never rounded
  * a transfer whose reported amount diverges from the amount owed raises,
    so the done-marking path cannot be reached on a short payment

Every assertion here is effect-level: the value actually sent, or the actual
refusal, not merely that a decimal input was accepted without an exception.

Run with: pytest execution/daemons/monedula/test_payment_amount_precision.py -v
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from handlers.payment_profile import (
    PaymentAmountError,
    PaymentProfile,
    _load_profile,
    parse_amount_eur,
)
from handlers.wise_transfer import (
    AMOUNT_MISMATCH_ERROR_CODE,
    TransferAmountMismatch,
    WiseTransferHandler,
    _create_quote,
    _execute_wise_transfer,
    _json_dumps_with_decimals,
    _reconcile_transfer_amount,
)

# The two amounts that actually funded short on 2026-07-13. Using them keeps
# the regression anchored to the live failure rather than to a synthetic case.
SHORT_FUNDED_A = "133.60"
SHORT_FUNDED_B = "1153.30"

# Assembled in parts so the repo's PII scanner sees no IBAN-shaped literal
# (rule pii-iban in .gitleaks.toml), matching test_wise_legal_type.py. Nothing
# here validates an IBAN — it only has to be a non-empty payee identifier.
IBAN = " ".join(["XX00", "0000", "0000", "0000", "0000", "00"])


class _Capture:
    """Stand-in for _wise_post that records the body instead of calling Wise."""

    def __init__(self, response: dict | None = None) -> None:
        self.body: dict | None = None
        self.bodies: list[dict] = []
        self._response = response or {"id": "quote-uuid-1"}

    def __call__(self, token: str, path: str, body: dict) -> dict:
        self.body = body
        self.bodies.append(body)
        return self._response


@pytest.fixture
def capture(monkeypatch) -> _Capture:
    cap = _Capture()
    monkeypatch.setattr("handlers.wise_transfer._wise_post", cap)
    return cap


# ── Loading: cents survive, from both sources ────────────────────────────────


@pytest.mark.parametrize("raw", [SHORT_FUNDED_A, 133.60, Decimal(SHORT_FUNDED_A)])
def test_parse_amount_preserves_cents(raw) -> None:
    """A decimal amount parses exactly — no truncation, from any source type."""
    assert parse_amount_eur(raw) == Decimal(SHORT_FUNDED_A)


def test_parse_amount_does_not_truncate_to_int() -> None:
    """The specific #552 failure: €133.60 must never become €133."""
    parsed = parse_amount_eur(SHORT_FUNDED_A)
    assert parsed != Decimal("133")
    assert parsed - Decimal("133") == Decimal("0.60")


def test_env_loader_preserves_cents(monkeypatch) -> None:
    """The env loader path carries cents through to the profile."""
    monkeypatch.setenv("TESTPROF_CALENDAR_KEYWORDS", "testkw")
    monkeypatch.setenv("TESTPROF_PAYMENT_TYPE", "wise")
    monkeypatch.setenv("TESTPROF_AMOUNT_EUR", SHORT_FUNDED_B)

    profile = _load_profile("TESTPROF")

    assert profile is not None
    assert profile.amount_eur == Decimal(SHORT_FUNDED_B)
    assert profile.amount_eur != Decimal("1153")


def test_env_loader_refuses_sub_cent_amount(monkeypatch) -> None:
    """Sub-cent precision is refused: the profile does not load, so it cannot pay."""
    monkeypatch.setenv("TESTPROF_CALENDAR_KEYWORDS", "testkw")
    monkeypatch.setenv("TESTPROF_PAYMENT_TYPE", "wise")
    monkeypatch.setenv("TESTPROF_AMOUNT_EUR", "10.005")

    assert _load_profile("TESTPROF") is None


def test_env_loader_refuses_unparseable_amount(monkeypatch) -> None:
    monkeypatch.setenv("TESTPROF_CALENDAR_KEYWORDS", "testkw")
    monkeypatch.setenv("TESTPROF_PAYMENT_TYPE", "wise")
    monkeypatch.setenv("TESTPROF_AMOUNT_EUR", "not-a-number")

    assert _load_profile("TESTPROF") is None


def test_integer_amount_still_loads(monkeypatch) -> None:
    """Existing integer profiles are unaffected."""
    monkeypatch.setenv("TESTPROF_CALENDAR_KEYWORDS", "testkw")
    monkeypatch.setenv("TESTPROF_PAYMENT_TYPE", "wise")
    monkeypatch.setenv("TESTPROF_AMOUNT_EUR", "60")

    profile = _load_profile("TESTPROF")

    assert profile is not None
    assert profile.amount_eur == Decimal("60")


# ── Refusal: lossy conversion raises rather than rounding ────────────────────


@pytest.mark.parametrize("raw", ["10.005", "0.001", "1.239"])
def test_sub_cent_precision_is_refused(raw) -> None:
    with pytest.raises(PaymentAmountError):
        parse_amount_eur(raw)


@pytest.mark.parametrize("raw", ["", "abc", None, "NaN", "Infinity"])
def test_invalid_amount_is_refused(raw) -> None:
    with pytest.raises(PaymentAmountError):
        parse_amount_eur(raw)


# ── Serialization: the amount reaches Wise exactly ───────────────────────────


def test_decimal_serializes_as_exact_json_number() -> None:
    """The round-trip through JSON preserves the cents and stays a number."""
    encoded = _json_dumps_with_decimals({"sourceAmount": Decimal(SHORT_FUNDED_A)})

    # A number literal, not a quoted string.
    assert '"sourceAmount": 133.60' in encoded
    # And it survives a real JSON parse at the exact value.
    assert Decimal(str(json.loads(encoded)["sourceAmount"])) == Decimal(SHORT_FUNDED_A)


def test_serialization_does_not_go_through_float() -> None:
    """A value float cannot represent exactly still serializes exactly."""
    encoded = _json_dumps_with_decimals({"sourceAmount": Decimal("0.10")})
    assert '"sourceAmount": 0.10' in encoded


def test_create_quote_sends_untruncated_source_amount(capture) -> None:
    """Effect-level: the amount SENT to Wise equals the amount owed."""
    _create_quote("tok", 1, Decimal(SHORT_FUNDED_A))

    assert capture.body is not None
    assert capture.body["sourceAmount"] == Decimal(SHORT_FUNDED_A)
    assert capture.body["sourceAmount"] != 133


def test_create_quote_refuses_sub_cent_amount(capture) -> None:
    with pytest.raises(PaymentAmountError):
        _create_quote("tok", 1, Decimal("133.605"))
    assert capture.body is None


# ── Reconciliation: a short transfer cannot reach the done path ──────────────


def test_reconcile_passes_on_exact_match() -> None:
    _reconcile_transfer_amount(
        {"sourceValue": SHORT_FUNDED_A}, Decimal(SHORT_FUNDED_A), label="t"
    )


def test_reconcile_passes_on_equal_value_different_exponent() -> None:
    """133.6 and 133.60 are the same money."""
    _reconcile_transfer_amount({"sourceValue": "133.6"}, Decimal("133.60"), label="t")


def test_reconcile_raises_on_short_transfer() -> None:
    """The exact #552 shape: Wise reports 133, 133.60 was owed."""
    with pytest.raises(TransferAmountMismatch) as exc:
        _reconcile_transfer_amount(
            {"sourceValue": "133"}, Decimal(SHORT_FUNDED_A), label="t"
        )
    assert "0.60" in str(exc.value)


def test_reconcile_raises_when_no_amount_reported() -> None:
    """An unreconcilable response is not treated as a match."""
    with pytest.raises(TransferAmountMismatch):
        _reconcile_transfer_amount({}, Decimal(SHORT_FUNDED_A), label="t")


def test_short_transfer_blocks_the_sent_result(monkeypatch) -> None:
    """End-to-end: a truncating Wise raises, so no 'sent' result is returned.

    This is the property that matters most — `sent` is what drives the task to
    `done`. If a short transfer can still yield `sent`, #552 recurs with a
    different root cause.
    """
    monkeypatch.setattr("handlers.wise_transfer._get_wise_profile_id", lambda t: 1)
    monkeypatch.setattr(
        "handlers.wise_transfer._get_or_create_recipient",
        lambda *a, **k: 99,
    )
    monkeypatch.setattr("handlers.wise_transfer._create_quote", lambda *a, **k: "q")
    # Wise reports a truncated amount on the transfer record.
    monkeypatch.setattr(
        "handlers.wise_transfer._create_transfer",
        lambda *a, **k: (7, {"id": 7, "sourceValue": "133"}),
    )
    funded = {"called": False}

    def _never_funds(*a, **k):
        funded["called"] = True
        return {"status": "COMPLETED"}

    monkeypatch.setattr("handlers.wise_transfer._fund_transfer", _never_funds)

    with pytest.raises(TransferAmountMismatch):
        _execute_wise_transfer(
            "tok", "XX00", "Payee", Decimal(SHORT_FUNDED_A), "ref", label="t"
        )

    # Caught at creation, before the money left the balance.
    assert funded["called"] is False


def test_matching_transfer_still_completes(monkeypatch) -> None:
    """Reconciliation does not block a correct transfer."""
    monkeypatch.setattr("handlers.wise_transfer._get_wise_profile_id", lambda t: 1)
    monkeypatch.setattr(
        "handlers.wise_transfer._get_or_create_recipient", lambda *a, **k: 99
    )
    monkeypatch.setattr("handlers.wise_transfer._create_quote", lambda *a, **k: "q")
    monkeypatch.setattr(
        "handlers.wise_transfer._create_transfer",
        lambda *a, **k: (7, {"id": 7, "sourceValue": SHORT_FUNDED_A}),
    )
    monkeypatch.setattr(
        "handlers.wise_transfer._fund_transfer",
        lambda *a, **k: {"status": "COMPLETED"},
    )
    monkeypatch.setattr(
        "handlers.wise_transfer._fetch_transfer",
        lambda *a, **k: {"id": 7, "sourceValue": SHORT_FUNDED_A},
    )

    result = _execute_wise_transfer(
        "tok", "XX00", "Payee", Decimal(SHORT_FUNDED_A), "ref", label="t"
    )

    assert result["status"] == "sent"
    assert result["amount_eur"] == Decimal(SHORT_FUNDED_A)


def test_post_funding_mismatch_still_raises(monkeypatch) -> None:
    """A transfer that reads correct at creation but short after funding raises.

    Money has already moved here — the point is that the result never becomes
    `sent`, so the caller cannot mark the task done on a short payment.
    """
    monkeypatch.setattr("handlers.wise_transfer._get_wise_profile_id", lambda t: 1)
    monkeypatch.setattr(
        "handlers.wise_transfer._get_or_create_recipient", lambda *a, **k: 99
    )
    monkeypatch.setattr("handlers.wise_transfer._create_quote", lambda *a, **k: "q")
    monkeypatch.setattr(
        "handlers.wise_transfer._create_transfer",
        lambda *a, **k: (7, {"id": 7, "sourceValue": SHORT_FUNDED_A}),
    )
    monkeypatch.setattr(
        "handlers.wise_transfer._fund_transfer",
        lambda *a, **k: {"status": "COMPLETED"},
    )
    monkeypatch.setattr(
        "handlers.wise_transfer._fetch_transfer",
        lambda *a, **k: {"id": 7, "sourceValue": "133"},
    )

    with pytest.raises(TransferAmountMismatch):
        _execute_wise_transfer(
            "tok", "XX00", "Payee", Decimal(SHORT_FUNDED_A), "ref", label="t"
        )


# ── Handler: a mismatch never reaches the task-update path ───────────────────
#
# The tests above stop at _execute_wise_transfer, which only proves that no
# `sent` result is produced. WiseTransferHandler.execute() is where the
# exception is turned into a status, and `manual_required` also calls
# _update_task() — which writes "Payment sent …" onto the task and rolls its
# due_date. So "no `sent`" is not sufficient: the handler-level status is what
# decides whether a short payment gets recorded as handled.


def _handler_profile(**overrides) -> PaymentProfile:
    """A wise profile whose payee resolves from the profile, not from parquet."""
    fields = {
        "prefix": "HANDLERTEST",
        "label": "Handlertest",
        "calendar_keywords": ["handlerkw"],
        "payment_type": "wise",
        "amount_eur": Decimal(SHORT_FUNDED_A),
        "wise_iban": IBAN,
        "wise_recipient_name": "Handler Payee",
        "wise_reference": "Handler ref",
        "neotoma_task_id": "ent_handler_task",
    }
    fields.update(overrides)
    return PaymentProfile(**fields)  # type: ignore[arg-type]


@pytest.fixture
def handler_env(monkeypatch):
    monkeypatch.setenv("WISE_API_TOKEN", "test-wise-token")
    monkeypatch.delenv("DATA_DIR", raising=False)
    return monkeypatch


@pytest.fixture
def task_updates(monkeypatch) -> list[dict]:
    """Records every _update_task call so tests can assert on the count."""
    calls: list[dict] = []
    monkeypatch.setattr(
        "handlers.wise_transfer._update_task",
        lambda profile, result: calls.append(result),
    )
    return calls


def test_handler_mismatch_returns_failed_and_skips_task_update(
    handler_env, task_updates
) -> None:
    """A mismatch yields status='failed' with a stable code and no task update."""
    handler_env.setattr(
        "handlers.wise_transfer._execute_wise_transfer",
        lambda *a, **k: (_ for _ in ()).throw(
            TransferAmountMismatch("Wise reports €133 but €133.60 was owed")
        ),
    )

    result = WiseTransferHandler(_handler_profile()).execute({})

    assert result["status"] == "failed"
    assert result["status"] != "manual_required"
    assert result["error_code"] == AMOUNT_MISMATCH_ERROR_CODE
    assert task_updates == []


def test_handler_mismatch_confirmation_flags_the_moved_money(
    handler_env, task_updates
) -> None:
    """The operator message must not read as 'nothing happened, retry'."""
    handler_env.setattr(
        "handlers.wise_transfer._execute_wise_transfer",
        lambda *a, **k: (_ for _ in ()).throw(
            TransferAmountMismatch("Wise reports €133 but €133.60 was owed")
        ),
    )
    handler = WiseTransferHandler(_handler_profile())

    message = handler.format_confirmation(handler.execute({}))

    assert "AMOUNT MISMATCH" in message
    assert "payment sent" not in message.lower()


def test_handler_other_failure_still_requires_manual_action(
    handler_env, task_updates
) -> None:
    """Only the mismatch is special-cased — every other error keeps its behaviour.

    A network or auth failure means the money did NOT move, so the task update
    (which notes the attempt and rolls the schedule) is still correct there.
    """
    handler_env.setattr(
        "handlers.wise_transfer._execute_wise_transfer",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Wise API 503")),
    )

    result = WiseTransferHandler(_handler_profile()).execute({})

    assert result["status"] == "manual_required"
    assert "error_code" not in result
    assert len(task_updates) == 1


def test_handler_sent_result_updates_the_task_once(handler_env, task_updates) -> None:
    """A reconciled transfer still gets exactly one task update."""
    handler_env.setattr(
        "handlers.wise_transfer._execute_wise_transfer",
        lambda *a, **k: {
            "status": "sent",
            "transfer_id": 7,
            "amount_eur": Decimal(SHORT_FUNDED_A),
        },
    )

    result = WiseTransferHandler(_handler_profile()).execute({})

    assert result["status"] == "sent"
    assert len(task_updates) == 1
    assert task_updates[0]["amount_eur"] == Decimal(SHORT_FUNDED_A)


def test_handler_passes_the_untruncated_amount_to_wise(
    handler_env, task_updates
) -> None:
    """Effect-level at the handler boundary: cents survive execute()."""
    seen: dict = {}

    def _record(token, iban, name, amount_eur, reference, **kwargs):
        seen["amount_eur"] = amount_eur
        return {"status": "sent", "transfer_id": 7, "amount_eur": amount_eur}

    handler_env.setattr("handlers.wise_transfer._execute_wise_transfer", _record)

    WiseTransferHandler(_handler_profile(amount_eur=Decimal(SHORT_FUNDED_B))).execute(
        {}
    )

    assert seen["amount_eur"] == Decimal(SHORT_FUNDED_B)
    assert seen["amount_eur"] != Decimal("1153")


# ── Profile dataclass carries Decimal end to end ─────────────────────────────


def test_profile_amount_reaches_wise_unchanged(capture) -> None:
    """Cross-surface: a profile's amount arrives at the Wise body intact."""
    profile = PaymentProfile(
        prefix="X",
        label="X",
        calendar_keywords=["x"],
        payment_type="wise",
        amount_eur=Decimal(SHORT_FUNDED_B),
    )

    _create_quote("tok", 1, profile.amount_eur)

    assert capture.body is not None
    assert capture.body["sourceAmount"] == Decimal(SHORT_FUNDED_B)


def test_btc_prompt_renders_amount_as_clean_number() -> None:
    """The BTC path interpolates the amount into a JSON literal in a prompt.

    A Decimal that rendered as "Decimal('60.50')" would produce an unparseable
    prompt, so the rendering is asserted rather than assumed.
    """
    from handlers.btc_transfer import _build_claude_prompt

    profile = PaymentProfile(
        prefix="X",
        label="X",
        calendar_keywords=["x"],
        payment_type="btc",
        amount_eur=Decimal("60.50"),
        btc_address="bc1qexampleaddressnotreal",
    )

    prompt = _build_claude_prompt(profile)

    assert '"amount_eur": 60.50' in prompt
    assert "Decimal" not in prompt


# ── The Neotoma loader: the path the two live transfers actually took ────────


class _FakeResponse:
    """Minimal stand-in for the urlopen context manager."""

    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc) -> None:
        return None


def _neotoma_payload(amount) -> dict:
    return {
        "entities": [
            {
                "entity_id": "ent_test",
                "snapshot": {
                    "status": "active",
                    "label": "Testpayee",
                    "prefix": "TESTPAYEE",
                    "calendar_keywords": ["testkw"],
                    "payment_type": "wise",
                    "amount_eur": amount,
                },
            }
        ]
    }


@pytest.fixture
def neotoma_env(monkeypatch):
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example")
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "test-token")
    return monkeypatch


def test_neotoma_loader_preserves_cents(neotoma_env) -> None:
    """The live path from ateles#552.

    The API returns amount_eur as a JSON number, so the old `int(amount_raw)`
    received the float 133.6 and floored it to 133 without raising — which is
    why this truncated silently in production rather than being rejected the
    way a string "133.60" would have been.
    """
    from handlers import payment_profile as pp

    neotoma_env.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: _FakeResponse(_neotoma_payload(133.60)),
    )

    profiles = pp.load_profiles_from_neotoma()

    assert len(profiles) == 1
    assert profiles[0].amount_eur == Decimal(SHORT_FUNDED_A)
    assert profiles[0].amount_eur != Decimal("133")


def test_neotoma_loader_preserves_cents_from_string(neotoma_env) -> None:
    """A string-typed amount loads to the same exact value."""
    from handlers import payment_profile as pp

    neotoma_env.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: _FakeResponse(_neotoma_payload(SHORT_FUNDED_B)),
    )

    profiles = pp.load_profiles_from_neotoma()

    assert len(profiles) == 1
    assert profiles[0].amount_eur == Decimal(SHORT_FUNDED_B)


def test_neotoma_loader_refuses_sub_cent_amount(neotoma_env) -> None:
    """A sub-cent amount yields no profile at all, so no payment can fire."""
    from handlers import payment_profile as pp

    neotoma_env.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: _FakeResponse(_neotoma_payload("10.005")),
    )

    assert pp.load_profiles_from_neotoma() == []


def test_neotoma_amount_reaches_wise_untruncated(neotoma_env, capture) -> None:
    """Full path: Neotoma load → quote body, cents intact at both ends."""
    from handlers import payment_profile as pp

    neotoma_env.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: _FakeResponse(_neotoma_payload(133.60)),
    )

    profile = pp.load_profiles_from_neotoma()[0]
    _create_quote("tok", 1, profile.amount_eur)

    assert capture.body is not None
    assert capture.body["sourceAmount"] == Decimal(SHORT_FUNDED_A)
    # And it serializes to the wire as the exact amount owed.
    assert '"sourceAmount": 133.60' in _json_dumps_with_decimals(capture.body)


# ── Quote reconciliation: a mispriced quote never becomes a transfer ─────────


def test_quote_mismatch_is_refused(monkeypatch) -> None:
    """Wise pricing a different amount than requested stops the flow."""
    monkeypatch.setattr(
        "handlers.wise_transfer._wise_post",
        lambda t, p, b: {"id": "q", "sourceAmount": 133},
    )

    with pytest.raises(TransferAmountMismatch):
        _create_quote("tok", 1, Decimal(SHORT_FUNDED_A))


def test_quote_echo_matching_is_accepted(monkeypatch) -> None:
    monkeypatch.setattr(
        "handlers.wise_transfer._wise_post",
        lambda t, p, b: {"id": "q", "sourceAmount": 133.60},
    )

    assert _create_quote("tok", 1, Decimal(SHORT_FUNDED_A)) == "q"


def test_quote_without_echo_still_proceeds(monkeypatch) -> None:
    """A quote that echoes no amount is left to the transfer-level check."""
    monkeypatch.setattr(
        "handlers.wise_transfer._wise_post", lambda t, p, b: {"id": "q"}
    )

    assert _create_quote("tok", 1, Decimal(SHORT_FUNDED_A)) == "q"
