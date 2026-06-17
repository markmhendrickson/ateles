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


# ── System-prompt assembly ─────────────────────────────────────────────────────


def build_system_prompt(agent_def: AgentDefinition, skill_md: str) -> tuple[str, bool]:
    """
    Build the composite system prompt for a role dispatch.

    Returns (prompt, degraded) where degraded=True means the agent_definition
    did not contribute (empty prompt_markdown) and the subprocess will run with
    SKILL.md alone.

    The agent_definition's canonical instructions come FIRST so they establish
    identity, permissions, and behavioral constraints before the per-task skill
    instructions. Separated by a clear boundary so the model can parse both layers.
    """
    definition_prompt = (agent_def.prompt_markdown or "").strip()
    if definition_prompt:
        return (
            f"{definition_prompt}\n\n"
            "---\n\n"
            f"{skill_md}",
            False,
        )
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
    system_prompt, degraded = build_system_prompt(agent_def, skill_md)

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

    tools = agent_def.tools  # property: list[str]; ['*'] means all
    if tools != ["*"]:
        # --allowed-tools is confirmed present in `claude --print --help`
        # (alias: --allowedTools). Accepts comma- or space-separated tool names.
        allowed = ",".join(tools)
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
    _start_ns = time.monotonic_ns()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=subprocess_env,
    )

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
