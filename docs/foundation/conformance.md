# Conformance: how work binds to the foundation

**Keyed document:** read when `docs/foundation/` changes. **Kind:** foundation; describes the binding
mechanism (built as ateles#744): names documents and paths, never what a checkout implements.
**Derived from:** plan `ent_533d4ec2f7bfb60f66fb3fce` decisions
`binding_is_the_reviewer_reading_a_kernel_not_a_loading_order` and
`ateles_binding_extends_three_existing_mechanisms`, synthesis `ent_b0ce322f768e4fc676b73139` (PR-29, C7),
prior art `ent_08460968e6f49dac21510f4a` (OPA: decision decoupled from enforcement), and PR #745 operator
review (2026-09-04), and the operator's 2026-09-05 review of review relevance (revision 19: the `applies_when` condition on an optional step, and two terms retired in favour of `review step`), and revision 21 (2026-09-05: the `gmail.md` and `calendar.md` keyed rows and their canonical-source entries), and revision 24 (2026-09-05: the `telegram.md` and `payments.md` keyed rows and their canonical-source entries), and PR #745 operator review (2026-09-05, rulings 13–14, 16–18, 23–29: twelve register rows moved from open to ruled), and the operator's 2026-09-05 rulings of decision 15 and decision 30 (revision 27: the last open row moved to ruled, and the recurring task registered as ruled on the operator's proposal), and revision 28 (2026-09-05: `migration.md` registered as an authored companion, and decision 31 opened). Revised by the simplification pass of 2026-09-05 (revision 29: the `feedback` entity removed from the direction-of-truth table in favour of the finding; `scenarios_extended.md` merged into `scenarios.md`; decisions 32 to 35 opened). Revised for the operator's 2026-09-05 22:02–22:13 memos on how tasks come into existence (revision 30, 2026-09-06: `intake_rule` registered in the direction-of-truth table, and decision 36 opened). Revised by the memo-gap pass of 2026-09-06 (revision 31: decisions 37 to 41 registered as ruled). Revised by revision 33 (2026-09-06: the conformance-suite design — `conformance_suite.md`, its keyed row, the rule-coverage check named as a contract, its direction-of-truth row, and decisions 43 and 44 opened). Revised by the workflow-format pass of 2026-09-06 (revision 34: decision 45 registered). Revised by the consistency pass of 2026-09-06 (revision 35: decisions 46 to 54 registered from `authority_model.md`'s long-open questions). Revised by the second workflow-format pass of 2026-09-06 (revision 36: decision 56 registered). Revised by the testability pass of 2026-09-06 (revision 37: decision 56 registered; the decision-citations lint named as a contract and its syntactic rule stated under *Phases and implementation state*). Revised by the rulings pass of 2026-09-06 (revision 38: decisions 31, 32, 35, 42, 44, 45, 46, 48, 49, 51, and 54 moved to ruled; 43, 50, and 53 marked ruled in part, each with its open half stated; the **ruled in part** status value). Revised by the second rulings pass of 2026-09-06 (revision 39: 36, 47, 52, and 56 moved to ruled; 43, 50, and 53 from ruled in part to ruled; the operator's review of 37 to 41 recorded; three rows open — 33, 34, and 55). Revised by the planning pass of 2026-09-06 (revision 40: `planning_model.md` keyed, with its direction-of-truth row; decision 57 opened, and 58 opened and ruled with 43, 47, and 56).

## Purpose

State how issue-based swarm work is checked against `docs/foundation/`: which documents a reviewer reads
on every change, which it reads when particular paths change, what a design-basis statement is, what
happens to an issue that conforms to nothing, which mechanical checks hold this directory to its own
rules, and which design questions the foundation has left open. Consumed by `execution/daemons/apis/foundation.py`, `skill_runner.SWARM_FOUNDATION_CONTRACT`, and
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

`docs/foundation/workflows.md`, `docs/foundation/scenarios.md`, and `docs/foundation/migration.md` are
authored companions: they
bind via `workflow` entities + `render_workflow_docs.py --check` (workflows), as walkthroughs of the kernel
(scenarios), or as the design of the population plan's second leg, whose stages are tasks going through the
`record_migration` workflow it declares (migration), and are currently **not** inlined into review prompts.
Whether the first two are keyed is a budget decision recorded in `status.md`, not a design fact;
`migration.md` is never keyed, because it governs no code path and its figures live in `status.md`.

## Always read

The kernel: at most three documents, every review. Three is a budget, not a count of what matters:
Neotoma's reading list records that a six-document always-read set consumed the reviewer's turn before
any diff was read.

| Doc | What it states |
|-----|----------------|
| `docs/foundation/principles.md` | Invariants: a mechanism that does not bind is not a control; a write that reports success has not necessarily happened; a test that cannot fail on the thing it watches is decoration; fail closed on the safety field; unknown stays distinct from a verdict. |
| `docs/foundation/work_model.md` | Pull-only delivery; assignment as eligibility; claim=lease (lease as relationship); liveness derived at read; task = status + edges; intake first; tasks go through workflows in batches; artifacts ≠ subjects. |
| `docs/foundation/gates_and_workflows.md` | `workflow` declares; a batch is the tasks going through it and the record of that; step state from batch + lease + `sign-off`; `step_status` projects; one step set; successors + `FOLLOWS`; actions are entities and are taken through the action gate; the checkpoint. |

A missing kernel document is reported as not yet written; that domain is reviewed on the review step's standing
criteria only, and a change is never blocked for lacking a citation to a document that does not exist.

## Read when these paths changed

First cell: regexes matched anywhere in a changed path; second:
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
| `execution/daemons/apis/github_gateway`, `execution/daemons/apis/swarm_dispatch`, `execution/scripts/lanius_sweep`, `\.claude/skills/lanius/`, `docs/agents/lanius\.md`, `\.claude/skills/vanellus/`, `docs/agents/vanellus\.md`, `execution/daemons/apis/review_panel`, `execution/mcp/github_harness`, `docs/foundation/github\.md` | `docs/foundation/github.md` |
| `execution/daemons/turdus/`, `execution/daemons/riparia/`, `lib/daemon_runtime/run_email`, `lib/approval/email_channel`, `\.claude/hooks/gmail_send_gate`, `docs/foundation/gmail\.md` | `docs/foundation/gmail.md` |
| `execution/daemons/sylvia/`, `execution/daemons/cotinga/`, `execution/daemons/monedula/`, `docs/foundation/calendar\.md` | `docs/foundation/calendar.md` |
| `execution/lib/telegram`, `lib/notify`, `execution/daemons/cyphorhinus/`, `lib/activity`, `docs/foundation/telegram\.md` | `docs/foundation/telegram.md` |
| `execution/daemons/monedula/`, `docs/foundation/payments\.md` | `docs/foundation/payments.md` |

### The foundation itself

| Changed path | Read |
|---|---|
| `docs/foundation/` | `docs/foundation/conformance.md` |

### The conformance suite

| Changed path | Read |
|---|---|
| `docs/foundation/conformance_suite\.md`, `execution/conformance/`, `execution/scripts/check_foundation_rule_coverage` | `docs/foundation/conformance_suite.md` |

### The planning model

| Changed path | Read |
|---|---|
| `execution/scripts/render_plan_docs`, `\.claude/hooks/session_start`, `\.claude/hooks/stop_finalizer`, `\.claude/hooks/user_prompt_submit`, `\.claude/skills/update-plan/`, `\.claude/skills/update-tasks/`, `\.claude/skills/create-execution-plan/`, `docs/foundation/planning_model\.md` | `docs/foundation/planning_model.md` |

## Design basis

Every issue and PR states its design basis: the foundation document and section it conforms to, or that no
design applies.

- Issue: first section of the swarm specification (`issue_spec.py`, section `basis`). The `pm` step owner
  states it at intake's `classify` step (`workflows.md#intake`), from the kernel; the arch review step checks it.
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
| Term links | `execution/scripts/link_vocabulary_terms.py --check`; asserted in `test_foundation.py` | a first mention of a defined term, in an entry or section of `vocabulary.md`, that carries no link to its definition; run the script without `--check` to link them |
| Workflow tables | `execution/scripts/render_workflow_docs.py --check` (contract; `status.md` says whether it exists) | a step table in `workflows.md` that differs from its `workflow` entity |
| Data-model tables | `execution/scripts/render_data_model.py --check` (contract; `status.md` says whether it exists) | a concept or relationship table in `data_model.md` that differs from the schema registry |
| Rule coverage | `execution/scripts/check_foundation_rule_coverage.py` (contract; `status.md` says whether it exists) | a rule-bearing heading in a kernel or keyed document (`conformance_suite.md#what-the-rule-coverage-check-reads` says which headings those are) with no row in `conformance_suite.md` whose pointer resolves to it; a row whose pointer resolves to nothing; or a decision opened in a document and absent from the register below |
| Decision citations | `execution/scripts/check_foundation_citations.py` (contract; `status.md` says whether it exists) | a commit hash in any document here but `status.md`; an issue or pull-request number outside the positions `#phases-and-implementation-state` names |

## Direction of truth per class of record

Rules and decisions have four homes, chosen for four audiences (synthesis C7). Each class has one
authoritative side; the others are mirrors or restatements that are wrong until corrected. (An
`artifact` in `vocabulary.md` is an entry an external system holds that a batch leaves; the classes below are
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
| Walkthroughs of the work/gate model | the kernel documents | `docs/foundation/scenarios.md` |
| The suite's design: what each test sets up, does, and observes, per rule | `docs/foundation/conformance_suite.md`, PR-reviewed | the suite's code, judged against the design and never the reverse; a row the code cannot pass is drift in the code or a proposed change to the foundation through a PR, never an edit to the row |
| Skill bodies (what a skill instructs) | the `agent_policy` entity the skill renders from | `.claude/skills/<name>/SKILL.md` on disk |
| Agent prompt text | `agent.prompt_markdown` | `docs/agents/` and the rendered skill mirrors, via `render_agent_docs.py --check`, which also prunes a mirror whose definition is gone |
| Which changes in the record are work (intake rules) | the `intake_rule` entities on the record, each written through the gate as a governance write | none; a predicate in a daemon's code that creates a task on an entity change is a rule with no home, and is drift (`work_model.md#an-intake-rule-turns-a-described-change-in-the-record-into-a-task-and-nothing-else`) |
| Operator preferences | `task_policy` entities on the record | none; a harness memory file is a cache of them, never their home, and a preference that exists only in one is unreadable by every agent that is not that harness. The operator's input on reviewed work is not a preference and has no entity of its own: it is a finding on the batch; where a standing finding's change is operator-specific, the change it produces lands here, never in a public prompt or `agent_policy` (`gates_and_workflows.md#a-finding-is-one-off-or-standing-and-a-standing-one-obliges-a-change-to-what-produced-it`) |
| External-system mapping (what an event becomes; what a step's operation is) | `docs/foundation/adapters.md`, PR-reviewed | the adapter daemons' code; the per-instance binding of a system to an operator is the `channel_config` and `vendor_binding` entities, which bind and never redefine the mapping |
| The code host's per-event mapping and per-step operation, in full | `docs/foundation/github.md`, PR-reviewed | the GitHub receiver and the pipeline reading from it; `adapters.md` holds the general rules the document applies and carries the pointer to it |
| The mail system's per-signal mapping and per-step operation, in full | `docs/foundation/gmail.md`, PR-reviewed | the mail poller and the send path; `adapters.md` holds the general rules and carries the pointer to it |
| The calendar's per-signal mapping and per-step operation, in full | `docs/foundation/calendar.md`, PR-reviewed | the calendar-reading daemons and the one event-write path; `adapters.md` holds the general rules and carries the pointer to it |
| The chat channel's per-update mapping and per-step operation, in full | `docs/foundation/telegram.md`, PR-reviewed | the chat send helpers and the one inbound poller; `adapters.md` holds the general rules and carries the pointer to it |
| The payment rails' per-signal mapping and per-step operation, in full | `docs/foundation/payments.md`, PR-reviewed | the payment daemon and its per-rail handlers; `adapters.md` holds the general rules and carries the pointer to it |
| The carrying of an instance's record into the design's types (each type's disposition, the primitive that carries it, the order, and how the carrying is governed), and of the skills the harnesses hold into agents, declarations, and policies | `docs/foundation/migration.md`, PR-reviewed | the population plan `ent_0916804d07280d1751106d82`, whose `next_steps` names the leg, and the `record_migration` workflow declaration on an instance, which binds the plan to that instance and never redefines a disposition; the counts and shapes the plan starts from are `status.md` |
| A planning record's authored content — its statement and the planning decisions under it | the planning record entity on the record and the `decision` entities `PART_OF` it, written only by the `planning` workflow's `amend` step as `amend_<level>` actions (`docs/foundation/planning_model.md`) | any document rendered from a statement, held equal by a renderer's `--check`; a harness's plan file; a session's copy. A record's progress, blockers, and next steps are derived reads over its descendants and have no authoritative side, because they are never written (`planning_model.md#downward-state-is-derived-upward-content-is-authored-as-entities`) |

A rule in two classes is written once in its authoritative home and cited from the other, never copied: a
comment or a second document claiming to mirror the first is not a mechanism that keeps them matching
(principle 9).

**When the mirror holds more than the canonical side.** "Wrong until corrected" describes the mirror's
*authority*, not its *content*: a mirror that has been edited in place can carry material the
authoritative side never received, and regenerating over it destroys work rather than fixing a defect.
The direction never flips — the mirror does not become canonical because it is currently richer, and no
item is exempt. What the richer mirror is, is a **draft of a correction to the authoritative side**: its
content is read, merged upward as a correction to the canonical entity or document, read back there, and
only then is the mirror re-rendered from it. So the sequence is merge upward, then render downward, and a
regeneration that has not been preceded by the merge is the destructive step. Two consequences worth
stating. Editing a mirror is not forbidden, but it is drafting, and a draft that is never merged upward
is lost at the next render — the merge is the author's obligation, not the renderer's. And a `--check`
failure means only that the two sides differ; it does not say which one is ahead, so the correct response
is to read both before regenerating, never to regenerate reflexively because the check went red.

## The register of open design decisions

Every question the foundation marks **open** is listed here once, with a pointer to where it is argued.
This is an index and not an argument: each row states the question in one line and names the section that
holds it, and no row restates the reasoning, the options, or what would decide it — those live in the
document that owns the subject, and reading the row is never a substitute for reading the section
(principle 9). A decision that is argued in two places is a defect in one of them.

**Why the register is here and not in `status.md`.** An open decision is a **design** record, and the
direction-of-truth table above puts design in this directory. `status.md` is the state home: dated,
perishable, regenerated by repeating its measurements, and deliberately out of the reading list, so a
register there would be rewritten by the next regeneration and never reach a reviewer. The register's
readers are the reviewer deciding whether a change is premature (see *Phases and implementation state*
below) and the author deciding whether a question is already open, and both of them read this document.
What `status.md` continues to hold, and what this register therefore never carries, is whether a checkout
has taken an answer the design has not — the de facto answers it records against decisions 24 and 29 are
measurements, and they belong there.

**Status values.** **open** — argued in a document, unruled; every open decision has an argued home, and
a row that could not name one would be recording a defect rather than a decision. **ruled** — settled and
written into the design; a ruled row keeps its date, the ruling in one line, and the pointer to the rule it
became, and nothing more — the reasoning, the cost, and what would reopen it are the home document's. The
twelve of revision 12 predate the register and keep their one summary row. **ruled in part** — a question with two halves, one settled and one not: the row states the half ruled, with its date and pointer, and the half that stays open, in the same row, so that the number keeps naming one question and the open half is findable where the question is. **withdrawn** and **not a decision** — numbers that carry no question, kept in the table
so that they are not reused; see the two notes below.

| # | The question | Argued in | Blocks | Status |
|---|---|---|---|---|
| 1–12 | the twelve questions of revision 12 | ruled and written into the documents they concern | — | **ruled** (2026-09-04) |
| 13 | whether a batch may hold on a condition discovered mid-flight | `work_model.md#a-batch-may-hold-on-a-condition-discovered-mid-flight` | — | **ruled** (2026-09-05): yes; the step owner records a finding naming the condition, writes no sign-off, and renews the lease; no held state, no field; bounded by sign-off, checkpoint, or lapse |
| 14 | whether a batch may depend on a task it created | `work_model.md#a-batch-may-depend-on-a-task-it-created` | — | **ruled** (2026-09-05): yes, as a case of 13; a `DEPENDS_ON` edge from the batch to the task, never a field; the sign-off is refused while it is unended; a cycle is refused at write and at attach, and one found later escalates every batch in it as `dependency_cycle` |
| 15 | whether adapters live in a repository of their own | `adapters.md#the-adapter-and-the-engine-are-two-roles` | — | **ruled** (2026-09-05): bundled in this repository, for now; the six admission obligations are checked by mechanisms here, and separating would split their review across two release cadences before a second consumer exists; separation is the intended end state, revisited when a second consumer of the adapters exists, not in anticipation of one |
| 16 | whether the swarm builds its inbound receivers or rides a shared one | `adapters.md#where-inbound-delivery-lands-the-adapter-verifies-and-identifies-it-and-the-records-own-subscriptions-are-not-it` | — | **ruled** (2026-09-05): the adapter owns signature verification, delivery-id extraction, and acknowledgement; the transport listener may be shared plumbing that verifies, deduplicates, acknowledges, and parses nothing |
| 17 | whether institutionalizing a standing finding is itself a workflow, and how the two batches sequence | `gates_and_workflows.md#a-finding-is-one-off-or-standing-and-a-standing-one-obliges-a-change-to-what-produced-it` | — | **ruled** (2026-09-05): it is a workflow, by the work model's general rule; the raising batch does not wait, and the institutionalization task enters intake independently |
| 18 | whether a governance write is reserved to the operator by default, or gated at a high blast tier | `work_model.md#changing-the-swarm-is-work-and-it-goes-through-a-workflow-like-any-other` | — | **ruled** (2026-09-05): reserved; each governance class resolves to `NEVER` until the operator grants it a policy value, class by class |
| 19 | — | never assigned; see the gap note below | — | **not a decision** |
| 20 | — | never assigned as a distinct question; the two prose pointers that cited it named what is now 27 | — | **withdrawn** |
| 21 | — | never assigned as a distinct question; the prose pointer that cited it named what is now 28 | — | **withdrawn** |
| 22 | — | never assigned; see the gap note below | — | **not a decision** |
| 23 | whether a mail thread is one artifact or a container of artifacts | `gmail.md#a-thread-and-its-messages-are-each-artifacts-related-by-part_of` | — | **ruled** (2026-09-05, with 24): both levels are artifacts, the message `PART_OF` its thread; an event links to the unit whose id it carries, an action to the unit its operation needs, a task to the unit it names |
| 24 | whether a recurring calendar series is one artifact or many | `calendar.md#a-series-and-its-occurrences-are-each-artifacts-related-by-part_of` | — | **ruled** (2026-09-05, with 23): both levels are artifacts, the occurrence `PART_OF` its series; the same rule, stated once under linkage in `adapters.md` |
| 25 | whether a chat reaction may ever carry a decision | `telegram.md#a-reaction-never-carries-a-decision` | — | **ruled** (2026-09-05): never; a reaction is an observation — it can be silently removed and its meaning is the channel's, not the swarm's |
| 26 | whether the swarm answers a read on the chat channel without the record | `telegram.md#during-a-halt-a-read-on-the-channel-is-answered-with-the-halt-and-never-with-data` | — | **ruled** (2026-09-05): it answers with the halt itself — since when, and why — on the announcement path, in the chat that path already reaches, and with no data |
| 27 | whether a payment's approver must be shown what the verifier signed | `payments.md#a-payments-approver-is-shown-exactly-what-the-verifier-signed` | — | **ruled** (2026-09-05): yes; the checkpoint carries payee, amount, currency, period, and rail as the `verify` sign-off recorded them, and `pay` is taken only on those parameters |
| 28 | what tolerance, if any, a payment's consent carries | `payments.md#tolerance-is-an-action_policy-value-and-its-default-is-zero` | — | **ruled** (2026-09-05): a per-class `action_policy` value, zero where absent; any change to what the payee receives or the operator pays is a new checkpoint until the operator sets one |
| 29 | what depth or state counts as terminal, and where it is declared | `payments.md#terminal-is-declared-in-the-rails-adapter-document-and-the-value-is-bound-per-instance` | — | **ruled** (2026-09-05): the criterion — settled, never sent, on a bank rail; *N* confirmations on a chain — is stated in the rail's adapter document; the value is bound per instance in the `vendor_binding`; a profile may deepen it and never shallow it |
| 30 | how a recurring task is modelled | `work_model.md#a-recurring-task-is-one-live-instance-and-its-completion-creates-the-next` | — | **ruled** (2026-09-05, on the operator's proposal): one live instance carrying its own `recurrence` rule; its closing sign-off creates the next instance, `FOLLOWS` task to task, with `due_date` computed from the schedule and never from the completion; the reschedule-instead-of-complete pattern is superseded for tasks modelled this way; an action series is a different thing and the two meet only at the gate |
| 31 | how a registered entity type is renamed on a live record: a merge into a new-typed entity (ids change, edges repoint, the old id redirects), a registry alias (no id changes, a capability the record lacks), or a permanent tolerant reader over both types (nothing written) | `migration.md#how-a-registered-entity-type-is-renamed-on-a-live-record` | — | **ruled** (2026-09-06): the merge form — register the target type, interpret over the same source, merge the retired entity into the survivor; the retired id redirects, every inbound edge is repointed, and the tolerant reader over both type names is kept permanently; no alias is built |
| 32 | whether a sign-off's `verdict` is a stored field or a read over its findings and its author | `gates_and_workflows.md#whether-the-verdict-is-a-stored-field-or-a-read-over-the-findings-and-the-author` | — | **ruled** (2026-09-06): a stored field, kept as the sign-off's own projection of its findings and its author and reconciled at the write by the refusal at submission; under derivation a sign-off with no finding would be `signed`, and the stated verdict is what the findings are read back against |
| 33 | whether a stage, and the `phase` field on a step, names anything a step does not | `workflows.md#whether-a-stage-names-anything-a-step-does-not` | — | **open** (simplification pass, 2026-09-05; unverified against the matrix) |
| 34 | whether the step path is an execution mechanism of its own, and whether the component that opens steps is `pipeline` or `engine` | `work_model.md#whether-the-step-path-is-a-mechanism-of-its-own-and-what-the-engine-is-called` | — | **open** (simplification pass, 2026-09-05; unverified against the matrix) |
| 35 | whether one binding type or two (`channel_config`, `vendor_binding`) names an external system's per-instance binding | `adapters.md#whether-one-binding-type-or-two-names-an-external-systems-instance` | — | **ruled** (2026-09-06, as settled by revision 33): one binding type per external system, routing a field of it — the matrix found no rule that reads the two differently, which was the deciding condition; the name and the substitution are the condensation pass's, and the two names stand in the text until it lands |
| 36 | whether an intake rule may key on the work model's own records (task, batch, lease, sign-off, action, checkpoint, agent session), or only on artifacts and the swarm's other entities | `work_model.md#whether-an-intake-rule-may-key-on-the-work-models-own-records` | the evaluator's subject set; a rule on any other type is unaffected | **ruled** (2026-09-06): a rule keys on no work-model record type — `task`, `batch`, `lease`, `sign_off`, `action`, `checkpoint`, `agent_session` — and on any other type, a field a step wrote on an ordinary entity included; a rule naming one of the seven is refused at the write; the operator's lean toward every type was considered and set aside, the listener it asked for being met on every ordinary entity |
| 37 | where the operator's view of work lives, and what a channel carries | `gates_and_workflows.md#work-is-reviewed-on-the-record-and-a-channel-carries-only-what-awaits-the-operator-or-cannot-wait` | — | **ruled** (2026-09-06, on the operator's memo): the view is a read of the record, made under the operator's grant — not an adapter, not an actor with a write contract; a channel carries a declared subset — a checkpoint awaiting the operator, the announcement path, a delivery a workflow declared — and completed work is read, never carried, unless the binding or a `deliver` step says so |
| 38 | how the operator's input on closed work re-enters | `gates_and_workflows.md#closed-work-is-reviewed-on-the-record-and-redone-through-intake-never-reopened` | — | **ruled** (2026-09-06): a closed batch is never reopened; the input is a finding on it, the redo is a new task through intake referring to the closed batch's artifacts, and the standing half is the institutionalization task of decision 17 |
| 39 | whether intake attaches a task's context — internal entities as well as external records — or each step retrieves its own | `workflows.md#what-link-attaches-and-what-it-leaves-to-hydration` | — | **ruled** (2026-09-06, the operator's inclination interrogated as asked): the hybrid — `link` attaches what the task names, internal and external alike, by `REFERS_TO`, and nothing on relevance alone; hydration resolves each step's declared reads from those anchors; context a step discovers is written back as the same edge |
| 40 | what a step records at close about the session it ran in | `gates_and_workflows.md#what-a-step-leaves-at-close-what-it-produced-and-a-reference-to-what-it-read` | — | **ruled** (2026-09-06): what it produced is written as the entities it is; what it read is named on the sign-off and reproduced by an as-of read at `signed_at`, never copied; its reasoning is not written; `agent_session` stays the identity and liveness half |
| 41 | whether write admission per entity type is default-allow with attribution, or default-deny by grant | `authority_model.md#grants` | — | **ruled** (2026-09-06): default-deny; the `agent_grant` is the allowlist, read at every enforcement point and widened by a governance write; attribution is required besides and prevents nothing; a wildcard over types is the fail-open shape, not an allowlist |
| 42 | where a skill's harness mechanics live — the tool allowlist, harness preference, model tier, and hook wiring a skill carries: a harness-binding context entity in the record, fields on the `agent`, or the harness's own configuration outside the record | `migration.md#where-a-skills-harness-mechanics-live` | — | **ruled** (2026-09-06): split by what each mechanic is — the tools a principal may invoke are a dimension of its `agent_grant`'s capabilities, and a harness's allowlist is a copy derived from or held equal to it; harness preference and model tier are a `vendor_binding` for the harness as a vendor; hook wiring and environment stay in the harness's configuration; no new context type |
| 43 | what the bootstrap set is — the closed list of records an operator writes before the gate can hold — and whether the operator's own governance writes after bootstrap are gated or exempt | `conformance_suite.md#what-the-bootstrap-set-is-and-whether-the-operators-later-governance-writes-are-gated` | the audit shape of the operator's own governance writes; decision 56, put beside it | **ruled** (2026-09-06, in two halves): the bootstrap set is closed and enumerated, and the list is the thirteen-record table in `conformance_suite.md#the-minimal-record-set-in-order`, every member read back, a write to those types after the set exists never provisioning; and the operator's own governance write after bootstrap is gated like any other — held at the gate, resolved by the operator, and marked self-resolved on the checkpoint (with 47), an audit trail and never a block |
| 44 | whether a sign-off from a step owner whose lease has lapsed closes the step | `conformance_suite.md#whether-a-sign-off-from-a-step-owner-whose-lease-has-lapsed-closes-the-step` | — | **ruled** (2026-09-06): a sign-off carrying `signed` or a blocking verdict requires a held lease by its signer at the moment of the write; a late one is refused at submission, and the current lease holder's verdict is the one that can stand; the operator's `waived` is the one sign-off written without a lease, under its own rule |
| 45 | whether the host a daemon runs on is an external system reached through an adapter — its processes and checkouts artifacts, process control that adapter's outbound classes — or inside the boundary, with the runner's `agent_session` as its only record and process control an operator act | `adapters.md#whether-the-host-a-daemon-runs-on-is-an-external-system` | — | **ruled** (2026-09-06): an external system — its processes and checkouts artifacts with `system` and `external_id`; restart, redeploy, and checkout update the host adapter's action classes, a reset that discards commits `operator_only`; nothing is built until a declaration reads host state, and until one does no declaration reads the host and process control is `operator_only` |
| 46 | what owning confers — sole decision below the domain's blast tier, a required seat above it and on cross-domain actions, or both | `authority_model.md#what-owning-confers-the-required-seat` | — | **ruled** (2026-09-06): the required seat — the principal an `ownership_grant` names is the required approver on any checkpoint whose subject concerns the object, whatever the tier; exclusivity below the tier is not conferred by owning and is grant configuration |
| 47 | whether the raiser of a checkpoint may resolve it | `authority_model.md#the-raiser-of-a-checkpoint-does-not-resolve-it-and-the-operators-self-resolution-is-marked` | — | **ruled** (2026-09-06, with 43): the raiser may not resolve, refused against `RAISED_BY` under 48's counting; the one exception is the operator resolving a checkpoint the operator raised, admitted only with the `self_resolved` mark and refused without it |
| 48 | the counting rule for quorum and separation of duties — an agent counts as its bound principal, as itself, or as itself for attribution only | `authority_model.md#the-counting-rule-an-agent-counts-as-its-bound-principal` | — | **ruled** (2026-09-06): for quorum and separation of duties an agent counts as the principal its `principal_binding` names — one interest; for attribution it is recorded as itself, A-for-B |
| 49 | whether structural checks are count and disjointness over the checkpoint's principals or a second mechanism | `authority_model.md#structural-checks-are-reads-over-the-checkpoints-principal-edges` | — | **ruled** (2026-09-06): count and disjointness over the checkpoint's own `AWAITS`, `RESOLVED_BY`, and `RAISED_BY` edges, under 48's counting rule; no second mechanism, no second object |
| 50 | which structural checks apply at a dozen principals, and where the threshold lives | `authority_model.md#the-thresholds-home-is-the-action_policy-per-class` | — | **ruled** (2026-09-06): the thresholds' home is the `action_policy`, per action class — `quorum` and `disjoint_roles[]` beside `confidence_threshold` and `consent_tolerance`; the default where no value is written is fail-closed, every awaited principal and every named pair; which checks a class carries is a value of those fields — policy data with an author and a date, never a rule of the design — so the row closes with no number supplied |
| 51 | whether initiative approval is the checkpoint or a second approval object | `authority_model.md#initiative-approval-is-the-checkpoint` | — | **ruled** (2026-09-06): the checkpoint; an initiative enters intake as a task, its acceptance is the resolution of the checkpoint on the action it implies — a governance write or a re-prioritization — and no `initiative`, `proposal`, or `approval` entity type exists |
| 52 | what unit stops when an initiative is accepted, who confirms it, and who may propose | `authority_model.md#what-stops-is-a-task-the-owner-seat-confirms-it-through-the-checkpoint-and-proposing-is-a-grant-capability` | — | **ruled** (2026-09-06): what stops is a task — a batch closing naming no successor, or a `priority` correction, each an observation the initiative task `REFERS_TO`; what confirms it is the owner seat on the stopped task, or the operator, as the required approver on the checkpoint whose subject concerns it, the resolution the confirmation and the stop read back; who may propose is a grant capability, default-deny under 41 |
| 53 | whether budget is a scope term that attenuates or a blast tier, and over which resources | `authority_model.md#budget-is-a-scope-term-that-attenuates` | — | **ruled** (2026-09-06): a scope term — a parameter constraint on a capability or a term of a delegation's `scope`, attenuating down the chain — with consumption a derived read over confirmed actions, never a maintained balance; which resources are metered is `metered_resources[]` on the `action_policy` per class, none until written — fail-closed on the limit and not on the permission, since an unmetered class is still gated and a metered class with no budget term written is `NEVER` |
| 54 | whether credit is a stored object or a read model | `authority_model.md#credit-is-a-read-model-over-attribution` | — | **ruled** (2026-09-06): a read model over attribution — sign-offs, actions, and observations with the principals they carry — never stored, as the `authority_chain` is not |
| 55 | whether a second instance of the record's own software, owned by another party, is an external system reached through an adapter — its entities artifacts, writes to it actions approved by the principal accountable for the data — or the same record extended by peering, with a write to it an internal write | `adapters.md#whether-a-second-instance-of-the-record-is-an-external-system` | a declaration whose step reads or writes a second instance; nothing before one | **open** (second workflow-format pass, 2026-09-06; the boundary definition leans to the first, and the record's peering is why it is the operator's) |
| 56 | where the enforcement point sits that ties an admitted governance write to a permitted action — the record's admission check reading the action, a proxy in front of the record, or a sole-writer grant | `gates_and_workflows.md#where-the-enforcement-point-for-a-governance-write-sits` | — | **ruled** (2026-09-06, with 43): a sole-writer grant — for each governance type exactly one principal, the engine acting for the gate, holds the write capability and writes on a permit; every other write is refused at the record under 41; the operator's path after bootstrap runs through the same grant, never around it |
| 57 | which planning levels an instance declares, in which order and under which names — the full set the operator's instance sketches, a shorter one, or the task and one level above it with the rest admitted as registered | `planning_model.md#which-levels-an-instance-declares-and-what-it-calls-them` | the registry's level marks on an instance, and the `amend_<level>` classes its policy lists; no rule of the design, which reads the mark and the ascent and never a level by name | **open** (the planning pass, 2026-09-06; the operator's, as a statement of how they think about their own work) |
| 58 | whether the operator's own amendment to a planning record at a reserved level passes the gate as an `operator_only` action taken by the operator, or is an internal write under the operator's grant with attribution as its only record | `planning_model.md#whether-the-operators-own-amendment-passes-the-gate` | — | **ruled** (2026-09-06, with 43, 47, and 56): it does — the operator's amendment at a reserved level is an action held at the gate, resolved by the operator as a marked self-resolution, and written by the engine on that permit, never an internal write under the operator's own grant |
| 59 | where a minimum model tier per action class or blast radius would live, should an instance choose to declare one — the `action_policy` beside `metered_resources[]` and `recoveries`, a field on the workflow declaration's step, or the `vendor_binding` itself | `gates_and_workflows.md#blast-radius-selects-the-gate-nothing-yet-selects-the-model-a-step-runs-at` | a runner whose bound tier is below a class's floor being scored as a lower-confidence action rather than disqualified outright; decision 60's tier-eligibility-on-re-claim ruling, which assumes this shape | **open** (model-and-harness-routing pass, 2026-09-06; the shape is argued from principles 6 and 9, the values — whether to write one, and to what — are the operator's) |
| 60 | what a step does when the runner already holding its lease loses the model or harness it started with mid-execution: which lapse timing applies, and whether a re-claim may land below the class's `min_tier` (decision 59) once one is written | `failure_posture.md#a-runners-model-or-harness-going-unavailable-mid-step` | — | **open** (model-and-harness-routing pass, 2026-09-06; the tier-eligibility half is ruled in the same section as a consequence of decision 59's shape — a re-claim reads the same floor as a first claim — and only the lapse-timing choice and the reason-class question are left) |

**Every ruled decision now has a heading of its own.** 25 through 29 were opened as bold paragraphs inside
their documents' *What this document does not decide* sections, and the register's pointers resolved to the
enclosing heading; the rulings of 2026-09-05 gave each its own section, as 13, 14, 23, and 24 already had, so
every pointer above lands on the ruling itself. 15 and 30 are argued under headings that name their subject
rather than their number, as 13 and 14 are. Three rows are open — 33 and 34, which the simplification pass of 2026-09-05 opened as removals whose guarantee coverage would shift rather than be exactly preserved, each argued under a heading naming its subject in the document that owns it, and each deferred to the condensation pass; and 55, opened by the second workflow-format pass from what nine declarations could not say and argued beside 45 in `adapters.md`, which turns on the record's peering as a product and is the operator's. The rulings pass of 2026-09-06 (revision 38) took 31, 32, 35, 42, 44, 45, 46, 48, 49, 51, and 54 whole and 43, 50, and 53 in part, on the operator's standing instruction to apply the recommended side of each decision the design's own logic settles and to leave him every one that turns on his values, strategy, or appetite; the second rulings pass the same day (revision 39) re-examined the seven it had left — 36, 47, 52, and 56, and the second halves of 43, 50, and 53 — and ruled each as derivable from principles already in the documents, every ruling under a heading naming its subject and recording, where the operator had recorded a lean, that the lean was considered and set aside and why. 42 was opened by the skills leg of the migration, 43 and 44 by the conformance-suite design, 45 by the workflow-format pass, 46 to 54 by the consistency pass from `authority_model.md`'s long-open questions (C13, marked open beside them, was found settled by C9 and decision 37 and is marked so in place), 55 by the second workflow-format pass, and 56 by the testability pass from the one enforcement point the conformance suite found unnamed; 37 to 41 were opened and ruled
in one revision from the operator's memos of 2026-09-05, each under a heading naming its subject — and the register exists for
the ruled rows too: a ruled row is where a reviewer learns a question was once open and where its rule now
lives, and where the next author reads before opening a question that was already taken. The planning pass
of 2026-09-06 (revision 40) opened 57, the operator's — which levels an instance declares and what it calls
them — and 58, opened and ruled in the same pass as derivable from 43, 47, and 56, both argued in
`planning_model.md`, which also closes gaps G9, G10, and G31 of `migration.md` without opening a decision
for any of them; the open rows were then four — 33, 34, 55, and 57. The model-and-harness-routing pass of
2026-09-06 opened 59 (where a minimum model tier per action class would live), argued in
`gates_and_workflows.md`, and 60 (a runner's lease-held step losing its model or harness mid-execution),
argued in `failure_posture.md`; the same pass settled, without opening a decision, that `runner`
(`vocabulary.md#runner`) already names the seat gap 2 of the operator's request asked after, in
`work_model.md`. The open rows are now six — 33, 34, 55, 57, 59, and 60.

**Decisions 37 to 41 were ruled by derivation on 2026-09-06 and held for the operator's veto; the operator reviewed all five the same day and upheld them.** Nothing in those rows or in their rulings changed; the review is recorded here so that the window is known to have closed by a decision rather than to have lapsed.

**Decisions 31, 33, and 42 are argued under headings of their own in authored companions** — 31 and 42 in
`migration.md`, where the migration they bear on is designed, and 33 in `workflows.md`, where the Stages
lines it concerns are written; the register rows above point at them.

**19 and 22 were never assigned, and the numbers stay unused.** Neither appears in any revision of any
foundation document. They are the gaps left by several documents opening decisions concurrently on this
branch and renumbering around each other, and they are recorded as gaps rather than closed up: renumbering
would break every cross-reference the documents already carry, and silence would invite the next author to
reuse the number for something unrelated. **Do not assign 19 or 22 to a new decision.** The next number is
61.

**20 and 21 were assigned, then renumbered, and two pointers were left behind.** Both were opened in
`payments.md` and renumbered to 27 and 28 before that document was committed, to avoid colliding with 23
through 26, which were opened concurrently in other documents. Two prose pointers kept the old numbers and
resolved to nothing; they now cite 27 and 28. No question was lost — the register records the numbers as
withdrawn so that they are not reused either, and 27 and 28 carry the questions in full.

## Amending a foundation document

Change through a PR that cites the plan decision it consolidates (entity id + decision key), so the event
log and the reviewed statement stay traceable to each other. The keyed entry above ensures this document
is read when the foundation changes, and the checks above run on the change.

**A decision opened in a document is registered in the same change.** A question marked open in a
foundation document and absent from the register above is unfindable — the reporting-without-binding
defect principle 1 names — so the PR that opens a decision adds its row, and the PR that rules one moves
it to **ruled**. The register is the index; the document is the argument.

## Phases and implementation state

The foundation is phase-agnostic. Each document defines its part of the design whole, marks undecided
questions **open** with their options, and says nothing about what a checkout implements. README vision
phases (P1–P5) are a roadmap over that one design; `status.md` records, as of its date, which sections are
built, which are designed and unbuilt, and which are open. Implementation phases (`docs/phases.md`) are a
separate axis and are never merged with the roadmap: an implementation item cites the foundation document
and section it implements, not a vision phase.

An implementation item whose section is marked open in the foundation is premature: it waits on the
decision, marked with the roadmap phase that decision belongs to, and stays open. Which sections are open
is the register above; a reviewer deciding whether an item is premature reads it rather than searching the
documents. An item with no citable
section is either covered by the design as written, in which case it cites the foundation, or conforms to
nothing (above).

Two rules: a foundation document never carries "today", "on main", a commit hash, a count, or an
open-issue reference as evidence a defect is live; an issue/PR is cited only as the record of a decision,
never as state. A PR that adds either is blocked on this section. The second rule has a syntactic form, so
that the lint above (*Decision citations*) can read it rather than a reviewer sensing it: a commit hash
appears in no document in this directory but `status.md`; an issue or pull-request number appears only in a
document's `**Derived from:**` header, in a `Sources:` clause, or in its *Scope*, *Contradictions this
document settles*, *Prior art*, or *Beyond the sources* section — the positions where a document names what
it derived from — and a number anywhere else is a state claim and fails. A decision cited elsewhere in a
document is cited by its register number, or by the header's source in words.
