"""
Tests for cutover state inventory / merge / read-back (ateles#515).

These are the red-before effect tests for:
- missing / unreconciled state (refuse reload)
- fake path / PID success (reject unchanged PID and shared-clone exec paths)

Run: pytest lib/daemon_runtime/test_cutover_state.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cutover_state import (  # noqa: E402
    DAEMON_REL_DIRS,
    assert_all_readbacks,
    assert_reconciled,
    inventory_state_files,
    merge_state_pair,
    prove_daemon_cutover,
    reconcile_all,
    snapshot_inventory,
    verify_release_paths,
)


@pytest.fixture
def trees(tmp_path):
    shared = tmp_path / "repos" / "ateles"
    rc = tmp_path / "ateles-rc-src"
    for daemon, rel in DAEMON_REL_DIRS.items():
        (shared / rel).mkdir(parents=True)
        (rc / rel).mkdir(parents=True)
    return shared, rc


def test_inventory_finds_registered_files(trees):
    shared, rc = trees
    pic = DAEMON_REL_DIRS["piculet"]
    (shared / pic / "held_memos.json").write_text("{}\n")
    (rc / pic / "held_memos.json").write_text('{"a": {}}\n')
    (shared / pic / "seen_files.json").write_text("[]\n")
    (rc / pic / "seen_files.json").write_text("[]\n")
    items = inventory_state_files(shared, rc)
    held = [i for i in items if i.ref.rel_path == "held_memos.json"]
    assert len(held) == 1
    assert held[0].shared_exists and held[0].rc_exists
    assert not held[0].identical


def test_json_seen_union_merge(trees):
    shared, rc = trees
    s = shared / DAEMON_REL_DIRS["piculet"] / "seen_files.json"
    r = rc / DAEMON_REL_DIRS["piculet"] / "seen_files.json"
    s.write_text(json.dumps(["a", "b"]) + "\n")
    r.write_text(json.dumps(["b", "c"]) + "\n")
    mr = merge_state_pair(s, r, "seen_files.json")
    assert mr.status == "merged"
    assert set(json.loads(r.read_text())) == {"a", "b", "c"}


def test_held_memos_key_union(trees):
    shared, rc = trees
    s = shared / DAEMON_REL_DIRS["piculet"] / "held_memos.json"
    r = rc / DAEMON_REL_DIRS["piculet"] / "held_memos.json"
    s.write_text(
        json.dumps({"only_shared": {"held_at": "2026-09-15T00:00:00Z", "reason": "x"}})
        + "\n"
    )
    r.write_text(
        json.dumps({"only_rc": {"held_at": "2026-09-09T00:00:00Z", "reason": "y"}})
        + "\n"
    )
    mr = merge_state_pair(s, r, "held_memos.json")
    assert mr.status == "merged"
    data = json.loads(r.read_text())
    assert "only_shared" in data and "only_rc" in data


def test_unresolved_last_sha_refuses(trees):
    shared, rc = trees
    rel = DAEMON_REL_DIRS["phoenicurus-prepare"]
    s = shared / rel / ".phoenicurus_prepare_last_sha"
    r = rc / rel / ".phoenicurus_prepare_last_sha"
    s.write_text("aaa\n")
    r.write_text("bbb\n")
    mr = merge_state_pair(s, r, ".phoenicurus_prepare_last_sha")
    assert mr.status == "unresolved"


def test_assert_reconciled_fails_on_unresolved(trees):
    shared, rc = trees
    rel = DAEMON_REL_DIRS["phoenicurus-prepare"]
    s = shared / rel / ".phoenicurus_prepare_last_sha"
    r = rc / rel / ".phoenicurus_prepare_last_sha"
    s.write_text("aaa\n")
    r.write_text("bbb\n")
    items = inventory_state_files(shared, rc)
    merges = reconcile_all(items)
    with pytest.raises(RuntimeError, match="unresolved"):
        assert_reconciled(merges)


def test_missing_state_copied_then_reconciled(trees):
    shared, rc = trees
    rel = DAEMON_REL_DIRS["cotinga"]
    s = shared / rel / ".cotinga_last_run"
    s.write_text("2026-09-15\n")
    items = inventory_state_files(shared, rc)
    merges = reconcile_all(items)
    assert_reconciled(merges)
    assert (rc / rel / ".cotinga_last_run").read_text().strip() == "2026-09-15"


def test_snapshot_records_hashes(trees, tmp_path):
    shared, rc = trees
    rel = DAEMON_REL_DIRS["sylvia"]
    (shared / rel / ".sylvia_last_run").write_text("2026-09-15\n")
    items = inventory_state_files(shared, rc)
    backup = tmp_path / "backup"
    manifest = snapshot_inventory(items, backup)
    assert manifest["files"]
    assert (backup / "manifest.json").is_file()


def test_verify_release_paths_rejects_shared_executable(tmp_path):
    rc = tmp_path / "ateles-rc-src"
    shared = tmp_path / "repos" / "ateles"
    rc.mkdir(parents=True)
    shared.mkdir(parents=True)
    cmd = f"{shared}/.venv/bin/python3 {shared}/execution/daemons/cotinga/cotinga.py"
    ok, detail = verify_release_paths(cmd, rc, shared_root=shared)
    assert not ok
    assert "release checkout" in detail.lower() or "shared" in detail.lower()


def test_verify_release_paths_accepts_rc(tmp_path):
    rc = tmp_path / "ateles-rc-src"
    shared = tmp_path / "repos" / "ateles"
    rc.mkdir(parents=True)
    shared.mkdir(parents=True)
    cmd = f"{rc}/.venv/bin/python3 {rc}/execution/daemons/cotinga/cotinga.py"
    ok, detail = verify_release_paths(cmd, rc, shared_root=shared)
    assert ok, detail


def test_fake_pid_success_rejected(tmp_path):
    """Same PID before/after must not count as cutover success."""
    rc = tmp_path / "ateles-rc-src"
    rc.mkdir()
    result = prove_daemon_cutover(
        "cotinga",
        pre_pid="12345",
        rc_root=rc,
        pid_provider=lambda _label: "12345",
        command_provider=lambda _pid: f"{rc}/.venv/bin/python3 {rc}/x.py",
    )
    assert not result.ok
    assert "unchanged" in result.detail.lower() or "not a new" in result.detail.lower()


def test_fake_path_success_rejected(tmp_path):
    """New PID with shared-clone exec path must not pass read-back."""
    rc = tmp_path / "ateles-rc-src"
    shared = tmp_path / "repos" / "ateles"
    rc.mkdir()
    shared.mkdir(parents=True)
    result = prove_daemon_cutover(
        "piculet",
        pre_pid="1",
        rc_root=rc,
        shared_root=shared,
        pid_provider=lambda _label: "99",
        command_provider=lambda _pid: (
            f"{shared}/.venv/bin/python3 {shared}/execution/daemons/piculet/watch.py"
        ),
    )
    assert not result.ok
    with pytest.raises(RuntimeError, match="read-back failed"):
        assert_all_readbacks([result])


def test_genuine_readback_passes(tmp_path):
    rc = tmp_path / "ateles-rc-src"
    shared = tmp_path / "repos" / "ateles"
    rc.mkdir()
    shared.mkdir(parents=True)
    result = prove_daemon_cutover(
        "sylvia",
        pre_pid="10",
        rc_root=rc,
        shared_root=shared,
        pid_provider=lambda _label: "11",
        command_provider=lambda _pid: (
            f"{rc}/.venv/bin/python3 {rc}/execution/daemons/sylvia/sylvia.py"
        ),
    )
    assert result.ok, result.detail
    assert_all_readbacks([result])
