#!/usr/bin/env python3
"""
execution/daemons/apis/dispatch_role.py — dispatch one-off work to a NAMED
swarm role through the quota-aware harness router, from an orchestrating
session.

WHY THIS EXISTS
---------------
``skill_runner.run_skill()`` already does everything a governed dispatch needs:
it loads the role's ``agent_definition`` from Neotoma (prompt_markdown,
tool_allowlist, aauth_sub), picks a provider via ``harness_router`` honouring
the headroom file, passes ``--allowed-tools``, strips metered API credentials,
injects the role's AAuth signing identity, and writes ``harness_event`` rows at
start / completion / failure.

But until now that machinery was reachable ONLY from a dispatched task — a
GitHub webhook (``swarm_dispatch.py``) or a Neotoma ``task`` entity with
``assigned_to`` (``apis.py``). An orchestrating session with a one-off piece of
work had no door in, so it fell back to anonymous harness subagents: no
agent_definition, no aauth_sub, no tool_allowlist, no harness_event — and,
critically, no route to codex or cursor. Every such subagent burned Claude
quota regardless of the headroom file's verdict.

This module is that door, and nothing more. It is a THIN entrypoint: it adds no
policy, no routing logic, and no provider handling of its own. Every decision is
still made by ``run_skill`` / ``harness_router``. Deliberately chosen over an
importable helper (which would push env + sys.path + asyncio bootstrap onto
every caller as an unauditable ``python -c`` incantation) and over an MCP tool
(which needs the Ateles MCP write-tool suite tracked separately as
ent_31187e772fdaaec5d82228c0 — this entrypoint is a natural backend for such a
tool later, but does not require it now).

USAGE
-----
    python3 execution/daemons/apis/dispatch_role.py \\
        --role cicada \\
        --task "Report the current git branch and HEAD sha." \\
        [--provider codex] \\
        [--cwd /path/to/worktree] \\
        [--timeout 600] \\
        [--task-entity-id ent_...] \\
        [--json]

``--provider`` pins the run to one adapter, bypassing weighted selection but
NOT the eligibility rules (headroom floor, cooldowns, binary presence). The
operator wants this while automatic balancing is still being trusted. Without
it, ``harness_router`` chooses using the headroom file.

Exit codes: 0 on a successful run, 1 on any failure. The agent's stdout goes to
this process's stdout; diagnostics go to stderr, so the caller can pipe the
result cleanly.

NOT IN SCOPE (deliberately)
---------------------------
* Model selection — no provider gets a ``--model`` flag today; each takes its
  ambient default. Changing that is filed separately.
* Any change to ``skill_runner`` or ``harness_router`` behaviour.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# ── Env bootstrap ─────────────────────────────────────────────────────────────
# An orchestrating session's shell does not necessarily carry the daemon env,
# and skill_runner hard-requires NEOTOMA_BASE_URL (no localhost default by
# design — see the 2026-08-04 hosted migration). Mirrors apis.py's bootstrap so
# an ad-hoc run behaves identically to a launchd-started daemon. setdefault
# throughout: an explicitly exported value always wins over the file.
_NEOTOMA_ENV_FILE = Path.home() / ".config" / "neotoma" / ".env"
if _NEOTOMA_ENV_FILE.exists():
    for _line in _NEOTOMA_ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _v = _v.strip()
            # Strip an inline ` # comment` from UNQUOTED values only (quoted
            # values may legitimately contain '#' inside a token).
            if _v[:1] not in ('"', "'") and " #" in _v:
                _v = _v.split(" #", 1)[0].strip()
            os.environ.setdefault(_k.strip(), _v.strip('"').strip("'"))


def _looks_local(base_url: str) -> bool:
    """True unless the host is positively identifiable as remote.

    Fails SAFE: anything loopback, private, *.local, unparseable, or empty is
    treated as local, so the failure mode is "don't promote the prod token"
    rather than "send a prod token at the wrong instance". Same classifier as
    apis.py.
    """
    from urllib.parse import urlparse

    if not base_url:
        return True
    host = (urlparse(base_url).hostname or "").lower()
    if not host:
        return True
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        return True
    if host.endswith(".local") or host.endswith(".localhost"):
        return True
    if host.startswith(("10.", "192.168.", "169.254.")):
        return True
    if host.startswith("172."):
        try:
            if 16 <= int(host.split(".")[1]) <= 31:
                return True
        except (IndexError, ValueError):
            pass
    return False


# The shared env file carries a LOCAL-scoped NEOTOMA_BEARER_TOKEN; prod entity
# reads (agent_definition load) and harness_event writes need the prod-scoped
# one when the base URL is remote.
if not _looks_local(os.environ.get("NEOTOMA_BASE_URL", "")):
    _prod_token = os.environ.get("NEOTOMA_BEARER_TOKEN_PROD", "").strip()
    if _prod_token:
        os.environ["NEOTOMA_BEARER_TOKEN"] = _prod_token

# ── Path bootstrap ────────────────────────────────────────────────────────────
# skill_runner imports its siblings (harness_router) as top-level modules, so
# this daemon's own directory must be on sys.path, as must the repo root for
# `lib.daemon_runtime`.
_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from harness_router import (  # noqa: E402
    configured_headroom,
    configured_providers,
    cooling_providers,
)
from skill_runner import (  # noqa: E402
    ATELES_REPO,
    SkillResult,
    _load_agent_def,
    run_skill,
)


def available_roles() -> list[str]:
    """Role names dispatchable here: every skill with a SKILL.md.

    A role is dispatchable when ``<ateles>/.claude/skills/<role>/SKILL.md``
    exists, because that is exactly what ``_run_skill_once`` reads. Resolved
    against ATELES_REPO (overridable with ATELES_REPO_PATH), NOT against this
    file's location — a worktree checkout can dispatch using the main clone's
    skills, or its own, depending on how the operator points that variable.
    """
    skills_dir = ATELES_REPO / ".claude" / "skills"
    if not skills_dir.is_dir():
        return []
    return sorted(
        p.name for p in skills_dir.iterdir() if (p / "SKILL.md").is_file()
    )


async def dispatch(
    role: str,
    task: str,
    *,
    provider: str | None = None,
    cwd: str | None = None,
    timeout: int | None = None,
    task_entity_id: str = "",
) -> SkillResult:
    """Dispatch one piece of work to a named role via the harness router.

    ``role`` names both the agent_definition to load AND the SKILL.md to run —
    they are the same string throughout this codebase, which is why run_skill's
    ``skill`` and ``role`` parameters both receive it.

    Everything that makes this a GOVERNED dispatch rather than an anonymous
    subagent happens inside run_skill: identity, allowlist, provider routing,
    credential stripping, and the harness_event rows. This function's only job
    is to hand it a well-formed request.
    """
    return await run_skill(
        role,
        task,
        role=role,
        task_entity_id=task_entity_id,
        timeout=timeout,
        cwd=cwd,
        provider=provider,
    )


def _preflight(role: str, *, provider: str | None) -> str | None:
    """Return a human-readable reason to refuse, or None to proceed.

    Catches the misconfigurations that would otherwise surface as an opaque
    run_skill error string, and reports them BEFORE any harness_event is
    written — a refused dispatch should leave no audit trail suggesting work
    was attempted.
    """
    roles = available_roles()
    if not roles:
        return (
            f"no SKILL.md files found under {ATELES_REPO / '.claude' / 'skills'} — "
            "set ATELES_REPO_PATH to the checkout whose skills you mean to use"
        )
    if role not in roles:
        return (
            f"unknown role {role!r}. Roles with a SKILL.md in {ATELES_REPO}: "
            + ", ".join(roles)
        )
    if provider is not None and provider not in configured_providers():
        return (
            f"provider {provider!r} is not in the configured order "
            f"({', '.join(configured_providers())}); "
            "set APIS_HARNESS_PROVIDERS to include it"
        )
    return None


def _headroom_note() -> str:
    """One line naming the headroom actually in force and where it came from.

    configured_headroom() takes the FIRST of (file, env) that parses, so
    APIS_HARNESS_HEADROOM does NOT override the file while the file exists.
    That precedence has surprised operators; surfacing the effective source on
    every run makes a stale file self-evident instead of silently authoritative.
    """
    configured_path = os.environ.get("APIS_HARNESS_HEADROOM_FILE", "").strip()
    path = (
        Path(configured_path).expanduser()
        if configured_path
        else Path.home() / ".config" / "ateles" / "harness-headroom.json"
    )
    if path.is_file():
        source = f"file {path}"
    elif os.environ.get("APIS_HARNESS_HEADROOM", "").strip():
        source = "env APIS_HARNESS_HEADROOM"
    else:
        source = "defaults (all 1.0)"
    values = configured_headroom()
    rendered = ", ".join(f"{p}={values[p]:g}" for p in configured_providers())
    cooling = ", ".join(sorted(cooling_providers())) or "none"
    return f"headroom [{source}]: {rendered}; cooling: {cooling}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dispatch_role",
        description=(
            "Dispatch one-off work to a NAMED swarm role through the "
            "quota-aware harness router, with the role's agent_definition, "
            "tool allowlist, AAuth identity, and harness_event audit trail."
        ),
    )
    parser.add_argument(
        "--role",
        help="Swarm role name (e.g. cicada, pavo, lanius). Must have a SKILL.md.",
    )
    parser.add_argument(
        "--task",
        help="The work to dispatch. Use --task-file, or '-' to read stdin.",
    )
    parser.add_argument(
        "--task-file",
        help="Read the task description from this file instead of --task.",
    )
    parser.add_argument(
        "--provider",
        choices=["claude", "codex", "cursor"],
        help=(
            "Force one provider, bypassing weighted selection (eligibility "
            "rules still apply). Omit to let the router choose on headroom."
        ),
    )
    parser.add_argument(
        "--cwd",
        help="Working directory for the dispatched child (e.g. a worktree).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        help="Seconds before the child is killed (default: APIS_DISPATCH_TIMEOUT).",
    )
    parser.add_argument(
        "--task-entity-id",
        default="",
        help=(
            "Neotoma task entity id to record on the harness_event rows. "
            "Optional — a one-off dispatch need not have one."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the full result as JSON on stdout instead of raw agent output.",
    )
    parser.add_argument(
        "--list-roles",
        action="store_true",
        help="Print the dispatchable role names and exit.",
    )
    args = parser.parse_args(argv)

    if args.list_roles:
        for name in available_roles():
            print(name)
        return 0
    if not args.role:
        parser.error("--role is required (or use --list-roles)")

    # Resolve the task text from exactly one source.
    if args.task_file:
        task = Path(args.task_file).read_text(encoding="utf-8")
    elif args.task == "-":
        task = sys.stdin.read()
    elif args.task:
        task = args.task
    else:
        parser.error("one of --task, --task-file is required")
    if not task.strip():
        parser.error("task description is empty")

    role = args.role.strip().lower()

    refusal = _preflight(role, provider=args.provider)
    if refusal:
        print(f"dispatch_role: {refusal}", file=sys.stderr)
        return 1

    # Report the identity actually loaded, so a degraded dispatch (Neotoma
    # unreachable -> stub definition, wildcard tools) is visible at the point of
    # dispatch rather than inferred afterwards from a harness_event.
    try:
        agent_def = _load_agent_def(role)
        tools = agent_def.tools
        identity = (
            f"role={role} sub={agent_def.aauth_sub or '(none)'} "
            f"tier={agent_def.tier or '(unset)'} "
            f"tools={'ALL' if tools == ['*'] else f'{len(tools)} allowlisted'} "
            f"prompt={'loaded' if agent_def.prompt_markdown.strip() else 'EMPTY (degraded)'}"
        )
    except Exception as exc:  # noqa: BLE001 — reporting must not block dispatch
        identity = f"role={role} (agent_definition preload failed: {exc})"

    print(f"dispatch_role: {identity}", file=sys.stderr)
    print(f"dispatch_role: {_headroom_note()}", file=sys.stderr)
    print(
        "dispatch_role: provider "
        + (f"FORCED to {args.provider}" if args.provider else "chosen by router"),
        file=sys.stderr,
    )

    result = asyncio.run(
        dispatch(
            role,
            task,
            provider=args.provider,
            cwd=args.cwd,
            timeout=args.timeout,
            task_entity_id=args.task_entity_id,
        )
    )

    if args.json:
        print(
            json.dumps(
                {
                    "role": role,
                    "ok": result.ok,
                    "provider": result.provider,
                    "attempted_providers": list(result.attempted_providers),
                    "returncode": result.returncode,
                    "error": result.error,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                },
                indent=2,
            )
        )
    else:
        if result.stdout:
            print(result.stdout)

    print(
        f"dispatch_role: ok={result.ok} provider={result.provider or '(none)'} "
        f"attempted={','.join(result.attempted_providers) or '(none)'} "
        f"rc={result.returncode}",
        file=sys.stderr,
    )
    if not result.ok:
        if result.error:
            print(f"dispatch_role: error: {result.error}", file=sys.stderr)
        if result.stderr:
            print(f"dispatch_role: child stderr:\n{result.stderr}", file=sys.stderr)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
