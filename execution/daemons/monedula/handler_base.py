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
from datetime import datetime, timezone
from typing import Any


def event_matches_profile(profile: Any, event: dict) -> bool:
    """Match an event's title and, when configured, its exact series identity."""
    summary = event.get("summary") or ""
    if not isinstance(summary, str) or not any(
        keyword in summary.lower() for keyword in profile.calendar_keywords
    ):
        return False
    series_id = getattr(profile, "calendar_recurring_event_id", "")
    if not isinstance(series_id, str):
        return False
    series_id = series_id.strip()
    if not series_id:
        return True
    event_id = event.get("id")
    if not isinstance(event_id, str) or not event_id.strip():
        return False
    return any(
        isinstance(value, str) and bool(value.strip()) and value == series_id
        for value in (event.get("recurringEventId"), event.get("id"))
    )


def matching_events(profile: Any, events: list[dict]) -> list[dict]:
    """Return matching events, collapsing a series session copied to calendars.

    The smallest event id wins regardless of calendar enumeration order. A
    lone event keeps its existing id and therefore its existing payment key.
    """
    matched = [event for event in events if event_matches_profile(profile, event)]
    if not str(getattr(profile, "calendar_recurring_event_id", "") or "").strip():
        return matched
    sessions: dict[tuple[str, str], dict] = {}
    without_start: list[dict] = []
    for event in matched:
        start = event.get("start") or {}
        if not isinstance(start, dict):
            without_start.append(event)
            continue
        instant = start.get("dateTime") or start.get("date") or ""
        if not isinstance(instant, str) or not instant.strip():
            without_start.append(event)
            continue
        if "T" in instant:
            try:
                instant = datetime.fromisoformat(instant.replace("Z", "+00:00"))
                if instant.tzinfo is not None:
                    instant = instant.astimezone(timezone.utc).isoformat()
            except ValueError:
                pass
        title = " ".join(event["summary"].split()).casefold()
        key = (instant, title)
        current = sessions.get(key)
        if current is None or str(event.get("id") or "") < str(current.get("id") or ""):
            sessions[key] = event
    return list(sessions.values()) + without_start


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
