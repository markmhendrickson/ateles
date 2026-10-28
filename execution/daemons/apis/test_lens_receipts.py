"""Lens entries in the Fly receipt ledger: the key, restart survival, write
failure, compare-and-set, release by status, the once-only notification claim,
an unreadable ledger, and the operator's clear command. Offline; a throwaway
ledger file."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_delivery
import fly_delivery_receipt as cli
import fly_runner
import lens_receipts as lr

HEAD = "a" * 40
KEY = lr.lens_key("Owner/Repo", 7, HEAD, "qa")
META = lr.meta_for("Owner/Repo", 7, HEAD, "qa")


@pytest.fixture(name="path")
def _path(monkeypatch, tmp_path):
    path = tmp_path / "ledger.json"
    monkeypatch.setenv("APIS_FLY_LEDGER_FILE", str(path))
    return path


def fresh(path):
    """A new ledger instance on the same file: a restart."""
    return fly_runner.FlyLedger(path)


def test_lens_key_is_canonical_and_validated():
    assert KEY == f"lens:owner/repo#7@{HEAD}:qa"
    assert build_delivery.canonical_key(KEY) == KEY
    for bad in (("o/r", 7, "a" * 39, "qa"), ("o/r", 7, "A" * 40, "qa"),
                ("o/r", 0, HEAD, "qa"), ("o/r", "7", HEAD, "qa"),
                ("o/r", 7, HEAD, "q a"), ("o/r", True, HEAD, "qa")):  # fmt: skip
        with pytest.raises(ValueError):
            lr.lens_key(*bad)


def test_reservation_survives_a_restart(path):
    outcome, res = lr.reserve(KEY, META, ledger=fresh(path))
    assert outcome == "reserved" and res.key == KEY
    assert res.meta == {"kind": "lens", "repository": "owner/repo", "pr": 7,
                        "head": HEAD, "lens": "qa"}  # fmt: skip
    b = fresh(path)
    assert lr.held(KEY, ledger=b)["status"] == lr.LAUNCHING
    outcome, existing = lr.reserve(KEY, META, ledger=b)
    assert outcome == "held" and existing["token"] == res.token


def test_a_failed_write_is_an_error_and_leaves_nothing(path, monkeypatch):
    def boom(self, data):
        raise OSError("disk full")

    monkeypatch.setattr(fly_runner.FlyLedger, "_save", boom)
    assert lr.reserve(KEY, META, ledger=fresh(path)) == ("error", None)
    monkeypatch.undo()
    assert lr.held(KEY, ledger=fresh(path)) is None


def test_settle_with_the_wrong_token_changes_nothing(path):
    _, res = lr.reserve(KEY, META, ledger=fresh(path))
    before = lr.held(KEY, ledger=fresh(path))
    forged = lr.Reservation(key=KEY, token="0" * 32, meta=res.meta)
    assert lr.settle(forged, lr.FAILED, ledger=fresh(path)) is False
    assert lr.held(KEY, ledger=fresh(path)) == before
    assert lr.settle(res, lr.AWAITING, ledger=fresh(path), run_id="run-1") is True
    after = lr.held(KEY, ledger=fresh(path))
    assert after["status"] == lr.AWAITING and after["run_id"] == "run-1"


def test_release_by_status_and_the_delivery_default_is_pinned(path):
    _, res = lr.reserve(KEY, META, ledger=fresh(path))
    assert lr.settle(res, lr.AWAITING, ledger=fresh(path))
    # The delivery default still removes only a launching receipt.
    assert fresh(path).release_delivery(KEY, res.token) is False
    assert lr.held(KEY, ledger=fresh(path))["status"] == lr.AWAITING
    assert lr.release(res, statuses=(lr.LAUNCHING,), ledger=fresh(path)) is False
    assert lr.release(res, statuses=(lr.AWAITING,), ledger=fresh(path)) is True
    assert lr.held(KEY, ledger=fresh(path)) is None


def test_release_refuses_a_bare_string_of_statuses(path):
    _, res = lr.reserve(KEY, META, ledger=fresh(path))
    with pytest.raises(fly_runner.FlyLedgerError):
        fresh(path).release_delivery(KEY, res.token, "launching")
    with pytest.raises(TypeError):
        lr.release(res, statuses="launching", ledger=fresh(path))
    assert lr.held(KEY, ledger=fresh(path))["status"] == lr.LAUNCHING


def test_notification_is_claimed_exactly_once(path):
    _, res = lr.reserve(KEY, META, ledger=fresh(path))
    assert lr.settle(res, lr.FAILED, ledger=fresh(path))
    claimed = lr.claim_notification(KEY, ledger=fresh(path), clock=lambda: 1_700_000_000.0)
    assert claimed is not None and claimed["status"] == lr.FAILED
    assert lr.claim_notification(KEY, ledger=fresh(path)) is None
    assert lr.held(KEY, ledger=fresh(path))["notified_at"] == 1_700_000_000.0
    assert lr.claim_notification("lens:owner/repo#8@" + HEAD + ":qa", ledger=fresh(path)) is None


def test_two_notifiers_that_read_the_same_entry_cannot_both_win(path, monkeypatch):
    _, res = lr.reserve(KEY, META, ledger=fresh(path))
    stale = dict(lr.held(KEY, ledger=fresh(path)))
    assert lr.claim_notification(KEY, ledger=fresh(path)) is not None
    # A second notifier that read the entry before the first one's write.
    loser = fresh(path)
    monkeypatch.setattr(loser, "delivery_receipts", lambda: {KEY: stale})
    assert lr.claim_notification(KEY, ledger=loser) is None


def test_an_unreadable_ledger_is_never_reported_as_absent(path):
    path.write_text("{not json")
    with pytest.raises(lr.LensReceiptUnreadable):
        lr.held(KEY, ledger=fresh(path))


def test_the_operator_command_shows_and_clears_a_lens_entry(path):
    lr.reserve(KEY, META, ledger=fresh(path))
    out, err = io.StringIO(), io.StringIO()
    assert cli.main(["clear", KEY], ledger=fresh(path), out=out, err=err) == 0
    assert "lens:" in out.getvalue() and HEAD in out.getvalue()
    assert "pull:      7" in out.getvalue()
    assert lr.held(KEY, ledger=fresh(path)) is not None
    out = io.StringIO()
    assert cli.main(["clear", KEY, "--confirm", KEY], ledger=fresh(path), out=out, err=err) == 0
    assert lr.held(KEY, ledger=fresh(path)) is None
    audit = cli.audit_path(fresh(path)).read_text()
    assert '"kind": "lens"' in audit and HEAD in audit
