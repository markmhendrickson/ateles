#!/usr/bin/env python3
"""PreToolUse hook — inject the full text of high-risk rules at point of use.

Audit `ent_b66293f0dcc8c887d4fdbeae` (rule delivery audit, 2026-09-26) found
the session-start index's own closing instruction — "fetch the full rule by
id before acting on a conditional rule" — was followed 1 time in 17
applicable occasions (6%). The index gives a trigger line or a one-line
summary and trusts the model to notice it needs more and go fetch it; that
trust does not hold. Two of the audit's own violations are exactly the
actions this hook targets: a grant write reported "verified" from a
shape-only read-back that left `entity_types: []` (all Ateles signed writes
silently disabled for ~2 hours), and a subagent that rewrote Cursor's
`mcp.json` to a stdio script because its brief never carried the rule
governing that file. Recommendation 5 of that audit is this hook.

MECHANISM (measured, not inferred). `PreToolUse`'s `additionalContext` field
is placed into the model's context as its own `<system-reminder>` block,
immediately before the gated tool executes — confirmed by a controlled probe
against Claude Code 2.1.283 (a scratch project with a `PreToolUse` hook
returning `additionalContext`, driven headlessly via `claude -p`, with the
canary string it emitted read back out of the session transcript's rendered
attachment). `docs/foundation/harness_carriers.md` names pre-action hooks as
a Guards carrier but never establishes this as a Rules delivery channel; this
is that measurement. Unlike the `deny` calls in `git_stash_guard.py` /
`gmail_send_gate.py` / `refuse_task_chip.py`, this hook always allows — its
job is injection, not refusal, so exit code is always 0 and
`permissionDecision` is always `allow`.

TARGET CATEGORIES (from the audit's recommendation 5), matched on the
gated tool call, never on free-text `applies_when`:

  - grant_write:    a Neotoma write whose payload targets `agent_grant`
  - policy_write:   a Neotoma write whose payload targets `agent_policy` or
                    `relationship_type`
  - harness_config: an Edit/Write/NotebookEdit whose file_path, OR a Codex
                    `apply_patch` payload naming a path it will Add/Update/
                    Delete/Move to, is one of the operator's own harness
                    config files (~/.cursor/mcp.json, ~/.claude/settings.json,
                    ~/.neotoma/aauth) — the #2482 breakage this audit traced
  - advisory:       a `gh` CLI call that reads or writes a GitHub security
                    advisory

CATEGORY -> RULE IDS is a small local map (`_CATEGORY_RULE_IDS` below), never
rule TEXT: only entity ids are named here, mirroring
`install_codex_hooks.py`'s `MANAGED_SCRIPT_NAMES` precedent for "a short
local list of identifiers is fine; the governed content stays in the single
source." The hook fetches those rows live every time (same readers
`session_rule_index.py` uses: `fetch_active_policy_rows` / `render_skills`,
which resolves this session's own agent identity and live `GOVERNS` edges
before scope-filtering — see `_fetch_rows_by_id`) and renders `.body` (the
FULL `rule` text) — so an edit to a rule's content in Neotoma is picked up on
the next matching action with no code change, and a rule retired or
rescoped away from this session simply stops being injected.

GRANT WRITES ALSO GET A PROBE REMINDER. The audited failure was not just a
missing rule — it was a write reported "verified" from a read-back that
checked the field's *shape*, not whether the grant still ADMITS. This hook
appends one line naming the same-turn probe pattern documented in the rule
delivery evals (`get_session_identity` before AND after an `agent_grant`
edit; the read-back is the second call, not a shape check on the first). It
does not attempt to detect whether the probe actually happens — that is a
downstream audit's job (the eval harness's `check_grant_admission`), not a
gate that fires before the write itself even lands.

FAIL-OPEN, STDLIB-ONLY, NEVER LOGS A RULE BODY TO STDERR (only entity ids and
counts — same posture as session_rule_index.py; some `agent_policy` rows
carry operator payment details, CLAUDE.md).

Wire under PreToolUse with matcher
"Edit|Write|NotebookEdit|mcp__mcpsrv_neotoma__correct|mcp__mcpsrv_neotoma__store|Bash"
under Claude Code (no separate MultiEdit tool name in this Claude Code
version — every other hook in this directory matches the same four-tool
set), and additionally "apply_patch" under Codex
(`.codex/hooks.json`) — Codex's native file-edit tool, parsed via
`_apply_patch_paths` (delegated to `sibling_repo_worktree_guard.py`'s parser
of the same name, not re-implemented) so a harness_config edit made through
`apply_patch` is caught the same way an Edit/Write is under Claude Code.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _session_integrity import read_hook_input  # noqa: E402
from action_capabilities import classify_tool_call  # noqa: E402

_HOOK_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _HOOK_DIR.parent.parent
_LIB_DIR = _REPO_ROOT / "lib"

# Category -> the agent_policy entity ids whose FULL TEXT this category
# injects. Content lives in Neotoma; only ids are named here (see module
# docstring). Extend this map, never hardcode rule prose here.
_CATEGORY_RULE_IDS: dict[str, tuple[str, ...]] = {
    "grant_write": ("ent_1c0cbb99d2c8011358ff1dc3",),
    "policy_write": ("ent_1c0cbb99d2c8011358ff1dc3", "ent_82b64b6c4104843e43853666"),
    "harness_config": ("ent_c4d33237ff2d12b4aaec71af", "ent_663888501a290e9aaf60270c"),
    "advisory": ("ent_e774dddc392478472c3a84c6",),
    "publication": ("ent_f67e021874c1e1595dbbe768",),
}

_HARNESS_CONFIG_PATH_RE = re.compile(
    r"(\.cursor/mcp\.json|\.claude/settings(\.local)?\.json|\.neotoma/aauth)"
)

# --------------------------------------------------------------------------
# Bounded Bash command-shape classifier for harness_config.
#
# A bare `_HARNESS_CONFIG_PATH_RE.search(command)` matches the path STRING
# anywhere in the command — including inside a `git diff`/`git show`/`git
# log` read, a `cat`/`rg`/`grep` inspection, or a `gh` comment body that
# merely quotes the path. Accipiter's current-head UX review on PR #1320
# reproduced full rule-body injection from exactly those shapes (task
# ent_70038a4a54c9bbc82d606774): read-only commands that never write a byte
# to the file still fired the block, training the same skim-past reflex the
# hook exists to break, at a higher frequency than the real hazard warrants.
#
# The fix narrows matching to command SHAPES that can actually mutate the
# target path, using the same segment-split + text-bearing-leader-exemption
# pattern as `gmail_send_gate.py` (mirrored deliberately — one convention
# for "judge a Bash command per compound-segment, per shape" in this repo,
# not two that can drift):
#
#   - Split on `&&`/`;`/`|`/newline so a mutation hidden after an innocuous
#     first segment is still caught, and fold `\<newline>` continuations so
#     a wrapped mutating command stays one segment.
#   - A segment counts as a harness_config MUTATION only when the path
#     pattern is present AND the segment matches one of a bounded set of
#     mutation shapes: output redirection (`>`/`>>` with the path on the
#     redirect's target side — checked first, see below), `sed -i`, `tee`,
#     `cp`/`mv`/`install`/`rsync` naming the path, `git
#     checkout`/`restore`/`apply`/`stash pop` touching the path, or a
#     direct removal/edit (`rm`, `truncate`, `chmod`) naming the path. This
#     is an AFFIRMATIVE allowlist, not a leader-exemption blocklist: a
#     segment that merely mentions the path with no recognized mutation
#     shape — a `git diff`/`git show`/`git log`/`git blame` pathspec, a
#     `cat`/`rg`/`grep`/`ls`/`head` read, a `gh pr comment`/`view`/`diff`
#     body or a `git commit -m` message quoting it — defaults to NOT
#     matching, which is what keeps read-only inspection and prose quiet
#     without needing to enumerate every possible read-only command.
#   - Redirection is checked BEFORE the read-only-leader exemption list
#     below, because `echo`/`printf` are the only leaders in that list that
#     can themselves also write a file via shell redirection —
#     `echo '{}' > .claude/settings.json` must still count as a mutation
#     even though a bare `echo "mentions the path"` must not. Every OTHER
#     mutation shape is checked AFTER the leader exemption: none of their
#     leaders (`sed`, `tee`, `cp`, ...) appears in the exemption list, so a
#     mutation keyword occurring only as PROSE behind a read-only leader
#     (e.g. a `gh pr comment --body "uses sed -i to patch
#     .claude/settings.json"`) is correctly still exempt rather than being
#     reclassified as a write — checking the general mutation-shape set
#     before the leader exemption was tried and reverted for exactly this
#     false-positive.
#   - The read-only-leader list itself (`git diff`, `git show`, `git log`,
#     `git blame`, `git commit`/`tag`/`notes`, `cat`, `less`, `head`,
#     `tail`, `rg`, `grep`, `ls`, `gh pr`/`issue`
#     `comment`/`view`/`diff`/`list`, `echo`, `printf`) exists only to keep
#     the AFTER-redirect mutation shapes (`sed`, `tee`, `cp`, ...) from
#     matching when they appear as prose inside one of these commands'
#     arguments — it is not itself the source of truth for "mutating",
#     since the affirmative allowlist already defaults everything else to
#     not-matching.
#
# This intentionally does NOT attempt full shell parsing (no subshell/
# quoting-aware tokenizer) — same bounded-heuristic posture as
# `gmail_send_gate.py` and `git_stash_guard.py`, which is why every
# exemption and mutation shape below is proven by a paired red/green test
# rather than trusted from the regex alone.
_SEGMENT_SPLIT = re.compile(r"&&|[;\n|]")

_READ_ONLY_LEADERS = re.compile(
    r"^(?:"
    r"git\s+(?:diff|show|log|blame|grep|status|cat-file|commit|tag|notes)\b"
    r"|cat|less|more|head|tail|wc|file"
    r"|rg|grep|ag|ack"
    r"|ls|find\s+.*-name"
    r"|gh\s+(?:pr|issue)\s+(?:comment|view|diff|list)"
    r"|echo|printf"
    r")\b"
)

# Output redirection is checked separately from, and BEFORE, the leader
# exemption — see the "Redirection" bullet above for why only this one
# shape is allowed to override an `echo`/`printf` leader.
_REDIRECT_TO_PATH_RE = re.compile(
    r">>?\s*[\"']?[^|;&\n]*" + _HARNESS_CONFIG_PATH_RE.pattern
)

# Every other bounded mutation shape. Applied only to segments that survive
# `_READ_ONLY_LEADERS` — unlike the redirect check above, none of these
# leaders (`sed`, `tee`, `cp`, ...) appears in the exemption list, so
# checking them after the leader exemption cannot let mutation prose behind
# a read-only leader (e.g. a `gh pr comment` body that merely says "sed -i")
# re-trigger a match.
_OTHER_MUTATION_SHAPE_RE = re.compile(
    r"(?:"
    r"\bsed\b.*-i\b"
    r"|\btee\b"
    r"|\b(?:cp|mv|install|rsync)\b"
    r"|\bgit\s+(?:checkout|restore|apply|stash\s+pop)\b"
    r"|\brm\b"
    r"|\btruncate\b"
    r"|\bchmod\b"
    r")"
)


def _join_line_continuations(command: str) -> str:
    r"""Fold `\<newline>` sequences so a continued command stays ONE segment
    (mirrors `gmail_send_gate.py`'s helper of the same name)."""
    return re.sub(r"\\[ \t]*\n", " ", command)


def _bash_touches_harness_config(command: str) -> bool:
    """True iff some segment of `command` is a mutation-capable operation
    whose target is a harness-config path — never true for a segment that
    only reads, inspects, or quotes the path as text.

    Order matters: redirection is checked first (the one shape that can
    hide behind an exempt `echo`/`printf` leader), then the closed
    read-only-leader exemption, then every other mutation shape. Checking
    the general mutation-shape set BEFORE the leader exemption was tried
    and reverted — it reintroduced false positives for prose mentioning a
    mutation keyword near the path (e.g. a `gh pr comment` body describing
    a `sed -i` fix), the same class of bug this classifier exists to fix.
    """
    for segment in _SEGMENT_SPLIT.split(_join_line_continuations(command)):
        normalized = " ".join(segment.split())
        if not normalized:
            continue
        if not _HARNESS_CONFIG_PATH_RE.search(normalized):
            continue
        if _REDIRECT_TO_PATH_RE.search(normalized):
            return True
        if _READ_ONLY_LEADERS.match(normalized):
            continue
        if _OTHER_MUTATION_SHAPE_RE.search(normalized):
            return True
    return False


# --------------------------------------------------------------------------
# Advisory classifier.
#
# A bare `security[-_]advisor|/security-advisories\b` substring search over
# the WHOLE command matches the words wherever they occur — including inside
# a `gh pr comment`/`issue create` body or title that merely MENTIONS
# "security-advisory" as prose. Accipiter's current-head UX review on PR
# #1320 reproduced this live: `gh pr comment 1320 --body "this PR touches
# security-advisory handling in the linter"` fired the full advisory
# injection even though the command neither reads nor writes an actual
# GitHub security advisory — the identical false-positive SHAPE the
# harness_config Bash fix above exists to eliminate, left live one category
# over (the prior test suite even asserted this false positive as correct
# behavior — `test_bash_gh_advisory_prose_matches_advisory`, now flipped).
#
# `gh` has no dedicated `security-advisory` subcommand; the only way to
# actually read or write a GitHub security advisory through the `gh` CLI is
# `gh api` (REST, with the advisories path in the endpoint argument) or
# `gh api graphql` (a GraphQL query/mutation naming a
# `securityAdvisor(y|ies)` field). So — mirroring `_bash_touches_harness_config`
# above — this checks the command's SHAPE: a `gh api` invocation whose
# endpoint argument contains the advisories path, or a `gh api graphql` call
# whose query/mutation body names a `securityAdvisor` field. A `gh
# pr`/`issue`/`repo` subcommand whose --body/--title/--message TEXT merely
# contains the words never matches, because those subcommands are not `gh
# api` at all — same "affirmative shape, not substring-anywhere" posture as
# the harness_config fix, using the SAME segment-split
# (`_SEGMENT_SPLIT`/`_join_line_continuations`) so a mutation hidden after an
# innocuous first segment is still caught.
# --------------------------------------------------------------------------
_GH_API_LEADER_RE = re.compile(r"^gh\s+api\b")
_ADVISORY_PATH_RE = re.compile(r"/security-advisories\b", re.IGNORECASE)
_GRAPHQL_ADVISORY_FIELD_RE = re.compile(
    r"securityAdvisor(y|ies)\b", re.IGNORECASE
)


def _gh_segment_touches_advisory(segment: str) -> bool:
    """True iff one already-`&&`/`;`/`|`/newline-split command segment is an
    actual `gh api` call reading or writing a GitHub security advisory —
    never true for a `gh pr`/`issue`/... subcommand whose free-text body
    only mentions the words."""
    if not _GH_API_LEADER_RE.match(segment):
        return False
    if _ADVISORY_PATH_RE.search(segment):
        return True
    # `gh api graphql -f query='... securityAdvisories { ... }'` — the
    # advisories path never appears (GraphQL has no REST path), so the
    # query/mutation body's field name is the only signal.
    if "graphql" in segment and _GRAPHQL_ADVISORY_FIELD_RE.search(segment):
        return True
    return False


def _bash_touches_advisory(command: str) -> bool:
    """Segment-split counterpart to `_bash_touches_harness_config` for the
    `advisory` category — see the module comment above
    `_gh_segment_touches_advisory`."""
    for segment in _SEGMENT_SPLIT.split(_join_line_continuations(command)):
        normalized = " ".join(segment.split())
        if not normalized:
            continue
        if _gh_segment_touches_advisory(normalized):
            return True
    return False


def _apply_patch_paths(command: str) -> list[str]:
    """Every path an `apply_patch` payload says it will mutate.

    Delegates to `sibling_repo_worktree_guard.py`'s own `_apply_patch_paths`
    — imported lazily (not at module load time) so a broken sibling module
    degrades this ONE category to "no match" rather than crashing the whole
    hook before `main()`'s fail-open guard can catch it, and imported (not
    re-implemented) so the two hooks can never silently drift on what an
    apply_patch payload's path syntax is (CLAUDE.md "extend the mechanism
    that already generalizes"; Falco, PR #1320 round 2: this hook's
    harness_config category was file-path-based by design and the parser
    already existed next door)."""
    try:
        from sibling_repo_worktree_guard import (  # noqa: PLC0415
            _apply_patch_paths as _shared_apply_patch_paths,
        )
    except Exception:  # noqa: BLE001 — fail open (see docstring)
        return []
    return _shared_apply_patch_paths(command)


_GRANT_ENTITY_TYPES = {"agent_grant"}
_POLICY_ENTITY_TYPES = {"agent_policy", "relationship_type"}


def _log(msg: str) -> None:
    sys.stderr.write(f"[rule_injection_gate] {msg}\n")


def _neotoma_entity_types_touched(tool_name: str, tool_input: dict) -> set[str]:
    """Entity types a Neotoma `correct`/`store` call's payload targets.

    `correct` carries a bare `entity_type` field. `store` carries a list of
    entity dicts under `entities`, each with its own `entity_type` — reads
    both shapes since one hook covers both tools (module docstring).
    """
    if not tool_name.endswith("__correct") and not tool_name.endswith("__store"):
        return set()
    types: set[str] = set()
    et = tool_input.get("entity_type")
    if isinstance(et, str) and et:
        types.add(et)
    entities = tool_input.get("entities")
    if isinstance(entities, list):
        for e in entities:
            if isinstance(e, dict):
                et2 = e.get("entity_type")
                if isinstance(et2, str) and et2:
                    types.add(et2)
    return types


def _file_path_from(tool_input: dict) -> str:
    for key in ("file_path", "notebook_path"):
        v = tool_input.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def _bash_command(tool_input: dict) -> str:
    v = tool_input.get("command")
    return v if isinstance(v, str) else ""


def matched_categories(tool_name: str, tool_input: dict) -> list[str]:
    """Every category this ONE tool call belongs to, in a stable order.

    A single call can match more than one category (e.g. a `store` writing
    both an `agent_grant` and an `agent_policy` row in the same request) —
    every matching category's rules are injected, not just the first.
    """
    capabilities = classify_tool_call(tool_name, tool_input)
    injection_categories = _CATEGORY_RULE_IDS.keys()
    return [
        action_class
        for action_class in capabilities.action_classes
        if action_class in injection_categories
    ]


def _fetch_rows_by_id(ids: set[str]) -> dict[str, dict]:
    """Live-fetch active `agent_policy` rows, keyed by entity id, filtered to
    `ids`. Returns {} on any transport/import failure (fail-open — the
    caller then injects nothing rather than block or crash)."""
    if not ids:
        return {}
    try:
        if str(_REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(_REPO_ROOT))
        if str(_LIB_DIR) not in sys.path:
            sys.path.insert(0, str(_LIB_DIR))
        from lib.daemon_runtime.policy_skill_renderer import (  # noqa: PLC0415
            fetch_active_policy_rows,
            render_skills,
        )
    except Exception as exc:  # noqa: BLE001 — fail open (e.g. no httpx)
        _log(f"renderer unavailable: {type(exc).__name__}: {exc}")
        return {}

    try:
        rows = fetch_active_policy_rows()
    except Exception as exc:  # noqa: BLE001 — fail open on transport failure
        _log(f"could not reach Neotoma: {type(exc).__name__}: {exc}")
        return {}

    # `render_skills(rows)` is the SAME entry point `session_rule_index.py`
    # calls: with no `agent_definition_id=`/`governs=` supplied, it resolves
    # this session's own `agent_definition` id (`resolve_agent_definition_id`
    # against `session_principal()`) and fetches the live `GOVERNS` edge map
    # (`fetch_governs_edges`) itself, THEN applies `_session_scope_ok` with
    # those resolved values before projecting to `PolicySkill` (`to_skill`).
    # Calling the bare `_session_scope_ok(row)` here — as an earlier revision
    # did — passes neither, so every row (including one edged to a DIFFERENT
    # agent) is scored as if it had no `GOVERNS` edge at all: a
    # `global`/`swarm`-scoped row edged only to another agent then falls
    # through to the swarm-wide branch and is wrongly treated as binding
    # this session, even though an edge is supposed to override scope
    # (`policy_binds_agent_by_edge`'s own doc: "an edge overrides rather
    # than adds to" scope). Reusing `render_skills` end to end is what keeps
    # this hook and the SessionStart index from silently disagreeing about
    # who may see a row.
    skills = render_skills(rows)
    out: dict[str, dict] = {}
    for skill in skills:
        if skill.entity_id in ids:
            out[skill.entity_id] = skill
    return out


_GRANT_PROBE_REMINDER = (
    "\n\nThis is an agent_grant write. Per the rule delivery audit "
    "(ent_b66293f0dcc8c887d4fdbeae): a shape-only read-back of the field you "
    "wrote is not verification — it proved the write landed, not that the "
    "grant still ADMITS. Call the identity/session probe (e.g. "
    "get_session_identity as the affected agent) BOTH before and after this "
    "write, and only report the change verified if the AFTER probe still "
    "admits the access the agent needs."
)


def _render_context(
    category_to_rows: dict[str, dict[str, object]],
) -> tuple[str, list[str]]:
    blocks: list[str] = []
    injected_ids: list[str] = []
    for category in sorted(category_to_rows):
        rows = category_to_rows[category]
        for eid in sorted(rows):
            skill = rows[eid]
            blocks.append(f"### [{category}] rule {eid}\n{skill.body}")
            injected_ids.append(eid)
    text = (
        "Point-of-use rule injection (ateles rule delivery audit "
        "ent_b66293f0dcc8c887d4fdbeae, recommendation 5): this action matches "
        "a high-risk category, so the full text of the governing rule(s) is "
        "below rather than left to a fetch you may not make.\n\n"
        + "\n\n".join(blocks)
    )
    if "grant_write" in category_to_rows:
        text += _GRANT_PROBE_REMINDER
    return text, injected_ids


def main() -> int:
    ev = read_hook_input()
    tool_name = str(ev.get("tool_name") or "")
    tool_input = ev.get("tool_input")
    if not tool_name or not isinstance(tool_input, dict):
        return 0

    categories = matched_categories(tool_name, tool_input)
    if not categories:
        return 0

    wanted_ids: set[str] = set()
    for c in categories:
        wanted_ids.update(_CATEGORY_RULE_IDS.get(c, ()))

    rows = _fetch_rows_by_id(wanted_ids)
    category_to_rows = {
        c: {eid: rows[eid] for eid in _CATEGORY_RULE_IDS.get(c, ()) if eid in rows}
        for c in categories
    }
    # Even with no row fetched (Neotoma unreachable), a grant write still gets
    # the probe reminder — that instruction does not depend on Neotoma.
    if not any(category_to_rows.values()) and "grant_write" not in categories:
        return 0

    context, injected_ids = _render_context(category_to_rows)
    _log(
        f"tool={tool_name} categories={','.join(categories)} "
        f"injected={','.join(injected_ids) or '(none — fail-open)'}"
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "additionalContext": context,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail open, never block a tool call
        _log(f"error (ignored): {exc}")
        sys.exit(0)
