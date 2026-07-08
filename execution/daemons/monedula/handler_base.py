"""
handler_base.py — Abstract base class for Monedula payment handlers.

Each handler encapsulates a single recurring payment obligation that is
triggered by calendar events.  Subclasses implement three methods:

  matches(events)  → list of match dicts (one per triggered event)
  preview(match)   → human-readable preview string for Telegram
  execute(match)   → executes the payment, returns a result dict

The result dict returned by execute() must include at least:
  {
    "status":   "sent" | "failed" | "manual_required",
    "handler":  <handler name>,
    ...handler-specific fields...
  }
"""

from __future__ import annotations

import abc
import logging
from typing import Any

log = logging.getLogger(__name__)


def match_events_for_profile(profile: Any, events: list[dict], handler_name: str) -> list[dict]:
    """
    Shared event-matching logic for handlers whose PaymentProfile may
    configure a stable calendar_recurring_event_id and/or an explicit
    calendar_event_ids allowlist.

    Matching precedence:
      1. If profile.calendar_recurring_event_id is set, select ONLY events
         whose recurringEventId equals it (recurring-series instances), or
         whose own id equals it (the defining/original event, which has no
         recurringEventId of its own but IS the series root in some
         calendars) — title keywords are ignored entirely.
      2. Else if profile.calendar_event_ids is a non-empty explicit
         allowlist, select ONLY events whose id is in that set — title
         keywords are ignored entirely.
      3. Else (no id configured), fall back to calendar_keywords substring
         matching against the event title — the legacy behaviour, kept so
         unconfigured profiles keep working.

    This exists so two events with an overlapping keyword (e.g. "Therapy
    in-person" and "Walk to therapy" both containing "therapy") can be told
    apart once a stable id is available, without duplicating the id/keyword
    branch in every handler's matches().
    """
    recurring_id = getattr(profile, "calendar_recurring_event_id", "") or ""
    event_id_allowlist = set(getattr(profile, "calendar_event_ids", None) or [])

    matched: list[dict] = []

    if recurring_id:
        for event in events:
            event_recurring_id = str(event.get("recurringEventId") or "")
            event_own_id = str(event.get("id") or "")
            if event_recurring_id == recurring_id or event_own_id == recurring_id:
                summary = event.get("summary", "") or ""
                log.info(f"[{handler_name}] Matched event by recurringEventId: {summary!r}")
                matched.append({"event": event, "summary": summary})
        return matched

    if event_id_allowlist:
        for event in events:
            if str(event.get("id") or "") in event_id_allowlist:
                summary = event.get("summary", "") or ""
                log.info(f"[{handler_name}] Matched event by event-id allowlist: {summary!r}")
                matched.append({"event": event, "summary": summary})
        return matched

    # Fallback: keyword match against the event title. Default is substring
    # ("therapy" matches anywhere in the title); "prefix" mode requires the
    # title to START WITH the keyword, which separates a payable session from
    # an incidental event that merely mentions it (e.g. "Therapy in-person"
    # matches the "therapy" prefix while "Walk to therapy" does not).
    # Drop empty/whitespace keywords: "" makes both `kw in low` and
    # `low.startswith("")` True for EVERY event, which would match (and could
    # pay for) unrelated sessions. Loaders already filter these, but guard here
    # too since this is a payment-matching path.
    calendar_keywords = [
        kw for kw in (getattr(profile, "calendar_keywords", None) or []) if kw and kw.strip()
    ]
    match_mode = (getattr(profile, "keyword_match_mode", "") or "substring").lower()
    for event in events:
        summary = event.get("summary", "") or ""
        low = summary.lower()
        if match_mode == "prefix":
            hit = any(low.startswith(kw) for kw in calendar_keywords)
        else:
            hit = any(kw in low for kw in calendar_keywords)
        if hit:
            log.info(
                f"[{handler_name}] Matched event by keyword ({match_mode}): {summary!r}"
            )
            matched.append({"event": event, "summary": summary})
    return matched


class PaymentHandler(abc.ABC):
    """Abstract base class for Monedula payment handlers."""

    # Short identifier used in Telegram replies (e.g. "yoga", "therapy").
    name: str = ""

    @abc.abstractmethod
    def matches(self, events: list[dict]) -> list[dict]:
        """
        Inspect yesterday's calendar events and return a list of match dicts
        for each event that triggers this handler.  Return an empty list if
        no payment is due.

        Each match dict may carry arbitrary data that preview() and execute()
        need (e.g. event title, date, contact details).  At minimum it should
        include {"event": <original event dict>}.
        """

    @abc.abstractmethod
    def preview(self, match: dict) -> str:
        """
        Return a short human-readable preview string that will be embedded in
        the Telegram payment-check message (no leading newline, no trailing
        newline).
        """

    @abc.abstractmethod
    def execute(self, match: dict) -> dict[str, Any]:
        """
        Execute the payment described by *match*.

        Must return a dict with at least:
          {
            "status":  "sent" | "failed" | "manual_required",
            "handler": self.name,
          }
        Additional fields are handler-specific (e.g. txid, transfer_id, iban).
        """
