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
  <PREFIX>_AMOUNT_EUR     Transfer amount in EUR (integer)
  <PREFIX>_WISE_REFERENCE (wise only) Wise transfer reference string
  <PREFIX>_BTC_ADDRESS    (btc only) Destination BTC address
  <PREFIX>_NEOTOMA_TASK_ID    Neotoma task entity ID to update after payment
  <PREFIX>_TASK_KEYWORDS  (optional) Comma-separated keywords for Neotoma task search fallback

Profile list is driven by MONEDULA_PROFILES env var:
  MONEDULA_PROFILES=THERAPY,YOGA

Example .env additions:
  MONEDULA_PROFILES=THERAPY,YOGA
  THERAPY_LABEL=Therapy
  THERAPY_CALENDAR_KEYWORDS=therapy,terapia
  THERAPY_PAYMENT_TYPE=wise
  THERAPY_CONTACT_ID=578f6ce3-f9a4-4f
  THERAPY_CONTACT_CATEGORY=health
  THERAPY_CONTACT_PLATFORM=wise
  THERAPY_AMOUNT_EUR=60
  THERAPY_WISE_REFERENCE=Pago terapia
  THERAPY_NEOTOMA_TASK_ID=
  YOGA_LABEL=Yoga
  YOGA_CALENDAR_KEYWORDS=manel
  YOGA_PAYMENT_TYPE=btc
  YOGA_BTC_ADDRESS=bc1q7ce96cl9zmtwhgl9stsfvsv6fj8zdtrvta9raf
  YOGA_AMOUNT_EUR=60
  YOGA_NEOTOMA_TASK_ID=ent_4927189254ac1cd0232bf359
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Literal

log = logging.getLogger(__name__)


@dataclass
class PaymentProfile:
    prefix: str  # env var prefix, e.g. "THERAPY"
    label: str  # human label, e.g. "Therapy"
    calendar_keywords: list[str]  # event title match keywords
    payment_type: Literal["wise", "btc"]
    amount_eur: int

    # Wise-specific
    contact_id: str = ""  # Neotoma contact_id prefix for IBAN lookup
    contact_category: str = ""  # fallback: contacts.parquet category
    contact_platform: str = ""  # fallback: contacts.parquet platform
    wise_reference: str = ""  # Wise transfer reference

    # BTC-specific
    btc_address: str = ""

    # Neotoma task
    neotoma_task_id: str = ""
    task_keywords: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        """Unique slug used as handler name and in Telegram replies."""
        return self.prefix.lower()


def load_profiles_from_neotoma() -> list[PaymentProfile]:
    """
    Load PaymentProfiles from Neotoma payment_profile entities (Phase 5+).

    Queries Neotoma for active payment_profile entities belonging to this
    operator, constructs PaymentProfile objects from snapshot fields.

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
    base_url = os.environ.get(
        "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
    ).rstrip("/")

    if not bearer:
        log.debug("NEOTOMA_BEARER_TOKEN not set — skipping Neotoma profile load")
        return []

    try:
        url = f"{base_url}/entities?entity_type=payment_profile&limit=50&include_snapshots=true"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {bearer}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
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
        snap: dict = item.get("snapshot") or {}
        if snap.get("status", "active") not in ("active",):
            continue  # skip paused/archived

        label = snap.get("label", "")
        prefix = snap.get("prefix", label.upper().replace(" ", "_"))
        if not label:
            log.warning(
                f"payment_profile entity {item.get('entity_id')} missing label — skipped"
            )
            continue

        keywords_raw: list | str = snap.get("calendar_keywords", [])
        if isinstance(keywords_raw, str):
            try:
                keywords_raw = json.loads(keywords_raw)
            except (ValueError, TypeError):
                keywords_raw = [k.strip() for k in keywords_raw.split(",") if k.strip()]
        calendar_keywords = [str(k).strip().lower() for k in keywords_raw if k]

        if not calendar_keywords:
            log.warning(f"payment_profile {label!r} has no calendar_keywords — skipped")
            continue

        payment_type_raw = str(snap.get("payment_type", "wise")).lower()
        if payment_type_raw not in ("wise", "btc"):
            log.warning(
                f"payment_profile {label!r} unknown payment_type={payment_type_raw!r} — skipped"
            )
            continue
        payment_type: Literal["wise", "btc"] = payment_type_raw  # type: ignore[assignment]

        amount_raw = snap.get("amount_eur", 0)
        try:
            amount_eur = int(amount_raw)
        except (ValueError, TypeError):
            log.warning(
                f"payment_profile {label!r} invalid amount_eur={amount_raw!r} — skipped"
            )
            continue

        if amount_eur <= 0:
            log.warning(
                f"payment_profile {label!r} amount_eur must be positive — skipped"
            )
            continue

        task_kw_raw: list | str = snap.get("task_keywords", [])
        if isinstance(task_kw_raw, str):
            try:
                task_kw_raw = json.loads(task_kw_raw)
            except (ValueError, TypeError):
                task_kw_raw = [k.strip() for k in task_kw_raw.split(",") if k.strip()]
        task_keywords = [
            str(k).strip().lower() for k in task_kw_raw if k
        ] or calendar_keywords

        profiles.append(
            PaymentProfile(
                prefix=prefix,
                label=label,
                calendar_keywords=calendar_keywords,
                payment_type=payment_type,
                amount_eur=amount_eur,
                contact_id=snap.get("contact_id", ""),
                contact_category=snap.get("contact_category", ""),
                contact_platform=snap.get("contact_platform", ""),
                wise_reference=snap.get("wise_reference", ""),
                btc_address=snap.get("btc_address", ""),
                neotoma_task_id=snap.get("neotoma_task_id", ""),
                task_keywords=task_keywords,
            )
        )

    log.info(
        f"Loaded {len(profiles)} payment profile(s) from Neotoma: "
        f"{[p.name for p in profiles]}"
    )
    return profiles


def load_profiles_with_neotoma_fallback() -> list[PaymentProfile]:
    """
    Load PaymentProfiles: try Neotoma first, fall back to env vars.

    Phase 5 entrypoint. Monedula callers should use this instead of
    load_profiles() to transparently prefer Neotoma-sourced profiles.
    """
    profiles = load_profiles_from_neotoma()
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
        # btc
        btc_address=env("BTC_ADDRESS"),
        # neotoma
        neotoma_task_id=env("NEOTOMA_TASK_ID"),
        task_keywords=task_keywords,
    )
