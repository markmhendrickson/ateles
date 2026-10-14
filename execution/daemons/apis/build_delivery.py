"""
execution/daemons/apis/build_delivery.py — what a Cicada dispatch that is meant to
deliver a PR needs, whichever path launched it.

WHY THIS EXISTS
---------------
Two independent launch paths start a Cicada build:

  * the swarm issue pipeline's auto-build handoff
    (``SwarmDispatcher._open_implementation_pr``), and
  * a Neotoma ``task`` entity routed to Cicada (``apis._spawn_harness_skill``).

Only the first one asked for the delivery grants. On Codex that is the whole
difference between a build that opens a PR and one that writes correct code and
cannot push it: ``--sandbox workspace-write`` denies the network, and a linked
worktree's gitdir is outside the sandbox unless the dispatch is given a worktree
(``cwd``) from which ``skill_runner`` derives the git roots (ateles#590). A
task-path build got no network, no ``cwd`` and so no git roots, ran for ~18
minutes, and failed with "sandbox denied network access" (canary
markmhendrickson/ateles#1448).

This module is the one place that decides and provisions the three things a
delivery-bearing Cicada dispatch needs, so no path can forget one:

  1. a dedicated linked worktree on a fresh branch off ``origin/main``;
  2. (derived by ``skill_runner`` from that worktree as ``cwd``) the two git
     roots the sandbox must be able to write;
  3. (derived by ``skill_runner`` from ``include_github_contract=True``) network
     for THIS dispatch only — never ``danger-full-access``.

It also keeps a small in-process registry of builds in flight. A task the build
files to track its own work carries the build's run marker and is recognised as
that build; any other task about the same issue is deferred until the build's
claim is released, then dispatched. And it refuses to provision from a clone that
is not the requested repository, before anything is fetched, granted or signed.

Nothing here widens a sandbox. A dispatch that does not deliver (a read-only or
local-edit task) gets none of this.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Iterator, Mapping

log = logging.getLogger("apis.build_delivery")

# Roles that implement an issue and open a PR for it. Reviewers and merge
# carriers (vanellus) share the `open_or_merge_pr` action class but do not
# author a build, so they are deliberately not listed.
BUILDER_SKILLS: frozenset[str] = frozenset({"cicada"})

# The action class whose whole purpose is delivery. Matches the vocabulary of
# `apis._AGENT_ACTION_TYPE` and the default execution policy: "open_or_merge_pr"
# is the high-blast class the gate holds for the operator. A task that declares
# a different class (`local_edit`, `compute_only_analysis`, ...) has not been
# cleared to deliver, so it is NOT given network or push ability.
DELIVERY_ACTION_TYPE = "open_or_merge_pr"

# Where each repository's clone lives, as "owner/name=/path" pairs separated by
# commas. Unlisted repositories fall back to ~/repos/<name>.
BUILD_CLONES_ENV = "ATELES_BUILD_CLONES"

_GIT_TIMEOUT_S = 120


def is_delivery_dispatch(skill: str | None, action_class: str | None) -> bool:
    """Whether this dispatch is a builder that is meant to deliver a PR."""
    return (
        (skill or "").strip().lower() in BUILDER_SKILLS
        and (action_class or "").strip().lower() == DELIVERY_ACTION_TYPE
    )


# ── Which issue does a task belong to? ───────────────────────────────────────
#
# The REPOSITORY is never decided here. The caller passes the one the dispatcher
# already resolved (`apis._dispatch_repo_from_snapshot`: the explicit `repo` /
# `dispatch_repo` / `github_repo` / `repository` field, first present). A
# reference in prose never supplies or overrides it: a worktree and a token follow
# the repository, so a sentence in a task body must not be able to steer either.

_REPO_ISSUE_RE = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)\b")
_ISSUE_URL_RE = re.compile(
    r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(?:issues|pull)/(\d+)"
)


def _as_issue_number(value: object) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def issue_key(
    snapshot: Mapping[str, object], repository: str | None
) -> tuple[str, int] | None:
    """The (repository, issue number) a task is about, or None when unknown.

    ``repository`` is the dispatcher's resolved repository; without one there is
    no key. The number is the structured `issue_number` field, else ONE
    unambiguous reference in the task's text that names THAT repository (a
    reference to another repository is not this issue; two different numbers are
    ambiguous).
    """
    repo = (repository or "").strip().lower()
    if not repo:
        return None
    number = _as_issue_number(snapshot.get("issue_number"))
    if number:
        return repo, number
    text = " ".join(
        str(snapshot.get(field) or "") for field in ("title", "description", "body")
    )
    refs = {
        int(m.group(2))
        for rx in (_REPO_ISSUE_RE, _ISSUE_URL_RE)
        for m in rx.finditer(text)
        if m.group(1).lower() == repo
    }
    return (repo, next(iter(refs))) if len(refs) == 1 else None


# ── In-flight builds ─────────────────────────────────────────────────────────
#
# A build registers the issue it is building for as long as it runs. Two things
# follow from that, and they are different:
#
#   * a task that is PROVABLY the build's own tracking task (it carries the
#     build's run marker, see `tracking_instructions`) is that build, and is
#     never dispatched;
#   * any OTHER task about the same issue is independent work. It is DEFERRED —
#     re-dispatched when the build's claim is released, however it ends — and
#     never dropped. `task.created` is delivered once per entity per process, so
#     a task that is only skipped is never looked at again.

RUN_MARKER_PREFIX = "build-run:"
_RUN_MARKER_RE = re.compile(r"\bbuild-run:\s*([0-9a-f]{16})\b")


@dataclass(frozen=True)
class BuildClaim:
    repository: str
    number: int
    holder: str
    run_id: str


Retry = Callable[[], Awaitable[None]]

_inflight: dict[tuple[str, int], BuildClaim] = {}
_runs: dict[str, tuple[str, int]] = {}
_deferred: dict[tuple[str, int], list[Retry]] = {}
_inflight_lock = threading.Lock()
_background: set[asyncio.Task] = set()


async def _drain(retries: list[Retry]) -> None:
    # One at a time: each retry may start a build, and the next one's guard must
    # see whatever that build claimed.
    for retry in retries:
        try:
            await retry()
        except Exception:  # noqa: BLE001 — one bad retry must not strand the rest
            log.exception("[apis] deferred task retry failed")


@contextlib.contextmanager
def claim_build(repository: str, number: int, holder: str) -> Iterator[BuildClaim]:
    """Record that a build of repository#number is running, for its duration.

    Released on EVERY exit path, success or failure; releasing is what re-runs
    the tasks deferred behind it. A second claim on a key already held keeps the
    FIRST holder as the owner, and only the owner's exit releases the key.
    """
    key = (repository.strip().lower(), int(number))
    claim = BuildClaim(key[0], key[1], holder, secrets.token_hex(8))
    with _inflight_lock:
        owned = key not in _inflight
        if owned:
            _inflight[key] = claim
        _runs[claim.run_id] = key
    try:
        yield claim
    finally:
        retries: list[Retry] = []
        with _inflight_lock:
            _runs.pop(claim.run_id, None)
            if owned:
                _inflight.pop(key, None)
                retries = _deferred.pop(key, [])
        if retries:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                log.error(
                    "[apis] build of %s#%s released outside an event loop — %d "
                    "deferred task(s) NOT retried",
                    key[0],
                    key[1],
                    len(retries),
                )
            else:
                task = loop.create_task(_drain(retries))
                _background.add(task)
                task.add_done_callback(_background.discard)


def active_build(repository: str, number: int) -> BuildClaim | None:
    """The in-flight build of repository#number, else None."""
    with _inflight_lock:
        return _inflight.get((repository.strip().lower(), int(number)))


def defer_until_released(key: tuple[str, int], retry: Retry) -> bool:
    """Queue ``retry`` to run when the build of ``key`` is released.

    Returns False when no build holds the key any more (it was released since the
    caller looked), in which case nothing was queued and the caller should
    proceed instead.
    """
    with _inflight_lock:
        if key not in _inflight:
            return False
        _deferred.setdefault(key, []).append(retry)
        return True


def tracking_instructions(claim: BuildClaim) -> str:
    """The prompt block that lets a build mark the task it files for itself."""
    return (
        "TRACKING TASK: if you file a Neotoma task to track THIS build, put the "
        f"line `{RUN_MARKER_PREFIX} {claim.run_id}` in its description, exactly "
        "once. Put it on no other task: a task carrying it is recognised as this "
        "run's own and is not dispatched again, and any other task about the "
        "issue is held until this run ends."
    )


def tracking_run_id(snapshot: Mapping[str, object]) -> str | None:
    """The run marker a task carries, if it is a registered build's own task."""
    ids = {
        m.group(1)
        for field in ("description", "body", "notes")
        for m in _RUN_MARKER_RE.finditer(str(snapshot.get(field) or ""))
    }
    if len(ids) != 1:
        return None
    run_id = next(iter(ids))
    with _inflight_lock:
        return run_id if run_id in _runs else None


# ── The worktree ─────────────────────────────────────────────────────────────


class BuildWorktreeError(RuntimeError):
    """A delivery worktree could not be provisioned."""


@dataclass(frozen=True)
class BuildWorktree:
    path: str
    branch: str
    base_sha: str
    clone: str
    base_fresh: bool  # False when `git fetch` failed and the local ref was used


_REPO_NAME_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.][A-Za-z0-9._-]{0,99}",
    re.ASCII,
)


def validate_repository(repository: object) -> str:
    """The repository, when it is a strict ``owner/name``; else BuildWorktreeError.

    One slash and nothing that could steer a path or a command: no whitespace,
    separators, ``..``, encodings or non-ASCII, and no ``.git`` suffix (the clone
    check compares names exactly). The check is exact rather than a clean-up:
    a name that needs normalising is refused, not rewritten.
    """
    if (
        not isinstance(repository, str)
        or not _REPO_NAME_RE.fullmatch(repository)
        or ".." in repository
        or repository.endswith(".git")
        or repository.rsplit("/", 1)[-1] in {".", ".."}
    ):
        raise BuildWorktreeError(
            f"{str(repository)[:80]!r} is not a strict owner/repo name"
        )
    return repository


def clone_for(repository: str) -> Path | None:
    """The local clone to branch worktrees from, or None when there is none.

    ``repository`` must already be validated (`validate_repository`), which is
    what makes ``~/repos/<name>`` safe to build.
    """
    wanted = repository.lower()
    for pair in os.environ.get(BUILD_CLONES_ENV, "").split(","):
        name, sep, raw = pair.partition("=")
        if sep and name.strip().lower() == wanted and raw.strip():
            candidate = Path(raw.strip()).expanduser()
            return candidate if (candidate / ".git").exists() else None
    candidate = Path("~/repos").expanduser() / repository.rsplit("/", 1)[-1]
    return candidate if (candidate / ".git").exists() else None


_SCP_REMOTE_RE = re.compile(
    r"(?:[^@/\s:]+@)?(?P<host>[^:/\s]+):(?P<owner>[^/\s]+)/(?P<name>[^/\s]+?)(?:\.git)?/?"
)
_URL_REMOTE_RE = re.compile(
    r"(?:https?|ssh|git)://(?:[^@/\s]+@)?(?P<host>[^:/\s]+)(?::\d+)?"
    r"/(?P<owner>[^/\s]+)/(?P<name>[^/\s]+?)(?:\.git)?/?",
    re.IGNORECASE,
)


def normalise_remote(url: str) -> str | None:
    """``host/owner/name``, lowercased, for an https, ssh or scp-style remote.

    None for anything else (a local path, an unparsable URL), which therefore
    never matches a repository.
    """
    url = url.strip()
    match = _URL_REMOTE_RE.fullmatch(url) or (
        None if "://" in url else _SCP_REMOTE_RE.fullmatch(url)
    )
    if not match:
        return None
    return "/".join(match.group(g) for g in ("host", "owner", "name")).lower()


async def verify_clone_identity(clone: Path, repository: str) -> None:
    """Refuse a clone whose origin is not exactly ``github.com/<repository>``.

    Reads the CONFIGURED remote URLs (fetch and push), not their `insteadOf`
    expansion, so it answers "which repository does this clone say it is". Every
    configured URL must match, and there must be at least one. Runs before any
    fetch, worktree, network grant or token: a clone of a different repository
    would otherwise receive that repository's delivery grants.
    """
    want = f"github.com/{repository}".lower()
    for key in ("remote.origin.url", "remote.origin.pushurl"):
        rc, out, _ = await _git(["config", "--get-all", key], clone)
        urls = [line for line in out.splitlines() if line.strip()]
        if key == "remote.origin.url" and (rc != 0 or not urls):
            raise BuildWorktreeError(f"{clone} has no origin remote")
        for url in urls:
            if normalise_remote(url) != want:
                raise BuildWorktreeError(
                    f"the clone at {clone} has an origin that is not "
                    f"{repository} — refusing to build {repository} from it"
                )


async def _git(args: list[str], cwd: Path, timeout: int = _GIT_TIMEOUT_S):
    """Run git; returns (rc, stdout, stderr). Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (
            proc.returncode or 0,
            out.decode(errors="replace"),
            err.decode(errors="replace"),
        )
    except Exception as exc:  # noqa: BLE001 — reported by the caller
        return 1, "", f"{type(exc).__name__}: {exc}"


def _slug(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return (slug or "build")[:40]


async def prepare_build_worktree(repository: str, label: str) -> BuildWorktree:
    """Create a linked worktree on a fresh branch off ``origin/main``.

    Never touches the clone's own working tree or checked-out branch: it fetches
    ``origin`` (remote-tracking refs and objects only) and adds a SEPARATE
    worktree. Raises BuildWorktreeError, with the reason, when it cannot — the
    caller decides what a missing worktree means for its dispatch.
    """
    validate_repository(repository)
    clone = clone_for(repository)
    if clone is None:
        raise BuildWorktreeError(
            f"no local clone of {repository} to branch a worktree from "
            f"(looked in ${BUILD_CLONES_ENV} and ~/repos/<name>)"
        )
    # Before the fetch, the worktree, or anything the caller grants afterwards.
    await verify_clone_identity(clone, repository)

    rc, _, err = await _git(["fetch", "origin", "main"], clone)
    fresh = rc == 0
    if not fresh:
        log.warning(
            "[apis] build worktree: fetch of %s failed (%s) — branching from the "
            "local origin/main, which may be stale",
            repository,
            err.strip()[:200],
        )

    rc, base_sha, err = await _git(["rev-parse", "--verify", "origin/main"], clone)
    if rc != 0:
        raise BuildWorktreeError(
            f"{clone} has no origin/main to branch from: {err.strip()[:200]}"
        )
    base_sha = base_sha.strip()

    stamp = f"{time.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"
    slug = _slug(label)
    branch = f"cicada/{slug}-{stamp}"
    parent = clone.parent / f"{clone.name}-worktrees"
    path = parent / f"cicada-{slug}-{stamp}"
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BuildWorktreeError(f"cannot create {parent}: {exc}") from exc

    rc, _, err = await _git(
        ["worktree", "add", "--no-track", "-b", branch, str(path), base_sha], clone
    )
    if rc != 0:
        raise BuildWorktreeError(f"git worktree add failed: {err.strip()[:300]}")

    log.info(
        "[apis] build worktree prepared: %s on %s (base %s%s)",
        path,
        branch,
        base_sha[:8],
        "" if fresh else ", STALE — fetch failed",
    )
    return BuildWorktree(str(path), branch, base_sha, str(clone), fresh)


def worktree_instructions(wt: BuildWorktree) -> str:
    """The prompt block that tells the builder it is already in its worktree."""
    stale = (
        ""
        if wt.base_fresh
        else " The daemon could not fetch just now, so `git fetch origin` first "
        "and rebase onto origin/main if it has moved."
    )
    return (
        f"YOUR WORKTREE: your working directory is already a dedicated linked "
        f"worktree on branch `{wt.branch}`, created from origin/main at "
        f"{wt.base_sha[:12]}. Work here; do NOT create another worktree and do "
        f"not touch any other checkout.{stale}"
    )
