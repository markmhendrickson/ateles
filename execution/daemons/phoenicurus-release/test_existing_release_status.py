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


def test_stale_block_re_escalates_once_cadence_elapses(ready_to_prepare, monkeypatch):
    """
    ateles#1304: before this fix, a stale block escalated exactly ONCE per
    blocking entity, ever — then logged "already escalated — not
    re-notifying" on every subsequent run FOREVER, even as days passed and
    more commits piled up behind it. v0.23.1 blocked prep of 13 commits
    (3 security fixes) for 9+ days this way, with a single silent-after-the-
    first-page warning as the only trace.

    Simulate the cadence window elapsing by backdating the escalation marker
    itself, rather than sleeping — the marker's age is exactly what the
    cadence check reads.
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

    # Still within the cadence window: no second page.
    monkeypatch.setattr(prepare, "_head_sha", lambda: "c" * 40)
    prepare.run_prepare(dry_run=False, force=False, on_merge=True)
    assert len(notices) == 1, "must not re-notify before the cadence window elapses"

    # Backdate the marker past STALE_RELEASE_REESCALATE_HOURS, simulating time
    # having passed with the SAME blocker still stuck.
    from datetime import datetime, timedelta, timezone

    overdue_ts = (
        datetime.now(timezone.utc)
        - timedelta(hours=prepare.STALE_RELEASE_REESCALATE_HOURS + 1)
    ).timestamp()
    prepare.STALE_ESCALATION_FILE.write_text(f"ent_still_stale {overdue_ts}")

    monkeypatch.setattr(prepare, "_head_sha", lambda: "d" * 40)
    prepare.run_prepare(dry_run=False, force=False, on_merge=True)

    assert len(notices) == 2, (
        "a block still stuck past the re-escalation cadence must page again, "
        "not go silent forever after the first notice"
    )


# ── npm auto-reconciliation ──────────────────────────────────────────────────


def test_npm_reconcile_unblocks_prep_when_registry_already_shows_it_published(
    ready_to_prepare, monkeypatch
):
    """
    ateles#1304's actual incident: v0.23.1 published to npm at 13:41Z, but the
    release_result's terminal status write never landed, leaving it stuck at
    'publishing'. Every prepare run thereafter re-blocked on a fact the
    registry already contradicted. Once npm confirms the version is live,
    prep must go ahead on the SAME run rather than waiting for a human to
    notice and correct the record by hand.
    """
    rows = [
        _row(
            "ent_v0231",
            "v0.23.1",
            "publishing",
            age_days=prepare.STALE_RELEASE_BLOCK_DAYS + 6,
        )
    ]
    monkeypatch.setattr(prepare.urllib.request, "urlopen", _fake_urlopen_returning(rows))
    monkeypatch.setattr(prepare, "notify_operator", lambda *a, **k: None)
    monkeypatch.setattr(prepare, "_npm_version_published", lambda v, package="neotoma": v == "0.23.1")

    corrections: list[tuple] = []

    class _FakeWriteResult:
        status = 200

    class _FakeWriter:
        def __init__(self, _agent):
            pass

        def correct(self, entity_type, entity_id, field_name, value, *, idempotency_key):
            corrections.append((entity_type, entity_id, field_name, value))
            return _FakeWriteResult()

    monkeypatch.setitem(
        sys.modules,
        "neotoma_signed",
        type(sys)("neotoma_signed"),
    )
    sys.modules["neotoma_signed"].NeotomaWriter = _FakeWriter

    rc = prepare.run_prepare(dry_run=False, force=False, on_merge=True)

    assert rc == 0
    assert corrections == [("release_result", "ent_v0231", "status", "published")], (
        "npm showing the version live must correct the stuck record to "
        "published, signed via NeotomaWriter.correct, not left blocking"
    )


def test_npm_reconcile_does_not_fire_when_registry_disagrees(ready_to_prepare, monkeypatch):
    """A publish genuinely still in flight (or stuck before npm) must NOT be
    auto-corrected — only an npm registry match justifies overriding the
    stored status."""
    rows = [
        _row(
            "ent_v0231",
            "v0.23.1",
            "publishing",
            age_days=prepare.STALE_RELEASE_BLOCK_DAYS + 6,
        )
    ]
    monkeypatch.setattr(prepare.urllib.request, "urlopen", _fake_urlopen_returning(rows))
    notices: list[str] = []
    monkeypatch.setattr(prepare, "notify_operator", lambda *a, **k: notices.append(a[0] if a else ""))
    monkeypatch.setattr(prepare, "_npm_version_published", lambda v, package="neotoma": v == "0.23.0")

    rc = prepare.run_prepare(dry_run=False, force=False, on_merge=True)

    assert rc == 0
    assert ready_to_prepare == [], "must still block — npm does not confirm publication"
    assert notices, "must still escalate normally when reconciliation does not apply"


def test_npm_reconcile_scoped_to_publishing_status_only(ready_to_prepare, monkeypatch):
    """
    'prepared'/'pending_approval'/'approved' are stages BEFORE the
    irreversible npm publish step. npm showing a matching version there would
    mean something else entirely (e.g. a hand-run publish bypassing this
    pipeline) — never auto-correct those; only 'publishing' is in scope.
    """
    rows = [
        _row(
            "ent_v0231",
            "v0.23.1",
            "approved",
            age_days=prepare.STALE_RELEASE_BLOCK_DAYS + 1,
        )
    ]
    monkeypatch.setattr(prepare.urllib.request, "urlopen", _fake_urlopen_returning(rows))
    monkeypatch.setattr(prepare, "notify_operator", lambda *a, **k: None)
    npm_calls: list[str] = []
    monkeypatch.setattr(
        prepare,
        "_npm_version_published",
        lambda v, package="neotoma": (npm_calls.append(v), True)[1],
    )

    rc = prepare.run_prepare(dry_run=False, force=False, on_merge=True)

    assert rc == 0
    assert ready_to_prepare == [], "'approved' must still block, not auto-reconcile"
    assert not npm_calls, "npm must not even be consulted for a non-'publishing' status"


# ── early 'publishing' reconcile (v0.28.0 duplicate, 2026-11-13) ─────────────


def _install_fake_writer(monkeypatch):
    corrections: list[tuple] = []

    class _Res:
        status = 200

    class _Writer:
        def __init__(self, _agent):
            pass

        def correct(self, entity_type, entity_id, field_name, value, *, idempotency_key):
            corrections.append((entity_type, entity_id, field_name, value))
            return _Res()

    mod = type(sys)("neotoma_signed")
    mod.NeotomaWriter = _Writer
    monkeypatch.setitem(sys.modules, "neotoma_signed", mod)
    return corrections


def test_young_publishing_block_past_grace_reconciles_when_exact_version_live(
    ready_to_prepare, monkeypatch
):
    """
    The v0.28.0 incident: a stray duplicate 'publishing' row blocked prepare
    for ~40h although npm showed the version, because the reconcile only ran
    past the 3-day stale threshold. A block past the grace window but well
    under 3 days must reconcile against the EXACT version, not `latest`.
    """
    age = (prepare.PUBLISHING_RECONCILE_GRACE_MINUTES + 60) / 1440.0
    assert age < prepare.STALE_RELEASE_BLOCK_DAYS
    rows = [_row("ent_v0280_dup", "v0.28.0", "publishing", age_days=age)]
    monkeypatch.setattr(prepare.urllib.request, "urlopen", _fake_urlopen_returning(rows))
    monkeypatch.setattr(prepare, "notify_operator", lambda *a, **k: None)
    monkeypatch.setattr(
        prepare, "_npm_version_published", lambda v, package="neotoma": v == "0.28.0"
    )
    corrections = _install_fake_writer(monkeypatch)

    assert prepare.run_prepare(dry_run=False, force=False, on_merge=True) == 0
    assert corrections == [("release_result", "ent_v0280_dup", "status", "published")]


def test_young_publishing_block_reconciled_then_prep_proceeds(ready_to_prepare, monkeypatch):
    """Once the correction lands (re-query shows no blocker), prep falls
    through and spawns on the same run."""
    age = (prepare.PUBLISHING_RECONCILE_GRACE_MINUTES + 60) / 1440.0
    state = {"rows": [_row("ent_v0280_dup", "v0.28.0", "publishing", age_days=age)]}
    monkeypatch.setattr(
        prepare.urllib.request,
        "urlopen",
        lambda req, timeout=None: _fake_urlopen_returning(state["rows"])(req, timeout=timeout),
    )
    monkeypatch.setattr(prepare, "notify_operator", lambda *a, **k: None)
    monkeypatch.setattr(prepare, "_npm_version_published", lambda v, package="neotoma": True)

    corrections: list[str] = []

    class _Res:
        status = 200

    class _Writer:
        def __init__(self, _agent):
            pass

        def correct(self, entity_type, entity_id, field_name, value, *, idempotency_key):
            corrections.append(entity_id)
            state["rows"] = [_row(entity_id, "v0.28.0", "published", age_days=age)]
            return _Res()

    mod = type(sys)("neotoma_signed")
    mod.NeotomaWriter = _Writer
    monkeypatch.setitem(sys.modules, "neotoma_signed", mod)

    assert prepare.run_prepare(dry_run=False, force=False, on_merge=True) == 0
    assert corrections == ["ent_v0280_dup"]
    assert ready_to_prepare == ["v0.20.0"], "prep must proceed once unblocked"


def test_publishing_block_inside_grace_makes_no_npm_call_and_blocks(
    ready_to_prepare, monkeypatch
):
    age = (prepare.PUBLISHING_RECONCILE_GRACE_MINUTES / 2) / 1440.0
    rows = [_row("ent_inflight", "v0.28.0", "publishing", age_days=age)]
    monkeypatch.setattr(prepare.urllib.request, "urlopen", _fake_urlopen_returning(rows))
    monkeypatch.setattr(prepare, "notify_operator", lambda *a, **k: None)
    calls: list[str] = []
    monkeypatch.setattr(
        prepare,
        "_npm_version_published",
        lambda v, package="neotoma": (calls.append(v), True)[1],
    )

    assert prepare.run_prepare(dry_run=False, force=False, on_merge=True) == 0
    assert calls == [], "a publish inside the grace window must not hit npm"
    assert ready_to_prepare == []


def test_superseded_version_not_latest_but_on_npm_is_reconciled(ready_to_prepare, monkeypatch):
    """Exact-version check: a stuck row for an older version npm still hosts
    (no longer `latest`) reconciles. Drives the real _npm_version_published
    through a fake `npm view neotoma@<v> version`."""
    rows = [_row("ent_old", "v0.27.0", "publishing", age_days=2.0)]
    monkeypatch.setattr(prepare.urllib.request, "urlopen", _fake_urlopen_returning(rows))
    monkeypatch.setattr(prepare, "notify_operator", lambda *a, **k: None)
    argvs: list[list[str]] = []

    class _Proc:
        returncode = 0
        stdout = "0.27.0\n"

    class _Sub(_NoopSubprocess):
        def run(self, argv, **kw):
            if argv[0] != "npm":
                return None
            argvs.append(argv)
            return _Proc()

    monkeypatch.setattr(prepare, "subprocess", _Sub())
    corrections = _install_fake_writer(monkeypatch)

    assert prepare.run_prepare(dry_run=False, force=False, on_merge=True) == 0
    assert argvs == [["npm", "view", "neotoma@0.27.0", "version"]]
    assert corrections == [("release_result", "ent_old", "status", "published")]


@pytest.mark.parametrize("failure", ["timeout", "oserror", "nonzero", "empty"])
def test_npm_error_or_timeout_does_not_reconcile_and_logs(
    ready_to_prepare, monkeypatch, caplog, failure
):
    import subprocess as real_subprocess

    rows = [_row("ent_v0280_dup", "v0.28.0", "publishing", age_days=0.5)]
    monkeypatch.setattr(prepare.urllib.request, "urlopen", _fake_urlopen_returning(rows))
    monkeypatch.setattr(prepare, "notify_operator", lambda *a, **k: None)

    class _Proc:
        returncode = 1 if failure == "nonzero" else 0
        stdout = ""

    class _Sub(_NoopSubprocess):
        SubprocessError = real_subprocess.SubprocessError

        def run(self, argv, **kw):
            if argv[0] != "npm":
                return None
            if failure == "timeout":
                raise real_subprocess.TimeoutExpired(argv, 30)
            if failure == "oserror":
                raise OSError("npm missing")
            return _Proc()

    monkeypatch.setattr(prepare, "subprocess", _Sub())
    corrections = _install_fake_writer(monkeypatch)

    with caplog.at_level("INFO"):
        assert prepare.run_prepare(dry_run=False, force=False, on_merge=True) == 0
    assert corrections == []
    assert ready_to_prepare == [], "unconfirmed npm must keep blocking"
    assert "could not confirm" in caplog.text, "the attempt must be logged"


@pytest.mark.parametrize("status", ["prepared", "pending_approval", "approved"])
@pytest.mark.parametrize("age_days", [0.5, 5.0])
def test_non_publishing_statuses_never_reconcile_regardless_of_age(
    ready_to_prepare, monkeypatch, status, age_days
):
    rows = [_row("ent_pre", "v0.28.0", status, age_days=age_days)]
    monkeypatch.setattr(prepare.urllib.request, "urlopen", _fake_urlopen_returning(rows))
    monkeypatch.setattr(prepare, "notify_operator", lambda *a, **k: None)
    calls: list[str] = []
    monkeypatch.setattr(
        prepare,
        "_npm_version_published",
        lambda v, package="neotoma": (calls.append(v), True)[1],
    )
    corrections = _install_fake_writer(monkeypatch)

    assert prepare.run_prepare(dry_run=False, force=False, on_merge=True) == 0
    assert calls == [] and corrections == []
    assert ready_to_prepare == []


# ── round 2: rebinding after reconcile, dry run, timestamp-less, npm parsing ──


def _state_writer(monkeypatch, state, after):
    """Writer whose correct() swaps the visible rows to ``after(entity_id)``."""

    class _Res:
        status = 200

    class _Writer:
        def __init__(self, _agent):
            pass

        def correct(self, entity_type, entity_id, field_name, value, *, idempotency_key):
            state["rows"] = after(entity_id)
            return _Res()

    mod = type(sys)("neotoma_signed")
    mod.NeotomaWriter = _Writer
    monkeypatch.setitem(sys.modules, "neotoma_signed", mod)


@pytest.mark.parametrize(
    "second_age_days,expect_page",
    [(prepare.STALE_RELEASE_BLOCK_DAYS + 2, True), (0.5, False)],
)
def test_second_blocking_row_judged_on_its_own_after_first_reconciles(
    ready_to_prepare, monkeypatch, second_age_days, expect_page
):
    """#518 duplicate case: after row A is corrected, a different row B still
    blocks. No page may name A, the marker must not be recorded against A, and
    B is escalated only if B itself is stale."""
    a_age = (prepare.PUBLISHING_RECONCILE_GRACE_MINUTES + 60) / 1440.0
    row_a = _row("ent_dup_a", "v0.28.0", "publishing", age_days=a_age)
    row_b = _row("ent_real_b", "v0.29.0", "prepared", age_days=second_age_days)
    state = {"rows": [row_a, row_b]}
    monkeypatch.setattr(
        prepare.urllib.request,
        "urlopen",
        lambda req, timeout=None: _fake_urlopen_returning(state["rows"])(req, timeout=timeout),
    )
    notices: list[str] = []
    monkeypatch.setattr(prepare, "notify_operator", lambda *a, **k: notices.append(a[0] if a else ""))
    monkeypatch.setattr(prepare, "_npm_version_published", lambda v, package="neotoma": True)
    _state_writer(monkeypatch, state, lambda eid: [row_b])
    marked: list[str] = []
    monkeypatch.setattr(prepare, "_mark_escalated_stale_block", lambda eid: marked.append(eid))

    assert prepare.run_prepare(dry_run=False, force=False, on_merge=True) == 0
    assert ready_to_prepare == []
    # The reconcile's own success notice ("✅ ... auto-corrected") is expected;
    # only escalation pages ("⚠️") are forbidden.
    pages = [n for n in notices if "⚠️" in n]
    assert not any("ent_dup_a" in n for n in pages), "no page about the corrected row"
    assert "ent_dup_a" not in marked
    if expect_page:
        assert len(pages) == 1 and "ent_real_b" in pages[0]
        assert marked == ["ent_real_b"]
    else:
        assert pages == [] and marked == []


def test_dry_run_with_reconcilable_stale_row_never_pages(ready_to_prepare, monkeypatch):
    marked_ran: list = []
    monkeypatch.setattr(prepare, "_mark_ran", lambda *a, **k: marked_ran.append(a))
    rows = [
        _row(
            "ent_dup", "v0.28.0", "publishing",
            age_days=prepare.STALE_RELEASE_BLOCK_DAYS + 1,
        )
    ]
    monkeypatch.setattr(prepare.urllib.request, "urlopen", _fake_urlopen_returning(rows))
    notices: list[str] = []
    monkeypatch.setattr(prepare, "notify_operator", lambda *a, **k: notices.append(a[0] if a else ""))
    monkeypatch.setattr(prepare, "_npm_version_published", lambda v, package="neotoma": True)

    assert prepare.run_prepare(dry_run=True, force=False, on_merge=True) == 0
    assert notices == [], "a dry run must never page or notify"
    assert marked_ran == [], "a dry run must never mark the run"
    assert ready_to_prepare == []


def test_publishing_block_with_no_timestamp_is_reconciled(ready_to_prepare, monkeypatch):
    row = {"entity_id": "ent_nots", "snapshot": {"version": "v0.28.0", "status": "publishing"}}
    monkeypatch.setattr(prepare.urllib.request, "urlopen", _fake_urlopen_returning([row]))
    monkeypatch.setattr(prepare, "notify_operator", lambda *a, **k: None)
    assert prepare.existing_release_status()[3] is None, "precondition: age_days is None"
    calls: list[str] = []
    monkeypatch.setattr(
        prepare, "_npm_version_published",
        lambda v, package="neotoma": (calls.append(v), True)[1],
    )
    corrections = _install_fake_writer(monkeypatch)

    assert prepare.run_prepare(dry_run=False, force=False, on_merge=True) == 0
    assert calls == ["0.28.0"]
    assert corrections == [("release_result", "ent_nots", "status", "published")]


@pytest.mark.parametrize(
    "returncode,stdout,expected",
    [
        (0, "0.28.0\n", True),
        (0, "0.28.01\n", False),  # substring/prefix only
        (0, "10.28.0\n", False),  # substring only
        (0, "some other output\n", False),
        (0, "", False),
        (1, "0.28.0\n", False),  # non-zero exit with the version in stdout
    ],
)
def test_npm_version_published_requires_zero_exit_and_exact_line(
    monkeypatch, returncode, stdout, expected
):
    class _Proc:
        pass

    proc = _Proc()
    proc.returncode, proc.stdout = returncode, stdout

    class _Sub:
        SubprocessError = Exception

        def run(self, argv, **kw):
            return proc

    monkeypatch.setattr(prepare, "subprocess", _Sub())
    assert prepare._npm_version_published("v0.28.0") is expected


@pytest.mark.parametrize("raw", ["0", "-5", "abc", "nan", "", None])
def test_grace_falls_back_to_default_when_invalid(raw):
    assert prepare._parse_publishing_grace(raw) == 60.0


def test_grace_accepts_positive_override():
    assert prepare._parse_publishing_grace("15") == 15.0


def test_same_row_still_visible_after_reconcile_blocks_without_paging(
    ready_to_prepare, monkeypatch
):
    """Correction returns 2xx but the re-read shows the SAME stale row: block
    quietly (as on main) -- no page, no escalation mark, one _mark_ran."""
    rows = [
        _row(
            "ent_same", "v0.28.0", "publishing",
            age_days=prepare.STALE_RELEASE_BLOCK_DAYS + 1,
        )
    ]
    monkeypatch.setattr(prepare.urllib.request, "urlopen", _fake_urlopen_returning(rows))
    notices: list[str] = []
    monkeypatch.setattr(prepare, "notify_operator", lambda *a, **k: notices.append(a[0] if a else ""))
    monkeypatch.setattr(prepare, "_npm_version_published", lambda v, package="neotoma": True)
    _install_fake_writer(monkeypatch)  # 2xx, rows left unchanged
    marked_ran: list = []
    monkeypatch.setattr(prepare, "_mark_ran", lambda *a, **k: marked_ran.append(a))
    escalated: list[str] = []
    monkeypatch.setattr(prepare, "_mark_escalated_stale_block", lambda e: escalated.append(e))

    assert prepare.run_prepare(dry_run=False, force=False, on_merge=True) == 0
    assert [n for n in notices if "⚠️" in n] == [], "no page about the row"
    assert escalated == []
    assert len(marked_ran) == 1
    assert ready_to_prepare == []


@pytest.mark.parametrize("bad", ["abc", "0", "-5"])
def test_invalid_grace_override_imports_cleanly_with_fallback(bad):
    """A bad env override must not crash the daemon at import (the parser logs
    a warning, so it may not run before the logger exists)."""
    import os
    import subprocess as sp
    from pathlib import Path

    here = Path(prepare.__file__).resolve().parent
    env = {**os.environ, "PHOENICURUS_PUBLISHING_RECONCILE_GRACE_MINUTES": bad}
    proc = sp.run(
        [sys.executable, "-c",
         "import prepare; print(prepare.PUBLISHING_RECONCILE_GRACE_MINUTES)"],
        cwd=str(here), env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "60.0"


@pytest.mark.parametrize("raw,expected", [("1", 5.0), ("4.9", 5.0), ("5", 5.0), ("10", 10.0)])
def test_grace_floor_is_clamped(raw, expected):
    assert prepare._parse_publishing_grace(raw) == expected
