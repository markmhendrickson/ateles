#!/usr/bin/env python3
"""UserPromptSubmit hook — re-deliver the rule index when the corpus changes.

ateles#1261 follow-up. Audit ent_b66293f0dcc8c887d4fdbeae found the
SessionStart index (`session_rule_index.py`) delivered ONCE in a 17.5-hour
session, at its only compaction: the hook fires only on startup, resume,
clear and compact, so a long-running session between those events never saw
a rule created, backfilled, or changed after its one delivery. 38 backfilled
summaries and 17 new rules never reached that session at all.

This hook closes that gap on the cheap per-turn path: it re-fetches the
live, session-scoped `agent_policy` set every prompt, hashes it (via the
SHARED `rule_index_state` module — see that file's docstring for why the
signature logic lives there once rather than being copied), and injects
ONLY what changed since the hash last delivered to this session — never the
whole index again.

Reuses the EXACT reader and scoping `session_rule_index.py` already uses
(CLAUDE.md: "extend the mechanism that already generalizes; do not build a
parallel one"):

  - `lib.daemon_runtime.policy_skill_renderer.fetch_active_policy_rows` —
    the stdlib-only (urllib) transport, redirect-refusing, retrying reader.
  - `lib.daemon_runtime.policy_skill_renderer.render_skills` — session
    scoping via `_session_scope_ok` / `agent_loader.policy_binds_agent`,
    plus the same field sanitization `to_skill` already applies (every
    row-derived string that reaches this hook's stdout has already been
    through the injection-hardening `_sanitize_field` pass; nothing here
    re-implements or bypasses it).

No new Neotoma query shape, no new scope predicate, no new sanitizer.

DELTA, NOT FULL RE-INJECTION. Compares the CURRENT scoped row signature
(`rule_index_state.row_signature` — a content fingerprint per entity id, see
that module's docstring for why content rather than a server timestamp)
against the previously delivered one:
  - a NEW row (never delivered before) or a CHANGED row (delivered before,
    different content fingerprint now) is injected;
  - a row with no signature change is skipped entirely — this is what keeps
    a mid-session addition a small, one-time injection rather than a repeat
    of the whole corpus every prompt;
  - a row that no longer scopes to this session (retired, re-scoped away, or
    no longer active/provisional) is simply absent from the next recorded
    signature — there is nothing safe to say about a rule that no longer
    applies, and the closing instruction below still tells the session to
    fetch by id before acting on anything conditional.

RENDER SHAPE PER ROW, per the task's own instruction: full `rule` text for
`rule_kind == "mandatory"` rows (already on hand via `to_skill`'s
`PolicySkill.body`, sanitized-safe-to-print text plus the entity id — never
re-derived here), and the existing one-line tier-B form (`- When <trigger>:
<imperative or nothing> [<entity_id>]`) for every other changed/new row.
Mirrors `policy_skill_renderer.render_index_text`'s own mandatory-first
framing (tier C already orders mandatory before advisory) rather than
inventing a second notion of "important."

BOUNDED. Reuses the measured 10,000-char SessionStart/UserPromptSubmit hook
cap (session_rule_index.py's own docstring: exact cutoff proven by direct
measurement, ateles#1254 follow-up) — this hook's OWN budget is smaller,
9,000 chars, leaving headroom below the same hard cutoff for
`user_prompt_submit.py`'s own (tiny) stdout sharing the same
UserPromptSubmit event. Degrades the SAME way `render_index_text` degrades
on size — never fails open merely for being large: changed mandatory rows
are kept in full first (sorted by entity_id for determinism), then changed
advisory rows are added as one-line entries until the budget is exhausted;
anything still left over is named by count, never truncated mid-rule.

FIRST PROMPT IS A NO-OP BY DESIGN. `session_rule_index.py` (companion change
in this PR) now records the hash of what it delivered into this session's
state file via `rule_index_state.record_delivery`, BEFORE this hook ever
runs — so a session's first prompt compares against that recorded hash and
(barring a same-turn correction) finds nothing changed. A session that
somehow never got a SessionStart delivery (no matching state key) is treated
as "nothing delivered yet," so the first prompt's diff is against an empty
set — i.e. it injects the full current scoped set that turn, same as a
fresh SessionStart would have. That is a feature, not a gap: a missing prior
hash must not read as "nothing ever changes."

FAIL-OPEN, STDLIB-ONLY. Any Neotoma unreachability, renderer import failure
(no httpx — the same reason `session_rule_index.py` treats it as fail-open),
or unexpected exception prints nothing and exits 0. A per-turn hook that
could ever block or visibly error on a transient network blip would make
every prompt slower and noisier than the problem it fixes.

Never logs a rule body to stderr (only entity ids and counts) — some
agent_policy rows hold operator payment details (CLAUDE.md).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _session_integrity import read_hook_input, load_state, save_state, log  # noqa: E402
from rule_index_state import (  # noqa: E402
    hash_signature, last_delivered, record_delivery, row_signature,
)

_HOOK_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _HOOK_DIR.parent.parent
_LIB_DIR = _REPO_ROOT / "lib"

# Smaller than session_rule_index.py's 9,800: this hook shares the same
# measured 10,000-char per-hook stdout cap but fires every turn, not only at
# lifecycle events, so it is deliberately conservative — a delta injection
# should never approach the size of a full-index delivery in practice, and
# leaving more headroom costs nothing on the common (small-delta) path.
BUDGET_CHARS = 9000


def _log(msg: str) -> None:
    sys.stderr.write(f"[session-rule-delivery] {msg}\n")


def _fetch_rows_and_skills():
    """Live (session-scoped raw rows, session-scoped PolicySkills), or None
    on fail-open. Mirrors session_rule_index.py's own import-and-fetch shape
    exactly — same functions, same order, same exception handling.

    `fetch_active_policy_rows` already returns rows run through
    `agent_loader.unwrap_policy_entities` — flat snapshot dicts with
    `_entity_id` stamped on directly, NOT `{"entity_id": ..., "snapshot":
    {...}}` — so `_session_scope_ok` (which reads `scope`/`agent_sub` off
    the snapshot) is called on each row directly, not on `row["snapshot"]`.
    """
    try:
        if str(_REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(_REPO_ROOT))
        if str(_LIB_DIR) not in sys.path:
            sys.path.insert(0, str(_LIB_DIR))
        from lib.daemon_runtime.policy_skill_renderer import (  # noqa: PLC0415
            fetch_active_policy_rows,
            render_skills,
            _session_scope_ok,
        )
    except Exception as exc:  # noqa: BLE001 — fail open (e.g. no httpx)
        _log(f"renderer unavailable: {type(exc).__name__}: {exc}")
        return None

    try:
        rows = fetch_active_policy_rows()
    except Exception as exc:  # noqa: BLE001 — fail open on transport failure
        _log(f"could not reach Neotoma: {type(exc).__name__}: {exc}")
        return None

    try:
        scoped_rows = [r for r in rows if _session_scope_ok(r)]
        skills = render_skills(rows)
    except Exception as exc:  # noqa: BLE001 — fail open on a renderer bug
        _log(f"could not scope/render rows: {type(exc).__name__}: {exc}")
        return None

    return scoped_rows, skills


def _mandatory_full_block(entity_id: str, body: str) -> str:
    return f"### Mandatory rule changed/added [{entity_id}]\n{body}\n"


def _advisory_summary_line(skill) -> str:  # noqa: ANN001 — PolicySkill, imported lazily
    trigger = skill.applies_when if skill.applies_when else "(trigger not recorded)"
    return f"- When {trigger}: {skill.description} [{skill.entity_id}]"


_HEADER = (
    "# Rule index update (agent_policy changed since last delivered to this "
    "session, ateles#1261 follow-up)\n"
)
_CLOSER = (
    "\nFetch the full rule from Neotoma by entity id before acting on a "
    "conditional rule above (agent_policy.rule; do not act on the one-line "
    "summary alone)."
)


def _render_delta(added_or_changed: list, budget_chars: int) -> str:
    """added_or_changed: list[PolicySkill]. Mandatory rows rendered in full
    first (sorted by entity_id for determinism), advisory rows as one-line
    summaries, until the budget runs out — never cuts a rendered row
    mid-way, only drops whole rows and names the omitted count (same
    degrade-loudly posture as policy_skill_renderer.render_index_text)."""
    mandatory = sorted(
        (s for s in added_or_changed if s.rule_kind == "mandatory"),
        key=lambda s: s.entity_id,
    )
    advisory = sorted(
        (s for s in added_or_changed if s.rule_kind != "mandatory"),
        key=lambda s: s.entity_id,
    )
    ordered = [("mandatory", s) for s in mandatory] + [("advisory", s) for s in advisory]

    def _fits(n_blocks: int, n_omitted: int) -> bool:
        blocks = [
            _mandatory_full_block(s.entity_id, s.body) if kind == "mandatory"
            else _advisory_summary_line(s)
            for kind, s in ordered[:n_blocks]
        ]
        tail = f"\n({n_omitted} more changed rule(s) omitted for space.)" if n_omitted else ""
        text = _HEADER + "\n".join(blocks) + _CLOSER + tail
        return len(text) <= budget_chars

    kept = 0
    for n in range(len(ordered), -1, -1):
        if _fits(n, len(ordered) - n):
            kept = n
            break

    blocks = [
        _mandatory_full_block(s.entity_id, s.body) if kind == "mandatory"
        else _advisory_summary_line(s)
        for kind, s in ordered[:kept]
    ]
    omitted = len(ordered) - kept
    tail = f"\n({omitted} more changed rule(s) omitted for space.)" if omitted else ""
    return _HEADER + "\n".join(blocks) + _CLOSER + tail


def main() -> int:
    ev = read_hook_input()
    session_id = ev.get("session_id", "")
    if not session_id:
        return 0

    fetched = _fetch_rows_and_skills()
    if fetched is None:
        return 0  # fail open — nothing to compare against, say nothing
    scoped_rows, skills = fetched

    current_sig = row_signature(scoped_rows)
    current_hash = hash_signature(current_sig)

    state = load_state(session_id)
    prior_hash, prior_rows = last_delivered(state)

    if prior_hash == current_hash:
        return 0  # unchanged set — inject nothing (per spec)

    changed_ids = {
        eid for eid, ts in current_sig.items() if prior_rows.get(eid) != ts
    }
    if changed_ids:
        by_id = {s.entity_id: s for s in skills}
        added_or_changed = [by_id[eid] for eid in changed_ids if eid in by_id]
        if added_or_changed:
            print(_render_delta(added_or_changed, BUDGET_CHARS))

    # Record what we just observed (the full current signature, not only the
    # changed subset) so the NEXT prompt's diff is against everything now
    # known, not just what happened to change this turn.
    save_state(session_id, record_delivery(state, scoped_rows))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail open, never block a prompt
        log(f"session_rule_delivery hook error (ignored): {exc}")
        sys.exit(0)
