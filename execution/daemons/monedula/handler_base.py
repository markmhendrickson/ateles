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


class PaymentHandler(abc.ABC):
    """Abstract base class for Monedula payment handlers."""

    # Short identifier used in Telegram replies (e.g. "yoga", "therapy").
    name: str = ""

    def match_events(self, events: list[dict]) -> list[dict]:
        """
        Shared calendar matcher used by every handler. Selects the events that
        genuinely represent this profile's recurring session, and returns at
        most ONE match per obligation so a single day never yields two payments.

        Match precedence, strongest signal first:
          1. calendar_recurring_event_id — if the profile declares its recurring
             series id, an event matches ONLY when its recurringEventId equals it.
             Title text is irrelevant, so an unrelated event that merely mentions
             the payee (e.g. "Manel work session") can never trigger a payment.
          2. keyword fallback — only when no recurring id is set. Requires the
             title to contain ALL calendar_keywords (previously ANY, which let a
             single shared word match), guarding against loose partial hits.

        Whichever path fires, the result is de-duplicated to a single match
        (the first qualifying event) — the obligation is "pay once for this
        session", not "pay per calendar entry that mentions it".
        """
        profile = getattr(self, "profile", None)
        recurring_id = str(getattr(profile, "calendar_recurring_event_id", "") or "")
        keywords = list(getattr(profile, "calendar_keywords", []) or [])

        qualifying: list[dict] = []
        for event in events:
            summary = event.get("summary", "") or ""
            if recurring_id:
                ev_series = str(event.get("recurringEventId", "") or "")
                if ev_series and ev_series == recurring_id:
                    qualifying.append({"event": event, "summary": summary})
            elif keywords:
                low = summary.lower()
                if all(kw in low for kw in keywords):
                    qualifying.append({"event": event, "summary": summary})

        if not qualifying:
            return []
        if len(qualifying) > 1:
            log.info(
                f"[{self.name}] {len(qualifying)} events matched this obligation "
                f"({[m['summary'] for m in qualifying]}); paying once for the first."
            )
        match = qualifying[0]
        log.info(f"[{self.name}] Matched event: {match['summary']!r}")
        return [match]

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
