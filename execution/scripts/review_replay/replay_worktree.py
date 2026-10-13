"""The checkout a replayed case is reviewed in.

``harness_lens_runner.Worktree`` adds a worktree to the shared clone. That is
right for a live PR but wrong for replaying history: the worktree shares every
ref with the clone, so ``git log --all`` shows commits made after the reviewed
head, including the fix that produced the label. This class builds the same
kind of throwaway checkout from a shared-object clone whose refs are stripped:

* HEAD is detached at the case head (or at a new commit holding the case's
  planted patch on top of it),
* ``main`` and ``origin/main`` both point at the case's recorded base commit, so
  the brief's "diff against the base branch" gives the original PR diff,
* no other branch, tag or remote ref exists, and the remote is unusable.

Objects are shared with the source clone by path, not copied, so this is cheap.
The source clone is only ever read (and, for the runner's own repo, fetched).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path

GIT_IDENTITY = ("replay", "replay@example.invalid")


class ReplayWorktreeError(RuntimeError):
    pass


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=check
    )


def ensure_commit(source: Path, sha: str, pr: int | None, *, may_fetch: bool) -> None:
    """Make sure *sha* exists in *source*; fetch only when allowed."""
    if _git(source, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0:
        return
    if not may_fetch:
        raise ReplayWorktreeError(
            f"commit {sha[:12]} is not in {source.name} and fetching is not allowed"
        )
    attempts = ([f"pull/{pr}/head"] if pr else []) + [sha]
    for ref in attempts:
        if _git(source, "fetch", "-q", "origin", ref, check=False).returncode == 0:
            if (
                _git(
                    source, "cat-file", "-e", f"{sha}^{{commit}}", check=False
                ).returncode
                == 0
            ):
                return
    raise ReplayWorktreeError(f"could not obtain commit {sha[:12]} for PR {pr}")


@dataclass
class ReplayWorktree:
    source: Path
    path: Path
    base_sha: str | None
    pr: int | None = None
    patches: list[Path] = field(default_factory=list)
    pr_description: str = "(no description is available in this replay)"
    may_fetch: bool = False
    head_sha: str = ""  # the commit the lens is told to review
    _created: bool = False

    def create(self, *, head: str) -> None:
        if not self.base_sha:
            raise ReplayWorktreeError("case has no recorded base commit")
        ensure_commit(self.source, head, self.pr, may_fetch=self.may_fetch)
        ensure_commit(self.source, self.base_sha, None, may_fetch=False)
        subprocess.run(
            [
                "git",
                "clone",
                "-q",
                "--shared",
                "--no-checkout",
                str(self.source),
                str(self.path),
            ],
            check=True,
            capture_output=True,
        )
        self._created = True
        p = self.path
        _git(p, "config", "user.name", GIT_IDENTITY[0])
        _git(p, "config", "user.email", GIT_IDENTITY[1])
        _git(p, "config", "gc.auto", "0")
        _git(p, "checkout", "-q", "--detach", head)
        # Strip every ref, then recreate only the base branch and the detached head.
        refs = _git(p, "for-each-ref", "--format=%(refname)").stdout.split()
        for ref in refs:
            _git(p, "update-ref", "-d", ref)
        _git(p, "remote", "remove", "origin", check=False)
        _git(p, "update-ref", "refs/heads/main", self.base_sha)
        _git(p, "update-ref", "refs/remotes/origin/main", self.base_sha)
        _git(p, "config", "remote.origin.url", "none://replay-has-no-remote")
        _git(p, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        _git(p, "reflog", "expire", "--expire=now", "--all", check=False)
        shutil.rmtree(p / ".git" / "logs", ignore_errors=True)
        for name in (".env", ".env.development", ".env.local"):
            if (p / name).is_file():
                (p / name).unlink()
        for patch in self.patches:
            r = _git(p, "apply", "--whitespace=nowarn", str(patch), check=False)
            if r.returncode != 0:
                raise ReplayWorktreeError(
                    f"patch {patch.name} does not apply: {r.stderr.strip()[:200]}"
                )
        if self.patches:
            _git(p, "add", "-A")
            _git(p, "commit", "-q", "-m", "Proposed change under review (replay)")
        self.head_sha = _git(p, "rev-parse", "HEAD").stdout.strip()
        (p / ".git" / "info").mkdir(exist_ok=True)
        with (p / ".git" / "info" / "exclude").open("a") as fh:
            fh.write("PR_DESCRIPTION.md\n")
        (p / "PR_DESCRIPTION.md").write_text(
            self.pr_description + "\n", encoding="utf-8"
        )

    def remove(self) -> None:
        if self._created:
            shutil.rmtree(self.path, ignore_errors=True)


def retarget(worktree: ReplayWorktree, target):
    """``after_create`` hook for the runner: review the (possibly patched) head."""
    return replace(target, head=worktree.head_sha)
