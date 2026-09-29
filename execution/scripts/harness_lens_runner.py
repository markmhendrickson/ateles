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
protections. Those hooks remain useful for Claude, but they govern Claude tool
calls rather than direct subprocess/file/service access and therefore are not
treated as credential containment. Every provider, including Claude, must
pass the same outer enforcement probes or is refused before launch.

For every provider, this script builds and MECHANICALLY BINDS the two guards
whose absence has a concrete blast radius, and PROBES each one — against a
fixture, never a real credential file or the real shared git dir — before
ever deciding a dispatch may proceed (see ``HarnessSandbox.build()``,
``probe_sandbox_exec_denies_read/write``,
``probe_stash_effect_denied_across_git_binaries``):

  * **Credential-file reads, binding integrity, and user-config writes** — a macOS
    ``sandbox-exec`` profile (``build_sandbox_exec_profile``) denies
    ``file-read*`` on the credential-file globs named in this task
    (Neotoma auth, GitHub CLI hosts, git credential stores, SSH private keys,
    ``~/.claude.json``, ``~/.netrc``, and SOPS age keys), denies execution of
    ordinary keychain/credential helpers, blocks the keychain service lookup,
    and
    ``file-write*`` on the credential names and their narrow security-relevant
    config ancestors, plus ``~/.claude``, ``~/.cursor``, ``~/.codex``. The
    profile separately confines writes to isolated runtime scratch and the
    exact verdict path, leaving the rest of the review checkout read-only.
    A disposable adversarial fixture and an ordinary-filesystem positive
    control probe this boundary before dispatch. The
    profile is prepended to the dispatched child's REAL argv via
    ``command_wrapper=["/usr/bin/sandbox-exec", "-f", <profile>]`` — see
    ``skill_runner._run_skill_once``'s ``command_wrapper`` parameter, added
    specifically so this could bind onto the actual subprocess rather than
    describe an intended mitigation beside code that runs unwrapped.
    The profile is positively exercised only against synthetic credential
    files and helper executables; it never opens a real credential store or
    keychain. **What this does NOT claim**: Codex's own ``workspace-write`` sandbox is
    not the read-deny mechanism. More importantly, macOS does not permit
    Codex to apply that inner Seatbelt profile while this outer
    ``sandbox-exec`` profile is already active: the nested operation fails
    with ``sandbox_apply: Operation not permitted`` before any agent command
    can run. The runner therefore tells the existing Codex adapter to use
    ``--sandbox danger-full-access`` for this dispatch only. That name means
    Codex installs no second sandbox; it does NOT remove the already-bound
    outer profile, which remains the effect-level enforcement boundary for
    credential reads, user-config writes, and stash effects. Generic Codex
    dispatches still use ``workspace-write``. ``CODEX_HOME``/``HOME``
    overrides still relocate each CLI's OWN
    config store (so the child cannot read the operator's real
    ``~/.codex/config.toml``, whose ``approval_policy = "never"`` and
    per-project trust would be the opposite of safe for an unattended run).
    The one required capability, ChatGPT subscription authentication, is
    exposed without copying its value: the isolated ``CODEX_HOME/auth.json``
    is a symlink to the existing login file, while the outer sandbox denies
    writes to the real ``~/.codex`` target. If that source is absent, the
    runner refuses before dispatch rather than starting a model call that
    cannot authenticate.
  * **``git stash``** — denied by EFFECT, not by binary, as of round 4
    (ent_9e88db1882c668e6c5c32be9's own round-4 follow-up). Rounds 1-3 tried
    enforcing this by refusing/enumerating specific git BINARIES: a
    PATH-first shim (round 1's revision), then a ``process-exec`` deny for
    every git binary a ``PATH`` scan could resolve, symlinks included (round
    3, after round 3's own review found an absolute-path invocation bypassed
    the shim entirely). Round 4's review found a THIRD real git binary that
    PATH-scan enumeration never found — Xcode's own bundled copy, reachable
    via ``xcrun -f git`` or its own absolute path, never on ``PATH`` — and a
    stash through it landed a real entry while the mechanism reported itself
    fully bound. **Enumerating binaries loses by construction: there is
    always another copy.** The fix instead denies the EFFECT every git
    binary must produce to record a stash — writing (or reading) the stash
    ref itself:

    * ``build_sandbox_exec_profile`` denies ``file-write*`` AND
      ``file-read*`` on ``refs/stash`` and its reflog (``logs/refs/stash``),
      matched for both a plain repo's ``.git`` and the worktree-common-dir
      equivalent (``.git/worktrees/<name>/refs/stash`` — see
      ``_STASH_REF_DENY_REGEXES``). Every git binary that has ever existed
      or will exist records a stash by writing this SAME ref path, so this
      deny is binary-agnostic: no enumeration to keep current, no new binary
      to discover and add.
    * The ``PATH``-first ``git`` shim (``_GIT_STASH_SHIM``) is now
      **advisory only** — a friendly early-refusal message for the common
      case where a dispatched child resolves ``git`` via ``PATH``, exactly
      like any other tool invocation. It is NOT the control: it execs the
      REAL git unconditionally for every non-``stash`` invocation, with no
      staged copy and no exec-path deny, because closing off which binary
      may run is no longer what stops a stash. If this shim were bypassed
      entirely (an absolute-path invocation, a binary this host adds
      tomorrow), the sandbox-exec deny above still stops the write.

    ``HarnessSandbox.git_stash_denied`` is set from
    ``probe_stash_effect_denied_across_git_binaries``, which exercises this
    deny against every real git invocation this host happens to offer
    (``PATH``-resolved ``git``, ``/usr/bin/git`` by absolute path, ``xcrun
    git``, and Xcode's bundled binary by its own absolute path — see
    ``discover_probe_git_invocations``) — building empirical confidence
    across several real binaries, but the deny's CORRECTNESS never depended
    on that list being exhaustive, unlike round 3's mechanism.

    **Residual, stated rather than hidden**: once ``refs/stash`` has been
    folded into the shared ``packed-refs`` file, this profile cannot deny
    only that entry without denying ordinary reads and writes of every packed
    ref. A dispatched child can therefore read an already-packed stash or
    hand-edit ``packed-refs`` to redirect it. The runner snapshots both the
    resolved ``refs/stash`` value and its underlying packed entry immediately
    before dispatch, then verifies both again immediately afterward before
    trusting or posting a verdict. That detects a net mutation, including one
    hidden beneath an unchanged loose ref, and fails the run closed; it does
    not prevent a transient mutation that the child restores before exit.
    This is a real, explicit limitation rather than a claim of exhaustive
    containment.

  The credential-read, credential-binding, user-config-write, and stash-ref
  denies live in
  the ONE sandbox-exec profile ``build_sandbox_exec_profile`` writes, and
  each is ACTUALLY RUN against a throwaway fixture in
  ``HarnessSandbox.build()`` before any real dispatch: the credential/
  user-config denies are tested with a real ``cat``/``touch`` against a
  fixture file this script creates (never the operator's own files), and the
  stash-ref deny is tested with a real ``git stash push`` — via several real
  git invocations this host offers — inside a scratch repo this script
  ``git init``s for the probe (never the shared clone or its stash ref). The
  resulting booleans (``credential_read_denied``,
  ``credential_binding_protected``, ``user_config_write_denied``, and
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

  The child receives only a provider subscription capability: an isolated
  Codex auth link or a dedicated Claude OAuth environment value. GitHub and
  Neotoma publication authority remain parent-only. Cursor is refused because
  no equivalently narrow subscription capability is currently wired.

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
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
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
import harness_router  # noqa: E402
from harness_router import headroom_resolution  # noqa: E402
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
TRUSTED_MACOS_SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")

# These are the two network-only delivery denials emitted by skill_runner.
# They are intentionally copied as exact values rather than matched loosely:
# the lens runner may recover a local verdict only when the child process
# itself exited cleanly and the sole reported failure was GitHub/git-remote
# delivery. Index, object-store, ref, stash, and arbitrary failures remain
# unrecoverable even when a plausible verdict file happens to exist.
_RECOVERABLE_LOCAL_VERDICT_DELIVERY_DENIALS = frozenset(
    {
        "sandbox denied network access — the child could not push or reach the GitHub API",
        "the child could not reach the git remote — nothing was pushed",
    }
)
_POST_VERDICT_ATTEMPTS = 3

# A lens verdict is a few KiB of markdown. The cap bounds what an untrusted
# child can make the unsandboxed parent read into memory and print.
MAX_VERDICT_BYTES = 256 * 1024


# ── Reading the child-writable verdict path ------------------------------------


@dataclass(frozen=True)
class LocalVerdictRead:
    """Outcome of reading the local verdict file.

    ``text is None and not refusal`` means the file is absent (the caller may
    fall back to the child's stdout). ``refusal`` non-empty means something
    was there and was NOT a trustworthy verdict file; the run must refuse.
    """

    text: str | None
    refusal: str = ""


def read_local_verdict(
    path: Path, *, max_bytes: int = MAX_VERDICT_BYTES
) -> LocalVerdictRead:
    """Read the verdict file the sandboxed child was allowed to write.

    The verdict path is the one path the sandbox profile lets the child write,
    and this parent runs UNSANDBOXED, so a plain ``read_text`` is a confused
    deputy: a child that plants a symlink there makes the parent read a file
    the child itself is denied. Open with ``O_NOFOLLOW`` (a symlink, dangling
    or not, is refused), then judge the OPEN descriptor (no path re-resolution
    between check and read): it must be a regular file, owned by this user,
    with a single link (a hard link to a protected file is refused), and no
    larger than ``max_bytes``. ``O_NONBLOCK`` keeps a planted FIFO from
    blocking the open. Content must be strict UTF-8.
    """
    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return LocalVerdictRead(None)
    except OSError as exc:
        return LocalVerdictRead(
            None,
            f"local verdict path {path} could not be opened safely "
            f"({type(exc).__name__}: {exc.strerror or exc}); a symlink or "
            "special file at that path is refused",
        )
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return LocalVerdictRead(
                None, f"local verdict path {path} is not a regular file"
            )
        if info.st_nlink != 1:
            return LocalVerdictRead(
                None,
                f"local verdict path {path} has {info.st_nlink} hard links; "
                "expected exactly one",
            )
        if info.st_uid != os.geteuid():
            return LocalVerdictRead(
                None, f"local verdict path {path} is not owned by this user"
            )
        if info.st_size > max_bytes:
            return LocalVerdictRead(
                None,
                f"local verdict file is {info.st_size} bytes, over the "
                f"{max_bytes}-byte cap",
            )
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    except OSError as exc:
        return LocalVerdictRead(
            None, f"local verdict file could not be read: {exc}"
        )
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        return LocalVerdictRead(
            None, f"local verdict file exceeds the {max_bytes}-byte cap"
        )
    try:
        return LocalVerdictRead(data.decode("utf-8"))
    except UnicodeDecodeError:
        return LocalVerdictRead(
            None, "local verdict file is not valid UTF-8; refusing to parse it"
        )


# ── Headroom -------------------------------------------------------------------


class HeadroomExhausted(RuntimeError):
    """Raised when the requested provider's configured headroom is 0."""


def _headroom_refusal(provider: str, value: float, source: str) -> str:
    """Refusal text that names the headroom source that WON, and its remedy.

    ``harness_router.headroom_resolution`` resolves precedence (manual or dated
    override, then the live usage snapshot, then an undated override, then
    1.0). A ``0.0`` can come from any of them, and only the undated override is
    fixed by editing the headroom file, so the message must say which one it
    was rather than always sending the operator back to the file.
    """
    head = f"provider {provider!r} has headroom={value:g} — refusing to dispatch."
    override_file = harness_router._headroom_path()
    usage_file = harness_router._usage_path()
    origin = harness_router.headroom_override_origin()
    where = (
        f"the override file {override_file}"
        if origin != "env"
        else "the APIS_HARNESS_HEADROOM variable (no valid "
        f"override file at {override_file})"
    )
    if source == harness_router.HEADROOM_SOURCE_LIVE_USAGE:
        return (
            f"{head} Source: the LIVE USAGE SNAPSHOT ({usage_file}), which "
            "reports this provider exhausted. A hand-set value in "
            f"{where} cannot override a live 0.0, so editing it will not "
            "restore this provider. Wait for the provider's reported reset "
            "(the snapshot refreshes on the next usage observation), then "
            "re-run. To see the snapshot's opinion, run: python3 -c "
            "\"import sys; sys.path.insert(0, 'execution/daemons/apis'); "
            f"import harness_router as h; print(h.live_headroom('{provider}'))\""
            ". See docs/runbooks/harness-headroom-restore.md, 'Refusal "
            "survives the edit'."
        )
    if source == harness_router.HEADROOM_SOURCE_MANUAL_OVERRIDE:
        return (
            f"{head} Source: a MANUAL override object "
            '(cooldown_reason "manual") for this provider in '
            f"{where}. It stays in force until that entry is removed or "
            "replaced; a bare number is not enough while the object is "
            "present. Operator-owned; this script never edits it."
        )
    if source == harness_router.HEADROOM_SOURCE_DATED_OVERRIDE:
        return (
            f"{head} Source: an override object with a FUTURE cooldown_until "
            f"in {where}. It stops blocking at that time on its own; to "
            "restore earlier, the operator removes or replaces that entry. "
            "This script never edits it."
        )
    return (
        f"{head} Source: {where}. This is the operator-reset gate: headroom "
        "is restored by editing that source to a non-zero value "
        "(operator- or session-owned, see the PR body's 'Headroom' section), "
        "never by this script. If the value there is already non-zero and "
        "this still refuses, another source won: see "
        "docs/runbooks/harness-headroom-restore.md, 'Refusal survives the "
        "edit'."
    )


def check_headroom(provider: str) -> float:
    """Return the provider's configured headroom, raising if it is exactly 0.

    Uses ``harness_router.headroom_resolution`` — the SAME precedence the router
    applies to dispatch (override object, live usage snapshot, undated
    override, default) — so this function adds no second source of truth. It
    only turns "0.0" into a loud, named refusal that says WHICH source won,
    instead of a routing failure discovered three steps later. A provider
    sitting above 0 but below ``APIS_HARNESS_MIN_HEADROOM`` is left to the
    router's own eligibility check (``harness_router.provider_candidates``),
    which already refuses it with a specific reason; this function only
    special-cases the exact-zero operator-reset signal named in the task.
    """
    value, source = headroom_resolution().get(
        provider, (1.0, harness_router.HEADROOM_SOURCE_DEFAULT)
    )
    if value <= 0.0:
        raise HeadroomExhausted(_headroom_refusal(provider, value, source))
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


def final_child_instruction(verdict_path: Path) -> str:
    """Return the final, authoritative boundary for the review child.

    This text is appended after the canonical agent definition. That ordering
    matters: lens definitions carry the general GitHub publication contract,
    while this runner deliberately splits inference from publication. The
    child produces one local artifact; the trusted parent owns every external
    and identity-sensitive gate.
    """
    return textwrap.dedent(
        f"""\
        AUTHORITATIVE FINAL CHILD INSTRUCTION
        This review child is read-only except for its one local verdict artifact.
        It must not call GitHub and must not attempt publication; do not use gh,
        push, comment, review, or invoke any other external delivery path.
        It must only write the verdict to {verdict_path} using the exact strict format
        from the brief, then print that same verdict to stdout and stop.
        The trusted parent exclusively owns local-verdict parsing, live-head verification,
        expected-identity verification, the explicit --post gate, and GitHub publication.
        This final instruction overrides any publication instruction in the appended
        canonical agent definition above.
        """
    )


# ── Throwaway worktree -----------------------------------------------------------


@contextlib.contextmanager
def _shared_fetch_lock(repo_path: Path):
    """Serialize ref-updating fetches for one shared repository.

    Parallel lens runs use separate throwaway worktrees, but prepare them from
    the same clone. Git fetch transactions can race when two processes update
    the same remote-tracking ref from different expected old values. Key the
    lock by the resolved common git directory so paths sharing refs share it.

    The lock file intentionally remains in the system temp directory:
    unlinking it while another process waits on its inode can admit a third
    process through a newly-created inode.
    """
    result = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    )
    common_dir = Path(result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = repo_path / common_dir
    lock_key = hashlib.sha256(str(common_dir.resolve()).encode("utf-8")).hexdigest()
    lock_path = Path(tempfile.gettempdir()) / f"ateles-harness-fetch-{lock_key}.lock"
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _fetch_origin_serialized(repo_path: Path) -> None:
    """Fetch origin while holding the repository's shared-ref update lock."""
    with _shared_fetch_lock(repo_path):
        subprocess.run(
            ["git", "-C", str(repo_path), "fetch", "-q", "origin"],
            check=True,
        )


@dataclass
class Worktree:
    repo_name: str
    path: Path
    _created: bool = field(default=False, repr=False)

    def create(self, *, head: str) -> None:
        repo_path = Path.home() / "repos" / self.repo_name
        _fetch_origin_serialized(repo_path)
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
        subprocess.run(["git", "clean", "-fdxq"], cwd=str(self.path), check=False)
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
    # Seatbelt's regex dialect does not honor the PCRE non-capturing-group
    # spelling ``(?:...)``; keep these as separate, simple expressions.
    r"/\.config/gh/hosts\.yml$",
    r"/\.config/gh/state\.yml$",
    r"/\.git-credentials$",
    r"/\.config/git/credentials$",
    r"/\.ssh/.*$",
    r"/\.claude\.json$",
    r"/\.netrc$",
    r"/\.config/sops/age/.*",
)

_SYNTHETIC_PUBLICATION_CREDENTIAL_PATHS: tuple[str, ...] = (
    ".config/neotoma/.env",
    ".config/gh/hosts.yml",
    ".git-credentials",
    ".config/git/credentials",
    ".ssh/id_ed25519",
)

_CREDENTIAL_HELPER_EXEC_PATHS: tuple[Path, ...] = (
    Path("/usr/bin/security"),
    Path("/usr/libexec/git-core/git-credential-osxkeychain"),
    Path(
        "/Applications/Xcode.app/Contents/Developer/usr/libexec/"
        "git-core/git-credential-osxkeychain"
    ),
    Path("/opt/homebrew/bin/git-credential-osxkeychain"),
    Path("/usr/local/bin/git-credential-osxkeychain"),
)

# Harmless system executables used to prove that Seatbelt's process-exec
# literal matcher discriminates one real executable from another.  A shell
# script is not a valid probe here: Seatbelt sees its resolved interpreter,
# not the script pathname.  ``false`` is expected to fail on its own, so the
# probe additionally requires sandbox-exec's explicit denial diagnostic.
_CREDENTIAL_HELPER_PROBE_DENIED = Path("/usr/bin/false")
_CREDENTIAL_HELPER_PROBE_CONTROL = Path("/usr/bin/true")

_KEYCHAIN_MACH_SERVICES: tuple[str, ...] = (
    "com.apple.securityd",
    "com.apple.securityd.xpc",
    "com.apple.securityd.general.xpc",
)

# User-level harness config directories a dispatched child must never be able
# to WRITE to (writing a new hook/rule/mcp.json there would let a codex/cursor
# child silently alter what future sessions in ANY harness trust).
_USER_CONFIG_WRITE_DENY_REGEXES: tuple[str, ...] = (
    r"/\.claude(/.*)?$",
    r"/\.cursor(/.*)?$",
    r"/\.codex(/.*)?$",
)


def trusted_sandbox_exec_path() -> str | None:
    """Return the pinned macOS Seatbelt launcher, never a PATH resolution.

    The runner's probes and dispatched child must use one platform identity.
    Resolving by name would let a PATH-preferred look-alike validate itself and
    would make the parent probe and child launch sensitive to different PATHs.
    """
    path = TRUSTED_MACOS_SANDBOX_EXEC
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        return None
    return str(path)


def _credential_binding_write_deny_regexes(
    home_roots: tuple[Path, ...],
) -> tuple[str, ...]:
    """Protect credential bindings and only their security-relevant ancestors.

    Reads are denied by the credential globs above. These rooted write denies
    prevent moving/replacing those files or the narrow config directories that
    bind their names, without denying ordinary writes elsewhere in a worktree.
    """
    patterns: list[str] = []
    for home_root in home_roots:
        # Seatbelt matches canonical vnode paths. macOS TemporaryDirectory
        # returns /var/... while the kernel reports /private/var/...; resolve
        # before embedding a rooted deny or the live probe cannot bind.
        root = re.escape(str(home_root.resolve()))
        patterns.extend(
            (
                rf"{root}/\.config$",
                rf"{root}/\.config/neotoma(/.*)?$",
                rf"{root}/\.config/gh(/.*)?$",
                rf"{root}/\.config/git(/.*)?$",
                rf"{root}/\.config/sops$",
                rf"{root}/\.config/sops/age(/.*)?$",
                rf"{root}/\.neotoma(/.*)?$",
                rf"{root}/\.ssh(/.*)?$",
                rf"{root}/\.git-credentials$",
                rf"{root}/\.claude\.json$",
                rf"{root}/\.netrc$",
            )
        )
    return tuple(patterns)


# The git-stash EFFECT, denied at both read and write (PR #1308 round 4,
# ent_9e88db1882c668e6c5c32be9's own round-4 follow-up): matches the stash
# ref and its reflog in a plain repo's `.git`, AND the worktree-common-dir
# equivalent under `.git/worktrees/<name>/`. Deliberately binary-agnostic —
# rounds 1-3 tried enforcing this by refusing/enumerating specific git
# BINARIES (a PATH shim, then a process-exec deny per discovered path); round
# 4's own review found a THIRD real git binary neither round enumerated
# (Xcode's bundled copy, reachable via `xcrun -f git`, never on PATH) that
# still landed a stash. Denying the EFFECT — any write to `refs/stash` or its
# reflog, from ANY process — closes this regardless of which or how many git
# binaries exist on the host, because every one of them writes through the
# same ref path to record a stash. See DiscussionResidualStashRisk below and
# the module docstring's GUARD BINDING section for what this does NOT cover
# (a stash ref already folded into `packed-refs`).
_STASH_REF_DENY_REGEXES: tuple[str, ...] = (
    r"/\.git/refs/stash$",
    r"/\.git/logs/refs/stash$",
    r"/\.git/worktrees/[^/]+/refs/stash$",
    r"/\.git/worktrees/[^/]+/logs/refs/stash$",
)

_GIT_STASH_SHIM = """#!/bin/bash
# PATH-first git shim (ent_89a4d44b063cb0902106da49) — ADVISORY ONLY, per PR
# #1308 round 4 review (ent_... round-4 task): this shim is a friendly early
# refusal for the common case (a dispatched child resolves `git` via PATH,
# same as any ordinary tool invocation), NOT the enforcement mechanism. The
# actual control is the sandbox-exec profile's deny on refs/stash and its
# reflog (see build_sandbox_exec_profile and the module docstring's GUARD
# BINDING section) — a deny on the EFFECT (writing/reading the stash ref
# itself), which holds regardless of which git BINARY is used to attempt it.
# Round 3 tried enumerating and denying every real git binary's exec path;
# round 4's own review found a THIRD real git binary (Xcode's bundled copy,
# reachable via `xcrun -f git` or its own absolute path, never on PATH) that
# enumeration missed and that landed a real stash despite git_stash_denied
# reporting True. Enumerating binaries loses by construction — there is
# always another copy (a future Xcode/Homebrew update, an MDM-managed git, a
# statically linked git someone builds) — so this shim no longer tries; it
# execs the REAL git unconditionally for every non-stash invocation (no
# process-exec deny, no staged copy — removed as unneeded complexity now
# that exec is not what is being denied), and the sandbox profile is what
# actually stops a stash regardless of which binary attempts it.
for arg in "$@"; do
  case "$arg" in
    stash)
      echo "git-stash-shim: 'git stash' is refused for a dispatched harness child (ent_89a4d44b063cb0902106da49) — this message is a courtesy; the actual enforcement is the sandbox-exec deny on refs/stash, which applies even if this shim is bypassed" >&2
      exit 1
      ;;
  esac
done
exec "REAL_GIT_PLACEHOLDER" "$@"
"""


def _real_git_path() -> str:
    real = shutil.which("git")
    if not real:
        raise RuntimeError(
            "git not found on PATH — cannot build the stash-refusing shim"
        )
    return real


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


def build_sandbox_exec_profile(
    profile_path: Path,
    *,
    credential_home_roots: tuple[Path, ...] | None = None,
    credential_extra_roots: tuple[Path, ...] = (),
    runtime_write_root: Path | None = None,
    verdict_path: Path | None = None,
    credential_helper_exec_paths: tuple[Path, ...] = _CREDENTIAL_HELPER_EXEC_PATHS,
) -> None:
    """Write a macOS sandbox-exec profile denying:

    * reads of the credential-file globs (``_CREDENTIAL_READ_DENY_REGEXES``),
    * writes that could replace those credential bindings or their narrow
      security-relevant config ancestors,
    * writes to the user-level harness config directories
      (``_USER_CONFIG_WRITE_DENY_REGEXES``),
    * reads AND writes of the git-stash ref and its reflog
      (``_STASH_REF_DENY_REGEXES`` — ent_9e88db1882c668e6c5c32be9, PR #1308
      round 4),

    while leaving reads and non-filesystem operations available to the
    provider runtime. Filesystem writes are denied globally and reopened only
    beneath the isolated runtime root plus the exact verdict pathname and the
    literal ``/dev/null`` device required by Git and subprocess plumbing. No
    ``/dev`` subpath is allowed.

    THE STASH DENY IS BY EFFECT, NOT BY BINARY (round 4's own fix, replacing
    round 3's ``process-exec`` enumeration). Round 3 denied ``process-exec``
    of every git binary a PATH scan could find; round 4's own review found a
    THIRD real git binary (Xcode's bundled copy, reachable via ``xcrun -f
    git`` or its own absolute path, never on PATH) that enumeration missed,
    and a stash through it landed while the mechanism reported itself fully
    bound. Enumerating binaries loses by construction: there is always
    another copy an enumeration didn't anticipate. Denying WRITES (and
    READS) to the stash ref path itself is binary-agnostic — every git
    binary, present or future, records a stash by writing the SAME ref path
    (``refs/stash``) and reflog (``logs/refs/stash``), so a deny on that path
    stops ``stash push``/``pop``/``apply``/``drop``/``clear`` regardless of
    which binary attempts it, with no enumeration to keep current.

    Denying READS too (not just writes) additionally stops ``stash
    apply``/``list``/``show`` from consuming an existing stash entry — a
    dispatched child that finds an ALREADY-STASHED entry left by another
    session could otherwise read (and thereby exfiltrate or reason about)
    that entry's diff even without writing anything itself. Verified
    empirically: a sandboxed ``stash list`` against a fixture repo with a
    real pre-existing entry returns exit 0 with EMPTY output rather than an
    error — Seatbelt's deny makes git behave as if the ref does not exist,
    which is functionally the desired outcome (the entry is not visible) even
    though it is not a nonzero exit code; the probe below checks for this
    exact "denied reads as absent" shape rather than assuming an error code.

    RESIDUAL, NOT CLOSED BY THIS MECHANISM, BOTH READ AND WRITE (stated here,
    not hidden): ``packed-refs`` is the one file that determines
    ``refs/stash``'s resolution that this profile does NOT deny, in either
    direction.

    * **Read**: once ``refs/stash`` is folded into ``packed-refs`` (by ``git
      pack-refs --all`` or ``gc`` — never by ``stash push`` itself, verified
      empirically: a plain ``stash push`` writes only the LOOSE ref, even
      against an already-packed repo, where it creates a new loose ref that
      shadows the packed one — which is why this deny still stops every NEW
      push regardless of whether the repo's stash ref happens to already be
      packed), an entry already resident there is readable, because this
      profile does not deny reads of ``packed-refs`` itself.
    * **Write**: a sandboxed process can also WRITE ``packed-refs`` freely (a
      plain shell append/rewrite succeeds), and a well-formed rewrite
      replacing the existing
      ``refs/stash`` line's sha with an attacker-chosen sha already present
      in the repository's object store makes ``git rev-parse refs/stash``
      resolve to that content afterward — i.e. a dispatched child can PLANT
      or REDIRECT a resolvable stash entry via a direct file rewrite, never
      calling ``git stash`` at all, and without touching any of the four
      denied paths.

    WHY THIS IS DELIBERATELY OUT OF SCOPE FOR THIS CONTROL, NOT AN OVERSIGHT:
    denying ``file-write*`` on
    ``packed-refs`` would break ordinary git operations this runner's own
    dispatched work legitimately needs — ``branch -d``, ``fetch --prune``,
    and ``pack-refs``/``gc`` themselves all rewrite ``packed-refs`` as part
    of NORMAL git use, not as an attack. ``packed-refs`` is shared
    repository-wide state that git itself rewrites during routine operation;
    a deny broad enough to stop a hand-crafted rewrite would also stop every
    legitimate one, for every ref in the repository, not just
    ``refs/stash``. This control's threat model is an agent USING the
    stash/git machinery as a normal caller would — the resolution-path gaps
    rounds 3 and 4 closed were exactly that shape (a normal `git stash`
    invocation reaching a normal git binary through an unguarded path). A
    hand-crafted rewrite of a ref-storage file is a DIFFERENT threat model —
    a deliberately adversarial agent constructing shared Git metadata rather
    than calling git. Verdict-only write confinement closes ordinary checkout
    writes, but the common Git directory remains outside that checkout; this
    stash-specific residual is detected by the comparison below rather than
    claimed as prevented.

    WHAT DETECTS IT INSTEAD: ``verify_stash_ref_unchanged_after_dispatch``
    (called by ``run_one`` after the dispatched child exits, before the
    verdict is trusted) reads both the resolved ``refs/stash`` value via the
    real, unmodified system git and the raw ``refs/stash`` packed entry
    immediately before dispatch and again immediately after. It fails the run
    LOUDLY — refusing to post any verdict — if either changes, including a
    packed mutation hidden beneath an unchanged loose ref. This does not
    prevent a forge; it turns "silent" into "detected and the run refused,"
    which is the property this residual can actually be given without
    breaking the runner's own legitimate git use.
    """
    extra_read_denies = tuple(
        rf"{re.escape(str(path.resolve()))}(/.*)?$" for path in credential_extra_roots
    ) + (
        rf"{re.escape(str(Path(tempfile.gettempdir()).resolve()))}/.*apis_mcp_[^/]*\.json$",
    )
    read_denies = "\n".join(
        f'  (regex #"{p}")'
        for p in (
            *_CREDENTIAL_READ_DENY_REGEXES,
            *extra_read_denies,
            *_STASH_REF_DENY_REGEXES,
        )
    )
    binding_write_denies = (
        _credential_binding_write_deny_regexes(credential_home_roots or (Path.home(),))
        + tuple(
            rf"{re.escape(str(path.resolve()))}(/.*)?$"
            for path in credential_extra_roots
        )
        + (
            rf"{re.escape(str(Path(tempfile.gettempdir()).resolve()))}/.*apis_mcp_[^/]*\.json$",
        )
    )
    write_denies = "\n".join(
        f'  (regex #"{p}")'
        for p in (
            *_USER_CONFIG_WRITE_DENY_REGEXES,
            *_STASH_REF_DENY_REGEXES,
            *binding_write_denies,
        )
    )
    helper_denies = "\n".join(
        f'  (literal "{str(path.resolve()).replace(chr(34), chr(92) + chr(34))}")'
        for path in credential_helper_exec_paths
    )
    keychain_service_denies = "\n".join(
        f'  (global-name "{service}")' for service in _KEYCHAIN_MACH_SERVICES
    )
    runtime_write_root = runtime_write_root or profile_path.parent
    verdict_path = verdict_path or (profile_path.parent / "verdict.md")
    runtime_root = str(runtime_write_root.resolve()).replace('"', '\\"')
    verdict = str(verdict_path.resolve()).replace('"', '\\"')
    profile = (
        "(version 1)\n"
        "(allow default)\n"
        "(deny file-write*)\n"
        "(allow file-write*\n"
        '  (literal "/dev/null")\n'
        f'  (literal "{runtime_root}")\n'
        f'  (subpath "{runtime_root}")\n'
        f'  (literal "{verdict}")\n'
        ")\n"
        "(deny file-read*\n"
        f"{read_denies}\n"
        ")\n"
        "(deny file-write*\n"
        f"{write_denies}\n"
        ")\n"
        "(deny process-exec\n"
        f"{helper_denies}\n"
        ")\n"
        "(deny mach-lookup\n"
        f"{keychain_service_denies}\n"
        ")\n"
    )
    profile_path.write_text(profile, encoding="utf-8")


def probe_credential_helper_isolation(
    profile_path: Path,
    *,
    denied_helper: Path,
    control_helper: Path,
) -> bool:
    """Prove the profile blocks a synthetic credential helper executable.

    The inputs are harmless real system executables; this never invokes
    ``security``, a credential helper, or a keychain.  They must be real
    executables rather than shell scripts: Seatbelt matches ``process-exec``
    against the resolved executable (``/bin/sh`` for a script), not the script
    pathname.  The control proves the profile did not simply make all process
    execution fail.  The denied probe uses ``/usr/bin/false``, so success
    requires sandbox-exec's explicit denial diagnostic rather than merely a
    nonzero status.
    """
    sandbox_exec = trusted_sandbox_exec_path()
    if sandbox_exec is None:
        return False
    denied = subprocess.run(
        [sandbox_exec, "-f", str(profile_path), str(denied_helper)],
        capture_output=True,
        text=True,
    )
    if denied.returncode == 0 or "Operation not permitted" not in denied.stderr:
        return False
    control = subprocess.run(
        [sandbox_exec, "-f", str(profile_path), str(control_helper)],
        capture_output=True,
        text=True,
    )
    return control.returncode == 0


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
    sandbox_exec = trusted_sandbox_exec_path()
    if sandbox_exec is None:
        return False
    denied = subprocess.run(
        [sandbox_exec, "-f", str(profile_path), "cat", str(fixture_path)],
        capture_output=True,
        text=True,
    )
    if denied.returncode == 0:
        return False
    control = subprocess.run(
        [sandbox_exec, "-f", str(profile_path), "cat", str(control_path)],
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
    sandbox_exec = trusted_sandbox_exec_path()
    if sandbox_exec is None:
        return False
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    denied = subprocess.run(
        [sandbox_exec, "-f", str(profile_path), "touch", str(fixture_path)],
        capture_output=True,
        text=True,
    )
    if denied.returncode == 0 or fixture_path.exists():
        return False
    control_path.parent.mkdir(parents=True, exist_ok=True)
    control = subprocess.run(
        [sandbox_exec, "-f", str(profile_path), "touch", str(control_path)],
        capture_output=True,
        text=True,
    )
    return control.returncode == 0 and control_path.exists()


def probe_review_write_confinement(
    profile_path: Path,
    *,
    runtime_root: Path,
    verdict_path: Path,
    review_worktree: Path,
) -> bool:
    """Exercise the exact local-review write boundary with disposable files.

    The parent creates every negative fixture.  The sandboxed process must be
    able to open literal ``/dev/null`` read-write, write the one verdict and
    its isolated runtime root, while writes inside the review checkout, a
    sibling checkout-shaped directory, and an ordinary outside directory all
    fail without changing their sentinels.
    """
    sandbox_exec = trusted_sandbox_exec_path()
    if sandbox_exec is None:
        return False
    fixture_root = runtime_root.parent / f"{runtime_root.name}-write-boundary"
    sibling = fixture_root / "sibling-checkout" / "sentinel"
    outside = fixture_root / "ordinary-outside" / "sentinel"
    tracked = review_worktree / ".ateles-review-write-probe"
    runtime = runtime_root / "write-probe" / "allowed"
    paths = (sibling, outside, tracked)
    try:
        null_control = subprocess.run(
            [
                sandbox_exec,
                "-f",
                str(profile_path),
                "/usr/bin/python3",
                "-c",
                "with open('/dev/null', 'r+b', buffering=0) as sink: sink.write(b'x')",
            ],
            capture_output=True,
            text=True,
        )
        if null_control.returncode != 0:
            return False
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("unchanged\n", encoding="utf-8")
        verdict_path.parent.mkdir(parents=True, exist_ok=True)
        command = [sandbox_exec, "-f", str(profile_path), "/bin/sh", "-c"]
        allowed = []
        for path in (verdict_path, runtime):
            attempt = subprocess.run(
                [
                    *command,
                    'mkdir -p "$(dirname "$1")" && printf allowed > "$1"',
                    "probe",
                    str(path),
                ],
                capture_output=True,
                text=True,
            )
            allowed.append(attempt.returncode == 0 and path.read_text() == "allowed")
        denied = []
        for path in paths:
            attempt = subprocess.run(
                [*command, 'printf changed > "$1"', "probe", str(path)],
                capture_output=True,
                text=True,
            )
            denied.append(
                attempt.returncode != 0
                and path.read_text(encoding="utf-8") == "unchanged\n"
            )
        return all(allowed) and all(denied)
    except (OSError, UnicodeError):
        return False
    finally:
        verdict_path.unlink(missing_ok=True)
        tracked.unlink(missing_ok=True)
        shutil.rmtree(fixture_root, ignore_errors=True)


def probe_sandbox_exec_preserves_credential_binding(
    profile_path: Path,
    *,
    protected_dir: Path,
    protected_file: Path,
    control_root: Path,
) -> bool:
    """Prove a protected credential name cannot be rebound by mutation.

    The negative fixture covers the protected directory and its relevant
    ancestor. The positive control performs the same ordinary filesystem
    operation outside that boundary, so a broken profile that rejects all
    mutations cannot be mistaken for a working, discriminating guard.
    """
    sandbox_exec = trusted_sandbox_exec_path()
    if sandbox_exec is None:
        return False
    protected_ancestor = protected_dir.parent
    try:
        relative_file = protected_file.relative_to(protected_ancestor)
    except ValueError:
        return False
    control_root.mkdir(parents=True, exist_ok=True)
    denied_target = control_root / "denied-binding-target"
    command = '/bin/mv "$1" "$2" && /bin/cat "$2/$3"'
    denied = subprocess.run(
        [
            sandbox_exec,
            "-f",
            str(profile_path),
            "/bin/sh",
            "-c",
            command,
            "binding-probe",
            str(protected_ancestor),
            str(denied_target),
            str(relative_file),
        ],
        capture_output=True,
        text=True,
    )
    original = protected_file.read_text(encoding="utf-8")
    denied_cleanly = (
        denied.returncode != 0
        and protected_ancestor.is_dir()
        and protected_file.is_file()
        and not denied_target.exists()
    )
    if not denied_cleanly:
        return False

    control_source = control_root / "control-source"
    control_relative_file = Path("ordinary.txt")
    control_file = control_source / control_relative_file
    control_file.parent.mkdir(parents=True, exist_ok=True)
    control_file.write_text(original, encoding="utf-8")
    control_target = control_root / "control-target"
    control = subprocess.run(
        [
            sandbox_exec,
            "-f",
            str(profile_path),
            "/bin/sh",
            "-c",
            command,
            "binding-control",
            str(control_source),
            str(control_target),
            str(control_relative_file),
        ],
        capture_output=True,
        text=True,
    )
    return (
        control.returncode == 0
        and control.stdout == original
        and not control_source.exists()
        and (control_target / control_relative_file).is_file()
    )


@dataclass(frozen=True)
class StashListRead:
    """One attempt to observe the real stash stack."""

    succeeded: bool
    listing: str | None


def _real_stash_list(scratch_git_dir: Path) -> StashListRead:
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
        capture_output=True,
        text=True,
    )
    if listing.returncode != 0:
        return StashListRead(succeeded=False, listing=None)
    return StashListRead(succeeded=True, listing=listing.stdout)


def probe_git_shim_denies_stash_push(shim_path: Path, scratch_git_dir: Path) -> bool:
    """Return True iff invoking `git stash push` THROUGH THE SHIM (PATH-first,
    the normal resolution path) in a SCRATCH git repo (never the shared
    clone) exits non-zero AND leaves the real stash stack untouched.

    ADVISORY-ONLY as of PR #1308 round 4 (ent_9e88db1882c668e6c5c32be9's own
    round-4 follow-up): this probe verifies the shim's friendly early-refusal
    message still works for the common PATH-resolved case, but
    `HarnessSandbox.git_stash_denied` no longer depends on this probe's
    result — the actual control is
    `probe_stash_effect_denied_across_git_binaries`, which tests the
    sandbox-exec deny on the stash ref itself rather than the shim. Kept as
    a standalone, separately testable diagnostic for the shim's own
    behaviour rather than removed outright, since the shim itself is kept
    (as advisory UX, not enforcement) rather than removed.

    *scratch_git_dir* must already contain at least one commit — otherwise a
    real, unshimmed `git stash push` would ALSO fail (with "You do not have
    the initial commit yet"), which would make this probe pass for the wrong
    reason on a broken shim that never actually refuses anything.
    """
    env = {**os.environ, "PATH": f"{shim_path.parent}:{os.environ.get('PATH', '')}"}
    before = _real_stash_list(scratch_git_dir)
    if not before.succeeded:
        return False
    push = subprocess.run(
        ["git", "-C", str(scratch_git_dir), "stash", "push"],
        capture_output=True,
        text=True,
        env=env,
    )
    if push.returncode == 0:
        return False
    after = _real_stash_list(scratch_git_dir)
    return after.succeeded and after.listing == before.listing


def discover_probe_git_invocations(scratch_git_dir: Path) -> list[list[str]]:
    """Return several DISTINCT ways to invoke `git stash push` against
    *scratch_git_dir*, covering every git binary this host happens to have —
    used ONLY to build confidence in the probe below across several real
    binaries, never as the deny mechanism itself (round 3 tried enumerating
    binaries as the mechanism; round 4 replaces that with a deny on the
    EFFECT, which does not need enumeration to be correct — see
    build_sandbox_exec_profile). Distinct invocations tried, whichever exist
    on this host: the `git` resolved via PATH, `/usr/bin/git` by absolute
    path, `xcrun git` (Xcode's own resolution mechanism, which the previous
    round's PATH-only enumeration missed entirely), and Xcode's bundled git
    binary by its own absolute path (`xcrun -f git`'s answer). Each is
    returned as an argv PREFIX (before `-C <repo> stash push`); a binary this
    host does not have is simply omitted, never fabricated.
    """
    invocations: list[list[str]] = []
    on_path = shutil.which("git")
    if on_path:
        invocations.append(["git"])
    if Path("/usr/bin/git").is_file():
        invocations.append(["/usr/bin/git"])
    if shutil.which("xcrun"):
        xcrun_result = subprocess.run(
            ["xcrun", "-f", "git"], capture_output=True, text=True
        )
        if xcrun_result.returncode == 0 and xcrun_result.stdout.strip():
            xcode_git = xcrun_result.stdout.strip()
            invocations.append(["xcrun", "git"])
            if xcode_git not in (on_path, "/usr/bin/git"):
                invocations.append([xcode_git])
    return invocations


def probe_stash_effect_denied_across_git_binaries(
    command_wrapper: list[str], scratch_git_dir: Path
) -> bool:
    """Return True iff `git stash push` is refused AND leaves the real stash
    stack untouched, for EVERY git invocation `discover_probe_git_invocations`
    finds on this host — proving the deny is binary-agnostic rather than
    merely re-testing one binary, without the probe ITSELF depending on
    enumeration being exhaustive (unlike round 3's mechanism, this round's
    deny targets the ref path, so any binary this probe happens to miss would
    still be denied in production — this function only builds empirical
    confidence across as many real binaries as this host happens to offer).

    An empty *command_wrapper* (no sandbox-exec available) always returns
    False. If NO git invocation is discovered at all, also returns False —
    that would mean the probe proved nothing, which must not be misread as a
    pass.
    """
    if not command_wrapper:
        return False
    invocations = discover_probe_git_invocations(scratch_git_dir)
    if not invocations:
        return False
    before = _real_stash_list(scratch_git_dir)
    if not before.succeeded:
        return False
    for prefix in invocations:
        attempt = subprocess.run(
            [*command_wrapper, *prefix, "-C", str(scratch_git_dir), "stash", "push"],
            capture_output=True,
            text=True,
        )
        # Seatbelt's read/write deny on refs/stash makes the WRITE attempt
        # fail outright (nonzero exit) in every case observed — but the
        # verification is the real stash list, not the exit code, per this
        # module's own standing practice of never trusting a subject's own
        # report of its result.
        if attempt.returncode == 0:
            return False
        after = _real_stash_list(scratch_git_dir)
        if not after.succeeded or after.listing != before.listing:
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
    credential_binding_protected: bool
    user_config_write_denied: bool
    git_stash_denied: bool
    authentication_ready: bool
    unavailable_guards: tuple[str, ...]
    review_write_confined: bool = False

    @property
    def fully_guarded(self) -> bool:
        """True only when every guard this class attempts actually probed as
        bound. `refuse_if_guard_required` is the only reader that matters."""
        return (
            self.credential_read_denied
            and self.credential_binding_protected
            and self.user_config_write_denied
            and self.review_write_confined
            and self.git_stash_denied
            and len(self.command_wrapper) == 3
            and self.command_wrapper[0] == str(TRUSTED_MACOS_SANDBOX_EXEC)
            and self.command_wrapper[1] == "-f"
            and Path(self.command_wrapper[2]).is_absolute()
        )

    @property
    def codex_outer_sandbox_probed(self) -> bool:
        """Whether the exact trusted wrapper/profile pair passed all probes."""
        return self.provider == "codex" and self.fully_guarded

    @property
    def ready_to_dispatch(self) -> bool:
        """Both the safety controls and provider authentication are present."""
        return self.fully_guarded and self.authentication_ready

    @classmethod
    def build(
        cls,
        provider: str,
        tmp_root: Path,
        *,
        review_worktree: Path | None = None,
        verdict_path: Path | None = None,
    ) -> "HarnessSandbox":
        sandbox_home = tmp_root / f"{provider}-home"
        sandbox_home.mkdir(parents=True, exist_ok=True)
        review_worktree = review_worktree or (tmp_root / "probe-review-worktree")
        review_worktree.mkdir(parents=True, exist_ok=True)
        verdict_path = verdict_path or (review_worktree / "verdict.md")

        if provider == "codex":
            env_extra = {
                "CODEX_HOME": str(sandbox_home),
                "ATELES_LOCAL_REVIEW_HOME": str(sandbox_home),
                "TMPDIR": str(sandbox_home / "tmp"),
                "TMP": str(sandbox_home / "tmp"),
                "TEMP": str(sandbox_home / "tmp"),
            }
            authentication_ready = link_codex_auth(sandbox_home)
        elif provider == "cursor":
            env_extra = {
                "HOME": str(sandbox_home),
                "ATELES_LOCAL_REVIEW_HOME": str(sandbox_home),
                "TMPDIR": str(sandbox_home / "tmp"),
                "TMP": str(sandbox_home / "tmp"),
                "TEMP": str(sandbox_home / "tmp"),
            }
            # Cursor subscription state is stored outside the isolated home
            # and has no narrowly exposable capability equivalent to Codex's
            # auth.json or Claude's dedicated OAuth environment variable.
            authentication_ready = False
        elif provider == "claude":
            env_extra = {
                "ATELES_LOCAL_REVIEW_HOME": str(sandbox_home),
                "TMPDIR": str(sandbox_home / "tmp"),
                "TMP": str(sandbox_home / "tmp"),
                "TEMP": str(sandbox_home / "tmp"),
            }
            authentication_ready = bool(
                (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()
            )
        else:
            raise ValueError(f"unsupported harness provider: {provider}")

        # ── sandbox-exec profile: build (credential/user-config/stash-ref
        # denies, all by EFFECT — see build_sandbox_exec_profile), then PROBE
        # against fixtures.
        profile_path = sandbox_home / "profile.sb"
        credential_read_denied = False
        credential_binding_protected = False
        user_config_write_denied = False
        credential_helper_isolated = False
        review_write_confined = False
        command_wrapper: list[str] = []
        try:
            fixture_home = sandbox_home / "probe-fixtures" / "fixture-user"
            denied_helper = _CREDENTIAL_HELPER_PROBE_DENIED
            control_helper = _CREDENTIAL_HELPER_PROBE_CONTROL
            if not denied_helper.is_file() or not control_helper.is_file():
                raise OSError("credential-helper probe executables unavailable")
            (sandbox_home / "tmp").mkdir(parents=True, exist_ok=True)
            configured_keys = os.environ.get("ATELES_PRIVATE_KEYS_DIR", "").strip()
            key_roots = [REPO_ROOT.parent / "ateles-private" / "keys"]
            if configured_keys:
                key_roots.append(Path(configured_keys))
            fixture_keys = sandbox_home / "probe-fixtures" / "aauth-keys"
            key_roots.append(fixture_keys)
            build_sandbox_exec_profile(
                profile_path,
                credential_home_roots=(Path.home(), fixture_home),
                credential_extra_roots=tuple(key_roots),
                runtime_write_root=sandbox_home,
                verdict_path=verdict_path,
                credential_helper_exec_paths=(
                    *_CREDENTIAL_HELPER_EXEC_PATHS,
                    denied_helper,
                ),
            )
            read_control = sandbox_home / "probe-fixtures" / "control-read.txt"
            read_control.parent.mkdir(parents=True, exist_ok=True)
            read_control.write_text("control\n", encoding="utf-8")
            read_fixtures = tuple(
                fixture_home / relative
                for relative in _SYNTHETIC_PUBLICATION_CREDENTIAL_PATHS
            ) + (fixture_keys / "probe.jwk.json",)
            mcp_fd, mcp_fixture_raw = tempfile.mkstemp(
                prefix="apis_mcp_probe_", suffix=".json"
            )
            os.close(mcp_fd)
            mcp_fixture = Path(mcp_fixture_raw)
            read_fixtures = (*read_fixtures, mcp_fixture)
            for read_fixture in read_fixtures:
                read_fixture.parent.mkdir(parents=True, exist_ok=True)
                read_fixture.write_text(
                    "PROBE_NOT_A_REAL_CREDENTIAL=1\n", encoding="utf-8"
                )
            credential_read_denied = all(
                probe_sandbox_exec_denies_read(
                    profile_path, read_fixture, control_path=read_control
                )
                for read_fixture in read_fixtures
            )
            read_fixture_dir = fixture_home / ".config" / "neotoma"
            read_fixture = read_fixture_dir / ".env"
            credential_binding_protected = (
                probe_sandbox_exec_preserves_credential_binding(
                    profile_path,
                    protected_dir=read_fixture_dir,
                    protected_file=read_fixture,
                    control_root=sandbox_home / "probe-fixtures" / "binding-control",
                )
                and probe_sandbox_exec_denies_write(
                    profile_path,
                    fixture_keys / "replacement.jwk.json",
                    control_path=sandbox_home / "probe-fixtures" / "key-control",
                )
                and probe_sandbox_exec_denies_write(
                    profile_path,
                    mcp_fixture.with_name(f"apis_mcp_write_{mcp_fixture.name}"),
                    control_path=sandbox_home / "probe-fixtures" / "mcp-control",
                )
            )
            credential_helper_isolated = probe_credential_helper_isolation(
                profile_path,
                denied_helper=denied_helper,
                control_helper=control_helper,
            )

            write_fixture = sandbox_home / "probe-fixtures" / ".claude" / "probe-write"
            write_control = (
                sandbox_home / "probe-fixtures" / "control-write-dir" / "probe"
            )
            user_config_write_denied = probe_sandbox_exec_denies_write(
                profile_path, write_fixture, control_path=write_control
            )
            review_write_confined = probe_review_write_confinement(
                profile_path,
                runtime_root=sandbox_home,
                verdict_path=verdict_path,
                review_worktree=review_worktree,
            )
            sandbox_exec = trusted_sandbox_exec_path()
            if sandbox_exec is not None:
                command_wrapper = [sandbox_exec, "-f", str(profile_path)]
        except OSError:
            credential_read_denied = False
            credential_binding_protected = False
            user_config_write_denied = False
            credential_helper_isolated = False
            review_write_confined = False
        finally:
            if "mcp_fixture" in locals():
                mcp_fixture.unlink(missing_ok=True)

        # ── git-stash: the shim is advisory-only (a friendly early message
        # for the common PATH-resolved case); the REAL control is the
        # sandbox-exec profile's deny on refs/stash, already built above.
        # This probe exercises the sandbox effect across every real git
        # invocation this host happens to offer (see
        # discover_probe_git_invocations) to build confidence the deny is
        # genuinely binary-agnostic — it does NOT need to be exhaustive for
        # production correctness, unlike round 3's enumeration-as-mechanism,
        # because production denies the ref path, not a list of binaries.
        shim_dir = sandbox_home / "shim-bin"
        shim_dir.mkdir(parents=True, exist_ok=True)
        shim_path = shim_dir / "git"
        git_stash_denied = False
        try:
            real_git = _real_git_path()
            shim_path.write_text(
                _GIT_STASH_SHIM.replace("REAL_GIT_PLACEHOLDER", real_git),
                encoding="utf-8",
            )
            shim_path.chmod(0o755)
            scratch_repo = sandbox_home / "stash-probe-repo"
            scratch_repo.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "init", "-q", str(scratch_repo)],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(scratch_repo),
                    "config",
                    "user.email",
                    "probe@example.com",
                ],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(scratch_repo), "config", "user.name", "probe"],
                check=True,
                capture_output=True,
            )
            (scratch_repo / "probe.txt").write_text("probe\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(scratch_repo), "add", "-A"],
                check=True,
                capture_output=True,
            )
            # A REAL commit — required so a real, unsandboxed `git stash
            # push` would actually succeed, which is what makes the probe
            # below meaningful: on a broken guard, this commit is what lets
            # the bypass produce a real stash entry for the probe to catch,
            # rather than failing on its own with "no initial commit" for an
            # unrelated reason. See probe_stash_effect_denied_across_git_
            # binaries's docstring.
            subprocess.run(
                ["git", "-C", str(scratch_repo), "commit", "-q", "-m", "probe"],
                check=True,
                capture_output=True,
            )
            (scratch_repo / "probe.txt").write_text("probe changed\n", encoding="utf-8")
            git_stash_denied = probe_stash_effect_denied_across_git_binaries(
                command_wrapper, scratch_repo
            )
        except (RuntimeError, subprocess.CalledProcessError, OSError):
            git_stash_denied = False
        env_extra["PATH"] = f"{shim_dir}:{os.environ.get('PATH', '')}"

        unavailable = []
        if not git_stash_denied:
            unavailable.append(
                "git_stash_guard (sandbox-exec unavailable, or its "
                "refs/stash deny did not probe as denying a real stash "
                "push across the git binaries this host offers — see the "
                "sandbox's own probe result, not asserted)"
            )
        if not credential_read_denied:
            unavailable.append(
                "credential_read_guard (sandbox-exec unavailable, or its "
                "profile did not probe as denying a read of the fixture path "
                "— see the sandbox's own probe result, not asserted)"
            )
        if not credential_helper_isolated:
            unavailable.append(
                "credential_helper_guard (the outer profile did not probe as "
                "denying a synthetic credential helper while allowing an "
                "ordinary control executable; no real helper or keychain was "
                "invoked)"
            )
        if not credential_binding_protected:
            unavailable.append(
                "credential_binding_guard (the profile did not probe as "
                "preventing mutation of a protected credential binding and "
                "its relevant config ancestor while allowing the same "
                "ordinary operation outside that boundary)"
            )
        if not user_config_write_denied:
            unavailable.append(
                "user_config_write_guard (sandbox-exec unavailable, or its "
                "profile did not probe as denying a write to the fixture "
                "path)"
            )
        if not review_write_confined:
            unavailable.append(
                "review_write_guard (the outer profile did not prove that only "
                "the exact verdict and isolated runtime scratch are writable)"
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
            credential_read_denied=(
                credential_read_denied and credential_helper_isolated
            ),
            credential_binding_protected=credential_binding_protected,
            user_config_write_denied=user_config_write_denied,
            review_write_confined=review_write_confined,
            git_stash_denied=git_stash_denied,
            authentication_ready=authentication_ready,
            unavailable_guards=tuple(unavailable),
        )


def refuse_if_guard_required(sandbox: "HarnessSandbox") -> str | None:
    """Return a refusal reason when *sandbox*'s OWN probed state shows a
    required guard is unbound, or None to proceed.

    Driven entirely by ``sandbox.fully_guarded`` — a boolean produced by
    actually running the sandbox-exec profile and the git shim against
    synthetic fixtures in ``HarnessSandbox.build()``, never by a
    caller-supplied constant. Every provider, including Claude, needs the
    same outer credential boundary: project hooks govern tool calls but do
    not prove that direct absolute-path/helper/keychain access is contained.
    There is no lighter-weight lens run that can accept a partial bind.
    """
    if not sandbox.authentication_ready:
        return (
            f"provider {sandbox.provider!r} has no authentication available "
            "inside its isolated harness home — refusing before a model call. "
            "Codex requires its isolated auth.json capability; Claude requires "
            "CLAUDE_CODE_OAUTH_TOKEN; Cursor requires a named subscription "
            "capability that is not yet wired."
        )
    if not sandbox.fully_guarded:
        missing = "; ".join(sandbox.unavailable_guards) or "unspecified"
        return (
            f"provider {sandbox.provider!r} failed to probe as fully guarded "
            f"(credential_read_denied={sandbox.credential_read_denied}, "
            "credential_binding_protected="
            f"{sandbox.credential_binding_protected}, "
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
        sandbox_mode = (
            "danger-full-access"
            if sandbox.codex_outer_sandbox_probed
            else "workspace-write"
        )
        example_cmd = [
            "codex",
            "exec",
            "--sandbox",
            sandbox_mode,
            *(
                []
                if sandbox_mode == "danger-full-access"
                else ["--add-dir", str(worktree_path)]
            ),
            "--ephemeral",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--cd",
            str(worktree_path),
            "-",
        ]
    elif provider == "cursor":
        example_cmd = [
            "cursor-agent",
            "--print",
            "--force",
            "--trust",
            "--approve-mcps",
            "--output-format",
            "text",
            "--workspace",
            str(worktree_path),
            "<prompt omitted — see prompt_chars>",
        ]
    else:
        example_cmd = ["claude", "--print", "--append-system-prompt", "<system prompt>"]

    # command_wrapper is prepended to the REAL argv by skill_runner
    # (see _run_skill_once's command_wrapper param) — shown here too so the
    # dry run's printed command matches what actually runs, not a description
    # of it that could drift.
    example_cmd = [*sandbox.command_wrapper, *example_cmd]

    refusal = refuse_if_guard_required(sandbox)

    report = {
        "ok": refusal is None,
        "dry_run": True,
        "provider": provider,
        "repo": target.repo,
        "pr": target.pr,
        "head": target.head,
        "lens": target.lens,
        "agent": target.agent,
        "worktree": str(worktree_path),
        "sandbox_env_extra": {
            k: v for k, v in sandbox.env_extra.items() if k != "PATH"
        },
        "sandbox_path_prefix": sandbox.env_extra.get("PATH", "").split(os.pathsep)[0]
        if "PATH" in sandbox.env_extra
        else "",
        "command_wrapper": list(sandbox.command_wrapper),
        "fully_guarded": sandbox.fully_guarded,
        "credential_read_denied": sandbox.credential_read_denied,
        "credential_binding_protected": sandbox.credential_binding_protected,
        "user_config_write_denied": sandbox.user_config_write_denied,
        "git_stash_denied": sandbox.git_stash_denied,
        "authentication_ready": sandbox.authentication_ready,
        "unavailable_guards": list(sandbox.unavailable_guards),
        "example_command": example_cmd,
        "prompt_chars": len(task_text),
        "no_model_call_made": True,
        "would_refuse": refusal,
    }
    if refusal:
        report["reason"] = refusal
    return report


# ── Posting gate -----------------------------------------------------------------


_REVIEW_MARKER_RE = re.compile(
    r"^<!-- review:(?P<lens>[a-z0-9_-]+) commit=(?P<head>[0-9a-f]{40}) -->$"
)


_ECHO_LINE_CAP = 300


@dataclass
class VerdictCheck:
    """Result of validating a captured verdict file before any post."""

    ok: bool
    reason: str
    lens_verdict: str | None = None
    sign_off_warranted: bool | None = None
    pre_post: dict = field(default_factory=dict)
    artifact_binding: dict = field(default_factory=dict)


def validate_verdict(
    verdict_text: str,
    *,
    lens_agent: str,
    expected_lens: str | None = None,
    expected_head: str | None = None,
) -> VerdictCheck:
    """Run the SAME reader a Claude bootstrap lens run is instructed to run
    by hand (the lens brief's PRE-POST CHECK step) — swarm_dispatch's own
    ``lens_own_verdict`` / ``sign_off_is_warranted`` — so a codex/cursor
    verdict is held to the identical bar a Claude verdict is, not a
    weaker parallel check this script invents.
    """
    lines = verdict_text.splitlines()
    marker = _REVIEW_MARKER_RE.fullmatch(lines[0]) if lines else None

    def _echo(index: int) -> str:
        """Echo a leading line back into the report only when it is safe.

        The verdict file is child-written, untrusted text, and this report is
        printed to the invoking session. Nothing is echoed unless line 1 is a
        strict review marker (a fixed-shape line that cannot carry arbitrary
        content); lines 2 and 3 are then echoed capped, so the operator can
        still see why the reader rejected a well-formed-looking verdict.
        """
        line = lines[index] if len(lines) > index else ""
        if marker is None and line:
            return f"<withheld: {len(line)} chars, verdict marker not valid>"
        return line[:_ECHO_LINE_CAP]

    pre_post = {
        "line1": _echo(0),
        "line2": _echo(1),
        "line3": _echo(2),
        "blocking_count": verdict_text.count("[BLOCKING]"),
    }
    observed = {
        "marker": pre_post["line1"],
        "lens": marker.group("lens") if marker else None,
        "head": marker.group("head") if marker else None,
    }
    marker_count = sum(1 for line in lines if _REVIEW_MARKER_RE.fullmatch(line))
    expected = {"lens": expected_lens, "head": expected_head}
    artifact_binding = {"expected": expected, "observed": observed}
    if marker is None:
        return VerdictCheck(
            ok=False,
            reason=(
                "strict verdict marker missing or malformed — "
                f"expected={expected!r} observed={observed!r}"
            ),
            pre_post=pre_post,
            artifact_binding=artifact_binding,
        )
    if marker_count != 1:
        observed["marker_count"] = marker_count
        return VerdictCheck(
            ok=False,
            reason=(
                "verdict artifact must contain exactly one strict review marker — "
                f"expected={expected!r} observed={observed!r}"
            ),
            pre_post=pre_post,
            artifact_binding=artifact_binding,
        )
    if expected_lens is not None and observed["lens"] != expected_lens:
        return VerdictCheck(
            ok=False,
            reason=(
                "verdict marker lens mismatch — "
                f"expected={expected!r} observed={observed!r}"
            ),
            pre_post=pre_post,
            artifact_binding=artifact_binding,
        )
    if expected_head is not None and observed["head"] != expected_head:
        return VerdictCheck(
            ok=False,
            reason=(
                "verdict marker commit mismatch — "
                f"expected={expected!r} observed={observed!r}"
            ),
            pre_post=pre_post,
            artifact_binding=artifact_binding,
        )
    if swarm_dispatch is None:
        return VerdictCheck(
            ok=False,
            reason="swarm_dispatch could not be imported — cannot validate a "
            "verdict without the real reader; refusing to post rather than "
            "invent a weaker check",
            pre_post=pre_post,
            artifact_binding=artifact_binding,
        )
    verdict = swarm_dispatch.lens_own_verdict(verdict_text, lens_agent=lens_agent)
    warranted = swarm_dispatch.sign_off_is_warranted(
        verdict_text, lens_agent=lens_agent
    )
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
            artifact_binding=artifact_binding,
        )
    return VerdictCheck(
        ok=True,
        reason="verdict readable at fixed position",
        lens_verdict=verdict,
        sign_off_warranted=warranted,
        pre_post=pre_post,
        artifact_binding=artifact_binding,
    )


def _dispatch_diagnostics(result: SkillResult) -> dict:
    """Preserve the child's original diagnostics and structured classifier."""
    return {
        "returncode": result.returncode,
        "error": result.error,
        "stderr": result.stderr,
        "delivery_failure_reason": result.delivery_failure_reason,
        "delivery_failure_reasons": list(result.delivery_failure_reasons),
        "delivery_failure_conflicts": list(result.delivery_failure_conflicts),
    }


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
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return json.loads(result.stdout or "{}").get("headRefOid", "")
    except json.JSONDecodeError:
        return ""


def read_stash_ref_oid(worktree: Path) -> str | None:
    """Resolve refs/stash through loose or packed storage using real git.

    ``None`` is the single expected missing-ref state. Any other failure is
    unsafe to interpret as absence: a malformed or unreadable packed-refs file
    must fail the dispatch closed instead of making a changed ref look empty.
    """
    git = _real_git_path()
    existence = subprocess.run(
        [git, "-C", str(worktree), "show-ref", "--exists", "refs/stash"],
        capture_output=True,
        text=True,
        check=False,
    )
    if existence.returncode == 2 and not existence.stdout.strip():
        return None
    if existence.returncode != 0:
        detail = (existence.stderr or "").strip() or (
            f"git exit {existence.returncode}"
        )
        raise RuntimeError(f"cannot test refs/stash existence: {detail}")

    result = subprocess.run(
        [
            git,
            "-C",
            str(worktree),
            "show-ref",
            "--verify",
            "--hash",
            "refs/stash",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout.strip()
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or f"git exit {result.returncode}"
        raise RuntimeError(f"cannot resolve refs/stash: {detail}")
    if (
        "\n" in output
        or len(output) not in (40, 64)
        or any(char not in "0123456789abcdef" for char in output.lower())
    ):
        raise RuntimeError(f"cannot resolve refs/stash: invalid object id {output!r}")
    return output.lower()


def read_packed_stash_ref_oid(worktree: Path) -> str | None:
    """Read the refs/stash entry stored in packed-refs, even if shadowed."""
    git_path = subprocess.run(
        [
            _real_git_path(),
            "-C",
            str(worktree),
            "rev-parse",
            "--git-path",
            "packed-refs",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if git_path.returncode != 0 or not git_path.stdout.strip():
        detail = (git_path.stderr or "").strip() or f"git exit {git_path.returncode}"
        raise RuntimeError(f"cannot locate packed-refs: {detail}")
    packed_refs = Path(git_path.stdout.strip())
    if not packed_refs.is_absolute():
        packed_refs = worktree / packed_refs
    if not packed_refs.exists():
        return None

    matches: list[str] = []
    try:
        lines = packed_refs.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"cannot read packed-refs: {exc}") from exc
    for line in lines:
        if not line or line.startswith(("#", "^")):
            continue
        oid, separator, ref_name = line.partition(" ")
        if separator and ref_name == "refs/stash":
            if len(oid) not in (40, 64) or any(
                char not in "0123456789abcdef" for char in oid.lower()
            ):
                raise RuntimeError(f"invalid packed refs/stash object id {oid!r}")
            matches.append(oid.lower())
    if len(matches) > 1:
        raise RuntimeError("packed-refs contains duplicate refs/stash entries")
    return matches[0] if matches else None


@dataclass(frozen=True)
class StashRefState:
    """Resolved stash value plus its underlying packed entry, if any."""

    resolved_oid: str | None
    packed_oid: str | None


def capture_stash_ref_state(worktree: Path) -> StashRefState:
    """Capture both observable and shadowed refs/stash storage state."""
    return StashRefState(
        resolved_oid=read_stash_ref_oid(worktree),
        packed_oid=read_packed_stash_ref_oid(worktree),
    )


def verify_stash_ref_unchanged_after_dispatch(
    worktree: Path, before: StashRefState
) -> str | None:
    """Return a fail-closed reason when dispatch changed refs/stash.

    Resolving the ref through git deliberately covers both loose refs and the
    residual packed-refs path. This is a postcondition, not prevention: it
    detects the net effect before any child verdict can be trusted or posted.
    """
    try:
        after = capture_stash_ref_state(worktree)
    except RuntimeError as exc:
        return (
            "refs/stash could not be verified after the dispatched run — "
            f"refusing to trust its verdict: {exc}"
        )
    if after == before:
        return None
    changes: list[str] = []
    if after.resolved_oid != before.resolved_oid:
        changes.append(
            "resolved refs/stash changed "
            f"({before.resolved_oid or '<absent>'} -> "
            f"{after.resolved_oid or '<absent>'})"
        )
    if after.packed_oid != before.packed_oid:
        changes.append(
            "packed refs/stash changed "
            f"({before.packed_oid or '<absent>'} -> "
            f"{after.packed_oid or '<absent>'})"
        )
    return (
        "refs/stash changed during the dispatched run: "
        f"{'; '.join(changes)} — refusing to "
        "trust or post the verdict"
    )


def _verdict_identity(verdict_text: str) -> tuple[str, str]:
    """Return the strict lens and attribution header for a verdict body."""
    lines = verdict_text.splitlines()
    marker = _REVIEW_MARKER_RE.fullmatch(lines[0]) if lines else None
    if len(lines) < 2 or marker is None:
        raise ValueError("verdict lacks a strict exact-head review marker")
    attribution = lines[1]
    if not (
        attribution.startswith("**\U0001f916 ")
        and " — Ateles swarm, " in attribution
        and attribution.endswith("**")
    ):
        raise ValueError("verdict lacks the canonical lens attribution header")
    return marker.group("lens"), attribution


def _pr_comments(*, repo: str, pr: int) -> list[dict]:
    """Read every PR issue comment, failing closed when the list is unreadable."""
    result = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repo}/issues/{pr}/comments?per_page=100",
            "--paginate",
            "--slurp",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    pages = json.loads(result.stdout or "[]")
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ValueError("GitHub comment listing had an unexpected shape")
    return [comment for page in pages for comment in page if isinstance(comment, dict)]


def _matching_verdict_comments(
    *, comments: list[dict], lens: str, attribution: str
) -> list[dict]:
    """Find this machine account's comments for one lens identity."""
    matches = []
    for comment in comments:
        if ((comment.get("user") or {}).get("login") or "") != "ateles-agent":
            continue
        lines = str(comment.get("body") or "").splitlines()
        marker = _REVIEW_MARKER_RE.fullmatch(lines[0]) if lines else None
        if (
            len(lines) >= 2
            and marker is not None
            and marker.group("lens") == lens
            and lines[1] == attribution
        ):
            matches.append(comment)
    return matches


def _one_verdict_comment(
    *, repo: str, pr: int, lens: str, attribution: str
) -> dict | None:
    matches = _matching_verdict_comments(
        comments=_pr_comments(repo=repo, pr=pr),
        lens=lens,
        attribution=attribution,
    )
    if len(matches) > 1:
        ids = ", ".join(str(comment.get("id") or "<missing>") for comment in matches)
        raise RuntimeError(
            f"ambiguous prior review:{lens} comments by ateles-agent ({ids}); "
            "refusing to choose one to edit"
        )
    return matches[0] if matches else None


def post_verdict(*, repo: str, pr: int, verdict_text: str) -> str:
    """Create or update the lens's one PR comment and return its URL.

    Never called unless ``validate_verdict(...).ok`` AND the caller passed
    ``--post``. Publication still validates the strict identity fields it needs
    to honour the canonical edit-not-duplicate contract.
    """
    lens, attribution = _verdict_identity(verdict_text)
    last_error: Exception | None = None
    for _attempt in range(1, _POST_VERDICT_ATTEMPTS + 1):
        existing = _one_verdict_comment(
            repo=repo,
            pr=pr,
            lens=lens,
            attribution=attribution,
        )
        if existing is None:
            command = [
                "gh",
                "api",
                "-X",
                "POST",
                f"repos/{repo}/issues/{pr}/comments",
                "--input",
                "-",
            ]
            run_kwargs: dict = {"input": json.dumps({"body": verdict_text})}
        else:
            comment_id = existing.get("id")
            if not isinstance(comment_id, int):
                raise RuntimeError(
                    f"prior review:{lens} comment has no integer id; refusing to edit"
                )
            command = [
                "gh",
                "api",
                "-X",
                "PATCH",
                f"repos/{repo}/issues/comments/{comment_id}",
                "--input",
                "-",
            ]
            run_kwargs = {"input": json.dumps({"body": verdict_text})}
        try:
            subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=True,
                **run_kwargs,
            )
        except subprocess.CalledProcessError as exc:
            last_error = exc
        landed = _one_verdict_comment(
            repo=repo,
            pr=pr,
            lens=lens,
            attribution=attribution,
        )
        if landed is not None and (landed.get("body") or "") == verdict_text:
            url = str(landed.get("html_url") or "")
            if not url:
                raise RuntimeError(f"review:{lens} write read back without an html_url")
            return url
        if last_error is None:
            last_error = RuntimeError(
                f"review:{lens} write did not read back with the submitted body"
            )
    if last_error is None:  # pragma: no cover - loop always attempts at least once
        raise AssertionError("posting retry loop made no attempt")
    raise last_error


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

    worktree_path = (
        scratch_root / f"{repo_worktree_name}-wt-{target.lens}-{target.pr}-{provider}"
    )
    worktree = Worktree(repo_name=repo_worktree_name, path=worktree_path)
    verdict_path = worktree.path / f"{target.lens}{target.pr}_verdict.md"

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
            sandbox = HarnessSandbox.build(
                provider,
                scratch_root,
                review_worktree=worktree.path,
                verdict_path=verdict_path,
            )
            agent_prompt = read_agent_prompt(worktree.path, target.agent)
            task_text = (
                render_lens_task(target, brief_path)
                + "\n\n---\n\n"
                + agent_prompt
                + "\n\n---\n\n"
                + final_child_instruction(verdict_path)
            )
            report = dry_run_report(
                target,
                provider=provider,
                sandbox=sandbox,
                task_text=task_text,
                worktree_path=worktree.path,
            )
        finally:
            worktree.remove()
        return report

    worktree.create(head=target.head)
    try:
        sandbox = HarnessSandbox.build(
            provider,
            scratch_root,
            review_worktree=worktree.path,
            verdict_path=verdict_path,
        )
        refusal = refuse_if_guard_required(sandbox)
        if refusal:
            return {"ok": False, "provider": provider, "reason": refusal}
        agent_prompt = read_agent_prompt(worktree.path, target.agent)
        task_text = (
            render_lens_task(target, brief_path)
            + "\n\n---\n\n"
            + agent_prompt
            + "\n\n---\n\n"
            + final_child_instruction(verdict_path)
        )

        try:
            stash_ref_before = capture_stash_ref_state(worktree.path)
        except RuntimeError as exc:
            reason = (
                "refs/stash baseline could not be established before dispatch — "
                f"refusing before a model call: {exc}"
            )
            return {
                "ok": False,
                "provider": provider,
                "reason": reason,
                "refusal_reason": reason,
                "posted": False,
            }

        try:
            result: SkillResult = await dispatch_role.dispatch(
                target.agent,
                task_text,
                provider=provider,
                cwd=str(worktree.path),
                timeout=timeout,
                task_entity_id=target.task_entity_id,
                env_extra=sandbox.env_extra,
                seated_reviewer=False,  # see module docstring: no MCP grant requested
                command_wrapper=sandbox.command_wrapper,
                codex_outer_sandboxed=sandbox.codex_outer_sandbox_probed,
                local_review=True,
            )
        except Exception as dispatch_error:
            stash_ref_failure = verify_stash_ref_unchanged_after_dispatch(
                worktree.path, stash_ref_before
            )
            if stash_ref_failure:
                raise RuntimeError(stash_ref_failure) from dispatch_error
            raise

        stash_ref_failure = verify_stash_ref_unchanged_after_dispatch(
            worktree.path, stash_ref_before
        )
        if stash_ref_failure:
            return {
                "ok": False,
                "provider": provider,
                "reason": stash_ref_failure,
                "refusal_reason": stash_ref_failure,
                "posted": False,
            }

        delivery_denial_recovered = False
        if not result.ok and not (
            result.returncode == 0
            and result.delivery_failure_reason
            in _RECOVERABLE_LOCAL_VERDICT_DELIVERY_DENIALS
            and result.error == result.delivery_failure_reason
            and result.delivery_failure_reasons == (result.delivery_failure_reason,)
            and not result.delivery_failure_conflicts
        ):
            return {
                "ok": False,
                "provider": provider,
                "reason": result.error or "dispatch failed",
                "attempted_providers": list(result.attempted_providers),
                "stderr": result.stderr,
                "dispatch_diagnostics": _dispatch_diagnostics(result),
                "posted": False,
            }

        # Read the child-writable verdict path ONCE, through the hardened
        # reader (no symlink follow, regular file we own, single link, size
        # cap, strict UTF-8). Anything else at that path is refused, on the
        # recovery path and the ordinary path alike.
        local_verdict = read_local_verdict(verdict_path)
        if local_verdict.refusal:
            reason = local_verdict.refusal
            return {
                "ok": False,
                "provider": provider,
                "reason": reason,
                "refusal_reason": reason,
                "attempted_providers": list(result.attempted_providers),
                "stderr": result.stderr,
                "dispatch_diagnostics": _dispatch_diagnostics(result),
                "posted": False,
            }

        if not result.ok:
            if local_verdict.text is None:
                reason = (
                    f"{result.error}; expected local verdict file was not produced "
                    f"at {verdict_path} — refusing network-delivery recovery"
                )
                return {
                    "ok": False,
                    "provider": provider,
                    "reason": reason,
                    "refusal_reason": reason,
                    "attempted_providers": list(result.attempted_providers),
                    "stderr": result.stderr,
                    "dispatch_diagnostics": _dispatch_diagnostics(result),
                    "posted": False,
                }
            delivery_denial_recovered = True

        verdict_text = (
            local_verdict.text if local_verdict.text is not None else result.stdout
        )
        check = validate_verdict(
            verdict_text,
            lens_agent=target.agent,
            expected_lens=target.lens,
            expected_head=target.head,
        )

        report = {
            "ok": check.ok,
            "provider": provider,
            "lens": target.lens,
            "agent": target.agent,
            "lens_verdict": check.lens_verdict,
            "sign_off_warranted": check.sign_off_warranted,
            "pre_post": check.pre_post,
            "artifact_binding": check.artifact_binding,
            # Unvalidated child output is never echoed: this report is printed
            # to the invoking session, and a verdict that fails validation is
            # untrusted text (it may carry anything the child could read).
            "verdict_text": verdict_text if check.ok else "",
            "verdict_text_withheld": not check.ok,
            "verdict_chars": len(verdict_text),
            "posted": False,
            "comment_url": "",
            "delivery_status": "not_attempted",
            "delivery_denial_recovered": delivery_denial_recovered,
            "dispatch_diagnostics": _dispatch_diagnostics(result),
        }
        if not check.ok:
            report["refusal_reason"] = check.reason
            return report

        # Re-check the head before posting — the brief's own step 1, applied
        # here rather than trusted from before dispatch (a long-running codex
        # attempt could span a force-push).
        try:
            current_head = current_pr_head(repo=target.repo, pr=target.pr)
        except Exception as exc:
            report["ok"] = False
            report["retryable"] = True
            report["error_kind"] = "head_verification_failed"
            report["refusal_reason"] = (
                "PR head verification failed before posting "
                f"({type(exc).__name__}: {exc}) — retry the run"
            )
            return report
        if not current_head:
            report["ok"] = False
            report["retryable"] = True
            report["error_kind"] = "head_verification_failed"
            report["refusal_reason"] = (
                "PR head verification returned no readable head before "
                "posting — retry the run"
            )
            return report
        if current_head != target.head:
            report["ok"] = False
            report["retryable"] = False
            report["error_kind"] = "head_mismatch"
            report["refusal_reason"] = (
                f"PR head moved from {target.head} to {current_head} during "
                "the run — refusing to post a verdict against a stale head"
            )
            return report

        if not post:
            report["delivery_status"] = "not_requested"
            report["refusal_reason"] = (
                "--post not set; verdict validated but not posted"
            )
            return report

        try:
            login = gh_login()
        except Exception as exc:
            report["ok"] = False
            report["retryable"] = True
            report["error_kind"] = "identity_verification_failed"
            report["refusal_reason"] = (
                "publication identity could not be verified after verdict "
                f"validation ({type(exc).__name__}: {exc})"
            )
            return report
        if login != "ateles-agent":
            report["ok"] = False
            report["refusal_reason"] = (
                f"gh api user -q .login returned {login!r}, expected "
                "'ateles-agent' — refusing to post under the wrong identity"
            )
            return report

        try:
            report["comment_url"] = post_verdict(
                repo=target.repo,
                pr=target.pr,
                verdict_text=verdict_text,
            )
        except Exception as exc:
            report["ok"] = False
            report["retryable"] = True
            report["error_kind"] = "publication_unconfirmed"
            report["delivery_status"] = "unconfirmed"
            report["refusal_reason"] = (
                "validated verdict publication could not be confirmed after "
                f"reconciliation ({type(exc).__name__}: {exc})"
            )
            return report
        report["posted"] = True
        report["delivery_status"] = "confirmed"
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
            "Path to the shared lens brief markdown file. Required, with no "
            "default: no canonical copy of the brief is checked in, and a guessed "
            "default would silently run a stale or wrong brief."
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
        help=(
            "Print the exact command and prompt size; make no model call. "
            "Exits 0 even when the preflight refuses: require \"ok\": true "
            "in the --json output, not the exit status."
        ),
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
                        target,
                        providers=providers,
                        dry_run=args.dry_run,
                        post=False,
                        repo_worktree_name=repo_worktree_name,
                        scratch_root=scratch_root,
                        brief_path=brief_path,
                        timeout=args.timeout,
                    )
                )
            else:
                report = asyncio.run(
                    run_one(
                        target,
                        provider=args.provider,
                        post=args.post,
                        dry_run=args.dry_run,
                        repo_worktree_name=repo_worktree_name,
                        scratch_root=scratch_root,
                        brief_path=brief_path,
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

    ok = (
        report.get("ok", False)
        if "compare" not in report
        else all(r.get("ok", False) for r in report["results"].values())
    )
    return 0 if ok or args.dry_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
