#!/usr/bin/env python3
"""
check_neotoma_rest_paths.py — stop MCP *tool names* being used as Neotoma REST
*URL paths*.

WHY THIS EXISTS

  Neotoma has two distinct surfaces with confusingly similar vocabulary:

    * the MCP tool surface, whose tools are named `retrieve_entities`,
      `retrieve_entity_snapshot`, `retrieve_related_entities`, `correct`, ...
    * the REST surface daemons actually speak over httpx, whose list route is
      POST `/entities/query`.

  `retrieve_entities` is a TOOL NAME, not a REST path. POSTing to
  `/retrieve_entities` returns 404 on the hosted instance. Verified live
  2026-08-31 against prod: POST /retrieve_entities → 404 ("Cannot POST
  /retrieve_entities"), POST /entities/query → 200.

  The mistake is pernicious because these reads almost always degrade
  gracefully — a 404 is caught, logged at WARNING, and the caller continues
  with an empty result. So the daemon reports success while running blind:

    * ateles#584 — Anthus read participation state from the dead route, so
      every work entity looked like a fresh one with no gates run.
    * issue_spec.py — every spec load silently degraded to an empty state.
    * gate_waive.py — every gate read as "no entity" and failed closed.
    * agent_loader.py — agent_policy loads returned nothing, so agents were
      dispatched without their learned policies.

  Four independent modules made the identical mistake, and each was found only
  after it caused a production symptom. Fixing occurrences one at a time does
  not stop the fifth. This linter makes the wrong path unmergeable.

SCOPE: lib/, execution/, scripts/ Python files. Tests are exempt — a test may
legitimately assert the old behaviour or simulate a 404 (see
execution/daemons/apis/test_issue_spec.py, which accepts both paths).

SUPPRESSION: append `# neotoma-rest-path-ok: <reason>` to the line.

Usage:
  python3 scripts/linters/check_neotoma_rest_paths.py [file1.py ...]
  (no args → scans the default trees)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SUPPRESS = "neotoma-rest-path-ok"

DEFAULT_DIRS = ("lib", "execution", "scripts")

# MCP tool names that are NOT REST paths, mapped to the route to use instead.
TOOL_NAME_TO_REST = {
    "retrieve_entities": "/entities/query (POST)",
    "retrieve_entity_snapshot": "/entities/<entity_id> (GET)",
    "retrieve_related_entities": "/entities/<entity_id>/related (GET)",
}

# A tool name used as a URL path. Matches the two shapes daemons write:
#   f"{NEOTOMA_BASE_URL}/retrieve_entities"     → leading slash
#   await _post("retrieve_entities", {...})     → bare path passed to a helper
#     that prefixes the base URL
_PATH_RE = re.compile(
    r"""(?P<q>["'])                 # opening quote
        (?:\{NEOTOMA_BASE_URL\})?   # optional f-string base-url prefix
        /?                          # optional leading slash
        (?P<name>%s)                # the offending tool name
        (?P=q)                      # closing quote
    """
    % "|".join(TOOL_NAME_TO_REST),
    re.VERBOSE,
)


def iter_files(argv: list[str]) -> list[Path]:
    if argv:
        return [Path(a) for a in argv if a.endswith(".py")]
    out: list[Path] = []
    for d in DEFAULT_DIRS:
        root = Path(d)
        if root.is_dir():
            out.extend(root.rglob("*.py"))
    return out


def is_exempt(path: Path) -> bool:
    name = path.name
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    if name == Path(__file__).name:
        return True
    return ".venv" in path.parts or "venv" in path.parts


def scan(path: Path) -> list[tuple[int, str, str]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    hits: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if SUPPRESS in line:
            continue
        # A bare mention in a comment or docstring is documentation, not a call.
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        m = _PATH_RE.search(line)
        if m:
            name = m.group("name")
            hits.append((lineno, name, TOOL_NAME_TO_REST[name]))
    return hits


def main(argv: list[str]) -> int:
    total = 0
    for path in iter_files(argv):
        if is_exempt(path):
            continue
        for lineno, name, correct in scan(path):
            if total == 0:
                print(
                    "❌ NEOTOMA REST PATH VIOLATION: an MCP tool name is being "
                    "used as a REST URL path.",
                    file=sys.stderr,
                )
                print(
                    "   These 404 on the hosted instance, and the 404 is "
                    "usually swallowed — the caller reports success while "
                    "reading nothing. See ateles#584.\n",
                    file=sys.stderr,
                )
            total += 1
            print(f"  {path}:{lineno}: {name!r} is an MCP tool, not a route", file=sys.stderr)
            print(f"      fix: use {correct}", file=sys.stderr)
    if total:
        print(
            f"\n{total} violation(s). If a literal is intentional (a test "
            f"fixture, or a deliberate 404 probe), append "
            f"`# {SUPPRESS}: <reason>` to the line.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
