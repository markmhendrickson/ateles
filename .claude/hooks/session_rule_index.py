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

Budget: 9,800 characters, measured directly (ateles#1254 follow-up) rather
than inferred from secondary sources. Method: a throwaway SessionStart hook
(`probe.py`) printed an exact byte count of numbered filler text into a
scratch project directory, driven headless via `claude -p --settings
<scratch settings.json>` at several sizes, then the session's own transcript
(`~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`) was read back for the
`hook_success` attachment's `content` field — the actual text that reached
context, not just an exit code. Binary search over that field's length found
the cutoff is EXACT: content of length 10,000 reaches context byte-for-byte
(`content` field length == 10000); content of length 10,001 is replaced with
a `<persisted-output>` pointer + a ~2,048-byte ("first 2KB") preview, e.g.
"Output too large (11.7KB). Full output saved to: .../tool-results/
hook-<id>-stdout.txt\n\nPreview (first 2KB): ...". This confirms, with a
first-party measurement this build captured directly, the figure the
previous revision could only cite from secondary sources (GitHub issues
anthropics/claude-code#44086, #94358, #70460) — 10,000 chars, not "~10,000".
It also confirms the same shape the operator had already observed by hand
(16.3 KB replaced by a ~2 KB preview, ateles#1254) is this exact mechanism,
not a different one at a different size.

The cap applies PER HOOK INVOCATION, not pooled across every SessionStart
hook firing in the same lifecycle event: one measurement run captured BOTH
`neotoma_session_start_instructions.sh` (6,433 chars) and the probe hook
(8,000 chars) — 14,433 chars combined across two hook stdouts in one
SessionStart event — with neither truncated, because each hook's stdout is
captured into its own `hook_success` attachment and truncated (or not)
independently. So this hook's budget does not need to be sized DOWN to make
room for its co-tenants' output; 9,800 leaves ~140 chars of margin below the
hook's own 10,000-char cap, covering the ~62-byte "# Agent policy rule
index..." header this hook prints ahead of `render_index_text()`'s own
output plus the two trailing newlines from the two `print()` calls, with a
small buffer against measurement variance. (The other SessionStart hooks
sharing this channel — `ateles-session-start.sh` at 16,807 chars,
`session_start.py` at 1,756, `reinject_working_method.py` at 1,669 on
compact, `neotoma_session_start_instructions.sh` at 6,455 — are each judged
against this SAME per-hook cap independently; `ateles-session-start.sh`
already exceeds it on its own, which is a pre-existing condition of that
hook, not something this budget change causes or can fix.)

Never follows a redirect: the renderer's transport refuses any 3xx, so the
bearer token is only ever sent to the configured Neotoma host (a refused
redirect is a transport failure and falls open like one).

Never logs rule bodies — some `agent_policy` rows hold operator payment
details (CLAUDE.md). Only the rendered index (which itself contains no rule
bodies, only entity ids and one-line summaries) reaches stdout; anything
diagnostic goes to stderr and never includes `rule` text.

RECORDS WHAT IT DELIVERED (ateles#1261 follow-up, audit
ent_b66293f0dcc8c887d4fdbeae). After a successful render, this hook writes
the delivered row signature into this session's `.claude/.session_state/`
file via the SHARED `rule_index_state` module (also used by the
UserPromptSubmit companion, `session_rule_delivery.py`), so that hook's very
first comparison finds "nothing changed yet" instead of re-printing the
whole index the very next prompt. A fail-open render (Neotoma unreachable,
etc.) records nothing — there is nothing delivered to record, and the
delivery hook's "no prior signature" fallback (inject everything once it is
next asked) is the correct behavior in that case, not a bug to route around.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _session_integrity import read_hook_input, load_state, save_state  # noqa: E402
from rule_index_state import record_delivery  # noqa: E402

# Resolve siblings relative to THIS FILE, never cwd/CLAUDE_PROJECT_DIR — the
# one property that makes this hook usable from a user-level settings.json
# invoking it by absolute path from a directory outside any ateles checkout.
_HOOK_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _HOOK_DIR.parent.parent
_LIB_DIR = _REPO_ROOT / "lib"

BUDGET_CHARS = 9800


def _log(msg: str) -> None:
    sys.stderr.write(f"[session-rule-index] {msg}\n")


def _render() -> tuple[str, list[dict]] | None:
    """Live-render the index plus the session-scoped raw rows it was built
    from (for delivery-signature recording), or None on a genuine failure
    (fail-open): Neotoma unreachable, the renderer unimportable, or a corpus
    too large even for tier C. A merely large corpus does NOT hit this path
    — it returns a tiered (A/B/C) string from render_index_text instead.
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
            _session_scope_ok,
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
        text = render_index_text(skills, BUDGET_CHARS)
        # `rows` is already flattened by unwrap_policy_entities (bare
        # snapshot dicts with `_entity_id` stamped on) — scope on the row
        # directly, not `row["snapshot"]`, which does not exist here.
        scoped_rows = [r for r in rows if _session_scope_ok(r)]
        return text, scoped_rows
    except Exception as exc:  # noqa: BLE001 — includes the budget-overflow raise
        _log(f"could not render rule index: {type(exc).__name__}: {exc}")
        return None


def main() -> int:
    ev = read_hook_input()
    session_id = ev.get("session_id", "")

    rendered = _render()
    if rendered is None:
        print(
            "[agent_policy] The live rule index could not be loaded this "
            "session — proceeding WITHOUT the swarm's governing rules from "
            "the record. See stderr for why; standing constraints in "
            "CLAUDE.md still apply."
        )
        return 0

    text, scoped_rows = rendered
    print("# Agent policy rule index (live from Neotoma, ateles#1261)\n")
    print(text)

    if session_id:
        try:
            state = load_state(session_id)
            save_state(session_id, record_delivery(state, scoped_rows))
        except Exception as exc:  # noqa: BLE001 — never let bookkeeping break delivery
            _log(f"could not record delivered signature: {exc}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — fail open; never block a session start
        sys.exit(0)
