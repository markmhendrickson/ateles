"""The operator command that clears one Fly delivery receipt: a dry run by
default, an exact-key confirmation, the ledger's own lock, an audit line, and
no credential. Offline; a throwaway ledger file."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_delivery as delivery
import fly_delivery_receipt as cli
import fly_runner

KEY = "owner/repo#123"
OTHER = "owner/repo#124"
ENTRY = {
    "status": "not_delivered",
    "branch": "cicada/issue-123-abc",
    "run_id": "run-9",
    "commit": "b" * 40,
    "recorded_at": 1_700_000_000.0,
}


@pytest.fixture(name="ledger")
def _ledger(monkeypatch, tmp_path):
    path = tmp_path / "ledger.json"
    monkeypatch.setenv(delivery.FLY_DELIVERY_REGISTRY_ENV, str(path))
    led = delivery._delivery_ledger()
    led.record_delivery(KEY, dict(ENTRY))
    led.record_delivery(OTHER, {**ENTRY, "status": "pr_failed", "run_id": "run-10"})
    return led


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    rc = cli.main(list(argv), out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


def test_dry_run_prints_the_receipt_and_changes_nothing(ledger):
    before = ledger.path.read_bytes()
    rc, out, _ = run("clear", KEY)
    assert rc == 0 and ledger.path.read_bytes() == before
    for fact in (KEY, "not_delivered", "run-9", ENTRY["branch"], "b" * 40, "2023-11-14"):
        assert fact in out
    assert "Dry run" in out and f"--confirm {KEY}" in out
    assert not cli.audit_path(ledger).exists()


def test_confirm_removes_exactly_one_receipt_and_audits_it(ledger):
    rc, out, _ = run("clear", KEY, "--confirm", KEY)
    assert rc == 0 and "Removed" in out
    assert list(ledger.delivery_receipts()) == [OTHER]
    lines = [json.loads(x) for x in cli.audit_path(ledger).read_text().splitlines()]
    assert [x["stage"] for x in lines] == ["requested", "removed"]
    for line in lines:
        assert line["key"] == KEY and line["status"] == "not_delivered"
        assert line["by"] and line["at"] and line["run_id"] == "run-9"
    assert "token" not in cli.audit_path(ledger).read_text().lower()
    assert delivery.fly_delivery_blocker(KEY) == ""


def test_a_wrong_or_mismatched_key_refuses_and_changes_nothing(ledger):
    before = ledger.path.read_bytes()
    rc, _, err = run("clear", "owner/repo#999", "--confirm", "owner/repo#999")
    assert rc == 1 and "no delivery receipt" in err
    rc, _, err = run("clear", KEY, "--confirm", OTHER)
    assert rc == 1 and "exact key" in err
    assert ledger.path.read_bytes() == before
    assert not cli.audit_path(ledger).exists()


def test_the_key_may_be_spelled_any_way_the_dispatcher_accepts(ledger):
    rc, _, _ = run("clear", "https://github.com/Owner/Repo#123", "--confirm", "Owner/Repo#123")
    assert rc == 0 and list(ledger.delivery_receipts()) == [OTHER]


@pytest.mark.parametrize("text", ["{not json", '{"deliveries": []}', ""])
def test_a_malformed_ledger_is_refused_untouched(ledger, text):
    ledger.path.write_text(text)
    rc, _, err = run("clear", KEY, "--confirm", KEY)
    assert rc == 2 and "could not be read" in err
    assert ledger.path.read_text() == text
    assert not cli.audit_path(ledger).exists()


def test_a_receipt_that_changed_since_it_was_shown_is_left_alone(ledger):
    stale = dict(ENTRY)
    ledger.record_delivery(KEY, {**ENTRY, "status": "unknown"})
    assert ledger.clear_delivery(KEY, stale) is False
    assert KEY in ledger.delivery_receipts()


def test_an_audit_that_cannot_be_written_removes_nothing(ledger, monkeypatch):
    def fail(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(cli, "_append_audit", fail)
    rc, _, err = run("clear", KEY, "--confirm", KEY)
    assert rc == 2 and "nothing was removed" in err
    assert KEY in ledger.delivery_receipts()


def test_concurrent_clears_remove_exactly_one_receipt_once(ledger):
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import fly_delivery_receipt as c;"
        "sys.exit(c.main(['clear', sys.argv[2], '--confirm', sys.argv[2]]))"
    )
    env_ledger = str(ledger.path)
    import os

    env = {**os.environ, delivery.FLY_DELIVERY_REGISTRY_ENV: env_ledger}
    kids = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(Path(__file__).parent), KEY],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    codes = []
    for kid in kids:
        kid.communicate(timeout=60)
        codes.append(kid.returncode)
    assert codes.count(0) == 1 and set(codes) <= {0, 1}
    assert list(ledger.delivery_receipts()) == [OTHER]
    removed = [
        json.loads(x)
        for x in cli.audit_path(ledger).read_text().splitlines()
        if json.loads(x)["stage"] == "removed"
    ]
    assert len(removed) == 1


def test_list_prints_every_receipt_and_changes_nothing(ledger):
    before = ledger.path.read_bytes()
    rc, out, _ = run("list")
    assert rc == 0 and KEY in out and OTHER in out and "2 delivery receipt" in out
    assert ledger.path.read_bytes() == before


def test_clearing_is_never_automatic():
    source = Path(delivery.__file__).read_text() + Path(fly_runner.__file__).read_text()
    callers = [
        line
        for line in source.splitlines()
        if "clear_delivery(" in line and "def clear_delivery" not in line
    ]
    assert callers == []
