"""
execution/daemons/apis/skill_runner.py — spawn a T4 agent via a bundled-plan CLI.

Single implementation of the spawn pattern previously inlined in apis.py. A
quota-aware router selects among Claude Code, Codex, and Cursor Agent, all using
the operator's subscription login; usage-based API credentials are removed from
the child environment unless the operator explicitly enables metered fallback.
The GitHub trigger pipelines (swarm_dispatch.py) reuse this implementation and
capture agent output for the review learning loop.

Stage 1 (ateles#94): loads the dispatched role's agent_definition from Neotoma
so the spawned subprocess gets the role's canonical system prompt prepended to
SKILL.md, and (when the definition specifies a restricted tool_allowlist) passes
--allowed-tools to confine the subprocess.

Stage 2 (ateles#94): writes a harness_event to Neotoma at dispatch start,
completion, and failure.

Stage 5 (ateles#94): when no agent_definition loads (empty prompt_markdown),
emits a notifier WARN and a harness_event with the degraded_generic_subagent
marker so degraded dispatches are observable. Dispatch still proceeds.

ateles#257: on a FAILED dispatch the complete child stdout AND stderr are
persisted to a per-dispatch file under ``~/Library/Logs/ateles/dispatch-failures/``
and that path is echoed into both the ERROR log line and the harness_event
``output_summary``, so a failure is never again unreconstructable from a
truncated slice. Failed dispatches also raise a rate-limited operator
notification, so a swarm-wide breakage produces a signal instead of silence.

Failures never raise — callers get a SkillResult and decide how to degrade;
one bad dispatch must not take down the daemon.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# ── Path bootstrap (mirrors apis.py so this module is importable standalone) ──
_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib.credential_scrub import (  # noqa: E402
    AGENT_CHILD_MARKER_ENV,
    is_generation_credential,
)
from lib.daemon_runtime import AgentDefinition, AgentLoader  # noqa: E402
from dispatch_usage import DispatchUsage, parse_dispatch_usage  # noqa: E402
from foundation import (  # noqa: E402
    SWARM_FOUNDATION_CONTRACT,  # noqa: F401 — re-exported beside the sibling contracts
    foundation_contract,
)
import local_provider  # noqa: E402
import model_tiering  # noqa: E402
from harness_router import (  # noqa: E402
    cool_down,
    cooled_until_all,
    cooling_providers,
    provider_candidates,
    provider_exclusion_reason,
    record_cooling,
    render_wall,
    usable_provider_names,
    usage_gate_refusals_all,
)
from limit_reset import parse_refusal  # noqa: E402
from usage_probe import refresh_usage_if_stale  # noqa: E402

# Cloudflare fronts the hosted Neotoma instance and blocks urllib's default
# User-Agent with a 1010 "browser signature" 403. Any explicit UA passes.
NEOTOMA_USER_AGENT = "ateles-neotoma-sync/1.0"

log = logging.getLogger("apis.skill_runner")

CLAUDE_BIN = os.environ.get("APIS_CLAUDE_BIN") or shutil.which("claude")
_CODEX_APP_BIN = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
CODEX_BIN = (
    os.environ.get("APIS_CODEX_BIN")
    or (str(_CODEX_APP_BIN) if _CODEX_APP_BIN.is_file() else None)
    or shutil.which("codex")
)
CURSOR_BIN = os.environ.get("APIS_CURSOR_BIN") or shutil.which("cursor-agent")
TRUSTED_MACOS_SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
DISPATCH_TIMEOUT_SECONDS = int(os.environ.get("APIS_DISPATCH_TIMEOUT", "1800"))


async def _kill_spawned_process_group(proc: asyncio.subprocess.Process) -> None:
    """Kill and drain exactly the process group created for one provider run.

    Provider CLIs commonly launch a native child. Killing only the immediate
    wrapper can orphan that child with the stdout/stderr pipes still open,
    which makes the timeout path's follow-up ``communicate()`` hang forever.
    Every caller launches with ``start_new_session=True``, so the wrapper PID
    is also the exact process-group ID; no process-name or broad PID matching
    is involved.
    """
    pid = getattr(proc, "pid", None)
    if isinstance(pid, int) and hasattr(os, "killpg"):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        proc.kill()
    await proc.communicate()


ATELES_REPO = Path(
    os.environ.get("ATELES_REPO_PATH", str(Path.home() / "repos" / "ateles"))
)

# ── Dropped-allowlist-rule detection (ateles#255) ──────────────────────────────
# The CLI silently continues past a rejected `--allowedTools` rule, logging a
# single stderr line per dropped rule instead of failing the dispatch. Before
# this, that line was only visible in ~/Library/Logs/ateles/apis.log — a
# swarm-wide breakage (see issue #255) went unnoticed for a week because
# nothing surfaced it to the operator. This regex + helper turn every dropped
# rule into one batched notifier alert per dispatch (never one alert per rule,
# to avoid paging noise on a single bad grant).
#
# The CLI line-wraps this message (confirmed in the issue's own quoted repro):
#   "... dispatch failed (rc=1): Ignoring\n--allowedTools rule \"pr*\": ..."
# — a newline, not a space, separates "Ignoring" and "--allowedTools". `\s+`
# (DOTALL not needed; \s already matches \n) tolerates that wrap so the
# detector actually matches real CLI output, not just a single-line fixture.
_DROPPED_ALLOWLIST_RULE_RE = re.compile(r'Ignoring\s+--allowedTools rule "([^"]*)"')

# ── Gate-writeback tool grant (ateles#795) ────────────────────────────────────
# The Neotoma tools a gate-owning review lens is pre-approved for READ-ONLY
# access to, so it can look up and read back the parent issue's `gate_status`
# for its own situational awareness.
#
# HISTORY, and why `correct` is NO LONGER in this tuple. A panelist seated
# because it owns a pending pre-impl gate used to be instructed to `correct()`
# `gate_status.<lens>` on the parent issue entity itself, pre-approved via this
# same allowlist. On PR #791 that write was DENIED by the LOCAL harness (a
# headless `claude --print` child runs in `default` permission mode, where an
# MCP write tool it was not explicitly granted raises an approval prompt a
# non-interactive child cannot answer), so this tuple originally added
# `mcp__mcpsrv_neotoma__correct` to pre-approve exactly that write.
#
# The operator's amended ADR on ateles#795 moved the system-of-record write
# off this path entirely: `gate_waive.IssueGateStore.sign_off`, called by the
# DISPATCHER after a clean lens verdict, signs the write with the LENS's own
# AAuth keypair — never this MCP session's shared daemon bearer. Leaving
# `mcp__mcpsrv_neotoma__correct` pre-approved here left an unattributed sibling
# path wide open: every lens session presents the SAME shared bearer over MCP
# (see `neotoma_token_for_agent`'s Tier-2 fallback), so a pre-approved
# `correct()` on `gate_status` from ANY seated lens could clear a gate with no
# lens AAuth signature at all — and `sign_off`'s own idempotent no-op on an
# already-cleared gate (see `gate_waive.sign_off`) would then read that
# shared-bearer write back as a verified success. Removing `correct` from this
# allowlist closes that sink: an agent MCP `correct()` of `gate_status` is no
# longer pre-approved at the tool-permission layer, so it falls back to the
# same `default`-mode approval prompt a non-interactive child cannot answer —
# best-effort at most, never a clearance path (Falco's security review on PR
# #1181, ateles#795 amended ADR).
#
# The two retrieve tools remain pre-approved: they are read-only situational
# awareness (what does `gate_status` currently say), not a system-of-record
# mutation, so pre-approving them carries none of the attribution risk above.
GATE_WRITEBACK_TOOLS: tuple[str, ...] = (
    "mcp__mcpsrv_neotoma__retrieve_entity_by_identifier",
    "mcp__mcpsrv_neotoma__retrieve_entity_snapshot",
)


def gate_writeback_allowlist(tools: list[str]) -> list[str]:
    """Extend *tools* with the gate-writeback tools, preserving order.

    Additive by construction: the agent's own allowlist is never rebuilt or
    reordered, only extended with entries it lacks. Rebuilding a grant list from
    a partial view is how ateles#762 dropped grants in the first place.
    """
    merged = list(tools)
    for tool in GATE_WRITEBACK_TOOLS:
        if tool not in merged:
            merged.append(tool)
    return merged


# ── Deny the sibling attribution-bypass sink (Falco's REQUEST_CHANGES, PR #1181) ──
# `GATE_WRITEBACK_TOOLS` is additive — it never carried `correct`, but an
# ADDITIVE allowlist cannot un-grant a tool the agent already has through the
# `mcp__mcpsrv_neotoma__*` wildcard (every claude dispatch gets this wildcard;
# see the `--mcp-config` block below) or through `tools == ["*"]` (every tool,
# unconditionally). Removing `correct` from `GATE_WRITEBACK_TOOLS` was
# necessary but not sufficient: the wildcard/`*` path still pre-approves
# `mcp__mcpsrv_neotoma__correct` for a gate-owning lens exactly as before,
# which is the identical shared-bearer attribution-bypass sink `sign_off` was
# built to close (Falco's finding `ent_f276d2310e1705a7bbbe1fd0`, reconfirmed
# at commit `104867d`).
#
# This is the actual close: for a run that OWNS a pending pre-impl gate
# (`owns_pending_gate=True` — every run whose clean verdict `sign_off` records,
# i.e. the PR panel and the Phase-1 issue-spec pm turn), `correct` is placed on
# the CLI's DENY list, which takes precedence over any allow entry including a
# wildcard. This is a subtractive control, not an additive grant, so it cannot
# be defeated by the same class of gap that made the additive fix incomplete.
#
# Since the dispatcher security run at e874537f (BLOCKING
# `incomplete_class_sweep`), the deny also covers EVERY seated reviewer
# (`run_skill(seated_reviewer=True)`), not only a run that owns a pending gate:
# an advisory seat, or a gate owner re-seated after its gate cleared, held the
# same wildcard over the same shared bearer.
GATE_OWNER_DENIED_TOOLS: tuple[str, ...] = ("mcp__mcpsrv_neotoma__correct",)


# ── Per-agent Neotoma credential (ateles#795) ─────────────────────────────────
# A lens that owns a pre-impl gate is INSTRUCTED to record its own verdict by
# `correct()`-ing the parent issue entity. Neotoma admits that write against the
# `agent_grant` matched on the principal the CALLER presents — so the child must
# present the LENS's credential, or the write is refused however correct the
# review was.
#
# It does not. The child reaches Neotoma over HTTP MCP, and the Authorization
# header on that connection carries one process-wide `NEOTOMA_BEARER_TOKEN`
# (see the `--mcp-config` block in `_run_skill_once`). Every lens therefore
# presents the SAME principal — the daemon's — and a lens whose own grant names
# `issue` never gets to use it, because the grant matched is not its own.
#
# The per-agent AAuth keypairs under ATELES_PRIVATE_KEYS_DIR do not close this.
# They are injected as NEOTOMA_AAUTH_* env vars, which are read by the
# TypeScript client signer (`lib/daemon_runtime/neotoma_signed.py` shells out to
# it per request). An MCP HTTP session authenticates once, with a static header,
# so a per-request signer cannot supply its identity. The keys are real and the
# vars are set; they are simply not on this path.
#
# Consequence, and the reason ateles#795 stayed open after #762/#769 filed the
# grants: the lens reviews, is refused, and `gate_status.<lens>` stays
# `pending` — INDISTINGUISHABLE from a review that never ran. Nothing fails,
# nothing logs at ERROR, and the gate merely looks outstanding.
#
# This resolves a per-agent bearer on the GitHub pattern already established by
# `_token_for_agent_on_repo`: an agent-specific env name, falling back to the
# shared daemon token. The fallback is deliberate — an advisory lens that owns
# no gate still produces its whole product (a PR comment) unsigned, so degrading
# there costs nothing. A gate OWNER's product is a durable write, so for that
# case the caller refuses instead (`gate_writeback_identity_error`), per
# CLAUDE.md "fail closed on the field that carries the safety meaning".
NEOTOMA_IDENTITY_UNAVAILABLE = "per-agent Neotoma identity unavailable"

# ateles#795, Falco's REQUEST_CHANGES on PR #1181: the public-safe error-class
# prefix for a gate-owning run refused because its provider cannot deny a
# single MCP tool (see the `owns_pending_gate and provider != "claude"` check
# in `_run_skill_once`). Mirrors `NEOTOMA_IDENTITY_UNAVAILABLE`'s shape so
# `swarm_dispatch.review_failure_class` can match on it the same way.
GATE_OWNER_TOOL_DENY_UNAVAILABLE = "gate-owner tool-deny unavailable on provider"


def neotoma_token_env_name(role: str) -> str:
    """Env var holding *role*'s own Neotoma bearer, e.g. `ACCIPITER_NEOTOMA_TOKEN`."""
    return f"{role.upper().replace('-', '_')}_NEOTOMA_TOKEN"


def neotoma_token_for_agent(role: str) -> tuple[str, bool]:
    """Resolve (token, is_own_identity) for *role*'s Neotoma calls.

    Tier 1 — `<ROLE>_NEOTOMA_TOKEN`: the agent's own principal. `is_own_identity`
             is True only here.
    Tier 2 — `NEOTOMA_BEARER_TOKEN`: the shared daemon bearer, preserving exact
             current behaviour for every agent that has no credential of its own.

    Returning the tier alongside the token is the point: a caller that needs the
    write ATTRIBUTED (a gate writeback) must be able to tell the two apart, and a
    bare token string cannot say which principal it speaks for.
    """
    own = os.environ.get(neotoma_token_env_name(role), "").strip()
    if own:
        return own, True
    return os.environ.get("NEOTOMA_BEARER_TOKEN", ""), False


def gate_writeback_identity_error(role: str, *, is_own_identity: bool) -> str | None:
    """Why *role* cannot record its own gate verdict, or None when it can.

    Pure, so the preflight is testable without a subprocess. Only a gate OWNER
    calls this; an advisory lens runs on the shared bearer exactly as before.
    """
    if is_own_identity:
        return None
    return (
        f"{NEOTOMA_IDENTITY_UNAVAILABLE}: '{role}' owns a pre-impl gate and must "
        f"`correct()` the parent issue entity to record its verdict, but no "
        f"{neotoma_token_env_name(role)} is set, so it would present the shared "
        "daemon bearer instead of its own principal. Neotoma matches the "
        f"agent_grant on the caller's principal, so the write would be refused "
        "and the gate would stay `pending` — indistinguishable from a review "
        f"that never ran (ateles#795). Provision {neotoma_token_env_name(role)} "
        f"for '{role}@ateles-swarm' and file an agent_grant carrying retrieve + "
        "correct on `issue`."
    )


def _require_neotoma_base_url() -> str:
    """Return NEOTOMA_BASE_URL (trailing slash stripped) or raise.

    No localhost default by design — see the 2026-08-04 migration off local
    hosting; a fallback would silently target a dead port.
    """
    v = os.environ.get("NEOTOMA_BASE_URL", "").strip()
    if not v:
        raise RuntimeError(
            "NEOTOMA_BASE_URL is not set. It must point at the Neotoma instance (e.g. https://neotoma.markmhendrickson.com). Local hosting was retired 2026-08-04 and http://localhost:9180 no longer serves anything, so there is deliberately no default: a silent fallback would send writes at a dead port. Under launchd the plist supplies this; for an ad-hoc run, export it or source ~/.config/neotoma/.env first."
        )
    return v.rstrip("/")


def _find_dropped_allowlist_rules(stderr: str) -> list[str]:
    """Return the distinct dropped-rule names named in a dispatch's stderr.

    Order-preserving, de-duplicated (the same rule can be logged more than
    once for a single dispatch). Empty input / no match -> empty list.
    """
    if not stderr:
        return []
    seen: dict[str, None] = {}
    for m in _DROPPED_ALLOWLIST_RULE_RE.finditer(stderr):
        seen.setdefault(m.group(1), None)
    return list(seen)


def _notify_dropped_allowlist_rules(
    notifier, *, role: str, rules: list[str], returncode: int | None
) -> None:
    """Send ONE batched notifier alert naming every dropped rule for this
    dispatch (never one alert per rule — see module docstring above)."""
    if not rules or notifier is None:
        return
    rule_list = ", ".join(f'"{r}"' for r in rules)
    msg = (
        f"Agent {role!r} dispatch had {len(rules)} --allowedTools rule(s) "
        f"silently dropped by the CLI: {rule_list} (rc={returncode}). "
        "The corresponding tool_allowlist grant(s) never reached the agent — "
        "fix the grant grammar in the agent_definition."
    )
    try:
        from lib.notify import Priority

        notifier.send(msg, priority=Priority.WARN, handler="apis")
    except Exception as exc:
        log.debug(f"[apis] dropped-allowlist-rule notifier.send failed: {exc}")


# ── Agent-definition cache ─────────────────────────────────────────────────────
# Per-role cache within the process lifetime. AgentLoader.load() makes a
# synchronous HTTP call to Neotoma; caching avoids refetching on every task
# dispatch for the same role.
_agent_def_cache: dict[str, AgentDefinition] = {}


def _load_agent_def(role: str) -> AgentDefinition:
    """Load (and cache) an AgentDefinition for the given role name."""
    if role not in _agent_def_cache:
        _agent_def_cache[role] = AgentLoader(role).load()
    return _agent_def_cache[role]


def _load_active_policy_prompt(role: str) -> str:
    """Render the role's live Neotoma policy without caching it locally."""
    try:
        rendered = AgentLoader(role).render_policy_prompt()
        return rendered if isinstance(rendered, str) else ""
    except Exception as exc:  # noqa: BLE001 — policy outage must not kill dispatch
        log.warning(f"[apis] live policy unavailable for role {role!r}: {exc}")
        return ""


# ── Review-verdict vocabulary (ateles#938 — one source, defined once) ─────────
# THE single authoritative list of bold verdict tokens a swarm agent may emit as
# its one-line GitHub verdict. Every other place that needs this vocabulary —
# the contract text below, the regression test, and
# `swarm_dispatch._REVIEW_VERDICT` (the parser that reads the token back off
# Vanellus's aggregation) — imports THIS tuple rather than hand-typing its own
# copy. That is the fix for ateles#938: the dispatcher instructed `SIGNED_OFF`
# in this very contract (see the "Verdict vocabulary" section immediately
# below) while its parser's regex, maintained separately, never learned the
# fifth token — a correctly-formed sign-off therefore parsed as unparseable and
# escalated to the operator instead of clearing the panel.
#
# `SIGNED_OFF` is NOT a synonym for `APPROVE`. It certifies a narrower, prior
# claim: that a gate-owning lens's `gate_status` write landed, confirmed by an
# immediate read-back — as of ateles#795's amended ADR that write is the
# DISPATCHER's own lens-AAuth-signed `gate_waive.IssueGateStore.sign_off`
# (called from `swarm_dispatch._run_pr_review_panel` and
# `_run_issue_spec_pipeline`), never the lens's own in-session `correct()`.
# `APPROVE` certifies a judgement about the
# PR as a whole. Collapsing the two would let a durable-write confirmation
# stand in for a merge authorisation it never made — so `SIGNED_OFF` maps to
# the inert GitHub `COMMENT` event, never `APPROVE` (see
# `swarm_dispatch.verdict_to_review_event`), and a `SIGNED_OFF` verdict is
# still `review_verdict_is_clear` (it does not block the merge path or route
# findings back) without being treated as approval.
#
# Extend this tuple — never a second regex or a second hand-typed list — when
# the swarm needs a new verdict token. Both call sites below are keyed off it,
# and `test_swarm_dispatch.py::test_instructed_review_verdict_tokens_subseteq_parser`
# fails CI the moment a token here and the parser regex disagree.
REVIEW_VERDICT_TOKENS: tuple[str, ...] = (
    "APPROVE",
    "REQUEST_CHANGES",
    "COMMENT",
    "BLOCKED",
    "SIGNED_OFF",
)

# The one sentence every gate-verdict prompt uses to state where the
# dispatcher reads a verdict (`swarm_dispatch.lens_own_verdict`: a fixed
# position, independent security run at 8f51ffc2 on PR #1181). The contract
# below and `swarm_dispatch.gate_verdict_instruction` both render it from
# here, so the prompts cannot state two different rules.
GATE_VERDICT_POSITION_RULE = (
    "the FIRST line of your reply must be your header; the second line your verdict"
)


# ── Shared GitHub-interaction convention (Phase 1 / Layer A) ──────────────────
# Injected into every GitHub-dispatched agent's system prompt by build_system_prompt
# when include_github_contract=True.  Lives in ONE place — not duplicated across
# agent_definitions.  Complements (never contradicts) per-prompt format instructions
# already present in swarm_dispatch.py prompts.
#
# See docs/swarm_github_interaction_design.md — Layer A.

_VERDICT_VOCABULARY_LINES = {
    "APPROVE": "all checks pass, no blockers.",
    "REQUEST_CHANGES": "one or more [BLOCKING] findings; the author must address them.",
    "COMMENT": "observations only; nothing blocks merge.",
    "BLOCKED": "cannot proceed (missing information, open pre-impl gate, etc.).",
    "SIGNED_OFF": "your gate/phase is signed off.",
}

# Rendered from REVIEW_VERDICT_TOKENS so the contract text an agent reads can
# never enumerate a token the parser (built from the same tuple, see
# swarm_dispatch._REVIEW_VERDICT) does not accept.
_VERDICT_VOCABULARY_BLOCK = "\n".join(
    f"- `**{token}**` — {_VERDICT_VOCABULARY_LINES[token]}"
    for token in REVIEW_VERDICT_TOKENS
)

SWARM_GITHUB_CONTRACT = """\
## Swarm GitHub interaction contract (Layer A)

Every GitHub comment you post as part of the Ateles swarm MUST follow this convention.

### Attribution header — exact, verbatim form

Every comment MUST open with this header (bold, em-dash, literal "Ateles swarm,"):

```
**🤖 <AgentNameTitleCase> — Ateles swarm, <role-phrase>**
```

Rules — read carefully:

- **Agent name in Title Case** — e.g. `Pavo`, `Cicada`, `Lanius`. Never lowercase.
- **Em-dash** (—, U+2014), not a hyphen (-) or double-hyphen (--).
- **"Ateles swarm,"** is literal — include the comma, no variations.
- **`<role-phrase>`** is YOUR fixed role label used consistently every time \
(e.g. "pm gate owner", "issue triage", "arch reviewer"). Do not vary it between comments.
- **Do NOT append** `· <repo>#<n>` or any issue/repo suffix to the header. \
The repository and issue context are already visible from where the comment lives; \
appending them caused the inconsistency observed in neotoma#1686 (Pavo posted two \
different header forms in the same thread). Drop that suffix entirely.

**Reproduce this header format EXACTLY on every comment — same capitalization, same \
em-dash, same "Ateles swarm," prefix. Do not add repository/issue suffixes or restyle it.**

Per ateles#109: when posting under your own dedicated provisioned account (avatar is \
attribution), the header MAY be omitted. When included, it MUST be the exact form above.

**Gate verdicts are read from your header only.** When you own a gate, the dispatcher \
reads your verdict from a fixed position in the reply you return: \
{gate_verdict_position_rule}. That holds on every account, including a dedicated one; \
at most one `<!-- review:<lens> commit=<sha> -->` marker line may come before the \
header, and everything else goes after the verdict line. A first line that is not your \
header, a second line that is not your verdict, a second header, or a second verdict \
line anywhere leaves the gate pending. Never reproduce an earlier comment's header \
or verdict line, quoted or not.

**A gate-verdict comment's attribution (header, or avatar on a dedicated account) is \
already its attribution — omit the generic harness footer on it.** Your harness may \
separately instruct you to close every reply with a "Generated with…" / \
`Co-Authored-By:` trailer. \
That trailer exists to mark AI-authored content when nothing else on the comment does, \
and on a comment carrying a `<!-- review:<lens> commit=<sha> -->` marker something else \
already does that job — the attribution header above on a shared account, or the \
dedicated account's own avatar per the ateles#109 carve-out just above. Either way, do \
not also append the generic trailer to that comment (ateles#1326: on a shared account \
the trailer repeats the header's robot emoji, which reads to the dispatcher as a second \
header on an otherwise valid sign-off). This never leaves a gate-verdict comment with NO \
attribution at all — it always has one of the two. This applies only to gate-verdict \
comments; a commit message or PR description still gets the generic trailer as normal.

### Verdict line — exact, verbatim form

Immediately after the attribution header, on its own line:

```
**<VERDICT>**
```

### Verdict vocabulary

Use exactly ONE of these tokens as the bold status line — one per comment, always present:

{verdict_vocabulary_block}

### Worked example — reproduce this pattern exactly

```
**🤖 Pavo — Ateles swarm, pm gate owner**
**APPROVE**

- [x] Acceptance criteria met
- [x] No open blockers

PM gate signed off. Ready to merge.

---
📎 Neotoma: [neotoma#1686](https://neotoma.markmhendrickson.com/entities/ent_abc123)
```

### Checklists

All definition-of-done checklists use GitHub task-list syntax:

```
- [ ] Not yet verified
- [x] Confirmed satisfied
```

### Blocking markers

Prefix each finding with its severity so the aggregator and humans can parse uniformly:

```
[BLOCKING] <category>: <summary>
[NON-BLOCKING] <category>: <summary>
```

### Cite standing rules

When a finding rests on a guardrail, decision, or doc, say so explicitly — that marks \
it as systemic, not opinion. Link the Neotoma record when it is publicly readable (see \
Neotoma backlinks below).

### Edit, don't duplicate

Update your prior comment in place rather than posting a new one when you are revisiting \
the same issue or PR. Use `gh api -X PATCH repos/<owner>/<repo>/issues/comments/<id> \
-f body='...'` to edit.

### PR review head and supersession

Every PR verdict is scoped to the exact artifact it reviewed. Put the full current
40-hex head SHA in ONE HTML marker line at the start of the comment, above the header:
the first form for a lens review, the second for a Vanellus aggregation, never both.

```
<!-- review:<lens> commit=<full40hex> -->
<!-- vanellus-aggregation commit=<full40hex> -->
```

The marker is authoritative. A prose `Reviewed commit:` line is only for readers and
is ignored by the dispatcher. A missing, malformed, or different SHA never stands for
the current head. When a new head supersedes a bot verdict, the dispatcher may PATCH
that prior comment in place with a visible superseded banner; preserve that history and
write the new verdict against the new full SHA.

### Neotoma backlinks

Every comment that references or is sourced by canonical Neotoma data MUST link the \
relevant record(s) in a footer line:

```
📎 Neotoma: <label> · <label>
```

Using the URL form: `https://neotoma.markmhendrickson.com/entities/<id>`

**Visibility rule**: link only entity records whose schema allows public read \
(`guest_access_policy: read_only`). Until the Phase 3a-0 policy change ships, only \
`issue` entities are known to be guest-readable; link those. For all other entity types \
(harness_event, plan_contribution, gate_status, etc.) that are not yet public, reference \
the entity id in prose — e.g. "see harness_event `ent_abc123`" — WITHOUT a bare URL \
that would 401 for public readers. Once Phase 3a-0 sets `read_only` on the \
public-orchestration types, the full link form applies to all of them.

### Brevity

Keep comments checklist/structured. Avoid essay-style prose. The implementer and \
aggregator (Vanellus) parse these; treat them as structured data with a human-readable \
summary, not a narrative.\
"""

# Splice the rendered vocabulary block in via plain string replacement rather
# than `.format()` — the contract text above is full of literal `{`/`}`-free
# but backtick- and code-fence-heavy examples, and a stray brace anywhere in a
# future edit would make `.format()` raise. `.replace()` on a placeholder that
# cannot occur elsewhere in the text has no such failure mode.
SWARM_GITHUB_CONTRACT = SWARM_GITHUB_CONTRACT.replace(
    "{verdict_vocabulary_block}", _VERDICT_VOCABULARY_BLOCK
).replace("{gate_verdict_position_rule}", GATE_VERDICT_POSITION_RULE)

# ── Prior-art contract (check existing context before building) ───────────────
# Injected into every dispatched agent's system prompt by build_system_prompt,
# alongside SWARM_GITHUB_CONTRACT.  Lives in ONE place, exactly like that
# contract — the point is that no brief author has to remember to ask for it.
#
# Motivated by two wasted runs on 2026-09-01: an agent was dispatched to build
# provider load balancing that already existed in harness_router.py
# (provider_candidates), and another was sent after neotoma#2279 — an issue
# filed against a clone 139 commits behind main, describing problems already
# fixed — and spent its entire run refuting its own brief.  On the same day the
# check paid off twice where a brief happened to request it: workflow_definition
# and participation_record already existed (so no new entity types were needed),
# and the non-blocking DB worker pool had shipped in July and merely was not
# selected (so the fix was one config line rather than building a pool).
#
# Scoped deliberately to three checks with observed payoff rather than a general
# "be careful" instruction, because a check that fires noise on every dispatch
# gets ignored — which is the same failure as having no check at all.

SWARM_PRIOR_ART_CONTRACT = """\
## Prior-art contract — check before you build

This codebase's dominant failure mode is **correct code that nothing invokes**.
Several mechanisms here are fully written and tested but wired into nothing, so
work that looks unbuilt is often built-but-unreferenced. Assume the thing you
were asked to build may already exist until you have checked.

Before you write code or open a PR, run these three checks. They are fast, and
each one has caught a wasted run in this swarm.

1. **Existing issues and PRs** — is this already filed, in flight, or fixed?
   `gh issue list --search '<terms>' --state all` and
   `gh pr list --search '<terms>' --state all` in the repo you were pointed at.
   An issue can also be *stale*: check whether the clone or branch it describes
   is behind main before you trust its problem statement.

2. **The codebase** — does this mechanism already exist, perhaps unwired?
   Grep for the capability, not just the name you were given: the existing
   implementation almost certainly uses different vocabulary than your brief.
   Search for the function it would perform and the config it would read. If you
   find it, check whether anything *calls* it — an unwired implementation needs
   connecting, not rebuilding.

3. **Existing tasks and plans** — is another agent already on this? Many agents
   run concurrently with overlapping scopes. Check open Neotoma `task` entities
   and the relevant `plan` before starting.

**Report what you found, at the top of your output, before your work.** One or
two lines: what you searched, and whether anything already covers this. If the
checks turned up prior art, say what it is and how it changed your approach.
If they turned up nothing, say that — a stated negative result is what makes
this contract auditable.

**When the brief's premise is wrong, say so and stop.** If the thing already
exists, or the issue describes a problem already fixed, that finding IS the
deliverable. Report it and do not build the duplicate. Correcting a brief is a
successful outcome, not a failed one — do not treat "I was told to build it" as
a reason to build something the repository already has.\
"""


# ── System-prompt assembly ─────────────────────────────────────────────────────


def build_system_prompt(
    agent_def: AgentDefinition,
    skill_md: str,
    include_github_contract: bool = False,
    policy_prompt: str = "",
) -> tuple[str, bool]:
    """
    Build the composite system prompt for a role dispatch.

    Returns (prompt, degraded) where degraded=True means the agent_definition
    did not contribute (empty prompt_markdown) and the subprocess will run with
    SKILL.md alone.

    The agent_definition's canonical instructions come FIRST so they establish
    identity, permissions, and behavioral constraints before the per-task skill
    instructions. Separated by a clear boundary so the model can parse both layers.

    When include_github_contract=True, SWARM_GITHUB_CONTRACT is inserted between
    the definition prompt and the skill_md so all GitHub-dispatched agents receive
    the shared comment convention in ONE place.  The contract is injected even in
    degraded mode (no definition_prompt) because it is useful guidance regardless.
    When include_github_contract=False (the default), behaviour is byte-identical
    to the pre-contract implementation — the SSE/non-GitHub task path is unchanged.

    SWARM_PRIOR_ART_CONTRACT is injected on the SAME condition, immediately after
    the GitHub contract.  It is deliberately not given its own flag: the whole
    point is that no brief author has to remember to ask for a prior-art check,
    and a second flag would just relocate the forgetting.  It is injected in
    degraded mode too, for the same reason the GitHub contract is — checking for
    existing work is useful regardless of which definition loaded.

    The design-basis contract (foundation.foundation_contract: the
    SWARM_FOUNDATION_CONTRACT rule plus the kernel documents actually on this
    checkout) follows on the same flag, for the same reason.  It is EMPTY — and
    so absent — on a checkout with no docs/foundation/conformance.md, so the
    prompt only ever names a reading list the agent can open; the day a kernel
    document lands, the injected text changes to name it.

    ``policy_prompt`` is the live, role-scoped agent_policy rendering from
    Neotoma. It follows the stable definition and skill layers so conditional
    rules are delivered through the same provider-neutral prompt carrier. An
    empty rendering preserves the pre-policy prompt byte-for-byte.
    """
    def with_policy(prompt: str) -> str:
        rendered_policy = policy_prompt.strip()
        if not rendered_policy:
            return prompt
        return f"{prompt}\n\n---\n\n{rendered_policy}"

    contracts = ""
    if include_github_contract:
        contracts = f"{SWARM_GITHUB_CONTRACT}\n\n---\n\n{SWARM_PRIOR_ART_CONTRACT}"
        foundation = foundation_contract()
        if foundation:
            contracts = f"{contracts}\n\n---\n\n{foundation}"
    definition_prompt = (agent_def.prompt_markdown or "").strip()
    if definition_prompt:
        if include_github_contract:
            return with_policy(
                f"{definition_prompt}\n\n"
                "---\n\n"
                f"{contracts}\n\n"
                "---\n\n"
                f"{skill_md}"
            ), False
        return with_policy(f"{definition_prompt}\n\n---\n\n{skill_md}"), False
    # Degraded: no definition_prompt.
    if include_github_contract:
        return with_policy(f"{contracts}\n\n---\n\n{skill_md}"), True
    return with_policy(skill_md), True


# ── Neotoma harness_event writer ───────────────────────────────────────────────


def _write_harness_event(
    *,
    task_entity_id: str,
    role: str,
    agent_sub: str,
    event_type: str,
    tool_name: str,
    success: str,
    agent_session_id: str = "",
    input_summary: str = "",
    output_summary: str = "",
    duration_ms: int | None = None,
    usage: "DispatchUsage | None" = None,
    resolved_tier: "model_tiering.ResolvedTier | None" = None,
) -> None:
    """
    Best-effort write of a harness_event entity to Neotoma.

    Uses the same /store endpoint and pattern as lib/activity/_store_activity_log.
    Never raises — a harness_event failure must not crash dispatch.

    ``usage`` carries per-dispatch model/provider/token attribution (see
    dispatch_usage.py). Its fields are merged in only when actually reported —
    a harness that reports nothing adds no keys, so an absent field reads as
    "not reported" rather than as a measured zero.

    ``resolved_tier`` (operator ruling 2026-09-29, model_tiering.py) records
    the ACTION-CLASS tiering decision — which tier this dispatch was required
    to run at and why (policy / unresolved-fail-closed / escalated) — which is
    a distinct fact from ``usage.model``/``usage.model_source`` (what model
    actually ran and how confidently that is known). Both are recorded so a
    reader can ask "was this dispatch tiered correctly" independent of
    whether the harness happened to report its own model.
    """
    base_url = _require_neotoma_base_url()
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    if not token:
        # Best-effort diagnostic write: log loudly and skip rather than raise.
        # Every caller of this function already wraps it in
        # `try/except Exception: log.debug(...)`, so a raise here would only
        # get demoted to a debug line indistinguishable from a transient
        # network blip — losing the signal instead of surfacing it. Logging
        # at WARNING here, before returning, is what actually makes an empty
        # token visible: the audit trail has a hole, but dispatch (the thing
        # this event is only OBSERVING) is not the thing failing.
        log.warning(
            "[apis] NEOTOMA_BEARER_TOKEN is not set; skipping harness_event "
            f"write (event_type={event_type}, task_entity_id={task_entity_id})"
        )
        return

    event_at = datetime.now(timezone.utc).isoformat()
    # canonical_name_fields per schema: session_id, event_type, event_at.
    session_key = agent_session_id or f"{role}:{task_entity_id}"
    idempotency_key = f"harness-event-{session_key}-{event_type}-{event_at}"

    entity: dict = {
        "entity_type": "harness_event",
        "event_type": event_type,
        "event_at": event_at,
        "tool_name": tool_name,
        "agent_sub": agent_sub,
        "success": success,
        "task_entity_id": task_entity_id,
    }
    if agent_session_id:
        entity["session_id"] = agent_session_id
    if input_summary:
        entity["input_summary"] = input_summary[:500]
    if resolved_tier is not None:
        # Prefixed (not appended) so the 500-char truncation below can never
        # cut it: the marker survives even where the server drops the
        # dedicated tier fields as undeclared on this schema.
        tier_marker = f"tiering={resolved_tier.tier}({resolved_tier.source})"
        output_summary = (
            f"{tier_marker} {output_summary}" if output_summary else tier_marker
        )
    if output_summary:
        entity["output_summary"] = output_summary[:500]
    if duration_ms is not None:
        entity["duration_ms"] = duration_ms
    if usage is not None:
        # Additive: only fields the harness actually reported. If the
        # harness_event schema has not yet declared these, Neotoma accepts the
        # write and drops the undeclared keys — the pre-existing fields still
        # land, so this can never regress what was already recorded.
        entity.update(usage.as_event_fields())
    if resolved_tier is not None:
        # Additive, same posture as `usage` above: an undeclared field on an
        # unrecognized schema is dropped server-side, never rejected. The
        # `tiering=` marker is ALSO prefixed onto output_summary above, so
        # the requested tier survives even where the dedicated field does
        # not — the same durability strategy #567 established for `usage`'s
        # model marker.
        entity["requested_tier"] = resolved_tier.tier
        entity["tier_action_class"] = resolved_tier.action_class
        entity["tier_source"] = resolved_tier.source
        if resolved_tier.escalation_reasons:
            entity["tier_escalation_reasons"] = list(resolved_tier.escalation_reasons)

    payload = {
        "idempotency_key": idempotency_key,
        "observation_source": "workflow_state",
        "entities": [entity],
    }
    relationships = []
    if task_entity_id:
        relationships.append({
            "source_index": 0,
            "target_entity_id": task_entity_id,
            "relationship_type": "REFERS_TO",
        })
    if agent_session_id:
        relationships.append({
            "source_index": 0,
            "target_entity_id": agent_session_id,
            "relationship_type": "REFERS_TO",
        })
    if relationships:
        payload["relationships"] = relationships
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{base_url}/store",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    req.add_header("User-Agent", NEOTOMA_USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=5.0) as response:
            raw = response.read()
        stored = json.loads(raw) if raw else {}
        event_id = next((
            row.get("entity_id")
            for row in stored.get("entities", [])
            if row.get("entity_type") == "harness_event"
        ), None)
        if event_id:
            readback_req = urllib.request.Request(
                f"{base_url}/entities/{event_id}", method="GET",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            readback_req.add_header("User-Agent", NEOTOMA_USER_AGENT)
            with urllib.request.urlopen(readback_req, timeout=5.0) as response:
                readback = json.loads(response.read())
            snapshot = readback.get("snapshot") or {}
            if isinstance(snapshot.get("snapshot"), dict):
                snapshot = snapshot["snapshot"]
            expected = {
                "task_entity_id": task_entity_id,
                **({"session_id": agent_session_id} if agent_session_id else {}),
            }
            if any(snapshot.get(field) != value for field, value in expected.items()):
                log.warning(
                    "[apis] harness_event readback mismatch "
                    f"(entity_id={event_id}, task_entity_id={task_entity_id})"
                )
            relationships_req = urllib.request.Request(
                f"{base_url}/entities/{event_id}/relationships", method="GET",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            relationships_req.add_header("User-Agent", NEOTOMA_USER_AGENT)
            with urllib.request.urlopen(relationships_req, timeout=5.0) as response:
                stored_relationships = json.loads(response.read()).get("relationships", [])
            expected_targets = {target for target in (task_entity_id, agent_session_id) if target}
            actual_targets = {
                rel.get("target_entity_id")
                for rel in stored_relationships
                if rel.get("source_entity_id") == event_id
                and rel.get("relationship_type") == "REFERS_TO"
            }
            if not expected_targets.issubset(actual_targets):
                log.warning(
                    "[apis] harness_event relationship readback mismatch "
                    f"(entity_id={event_id}, task_entity_id={task_entity_id})"
                )
    except Exception as exc:
        log.debug(f"[apis] harness_event write failed (non-fatal): {exc}")


# ── Dispatch-failure diagnostics (ateles#257) ──────────────────────────────────
#
# On a failed dispatch the ONLY durable evidence used to be `stderr[:500]` in a
# log line and `stderr[:200]` in a harness_event — stdout was dropped entirely.
# That is why ateles#256's rc=1 root cause is unrecoverable: the leading bytes
# of stderr were an incidental `--allowedTools` warning and the real error sat
# past the cut, or in stdout.
#
# We now write the COMPLETE stdout+stderr of every failed dispatch to a
# per-dispatch file and put its path everywhere the failure surfaces.
# Everything here is best-effort: a diagnostics failure must NEVER break
# dispatch.

DISPATCH_FAILURE_LOG_DIR = Path(
    os.environ.get(
        "ATELES_DISPATCH_FAILURE_LOG_DIR",
        str(Path.home() / "Library" / "Logs" / "ateles" / "dispatch-failures"),
    )
)

# Rate-limit window for operator notifications about dispatch failures. A
# swarm-wide breakage should produce a signal, not 200 of them: identical
# (skill, returncode, stderr-shape) failures notify at most once per window.
DISPATCH_FAILURE_NOTIFY_WINDOW_SECONDS = int(
    os.environ.get("ATELES_DISPATCH_FAILURE_NOTIFY_WINDOW", "3600")
)

# signature -> monotonic seconds of last notification. Process-local; the daemon
# is long-lived, so this is the right lifetime for burst suppression.
_dispatch_failure_notified_at: dict[str, float] = {}

# Env vars whose values must never land in a diagnostics file, in case a failing
# child echoes its own environment or argv.
_REDACTED_ENV_VARS = (
    "NEOTOMA_BEARER_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "TELEGRAM_BOT_TOKEN",
)

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value: str, *, limit: int = 60) -> str:
    """
    Filesystem-safe slug for one path component. Never raises.

    Dots are collapsed so no ``..`` survives: ``skill`` reaches this from a
    caller-supplied name, and the result is joined onto a directory path.
    """
    cleaned = _SLUG_RE.sub("-", (value or "").strip())
    cleaned = re.sub(r"\.+", ".", cleaned).strip("-.")
    return (cleaned or "unknown")[:limit]


def _redact_secrets(text: str) -> str:
    """Replace any known secret value appearing in child output."""
    out = text
    for var in _REDACTED_ENV_VARS:
        secret = os.environ.get(var, "")
        if secret and len(secret) >= 8:
            out = out.replace(secret, f"<redacted:{var}>")
    return out


def _summarize_command(cmd: list[str]) -> str:
    """
    Render the dispatch command for the diagnostics header.

    The ``--append-system-prompt`` payload is multi-KB and not diagnostic, so it
    is elided by length rather than inlined.
    """
    parts: list[str] = []
    elide_next = False
    for arg in cmd:
        if elide_next:
            parts.append(f"<{len(arg)}B system-prompt elided>")
            elide_next = False
            continue
        parts.append(arg)
        if arg == "--append-system-prompt":
            elide_next = True
    return _redact_secrets(" ".join(parts))


def write_dispatch_failure_log(
    *,
    skill: str,
    role: str,
    returncode: int | None,
    stdout: str,
    stderr: str,
    task_entity_id: str = "",
    cmd: list[str] | None = None,
    cwd: str | None = None,
    duration_ms: int | None = None,
) -> str:
    """
    Persist the COMPLETE stdout and stderr of a failed dispatch to a file.

    Returns the absolute path written, or ``""`` when the write could not be
    made. NEVER raises — diagnostics must not be able to break a dispatch.

    The file is written mode-0600: child output can contain repository content
    and, in pathological cases, tokens echoed by a failing tool.
    """
    try:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        DISPATCH_FAILURE_LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = DISPATCH_FAILURE_LOG_DIR / f"{_slug(skill)}-{ts}.log"

        header = [
            "# Ateles dispatch failure (ateles#257)",
            f"written_at: {datetime.now(timezone.utc).isoformat()}",
            f"skill: {skill}",
            f"role: {role}",
            f"returncode: {returncode}",
            f"task_entity_id: {task_entity_id or '(none)'}",
            f"cwd: {cwd or '(daemon default)'}",
            f"duration_ms: {duration_ms if duration_ms is not None else '(unknown)'}",
            f"stdout_bytes: {len(stdout)}",
            f"stderr_bytes: {len(stderr)}",
        ]
        if cmd:
            header.append(f"command: {_summarize_command(list(cmd))}")

        body = (
            "\n".join(header)
            + "\n\n===== STDOUT (complete) =====\n"
            + _redact_secrets(stdout)
            + "\n===== END STDOUT =====\n"
            + "\n===== STDERR (complete) =====\n"
            + _redact_secrets(stderr)
            + "\n===== END STDERR =====\n"
        )
        path.write_text(body, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return str(path)
    except Exception as exc:  # noqa: BLE001 — diagnostics must never break dispatch
        log.warning(f"[apis] could not write dispatch-failure log (non-fatal): {exc}")
        return ""


def _failure_signature(skill: str, returncode: int | None, stderr: str) -> str:
    """
    Stable dedup key for a dispatch failure.

    Keys on (skill, returncode, hash of the stderr *shape*) so a burst of the
    SAME systemic breakage collapses to one notification while a genuinely
    different failure still gets through. Long hex runs and digits are
    normalized out so per-run ids and timestamps don't defeat the dedup.
    """
    normalized = re.sub(r"[0-9a-f]{6,}", "<hex>", (stderr or "")[-2000:])
    normalized = re.sub(r"\d+", "<n>", normalized)
    digest = hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()[:12]
    return f"{skill}:{returncode}:{digest}"


def _should_notify_dispatch_failure(
    signature: str, *, now: float | None = None
) -> bool:
    """
    True when this failure signature has not notified within the rate-limit
    window. Records the notification time as a side effect when it returns True.
    """
    current = time.monotonic() if now is None else now
    last = _dispatch_failure_notified_at.get(signature)
    if last is not None and (current - last) < DISPATCH_FAILURE_NOTIFY_WINDOW_SECONDS:
        return False
    _dispatch_failure_notified_at[signature] = current
    return True


def notify_dispatch_failure(
    notifier,
    *,
    skill: str,
    role: str,
    returncode: int | None,
    stderr: str,
    task_entity_id: str = "",
    log_path: str = "",
) -> bool:
    """
    Send a rate-limited operator notification for a failed dispatch.

    Returns True when a notification was actually delivered to the notifier,
    False when suppressed by dedup, when no notifier was supplied, or when
    delivery raised. Never raises.

    Priority is BLOCKER per the rubric: a failed dispatch is work that did not
    happen and will not retry itself — it must reach the operator promptly
    rather than wait for a digest. Dedup is what keeps that from becoming spam.
    """
    if notifier is None:
        return False
    try:
        signature = _failure_signature(skill, returncode, stderr)
        if not _should_notify_dispatch_failure(signature):
            log.debug(
                f"[apis] dispatch-failure notification suppressed (dedup): {signature}"
            )
            return False

        preview = _redact_secrets(" ".join((stderr or "").split()))[:300]
        message = (
            f"Dispatch FAILED: {skill} (role {role}, rc={returncode}) "
            f"for task {task_entity_id or '(unknown)'}. "
            f"Full output: "
            f"{log_path or '(diagnostics file unavailable — see daemon log)'}"
        )
        if preview:
            message += f" — stderr head: {preview}"

        from lib.notify import Priority

        notifier.send(message, priority=Priority.BLOCKER, handler="apis")
        return True
    except Exception as exc:  # noqa: BLE001 — notification must never break dispatch
        log.debug(f"[apis] dispatch-failure notifier.send failed: {exc}")
        return False


# ── SkillResult ────────────────────────────────────────────────────────────────


@dataclass
class SkillResult:
    skill: str
    ok: bool
    returncode: int | None
    stdout: str
    stderr: str
    error: str = ""  # non-process failure: missing binary / SKILL.md / timeout
    provider: str = ""
    attempted_providers: tuple[str, ...] = ()
    # Set only when the runner independently established that a zero-exit
    # child encountered one canonical delivery denial and no provider
    # capacity/auth/launch diagnostic.  Callers must not infer this from the
    # free-form transcript or from ``error`` alone.
    delivery_failure_reason: str = ""
    # Every distinct delivery denial found across the complete stderr
    # diagnostic set, in canonical signature order rather than transcript
    # order.  ``delivery_failure_reason`` is trusted only when this tuple has
    # exactly that one recoverable entry and ``delivery_failure_conflicts`` is
    # empty.
    delivery_failure_reasons: tuple[str, ...] = ()
    delivery_failure_conflicts: tuple[str, ...] = ()
    # Set (ISO-8601, local zone) when NOTHING ran because every eligible
    # provider is inside a persisted session/usage-limit cooling window: the
    # earliest instant one comes back. Distinct from generic exhaustion.
    cooled_until: str = ""
    # Per-dispatch model + token attribution (dispatch_usage.py). None when the
    # dispatch never reached a harness (missing binary, unreadable SKILL.md).
    usage: DispatchUsage | None = None
    # Why a claude-local attempt failed (`kind` or `kind:reason`, see
    # local_provider.describe_failure). Set whenever local ran or was skipped and
    # failed, even when a later provider then succeeded — so a run that cost
    # frontier spend is never indistinguishable from one that stayed local.
    local_failure: str = ""


# ── Harness adapters + capacity detection ─────────────────────────────────────

_CAPACITY_FAILURE_SIGNATURES = (
    "usage limit",
    "rate limit reached",
    "rate_limit_error",
    "you've hit your limit",
    "you have hit your limit",
    "weekly limit",
    "session limit",
    "maximum usage",
    "quota exceeded",
    "out of requests",
    "no requests remaining",
    "resets at",
    "resets in",
)

_AUTH_FAILURE_SIGNATURES = (
    "invalid authentication credentials",
    "could not resolve authentication",
    "oauth token has expired",
    "authentication_error",
    "authentication required",
    "please run /login",
    "please run `claude auth login`",
    "please run 'agent login'",
    "invalid api key",
    "not logged in",
    "login required",
    "permission denied (publickey)",
    "could not read username for",
    "authentication failed for",
)

_METERED_CREDENTIALS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "CURSOR_API_KEY",
)


def _provider_binaries() -> dict[str, str | None]:
    return {
        "claude": CLAUDE_BIN,
        "codex": CODEX_BIN,
        "cursor": CURSOR_BIN,
        # The claude CLI against a local model; absent unless configured
        # (local_provider.load_config), so an unconfigured host never sees it.
        local_provider.LOCAL_PROVIDER: (
            CLAUDE_BIN if local_provider.load_config() is not None else None
        ),
    }


_DIAGNOSTIC_PREFIX_RE = re.compile(r"^(?:(?:fatal|error):\s*)+")
_API_ERROR_PREFIX_RE = re.compile(r"^api error:\s*(?:\d{3}\s*:?[ \t]*)?")
_ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")
_SSH_AUTH_DIAGNOSTIC_RE = re.compile(
    r"^(?:[\w.+-]+@[\w.-]+:\s*)?permission denied \(publickey\)(?:$|[ \t:;,.-])"
)
_CAPACITY_DIAGNOSTIC_PATTERNS = (
    re.compile(r"^you(?:'ve| have) hit (?:your )?(?:weekly |session )?usage limit\b"),
    re.compile(r"^you(?:'ve| have) hit your (?:weekly |session )?limit\b"),
    re.compile(r"^you(?:'ve| have) reached your usage limit\b"),
)


def _diagnostic_starts_with(line: str, signature: str) -> bool:
    """Match a diagnostic token with an optional explanatory suffix."""
    if not line.startswith(signature):
        return False
    if len(line) == len(signature):
        return True
    return line[len(signature)] in " \t:;,.-(["


def _normalize_diagnostic_candidate(candidate: str) -> str:
    """Normalize one anchored diagnostic without searching within its prose."""
    normalized = _ANSI_SGR_RE.sub("", candidate).replace("\u00a0", " ").strip().lower()
    while normalized:
        previous = normalized
        normalized = _DIAGNOSTIC_PREFIX_RE.sub("", normalized)
        normalized = _API_ERROR_PREFIX_RE.sub("", normalized)
        if normalized == previous:
            break
    return normalized


def _diagnostic_envelope_candidates(envelope: object) -> tuple[str, ...]:
    """Return only the supported string fields from a provider JSON envelope."""
    if not isinstance(envelope, dict):
        return ()
    error = envelope.get("error")
    if isinstance(error, str):
        return (error,)
    if isinstance(error, dict):
        return tuple(
            value
            for key in ("message", "type")
            if isinstance(value := error.get(key), str)
        )
    return ()


def _diagnostic_candidate_kind(candidate: str) -> str | None:
    """Classify one already-extracted candidate at its normalized start."""
    normalized = _normalize_diagnostic_candidate(candidate)
    if re.match(r"^(?:codex|claude|cursor(?:-agent)?) launch failed:", normalized):
        return "launch"
    if _SSH_AUTH_DIAGNOSTIC_RE.match(normalized) or any(
        _diagnostic_starts_with(normalized, signature)
        for signature in _AUTH_FAILURE_SIGNATURES
    ):
        return "auth"
    if any(
        _diagnostic_starts_with(normalized, signature)
        for signature in _CAPACITY_FAILURE_SIGNATURES
    ) or any(pattern.match(normalized) for pattern in _CAPACITY_DIAGNOSTIC_PATTERNS):
        return "capacity"
    return None


def _diagnostic_line_kind(raw_line: str) -> str | None:
    """Classify one provider diagnostic without scanning ordinary prose.

    Normalize provider API/JSON envelopes, SGR color, and Git's ``fatal:`` or
    ``error:`` prefixes. Keep matching anchored at the extracted diagnostic
    start so a prompt or verdict discussing these words is excluded.
    """
    normalized = _normalize_diagnostic_candidate(raw_line)
    if not normalized:
        return None
    candidates = [normalized]
    if normalized.startswith("{"):
        try:
            envelope = json.loads(normalized)
        except json.JSONDecodeError:
            envelope = None
        extracted = _diagnostic_envelope_candidates(envelope)
        if extracted:
            candidates = list(extracted)
    for candidate in candidates:
        if (kind := _diagnostic_candidate_kind(candidate)) is not None:
            return kind
    return None


def _structured_diagnostic_kinds(text: str) -> set[str]:
    """Classify JSON envelopes that start at a diagnostic line boundary.

    ``json.JSONDecoder.raw_decode`` lets a formatted envelope end before a
    following diagnostic line. Requiring the object to start after only the
    same supported prefixes as the line classifier, and to occupy the rest of
    its closing line, keeps arbitrary prose out of this structured path.
    """
    cleaned = _ANSI_SGR_RE.sub("", text).replace("\u00a0", " ")
    decoder = json.JSONDecoder()
    kinds: set[str] = set()
    line_start = 0
    while line_start < len(cleaned):
        line_end = cleaned.find("\n", line_start)
        if line_end < 0:
            line_end = len(cleaned)
        first_line = cleaned[line_start:line_end]
        brace_offset = first_line.find("{")
        if brace_offset >= 0:
            prefix = first_line[:brace_offset]
            if _normalize_diagnostic_candidate(prefix + "{}") == "{}":
                object_start = line_start + brace_offset
                try:
                    envelope, object_end = decoder.raw_decode(cleaned, object_start)
                except json.JSONDecodeError:
                    envelope = None
                    object_end = object_start
                closing_line_end = cleaned.find("\n", object_end)
                if closing_line_end < 0:
                    closing_line_end = len(cleaned)
                if not cleaned[object_end:closing_line_end].strip():
                    for candidate in _diagnostic_envelope_candidates(envelope):
                        if (kind := _diagnostic_candidate_kind(candidate)) is not None:
                            kinds.add(kind)
        line_start = line_end + 1
    return kinds


def _diagnostic_failure_kinds(*texts: str) -> set[str]:
    kinds = {
        kind
        for text in texts
        for raw_line in text.splitlines()
        if (kind := _diagnostic_line_kind(raw_line)) is not None
    }
    for text in texts:
        kinds.update(_structured_diagnostic_kinds(text))
    return kinds


def _cool_after_capacity_failure(provider: str, result: "SkillResult") -> None:
    """Cool ``provider`` until the reset its refusal states, and persist it.

    The in-process timer alone was invisible to the next ``dispatch_role``
    process, so a spent 5-hour session window was relaunched (and refused
    instantly) by every dispatch until the hour-long timer of some long-lived
    process happened to cover it.  The window is written where the router reads
    selection state.

    Persisting is a cross-process hold-out, so it trusts only the provider's own
    refusal: the run must have FAILED (a successful run that merely discusses
    limits never persists), the refusal must be a short limit line at the end of
    its output (``limit_reset.parse_refusal``), and the stated reset is clamped
    to the longest window that kind of limit can have.  Anything else keeps the
    old in-process timer only.  An unreadable reset gets the conservative
    default (``APIS_HARNESS_COOLDOWN_SECONDS``, one hour) and says so.
    """
    cool_down(provider)
    if result.ok or result.returncode in (0, None):
        return
    try:
        refusal = parse_refusal(result.stdout, result.stderr, provider=provider)
        if refusal is None:
            log.warning(
                f"[apis] {provider} was classified as a capacity failure but its "
                "output does not end in a limit refusal; cooled in-process only"
            )
            return
        now = time.time()
        if refusal.until_wall is not None:
            until = refusal.until_wall
            reason = {"session": "session_limit", "weekly": "weekly_limit"}.get(
                refusal.kind, "usage_limit"
            )
            log.warning(
                f"[apis] {provider} refused with a {refusal.kind} limit; cooled until "
                f"{render_wall(until)} (stated reset {refusal.matched!r} in "
                f"{refusal.zone}{'; clamped' if refusal.clamped else ''})"
            )
        else:
            try:
                default = max(
                    0.0, float(os.environ.get("APIS_HARNESS_COOLDOWN_SECONDS", "3600"))
                )
            except ValueError:
                default = 3600.0
            until, reason = now + default, "capacity_unparsed_reset"
            log.warning(
                f"[apis] {provider} refused with a capacity failure whose reset "
                f"time could not be read; cooled for the default {int(default)}s "
                f"(until {render_wall(until)})"
            )
        record_cooling(provider, until, reason=reason, observed_at=now)
    except Exception as exc:  # noqa: BLE001 — persistence must never break failover
        log.error(f"[apis] could not persist {provider} cooling window: {exc}")


def _refresh_usage_snapshot(binaries: dict[str, str | None]) -> None:
    """Refresh the usage snapshot under the subscription-only child environment."""
    try:
        refresh_usage_if_stale(binaries, env=_subscription_only_env())
    except Exception as exc:  # noqa: BLE001 - feeding must never break dispatch
        log.error(f"[apis] usage snapshot refresh failed: {exc}")


def _usage_gate_message(gates: dict) -> tuple[str, str]:
    """The distinct "usage gate refused frontier dispatch" text and retry time.

    Names WHICH gate refused (stale reading vs weekly pace) so the operator is
    never told a stale meter is exhaustion, and says when to expect capacity.
    """
    retry_at = render_wall(min(float(g.returns_at) for g in gates.values()))
    detail = "; ".join(f"{name}: {g.message}" for name, g in sorted(gates.items()))
    return (
        f"frontier dispatch refused by the usage gate ({detail}); "
        f"local/mechanical work is unaffected; retry after {retry_at}",
        retry_at,
    )


def _cooled_message(windows: dict[str, dict[str, object]]) -> tuple[str, str]:
    """The distinct "every eligible provider is cooled" text and its end time."""
    earliest = render_wall(min(float(w["until"]) for w in windows.values()))
    detail = "; ".join(
        f"{name}: {w['reason']}, until {render_wall(float(w['until']))}"
        for name, w in sorted(windows.items())
    )
    return (
        "all eligible subscription-backed harness providers are cooled until "
        f"{earliest} after a session/usage limit ({detail}); no CLI was "
        "launched for this dispatch",
        earliest,
    )


def _provider_failure_kind(*texts: str) -> str | None:
    """Classify failures that are safe to retry on another harness."""
    kinds = _diagnostic_failure_kinds(*texts)
    if "capacity" in kinds:
        return "capacity"
    if "auth" in kinds:
        return "auth"

    # Failed-provider payloads may embed diagnostics in arbitrary sentences.
    # Preserve that established fallback for failed attempts only; delivery
    # recovery uses only the anchored line classifier above.
    blob = " ".join(text for text in texts if text).lower()
    if any(signature in blob for signature in _CAPACITY_FAILURE_SIGNATURES):
        return "capacity"
    if any(signature in blob for signature in _AUTH_FAILURE_SIGNATURES):
        return "auth"
    return None


# ── Codex sandbox: writable git roots + network (ateles#590) ──────────────────
# `codex exec --sandbox workspace-write` grants write access to the working
# directory, /tmp, and $TMPDIR — and nothing else. That is fine for a plain
# clone, whose entire `.git` lives inside the workdir. It is not fine for a
# LINKED WORKTREE, which is the layout this swarm mandates: the repo-isolation
# guard and ateles#572 both push every dispatch into its own worktree so
# concurrent agents cannot collide. In a linked worktree `.git` is a FILE
# pointing at `<main clone>/.git/worktrees/<name>`, and the object database is
# further out still, in `<main clone>/.git`. Both are outside the sandbox, so
# every git write is denied:
#
#   fatal: Unable to create '.../worktrees/<name>/index.lock': Operation not permitted
#   error: unable to create temporary file: Operation not permitted     (git add)
#
# Note that BOTH roots are required, and this was verified rather than assumed:
# granting only the per-worktree gitdir still fails at `git add`, because loose
# objects are written under the COMMON dir. Granting only the common dir leaves
# index.lock denied. So the adapter grants exactly the two directories git
# actually needs, and nothing more.
#
# Network is the second, independent cause: workspace-write denies it by
# default, so `git push` and `gh` cannot resolve github.com ("Could not resolve
# host"). It is re-enabled through the documented config key rather than by
# dropping to --sandbox danger-full-access, which would also surrender
# filesystem confinement everywhere on the operator's machine — a far larger
# grant than the delivery path needs.


def _git_roots_for_sandbox(cwd: str | None) -> list[str]:
    """The directories git must be able to write to for a commit to succeed.

    Returns the resolved gitdir and common-dir for ``cwd``, de-duplicated and
    excluding anything already inside ``cwd`` (a plain clone needs no extra
    grant — its ``.git`` is under the workdir and workspace-write covers it).

    Returns ``[]`` when ``cwd`` is not a git repository or git is unavailable.
    A dispatch into a non-repo is legitimate; it simply needs no git roots.
    """
    if not cwd:
        return []
    try:
        workdir = Path(cwd).resolve()
    except OSError:
        return []

    roots: list[str] = []
    for flag in ("--git-dir", "--git-common-dir"):
        try:
            out = subprocess.run(
                ["git", "-C", str(workdir), "rev-parse", flag],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if out.returncode != 0:
            return []
        raw = out.stdout.strip()
        if not raw:
            continue
        # `rev-parse` may answer with a path relative to cwd (".git").
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = workdir / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        # Already covered by the workdir grant — do not widen the sandbox for
        # a path the sandbox already contains.
        if resolved == workdir or workdir in resolved.parents:
            continue
        as_str = str(resolved)
        if as_str not in roots:
            roots.append(as_str)
    return roots


# Signatures of a child that did real work and then could not deliver it.
# Each is a sandbox or network denial, not a code defect: the agent wrote
# correct output and the harness refused to let it out. Matching any of these
# turns a `returncode == 0` run into an explicit failure (ateles#590), because
# the alternative — the pre-fix behaviour — was `ok: true` over an empty
# delivery, the same class of lie as ateles#585 (envelope never written),
# ateles#566 (401 reported ok) and ateles#560 (grant_checker failing open).
_DELIVERY_DENIAL_SIGNATURES: tuple[tuple[str, str], ...] = (
    (
        r"(?:fatal|error): unable to create '[^']*index\.lock': "
        r"operation not permitted",
        "sandbox denied the git index lock — the child could not commit",
    ),
    (
        r"(?:fatal|error): unable to create temporary file: "
        r"operation not permitted",
        "sandbox denied writes to the git object store — the child could not commit",
    ),
    (
        r"fatal: unable to access '[^']*': could not resolve host: "
        r"(?:github\.com|api\.github\.com)",
        "sandbox denied network access — the child could not push or reach the GitHub API",
    ),
    (
        r"fatal: could not read from remote repository",
        "the child could not reach the git remote — nothing was pushed",
    ),
)

# The canonical, exact errors emitted when a zero-exit child did useful work
# but the delivery mechanism refused it. Provider routing must preserve these
# errors verbatim: they describe a task/delivery failure, not provider capacity
# or authentication, even when the larger transcript quotes those words.
_DELIVERY_DENIAL_REASONS = frozenset(
    reason for _pattern, reason in _DELIVERY_DENIAL_SIGNATURES
)
_RECOVERABLE_DELIVERY_DENIAL_REASONS = frozenset(
    {
        "sandbox denied network access — the child could not push or reach the GitHub API",
        "the child could not reach the git remote — nothing was pushed",
    }
)


def _delivery_failure_reasons(*texts: str) -> tuple[str, ...]:
    """Return every distinct delivery denial in canonical signature order.

    Signature order, rather than line order, makes the classification stable
    when two diagnostics are emitted in the opposite order.  Only stderr is
    supplied by the runner; stdout may quote these strings as reviewed prose.
    """
    lines = [
        line.strip() for text in texts if text for line in text.lower().splitlines()
    ]
    return tuple(
        reason
        for pattern, reason in _DELIVERY_DENIAL_SIGNATURES
        if any(re.match(pattern, line) for line in lines)
    )


def _delivery_failure_conflicts(
    reasons: tuple[str, ...], *texts: str
) -> tuple[str, ...]:
    """Name every condition that makes delivery-only recovery ambiguous."""
    conflicts: list[str] = []
    if len(reasons) > 1:
        conflicts.append("multiple_delivery_failures")
    diagnostic_kinds = _diagnostic_failure_kinds(*texts)
    conflicts.extend(
        kind for kind in ("auth", "capacity", "launch") if kind in diagnostic_kinds
    )
    if len(reasons) == 1 and reasons[0] not in _RECOVERABLE_DELIVERY_DENIAL_REASONS:
        conflicts.append("nonrecoverable_delivery_failure")
    return tuple(conflicts)


def _delivery_diagnostic_error(
    reasons: tuple[str, ...], conflicts: tuple[str, ...]
) -> str:
    if not reasons:
        return ""
    if not conflicts:
        return reasons[0]
    return (
        "mixed delivery diagnostics — delivery="
        f"{list(reasons)!r}; conflicts={list(conflicts)!r}"
    )


def _delivery_failure_reason(*texts: str) -> str | None:
    """Name the delivery denial in a child's output, if there is one.

    Read-only over the child's own words: no assumption is made about what the
    task was meant to deliver, so a task that never intended to commit is not
    penalised — it simply never emits these lines.
    """
    # Matched per LINE, anchored at the start, because that is where git emits
    # these — "fatal: ..." and "error: ..." begin a line. Searching a joined
    # blob matched the strings wherever they appeared, including quoted inside
    # ordinary prose: this PR's own body and diff both trip three of the four
    # signatures, so any agent dispatched to read them was reported as a failed
    # delivery (ateles#601 pm lens, reproduced). Anchoring is what separates
    # "git said this" from "someone wrote this down".
    reasons = _delivery_failure_reasons(*texts)
    return reasons[0] if reasons else None


def _provider_command(
    provider: str,
    binary: str,
    system_prompt: str,
    work_prompt: str,
    *,
    cwd: str | None,
    network: bool = False,
    codex_outer_sandboxed: bool = False,
    model: str | None = None,
) -> tuple[list[str], bytes | None]:
    """Build one provider's noninteractive command and initial stdin payload.

    ``network`` opens the codex sandbox's network access for THIS dispatch only.
    #590 asks for it "without granting blanket network access to every
    dispatch", so it is off by default and the caller turns it on for the
    dispatches whose task actually involves GitHub delivery.

    ``codex_outer_sandboxed`` is reserved for a caller that has already bound
    a separately probed OS sandbox around the Codex process. In that one case
    Codex must not install its own nested macOS Seatbelt profile:
    ``sandbox-exec`` rejects the nested ``sandbox_apply`` operation before any
    command can run. ``danger-full-access`` here means "no inner Codex
    sandbox"; the caller's outer sandbox remains the enforcement boundary.
    `_run_skill_once` refuses this flag unless the real command is wrapped by
    ``sandbox-exec``.

    ``model`` (ateles#567 / operator ruling 2026-09-29, model_tiering.py):
    when non-empty, pins this dispatch to a named model instead of the
    provider's ambient default, on all three providers. ``None`` (the
    default) reproduces exact prior behaviour — no flag is added — which
    matters for ``claude``: ``model_tiering.model_for_tier`` returns ``None``
    only when no vendor_binding is configured at all, and this module never
    guesses a model alias in that case.
    """
    if provider == "claude":
        return (
            [
                binary,
                "--print",
                *(["--model", model] if model else []),
                "--append-system-prompt",
                system_prompt,
            ],
            None,
        )

    composite_prompt = f"{system_prompt}\n\n---\n\n## Dispatched task\n\n{work_prompt}"
    if provider == "codex":
        # See the ateles#590 note above _git_roots_for_sandbox: without these
        # two additions a codex child in a linked worktree writes correct code
        # and then cannot commit, push, or open a PR.
        git_roots = [] if codex_outer_sandboxed else _git_roots_for_sandbox(cwd)
        add_dir_flags: list[str] = []
        for root in git_roots:
            add_dir_flags += ["--add-dir", root]
        if git_roots:
            log.info(
                "[apis] codex sandbox: granting git roots %s", ", ".join(git_roots)
            )
        # Delivery needs github.com — but only a delivery-bearing dispatch does.
        # Scoped to the workspace-write policy rather than dropping the sandbox,
        # and to the dispatches that need it rather than to all of them (#590:
        # "without granting blanket network access to every dispatch").
        network_flags = (
            []
            if codex_outer_sandboxed
            else ["-c", "sandbox_workspace_write.network_access=true"]
            if network
            else []
        )
        if network and not codex_outer_sandboxed:
            log.info("[apis] codex sandbox: network enabled for this dispatch")
        sandbox_mode = (
            "danger-full-access" if codex_outer_sandboxed else "workspace-write"
        )
        return (
            [
                binary,
                "exec",
                *(["--model", model] if model else []),
                "--sandbox",
                sandbox_mode,
                *network_flags,
                *add_dir_flags,
                "--ephemeral",
                "--skip-git-repo-check",
                "--color",
                "never",
                *(["--cd", cwd] if cwd else []),
                "-",
            ],
            composite_prompt.encode(),
        )
    if provider == "cursor":
        return (
            [
                binary,
                "--print",
                "--force",
                "--trust",
                "--approve-mcps",
                "--output-format",
                "text",
                *(["--model", model] if model else []),
                *(["--workspace", cwd] if cwd else []),
                composite_prompt,
            ],
            None,
        )
    raise ValueError(f"unsupported harness provider: {provider}")


def _requested_model(provider: str, cmd: list[str]) -> str | None:
    """Return the model this dispatch ASKED for, read off the built command.

    Deliberately derived from the argv actually being executed rather than from
    a parameter, so it stays correct no matter which layer decides the model —
    today nothing passes one (every dispatch takes the provider's ambient
    default), and the per-model fallback work in ateles#667 adds `--model` in
    the command builder. Reading argv means this keeps reporting the truth
    across that change instead of silently going stale.

    A requested model is NOT evidence of the model that ran; callers mark it
    ``model_source="requested"``. Returns None when no model was pinned.
    """
    flags = {"--model", "-m"}
    for i, arg in enumerate(cmd or []):
        if arg in flags and i + 1 < len(cmd):
            value = (cmd[i + 1] or "").strip()
            return value or None
        # Support the `--model=x` spelling too.
        for flag in ("--model=",):
            if arg.startswith(flag):
                value = arg[len(flag) :].strip()
                return value or None
    return None


def _subscription_only_env(
    env_extra: dict[str, str] | None = None,
    *,
    local_review: bool = False,
    local_review_home: str | None = None,
) -> dict[str, str]:
    """Build the child environment for a governed harness invocation.

    Local review inference gets an allowlisted environment rather than the
    daemon's ambient credentials.  Its isolated HOME/GH_CONFIG_DIR plus Git's
    explicit no-helper settings close credential-file, keychain, SSH-agent,
    and inherited-token publication paths while retaining only subscription
    authentication needed to run the selected model.

    Two independent controls on metered API billing:

    * The harness metered keys (``_METERED_CREDENTIALS``) are stripped unless
      ``APIS_ALLOW_METERED_HARNESS=1`` explicitly allows them.
    * Media-generation vendor credentials are ALWAYS stripped, by name and by
      prefix (``lib.credential_scrub``), and the override above
      never releases them. They belong to the host-side capability client only
      (ateles#1189); a dispatched agent must never hold one. The child is also
      marked so the capability client refuses to run inside it.

    STAGED CONTROL: this is still a denylist. A metered key nobody has named
    passes through. The standing fix is an allowlist child environment
    (tracked as a follow-up); until then ``test_unnamed_future_metered_key_is_scrubbed``
    is an ``xfail(strict=True)`` documenting the gap.
    """
    merged = {**os.environ, **(env_extra or {})}
    if local_review:
        if not local_review_home:
            raise ValueError("local_review requires an isolated local_review_home")
        allowed = {
            "PATH",
            "TMPDIR",
            "TMP",
            "TEMP",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TERM",
            "NO_COLOR",
            "CODEX_HOME",
            "CLAUDE_CODE_OAUTH_TOKEN",
        }
        child = {key: value for key, value in merged.items() if key in allowed}
        isolated = Path(local_review_home)
        child.update(
            {
                "HOME": str(isolated),
                "XDG_CONFIG_HOME": str(isolated / ".config"),
                "GH_CONFIG_DIR": str(isolated / ".config" / "gh"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "never",
                "GIT_ASKPASS": "/usr/bin/false",
                "SSH_ASKPASS": "/usr/bin/false",
                # An empty credential.helper resets any helper accumulated
                # from repository-local configuration before disabling
                # interactive fallback.
                "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": "credential.helper",
                "GIT_CONFIG_VALUE_0": "",
                "GIT_CONFIG_KEY_1": "credential.interactive",
                "GIT_CONFIG_VALUE_1": "never",
            }
        )
    else:
        child = merged
    if child.get("APIS_ALLOW_METERED_HARNESS", "0") != "1":
        for key in _METERED_CREDENTIALS:
            child.pop(key, None)
    elif child.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
        # Even under the explicit override, prefer Max-plan OAuth for Claude.
        child.pop("ANTHROPIC_API_KEY", None)
    for key in [k for k in child if is_generation_credential(k)]:
        child.pop(key, None)
    child[AGENT_CHILD_MARKER_ENV] = "1"
    return child


# ── Single-provider runner ─────────────────────────────────────────────────────


async def _run_skill_once(
    skill: str,
    prompt: str,
    *,
    provider: str,
    role: str | None = None,
    task_entity_id: str = "",
    agent_session_id: str = "",
    timeout: int | None = None,
    env_extra: dict[str, str] | None = None,
    notifier=None,  # lib.notify.Notifier | None — kept optional to avoid hard dep
    github_token: str | None = None,
    include_github_contract: bool = False,
    cwd: str | None = None,
    owns_pending_gate: bool = False,
    command_wrapper: list[str] | None = None,
    codex_outer_sandboxed: bool = False,
    local_review: bool = False,
    work_class: str | None = None,
    action_class: str | None = None,
    escalation_signals: "model_tiering.EscalationSignals | None" = None,
    model: str | None = None,
    precomputed_tier: "model_tiering.ResolvedTier | None" = None,
) -> SkillResult:
    """
    Run one T4 agent to completion and return its output.

    Stage 1: loads the role's agent_definition and live agent_policy (role
    defaults to skill when not passed — skill name == role name in this
    codebase). Prepends the definition's prompt_markdown to SKILL.md, appends
    the policy rendering, and applies the tool allowlist when restricted.

    Stage 2: writes harness_event entities to Neotoma at start, completion, and
    failure.

    Model tiering (operator ruling 2026-09-29, model_tiering.py): when
    ``action_class`` is supplied, the dispatch's tier is resolved from the
    live ``action_policy`` config (escalated by ``escalation_signals``, when
    given), and the tier is turned into a model via the live
    ``vendor_binding`` config for ``provider``. ``model`` overrides both —
    an explicit caller-supplied model wins outright, matching every other
    override-beats-config precedent in this module (``env_extra``,
    ``timeout``). ``action_class=None`` (every call site that predates this)
    reproduces EXACT prior behaviour: no tier is resolved, no model is
    requested, the provider's ambient default runs unchanged. An unreadable
    or unbound policy/binding never falls back to the ambient default when
    ``action_class`` WAS supplied — see ``model_tiering``'s module docstring
    for why that asymmetry is deliberate (fail up to the strongest tier,
    never down to silence).

    ``precomputed_tier``: when supplied, THIS tier is used instead of
    re-resolving from ``action_class``/``escalation_signals`` — only the
    provider-specific ``model_for_tier`` lookup still runs. ``run_skill``
    resolves the tier exactly ONCE per logical dispatch and passes it to
    every provider attempt in its failover loop, so a config edit landing
    mid-failover cannot retier the same dispatch differently across two
    providers (the tier is a property of the work, not of when in the
    failover sequence a provider happened to be tried). A direct caller of
    this function (tests, `dispatch_role.py`'s single-attempt path) that
    passes ``action_class`` with no ``precomputed_tier`` still gets a fresh
    resolution, unchanged from before.

    Stage 5: when agent_definition carries empty prompt_markdown, logs a WARN,
    sends a notifier alert (when a notifier is supplied), and records a
    degraded_generic_subagent harness_event. Dispatch still proceeds.

    ``github_token`` (#109 — per-agent GitHub identity): when supplied and
    non-empty, the token is injected into subprocess_env as both
    ``GITHUB_TOKEN`` and ``GH_TOKEN`` so the spawned agent's ``gh`` calls
    authenticate as the correct identity. When not supplied (``None``) on a
    dispatch that does NOT set ``include_github_contract``, the child inherits
    the daemon's ambient env unchanged (current behaviour for all callers that
    predate #109). An explicitly empty string is a distinct, refused case —
    see below.

    ``include_github_contract`` (Phase 1 / Layer A): when True, SWARM_GITHUB_CONTRACT
    is injected into the system prompt between the agent_definition and the SKILL.md,
    AND (ateles#590) this dispatch is network-enabled (see ``network=`` on the
    ``_provider_command`` call below). Pass True from GitHub-trigger call sites in
    swarm_dispatch.py, or from a manual/programmatic dispatch (dispatch_role.py)
    whose task must commit, push, or open a pull request; leave as False (the
    default) for all other dispatches so the contract never appears in payment,
    health, finance, or other non-GitHub work.

    Credential-boundary contract (ateles#590 security repair, PR #1334): a
    network-enabled dispatch (``include_github_contract=True``) REQUIRES a
    non-empty ``github_token`` — omitted or empty both return a failed
    ``SkillResult`` before any child is spawned, rather than falling back to
    the daemon's ambient GitHub identity. On any dispatch, an explicitly empty
    ``github_token`` (requested but resolved to ``""``) is refused the same
    way, since that shape means a caller tried to resolve a per-agent PAT and
    failed — silently falling back to the ambient identity there is exactly
    what let a PR land under the operator's own account instead of the
    agent's (ateles#109's original incident).

    Claude's `--allowed-tools` and injected Neotoma MCP config remain specific
    to the Claude adapter. Claude, Codex, and Cursor receive the same definition
    + skill + live-policy instructions through their provider prompt carriers
    and use their ambient configured tools.

    ``local_review`` gives a child inference-only authority: no GitHub or
    Neotoma publication credentials, no ambient credential files/keychain,
    no SSH agent, and (for Claude) an empty strict MCP configuration.  Parent
    publication remains outside this child environment.

    ``cwd`` (QE3 — eval-authoring affordance): when supplied, the dispatched
    child subprocess runs with this working directory instead of inheriting the
    daemon's. This is how the qa lens (Phoenicurus) is given a writable checkout
    of a PR branch so it can author an eval fixture, run ``eval:tier1``, commit,
    and push. When None (every call site that predates QE3), the child inherits
    the daemon's directory unchanged — exact current behaviour, no regression.

    ``command_wrapper`` (harness_lens_runner, ent_89a4d44b063cb0902106da49):
    when supplied, its elements are PREPENDED to the provider's own argv
    before ``asyncio.create_subprocess_exec`` runs it below — e.g.
    ``["/usr/bin/sandbox-exec", "-f", "/path/to/profile.sb"]`` to run
    codex/cursor's real binary under a macOS sandbox that denies specific file
    reads/writes. The absolute executable and profile shape are validated here;
    PATH resolution is never accepted for the no-inner-sandbox transition.
    This is the only point in the dispatch path where the process that will
    actually execute is assembled, so it is the only point a caller can make
    a guard bind onto the REAL subprocess rather than merely describe an
    intended mitigation next to code that runs unwrapped. Applied
    unconditionally when given — a caller that only wants it for codex/cursor
    passes ``None`` here for claude. The wrapper itself never needs and is
    never handed credential material; it wraps argv only.

    ``codex_outer_sandboxed`` disables Codex's inner sandbox only after this
    function verifies that the actual command is wrapped by ``sandbox-exec``.
    It exists for harness_lens_runner's probed effect-level guard; ordinary
    Codex dispatches retain ``workspace-write``.

    ``work_class`` (ateles task ent_71387d9c1d1d3d1eef9ecc01 — lean local
    prompt): when ``provider == local_provider.LOCAL_PROVIDER``, this replaces
    the full agent_definition + live-policy system prompt with
    ``local_provider.build_lean_prompt`` — role summary, the work class's own
    hard rules, and the task's SKILL.md, nothing else. This keeps the local
    prompt well under the local context ceiling regardless of how large the
    frontier prompt (agent_definition + agent_policy) grows. It has no effect
    on any other provider: a Claude/Codex/Cursor dispatch is built exactly as
    before, full stop, whether or not ``work_class`` is passed. After a local
    run, ``local_provider.verify_postcondition`` checks the work class's
    ground truth (where one exists) and fails the result over to a frontier
    provider rather than accepting a wrong local answer as ``ok``.
    """
    _role = (role or skill).lower()
    timeout = timeout or DISPATCH_TIMEOUT_SECONDS

    if codex_outer_sandboxed:
        trusted_wrapper_profile_pair = bool(
            provider == "codex"
            and command_wrapper
            and len(command_wrapper) == 3
            and command_wrapper[0] == str(TRUSTED_MACOS_SANDBOX_EXEC)
            and command_wrapper[1] == "-f"
            and Path(command_wrapper[2]).is_absolute()
            and TRUSTED_MACOS_SANDBOX_EXEC.is_file()
            and os.access(TRUSTED_MACOS_SANDBOX_EXEC, os.X_OK)
        )
        if not trusted_wrapper_profile_pair:
            msg = (
                "codex_outer_sandboxed requires provider='codex' and the exact "
                "trusted wrapper/profile pair rooted at "
                f"{TRUSTED_MACOS_SANDBOX_EXEC}; refusing to disable the inner "
                "Codex sandbox for a PATH-resolved, relative, malformed, or "
                "look-alike wrapper"
            )
            return SkillResult(skill, False, None, "", "", error=msg, provider=provider)

    # ── Load agent_definition (Stage 1) ───────────────────────────────────────
    agent_def = await asyncio.to_thread(_load_agent_def, _role)

    binary = _provider_binaries().get(provider)
    if binary is None:
        msg = (
            f"{provider} binary unavailable "
            f"(APIS_{provider.upper()}_BIN unset, not on PATH)"
        )
        log.warning(f"[apis] {skill} dispatch skipped — {msg}")
        return SkillResult(skill, False, None, "", "", error=msg, provider=provider)

    skill_path = ATELES_REPO / ".claude" / "skills" / skill / "SKILL.md"
    if not skill_path.exists():
        msg = f"SKILL.md not found at {skill_path}"
        log.error(f"[apis] {skill} dispatch skipped — {msg}")
        return SkillResult(skill, False, None, "", "", error=msg, provider=provider)

    # ── Model tiering (operator ruling 2026-09-29, model_tiering.py) ──────────
    # Resolved AFTER the binary/SKILL.md checks above (so a genuinely missing
    # binary or skill is diagnosed as that, not misdirected toward tiering
    # config) but BEFORE the degraded-dispatch event below, so every
    # harness_event path — degraded, start, success, failure, timeout —
    # carries the same tiering decision for this dispatch. `model` (an
    # explicit caller override) always wins; otherwise an `action_class`
    # resolves a tier via the live action_policy (escalated by
    # `escalation_signals`), and the tier resolves to a model via the live
    # vendor_binding for THIS provider. No `action_class` at all reproduces
    # exact prior behaviour (no tier resolved, no model requested).
    resolved_tier: model_tiering.ResolvedTier | None = precomputed_tier
    resolved_model = model
    if resolved_model is None and (action_class is not None or resolved_tier is not None):
        if resolved_tier is None:
            resolved_tier = model_tiering.resolve_tier(
                action_class, signals=escalation_signals
            )
    # claude-local runs the model its own local_provider config names (its
    # command is built separately and takes no --model), so vendor_binding
    # has nothing to resolve for it: the tier is still recorded on its events,
    # but a missing binding must not refuse the local attempt and push
    # mechanical work onto a frontier provider.
    if (
        resolved_model is None
        and resolved_tier is not None
        and provider != local_provider.LOCAL_PROVIDER
    ):
        try:
            resolved_model = model_tiering.model_for_tier(provider, resolved_tier.tier)
        except model_tiering.UnboundTierError as exc:
            msg = str(exc)
            log.error(f"[apis] {skill} dispatch refused — {msg}")
            try:
                await asyncio.to_thread(
                    _write_harness_event,
                    task_entity_id=task_entity_id,
                    agent_session_id=agent_session_id,
                    role=_role,
                    agent_sub=agent_def.aauth_sub,
                    event_type="subprocess",
                    tool_name=f"{provider}:{skill}",
                    success="false",
                    output_summary=msg[:500],
                    resolved_tier=resolved_tier,
                )
            except Exception as write_exc:  # noqa: BLE001
                log.debug(f"[apis] unbound-tier harness_event write failed: {write_exc}")
            return SkillResult(skill, False, None, "", "", error=msg, provider=provider)

    # ── Fail closed on providers that cannot deny a specific MCP tool ──────────
    # Falco's REQUEST_CHANGES on PR #1181: the `claude` adapter can put
    # `correct` on a CLI deny list (`GATE_OWNER_DENIED_TOOLS`, applied below),
    # which takes precedence over the `mcp__mcpsrv_neotoma__*` wildcard every
    # dispatch grants. Neither other adapter has an equivalent mechanism in
    # this codebase today:
    #   - `codex exec` here is launched with `--sandbox workspace-write` and
    #     no `--mcp-config` / tool-allowlist flag at all (see
    #     `_provider_command`) — there is no per-tool grant to narrow.
    #   - `cursor-agent --print` is launched with `--force --approve-mcps`
    #     (see `_provider_command`), which auto-approves EVERY MCP tool call
    #     with no per-tool exception.
    # A gate-owning run (`owns_pending_gate=True` — every run whose clean
    # verdict `sign_off` records) must never reach either adapter unrestricted:
    # that would silently reopen the exact sink this PR closes for `claude`,
    # on a path nobody is failing loudly on. Refuse the launch instead, with a
    # legible reason surfaced through the same `SkillResult.ok=False` +
    # `error` shape every other launch failure already uses — `swarm_dispatch`
    # already maps that shape to a public failure-class string in
    # `review_failure_class` / `_surface_failed_sign_offs`, so this reuses the
    # existing surfacing rather than inventing a new one.
    if owns_pending_gate and provider != "claude":
        msg = (
            f"{GATE_OWNER_TOOL_DENY_UNAVAILABLE}: provider {provider!r} has no "
            "mechanism in this codebase to deny a single MCP tool "
            f"({GATE_OWNER_DENIED_TOOLS[0]}) while still granting the rest of "
            "the agent's Neotoma access — refusing to launch a seated "
            "reviewer or gate-owning lens on it rather than running "
            "unrestricted (ateles#795, "
            "Falco's security review on PR #1181)."
        )
        log.error(f"[apis] {skill} dispatch refused — {msg}")
        return SkillResult(skill, False, None, "", "", error=msg, provider=provider)

    # ateles#590 security repair (PR #1334): a network-enabled dispatch must
    # never silently keep the daemon's ambient GitHub identity. Before #109,
    # `github_token=None` (the default for every call site) left
    # GITHUB_TOKEN/GH_TOKEN exactly as `_subscription_only_env` copied them
    # from `os.environ`. That was harmless while `include_github_contract`
    # (which also opens Codex network — see `network=include_github_contract`
    # below) was reachable ONLY from GitHub-triggered call sites in
    # swarm_dispatch.py, and every one of those already resolves and passes a
    # `github_token` string (possibly "") via `_token_for_agent_on_repo`. But
    # `dispatch_role.py`'s manual/programmatic entrypoint (#590) can request
    # `include_github_contract=True` on its own, and can omit `github_token`
    # entirely — reaching exactly the silent-inherit path this refusal closes.
    #
    # This is a PREFLIGHT refusal — checked and returned here, BEFORE the
    # Stage 2 "dispatch start" harness_event write and before any subprocess
    # command is built — for the same reason the owns_pending_gate refusal
    # above is: a refused dispatch must leave no audit trail suggesting work
    # was attempted, and every caller of run_skill/`_run_skill_once` expects a
    # `SkillResult` on failure, never an exception (see this module's own
    # docstring on failures never raising, and `_run_provider_attempts`'s
    # `await attempt(selected)` call, which has no try/except around it).
    if include_github_contract and not github_token:
        msg = (
            "include_github_contract=True (network-enabled GitHub delivery) "
            "requires an explicit GitHub credential binding via github_token, "
            "and none was supplied "
            f"({'omitted' if github_token is None else 'resolved to an EMPTY string'}). "
            "Refusing to spawn a network-enabled child with no scoped "
            "identity — that silent fallback to the daemon's ambient "
            "GITHUB_TOKEN/GH_TOKEN is exactly what let a PR land under the "
            "operator's own account instead of the agent's. Provision a "
            "scoped PAT for this dispatch before retrying; never proceed "
            "unauthenticated or on keyring fallback."
        )
        log.error(f"[apis] {skill} dispatch refused — {msg}")
        return SkillResult(skill, False, None, "", "", error=msg, provider=provider)

    # ateles#109 — per-agent GitHub identity on a NON-network-enabled
    # dispatch (e.g. a Neotoma-only task that still wants `gh` calls
    # attributed correctly under the requesting agent). Same
    # explicit-empty-fails-closed contract as above: a caller that passes
    # github_token="" (requested but resolved EMPTY, distinct from not
    # requested at all — see the subprocess_env block below) must not fall
    # through to the daemon's ambient identity either.
    if not include_github_contract and github_token == "":
        msg = (
            "github_token was explicitly requested for this dispatch but "
            "resolved to an EMPTY string (no <AGENT>_AGENT_PAT, "
            "ATELES_AGENT_PAT, NEOTOMA_AGENT_PAT, or GITHUB_TOKEN configured "
            "for this agent/repo). Refusing to spawn the child with the "
            "daemon's ambient GitHub identity — that silent fallback is "
            "exactly what let a PR land under the operator's own account "
            "instead of the agent's. Provision the missing PAT before "
            "retrying; never proceed unauthenticated or on keyring fallback."
        )
        log.error(f"[apis] {skill} dispatch refused — {msg}")
        return SkillResult(skill, False, None, "", "", error=msg, provider=provider)

    # ateles#795 amended ADR — this preflight refusal is RELAXED, not removed.
    #
    # It used to hard-block a gate owner from launching at all when it had no
    # per-agent Neotoma bearer (`<ROLE>_NEOTOMA_TOKEN`): the lens's own MCP
    # `correct()` was the only planned writeback path, and a write Neotoma
    # would refuse for lack of attribution was worse than never running.
    #
    # The operator's amended decision moves the SYSTEM-OF-RECORD write off the
    # lens's MCP session entirely: `swarm_dispatch` now calls
    # `IssueGateStore.sign_off()` (`gate_waive.py`) after a clean verdict,
    # which signs the write with the LENS's own AAuth keypair via
    # `neotoma_signed.signed_request` — not the lens's MCP bearer, and not the
    # daemon's shared bearer either. No prompt instructs a gate-owning lens
    # to `correct()` `gate_status` itself any more — the panel's
    # `_panelist_prompt` GATE WRITEBACK block and the additive-spec
    # pipeline's `pm_gate_block` / `_pavo_prompt` were all rewritten to state
    # a verdict via comment only (Falco's security review, PR #1181's
    # follow-up round) — so a lens's own MCP `correct()` of `gate_status` is
    # unsolicited at best, not even "advisory", and the absence of a
    # per-agent MCP bearer is no longer a reason to refuse the launch — refusing here
    # would leave pm/arch/ux permanently unable to run, which is the second
    # failure this amendment exists to fix (the operator's decision comment
    # names this explicitly: provisioning `<ROLE>_NEOTOMA_TOKEN` "routes
    # around the design rather than implementing it").
    #
    # `gate_writeback_identity_error` / `neotoma_token_for_agent` are kept
    # (still exercised by their own unit tests) for any OTHER caller that
    # still needs to refuse a write attempted as the wrong principal — this
    # call site just no longer treats their answer as a launch gate.

    try:
        skill_md = skill_path.read_text(encoding="utf-8")
    except OSError as exc:
        return SkillResult(
            skill,
            False,
            None,
            "",
            "",
            error=f"read failed: {exc}",
            provider=provider,
        )

    # ── Build system prompt (Stage 1 + Stage 5) ────────────────────────────────
    # claude-local gets the LEAN prompt (role + work-class hard rules + task),
    # never the full agent_definition + live-policy rendering: that full
    # prompt is sized for a frontier context window and does not fit a local
    # one (ateles task ent_71387d9c1d1d3d1eef9ecc01). Every other provider is
    # built exactly as before — this branch changes nothing for them.
    if provider == local_provider.LOCAL_PROVIDER:
        system_prompt = local_provider.build_lean_prompt(work_class)
        # `degraded` must reflect whether agent_def actually loaded (a stub
        # from AgentLoader's load-failure fallback has empty prompt_markdown
        # — see lib/daemon_runtime/agent_loader.py's `_stub()`), the same
        # condition build_system_prompt uses below, NOT whether the lean
        # prompt happens to omit prompt_markdown by design. Getting this
        # wrong would silently skip the undefined-role alerting AND let a
        # stub's synthesized aauth_sub receive AAuth signing-key injection
        # further down (`if not degraded and agent_def.aauth_sub:`).
        degraded = not (agent_def.prompt_markdown or "").strip()
    else:
        # Fetch policy at dispatch time: unlike the stable agent definition,
        # active rules may change between tasks and must not inherit the
        # definition cache.
        policy_prompt = await asyncio.to_thread(_load_active_policy_prompt, _role)
        system_prompt, degraded = build_system_prompt(
            agent_def,
            skill_md,
            include_github_contract=include_github_contract,
            policy_prompt=policy_prompt,
        )

    if degraded:
        _title_hint = prompt[:80].replace("\n", " ")
        warn_msg = (
            f"Role {_role!r} ran DEGRADED (no agent_definition loaded) "
            f"for task {task_entity_id or '(unknown)'!r}. "
            "Dispatching with SKILL.md and any available live policy."
        )
        log.warning(f"[apis] {warn_msg}")
        # A role with no agent_definition is ONE fact about that ROLE, not one
        # per task that happens to route to it. Notifying per task paged the
        # operator N times for a single missing definition; dedup on the role so
        # it escalates once (and again after the ledger's re-assert window, so a
        # role that stays undefined does not go quiet forever).
        _should_notify = True
        try:
            from unroutable_ledger import shared_ledger

            # The SHARED instance — never a second UnroutableLedger() on the same
            # file. Two instances each save their own stale view and drop each
            # other's records (Loxia review, ateles#656).
            _should_notify = shared_ledger().note_undefined_role(str(_role))
        except Exception as exc:  # noqa: BLE001 — dedup must never block the warning
            log.debug(f"[apis] undefined-role dedup unavailable: {exc}")
        if notifier is not None and _should_notify:
            try:
                from lib.notify import Priority

                notifier.send(
                    f"Role {_role!r} has no agent_definition — every task routed "
                    "to it runs DEGRADED (SKILL.md only). Reported once per role.",
                    priority=Priority.WARN,
                    handler="apis",
                )
            except Exception as exc:
                log.debug(f"[apis] notifier.send failed: {exc}")

        # Stage 5: degraded harness_event
        try:
            await asyncio.to_thread(
                _write_harness_event,
                task_entity_id=task_entity_id,
                agent_session_id=agent_session_id,
                role=_role,
                agent_sub=agent_def.aauth_sub,
                event_type="subprocess",
                tool_name=f"{provider}:{skill}",
                success="partial",
                input_summary=_title_hint,
                output_summary="degraded_generic_subagent",
                resolved_tier=resolved_tier,
            )
        except Exception as exc:
            log.debug(f"[apis] degraded harness_event write failed: {exc}")

    # ── Build provider command ─────────────────────────────────────────────────
    # A dispatch carrying the GitHub contract is one whose task involves
    # commit/push/PR — the delivery path #590 is about. Everything else runs
    # with the sandbox's default network denial.
    local_cfg = None
    if provider == local_provider.LOCAL_PROVIDER:
        # claude-local: refuse before launch when the prompt cannot fit the
        # local window or the guard hooks cannot be bound. Either refusal is a
        # launch failure, so `_run_provider_attempts` falls over to frontier.
        local_cfg = local_provider.load_config()
        refusal = (
            local_provider.ceiling_refusal(system_prompt, prompt, local_cfg)
            if local_cfg is not None
            else f"{provider} is not configured"
        )
        guards_path = ""
        if refusal is None:
            # ATELES_REPO_PATH unset means ATELES_REPO (module-level,
            # resolved once at import) silently fell back to ~/repos/ateles —
            # the shared interactive clone, not the deployment checkout the
            # daemon actually runs from. Reading guards from ATELES_REPO in
            # that case would bind the launch to whatever state that clone
            # happens to be in and proceed on unverified guards rather than
            # refuse. Building guards_repo from the env var read here (not
            # from the separately-resolved ATELES_REPO) keeps the refuse/
            # proceed decision and the path guards are actually read from as
            # one value instead of two independent reads of the same var.
            repo_path_env = os.environ.get("ATELES_REPO_PATH", "").strip()
            if not repo_path_env:
                refusal = (
                    f"{local_provider.FAILURE_GUARDS}: ATELES_REPO_PATH is not "
                    "set — refusing to read guards from the ATELES_REPO "
                    f"fallback ({ATELES_REPO}); set ATELES_REPO_PATH to the "
                    "checkout whose guards this dispatch must bind"
                )
            else:
                log.info(
                    f"[apis] {skill} claude-local guards read from "
                    f"ATELES_REPO_PATH={repo_path_env}"
                )
                try:
                    guards_path = local_provider.write_guards_file(Path(repo_path_env))
                except (local_provider.LocalProviderError, OSError) as exc:
                    refusal = f"{local_provider.FAILURE_GUARDS}: {exc}"
        if refusal is not None:
            msg = f"{provider} launch failed: {refusal}"
            log.warning(f"[apis] {skill} dispatch skipped — {msg}")
            return SkillResult(skill, False, None, "", "", error=msg, provider=provider)
        cmd = local_provider.build_command(
            binary, system_prompt, agent_def.tools, local_cfg, guards_path
        )
        stdin_payload = None
    else:
        cmd, stdin_payload = _provider_command(
            provider,
            binary,
            system_prompt,
            prompt,
            cwd=cwd,
            network=include_github_contract,
            codex_outer_sandboxed=codex_outer_sandboxed,
            model=resolved_model,
        )
    if command_wrapper:
        # Prepended to the REAL argv that create_subprocess_exec below will
        # run — not a parallel description of a guard, the guard itself. See
        # this parameter's docstring on _run_skill_once.
        cmd = [*command_wrapper, *cmd]

    local_review_home = (
        (env_extra or {}).get("ATELES_LOCAL_REVIEW_HOME", "") if local_review else ""
    )
    if local_review:
        if not local_review_home or not Path(local_review_home).is_absolute():
            raise RuntimeError(
                "local_review dispatch requires an absolute "
                "ATELES_LOCAL_REVIEW_HOME supplied by the caller"
            )
        if github_token is not None:
            raise RuntimeError(
                "local_review dispatch cannot accept github_token; publication "
                "authority belongs exclusively to the parent"
            )
        (Path(local_review_home) / ".config" / "gh").mkdir(parents=True, exist_ok=True)

    # ── Stage 6: inject Neotoma MCP config so dispatched child can reach Neotoma ─
    # Dispatched `claude --print` children inherit the ambient Claude MCP config,
    # but in the daemon's context (ateles project scope) there is no neotoma MCP
    # server entry. Without it, role agents (Lanius/Pavo) cannot load
    # workflow_definition, init gate_status, or store plan_contribution — they
    # exit rc=0 without completing their Neotoma-dependent protocols.
    #
    # We inject a --mcp-config pointing the child at the local Neotoma HTTP MCP
    # endpoint (NEOTOMA_BASE_URL/mcp + bearer auth). We do NOT use
    # --strict-mcp-config so any other MCP servers the agent legitimately has
    # (from its own ambient config) are preserved; we only ADD neotoma.
    #
    # MCP tool allowlist syntax (ateles#1687 finding):
    #   claude --print --allowed-tools accepts "mcp__<servername>__*" as a wildcard
    #   that permits all tools from the named MCP server. The double-underscore
    #   separator matches the mcp__<server>__<tool> naming convention Claude uses
    #   internally. The server name must exactly match the key in mcpServers.
    #   So for {"mcpServers": {"mcpsrv_neotoma": ...}} the entry is
    #   "mcp__mcpsrv_neotoma__*" — matching the convention used across all 31 agent
    #   SKILL.md files and 24 agent_definition tool_allowlists in this codebase.
    #
    # Security tradeoff:
    #   Passing the bearer token as an inline JSON string in --mcp-config would
    #   expose it in the child's argv (visible via `ps aux`). Instead, we write
    #   the config to a mode-0600 temp file and pass the file path to --mcp-config.
    #   The temp file is cleaned up in a try/finally after the subprocess exits.
    _mcp_tmp_path: str | None = None
    if provider == "claude" and local_review:
        # Claude otherwise merges project/user MCP configuration. A local
        # review child has no Neotoma publication role at all, so give it an
        # explicitly empty, strict MCP universe rather than relying on absent
        # tokens alone. The parent process retains its own MCP/GitHub authority.
        fd, _mcp_tmp_path = tempfile.mkstemp(
            suffix=".json", prefix="apis_local_review_mcp_"
        )
        os.chmod(_mcp_tmp_path, 0o600)
        with os.fdopen(fd, "w") as _f:
            json.dump({"mcpServers": {}}, _f)
        cmd += ["--mcp-config", _mcp_tmp_path, "--strict-mcp-config"]
    elif provider == "claude":
        _neotoma_base = os.environ.get("NEOTOMA_BASE_URL", "").rstrip("/")
        # ateles#795: prefer the ROLE's own Neotoma principal. Falls back to the
        # shared daemon bearer, so every agent without its own credential behaves
        # exactly as before. A gate owner is NOT refused here for lacking one
        # (amended ADR): the system-of-record write is now the dispatcher's
        # `IssueGateStore.sign_off`, signed with the lens's own AAuth key,
        # independent of this MCP session's bearer. The prompt no longer
        # instructs a gate-owning lens to `correct()` `gate_status` itself (the
        # GATE WRITEBACK block was removed from `_panelist_prompt`), and
        # `mcp__mcpsrv_neotoma__correct` is no longer pre-approved for this
        # session either (see `GATE_WRITEBACK_TOOLS`) — an unsolicited `correct`
        # attempt on `gate_status` would fall back to an unanswerable
        # `default`-mode approval prompt, never a clearance path.
        _neotoma_token, _ = neotoma_token_for_agent(_role)
        if not _neotoma_token:
            # Unlike the harness_event writer above, this is not a
            # best-effort diagnostic — it constructs the MCP config the
            # spawned child actually connects with. Neotoma's /mcp endpoint
            # requires auth (verified live: an unauthenticated POST to
            # /mcp returns HTTP 401 with "Unauthorized: Authentication
            # required"), so a missing token here is not a degraded-but-
            # usable config — it is a config that guarantees the child's
            # own MCP handshake fails, far from this call site and with no
            # indication the cause was an empty env var in the parent.
            # Raise here, matching _require_neotoma_base_url's convention,
            # so the failure is attributed to its actual cause. This applies
            # regardless of which tier (role-owned or shared daemon bearer)
            # neotoma_token_for_agent() resolved from — both can be empty.
            raise RuntimeError(
                "NEOTOMA_BEARER_TOKEN is not set; refusing to construct an "
                "--mcp-config for the dispatched child. Neotoma's /mcp "
                "endpoint requires authentication, so a config built "
                "without a bearer token would only fail later, inside the "
                "child's own MCP handshake, with no trace back to this "
                "cause. Under launchd the plist supplies this; for an "
                "ad-hoc run, export it or source ~/.config/neotoma/.env "
                "first."
            )
        _mcp_cfg: dict = {
            "mcpServers": {
                "mcpsrv_neotoma": {
                    "type": "http",
                    "url": f"{_neotoma_base}/mcp",
                    "headers": {"Authorization": f"Bearer {_neotoma_token}"},
                }
            }
        }

        # Write the MCP config to a mode-0600 temp file to avoid argv exposure.
        try:
            fd, _mcp_tmp_path = tempfile.mkstemp(suffix=".json", prefix="apis_mcp_")
            os.chmod(_mcp_tmp_path, 0o600)
            with os.fdopen(fd, "w") as _f:
                json.dump(_mcp_cfg, _f)
            cmd += ["--mcp-config", _mcp_tmp_path]
            log.debug(
                f"[apis] Injected --mcp-config {_mcp_tmp_path} "
                "(mcpsrv_neotoma HTTP MCP)"
            )
        except Exception as exc:
            # Non-fatal: proceed without injection rather than abort.
            log.warning(
                f"[apis] Could not write MCP config temp file (non-fatal): {exc}"
            )
            _mcp_tmp_path = None

    tools = agent_def.tools  # property: list[str]; ['*'] means all
    # ateles#795, Falco's REQUEST_CHANGES on PR #1181: for a gate-owning run,
    # `correct` goes on the CLI's DENY list, never relying on the additive
    # allowlist alone. `--disallowed-tools` takes precedence over
    # `--allowed-tools` (verified against this installed CLI's `--help`,
    # which documents both flags; Claude Code's allow/deny precedence is
    # deny-wins, matching every other permission layer in the product — see
    # the PR comment for the reviewer-run provider table), so this closes the
    # wildcard/`*` gap the additive `GATE_WRITEBACK_TOOLS` fix left open: a
    # gate owner's session can no longer clear `gate_status` over the shared
    # bearer no matter what its allowlist otherwise grants.
    disallowed_list = list(GATE_OWNER_DENIED_TOOLS) if owns_pending_gate else []
    if provider == "claude" and tools != ["*"]:
        # --allowed-tools is confirmed present in `claude --print --help`
        # (alias: --allowedTools). Accepts comma- or space-separated tool names.
        # MCP server tools use the "mcp__<servername>__*" wildcard form, where the
        # server name matches the mcpServers key (here: "mcpsrv_neotoma" — the
        # universal convention across all 31 agent SKILLs and 24 agent_definitions).
        # This allows all tools from that MCP server without enumerating them individually.
        allowed_list = [
            tool for tool in tools if not (local_review and tool.startswith("mcp__"))
        ]
        if not local_review and "mcp__mcpsrv_neotoma__*" not in allowed_list:
            allowed_list.append("mcp__mcpsrv_neotoma__*")
        # ateles#795: name the two read-only gate tools explicitly even though
        # the `mcp__mcpsrv_neotoma__*` wildcard above nominally covers them —
        # exact tool names are the durable statement of intent, not reliance on
        # a wildcard that could later narrow. `correct` is deliberately absent
        # (see `GATE_WRITEBACK_TOOLS`'s docstring): the dispatcher's signed
        # `sign_off` is the system-of-record write now, and this session's own
        # `correct()` of `gate_status` must NOT be pre-approved — and, for a
        # gate-owning run, is additionally on the deny list below regardless
        # of the wildcard.
        if not local_review:
            allowed_list = gate_writeback_allowlist(allowed_list)
        allowed = ",".join(allowed_list)
        cmd += ["--allowed-tools", allowed]
        log.info(
            f"[apis] Spawning via {provider}: "
            f"<{_role}:agent_def+{skill}.SKILL.md> "
            f"--allowed-tools {allowed} timeout={timeout}s"
        )
    elif provider == "claude":
        # tools == ['*'] — "all tools", which previously meant NO
        # `--allowed-tools` flag at all. That is not the permissive state it
        # reads as: with no flag the child stays in `default` permission mode,
        # where every MCP write tool prompts, and a headless `--print` child
        # cannot answer a prompt. So the most-trusted agents were the ones
        # whose gate writeback was surest to be denied (ateles#795).
        #
        # Granting the two read-only gate tools by name is a courtesy grant for
        # situational awareness, not a writeback fix — `correct` on
        # `gate_status` is deliberately NOT pre-approved (see
        # `GATE_WRITEBACK_TOOLS`'s docstring: the dispatcher's signed
        # `sign_off` is the system-of-record write now). This does NOT narrow
        # the agent: the `*` wildcard is preserved as the first entry, so every
        # other tool the agent had remains available exactly as before. For a
        # gate-owning run, `correct` is additionally denied below — the `*`
        # wildcard on its own left `correct` reachable, which is exactly the
        # sink Falco's review confirmed on PR #1181.
        allowed = ",".join(["*"] if local_review else gate_writeback_allowlist(["*"]))
        cmd += ["--allowed-tools", allowed]
        log.info(
            f"[apis] Spawning via {provider}: "
            f"<{_role}:{'agent_def+' if not degraded else 'degraded-'}{skill}.SKILL.md> "
            f"--allowed-tools {allowed} timeout={timeout}s"
        )
    if provider == "claude" and disallowed_list:
        cmd += ["--disallowed-tools", ",".join(disallowed_list)]
        log.info(
            f"[apis] Spawning via {provider}: <{_role}:{skill}.SKILL.md> "
            f"--disallowed-tools {','.join(disallowed_list)} "
            "(seated reviewer or gate-owning run — correct() denied "
            "regardless of allowlist)"
        )
    if provider != "claude":
        log.info(
            f"[apis] Spawning via {provider}: "
            f"<{_role}:{'agent_def+' if not degraded else 'degraded-'}{skill}.SKILL.md> "
            f"timeout={timeout}s"
        )

    # ── Tier observability (operator ruling 2026-09-29) ────────────────────────
    # One log line and one ledger row per dispatch attempt: the tier, why, and
    # the model actually requested. An un-tiered dispatch is logged as such
    # (`tiering=untiered(no_action_class)`), never omitted, so a call site that
    # forgot to name an action class shows up here instead of hiding.
    log.info(
        f"[apis] {skill} via {provider}: "
        + model_tiering.record_dispatch(
            skill=skill, provider=provider, resolved=resolved_tier,
            model=resolved_model,
        )
    )

    # ── Stage 2: harness_event at dispatch start ───────────────────────────────
    try:
        await asyncio.to_thread(
            _write_harness_event,
            task_entity_id=task_entity_id,
            agent_session_id=agent_session_id,
            role=_role,
            agent_sub=agent_def.aauth_sub,
            event_type="subprocess",
            tool_name=f"{provider}:{skill}",
            success="partial",  # "partial" = in-flight / started
            input_summary=prompt[:200],
            resolved_tier=resolved_tier,
        )
    except Exception as exc:
        log.debug(f"[apis] start harness_event write failed (non-fatal): {exc}")

    # Hard boundary from the approved plan: all three adapters use bundled
    # subscription auth by default. API-key credentials are removed so a capped
    # plan queues/fails over instead of silently spending metered tokens.
    subprocess_env = _subscription_only_env(
        env_extra,
        local_review=local_review,
        local_review_home=local_review_home or None,
    )
    if local_cfg is not None:
        # Local inference: point the CLI at the loopback proxy and strip any
        # frontier OAuth credential so it cannot be sent there.
        local_provider.apply_env(subprocess_env, local_cfg)

    # ateles#109 / ateles#590 (PR #1334): inject the resolved per-agent GitHub
    # identity, if any. The fail-closed refusals for a network-enabled
    # dispatch with no token, and for an explicitly-empty token on any
    # dispatch, already returned a SkillResult in the preflight block above —
    # by this point github_token is either None (not requested; ambient env
    # passes through unchanged, exact pre-#109 behaviour) or a non-empty
    # scoped string to inject. When include_github_contract is True, the
    # ambient GITHUB_TOKEN/GH_TOKEN are stripped first so no path leaves the
    # daemon's own identity in a network-enabled child's env.
    if include_github_contract:
        subprocess_env.pop("GITHUB_TOKEN", None)
        subprocess_env.pop("GH_TOKEN", None)
    if github_token:
        subprocess_env["GITHUB_TOKEN"] = github_token
        subprocess_env["GH_TOKEN"] = github_token

    # Stage 3 (ateles#94): inject the Neotoma AAuth client signer env vars so
    # the dispatched child can sign its own Neotoma writes as <role>@ateles-swarm.
    # The Neotoma client signer (aauth_client_signer.ts) reads three vars:
    #   NEOTOMA_AAUTH_PRIVATE_JWK_PATH — path to the EC/P-256 JWK keypair file
    #   NEOTOMA_AAUTH_SUB              — the signing subject (e.g. cicada@ateles-swarm)
    #   NEOTOMA_AAUTH_ISS              — the issuer (https://markmhendrickson.com)
    # We only inject when the role JWK file actually exists at the expected path;
    # if it is absent the child proceeds unsigned (graceful degradation, as today).
    # When degraded (empty prompt_markdown) we inject nothing — child runs unsigned.
    #
    # SCOPE (ateles#795): these vars reach only code paths that shell out to the
    # TypeScript client signer (`lib/daemon_runtime/neotoma_signed.py`). They do
    # NOT govern the child's Neotoma MCP connection, which authenticates once
    # with the static Authorization header built above — an MCP session cannot
    # carry a per-request signature. So a present JWK here is not evidence that
    # a child's MCP writes are attributed to the role; the header is. Reading
    # these three vars as "the lens writes as itself" is what let the shared
    # bearer pass for a per-lens identity while ateles#795 stayed open.
    if not local_review and not degraded and agent_def.aauth_sub:
        keys_dir = os.environ.get("ATELES_PRIVATE_KEYS_DIR", "")
        if keys_dir:
            jwk_path = os.path.join(keys_dir, f"{_role}.jwk.json")
            if os.path.exists(jwk_path):
                subprocess_env["NEOTOMA_AAUTH_PRIVATE_JWK_PATH"] = jwk_path
                subprocess_env["NEOTOMA_AAUTH_SUB"] = agent_def.aauth_sub
                subprocess_env["NEOTOMA_AAUTH_ISS"] = os.environ.get(
                    "NEOTOMA_AAUTH_ISS", "https://markmhendrickson.com"
                )

    _start_ns = time.monotonic_ns()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=subprocess_env,
            cwd=cwd,  # QE3: qa lens runs in a PR-branch worktree
            start_new_session=True,
        )
    except OSError as exc:
        if _mcp_tmp_path is not None:
            try:
                os.unlink(_mcp_tmp_path)
            except OSError:
                pass
        msg = f"{provider} launch failed: {exc}"
        log.warning(f"[apis] {skill} dispatch skipped — {msg}")
        return SkillResult(
            skill,
            False,
            None,
            "",
            "",
            error=msg,
            provider=provider,
        )

    try:
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(
                    input=stdin_payload
                    if stdin_payload is not None
                    else prompt.encode()
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            await _kill_spawned_process_group(proc)
            duration_ms = int((time.monotonic_ns() - _start_ns) / 1_000_000)
            msg = f"timed out after {timeout}s"
            log.error(f"[apis] {skill} dispatch {msg}")

            # Stage 2: harness_event on timeout (failure)
            try:
                await asyncio.to_thread(
                    _write_harness_event,
                    task_entity_id=task_entity_id,
                    agent_session_id=agent_session_id,
                    role=_role,
                    agent_sub=agent_def.aauth_sub,
                    event_type="subprocess",
                    tool_name=f"{provider}:{skill}",
                    success="false",
                    output_summary=f"timeout after {timeout}s",
                    duration_ms=duration_ms,
                    # A killed child's stdout is discarded, so no token counts
                    # exist for a timeout — but the provider and the requested
                    # model still do, and a dispatch that ran to the full
                    # timeout is the most expensive kind there is. Recording
                    # provider+model here is what makes "which model keeps
                    # timing out" answerable at all.
                    usage=parse_dispatch_usage(
                        provider,
                        "",
                        requested_model=_requested_model(provider, cmd),
                    ),
                    resolved_tier=resolved_tier,
                )
            except Exception as exc:
                log.debug(f"[apis] timeout harness_event write failed: {exc}")

            # ateles#257 — a timed-out dispatch is the same silent failure class;
            # route it through the same rate-limited operator notification.
            notify_dispatch_failure(
                notifier,
                skill=skill,
                role=_role,
                returncode=None,
                stderr=msg,
                task_entity_id=task_entity_id,
            )

            return SkillResult(
                skill,
                False,
                None,
                "",
                "",
                error=msg,
                provider=provider,
            )

        duration_ms = int((time.monotonic_ns() - _start_ns) / 1_000_000)
        _stdout_text = stdout.decode("utf-8", errors="replace")
        _stderr_text = stderr.decode("utf-8", errors="replace")

        # ── Delivery-failure detection (ateles#590) ──────────────────────────────
        # A child that could not commit or push exits 0: it did everything it
        # was permitted to do, and says so plainly in its own output. Reading
        # only the exit code turns that into `ok: true` over an undelivered
        # change — a dispatch that reports success while delivering nothing.
        # The exit code is therefore necessary but not sufficient: a run is ok
        # only if the process succeeded AND nothing in its output says the
        # sandbox refused the delivery.
        # STDERR ONLY. git writes these lines to stderr; an agent that merely
        # QUOTES one — reading this PR, an issue, or a transcript — emits it on
        # stdout as prose. Scanning both made "the child read about a denial"
        # indistinguishable from "the child was denied", and the reproduction
        # transcript in #601's own body is a verbatim instance of that.
        _delivery_reasons = _delivery_failure_reasons(_stderr_text)
        _delivery_conflicts = _delivery_failure_conflicts(
            _delivery_reasons, _stderr_text
        )
        _delivery_denial = _delivery_diagnostic_error(
            _delivery_reasons, _delivery_conflicts
        )
        _delivery_only_reason = (
            _delivery_reasons[0]
            if (
                len(_delivery_reasons) == 1
                and not _delivery_conflicts
                and proc.returncode == 0
            )
            else ""
        )
        if _delivery_denial and proc.returncode == 0:
            log.error(
                f"[apis] {skill} dispatch via {provider} exited 0 but could not "
                f"deliver: {_delivery_denial}"
            )

        # ── Local post-condition check (ateles task ent_71387d9c1d1d3d1eef9ecc01) ──
        # A local run that exits 0 with a syntactically fine reply is not
        # necessarily a CORRECT one — the regression this exists to catch was
        # exactly that: rc=0, plausible prose, and a wrong count. Checked only
        # for claude-local, only for a work class with a real ground truth
        # (local_provider._POSTCONDITION_CHECKS); every other provider and
        # every other work class is unaffected.
        _postcondition_failure = (
            local_provider.verify_postcondition(work_class, _stdout_text, cwd=cwd)
            if provider == local_provider.LOCAL_PROVIDER
            else None
        )
        if _postcondition_failure:
            log.error(
                f"[apis] {skill} dispatch via {provider} exited 0 but failed "
                f"its post-condition check: {_postcondition_failure}"
            )

        # ── Per-dispatch usage attribution ───────────────────────────────────────
        # Parsed from what the harness already emitted; never estimated. Under
        # the swarm's text-mode invocations most harnesses report no token
        # counts, in which case this records provider + model_source and leaves
        # the token fields absent rather than writing a fabricated zero.
        _usage = await asyncio.to_thread(
            parse_dispatch_usage,
            provider,
            _stdout_text,
            requested_model=_requested_model(provider, cmd),
        )

        result = SkillResult(
            skill=skill,
            ok=(
                proc.returncode == 0
                and not _delivery_denial
                and _postcondition_failure is None
            ),
            returncode=proc.returncode,
            stdout=_stdout_text,
            stderr=_stderr_text,
            provider=provider,
            error=(
                _delivery_denial
                if (_delivery_denial and proc.returncode == 0)
                else (_postcondition_failure or "")
            ),
            delivery_failure_reason=_delivery_only_reason,
            delivery_failure_reasons=(
                _delivery_reasons if proc.returncode == 0 else ()
            ),
            delivery_failure_conflicts=(
                _delivery_conflicts if proc.returncode == 0 else ()
            ),
            usage=_usage,
        )

        # ── Dropped-allowlist-rule notification (ateles#255) ──────────────────────
        # Checked regardless of exit code: the CLI logs "Ignoring --allowedTools
        # rule" and continues, so a drop can coexist with rc=0. One batched alert
        # per dispatch, not one per rule. Off-loaded to a thread (like the
        # harness_event writes below) so an unusually large stderr blob can't
        # block the event loop for other concurrent dispatches.
        dropped_rules = (
            await asyncio.to_thread(_find_dropped_allowlist_rules, result.stderr)
            if provider == "claude"
            else []
        )
        if dropped_rules:
            _notify_dropped_allowlist_rules(
                notifier, role=_role, rules=dropped_rules, returncode=proc.returncode
            )

        # ── Stage 2: harness_event at completion ──────────────────────────────────
        if result.ok:
            log.info(
                f"[apis] {skill} dispatch via {provider} ok "
                f"({len(result.stdout)}B stdout) [{_usage.summary()}]"
            )
            try:
                await asyncio.to_thread(
                    _write_harness_event,
                    task_entity_id=task_entity_id,
                    agent_session_id=agent_session_id,
                    role=_role,
                    agent_sub=agent_def.aauth_sub,
                    event_type="subprocess",
                    tool_name=f"{provider}:{skill}",
                    success="true",
                    output_summary=(
                        f"provider={provider} {len(result.stdout)}B stdout rc=0"
                        f" {_usage.summary()}"
                    ),
                    duration_ms=duration_ms,
                    usage=_usage,
                    resolved_tier=resolved_tier,
                )
            except Exception as exc:
                log.debug(f"[apis] success harness_event write failed: {exc}")
        else:
            # ateles#257 — persist the COMPLETE stdout+stderr before anything
            # truncates them, then name that file everywhere the failure surfaces.
            failure_log_path = await asyncio.to_thread(
                write_dispatch_failure_log,
                skill=skill,
                role=_role,
                returncode=proc.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                task_entity_id=task_entity_id,
                cmd=cmd,
                cwd=cwd,
                duration_ms=duration_ms,
            )
            path_note = failure_log_path or "(diagnostics file unavailable)"

            log.error(
                f"[apis] {skill} dispatch via {provider} failed "
                f"(rc={proc.returncode}); "
                f"full output: {path_note} "
                f"(stdout {len(result.stdout)}B, stderr {len(result.stderr)}B); "
                f"stderr head: {result.stderr[:500]}"
            )
            try:
                await asyncio.to_thread(
                    _write_harness_event,
                    task_entity_id=task_entity_id,
                    agent_session_id=agent_session_id,
                    role=_role,
                    agent_sub=agent_def.aauth_sub,
                    event_type="subprocess",
                    tool_name=f"{provider}:{skill}",
                    success="false",
                    output_summary=(
                        f"provider={provider} rc={proc.returncode} "
                        f"{_usage.summary()} "
                        f"full_output={path_note} "
                        f"{result.stderr[:200]}"
                    ),
                    duration_ms=duration_ms,
                    # A failed dispatch still spent tokens. Recording usage only
                    # on success would systematically under-count exactly the
                    # dispatches most likely to have burned a retry loop.
                    usage=_usage,
                    resolved_tier=resolved_tier,
                )
            except Exception as exc:
                log.debug(f"[apis] failure harness_event write failed: {exc}")

            # ateles#257 — a dispatch failure must reach the operator, not just a
            # log file. Rate-limited so a swarm-wide breakage is one signal.
            if _provider_failure_kind(result.stdout, result.stderr) is None:
                notify_dispatch_failure(
                    notifier,
                    skill=skill,
                    role=_role,
                    returncode=proc.returncode,
                    stderr=result.stderr,
                    task_entity_id=task_entity_id,
                    log_path=failure_log_path,
                )

        return result

    finally:
        # Clean up the MCP config temp file (always, even on timeout/exception).
        if _mcp_tmp_path is not None:
            try:
                os.unlink(_mcp_tmp_path)
            except OSError:
                pass


def usable_providers() -> set[str]:
    """Providers that could actually serve a run right now.

    The same view `run_skill` routes over: configured order ∩ providers whose
    binary resolves, minus those cooling down after a capacity/auth failure.
    Callers use this to decide whether a per-lens provider PREFERENCE can be
    honored, so that pinning never turns into "the lens silently did not run"
    (review_panel.resolve_lens_provider).
    """
    return usable_provider_names(_provider_binaries())


async def run_skill(
    skill: str,
    prompt: str,
    *,
    role: str | None = None,
    task_entity_id: str = "",
    agent_session_id: str = "",
    timeout: int | None = None,
    env_extra: dict[str, str] | None = None,
    notifier=None,
    github_token: str | None = None,
    include_github_contract: bool = False,
    cwd: str | None = None,
    provider: str | None = None,
    preferred_provider: str | None = None,
    owns_pending_gate: bool = False,
    seated_reviewer: bool = False,
    command_wrapper: list[str] | None = None,
    codex_outer_sandboxed: bool = False,
    local_review: bool = False,
    work_class: str | None = None,
    action_class: str | None = None,
    escalation_signals: "model_tiering.EscalationSignals | None" = None,
    model: str | None = None,
) -> SkillResult:
    """Route one skill run across subscription-backed harness providers.

    ``command_wrapper``: forwarded verbatim to ``_run_skill_once`` on every
    attempt — see that function's docstring. Unrelated to provider selection;
    it wraps whichever provider's binary ends up chosen.

    ``codex_outer_sandboxed`` is the paired adapter signal for an already
    probed outer ``sandbox-exec`` wrapper. `_run_skill_once` validates the pair
    before choosing Codex's no-inner-sandbox mode.

    ``local_review`` selects a child environment with inference authority but
    no ambient GitHub or Neotoma publication authority. The parent retains and
    independently applies any publication gate.

    The first candidate is selected with smooth weighted round-robin using the
    operator-supplied headroom estimates. Capacity, authentication, and launch
    failures cool that provider down and immediately try the next eligible CLI.
    Ordinary task failures and timeouts do not fail over because replaying a
    side-effecting task on another provider could duplicate work.

    Passing ``provider`` pins the invocation to one adapter, primarily for
    diagnostics and focused tests.

    ``owns_pending_gate`` (ateles#795): True when this run is seated because it
    OWNS a pending pre-impl gate (pm/arch/ux). Historically this refused the
    run outright when the role had no per-agent Neotoma credential, because
    the lens's own in-session `correct()` was the only writeback path and a
    write Neotoma would refuse for lack of attribution was worse than not
    running. The amended ADR moves the system-of-record write to the
    dispatcher (`gate_waive.IssueGateStore.sign_off`, signed with the lens's
    own AAuth key, independent of this session), so this flag no longer gates
    the launch — it is threaded through to `_run_skill_once` for any caller
    that still wants to distinguish a gate-owning run from an advisory one
    (e.g. logging), and MUST NOT be reintroduced as a hard launch refusal
    without re-litigating the amended ADR on ateles#795.

    ``seated_reviewer`` (PR #1181, dispatcher security run at e874537f,
    BLOCKING `incomplete_class_sweep`): True for EVERY lens the dispatcher
    seats on a PR panel, an issue-spec section, a missing-lens re-run, or a
    fix-guidance round, whether or not it owns a pending gate. Each such run
    gets the `mcp__mcpsrv_neotoma__*` wildcard over the shared daemon bearer,
    so an advisory seat (security, content, ...) or a gate owner re-seated
    after its gate cleared could otherwise still `correct` the shared
    `gate_status` map. It carries the same controls as a gate-owning run:
    `correct` on the CLI deny list, and claude-only routing, since no other
    adapter here can deny a single MCP tool. No seated lens needs `correct`
    for anything but gate state: they file findings through `store`.

    ``work_class`` names the kind of work (``local_provider.MECHANICAL_WORK_CLASSES``).
    When it is a configured mechanical class and the run is unpinned and not a
    seated reviewer, ``claude-local`` is tried first. A local failure falls over
    ONLY to a frontier provider with a model bound to the cheapest frontier tier
    (``LOCAL_FALLBACK_TIER``) in the ``vendor_binding``, pinned to that model;
    with none bound the run is refused and recorded as a local failure, never
    replayed on a provider's ambient default. An explicit ``model`` is the
    caller's own choice and is used for the fallback as given. Any other value,
    or None, leaves routing exactly as before.

    ``action_class``/``escalation_signals``/``model`` (operator ruling
    2026-09-29, model_tiering.py): the TIER is resolved exactly ONCE here
    (when ``model`` is not already an explicit override), before any provider
    is attempted, and that same resolved tier is passed to every
    ``_run_skill_once`` attempt in the failover loop — only the
    provider-specific model-for-tier lookup runs per attempt. This closes a
    race a per-attempt re-resolution would otherwise have: without pinning
    the tier once, an action_policy/vendor_binding edit landing in the window
    between a cursor attempt and a codex fallback could retier the SAME
    logical dispatch differently across the two providers, even though the
    tier is a property of the WORK, not of which provider happens to run it
    or when in the failover sequence that happened. ``action_class=None``
    (every call site that predates this) leaves routing and model selection
    exactly as before.
    """
    # One control, two reasons to apply it. The internal name stays
    # `owns_pending_gate` because `_run_skill_once`/`_run_provider_attempts`
    # use it only for the deny and the claude-only routing.
    deny_correct = owns_pending_gate or seated_reviewer

    precomputed_tier: model_tiering.ResolvedTier | None = None
    if model is None and action_class is not None:
        precomputed_tier = model_tiering.resolve_tier(
            action_class, signals=escalation_signals
        )

    async def attempt(selected: str, fallback_model: str | None = None) -> SkillResult:
        return await _run_skill_once(
            skill,
            prompt,
            provider=selected,
            role=role,
            task_entity_id=task_entity_id,
            timeout=timeout,
            env_extra=env_extra,
            notifier=notifier,
            github_token=github_token,
            include_github_contract=include_github_contract,
            cwd=cwd,
            owns_pending_gate=deny_correct,
            command_wrapper=command_wrapper,
            codex_outer_sandboxed=codex_outer_sandboxed,
            local_review=local_review,
            agent_session_id=agent_session_id,
            work_class=work_class,
            action_class=action_class,
            escalation_signals=escalation_signals,
            model=fallback_model or model,
            precomputed_tier=precomputed_tier,
        )

    local_first = (
        provider is None
        and not deny_correct
        # A GitHub-delivery dispatch (commit/push/PR) must never route
        # local-first: local_provider.build_lean_prompt deliberately omits
        # SWARM_GITHUB_CONTRACT/SWARM_PRIOR_ART_CONTRACT (they don't fit the
        # local window either), so a local child given this work would have
        # no attribution-header/verdict-vocabulary contract and no prior-art
        # check — neither of which any `_LEAN_HARD_RULES` entry covers. Every
        # work class this could combine with today is mechanical
        # (ci_log_triage is the live example, swarm_dispatch.py's CI-fix
        # path), so this only removes local eligibility, never frontier
        # eligibility.
        and not include_github_contract
        # A guarded dispatch (harness_lens_runner's probed sandbox wrapper or
        # its inference-only local_review mode) was probed for the frontier
        # provider it names; never reroute it to a provider it was not probed
        # for.
        and not command_wrapper
        and not local_review
        and local_provider.is_eligible(work_class, local_provider.load_config())
    )
    return await _run_provider_attempts(
        skill,
        attempt,
        fallback_models_for=(
            (lambda names: {name: model for name in names}) if model else None
        ),
        binaries=_tier_bound_binaries(
            _provider_binaries(), precomputed_tier, provider,
            restricted_to_claude=deny_correct,
        ),
        provider=provider,
        role=role,
        task_entity_id=task_entity_id,
        notifier=notifier,
        preferred_provider=preferred_provider,
        owns_pending_gate=deny_correct,
        local_first=local_first,
    )


def _tier_bound_binaries(
    binaries: dict[str, str | None],
    tier: "model_tiering.ResolvedTier | None",
    pinned_provider: str | None,
    *,
    restricted_to_claude: bool = False,
) -> dict[str, str | None]:
    """Drop frontier providers with no model bound for ``tier`` before selection.

    Same shape as the #1181 fix in ``_run_provider_attempts``: the in-attempt
    ``UnboundTierError`` refusal is neither a classified ``failure_kind`` nor a
    launch failure, so on its own it would stop the dispatch at the first
    unbound provider instead of failing over to a bound one. Filtering here
    means an unbound provider is simply never a candidate.

    Left unfiltered (so the in-attempt refusal stays the backstop and names
    the missing binding) when: no tier was resolved, no vendor_binding is
    configured at all, the caller pinned a provider, the run is a gate-owning
    or seated-reviewer run (``_run_provider_attempts`` narrows those to
    claude itself; filtering claude out first would misreport the cause as a
    missing claude binary), or no router-eligible frontier provider would
    remain (configured, headroom, not cooling — ``usable_provider_names``,
    which unlike ``provider_candidates`` does not advance the round-robin).
    ``claude-local`` is never filtered — it runs its own configured model and
    takes no vendor_binding.
    """
    if tier is None or pinned_provider is not None or restricted_to_claude:
        return binaries
    binding = model_tiering.configured_vendor_binding()
    if not binding:
        return binaries
    filtered = {
        name: path
        for name, path in binaries.items()
        if name == local_provider.LOCAL_PROVIDER
        or tier.tier in binding.get(name, {})
    }
    if not (usable_provider_names(filtered) - {local_provider.LOCAL_PROVIDER}):
        return binaries
    return filtered

# The cheapest tier a frontier provider can run: the one just above "local" in
# model_tiering.TIERS. Pinned by a test so a change to the ladder cannot move the
# fallback silently.
LOCAL_FALLBACK_TIER = "mechanical"


def _local_fallback_models(providers: list[str]) -> dict[str, str]:
    """Provider -> model for the providers a failed local run may fall over to.

    A provider appears only when the vendor_binding names a model for
    ``LOCAL_FALLBACK_TIER``. No binding at all, a provider absent from it, or one
    lacking that tier means "no fallback", never the provider's ambient default
    (which ``model_tiering.model_for_tier`` would otherwise return as ``None``
    for an unconfigured deployment, and which is exactly the spend this refuses).
    """
    models: dict[str, str] = {}
    for name in providers:
        try:
            model = model_tiering.model_for_tier(name, LOCAL_FALLBACK_TIER)
        except model_tiering.UnboundTierError:
            continue
        if model:
            models[name] = model
    return models


def _refuse_local_fallback(
    skill: str,
    role: str | None,
    task_entity_id: str,
    notifier,
    reason: str,
    *,
    remaining: list[str],
    last_result: "SkillResult | None",
    attempted: list[str],
) -> SkillResult:
    """The failed result for a local-first run that may not fall over to frontier.

    Never ok, and carrying the local failure, so a refused run is reported as a
    failure rather than dropped or replayed on a provider's default model.
    """
    named = ", ".join(remaining) or "any provider"
    error = (
        f"{local_provider.LOCAL_PROVIDER} failed ({reason}) and the frontier "
        f"fallback was refused: the vendor_binding binds no "
        f"{LOCAL_FALLBACK_TIER!r}-tier model for {named}. Bind one to allow a "
        "cheapest-tier fallback: add e.g. {\"claude\": {\"mechanical\": \"haiku\"}} to "
        "~/.config/ateles/vendor-binding.json (or the file named by APIS_VENDOR_BINDING_FILE)."
    )
    log.error(f"[apis] {skill} dispatch refused — {error}")
    if last_result is None:
        result = SkillResult(skill, False, None, "", "", error=error)
    else:
        result = last_result
        result.ok = False
        result.error = error
    result.local_failure = reason
    result.attempted_providers = tuple(attempted)
    notify_dispatch_failure(
        notifier,
        skill=skill,
        role=(role or skill).lower(),
        returncode=result.returncode,
        stderr=error,
        task_entity_id=task_entity_id,
    )
    return result


def _record_local_failover(
    *,
    skill: str,
    role: str,
    task_entity_id: str,
    reason: str,
    next_provider: str | None,
    detail: str,
) -> None:
    """harness_event row naming a claude-local failure and where the run went next."""
    cfg = local_provider.load_config()
    try:
        agent_sub = _load_agent_def(role).aauth_sub
    except Exception:  # noqa: BLE001 — provenance must not block the fallback
        agent_sub = ""
    try:
        _write_harness_event(
            task_entity_id=task_entity_id,
            role=role,
            agent_sub=agent_sub,
            event_type="provider_failover",
            tool_name=f"{local_provider.LOCAL_PROVIDER}:{skill}",
            success="false",
            output_summary=(
                f"provider={local_provider.LOCAL_PROVIDER} failover_reason={reason} "
                f"next_provider={next_provider or 'none'} {detail}"
            ),
            usage=DispatchUsage(
                provider=local_provider.LOCAL_PROVIDER,
                model=cfg.model if cfg else None,
                model_source="requested" if cfg else None,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        log.debug(f"[apis] failover harness_event write failed: {exc}")


async def _run_provider_attempts(
    skill, attempt, *, binaries, provider=None, role=None, task_entity_id="",
    notifier=None, retry_safe=False, preferred_provider=None,
    owns_pending_gate: bool = False, local_first: bool = False,
    fallback_models_for=None,
) -> SkillResult:
    """One selection/cooldown/failover mechanism for every harness entrypoint.

    ``fallback_models_for`` (local-first runs only): maps the providers a failed
    or unavailable claude-local may fall over to onto the model each is pinned
    to; a provider missing from the result is not a fallback. Defaults to the
    cheapest-tier binding (``_local_fallback_models``).

    ``retry_safe`` is reserved for tool-free inference: only those calls can
    safely repeat after a timeout/outage without duplicating external effects.

    ``owns_pending_gate`` (ateles#795 / #1181 operational finding): an
    UNPINNED gate-owning reviewer run (``provider is None``, the normal
    dispatch path) must land on ``claude`` only — it is the sole provider
    that can deny `mcp__mcpsrv_neotoma__correct` at the tool-permission layer
    (see the preflight refusal in `_run_skill_once`). That refusal used to be
    the ONLY mechanism enforcing this, but it fires from *inside* a
    per-provider attempt, after `provider_candidates` has already picked
    cursor or codex first (the common case — see the live dispatch mix in the
    #1181 finding). `_run_provider_attempts` only fails over on a classified
    `failure_kind` or a `"{selected} launch failed:"` error, and the in-attempt
    refusal is neither, so it returned immediately with no failover to claude
    and most gate-owner runs would stop. Filtering candidates to `claude`
    BEFORE selection, here, fixes that: claude is simply the only candidate an
    unpinned gate-owning run ever sees, so normal failover logic (try the next
    eligible candidate) never needs to special-case this refusal.

    A caller that hard-PINS a specific non-claude ``provider`` (diagnostics,
    focused tests) is left untouched by this filter — pinning already means
    "run exactly this adapter or fail," so it still reaches the in-attempt
    refusal in `_run_skill_once` unchanged, which remains the backstop for
    every call path (pinned or not).
    """
    if owns_pending_gate and provider is None:
        if preferred_provider and preferred_provider != "claude":
            # A lens preference (e.g. the security lens's second-model
            # `codex`) cannot be honoured on a run that must deny `correct`:
            # say so rather than drop it silently.
            log.info(
                f"[apis] {skill}: preferred provider {preferred_provider!r} "
                "not used — this run denies mcp__mcpsrv_neotoma__correct, which "
                "only the claude adapter can enforce"
            )
        binaries = {"claude": binaries.get("claude")}
        preferred_provider = None

    if provider in (None, "claude"):
        # Feed the live usage snapshot before selection reads it (harness_router
        # usage gate): a run that reports nothing about the plan cannot refresh
        # it, so the reading is refreshed here when it has aged past the refresh
        # bound.  Never raises; a failed refresh leaves the reading to age out
        # and the gate refuses on it.
        await asyncio.to_thread(_refresh_usage_snapshot, binaries)
    candidates = provider_candidates(
        binaries, preferred=provider, local_first=local_first and not owns_pending_gate
    )
    if preferred_provider in candidates and provider is None:
        # A lens preference reorders the frontier providers; a local-first
        # claude-local stays ahead of them.
        head = [p for p in candidates[:1] if p == local_provider.LOCAL_PROVIDER]
        rest = [p for p in candidates if p not in head and p != preferred_provider]
        candidates = [*head, preferred_provider, *rest]
    if not candidates:
        # Cooling is checked FIRST, over exactly the providers this run could
        # have used (the claude-only narrowing for a gate-owning run, the pinned
        # provider for a pinned one): a live window is a self-clearing wait, not
        # a capability refusal, and must carry cooled_until so the panel defers
        # and resumes instead of paging the operator.
        scope = {provider: binaries.get(provider)} if provider else binaries
        windows = cooled_until_all(scope)
        if windows:
            msg, earliest = _cooled_message(windows)
            log.warning(f"[apis] {skill} not dispatched — {msg}")
            return SkillResult(
                skill, False, None, "", "", error=msg, cooled_until=earliest,
            )
        gated = usage_gate_refusals_all(scope)
        if gated:
            msg, retry_at = _usage_gate_message(gated)
            log.warning(f"[apis] {skill} not dispatched — {msg}")
            return SkillResult(
                skill, False, None, "", "", error=msg, cooled_until=retry_at,
            )
        if owns_pending_gate and provider is None:
            reason = provider_exclusion_reason("claude", binaries) or "not eligible"
            msg = (
                f"{GATE_OWNER_TOOL_DENY_UNAVAILABLE}: claude is the only "
                "provider that can deny a single MCP tool for a seated "
                "reviewer or gate-owning run, and it is currently ineligible "
                f"({reason}) — refusing "
                "to launch on cursor/codex unrestricted rather than falling "
                "over to them (ateles#795, #1181 operational finding). "
                "Nothing was cooled down."
            )
            log.error(f"[apis] {skill} dispatch refused — {msg}")
            return SkillResult(skill, False, None, "", "", error=msg)
        if provider is not None:
            reason = provider_exclusion_reason(provider, binaries)
            msg = (
                f"subscription-backed harness provider '{provider}' is "
                f"ineligible: {reason or 'not selected'}"
            )
            return SkillResult(skill, False, None, "", "", error=msg)
        configured = os.environ.get("APIS_HARNESS_PROVIDERS", "claude,codex,cursor")
        cooling = ",".join(sorted(cooling_providers())) or "none"
        msg = (
            "no subscription-backed harness provider has usable headroom "
            f"(configured={configured}; cooling={cooling})"
        )
        return SkillResult(skill, False, None, "", "", error=msg)

    attempted: list[str] = []
    last_result: SkillResult | None = None
    # Local-first work has no silent frontier path. `fallback_models` is None
    # while claude-local is still ahead in the queue; once local has failed (or
    # was never launchable) it holds the providers the operator explicitly
    # allowed as a cheapest-tier fallback, each with the model to pin.
    fallback_for = fallback_models_for or _local_fallback_models
    local_failure = ""
    fallback_models: dict[str, str] | None = None
    pending = list(candidates)
    if (
        local_first
        and provider is None
        and not owns_pending_gate
        and local_provider.LOCAL_PROVIDER not in pending
    ):
        # Requested local-first but claude-local is not launchable right now
        # (cooling down, binary gone). That is a local failure too, and takes
        # the same policy: frontier only where a fallback model is named.
        local_failure = local_provider.FAILURE_ENDPOINT + ":claude-local_unavailable"
        fallback_models = fallback_for(list(pending))
        pending = [p for p in pending if p in fallback_models]
        if not pending:
            return _refuse_local_fallback(
                skill, role, task_entity_id, notifier, local_failure,
                remaining=[p for p in candidates], last_result=None, attempted=[],
            )
    index = 0
    while index < len(pending):
        selected = pending[index]
        index += 1
        attempted.append(selected)
        pinned = (fallback_models or {}).get(selected)
        result = await (
            attempt(selected, fallback_model=pinned) if pinned else attempt(selected)
        )
        result.attempted_providers = tuple(attempted)
        # _run_skill_once carries an explicit delivery-only signal only after
        # independently excluding provider diagnostics from stderr. Preserve
        # that result before scanning the child's large stdout transcript:
        # quoted "session limit" prose in a completed verdict is not capacity.
        # Synthetic/alternate callers must satisfy the same exact invariants;
        # an error string alone is never the signal.
        delivery_only = (
            not result.ok
            and result.returncode == 0
            and result.delivery_failure_reason in _DELIVERY_DENIAL_REASONS
            and result.error == result.delivery_failure_reason
            and result.delivery_failure_reasons == (result.delivery_failure_reason,)
            and not result.delivery_failure_conflicts
        )
        if delivery_only:
            return result
        # A result claiming delivery-only while carrying mixed provider
        # diagnostics is ambiguous and must take the ordinary failure path.
        if result.delivery_failure_reason and not delivery_only:
            result.delivery_failure_reason = ""
        # A child that emitted any delivery diagnostic but did not satisfy the
        # exact delivery-only contract is not safe to replay on another
        # provider.  Returning the original result preserves its complete
        # stderr and the structured conflict set instead of cooling a healthy
        # provider because the completed review's stdout happened to quote a
        # capacity phrase.  This is the cause of the generic Codex-capacity
        # reports observed by the parent runner at 0a63667d.
        if result.delivery_failure_reasons:
            return result
        if local_failure and selected != local_provider.LOCAL_PROVIDER:
            result.local_failure = local_failure
        # ONE success-path rule: a zero-exit result is never stream-classified.
        # A successful harness may carry its prompt, transcript, or verdict on
        # stderr (a Codex review always does), and that prose can legitimately
        # quote "usage limit" or "quota exceeded". Once the adapter has
        # established success and the delivery checks above found no denial,
        # those streams are result content rather than provider diagnostics.
        # On the non-success path every stream (error, stderr, stdout) is
        # classified, as before.
        failure_kind = (
            None
            if result.ok
            else _provider_failure_kind(result.error, result.stderr, result.stdout)
        )
        launch_failure = result.error.startswith(f"{selected} launch failed:")

        if result.ok and failure_kind is None:
            return result
        if selected == local_provider.LOCAL_PROVIDER:
            # Local inference failed: record why, and fall over to frontier.
            # Local-first routing is limited to mechanical work classes, which
            # are safe to re-run from the start (rebase, regeneration, triage).
            kind = local_provider.classify_failure(
                result.error, result.stderr, result.stdout
            )
            reason = local_provider.describe_failure(
                result.error, result.stderr, result.stdout
            )
            if kind in local_provider.COOLDOWN_FAILURES:
                # Only an unusable local path is held out; a too-long prompt
                # or an ordinary task failure says nothing about its health.
                cool_down(selected)
            last_result = result
            local_failure = reason
            result.local_failure = reason
            remaining = pending[index:]
            fallback_models = fallback_for(list(remaining))
            allowed = [p for p in remaining if p in fallback_models]
            next_provider = allowed[0] if allowed else None
            log.warning(
                f"[apis] {skill}: {selected} failed ({reason}); "
                + (
                    f"falling over to {next_provider} on {fallback_models[next_provider]}"
                    if next_provider
                    else "no cheapest-tier model bound for a frontier provider — refusing"
                )
            )
            await asyncio.to_thread(
                _record_local_failover,
                skill=skill,
                role=(role or skill).lower(),
                task_entity_id=task_entity_id,
                reason=reason,
                next_provider=next_provider,
                detail=(
                    ("" if next_provider else "frontier_fallback=refused ")
                    + (result.error or result.stderr[:200])
                ),
            )
            if not allowed:
                return _refuse_local_fallback(
                    skill, role, task_entity_id, notifier, reason,
                    remaining=remaining, last_result=result, attempted=attempted,
                )
            pending = pending[:index] + allowed
            continue
        retryable_prompt_failure = retry_safe and (
            not result.ok or failure_kind is not None
        )
        if failure_kind is None and not launch_failure and not retryable_prompt_failure:
            return result

        if failure_kind == "capacity":
            _cool_after_capacity_failure(selected, result)
        else:
            cool_down(selected)
        last_result = result
        log.warning(
            f"[apis] {skill}: {selected} {failure_kind or 'launch'} failure; "
            "trying next subscription-backed provider"
        )

    assert last_result is not None  # candidates was non-empty
    last_result.ok = False
    last_result.error = (
        "all eligible subscription-backed harness providers were exhausted "
        f"after attempts: {', '.join(attempted)}"
    )
    windows = cooled_until_all({provider: binaries.get(provider)} if provider else binaries)
    if windows:
        # This dispatch's own refusals spent the last usable window: say when
        # one returns, not just that they were exhausted.
        cooled_msg, last_result.cooled_until = _cooled_message(windows)
        last_result.error = f"{last_result.error}; {cooled_msg}"
    last_result.attempted_providers = tuple(attempted)
    notify_dispatch_failure(
        notifier,
        skill=skill,
        role=(role or skill).lower(),
        returncode=last_result.returncode,
        stderr=last_result.error,
        task_entity_id=task_entity_id,
    )
    return last_result


async def run_review_prompt(
    *, role: str, prompt: str, timeout: int = 180
) -> SkillResult:
    """Route an inference-only review without granting any publisher authority.

    The caller owns the role prompt and GitHub signature. Models must be
    explicitly qualified in APIS_REVIEW_MODELS; an installed CLI alone is not
    evidence of review capability. Cursor has no verified tool-free adapter and
    is therefore ineligible on this surface until that adapter is provided.
    """
    try:
        models = json.loads(os.environ.get("APIS_REVIEW_MODELS", "{}"))
    except (TypeError, ValueError):
        models = {}
    if not isinstance(models, dict):
        models = {}
    binaries = {
        provider: binary
        for provider, binary in _provider_binaries().items()
        if provider in {"claude", "codex"}
        and isinstance(models.get(provider), str)
        and models[provider].strip()
    }
    inherited = _subscription_only_env()
    # Inference gets only its subscription authentication and runtime settings.
    # GitHub/Neotoma credentials remain exclusively with the publisher process.
    env = {
        key: value
        for key, value in inherited.items()
        if key
        in {
            "PATH",
            "HOME",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "CODEX_HOME",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "NO_PROXY",
        }
    }

    async def attempt(provider: str) -> SkillResult:
        binary = binaries[provider]
        model = models[provider].strip()
        with tempfile.TemporaryDirectory(prefix="ateles-review-") as workdir:
            if provider == "claude":
                cmd = [
                    binary,
                    "--print",
                    "--model",
                    model,
                    "--tools",
                    "",
                    "--strict-mcp-config",
                    "--mcp-config",
                    '{"mcpServers":{}}',
                    "--setting-sources",
                    "",
                ]
            else:
                cmd = [
                    binary,
                    "exec",
                    "--model",
                    model,
                    "--ignore-user-config",
                    "--sandbox",
                    "read-only",
                    "--ephemeral",
                    "--skip-git-repo-check",
                    "-c",
                    "features.shell_tool=false",
                    "-c",
                    "features.unified_exec=false",
                    "-c",
                    'web_search="disabled"',
                    "-c",
                    'forced_login_method="chatgpt"',
                    "--color",
                    "never",
                    "-",
                ]
            started = time.monotonic()
            process = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=workdir,
                    env=env,
                    start_new_session=True,
                )
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(input=prompt.encode()),
                    timeout=timeout,
                )
                out, err = (
                    stdout.decode(errors="replace"),
                    stderr.decode(errors="replace"),
                )
                result = SkillResult(
                    role,
                    process.returncode == 0 and bool(out.strip()),
                    process.returncode,
                    out,
                    err,
                    provider=provider,
                )
                if result.ok:
                    verdicts = re.findall(
                        r"(?im)^\s*Verdict\s*:\s*(APPROVE|REQUEST_CHANGES|COMMENT)\s*$",
                        out.replace("**", ""),
                    )
                    if len(verdicts) != 1:
                        result.ok = False
                        result.error = (
                            "review response must contain exactly one explicit verdict"
                        )
                elif not out.strip() and process.returncode == 0:
                    result.error = "empty review response"
            except asyncio.TimeoutError:
                if process is not None:
                    await _kill_spawned_process_group(process)
                result = SkillResult(
                    role,
                    False,
                    None,
                    "",
                    "",
                    error=f"review timed out after {timeout}s",
                    provider=provider,
                )
            except OSError as exc:
                result = SkillResult(
                    role,
                    False,
                    None,
                    "",
                    "",
                    error=f"{provider} launch failed: {exc}",
                    provider=provider,
                )
            try:
                await asyncio.to_thread(
                    _write_harness_event,
                    task_entity_id="",
                    role=role,
                    agent_sub="",
                    event_type="subprocess",
                    tool_name=f"{provider}:{role}",
                    success="true" if result.ok else "false",
                    input_summary=f"tool-restricted review; requested model={model}",
                    output_summary=f"provider={provider}; role={role}; {result.error or result.returncode}",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            except Exception as exc:
                log.debug("[apis] review attempt attribution unavailable: %s", exc)
            return result

    return await _run_provider_attempts(
        role,
        attempt,
        binaries=binaries,
        role=role,
        retry_safe=True,
    )
