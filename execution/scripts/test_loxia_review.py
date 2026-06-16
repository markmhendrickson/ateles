"""Tests for the review-comment upsert logic (in-place update instead of one
comment per push, per reviewer). Loads loxia_review as a module and stubs
urlopen so no network or env config is required."""

import io
import json
import sys
from pathlib import Path

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
