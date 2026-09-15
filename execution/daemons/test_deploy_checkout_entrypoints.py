"""
Entrypoint effect tests for deploy-checkout identity (ateles#515).

Subprocess each real daemon entrypoint the way launchd does. Identity is forced
via ATELES_CHECKOUT_IDENTITY_ROOT / ATELES_DEPLOY_CHECKOUT so the fixture trees
are what the guard inspects without copying the whole repo.

Run: pytest execution/daemons/test_deploy_checkout_entrypoints.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DAEMON_RUNTIME = REPO_ROOT / "lib" / "daemon_runtime"

DAEMONS = [
    pytest.param(
        "cotinga",
        "execution/daemons/cotinga/cotinga.py",
        [],
        "Running daily event prep",
        30,
        id="cotinga",
    ),
    pytest.param(
        "cyphorhinus",
        "execution/daemons/cyphorhinus/cyphorhinus.py",
        [],
        "Long-polling",
        8,
        id="cyphorhinus",
    ),
    pytest.param(
        "piculet",
        "execution/daemons/piculet/watch.py",
        [],
        "Watcher started",
        8,
        id="piculet",
    ),
    pytest.param(
        "sylvia",
        "execution/daemons/sylvia/sylvia.py",
        [],
        "Sylvia starting",
        30,
        id="sylvia",
    ),
    pytest.param(
        "phoenicurus-prepare",
        "execution/daemons/phoenicurus-release/prepare.py",
        ["--dry-run"],
        "[dry-run]",
        60,
        id="phoenicurus-prepare",
    ),
]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), capture_output=True, check=False)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--quiet", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "README").write_text("x\n")
    _git(path, "add", "README")
    _git(path, "commit", "--quiet", "-m", "init")
    return path


@pytest.fixture
def deploy_and_session(tmp_path):
    deploy = _init_repo(tmp_path / "ateles-rc-src")
    session = _init_repo(tmp_path / "repos" / "ateles")
    return deploy, session


def _base_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    # Prevent live credential side-effects in happy-path probes.
    env["NEOTOMA_BEARER_TOKEN"] = ""
    env["CYPHORHINUS_TELEGRAM_BOT_TOKEN"] = env.get(
        "CYPHORHINUS_TELEGRAM_BOT_TOKEN", "dummy-token"
    )
    env["CYPHORHINUS_TELEGRAM_CHAT_ID"] = env.get(
        "CYPHORHINUS_TELEGRAM_CHAT_ID", "0"
    )
    if extra:
        env.update(extra)
    return env


def _run_daemon(
    script_rel: str,
    args: list[str],
    env: dict[str, str],
    timeout: float,
) -> subprocess.CompletedProcess:
    script = REPO_ROOT / script_rel

    def _as_text(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

    try:
        return subprocess.run(
            [sys.executable, str(script), *args],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        # Long-running daemons (cyphorhinus / piculet): return a synthetic
        # result carrying whatever output was captured before the timeout.
        return subprocess.CompletedProcess(
            args=list(exc.args) if isinstance(exc.args, (list, tuple)) else [str(exc.args)],
            returncode=124,
            stdout=_as_text(exc.stdout),
            stderr=_as_text(exc.stderr),
        )


@pytest.mark.parametrize(
    "daemon_name,script_rel,invoke_args,post_guard_marker,timeout",
    DAEMONS,
)
def test_wrong_tree_exits_78_before_side_effects(
    daemon_name,
    script_rel,
    invoke_args,
    post_guard_marker,
    timeout,
    deploy_and_session,
):
    deploy, session = deploy_and_session
    env = _base_env(
        {
            "ATELES_DEPLOY_CHECKOUT": str(deploy),
            "ATELES_CHECKOUT_IDENTITY_ROOT": str(session),
            "ATELES_CHECKOUT_DRIFT_NO_FETCH": "1",
            "NEOTOMA_REPO_ROOT": str(session / "no-neotoma"),
        }
    )
    proc = _run_daemon(script_rel, invoke_args, env, timeout)
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 78, combined
    assert "FATAL: wrong checkout" in combined
    assert post_guard_marker not in combined


@pytest.mark.parametrize(
    "daemon_name,script_rel,invoke_args,post_guard_marker,timeout",
    DAEMONS,
)
def test_happy_path_past_identity_guard(
    daemon_name,
    script_rel,
    invoke_args,
    post_guard_marker,
    timeout,
    deploy_and_session,
):
    deploy, _session = deploy_and_session
    env = _base_env(
        {
            "ATELES_DEPLOY_CHECKOUT": str(deploy),
            "ATELES_CHECKOUT_IDENTITY_ROOT": str(deploy),
            "ATELES_CHECKOUT_DRIFT_NO_FETCH": "1",
            "NEOTOMA_REPO_ROOT": str(deploy / "no-neotoma"),
        }
    )
    # Sylvia: avoid same-day early exit masking the marker we care about —
    # "Sylvia starting" is logged before the date guard.
    proc = _run_daemon(script_rel, invoke_args, env, timeout)
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert "FATAL: wrong checkout" not in combined, combined
    # Either we reached the post-guard marker, or a *different* post-guard
    # failure (missing telegram env, already-ran, neotoma unreachable, timeout
    # after starting). Identity must not be what stopped us.
    if post_guard_marker not in combined:
        assert proc.returncode != 78, combined
        assert "FATAL: wrong checkout" not in combined


def test_phoenicurus_identity_precedes_freshness(deploy_and_session, tmp_path):
    """Wrong tree + stale must report identity (78), not freshness."""
    deploy, session = deploy_and_session
    # Make session look drifted relative to a fake upstream (optional); identity
    # alone is enough — force identity fail and freshness enforce.
    env = _base_env(
        {
            "ATELES_DEPLOY_CHECKOUT": str(deploy),
            "ATELES_CHECKOUT_IDENTITY_ROOT": str(session),
            "ATELES_ENFORCE_CHECKOUT_FRESHNESS": "1",
            "ATELES_CHECKOUT_DRIFT_ROOT": str(session),
            "ATELES_CHECKOUT_DRIFT_NO_FETCH": "1",
            "NEOTOMA_REPO_ROOT": str(tmp_path / "no-neotoma"),
        }
    )
    proc = _run_daemon(
        "execution/daemons/phoenicurus-release/prepare.py",
        ["--dry-run"],
        env,
        60,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 78, combined
    assert "FATAL: wrong checkout" in combined
    assert "CheckoutDriftError" not in combined


@pytest.mark.parametrize(
    "setup,expected_code,fatal_snip",
    [
        ("no_git", 79, "cannot determine checkout identity"),
        ("missing_expected", 80, "deploy checkout not provisioned"),
    ],
)
def test_cotinga_edge_states_end_to_end(
    setup, expected_code, fatal_snip, tmp_path, deploy_and_session
):
    deploy, session = deploy_and_session
    if setup == "no_git":
        plain = tmp_path / "plain"
        plain.mkdir()
        identity_root = plain
        expected = deploy
    else:
        identity_root = session
        expected = tmp_path / "missing-deploy"
    env = _base_env(
        {
            "ATELES_DEPLOY_CHECKOUT": str(expected),
            "ATELES_CHECKOUT_IDENTITY_ROOT": str(identity_root),
        }
    )
    proc = _run_daemon(
        "execution/daemons/cotinga/cotinga.py", [], env, 30
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == expected_code, combined
    assert fatal_snip in combined
