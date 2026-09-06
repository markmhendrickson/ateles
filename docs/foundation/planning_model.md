# Planning model: the layered planning records, what a step reads above and below its task, and how the layers are kept cohesive by work

**Keyed document:** read when the plan renderer, the session hooks, the plan and task skills, or this
document change (`conformance.md`). **Kind:** foundation; states the design of the planning layers and
never the state of a checkout or the contents of any operator's records. **Derived from:** the operator's
2026-09-06 question on whether the swarm should maintain an explicit hierarchy of planning materials so
that a step leverages higher- and lower-level context while executing and keeps both cohesive; the
plan-and-task maintenance section of `CLAUDE.md`, read as the requirements the operator has already
stated and not as the target; the planning inventory of the operator's instance as revision 40 of
`status.md` measures it (types, counts, field names, and the edges in use — never a record's text); the
approved instance plan `ent_d10ad28dffb8c6604a4151c2` and its decision keys `enforcement_tier`,
`role_ownership`, and `soft_gate`, read as evidence of need; `work_model.md`, `gates_and_workflows.md`,
`data_model.md`, and `workflows.md`; decisions 17, 18, 30, 36, 39, 41, 43, 46, 47, 51, 52, and 56; and gaps G9, G10, and G31
(`migration.md`), which this document closes. Written against the second rulings pass (revision 39), whose
rulings of 36, 43, 47, 52, and 56 it carries. What is built is `status.md`; how each concept is recorded
is `data_model.md`. Revised by the ancestry pass of 2026-09-06 (revision 45: the operator's question on
missing ancestry — whether the swarm derives an absent parent, or finds the gap — settled against
`#authority-per-level-an-amendment-is-an-action-and-its-class-is-the-levels` and decisions 17, 41, 51, and
52; `judge`'s and `route`'s defect lists extended by one item each, with no new step or type; decision 61
opened). Revised by the priority pass of 2026-09-06 (revision 47: the permissive orphan default reversed on
the operator's argument that explicit ancestry serves both execution and prioritization — every task now
expects an ancestor by default, satisfied at near-zero noise by a default ancestor per intake class rather
than a finding on every orphan; the no-authoring refusal restated as the line the default and the refusal
meet at; decision 61 reframed from a per-level expectation to a per-class default, still open and still
blocked on decision 57). Revised by the rulings pass of 2026-09-06 (revision 48: decision 57 ruled — five
levels, task → plan → project → strategy → mission, on the operator's word; objective settled as content
within a strategy record and not a sixth level. Decision 61 not ruled: the priority pass (revision 47)
reframed the question after this ruling was drafted against the pre-reversal framing, from "where a
per-level ancestry expectation is declared" to "where an intake class's default ancestor is declared," and
the reframed question is left as revision 47 stated it, still open and still blocked on 57, since ruling on
the superseded framing would rule a question that no longer exists in that shape). Revised by the
rulings-61-62-64 pass of 2026-09-06 (decision 61 ruled — the default ancestor named on the `intake_rule`,
unblocked now that decision 57 fixes the levels). Revised by the project-term pass of 2026-09-06 (revision 67:
decision 70 opened — `project` names decision 57's planning level and is also the scoping key on `workflow`
and `batch`, and nothing yet says whether the two are one thing; the possessives that read the key as
ownership are resolved to the owners the rules name, in this document and across the corpus).

## Purpose

State how the swarm holds a hierarchy of planning records — a task under a plan, a plan under whatever
the operator's instance holds above it, up to the record with no parent — so that a step reads the
context above and below its task while executing, and so that the layers are kept consistent with each
other by work the swarm does, under sign-offs, rather than by a convention a session is asked to remember.

## Scope

The planning records: what a planning record is, which edge relates the levels, what a step reads up
and down that edge, which of a record's fields are derived and which are authored, the workflow whose
subject is a planning record, the authority a change at each level needs, what replaces a session's
binding to a plan, and what prevents one workstream's writes from landing in another's record. The task
is `work_model.md`; steps, hydration, and the gate are `gates_and_workflows.md`; the record is
`data_model.md`; the `planning` workflow's step table is `workflows.md#planning`. Which levels an instance
holds, and what it calls them, is the instance's (`#which-levels-an-instance-declares-and-what-it-calls-them`).

## The invariants

### The hierarchy is edges, and a task has one line upward

A **planning record** is an entity of a registered type the registry marks as a planning type, with a
**level**: the rank the mark carries, so that a plan is below a project and a strategy is above an
objective without the design naming any of them. The record's own registry carries the mark, as it carries
the special-category mark and the merge policy (`data_model.md#record-conventions`): a property of the
type, set at registration by the principal accountable for it, and read by the rules below. The design
fixes the shape and not the names: what it says of a plan holds of every level, and an instance that holds
six levels and an instance that holds two are both instances of it.

**The hierarchy is `PART_OF`, upward, one edge per record.** A planning record carries at most one
`PART_OF` edge, to a planning record of a higher level; a task's one `PART_OF` edge — the design already
gives a task at most one (`work_model.md#parent-and-child-tasks`) — targets its parent task or a planning
record, and a task under a parent reaches the planning record through the parent. The **root** is the
record with no `PART_OF`; the depth is open, because the record's cycle check on its hierarchical edges
(`adapters.md#what-the-record-supplies-and-what-an-adapter-therefore-never-builds`) and the level mark on
the type are what bound the walk, not a count of levels. A `PART_OF` whose target is not of a higher level
than its source is refused before it is written, by its one writer: the step owner at `classify` for a
task's edge, and for a planning record's own edge the engine, which under decision 56's shape is the only
principal whose grant admits the write and checks before it writes, as the cross-type cycle walk does
(`work_model.md#a-batch-may-depend-on-a-task-it-created`) — so the walk upward from any task is a path
that ends, and the level of every record on it rises.

That path is the task's **ascent**: the planning records above it, read along `PART_OF` from the task to
the root. It is a derived read, never stored; a task carries no `plan_id`, no list of the records above it,
and no copy of anything they say (principle 11). The word is not *chain*: a chain is the batches a task
has gone through, read along `FOLLOWS` (`gates_and_workflows.md#sequencing-is-data-successors-and-the-chain`),
and the two are different reads of the same task — where it has been, and what it is for.

**One line, never a tree, and the reason is what a second parent would do.** A task that belonged to two
plans would read two ascents, and a step reading its context would have two statements to judge against
that need not agree; every derived read over descendants (below) would count it twice, once under each;
and the one workstream writing into another's record — the failure the maintenance convention was written
against — would be a task legitimately under both, with no edge to say which was wrong. So a task has one
ascent, and a second membership is one of the two edges the design already has for it: `REFERS_TO`, task →
planning record, where the task concerns a record it is not under (a task under a product plan that also
bears on a hiring objective), attached at intake's `link` step because the task names it
(`workflows.md#what-link-attaches-and-what-it-leaves-to-hydration`); or `DEPENDS_ON`, from a planning batch
to a task under another record that it holds on (`work_model.md#a-batch-may-depend-on-a-task-it-created`).
Both are readable from either end, neither is an ascent, and neither is a home for the task's status.

**This closes gap G9.** The instance uses `PART_OF` for a task's membership in a plan and the design
reserved it for child-to-parent; the two were one edge with two targets, and the rule above says so: one
edge type, one parent, the target a task or a planning record. No second edge is named, and no migration
rewrites the edges the instance holds.

### Upward context is a declared read, resolved along the ascent at hydration

**A step declares which planning types it reads, and hydration resolves them along the task's ascent.**
The declaration already carries `reads_to_enter` and `reads_to_close`, the entity types a step must be
able to read before it opens and before it closes (`gates_and_workflows.md#declaration-batch-projection`),
and a planning type is an entity type. So a step that judges a change against its plan's criteria declares
the plan's type; a step that prioritizes declares the types whose weight the rubric reads; a step that
drafts a message to a partner declares the level whose statement says what the operator is pursuing with
that partner. Hydration resolves each from the task's anchors, and the ascent is the anchor set for
planning types: the walk starts at the task, follows `PART_OF` upward, and stops at the highest level the
declaration names (`workflows.md#what-link-attaches-and-what-it-leaves-to-hydration`, decision 39). No
mechanism is added: a "context depth" is not a number on the step but the set of planning types it
declared, and a step that needs the mission declares the mission's type and reads the whole ascent to
reach it.

**An ascent that ends before a declared type yields an empty read, and empty is not unknown.** A task with
no planning record above it — reactive work, a chore, a task nobody planned — reads an empty plan set, and
the step proceeds on it as on any successful read that returned nothing; `unknown` is what a read returns
when the record could not be read (principle 7), and a walk that ended is a read that succeeded. Such a
task is **unplanned**: a derived read over its `PART_OF` edge, counted where the swarm's other instruments
are counted (`principles.md#3-validate-the-instrument-before-believing-the-measurement`), and never a
status. Unplanned work is admitted through intake like any task, because refusing it would force every
chore under a plan it does not serve, and a plan that holds work it does not describe is a plan whose
derived reads mean nothing; the instance's own earlier decision on this, under the key `soft_gate` of the
plan the header cites, is the same position. A step that must not proceed on unplanned work says so in its
own condition — an `applies_when` or a close condition that names the plan — and never in a gate beside
intake (principle 6).

**Downward, a step reads derived state, and a sibling's content only by declaring the sibling's type.**
The derived reads over a planning record's descendants (below) — completion, the open and terminal counts,
which descendants are held by a checkpoint, the open tasks in priority order — are read by any step that
reads the record, because they are reads over its edges and not fields of it. A step that needs a sibling
task's *content* — what another task under the same plan decided, what its acceptance criteria say — is
reading a task, declares `task`, and hydration resolves the siblings from the plan anchor's inbound
`PART_OF` edges by the same bounded retrieval that resolves everything else. It never reads a sibling's
in-progress reasoning, because there is none in the record (`data_model.md#what-each-actor-reads-and-writes`);
what a sibling has decided is in its sign-offs and the entities its batches produced.

**Intake reads the ascent twice, and writes it once.** At `classify`, the task's `PART_OF` to a planning
record is written where the task names one — an issue filed against a plan, an ask the operator made
under an objective, a task a planning batch created under its record — which is what that step already
does for a parent task (`workflows.md#intake`); a task that names none is unplanned from creation. At
`prioritize`, the `priority_rubric` may read the ascent: a task's weight is then a function of the
objective it serves and not only of its own urgency, which is the "prioritization as a read over the
hierarchy" the instance's plan asked for under its key `enforcement_tier`, done at the one step whose
work is priority.

### Downward state is derived; upward content is authored, as entities

**Every field of a planning record that describes the state of the work beneath it is a derived read, and
none is stored.** Completion — every descendant task terminal — is the parent task's rule
(`work_model.md#parent-and-child-tasks`) applied one level up and then at every level: a plan is complete
when its tasks are, a project when its plans are, and so on to the root. The open and terminal counts, the
descendants held by an open checkpoint (the plan's *blockers*), the most recent activity on any descendant,
the fraction of descendants whose chains ended under a declaration that permits the ending (the plan's
*landed* work — `work_model.md#a-task-is-executed-only-through-a-workflow`), and the open descendants in
priority order (what the maintenance convention wrote as `next_steps`) are each read over the `PART_OF`
edges beneath the record and never written to it. This is principle 11 at the level where it was violated:
a stored `status`, a stored `todos_pending` count, a stored list of next steps are each a field that a
session had to keep true, and the June corruption the maintenance convention names was two sessions
keeping the same field true from two stale copies. A record with no stored progress cannot be corrupted
that way, because there is nothing to overwrite.

**What a planning record carries, and is authored, is its statement and its planning decisions.** The
**statement** is what the record is for: its purpose, its scope and what is out of it, and its
`completion_criteria[]` — the conditions under which the work beneath it is done, stated by a principal
and judged by the `planning` workflow (below), where a task's `acceptance_criteria[]` are stated at `pm`
and judged by its own batch (`gates_and_workflows.md#declaration-batch-projection`). A **planning
decision** is a `decision` entity, `PART_OF` the record it was taken under: one entity per decision, with
the decision, its reason, and its date, written once and never edited into another; a decision reversed is
a new decision `SUPERSEDES` the old, and the old stays readable as what was decided then. A record's
`decisions` are therefore a read over its inbound `PART_OF` edges from `decision` entities, and the record
holds no map of them.

**This is what replaces correcting the whole field.** The maintenance convention asks a session to re-read
a `decisions` map and a `todos` array before correcting either, and to merge, because a correction
replaces the entire field and a stale copy silently deletes every other session's entries. Under this
model there is no field to merge: a decision is one entity with its own idempotency key, a todo is a task
(next paragraph), and two batches recording two decisions under one plan write two entities that never
touch. The convention's rule was right about the hazard and wrong about the remedy — the remedy to a field
that several writers must merge is not a merge discipline, which is a rule read once (principle 1,
placement), but a shape in which nothing is merged.

**A todo is a task, and this closes gap G10.** The instance's plans carry a `todos` field, a second task
list beside the `task` type, and the migration held plans until the design said whether a todo is a task
that failed to be filed or a plan's own record. It is the first. A todo is work owed under the plan; work
is a task; a task under a plan is `PART_OF` it; and everything the convention asked of a todo — mark it
done only citing an artifact that resolves, carry the entity ids and paths in a `notes` field — is what a
task's chain and its closing sign-off already carry, with the artifact by edge and "landed" derived rather
than asserted (principle 10). New writes make tasks; the `todos` field and the counts beside it are read by
the tolerant reader as history and written by no canonical writer (`data_model.md#record-conventions`);
plans are no longer outside the four models, because a plan is a planning record and its todos are tasks.

**The write contract, the same at every level.** A planning record's authored content — the statement and
the decisions under it — is written only by a step of a batch whose subject is that record (the next
section), under a held lease, read back, and the write is an action of the class the level carries
(`#authority-per-level-an-amendment-is-an-action-and-its-class-is-the-levels`). Nothing else writes it: not
a session's correction, not a daemon, not a step of a batch under the record whose work turned up a
decision — that step records a finding, and the finding reaches the record through the `planning` workflow.
The derived reads are written by nothing. The one exception is the record's creation, which is a write a
planning batch makes one level up (a plan is created by the `amend` step of its project's planning batch)
or, at the root and at bootstrap, the operator's, as the first declaration is
(`work_model.md#changing-the-swarm-is-work-and-it-goes-through-a-workflow-like-any-other`).

### Maintenance is work: the `planning` workflow

**A planning record is maintained by a workflow whose subject is the record, and by nothing else.** The
design had no workflow whose subject is a plan (gap G31), and what stood in its place was a convention:
a session binds a plan, and on every turn corrects its fields as the work moves. That convention binds
where it is read — in an interactive session, after compaction — and reaches no agent the swarm runs and
no daemon (`principles.md#contradictions-this-document-settles`, C7), which is why the plan it was written
for was corrupted by sessions that had it in context. The `planning` workflow is the design's answer: a
declared workflow, per project and once for the levels above a project, whose batch's task is `PART_OF`
the record it maintains, so that the record is the first entity on the task's ascent and the batch's
subject by construction. Its steps are `workflows.md#planning`; this section states what enters it, what
each step may write, and what none of them may.

**It is entered by a recurring task, one live instance per record, and a child's close pulls the instance
forward.** Every planning record has exactly one live `planning` task under it, carrying a `recurrence`
rule read from the record's `cadence` and a `due_date` on the schedule (`work_model.md#a-recurring-task-is-one-live-instance-and-its-completion-creates-the-next`,
decision 30); the closing sign-off of one instance creates the next, and the first is created with the
record. What makes maintenance follow the work rather than the calendar is one correction: **the closing
sign-off of a task's last batch, where the task is `PART_OF` a planning record, corrects the `due_date` of
that record's live `planning` instance to now.** A postponement by correcting `due_date` is already the
ordinary way to say an occurrence is expected later (`work_model.md#a-recurring-task-is-one-live-instance-and-its-completion-creates-the-next`);
this is the same write in the other direction, idempotent on the record and the closing task, and it
creates nothing. So a plan whose tasks are closing is reconciled after each close, a plan whose tasks are
idle is reconciled on its cadence, and a plan with no live `planning` task is an **unmaintained** record —
a derived read anyone can take, and the shape in which a plan nobody maintains presents: as an absence on
one edge, never as a stale field. The operator's finding on a record they read
(`gates_and_workflows.md#closed-work-is-reviewed-on-the-record-and-redone-through-intake-never-reopened`)
pulls the instance forward the same way. An `intake_rule` may key on a planning type — planning records are
not work-model records, so the exclusion decision 36 rules does not reach them — and a rule that turns a `decision`
created under a record into a `planning` task on the record's parent is a rule the operator may write; none
is required, because the pull-forward above already covers the case a child's close makes.

**Its steps are three, and they divide what is read from what is judged from what is written.** `survey`
reads: the record and the statement of its parent, the derived reads over its descendants, and the
decisions under it — the declared reads of the step, resolved by hydration along the task's ascent and
down the record's edges, and named on its sign-off as what it read (decision 40). `judge` records
findings, one defect each: a completion criterion met that nobody closed on; a criterion the descendants
cannot meet as stated; a descendant whose work the statement does not describe; a decision under the
record that the work has since contradicted; a learning the descendants produced that belongs to the
record's parent; a child record with no live `planning` task. `amend` writes what the findings oblige, and
only through the record's own primitives: a correction to the statement, as an action of the level's
class; a `decision` entity `PART_OF` the record, as the same; tasks created `PART_OF` the record for the
work a criterion still needs, each entering its own intake; one task `PART_OF` the record's **parent** for
each finding whose remedy is a change to the parent's statement — which is the upward half of cohesion,
and it is the standing-finding rule one level up: a lesson at the plan's scope is a finding whose change
lands on what the plan was derived from, and it travels as a task through intake exactly as an
institutionalization task does (`gates_and_workflows.md#a-finding-is-one-off-or-standing-and-a-standing-one-obliges-a-change-to-what-produced-it`,
decision 17), with the batch that raised it not waiting. `amend`'s sign-off is the batch's closing
sign-off; it names no successor and creates the next `planning` instance before it is written.

**What the workflow never writes.** A descendant's status: a task is closed by its own chain, and a plan
that judges a task done writes a finding on the task's batch or a task for the redo, never the status. A
derived read of any kind: no progress, no count, no next-steps list, no completion flag. The parent's
statement: a change the parent needs is a task under the parent, judged by the parent's own planning
batch, so that a plan cannot rewrite the objective it was written under from below. A sibling record's
anything. And nothing on a record the batch's task is not `PART_OF`, which is the collision rule below.

**Other roles judge a planning record as review steps of this workflow, and this closes gap G31.** A
dozen roles on the instance carried a plan-participation protocol — subscribe to plan events, check the
plan against the role's predicate, file a contribution record — which the migration found to be recurring
work with no target. Its target is here: a role's predicate is an `applies_when` on an optional review step
of the `planning` declaration, seated between `judge` and `amend`, whose sign-off carries that role's
findings on the record (`gates_and_workflows.md#declaration-batch-projection`); a contribution is a finding;
a concern is a blocking finding, which `amend` cannot sign around; a sign-off is a sign-off. The
`plan_contribution` type the instance holds is the retired shape of exactly this, and the migration carries
it as the gate-model table carries the other contribution records.

### Authority per level: an amendment is an action, and its class is the level's

**A write to a planning record's authored content is not a governance write.** The closed list of eight
is admitted by one test — what a write to the type changes: what the swarm may do, what a principal is,
or how work reaches the swarm (`gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken`)
— and a statement changes none of them. It changes what the swarm is *for*: which work is created under
it, what every descendant is judged against, which asks are declined because they serve no objective. So a
planning type is admitted to a writer by grant, default-deny, like every other type (decision 41,
`authority_model.md#grants`), and the list of eight is not lengthened.

**It is an action, because of what it can destroy.** The line that makes a governance write and a lossy
mutation actions is not where the write goes but what it can destroy
(`gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken`), and an
amendment to a statement sits on that line: a rewritten objective changes what every plan under it is
measured by and what every task under those is for, and the blast is the record's descendants, which at
the root is everything. So an amendment — a correction to a statement, a `decision` written under a
record, a planning record created — is an **action**, of a class named per level: `amend_<level>`, with
the level's name as the registry marks it, taken at `amend` through the action gate and confirmed by the
read-back of the write. This is the third named class of internal writes that are actions, beside the two
the gate document names, admitted by the same test, and it uses the gate and the queue that exist rather
than a second approval (principle 6). Its enforcement point is the one decision 56 gives a governance
write: the planning types' authored fields and the `decision` type are on the engine's grant alone, the
engine writes them as the effect of a permitted `amend_<level>` action and reads each back, and every other
principal's write to them — the `planner` role's included, whose grant admits `task` and `finding` and no
statement — is refused at the record's admission under decision 41. An amendment with no permitted action
behind it therefore cannot land, for the reason a governance write cannot: the only credential that could
write it writes on nothing else.

**The top is reserved by construction, and loosened per level by the operator.** An action class the
policy lists in neither tier resolves to `NEVER` (`gates_and_workflows.md#confidence-and-three-blast-tiers`),
so an `amend_<level>` class the operator has not listed is held at the gate and awaits the operator, whatever
the confidence and however many times the class has been taken. The operator loosens a level by listing
its class with the tier they want — `amend_plan` low, so that a plan's statement follows its work with no
checkpoint; `amend_objective` high, held once and then taken as its series graduates; the classes above
those unlisted, so that a strategy or a mission changes only by the operator's decision on a checkpoint
that names the change. This is decision 18's shape, arrived at without extending its list: the reservation
is per class, it lives in the policy, and it is the operator's to write. The cost decision 18 accepted is
accepted here — until a level is listed, every amendment at it is a checkpoint the operator resolves —
and the argument for it is the same: the safe direction to be unmeasured in is reserved, and the operator
who has watched the queue grants the level. Where an `ownership_grant` names a principal for a record, that
principal is the required seat on every checkpoint whose subject concerns the record (decision 46), so a
strategy the operator has given to a role is amended only with that role's approval, whatever the tier.

**An initiative is the same action, at the level it changes.** Decision 51 makes an initiative a task by
class whose acceptance is the resolution of the checkpoint on the action it implies — a governance write
or a re-prioritization. A proposed change to what is pursued is an `amend_<level>` action on the record it
would change; what stops is a task, confirmed through the same checkpoint by the seat the record's
`ownership_grant` names or by the operator (decision 52), and the right to propose is the initiative-class
constraint on a `task` write capability that ruling names. This document names the action's class and adds
no object.

### Binding dissolves: a task's ascent is its binding

**The session-binds-one-plan rule does not survive, and what replaces it is the edge every task already
carries.** The convention asked each session to resolve the plan matching its workstream once, bind to
it, and maintain only that plan for the rest of the session. Under this model a session binds nothing: it
is the one execution mechanism holding no lease (`work_model.md#the-four-execution-mechanisms`), its output
becomes tasks, and each task it creates names the planning record it is under at creation, written at
`classify`. The plan a piece of work reports to is a property of the task, fixed at intake by a step
owner's sign-off, and read by anyone who reads the task's ascent; it is not a property of the session that
happened to produce the task, and two sessions producing tasks under two plans are two sets of edges and no
collision. What a session may not do is what the convention had it do on every turn — correct the plan's
fields directly. A session that has learned something a plan should record creates a task under the plan;
the `planning` batch judges it and writes the decision. A session that finishes a piece of work closes
nothing on the plan; the task's chain ends, and the close pulls the plan's `planning` instance forward. The
session-level hooks that bind a default plan at start and judge the session at stop are, under this model,
harness plumbing that reads the record and writes tasks; whether they exist on a checkout is `status.md`.

**The requirements the convention stated, and where each lands.** Every rule of the convention was a
statement of need, and each has a home here:

| The convention required | Under this model |
|---|---|
| bind one plan per session, matching the workstream | a task's `PART_OF` to its planning record, written at `classify`; no session binding |
| re-read and merge before correcting `decisions` or `todos` | nothing is merged: a decision is a `decision` entity, a todo is a task, each its own write |
| mark a todo done only citing an artifact that resolves | the task's closing sign-off, its artifacts by edge, and "landed" as a derived read over its chain |
| record a settled decision as one sentence under a snake_case key | a `decision` entity `PART_OF` the record, written at `amend` as an `amend_<level>` action |
| correct `next_steps` when blockers change | a derived read: the record's open descendants in priority order, and those held by a checkpoint |
| create a task and link it `PART_OF` the plan | unchanged, and the only way work enters a plan |
| correct stale references in `body`, `decisions`, and `todos` when something is renamed | a finding at `judge`, corrected at `amend`; a rename that reaches the record's decisions is a `SUPERSEDES` |
| never write one workstream's entries into another's plan | the subject rule below |

### The mechanism against cross-record collision is the subject

**A planning write lands only on the record the writing batch's task is `PART_OF`, and three things make
that a control rather than a rule.** The edge: the task of a `planning` batch is `PART_OF` exactly one
record, so the record its writes may reach is a read over one edge, with no judgement in it. The action:
every authored write is an `amend_<level>` action `PRODUCES` from that task and `REFERS_TO` the record it
amends, and the engine — the one writer, under decision 56's shape — refuses the write where the referred
record is not the first record on the producing task's ascent, checking before it writes as the cross-type
cycle walk does; so a batch under one plan cannot amend another by naming it, and the refusal is at the
write, not in a rule the batch reads once. The grant: the planning types' authored fields are on the
engine's grant alone, a `planner`'s grant admits `task` and `finding`, and a role whose grant names no
planning type writes none (decision 41). What the June collision was, restated in these terms, is a session — which holds no lease
and is under no task — correcting a field on a record by naming its id, with no edge to check the name
against and a whole map replaced on each write. None of the three conditions held. Under this model the
write has a subject, the subject is an edge, and the field is an entity.

**And where a write does land wrong, the cost is one edge.** A `decision` written under the wrong record
is one entity whose `PART_OF` edge is ended and rewritten by the planning batch of the record it belongs
to, with the misfiling readable as a finding; nothing is rebuilt, and no other decision is touched. That is
the difference between a map and a set of entities, and it is why the convention's merge rule had to be
written and this model has none.

## Which levels an instance declares, and what it calls them

**Ruled (2026-09-06).** Registered in `conformance.md#the-register-of-open-design-decisions`. Five levels,
in order: **task → plan → project → strategy → mission**, with the tenets and principles above the mission
as a level that is read and never amended by the swarm. The operator, on the recommendation put to him:
*"strategic materials (which perhaps can themselves contain objectives) do seem like an important link
between projects and missions."*

**Strategy is a level.** A project's justification jumping straight to a mission is too coarse to derive
acceptance criteria or relative standing from — a mission states a direction long enough to outlive any
one project, and a project needs something at the grain of a strategy to check its criteria against and to
rank against its siblings. Strategy is where a mission becomes actionable, sitting between the two and
carrying that translation. **Cost accepted:** a fifth `amend_<level>` class, and a rung in every declared
ascent — every step that reads upward through a project now has a strategy record between it and the
mission, whether or not that step declared a read that far.

**Objective is not a level.** The operator's own phrase carries the ruling: strategic materials "can
themselves contain objectives" — an objective is authored content *within* a strategy record, never a
record with its own `amend_objective` class, its own reserved grant, and its own rung in the ascent.
Reason: principle 6 (extend the mechanism that already generalizes; do not build a second one where
content already serves) and the identical reasoning that made a `decision` an entity carried under a
planning record rather than a level of the ladder in its own right
(`#downward-state-is-derived-upward-content-is-authored-as-entities`). A strategy's statement may hold
objective-shaped content — the criteria a project beneath it is measured against — and that content is the
strategy's to author and amend, not a second thing the registry marks.

**The options considered.** The full set the operator's instance sketches — task, plan, project, objective,
strategy, mission, with the tenets and principles above the mission as a level that is read and never
amended by the swarm; a shorter set, in which a project is a plan with a longer horizon and an objective is
a strategy's criterion rather than a record of its own; or the two the design needs to state itself — the
task and one planning level above it — with the rest admitted as the operator registers them. The ruling
takes the middle path in a different cut than any of the three as first framed: five named levels, with
objective explicitly excluded from the count as content rather than a level. **What would have decided it
otherwise:** whether any rule above reads a level by name. None does — every rule reads the mark and the
ascent — so the design remained indifferent on the mechanism and the choice was the operator's alone, now
made. Two consequences follow knowingly: each registered level is an `amend_<level>` class the operator
lists or leaves reserved, so five levels are five classes to write, one more than four; and a level whose
records are read by every step beneath it — a mission — is on every ascent, so what it says is read on
every batch that declares it, a statement that long changes the reading budget of every step that reads it
(`conformance.md#always-read`).

**What reopens it.** A rule of the design coming to read a level by name — none does today — or the
operator later finding objective-shaped content outgrowing what a strategy record can carry (a criterion
needing its own lifecycle, its own sign-off, independent of its parent strategy's amendment), which would
be the same argument decision 51 already settled against a second `initiative`-shaped entity, re-litigated
here.

### `project` names a planning level; the scoping key on a declaration is the same word and not yet the same thing

Decision 57 makes `project` a **planning level** — the tier above `plan`, an ancestry mark a task reaches
by its `PART_OF` line, read for justification and for relative standing
(`#the-hierarchy-is-edges-and-a-task-has-one-line-upward`). A level owns its own record, its own
`planning` batch, and the child records its `amend` step creates, and it owns nothing else: no policy,
no workflow declaration, no roster.

The same word is also a **scoping key on two declarations**. `workflow` and `batch` each carry a `project`
field (`data_model.md#concepts`), and `workflow` declares one entity per (project, workflow type)
(`gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken`); the
roster is described as binding per project (`workflows.md#roles-named-in-this-document`) while an
instance's is one global map (`migration.md`, G20). Nothing states whether the key is the level's record
or a separate scope that merely shares its name, and `conformance_suite.md#against-migrationmds-bootstrap-leg`
already records the consequence: a task carries no `project`, no rule says how it acquires one, and so the
intake batch opened at its creation has a `project` from nowhere.

**What this pass settled, and what it did not.** The possessive "the project's `X`" was doing the work of
an answer wherever it appeared — reading as though a project owned a policy, a workflow, or a criteria
entity. Where the design already names an owner, the possessive is now that owner's: the `action_policy`
is the **instance's**, since its row declares no `project` field and it is the one policy a principal
evaluates the action gate against (`github.md#what-this-section-does-not-decide` already names it that way);
`release_criteria` is a context entity **retrieved by type**, like `priority_rubric` and `brand_voice`,
and carries no possessive; a `record_migration` declaration is named by its scoped id,
`` `<project>|record_migration` ``, which states the scoping without asserting ownership. What stays
undecided is the key itself.

**The question, open.** Whether the `project` field on `workflow` and `batch` is a reference to the
planning record at the `project` level — in which case a task acquires one from its ascent and the
`*|intake` wildcard is a declaration that binds at every project — or a separate deployment-scope term
that shares the word and should be renamed under invariant 12, which forbids one term carrying two senses.
Registered as decision 70. Until it is ruled, no document should read the field as ownership: a scoping
key selects which declaration applies, and selecting is not owning.

## Missing ancestry: whether the swarm derives an absent parent, or finds the gap

**The operator's question (2026-09-06):** whether the swarm should derive higher-level planning materials
when lower-level ones lack them — a task serving no plan, a plan serving no project, a project with no
strategy above it, transitively — or report the discrepancy with a proposal to resolve, before or after
resolving it.

### The swarm may not author a missing ancestor

**It may not, and the refusal is already written, not implied.** Deriving a plan to parent an orphan task
is authoring a planning record's statement — the plan's purpose, its scope, its `completion_criteria[]`
(`#downward-state-is-derived-upward-content-is-authored-as-entities`) — under another name than `amend`.
Every authored write to a planning record is an `amend_<level>` action, admitted only through the record's
own workflow, on the engine's sole grant, and an `amend_<level>` class the operator has not listed resolves
to `NEVER` at the gate whatever the confidence
(`#authority-per-level-an-amendment-is-an-action-and-its-class-is-the-levels`). A step that notices the gap
and writes the parent itself — rather than raising a task toward it — is not a special case this rule
missed; it is the exact write the rule names, taken by a mechanism instead of a principal. Nothing in the
gate's test ("what a write to the type changes") carves out a parent created to hold an orphan: it still
changes what the swarm is *for*, which is the whole of what makes an amendment an action. **The refusal
holds, stated plainly rather than left to the gate's silence: the swarm never derives a missing planning
record.** The one exception already on the books is creation by the level above — a plan created by its
project's `amend` step, or the operator's at the root and at bootstrap
(`#downward-state-is-derived-upward-content-is-authored-as-entities`) — which is a parent authoring a child
it already has, never a child inferring a parent it lacks.

### What happens instead: a finding, with a proposal riding beside it

**Missing ancestry is a finding, not a report.** A "report to the operator" outside the record is the
reporting-without-binding shape the finding mechanism replaces everywhere else in this design
(`gates_and_workflows.md#findings-verdicts-and-what-a-blocking-finding-obliges`): it has no author of
record, no severity that binds a verdict, and no edge a later reader can check the disposition against. A
finding has all three, and the design already gives it a route to the checkpoint queue where the defect
cannot be classified alone (`gates_and_workflows.md#a-finding-is-one-off-or-standing-and-a-standing-one-obliges-a-change-to-what-produced-it`).
Missing ancestry is recorded the same way, at the point each shape below names.

**Its kind is `decision_or_attestation`, never `implementation_only`.** Whether a task should serve a plan,
or a plan a project, is a judgement about what the operator is pursuing — exactly the judgement
`implementation_only` excludes, because routing it to an implementer would ask the implementer to supply
the statement the finding exists to demand (`gates_and_workflows.md#findings-verdicts-and-what-a-blocking-finding-obliges`).
The swarm may propose the shape of the missing record; it may not decide that the shape is correct and
write it.

**Its scope is `unknown` by the same test the finding's standing axis already applies.** The finding's
`scope` field decides where a lesson lands — this batch, a step, a workflow, an agent
(`data_model.md#concepts`) — and none of the four is what a missing plan or project *is about*: the gap is
not a defect in how work gets made, it is a defect in what stands above one record. `scope: unknown` is the
correct value on the finding's existing axis, not a misuse of it, because the axis was built for "we cannot
tell which of these four" and this is a fifth kind of question the axis was never asked; recording it as
`unknown` follows the same rule that forbids coercing an undetermined scope to the narrowest one
(`gates_and_workflows.md#a-finding-is-one-off-or-standing-and-a-standing-one-obliges-a-change-to-what-produced-it`)
rather than extending the finding's scope enum with a value this document is not positioned to register.
What the finding's `text` and `REFERS_TO` state — which record is short an ancestor, and at which level —
is what makes the finding actionable without a new field.

**A proposal rides beside the finding because proposing is a grant capability, not a write.** Decision 52
holds that creating a task of the initiative class is a capability distinct from the write it would later
confirm (`authority_model.md#what-stops-is-a-task-the-owner-seat-confirms-it-through-the-checkpoint-and-proposing-is-a-grant-capability`),
and the institutionalization pattern already carries a proposed remedy as a task `REFERS_TO` the finding
that argued it, through its own intake, with the raising batch not waiting
(`#maintenance-is-work-the-planning-workflow`, decision 17). The proposal here is the same shape: a task
`REFERS_TO` the finding, naming the ancestor's proposed statement and level, entering intake, and stopping
at the gate as any `amend_<level>` action does until a principal with standing resolves it. Nothing new is
built — no `proposal` entity exists in this design (decision 51) — and the finding and its proposal are two
entities on one edge, exactly as a standing finding and its institutionalization task are.

**Where the finding is recorded, on a record that already exists.** A plan with no project above it, or a
project with no strategy above it, is found at that record's own `survey`: the declared read of "the
statement of its parent" (`workflows.md#planning`) resolves to nothing, and `judge` records the gap as a
finding of the kind above. This extends `judge`'s defect list by one item — no record above, where the
instance's registered levels say one belongs — the same way the list already carries "a child record with
no live `planning` task" as a defect of what is missing rather than what is wrong (`#maintenance-is-work-the-planning-workflow`).
**Where the finding is recorded, on a task with no plan at all.** Here there is no planning batch to raise
it — the `planning` workflow's entry condition is a task `PART_OF` exactly one planning record
(`workflows.md#planning`), and an unplanned task by definition has none — so the finding is intake's, not
the planning workflow's: `route` closes the task's intake batch with a finding naming the gap, the same
mechanism that already closes intake with a finding when a task's `link` step reads a standing constraint
on an entity it names (`workflows.md#intake`). No new step is added to either workflow; each already has
the seat this finding sits in.

### Before or after: whether a step declared the read

**The operator's "before or after resolving" is answered by whether a step declared the ascent as a read
it needed, not by a rule about ancestry in general.** A step that declares a planning type and reads an
ascent that ends before that type is a step whose declared read returned nothing —
"an ascent that ends before a declared type yields an empty read, and empty is not unknown"
(`#upward-context-is-a-declared-read-resolved-along-the-ascent-at-hydration`) — and a successful read of
nothing is not a hold. So where no step has declared the missing level as a read it needs, nothing is
blocked: the finding above is recorded and rides alongside the work, surfacing when the record's own
`planning` workflow next runs `survey`, exactly as any other finding surfaces at the step that judges the
batch. Where a step **has** declared the missing level — a step that judges a change against a strategy's
criteria, on a task whose ascent ends at a plan — the read is not empty, it is `unknown`: the record the
step needed could not be resolved because it does not exist, which is a failed read and not a successful
one that found nothing (principle 7), and the step holds exactly as any other declared read that cannot
resolve holds. **The test that separates the two:** whether some step on the task's chain named the absent
level in its `reads_to_enter` or `reads_to_close`. A declared read holds the step; an undeclared gap is a
finding that does not.

### Whether an orphan is a defect at all

**Reversed (2026-09-06, superseding the permissive default this section stated the same day).** The
operator argued the other way, in his own words: *"an argument can be made that all work – even household
and correspondence work ought to be taken – in light of higher level justifications and planning since
there's always the question of why we're doing the work even if the answer is often implicit. Making
explicit could help both execute the work with a clearer idea of what it takes to be successful as well as
help prioritize."* The load-bearing point is not tidiness. **An ancestor is where a task's acceptance
criteria and its relative standing come from**
(`#downward-state-is-derived-upward-content-is-authored-as-entities`): a chore `PART_OF` a plan whose
statement is "keep the household unattended and provisioned" has a different `completion_criteria[]` and a
different claim on attention than the same chore with nothing above it, and a swarm that cannot read what a
task is *for* cannot judge either — the connection to priority is argued in full at
`work_model.md#priority-orders-the-claimable-pool-it-does-not-enter-it`. The prior paragraph's reasoning was
not wrong about finding-volume — it is answered below, not retracted — but it was wrong to conclude from
finding-volume alone that no expectation should exist; the operator's argument is that the expectation
should be universal and the noise should be solved a different way. **The default is now: every task has an
ancestor.** An unplanned task is the gap, not the tolerated shape.

**What this does not reopen.** Unplanned work is still admitted through intake — nothing here blocks
ordinary work on a missing ancestor, which is the same refusal-by-forcing this document already named as
wrong, and the operator's own argument is explicitly about explicitness, not gating: "could help... execute
the work," never "must have or cannot proceed." `#before-or-after-whether-a-step-declared-the-read` is
unchanged by the reversal: a step that has not declared the ancestor as a read it needs still opens; a step
that has still holds on the same test as before. What changes is only what happens where nothing was
declared and no default resolves the gap — previously nothing, now a finding, per the next paragraph.

**Where the volume goes: a default ancestor per intake class, not a finding on every orphan.** The prior
section's real concern survives the reversal and is answered here rather than by falling back to
permissiveness. Two readings satisfy "every task has an ancestor":

1. **A finding on every orphan.** `judge` (at a `survey`) and `route` (at intake) raise the
   `decision_or_attestation` finding described above on every task or record with nothing above it,
   whatever the intake class. This gives the operator's ask in full but at the finding-volume cost the prior
   paragraph named: a swarm that flagged every unplanned chore would still be asserting, by volume, that the
   operator's own instance is wrong to run one — the same objection, now overruled by the operator's
   argument rather than by the swarm's judgement of it, but the noise itself does not disappear because the
   objection was overruled.
2. **A default ancestor per intake class.** An `intake_rule`'s classification carries a default `PART_OF`
   target — a standing plan the operator names once per class ("household chore" classifies under the
   operator's standing household plan; "correspondence" under a standing correspondence plan) — written at
   `classify` the same way a named parent is written today
   (`workflows.md#intake`, step 1: "`PART_OF` to a parent... where the work is an aggregate," extended to
   read a class default where the task names no parent of its own). A task classified into a class with a
   registered default is never an orphan at all: it acquires the ancestor at intake, at zero authoring cost
   per task, and the operator has stated the justification exactly once, for the whole class, rather than
   deriving it per instance. Only a task in a class with **no** registered default, or one `classify` cannot
   place in any registered class, reaches `route` still unplanned — and that residue is small by
   construction, not by the swarm's forbearance, so the finding on it is not the finding-volume problem the
   prior paragraph named.

**The second reading carries this pass, evaluated against the first on its own terms.** It gives the
operator what he asked for — every task explicable against a higher-level justification, explicit rather
than implicit — at close to the noise of the permissive default, because the "explaining" work is done once
per class by a principal rather than once per task by a finding. It costs the operator one authoring act per
intake class (naming the standing plan a class of chore serves) instead of zero, which is the price of
explicitness the first reading would extract per-task in findings instead. The first reading is not wrong
and is not foreclosed — an instance may still declare "flag every orphan, even in a defaulted class" as a
per-level or per-class override of the default — but it is not this document's default, for the reason
`#which-levels-an-instance-declares-and-what-it-calls-them` already gives about volume-by-assertion: a
standing finding on every member of a well-understood class asserts, by volume, that the class should not
exist, which is a judgement decision 57 already reserves and which the operator's own argument does not
ask for — he asked for explicitness, not for the swarm to keep re-litigating that chores belong under a
household plan once he has said so.

**What intake does with an orphan.** `classify` first checks the task's own name (an issue against a plan,
an ask under an objective) as it already does; failing that, it checks the task's intake class against the
registered class defaults above and writes the default `PART_OF` where one exists; only where neither
resolves does the task reach `route` unplanned. `route` then does what `#what-happens-instead-a-finding-with-a-proposal-riding-beside-it`
already specifies — closes with the `decision_or_attestation` finding, `scope: unknown`, and a proposal
task riding beside it naming the missing ancestor's shape — and still routes the task to its successor
workflow on the same batch. **The finding never holds the task; it rides beside it.** This is the same
before/after test this document already draws: an *undeclared* gap is a finding that does not block; a
*declared* read on the ascent that cannot resolve is `unknown` and holds the step that declared it
(`#before-or-after-whether-a-step-declared-the-read`). Nothing about the reversal changes which of those two
a given gap is — it changes only how many gaps a well-defaulted instance has left to be either one.

**The refusal from the prior pass is unchanged and restated because the distinction is now load-bearing.**
The swarm still may not author a missing ancestor: `#the-swarm-may-not-author-a-missing-ancestor` holds
exactly as ruled, and the class default above is not an exception to it, stated plainly because the two are
easy to conflate now that a default routinely supplies what a per-task authoring act used to leave missing.
**A default ancestor is a pre-authored record the operator declared once, for a class, before any orphan of
that class existed; it is never the swarm inventing a parent for the orphan in front of it.** The write
`classify` makes is `PART_OF` an existing plan the operator's `intake_rule` already names — the identical
write it makes today when a task names its own parent by identifier — never a write to the plan's own
statement, its `completion_criteria[]`, or its existence. Where no class default exists and no name
resolves, `classify` writes nothing upward and the gap goes to the finding above: the class-default path and
the authoring refusal meet at exactly that line, and nowhere does resolving the operator's argument require
crossing it.

**Decision 61, ruled (2026-09-06).** The default ancestor is named on the `intake_rule`. Of the three
candidates this section carried, the other two depended on decision 57 fixing what the levels are and are
called before a per-level field or a workflow declaration naming "objective" or "strategy" would have
anything to attach to; `intake_rule` is the one candidate that names a record rather than a level, so it
never waited on 57 at all, and 57 is now ruled besides (five levels, `#which-levels-an-instance-declares-and-what-it-calls-them`).

Reason, from principles already in force: the rule that admits a class of task is the thing that knows
which standing planning record that class serves — `intake_rule` already carries, as data, the predicate
that decides a change is a task of a given class
(`work_model.md#an-intake-rule-turns-a-described-change-in-the-record-into-a-task-and-nothing-else`: "a
subject, a predicate, and the text of the task it creates"), so a default `PART_OF` target is one more
value of the same kind on the same record, not a second mechanism (principle 6). It is also already a
governance write reserved under decision 18 — writing an intake rule is reserved to the operator by default,
gated at the same class as a `workflow` declaration — so declaring a default ancestor there inherits that
authority without inventing a new gate for it. The class-default write `classify` makes when it finds one is
the identical `PART_OF` write it makes today when a task names its own parent by identifier
(`#where-the-volume-goes-a-default-ancestor-per-intake-class-not-a-finding-on-every-orphan`); naming the
default on the rule that already produces the task keeps that write and its authorization in one place.
Revision 45's refusal survives unchanged: the swarm still may not author a missing ancestor
(`#the-swarm-may-not-author-a-missing-ancestor`); an `intake_rule`'s default is a record the operator
pre-authored once, for the class, before any orphan of that class existed, exactly as
`#where-the-volume-goes-a-default-ancestor-per-intake-class-not-a-finding-on-every-orphan` already states —
this ruling only fixes where that pre-authored value is written down, not who may write it.

**Confirmed: the `intake_rule` declaration shape carries it.** The shape `work_model.md` states — "It names
the entity types it applies to; the change kinds it fires on... a predicate over the entity's fields... a
predicate over the change's provenance... the title and description the created task will carry... and a
ceiling" — has no field for a default ancestor today, but the shape is data on the record, not a fixed
schema this design closes off; a `default_ancestor` (a `PART_OF` target: a specific plan, project, strategy,
or mission id) is a value of the same shape as the subject and predicate it already carries, checked by
`classify` the same way the existing fields are, and the registered type's own schema (`data_model.md#concepts`)
is where its exact field name and shape are declared, per that section's "What this section does not
decide." Nothing above blocks adding it; nothing above requires a new record to carry it.

**Cost accepted.** A class with no `intake_rule`, or an `intake_rule` with no `default_ancestor` written,
has no default: its tasks reach `route` still unplanned and take the `decision_or_attestation` finding
`#what-happens-instead-a-finding-with-a-proposal-riding-beside-it` already specifies, riding beside the task
rather than blocking it. This is the correct, visible failure — the same fail-closed shape decision 41 uses
for a write with no grant: a class the operator has not yet given a default is a class that says so, in the
open, rather than one silently inheriting a default that does not exist.

**What this does not decide.** The registered field name, type, and merge policy `default_ancestor` (or
whatever a schema author names it) carries on `intake_rule` is the schema's, per `data_model.md#concepts`;
this ruling fixes only the site, not the field's shape. Registered in
`conformance.md#the-register-of-open-design-decisions`.

### Transitivity: judged edge by edge, not over the whole chain

**A gap is judged at each edge, never over the whole chain to a root, because there is no root the design
guarantees every chain reaches.** The **root** is defined as "the record with no `PART_OF`"
(`#the-hierarchy-is-edges-and-a-task-has-one-line-upward`) — a description of whatever a chain happens to
end at, not a named level every instance is required to register. Decision 57 rules this operator's
instance to five levels — task, plan, project, strategy, mission — but the mechanism stated here holds for
any instance's count, including one that registers fewer or declares them under other names: an instance
that declares only `task` and `plan` has a plan as its root, and a plan with nothing above it in that
instance is not missing a project, because the instance never declared one. Judging "does this chain reach
a strategy" would require a level named in the judgement, which is exactly what no rule above does
(`#which-levels-an-instance-declares-and-what-it-calls-them`: "every rule reads the mark and the
ascent... the design is indifferent") even where a level's name and count are now fixed for this instance.
So the only judgement the design makes is local: does *this* record have a `PART_OF` to a record one level
up, where the instance's own registered order says one belongs — the same test `survey` already runs for
the single parent it reads (`workflows.md#planning`), applied at whichever edge is being walked, and never
accumulated into a single verdict about the whole ascent. **This is independent of any instance's level
count:** the judgement is per-edge whether an instance registers two levels or five, because no rule reads
a level by name — only the mark and the ascent.

## Whether the operator's own amendment passes the gate

**Ruled (decision 58, 2026-09-06, with 43, 47, and 56): it does — the operator's amendment at a reserved
level is an action held at the gate, resolved by the operator as a marked self-resolution, and written by
the engine on that permit, never an internal write under the operator's own grant.** Registered as ruled in
`conformance.md#the-register-of-open-design-decisions`. An amendment is an action, and the top levels are
reserved to the operator by construction; the operator amending their own mission by hand is then a
principal taking an action of a class that resolves to `NEVER`, and the question was whether the
operator's path runs through the gate or around it.

**The options, and why the first.** The operator's amendment could be an internal write under a grant that
admits the planning types directly, with attribution as its only record; or an action like any other
principal's, held, resolved, and written by the engine. The three rulings this is ruled with leave only the
second. Decision 43 rules that the operator's own governance write after bootstrap is gated, held, and
resolved by the operator as a marked self-resolution
(`conformance_suite.md#what-the-bootstrap-set-is-and-whether-the-operators-later-governance-writes-are-gated`);
decision 47 rules that a raiser does not resolve, the operator's marked self-resolution the one exception
(`authority_model.md#the-raiser-of-a-checkpoint-does-not-resolve-it-and-the-operators-self-resolution-is-marked`);
and decision 56 puts the governance types on the engine's grant alone, so that a second grant on the
operator's credential would make the sole writer not sole
(`gates_and_workflows.md#where-the-enforcement-point-for-a-governance-write-sits`). An amendment to a
planning record takes the same enforcement point (`#authority-per-level-an-amendment-is-an-action-and-its-class-is-the-levels`),
so the operator's amendment takes the same path: the operator's task produces the action, the gate holds
it, the operator resolves it as a marked self-resolution, and the engine writes and reads back. What the
operator wanted to read back later — that a mission changed by hand is findable as a decision with a date and
a reason — is what this gives: the `decision` entity, the checkpoint that carried it, and the resolution marked
as the operator's own.

**Cost accepted.** The operator's change to their own mission is a task, a checkpoint, and a resolution
before it is a write — three records for one edit, and a step of the operator-only workflow carrying it
(`workflows.md#operator-only`). Accepted for the reason 43 accepted it: the alternative is a second grant
over the record's most consequential types, held by the one principal whose writes to them are the
hardest to review afterwards.

**What would reopen it.** Decision 43 or 56 reopening; nothing about planning records in particular.

## What this document does not decide

How a `cadence` is spelled, and how the level mark is written in the registry — the schema's
(`data_model.md#record-conventions`). Which `amend_<level>` classes an operator lists, and at which tier —
policy values under decision 18's shape. Whether an intake rule keys on a planning type, and on which
change — the rule's, written through the gate. What a `priority_rubric` reads from the ascent, and how it
weights it — the rubric's, a context entity retrieved by type. Whether a rendered document is derived from a
planning record's statement — a render target of it, checked by `--check`, as the plan-mirrored documents
already are (`conformance.md#direction-of-truth-per-class-of-record`), and the direction stays record to
document. And which of an instance's existing records are planning records at all: a record the operator
keeps for reading and never amends — a tenet, a principle — may be registered as a level the swarm reads and
whose `amend_<level>` class is never listed, or left out of the hierarchy entirely, and the choice is
decision 57's.

## Contradictions this document settles

**The maintenance convention against the work model.** `CLAUDE.md`'s plan-and-task maintenance section
asks a session to bind a plan and correct its fields every turn; `work_model.md#the-four-execution-mechanisms`
says a session holds no lease and its output becomes tasks, and `principles.md` says a rule read once at a
session's start binds weakly. Resolved for the work model: the convention's requirements are met by tasks,
edges, and the `planning` workflow (`#binding-dissolves-a-tasks-ascent-is-its-binding`), and the convention
is what the design replaces. The `CLAUDE.md` section stays as it is for the checkouts that read it; whether
it has been retired on a checkout is `status.md`.

**Stored plan state against principle 11.** The instance's planning types carry `status`, `outcome`,
`todos_pending`, `todos_completed`, and `next_steps` as stored fields, and a `decisions` map. Resolved for
the principle: each is a derived read or a set of entities under this model; the fields stay for the
tolerant reader and are written by no canonical writer, and the disposition is `migration.md`'s.

## Prior art

The division between an authored objective and derived progress is the OKR discipline's (Grove; Doerr): an
objective is stated by a person and its key results are measured, and a key result nobody measured is not
one. What the design refuses is the tool-shaped inversion of it — a hierarchy whose every level carries a
rolled-up status field that a sync job keeps true, which is the shape the instance's imported project type
carries under its `outcome_id` and the parent-task rule already refuses at the task. A decision as an
immutable entity that a later one supersedes is the architecture decision record's form (Nygard): one
record per decision, never edited, superseded by a new one that says so. Sources: the operator's instance
inventory (`status.md`, revision 40); `ent_08460968e6f49dac21510f4a`.

## Beyond the sources

The ascent as a derived read distinct from the chain; the `planning` workflow's three steps and its entry
by one live recurring instance pulled forward on a child's close; the `amend_<level>` action class as the
third named class of internal writes that are actions, reserved by construction through the unclassified
case rather than by lengthening the governance list; the closing of G9, G10, and G31 by one edge, one
type, and one workflow; and the reading of the maintenance convention as requirements met elsewhere are
this document's. The instance's plan under `ent_d10ad28dffb8c6604a4151c2` proposed a plan-tier link with a
warn-only sweep; this document keeps its position on admitting unplanned work and replaces the sweep with a
derived read, the tier with the ascent, and the impact loop its keys name with the `planning` workflow's
findings against the record's own `completion_criteria[]`.
