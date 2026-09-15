"""
lib/notify/ — Apprise-backed notification routing for Ateles daemons.

Usage:
    from lib.notify import Notifier
    notifier = Notifier.from_neotoma()   # loads priority_rubric at startup
    notifier.send("Payment sent ✅", priority="info", handler="monedula")
    notifier.send("API error: 503", priority="blocker", handler="monedula")

Priority levels (maps to priority_rubric fields):
    critical          — immediate, bypasses silence window
    blocker           — send now
    operator_decision — send now; held across silence for individual delivery
    warn              — send now outside silence; held across silence
    info              — logged only (no digest email, no queue)

Routine digests are disabled. See lib/notify/notifier.py.
"""

from .notifier import Notifier, Priority, is_actionable_held_notice

__all__ = ["Notifier", "Priority", "is_actionable_held_notice"]
