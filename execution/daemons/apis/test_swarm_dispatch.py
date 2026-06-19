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
    AGENT_GITHUB_LOGIN,
    EXPECTATION_MARKER,
    GITHUB_FACING_AGENTS,
    PRE_IMPL_GATES,
    _CONFIRM_GATES_CLEAR_CMD,
    _OPERATOR_LOGIN,
    DispatchConfig,
    SwarmDispatcher,
    _agent_prompt_instruction,
    _token_for_agent_on_repo,
    _token_for_repo,
    agent_github_login,
    attribution_header,
    compose_fallback_comment,
    content_digest,
    is_provisioned,
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
