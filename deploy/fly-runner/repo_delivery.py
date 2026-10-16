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

Path names are raw bytes
------------------------
Every git output that feeds a decision is read NUL-terminated (``-z``) or as
fixed-format object ids, and path names and link targets are parsed and
compared as bytes. Nothing is decoded or unquoted, and no path is passed back
to git as an argument.

Limits, stated plainly
----------------------
* The clone cap (``MAX_ORIGIN_BYTES``) is checked after the fetch: it decides
  whether the clone is accepted, it does not bound peak disk use while the
  fetch runs.
* Paths and links are checked on the entries the agent's commits add or
  change. A changed link's target is checked lexically and, in addition, every
  component it passes through is looked up in the commit's tree: a target that
  passes through any link (one inherited from the base included), or names a
  link, is refused. A changed ordinary path cannot pass through a link: a git
  tree cannot hold a link and a path below it at once.

Errors
------
Every failure an exported function reports is a ``RepoWorkError`` with a
fixed category (``clone_failed``, ``import_refused``, ...), a fixed reason
and, for a git failure, only the class of its exit status. Git's output and
arguments, and the file names and messages of filesystem errors, are never
forwarded, and no error is chained to one that carries them. Nothing is kept
for debugging.

Import contract
---------------
``import_agent_work`` returns ``Imported`` only when the branch is recorded
in ``origin.git`` AND the quarantine ref is confirmed gone. ANY
``RepoWorkError`` means nothing may be pushed, whatever state it reports. If
the quarantine ref cannot be removed after the branch was recorded, the
branch record is deleted and read back: the error says the withdrawal was
confirmed only when the branch was read back absent, and otherwise that it is
unresolved.

Each import attempt copies the agent's bundle to a private file in the job
directory, replacing any copy a previous attempt left, and removes it on every
exit path of the attempt, including a refusal or failure while copying. A
failed removal never replaces the error already being raised; after an
otherwise successful import it is ``import_failed``. ``remove_work`` removes
the copy with everything else.
"""

from __future__ import annotations

import base64
import contextlib
import functools
import json
import os
import re
import shutil
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
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
_OID_RE = re.compile(rb"[0-9a-f]{40}")

MAX_ORIGIN_BYTES = 300 * 1024 * 1024
MAX_BUNDLE_BYTES = 50 * 1024 * 1024
MAX_BLOB_BYTES = 10 * 1024 * 1024
MAX_COMMITS = 50
GIT_TIMEOUT_SECONDS = 300

QUARANTINE_REF = "refs/ateles/incoming"
BUNDLE_NAME = "work.bundle"
IMPORT_COPY_NAME = "import.bundle"
_GITLINK_MODE = b"160000"
_SYMLINK_MODE = b"120000"
_ZERO = "0" * 40

# Error categories: what the caller acts on.
WORK_ROOT_REFUSED = "work_root_refused"
WORK_FAILED = "work_failed"
BRANCH_REFUSED = "branch_refused"
CLONE_FAILED = "clone_failed"
CLONE_REFUSED = "clone_refused"
CHECKOUT_FAILED = "checkout_failed"
EXPORT_FAILED = "export_failed"
IMPORT_FAILED = "import_failed"
IMPORT_REFUSED = "import_refused"
QUARANTINE_FAILED = "quarantine_failed"
PUSH_REFUSED = "push_refused"
PUSH_FAILED = "push_failed"
PR_FAILED = "pr_failed"
NO_TIME = "no_time"
NO_TIME_REASON = "the delivery deadline has passed; the step was not started"
# A git or API call is started only with at least this much time left.
MIN_CALL_SECONDS = 1.0

# Delivery to GitHub: the push target and API. Fixed in code; a job cannot
# name another host. Tests replace them with local stand-ins.
GITHUB_REMOTE = "https://github.com/{repository}.git"
GITHUB_REMOTE_TRANSPORT = "https"
GITHUB_API = "https://api.github.com"
# A release-looking or default-looking branch is never pushed, whatever the
# builder pattern says.
_PROTECTED_BRANCH_RE = re.compile(r"(?i)(?:main|master|trunk|develop|release[/_-].*|releases/.*"
                                  r"|hotfix/.*|v?[0-9]+(?:\.[0-9]+)*)")  # fmt: skip
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}", re.ASCII)
# The agent's pull request text: <export>/pull_request.md, first line the title.
PR_TEXT_NAME = "pull_request.md"
MAX_PR_TEXT_BYTES = 64 * 1024
MAX_PR_TITLE_CHARS = 256
API_TIMEOUT_SECONDS = 30

WITHDRAWAL_CONFIRMED = ("the quarantine ref could not be removed; the branch record was withdrawn "
                        "and read back absent; nothing may be pushed")  # fmt: skip
WITHDRAWAL_UNRESOLVED = ("the quarantine ref could not be removed and the branch record could not "
                         "be confirmed withdrawn; nothing may be pushed")  # fmt: skip


class RepoWorkError(Exception):
    """A step was refused or failed. Carries a fixed category, a fixed reason
    and at most the class of a git exit status; never a value."""

    def __init__(self, category: str, reason: str, status: str = "") -> None:
        self.category, self.reason, self.status = category, reason, status
        super().__init__(f"{category}: {reason}" + (f" [{status}]" if status else ""))


def _boundary(category: str):
    """Exported functions report every failure as ``RepoWorkError``: an
    unexpected filesystem or parsing error becomes *category* with a fixed
    reason, raised OUTSIDE the handler so it is chained to nothing."""

    def wrap(fn):
        @functools.wraps(fn)
        def guarded(*args, **kwargs):
            failed = False
            try:
                return fn(*args, **kwargs)
            except RepoWorkError:
                raise
            except (OSError, ValueError, TypeError, UnicodeError):
                failed = True
            if failed:
                raise RepoWorkError(category, "a filesystem or parsing step failed", "os_error")
            raise AssertionError("unreachable")  # pragma: no cover

        return guarded

    return wrap


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


@_boundary(WORK_ROOT_REFUSED)
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


@_boundary(WORK_FAILED)
def clear_work_root(root: Path) -> None:
    """Delete everything under *root*, never following a link. A refused
    root deletes nothing."""
    _clear(check_work_root(root))


@_boundary(WORK_FAILED)
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


@_boundary(WORK_FAILED)
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
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_ALLOW_PROTOCOL": allow,
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "GIT_CONFIG_COUNT": str(len(settings)),
    }
    for i, (key, value) in enumerate(settings):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


# The wall-clock time by which every git and API call must have finished,
# while a ``bounded_by`` block is active; None outside one.
_DEADLINE: list[float] = []


@contextlib.contextmanager
def bounded_by(deadline: float):
    """Bound every git and API call made inside the block by *deadline*."""
    _DEADLINE.append(deadline)
    try:
        yield
    finally:
        _DEADLINE.pop()


def _call_timeout(limit: float) -> float | None:
    """How long the next git or API call may take, or None when the active
    deadline leaves less than ``MIN_CALL_SECONDS``: the call is then not
    started at all (never a fresh minimum allowance)."""
    if not _DEADLINE:
        return limit
    left = _DEADLINE[-1] - time.time()
    if left < MIN_CALL_SECONDS:
        return None
    return min(limit, left)


def _run(args: list[str], *, env: dict[str, str], ident: tuple[int, int] | None = None,
         stdin: bytes | None = None) -> subprocess.CompletedProcess | str:  # fmt: skip
    """Run git; the completed process, or the class of why it did not run or
    finish (``no_time`` when the deadline leaves no time to start it)."""
    drop = {} if ident is None else {"user": ident[0], "group": ident[1], "extra_groups": []}
    limit = _call_timeout(GIT_TIMEOUT_SECONDS)
    if limit is None:
        return NO_TIME
    outcome: subprocess.CompletedProcess | str = "not_started"
    try:
        outcome = subprocess.run([GIT, *args], env=env, capture_output=True, input=stdin,
                                 timeout=limit, check=False,
                                 **drop)  # fmt: skip
    except subprocess.TimeoutExpired:
        outcome = "timed_out"
    except (OSError, subprocess.SubprocessError):
        outcome = "not_started"
    return outcome


def _require_time() -> None:
    """Refuse to start an external step once the active deadline is spent."""
    if _call_timeout(GIT_TIMEOUT_SECONDS) is None:
        raise RepoWorkError(NO_TIME, NO_TIME_REASON)


def _status_class(done: subprocess.CompletedProcess | str) -> str:
    if isinstance(done, str):
        return done
    return "killed_by_signal" if done.returncode < 0 else "exit_nonzero"


def _git(args: list[str], *, env: dict[str, str], category: str, reason: str,
         ident: tuple[int, int] | None = None, stdin: bytes | None = None) -> bytes:  # fmt: skip
    """Run git and return its raw output; on failure raise *category*/*reason*
    with only the class of the exit status. Git's stderr is discarded."""
    done = _run(args, env=env, ident=ident, stdin=stdin)
    if done == NO_TIME:
        raise RepoWorkError(NO_TIME, NO_TIME_REASON)
    if isinstance(done, str) or done.returncode != 0:
        raise RepoWorkError(category, reason, _status_class(done))
    return done.stdout


def _oid(raw: bytes, category: str, reason: str) -> str:
    """One object id from git's output, or *category*/*reason*."""
    value = raw.strip()
    if not _OID_RE.fullmatch(value):
        raise RepoWorkError(category, reason)
    return value.decode("ascii")


def _ref_present(origin: list[str], env: dict[str, str], ref: str) -> bool | None:
    """True or False when git answered; None when it could not say."""
    check = _run([*origin, "rev-parse", "--verify", "-q", ref], env=env)
    if isinstance(check, str) or check.returncode not in (0, 1):
        return None
    return check.returncode == 0


def _size(path: Path) -> int:
    total = 0
    for dirpath, _, files in os.walk(path):
        for name in files:
            total += os.lstat(os.path.join(dirpath, name)).st_size
    return total


# ── The clone ────────────────────────────────────────────────────────────────


@_boundary(CLONE_FAILED)
def clone_origin(work: Work, *, url: str, base_branch: str, token: str,
                 allow: str = "https", max_bytes: int = MAX_ORIGIN_BYTES) -> str:  # fmt: skip
    """Fetch the base branch (depth 1) into a fresh root-owned ``origin.git``
    and return its commit. The token reaches only the fetching process."""
    _check_work(work)
    if not isinstance(base_branch, str) or not BASE_BRANCH_RE.fullmatch(base_branch) \
            or ".." in base_branch:  # fmt: skip
        raise RepoWorkError(CLONE_REFUSED, "the base branch name is not acceptable")
    _require_time()
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
    return _oid(_git([*origin, "rev-parse", "--verify", f"refs/heads/{base_branch}^{{commit}}"],
                     env=plain, category=CLONE_FAILED, reason="reading the base"),
                CLONE_FAILED, "reading the base")  # fmt: skip


@_boundary(CHECKOUT_FAILED)
def make_checkout(work: Work, *, branch: str, base_branch: str, repository: str,
                  ident: tuple[int, int] | None, author: tuple[str, str]) -> None:  # fmt: skip
    """The agent's checkout of ``origin.git`` on its assigned branch, handed to
    the agent's user with its export directory. Its remote names the plain
    https URL; it carries no credential."""
    _check_work(work)
    check_branch(branch)
    env = git_env(work.home, allow="file")
    checkout = ["-C", str(work.checkout)]
    _require_time()
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


@_boundary(EXPORT_FAILED)
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
    _require_time()
    head = _oid(_git([*checkout, "rev-parse", "--verify", "-q", f"refs/heads/{branch}^{{commit}}"],
                     env=env, ident=ident, category=EXPORT_FAILED,
                     reason="reading the assigned branch"),
                EXPORT_FAILED, "reading the assigned branch")  # fmt: skip
    if head == base:
        return False
    _git([*checkout, "bundle", "create", "-q", str(work.export / BUNDLE_NAME),
          f"refs/heads/{branch}", f"^{base}"], env=env, ident=ident, category=EXPORT_FAILED,
         reason="bundling the agent's work")  # fmt: skip
    return True


def _open_export(work: Work) -> int:
    """The agent's bundle, opened without following a link at either level;
    a descriptor, or a refusal raised outside any handler."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    reason = ""
    dfd = fd = -1
    try:
        dfd = os.open(work.export, os.O_RDONLY | os.O_DIRECTORY | nofollow)
    except OSError:
        reason = "the export directory is missing or is a link"
    if not reason:
        try:
            fd = os.open(BUNDLE_NAME, os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0),
                         dir_fd=dfd)  # fmt: skip
        except OSError:
            reason = "the bundle is missing or is a link"
        finally:
            os.close(dfd)
    if reason:
        raise RepoWorkError(IMPORT_REFUSED, reason)
    return fd


def _copy_export(work: Work, max_bytes: int, copy: Path) -> None:
    """Copy the agent's bundle into *copy*, the job directory's private copy
    (root's, mode 0600), replacing any copy an earlier attempt left. The
    caller removes *copy* on every exit path."""
    fd = _open_export(work)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RepoWorkError(IMPORT_REFUSED, "the bundle is not a regular file")
        _remove(copy)
        out_fd = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(out_fd, "wb") as out:
            total = 0
            while chunk := os.read(fd, 1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise RepoWorkError(IMPORT_REFUSED, "the bundle is larger than accepted")
                out.write(chunk)
    finally:
        os.close(fd)


def _removed(path: Path) -> bool:
    """Remove *path*; True when it is gone. Never raises, so a failed removal
    cannot mask the error that is already propagating."""
    try:
        _remove(path)
    except OSError:
        return False
    return not os.path.lexists(path)


def _bad_path(path: bytes) -> str:
    """Why a path an agent commit adds or changes is refused, or ``""``.
    Compared as bytes; ASCII case folding only."""
    for part in path.split(b"/"):
        folded = part.lower().rstrip(b". ")
        if part in (b"", b".", b"..") or folded in (b".git", b"git~1"):
            return "a path leaves the repository or enters .git"
        if folded == b".gitmodules":
            return "a submodule definition is added"
    return ""


def _link_target_path(path: bytes, target: bytes) -> bytes | None:
    """The repository path (bytes) a link's target names, or None when it is
    absolute, climbs above the root, or has a ``..`` after an ordinary name
    (that name may itself be a link)."""
    if not target or target.startswith(b"/") or b"\0" in target:
        return None
    parts = [p for p in target.split(b"/") if p not in (b"", b".")]
    ups = 0
    while ups < len(parts) and parts[ups] == b"..":
        ups += 1
    rest = parts[ups:]
    base = [p for p in path.split(b"/")[:-1] if p]
    if ups > len(base) or b".." in rest:
        return None
    resolved = b"/".join(base[:len(base) - ups] + rest)
    return None if resolved and _bad_path(resolved) else resolved


def _links_in(origin: list[str], env: dict[str, str], commit: str) -> set[bytes]:
    """Every link path in *commit*'s tree, as raw bytes (``ls-tree -r -z``,
    no path arguments)."""
    raw = _git([*origin, "ls-tree", "-r", "-z", "--full-tree", commit], env=env,
               category=IMPORT_FAILED, reason="reading a commit's tree")  # fmt: skip
    links = set()
    for entry in raw.split(b"\0"):
        meta, tab, name = entry.partition(b"\t")
        if not tab:
            continue
        if meta.split(b" ", 1)[0] == _SYMLINK_MODE:
            links.add(name)
    return links


def _passes_through_a_link(links: set[bytes], resolved: bytes) -> bool:
    """Whether *resolved*, or any directory it passes through, is a link."""
    parts = resolved.split(b"/") if resolved else []
    return any(b"/".join(parts[:i]) in links for i in range(1, len(parts) + 1))


def _drop_quarantine(origin: list[str], env: dict[str, str]) -> bool:
    """Remove the quarantine ref; True only when it is read back absent."""
    for _ in range(2):
        _run([*origin, "update-ref", "-d", QUARANTINE_REF], env=env)
        if _ref_present(origin, env, QUARANTINE_REF) is False:
            return True
    return False


@_boundary(IMPORT_FAILED)
def import_agent_work(work: Work, *, branch: str, base: str,
                      max_bundle_bytes: int = MAX_BUNDLE_BYTES,
                      max_commits: int = MAX_COMMITS,
                      max_blob_bytes: int = MAX_BLOB_BYTES) -> Imported:  # fmt: skip
    """Root's side: check the agent's bundle and, only if every check passes,
    point ``refs/heads/<branch>`` in ``origin.git`` at its head. See the
    module's import contract."""
    _check_work(work)
    check_branch(branch)
    if not isinstance(base, str) or not _OID_RE.fullmatch(base.encode("ascii", "replace")):
        raise RepoWorkError(IMPORT_REFUSED, "the base is not an object id")
    _require_time()
    env = git_env(work.home, allow="file")
    origin = ["-C", str(work.origin)]
    copy = work.job / IMPORT_COPY_NAME
    # The private copy is created inside this try, so it is removed on every
    # exit path: a refusal or failure while copying, an interrupt, or success.
    try:
        _copy_export(work, max_bundle_bytes, copy)
        imported = _import_copy(origin, env, copy, branch, base, max_commits, max_blob_bytes)
    finally:
        removed = _removed(copy)
    if not removed:
        raise RepoWorkError(IMPORT_FAILED, "the private copy of the bundle could not be removed; "
                            "nothing may be pushed")  # fmt: skip
    return imported


def _import_copy(origin: list[str], env: dict[str, str], bundle: Path, branch: str, base: str,
                 max_commits: int, max_blob_bytes: int) -> Imported:  # fmt: skip
    heads = _git([*origin, "bundle", "list-heads", str(bundle)], env=env,
                 category=IMPORT_REFUSED, reason="the bundle could not be read")  # fmt: skip
    lines = [line for line in heads.split(b"\n") if line]
    listed = lines[0].split(b" ", 1) if len(lines) == 1 else []
    if len(listed) != 2 or listed[1] != f"refs/heads/{branch}".encode("ascii") \
            or not _OID_RE.fullmatch(listed[0]):  # fmt: skip
        raise RepoWorkError(IMPORT_REFUSED, "the bundle carries refs other than the assigned branch")
    refused = True
    try:
        imported = _validate_and_record(origin, env, bundle, branch, base,
                                        listed[0].decode("ascii"), max_commits,
                                        max_blob_bytes)  # fmt: skip
        refused = False
    finally:
        if refused:
            _drop_quarantine(origin, env)  # nothing was recorded; nothing may be pushed
    if _drop_quarantine(origin, env):
        return imported
    # Recorded but not clean: delete the branch record and read it back. The
    # error says "withdrawn" only when the branch was read back absent.
    _run([*origin, "update-ref", "-d", f"refs/heads/{branch}", imported.head], env=env)
    gone = _ref_present(origin, env, f"refs/heads/{branch}") is False
    raise RepoWorkError(QUARANTINE_FAILED, WITHDRAWAL_CONFIRMED if gone else WITHDRAWAL_UNRESOLVED)


def _validate_and_record(origin: list[str], env: dict[str, str], bundle: Path, branch: str,
                         base: str, listed_head: str, max_commits: int,
                         max_blob_bytes: int) -> Imported:  # fmt: skip
    _git([*origin, "fetch", "-q", "--no-tags", "--no-write-fetch-head", str(bundle),
          f"+refs/heads/{branch}:{QUARANTINE_REF}"], env=env, category=IMPORT_REFUSED,
         reason="the bundle could not be imported")  # fmt: skip
    head = _oid(_git([*origin, "rev-parse", "--verify", f"{QUARANTINE_REF}^{{commit}}"], env=env,
                     category=IMPORT_FAILED, reason="reading the imported head"),
                IMPORT_FAILED, "reading the imported head")  # fmt: skip
    if head != listed_head:
        raise RepoWorkError(IMPORT_REFUSED, "the imported head is not the bundle's head")
    descends = _run([*origin, "merge-base", "--is-ancestor", base, head], env=env)
    if descends == NO_TIME:
        raise RepoWorkError(NO_TIME, NO_TIME_REASON)
    if isinstance(descends, str) or descends.returncode != 0:
        raise RepoWorkError(IMPORT_REFUSED, "the branch does not descend from the base")
    commits = _git([*origin, "rev-list", f"{base}..{head}"], env=env, category=IMPORT_FAILED,
                   reason="listing the commits").split()  # fmt: skip
    if not all(_OID_RE.fullmatch(c) for c in commits):
        raise RepoWorkError(IMPORT_FAILED, "listing the commits")
    if not 1 <= len(commits) <= max_commits:
        raise RepoWorkError(IMPORT_REFUSED, "the branch adds more commits than accepted")
    if _git([*origin, "rev-list", "--min-parents=2", f"{base}..{head}"], env=env,
            category=IMPORT_FAILED, reason="looking for merges").strip():  # fmt: skip
        raise RepoWorkError(IMPORT_REFUSED, "the branch contains a merge commit")
    _check_changes(origin, env, [c.decode("ascii") for c in commits])
    _check_objects(origin, env, base, head, max_blob_bytes)
    _git([*origin, "update-ref", f"refs/heads/{branch}", head, _ZERO], env=env,
         category=IMPORT_FAILED, reason="recording the branch")  # fmt: skip
    return Imported(head=head, commits=len(commits))


def _check_changes(origin: list[str], env: dict[str, str], commits: list[str]) -> None:
    """Every path an agent commit adds or changes, and its mode, as raw bytes
    from ``diff-tree -z``."""
    for commit in commits:
        raw = _git([*origin, "diff-tree", "-r", "-z", "--no-renames", "--no-commit-id",
                    "--root", commit], env=env, category=IMPORT_FAILED,
                   reason="reading a commit's changes")  # fmt: skip
        fields = raw.split(b"\0")
        if fields and fields[-1] == b"":
            fields.pop()
        if len(fields) % 2:
            raise RepoWorkError(IMPORT_FAILED, "a commit's changes could not be read")
        links: set[bytes] | None = None
        for i in range(0, len(fields), 2):
            meta, path = fields[i].split(b" "), fields[i + 1]
            if len(meta) != 5 or not meta[0].startswith(b":"):
                raise RepoWorkError(IMPORT_FAILED, "a commit's changes could not be read")
            new_mode, new_oid, status = meta[1], meta[3], meta[4]
            if status.startswith(b"D"):
                continue
            why = _bad_path(path)
            if why:
                raise RepoWorkError(IMPORT_REFUSED, why)
            if new_mode == _GITLINK_MODE:
                raise RepoWorkError(IMPORT_REFUSED, "a submodule entry is added")
            if new_mode == _SYMLINK_MODE:
                if not _OID_RE.fullmatch(new_oid):
                    raise RepoWorkError(IMPORT_FAILED, "a commit's changes could not be read")
                target = _git([*origin, "cat-file", "blob", new_oid.decode("ascii")], env=env,
                              category=IMPORT_FAILED, reason="reading a link")  # fmt: skip
                resolved = _link_target_path(path, target)
                if resolved is not None and links is None:
                    links = _links_in(origin, env, commit)
                if resolved is None or _passes_through_a_link(links, resolved):
                    raise RepoWorkError(IMPORT_REFUSED, "a link points outside the repository")


def _check_objects(origin: list[str], env: dict[str, str], base: str, head: str,
                   max_blob_bytes: int) -> None:  # fmt: skip
    listed = _git([*origin, "rev-list", "--objects", "--no-object-names", head, f"^{base}"],
                  env=env, category=IMPORT_FAILED, reason="listing the new objects")  # fmt: skip
    oids = listed.split()
    if not oids:
        return
    if not all(_OID_RE.fullmatch(o) for o in oids):
        raise RepoWorkError(IMPORT_FAILED, "listing the new objects")
    sizes = _git([*origin, "cat-file", "--batch-check=%(objecttype) %(objectsize)"], env=env,
                 stdin=b"\n".join(oids) + b"\n", category=IMPORT_FAILED,
                 reason="sizing the new objects")  # fmt: skip
    rows = sizes.split(b"\n")
    if rows and rows[-1] == b"":
        rows.pop()
    if len(rows) != len(oids):
        raise RepoWorkError(IMPORT_FAILED, "sizing the new objects")
    for row in rows:
        kind, _, size = row.partition(b" ")
        if not size.isdigit():
            raise RepoWorkError(IMPORT_FAILED, "sizing the new objects")
        if kind == b"blob" and int(size) > max_blob_bytes:
            raise RepoWorkError(IMPORT_REFUSED, "a file is larger than accepted")


# ── Push: one explicit refspec, from origin.git only ─────────────────────────


def check_repository(repository: str) -> str:
    if not isinstance(repository, str) or not _REPOSITORY_RE.fullmatch(repository) \
            or repository.split("/", 1)[1] in (".", ".."):  # fmt: skip
        raise RepoWorkError(PUSH_REFUSED, "the repository is not one owner/name")
    return repository


def check_push_branch(branch: str, default_branch: str) -> str:
    """The branch a delivery may push: the builder pattern, never the
    repository's default branch, never a release-looking name."""
    check_branch(branch)
    leaf = branch.split("/", 1)[1]
    if branch == default_branch or _PROTECTED_BRANCH_RE.fullmatch(branch) \
            or _PROTECTED_BRANCH_RE.fullmatch(leaf):  # fmt: skip
        raise RepoWorkError(PUSH_REFUSED, "the branch is a default or release branch")
    return branch


@_boundary(PUSH_FAILED)
def push_branch(work: Work, *, repository: str, branch: str, default_branch: str,
                head: str, token: str) -> None:  # fmt: skip
    """Push exactly ``refs/heads/<branch>`` from ``origin.git`` to GitHub: no
    force, so only a new branch or a fast-forward lands; no tags, no other
    ref. Refused while the quarantine ref exists or when the branch does not
    point at *head*. One retry when git reports a connection-level failure."""
    _check_work(work)
    check_repository(repository)
    check_push_branch(branch, default_branch)
    if not isinstance(head, str) or not _OID_RE.fullmatch(head.encode("ascii", "replace")):
        raise RepoWorkError(PUSH_REFUSED, "the head is not an object id")
    _require_time()
    plain = git_env(work.home, allow="file")
    origin = ["-C", str(work.origin)]
    if _ref_present(origin, plain, QUARANTINE_REF) is not False:
        raise RepoWorkError(PUSH_REFUSED, "the quarantine ref is not confirmed gone")
    recorded = _git([*origin, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"],
                    env=plain, category=PUSH_REFUSED, reason="the branch is not recorded")  # fmt: skip
    if _oid(recorded, PUSH_REFUSED, "the branch is not recorded") != head:
        raise RepoWorkError(PUSH_REFUSED, "the recorded branch is not the imported head")
    env = git_env(work.home, allow=GITHUB_REMOTE_TRANSPORT,
                  config=[("push.followTags", "false"), ("push.recurseSubmodules", "no"),
                          *auth_config(token)])  # fmt: skip
    url = GITHUB_REMOTE.format(repository=repository)
    args = [*origin, "push", "--no-verify", "--quiet", url,
            f"refs/heads/{branch}:refs/heads/{branch}"]  # fmt: skip
    for attempt in range(2):
        done = _run(args, env=env)
        if done == NO_TIME:
            raise RepoWorkError(NO_TIME, NO_TIME_REASON)
        if not isinstance(done, str) and done.returncode == 0:
            return
        if attempt or isinstance(done, str) or done.returncode != 128:
            break
    raise RepoWorkError(PUSH_FAILED, "the push was not accepted", _status_class(done))


# ── The pull request: found by its head branch, else opened ──────────────────


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    action: str  # "opened" or "updated"


def _api(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, object]:
    """One GitHub API call: ``(status, parsed JSON or None)``. Never raises
    for an HTTP or network failure, and never returns a response body as text."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{GITHUB_API}{path}", data=data, method=method,
        headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
                 "User-Agent": "ateles-fly-runner/1.0", "X-GitHub-Api-Version": "2022-11-28",
                 **({"Content-Type": "application/json"} if data else {})},
    )  # fmt: skip
    limit = _call_timeout(API_TIMEOUT_SECONDS)
    if limit is None:
        raise RepoWorkError(NO_TIME, NO_TIME_REASON)
    status, payload = 0, None
    try:
        with urllib.request.urlopen(request, timeout=limit) as resp:
            status, raw = resp.status, resp.read(1024 * 1024)
        payload = json.loads(raw or b"null")
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (urllib.error.URLError, OSError, ValueError):
        status = 0
    return status, payload


def _status_word(status: int) -> str:
    return f"http_{status}" if status else "unreachable"


def _pr_from(payload: object, repository: str, branch: str, default_branch: str) -> PullRequest | None:
    if not isinstance(payload, dict):
        return None
    number, url = payload.get("number"), payload.get("html_url")
    head, base = payload.get("head"), payload.get("base")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        return None
    if not isinstance(url, str) or url != f"https://github.com/{repository}/pull/{number}":
        return None
    if not isinstance(head, dict) or head.get("ref") != branch:
        return None
    repo = head.get("repo")
    if not isinstance(repo, dict) or str(repo.get("full_name", "")).lower() != repository.lower():
        return None
    if not isinstance(base, dict) or base.get("ref") != default_branch:
        return None
    return PullRequest(number=number, url=url, action="")


@_boundary(PR_FAILED)
def open_or_update_pr(*, repository: str, branch: str, default_branch: str, token: str,
                      title: str, body: str) -> PullRequest:  # fmt: skip
    """The open pull request whose head is *branch* (updated with the agent's
    text when it gave one), or a new one against the default branch."""
    check_repository(repository)
    check_push_branch(branch, default_branch)
    owner = repository.split("/", 1)[0]
    _require_time()
    head = urllib.parse.quote(f"{owner}:{branch}", safe="")
    status, found = _api("GET", f"/repos/{repository}/pulls?head={head}&state=open", token)
    if status != 200 or not isinstance(found, list):
        raise RepoWorkError(PR_FAILED, "the open pull requests could not be read",
                            _status_word(status))  # fmt: skip
    if len(found) > 1:
        raise RepoWorkError(PR_FAILED, "more than one open pull request has this head")
    if found:
        existing = _pr_from(found[0], repository, branch, default_branch)
        if existing is None:
            raise RepoWorkError(PR_FAILED, "the open pull request is not this branch's")
        if title:
            status, _ = _api("PATCH", f"/repos/{repository}/pulls/{existing.number}", token,
                             {"title": title, "body": body})  # fmt: skip
            if status != 200:
                raise RepoWorkError(PR_FAILED, "the pull request could not be updated",
                                    _status_word(status))  # fmt: skip
        return PullRequest(number=existing.number, url=existing.url, action="updated")
    status, made = _api("POST", f"/repos/{repository}/pulls", token,
                        {"title": title or f"Changes on {branch}", "head": branch,
                         "base": default_branch, "body": body})  # fmt: skip
    created = _pr_from(made, repository, branch, default_branch) if status == 201 else None
    if created is None:
        raise RepoWorkError(PR_FAILED, "the pull request could not be opened", _status_word(status))
    return PullRequest(number=created.number, url=created.url, action="opened")


@_boundary(PR_FAILED)
def read_pr_text(work: Work) -> tuple[str, str]:
    """The agent's pull request text from ``<export>/pull_request.md``: the
    first line (``# `` stripped) is the title, the rest the body. Read without
    following a link and size-capped. Missing, oversized, not UTF-8, or
    unreadable text gives ``("", "")``: the delivery goes ahead with a
    default title."""
    _check_work(work)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        dfd = os.open(work.export, os.O_RDONLY | os.O_DIRECTORY | nofollow)
        try:
            fd = os.open(PR_TEXT_NAME, os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0),
                         dir_fd=dfd)  # fmt: skip
        finally:
            os.close(dfd)
    except OSError:
        fd = -1
    if fd < 0:
        return "", ""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return "", ""
        raw = os.read(fd, MAX_PR_TEXT_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_PR_TEXT_BYTES:
        return "", ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "", ""
    first, _, rest = text.strip().partition("\n")
    title = first.lstrip("#").strip()[:MAX_PR_TITLE_CHARS]
    return title, rest.strip()
