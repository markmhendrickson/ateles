# Workflows: the core workflows, each from its own purpose

**Authored companion (not on the review reading list):** binds via the `workflow` entity for each
(project, type) and `execution/scripts/render_workflow_docs.py --check`. Too large to inline under
`MAX_DOC_CHARS` / `MAX_BLOCK_CHARS`; reviewers load the kernel + gates instead. See `conformance.md`.

**Keyed document:** read when a `workflow` declaration, the workflow resolver, the pipeline's step
sequencing, the gating paths, or this document change (`conformance.md`). **Kind:** foundation; states the
design of each core workflow, why its steps exist, and what it hands its tasks to, and never the state of
a checkout. **Derived from:** `work_model.md`, `gates_and_workflows.md`, the `workflow` declarations on the
record for the built workflows (their step lists and fast paths, not their agent names), the agent
policies governing outreach, payment, and people-data, `CLAUDE.md`'s people-data section, and PR #745
operator review (2026-09-04). Which workflows have a declaration on the record, and which are envisioned
only, is `status.md`.

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
stated here and differ in owners and thresholds. Step owners are named by role, never by agent: the roster
binds a role to an agent per project (`swarm_roster`, by role), and a renamed agent leaves no stale name
here. No section names an operator, a payee, a contact, or a channel; those are context entities the step
owner retrieves at runtime.

## How to read a workflow section

Every section has the same shape, so the sections can be compared and so the step tables can be rendered
from the declarations (below).

- **Purpose**: one sentence, the reason the workflow exists.
- **Entry condition**: what makes a task eligible for a passage of this workflow to open. For every
  workflow but intake, the entry condition includes that an intake passage closed naming this workflow as
  successor (`work_model.md#intake-is-every-tasks-first-passage`).
- **Steps**: the ordered list, each with its owner by role, whether it is required, and its parallel group
  and join where it has one. A workflow's last step is always a single step, never a parallel group; its
  sign-off is the passage's closing sign-off and names the successor
  (`gates_and_workflows.md#sequencing-is-data-successors-and-the-chain`). A step's state within a
  passage is open, claimed, or signed, derived from edges (`gates_and_workflows.md`). A sign-off carries a
  verdict; a failing verdict does not advance the passage, and the workflow declares per step which
  earlier step opens again (`on_fail`), the failing sign-off staying in the record as history.
- **Stages**: named groups of contiguous steps, for reading and for reporting where a passage is.
- **Artifacts**: the records in external systems the passage produces or references, attached by edge and
  never the subject of a step (`work_model.md#artifacts-are-records-a-passage-leaves-never-its-subject`).
  Where a workflow's main product is an entity in the record rather than an external record, the section
  says so; an entity in the record is not an artifact.
- **Typical action classes**: the `action_type` values the passage's actions usually carry. Every one is
  evaluated at the execution gate at the moment of execution, whatever the workflow
  (`gates_and_workflows.md#actions-are-entities-only-actions-execute`); the list is what to expect, not
  a bound.
- **Successors**: the workflows the closing sign-off may name, or none. A passage hands its tasks to
  exactly one successor or to none.
- **Fast paths**: the declared skips and the condition that permits each. A condition is a property of the
  task set at intake, never a label on an artifact.

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
| product lens | the `pm` step of every code workflow and every step of intake |
| ux lens, arch lens, pr-review lens, qa lens, legal lens | the review step of the same name (`vocabulary.md#lens`) |
| implementer | the `impl` step |
| steward | the `merge` step, whose work is the merge action (`vocabulary.md#steward`) |
| release steward | every step of the release workflow |
| copywriter | the `copy` step |
| content author | the drafting and posting steps of social content and outreach |
| lint runner | a deterministic step whose work is a script and whose sign-off is the script's result |
| operator-facing agent | every step that carries a `checkpoint_brief` or a task to the operator (`vocabulary.md#operator-facing-agent`) |
| payer, verifier | the two disjoint roles of the payment workflow |
| researcher, analyst | the working steps of research and of meeting processing |

## intake

**Purpose:** turn a created task into a routed one: classified, linked to the records it concerns,
deduplicated, prioritized, and handed to exactly one successor workflow, to none, or to the operator.

**Entry condition:** a task exists in the record and has no intake passage. Creation is publication
(`work_model.md#the-transition-vocabulary`), so every task meets this condition once, at creation, and no
task meets it twice. A task with no intake passage is by that fact unrouted; there is no separate
unrouted state (`work_model.md#intake-is-every-tasks-first-passage`).

**Steps**

<!-- rendered: workflow=*|intake steps -->

| # | Step | Owner (role) | Required | Parallel / join | Closes on |
|---|---|---|---|---|---|
| 1 | `classify` | product lens | yes | | the task's `action_type` declares the classes of action it expects to produce, from what the task does; `assigned_to` is written only where a named principal is the point; `PART_OF` to a parent, or children split out, where the work is an aggregate |
| 2 | `link` | product lens | yes | | every existing external record the task concerns (an issue, a pull request, a thread, a transcript, a page) is attached as an artifact by edge; finding none is a valid close |
| 3 | `dedupe` | product lens | yes | | the task is compared against open tasks; a duplicate closes terminal with an edge to the task it duplicates and this passage ends with no successor |
| 4 | `prioritize` | product lens | yes | | the task's priority is set from the `priority_rubric` entity, retrieved by type, never from the classifier's own sense of urgency |
| 5 | `route` | product lens | yes | | the closing sign-off names one successor workflow, or none, or `operator-only` |

<!-- /rendered -->

The steps exist because each failure they prevent has a name. Without `classify`, blast radius is inferred
from the handling agent at execution time (`gates_and_workflows.md#confidence-and-three-blast-tiers`).
Without `link`, the passage that follows opens a second issue for work that has one. Without `dedupe`, two
passages carry the same change to two pull requests. Without `prioritize`, the claim order is the creation
order. Without `route`, a task reaches a workflow by whichever engine noticed it first.

**Stages:** triage (`classify`, `link`, `dedupe`); disposition (`prioritize`, `route`).

**Artifacts:** none produced. Existing artifacts are attached.

**Typical action classes:** none. Every write intake makes is an internal operational write to the record,
which is not an action (`gates_and_workflows.md#two-policies-workflow-policy-and-execution-policy`).

**Successors:** any workflow in this document except intake; or none; or operator-only, which is a
workflow like the others and is named the same way.

**Fast paths:** `inherits`, for a child task created by a passage that is already routed: `link` and
`dedupe` are skipped and `classify` may copy the parent's declaration, since the parent's intake did that
work; `prioritize` and `route` are never skipped, because a child may need a different workflow from its
siblings (`work_model.md#parent-and-child-tasks`).

## feature

**Purpose:** carry a change that adds or alters behaviour from scoped intent to merged code, reviewed by
every lens the change concerns.

**Entry condition:** intake closed naming `feature`; the task names a repository, which is the domain the
step owners' grants are checked against (`authority_model.md#grants`).

**Steps**

<!-- rendered: workflow=<project>|feature steps -->

| # | Step | Owner (role) | Required | Parallel / join | Closes on |
|---|---|---|---|---|---|
| 1 | `pm` | product lens | yes | | the scope, the acceptance evidence, and the design basis are stated on the task (`conformance.md#design-basis`) |
| 2 | `ux` | ux lens | yes | group `design`, joins `arch` | the change's user-facing behaviour is judged against the stated scope |
| 3 | `arch` | arch lens | yes | group `design`, joins `ux` | the design basis is checked and the change conforms to the cited section, or the citation is found false |
| 4 | `impl` | implementer | yes | | a pull request exists as an artifact of the passage and its CI is green |
| 5 | `pr_review` | pr-review lens | yes | | the full diff is read and judged for correctness |
| 6 | `qa` | qa lens | yes | group `verification`, joins `legal` | the tests can fail on the thing they watch (`principles.md`, invariant 4) |
| 7 | `legal` | legal lens | no | group `verification`, joins `qa` | licensing, data-handling, and disclosure are judged where the change touches them |
| 8 | `merge` | steward | yes | | the merge action executed through the execution gate and the merged pull request is read back; the sign-off names the successor |

<!-- /rendered -->

The two parallel groups exist because their lenses judge independent things and neither needs the
other's verdict; the join is what makes the passage wait for both. `merge` is a step so that the merge
action has a step owner to claim it, an execution to record, and a sign-off to close the passage with;
the action itself is governed by execution policy, not by the step
(`gates_and_workflows.md#two-policies-workflow-policy-and-execution-policy`).

**Stages:** scoping (`pm`); design (`ux`, `arch`); implementation (`impl`); review (`pr_review`, `qa`,
`legal`); integration (`merge`).

**Artifacts:** the issue that carries the specification; the pull request; its CI check runs; the merge
commit.

**Typical action classes:** `build`, `docs`, `git_push`, `open_pr`, `merge_pr`.

**Successors:** `release`; or none, where the project deploys its default branch on its own cadence and
the merge is the last effect the task needs.

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

| # | Step | Owner (role) | Required | Parallel / join | Closes on |
|---|---|---|---|---|---|
| 1 | `pm` | product lens | yes | | the defect is reproduced or the reproduction's absence is stated; the acceptance evidence is the test that goes red without the fix |
| 2 | `impl` | implementer | yes | | a pull request exists with the fix and the red-then-green result recorded in its body |
| 3 | `pr_review` | pr-review lens | yes | | the full diff is read; the fix addresses the cause, not the symptom |
| 4 | `qa` | qa lens | yes | | the test fails on the reverted fix (`principles.md`, invariant 4) |
| 5 | `merge` | steward | yes | | the merge action executed through the execution gate and read back; the sign-off names the successor |

<!-- /rendered -->

There is no `ux` or `arch` step because a fix restores stated behaviour and does not choose new
behaviour; a bug whose fix requires a design choice is re-routed to `feature` by ending this passage
without a successor and opening a new intake, not by adding steps here.

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

| # | Step | Owner (role) | Required | Parallel / join | Closes on |
|---|---|---|---|---|---|
| 1 | `pm` | product lens | yes | | the affected surface and the fix's scope are stated in the record; the public artifacts carry no exploit detail |
| 2 | `impl` | implementer | yes | | a pull request exists with the fix; its body describes the change, not the exploit |
| 3 | `pr_review` | pr-review lens | yes | | the full diff is read and the fix is judged complete for the stated surface |
| 4 | `merge` | steward | yes | | the merge action executed through the execution gate and read back; the sign-off names `release` |

<!-- /rendered -->

The review steps that are absent (`ux`, `qa`, `legal`) are absent for speed, and the workflow compensates
by never closing without a successor: a security fix that is merged and not released is not fixed, and
the closing sign-off's only permitted successor is `release`. Disclosure hygiene is a property of the
artifacts, checked at `pm` and `pr_review`, because the pull request and the release are public records.

**Stages:** scoping (`pm`); implementation (`impl`); review (`pr_review`); integration (`merge`).

**Artifacts:** the issue (private where the system allows); the pull request; the merge commit.

**Typical action classes:** `build`, `git_push`, `open_pr`, `merge_pr`.

**Successors:** `release`, always.

**Fast paths:** none.

## copy

**Purpose:** carry a change to the words a product shows, in a site, a document, or a message template,
through a copy-specific review to merged content.

**Entry condition:** intake closed naming `copy`; the task names the surface whose words change and the
repository that holds it.

**Steps**

<!-- rendered: workflow=<project>|copy steps -->

| # | Step | Owner (role) | Required | Parallel / join | Closes on |
|---|---|---|---|---|---|
| 1 | `pm` | product lens | yes | | the surface, the audience, and the intent of the change are stated |
| 2 | `copy` | copywriter | yes | | the words are written against the `brand_voice` entity, retrieved by type, and stored on the task |
| 3 | `ux` | ux lens | no | | layout consequences of the new words are judged; nothing about the words themselves |
| 4 | `impl` | implementer | yes | | a pull request carries the words into the surface |
| 5 | `pr_review` | pr-review lens | yes | | the diff changes only the words the task names |
| 6 | `legal` | legal lens | no | | claims, comparisons, and regulated wording are judged where the copy makes them |
| 7 | `merge` | steward | yes | | the merge action executed through the execution gate and read back; the sign-off names the successor |

<!-- /rendered -->

`copy` precedes `impl` because the words are the deliverable and the implementation is their carriage;
`ux` is optional and scoped to layout so that the copy step's verdict on the words is not re-litigated by
a second lens. There is no `arch` step: copy changes no behaviour.

**Stages:** scoping (`pm`); authoring (`copy`, `ux`); implementation (`impl`); review (`pr_review`,
`legal`); integration (`merge`).

**Artifacts:** the pull request; the merge commit; the published surface once released.

**Typical action classes:** `docs`, `git_push`, `open_pr`, `merge_pr`.

**Successors:** `release`; or none, as for feature.

**Fast paths:** none.

## social content

**Purpose:** produce share material for a piece of content across the platforms it targets and post it,
with the operator's consent recorded through the execution gate before anything is public.

**Entry condition:** intake closed naming `social_content`; the task names the content being shared and
the target platforms, from the `channel_config` entity retrieved by type.

**Steps**

<!-- rendered: workflow=<project>|social_content steps -->

| # | Step | Owner (role) | Required | Parallel / join | Closes on |
|---|---|---|---|---|---|
| 1 | `draft` | content author | yes | | complete drafts exist for every targeted platform, stored in the record, against the `brand_voice` entity |
| 2 | `draft_lint` | lint runner | yes | on fail: `draft` | the deterministic checks pass: no relative-time anchors, a substance floor per platform, no near-duplicate text across platforms, every targeted platform present |
| 3 | `operator_preview` | operator-facing agent | yes | on fail: `draft` | the `checkpoint_brief` on the `publish` action, carrying the drafts inline, is resolved by the operator; feedback reopens `draft` |
| 4 | `post` | content author | yes | | the `publish` action executed through the execution gate on each platform and the posts read back; the sign-off closes the passage |

<!-- /rendered -->

`draft_lint` sits before the operator sees anything so that the operator's attention is spent on
judgment, not on defects a script can find. `operator_preview` is not a second consent gate: the `publish`
action is created when the drafts pass lint, the execution gate evaluates it, and the step is where the
operator-facing agent carries the gate's `checkpoint_brief` and records the decision
(`gates_and_workflows.md#the-execution-gate-is-pr-independent`, principle 6).

**Stages:** authoring (`draft`, `draft_lint`); consent (`operator_preview`); publication (`post`).

**Artifacts:** the posts on each platform, each attached by edge with its platform identifier.

**Typical action classes:** `publish`.

**Successors:** none.

**Fast paths:** `approved` skips `operator_preview`, permitted only when the execution gate would not
checkpoint the `publish` action: a recurring series for the class has graduated under the
`execution_policy`, or the operator's standing approval for the content is already recorded on the task.
The fast path never bypasses the gate; it skips the step that would have carried a brief the gate would
not have written.

## release

**Purpose:** cut and ship a release from merged code, verify it is the code that was reviewed, and
confirm it reached the deployed checkout.

**Entry condition:** intake closed naming `release`, or a code passage closed naming `release` as its
successor. Several tasks whose code passages closed since the last release are normally aggregated into
one release passage (`work_model.md#what-passes-through-a-workflow-is-a-passage-of-tasks`).

**Steps**

<!-- rendered: workflow=<project>|release steps -->

| # | Step | Owner (role) | Required | Parallel / join | Closes on |
|---|---|---|---|---|---|
| 1 | `criteria` | release steward | yes | | every criterion in the project's `release_criteria` entity, retrieved by type, is read and holds; a criterion that cannot be read is `unknown`, and unknown holds the passage (`principles.md`, invariant 7) |
| 2 | `release` | release steward | yes | on fail: `criteria` | the `release` action executed through the execution gate; the tag, package, or deployment is read back at its terminal status (`principles.md`, invariant 2) |
| 3 | `verify_deployed` | release steward | yes | on fail: `release` | the deployed checkout reports the released version; the sign-off closes the passage |

<!-- /rendered -->

`verify_deployed` is a separate step because "released" and "landed" are different claims
(`principles.md`, invariant 10), and a passage that closed on the release action's success would record
the first as the second. `criteria` is separate from `release` so that the read of the criteria and the
execution of the release are two sign-offs, and a release executed against criteria nobody read is
visible as a passage missing one.

**Stages:** readiness (`criteria`); shipping (`release`, `verify_deployed`).

**Artifacts:** the tag; the package or image; the release notes; the deployment record.

**Typical action classes:** `release`, `git_push`, `external_api_write`.

**Successors:** none.

**Fast paths:** none. A release that skips its criteria is the failure the workflow exists to prevent.

## outreach

**Purpose:** compose and send a message to a party outside the swarm, with the message reviewed, the send
consented to through the execution gate, and the follow-up owned.

**Entry condition:** intake closed naming `outreach`; the task names the recipient by reference to a
`contact` entity and the purpose of the message; the recipient's history with the operator is retrieved
from the record and the mail archive before anything is drafted, never assumed.

**Steps**

<!-- rendered: workflow=<project>|outreach steps -->

| # | Step | Owner (role) | Required | Parallel / join | Closes on |
|---|---|---|---|---|---|
| 1 | `draft` | content author | yes | | a draft exists in the record, written against the `brand_voice` entity and the operator's voice guidance, every factual claim in it traced to a source the author read |
| 2 | `review` | pr-review lens | yes | on fail: `draft` | the draft is judged for facts, voice, scope (it answers what was asked and nothing else), and for what it discloses |
| 3 | `consent` | operator-facing agent | yes | on fail: `draft` | the `checkpoint_brief` on the `send_external_comms` action, carrying the full draft, is resolved by the operator |
| 4 | `send` | content author | yes | | the `send_external_comms` action executed through the execution gate and the sent message read back from the mail system, never inferred from the send call's return |
| 5 | `follow_up` | content author | no | | a reply is linked as an artifact, or the declared follow-up interval passes and one follow-up was sent through the same gate, or the operator ends the follow-up; the sign-off closes the passage |

<!-- /rendered -->

`review` precedes `consent` so that the operator sees a draft that has already been checked, and the
draft the operator consents to is the draft that is sent: `send` executes the reviewed content by its
dedup key, and any change after consent is a new `draft`. A staged draft is never modified in place in
the mail system, because on some systems an update is a send; the design's staging is the draft in the
record. `follow_up` is a step of the same passage so that an unanswered message has an owner until the
passage closes, and so that a follow-up passes the same gate as the first message.

**Stages:** composition (`draft`, `review`); consent (`consent`); delivery (`send`, `follow_up`).

**Artifacts:** the sent message; the thread; any reply.

**Typical action classes:** `send_external_comms`.

**Successors:** none. A reply that needs work of another kind is a new task, created at `follow_up` and
routed by its own intake.

**Fast paths:** none. Every outward message passes the gate.

## payment

**Purpose:** move money to a payee for an obligation the record holds, with the payee and amount verified
by a second principal against the payment profile before the operator consents and before anything is
executed.

**Entry condition:** intake closed naming `payment`; the task references the obligation (an invoice, a
recurring fee, a wage) and the `payment_profile` entity, retrieved by type, that names the payee, the
rail, and the constraints. A task that names a payee or an amount inline, rather than by reference to a
profile, fails `classify` at intake.

**Steps**

<!-- rendered: workflow=<project>|payment steps -->

| # | Step | Owner (role) | Required | Parallel / join | Closes on |
|---|---|---|---|---|---|
| 1 | `prepare` | payer | yes | | the payee, amount, currency, rail, and reference are assembled from the profile and the obligation and stored on the task; the profile's constraints (no memo, attendance gate, cadence) are applied |
| 2 | `verify` | verifier | yes | on fail: `prepare` | payee and amount match the profile and the obligation; the verifier is a principal disjoint from the payer (`authority_model.md#structural-checks-quorum-and-separation-of-duties`) |
| 3 | `checkpoint` | operator-facing agent | yes | on fail: `prepare` | the `checkpoint_brief` on the `payment` action, carrying payee, amount, and reference, is resolved by the operator |
| 4 | `execute` | payer | yes | | the `payment` action executed through the execution gate, keyed on its dedup key so a re-claim never pays twice (`work_model.md#at-least-once-implies-effect-dedup`) |
| 5 | `reconcile` | verifier | yes | on fail: `execute` | the transfer is read back from the rail at its terminal status, matched to the obligation, and recorded as a `transaction` entity; the sign-off closes the passage |

<!-- /rendered -->

`verify` and `reconcile` belong to a principal other than the payer so that one principal never both
proposes and confirms a movement of money; this is the smallest separation of duties the authority model
names, applied to the workflow where it matters most. `reconcile` exists because a rail's acceptance of a
transfer is not its settlement; a payment whose reconcile step never signed is visible as a passage
missing one.

**Stages:** preparation (`prepare`, `verify`); consent (`checkpoint`); settlement (`execute`,
`reconcile`).

**Artifacts:** the transfer record at the rail; the receipt or confirmation message. The `transaction`
entity is a record in the record, not an artifact.

**Typical action classes:** `payment`, `transfer`.

**Successors:** none. A confirmation message to the payee is an outreach task, created at `reconcile`
and routed by its own intake.

**Fast paths:** none. A recurring payment graduates under the `execution_policy`'s recurrence rule at the
gate, which changes whether `checkpoint` carries a brief, not whether the step exists.

## research and analysis

**Purpose:** answer a stated question from sources, with the answer persisted as an entity whose claims
trace to what was read, and delivered to whoever asked.

**Entry condition:** intake closed naming `research`; the task states the question, the scope, and the
sources permitted; a task that states a conclusion to confirm rather than a question to answer fails
`classify`.

**Steps**

<!-- rendered: workflow=<project>|research steps -->

| # | Step | Owner (role) | Required | Parallel / join | Closes on |
|---|---|---|---|---|---|
| 1 | `brief` | product lens | yes | | the question, the scope, the permitted sources, the audience, and the form of the deliverable are stated on the task |
| 2 | `gather` | researcher | yes | | every source read is recorded with its provenance; a source that could not be read is recorded as unread, not omitted |
| 3 | `synthesize` | researcher | yes | on fail: `gather` | the analysis is written with each claim traced to a gathered source; a claim with no source is marked as the author's |
| 4 | `persist` | researcher | yes | | an `analysis` entity holds the full body and is read back; the task refers to it |
| 5 | `deliver` | researcher | yes | | the analysis reaches its audience in the briefed form: a rendered page, a message, or nothing beyond the entity; the sign-off closes the passage |

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

| # | Step | Owner (role) | Required | Parallel / join | Closes on |
|---|---|---|---|---|---|
| 1 | `ingest` | analyst | yes | | the transcript is in the record as a source with provenance, linked to the calendar event where one is found |
| 2 | `summarize` | analyst | yes | | a `meeting_analysis` entity holds the summary, the decisions, and the open questions |
| 3 | `extract` | analyst | yes | on fail: `summarize` | the action items, commitments, and participants are extracted; each participant is a `contact` entity holding what serves the relationship and nothing incidental or sensitive (RGPD Art. 9 categories are summarized or omitted, never transcribed) |
| 4 | `persist` | analyst | yes | | every extracted task is created in the record, each opening its own intake passage; every entity is read back; the sign-off closes the passage |
| 5 | `deliver` | analyst | no | | a recap per participant is drafted as an outreach task, where the brief asked for one; never sent from this passage |

<!-- /rendered -->

`extract` carries the people-data rule as its closing condition because the extraction is where a
transcript's incidental disclosures would otherwise become durable profile fields. `deliver` is optional
and creates tasks rather than sending, so that no recap reaches a participant without passing the
outreach workflow's review and consent.

**Stages:** intake of the record (`ingest`); analysis (`summarize`, `extract`); output (`persist`,
`deliver`).

**Artifacts:** the transcript file; the calendar event. The `meeting_analysis`, `contact`, and `task`
entities are in the record.

**Typical action classes:** none. Every outward effect is a task for another workflow.

**Successors:** `outreach`, per recap task; otherwise none.

**Fast paths:** none.

## operator-only

**Purpose:** carry a task the swarm structurally cannot complete to the operator, hold it while the
operator decides or acts, and record the outcome, so that operator-only work is on the task path and
visible rather than a notification nobody owns.

**Entry condition:** intake closed naming `operator-only`, or the task's declared action classes include
`operator_only`. The operator-facing agent is the only eligible claimant
(`work_model.md#operator-only-tasks-are-claimed-by-the-operator-facing-agent`).

**Steps**

<!-- rendered: workflow=*|operator_only steps -->

| # | Step | Owner (role) | Required | Parallel / join | Closes on |
|---|---|---|---|---|---|
| 1 | `present` | operator-facing agent | yes | | the task, its context, and the exact operator action it needs are carried to the operator through the channel the `channel_config` entity names; where an action exists, its `checkpoint_brief` is what is carried |
| 2 | `await` | operator-facing agent | yes | | the operator's decision or the operator's report of the action taken is recorded; the lease is renewed throughout; the deferral is bounded and its exhaustion escalates (`failure_posture.md#the-rules`, rule 5) |
| 3 | `record` | operator-facing agent | yes | | the outcome is written on the task and read back; the sign-off closes the passage and names the successor the outcome calls for |

<!-- /rendered -->

The workflow has three steps rather than one so that "presented and awaiting" is a readable state of the
passage and not a notification's delivery status, and so that a task the operator never answers is
visible as a passage whose `await` step has been open past its bound.

**Stages:** handover (`present`); decision (`await`, `record`).

**Artifacts:** whatever record the operator's action left, attached when the operator reports it.

**Typical action classes:** `operator_only`. The class resolves to `NEVER` ahead of any policy; nothing
in this passage executes without the operator (`gates_and_workflows.md#confidence-and-three-blast-tiers`).

**Successors:** whichever workflow the operator's decision calls for, or none. A task the operator
completed by hand closes with none; a task the operator redirected is routed by this passage's closing
sign-off, not by a new intake, because the classification did not change.

**Fast paths:** none.

## What no workflow in this document does

None opens a passage for a task that has no intake passage, except intake itself. None names two
successors. None takes a step on an issue or a pull request rather than on the passage's tasks. None
executes an outward effect outside the execution gate, and none carries a second consent gate beside it.
None names an agent, an operator, a payee, a contact, or a channel; each is resolved from a context
entity at runtime. Each absence is an invariant of `work_model.md` or `gates_and_workflows.md`, and a
workflow that needs one of them is a change to the foundation, made through a PR that says so
(`conformance.md`).
