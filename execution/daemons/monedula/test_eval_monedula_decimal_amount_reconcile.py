"""
Eval `monedula_decimal_amount_reconcile` — agent-facing regression eval for ateles#552.

WHY THIS IS A PYTEST FILE AND NOT A SCENARIO FIXTURE
----------------------------------------------------
This repo has no `tests/fixtures/agentic_eval/` tree and no
`packages/eval-harness/` (that substrate lives in the Neotoma repo), so a
`*.json` / `*.scenario.yaml` artifact here would be a file nothing executes.
The eval is therefore encoded repo-native and executable: it lives under
`execution/daemons/monedula/`, which `.github/workflows/ateles-tests.yml` runs
via `pytest execution/daemons/monedula/ -q` on every change to that path. The
scenario contract below is the eval; the assertions are its scoring.

WHAT IT DRIVES
--------------
The full recipe, end to end, faked only at the HTTP boundary
(`urllib.request.urlopen`). Both the Neotoma profile fetch and every Wise call
go through that one seam, so `load_profiles_from_neotoma()`,
`WiseTransferHandler.execute()`, `_get_wise_profile_id`,
`_get_or_create_recipient`, `_create_quote`, `_create_transfer`,
`_fund_transfer`, `_fetch_transfer`, `_reconcile_transfer_amount` and the real
Decimal JSON serializer all execute unmocked. Nothing between the profile
entity and the request bytes is stubbed, which is the point: the ateles#552
truncation happened in exactly that stretch.

SCENARIOS (each one an assertion about an observable effect, not about a
contract accepting input)
-------------------------------------------------------------------------
1. `decimal_reaches_wire`      — profile `amount_eur=133.60` reaches the Wise
                                 quote body as `Decimal("133.60")` and is sent
                                 as JSON `"sourceAmount": 133.60`, never 133.
2. `short_transfer_blocks_task`— Wise reporting `sourceValue=133` against owed
                                 133.60 fails with zero `_update_task` calls.
3. `exact_transfer_updates_once` — Wise reporting `sourceValue=133.60` returns
                                 `sent` and triggers exactly one task update.
4. `sub_cent_never_pays`       — profile `amount_eur=10.005` loads no profile
                                 and makes zero Wise calls.

Run standalone: python execution/daemons/monedula/test_eval_monedula_decimal_amount_reconcile.py
Run in CI:      pytest execution/daemons/monedula/ -q
"""

from __future__ import annotations

import json
import re
from decimal import Decimal

import pytest

from handlers import payment_profile as pp
from handlers import wise_transfer as wt
from handlers.wise_transfer import AMOUNT_MISMATCH_ERROR_CODE, WiseTransferHandler

EVAL_NAME = "monedula_decimal_amount_reconcile"

# The amount one of the two 2026-07-13 transfers actually owed, and what it
# funded instead. Keeping the live values makes the eval a regression on the
# incident rather than on a synthetic case.
OWED = "133.60"
TRUNCATED = "133"

NEOTOMA_BASE_URL = "https://neotoma.eval.invalid"
WISE_TRANSFER_ID = 7
WISE_ACCOUNT_ID = 99
WISE_PROFILE_ID = 1

# Assembled in parts so the repo's PII scanner sees no IBAN-shaped literal
# (rule pii-iban in .gitleaks.toml), matching test_wise_legal_type.py. Nothing
# here validates an IBAN — it only has to be a non-empty payee identifier.
IBAN = " ".join(["XX00", "0000", "0000", "0000", "0000", "00"])


# ---------------------------------------------------------------------------
# The single HTTP seam: Neotoma and Wise both answered from one fake
# ---------------------------------------------------------------------------


class _Response:
    """urlopen's context-manager contract, narrowed to what the callers use."""

    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeHttp:
    """Routes Neotoma + Wise requests by URL and records every one of them.

    `wise_requests` is what scenario 4 asserts on: an empty list is the proof
    that a refused profile costs zero Wise calls, which no amount of
    "the loader returned []" on its own establishes.
    """

    def __init__(
        self,
        *,
        profile_amount: object,
        transfer_source_value: str | None = None,
    ) -> None:
        self.profile_amount = profile_amount
        # What Wise claims the transfer is worth, at creation and after funding.
        # Defaults to the owed amount so a scenario only has to name a
        # divergence when it wants one.
        self.transfer_source_value = transfer_source_value or OWED
        self.neotoma_requests: list[tuple[str, str]] = []
        self.wise_requests: list[tuple[str, str, str | None]] = []
        # Bodies as the handler built them, before serialization — this is where
        # a Decimal is still a Decimal. `wise_requests` holds the same bodies as
        # wire text. Both are recorded because the two claims differ: one is
        # about the in-memory contract, the other about the bytes Wise reads.
        self.posted_bodies: list[tuple[str, dict]] = []

    # -- recording ---------------------------------------------------------

    def __call__(self, req: object, *args: object, **kwargs: object) -> _Response:
        url = req.full_url  # type: ignore[attr-defined]
        method = req.get_method()  # type: ignore[attr-defined]
        raw = getattr(req, "data", None)
        body = raw.decode() if raw else None

        if url.startswith(NEOTOMA_BASE_URL):
            self.neotoma_requests.append((method, url))
            return _Response(self._neotoma_payload())

        self.wise_requests.append((method, url, body))
        return _Response(self._wise_payload(method, url))

    def wise_body(self, path_fragment: str) -> dict:
        """The last body handed to `_wise_post` for a path, un-serialized."""
        for path, body in reversed(self.posted_bodies):
            if path_fragment in path:
                return body
        raise AssertionError(f"no Wise post body recorded for {path_fragment!r}")

    def wise_raw_body(self, path_fragment: str) -> str:
        """The exact bytes-as-text sent for that request, before any JSON parse.

        Asserting on the raw text is deliberate: `json.loads` would happily turn
        a truncated `133` into an int and a float-rounded `133.6000000000000014`
        into something that compares equal enough. The wire text is the effect.
        """
        for _method, url, body in reversed(self.wise_requests):
            if path_fragment in url and body:
                return body
        raise AssertionError(f"no Wise request body recorded for {path_fragment!r}")

    # -- payloads ----------------------------------------------------------

    def _neotoma_payload(self) -> dict:
        return {
            "entities": [
                {
                    "entity_id": "ent_eval_552",
                    "snapshot": {
                        "status": "active",
                        "label": "Evalpayee",
                        "prefix": "EVALPAYEE",
                        "calendar_keywords": ["evalsession"],
                        "payment_type": "wise",
                        "amount_eur": self.profile_amount,
                        # Carried on the profile so the recipe resolves the payee
                        # without a contacts.parquet fixture.
                        "wise_iban": IBAN,
                        "wise_recipient_name": "Eval Payee",
                        "wise_reference": "Eval ref",
                        "neotoma_task_id": "ent_eval_task",
                    },
                }
            ]
        }

    def _wise_payload(self, method: str, url: str) -> object:
        if url.endswith("/payments"):
            return {"status": "COMPLETED"}
        if "/quotes" in url:
            # Echo the requested amount so the quote check passes and the
            # scenario's divergence is isolated to the transfer record.
            return {"id": "quote-eval-1", "sourceAmount": OWED}
        if url.endswith("/v1/profiles"):
            return [{"id": WISE_PROFILE_ID, "type": "personal"}]
        if "/v1/accounts" in url:
            # Empty lookup, then a created recipient.
            return {"id": WISE_ACCOUNT_ID} if method == "POST" else []
        if re.search(r"/v1/transfers/\d+$", url):
            return {
                "id": WISE_TRANSFER_ID,
                "sourceValue": self.transfer_source_value,
            }
        if url.endswith("/v1/transfers"):
            return {
                "id": WISE_TRANSFER_ID,
                "sourceValue": self.transfer_source_value,
            }
        raise AssertionError(f"unexpected Wise URL in eval: {method} {url}")


class _TaskUpdates:
    """Counts calls into the Neotoma task-update path.

    The count, not a boolean: "exactly one" and "at least one" are different
    claims, and scenario 3 makes the stronger one.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, profile: object, result: dict) -> None:
        self.calls.append((getattr(profile, "name", "?"), result.get("status", "?")))


@pytest.fixture
def eval_env(monkeypatch):
    """Env the recipe needs, and nothing it does not."""
    monkeypatch.setenv("NEOTOMA_BASE_URL", NEOTOMA_BASE_URL)
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "eval-token")
    monkeypatch.setenv("WISE_API_TOKEN", "eval-wise-token")
    # No DATA_DIR: the payee must resolve from the profile, so a missing
    # contacts.parquet cannot be what a scenario is really testing.
    monkeypatch.delenv("DATA_DIR", raising=False)
    return monkeypatch


def _run_recipe(
    monkeypatch,
    *,
    profile_amount: object,
    transfer_source_value: str | None = None,
) -> tuple[list, dict | None, _FakeHttp, _TaskUpdates]:
    """Load profiles from Neotoma, then execute the Wise transfer for the first.

    Returns (profiles, result, http, task_updates). `result` is None when no
    profile loaded — which is itself one of the scored outcomes.
    """
    http = _FakeHttp(
        profile_amount=profile_amount,
        transfer_source_value=transfer_source_value,
    )
    updates = _TaskUpdates()
    monkeypatch.setattr("urllib.request.urlopen", http)
    monkeypatch.setattr("handlers.wise_transfer._update_task", updates)

    # A pass-through spy, not a stub: the real _wise_post still runs, so the
    # Decimal serializer and the request bytes are the production ones.
    real_wise_post = wt._wise_post

    def _spy(token: str, path: str, body: dict) -> dict:
        http.posted_bodies.append((path, body))
        return real_wise_post(token, path, body)

    monkeypatch.setattr("handlers.wise_transfer._wise_post", _spy)

    profiles = pp.load_profiles_from_neotoma()
    if not profiles:
        return profiles, None, http, updates

    result = WiseTransferHandler(profiles[0]).execute({})
    return profiles, result, http, updates


# ---------------------------------------------------------------------------
# Scenario 1 — decimal_reaches_wire
# ---------------------------------------------------------------------------


def test_eval_decimal_reaches_the_wise_quote_body(eval_env) -> None:
    """€133.60 from the profile arrives in the quote as Decimal("133.60")."""
    profiles, result, http, _updates = _run_recipe(eval_env, profile_amount=133.60)

    assert profiles[0].amount_eur == Decimal(OWED)

    quote_body = http.wise_body("/quotes")
    assert quote_body["sourceAmount"] == Decimal(OWED)
    assert quote_body["sourceAmount"] != Decimal(TRUNCATED)
    assert result is not None and result["status"] == "sent"


def test_eval_decimal_reaches_the_wire_as_json_number(eval_env) -> None:
    """The bytes Wise receives carry `"sourceAmount": 133.60`, not 133."""
    _profiles, _result, http, _updates = _run_recipe(eval_env, profile_amount=133.60)

    raw = http.wise_raw_body("/quotes")
    assert '"sourceAmount": 133.60' in raw
    assert '"sourceAmount": 133' not in raw.replace('"sourceAmount": 133.60', "")
    # A number literal, and one a strict parse agrees on.
    assert Decimal(str(json.loads(raw)["sourceAmount"])) == Decimal(OWED)


# ---------------------------------------------------------------------------
# Scenario 2 — short_transfer_blocks_task
# ---------------------------------------------------------------------------


def test_eval_short_transfer_never_reaches_the_task_update(eval_env) -> None:
    """Wise reporting €133 against €133.60 owed makes zero task updates.

    This is the half of ateles#552 that turned a €0.60 shortfall into a task
    marked done. `manual_required` would have updated the task; the mismatch
    must not take that path at all.
    """
    _profiles, result, _http, updates = _run_recipe(
        eval_env,
        profile_amount=133.60,
        transfer_source_value=TRUNCATED,
    )

    assert result is not None
    assert result["status"] == "failed"
    assert result["status"] != "manual_required"
    assert result["error_code"] == AMOUNT_MISMATCH_ERROR_CODE
    assert updates.calls == []


def test_eval_short_transfer_reports_the_shortfall(eval_env) -> None:
    """The failure names the amount, so the operator can check Wise."""
    _profiles, result, _http, _updates = _run_recipe(
        eval_env,
        profile_amount=133.60,
        transfer_source_value=TRUNCATED,
    )

    assert result is not None
    assert "0.60" in result["error"]


def test_eval_short_transfer_confirmation_does_not_claim_success(eval_env) -> None:
    """The operator-facing message says mismatch, not "sent" and not "failed"."""
    profiles, result, _http, _updates = _run_recipe(
        eval_env,
        profile_amount=133.60,
        transfer_source_value=TRUNCATED,
    )

    message = WiseTransferHandler(profiles[0]).format_confirmation(result or {})

    assert "AMOUNT MISMATCH" in message
    assert "payment sent" not in message.lower()


# ---------------------------------------------------------------------------
# Scenario 3 — exact_transfer_updates_once
# ---------------------------------------------------------------------------


def test_eval_exact_transfer_sends_and_updates_the_task_once(eval_env) -> None:
    """Reconciliation does not cost a correct payment its task update."""
    _profiles, result, _http, updates = _run_recipe(
        eval_env,
        profile_amount=133.60,
        transfer_source_value=OWED,
    )

    assert result is not None
    assert result["status"] == "sent"
    assert result["amount_eur"] == Decimal(OWED)
    assert result["transfer_id"] == WISE_TRANSFER_ID
    assert len(updates.calls) == 1
    assert updates.calls[0][1] == "sent"


def test_eval_exact_transfer_reconciles_across_exponents(eval_env) -> None:
    """Wise answering `133.6` for €133.60 owed is the same money, not a mismatch."""
    _profiles, result, _http, updates = _run_recipe(
        eval_env,
        profile_amount=133.60,
        transfer_source_value="133.6",
    )

    assert result is not None
    assert result["status"] == "sent"
    assert len(updates.calls) == 1


# ---------------------------------------------------------------------------
# Scenario 4 — sub_cent_never_pays
# ---------------------------------------------------------------------------


def test_eval_sub_cent_profile_loads_nothing_and_calls_wise_zero_times(
    eval_env,
) -> None:
    """€10.005 is refused at load, so no Wise request is ever made.

    Refusal is only meaningful if it happens before the money path opens.
    Asserting on the recorded Wise calls is what shows that; a `[]` from the
    loader alone would not.
    """
    profiles, result, http, updates = _run_recipe(eval_env, profile_amount="10.005")

    assert profiles == []
    assert result is None
    assert http.wise_requests == []
    assert updates.calls == []
    # The Neotoma fetch itself did happen — the refusal is the loader's, not a
    # transport failure masquerading as one.
    assert len(http.neotoma_requests) == 1


def test_eval_sub_cent_float_profile_also_calls_wise_zero_times(eval_env) -> None:
    """Same refusal when Neotoma sends the amount as a JSON number.

    The float shape is the one that truncated live: `int(10.005)` would have
    produced a payable 10 rather than raising the way `int("10.005")` does.
    """
    profiles, _result, http, _updates = _run_recipe(eval_env, profile_amount=10.005)

    assert profiles == []
    assert http.wise_requests == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
