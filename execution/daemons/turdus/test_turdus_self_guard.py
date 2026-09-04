"""Regression tests for the Turdus self-notification guard.

The bug: Turdus delivers its own digests to the operator inbox via a `+swarm`
Gmail alias (same inbox, unread). Those "[Ateles] [turdus] … invoice(s) …"
digests match the invoice/actionable keyword filters, so the next poll
re-triages Turdus's OWN notification and fires another one — a runaway
self-feeding loop (148 self-notifications in ~14h on 2026-07-07).

_is_self_notification must skip messages Turdus itself authored while letting
genuine invoices / actionable mail through unchanged.
"""

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))


def _load_turdus_with_swarm_email(monkeypatch, addr: str):
    """(Re)import turdus with ATELES_SWARM_EMAIL set — it's read at import time."""
    monkeypatch.setenv("ATELES_SWARM_EMAIL", addr)
    sys.modules.pop("turdus", None)
    return importlib.import_module("turdus")


def test_self_notification_matched_by_swarm_from_address(monkeypatch):
    turdus = _load_turdus_with_swarm_email(monkeypatch, "swarm@example.test")
    assert turdus._is_self_notification(
        "Ateles Swarm <swarm@example.test>",
        "[Ateles] [turdus] turdus: 5 invoice(s) → urgent task(s) created for monedula",
    )


def test_self_notification_matched_by_subject_prefix_when_addr_absent(monkeypatch):
    # No swarm address configured → fall back to the "[Ateles]" subject prefix.
    turdus = _load_turdus_with_swarm_email(monkeypatch, "")
    assert turdus._is_self_notification(
        "someone@example.com", "[Ateles] weird thing"
    )


def test_real_invoice_is_not_self_notification(monkeypatch):
    turdus = _load_turdus_with_swarm_email(monkeypatch, "swarm@example.test")
    # A genuine vendor invoice (synthetic sender) must still be triaged.
    assert not turdus._is_self_notification(
        "billing@vendor.example", "Your invoice is ready"
    )
    # A payment-request in another language (synthetic sender) too.
    assert not turdus._is_self_notification(
        "Vendor <accounts@vendor.example>", "factura 260530 manteniment maig"
    )


def test_actionable_github_mail_is_not_self_notification(monkeypatch):
    turdus = _load_turdus_with_swarm_email(monkeypatch, "swarm@example.test")
    assert not turdus._is_self_notification(
        "Sender <notify@forge.example>", "you were invited to a repo"
    )
