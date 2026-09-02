# Conformance: how work binds to the foundation

**Vision phase:** P1 (governed execution for one principal). **Kind:** consolidation, not design: this
document describes the binding ateles#744 built. **Derived from:** plan `ent_533d4ec2f7bfb60f66fb3fce`
decisions `binding_is_the_reviewer_reading_a_kernel_not_a_loading_order` and
`ateles_binding_extends_three_existing_mechanisms`, synthesis `ent_b0ce322f768e4fc676b73139` (PR-29, C7),
prior art `ent_08460968e6f49dac21510f4a` (OPA: decision decoupled from enforcement).

## Purpose

State how issue-based swarm work is checked against the design documents in
`docs/foundation/`: which documents a reviewer reads on every change, which it
reads when particular paths change, what a design-basis statement is, and what
happens to an issue that conforms to nothing.

This is the reading list. It is consumed by code, not only by people: the arch
gate and the review lenses load it at review time
(`execution/daemons/apis/foundation.py`), every dispatched agent's system prompt
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

## Always read

The kernel: at most three documents, loaded on every review regardless of what
changed. Three is a budget, not a count of what matters. Neotoma's reading list
records that a six-document always-read set consumed the reviewer's turns before
any diff was read; every other document is keyed to a path below.

| Doc | What it states |
|-----|----------------|
| `docs/foundation/principles.md` | The invariants: a mechanism that does not bind is not a control; a write that reports success has not necessarily happened; a test that cannot fail on the thing it watches is decoration; fail closed on the field that carries the safety meaning; unknown stays distinct from a verdict. |
| `docs/foundation/work_model.md` | How work moves: pull over push; the claim and the lease are one primitive; liveness derived at read time; the transition vocabulary. |
| `docs/foundation/gates_and_workflows.md` | The gate model: `workflow_definition` declares, `participation_record` instantiates, `gate_status` projects; one gate-set constant; advisory and enforcing paths agree. |

A kernel document that is not yet written is reported to the reviewer as such.
It states nothing; that domain is reviewed on the lens's standing criteria, and
a change is never blocked for lacking a citation to a document that does not
exist.

## Read when these paths changed

The first cell holds regular expressions matched anywhere in a changed path
(the same convention as `Lens.diff_patterns` in `review_panel.py`); the second
names the documents to read. A document is read at most once per review, kernel
first.

### Work and dispatch

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

### The foundation itself

| Changed path | Read |
|---|---|
| `docs/foundation/` | `docs/foundation/conformance.md` |

## Design basis

Every issue and every PR states its design basis: the foundation document and
section it conforms to, or the explicit statement that no design applies.

- On an issue, the design basis is the first section of the swarm
  specification (`issue_spec.py`, section `basis`). The pm gate states it at
  intake, from the kernel; the arch gate checks it.
- On a PR, the design basis is one line in the body:
  `Design basis: docs/foundation/work_model.md#claim-and-lease`, or
  `Design basis: no design applies — <one line why>`.

What the gates check, in order:

1. Mechanically, before judgement (`foundation.check_design_basis`): the
   statement exists; every `docs/foundation/` path it cites is on the checkout;
   or it declares `no design applies`. A missing or invalid basis is a
   `[BLOCKING] design-basis` finding.
2. By reading: the gate opens the cited section and judges whether the change
   conforms to it. A citation is not conformance. A change that contradicts the
   cited document is blocked citing the document by path.
3. A declaration that no design applies is judged too: if a kernel or keyed
   document does govern the change, the declaration is false and blocking.

## An issue that conforms to nothing

An issue whose design basis cannot name a document, and for which no design
applies, is visible at intake rather than at audit. It is not closed by the
gate; it is one of five dispositions the audit applies in batch with operator
approval: conforms, align (the ask is right, the framing names no basis),
close (conforms to nothing), supersede (already a step in a sibling plan), or
premature (work for a later vision phase, marked with that phase and kept
open).

## Direction of truth per artifact class

Rules and decisions have four homes, chosen for four audiences (synthesis C7). Each class has one
authoritative side; the others are mirrors or restatements that are wrong until corrected.

| Artifact class | Authoritative side | Mirror or restatement |
|---|---|---|
| A design invariant, the work model, the gate model, the failure posture, the authority model | this directory, repo-authored, PR-reviewed | plan `decisions` maps hold the event log of when each was decided; `CLAUDE.md`'s session-only section restates six of the principles for interactive sessions and says it does not bind dispatched agents |
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

## Vision phase

Each foundation document carries in its header the vision phase it belongs to
(P1 governed execution for one principal, P2 multi-operator identity and
ownership, P3 delegation and approval, P4 distributed authority, P5
organizational operating system). Implementation items cite the vision-phase
document they implement; an item with no citable document is either P1, in
which case it cites the foundation, or premature, in which case it is marked
with its phase and waits.
