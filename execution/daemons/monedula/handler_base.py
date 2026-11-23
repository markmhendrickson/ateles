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
import hashlib
import unicodedata
from datetime import datetime, timezone
from typing import Any


MAX_CALENDAR_ID_LENGTH = 1024


def valid_calendar_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value.strip()) <= MAX_CALENDAR_ID_LENGTH
        and not any(unicodedata.category(char) == "Cc" for char in value)
    )


def valid_configured_series_id(value: Any) -> bool:
    return isinstance(value, str) and (
        valid_calendar_id(value)
        or (not value.strip() and not any(unicodedata.category(char) == "Cc" for char in value))
    )


def session_start_key(event: dict) -> str:
    """Canonical start, with distinct namespaces for dates and instants."""
    if not isinstance(event, dict):
        return ""
    start = event.get("start")
    if not isinstance(start, dict):
        return ""
    raw = start.get("dateTime")
    if isinstance(raw, str):
        try:
            instant = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if instant.tzinfo is not None:
                return "instant:" + instant.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
        return ""
    raw = start.get("date")
    if isinstance(raw, str):
        try:
            return "date:" + datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
        except ValueError:
            pass
    return ""


def series_match_id(profile: Any, event: dict) -> str:
    series_id = getattr(profile, "calendar_recurring_event_id", "")
    start = session_start_key(event)
    if not valid_calendar_id(series_id) or not start:
        return ""
    digest = hashlib.sha256(f"{series_id.strip()}|{start}".encode()).hexdigest()[:32]
    return f"series-{digest}"


def event_matches_profile(profile: Any, event: dict) -> bool:
    """Match an event's title and, when configured, its exact series identity."""
    if not title_matches_profile(profile, event):
        return False
    series_id = getattr(profile, "calendar_recurring_event_id", "")
    if not valid_configured_series_id(series_id):
        return False
    if not series_id.strip():
        return True
    if not valid_calendar_id(series_id):
        return False
    series_id = series_id.strip()
    event_id = event.get("id")
    if not valid_calendar_id(event_id) or not session_start_key(event):
        return False
    recurring = event.get("recurringEventId")
    if recurring is not None:
        return valid_calendar_id(recurring) and recurring.strip() == series_id
    return event_id.strip() == series_id


def title_matches_profile(profile: Any, event: dict) -> bool:
    """Keyword check shared by matching and mismatch notice counting."""
    if not isinstance(event, dict):
        return False
    summary = event.get("summary") or ""
    return isinstance(summary, str) and any(
        keyword in summary.lower() for keyword in profile.calendar_keywords
    )


def matching_events(profile: Any, events: list[dict]) -> list[dict]:
    """Return matching events, collapsing a series session copied to calendars.

    The display copy may vary; the payment identity never does.
    """
    matched = [event for event in events if event_matches_profile(profile, event)]
    if not getattr(profile, "calendar_recurring_event_id", ""):
        return matched
    sessions: dict[str, dict] = {}
    for event in matched:
        key = series_match_id(profile, event)
        current = sessions.get(key)
        if current is None or str(event.get("id") or "") < str(current.get("id") or ""):
            sessions[key] = event
    return list(sessions.values())


def legacy_event_ids(profile: Any, events: list[dict], selected: dict) -> list[str]:
    """Event IDs previously used for this session among visible copies."""
    identity = series_match_id(profile, selected)
    return sorted({
        event["id"] for event in events
        if event_matches_profile(profile, event)
        and series_match_id(profile, event) == identity
    })


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
