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
        [--work-class rebase] \\
        [--rebase-onto origin/main [--integration rebase|merge]] \\
        [--regenerate-cmd "python3 scripts/gen.py"] \\
        [--cwd /path/to/worktree] \\
        [--timeout 600] \\
        [--task-entity-id ent_...] \\
        [--github-delivery] \\
        [--github-token-env ATELES_AGENT_PAT] \\
        [--json]

``--provider`` pins the run to one adapter, bypassing weighted selection but
NOT the eligibility rules (headroom floor, cooldowns, binary presence). The
operator wants this while automatic balancing is still being trusted. Without
it, ``harness_router`` chooses using the headroom file.

``--work-class`` names the kind of work. A mechanical class
(``local_provider.MECHANICAL_WORK_CLASSES``) that the ``claude-local``
config enables runs on the local model first; ``--provider claude-local`` pins
the local model outright. A failed local run falls over only to a frontier
provider with a model bound to the cheapest frontier tier in the vendor_binding
(pinned to it); with none bound the dispatch fails and says why. It is never
replayed on a provider's default model.

DETERMINISTIC FIRST (mechanical_first.py)
-----------------------------------------
``--work-class rebase --rebase-onto BASE`` integrates BASE with git before any
model is considered. ``--integration rebase`` (default) rebases; ``--integration
merge`` makes a merge commit, which is the right method for a branch that is
already pushed and reviewed. A clean result is verified against the repository
and reported with NO model call. Only when git stops on conflicts is a model
invoked, with a brief scoped to the conflicted hunks; its result is verified the
same way, and a failed run is undone so the worktree is left as it was found. Both
refuse anything but a dedicated linked worktree (never a shared main clone), and
verification proves the branch's own work survived, not only that the base is in
HEAD. ``--work-class regenerate_generated_files --regenerate-cmd CMD`` (repeatable)
runs the named generator directly and reports the changed files, also with no
model call. Without those flags the class runs on the model path as before.

Exit codes: 0 on a successful run, non-zero on any failure. The agent's stdout
goes to this process's stdout; diagnostics go to stderr, so the caller can pipe
the result cleanly.

THE ENVELOPE IS UNCONDITIONAL (ateles#585)
------------------------------------------
Under ``--json`` this entrypoint writes exactly one JSON envelope on stdout on
EVERY exit path — success, refusal, provider failure, unhandled exception,
usage error, and death by signal. An empty output file is not a reachable
outcome.

That is a correctness requirement, not a nicety. Before #585 the envelope was
written only after ``asyncio.run`` returned, so anything that killed the
process mid-dispatch (a SIGTERM/SIGHUP from a harness that backgrounds or
times out its shell call being the observed cause) left a 0-byte file, three
healthy-looking banner lines on stderr, and no other trace. The caller could
not distinguish "still working" from "died ten seconds ago", and read the
silence as success. A dispatcher that fails invisibly is worse than one that
crashes loudly, so failure is now always self-reporting:

* ``ok: false`` with a ``reason`` naming the failure class,
* a non-zero exit code, and
* for a signal, exit ``128 + signum`` (SIGTERM -> 143), preserving the shell
  convention the caller already knows how to read.

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
import signal
import sys
from pathlib import Path


# ── Env bootstrap ─────────────────────────────────────────────────────────────
# An orchestrating session's shell does not necessarily carry the daemon env,
# and skill_runner hard-requires NEOTOMA_BASE_URL (no localhost default by
# design — see the 2026-08-04 hosted migration). Mirrors apis.py's bootstrap so
# an ad-hoc run behaves identically to a launchd-started daemon. setdefault
# throughout: an explicitly exported value always wins over the file.
#
# Skipped under pytest (ateles#1285), same rationale as apis.py: this module
# is imported directly by several test_*.py here, and the operator's
# materialized dotenv carries operator-behaviour switches (e.g.
# ATELES_SWARM_REQUIRE_LABEL) that must not silently reach a test process.
def _dotenv_should_load() -> bool:
    if (os.environ.get("ATELES_SKIP_DOTENV") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return False
    if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST") is not None:
        return False
    return True


_NEOTOMA_ENV_FILE = Path.home() / ".config" / "neotoma" / ".env"
if _dotenv_should_load() and _NEOTOMA_ENV_FILE.exists():
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
    live_headroom,
)
from local_provider import (  # noqa: E402
    LOCAL_PROVIDER,
    MECHANICAL_WORK_CLASSES,
    config_path as local_config_path,
    load_config as load_local_config,
)
import model_tiering  # noqa: E402
import mechanical_first  # noqa: E402
from skill_runner import (  # noqa: E402
    ATELES_REPO,
    SkillResult,
    _load_agent_def,
    run_skill,
)

# A pseudo-provider for runs that made no model call: it spends nothing, so a
# per-provider budget or cooldown consumer must treat it as "no spend", not as an
# unknown provider.
DETERMINISTIC_PROVIDER = "deterministic"


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
    return sorted(p.name for p in skills_dir.iterdir() if (p / "SKILL.md").is_file())


async def dispatch(
    role: str,
    task: str,
    *,
    provider: str | None = None,
    cwd: str | None = None,
    timeout: int | None = None,
    task_entity_id: str = "",
    env_extra: dict[str, str] | None = None,
    seated_reviewer: bool = False,
    command_wrapper: list[str] | None = None,
    codex_outer_sandboxed: bool = False,
    local_review: bool = False,
    work_class: str | None = None,
    github_delivery: bool = False,
    github_token: str | None = None,
    action_class: str | None = None,
    model: str | None = None,
    escalation_signals: "model_tiering.EscalationSignals | None" = None,
    integration_base: str | None = None,
    integration_mode: str = mechanical_first.MODE_REBASE,
    regenerate_commands: list[str] | None = None,
) -> SkillResult:
    """Dispatch one piece of work to a named role via the harness router.

    ``role`` names both the agent_definition to load AND the SKILL.md to run —
    they are the same string throughout this codebase, which is why run_skill's
    ``skill`` and ``role`` parameters both receive it.

    Everything that makes this a GOVERNED dispatch rather than an anonymous
    subagent happens inside run_skill: identity, allowlist, provider routing,
    credential stripping, and the harness_event rows. This function's only job
    is to hand it a well-formed request.

    ``env_extra`` (harness-lens-runner, ent_898998f41372ce24369fb365): merged
    on top of the child's environment by ``_subscription_only_env`` inside
    ``run_skill`` — the mechanism a caller uses to override ``HOME`` /
    ``CODEX_HOME`` for an isolated, credential-blind sandbox on a non-Claude
    provider. This process's own environment (and hence its Neotoma/gh
    access) is untouched; only the dispatched CHILD sees the override.

    ``seated_reviewer`` forwards to ``run_skill`` unchanged. It does NOT mean
    "this is a lens run" in general — it means the run must be treated exactly
    like a panel-seated reviewer with shared-bearer Neotoma MCP access, which
    restricts routing to adapters with a proven subtractive deny for
    ``correct`` (see ``run_skill``'s docstring). A caller dispatching an
    inference-only review that never receives Neotoma MCP tools should leave
    this False; it does not need the seated-review control.

    ``command_wrapper`` (ent_89a4d44b063cb0902106da49): forwarded verbatim to
    ``run_skill`` -> ``_run_skill_once``, which prepends it to the provider's
    OWN argv before the subprocess actually runs. This is how a caller makes
    a guard (e.g. a macOS ``sandbox-exec`` profile denying reads of specific
    credential paths) bind onto the real dispatched process rather than
    merely describe an intended mitigation next to code that runs unwrapped.

    ``codex_outer_sandboxed`` accompanies harness_lens_runner's probed
    ``sandbox-exec`` wrapper. It tells the Codex adapter not to attempt an
    unsupported nested Seatbelt sandbox; ``run_skill`` fails closed if the
    flag is supplied without that outer wrapper.

    ``local_review`` selects the inference-only environment: no ambient
    GitHub/Neotoma publication authority or credential fallback reaches the
    child. The caller, not the child, owns any later publication.
    ``github_delivery`` states that this task must commit, push, or open a pull
    request. It reuses ``run_skill``'s existing GitHub-contract path, which both
    injects the delivery contract and enables Codex network for this dispatch.
    Such a run must also supply ``github_token`` explicitly; the shared runner
    refuses omitted or empty bindings instead of inheriting the daemon's ambient
    GitHub identity. The default stays False so read-only and filesystem-only
    work remains under the sandbox's network denial.

    ``action_class``/``model`` (operator ruling 2026-09-29, model_tiering.py):
    forwarded unchanged to ``run_skill``. ``action_class`` resolves a tier from
    the live action_policy/vendor_binding config; ``model`` overrides that
    resolution outright. Neither is required — an orchestrating session's
    one-off dispatch that names no ``action_class`` runs exactly as before.

    ``integration_base`` / ``integration_mode`` (work class ``rebase``) and
    ``regenerate_commands`` (work class ``regenerate_generated_files``) opt the
    dispatch into the deterministic-first path: see ``mechanical_first``. Without
    them every class runs on the model path exactly as before.
    """
    run_kwargs = dict(
        role=role,
        task_entity_id=task_entity_id,
        timeout=timeout,
        cwd=cwd,
        provider=provider,
        env_extra=env_extra,
        seated_reviewer=seated_reviewer,
        command_wrapper=command_wrapper,
        codex_outer_sandboxed=codex_outer_sandboxed,
        local_review=local_review,
        work_class=work_class,
        github_token=github_token,
        include_github_contract=github_delivery,
        action_class=action_class,
        escalation_signals=escalation_signals,
        model=model,
    )
    if work_class == "rebase" and integration_base:
        return await _integrate_then_model(
            role,
            task,
            base=integration_base,
            mode=integration_mode,
            workdir=cwd or os.getcwd(),
            run_kwargs=run_kwargs,
        )
    if work_class == "regenerate_generated_files" and regenerate_commands:
        outcome = await asyncio.to_thread(
            mechanical_first.run_generators, cwd or os.getcwd(), regenerate_commands
        )
        return _deterministic_result(role, task_entity_id, work_class, outcome)
    # work_class reaches run_skill's local-first routing AND (via
    # _run_skill_once) the lean-prompt/post-condition path — see
    # local_provider.build_lean_prompt / verify_postcondition.
    return await run_skill(role, task, **run_kwargs)


def _record_deterministic(
    role: str, task_entity_id: str, work_class: str, ok: bool, summary: str
) -> None:
    """Best-effort harness_event for a run that finished without a model."""
    try:
        from skill_runner import DispatchUsage, _write_harness_event

        _write_harness_event(
            task_entity_id=task_entity_id,
            role=role,
            agent_sub="",
            event_type="subprocess",
            tool_name=f"{DETERMINISTIC_PROVIDER}:{work_class}",
            success="true" if ok else "false",
            output_summary=f"no model call: {summary}"[:500],
            usage=DispatchUsage(provider=DETERMINISTIC_PROVIDER),
        )
    except Exception as exc:  # noqa: BLE001 — provenance must not fail the run
        print(f"dispatch_role: harness_event not written: {exc}", file=sys.stderr)


def _deterministic_result(
    role: str, task_entity_id: str, work_class: str, outcome: "mechanical_first.Outcome"
) -> SkillResult:
    """The SkillResult for a mechanical run that ended without a model call."""
    ok = outcome.status == mechanical_first.DONE
    summary = outcome.summary
    if ok and outcome.changed_files:
        summary += ": " + ", ".join(outcome.changed_files[:50])
    _record_deterministic(role, task_entity_id, work_class, ok, summary)
    return SkillResult(
        role,
        ok,
        0 if ok else 1,
        f"deterministic {work_class}: {summary}\n" if ok else "",
        "",
        error="" if ok else f"deterministic {work_class} {outcome.status}: {summary}",
        provider=DETERMINISTIC_PROVIDER,
        attempted_providers=(DETERMINISTIC_PROVIDER,),
    )


async def _integrate_then_model(
    role: str, task: str, *, base: str, mode: str, workdir: str, run_kwargs: dict
) -> SkillResult:
    """Integrate with git first; call a model only for conflicts, and verify it."""
    outcome = await asyncio.to_thread(
        mechanical_first.attempt_integration, workdir, base, mode
    )
    if outcome.status != mechanical_first.CONFLICTS:
        return _deterministic_result(
            role, run_kwargs["task_entity_id"], "rebase", outcome
        )
    print(
        f"dispatch_role: {outcome.summary}; invoking a model on the conflicted hunks only",
        file=sys.stderr,
    )
    prompt = (
        outcome.brief
        + "\n\n---\nThe caller's original task, for context only (the brief above is "
        + "authoritative):\n"
        + task[:2000]
    )
    try:
        result = await run_skill(role, prompt, **run_kwargs)
    except BaseException:
        # A crash or cancellation mid-run must not leave the integration open.
        restored = await asyncio.to_thread(
            mechanical_first.restore_original,
            workdir,
            outcome.orig_head,
            outcome.orig_ref,
        )
        if restored.problem:
            print(f"dispatch_role: RESTORE FAILED: {restored.problem}", file=sys.stderr)
        raise
    if result.ok:
        problem = await asyncio.to_thread(
            mechanical_first.verify_integration,
            workdir,
            outcome.base_sha,
            mode,
            outcome.orig_head,
        )
        if problem:
            result.ok = False
            result.error = f"the model reported success but the integration did not verify: {problem}"
    if not result.ok:
        restored = await asyncio.to_thread(
            mechanical_first.restore_original,
            workdir,
            outcome.orig_head,
            outcome.orig_ref,
        )
        detail = result.error or "dispatch failed"
        if restored.problem:
            print(f"dispatch_role: RESTORE FAILED: {restored.problem}", file=sys.stderr)
            result.error = f"{detail} (RESTORE FAILED: {restored.problem})"
        elif restored.undone:
            result.error = (
                f"{detail} (undid {restored.undone}; the worktree is as it was found)"
            )
    result.attempted_providers = (DETERMINISTIC_PROVIDER, *result.attempted_providers)
    return result


def _signals_from_args(args: argparse.Namespace) -> "model_tiering.EscalationSignals | None":
    """Build escalation signals from the CLI's measured-fact flags, or None
    when none was given (so an untouched invocation resolves exactly as the
    policy alone says)."""
    if not (
        args.review_round > 1
        or args.prior_blocking_finding
        or args.prior_attempt_failed
        or args.new_blocking_finding
        or args.diff_lines
        or args.changed_file
    ):
        return None
    return model_tiering.EscalationSignals(
        changed_files=tuple(args.changed_file or ()),
        diff_lines_changed=args.diff_lines or 0,
        prior_blocking_finding=args.prior_blocking_finding,
        review_round=args.review_round,
        prior_attempt_failed=args.prior_attempt_failed,
        new_blocking_finding=args.new_blocking_finding,
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
    if provider == LOCAL_PROVIDER:
        if load_local_config() is None:
            return (
                f"provider {LOCAL_PROVIDER!r} is not configured: no valid, "
                f"enabled config at {local_config_path()}"
            )
    elif provider is not None and provider not in configured_providers():
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
    live = [p for p in configured_providers() if live_headroom(p) is not None]
    if live:
        source += f"; live usage for {', '.join(live)}"
    values = configured_headroom()
    rendered = ", ".join(f"{p}={values[p]:g}" for p in configured_providers())
    cooling = ", ".join(sorted(cooling_providers())) or "none"
    return f"headroom [{source}]: {rendered}; cooling: {cooling}"


class _Emitter:
    """Guarantees exactly one JSON envelope on stdout, whatever happens.

    Every exit path funnels through ``emit``. The ``_done`` latch makes it
    idempotent, which matters because the paths race: a SIGTERM can arrive
    while the normal result is being written, and two envelopes in one stream
    is a parse error for the caller — as unusable as zero.

    ``enabled`` is False without ``--json``: the human-readable mode keeps its
    plain-text output, and the failure detail still reaches stderr.
    """

    def __init__(self, *, enabled: bool, role: str) -> None:
        self.enabled = enabled
        self.role = role
        self._done = False

    def emit(self, payload: dict) -> None:
        if self._done:
            return
        self._done = True
        if not self.enabled:
            return
        body = {"role": self.role, **payload}
        try:
            sys.stdout.write(json.dumps(body, indent=2) + "\n")
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            # stdout is gone (closed pipe / full disk). Nothing further can be
            # done for the caller, but this must not mask the original failure
            # or turn a reported error into a traceback.
            pass

    def emit_failure(self, reason: str, **extra) -> None:
        """The failure envelope. ``reason`` is required and never empty."""
        self.emit(
            {
                "ok": False,
                "reason": reason,
                "provider": extra.pop("provider", None),
                "attempted_providers": extra.pop("attempted_providers", []),
                "returncode": extra.pop("returncode", None),
                "error": extra.pop("error", reason),
                "stdout": extra.pop("stdout", ""),
                "stderr": extra.pop("stderr", ""),
                **extra,
            }
        )


def _usage_failure(emitter: _Emitter, reason: str) -> int:
    """A usage error, reported through the emitter rather than argparse.

    ``parser.error`` raises ``SystemExit(2)`` straight past the emitter, which
    left ``--json`` callers with the 0-byte stdout of ateles#585 — the exact
    signature this module exists to eliminate. Loud on stderr (argparse's own
    behaviour is preserved) AND structured on stdout, so neither a human nor a
    parser is left guessing. Exit 2 is kept: it is the conventional usage
    status and callers may already branch on it.
    """
    print(f"dispatch_role: error: {reason}", file=sys.stderr)
    emitter.emit_failure(reason, error="usage error")
    return 2


def _install_signal_envelope(emitter: _Emitter) -> None:
    """Turn a fatal signal into a reported failure instead of silence.

    SIGTERM and SIGHUP are the observed killers (#585): a harness that
    backgrounds or times out its shell call delivers one of them, and Python's
    default disposition is to die immediately, writing nothing. SIGINT is
    included for symmetry with an operator's Ctrl-C.

    The handler emits, then re-raises the signal through the default handler so
    the process still dies with the conventional ``128 + signum`` status rather
    than a laundered exit 0 — a caller checking only the exit code must not be
    told a killed dispatch succeeded.
    """

    def _handler(signum, _frame):  # pragma: no cover - exercised as a subprocess
        name = signal.Signals(signum).name
        emitter.emit_failure(
            f"dispatch terminated by {name} before the run completed",
            error=f"killed by {name}",
        )
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for _sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        try:
            signal.signal(_sig, _handler)
        except (ValueError, OSError, AttributeError):
            # Not the main thread, or the platform lacks the signal. Losing one
            # handler must not prevent the dispatch from running at all.
            pass


def _peek_argv(argv: list[str]) -> tuple[bool, str]:
    """Detect ``--json`` and ``--role`` before argparse runs.

    ``parse_args`` can fail (unknown flags, bad types) by calling ``error``,
    which raises ``SystemExit(2)`` before the caller knows ``args.json``. A
    caller redirecting stdout to a file still gets the #585 0-byte signature
    unless the emitter exists first.
    """
    json_mode = "--json" in argv
    role = ""
    for i, arg in enumerate(argv):
        if arg == "--role" and i + 1 < len(argv):
            role = argv[i + 1].strip().lower()
            break
    return json_mode, role


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    json_mode, peek_role = _peek_argv(argv)
    emitter = _Emitter(enabled=json_mode, role=peek_role)

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
        choices=["claude", "codex", "cursor", LOCAL_PROVIDER],
        help=(
            "Force one provider, bypassing weighted selection (eligibility "
            "rules still apply). Omit to let the router choose on headroom."
        ),
    )
    parser.add_argument(
        "--work-class",
        choices=sorted(MECHANICAL_WORK_CLASSES),
        help=(
            "Mechanical work class. When the claude-local config enables it, "
            "the local model runs first and the frontier providers are the "
            "fallback."
        ),
    )
    parser.add_argument(
        "--action-class",
        help=(
            "Action class for model tiering (operator ruling 2026-09-29, "
            "model_tiering.py), e.g. 'build', 'lens_review:security', "
            "'carry_forward_check'. Resolves a minimum tier from the live "
            "action_policy config, escalated by measured signals, then a "
            "model from the live vendor_binding config for the chosen "
            "provider. Omit to leave model selection exactly as before "
            "(the provider's ambient default)."
        ),
    )
    parser.add_argument(
        "--review-round", type=int, default=1,
        help=(
            "Review round number for tier escalation. Round 2 or later raises "
            "to top, except a small pm/qa/ux re-review, which stays mid."
        ),
    )
    parser.add_argument(
        "--prior-blocking-finding", action="store_true",
        help=(
            "An earlier round raised a blocking finding; raises the tier to "
            "top (not for a pm/qa/ux re-review, which needs a NEW finding)."
        ),
    )
    parser.add_argument(
        "--new-blocking-finding", action="store_true",
        help=(
            "The last round raised a blocking finding no earlier round had; "
            "raises the tier to top, including for pm/qa/ux re-reviews."
        ),
    )
    parser.add_argument(
        "--prior-attempt-failed", action="store_true",
        help="A previous attempt at this work failed; raises the tier to top.",
    )
    parser.add_argument(
        "--diff-lines", type=int, default=0,
        help="Added + deleted lines in the change; over 400 raises the tier to top.",
    )
    parser.add_argument(
        "--changed-file", action="append", metavar="PATH",
        help=(
            "A file the change touches (repeatable); a security-sensitive path "
            "raises the tier to top."
        ),
    )
    parser.add_argument(
        "--model",
        help=(
            "Explicit model id, overriding any --action-class tier "
            "resolution outright. Passed as the provider's own --model flag "
            "(claude/codex/cursor); has no effect on claude-local."
        ),
    )
    parser.add_argument(
        "--rebase-onto",
        metavar="BASE",
        help=(
            "With --work-class rebase: integrate this base ref into the current "
            "branch with git BEFORE any model. A clean result is verified and "
            "reported with no model call; only conflicts invoke a model, on the "
            "conflicted hunks alone. The base must already resolve locally."
        ),
    )
    parser.add_argument(
        "--integration",
        choices=list(mechanical_first.MODES),
        default=None,
        help=(
            "How --rebase-onto integrates: 'rebase' (default) or 'merge' for a "
            "merge commit — use merge for a branch that is already pushed and "
            "reviewed, since a rebase rewrites its published history."
        ),
    )
    parser.add_argument(
        "--regenerate-cmd",
        action="append",
        metavar="CMD",
        help=(
            "With --work-class regenerate_generated_files: a generator command to "
            "run directly (no shell), repeatable, in order. Reports the changed "
            "files with no model call; a failing generator fails the dispatch."
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
        "--github-delivery",
        action="store_true",
        help=(
            "Task must commit, push, or open a pull request. Injects the shared "
            "GitHub delivery contract and enables scoped Codex network access "
            "for this dispatch only."
        ),
    )
    parser.add_argument(
        "--github-token-env",
        help=(
            "Environment variable containing the scoped token for this GitHub "
            "delivery invocation. The value is never accepted on argv or "
            "included in output. Required with --github-delivery until a "
            "named non-token identity mechanism is established for this path."
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

    def _parser_error(message: str) -> None:
        print(f"dispatch_role: error: {message}", file=sys.stderr)
        emitter.emit_failure(message, error="usage error")
        raise SystemExit(2)

    parser.error = _parser_error  # type: ignore[method-assign]
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if args.list_roles:
        for name in available_roles():
            print(name)
        return 0
    # Armed before ANY work that can fail — including the usage checks below —
    # and before the signal handlers, so there is no window in which this
    # process can die reporting nothing. A usage error is still a dispatch that
    # produced no result, and a caller parsing --json must not be handed the
    # 0-byte file of ateles#585 just because argparse rejected the arguments.
    emitter.enabled = args.json
    emitter.role = (args.role or peek_role or "").strip().lower()
    _install_signal_envelope(emitter)

    if not args.role:
        return _usage_failure(emitter, "--role is required (or use --list-roles)")

    role = args.role.strip().lower()
    emitter.role = role

    # Resolve the task text from exactly one source. A missing or unreadable
    # --task-file used to raise straight out of main() as a traceback with no
    # envelope; it is a dispatch failure like any other.
    if args.task_file:
        try:
            task = Path(args.task_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            reason = f"could not read --task-file {args.task_file!r}: {exc}"
            print(f"dispatch_role: {reason}", file=sys.stderr)
            emitter.emit_failure(reason)
            return 1
    elif args.task == "-":
        # Reading a closed or blocked stdin raises; it is a dispatch failure
        # like any other, not a traceback with no envelope.
        try:
            task = sys.stdin.read()
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            reason = f"could not read task from stdin: {exc}"
            print(f"dispatch_role: {reason}", file=sys.stderr)
            emitter.emit_failure(reason)
            return 1
    elif args.task:
        task = args.task
    else:
        return _usage_failure(emitter, "one of --task, --task-file is required")
    if not task.strip():
        reason = "task description is empty"
        print(f"dispatch_role: {reason}", file=sys.stderr)
        emitter.emit_failure(reason)
        return 1

    if args.rebase_onto and args.work_class != "rebase":
        return _usage_failure(emitter, "--rebase-onto requires --work-class rebase")
    if args.regenerate_cmd and args.work_class != "regenerate_generated_files":
        return _usage_failure(
            emitter, "--regenerate-cmd requires --work-class regenerate_generated_files"
        )
    if args.integration and not args.rebase_onto:
        return _usage_failure(emitter, "--integration requires --rebase-onto")
    if args.rebase_onto and not args.cwd:
        return _usage_failure(
            emitter, "--rebase-onto requires --cwd (the worktree to integrate in)"
        )
    if args.regenerate_cmd and not args.cwd:
        return _usage_failure(
            emitter, "--regenerate-cmd requires --cwd (the worktree to regenerate in)"
        )

    github_token: str | None = None
    if args.github_token_env:
        if not args.github_delivery:
            return _usage_failure(
                emitter, "--github-token-env requires --github-delivery"
            )
        if not args.github_token_env.isidentifier():
            return _usage_failure(
                emitter, "--github-token-env must name a valid environment variable"
            )
        github_token = os.environ.get(args.github_token_env, "")
    elif args.github_delivery:
        # Caught here as a fast, structured usage error — same shape as every
        # other CLI misuse below — rather than left to surface deep inside
        # skill_runner's credential-boundary refusal (ateles#590 security
        # repair) as an unstructured "dispatch raised" error. Both paths
        # ultimately refuse the same run; this one is knowable from the
        # parsed arguments alone and should say so immediately.
        return _usage_failure(
            emitter,
            "--github-delivery requires --github-token-env (a network-enabled "
            "GitHub delivery run must bind an explicit scoped credential; "
            "omitting it would otherwise be refused deeper in the dispatch)",
        )

    refusal = _preflight(role, provider=args.provider)
    if refusal:
        print(f"dispatch_role: {refusal}", file=sys.stderr)
        emitter.emit_failure(refusal)
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

    # run_skill is documented to return a SkillResult on every failure it
    # anticipates, but "anticipates" is the operative word: an unhandled
    # exception anywhere beneath it (a provider adapter, the Neotoma client, an
    # asyncio teardown) previously escaped as a traceback with an empty
    # envelope. Catch BaseException so a SystemExit or KeyboardInterrupt raised
    # deep in the stack is reported too, then re-raise nothing — the reason is
    # already in the envelope and the exit code carries the failure.
    try:
        result = asyncio.run(
            dispatch(
                role,
                task,
                provider=args.provider,
                cwd=args.cwd,
                timeout=args.timeout,
                task_entity_id=args.task_entity_id,
                work_class=args.work_class,
                github_delivery=args.github_delivery,
                github_token=github_token,
                action_class=args.action_class,
                escalation_signals=_signals_from_args(args),
                model=args.model,
                integration_base=args.rebase_onto,
                integration_mode=args.integration or mechanical_first.MODE_REBASE,
                regenerate_commands=args.regenerate_cmd,
            )
        )
    except BaseException as exc:  # noqa: BLE001 — see above
        reason = f"dispatch raised {type(exc).__name__}: {exc}"
        print(f"dispatch_role: {reason}", file=sys.stderr)
        import traceback

        traceback.print_exc(file=sys.stderr)
        emitter.emit_failure(reason)
        return 1

    emitter.emit(
        {
            "ok": result.ok,
            **({} if result.ok else {"reason": result.error or "dispatch failed"}),
            "provider": result.provider,
            "attempted_providers": list(result.attempted_providers),
            "returncode": result.returncode,
            "error": result.error,
            "local_failure": result.local_failure,
            "stdout": result.stdout,
            "stderr": result.stderr,
            **({"cooled_until": result.cooled_until} if result.cooled_until else {}),
        }
    )
    if not args.json and result.stdout:
        print(result.stdout)

    print(
        f"dispatch_role: ok={result.ok} provider={result.provider or '(none)'} "
        f"attempted={','.join(result.attempted_providers) or '(none)'} "
        f"rc={result.returncode}",
        file=sys.stderr,
    )
    if result.local_failure:
        print(
            f"dispatch_role: claude-local failed ({result.local_failure})"
            + ("; the run then ran on " + result.provider if result.ok else ""),
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
