"""
test_btc_explorer.py — BTC confirmations expose a network-aware explorer link.

Covers _explorer_url (mainnet/testnet/signet + BTC_EXPLORER_BASE override) and
that a sent BTC result renders a labelled "Blockchain explorer:" line and carries
explorer_url in the structured result.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from handlers.btc_transfer import BtcTransferHandler, _explorer_url  # noqa: E402
from handlers.payment_profile import PaymentProfile  # noqa: E402


def _profile():
    return PaymentProfile(prefix="X", label="Labor", calendar_keywords=[],
                          payment_type="btc", amount_eur=210, btc_address="bc1x")


def test_explorer_url_mainnet():
    assert _explorer_url("abc", "mainnet") == "https://mempool.space/tx/abc"


def test_explorer_url_testnet_and_signet():
    assert _explorer_url("abc", "testnet") == "https://mempool.space/testnet/tx/abc"
    assert _explorer_url("abc", "signet") == "https://mempool.space/signet/tx/abc"


def test_explorer_url_default_is_mainnet():
    assert _explorer_url("abc") == "https://mempool.space/tx/abc"


def test_explorer_url_base_override(monkeypatch):
    monkeypatch.setenv("BTC_EXPLORER_BASE", "https://example.org/explorer/")
    assert _explorer_url("abc", "mainnet") == "https://example.org/explorer/tx/abc"


def test_confirmation_includes_explorer_line():
    h = BtcTransferHandler(_profile())
    txt = h.format_confirmation({
        "status": "sent", "txid": "fca18e29",
        "explorer_url": "https://mempool.space/tx/fca18e29",
    })
    assert "Blockchain explorer: https://mempool.space/tx/fca18e29" in txt
    assert "fca18e29" in txt


def test_confirmation_falls_back_to_computed_url_when_missing():
    # No explorer_url in result → computed from txid + network.
    h = BtcTransferHandler(_profile())
    txt = h.format_confirmation({"status": "sent", "txid": "zzz", "network": "testnet"})
    assert "https://mempool.space/testnet/tx/zzz" in txt
