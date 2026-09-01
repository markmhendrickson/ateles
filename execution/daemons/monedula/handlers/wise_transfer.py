"""
handlers/wise_transfer.py — Generic Wise transfer handler for Monedula.

Executes a Wise IBAN transfer for any PaymentProfile with payment_type="wise".
Contact (name + IBAN) is loaded from contacts.parquet using the profile's
contact_id prefix and category/platform fallback — all driven by env vars,
no business-specific values hardcoded here.

Wise API flow:
  1. GET /v1/profiles → pick personal profile_id
  2. POST /v3/profiles/{profile_id}/quotes → get quote_uuid
  3. POST /v1/accounts → get or create recipient account (IBAN)
  4. POST /v1/transfers → create transfer with quote_uuid + target_account_id
  5. POST /v3/profiles/{profile_id}/transfers/{transfer_id}/payments → fund

On any Wise step failure, returns status="manual_required" with full payment
details so the operator can execute manually.

The one exception is an amount mismatch (TransferAmountMismatch), which returns
status="failed" instead. "manual_required" triggers a Neotoma task update, and
on a mismatch the money may already have moved for the wrong amount — updating
the task there is the half of ateles#552 that made the short transfers
invisible.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

try:
    from ..handler_base import PaymentHandler
except ImportError:
    from handler_base import PaymentHandler  # type: ignore[no-redef]
from .payment_profile import (
    PROFILE_STATUS_AWAITING_SETTLEMENT,
    PaymentAmountError,
    PaymentProfile,
    parse_amount_eur,
)

log = logging.getLogger(__name__)

WISE_BASE_URL = "https://api.transferwise.com"

# Stable machine-readable code for the amount-mismatch outcome. Callers and
# alerting match on this rather than on the human-readable error text, which is
# free to change.
AMOUNT_MISMATCH_ERROR_CODE = "wise_transfer_amount_mismatch"

# ---------------------------------------------------------------------------
# Settlement state (ateles#575)
# ---------------------------------------------------------------------------
#
# Funding a Wise transfer and Wise delivering it are two different events,
# minutes to days apart, and the funding response describes only the first.
#
# _fund_transfer() POSTs to /v3/profiles/{id}/transfers/{id}/payments and reads
# `status` off the PAYMENT object. COMPLETED there means "the money left the
# balance", not "the transfer settled". PENDING and PROCESSING mean not even
# that. Before ateles#575 all three collapsed into result status "sent", which
# drove the task to done and archived the profile.
#
# So the funding status is not the settlement signal for ANY of its values, and
# a fix that merely stopped honouring PENDING/PROCESSING would leave the same
# defect on the COMPLETED path. The operator's own hand-run ledger records the
# case directly: a transfer funded with payment status COMPLETED and sat at
# transfer status "processing" afterwards.
#
# Settlement is therefore decided by the TRANSFER record's own status, read
# back from Wise; the funding status only distinguishes "accepted" from "the
# call failed outright".
FUNDING_ACCEPTED = frozenset({"COMPLETED", "PENDING", "PROCESSING"})

# Wise's own transfer-record statuses (lowercase on the wire). This is the
# settlement signal.
#
# Only outgoing_payment_sent is settled. Everything not explicitly named in one
# of these two sets — including "unknown" — is treated as still in flight, so
# the classifier fails towards "keep watching" rather than towards "delivered".
#
# A "failed" classification is never acted on from a single read. The operator's
# hand-run ledger records bounced_back appearing ~20s after funding and
# resolving back to processing on the next poll, so a one-read verdict would
# have declared a healthy transfer dead. The sweep requires the same failed
# status on two consecutive observations before it records a terminal outcome.
WISE_TRANSFER_SETTLED = frozenset({"outgoing_payment_sent"})
WISE_TRANSFER_FAILED = frozenset(
    {"cancelled", "funds_refunded", "bounced_back", "charged_back"}
)

# Handler result status for a transfer submitted but not yet delivered. It is
# NOT a failure and NOT a success: the money has left the balance, so it must
# not be retried, and it has not arrived, so the task must not be closed.
RESULT_AWAITING_SETTLEMENT = "awaiting_settlement"

# How long an unsettled transfer may sit before the sweep reports it as suspect
# rather than merely in flight. Wise itself can take minutes to days.
DEFAULT_SETTLEMENT_ALERT_DAYS = 5


def settlement_alert_days() -> int:
    """Days before an in-flight transfer is reported suspect.

    Read at call time so tests and the operator can set it per run. An
    unparseable value falls back to the default with a warning — a bad env var
    must not decide how long money stays unwatched.
    """
    raw = os.environ.get("MONEDULA_SETTLEMENT_ALERT_DAYS", "").strip()
    if not raw:
        return DEFAULT_SETTLEMENT_ALERT_DAYS
    try:
        value = int(raw)
    except ValueError:
        log.warning(
            f"MONEDULA_SETTLEMENT_ALERT_DAYS={raw!r} is not an integer — "
            f"using {DEFAULT_SETTLEMENT_ALERT_DAYS}"
        )
        return DEFAULT_SETTLEMENT_ALERT_DAYS
    if value < 0:
        log.warning(
            f"MONEDULA_SETTLEMENT_ALERT_DAYS={raw!r} is negative — "
            f"using {DEFAULT_SETTLEMENT_ALERT_DAYS}"
        )
        return DEFAULT_SETTLEMENT_ALERT_DAYS
    return value


def classify_transfer_state(transfer: dict | None) -> str:
    """Classify a Wise transfer record: settled | failed | in_flight | unreadable.

    "unreadable" is deliberately distinct from "in_flight": a Wise read that
    failed says nothing about the transfer, and conflating the two would let a
    network error look like a state observation. Neither ever classifies as
    settled — only an explicit WISE_TRANSFER_SETTLED status does. A transfer is
    declared delivered because Wise said so, never because nothing said
    otherwise.
    """
    if not isinstance(transfer, dict):
        return "unreadable"
    raw = transfer.get("status")
    if raw is None:
        return "unreadable"
    status = str(raw).strip().lower()
    if status in WISE_TRANSFER_SETTLED:
        return "settled"
    if status in WISE_TRANSFER_FAILED:
        return "failed"
    return "in_flight"


class TransferAmountMismatch(RuntimeError):
    """Wise's own record of a transfer does not match the amount owed.

    Raised at the quote, at transfer creation, and again after funding. The
    post-funding case means the money has already moved, so it is raised rather
    than logged: the caller's next step is to mark the task done, and a task
    marked done is the record that the payment was correct and complete. A
    short transfer that is also marked done is invisible — which is how
    ateles#552 stayed undetected.

    WiseTransferHandler.execute() maps this to status="failed" with
    error_code=AMOUNT_MISMATCH_ERROR_CODE, deliberately outside the set of
    statuses that trigger a Neotoma task update, so the task keeps its original
    due_date and no note claims a payment that did not happen as specified.
    """


class WiseTransferHandler(PaymentHandler):
    """Generic Wise transfer handler parameterised by a PaymentProfile."""

    def __init__(self, profile: PaymentProfile) -> None:
        self.profile = profile

    @property
    def name(self) -> str:
        return self.profile.name

    def matches(self, events: list[dict]) -> list[dict]:
        """Return matches for this profile.

        Two trigger kinds, deliberately kept separate:

        * Recurring (calendar_keywords): a calendar event whose title matches.
          These are ATTENDANCE-GATED — the event is not proof the session
          happened, so approval must name the handler (see _parse_reply).
        * One-off (due_date): an invoice due today or overdue. There is no
          session to attend, so there is no calendar event to match against.

        A one-off never matches on a calendar event, and a recurring profile
        never matches on a date — the two paths do not interact.
        """
        # Calendar keywords win: a profile with them is recurring and therefore
        # attendance-gated, even if it also carries a due_date. Only a profile
        # with NO keywords is a one-off that may fire on a date alone — this is
        # what stops a stray due_date from bypassing the attendance gate.
        if not self.profile.calendar_keywords and self.profile.due_date:
            return _due_date_matches(self.profile, self.name)

        matched = []
        for event in events:
            summary = event.get("summary", "") or ""
            low = summary.lower()
            if any(kw in low for kw in self.profile.calendar_keywords):
                log.info(f"[{self.name}] Matched event: {summary!r}")
                matched.append({"event": event, "summary": summary})
        return matched

    def preview(self, match: dict) -> str:
        summary = match.get("summary", self.profile.label)
        if match.get("trigger") == "due_date":
            summary = f"due {match.get('due_date', '?')} (one-off invoice)"
        contact = _load_contact(self.profile)
        name = contact.get("name", "[recipient]") if contact else "[recipient]"
        iban = contact.get("iban", "…") if contact else "…"
        task_id = _find_task_id(self.profile)
        iban_preview = (iban[:10] + "…") if len(iban) > 10 else iban
        lines = [
            f"💳 {self.profile.label}",
            f"  €{self.profile.amount_eur} Wise → {name} (IBAN: {iban_preview})",
            f"  Task: {task_id or '(unknown)'}",
            f"  Event: {summary}",
        ]
        # Defence in depth for the double-payment guard: a parked profile
        # should never reach a preview at all, because the loader matches
        # active profiles only. If one does, the operator must see that money
        # is already in flight before approving a second transfer.
        if self.profile.pending_transfer_id:
            submitted = self.profile.pending_transfer_at or "an earlier run"
            lines.append(
                f"  ⚠️ ALREADY IN FLIGHT: Wise transfer "
                f"{self.profile.pending_transfer_id} submitted {submitted} and not "
                f"yet settled — do NOT approve this again."
            )
        return "\n".join(lines)

    def execute(self, match: dict) -> dict[str, Any]:
        """Execute Wise transfer. Returns result dict with status and details."""
        log.info(f"[{self.name}] Executing Wise payment...")

        contact = _load_contact(self.profile)
        if not contact:
            return {
                "status": "manual_required",
                "handler": self.name,
                "error": "Could not load contact",
                "amount_eur": self.profile.amount_eur,
                "reference": self.profile.wise_reference,
            }

        iban = contact.get("iban", "")
        recipient_name = contact.get("name", "")

        if not iban:
            return {
                "status": "manual_required",
                "handler": self.name,
                "error": "No IBAN found in contact",
                "amount_eur": self.profile.amount_eur,
                "reference": self.profile.wise_reference,
                "recipient_name": recipient_name,
            }

        token = os.environ.get("WISE_API_TOKEN", "").strip()
        if not token:
            log.error(f"[{self.name}] WISE_API_TOKEN not set")
            return {
                "status": "manual_required",
                "handler": self.name,
                "error": "WISE_API_TOKEN not set",
                "amount_eur": self.profile.amount_eur,
                "iban": iban,
                "recipient_name": recipient_name,
                "reference": self.profile.wise_reference,
            }

        try:
            result = _execute_wise_transfer(
                token,
                iban,
                recipient_name,
                self.profile.amount_eur,
                self.profile.wise_reference,
                label=self.name,
                legal_type=self.profile.wise_legal_type,
            )
        except TransferAmountMismatch as exc:
            # Handled apart from every other failure because `manual_required`
            # reaches _update_task(), and _update_task() writes "Payment sent
            # <date>: €<amount>" onto the task and rolls its due_date to the
            # next occurrence. On an amount mismatch the money HAS moved, for
            # the wrong amount — so that note is a false record and the rolled
            # due_date retires a payment that was never made correctly. That
            # combination is ateles#552. `failed` is outside the update set, so
            # the task keeps its original due_date and no note claims success.
            log.error(f"[{self.name}] AMOUNT MISMATCH — task NOT updated: {exc}")
            result = {
                "status": "failed",
                "handler": self.name,
                "error_code": AMOUNT_MISMATCH_ERROR_CODE,
                "error": str(exc),
                "amount_eur": self.profile.amount_eur,
                "iban": iban,
                "recipient_name": recipient_name,
                "reference": self.profile.wise_reference,
            }
        except Exception as exc:
            log.error(f"[{self.name}] Wise transfer exception: {exc}")
            result = {
                "status": "manual_required",
                "handler": self.name,
                "error": str(exc),
                "amount_eur": self.profile.amount_eur,
                "iban": iban,
                "recipient_name": recipient_name,
                "reference": self.profile.wise_reference,
            }

        result["handler"] = self.name

        if result.get("status") in (
            "sent",
            RESULT_AWAITING_SETTLEMENT,
            "manual_required",
        ):
            _update_task(self.profile, result)

        return result

    def format_confirmation(self, result: dict) -> str:
        status = result.get("status")
        amount = self.profile.amount_eur
        reference = self.profile.wise_reference
        if status == "sent":
            transfer_id = result.get("transfer_id", "unknown")
            name = result.get("recipient_name", "recipient")
            return (
                f"✅ {self.profile.label} payment sent via Wise!\n"
                f"  Transfer ID: {transfer_id}\n"
                f"  Recipient: {name}\n"
                f"  Amount: €{amount}\n"
                f"  Reference: {reference}"
            )
        elif status == RESULT_AWAITING_SETTLEMENT:
            # Neither ✅ nor ❌: the money left the balance and Wise has not
            # confirmed delivery. Saying "sent" here is the ateles#575 defect
            # in prose, and saying "failed" would invite a re-send of a
            # transfer that is still on its way.
            transfer_id = result.get("transfer_id", "unknown")
            name = result.get("recipient_name", "recipient")
            wise_state = result.get("wise_transfer_status") or "in flight"
            return (
                f"⏳ {self.profile.label} transfer submitted — awaiting settlement.\n"
                f"  Transfer ID: {transfer_id}\n"
                f"  Recipient: {name}\n"
                f"  Amount: €{amount}\n"
                f"  Reference: {reference}\n"
                f"  Wise transfer status: {wise_state}\n"
                f"  The task stays open until Wise confirms delivery. "
                f"Do not re-send this payment."
            )
        elif status == "manual_required":
            iban = result.get("iban", "see contacts")
            name = result.get("recipient_name", "recipient")
            error = result.get("error", "")
            return (
                f"⚠️ {self.profile.label} payment requires manual action.\n"
                f"  Error: {error}\n\n"
                f"  Manual payment details:\n"
                f"  Recipient: {name}\n"
                f"  IBAN: {iban}\n"
                f"  Amount: €{amount}\n"
                f"  Reference: {reference}"
            )
        elif result.get("error_code") == AMOUNT_MISMATCH_ERROR_CODE:
            # A plain "payment failed" would read as "nothing happened", and the
            # operator would retry. The money may already have left for the
            # wrong amount, so say what the actual state is before they do.
            return (
                f"🚨 {self.profile.label} payment AMOUNT MISMATCH — "
                f"task NOT marked complete.\n"
                f"  {result.get('error', 'amount mismatch')}\n"
                f"  Owed: €{amount}\n"
                f"  Reference: {reference}\n"
                f"  Check Wise for a partial transfer before retrying."
            )
        else:
            error = result.get("error", "unknown error")
            return f"❌ {self.profile.label} payment failed: {error}"


# ---------------------------------------------------------------------------
# One-off due-date trigger
# ---------------------------------------------------------------------------


def _due_date_matches(profile: PaymentProfile, name: str) -> list[dict]:
    """Match a one-off profile whose due_date is today or already past.

    Returns at most one match. A malformed due_date does NOT fire the payment —
    it logs and returns nothing, so a typo can never cause an unintended
    transfer.
    """
    raw = (profile.due_date or "").strip()
    try:
        due = date.fromisoformat(raw)
    except ValueError:
        log.warning(
            f"[{name}] invalid due_date {raw!r} (expected ISO YYYY-MM-DD) — not matching"
        )
        return []

    today = date.today()
    if due > today:
        log.info(f"[{name}] due {due.isoformat()}, not yet due — skipping.")
        return []

    overdue_days = (today - due).days
    log.info(
        f"[{name}] one-off payment due {due.isoformat()}"
        + (f" ({overdue_days}d overdue)" if overdue_days else " (today)")
    )
    return [
        {
            "trigger": "due_date",
            "due_date": due.isoformat(),
            "overdue_days": overdue_days,
            "summary": profile.label,
        }
    ]


# ---------------------------------------------------------------------------
# Contact loading (from contacts.parquet, generic)
# ---------------------------------------------------------------------------


def _load_contact(profile: PaymentProfile) -> dict | None:
    """
    Resolve the payee for this profile.

    Order of precedence:
      1. The profile's own wise_iban / wise_recipient_name. A one-off invoice
         payee (a law firm, a supplier) is not a standing contact, so the
         profile carries the details directly rather than requiring a
         contacts.parquet row.
      2. contacts.parquet, by contact_id prefix then category+platform.

    Returns a dict with at least 'name' and 'iban', or None on failure.
    """
    iban_on_profile = (getattr(profile, "wise_iban", "") or "").strip()
    name_on_profile = (getattr(profile, "wise_recipient_name", "") or "").strip()
    if iban_on_profile and name_on_profile:
        log.info(f"[{profile.name}] payee resolved from profile (no contacts lookup)")
        return {"name": name_on_profile, "iban": iban_on_profile}
    if iban_on_profile or name_on_profile:
        log.warning(
            f"[{profile.name}] profile carries only one of wise_iban/"
            f"wise_recipient_name — both are required; falling back to contacts"
        )

    data_dir = os.environ.get("DATA_DIR", "").strip()
    if not data_dir:
        log.warning(f"[{profile.name}] DATA_DIR not set — cannot load contacts")
        return None

    contacts_path = Path(data_dir) / "contacts" / "contacts.parquet"
    if not contacts_path.exists():
        log.warning(f"[{profile.name}] contacts.parquet not found at {contacts_path}")
        return None

    try:
        import pyarrow.parquet as pq

        table = pq.read_table(str(contacts_path))
        df = table.to_pydict()
        n = len(next(iter(df.values())))
        rows = [{k: df[k][i] for k in df} for i in range(n)]
        return _find_contact_in_rows(rows, profile)
    except ImportError:
        return _load_contact_pandas(contacts_path, profile)
    except Exception as exc:
        log.error(f"[{profile.name}] Error loading contacts: {exc}")
        return None


def _load_contact_pandas(contacts_path: Path, profile: PaymentProfile) -> dict | None:
    """Fallback contact loader using pandas."""
    try:
        import pandas as pd

        df = pd.read_parquet(str(contacts_path))
        rows = df.to_dict(orient="records")
        return _find_contact_in_rows(rows, profile)
    except Exception as exc:
        log.error(f"[{profile.name}] Pandas contact load error: {exc}")
        return None


def _find_contact_in_rows(rows: list[dict], profile: PaymentProfile) -> dict | None:
    """Find a matching contact row using profile's contact_id prefix or category/platform."""
    # Primary: match by contact_id prefix
    if profile.contact_id:
        for row in rows:
            cid = str(row.get("contact_id") or row.get("id") or "")
            if cid.startswith(profile.contact_id):
                return _normalize_contact(row)

    # Fallback: category + platform
    if profile.contact_category or profile.contact_platform:
        for row in rows:
            cat = str(row.get("category") or "").lower()
            plat = str(row.get("platform") or "").lower()
            cat_match = (not profile.contact_category) or (
                cat == profile.contact_category.lower()
            )
            plat_match = (not profile.contact_platform) or (
                plat == profile.contact_platform.lower()
            )
            if cat_match and plat_match:
                return _normalize_contact(row)

    log.warning(f"[{profile.name}] Contact not found in contacts.parquet")
    return None


def _normalize_contact(row: dict) -> dict:
    """Extract name and IBAN from a contact row (handles varied column names)."""
    name = row.get("name") or row.get("full_name") or row.get("display_name") or ""
    iban = (
        row.get("iban") or row.get("bank_account") or row.get("payment_details") or ""
    )
    phone = row.get("phone") or row.get("phone_number") or ""
    return {"name": str(name), "iban": str(iban), "phone": str(phone), **row}


# ---------------------------------------------------------------------------
# Wise API (generic — no profile-specific logic)
# ---------------------------------------------------------------------------


def _wise_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _wise_get(token: str, path: str) -> Any:
    import urllib.request

    url = f"{WISE_BASE_URL}{path}"
    req = urllib.request.Request(url, headers=_wise_headers(token))
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _json_dumps_with_decimals(body: dict) -> str:
    """json.dumps(body), rendering any Decimal as an exact JSON number.

    json.dumps() cannot encode a Decimal at all, and float(Decimal("133.60"))
    is the binary approximation that Decimal exists to avoid. Rendering the
    Decimal's own digits keeps the amount Wise receives identical to the
    amount the profile declares: 133.60 stays 133.60 all the way to the wire.
    """

    def _default(value: Any) -> Any:
        raise TypeError(f"cannot encode {type(value).__name__} for Wise")

    def _encode(value: Any) -> str:
        if isinstance(value, Decimal):
            # format(..., "f") avoids scientific notation for any EUR amount.
            return format(value, "f")
        if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
            return json.dumps(value)
        if isinstance(value, str):
            return json.dumps(value)
        if isinstance(value, dict):
            inner = ", ".join(
                f"{json.dumps(str(k))}: {_encode(v)}" for k, v in value.items()
            )
            return "{" + inner + "}"
        if isinstance(value, (list, tuple)):
            return "[" + ", ".join(_encode(v) for v in value) + "]"
        return json.dumps(value, default=_default)

    return _encode(body)


def _wise_post(token: str, path: str, body: dict) -> dict:
    import urllib.error
    import urllib.request

    url = f"{WISE_BASE_URL}{path}"
    data = _json_dumps_with_decimals(body).encode()
    req = urllib.request.Request(
        url, data=data, headers=_wise_headers(token), method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body_bytes = exc.read()
        raise RuntimeError(
            f"Wise API {path} HTTP {exc.code}: {body_bytes.decode()[:400]}"
        ) from exc


def _get_wise_profile_id(token: str) -> int:
    profiles = _wise_get(token, "/v1/profiles")
    for profile in profiles:
        if profile.get("type") == "personal":
            return profile["id"]
    if profiles:
        return profiles[0]["id"]
    raise RuntimeError("No Wise profiles found")


def _normalize_iban(iban: str) -> str:
    """IBANs compare without spacing or case — 'ES62 2100' == 'es622100'."""
    return "".join((iban or "").split()).upper()


def _find_existing_recipient(token: str, profile_id: int, iban: str) -> int | None:
    """Return an existing recipient account id for this IBAN, or None.

    Matching is by IBAN, not by name. The IBAN is the account's identity; the
    account-holder name is a label that varies in spacing, accents and
    abbreviation between what a payee tells you and what their bank stores.

    A lookup failure is not fatal: the caller falls back to creating the
    recipient, which is the pre-existing behaviour.
    """
    try:
        accounts = _wise_get(token, f"/v1/accounts?profile={profile_id}&currency=EUR")
    except Exception as exc:  # network, auth, schema drift — all non-fatal here
        log.warning(f"Wise recipient lookup failed ({exc}) — will attempt create")
        return None

    if isinstance(accounts, dict):
        accounts = accounts.get("content") or accounts.get("accounts") or []
    if not isinstance(accounts, list):
        log.warning(f"Wise /v1/accounts returned unexpected shape: {type(accounts)}")
        return None

    want = _normalize_iban(iban)
    for acct in accounts:
        if not isinstance(acct, dict):
            continue
        details = acct.get("details") or {}
        if _normalize_iban(str(details.get("iban") or "")) == want:
            account_id = acct.get("id")
            if account_id:
                log.info(f"Reusing existing Wise recipient {account_id} for this IBAN")
                return int(account_id)
    return None


def _get_or_create_recipient(
    token: str,
    profile_id: int,
    iban: str,
    name: str,
    legal_type: str = "PRIVATE",
) -> int:
    """Return an existing Wise IBAN recipient for this IBAN, or create one.

    Looks up first. Creating unconditionally would add a duplicate recipient
    every run for a payee that already exists — and recipient creation is the
    step Wise runs Verification of Payee against, so a needless create is also
    a needless chance to be blocked by a name mismatch on an account that is
    already known-good.

    legal_type must be PRIVATE (individual) or BUSINESS (company). Sending
    PRIVATE for a company-held account risks the transfer being returned on a
    name/legal-type mismatch — after the funds have already left the balance.
    """
    existing = _find_existing_recipient(token, profile_id, iban)
    if existing is not None:
        return existing

    legal_type = (legal_type or "PRIVATE").strip().upper()
    if legal_type not in ("PRIVATE", "BUSINESS"):
        raise ValueError(
            f"Invalid Wise legalType {legal_type!r} — expected PRIVATE or BUSINESS"
        )
    body = {
        "profile": profile_id,
        "accountHolderName": name,
        "currency": "EUR",
        "type": "iban",
        "details": {"legalType": legal_type, "iban": iban.replace(" ", "")},
    }
    result = _wise_post(token, "/v1/accounts", body)
    account_id = result.get("id")
    if not account_id:
        raise RuntimeError(f"Wise /v1/accounts returned no id: {result}")
    return account_id


def _create_quote(token: str, profile_id: int, amount_eur: Decimal) -> str:
    # amount_eur reaches Wise as an exact decimal number. It is never coerced
    # to int or float on the way — that coercion is ateles#552, where int()
    # truncated €133.60 to €133 and the transfer funded short in silence.
    amount_eur = parse_amount_eur(amount_eur)
    body = {
        "sourceCurrency": "EUR",
        "targetCurrency": "EUR",
        "sourceAmount": amount_eur,
        "profile": profile_id,
        "payOut": "BANK_TRANSFER",
    }
    result = _wise_post(token, f"/v3/profiles/{profile_id}/quotes", body)
    quote_uuid = result.get("id") or result.get("uuid")
    if not quote_uuid:
        raise RuntimeError(f"Wise quote returned no id: {result}")

    # The quote is what fixes the amount for the transfer that follows, so
    # check Wise priced the amount we asked for before anything is created
    # against it. Wise echoes sourceAmount back; a missing echo is not treated
    # as a mismatch here because the transfer record is reconciled separately.
    quoted_raw = result.get("sourceAmount")
    if quoted_raw is not None:
        try:
            quoted = parse_amount_eur(quoted_raw)
        except PaymentAmountError:
            quoted = None
        if quoted is not None and quoted != amount_eur:
            raise TransferAmountMismatch(
                f"Wise quoted €{quoted} but €{amount_eur} was requested — "
                f"refusing to create a transfer against a mispriced quote"
            )

    return str(quote_uuid)


def _create_transfer(
    token: str, target_account_id: int, quote_uuid: str, reference: str
) -> tuple[int, dict]:
    import uuid as _uuid

    body = {
        "targetAccount": target_account_id,
        "quoteUuid": quote_uuid,
        "customerTransactionId": str(_uuid.uuid4()),
        "details": {
            "reference": reference,
            "transferPurpose": "personal.family.support",
            "sourceOfFunds": "personal.savings",
        },
    }
    result = _wise_post(token, "/v1/transfers", body)
    transfer_id = result.get("id")
    if not transfer_id:
        raise RuntimeError(f"Wise /v1/transfers returned no id: {result}")
    return int(transfer_id), result


def _fund_transfer(token: str, profile_id: int, transfer_id: int) -> dict:
    body = {"type": "BALANCE"}
    return _wise_post(
        token, f"/v3/profiles/{profile_id}/transfers/{transfer_id}/payments", body
    )


def _fetch_transfer(token: str, transfer_id: int) -> dict | None:
    """Re-read a transfer from Wise after funding. None if it cannot be read.

    A read failure must not be mistaken for a reconciled transfer, so the
    caller falls back to the transfer record captured at creation rather than
    skipping the check.
    """
    try:
        result = _wise_get(token, f"/v1/transfers/{transfer_id}")
        return result if isinstance(result, dict) else None
    except Exception as exc:
        log.warning(f"Could not re-read Wise transfer {transfer_id}: {exc}")
        return None


def _reported_source_amount(transfer: dict) -> Decimal | None:
    """Extract Wise's own reported source amount from a transfer record.

    Returns None when the response carries no recognisable amount field, which
    the caller treats as un-reconcilable rather than as a match.
    """
    for key in ("sourceValue", "sourceAmount"):
        raw = transfer.get(key)
        if raw is None:
            continue
        try:
            return parse_amount_eur(raw)
        except Exception:
            log.warning(f"Wise transfer field {key}={raw!r} is not a usable amount")
            return None
    return None


def _reconcile_transfer_amount(
    transfer: dict, expected_eur: Decimal, label: str = "payment"
) -> None:
    """Assert Wise sent exactly what was owed. Raises on any divergence.

    Compared with Decimal equality after normalising the exponent, so that
    Decimal("133.6") and Decimal("133.60") reconcile as the same money.
    """
    reported = _reported_source_amount(transfer)
    if reported is None:
        raise TransferAmountMismatch(
            f"[{label}] Wise transfer record carries no source amount to "
            f"reconcile against €{expected_eur} — cannot confirm the transfer "
            f"funded the full amount"
        )

    if reported != expected_eur:
        raise TransferAmountMismatch(
            f"[{label}] TRANSFER AMOUNT MISMATCH: Wise reports €{reported} but "
            f"€{expected_eur} was owed (short by €{expected_eur - reported}). "
            f"The money has already moved — do not mark this payment complete."
        )

    log.info(f"[{label}] Reconciled: Wise sent €{reported}, matching the amount owed.")


def _execute_wise_transfer(
    token: str,
    iban: str,
    recipient_name: str,
    amount_eur: Decimal,
    reference: str,
    label: str = "payment",
    legal_type: str = "PRIVATE",
) -> dict:
    """Full Wise transfer flow. Returns result dict with status and details."""
    log.info(f"[{label}] Starting Wise transfer: €{amount_eur} to IBAN {iban[:10]}…")

    profile_id = _get_wise_profile_id(token)
    log.info(f"[{label}] Wise profile_id: {profile_id}")

    account_id = _get_or_create_recipient(
        token, profile_id, iban, recipient_name, legal_type
    )
    log.info(f"[{label}] Wise recipient account_id: {account_id}")

    quote_uuid = _create_quote(token, profile_id, amount_eur)
    log.info(f"[{label}] Wise quote_uuid: {quote_uuid}")

    transfer_id, transfer_record = _create_transfer(
        token, account_id, quote_uuid, reference
    )
    log.info(f"[{label}] Wise transfer_id: {transfer_id}")

    # Reconcile BEFORE funding where possible: the transfer record already
    # carries Wise's own source amount, so a mismatch caught here is caught
    # while the money is still in the balance.
    _reconcile_transfer_amount(transfer_record, amount_eur, label=label)

    funding_result = _fund_transfer(token, profile_id, transfer_id)
    log.info(f"[{label}] Wise funding result: {funding_result}")

    # One post-funding read, used for BOTH the amount reconciliation and the
    # settlement classification. Reading twice would let the two decisions judge
    # different records, so the transfer that reconciles is not necessarily the
    # transfer whose state is classified.
    funded_record = _fetch_transfer(token, transfer_id) or transfer_record

    # Reconcile against the funded transfer as Wise reports it now. This runs
    # BEFORE any status decision: a short payment must not reach the
    # done-marking path regardless of how it settles (ateles#552).
    _reconcile_transfer_amount(funded_record, amount_eur, label=label)

    status = str(funding_result.get("status", "") or "")
    if status not in FUNDING_ACCEPTED:
        raise RuntimeError(
            f"Wise funding status unexpected: {status} — full result: {funding_result}"
        )

    transfer_state = classify_transfer_state(funded_record)

    def _result(result_status: str) -> dict:
        # One payload builder for every branch, so a consumer that reads a
        # field on the sent path cannot find it missing on the unsettled one.
        return {
            "status": result_status,
            "transfer_id": transfer_id,
            "quote_uuid": quote_uuid,
            "account_id": account_id,
            "amount_eur": amount_eur,
            "iban": iban,
            "recipient_name": recipient_name,
            "reference": reference,
            "wise_status": status,
            "wise_transfer_status": str(
                (funded_record or {}).get("status", "") or ""
            ),
        }

    if transfer_state == "settled":
        log.info(f"[{label}] Wise reports the transfer delivered — payment complete.")
        return _result("sent")

    if transfer_state == "failed":
        # Fail loudly rather than parking: the caller's except turns this into
        # manual_required, which leaves the task open and tells the operator.
        raise RuntimeError(
            f"Wise transfer {transfer_id} reports status "
            f"{funded_record.get('status')!r} after funding — the payment did not "
            f"go through and needs manual attention"
        )

    # in_flight or unreadable: the money has left the balance and Wise has not
    # said it arrived. Not a success and not a failure — the one state that
    # must not be collapsed into either.
    log.info(
        f"[{label}] transfer {transfer_id} submitted, awaiting settlement "
        f"(funding={status}, transfer={transfer_state})"
    )
    return _result(RESULT_AWAITING_SETTLEMENT)


# ---------------------------------------------------------------------------
# Neotoma task update (generic, driven by profile)
# ---------------------------------------------------------------------------


def _find_task_id(profile: PaymentProfile) -> str:
    """Return the Neotoma task ID: use profile value if set, else search by keywords."""
    if profile.neotoma_task_id:
        return profile.neotoma_task_id

    import shutil

    neotoma = shutil.which("neotoma")
    if not neotoma:
        return ""

    query = (
        " ".join(profile.task_keywords[:3]) if profile.task_keywords else profile.label
    )
    try:
        result = subprocess.run(
            [
                neotoma,
                "--json",
                "--api-only",
                "entities",
                "search",
                "--query",
                query,
                "--entity-type",
                "task",
                "--limit",
                "5",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            env=os.environ,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            entities = data.get("entities") or data.get("results") or []
            for e in entities:
                snap = e.get("snapshot") or {}
                title = (snap.get("title") or snap.get("name") or "").lower()
                if any(kw in title for kw in profile.task_keywords):
                    return e.get("entity_id") or e.get("id") or ""
    except Exception as exc:
        log.debug(f"[{profile.name}] Task ID lookup failed: {exc}")
    return ""


def note_task(profile: PaymentProfile, task_id: str, neotoma: str, text: str) -> bool:
    """Write a note onto a Neotoma task. Returns True on success.

    Shared by the execute path and the settlement sweep so both write notes
    through one implementation and one argv shape.
    """
    if not task_id:
        return False
    ok = _neotoma_set_field(neotoma, task_id, "notes", text)
    if ok:
        log.info(f"[{profile.name}] Neotoma task {task_id} notes updated.")
    return ok


def _update_task(profile: PaymentProfile, result: dict) -> None:
    """Update the Neotoma payment task: add note, park or close, roll due_date."""
    import shutil

    status = result.get("status")
    neotoma = shutil.which("neotoma")
    if not neotoma:
        log.warning(f"[{profile.name}] neotoma CLI not found — skipping task update")
        # Parking is not conditional on the task update working. The money is
        # in flight either way, and an unparked profile re-previews the same
        # invoice on the next tick — so this early return must not skip it.
        if status == RESULT_AWAITING_SETTLEMENT:
            log.error(
                f"[{profile.name}] CANNOT PARK PROFILE: neotoma CLI not found while a "
                f"transfer is in flight (transfer_id={result.get('transfer_id')}). "
                f"This profile may re-trigger — set its status to "
                f"{PROFILE_STATUS_AWAITING_SETTLEMENT} by hand."
            )
            _escalate(
                f"monedula: transfer {result.get('transfer_id')} is in flight for "
                f"{profile.label} but the profile could NOT be parked (neotoma CLI "
                f"missing) — it may be re-previewed for payment. Park it by hand."
            )
        return

    task_id = _find_task_id(profile)
    if not task_id:
        log.warning(f"[{profile.name}] Could not find task ID — skipping task update")
        # Same reasoning: a profile whose task cannot be resolved still has
        # money in flight, and is the least observable path on which to leave
        # it active.
        if status == RESULT_AWAITING_SETTLEMENT:
            _mark_awaiting_settlement(profile, result, neotoma)
        return

    today = date.today()
    transfer_id = result.get("transfer_id", "unknown")
    amount = profile.amount_eur
    reference = profile.wise_reference
    if status == RESULT_AWAITING_SETTLEMENT:
        note = (
            f"Payment submitted {today.isoformat()}: "
            f"€{amount} Wise transfer_id={transfer_id} ref={reference} — "
            f"AWAITING SETTLEMENT (wise_status={result.get('wise_status', '')}, "
            f"wise_transfer_status={result.get('wise_transfer_status', '')})"
        )
    elif status == "manual_required":
        # NEVER "Payment sent" for a manual_required. Before the settlement
        # states existed every manual_required fired BEFORE money moved, which
        # is why the generic note was tolerable; now a transfer can fail AFTER
        # funding and reach here, and asserting a completed payment over it is
        # the ateles#552 defect this PR was written to remove (ateles#604
        # review, demonstrated by execution).
        note = (
            f"Payment NOT COMPLETED {today.isoformat()}: "
            f"€{amount} Wise transfer_id={transfer_id} ref={reference} — "
            f"{result.get('error', 'manual intervention required')}; "
            f"task left open, due_date not rolled, operator decision required"
        )
    else:
        note = (
            f"Payment sent {today.isoformat()}: "
            f"€{amount} Wise transfer_id={transfer_id} ref={reference}"
        )

    note_task(profile, task_id, neotoma, note)

    # A submitted-but-unsettled transfer closes nothing and rolls nothing. The
    # single branch that satisfies "never mark done on an unsettled transfer":
    # no --status done, no --status archived, no --due-date roll.
    if status == RESULT_AWAITING_SETTLEMENT:
        _mark_awaiting_settlement(profile, result, neotoma)
        return

    # Same for a manual_required: the note above records what happened, and
    # nothing further is written. Rolling the due_date here would retire a
    # payment that did not complete — and if the failure came after funding,
    # the money has already moved.
    if status == "manual_required":
        return

    # A one-off invoice has no "next" occurrence: close the task and archive the
    # profile so the next daily run cannot pay the same invoice twice.
    if profile.one_off:
        if result.get("status") != "sent":
            log.info(
                f"[{profile.name}] one-off payment not sent — leaving task and "
                f"profile active for retry."
            )
            return
        _close_one_off(profile, task_id, neotoma)
        return

    next_due = _find_next_event_due_date(profile)
    if next_due:
        if _neotoma_set_field(neotoma, task_id, "due_date", next_due):
            log.info(f"[{profile.name}] Neotoma task due_date set to {next_due}.")
        else:
            log.warning(
                f"[{profile.name}] neotoma due_date correction failed for task {task_id}"
            )
    else:
        log.warning(
            f"[{profile.name}] Could not find next event date — due_date not updated."
        )


def _escalate(message: str) -> None:
    """Surface an operator-visible blocker. Never raises.

    Imported lazily and defensively: the handler module is imported by tests
    and by the daemon alike, and a notifier that is unavailable must not turn a
    money-path warning into a crash.
    """
    try:
        from lib.notify import Notifier, Priority  # type: ignore[import-not-found]

        notifier = Notifier.from_neotoma()
        if notifier is None:
            raise RuntimeError("no notifier configured")
        notifier.send(message, priority=Priority.BLOCKER, handler="monedula")
    except Exception as exc:  # notifier absent, misconfigured, or offline
        log.error(f"ESCALATION NOT DELIVERED ({exc}): {message}")


def _neotoma_set_field(neotoma: str, entity_id: str, field: str, value: str) -> bool:
    """Set one snapshot field on a Neotoma entity. Returns True on success.

    Uses `corrections create` for every snapshot write. CLI 0.16.0 has no
    `entities update` subcommand at all — status, notes, due_date and the
    in-flight guard fields all go through correction observations.
    """
    if not entity_id:
        log.warning(f"cannot set {field}: no entity id")
        return False
    try:
        res = subprocess.run(
            [
                neotoma,
                "--api-only",
                "corrections",
                "create",
                "--entity-id",
                entity_id,
                "--field-name",
                field,
                "--corrected-value",
                str(value),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=os.environ,
        )
        if res.returncode != 0:
            log.error(
                f"neotoma correction failed ({entity_id}.{field}={value}): "
                f"{res.stderr.strip()[:200]}"
            )
            return False
        return True
    except Exception as exc:
        log.error(f"neotoma correction error ({entity_id}.{field}={value}): {exc}")
        return False


def _mark_awaiting_settlement(
    profile: PaymentProfile, result: dict, neotoma: str
) -> None:
    """Park a profile whose transfer is in flight. Never raises.

    Parking is the double-payment guard: load_profiles_from_neotoma() matches
    active profiles only, so a parked profile cannot re-match, re-preview or
    re-pay while its transfer is on its way.

    A failed park is escalated rather than merely logged. If the status
    correction does not land, the profile stays active with money in flight AND
    the preview's in-flight warning — which reads pending_transfer_id, written
    by the same failing call — is missing too. Both defences fail together, so
    the operator's Telegram approval becomes the only thing between an
    unsettled transfer and a second one.

    It never raises: the money has already moved, and a bookkeeping failure
    must not be reported to the caller as a payment failure.
    """
    transfer_id = str(result.get("transfer_id", "") or "")
    if not profile.entity_id:
        log.error(
            f"[{profile.name}] CANNOT PARK PROFILE: no entity id, transfer "
            f"{transfer_id} is in flight — this profile may re-trigger."
        )
        _escalate(
            f"monedula: transfer {transfer_id} is in flight for {profile.label} but "
            f"the profile has no entity id and could NOT be parked — it may be "
            f"re-previewed for payment."
        )
        return

    status_ok = _neotoma_set_field(
        neotoma,
        profile.entity_id,
        "status",
        PROFILE_STATUS_AWAITING_SETTLEMENT,
    )
    _neotoma_set_field(neotoma, profile.entity_id, "pending_transfer_id", transfer_id)
    _neotoma_set_field(
        neotoma, profile.entity_id, "pending_transfer_at", date.today().isoformat()
    )

    if status_ok:
        log.info(
            f"[{profile.name}] parked awaiting settlement (transfer {transfer_id})."
        )
        return

    log.error(
        f"[{profile.name}] PARK FAILED: profile {profile.entity_id} is still active "
        f"while transfer {transfer_id} is in flight — archive or park it by hand "
        f"before the next tick."
    )
    _escalate(
        f"monedula: transfer {transfer_id} is in flight for {profile.label} but the "
        f"profile could NOT be parked — it may be re-previewed for payment. Park "
        f"profile {profile.entity_id} by hand."
    )


# Public delegates. settlement.py drives the same operations without reaching
# across modules for private names, and they resolve at call time so a test
# that monkeypatches the underscore-prefixed function still takes effect.


def fetch_transfer(token: str, transfer_id: int) -> dict | None:
    return _fetch_transfer(token, transfer_id)


def close_one_off(profile: PaymentProfile, task_id: str, neotoma: str) -> None:
    _close_one_off(profile, task_id, neotoma)


def set_entity_field(neotoma: str, entity_id: str, field: str, value: str) -> bool:
    return _neotoma_set_field(neotoma, entity_id, field, value)


def find_task_id(profile: PaymentProfile) -> str:
    return _find_task_id(profile)


def find_next_event_due_date(profile: PaymentProfile) -> str | None:
    return _find_next_event_due_date(profile)


def escalate(message: str) -> None:
    """Surface an operator-visible blocker. Public delegate for settlement.py."""
    _escalate(message)


def _close_one_off(profile: PaymentProfile, task_id: str, neotoma: str) -> None:
    """Mark a one-off task done and archive its profile after a paid transfer.

    Archiving matters: load_profiles_from_neotoma() skips non-active profiles,
    so this is what stops a paid one-off invoice from matching again tomorrow.
    A failure here is logged loudly rather than raised — the money has already
    moved, and the caller must not treat a bookkeeping failure as a payment
    failure. The risk it leaves is a duplicate preview, not a duplicate payment:
    the operator still has to approve by name at the Telegram gate.
    """
    for entity_id, value, what in (
        (task_id, "done", "task status=done"),
        (profile.entity_id, "archived", "profile status=archived"),
    ):
        if not entity_id:
            log.warning(f"[{profile.name}] cannot set {what}: no entity id")
            continue
        if _neotoma_set_field(neotoma, entity_id, "status", value):
            log.info(f"[{profile.name}] one-off {what} set.")
        else:
            log.error(
                f"[{profile.name}] ONE-OFF CLEANUP FAILED ({what}) — "
                f"this profile may re-trigger; archive it by hand."
            )


def _find_next_event_due_date(profile: PaymentProfile) -> str | None:
    """
    Search Google Calendar for the next event matching profile keywords.
    Returns ISO date string of (next event date + 1 day), or None.
    """
    import shutil

    gws = shutil.which("gws")
    if not gws:
        log.warning(
            f"[{profile.name}] gws CLI not found — cannot look up next event date"
        )
        return None

    today = date.today()
    time_min = today.strftime("%Y-%m-%dT00:00:00+02:00")
    time_max = (today + timedelta(days=92)).strftime("%Y-%m-%dT23:59:59+02:00")

    for query in profile.calendar_keywords:
        params = {
            "calendarId": "primary",
            "singleEvents": True,
            "orderBy": "startTime",
            "q": query,
            "timeMin": time_min,
            "timeMax": time_max,
        }
        try:
            result = subprocess.run(
                [gws, "calendar", "events", "list", "--params", json.dumps(params)],
                capture_output=True,
                text=True,
                timeout=30,
                env=os.environ,
            )
            if result.returncode != 0:
                continue
            data = json.loads(result.stdout)
            for item in data.get("items") or []:
                summary_low = (item.get("summary") or "").lower()
                if any(kw in summary_low for kw in profile.calendar_keywords):
                    start = item.get("start", {})
                    event_date_str = start.get("date") or start.get("dateTime", "")[:10]
                    if event_date_str:
                        event_date = date.fromisoformat(event_date_str)
                        due = event_date + timedelta(days=1)
                        log.info(
                            f"[{profile.name}] Next event: {event_date_str}, due: {due.isoformat()}"
                        )
                        return due.isoformat()
        except Exception as exc:
            log.warning(
                f"[{profile.name}] Calendar search error (query={query!r}): {exc}"
            )

    log.info(f"[{profile.name}] No upcoming events found in calendar.")
    return None
