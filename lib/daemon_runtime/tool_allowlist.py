"""Shared enforcement of an agent_definition's ``tool_allowlist``.

Why this module exists
----------------------
Before it, ``tool_allowlist`` was enforced in exactly ONE place —
``execution/daemons/apis/skill_runner.py`` — and every other path that spawns
an agent ignored it. An audit found three independent leaks:

1. **Provider asymmetry.** Only the ``claude`` adapter passed
   ``--allowed-tools``; ``codex`` and ``cursor`` received no restriction at
   all. Because the provider order is ``claude,codex,cursor`` with fallthrough
   on capacity/auth failure, the SAME agent with the SAME allowlist was
   confined or unconfined depending on quota headroom at dispatch time.
2. **Direct spawners.** Eight daemons build ``claude --print`` argv themselves
   and never consult the definition. Anthus is the sharpest case: it loads the
   agent_definition (to pin ``agent_definition_ref`` for provenance) and still
   never reads ``.tools``.
3. **``--dangerously-skip-permissions``.** Seven of those daemons pass it
   unconditionally.

The third is not merely additive — it *defeats* the first two. See
`SKIP_PERMISSIONS_DEFEATS_ALLOWLIST` below.

The CLI's permission ordering (verified, not assumed)
-----------------------------------------------------
Read directly out of the Claude Code CLI bundle (npm build, v2.x). The main
permission flow evaluates, in this exact order:

    1. DENY rules      -> return "deny"      (``--disallowed-tools``)
    2. ASK rules       -> return "ask"
    3. tool self-check -> may return "deny"/"ask"
    4. if mode == "bypassPermissions" -> return "allow"      <<<<
    5. ALLOW rules     -> return "allow"     (``--allowed-tools``)

Step 4 short-circuits step 5. So:

* ``--dangerously-skip-permissions`` makes ``--allowed-tools`` a **no-op**.
  Passing both is not "restricted plus convenience" — it is unrestricted, and
  it *looks* restricted in the argv and in the logs, which is worse than an
  honest wildcard.
* ``--disallowed-tools`` is evaluated at step 1 and therefore SURVIVES bypass
  mode. It is the only lever that still binds on a bypassing dispatch.

This is the single most consequential fact about enforcing an allowlist here,
and it is why `plan_enforcement` refuses to report a dispatch as confined when
bypass is in play.

Capability-slot aliases
-----------------------
Several agent_definitions declare tokens that are *capability slots* rather
than CLI tool names — ``git``, ``subprocess``, ``github``, ``neotoma_read``,
``gws_gmail``, bare ``store``/``correct``, and similar. Passed to
``--allowed-tools`` these match NOTHING, so an agent whose allowlist is
entirely aliases would be unable to act at all the moment enforcement is
switched on. That is latent today only because the paths those agents run on
never applied the allowlist.

`classify_token` names them, and `plan_enforcement` refuses to enforce a plan
whose allowlist would leave the agent with no usable tool — reporting
``unenforceable`` instead of silently shipping a broken dispatch.

Posture
-------
Enforcement is **opt-in and log-only by default**, matching the reasoning that
keeps ``checkout_drift`` advisory: a guard that breaks eight daemons is worse
than the gap it closes. See `enforcement_mode`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "ALIAS_TOKENS",
    "EnforcementMode",
    "SKIP_PERMISSIONS_DEFEATS_ALLOWLIST",
    "TokenKind",
    "ToolPlan",
    "apply_to_claude_argv",
    "build_claude_argv",
    "classify_token",
    "enforcement_mode",
    "log_tool_plan",
    "plan_enforcement",
    "provider_supports_allowlist",
]


# Step 4 of the CLI permission flow returns "allow" before step 5 reads the
# allow rules. Named as a constant so call sites read as an assertion about
# the CLI rather than as a magic boolean.
SKIP_PERMISSIONS_DEFEATS_ALLOWLIST = True


class EnforcementMode(str, Enum):
    """How hard `tool_allowlist` binds on a dispatch.

    OFF      — do not pass a restriction. Current behaviour everywhere except
               skill_runner's claude adapter.
    LOG_ONLY — compute the restriction and report what WOULD have been denied,
               but dispatch unrestricted. This is the default: it makes the
               blast radius of turning enforcement on measurable before it is
               load-bearing.
    ENFORCE  — actually pass the restriction to the provider.
    """

    OFF = "off"
    LOG_ONLY = "log_only"
    ENFORCE = "enforce"


_ENV_VAR = "ATELES_ENFORCE_TOOL_ALLOWLIST"


def enforcement_mode(env: dict[str, str] | None = None) -> EnforcementMode:
    """Read the enforcement posture from the environment.

    Defaults to LOG_ONLY — never OFF and never ENFORCE. LOG_ONLY costs
    nothing at runtime (the restriction is computed but not applied) and is
    what makes the eventual flip to ENFORCE an informed decision instead of a
    guess. An unrecognised value degrades to LOG_ONLY rather than raising:
    a typo in a plist must not take a daemon down.
    """
    raw = (env if env is not None else os.environ).get(_ENV_VAR, "").strip().lower()
    if raw in ("1", "true", "yes", "enforce"):
        return EnforcementMode.ENFORCE
    if raw in ("0", "false", "no", "off"):
        return EnforcementMode.OFF
    return EnforcementMode.LOG_ONLY


class TokenKind(str, Enum):
    """What one token in a `tool_allowlist` actually is."""

    BUILTIN = "builtin"      # Read, Edit, Bash, Bash(gh pr:*) ...
    MCP = "mcp"              # mcp__server__tool / mcp__server__*
    ALIAS = "alias"          # capability slot; matches no CLI tool
    WILDCARD = "wildcard"    # "*"


# Built-in Claude Code tool names. Scoped forms like ``Bash(gh pr:*)`` are
# recognised by stripping the parenthesised rule before lookup.
_BUILTIN_TOOLS = frozenset(
    {
        "agent", "bash", "bashoutput", "edit", "exitplanmode", "glob", "grep",
        "killshell", "notebookedit", "read", "slashcommand", "task",
        "todowrite", "webfetch", "websearch", "write",
    }
)

_MCP_RE = re.compile(r"^mcp__[A-Za-z0-9_.-]+__(?:\*|[A-Za-z0-9_.-]+)$")
_SCOPED_RE = re.compile(r"^([A-Za-z]+)\(.*\)$")

# Capability-slot vocabulary observed in live agent_definitions. Kept as data
# rather than inferred, so that adding a real tool name never silently
# reclassifies as an alias — anything unrecognised is reported as an alias
# anyway, and this set exists to make the common cases legible in reports.
ALIAS_TOKENS = frozenset(
    {
        "git", "subprocess", "github", "gh", "telegram", "telegram_notify",
        "neotoma_read", "neotoma_write", "neotoma_correct",
        "gws_gmail", "gws_gmail_read", "gws_calendar", "gws_calendar_read",
        "filesystem_read", "filesystem_write",
        "btc_wallet_preview_transfer", "btc_wallet_send_transfer",
    }
)


def classify_token(token: str) -> TokenKind:
    """Classify one allowlist token.

    Anything that is not a recognised built-in or a well-formed ``mcp__``
    name is an ALIAS — including bare Neotoma tool names such as ``store``
    that are missing the ``mcp__mcpsrv_neotoma__`` prefix. Those read as real
    tools to a human but match nothing in the CLI, which is exactly the
    failure mode this classification exists to surface.
    """
    text = (token or "").strip()
    if not text:
        return TokenKind.ALIAS
    if text == "*":
        return TokenKind.WILDCARD
    if _MCP_RE.match(text):
        return TokenKind.MCP
    scoped = _SCOPED_RE.match(text)
    bare = scoped.group(1) if scoped else text
    if bare.lower() in _BUILTIN_TOOLS:
        return TokenKind.BUILTIN
    return TokenKind.ALIAS


def provider_supports_allowlist(provider: str) -> bool:
    """Whether a provider can accept a per-dispatch tool restriction.

    Only ``claude`` can. This was established against each CLI's own help
    output:

    * ``claude``  — ``--allowed-tools`` / ``--disallowed-tools``. Yes.
    * ``codex``   — exposes a filesystem/network SANDBOX (``--sandbox``), not
      a tool allowlist. A sandbox constrains where a tool may write; it does
      not constrain WHICH tools exist. There is no per-dispatch equivalent.
    * ``cursor``  — ``--force``/``--approve-mcps`` only widen approval. No
      restriction flag.

    That is a finding, not a failure: two of the three providers genuinely
    cannot honour a tool_allowlist. `plan_enforcement` therefore reports a
    restrictive allowlist on codex/cursor as ``unsupported`` so the caller can
    refuse the dispatch rather than let it run unconfined and unremarked.
    """
    return provider == "claude"


@dataclass
class ToolPlan:
    """What enforcement would do for one dispatch, and whether it can.

    ``status`` is the single field callers branch on:

      ``enforced``      — a restriction is being applied (``allowed`` is set).
      ``would_enforce`` — LOG_ONLY: ``allowed`` is what WOULD have been passed.
      ``wildcard``      — the agent declares ``*``; nothing to restrict.
      ``unsupported``   — provider cannot accept a restriction (codex/cursor).
      ``defeated``      — bypass mode is on, so an allowlist would be a no-op.
      ``unenforceable`` — the allowlist is entirely capability-slot aliases;
                          enforcing it would leave the agent unable to act.
      ``off``           — enforcement disabled.
    """

    status: str
    allowed: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def applies(self) -> bool:
        """True only when argv should actually carry the restriction."""
        return self.status == "enforced"

    @property
    def is_confined(self) -> bool:
        """True when this dispatch is genuinely restricted.

        Deliberately NOT true for ``would_enforce`` — a log-only dispatch is
        unconfined, and reporting otherwise would recreate the exact lie this
        module exists to remove.
        """
        return self.status == "enforced"


# Always appended when a restriction is applied: every agent reaches Neotoma,
# and skill_runner injects this MCP server into the child's config. Omitting
# it would break every dispatch the moment enforcement is enabled.
_NEOTOMA_MCP_WILDCARD = "mcp__mcpsrv_neotoma__*"


def plan_enforcement(
    tools: list[str] | None,
    *,
    provider: str = "claude",
    skip_permissions: bool = False,
    mode: EnforcementMode | None = None,
    absent_is_wildcard: bool = True,
) -> ToolPlan:
    """Decide what confinement a dispatch should get, and say so honestly.

    ``tools`` is ``AgentDefinition.tools`` — ``["*"]`` means unrestricted.

    ``absent_is_wildcard`` mirrors today's fail-open default at
    ``agent_loader.py`` (a missing ``tool_allowlist`` becomes ``["*"]``).
    It is a parameter rather than a hardcoded assumption so the default can be
    inverted later without touching call sites — but flipping it is deliberately
    NOT done here: agents with no allowlist would break, and provisioning them
    is a prerequisite, not a side effect.

    The order of the checks below is load-bearing. Bypass is tested BEFORE the
    provider and alias checks because a bypassing dispatch is unconfined no
    matter what the allowlist says, and reporting anything else would be false.
    """
    mode = mode if mode is not None else enforcement_mode()

    tokens = [t.strip() for t in (tools or []) if t and t.strip()]
    if not tokens:
        tokens = ["*"] if absent_is_wildcard else []

    if mode is EnforcementMode.OFF:
        return ToolPlan("off", reason="enforcement disabled")

    if tokens == ["*"]:
        return ToolPlan("wildcard", reason="agent declares unrestricted tools")

    if not tokens:
        return ToolPlan(
            "unenforceable",
            reason=(
                "no tool_allowlist and absent_is_wildcard=False — the agent "
                "has no grants to enforce and would be unable to act"
            ),
        )

    aliases = [t for t in tokens if classify_token(t) is TokenKind.ALIAS]
    usable = [t for t in tokens if classify_token(t) is not TokenKind.ALIAS]

    # Bypass defeats the allowlist outright — say so rather than pass a
    # restriction that the CLI will step over at stage 4.
    if skip_permissions and SKIP_PERMISSIONS_DEFEATS_ALLOWLIST:
        return ToolPlan(
            "defeated",
            aliases=aliases,
            reason=(
                "--dangerously-skip-permissions bypasses the allow-rule check, "
                "so --allowed-tools would be a silent no-op on this dispatch"
            ),
        )

    if not provider_supports_allowlist(provider):
        return ToolPlan(
            "unsupported",
            aliases=aliases,
            reason=(
                f"provider {provider!r} accepts no per-dispatch tool "
                "restriction; a restrictive allowlist cannot be honoured here"
            ),
        )

    # An allowlist made entirely of capability-slot aliases would confine the
    # agent to nothing at all. Refuse rather than ship a dispatch that cannot
    # act — and name the tokens so the grant gets fixed instead of widened.
    if not usable:
        return ToolPlan(
            "unenforceable",
            aliases=aliases,
            reason=(
                "every token is a capability-slot alias that matches no CLI "
                f"tool ({', '.join(aliases)}); enforcing would leave the agent "
                "unable to act — fix the grant, do not widen it"
            ),
        )

    allowed = list(usable)
    if _NEOTOMA_MCP_WILDCARD not in allowed:
        allowed.append(_NEOTOMA_MCP_WILDCARD)

    if mode is EnforcementMode.LOG_ONLY:
        return ToolPlan(
            "would_enforce",
            allowed=allowed,
            aliases=aliases,
            reason="log-only: dispatch runs unrestricted",
        )

    return ToolPlan("enforced", allowed=allowed, aliases=aliases)


def apply_to_claude_argv(cmd: list[str], plan: ToolPlan) -> list[str]:
    """Append ``--allowed-tools`` to a claude argv when the plan enforces.

    Returns a NEW list; the input is not mutated. A non-enforcing plan returns
    the argv unchanged, so callers can apply this unconditionally.
    """
    if not plan.applies:
        return list(cmd)
    return [*cmd, "--allowed-tools", ",".join(plan.allowed)]


def log_tool_plan(log, *, role: str, provider: str, plan: ToolPlan) -> None:
    """Emit one line per dispatch recording what confinement actually applied.

    The level is chosen so the log distinguishes "confined" from "looked
    confined". A dispatch that cannot be confined is logged at WARNING even
    though nothing failed, because an unconfined privileged dispatch is
    exactly the condition the audit found nobody could see.
    """
    prefix = f"[tool_allowlist] {role} via {provider}:"

    if plan.status == "enforced":
        log.info(f"{prefix} CONFINED to {len(plan.allowed)} tool(s): "
                 f"{','.join(plan.allowed)}")
    elif plan.status == "would_enforce":
        log.info(
            f"{prefix} LOG-ONLY — running UNCONFINED. Enforcement would have "
            f"restricted to {len(plan.allowed)} tool(s): {','.join(plan.allowed)}. "
            f"Set {_ENV_VAR}=1 to enforce."
        )
    elif plan.status == "wildcard":
        log.info(f"{prefix} unrestricted (agent declares '*')")
    elif plan.status == "off":
        log.debug(f"{prefix} enforcement disabled")
    else:
        # defeated / unsupported / unenforceable — all mean the dispatch runs
        # with more privilege than the definition asks for.
        log.warning(f"{prefix} NOT CONFINED [{plan.status}] — {plan.reason}")

    if plan.aliases:
        log.warning(
            f"{prefix} {len(plan.aliases)} capability-slot alias(es) in "
            f"tool_allowlist match no CLI tool and grant nothing: "
            f"{','.join(plan.aliases)}. Fix the grant; do not widen it."
        )


def build_claude_argv(
    cmd: list[str],
    tools: list[str] | None,
    *,
    log,
    role: str,
    skip_permissions: bool,
    prompt: str | None = None,
) -> list[str]:
    """Build a ``claude --print`` argv that applies ``tools`` and says what it did.

    This is the entry point for the daemons that construct their own argv
    rather than going through ``skill_runner``. It exists so those call sites
    converge on ONE implementation: this codebase already carries four
    divergent copies of a gate set, two of them wrong, and copying the
    enforcement logic eight times is precisely how that happened.

    ``skip_permissions`` is passed rather than inferred, because it changes the
    verdict: with bypass on, an allowlist cannot bind (the CLI returns "allow"
    at the bypass branch before reading allow rules), so nothing is appended
    and the log records the dispatch as unconfined instead of pretending
    otherwise.

    ``prompt``, when given, is appended LAST — after every flag — because
    ``--allowed-tools`` is variadic and would otherwise swallow a trailing
    positional prompt as another tool name.
    """
    plan = plan_enforcement(
        tools, provider="claude", skip_permissions=skip_permissions
    )
    argv = apply_to_claude_argv(cmd, plan)
    log_tool_plan(log, role=role, provider="claude", plan=plan)
    if skip_permissions:
        argv = [*argv, "--dangerously-skip-permissions"]
    if prompt is not None:
        argv = [*argv, prompt]
    return argv
