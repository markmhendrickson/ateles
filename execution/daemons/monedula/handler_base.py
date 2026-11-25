"""
handler_base.py — Abstract base class for Monedula payment handlers.

Each handler encapsulates a single recurring payment obligation that is
triggered by calendar events.  Subclasses implement three methods:

  matches(events)  → list of match dicts (one per triggered event)
  preview(match)   → human-readable preview string for Telegram
  execute(match)   → executes the payment, returns a result dict

The result dict returned by execute() must include at least:
  {
    "status":   "sent" | "funding_pending" | "submission_unknown" | "failed" | "manual_required",
    "handler":  <handler name>,
    ...handler-specific fields...
  }
"""

from __future__ import annotations

import abc
import unicodedata
from typing import Any

STATUS_FUNDING_PENDING = "funding_pending"
STATUS_SUBMISSION_UNKNOWN = "submission_unknown"


MAX_CALENDAR_ID_LENGTH = 1024


def valid_calendar_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= MAX_CALENDAR_ID_LENGTH
        and not any(unicodedata.category(char) == "Cc" for char in value)
    )


def valid_configured_series_id(value: Any) -> bool:
    return value is None or value == "" or valid_calendar_id(value)


def event_matches_profile(profile: Any, event: dict) -> bool:
    """Match an event's title and, when configured, its exact series identity."""
    if not title_matches_profile(profile, event):
        return False
    series_id = getattr(profile, "calendar_recurring_event_id", "")
    if not valid_configured_series_id(series_id):
        return False
    if not series_id:
        return True
    event_id = event.get("id")
    if not valid_calendar_id(event_id):
        return False
    # Presence, not value: a present recurringEventId (even null or blank) is recurrence
    # metadata and must be a valid id equal to the series id; only a missing field means
    # the event could be the series parent itself.
    if "recurringEventId" in event:
        recurring = event["recurringEventId"]
        return valid_calendar_id(recurring) and recurring == series_id
    return event_id == series_id


def title_matches_profile(profile: Any, event: dict) -> bool:
    """Keyword check shared by matching and mismatch notice counting."""
    if not isinstance(event, dict):
        return False
    summary = event.get("summary") or ""
    return isinstance(summary, str) and any(
        keyword in summary.lower() for keyword in profile.calendar_keywords
    )


def matching_events(profile: Any, events: list[dict]) -> list[dict]:
    """Return every matching event in calendar input order."""
    return [event for event in events if event_matches_profile(profile, event)]


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
            "status":  "sent" | "funding_pending" | "submission_unknown" | "failed" | "manual_required",
            "handler": self.name,
          }
        Additional fields are handler-specific (e.g. txid, transfer_id, iban).
        """
