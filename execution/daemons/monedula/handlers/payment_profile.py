"""
handlers/payment_profile.py — Generic recurring payment profile loader.

A PaymentProfile is a dict of configuration loaded entirely from env vars.
No business-specific values are hardcoded here.

Each profile is identified by a PREFIX (e.g. "THERAPY", "YOGA") and reads:
  <PREFIX>_LABEL          Human-readable label for Telegram messages
  <PREFIX>_CALENDAR_KEYWORDS  Comma-separated keywords to match against calendar event titles
  <PREFIX>_PAYMENT_TYPE   "wise" | "btc"
  <PREFIX>_CONTACT_ID     (wise only) Neotoma contact_id prefix for IBAN lookup
  <PREFIX>_CONTACT_CATEGORY  (wise only) Fallback category for contact lookup
  <PREFIX>_CONTACT_PLATFORM  (wise only) Fallback platform for contact lookup
  <PREFIX>_AMOUNT_EUR     Transfer amount in EUR (decimal, max 2dp, e.g. 133.60)
  <PREFIX>_WISE_REFERENCE (wise only) Wise transfer reference string
  <PREFIX>_WISE_LEGAL_TYPE (wise only) Recipient legalType: PRIVATE (default) | BUSINESS
  <PREFIX>_BTC_ADDRESS    (btc only) Destination BTC address
  <PREFIX>_NEOTOMA_TASK_ID    Neotoma task entity ID to update after payment
  <PREFIX>_TASK_KEYWORDS  (optional) Comma-separated keywords for Neotoma task search fallback

Profile list is driven by MONEDULA_PROFILES env var:
  MONEDULA_PROFILES=THERAPY,YOGA

Example .env additions (placeholder values — real config lives in the
operator's private .env, never in this public file):
  MONEDULA_PROFILES=THERAPY,YOGA
  THERAPY_LABEL=Therapy
  THERAPY_CALENDAR_KEYWORDS=therapy,terapia
  THERAPY_PAYMENT_TYPE=wise
  THERAPY_CONTACT_ID=<neotoma-contact-id-prefix>
  THERAPY_CONTACT_CATEGORY=health
  THERAPY_CONTACT_PLATFORM=wise
  THERAPY_AMOUNT_EUR=60
  THERAPY_WISE_REFERENCE=Pago terapia
  THERAPY_NEOTOMA_TASK_ID=
  YOGA_LABEL=Yoga
  YOGA_CALENDAR_KEYWORDS=yoga,ioga
  YOGA_PAYMENT_TYPE=btc
  YOGA_BTC_ADDRESS=<destination-btc-address>
  YOGA_AMOUNT_EUR=60
  YOGA_NEOTOMA_TASK_ID=<ent_...>
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Literal

# Cloudflare fronts the hosted Neotoma instance and blocks urllib's default
# User-Agent with a 1010 "browser signature" 403. Any explicit UA passes.
NEOTOMA_USER_AGENT = "ateles-neotoma-sync/1.0"

# Cloudflare fronts the hosted Neotoma instance and blocks urllib's default
# User-Agent with a 1010 "browser signature" 403. Any explicit UA passes.
NEOTOMA_USER_AGENT = "ateles-neotoma-sync/1.0"

log = logging.getLogger(__name__)

# Wise accepts only these legalType values when creating an IBAN recipient.
_WISE_LEGAL_TYPES = {"PRIVATE", "BUSINESS"}

# payment_profile.status vocabulary.
#
# Only ACTIVE profiles are matched for payment. AWAITING_SETTLEMENT parks a
# profile whose transfer has been submitted to Wise but not yet delivered — it
# is the double-payment guard for the in-flight window (ateles#575), because
# load_profiles_from_neotoma() matches active profiles only. PAYMENT_FAILED is
# terminal-until-an-operator-acts: re-arming a failed payment automatically is
# a double-payment risk, so it stays a human decision.
PROFILE_STATUS_ACTIVE = "active"
PROFILE_STATUS_AWAITING_SETTLEMENT = "awaiting_settlement"
PROFILE_STATUS_PAYMENT_FAILED = "payment_failed"
PROFILE_STATUS_ARCHIVED = "archived"


# EUR is a two-decimal currency. This is the amount contract for the whole
# Monedula money path: a profile amount carries at most cents, and anything
# finer is refused rather than rounded. Rounding is what created ateles#552 —
# int() truncated €133.60 to €133 and the transfer funded short in silence.
AMOUNT_DECIMAL_PLACES = 2


class PaymentAmountError(ValueError):
    """A profile amount could not be represented exactly as a EUR amount.

    Raised instead of coercing, because the failure modes are not symmetric:
    a payment that never leaves is recoverable by rerunning it, whereas a
    payment that left for the wrong amount is money gone with no record that
    anything was wrong.
    """


def parse_amount_eur(raw: object) -> Decimal:
    """Parse a raw profile amount into an exact 2dp-or-less EUR Decimal.

    Always goes through str() before Decimal(): constructing a Decimal from a
    float would inherit the binary rounding error the Decimal is here to
    avoid, so a float source is stringified first and only then parsed.

    Raises PaymentAmountError on anything that is not an exact EUR amount —
    unparseable input, non-finite values, or more precision than cents.
    """
    if isinstance(raw, Decimal):
        amount = raw
    else:
        try:
            amount = Decimal(str(raw).strip())
        except (InvalidOperation, ValueError, TypeError, ArithmeticError) as exc:
            raise PaymentAmountError(f"amount_eur={raw!r} is not a valid number") from exc

    if not amount.is_finite():
        raise PaymentAmountError(f"amount_eur={raw!r} is not a finite amount")

    # exponent < -2 means the value carries sub-cent precision. Quantizing it
    # away would silently change how much money moves, so refuse instead.
    if -amount.as_tuple().exponent > AMOUNT_DECIMAL_PLACES:
        raise PaymentAmountError(
            f"amount_eur={raw!r} has more than {AMOUNT_DECIMAL_PLACES} decimal "
            f"places — EUR amounts are exact to the cent, and rounding a payment "
            f"amount is never done implicitly"
        )

    # Normalise to exactly 2dp so every EUR amount has one representation.
    # This never changes the value — the check above already guaranteed the
    # amount carries no more than cents — it only pads 133.6 to 133.60 so the
    # amount is formatted as money everywhere it is displayed or serialized.
    return amount.quantize(Decimal(1).scaleb(-AMOUNT_DECIMAL_PLACES))


@dataclass
class PaymentProfile:
    prefix: str  # env var prefix, e.g. "THERAPY"
    label: str  # human label, e.g. "Therapy"
    calendar_keywords: list[str]  # event title match keywords
    payment_type: Literal["wise", "btc"]
    amount_eur: Decimal

    # Wise-specific
    contact_id: str = ""  # Neotoma contact_id prefix for IBAN lookup
    contact_category: str = ""  # fallback: contacts.parquet category
    contact_platform: str = ""  # fallback: contacts.parquet platform
    wise_reference: str = ""  # Wise transfer reference
    wise_legal_type: str = "PRIVATE"  # Wise recipient legalType: PRIVATE | BUSINESS
    # A one-off payee is not a standing contact, so the profile can carry the
    # bank details directly instead of requiring a contacts.parquet row.
    wise_iban: str = ""
    wise_recipient_name: str = ""

    # BTC-specific
    btc_address: str = ""

    # One-off payments: due on a date rather than gated by a calendar event.
    # A profile with a due_date and no calendar_keywords is a one-off invoice;
    # it matches on the date instead of on attendance at a session.
    due_date: str = ""  # ISO YYYY-MM-DD; empty for recurring profiles
    one_off: bool = False  # archive the profile after a successful transfer
    entity_id: str = ""  # Neotoma payment_profile entity id (for archiving)

    # In-flight settlement state (ateles#575). Set on the profile when a Wise
    # transfer has been submitted but Wise has not yet reported it delivered.
    # The settlement sweep reads pending_transfer_id to resolve the final state
    # and pending_transfer_at to age the wait for operator escalation.
    pending_transfer_id: str = ""
    pending_transfer_at: str = ""  # ISO YYYY-MM-DD the transfer was submitted

    # Neotoma task
    neotoma_task_id: str = ""
    task_keywords: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        """Unique slug used as handler name and in Telegram replies."""
        return self.prefix.lower()


def _profile_from_entity(item: dict) -> PaymentProfile | None:
    """Build a PaymentProfile from one Neotoma payment_profile entity.

    Returns None — with a log line naming the reason — for any entity that
    cannot become a payable profile. Every rejection here is a refusal to pay:
    a profile that never loads can never move money for the wrong reason.

    The status filter is deliberately NOT applied here; the caller owns which
    statuses it wants, so a parked profile is validated exactly as a live one.
    """
    import json

    snap: dict = item.get("snapshot") or {}

    label = snap.get("label", "")
    prefix = snap.get("prefix", label.upper().replace(" ", "_"))
    if not label:
        log.warning(
            f"payment_profile entity {item.get('entity_id')} missing label — skipped"
        )
        return None

    keywords_raw: list | str = snap.get("calendar_keywords", [])
    if isinstance(keywords_raw, str):
        try:
            keywords_raw = json.loads(keywords_raw)
        except (ValueError, TypeError):
            keywords_raw = [k.strip() for k in keywords_raw.split(",") if k.strip()]
    calendar_keywords = [str(k).strip().lower() for k in keywords_raw if k]

    due_date = str(snap.get("due_date") or "").strip()
    one_off = bool(snap.get("one_off")) or (not calendar_keywords and bool(due_date))

    # A profile needs at least one trigger: calendar keywords (recurring,
    # attendance-gated) or a due date (one-off invoice). With neither it is
    # unreachable — matches() can never fire — so say so plainly.
    if not calendar_keywords and not due_date:
        log.warning(
            f"payment_profile {label!r} is UNREACHABLE: no calendar_keywords "
            f"(recurring trigger) and no due_date (one-off trigger) — skipped"
        )
        return None

    payment_type_raw = str(snap.get("payment_type", "wise")).lower()
    if payment_type_raw not in ("wise", "btc"):
        log.warning(
            f"payment_profile {label!r} unknown payment_type={payment_type_raw!r} — skipped"
        )
        return None
    payment_type: Literal["wise", "btc"] = payment_type_raw  # type: ignore[assignment]

    amount_raw = snap.get("amount_eur", 0)
    try:
        amount_eur = parse_amount_eur(amount_raw)
    except PaymentAmountError as exc:
        # Skipping is the refusal: a profile that never loads can never pay.
        # The alternative — coercing to something payable — is exactly the
        # silent truncation this guards against.
        log.error(
            f"payment_profile {label!r} REFUSED: {exc} — profile skipped, "
            f"no payment will be attempted until the amount is corrected"
        )
        return None

    if amount_eur <= 0:
        log.warning(f"payment_profile {label!r} amount_eur must be positive — skipped")
        return None

    task_kw_raw: list | str = snap.get("task_keywords", [])
    if isinstance(task_kw_raw, str):
        try:
            task_kw_raw = json.loads(task_kw_raw)
        except (ValueError, TypeError):
            task_kw_raw = [k.strip() for k in task_kw_raw.split(",") if k.strip()]
    task_keywords = [str(k).strip().lower() for k in task_kw_raw if k] or calendar_keywords

    legal_type_raw = str(snap.get("wise_legal_type") or "PRIVATE").strip().upper()
    if legal_type_raw not in _WISE_LEGAL_TYPES:
        log.warning(
            f"payment_profile {label!r} invalid wise_legal_type="
            f"{legal_type_raw!r} (expected one of {sorted(_WISE_LEGAL_TYPES)}) "
            f"— skipped"
        )
        return None

    return PaymentProfile(
        prefix=prefix,
        label=label,
        calendar_keywords=calendar_keywords,
        payment_type=payment_type,
        amount_eur=amount_eur,
        contact_id=snap.get("contact_id", ""),
        contact_category=snap.get("contact_category", ""),
        contact_platform=snap.get("contact_platform", ""),
        wise_reference=snap.get("wise_reference", ""),
        wise_legal_type=legal_type_raw,
        wise_iban=str(snap.get("wise_iban") or "").strip(),
        wise_recipient_name=str(snap.get("wise_recipient_name") or "").strip(),
        btc_address=snap.get("btc_address", ""),
        due_date=due_date,
        one_off=one_off,
        entity_id=str(item.get("entity_id") or "").strip(),
        pending_transfer_id=str(snap.get("pending_transfer_id") or "").strip(),
        pending_transfer_at=str(snap.get("pending_transfer_at") or "").strip(),
        neotoma_task_id=snap.get("neotoma_task_id", ""),
        task_keywords=task_keywords,
    )


def load_profiles_from_neotoma(
    statuses: tuple[str, ...] = (PROFILE_STATUS_ACTIVE,),
) -> list[PaymentProfile]:
    """
    Load PaymentProfiles from Neotoma payment_profile entities (Phase 5+).

    Queries Neotoma for payment_profile entities belonging to this operator and
    constructs PaymentProfile objects from snapshot fields.

    *statuses* selects which profile statuses are returned. The default is
    ACTIVE only, which is what the payment leg must ever see: a profile parked
    in awaiting_settlement, paused, archived or payment_failed must not match,
    preview, or pay. The filter fails CLOSED — any status not named here is
    skipped, so a status value nobody anticipated cannot move money.

    Falls back to empty list on any error — caller should then call
    load_profiles() to use env-var fallback.

    Required env vars:
      NEOTOMA_BEARER_TOKEN   Neotoma API auth token
      NEOTOMA_BASE_URL       Neotoma API base URL
    """
    import json
    import urllib.error
    import urllib.request

    bearer = os.environ.get("NEOTOMA_BEARER_TOKEN", "").strip()
    # No default: local hosting was retired 2026-08-04, and any fallback here is
    # a silent-failure vector — an unreachable default reads as "no profiles
    # configured" and the run pays nothing while reporting success. Fail loudly.
    base_url = os.environ.get("NEOTOMA_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError(
            "NEOTOMA_BASE_URL is not set — refusing to guess a Neotoma endpoint. "
            "Set it to the hosted instance URL before running Monedula."
        )

    # On a loopback target the server trusts localhost (NEOTOMA_TRUST_PROD_LOOPBACK)
    # and a stale/invalid bearer is actively rejected — so omit the header locally.
    is_loopback = "localhost" in base_url or "127.0.0.1" in base_url

    try:
        url = f"{base_url}/entities/query"
        body = json.dumps(
            {
                "entity_type": "payment_profile",
                "limit": 50,
                "include_snapshots": True,
            }
        ).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if bearer and not is_loopback:
            headers["Authorization"] = f"Bearer {bearer}"
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers=headers,
        )
        req.add_header("User-Agent", NEOTOMA_USER_AGENT)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        # An auth/transport rejection is NOT "no profiles configured". Returning
        # [] quietly here is how every scheduled run since the hosted migration
        # reported success while paying nothing: Cloudflare answered 403 and the
        # daemon could not tell that apart from an empty result. Log at ERROR so
        # it is visible, and name the two causes worth checking first.
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        log.error(
            f"Neotoma payment_profile fetch REJECTED: HTTP {exc.code} — no profiles "
            f"loaded, so NO payments will be proposed this run. "
            f"Check NEOTOMA_BEARER_TOKEN (401/403) and that the request carries a "
            f"User-Agent (Cloudflare 1010 blocks urllib's default). {detail}"
        )
        return []
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        log.warning(f"Neotoma payment_profile fetch failed: {exc}")
        return []

    items: list[dict] = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("entities") or data.get("items") or data.get("results") or []

    profiles: list[PaymentProfile] = []
    for item in items:
        snap_status = str(
            (item.get("snapshot") or {}).get("status", PROFILE_STATUS_ACTIVE)
        )
        if snap_status not in statuses:
            continue  # not a status this caller asked for

        profile = _profile_from_entity(item)
        if profile is not None:
            profiles.append(profile)

    log.info(
        f"Loaded {len(profiles)} payment profile(s) from Neotoma "
        f"(statuses={list(statuses)}): {[p.name for p in profiles]}"
    )
    return profiles


def load_profiles_awaiting_settlement() -> list[PaymentProfile]:
    """Load profiles parked awaiting settlement, for the settlement sweep.

    Drops — loudly — any parked profile whose pending_transfer_id cannot be
    resolved to a Wise transfer. A profile parked with no resolvable transfer
    id can never leave awaiting_settlement on its own, so it must be visible to
    the operator rather than silently retried or silently stuck.

    There is no env-var equivalent: in-flight state is written by the daemon to
    Neotoma, and the env loader is a static-config fallback that cannot carry
    per-run state.
    """
    parked = load_profiles_from_neotoma(statuses=(PROFILE_STATUS_AWAITING_SETTLEMENT,))
    resolvable: list[PaymentProfile] = []
    for profile in parked:
        if _valid_transfer_id(profile.pending_transfer_id) is None:
            log.warning(
                f"payment_profile {profile.label!r} is parked in "
                f"{PROFILE_STATUS_AWAITING_SETTLEMENT} with an unusable "
                f"pending_transfer_id={profile.pending_transfer_id!r} — it cannot be "
                f"resolved against Wise and will stay parked until corrected by hand"
            )
            continue
        resolvable.append(profile)
    return resolvable


def _valid_transfer_id(raw: object) -> int | None:
    """Return a positive int Wise transfer id, or None if *raw* is not one.

    Validity, not str.isdigit(): "0" and "-1" are numeric and are not transfer
    ids, and a GET against either would be a request that can only fail.
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def load_profiles_with_neotoma_fallback() -> list[PaymentProfile]:
    """
    Load PaymentProfiles: try Neotoma first, fall back to env vars.

    Phase 5 entrypoint. Monedula callers should use this instead of
    load_profiles() to transparently prefer Neotoma-sourced profiles.
    """
    try:
        profiles = load_profiles_from_neotoma()
    except RuntimeError as exc:
        # Neotoma is unconfigured, not unreachable. That is loud in the log but
        # recoverable here, because env-var profiles are a real second source —
        # so fall through rather than taking the whole run down.
        log.error(f"Neotoma payment profiles unavailable: {exc}")
        profiles = []
    if profiles:
        return profiles

    log.info("No Neotoma payment profiles found — falling back to env vars")
    return load_profiles()


def load_profiles() -> list[PaymentProfile]:
    """
    Load all PaymentProfiles from env vars.
    Driven by MONEDULA_PROFILES (comma-separated prefix list).
    Returns empty list if MONEDULA_PROFILES is not set or empty.
    """
    raw = os.environ.get("MONEDULA_PROFILES", "").strip()
    if not raw:
        log.warning(
            "MONEDULA_PROFILES not set — no payment profiles loaded. "
            "Set e.g. MONEDULA_PROFILES=THERAPY,YOGA"
        )
        return []

    prefixes = [p.strip().upper() for p in raw.split(",") if p.strip()]
    profiles: list[PaymentProfile] = []

    for prefix in prefixes:
        profile = _load_profile(prefix)
        if profile:
            profiles.append(profile)

    log.info(f"Loaded {len(profiles)} payment profile(s): {[p.name for p in profiles]}")
    return profiles


def _load_profile(prefix: str) -> PaymentProfile | None:
    """Load a single PaymentProfile from env vars for the given prefix."""

    def env(key: str, default: str = "") -> str:
        return os.environ.get(f"{prefix}_{key}", default).strip()

    label = env("LABEL") or prefix.capitalize()
    keywords_raw = env("CALENDAR_KEYWORDS")
    calendar_keywords = [
        k.strip().lower() for k in keywords_raw.split(",") if k.strip()
    ]
    if not calendar_keywords:
        log.warning(f"[{prefix}] {prefix}_CALENDAR_KEYWORDS not set — profile skipped")
        return None

    payment_type_raw = env("PAYMENT_TYPE", "wise").lower()
    if payment_type_raw not in ("wise", "btc"):
        log.warning(
            f"[{prefix}] Unknown payment type {payment_type_raw!r} — profile skipped"
        )
        return None
    payment_type: Literal["wise", "btc"] = payment_type_raw  # type: ignore[assignment]

    amount_raw = env("AMOUNT_EUR", "0")
    try:
        amount_eur = parse_amount_eur(amount_raw)
    except PaymentAmountError as exc:
        log.error(
            f"[{prefix}] REFUSED {prefix}_AMOUNT_EUR={amount_raw!r}: {exc} — "
            f"profile skipped, no payment will be attempted until it is corrected"
        )
        return None

    if amount_eur <= 0:
        log.warning(
            f"[{prefix}] {prefix}_AMOUNT_EUR must be positive — profile skipped"
        )
        return None

    task_kw_raw = env("TASK_KEYWORDS", keywords_raw)
    task_keywords = [k.strip().lower() for k in task_kw_raw.split(",") if k.strip()]

    return PaymentProfile(
        prefix=prefix,
        label=label,
        calendar_keywords=calendar_keywords,
        payment_type=payment_type,
        amount_eur=amount_eur,
        # wise
        contact_id=env("CONTACT_ID"),
        contact_category=env("CONTACT_CATEGORY"),
        contact_platform=env("CONTACT_PLATFORM"),
        wise_reference=env("WISE_REFERENCE"),
        wise_legal_type=(env("WISE_LEGAL_TYPE") or "PRIVATE").strip().upper(),
        wise_iban=env("WISE_IBAN"),
        wise_recipient_name=env("WISE_RECIPIENT_NAME"),
        due_date=env("DUE_DATE"),
        one_off=env("ONE_OFF") == "1",
        # btc
        btc_address=env("BTC_ADDRESS"),
        # neotoma
        neotoma_task_id=env("NEOTOMA_TASK_ID"),
        task_keywords=task_keywords,
    )
