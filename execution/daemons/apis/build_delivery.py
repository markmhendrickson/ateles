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
from typing import Callable, Iterator, Mapping
from urllib.parse import urlsplit

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


# Called, with the key, when the OWNER of a claim releases it (success or
# failure). The daemon installs the hook that sweeps for deferred tasks. Nothing
# is queued here: what is waiting lives on the tasks themselves (see below), so
# it survives a restart and no in-process list can be lost.
ReleaseHook = Callable[[tuple[str, int]], None]

_inflight: dict[tuple[str, int], BuildClaim] = {}
_runs: dict[str, tuple[str, int]] = {}
_inflight_lock = threading.Lock()
_release_hook: ReleaseHook | None = None


def set_release_hook(hook: ReleaseHook | None) -> None:
    global _release_hook
    _release_hook = hook


@contextlib.contextmanager
def claim_build(repository: str, number: int, holder: str) -> Iterator[BuildClaim]:
    """Record that a build of repository#number is running, for its duration.

    Released on EVERY exit path, success or failure; releasing runs the release
    hook, which is what re-dispatches tasks deferred behind it. A second claim on
    a key already held keeps the FIRST holder as the owner, and only the owner's
    exit releases the key.
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
        with _inflight_lock:
            _runs.pop(claim.run_id, None)
            if owned:
                _inflight.pop(key, None)
        if owned and _release_hook is not None:
            try:
                _release_hook(key)
            except Exception:  # noqa: BLE001 — a hook failure must not mask the build's outcome
                log.exception("[apis] build release hook failed for %s#%s", *key)


def active_build(repository: str, number: int) -> BuildClaim | None:
    """The in-flight build of repository#number, else None."""
    with _inflight_lock:
        return _inflight.get((repository.strip().lower(), int(number)))


# ── The durable deferral record ──────────────────────────────────────────────
#
# A task deferred behind a build is marked ON THE TASK, with existing task fields,
# before the deferral is acknowledged. The fields are written in this order, with
# blocked_reason LAST as the commit marker, and removed in the reverse order (reason
# first); a marker without a matching reason is never acted on:
#
#   status          pending   (unchanged: it has not been routed)
#   disposition     DEFERRED_DISPOSITION   (exact-matchable by the recovery sweep)
#   dispatch_context  "deferred-behind:<repo>#<n>:<run id>"   (what it waits for)
#   blocked_reason  a sentence the operator can read, naming the build and what
#                   happens next
#
# Nothing else identifies a deferred task, so the recovery sweep touches only
# tasks this mechanism marked and never the historical backlog.

DEFERRED_DISPOSITION = "deferred_behind_build"
_DEFERRAL_CONTEXT_RE = re.compile(
    r"deferred-behind:([^\s#]+)#(\d+):([0-9a-f]{16})"
)


def deferral_context(claim: BuildClaim) -> str:
    return f"deferred-behind:{claim.repository}#{claim.number}:{claim.run_id}"


def deferral_reason(claim: BuildClaim) -> str:
    return (
        f"Deferred: {claim.holder} is building {claim.repository}#{claim.number} "
        f"(run {claim.run_id}). Apis re-dispatches this task when that build ends "
        "and on every restart; no action needed unless you are notified."
    )


_DEFERRAL_REASON_RE = re.compile(
    r"Deferred: (?P<holder>\S+) is building (?P<repo>[^#\s]+)#(?P<number>\d+) "
    r"\(run (?P<run>[0-9a-f]{16})\)\. Apis re-dispatches this task when that "
    r"build ends and on every restart; no action needed unless you are notified\."
)


def reason_matches_context(reason: object, context: tuple[str, int, str]) -> bool:
    """Whether ``reason`` is the exact recorded form, for the build ``context`` names.

    The reason is the COMMIT MARKER of a deferral: it is written last and cleared
    first, so a task carrying the other fields without it is a half-written (or
    half-removed) record, never a deferral to act on.
    """
    match = _DEFERRAL_REASON_RE.fullmatch(str(reason or ""))
    return bool(
        match
        and match.group("repo") == context[0]
        and int(match.group("number")) == context[1]
        and match.group("run") == context[2]
    )


def parse_deferral_context(text: object) -> tuple[str, int, str] | None:
    """(repository, issue number, run id) from a deferral record, else None."""
    match = _DEFERRAL_CONTEXT_RE.fullmatch(str(text or "").strip())
    if not match:
        return None
    return match.group(1), int(match.group(2)), match.group(3)


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
    """A delivery worktree could not be provisioned.

    ``fix`` names the prerequisite to put right before retrying, so the operator
    is told what to do and not only what failed.
    """

    def __init__(self, reason: str, fix: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.fix = fix

    def __str__(self) -> str:
        return self.reason


class BuildNotStarted(RuntimeError):
    """The build was never launched. Distinct from a build that ran and produced
    no PR: nothing ran, so nothing about the spec or the gates is in question."""

    def __init__(self, repository: str, number: int, cause: BuildWorktreeError):
        super().__init__(f"{repository}#{number}: {cause.reason}")
        self.repository = repository
        self.number = number
        self.reason = cause.reason
        self.fix = cause.fix

    def operator_text(self) -> str:
        return (
            f"the build was NOT started: {self.reason}."
            + (f" Before retrying: {self.fix}" if self.fix else "")
        )


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


_SCP_REMOTE_RE = re.compile(r"git@(?P<host>[^:/@\\\s]+):(?P<path>[^:@\\\s?#%]+)")
_ALLOWED_SCHEMES = {"https": 443, "ssh": 22}
_USERINFO_RE = re.compile(r"[A-Za-z0-9._~:-]*", re.ASCII)
_GITHUB_HOST = "github.com"


def _repo_from_path(path: str) -> str | None:
    """``owner/name`` from exactly two path segments (``.git`` optional), else None."""
    segments = path.split("/")
    if len(segments) != 2 or not all(segments):
        return None
    owner, name = segments
    if name.endswith(".git"):
        name = name[: -len(".git")]
    candidate = f"{owner}/{name}"
    try:
        return validate_repository(candidate).lower()
    except BuildWorktreeError:
        return None


def normalise_remote(url: object) -> str | None:
    """``github.com/owner/name`` (lowercased) for an https or ssh GitHub remote.

    Accepts only ``https://[userinfo@]github.com/owner/repo[.git]``,
    ``ssh://[git@]github.com/owner/repo[.git]`` and the scp form
    ``git@github.com:owner/repo[.git]``. Everything else is None, and None never
    matches a repository: other transports (http, git, file, ext::, a bare path),
    a query or fragment, a port other than the scheme's default, a path with other
    than two segments (a trailing slash included), any percent-encoding,
    non-ASCII, whitespace or backslash, an ambiguous userinfo/host split (more
    than one ``@``), and a host that is not exactly ``github.com`` (no trailing
    dot, no look-alike). A refusal is cheap; a wrong accept grants delivery.
    """
    if (
        not isinstance(url, str)
        or not url
        or not url.isascii()
        or any(ch in url for ch in "%?#\\")
        or any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url)
    ):
        return None

    if "://" not in url:
        match = _SCP_REMOTE_RE.fullmatch(url)
        if not match or match.group("host").lower() != _GITHUB_HOST:
            return None
        repo = _repo_from_path(match.group("path"))
        return f"{_GITHUB_HOST}/{repo}" if repo else None

    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        netloc = parts.netloc
        port = parts.port
    except ValueError:
        return None
    if scheme not in _ALLOWED_SCHEMES or parts.query or parts.fragment or ";" in parts.path:
        return None
    if netloc.count("@") > 1:
        return None
    userinfo, at, hostport = netloc.rpartition("@")
    if at:
        if not _USERINFO_RE.fullmatch(userinfo) or userinfo.count(":") > 1:
            return None
        if scheme == "ssh" and userinfo != "git":
            return None
    host, colon, _ = hostport.partition(":")
    if host.lower() != _GITHUB_HOST:
        return None
    if colon and port != _ALLOWED_SCHEMES[scheme]:
        return None
    if not parts.path.startswith("/"):
        return None
    repo = _repo_from_path(parts.path[1:])
    return f"{_GITHUB_HOST}/{repo}" if repo else None


# A local path that a test may route a clone's github.com remote to through git's
# `url.<base>.insteadOf`. EMPTY in production and settable only from code (never
# from the environment): production refuses any rewrite of a clone's remote to
# anything but github.com/<repository>. The build-delivery eval sets it to its
# local bare repository, which stands in for GitHub.
TEST_ONLY_LOCAL_TRANSPORTS: frozenset[str] = frozenset()

_GIT_FAILED = -1  # `_git` could not run git at all (spawn error, timeout)


def _identity_fix(path: Path, repository: str, what: str = "clone") -> str:
    if what == "worktree":
        return (
            "verify the worktree's effective remote configuration (for example "
            "`git config --show-origin --get-all remote.origin.url` and `git "
            f"remote get-url --push --all origin` from inside it), or point "
            f"${BUILD_CLONES_ENV} ({repository}=<path>) at a clone of {repository} "
            "whose worktrees resolve to it"
        )
    return (
        f"set the origin of {path} to https://github.com/{repository}.git and "
        "remove any url.*.insteadOf or pushInsteadOf rewrite of it, or point "
        f"${BUILD_CLONES_ENV} ({repository}=<path>) at a clone of {repository}"
    )


async def _remote_urls(
    path: Path, repository: str, args: list[str], *, may_be_unset: bool, what: str = "clone"
) -> list[str]:
    """The URLs a git query returns; ANY git error refuses.

    The only tolerated non-zero is git's documented "key not set" for `git
    config --get-all`: exit status 1 with nothing on either stream. A failed
    spawn, a timeout, any other status, or a failure that printed something is a
    refusal, never an absent value.
    """
    rc, out, err = await _git(args, path)
    urls = [line.strip() for line in out.splitlines() if line.strip()]
    if rc == 0:
        return urls
    if may_be_unset and rc == 1 and not out.strip() and not err.strip():
        return []
    raise BuildWorktreeError(
        f"could not read the remote URLs of the {what} at {path} "
        f"(git {' '.join(args[:3])}: rc={rc}, {err.strip()[:120] or 'no detail'})",
        fix=_identity_fix(path, repository, what),
    )


async def verify_clone_identity(
    clone: Path, repository: str, *, what: str = "clone"
) -> None:
    """Refuse a checkout unless every URL git will use for origin is the repository.

    ``what`` is "clone" for the source clone and "worktree" for the worktree made
    from it: git configuration can depend on where it is read from, so the
    answer for the source says nothing about a checkout made from it, and BOTH are
    verified (the worktree before it is returned, before any token or grant).

    Two views must BOTH resolve to exactly ``github.com/<repository>``: the
    CONFIGURED fetch and push URLs (what the checkout says it is) and the
    EFFECTIVE ones from ``git remote get-url [--push] --all origin`` (what git
    will really contact, after `insteadOf` / `pushInsteadOf` rewrites). Any git
    error refuses. Runs before any fetch, worktree, network grant or token for
    the clone, and before the worktree is returned for the worktree.
    """
    want = f"github.com/{repository}".lower()
    fix = _identity_fix(clone, repository, what)

    def refuse(detail: str):
        return BuildWorktreeError(
            f"the {what} at {clone} has {detail} that is not {repository} — "
            f"refusing to build {repository} from it",
            fix=fix,
        )

    configured = await _remote_urls(
        clone, repository, ["config", "--get-all", "remote.origin.url"],
        may_be_unset=True, what=what,
    )
    if not configured:
        raise BuildWorktreeError(f"the {what} at {clone} has no origin remote", fix=fix)
    pushurls = await _remote_urls(
        clone, repository, ["config", "--get-all", "remote.origin.pushurl"],
        may_be_unset=True, what=what,
    )
    for url in [*configured, *pushurls]:
        if normalise_remote(url) != want:
            raise refuse("an origin")

    for args in (
        ["remote", "get-url", "--all", "origin"],
        ["remote", "get-url", "--push", "--all", "origin"],
    ):
        effective = await _remote_urls(
            clone, repository, args, may_be_unset=False, what=what
        )
        if not effective:
            raise BuildWorktreeError(
                f"git reports no URL for origin in the {what} at {clone}", fix=fix
            )
        for url in effective:
            if normalise_remote(url) != want and url not in TEST_ONLY_LOCAL_TRANSPORTS:
                raise refuse("an effective origin (after URL rewrites)")


async def _git(args: list[str], cwd: Path, timeout: int = _GIT_TIMEOUT_S):
    """Run git; returns (rc, stdout, stderr). Never raises.

    ``rc`` is ``_GIT_FAILED`` (-1) when git could not be run to completion, so a
    spawn failure or timeout is never mistaken for a status git itself returned.
    """
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
        return _GIT_FAILED, "", f"{type(exc).__name__}: {exc}"


async def _discard_worktree(clone: Path, path: Path, branch: str) -> bool:
    """Remove a worktree that was refused, and its branch. True if both went."""
    rc, _, _ = await _git(["worktree", "remove", "--force", str(path)], clone)
    rc2, _, _ = await _git(["branch", "-D", branch], clone)
    return rc == 0 and rc2 == 0 and not path.exists()


def _new_branch(label: str) -> tuple[str, str, str]:
    """``(branch, slug, stamp)``: the one naming scheme for a builder's branch,
    local or on Fly: ``cicada/<slug>-<YYYYmmddTHHMMSS>-<6 hex>``."""
    stamp = f"{time.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"
    slug = _slug(label)
    return f"cicada/{slug}-{stamp}", slug, stamp


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
            f"(looked in ${BUILD_CLONES_ENV} and ~/repos/<name>)",
            fix=(
                f"clone https://github.com/{repository}.git to "
                f"~/repos/{repository.rsplit('/', 1)[-1]}, or set "
                f"${BUILD_CLONES_ENV}={repository}=<path to an existing clone>"
            ),
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

    branch, slug, stamp = _new_branch(label)
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

    # Verified FROM the worktree, before it is returned and so before any caller
    # resolves a token or launches in it. A refused worktree holds only a checkout
    # of origin/main (nothing has run in it), so it is removed with its branch;
    # if removal fails its path is named and nothing is ever launched in it.
    try:
        await verify_clone_identity(path, repository, what="worktree")
    except BuildWorktreeError as exc:
        removed = await _discard_worktree(clone, path, branch)
        log.error(
            "[apis] build worktree for %s refused (%s); %s",
            repository,
            exc.reason,
            "removed" if removed else f"could not be removed — left at {path}, unused",
        )
        raise

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



# ── Delivery on Fly (operator opt-in) ────────────────────────────────────────
#
# With the delivery class listed in APIS_FLY_RUNNER_CLASSES, a builder's
# delivery runs on a Fly machine instead of a local worktree: the job runner
# clones the repository, runs the CLI in its own checkout, imports the
# agent's commits, pushes the one branch and opens or updates the pull
# request with a token minted for that job. Unset, nothing here routes.
#
# A launched delivery is never started twice for the same issue: a durable
# binding in the existing Fly ledger is written BEFORE the
# launch and kept with its outcome. Every lane that can re-run a builder (the
# swarm build handoff, the task path, the startup resume sweep that re-runs an
# interrupted issue pipeline, the task watchdog) reaches the builder through
# ``run_fly_delivery``, which refuses while any record exists for the key.
#
# Every status that reaches the receipt is terminal, with no exception:
# ``launching``, ``unknown`` and ``unreported`` (the outcome is not known), and
# every status the runner reports (``opened``, ``updated``, ``no_changes``,
# ``clone_failed``, ``not_delivered``, ``push_failed``, ``pr_failed``,
# ``no_time``). The runner assigns all of them only after the machine has
# launched, so none proves that nothing ran, and none is replayed or falls back
# to the local path, whether or not the class is still opted in. The only
# outcomes that may start over are the ones proven to be before any launch
# (not eligible, a refused token mint, a launch request that provably did not
# take effect); those never reach the receipt as a status, and the receipt
# reserved for them is removed. Automatic retry of a launched failure is not
# provided: it would be a separate, deliberate change.

FLY_DELIVERY_BASE_BRANCH = "main"
FLY_DELIVERY_AUTHOR = ("cicada (ateles swarm)", "cicada@ateles-swarm.invalid")
# Same ledger and lock as executor admission, accounting, and reaping.
FLY_DELIVERY_REGISTRY_ENV = "APIS_FLY_LEDGER_FILE"
# No receipt status is retryable: see the note above.


@dataclass(frozen=True)
class FlyDeliveryPlan:
    repository: str
    branch: str
    base_branch: str
    key: str  # "owner/name#123", or "owner/name#task:<id>" for a task with no issue
    issue_number: int | None

    def spec(self) -> dict:
        """The job.json delivery spec the Fly job runner accepts."""
        return {"branch": self.branch, "base_branch": self.base_branch,
                "author_name": FLY_DELIVERY_AUTHOR[0], "author_email": FLY_DELIVERY_AUTHOR[1]}


class FlyDeliveryEnded(RuntimeError):
    """A Fly delivery ran (or was refused as a repeat) without a pull request
    to hand on; ``operator_text`` says what the record shows."""

    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


def plan_fly_delivery(repository: str, label: str, *, key: str,
                      issue_number: int | None) -> FlyDeliveryPlan | None:  # fmt: skip
    """A Fly delivery plan when the operator opted deliveries onto Fly, else
    None (the builder runs locally, exactly as before)."""
    import fly_runner

    if not fly_runner.delivery_opted_in():
        return None
    validate_repository(repository)
    branch, _, _ = _new_branch(label)
    return FlyDeliveryPlan(repository=repository, branch=branch,
                           base_branch=FLY_DELIVERY_BASE_BRANCH, key=key,
                           issue_number=issue_number)  # fmt: skip


def fly_delivery_instructions(plan: FlyDeliveryPlan) -> str:
    """The Fly variant of the builder's workspace and delivery instructions.
    The local variant (``worktree_instructions``) is unchanged."""
    import fly_runner

    closes = (f" Reference the issue in the body with `Closes #{plan.issue_number}`."
              if plan.issue_number else "")  # fmt: skip
    return (
        f"YOUR WORKSPACE: your working directory is a fresh checkout of "
        f"{plan.repository} on branch `{plan.branch}`, created from origin/"
        f"{plan.base_branch} for this job. Work only here.\n"
        "DELIVERY: commit your changes on this branch with `git commit`. Do not push, "
        "do not open a pull request, and do not create other branches or change the "
        "remote: when you finish, the runner delivers this branch's new commits and "
        "opens or updates the pull request.\n"
        f"PULL REQUEST TEXT: write it to the file {fly_runner.PR_FILE_PLACEHOLDER} as "
        f"plain UTF-8 text. The first line is the title; the rest is the body.{closes}"
    )


def canonical_repository(repository: object) -> str:
    """``owner/name`` as GitHub compares it: case-folded, with surrounding
    whitespace, a ``https://github.com/`` / ``ssh://`` / ``git@github.com:``
    form and a trailing ``.git`` removed.

    The ONE place a repository is normalised for a delivery receipt. A value
    that is not a GitHub repository in any of those forms cannot start a Fly
    delivery (``plan_fly_delivery`` validates it strictly), so it only gets the
    whitespace-and-case treatment and a stable key.
    """
    text = str(repository or "").strip()
    if "://" in text or text.startswith("git@"):
        remote = normalise_remote(text)
        if remote:
            return remote[len(_GITHUB_HOST) + 1:]
    elif text.endswith(".git"):
        text = text[: -len(".git")]
    try:
        return validate_repository(text).lower()
    except BuildWorktreeError:
        return text.lower()


def fly_delivery_key(repository: object, issue_number: object = None, task_id: str = "") -> str:
    """The ONE derivation of a delivery receipt key. Every entrance, the
    blocker, the reservation and the record go through it, so the same
    repository and issue are the same receipt however each lane spells them."""
    repo = canonical_repository(repository)
    number = _as_issue_number(issue_number)
    if number:
        return f"{repo}#{number}"
    return f"{repo}#task:{str(task_id).strip()}"


def canonical_key(key: str) -> str:
    """*key* with its repository part re-derived by ``canonical_repository``
    (a key built elsewhere, or read back from a ledger written earlier)."""
    repo, sep, rest = str(key).strip().partition("#")
    return f"{canonical_repository(repo)}{sep}{rest.strip()}"


def _registry_path() -> Path:
    import fly_runner

    return fly_runner.FlyConfig.from_env().ledger_path


def _delivery_ledger():
    import fly_runner

    return fly_runner.FlyLedger(_registry_path())


# The supported operator action that clears one delivery receipt (dry run by
# default; see fly_delivery_receipt.py). Nothing clears a receipt automatically.
FLY_RECEIPT_COMMAND = "python3 execution/daemons/apis/fly_delivery_receipt.py"


def _receipt_for(key: str, receipts: dict) -> dict | None:
    """The receipt for the canonical *key*. A receipt written under another
    spelling of the same repository (an older key form) is found too."""
    if key in receipts:
        return receipts[key]
    for stored, entry in receipts.items():
        if canonical_key(stored) == key:
            return entry
    return None


def fly_delivery_facts(key: str, status, branch, run_id, commit=None) -> str:
    """The status, run and branch of a recorded delivery, one scannable line."""
    return (f"Status: {status or 'not reported'}. Run: {run_id or 'not reported'}. "
            f"Branch: {branch or 'not reported'}. Commit: {commit or 'not reported'}. "
            f"Key: {key}.")  # fmt: skip


def fly_delivery_recovery(key: str) -> str:
    """What the operator does next, in order, once a delivery is recorded."""
    return (
        "To recover:\n"
        "1. Inspect the remote branch named above and the run record for the run above.\n"
        "2. Reconcile what actually happened.\n"
        f"3. Clear the delivery receipt for this issue: {FLY_RECEIPT_COMMAND} clear {key} "
        f"(a dry run; to remove it, repeat with --confirm {key}).\n"
        "4. Only then re-dispatch.\n"
        "Until the receipt is cleared, any re-dispatch is refused and nothing falls back "
        "to the local path."
    )


def fly_delivery_blocker(key: str) -> str:
    """Why a new delivery for *key* must not start, or an empty string.

    Uses the executor's own durable ledger. A receipt survives reservation
    settlement and provider/accounting resets; the result is what the executor
    read back, bound to the issue/task before launch.
    """
    import fly_runner

    key = canonical_key(key)
    try:
        entry = _receipt_for(key, _delivery_ledger().delivery_receipts())
    except (OSError, fly_runner.FlyLedgerError):
        return ("the Fly delivery ledger could not be read, so whether a delivery for "
                f"{key} already ran is unknown; not starting another")
    if entry is None:
        return ""
    return ("Fly delivery refused: a delivery for this issue or task is already recorded.\n"
            + fly_delivery_facts(key, entry["status"], entry.get("branch"), entry.get("run_id"),
                                 entry.get("commit")) + "\n"
            + fly_delivery_recovery(key))


def _new_token() -> str:
    """A unique identity for one reservation."""
    return secrets.token_hex(16)


def _reserve_delivery(plan: FlyDeliveryPlan) -> str:
    """Reserve the receipt for *plan*'s key and return this reservation's token,
    or an empty string when a receipt already exists. The token is stored in the
    receipt: only the holder of it may settle or release that receipt."""
    import fly_runner

    token = _new_token()
    try:
        reserved = _delivery_ledger().reserve_delivery(
            canonical_key(plan.key),
            {"status": "launching", "branch": plan.branch, "run_id": "", "token": token,
             "recorded_at": time.time()},
        )
    except fly_runner.FlyLedgerError as exc:
        raise ValueError(str(exc)) from None
    return token if reserved else ""


def _settle_delivery(plan: FlyDeliveryPlan, token: str, entry: dict) -> bool:
    """Record *entry* as the outcome, only if the receipt is still this
    dispatch's reservation. False: it is not (nothing was written)."""
    import fly_runner

    try:
        return _delivery_ledger().settle_delivery(
            canonical_key(plan.key), token, {**entry, "token": token, "recorded_at": time.time()})
    except fly_runner.FlyLedgerError as exc:
        raise ValueError(str(exc)) from None


def _release_delivery(plan: FlyDeliveryPlan, token: str) -> bool:
    """Remove this dispatch's own still-``launching`` reservation after a
    proven pre-launch outcome. False: the receipt is not this dispatch's (or no
    longer ``launching``), so it was left untouched and nothing was cleaned up.
    Any exception means the ledger state is UNCERTAIN (the write may or may not
    have happened); the caller must not treat it as a cleanup or as ownership."""
    return _delivery_ledger().release_delivery(canonical_key(plan.key), token)


async def run_fly_delivery(plan: FlyDeliveryPlan, launch, blocked):
    """Run *launch* (the builder's ``run_skill`` call with the delivery spec)
    under the durable record. Returns the dispatch's SkillResult, ``blocked(
    reason)`` when a delivery for this key is already recorded, or None when
    nothing was sent to Fly (not eligible, or the mint was refused): the
    builder then runs its local path, as it would without Fly.

    Every write to the receipt after the reservation is a compare against the
    reservation's token: a dispatch that no longer owns the receipt (it was
    cleared and another dispatch reserved the key) changes nothing, and never
    falls back to the local path."""
    reason = fly_delivery_blocker(plan.key)
    if reason:
        log.warning("[apis] %s", reason)
        return blocked(reason)
    try:
        token = _reserve_delivery(plan)
    except (OSError, ValueError):
        return blocked("the Fly delivery record could not be written; not launching")
    if not token:
        return blocked("another dispatcher already reserved this Fly delivery; not launching")
    try:
        result = await launch()
    except BaseException:
        # The launch may have happened: keep it recorded, as unknown.
        with contextlib.suppress(Exception):
            _settle_delivery(plan, token, {"status": "unknown", "branch": plan.branch,
                                           "run_id": ""})  # fmt: skip
        raise
    launched = bool(result.fly_delivery_status or result.remote_run_id
                    or result.remote_outcome_unknown)
    if not launched:
        # Nothing reached Fly: this dispatch's own reservation is removed. Only a
        # release that RETURNS success lets the local path run. A release that
        # raised left the ledger in an unknown state (the removal may have
        # happened, and another dispatch may already hold the key): fail closed.
        try:
            owned = _release_delivery(plan, token)
        except Exception as exc:  # noqa: BLE001 - any failure is an uncertain state
            kind = type(exc).__name__
            log.warning("[apis] the delivery receipt for %s could not be confirmed after a "
                        "failed cleanup (%s); not starting the local path", plan.key, kind)
            if result.fly_ineligible_before_launch:
                return blocked(
                    f"the delivery receipt for {canonical_key(plan.key)} could not be confirmed "
                    f"after a failed cleanup ({kind}); the local path was NOT started. Run "
                    f"{FLY_RECEIPT_COMMAND} list, reconcile, and clear the receipt if needed.")
            return result
        if not owned:
            log.warning("[apis] the delivery receipt for %s is no longer this dispatch's; "
                        "left untouched", plan.key)
            if result.fly_ineligible_before_launch:
                # Another dispatch holds the key now: no local path beside it.
                return blocked("the delivery record for this issue or task now belongs to "
                               "another dispatch; not starting the local path")
            return result
        return None if result.fly_ineligible_before_launch else result
    try:
        settled = _settle_delivery(plan, token, {
            "status": result.fly_delivery_status or "unknown", "branch": plan.branch,
            "run_id": result.remote_run_id, "commit": result.fly_delivery_commit,
            "reason": result.fly_delivery_reason, "pr_number": result.fly_pr_number,
            "pr_url": result.fly_pr_url})  # fmt: skip
    except (OSError, ValueError):
        settled = True  # unreadable: the reservation stays as it was written
    if not settled:
        log.warning("[apis] the delivery receipt for %s is no longer this dispatch's; its "
                    "outcome was not written over it", plan.key)
    return result


async def verify_fly_delivery(result, plan) -> str | None:
    """Read a Fly delivery's pull request back from GitHub and compare it with
    the runner's report. None when it matches; otherwise the reason, which names
    the field that failed. A delivery is not verified until this returns None.

    Expected: the dispatched repository and base, and the branch, commit and
    pull request number the runner's record reported."""
    from lib.daemon_runtime.artifact_resolver import read_back_delivered_pr

    return await asyncio.to_thread(
        read_back_delivered_pr, repo=plan.repository, number=result.fly_pr_number,
        base=plan.base_branch, branch=result.fly_delivery_branch,
        commit=result.fly_delivery_commit)  # fmt: skip


_FLY_STATUS_TEXT = {
    "opened": "opened pull request {pr}",
    "updated": "updated pull request {pr}",
    "no_changes": "finished with no new commits on {branch}; nothing was pushed",
    "clone_failed": ("ended as clone_failed: the runner reported that it could not prepare its "
                     "clone of the repository"),
    "not_delivered": "ended as not_delivered: the runner's record reports no delivery",
    "push_failed": "did not get its push of {branch} accepted; no pull request was opened",
    "pr_failed": ("pushed {branch} ({commit}), but the pull request step did not complete; "
                  "the branch is on the remote"),
    "no_time": "ran out of time",
    "unknown": "has an UNKNOWN outcome (run {run}); it may have pushed {branch}",
    "unreported": ("finished, but its result record carries no delivery status (run {run}); "
                   "it may have pushed {branch}"),
}


def fly_delivery_summary(result, key: str = "") -> str:
    """Operator-facing text for a Fly delivery's outcome: what the record
    says, with the reason it gave, and no claim about why.

    Any outcome other than an opened or updated pull request starts with the
    status, run and branch, then says what the operator does next and how
    (``fly_delivery_recovery``) when *key* names the receipt it left."""
    status = result.fly_delivery_status
    template = _FLY_STATUS_TEXT.get(status, "ended with status {status}")
    text = "Fly delivery " + template.format(
        pr=f"#{result.fly_pr_number} ({result.fly_pr_url})" if result.fly_pr_url else "(none)",
        branch=result.fly_delivery_branch or "its branch",
        commit=(result.fly_delivery_commit or "")[:12] or "commit not reported",
        run=result.remote_run_id or "not known", status=status or "none")
    if status == "no_time" and result.fly_delivery_commit:
        text += (f"; {result.fly_delivery_branch} ({result.fly_delivery_commit[:12]}) was "
                 "pushed and no pull request was opened")  # fmt: skip
    if result.fly_delivery_reason and status not in ("opened", "updated"):
        text += f" ({result.fly_delivery_reason})"
    if status in ("opened", "updated"):
        return text + (
            f". Run {result.remote_run_id or 'not reported'}; "
            f"branch {result.fly_delivery_branch or 'not reported'}; "
            f"commit {result.fly_delivery_commit or 'not reported'}."
        )
    head = ("Fly delivery needs operator reconciliation.\n"
            + fly_delivery_facts(key or "not reported", status, result.fly_delivery_branch,
                                 result.remote_run_id, result.fly_delivery_commit))
    out = f"{head}\n{text}."
    return out + "\n" + fly_delivery_recovery(key) if key else out
