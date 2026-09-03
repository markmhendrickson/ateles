"""Tests for shared tool_allowlist enforcement.

The central claims under test are behavioural, not cosmetic:

* a bypassing dispatch is reported as UNCONFINED even when an allowlist exists
  (because the CLI's permission flow returns "allow" at bypass before it ever
  reads the allow rules);
* a provider that cannot accept a restriction is reported as unsupported
  rather than silently running unconfined;
* an allowlist made entirely of capability-slot aliases is refused rather
  than shipped as a dispatch that cannot act.
"""

from __future__ import annotations

import pytest

from lib.daemon_runtime.tool_allowlist import (
    EnforcementMode,
    TokenKind,
    apply_to_claude_argv,
    classify_token,
    enforcement_mode,
    plan_enforcement,
    provider_supports_allowlist,
)

ENFORCE = EnforcementMode.ENFORCE
LOG_ONLY = EnforcementMode.LOG_ONLY


# ── token classification ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "token,kind",
    [
        ("Read", TokenKind.BUILTIN),
        ("Bash", TokenKind.BUILTIN),
        ("bash", TokenKind.BUILTIN),
        ("Bash(gh pr:*)", TokenKind.BUILTIN),
        ("Bash(git:*)", TokenKind.BUILTIN),
        ("WebSearch", TokenKind.BUILTIN),
        ("mcp__mcpsrv_neotoma__store", TokenKind.MCP),
        ("mcp__mcpsrv_neotoma__*", TokenKind.MCP),
        ("mcp__github_harness__*", TokenKind.MCP),
        ("*", TokenKind.WILDCARD),
    ],
)
def test_real_tokens_classify_as_usable(token, kind):
    assert classify_token(token) is kind


@pytest.mark.parametrize(
    "token",
    [
        "git", "subprocess", "github", "gh", "telegram", "neotoma_read",
        "neotoma_write", "gws_gmail", "gws_calendar_read", "filesystem_read",
        "btc_wallet_send_transfer",
        # Bare Neotoma tool names missing the mcp__ prefix read as real tools
        # to a human but match nothing in the CLI. This is the subtlest case.
        "store", "correct", "retrieve_entities",
        "", "   ",
    ],
)
def test_capability_slot_aliases_classify_as_alias(token):
    assert classify_token(token) is TokenKind.ALIAS


# ── the bypass interaction: the central finding ───────────────────────────────


def test_skip_permissions_defeats_allowlist_and_is_reported_as_such():
    """A bypassing dispatch must never be reported as confined.

    The CLI returns "allow" at the bypassPermissions branch BEFORE consulting
    allow rules, so passing --allowed-tools alongside
    --dangerously-skip-permissions produces argv that LOOKS restricted and
    behaves unrestricted.
    """
    plan = plan_enforcement(
        ["Read", "Bash"], provider="claude", skip_permissions=True, mode=ENFORCE
    )
    assert plan.status == "defeated"
    assert plan.applies is False
    assert plan.is_confined is False
    assert "no-op" in plan.reason


def test_bypass_is_checked_before_provider_and_alias_paths():
    """Bypass wins over every other verdict — it is the strongest fact."""
    plan = plan_enforcement(
        ["git", "subprocess"], provider="cursor", skip_permissions=True, mode=ENFORCE
    )
    assert plan.status == "defeated"


def test_without_bypass_the_same_allowlist_enforces():
    plan = plan_enforcement(
        ["Read", "Bash"], provider="claude", skip_permissions=False, mode=ENFORCE
    )
    assert plan.status == "enforced"
    assert plan.applies is True
    assert plan.is_confined is True


# ── provider parity ───────────────────────────────────────────────────────────


def test_only_claude_supports_a_tool_allowlist():
    assert provider_supports_allowlist("claude") is True
    assert provider_supports_allowlist("codex") is False
    assert provider_supports_allowlist("cursor") is False


@pytest.mark.parametrize("provider", ["codex", "cursor"])
def test_restrictive_allowlist_on_unconfining_provider_is_unsupported(provider):
    """The fallthrough must not be silent.

    Provider order is claude,codex,cursor with fallthrough on capacity/auth
    failure. Without this verdict the same agent with the same allowlist is
    confined or unconfined depending on quota headroom at dispatch time.
    """
    plan = plan_enforcement(["Read", "Bash"], provider=provider, mode=ENFORCE)
    assert plan.status == "unsupported"
    assert plan.applies is False
    assert plan.is_confined is False
    assert provider in plan.reason


@pytest.mark.parametrize("provider", ["codex", "cursor"])
def test_wildcard_agent_on_unconfining_provider_is_not_flagged(provider):
    """An agent that declares `*` loses nothing by running on codex/cursor."""
    plan = plan_enforcement(["*"], provider=provider, mode=ENFORCE)
    assert plan.status == "wildcard"


# ── alias allowlists would break agents ───────────────────────────────────────


def test_all_alias_allowlist_is_refused_not_shipped():
    """apus declares ["git","subprocess"]; formica declares ["github"].

    Passed to --allowed-tools these match nothing, so the agent could not act.
    Enforcement must refuse and name the tokens rather than ship a dead
    dispatch — and must not widen the grant to compensate.
    """
    plan = plan_enforcement(["git", "subprocess"], provider="claude", mode=ENFORCE)
    assert plan.status == "unenforceable"
    assert plan.applies is False
    assert sorted(plan.aliases) == ["git", "subprocess"]
    assert "git" in plan.reason and "subprocess" in plan.reason


def test_single_alias_allowlist_is_refused():
    plan = plan_enforcement(["github"], provider="claude", mode=ENFORCE)
    assert plan.status == "unenforceable"


def test_mixed_allowlist_enforces_usable_tokens_and_reports_aliases():
    """A partially-aliased grant still confines, but the aliases are named."""
    plan = plan_enforcement(
        ["Bash", "gh", "gws_gmail", "mcp__mcpsrv_neotoma__store"],
        provider="claude",
        mode=ENFORCE,
    )
    assert plan.status == "enforced"
    assert "gh" in plan.aliases and "gws_gmail" in plan.aliases
    assert "gh" not in plan.allowed
    assert "Bash" in plan.allowed


def test_bare_neotoma_names_are_aliases_not_grants():
    """pavo declares bare `store`/`correct` alongside prefixed forms."""
    plan = plan_enforcement(
        ["store", "correct", "mcp__mcpsrv_neotoma__store"],
        provider="claude",
        mode=ENFORCE,
    )
    assert plan.status == "enforced"
    assert "store" in plan.aliases
    assert "correct" in plan.aliases


# ── neotoma MCP is always reachable ───────────────────────────────────────────


def test_neotoma_mcp_wildcard_is_always_appended():
    plan = plan_enforcement(["Read"], provider="claude", mode=ENFORCE)
    assert "mcp__mcpsrv_neotoma__*" in plan.allowed


def test_neotoma_mcp_wildcard_is_not_duplicated():
    plan = plan_enforcement(
        ["Read", "mcp__mcpsrv_neotoma__*"], provider="claude", mode=ENFORCE
    )
    assert plan.allowed.count("mcp__mcpsrv_neotoma__*") == 1


# ── wildcard and absent ───────────────────────────────────────────────────────


def test_wildcard_agent_is_not_restricted():
    plan = plan_enforcement(["*"], provider="claude", mode=ENFORCE)
    assert plan.status == "wildcard"
    assert plan.applies is False


@pytest.mark.parametrize("tools", [None, [], ["  "]])
def test_absent_allowlist_defaults_to_wildcard_today(tools):
    """Documents the CURRENT fail-open default rather than endorsing it.

    `concierge` and `ops` have no allowlist; flipping this without
    provisioning them would break them, so the inversion is sequenced
    separately and exposed as a parameter.
    """
    plan = plan_enforcement(tools, provider="claude", mode=ENFORCE)
    assert plan.status == "wildcard"


@pytest.mark.parametrize("tools", [None, []])
def test_absent_allowlist_can_be_made_fail_closed(tools):
    plan = plan_enforcement(
        tools, provider="claude", mode=ENFORCE, absent_is_wildcard=False
    )
    assert plan.status == "unenforceable"
    assert plan.applies is False


# ── posture / mode ────────────────────────────────────────────────────────────


def test_log_only_computes_the_restriction_but_does_not_apply_it():
    plan = plan_enforcement(["Read", "Bash"], provider="claude", mode=LOG_ONLY)
    assert plan.status == "would_enforce"
    assert "Read" in plan.allowed          # what WOULD have been passed
    assert plan.applies is False           # but nothing is applied
    assert plan.is_confined is False       # and it must not read as confined


def test_off_mode_plans_nothing():
    plan = plan_enforcement(["Read"], provider="claude", mode=EnforcementMode.OFF)
    assert plan.status == "off"
    assert plan.applies is False


def test_default_mode_is_log_only_not_enforce_and_not_off():
    assert enforcement_mode({}) is EnforcementMode.LOG_ONLY


@pytest.mark.parametrize("raw", ["1", "true", "yes", "enforce", "ENFORCE"])
def test_env_can_enable_enforcement(raw):
    assert enforcement_mode({"ATELES_ENFORCE_TOOL_ALLOWLIST": raw}) is ENFORCE


@pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
def test_env_can_disable_enforcement(raw):
    assert enforcement_mode({"ATELES_ENFORCE_TOOL_ALLOWLIST": raw}) is EnforcementMode.OFF


def test_unrecognised_env_value_degrades_to_log_only():
    """A typo in a plist must not take a daemon down, nor silently enforce."""
    assert enforcement_mode({"ATELES_ENFORCE_TOOL_ALLOWLIST": "hunoz"}) is LOG_ONLY


# ── argv application ──────────────────────────────────────────────────────────


def test_argv_gets_allowed_tools_when_enforcing():
    plan = plan_enforcement(["Read"], provider="claude", mode=ENFORCE)
    argv = apply_to_claude_argv(["claude", "--print"], plan)
    assert "--allowed-tools" in argv
    assert argv[argv.index("--allowed-tools") + 1].startswith("Read")


@pytest.mark.parametrize("mode", [LOG_ONLY, EnforcementMode.OFF])
def test_argv_unchanged_when_not_enforcing(mode):
    plan = plan_enforcement(["Read"], provider="claude", mode=mode)
    assert apply_to_claude_argv(["claude", "--print"], plan) == ["claude", "--print"]


def test_argv_unchanged_under_bypass():
    """The argv must not carry a restriction the CLI will step over."""
    plan = plan_enforcement(
        ["Read"], provider="claude", skip_permissions=True, mode=ENFORCE
    )
    assert apply_to_claude_argv(["claude", "--print"], plan) == ["claude", "--print"]


def test_apply_does_not_mutate_input_argv():
    cmd = ["claude", "--print"]
    plan = plan_enforcement(["Read"], provider="claude", mode=ENFORCE)
    apply_to_claude_argv(cmd, plan)
    assert cmd == ["claude", "--print"]


# ── failure classes found in the live Neotoma inventory ───────────────────────
#
# A read-only sweep of all 40 agent_definition entities in Neotoma prod turned
# up four distinct ways an allowlist fails under enforcement. Each is pinned
# here against the real declared values so a future grant migration has a
# regression target.


@pytest.mark.parametrize(
    "agent,tools",
    [
        ("apis", ["neotoma_read", "neotoma_write", "neotoma_correct"]),
        ("turdus", ["neotoma_read", "neotoma_write", "gws_gmail"]),
        ("anthus", ["neotoma_read", "neotoma_write", "telegram"]),
        ("tyto", ["neotoma_read", "neotoma_write", "filesystem_read"]),
        ("formica", ["github"]),
        ("apus", ["git", "subprocess"]),
        ("sitta", ["neotoma_read", "neotoma_write"]),
    ],
)
def test_all_alias_agents_are_refused_rather_than_bricked(agent, tools):
    """Class 1: seven agents declare NOTHING but capability slots.

    These include the dispatcher (apis) and the mail daemon (turdus). Turning
    enforcement on without a grant migration would leave each unable to call a
    single tool, so the planner must refuse rather than ship the dispatch.
    """
    plan = plan_enforcement(tools, provider="claude", mode=ENFORCE)
    assert plan.status == "unenforceable", f"{agent} must not silently brick"
    assert plan.applies is False


@pytest.mark.parametrize(
    "agent,tools,lost",
    [
        ("sturnus", ["mcp__mcpsrv_neotoma__store", "gws_gmail", "gws_calendar"],
         ["gws_gmail", "gws_calendar"]),
        ("picus", ["mcp__mcpsrv_neotoma__store", "gws_gmail_read"], ["gws_gmail_read"]),
        ("sylvia", ["mcp__mcpsrv_neotoma__store", "gws_calendar", "telegram_notify"],
         ["gws_calendar", "telegram_notify"]),
        ("ploceus", ["Bash", "gh", "gws_gmail"], ["gh", "gws_gmail"]),
        ("nucifraga", ["WebSearch", "gws_calendar_read"], ["gws_calendar_read"]),
    ],
)
def test_partially_aliased_agents_keep_reads_and_lose_outward_actions(agent, tools, lost):
    """Class 2: the tokens that fail are disproportionately SEND capabilities.

    Neotoma reads survive; Gmail, calendar, and notification grants do not. A
    fail-closed enforcement would leave these agents reading normally and
    silently not acting — which presents as a data problem, not a permissions
    problem. The aliases are therefore reported explicitly so the failure is
    attributable.
    """
    plan = plan_enforcement(tools, provider="claude", mode=ENFORCE)
    assert plan.status == "enforced"
    assert sorted(plan.aliases) == sorted(lost)
    for token in lost:
        assert token not in plan.allowed


def test_miscased_mcp_server_is_syntactically_valid_but_matches_nothing():
    """Class 3: monedula declares `mcp__Claude_in_Chrome__*`.

    The live server is `claude-in-chrome`, so the real prefix is
    `mcp__claude-in-chrome__*`. The token is well-formed, so it classifies as
    MCP and passes through to --allowed-tools — where the CLI matches it
    against nothing. Classification cannot catch this; only a roster check
    against live server names can, which is why enforcement stays log-only
    until the grants are migrated.
    """
    assert classify_token("mcp__Claude_in_Chrome__navigate") is TokenKind.MCP
    assert classify_token("mcp__claude-in-chrome__navigate") is TokenKind.MCP

    plan = plan_enforcement(
        [
            "mcp__Claude_in_Chrome__navigate",
            "btc_wallet_send_transfer",
            "mcp__mcpsrv_neotoma__store",
        ],
        provider="claude",
        mode=ENFORCE,
    )
    # The wallet alias is caught; the mis-cased MCP token is not.
    assert plan.status == "enforced"
    assert plan.aliases == ["btc_wallet_send_transfer"]
    assert "mcp__Claude_in_Chrome__navigate" in plan.allowed


def test_sitta_string_encoded_allowlist_survives_the_loader():
    """Class 4: sitta stores tool_allowlist as a JSON *string*, not a list.

    AgentDefinition.tools already parses that shape, so enforcement receives
    two tokens rather than thirty characters. Pinned so a loader refactor
    cannot regress it into per-character iteration.
    """
    from lib.daemon_runtime.agent_loader import AgentDefinition

    definition = AgentDefinition(tool_allowlist='["neotoma_read", "neotoma_write"]')
    assert definition.tools == ["neotoma_read", "neotoma_write"]
    assert plan_enforcement(definition.tools, provider="claude", mode=ENFORCE).status == (
        "unenforceable"
    )
