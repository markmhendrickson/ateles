#!/usr/bin/env python3
"""Render the operator's standing rules into Codex's own load point.

The gap this closes
-------------------
`CLAUDE.md` is re-injected from disk on every compaction, which is why the
standing session rules live there rather than in conversation. Codex has an
equivalent load point — the root `AGENTS.md`, whose contents Codex inlines into
the developer message — and on 2026-09-13 that file was **0 bytes**. A Codex
session therefore started with none of the operator's rules in force: not
``NEVER `git stash` ``, not `One worktree, one agent`, not the consent gates.
The rules existed, in a file Codex never reads.

Why a generated copy, and not the two alternatives
--------------------------------------------------
**Symlink `AGENTS.md` -> `CLAUDE.md`.** Staleness becomes impossible, which is
the one real advantage, and it was rejected anyway: `CLAUDE.md` is
Claude-Code-specific in places that matter. It documents hooks by filename
(`git_stash_guard.py`, `decision_shape_gate.py`), names `.claude/settings.json`
as the wiring, and cites tool names (`spawn_task`, `mcp__mcpsrv_neotoma__*`)
that do not exist in Codex. A Codex session reading that verbatim is told a
mechanism enforces a rule when in that harness nothing does — which is worse
than silence, because it invites relying on a guard that is not there.

**A short pointer file.** Also rejected as the sole mechanism. Codex inlines
the root `AGENTS.md` into the developer message, so a pointer puts the
*pointer* in force, not the rules; whether the rules bind then depends on the
model choosing to follow it. A rule whose enforcement is optional is the
condition this script exists to end. The rendered file carries the pointer too,
but as a supplement to the rules, never instead of them.

So: a generated copy that carries every rule **identity** from `CLAUDE.md`,
with the enforcement mechanisms translated into what actually holds in Codex,
and a staleness check that fails when `CLAUDE.md` grows a rule this file lacks.

How staleness is detected
-------------------------
By reusing `scripts/verify_claude_md_merge.py`'s own rule extractor rather than
writing a second parser. That matters for more than economy: `lint.sh` already
gates `CLAUDE.md` on those exact rule identities (the **bolded lead** of each
bullet), so a rule that cannot go missing from `CLAUDE.md` also cannot go
missing here without this check seeing it. Two parsers would drift, and the
drift would land precisely on the rules whose names nobody thought to check —
the ateles#973 failure over again.

`--check` compares the rule keys in `CLAUDE.md` against those in the rendered
`AGENTS.md` and fails on any rule present in the former and absent from the
latter. It is deliberately **one-directional**: the rendered file adds
Codex-specific material of its own (the harness-translation section), and
flagging those as drift would train the reader to ignore the check.

Exit codes
----------
0  rendered (no flag), or the rendered file carries every rule (`--check`)
1  `--check` found a rule in CLAUDE.md absent from AGENTS.md
2  usage or input error — the check did NOT run, which is not the same as
   passing (`docs/foundation/principles.md#5`)

Usage
-----
    execution/scripts/render_codex_agents_md.py            # render
    execution/scripts/render_codex_agents_md.py --check    # CI parity gate

Stdlib only. No network, no Neotoma. Reads `CLAUDE.md`; writes only the
`AGENTS.md` it is pointed at.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path
from typing import NoReturn

EXIT_USAGE = 2

REPO_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
EXTRACTOR = REPO_ROOT / "scripts" / "verify_claude_md_merge.py"

# Codex reads the root AGENTS.md from its home directory. Overridable so a test
# never writes to the operator's real Codex home.
DEFAULT_AGENTS_MD = Path(
    os.environ.get("CODEX_HOME", Path.home() / ".codex")
) / "AGENTS.md"

# Marks the generated region. Everything between the markers is owned by this
# script; anything a human adds outside them survives a re-render, so an
# operator note is never silently eaten.
BEGIN = "<!-- BEGIN GENERATED: ateles standing rules -->"
END = "<!-- END GENERATED: ateles standing rules -->"


def die(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(EXIT_USAGE)


def load_extractor():
    """Import `verify_claude_md_merge` for its rule extractor.

    Deliberately reused rather than reimplemented: `lint.sh` gates CLAUDE.md on
    these exact rule identities, and a second parser would drift from the first
    exactly where it matters least visibly.
    """
    if not EXTRACTOR.exists():
        die(
            f"cannot find the rule extractor at {EXTRACTOR}. "
            "The check did NOT run."
        )
    name = "_ateles_claude_md_extractor"
    spec = importlib.util.spec_from_file_location(name, EXTRACTOR)
    if spec is None or spec.loader is None:
        die(f"cannot load {EXTRACTOR}. The check did NOT run.")
    module = importlib.util.module_from_spec(spec)
    # Register before executing: the extractor defines @dataclass classes, and
    # dataclasses resolves a class's own module out of sys.modules while
    # processing it. Executing an unregistered module raises AttributeError on
    # the first @dataclass — which cost a debugging round on Python 3.14.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        del sys.modules[name]
        die(f"cannot execute {EXTRACTOR}: {exc}. The check did NOT run.")
    return module


# The header. States what the file is, that it is generated, and where the
# canonical rules live — so a Codex session that wants the full text can get it.
HEADER = """# Ateles / Neotoma — standing operator instructions (Codex)

**This file is generated. Do not hand-edit inside the generated markers.**
Source of truth: `CLAUDE.md` on `origin/main` of the `ateles` repo.
Regenerate with `execution/scripts/render_codex_agents_md.py`; `--check` holds
it equal to its source in `scripts/lint.sh`.

These are standing operator instructions, not suggestions. Each was set or
re-stated because it was broken and cost real work. They are reproduced here
because Codex inlines this file into every session, the same way Claude Code
re-injects `CLAUDE.md` after every compaction — an instruction that lives only
in conversation is lost at the first context boundary.

## What is different about running in Codex

`CLAUDE.md` documents several rules as *mechanically enforced* by Claude Code
hooks (`git_stash_guard.py`, `gmail_send_gate.py`,
`sibling_repo_worktree_guard.py`, `decision_shape_gate.py`,
`stop_finalizer.py`). **Those hooks do not run in Codex.** Codex has its own
hook mechanism (`session_start_command` / `session_end_command` in
`~/.codex/config.toml`), and as of this writing it carries session-anchoring
only, not any of the guards.

So in Codex every rule below is enforced by **you reading it**, with no
mechanism behind it. Where a rule names a hook as its enforcement, treat the
rule as fully binding and the hook as absent. The two that have bitten hardest
with no guard in place:

- **`git stash` is not blocked here.** Never run it in any form.
- **A Gmail send is not gated here.** No `gws gmail ... drafts update`,
  `drafts send`, or `messages send` without a per-message operator approval.

## Resuming a workstream

To pick up an Ateles or Neotoma workstream that stopped mid-flight, use the
`continue-session` skill (`~/.codex/skills/continue-session/`). It derives
present state rather than asserting it, and it carries these same constraints.
"""

# The rules. Each bullet's **bolded lead** is the rule identity the parity
# check keys on, so the lead text must match CLAUDE.md's exactly. Bodies are
# condensed and Claude-specific enforcement is rewritten; the identity is not.
RULES = """
## Hard constraints — these hold regardless of workstream

- **NEVER `git stash`** in any form — the stash stack is shared across worktrees and other sessions pop it. Use a WIP commit. Repeat this in every agent brief; the ambient warning alone has not held. In Claude Code `.claude/hooks/git_stash_guard.py` blocks it; **in Codex nothing does.**
- **One worktree, one agent.** Never point two agents at the same worktree: the second's uncommitted work reads to the first as an unknown session mid-edit, and it stops or clobbers. Sequence behind the push, or give the new agent its own worktree.
- **Verify before asserting.** Check the live system of record, not a cached copy or a local checkout — a stale worktree reported as current has already produced wrong diagnoses here. Report a claim as verified only when you just checked it.
- **Never hardcode secrets, IBANs, or contact details** — always read from env or parquet.
- **Merge stays gated; product releases, credentials, sends, and grants keep their existing gates.** Do not merge where a live blocking review stands — live means its finding is unaddressed, not merely that it exists. Routine deployments, including production and client-instance deployments, may proceed after retrieving the current `deployment_configuration` and verifying the established target, command, authority, budget, checks, action gates, and read-back. Product release decisions remain the operator's: publishing a version, tag, package, or release notes requires explicit approval. A hosting provider's deployment revision or “release” record is deployment evidence, not a product release decision. Credential rotation, new access grants, and outward sends retain their existing gates.
- **Never mark a task or todo `done` while citing a commit, branch, file, or PR that does not resolve.** Verify the artifact exists first (git `cat-file`/`ls-remote`, GitHub). Unverifiable completion claims are the second failure mode that corrupted the swarm plan in June 2026.

Both `ateles` and `neotoma` are **public** repositories. No PII, no credential
values, no client names, and no operator home paths in anything committed,
pushed, or filed.

## Session conduct — how the operator wants to be worked with

- **Dispatch, don't work inline.** Create a Neotoma `task` entity and let an agent claim it; use a subagent only where no swarm path exists. This binds to *all* work — research, analysis, design, payments, investigation — not only code. Work you recommend is work you file, in the same turn you recommend it, without waiting to be asked. Reserve the session for judgement and conversation. A harness task chip is not an entity, so it is unclaimable and invisible to the swarm.
- **Dispatch, don't drift inline.** Work that belongs to an agent goes to an agent. The failure is drift — one small step at a time until a session has done an agent's whole job itself. A finding you can't act on gets dispatched or filed, never spawned as a task chip for Mark to click.
- **Summarize what the operator said at the top of each reply,** cleaned up. Most operator input arrives as live voice transcription, which garbles names and can fabricate whole sentences; echoing what was heard is how the operator catches it. Do this even when the turn seems routine.
- **Give status updates unprompted** — what moved, what is blocked, and one recommended next step *per workstream*, so the operator can confirm whether to stop that workstream for now.
- **Maintain a live session workboard.** Every Ateles-led operator session with managed work maintains one stable `session_digest` as a derived workboard. Link it to the conversation, paramount plan, subordinate workstreams, and current tasks; group work as active, queued, blocked, done, and operator-needed; record executor and next action; refresh it after material state changes and before accepting substantial new work. Plans and tasks remain authoritative. Show the operator a compact workboard table when state changes or when requested. The full implementation is owned by its dedicated task; do not build a parallel status store.
- **Name the related task entities whenever discussing work,** and link each by id so the operator can open it. Work discussed without a task id cannot be tracked or found again.
- **Proceed with your recommendation — don't ask.** When you have a recommended course of action, take it and report what you did, not what you considered. Stop only at a genuine fork where context can't settle it, or where the consent gate binds independently (public sends, email, irreversible external actions, closing someone else's PR). Laying out a recommendation and asking permission to execute it IS the violation. If you asked something and it went unanswered, re-surface it each turn until it is answered rather than dropping it.
- **Escalate choices by material operator outcome.** Ask the operator only when plausible options would materially change their goals, risk, cost, external commitments, data loss, or irreversible state and existing context does not select among them. Advance mechanical, representational, and technical choices with equivalent operator outcomes automatically or send them to the appropriate review lens. Routine deployments, including production and client-instance deployments, are not operator-reserved when the current `deployment_configuration` is retrieved, the target and command are established, existing credentials and grants suffice, spend stays within the authorized budget, checks and action gates are clear, and no live blocking review remains. The operator controls product release decisions: publishing a version, tag, package, release notes, or otherwise approving the release workflow requires explicit approval. A hosting provider's deployment revision or “release” record created as a consequence of deployment is deployment evidence, not a product release decision. Credential rotation, new access grants, outward sends, live-blocking-review resolution, and actions another explicit rule reserves retain their existing gates.
- **End every turn with the decisions that need Mark**, with enough context to decide: the options, what each implies, what is already settled. Never re-raise one by name alone. Keep repeating them each turn until answered. Carry every open decision, not just the most recent.
- **A decision handed to the operator carries three things and nothing else**: the choice stated as options with what each implies; what is already settled, so he is not re-deriving context; and a recommendation, plus what happens if he says nothing. Naming the category instead of the choice is the failure — "open design decision", "operator-only by rule" are labels, not decisions. Never write "unchanged" for a carried decision: restate the choice. Avoid internal shorthand — decision numbers, phase labels, and file-and-line citations belong in the issue, not in a question the operator answers from a phone.
- **For an operator-only action, give the exact command and what to verify after.** Operator-only means the operator runs it, not that he works out what to run. A runnable block, then the check that confirms it took effect.
- **Classify a blocker before surfacing it.** When a pull request or task is blocked, decide which kind it is. **Dispatch and report** — never ask — when the blocker is a verified, specced deliverable: a missing column an issue already names, a review whose finding is already discharged, a rebase, a regeneration, a stale reference. **Surface it** only when it needs a judgement that cannot be derived: an acceptance standard, an unruled design question, or a merge where a live blocking objection stands.
- **Fix the swarm rather than routing around it.** Repair a broken daemon, gate, or dispatch path rather than doing its job by hand, and do so without asking. When the defect belongs to a dependency, file an issue against that repo with a test rather than writing an instruction that works around it.
- **Contextualize before executing.** Dispatched work must first check existing tasks, issues, PRs, and the codebase, so the swarm neither duplicates work nor re-decides settled questions.
- **Capture reusable operator feedback as a standing rule.** When operator feedback establishes a recurring failure, ambiguity, or better general operating pattern, persist or merge it into the durable `standing_rule` set in the same workstream; do not wait for a separate request to remember it. Preserve every prior valid constraint, keep a one-off preference in its narrower context rather than generalizing it, and verify the active rule is read back through the canonical MCP instruction path. A harness-local skill may not override a more authoritative operator instruction; reconcile the skill at its source when they conflict.
- **Advance fully verified mechanical gates automatically.** A phase or exit gate whose predicates are fully mechanical and live-cross-checked is progression, not a new operator decision. Record the gate as met and continue without asking unless a governing rule explicitly reserves sign-off to the operator, a separately gated action remains, or the evidence leaves a substantive choice unresolved. A stored status, self-report, or single unchecked measurement is not enough.
- **`docs/foundation/` states the design, not the implementation.** A Neotoma field name, a schema value, what a daemon happens to do — none is evidence for a design claim. A glossary check is not a design check: verifying a term is defined says nothing about whether it is the right term. Where design and implementation names differ, the doc takes the design's and `migration.md` records the mapping.
- **Restart daemons as needed, without asking.** Standing authorization, 2026-09-11. A merged fix reaches nothing until the checkout a daemon runs from is updated and the process restarts. Daemons run from `~/ateles-rc-src` (Ateles) and `~/neotoma-rc-src` (Phoenicurus release), never the shared clone. The sequence is `git fetch && git merge --ff-only origin/main` in the deployment checkout, then `launchctl unload` and `launchctl load` the plist, then VERIFY from the running process — a new pid, and the merged change present in the file the daemon loaded. Restart only the daemons the change touches, and say which. Credential rotation remains operator-only; a restart does not authorize a product release.
- **A plain comment does NOT re-dispatch a swarm review.** Every `issue_comment` event routes to the operator-override command handler, so a comment carrying no command reaches a handler that finds nothing and stops. `PR_ACTIONS = {"opened", "reopened", "synchronize"}` are the only events that re-run a PR review. Only the two operator-gated slash commands drive the pipeline from a comment; otherwise a push or a review event is what moves it.
- **Re-request review yourself whenever monitoring shows it is warranted — do not wait to be asked.** Standing authorization, 2026-09-11. The way to request one is a push or a close-and-reopen; a comment cannot do it. Warranted means every blocking finding is discharged in substance, checks are green, and the PR is otherwise mergeable. NOT warranted, and these stay Mark's: a live finding nobody has answered, or an adjudication this session made that no lens has seen. Say on the PR why it was reopened, so the reopen is not mistaken for flapping.
- **Comment `/confirm-gates-clear` yourself when a PR is blocked only by swarm MECHANICS, never when it is blocked by JUDGEMENT.** Standing authorization, 2026-09-11. Qualifying: a verdict the parser cannot read, a gate never initialized, a trigger routed to a handler that does nothing with it, a lens that never ran. NOT qualifying, and these stay Mark's: a live blocking finding no lens has withdrawn; an adjudication made by this session rather than by a lens; a gate whose owning lens has not looked at the current head. Say in the comment which mechanical failure is being cleared and what evidence shows the work itself is sound.
- **Prefer a genuine re-review over waiving a gate.** A gate waive is for a PR blocked by swarm *mechanics* where no lens judgement is obtainable; it substitutes session judgement for a lens's. Where a lens objected and its objection has been answered, ask the lens rather than skipping it.

> The last two rules pull against each other, and `CLAUDE.md` marks the pair as
> deliberately unresolved: one grants a standing authorization to waive, the
> other treats waiving as a last resort. Follow the **stricter** reading —
> prefer a real re-review wherever one is available, and treat a waive as
> available only when the pipeline itself is mechanically broken. Do not
> resolve the tension by reasoning it away; it is the operator's call.

## Plan and task maintenance

Each session maintains the Neotoma `plan` entity matching **its own
workstream** — never a fixed, hardcoded plan. Resolve it once per session with
`retrieve_entities` (`entity_type: plan`, searching the workstream); create one
if none fits; maintain only that plan thereafter. Writing one workstream's
`decisions`/`todos` into another's is the collision that corrupted the swarm
plan in June 2026. Always use Neotoma **prod**, never the dev instance.

- **Before correcting `decisions` or `todos`, RE-READ the current field and MERGE.** `correct` replaces the *entire* field, so add or update only the keys/items you authored and preserve every entry already present. NEVER rebuild a field from a stale in-memory copy — that silently deletes other sessions' entries. If the field changed since you last read it, re-read and re-merge before writing.
- **When a todo item is completed** — correct its `status` to `"done"` in `todos`, with relevant entity IDs, file paths, or PR numbers in a `notes` field.
- **When a decision is settled** — add or correct one entry in the `decisions` map. One sentence, snake_case key.
- **When blockers change** (something unblocked, something newly blocked) — correct `next_steps` to reflect the current state.
- **When a new actionable task is identified** — create a `task` entity and link it `PART_OF` the bound plan.
- **When a daemon, entity, or file is renamed** — correct any stale references in `body`, `decisions`, and `todos` in the same turn the rename happens.

Apply these in the same turn as the work, not at end of session.

## Standing constraints on what may be written where

- **Plan-mirrored docs are render targets, not source files.** `docs/taxonomy.md`, `docs/phases.md`, and `docs/architecture.md` mirror plan fields. Never edit these files directly: correct the plan field, then run `python3 execution/scripts/render_plan_docs.py`; run `--check` before committing them.
- **`docs/foundation/decision_state.md` is generated, never authored.** It projects the decision register and is held equal to its source by `--check` in `scripts/lint.sh`. The fix for a wrong row is in the register it reads or in the generator, never in the output.
- **Agent prompts are always public and PII-free.** `agent_definition.prompt_markdown` and its skill mirror describe how an agent reasons and acts — never operator data. No payee names, IBANs, BTC addresses, contact names/emails, phone numbers, addresses, health facts, or financial figures. Operator-specifics live in Neotoma entities retrieved at runtime.
- **Agent prompts describe a role generically; specifics come from context entities.** A prompt states what the agent *does*, not who it does it for. Identity, jurisdiction, products, roster, vendors, channels, and deploy targets all resolve at runtime from context entities. The test is the fork test: if another operator cloning this repo would have to edit code rather than supply an entity, the specifics are in the wrong place. Always give a missing-entity fallback that degrades safely or surfaces a blocker — never one that silently substitutes a default.
- **Operator-specific config is env/Neotoma-sourced, never baked into code.** Operator identity, calendar IDs, recipients, and entity IDs that vary per operator are read at runtime so the swarm stays portable — not literals in daemon code. Prefer the Neotoma entity as the single source, and never copy a set of values into code where it can drift. When a copy is genuinely needed for import hygiene, derive it from the single source at import time or assert equality in a test — a comment claiming two constants match is not a mechanism that keeps them matching.
- **A renamed agent leaves no reference behind.** When an agent is renamed, every reference must move in the same change — a retired name in routing code sets an owner no dispatcher can resolve. Prompts and skill mirrors are generated: fix a stale name on the `agent_definition` entity, never on disk.
- **Never deploy a hosted client instance from memory or from a repo doc alone — retrieve its `deployment_configuration` first.** The repo doc is the *generic* method and deliberately names no client; the per-instance binding is the canonical source. Never add client-identifying deploy config to a public repo.
- **Strip PII before filing issues** — scrub usernames, worktree names, and platform names; use `visibility: private` for session-derived issues.
- **Yoga payments: never include memo/OP_RETURN** — do not pass a `memo` parameter.
- **Yoga/therapy tasks: never mark as completed** — only update `due_date`.

## Which checkout to work from

Daemons run **dedicated checkouts**, never the shared main clones where
interactive sessions work: `~/ateles-rc-src` for Ateles daemons,
`~/neotoma-rc-src` for the Phoenicurus release. The shared clones
(`~/repos/ateles`, `~/repos/neotoma`) are for sessions and are dirty most of
the time. Two consequences when debugging a daemon: verify against the checkout
the daemon actually runs from, not the worktree you are editing in; and a
merged fix does nothing until that checkout is updated.

Never write into a sibling repo's shared main clone. Create a dedicated
worktree first: `git worktree add ~/repos/<repo>-wt-<slug> origin/main`.

## Google Workspace

Prefer the `gws` CLI over any Gmail/Workspace MCP — it wraps the full REST API
and is consistently more capable. Before any **irreversible or outward-facing**
action — sending mail, permanently deleting, changing sharing or ACLs — confirm
with the operator unless he already authorized that specific action this
session. Reads, searches, and draft creation need no confirmation.

**`gws gmail users drafts update` can SEND.** Re-supplying `raw` + `threadId`
can consume a staged draft into a sent message. To edit a staged draft, build a
NEW draft rather than updating in place.

- **Gmail**: always use `gws gmail ...` commands, not the Gmail MCP server.
- **Google Calendar**: always use `gws` CLI with `Europe/Madrid` timezone.
- **Always use Neotoma prod** (`mcp__mcpsrv_neotoma__*` or the Neotoma CLI), never the dev instance.

## Verification discipline — standing engineering rules

Each was derived from three or more independent failures on a single day
(2026-09-02). `CLAUDE.md` scopes them to interactive sessions rather than
dispatched agents — **a Codex session is an interactive session, so they bind
here.** Where a rule names its enforcement, note that the Claude Code linters
and hooks are not running in Codex: apply all of these by hand.

- **A mechanism that does not bind is not a control.** Before treating a linter, gate, review, or status as enforcement, check that something actually fails when it is violated. Ways this repo has produced non-binding controls: a linter registered only in `scripts/lint.sh` that no workflow invokes; a workflow step carrying `continue-on-error: true`, or a lane triggered on `pull_request` paths only and so never on push to main; a verdict posted as an **issue comment**, which GitHub does not enforce; and a status field asserting liveness with no lease behind it, so a killed runner pins a task permanently. When you add a control, name the thing that fails. If nothing fails, you have written documentation.
- **A write that reports success has not necessarily happened. Read it back.** Neotoma `/store` accepts undeclared fields and silently routes them to `raw_fragments`, so a store succeeds and drops the field — four separate agents hit this in one day, and one nesting mistake produced hundreds of empty entity shells. After any write that matters, retrieve it and assert the specific field you wrote is present with the value you wrote. Never treat a 2xx or `success: true` as evidence.
- **Validate the instrument before believing the measurement.** A zero, an empty result, or a silent pass is a claim about your tooling before it is a claim about the world, and a surprising number is usually the tool. Three independent false zeros landed on one day from querying the wrong field name, reading a markdown string as a dict, and keying on a bare repo name where the data stores `owner/repo`. Prove the instrument non-zero on a case you know is positive before reporting a zero, and anchor process-matching patterns so they exclude `test_*` and sibling sessions.
- **A test that cannot fail on the thing it watches is decoration.** Before trusting a test as coverage, revert the fix and confirm the test goes red. A test written against current behaviour ratifies the bug — one asserted a renamed agent's old name, pinning the bug as expected behaviour. For any test offered as proof of a fix, say what it looked like red.
- **Fail closed on the field that carries the safety meaning.** When a value is absent, unrecognized, or malformed, the default must be the *restrictive* branch, and it must be so for the field that encodes the risk. A live instance had `operator_only` missing from both blast-radius vocabularies, so it fell through to LOW — the more confident an agent was that a task required a human, the more certainly it auto-executed. When adding a value to a safety vocabulary, add it to *both* sides, and test that the default branch is the restrictive one.
- **Extend the mechanism that already generalizes; do not build a parallel one.** Before building, look for the thing that already does this — and search the code, not your memory of it. Three times in one day work was commissioned that already existed. This includes *types*: reuse the existing relationship or entity type rather than minting one, and keep a name that is already accurate rather than renaming for tidiness.
- **Never bypass the pre-commit hook with `--no-verify`.** When a commit must land with tests skipped, use the hook's own escape hatch: `SKIP_TESTS=1 SKIP_TESTS_REASON="<why>"`. `--no-verify` silently skips every check, so a self-inflicted failure arrives later looking environmental. The named reason keeps the skip attributable.

## People-data processing (RGPD legitimate-interest basis)

Storage of third-party personal data for relationship management runs under
**RGPD Art. 6(1)(f) legitimate interest**, NOT the household exemption — the
data drives professional action toward those people. Apply as standing
discipline:

- **Minimize at capture.** When storing a person from a transcript or meeting, retain what serves the relationship (role, context, commitments, follow-ups). Do NOT persist incidental sensitive disclosures — health, finances, family situations, political/religious views (RGPD Art. 9 categories) — into durable contact profiles unless directly relevant to a stored task. Summarize, don't transcribe verbatim, when the detail is sensitive and incidental.
- **Purpose-bind.** Enrichment is for managing the operator's actual relationships. Do not build profiles on people with no relationship to the operator.
- **Honor objection.** If a person asks not to be tracked, or asks what's held, treat it as an Art. 21 objection / Art. 15 access request: stop enrichment on that entity and surface it to the operator. Never argue the person down.
- **No external publication of person-data** without the operator's explicit per-case approval.

Recording a call the operator is **not** a party to is a hard refusal — it loses
both the US one-party-consent basis and the Spain Art. 197 safe harbor.
"""

# Rule identities in CLAUDE.md that are deliberately NOT carried here, each
# with the reason. This list is the honest alternative to a blanket filter: a
# suppression nobody has to justify is how a real rule goes missing. Anything
# not listed and not rendered fails the check.
#
# Two kinds only:
#   1. A bullet whose bolded lead is a FILENAME, in a section documenting a
#      Claude Code hook or linter. These are descriptions of mechanisms that do
#      not exist in Codex, not instructions. The rules they enforce ARE carried
#      — `git_stash_guard.py` is excluded while ``NEVER `git stash` `` is
#      rendered — so excluding the description loses nothing that binds.
#   2. A "Recently resolved" changelog entry: a historical note about work that
#      finished, not a standing instruction.
DELIBERATE_OMISSIONS: dict[str, str] = {
    "session_start.py": "Claude Code hook filename; mechanism absent in Codex",
    "user_prompt_submit.py": "Claude Code hook filename; absent in Codex",
    "stop_finalizer.py": "Claude Code hook filename; absent in Codex",
    "scripts/verify_claude_md_merge.py": (
        "repo linter description, not a session instruction"
    ),
    ".claude/hooks/hook_wiring_reference.py": (
        "Claude Code hook filename; absent in Codex"
    ),
    "decision_shape_gate.py": "Claude Code hook filename; absent in Codex",
    "sibling_repo_worktree_guard.py": (
        "Claude Code hook filename; absent in Codex. The rule it enforces is "
        "carried as prose under 'Which checkout to work from'."
    ),
    "gmail_send_gate.py": (
        "Claude Code hook filename; absent in Codex. The rule it enforces is "
        "carried under 'Google Workspace', including that drafts update can "
        "send."
    ),
    "Secrets management — SOPS+age, snapshots in the PRIVATE `ateles-private` "
    "repo (Design B)": "Recently-resolved changelog entry, not a rule",
    "Issues sync runaway": "Recently-resolved changelog entry, not a rule",
    "Post-checkout hook in worktrees": (
        "Recently-resolved changelog entry, not a rule"
    ),
}


def render() -> str:
    return f"{HEADER}\n{BEGIN}\n{RULES.strip()}\n{END}\n"


def rule_keys(module, text: str, label: str) -> dict[str, object]:
    return module.extract(text, label).rules


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="render_codex_agents_md.py",
        description=(
            "Render the operator's standing rules into Codex's AGENTS.md, or "
            "verify the rendered file still carries every CLAUDE.md rule."
        ),
    )
    p.add_argument(
        "--check",
        action="store_true",
        help=(
            "Verify only: fail if a rule in CLAUDE.md is absent from the "
            "rendered AGENTS.md. Writes nothing."
        ),
    )
    p.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_AGENTS_MD,
        help=f"AGENTS.md to render or check (default: {DEFAULT_AGENTS_MD}).",
    )
    p.add_argument(
        "--source",
        type=Path,
        default=CLAUDE_MD,
        help=f"CLAUDE.md to read rules from (default: {CLAUDE_MD}).",
    )
    args = p.parse_args(argv)

    module = load_extractor()

    if not args.source.exists():
        die(f"cannot read {args.source}. The check did NOT run.")
    source_text = args.source.read_text(encoding="utf-8")
    source_rules = rule_keys(module, source_text, str(args.source))

    if not args.check:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(render(), encoding="utf-8")
        print(f"rendered {args.out} ({args.out.stat().st_size} bytes)")
        return 0

    if not args.out.exists():
        print(
            f"FAIL: {args.out} does not exist. Codex loads its standing rules "
            "from this file; an absent file means a Codex session starts with "
            "no operator rules in force. Run "
            "execution/scripts/render_codex_agents_md.py to create it."
        )
        return 1

    rendered_text = args.out.read_text(encoding="utf-8")
    if not rendered_text.strip():
        print(
            f"FAIL: {args.out} is empty (0 bytes). This is the exact state "
            "found on 2026-09-13: the load point existed and carried nothing, "
            "so a Codex session ran with no operator rules. Run "
            "execution/scripts/render_codex_agents_md.py."
        )
        return 1

    rendered_rules = rule_keys(module, rendered_text, str(args.out))

    # Headings are structural, not rules. The two files are deliberately
    # organized differently — the Codex rendering groups by what binds rather
    # than by which hook enforces it — so gating on headings would report a
    # reorganization as a lost rule. Only bullet rules are gated.
    #
    # One-directional by design in the other axis too: the rendered file adds
    # Codex-specific material CLAUDE.md has no reason to carry, and reporting
    # that as drift would train the reader to ignore this check.
    omitted = {module.normalize(k): v for k, v in DELIBERATE_OMISSIONS.items()}

    missing = []
    honoured: list[tuple[object, str]] = []
    for key, rule in source_rules.items():
        if rule.kind != "bullet" or key in rendered_rules:
            continue
        if key in omitted:
            honoured.append((rule, omitted[key]))
        else:
            missing.append(rule)

    # A stale omission fails the gate rather than widening it, the same way
    # verify_claude_md_merge.py treats its dedup records. An entry naming a
    # rule that no longer exists in CLAUDE.md, or one now rendered anyway, is
    # a suppression nobody is checking.
    stale = []
    for key, reason in omitted.items():
        if key not in source_rules:
            stale.append(
                f"  {key!r} is recorded as deliberately omitted but no longer "
                f"exists in {args.source.name}. Remove the entry."
            )
        elif key in rendered_rules:
            stale.append(
                f"  {key!r} is recorded as deliberately omitted but IS "
                f"rendered in {args.out.name}. Remove the entry."
            )
    if stale:
        print(f"FAIL: {len(stale)} stale omission record(s)")
        for line in stale:
            print(line)
        print()

    if honoured:
        print(
            f"Deliberate omissions honoured ({len(honoured)}) — each is a "
            "mechanism description or changelog entry, never a standing rule:"
        )
        for rule, reason in honoured:
            print(f"  **{rule.lead}** — {reason}")
        print()

    if missing:
        print(
            f"FAIL: {len(missing)} standing rule(s) in {args.source} are "
            f"absent from {args.out}"
        )
        for rule in missing:
            print(
                f"  **{rule.lead}**\n"
                f"      {args.source} line {rule.line}, absent from "
                f"{args.out.name}"
            )
        print(
            "\nEach line is a rule a Codex session would not have in force. "
            "Add it to RULES in execution/scripts/render_codex_agents_md.py "
            "(keeping the bolded lead identical to CLAUDE.md's, since that is "
            "the key this check compares), then re-render."
        )

    if missing or stale:
        return 1

    suffix = f", {len(honoured)} deliberate omission(s)" if honoured else ""
    print(
        f"OK: {args.out.name} carries every standing rule in "
        f"{args.source.name} ({len(source_rules)} rule identities "
        f"checked{suffix})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
