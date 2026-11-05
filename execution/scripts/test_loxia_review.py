"""Tests for the review-comment upsert logic (in-place update instead of one
comment per push, per reviewer). Loads loxia_review as a module and stubs
urlopen so no network or env config is required."""

import io
import json
import sys
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


def test_run_reviewer_returns_false_and_posts_failure_on_error(monkeypatch):
    # On a failed review: returns False (so main() exits non-zero) AND posts a
    # clearly-marked failure comment (so the PR shows the reviewer is broken).
    posted = {}
    monkeypatch.setattr(lx, "DRY_RUN", False)
    monkeypatch.setattr(lx, "HEAD_SHA", "abcdef123456")

    def boom(_prompt, **kwargs):
        raise lx.ReviewError("Claude API HTTP 400 — credit too low")

    monkeypatch.setattr(lx, "call_review", boom)
    monkeypatch.setattr(
        lx, "post_github_comment",
        lambda body, marker=None: posted.update(body=body, marker=marker),
    )

    outcome = lx.run_reviewer(lx.LOXIA, "diff", ["f.py"])
    assert outcome is lx.ReviewOutcome.FAILED
    assert "Review could not run" in posted["body"]
    assert "NOT an approval" in posted["body"]


def test_run_reviewer_returns_true_on_success(monkeypatch):
    monkeypatch.setattr(lx, "DRY_RUN", True)  # skip posting
    monkeypatch.setattr(lx, "call_review", lambda _p, **kw: "## Loxia Review 🪶\nVerdict: APPROVE")
    assert lx.run_reviewer(lx.LOXIA, "diff", ["f.py"]) is lx.ReviewOutcome.REVIEWED


def test_call_review_preserves_requested_role(monkeypatch):
    import types
    calls = []
    async def run(**kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(ok=True, stdout="Verdict: APPROVE", provider="codex", attempted_providers=("claude", "codex"))
    monkeypatch.setitem(sys.modules, "skill_runner", types.SimpleNamespace(run_review_prompt=run))
    assert lx.call_review("role-specific input", role="monedula") == "Verdict: APPROVE"
    assert calls == [{"role":"monedula", "prompt":"role-specific input"}]


def test_capacity_failure_does_not_publish_review(monkeypatch):
    import types
    async def run(**kwargs):
        return types.SimpleNamespace(ok=False, stdout="", error="no qualified capacity", provider="", attempted_providers=())
    monkeypatch.setitem(sys.modules, "skill_runner", types.SimpleNamespace(run_review_prompt=run))
    with pytest.raises(lx.ReviewError, match="no qualified capacity"):
        lx.call_review("input", role="loxia")


# ── Usage-gate refusal: reported, not failed (never an approval) ────────────


def _skill_runner_returning(monkeypatch, **fields):
    import types

    base = dict(ok=False, stdout="", error="", provider="", attempted_providers=(), cooled_until="")
    base.update(fields)

    async def run(**kwargs):
        return types.SimpleNamespace(**base)

    monkeypatch.setitem(sys.modules, "skill_runner", types.SimpleNamespace(run_review_prompt=run))


def _run_main(monkeypatch, capsys):
    monkeypatch.setattr(lx, "PR_NUMBER", "87")
    monkeypatch.setattr(lx, "get_pr_diff", lambda: "diff")
    monkeypatch.setattr(lx, "get_changed_files", lambda: ["f.py"])
    monkeypatch.setattr(lx, "select_reviewers", lambda _f: [lx.LOXIA])
    code = 0
    try:
        lx.main()
    except SystemExit as exc:
        code = exc.code
    return code, capsys.readouterr()


def test_usage_gate_refusal_posts_comment_and_does_not_fail(monkeypatch, capsys):
    # The dispatcher's usage-gate refusal: nothing ran, cooled_until set.
    _skill_runner_returning(
        monkeypatch,
        error="frontier dispatch refused by the usage gate (claude: stale); retry after 22:00",
        cooled_until="2026-11-05T22:00:00+01:00",
    )
    posted = []
    filed = []
    monkeypatch.setattr(lx, "DRY_RUN", False)
    monkeypatch.setattr(lx, "HEAD_SHA", "abcdef123456")
    monkeypatch.setattr(lx, "post_github_comment", lambda body, marker=None: posted.append(body))
    monkeypatch.setattr(lx, "file_neotoma_issue", lambda **kw: filed.append(kw))

    assert lx.run_reviewer(lx.LOXIA, "diff", ["f.py"]) is lx.ReviewOutcome.NOT_RUN_CAPACITY
    out = capsys.readouterr().out
    assert "::warning " in out and "not an approval" in out

    posted.clear()
    code, captured = _run_main(monkeypatch, capsys)
    assert code == 0  # the job does not fail on the refusal
    body = posted[0]
    assert "Review could not run" in body
    assert "usage gate" in body  # the reason is shown
    assert "NOT an approval" in body
    assert "The check fails" not in body
    assert "Verdict" not in body  # no verdict of any kind is emitted
    assert filed == []
    assert "::warning " in captured.out


def test_other_review_error_still_fails(monkeypatch, capsys):
    # A provider failure after dispatch carries no cooled_until: still fails.
    _skill_runner_returning(monkeypatch, error="review timed out after 180s", provider="claude")
    posted = []
    monkeypatch.setattr(lx, "DRY_RUN", False)
    monkeypatch.setattr(lx, "post_github_comment", lambda body, marker=None: posted.append(body))
    code, captured = _run_main(monkeypatch, capsys)
    assert code == 1
    assert "The check fails" in posted[0]
    assert "::warning " not in captured.out


def test_capacity_refusal_requires_typed_field_not_text(monkeypatch):
    # Text that looks like a refusal but without cooled_until is a plain error.
    _skill_runner_returning(monkeypatch, error="frontier dispatch refused by the usage gate")
    with pytest.raises(lx.ReviewError) as info:
        lx.call_review("input", role="loxia")
    assert not isinstance(info.value, lx.ReviewCapacityRefused)


def test_request_changes_outcome_unchanged(monkeypatch, capsys):
    # A completed REQUEST_CHANGES review takes the reviewed path exactly as
    # before: verdict posted, Neotoma issue filed, no refusal warning.
    _skill_runner_returning(
        monkeypatch, ok=True, stdout="## Loxia Review 🪶\nVerdict: REQUEST_CHANGES", provider="codex",
    )
    posted, filed = [], []
    monkeypatch.setattr(lx, "DRY_RUN", False)
    monkeypatch.setattr(lx, "post_github_comment", lambda body, marker=None: posted.append(body))
    monkeypatch.setattr(lx, "file_neotoma_issue", lambda **kw: filed.append(kw))
    assert lx.run_reviewer(lx.LOXIA, "diff", ["f.py"]) is lx.ReviewOutcome.REVIEWED
    assert "REQUEST_CHANGES" in posted[0]
    assert len(filed) == 1
    assert "::warning " not in capsys.readouterr().out


# ── cooled_until alone is not proof nothing ran ─────────────────────────────


def test_attempted_provider_failure_with_cooling_still_fails(monkeypatch, capsys):
    # A provider was launched and failed, and the pool was then cooled, so
    # cooled_until is set. Something ran: this must fail the job, not warn.
    _skill_runner_returning(
        monkeypatch,
        error="all eligible subscription-backed harness providers were exhausted after attempts: claude",
        provider="claude",
        attempted_providers=("claude",),
        cooled_until="2026-11-05T22:00:00+01:00",
    )
    posted = []
    monkeypatch.setattr(lx, "DRY_RUN", False)
    monkeypatch.setattr(lx, "post_github_comment", lambda body, marker=None: posted.append(body))
    code, captured = _run_main(monkeypatch, capsys)
    assert code == 1
    assert "Review could not run" in posted[0]
    assert "The check fails" in posted[0]
    assert "::warning " not in captured.out


def test_main_capacity_refusal_exit_zero_warning_comment_no_approval(monkeypatch, capsys):
    _skill_runner_returning(
        monkeypatch, error="usage gate refused", cooled_until="2026-11-05T22:00:00+01:00",
    )
    posted, filed = [], []
    monkeypatch.setattr(lx, "DRY_RUN", False)
    monkeypatch.setattr(lx, "post_github_comment", lambda body, marker=None: posted.append(body))
    monkeypatch.setattr(lx, "file_neotoma_issue", lambda **kw: filed.append(kw))
    code, captured = _run_main(monkeypatch, capsys)
    assert code == 0
    assert "::warning " in captured.out
    assert len(posted) == 1
    body = posted[0]
    assert "NOT an approval" in body
    assert "opportunity, not proof of recovery" in body
    assert "credentials" in body and "waiting alone will not fix" in body
    assert "APPROVE" not in body
    assert filed == []


def test_main_completed_request_changes_posts_review_and_exits_zero(monkeypatch, capsys):
    # Pins current behaviour: a completed REQUEST_CHANGES review is posted and
    # filed but does NOT fail the job. Only a review that could not run does.
    _skill_runner_returning(
        monkeypatch, ok=True, stdout="## Loxia Review 🪶\nVerdict: REQUEST_CHANGES",
        provider="codex", attempted_providers=("codex",),
    )
    posted, filed = [], []
    monkeypatch.setattr(lx, "DRY_RUN", False)
    monkeypatch.setattr(lx, "post_github_comment", lambda body, marker=None: posted.append(body))
    monkeypatch.setattr(lx, "file_neotoma_issue", lambda **kw: filed.append(kw))
    code, captured = _run_main(monkeypatch, capsys)
    assert code == 0
    assert "REQUEST_CHANGES" in posted[0]
    assert len(filed) == 1
    assert "::warning " not in captured.out


# ── The real producer: skill_runner's attempt path, provider launches stubbed ─


def _daemon_modules():
    daemon_dir = str(Path(lx.__file__).resolve().parents[1] / "daemons" / "apis")
    if daemon_dir not in sys.path:
        sys.path.insert(0, daemon_dir)
    import harness_router
    import skill_runner

    return skill_runner, harness_router


def _producer_result(monkeypatch, *, candidates, gates, attempt):
    """Drive the real skill_runner._run_provider_attempts, stubbing only the
    selection inputs and the provider launch, and return its SkillResult."""
    import asyncio
    import time as _time

    sr, _hr = _daemon_modules()
    cooled = {}
    monkeypatch.setattr(sr, "_refresh_usage_snapshot", lambda b: None)
    monkeypatch.setattr(sr, "provider_candidates", lambda *a, **k: list(candidates))
    monkeypatch.setattr(sr, "usage_gate_refusals_all", lambda *a, **k: gates)
    monkeypatch.setattr(sr, "provider_exclusion_reasons", lambda *a, **k: {})
    monkeypatch.setattr(sr, "cooled_until_all", lambda *a, **k: dict(cooled))
    monkeypatch.setattr(sr, "cooling_providers", lambda *a, **k: set(cooled))
    monkeypatch.setattr(sr, "notify_dispatch_failure", lambda *a, **k: None)

    def fake_cool(provider, *a, **k):
        cooled[provider] = {"until": _time.time() + 3600, "reason": "session limit"}

    monkeypatch.setattr(sr, "cool_down", fake_cool)
    monkeypatch.setattr(sr, "_cool_after_capacity_failure", lambda p, r: fake_cool(p))
    return asyncio.run(
        sr._run_provider_attempts(
            "loxia", attempt, binaries={"claude": "/bin/claude"}, role="loxia", retry_safe=True,
        )
    )


def _through_call_review(monkeypatch, result):
    import types

    async def run(**kwargs):
        return result

    monkeypatch.setitem(sys.modules, "skill_runner", types.SimpleNamespace(run_review_prompt=run))
    return lx.call_review("input", role="loxia")


def test_real_producer_usage_gate_refusal_has_no_attempts_and_is_capacity_refusal(monkeypatch):
    import time as _time

    _sr, hr = _daemon_modules()

    async def never(selected, **kw):  # pragma: no cover
        raise AssertionError("provider launched despite a pre-launch refusal")

    # Unknown capacity (stale reading) is a usage-gate refusal too.
    gate = hr.UsageGate(
        provider="claude", allowed=False, code="stale",
        message="usage reading unknown (stale); capacity cannot be established",
        returns_at=_time.time() + 3600,
    )
    result = _producer_result(monkeypatch, candidates=[], gates={"claude": gate}, attempt=never)
    assert not result.ok
    assert result.cooled_until
    assert tuple(result.attempted_providers) == ()
    with pytest.raises(lx.ReviewCapacityRefused):
        _through_call_review(monkeypatch, result)


def test_real_producer_launched_capacity_failure_has_attempts_and_is_plain_failure(monkeypatch):
    sr, _hr = _daemon_modules()

    async def attempt(selected, **kw):
        return sr.SkillResult(
            "loxia", False, 1, "", "You've hit your usage limit. Try again later.",
            error="usage limit reached", provider=selected,
        )

    result = _producer_result(monkeypatch, candidates=["claude"], gates=None, attempt=attempt)
    assert not result.ok
    assert result.cooled_until, "the producer sets cooled_until after cooling the pool"
    assert tuple(result.attempted_providers) == ("claude",)
    with pytest.raises(lx.ReviewError) as info:
        _through_call_review(monkeypatch, result)
    assert not isinstance(info.value, lx.ReviewCapacityRefused)
