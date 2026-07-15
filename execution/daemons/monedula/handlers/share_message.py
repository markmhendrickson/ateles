"""
handlers/share_message.py — premade payee-facing message for a completed payment.

Operator standing rule: every payment confirmation carries a ready-to-send
message the operator can copy-paste to the recipient (WhatsApp/SMS), including a
block-explorer link when the payment was on-chain.

The message is written in the payee's language, resolved from the `locale_profile`
entity (`language`), defaulting to Spanish — the operator's locale. It is
deliberately plain text with no markdown, so it pastes cleanly into a messenger.

Contains no secrets: an on-chain txid and explorer URL are already public, and a
Wise transfer id is meaningless without account access. IBANs are NEVER included.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


_LANG_ALIASES = {
    "english": "en", "en": "en",
    "spanish": "es", "español": "es", "castellano": "es", "es": "es",
    "catalan": "ca", "català": "ca", "ca": "ca",
}


def _normalize_lang(value: str) -> str:
    """Map a language name or code ('Spanish', 'es', 'Català') to a 2-letter code."""
    v = str(value or "").strip().lower()
    return _LANG_ALIASES.get(v, v[:2])


def payee_language(contact_id: str = "", default: str = "es") -> str:
    """Resolve the language to write the PAYEE in.

    Order: the payee's own contact entity (`language`), then the operator's
    locale_profile `secondary_languages` (the local language they transact in),
    then `default`.

    NOT the operator's primary language: this message is for the payee. Resolving
    the operator's locale here produced English messages for Spanish-speaking
    payees.
    """
    if contact_id:
        snap = _fetch_entity_snapshot(contact_id)
        if snap:
            lang = snap.get("language") or snap.get("preferred_language")
            if lang:
                return _normalize_lang(str(lang))

    secondary = _locale_field("secondary_languages")
    if secondary:
        if isinstance(secondary, list) and secondary:
            return _normalize_lang(str(secondary[0]))
        if isinstance(secondary, str) and secondary.strip():
            first = secondary.replace(",", " ").split()
            if first:
                return _normalize_lang(first[0])
    return default


def _fetch_entity_snapshot(entity_id: str) -> dict | None:
    """Fetch a Neotoma entity snapshot by id. None on any error."""
    base_url = os.environ.get("NEOTOMA_BASE_URL", "http://localhost:3180").rstrip("/")
    bearer = os.environ.get("NEOTOMA_BEARER_TOKEN", "").strip()
    is_loopback = "localhost" in base_url or "127.0.0.1" in base_url
    try:
        headers = {"Accept": "application/json"}
        if bearer and not is_loopback:
            headers["Authorization"] = f"Bearer {bearer}"
        req = urllib.request.Request(f"{base_url}/entities/{entity_id}",
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            entity = json.loads(resp.read())
        snap = entity.get("snapshot") or entity
        if isinstance(snap.get("snapshot"), dict):
            snap = snap["snapshot"]
        return snap if isinstance(snap, dict) else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _locale_field(field: str):
    """Return one field from the operator's locale_profile entity, or None."""
    base_url = os.environ.get("NEOTOMA_BASE_URL", "http://localhost:3180").rstrip("/")
    bearer = os.environ.get("NEOTOMA_BEARER_TOKEN", "").strip()
    is_loopback = "localhost" in base_url or "127.0.0.1" in base_url
    try:
        body = json.dumps({
            "entity_type": "locale_profile", "limit": 1, "include_snapshots": True
        }).encode()
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if bearer and not is_loopback:
            headers["Authorization"] = f"Bearer {bearer}"
        req = urllib.request.Request(f"{base_url}/entities/query", data=body,
                                     method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        items = data.get("entities") or data.get("items") or []
        if items:
            snap = items[0].get("snapshot") or {}
            snap = snap.get("snapshot", snap)
            return snap.get(field)
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError):
        pass
    return None


def _operator_language(default: str = "es") -> str:
    """Deprecated: resolves the OPERATOR's language. Use payee_language()."""
    base_url = os.environ.get("NEOTOMA_BASE_URL", "http://localhost:3180").rstrip("/")
    bearer = os.environ.get("NEOTOMA_BEARER_TOKEN", "").strip()
    is_loopback = "localhost" in base_url or "127.0.0.1" in base_url
    try:
        body = json.dumps({
            "entity_type": "locale_profile", "limit": 1, "include_snapshots": True
        }).encode()
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if bearer and not is_loopback:
            headers["Authorization"] = f"Bearer {bearer}"
        req = urllib.request.Request(f"{base_url}/entities/query", data=body,
                                     method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        items = data.get("entities") or data.get("items") or []
        if items:
            snap = items[0].get("snapshot") or {}
            snap = snap.get("snapshot", snap)
            return str(snap.get("language") or default).lower()[:2]
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError):
        pass
    return default


def build_share_message(
    *,
    amount_eur,
    label: str,
    rail: str,
    explorer_url: str = "",
    session_date: str = "",
    language: str | None = None,
    contact_id: str = "",
) -> str:
    """Return a plain-text message the operator can paste to the payee.

    `rail` is "btc" or "wise". For BTC the explorer URL is included so the payee
    can verify the transfer themselves; for Wise there is no public link, so the
    message just states the transfer was sent.

    Language is the PAYEE's (from their contact entity), not the operator's.
    """
    lang = _normalize_lang(language) if language else payee_language(contact_id)
    amt = f"{amount_eur}"
    when = f" de la sesión del {session_date}" if session_date else ""
    when_en = f" for the {session_date} session" if session_date else ""

    if lang == "es":
        msg = f"¡Hola! Te acabo de enviar {amt}€{when} 🙏"
        if explorer_url:
            msg += f"\nPuedes verlo aquí: {explorer_url}"
        msg += "\n¡Gracias!"
        return msg

    if lang == "ca":
        msg = f"Hola! Acabo d'enviar-te {amt}€{when} 🙏"
        if explorer_url:
            msg += f"\nHo pots veure aquí: {explorer_url}"
        msg += "\nGràcies!"
        return msg

    msg = f"Hi! I've just sent you €{amt}{when_en} 🙏"
    if explorer_url:
        msg += f"\nYou can verify it here: {explorer_url}"
    msg += "\nThanks!"
    return msg
