"""Tests for the PR dispatch pipeline hardening (PR-87 self-dogfood
findings): content-digest idempotency keys, Lanius verdict retry, and the
dispatcher-side fallback for unposted panel review comments."""

import asyncio

import swarm_dispatch
from github_gateway import SwarmTrigger
from skill_runner import SkillResult
from swarm_dispatch import (
    DispatchConfig,
    SwarmDispatcher,
    _token_for_repo,
    attribution_header,
    compose_fallback_comment,
    content_digest,
    lenses_missing_comments,
    parse_gate_verdict,
)


def _trigger(**overrides):
    base = dict(
        kind="pr_opened",
        repository="owner/repo",
        number=87,
        title="A pull request",
        body="Closes #80.",
        author="someone",
        html_url="https://github.com/owner/repo/pull/87",
        delivery_id="manual-test",
        action="opened",
    )
    base.update(overrides)
    return SwarmTrigger(**base)


class _StubNotifier:
    def __init__(self):
        self.sent = []

    def send(self, message, priority=None, handler=None):
        self.sent.append(message)


def _config():
    # No tokens: Neotoma stores and GitHub fallbacks short-circuit with a log.
    return DispatchConfig(neotoma_token="", github_token="")


# ── content_digest ──────────────────────────────────────────────────────────


def test_content_digest_is_stable_for_identical_payloads():
    entities = [{"entity_type": "harness_event", "occurred_at": "2026-06-12T10:00:00Z"}]
    assert content_digest(entities) == content_digest(list(entities))


def test_content_digest_changes_when_content_changes():
    a = [{"entity_type": "harness_event", "occurred_at": "2026-06-12T10:00:00Z"}]
    b = [{"entity_type": "harness_event", "occurred_at": "2026-06-12T10:00:01Z"}]
    assert content_digest(a) != content_digest(b)


# ── parse_gate_verdict ──────────────────────────────────────────────────────


def test_parse_gate_verdict_extracts_clear_and_blocked():
    assert parse_gate_verdict("…\nGATE_INHERITANCE: clear") == "clear"
    assert parse_gate_verdict("gate_inheritance: BLOCKED") == "blocked"


def test_parse_gate_verdict_none_when_absent():
    assert parse_gate_verdict("I could not verify the gates.") is None
    assert parse_gate_verdict("") is None
    assert parse_gate_verdict(None) is None


# ── lenses_missing_comments ─────────────────────────────────────────────────


def test_lenses_missing_comments_detects_unposted_reviews():
    bodies = ["review:pm\n\nLooks good.", "unrelated comment"]
    assert lenses_missing_comments(bodies, ["pm", "qa"]) == ["qa"]


def test_lenses_missing_comments_tolerates_leading_whitespace():
    bodies = ["  review:qa\nfine"]
    assert lenses_missing_comments(bodies, ["qa"]) == []


def test_lenses_missing_comments_all_missing_when_no_comments():
    assert lenses_missing_comments([], ["pm", "qa"]) == ["pm", "qa"]


# ── comment attribution ─────────────────────────────────────────────────────


def test_attribution_header_names_agent_and_role():
    header = attribution_header("corvus", "content lens panelist")
    assert "Corvus" in header
    assert "Ateles swarm" in header
    assert "content lens panelist" in header


def test_fallback_comment_carries_lens_prefix_and_attribution():
    body = compose_fallback_comment("qa", "phoenicurus", "Looks solid.")
    assert body.startswith("review:qa\n")
    assert "Phoenicurus" in body
    assert "Looks solid." in body
    assert "Apis dispatcher" in body
    # The prefix line must stay machine-detectable for dedup.
    assert lenses_missing_comments([body], ["qa"]) == []


# ── Lanius verdict retry ────────────────────────────────────────────────────


def test_lanius_missing_verdict_retried_once_then_blocked(monkeypatch):
    calls = []

    async def fake_run_skill(skill, prompt, **kwargs):
        calls.append((skill, prompt))
        if skill == "lanius" and len(calls) == 1:
            return SkillResult(skill, True, 0, "no verdict here", "")
        return SkillResult(skill, True, 0, "GATE_INHERITANCE: blocked", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())

    asyncio.run(dispatcher._handle_pr(_trigger()))

    lanius_calls = [c for c in calls if c[0] == "lanius"]
    assert len(lanius_calls) == 2
    assert "REMINDER" in lanius_calls[1][1]
    # Blocked verdict on retry → panel never spawned, operator notified.
    assert [c[0] for c in calls] == ["lanius", "lanius"]
    assert any("blocked by Lanius" in m for m in notifier.sent)


def test_lanius_verdict_on_first_try_not_retried(monkeypatch):
    calls = []

    async def fake_run_skill(skill, prompt, **kwargs):
        calls.append(skill)
        return SkillResult(skill, True, 0, "GATE_INHERITANCE: blocked", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    dispatcher = SwarmDispatcher(_StubNotifier(), _config())

    asyncio.run(dispatcher._handle_pr(_trigger()))

    assert calls == ["lanius"]


def test_lanius_pr_prompt_carries_legacy_issue_rule():
    # The PR-gate prompt must teach Lanius the legacy distinction so an issue
    # that predates the pipeline (no gate metadata) is cleared, not blocked.
    prompt = SwarmDispatcher._lanius_pr_prompt(_trigger(), parent=80)
    assert "LEGACY-ISSUE RULE" in prompt
    assert "never initialized" in prompt or "NO gate_status" in prompt
    assert "GATE_INHERITANCE: clear" in prompt
    assert "trigger_swarm_pr.py issue" in prompt


# ── _token_for_repo (#95) ────────────────────────────────────────────────────


def test_token_for_repo_neotoma_uses_neotoma_agent_pat(monkeypatch):
    """markmhendrickson/neotoma should use NEOTOMA_AGENT_PAT."""
    monkeypatch.setenv("NEOTOMA_AGENT_PAT", "neotoma-secret")
    monkeypatch.setenv("ATELES_AGENT_PAT", "ateles-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "shared-token")
    assert _token_for_repo("markmhendrickson/neotoma") == "neotoma-secret"


def test_token_for_repo_ateles_uses_ateles_agent_pat(monkeypatch):
    """markmhendrickson/ateles (and any non-neotoma repo) should use ATELES_AGENT_PAT."""
    monkeypatch.setenv("NEOTOMA_AGENT_PAT", "neotoma-secret")
    monkeypatch.setenv("ATELES_AGENT_PAT", "ateles-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "shared-token")
    assert _token_for_repo("markmhendrickson/ateles") == "ateles-secret"


def test_token_for_repo_neotoma_falls_back_to_github_token_when_pat_absent(monkeypatch):
    """When NEOTOMA_AGENT_PAT is unset, fall back to GITHUB_TOKEN."""
    monkeypatch.delenv("NEOTOMA_AGENT_PAT", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "shared-token")
    assert _token_for_repo("markmhendrickson/neotoma") == "shared-token"


def test_token_for_repo_ateles_falls_back_to_github_token_when_pat_absent(monkeypatch):
    """When ATELES_AGENT_PAT is unset, fall back to GITHUB_TOKEN."""
    monkeypatch.delenv("ATELES_AGENT_PAT", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "shared-token")
    assert _token_for_repo("markmhendrickson/ateles") == "shared-token"


def test_token_for_repo_returns_empty_string_when_no_tokens(monkeypatch):
    """When no tokens are set at all, return an empty string (no crash)."""
    monkeypatch.delenv("NEOTOMA_AGENT_PAT", raising=False)
    monkeypatch.delenv("ATELES_AGENT_PAT", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert _token_for_repo("markmhendrickson/neotoma") == ""
    assert _token_for_repo("markmhendrickson/ateles") == ""


def test_token_for_repo_only_suffix_matters(monkeypatch):
    """Any repo ending in /neotoma picks the neotoma PAT, not just the canonical slug."""
    monkeypatch.setenv("NEOTOMA_AGENT_PAT", "neotoma-secret")
    monkeypatch.setenv("ATELES_AGENT_PAT", "ateles-secret")
    # An org fork would still route via NEOTOMA_AGENT_PAT.
    assert _token_for_repo("someorg/neotoma") == "neotoma-secret"
    # A repo merely containing "neotoma" in the name but not ending in /neotoma
    # should use ATELES_AGENT_PAT.
    assert _token_for_repo("markmhendrickson/neotoma-fork") == "ateles-secret"
