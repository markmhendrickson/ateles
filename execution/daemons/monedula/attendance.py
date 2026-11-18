"""Shared attendance-answer parser for every Monedula payment path."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def parse_reply(reply: str | None, handler_names: list[str]) -> set[str]:
    """Return handlers explicitly confirmed as attended.

    A bare yes never approves an attendance-gated recurring payment.
    """
    if not reply:
        return set()

    low = reply.lower().strip()
    if low in (
        "no",
        "no all",
        "skip",
        "skip all",
        "skipped",
        "n",
        "didn't go",
        "did not attend",
    ):
        return set()
    if low in (
        "attended all",
        "paid all",
        "yes attended",
        "attended",
        "all attended",
    ):
        return set(handler_names)

    approved: set[str] = set()
    for name in handler_names:
        if low in (
            f"attended {name}",
            f"paid {name}",
            f"yes {name}",
            f"y {name}",
            f"{name} attended",
            name,
        ):
            approved.add(name)
    if approved:
        return approved

    if low in ("yes", "yes all", "y", "y all"):
        log.warning(
            "Bare 'yes' received but payments are attendance-gated — "
            "need 'attended all' or 'attended <session>'. Treating as skip."
        )
        return set()

    log.warning(f"Unrecognised reply: {reply!r} — treating as skip all")
    return set()
