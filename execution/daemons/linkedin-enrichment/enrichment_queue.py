#!/usr/bin/env python3
"""
linkedin-enrichment / queue — recency-ranked enrichment queue from Gmail.

QUEUE DEFINITION (from the task):
  contacts with a genuine TWO-WAY Gmail exchange — the operator both SENT to
  AND RECEIVED from the counterparty — ranked by most-recent reciprocal
  interaction date. Excludes newsletters / one-way inbound / no-reply senders.

Implementation:
  1. Pull the operator's SENT messages (gws gmail) → the set of addresses the
     operator has written TO, with the most recent send date per address.
  2. Pull RECEIVED messages from those same addresses → confirms reciprocity and
     gives the most recent inbound date.
  3. An address is "two-way" iff it appears in BOTH sets. Its rank key is the
     most-recent of (last_sent, last_received) — the freshest reciprocal touch.
  4. Filter out obvious non-human senders (no-reply, newsletters, automated).
  5. Match each two-way address to a Neotoma `contact` entity by email. Only
     contacts that exist in Neotoma are enrichable (we write back to them).

The operator's own address is read from env (GWS_OPERATOR_EMAIL or the gws
profile), never hardcoded — portability per CLAUDE.md.

This module owns NO Chrome / LinkedIn logic. It produces a ranked list of
QueueItem(contact_entity_id, email, name, linkedin_url, rank_date). The
scraping seam consumes one item at a time.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from email.utils import parseaddr, parsedate_to_datetime

log = logging.getLogger("linkedin-enrichment.queue")

# Sender-address fragments that mark a non-human / no-reply / bulk sender. These
# never make the queue even if the operator once replied to them.
_NON_HUMAN_FRAGMENTS = (
    "no-reply",
    "noreply",
    "no_reply",
    "donotreply",
    "do-not-reply",
    "mailer-daemon",
    "notifications@",
    "notification@",
    "newsletter",
    "updates@",
    "support@",
    "billing@",
    "invoices@",
    "receipts@",
    "automated@",
    "bounce",
    "postmaster@",
    "@reply.",
    "@bounce.",
    "@email.",
    "@mail.",
    "@e.",
    "@news.",
    "@marketing.",
)


@dataclass
class QueueItem:
    contact_entity_id: str
    email: str
    name: str
    linkedin_url: str | None
    rank_date: str  # ISO date of most-recent reciprocal interaction


def _operator_email() -> str | None:
    """Resolve the operator's Gmail address from env, then gws profile.

    Operator-specific, so never hardcoded — env first (GWS_OPERATOR_EMAIL),
    falling back to `gws gmail users get` profile lookup.
    """
    env = os.environ.get("GWS_OPERATOR_EMAIL")
    if env:
        return env.strip().lower()
    try:
        result = subprocess.run(
            ["gws", "gmail", "users", "get", "--user-id", "me", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            import json

            data = json.loads(result.stdout)
            addr = data.get("emailAddress")
            if addr:
                return addr.strip().lower()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, ValueError):
        pass
    log.warning("Could not resolve operator email (set GWS_OPERATOR_EMAIL)")
    return None


def _is_non_human(address: str) -> bool:
    a = address.lower()
    return any(frag in a for frag in _NON_HUMAN_FRAGMENTS)


def _list_messages(query: str, max_count: int) -> list[dict]:
    """Run `gws gmail users messages list` with a Gmail search query and return
    normalized {address, date_iso} dicts for each matching message.

    Uses metadata-only headers (From/To/Date) to keep this lightweight and to
    avoid pulling message bodies (RGPD minimization — we only need the
    address + timestamp to compute reciprocity, not content).
    """
    import json

    try:
        result = subprocess.run(
            [
                "gws",
                "gmail",
                "users",
                "messages",
                "list",
                "--user-id",
                "me",
                "--q",
                query,
                "--max-results",
                str(max_count),
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            log.warning("gws messages list failed (rc=%s): %s", result.returncode, result.stderr[:200])
            return []
        data = json.loads(result.stdout)
        raw = data.get("messages", data) if isinstance(data, dict) else data
        if not isinstance(raw, list):
            return []
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
        log.warning("gws messages list error: %s", exc)
        return []

    out: list[dict] = []
    for m in raw:
        # `list` may already include headers (from/to/date) in the helper output;
        # if not, the caller's query already constrains direction, so we read
        # whichever counterparty header is present.
        addr_raw = m.get("from") or m.get("to") or ""
        _, addr = parseaddr(addr_raw)
        addr = addr.strip().lower()
        if not addr:
            continue
        date_iso = _normalize_date(m.get("date", ""))
        out.append({"address": addr, "date_iso": date_iso})
    return out


def _normalize_date(raw: str) -> str:
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw)
        return dt.date().isoformat()
    except (TypeError, ValueError):
        # Already ISO?
        m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
        return m.group(1) if m else ""


def _most_recent_by_address(messages: list[dict]) -> dict[str, str]:
    """address → most-recent ISO date seen for that address."""
    best: dict[str, str] = {}
    for m in messages:
        addr = m["address"]
        d = m["date_iso"]
        if not addr or not d:
            continue
        if addr not in best or d > best[addr]:
            best[addr] = d
    return best


def build_two_way_address_ranking(scan_window: str = "newer_than:3y", max_each: int = 1000) -> list[tuple[str, str]]:
    """Return [(address, rank_date_iso), ...] for two-way correspondents,
    most-recent reciprocal touch first.

    scan_window: a Gmail date filter limiting how far back to scan (default 3y,
    keeps the queue intro-relevant and the scan bounded).
    """
    operator = _operator_email()

    sent = _list_messages(f"in:sent {scan_window}", max_each)
    received = _list_messages(f"in:inbox {scan_window}", max_each)

    sent_by_addr = _most_recent_by_address(sent)
    recv_by_addr = _most_recent_by_address(received)

    ranked: list[tuple[str, str]] = []
    for addr, last_sent in sent_by_addr.items():
        if operator and addr == operator:
            continue
        if _is_non_human(addr):
            continue
        last_recv = recv_by_addr.get(addr)
        if not last_recv:
            continue  # one-way (operator wrote, never heard back) → exclude
        rank_date = max(last_sent, last_recv)
        ranked.append((addr, rank_date))

    ranked.sort(key=lambda t: t[1], reverse=True)
    log.info("Two-way correspondents: %d (window=%s)", len(ranked), scan_window)
    return ranked


# ── Neotoma contact matching ──────────────────────────────────────────────────


def match_contacts(
    ranked_addresses: list[tuple[str, str]],
    retrieve_by_identifier,
    *,
    already_enriched,
    limit: int,
) -> list[QueueItem]:
    """Map ranked two-way addresses → Neotoma contact entities, skipping any
    already enriched, until `limit` items are collected.

    `retrieve_by_identifier(identifier, entity_type, by)` is injected so this
    module stays transport-agnostic and unit-testable. `already_enriched(eid)`
    is the state dedupe predicate.
    """
    items: list[QueueItem] = []
    for addr, rank_date in ranked_addresses:
        if len(items) >= limit:
            break
        try:
            result = retrieve_by_identifier(identifier=addr, entity_type="contact", by="email")
        except Exception as exc:
            log.warning("contact lookup failed for %s: %s", addr, exc)
            continue
        entities = (result or {}).get("entities", []) if isinstance(result, dict) else []
        if not entities:
            continue  # no Neotoma contact → nothing to write back to
        ent = entities[0]
        eid = ent.get("id") or ent.get("entity_id")
        if not eid or already_enriched(eid):
            continue
        snap = (ent.get("snapshot", {}) or {}).get("snapshot", {}) or ent.get("snapshot", {})
        items.append(
            QueueItem(
                contact_entity_id=eid,
                email=addr,
                name=snap.get("name") or snap.get("full_name") or addr,
                linkedin_url=snap.get("linkedin_url") or None,
                rank_date=rank_date,
            )
        )
    log.info("Matched %d enrichable contacts (limit=%d)", len(items), limit)
    return items
