#!/usr/bin/env python3
"""
validate_tool_allowlist.py — reject the undocumented `bash:<command>` grant
prefix in agent_definition.tool_allowlist (ateles#255).

WHY THIS EXISTS

`tool_allowlist` entries used an undocumented `bash:<command>` prefix that
Claude Code's `--allowedTools` parser silently drops — no error, the grant is
just excluded from the effective allowlist. The CLI-native, working grammar is
`Bash(<command>:*)`. This linter closes the regression path: it fails the
build the moment a `bash:` prefix reappears in any agent's tool_allowlist,
naming the offending agent, the bad rule, and the fixed form inline so the fix
is a one-line diff, not a investigation.

Non-bash tool entries (MCP tools, bare `Bash`, `Read`, etc.) and legitimate
`Bash(...)` grants are left alone. A grant with no recognizable prefix at all
(neither `bash:` nor a bare known tool name) is flagged as a distinct
"unrecognized grant form" warning class rather than silently accepted — see
UNRECOGNIZED_RE below.

USAGE

  # Validate the live Neotoma tool_allowlist for every active agent_definition:
  python3 scripts/linters/validate_tool_allowlist.py

  # Validate a JSON fixture instead of hitting Neotoma (used by tests and by
  # anyone who wants to check a draft agent_definition before storing it):
  python3 scripts/linters/validate_tool_allowlist.py --fixture path/to/agents.json

Fixture shape: a JSON array of objects, each `{"name": "<agent>",
"tool_allowlist": [...]}` (the same shape `_as_list()` in
execution/scripts/render_agent_docs.py normalizes tool_allowlist into).

Exit code: 0 when no `bash:`-prefixed grant is found (unrecognized-form
warnings do not fail the build — see WARN vs FAIL below); 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Reuse the Neotoma fetch + list-normalization logic from the sibling script
# that owns it, rather than maintaining a second copy that can silently drift
# from what render_agent_docs.py actually treats as an agent_definition's
# tool_allowlist (including its "real agent" filter — tier/genus/aauth_sub
# required — that excludes skills miscategorized as agent_definition).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "execution" / "scripts"))
import render_agent_docs  # noqa: E402

_as_list = render_agent_docs._as_list

BASH_PREFIX_RE = re.compile(r"^bash:(.+)$")

# A grant is "recognized" when it is either:
#   - a bare tool/capability-slot name (Read, Bash, neotoma_read, gws_gmail, ...)
#     — CamelCase harness tools and snake_case capability-slot aliases both
#     appear across existing agent_definitions, so both are accepted here
#   - the universal wildcard grant "*"
#   - an MCP tool reference (mcp__<server>__<tool-or-*>)
#   - the CLI-native scoped-bash grammar Bash(<command>:*) or Bash(<command>)
# Anything else that isn't the banned `bash:` prefix is flagged as a distinct,
# non-fatal "unrecognized form" warning rather than silently passed through —
# a typo'd grant should not look identical to a validated one.
KNOWN_BARE_TOOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
MCP_TOOL_RE = re.compile(r"^mcp__[A-Za-z0-9_-]+__[A-Za-z0-9_*]+$")
SCOPED_BASH_RE = re.compile(r"^Bash\([^)]*\)$")


def _suggested_fix(rule: str, offending: str) -> str:
    """Suggest the Bash(<command>:*) replacement for a bash:<command> rule."""
    return f"Bash({offending}:*)"


def check_agent(name: str, tool_allowlist) -> tuple[list[str], list[str]]:
    """Return (blocking_findings, warnings) for one agent's tool_allowlist."""
    blocking: list[str] = []
    warnings: list[str] = []
    for rule in _as_list(tool_allowlist):
        m = BASH_PREFIX_RE.match(rule)
        if m:
            fixed = _suggested_fix(rule, m.group(1))
            blocking.append(
                f'agent "{name}": rule "{rule}" uses the banned bash: prefix '
                f'(silently dropped by the CLI) — replace with "{fixed}"'
            )
            continue
        if (
            rule == "*"
            or KNOWN_BARE_TOOL_RE.match(rule)
            or MCP_TOOL_RE.match(rule)
            or SCOPED_BASH_RE.match(rule)
        ):
            continue
        warnings.append(
            f'agent "{name}": rule "{rule}" is not a recognized grant form '
            "(neither a bare tool name, an mcp__<server>__<tool> reference, "
            "nor Bash(<command>)) — verify this is intentional"
        )
    return blocking, warnings


def fetch_agents_from_neotoma() -> list[dict] | None:
    """Fetch agent_definition entities via render_agent_docs.py's own fetch —
    same Neotoma query, same env/token resolution, same "real agent" filter
    (tier/genus/aauth_sub required), so this linter can never disagree with
    the renderer about which entities are agents or what their tool_allowlist
    values normalize to.

    Returns None (rather than raising) when NEOTOMA_BASE_URL/NEOTOMA_BEARER_TOKEN
    aren't configured or Neotoma rejects/can't be reached — this lane runs on
    every PR touching agent config, including forks and environments where the
    token secret isn't provisioned, and a live-state check has no fixture to
    fall back to. Same optional-token posture as loxia_review.py's
    NEOTOMA_BEARER_TOKEN handling: skip cleanly rather than fail the build on
    infrastructure the PR didn't touch."""
    try:
        base_url, token = render_agent_docs._load_env()
    except SystemExit:
        return None
    if not token:
        return None
    try:
        agents = render_agent_docs.fetch_agents(base_url, token)
    except SystemExit:
        return None
    return [
        {"name": a["name"], "tool_allowlist": a.get("tool_allowlist")} for a in agents
    ]


def load_fixture(path: str) -> list[dict]:
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise SystemExit(f"fixture must be a JSON array of agent objects: {path}")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        help="path to a JSON fixture (array of {name, tool_allowlist}) instead of live Neotoma",
    )
    args = parser.parse_args()

    agents = load_fixture(args.fixture) if args.fixture else fetch_agents_from_neotoma()

    if agents is None:
        # Neotoma unreachable (e.g. NEOTOMA_BEARER_TOKEN not configured in this
        # environment) is an infra gap, not a grant-grammar defect — do not
        # fail the build over it. lanius-stale-issues.yml hits the same
        # missing-secret condition; both are pre-existing and outside any
        # single PR's control. Mirrors the informational treatment already
        # given to the doc-mirror-freshness step in CI.
        print(
            "SKIP — NEOTOMA_BEARER_TOKEN not configured or Neotoma unreachable; "
            "cannot validate live agent_definition state in this environment"
        )
        return 0

    all_blocking: list[str] = []
    all_warnings: list[str] = []
    for agent in agents:
        name = agent.get("name", "<unknown>")
        blocking, warnings = check_agent(name, agent.get("tool_allowlist"))
        all_blocking += blocking
        all_warnings += warnings

    if all_warnings:
        print(f"WARN — {len(all_warnings)} unrecognized grant form(s):")
        for w in all_warnings:
            print(f"  {w}")

    if all_blocking:
        print(f"FAIL — {len(all_blocking)} banned bash: prefix grant(s) found:")
        for b in all_blocking:
            print(f"  {b}")
        return 1

    print(f"OK — {len(agents)} agent(s) checked, no bash: prefix grants found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
