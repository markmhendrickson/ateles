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
