#!/usr/bin/env python3
"""PreToolUse guard that confines an eval session's file tools to its run dir.

Scenario prompts mention real-sounding things ("Cursor's MCP config"), and a
model that goes looking may reach for the operator's real ``~/.cursor`` or
``~/.claude`` files. The runner already disallows Bash, subagents and web
tools; this hook closes the file tools. Every path-bearing argument of every
file tool the sandbox allows is checked, not only ``file_path``:

  Read, Edit, Write, MultiEdit   file_path
  NotebookEdit                   notebook_path
  Glob                           path, and pattern (a glob, so a location)
  Grep                           path, and glob (a glob); Grep's ``pattern``
                                 is a regex over file CONTENTS, never a location

Plain paths are resolved against the run directory (or the tool's ``path``)
and must stay inside it. Globs are held to a stricter rule, since a wildcard
cannot be resolved statically: no ``~``, no ``..`` component anywhere, no
brace alternative that starts a new absolute path, and the literal prefix
before the first wildcard component must resolve inside the run directory.

Anything else in ``tool_input`` that looks like a location is also checked:
keys naming a path or directory (``path``, ``paths``, ``cwd``, ``dir`` ...),
in any tool, and list values item by item. A tool the guard does not know is
refused, so adding one to the runner's allow-list without teaching the guard
fails closed (and a test pins the two lists together).

A refusal exits 2 and is appended to ``guard_blocks.jsonl`` so a run that
tried to leave the sandbox is visible in the results rather than silently
scored. An unparseable payload is refused as well: the thing this protects
is the operator's real configuration. Security finding
ent_f49706fabf9c726b248b7a80.

Usage (from the eval settings file): ``python3 sandbox_guard.py <run_dir>``.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Path-bearing arguments per tool: (plain path keys, glob keys).
TOOL_PATH_ARGS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "Read": (("file_path",), ()),
    "Edit": (("file_path",), ()),
    "Write": (("file_path",), ()),
    "MultiEdit": (("file_path",), ()),
    "NotebookEdit": (("notebook_path",), ()),
    "Glob": (("path",), ("pattern",)),
    "Grep": (("path",), ("glob",)),
}

# Any other key that names a location is checked as a plain path, whatever the
# tool (a future tool version adding e.g. `cwd` or `paths` fails closed).
_LOCATION_KEY = re.compile(r"(path|dir|cwd|root|file)", re.IGNORECASE)
# Keys that are never locations even though a regex above might match them.
_NOT_LOCATION = {"Grep": {"pattern"}}
_WILDCARD = re.compile(r"[*?\[{]")


class Refused(Exception):
    pass


def _inside(p: Path, run_dir: Path) -> bool:
    resolved = p.resolve()
    return resolved == run_dir or run_dir in resolved.parents


def _check_plain(raw: str, base: Path, run_dir: Path) -> None:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = base / p
    if not _inside(p, run_dir):
        raise Refused(raw)


def _check_glob(raw: str, base: Path, run_dir: Path) -> None:
    if "~" in raw:
        raise Refused(raw)
    parts = re.split(r"[/\\]", raw)
    if ".." in parts:
        raise Refused(raw)
    # A brace alternative that begins a new absolute path, e.g. {/etc,x}/*.
    if re.search(r"[{,]\s*[/\\]", raw):
        raise Refused(raw)
    p = Path(raw)
    if not p.is_absolute():
        p = base / p
    literal: list[str] = []
    for part in p.parts:
        if _WILDCARD.search(part):
            break
        literal.append(part)
    prefix = Path(*literal) if literal else base
    if not _inside(prefix, run_dir):
        raise Refused(raw)


def _values(v) -> list[str]:
    if isinstance(v, str):
        return [v] if v else []
    if isinstance(v, (list, tuple)):
        return [x for x in v if isinstance(x, str) and x]
    return []


def check(tool: str, tool_input: dict, run_dir: Path) -> None:
    """Raise Refused(<argument>) if any location in tool_input leaves run_dir."""
    if tool not in TOOL_PATH_ARGS:
        raise Refused(f"unknown tool {tool!r}")
    plain_keys, glob_keys = TOOL_PATH_ARGS[tool]
    skip = _NOT_LOCATION.get(tool, set())

    base = run_dir
    for key in plain_keys:
        for raw in _values(tool_input.get(key)):
            _check_plain(raw, run_dir, run_dir)
            if key == "path":
                base = (run_dir / Path(raw).expanduser()).resolve()
    for key in glob_keys:
        for raw in _values(tool_input.get(key)):
            _check_glob(raw, base, run_dir)
    for key, value in tool_input.items():
        if key in plain_keys or key in glob_keys or key in skip:
            continue
        if _LOCATION_KEY.search(key):
            for raw in _values(value):
                _check_plain(raw, run_dir, run_dir)


def main() -> int:
    run_dir = Path(sys.argv[1]).resolve()
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool = str(payload.get("tool_name") or "")
        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            raise Refused("tool_input is not an object")
        check(tool, tool_input, run_dir)
    except Refused as exc:
        with (run_dir / "guard_blocks.jsonl").open("a") as fh:
            fh.write(
                json.dumps({"tool": payload.get("tool_name"), "arg": str(exc)}) + "\n"
            )
        sys.stderr.write(
            f"eval sandbox: {exc} is outside this workspace; only files under "
            f"{run_dir} are available.\n"
        )
        return 2
    except Exception as exc:  # noqa: BLE001 — fail closed on anything unexpected
        sys.stderr.write(
            f"eval sandbox: unreadable tool call refused ({type(exc).__name__})\n"
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
