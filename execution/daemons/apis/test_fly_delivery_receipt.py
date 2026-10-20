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
    assert led.reserve_delivery(KEY, dict(ENTRY))
    assert led.reserve_delivery(OTHER, {**ENTRY, "status": "pr_failed", "run_id": "run-10"})
    return led


def run(*argv, ledger=None):
    out, err = io.StringIO(), io.StringIO()
    rc = cli.main(list(argv), ledger=ledger, out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


def audit(ledger):
    path = cli.audit_path(ledger)
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


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
    lines = audit(ledger)
    assert [x["stage"] for x in lines] == ["removing", "removed"]
    for line in lines:
        assert line["key"] == KEY and line["status"] == "not_delivered"
        assert line["by"] and line["at"] and line["run_id"] == "run-9"
        assert line["action"] == "clear_delivery_receipt" and line["commit"] == "b" * 40
        assert line["branch"] == ENTRY["branch"] and line["recorded_at"] == ENTRY["recorded_at"]
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
    assert ledger.clear_delivery(KEY, stale)
    assert ledger.reserve_delivery(KEY, {**ENTRY, "status": "unknown"})
    assert ledger.clear_delivery(KEY, stale) is False
    assert KEY in ledger.delivery_receipts()


def test_an_audit_that_cannot_be_written_removes_nothing(ledger, monkeypatch):
    def fail(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(cli, "_append_audit", fail)
    rc, _, err = run("clear", KEY, "--confirm", KEY)
    assert rc == 2 and "nothing was removed" in err
    assert KEY in ledger.delivery_receipts()
    assert audit(ledger) == []


def failing_stage(real, stages):
    def append(led, line):
        if line["stage"] in stages:
            raise OSError("disk full")
        return real(led, line)

    return append


def test_a_failed_confirmation_audit_reports_the_verified_removal(ledger, monkeypatch):
    monkeypatch.setattr(
        cli, "_append_audit", failing_stage(cli._append_audit, {"removed", "remove_failed"})
    )
    rc, out, err = run("clear", KEY, "--confirm", KEY)
    assert rc == 4 and "WAS removed" in err and "verified" in err
    assert str(cli.audit_path(ledger)) in err and "'removing' record" in err
    assert "Removed the delivery receipt" not in out
    assert KEY not in ledger.delivery_receipts()
    assert [x["stage"] for x in audit(ledger)] == ["removing"]


def test_a_removal_that_raised_after_the_replace_is_never_reported_complete(
    ledger, monkeypatch
):
    """A directory-sync error after the replace: the fresh read shows the receipt
    gone, which is visibility, not durability. Exit 6, never 0."""
    real = ledger._save

    def save_then_fail(data):
        real(data)
        raise OSError("directory sync failed after the replace")

    monkeypatch.setattr(ledger, "_save", save_then_fail)
    rc, out, err = run("clear", KEY, "--confirm", KEY, ledger=ledger)
    assert rc == 6 and "NOT confirmed durable" in err and "crash may bring it back" in err
    assert "Removed the delivery receipt" not in out
    assert KEY not in ledger.delivery_receipts()
    events = audit(ledger)
    assert [x["stage"] for x in events] == ["removing", "removed_unconfirmed"]
    assert events[-1]["error"] == "OSError"


def test_the_unconfirmed_event_failing_too_still_exits_6(ledger, monkeypatch):
    real = ledger._save

    def save_then_fail(data):
        real(data)
        raise OSError("directory sync failed after the replace")

    monkeypatch.setattr(ledger, "_save", save_then_fail)
    monkeypatch.setattr(
        cli, "_append_audit", failing_stage(cli._append_audit, {"removed_unconfirmed"})
    )
    rc, _, err = run("clear", KEY, "--confirm", KEY, ledger=ledger)
    assert rc == 6 and "NOT confirmed durable" in err and "'removing' record" in err
    assert [x["stage"] for x in audit(ledger)] == ["removing"]


def test_exit_zero_needs_a_clean_removal_and_a_synced_closing_event(ledger):
    rc, out, err = run("clear", KEY, "--confirm", KEY, ledger=ledger)
    assert rc == 0 and err == "" and "Removed the delivery receipt" in out
    assert [x["stage"] for x in audit(ledger)] == ["removing", "removed"]


def test_the_audit_directory_is_synced_after_the_file(ledger, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_fsync_dir", lambda d: calls.append(Path(d)))
    rc, _, _ = run("clear", KEY, "--confirm", KEY, ledger=ledger)
    assert rc == 0 and calls == [ledger.path.parent, ledger.path.parent]


def test_a_failed_directory_sync_of_the_removing_event_removes_nothing(ledger, monkeypatch):
    def fail(directory):
        raise OSError("directory sync failed")

    monkeypatch.setattr(cli, "_fsync_dir", fail)
    rc, out, err = run("clear", KEY, "--confirm", KEY, ledger=ledger)
    assert rc == 2 and "nothing was removed" in err and "Removed" not in out
    assert KEY in ledger.delivery_receipts()


def test_a_failed_directory_sync_of_the_closing_event_exits_4(ledger, monkeypatch):
    calls = []

    def second_fails(directory):
        calls.append(directory)
        if len(calls) == 2:
            raise OSError("directory sync failed")

    monkeypatch.setattr(cli, "_fsync_dir", second_fails)
    rc, out, err = run("clear", KEY, "--confirm", KEY, ledger=ledger)
    assert rc == 4 and "WAS removed" in err and str(cli.audit_path(ledger)) in err
    assert "Removed the delivery receipt" not in out
    assert KEY not in ledger.delivery_receipts()


def test_a_removal_that_fails_before_the_replace_is_recorded_as_failed(ledger, monkeypatch):
    def refuse(data):
        raise OSError("read-only file system")

    monkeypatch.setattr(ledger, "_save", refuse)
    rc, out, err = run("clear", KEY, "--confirm", KEY, ledger=ledger)
    assert rc == 3 and "still in the ledger" in err and "Removed" not in out
    assert KEY in ledger.delivery_receipts()
    assert [x["stage"] for x in audit(ledger)] == ["removing", "remove_failed"]


def test_a_ledger_that_cannot_be_read_back_is_reported_unverified(ledger, monkeypatch):
    real = ledger.delivery_receipts
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) > 1:
            raise OSError("gone")
        return real()

    monkeypatch.setattr(ledger, "delivery_receipts", flaky)
    rc, out, err = run("clear", KEY, "--confirm", KEY, ledger=ledger)
    assert rc == 5 and "UNVERIFIED" in err and "Removed" not in out
    assert [x["stage"] for x in audit(ledger)] == ["removing"]


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
    assert [x["stage"] for x in audit(ledger)] == ["removing", "removed"]


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
