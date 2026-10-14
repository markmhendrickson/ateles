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

It also keeps a small in-process registry of builds in flight, so that a task an
in-flight build files to track its own work is recognised as that build and not
dispatched a second time.

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
from typing import Iterator, Mapping

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


def issue_key(snapshot: Mapping[str, object]) -> tuple[str, int] | None:
    """The (repository, issue number) a task is about, or None when unknown.

    Structured fields win (`repository` + `issue_number`). A task that carries
    neither is read for ONE unambiguous `owner/name#N` reference in its title or
    description; two different references are ambiguous and give None, because
    matching the wrong issue would suppress a legitimate dispatch.
    """
    repo = str(snapshot.get("repository") or "").strip().lower()
    number = _as_issue_number(snapshot.get("issue_number"))
    if repo and "/" in repo and number:
        return repo, number

    text = " ".join(
        str(snapshot.get(field) or "")
        for field in ("title", "description", "body")
    )
    refs = {
        (m.group(1).lower(), int(m.group(2))) for m in _REPO_ISSUE_RE.finditer(text)
    } | {(m.group(1).lower(), int(m.group(2))) for m in _ISSUE_URL_RE.finditer(text)}
    return next(iter(refs)) if len(refs) == 1 else None


def task_repository(snapshot: Mapping[str, object]) -> str:
    """The repository slug a task targets, '' when it names none."""
    key = issue_key(snapshot)
    if key:
        return key[0]
    repo = str(snapshot.get("repository") or "").strip()
    return repo if "/" in repo else ""


# ── In-flight builds ─────────────────────────────────────────────────────────

_inflight: dict[tuple[str, int], str] = {}
_inflight_lock = threading.Lock()


@contextlib.contextmanager
def claim_build(repository: str, number: int, holder: str) -> Iterator[None]:
    """Record that a build of repository#number is running, for its duration.

    The claim is released on every exit path. Re-claiming a key that is already
    held (two builders for one issue) keeps the FIRST holder, and only that
    holder's exit clears it.
    """
    key = (repository.strip().lower(), int(number))
    with _inflight_lock:
        owned = key not in _inflight
        if owned:
            _inflight[key] = holder
    try:
        yield
    finally:
        if owned:
            with _inflight_lock:
                _inflight.pop(key, None)


def active_build(repository: str, number: int) -> str | None:
    """The holder of the in-flight build of repository#number, else None."""
    with _inflight_lock:
        return _inflight.get((repository.strip().lower(), int(number)))


def inflight_build_for_task(snapshot: Mapping[str, object]) -> str | None:
    """The in-flight build a task would duplicate, or None.

    A task is a duplicate when it is about an issue some builder is already
    building right now: that builder files the task to track its own work, and
    dispatching it would start a second builder on the same issue.
    """
    key = issue_key(snapshot)
    return active_build(*key) if key else None


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


def clone_for(repository: str) -> Path | None:
    """The local clone to branch worktrees from, or None when there is none."""
    repository = repository.strip()
    configured = os.environ.get(BUILD_CLONES_ENV, "")
    for pair in configured.split(","):
        name, sep, raw = pair.partition("=")
        if sep and name.strip().lower() == repository.lower() and raw.strip():
            candidate = Path(raw.strip()).expanduser()
            return candidate if (candidate / ".git").exists() else None
    name = repository.rsplit("/", 1)[-1]
    if not name:
        return None
    candidate = Path("~/repos").expanduser() / name
    return candidate if (candidate / ".git").exists() else None


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
    clone = clone_for(repository)
    if clone is None:
        raise BuildWorktreeError(
            f"no local clone of {repository} to branch a worktree from "
            f"(looked in ${BUILD_CLONES_ENV} and ~/repos/<name>)"
        )

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
