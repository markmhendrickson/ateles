#!/usr/bin/env python3
"""
linkedin-enrichment / enrich — RGPD-minimized write-back to Neotoma contacts.

CAPTURE (from the task): present title, employer, location, notable recent
activity. APPEND a dated 'LinkedIn enrichment <date>:' line to the contact
`notes` (NEVER overwrite existing notes). Reuse linkedin_url if on file; else
the scrape supplies the resolved profile URL. Output is SILENT, store-only.

RGPD discipline (CLAUDE.md legitimate-interest):
  * minimize at capture — store only role/employer/location/professional
    activity that serves relationship management,
  * NO incidental Art. 9 sensitive categories (health, finances, family,
    political/religious views) ever land in a durable profile,
  * internal-only — no external publication without per-case operator approval.

`scrub_sensitive` is a best-effort filter on the free-text "recent activity"
field: it drops a line if it trips an Art. 9 keyword, since that field is the
one place a human-written LinkedIn post could leak a sensitive disclosure.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger("linkedin-enrichment.enrich")

# Marker prefix used both to write and to detect prior enrichment in notes.
ENRICH_MARKER = "LinkedIn enrichment"

# Art. 9 special-category cues. Conservative: if recent-activity text trips any,
# we drop that activity line rather than persist a sensitive inference.
_ART9_CUES = re.compile(
    r"\b("
    r"diagnos|cancer|illness|disease|surgery|therapy|disabilit|pregnan|"
    r"depress|anxiety|mental health|"
    r"bankrupt|debt|salary|divorce|custody|"
    r"muslim|christian|jewish|hindu|buddhist|atheist|church|mosque|synagogue|"
    r"republican|democrat|labour|tory|conservative party|political party|union member"
    r")\b",
    re.IGNORECASE,
)


@dataclass
class ProfileCapture:
    """Structured result of one LinkedIn profile scrape. Populated by the agent
    layer (Chrome MCP); consumed here. All fields optional — a partial capture
    still enriches what it can."""

    title: str | None = None
    employer: str | None = None
    location: str | None = None
    recent_activity: str | None = None
    linkedin_url: str | None = None


def scrub_sensitive(text: str | None) -> str | None:
    """Drop the value if it contains an Art. 9 special-category cue."""
    if not text:
        return None
    if _ART9_CUES.search(text):
        log.info("Dropping recent-activity line: tripped Art. 9 sensitive-category filter")
        return None
    return text.strip() or None


def format_enrichment_note(capture: ProfileCapture, enriched_on: str) -> str:
    """Build the single dated line appended to `notes`.

    Example:
      LinkedIn enrichment 2026-06-30: Head of Operations at Theodóre · Barcelona,
      Spain · recent: posted about retail automation.
    """
    parts: list[str] = []
    if capture.title and capture.employer:
        parts.append(f"{capture.title} at {capture.employer}")
    elif capture.title:
        parts.append(capture.title)
    elif capture.employer:
        parts.append(capture.employer)
    if capture.location:
        parts.append(capture.location)
    activity = scrub_sensitive(capture.recent_activity)
    if activity:
        parts.append(f"recent: {activity}")
    body = " · ".join(parts) if parts else "(no public professional data found)"
    return f"{ENRICH_MARKER} {enriched_on}: {body}"


def append_notes(existing: str | None, enrichment_line: str) -> str:
    """Append the enrichment line to existing notes, preserving everything.

    NEVER overwrites. Idempotent on the date marker: if a line for the same
    date already exists it is replaced (re-scrape on the same day), otherwise
    appended.
    """
    existing = (existing or "").rstrip()
    date_token = enrichment_line.split(":", 1)[0]  # "LinkedIn enrichment 2026-06-30"
    if existing:
        kept = [ln for ln in existing.splitlines() if not ln.startswith(date_token)]
        kept.append(enrichment_line)
        return "\n".join(kept)
    return enrichment_line


@dataclass
class EnrichmentWrite:
    """The set of contact-field corrections produced from one capture. Returned
    to the daemon, which applies them via Neotoma `correct` (one observation per
    field). Kept as data so it is previewable in dry-run / supervised mode."""

    contact_entity_id: str
    notes: str
    title: str | None = None
    company: str | None = None
    location: str | None = None
    linkedin_url: str | None = None


def build_write(
    *,
    contact_entity_id: str,
    existing_notes: str | None,
    existing_linkedin_url: str | None,
    capture: ProfileCapture,
    enriched_on: str,
) -> EnrichmentWrite:
    line = format_enrichment_note(capture, enriched_on)
    return EnrichmentWrite(
        contact_entity_id=contact_entity_id,
        notes=append_notes(existing_notes, line),
        title=capture.title or None,
        company=capture.employer or None,
        location=capture.location or None,
        # Reuse on-file URL; only set if scrape resolved a new one.
        linkedin_url=existing_linkedin_url or capture.linkedin_url or None,
    )
