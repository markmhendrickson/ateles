"""
execution/daemons/apis/skill_runner.py — spawn a T4 agent via `claude --print`.

Single implementation of the spawn pattern previously inlined in apis.py (and
mirrored in Formica): `claude --print --append-system-prompt <SKILL.md>` with
the work prompt piped on stdin. Extracted so the GitHub trigger pipelines
(swarm_dispatch.py) can reuse it and capture agent output for the review
learning loop.

Stage 1 (ateles#94): loads the dispatched role's agent_definition from Neotoma
so the spawned subprocess gets the role's canonical system prompt prepended to
SKILL.md, and (when the definition specifies a restricted tool_allowlist) passes
--allowed-tools to confine the subprocess.

Stage 2 (ateles#94): writes a harness_event to Neotoma at dispatch start,
completion, and failure.

Stage 5 (ateles#94): when no agent_definition loads (empty prompt_markdown),
emits a notifier WARN and a harness_event with the degraded_generic_subagent
marker so degraded dispatches are observable. Dispatch still proceeds.

Failures never raise — callers get a SkillResult and decide how to degrade;
one bad dispatch must not take down the daemon.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
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

from lib.daemon_runtime import AgentDefinition, AgentLoader  # noqa: E402

log = logging.getLogger("apis.skill_runner")

CLAUDE_BIN = os.environ.get("APIS_CLAUDE_BIN") or shutil.which("claude")
DISPATCH_TIMEOUT_SECONDS = int(os.environ.get("APIS_DISPATCH_TIMEOUT", "1800"))
ATELES_REPO = Path(
    os.environ.get("ATELES_REPO_PATH", str(Path.home() / "repos" / "ateles"))
)

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


# ── Shared GitHub-interaction convention (Phase 1 / Layer A) ──────────────────
# Injected into every GitHub-dispatched agent's system prompt by build_system_prompt
# when include_github_contract=True.  Lives in ONE place — not duplicated across
# agent_definitions.  Complements (never contradicts) per-prompt format instructions
# already present in swarm_dispatch.py prompts.
#
# See docs/swarm_github_interaction_design.md — Layer A.

SWARM_GITHUB_CONTRACT = """\
## Swarm GitHub interaction contract (Layer A)

Every GitHub comment you post as part of the Ateles swarm MUST follow this convention.

### Comment skeleton

```
🤖 <Agent> — <role> · <repo>#<n>      ← attribution header (omit when posting as your own account per #109)
**<VERDICT>**                          ← one-line machine-readable status

<role-specific body>                   ← your expertise (structured, not essay)

---
📎 Neotoma: <label> · <label>          ← footer: Neotoma backlinks (when applicable)
```

### Verdict vocabulary

Use exactly ONE of these tokens as the bold status line — one per comment, always present:

- `**APPROVE**` — all checks pass, no blockers.
- `**REQUEST_CHANGES**` — one or more [BLOCKING] findings; the author must address them.
- `**COMMENT**` — observations only; nothing blocks merge.
- `**BLOCKED**` — cannot proceed (missing information, open pre-impl gate, etc.).
- `**SIGNED_OFF**` — your gate/phase is signed off.

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

# ── System-prompt assembly ─────────────────────────────────────────────────────


def build_system_prompt(
    agent_def: AgentDefinition,
    skill_md: str,
    include_github_contract: bool = False,
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
    """
    definition_prompt = (agent_def.prompt_markdown or "").strip()
    if definition_prompt:
        if include_github_contract:
            return (
                f"{definition_prompt}\n\n"
                "---\n\n"
                f"{SWARM_GITHUB_CONTRACT}\n\n"
                "---\n\n"
                f"{skill_md}",
                False,
            )
        return (
            f"{definition_prompt}\n\n"
            "---\n\n"
            f"{skill_md}",
            False,
        )
    # Degraded: no definition_prompt.
    if include_github_contract:
        return f"{SWARM_GITHUB_CONTRACT}\n\n---\n\n{skill_md}", True
    return skill_md, True


# ── Neotoma harness_event writer ───────────────────────────────────────────────


def _write_harness_event(
    *,
    task_entity_id: str,
    role: str,
    agent_sub: str,
    event_type: str,
    tool_name: str,
    success: str,
    input_summary: str = "",
    output_summary: str = "",
    duration_ms: int | None = None,
) -> None:
    """
    Best-effort write of a harness_event entity to Neotoma.

    Uses the same /store endpoint and pattern as lib/activity/_store_activity_log.
    Never raises — a harness_event failure must not crash dispatch.
    """
    base_url = os.environ.get("NEOTOMA_BASE_URL", "http://localhost:3180").rstrip("/")
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    if not token:
        return

    event_at = datetime.now(timezone.utc).isoformat()
    # canonical_name_fields per schema: session_id, event_type, event_at
    # session_id is not available at dispatch time; use role+task+event_at as a
    # stable-enough dedup key without it.
    idempotency_key = f"harness-event-{role}-{task_entity_id}-{event_type}-{event_at}"

    entity: dict = {
        "entity_type": "harness_event",
        "event_type": event_type,
        "event_at": event_at,
        "tool_name": tool_name,
        "agent_sub": agent_sub,
        "success": success,
        "task_entity_id": task_entity_id,
    }
    if input_summary:
        entity["input_summary"] = input_summary[:500]
    if output_summary:
        entity["output_summary"] = output_summary[:500]
    if duration_ms is not None:
        entity["duration_ms"] = duration_ms

    payload = {
        "idempotency_key": idempotency_key,
        "observation_source": "workflow_state",
        "entities": [entity],
    }
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
    try:
        with urllib.request.urlopen(req, timeout=5.0):
            pass
    except Exception as exc:
        log.debug(f"[apis] harness_event write failed (non-fatal): {exc}")


# ── SkillResult ────────────────────────────────────────────────────────────────


@dataclass
class SkillResult:
    skill: str
    ok: bool
    returncode: int | None
    stdout: str
    stderr: str
    error: str = ""  # non-process failure: missing binary / SKILL.md / timeout


# ── Main runner ────────────────────────────────────────────────────────────────


async def run_skill(
    skill: str,
    prompt: str,
    *,
    role: str | None = None,
    task_entity_id: str = "",
    timeout: int | None = None,
    env_extra: dict[str, str] | None = None,
    notifier=None,  # lib.notify.Notifier | None — kept optional to avoid hard dep
    github_token: str | None = None,
    include_github_contract: bool = False,
) -> SkillResult:
    """
    Run one T4 agent to completion and return its output.

    Stage 1: loads the role's agent_definition (role defaults to skill when not
    passed — skill name == role name in this codebase). Prepends the definition's
    prompt_markdown to SKILL.md; applies the tool allowlist when restricted.

    Stage 2: writes harness_event entities to Neotoma at start, completion, and
    failure.

    Stage 5: when agent_definition carries empty prompt_markdown, logs a WARN,
    sends a notifier alert (when a notifier is supplied), and records a
    degraded_generic_subagent harness_event. Dispatch still proceeds.

    ``github_token`` (#109 — per-agent GitHub identity): when supplied, the token
    is injected into subprocess_env as both ``GITHUB_TOKEN`` and ``GH_TOKEN`` so
    the spawned agent's ``gh`` calls authenticate as the correct identity.  When
    not supplied, the child inherits the daemon's ambient env unchanged (current
    behaviour for all callers that predate #109).  Only GitHub-triggered pipeline
    call sites pass this; SSE task-path dispatches leave it unset.

    ``include_github_contract`` (Phase 1 / Layer A): when True, SWARM_GITHUB_CONTRACT
    is injected into the system prompt between the agent_definition and the SKILL.md.
    Pass True ONLY from GitHub-trigger call sites in swarm_dispatch.py; leave as
    False (the default) for all SSE/non-GitHub task dispatches so the contract never
    appears in payment, health, finance, or other non-GitHub work.

    `claude --print` tool-allowlist flag: --allowed-tools (confirmed present;
    accepts comma- or space-separated tool names, e.g. "Bash,Edit,Read").
    """
    _role = (role or skill).lower()
    timeout = timeout or DISPATCH_TIMEOUT_SECONDS

    # ── Load agent_definition (Stage 1) ───────────────────────────────────────
    agent_def = await asyncio.to_thread(_load_agent_def, _role)

    if CLAUDE_BIN is None:
        msg = "claude binary unavailable (APIS_CLAUDE_BIN unset, not on PATH)"
        log.warning(f"[apis] {skill} dispatch skipped — {msg}")
        return SkillResult(skill, False, None, "", "", error=msg)

    skill_path = ATELES_REPO / ".claude" / "skills" / skill / "SKILL.md"
    if not skill_path.exists():
        msg = f"SKILL.md not found at {skill_path}"
        log.error(f"[apis] {skill} dispatch skipped — {msg}")
        return SkillResult(skill, False, None, "", "", error=msg)

    try:
        skill_md = skill_path.read_text(encoding="utf-8")
    except OSError as exc:
        return SkillResult(skill, False, None, "", "", error=f"read failed: {exc}")

    # ── Build system prompt (Stage 1 + Stage 5) ────────────────────────────────
    system_prompt, degraded = build_system_prompt(
        agent_def, skill_md, include_github_contract=include_github_contract
    )

    if degraded:
        _title_hint = prompt[:80].replace("\n", " ")
        warn_msg = (
            f"Role {_role!r} ran DEGRADED (no agent_definition loaded) "
            f"for task {task_entity_id or '(unknown)'!r}. "
            "Dispatching with SKILL.md only."
        )
        log.warning(f"[apis] {warn_msg}")
        if notifier is not None:
            try:
                from lib.notify import Priority
                notifier.send(warn_msg, priority=Priority.WARN, handler="apis")
            except Exception as exc:
                log.debug(f"[apis] notifier.send failed: {exc}")

        # Stage 5: degraded harness_event
        try:
            await asyncio.to_thread(
                _write_harness_event,
                task_entity_id=task_entity_id,
                role=_role,
                agent_sub=agent_def.aauth_sub,
                event_type="subprocess",
                tool_name=skill,
                success="partial",
                input_summary=_title_hint,
                output_summary="degraded_generic_subagent",
            )
        except Exception as exc:
            log.debug(f"[apis] degraded harness_event write failed: {exc}")

    # ── Build command (Stage 1: tool allowlist) ────────────────────────────────
    cmd = [CLAUDE_BIN, "--print", "--append-system-prompt", system_prompt]

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
    _neotoma_base = os.environ.get("NEOTOMA_BASE_URL", "http://localhost:9180").rstrip("/")
    _neotoma_token = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    _mcp_cfg: dict = {
        "mcpServers": {
            "mcpsrv_neotoma": {
                "type": "http",
                "url": f"{_neotoma_base}/mcp",
            }
        }
    }
    if _neotoma_token:
        _mcp_cfg["mcpServers"]["mcpsrv_neotoma"]["headers"] = {
            "Authorization": f"Bearer {_neotoma_token}"
        }

    # Write the MCP config to a mode-0600 temp file to avoid argv exposure.
    try:
        fd, _mcp_tmp_path = tempfile.mkstemp(suffix=".json", prefix="apis_mcp_")
        os.chmod(_mcp_tmp_path, 0o600)
        with os.fdopen(fd, "w") as _f:
            json.dump(_mcp_cfg, _f)
        cmd += ["--mcp-config", _mcp_tmp_path]
        log.debug(f"[apis] Injected --mcp-config {_mcp_tmp_path} (mcpsrv_neotoma HTTP MCP)")
    except Exception as exc:
        # Non-fatal: proceed without the MCP config injection rather than abort.
        log.warning(f"[apis] Could not write MCP config temp file (non-fatal): {exc}")
        _mcp_tmp_path = None

    tools = agent_def.tools  # property: list[str]; ['*'] means all
    if tools != ["*"]:
        # --allowed-tools is confirmed present in `claude --print --help`
        # (alias: --allowedTools). Accepts comma- or space-separated tool names.
        # MCP server tools use the "mcp__<servername>__*" wildcard form, where the
        # server name matches the mcpServers key (here: "mcpsrv_neotoma" — the
        # universal convention across all 31 agent SKILLs and 24 agent_definitions).
        # This allows all tools from that MCP server without enumerating them individually.
        allowed_list = list(tools)
        if "mcp__mcpsrv_neotoma__*" not in allowed_list:
            allowed_list.append("mcp__mcpsrv_neotoma__*")
        allowed = ",".join(allowed_list)
        cmd += ["--allowed-tools", allowed]
        log.info(
            f"[apis] Spawning: claude --print --append-system-prompt "
            f"<{_role}:agent_def+{skill}.SKILL.md> "
            f"--allowed-tools {allowed} timeout={timeout}s"
        )
    else:
        log.info(
            f"[apis] Spawning: claude --print --append-system-prompt "
            f"<{_role}:{'agent_def+' if not degraded else 'degraded-'}{skill}.SKILL.md> "
            f"timeout={timeout}s"
        )

    # ── Stage 2: harness_event at dispatch start ───────────────────────────────
    try:
        await asyncio.to_thread(
            _write_harness_event,
            task_entity_id=task_entity_id,
            role=_role,
            agent_sub=agent_def.aauth_sub,
            event_type="subprocess",
            tool_name=skill,
            success="partial",  # "partial" = in-flight / started
            input_summary=prompt[:200],
        )
    except Exception as exc:
        log.debug(f"[apis] start harness_event write failed (non-fatal): {exc}")

    subprocess_env = {**os.environ, **(env_extra or {})}

    # ateles#109 — per-agent GitHub identity: when the caller resolved a
    # per-agent token (e.g. via _token_for_agent_on_repo in swarm_dispatch),
    # override both GITHUB_TOKEN and GH_TOKEN so the child's `gh` calls
    # authenticate as that agent's own account.  When github_token is None
    # (all SSE task-path and non-GitHub call sites), this block is skipped and
    # the child inherits the daemon's ambient tokens unchanged — exact
    # current behaviour, no regression.
    if github_token:
        subprocess_env["GITHUB_TOKEN"] = github_token
        subprocess_env["GH_TOKEN"] = github_token

    # Stage 3 (ateles#94): inject the Neotoma AAuth client signer env vars so
    # the dispatched child can sign its own Neotoma writes as <role>@ateles-swarm.
    # The Neotoma client signer (aauth_client_signer.ts) reads three vars:
    #   NEOTOMA_AAUTH_PRIVATE_JWK_PATH — path to the EC/P-256 JWK keypair file
    #   NEOTOMA_AAUTH_SUB              — the signing subject (e.g. gryllus@ateles-swarm)
    #   NEOTOMA_AAUTH_ISS              — the issuer (https://markmhendrickson.com)
    # We only inject when the role JWK file actually exists at the expected path;
    # if it is absent the child proceeds unsigned (graceful degradation, as today).
    # When degraded (empty prompt_markdown) we inject nothing — child runs unsigned.
    if not degraded and agent_def.aauth_sub:
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
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=subprocess_env,
    )

    try:
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode()), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            duration_ms = int((time.monotonic_ns() - _start_ns) / 1_000_000)
            msg = f"timed out after {timeout}s"
            log.error(f"[apis] {skill} dispatch {msg}")

            # Stage 2: harness_event on timeout (failure)
            try:
                await asyncio.to_thread(
                    _write_harness_event,
                    task_entity_id=task_entity_id,
                    role=_role,
                    agent_sub=agent_def.aauth_sub,
                    event_type="subprocess",
                    tool_name=skill,
                    success="false",
                    output_summary=f"timeout after {timeout}s",
                    duration_ms=duration_ms,
                )
            except Exception as exc:
                log.debug(f"[apis] timeout harness_event write failed: {exc}")

            return SkillResult(skill, False, None, "", "", error=msg)

        duration_ms = int((time.monotonic_ns() - _start_ns) / 1_000_000)
        result = SkillResult(
            skill=skill,
            ok=proc.returncode == 0,
            returncode=proc.returncode,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )

        # ── Stage 2: harness_event at completion ──────────────────────────────────
        if result.ok:
            log.info(f"[apis] {skill} dispatch ok ({len(result.stdout)}B stdout)")
            try:
                await asyncio.to_thread(
                    _write_harness_event,
                    task_entity_id=task_entity_id,
                    role=_role,
                    agent_sub=agent_def.aauth_sub,
                    event_type="subprocess",
                    tool_name=skill,
                    success="true",
                    output_summary=f"{len(result.stdout)}B stdout rc=0",
                    duration_ms=duration_ms,
                )
            except Exception as exc:
                log.debug(f"[apis] success harness_event write failed: {exc}")
        else:
            log.error(
                f"[apis] {skill} dispatch failed (rc={proc.returncode}): "
                f"{result.stderr[:500]}"
            )
            try:
                await asyncio.to_thread(
                    _write_harness_event,
                    task_entity_id=task_entity_id,
                    role=_role,
                    agent_sub=agent_def.aauth_sub,
                    event_type="subprocess",
                    tool_name=skill,
                    success="false",
                    output_summary=f"rc={proc.returncode} {result.stderr[:200]}",
                    duration_ms=duration_ms,
                )
            except Exception as exc:
                log.debug(f"[apis] failure harness_event write failed: {exc}")

        return result

    finally:
        # Clean up the MCP config temp file (always, even on timeout/exception).
        if _mcp_tmp_path is not None:
            try:
                os.unlink(_mcp_tmp_path)
            except OSError:
                pass
