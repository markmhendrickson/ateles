"""Tests for the review-comment upsert logic (in-place update instead of one
comment per push, per reviewer). Loads loxia_review as a module and stubs
urlopen so no network or env config is required."""

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import loxia_review as lx


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _resp(payload):
    return _FakeResp(json.dumps(payload).encode())


def test_marker_is_per_reviewer():
    # Each reviewer's marker matches its own `## {display} Review {emoji}`
    # heading so they never collide on a multi-reviewer PR.
    assert lx.review_comment_marker(lx.LOXIA) == "## Loxia Review 🪶"
    markers = {lx.review_comment_marker(r) for r in lx.DOMAIN_REVIEWERS.values()}
    markers.add(lx.review_comment_marker(lx.LOXIA))
    assert len(markers) == len(lx.DOMAIN_REVIEWERS) + 1  # all distinct


def test_find_existing_returns_latest_matching_marker(monkeypatch):
    loxia = lx.review_comment_marker(lx.LOXIA)
    comments = [
        {"id": 1, "body": "unrelated comment"},
        {"id": 2, "body": f"{loxia}\n\nVerdict: COMMENT"},
        {"id": 3, "body": "## Monedula Review 🪙\n\nVerdict: APPROVE"},
        {"id": 4, "body": f"{loxia}\n\nVerdict: REQUEST_CHANGES"},
    ]
    monkeypatch.setattr(lx, "GITHUB_TOKEN", "t")
    monkeypatch.setattr(lx, "PR_NUMBER", "87")
    monkeypatch.setattr(lx, "REPO", "owner/repo")
    monkeypatch.setattr(lx.urllib.request, "urlopen", lambda *a, **k: _resp(comments))
    # Loxia matches only its own comments, ignores Monedula's.
    assert lx.find_existing_review_comment(loxia) == 4


def test_find_existing_none_when_marker_absent(monkeypatch):
    comments = [{"id": 1, "body": "## Loxia Review 🪶 — old"}]
    monkeypatch.setattr(lx, "GITHUB_TOKEN", "t")
    monkeypatch.setattr(lx, "PR_NUMBER", "87")
    monkeypatch.setattr(lx, "REPO", "owner/repo")
    monkeypatch.setattr(lx.urllib.request, "urlopen", lambda *a, **k: _resp(comments))
    assert lx.find_existing_review_comment("## Monedula Review 🪙") is None


def test_post_patches_when_existing(monkeypatch):
    captured = {}

    def fake_urlopen(req, *a, **k):
        captured["method"] = req.get_method()
        captured["url"] = req.full_url
        return _resp({"html_url": "https://example/c"})

    monkeypatch.setattr(lx, "GITHUB_TOKEN", "t")
    monkeypatch.setattr(lx, "PR_NUMBER", "87")
    monkeypatch.setattr(lx, "REPO", "owner/repo")
    monkeypatch.setattr(lx, "find_existing_review_comment", lambda m: 4242)
    monkeypatch.setattr(lx.urllib.request, "urlopen", fake_urlopen)

    lx.post_github_comment("body", marker="## Loxia Review 🪶")

    assert captured["method"] == "PATCH"
    assert captured["url"].endswith("/issues/comments/4242")


def test_post_creates_when_no_existing(monkeypatch):
    captured = {}

    def fake_urlopen(req, *a, **k):
        captured["method"] = req.get_method()
        captured["url"] = req.full_url
        return _resp({"html_url": "https://example/c"})

    monkeypatch.setattr(lx, "GITHUB_TOKEN", "t")
    monkeypatch.setattr(lx, "PR_NUMBER", "87")
    monkeypatch.setattr(lx, "REPO", "owner/repo")
    monkeypatch.setattr(lx, "find_existing_review_comment", lambda m: None)
    monkeypatch.setattr(lx.urllib.request, "urlopen", fake_urlopen)

    lx.post_github_comment("body", marker="## Loxia Review 🪶")

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/issues/87/comments")


def test_post_without_marker_always_creates(monkeypatch):
    # Backward-compatible: no marker → no lookup, always POST.
    captured = {}

    def fake_urlopen(req, *a, **k):
        captured["method"] = req.get_method()
        return _resp({"html_url": "https://example/c"})

    monkeypatch.setattr(lx, "GITHUB_TOKEN", "t")
    monkeypatch.setattr(lx, "PR_NUMBER", "87")
    monkeypatch.setattr(lx, "REPO", "owner/repo")
    monkeypatch.setattr(lx.urllib.request, "urlopen", fake_urlopen)

    lx.post_github_comment("body")
    assert captured["method"] == "POST"


# ── Review-failure handling (no false-green) ────────────────────────────────


def test_call_claude_raises_on_http_error(monkeypatch):
    # A credit-exhausted / auth 400 must raise ClaudeReviewError, NOT return the
    # error text as if it were a review (the #174 false-green bug).
    monkeypatch.setattr(lx, "CLAUDE_OAUTH_TOKEN", "")
    monkeypatch.setattr(lx, "ANTHROPIC_API_KEY", "k")

    def raise_http(*a, **k):
        raise urllib.error.HTTPError(
            "u", 400, "Bad Request", {}, io.BytesIO(b'{"error":"credit too low"}')
        )

    monkeypatch.setattr(lx.urllib.request, "urlopen", raise_http)
    with pytest.raises(lx.ClaudeReviewError):
        lx.call_claude("prompt")


def test_call_claude_raises_on_empty_response(monkeypatch):
    monkeypatch.setattr(lx, "CLAUDE_OAUTH_TOKEN", "")
    monkeypatch.setattr(lx, "ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(
        lx.urllib.request, "urlopen",
        lambda *a, **k: _resp({"content": [{"text": "   "}]}),
    )
    with pytest.raises(lx.ClaudeReviewError):
        lx.call_claude("prompt")


def test_run_reviewer_returns_false_and_posts_failure_on_error(monkeypatch):
    # On a failed review: returns False (so main() exits non-zero) AND posts a
    # clearly-marked failure comment (so the PR shows the reviewer is broken).
    posted = {}
    monkeypatch.setattr(lx, "DRY_RUN", False)
    monkeypatch.setattr(lx, "HEAD_SHA", "abcdef123456")

    def boom(_prompt):
        raise lx.ClaudeReviewError("Claude API HTTP 400 — credit too low")

    monkeypatch.setattr(lx, "call_claude", boom)
    monkeypatch.setattr(
        lx, "post_github_comment",
        lambda body, marker=None: posted.update(body=body, marker=marker),
    )

    ok = lx.run_reviewer(lx.LOXIA, "diff", ["f.py"])
    assert ok is False
    assert "Review could not run" in posted["body"]
    assert "NOT an approval" in posted["body"]


def test_run_reviewer_returns_true_on_success(monkeypatch):
    monkeypatch.setattr(lx, "DRY_RUN", True)  # skip posting
    monkeypatch.setattr(lx, "call_claude", lambda _p: "## Loxia Review 🪶\nVerdict: APPROVE")
    assert lx.run_reviewer(lx.LOXIA, "diff", ["f.py"]) is True


def test_oauth_token_preferred_over_api_key(monkeypatch):
    # When both creds are present, the subscription OAuth token is used
    # (Bearer auth), not the metered x-api-key.
    captured = {}
    monkeypatch.setattr(lx, "CLAUDE_OAUTH_TOKEN", "oauth-tok")
    monkeypatch.setattr(lx, "ANTHROPIC_API_KEY", "metered-key")

    def fake_urlopen(req, *a, **k):
        captured["auth"] = req.get_header("Authorization")
        captured["xapikey"] = req.get_header("X-api-key")
        return _resp({"content": [{"text": "ok review"}]})

    monkeypatch.setattr(lx.urllib.request, "urlopen", fake_urlopen)
    lx.call_claude("prompt")
    assert captured["auth"] == "Bearer oauth-tok"
    assert captured["xapikey"] is None
