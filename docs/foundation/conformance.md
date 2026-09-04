# Conformance: how work binds to the foundation

**Keyed document:** read when `docs/foundation/` changes. **Kind:** foundation; describes the binding
mechanism (built as ateles#744): names documents and paths, never what a checkout implements.
**Derived from:** plan `ent_533d4ec2f7bfb60f66fb3fce` decisions
`binding_is_the_reviewer_reading_a_kernel_not_a_loading_order` and
`ateles_binding_extends_three_existing_mechanisms`, synthesis `ent_b0ce322f768e4fc676b73139` (PR-29, C7),
prior art `ent_08460968e6f49dac21510f4a`, and PR #745 operator review (2026-09-04).

## Purpose

State how issue-based swarm work is checked against `docs/foundation/`: which documents a reviewer reads
on every change, which it reads when particular paths change, what a design-basis statement is, and what
happens to an issue that conforms to nothing. Consumed by `execution/daemons/apis/foundation.py`,
`skill_runner.SWARM_FOUNDATION_CONTRACT`, and the issue-spec design-basis check.

## Scope

The `docs/foundation/` directory. Direction of truth is the repository (PR-reviewed). Plan `decisions`
maps remain the event log; when they disagree, the document is wrong until a PR corrects it.

Out of scope: `docs/architecture.md`, `docs/taxonomy.md`, `docs/phases.md` (architecture-plan render
targets).

`docs/foundation/status.md` is colocated and deliberately **not** in the reading list: dated, perishable
measurement, regenerated. Foundation docs may name it as the state home only; they must not embed its
figures as design evidence. The parser reads only the tables below.

`docs/foundation/workflows.md`, `docs/foundation/scenarios.md`, and
`docs/foundation/scenarios_extended.md` are authored companions: they bind via `workflow` entities +
`render_workflow_docs.py --check` (workflows) or human-reference walkthroughs (scenarios), and are
**not** inlined into review prompts. Runtime claim/lifecycle/gating paths load the kernel (and gates)
instead.

## Always read

The kernel: at most three documents, every review. Three is a budget (Neotoma's six-document always-read
consumed the turn before any diff).

| Doc | What it states |
|-----|----------------|
| `docs/foundation/principles.md` | Invariants: a mechanism that does not bind is not a control; a write that reports success has not necessarily happened; a test that cannot fail on the thing it watches is decoration; fail closed on the safety field; unknown stays distinct from a verdict. |
| `docs/foundation/work_model.md` | Pull-only delivery; assignment as eligibility; claim=lease (lease as relationship); liveness derived at read; task = status + edges; intake first; passages; artifacts ≠ subjects. |
| `docs/foundation/gates_and_workflows.md` | `workflow` declares; `passage` carries tasks; step state from passage + lease + `sign-off`; `step_status` projects; one step set; successors + `FOLLOWS`; actions are entities; execution gate per action. |

A missing kernel document is reported as not yet written; that domain is reviewed on the lens's standing
criteria only.

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

### The foundation itself

| Changed path | Read |
|---|---|
| `docs/foundation/` | `docs/foundation/conformance.md` |

## Design basis

Every issue and PR states its design basis: the foundation document and section it conforms to, or that no
design applies.

- Issue: first section of the swarm specification (`issue_spec.py`, section `basis`). Product lens states
  it at intake `classify`; arch checks it.
- PR body line: `Design basis: docs/foundation/work_model.md#claim-and-lease`, or
  `Design basis: no design applies — <one line why>`.

Checks, in order:

1. Mechanical (`foundation.check_design_basis`): statement exists; every cited `docs/foundation/` path is
   on the checkout; or `no design applies`. Missing/invalid → `[BLOCKING] design-basis`.
2. By reading: the cited section must actually govern the change. Citation ≠ conformance.
3. `no design applies` is judged too: if a kernel or keyed document governs the change, the declaration is
   false and blocking.

## An issue that conforms to nothing

Visible at intake, not only at audit. Not closed at the pm step. One of five dispositions under operator
approval: conforms, align, close, supersede, or premature (later vision phase; kept open).

## Direction of truth per class of record

| Class of record | Authoritative side | Mirror or restatement |
|---|---|---|
| Implementation state | `docs/foundation/status.md` (as of its date) | none; a foundation doc that states state is wrong |
| Design invariants / work / steps / failure / authority | this directory, PR-reviewed | plan `decisions` event log; `CLAUDE.md` session restatement of six principles |
| Agent behavioural rule | `agent_policy` entities | rendered `.claude/skills/` and `docs/agents/` |
| System composition / roster / roadmap | plan `ent_99ace4dd6673aa36ed08b1fe` | `docs/architecture.md`, `taxonomy.md`, `phases.md` |
| Session standing instruction | `CLAUDE.md` | none |
| Core workflow step lists / fast paths / successors | the `workflow` entity for (project, type) | `docs/foundation/workflows.md` tables via `render_workflow_docs.py --check` (prose authored in the file; **not** inlined into review prompts) |
| Worked examples of the work/gate model | the kernel documents | `docs/foundation/scenarios.md` (+ `scenarios_extended.md`); human reference, **not** inlined |

A rule in two classes is written once in its authoritative home and cited from the other (principle 9).

## Amending a foundation document

Change through a PR that cites the plan decision it consolidates (entity id + decision key). The keyed
entry above ensures this document is read when the foundation changes.

## Phases and implementation state

The foundation is phase-agnostic. Each document defines its part of the design whole, marks undecided
questions **open**, and says nothing about what a checkout implements. README vision phases (P1–P5) are a
roadmap over that one design; `status.md` records what is built. Implementation phases (`docs/phases.md`)
are a separate axis.

Two rules: a foundation document never carries "today", "on main", a commit hash, a count, or an
open-issue reference as evidence a defect is live; an issue/PR is cited only as the record of a decision,
never as state. A PR that adds either is blocked on this section.
