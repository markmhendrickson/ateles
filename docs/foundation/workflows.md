# Workflows: the core workflows, each from its own purpose

**Authored companion (not on the review reading list; whether it is keyed is the operator's budget
decision, recorded in `status.md`):** binds via the `workflow` entity for each (project, type) and
`execution/scripts/render_workflow_docs.py --check`; reviewers load the kernel and gates instead
(`conformance.md`). **Kind:** foundation; states the design of each core workflow, why its steps exist,
and which successors its tasks may enter, and never the state of a checkout. **Derived from:** `work_model.md`, `gates_and_workflows.md`, the `workflow` declarations on the
record for the built workflows (their step lists and fast paths, not their agent names), the agent
policies governing outreach, payment, and people-data, `CLAUDE.md`'s people-data section, and PR #745
operator review (2026-09-04), and the operator's 2026-09-05 terminology review (revision 17: the one boundary and the term `external system`, the `action series` rename, `subject` defined, and the two-part `checkpoint`), and the operator's 2026-09-05 review of review relevance (revision 19: the `applies_when` condition on an optional step, and two terms retired in favour of `review step`). Which workflows have a declaration on the record, and which are envisioned
only, is `status.md`. Revised by the simplification pass of 2026-09-05 (revision 29: `operator_preview` renamed `consent`; open decision 33). Revised by the memo-gap pass of 2026-09-06 (revision 31: decision 39 ruled here — what intake's `link` attaches and what hydration resolves; the payment `consent` row aligned with decision 27). Revised by the workflow-format pass of 2026-09-06 (revision 34: the two declared intervals and the planned wait, cited in *How to read a workflow section*, `outreach`, and `operator-only`; a standing constraint on an entity a task names, read at intake). Revised by the consistency pass of 2026-09-06 (revision 35: the three `consent`-carrying workflows cite when their checkpoint is written and what the take re-evaluates). Revised by the second workflow-format pass of 2026-09-06 (revision 36: a bound declared as the task's `due_date` and the `operator_only` step, cited in *How to read a workflow section* and `operator-only`; a matter, a case, or a filing as a record entity the task names, under *What `link` attaches*). Revised by the testability pass of 2026-09-06 (revision 37: `DUPLICATE_OF` at `dedupe`; `impl` closes on a mergeable pull request; `none_permitted` on `feature` and `security`; a bug needing a design choice becomes a new task; the transcript is a source, not an artifact; the `contact` allowlist at `extract`).

## Purpose

Give the abstract model of `work_model.md` and `gates_and_workflows.md` concrete reference points, and
ensure each core workflow is envisioned from its own purpose rather than from what happens to be built.
A reviewer reading a change to a step list, a fast path, or a successor checks it against the section
here for that workflow; a workflow with no section here has no stated purpose, and a step whose reason
this document cannot give is a step to question.

## Scope

Twelve core workflows: intake, feature, bug, security, copy, social content, release, outreach, payment,
research and analysis, meeting processing, and operator-only. Each is a workflow type; a `workflow` entity
is declared per (project, workflow type), so one type may have several declarations that share the design
stated here and differ in step owners and thresholds. A step owner is declared as a **role** and resolved
to a principal at claim time: the declaration's `owner_role` holds the role, the roster binds that role to
an agent per project (`swarm_roster`, by role), and the binding is read when the step is claimed, so a
renamed or replaced agent leaves no stale name here or in the declaration (`vocabulary.md#step-owner`).
The step-owner column of every table below is therefore a role, and a role that resolves to no principal
raises a checkpoint (reason `unspawnable_assignee`) rather than falling through to any available agent. No section names an operator, a payee, a contact, or a channel; those are context entities the step
owner retrieves at runtime.

## How to read a workflow section

Every section has the same shape, so the sections can be compared and so the step tables can be rendered
from the declarations (below).

- **Purpose**: one sentence, the reason the workflow exists.
- **Entry condition**: what makes a task eligible to enter this workflow. For every workflow but intake,
  the entry condition includes that an intake batch closed naming this workflow as successor
  (`work_model.md#intake-is-every-tasks-first-workflow`).
- **Steps**: the ordered list, each with its step owner by role, whether it is required, and its parallel group
  and join where it has one. A workflow's last step is always a single step, never a parallel group; its
  sign-off is the batch's closing sign-off and names the successor
  (`gates_and_workflows.md#sequencing-is-data-successors-and-the-chain`). A step's state within a batch
  is open, claimed, or signed, derived from edges (`gates_and_workflows.md`). A sign-off carries a
  verdict; a failing verdict does not advance the batch, and the workflow declares per step which earlier
  step opens again (`on_fail`), the failing sign-off staying in the record as history; a declared cap on
  such rounds, when reached, escalates the batch's tasks (`failure_posture.md`, reason `rounds_exhausted`).
  Two intervals are declared on every step and shown in no table: `unclaimed_after`, after which an
  unclaimed step raises `unclaimed_step`, and `hold_bound`, the most a claimed step may hold on a condition
  before its declared alternative close or `rounds_exhausted`
  (`gates_and_workflows.md#declaration-batch-projection`). Where a section's prose names a declared interval
  — a follow-up interval, the bound on an `await` — it is that step's `hold_bound`; a step whose close
  condition names an arrival from outside the swarm is a planned wait, held under decision 13 and ended by
  that bound. Either interval may be declared as the task's `due_date` where the bound is a date fixed
  outside the swarm and known at formation; reaching it is reaching the bound, and it closes nothing. A step
  whose action is `operator_only` carries the gate's checkpoint as a `consent` step does, holds for the
  confirmation, and closes on it, never on the resolution
  (`gates_and_workflows.md#an-operator_only-action-is-taken-by-the-operator-and-the-step-that-carries-it-closes-on-the-confirmation-never-on-the-resolution`).
- **Stages**: named groups of contiguous steps, for reading and for reporting where a batch is.
- **Artifacts**: the records in external systems the batch produces or references, attached by edge and
  never the subject of a step (`work_model.md#artifacts-are-records-a-batch-leaves-never-its-subject`).
  Where a workflow's main product is an entity in the record rather than an external record, the section
  says so; an entity in the record is not an artifact.
- **Typical action classes**: the `action_type` values the batch's actions usually carry. Every one is
  evaluated at the action gate at the moment it would be taken, whatever the workflow
  (`gates_and_workflows.md#actions-are-entities-only-actions-are-taken`); the list is what to expect, not
  a bound.
- **Successors**: the workflows the closing sign-off may name, and whether it may name none
  (`none_permitted` — `gates_and_workflows.md#sequencing-is-data-successors-and-the-chain`). A batch's
  tasks enter exactly one successor or, where the declaration permits it, none.
- **Fast paths**: the declared skips and the condition that permits each. A fast path's condition is a
  property of the task set fixed at intake — never a label on an artifact, and never a reading of what the
  change touches, because a fast path is declared before the change exists.
- **Applicability**: which steps are optional (`required: no`) and the declared `applies_when` condition
  that decides whether each opens. Unlike a fast path, an applicability condition **may** read what the
  batch's change touches, because the question it answers — which perspectives this particular change
  warrants — is one intake cannot answer. It is still never a label on an artifact: the condition lives on
  the workflow declaration, and nothing the reviewed change says about itself seats or unseats a reviewer
  (`gates_and_workflows.md#declaration-batch-projection`). A step the condition rules out is recorded
  **inapplicable** on the batch, which never reads as signed.

## Rendering

The design of each workflow lives here; the operative declaration is the `workflow` entity. The two are
kept from diverging the way the plan-mirrored documents are: the step table in each section is a render
target, produced from the entity by `execution/scripts/render_workflow_docs.py`, whose `--check` mode
exits non-zero when a table on disk differs from the record (the pattern of `render_plan_docs.py`,
`conformance.md`). Each table sits between markers naming the (project, workflow type) it renders. The
prose around a table (purpose, entry condition, stages, artifacts, successors, the reason for each step)
is authored here and reviewed in PRs; a change to a step list is made to the entity and rendered, and a
PR that edits a table by hand fails the check. A section whose workflow has no declaration yet carries a
hand-authored table with the same marker, which becomes the check's expected content the day the entity
is declared.

## Roles named in this document

| Role | Claims |
|---|---|
| `pm` step owner | the `pm` step of every code workflow and every step of intake |
| `ux`, `arch`, `pr_review`, `qa`, `legal` step owners | the review step of the same name (`vocabulary.md#review-step`) |
| implementer | the `impl` step |
| steward | the `merge` step, whose work is the merge action (`vocabulary.md#steward`) |
| release steward | every step of the release workflow |
| copywriter | the `copy` step |
| content author | the drafting and posting steps of social content and outreach |
| lint runner | a deterministic step whose work is a script and whose sign-off is the script's result |
| operator-facing agent | every step that carries a checkpoint or a task to the operator (`vocabulary.md#operator-facing-agent`) |
| payer, verifier | the two disjoint roles of the payment workflow |
| researcher, analyst | the working steps of research and of meeting processing |

## intake

**Purpose:** turn a created task into a routed one: classified, linked to the records it concerns,
deduplicated, prioritized, and handed to exactly one successor workflow, to none, or to the operator.

**Entry condition:** a task exists in the record and has no intake batch. Creation is publication
(`work_model.md#the-transition-vocabulary`), so every task meets this condition once, at creation, and no
task meets it twice. A task with no intake batch is by that fact unrouted; there is no separate unrouted
state (`work_model.md#intake-is-every-tasks-first-workflow`).

**Steps**

<!-- rendered: workflow=*|intake steps -->

| # | Step | Step owner (role) | Required | Parallel / join | Closes on |
|---|---|---|---|---|---|
| 1 | `classify` | `pm` step owner | yes | | the task's `action_type` declares the classes of action it expects to produce, from what the task does; `assigned_to` is written only where a named principal is the point; `PART_OF` to a parent, or children split out, where the work is an aggregate |
| 2 | `link` | `pm` step owner | yes | | every existing record the task **names** is attached by edge: an external record (an issue, a pull request, a thread, a page) as an artifact, and a record already in the record (a transcription, an obligation, a profile, a plan, a contact) by `REFERS_TO` from the task; nothing is attached on relevance alone (`#what-link-attaches-and-what-it-leaves-to-hydration`); finding none is a valid close |
| 3 | `dedupe` | `pm` step owner | yes | | the task is compared against tasks that are not terminal; a duplicate closes terminal with a `DUPLICATE_OF` edge to the task it duplicates (`data_model.md#relationships`) and this batch closes with no successor |
| 4 | `prioritize` | `pm` step owner | yes | | the task's priority is set from the `priority_rubric` entity, retrieved by type, never from the classifier's own sense of urgency |
| 5 | `route` | `pm` step owner | yes | | the closing sign-off names one successor workflow, or none, or `operator-only` |

<!-- /rendered -->

The steps exist because each failure they prevent has a name. Without `classify`, blast radius is inferred
from the handling agent at the moment the action is taken (`gates_and_workflows.md#confidence-and-three-blast-tiers`).
Without `link`, the workflow that follows opens a second issue for work that has one. Without `dedupe`,
two batches carry the same change to two pull requests. Without `prioritize`, the claim order is the creation
order. Without `route`, a task reaches a workflow by whichever engine noticed it first.

**A standing constraint recorded on an entity the task names is read at intake, and it is a fact on that
entity, not a rule beside the record.** A person's recorded objection to further processing is the case: it
outlives any batch and binds every future one about that person, and nothing per batch, per principal, or
per class carries it — a `task_policy` is the operator's preference, a grant is one principal's capability,
and an `action_policy` is per class of effect. What carries it is the entity itself: the objection is an
observation on the `contact`, with provenance naming the message or the meeting that stated it and the time
it was stated (`data_model.md#record-conventions`), and `link` attaches that contact because the task names
it (`#what-link-attaches-and-what-it-leaves-to-hydration`). `route` then closes the batch with no successor,
its sign-off carrying a finding that names the constraint, so that the refusal is a recorded verdict and not
a silent skip; a step of a later workflow whose `reads_to_enter` names the type reads the same observation
and holds the same way. The shape is principle 11's: a constraint written once on the thing it constrains
needs no process to keep it true, where a prohibition list kept beside the record is the second source that
goes stale in the direction that matters. Its field is the schema's, as every context entity's shape is; the
design states only where the constraint lives and who reads it, and what the operator is owed on it — that
the objection was received and honoured — is the observation and the closing finding, read on the record
like any closed work
(`gates_and_workflows.md#work-is-reviewed-on-the-record-and-a-channel-carries-only-what-awaits-the-operator-or-cannot-wait`).

**Stages:** triage (`classify`, `link`, `dedupe`); disposition (`prioritize`, `route`).

**Artifacts:** none produced. Existing artifacts are attached.

**Typical action classes:** none. Every write intake makes is an internal operational write to the record,
which is not an action (`gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken`).

**Successors:** any workflow in this document except intake; or none; or operator-only, which is a
workflow like the others and is named the same way.

**Fast paths:** `inherits`, for a child task created by a batch that is already routed: `link` and
`dedupe` are skipped and `classify` may copy the parent's declaration, since the parent's intake did that
work; `prioritize` and `route` are never skipped, because a child may need a different workflow from its
siblings (`work_model.md#parent-and-child-tasks`).

### What `link` attaches, and what it leaves to hydration

**Ruled (decision 39, 2026-09-06): intake attaches what the task names, records in the record and external
records alike, and attaches nothing on relevance alone; what a step needs beyond that is resolved by
hydration, per step, from those anchors.** Registered in
`conformance.md#the-register-of-open-design-decisions`. The question was whether a task should enter the
record already related to everything in it that bears on the task — the entities in scope of a finance
task, the people and threads around a meeting — so that context travels with the task instead of being
found again at each step; or whether each step should retrieve its own context as it opens; or a hybrid of
the two. The operator proposed the first and asked that the inclination be interrogated rather than taken.
This section is that interrogation, and the answer is the hybrid, with a precise line between its halves.

**What `link` attaches.** Every record the task **names** — by identifier, by reference in its
description, or by the external record an adapter created it for — is attached by edge at intake: an
external record as an artifact, `REFERS_TO` task → artifact; a record the record already holds as
`REFERS_TO` task → entity (`data_model.md#relationships`). Names are what make the attachment bounded:
`link` resolves each to the one entity that exists for it, by the bounded retrieval the record conventions
require before any edge is written (`data_model.md#record-conventions`), and where a name resolves to
nothing, nothing is created and nothing is guessed. A task that names nothing leaves `link` with no edges,
which is a valid close.

**What `link` does not attach: anything the task does not name.** A general pull — "everything
contextually related" — fails three tests the design already applies. It has no stopping rule: relatedness
is a judgement with no boundary, and an edge written on a judgement of relevance is read later, by every
step and every reader, as a fact that the task concerns that entity, with the judgement gone. It goes
stale: a link is a claim about relevance at intake time, and the step that reads it may run after the
context has changed — which is the reason the design resolves a step's reads at the step and never before
it (`gates_and_workflows.md#declaration-batch-projection`, hydration). And it is purpose-blind: attaching a
task to every person entity that might bear on it is the profile-building the people-data rule forbids,
where attaching the contact the task names is the relationship the rule exists for. The intake-wide pull is
the pattern principle 11 warns of, applied to edges — a set of relations some process would have to keep
true as relevance moves, or that quietly stays wrong.

**What hydration resolves, and from where.** A step declares the entity types it must read
(`reads_to_enter`, `reads_to_close`), the agent's definition bounds the types it may read at all
(`data_model.md#what-each-actor-reads-and-writes`), and the hydration phase resolves the instances before
the step opens. The instances are found from the task's anchors — its `REFERS_TO` edges and the edges those
entities carry — by the same bounded retrieval, so that a payment step reads the profile the task names
and not every profile, and a meeting step reads the contacts the transcript names and not the address
book. This is the per-step arm of the hybrid, and it is where "which context" is decided, because the step
is the one component that knows what it is about to judge. Context a step discovers the task concerns is
written back onto the task as the same `REFERS_TO` edge `link` writes
(`gates_and_workflows.md#what-a-step-leaves-at-close-what-it-produced-and-a-reference-to-what-it-read`),
so the anchors grow as the batch proceeds, and a later step, or a later batch on the same task, starts from
more than intake had.

**Reason.** The two arms answer different questions. `link` answers "what is this task about", which is
knowable at intake and is a property of the task; hydration answers "what does this step need", which is
knowable only at the step and is a property of the declaration. Giving intake the second question makes it
guess; giving the step the first makes every step re-derive what the task already said. The anchors are
the interface between them, and typed edges are what "well organized" means here: a reader walks from the
task to what it concerns and from there to what a step read, and no free-text field holds any of it.

**The cost accepted** is that a step whose declared type is not reachable from the task's anchors — a
research step whose sources the task did not name — finds them by search, which is the ordinary retrieval
an agent makes within its context types, and writes back what it found. **What would reopen it:** a class
of task in practice whose steps repeatedly retrieve the same unnamed context, batch after batch — which
would argue for naming it in the task at creation, or for a declaration that names it as a read, and not
for a general pull at intake.

**A matter, a case, or a filing is such an anchor, and it is a record entity, not a work-model one.** Work
that runs for months around one dispute, one claim, or one obligation to an agency — the correspondence, the
documents, the parties, the dates — needs a bounded set for its steps to read from and write to, and the
answer is the one this section already gives: the task names the matter, `link` attaches it by `REFERS_TO`,
and hydration resolves each step's reads from it and from the edges it carries — the documents and threads
that refer to it, the `contact` entities with their roles in it, the tasks that are its deadlines, each with
its `due_date` (`data_model.md#concepts`). What the matter *is* in the record is a context entity of the
operator's, retrieved by type and shaped by the registry
(`migration.md#context-entities-the-design-retrieves-and-never-migrates`): the standing of an obligation, an
episode, or a `payment_profile`, and like them named here by role and never defined. It is not a `plan`,
which is outside the four models and held (`migration.md#gaps-and-contradictions-the-mapping-exposed`, G10);
not a batch, which goes through one workflow for its whole life and holds on a task by edge (decisions 13
and 14, `work_model.md#a-batch-may-depend-on-a-task-it-created`); not a parent task, which groups tasks,
completes when they do, and holds no document or party; and not a new work-model type, because nothing about
it is work — it is what the work concerns. Its tasks over the months are ordinary tasks, each `REFERS_TO` the
matter, each with its own intake and chain, and a reader asking what has been done in a matter walks its
edges. An intake rule for correspondence in a matter keys on a field, so membership reaches a rule only as a
field a step wrote — a thread's classification naming the matter
(`work_model.md#an-intake-rule-turns-a-described-change-in-the-record-into-a-task-and-nothing-else`) — and
never as an edge the rule infers at evaluation.

## feature

**Purpose:** carry a change that adds or alters behaviour from scoped intent to merged code, reviewed by
every review step the change concerns.

**Entry condition:** intake closed naming `feature`; the task names a repository, which is the domain the
step owners' grants are checked against (`authority_model.md#grants`).

**Steps**

<!-- rendered: workflow=<project>|feature steps -->

| # | Step | Step owner (role) | Required | Parallel / join | Closes on |
|---|---|---|---|---|---|
| 1 | `pm` | `pm` step owner | yes | | the scope, the acceptance evidence (the task's `acceptance_criteria[]`), and the design basis are stated on the task (`conformance.md#design-basis`) |
| 2 | `ux` | `ux` step owner | yes | group `design`, joins `arch` | the change's user-facing behaviour is judged against the stated scope |
| 3 | `arch` | `arch` step owner | yes | group `design`, joins `ux` | the design basis is checked and the change conforms to the cited section, or the citation is found false |
| 4 | `impl` | implementer | yes | | a pull request exists as an artifact of the batch, its CI is green at the pinned head, and it is mergeable as read (`github.md#conditions-that-are-not-events`) |
| 5 | `pr_review` | `pr_review` step owner | yes | | the full diff is read and judged for correctness |
| 6 | `qa` | `qa` step owner | yes | group `verification`, joins `legal` | the tests can fail on the thing they watch (`principles.md`, invariant 4) |
| 7 | `legal` | `legal` step owner | no | group `verification`, joins `qa` | licensing, data-handling, and disclosure are judged where the change touches them |
| 8 | `merge` | steward | yes | | the merge action taken through the action gate and the merged pull request read back; the sign-off names the successor |

<!-- /rendered -->

The two parallel groups exist because their reviewing step owners judge independent things and neither
needs the other's verdict; the join is what makes the batch wait for both. `legal` is the one optional
step, and it carries an `applies_when` condition: it opens where the change touches licensing,
data-handling, or disclosure, and is recorded inapplicable where it does not, with the condition that
ruled it out (`gates_and_workflows.md#declaration-batch-projection`). Every other step here is required
and opens on every batch. `merge` is a step so that the merge
action has a step owner to claim it, a taking to record, and a sign-off to close the batch with; the
action itself is governed by action policy, not by the step
(`gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken`).

**Stages:** scoping (`pm`); design (`ux`, `arch`); implementation (`impl`); review (`pr_review`, `qa`,
`legal`); integration (`merge`).

**Artifacts:** the issue that carries the specification; the pull request; its CI check runs; the merge
commit.

**Typical action classes:** `build`, `docs`, `git_push`, `open_pr`, `merge_pr`.

**Successors:** `release`; or none, where the declaration permits it (`none_permitted`) because the project
deploys its default branch on its own cadence and the merge is the last effect the task needs — a
declaration for a project that does not is written with none not permitted, and a closing sign-off naming
none under it is refused (`gates_and_workflows.md#sequencing-is-data-successors-and-the-chain`).

**Fast paths:** `bug` skips `ux`; `copy` skips `arch`; `security` skips `ux`, `qa`, and `legal`. Each is
for a project that declares no dedicated workflow of that type; where the project does declare one,
intake routes to it and the fast path is never taken.

## bug

**Purpose:** carry a fix for behaviour that contradicts its specification to merged code with the
minimum review that still proves the fix.

**Entry condition:** intake closed naming `bug`; the task names a repository and cites the behaviour that
is wrong, ideally as a failing test.

**Steps**

<!-- rendered: workflow=<project>|bug steps -->

| # | Step | Step owner (role) | Required | Parallel / join | Closes on |
|---|---|---|---|---|---|
| 1 | `pm` | `pm` step owner | yes | | the defect is reproduced or the reproduction's absence is stated; the acceptance evidence is the test that goes red without the fix |
| 2 | `impl` | implementer | yes | | a pull request exists with the fix, mergeable as read, and the red-then-green result recorded in its body |
| 3 | `pr_review` | `pr_review` step owner | yes | | the full diff is read; the fix addresses the cause, not the symptom |
| 4 | `qa` | `qa` step owner | yes | | the test fails on the reverted fix (`principles.md`, invariant 4) |
| 5 | `merge` | steward | yes | | the merge action taken through the action gate and read back; the sign-off names the successor |

<!-- /rendered -->

There is no `ux` or `arch` step because a fix restores stated behaviour and does not choose new
behaviour; a bug whose fix requires a design choice is not carried by adding steps here: this batch closes
without a successor, its closing sign-off carrying the finding that names the choice, and the design work is
a **new** task through intake, referring to this batch's artifacts and produced from that finding
(`gates_and_workflows.md#closed-work-is-reviewed-on-the-record-and-redone-through-intake-never-reopened`)
— no task enters intake twice (`#intake`).

**Stages:** scoping (`pm`); implementation (`impl`); review (`pr_review`, `qa`); integration (`merge`).

**Artifacts:** the issue; the pull request; its CI check runs; the merge commit.

**Typical action classes:** `build`, `git_push`, `open_pr`, `merge_pr`.

**Successors:** `release`; or none, as for feature.

**Fast paths:** none. This workflow is itself the reduced path.

## security

**Purpose:** carry a fix for a vulnerability to merged and released code faster than the feature or bug
workflows allow, without widening what the public record says about the vulnerability before the fix
is deployed.

**Entry condition:** intake closed naming `security`; the task is marked as security-sensitive at
`classify`, which also restricts what its artifacts may say.

**Steps**

<!-- rendered: workflow=<project>|security steps -->

| # | Step | Step owner (role) | Required | Parallel / join | Closes on |
|---|---|---|---|---|---|
| 1 | `pm` | `pm` step owner | yes | | the affected surface and the fix's scope are stated in the record; the public artifacts carry no exploit detail |
| 2 | `impl` | implementer | yes | | a pull request exists with the fix, mergeable as read; its body describes the change, not the exploit |
| 3 | `pr_review` | `pr_review` step owner | yes | | the full diff is read and the fix is judged complete for the stated surface |
| 4 | `merge` | steward | yes | | the merge action taken through the action gate and read back; the sign-off names `release` |

<!-- /rendered -->

The review steps that are absent (`ux`, `qa`, `legal`) are absent for speed, and the workflow compensates
by never closing without a successor: a security fix that is merged and not released is not fixed, and
the closing sign-off's only permitted successor is `release`. Disclosure hygiene is a property of the
artifacts, checked at `pm` and `pr_review`, because the pull request and the release are public records.

**Stages:** scoping (`pm`); implementation (`impl`); review (`pr_review`); integration (`merge`).

**Artifacts:** the issue (private where the system allows); the pull request; the merge commit.

**Typical action classes:** `build`, `git_push`, `open_pr`, `merge_pr`.

**Successors:** `release`, always: the declaration does not permit none (`none_permitted`), so a closing
sign-off naming none is refused at the write (`gates_and_workflows.md#sequencing-is-data-successors-and-the-chain`).

**Fast paths:** none.

## copy

**Purpose:** carry a change to the words a product shows, in a site, a document, or a message template,
through a copy-specific review to merged content.

**Entry condition:** intake closed naming `copy`; the task names the surface whose words change and the
repository that holds it.

**Steps**

<!-- rendered: workflow=<project>|copy steps -->

| # | Step | Step owner (role) | Required | Parallel / join | Closes on |
|---|---|---|---|---|---|
| 1 | `pm` | `pm` step owner | yes | | the surface, the audience, and the intent of the change are stated |
| 2 | `copy` | copywriter | yes | | the words are written against the `brand_voice` entity, retrieved by type, and stored on the task |
| 3 | `ux` | `ux` step owner | no | | layout consequences of the new words are judged; nothing about the words themselves |
| 4 | `impl` | implementer | yes | | a pull request carries the words into the surface |
| 5 | `pr_review` | `pr_review` step owner | yes | | the diff changes only the words the task names |
| 6 | `legal` | `legal` step owner | no | | claims, comparisons, and regulated wording are judged where the copy makes them |
| 7 | `merge` | steward | yes | | the merge action taken through the action gate and read back; the sign-off names the successor |

<!-- /rendered -->

`copy` precedes `impl` because the words are the deliverable and the implementation is their carriage;
`ux` is optional and scoped to layout so that the copy step's verdict on the words is not re-litigated by
a second reviewing step, and its `applies_when` opens it where the change alters the surface the words sit
in rather than only the words. `legal` is optional on the same footing as on feature. There is no `arch`
step: copy changes no behaviour.

**Stages:** scoping (`pm`); authoring (`copy`, `ux`); implementation (`impl`); review (`pr_review`,
`legal`); integration (`merge`).

**Artifacts:** the pull request; the merge commit; the published surface once released.

**Typical action classes:** `docs`, `git_push`, `open_pr`, `merge_pr`.

**Successors:** `release`; or none, as for feature.

**Fast paths:** none.

## social content

**Purpose:** produce share material for a piece of content across the platforms it targets and post it,
with the operator's consent recorded through the action gate before anything is public.

**Entry condition:** intake closed naming `social_content`; the task names the content being shared and
the target platforms, from the `channel_config` entity retrieved by type.

**Steps**

<!-- rendered: workflow=<project>|social_content steps -->

| # | Step | Step owner (role) | Required | Parallel / join | Closes on |
|---|---|---|---|---|---|
| 1 | `draft` | content author | yes | | complete drafts exist for every targeted platform, stored in the record, against the `brand_voice` entity |
| 2 | `draft_lint` | lint runner | yes | on fail: `draft` | the deterministic checks pass: no relative-time anchors, a substance floor per platform, no near-duplicate text across platforms, every targeted platform present |
| 3 | `consent` | operator-facing agent | yes | on fail: `draft` | the checkpoint on the `publish` action, carrying the drafts inline, is resolved by the operator; feedback reopens `draft` |
| 4 | `post` | content author | yes | | the `publish` action taken through the action gate on each platform and the posts read back; the sign-off closes the batch |

<!-- /rendered -->

`draft_lint` sits before the operator sees anything so that the operator's attention is spent on
judgment, not on defects a script can find. `consent` is not a second gate: the `publish` action
is created when the drafts pass lint, the action gate evaluates it, and the step is where the
operator-facing agent carries the gate's checkpoint and records the decision
(`gates_and_workflows.md#the-action-gate-is-pr-independent`, principle 6). The checkpoint is written when
the step opens and the gate holds the action; `post` asks the gate again at the take, on the standing
resolution, the parameters, the policy as read then, and the key (`gates_and_workflows.md#the-checkpoint-is-written-where-the-gate-first-holds-the-action-and-the-permit-is-decided-at-the-take`).

**Stages:** authoring (`draft`, `draft_lint`); consent (`consent`); publication (`post`).

**Artifacts:** the posts on each platform, each attached by edge with its platform identifier.

**Typical action classes:** `publish`.

**Successors:** none.

**Fast paths:** `approved` skips `consent`, permitted only when the action gate would not
checkpoint the `publish` action: an action series for the class has graduated under the
`action_policy`, or the operator's standing approval for the content is already recorded on the task.
The fast path never bypasses the gate; it skips the step that would have carried a checkpoint the gate
would not have written.

## release

**Purpose:** cut and ship a release from merged code, verify it is the code that was reviewed, and
confirm it reached the deployed checkout.

**Entry condition:** intake closed naming `release`, or a code batch closed naming `release` as its
successor. Several tasks whose code batches closed since the last release normally enter the release
workflow together, as one batch (`work_model.md#what-goes-through-a-workflow-is-a-batch-of-tasks`).

**Steps**

<!-- rendered: workflow=<project>|release steps -->

| # | Step | Step owner (role) | Required | Parallel / join | Closes on |
|---|---|---|---|---|---|
| 1 | `criteria` | release steward | yes | | every criterion in the project's `release_criteria` entity, retrieved by type, is read and holds; a criterion that cannot be read is `unknown`, and unknown holds the batch (`principles.md`, invariant 7) |
| 2 | `release` | release steward | yes | on fail: `criteria` | the `release` action taken through the action gate; the tag, package, or deployment is read back at its terminal status (`principles.md`, invariant 2) |
| 3 | `verify_deployed` | release steward | yes | on fail: `release` | the deployed checkout reports the released version; the sign-off closes the batch |

<!-- /rendered -->

`verify_deployed` is a separate step because "released" and "landed" are different claims
(`principles.md`, invariant 10), and a batch that closed on the release action's success would record
the first as the second. `criteria` is separate from `release` so that the read of the criteria and the
taking of the release are two sign-offs, and a release taken against criteria nobody read is visible as a
batch missing one.

**Stages:** readiness (`criteria`); shipping (`release`, `verify_deployed`).

**Artifacts:** the tag; the package or image; the release notes; the deployment record.

**Typical action classes:** `release`, `git_push`, `external_api_write`.

**Successors:** none.

**Fast paths:** none. A release that skips its criteria is the failure the workflow exists to prevent.

## outreach

**Purpose:** compose and send a message to a party outside the swarm, with the message reviewed, the send
consented to through the action gate, and the follow-up owned.

**Entry condition:** intake closed naming `outreach`; the task names the recipient by reference to a
`contact` entity and the purpose of the message; the recipient's history with the operator is retrieved
from the record and the mail archive before anything is drafted, never assumed.

**Steps**

<!-- rendered: workflow=<project>|outreach steps -->

| # | Step | Step owner (role) | Required | Parallel / join | Closes on |
|---|---|---|---|---|---|
| 1 | `draft` | content author | yes | | a draft exists in the record, written against the `brand_voice` entity and the operator's voice guidance, every factual claim in it traced to a source the author read |
| 2 | `review` | `pr_review` step owner | yes | on fail: `draft` | the draft is judged for facts, voice, scope (it answers what was asked and nothing else), and for what it discloses |
| 3 | `consent` | operator-facing agent | yes | on fail: `draft` | the checkpoint on the `send_external_comms` action, carrying the full draft, is resolved by the operator |
| 4 | `send` | content author | yes | | the `send_external_comms` action taken through the action gate and the sent message read back from the mail system, never inferred from the send call's return |
| 5 | `follow_up` | content author | no | | a reply is linked as an artifact, or the declared follow-up interval passes and one follow-up was sent through the same gate, or the operator ends the follow-up; the sign-off closes the batch |

<!-- /rendered -->

`review` precedes `consent` so that the operator sees a draft that has already been checked, and the
draft the operator consents to is the draft that is sent: `send` takes the action on the reviewed content
by its dedup key, and any change after consent is a new `draft`. The checkpoint `consent` carries is written
when the step opens and the gate holds the `send_external_comms` action; at `send` the gate is evaluated
again, on the standing resolution, the parameters, the policy as read then, and the key
(`gates_and_workflows.md#the-checkpoint-is-written-where-the-gate-first-holds-the-action-and-the-permit-is-decided-at-the-take`). A staged draft is never modified in place in
the mail system, because on some systems an update is a send; the design's staging is the draft in the
record. `follow_up` is a step of the same batch so that an unanswered message has a step owner until
the batch closes, and so that a follow-up goes through the same gate as the first message. The follow-up
interval is `follow_up`'s `hold_bound`: the step holds on the reply under decision 13, and reaching the bound
is the cue to sign on the step's alternative close rather than a checkpoint
(`gates_and_workflows.md#declaration-batch-projection`).

**Stages:** composition (`draft`, `review`); consent (`consent`); delivery (`send`, `follow_up`).

**Artifacts:** the sent message; the thread; any reply.

**Typical action classes:** `send_external_comms`.

**Successors:** none. A reply that needs work of another kind is a new task, created at `follow_up` and
routed by its own intake.

**Fast paths:** none. Every outward message passes the gate.

## payment

**Purpose:** move money to a payee for an obligation the record holds, with the payee and amount verified
by a second principal against the payment profile before the operator consents and before the payment is
taken.

**Entry condition:** intake closed naming `payment`; the task references the obligation (an invoice, a
recurring fee, a wage) and the `payment_profile` entity, retrieved by type, that names the payee, the
rail, and the constraints. A task that names a payee or an amount inline, rather than by reference to a
profile, fails `classify` at intake.

**Steps**

<!-- rendered: workflow=<project>|payment steps -->

| # | Step | Step owner (role) | Required | Parallel / join | Closes on |
|---|---|---|---|---|---|
| 1 | `prepare` | payer | yes | | the payee, amount, currency, rail, and reference are assembled from the profile and the obligation and stored on the task; the profile's constraints (no memo, attendance gate, cadence) are applied |
| 2 | `verify` | verifier | yes | on fail: `prepare` | payee and amount match the profile and the obligation; the verifier is a principal disjoint from the payer (`authority_model.md#structural-checks-quorum-and-separation-of-duties`) |
| 3 | `consent` | operator-facing agent | yes | on fail: `prepare` | the checkpoint on the `payment` action is resolved by the operator; it carries the payee, amount, currency, period, and rail exactly as the `verify` sign-off recorded them, and refers to that sign-off (`payments.md#a-payments-approver-is-shown-exactly-what-the-verifier-signed`, decision 27) |
| 4 | `pay` | payer | yes | | the `payment` action taken through the action gate, keyed on its dedup key so a re-claim never pays twice (`work_model.md#at-least-once-implies-effect-dedup`) |
| 5 | `reconcile` | verifier | yes | on fail: `pay` | the transfer is read back from the rail at its terminal status, matched to the obligation, and recorded as a `transaction` entity; the sign-off closes the batch |

<!-- /rendered -->

`verify` and `reconcile` belong to a principal other than the payer so that one principal never both
proposes and confirms a movement of money; this is the smallest separation of duties the authority model
names, applied to the workflow where it matters most. `reconcile` exists because a rail's acceptance of a
transfer is not its settlement; a payment whose reconcile step never signed is visible as a batch missing
one. The consent step is named `consent`, not `checkpoint`: a checkpoint is the held state of the
`payment` action, which the step carries to the operator; the step is not the checkpoint. The checkpoint is
written when `consent` opens and the gate holds the action; `pay` asks the gate again at the take, on the
standing resolution, the figures against the tolerance (decision 28), the policy as read then, and the
key (`gates_and_workflows.md#the-checkpoint-is-written-where-the-gate-first-holds-the-action-and-the-permit-is-decided-at-the-take`).

**Stages:** preparation (`prepare`, `verify`); consent (`consent`); settlement (`pay`, `reconcile`).

**Artifacts:** the transfer record at the rail; the receipt or confirmation message. The `transaction`
entity is a record in the record, not an artifact.

**Typical action classes:** `payment`, `transfer`.

**Successors:** none. A confirmation message to the payee is an outreach task, created at `reconcile`
and routed by its own intake.

**Fast paths:** none. A recurring payment graduates under the `action_policy`'s recurrence rule at the
gate, which changes whether `consent` carries a checkpoint, not whether the step exists.

## research and analysis

**Purpose:** answer a stated question from sources, with the answer persisted as an entity whose claims
trace to what was read, and delivered to whoever asked.

**Entry condition:** intake closed naming `research`; the task states the question, the scope, and the
sources permitted; a task that states a conclusion to confirm rather than a question to answer fails
`classify`.

**Steps**

<!-- rendered: workflow=<project>|research steps -->

| # | Step | Step owner (role) | Required | Parallel / join | Closes on |
|---|---|---|---|---|---|
| 1 | `brief` | `pm` step owner | yes | | the question, the scope, the permitted sources, the audience, and the form of the deliverable are stated on the task |
| 2 | `gather` | researcher | yes | | every source read is recorded with its provenance; a source that could not be read is recorded as unread, not omitted |
| 3 | `synthesize` | researcher | yes | on fail: `gather` | the analysis is written with each claim traced to a gathered source; a claim with no source is marked as the author's |
| 4 | `persist` | researcher | yes | | an `analysis` entity holds the full body and is read back; the task refers to it |
| 5 | `deliver` | researcher | yes | | the analysis reaches its audience in the briefed form: a rendered page, a message, or nothing beyond the entity; the sign-off closes the batch |

<!-- /rendered -->

`persist` precedes `deliver` so that what was delivered is what the record holds, and so that a delivery
that fails leaves the work. `gather` records unread sources because a synthesis over a partial corpus
that looks complete is the instrument failure principle 3 names.

**Stages:** framing (`brief`); work (`gather`, `synthesize`); output (`persist`, `deliver`).

**Artifacts:** the rendered page or the sent message, where the briefed form is external. The `analysis`
entity is in the record, not an artifact.

**Typical action classes:** `publish` or `send_external_comms` at `deliver`; none otherwise.

**Successors:** `outreach`, where the deliverable is a message to an external party and the brief said
so; otherwise none.

**Fast paths:** none.

## meeting processing

**Purpose:** turn a transcript into the record's account of the meeting: a summary, the decisions, the
tasks and commitments it created, and the people it involved, captured under the people-data rules.

**Entry condition:** intake closed naming `meeting_processing`; the task references a transcript, as a
`transcription` entity or an uploaded source, and the calendar event or the recording time that locates
it. Only recordings the operator was a party to are processed; a recording the operator was not party
to fails `classify` (`CLAUDE.md`, people-data processing).

**Steps**

<!-- rendered: workflow=<project>|meeting_processing steps -->

| # | Step | Step owner (role) | Required | Parallel / join | Closes on |
|---|---|---|---|---|---|
| 1 | `ingest` | analyst | yes | | the transcript is in the record as a source with provenance, linked to the calendar event where one is found |
| 2 | `summarize` | analyst | yes | | a `meeting_analysis` entity holds the summary, the decisions, and the open questions |
| 3 | `extract` | analyst | yes | on fail: `summarize` | the action items, commitments, and participants are extracted; each participant is a `contact` entity holding what serves the relationship and nothing incidental or sensitive (RGPD Art. 9 categories are summarized or omitted, never transcribed); the fields a `contact` may take from a transcript are the allowlist on the analyst's grant, and a write outside them is denied at admission (`authority_model.md#grants`) |
| 4 | `persist` | analyst | yes | | every extracted task is created in the record and enters its own intake; every entity is read back; the sign-off closes the batch |
| 5 | `deliver` | analyst | no | | a recap per participant is drafted as an outreach task, where the brief asked for one; never sent from this batch |

<!-- /rendered -->

`extract` carries the people-data rule as its closing condition because the extraction is where a
transcript's incidental disclosures would otherwise become durable profile fields. `deliver` is optional
and creates tasks rather than sending, so that no recap reaches a participant without passing the
outreach workflow's review and consent.

**Stages:** intake of the record (`ingest`); analysis (`summarize`, `extract`); output (`persist`,
`deliver`).

**Artifacts:** the calendar event. The transcript is not an artifact: it is a source in the record — the
`transcription` entity, or the uploaded file with its provenance — reached by retrieval and through no
adapter (`work_model.md#artifacts-are-records-a-batch-leaves-never-its-subject`). The `meeting_analysis`,
`contact`, and `task` entities are in the record.

**Typical action classes:** none. Every outward effect is a task for another workflow.

**Successors:** `outreach`, per recap task; otherwise none.

**Fast paths:** none.

## operator-only

**Purpose:** carry a task the swarm structurally cannot complete to the operator, hold it while the
operator decides or acts, and record the outcome, so that operator-only work is on the task path and
visible rather than a notification nobody owns.

**Entry condition:** intake closed naming `operator-only`, or the task's declared action classes include
`operator_only`. The operator-facing agent is the only principal eligible to claim it
(`work_model.md#operator-only-tasks-are-claimed-by-the-operator-facing-agent`). The task is an ordinary
task, not a checkpoint: it raises one only when an action inside it reaches the action gate, which holds
an `operator_only` action with reason `gate_hold`; the checkpoint is what this workflow carries
(`failure_posture.md#what-a-checkpoint-does-not-absorb`).

**Steps**

<!-- rendered: workflow=*|operator_only steps -->

| # | Step | Step owner (role) | Required | Parallel / join | Closes on |
|---|---|---|---|---|---|
| 1 | `present` | operator-facing agent | yes | | the task, its context, and the exact operator action it needs are carried to the operator through the channel the `channel_config` entity names; where an action exists, its checkpoint is what is carried, through the one decision queue |
| 2 | `await` | operator-facing agent | yes | | the operator's decision or the operator's report of the action taken is recorded; the lease is renewed throughout; the deferral is bounded and its exhaustion escalates the task with reason `rounds_exhausted` (`failure_posture.md#the-rules`, rule 5) |
| 3 | `record` | operator-facing agent | yes | | the outcome is written on the task and read back; the sign-off closes the batch and names the successor the outcome calls for |

<!-- /rendered -->

The workflow has three steps rather than one so that "presented and awaiting" is a readable state of the
batch and not a notification's delivery status, and so that a task the operator never answers is
visible as a batch whose `await` step has been open past its bound. That bound is `await`'s `hold_bound`, and
because the step's close condition names no alternative to the operator's decision, reaching it is rule 5's
ceiling (`gates_and_workflows.md#declaration-batch-projection`).

A single step of another workflow whose action is `operator_only` does not route here. It carries the
checkpoint, holds for the confirmation, and closes on it in place — the three steps above are what its life
looks like from its own batch
(`gates_and_workflows.md#an-operator_only-action-is-taken-by-the-operator-and-the-step-that-carries-it-closes-on-the-confirmation-never-on-the-resolution`). This workflow is for a task whose whole work is the operator's.

**Stages:** presentation (`present`); decision (`await`, `record`).

**Artifacts:** whatever record the operator's action left, attached when the operator reports it.

**Typical action classes:** `operator_only`. The class resolves to `NEVER` ahead of any policy; no action
in this batch is taken without the operator (`gates_and_workflows.md#confidence-and-three-blast-tiers`).

**Successors:** whichever workflow the operator's decision calls for, or none. A task the operator
completed by hand closes with none; a task the operator redirected is routed by this batch's closing
sign-off, not by a new intake, because the classification did not change.

**Fast paths:** none.

## session digestion

**Purpose:** turn what an [interactive session](vocabulary.md#interactive-session) did and left undone
into tasks, so that the one execution mechanism holding no lease has a recovery path that is designed and
owned rather than emergent. A session claims nothing, so nothing lapses when it dies and there is no task
to make claimable again; digestion is what stands in for the lease, and it is a workflow because a
recovery nobody owns is not a recovery.

**Entry condition:** intake closed naming `session_digestion`; the task references one or more sessions by
their transcripts. A session is eligible whether it ended cleanly or was interrupted — an interrupted one
is the case the workflow exists for, and waiting for a session to "finish" would exclude it.

**Steps**

<!-- rendered: workflow=<project>|session_digestion steps -->

| # | Step | Step owner (role) | Required | Parallel / join | Closes on |
|---|---|---|---|---|---|
| 1 | `digest` | analyst | yes | | each session in scope has one digest in the record, stating what it claimed to do and what it left open; a session whose transcript cannot be read is recorded as unread, never as empty |
| 2 | `verify` | analyst | yes | on fail: `digest` | every claim the digest carries is checked against the system of record that would hold its effect, and each is marked confirmed, refuted, or unverifiable; an unverifiable claim stays unverifiable and is never promoted to confirmed |
| 3 | `reconcile` | analyst | yes | | claims that survive verification are reconciled against the tasks that already exist, so that a claim already tracked produces no duplicate |
| 4 | `file` | analyst | yes | | every unreconciled item is created as a task and enters its own intake; each is read back; the sign-off closes the batch |

<!-- /rendered -->

`verify` is required and separate from `digest` because a session's own account of itself is a claim and
not evidence: the digest states what the session said it did, and only the system that would hold the
effect can say whether it happened (principle 2). `file` creates tasks and completes none — digestion
recovers work by making it claimable again, which is the guarantee the lease gives the other three
mechanisms, and it never marks the recovered work done on the strength of the session having attempted it
(principle 10).

**Stages:** reconstruction (`digest`); confirmation (`verify`, `reconcile`); output (`file`).

**Artifacts:** the session transcripts. The digests and the tasks are entities in the record.

**Typical action classes:** none. Every outward effect the digestion finds outstanding becomes a task for
the workflow that owns it.

**Successors:** none. Each filed task enters intake on its own.

**Fast paths:** none.

## Whether a stage names anything a step does not

**Open decision 33.** Registered in `conformance.md#the-register-of-open-design-decisions`. Every workflow section above carries a **Stages** line, and
`gates_and_workflows.md#declaration-batch-projection` defines a stage as a contiguous named group of steps
and gives each declared step a `phase` field. No rule reads either: no gate, verdict, fast path,
`applies_when`, successor, or checkpoint keys on a stage, and where a batch is is already its current step
(`vocabulary.md#owner-five-meanings-one-word-forbidden-alone`). So the grouping is stated in two homes —
the prose line and the declaration's field — with no mechanism behind either, which is the shape
principle 9 names.

**The options.** Retire `stage` and the `phase` field, and report where a batch is by its current step.
Or keep the Stages line as authored prose and drop the field, so the grouping has one home and the
declaration carries nothing no rule reads. Or keep both as they stand.

**Why proposed rather than applied.** The Stages lines cannot be replaced by an existing term, only
deleted, and deleting them loses a reporting grain — "the batch is in review" against "the batch is at
`qa`" — which is a convenience rather than a guarantee but is still information a reader has today. And
`migration.md` uses the word in another sense, for its own ordered stages, so a retirement would ban a
word a companion relies on, or force that companion to rename. **What would decide it:** whether any
reader reports on a batch at the stage grain; if none does, the field is decoration by principle 4's own
test. Opened by the simplification pass of 2026-09-05 without the conformance matrix, which had not landed; the proof above rests on principles 6 and 9 alone and is unverified against the matrix.

## What no workflow in this document does

None lets a task that has no intake batch enter it, except intake itself. None names two successors.
None takes a step on an issue or a pull request rather than on the batch's tasks. None takes an outward
effect outside the action gate, none carries a second gate beside it, and none raises task-level failure
anywhere but the checkpoint queue.
None names an agent, an operator, a payee, a contact, or a channel; each is resolved from a context
entity at runtime. Each absence is an invariant of `work_model.md` or `gates_and_workflows.md`, and a
workflow that needs one of them is a change to the foundation, made through a PR that says so
(`conformance.md`).
