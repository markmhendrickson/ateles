"""The token a delivering Fly job gets: one repository, two permissions.

A Fly job that pushes a branch and opens a pull request gets a fresh token
from the PR-opener App, requested for exactly one repository with
``contents: write`` and ``pull_requests: write``. What GitHub returns is
checked before the token is used: any difference in permissions, repository
scope or expiry revokes it and keeps the job off Fly. A job that could outlive
the token's fixed one-hour life is refused before anything is minted.

Offline: every GitHub call is a stand-in.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

import fly_runner  # noqa: E402
from lib import github_app_token as gat  # noqa: E402

REPO = "owner/repo"
NOW = 1_800_000_000.0
MINTED = "ghs_delivery_token_value_placeholder_0001"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _granted(**overrides):
    payload = {
        "token": MINTED,
        "expires_at": _iso(NOW + 3600),
        "permissions": {"contents": "write", "pull_requests": "write", "metadata": "read"},
        "repository_selection": "selected",
        "repositories": [{"full_name": REPO, "name": "repo"}],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def github(monkeypatch):
    """A stand-in GitHub and PR-opener App; records every call and prefix."""
    state = {"calls": [], "prefixes": [], "revoked": [], "payload": _granted(),
             "configured": {gat.pr_opener_prefix()}}

    def configured(prefix):
        state["prefixes"].append(prefix)
        return prefix in state["configured"]

    def jwt(prefix):
        state["prefixes"].append(prefix)
        return "app-jwt"

    def installation(prefix, repo, factory):
        state["prefixes"].append(prefix)
        return "42"

    def request(method, path, bearer, body=None):
        state["calls"].append((method, path, bearer, body))
        if isinstance(state["payload"], Exception):
            raise state["payload"]
        return state["payload"]

    monkeypatch.setattr(gat, "app_configured", configured)
    monkeypatch.setattr(gat, "mint_app_jwt", jwt)
    monkeypatch.setattr(gat, "_resolve_installation_id", installation)
    monkeypatch.setattr(fly_runner, "_github_request", request)
    return state


def _mint(state, timeout=1800, clock=lambda: NOW):
    return fly_runner.mint_delivery_github_token(
        REPO, timeout=timeout, clock=clock, revoke=state["revoked"].append)


# ── The request ──────────────────────────────────────────────────────────────


def test_the_request_names_one_repository_and_exactly_two_permissions(github):
    minted = _mint(github)
    assert minted.token == MINTED and minted.repository == REPO
    assert minted.expires_at == NOW + 3600
    assert github["calls"] == [(
        "POST", "/app/installations/42/access_tokens", "app-jwt",
        {"repositories": ["repo"],
         "permissions": {"contents": "write", "pull_requests": "write"}},
    )]
    assert github["revoked"] == []


def test_the_token_comes_from_the_pr_opener_app_only(github):
    _mint(github)
    assert set(github["prefixes"]) == {gat.pr_opener_prefix()}


def test_an_unconfigured_pr_opener_app_never_falls_back(github):
    # The read-class (swarm) App is configured; the PR-opener App is not.
    github["configured"] = {"ATELES_REVIEWER_APP"}
    with pytest.raises(fly_runner.FlyIneligible, match="PR-opener App is not configured"):
        _mint(github)
    assert github["calls"] == [] and github["revoked"] == []


def test_a_minted_token_never_shows_in_its_repr(github):
    assert MINTED not in repr(_mint(github))


def test_metadata_read_may_be_listed_or_omitted(github):
    github["payload"] = _granted(permissions={"contents": "write", "pull_requests": "write"})
    assert _mint(github).token == MINTED


@pytest.mark.parametrize("repository", ["", "repo", "owner/", "/repo", "owner/repo/extra",
                                        "owner/..", "own er/repo", "owner/re po",
                                        "https://github.com/owner/repo", None])  # fmt: skip
def test_a_delivery_needs_exactly_one_owner_name_repository(github, repository):
    with pytest.raises(fly_runner.FlyIneligible, match="exactly one owner/name repository"):
        fly_runner.mint_delivery_github_token(repository, timeout=1800, clock=lambda: NOW,
                                              revoke=github["revoked"].append)  # fmt: skip
    assert github["calls"] == []


# ── What GitHub granted must be exactly what was asked for ───────────────────

_MISMATCHES = {
    "extra-workflows": {"permissions": {"contents": "write", "pull_requests": "write",
                                        "metadata": "read", "workflows": "write"}},
    "extra-issues": {"permissions": {"contents": "write", "pull_requests": "write",
                                     "issues": "write"}},
    "extra-administration": {"permissions": {"contents": "write", "pull_requests": "write",
                                             "administration": "read"}},
    "metadata-write": {"permissions": {"contents": "write", "pull_requests": "write",
                                       "metadata": "write"}},
    "contents-read-only": {"permissions": {"contents": "read", "pull_requests": "write"}},
    "pull-requests-missing": {"permissions": {"contents": "write"}},
    "permissions-absent": {"permissions": None},
    "permissions-not-an-object": {"permissions": ["contents"]},
    "all-repositories": {"repository_selection": "all"},
    "selection-absent": {"repository_selection": None},
    "two-repositories": {"repositories": [{"full_name": REPO}, {"full_name": "owner/other"}]},
    "another-repository": {"repositories": [{"full_name": "owner/other"}]},
    "repositories-absent": {"repositories": None},
    "repositories-empty": {"repositories": []},
    "repository-entry-not-an-object": {"repositories": [REPO]},
    "expiry-absent": {"expires_at": None},
    "expiry-unreadable": {"expires_at": "soon"},
    "expiry-without-zone": {"expires_at": "2027-01-15T08:00:00"},
    "already-expired": {"expires_at": _iso(NOW - 1)},
    "longer-than-an-installation-token": {"expires_at": _iso(NOW + 3 * 3600)},
}


@pytest.mark.parametrize("case", sorted(_MISMATCHES))
def test_a_mismatched_grant_is_revoked_and_refused(github, case):
    github["payload"] = _granted(**_MISMATCHES[case])
    with pytest.raises(fly_runner.FlyIneligible) as exc:
        _mint(github)
    assert github["revoked"] == [MINTED]  # never left alive
    assert MINTED not in str(exc.value)


@pytest.mark.parametrize("payload", [_granted(token=""), _granted(token=None), ["not", "an",
                                     "object"], "text"])  # fmt: skip
def test_a_response_without_a_token_is_refused_with_nothing_to_revoke(github, payload):
    github["payload"] = payload
    with pytest.raises(fly_runner.FlyIneligible):
        _mint(github)
    assert github["revoked"] == []


def test_the_repository_name_is_compared_without_case(github):
    github["payload"] = _granted(repositories=[{"full_name": "Owner/Repo"}])
    assert _mint(github).token == MINTED


def test_a_failed_mint_is_refused_and_names_no_value(github):
    github["payload"] = RuntimeError("POST /app/installations/42/access_tokens -> HTTP 422")
    with pytest.raises(fly_runner.FlyIneligible, match="delivery token mint failed"):
        _mint(github)
    assert github["revoked"] == []


def test_a_mismatch_is_refused_even_when_the_revoke_fails(github):
    github["payload"] = _granted(repository_selection="all")

    def failing_revoke(token):
        raise RuntimeError("DELETE /installation/token -> HTTP 500")

    with pytest.raises(fly_runner.FlyIneligible, match="selected repositories"):
        fly_runner.mint_delivery_github_token(REPO, timeout=1800, clock=lambda: NOW,
                                              revoke=failing_revoke)  # fmt: skip


# ── The token must outlive the job ───────────────────────────────────────────


def test_the_default_dispatch_timeout_fits_the_token_life(github):
    assert fly_runner.delivery_fits_token_life(fly_runner.DISPATCH_TIMEOUT_DEFAULT,
                                               NOW + 3600, NOW)  # fmt: skip
    assert _mint(github, timeout=fly_runner.DISPATCH_TIMEOUT_DEFAULT).token == MINTED


def test_a_job_that_could_outlive_any_token_is_refused_before_minting(github):
    longest = (fly_runner.GITHUB_TOKEN_LIFETIME_SECONDS - fly_runner.DELIVERY_RESERVE_SECONDS
               - fly_runner.APIS_DEADLINE_GRACE_SECONDS - fly_runner.TOKEN_EXPIRY_MARGIN_SECONDS)
    assert _mint(github, timeout=longest).token == MINTED
    github["calls"].clear()
    with pytest.raises(fly_runner.FlyIneligible, match="stays on the Mac"):
        _mint(github, timeout=longest + 1)
    assert github["calls"] == []  # nothing minted


def test_a_token_that_expires_before_the_job_would_end_is_revoked(github):
    github["payload"] = _granted(expires_at=_iso(NOW + 1800))  # less than the run needs
    with pytest.raises(fly_runner.FlyIneligible, match="expires before"):
        _mint(github, timeout=1800)
    assert github["revoked"] == [MINTED]


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), "1800", None])
def test_an_invalid_timeout_is_refused_before_minting(github, timeout):
    with pytest.raises(fly_runner.FlyIneligible):
        _mint(github, timeout=timeout)
    assert github["calls"] == []


def test_the_run_a_token_must_cover_includes_the_delivery_steps():
    assert fly_runner.delivery_run_seconds(1800) == (
        1800 + fly_runner.DELIVERY_RESERVE_SECONDS + fly_runner.APIS_DEADLINE_GRACE_SECONDS)


# ── Nothing else changes ─────────────────────────────────────────────────────


def test_the_read_class_job_token_is_unchanged(monkeypatch):
    calls = []
    monkeypatch.setattr(gat, "app_configured", lambda prefix: True)
    monkeypatch.setattr(gat, "mint_app_jwt", lambda prefix: "app-jwt")
    monkeypatch.setattr(gat, "_resolve_installation_id", lambda prefix, repo, f: "42")
    monkeypatch.setattr(fly_runner, "_github_request",
                        lambda m, p, b, body=None: calls.append(body) or {"token": "t"})
    fly_runner.mint_job_github_token(REPO, "ATELES_REVIEWER_APP")
    assert calls == [{"repositories": ["repo"]}]  # no permissions narrowing added there


def test_the_executor_still_mints_with_the_read_class_function():
    import inspect

    defaults = inspect.signature(fly_runner.FlyExecutor.__init__).parameters
    assert defaults["mint"].default is fly_runner.mint_job_github_token


# ── What the log says when a refused token is revoked, or cannot be ──────────

_NO_DEADLINE_PROMISE = ("within the hour", "expires within", "one hour", "will expire")


def _failing_revoke(token):
    raise RuntimeError("DELETE /installation/token -> HTTP 500")


def _refuse_with_failed_revoke(github, caplog, payload, match, timeout=1800):
    github["payload"] = payload
    caplog.set_level("INFO", logger="apis.fly_runner")
    with pytest.raises(fly_runner.FlyIneligible, match=match) as exc:
        fly_runner.mint_delivery_github_token(REPO, timeout=timeout, clock=lambda: NOW,
                                              revoke=_failing_revoke)  # fmt: skip
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert MINTED not in text and MINTED not in str(exc.value)
    assert "attempted and failed" in text and "revoke it manually" in text
    return text


@pytest.mark.parametrize("expiry", [None, "soon", "2027-01-15T08:00:00", _iso(NOW + 3 * 3600),
                                    _iso(NOW - 1)])  # fmt: skip
def test_a_failed_revoke_after_an_unconfirmed_expiry_promises_no_deadline(github, caplog,
                                                                          expiry):  # fmt: skip
    text = _refuse_with_failed_revoke(github, caplog, _granted(expires_at=expiry), "expir")
    assert "its expiry was not confirmed" in text
    assert not any(p in text for p in _NO_DEADLINE_PROMISE)


@pytest.mark.parametrize("case,match", [("extra-workflows", "permissions differ"),
                                        ("all-repositories", "selected repositories")])
def test_a_failed_revoke_after_a_grant_refusal_promises_no_deadline(github, caplog, case,
                                                                     match):  # fmt: skip
    text = _refuse_with_failed_revoke(github, caplog, _granted(**_MISMATCHES[case]), match)
    assert "its expiry was not confirmed" in text
    assert not any(p in text for p in _NO_DEADLINE_PROMISE)


def test_a_failed_revoke_after_a_validated_expiry_states_only_that_expiry(github, caplog):
    text = _refuse_with_failed_revoke(github, caplog, _granted(expires_at=_iso(NOW + 1800)),
                                      "expires before")  # fmt: skip
    assert "GitHub reported its expiry as " + fly_runner._render_wall(NOW + 1800) in text
    assert "not confirmed" not in text
    assert not any(p in text for p in _NO_DEADLINE_PROMISE)


def test_a_confirmed_revoke_is_reported_as_done(github, caplog):
    github["payload"] = _granted(repository_selection="all")
    caplog.set_level("INFO", logger="apis.fly_runner")
    with pytest.raises(fly_runner.FlyIneligible, match="selected repositories"):
        _mint(github)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert github["revoked"] == [MINTED]
    assert "a refused delivery token was revoked" in text and "failed" not in text
    assert MINTED not in text
