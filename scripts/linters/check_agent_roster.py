#!/usr/bin/env python3
"""
check_agent_roster.py — fail when a retired agent name appears in an agent
prompt, mirror, or daemon routing table.

WHY THIS EXISTS:

  Swarm agents are renamed occasionally (gryllus -> cicada and
  bombycilla -> waxwing, both 2026-06-12, for voice/ASR robustness). The
  standing rule in CLAUDE.md is that a rename corrects every stale reference
  in the same turn. Nothing enforced that, so seven weeks later the Lanius
  gate-owner table still named `Bombycilla` (arch) and `Gryllus` (impl).

  The concrete harm: a gate board that names a retired agent points the
  operator at a command that does not exist, and `current_owner: "gryllus"`
  sets an owner no dispatcher can resolve — the gate stalls silently. See
  ateles#320.

  Retired names are a CLOSED, ENUMERABLE set, which makes this cheap to
  enforce: any agent name that is not in the live roster must not appear as a
  role reference.

WHAT IT CHECKS:

  For every file in scope, flag a retired name unless the line is an
  explicitly allowed historical reference (see ALLOWED_CONTEXTS) — a rename
  note ("formerly Gryllus; renamed 2026-06-12"), or a genus mention
  (`Bombycilla garrulus` is the waxwing's actual Linnaean genus and belongs in
  Waxwing's own identity section).

  The live roster is derived from `docs/agents/*.md` — the generated mirror of
  the Neotoma `agent_definition` entities — so it stays correct without this
  linter carrying its own copy of the roster.

SCOPE: execution/daemons/, execution/scripts/, lib/ — the always-on runtime,
where a retired name is a live routing defect. Test files, and this linter, are
exempt.

The generated mirrors (docs/agents/, .claude/skills/) are scanned only with
`--include-mirrors`. They are excluded by default because every agent mirror is
currently behind Neotoma — `render_agent_docs.py --check` fails repo-wide, for
reasons broader than names — so scanning them by default would keep this linter
red for a condition it does not own. The canonical entities are clean. Fold
MIRROR_SCOPE into DEFAULT_SCOPE in the same change that regenerates the mirrors
(ateles#320).

SUPPRESSION: append `# roster-ok: <reason>` (or `<!-- roster-ok: <reason> -->`
in Markdown) to a line that legitimately names a retired agent. Use sparingly —
the usual correct action is to fix the name, and for a prompt or mirror the fix
belongs in Neotoma (`correct()` the agent_definition), not on disk.

Usage:
  python3 scripts/linters/check_agent_roster.py [file1 file2 ...]
  # with no arguments, scans the runtime scope
  python3 scripts/linters/check_agent_roster.py --include-mirrors
  # also scans the generated agent mirrors
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Agent names that have been retired, mapped to their current name. Add an
# entry here as part of any future rename — that is the one manual step, and it
# is what turns the rename into an enforced invariant.
RETIRED_AGENTS: dict[str, str] = {
    "gryllus": "cicada",
    "bombycilla": "waxwing",
}

# Directories scanned by default: the always-on runtime, where a retired name
# is a live routing defect rather than stale prose. `review_learning.py` shipped
# `DEFAULT_OWNER = "gryllus"` for seven weeks after the rename, assigning every
# systemic review finding to an agent that does not exist.
DEFAULT_SCOPE = (
    "execution/daemons",
    "execution/scripts",
    "lib",
)

# Generated mirrors of the Neotoma agent_definition entities. NOT scanned by
# default (see --include-mirrors): as of 2026-07-29 every agent mirror is behind
# Neotoma — `render_agent_docs.py --check` fails repo-wide, not just on names —
# so scanning them here would make this linter permanently red for a reason it
# does not own. The canonical entities are clean; the mirrors need one
# regeneration, tracked in ateles#320. Add these to the default scope in the
# same change that regenerates them.
MIRROR_SCOPE = (
    "docs/agents",
    ".claude/skills",
)

SCANNED_SUFFIXES = {".md", ".py"}

# Test files are exempt: fixtures legitimately use arbitrary agent names as
# stand-ins, and pinning them to the live roster would make the tests brittle
# without making the swarm any more correct.
TEST_FILE = re.compile(r"(^|/)(test_[^/]+|[^/]+_test)\.py$|(^|/)tests?/")

# Lines matching any of these keep a retired name legitimately.
ALLOWED_CONTEXTS = (
    # "Formerly Gryllus; renamed 2026-06-12 ..." — the rename note itself.
    re.compile(r"formerly\s+(gryllus|bombycilla)", re.IGNORECASE),
    # `Bombycilla garrulus` is the waxwing's real genus, not a stale role ref.
    re.compile(r"bombycilla\s+garrulus", re.IGNORECASE),
    # A rename example that names both sides — "gryllus -> cicada" documents
    # the mapping itself and is correct wherever it appears.
    re.compile(
        r"(gryllus|bombycilla)\s*(->|→|to)\s*(cicada|waxwing)", re.IGNORECASE
    ),
    # Explicit reviewed suppression.
    re.compile(r"roster-ok:"),
)

# This linter necessarily contains the retired names.
SELF_EXEMPT = {Path(__file__).resolve()}


def live_roster() -> set[str]:
    """Agent names currently defined, from the generated docs/agents mirror."""
    agents_dir = REPO_ROOT / "docs" / "agents"
    if not agents_dir.is_dir():
        return set()
    return {
        path.stem.lower()
        for path in agents_dir.glob("*.md")
        if path.stem.lower() != "readme"
    }


def iter_scope(paths: list[str], include_mirrors: bool = False) -> list[Path]:
    if paths:
        candidates = [Path(p).resolve() for p in paths]
    else:
        scope = DEFAULT_SCOPE + (MIRROR_SCOPE if include_mirrors else ())
        candidates = []
        for rel in scope:
            root = REPO_ROOT / rel
            if root.is_dir():
                candidates.extend(sorted(root.rglob("*")))
    return [
        p
        for p in candidates
        if p.is_file()
        and p.suffix in SCANNED_SUFFIXES
        and p not in SELF_EXEMPT
        and not TEST_FILE.search(p.as_posix())
    ]


def check_file(path: Path, retired: dict[str, str]) -> list[tuple[int, str, str]]:
    """Return (line_number, retired_name, line_text) for each violation."""
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(name) for name in retired) + r")\b",
        re.IGNORECASE,
    )
    findings: list[tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings

    for lineno, line in enumerate(text.splitlines(), start=1):
        match = pattern.search(line)
        if not match:
            continue
        if any(allowed.search(line) for allowed in ALLOWED_CONTEXTS):
            continue
        findings.append((lineno, match.group(1).lower(), line.strip()))
    return findings


def main(argv: list[str]) -> int:
    include_mirrors = "--include-mirrors" in argv
    argv = [a for a in argv if a != "--include-mirrors"]
    roster = live_roster()
    # Guard against a rename that has not been recorded here: if a "retired"
    # name is still a live agent, this linter is out of date, not the code.
    retired = {
        name: replacement
        for name, replacement in RETIRED_AGENTS.items()
        if name not in roster
    }
    stale_entries = set(RETIRED_AGENTS) - set(retired)
    if stale_entries:
        print(
            "check_agent_roster: these names are listed as retired but still "
            f"have a docs/agents entry: {', '.join(sorted(stale_entries))}. "
            "Update RETIRED_AGENTS.",
            file=sys.stderr,
        )
    if not retired:
        return 0

    violations = 0
    for path in iter_scope(argv, include_mirrors=include_mirrors):
        for lineno, name, line in check_file(path, retired):
            rel = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
            print(
                f"{rel}:{lineno}: retired agent name '{name}' "
                f"(now '{retired[name]}'): {line[:120]}"
            )
            violations += 1

    if violations:
        print(
            f"\n{violations} retired-agent reference(s) found.\n"
            "Agent prompts and .claude/skills mirrors are GENERATED from Neotoma — "
            "fix them with correct() on the agent_definition entity, then re-render "
            "(execution/scripts/render_agent_docs.py). Editing the mirror alone will "
            "be overwritten.\n"
            "For a legitimate historical reference, append 'roster-ok: <reason>'.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
