#!/usr/bin/env python3
"""SessionStart hook — push the live `agent_policy` rule index into context.

ateles#1261 (E2 session transport). PR #1255 removed the swarm's governing
rules from the MCP server's `instructions` field, which the client caps at
~2,048 chars across ALL connected servers — so a rule written to the record
stopped reaching sessions entirely. This hook is the replacement: it renders
`agent_policy` LIVE from Neotoma every time a session starts, resumes, is
cleared, or is compacted, and prints the always-applies preamble plus one
line per conditional rule so the session can fetch the full rule by entity
id before acting on it.

TIERED, NOT FAIL-OPEN, ON SIZE. The first live measurement against real
Neotoma (53 rows, 50 session-scoped) rendered to 12,633 chars — over budget.
Operator ruling (ateles#1261 follow-up): a corpus too large for the budget
must DEGRADE the index, never fail open — fail-open is reserved for Neotoma
being unreachable or the renderer itself raising, never merely for being
over budget. `policy_skill_renderer.render_index_text` now tries three
tiers (full conditional lines -> trigger+id only -> mandatory-first with an
omitted-count line) and only raises if even the smallest tier (preamble +
closing line, every conditional rule dropped) still doesn't fit — that is
the one case this hook still treats as fail-open, because there is nothing
smaller left to render.

NO GENERATED FILES. This prints to stdout only — nothing is written to disk.
A file copy per checkout is how CLAUDE.md drifted into 31 versions across
repos; that failure mode is exactly what "render live" avoids.

CWD-INDEPENDENT BY DESIGN. This script is wired at repo level
(`.claude/settings.json`, matcher `startup|resume|clear|compact`) AND is
meant to be invoked from a **user-level** `~/.claude/settings.json` by its
absolute path inside the `~/ateles-rc-src` deploy checkout, so it must work
regardless of which repo (or non-repo directory) the session's cwd happens
to be. It never reads `CLAUDE_PROJECT_DIR` or the working directory to find
its sibling code — like every other hook in this directory
(gh_identity_guard.py, git_stash_guard.py, ...), it resolves `lib/` relative
to `Path(__file__).resolve()`, which is fixed at wherever the script
physically lives, not at whatever directory launched it.

Fail-open for the SESSION only when there is genuinely nothing to render: if
Neotoma is unreachable, the renderer's import fails (e.g. no `httpx` in this
Python environment — the renderer imports `agent_loader.policy_binds_agent`,
which hard-imports httpx), or the corpus cannot fit even the smallest tier
(`PolicyIndexError`, see policy_skill_renderer.py). Any of these print ONE
line saying the rules could not be loaded and exit 0. Never crash a session
start, and never truncate a line mid-rule — but an over-budget corpus is
NOT one of these cases: it degrades through tiers A -> B -> C instead
(docs/foundation/principles.md#1: a mechanism that fails silently past its
own limit is not a control; degrading loudly, tier by tier, is what keeps
this one a control rather than documentation).

Budget: 8,000 characters, conservative below the ~10,000-char Claude Code
hook-output cap documented for `additionalContext`/plain stdout (multiple
independent secondary sources — GitHub issues anthropics/claude-code#44086,
#94358, #70460 — cite this figure from the official Hooks reference at
code.claude.com/docs/en/hooks; a direct WebFetch of that page during this
build did not itself surface the number in the crawled text, so the figure
is corroborated by several converging sources rather than confirmed by a
first-party quote this build captured directly — treat it as empirically
unconfirmed by this build and needing a live before/after measurement to
pin down exactly). This hook's own budget leaves headroom for the OTHER
three SessionStart hooks that run in the same lifecycle event
(`ateles-session-start.sh`, `session_start.py`, and on compact
`reinject_working_method.py`), which each emit their own stdout against the
same per-hook cap.

Never logs rule bodies — some `agent_policy` rows hold operator payment
details (CLAUDE.md). Only the rendered index (which itself contains no rule
bodies, only entity ids and one-line summaries) reaches stdout; anything
diagnostic goes to stderr and never includes `rule` text.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Resolve siblings relative to THIS FILE, never cwd/CLAUDE_PROJECT_DIR — the
# one property that makes this hook usable from a user-level settings.json
# invoking it by absolute path from a directory outside any ateles checkout.
_HOOK_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _HOOK_DIR.parent.parent
_LIB_DIR = _REPO_ROOT / "lib"

BUDGET_CHARS = 8000


def _log(msg: str) -> None:
    sys.stderr.write(f"[session-rule-index] {msg}\n")


def _render() -> str | None:
    """Live-render the index, or None on a genuine failure (fail-open):
    Neotoma unreachable, the renderer unimportable, or a corpus too large
    even for tier C. A merely large corpus does NOT hit this path — it
    returns a tiered (A/B/C) string from render_index_text instead.
    """
    try:
        if str(_REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(_REPO_ROOT))
        if str(_LIB_DIR) not in sys.path:
            sys.path.insert(0, str(_LIB_DIR))
        from lib.daemon_runtime.policy_skill_renderer import (  # noqa: PLC0415
            fetch_active_policy_rows,
            render_index_text,
            render_skills,
        )
    except Exception as exc:  # noqa: BLE001 — fail open on import (e.g. no httpx)
        _log(f"renderer unavailable: {type(exc).__name__}: {exc}")
        return None

    try:
        rows = fetch_active_policy_rows()
    except Exception as exc:  # noqa: BLE001 — fail open on transport failure
        _log(f"could not reach Neotoma: {type(exc).__name__}: {exc}")
        return None

    try:
        skills = render_skills(rows)
        return render_index_text(skills, BUDGET_CHARS)
    except Exception as exc:  # noqa: BLE001 — includes the budget-overflow raise
        _log(f"could not render rule index: {type(exc).__name__}: {exc}")
        return None


def main() -> int:
    text = _render()
    if text is None:
        print(
            "[agent_policy] The live rule index could not be loaded this "
            "session — proceeding WITHOUT the swarm's governing rules from "
            "the record. See stderr for why; standing constraints in "
            "CLAUDE.md still apply."
        )
        return 0
    print("# Agent policy rule index (live from Neotoma, ateles#1261)\n")
    print(text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — fail open; never block a session start
        sys.exit(0)
