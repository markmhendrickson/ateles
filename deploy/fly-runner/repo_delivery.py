"""Repository work for a Fly job that delivers a branch: the clone, and the
import of the agent's commits into a repository the agent cannot touch.

Not wired into ``run_job.py`` yet: nothing here runs until a later change
hands a job a delivery and an operator opts in.

The trust boundary
------------------
``run_job.py`` runs as root and is the only process that ever holds the job's
GitHub token. The CLI runs as an unprivileged user and never sees it. This
module keeps that boundary for the repository itself:

* ``origin.git`` is a bare repository owned by root that the agent cannot
  write. It is fetched from GitHub with the token passed in the fetching
  process's environment only (never written to any config file or URL).
* The agent works in ``checkout/``, a plain clone of ``origin.git`` that it
  owns. Everything in it, including its ``.git`` directory, is treated as
  hostile once the agent has run.
* The agent's work comes back as a git bundle written by a process running as
  the agent. Root reads that one file without following links, then fetches
  it into a quarantine ref in ``origin.git`` with fsck on, hooks off, and no
  system, global or agent configuration. Before the branch is updated, root
  checks that the bundle names only the assigned branch, that its head
  descends from the base, that it adds no more than a fixed number of
  commits, has no merges, adds no submodule, ``.gitmodules``, path that
  escapes the repository or link that points outside it, and no oversized
  object.

Every message names what was refused, never a value.
"""

from __future__ import annotations

import base64
import os
import posixpath
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

GIT = shutil.which("git", path="/usr/local/bin:/usr/bin:/bin") or "/usr/bin/git"
WORK_ROOT = Path("/work")
# Never under the login volume or the job's credential directory.
FORBIDDEN_ROOTS = ("/data", "/run/job")

# Branches a builder may deliver to: <builder>/<slug>-<YYYYmmddTHHMMSS>-<6 hex>,
# the same shape local builds use. Anything else (the default branch, a
# release branch, a ref path, an uppercase or dotted name) is refused.
BUILDER_PREFIXES = ("cicada",)
BRANCH_RE = re.compile(
    r"(?:%s)/[a-z0-9]+(?:-[a-z0-9]+)*-[0-9]{8}T[0-9]{6}-[0-9a-f]{6}" % "|".join(BUILDER_PREFIXES),
    re.ASCII,
)
BASE_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", re.ASCII)

MAX_ORIGIN_BYTES = 300 * 1024 * 1024
MAX_BUNDLE_BYTES = 50 * 1024 * 1024
MAX_BLOB_BYTES = 10 * 1024 * 1024
MAX_COMMITS = 50
GIT_TIMEOUT_SECONDS = 300

QUARANTINE_REF = "refs/ateles/incoming"
_GITLINK_MODE = "160000"
_SYMLINK_MODE = "120000"
_ZERO = "0" * 40


class RepoWorkError(Exception):
    """A clone or import step was refused or failed; the message names what,
    never a value."""


@dataclass(frozen=True)
class Work:
    job: Path
    origin: Path
    checkout: Path
    export: Path
    home: Path


@dataclass(frozen=True)
class Imported:
    head: str
    commits: int


# ── Paths ────────────────────────────────────────────────────────────────────


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def check_work_root(root: Path, forbidden: tuple[str, ...] = FORBIDDEN_ROOTS) -> Path:
    """*root* as given, when it is absolute, free of links in every component
    and outside the login volume and the job directory."""
    text = str(root)
    if not os.path.isabs(text) or os.path.normpath(text) != text:
        raise RepoWorkError("the work directory must be an absolute, normalised path")
    if os.path.realpath(text) != text:
        raise RepoWorkError("the work directory must not pass through a link")
    for bad in forbidden:
        if _under(text, bad) or _under(bad, text):
            raise RepoWorkError("the work directory overlaps the login volume or the job directory")
    return root


def _remove(path: Path) -> None:
    """Remove *path* without following a link at any level."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def clear_work_root(root: Path) -> None:
    """Delete everything under *root*, never following a link."""
    for entry in os.listdir(root):
        _remove(root / entry)


def prepare_work(root: Path = WORK_ROOT, *, run_id: str,
                 forbidden: tuple[str, ...] = FORBIDDEN_ROOTS) -> Work:  # fmt: skip
    """Clear any leftover work and lay out this job's directories (root-owned;
    the agent can traverse the job directory but write only its checkout and
    its export directory, which ``make_checkout`` hands over)."""
    check_work_root(root, forbidden)
    root.mkdir(mode=0o711, parents=True, exist_ok=True)
    if os.path.islink(root) or not root.is_dir():
        raise RepoWorkError("the work directory is not a plain directory")
    clear_work_root(root)
    safe = "".join(c for c in run_id if c.isalnum() or c in "-_")[:40] or "run"
    job = root / f"job-{safe}"
    job.mkdir(mode=0o711)
    work = Work(job=job, origin=job / "origin.git", checkout=job / "checkout",
                export=job / "export", home=job / "home")  # fmt: skip
    work.home.mkdir(mode=0o700)
    return work


def remove_work(root: Path = WORK_ROOT) -> None:
    """The job's ``finally``: nothing of the clone outlives the job."""
    if root.is_dir() and not os.path.islink(root):
        clear_work_root(root)


# ── Git, with nothing inherited ──────────────────────────────────────────────


def auth_config(token: str) -> list[tuple[str, str]]:
    """The one setting that authenticates a fetch or push to GitHub, for a
    process environment only: an ``Authorization`` header, never a URL or a
    config file."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return [("http.https://github.com/.extraheader", f"AUTHORIZATION: basic {basic}")]


def git_env(home: Path, *, allow: str, config: list[tuple[str, str]] = ()) -> dict[str, str]:
    """A git environment built from scratch: no system or global config, no
    replace objects, no prompts, hooks off, only the named transports."""
    settings = [
        ("core.hooksPath", "/dev/null"),
        ("credential.helper", ""),
        ("core.fsmonitor", "false"),
        ("transfer.fsckObjects", "true"),
        ("fetch.fsckObjects", "true"),
        ("protocol.allow", "never"),
        *[(f"protocol.{name}.allow", "always") for name in allow.split(":")],
        *config,
    ]
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_ALLOW_PROTOCOL": allow,
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "GIT_CONFIG_COUNT": str(len(settings)),
    }
    for i, (key, value) in enumerate(settings):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


def _git(args: list[str], *, env: dict[str, str], cwd: Path | None = None,
         ident: tuple[int, int] | None = None, what: str,
         secrets: tuple[str, ...] = ()) -> str:  # fmt: skip
    drop = {} if ident is None else {"user": ident[0], "group": ident[1], "extra_groups": []}
    try:
        done = subprocess.run([GIT, *args], env=env, cwd=cwd, capture_output=True,
                              timeout=GIT_TIMEOUT_SECONDS, check=False, **drop)  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        raise RepoWorkError(f"{what}: git could not run") from None
    if done.returncode != 0:
        tail = done.stderr.decode("utf-8", "replace").strip().splitlines()[-1:]
        text = tail[0][:200] if tail else f"exit {done.returncode}"
        for secret in secrets:
            text = text.replace(secret, "[redacted]")
        raise RepoWorkError(f"{what} failed: {text}")
    return done.stdout.decode("utf-8", "replace")


def _size(path: Path) -> int:
    total = 0
    for dirpath, _, files in os.walk(path):
        for name in files:
            total += os.lstat(os.path.join(dirpath, name)).st_size
    return total


# ── The clone ────────────────────────────────────────────────────────────────


def clone_origin(work: Work, *, url: str, base_branch: str, token: str,
                 allow: str = "https", max_bytes: int = MAX_ORIGIN_BYTES) -> str:  # fmt: skip
    """Fetch the base branch (depth 1) into a fresh root-owned ``origin.git``
    and return its commit. The token reaches only the fetching process."""
    if not BASE_BRANCH_RE.fullmatch(base_branch) or ".." in base_branch:
        raise RepoWorkError("the base branch name is not acceptable")
    auth = auth_config(token)
    secrets = (token, auth[0][1])
    _git(["init", "-q", "--bare", str(work.origin)], env=git_env(work.home, allow=allow),
         what="creating the origin repository")  # fmt: skip
    os.chmod(work.origin, 0o700)  # root's alone; the agent clones nothing from it itself
    _git(["-C", str(work.origin), "fetch", "-q", "--depth=1", "--no-tags",
          "--no-write-fetch-head", url, f"+refs/heads/{base_branch}:refs/heads/{base_branch}"],
         env=git_env(work.home, allow=allow, config=auth), what="the clone",
         secrets=secrets)  # fmt: skip
    _git(["-C", str(work.origin), "symbolic-ref", "HEAD", f"refs/heads/{base_branch}"],
         env=git_env(work.home, allow=allow), what="setting the origin head")  # fmt: skip
    if _size(work.origin) > max_bytes:
        raise RepoWorkError(f"the clone is larger than {max_bytes} bytes")
    return _git(["-C", str(work.origin), "rev-parse", "--verify",
                 f"refs/heads/{base_branch}^{{commit}}"],
                env=git_env(work.home, allow=allow), what="reading the base").strip()  # fmt: skip


def make_checkout(work: Work, *, branch: str, base_branch: str, repository: str,
                  ident: tuple[int, int] | None, author: tuple[str, str]) -> None:  # fmt: skip
    """The agent's checkout of ``origin.git`` on its assigned branch, handed to
    the agent's user with its export directory. Its remote names the plain
    https URL; it carries no credential."""
    check_branch(branch)
    env = git_env(work.home, allow="file")
    _git(["clone", "-q", "--no-hardlinks", "--branch", base_branch, str(work.origin),
          str(work.checkout)], env=env, what="the agent's checkout")  # fmt: skip
    for key, value in (("remote.origin.url", f"https://github.com/{repository}.git"),
                       ("user.name", author[0]), ("user.email", author[1])):  # fmt: skip
        _git(["-C", str(work.checkout), "config", key, value], env=env,
             what="configuring the agent's checkout")  # fmt: skip
    _git(["-C", str(work.checkout), "checkout", "-q", "-b", branch], env=env,
         what="creating the assigned branch")  # fmt: skip
    work.export.mkdir(mode=0o700)
    if ident is not None:
        for path in (work.checkout, work.export):
            for dirpath, dirnames, filenames in os.walk(path):
                os.chown(dirpath, *ident)
                for name in [*dirnames, *filenames]:
                    os.chown(os.path.join(dirpath, name), *ident, follow_symlinks=False)


def check_branch(branch: str) -> str:
    if not isinstance(branch, str) or not BRANCH_RE.fullmatch(branch):
        raise RepoWorkError("the delivery branch name does not match the builder pattern")
    return branch


# ── The import ───────────────────────────────────────────────────────────────

BUNDLE_NAME = "work.bundle"


def export_agent_work(work: Work, *, branch: str, base: str,
                      ident: tuple[int, int] | None) -> bool:  # fmt: skip
    """Run AS THE AGENT: bundle the assigned branch's commits after *base*
    into the export directory. False when the branch adds nothing. What it
    writes is untrusted; ``import_agent_work`` checks all of it."""
    check_branch(branch)
    env = git_env(work.home, allow="file")
    env["HOME"] = str(work.export)
    head = _git(["-C", str(work.checkout), "rev-parse", "--verify", "-q",
                 f"refs/heads/{branch}^{{commit}}"], env=env, ident=ident,
                what="reading the assigned branch").strip()  # fmt: skip
    if head == base:
        return False
    _git(["-C", str(work.checkout), "bundle", "create", "-q",
          str(work.export / BUNDLE_NAME), f"refs/heads/{branch}", f"^{base}"],
         env=env, ident=ident, what="bundling the agent's work")  # fmt: skip
    return True


def _read_export(work: Work, max_bytes: int) -> Path:
    """Copy the agent's bundle into a root-owned file, opening the export
    directory and the file without following a link."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        dfd = os.open(work.export, os.O_RDONLY | os.O_DIRECTORY | nofollow)
    except OSError:
        raise RepoWorkError("the export directory is missing or is a link") from None
    try:
        fd = os.open(BUNDLE_NAME, os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0),
                     dir_fd=dfd)  # fmt: skip
    except OSError:
        raise RepoWorkError("the agent's bundle is missing or is a link") from None
    finally:
        os.close(dfd)
    copy = work.job / "import.bundle"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RepoWorkError("the agent's bundle is not a regular file")
        with os.fdopen(os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
            total = 0
            while chunk := os.read(fd, 1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise RepoWorkError(f"the agent's bundle is larger than {max_bytes} bytes")
                out.write(chunk)
    finally:
        os.close(fd)
    return copy


def _bad_path(path: str) -> str:
    """Why a path an agent commit adds or changes is refused, or ``""``."""
    parts = path.split("/")
    for part in parts:
        folded = part.casefold().rstrip(". ")
        if part in ("", ".", "..") or folded == ".git" or folded == "git~1":
            return "a path that leaves the repository or enters .git"
        if folded == ".gitmodules":
            return "a submodule definition (.gitmodules)"
    return ""


def _link_stays_inside(path: str, target: str) -> bool:
    """A link's target must be relative, climb only as a prefix (``../..``)
    and never above the repository root, and never name ``.git``. A ``..``
    after an ordinary name is refused: that name may itself be a link."""
    if not target or target.startswith("/") or "\0" in target:
        return False
    parts = [p for p in target.split("/") if p not in ("", ".")]
    ups = 0
    while ups < len(parts) and parts[ups] == "..":
        ups += 1
    rest = parts[ups:]
    depth = len([p for p in posixpath.dirname(path).split("/") if p])
    return ups <= depth and ".." not in rest and not _bad_path("/".join(rest) or "x")


def import_agent_work(work: Work, *, branch: str, base: str,
                      max_bundle_bytes: int = MAX_BUNDLE_BYTES,
                      max_commits: int = MAX_COMMITS,
                      max_blob_bytes: int = MAX_BLOB_BYTES) -> Imported:  # fmt: skip
    """Root's side: check the agent's bundle and, only if every check passes,
    point ``refs/heads/<branch>`` in ``origin.git`` at its head."""
    check_branch(branch)
    bundle = _read_export(work, max_bundle_bytes)
    env = git_env(work.home, allow="file")
    origin = ["-C", str(work.origin)]
    heads = _git([*origin, "bundle", "list-heads", str(bundle)], env=env,
                 what="reading the agent's bundle").splitlines()  # fmt: skip
    if len(heads) != 1 or heads[0].split(" ", 1)[1:] != [f"refs/heads/{branch}"]:
        raise RepoWorkError("the agent's bundle carries refs other than the assigned branch")
    try:
        _git([*origin, "fetch", "-q", "--no-tags", "--no-write-fetch-head", str(bundle),
              f"+refs/heads/{branch}:{QUARANTINE_REF}"], env=env,
             what="importing the agent's bundle")  # fmt: skip
        head = _git([*origin, "rev-parse", "--verify", f"{QUARANTINE_REF}^{{commit}}"],
                    env=env, what="reading the imported head").strip()  # fmt: skip
        if head != heads[0].split(" ", 1)[0]:
            raise RepoWorkError("the imported head is not the bundle's head")
        try:
            _git([*origin, "merge-base", "--is-ancestor", base, head], env=env,
                 what="checking descent")  # fmt: skip
        except RepoWorkError:
            raise RepoWorkError("the agent's branch does not descend from the base") from None
        commits = _git([*origin, "rev-list", f"{base}..{head}"], env=env,
                       what="listing the commits").split()  # fmt: skip
        if not 1 <= len(commits) <= max_commits:
            raise RepoWorkError(f"the agent's branch adds {len(commits)} commits; at most "
                                f"{max_commits} are accepted")  # fmt: skip
        if _git([*origin, "rev-list", "--min-parents=2", f"{base}..{head}"], env=env,
                what="looking for merges").strip():  # fmt: skip
            raise RepoWorkError("the agent's branch contains a merge commit")
        _check_changes(origin, env, commits)
        _check_objects(origin, env, base, head, max_blob_bytes)
        _git([*origin, "update-ref", f"refs/heads/{branch}", head, _ZERO], env=env,
             what="recording the branch")  # fmt: skip
        return Imported(head=head, commits=len(commits))
    finally:
        subprocess.run([GIT, *origin, "update-ref", "-d", QUARANTINE_REF], env=env,
                       capture_output=True, timeout=GIT_TIMEOUT_SECONDS, check=False)  # fmt: skip


def _check_changes(origin: list[str], env: dict[str, str], commits: list[str]) -> None:
    """Every path an agent commit adds or changes, and its mode."""
    for commit in commits:
        raw = _git([*origin, "diff-tree", "-r", "-z", "--no-renames", "--no-commit-id",
                    "--root", commit], env=env, what="reading a commit's changes")  # fmt: skip
        fields = raw.split("\0")
        for i in range(0, len(fields) - 1, 2):
            meta, path = fields[i].split(" "), fields[i + 1]
            if len(meta) < 5:
                raise RepoWorkError("a commit's changes could not be read")
            new_mode, new_sha, status = meta[1], meta[3], meta[4]
            if status.startswith("D"):
                continue
            why = _bad_path(path)
            if why:
                raise RepoWorkError(f"the agent's branch adds {why}")
            if new_mode == _GITLINK_MODE:
                raise RepoWorkError("the agent's branch adds a submodule entry")
            if new_mode == _SYMLINK_MODE:
                target = _git([*origin, "cat-file", "blob", new_sha], env=env,
                              what="reading a link")  # fmt: skip
                if not _link_stays_inside(path, target):
                    raise RepoWorkError("the agent's branch adds a link that points outside "
                                        "the repository")  # fmt: skip


def _check_objects(origin: list[str], env: dict[str, str], base: str, head: str,
                   max_blob_bytes: int) -> None:  # fmt: skip
    listed = _git([*origin, "rev-list", "--objects", "--no-object-names", head, f"^{base}"],
                  env=env, what="listing the new objects")  # fmt: skip
    if not listed.strip():
        return
    done = subprocess.run([GIT, *origin, "cat-file", "--batch-check=%(objecttype) %(objectsize)"],
                          input=listed.encode(), env=env, capture_output=True,
                          timeout=GIT_TIMEOUT_SECONDS, check=False)  # fmt: skip
    if done.returncode != 0:
        raise RepoWorkError("the new objects could not be sized")
    for line in done.stdout.decode().splitlines():
        kind, _, size = line.partition(" ")
        if kind == "blob" and int(size) > max_blob_bytes:
            raise RepoWorkError(f"the agent's branch adds a file larger than {max_blob_bytes} "
                                "bytes")  # fmt: skip
