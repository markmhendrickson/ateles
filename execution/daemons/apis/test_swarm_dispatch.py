"""Tests for the PR dispatch pipeline hardening (PR-87 self-dogfood
findings): content-digest idempotency keys, Lanius verdict retry, and the
dispatcher-side fallback for unposted panel review comments.

Also covers the checkbox definition-of-done changes:
  A — _expectation_prompt mandates GitHub task-list syntax
  B — _panelist_prompt includes a check-off instruction when parent + expectation present
"""

import asyncio

import swarm_dispatch
from github_gateway import SwarmTrigger
from review_panel import Lens
from skill_runner import SkillResult
from swarm_dispatch import (
    EXPECTATION_MARKER,
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


# ── Change A: _expectation_prompt mandates GitHub task-list syntax ───────────

def _sample_lens(forward_looking: bool = False) -> Lens:
    return Lens(
        agent="phoenicurus",
        lens="qa",
        gate="qa",
        checks="quality, test coverage, regression risk",
        forward_looking=forward_looking,
    )


def test_expectation_prompt_contains_checkbox_syntax():
    """_expectation_prompt must include the `- [ ]` task-list placeholder."""
    t = _trigger()
    prompt = SwarmDispatcher._expectation_prompt(t, _sample_lens())
    assert "- [ ]" in prompt


def test_expectation_prompt_instructs_github_task_list():
    """The prompt must explicitly name 'GitHub task-list' (or equivalent) checkbox syntax."""
    t = _trigger()
    prompt = SwarmDispatcher._expectation_prompt(t, _sample_lens())
    assert "GitHub task-list" in prompt or "task-list checkbox" in prompt


def test_expectation_prompt_header_unchanged():
    """The `review_expectation (<lens>)` header line must survive the change."""
    t = _trigger()
    prompt = SwarmDispatcher._expectation_prompt(t, _sample_lens())
    assert f"**{EXPECTATION_MARKER} (qa)**" in prompt


def test_expectation_prompt_no_plain_bullet_placeholder():
    """Plain `- <...>` placeholder without checkbox must no longer be present."""
    t = _trigger()
    prompt = SwarmDispatcher._expectation_prompt(t, _sample_lens())
    # The old placeholder was literally "- <3 to 6 tight ...>"
    assert "- <3 to 6 tight" not in prompt


# ── Change B: _panelist_prompt check-off instruction ────────────────────────

def test_panelist_prompt_checkoff_included_when_parent_and_expectation():
    """When parent issue and expectation are both present, check-off block appears."""
    t = _trigger()
    expectation = (
        f"**{EXPECTATION_MARKER} (qa)** — what phoenicurus will verify:\n"
        "- [ ] Tests exist for the new code path\n"
        "- [ ] No regressions in existing tests\n"
    )
    prompt = SwarmDispatcher._panelist_prompt(t, _sample_lens(), expectation, parent=80)
    assert "edit" in prompt
    assert "- [x]" in prompt
    assert "#80" in prompt


def test_panelist_prompt_checkoff_references_correct_parent():
    """Check-off instruction must reference the actual parent issue number."""
    t = _trigger()
    expectation = "- [ ] Some check\n"
    prompt = SwarmDispatcher._panelist_prompt(t, _sample_lens(), expectation, parent=42)
    assert "#42" in prompt


def test_panelist_prompt_no_checkoff_when_parent_none():
    """Without a parent issue, the check-off block must NOT appear."""
    t = _trigger()
    expectation = "- [ ] Some check\n"
    prompt = SwarmDispatcher._panelist_prompt(t, _sample_lens(), expectation, parent=None)
    assert "- [x]" not in prompt
    assert "edit the existing" not in prompt


def test_panelist_prompt_no_checkoff_when_expectation_empty():
    """Without a pre-registered expectation, the check-off block must NOT appear."""
    t = _trigger()
    prompt = SwarmDispatcher._panelist_prompt(t, _sample_lens(), expectation="", parent=80)
    assert "- [x]" not in prompt
    assert "edit the existing" not in prompt


def test_panelist_prompt_no_checkoff_when_both_missing():
    """No parent AND no expectation: definitely no check-off."""
    t = _trigger()
    prompt = SwarmDispatcher._panelist_prompt(t, _sample_lens(), expectation="", parent=None)
    assert "- [x]" not in prompt


def test_panelist_prompt_checkoff_scoped_to_own_lens():
    """Check-off instruction must name the lens so the agent edits only its own comment."""
    t = _trigger()
    expectation = "- [ ] Some check\n"
    prompt = SwarmDispatcher._panelist_prompt(t, _sample_lens(), expectation, parent=80)
    # The instruction must reference the lens-specific comment marker so the
    # agent doesn't accidentally edit another lens's expectation comment.
    assert f"{EXPECTATION_MARKER} (qa)" in prompt


def test_panelist_prompt_checkoff_forward_looking_lens_also_gets_checkoff():
    """Forward-looking lenses pre-register too, so they should also check off."""
    t = _trigger()
    expectation = "- [ ] Forward-looking check\n"
    prompt = SwarmDispatcher._panelist_prompt(
        t, _sample_lens(forward_looking=True), expectation, parent=80
    )
    assert "- [x]" in prompt
    assert "#80" in prompt


def test_panelist_prompt_review_comment_instruction_still_present():
    """The original 'Post your review as a PR comment' instruction must remain."""
    t = _trigger()
    expectation = "- [ ] Some check\n"
    prompt = SwarmDispatcher._panelist_prompt(t, _sample_lens(), expectation, parent=80)
    assert "Post your review as a PR comment" in prompt
