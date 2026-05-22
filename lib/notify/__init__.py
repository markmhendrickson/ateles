"""
lib/notify/ — Apprise-backed notification routing for Ateles daemons.

Usage:
    from lib.notify import Notifier
    notifier = Notifier.from_neotoma()   # loads priority_rubric at startup
    notifier.send("Payment sent ✅", priority="info", handler="monedula")
    notifier.send("API error: 503", priority="blocker", handler="monedula")

Priority levels (maps to priority_rubric fields):
    critical        — immediate, bypasses silence window
    blocker         — send now, retry after 30min
    operator_decision — hold for digest unless urgent
    info            — hold for digest

See lib/notify/notifier.py for implementation.
"""

from .notifier import Notifier, Priority

__all__ = ["Notifier", "Priority"]
