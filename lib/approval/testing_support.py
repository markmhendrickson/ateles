"""Synthetic sent notices used by approval reader tests."""

import re

from lib.approval.email_channel import SentRequest


def sent_notice(token: str, subject: str, body: str | None = None) -> SentRequest:
    """Supply a sender-built notice through the reader's request_bodies seam."""
    if body is None:
        if token == "release-approve:":
            marker = f"release-approve: {subject.split('Release ', 1)[-1].split()[0]}"
        elif token == "swarm-approve:":
            marker = f"swarm-approve: {subject.split('PR ', 1)[-1].split()[0]}"
        else:
            marker = f"[APPROVE-{token}]"
        body = f"Pending approval.\n{marker}"
    return SentRequest(body, "Swarm <swarm@example.net>",
                       "Op <op@example.com>", subject.removeprefix("Re: "),
                       "Tue, 17 Nov 2026 12:00:00 +0000")


def payment_sent_notice(body: str) -> SentRequest:
    """Rebuild the subject the consent sender uses for a recorded request body."""
    marker = re.search(r"\[APPROVE-[0-9A-F]{8}\]", body)
    payment_date = re.search(r"\|\s*(\d{4}-\d{2}-\d{2})\s*\|", body)
    if marker is None or payment_date is None:
        raise AssertionError("sender-built consent request is incomplete")
    subject = f"[Ateles] Monedula consent {payment_date.group(1)} {marker.group(0)}"
    return sent_notice("", subject, body)
