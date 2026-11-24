"""
handlers/payment_profile.py — Generic recurring payment profile loader.

A PaymentProfile is a dict of configuration loaded entirely from env vars.
No business-specific values are hardcoded here.

Each profile is identified by a PREFIX (e.g. "THERAPY", "YOGA") and reads:
  <PREFIX>_LABEL          Human-readable label for Telegram messages
  <PREFIX>_CALENDAR_KEYWORDS  Comma-separated keywords to match against calendar event titles
  <PREFIX>_CALENDAR_RECURRING_EVENT_ID  Optional exact calendar series id
  <PREFIX>_PAYMENT_TYPE   "wise" | "btc"
  <PREFIX>_CONTACT_ID     (wise only) Neotoma contact_id prefix for IBAN lookup
  <PREFIX>_CONTACT_CATEGORY  (wise only) Fallback category for contact lookup
  <PREFIX>_CONTACT_PLATFORM  (wise only) Fallback platform for contact lookup
  <PREFIX>_AMOUNT_EUR     Transfer amount in EUR (integer)
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

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Literal

try:
    from ..handler_base import valid_configured_series_id
except ImportError:
    from handler_base import valid_configured_series_id

# Cloudflare fronts the hosted Neotoma instance and blocks urllib's default
# User-Agent with a 1010 "browser signature" 403. Any explicit UA passes.
NEOTOMA_USER_AGENT = "ateles-neotoma-sync/1.0"

log = logging.getLogger(__name__)

# Wise accepts only these legalType values when creating an IBAN recipient.
_WISE_LEGAL_TYPES = {"PRIVATE", "BUSINESS"}


@dataclass
class PaymentProfile:
    prefix: str  # env var prefix, e.g. "THERAPY"
    label: str  # human label, e.g. "Therapy"
    calendar_keywords: list[str]  # event title match keywords
    payment_type: Literal["wise", "btc"]
    amount_eur: int
    calendar_recurring_event_id: str = ""  # optional exact calendar series id

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
    # The Wise recipient account to pay, when the profile pins one. Stored as
    # a number or a numeric string; always normalised to an int. When set, the
    # handler pays exactly this account and never creates a recipient.
    wise_recipient_id: int | None = None

    # BTC-specific
    btc_address: str = ""

    # One-off payments: due on a date rather than gated by a calendar event.
    # A profile with a due_date and no calendar_keywords is a one-off invoice;
    # it matches on the date instead of on attendance at a session.
    due_date: str = ""  # ISO YYYY-MM-DD; empty for recurring profiles
    one_off: bool = False  # archive the profile after a successful transfer
    entity_id: str = ""  # Neotoma payment_profile entity id (for archiving)

    # Neotoma task
    neotoma_task_id: str = ""
    task_keywords: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        """Unique slug used as handler name and in Telegram replies."""
        return self.prefix.lower()


def parse_wise_recipient_id(raw: Any) -> int | None:
    """Normalise a stored ``wise_recipient_id`` to a positive int.

    Accepts an int, an integral float, or a numeric string. Empty/None means
    "not set" (returns None). Anything else raises ``ValueError``: a recipient
    id that does not parse must fail closed, never be guessed at.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValueError(f"wise_recipient_id {raw!r} is not a number")
    if isinstance(raw, float):
        if raw != int(raw):
            raise ValueError(f"wise_recipient_id {raw!r} is not an integer")
        raw = int(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        if not text.isdigit():
            raise ValueError("wise_recipient_id is not numeric")
        raw = int(text)
    if not isinstance(raw, int) or raw <= 0:
        raise ValueError("wise_recipient_id must be a positive integer")
    return raw


def parse_amount_eur(raw: Any) -> int:
    """Normalise a stored ``amount_eur`` to a whole number of euros.

    Accepts an int, an integral float, or a string of plain digits. A boolean, a fractional or
    non-finite number, a string that is not plain digits, and anything else raises ``ValueError``
    or ``TypeError``: an amount must never be coerced (a boolean would read as 1 and a fraction
    would be truncated).
    """
    if isinstance(raw, bool):
        raise TypeError("amount_eur must be a number, not a boolean")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if raw != raw or raw in (float("inf"), float("-inf")) or not raw.is_integer():
            raise ValueError("amount_eur must be a whole number")
        return int(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if not re.fullmatch(r"[+-]?[0-9]+", text):
            raise ValueError("amount_eur must be plain digits")
        return int(text)
    raise TypeError("amount_eur must be a number")


def _text_field(snap: dict, name: str) -> str:
    """A stored text field: missing or null reads as empty text; anything that is not text (a
    mapping, a list, a number, a boolean) is malformed and raises TypeError so the caller holds
    that one profile instead of passing the value through."""
    value = snap.get(name, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text")
    return value


def _keyword_list(raw: Any) -> list[str]:
    """Parse a stored keyword collection: a list of text, a JSON list, or a comma-separated string.

    Anything else (a mapping, a number, a boolean, a null, a JSON object or scalar, a list that
    holds non-text) is malformed and raises TypeError, so the caller holds that one profile with
    a malformed-profile stranding instead of coercing the value (iterating a mapping would
    silently load its keys as keywords).
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = [k.strip() for k in raw.split(",") if k.strip()]
    if not isinstance(raw, (list, tuple)):
        raise TypeError("keywords must be a list of text")
    if not all(isinstance(k, str) for k in raw if k):
        raise TypeError("keywords must be text")
    return [k.strip().lower() for k in raw if k and k.strip()]


def load_profiles_from_neotoma(
    strandings: list | None = None,
) -> list[PaymentProfile]:
    """
    Load PaymentProfiles from Neotoma payment_profile entities (Phase 5+).

    Queries Neotoma for active payment_profile entities belonging to this
    operator, constructs PaymentProfile objects from snapshot fields.

    Returns an empty list on any error. ``load_profiles_with_neotoma_fallback`` decides
    whether env-var profiles may be used: only when no Neotoma base URL is configured.

    ``strandings``, when supplied, is appended to with one ``Stranding`` per
    ACTIVE profile that was rejected, and one for a failed fetch. Every
    rejection below used to be a bare ``log.warning(...); continue``: the run
    exited clean and the only trace was a line in an 8 MB log file, so a
    payment that could not fire looked exactly like a day with nothing due
    (ateles#553). The caller escalates whatever lands in this list.

    Passing ``None`` keeps the historical warn-and-continue behaviour, which
    older callers expect.

    Required env vars:
      NEOTOMA_BEARER_TOKEN   Neotoma API auth token
      NEOTOMA_BASE_URL       Neotoma API base URL
    """
    import json
    import urllib.error
    import urllib.request

    from strandings import (
        REASON_BAD_AMOUNT,
        REASON_BAD_LEGAL_TYPE,
        REASON_BAD_PAYMENT_TYPE,
        REASON_BAD_RECIPIENT_ID,
        REASON_BAD_CALENDAR_SERIES_ID,
        NEOTOMA_TIMEOUT_SEC,
        REASON_FETCH_FAILED,
        REASON_FETCH_REJECTED,
        REASON_MALFORMED_PROFILE,
        REASON_MISSING_LABEL,
        REASON_UNREACHABLE,
        Stranding,
    )

    def _strand(entity_id: str, label: str, reason: str, detail: str) -> None:
        """Record a rejected ACTIVE profile so the caller can escalate it."""
        if strandings is None:
            return
        strandings.append(
            Stranding(
                entity_id=str(entity_id or ""),
                label=label,
                reason=reason,
                detail=detail,
            )
        )

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
        # Bounded retry (budget and rationale: strandings.NEOTOMA_*). Only a
        # transport error, timeout or HTTP 5xx is retried; a 4xx is reported
        # at once.
        from strandings import call_with_retry

        def _fetch():
            with urllib.request.urlopen(req, timeout=NEOTOMA_TIMEOUT_SEC) as resp:
                return json.loads(resp.read())

        data = call_with_retry(_fetch, "Neotoma payment_profile fetch")
    except urllib.error.HTTPError as exc:
        # An auth/transport rejection is NOT "no profiles configured". Returning
        # [] quietly here is how every scheduled run since the hosted migration
        # reported success while paying nothing: Cloudflare answered 403 and the
        # daemon could not tell that apart from an empty result. Log at ERROR so
        # it is visible, and name the two causes worth checking first.
        log.error(
            f"Neotoma payment_profile fetch REJECTED: HTTP {exc.code} — no profiles "
            f"loaded from Neotoma this run. "
            f"Check NEOTOMA_BEARER_TOKEN (401/403) and that the request carries a "
            f"User-Agent (Cloudflare 1010 blocks urllib's default)."
        )
        if exc.code >= 500:
            # Still failing after the bounded retries: an outage, not a
            # rejection. Transient by the same reasoning as a timeout.
            _strand(
                "",
                "(all profiles)",
                REASON_FETCH_FAILED,
                "Neotoma unreachable (server error); payment profiles not loaded "
                "this run, so none can be proposed; retrying next tick.",
            )
        else:
            # A 4xx never clears by itself: reported at once, with its remedy.
            _strand(
                "",
                "(all profiles)",
                REASON_FETCH_REJECTED,
                f"Neotoma rejected the payment profile fetch with HTTP {exc.code}. "
                "Check NEOTOMA_BEARER_TOKEN and the request User-Agent.",
            )
        return []
    except json.JSONDecodeError as exc:
        log.error(f"Neotoma payment_profile fetch returned an unreadable body: {exc}")
        _strand(
            "",
            "(all profiles)",
            REASON_FETCH_REJECTED,
            "Neotoma answered the payment profile fetch with an unreadable "
            "response. Check the Neotoma host and any proxy in front of it.",
        )
        return []
    except (urllib.error.URLError, OSError) as exc:
        # A transport failure is not "no profiles configured" either. This
        # branch fired 1,753 times at WARNING and escalated nothing: during
        # those windows every profile was stranded at once, which is strictly
        # worse than the single-profile case below.
        log.error(f"Neotoma payment_profile fetch failed: {type(exc).__name__}")
        _strand(
            "",
            "(all profiles)",
            REASON_FETCH_FAILED,
            "Neotoma unreachable; payment profiles not loaded this run, so none "
            "can be proposed; retrying next tick.",
        )
        return []

    # A successful response must carry an explicit profile collection (possibly empty). A body
    # without one (an error object, an unexpected shape) is reported as a rejected read so the
    # caller escalates it; it is never read as "no profiles configured".
    items: list[dict] | None = None
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        # Exactly ONE recognised collection key may be present, and it must be a list: a
        # malformed value under one key, or two recognised keys that could disagree, is a
        # rejected read, never a pick of whichever key happens to look valid.
        present = [key for key in ("entities", "items", "results") if key in data]
        if len(present) == 1 and isinstance(data[present[0]], list):
            items = data[present[0]]
    if (
        items is None
        or not all(isinstance(entry, dict) for entry in items)
        # a truthy non-object snapshot is a malformed record: reject the whole read
        or not all(isinstance(entry.get("snapshot") or {}, dict) for entry in items)
    ):
        log.error("Neotoma payment_profile fetch returned no valid profile collection")
        _strand(
            "",
            "(all profiles)",
            REASON_FETCH_REJECTED,
            "Neotoma answered the payment profile fetch without a valid profile "
            "collection. Check the Neotoma host and any proxy in front of it.",
        )
        return []

    if not items:
        # An empty collection from a configured source cannot be told from a failed read (a 2xx
        # body can dress either one in any metadata), so it is reported as a rejected read: the
        # caller then neither claims the day nor clears pending consent.
        log.warning("Neotoma returned an empty payment profile collection")
        _strand(
            "",
            "(all profiles)",
            REASON_FETCH_REJECTED,
            "Neotoma returned no payment profile. An empty answer cannot be told from a "
            "failed one, so no profiles were loaded this run. Check the Neotoma host and "
            "that the payment profiles exist.",
        )
        return []

    profiles: list[PaymentProfile] = []

    def _parse_item(item: dict) -> None:
        snap: dict = item.get("snapshot") or {}
        if snap.get("status", "active") not in ("active",):
            return  # skip paused/archived

        label = snap.get("label", "")
        prefix = snap.get("prefix", label.upper().replace(" ", "_"))
        if (label and not isinstance(label, str)) or not isinstance(prefix, str):
            raise TypeError("label and prefix must be text")
        if not label:
            log.warning(
                f"payment_profile entity {item.get('entity_id')} missing label — skipped"
            )
            _strand(
                item.get("entity_id"),
                f"(unlabelled {item.get('entity_id')})",
                REASON_MISSING_LABEL,
                "Active payment_profile has no label, so it cannot be identified "
                "to the operator or matched to a handler.",
            )
            return

        calendar_keywords = _keyword_list(snap.get("calendar_keywords", []))
        recurring_id = snap.get("calendar_recurring_event_id")
        if not valid_configured_series_id(recurring_id):
            log.warning("payment_profile has malformed calendar series id — skipped")
            _strand(
                item.get("entity_id"), label, REASON_BAD_CALENDAR_SERIES_ID,
                "Calendar series id is invalid; this profile was held. Check its type, length and control characters.",
            )
            return

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
            _strand(
                item.get("entity_id"),
                label,
                REASON_UNREACHABLE,
                "Profile has neither calendar_keywords (the recurring, "
                "attendance-gated trigger) nor due_date (the one-off trigger), so "
                "matches() can never fire for it. Set one of the two.",
            )
            return

        payment_type_raw = str(snap.get("payment_type", "wise")).lower()
        if payment_type_raw not in ("wise", "btc"):
            log.warning(
                f"payment_profile {label!r} unknown payment_type={payment_type_raw!r} — skipped"
            )
            _strand(
                item.get("entity_id"),
                label,
                REASON_BAD_PAYMENT_TYPE,
                f"payment_type={payment_type_raw!r} is not a rail Monedula can "
                f"execute (expected 'wise' or 'btc').",
            )
            return
        payment_type: Literal["wise", "btc"] = payment_type_raw  # type: ignore[assignment]

        amount_raw = snap.get("amount_eur", 0)
        try:
            amount_eur = parse_amount_eur(amount_raw)
        except (ValueError, TypeError, OverflowError):
            log.warning(
                f"payment_profile {label!r} invalid amount_eur={amount_raw!r} — skipped"
            )
            _strand(
                item.get("entity_id"),
                label,
                REASON_BAD_AMOUNT,
                "amount_eur is not a whole number of euros, so no transfer can "
                "be constructed from this profile.",
            )
            return

        if amount_eur <= 0:
            log.warning(
                f"payment_profile {label!r} amount_eur must be positive — skipped"
            )
            _strand(
                item.get("entity_id"),
                label,
                REASON_BAD_AMOUNT,
                "amount_eur must be positive; the profile is active but "
                "describes no payable amount.",
            )
            return

        task_keywords = _keyword_list(snap.get("task_keywords", [])) or calendar_keywords

        legal_type_raw = str(snap.get("wise_legal_type") or "PRIVATE").strip().upper()
        if legal_type_raw not in _WISE_LEGAL_TYPES:
            log.warning(
                f"payment_profile {label!r} invalid wise_legal_type="
                f"{legal_type_raw!r} (expected one of {sorted(_WISE_LEGAL_TYPES)}) "
                f"— skipped"
            )
            _strand(
                item.get("entity_id"),
                label,
                REASON_BAD_LEGAL_TYPE,
                f"wise_legal_type={legal_type_raw!r} is not accepted by Wise "
                f"(expected one of {sorted(_WISE_LEGAL_TYPES)}).",
            )
            return

        try:
            recipient_id = parse_wise_recipient_id(snap.get("wise_recipient_id"))
        except ValueError as exc:
            log.error(f"payment_profile {label!r}: {exc} — skipped")
            _strand(
                item.get("entity_id"),
                label,
                REASON_BAD_RECIPIENT_ID,
                f"{exc}. Payments for this profile are held until it is fixed.",
            )
            return

        profile = (
            PaymentProfile(
                prefix=prefix,
                label=label,
                calendar_keywords=calendar_keywords,
                calendar_recurring_event_id=recurring_id or "",
                payment_type=payment_type,
                amount_eur=amount_eur,
                contact_id=_text_field(snap, "contact_id"),
                contact_category=_text_field(snap, "contact_category"),
                contact_platform=_text_field(snap, "contact_platform"),
                wise_reference=_text_field(snap, "wise_reference"),
                wise_legal_type=legal_type_raw,
                wise_iban=str(snap.get("wise_iban") or "").strip(),
                wise_recipient_name=str(snap.get("wise_recipient_name") or "").strip(),
                wise_recipient_id=recipient_id,
                btc_address=_text_field(snap, "btc_address"),
                due_date=due_date,
                one_off=one_off,
                entity_id=str(item.get("entity_id") or "").strip(),
                neotoma_task_id=_text_field(snap, "neotoma_task_id"),
                task_keywords=task_keywords,
            )
        )
        # derived properties must work before the profile is accepted, so a malformed field can
        # never escape later (for example the profile's name)
        profile.name
        profiles.append(profile)

    for item in items:
        try:
            _parse_item(item)
        except Exception as exc:  # noqa: BLE001 - one malformed entry must never abort the read
            # A malformed field must hold that one profile and be reported, never raise out of the
            # whole read.
            log.error(
                f"payment_profile {item.get('entity_id')} could not be read "
                f"({type(exc).__name__}) — skipped"
            )
            _strand(
                item.get("entity_id"),
                f"(unreadable {item.get('entity_id')})",
                REASON_MALFORMED_PROFILE,
                "An active payment_profile has a malformed field and could not be read; it is "
                "held until it is fixed.",
            )

    log.info(
        f"Loaded {len(profiles)} payment profile(s) from Neotoma: "
        f"{[p.name for p in profiles]}"
    )
    return profiles


def load_profiles_with_neotoma_fallback(
    strandings: list | None = None,
) -> list[PaymentProfile]:
    """
    Load PaymentProfiles from the configured source: Neotoma, or env vars only when no Neotoma
    base URL is configured.

    Phase 5 entrypoint. Monedula callers should use this instead of
    load_profiles() to transparently prefer Neotoma-sourced profiles.

    When a Neotoma base URL IS configured, env-var profiles are NEVER used, whatever the read
    returned (a failure, a rejected or malformed response, no profile, or only paused ones).
    An env-var profile carries no calendar series binding, so it would resume title-only
    matching for a payment whose configured profile requires a series, or resurrect a paused
    payment. A failed read is reported as a stranding for the caller to escalate.

    ``strandings`` is forwarded to the Neotoma loader; see its docstring.
    """
    if not os.environ.get("NEOTOMA_BASE_URL", "").strip():
        # An env-only install: env-var profiles are the only source.
        log.info("No Neotoma source configured — using env-var payment profiles")
        return load_profiles(strandings)

    collected: list = []
    try:
        profiles = load_profiles_from_neotoma(collected)
    except RuntimeError as exc:
        log.error(f"Neotoma payment profiles unavailable: {exc}")
        profiles = []
        from strandings import REASON_FETCH_FAILED, Stranding

        collected.append(Stranding(
            entity_id="", label="(all profiles)", reason=REASON_FETCH_FAILED,
            detail="The configured Neotoma source failed during the payment profile "
                   "read; no profiles were loaded this run, so none can be proposed.",
        ))
    if strandings is not None:
        strandings.extend(collected)
    if not profiles:
        log.warning(
            "Neotoma is configured but yielded no usable payment profile; "
            "env-var profiles are not used when a Neotoma source is configured"
        )
    return profiles


def load_profiles(strandings: list | None = None) -> list[PaymentProfile]:
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
        profile = _load_profile(prefix, strandings)
        if profile:
            profiles.append(profile)

    log.info(f"Loaded {len(profiles)} payment profile(s): {[p.name for p in profiles]}")
    return profiles


def _load_profile(
    prefix: str, strandings: list | None = None
) -> PaymentProfile | None:
    """Load a single PaymentProfile from env vars for the given prefix.

    A bad ``WISE_RECIPIENT_ID`` is recorded in ``strandings`` (when supplied),
    exactly as the Neotoma loader does, so it alerts instead of only logging.
    """

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

    recurring_id = os.environ.get(f"{prefix}_CALENDAR_RECURRING_EVENT_ID", "")
    if not valid_configured_series_id(recurring_id):
        log.warning(f"[{prefix}] Invalid calendar series id — profile skipped")
        if strandings is not None:
            from strandings import REASON_BAD_CALENDAR_SERIES_ID, Stranding

            strandings.append(Stranding(
                entity_id="", label=label, reason=REASON_BAD_CALENDAR_SERIES_ID,
                detail="Calendar series id is invalid; this profile was held. Check its type, length and control characters.",
            ))
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
        amount_eur = int(amount_raw)
    except ValueError:
        log.warning(
            f"[{prefix}] Invalid {prefix}_AMOUNT_EUR={amount_raw!r} — profile skipped"
        )
        return None

    if amount_eur <= 0:
        log.warning(
            f"[{prefix}] {prefix}_AMOUNT_EUR must be positive — profile skipped"
        )
        return None

    task_kw_raw = env("TASK_KEYWORDS", keywords_raw)
    task_keywords = [k.strip().lower() for k in task_kw_raw.split(",") if k.strip()]

    try:
        recipient_id = parse_wise_recipient_id(env("WISE_RECIPIENT_ID"))
    except ValueError as exc:
        log.error(f"[{prefix}] {exc} — profile skipped")
        if strandings is not None:
            from strandings import REASON_BAD_RECIPIENT_ID, Stranding

            strandings.append(
                Stranding(
                    entity_id="",
                    label=label,
                    reason=REASON_BAD_RECIPIENT_ID,
                    detail=f"{exc}. Payments for this profile are held until it is fixed.",
                )
            )
        return None

    return PaymentProfile(
        prefix=prefix,
        label=label,
        calendar_keywords=calendar_keywords,
        calendar_recurring_event_id=recurring_id,
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
        wise_recipient_id=recipient_id,
        due_date=env("DUE_DATE"),
        one_off=env("ONE_OFF") == "1",
        # btc
        btc_address=env("BTC_ADDRESS"),
        # neotoma
        neotoma_task_id=env("NEOTOMA_TASK_ID"),
        task_keywords=task_keywords,
    )
