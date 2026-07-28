"""
test_digest_format.py — Turdus digests name WHO and WHAT, not just a count.

Regression guard: the notification used to be "turdus: 1 invoice(s) → urgent
task(s) created for monedula" — no sender, no subject, no task id, so it was
unactionable without opening Neotoma.

Also pins the first line, because lib/notify derives the email SUBJECT from it
and Turdus's self-notification guard matches the "[Ateles] [turdus] …" subject
prefix. A digest whose first line drifts would break the loop guard.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("NEOTOMA_BASE_URL", "http://localhost:9180")

import turdus as t  # noqa: E402


# Synthetic senders only — this is a public repo, and real third-party addresses
# are PII (the pii-email gitleaks rule blocks them, correctly).
ITEMS = [
    {"sender": "Utility Co <billing@utility.test>",
     "subject": "Ya tienes disponible tu factura", "task_id": "ent_abc123"},
    {"sender": "invoices@vendor.test",
     "subject": "Invoice #42 due", "task_id": "ent_def456"},
]


def test_digest_names_sender_subject_and_task():
    out = t._format_digest("2 invoice(s) → urgent task(s) for monedula", ITEMS)
    assert "Ya tienes disponible tu factura" in out
    assert "billing@utility.test" in out
    assert "ent_abc123" in out
    assert "Invoice #42 due" in out
    assert "ent_def456" in out


def test_first_line_keeps_daemon_prefix_for_self_guard():
    # lib/notify builds the subject from line 1; the self-guard keys off it.
    out = t._format_digest("1 invoice(s) → urgent task(s) for monedula", ITEMS[:1])
    assert out.splitlines()[0].startswith(f"{t.DAEMON_NAME}: ")


def test_failed_task_creation_is_surfaced_not_hidden():
    out = t._format_digest("1 invoice(s)", [
        {"sender": "a@b.test", "subject": "S", "task_id": None}])
    assert "creation failed" in out


def test_long_subject_is_truncated():
    out = t._format_digest("1", [
        {"sender": "a@b.test", "subject": "x" * 200, "task_id": "ent_1"}])
    assert "…" in out
    assert len(max(out.splitlines(), key=len)) < 100


def test_more_than_max_items_is_disclosed_not_silently_dropped():
    many = [{"sender": f"s{i}@b.test", "subject": f"S{i}", "task_id": f"ent_{i}"}
            for i in range(15)]
    out = t._format_digest("15 invoice(s)", many, max_items=10)
    assert "and 5 more" in out


def test_sender_formatting():
    assert t._format_sender("Name <a@b.test>") == "Name (a@b.test)"
    assert t._format_sender("a@b.test") == "a@b.test"
    assert t._format_sender("") == "(unknown sender)"
