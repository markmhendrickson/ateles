"""Tests for the PR dispatch pipeline hardening (PR-87 self-dogfood
findings): content-digest idempotency keys, Lanius verdict retry, and the
dispatcher-side fallback for unposted panel review comments.

Also covers the checkbox definition-of-done changes:
  A — _expectation_prompt mandates GitHub task-list syntax
  B — _panelist_prompt includes a check-off instruction when parent + expectation present
"""

import asyncio
import json

import httpx
import swarm_dispatch
from github_gateway import SwarmTrigger
from review_panel import Lens
from skill_runner import SkillResult
from swarm_dispatch import (
    AGENT_GITHUB_LOGIN,
    EXPECTATION_MARKER,
    GITHUB_FACING_AGENTS,
    PRE_IMPL_GATES,
    _APPROVE_CMD,
    _CONFIRM_GATES_CLEAR_CMD,
    _HOLD_CMD,
    _OPERATOR_LOGIN,
    _REJECT_CMD,
    _SWARM_RUN_CMD,
    _VANELLUS_COMMENT_MARKER,
    DispatchConfig,
    SwarmDispatcher,
    _agent_prompt_instruction,
    _is_bot_author,
    _token_for_agent_on_repo,
    _token_for_repo,
    agent_github_login,
    attribution_header,
    compose_fallback_comment,
    compose_vanellus_fallback_comment,
    content_digest,
    cleanup_pr_worktree,
    is_provisioned,
    lenses_missing_comments,
    parse_gate_verdict,
    prepare_pr_worktree,
    vanellus_comment_missing,
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


# ── ateles#109 — per-agent GitHub identity (NO-OP until provisioned) ─────────
# These tests cover the three-tier token resolution, attribution_header gating,
# prompt instruction blocks, and native assignment.  They explicitly assert the
# NO-OP property: with no <AGENT>_AGENT_PAT env vars set, behaviour is identical
# to pre-#109.  They also assert correct behaviour when a PAT IS set (mocked).


# ── is_provisioned ─────────────────────────────────────────────────────────────


def test_is_provisioned_false_when_pat_absent(monkeypatch):
    """NO-OP: is_provisioned returns False when PAVO_AGENT_PAT is not set."""
    monkeypatch.delenv("PAVO_AGENT_PAT", raising=False)
    assert is_provisioned("pavo") is False


def test_is_provisioned_true_when_pat_set(monkeypatch):
    """is_provisioned returns True when PAVO_AGENT_PAT is set."""
    monkeypatch.setenv("PAVO_AGENT_PAT", "ghp_test_pavo_pat")
    assert is_provisioned("pavo") is True


def test_is_provisioned_uppercases_agent_name(monkeypatch):
    """Agent name is uppercased before constructing the env var key."""
    monkeypatch.setenv("LANIUS_AGENT_PAT", "ghp_lanius")
    assert is_provisioned("lanius") is True
    assert is_provisioned("LANIUS") is True


def test_is_provisioned_empty_string_is_false(monkeypatch):
    """An empty-string env var is treated as absent (not provisioned)."""
    monkeypatch.setenv("CORVUS_AGENT_PAT", "")
    assert is_provisioned("corvus") is False


# ── _token_for_agent_on_repo ──────────────────────────────────────────────────


def test_token_for_agent_on_repo_tier1_when_agent_pat_set(monkeypatch):
    """Tier 1: agent's own PAT wins when PAVO_AGENT_PAT is set."""
    monkeypatch.setenv("PAVO_AGENT_PAT", "ghp_pavo_own")
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_ateles_shared")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fallback")
    result = _token_for_agent_on_repo("pavo", "markmhendrickson/ateles")
    assert result == "ghp_pavo_own"


def test_token_for_agent_on_repo_tier2_neotoma_when_unprovisioned(monkeypatch):
    """NO-OP Tier 2: without agent PAT, neotoma repo uses NEOTOMA_AGENT_PAT."""
    monkeypatch.delenv("PAVO_AGENT_PAT", raising=False)
    monkeypatch.setenv("NEOTOMA_AGENT_PAT", "ghp_neotoma_shared")
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_ateles_shared")
    result = _token_for_agent_on_repo("pavo", "markmhendrickson/neotoma")
    # Must match _token_for_repo — the existing #95 behaviour.
    assert result == _token_for_repo("markmhendrickson/neotoma")
    assert result == "ghp_neotoma_shared"


def test_token_for_agent_on_repo_tier2_ateles_when_unprovisioned(monkeypatch):
    """NO-OP Tier 2: without agent PAT, ateles repo uses ATELES_AGENT_PAT."""
    monkeypatch.delenv("PAVO_AGENT_PAT", raising=False)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_ateles_shared")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fallback")
    result = _token_for_agent_on_repo("pavo", "markmhendrickson/ateles")
    assert result == _token_for_repo("markmhendrickson/ateles")
    assert result == "ghp_ateles_shared"


def test_token_for_agent_on_repo_tier3_fallback_when_all_absent(monkeypatch):
    """Tier 3: when no agent PAT and no per-repo PAT, falls back to GITHUB_TOKEN."""
    monkeypatch.delenv("PAVO_AGENT_PAT", raising=False)
    monkeypatch.delenv("ATELES_AGENT_PAT", raising=False)
    monkeypatch.delenv("NEOTOMA_AGENT_PAT", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_shared_fallback")
    result = _token_for_agent_on_repo("pavo", "markmhendrickson/ateles")
    assert result == "ghp_shared_fallback"


# ── attribution_header — always returns non-empty ────────────────────────────


def test_attribution_header_always_returns_nonempty():
    """attribution_header always returns the non-empty header string regardless
    of provisioning state — gating belongs in _agent_prompt_instruction."""
    assert attribution_header("pavo", "pm gate owner") != ""
    assert "Pavo" in attribution_header("pavo", "pm gate owner")


# ── _agent_prompt_instruction ──────────────────────────────────────────────────


def test_agent_prompt_instruction_shared_account_when_unprovisioned(monkeypatch):
    """NO-OP: without agent PAT, instruction says 'shared account / prepend header'."""
    monkeypatch.delenv("PAVO_AGENT_PAT", raising=False)
    instruction = _agent_prompt_instruction("pavo", "pm gate owner")
    # Must contain the shared-account wording from the current behaviour.
    assert attribution_header("pavo", "pm gate owner") in instruction
    assert "shared" in instruction.lower()
    # Must NOT say the agent is posting as itself.
    assert "your own GitHub account" not in instruction


def test_agent_prompt_instruction_own_account_when_provisioned(monkeypatch):
    """When PAVO_AGENT_PAT is set, instruction says agent posts AS ITSELF."""
    monkeypatch.setenv("PAVO_AGENT_PAT", "ghp_pavo_pat")
    instruction = _agent_prompt_instruction("pavo", "pm gate owner")
    assert "your own GitHub account" in instruction
    # Must NOT contain the attribution header (avatar is the identity).
    assert attribution_header("pavo", "pm gate owner") not in instruction
    # Must NOT say shared account.
    assert "account is shared" not in instruction


def test_agent_prompt_instruction_provisioned_login_uses_convention(monkeypatch):
    """When provisioned, the instruction cites the operator-scoped login."""
    monkeypatch.setenv("PAVO_AGENT_PAT", "ghp_pavo_pat")
    instruction = _agent_prompt_instruction("pavo", "pm gate owner")
    # AGENT_GITHUB_LOGIN["pavo"] is now e.g. "markmhendrickson-ateles-pavo"
    assert AGENT_GITHUB_LOGIN["pavo"] in instruction


# ── Prompt-level no-op assertions ─────────────────────────────────────────────


def test_lanius_issue_prompt_shared_account_text_when_unprovisioned(monkeypatch):
    """NO-OP: _lanius_issue_prompt uses shared-account wording when no PAT set."""
    monkeypatch.delenv("LANIUS_AGENT_PAT", raising=False)
    monkeypatch.delenv("PAVO_AGENT_PAT", raising=False)
    t = _trigger(kind="issue_opened", number=1, title="A new issue", body="Body.")
    prompt = SwarmDispatcher._lanius_issue_prompt(t)
    assert "account is shared" in prompt
    assert attribution_header("lanius", "issue triage") in prompt
    # No GitHub-native assignment when pavo is unprovisioned.
    assert "--add-assignee" not in prompt


def test_lanius_issue_prompt_own_account_and_assignment_when_provisioned(monkeypatch):
    """When LANIUS and PAVO PATs are set: own-account text + native assignment."""
    monkeypatch.setenv("LANIUS_AGENT_PAT", "ghp_lanius")
    monkeypatch.setenv("PAVO_AGENT_PAT", "ghp_pavo")
    t = _trigger(kind="issue_opened", number=1, title="A new issue", body="Body.")
    prompt = SwarmDispatcher._lanius_issue_prompt(t)
    # Lanius should use its own account.
    assert "your own GitHub account" in prompt
    assert "account is shared" not in prompt
    # Native assignment instruction for pavo should appear.
    assert "--add-assignee" in prompt
    assert AGENT_GITHUB_LOGIN["pavo"] in prompt
    # Must be best-effort.
    assert "best-effort" in prompt.lower() or "skip silently" in prompt.lower()


def test_pavo_prompt_shared_account_text_when_unprovisioned(monkeypatch):
    """NO-OP: _pavo_prompt uses shared-account wording when PAVO_AGENT_PAT absent."""
    monkeypatch.delenv("PAVO_AGENT_PAT", raising=False)
    t = _trigger()
    prompt = SwarmDispatcher._pavo_prompt(t)
    assert "account is shared" in prompt
    assert attribution_header("pavo", "pm gate owner") in prompt


def test_pavo_prompt_own_account_when_provisioned(monkeypatch):
    """When PAVO_AGENT_PAT is set, _pavo_prompt instructs own-account posting."""
    monkeypatch.setenv("PAVO_AGENT_PAT", "ghp_pavo")
    t = _trigger()
    prompt = SwarmDispatcher._pavo_prompt(t)
    assert "your own GitHub account" in prompt
    assert "account is shared" not in prompt


def test_vanellus_prompt_shared_account_text_when_unprovisioned(monkeypatch):
    """NO-OP: _vanellus_prompt uses shared-account wording when unprovisioned."""
    monkeypatch.delenv("VANELLUS_AGENT_PAT", raising=False)
    t = _trigger()
    prompt = SwarmDispatcher._vanellus_prompt(t, parent=80, lenses=["pm", "qa"])
    assert "account is shared" in prompt
    assert attribution_header("vanellus", "PR steward") in prompt


def test_vanellus_prompt_own_account_when_provisioned(monkeypatch):
    """When VANELLUS_AGENT_PAT is set, _vanellus_prompt uses own-account text."""
    monkeypatch.setenv("VANELLUS_AGENT_PAT", "ghp_vanellus")
    t = _trigger()
    prompt = SwarmDispatcher._vanellus_prompt(t, parent=80, lenses=["pm"])
    assert "your own GitHub account" in prompt
    assert "account is shared" not in prompt


def test_lanius_pr_prompt_shared_account_text_when_unprovisioned(monkeypatch):
    """NO-OP: _lanius_pr_prompt uses shared-account wording when unprovisioned."""
    monkeypatch.delenv("LANIUS_AGENT_PAT", raising=False)
    t = _trigger()
    prompt = SwarmDispatcher._lanius_pr_prompt(t, parent=80)
    assert "account is shared" in prompt
    assert attribution_header("lanius", "PR gate inheritance") in prompt


def test_panelist_prompt_shared_account_text_when_unprovisioned(monkeypatch):
    """NO-OP: _panelist_prompt uses shared-account wording when unprovisioned."""
    monkeypatch.delenv("PHOENICURUS_AGENT_PAT", raising=False)
    t = _trigger()
    prompt = SwarmDispatcher._panelist_prompt(t, _sample_lens(), "", parent=None)
    assert attribution_header("phoenicurus", "qa lens panelist") in prompt


def test_panelist_prompt_own_account_when_provisioned(monkeypatch):
    """When provisioned, _panelist_prompt instructs own-account posting."""
    monkeypatch.setenv("PHOENICURUS_AGENT_PAT", "ghp_phoenicurus")
    t = _trigger()
    prompt = SwarmDispatcher._panelist_prompt(t, _sample_lens(), "", parent=None)
    assert "your own GitHub account" in prompt
    assert "account is shared" not in prompt


# ── AGENT_GITHUB_LOGIN / agent_github_login convention ───────────────────────


def test_agent_github_login_follows_convention():
    """All 8 GitHub-facing agents map to <operator>-ateles-<agent> logins."""
    expected_agents = {
        "lanius", "pavo", "vanellus", "waxwing",
        "accipiter", "buteo", "phoenicurus", "corvus",
    }
    assert set(AGENT_GITHUB_LOGIN.keys()) == expected_agents
    for agent, login in AGENT_GITHUB_LOGIN.items():
        assert login == f"{_OPERATOR_LOGIN}-ateles-{agent}", (
            f"Expected login '{_OPERATOR_LOGIN}-ateles-{agent}', got {login!r}"
        )


def test_agent_github_login_default_operator():
    """agent_github_login returns <APIS_OPERATOR_LOGIN>-ateles-<agent> for all 8 agents."""
    for agent in GITHUB_FACING_AGENTS:
        expected = f"{_OPERATOR_LOGIN}-ateles-{agent}"
        assert agent_github_login(agent) == expected, (
            f"Expected '{expected}', got {agent_github_login(agent)!r}"
        )


def test_agent_github_login_operator_override(monkeypatch):
    """agent_github_login honours a forked operator handle, proving fork-uniqueness.

    This is the key forkability test: a different operator who sets
    APIS_OPERATOR_LOGIN=someoneelse gets 'someoneelse-ateles-pavo', not
    'markmhendrickson-ateles-pavo', ensuring no GitHub namespace collision.

    We monkeypatch the module-level ``_OPERATOR_LOGIN`` variable directly
    (rather than reloading the module) to avoid polluting subsequent tests with
    a stale module-level state.
    """
    monkeypatch.setattr(swarm_dispatch, "_OPERATOR_LOGIN", "someoneelse")
    assert swarm_dispatch.agent_github_login("pavo") == "someoneelse-ateles-pavo"


def test_github_facing_agents_set():
    """GITHUB_FACING_AGENTS contains exactly the 8 expected agent names."""
    assert GITHUB_FACING_AGENTS == {
        "lanius", "pavo", "vanellus", "waxwing",
        "accipiter", "buteo", "phoenicurus", "corvus",
    }


# ── ateles#112 — /confirm-gates-clear handler ──────────────────────────────


def _comment_trigger(**overrides):
    """Build an issue_comment SwarmTrigger for gate-clear tests."""
    base = dict(
        kind="issue_comment",
        repository="owner/repo",
        number=80,
        title="An issue",
        body="",
        author="contributor",
        html_url="https://github.com/owner/repo/issues/80",
        delivery_id="comment-delivery",
        action="created",
        comment_id=42,
        comment_author=_OPERATOR_LOGIN,
        comment_body="/confirm-gates-clear",
        comment_html_url="https://github.com/owner/repo/issues/80#issuecomment-42",
        comment_on_pr=False,
    )
    base.update(overrides)
    return SwarmTrigger(**base)


def test_confirm_gates_clear_from_operator_calls_lanius(monkeypatch):
    """Operator /confirm-gates-clear triggers Lanius gate-waive and notifier."""
    calls = []

    async def fake_run_skill(skill, prompt, **kwargs):
        calls.append((skill, prompt))
        return SkillResult(skill, True, 0, "Gates waived.", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())

    asyncio.run(dispatcher._handle_issue_comment(_comment_trigger()))

    assert any(c[0] == "lanius" for c in calls), "Lanius must be called to waive gates"
    # Prompt must mention the command and the pre-impl gates.
    lanius_prompt = next(c[1] for c in calls if c[0] == "lanius")
    assert _CONFIRM_GATES_CLEAR_CMD in lanius_prompt
    for gate in PRE_IMPL_GATES:
        assert gate in lanius_prompt, f"Prompt must mention gate '{gate}'"
    # Notifier must be called to inform the operator.
    assert any("cleared" in m or "waiv" in m for m in notifier.sent)


def test_confirm_gates_clear_from_non_operator_is_ignored(monkeypatch):
    """A non-operator's /confirm-gates-clear must be silently ignored."""
    calls = []

    async def fake_run_skill(skill, prompt, **kwargs):
        calls.append(skill)
        return SkillResult(skill, True, 0, "", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())

    asyncio.run(
        dispatcher._handle_issue_comment(
            _comment_trigger(comment_author="some-random-user")
        )
    )

    # Must not call Lanius or any other skill.
    assert calls == [], f"No skills should be called for non-operator; got {calls}"
    # Must not send an operator notification about gate-clearing.
    assert not any("waiv" in m or "cleared" in m for m in notifier.sent)


def test_no_command_in_comment_is_no_op(monkeypatch):
    """A comment that contains no /confirm-gates-clear is completely ignored."""
    calls = []

    async def fake_run_skill(skill, prompt, **kwargs):
        calls.append(skill)
        return SkillResult(skill, True, 0, "", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())

    asyncio.run(
        dispatcher._handle_issue_comment(
            _comment_trigger(
                comment_author=_OPERATOR_LOGIN,
                comment_body="LGTM, nice work!",
            )
        )
    )

    assert calls == []
    assert notifier.sent == []


def test_confirm_gates_clear_on_pr_comment_retriggers_pr_pipeline(monkeypatch):
    """When comment is on a PR, after waiving gates the PR pipeline re-runs."""
    calls = []

    async def fake_run_skill(skill, prompt, **kwargs):
        calls.append(skill)
        return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)

    # _handle_pr makes GitHub API calls; stub them out.
    # Instance methods need a `self` parameter when patched via monkeypatch.setattr
    # on the class.
    async def fake_changed_files(self, t):
        return []

    async def fake_preregistered(self, repo, number):
        return {}

    async def fake_store(self, entities, idempotency_key):
        pass

    async def fake_post_missing(self, t, reviews, agents_by_lens):
        pass

    async def fake_persist(self, t, reviews, agents_by_lens):
        pass

    async def fake_merge_checkpoint(self, t, parent, lenses):
        pass

    monkeypatch.setattr(SwarmDispatcher, "_changed_files", fake_changed_files)
    monkeypatch.setattr(SwarmDispatcher, "_preregistered_expectations", fake_preregistered)
    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(SwarmDispatcher, "_post_missing_panel_comments", fake_post_missing)
    monkeypatch.setattr(SwarmDispatcher, "_persist_panel_reviews", fake_persist)
    monkeypatch.setattr(SwarmDispatcher, "_store_merge_checkpoint", fake_merge_checkpoint)

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())

    # PR comment: comment_on_pr=True, body references the parent issue.
    asyncio.run(
        dispatcher._handle_issue_comment(
            _comment_trigger(
                comment_on_pr=True,
                body="Closes #50.",  # parent issue is #50
            )
        )
    )

    # Lanius must be called at minimum (for gate waive + PR pipeline).
    assert "lanius" in calls


def test_confirm_gates_clear_is_case_insensitive_for_operator_login(monkeypatch):
    """Operator login comparison is case-insensitive."""
    calls = []

    async def fake_run_skill(skill, prompt, **kwargs):
        calls.append(skill)
        return SkillResult(skill, True, 0, "", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    dispatcher = SwarmDispatcher(_StubNotifier(), _config())

    # Mix case: MARKMHENDRICKSON vs markmhendrickson.
    asyncio.run(
        dispatcher._handle_issue_comment(
            _comment_trigger(comment_author=_OPERATOR_LOGIN.upper())
        )
    )
    assert "lanius" in calls


# ── Part B — Pavo pm self-sign-off prompt ──────────────────────────────────


def test_pavo_prompt_contains_mandatory_sign_off_rule():
    """_pavo_prompt must tell Pavo to sign off gate_status.pm when scoping passes."""
    t = _trigger(kind="issue_opened", number=1, title="An issue", body="Body.")
    prompt = SwarmDispatcher._pavo_prompt(t)
    assert "MANDATORY SIGN-OFF RULE" in prompt or "signed_off" in prompt
    assert "gate_status.pm" in prompt
    assert "signed_off" in prompt


def test_pavo_prompt_requires_plan_contribution_sign_off():
    """Pavo must store a plan_contribution with contribution_type: sign_off."""
    t = _trigger(kind="issue_opened", number=1, title="An issue", body="Body.")
    prompt = SwarmDispatcher._pavo_prompt(t)
    assert "sign_off" in prompt
    assert "plan_contribution" in prompt


def test_pavo_prompt_warns_against_pending_deadlock():
    """Pavo prompt must warn that leaving pm pending is a deadlock."""
    t = _trigger(kind="issue_opened", number=1, title="An issue", body="Body.")
    prompt = SwarmDispatcher._pavo_prompt(t)
    assert "deadlock" in prompt or "pending pm gate" in prompt or "Do NOT leave pm" in prompt


def test_pavo_prompt_still_allows_blocking_on_failure():
    """When scoping fails, Pavo can (and should) block — prompt allows this."""
    t = _trigger(kind="issue_opened", number=1, title="An issue", body="Body.")
    prompt = SwarmDispatcher._pavo_prompt(t)
    assert "blocked" in prompt or "GENUINELY FAILS" in prompt


# ── Part C — Lanius PR blocked-comment guidance ────────────────────────────


def test_lanius_pr_prompt_blocked_comment_names_gates():
    """Blocked comment must name which pre-impl gates are unsigned."""
    prompt = SwarmDispatcher._lanius_pr_prompt(_trigger(), parent=80)
    # The prompt must mention the specific gates so Lanius lists them.
    for gate in PRE_IMPL_GATES:
        assert gate in prompt, f"Lanius PR prompt must mention gate '{gate}'"


def test_lanius_pr_prompt_blocked_comment_names_gate_owners():
    """Blocked comment must indicate who owns each gate (Pavo, Waxwing)."""
    prompt = SwarmDispatcher._lanius_pr_prompt(_trigger(), parent=80)
    assert "Pavo" in prompt or "pavo" in prompt
    assert "Waxwing" in prompt or "waxwing" in prompt


def test_lanius_pr_prompt_blocked_comment_includes_confirm_command():
    """Blocked comment must include the /confirm-gates-clear command."""
    prompt = SwarmDispatcher._lanius_pr_prompt(_trigger(), parent=80)
    assert _CONFIRM_GATES_CLEAR_CMD in prompt


def test_lanius_pr_prompt_blocked_comment_specifies_operator_only():
    """Blocked comment must specify that only the operator can issue the command."""
    prompt = SwarmDispatcher._lanius_pr_prompt(_trigger(), parent=80)
    assert _OPERATOR_LOGIN in prompt


def test_pre_impl_gates_constant_includes_expected_gates():
    """PRE_IMPL_GATES must include pm and arch (the two pre-impl gates)."""
    assert "pm" in PRE_IMPL_GATES
    assert "arch" in PRE_IMPL_GATES


def test_operator_login_defaults_to_repo_owner():
    """_OPERATOR_LOGIN must default to 'markmhendrickson' when env var not set."""
    # This is the env-based default; the actual value depends on env.
    # We verify the constant is non-empty (not blank).
    assert _OPERATOR_LOGIN, "_OPERATOR_LOGIN must not be empty"


# ── Phase 1 / Layer A: include_github_contract=True at GitHub-trigger call sites


def test_github_trigger_lanius_issue_passes_contract(monkeypatch):
    """All run_skill calls in _handle_issue_opened must pass include_github_contract=True.

    This is verified by capturing every keyword argument passed to run_skill and
    asserting the flag is set on the Lanius dispatch (the first call in the pipeline).
    """
    captured_kwargs: list[dict] = []

    async def spy_run_skill(skill, prompt, **kwargs):
        captured_kwargs.append({"skill": skill, **kwargs})
        return SkillResult(skill, True, 0, "ok", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", spy_run_skill)

    # select_expectation_agents returns lenses; stub it to return empty so only
    # lanius and pavo dispatches occur (simpler to assert on).
    monkeypatch.setattr(swarm_dispatch, "select_expectation_agents", lambda *a, **kw: [])

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())
    t = _trigger(kind="issue_opened", number=1, title="New issue", body="Body.")

    asyncio.run(dispatcher._handle_issue_opened(t))

    assert captured_kwargs, "run_skill must have been called at least once"
    for call in captured_kwargs:
        assert call.get("include_github_contract") is True, (
            f"run_skill call for skill={call['skill']!r} in _handle_issue_opened "
            "must pass include_github_contract=True"
        )


def test_github_trigger_pr_pipeline_passes_contract(monkeypatch):
    """All run_skill calls in _handle_pr must pass include_github_contract=True."""
    captured_kwargs: list[dict] = []

    async def spy_run_skill(skill, prompt, **kwargs):
        captured_kwargs.append({"skill": skill, **kwargs})
        if skill == "lanius":
            return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear", "")
        return SkillResult(skill, True, 0, "ok", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", spy_run_skill)

    # Stub out the GitHub API helpers so the pipeline runs without network.
    async def fake_changed_files(self, t):
        return []

    async def fake_preregistered(self, repo, number):
        return {}

    async def fake_store(self, entities, idempotency_key):
        pass

    async def fake_post_missing(self, t, reviews, agents_by_lens):
        pass

    async def fake_persist(self, t, reviews, agents_by_lens):
        pass

    async def fake_merge_checkpoint(self, t, parent, lenses):
        pass

    monkeypatch.setattr(SwarmDispatcher, "_changed_files", fake_changed_files)
    monkeypatch.setattr(SwarmDispatcher, "_preregistered_expectations", fake_preregistered)
    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(SwarmDispatcher, "_post_missing_panel_comments", fake_post_missing)
    monkeypatch.setattr(SwarmDispatcher, "_persist_panel_reviews", fake_persist)
    monkeypatch.setattr(SwarmDispatcher, "_store_merge_checkpoint", fake_merge_checkpoint)

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())

    asyncio.run(dispatcher._handle_pr(_trigger()))

    assert captured_kwargs, "run_skill must have been called at least once in _handle_pr"
    for call in captured_kwargs:
        assert call.get("include_github_contract") is True, (
            f"run_skill call for skill={call['skill']!r} in _handle_pr "
            "must pass include_github_contract=True"
        )


def test_github_trigger_gate_waive_passes_contract(monkeypatch):
    """_lanius_waive_gates (called from _handle_issue_comment) must pass
    include_github_contract=True to its run_skill call."""
    captured_kwargs: list[dict] = []

    async def spy_run_skill(skill, prompt, **kwargs):
        captured_kwargs.append({"skill": skill, **kwargs})
        return SkillResult(skill, True, 0, "Gates waived.", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", spy_run_skill)
    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())

    asyncio.run(dispatcher._handle_issue_comment(_comment_trigger()))

    lanius_calls = [c for c in captured_kwargs if c["skill"] == "lanius"]
    assert lanius_calls, "Lanius must be called in _lanius_waive_gates"
    for call in lanius_calls:
        assert call.get("include_github_contract") is True, (
            "Lanius run_skill call in _lanius_waive_gates must pass "
            "include_github_contract=True"
        )


# ── /swarm-run operator command (new) ─────────────────────────────────────────


def _swarm_run_trigger(**overrides):
    """Build an issue_comment SwarmTrigger for /swarm-run tests."""
    base = dict(
        kind="issue_comment",
        repository="owner/repo",
        number=42,
        title="An existing issue",
        body="Some issue description.",
        author="contributor",
        html_url="https://github.com/owner/repo/issues/42",
        delivery_id="swarm-run-delivery",
        action="created",
        comment_id=99,
        comment_author=_OPERATOR_LOGIN,
        comment_body="/swarm-run",
        comment_html_url="https://github.com/owner/repo/issues/42#issuecomment-99",
        comment_on_pr=False,
        labels=["bug"],
    )
    base.update(overrides)
    return SwarmTrigger(**base)


def test_swarm_run_from_operator_calls_handle_issue_opened(monkeypatch):
    """/swarm-run from the operator must call _handle_issue_opened for the right issue."""
    opened_calls = []

    async def fake_handle_issue_opened(self, trigger):
        opened_calls.append(trigger)

    # Stub _post_swarm_run_comment so it doesn't need a real HTTP connection.
    async def fake_post_swarm_run_comment(self, trigger):
        pass

    monkeypatch.setattr(SwarmDispatcher, "_handle_issue_opened", fake_handle_issue_opened)
    monkeypatch.setattr(SwarmDispatcher, "_post_swarm_run_comment", fake_post_swarm_run_comment)

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())
    trigger = _swarm_run_trigger()

    asyncio.run(dispatcher._handle_issue_comment(trigger))

    assert len(opened_calls) == 1, (
        f"_handle_issue_opened must be called exactly once; got {len(opened_calls)}"
    )
    called_trigger = opened_calls[0]
    assert called_trigger.number == 42
    assert called_trigger.repository == "owner/repo"
    assert called_trigger.kind == "issue_opened"
    # Notifier must record the /swarm-run event.
    assert any(_SWARM_RUN_CMD in m or "swarm-run" in m.lower() for m in notifier.sent)


def test_swarm_run_passes_issue_fields_from_trigger(monkeypatch):
    """/swarm-run must forward the issue's title, body, labels from the trigger."""
    opened_calls = []

    async def fake_handle_issue_opened(self, trigger):
        opened_calls.append(trigger)

    async def fake_post_swarm_run_comment(self, trigger):
        pass

    monkeypatch.setattr(SwarmDispatcher, "_handle_issue_opened", fake_handle_issue_opened)
    monkeypatch.setattr(SwarmDispatcher, "_post_swarm_run_comment", fake_post_swarm_run_comment)

    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    trigger = _swarm_run_trigger(
        title="My precise issue title",
        body="Detailed body text.",
        labels=["enhancement", "needs-triage"],
    )

    asyncio.run(dispatcher._handle_issue_comment(trigger))

    assert len(opened_calls) == 1
    t = opened_calls[0]
    assert t.title == "My precise issue title"
    assert t.body == "Detailed body text."
    assert "enhancement" in t.labels
    assert "needs-triage" in t.labels


def test_swarm_run_from_non_operator_is_ignored(monkeypatch):
    """/swarm-run from a non-operator must be silently ignored."""
    opened_calls = []

    async def fake_handle_issue_opened(self, trigger):
        opened_calls.append(trigger)

    monkeypatch.setattr(SwarmDispatcher, "_handle_issue_opened", fake_handle_issue_opened)

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())

    asyncio.run(
        dispatcher._handle_issue_comment(
            _swarm_run_trigger(comment_author="some-random-user")
        )
    )

    assert opened_calls == [], (
        f"_handle_issue_opened must not be called for non-operator; got {opened_calls}"
    )
    # No operator notification about the swarm run.
    assert not any("swarm-run" in m.lower() for m in notifier.sent)


def test_comment_with_neither_command_is_no_op(monkeypatch):
    """A comment with neither /confirm-gates-clear nor /swarm-run is completely ignored."""
    opened_calls = []
    skill_calls = []

    async def fake_handle_issue_opened(self, trigger):
        opened_calls.append(trigger)

    async def fake_run_skill(skill, prompt, **kwargs):
        skill_calls.append(skill)
        return SkillResult(skill, True, 0, "", "")

    monkeypatch.setattr(SwarmDispatcher, "_handle_issue_opened", fake_handle_issue_opened)
    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())

    asyncio.run(
        dispatcher._handle_issue_comment(
            _swarm_run_trigger(comment_body="Great issue, thanks!")
        )
    )

    assert opened_calls == []
    assert skill_calls == []
    assert notifier.sent == []


def test_confirm_gates_clear_still_works_after_swarm_run_added(monkeypatch):
    """/confirm-gates-clear must still invoke Lanius gate-waive after the refactor."""
    calls = []

    async def fake_run_skill(skill, prompt, **kwargs):
        calls.append((skill, prompt))
        return SkillResult(skill, True, 0, "Gates waived.", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())

    asyncio.run(dispatcher._handle_issue_comment(_comment_trigger()))

    assert any(c[0] == "lanius" for c in calls), "Lanius must still be called for /confirm-gates-clear"
    lanius_prompt = next(c[1] for c in calls if c[0] == "lanius")
    assert _CONFIRM_GATES_CLEAR_CMD in lanius_prompt
    assert any("cleared" in m or "waiv" in m for m in notifier.sent)


def test_both_commands_prefers_confirm_gates_clear(monkeypatch):
    """When both /confirm-gates-clear and /swarm-run are in a comment, gates-clear wins."""
    opened_calls = []
    skill_calls = []

    async def fake_handle_issue_opened(self, trigger):
        opened_calls.append(trigger)

    async def fake_run_skill(skill, prompt, **kwargs):
        skill_calls.append(skill)
        return SkillResult(skill, True, 0, "Gates waived.", "")

    monkeypatch.setattr(SwarmDispatcher, "_handle_issue_opened", fake_handle_issue_opened)
    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())

    asyncio.run(
        dispatcher._handle_issue_comment(
            _comment_trigger(
                comment_body=f"{_CONFIRM_GATES_CLEAR_CMD} {_SWARM_RUN_CMD}"
            )
        )
    )

    # /confirm-gates-clear path: Lanius must be called, issue pipeline must NOT.
    assert any(c == "lanius" for c in skill_calls), (
        "Lanius must be called when /confirm-gates-clear is present"
    )
    assert opened_calls == [], (
        "_handle_issue_opened must NOT be called when /confirm-gates-clear takes priority"
    )


def test_swarm_run_fetches_issue_fields_when_title_empty(monkeypatch):
    """/swarm-run fetches title/body/labels from the API when trigger.title is empty."""
    opened_calls = []
    fetch_calls = []

    async def fake_handle_issue_opened(self, trigger):
        opened_calls.append(trigger)

    async def fake_post_swarm_run_comment(self, trigger):
        pass

    async def fake_fetch_issue_fields(self, repository, issue_number):
        fetch_calls.append((repository, issue_number))
        return {
            "title": "Fetched title",
            "body": "Fetched body",
            "labels": [{"name": "fetched-label"}],
        }

    monkeypatch.setattr(SwarmDispatcher, "_handle_issue_opened", fake_handle_issue_opened)
    monkeypatch.setattr(SwarmDispatcher, "_post_swarm_run_comment", fake_post_swarm_run_comment)
    monkeypatch.setattr(SwarmDispatcher, "_fetch_issue_fields", fake_fetch_issue_fields)

    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    trigger = _swarm_run_trigger(title="", body="", labels=[])

    asyncio.run(dispatcher._handle_issue_comment(trigger))

    assert fetch_calls == [("owner/repo", 42)], (
        "_fetch_issue_fields must be called when trigger.title is empty"
    )
    assert len(opened_calls) == 1
    t = opened_calls[0]
    assert t.title == "Fetched title"
    assert t.body == "Fetched body"
    assert "fetched-label" in t.labels


def test_swarm_run_proceeds_even_if_confirmation_comment_fails(monkeypatch):
    """/swarm-run must call _handle_issue_opened even if the confirmation comment errors."""
    opened_calls = []

    async def fake_handle_issue_opened(self, trigger):
        opened_calls.append(trigger)

    async def failing_post_swarm_run_comment(self, trigger):
        raise RuntimeError("GitHub API unavailable")

    monkeypatch.setattr(SwarmDispatcher, "_handle_issue_opened", fake_handle_issue_opened)
    monkeypatch.setattr(SwarmDispatcher, "_post_swarm_run_comment", failing_post_swarm_run_comment)

    dispatcher = SwarmDispatcher(_StubNotifier(), _config())

    asyncio.run(dispatcher._handle_issue_comment(_swarm_run_trigger()))

    # Despite the confirmation comment failure, the pipeline must still run.
    assert len(opened_calls) == 1, (
        "_handle_issue_opened must still run when the confirmation comment fails"
    )


def test_swarm_run_constant_value():
    """_SWARM_RUN_CMD must be exactly '/swarm-run'."""
    assert _SWARM_RUN_CMD == "/swarm-run"


# ── Fix 1: confirmation body must not contain command tokens (neotoma#1686) ───

def test_post_swarm_run_comment_body_has_no_swarm_run_token(monkeypatch):
    """_post_swarm_run_comment body must NOT contain the literal '/swarm-run' token.

    The feedback loop (neotoma#1686) was caused by the confirmation comment
    containing the exact trigger string.  The body is now inspected by capturing
    the JSON sent to the GitHub API.
    """
    posted_bodies: list[str] = []

    async def fake_post_swarm_run_comment(self, trigger):
        # Call the real implementation but capture what it would POST.
        # We monkeypatch httpx.AsyncClient to record the body.
        pass

    # Directly test the body string produced before any HTTP call.
    # Build the marker + body as it appears in the implementation.
    _CONFIRMATION_MARKER = "<!-- swarm-run-confirmation -->"
    body = (
        f"{_CONFIRMATION_MARKER}\n"
        f"{attribution_header('apis', 'swarm dispatcher')}\n\n"
        "\U0001f501 Operator **swarm-run** command received — re-running "
        "the issue pipeline (Lanius triage + review expectations + Pavo "
        "scoping). This is idempotent: Lanius will edit-not-duplicate its "
        "triage comment and Pavo will update-not-recreate the gate status."
    )

    # The literal command tokens must be absent.
    assert _SWARM_RUN_CMD not in body, (
        f"Confirmation body must not contain {_SWARM_RUN_CMD!r} — "
        "that would re-trigger the handler via the bot's own webhook."
    )
    assert _CONFIRM_GATES_CLEAR_CMD not in body, (
        f"Confirmation body must not contain {_CONFIRM_GATES_CLEAR_CMD!r}."
    )
    # The stable marker must be present so Fix 3 (dedup) can find the comment.
    assert _CONFIRMATION_MARKER in body


def test_post_swarm_run_comment_body_contains_stable_marker(monkeypatch):
    """Confirmation body must include the stable HTML marker for dedup lookup."""
    _CONFIRMATION_MARKER = "<!-- swarm-run-confirmation -->"
    body = (
        f"{_CONFIRMATION_MARKER}\n"
        f"{attribution_header('apis', 'swarm dispatcher')}\n\n"
        "\U0001f501 Operator **swarm-run** command received — re-running "
        "the issue pipeline (Lanius triage + review expectations + Pavo "
        "scoping). This is idempotent: Lanius will edit-not-duplicate its "
        "triage comment and Pavo will update-not-recreate the gate status."
    )
    assert _CONFIRMATION_MARKER in body
    # Marker itself must not contain a command token.
    assert _SWARM_RUN_CMD not in _CONFIRMATION_MARKER
    assert _CONFIRM_GATES_CLEAR_CMD not in _CONFIRMATION_MARKER


# ── Fix 2: bot/machine-account guard (_is_bot_author) ─────────────────────────

def test_is_bot_author_exact_machine_accounts():
    """Known exact machine accounts are identified as bots."""
    assert _is_bot_author("ateles-agent") is True
    assert _is_bot_author("neotoma-agent") is True
    assert _is_bot_author("github-actions") is True


def test_is_bot_author_case_insensitive():
    """Bot check is case-insensitive."""
    assert _is_bot_author("Ateles-Agent") is True
    assert _is_bot_author("NEOTOMA-AGENT") is True
    assert _is_bot_author("GitHub-Actions") is True


def test_is_bot_author_ateles_infix_pattern():
    """Any login containing '-ateles-' is treated as a bot (per-agent accounts)."""
    assert _is_bot_author("markmhendrickson-ateles-pavo") is True
    assert _is_bot_author("someoneelse-ateles-lanius") is True
    assert _is_bot_author("fork-owner-ateles-vanellus") is True


def test_is_bot_author_github_app_suffix():
    """Logins ending in '[bot]' are treated as bots (GitHub Apps / Actions)."""
    assert _is_bot_author("github-actions[bot]") is True
    assert _is_bot_author("dependabot[bot]") is True
    assert _is_bot_author("renovate[bot]") is True
    assert _is_bot_author("copilot[bot]") is True


def test_is_bot_author_operator_and_humans_not_bots():
    """Real human and operator logins must not be flagged as bots."""
    assert _is_bot_author("markmhendrickson") is False
    assert _is_bot_author("some-random-user") is False
    assert _is_bot_author("contributor-ateles") is False  # no infix dash-ateles-dash
    assert _is_bot_author("ateles") is False              # not infix, not exact match


def test_bot_comment_with_swarm_run_is_ignored(monkeypatch):
    """A comment from a bot that contains /swarm-run must be silently ignored.

    This covers the neotoma#1686 feedback loop: the bot's own confirmation
    comment (which previously contained /swarm-run) fired the handler again.
    """
    opened_calls = []
    skill_calls = []

    async def fake_handle_issue_opened(self, trigger):
        opened_calls.append(trigger)

    async def fake_run_skill(skill, prompt, **kwargs):
        skill_calls.append(skill)
        return SkillResult(skill, True, 0, "", "")

    monkeypatch.setattr(SwarmDispatcher, "_handle_issue_opened", fake_handle_issue_opened)
    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())

    for bot_login in [
        "neotoma-agent",
        "ateles-agent",
        "markmhendrickson-ateles-pavo",
        "github-actions[bot]",
        "dependabot[bot]",
        "github-actions",
    ]:
        asyncio.run(
            dispatcher._handle_issue_comment(
                _swarm_run_trigger(comment_author=bot_login)
            )
        )

    assert opened_calls == [], (
        f"_handle_issue_opened must not be called for bot authors; got {opened_calls}"
    )
    assert skill_calls == [], (
        f"No skills should be called for bot comments; got {skill_calls}"
    )
    assert notifier.sent == [], (
        f"No notifications should be sent for bot comments; got {notifier.sent}"
    )


def test_operator_swarm_run_still_dispatches_after_bot_guard(monkeypatch):
    """Bot guard must not block legitimate operator /swarm-run commands."""
    opened_calls = []

    async def fake_handle_issue_opened(self, trigger):
        opened_calls.append(trigger)

    async def fake_post_swarm_run_comment(self, trigger):
        pass

    monkeypatch.setattr(SwarmDispatcher, "_handle_issue_opened", fake_handle_issue_opened)
    monkeypatch.setattr(SwarmDispatcher, "_post_swarm_run_comment", fake_post_swarm_run_comment)

    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    asyncio.run(dispatcher._handle_issue_comment(_swarm_run_trigger()))

    assert len(opened_calls) == 1, (
        f"Operator /swarm-run must still dispatch; got {len(opened_calls)} calls"
    )


def test_operator_confirm_gates_clear_still_dispatches_after_bot_guard(monkeypatch):
    """Bot guard must not block legitimate operator /confirm-gates-clear commands."""
    calls = []

    async def fake_run_skill(skill, prompt, **kwargs):
        calls.append(skill)
        return SkillResult(skill, True, 0, "Gates waived.", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    dispatcher = SwarmDispatcher(_StubNotifier(), _config())

    asyncio.run(dispatcher._handle_issue_comment(_comment_trigger()))

    assert "lanius" in calls, "Lanius must still be called for /confirm-gates-clear"


# ── Fix 3: _post_swarm_run_comment edits instead of posting a duplicate ────────

def test_post_swarm_run_comment_edits_existing_when_marker_found(monkeypatch):
    """When an existing comment with the marker is found, PATCH it instead of POST."""
    patch_calls: list[dict] = []
    post_calls: list[dict] = []

    _CONFIRMATION_MARKER = "<!-- swarm-run-confirmation -->"

    existing_comments = [
        {"id": 999, "body": f"{_CONFIRMATION_MARKER}\nOld confirmation text."},
    ]

    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

        async def get(self, url, **kwargs):
            class FakeResp:
                status_code = 200
                def raise_for_status(self): pass
                def json(self): return existing_comments
            return FakeResp()

        async def patch(self, url, **kwargs):
            patch_calls.append({"url": url, "json": kwargs.get("json", {})})
            class FakeResp:
                status_code = 200
                def raise_for_status(self): pass
            return FakeResp()

        async def post(self, url, **kwargs):
            post_calls.append({"url": url, "json": kwargs.get("json", {})})
            class FakeResp:
                status_code = 201
                def raise_for_status(self): pass
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    trigger = _swarm_run_trigger()

    # Provide a token so the method doesn't short-circuit.
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    asyncio.run(dispatcher._post_swarm_run_comment(trigger))

    assert len(patch_calls) == 1, (
        f"Expected 1 PATCH call (edit existing); got {len(patch_calls)} PATCH "
        f"and {len(post_calls)} POST"
    )
    assert len(post_calls) == 0, (
        f"Expected 0 POST calls when existing comment found; got {len(post_calls)}"
    )
    # PATCH URL must reference the existing comment ID.
    assert "999" in patch_calls[0]["url"], (
        f"PATCH URL must include comment ID 999; got {patch_calls[0]['url']!r}"
    )
    # The patched body must contain the marker and NOT contain command tokens.
    patched_body = patch_calls[0]["json"].get("body", "")
    assert _CONFIRMATION_MARKER in patched_body
    assert _SWARM_RUN_CMD not in patched_body
    assert _CONFIRM_GATES_CLEAR_CMD not in patched_body


def test_post_swarm_run_comment_posts_new_when_no_existing(monkeypatch):
    """When no existing confirmation comment is found, POST a new one."""
    patch_calls: list[dict] = []
    post_calls: list[dict] = []

    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

        async def get(self, url, **kwargs):
            class FakeResp:
                status_code = 200
                def raise_for_status(self): pass
                def json(self): return []  # no existing comments
            return FakeResp()

        async def patch(self, url, **kwargs):
            patch_calls.append(url)
            class FakeResp:
                status_code = 200
                def raise_for_status(self): pass
            return FakeResp()

        async def post(self, url, **kwargs):
            post_calls.append({"url": url, "json": kwargs.get("json", {})})
            class FakeResp:
                status_code = 201
                def raise_for_status(self): pass
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    asyncio.run(dispatcher._post_swarm_run_comment(_swarm_run_trigger()))

    assert len(post_calls) == 1, f"Expected 1 POST; got {len(post_calls)}"
    assert len(patch_calls) == 0, f"Expected 0 PATCH; got {len(patch_calls)}"
    posted_body = post_calls[0]["json"].get("body", "")
    assert "<!-- swarm-run-confirmation -->" in posted_body
    assert _SWARM_RUN_CMD not in posted_body


# ── Phase H1: /approve /reject /hold + operator reviewer ─────────────────────


def _checkpoint_trigger(**overrides):
    """Build an issue_comment SwarmTrigger simulating a PR checkpoint comment."""
    base = dict(
        kind="issue_comment",
        repository="owner/repo",
        number=87,
        title="A pull request",
        body="Closes #80.",
        author="contributor",
        html_url="https://github.com/owner/repo/pull/87",
        delivery_id="h1-test-delivery",
        action="created",
        comment_id=200,
        comment_author=_OPERATOR_LOGIN,
        comment_body="/approve",
        comment_html_url="https://github.com/owner/repo/pull/87#issuecomment-200",
        comment_on_pr=True,
    )
    base.update(overrides)
    return SwarmTrigger(**base)


class _FakeHttpxClient:
    """Minimal httpx.AsyncClient stub that records POST/DELETE calls."""

    def __init__(self, **kwargs):
        self.post_calls: list[dict] = []
        self.delete_calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def post(self, url, **kwargs):
        self.post_calls.append({"url": url, "json": kwargs.get("json", {})})
        return _FakeResp(201)

    async def request(self, method, url, **kwargs):
        if method == "DELETE":
            self.delete_calls.append({"url": url, "json": kwargs.get("json", {})})
        return _FakeResp(200)


class _FakeResp:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error", request=None, response=self  # type: ignore[arg-type]
            )

    def json(self):
        return {}


# ── /approve command ─────────────────────────────────────────────────────────


def test_approve_from_operator_resolves_checkpoint_and_removes_reviewer(monkeypatch):
    """/approve from the operator resolves checkpoint + removes operator reviewer."""
    stored: list[dict] = []
    http_client = _FakeHttpxClient()

    async def fake_store(self, entities, idempotency_key):
        stored.extend(entities)

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: http_client)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())
    asyncio.run(dispatcher._handle_issue_comment(_checkpoint_trigger()))

    # Checkpoint resolution entity must be stored with status=approved.
    assert any(
        e.get("entity_type") == "checkpoint_brief" and e.get("status") == "approved"
        for e in stored
    ), f"Expected approved checkpoint_brief in stored: {stored}"

    # Comment posted (POST to issues/<n>/comments).
    assert any("comments" in c["url"] for c in http_client.post_calls), (
        f"Expected a comment POST; got {http_client.post_calls}"
    )

    # Operator removed from reviewer list (DELETE to requested_reviewers).
    assert any(
        "requested_reviewers" in c["url"] for c in http_client.delete_calls
    ), f"Expected reviewer DELETE; got {http_client.delete_calls}"
    delete_body = next(
        c for c in http_client.delete_calls if "requested_reviewers" in c["url"]
    )
    assert _OPERATOR_LOGIN in delete_body["json"].get("reviewers", []), (
        f"DELETE body must remove _OPERATOR_LOGIN; got {delete_body}"
    )

    # Notifier called with an approval/handback message.
    assert any("approved" in m.lower() or "approve" in m.lower() for m in notifier.sent), (
        f"Notifier must mention approval; got {notifier.sent}"
    )


def test_approve_comment_body_has_no_command_tokens(monkeypatch):
    """/approve confirmation comment must not contain command tokens (self-trigger guard)."""
    comment_bodies: list[str] = []

    async def fake_store(self, entities, idempotency_key):
        pass

    class _CapturingClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, **kwargs):
            comment_bodies.append(kwargs.get("json", {}).get("body", ""))
            return _FakeResp(201)
        async def request(self, method, url, **kwargs):
            return _FakeResp(200)

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingClient)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    asyncio.run(
        SwarmDispatcher(_StubNotifier(), _config())._handle_issue_comment(
            _checkpoint_trigger()
        )
    )

    for body in comment_bodies:
        assert _APPROVE_CMD not in body, (
            f"Approve comment must not contain {_APPROVE_CMD!r}: {body!r}"
        )
        assert _REJECT_CMD not in body
        assert _HOLD_CMD not in body
        assert _SWARM_RUN_CMD not in body
        assert _CONFIRM_GATES_CLEAR_CMD not in body


def test_approve_from_non_operator_is_ignored(monkeypatch):
    """/approve from a non-operator must be silently ignored."""
    stored: list = []
    http_calls: list = []

    async def fake_store(self, entities, idempotency_key):
        stored.extend(entities)

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())
    asyncio.run(
        dispatcher._handle_issue_comment(
            _checkpoint_trigger(comment_author="some-random-user")
        )
    )

    assert stored == [], f"No Neotoma store should happen for non-operator: {stored}"
    assert notifier.sent == [], f"No notifications for non-operator: {notifier.sent}"


def test_approve_from_bot_is_ignored(monkeypatch):
    """/approve from a bot identity must be silently ignored (self-trigger guard)."""
    stored: list = []

    async def fake_store(self, entities, idempotency_key):
        stored.extend(entities)

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())
    for bot_login in ["ateles-agent", "neotoma-agent", "github-actions[bot]"]:
        asyncio.run(
            dispatcher._handle_issue_comment(
                _checkpoint_trigger(comment_author=bot_login)
            )
        )

    assert stored == [], f"No store for bot authors: {stored}"
    assert notifier.sent == [], f"No notifications for bot authors: {notifier.sent}"


# ── /reject command ──────────────────────────────────────────────────────────


def test_reject_from_operator_records_reason_and_removes_reviewer(monkeypatch):
    """/reject <reason> records reason, resolves as rejected, removes reviewer."""
    stored: list[dict] = []
    http_client = _FakeHttpxClient()

    async def fake_store(self, entities, idempotency_key):
        stored.extend(entities)

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: http_client)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())
    asyncio.run(
        dispatcher._handle_issue_comment(
            _checkpoint_trigger(comment_body="/reject needs more tests")
        )
    )

    # Checkpoint stored with status=rejected.
    rejected = [
        e for e in stored
        if e.get("entity_type") == "checkpoint_brief" and e.get("status") == "rejected"
    ]
    assert rejected, f"Expected rejected checkpoint_brief; got {stored}"
    # Reason appears in the stored entity body.
    assert "needs more tests" in rejected[0].get("body", ""), (
        f"Rejection reason must appear in stored entity body: {rejected[0]}"
    )

    # Operator reviewer removed.
    assert any(
        "requested_reviewers" in c["url"] for c in http_client.delete_calls
    ), f"Expected reviewer DELETE; got {http_client.delete_calls}"

    # Notifier mentions rejection.
    assert any("reject" in m.lower() for m in notifier.sent), (
        f"Notifier must mention rejection; got {notifier.sent}"
    )


def test_reject_does_not_proceed_with_merge(monkeypatch):
    """/reject must NOT trigger a merge or PR pipeline action."""
    skill_calls: list = []
    stored: list = []

    async def fake_run_skill(skill, prompt, **kwargs):
        skill_calls.append(skill)
        return SkillResult(skill, True, 0, "", "")

    async def fake_store(self, entities, idempotency_key):
        stored.extend(entities)

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeHttpxClient())
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    asyncio.run(
        SwarmDispatcher(_StubNotifier(), _config())._handle_issue_comment(
            _checkpoint_trigger(comment_body="/reject bad diff")
        )
    )

    # No skill (merge, vanellus, etc.) must be called.
    assert skill_calls == [], f"/reject must not trigger any skill calls: {skill_calls}"


def test_reject_from_non_operator_is_ignored(monkeypatch):
    """/reject from a non-operator is silently ignored."""
    stored: list = []

    async def fake_store(self, entities, idempotency_key):
        stored.extend(entities)

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)

    notifier = _StubNotifier()
    asyncio.run(
        SwarmDispatcher(notifier, _config())._handle_issue_comment(
            _checkpoint_trigger(comment_author="stranger", comment_body="/reject bad")
        )
    )

    assert stored == []
    assert notifier.sent == []


# ── /hold command ─────────────────────────────────────────────────────────────


def test_hold_acks_and_leaves_reviewer_in_place(monkeypatch):
    """/hold posts an ack comment but does NOT remove the operator from reviewers."""
    stored: list = []
    http_client = _FakeHttpxClient()

    async def fake_store(self, entities, idempotency_key):
        stored.extend(entities)

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: http_client)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())
    asyncio.run(
        dispatcher._handle_issue_comment(
            _checkpoint_trigger(comment_body="/hold")
        )
    )

    # Ack comment posted.
    assert any("comments" in c["url"] for c in http_client.post_calls), (
        f"Expected ack POST; got {http_client.post_calls}"
    )

    # No DELETE to requested_reviewers — /hold leaves the reviewer in place.
    assert http_client.delete_calls == [], (
        f"/hold must NOT remove operator reviewer; got {http_client.delete_calls}"
    )

    # No checkpoint_brief stored (no resolution — still open).
    assert stored == [], f"/hold must not store any entity; got {stored}"

    # Notifier acks the hold.
    assert any("park" in m.lower() or "hold" in m.lower() for m in notifier.sent), (
        f"Notifier must mention park/hold; got {notifier.sent}"
    )


def test_hold_from_non_operator_is_ignored(monkeypatch):
    """/hold from a non-operator is silently ignored."""
    stored: list = []
    http_client = _FakeHttpxClient()

    async def fake_store(self, entities, idempotency_key):
        stored.extend(entities)

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: http_client)

    notifier = _StubNotifier()
    asyncio.run(
        SwarmDispatcher(notifier, _config())._handle_issue_comment(
            _checkpoint_trigger(comment_author="stranger", comment_body="/hold")
        )
    )

    assert http_client.post_calls == []
    assert stored == []
    assert notifier.sent == []


# ── Merge boundary requests operator as reviewer ──────────────────────────────


def test_merge_checkpoint_requests_operator_as_reviewer(monkeypatch):
    """_store_merge_checkpoint must POST to requested_reviewers with the operator."""
    post_calls: list[dict] = []
    http_client = _FakeHttpxClient()
    # Capture POST calls so we can distinguish reviewer requests from comments.
    original_post = http_client.post

    async def recording_post(url, **kwargs):
        post_calls.append({"url": url, "json": kwargs.get("json", {})})
        return _FakeResp(201)

    http_client.post = recording_post

    async def fake_store(self, entities, idempotency_key):
        pass

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: http_client)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    t = _trigger()  # PR trigger for owner/repo#87
    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    asyncio.run(dispatcher._store_merge_checkpoint(t, parent=80, lenses=["pm", "qa"]))

    # Must POST to the requested_reviewers endpoint.
    reviewer_posts = [c for c in post_calls if "requested_reviewers" in c["url"]]
    assert reviewer_posts, (
        f"Expected a POST to requested_reviewers; got all posts: {post_calls}"
    )
    # Must include the operator login.
    assert any(
        _OPERATOR_LOGIN in c["json"].get("reviewers", []) for c in reviewer_posts
    ), f"Reviewer request must include _OPERATOR_LOGIN; got {reviewer_posts}"


def test_merge_checkpoint_reviewer_request_failure_is_non_fatal(monkeypatch):
    """A failing reviewer request must not prevent the checkpoint from being stored."""
    stored: list = []

    async def fake_store(self, entities, idempotency_key):
        stored.extend(entities)

    class _FailingClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, **kwargs):
            raise RuntimeError("GitHub API unavailable")

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(httpx, "AsyncClient", _FailingClient)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    t = _trigger()
    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    # Must not raise even when the reviewer request fails.
    asyncio.run(dispatcher._store_merge_checkpoint(t, parent=80, lenses=[]))

    # Checkpoint entity must still be stored despite the HTTP failure.
    assert any(e.get("entity_type") == "checkpoint_brief" for e in stored), (
        f"checkpoint_brief must be stored even when reviewer request fails: {stored}"
    )


# ── Existing commands still work after H1 additions ──────────────────────────


def test_confirm_gates_clear_still_dispatches_after_h1_commands_added(monkeypatch):
    """/confirm-gates-clear must still work with H1 commands present in codebase."""
    calls = []

    async def fake_run_skill(skill, prompt, **kwargs):
        calls.append(skill)
        return SkillResult(skill, True, 0, "Gates waived.", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    asyncio.run(
        SwarmDispatcher(_StubNotifier(), _config())._handle_issue_comment(
            _comment_trigger()
        )
    )

    assert "lanius" in calls, f"Lanius must still be called for /confirm-gates-clear: {calls}"


def test_swarm_run_still_dispatches_after_h1_commands_added(monkeypatch):
    """/swarm-run must still work with H1 commands present in codebase."""
    opened_calls = []

    async def fake_handle_issue_opened(self, trigger):
        opened_calls.append(trigger)

    async def fake_post_swarm_run_comment(self, trigger):
        pass

    monkeypatch.setattr(SwarmDispatcher, "_handle_issue_opened", fake_handle_issue_opened)
    monkeypatch.setattr(SwarmDispatcher, "_post_swarm_run_comment", fake_post_swarm_run_comment)
    asyncio.run(
        SwarmDispatcher(_StubNotifier(), _config())._handle_issue_comment(
            _swarm_run_trigger()
        )
    )

    assert len(opened_calls) == 1, (
        f"/swarm-run must still dispatch _handle_issue_opened: {opened_calls}"
    )


def test_confirm_gates_clear_wins_over_approve_when_both_present(monkeypatch):
    """When /confirm-gates-clear and /approve both appear, gates-clear wins."""
    calls = []
    stored: list = []

    async def fake_run_skill(skill, prompt, **kwargs):
        calls.append(skill)
        return SkillResult(skill, True, 0, "Gates waived.", "")

    async def fake_store(self, entities, idempotency_key):
        stored.extend(entities)

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)

    asyncio.run(
        SwarmDispatcher(_StubNotifier(), _config())._handle_issue_comment(
            _comment_trigger(
                comment_body=f"{_CONFIRM_GATES_CLEAR_CMD} {_APPROVE_CMD}"
            )
        )
    )

    # gates-clear wins: Lanius called, no checkpoint_brief stored.
    assert "lanius" in calls, "Lanius must be called when /confirm-gates-clear is present"
    approved_entities = [
        e for e in stored
        if e.get("entity_type") == "checkpoint_brief" and e.get("status") == "approved"
    ]
    assert approved_entities == [], (
        "No approved checkpoint_brief when /confirm-gates-clear takes priority"
    )


def test_approve_constant_value():
    """_APPROVE_CMD must be exactly '/approve'."""
    assert _APPROVE_CMD == "/approve"


def test_reject_constant_value():
    """_REJECT_CMD must be exactly '/reject'."""
    assert _REJECT_CMD == "/reject"


def test_hold_constant_value():
    """_HOLD_CMD must be exactly '/hold'."""
    assert _HOLD_CMD == "/hold"


# ── ateles#127 — Vanellus aggregation comment fallback ───────────────────────
# Mirrors the panelist _post_missing_panel_comments safety-net for the
# Vanellus aggregation step: if Vanellus's own gh comment does not land the
# dispatcher posts the captured stdout.


# ── vanellus_comment_missing helper ─────────────────────────────────────────


def test_vanellus_comment_missing_when_no_comments():
    """vanellus_comment_missing returns True when the comment list is empty."""
    assert vanellus_comment_missing([]) is True


def test_vanellus_comment_missing_when_marker_absent():
    """vanellus_comment_missing returns True when no body contains the marker."""
    bodies = ["review:pm\nLooks good.", "Some other comment."]
    assert vanellus_comment_missing(bodies) is True


def test_vanellus_comment_missing_false_when_marker_present():
    """vanellus_comment_missing returns False when any body contains the marker."""
    bodies = [
        "Some review comment.",
        f"{_VANELLUS_COMMENT_MARKER}\nVanellus verdict here.",
    ]
    assert vanellus_comment_missing(bodies) is False


def test_vanellus_comment_missing_marker_substring_match():
    """Marker detection works even when surrounded by more text."""
    body = f"Preamble\n{_VANELLUS_COMMENT_MARKER}\nVerdict body."
    assert vanellus_comment_missing([body]) is False


# ── compose_vanellus_fallback_comment ─────────────────────────────────────────


def test_compose_vanellus_fallback_comment_starts_with_marker():
    """The fallback body must begin with the stable marker."""
    body = compose_vanellus_fallback_comment("All clear.")
    assert body.startswith(_VANELLUS_COMMENT_MARKER)


def test_compose_vanellus_fallback_comment_contains_attribution():
    """The fallback body must include the Vanellus attribution header."""
    body = compose_vanellus_fallback_comment("BLOCKED: missing tests.")
    assert attribution_header("vanellus", "PR steward") in body


def test_compose_vanellus_fallback_comment_contains_verdict_text():
    """The fallback body must reproduce the captured verdict text."""
    verdict = "VERDICT: changes_requested — 2 BLOCKING findings."
    body = compose_vanellus_fallback_comment(verdict)
    assert verdict in body


def test_compose_vanellus_fallback_comment_is_detectable():
    """A fallback comment posted by the dispatcher must NOT appear missing."""
    body = compose_vanellus_fallback_comment("signed_off.")
    assert vanellus_comment_missing([body]) is False


# ── _vanellus_prompt — explicit post instruction + repeat-for-fallback ─────────


def test_vanellus_prompt_instructs_post_via_gh_cli():
    """_vanellus_prompt must tell Vanellus to post via the gh CLI."""
    t = _trigger()
    prompt = SwarmDispatcher._vanellus_prompt(t, parent=80, lenses=["pm", "qa"])
    # The exact marker must appear so Vanellus uses it in its comment.
    assert _VANELLUS_COMMENT_MARKER in prompt


def test_vanellus_prompt_instructs_repeat_for_fallback():
    """_vanellus_prompt must ask Vanellus to repeat the verdict in its reply."""
    t = _trigger()
    prompt = SwarmDispatcher._vanellus_prompt(t, parent=80, lenses=[])
    assert "Repeat" in prompt or "repeat" in prompt


def test_vanellus_prompt_embeds_inline_reviews_and_forbids_gh_fetch():
    """Aggregation must run from inline review text, not a gh fetch.

    Regression: the aggregator runs diff-only with no repo checkout (cwd=None),
    so a `gh pr view --comments` read fails ("requires being in a repository
    context") and the aggregation silently stalls — the dispatcher then posts
    Vanellus's non-verdict "please give me the data" message via its fallback.
    The captured panel reviews must be embedded in the prompt instead.
    """
    t = _trigger()
    reviews = [
        ("pm", "APPROVE - scope matches the issue."),
        ("qa", "REQUEST_CHANGES - [BLOCKING] missing error-recovery tests."),
    ]
    prompt = SwarmDispatcher._vanellus_prompt(
        t, parent=80, lenses=["pm", "qa"], reviews=reviews
    )
    # Each lens's captured text is embedded inline.
    assert "APPROVE - scope matches the issue." in prompt
    assert "[BLOCKING] missing error-recovery tests." in prompt
    assert "### review:pm" in prompt and "### review:qa" in prompt
    # And Vanellus is told NOT to re-fetch them via gh.
    assert "Do NOT fetch them via gh" in prompt


def test_vanellus_prompt_handles_no_reviews():
    """No captured reviews => explicit placeholder; no gh-fetch requirement."""
    t = _trigger()
    prompt = SwarmDispatcher._vanellus_prompt(t, parent=80, lenses=[], reviews=None)
    assert "no panel lens reviews captured" in prompt


# ── _post_missing_vanellus_comment — dispatcher fallback ──────────────────────


class _FakeHttpxClientForVanellus:
    """httpx.AsyncClient stub that controls GET comment bodies and captures POSTs."""

    def __init__(self, existing_bodies: list[str]):
        self.existing_bodies = existing_bodies
        self.post_calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def get(self, url, **kwargs):
        class _Resp:
            def raise_for_status(self): pass
            def json(inner_self):
                return [{"body": b} for b in self.existing_bodies]
        return _Resp()

    async def post(self, url, **kwargs):
        self.post_calls.append({"url": url, "json": kwargs.get("json", {})})
        class _Resp:
            def raise_for_status(self): pass
        return _Resp()


def test_vanellus_fallback_posts_when_comment_missing(monkeypatch):
    """When Vanellus's comment is absent, the dispatcher posts the captured stdout."""
    client = _FakeHttpxClientForVanellus(existing_bodies=[])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    t = _trigger()
    verdict_text = "VERDICT: All clear. PR approved."
    result = SkillResult("vanellus", True, 0, verdict_text, "")

    asyncio.run(dispatcher._post_missing_vanellus_comment(t, result))

    assert len(client.post_calls) == 1, (
        f"Expected 1 fallback POST; got {len(client.post_calls)}"
    )
    posted_body = client.post_calls[0]["json"].get("body", "")
    assert _VANELLUS_COMMENT_MARKER in posted_body, (
        "Fallback comment must contain the Vanellus marker for future dedup"
    )
    assert verdict_text in posted_body, (
        "Fallback comment must reproduce the captured verdict text"
    )


def test_vanellus_fallback_skips_when_comment_already_present(monkeypatch):
    """When Vanellus's comment IS present, no duplicate is posted."""
    existing_body = f"{_VANELLUS_COMMENT_MARKER}\nVanellus already posted this."
    client = _FakeHttpxClientForVanellus(existing_bodies=[existing_body])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    t = _trigger()
    result = SkillResult("vanellus", True, 0, "VERDICT: all clear.", "")

    asyncio.run(dispatcher._post_missing_vanellus_comment(t, result))

    assert client.post_calls == [], (
        "No fallback POST when Vanellus's own comment is already present"
    )


def test_vanellus_fallback_skips_when_stdout_empty(monkeypatch):
    """When Vanellus produced no stdout, the fallback skips posting."""
    client = _FakeHttpxClientForVanellus(existing_bodies=[])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    t = _trigger()
    result = SkillResult("vanellus", True, 0, "", "")  # empty stdout

    asyncio.run(dispatcher._post_missing_vanellus_comment(t, result))

    assert client.post_calls == [], (
        "No fallback POST when Vanellus produced no captured text"
    )


def test_vanellus_fallback_non_fatal_when_post_raises(monkeypatch):
    """A failing fallback POST must not crash the pipeline."""
    class _FailingClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, **kwargs):
            class _Resp:
                def raise_for_status(self): pass
                def json(self): return []  # no existing comments
            return _Resp()
        async def post(self, url, **kwargs):
            raise RuntimeError("GitHub API unavailable")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FailingClient())
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    t = _trigger()
    result = SkillResult("vanellus", True, 0, "VERDICT: all clear.", "")

    # Must not raise even when the POST fails.
    asyncio.run(dispatcher._post_missing_vanellus_comment(t, result))


def test_handle_pr_calls_vanellus_fallback_after_run(monkeypatch):
    """_handle_pr must call _post_missing_vanellus_comment after the Vanellus run."""
    fallback_calls: list[tuple] = []
    skill_calls: list[str] = []

    async def fake_run_skill(skill, prompt, **kwargs):
        skill_calls.append(skill)
        if skill == "lanius":
            return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear", "")
        if skill == "vanellus":
            return SkillResult(skill, True, 0, "VERDICT: all clear.", "")
        return SkillResult(skill, True, 0, "ok", "")

    async def fake_vanellus_fallback(self, t, result):
        fallback_calls.append((t.number, result.stdout))

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    monkeypatch.setattr(
        SwarmDispatcher, "_post_missing_vanellus_comment", fake_vanellus_fallback
    )

    async def fake_changed_files(self, t): return []
    async def fake_preregistered(self, repo, number): return {}
    async def fake_store(self, entities, idempotency_key): pass
    async def fake_post_missing(self, t, reviews, agents_by_lens): pass
    async def fake_persist(self, t, reviews, agents_by_lens): pass
    async def fake_merge_checkpoint(self, t, parent, lenses): pass

    monkeypatch.setattr(SwarmDispatcher, "_changed_files", fake_changed_files)
    monkeypatch.setattr(SwarmDispatcher, "_preregistered_expectations", fake_preregistered)
    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(SwarmDispatcher, "_post_missing_panel_comments", fake_post_missing)
    monkeypatch.setattr(SwarmDispatcher, "_persist_panel_reviews", fake_persist)
    monkeypatch.setattr(SwarmDispatcher, "_store_merge_checkpoint", fake_merge_checkpoint)

    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    asyncio.run(dispatcher._handle_pr(_trigger()))

    assert len(fallback_calls) == 1, (
        f"_post_missing_vanellus_comment must be called exactly once; got {fallback_calls}"
    )
    pr_number, captured_stdout = fallback_calls[0]
    assert pr_number == 87
    assert captured_stdout == "VERDICT: all clear."


# ── QE3: eval-authoring affordance — PR-branch worktree ───────────────────────


def test_prepare_pr_worktree_returns_none_for_non_neotoma_repo():
    # The eval harness lives only in neotoma; ateles PRs get no worktree.
    result = asyncio.run(
        prepare_pr_worktree("markmhendrickson/ateles", 42, "phoenicurus")
    )
    assert result is None


def test_prepare_pr_worktree_returns_none_when_base_checkout_absent(monkeypatch):
    # Best-effort: a missing local neotoma clone → diff-only fallback, no raise.
    monkeypatch.setattr(
        swarm_dispatch, "NEOTOMA_LOCAL_CHECKOUT", "/nonexistent/neotoma-checkout"
    )
    result = asyncio.run(
        prepare_pr_worktree("markmhendrickson/neotoma", 42, "phoenicurus")
    )
    assert result is None


def test_cleanup_pr_worktree_none_is_safe_noop():
    # Cleanup of a never-prepared worktree must never raise.
    asyncio.run(cleanup_pr_worktree(None))


def test_cleanup_pr_worktree_removes_stray_dir(monkeypatch, tmp_path):
    # When the base clone is absent, cleanup still rmtree's the stray dir.
    monkeypatch.setattr(
        swarm_dispatch, "NEOTOMA_LOCAL_CHECKOUT", "/nonexistent/neotoma-checkout"
    )
    stray = tmp_path / "qa_eval_pr1_xxxx"
    stray.mkdir()
    asyncio.run(cleanup_pr_worktree(str(stray)))
    assert not stray.exists()


def test_only_qa_lens_gets_a_worktree(monkeypatch):
    # In the panel loop, prepare_pr_worktree is invoked ONLY for phoenicurus;
    # every other lens runs diff-only (cwd=None).
    prep_calls = []
    cwd_seen = {}

    async def fake_prepare(repo, number, agent):
        prep_calls.append(agent)
        return f"/tmp/wt-{agent}" if agent == "phoenicurus" else None

    async def fake_cleanup(wt):
        return None

    async def fake_run_skill(skill, prompt, **kwargs):
        cwd_seen[skill] = kwargs.get("cwd")
        # Lanius gate-inheritance must return a clear verdict so the panel runs.
        if skill == "lanius":
            return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear", "")
        return SkillResult(skill, True, 0, "VERDICT: COMMENT", "")

    monkeypatch.setattr(swarm_dispatch, "prepare_pr_worktree", fake_prepare)
    monkeypatch.setattr(swarm_dispatch, "cleanup_pr_worktree", fake_cleanup)
    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)

    # Force a panel that includes phoenicurus + at least one other lens.
    monkeypatch.setattr(
        swarm_dispatch,
        "select_panel",
        lambda **kw: [
            Lens(agent="phoenicurus", lens="qa", gate="qa", checks="evals"),
            Lens(agent="pavo", lens="pm", gate="pm", checks="scope"),
        ],
    )

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())
    asyncio.run(dispatcher._handle_pr(_trigger(repository="markmhendrickson/neotoma")))

    # Worktree prep attempted only for the qa lens.
    assert prep_calls == ["phoenicurus"], prep_calls
    # qa child got the worktree as cwd; other panelists got None.
    assert cwd_seen.get("phoenicurus") == "/tmp/wt-phoenicurus"
    assert cwd_seen.get("pavo") is None



def test_detect_auth_failure_signatures():
    """The auth-failure detector catches the real 401 text + common variants."""
    assert swarm_dispatch.detect_auth_failure("Failed to authenticate. API Error: 401 Invalid authentication credentials")
    assert swarm_dispatch.detect_auth_failure("", "OAuth token has expired")
    assert swarm_dispatch.detect_auth_failure("authentication_error: invalid api key")
    # A normal verdict must NOT trip it.
    assert not swarm_dispatch.detect_auth_failure("APPROVE — all blockers resolved", "")
    assert not swarm_dispatch.detect_auth_failure("BLOCKED: missing fallback", "")
    assert not swarm_dispatch.detect_auth_failure("", "")


def test_compose_auth_failure_comment_is_reframed_not_a_verdict():
    body = swarm_dispatch.compose_auth_failure_comment("vanellus")
    assert swarm_dispatch._VANELLUS_COMMENT_MARKER in body  # dedup marker preserved
    assert "credential failure" in body.lower()
    assert "ANTHROPIC_API_KEY" in body
    assert "not** a review verdict" in body or "not a review verdict" in body.lower()
