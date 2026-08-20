"""
Tests for `existing_release_status` / `is_sentinel_version` / the run_prepare
inflight short-circuit + stale-block escalation — ateles#461.

Root cause: `existing_release_status` ignored its `next_version_hint`
argument entirely, so ANY release_result with a blocking status — including
a sentinel/test entity like `v0.0.0-lint-check` — silently blocked prep of
every real release behind it. A lint-check artifact written 2026-08-09
blocked release prep for 11 days with every run exiting rc=0.

Effect-level throughout: assertions are on whether `spawn_prepare_agent` runs
and whether `notify_operator` fires — not merely on what a helper returns.
Only externals are mocked (`urllib.request.urlopen`, `subprocess`, the
notify/spawn boundary); the filter/branch logic under test is never mocked.

Run: pytest execution/daemons/phoenicurus-release/test_existing_release_status.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import prepare  # noqa: E402

SHA = "b" * 40


@pytest.fixture(autouse=True)
def _neotoma_base_url(monkeypatch):
    """
    `existing_release_status` builds `urllib.request.Request(f"{base}/...")`
    itself before the mocked `urlopen` ever runs — with NEOTOMA_BASE_URL unset
    (the default in a clean CI env) `base` is "", so the Request constructor
    raises `ValueError: unknown url type` before the fake urlopen is reached.
    Every test in this file needs a valid absolute base URL regardless of
    whatever the host environment happens to have set, matching the pattern
    in test_store_release_result.py / test_publish.py.
    """
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.test")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _iso_days_ago(days: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace(
        "+00:00", "Z"
    )


def _fake_urlopen_returning(rows: list[dict]):
    def _urlopen(req, timeout):
        resp = MagicMock()
        resp.read.return_value = json.dumps({"entities": rows}).encode()
        resp.__enter__ = lambda self: resp
        resp.__exit__ = lambda self, *a: False
        return resp

    return _urlopen


def _row(entity_id: str, version: str, status: str, age_days: float = 0.0) -> dict:
    return {
        "entity_id": entity_id,
        "updated_at": _iso_days_ago(age_days),
        "snapshot": {"version": version, "status": status},
    }


# ── is_sentinel_version ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "version,expected",
    [
        ("", True),
        (None, True),
        ("v0.0.0-lint-check", True),
        ("v0.0.0-ci-smoke", True),
        ("v1.2.3-lint-check", True),
        ("v0.19.0", False),
        ("v1.0.0", False),
    ],
)
def test_is_sentinel_version(version, expected):
    assert prepare.is_sentinel_version(version) is expected


# ── existing_release_status: version filtering ──────────────────────────────


def test_same_version_prepared_blocks_spawn(monkeypatch):
    rows = [_row("ent_same", "v0.20.0", "prepared", age_days=1)]
    monkeypatch.setattr(prepare.urllib.request, "urlopen", _fake_urlopen_returning(rows))

    result = prepare.existing_release_status("v0.20.0")

    assert result is not None
    status, entity_id, version, _age = result
    assert (status, entity_id, version) == ("prepared", "ent_same", "v0.20.0")


def test_unrelated_version_prepared_does_not_block(monkeypatch):
    rows = [_row("ent_other", "v0.19.0", "prepared", age_days=1)]
    monkeypatch.setattr(prepare.urllib.request, "urlopen", _fake_urlopen_returning(rows))

    result = prepare.existing_release_status("v0.20.0")

    assert result is None


def test_non_blocking_statuses_ignored(monkeypatch):
    rows = [
        _row("ent_a", "v0.20.0", "superseded"),
        _row("ent_b", "v0.20.0", "published"),
        _row("ent_c", "v0.20.0", "skipped"),
        _row("ent_d", "v0.20.0", "failed"),
    ]
    monkeypatch.setattr(prepare.urllib.request, "urlopen", _fake_urlopen_returning(rows))

    result = prepare.existing_release_status("v0.20.0")

    assert result is None


def test_empty_hint_blocks_any_non_sentinel(monkeypatch):
    rows = [_row("ent_real", "v0.19.9", "prepared", age_days=1)]
    monkeypatch.setattr(prepare.urllib.request, "urlopen", _fake_urlopen_returning(rows))

    assert prepare.existing_release_status("") is not None
    assert prepare.existing_release_status(None) is not None


def test_empty_hint_sentinel_alone_does_not_block(monkeypatch):
    rows = [_row("ent_sentinel", "v0.0.0-lint-check", "prepared", age_days=1)]
    monkeypatch.setattr(prepare.urllib.request, "urlopen", _fake_urlopen_returning(rows))

    assert prepare.existing_release_status("") is None
    assert prepare.existing_release_status(None) is None


def test_ordering_prefers_newest_blocking_match(monkeypatch):
    rows = [
        _row("ent_old", "v0.20.0", "prepared", age_days=10),
        _row("ent_new", "v0.20.0", "pending_approval", age_days=1),
    ]
    monkeypatch.setattr(prepare.urllib.request, "urlopen", _fake_urlopen_returning(rows))

    result = prepare.existing_release_status("v0.20.0")

    assert result is not None
    _status, entity_id, _version, _age = result
    assert entity_id == "ent_new", "the newest blocking match must win"


# ── run_prepare: effect-level P0 regression ─────────────────────────────────


class _NoopSubprocess:
    @staticmethod
    def run(*_a, **_k):
        return None


class _FakeRepoRoot:
    def __init__(self, tmp_path):
        self._tmp = tmp_path
        (tmp_path / "package.json").write_text("{}")

    def __truediv__(self, other):
        return self._tmp / other


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare, "MERGE_STATE_FILE", tmp_path / ".sha")
    monkeypatch.setattr(prepare, "STATE_FILE", tmp_path / ".day")
    monkeypatch.setattr(prepare, "SPAWN_COUNT_FILE", tmp_path / ".count")
    monkeypatch.setattr(prepare, "SPAWN_PID_FILE", tmp_path / ".pid")
    monkeypatch.setattr(prepare, "STALE_ESCALATION_FILE", tmp_path / ".stale")
    return tmp_path


@pytest.fixture
def ready_to_prepare(isolated_state, monkeypatch, tmp_path):
    """Drive `run_prepare` to the inflight check with every other gate green."""
    monkeypatch.setattr(prepare, "_head_sha", lambda: SHA)
    monkeypatch.setattr(prepare, "latest_tag", lambda: "v0.20.0")
    monkeypatch.setattr(prepare, "unreleased_commit_count", lambda _tag: 4)
    monkeypatch.setattr(prepare, "main_ci_green", lambda: True)
    monkeypatch.setattr(prepare, "_live_peer_pid", lambda _h: None)
    monkeypatch.setattr(prepare, "subprocess", _NoopSubprocess())
    monkeypatch.setattr(
        prepare, "NEOTOMA_REPO_ROOT", _FakeRepoRoot(tmp_path), raising=False
    )

    spawns: list[str] = []

    def _fake_spawn(tag, count, dry_run, head=""):
        spawns.append(tag)
        return True

    monkeypatch.setattr(prepare, "spawn_prepare_agent", _fake_spawn)
    return spawns


def test_prepared_sentinel_does_not_block_prepare_spawn(ready_to_prepare, monkeypatch):
    """
    P0 effect regression (ateles#461): a `prepared` v0.0.0-lint-check entity
    plus unreleased commits and green CI must still spawn a prepare agent for
    the REAL version under prep. Fails against pre-fix code, which ignored
    next_version_hint and blocked on the first blocking-status row regardless
    of version.
    """
    rows = [_row("ent_18aa628da7b33333167a5826", "v0.0.0-lint-check", "prepared", age_days=11)]
    monkeypatch.setattr(
        prepare.urllib.request, "urlopen", _fake_urlopen_returning(rows)
    )
    notices: list[str] = []
    monkeypatch.setattr(prepare, "notify_operator", lambda *a, **k: notices.append(a[0] if a else k))

    rc = prepare.run_prepare(dry_run=False, force=False, on_merge=True)

    assert rc == 0
    assert ready_to_prepare == ["v0.20.0"], (
        "sentinel v0.0.0-lint-check must never block prep of a real version"
    )


def test_same_version_prepared_blocks_spawn_via_run_prepare(ready_to_prepare, monkeypatch):
    rows = [_row("ent_real_pending", "v0.20.0", "prepared", age_days=1)]
    monkeypatch.setattr(prepare.urllib.request, "urlopen", _fake_urlopen_returning(rows))
    monkeypatch.setattr(prepare, "notify_operator", lambda *a, **k: None)

    rc = prepare.run_prepare(dry_run=False, force=False, on_merge=True)

    assert rc == 0
    assert ready_to_prepare == [], "a real same-version pending release must still block"


def test_rc_pending_for_a_new_version_blocks_even_though_latest_tag_is_older(
    ready_to_prepare, monkeypatch
):
    """
    `ready_to_prepare` stubs `latest_tag` -> "v0.20.0" (the LAST RELEASED
    version), which is what `run_prepare` used to pass as the hint. The real
    RC an in-flight release_result tracks is chosen by the spawned agent's
    semver bump and is NEVER equal to that last-released tag — so a hint of
    `tag` could never match it. This is the call-site bug flagged in the
    ateles#461 QA/Arch review: `run_prepare` must call
    `existing_release_status()` with no hint, not `existing_release_status(tag)`.
    """
    rows = [_row("ent_rc_pending", "v0.21.0", "pending_approval", age_days=1)]
    monkeypatch.setattr(prepare.urllib.request, "urlopen", _fake_urlopen_returning(rows))
    monkeypatch.setattr(prepare, "notify_operator", lambda *a, **k: None)

    rc = prepare.run_prepare(dry_run=False, force=False, on_merge=True)

    assert rc == 0
    assert ready_to_prepare == [], (
        "an in-flight RC for a version other than latest_tag() must still "
        "block — the hint must not be the last-released tag"
    )


def test_block_log_includes_entity_id_and_version(ready_to_prepare, monkeypatch, caplog):
    rows = [_row("ent_block123", "v0.20.0", "pending_approval", age_days=1)]
    monkeypatch.setattr(prepare.urllib.request, "urlopen", _fake_urlopen_returning(rows))
    monkeypatch.setattr(prepare, "notify_operator", lambda *a, **k: None)

    with caplog.at_level("INFO"):
        prepare.run_prepare(dry_run=False, force=False, on_merge=True)

    assert "pending_approval" in caplog.text
    assert "v0.20.0" in caplog.text
    assert "ent_block123" in caplog.text


# ── Stale-block escalation ───────────────────────────────────────────────────


def test_stale_block_escalates(ready_to_prepare, monkeypatch, caplog):
    rows = [
        _row(
            "ent_stale",
            "v0.20.0",
            "prepared",
            age_days=prepare.STALE_RELEASE_BLOCK_DAYS + 1,
        )
    ]
    monkeypatch.setattr(prepare.urllib.request, "urlopen", _fake_urlopen_returning(rows))
    notices: list[str] = []
    monkeypatch.setattr(prepare, "notify_operator", lambda *a, **k: notices.append(a[0] if a else ""))

    with caplog.at_level("WARNING"):
        prepare.run_prepare(dry_run=False, force=False, on_merge=True)

    assert notices, "a block older than the stale threshold must escalate to the operator"
    assert "WARNING" in caplog.text or any(
        r.levelname in ("WARNING", "ERROR") for r in caplog.records
    )
    assert any(
        str(round(prepare.STALE_RELEASE_BLOCK_DAYS + 1)) in n or "day" in n
        for n in notices
    ), "escalation should include age-in-days"


def test_fresh_block_info_only(ready_to_prepare, monkeypatch):
    rows = [
        _row(
            "ent_fresh",
            "v0.20.0",
            "prepared",
            age_days=prepare.STALE_RELEASE_BLOCK_DAYS - 1,
        )
    ]
    monkeypatch.setattr(prepare.urllib.request, "urlopen", _fake_urlopen_returning(rows))
    notices: list[str] = []
    monkeypatch.setattr(prepare, "notify_operator", lambda *a, **k: notices.append(a[0] if a else ""))

    prepare.run_prepare(dry_run=False, force=False, on_merge=True)

    assert not notices, "a fresh block (within threshold) must not escalate"


def test_stale_block_escalates_once_not_every_merge(ready_to_prepare, monkeypatch):
    """
    The on-merge path is rate-limited per COMMIT, not per day (module
    docstring) — several merges can land while the same release_result is
    still stuck. A repeat run over a NEW head, same still-stale blocker, must
    not re-page the operator.
    """
    rows = [
        _row(
            "ent_still_stale",
            "v0.20.0",
            "prepared",
            age_days=prepare.STALE_RELEASE_BLOCK_DAYS + 1,
        )
    ]
    monkeypatch.setattr(prepare.urllib.request, "urlopen", _fake_urlopen_returning(rows))
    notices: list[str] = []
    monkeypatch.setattr(prepare, "notify_operator", lambda *a, **k: notices.append(a[0] if a else ""))

    prepare.run_prepare(dry_run=False, force=False, on_merge=True)
    assert len(notices) == 1

    # A later merge (different head) re-evaluates the same still-stale block.
    monkeypatch.setattr(prepare, "_head_sha", lambda: "c" * 40)
    prepare.run_prepare(dry_run=False, force=False, on_merge=True)

    assert len(notices) == 1, "must not re-notify for a blocker already escalated"


def test_stale_block_escalates_again_once_the_blocker_changes(ready_to_prepare, monkeypatch):
    """A DIFFERENT stale blocking entity must still get its own escalation."""
    rows = [
        _row(
            "ent_first_stale",
            "v0.20.0",
            "prepared",
            age_days=prepare.STALE_RELEASE_BLOCK_DAYS + 1,
        )
    ]
    monkeypatch.setattr(prepare.urllib.request, "urlopen", _fake_urlopen_returning(rows))
    notices: list[str] = []
    monkeypatch.setattr(prepare, "notify_operator", lambda *a, **k: notices.append(a[0] if a else ""))

    prepare.run_prepare(dry_run=False, force=False, on_merge=True)
    assert len(notices) == 1

    rows2 = [
        _row(
            "ent_second_stale",
            "v0.20.0",
            "prepared",
            age_days=prepare.STALE_RELEASE_BLOCK_DAYS + 5,
        )
    ]
    monkeypatch.setattr(prepare.urllib.request, "urlopen", _fake_urlopen_returning(rows2))
    monkeypatch.setattr(prepare, "_head_sha", lambda: "d" * 40)
    prepare.run_prepare(dry_run=False, force=False, on_merge=True)

    assert len(notices) == 2, "a newly-stale, different blocking entity must escalate"
