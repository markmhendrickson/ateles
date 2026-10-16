"""
Tests for lib/daemon_runtime/checkout_identity.py (ateles#515).

Run: pytest lib/daemon_runtime/test_checkout_identity.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from checkout_identity import (  # noqa: E402
    DAEMON_REGISTRY,
    DRIFT_STATES,
    EXIT_CANNOT_VERIFY,
    EXIT_CHECK_ERROR,
    EXIT_CODES,
    EXIT_EXPECTED_MISSING,
    EXIT_WRONG_TREE,
    FATAL_PREFIXES,
    FIX_BLOCK,
    IDENTITY_STATES,
    check_checkout_identity,
    enforce_deploy_checkout,
    format_fatal_message,
    IdentityReport,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), capture_output=True, check=False)


def _init_repo(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--quiet", "-b", branch)
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "README").write_text("x\n")
    _git(path, "add", "README")
    _git(path, "commit", "--quiet", "-m", "init")
    return path


@pytest.fixture
def twin_clones(tmp_path):
    deploy = _init_repo(tmp_path / "ateles-rc-src")
    session = _init_repo(tmp_path / "repos" / "ateles")
    return deploy, session


def test_ok_when_actual_equals_expected(twin_clones, monkeypatch):
    deploy, _session = twin_clones
    monkeypatch.delenv("ATELES_CHECKOUT_IDENTITY_ROOT", raising=False)
    script = deploy / "execution" / "daemons" / "cotinga" / "cotinga.py"
    script.parent.mkdir(parents=True)
    script.write_text("# stub\n")
    r = check_checkout_identity(script_path=script, expected_root=deploy)
    assert r.state == "ok"
    assert Path(r.actual_root).resolve() == deploy.resolve()


def test_wrong_tree_different_clone(twin_clones, monkeypatch):
    deploy, session = twin_clones
    script = session / "execution" / "daemons" / "cotinga" / "cotinga.py"
    script.parent.mkdir(parents=True)
    script.write_text("# stub\n")
    monkeypatch.delenv("ATELES_CHECKOUT_IDENTITY_ROOT", raising=False)
    r = check_checkout_identity(script_path=script, expected_root=deploy)
    assert r.state == "wrong_tree"
    assert "ateles" in r.actual_root or str(session) in r.actual_root


def test_wrong_tree_detached_branch_field(twin_clones, monkeypatch):
    deploy, session = twin_clones
    _git(session, "checkout", "--quiet", "--detach")
    script = session / "x.py"
    script.write_text("#\n")
    r = check_checkout_identity(script_path=script, expected_root=deploy)
    assert r.state == "wrong_tree"
    assert r.branch == "(detached)"


def test_cannot_verify_no_git(tmp_path, monkeypatch):
    plain = tmp_path / "tarball"
    plain.mkdir()
    script = plain / "daemon.py"
    script.write_text("#\n")
    expected = _init_repo(tmp_path / "ateles-rc-src")
    monkeypatch.delenv("ATELES_CHECKOUT_IDENTITY_ROOT", raising=False)
    r = check_checkout_identity(script_path=script, expected_root=expected)
    assert r.state == "cannot_verify"


def test_cannot_verify_corrupt_head(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "broken")
    head = repo / ".git" / "HEAD"
    head.write_text("ref: refs/heads/does-not-exist\n")
    expected = _init_repo(tmp_path / "ateles-rc-src")
    script = repo / "x.py"
    script.write_text("#\n")
    monkeypatch.delenv("ATELES_CHECKOUT_IDENTITY_ROOT", raising=False)
    r = check_checkout_identity(script_path=script, expected_root=expected)
    # Missing ref → cannot_verify (not ok / wrong_tree misclassification).
    assert r.state == "cannot_verify"


def test_expected_missing(tmp_path, monkeypatch):
    session = _init_repo(tmp_path / "session")
    script = session / "x.py"
    script.write_text("#\n")
    missing = tmp_path / "no-such-deploy"
    monkeypatch.delenv("ATELES_CHECKOUT_IDENTITY_ROOT", raising=False)
    r = check_checkout_identity(script_path=script, expected_root=missing)
    assert r.state == "expected_missing"


def test_check_error_when_git_subprocess_raises(twin_clones, monkeypatch):
    deploy, session = twin_clones
    script = session / "x.py"
    script.write_text("#\n")

    def boom(*_a, **_k):
        raise PermissionError("denied")

    monkeypatch.setattr("checkout_identity.subprocess.run", boom)
    r = check_checkout_identity(script_path=script, expected_root=deploy)
    assert r.state in ("check_error", "cannot_verify")


def test_symlink_equivalence_is_ok(twin_clones, tmp_path, monkeypatch):
    deploy, _session = twin_clones
    link = tmp_path / "link-to-deploy"
    link.symlink_to(deploy)
    script = link / "x.py"
    script.write_text("#\n")
    monkeypatch.delenv("ATELES_CHECKOUT_IDENTITY_ROOT", raising=False)
    r = check_checkout_identity(script_path=script, expected_root=deploy)
    assert r.state == "ok"


def test_identity_states_do_not_overlap_drift_states():
    assert IDENTITY_STATES.isdisjoint(DRIFT_STATES)


def test_format_fatal_message_order_and_no_ansi():
    report = IdentityReport(
        state="wrong_tree",
        actual_root="/tmp/repos/ateles",
        expected_root="/tmp/ateles-rc-src",
        branch="feature/x",
    )
    msg = format_fatal_message(
        "phoenicurus-prepare",
        report,
        why=DAEMON_REGISTRY["phoenicurus-prepare"]["why"],
        plist_label="com.ateles.phoenicurus-prepare",
    )
    assert msg.startswith("FATAL: wrong checkout")
    assert "\x1b" not in msg
    assert msg.index("daemon:") < msg.index("running from:")
    assert msg.index("running from:") < msg.index("expected:")
    assert "isolate_daemons_to_rc_src.sh --apply" in msg
    assert "ateles-daemon-doctor" not in msg
    assert FIX_BLOCK.splitlines()[0] in msg


@pytest.mark.parametrize(
    "state,code",
    [
        ("wrong_tree", EXIT_WRONG_TREE),
        ("cannot_verify", EXIT_CANNOT_VERIFY),
        ("expected_missing", EXIT_EXPECTED_MISSING),
        ("check_error", EXIT_CHECK_ERROR),
    ],
)
def test_exit_code_table(state, code):
    assert EXIT_CODES[state] == code
    assert FATAL_PREFIXES[state].startswith("FATAL:")


def test_enforce_exits_78_on_wrong_tree(twin_clones, monkeypatch, capsys):
    deploy, session = twin_clones
    script = session / "x.py"
    script.write_text("#\n")
    monkeypatch.delenv("ATELES_CHECKOUT_IDENTITY_ROOT", raising=False)
    with pytest.raises(SystemExit) as ei:
        enforce_deploy_checkout(
            "cotinga",
            script,
            why=DAEMON_REGISTRY["cotinga"]["why"],
            plist_label="com.ateles.cotinga",
            expected_root=deploy,
        )
    assert ei.value.code == EXIT_WRONG_TREE
    err = capsys.readouterr().err
    assert "FATAL: wrong checkout" in err


def test_enforce_silent_on_ok(twin_clones, monkeypatch, capsys):
    deploy, _ = twin_clones
    script = deploy / "x.py"
    script.write_text("#\n")
    monkeypatch.delenv("ATELES_CHECKOUT_IDENTITY_ROOT", raising=False)
    enforce_deploy_checkout(
        "cotinga",
        script,
        expected_root=deploy,
    )
    assert capsys.readouterr().err == ""
