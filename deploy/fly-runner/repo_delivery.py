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
  read or write. It is fetched from GitHub with the token passed in the
  fetching process's environment only (never written to any config file or
  URL).
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

Limits, stated plainly
----------------------
* The clone cap (``MAX_ORIGIN_BYTES``) is checked after the fetch: it decides
  whether the clone is accepted, it does not bound peak disk use while the
  fetch runs.
* Paths and links are checked on the entries the agent's commits add or
  change. A changed link's target is checked lexically and, in addition,
  every component it passes through is looked up in the commit's tree: a
  target that passes through any link (one inherited from the base included)
  is refused, so a link cannot reach outside through another link. A changed
  ordinary path cannot pass through a link: a git tree cannot hold a link and
  a path below it at once.

Errors
------
Every failure is a ``RepoWorkError`` with a fixed category (``clone_failed``,
``import_refused``, ...), a fixed reason and, for a git failure, only the
class of its exit status. Git's own output is never forwarded: it can carry
URLs, paths, branch names or credentials. Nothing is kept for debugging.

Import contract
---------------
``import_agent_work`` returns ``Imported`` only when the branch is recorded
in ``origin.git`` AND the quarantine ref is confirmed gone. Any
``RepoWorkError`` means the import did not complete and nothing may be pushed.
If the quarantine ref cannot be removed after the branch was recorded, the
branch record is withdrawn and ``quarantine_failed`` is raised.
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
BUNDLE_NAME = "work.bundle"
_GITLINK_MODE = "160000"
_SYMLINK_MODE = "120000"
_ZERO = "0" * 40

# Error categories: what the caller acts on.
WORK_ROOT_REFUSED = "work_root_refused"
BRANCH_REFUSED = "branch_refused"
CLONE_FAILED = "clone_failed"
CLONE_REFUSED = "clone_refused"
CHECKOUT_FAILED = "checkout_failed"
EXPORT_FAILED = "export_failed"
IMPORT_FAILED = "import_failed"
IMPORT_REFUSED = "import_refused"
QUARANTINE_FAILED = "quarantine_failed"


class RepoWorkError(Exception):
    """A step was refused or failed. Carries a fixed category, a fixed reason
    and at most the class of a git exit status; never a value."""

    def __init__(self, category: str, reason: str, status: str = "") -> None:
        self.category, self.reason, self.status = category, reason, status
        super().__init__(f"{category}: {reason}" + (f" [{status}]" if status else ""))


@dataclass(frozen=True)
class Work:
    root: Path
    job: Path
    origin: Path
    checkout: Path
    export: Path
    home: Path


@dataclass(frozen=True)
class Imported:
    head: str
    commits: int


# ── Work roots: one guard for every entry point ──────────────────────────────


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def check_work_root(root: Path) -> Path:
    """*root* as given, when it is absolute, normalised, free of links in
    every component and outside the login volume and the job directory.
    Every entry point that deletes or creates under a work root applies it."""
    text = str(root)
    if not os.path.isabs(text) or os.path.normpath(text) != text:
        raise RepoWorkError(WORK_ROOT_REFUSED, "not an absolute, normalised path")
    if os.path.realpath(text) != text:
        raise RepoWorkError(WORK_ROOT_REFUSED, "passes through a link")
    for bad in FORBIDDEN_ROOTS:
        if _under(text, bad) or _under(bad, text):
            raise RepoWorkError(WORK_ROOT_REFUSED, "overlaps the login volume or the job directory")
    return root


def _check_work(work: Work) -> Work:
    """A ``Work`` is accepted only with the layout ``prepare_work`` gives."""
    check_work_root(work.root)
    job = work.job
    if job.parent != work.root or not job.name.startswith("job-") or work != _layout(work.root, job):
        raise RepoWorkError(WORK_ROOT_REFUSED, "not a prepared job layout")
    if os.path.realpath(job) != str(job):
        raise RepoWorkError(WORK_ROOT_REFUSED, "passes through a link")
    return work


def _layout(root: Path, job: Path) -> Work:
    return Work(root=root, job=job, origin=job / "origin.git", checkout=job / "checkout",
                export=job / "export", home=job / "home")  # fmt: skip


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


def _clear(root: Path) -> None:
    """Delete everything under an ALREADY GUARDED *root*, never following a
    link. Private: every exported caller applies ``check_work_root`` first."""
    if os.path.islink(root) or not root.is_dir():
        raise RepoWorkError(WORK_ROOT_REFUSED, "not a plain directory")
    for entry in os.listdir(root):
        _remove(root / entry)


def clear_work_root(root: Path) -> None:
    """Delete everything under *root*, never following a link. A refused
    root deletes nothing."""
    _clear(check_work_root(root))


def prepare_work(root: Path = WORK_ROOT, *, run_id: str) -> Work:
    """Clear any leftover work and lay out this job's directories (root-owned;
    the agent can traverse the job directory but write only its checkout and
    its export directory, which ``make_checkout`` hands over)."""
    check_work_root(root)
    root.mkdir(mode=0o711, parents=True, exist_ok=True)
    _clear(root)
    safe = "".join(c for c in run_id if c.isalnum() or c in "-_")[:40] or "run"
    job = root / f"job-{safe}"
    job.mkdir(mode=0o711)
    work = _layout(root, job)
    work.home.mkdir(mode=0o700)
    return work


def remove_work(root: Path = WORK_ROOT) -> None:
    """The job's ``finally``: nothing of the clone outlives the job. A
    refused root deletes nothing."""
    check_work_root(root)
    if os.path.lexists(root):
        _clear(root)


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


def _run(args: list[str], *, env: dict[str, str], ident: tuple[int, int] | None = None,
         stdin: bytes | None = None) -> subprocess.CompletedProcess | str:  # fmt: skip
    """Run git; the completed process, or the class of why it did not finish."""
    drop = {} if ident is None else {"user": ident[0], "group": ident[1], "extra_groups": []}
    try:
        return subprocess.run([GIT, *args], env=env, capture_output=True, input=stdin,
                              timeout=GIT_TIMEOUT_SECONDS, check=False, **drop)  # fmt: skip
    except subprocess.TimeoutExpired:
        return "timed_out"
    except (OSError, subprocess.SubprocessError):
        return "not_started"


def _status_class(done: subprocess.CompletedProcess | str) -> str:
    if isinstance(done, str):
        return done
    return "killed_by_signal" if done.returncode < 0 else "exit_nonzero"


def _git(args: list[str], *, env: dict[str, str], category: str, reason: str,
         ident: tuple[int, int] | None = None, stdin: bytes | None = None) -> str:  # fmt: skip
    """Run git and return its output; on failure raise *category*/*reason*
    with only the class of the exit status. Git's stderr is discarded."""
    done = _run(args, env=env, ident=ident, stdin=stdin)
    if isinstance(done, str) or done.returncode != 0:
        raise RepoWorkError(category, reason, _status_class(done))
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
    _check_work(work)
    if not isinstance(base_branch, str) or not BASE_BRANCH_RE.fullmatch(base_branch) \
            or ".." in base_branch:  # fmt: skip
        raise RepoWorkError(CLONE_REFUSED, "the base branch name is not acceptable")
    plain = git_env(work.home, allow=allow)
    origin = ["-C", str(work.origin)]
    _git(["init", "-q", "--bare", str(work.origin)], env=plain, category=CLONE_FAILED,
         reason="creating the origin repository")  # fmt: skip
    os.chmod(work.origin, 0o700)  # root's alone
    _git([*origin, "fetch", "-q", "--depth=1", "--no-tags", "--no-write-fetch-head", url,
          f"+refs/heads/{base_branch}:refs/heads/{base_branch}"],
         env=git_env(work.home, allow=allow, config=auth_config(token)),
         category=CLONE_FAILED, reason="fetching the base branch")  # fmt: skip
    _git([*origin, "symbolic-ref", "HEAD", f"refs/heads/{base_branch}"], env=plain,
         category=CLONE_FAILED, reason="setting the origin head")  # fmt: skip
    if _size(work.origin) > max_bytes:
        raise RepoWorkError(CLONE_REFUSED, "the clone is larger than the accepted size")
    return _git([*origin, "rev-parse", "--verify", f"refs/heads/{base_branch}^{{commit}}"],
                env=plain, category=CLONE_FAILED, reason="reading the base").strip()  # fmt: skip


def make_checkout(work: Work, *, branch: str, base_branch: str, repository: str,
                  ident: tuple[int, int] | None, author: tuple[str, str]) -> None:  # fmt: skip
    """The agent's checkout of ``origin.git`` on its assigned branch, handed to
    the agent's user with its export directory. Its remote names the plain
    https URL; it carries no credential."""
    _check_work(work)
    check_branch(branch)
    env = git_env(work.home, allow="file")
    checkout = ["-C", str(work.checkout)]
    _git(["clone", "-q", "--no-hardlinks", "--branch", base_branch, str(work.origin),
          str(work.checkout)], env=env, category=CHECKOUT_FAILED,
         reason="cloning the origin repository")  # fmt: skip
    for key, value in (("remote.origin.url", f"https://github.com/{repository}.git"),
                       ("user.name", author[0]), ("user.email", author[1])):  # fmt: skip
        _git([*checkout, "config", key, value], env=env, category=CHECKOUT_FAILED,
             reason="configuring the checkout")  # fmt: skip
    _git([*checkout, "checkout", "-q", "-b", branch], env=env, category=CHECKOUT_FAILED,
         reason="creating the assigned branch")  # fmt: skip
    work.export.mkdir(mode=0o700)
    if ident is not None:
        for path in (work.checkout, work.export):
            for dirpath, dirnames, filenames in os.walk(path):
                os.chown(dirpath, *ident)
                for name in [*dirnames, *filenames]:
                    os.chown(os.path.join(dirpath, name), *ident, follow_symlinks=False)


def check_branch(branch: str) -> str:
    if not isinstance(branch, str) or not BRANCH_RE.fullmatch(branch):
        raise RepoWorkError(BRANCH_REFUSED, "the branch name does not match the builder pattern")
    return branch


# ── The import ───────────────────────────────────────────────────────────────


def export_agent_work(work: Work, *, branch: str, base: str,
                      ident: tuple[int, int] | None) -> bool:  # fmt: skip
    """Run AS THE AGENT: bundle the assigned branch's commits after *base*
    into the export directory. False when the branch adds nothing. What it
    writes is untrusted; ``import_agent_work`` checks all of it."""
    _check_work(work)
    check_branch(branch)
    env = git_env(work.home, allow="file")
    env["HOME"] = str(work.export)
    checkout = ["-C", str(work.checkout)]
    head = _git([*checkout, "rev-parse", "--verify", "-q", f"refs/heads/{branch}^{{commit}}"],
                env=env, ident=ident, category=EXPORT_FAILED,
                reason="reading the assigned branch").strip()  # fmt: skip
    if head == base:
        return False
    _git([*checkout, "bundle", "create", "-q", str(work.export / BUNDLE_NAME),
          f"refs/heads/{branch}", f"^{base}"], env=env, ident=ident, category=EXPORT_FAILED,
         reason="bundling the agent's work")  # fmt: skip
    return True


def _read_export(work: Work, max_bytes: int) -> Path:
    """Copy the agent's bundle into a root-owned file, opening the export
    directory and the file without following a link."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        dfd = os.open(work.export, os.O_RDONLY | os.O_DIRECTORY | nofollow)
    except OSError:
        raise RepoWorkError(IMPORT_REFUSED, "the export directory is missing or is a link") from None
    try:
        fd = os.open(BUNDLE_NAME, os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0),
                     dir_fd=dfd)  # fmt: skip
    except OSError:
        raise RepoWorkError(IMPORT_REFUSED, "the bundle is missing or is a link") from None
    finally:
        os.close(dfd)
    copy = work.job / "import.bundle"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RepoWorkError(IMPORT_REFUSED, "the bundle is not a regular file")
        with os.fdopen(os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
            total = 0
            while chunk := os.read(fd, 1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise RepoWorkError(IMPORT_REFUSED, "the bundle is larger than accepted")
                out.write(chunk)
    finally:
        os.close(fd)
    return copy


def _bad_path(path: str) -> str:
    """Why a path an agent commit adds or changes is refused, or ``""``."""
    for part in path.split("/"):
        folded = part.casefold().rstrip(". ")
        if part in ("", ".", "..") or folded == ".git" or folded == "git~1":
            return "a path leaves the repository or enters .git"
        if folded == ".gitmodules":
            return "a submodule definition is added"
    return ""


def _link_target_path(path: str, target: str) -> str | None:
    """The repository path a link's target names, or None when it is absolute,
    climbs above the root, or has a ``..`` after an ordinary name (that name
    may itself be a link)."""
    if not target or target.startswith("/") or "\0" in target:
        return None
    parts = [p for p in target.split("/") if p not in ("", ".")]
    ups = 0
    while ups < len(parts) and parts[ups] == "..":
        ups += 1
    rest = parts[ups:]
    base = [p for p in posixpath.dirname(path).split("/") if p]
    if ups > len(base) or ".." in rest:
        return None
    resolved = "/".join(base[:len(base) - ups] + rest)
    return None if resolved and _bad_path(resolved) else resolved


def _passes_through_a_link(origin: list[str], env: dict[str, str], commit: str,
                           resolved: str) -> bool:  # fmt: skip
    """Whether any component of *resolved* is a link in *commit*'s tree."""
    parts = resolved.split("/") if resolved else []
    prefixes = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    if not prefixes:
        return False
    listed = _git([*origin, "ls-tree", "-z", commit, "--", *prefixes], env=env,
                  category=IMPORT_FAILED, reason="reading a link's path")  # fmt: skip
    modes = {}
    for entry in listed.split("\0"):
        meta, _, name = entry.partition("\t")
        if name:
            modes[name] = meta.split(" ", 1)[0]
    return any(modes.get(p) == _SYMLINK_MODE for p in prefixes)


def _drop_quarantine(origin: list[str], env: dict[str, str]) -> bool:
    """Remove the quarantine ref; True only when it is confirmed gone."""
    for _ in range(2):
        _run([*origin, "update-ref", "-d", QUARANTINE_REF], env=env)
        check = _run([*origin, "rev-parse", "--verify", "-q", QUARANTINE_REF], env=env)
        if not isinstance(check, str) and check.returncode == 1:
            return True
    return False


def import_agent_work(work: Work, *, branch: str, base: str,
                      max_bundle_bytes: int = MAX_BUNDLE_BYTES,
                      max_commits: int = MAX_COMMITS,
                      max_blob_bytes: int = MAX_BLOB_BYTES) -> Imported:  # fmt: skip
    """Root's side: check the agent's bundle and, only if every check passes,
    point ``refs/heads/<branch>`` in ``origin.git`` at its head. See the
    module's import contract."""
    _check_work(work)
    check_branch(branch)
    bundle = _read_export(work, max_bundle_bytes)
    env = git_env(work.home, allow="file")
    origin = ["-C", str(work.origin)]
    heads = _git([*origin, "bundle", "list-heads", str(bundle)], env=env,
                 category=IMPORT_REFUSED, reason="the bundle could not be read").splitlines()  # fmt: skip
    if len(heads) != 1 or heads[0].split(" ", 1)[1:] != [f"refs/heads/{branch}"]:
        raise RepoWorkError(IMPORT_REFUSED, "the bundle carries refs other than the assigned branch")
    try:
        imported = _validate_and_record(origin, env, bundle, branch, base, heads[0],
                                        max_commits, max_blob_bytes)  # fmt: skip
    except RepoWorkError:
        _drop_quarantine(origin, env)  # nothing was recorded; nothing may be pushed
        raise
    if not _drop_quarantine(origin, env):
        # Recorded but not clean: withdraw the record so no later step can
        # push from this state, and say so.
        _run([*origin, "update-ref", "-d", f"refs/heads/{branch}", imported.head], env=env)
        raise RepoWorkError(QUARANTINE_FAILED, "the quarantine ref could not be removed; the "
                            "import is withdrawn and nothing may be pushed")  # fmt: skip
    return imported


def _validate_and_record(origin: list[str], env: dict[str, str], bundle: Path, branch: str,
                         base: str, listed_head: str, max_commits: int,
                         max_blob_bytes: int) -> Imported:  # fmt: skip
    _git([*origin, "fetch", "-q", "--no-tags", "--no-write-fetch-head", str(bundle),
          f"+refs/heads/{branch}:{QUARANTINE_REF}"], env=env, category=IMPORT_REFUSED,
         reason="the bundle could not be imported")  # fmt: skip
    head = _git([*origin, "rev-parse", "--verify", f"{QUARANTINE_REF}^{{commit}}"], env=env,
                category=IMPORT_FAILED, reason="reading the imported head").strip()  # fmt: skip
    if head != listed_head.split(" ", 1)[0]:
        raise RepoWorkError(IMPORT_REFUSED, "the imported head is not the bundle's head")
    descends = _run([*origin, "merge-base", "--is-ancestor", base, head], env=env)
    if isinstance(descends, str) or descends.returncode != 0:
        raise RepoWorkError(IMPORT_REFUSED, "the branch does not descend from the base")
    commits = _git([*origin, "rev-list", f"{base}..{head}"], env=env, category=IMPORT_FAILED,
                   reason="listing the commits").split()  # fmt: skip
    if not 1 <= len(commits) <= max_commits:
        raise RepoWorkError(IMPORT_REFUSED, "the branch adds more commits than accepted")
    if _git([*origin, "rev-list", "--min-parents=2", f"{base}..{head}"], env=env,
            category=IMPORT_FAILED, reason="looking for merges").strip():  # fmt: skip
        raise RepoWorkError(IMPORT_REFUSED, "the branch contains a merge commit")
    _check_changes(origin, env, commits)
    _check_objects(origin, env, base, head, max_blob_bytes)
    _git([*origin, "update-ref", f"refs/heads/{branch}", head, _ZERO], env=env,
         category=IMPORT_FAILED, reason="recording the branch")  # fmt: skip
    return Imported(head=head, commits=len(commits))


def _check_changes(origin: list[str], env: dict[str, str], commits: list[str]) -> None:
    """Every path an agent commit adds or changes, and its mode."""
    for commit in commits:
        raw = _git([*origin, "diff-tree", "-r", "-z", "--no-renames", "--no-commit-id",
                    "--root", commit], env=env, category=IMPORT_FAILED,
                   reason="reading a commit's changes")  # fmt: skip
        fields = raw.split("\0")
        for i in range(0, len(fields) - 1, 2):
            meta, path = fields[i].split(" "), fields[i + 1]
            if len(meta) < 5:
                raise RepoWorkError(IMPORT_FAILED, "a commit's changes could not be read")
            new_mode, new_sha, status = meta[1], meta[3], meta[4]
            if status.startswith("D"):
                continue
            why = _bad_path(path)
            if why:
                raise RepoWorkError(IMPORT_REFUSED, why)
            if new_mode == _GITLINK_MODE:
                raise RepoWorkError(IMPORT_REFUSED, "a submodule entry is added")
            if new_mode == _SYMLINK_MODE:
                target = _git([*origin, "cat-file", "blob", new_sha], env=env,
                              category=IMPORT_FAILED, reason="reading a link")  # fmt: skip
                resolved = _link_target_path(path, target)
                if resolved is None or _passes_through_a_link(origin, env, commit, resolved):
                    raise RepoWorkError(IMPORT_REFUSED, "a link points outside the repository")


def _check_objects(origin: list[str], env: dict[str, str], base: str, head: str,
                   max_blob_bytes: int) -> None:  # fmt: skip
    listed = _git([*origin, "rev-list", "--objects", "--no-object-names", head, f"^{base}"],
                  env=env, category=IMPORT_FAILED, reason="listing the new objects")  # fmt: skip
    if not listed.strip():
        return
    sizes = _git([*origin, "cat-file", "--batch-check=%(objecttype) %(objectsize)"], env=env,
                 stdin=listed.encode(), category=IMPORT_FAILED,
                 reason="sizing the new objects")  # fmt: skip
    for line in sizes.splitlines():
        kind, _, size = line.partition(" ")
        if kind == "blob" and int(size) > max_blob_bytes:
            raise RepoWorkError(IMPORT_REFUSED, "a file is larger than accepted")
