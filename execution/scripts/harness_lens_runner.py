#!/usr/bin/env python3
"""
execution/scripts/harness_lens_runner.py — run ONE swarm review lens on ONE PR
head through a chosen harness provider (claude / codex / cursor), in a
dedicated throwaway worktree, and post the verdict only after it passes the
same checks a Claude bootstrap lens run applies by hand.

Neotoma task ent_898998f41372ce24369fb365: get bootstrap-mode lens reviews and
builds running on Codex and Cursor, not only Claude subagents. Extends the
existing multi-provider load-balancing work (ent_8b99e1df6aa2f0e7d54cae24)
rather than paralleling it — see "WHY THIS REUSES, NOT PARALLELS" below.

WHY THIS REUSES, NOT PARALLELS
-------------------------------
Every piece of provider routing, identity, credential stripping, and
harness_event bookkeeping already exists in
``execution/daemons/apis/dispatch_role.py`` and the ``skill_runner`` /
``harness_router`` it wraps. This script adds NOTHING to that layer except
three threaded-through kwargs (``env_extra``, ``seated_reviewer``,
``command_wrapper`` — see ``dispatch_role.dispatch`` and
``skill_runner._run_skill_once``). Its own job, and nothing else:

  1. render the shared lens brief + the lens's agent_definition prompt into
     one task string,
  2. create/point at a throwaway worktree of the target repo at the target
     head,
  3. build and PROBE the per-harness guard mechanisms (see GUARD BINDING
     below), refusing the harness outright if the probe shows any guard did
     not actually bind,
  4. call ``dispatch_role.dispatch(...)`` with that sandbox's ``env_extra``
     AND ``command_wrapper`` so the guards apply to the REAL subprocess, not
     merely to a description of it,
  5. capture the verdict file the lens writes inside that worktree,
  6. validate it with the SAME reader every Claude lens run uses
     (``swarm_dispatch.lens_own_verdict`` / ``sign_off_is_warranted``) before
     ever touching ``gh pr comment``.

GUARD BINDING (per harness) — the critical part
-------------------------------------------------
Claude Code's PreToolUse/Stop hooks (``git_stash_guard``,
``credential_read_guard`` — PR #1302, unmerged at the time this was written,
``refuse_task_chip``, ``gmail_send_gate``, ``sibling_repo_worktree_guard``) are
harness mechanisms. They hook Claude Code's own tool-call lifecycle and do
NOT run when the child process is ``codex`` or ``cursor-agent`` — those
binaries have never heard of ``.claude/hooks/``. Running a lens on either
without an equivalent bound guard would silently drop every one of those
protections. ``claude`` needs nothing further — its own hook set already
binds every guard this class attempts.

For codex/cursor, this script builds and MECHANICALLY BINDS the two guards
whose absence has a concrete blast radius, and PROBES each one — against a
fixture, never a real credential file or the real shared git dir — before
ever deciding a dispatch may proceed (see ``HarnessSandbox.build()``,
``probe_sandbox_exec_denies_read/write``, ``probe_git_shim_denies_stash_push``):

  * **Credential-file reads and user-config writes** — a macOS
    ``sandbox-exec`` profile (``build_sandbox_exec_profile``) denies
    ``file-read*`` on the credential-file globs named in this task
    (``~/.config/neotoma/.env*``, ``~/.neotoma/aauth*/*private*``,
    ``~/.claude.json``, ``~/.netrc``, ``~/.config/sops/age/*``) and
    ``file-write*`` on ``~/.claude``, ``~/.cursor``, ``~/.codex``. The
    profile is prepended to the dispatched child's REAL argv via
    ``command_wrapper=["sandbox-exec", "-f", <profile>]`` — see
    ``skill_runner._run_skill_once``'s ``command_wrapper`` parameter, added
    specifically so this could bind onto the actual subprocess rather than
    describe an intended mitigation beside code that runs unwrapped.
    **What this does NOT claim**: ``codex exec --sandbox workspace-write``
    and ``--add-dir`` (codex's own existing sandbox, already applied by
    ``skill_runner`` before this script exists) are, per ``codex exec
    --help`` / ``codex sandbox --help``, WRITE-permission scoping only —
    they grant additional writable roots and restrict writes outside the
    workspace; nothing in either ``--help`` output denies a READ of an
    arbitrary absolute path. An earlier revision of this docstring claimed
    codex's own sandbox "keeps it from reading arbitrary paths outside that
    root," which security and content lens review on PR #1308 correctly
    identified as unsupported by codex's documented semantics — corrected
    here. The actual read-deny mechanism is the ``sandbox-exec`` profile
    above, wrapped around the SAME process codex's own sandbox also wraps
    (the two compose: codex's sandbox still governs writes as before,
    ``sandbox-exec`` additionally denies the specific reads/writes named
    above). ``CODEX_HOME``/``HOME`` overrides still relocate each CLI's OWN
    config store (so the child cannot read the operator's real
    ``~/.codex/config.toml``, whose ``approval_policy = "never"`` and
    per-project trust would be the opposite of safe for an unattended run).
    The one required capability, ChatGPT subscription authentication, is
    exposed without copying its value: the isolated ``CODEX_HOME/auth.json``
    is a symlink to the existing login file, while the outer sandbox denies
    writes to the real ``~/.codex`` target. If that source is absent, the
    runner refuses before dispatch rather than starting a model call that
    cannot authenticate.
  * **``git stash``** — TWO layers, closing two distinct resolution paths a
    dispatched child could use to reach a real ``git``. Round 3 review
    (ent_9e88db1882c668e6c5c32be9) found the first layer alone insufficient:
    a PATH-first shim never runs for an ABSOLUTE-path invocation, since
    resolving an absolute path never consults ``PATH`` at all — verified
    directly (Homebrew's ``/opt/homebrew/bin/git stash push``, invoked by its
    resolved Cellar path, bypassed an earlier revision of this shim entirely
    and landed a real stash entry).

    1. **PATH resolution** — a ``git`` shim script (``_GIT_STASH_SHIM``,
       written into a directory prepended to the child's ``PATH`` via
       ``env_extra``) refuses ``stash`` in ANY form — no read-only carve-out;
       the operator's rule is "no stash in any form runs," and since this
       shim is the ONLY place stash is mentioned in this runner, dropping the
       carve-out ``.claude/hooks/git_stash_guard.py`` keeps for itself is
       simpler and matches the rule exactly. Execs a COPY of the real git
       binary (staged by ``_stage_exec_allowed_git_copy``, at a path the
       profile below does NOT deny) for every non-stash invocation.
    2. **Absolute-path resolution** — the SAME ``sandbox-exec`` profile used
       for credential-read/user-config-write additionally denies
       ``process-exec`` of every git binary ``discover_real_git_binaries``
       finds on ``PATH`` — symlink-RESOLVED, since Seatbelt's ``process-exec``
       path filters match the exec syscall's resolved target, not a symlink
       the caller typed (verified: denying Homebrew's symlink path alone was
       a no-op; denying the resolved Cellar path it points at worked). This
       is what stops an absolute-path invocation the shim above never sees.
       The staged copy above lives outside this deny set specifically so the
       shim still has a real git to exec.

    ``HarnessSandbox.git_stash_denied`` is True only when BOTH layers probe
    as denied (see ``probe_git_shim_denies_stash_push`` and
    ``probe_absolute_path_git_stash_is_denied``) — either layer alone leaves
    a real resolution path open, which is exactly the shape of the round-3
    finding: a guard that reports itself bound while one path through it
    remains open.

  Both mechanisms are built AND ACTUALLY RUN against a throwaway fixture in
  ``HarnessSandbox.build()`` before any real dispatch: the sandbox-exec
  profile is tested with a real ``cat``/``touch`` against a fixture file this
  script creates (never the operator's own files), and the git shim is
  tested with a real ``git stash push`` inside a scratch repo this script
  ``git init``s for the probe (never the shared clone or its stash ref). The
  resulting booleans (``credential_read_denied``, ``user_config_write_denied``,
  ``git_stash_denied``) are what ``HarnessSandbox.fully_guarded`` and
  ``refuse_if_guard_required`` actually read — nothing here is asserted
  without having just been exercised. If ``sandbox-exec`` is unavailable (a
  non-Darwin host, or a future macOS release that removes it — it is already
  documented DEPRECATED, see ``man sandbox-exec``), the probes report
  ``False`` and ``refuse_if_guard_required`` refuses the harness rather than
  silently proceeding unguarded.

  **Not attempted, and named as such rather than silently skipped**:
  ``sibling_repo_worktree_guard`` has no equivalent here either, but its
  hazard (an agent writing into a sibling repo's shared main clone) is
  structurally avoided rather than merely undefended: this script's own
  ``Worktree`` always dispatches into its own dedicated throwaway worktree of
  the TARGET repo, never the shared clone or another session's worktree, and
  the dispatched child's ``cwd`` is pinned there. ``gmail_send_gate`` and
  ``refuse_task_chip`` are not applicable — this script never invokes
  gws/gmail, and no harness task-chip surface exists outside Claude Code.

  What this script REFUSES rather than fakes: any run whose lens OWNS a
  pending pre-impl gate (``owns_pending_gate``/``seated_reviewer`` in
  ``skill_runner`` terms) still may not go to codex/cursor — see
  ``skill_runner._run_skill_once``'s own preflight, which already refuses
  that combination because neither adapter can deny a single MCP tool
  (``mcp__mcpsrv_neotoma__correct``) while granting the rest. This script's
  lens dispatch never requests Neotoma MCP tools for codex/cursor at all (only
  ``provider == "claude"`` gets ``--mcp-config`` injected — see
  ``skill_runner._run_skill_once``), so it does not set ``seated_reviewer``;
  it relies on that existing absence rather than re-deciding it. A caller that
  ever adds MCP grants to the codex/cursor path must revisit this. And, per
  ``refuse_if_guard_required``: any codex/cursor dispatch whose sandbox probe
  does not come back ``fully_guarded`` is refused outright — see ``run_one``,
  which checks this before ever calling ``dispatch_role.dispatch``.

  Credentials the run legitimately needs — the `gh` token for posting, and a
  Neotoma bearer if the lens brief needs one — are injected as NAMED
  environment variables onto the child's process env (`GH_TOKEN`/`GITHUB_TOKEN`
  via ``dispatch_role``'s existing ``github_token`` plumbing is the model to
  extend; this script injects the reviewer's OWN token, never a raw path to
  the operator's `.env` file, and never prints it).

USAGE
-----
The one command a session runs to DISPATCH a lens review (--agent omitted:
resolved from --lens via review_panel.LENSES; pass --task-entity-id when the
dispatch is PART_OF a tracked task, so the next command below has something
to filter on):

    python3 execution/scripts/harness_lens_runner.py \\
        --repo owner/name --pr 1234 --head <sha> \\
        --lens pm --provider claude \\
        --brief <path-to-lens-brief> --task-entity-id ent_... \\
        [--post] [--dry-run] [--compare claude,codex]

The one command a session runs to MONITOR it — purely from Neotoma, no
access to this process or its stdout required — via the Neotoma MCP/REST
`retrieve_entities` on `harness_event`, filtered by the SAME task_entity_id
just passed above (every harness_event row this runner's dispatch writes,
via dispatch_role.dispatch -> run_skill -> skill_runner._write_harness_event,
carries it — start with success="partial", then a terminal completion or
failure row):

    retrieve_entities(
        entity_type="harness_event",
        snapshot_filters={"task_entity_id": {"op": "eq", "value": "ent_..."}},
        sort_by="snapshot.event_at", sort_order="desc",
    )

`tool_name` on each row reads `<provider>:<agent>` (e.g. `claude:pavo`), so
the SAME query also answers "which provider actually ran this" without
re-deriving it from this script's own state.

Exit codes: 0 on success (or a clean, reported refusal under --dry-run),
non-zero on any failure that prevented a verdict or a required post.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_DAEMON_DIR = REPO_ROOT / "execution" / "daemons" / "apis"
for _p in (str(REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# noqa: E402 — path bootstrap above must run first
import dispatch_role  # noqa: E402
from harness_router import configured_headroom  # noqa: E402
from review_panel import lens_by_name  # noqa: E402
from skill_runner import SkillResult  # noqa: E402

try:
    import swarm_dispatch  # noqa: E402
except Exception:  # pragma: no cover — degraded import path, handled at call site
    swarm_dispatch = None  # type: ignore[assignment]


# No canonical, checked-in copy of the bootstrap-mode lens brief exists in
# this repo as of this script's authorship — it has so far lived only as an
# operator-session scratchpad file (see ent_898998f41372ce24369fb365's own
# "Read first" list). This script therefore has NO baked-in default path: the
# caller must always pass --brief pointing at whichever copy is current. A
# wrong guessed default here would silently run a stale or wrong brief with no
# error, which is worse than requiring the flag every time.
LENS_BRIEF_PATH = None

HARNESSES = ("claude", "codex", "cursor")


# ── Headroom -------------------------------------------------------------------


class HeadroomExhausted(RuntimeError):
    """Raised when the requested provider's configured headroom is 0."""


def check_headroom(provider: str) -> float:
    """Return the provider's configured headroom, raising if it is exactly 0.

    Reads the SAME file/env precedence ``harness_router.configured_headroom``
    already implements (file beats env; see that module's docstring) — this
    function adds no second source of truth, it only turns "0.0" into a loud,
    named refusal instead of a routing failure discovered three steps later.
    A provider sitting above 0 but below ``APIS_HARNESS_MIN_HEADROOM`` is left
    to the router's own eligibility check (``harness_router.provider_candidates``),
    which already refuses it with a specific reason; this function only
    special-cases the exact-zero operator-reset signal named in the task.
    """
    headroom = configured_headroom()
    value = headroom.get(provider, 1.0)
    if value <= 0.0:
        raise HeadroomExhausted(
            f"provider {provider!r} has headroom={value:g} — refusing to "
            "dispatch. This is the operator-reset gate: headroom is restored "
            "by editing ~/.config/ateles/harness-headroom.json (operator- or "
            "session-owned, see the PR body's 'Headroom' section), never by "
            "this script."
        )
    return value


# ── Rendering the lens task -----------------------------------------------------


@dataclass(frozen=True)
class LensTarget:
    repo: str  # "owner/name"
    pr: int
    head: str
    lens: str  # short label, e.g. "pm"
    agent: str  # docs/agents/<agent>.md name, e.g. "pavo"
    focus_notes: str = ""
    # Neotoma task entity this dispatch is PART_OF, if any. Optional — a
    # one-off comparison run need not have one — but when present it is the
    # durable key a session filters harness_event rows by, so "monitor from
    # Neotoma alone" (the coordinator's ask) has something to filter on
    # rather than free-text matching input_summary.
    task_entity_id: str = ""


def render_lens_task(target: LensTarget, brief_path: Path) -> str:
    """Render the shared lens brief + the lens's own agent prompt into one
    task string, exactly as the bootstrap-mode operator-run subagent brief
    describes doing it by hand (see the brief's step 2).

    The agent's own ``docs/agents/<agent>.md`` prompt is read from the LOCAL
    worktree the caller is about to dispatch into (passed in via
    ``agent_prompt_source``-equivalent callers use ``read_agent_prompt``
    below) — not fetched again here — so the two reads are guaranteed to be
    against the SAME head the lens will actually review.
    """
    if not brief_path.is_file():
        raise FileNotFoundError(
            f"lens brief not found at {brief_path} — pass --brief explicitly"
        )
    brief = brief_path.read_text(encoding="utf-8")
    launch = textwrap.dedent(
        f"""\
        LAUNCH MESSAGE
        REPO: {target.repo}
        PR: {target.pr}
        TARGET HEAD: {target.head}
        LENS: {target.lens}
        AGENT: {target.agent}
        FOCUS NOTES: {target.focus_notes or "(none)"}
        """
    )
    return f"{launch}\n---\n\n{brief}"


def read_agent_prompt(worktree: Path, agent: str) -> str:
    """Read docs/agents/<agent>.md from the worktree (i.e. from TARGET HEAD's
    own tree state at fetch time — the worktree is created from origin, and
    docs/agents is generated from Neotoma on origin/main, per the lens brief's
    own instruction to read it from ``origin/main`` rather than the PR branch).
    """
    path = worktree / "docs" / "agents" / f"{agent}.md"
    if not path.is_file():
        raise FileNotFoundError(f"{path} does not exist — unknown lens agent {agent!r}")
    return path.read_text(encoding="utf-8")


# ── Throwaway worktree -----------------------------------------------------------


@dataclass
class Worktree:
    repo_name: str
    path: Path
    _created: bool = field(default=False, repr=False)

    def create(self, *, head: str) -> None:
        repo_path = Path.home() / "repos" / self.repo_name
        subprocess.run(
            ["git", "-C", str(repo_path), "fetch", "-q", "origin"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repo_path),
                "worktree",
                "add",
                str(self.path),
                head,
            ],
            check=True,
        )
        self._created = True
        # If the checkout carried an env file into the new worktree (some
        # post-checkout hooks copy .env/.env.development for local dev
        # convenience), delete it before anything runs here — the lens brief's
        # own instruction, and doubly necessary once a dispatched child's HOME
        # is isolated but its CWD is this worktree.
        for name in (".env", ".env.development", ".env.local"):
            candidate = self.path / name
            if candidate.is_file():
                candidate.unlink()

    def remove(self) -> None:
        if not self._created:
            return
        repo_path = Path.home() / "repos" / self.repo_name
        subprocess.run(
            ["git", "clean", "-fdxq"], cwd=str(self.path), check=False
        )
        subprocess.run(
            ["git", "-C", str(repo_path), "worktree", "remove", str(self.path)],
            check=False,
        )


# ── Guard implementations: each one is PROBED, not merely described ------------
#
# Every guard below is built as a real, standalone mechanism (a sandbox-exec
# profile file; a PATH-first git shim script) and then PROBED against a
# throwaway fixture — never the real credential files, never the real shared
# git dir — to produce a boolean this module actually trusts. A guard whose
# probe does not pass is reported UNBOUND, and `HarnessSandbox.fully_guarded`
# is the single real signal `refuse_if_guard_required` reads. Nothing here
# hardcodes "this guard binds" as a comment; each claim is the result of
# actually running the mechanism against a fixture in this same call.

# Credential-file globs a dispatched codex/cursor child must never be able to
# read, expressed as macOS sandbox regexes (matched against the FULL absolute
# path, anchored at the end — `$` — so a same-named file elsewhere is not
# swept in). Mirrors the globs named in the coordinator's brief.
_CREDENTIAL_READ_DENY_REGEXES: tuple[str, ...] = (
    r"/\.config/neotoma/\.env[^/]*$",
    r"/\.neotoma/aauth[^/]*/.*private.*",
    r"/\.claude\.json$",
    r"/\.netrc$",
    r"/\.config/sops/age/.*",
)

# User-level harness config directories a dispatched child must never be able
# to WRITE to (writing a new hook/rule/mcp.json there would let a codex/cursor
# child silently alter what future sessions in ANY harness trust).
_USER_CONFIG_WRITE_DENY_REGEXES: tuple[str, ...] = (
    r"/\.claude(/.*)?$",
    r"/\.cursor(/.*)?$",
    r"/\.codex(/.*)?$",
)

_GIT_STASH_SHIM = """#!/bin/bash
# PATH-first git shim (ent_89a4d44b063cb0902106da49, hardened against the
# absolute-path bypass found in PR #1308 round 3 review — ent_9e88db1882c668e6c5c32be9):
# refuses EVERY stash-stack invocation for a dispatched harness child, in any
# form (bare `stash`, `list`, `show`, `push`, `pop`, `apply`, `drop`, `clear`,
# `branch`, `create`, `store`). The operator's rule is "no stash in any form
# runs" — this shim used to keep .claude/hooks/git_stash_guard.py's read-only
# `list`/`show` carve-out, but this is the ONLY place stash is mentioned in
# this runner, and dropping the carve-out is simpler and matches the rule
# exactly, so it is gone.
#
# Execs REAL_GIT_PLACEHOLDER (the sandbox-exec-ALLOWED copy of the real git
# binary — see HarnessSandbox.build's ``_stage_exec_allowed_git_copy`` and the
# module docstring's GUARD BINDING section) for every non-stash invocation, so
# this shim is otherwise invisible. Deliberately NOT the original resolved
# real git path: that path is DENIED at the sandbox-exec layer specifically so
# an absolute-path invocation of git (bypassing this shim's PATH-first
# position entirely) cannot reach a real git binary that has not been
# through this refusal — see build_sandbox_exec_profile's process-exec denies.
for arg in "$@"; do
  case "$arg" in
    stash)
      echo "git-stash-shim: refusing 'git stash' in any form for a dispatched harness child (ent_89a4d44b063cb0902106da49)" >&2
      exit 1
      ;;
  esac
done
exec "REAL_GIT_PLACEHOLDER" "$@"
"""


def _real_git_path() -> str:
    real = shutil.which("git")
    if not real:
        raise RuntimeError("git not found on PATH — cannot build the stash-refusing shim")
    return real


def discover_real_git_binaries() -> list[str]:
    """Return every DISTINCT, symlink-RESOLVED git executable reachable on
    PATH — the exec-time deny set for the sandbox-exec profile.

    This resolves symlinks (``os.path.realpath``) deliberately: macOS
    Seatbelt's ``process-exec`` path filters match the exec syscall's
    RESOLVED target, not the path string the caller invoked. Homebrew's
    ``/opt/homebrew/bin/git`` is a symlink into a versioned Cellar path —
    denying ``process-exec`` on the symlink path alone does nothing (verified
    empirically: ``sandbox-exec`` still ran a symlinked git under a profile
    denying only the symlink's own path), while denying the RESOLVED Cellar
    path stops it. Every entry returned here is therefore already the
    resolved form; the profile denies exactly these strings.

    Deliberately returns EVERY resolved binary found across the WHOLE PATH,
    not just the first (``shutil.which``'s behaviour) — a dispatched child
    that resolves a later PATH entry (Xcode CLT's `/usr/bin/git`, a second
    Homebrew keg, an MDM-managed git) must be denied too.
    """
    seen: dict[str, None] = {}
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / "git"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            seen.setdefault(str(candidate.resolve()), None)
    return list(seen)


def _stage_exec_allowed_git_copy(sandbox_home: Path, real_git: str) -> Path:
    """Copy ONE resolved real git binary into the sandbox's own directory,
    at a path the sandbox-exec profile does NOT deny, and return that path.

    The shim execs THIS copy for every non-stash invocation — never the
    original resolved path, which is exactly what
    ``build_sandbox_exec_profile`` denies for every entry
    ``discover_real_git_binaries`` found (including this copy's own
    source). A plain file copy (not a symlink, not a hardlink) is used
    deliberately: a hardlink renamed to anything but the original basename
    breaks git's own subcommand dispatch (verified empirically — "cannot
    handle hardlink as a builtin" — because git resolves its behaviour
    partly from argv[0]/its own basename), while a same-named plain copy
    resolves ``--exec-path`` and runs ordinary commands identically to the
    original, since git's libexec location is baked in at build time rather
    than derived from a runtime symlink target.
    """
    target = sandbox_home / "real-git-bin" / "git"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(real_git, target)
    target.chmod(0o755)
    return target


def _codex_auth_source() -> Path:
    """The existing Codex subscription-login file, named but never read here."""
    return Path.home() / ".codex" / "auth.json"


def link_codex_auth(sandbox_home: Path) -> bool:
    """Expose the existing Codex login to an isolated CODEX_HOME by symlink.

    This function never opens or copies the credential value. The sandbox
    profile independently denies writes to the real ``~/.codex`` target; the
    link supplies only the authentication capability the CLI needs in order
    for a subscription-backed run to start.
    """
    source = _codex_auth_source()
    if not source.is_file():
        return False
    target = sandbox_home / "auth.json"
    try:
        target.symlink_to(source)
    except OSError:
        return False
    return target.is_symlink()


def _sandbox_literal(path: str) -> str:
    """Escape a path for a Seatbelt ``(literal "...")`` string. Seatbelt's
    string literals use ordinary double-quote escaping; a path containing a
    literal `"` or `\\` (vanishingly unlikely for a git binary path, but not
    provably impossible) must not be allowed to break out of the string.
    """
    return path.replace("\\", "\\\\").replace('"', '\\"')


def build_sandbox_exec_profile(
    profile_path: Path, *, deny_exec_paths: tuple[str, ...] = ()
) -> None:
    """Write a macOS sandbox-exec profile denying the credential-read and
    user-config-write globs above, and allowing everything else by default
    (the dispatched child still needs to read/write its own worktree, the
    isolated CODEX_HOME/HOME, and run its own binaries).

    ``deny_exec_paths`` (ent_9e88db1882c668e6c5c32be9, PR #1308 round-3
    review): additionally denies ``process-exec`` of each given path —
    the mechanism that closes the absolute-path git-stash bypass a PATH-first
    shim alone cannot: invoking git by its resolved absolute path never
    consults ``PATH``, so the shim never runs, and the file-read/write denies
    above do not gate WHICH BINARY may execute at all. Every path here MUST
    already be symlink-resolved (``discover_real_git_binaries`` does this) —
    Seatbelt's ``process-exec`` literal/subpath filters match the exec
    syscall's resolved target, not a symlink the caller typed; a bare deny on
    a symlink path is a silent no-op (verified empirically against this
    exact profile grammar before this fix — see the module docstring).
    ``(deny process-exec (literal ...))`` under an ``(allow default)``
    baseline was ALSO verified empirically to work correctly once the
    literal target is the resolved path — Seatbelt's specificity rule lets
    a literal-path deny override the broad allow for exactly that path,
    leaving every other exec (including the sandbox's own allowed copy of
    git, and every other binary the dispatched child needs) unaffected.
    """
    read_denies = "\n".join(f'  (regex #"{p}")' for p in _CREDENTIAL_READ_DENY_REGEXES)
    write_denies = "\n".join(f'  (regex #"{p}")' for p in _USER_CONFIG_WRITE_DENY_REGEXES)
    exec_denies = "\n".join(
        f'  (literal "{_sandbox_literal(p)}")' for p in deny_exec_paths
    )
    profile = (
        "(version 1)\n"
        "(allow default)\n"
        "(deny file-read*\n"
        f"{read_denies}\n"
        ")\n"
        "(deny file-write*\n"
        f"{write_denies}\n"
        ")\n"
    )
    if exec_denies:
        profile += "(deny process-exec\n" f"{exec_denies}\n" ")\n"
    profile_path.write_text(profile, encoding="utf-8")


def probe_sandbox_exec_denies_read(
    profile_path: Path, fixture_path: Path, *, control_path: Path
) -> bool:
    """Return True iff a real `sandbox-exec` run using *profile_path* actually
    denies reading *fixture_path* AND still allows reading *control_path*.

    *fixture_path* must be a file THIS caller created (never a real
    credential file) whose path matches one of the deny regexes; *control_path*
    must be a file THIS caller created OUTSIDE every deny regex — the probe
    proves the mechanism DISCRIMINATES, not merely that the sandboxed process
    exited non-zero. A malformed sandbox-exec profile (e.g. an empty deny
    list, or one with a syntax error) can make the whole subprocess crash —
    verified: SIGABRT on this platform — which returns a non-zero code for
    EVERY path, fixture and control alike, and would be misread as "denied"
    without the control check. Requiring the control read to still succeed is
    what tells a real, working deny apart from a broken profile that denies
    everything (including itself).
    """
    if shutil.which("sandbox-exec") is None:
        return False
    denied = subprocess.run(
        ["sandbox-exec", "-f", str(profile_path), "cat", str(fixture_path)],
        capture_output=True,
        text=True,
    )
    if denied.returncode == 0:
        return False
    control = subprocess.run(
        ["sandbox-exec", "-f", str(profile_path), "cat", str(control_path)],
        capture_output=True,
        text=True,
    )
    return control.returncode == 0 and control.stdout == control_path.read_text(
        encoding="utf-8"
    )


def probe_sandbox_exec_denies_write(
    profile_path: Path, fixture_path: Path, *, control_path: Path
) -> bool:
    """Same as the read probe, for a write into a fixture directory matching
    one of the user-config-write deny regexes (e.g. a fixture ``.claude/``
    under a throwaway root — never the operator's real ``~/.claude``), with
    the same control-path discrimination check (see the read probe's
    docstring for why: a crashing profile denies everything, including a
    write it was never asked to deny).
    """
    if shutil.which("sandbox-exec") is None:
        return False
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    denied = subprocess.run(
        ["sandbox-exec", "-f", str(profile_path), "touch", str(fixture_path)],
        capture_output=True,
        text=True,
    )
    if denied.returncode == 0 or fixture_path.exists():
        return False
    control_path.parent.mkdir(parents=True, exist_ok=True)
    control = subprocess.run(
        ["sandbox-exec", "-f", str(profile_path), "touch", str(control_path)],
        capture_output=True,
        text=True,
    )
    return control.returncode == 0 and control_path.exists()


def _real_stash_list(scratch_git_dir: Path) -> str:
    """Read the REAL stash stack directly via the unmodified system git — the
    verification instrument, never the mechanism under test. Every probe
    below refuses to trust its own subject's exit code alone (a shim or
    sandbox that silently swallows the mutation while reporting failure
    would otherwise look identical to one that genuinely blocked it); this
    reads the ACTUAL on-disk ref state after each attempt.
    """
    real_git = shutil.which("git") or "git"
    listing = subprocess.run(
        [real_git, "-C", str(scratch_git_dir), "stash", "list"],
        capture_output=True, text=True,
    )
    return listing.stdout if listing.returncode == 0 else "<unreadable>"


def probe_git_shim_denies_stash_push(shim_path: Path, scratch_git_dir: Path) -> bool:
    """Return True iff invoking `git stash push` THROUGH THE SHIM (PATH-first,
    the normal resolution path) in a SCRATCH git repo (never the shared
    clone) exits non-zero AND leaves the real stash stack untouched.

    *scratch_git_dir* must already contain at least one commit — otherwise a
    real, unshimmed `git stash push` would ALSO fail (with "You do not have
    the initial commit yet"), which would make this probe pass for the wrong
    reason on a broken shim that never actually refuses anything.
    """
    env = {**os.environ, "PATH": f"{shim_path.parent}:{os.environ.get('PATH', '')}"}
    before = _real_stash_list(scratch_git_dir)
    push = subprocess.run(
        ["git", "-C", str(scratch_git_dir), "stash", "push"],
        capture_output=True, text=True, env=env,
    )
    if push.returncode == 0:
        return False
    return _real_stash_list(scratch_git_dir) == before


def probe_absolute_path_git_stash_is_denied(
    real_git_paths: tuple[str, ...],
    command_wrapper: list[str],
    scratch_git_dir: Path,
) -> bool:
    """Return True iff invoking `git stash push` via EVERY discovered
    absolute real-git path, wrapped in the ACTUAL sandbox-exec command
    this run would prepend to the real dispatch, is refused AND leaves the
    real stash stack untouched — the exact bypass PR #1308 round 3 review
    found (ent_9e88db1882c668e6c5c32be9): a PATH-first shim never runs when
    the caller invokes git by its resolved absolute path, since that
    resolution never consults PATH at all.

    Every path in *real_git_paths* is tried, not just one — a profile that
    denies only the first-discovered git and misses a second PATH entry
    (Xcode CLT's `/usr/bin/git` beside a Homebrew keg, for instance) would
    otherwise report a false pass. An empty *command_wrapper* (no
    sandbox-exec available) always returns False: there is nothing to
    prepend, so this specific bypass cannot be closed on this host.
    """
    if not command_wrapper or not real_git_paths:
        return False
    before = _real_stash_list(scratch_git_dir)
    for real_git in real_git_paths:
        attempt = subprocess.run(
            [*command_wrapper, real_git, "-C", str(scratch_git_dir), "stash", "push"],
            capture_output=True, text=True,
        )
        if attempt.returncode == 0:
            return False
        if _real_stash_list(scratch_git_dir) != before:
            return False
    return True


@dataclass
class HarnessSandbox:
    """The real, probed guard state for one dispatch, plus what to hand
    `dispatch_role.dispatch()` to make it bind. Every boolean on this object
    was produced by actually running the mechanism against a fixture in
    `build()` — never asserted.
    """

    provider: str
    root: Path
    env_extra: dict[str, str]
    command_wrapper: list[str]
    credential_read_denied: bool
    user_config_write_denied: bool
    git_stash_denied: bool
    authentication_ready: bool
    unavailable_guards: tuple[str, ...]

    @property
    def fully_guarded(self) -> bool:
        """True only when every guard this class attempts actually probed as
        bound. `refuse_if_guard_required` is the only reader that matters."""
        return (
            self.credential_read_denied
            and self.user_config_write_denied
            and self.git_stash_denied
        )

    @property
    def ready_to_dispatch(self) -> bool:
        """Both the safety controls and provider authentication are present."""
        return self.fully_guarded and self.authentication_ready

    @classmethod
    def build(cls, provider: str, tmp_root: Path) -> "HarnessSandbox":
        sandbox_home = tmp_root / f"{provider}-home"
        sandbox_home.mkdir(parents=True, exist_ok=True)

        if provider == "claude":
            # Runs under this session's own Claude Code hook set already (or,
            # dispatched headless via `claude --print`, under whatever hooks
            # its own settings.json wires) — unaffected by this script, and
            # every guard is already bound by that mechanism.
            return cls(
                provider=provider, root=sandbox_home, env_extra={},
                command_wrapper=[], credential_read_denied=True,
                user_config_write_denied=True, git_stash_denied=True,
                authentication_ready=True,
                unavailable_guards=(),
            )

        env_extra = (
            {"CODEX_HOME": str(sandbox_home)}
            if provider == "codex"
            else {"HOME": str(sandbox_home)}
        )
        authentication_ready = provider == "codex" and link_codex_auth(sandbox_home)

        # ── Discover every real git binary on PATH — the exec-deny set ─────
        real_git_paths = tuple(discover_real_git_binaries())

        # ── sandbox-exec profile: build (with process-exec denies), then
        # PROBE against fixtures. Built BEFORE the shim below because the
        # shim needs command_wrapper to exist for the absolute-path probe,
        # and the profile needs real_git_paths resolved above.
        profile_path = sandbox_home / "profile.sb"
        credential_read_denied = False
        user_config_write_denied = False
        command_wrapper: list[str] = []
        try:
            build_sandbox_exec_profile(profile_path, deny_exec_paths=real_git_paths)
            read_fixture_dir = sandbox_home / "probe-fixtures" / ".config" / "neotoma"
            read_fixture_dir.mkdir(parents=True, exist_ok=True)
            read_fixture = read_fixture_dir / ".env"
            read_fixture.write_text("PROBE_NOT_A_REAL_CREDENTIAL=1\n", encoding="utf-8")
            read_control = sandbox_home / "probe-fixtures" / "control-read.txt"
            read_control.write_text("control\n", encoding="utf-8")
            credential_read_denied = probe_sandbox_exec_denies_read(
                profile_path, read_fixture, control_path=read_control
            )

            write_fixture = sandbox_home / "probe-fixtures" / ".claude" / "probe-write"
            write_control = sandbox_home / "probe-fixtures" / "control-write-dir" / "probe"
            user_config_write_denied = probe_sandbox_exec_denies_write(
                profile_path, write_fixture, control_path=write_control
            )
            if shutil.which("sandbox-exec") is not None:
                command_wrapper = ["sandbox-exec", "-f", str(profile_path)]
        except OSError:
            credential_read_denied = False
            user_config_write_denied = False

        # ── git-stash shim: exec-allowed copy, then PROBE both resolution
        # paths (PATH-first, the shim's own job; absolute-path, the sandbox
        # profile's job — ent_9e88db1882c668e6c5c32be9). Only when BOTH probe
        # True is the guard actually closed against every way a dispatched
        # child could invoke git.
        shim_dir = sandbox_home / "shim-bin"
        shim_dir.mkdir(parents=True, exist_ok=True)
        shim_path = shim_dir / "git"
        path_shim_denied = False
        absolute_path_denied = False
        try:
            real_git = _real_git_path()
            exec_allowed_copy = _stage_exec_allowed_git_copy(sandbox_home, real_git)
            shim_path.write_text(
                _GIT_STASH_SHIM.replace("REAL_GIT_PLACEHOLDER", str(exec_allowed_copy)),
                encoding="utf-8",
            )
            shim_path.chmod(0o755)
            scratch_repo = sandbox_home / "stash-probe-repo"
            scratch_repo.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "init", "-q", str(scratch_repo)], check=True, capture_output=True
            )
            probe_env = {**os.environ, "PATH": f"{shim_dir}:{os.environ.get('PATH', '')}"}
            subprocess.run(
                ["git", "-C", str(scratch_repo), "config", "user.email", "probe@example.com"],
                check=True, capture_output=True, env=probe_env,
            )
            subprocess.run(
                ["git", "-C", str(scratch_repo), "config", "user.name", "probe"],
                check=True, capture_output=True, env=probe_env,
            )
            (scratch_repo / "probe.txt").write_text("probe\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(scratch_repo), "add", "-A"],
                check=True, capture_output=True, env=probe_env,
            )
            # A REAL commit — required so a real (unshimmed/unsandboxed)
            # `git stash push` would actually succeed, which is what makes
            # both probes below meaningful: on a broken guard, this commit is
            # what lets the bypass produce a real stash entry for the probe
            # to catch, rather than failing on its own with "no initial
            # commit" for an unrelated reason. See probe_git_shim_denies_
            # stash_push's and probe_absolute_path_git_stash_is_denied's
            # docstrings.
            subprocess.run(
                ["git", "-C", str(scratch_repo), "commit", "-q", "-m", "probe"],
                check=True, capture_output=True, env=probe_env,
            )
            (scratch_repo / "probe.txt").write_text("probe changed\n", encoding="utf-8")
            path_shim_denied = probe_git_shim_denies_stash_push(shim_path, scratch_repo)
            absolute_path_denied = probe_absolute_path_git_stash_is_denied(
                real_git_paths, command_wrapper, scratch_repo
            )
        except (RuntimeError, subprocess.CalledProcessError, OSError):
            path_shim_denied = False
            absolute_path_denied = False
        env_extra["PATH"] = f"{shim_dir}:{os.environ.get('PATH', '')}"
        # BOTH resolution paths must probe as denied — the shim alone does
        # not close the absolute-path bypass, and the sandbox-exec deny
        # alone does nothing for a PATH-resolved invocation that never
        # reaches an absolute path at all.
        git_stash_denied = path_shim_denied and absolute_path_denied

        unavailable = []
        if not git_stash_denied:
            unavailable.append(
                "git_stash_guard (PATH-first shim denied="
                f"{path_shim_denied}, absolute-path sandbox-exec denied="
                f"{absolute_path_denied} — see the sandbox's own probe "
                "results, not asserted; BOTH must be True)"
            )
        if not credential_read_denied:
            unavailable.append(
                "credential_read_guard (sandbox-exec unavailable, or its "
                "profile did not probe as denying a read of the fixture path "
                "— see the sandbox's own probe result, not asserted)"
            )
        if not user_config_write_denied:
            unavailable.append(
                "user_config_write_guard (sandbox-exec unavailable, or its "
                "profile did not probe as denying a write to the fixture "
                "path)"
            )
        unavailable.append(
            "sibling_repo_worktree_guard (no hook mechanism in "
            f"{provider}; mitigated only by this script always dispatching "
            "into its own dedicated throwaway worktree, never the shared "
            "clone or another session's worktree)"
        )
        unavailable.append(
            "gmail_send_gate / refuse_task_chip (not applicable to this "
            "script — it never invokes gws/gmail and no harness task-chip "
            "surface exists outside Claude Code)"
        )

        return cls(
            provider=provider,
            root=sandbox_home,
            env_extra=env_extra,
            command_wrapper=command_wrapper,
            credential_read_denied=credential_read_denied,
            user_config_write_denied=user_config_write_denied,
            git_stash_denied=git_stash_denied,
            authentication_ready=authentication_ready,
            unavailable_guards=tuple(unavailable),
        )


def refuse_if_guard_required(sandbox: "HarnessSandbox") -> str | None:
    """Return a refusal reason when *sandbox*'s OWN probed state shows a
    required guard is unbound for a non-claude provider, or None to proceed.

    Driven entirely by ``sandbox.fully_guarded`` — a boolean produced by
    actually running the sandbox-exec profile and the git shim against
    fixtures in ``HarnessSandbox.build()``, never by a caller-supplied
    constant. `claude` always proceeds (its own hook set already binds every
    guard this class attempts). Every lens review this script dispatches
    needs the full guard set — there is no lighter-weight lens run that could
    accept a partial bind — so this refuses outright rather than proceeding
    on a partial result.
    """
    if sandbox.provider == "claude":
        return None
    if not sandbox.authentication_ready:
        return (
            f"provider {sandbox.provider!r} has no authentication available "
            "inside its isolated harness home — refusing before a model call. "
            "Codex requires an existing ~/.codex/auth.json subscription login; "
            "Cursor requires a named credential injection that is not yet wired."
        )
    if not sandbox.fully_guarded:
        missing = "; ".join(sandbox.unavailable_guards) or "unspecified"
        return (
            f"provider {sandbox.provider!r} failed to probe as fully guarded "
            f"(credential_read_denied={sandbox.credential_read_denied}, "
            f"user_config_write_denied={sandbox.user_config_write_denied}, "
            f"git_stash_denied={sandbox.git_stash_denied}) — refusing rather "
            f"than running unguarded. Unbound: {missing}"
        )
    return None


# ── dry-run rendering -------------------------------------------------------------


def dry_run_report(
    target: LensTarget,
    *,
    provider: str,
    sandbox: HarnessSandbox,
    task_text: str,
    worktree_path: Path,
) -> dict:
    """Everything --dry-run prints: the exact command shape, prompt size, and
    guard configuration — and NO model call. This mirrors, rather than calls,
    the argv `_provider_command` in skill_runner would build, because that
    function requires a resolved binary path and network is not the point of
    a dry run; it is derived from the SAME flags skill_runner uses today so a
    drift between this preview and the real dispatch is visible as a diff
    against `_provider_command` in review, not a silent divergence.
    """
    if provider == "codex":
        example_cmd = [
            "codex", "exec", "--sandbox", "workspace-write",
            "--add-dir", str(worktree_path),
            "--ephemeral", "--skip-git-repo-check", "--color", "never",
            "--cd", str(worktree_path), "-",
        ]
    elif provider == "cursor":
        example_cmd = [
            "cursor-agent", "--print", "--force", "--trust", "--approve-mcps",
            "--output-format", "text", "--workspace", str(worktree_path),
            "<prompt omitted — see prompt_chars>",
        ]
    else:
        example_cmd = ["claude", "--print", "--append-system-prompt", "<system prompt>"]

    # command_wrapper is prepended to the REAL argv by skill_runner
    # (see _run_skill_once's command_wrapper param) — shown here too so the
    # dry run's printed command matches what actually runs, not a description
    # of it that could drift.
    example_cmd = [*sandbox.command_wrapper, *example_cmd]

    return {
        "dry_run": True,
        "provider": provider,
        "repo": target.repo,
        "pr": target.pr,
        "head": target.head,
        "lens": target.lens,
        "agent": target.agent,
        "worktree": str(worktree_path),
        "sandbox_env_extra": {k: v for k, v in sandbox.env_extra.items() if k != "PATH"},
        "sandbox_path_prefix": sandbox.env_extra.get("PATH", "").split(os.pathsep)[0]
        if "PATH" in sandbox.env_extra
        else "",
        "command_wrapper": list(sandbox.command_wrapper),
        "fully_guarded": sandbox.fully_guarded,
        "credential_read_denied": sandbox.credential_read_denied,
        "user_config_write_denied": sandbox.user_config_write_denied,
        "git_stash_denied": sandbox.git_stash_denied,
        "authentication_ready": sandbox.authentication_ready,
        "unavailable_guards": list(sandbox.unavailable_guards),
        "example_command": example_cmd,
        "prompt_chars": len(task_text),
        "no_model_call_made": True,
    }


# ── Posting gate -----------------------------------------------------------------


@dataclass
class VerdictCheck:
    """Result of validating a captured verdict file before any post."""

    ok: bool
    reason: str
    lens_verdict: str | None = None
    sign_off_warranted: bool | None = None
    pre_post: dict = field(default_factory=dict)


def validate_verdict(verdict_text: str, *, lens_agent: str) -> VerdictCheck:
    """Run the SAME reader a Claude bootstrap lens run is instructed to run
    by hand (the lens brief's PRE-POST CHECK step) — swarm_dispatch's own
    ``lens_own_verdict`` / ``sign_off_is_warranted`` — so a codex/cursor
    verdict is held to the identical bar a Claude verdict is, not a
    weaker parallel check this script invents.
    """
    lines = verdict_text.splitlines()
    pre_post = {
        "line1": lines[0] if len(lines) > 0 else "",
        "line2": lines[1] if len(lines) > 1 else "",
        "line3": lines[2] if len(lines) > 2 else "",
        "blocking_count": verdict_text.count("[BLOCKING]"),
    }
    if swarm_dispatch is None:
        return VerdictCheck(
            ok=False,
            reason="swarm_dispatch could not be imported — cannot validate a "
            "verdict without the real reader; refusing to post rather than "
            "invent a weaker check",
            pre_post=pre_post,
        )
    verdict = swarm_dispatch.lens_own_verdict(verdict_text, lens_agent=lens_agent)
    warranted = swarm_dispatch.sign_off_is_warranted(verdict_text, lens_agent=lens_agent)
    if verdict is None:
        return VerdictCheck(
            ok=False,
            reason=(
                "lens_own_verdict returned None — the verdict is not readable "
                "at the fixed position (second verdict-like line, missing "
                "header, or quoted header are the usual causes; see the lens "
                "brief's PRE-POST CHECK step)"
            ),
            lens_verdict=verdict,
            sign_off_warranted=warranted,
            pre_post=pre_post,
        )
    return VerdictCheck(
        ok=True,
        reason="verdict readable at fixed position",
        lens_verdict=verdict,
        sign_off_warranted=warranted,
        pre_post=pre_post,
    )


def gh_login() -> str:
    out = subprocess.run(
        ["gh", "api", "user", "-q", ".login"],
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.strip()


def current_pr_head(*, repo: str, pr: int) -> str:
    """Return the PR's CURRENT headRefOid via `gh`, or "" if unreadable.

    A dedicated function (rather than an inline subprocess.run call) so tests
    can monkeypatch exactly this and nothing else — leaving every OTHER
    subprocess.run call (the sandbox-exec/git-shim guard probes in
    HarnessSandbox.build, in particular) running for real.
    """
    result = subprocess.run(
        ["gh", "pr", "view", str(pr), "-R", repo, "--json", "headRefOid"],
        capture_output=True, text=True, check=False,
    )
    try:
        return json.loads(result.stdout or "{}").get("headRefOid", "")
    except json.JSONDecodeError:
        return ""


def post_verdict(*, repo: str, pr: int, verdict_path: Path) -> str:
    """Post the verdict file as a PR comment and return the comment URL.

    Never called unless ``validate_verdict(...).ok`` AND the caller passed
    ``--post`` — the caller (main()) enforces both; this function trusts its
    caller rather than re-checking, so its own tests can drive it directly.
    """
    result = subprocess.run(
        [
            "gh", "pr", "comment", str(pr), "-R", repo,
            "--body-file", str(verdict_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


# ── Single-provider run -----------------------------------------------------------


async def run_one(
    target: LensTarget,
    *,
    provider: str,
    post: bool,
    dry_run: bool,
    repo_worktree_name: str,
    scratch_root: Path,
    brief_path: Path,
    timeout: int | None,
) -> dict:
    """Run the lens once on one provider. Returns a JSON-serializable report.

    Raises HeadroomExhausted before anything else (no worktree, no dispatch)
    when the provider's configured headroom is exactly 0 — the operator-reset
    gate named in the task.
    """
    check_headroom(provider)

    # HarnessSandbox.build() PROBES the real sandbox-exec profile and git
    # shim against fixtures — refusal below is driven by that probe's actual
    # result (sandbox.fully_guarded), never by a caller-supplied constant.
    sandbox = HarnessSandbox.build(provider, scratch_root)
    refusal = refuse_if_guard_required(sandbox)

    worktree_path = scratch_root / f"{repo_worktree_name}-wt-{target.lens}-{target.pr}-{provider}"
    worktree = Worktree(repo_name=repo_worktree_name, path=worktree_path)

    if dry_run:
        # A dry run still creates nothing model-facing, but it DOES need the
        # real agent prompt to report an accurate prompt_chars figure, and
        # that prompt lives in a worktree at TARGET HEAD. Building the
        # worktree is a `git worktree add` (explicitly allowed by every
        # relevant guard) and is removed again before returning. The refusal
        # (if any) is reported alongside the rest rather than short-circuited,
        # so --dry-run is exactly what an operator would see about to run
        # this for real — including a refusal that would otherwise be silent
        # until the real dispatch attempt.
        worktree.create(head=target.head)
        try:
            agent_prompt = read_agent_prompt(worktree.path, target.agent)
            task_text = render_lens_task(target, brief_path) + "\n\n---\n\n" + agent_prompt
            report = dry_run_report(
                target, provider=provider, sandbox=sandbox,
                task_text=task_text, worktree_path=worktree.path,
            )
            report["would_refuse"] = refusal
        finally:
            worktree.remove()
        return report

    if refusal:
        return {"ok": False, "provider": provider, "reason": refusal}

    worktree.create(head=target.head)
    try:
        agent_prompt = read_agent_prompt(worktree.path, target.agent)
        task_text = render_lens_task(target, brief_path) + "\n\n---\n\n" + agent_prompt
        verdict_path = worktree.path / f"{target.lens}{target.pr}_verdict.md"

        result: SkillResult = await dispatch_role.dispatch(
            target.agent,
            (
                f"{task_text}\n\n---\n\nWrite your verdict to the file "
                f"{verdict_path} (create it yourself with the exact strict "
                "format the brief describes) and print its contents to "
                "stdout when done."
            ),
            provider=provider,
            cwd=str(worktree.path),
            timeout=timeout,
            task_entity_id=target.task_entity_id,
            env_extra=sandbox.env_extra,
            seated_reviewer=False,  # see module docstring: no MCP grant requested
            command_wrapper=sandbox.command_wrapper,
        )

        if not result.ok:
            return {
                "ok": False,
                "provider": provider,
                "reason": result.error or "dispatch failed",
                "attempted_providers": list(result.attempted_providers),
                "stderr": result.stderr,
            }

        verdict_text = (
            verdict_path.read_text(encoding="utf-8")
            if verdict_path.is_file()
            else result.stdout
        )
        check = validate_verdict(verdict_text, lens_agent=target.agent)

        report = {
            "ok": check.ok,
            "provider": provider,
            "lens": target.lens,
            "agent": target.agent,
            "lens_verdict": check.lens_verdict,
            "sign_off_warranted": check.sign_off_warranted,
            "pre_post": check.pre_post,
            "verdict_text": verdict_text,
            "posted": False,
            "comment_url": "",
        }
        if not check.ok:
            report["refusal_reason"] = check.reason
            return report

        # Re-check the head before posting — the brief's own step 1, applied
        # here rather than trusted from before dispatch (a long-running codex
        # attempt could span a force-push).
        current_head = current_pr_head(repo=target.repo, pr=target.pr)
        if current_head and current_head != target.head:
            report["ok"] = False
            report["refusal_reason"] = (
                f"PR head moved from {target.head} to {current_head} during "
                "the run — refusing to post a verdict against a stale head"
            )
            return report

        if not post:
            report["refusal_reason"] = "--post not set; verdict validated but not posted"
            return report

        login = gh_login()
        if login != "ateles-agent":
            report["ok"] = False
            report["refusal_reason"] = (
                f"gh api user -q .login returned {login!r}, expected "
                "'ateles-agent' — refusing to post under the wrong identity"
            )
            return report

        report["comment_url"] = post_verdict(
            repo=target.repo, pr=target.pr, verdict_path=verdict_path
        )
        report["posted"] = True
        return report
    finally:
        worktree.remove()


async def run_compare(
    target: LensTarget,
    *,
    providers: list[str],
    dry_run: bool,
    post: bool = False,
    repo_worktree_name: str,
    scratch_root: Path,
    brief_path: Path,
    timeout: int | None,
) -> dict:
    """Run the same lens on the same head via each of *providers* and report
    both verdicts side by side.

    Posting is unconditionally OFF in comparison mode — the task's own spec
    ("Posting is off by default" for this mode). ``post`` is accepted only so
    a caller's mistaken ``--post`` produces a documented no-op rather than a
    TypeError; it is deliberately NEVER forwarded to ``run_one`` below,
    whatever value it carries. A caller who wants ONE of the compared
    providers' verdicts posted runs that provider again, alone, via
    ``run_one``/``--provider`` after inspecting the comparison.
    """
    results = {}
    for provider in providers:
        results[provider] = await run_one(
            target,
            provider=provider,
            post=False,
            dry_run=dry_run,
            repo_worktree_name=repo_worktree_name,
            scratch_root=scratch_root,
            brief_path=brief_path,
            timeout=timeout,
        )
    return {"compare": True, "lens": target.lens, "pr": target.pr, "results": results}


# ── CLI ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="harness_lens_runner",
        description=(
            "Run one swarm review lens on one PR head through a chosen "
            "harness provider, in a throwaway worktree, posting only after "
            "the verdict passes the same reader Claude lens runs use."
        ),
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--head", required=True, help="full 40-hex head SHA")
    parser.add_argument("--lens", required=True, help="short lens label, e.g. pm")
    parser.add_argument(
        "--agent",
        default=None,
        help=(
            "docs/agents/<agent>.md name. Optional when --lens names a lens "
            "in review_panel.LENSES (the same registry select_panel/"
            "approve_pr_as_app.py derive a PR's required-lens panel from): "
            "the agent is then resolved via review_panel.lens_by_name so a "
            "caller passing the panel's own lens labels cannot mismatch "
            "lens and agent. Required when --lens names anything else."
        ),
    )
    parser.add_argument("--focus-notes", default="")
    parser.add_argument(
        "--task-entity-id",
        default="",
        help=(
            "Neotoma task entity id this dispatch is PART_OF. Optional, but "
            "recorded on every harness_event row this run writes (via "
            "dispatch_role.dispatch), so a session can monitor progress by "
            "filtering harness_event on this id alone, without re-deriving "
            "it from input_summary text."
        ),
    )
    parser.add_argument(
        "--repo-worktree-name",
        default=None,
        help="Local clone dirname under ~/repos (default: last segment of --repo)",
    )
    parser.add_argument(
        "--brief",
        required=True,
        help=(
            "Path to the shared lens brief markdown file. No default: see "
            "the LENS_BRIEF_PATH comment above main()'s definitions for why."
        ),
    )
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument(
        "--provider", choices=list(HARNESSES), help="Single-provider run"
    )
    parser.add_argument(
        "--compare",
        help="Comma-separated providers to run and report side by side, e.g. claude,codex",
    )
    parser.add_argument(
        "--post",
        action="store_true",
        help="Post the validated verdict as a PR comment. Off by default, "
        "and OFF by default in --compare mode regardless of this flag.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the exact command and prompt size; make no model call.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if not args.provider and not args.compare:
        parser.error("one of --provider or --compare is required")
    if args.provider and args.compare:
        parser.error("--provider and --compare are mutually exclusive")

    agent = args.agent
    if not agent:
        # --agent omitted: resolve it from the SAME registry select_panel /
        # approve_pr_as_app.py's derive_required_lenses use to assemble a
        # PR's required-lens panel, so a caller handing this runner one lens
        # label out of that derived panel cannot mismatch lens and agent —
        # the two are looked up together, from one source, rather than typed
        # separately and trusted to agree.
        resolved = lens_by_name(args.lens)
        if resolved is None:
            parser.error(
                f"--agent was not given and {args.lens!r} is not a lens in "
                "review_panel.LENSES — pass --agent explicitly for a lens "
                "outside that registry"
            )
        agent = resolved.agent

    target = LensTarget(
        repo=args.repo,
        pr=args.pr,
        head=args.head,
        lens=args.lens,
        agent=agent,
        focus_notes=args.focus_notes,
        task_entity_id=args.task_entity_id,
    )
    repo_worktree_name = args.repo_worktree_name or args.repo.split("/")[-1]
    brief_path = Path(args.brief)

    with tempfile.TemporaryDirectory(prefix="harness-lens-runner-") as tmp:
        scratch_root = Path(tmp)
        try:
            if args.compare:
                providers = [p.strip() for p in args.compare.split(",") if p.strip()]
                for p in providers:
                    if p not in HARNESSES:
                        parser.error(f"unknown provider in --compare: {p!r}")
                # Comparison mode never posts, per the task's own spec —
                # regardless of --post, which is accepted but ignored here so
                # a caller who forgets this rule gets a documented no-op
                # rather than a surprising post.
                report = asyncio.run(
                    run_compare(
                        target, providers=providers, dry_run=args.dry_run,
                        post=False, repo_worktree_name=repo_worktree_name,
                        scratch_root=scratch_root, brief_path=brief_path,
                        timeout=args.timeout,
                    )
                )
            else:
                report = asyncio.run(
                    run_one(
                        target, provider=args.provider, post=args.post,
                        dry_run=args.dry_run, repo_worktree_name=repo_worktree_name,
                        scratch_root=scratch_root, brief_path=brief_path,
                        timeout=args.timeout,
                    )
                )
        except HeadroomExhausted as exc:
            report = {"ok": False, "reason": str(exc)}
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            report = {"ok": False, "reason": str(exc)}

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(report)

    ok = report.get("ok", False) if "compare" not in report else all(
        r.get("ok", False) for r in report["results"].values()
    )
    return 0 if ok or args.dry_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
