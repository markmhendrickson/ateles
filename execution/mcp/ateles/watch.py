#!/usr/bin/env python3
"""Block until a watched swarm task changes, print a short summary, exit.

The background half of ateles#1275's "watch task X": a Claude Code session runs
this with `run_in_background`, and the harness tells the session when it exits
— the same completion notice a harness subagent gives its parent. It is the
`watch_swarm` MCP tool's own code (imported from server.py, not copied), so the
two can never disagree about what counts as a change.

    .mcp-venv/bin/python execution/mcp/ateles/watch.py --task ent_... [--task ent_...]
        [--checkpoints] [--until change|terminal] [--timeout 3600] [--cursor C]

Read-only: it reads Neotoma and writes nothing.

Exit codes:
  0  a matching change was seen, or the timeout passed (the output says which)
  2  bad arguments
  3  the record could not be read, three times running

The last line printed is always `cursor: <cursor>`, so the session can pick up
exactly where the watcher stopped (`watch_swarm(cursor=...)`, or `--cursor`).
Use the interpreter the MCP server uses (`.mcp-venv`): this imports server.py,
which needs `mcp` and `httpx`.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

MAX_CONSECUTIVE_ERRORS = 3


def _load_neotoma_env() -> None:
    """Credentials the way run_ateles_mcp.sh provides them to the server.

    The wrapper is shell and cannot be imported, so its two rules are restated
    here: existing environment wins over ~/.config/neotoma/.env (or
    NEOTOMA_ENV_FILE), and NEOTOMA_BEARER_TOKEN_PROD is promoted when the base
    URL is remote. Must run before server.py is imported, since the server reads
    its token at import time.
    """
    env_file = Path(
        os.environ.get("NEOTOMA_ENV_FILE", str(Path.home() / ".config" / "neotoma" / ".env"))
    )
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if value[:1] in ("'", '"') and value[-1:] == value[:1]:
                value = value[1:-1]
            elif " #" in value:
                value = value.split(" #", 1)[0].strip()
            if key.isidentifier() and not os.environ.get(key):
                os.environ[key] = value
    base = os.environ.get("NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com")
    host = (urlsplit(base).hostname or "").lower()
    local = (
        not host
        or host in ("localhost", "127.0.0.1", "0.0.0.0", "::1")
        or host.endswith((".local", ".localhost"))
        or host.startswith(("10.", "192.168.", "169.254."))
    )
    prod = os.environ.get("NEOTOMA_BEARER_TOKEN_PROD", "").strip()
    if not local and prod:
        os.environ["NEOTOMA_BEARER_TOKEN"] = prod


def _server():
    import server

    return server


def _short(entity_id: str | None) -> str:
    return str(entity_id or "?")


def _describe(change: dict, previous: dict[str, str]) -> str:
    subject = change.get("subject") or {}
    kind = change.get("kind")
    at = change.get("at")
    by = f" (by {change['by']})" if change.get("by") else ""
    if "checkpoint" in subject:
        return (
            f"checkpoint {_short(subject['checkpoint'])} on task {_short(subject.get('task'))} "
            f"{kind}: {change.get('value')}{by} at {at} (raised {change.get('raised_at')})"
        )
    task = _short(subject.get("task"))
    if kind == "status":
        before = previous.get(task)
        arrow = f"{before} -> {change.get('value')}" if before else str(change.get("value"))
        return f"{task} status: {arrow}{by} at {at}"
    return f"{task} {kind}: {change.get('value')}{by} at {at}"


def _matches(result: dict, until: str) -> bool:
    if until == "terminal":
        return bool(result.get("terminal_subjects"))
    return bool(result.get("changes") or result.get("checkpoints"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="watch.py",
        description="Block until a watched swarm task changes, then print what changed.",
    )
    parser.add_argument("--task", action="append", default=[], help="task entity id (repeatable)")
    parser.add_argument(
        "--checkpoints", action="store_true", help="also stop on any checkpoint raised or resolved"
    )
    parser.add_argument(
        "--until",
        choices=("change", "terminal"),
        default="change",
        help="stop on any change (default) or only when a watched task reaches a terminal status",
    )
    parser.add_argument("--timeout", type=float, default=3600, help="seconds before giving up (default 3600)")
    parser.add_argument("--cursor", default=None, help="resume from a cursor a previous run printed")
    parser.add_argument(
        "--no-env-file", action="store_true", help="do not read ~/.config/neotoma/.env (tests)"
    )
    args = parser.parse_args(argv)
    if not args.task and not args.checkpoints:
        parser.error("pass at least one --task, or --checkpoints")
    if args.until == "terminal" and not args.task:
        parser.error("--until terminal needs at least one --task")

    if not args.no_env_file:
        _load_neotoma_env()
    srv = _server()

    previous: dict[str, str] = {}
    cursor = args.cursor
    errors = 0
    started = srv._watch_clock()

    def say(line: str) -> None:
        print(f"[ateles-watch] {line}", flush=True)

    while True:
        elapsed = srv._watch_clock() - started
        remaining = args.timeout - elapsed
        if cursor and remaining <= 0:
            say(f"no {'terminal status' if args.until == 'terminal' else 'change'} within {args.timeout:g}s")
            print(f"cursor: {cursor}", flush=True)
            return 0
        result = srv._watch_swarm(
            cursor,
            args.task,
            args.checkpoints,
            0 if not cursor else min(srv.WATCH_MAX_WAIT_SECONDS, max(0.0, remaining)),
        )
        if "error" in result:
            errors += 1
            say(f"read failed ({errors}/{MAX_CONSECUTIVE_ERRORS}): {result['error']} {result.get('detail') or ''}".rstrip())
            if errors >= MAX_CONSECUTIVE_ERRORS:
                if cursor:
                    print(f"cursor: {cursor}", flush=True)
                return 3
            srv._watch_sleep(srv.WATCH_POLL_SECONDS)
            continue
        errors = 0

        if result.get("baseline"):
            for task in result.get("tasks") or []:
                previous[task["id"]] = str(task.get("status"))
                say(f"watching {task['id']} (status {task.get('status')})")
            for cp in result.get("pending_checkpoints") or []:
                say(f"open checkpoint {cp['checkpoint_id']} on {cp['task']} raised {cp.get('raised_at')}")
            cursor = result["cursor"]
            if args.until == "terminal" and result.get("terminal_subjects"):
                say(f"already terminal: {', '.join(result['terminal_subjects'])}")
                print(f"cursor: {cursor}", flush=True)
                return 0
            continue

        cursor = result.get("cursor") or cursor
        if _matches(result, args.until):
            for change in result.get("changes") or []:
                say(_describe(change, previous))
            for change in result.get("checkpoints") or []:
                say(_describe(change, previous))
            for task_id in args.task:
                say(f"next: get_task_timeline(task_entity_id=\"{task_id}\")")
            print(f"cursor: {cursor}", flush=True)
            return 0
        # A non-terminal change while waiting for a terminal one: remember the
        # status so the eventual summary shows where it came from.
        for change in result.get("changes") or []:
            if change.get("kind") == "status":
                previous[str((change.get("subject") or {}).get("task"))] = str(change.get("value"))


if __name__ == "__main__":
    sys.exit(main())
