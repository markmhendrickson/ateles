#!/usr/bin/env python3
"""SessionStart hook — warn when an interactive session's checkout is stale.

`lib/daemon_runtime/checkout_drift.py` reports this for DAEMONS at startup.
Interactive sessions had no equivalent, which is exactly how one ran for its
entire length today detached at a commit 8 behind `origin/main`: it grepped
the stale tree for a test enforcing `_AGENT_ACTION_TYPE` vocabulary parity,
found none, and concluded the mechanism had never been built. The test
existed on `origin/main` (`execution/daemons/apis/test_action_type_vocabulary.py`,
PR #1049) and mentioned the constant seven times — the grep was correct about
the tree it searched, but the tree was wrong. This module closes that gap for
sessions the same way `checkout_drift.py` closes it for daemons.

## Posture — advisory only

This is a SessionStart hook: it reports, it never blocks. A session that
cannot start is worse than one that starts on a stale checkout and is told
so. There is no enforce/fatal mode here, deliberately unlike
`checkout_drift.py`'s `ATELES_ENFORCE_CHECKOUT_FRESHNESS` — a daemon can be
restarted from a fresh checkout with no user waiting on it; an interactive
session opening is the user waiting on it.

## No fetch by default

Comparing against `origin/main` accurately would need `git fetch`, but a
fetch on every session start adds network latency to every session start and
fails outright when offline. So by default this compares HEAD against
whatever `refs/remotes/origin/main` already points to locally — the same
non-fetching comparison `checkout_drift.py` falls back to when
`ATELES_CHECKOUT_DRIFT_NO_FETCH=1` is set, just made the default here instead
of the opt-in. Set `ATELES_SESSION_FRESHNESS_FETCH=1` to fetch first for a
fully current answer. A failed or timed-out opt-in fetch is `unknown` and
does not consult the local ref. Unset `ATELES_SESSION_FRESHNESS_FETCH` still
does not fetch. This hook still never blocks: it reports and exits 0.

## `unknown` is not `current`

A failed or absent comparison — no `origin/main` remote-tracking ref, a
detached HEAD with nothing to compare, a git error, a failed opt-in fetch, a
failed `git status` — reports `unknown`. `clean` prints nothing; `unknown`
prints one unverified line and does not call `banner()`; `not_a_repo` prints
nothing. It is NOT reported as fresh. `checkout_drift.py` makes the same
distinction: offline (or otherwise unanswerable) must not look identical to
up-to-date. `is_drifted` still excludes `unknown`, so the unverified line is
not the drift warning.

## Fail-open, stdlib-only

Follows `.claude/hooks/hook_wiring_reference.py`'s stated rationale: this
runs on every session start in every environment, including sandboxed and
offline agent runs, so a checker that can hang or throw must not halt the
session. An exception prints the unverified line (exception class name only)
and the hook still exits 0.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: Opt-in: fetch `origin/main` before comparing, for a fully current answer.
#: Off by default — see module docstring ("No fetch by default").
FETCH_ENV = "ATELES_SESSION_FRESHNESS_FETCH"

_GIT_TIMEOUT = 8  # seconds; a hung git must never hang session start


@dataclass(frozen=True)
class FreshnessReport:
    """How this checkout compares to the last-known `origin/main`."""

    #: "clean" | "behind" | "diverged" | "dirty" | "unknown" | "not_a_repo"
    state: str
    behind: int = 0
    ahead: int = 0
    head: str = ""
    detail: str = ""
    worktree_path: str = ""

    @property
    def is_drifted(self) -> bool:
        """True only for a positive, unknown-safe verdict of drift.

        `unknown` and `not_a_repo` are deliberately excluded — neither is
        evidence of staleness, and reporting them as drift would train the
        operator to ignore the message the same way a false answer would.
        """
        return self.state in ("behind", "diverged", "dirty")


def _git(args: list[str], cwd: Path) -> tuple[int, str]:
    try:
        p = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
        return p.returncode, (p.stdout or p.stderr or "").strip()
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 1, str(exc)


def check_freshness(
    root: Path | str | None = None, *, fetch: bool | None = None
) -> FreshnessReport:
    """Report how the checkout containing `root` compares to `origin/main`.

    `root` defaults to the current working directory. Never raises.
    """
    start = Path(root) if root is not None else Path.cwd()

    rc, top = _git(["rev-parse", "--show-toplevel"], start)
    if rc != 0:
        return FreshnessReport(state="not_a_repo", detail=top[:200])
    repo = Path(top)

    rc, head = _git(["rev-parse", "--short", "HEAD"], repo)
    if rc != 0:
        return FreshnessReport(
            state="unknown", detail=f"cannot read HEAD: {head[:120]}"
        )

    # Identify whether HEAD sits in a linked worktree (vs. the main clone) so
    # the remedy message can be worktree-aware.
    rc, git_dir = _git(["rev-parse", "--git-dir"], repo)
    rc2, common_dir = _git(["rev-parse", "--git-common-dir"], repo)
    in_linked_worktree = (
        rc == 0 and rc2 == 0 and Path(git_dir).resolve() != Path(common_dir).resolve()
    )

    do_fetch = fetch if fetch is not None else os.environ.get(FETCH_ENV) == "1"
    if do_fetch:
        rc, out = _git(["fetch", "--quiet", "--no-tags", "origin", "main"], repo)
        if rc != 0:
            # _git already maps TimeoutExpired and OSError to return code 1.
            return FreshnessReport(
                state="unknown",
                head=head,
                detail=f"fetch failed: {out[:120]}",
            )

    # Compare against the local remote-tracking ref, never a live fetch by
    # default (see module docstring). `origin/main` is the convention this
    # repo uses throughout (`checkout_drift.py`, `hook_wiring_reference.py`).
    rc, _ = _git(["rev-parse", "--verify", "-q", "refs/remotes/origin/main"], repo)
    if rc != 0:
        return FreshnessReport(
            state="unknown", head=head, detail="no refs/remotes/origin/main"
        )

    rc, counts = _git(
        ["rev-list", "--left-right", "--count", "HEAD...refs/remotes/origin/main"], repo
    )
    if rc != 0:
        return FreshnessReport(state="unknown", head=head, detail=counts[:120])
    try:
        ahead_s, behind_s = counts.split()
        ahead, behind = int(ahead_s), int(behind_s)
    except ValueError:
        return FreshnessReport(
            state="unknown",
            head=head,
            detail=f"unparseable rev-list output: {counts[:80]}",
        )

    if ahead and behind:
        state = "diverged"
    elif behind:
        state = "behind"
    elif ahead:
        state = "diverged"  # ahead-only: unpushed local commits, not "current"
    else:
        rc, dirty = _git(["status", "--porcelain"], repo)
        if rc != 0:
            return FreshnessReport(
                state="unknown",
                behind=behind,
                ahead=ahead,
                head=head,
                detail=(dirty[:120] or "git status failed"),
                worktree_path=str(repo) if in_linked_worktree else "",
            )
        tracked = [ln for ln in dirty.splitlines() if ln[:2] not in ("??",)]
        state = "dirty" if tracked else "clean"

    return FreshnessReport(
        state=state,
        behind=behind,
        ahead=ahead,
        head=head,
        worktree_path=str(repo) if in_linked_worktree else "",
    )


def remedy(report: FreshnessReport) -> str:
    """The command(s) to run to fix the reported drift, worktree-aware.

    Names the fallback discovered today: `git checkout main` fails with
    "already used by worktree" when `main` is checked out elsewhere (as it
    was in the abandoned `ateles-wt-routing` worktree, 127 commits behind and
    idle for weeks) — `git checkout --detach origin/main` works regardless.
    """
    return (
        "git checkout --detach origin/main   (use --detach: plain "
        '`git checkout main` fails with "already used by worktree" if '
        "main is checked out in another worktree — this repo has hit that "
        "exact case)"
    )


def summary(report: FreshnessReport) -> str:
    if report.state == "behind":
        return f"{report.behind} commit(s) BEHIND origin/main"
    if report.state == "diverged":
        return (
            f"DIVERGED from origin/main: {report.ahead} local commit(s) not "
            f"upstream, {report.behind} upstream commit(s) missing"
        )
    if report.state == "dirty":
        return "has uncommitted changes"
    return report.state


def banner(report: FreshnessReport) -> str:
    return (
        f"[checkout-freshness] WARNING: this session's working tree is "
        f"{summary(report)} (HEAD={report.head}). A grep, a test run, or a "
        "diagnosis against this tree may be answering about code that no "
        "longer matches origin/main — see ateles#973-adjacent incident "
        "2026-09-18, where a session ran its entire length 8 commits behind "
        "and concluded a mechanism did not exist that had already merged. "
        f"To refresh: {remedy(report)}. This is advisory only — nothing is "
        "blocked."
    )


def main() -> int:
    try:
        report = check_freshness()
        if report.is_drifted:
            print(banner(report))
        elif report.state == "unknown":
            # Not banner(): that line claims drift (WARNING / BEHIND / --detach).
            print(
                "[checkout-freshness] UNVERIFIED: could not compare this "
                f"checkout to origin/main ({report.detail}). This check is "
                "not currently protecting anything."
            )
    except Exception as exc:  # noqa: BLE001 — report, never halt session start
        print(
            "[checkout-freshness] UNVERIFIED: the freshness check raised "
            f"({type(exc).__name__}). This check is not currently protecting "
            "anything."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
