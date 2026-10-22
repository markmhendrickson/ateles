"""Bounded, value-free cleanup assertions shared by delivery tests only."""

import json
import os
import stat
from pathlib import Path

import pytest


def cleanup_error(exc, *, boundary_error=None):
    # Exception text, filenames and user-defined class names are untrusted.
    kinds = (PermissionError, FileNotFoundError, NotADirectoryError, IsADirectoryError,
             FileExistsError, OSError, ValueError, TypeError, UnicodeError)
    if boundary_error is not None:
        kinds += (boundary_error,)
    kind = next((kind.__name__ for kind in kinds if type(exc) is kind), "Error")
    number = getattr(exc, "errno", None)
    return {"type": kind, "errno": number if type(number) is int else None}


def capture_original_cleanup(root, cleanup, delivery, monkeypatch, causes):
    """Observe fixture cleanup failures before the existing boundary wraps them.

    The original validator, deletion functions and exception behavior remain
    in place. Only fixed metadata is retained; paths and exception text are not.
    """
    guard = delivery.check_work_root
    guard_body = getattr(guard, "__wrapped__", None)
    if not callable(guard_body):
        raise AssertionError("cleanup guard observation is unavailable")
    listdir = delivery.os.listdir
    remove = delivery._remove

    def record(phase, exc):
        if len(causes) >= 3:
            return
        category = getattr(exc, "category", None)
        status = getattr(exc, "status", None)
        causes.append({
            "phase": phase,
            **cleanup_error(exc, boundary_error=delivery.RepoWorkError),
            "category": category if type(category) is str and category in {
                "work_root_refused", "work_failed"} else None,
            "status": status if type(status) is str and status in {
                "os_error", "not_started", "timed_out", "exit_nonzero",
                "killed_by_signal", "no_time"} else None,
        })

    def observed_guard(path):
        try:
            return guard_body(path)
        except (delivery.RepoWorkError, OSError, ValueError, TypeError, UnicodeError) as exc:
            if path == root:
                record("guard", exc)
            raise

    def observed_listdir(path):
        try:
            return listdir(path)
        except OSError as exc:
            if Path(path) == root:
                record("list", exc)
            raise

    def observed_remove(path):
        try:
            return remove(path)
        except (delivery.RepoWorkError, OSError, ValueError, TypeError, UnicodeError) as exc:
            if Path(path).parent == root:
                record("remove", exc)
            raise

    with monkeypatch.context() as scoped:
        # Reuse the SAME production decorator around the SAME validator body:
        # observation happens inside its normalization, not instead of it.
        scoped.setattr(delivery, "check_work_root",
                       delivery._boundary(delivery.WORK_ROOT_REFUSED)(observed_guard))
        scoped.setattr(delivery.os, "listdir", observed_listdir)
        scoped.setattr(delivery, "_remove", observed_remove)
        try:
            return cleanup(root)
        except (delivery.RepoWorkError, OSError, ValueError, TypeError, UnicodeError) as exc:
            if not causes:
                record("cleanup", exc)
            raise


def cleanup_details(root, *, cleanup_errors=()):
    """Bound every assertion probe and inventory behind value-free evidence.

    Never follow a root link (even a dangling one). Unknown relative components,
    exception messages and filenames are untrusted; only fixed metadata escapes.
    """
    known = {"job-fly-deliver-1", "origin.git", "checkout", "export", "home", ".git",
             "objects", "pack", "refs", "heads", "hooks", "logs", "info", "HEAD", "config",
             "index", "packed-refs", "description", "exclude", "shallow"}
    details = {"errors": cleanup_errors[:64], "leftovers": [],
               "truncated": len(cleanup_errors) > 64, "incomplete": False, "root": None}
    rows = details["leftovers"]

    def failure(exc, relative="<entry>"):
        details["incomplete"] = True
        if len(rows) < 64:
            rows.append({"relative": relative, **cleanup_error(exc)})
        else:
            details["truncated"] = True

    def entry(path, relative):
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            failure(exc, relative)
            return None
        row = {"relative": relative, "mode": f"{stat.S_IMODE(mode):04o}",
               "kind": "link" if stat.S_ISLNK(mode) else
               "directory" if stat.S_ISDIR(mode) else "file"}
        return row

    try:
        mode = root.lstat().st_mode
    except FileNotFoundError:
        return details  # Existing acceptance: an absent work root is clean.
    except OSError as exc:
        failure(exc, ".")
        return details
    details["root"] = {"mode": f"{stat.S_IMODE(mode):04o}",
                       "kind": "link" if stat.S_ISLNK(mode) else
                       "directory" if stat.S_ISDIR(mode) else "file"}
    if not stat.S_ISDIR(mode):
        rows.append({"relative": ".", **details["root"]})
        return details
    try:
        if not os.listdir(root):
            return details
        for parent, dirs, files in os.walk(root, followlinks=False, onerror=failure):
            for name in sorted(dirs + files):
                if len(rows) == 64:
                    details["truncated"] = True
                    return details
                path = Path(parent, name)
                relative = "/".join(part if part in known else "<entry>"
                                    for part in path.relative_to(root).parts)
                row = entry(path, relative)
                if row is not None:
                    rows.append(row)
    except OSError as exc:
        failure(exc)
    return details


def assert_cleanup(root, *, cleanup_errors=(), absent=False, present=False):
    details = cleanup_details(root, cleanup_errors=cleanup_errors)
    wrong_root = (absent and details["root"] is not None) or (present and details["root"] is None)
    if details["leftovers"] or details["incomplete"] or wrong_root:
        pytest.fail("delivery cleanup failed: " + json.dumps(details), pytrace=False)

