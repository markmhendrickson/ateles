"""
test_share_message.py — the payee copy-paste line for on-chain payments.

Convention: "[AMOUNT] € [LINK TO EXPLORER]" — terse, no language, blockchain
only. Wise and other non-chain rails have no explorer, so they produce NO line.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from handlers import share_message as sm  # noqa: E402


def test_btc_line_is_amount_euro_link():
    line = sm.build_share_message(
        amount_eur=60, rail="btc", explorer_url="https://mempool.space/tx/abc")
    assert line == "60 € 📤 https://mempool.space/tx/abc"


def test_wise_produces_no_line():
    # No blockchain, no explorer, nothing for the payee to verify.
    assert sm.build_share_message(amount_eur=60, rail="wise") == ""
    assert sm.build_share_message(
        amount_eur=60, rail="wise", explorer_url="https://x/tx/1") == ""


def test_btc_without_explorer_produces_no_line():
    # The link is the whole point — no link, no line.
    assert sm.build_share_message(amount_eur=60, rail="btc") == ""


def test_line_has_no_prose():
    # It is dropped into a chat next to the payment, not written as a letter.
    line = sm.build_share_message(
        amount_eur=210, rail="btc", explorer_url="https://mempool.space/tx/z")
    for word in ("Hola", "Hi", "Thanks", "Gracias", "sent", "enviar"):
        assert word not in line
    assert line.startswith("210 € ")
