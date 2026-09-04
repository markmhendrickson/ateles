# Conformance: how work binds to the foundation

**Keyed document:** read when `docs/foundation/` changes. **Kind:** foundation; describes the binding
mechanism (built as ateles#744), which is evergreen by construction: it names documents and paths, never
what a checkout implements. **Derived from:** plan `ent_533d4ec2f7bfb60f66fb3fce`
decisions `binding_is_the_reviewer_reading_a_kernel_not_a_loading_order` and
`ateles_binding_extends_three_existing_mechanisms`, synthesis `ent_b0ce322f768e4fc676b73139` (PR-29, C7),
prior art `ent_08460968e6f49dac21510f4a` (OPA: decision decoupled from enforcement), and PR #745
operator review (2026-09-04).

## Purpose

State how issue-based swarm work is checked against the design documents in
`docs/foundation/`: which documents a reviewer reads on every change, which it
reads when particular paths change, what a design-basis statement is, and what
happens to an issue that conforms to nothing.

This is the reading list. It is consumed by code, not only by people: the arch
lens and the other review lenses load it at review time
(`execution/daemons/apis/foundation.py`), every runner's system prompt
names it (`skill_runner.SWARM_FOUNDATION_CONTRACT`), and the issue spec's
design-basis section is checked against it. Neotoma's equivalent is
`docs/developer/pr_review_reading_list.md`.

## Scope

The `docs/foundation/` directory of this repository. Direction of truth for
these documents is the repository: they are authored here and reviewed in PRs,
not rendered from a Neotoma plan. Plan `decisions` maps remain the event log of
when a decision was taken; a foundation document is the consolidated, reviewed
statement. When they disagree, the document is wrong until a PR corrects it, and
the PR is the review.

`docs/architecture.md`, `docs/taxonomy.md`, and `docs/phases.md` are outside
this scope: they are render targets of the architecture plan and are corrected
through it.

`docs/foundation/status.md` is in this directory and deliberately not in the
reading list. It is the dated, perishable measurement of the gap between the
foundation and a checkout; it is regenerated, never maintained. Foundation
documents may name `status.md` only as the non-authoritative home of
implementation-state measurements (a pointer). They must not embed dated
figures, counts, commit SHAs, or checkout claims from it as design evidence.
The parser reads only the tables below, so it is never inlined; a reviewer who
needs state reads it by hand and checks its as-of date first.

## Always read

The kernel: at most three documents, loaded on every review regardless of what
changed. Three is a budget, not a count of what matters. Neotoma's reading list
records that a six-document always-read set consumed the reviewer's turns before
any diff was read; every other document is keyed to a path below.

| Doc | What it states |
|-----|----------------|
| `docs/foundation/principles.md` | The invariants: a mechanism that does not bind is not a control; a write that reports success has not necessarily happened; a test that cannot fail on the thing it watches is decoration; fail closed on the field that carries the safety meaning; unknown stays distinct from a verdict. |
| `docs/foundation/work_model.md` | How work moves: pull is the only delivery, assignment constrains eligibility; the claim and the lease are one primitive, the lease a relationship; liveness derived from activity at read time; tasks aggregate into passages and nest under parents; artifacts are records a passage leaves, never its subject. |
| `docs/foundation/gates_and_workflows.md` | The step and gate model: `workflow` declares steps, a `passage` carries tasks through them, a step's state is derived from the passage, a lease, and a `sign-off`, `step_status` projects; one step set; actions are entities and only actions execute; the execution gate decides per action; advisory and enforcing paths agree. |

A kernel document that is not yet written is reported to the reviewer as such.
It states nothing; that domain is reviewed on the lens's standing criteria, and
a change is never blocked for lacking a citation to a document that does not
exist.

## Read when these paths changed

The first cell holds regular expressions matched anywhere in a changed path
(the same convention as `Lens.diff_patterns` in `review_panel.py`); the second
names the documents to read. A document is read at most once per review, kernel
first.

### Work and workflows

| Changed path | Read |
|---|---|
| `lib/daemon_runtime/task_claim`, `lib/daemon_runtime/task_lifecycle`, `execution/daemons/apis/apis\.py`, `execution/daemons/apis/task_watchdog`, `execution/daemons/apis/routing` | `docs/foundation/work_model.md` |
| `execution/daemons/apis/swarm_dispatch`, `execution/daemons/apis/review_panel`, `execution/daemons/apis/issue_spec`, `lib/daemon_runtime/workflow_resolver`, `lib/issue_labels`, `lib/daemon_runtime/checkpoint`, `lib/daemon_runtime/gating`, `execution/daemons/anthus/`, `execution/mcp/ateles/` | `docs/foundation/gates_and_workflows.md` |
| `execution/daemons/apis/skill_runner`, `execution/daemons/apis/harness_router` | `docs/foundation/work_model.md`, `docs/foundation/gates_and_workflows.md` |

### Failure posture

| Changed path | Read |
|---|---|
| `lib/daemon_runtime/neotoma_reachability`, `lib/daemon_runtime/checkout_drift`, `lib/neotoma`, `lib/daemon_runtime/readiness`, `lib/neotoma_forensics` | `docs/foundation/failure_posture.md` |

### Authority

| Changed path | Read |
|---|---|
| `lib/daemon_runtime/agent_loader`, `grant_checker`, `aauth_signer`, `lib/approval`, `lib/notify`, `lib/daemon_runtime/checkpoint_posture`, `execution/mcp/ateles/`, `execution/mcp/mcp_tool_grant_proxy`, `execution/daemons/apis/a2a_` | `docs/foundation/authority_model.md` |

### Vocabulary and agent instructions

| Changed path | Read |
|---|---|
| `\.claude/skills/.*/SKILL\.md$`, `docs/agents/`, `execution/scripts/render_agent_docs` | `docs/foundation/vocabulary.md` |

### Scenarios

| Changed path | Read |
|---|---|
| `lib/daemon_runtime/task_claim`, `lib/daemon_runtime/task_lifecycle`, `execution/daemons/apis/task_watchdog`, `lib/daemon_runtime/workflow_resolver`, `lib/daemon_runtime/gating` | `docs/foundation/scenarios.md` |

### The foundation itself

| Changed path | Read |
|---|---|
| `docs/foundation/` | `docs/foundation/conformance.md`, `docs/foundation/scenarios.md` |

## Design basis

Every issue and every PR states its design basis: the foundation document and
section it conforms to, or the explicit statement that no design applies.

- On an issue, the design basis is the first section of the swarm
  specification (`issue_spec.py`, section `basis`). The pm step's owner states it
  at intake, from the kernel; the arch step's owner checks it.
- On a PR, the design basis is one line in the body:
  `Design basis: docs/foundation/work_model.md#claim-and-lease`, or
  `Design basis: no design applies — <one line why>`.

What the step owners check, in order:

1. Mechanically, before judgement (`foundation.check_design_basis`): the
   statement exists; every `docs/foundation/` path it cites is on the checkout;
   or it declares `no design applies`. A missing or invalid basis is a
   `[BLOCKING] design-basis` finding.
2. By reading: the step owner opens the cited section and judges whether the change
   conforms to it. A citation is not conformance. A change that contradicts the
   cited document is blocked citing the document by path.
3. A declaration that no design applies is judged too: if a kernel or keyed
   document does govern the change, the declaration is false and blocking.

## An issue that conforms to nothing

An issue whose design basis cannot name a document, and for which no design
applies, is visible at intake rather than at audit. It is not closed at the
pm step; it is one of five dispositions the audit applies in batch with operator
approval: conforms, align (the ask is right, the framing names no basis),
close (conforms to nothing), supersede (already a step in a sibling plan), or
premature (work for a later vision phase, marked with that phase and kept
open).

## Direction of truth per class of record

Rules and decisions have four homes, chosen for four audiences (synthesis C7). Each class has one
authoritative side; the others are mirrors or restatements that are wrong until corrected. (An
`artifact` in `vocabulary.md` is a record in an external system that a passage leaves; the classes
below are the swarm's own records, and the word is not used for them.)

| Class of record | Authoritative side | Mirror or restatement |
|---|---|---|
| Implementation state: what is built, what fails open, every count | `docs/foundation/status.md`, as of its date, regenerated from `origin/main` and the record | none; a foundation document that states implementation state is wrong |
| A design invariant, the work model, the step and gate model, the failure posture, the authority model | this directory, repo-authored, PR-reviewed | plan `decisions` maps hold the event log of when each was decided; `CLAUDE.md`'s session-only section restates six of the principles for interactive sessions and says it does not bind agents the swarm runs |
| An agent's behavioural rule | `agent_policy` entities in Neotoma | `.claude/skills/<name>/SKILL.md` and `docs/agents/*.md`, rendered by `render_agent_docs.py` |
| The system's composition, roster, and roadmap | plan `ent_99ace4dd6673aa36ed08b1fe` fields | `docs/architecture.md`, `docs/taxonomy.md`, `docs/phases.md`, rendered by `render_plan_docs.py` |
| A session's standing instruction | `CLAUDE.md` (re-injected after compaction) | none |

A rule that belongs to two classes is written once in its authoritative home and cited from the other, never
copied: a comment or a second document claiming to mirror the first is not a mechanism that keeps them
matching (`principles.md`, invariant 9).

## Amending a foundation document

A foundation document changes through a PR that cites the plan decision it
consolidates (plan entity id and decision key), so the event log and the
reviewed statement stay traceable to each other. That PR is reviewed like any
other; the keyed entry above ensures this document is read when it is.

## Phases and implementation state

The foundation is phase-agnostic. Each document defines its part of the design
whole, marks a decision the operator has not taken as **open** with its options,
and says nothing about which parts a checkout implements. The README's vision
phases (P1 governed execution for one principal, P2 multi-operator identity and
ownership, P3 delegation and approval, P4 distributed authority and initiative,
P5 organizational operating system) are a roadmap over that one design, and
`status.md` records, as of its date, which sections of which documents are built,
which are designed and unbuilt, and which are open. Implementation phases
(`docs/phases.md`, Phase 0 to 9) are the other axis and are never merged with
the roadmap: an implementation item cites the foundation document and section it
implements, not a vision phase.

An implementation item whose section is marked open in the foundation is
premature: it waits on the decision, marked with the roadmap phase that decision
belongs to, and stays open. An item with no citable section is either covered by
the design as written, in which case it cites the foundation, or conforms to
nothing (above).

Two rules keep the split: a foundation document never carries "today", "on main",
a commit hash, a count, or an open-issue reference as evidence a defect is live;
and an issue or PR is cited in a foundation document only as the record of a
decision (a merged PR, an operator-authored issue), never as state. A PR that
adds either to a foundation document is blocked on this section.
