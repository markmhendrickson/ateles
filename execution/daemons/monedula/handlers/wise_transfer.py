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
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import Any

try:
    from ..handler_base import PaymentHandler
except ImportError:
    from handler_base import PaymentHandler  # type: ignore[no-redef]
from .payment_profile import PaymentProfile

log = logging.getLogger(__name__)

WISE_BASE_URL = "https://api.transferwise.com"


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
        return (
            f"💳 {self.profile.label}\n"
            f"  €{self.profile.amount_eur} Wise → {name} (IBAN: {iban_preview})\n"
            f"  Task: {task_id or '(unknown)'}\n"
            f"  Event: {summary}"
        )

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

        if result.get("status") in ("sent", "manual_required"):
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


def _wise_post(token: str, path: str, body: dict) -> dict:
    import urllib.error
    import urllib.request

    url = f"{WISE_BASE_URL}{path}"
    data = json.dumps(body).encode()
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


def _create_quote(token: str, profile_id: int, amount_eur: int) -> str:
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
    return str(quote_uuid)


def _create_transfer(
    token: str, target_account_id: int, quote_uuid: str, reference: str
) -> int:
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
    return int(transfer_id)


def _fund_transfer(token: str, profile_id: int, transfer_id: int) -> dict:
    body = {"type": "BALANCE"}
    return _wise_post(
        token, f"/v3/profiles/{profile_id}/transfers/{transfer_id}/payments", body
    )


def _execute_wise_transfer(
    token: str,
    iban: str,
    recipient_name: str,
    amount_eur: int,
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

    transfer_id = _create_transfer(token, account_id, quote_uuid, reference)
    log.info(f"[{label}] Wise transfer_id: {transfer_id}")

    funding_result = _fund_transfer(token, profile_id, transfer_id)
    log.info(f"[{label}] Wise funding result: {funding_result}")

    status = funding_result.get("status", "")
    if status in ("COMPLETED", "PROCESSING", "PENDING"):
        return {
            "status": "sent",
            "transfer_id": transfer_id,
            "quote_uuid": quote_uuid,
            "account_id": account_id,
            "amount_eur": amount_eur,
            "iban": iban,
            "recipient_name": recipient_name,
            "reference": reference,
            "wise_status": status,
        }
    else:
        raise RuntimeError(
            f"Wise funding status unexpected: {status} — full result: {funding_result}"
        )


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


def _update_task(profile: PaymentProfile, result: dict) -> None:
    """Update the Neotoma payment task: add note and roll due_date."""
    import shutil

    neotoma = shutil.which("neotoma")
    if not neotoma:
        log.warning(f"[{profile.name}] neotoma CLI not found — skipping task update")
        return

    task_id = _find_task_id(profile)
    if not task_id:
        log.warning(f"[{profile.name}] Could not find task ID — skipping task update")
        return

    today = date.today()
    transfer_id = result.get("transfer_id", "unknown")
    amount = profile.amount_eur
    reference = profile.wise_reference
    note = (
        f"Payment sent {today.isoformat()}: "
        f"€{amount} Wise transfer_id={transfer_id} ref={reference}"
    )

    try:
        res = subprocess.run(
            [neotoma, "--api-only", "entities", "update", task_id, "--notes", note],
            capture_output=True,
            text=True,
            timeout=30,
            env=os.environ,
        )
        if res.returncode != 0:
            log.warning(
                f"[{profile.name}] neotoma notes update failed: {res.stderr.strip()[:200]}"
            )
        else:
            log.info(f"[{profile.name}] Neotoma task {task_id} notes updated.")
    except Exception as exc:
        log.warning(f"[{profile.name}] neotoma update error: {exc}")

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
        try:
            res = subprocess.run(
                [
                    neotoma,
                    "--api-only",
                    "entities",
                    "update",
                    task_id,
                    "--due-date",
                    next_due,
                ],
                capture_output=True,
                text=True,
                timeout=30,
                env=os.environ,
            )
            if res.returncode != 0:
                log.warning(
                    f"[{profile.name}] neotoma due_date update failed: {res.stderr.strip()[:200]}"
                )
            else:
                log.info(f"[{profile.name}] Neotoma task due_date set to {next_due}.")
        except Exception as exc:
            log.warning(f"[{profile.name}] neotoma due_date update error: {exc}")
    else:
        log.warning(
            f"[{profile.name}] Could not find next event date — due_date not updated."
        )


def _close_one_off(profile: PaymentProfile, task_id: str, neotoma: str) -> None:
    """Mark a one-off task done and archive its profile after a paid transfer.

    Archiving matters: load_profiles_from_neotoma() skips non-active profiles,
    so this is what stops a paid one-off invoice from matching again tomorrow.
    A failure here is logged loudly rather than raised — the money has already
    moved, and the caller must not treat a bookkeeping failure as a payment
    failure. The risk it leaves is a duplicate preview, not a duplicate payment:
    the operator still has to approve by name at the Telegram gate.
    """
    for args, what in (
        (["entities", "update", task_id, "--status", "done"], "task status=done"),
        (
            ["entities", "update", profile.entity_id, "--status", "archived"],
            "profile status=archived",
        ),
    ):
        if args[2] in ("", None):
            log.warning(f"[{profile.name}] cannot set {what}: no entity id")
            continue
        try:
            res = subprocess.run(
                [neotoma, "--api-only", *args],
                capture_output=True,
                text=True,
                timeout=30,
                env=os.environ,
            )
            if res.returncode != 0:
                log.error(
                    f"[{profile.name}] ONE-OFF CLEANUP FAILED ({what}): "
                    f"{res.stderr.strip()[:200]} — this profile may re-trigger; "
                    f"archive it by hand."
                )
            else:
                log.info(f"[{profile.name}] one-off {what} set.")
        except Exception as exc:
            log.error(
                f"[{profile.name}] ONE-OFF CLEANUP ERROR ({what}): {exc} — "
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
