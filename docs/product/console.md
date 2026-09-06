# The console: a product and UI/UX specification

**Kind:** product specification for one application. It states what the console is for, what its screens
are, and what each reads. It is **not** a foundation document: it decides nothing about the design, and
where it needed the design to say something the design does not, it records a gap rather than inventing an
answer (see *Documentation gaps* below).

**Derived from:** the foundation corpus at `docs/foundation/` — chiefly
[`gates_and_workflows.md`](../foundation/gates_and_workflows.md) decision 37, which is the only place the
design describes this application; [`work_model.md`](../foundation/work_model.md),
[`planning_model.md`](../foundation/planning_model.md), [`data_model.md`](../foundation/data_model.md),
[`principles.md`](../foundation/principles.md), and
[`conformance_suite.md`](../foundation/conformance_suite.md). Also from a read of the existing application
at `apps/task-dashboard/` as it stands on `origin/main`.

**Status:** specification. Nothing here is built. The existing application is described below as what
exists, not as what conforms.

---

## Why this document is here and not in `docs/foundation/`

`docs/foundation/` holds the design: the invariants issue-based work conforms to, argued once, with a
conformance row per rule. A specification for one application is not a design invariant — it is a product
decision *downstream* of the design, and it can be wrong without the design being wrong. Three properties
of the foundation directory decide it:

- **Every foundation document owes a conformance row per rule-bearing heading**
  (`conformance_suite.md#what-the-rule-coverage-check-reads`). A screen list is not a set of rules with
  failing artefacts; putting it there would either break the rule-coverage check or force fabricated rows.
- **The foundation states the design and never the state of a checkout.** This document names an existing
  application, its routes, and its stack — perishable facts of exactly the kind
  `principles.md` invariant 8 keeps out of the foundation and puts in `status.md`.
- **The reading list is path-keyed** (`conformance.md#read-when-these-paths-changed`), and a foundation
  document earns its place by being what a review reads when certain paths change. This document is read
  when someone builds the console, which is a different trigger.

It goes under `docs/product/` — a new directory, because `docs/` today has no home for "what we are
building, as distinct from how the system is designed or how to operate it". `docs/developer/` is
per-tool rules, `docs/policies/` is agent conduct, `docs/runbooks/` and `docs/on_demand/` are operational.
A product specification is none of those. If the corpus later prefers a flat `docs/console.md`, moving it
costs one `git mv`.

**One thing this document deliberately does not do:** it does not amend the foundation, and it registers no
decisions. Four PRs are open against `docs/foundation/` as this is written. Where the specification ran
out of design to build against, the gap is recorded in the last section and left there.

---

## 1. What the console is for

The design already answers this, in one sentence, and the specification adopts it rather than composing
a new one:

> An operator wanting to see the results of all work — what is open, what is held, what closed and with
> which verdicts — reads the record: the batches and their chains, the sign-offs and the findings they
> carry, the checkpoints and who resolved them, the artifacts by edge. **A dashboard is that read rendered
> for a principal, and it is nothing more than that read.**
> — `gates_and_workflows.md#work-is-reviewed-on-the-record-and-a-channel-carries-only-what-awaits-the-operator-or-cannot-wait` (decision 37, ruled 2026-09-06)

Four consequences follow directly from that ruling, and they are the console's whole architecture.

**It is a read, not a system.** The console holds no state that is a source of truth. Decision 37 names the
failing artefact by name: "a client that kept a picture of the queue beside the record would be a second
source of truth for the operator's decisions, and stale in the direction that matters (principle 11)."
Caches are permitted as caches — a cache that is wrong is a performance defect; a cache that is *read as
the answer* is the defect the ruling forbids. The test is whether anything reads the copy when the record
disagrees.

**It is made under the operator's credential and grant**, "like every other read"
(`authority_model.md#grants`). The console is not a principal. It has no identity of its own, no service
credential, and no grant. It carries whichever principal is reading.

**It crosses no boundary and is not an adapter.** The record is on the inside of the one boundary this
design has, so the console needs no adapter and is subject to none of the six admission obligations
(`adapters.md#the-admission-contract`). This is why it may read the record directly and why it must never
reach an external system: an artifact's state comes from the adapter's observations on the record, never
from a live call to the code host.

**It has no write contract of its own.** Whatever the operator writes through it is a write the design
already attributes to the operator principal — a checkpoint's resolution, a finding on a batch, a `waived`
sign-off, a task. The console adds no fifth. This is the crispest scoping rule in the document: *a
proposed console write that is not one of those four is out of scope until the design names it.*

### Who reads it

The design's word is **principal** (`vocabulary.md`), and the console must not invent a UI-side actor
(invariant 12 — the vocabulary is as small as the design needs, and no term overlaps another). Three
principals read it, and the design distinguishes them by grant, not by a role the console assigns:

| Reader | What they are, in the design's terms | What they come for |
|---|---|---|
| **the operator** | the human principal; the one `operator` entity every authority edge attaches to (`data_model.md#concepts`) | the decision queue; whether work is moving; what closed and with which verdicts |
| **an agent** | a non-human principal, reading under its own grant and bounded additionally by its `agent.context_entity_types[]` | its own claimable pool, in priority order; the batch and sign-offs it must read before it acts |
| **the operator-facing agent** | the agent the roster resolves to that role for a batch's project (`vocabulary.md#operator-facing-agent`) | the open checkpoints whose `AWAITS` names the operator, to carry them to the channel |

Two of those three already have a **retrieval contract** stating exactly what they read before acting and
what an absence means (`data_model.md#retrieval-contract`). The operator reading a console does not — see
gap **G1**.

**What the console is not for.** It is not the operator's notification surface. A channel carries a
declared subset — a checkpoint awaiting the operator, the announcement path, a delivery a workflow
declared — and completed work is not carried unless the binding or a `deliver` step says so. The console
is the other surface: it answers "what is the state of the work", which is a read over everything; the
channel answers "what needs me now". Building a notification stream into the console makes it imitate the
channel, and decision 37's reasoning explicitly refuses that.

### The obligation runs both ways: some things must be visible, some must provably not be

A reader's surface is usually specified as what it shows. Several conformance rows go red *because*
something reached a human, so the console carries a negative obligation of the same standing as its
positive one:

- **A defect must be surfaced, and an ordinary record must not.** A mail message on an untracked thread is
  an ordinary record; surfacing it as a defect is GM-2's red. The distinction is the record's, not the
  console's to soften.
- **A withheld advisory field must be absent everywhere**, not merely hidden behind a toggle (GH-9). If
  the record should not hold it, the console must not hold it either — including in a client-side cache
  or a search index.
- **Public artifacts carry no exploit detail** (WF-12), and `prompt_markdown` carries no
  operator-identifying string (GW-29a). Anything the console renders from those fields inherits the rule.
- **Completed work does not reach the channel.** The console showing a completion is correct; the console
  *pushing* one is the channel behaviour decision 37 refuses.

This is why "show everything the operator's grant admits" is not a safe default for a screen, and why
each screen below names what it reads rather than deferring to a general permission check.

### The vocabulary rule, applied to the interface

Invariant 12 binds here as much as anywhere: *as few terms as the design needs, and no fewer; no term
overlaps another.* A console label is a term. The rule the specification adopts:

> **Every noun on a screen is either a vocabulary term used in the design's sense, or a plain-English word
> the design does not define. It is never a new name for something the design already names.**

`vocabulary.md` bans specific substitutions by name — "chip", "work item", and "work entity" are banned for
task; "gate_status" is banned for `step_status`. It also distinguishes a concept written in prose (step
status, sign-off, action gate) from a field written in its record spelling and code font (`step_status`,
`action_type`). The console follows both: prose in headings and labels, code font where the reader would
need to type the string into a query.

The existing application fails this rule comprehensively, and that is the largest single finding of the
review below.

---

## 2. What exists today

Read at `apps/task-dashboard/` on `origin/main`. This section is a measurement and will decay; it is here
because a revamp specification that does not account for what is there is worthless.

**Stack.** React 18 + Vite 6, TypeScript strict, Tailwind + shadcn/ui, a hand-rolled hash router
(`src/route.ts`), Vitest with exactly one test file (`src/taskSearch.test.ts`, covering search parsing).
~16,800 lines of application source. No TODOs, no stubs, no `@ts-ignore` — it is a finished codebase, not
a sketch.

**Data.** Neotoma exclusively, over HTTP, through a Vite **dev-server middleware** (`server/neotomaProxy.ts`,
~2,280 lines) that attaches the bearer token so the browser never holds it. Sixteen `GET /api/*` routes,
all read-only. There are no fixtures and no local data files. This part already satisfies the operator's
"pulling from the Neotoma data, exclusively" constraint and should be preserved.

**Screens.** Seven nav destinations — Now, Tasks, Agents, Sessions, Workflows, Lifecycle, Schemas — plus
detail routes for agents, sessions, questions, and a generic entity page, plus a persistent open-questions
sidebar and a Cmd-K global search.

**Three deliberate non-Neotoma sources**, each documented in-file as non-live: a generated snapshot of a
static repo grep (`src/codeUsage.ts`, pinned to a commit), the 11-stage lifecycle vocabulary transcribed
from Python (`src/lifecycleData.ts`), and hand-verified execution claims (`src/workflowData.ts`). These are
honest about themselves, and two of the three are answering questions Neotoma genuinely cannot.

**What the review finds.** The application is well built and reads the right store. Its problem is that it
renders **the retired vocabulary**, and it renders concepts the design does not have.

| What the console shows today | What the design says | Where |
|---|---|---|
| `workflow_definition` entities and their `gates[]` | **retired name**; re-typed to `workflow`, whose `gates[]` become `steps[]` with `step_name`, `owner_role`, `join_step` | `migration.md#the-mapping` |
| an 11-stage task lifecycle with per-stage counts | **there is no task lifecycle; there are batches.** A task carries status and edges only. This is contradiction C1, and it is closed | `work_model.md#there-is-no-task-lifecycle-there-are-batches` |
| task status folded into UI-side "buckets" | the status vocabulary is the record's; the registered `task` type declares its terminal set, and the reader maps every spelling onto `open` or terminal | `work_model.md#what-a-claim-predicate-treats-as-claimable` |
| "undispatched" for an unset `assigned_to` | the design's reading is **assigned-and-unclaimed**, and an unassigned task is a shared pool. "Undispatched" implies a dispatcher; pull is the only delivery | `work_model.md#assignment-restricts-eligibility-it-never-creates-a-lease` |
| agent "tiers" T1–T4 with UI-side blurbs | the design has `role` resolved through the `swarm_roster`, and `agent` definitions. No tier concept exists | `vocabulary.md#role` |
| "open questions" as `task` entities carrying `category: "open_question"` | the design's open questions are `decision` entities and the register in `conformance.md#the-register-of-open-design-decisions`; a question put to a principal is a `checkpoint` | see gap **G7** |
| — (no screen) | `checkpoint`, the one decision queue | `gates_and_workflows.md#the-checkpoint` |
| — (no screen) | `batch`, `sign_off`, `finding`, `action`, the chain, the ascent | throughout |

The last row is the important one. **The console today has no view of the decision queue, no view of a
batch, no view of a sign-off or a finding, and no view of an action or the gate that held it.** The
`checkpoint_brief` type — itself a retired name — appears in the app only as an example of schema drift on
the Schemas page. The `mcp__ateles__*` server already exposes `list_checkpoints`, `get_gate_status`,
`list_pipeline_queue`, and `get_dispatch_health`, and the console reads none of them (it talks to Neotoma
directly, which is correct; the point is that this state is reachable and unrendered).

**Disposition.** This is a revamp, not a rewrite. Keep the stack, the Neotoma-only data layer, the proxy
pattern, the single `EntityDetail` renderer, the honest `Coverage`/`Count` types (`exact` | `saturated` |
`unmeasured` — that distinction is invariant 7, unknown staying distinct from a verdict, and it is already
right), and the skeleton discipline. Replace the screens and the vocabulary.

---

## 3. The screens

Nine screens. Each states the reader's question, what it shows, the entity types it reads, and the
foundation section it surfaces. Where a screen needs something the design does not specify, the gap is
marked inline as **[G*n*]** and expanded in section 6.

A convention used throughout: **a derived read is computed at read time and never stored** (invariant 11).
Where a column below is a derived read, it says so, because that is the difference between a screen that
is a read of the record and a screen that is a second source of truth.

### Screen states (empty · unknown · error)

Every screen has three mutually exclusive presentation states for each list or primary read. S1 already
states the triad for the queue (`data_model.md#retrieval-contract`, invariant 7); **this subsection
extends that rule to S2–S9** so implementers do not treat S1 as special-case prose.

- **empty** — the read succeeded; zero matching records (or zero claimable / zero open / whatever that
  screen defines as “nothing”). Empty is never inferred from failure.
- **unknown** — a required record, edge, or coverage window is unreachable; the proxy or primary read
  failed; or the design uses “unknown” for saturated-unmeasured coverage. **Never rendered as empty.**
- **error** — a write the operator just took failed, or a secondary read-back that must confirm that
  write failed. Distinct from unknown-on-list: the list may still be known while the action is not.

**Actionable recovery is mandatory on unknown and error.** At least one of: retry the read or write,
re-authenticate, open the subject record, or re-check the credential. Surface copy as `[COPY: …]`
placeholders — Paradisaea owns final strings. Confirmation / read-back *foundation* posture for the four
permitted writes remains gap **[G12]**; principal-reading-a-console retrieval remains gap **[G1]**. This
section does not invent answers for either.

| Screen | Empty means | Unknown means | Error means | Recovery affordance |
|---|---|---|---|---|
| S1 | open-checkpoint read succeeded; zero rows for the reader | queue read failed / `AWAITS` edge unreadable | resolution write failed or read-back failed | `[COPY: retry resolve]` / `[COPY: re-auth]` / open subject |
| S2 | claimable + open lists read OK; zero tasks in view | task / batch / `LEASE` read failed | (on task detail write paths) write failed | `[COPY: retry load]` / open record |
| S3 | batch read OK; no findings to show (a batch with no steps is illegal under a declaration — surface that as unknown or a declaration defect, not empty) | batch / `sign_off` / `LEASE` read failed | `waived` write / finding write failed | `[COPY: retry]` / open batch entity |
| S4 | chain read OK; no `FOLLOWS` predecessors (new task) | batch chain unreadable | redo-task write failed (otherwise n/a — read-only) | `[COPY: retry]` / open task |
| S5 | tree read OK; no children under focus | `PART_OF` / planning-type read failed | n/a (read-only) | `[COPY: retry]`; unplanned ≠ error |
| S6 | findings read OK; zero match filters | finding / `sign_off` edges failed | finding write failed | `[COPY: retry]` / `[COPY: save finding again]` |
| S7 | adapters read OK; zero adapters (unusual) | window observation / coverage unreadable — **not** the same as domain `unknown` on an artifact check | n/a (read-only) | `[COPY: retry]`; do not paint silence as healthy |
| S8 | register parse OK **and** planning `decision` read OK; both sides zero | markdown register unreadable **or** decision entities failed — show which half failed | n/a | `[COPY: retry]`; show source (file vs record) |
| S9 | search/detail: entity not found after a successful lookup | entity / snapshot read failed | n/a | `[COPY: retry]`; distinguish not-found vs failed-read |

### S1 · The queue — the operator's decisions

**The question:** *what needs me, and what am I deciding?*

The one decision queue (`gates_and_workflows.md#the-checkpoint`). This is the screen the console does not
have today and the reason to do the revamp.

**Reads:** `checkpoint` (`reason`, `needed_input`, `options[]`, `status`, `deferral_until`, `raised_at`,
`self_resolved`), and by edge: `CHECKPOINTS` → the subject (exactly one `action` or one `task`), `AWAITS` →
principal, `RAISED_BY`, `RESOLVED_BY`. For an action subject: the `action` (`action_type`, `confidence`,
`dedup_key`) and the `action_policy` that classified it. For a task subject: the `task` and its batch.

**Shows.** One row per open checkpoint whose `AWAITS` names the reader — the queue is a derived read
("open checkpoints whose `AWAITS` names the reader", `data_model.md#concepts`), not a stored list. Each row
carries the reason class, the subject rendered as what it is (an action names its class and its target
artifact; a task names its title and its batch), the options the checkpoint itself carries, and whom else
it awaits. **The options come from the checkpoint and are never composed by the console** — the
operator-facing agent's contract already forbids reading the operator's grants as a source of what to
present, "the checkpoint carries its own options" (`data_model.md#retrieval-contract`), and the console is
under the same constraint for the same reason.

**The card's field list is fixed by GW-47**, which goes red when any of them is absent: the reason, the
needed input, the options, whom it awaits, and — once resolved — the resolver. That row also names a
failing artefact this screen must avoid: "the resolver a bare status write". A resolution recorded through
the console carries its resolver and its `resolution_note`, never a status flip.

**`unclaimed_step` is the one reason class that does not hold its subject**, and the queue must show that:
its checkpoint reorders and never holds, so the step it names stays claimable by its role. Every other
task-subject class holds the task from claim. A queue that renders all reasons identically misstates the
state of the work for exactly one class.

**One row per stopped batch, not one per task.** Where a condition stops a whole batch, one checkpoint is
raised on one of its tasks, naming the batch and the step in `needed_input`; which task is the subject
carries no meaning and the console must not present it as though it did. The batch's other tasks are read
as held through their `ADDRESSED_BY` edge.

**Grouping.** Consent over several like actions is one presentation of several checkpoints, never one
checkpoint over several subjects. The queue presents open checkpoints that share **a batch, a step, and an
action class** as one set and takes one decision over the set; what is recorded is one resolution per
checkpoint, each attributed and each resuming its own action. So the set is a presentation affordance with
a defined key, and the row count under it must equal the number of resolutions written — "a decision over
forty archives is forty rows any reader can count, and a refusal of one of them is a refusal of one."
A refusal of one member must be reachable without leaving the set.

**A payment checkpoint shows exactly what the verifier signed** — payee, amount, currency, period, and
rail, as the `verify` sign-off recorded them (decision 27,
`payments.md#a-payments-approver-is-shown-exactly-what-the-verifier-signed`). Not the current values read
fresh; the recorded ones. This is a screen-level rule with a conformance row behind it.

**Writes:** the checkpoint's resolution — one of the four writes decision 37 admits. A self-resolution (the
operator resolving a checkpoint the operator raised) must be marked as one at the write, and the console
surfaces the mark rather than hiding it (decision 47).

**Empty, unknown, and failed-read are different.** "An empty queue is reported as an empty queue, never
inferred from a failed read — a read that failed is `unknown` and is announced as such"
(`data_model.md#retrieval-contract`). The console must have three states here, not two. This is invariant 7
on a screen. See §3 Screen states for the cross-cutting triad and recovery matrix.

**Gaps: [G1]** (no retrieval contract for a principal reading a console), **[G2]** (deferral, timeout, and
the bounded-deferral UI), **[G3]** (quorum display).

### S2 · Work — tasks and their batches

**The question:** *what is open, what is moving, and what is stuck?*

**Empty, unknown, and failed-read are different.** See §3 Screen states.

Replaces today's Tasks list. The correction is conceptual, not cosmetic: a task's position is not a stage
on a lifecycle, it is **the batch it is in and that batch's chain**.

**Reads:** `task` (`status`, `title`, `description`, `acceptance_criteria[]`, `action_type[]`,
`assigned_to`, `priority`, `due_date`, `recurrence`), `batch` (`workflow_type`, `status`, `opened_at`),
the `LEASE` edge, `checkpoint` by `CHECKPOINTS` edge, `workflow` for the step list.

**Shows.** One row per task, with these columns, each of them a derived read except the first two:

| Column | Derived from |
|---|---|
| title, status | the record; status mapped onto `open` or terminal by the tolerant reader |
| current batch and its workflow | the task's live `ADDRESSED_BY` edge — "asking *which workflow is this task in* is answered by its live batch alone" |
| current step | step state per step on the batch, read from the batch, the leases, and the sign-offs |
| **claimable** | not terminal, no held lease, no open checkpoint holding it, and `assigned_to` unset or naming the reader (`vocabulary.md#claimable`) |
| **held** | an open checkpoint on it — *not* a status. `blocked` is a retired status and must not appear as one |
| lease state | `held` / `lapsed` / `returned`, derived from `expires_at` against real time; nothing transitions it |
| **active** | held lease plus activity within the lease window. A derived read, never a state |

**The words that are forbidden on this screen**, each because the design retired them or bans them by
name: "blocked" as a status; "executing" or "running" as states (both retired —
`work_model.md#liveness-is-derived-from-activity-at-read-time-never-declared`); "running" and "in flight"
for **active**, which `vocabulary.md#active` bans outright, and "active" as a status value rather than a
derived read; "routed", "verified", or "in review" as a task status (all are facts about a batch, a lease,
or a sign-off); "undispatched"; "chip", "work item", "work entity" for task. A task with no lease holder is
**assigned-and-unclaimed** if `assigned_to` names someone, and in the **shared pool** if it does not.

`vocabulary.md#active` is also the one place the design tells this application what to do with a value:
*"Never stored; a dashboard derives live-versus-quiet from it."* The live/quiet distinction on this screen
is that derived read and is computed at render, never persisted.

**Ordering.** The claimable pool is ordered by a derived read over the ascent's standing, `due_date`, the
workflow's declared urgency, and the blast radius of what the task would produce — read at read time,
never stored (`work_model.md#priority-orders-the-claimable-pool-it-does-not-enter-it`). The stored
`priority` field and the derived order can disagree, and where they do the derived read is what is shown.
**This screen is load-bearing for a design rule**, not a convenience: "a principal's tool surface lists
claimable work in priority order, so declining the top of the list is a visible, attributable act rather
than an invisible skip." The console is that surface for a human principal.
**Gap [G4]:** the design names the inputs to the ordering but not their precedence, so two implementations
can order the same pool differently and both claim conformance.

**Detail.** One task shows its statement, its acceptance criteria, its ascent (S5), its chain, its current
batch with per-step state, the findings recorded against it, and the artifacts it refers to. It shows the
`FOLLOWS` edge for a recurring task's history — one live instance, its predecessors readable.

### S3 · A batch — steps, sign-offs, and what closed it

**The question:** *what has been judged here, by whom, against what, and what is left?*

**Empty, unknown, and failed-read are different.** See §3 Screen states.

**Reads:** `batch`, `workflow` (the declaration: `steps[]` with `owner_role`, `required`, `applies_when`,
`on_fail`, `rounds_cap`, `reads_to_enter[]`, `reads_to_close[]`, `unclaimed_after`, `hold_bound`),
`sign_off`, `finding`, `LEASE` edges with `step_name`, `task` by `ADDRESSED_BY`, `artifact` by `PRODUCES`,
`DEPENDS_ON` → task.

**Shows.** The declaration's steps in order, and for each: its state — **open, claimed, or signed** — derived
from the batch, the leases, and the sign-offs, never read from a per-step status row (a per-step status row
is a named failing artefact, GW-3).

**The projection rule, which is this screen's sharpest constraint.** `step_status` is the projection of the
batch's sign-offs, proved equal to them by a reconciler; neither is a second source of truth. GW-3a's
failing artefact is stated in reader's terms and names this console directly: *"a reader takes the
projection where it disagrees with the sign-offs."* So the screen may show `step_status` as the projection
it is, and **where it disagrees with the sign-offs the sign-offs are what the screen states**, with the
disagreement itself surfaced rather than smoothed. Read with GW-53's "a stored picture of the queue beside
the record", these two rows are the design's standing answer to whether the console may cache: it may
project, and if it projects it must reconcile and must prefer the source on disagreement.

Per signed step: the verdict (`signed`, a blocking value, or `waived` — and nothing else; a host's
approve/request-changes tokens are the adapter's inbound mapping and never appear as verdicts), the signer,
`signed_at`, the artifact refs **with the head each was pinned to at the moment of judgement**, and the
findings the sign-off carries.

**The staleness read matters and is a real column:** whether a referenced artifact's *current* pinned state
differs from the one judged. A sign-off is pinned and never re-pointed; the console shows the drift rather
than hiding it, because that drift is what tells a reader the verdict judged a different head.

**Holding.** A step is holding when it has a held lease, a finding naming a condition, and no sign-off. The
screen shows the condition, what would resolve it, and the `hold_bound` it is running against. A holding
step is not a state on the batch — there is no held, paused, or waiting field, and the console must derive
it (`data_model.md#concepts`, batch row, Deliberately-not-a-field).

**Dependencies.** `DEPENDS_ON` → task, with `created_at` and `ended_at` on the edge. A dependency cycle is a
checkpoint (`dependency_cycle`) and appears in S1.

**Closing.** The closing sign-off, its successor (one, or none where the declaration permits none), and the
`FOLLOWS` edge to the batch it opened.

### S4 · The chain — where a task has been

**The question:** *how did this get here, and has it landed?*

**Empty, unknown, and failed-read are different.** See §3 Screen states.

Not a separate nav destination; the spine of the task detail in S2. It is called the **chain** and never
the "history" or the "pipeline": the chain is the batches along `FOLLOWS`, and the **ascent** is the
planning records along `PART_OF`. The design insists the two are different reads of the same task — "where
it has been, and what it is for" — and the console must not merge them into one breadcrumb.

**Shows.** Each link: one batch, one workflow, entered by one verdict, with the sign-off that named it. A
closed batch is never reopened, and the console offers no affordance to do so. Where the operator wants a
redo, the affordance is **a finding on the closed batch**, and the redo is a new task entering intake
referring to the closed batch's artifacts — the console may offer to create that task, since a task is one
of the four writes the design already attributes to the operator.

**"Landed" is a derived read**, and this screen is where it is shown: the chain ended under a declaration
that permits its ending there. Not a badge someone sets. A task reading terminal after a `merge_pr` under a
declaration that permits no such end is the failing artefact of invariant 10, and this screen is where a
reader would see it.

### S5 · The hierarchy — the planning records and the ascent

**The question:** *what is this work for, and what is under this record?*

**Empty, unknown, and failed-read are different.** See §3 Screen states. Unplanned ascent is labeled
unplanned, not as an error.

**Reads:** planning records (a registered type the registry **marks as a planning type**, with a level —
the design fixes the shape and not the names), `task` by `PART_OF`, `decision` by `PART_OF`, `SUPERSEDES`
between decisions, `REFERS_TO` from tasks that concern a record they are not under, `DEPENDS_ON` from a
planning batch.

**Shows.** The tree by `PART_OF`, upward one edge per record, rooted at the record with no `PART_OF`. The
console **reads the level from the type's registry mark** and does not hardcode a ladder of names —
"an instance that holds six levels and an instance that holds two are both instances of it." Hardcoding
"strategy → objective → plan → project" would make the console non-portable to a fork, which is the
opposite of what the design is for.

Per record, all derived, none stored: completion (every descendant task terminal), open and terminal
counts, descendants held by an open checkpoint, most recent activity beneath it, the fraction of
descendants landed, the open descendants in priority order, its standing `decision` entities (those no
later decision supersedes), and whether it is maintained (one live `planning` task under it).

**A task's ascent** is the records above it read along `PART_OF` to the root. A task whose ascent is empty
is **unplanned** — a design word, shown as such, not as an error. A task whose `PART_OF` edges name two
planning records is a held case the migration defines and is surfaced for a `planning` batch to judge, not
silently resolved by picking one.

**No progress bars over stored status.** A planning record has no stored `status`, `outcome`, or progress,
and no `todos` list; those are named as deliberately-not-a-field. Everything on this screen is counted
from descendants at read time.

### S6 · Findings and verdicts

**The question:** *what was objected to, does it block, and was it discharged?*

**Empty, unknown, and failed-read are different.** See §3 Screen states.

**Reads:** `finding` (`severity`, `kind`, `scope`, `evidence`, `text`, `step_name`, `recorded_at`),
`sign_off` by `PART_OF`, `batch` by `REFERS_TO`, and the `task` a finding produced.

**Shows.** Findings across batches, filterable by the axes the design defines and no others:

- **`severity`** — `blocking` or `non_blocking`. **The severity is what blocks, never the verdict.** A
  sign-off carrying a blocking finding under a non-blocking verdict is refused at submission, so the
  console should never encounter one; if it does, that is the display of a defect and not a state to
  render smoothly.
- **`kind`** — `implementation_only` (routable to an implementer) or `decision_or_attestation` (not
  routable at all, because routing it would ask an implementer to supply the judgement the finding exists
  to demand). The screen must not offer a "route this" affordance on the second kind.
- **`scope`** — the standing axis: `batch` (one-off), `step`, `workflow`, `agent`, or `unknown`. `unknown`
  raises `undetermined_scope` and **is never coerced** — the console shows it as unknown.
- **`evidence`** — required on a blocking finding: the executed command and its output, or the mechanism
  that executed it and the result read. The console shows the evidence beside the block, because a
  blocking verdict that names no executed check is the thing invariant 2 exists to catch.

**Derived:** whether a standing finding was discharged (the task it produced reached a terminal status).
Not a stored resolved flag — that is a named non-field.

The operator's own input on reviewed work is a finding and takes the same axes as any other. That is the
console's write path for review, and it is one of the four writes decision 37 admits.

### S7 · Adapters — the boundary and its drop counts

**The question:** *is the boundary healthy, and what did it decide not to handle?*

**Empty, unknown, and failed-read are different.** See §3 Screen states. Domain `unknown` on an
artifact check (below) is not the same as **screen unknown** from a failed observation / coverage read.

**Reads:** the adapter's `agent` and its `agent_session`, whose per-window observations carry the window,
the **coverage** of the polls or deliveries made in it, and the **dispositions counted**; `artifact`
(`system`, `external_id`, `state`, `checks`, `head`).

**Shows.** Per adapter, per declared window: coverage, the four outcomes, and `dropped` with its reasons.
**Drops are aggregate by design** — counted per window on the adapter's own `agent_session`, never a table
of deliveries. The console must not offer a per-delivery drill-down, because a stored delivery log is
explicitly forbidden (`data_model.md#concepts`), and offering the affordance would create demand for the
record the design refuses to keep.

**`unknown` is a rendered value, not a blank.** A CI state the adapter could not read is `unknown` on the
artifact and holds the step; the console shows `unknown`, distinct from failing and from passing. That
domain value is still shown when the observation read *succeeded*; a failed observation read is screen
unknown per §3, not a blank health panel and not a coerced empty adapter list.

**Silence is a derived read**, and it is the one that matters most here: a daemon is **silent** when no
window observation exists past its declared window *while the record is reachable*. The screen must not
present absence of activity as health — "an idle swarm and a halted one look the same by that measure."
Silence under a successful read is not screen unknown; silence must still not be painted as healthy.

**The console never calls the external system.** Everything on this screen is the adapter's observations on
the record. **Gap [G5]:** the drop counters are named as one of the design's instruments requiring a
planted positive before a zero is believed, and the design does not say whether a reader is entitled to see
whether that planted positive has run — so a zero on this screen is not yet distinguishable from a
disconnected counter.

### S8 · The register — open decisions

**The question:** *what has the design not decided, and what is blocked on it?*

**Empty, unknown, and failed-read are different.** See §3 Screen states. When only one half of the dual
read fails, show which half (markdown register vs planning `decision` entities).

**Reads:** the register table in `conformance.md#the-register-of-open-design-decisions`, and `decision`
entities `PART_OF` planning records.

**Shows.** Two distinct things that must not be merged, because they are different objects with different
homes:

1. **Open design decisions** — the foundation's own register: number, the question in one line, the
   document that argues it, what it blocks, and status (`open`, `ruled`, `ruled in part`, `withdrawn`,
   `not a decision`). This is authored in a markdown file, not in the record. **Gap [G6]:** the register is
   a document, so the console can only render it by parsing the file — there is no `decision` entity
   for a foundation-level open question, and the design does not say there should be.
2. **Planning decisions** — `decision` entities `PART_OF` a planning record, with `SUPERSEDES` between
   them. These are in the record and are read normally. Which stand under a record is a derived read
   (those no later decision supersedes); a reversal keeps both readable.

**Gap [G7]:** today's "open questions" in the app are `task` entities carrying `category: "open_question"`,
which is neither of the above. The design has no such concept, and this specification does not invent one.

### S9 · The record — an entity, and the swarm's own shape

**The question:** *what does the record actually hold here?*

**Empty, unknown, and failed-read are different.** See §3 Screen states. Not-found after a successful
lookup is empty; a failed entity or snapshot read is unknown.

Keeps the existing generic `EntityDetail` renderer and the Cmd-K search, which are good and which the
revamp should not throw away. Adds the design's own framing:

- **Every field shown is the latest observation of that field**, with its timestamp and provenance. An
  observation is not an entity type — it is how anything is held — so the console renders provenance as a
  property of each value rather than as a separate log.
- **A projection is shown as a projection**, with its reconciliation state. `step_status` is the only one.
- The schema view is retained: the registered types, their declared fields and versions. It is genuinely
  useful and it is the screen where **schema drift** is visible. The design names four distinct failure
  modes of drift (`conformance.md#schema-drift-is-four-failure-modes-not-one`), and the console should
  name which one it is showing rather than presenting one undifferentiated "drift" count.

---

## 4. The pairing with the conformance suite

This is the part the operator asked for that needs a mechanism rather than an intention. The claim to make
concrete: *the interface and the tests are built against the same source.*

### What a row actually is

A conformance-matrix row has six cells:

```
| id | rule (a link to the section that owns it) | setup | action | the observable that goes red | class |
```

with `id` a stable prefixed number (`WM-1`, `GW-53`, `AD-12`), and `class` one of **M** (mechanical, a
test asserts it), **R** (review-only), **U** (untestable as written), **P** (pending an open decision), or
**D** (definitional). A real row:

```
| WM-1 | work_model.md#pull-is-the-only-delivery... | T1, two agents A and B with grants
| A writes a LEASE edge naming B as holder | the write is accepted; or RP shows a lease
whose writing credential is not the holder's | M |
```

Two structural facts make the pairing possible:

**Setups are named fixtures, and a fixture is a record state.** `B0` is the bootstrap set. `T1` is `B0`
plus one task whose intake batch is open at `classify`. `T-routed(W)` is a task whose intake batch closed
naming workflow `W`. `T-at(W, s)` is a batch of `W` with every step before `s` signed and `s` open. `LEG`
is a legacy-shaped population under the retired type names. A row states only what it *adds* to a fixture.

**A screen is also a function of a record state.** That is the whole join. A screen renders a record state;
a fixture *is* a record state; therefore a fixture can be rendered.

**The measured shape of the matrix**, counted on `feat/foundation-p1-docs` today: **372 conformance rules**
(338 M, 2 R, 3 P, 1 D), across 17 prefixes. The document holds **519 table rows in total** — the 372 rules
plus 34 findings (`U-*`, rules with no failing artefact), 18 contradictions (`X-*`), the 14-row bootstrap
table, the fixture table, and the permutation axes. The pairing below concerns the 372.

### The mechanism: three couplings, in increasing strength

#### Coupling 1 — the fixture gallery (the strong one)

**Every named fixture gets a rendered state, and each screen is built and reviewed against it.**

The suite already stands up `B0`, `T1`, `T-routed(W)`, `T-at(W, s)`, and `LEG` on a disposable instance.
Point the console at that instance, at each fixture, and capture what every screen renders. This gives
exactly what the operator asked for — "as we build out the functionality, we actually see it" — without
inventing any test data, because *the fixtures are not mock data; they are the record states the design's
own tests are written against.*

Concretely: a `console/fixtures/<fixture>/<screen>` capture per (fixture × screen) pair, produced by the
suite's own harness after it builds the fixture and before it tears the instance down. Reviewing a screen
means reviewing its render at `T-at(feature, qa)`, not clicking around production.

**This is the honest answer to "UI-driven".** It is not that the UI drives the tests; it is that the tests
already construct every interesting state of the record, and the UI is a second reader of those states. A
screen that cannot render `T-at(W, s)` legibly has found either a UI defect or a gap in what the design
says a reader needs.

**One hard constraint, and it is not negotiable.** The suite runs against a **disposable instance, created
empty per run and destroyed after it**, protected by four independent layers — a credential production does
not know, a positive nonce assertion, a refusal on a non-empty store, and no production credential in the
process environment. The console pointed at fixtures is pointed at *that* instance, under the run-minted
credential. It must never be a mode of the production console, and the fixture capture must never run with
a production credential materialized. Layer 3 in particular means the console cannot "seed a fixture into
production to look at it" — the suite refuses a non-empty store on purpose.

#### Coupling 2 — the surface column (the cheap one)

**Add one optional cell to a matrix row: the console surface on which the rule's violation would be
visible, or `—`.**

Not a new document and not a new object: the row already points at its rule by anchor, and this is the same
kind of pointer aimed at a screen id (`S1`…`S9`). `WM-1`'s mutant — a lease written by a credential that is
not the holder's — is visible on **S2** (the lease state column) and on **S3** (the step's claimant).
`GW-53`'s mutant "a stored picture of the queue beside the record" is visible on **S1**, and is in fact
about S1. `AD-12`'s "an event coerced to the nearest outcome instead of `unknown`" is visible on **S7**.

Two properties make this worth doing and keep it cheap:

- It is **derived, not authored twice.** The screen ids live in this document; the row points at one. If a
  screen is renamed, the pointer breaks loudly, exactly as the anchor check already breaks on a renamed
  heading.
- It produces a **coverage read in both directions**: which rules have no surface (below), and which
  screens surface no rule — a screen in that second set is a screen nobody can justify from the design,
  which is a finding about the screen.

**This cell is a proposal, not a change made here.** Adding a column to `conformance_suite.md` is a
foundation edit, four PRs are open against those files, and this specification does not make foundation
edits. It is recorded as gap **[G8]** and as the one concrete amendment this work would ask for.

#### Coupling 3 — the bootstrap sequence, and whether the UI can drive it

**Answer: it can display it, and it must not drive most of it.**

The bootstrap sequence is 14 rows (numbered 0–13) — the empty nonce-identified instance, the registry, the
`operator` entity, the credential binding, one `ownership_grant` per type, the `action_policy`, the first
agents, their grants and bindings, the `swarm_roster`, the context entities intake reads, the intake
declaration, the successor declarations, and per-adapter provisioning. Decision 43 ruled this the **closed
list**: every member is read back, and a write to any of those thirteen kinds of record *after* the set
exists is not provisioning — it is an action like any other, and the operator's own such write is gated,
its checkpoint resolved by the operator and marked self-resolved.

So a "run the bootstrap from the UI" wizard is the wrong shape, and the design says why in two places:
**every one of those writes is a governance write**, reserved to the operator by default (ruling 18), and
the console has **no write contract of its own** — only the four writes decision 37 admits, none of which
is a governance write.

What the console should do instead, and it is more useful:

- **Render the sequence as a readiness read** over the record: for each of the 14 steps, does the record
  hold it, and does reading it back confirm what it should carry? Step 1 reads back every type present
  with its declared fields; step 4 reads back exactly one `ownership_grant` edge per type; step 9 reads
  back that every role the intake declaration names resolves. This is a genuine screen for a fork —
  "what is missing before this swarm can route its first task" — and it is a pure read.
- **Show the five hard ordering edges** the documents impose (types before instances; the principal before
  any edge to it; the roster before the first declaration; the read-declaration's entities before the first
  task; the policy before any governance write meant to be permitted rather than held), so a partial
  bootstrap reads as *where it stopped*, not as a scatter of absences.
- **Never offer to write a missing step.** Where a step is absent, the console names it and stops. That is
  the same posture the design takes everywhere: the console surfaces, the operator acts.

This readiness read is a tenth screen in all but name; it is folded into S9 rather than given a nav slot,
because a fork needs it once and an established instance never looks at it.

### Which rows have no UI counterpart — honestly

Roughly **two thirds of the matrix will map to no screen**, and pretending otherwise would be the
decorative-control failure invariant 1 names. The classes, with why:

| Class of row | Why no surface | Examples |
|---|---|---|
| **Write-refusal rows** — the observable is that a write was *refused at submission* | The violation never lands in the record, so there is nothing for a reader of the record to see. This is the largest class: a blocking finding under a non-blocking verdict, a verdict carrying a condition, a blocking finding with no evidence, a terminal value outside the declared set, an `intake_rule` naming a work-model type. | GW rows on sign-off refusal; WM-11/16/20 |
| **Document-shape and lint rows** — the observable is a checker over the corpus | Nothing to do with the record at all: a commit hash outside `status.md`, an unresolved anchor, a rule-bearing heading with no row, a banned vocabulary word. | CF-*, VO-1…VO-4, PR-8 |
| **Code-shape rows** — the observable is the absence of a shape in code | "Where the design forbids local state, the suite's observable is that the state does not exist." A screen cannot render an absence in a codebase. | PR-9 (parallel mechanisms), the registry-closure census |
| **Instrument rows** — planted positives on the suite's own counters | The suite validating itself. A reader of the record is not the audience. | the three-instrument rows |
| **Proxy and fake-system rows** — the observable is in `RP`, `X(sys)`, or `CH` logs | The evidence is a request log or a fake channel's message list, which are the suite's instruments and not the record. Most adapter identity and redelivery rows are here. | AD-3, GH-1, GM-6, TG-2, PY-7 |
| **R, U, P, and D rows** | Review-only, untestable as written, pending a ruling, or definitional. A **P** row is *waiting on a decision*, so a screen for it would render a design that does not exist yet. | AU-6, PM-12, WM-38, WF-24 |

**What is left, and it is the valuable part.** The rows with a real surface are those whose observable is a
**state the record holds and a reader could misread**: a checkpoint's subject and queue membership (S1); a
lease's derived state and a task's claimability (S2); step state, verdict/finding agreement, and the
pinned-head drift (S3); the chain and "landed" (S4); the ascent, unplanned tasks, and derived completion
(S5); finding severity, kind, and scope (S6); coverage, dispositions, `unknown`, and silence (S7). On a
rough pass that is on the order of **60–90 rules with a genuine surface out of 372** — a quarter, not a
half. The right posture is to state that number honestly and use the surface column to compute it rather
than assert it.

**The one row that is about the console itself is GW-53**, and it is worth quoting because it is the
console's own acceptance test:

> a dashboard read under any credential but the operator's; a stored picture of the queue beside the record

Both of those are failing artefacts for this specification. The first says the console must carry the
reading principal's credential and never a service identity. The second says no cached queue may be read
as the answer. **GW-53 is the row the console is built to pass**, and it is the only one in the matrix that
names the console at all — which is itself a finding (gap **[G9]**).

**Two more rows are about presentation without naming the console.** GW-49 is the only row in the matrix
whose *action* cell is a presentation act — "present and resolve" a checkpoint on an action and one on a
task — and its failing artefact is "a second presentation path or resolution protocol". The console is a
presentation path; building a second queue beside it is that row going red. And GW-3a, quoted under S3, is
the design's rule for any reader that projects.

### The argument this pairing actually makes

Worth stating plainly, because it is the strongest reason to do the work at all. The suite classifies each
rule, and the count of **R**, **U**, and **D** rows is, in the document's own words, "a measure of how much
of the foundation is not yet a control". An **R** row is one whose only observable is a person reading
prose — and invariant 1's placement test says a rule binds *where it is read at the moment of the action it
governs*, which is why the suite concedes it "cannot make a person read".

A console is a surface read at the moment of a decision. That is precisely the placement an **R** row
lacks. So the console is not merely a viewer for rows the suite already tests mechanically; for a specific
minority of rows it is the mechanism that could convert a review-only rule into a control — by putting the
thing to be judged in front of the principal at the moment they judge it. The clearest instance is the one
the design already commits to: priority ordering binds "presentation and reporting, not selection", so that
"declining the top of the list is a visible, attributable act rather than an invisible skip". That rule has
no enforcement anywhere except a surface that lists claimable work in order. **S2 is that surface**, and
building it is the only way that rule stops being decoration.

This is also the honest bound on the claim. It applies to a handful of rows, not to the matrix. Most of the
suite tests writes and refusals no screen can witness, and the table above says which.

---

## 5. What not to build

A specification that specifies everything specifies nothing. Out of scope, each with the reason:

**Writes the design does not already attribute to the operator.** The console's write surface is exactly
four: a checkpoint's resolution, a finding on a batch, a `waived` sign-off, and a task. Not: claiming a
task or a step (pull is a principal's 1:1 judgement made by the principal that will act, and a human
clicking "claim" on behalf of an agent is the routing fallthrough the design refuses); attaching or
detaching a task from a batch (a step owner's judgement, recorded in that step's sign-off); writing or
editing a `workflow` declaration; any governance write.

**A task lifecycle view, in any form.** There is no task lifecycle; there are batches. The existing
11-stage view is not a screen to improve — it is a screen to delete. Its content moves to S3 as step state
on a batch.

**Reopening anything.** A closed batch is never reopened and a terminal task never returns to open. No
affordance, not even behind a confirmation. The redo path is a finding plus a new task through intake.

**Any live call to an external system.** No fetching a PR's current state from the code host, no reading a
mailbox. Artifact state comes from the adapter's observations. The console is not an adapter and must not
become one by accident.

**A per-delivery drop log.** Drops are aggregate per window by design. Building the drill-down would create
pressure for a record the design refuses to keep.

**A notification stream, an inbox, or a badge count of completions.** That is the channel's job, and the
channel carries a declared subset. Completed work is deliberately not carried.

**A second decision queue.** Not a "needs attention" list beside the checkpoint queue, not a separate
approvals inbox, not an alerts panel. One decision queue, one resolution protocol — the design says "do
not build a second gate, a second queue, or a second notification path" by name.

**Editing foundation documents, or the register, through the console.** The register is a document. The
console renders it.

**Offline mode, or any read served from a local store when the record is unreachable.** The correct
behaviour when the record cannot be read is to say `unknown` — loudly, in the specific place — and never to
serve a cached picture that looks current. This is invariant 7 and it is also the failure posture: the
swarm halts without the record, and a console that appears healthy during a halt is lying.

**Multi-instance or multi-tenant views.** One record, one operator, for this specification.

**Charts of throughput, velocity, or agent leaderboards.** Nothing in the design defines them, they invite
stored aggregates, and a metric nobody acts on is reporting. If one is wanted later, it needs a design
first.

---

## 6. Documentation gaps

Every place the specification could not be written because the foundation does not answer what a reader
needs to see. **These are not fixed here and no decisions are registered for them.** Each states what was
being specified, what the docs do not answer, and which document would own the answer.

---

**G1 · There is no retrieval contract for a principal reading a console.**

*Specifying:* what S1 and S2 must retrieve before rendering, and what an absence means.

*Not answered:* `data_model.md#retrieval-contract` gives five actor kinds — an agent claiming a step, an
adapter, the operator-facing agent, a self-triggering daemon, a reviewing step owner. Each states what it
retrieves, what it must not read, and what absence means. **A principal reading the record for review is
not among them**, even though decision 37 makes that read a first-class activity of the design. The
operator-facing agent's row is the closest and is explicitly about *carrying* checkpoints to a channel, not
about a general read. So the console has no declared read set and no declared meaning for absence, and two
implementations could disagree about whether a failed edge read renders as empty or as unknown.

*Would own it:* `data_model.md`, as a sixth row of the retrieval contract.

---

**G2 · A checkpoint's deferral and timeout are defined but their presentation is not.**

*Specifying:* the affordances on a queue row in S1.

*Not answered:* the design says a deferral is **bounded** and a timeout is **a terminal state that never
continues**, and that `deferral_until` is a field. It does not say whether the operator sets the bound or
the policy does, what the ceiling is, what a reader should be shown as it approaches, or whether a deferred
checkpoint stays in the queue. "A timeout is terminal and never continues" is a strong rule with no stated
reader-facing consequence: a checkpoint that timed out is a decision that never got made, and nothing says
whether it is shown, escalated, or silently terminal.

*Would own it:* `gates_and_workflows.md#the-checkpoint`, or `failure_posture.md` for the bound.

---

**G3 · Quorum and disjoint roles are computed but not presented.**

*Specifying:* what S1 shows when a checkpoint needs more than one approver.

*Not answered:* `action_policy` carries `quorum` and `disjoint_roles[]` per class, and the structural
checks are "reads over the checkpoint's principal edges" — count and disjointness over `AWAITS`,
`RESOLVED_BY`, and `RAISED_BY`. The design says the checks happen and where they live. It does not say what
the resolving principal is entitled to see: whether they know a quorum is required, how many have resolved,
whether they can see who, or what a checkpoint looks like between the first and last resolution. A reader
resolving one of three cannot tell from the design what state their screen should show.

*Would own it:* `authority_model.md#the-thresholds-home-is-the-action_policy-per-class`.

---

**G4 · Priority ordering names its inputs but not their precedence.**

*Specifying:* the order of the claimable pool in S2 — and the design makes this load-bearing, since "a
principal's tool surface lists claimable work in priority order" is how declining becomes visible.

*Not answered:* the derived read weighs "the standing of the task's ascent, a `due_date` at hand, an
urgency the workflow's own declaration carries, and the blast radius of what the task would produce — read
together". *Read together* is not an ordering. Nothing says how a near `due_date` trades against a
high-standing ascent, or whether blast radius raises or lowers standing. Two conforming implementations
can present different top-of-list, and the observable that catches a violation ("was there a claimable
task of strictly higher standing") is not computable without the precedence.

*Would own it:* `work_model.md#priority-orders-the-claimable-pool-it-does-not-enter-it`. Related but
distinct from the deferred cross-disciplinary rubric, which `principles.md` puts out of scope until P4 —
this is narrower and the design already commits to the four inputs.

---

**G5 · A reader cannot tell whether an instrument's planted positive has run.**

*Specifying:* how S7 renders a zero drop count.

*Not answered:* invariant 3 says a zero is a claim about the tooling before it is a claim about the world,
and names the swarm's instruments — drops per window, lapses per task, blocked claims per window, coverage
on every adapter observation — each requiring a planted positive "before a zero read from it is believed".
The suite validates its own instruments. But **nothing says the record carries whether a given instrument
has been proved non-zero**, so a reader of a live console sees `0 drops` and has no way to distinguish a
quiet window from a disconnected counter. That is precisely the failure the invariant exists to prevent,
one layer out from where it is enforced.

*Would own it:* `principles.md` invariant 3, or `adapters.md#what-the-adapter-does-with-every-event` for
the drop counter specifically.

---

**G6 · Open design decisions live in a document, with no record counterpart.**

*Specifying:* S8, the register.

*Not answered:* the register is a markdown table in `conformance.md`, and the document argues explicitly
that it belongs there rather than in `status.md`. Fine for a reviewer reading the repository. But a console
reads the record, and there is no `decision` entity for a foundation-level open question — the `decision`
type is a *planning* decision, `PART_OF` a planning record. So the console can render the register only by
parsing a file in the repository, which makes it the one screen whose source is not the record, and
therefore the one screen that goes stale against a checkout. The design does not say whether that is
intended.

*Would own it:* `conformance.md#the-register-of-open-design-decisions`, or `data_model.md` if the register
should have a record shape.

---

**G7 · "Open question" is a live UI concept with no design counterpart.**

*Specifying:* whether S8 keeps the existing open-questions sidebar.

*Not answered:* the running application treats `task` entities carrying `category: "open_question"` as
questions, with a dedicated persistent sidebar and a detail route. The design has no such concept. The
nearest three are: a `decision` entity (a planning decision, taken not open); the foundation register (a
document); and a `checkpoint` (a question put to a principal, which is the decision queue). If the
operator's practice of tracking open questions as tasks is a real need, the design does not name it — and
if it is not, roughly 800 lines of the running app are surfacing a concept that should not exist.

*Would own it:* `work_model.md#where-tasks-come-from-every-source-indexed`, or `vocabulary.md` if it is a
term.

---

**G8 · A conformance row cannot name the surface its violation would be visible on.**

*Specifying:* coupling 2 of section 4 — the mechanism the operator asked for.

*Not answered:* a row has six cells and none of them points at a reader-facing surface. Rows point at rules
by anchor, and the projection renderer already derives per-document files from them, so the machinery for
a derived pointer exists. Whether the matrix should carry such a column is a question about the suite's own
shape, and the suite's document does not consider a UI as a consumer of the matrix at all. Without it, the
pairing this specification proposes is maintained by hand in this file and drifts.

*Would own it:* `conformance_suite.md#how-the-suite-judges-and-what-a-row-is`.

---

**G9 · The console is described in one ruling and tested by one row.**

*Specifying:* everything above.

*Not answered:* decision 37 is the design's whole treatment of this surface — one paragraph inside a
section about channels, whose subject is really the channel and which mentions the dashboard to say what it
is *not*. Outside `status.md`, the word "dashboard" appears four times in the entire foundation corpus:
twice in that ruling, once in a `vocabulary.md` aside, and once in the conformance row. There is one
conformance row (GW-53), whose five failing artefacts are three about channels and two about the console.
There is no foundation document for the reader's surface, no vocabulary entry for it, and consequently no
term for a screen, a view, or the act of reading the record — which means the console's own vocabulary is
unconstrained by invariant 12 precisely where invariant 12 would be most useful. This specification adopted
the design's nouns by hand; nothing makes that binding.

*Would own it:* a foundation document that does not exist, or an expansion of decision 37's section.

---

**G10 · `apps/` appears in no row of the path-keyed reading list.**

*Specifying:* what a reviewer of a console change is obliged to read.

*Not answered:* `conformance.md#read-when-these-paths-changed` keys documents to changed paths across
`lib/`, `execution/`, `docs/foundation/`, and `.claude/skills/`. **No row matches `apps/`.** So a change to
the application that renders the record triggers no foundation reading at all, while a change to a daemon
that writes it triggers several. Given that the console's correctness is almost entirely a question of
whether it uses the design's vocabulary and derived reads, this is the reading list's most consequential
absence — and it is why the existing app drifted into the retired vocabulary without anything catching it.

*Would own it:* `conformance.md#read-when-these-paths-changed`.

---

**G11 · There is no stated rule for how a reader is shown a derived read versus a stored field.**

*Specifying:* nearly every column of S2, S3, and S5.

*Not answered:* the design is emphatic that state needing a watchdog belongs in a relationship, not a field
(invariant 11), and the concept table's *Derived reads* column is long. What it does not say is whether a
reader must be able to tell the two apart. This matters in one specific place the design does name: the
stored `priority` field and the derived claim-time ordering "can disagree, and where they do the derived
read is what a claim predicate consults". A console showing one number gives a reader no way to know which
they are looking at, and the design offers no rule.

*Would own it:* `data_model.md#record-conventions`.

---

**G12 · The four writes decision 37 admits have no stated confirmation posture.**

*Specifying:* what happens after the operator resolves a checkpoint in S1.

*Not answered:* invariant 2 requires a read-back after any write that carries a decision, and the
operator-facing agent's write contract states it must read back the resolution "before it reports to the
operator that the decision landed". The console writes as the operator directly, not through that agent, so
that row does not govern it — and no row does. Whether the console must read back before showing the
decision as landed, and what it shows while the read-back is outstanding, is unstated for exactly the four
writes the design permits it.

*Would own it:* `data_model.md#write-contract`, as the counterpart to G1's retrieval row.

---

## 7. Sequencing

Not a schedule; the order in which the screens stop being wrong.

1. **S1 (the queue).** The design's central object, absent from the console today, already reachable in the
   record. It is also the screen GW-53 tests.
2. **S2 and S3 (work and a batch).** Together, because a task's position is its batch. This is where the
   retired vocabulary is removed and the 11-stage lifecycle view is deleted.
3. **Coupling 1 (the fixture gallery).** As soon as two screens exist, so that they are reviewed against
   `T1` and `T-at(W, s)` rather than against production.
4. **S6, S5, S4 (findings, hierarchy, chain).** Read-only elaborations of what S2 and S3 establish.
5. **S7 and S8 (adapters, register).** S7 depends on adapters writing window observations; S8 is blocked on
   G6 for its first half.
6. **S9 (the record).** Largely already built; retained and reframed.

**The whole revamp is gated on one thing that is not a UI question:** whether the record holds the design's
types under the design's names. The migration's mapping is what turns `workflow_definition` into
`workflow` and `checkpoint_brief` into `action` plus `checkpoint`. Until that lands, the console can
render the design's screens only over `LEG`-shaped data, and every screen above would need a tolerant
reader for the retired names. That is a real dependency and it belongs at the top of any plan built from
this document.

## 8. Acceptance checklist (ux)

An implementation PR for this console is ux-complete only when:

- [ ] Every screen declares empty / unknown / error (failed read) as three distinct states; empty is never inferred from failure
- [ ] Every noun on a screen is a design vocabulary term or unmarked plain English (invariant 12); S2 forbidden-word list enforced
- [ ] S1 options come only from the checkpoint; resolver + `resolution_note` on write; self-resolution marked
- [ ] S2 forbids retired labels (blocked / executing / undispatched / tiers / …); claimable pool ordered by derived priority
- [ ] Failed writes and failed reads show actionable recovery (retry / re-auth / open record) — no silent no-op
- [ ] No second queue, notification stream, or reopen affordance (per §5)
- [ ] Unplanned ascent (S5) is labeled as unplanned, not as an error
- [ ] S7 never presents absence of window observations as health; artifact `unknown` is rendered, not blank

