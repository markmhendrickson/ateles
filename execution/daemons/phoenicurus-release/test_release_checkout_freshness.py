"""
Tests for `prepare.check_release_checkout_freshness` (ateles#1014).

## Why this check exists separately from the daemon's own drift check

`prepare.main()` already calls `warn_on_drift` — on `Path(__file__).parent`,
the daemon's OWN code checkout. That answers "am I running stale code".

It does not answer "is the tree I am about to release publishable", because the
release is cut from `NEOTOMA_REPO_ROOT`, a different repo entirely. Nothing
checked that one. On 2026-09-15 the two states were:

    ~/ateles-rc-src   (daemon code)    clean, current      <- checked
    ~/neotoma-rc-src  (release tree)   61 behind, dirty    <- NOT checked

So the guard reported healthy for six weeks while the release checkout was
frozen since 2026-08-05 by a single uncommitted file.

## Why dirty escalates and behind does not

`publish.preflight` refuses to tag atop a dirty tree. A dirty release checkout
is therefore not a risk of a future problem — it is a release path that is
already blocked and will stay blocked silently until a person clears it.

Behind is degradation, not blockage: a stale tree still publishes, and the next
successful release moves the checkout. Paging on it would fire on routine lag
and teach the operator to ignore the alert that does matter.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import prepare  # noqa: E402


def _report(state: str, **kw):
    """A DriftReport-shaped stub; only the fields the code reads."""
    r = MagicMock()
    r.state = state
    r.is_drifted = state in ("behind", "diverged", "dirty")
    r.summary.return_value = kw.get("summary", f"checkout is {state}")
    r.head = kw.get("head", "abc1234")
    r.upstream = kw.get("upstream", "origin/main")
    return r


@pytest.fixture
def notified(monkeypatch):
    """Capture operator notifications instead of sending them."""
    calls = []
    monkeypatch.setattr(
        prepare,
        "notify_operator",
        lambda text, **kw: calls.append({"text": text, **kw}),
    )
    return calls


def _patch_report(monkeypatch, report):
    """Make the freshness check see `report` for the release checkout."""
    mod = MagicMock()
    mod.check_checkout_drift.return_value = report
    monkeypatch.setitem(sys.modules, "checkout_drift", mod)
    return mod


class TestDirtyReleaseCheckoutEscalates:
    def test_dirty_notifies_the_operator(self, monkeypatch, notified):
        _patch_report(monkeypatch, _report("dirty"))

        prepare.check_release_checkout_freshness(dry_run=False)

        assert len(notified) == 1, "a blocked release path must reach a person"
        text = notified[0]["text"]
        # The message has to say what is wrong and what to do about it.
        assert "DIRTY" in text
        assert "blocked" in text.lower()
        assert "--ff-only" in text, "give the operator the actual remedy"
        assert "git" in text and "status" in text, "tell them how to look first"

    def test_notification_warns_before_discarding_real_work(
        self, monkeypatch, notified
    ):
        """
        The 2026-08-05 edit was real product work, not debris. A message that
        says only "clean your tree" invites discarding it.
        """
        _patch_report(monkeypatch, _report("dirty"))

        prepare.check_release_checkout_freshness(dry_run=False)

        text = notified[0]["text"].lower()
        assert "real work" in text or "before discarding" in text

    def test_dry_run_does_not_notify(self, monkeypatch, notified):
        """A diagnostic invocation must never page the operator."""
        _patch_report(monkeypatch, _report("dirty"))

        prepare.check_release_checkout_freshness(dry_run=True)

        assert notified == []


class TestNonBlockingStatesDoNotEscalate:
    @pytest.mark.parametrize("state", ["behind", "diverged"])
    def test_drifted_but_publishable_logs_without_paging(
        self, monkeypatch, notified, caplog, state
    ):
        _patch_report(monkeypatch, _report(state))

        with caplog.at_level("ERROR"):
            prepare.check_release_checkout_freshness(dry_run=False)

        assert notified == [], f"{state} degrades the release, it does not block it"
        assert "RELEASE CHECKOUT DRIFT" in caplog.text

    def test_clean_checkout_is_quiet(self, monkeypatch, notified, caplog):
        _patch_report(monkeypatch, _report("clean"))

        with caplog.at_level("ERROR"):
            prepare.check_release_checkout_freshness(dry_run=False)

        assert notified == []
        assert "RELEASE CHECKOUT DRIFT" not in caplog.text


class TestCheckNeverBreaksTheRelease:
    def test_import_failure_is_warned_not_raised(self, monkeypatch, notified, caplog):
        """A freshness report must never be why a release fails to prepare."""
        mod = MagicMock()
        mod.check_checkout_drift.side_effect = RuntimeError("boom")
        monkeypatch.setitem(sys.modules, "checkout_drift", mod)

        with caplog.at_level("WARNING"):
            prepare.check_release_checkout_freshness(dry_run=False)

        assert notified == []
        # Absent, but visible — a silently missing guard is how this class of
        # bug survives.
        assert "unavailable" in caplog.text

    def test_inspects_the_release_repo_not_the_daemon_checkout(self, monkeypatch):
        """
        The whole point: this check must look at NEOTOMA_REPO_ROOT. If it ever
        gets pointed back at the daemon's own directory it silently duplicates
        the existing check and the release tree goes unwatched again.
        """
        mod = _patch_report(monkeypatch, _report("clean"))

        prepare.check_release_checkout_freshness(dry_run=False)

        mod.check_checkout_drift.assert_called_once_with(prepare.NEOTOMA_REPO_ROOT)
