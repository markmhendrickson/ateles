#!/usr/bin/env python3
"""Detect a session running on a `.claude/settings.json` that is missing hook
wirings the canonical settings declare.

The failure this exists to catch (ateles#973): a session ran for hours from a
worktree whose `.claude/settings.json` was behind main by three wirings —
`git_stash_guard.py` (PreToolUse `Bash`), the `compact` matcher on
`session_start.py`, and `reinject_working_method.py` on `compact`. So that
session had **no mechanical enforcement of the never-stash rule** (prose alone,
in a repo where the stash stack is shared across worktrees), and **neither
compaction hook fired**, losing its interaction rules at exactly the boundary
those hooks exist to cover.

Nothing surfaced any of that. A hook that is not wired does not fail loudly —
it simply never runs, which is invisible by construction. This module makes it
visible at session start.

Comparison source
-----------------
Deliberately a **committed reference snapshot** (`hook_wiring_reference.json`),
not a live `git show origin/main:...`. A live fetch would put a network and
git-credential dependency inside a hook that fires on every session start in
every environment, including sandboxed and offline agent runs — a new failure
surface of exactly the kind this issue warns about. The snapshot is generated,
never hand-written:

    python3 .claude/hooks/hook_wiring_reference.py --write

and held equal to `.claude/settings.json` by:

    python3 .claude/hooks/hook_wiring_reference.py --check

One-directional by design: a wiring the canonical set declares and the local
file lacks is reported; a purely local EXTRA hook is not. Main is authoritative
for what must be present, and flagging local additions would train the operator
to ignore the banner.

Stdlib only. No network. Every entry point is fail-open at the call site.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
REFERENCE_PATH = HOOKS_DIR / "hook_wiring_reference.json"
SETTINGS_PATH = HOOKS_DIR.parent / "settings.json"

# Human-readable notes for the wirings whose absence has already cost real
# work, so the banner can say why a missing one matters rather than only that
# it is missing.
_WHY = {
    ("PreToolUse", "git_stash_guard.py"): (
        "the never-stash rule is enforced by prose alone without it"
    ),
    ("SessionStart", "session_start.py"): (
        "the session-integrity contract is not re-injected after a compaction"
    ),
    ("SessionStart", "reinject_working_method.py"): (
        "the interaction rules are lost at every compaction boundary"
    ),
}


def _script_name(command: str) -> str:
    """The hook script's bare filename, from whatever command form wires it.

    Matching on the filename rather than the whole command string is
    deliberate: `$CLAUDE_PROJECT_DIR` expansion, quoting style, and whether
    the script is invoked via `python3` or directly are all incidental, while
    the filename is the wiring's identity.
    """
    for token in reversed(command.replace('"', " ").replace("'", " ").split()):
        name = token.strip().rstrip("/").split("/")[-1]
        if name.endswith((".py", ".sh")):
            return name
    return command.strip()


def wirings(settings: dict) -> set[tuple[str, str, str]]:
    """Flatten a settings object to `(event, script, matcher)` triples."""
    out: set[tuple[str, str, str]] = set()
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            matcher = group.get("matcher") or ""
            entries = group.get("hooks")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                command = entry.get("command")
                if not isinstance(command, str):
                    continue
                out.add((str(event), _script_name(command), str(matcher)))
    return out


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def describe(event: str, script: str, matcher: str) -> str:
    """One missing wiring, named by filename so it can be grepped for."""
    where = f"{event} {matcher}".strip()
    text = f"{script} ({where})"
    why = _WHY.get((event, script))
    return f"{text} — {why}" if why else text


def missing(
    settings_path: Path = SETTINGS_PATH,
    reference_path: Path = REFERENCE_PATH,
) -> list[str]:
    """Wirings the reference declares that the local settings lack.

    Returns descriptions, already sorted for a stable banner. An absent or
    unreadable reference returns `[]` — degrade to silence rather than let the
    checker itself become the thing that breaks session start.
    """
    try:
        reference = _load(reference_path)
        local = _load(settings_path)
    except (OSError, ValueError):
        return []

    want = reference.get("wirings")
    if not isinstance(want, list):
        return []
    have = wirings(local)
    gaps: list[str] = []
    for row in want:
        if not isinstance(row, list) or len(row) != 3:
            continue
        event, script, matcher = (str(x) for x in row)
        if (event, script, matcher) not in have:
            gaps.append(describe(event, script, matcher))
    return sorted(gaps)


def banner(gaps: list[str]) -> str:
    """The operator-facing message. Names every missing hook by filename."""
    return (
        f"[hook-wiring] WARNING: {len(gaps)} hook wiring(s) declared in "
        ".claude/hooks/hook_wiring_reference.json are NOT wired in this "
        "checkout's .claude/settings.json: "
        + "; ".join(gaps)
        + ". A hook that is not wired never runs and never errors, so nothing "
        "else will tell you. Restore them with: "
        "git show origin/main:.claude/settings.json > .claude/settings.json "
        "(review the diff first — a local settings change you meant to keep "
        "would be overwritten), then re-check with "
        "python3 .claude/hooks/hook_wiring_reference.py --check"
    )


def _cmd_write() -> int:
    settings = _load(SETTINGS_PATH)
    rows = sorted(list(w) for w in wirings(settings))
    payload = {
        "_comment": (
            "GENERATED — do not hand-edit. Regenerate with "
            "`python3 .claude/hooks/hook_wiring_reference.py --write` after an "
            "intentional change to .claude/settings.json. This is the "
            "canonical hook-wiring set that session_start.py compares a "
            "checkout against (ateles#973); a committed snapshot rather than a "
            "live git fetch, so the check cannot hang or fail an offline "
            "session start."
        ),
        "wirings": rows,
    }
    REFERENCE_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {REFERENCE_PATH} ({len(rows)} wirings)")
    return 0


def _cmd_check() -> int:
    settings = _load(SETTINGS_PATH)
    try:
        reference = _load(REFERENCE_PATH)
    except (OSError, ValueError) as exc:
        print(f"error: cannot read {REFERENCE_PATH}: {exc}", file=sys.stderr)
        return 2
    want = {tuple(str(x) for x in r) for r in reference.get("wirings", [])}
    have = wirings(settings)
    absent = sorted(want - have)
    extra = sorted(have - want)
    if not absent and not extra:
        print(f"OK: reference matches .claude/settings.json ({len(want)} wirings)")
        return 0
    for event, script, matcher in absent:
        print(f"MISSING {describe(event, script, matcher)}")
    for event, script, matcher in extra:
        print(
            f"UNRECORDED {describe(event, script, matcher)} — wired locally but "
            "not in the reference; regenerate with --write if intentional"
        )
    print(
        "\nThe reference is generated from .claude/settings.json. If the "
        "settings change was intentional, run --write and commit the snapshot; "
        "if not, the settings file is the thing to fix."
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--write" in args:
        return _cmd_write()
    if "--check" in args:
        return _cmd_check()
    gaps = missing()
    if gaps:
        print(banner(gaps))
        return 1
    print("OK: every referenced hook wiring is present")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail open
        print(f"[hook-wiring] check error (ignored): {exc}", file=sys.stderr)
        sys.exit(0)
