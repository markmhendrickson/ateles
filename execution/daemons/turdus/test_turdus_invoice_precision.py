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


def test_forge_notification_mentioning_invoice_is_not_invoice():
    # A code-forge email about a PR titled "...break invoice loop" must not be
    # routed to the payment daemon just because "invoice" is in the subject.
    # `notifications@` is denied for any forge domain (synthetic here).
    assert not turdus._is_invoice(
        '"forge-bot[bot]" <notifications@forge.example>',
        "Re: [org/repo] fix: break invoice self-notification loop (#198)",
        "",
    )


def test_plain_noreply_without_invoice_signal_is_not_invoice():
    # A generic no-reply sender with no invoice signal at all is not an invoice.
    assert not turdus._is_invoice(
        "Newsletter <noreply@news.example>", "Your weekly digest", ""
    )


def test_noreply_billing_vendor_still_classifies():
    # A real billing system that mails from noreply@<billing-domain> with an
    # invoice keyword MUST still route (Phoenicurus edge-case on #206): the
    # no-reply prefix is a soft deny, overridden by a positive invoice signal.
    assert turdus._is_invoice(
        "Stripe <noreply@stripe.example>", "Your invoice is ready", ""
    )
    # no-reply@ whose local-part is not a billing keyword still routes when the
    # subject keyword is corroborated by a money amount (guard #3).
    assert turdus._is_invoice(
        "Billing <no-reply@acme-billing.example>",
        "Invoice #4471 — €1,762.09 due",
        "",
    )


def test_payment_received_receipt_is_not_invoice():
    assert not turdus._is_invoice(
        "Card <statements@bank.example>",
        "We've received your payment",
        "",
    )


def test_negative_substring_collision_is_a_known_limitation():
    # Documents the accepted tradeoff (Phoenicurus non-blocking note on #206):
    # a genuine invoice whose subject also contains a negative substring
    # ("on the way") is currently vetoed. Preferring a missed keyword-collision
    # invoice over a spurious monedula payment task is the conservative choice;
    # the negative keywords are deliberately broad. If this misses real invoices
    # in practice, anchor the phrase to refund context or move to the LLM
    # classifier (tracked on ateles#205).
    assert not turdus._is_invoice(
        "Shop <billing@shop.example>",
        "Your shipment is on the way — invoice #4471 attached",
        "",
    )
    # Same tradeoff, different negative keyword ("has been paid") — a trusted
    # billing sender referencing a prior installment collides with the veto.
    assert not turdus._is_invoice(
        "Vendor <billing@vendor.example>",
        "Invoice #452 — balance due, prior installment has been paid",
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


def test_payment_due_with_amount_still_classifies():
    # "payment due" is a subject keyword; from a non-billing sender it needs a
    # money signal (guard #3) to route.
    assert turdus._is_invoice(
        "Shop <orders@shop.example>", "Payment due: €49.90 for order 123", ""
    )


# ── guard #3: money-signal gate (ateles#205) ─────────────────────────────────


def test_bare_invoice_keyword_without_amount_is_not_invoice():
    # A subject invoice keyword with NO money amount and NO billing sender must
    # not route to monedula — the bare-keyword failure mode guard #3 closes.
    assert not turdus._is_invoice(
        "Alex <alex@company.example>", "Please see attached invoice", ""
    )


def test_bare_keyword_with_amount_in_snippet_classifies():
    # The money signal may live in the snippet, not just the subject.
    assert turdus._is_invoice(
        "Alex <alex@company.example>",
        "Please see attached invoice",
        "Total due: $1,240.00 by end of month",
    )


def test_trusted_billing_sender_needs_no_amount():
    # A known billing sender is sufficient on its own — no money signal required.
    assert turdus._is_invoice("Stripe <receipts@stripe.example>", "Your invoice", "")


# ── call-path effect tests (issue #205 QA item 4) ────────────────────────────
# These go through the real entry point `_create_task_for_email`, not the
# `_is_invoice` helper in isolation, and assert the *effect*: whether the task
# payload posted to Neotoma routes to monedula. A future change that shadows or
# bypasses `_is_invoice` inside `_create_task_for_email` would break these even
# if the helper-level tests stayed green.

import asyncio


class _CapturingAsyncClient:
    """Minimal async-context httpx.AsyncClient stub that records POST payloads."""

    def __init__(self, sink, *args, **kwargs):
        self._sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, **kwargs):
        self._sink.append({"url": url, "json": json})

        class _Resp:
            def raise_for_status(self):
                return None

            @staticmethod
            def json():
                return {"entity_id": "ent_test"}

        return _Resp()


def _run_create_task(monkeypatch, sender, subject):
    """Call _create_task_for_email through a stubbed httpx and return the posted
    task payload (the first POST), so tests can assert routing."""
    posted = []
    monkeypatch.setattr(turdus, "NEOTOMA_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(turdus, "NEOTOMA_BASE_URL", "http://neotoma.test")
    monkeypatch.setattr(turdus, "DRY_RUN", False)

    import httpx

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: _CapturingAsyncClient(posted, *a, **k)
    )
    msg = {"id": "m1", "sender": sender, "subject": subject, "snippet": ""}
    asyncio.run(turdus._create_task_for_email(msg, email_entity_id=None))
    # The first POST is the task entity.
    task_posts = [p for p in posted if p["json"].get("entity_type") == "task"]
    return task_posts[0]["json"]["snapshot"] if task_posts else None


def test_refund_does_not_route_to_monedula(monkeypatch):
    snap = _run_create_task(
        monkeypatch,
        '"service@paypal.example" <service@paypal.example>',
        "Your refund from A-SHOP is on the way",
    )
    assert snap is not None
    assert snap.get("assigned_to") != "monedula"
    assert snap.get("priority") != "urgent"


def test_forge_notification_does_not_route_to_monedula(monkeypatch):
    snap = _run_create_task(
        monkeypatch,
        '"forge-bot[bot]" <notifications@forge.example>',
        "Re: [org/repo] fix: break invoice self-notification loop (#198)",
    )
    assert snap is not None
    assert snap.get("assigned_to") != "monedula"


def test_genuine_invoice_does_route_to_monedula(monkeypatch):
    # Positive control on the effect path: a real invoice MUST reach monedula.
    snap = _run_create_task(
        monkeypatch, "Billing <billing@vendor.example>", "Your invoice is ready"
    )
    assert snap is not None
    assert snap.get("assigned_to") == "monedula"
    assert snap.get("priority") == "urgent"
