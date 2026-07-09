"""Regression tests for Turdus invoice-classifier precision (ateles#205).

`_is_invoice` used to return True on any message whose subject merely contained
an invoice keyword, misrouting non-payments to the monedula payment daemon:
  - a PayPal *refund* ("Your refund … is on the way"),
  - a Google *data-share* notification,
  - a GitHub notification about a PR whose title contains the word "invoice"
    (e.g. "fix(turdus): skip own swarm digest to break invoice loop").

The fix adds two guards, checked before any positive keyword match:
  1. notification/automation senders are never invoices,
  2. refund / receipt-of-payment / data-share subjects veto a keyword match.

Genuine invoices (vendor `invoice`, Spanish `factura`, a known billing
sender-domain, a real "payment due") must still classify as invoices.
"""

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

turdus = importlib.import_module("turdus")


# ── false positives that must now be excluded ────────────────────────────────


def test_refund_is_not_invoice():
    # A refund from a payment provider whose domain is on the invoice
    # sender-keyword list: the "refund" subject must veto it.
    assert not turdus._is_invoice(
        '"service@paypal.example" <service@paypal.example>',
        "Your refund from A-SHOP is on the way",
        "",
    )


def test_data_share_notification_is_not_invoice():
    assert not turdus._is_invoice(
        "Notifier <noreply@notify.example>",
        "You shared some Account data with an app",
        "",
    )


def test_github_notification_mentioning_invoice_is_not_invoice():
    # A code-forge email about a PR titled "...break invoice loop" must not be
    # routed to the payment daemon just because "invoice" is in the subject.
    assert not turdus._is_invoice(
        '"forge-bot[bot]" <notifications@github.com>',
        "Re: [org/repo] fix: break invoice self-notification loop (#198)",
        "",
    )


def test_noreply_sender_is_never_invoice():
    assert not turdus._is_invoice(
        "Billing <no-reply@vendor.example>",
        "Your invoice is ready",  # keyword present, but no-reply sender vetoes
        "",
    )


def test_payment_received_receipt_is_not_invoice():
    assert not turdus._is_invoice(
        "Card <statements@bank.example>",
        "We've received your payment",
        "",
    )


# ── genuine invoices that must still classify (positive controls) ────────────


def test_plain_vendor_invoice_still_classifies():
    assert turdus._is_invoice(
        "Billing <billing@vendor.example>", "Your invoice is ready", ""
    )


def test_spanish_factura_still_classifies():
    assert turdus._is_invoice(
        "Proveedor <accounts@vendor.example>", "factura 260530 manteniment maig", ""
    )


def test_billing_sender_fragment_still_classifies():
    # Sender match on a known billing fragment ("billing@") with a neutral
    # subject must still be an invoice.
    assert turdus._is_invoice(
        "Vendor <billing@vendor.example>", "manteniment maig", ""
    )


def test_payment_due_still_classifies():
    assert turdus._is_invoice(
        "Shop <orders@shop.example>", "Payment due for order 123", ""
    )
