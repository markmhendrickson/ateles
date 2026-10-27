"""A Fly job never gets a token that can approve, push or open a pull request
unless it was launched as a delivery.

Two ways a worker job could have been handed the swarm reviewing App's token
in place of the PR-opener token, both closed here:

* a job that asked for GitHub delivery but carried no delivery plan fell
  through to the read-class mint, which asked the reviewing App for a token
  with no permission block (the App's whole grant);
* the read-class mint itself did not say what it wanted, so it took everything.

Offline: every GitHub and Fly call is a stand-in.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

import dispatch_role  # noqa: E402
import fly_runner  # noqa: E402
from lib import github_app_token as gat  # noqa: E402
from lib.daemon_runtime import AgentDefinition  # noqa: E402
from skill_runner import SkillResult  # noqa: E402
from test_fly_runner import (  # noqa: E402,F401 -- fixtures
    FakeFly,
    _claude_runs_per_job_unless_its_login_machine_is_tested,
    make_executor,
)
from test_fly_runner import fly_env as _fly_env_fixture  # noqa: E402

fly_env = _fly_env_fixture  # the shared fixture, re-exported so pytest finds it here

REPO = "owner/repo"
NOW = 1_800_000_000.0
MINTED = "ghs_read_class_token_value_placeholder_0001"
REVIEWER_PREFIX = "ATELES_REVIEWER_APP"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _granted(**overrides):
    payload = {
        "token": MINTED,
        "expires_at": _iso(NOW + 3600),
        "permissions": {"contents": "read", "pull_requests": "read", "metadata": "read"},
        "repository_selection": "selected",
        "repositories": [{"full_name": REPO, "name": "repo"}],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def github(monkeypatch):
    state = {"calls": [], "prefixes": [], "revoked": [], "payload": _granted()}

    def configured(prefix):
        state["prefixes"].append(prefix)
        return True

    def jwt(prefix):
        state["prefixes"].append(prefix)
        return "app-jwt"

    def installation(prefix, repo, factory):
        state["prefixes"].append(prefix)
        return "42"

    def request(method, path, bearer, body=None):
        state["calls"].append((method, path, bearer, body))
        return state["payload"]

    monkeypatch.setattr(gat, "app_configured", configured)
    monkeypatch.setattr(gat, "mint_app_jwt", jwt)
    monkeypatch.setattr(gat, "_resolve_installation_id", installation)
    monkeypatch.setattr(fly_runner, "_github_request", request)
    return state


def _mint(state):
    return fly_runner.mint_job_github_token(
        REPO, REVIEWER_PREFIX, clock=lambda: NOW, revoke=state["revoked"].append)


# ── The read-class mint says what it wants, and checks what it got ───────────


def test_the_read_class_mint_asks_for_read_only_permissions(github):
    assert _mint(github) == MINTED
    assert github["calls"] == [(
        "POST", "/app/installations/42/access_tokens", "app-jwt",
        {"repositories": ["repo"],
         "permissions": {"contents": "read", "pull_requests": "read"}},
    )]
    assert github["revoked"] == []


def test_the_requested_permissions_can_all_be_read_only():
    assert set(fly_runner.READ_TOKEN_PERMISSIONS.values()) == {"read"}
    assert set(fly_runner.READ_TOKEN_IMPLICIT_PERMISSIONS.values()) == {"read"}


@pytest.mark.parametrize("permissions", [
    {"contents": "write", "pull_requests": "write", "metadata": "read"},  # the App's grant
    {"contents": "read", "pull_requests": "write", "metadata": "read"},
    {"contents": "read", "pull_requests": "read", "workflows": "write", "metadata": "read"},
    {"contents": "read", "metadata": "read"},  # narrower than asked is also a mismatch
])
def test_a_token_granted_more_broadly_than_asked_is_refused_and_revoked(github, permissions):
    github["payload"] = _granted(permissions=permissions)
    with pytest.raises(fly_runner.FlyIneligible, match="permissions differ"):
        _mint(github)
    assert github["revoked"] == [MINTED]


@pytest.mark.parametrize("case", [
    {"permissions": None},
    {"repository_selection": "all"},
    {"repositories": [{"full_name": "owner/other", "name": "other"}]},
    {"expires_at": None},
])
def test_any_other_grant_mismatch_is_refused_and_revoked(github, case):
    github["payload"] = _granted(**case)
    with pytest.raises(fly_runner.FlyIneligible):
        _mint(github)
    assert github["revoked"] == [MINTED]


def test_a_response_with_no_token_is_refused_without_a_revoke(github):
    github["payload"] = {"permissions": {"contents": "read"}}
    with pytest.raises(fly_runner.FlyIneligible, match="no token"):
        _mint(github)
    assert github["revoked"] == []


# ── A delivery contract with no delivery plan never reaches a mint ───────────


def _delivery_contract_job(**kw):
    return fly_runner.build_job(
        provider="claude", role="lanius",
        cmd=["/opt/homebrew/bin/claude", "--print", "--mcp-config", "/tmp/apis_mcp_x.json"],
        stdin=b"the task", timeout=60, repository=REPO, github_requested=True,
        mcp_config_path="/tmp/apis_mcp_x.json", **kw)  # fmt: skip


def test_building_a_delivery_contract_job_without_a_plan_is_refused(fly_env):
    with pytest.raises(fly_runner.FlyIneligible, match="delivery plan"):
        _delivery_contract_job(github_delivery_contract=True)


def test_the_launch_refuses_a_delivery_contract_job_before_any_mint(fly_env):
    # Even if a job slipped past build_job, the launch must not take the
    # read-class branch for it.
    job = _delivery_contract_job()
    job.github_delivery_contract = True
    fake = FakeFly()
    executor, minted, revoked = make_executor(fake, record=None)
    with pytest.raises(fly_runner.FlyIneligible, match="delivery plan"):
        asyncio.run(executor.run(job))
    assert minted == [] and revoked == []
    assert fake.machines == {}  # nothing was created
    assert job.secrets.get("GITHUB_TOKEN") is None


def test_a_plain_read_class_job_still_takes_the_read_class_mint(fly_env):
    fake = FakeFly()
    executor, minted, _ = make_executor(fake, record={"exit_code": 0, "stdout": "x"})
    asyncio.run(executor.run(_delivery_contract_job()))
    assert minted == [REPO]


def test_the_default_read_class_prefix_is_never_given_to_a_delivery_contract(fly_env):
    # The reviewer App is the default for the read-class mint. The only job
    # that may use it is one with no delivery contract.
    assert fly_runner.FlyConfig.from_env().github_app_prefix == REVIEWER_PREFIX
    with pytest.raises(fly_runner.FlyIneligible):
        _delivery_contract_job(github_delivery_contract=True)


# ── dispatch_role --runner fly --github-delivery never reaches a mint ────────


@pytest.fixture
def skills(monkeypatch, tmp_path):
    skill = tmp_path / ".claude" / "skills" / "lanius"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# lanius\n")
    monkeypatch.setattr(dispatch_role, "ATELES_REPO", tmp_path)

    async def _must_not_run(*a, **k):  # pragma: no cover - must not run
        raise AssertionError("run_skill reached for a Fly delivery with no plan")

    monkeypatch.setattr(dispatch_role, "run_skill", _must_not_run)
    return tmp_path


def test_the_cli_refuses_fly_with_github_delivery_and_never_dispatches(skills, capsys, github):
    rc = dispatch_role.main([
        "--role", "lanius", "--task", "t", "--runner", "fly",
        "--github-delivery", "--repository", REPO, "--json"])  # fmt: skip
    out = capsys.readouterr().out
    assert rc == 2
    assert "delivery plan" in out and "PR-opener" in out
    assert github["calls"] == [] and github["prefixes"] == []  # no App was asked for anything


def test_the_cli_refuses_it_with_a_token_env_too(skills, monkeypatch, github):
    monkeypatch.setenv("SOME_SCOPED_TOKEN", "x")
    rc = dispatch_role.main([
        "--role", "lanius", "--task", "t", "--runner", "fly", "--github-delivery",
        "--github-token-env", "SOME_SCOPED_TOKEN", "--repository", REPO])  # fmt: skip
    assert rc == 2
    assert github["calls"] == []


def test_the_programmatic_dispatch_refuses_it_too(skills, github):
    result = asyncio.run(dispatch_role.dispatch(
        "lanius", "t", runner="fly", github_delivery=True, repository=REPO))
    assert isinstance(result, SkillResult) and not result.ok
    assert "delivery plan" in result.error
    assert github["calls"] == []


def test_the_read_flag_asks_for_the_read_class_token_and_nothing_stronger(skills, monkeypatch):
    seen = {}

    async def _capture(skill, prompt, **kwargs):
        seen.update(kwargs)
        return SkillResult(skill, True, 0, "out", "", provider="claude")

    monkeypatch.setattr(dispatch_role, "run_skill", _capture)
    monkeypatch.setattr(dispatch_role, "_load_agent_def",
                        lambda role: AgentDefinition(name=role))
    rc = dispatch_role.main(["--role", "lanius", "--task", "t", "--runner", "fly",
                             "--github-read", "--repository", REPO])  # fmt: skip
    assert rc == 0
    assert seen["runner"] == "fly" and seen["repository"] == REPO
    assert seen["github_token"] == ""  # "requested": a read-class token is minted at launch
    assert seen["include_github_contract"] is False


def test_the_read_flag_with_a_local_token_never_falls_back_to_an_empty_one(skills, monkeypatch):
    seen = {}

    async def _capture(skill, prompt, **kwargs):
        seen.update(kwargs)
        return SkillResult(skill, True, 0, "out", "", provider="claude")

    monkeypatch.setattr(dispatch_role, "run_skill", _capture)
    monkeypatch.setattr(dispatch_role, "_load_agent_def",
                        lambda role: AgentDefinition(name=role))
    monkeypatch.setenv("SOME_SCOPED_TOKEN", "local-scoped-value")
    # A class opted onto Fly (no --runner) keeps its local credential for a fallback.
    rc = dispatch_role.main(["--role", "lanius", "--task", "t", "--github-read",
                             "--github-token-env", "SOME_SCOPED_TOKEN",
                             "--repository", REPO])  # fmt: skip
    assert rc == 0 and seen["github_token"] == "local-scoped-value"
    assert seen["include_github_contract"] is False
    # Without a local credential and without --runner fly, it is a usage error.
    assert dispatch_role.main(["--role", "lanius", "--task", "t", "--github-read",
                               "--repository", REPO]) == 2  # fmt: skip


@pytest.mark.parametrize("argv", [
    ["--github-read"],  # no fly
    ["--runner", "fly", "--github-read"],  # no repository
    ["--runner", "fly", "--github-read", "--repository", REPO, "--github-delivery"],
])
def test_the_read_flag_is_refused_when_misused(skills, argv):
    assert dispatch_role.main(["--role", "lanius", "--task", "t", *argv]) == 2
