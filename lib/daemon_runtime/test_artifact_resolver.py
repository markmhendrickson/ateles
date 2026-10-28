"""Exact-match read-back of a delivered pull request (offline: no GitHub)."""

from __future__ import annotations

import json
import subprocess

import pytest

from lib.daemon_runtime import artifact_resolver as resolver

REPO = "owner/repo"
BRANCH = "cicada/delivery-123456"
COMMIT = "a" * 40
EXPECTED = dict(repo=REPO, number=99, base="main", branch=BRANCH, commit=COMMIT)


def pr(**over):
    data = {
        "number": 99,
        "baseRefName": "main",
        "headRefName": BRANCH,
        "headRefOid": COMMIT,
        "headRepository": {"nameWithOwner": REPO},
    }
    data.update(over)
    return data


@pytest.fixture
def github(monkeypatch):
    """Serve one canned `gh pr view` answer; count the asks."""
    box = {"answer": (0, json.dumps(pr()), ""), "asks": 0, "tokens": []}

    def view(repo, number, token=None):
        box["asks"] += 1
        box["tokens"].append(token)
        answer = box["answer"]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(resolver, "_gh_pr_view", view)
    monkeypatch.setattr(resolver.time, "sleep", lambda s: None)
    return box


def read(**over):
    return resolver.read_back_delivered_pr(**{**EXPECTED, **over})


def test_matching_pull_request_passes(github):
    assert read() is None


def test_same_number_but_a_different_head_commit_fails(github):
    github["answer"] = (0, json.dumps(pr(headRefOid="b" * 40)), "")
    reason = read()
    assert reason and "head commit" in reason and "b" * 40 in reason


@pytest.mark.parametrize(
    "field, answer, named",
    [
        ("baseRefName", "release", "base branch"),
        ("headRefName", "someone/else", "head branch"),
        ("headRepository", {"nameWithOwner": "fork/repo"}, "repository"),
        ("number", 100, "pull request number"),
    ],
)
def test_any_other_differing_field_fails_and_is_named(github, field, answer, named):
    github["answer"] = (0, json.dumps(pr(**{field: answer})), "")
    reason = read()
    assert reason and named in reason


def test_repository_comparison_ignores_case_only(github):
    github["answer"] = (
        0,
        json.dumps(pr(headRepository={"nameWithOwner": "Owner/Repo"})),
        "",
    )
    assert read() is None


@pytest.mark.parametrize(
    "answer",
    [
        (1, "", "HTTP 404: Not Found"),
        (1, "", "HTTP 502: Bad Gateway"),
        (0, "not json", ""),
        (0, json.dumps([pr()]), ""),
        (0, json.dumps(pr(headRepository=None)), ""),
        (0, json.dumps({k: v for k, v in pr().items() if k != "headRefOid"}), ""),
        OSError("gh missing"),
        subprocess.TimeoutExpired("gh", 30),
    ],
    ids=[
        "404",
        "5xx",
        "malformed",
        "wrong-shape",
        "no-head-repo",
        "no-oid",
        "oserror",
        "timeout",
    ],
)
def test_an_unreadable_or_incomplete_pull_request_fails(github, answer):
    github["answer"] = answer
    reason = read()
    assert reason and ("not verified" in reason or "does not match" in reason)


def test_a_404_is_not_retried_but_an_outage_is(github):
    github["answer"] = (1, "", "HTTP 404: Not Found")
    read()
    assert github["asks"] == 1
    github["asks"] = 0
    github["answer"] = (1, "", "HTTP 503: Service Unavailable")
    read()
    assert github["asks"] == 3


def test_a_transient_failure_then_a_matching_answer_passes(github, monkeypatch):
    answers = iter([(1, "", "HTTP 503"), (0, json.dumps(pr()), "")])
    monkeypatch.setattr(resolver, "_gh_pr_view", lambda repo, number, token=None: next(answers))
    assert read() is None


@pytest.mark.parametrize(
    "field, name",
    [
        ("repo", "repository"),
        ("number", "pull request number"),
        ("base", "base branch"),
        ("branch", "head branch"),
        ("commit", "head commit"),
    ],
)
@pytest.mark.parametrize("missing", [None, "", "  "])
def test_a_field_the_runner_did_not_report_fails_without_asking(
    github, field, name, missing
):
    reason = read(**{field: missing})
    assert reason and name in reason and "did not report" in reason
    assert github["asks"] == 0


# -- read-back credential -----------------------------------------------------


def test_every_read_uses_the_token_from_the_provider(github):
    asked = []

    def provider(repo):
        asked.append(repo)
        return "tok-1"

    github["answer"] = (1, "", "HTTP 503: Service Unavailable")
    read(token_provider=provider)
    assert github["asks"] == 3
    assert github["tokens"] == ["tok-1"] * 3
    # Minted once, for the delivered repository, before the retries.
    assert asked == [REPO]


def test_a_matching_pull_request_read_with_a_token_passes(github):
    assert read(token_provider=lambda repo: "tok-1") is None
    assert github["tokens"] == ["tok-1"]


def test_without_a_provider_the_ambient_credential_is_used(github):
    assert read() is None
    assert github["tokens"] == [None]


@pytest.mark.parametrize("outcome", [RuntimeError("no app"), "", None])
def test_an_unavailable_credential_fails_closed_without_asking(github, outcome):
    def provider(repo):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    reason = read(token_provider=provider)
    assert reason == (
        f"{REPO}#99: the read-back credential is unavailable; "
        "the delivery is not verified"
    )
    assert github["asks"] == 0


def _capture_run(monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    monkeypatch.setattr(resolver.subprocess, "run", run)
    return seen


def test_gh_pr_view_authenticates_with_the_given_token(monkeypatch):
    seen = _capture_run(monkeypatch)
    resolver._gh_pr_view(REPO, 99, token="tok-2")
    assert seen["env"]["GH_TOKEN"] == "tok-2"
    assert seen["env"]["GITHUB_TOKEN"] == "tok-2"
    assert "tok-2" not in seen["argv"]


@pytest.mark.parametrize("token", [None, ""])
def test_gh_pr_view_without_a_token_keeps_the_ambient_environment(
    monkeypatch, token
):
    seen = _capture_run(monkeypatch)
    resolver._gh_pr_view(REPO, 99, token=token)
    assert "env" not in seen
