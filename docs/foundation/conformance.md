# Conformance: how work binds to the foundation

**Keyed document:** read when `docs/foundation/` changes. **Kind:** foundation; describes the binding
mechanism (built as ateles#744): names documents and paths, never what a checkout implements.
**Derived from:** plan `ent_533d4ec2f7bfb60f66fb3fce` decisions
`binding_is_the_reviewer_reading_a_kernel_not_a_loading_order` and
`ateles_binding_extends_three_existing_mechanisms`, synthesis `ent_b0ce322f768e4fc676b73139` (PR-29, C7),
prior art `ent_08460968e6f49dac21510f4a` (OPA: decision decoupled from enforcement), and PR #745 operator
review (2026-09-04).

## Purpose

State how issue-based swarm work is checked against `docs/foundation/`: which documents a reviewer reads
on every change, which it reads when particular paths change, what a design-basis statement is, what
happens to an issue that conforms to nothing, and which mechanical checks hold this directory to its own
rules. Consumed by `execution/daemons/apis/foundation.py`, `skill_runner.SWARM_FOUNDATION_CONTRACT`, and
the issue-spec design-basis check. Neotoma's equivalent is `docs/developer/pr_review_reading_list.md`.

## Scope

The `docs/foundation/` directory. Direction of truth is the repository: these documents are authored here
and reviewed in PRs, not rendered from a plan. Plan `decisions` maps remain the event log of when a
decision was taken; when they disagree, the document is wrong until a PR corrects it, and the PR is the
review.

Out of scope: `docs/architecture.md`, `docs/taxonomy.md`, `docs/phases.md` (architecture-plan render
targets, corrected through the plan).

`docs/foundation/status.md` is colocated and deliberately **not** in the reading list: dated, perishable
measurement, regenerated. Foundation docs may name it as the state home only; they must not embed its
figures as design evidence. The parser reads only the tables below, so it is never inlined; a reviewer
who needs state reads it by hand and checks its as-of date first.

`docs/foundation/workflows.md`, `docs/foundation/scenarios.md`, and
`docs/foundation/scenarios_extended.md` are authored companions: they bind via `workflow` entities +
`render_workflow_docs.py --check` (workflows) or as walkthroughs of the kernel (scenarios), and are
currently **not** inlined into review prompts. Whether they are keyed is a budget decision recorded in
`status.md`, not a design fact.

## Always read

The kernel: at most three documents, every review. Three is a budget, not a count of what matters:
Neotoma's reading list records that a six-document always-read set consumed the reviewer's turn before
any diff was read.

| Doc | What it states |
|-----|----------------|
| `docs/foundation/principles.md` | Invariants: a mechanism that does not bind is not a control; a write that reports success has not necessarily happened; a test that cannot fail on the thing it watches is decoration; fail closed on the safety field; unknown stays distinct from a verdict. |
| `docs/foundation/work_model.md` | Pull-only delivery; assignment as eligibility; claim=lease (lease as relationship); liveness derived at read; task = status + edges; intake first; tasks go through workflows in batches; artifacts ≠ subjects. |
| `docs/foundation/gates_and_workflows.md` | `workflow` declares; a batch is the tasks going through it and the record of that; step state from batch + lease + `sign-off`; `step_status` projects; one step set; successors + `FOLLOWS`; actions are entities and are taken through the action gate; the checkpoint. |

A missing kernel document is reported as not yet written; that domain is reviewed on the lens's standing
criteria only, and a change is never blocked for lacking a citation to a document that does not exist.

## Read when these paths changed

First cell: regexes matched anywhere in a changed path (same convention as `Lens.diff_patterns`); second:
documents to read. Each document at most once per review, kernel first.

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

### The data model

| Changed path | Read |
|---|---|
| `lib/neotoma`, `execution/scripts/.*schema`, `execution/scripts/render_data_model`, `docs/foundation/data_model\.md` | `docs/foundation/data_model.md` |

### Adapters

| Changed path | Read |
|---|---|
| `execution/daemons/apis/github_gateway`, `execution/daemons/apis/swarm_dispatch`, `execution/daemons/apus/`, `execution/daemons/formica/`, `execution/scripts/lanius_sweep`, `execution/mcp/github_harness`, `execution/daemons/turdus/`, `execution/daemons/riparia/`, `lib/daemon_runtime/run_email`, `execution/daemons/monedula/`, `execution/daemons/cotinga/`, `lib/notify`, `execution/lib/telegram`, `docs/foundation/adapters\.md` | `docs/foundation/adapters.md` |

### The foundation itself

| Changed path | Read |
|---|---|
| `docs/foundation/` | `docs/foundation/conformance.md` |

## Design basis

Every issue and PR states its design basis: the foundation document and section it conforms to, or that no
design applies.

- Issue: first section of the swarm specification (`issue_spec.py`, section `basis`). The product lens
  states it at intake's `classify` step (`workflows.md#intake`), from the kernel; the arch lens checks it.
- PR body line: `Design basis: docs/foundation/work_model.md#the-claim-and-the-lease-are-one-primitive`,
  or `Design basis: no design applies — <one line why>`.

Checks, in order:

1. Mechanical (`foundation.check_design_basis`): statement exists; every cited `docs/foundation/` path is
   on the checkout; or `no design applies`. Missing/invalid → `[BLOCKING] design-basis`. Recovery: replace
   the PR body line with `Design basis: docs/foundation/<doc>.md#<section>` or
   `Design basis: no design applies — <reason>`, then re-request review.
2. By reading: the step owner opens the cited section and judges whether the change conforms to it. A
   citation is not conformance; a change that contradicts the cited document is blocked citing the
   document by path.
3. `no design applies` is judged too: if a kernel or keyed document governs the change, the declaration is
   false and blocking.

## An issue that conforms to nothing

Visible at intake, not only at audit. Not closed at the pm step; one of five dispositions the audit applies
together under operator approval: conforms, align (the ask is right, the framing names no basis), close
(conforms to nothing), supersede (already a step in a sibling plan), or premature (work for a later
vision phase, marked with that phase and kept open).

## Mechanical checks on this directory

Each check is a control only while something fails on it (principle 1); the wiring that makes each fail
is `status.md`.

| Check | Runs | What fails |
|---|---|---|
| Reading-list budget | `execution/daemons/apis/test_foundation.py` (`TestRealDocumentBudget`) | a kernel or keyed document over `MAX_DOC_CHARS`, or a reading block over `MAX_BLOCK_CHARS`; the caps are `foundation.py`'s |
| Anchors | `execution/scripts/check_foundation_anchors.py` | any link or backticked citation of a document and section in this directory that names a file or heading that does not exist |
| Vocabulary | `execution/scripts/check_foundation_vocabulary.py`; asserted zero in `test_foundation.py` | any **Never** item from `vocabulary.md` in the prose of any document here but `status.md`; **Not for** items are printed as advisory and never fail |
| Workflow tables | `execution/scripts/render_workflow_docs.py --check` (contract; `status.md` says whether it exists) | a step table in `workflows.md` that differs from its `workflow` entity |
| Data-model tables | `execution/scripts/render_data_model.py --check` (contract; `status.md` says whether it exists) | a concept or relationship table in `data_model.md` that differs from the schema registry |

## Direction of truth per class of record

Rules and decisions have four homes, chosen for four audiences (synthesis C7). Each class has one
authoritative side; the others are mirrors or restatements that are wrong until corrected. (An
`artifact` in `vocabulary.md` is a record in an external system that a batch leaves; the classes below are
the swarm's own records, and the word is not used for them.)

| Class of record | Authoritative side | Mirror or restatement |
|---|---|---|
| Implementation state | `docs/foundation/status.md` (as of its date) | none; a foundation doc that states state is wrong |
| Design invariants / work / steps / failure / authority | this directory, PR-reviewed | plan `decisions` event log; `CLAUDE.md` session restatement of six principles, which says it does not bind agents the swarm runs |
| Agent behavioural rule | `agent_policy` entities | rendered `.claude/skills/` and `docs/agents/` |
| System composition / roster / roadmap | plan `ent_99ace4dd6673aa36ed08b1fe` | `docs/architecture.md`, `taxonomy.md`, `phases.md` |
| Session standing instruction | `CLAUDE.md` | none |
| Core workflow step lists / fast paths / successors | the `workflow` entity for (project, type) | `docs/foundation/workflows.md` tables via `render_workflow_docs.py --check` (prose authored in the file) |
| Entity types, fields, and edge types the design names | the schema registry on the record | `docs/foundation/data_model.md` tables via `render_data_model.py --check` (prose authored in the file) |
| Walkthroughs of the work/gate model | the kernel documents | `docs/foundation/scenarios.md` (+ `scenarios_extended.md`) |
| External-system mapping (what an event becomes; what a step's operation is) | `docs/foundation/adapters.md`, PR-reviewed | the adapter daemons' code; the per-instance binding of a system to an operator is the `channel_config` and `vendor_binding` entities, which bind and never redefine the mapping |

A rule in two classes is written once in its authoritative home and cited from the other, never copied: a
comment or a second document claiming to mirror the first is not a mechanism that keeps them matching
(principle 9).

## Amending a foundation document

Change through a PR that cites the plan decision it consolidates (entity id + decision key), so the event
log and the reviewed statement stay traceable to each other. The keyed entry above ensures this document
is read when the foundation changes, and the checks above run on the change.

## Phases and implementation state

The foundation is phase-agnostic. Each document defines its part of the design whole, marks undecided
questions **open** with their options, and says nothing about what a checkout implements. README vision
phases (P1–P5) are a roadmap over that one design; `status.md` records, as of its date, which sections are
built, which are designed and unbuilt, and which are open. Implementation phases (`docs/phases.md`) are a
separate axis and are never merged with the roadmap: an implementation item cites the foundation document
and section it implements, not a vision phase.

An implementation item whose section is marked open in the foundation is premature: it waits on the
decision, marked with the roadmap phase that decision belongs to, and stays open. An item with no citable
section is either covered by the design as written, in which case it cites the foundation, or conforms to
nothing (above).

Two rules: a foundation document never carries "today", "on main", a commit hash, a count, or an
open-issue reference as evidence a defect is live; an issue/PR is cited only as the record of a decision,
never as state. A PR that adds either is blocked on this section.
