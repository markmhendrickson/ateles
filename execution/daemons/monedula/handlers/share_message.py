"""
handlers/share_message.py — payee-facing copy-paste line for on-chain payments.

Operator convention: a blockchain payment confirmation carries a line the
operator can paste straight to the payee, in exactly this format:

    [AMOUNT] € [LINK TO EXPLORER]

e.g. "60 € 📤 https://mempool.space/tx/ee62…"

It is deliberately terse — it is dropped into a chat alongside the payment, not
written as a letter — and therefore has no language: an amount and a link read
the same to everyone.

BLOCKCHAIN ONLY. Wise (and any other non-chain rail) has no public explorer, so
there is nothing for the payee to verify and no share line is produced. Do not
invent one: build_share_message() returns "" for those rails.
"""

from __future__ import annotations

SHARE_EMOJI = "📤"


def build_share_message(
    *,
    amount_eur,
    rail: str,
    explorer_url: str = "",
) -> str:
    """Return the payee copy-paste line, or "" when there is nothing to share.

    Format: "[AMOUNT] € [LINK TO EXPLORER]".

    Only on-chain rails produce a line, and only when an explorer URL exists —
    the link is the entire point (it lets the payee verify the transfer).
    """
    if rail != "btc" or not explorer_url:
        return ""
    return f"{amount_eur} € {SHARE_EMOJI} {explorer_url}"
