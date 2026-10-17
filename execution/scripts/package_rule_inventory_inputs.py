#!/usr/bin/env python3
"""Package and validate bounded, explicitly selected input files.

The default mode copies the rule-inventory candidate corpus into a data-only tree.
For private evidence, --inventory-root is an owned canonical absolute directory;
--inventory names a relative JSON file with ``roots`` (name -> absolute directory)
and ``files`` (objects containing root name and relative path). --collect-manifest
and --verify-manifest name a relative output/input manifest within inventory-root.
The caller's inventory supplies the input scope for BOTH operations; a stored
manifest cannot add roots or paths. Only ordinary, single-link, owned files are
supported. Root paths must contain no directory aliases; canonicalize a trusted
factory-created root before selecting inputs, rather than resolving selected files.

It never executes a file from the candidate checkout: the candidate
contributes only the fixed repository inputs enumerated here, and the trusted
gate (`run_canonical_rule_inventory_gate.py`) validates the resulting manifest
before the trusted renderer reads the tree as data.

No workflow runs this since ateles#1333 removed the pull_request_target
measurement workflow and its self-hosted job: complete measurement is now a
private milestone audit run locally (`docs/runbooks/rule_inventory_audit.md`).
The packager and gate are kept as the rollback path and for auditing a
candidate tree as data.

Last-modified dates are recorded here, as data
----------------------------------------------
The packaged copies are freshly written, so their filesystem mtime is always
the day of the run -- a "last modified" taken from them measured the packager,
not the corpus. The packager therefore records each file's last git commit
date (committer time, as a UTC calendar date) in the manifest, read from the
candidate checkout's history. The date is informational: the renderer shows
it, and `render_rule_inventory.py --check` excludes it from the equality
comparison (see that script's docstring). A checkout the instrument cannot
date -- not a git work tree, not the tree's top level, or a shallow clone,
whose single grafted commit would claim to have touched every file -- records
`null` rather than a wrong date.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from collections.abc import Iterable
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath

MANIFEST = ".canonical-rule-inventory-inputs.json"
MANIFEST_SCHEMA = 2
RECORD_KEYS = frozenset({"path", "sha256", "size", "last_commit_date"})
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
EXACT_INPUTS = frozenset(
    {
        "CLAUDE.md",
        "docs/foundation/rule_inventory.md",
        "lib/daemon_runtime/agent_loader.py",
    }
)
MAX_FILES = 256
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_ROOTS = 8
MAX_COMPONENTS = 64
BOUNDARY_REASONS = frozenset(
    {
        "destination_exists",
        "invalid_root",
        "malformed_manifest",
        "missing_exact_inputs",
        "oversized",
        "path_escape",
        "symlink",
        "undeclared_path",
        "changed_input",
        "invalid_inventory",
        "unsupported_platform",
    }
)


class InputBoundaryError(Exception):
    """A candidate data tree crossed the fixed measurement boundary."""

    def __init__(self, reason: str) -> None:
        if reason not in BOUNDARY_REASONS:
            raise ValueError("unknown canonical input-boundary reason")
        self.reason = reason
        super().__init__(reason)


def _allowed(relative: str) -> bool:
    path = PurePosixPath(relative)
    parts = path.parts
    return (
        relative in EXACT_INPUTS
        or (
            len(parts) == 4
            and parts[:2] == (".claude", "skills")
            and parts[3] == "SKILL.md"
        )
        or (
            len(parts) == 3
            and parts[:2] == (".claude", "hooks")
            and path.suffix == ".py"
        )
    )


def _relative_parts(relative: str) -> tuple[str, ...]:
    if not isinstance(relative, str) or not relative or "\\" in relative or "\0" in relative:
        raise InputBoundaryError("path_escape")
    try:
        relative.encode("utf-8")
    except UnicodeError:
        raise InputBoundaryError("path_escape") from None
    path = PurePosixPath(relative)
    if (path.is_absolute() or path.as_posix() != relative
            or any(part in {".", ".."} for part in path.parts)
            or not path.parts or len(path.parts) > MAX_COMPONENTS):
        raise InputBoundaryError("path_escape")
    return path.parts


def _flags(*, directory: bool = False) -> int:
    if not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_DIRECTORY")):
        raise InputBoundaryError("unsupported_platform")
    return (os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            | getattr(os, "O_CLOEXEC", 0) | (os.O_DIRECTORY if directory else 0))


def _open_error(exc: OSError) -> InputBoundaryError:
    reason = ("symlink" if exc.errno in {errno.ELOOP, errno.ENOTDIR}
              else "missing_exact_inputs" if exc.errno == errno.ENOENT else "invalid_root")
    return InputBoundaryError(reason)


def _identity(info) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _unchanged(chain) -> None:
    for parent, name, expected in chain:
        try:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError:
            raise InputBoundaryError("changed_input") from None
        if _identity(current) != expected or stat.S_ISLNK(current.st_mode):
            raise InputBoundaryError("changed_input")


@contextmanager
def _directory_root(root: Path):
    """Hold the root and every ancestor descriptor, without resolving links."""
    if (not root.is_absolute() or ".." in root.parts or "\0" in str(root)
            or len(root.parts) > MAX_COMPONENTS):
        raise InputBoundaryError("invalid_root")
    try:
        str(root).encode("utf-8")
    except UnicodeError:
        raise InputBoundaryError("invalid_root") from None
    opened = []
    chain = []
    try:
        fd = os.open(root.anchor, _flags(directory=True))
        opened.append(fd)
        for part in root.parts[1:]:
            child = os.open(part, _flags(directory=True), dir_fd=fd)
            opened.append(child)
            chain.append((fd, part, _identity(os.fstat(child))))
            fd = child
        if os.fstat(fd).st_uid != os.geteuid():
            raise InputBoundaryError("invalid_root")
        _unchanged(chain)
        yield fd, chain
    except OSError as exc:
        raise _open_error(exc) from None
    finally:
        for fd in reversed(opened):
            os.close(fd)


@contextmanager
def _relative_parent(root_fd: int, relative: str, *, create: bool = False):
    parts = _relative_parts(relative)
    opened = []
    chain = []
    fd = root_fd
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, _flags(directory=True), dir_fd=fd)
            opened.append(child)
            info = os.fstat(child)
            if info.st_uid != os.geteuid():
                raise InputBoundaryError("invalid_root")
            chain.append((fd, part, _identity(info)))
            fd = child
        _unchanged(chain)
        yield fd, parts[-1], chain
    except OSError as exc:
        raise _open_error(exc) from None
    finally:
        for fd in reversed(opened):
            os.close(fd)


def _read_descriptor(root_fd: int, relative: str, root_chain, *, limit: int) -> bytes:
    with _relative_parent(root_fd, relative) as (parent, name, chain):
        fd = None
        try:
            _unchanged(root_chain)
            fd = os.open(name, _flags(), dir_fd=parent)
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_uid != os.geteuid()):
                raise InputBoundaryError("invalid_root")
            if before.st_size > limit:
                raise InputBoundaryError("oversized")
            leaf = [(parent, name, _identity(before))]
            _unchanged(root_chain + chain + leaf)
            chunks = []
            remaining = limit + 1
            while remaining:
                data = os.read(fd, min(65536, remaining))
                if not data:
                    break
                chunks.append(data)
                remaining -= len(data)
            data = b"".join(chunks)
            after = os.fstat(fd)
            def stamp(info):
                return (_identity(info), info.st_size, info.st_mtime_ns,
                        info.st_ctime_ns, info.st_nlink, info.st_uid)
            _unchanged(root_chain + chain + leaf)
            if stamp(before) != stamp(after):
                raise InputBoundaryError("changed_input")
            if len(data) > limit:
                raise InputBoundaryError("oversized")
            return data
        except OSError as exc:
            raise _open_error(exc) from None
        finally:
            if fd is not None:
                os.close(fd)


def _read_rooted(root: Path, relative: str, *, limit: int = MAX_FILE_BYTES) -> bytes:
    _relative_parts(relative)
    with _directory_root(root) as (fd, chain):
        return _read_descriptor(fd, relative, chain, limit=limit)


def _write_rooted(root: Path, relative: str, data: bytes, *, limit: int | None = None) -> None:
    """Publish only complete output through its held no-follow directory."""
    if len(data) > (MAX_FILE_BYTES if limit is None else limit):
        raise InputBoundaryError("oversized")
    with _directory_root(root) as (root_fd, root_chain):
        _write_descriptor(root_fd, root_chain, relative, data)


def _write_descriptor(root_fd, root_chain, relative, data):
    """Write through the same held root used for namespace admission."""
    with _relative_parent(root_fd, relative, create=True) as (parent, name, chain):
        temporary = ".input-" + uuid.uuid4().hex
        fd = None
        created_identity = None
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=parent)
            created_identity = _identity(os.fstat(fd))
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise InputBoundaryError("invalid_root")
                view = view[written:]
            os.fsync(fd)
            info = os.fstat(fd)
            if info.st_nlink != 1 or info.st_uid != os.geteuid():
                raise InputBoundaryError("changed_input")
            _unchanged(root_chain + chain + [(parent, temporary, created_identity)])
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            _unchanged(root_chain + chain + [(parent, name, created_identity)])
        except OSError as exc:
            raise _open_error(exc) from None
        finally:
            if fd is not None:
                os.close(fd)
            if created_identity is not None:
                try:
                    info = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
                    if _identity(info) == created_identity:
                        os.unlink(temporary, dir_fd=parent)
                    else:
                        raise InputBoundaryError("changed_input")
                except OSError as exc:
                    if exc.errno != errno.ENOENT:
                        raise _open_error(exc) from None


def _candidate_paths(source: Path) -> list[Path]:
    paths = [source / relative for relative in sorted(EXACT_INPUTS)]
    count = 0
    with _directory_root(source) as (root_fd, root_chain):
        for store in (".claude/skills", ".claude/hooks"):
            with ExitStack() as selected:
                try:
                    fd, _, chain = selected.enter_context(_relative_parent(root_fd, store + "/entry"))
                except InputBoundaryError as exc:
                    if exc.reason == "missing_exact_inputs":
                        continue
                    raise
                try:
                    with os.scandir(fd) as entries:
                        for entry in entries:
                            count += 1
                            if count > MAX_FILES:
                                raise InputBoundaryError("oversized")
                            if store.endswith("skills"):
                                info = entry.stat(follow_symlinks=False)
                                if stat.S_ISLNK(info.st_mode):
                                    raise InputBoundaryError("symlink")
                                if not stat.S_ISDIR(info.st_mode):
                                    continue
                                relative = store + "/" + entry.name + "/SKILL.md"
                                with _relative_parent(root_fd, relative) as (parent, name, nested):
                                    try:
                                        os.stat(name, dir_fd=parent, follow_symlinks=False)
                                    except FileNotFoundError:
                                        continue
                                    _unchanged(nested)
                                paths.append(source / relative)
                            elif entry.name.endswith(".py"):
                                paths.append(source / store / entry.name)
                    _unchanged(chain)
                except OSError as exc:
                    raise _open_error(exc) from None
        _unchanged(root_chain)
    return sorted(paths)


def _read_regular_file(root: Path, path: Path, *, limit: int | None = None) -> tuple[str, bytes]:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise InputBoundaryError("path_escape") from exc
    if not _allowed(relative):
        raise InputBoundaryError("undeclared_path")
    return relative, _read_rooted(root, relative,
                                  limit=MAX_FILE_BYTES if limit is None else min(MAX_FILE_BYTES, limit))



def read_repository_inputs(root: Path) -> dict[str, bytes]:
    """Acquire the bounded repository corpus once, through the shared reader.

    Local rendering permits absent optional inputs; present inputs still have
    exactly the same custody requirements as a packaged candidate.
    """
    contents = {}
    total = 0
    with _directory_root(root) as (fd, chain):
        for path in _candidate_paths(root):
            if len(contents) >= MAX_FILES:
                raise InputBoundaryError("oversized")
            relative = path.relative_to(root).as_posix()
            try:
                data = _read_descriptor(fd, relative, chain,
                                        limit=min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - total))
            except InputBoundaryError as exc:
                if exc.reason == "missing_exact_inputs" and relative in EXACT_INPUTS:
                    continue
                raise
            total += len(data)
            contents[relative] = data
        _unchanged(chain)
    return contents


def _is_iso_date(value: object) -> bool:
    if not isinstance(value, str) or not ISO_DATE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def git_last_commit_dates(
    source: Path, relatives: Iterable[str]
) -> dict[str, str | None]:
    """Return each file's last git commit date (UTC, ISO) or None.

    One `git log` pass over the declared paths. `None` wherever the history
    cannot answer: the source is not the top level of a git work tree, the
    clone is shallow (its grafted root would date every file to the head
    commit), or the file has no commit. Never raises: a date is informational,
    and an unanswerable date is reported as absent rather than guessed.
    """
    dates: dict[str, str | None] = dict.fromkeys(sorted(set(relatives)))
    if not dates:
        return dates
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "--literal-pathspecs", "-C", str(source), *args],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            check=False,
        )

    try:
        identity = git("rev-parse", "--show-toplevel", "--is-shallow-repository")
        lines = identity.stdout.splitlines()
        if (
            identity.returncode != 0
            or len(lines) != 2
            or Path(lines[0]).resolve() != source.resolve()
            or lines[1].strip() != "false"
        ):
            return dates
        log = git(
            "-c",
            "core.quotePath=false",
            "log",
            "--no-renames",
            "--format=%x00%ct",
            "--name-only",
            "--",
            *dates,
        )
        if log.returncode != 0:
            return dates
    except (OSError, subprocess.SubprocessError):
        return dates
    latest: dict[str, int] = {}
    stamp: int | None = None
    for line in log.stdout.splitlines():
        if line.startswith("\x00"):
            try:
                stamp = int(line[1:])
            except ValueError:
                stamp = None
            continue
        if stamp is not None and line in dates:
            latest[line] = max(latest.get(line, stamp), stamp)
    for relative, seconds in latest.items():
        dates[relative] = (
            datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()
        )
    return dates


def recorded_commit_dates(root: Path) -> dict[str, str]:
    """Read the commit dates a packaged tree's manifest recorded.

    Lenient by design: the privileged gate has already validated the manifest
    with `validate_inputs`, and a date is informational, so anything unreadable
    here yields no date rather than an error.
    """
    try:
        manifest = _json_input(root, MANIFEST)
    except (InputBoundaryError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        return {}
    records = manifest.get("files")
    if not isinstance(records, list):
        return {}
    return {
        record["path"]: record["last_commit_date"]
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get("path"), str)
        and _is_iso_date(record.get("last_commit_date"))
    }


def package_inputs(source: Path, destination: Path) -> None:
    if not source.is_absolute() or not source.is_dir():
        raise InputBoundaryError("invalid_root")
    if source.is_symlink():
        raise InputBoundaryError("symlink")
    if destination.exists():
        raise InputBoundaryError("destination_exists")
    records: list[dict[str, object]] = []
    contents: list[tuple[str, bytes]] = []
    total = 0
    for path in _candidate_paths(source):
        if len(records) >= MAX_FILES:
            raise InputBoundaryError("oversized")
        relative, data = _read_regular_file(source, path, limit=MAX_TOTAL_BYTES - total)
        total += len(data)
        if len(records) >= MAX_FILES or total > MAX_TOTAL_BYTES:
            raise InputBoundaryError("oversized")
        contents.append((relative, data))
        records.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        )
    if not EXACT_INPUTS.issubset({record["path"] for record in records}):
        raise InputBoundaryError("missing_exact_inputs")
    commit_dates = git_last_commit_dates(
        source, (str(record["path"]) for record in records)
    )
    for record in records:
        record["last_commit_date"] = commit_dates.get(str(record["path"]))
    manifest = {"schema": MANIFEST_SCHEMA, "files": records}
    try:
        with _directory_root(destination.parent) as (parent, chain):
            _unchanged(chain)
            os.mkdir(destination.name, mode=0o700, dir_fd=parent)
        for relative, data in contents:
            _write_rooted(destination, relative, data)
        _write_rooted(destination, MANIFEST,
                      (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode(),
                      limit=MAX_MANIFEST_BYTES)
    except OSError as exc:
        raise InputBoundaryError("invalid_root") from exc


def validate_inputs(root: Path) -> None:
    if not root.is_absolute() or not root.is_dir():
        raise InputBoundaryError("invalid_root")
    if root.is_symlink():
        raise InputBoundaryError("symlink")
    try:
        manifest = _json_input(root, MANIFEST)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InputBoundaryError("malformed_manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise InputBoundaryError("malformed_manifest")
    records = manifest.get("files")
    if not isinstance(records, list) or len(records) > MAX_FILES:
        reason = "oversized" if isinstance(records, list) else "malformed_manifest"
        raise InputBoundaryError(reason)
    expected: set[str] = set()
    total = 0
    for record in records:
        if not isinstance(record, dict) or set(record) != RECORD_KEYS:
            raise InputBoundaryError("malformed_manifest")
        commit_date = record["last_commit_date"]
        if commit_date is not None and not _is_iso_date(commit_date):
            raise InputBoundaryError("malformed_manifest")
        relative = record["path"]
        digest = record["sha256"]
        size = record["size"]
        if not isinstance(relative, str) or relative in expected:
            raise InputBoundaryError("malformed_manifest")
        if not _allowed(relative):
            raise InputBoundaryError("undeclared_path")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or not isinstance(size, int)
            or size < 0
        ):
            raise InputBoundaryError("malformed_manifest")
        if size > min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - total):
            raise InputBoundaryError("oversized")
        candidate = root / relative
        _, data = _read_regular_file(root, candidate, limit=MAX_TOTAL_BYTES - total)
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise InputBoundaryError("malformed_manifest")
        expected.add(relative)
        total += size
    if total > MAX_TOTAL_BYTES:
        raise InputBoundaryError("oversized")
    if not EXACT_INPUTS.issubset(expected):
        raise InputBoundaryError("missing_exact_inputs")
    with _directory_root(root) as (fd, chain):
        actual = _tree_files(fd)
        _unchanged(chain)
    declared = expected | {MANIFEST}
    if actual - declared:
        raise InputBoundaryError("undeclared_path")
    if declared - actual:
        raise InputBoundaryError("missing_exact_inputs")


def _tree_files(root_fd: int) -> set[str]:
    """Bounded metadata inventory, with no content reads or link traversal."""
    actual: set[str] = set()
    count = 0

    def walk(fd, prefix):
        nonlocal count
        with os.scandir(fd) as entries:
            for entry in entries:
                count += 1
                if count > (MAX_FILES + 1) * MAX_COMPONENTS:
                    raise InputBoundaryError("oversized")
                relative = prefix + entry.name
                _relative_parts(relative)
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise InputBoundaryError("symlink")
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(entry.name, _flags(directory=True), dir_fd=fd)
                    try:
                        if _identity(os.fstat(child)) != _identity(info):
                            raise InputBoundaryError("changed_input")
                        walk(child, relative + "/")
                        _unchanged([(fd, entry.name, _identity(info))])
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode):
                    actual.add(relative)
                    if len(actual) > MAX_FILES + 1:
                        raise InputBoundaryError("oversized")
                else:
                    raise InputBoundaryError("invalid_root")

    try:
        walk(root_fd, "")
    except OSError as exc:
        raise _open_error(exc) from None
    return actual


def _json_object(pairs):
    data = dict(pairs)
    if len(data) != len(pairs):
        raise InputBoundaryError("malformed_manifest")
    return data


def _json_input(root: Path, relative: str):
    with _directory_root(root) as (fd, chain):
        return _json_descriptor(fd, chain, relative)


def _json_descriptor(fd, chain, relative):
    try:
        return json.loads(_read_descriptor(fd, relative, chain, limit=MAX_MANIFEST_BYTES).decode("utf-8"),
                          object_pairs_hook=_json_object)
    except (UnicodeError, ValueError, RecursionError):
        raise InputBoundaryError("malformed_manifest") from None



def _namespace_identity(root_fd, relative, root_chain):
    """Metadata only: recognize existing aliases without opening their content."""
    try:
        with _relative_parent(root_fd, relative) as (parent, name, chain):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            _unchanged(root_chain + chain)
            return _identity(info)
    except FileNotFoundError:
        return None
    except InputBoundaryError as exc:
        if exc.reason == "missing_exact_inputs":
            return None
        raise
    except OSError as exc:
        raise _open_error(exc) from None


@contextmanager
def _packet_inputs(inventory_root: Path, inventory: str, manifest: str):
    _relative_parts(manifest)
    _relative_parts(inventory)
    if manifest == inventory:
        raise InputBoundaryError("invalid_inventory")
    with _directory_root(inventory_root) as (inventory_fd, inventory_chain):
        data = _json_descriptor(inventory_fd, inventory_chain, inventory)
        with _packet_namespace(inventory_root, inventory, manifest, data,
                               inventory_fd, inventory_chain) as admitted:
            yield admitted
        _unchanged(inventory_chain)


@contextmanager
def _packet_namespace(inventory_root, inventory, manifest, data, inventory_fd, inventory_chain):
    if not isinstance(data, dict) or set(data) != {"roots", "files"}:
        raise InputBoundaryError("invalid_inventory")
    roots, files = data["roots"], data["files"]
    if (not isinstance(roots, dict) or not 0 < len(roots) <= MAX_ROOTS
            or not isinstance(files, list) or not 0 < len(files) <= MAX_FILES):
        raise InputBoundaryError("invalid_inventory")
    for name, path in roots.items():
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
                or not isinstance(path, str) or not path or "\0" in path):
            raise InputBoundaryError("invalid_inventory")
    seen = set()
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"root", "path"}:
            raise InputBoundaryError("invalid_inventory")
        name, relative = entry["root"], entry["path"]
        if not isinstance(name, str) or name not in roots:
            raise InputBoundaryError("invalid_inventory")
        _relative_parts(relative)
        key = (name, relative)
        if key in seen or (Path(roots[name]) == inventory_root and relative == manifest):
            raise InputBoundaryError("invalid_inventory")
        seen.add(key)
    with ExitStack() as stack:
        opened = {name: stack.enter_context(_directory_root(Path(path)))
                  for name, path in roots.items()}
        # Compare the complete output namespace before reading selected content.
        # Canonical roots may overlap even when their inventory names differ.
        output = inventory_root / manifest
        protected = [inventory_root / inventory] + [
            Path(roots[entry["root"]]) / entry["path"] for entry in files
        ]
        if output in protected:
            raise InputBoundaryError("invalid_inventory")
        out_fd, out_chain = inventory_fd, inventory_chain
        output_identity = _namespace_identity(out_fd, manifest, out_chain)
        for entry in files:
            fd, chain = opened[entry["root"]]
            if (output_identity is not None and output_identity ==
                    _namespace_identity(fd, entry["path"], chain)):
                raise InputBoundaryError("invalid_inventory")
        if (output_identity is not None and output_identity ==
                _namespace_identity(out_fd, inventory, out_chain)):
            raise InputBoundaryError("invalid_inventory")
        yield files, opened, (inventory_fd, inventory_chain)
        for _, chain in opened.values():
            _unchanged(chain)


def _packet_records(files, roots):
    total = 0
    records = []
    for entry in files:
        fd, chain = roots[entry["root"]]
        data = _read_descriptor(fd, entry["path"], chain,
                                limit=min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - total))
        total += len(data)
        records.append({**entry, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return records


def collect_packet(inventory_root: Path, inventory: str, manifest: str) -> None:
    """Hash only declared root-relative files; inventory supplies the authority."""
    with _packet_inputs(inventory_root, inventory, manifest) as (files, roots, (fd, chain)):
        records = _packet_records(files, roots)
        output = {"kind": "explicit-inputs", "schema": 1, "files": records}
        data = (json.dumps(output, sort_keys=True) + "\n").encode()
        if len(data) > MAX_MANIFEST_BYTES:
            raise InputBoundaryError("oversized")
        _write_descriptor(fd, chain, manifest, data)


def verify_packet(inventory_root: Path, inventory: str, manifest: str) -> None:
    """A stored manifest cannot grant new roots or paths during verification."""
    with _packet_inputs(inventory_root, inventory, manifest) as (files, roots, (fd, chain)):
        data = _json_descriptor(fd, chain, manifest)
        if (not isinstance(data, dict) or set(data) != {"kind", "schema", "files"}
                or data["kind"] != "explicit-inputs" or type(data["schema"]) is not int or data["schema"] != 1
                or not isinstance(data["files"], list) or len(data["files"]) != len(files)):
            raise InputBoundaryError("malformed_manifest")
        for expected, record in zip(files, data["files"]):
            if (not isinstance(record, dict) or set(record) != {"root", "path", "size", "sha256"}
                    or any(record[key] != expected[key] for key in expected)
                    or type(record["size"]) is not int or not 0 <= record["size"] <= MAX_FILE_BYTES
                    or not isinstance(record["sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])):
                raise InputBoundaryError("malformed_manifest")
        if _packet_records(files, roots) != data["files"]:
            raise InputBoundaryError("changed_input")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--validate", type=Path)
    parser.add_argument("--inventory-root", type=Path,
                        help="owned canonical absolute root for the trusted inventory")
    parser.add_argument("--inventory", help="root-relative trusted inventory JSON")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--collect-manifest", help="root-relative output manifest")
    operation.add_argument("--verify-manifest", help="root-relative manifest to verify")
    args = parser.parse_args()
    packet_mode = any(value is not None for value in (args.inventory_root, args.inventory,
                                                     args.collect_manifest, args.verify_manifest))
    label = "explicit input" if packet_mode else "canonical rule inventory input"
    try:
        if packet_mode:
            if (args.inventory_root is None or args.inventory is None
                    or (args.collect_manifest is None and args.verify_manifest is None)
                    or any(value is not None for value in (args.source, args.destination, args.validate))):
                raise InputBoundaryError("invalid_inventory")
            function = collect_packet if args.collect_manifest is not None else verify_packet
            function(args.inventory_root, args.inventory,
                     args.collect_manifest if args.collect_manifest is not None else args.verify_manifest)
        elif args.validate is not None:
            if args.source is not None or args.destination is not None:
                raise InputBoundaryError("invalid_root")
            validate_inputs(args.validate)
        else:
            if args.source is None or args.destination is None:
                raise InputBoundaryError("invalid_root")
            package_inputs(args.source, args.destination)
    except InputBoundaryError as exc:
        print(
            f"{label} boundary rejected reason={exc.reason}",
            file=sys.stderr,
        )
        return 2
    print(f"{label} boundary verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
