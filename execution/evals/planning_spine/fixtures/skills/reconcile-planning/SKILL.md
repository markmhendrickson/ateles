---
name: reconcile-planning
description: Retrospectively reconcile planning records with reality by repairing duplicate, superseded, chained, and mis-levelled records while treating each task's PART_OF ascent as the live planning source of truth. Use for planning drift or a pre-meeting audit, not as the real-time status mechanism.
slug: reconcile-planning
user_invocable: true
triggers:
  - /reconcile-planning
  - reconcile planning
  - repair planning records
supported_harnesses:
  - claude-code
  - cursor
  - codex
---

# reconcile-planning

## Planning spine contract — retrospective repair only

This skill is **retrospective repair, not the real-time source of truth**. Interactive status and session resumption derive current scope from each task's upward `PART_OF` ascent every time they run. Reconciliation searches history for defects in that spine, records or routes repairs, and then stops; no session should wait for a reconciliation run before it can read a sound ascent.

For every task in scope, inspect its outbound `PART_OF` set before grouping or creating anything:

- **Exactly one:** walk the registered planning levels and report the resolved plan, project, strategy, and any higher declared level.
- **Zero:** record **missing ascent** (or explicitly unplanned work where that is valid). Never infer an ancestor from title similarity, session membership, or the nearest plan.
- **More than one:** record **duplicate ascent**, name every target, and do not choose one silently.
- **An early end:** preserve the partial chain and report the missing expected level; never synthesize it to make the table complete.

The task and its ascent are the planning source of truth. A prior digest, a session's compatibility binding, a plan's stored status/todos/next-steps fields, and this skill's previous report are evidence to check, never authority. The hierarchy report therefore starts with a bounded task-ascent ledger:

| Task | State | Plan | Project | Strategy | Ascent defect / repair |
|---|---|---|---|---|---|

A missing ancestor that requires judgement becomes a finding or proposal for the planning workflow. A duplicate ascent becomes an invariant-repair task. Only make a mechanical repair when an authoritative locator and the currently authorized write path determine the exact edge; read it back afterward. This section governs any older parent-creation language below: it never authorizes inventing ancestry.

This contract does not retire the legacy session plan-maintenance policy. Keep that compatibility policy active until the planning workflow, derived reads, and PM-10 write-path cutover are live; reconciliation may expose its drift but must not present its own output as authoritative live progress.

## One command, whole job — by invoking, not reimplementing

Running this skill reconciles planning records with reality **end to end**. The
caller issues one command and gets the whole outcome; they should never have to
know the pipeline has stages.

It gets there by **invoking** the stages that already exist, then doing the part
only it does:

| Step | How | Owns |
|---|---|---|
| 1 | invoke `/review-sessions` | sweeping sessions, transcript compilation, lineage dedup |
| 2 | invoke `/verify-work` | checking claims against systems of record |
| 3 | invoke `/reconcile-tasks` | filing tasks, candidate dedup, instance routing |
| 4a | **this skill** | **collapsing duplicate, superseded and chained records** |
| 4b | **this skill** | **plans and projects over what remains** |

**Invoke them; do not reimplement them.** An earlier version of this skill carried
its own copy of all three, and the copies drifted. Duplicated prose specs cannot
fail a test or show a diff, so the only symptom is two runs over the same sessions
disagreeing — which reads as one being wrong about the sessions rather than as the
skills having diverged. One measured instance: the `include_archived: true` fix
lived only in this skill, so the pipeline's own sweep kept undercounting.

Invoking keeps that fixed in one place. When `/review-sessions` improves, this skill
inherits it with no second edit.

**Why this is not merely tidier.** The duplicated version omitted verification
altogether. On 2026-09-17 it spent a full day writing plans and decisions into a
local scratch database while every call returned success, because nothing in it
checked what it was talking to. Step 2 is not a convenience; it is what makes the
writes real.

### When a step fails, say so — never silently continue

A skipped or failed stage must be **visible in the report**, not swallowed. If
`/review-sessions` cannot reach a transcript, or `/verify-work` cannot reach a
system of record, the hierarchy built on top is only as good as what did run.

Name in the report which steps ran, which were skipped and why. A run that implies
coverage it did not achieve is the same defect as a commitment nobody recorded.

### Skipping steps deliberately

Some callers legitimately want only part of this:

- **`--hierarchy-only`** — tasks are already swept, verified and filed, and only the
  parents are missing. Skips steps 1–3. The report MUST state that verification was
  not inherited, so the claims are only as good as their sources.
- **A pipeline run** that has already executed stages 2–3 invokes this skill directly
  at step 4; that is the same thing and carries the same caveat.

Default is the full sequence. Skipping is opt-in and always disclosed.

## Check the target before writing anything

Inherited from `/verify-work`, and non-negotiable because this skill does the
heaviest writes in the pipeline.

Before the first write, confirm which store you are on:

- `get_entity_type_counts` — does the magnitude match the instance you expect?
- `get_session_identity` — is `app_origin` the instance you intend, and is the
  `user_id` a real user rather than all-zeroes?
- `describe_instance_policy` — a `policy_id` naming a QA or test fixture means you
  are not on production.

Three hard-won transport rules that apply to every write here:

- The neotoma CLI can route writes to a local SQLite file and still return
  `success: true` while the remote is untouched (neotoma#2089).
- `localhost:9180` and a hosted origin are **different stores**, not two views of one.
- A write to an **undeclared field** is accepted, preserved in `raw_fragments`, and
  **excluded from the snapshot**. Read the field back out of `snapshot` before
  reporting it stored — a success code is not a landed value.

## Collapse the redundancy before drawing parents over it

Run this BEFORE creating any parent. A hierarchy built over duplicated records
makes the duplication permanent and harder to see, because each copy acquires a
different parent and then looks like distinct work.

### Match on locators, never on names

`list_potential_duplicates` does not do this job. It scores `canonical_name`
string similarity, which is the wrong test in both directions: two records of the
same work rarely share a title, and two that do are often genuinely different.
Measured on a live instance, it returned zero candidates across 174 open tasks
while confidently pairing two different people at 0.92 on a one-letter name
difference.

So match on what is unambiguous — the **locators** two records share:

- the same PR or issue number
- the same `ent_…`
- the same commit SHA
- the same Gmail draft id, file path, or external identifier

A shared locator is evidence. A shared word is not.

### Four shapes to look for, each with a different resolution

**1. A container closed while its real work stayed open.** The closed record reads
as done and the open one becomes invisible, because nothing links them. Resolution:
reopen the link, not the container — record on the open item that it is the live
remainder, and on the closed one what it did NOT discharge.

The case that motivated this rule: an issue titled for a payment outage was closed
COMPLETED while the issue naming its actual root cause stayed open, unassigned, for
over a week. Payments were silently failing the whole time, and the closure is why
nobody looked.

**2. Superseded but still open.** Work landed elsewhere and the original was never
closed. Resolution: **verify by content, never by ahead/behind or by topology** —
a squash merge hides supersession from the commit graph. Read the diff, or read
the merged artifact, before asserting one supersedes the other.

**3. A chain of blockers where the record shows only the outer link.** A task
blocked on a PR that is itself blocked on another PR. Resolution: walk the chain to
its end and record the REAL blocker on the outer task, not the immediate one. A
task whose stated blocker is itself blocked is a task whose stated blocker is wrong.

**4. The same work at two altitudes.** "Build the sync" and "Decide whether these
rows should enter at all" are one question split across a decision and its
implementation. Resolution: usually keep both and make the dependency explicit —
this is a real sequence, not an accident. Merge only when neither can be finished
without the other.

### What to do, and what never to do

- **Record the relationship; do not silently merge.** `SUPERSEDES` and `DUPLICATE_OF`
  are edges for exactly this. An edge is reversible and auditable; a merge is neither.
- **Never close a record to resolve a duplicate.** Closing is how shape 1 is created
  in the first place. Closing work is outside this skill entirely.
- **`delete_entity` does not clear the resolver index**, so deletion is not a
  cleanup path — a later store re-matches into the deleted row.
- Where the right resolution needs a judgement you cannot derive — which of two is
  canonical, whether supersession is complete — **leave both and name the question**.
  A wrong merge is far harder to find and undo than a visible duplicate.

### Report it

Name every pair found, its shape, the shared locator that proved it, and what you
did. Redundancy found and left unresolved is a finding, not a failure — say which
question is blocking it.

## Level is derived from structure, never from what a record is called

Before creating anything, check whether the records that exist are at the right
level. A record's TYPE and its actual SHAPE drift apart, and when they do every
count and every report built on top inherits the error.

**The test is structural and has one question: what are its children?**

- Children are **tasks** → it is a **plan**.
- Children are **plans** → it is a **project**.
- **No children and no siblings** → it is a **task**.

Nothing else decides it. Not the `is_project` flag, not the word in the title,
not how important the work feels. The flag is a claim about the structure and can
be wrong; the edges are the structure.

**`project` is not an entity type on this instance — it is `is_project: true` on
a `plan`.** Promoting to project means correcting that boolean, never creating a
different type. The schema describes the field as matching a harness's
`isProject` frontmatter flag, which is what it is: an import artifact that became
the data model.

**Measure the flag's distribution before trusting any single value of it.**
Measured 2026-09-17: 11 of 13 plans carried `is_project: true`, including
single-outcome deliverables like deploying an app and renaming a UI label. A flag
that is true on 85% of records carries no information, so a `true` read on one
record is not evidence of anything. Re-measure rather than assuming this ratio
still holds.

The structural test above is what the professional models converge on. PMI
defines a program as a group of projects *"managed in a coordinated way to
realize benefits that cannot be achieved separately"*; Linear groups issues
"around a shared outcome" and places initiatives above projects where several
contribute to one goal. Each is the same question: **is there a benefit here that
no single child delivers alone?** No benefit beyond the sum of the parts means
the level is a folder, and a folder should not exist as a record.

### Promote when a record is doing a parent's job

The tell is **a list inside a record** — inline `todos`, a numbered body section,
"phases" in prose. Those are children that never became entities. They have no
owner, no urgency, no due date, and no query can find them.

Observed: a plan showing zero children while carrying seven inline todos, one of
them blocked on a named external dependency. It was already a plan structurally
and was being read as an empty one. Promote by converting the list to real
records and attaching them, not by changing a flag.

### Demote when a record claims a level its children do not support

A parent flagged as a project whose children are all tasks is a plan. Nothing
breaks immediately, which is why it survives — but it ranks wrongly in every
hierarchy view, and a reader looking for the plans under a project finds tasks.

Observed, and worth stating because it is the failure mode of this whole section:
a plan holding 33 tasks was flagged `is_project: false`, and an instruction went
out to set it TRUE on the strength of its size. **An agent checked the children,
found all 33 were tasks, and set FALSE instead** — correctly refusing the
instruction. Size is not level. If a directive and the edges disagree, the edges
win, and the disagreement is worth reporting rather than quietly resolving.

### Do not invent depth — one level of nesting is the working limit

There are no subtasks on this instance: `task` has no parent-task field and the
only hierarchy edge is `PART_OF`. Do not simulate them by pointing a task at
another task. A task that needs children is not a task — it is a plan, and the
promote rule above is how it gets there.

This matches the practice literature rather than merely the schema. One level of
nesting covers the large majority of cases; three or more is the signal that a
task was really a project all along. The argument against deeper nesting is that
it becomes a hiding place — rearranging a hierarchy feels like progress while
producing none, and a step containing its own steps is a project wearing a step's
clothing. This pipeline exists to catch work that was recorded but never became
actionable, so a structure that hides complexity is the failure mode, not a
convenience.

### A plan nested inside a project is legitimate

A plan may be `PART_OF` another plan or a project and still have children of its
own. That is not mis-levelling; it is depth. It becomes a project only when its
own children are plans.

Show the nesting where it exists rather than flattening it — a nested plan listed
alongside its parent reads as a peer and inflates the apparent count.

### Correct the level, then say so

Level corrections are one-field writes and belong in the same run. But state every
one in the report with the evidence: **what the children actually are**. A level
changed without that is indistinguishable from a level changed by preference, and
the next run will change it back.

**Where the children are mixed** — some tasks, some plans — stop and ask. That is
usually a parent doing two jobs, and splitting it is a judgement about scope
rather than a mechanical correction.

## Create the parents the work needs

A run that reports "no project exists for this" and stops has left the reader holding the same gap it was sent to close. **Create the missing plan or project**, then attach the tasks to it. That is the difference between reporting drift and reconciling it.

The bar for creating a parent is evidence, not tidiness: **two or more tasks that share an outcome, where finishing one alone does not deliver it.** That is a plan. Where several plans share a goal and a reason for existing, that is a project. A single task with no siblings is a task — do not wrap it in ceremony.

### Naming

**Every record a person reads gets a human-readable title in imperative voice**, naming the outcome someone will be able to do. This applies to projects, plans and tasks alike — the rule is about records people read, not about one type.

- **Imperative, not nominal.** "Deliver the policy and skill roster to connected sessions", not "Instance provisioning" or "Provisioning work". A noun phrase describes a topic; an imperative names a result and makes it obvious when the thing is finished.
- **The outcome, not the activity.** "Prove the instance works when installed", not "Run evals". The activity is how; the title should carry what.
- **Never a slug.** `bottega8-app-ui-plan` is an identifier, not a title. Slugs belong in `slug`; `title` is required and exists for this.
- **Name the result, not the mechanic.** "Push commit 593e2e102" says what keystroke to make; it does not say what changes in the world. "Get standing rules reaching members' agents by pushing the fix that exists only locally" says both, and survives the commit being rebased. A title a reader cannot evaluate without opening the record has failed.
- **One task, one deliverable.** "Three unsent Gmail drafts" is a container, not a task: it cannot be done, only partly done, and its status is meaningless while any part is outstanding. Split it. Three emails to two people about three different subjects are three tasks, each named for what that message settles. The only things that belong together in one task are steps that must all land for any of them to count.

The test for both: could someone mark this done, and would everyone agree it was? A container fails because parts remain; a mechanic fails because pushing a commit that gets reverted is not done.

**A PARENT ALSO CARRIES WHO IS BETTER OFF.** Projects and plans get one more
requirement than tasks do: the title says what changes for someone, not only what
gets built. A task can name a deliverable and stop, because its parent supplies
the why. A parent has nothing above it to borrow from — if its title does not
carry the benefit, the benefit exists nowhere a reader will find it.

Usually this is a trailing clause, and it costs four or five words:

- "Deploy the Bottega8 app **so the team can use it**"
- "Settle one lead-qualification method **the whole team scores against**"
- "Harden the shared instance **so the client can audit who holds what access**"
- "Establish how Bottega8 works a lead together **rather than person by person**"
- "Settle the commercial boundary **before it is decided by default**"

Each names a person and what improves for them. Compare the ones that stop at the
deliverable — "Build v1 of the leads dashboard", "Finish the app UI decisions still
outstanding". Both are honest imperatives naming real outcomes, and both are
invisible to anyone who does not already know why they matter. Measured on a live
instance: nine of thirteen parents carried the benefit, and the four that did not
were exactly the ones reading as inward-facing engineering.

Two cases where the clause is wrong rather than missing:

- **When it would be invented.** "so the team moves faster" on work whose benefit
  nobody has argued is a guess wearing a rationale. Leave the title short and put
  the open question in the body. A fabricated benefit is worse than an absent one,
  because it forecloses the discussion.
- **When the outcome IS the benefit.** "Clear the replies and materials owed to
  Bottega8 partners" needs no clause — being owed nothing is the whole point.
  Adding "so partners are not waiting" only restates it.

The test: **read the title to someone who has never seen the project. Can they say
who is better off when it is done?** If the honest answer is "only someone who
already works on this", the title is naming a deliverable and calling it an outcome.

Where an existing parent fails this, correct the title in the same run — it is a
one-field correction, and leaving it means every future report inherits a row
nobody outside the work can read.

When reading records back, render `title`. A report showing slugs has substituted the machine's name for the human's and will read as though the records are poorly named when they are not.

Where an existing record's title is nominal or is a slug, **correct the title in the same run** — it is a one-field correction, and leaving it means every future report inherits the same unreadable row.

**Attach the tasks in the same run.** An orphaned parent is worse than none — it looks like structure while carrying nothing.

**Two cases where you stop and ask instead of creating:**

- **The shape depends on an unsettled decision.** If how the work is modelled is itself the open question, creating a parent bakes in an answer nobody gave. Say what the decision is and what each branch would produce.
- **You cannot tell which instance it belongs to.** Routing is not a coin flip; a wrong parent in a shared graph is visible to colleagues and cannot be quietly undone.

Where you do stop, say so as a proposed record with the name and reason you would have given it — so accepting it is one word rather than a fresh derivation.

## Reconcile — never blindly create

Before writing anything, retrieve what already exists. A task that duplicates one already recorded is worse than no task: it splits the truth in two and neither copy is authoritative.

- **Exists and is accurate** — leave it. Say so in the report.
- **Exists and is stale** — correct the field that moved, not the whole record.
- **Does not exist** — create it, in the right instance, at the right level.

Read every write back. A success code is not a landed value: a write to an undeclared field returns success, preserves the value in `raw_fragments`, and never reaches the snapshot the app reads. Confirm the field is in `snapshot` before reporting it stored.

## Identity, and the rule that matters most

Resolve every person before attaching work to them. Exactly one match, or stop and ask.

A real collision from this corpus: two people share a first name, one a colleague and one an unresolved lead from a bulk import. A meeting note was attributed to the wrong one and the error survived until someone noticed by eye. Attaching a commitment to the wrong person is worse than capturing none — it is invisible and inherits the credibility of everything around it.

Prefer `identify_entity_by_signals` and `list_potential_duplicates` over a name match.

## Assignee is not the only role

A person can be waiting on work without owning it. Today most task models carry an assignee and nothing else, which is why "what is outstanding between these two people" cannot be queried and has to be swept by hand.

Where the model supports it, record both: the **owner** who will do the work, and the **dependent** who is waiting on it. Where it does not yet, say so in the report rather than flattening the dependent into a note — the gap is the finding.


## Hand over the material AND the instruction to search it

This skill dispatches constantly — to `/review-sessions`, to `/verify-work`, to
subagents that read transcripts and verify claims. What you put in a brief
decides what comes back, and there is one failure that recurs.

**A curated candidate list reads as a finished search.** Name three things you
noticed in a source, and the agent will assess those three and report on the
source. It is not being lazy: a list from the dispatcher looks like the product
of a search the dispatcher already ran.

Observed 2026-09-18. A session pulled a 68-minute transcript, flagged three
passages as "candidates I noticed but did not develop", and handed both the file
and the list to another session. That session assessed the three and reported
nothing relevant. The operator pushed back, a term-frequency sweep of the same
file took thirty seconds, and it surfaced the passage that actually mattered —
one neither session had looked at.

So when a brief carries both material and a reading of it, say which is which:

- Give the **whole artifact**, not an extract. A path to the file, not quotes
  from it.
- State that your list is **incomplete by construction** — "these are starting
  points, not the result of a sweep."
- Name the **sweep to run**: a term-frequency pass, a grep list, a section walk.
  Cheap and mechanical beats a reader's judgement about where to look.
- Give the **negative controls** so the agent can verify your claims rather than
  inherit them. "grep -ci 'X' should return 0" is checkable; "X does not appear"
  is not.

### The same shape, three places

This is one instance of a pattern worth recognizing, because it also appears in
the data:

- A **matcher** that works on the common encoding and silently misses the rest —
  a name-based person match that misses every record keyed by URL; a JSON-only
  parser that scores 85 prose-encoded `next_steps` fields as zero items.
- A **count** taken against the wrong target that looks authoritative because
  nothing announces the gap.
- A **curated list** that stands in for the search.

Each produces a confident, fluent, checkable-but-unchecked answer. The remedy is
the same every time: prove the instrument on a known-positive case before
trusting a negative, and say which surfaces you actually searched.

## Report

Two tables, one per level, in descending order. Prose makes the reader rebuild the
hierarchy in their head, which is the thing this stage exists to stop.

**Link every record to its instance's app**, rendering the *title* as the link text
so the row stays readable — never a bare `ent_…`. **Keep cells narrow**: never a full
`owner/repo#N` in a cell (renderers expand it into a wide chip), short linked titles,
SHAs trimmed to eight characters.

### Projects

| Project | Instance | State | Plans | Open tasks | Moving? |
|---|---|---|---|---|---|

`Moving?` is the column that earns the table: name what is actually in flight under this project right now, or say **nothing** plainly. A project with plans and tasks but no movement is the finding.

**Every count in every table is queried, never left blank.** A dash means the queried answer was genuinely zero. It must never mean "did not look" — a table full of dashes reads as an empty graph and will send the reader hunting for structure that already exists.

Get child counts from the edges (`list_relationships` with the parent's `entity_id` and `PART_OF`; the `total` is the count, so `limit: 1` suffices). Do not infer parentage from a `canonical_name` prefix: prefixes appear on some records and not others, and counting them undercounts badly. A run that reported four plans as having no tasks was reading prefixes; the edges showed 47, 22 and 8.

Where a count genuinely cannot be obtained, write **?** and say why in the coverage note. An unobtainable number and a zero are different facts.

A project created by this run is marked **(created)** in its row. One that was NOT created because it hit a stop-and-ask case above is marked **(proposed — blocked on: …)** with the reason, never left out. An absent parent is invisible otherwise, and that absence is exactly the complaint this skill was built for.

### Plans

| Plan | Project | Instance | Status | Tasks done / open | What moved this run |
|---|---|---|---|---|---|

`What moved this run` is the reconciliation itself: created, corrected, confirmed-accurate, or nothing. A plan whose row reads "nothing" across several runs is stalled, and the table makes that visible without anyone tracking it separately.

### Tasks

**This skill does not render the tasks table.** That is `/report-tasks` (stage 6),
which owns urgency rendering, the dispatch column and the operator-involvement
column. Rendering it here produced a second copy that drifted from the first.

What this stage owes the task table is the `Project` column: every task it attaches
gets a `PART_OF` edge to its parent, so stage 6 can render the parent without
guessing. A task this stage leaves unattached is an **orphan** — name it in
Unresolved rather than letting a blank cell imply it was checked and found empty.

### Then, in prose

Only what a table cannot carry:

- **Unresolved** — identities that did not resolve to exactly one person, work whose instance is ambiguous, records that contradict each other across systems.
- **Coverage** — per session, read or skimmed, and what was not reached. A sweep that implies coverage it did not achieve is the same defect as a commitment that was never recorded.
- **Leverage** — one or two lines on which tasks unblock the most, and any that would collide if dispatched together. An ordering argument, not a restatement of the table.

**Decisions do NOT go here.** They were asked through the questions tool when they
surfaced, before this report was composed. What belongs in the report is at most one
line naming which decisions are still unanswered — never the decisions themselves,
restated as prose for the operator to work through after the fact.


## Ask the moment a decision appears — do not save it for the end

**Every decision that needs the operator, and every piece of input only they can
supply, goes through the harness questions tool as soon as the run discovers it.**
Not collected into a closing section, not offered as "shall I walk you through
them", not written as end-of-turn prose. The moment reconciling a task surfaces
something you cannot settle from the record, ask.

This is the rule that outranks the report. A run that produces a beautiful three-table
report and leaves six decisions sitting in prose underneath it has failed at the thing
the operator actually needed, because the decisions are what gate the work and the
tables are what describe it.

Why immediately rather than at the end:

- **The operator answers while the context is loaded.** A decision posed next to the
  record that raised it needs no re-derivation; the same decision posed forty rows
  later needs a paragraph of setup that the questions tool cannot carry well.
- **Answers change the rest of the run.** An instance-routing answer determines where
  every subsequent record is written. Asking at the end means writing first and
  discovering the routing was wrong afterwards, in a shared graph, where it cannot be
  quietly undone.
- **A closing offer is a second gate.** "Would you like to go through them?" asks the
  operator to opt in to work they already need. Skip it and ask the question itself.

Batch only where answers are genuinely independent and the operator is looking at one
screen anyway — up to four per call, which is the tool's limit. Never batch a question
whose answer changes another question in the same batch.

### Every question must stand on its own

The operator has run many sessions and does not carry this run's shorthand. A question
they cannot answer without going and looking something up is a failed question — it
costs a round trip and returns nothing.

**Never name an issue, PR, entity, branch, or file by identifier alone.** `bottega8#79`
is not a subject; it is an address. Say what the thing *is*, in plain terms, in the
question itself:

- Wrong: "bottega8#79 — evaluation vs free-form analysis?"
- Right: "When the app reads a lead's profile URL, should it score the profile against
  fixed criteria, or write a free-form summary of what it finds?"

Read the issue, PR, or entity before composing the question. If you have not read it,
you cannot describe it, and an identifier is what you fall back on. The identifier
belongs in the option descriptions as a reference, never as the subject.

**State what is already settled.** The operator should not be re-deriving context they
established two weeks ago. One clause is usually enough.

**Recommend one option and say why**, marking it `(Recommended)` and putting it first.
A decision handed over without a recommendation is a request for the operator to do the
analysis, which is the work the run was supposed to do.

**Say what happens if they say nothing.** Every option carries a consequence; make the
default visible.

### When a question turns out to be malformed

If the operator replies asking what something means, or challenges the premise — that is
the question's defect, not theirs. Do not re-pose it with the same framing and more
words. Go read the underlying record, find out what the thing actually is, and ask
again in plain terms. Say in one line that the earlier framing was wrong; do not
elaborate on the mistake.

### Input, not only decisions

The same rule covers anything only the operator can supply: a credential, an endpoint,
a figure that lives outside the graph, a fact about what someone said off-channel. Ask
for it through the tool at the point the run needs it, with what it unblocks stated —
not as a line in a closing list.

For an **operator-only action** — something they must run, not decide — do not pose it
as a question. Give the exact command and what to verify after, at the point the run
reaches it.


## Scope

Normally this stage receives its task set from `/reconcile-tasks` and organizes
exactly that set.

It also runs standalone, because "give me an accurate picture before this meeting"
is a real trigger that should not require driving the whole pipeline:

- **No argument** — the current session's tasks.
- **A session group** — resolve with `mcp__ccd_sidebar__list_groups`, then
  `list_sessions` with **`include_archived: true`**. The default listing hides
  archived sessions and most are archived: a group reported as holding 8 sessions
  held 43 once archived ones were included. Say which you included.
- **Named sessions** — exactly those.

When running standalone, say so in the report, and say that verification was not
inherited — the claims are then only as good as their sources.

## Unattended mode

The questions-tool rule above assumes someone is there to answer. Under a scheduled
run — the 06:06 drain, for instance — nobody is.

When `unattended` is set, do not call the questions tool. **File each decision as a
task instead**, carrying the options, what each implies, what is already settled,
and the recommendation, so the operator can answer it later from the record rather
than from a prompt nobody saw. A scheduled run that silently picks a default is how
an unsettled question becomes an unexamined fact.

## What this skill does not do

It does not close work, mark things done it cannot verify, or send anything. It
reconciles the record with reality; acting on the record is a separate decision.

It does not render the tasks table, grade dispatch readiness, or decide what to
dispatch next — those are `/report-tasks` and `/ready-tasks`, downstream of this.
