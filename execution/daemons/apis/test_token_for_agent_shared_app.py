"""`_token_for_agent_on_repo`: the shared swarm App precedes the per-repo PAT.

An agent with no App and no personal token used to fall straight through to the
long-lived per-repo PAT (`_token_for_repo`). When that PAT expired, review and
steward runs on the repository could not read GitHub (HTTP 401). The shared
swarm App's short-lived installation token now sits between the agent's own
credentials and the per-repo PAT; Tier 0 (an agent's own App) is unchanged.

Run: pytest execution/daemons/apis/test_token_for_agent_shared_app.py -v
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

import swarm_dispatch as sd
from lib import github_app_token as gat

NEOTOMA = "example-owner/neotoma"


@pytest.fixture
def shared_app(monkeypatch):
    """Configure the shared swarm App and mint with no GitHub call."""
    calls: list[tuple[str | None, str]] = []

    def fake_info(repo=None, *, prefix=gat.DEFAULT_APP_ENV_PREFIX):
        calls.append((repo, prefix))
        return gat.InstallationToken(
            token=f"shared-app-token-for-{repo}",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            installation_id="42",
            permissions={},
        )

    monkeypatch.setattr(sd, "_app_write_configured", lambda: True)
    monkeypatch.setattr(gat, "installation_token_info", fake_info)
    sd._clear_app_write_token_cache()
    return calls


def _declare(monkeypatch, identity: str, *, usable: bool = True) -> None:
    prefix = gat.identity_env_prefix(identity)
    monkeypatch.setenv(f"{prefix}_ID", "1")
    if usable:
        monkeypatch.setenv(f"{prefix}_PRIVATE_KEY", "fake-key-not-used-for-signing")


def test_shared_app_precedes_the_repo_token(monkeypatch, shared_app):
    monkeypatch.setenv("NEOTOMA_AGENT_PAT", "ghp_expired_repo_pat")
    monkeypatch.delenv("LANIUS_AGENT_PAT", raising=False)
    assert sd._token_for_agent_on_repo("lanius", NEOTOMA) == f"shared-app-token-for-{NEOTOMA}"
    assert shared_app == [(NEOTOMA, sd._REVIEWER_APP_ENV_PREFIX)]


def test_falls_back_to_repo_token_when_app_absent(monkeypatch):
    # The conftest leaves the shared App unconfigured.
    monkeypatch.setenv("NEOTOMA_AGENT_PAT", "ghp_repo_pat")

    def must_not_mint(*a, **k):
        raise AssertionError("an unconfigured App must not be asked to mint")

    monkeypatch.setattr(gat, "installation_token_info", must_not_mint)
    assert sd._token_for_agent_on_repo("lanius", NEOTOMA) == "ghp_repo_pat"


def test_falls_back_to_repo_token_when_mint_fails(monkeypatch, caplog):
    monkeypatch.setattr(sd, "_app_write_configured", lambda: True)
    sd._clear_app_write_token_cache()
    monkeypatch.setenv("NEOTOMA_AGENT_PAT", "ghp_repo_pat")

    def refuse(repo=None, *, prefix=gat.DEFAULT_APP_ENV_PREFIX):
        raise gat.AppTokenError(f"the App is not installed on {repo}")

    monkeypatch.setattr(gat, "installation_token_info", refuse)
    with caplog.at_level(logging.WARNING):
        assert sd._token_for_agent_on_repo("lanius", NEOTOMA) == "ghp_repo_pat"
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("falling back to the agent PAT" in m for m in warnings)
    assert not any("ghp_repo_pat" in m for m in warnings)


def test_agent_own_app_still_wins(monkeypatch, shared_app):
    _declare(monkeypatch, "reviewer_pm")
    monkeypatch.setenv("NEOTOMA_AGENT_PAT", "ghp_repo_pat")
    monkeypatch.setattr(
        gat, "identity_token", lambda identity, repo=None: f"own-app-token-{identity}"
    )
    assert sd._token_for_agent_on_repo("pavo", NEOTOMA) == "own-app-token-reviewer_pm"
    assert shared_app == []


def test_declared_app_that_cannot_mint_still_refuses(monkeypatch, shared_app):
    _declare(monkeypatch, "reviewer_pm")
    monkeypatch.setenv("NEOTOMA_AGENT_PAT", "ghp_repo_pat")

    def refuse(identity, repo=None):
        raise gat.AppTokenError("HTTP 401")

    monkeypatch.setattr(gat, "identity_token", refuse)
    assert sd._token_for_agent_on_repo("pavo", NEOTOMA) == ""
    assert shared_app == []


def test_agent_own_pat_still_precedes_the_shared_app(monkeypatch, shared_app):
    monkeypatch.setenv("LANIUS_AGENT_PAT", "ghp_lanius_own")
    assert sd._token_for_agent_on_repo("lanius", NEOTOMA) == "ghp_lanius_own"
    assert shared_app == []
