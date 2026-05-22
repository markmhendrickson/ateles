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
from typing import Any


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
