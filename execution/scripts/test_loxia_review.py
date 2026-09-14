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


def test_api_fallback_raises_on_http_error(monkeypatch):
    # Metered-key fallback path: a credit-exhausted / auth 400 must raise
    # ClaudeReviewError, NOT return the error text as a review (#174 false-green).
    monkeypatch.setattr(lx, "CLAUDE_OAUTH_TOKEN", "")
    monkeypatch.setattr(lx, "ANTHROPIC_API_KEY", "k")

    def raise_http(*a, **k):
        raise urllib.error.HTTPError(
            "u", 400, "Bad Request", {}, io.BytesIO(b'{"error":"credit too low"}')
        )

    monkeypatch.setattr(lx.urllib.request, "urlopen", raise_http)
    with pytest.raises(lx.ClaudeReviewError):
        lx.call_claude("prompt")


def test_api_fallback_raises_on_empty_response(monkeypatch):
    monkeypatch.setattr(lx, "CLAUDE_OAUTH_TOKEN", "")
    monkeypatch.setattr(lx, "ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(
        lx.urllib.request,
        "urlopen",
        lambda *a, **k: _resp({"content": [{"text": "   "}]}),
    )
    with pytest.raises(lx.ClaudeReviewError):
        lx.call_claude("prompt")


def test_no_credential_raises(monkeypatch):
    monkeypatch.setattr(lx, "CLAUDE_OAUTH_TOKEN", "")
    monkeypatch.setattr(lx, "ANTHROPIC_API_KEY", "")
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
        lx,
        "post_github_comment",
        lambda body, marker=None: posted.update(body=body, marker=marker),
    )

    ok = lx.run_reviewer(lx.LOXIA, "diff", ["f.py"])
    assert ok is False
    assert "Review could not run" in posted["body"]
    assert "NOT an approval" in posted["body"]


def test_run_reviewer_returns_true_on_success(monkeypatch):
    monkeypatch.setattr(lx, "DRY_RUN", True)  # skip posting
    monkeypatch.setattr(
        lx, "call_claude", lambda _p: "## Loxia Review 🪶\nVerdict: APPROVE"
    )
    assert lx.run_reviewer(lx.LOXIA, "diff", ["f.py"]) is True


def test_oauth_token_routes_to_claude_cli(monkeypatch):
    # When the subscription OAuth token is present, review goes via
    # `claude --print` (subscription-native), NOT the raw metered API.
    monkeypatch.setattr(lx, "CLAUDE_OAUTH_TOKEN", "oauth-tok")
    monkeypatch.setattr(lx, "ANTHROPIC_API_KEY", "metered-key")
    monkeypatch.setattr(lx.shutil, "which", lambda _bin: "/usr/bin/claude")

    called = {}

    class _Proc:
        returncode = 0
        stdout = "## Loxia Review 🪶\nVerdict: APPROVE"
        stderr = ""

    def fake_run(cmd, **kw):
        called["cmd"] = cmd
        called["input"] = kw.get("input")
        return _Proc()

    monkeypatch.setattr(lx.subprocess, "run", fake_run)
    out = lx.call_claude("the prompt")
    assert "claude" in called["cmd"][0]
    assert "--print" in called["cmd"]
    assert called["input"] == "the prompt"
    assert "APPROVE" in out


def test_claude_cli_missing_raises(monkeypatch):
    # Told to use the subscription but the CLI isn't installed → fail visibly,
    # do NOT silently fall back to the metered key the operator opted out of.
    monkeypatch.setattr(lx, "CLAUDE_OAUTH_TOKEN", "oauth-tok")
    monkeypatch.setattr(lx.shutil, "which", lambda _bin: None)
    with pytest.raises(lx.ClaudeReviewError):
        lx.call_claude("prompt")


def test_claude_cli_nonzero_exit_raises(monkeypatch):
    monkeypatch.setattr(lx, "CLAUDE_OAUTH_TOKEN", "oauth-tok")
    monkeypatch.setattr(lx.shutil, "which", lambda _bin: "/usr/bin/claude")

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "auth expired"

    monkeypatch.setattr(lx.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(lx.ClaudeReviewError):
        lx.call_claude("prompt")


# ── Deliberate second-principal native review (#864) ───────────────────────


def test_foundation_path_predicate_is_bounded():
    assert lx.is_foundation_pr(
        [
            "docs/foundation/github.md",
            "execution/scripts/check_foundation_anchors.py",
            "execution/daemons/apis/test_foundation_binding.py",
        ]
    )
    assert not lx.is_foundation_pr([])
    assert not lx.is_foundation_pr(
        ["docs/foundation/github.md", "execution/daemons/apis/swarm_dispatch.py"]
    )


def test_native_approve_is_identity_and_head_bound_with_readback(monkeypatch):
    requests = []
    head = "a" * 40

    def fake_urlopen(req, *args, **kwargs):
        payload = json.loads(req.data) if req.data else None
        requests.append((req.get_method(), req.full_url, payload))
        if req.full_url.endswith("/user"):
            return _resp({"login": "ateles-agent"})
        if req.full_url.endswith("/pulls/87"):
            return _resp({"head": {"sha": head}})
        if req.full_url.endswith("/pulls/87/reviews") and req.get_method() == "POST":
            return _resp({"id": 91})
        if req.full_url.endswith("/pulls/87/reviews/91"):
            return _resp(
                {
                    "id": 91,
                    "user": {"login": "ateles-agent"},
                    "commit_id": head,
                    "state": "APPROVED",
                }
            )
        if req.full_url.endswith("/graphql"):
            return _resp(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "headRefOid": head,
                                "reviewDecision": "APPROVED",
                            }
                        }
                    }
                }
            )
        raise AssertionError(req.full_url)

    monkeypatch.setattr(lx, "GITHUB_TOKEN", "token")
    monkeypatch.setattr(lx, "NATIVE_REVIEW_TOKEN", "native-token")
    monkeypatch.setattr(lx, "PR_NUMBER", "87")
    monkeypatch.setattr(lx, "REPO", "owner/repo")
    monkeypatch.setattr(lx, "EXPECTED_REVIEWER_LOGIN", "ateles-agent")
    monkeypatch.setattr(lx.urllib.request, "urlopen", fake_urlopen)

    result = lx.post_native_review(
        "**Verdict**: APPROVE\n\nLooks good.", expected_head=head
    )

    assert result["review_id"] == 91
    review_posts = [
        r for r in requests if r[1].endswith("/pulls/87/reviews") and r[0] == "POST"
    ]
    assert review_posts == [
        (
            "POST",
            "https://api.github.com/repos/owner/repo/pulls/87/reviews",
            {
                "body": "**Verdict**: APPROVE\n\nLooks good.",
                "event": "APPROVE",
                "commit_id": head,
            },
        )
    ]


def test_wrong_reviewer_identity_refuses_before_review_post(monkeypatch):
    requests = []
    head = "b" * 40

    def fake_urlopen(req, *args, **kwargs):
        requests.append((req.get_method(), req.full_url))
        if req.full_url.endswith("/user"):
            return _resp({"login": "github-actions[bot]"})
        raise AssertionError(req.full_url)

    monkeypatch.setattr(lx, "GITHUB_TOKEN", "token")
    monkeypatch.setattr(lx, "NATIVE_REVIEW_TOKEN", "native-token")
    monkeypatch.setattr(lx, "PR_NUMBER", "87")
    monkeypatch.setattr(lx, "REPO", "owner/repo")
    monkeypatch.setattr(lx, "EXPECTED_REVIEWER_LOGIN", "ateles-agent")
    monkeypatch.setattr(lx.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(lx.NativeReviewError, match="wrong_reviewer_identity"):
        lx.post_native_review("**Verdict**: APPROVE", expected_head=head)
    assert not any(url.endswith("/pulls/87/reviews") for _, url in requests)


def test_head_movement_refuses_before_review_post(monkeypatch):
    requests = []

    def fake_urlopen(req, *args, **kwargs):
        requests.append((req.get_method(), req.full_url))
        if req.full_url.endswith("/user"):
            return _resp({"login": "ateles-agent"})
        if req.full_url.endswith("/pulls/87"):
            return _resp({"head": {"sha": "d" * 40}})
        raise AssertionError(req.full_url)

    monkeypatch.setattr(lx, "GITHUB_TOKEN", "token")
    monkeypatch.setattr(lx, "NATIVE_REVIEW_TOKEN", "native-token")
    monkeypatch.setattr(lx, "PR_NUMBER", "87")
    monkeypatch.setattr(lx, "REPO", "owner/repo")
    monkeypatch.setattr(lx, "EXPECTED_REVIEWER_LOGIN", "ateles-agent")
    monkeypatch.setattr(lx.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(lx.NativeReviewError, match="head_changed"):
        lx.post_native_review("**Verdict**: APPROVE", expected_head="c" * 40)
    assert not any(url.endswith("/pulls/87/reviews") for _, url in requests)


def test_native_review_readback_mismatch_fails_closed(monkeypatch):
    head = "e" * 40

    def fake_urlopen(req, *args, **kwargs):
        if req.full_url.endswith("/user"):
            return _resp({"login": "ateles-agent"})
        if req.full_url.endswith("/pulls/87"):
            return _resp({"head": {"sha": head}})
        if req.full_url.endswith("/pulls/87/reviews") and req.get_method() == "POST":
            return _resp({"id": 92})
        if req.full_url.endswith("/pulls/87/reviews/92"):
            return _resp({"id": 92, "user": {"login": "other-reviewer"}})
        raise AssertionError(req.full_url)

    monkeypatch.setattr(lx, "GITHUB_TOKEN", "token")
    monkeypatch.setattr(lx, "NATIVE_REVIEW_TOKEN", "native-token")
    monkeypatch.setattr(lx, "PR_NUMBER", "87")
    monkeypatch.setattr(lx, "REPO", "owner/repo")
    monkeypatch.setattr(lx, "EXPECTED_REVIEWER_LOGIN", "ateles-agent")
    monkeypatch.setattr(lx.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(lx.NativeReviewError, match="review_readback_failed"):
        lx.post_native_review("**Verdict**: COMMENT", expected_head=head)


def test_self_authored_pr_refuses_before_review_post(monkeypatch):
    requests = []
    head = "f" * 40

    def fake_urlopen(req, *args, **kwargs):
        requests.append((req.get_method(), req.full_url))
        if req.full_url.endswith("/user"):
            return _resp({"login": "review-bot"})
        if req.full_url.endswith("/pulls/87"):
            return _resp({"head": {"sha": head}, "user": {"login": "review-bot"}})
        raise AssertionError(req.full_url)

    monkeypatch.setattr(lx, "GITHUB_TOKEN", "token")
    monkeypatch.setattr(lx, "NATIVE_REVIEW_TOKEN", "native-token")
    monkeypatch.setattr(lx, "PR_NUMBER", "87")
    monkeypatch.setattr(lx, "REPO", "owner/repo")
    monkeypatch.setattr(lx, "EXPECTED_REVIEWER_LOGIN", "review-bot")
    monkeypatch.setattr(lx.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(lx.NativeReviewError, match="self_approval_rejected"):
        lx.post_native_review("**Verdict**: APPROVE", expected_head=head)
    assert not any(url.endswith("/pulls/87/reviews") for _, url in requests)


def test_run_reviewer_native_failure_is_sanitized_and_fails(monkeypatch):
    posted = []
    monkeypatch.setattr(lx, "DRY_RUN", False)
    monkeypatch.setattr(lx, "HEAD_SHA", "a" * 40)
    monkeypatch.setattr(
        lx, "call_claude", lambda _p: "**Verdict**: APPROVE\n\nLooks good."
    )
    monkeypatch.setattr(
        lx,
        "post_github_comment",
        lambda body, marker=None: posted.append((body, marker)),
    )
    monkeypatch.setattr(lx, "preflight_native_review", lambda _head: None)

    def fail_native(*args, **kwargs):
        raise lx.NativeReviewError(
            "wrong_reviewer_identity", "sensitive transport detail"
        )

    monkeypatch.setattr(lx, "post_native_review", fail_native)
    ok = lx.run_reviewer(
        lx.LOXIA,
        "diff",
        ["docs/foundation/github.md"],
        native_review=True,
    )

    assert ok is False
    assert "wrong_reviewer_identity" in posted[-1][0]
    assert "sensitive transport detail" not in posted[-1][0]
    assert "**What happened:**" in posted[-1][0]
    assert "**Mergeability:**" in posted[-1][0]
    assert "**Next action:**" in posted[-1][0]


def test_native_preflight_failure_skips_model_and_posts_sanitized_reason(monkeypatch):
    posted = []
    model_called = False

    def fail_preflight(_head):
        raise lx.NativeReviewError(
            "wrong_reviewer_identity", "sensitive transport detail"
        )

    def call_model(_prompt):
        nonlocal model_called
        model_called = True
        return "**Verdict**: APPROVE"

    monkeypatch.setattr(lx, "DRY_RUN", False)
    monkeypatch.setattr(lx, "HEAD_SHA", "a" * 40)
    monkeypatch.setattr(lx, "preflight_native_review", fail_preflight)
    monkeypatch.setattr(lx, "call_claude", call_model)
    monkeypatch.setattr(
        lx,
        "post_github_comment",
        lambda body, marker=None: posted.append((body, marker)),
    )

    assert not lx.run_reviewer(
        lx.LOXIA,
        "diff",
        ["docs/foundation/github.md"],
        native_review=True,
    )
    assert model_called is False
    assert "wrong_reviewer_identity" in posted[-1][0]
    assert "sensitive transport detail" not in posted[-1][0]


def test_native_failure_messages_use_three_part_contract():
    for reason_class, sentence in lx.NATIVE_REASON_MESSAGES.items():
        body = lx.format_native_review_failure(reason_class, head_sha="a" * 40)
        assert body.index("**What happened:**") < body.index("**Mergeability:**")
        assert body.index("**Mergeability:**") < body.index("**Next action:**")
        assert sentence in body
        assert f"`{reason_class}`" in body
        assert "NOT approved" in body


def test_native_review_activation_is_loxia_and_foundation_only(monkeypatch):
    monkeypatch.setattr(lx, "NATIVE_FOUNDATION_REVIEW", True)
    assert lx.should_post_native_review(lx.LOXIA, ["docs/foundation/github.md"])
    assert not lx.should_post_native_review(lx.MONEDULA, ["docs/foundation/github.md"])
    assert not lx.should_post_native_review(
        lx.LOXIA, ["execution/scripts/loxia_review.py"]
    )


def test_api_diff_and_files_avoid_executing_pr_head(monkeypatch):
    requests = []

    def fake_urlopen(req, *args, **kwargs):
        requests.append((req.full_url, req.headers.get("Accept")))
        if req.full_url.endswith("/pulls/87"):
            return _FakeResp(b"diff --git a/a.py b/a.py\n")
        if req.full_url.endswith("/pulls/87/files?per_page=100"):
            return _resp([{"filename": "docs/foundation/github.md"}])
        raise AssertionError(req.full_url)

    monkeypatch.setattr(lx, "USE_GITHUB_PR_API", True)
    monkeypatch.setattr(lx, "GITHUB_TOKEN", "token")
    monkeypatch.setattr(lx, "NATIVE_REVIEW_TOKEN", "native-token")
    monkeypatch.setattr(lx, "PR_NUMBER", "87")
    monkeypatch.setattr(lx, "REPO", "owner/repo")
    monkeypatch.setattr(lx.urllib.request, "urlopen", fake_urlopen)

    assert lx.get_pr_diff() == "diff --git a/a.py b/a.py\n"
    assert lx.get_changed_files() == ["docs/foundation/github.md"]
    assert requests[0][1] == "application/vnd.github.v3.diff"


def test_privileged_review_workflow_executes_trusted_base_code():
    workflow_dir = Path(__file__).resolve().parents[2] / ".github" / "workflows"
    workflow = (workflow_dir / "loxia-native-foundation-review.yml").read_text()
    assert "pull_request_target:" in workflow
    assert "ref: ${{ steps.pr.outputs.base_sha }}" in workflow
    assert 'LOXIA_USE_GITHUB_PR_API: "true"' in workflow
    assert "LOXIA_REVIEW_TOKEN: ${{ secrets.ATELES_AGENT_PAT }}" in workflow
    assert "vars.LOXIA_NATIVE_FOUNDATION_REVIEW == 'true'" in workflow
    ordinary = (workflow_dir / "loxia-pr-review.yml").read_text()
    assert "ATELES_AGENT_PAT" not in ordinary


def test_approve_readback_allows_an_independent_current_head_blocker(monkeypatch):
    head = "1" * 40
    pr_reads = 0

    def fake_json(method, url, payload=None, **kwargs):
        nonlocal pr_reads
        if url.endswith("/user"):
            return {"login": "review-bot"}
        if url.endswith("/pulls/87"):
            pr_reads += 1
            return {"head": {"sha": head}, "user": {"login": "author"}}
        if url.endswith("/pulls/87/reviews") and method == "POST":
            return {"id": 101}
        if url.endswith("/pulls/87/reviews/101"):
            return {
                "id": 101,
                "user": {"login": "review-bot"},
                "commit_id": head,
                "state": "APPROVED",
            }
        if url.endswith("/graphql"):
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "headRefOid": head,
                            "reviewDecision": "CHANGES_REQUESTED",
                            "reviews": {
                                "nodes": [
                                    {
                                        "author": {"login": "review-bot"},
                                        "state": "APPROVED",
                                        "commit": {"oid": head},
                                        "submittedAt": "2026-09-14T10:00:00Z",
                                    },
                                    {
                                        "author": {"login": "security-reviewer"},
                                        "state": "CHANGES_REQUESTED",
                                        "commit": {"oid": head},
                                        "submittedAt": "2026-09-14T10:01:00Z",
                                    },
                                ]
                            },
                        }
                    }
                }
            }
        raise AssertionError((method, url))

    monkeypatch.setattr(lx, "NATIVE_REVIEW_TOKEN", "native-token")
    monkeypatch.setattr(lx, "PR_NUMBER", "87")
    monkeypatch.setattr(lx, "REPO", "owner/repo")
    monkeypatch.setattr(lx, "EXPECTED_REVIEWER_LOGIN", "review-bot")
    monkeypatch.setattr(lx, "_github_json", fake_json)

    result = lx.post_native_review("**Verdict**: APPROVE", expected_head=head)
    assert result["review_decision"] == "CHANGES_REQUESTED"
    assert pr_reads == 2


def test_latest_review_per_actor_controls_current_head_blocker(monkeypatch):
    head = "2" * 40

    def fake_json(method, url, payload=None, **kwargs):
        if url.endswith("/user"):
            return {"login": "review-bot"}
        if url.endswith("/pulls/87"):
            return {"head": {"sha": head}, "user": {"login": "author"}}
        if url.endswith("/pulls/87/reviews") and method == "POST":
            return {"id": 102}
        if url.endswith("/pulls/87/reviews/102"):
            return {
                "id": 102,
                "user": {"login": "review-bot"},
                "commit_id": head,
                "state": "APPROVED",
            }
        if url.endswith("/graphql"):
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "headRefOid": head,
                            "reviewDecision": "CHANGES_REQUESTED",
                            "reviews": {
                                "nodes": [
                                    {
                                        "author": {"login": "security-reviewer"},
                                        "state": "APPROVED",
                                        "commit": {"oid": head},
                                        "submittedAt": "2026-09-14T10:02:00Z",
                                    },
                                    {
                                        "author": {"login": "security-reviewer"},
                                        "state": "CHANGES_REQUESTED",
                                        "commit": {"oid": head},
                                        "submittedAt": "2026-09-14T10:01:00Z",
                                    },
                                ]
                            },
                        }
                    }
                }
            }
        raise AssertionError((method, url))

    monkeypatch.setattr(lx, "NATIVE_REVIEW_TOKEN", "native-token")
    monkeypatch.setattr(lx, "PR_NUMBER", "87")
    monkeypatch.setattr(lx, "REPO", "owner/repo")
    monkeypatch.setattr(lx, "EXPECTED_REVIEWER_LOGIN", "review-bot")
    monkeypatch.setattr(lx, "_github_json", fake_json)

    with pytest.raises(lx.NativeReviewError, match="review_decision_not_approved"):
        lx.post_native_review("**Verdict**: APPROVE", expected_head=head)


def test_native_only_nonfoundation_skips_before_claude_credential_check(monkeypatch):
    monkeypatch.setattr(lx, "PR_NUMBER", "87")
    monkeypatch.setattr(lx, "NATIVE_REVIEW_ONLY", True)
    monkeypatch.setattr(lx, "CLAUDE_OAUTH_TOKEN", "")
    monkeypatch.setattr(lx, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(lx, "get_changed_files", lambda: ["README.md"])
    monkeypatch.setattr(
        lx,
        "get_pr_diff",
        lambda: pytest.fail("non-foundation PR must not fetch the diff"),
    )

    lx.main()


def test_review_post_422_is_not_assumed_to_be_self_approval(monkeypatch):
    def raise_http(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "u", 422, "Unprocessable Entity", {}, io.BytesIO(b"{}")
        )

    monkeypatch.setattr(lx.urllib.request, "urlopen", raise_http)
    with pytest.raises(lx.NativeReviewError) as excinfo:
        lx._github_json(
            "POST",
            "https://api.github.com/repos/owner/repo/pulls/87/reviews",
            {"event": "APPROVE"},
            reason_class="review_post_failed",
            token="native-token",
        )
    assert excinfo.value.reason_class == "review_post_failed"
