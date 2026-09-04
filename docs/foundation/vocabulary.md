# Vocabulary: canonical terms

**Keyed document:** read when a skill, an agent document, or the agent-doc renderer changes
(`conformance.md`). **Kind:** foundation; compact term index — definition + forbidden synonyms + owning
document. Extended essays live in the owning docs, not here. **Derived from:** synthesis
`ent_b0ce322f768e4fc676b73139`, prior art `ent_08460968e6f49dac21510f4a`, task
`ent_da60df3beccb675ef8c8c0c5`, ateles#378 glossary, `docs/multi_tenant.md` §5, PR #745 operator review.

## Purpose

One bindable list of terms the swarm's documents, schemas, prompts, and error messages use — each with a
short definition, forbidden synonyms, and the document that owns the full rule.

## Scope

Entity-type terms are written as the entity type (`checkpoint_brief`). No phase markers; implementation
state is `status.md`. This file must stay ≤ `MAX_DOC_CHARS` so skills/agents reviews receive it whole.

## Work model (`work_model.md`)

| Term | Definition | Forbidden |
|---|---|---|
| task | Atomic unit of accountable work; a Neotoma `task`. | chip; ticket (that is an `issue`) |
| work | Synonym of task in prose; not a separate entity. | work item (retired) |
| artifact | External record a passage leaves (issue, PR, release, message), linked by edge. | treating it as a step subject |
| claim | Agent takes a task and holds its lease; atomic, keyed on the task. | assign (push); pick up; dispatch |
| claimant | Principal that holds the current lease. | assignee (eligibility only) |
| assign | Write `assigned_to` = eligibility; never creates a lease. | setAssignee-as-holder |
| lease | Edge principal↔task with `claimed_at` / `expires_at`; renewal is heartbeat. | lock; heartbeat alone |
| held / lapsed / returned | Lease states derived at read: future expiry; past without end; explicit end. | stuck; executing-as-liveness |
| active | Derived: held lease + recent activity; never stored. | running / executing as flags |
| created | Task exists in the record; publication is creation. | published (separate state) |
| claimable | Not terminal, not blocked, no held lease; assignee-only if assigned. | — |
| terminal | Status that ends claimability (`completed`, `done`, `canceled`, …). | — |
| runner | Process identity on the lease. | agent alone |
| agent_session | Host/checkout/branch/head identity half observations lack. | — |
| observation | Append-only status/provenance record on the task. | assignment log |
| watchdog | Observes repeated lapse and escalates; never chooses a claimant. | reaper; router |
| reaper | Retired: nothing to release when lapse is read, not written. | — |
| aggregation / split | Many tasks → one passage / one task → new passage; edges, never fields. | — |
| parent / child task | `PART_OF`; parent completion derived; no passage for a parent. | — |
| operator-facing agent | The `ateles` agent that claims operator-only tasks. | — |
| daemon / pipeline | Self-triggering loop / GitHub step sequencer — the other two execution mechanisms. | — |
| intake | Every task's first passage (`workflows.md#intake`). | undispatched state |

## Gate model (`gates_and_workflows.md`)

| Term | Definition | Forbidden |
|---|---|---|
| workflow | Declares steps, fast paths, successors for one (project, type). | workflow_definition; pipeline-as-entity |
| step | Named unit in `workflow.steps[]`; no entity of its own in a passage. | gate (reserved for execution gate) |
| stage | Named contiguous group of steps. | — |
| step owner | Agent declared to claim and sign a step. | — |
| sign-off | Terminal write closing a step on a passage (verdict, agent, pinned definition). | participation_record |
| passage | One passage of tasks through a workflow; subject is tasks. | run; participation_record |
| step state | Derived: open / claimed / signed from passage + lease + sign-off. | per-step status row as source |
| step_status | Hot-path projection of sign-offs on the task; reconciler-backed. | gate_status; history |
| fast path | Declared contiguous skip within a workflow. | — |
| successor / chain | Declared next workflow / passages along `FOLLOWS` back to intake. | super-workflow entity |
| gate | **Only** the execution gate decision. | workflow step name; stage; check |
| execution gate | Decide whether an action auto-executes or writes a checkpoint. | second consent gate |
| execution_policy | Entity the gate evaluates for blast and auto-execute rules. | — |
| workflow policy | Who may claim which steps (owners + grants). | conflating with execution_policy |
| action | Entity for one intended external effect; only actions execute. | execute (a task) |
| action_type | Declared/expected class of action for eligibility and blast. | inferring from agent name |
| blast radius | LOW / HIGH / NEVER resolved from action class under policy. | — |
| confidence | Score from proposing agent; gate uses with blast. | — |
| operator_only | Action class that is ALWAYS `NEVER`. | merely HIGH |
| checkpoint_brief | Interrupted request for a decision on one action. | terminal failure |
| steward / review panel | Merge/sign roles layered on GitHub review machinery. | — |
| effect dedup | Idempotency key on the action for at-least-once re-claim. | replay |

## Authority (`authority_model.md`)

| Term | Definition | Forbidden |
|---|---|---|
| authority | Tuple principal + domain + scope + action + conditions + time. | bare owner |
| principal | Who may act (human or agent); C9 open on which human entity. | user_id / AAuth sub as human |
| credential | Store or AAuth proof; not itself the human principal. | — |
| operator / agent | Human principal / machine principal with `agent_definition`. | — |
| tenant / grant | Tenancy boundary / permission record. | — |
| ownership / delegation | Who owns what / who may act for whom. | — |
| approval / quorum / SoD | Consent object / multi-party / separation of duties. | — |
| initiative / proposal | Objects that start or reframe work under P4+. | — |

## Failure posture (`failure_posture.md`)

| Term | Definition | Forbidden |
|---|---|---|
| halt | Stop work when Neotoma (or a hard dependency) is unreachable. | fail open on safety fields |
| reachability probe | Check before trusting the store. | — |
| read-back | Every write is verified by a subsequent read (principle 2). | report-success-as-done |
| unknown | Distinct from a verdict; holds rather than inventing one. | empty-tuple proceed |
| escalation | Aggregated raise when unknown persists or lapse repeats. | silent retry forever |

## Conformance (`conformance.md`)

| Term | Definition | Forbidden |
|---|---|---|
| kernel document | Always-read foundation doc (≤3). | stuffing more into always-read |
| keyed document | Read when matching paths change. | dual-keying oversized examples onto runtime |
| lens | Review perspective that loads the reading list. | — |
| design basis | Issue/PR citation to a foundation section, or `no design applies`. | citation-as-conformance |
| status | Perishable measurement in `status.md`; never a design claim. | embedding checkout figures in kernel |

## Verbs and the bare word `owner`

| Verb / word | Use | Forbidden |
|---|---|---|
| claim / assign / sign off / execute | Pull lease / write eligibility / close step / run an action | dispatch for those three jobs |
| release | Say which: lease return, software release, or a named step | bare "release" across senses |
| owner | Always qualify: step owner, plan owner, grant owner, … | bare "owner" alone |
