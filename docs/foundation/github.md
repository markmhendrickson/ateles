# GitHub: the code host's full event surface, mapped to the work model

**Keyed document:** read when the GitHub receiver, the engine that sequences from it, the triage or
summarising roles, or this document changes (`conformance.md`). **Kind:** foundation; states the design and
never the state of a checkout. **Derived from:** `adapters.md` (the two invariants, the four outcomes, and
the five adapter rules, which this document applies and does not restate), `work_model.md` (artifacts,
intake, the four execution mechanisms), `gates_and_workflows.md` (step state from edges; actions and the
action gate; the three verdict values), `workflows.md` (the code workflows, release, and security),
`failure_posture.md` (the halt, the recovery per action class, the checkpoint reason classes), and GitHub's
own webhook event and payload documentation, read 2026-09-04, and the operator's 2026-09-05 terminology review (revision 17: the one boundary and the term `external system`, the `action series` rename, `subject` defined, and the two-part `checkpoint`), and the operator's 2026-09-05 review of review relevance (revision 19: the `applies_when` condition on an optional step, and two terms retired in favour of `review step`). What is built, and which rows have no code
path, is `status.md`. Revised by the testability pass of 2026-09-06 (revision 37: the host's assignment never writes `assigned_to`; `impl` closes on a mergeable pull request, stated in `workflows.md`). Revised by the host-configuration pass of 2026-09-06 (revision 65: what the host must be configured to be, ruled an extension of obligations 1 and 6 rather than a seventh obligation — the subscription reconciliation obligation 1's drop counter structurally cannot perform, and the host's merge-permitting configuration as the standing form of the permit the gate never issued; the required state per repository, and the reporting-permission row that belongs to neither obligation; open decision 69, whether a difference at admission blocks the grant or is a finding on the review step).

## Purpose

Be the GitHub adapter in full: every event the code host can deliver, mapped to exactly one of the four
outcomes `adapters.md` defines or to `dropped` with a reason; every outbound operation a step takes on the
host, with its action class and what confirms it landed. `adapters.md` keeps the general adapter rules and
carries a pointer here; those rules are cited from this document and never restated in it (principle 9,
one home).

Completeness is the point rather than a courtesy. A table of what someone thought of reproduces the failure
it is written against: the incidents behind this document were not events handled badly, they were events
with **no defined response** — a draft pull request marked ready with no inbound mapping, a base retargeting
that left a required check green from a branch nobody reviewed against, issue deliveries discarded at a
debug log level behind a label test. Each looked like an adapter with nothing to do. So this document
enumerates from the host's own event list rather than from the receiver's, and marks each row **handled**,
**deliberately ignored**, or **unhandled** — the third being a gap, which is a `status.md` row and not a
silent omission here.

## Scope

Every event GitHub can deliver about the artifact classes the work model names, plus the repository-level
events the swarm's own operations produce or depend on. In scope: the inbound mapping per event and action,
the outbound operation per step, and the two properties that make the mapping auditable. Out of scope: the
general adapter rules (`adapters.md`), the workflows whose steps take these operations (`workflows.md`),
the gate's decision function (`gates_and_workflows.md`), what the adapter is granted
(`authority_model.md#grants`), and the per-instance binding of a repository to a declaration scope, which is a
`vendor_binding` context entity resolved at runtime and never named here.

Events outside those classes — the host's organization, membership, sponsorship, marketplace, project
board, discussion, wiki, star, watch, and fork surfaces — are enumerated under *Everything else the host
can deliver*, as one class with one disposition, rather than omitted.

## The property that makes this a control and not a list

A hand-written table is a list of what its author thought of, and its omissions are invisible in it. What
makes an omission here **observable** is a property that holds over deliveries rather than over rows:

**Every delivery resolves to one of the four outcomes or to `dropped` with a reason; the disposition is
counted per window and surfaced on the off-record announcement path.**

That is `adapters.md`'s disposition rule, and this document depends on it rather than repeating its
reasoning. The consequence worth stating here is what it does for *this* document: an event type absent
from the tables below is not silently unhandled. It arrives, matches no mapping, and resolves to `dropped`
with the reason that it matched none — which is counted, aggregated into the window's announcement, and
therefore **readable as a number that should be zero**. A rising count of drops with reason `unmapped` is
the signature of an event class this document does not cover, and it appears without anyone having thought
to look for it.

This is what makes the table auditable against reality rather than a promise about the author's diligence.
Two corollaries. A row marked **unhandled** below is a gap the design has *named*, and it is honest about
its own cost: until it is built, that event's deliveries resolve to `dropped` and are counted, so the gap
is measurable rather than theoretical. And a row marked **deliberately ignored** is not the same thing as
an absent row — it resolves to an outcome or to a drop with a *stated* reason, so the ignoring is a
decision on the record rather than an omission that looks like one.

## What the outcomes are, and the rule against a fifth

Every inbound row below resolves to exactly one of the four outcomes `adapters.md` names — **a sign-off by
a named principal**, **an observation on an artifact**, **an action confirmation**, or **a task for
intake** — or to **`dropped`** with a reason. The vocabulary is closed on purpose: an adapter that may
invent a fifth outcome has become an engine, deciding what an event *means* rather than what it *is*.

Where an event genuinely appears to need a fifth, that is a **finding** and it is named as one rather than
resolved by inventing an outcome. Two arose in the enumeration below, and both are recorded as findings
with an existing mechanism doing the work instead:

- **A derived condition is not an event and has no outcome.** A pull request's mergeability is not
  delivered; it is a field the host computes and a reader polls for. It cannot be an inbound row because
  nothing arrives. Rather than adding an outcome for "a condition changed", the design keeps it where
  conditions already live: an observation on the artifact, written by a **read** the adapter makes, with
  the sourcing and coverage every observation carries. See *Conditions that are not events*.
- **An event that invalidates a decision already made** — a force-update of the head after a review step signed, a
  review dismissed after a step closed — looks like it needs an outcome that *retracts* something. It does
  not get one. It is an observation, and the retraction is already the record's: a sign-off is pinned to
  the artifact state it judged, so an observation moving the head makes the pinned sign-off readable as
  stale by a derived read (`data_model.md#record-conventions`), and no adapter unsigns anything. See *The
  transitions the mining found unhandled*.

## Issues

The `issues` event carries twenty-one actions and `issue_comment` five; `sub_issues` carries four. The
issue is an artifact of kind `issue`, found by `system` and `external_id`.

| Event and action | Status | Outcome in the record |
|---|---|---|
| `issues.opened`, by a person | handled | a task with the issue as its artifact (`REFERS_TO`), entering intake; the opener's credential is recorded, resolved to a principal where one binds |
| `issues.opened`, by the swarm's own account | handled | an action confirmation on the batch's `open_issue`-class action; the issue is `PRODUCES` from the batch |
| `issues.edited` | handled | an observation on the artifact (title, body); the `changes` object says which field moved |
| `issues.closed` by a person | handled | an observation (`state: closed`); the task's status is written by the batch's sign-offs, never by the host |
| `issues.closed` by the host on a merge | handled | an observation; the merge confirmation already covers the effect |
| `issues.reopened` | handled | an observation (`state: open`); it opens no step and re-enters no workflow — a task needing further work is created and enters its own intake |
| `issues.assigned`, `issues.unassigned` | handled | an observation on the artifact; the host's assignment is never written to `assigned_to`, which is a principal's write in the record — at intake's `classify`, or by a principal later — and never an adapter's (`work_model.md#assignment-restricts-eligibility-it-never-creates-a-lease`) |
| `issues.labeled`, `issues.unlabeled` | handled | an observation on `labels[]`; **a label naming a step is not that step's state**, and no label opens, claims, or closes anything |
| `issues.milestoned`, `issues.demilestoned` | handled | an observation on the artifact |
| `issues.locked`, `issues.unlocked` | handled | an observation on the artifact |
| `issues.pinned`, `issues.unpinned` | deliberately ignored | `dropped`, reason `presentation_only`: pinning orders the host's own issue list and says nothing about the work |
| `issues.typed`, `issues.untyped` | handled | an observation on the artifact; the host's issue type is the host's classification and never the task's, which intake's `classify` writes |
| `issues.transferred` | **unhandled** | the artifact's `external_id` and repository both change, and the record's `system`/`external_id` pair no longer resolves. Until built, `dropped` with reason `identity_moved`. See *The transitions the mining found unhandled* |
| `issues.deleted` | **unhandled** | the artifact ceases to exist at the host while the record still refers to it. Until built, `dropped` with reason `artifact_deleted` |
| `issues.field_added`, `issues.field_removed` | deliberately ignored | `dropped`, reason `host_project_field`: these are the host's project-field surface, which the record does not mirror |
| `issue_comment.created` | handled | an observation on the artifact; a comment **from the step owner of an open step, carrying a verdict in the declared form**, may be that step owner's sign-off, by the identity rule (`adapters.md#what-the-adapter-does-with-every-event`) |
| `issue_comment.edited`, `issue_comment.deleted` | handled | an observation; an edited or deleted comment **never revises a sign-off already written** — a verdict is terminal and a new judgement is a new sign-off (`gates_and_workflows.md#findings-verdicts-and-what-a-blocking-finding-obliges`) |
| `issue_comment.pinned`, `issue_comment.unpinned` | deliberately ignored | `dropped`, reason `presentation_only` |
| `sub_issues.sub_issue_added`, `sub_issues.sub_issue_removed` | **unhandled** | the host's sub-issue edge is a second parent/child structure beside the record's `PART_OF`, and which is authoritative is undecided. Until decided, an observation on the artifact and never a `PART_OF` write. See `status.md` |
| `sub_issues.parent_issue_added`, `sub_issues.parent_issue_removed` | **unhandled** | the same, from the child's side |
| `issue_dependencies.*` (four actions) | deliberately ignored | `dropped`, reason `host_dependency_graph`: the record derives blocking from its own edges, and a second dependency graph the host maintains would be a second source of truth (principle 9) |
| `milestone.*`, `label.*` (repository-level definitions) | deliberately ignored | `dropped`, reason `host_taxonomy_definition`: these define the host's labels and milestones rather than saying anything about a tracked artifact |

**A duplicate marked at the host is not the record's dedupe.** GitHub closes an issue as a duplicate, which
arrives as `issues.closed` carrying that reason. It is an observation, and it is the third row of the
table above: the record's duplicate determination is intake's `dedupe` step, whose close writes the
terminal status and the edge to the task it duplicates (`workflows.md#intake`). Reading the host's reason
as the record's verdict would let an external actor close a task by closing an issue, which the second
invariant forbids.

## Pull requests

The `pull_request` event carries twenty-three actions. The pull request is an artifact of kind
`pull_request`.

| Event and action | Status | Outcome in the record |
|---|---|---|
| `pull_request.opened` | handled | the artifact is linked to the batch whose tasks it addresses; the implementer's sign-off on `impl` cites it in `artifact_refs[]`. A pull request naming no batch is an artifact with no batch and yields a task for intake |
| `pull_request.synchronize` | handled | an observation on `head`. Open sign-offs are unaffected; a workflow wanting review to open again on a new head declares that on the step. The **pinned-head** consequence is below |
| `pull_request.edited` | handled | an observation on title, body, or **base**. A base change is the retargeting case and carries its own rule, below |
| `pull_request.ready_for_review` | handled | an observation on the artifact's draft state, and **the condition the `impl` step owner reads before signing**. It opens no step: a draft marked ready is the implementer saying the artifact is judgeable, and the judgement is still a sign-off |
| `pull_request.converted_to_draft` | handled | an observation on draft state; sign-offs already written stand, pinned to the head they judged |
| `pull_request.closed`, unmerged | handled | an observation (`state: closed`); the batch's open step stays open until a principal signs it |
| `pull_request.closed`, merged | handled | an action confirmation on the batch's `merge_pr`-class action (`taken_at`, `result_ref` naming the merge commit); the merge commit is an artifact `PRODUCES` from the batch. **A merge the record has no action for is an observation and a defect to surface**, never a confirmation |
| `pull_request.reopened` | handled | an observation (`state: open`) |
| `pull_request.assigned`, `pull_request.unassigned` | handled | an observation; the host's assignment is not the step owner |
| `pull_request.review_requested`, `pull_request.review_request_removed` | handled | an observation; **the step owner claims its review step on its own loop, never because the host asked** |
| `pull_request.labeled`, `pull_request.unlabeled` | handled | an observation on `labels[]`; no label is step state |
| `pull_request.milestoned`, `pull_request.demilestoned` | handled | an observation |
| `pull_request.locked`, `pull_request.unlocked` | handled | an observation |
| `pull_request.auto_merge_enabled` | **unhandled** | the host is armed to merge without the `merge` step's action passing the action gate — a permit granted outside the gate. Until built, an observation on the artifact **and a condition the steward reads as blocking**. See below |
| `pull_request.auto_merge_disabled` | **unhandled** | the same, disarmed |
| `pull_request.enqueued`, `pull_request.dequeued` | **unhandled** | the host's merge queue is a second sequencer over the same merge. Until decided, an observation. See `status.md` |
| `pull_request.stacked` | **unhandled** | a stacked pull request's base is another pull request rather than a branch, so "the change this batch shipped" is not the diff against the default branch. Until decided, an observation |

**Auto-merge is the clearest case of the host holding a permit the gate did not issue.** A merge is an
action, evaluated at the action gate at the moment it would be taken
(`gates_and_workflows.md#actions-are-entities-only-actions-are-taken`). Auto-merge inverts that: the host
is instructed in advance to take the merge whenever conditions it evaluates come true, so the effect
crosses the boundary with no action entity, no gate evaluation, and no confirmation written back — and the
merge then arrives as a `pull_request.closed`-merged event the record has no action for, which the table
above already classes as a defect to surface. The design's position is that the swarm never enables
auto-merge, and that an auto-merge enabled by a person is a condition the steward reads as blocking rather
than a convenience. It is marked unhandled because reading it as blocking is a rule with no built path.

## Reviews, review comments, and threads

| Event and action | Status | Outcome in the record |
|---|---|---|
| `pull_request_review.submitted`, `APPROVE`, by a review step's principal on that step of the batch linked to this pull request | handled | **that step owner's sign-off on its review step**, verdict `signed`. The host's token is a signal the adapter maps to one of the record's three verdict values (`gates_and_workflows.md#findings-verdicts-and-what-a-blocking-finding-obliges`) |
| `pull_request_review.submitted`, `REQUEST_CHANGES`, by a review step's principal | handled | that step owner's sign-off with a blocking verdict; the step's `on_fail` names the earlier step that opens again |
| `pull_request_review.submitted`, `COMMENT`, by anyone | handled | an observation only; no sign-off |
| `pull_request_review.submitted` by a credential binding to no principal, or to a principal owning no open step | handled | an observation. **An automated account's `APPROVE` never stands in for a review step's owner** |
| `pull_request_review.submitted`, `APPROVE`, by the operator's credential while a checkpoint on this batch's merge action awaits the operator | handled | resolution of that checkpoint by the operator principal (`authority_model.md#approval`), recorded and read back; **not** a sign-off |
| `pull_request_review.dismissed` | **unhandled** | the host retracts a review after a step may already be signed. Until built, an observation — and never an unsigning, because no adapter revises a sign-off. See below |
| `pull_request_review.edited` | handled | an observation; an edited review body never revises a sign-off already written |
| `pull_request_review_comment.created` | handled | an observation on the artifact; a line comment is a remark and carries no verdict |
| `pull_request_review_comment.edited`, `.deleted` | handled | an observation |
| `pull_request_review_thread.resolved`, `.unresolved` | handled | an observation on the artifact. **A resolved thread is not a satisfied finding**: findings live on the sign-off and bind by their severity, and a thread resolved at the host closes nothing |

**A dismissed review does not unsign a step.** The record's rule is already sufficient and this row applies
rather than extends it: a verdict is terminal and never revised in place, and a step owner reaching a
different judgement writes a **new** sign-off, the latest per step owner per artifact head being the one
that stands (`gates_and_workflows.md#findings-verdicts-and-what-a-blocking-finding-obliges`). So a dismissal
at the host is an observation, and if the step owner genuinely no longer stands behind its verdict, that owner
writes a new sign-off. What is unhandled is the **surfacing**: a dismissal that silently leaves a signed
step signed is exactly the state a reader should be told about, and the design's answer is that the
observation is a condition the steward reads before taking the merge. That is a rule with no built path,
so it is a `status.md` row.

## Releases and tags

| Event and action | Status | Outcome in the record |
|---|---|---|
| `release.published` | handled | an action confirmation on the release batch's `release`-class action; the release is `PRODUCES` from that batch. **`verify_deployed` still reads the deployed checkout** (`workflows.md#release`) — published is not landed |
| `release.released` | handled | an observation on the artifact's state; it is the host's transition out of prerelease and confirms no action of its own |
| `release.prereleased` | handled | an action confirmation where the batch's action was a prerelease; an observation otherwise |
| `release.created` | handled | an observation; a draft release exists and nothing has shipped |
| `release.edited` | handled | an observation on the release's notes or metadata |
| `release.deleted` | handled | an observation (`state: deleted`). It is **never** read as the recovery having happened: a recovery is its own action through the gate (`failure_posture.md#the-operator-invoked-halt-and-what-undoes-an-action-already-taken`), and a release deleted by a person is an effect the record did not intend |
| `release.unpublished` | handled | an observation. The `publish` class's recovery is **deprecate and supersede**, forward-only; an unpublish observed here is recorded and never treated as a clean reversal |
| `create` (branch or tag created) | handled | an observation on the artifact that refers to the ref, where one exists; otherwise `dropped`, reason `untracked_ref` |
| `delete` (branch or tag deleted) | handled | the same. A tag deletion where the record holds a `retag_release` action is that action's confirmation, in its first half; the confirmation is complete only when the tag resolves to the intended commit |

## Security advisories, and what the adapter does not surface

This class carries a disclosure cost the others do not, and the rule is a **narrowing** of the default
mapping rather than an addition to it. The reason is concrete: security release candidates disclosed
exploit detail before the fix was upgrade-available, across consecutive releases, because the material
that reached a public surface was the material the record happened to hold.

`workflows.md#security` already states the standing rule from the workflow's side: the public artifacts
carry no exploit detail, checked at `pm` and at `pr_review`, because the pull request and the release are
public records. This document states it from the boundary's side, where the failure actually occurred.

| Event and action | Status | Outcome in the record |
|---|---|---|
| `security_advisory.published`, `.updated`, `.withdrawn` | handled, **narrowed** | a task for intake, carrying the advisory's **identifier, affected package, affected version range, and fixed version** — and not its description, proof of concept, or reproduction steps. The advisory is an artifact; the narrowing is on what the adapter **writes**, not on what it reads |
| `repository_advisory.published` | handled, **narrowed** | an observation on the artifact, the same fields and the same exclusion. A repository advisory the swarm itself drafted is `PRODUCES` from the security batch |
| `repository_advisory.reported` | handled, **narrowed** | a task for intake, carrying the identifier and the affected surface only. **This is the row where the cost is highest**: a report arrives before any fix exists, so everything the adapter writes about it is written at the moment disclosure is most damaging |
| `dependabot_alert.created`, `.reopened`, `.reintroduced` | handled, narrowed | a task for intake with the alert as its artifact: package, version range, and severity. Not the advisory prose |
| `dependabot_alert.fixed`, `.auto_dismissed`, `.dismissed`, `.auto_reopened`, `.assignees_changed` | handled | an observation on the alert artifact |
| `secret_scanning_alert.created`, `.validated`, `.publicly_leaked` | handled, **narrowed hardest** | a task for intake naming the **secret type and location** and never the secret's value, nor any span of the matched text. `.validated` and `.publicly_leaked` raise the task's priority; neither adds detail |
| `secret_scanning_alert.resolved`, `.reopened`, `.assigned`, `.unassigned`, `.metadata_created`, `.metadata_removed` | handled | an observation on the alert artifact |
| `secret_scanning_alert_location`, `secret_scanning_scan` | deliberately ignored | `dropped`, reason `redundant_detail`: the location surface repeats what the alert already carries at a finer grain than the record needs, and each additional copy is another place the value could leak |
| `code_scanning_alert.*` (seven actions) | handled, narrowed | a task for intake on `created`; an observation otherwise. Rule, location, and severity; not the alert's data-flow trace |
| `repository_vulnerability_alert.*` (four actions) | handled, narrowed | the same as `dependabot_alert`, which supersedes this older surface |

**What the adapter does not surface, stated plainly.** Three things, and the reason each is a boundary rule
rather than a workflow rule:

1. **Exploit detail never enters the record from an inbound advisory event.** Not the description, not a
   proof of concept, not reproduction steps, not a matched secret's value. The workflow's rule governs
   what a *person or agent writes* onto a public artifact; this rule governs what the *adapter writes at
   all*, and it is stricter because it is the earlier of the two. Detail the record never held cannot be
   copied out of it by a later step, a summary, a digest, a notification, or a rendered page — and the
   incidents were exactly that copying.
2. **An advisory's identifier is enough to work from.** The identifier, the affected range, and the fixed
   version are what the `criteria`, `impl`, and `release` steps need; a step owner needing more reads the
   advisory at the host under its own grant, deliberately, rather than finding it already in the record.
   That read is a decision someone makes, which is the point.
3. **The narrowing is on the write, and coverage says so.** A narrowed observation is not a complete
   observation, and recording it as complete would be the failure `adapters.md`'s coverage rule exists to
   prevent. So the observation's coverage states that fields were withheld by policy — distinguishing
   "the adapter did not read this" from "the adapter read it and did not write it", which are different
   facts about the record.

**How this interacts with `workflows.md#security`.** The two rules compose and neither substitutes for the
other. The workflow's is a **sign-off condition**: `pm` closes on the public artifacts carrying no exploit
detail, `pr_review` closes on the fix being complete for the stated surface, and the closing sign-off's only
permitted successor is `release`, because a security fix merged and not released is not fixed. The
adapter's is an **input constraint**: the material is not in the record for those artifacts to be built
from. A workflow rule alone leaves the detail one careless render away from a public surface; an adapter
rule alone leaves a step owner free to paste it in. The security workflow's outbound rows below carry the
same narrowing, so the constraint holds in both directions at the boundary — which is the property that
makes the release notes safe by construction rather than by review.

One consequence for the release path, stated because it is where the incidents landed: the `release`
step's notes are built from the record, so an advisory the adapter narrowed produces notes that name the
identifier and the fixed version and nothing more. Widening them is an operator decision made once the fix
is upgrade-available, taken as its own action, and never a default of the adapter.

## Checks and statuses

| Event and action | Status | Outcome in the record |
|---|---|---|
| `check_run.completed` | handled | an observation on the artifact: `checks` set to `passing`, `failing`, or `pending`, or **`unknown`** where the payload cannot be read |
| `check_run.created`, `.rerequested`, `.requested_action` | handled | an observation on `checks` (`pending`); no step moves |
| `check_suite.completed` | handled | the same rollup observation, at suite grain |
| `check_suite.requested`, `.rerequested` | handled | an observation (`pending`) |
| `status` (commit status set) | handled | an observation on `checks`, the same as a check run |
| `workflow_job.*` (four actions) | deliberately ignored | `dropped`, reason `subsumed_by_check_suite`: job-grain events multiply per head and say nothing the suite rollup does not |

**A CI result is a condition a step owner reads before signing; it is never a sign-off.** This is
`adapters.md`'s rule and it is the one most often eroded in practice, because a green check *looks* like a
verdict and arrives without anyone doing anything. The distinction is load-bearing in both directions. A
green check does not close `impl` or `qa` — those steps close on their owners' sign-offs, which cite the
check as evidence (`gates_and_workflows.md#findings-verdicts-and-what-a-blocking-finding-obliges`). And a
red check does not block a step by itself; it is a condition the owner reads and, ordinarily, blocks on
with a verdict that names the run and the output it produced.

`unknown` is the third value and it holds: a CI state the adapter cannot read is `unknown` on the artifact,
and unknown holds the step
(`gates_and_workflows.md#an-unreadable-workflow-is-unknown-and-unknown-holds`). It is never coerced to
`pending`, which would read as "still under way" and clear on its own, nor to `failing`, which would assert a
result nobody observed.

## Repository-level events the swarm's operations produce or depend on

| Event and action | Status | Outcome in the record |
|---|---|---|
| the ref-update event (commits reach a branch) | handled | an observation on the artifact that refers to the ref; the branch head equals what the operation sent. **A commit reaching the default branch is neither a release nor a merge confirmation on its own** |
| the ref-update event, force-updating a ref | handled | an observation on `head`, and the rewritten-history case below |
| the ref-update event, on an untracked ref | handled | `dropped`, reason `untracked_ref` |
| the host's own automation-run event, in each of its three actions (requested, in progress, completed) | deliberately ignored | `dropped`, reason `subsumed_by_check_suite`: the run's result reaches the record as a check suite, and reading both would give one head two CI sources |
| `deployment` | handled | an observation on the artifact for the deployment target |
| `deployment_status` | handled | an action confirmation on a `rollback_deploy`-class action where one matches; otherwise an observation. **The `verify_deployed` step reads the deployed checkout regardless** — a deployment status is the host's report of its own operation, and released is not landed (`principles.md`) |
| `deployment_review.requested`, `.approved`, `.rejected` | **unhandled** | the host's environment protection is a second approval surface beside the action gate. Until decided, an observation; an approval there is **never** a checkpoint resolution, by the identity rule |
| `deployment_protection_rule` | **unhandled** | the same, as configuration |
| `repository.renamed`, `.transferred` | **unhandled** | every artifact's `external_id` for that repository is affected at once. Until built, `dropped` with reason `identity_moved`; see *The transitions the mining found unhandled* |
| `repository.archived`, `.privatized`, `.publicized`, `.deleted`, `.created`, `.edited`, `.unarchived` | handled | an observation where a tracked artifact belongs to that repository; otherwise `dropped`, reason `untracked_repository`. `.publicized` is a **condition the security step owners read**: a repository becoming public changes what its artifacts disclose |
| `repository_ruleset.*`, `branch_protection_rule.*`, `branch_protection_configuration.*` | handled | an observation, where the ruleset governs a tracked repository. **A required check configured at the host is not a step**, and a change here never alters what a batch requires |
| `merge_group.checks_requested`, `.destroyed` | **unhandled** | the merge queue again, from the group's side |
| `repository_dispatch`, `workflow_dispatch` | deliberately ignored | `dropped`, reason `inbound_command`: each is an instruction to run something, and **an inbound event is never an instruction to a workflow** (`adapters.md#no-external-event-advances-a-step-by-itself`). The reason goes back to the host as an observation the requester can see |
| the host's delivery handshake event | handled | acknowledged; `dropped`, reason `transport_handshake` |
| `meta` (the delivery configuration itself changed) | handled | an observation, and announced on the off-record path: an adapter whose subscription was narrowed underneath it stops receiving events it has rows for, which is indistinguishable from quiet |

## Everything else the host can deliver

The host delivers many more event types than the classes above: organization, membership, team, and member
events; installation and app-authorization events; sponsorship, marketplace, and package events; project
board events in both generations; discussion and discussion-comment events; wiki, star, watch, fork,
public, page-build, and repository-import events; deploy keys, custom properties, and personal access
token requests.

| Class | Status | Outcome in the record |
|---|---|---|
| Every event type not named in the tables above | deliberately ignored | `dropped`, reason `out_of_scope_class`: the class says nothing about an artifact the work model names. The drop is counted like every other, so a class becoming relevant appears as a rising count rather than as silence |

Two of these are worth naming individually, because they look ignorable and are not:

- **`installation` and `installation_repositories`.** These change what the adapter is *able* to receive.
  An installation suspended or a repository removed silently ends the delivery of every event this document
  maps for that repository. Handled as an observation **and announced on the off-record path**, for the
  same reason `meta` is: an adapter that stops receiving looks exactly like a host with nothing happening,
  which is `failure_posture.md` rule 2's failure at the boundary.
- **`member`, `membership`, and `team`.** These change who can act on the host, and a credential binding
  resolves an actor to a principal. They are observations; they never alter a binding, which is
  `authority_model.md`'s to write and never an adapter's to infer.

## Conditions that are not events

Some state the swarm depends on is not delivered at all. It is read, and a read produces an observation
with sourcing and coverage like any other — this is the first of the two findings above, and it is why no
fifth outcome was invented for it.

| Condition | How it reaches the record | Why it is not an event |
|---|---|---|
| A pull request's mergeability, and whether it conflicts | an observation on the artifact, written by a read the adapter makes when a step owner needs it | the host computes it asynchronously after a change and delivers no event when it settles |
| Which checks a branch's rules **require**, versus which ran | an observation on the artifact | the rules are configuration; only their changes are delivered, and the applied set is a read |
| Whether a required check's result was produced against the current base | an observation on the artifact, comparing the check's head and base to the artifact's | nothing announces that a previously green check is now stale — the retargeting case |
| Whether a sign-off's pinned head is still the artifact's head | a **derived read**, never an observation and never a stored flag | `data_model.md#record-conventions`; a stored freshness flag needs a process to keep it true (principle 11) |

**A conflict has no step owner, and that is the gap.** A pull request becoming unmergeable is a condition
on the artifact with no step whose closing condition it violates: `impl` may already be signed, and
`merge`'s owner discovers it only when the merge action fails. The condition belongs to the `impl` step
owner, whose sign-off closes on a pull request existing, its CI green at the pinned head, **and it being
mergeable as read** (`workflows.md#feature`), so a conflict opens `impl` again by the step's `on_fail`
rather than surfacing at the merge. Whether that path is built is a `status.md` row.

## The transitions the mining found unhandled

Each of these is a concrete failure, and each has a row above. Collected here with its stated response,
because the rows are spread across four tables and the pattern is the point: in every case an event
occurred, nothing in the adapter matched it, and the absence looked like an adapter with nothing to do.

**A draft pull request marked ready.** `pull_request.ready_for_review` had no inbound mapping. Response: an
observation on the artifact's draft state, and the condition the `impl` step owner reads before signing.
It opens no step — the implementer marking a pull request ready is the implementer saying it is judgeable,
which is a fact about the artifact, and the judgement is still a sign-off by a named principal.

**Base retargeting leaving a stale required check.** `pull_request.edited` fires when the base changes, and
a check that ran against the old base stays green at the host. Response: the base change is an observation
on the artifact, and **`checks` is set to `unknown` on it**, not left at its prior value. Unknown holds the
step, so `merge` cannot proceed on a result produced against a branch nobody reviewed against. This is the
one row where the adapter *overwrites* a value the host still reports as green, and the justification is
principle 7: the previous value is no longer a reading of anything, and the honest representation of "we
do not know" is `unknown` rather than the last thing we knew. Whether the host's `changes` payload
distinguishes a base change from a title edit in every case was not confirmed from the documentation
read; where it cannot be distinguished, the safe reading is to treat an `edited` whose `changes` names the
base as a base change and any ambiguity as `unknown` — which fails toward holding the step.

**A pull request becoming unmergeable with no step owning the conflict.** Above, under *Conditions that
are not events*: the condition belongs to `impl`, whose sign-off closes on the artifact being mergeable,
and whose `on_fail` opens it again. Unhandled.

**A force-update after a sign-off.** Revision 12 ruled that sign-offs pin the artifact state they judged,
and this row applies that ruling rather than adding to it. A force-update arrives as the host's ref-update event and is an
observation moving the artifact's `head`. Every sign-off whose `artifact_refs[]` pinned the old head is
**readable as stale by a derived read** — the pinned head no longer equals the artifact's — and no adapter
unsigns anything, because a verdict is terminal and only its author writes a new one. The steward reads the
staleness before taking the merge; a workflow wanting review to open again on a new head declares that on
the step. What makes this handled rather than unhandled is that the mechanism is entirely the record's:
the adapter writes one observation, and the pinning does the rest.

**A review dismissed after a step was signed.** Above, under *Reviews*: an observation, never an unsigning;
unhandled in its surfacing.

**An automated account approving where a review step's owner should.** Handled, and it is the identity rule doing the
work: a verdict from a credential that binds to no principal, or to a principal who does not own the step,
is an observation. Nothing in the payload changes that — the same `APPROVE` from the same host, on the
same pull request, is a sign-off or an observation depending only on whom the login resolves to. This is
worth restating as its own line because it is the row that fails silently when it fails: an automated
approval that was read as a review step's verdict produces a batch that looks fully reviewed.

**An issue transferred, or closed as duplicate.** The duplicate half is handled: an observation, and the
record's dedupe is intake's `dedupe` step. The transfer half is unhandled: the artifact's identity moves,
and the `system`/`external_id` pair the record holds stops resolving. Until built, `dropped` with reason
`identity_moved` — which is at least counted, where the failure was that it was not.

**Issue deliveries discarded at a debug log level behind a label test.** This is the failure the disposition
rule exists to close, and it is the reason this document enumerates from the host's list rather than the
receiver's. An early return that logs at debug and returns nothing is a **silent branch**: receipt without
disposition, indistinguishable from an adapter with nothing to do. Under the rule, that branch does not
exist — the delivery resolves to an outcome or to `dropped` with the reason that decided it, and the drop
is counted and announced. A label test that matched nothing then shows up as a drop count with reason
`unmapped`, in the window's announcement, rather than as a quiet debug-level record nobody reads. The count itself
is `status.md`'s.

## Outbound: the operations the code workflows take on the host

Every row is an `action`, created when the effect becomes known and evaluated at the action gate at the
moment it would be taken (`gates_and_workflows.md#actions-are-entities-only-actions-are-taken`). The
adapter performs the operation on permit, performs nothing on a checkpoint, and **confirms by reading the
host back** — never by the operation's return code.

| Step or workflow | Operation on the host | Action class | What the action gate does with the class | What confirms it landed |
|---|---|---|---|---|
| `pm` (feature, bug, security, copy) | open an issue, where the task has none and the declaration scope keeps its specification on the host | `open_issue` | low blast under a policy that lists it; unlisted resolves to `NEVER` | the issue read back by number; artifact attached to the batch |
| `pm`, `impl` | edit an issue; apply or remove a label; request review from an account | `external_api_write` | as the policy lists it | the host reflects the change, read back |
| `impl` | commits reach a branch | `git_push` | as the policy lists it | the branch head, read back, equals the commit the adapter sent |
| `impl` | open a pull request | `open_pr` | as the policy lists it | the pull request read back by number; artifact attached |
| any review step | comment on the issue or the pull request | `external_api_write` | as the policy lists it; a policy wanting a low-blast comment lists the class the comment carries | the comment read back by id |
| `merge` (feature, bug, security, copy) | merge the pull request | `merge_pr` | ordinarily a checkpoint; the operator resolves it, and a `pull_request_review` `APPROVE` from the operator's credential is that resolution | the pull request reads `merged` with a merge commit; the commit is an artifact of the batch |
| `release` | create the tag; publish the release | `release` | high blast; checkpoint unless an action series has graduated | the tag and the release read back **at their terminal state** |
| `release` (security) | publish release notes | `release` | the same, **and narrowed**: the notes name the advisory identifier and the fixed version, never exploit detail. Widening is an operator decision taken as its own action | the release read back, and the published notes read back as published |
| `dedupe`, `record`, a closing sign-off | close the issue, with the reason | `external_api_write` | as the policy lists it | the issue reads `closed`; **the task's own status was written by the sign-off, before the action** |
| recovery of a merge | open and merge the inverse change | `revert_merge` | evaluated on its own; there is no privileged undo path around the gate | the revert commit on the branch, read back; both the merge and its revert stay readable |
| recovery of a release tag | delete the tag and retag | `retag_release` | the same | the tag resolves to the intended commit, read back; a tag consumers may hold is superseded rather than silently moved |
| recovery of a deploy | roll back to the prior release | `rollback_deploy` | the same | the deployed version equals the prior release, **read from the deployment target** rather than from the operation's exit |
| recovery of a publication | supersede the published release; mark the earlier one superseded | `deprecate_publication` | the same; forward-only, because unpublishing is barred after a window | both read back: the superseding release published, the earlier marked superseded |

**What the adapter never does outbound, at this host specifically.** It never enables auto-merge, which
would grant the host a permit the gate did not issue. It never approves a review under its own credential
to satisfy a branch rule, which would be an automated account standing in for a review step's owner from the other
direction. It never force-updates a branch a sign-off has pinned. It never deletes an issue, a pull request,
or a comment to make the record look clean — a superseded effect stays readable, which is the same rule the
`publish` recovery states. And it never takes any of these because it judged an earlier effect wrong: a
recovery is an action a principal takes through the gate
(`failure_posture.md#the-operator-invoked-halt-and-what-undoes-an-action-already-taken`).

## What the host must be configured to be, for this mapping to mean what it says

The operator's 2026-09-06 14:44 memo asked whether admitting the GitHub adapter should carry a plan to
configure the host holistically, so the host is "compatible and appropriate for the adapter." It should,
and this section is that plan. But the first thing it must do is settle *what kind* of obligation host
configuration is, because the obvious argument for it is wrong in a way that matters.

### The argument that host configuration makes the mapping true, and why the design rejects it

The argument runs: this document maps a `pull_request_review` `APPROVE` to a sign-off, a merge to a gated
action, and a check verdict to a step's evidence — and none of those hold if the repository lets anything
merge without review, so the adapter would be reporting a guarantee the host does not enforce, which is
invariant 1's reporting-without-binding defect one level up from the adapter's own code.

**The design already answered this, and answered it the other way.** Two rows above rule it directly. The
`branch_protection_rule.*` row states that *a required check configured at the host is not a step, and a
change here never alters what a batch requires*. The `pull_request_review` rows attach a sign-off to the
**review step's principal**, resolved through the credential binding — not to the host's required-approvals
count, which is a different object the host maintains for its own purposes. And obligation 2's negative
test is precisely that a verdict-shaped delivery from an unbound credential becomes an observation, which
is a rule about the record's own reading of the delivery and holds no matter how the repository is
configured.

So the mapping does not rest on host protection state and cannot be falsified by it. A repository with no
protection at all still produces sign-offs that are the step owners' own, gated merges that are still
actions, and check verdicts that are still evidence a step owner cites rather than a verdict. Making host
configuration a **seventh obligation** on that reasoning would state, as a rule, a dependency the design
deliberately does not have — and would put the host's settings inside the contract that decides whether the
adapter's *mapping* is fit to be believed, where they do not belong. The mapping is fit or unfit on its own
terms. **Host configuration is not a seventh obligation.**

### What it is instead: obligation 1's inbound half, and obligation 6's outbound half

What host configuration genuinely governs is not the mapping's truth but the **enumeration's completeness**
on the way in and the **gate's exclusivity** on the way out. Both already have obligations, and both
already have failing artefacts. Host configuration is an extension of those two, and adding a seventh would
create a second home for a rule the first and sixth already own (principle 9).

**Inbound, it extends obligation 1.** Obligation 1 requires that every delivery the adapter can receive
resolves to an outcome or to `dropped` with a reason, checked by the drop counter — a number that should be
zero and rises when the enumeration is wrong. That instrument has a blind spot this document already names
elsewhere and never carried back here: an event the host is **not configured to send** is never delivered,
so it is never dropped, so the counter never moves. *Everything else the host can deliver* states the
consequence exactly, for the installation case — "an adapter that stops receiving looks exactly like a host
with nothing happening" — and the `meta` row states it for a subscription narrowed underneath the adapter.
A row this document marks *handled* whose event the subscription does not carry is therefore the same
defect as a mis-mapped row, and strictly harder to see, because the mis-mapped one at least raises a count.

So obligation 1's demonstration extends by one clause: **the adapter states the subscription its mapping
requires, and its admission reads the host's actual subscription and refuses if the two differ.** The
failing artefact is not the drop counter — it cannot be — but a check of the same shape run at the same
place the other five are checked: a **subscription reconciliation**, comparing the event types the inbound
tables mark handled against the event types the host's delivery configuration actually carries, with any
handled row whose event is unsubscribed reported as a gap. This is the read-time instrument revision 61
required for a rendered interface, applied to a webhook: an instrument that catches the failure where
nothing arrives, which a counter of arrivals structurally cannot. It runs at admission and on every `meta`
delivery, since `meta` is the host announcing that this very configuration changed.

**Outbound, it extends obligation 6.** Obligation 6 requires every outbound class to be listed in an
`action_policy`, checked by the gate resolving an unlisted class to `NEVER`. That check is airtight for
effects the adapter takes. It says nothing about effects the **host** takes on the adapter's behalf without
an action entity — and this document already identifies that shape and calls it by name: auto-merge is "the
host holding a permit the gate did not issue," where "the effect crosses the boundary with no action
entity, no gate evaluation, and no confirmation written back." Auto-merge is one instance of a class. A
repository that merges without review, or that lets an administrator bypass the rules, produces the same
object: a merge arriving as `pull_request.closed`-merged with no action for it, which the pull-request
table already classes as **a defect to surface**.

Obligation 6 therefore extends by one clause too: **the outbound path's exclusivity is a property of the
host's configuration as well as the policy's, and the adapter's admission reads the host's merge-permitting
configuration and refuses where the host can merge along a path the gate never sees.** The failing artefact
here already exists and needs nothing added — it is the unmatched-merge defect the table names. What the
extension supplies is that the defect is *predictable from configuration*, so the check moves from
after-the-fact detection to admission. Measured on this repository, that detection was firing constantly
and nobody was reading it: 63 of the last 100 merged pull requests carried no approving review at all,
because until 2026-09-06 the default branch had no review requirement to break and `enforce_admins` was
false. Those 63 were not rule violations. There was no rule.

**And the third thing configuration governs is whether a failure can be reported at all**, which belongs to
neither obligation and is the reason this section is a plan rather than only a ruling. A workflow whose
token cannot write an issue cannot surface the escalation it exists to raise; the effect is a failure that
happens and is never seen, which is `failure_posture.md` rule 2's failure at the boundary in its plainest
form. This is not an adapter obligation because no adapter is involved — it is the host's own automation —
but it is host configuration the design depends on, and it is stated here because there is nowhere else
that reads it.

### The rule, stated once

> **A host's configuration is not part of the admission contract, and it is read at admission.** The
> contract judges whether an adapter's mapping is fit to be believed, and a mapping is fit or unfit on its
> own terms. What the host's configuration determines is whether the enumeration the mapping declares is
> the enumeration the host will deliver (obligation 1), and whether the outbound gate is the only path by
> which the host takes the effects the mapping claims it gates (obligation 6). Both are read as part of the
> admission task's arch review step, against the required state the adapter's document states, and a
> difference is a finding on that step rather than a new obligation.

Three consequences follow, each of which is a rule the design already had, applied here.

- **The adapter never writes these settings.** A repository's protection state is a governance property of
  the host, and changing it is an outbound action of its own class, gated like any other
  (`gates_and_workflows.md#actions-are-entities-only-actions-are-taken`). An adapter that could relax the
  configuration its own admission is checked against would be the reporting-without-binding defect closing
  its own loop. The class exists so an operator can take the change through the gate; the adapter never
  takes it to satisfy its own admission.
- **A configuration the adapter reads is an observation, with sourcing and coverage**, like every other
  read of a condition that is not delivered (*Conditions that are not events*). It is never a stored
  field the adapter maintains, which obligation 4 forbids.
- **A difference is a finding, and its severity is the review step owner's.** This section does not rule
  that every difference blocks. A repository whose merge settings differ from the required state is a
  blocking finding, because the outbound gate's exclusivity is what obligation 6 turns on; a repository
  whose webhook delivery retention is shorter than the design would like is an ordinary finding.

### The required state, per repository

What follows is the state the design requires of a code host, derived from what this document's tables
already claim. Each row names the claim it serves, so a row nobody can trace to a claim is a preference and
does not belong here. **This is a specification, not a report**: what the two repositories currently have
is `status.md`'s, and the drift is measured there.

| Setting | What the design requires | Which claim it serves |
|---|---|---|
| **Required approving reviews on the default branch** | at least 1 | The `pull_request_review` `APPROVE` row and `workflows.md`'s review steps. Not because the count *is* the sign-off — it is not — but because a branch that permits an unreviewed merge produces merges with no action entity, which obligation 6's extension names |
| **Dismiss stale reviews** | on | The pinned-head rule: a sign-off is pinned to the artifact state it judged, and a host approval carried silently across a new head is the host asserting something the record does not |
| **Enforce the rules for accounts holding the host's highest role** | on | Obligation 6's extension in its sharpest form. An exemption for that role is precisely "a path by which the host takes the effect the gate never sees," and it is the path that produced the unreviewed merge named in `status.md` |
| **Required status checks** | every check the workflows' sign-offs cite as evidence, named explicitly; strict (up-to-date-before-merge) **off** | The `check_run` / `check_suite` rows. A check a step owner cites as evidence but that the branch does not require can be absent at merge without anything noticing, and the sign-off then cites a run that never happened for that head. Strict is off deliberately: requiring the branch be current before merge is a sequencing rule, and sequencing is the engine's, not the host's |
| **Rewriting or deleting the default branch** | both blocked | The pinned-head rule again. A sign-off pins a head; a rewritten or deleted default branch makes a pinned sign-off unreadable, and `#the-transitions-the-mining-found-unhandled` names identity moving as the case the design has no built path for |
| **Auto-merge at the repository** | disabled | Stated already, in the pull-request table: the swarm never enables auto-merge, and the host being *able* to arm it is the standing form of the same permit |
| **Delete branch on merge** | off | The standing operator rule, and the design's own: a superseded effect stays readable. A merged branch is the artifact a sign-off pinned; deleting it on merge destroys the state the sign-off judged |
| **Environment protection, where a deployment step exists** | the policy admits every ref the deploying workflow can be triggered on, **including tag refs where the trigger is a release** | The `deployment` and `deployment_status` rows, and `workflows.md`'s `release` step. An environment whose policy admits branches only, on a workflow that runs at a tag, fails every release in about a second — and the failure is a deployment that never happened rather than one that failed, which reads as quiet |
| **Environment reviewers** | none, where the action gate already gates the deploy | The `deployment_review.*` rows: the host's environment protection is a second approval surface beside the action gate, and a second gate is what principle 6 forbids. Marked unhandled in the table; the configuration answer is to not create the second surface |
| **Webhook subscription** | exactly the event types the inbound tables mark *handled* — no more, no less | Obligation 1's extension. Fewer is the silent gap; more is deliveries the mapping drops as `out_of_scope_class`, which is noisy but honest, so the asymmetry is deliberate: a missing subscription is a defect, a surplus one is a count |
| **Webhook secret** | set, and the receiver verifies every delivery against it | The identity rule. A delivery whose origin is not established cannot resolve an actor to a principal, so an unverified webhook is obligation 2's failure at the transport rather than at the mapping |
| **Webhook content type and SSL verification** | JSON, and verification on | The payload the tables read is the host's JSON payload; a form-encoded delivery is a different object. Unverified SSL makes the origin unestablished, which is the secret's row again |
| **Webhook delivery retention and redelivery** | redelivery available for at least the halt's expected duration | `failure_posture.md` rules 1 and 4, and obligation 5: the adapter leaves a delivery unacknowledged during a halt and lets the host redeliver. That posture is only sound while the host still holds the delivery |
| **`meta` subscribed** | always | The `meta` row: it is the host announcing that the subscription itself changed, which is the one event that tells the adapter its own enumeration went stale |
| **Default workflow token permissions** | read, with each workflow declaring the writes it needs | Least privilege, and the design's own fail-closed default (invariant 5). The corollary is the next row |
| **Every workflow declares a `permissions` block covering what it writes** | required | The reporting half. A workflow that surfaces a failure by opening an issue needs `issues: write`, and one that lacks it fails at the moment it tries to report — a failure that is itself unreported. This is the boundary rule: a mechanism that cannot report its own failure is not a control |
| **Allowed actions, and action pinning** | out of scope for this document | The supply chain of the host's own automation is a real concern and not this document's. Named so its absence reads as a boundary rather than an oversight |

### What this section does not decide

Whether the required state above is *met* by any particular repository is `status.md`'s, and the drift is
measured there against both repositories on 2026-09-06. Which class an action changing a host setting
carries, and what tier a policy resolves it to, is the instance's `action_policy` and is not named here.
The supply-chain posture of the host's own automation is out of scope, above. And whether the
subscription reconciliation runs as part of the conformance suite or as an admission-time check of its own
is a build question for `conformance_suite.md`, not a design question this section settles: what this
section settles is that it must run, and against what.

## What this document does not decide

The general adapter rules are `adapters.md`'s and are cited here, not restated: the four outcomes, the five
rules that decide among them (identity, linkage, dedup, unknown-and-disposition, provenance-and-read-back),
the sourcing and coverage contract, and the rule that a recovery is an outbound operation like any other.
The step lists that take these operations are `workflows.md`'s. The gate's decision function is
`gates_and_workflows.md`'s. Which rows have a built path is `status.md`'s — and every row marked
**unhandled** above has one there. Whether either repository currently meets the required host state that
this document's last-but-one section specifies is `status.md`'s too, measured there as a drift table; this
document states what the host must be, never what it is.

## Prior art

GitHub's own webhook event and payload reference is the source of the enumeration, read 2026-09-04; the
event types and their action values are the host's, and the `status`/`disposition` columns are this
document's. GitHub's distinction between a review's state and a branch protection rule's required
approvals is the distinction the identity rule draws, stated by the host itself. The anti-corruption layer
(Evans) is the shape of the whole: the host's model — labels as state, checks as verdicts, required
approvals as sign-offs — never becomes the domain's.

## Beyond the sources

The per-event mapping, the handled / deliberately ignored / unhandled marking, the security narrowing and
its three stated rules, and the treatment of the derived conditions are this document's, applying
`adapters.md`'s rules to the host's full event list. The two findings named under *What the outcomes are*
are recorded as findings rather than resolved by inventing an outcome.
