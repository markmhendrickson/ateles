"""
handlers/wise_transfer.py — Generic Wise transfer handler for Monedula.

Executes a Wise IBAN transfer for any PaymentProfile with payment_type="wise".
The payee is resolved entirely from Neotoma (the swarm's canonical store) —
either from profile-carried fields (wise_recipient_id / wise_iban) or from the
linked Neotoma contact entity (profile.contact_id). There is NO parquet
dependency; no business-specific values are hardcoded here.

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
import urllib.error
import urllib.request
from datetime import date, timedelta
from typing import Any

try:
    from ..handler_base import PaymentHandler
except ImportError:
    from handler_base import PaymentHandler  # type: ignore[no-redef]
from .neotoma_cli import correct_field
from .payment_profile import PaymentProfile, effective_amount_eur

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
        """Return a match for each event whose title contains any profile keyword."""
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
        amount_eur = effective_amount_eur(self.profile, match)
        log.info(f"[{self.name}] Executing Wise payment (€{amount_eur})...")

        # Recipient resolution order (all Neotoma-sourced; no parquet):
        #   1. profile.wise_recipient_id  — reuse a verified Wise account (safest;
        #      no recipient re-creation, name already checked by Wise).
        #   2. profile.wise_iban          — IBAN carried on the profile itself.
        #   3. Neotoma contact entity (profile.contact_id) — reads
        #      wise_recipient_id / iban / name from the contact snapshot.
        recipient_id = str(getattr(self.profile, "wise_recipient_id", "") or "").strip()
        iban = str(getattr(self.profile, "wise_iban", "") or "").strip()
        recipient_name = str(getattr(self.profile, "wise_recipient_name", "") or "").strip()

        if not recipient_id and not iban:
            contact = _load_contact(self.profile)
            if not contact:
                return {
                    "status": "manual_required",
                    "handler": self.name,
                    "error": "No wise_recipient_id/wise_iban on profile and no Neotoma contact resolved",
                    "amount_eur": amount_eur,
                    "reference": self.profile.wise_reference,
                }
            # A contact may itself carry a verified Wise recipient id — prefer it.
            recipient_id = recipient_id or contact.get("wise_recipient_id", "")
            iban = contact.get("iban", "")
            recipient_name = recipient_name or contact.get("name", "")

        if not recipient_id and not iban:
            return {
                "status": "manual_required",
                "handler": self.name,
                "error": "No Wise recipient id or IBAN resolved",
                "amount_eur": amount_eur,
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
                "amount_eur": amount_eur,
                "iban": iban,
                "recipient_name": recipient_name,
                "reference": self.profile.wise_reference,
            }

        try:
            result = _execute_wise_transfer(
                token,
                iban,
                recipient_name,
                amount_eur,
                self.profile.wise_reference,
                label=self.name,
                recipient_id=recipient_id,
                legal_type=str(getattr(self.profile, "wise_legal_type", "") or ""),
                dry_run=os.environ.get("MONEDULA_DRYRUN", "1") != "0",
            )
        except Exception as exc:
            log.error(f"[{self.name}] Wise transfer exception: {exc}")
            result = {
                "status": "manual_required",
                "handler": self.name,
                "error": str(exc),
                "amount_eur": amount_eur,
                "iban": iban,
                "recipient_name": recipient_name,
                "reference": self.profile.wise_reference,
            }

        result["handler"] = self.name

        # Fetch the official Wise receipt PDF (the payee's "justificante") for a
        # real send, so the confirmation email can attach proof of payment.
        if result.get("status") == "sent" and result.get("transfer_id"):
            receipt_path = _fetch_wise_receipt(
                token, str(result["transfer_id"]), label=self.name
            )
            if receipt_path:
                result["receipt_path"] = receipt_path
                result["receipt_kind"] = "wise_pdf"

        if result.get("status") in ("sent", "manual_required"):
            _update_task(self.profile, result)

        return result

    def format_confirmation(self, result: dict) -> str:
        status = result.get("status")
        # Prefer the amount actually charged (carries any one-off override);
        # the profile's standing rate is only a last-resort fallback.
        amount = result.get("amount_eur", self.profile.amount_eur)
        reference = self.profile.wise_reference
        if status == "sent":
            transfer_id = result.get("transfer_id", "unknown")
            name = result.get("recipient_name", "recipient")
            # No payee copy-paste line for Wise: there is no public explorer, so
            # there is nothing for the payee to verify. The share line is a
            # blockchain-only convention (see handlers/share_message.py).
            return (
                f"✅ {self.profile.label} payment sent via Wise!\n"
                f"  Transfer ID: {transfer_id}\n"
                f"  Recipient: {name}\n"
                f"  Amount: €{amount}\n"
                f"  Reference: {reference}"
            )
        elif status == "dry_run":
            # A dry-run is a successful rehearsal, NOT a failure. Reporting it
            # through the generic error branch produced alarming
            # "payment failed: unknown error" emails.
            return (
                f"🧪 {self.profile.label} DRY-RUN ok (no money moved).\n"
                f"  Would send: €{amount} via Wise\n"
                f"  Recipient acct: {result.get('account_id', 'n/a')}\n"
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
            # Name the actual status — a bare "unknown error" hid the fact that
            # the result was simply a status this branch didn't know about.
            error = result.get("error") or f"unexpected status {status!r}"
            return f"❌ {self.profile.label} payment failed: {error}"


# ---------------------------------------------------------------------------
# Contact loading (from Neotoma — the swarm's canonical store; no parquet)
# ---------------------------------------------------------------------------


def _load_contact(profile: PaymentProfile) -> dict | None:
    """
    Load the payment contact from Neotoma (the canonical store) using the
    profile's `contact_id` (a Neotoma contact entity id). Reads payment fields
    directly from the contact snapshot: name, iban, wise_recipient_id,
    btc_address. Returns a dict with at least 'name' and 'iban', or None.

    Prefer profile-carried recipient fields (wise_recipient_id / wise_iban) over
    this lookup — see WiseTransferHandler.execute(). This function exists for the
    contact_id path; there is NO parquet dependency.
    """
    contact_id = str(getattr(profile, "contact_id", "") or "").strip()
    if not contact_id:
        log.warning(f"[{profile.name}] No contact_id on profile — cannot resolve payee from Neotoma")
        return None

    snap = _fetch_contact_snapshot(contact_id)
    if not snap:
        log.warning(f"[{profile.name}] Contact {contact_id} not found in Neotoma")
        return None

    return _normalize_contact(snap)


def _fetch_contact_snapshot(entity_id: str) -> dict | None:
    """Fetch a Neotoma contact entity's snapshot dict by id. None on any error."""
    import json
    import urllib.error
    import urllib.request

    base_url = os.environ.get("NEOTOMA_BASE_URL", "http://localhost:3180").rstrip("/")
    bearer = os.environ.get("NEOTOMA_BEARER_TOKEN", "").strip()
    is_loopback = "localhost" in base_url or "127.0.0.1" in base_url
    try:
        url = f"{base_url}/entities/{entity_id}"
        headers = {"Accept": "application/json"}
        if bearer and not is_loopback:
            headers["Authorization"] = f"Bearer {bearer}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            entity = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        log.error(f"Neotoma contact fetch failed for {entity_id}: {exc}")
        return None

    # The entity may nest the resolved fields under snapshot.snapshot.
    snap = entity.get("snapshot") or entity
    if isinstance(snap.get("snapshot"), dict):
        snap = snap["snapshot"]
    return snap if isinstance(snap, dict) else None


def _normalize_contact(snap: dict) -> dict:
    """Extract name + payment identifiers from a Neotoma contact snapshot."""
    name = snap.get("name") or snap.get("full_name") or snap.get("canonical_name") or ""
    iban = snap.get("iban") or ""
    return {
        "name": str(name),
        "iban": str(iban),
        "wise_recipient_id": str(snap.get("wise_recipient_id") or ""),
        "btc_address": str(snap.get("btc_address") or ""),
        "phone": str(snap.get("phone") or snap.get("mobile") or ""),
    }


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


def _fetch_wise_receipt(token: str, transfer_id: str, label: str = "payment") -> str | None:
    """Download the official Wise receipt PDF for a transfer.

    Returns a local file path (under MONEDULA_RECEIPTS_DIR, default a temp dir),
    or None on any failure — receipt fetch is best-effort and never blocks a
    payment that already succeeded.
    """
    import tempfile
    from pathlib import Path

    url = f"{WISE_BASE_URL}/v1/transfers/{transfer_id}/receipt.pdf"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        if not data[:5] == b"%PDF-":
            log.warning(f"[{label}] Wise receipt for {transfer_id} not a PDF — skipping")
            return None
        out_dir = Path(
            os.environ.get("MONEDULA_RECEIPTS_DIR", tempfile.gettempdir())
        ).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"wise-receipt-{transfer_id}.pdf"
        out_path.write_bytes(data)
        log.info(f"[{label}] Saved Wise receipt: {out_path} ({len(data)} bytes)")
        return str(out_path)
    except (urllib.error.URLError, OSError) as exc:
        log.warning(f"[{label}] Wise receipt fetch failed for {transfer_id}: {exc}")
        return None


def _get_wise_profile_id(token: str) -> int:
    profiles = _wise_get(token, "/v1/profiles")
    for profile in profiles:
        if profile.get("type") == "personal":
            return profile["id"]
    if profiles:
        return profiles[0]["id"]
    raise RuntimeError("No Wise profiles found")


def _get_or_create_recipient(
    token: str, profile_id: int, iban: str, name: str, legal_type: str = "PRIVATE"
) -> int:
    lt = (legal_type or "PRIVATE").upper()
    if lt not in ("PRIVATE", "BUSINESS"):
        lt = "PRIVATE"
    body = {
        "profile": profile_id,
        "accountHolderName": name,
        "currency": "EUR",
        "type": "iban",
        "details": {"legalType": lt, "iban": iban.replace(" ", "")},
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
    recipient_id: str = "",
    legal_type: str = "",
    dry_run: bool = False,
) -> dict:
    """Full Wise transfer flow. Returns result dict with status and details.

    When `recipient_id` is provided, reuse that verified Wise account instead of
    creating a recipient from the IBAN. When `dry_run` is True, authenticate and
    build the quote but do NOT create or fund a transfer (no money moves).
    """
    log.info(f"[{label}] Starting Wise transfer: €{amount_eur} (dry_run={dry_run})")

    profile_id = _get_wise_profile_id(token)
    log.info(f"[{label}] Wise profile_id: {profile_id}")

    if recipient_id:
        account_id = int(recipient_id)
        log.info(f"[{label}] Using verified Wise recipient account_id: {account_id}")
    else:
        account_id = _get_or_create_recipient(
            token, profile_id, iban, recipient_name, legal_type=legal_type
        )
        log.info(f"[{label}] Wise recipient account_id: {account_id} (legalType={legal_type or 'PRIVATE'})")

    quote_uuid = _create_quote(token, profile_id, amount_eur)
    log.info(f"[{label}] Wise quote_uuid: {quote_uuid}")

    if dry_run:
        # Auth + recipient + quote all succeeded; stop before creating/funding.
        log.info(f"[{label}] DRY-RUN — not creating or funding transfer.")
        return {
            "status": "dry_run",
            "handler": label,
            "account_id": account_id,
            "quote_uuid": quote_uuid,
            "amount_eur": amount_eur,
            "recipient_name": recipient_name,
            "reference": reference,
        }

    transfer_id = _create_transfer(token, account_id, quote_uuid, reference)
    log.info(f"[{label}] Wise transfer_id: {transfer_id}")

    # CRITICAL: from here on a Wise transfer EXISTS. Never raise past this point —
    # a raise would leave the task unstamped and the next poll would create a
    # SECOND transfer (double-pay). A transient bounced_back can self-recover
    # (observed 2026-06-23, transfer 2206919954), so an unexpected funding status
    # is reported as an in-flight transfer to be confirmed out-of-band, NOT retried.
    try:
        funding_result = _fund_transfer(token, profile_id, transfer_id)
    except Exception as exc:  # noqa: BLE001
        log.error(f"[{label}] Wise funding call failed AFTER transfer {transfer_id} "
                  f"created: {exc} — reporting as created_unconfirmed (no retry).")
        return {
            "status": "created_unconfirmed",
            "transfer_id": transfer_id,
            "quote_uuid": quote_uuid,
            "account_id": account_id,
            "amount_eur": amount_eur,
            "iban": iban,
            "recipient_name": recipient_name,
            "reference": reference,
            "wise_status": "funding_call_failed",
            "error": str(exc),
        }
    log.info(f"[{label}] Wise funding result: {funding_result}")

    status = funding_result.get("status", "")
    base = {
        "transfer_id": transfer_id,
        "quote_uuid": quote_uuid,
        "account_id": account_id,
        "amount_eur": amount_eur,
        "iban": iban,
        "recipient_name": recipient_name,
        "reference": reference,
        "wise_status": status,
    }
    if status in ("COMPLETED", "PROCESSING", "PENDING"):
        return {"status": "sent", **base}
    # Any other status (incl. a transient bounced_back) — the transfer exists.
    # Mark it in-flight so the idempotency marker is stamped and no re-create
    # happens; the operator/daemon confirms settlement out-of-band.
    log.warning(f"[{label}] Wise funding status {status!r} after transfer "
                f"{transfer_id} — created_unconfirmed (no retry; confirm settlement).")
    return {"status": "created_unconfirmed", "error":
            f"Wise funding status {status!r} — transfer created, confirm settlement", **base}


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
    """Append a payment note to the Neotoma task and roll its due_date."""
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
    correct_field(task_id, "notes", note, label=profile.name)

    next_due = _find_next_event_due_date(profile)
    if next_due:
        correct_field(task_id, "due_date", next_due, label=profile.name)
    else:
        log.warning(
            f"[{profile.name}] Could not find next event date — due_date not updated."
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
