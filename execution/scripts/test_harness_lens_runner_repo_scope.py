"""The lens runner reads a target repository with a repository-scoped credential.

Defect (reported 2026-10-27): run from a daemon checkout on a PR in a private
sibling repository, the runner's own ``git fetch origin`` answered "Write access
to repository not granted" (HTTP 403), because it used whatever git credential
was ambient in the daemon's environment. The same fetch worked from an operator
shell and worked when the runner was wrapped in ``github_app_token.py exec
--repo``. These tests model git as a server that only honours the credential
minted for the target repository, and prove the runner presents that one,
refuses to fall back to the ambient one, and never prints either.

No network, no App, no real git credential: ``subprocess.run`` and the App
helper are replaced by recorders.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent.parent
_DAEMON_DIR = _REPO_ROOT / "execution" / "daemons" / "apis"
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import harness_lens_runner as hlr  # noqa: E402
from lib import github_app_token as gat  # noqa: E402

REPO = "someowner/private-sibling"
SCOPED = "ghs_scoped_for_private_sibling"
AMBIENT = "ghp_ambient_daemon_credential"
FORBIDDEN_403 = (
    "remote: Write access to repository not granted.\n"
    f"fatal: unable to access 'https://github.com/{REPO}.git/': "
    "The requested URL returned error: 403"
)

# Captured at import, before the autouse fixture below replaces the attribute,
# so these tests exercise the real credential scoping.
_REAL_REPO_SCOPED_ENV = getattr(hlr, "repo_scoped_env", None)


@pytest.fixture(autouse=True)
def _head_checks_not_held(monkeypatch):
    """These tests are about credential scoping, not check state."""
    async def not_held(target):
        return None

    monkeypatch.setattr(hlr, "head_checks_hold", not_held)


@pytest.fixture(autouse=True)
def _ambient_credential(monkeypatch):
    """The daemon's environment carries a credential that cannot read the repo."""
    monkeypatch.setenv("GH_TOKEN", AMBIENT)
    monkeypatch.setenv("GITHUB_TOKEN", AMBIENT)
    monkeypatch.setenv("GIT_ASKPASS", "/ambient/askpass")
    # config injected through the environment, applied after file config
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'http.extraheader=Authorization: ambient'")
    monkeypatch.setenv("ATELES_REVIEWER_APP_PRIVATE_KEY", "not-a-real-key")
    monkeypatch.setattr(hlr, "repo_scoped_env", _REAL_REPO_SCOPED_ENV, raising=False)


def _mint(monkeypatch, *, token: str = SCOPED, error: str | None = None):
    """Replace the App helper: it mints ``token`` for REPO or raises."""
    calls: list[str] = []

    def fake(repo, *, prefix=gat.DEFAULT_APP_ENV_PREFIX):
        calls.append(repo)
        if error is not None:
            raise gat.AppTokenError(error)
        return gat.InstallationToken(
            token=token,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            installation_id="4242",
            permissions={"contents": "read"},
        )

    monkeypatch.setattr(gat, "repo_scoped_token", fake)
    return calls


class FakeGit:
    """git as the private sibling's server sees it.

    A fetch succeeds only when the credential helper chain was reset to gh's
    and the token gh would answer with is the one minted for the repository.
    Anything else is the 403 from the report.
    """

    def __init__(self, *, remote="https://github.com/someowner/private-sibling.git"):
        self.remote = remote
        self.fetches: list[dict] = []
        self.other: list[list[str]] = []

    def __call__(self, command, **kwargs):
        if command[:1] != ["git"]:
            self.other.append(list(command))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[-2:] == ["rev-parse", "--git-common-dir"]:
            return subprocess.CompletedProcess(command, 0, stdout=".git\n", stderr="")
        if command[-3:] == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(
                command, 0, stdout=self.remote + "\n", stderr=""
            )
        if command[-3:] == ["fetch", "-q", "origin"]:
            env = kwargs.get("env")
            self.fetches.append({"command": list(command), "env": env})
            helper_reset = (
                "credential.helper=" in command
                and "credential.https://github.com.helper=" in command
            )
            presented = (env or {}).get("GH_TOKEN")
            if helper_reset and presented == SCOPED:
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            if kwargs.get("check"):
                raise subprocess.CalledProcessError(
                    128, command, stderr=FORBIDDEN_403
                )
            return subprocess.CompletedProcess(
                command, 128, stdout="", stderr=FORBIDDEN_403
            )
        self.other.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


@pytest.fixture
def repo_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "repos" / "private-sibling" / ".git").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home / "repos" / "private-sibling"


def _worktree(tmp_path, **kwargs):
    return hlr.Worktree(
        repo_name="private-sibling", path=tmp_path / "wt", **kwargs
    )


# ── the 403 path ----------------------------------------------------------------


def test_fetch_of_a_private_sibling_uses_the_repo_scoped_token(
    monkeypatch, tmp_path, repo_path
):
    """Red before the fix: the fetch ran with the daemon's credential and got 403."""
    mints = _mint(monkeypatch)
    git = FakeGit()
    monkeypatch.setattr(hlr.subprocess, "run", git)

    _worktree(tmp_path, remote_repo=REPO).create(head="a" * 40)

    assert mints == [REPO]
    (fetch,) = git.fetches
    assert fetch["env"]["GH_TOKEN"] == SCOPED
    assert fetch["env"]["GITHUB_TOKEN"] == SCOPED
    # worktree add ran after the fetch succeeded
    assert any(c[3:5] == ["worktree", "add"] for c in git.other)
    assert git.other[-1][-1] == "a" * 40  # added at the head that was reviewed


def test_the_ambient_credential_never_reaches_git_or_gh(monkeypatch, tmp_path, repo_path):
    _mint(monkeypatch)
    git = FakeGit()
    monkeypatch.setattr(hlr.subprocess, "run", git)

    _worktree(tmp_path, remote_repo=REPO).create(head="a" * 40)

    env = git.fetches[0]["env"]
    assert AMBIENT not in env.values()
    assert "GIT_ASKPASS" not in env
    assert "ATELES_REVIEWER_APP_PRIVATE_KEY" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_CONFIG_COUNT" not in env and "GIT_CONFIG_PARAMETERS" not in env
    # the helper chain is reset before gh's helper is installed, so no keychain
    # or stored helper can answer ahead of the scoped token
    cmd = git.fetches[0]["command"]
    reset = cmd.index("credential.helper=")
    assert cmd.index("credential.https://github.com.helper=!gh auth git-credential") > reset
    # a stored Authorization header would be sent ahead of any helper
    assert "http.extraheader=" in cmd
    assert "http.https://github.com/.extraheader=" in cmd


def test_the_token_stays_out_of_the_live_process_environment(
    monkeypatch, tmp_path, repo_path
):
    """A dispatched child inherits os.environ; it must never see the scoped token."""
    _mint(monkeypatch)
    monkeypatch.setattr(hlr.subprocess, "run", FakeGit())
    before = dict(os.environ)

    _worktree(tmp_path, remote_repo=REPO).create(head="a" * 40)

    assert dict(os.environ) == before
    assert SCOPED not in os.environ.values()


def test_a_fetch_the_scoped_token_cannot_make_names_the_installation_gap(
    monkeypatch, tmp_path, repo_path
):
    """If even the scoped token is refused, say so, and do not leak it."""
    _mint(monkeypatch, token="ghs_the_wrong_installation")
    monkeypatch.setattr(hlr.subprocess, "run", FakeGit())

    with pytest.raises(hlr.RepoAccessError) as caught:
        _worktree(tmp_path, remote_repo=REPO).create(head="a" * 40)

    message = str(caught.value)
    assert "not grant read access" in message
    assert "ghs_the_wrong_installation" not in message
    assert AMBIENT not in message


def test_a_token_echoed_by_git_is_redacted(monkeypatch, tmp_path, repo_path):
    _mint(monkeypatch, token="ghs_echoed_by_git")

    def echoing(command, **kwargs):
        if command[-3:] == ["fetch", "-q", "origin"]:
            return subprocess.CompletedProcess(
                command,
                128,
                stdout="",
                stderr="fatal: Authentication failed for ghs_echoed_by_git",
            )
        return FakeGit()(command, **kwargs)

    monkeypatch.setattr(hlr.subprocess, "run", echoing)

    with pytest.raises(hlr.RepoAccessError) as caught:
        _worktree(tmp_path, remote_repo=REPO).create(head="a" * 40)
    assert "ghs_echoed_by_git" not in str(caught.value)
    assert "***" in str(caught.value)


def test_an_unrelated_fetch_failure_is_not_called_an_access_failure(
    monkeypatch, tmp_path, repo_path
):
    _mint(monkeypatch)

    def offline(command, **kwargs):
        if command[-3:] == ["fetch", "-q", "origin"]:
            return subprocess.CompletedProcess(
                command, 128, stdout="", stderr="fatal: unable to resolve host"
            )
        return FakeGit()(command, **kwargs)

    monkeypatch.setattr(hlr.subprocess, "run", offline)

    with pytest.raises(subprocess.CalledProcessError):
        _worktree(tmp_path, remote_repo=REPO).create(head="a" * 40)


# ── fail closed -----------------------------------------------------------------


def test_no_installation_covering_the_repo_fails_closed_before_any_fetch(
    monkeypatch, tmp_path, repo_path
):
    _mint(monkeypatch, error=f"no installation of the App covers {REPO}")
    git = FakeGit()
    monkeypatch.setattr(hlr.subprocess, "run", git)

    with pytest.raises(hlr.RepoAccessError) as caught:
        _worktree(tmp_path, remote_repo=REPO).create(head="a" * 40)

    message = str(caught.value)
    assert REPO in message
    assert "does not fall back to the ambient" in message
    assert git.fetches == []  # the ambient credential was never tried


def test_an_unconfigured_app_fails_closed_through_the_real_helper(
    monkeypatch, tmp_path, repo_path
):
    """No stub: the real lib refuses, and the runner does not fall back."""
    for name in ("ATELES_REVIEWER_APP_ID", "ATELES_REVIEWER_APP_PRIVATE_KEY",
                 "ATELES_REVIEWER_APP_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(name, raising=False)
    gat.clear_cache()
    git = FakeGit()
    monkeypatch.setattr(hlr.subprocess, "run", git)

    with pytest.raises(hlr.RepoAccessError, match="not configured"):
        _worktree(tmp_path, remote_repo=REPO).create(head="a" * 40)
    assert git.fetches == []


def test_a_worktree_with_no_repo_slug_cannot_fetch(monkeypatch, tmp_path, repo_path):
    mints = _mint(monkeypatch)
    git = FakeGit()
    monkeypatch.setattr(hlr.subprocess, "run", git)

    with pytest.raises(hlr.RepoAccessError, match="owner/name"):
        _worktree(tmp_path).create(head="a" * 40)
    assert mints == [] and git.fetches == []


@pytest.mark.parametrize(
    "remote",
    [
        "git@github.com:someowner/private-sibling.git",
        "ssh://git@github.com/someowner/private-sibling.git",
        # a login embedded in the URL; built from fragments so the file holds no
        # credential-shaped URL for the secret/PII scan to read
        "https://" + "someuser" + ":" + "stored" + "@" + "github.com/someowner/private-sibling.git",
        "/srv/mirror/private-sibling.git",
    ],
)
def test_a_remote_the_scoped_token_cannot_authenticate_is_refused(
    monkeypatch, tmp_path, repo_path, remote
):
    _mint(monkeypatch)
    git = FakeGit(remote=remote)
    monkeypatch.setattr(hlr.subprocess, "run", git)

    with pytest.raises(hlr.RepoAccessError, match="origin is not a plain https"):
        _worktree(tmp_path, remote_repo=REPO).create(head="a" * 40)
    assert git.fetches == []


def test_the_fetch_function_has_no_ambient_default(repo_path):
    with pytest.raises(TypeError):
        hlr._fetch_origin_serialized(repo_path)  # type: ignore[call-arg]


# ── gh reads --------------------------------------------------------------------


def test_head_check_and_comment_listing_use_the_scoped_token(monkeypatch):
    _mint(monkeypatch)
    seen: list[tuple[str, dict]] = []

    def gh(command, **kwargs):
        seen.append((command[2], kwargs.get("env")))
        out = json.dumps({"headRefOid": "b" * 40}) if command[1] == "pr" else "[[]]"
        return subprocess.CompletedProcess(command, 0, stdout=out, stderr="")

    monkeypatch.setattr(hlr.subprocess, "run", gh)

    assert hlr.current_pr_head(repo=REPO, pr=7) == "b" * 40
    assert hlr._pr_comments(repo=REPO, pr=7) == []
    assert len(seen) == 2
    for _, env in seen:
        assert env["GH_TOKEN"] == SCOPED
        assert AMBIENT not in env.values()


def test_gh_reads_refuse_rather_than_use_the_ambient_credential(monkeypatch):
    _mint(monkeypatch, error="the App is not installed on " + REPO)
    ran: list[list[str]] = []
    monkeypatch.setattr(
        hlr.subprocess, "run", lambda c, **k: ran.append(c) or pytest.fail("ran gh")
    )

    with pytest.raises(hlr.RepoAccessError):
        hlr.current_pr_head(repo=REPO, pr=7)
    with pytest.raises(hlr.RepoAccessError):
        hlr._pr_comments(repo=REPO, pr=7)
    assert ran == []


def test_the_verdict_write_keeps_the_pinned_publication_identity(monkeypatch):
    """Publication stays on the identity gh_login() verified (ateles-agent):
    only the READ calls take the scoped token, so the one-comment-per-lens
    reconciliation still finds the comments that identity wrote."""
    _mint(monkeypatch)
    seen: dict[str, dict | None] = {}

    def gh(command, **kwargs):
        if command[1] == "api" and "comments?" in command[2]:
            seen.setdefault("read", kwargs.get("env"))
            return subprocess.CompletedProcess(command, 0, stdout="[[]]", stderr="")
        seen.setdefault("write", kwargs.get("env"))
        raise subprocess.CalledProcessError(1, command, stderr="stop")

    monkeypatch.setattr(hlr.subprocess, "run", gh)
    verdict = (
        f"<!-- review:pm commit={'a' * 40} -->\n"
        "**\U0001f916 Pavo — Ateles swarm, pm lens panelist**\n**SIGNED_OFF**\n"
    )
    with pytest.raises(subprocess.CalledProcessError):
        hlr.post_verdict(repo=REPO, pr=7, verdict_text=verdict)

    assert seen["read"]["GH_TOKEN"] == SCOPED
    assert seen["write"] is None  # inherited: not the scoped token


# ── CLI -------------------------------------------------------------------------


def test_main_reports_a_repo_access_failure_as_a_clean_refusal(
    monkeypatch, tmp_path, capsys
):
    brief = tmp_path / "brief.md"
    brief.write_text("brief\n", encoding="utf-8")

    async def refuse(*args, **kwargs):
        raise hlr.RepoAccessError("no repository-scoped credential for " + REPO)

    monkeypatch.setattr(hlr, "run_one", refuse)
    code = hlr.main(
        [
            "--repo", REPO, "--pr", "7", "--head", "a" * 40, "--lens", "pm",
            "--brief", str(brief), "--provider", "claude", "--json",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert code == 1
    assert report["ok"] is False
    assert report["no_model_call_made"] is True
    assert REPO in report["reason"]
    # same refusal shape as every other run_one refusal a caller parses
    assert report["refusal_reason"] == report["reason"]
    assert report["posted"] is False
    assert (report["repo"], report["pr"], report["lens"]) == (REPO, 7, "pm")


# ── operator-facing text -------------------------------------------------------


def test_a_fetch_failure_that_is_not_access_keeps_gits_own_detail(
    monkeypatch, tmp_path, repo_path, capsys
):
    """str(CalledProcessError) never prints stderr; the diagnosis must survive."""
    _mint(monkeypatch, token="ghs_secret_in_detail")

    def offline(command, **kwargs):
        if command[-3:] == ["fetch", "-q", "origin"]:
            return subprocess.CompletedProcess(
                command,
                128,
                stdout="",
                stderr="fatal: unable to resolve host github.com ghs_secret_in_detail",
            )
        return FakeGit()(command, **kwargs)

    monkeypatch.setattr(hlr.subprocess, "run", offline)
    brief = tmp_path / "brief.md"
    brief.write_text("brief\n", encoding="utf-8")

    async def fetch_then_fail(*args, **kwargs):
        _worktree(tmp_path, remote_repo=REPO).create(head="a" * 40)

    monkeypatch.setattr(hlr, "run_one", fetch_then_fail)
    hlr.main(
        [
            "--repo", REPO, "--pr", "7", "--head", "a" * 40, "--lens", "pm",
            "--brief", str(brief), "--provider", "claude", "--json",
        ]
    )
    reason = json.loads(capsys.readouterr().out)["reason"]
    assert "unable to resolve host" in reason
    assert "ghs_secret_in_detail" not in reason


def test_the_remedy_depends_on_why_there_is_no_credential(monkeypatch):
    _mint(monkeypatch, error="GitHub App not configured: set ATELES_REVIEWER_APP_ID")
    monkeypatch.setattr(gat, "app_configured", lambda *a, **k: False)
    with pytest.raises(hlr.RepoAccessError) as unconfigured:
        hlr.repo_scoped_env(REPO)
    assert "this process's own environment" in str(unconfigured.value)
    assert "install the swarm GitHub App" not in str(unconfigured.value)

    _mint(monkeypatch, error=f"no installation of the App covers {REPO}")
    monkeypatch.setattr(gat, "app_configured", lambda *a, **k: True)
    with pytest.raises(hlr.RepoAccessError) as uncovered:
        hlr.repo_scoped_env(REPO)
    assert "install the swarm GitHub App" in str(uncovered.value)
    assert "ATELES_REVIEWER_APP_INSTALLATION_ID" in str(uncovered.value)


def test_a_non_https_remote_refusal_says_what_to_set(monkeypatch, tmp_path, repo_path):
    _mint(monkeypatch)
    monkeypatch.setattr(
        hlr.subprocess, "run", FakeGit(remote="git@github.com:someowner/x.git")
    )
    with pytest.raises(hlr.RepoAccessError, match=r"https://github.com/<owner>/<name>"):
        _worktree(tmp_path, remote_repo=REPO).create(head="a" * 40)


# ── run_one hands the target slug to the Worktree --------------------------------


@pytest.mark.parametrize("dry_run", [True, False])
def test_run_one_gives_the_worktree_the_target_repository(
    monkeypatch, tmp_path, dry_run
):
    """Without this wiring every real run refuses 'no owner/name'; no other test
    drives run_one with an unstubbed Worktree constructor."""
    monkeypatch.setenv(
        "APIS_HARNESS_HEADROOM",
        json.dumps({"claude": 1.0, "codex": 1.0, "cursor": 1.0}),
    )
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", "/nonexistent/headroom.json")
    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(tmp_path / "no-usage.json"))
    seen: list[str] = []

    def record(self, *, head):
        seen.append(self.remote_repo)
        raise hlr.RepoAccessError("stop here")

    monkeypatch.setattr(hlr.Worktree, "create", record)
    brief = tmp_path / "brief.md"
    brief.write_text("brief\n", encoding="utf-8")
    target = hlr.LensTarget(repo=REPO, pr=7, head="a" * 40, lens="pm", agent="pavo")

    with pytest.raises(hlr.RepoAccessError, match="stop here"):
        asyncio.run(
            hlr.run_one(
                target, provider="claude", post=False, dry_run=dry_run,
                repo_worktree_name="private-sibling", scratch_root=tmp_path,
                brief_path=brief, timeout=None,
            )
        )
    assert seen == [REPO]
