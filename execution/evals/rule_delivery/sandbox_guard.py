#!/usr/bin/env python3
"""PreToolUse guard that confines an eval session's file tools to its run dir.

Scenario prompts mention real-sounding things ("Cursor's MCP config"), and a
model that goes looking may reach for the operator's real ``~/.cursor`` or
``~/.claude`` files. The runner already disallows Bash, subagents and web
tools; this hook closes the file tools: any path argument that resolves
outside the run directory is refused (exit 2), and the refusal is counted by
the runner as a ``guard_block`` so a run that tried to leave the sandbox is
visible in the results rather than silently scored.

Usage (from the eval settings file): ``python3 sandbox_guard.py <run_dir>``.
Fails CLOSED: an unparseable payload is refused, since the thing it protects
is the operator's real configuration.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PATH_KEYS = ("file_path", "path", "notebook_path")


def main() -> int:
    run_dir = Path(sys.argv[1]).resolve()
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        sys.stderr.write("eval sandbox: unreadable tool payload refused\n")
        return 2
    tool_input = payload.get("tool_input") or {}
    for key in PATH_KEYS:
        raw = tool_input.get(key)
        if not raw:
            continue
        p = Path(str(raw)).expanduser()
        if not p.is_absolute():
            p = run_dir / p
        resolved = p.resolve()
        if resolved != run_dir and run_dir not in resolved.parents:
            with (run_dir / "guard_blocks.jsonl").open("a") as fh:
                fh.write(
                    json.dumps({"tool": payload.get("tool_name"), "path": str(raw)})
                    + "\n"
                )
            sys.stderr.write(
                f"eval sandbox: {raw} is outside this workspace; only files under "
                f"{run_dir} are available.\n"
            )
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
