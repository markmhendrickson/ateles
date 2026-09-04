# Adapters: how external systems reach the work model, and how it reaches them

**Keyed document:** read when a webhook receiver, a mail, chat, calendar, or payment daemon, the
notification path, the GitHub pipeline, or this document changes (`conformance.md`). **Kind:** foundation;
states the design and never the state of a checkout. **Derived from:** `work_model.md` (artifacts, intake,
the three execution mechanisms), `gates_and_workflows.md` (one engine sequences from the entities; actions
and the action gate), `authority_model.md` (credentials bind to principals; approval), `workflows.md` (the
steps whose effects leave the system), and PR #745 operator review (2026-09-04, the adapter decision).
What is built, and where the adapter and the engine are still one process, is `status.md`.

## Purpose

State how an external system (a code host, a mail system, a chat channel, a calendar, a payment rail) is
connected to the work model: through an adapter that translates in two directions via artifacts.
Inbound, an external event is a signal about an artifact, never an instruction to a workflow; the adapter
writes to the record, and the record drives the workflow. Outbound, a step's effect on an external system
is an action, taken through the action gate, whose result the adapter reads back and confirms on the
record. Table the mapping for GitHub in full and for the other systems in the same shape.

## Scope

Every boundary between the record and a system the swarm does not own. In scope: the two invariants at
the boundary, what an inbound event may become in the record, what an outbound operation is, and the
identity, linkage, dedup, unknown, and provenance rules every adapter applies. Out of scope: the
workflows themselves (`workflows.md`), the gate's decision function (`gates_and_workflows.md`), what an
adapter is granted (`authority_model.md#grants`), and the per-instance binding of a system to an operator,
which is the `channel_config` and `vendor_binding` context entities, resolved at runtime and never named
here.

## The two invariants

### The workflow engine never reads an external system; it reads the record

One engine opens steps from the entities and reads the sign-offs
(`gates_and_workflows.md#declaration-batch-projection`). At the boundary that means: the engine's inputs
are batches, leases, sign-offs, actions, checkpoints, and artifacts, all in the record, and nothing else.
It does not call a code host to ask whether a pull request is approved, a mail system to ask whether a
reply arrived, or a rail to ask whether a transfer settled. Only the adapter touches the external system,
and what it learns there it writes to the record as a signal about an artifact, with provenance
(`data_model.md#record-conventions`). This is "one engine sequences from the entities" applied to the
boundary. Reason: an engine that reads an external system has a second source of truth for step state
(principle 9), one that is unreachable in a halt (`failure_posture.md#the-decision`), that cannot be read
back (principle 2), and whose values do not carry the record's `unknown` (principle 7). Read from the
other side: a step's state is derived from edges in the record and from nothing outside it, so a change
in the external system that nobody wrote to the record changes no step.

### No external event advances a step by itself

An inbound event is a signal about an artifact. It can yield exactly one of four things in the record:

1. **a sign-off by a named principal**: the event is that principal's verdict on a step the batch has
   open, and the principal is the step owner. A principal's approval on a checkpoint it is required on
   is the same outcome in its other form, a decision attributed to a named principal and authorized
   against the required approvers (`authority_model.md#approval`);
2. **an observation on an artifact**: what the record knows about the external record is updated;
3. **an action confirmation**: the observation on an action that its effect exists in the external
   system;
4. **a new task for intake**: with the external record it concerns attached as its artifact.

Nothing else. An event never opens, claims, or closes a step, never names a successor, and never advances
a batch; the sign-off it may yield does that, by the rules the step model already states. A verdict from
an account that binds to no principal, or to a principal who does not own the step, is an observation on
the artifact and never a sign-off: an automated account's approval never stands in for a lens. A CI
result is a condition a step owner reads before signing, never a sign-off. This is
`work_model.md#artifacts-are-records-a-batch-leaves-never-its-subject` applied to events: the artifact is
the record an external system holds; what happens to it is information about the batch's tasks, not a
step taken on them.

## What the adapter does with every event

The four outcomes above are the adapter's whole vocabulary. Five rules decide among them.

**Identity.** The actor of an external event is a credential (a login, an address, a chat id), never a
principal; the adapter resolves it through the credential binding (`authority_model.md#principals`).
Resolved to the step owner of an open step: the event may be that step owner's sign-off. Resolved to a
required approver on an open checkpoint: the event may be that approval. Resolved to a principal in
neither role, or to no principal: the event is an observation. The adapter never invents a binding, and
it never resolves an unrecognized credential to the operator, which is the fallthrough the claim
predicate forbids (`work_model.md#pull-is-the-only-delivery-assignment-constrains-eligibility`). This is
also the path by which a human principal signs: an agent step owner writes its sign-off to the record
directly and needs no event, while a principal whose only interface is the host signs through the host,
and the adapter carries the verdict in.

**Linkage.** An event names an external record; the adapter finds the artifact for it by `system` and
`external_id` (`data_model.md#concepts`) — the pair that identifies every artifact, because an artifact
is by definition a record in an external system reached through an adapter, and never a thing the swarm
produced into the record (`work_model.md#artifacts-are-records-a-batch-leaves-never-its-subject`). An
adapter therefore never mints an artifact for something the record already holds: what the swarm writes
for itself is an entity, and only what the external system holds gets a `system` and an `external_id`. An artifact with no batch and no task is one the record does
not track: a new-record event on such a record (an issue opened, a message received) yields a task for
intake with the artifact attached; any other event on it is dropped with that reason, counted and
surfaced under the disposition rule below. The adapter never attaches an
artifact to a batch on its own guess: intake's `link` step and the implementer's `impl` sign-off do that
(`workflows.md#intake`, `workflows.md#feature`).

**Dedup.** Every inbound event carries the external system's delivery id as the idempotency key of the
write it produces, so a redelivered event lands once (`data_model.md#record-conventions`). Every outbound
action carries its `dedup_key`, and the adapter refuses to take an action whose key it has already
confirmed (`work_model.md#at-least-once-implies-effect-dedup`).

**Unknown, and every delivery's disposition.** An event the adapter cannot map (an unknown event type, a
payload missing the field the mapping keys on, an artifact it cannot resolve) is an observation that says
so; it is never coerced to the nearest outcome (principle 7). A CI state the adapter cannot read is
`unknown` on the artifact, and unknown holds the step
(`gates_and_workflows.md#an-unreadable-workflow-is-unknown-and-unknown-holds`).

**Every delivery resolves to one of the four outcomes or to `dropped` with a reason, and the disposition
is what is recorded — never receipt alone.** Receipt without disposition is indistinguishable from
handling, and an adapter discarding every delivery at a hidden log level is indistinguishable from one
with nothing to do, which is the signature failure `failure_posture.md` rule 2 names. So there is no
silent branch: an event outside the adapter's mapping, an event on an artifact the record does not track
and that is not a new-record event, a command the adapter refuses, and an outbound operation it cannot
take for want of a credential each resolve to `dropped` with the reason that decided it. Drops are counted
per window and surfaced on the same off-record announcement path as a halt, aggregated rather than one
message per drop. This is what makes a refusal distinguishable from a delivery that never arrived, which
from the external system's side look identical: where the refusal concerns a request a person made on the
external system, the reason goes back to that system as an observation the person can see, because a log
the operator does not read is not feedback.

**Provenance and read-back.** Every write names the adapter, the external system, and the delivery id;
every write that carries a decision (a sign-off, a resolution, a confirmation) is read back before the
adapter acknowledges the event (principle 2). During a halt the adapter writes nothing, acknowledges
nothing, and lets the external system redeliver; a signal the record cannot hold is not a signal the
engine may act on (`failure_posture.md#the-rules`).

**Sourcing, coverage, and freshness.** The record already holds, for every observation, where it came
from, when, and through which interpretation, so an adapter records its sourcing through that mechanism
and never builds freshness bookkeeping of its own
(`data_model.md#record-conventions`). Concretely, every observation an adapter
writes carries three things: its **source**, the external system and the adapter that read it; the time it
was **sourced** from that system, which is the system's own time for the event and not the time the write
landed; and the **coverage** of the read that produced it — the window or page the adapter asked the
system for and what it actually returned. Coverage is the part an adapter is most tempted to omit and the
part that matters most: without it a truncated page, a request the system rate-limited into a partial
answer, and a system with genuinely nothing to report all produce the same record, so the gap is
undetectable until something downstream has already relied on it. With it, an incomplete read is readable
as incomplete and can be asked for again. Freshness — how current the record's picture of a system is,
and whether any interval was ever completely read — is then **derived** by reading provenance across an
artifact's observations, never a `last_synced_at` field the adapter maintains: a maintained field needs a
process to keep it true, which is what principle 11 forbids, and it fails in the worst direction, going
stale into a confident-looking value at exactly the moment the adapter stops reading the system.

## Outbound: steps produce actions, adapters take them

Some steps close on an effect in an external system: `impl` closes on a pull request existing, `merge` on
the merge taken, `send` on the message sent, `pay` on the transfer taken. Each such effect is an `action`,
created when it becomes known, carrying its class, evaluated at the action gate at the moment it would be
taken (`gates_and_workflows.md#actions-are-entities-only-actions-are-taken`). The adapter is the
principal's hands at the boundary: on permit it performs the operation, reads the result back from the
external system, and writes the confirmation on the action (`taken_at`, `result_ref`); the record the
effect left is an artifact `PRODUCES` from the batch. On a checkpoint it performs nothing. The class is
resolved under the project's `action_policy`; a class in neither set resolves to `NEVER`
(`gates_and_workflows.md#confidence-and-three-blast-tiers`), so a policy that wants a low-blast comment
lists the class the comment carries. The class names in the tables below are the policy's data, not a
closed set: `open_issue` and `notify_operator` are this document's, beside the classes the workflows
already name.

## GitHub

The code host holds three kinds of artifact the work model names: `issue`, `pull_request`, and `release`;
the check runs on a pull request are read as a field of that artifact.

### Inbound

| External event | What it is a signal about | Outcome in the record |
|---|---|---|
| issue opened by a person | a new record the swarm does not track | a task is created with the issue as its artifact (`REFERS_TO`) and enters intake; the opener's credential is recorded on the task, resolved to a principal where one binds |
| issue opened by the swarm's own account | an artifact a batch left | an action confirmation on the batch's `open_issue`-class action; the artifact is `PRODUCES` from the batch |
| issue edited, labelled, or unlabelled | the artifact's text or labels | an observation on the artifact (`labels[]`, title, body); a label never opens, claims, or closes a step, and a label naming a step is not that step's state |
| issue assigned or unassigned on the host | the host's own assignment | an observation on the artifact; `assigned_to` on the task is written only by intake's `classify` |
| issue comment | a message on the artifact | an observation on the artifact; a comment from the step owner of an open step, carrying a verdict in the declared form, may be that step owner's sign-off, by the same identity rule as a review (below) |
| issue closed by a person | the host's state of the artifact | an observation on the artifact (`state: closed`); the task's status is written by the batch's sign-offs, never by the host |
| issue closed by the host on a merge | the effect of the merge action | an observation on the artifact; the merge confirmation (below) already covers the effect |
| pull request opened | a record left by the implementer | the artifact is linked to the batch whose tasks it addresses; the implementer's sign-off on `impl` cites it in `artifact_refs[]`; a pull request that names no batch is an artifact with no batch, and yields a task for intake |
| pull request synchronized (new commits) | the artifact's head | an observation on the artifact (`head`); open sign-offs are unaffected; a workflow that wants review to open again on a new head declares that on the step, and the event does not do it |
| review submitted, `APPROVE`, by a lens's principal on the review step of the batch linked to the pull request | that lens's verdict | that lens's sign-off on its review step, verdict signed |
| review submitted, `REQUEST_CHANGES`, by a lens's principal | that lens's verdict | that lens's sign-off, signed with a blocking verdict; the step's `on_fail` says which earlier step opens again |
| review submitted, `COMMENT`, by anyone | remarks on the artifact | an observation on the artifact only; no sign-off |
| review submitted by a credential that binds to no principal, or to a principal who does not own an open step | an account's opinion | an observation on the artifact; an automated account's `APPROVE` never stands in for a lens |
| review submitted, `APPROVE`, by the operator's credential while a checkpoint on the batch's merge action awaits the operator | the operator's decision | resolution of that checkpoint by the operator principal (`authority_model.md#approval`), recorded on the checkpoint and read back; not a sign-off |
| review requested from an account | the host's own routing | an observation on the artifact; the step owner claims the review step on its own loop, never because the host asked |
| check run or check suite completed | the head's CI state | an observation on the artifact: `checks` set to `passing`, `failing`, or `pending`, or `unknown` where the payload cannot be read; a condition the `impl` and `qa` step owners read before signing, never a sign-off |
| commit status set | the same | the same |
| pull request merged | the effect of the merge action | an action confirmation on the batch's `merge_pr`-class action (`taken_at`, `result_ref` naming the merge commit); the merge commit is an artifact `PRODUCES` from the batch; a merge the record has no action for is an observation on the artifact and a defect to surface, never a confirmation |
| pull request closed unmerged | the host's state of the artifact | an observation on the artifact (`state: closed`); the batch's open step stays open until a principal signs it, with the verdict that the change is withdrawn or that a new pull request follows |
| pull request reopened | the same | an observation on the artifact (`state: open`) |
| release published | the effect of the release action | an action confirmation on the release batch's `release`-class action; the release is an artifact `PRODUCES` from that batch; the `verify_deployed` step still reads the deployed checkout (`workflows.md#release`) |
| branch or tag created or deleted | a ref the host holds | an observation on the artifact that refers to it, where one exists; otherwise dropped |
| commits reach a branch | the same | the same; a commit reaching the default branch is not a release and not a merge confirmation on its own |

### Outbound

| Step | Operation on the host | Action class | What the adapter confirms |
|---|---|---|---|
| `pm` | open an issue, where the task has none and the project keeps its specification on the host | `open_issue` | the issue exists, read back by number; artifact `issue` attached to the batch |
| `impl` | open a pull request | `open_pr` | the pull request exists, read back by number; artifact `pull_request` attached to the batch |
| `impl` | commits reach a branch | `git_push` | the branch head equals the commit the adapter sent |
| any review step | comment on the issue or the pull request | `external_api_write` | the comment exists, read back by id |
| `pm`, `impl` | request review from an account; apply or remove a label; edit the issue | `external_api_write` | the host reflects the change, read back |
| `merge` | merge the pull request | `merge_pr` | the pull request reads `merged` with a merge commit; the commit is an artifact of the batch |
| `release` | create the tag; publish the release | `release` | the tag and the release exist, read back at their terminal state |
| `dedupe`, `record`, a closing sign-off | close the issue, with the reason | `external_api_write` | the issue reads `closed`; the task's own status was written by the sign-off, before the action |

**An artifact with no batch never receives retroactive step state.** No step of any workflow is opened on
an artifact that no batch addresses, and neither an adapter nor the workflow engine may initialize a step
on its behalf — not as `not_required`, not as `not_applicable`, not as clear, not as anything. Step state
is derived from a batch, a lease, and a sign-off (`gates_and_workflows.md#declaration-batch-projection`);
where there is no batch there is nothing to derive it from, and a value written in place of that
derivation is a fabricated verdict on work no principal judged. A pull request opened before any task
exists for it is therefore an artifact with no batch, and it yields a task for intake like any other
untracked record. The batch that later addresses that task opens its own steps, from the beginning of its
workflow, and its `impl` sign-off cites the existing pull request in `artifact_refs[]` — the earlier
existence of the artifact buys the batch nothing and skips nothing. A companion rule of the same kind: a
pull request whose shipped change exceeds the scope a lens signed does not inherit that narrower sign-off,
because a sign-off is pinned to the artifact state it judged (`data_model.md#record-conventions`).

Two things the tables show. A review's `APPROVE` becomes a sign-off only through identity: the same
verdict from the same login is a sign-off when the login binds to the step owner of an open step and an
observation otherwise, and nothing in the payload changes that. And every host state that looks like step
state (a label, a review decision, a check, a closed pull request) reaches the step only through a
principal who reads it and signs.

## Gmail

The mail system holds artifacts of kind `thread` and `message`. The inbound signal is a message; the
outbound effect is a send.

| External event | What it is a signal about | Outcome in the record |
|---|---|---|
| message received on a thread the record does not track | a new record | a task with the thread as its artifact, entering intake; the mail poller is the daemon mechanism of `work_model.md#the-three-execution-mechanisms`, which produces tasks and never receives one |
| message received on a thread attached to an open outreach batch | a reply | an observation on the thread artifact; the `follow_up` step owner reads it as the reply the step closes on (`workflows.md#outreach`) |
| message received from the operator's address, answering a message the operator-facing agent sent for a checkpoint | the operator's decision | resolution of that checkpoint by the operator principal, read back on the checkpoint; from any other address, an observation |
| message sent from the operator's account, observed in the sent folder | the effect of a send | an action confirmation on the `send_external_comms` action whose `dedup_key` matches; a sent message with no action is an observation and a defect to surface |
| message labelled, archived, or deleted | the artifact's state | an observation on the artifact |

| Step | Operation | Action class | What the adapter confirms |
|---|---|---|---|
| `send`, `follow_up` | send a message, or a reply on the thread | `send_external_comms` | the sent message read back from the mail system by its message id, never inferred from the send call's return |
| `draft` | stage a draft in the mail system (optional; the design's staging is the draft in the record, and a staged draft is never updated in place, `workflows.md#outreach`) | `external_api_write` | the draft exists, read back |
| `persist`, `record` | label or archive a thread | `external_api_write` | the thread's labels, read back |

## Telegram

The chat channel holds artifacts of kind `message`. It is a channel the operator-facing agent carries
checkpoints and operator-only tasks through, where the `channel_config` entity names it.

| External event | What it is a signal about | Outcome in the record |
|---|---|---|
| message from a chat id bound to the operator principal, answering nothing the record awaits | a new ask | a task with the message as its artifact, entering intake |
| message from that chat id, answering a checkpoint the operator-facing agent carried | the operator's decision | resolution of that checkpoint by the operator principal (`authority_model.md#approval`), read back on the checkpoint |
| message from that chat id, reporting an operator-only action taken | the outcome of `await` | an observation on the task's artifact that the `record` step owner reads and signs on (`workflows.md#operator-only`) |
| message from a chat id bound to no principal | noise | dropped, or an observation where a tracked artifact is named; never a task, never a resolution |

| Step | Operation | Action class | What the adapter confirms |
|---|---|---|---|
| `present`, `operator_preview`, `consent` | send the checkpoint or the task to the operator's chat | `notify_operator` | the message id, read back; the checkpoint stays open until resolved on the record, whatever the delivery status |
| `deliver` | send a digest or a result | `notify_operator` | the message id, read back |

A notification is an action with a class of its own so that a policy may keep it low-blast. Its delivery
is never the checkpoint's resolution, and a checkpoint nobody answers times out into its terminal state on
the record (`gates_and_workflows.md#the-checkpoint`).

## Calendar

The calendar holds artifacts of kind `event`.

| External event | What it is a signal about | Outcome in the record |
|---|---|---|
| event created, or an invitation received | a new record | an artifact; and a task for intake where the event carries an ask (a meeting to prepare for, a recurring obligation the calendar drives) |
| event updated, moved, or cancelled | the artifact's state | an observation on the artifact; a task whose due date follows the event reads it at `prioritize` or at claim, never through the event |
| event ended, with a recording | a transcript to process | a task for meeting processing with the event and the recording as its artifacts (`workflows.md#meeting-processing`) |

| Step | Operation | Action class | What the adapter confirms |
|---|---|---|---|
| `deliver`, `record` | create, move, or cancel an event; send an invitation | `external_api_write`; `send_external_comms` where a party outside the swarm is invited | the event read back by id |

## Payments

The rail holds artifacts of kind `transfer` and `receipt`. The separation of duties the payment workflow
names applies to the adapter as to any principal: the adapter that takes a `pay` action never writes the
`reconcile` sign-off (`workflows.md#payment`).

| External event | What it is a signal about | Outcome in the record |
|---|---|---|
| transfer reaches a terminal state at the rail | the effect of the `pay` action | an action confirmation on the `payment`-class action whose `dedup_key` matches; the transfer record is an artifact `PRODUCES` from the batch; the `reconcile` step owner reads it and signs, and the `transaction` entity is that step's write, not the adapter's |
| transfer fails, or is returned by the rail | the same | an action confirmation with the failing result; `reconcile`'s `on_fail` opens `pay` again through the gate, never a second submission by the adapter |
| incoming payment received | money that arrived | an observation on the artifact for the obligation it settles, where one is tracked; otherwise a task for intake with the transfer as its artifact |
| balance or rate changed | the rail's state | an observation, where a tracked artifact depends on it; otherwise dropped |

| Step | Operation | Action class | What the adapter confirms |
|---|---|---|---|
| `pay` | submit the transfer | `payment`, or `transfer` where the policy distinguishes them | the transfer read back from the rail by its id at its terminal status, never the submission's return |

## What an adapter never does

It never reads a workflow to decide what an event means: the four outcomes are decided by identity,
linkage, and the artifact, and the engine decides the rest from the record. It never opens, claims, or
closes a step, and never writes a task's status or a batch's successor. It never takes an action that has
not passed the gate, and never repeats one on its own. It never resolves an unrecognized credential to a
principal. It never holds step state of its own: an adapter that keeps a per-artifact map of which steps
are satisfied has become a second engine (`gates_and_workflows.md#declaration-batch-projection`). And it
never performs an operation and reports success without reading the external system back.

## The adapter and the engine are two roles

Whether one process hosts both is an implementation choice; the design's requirement is that they meet
only in the record. An adapter is a daemon in the sense of `work_model.md#the-three-execution-mechanisms`:
it self-triggers on the external system's events, produces writes to the record, and receives no task.
The engine opens steps from the entities. The two are separable because the only thing that passes
between them is what the adapter wrote and the engine read, with provenance on every write; a process
that lets an event drive a step without a write in between has merged them, and where that is the case
on a checkout is `status.md`.

## Prior art

The anti-corruption layer (Evans) is the shape: a translation layer at the boundary, so that the external
system's model never becomes the domain's. Kafka Connect's source and sink connectors are the two
directions as separate roles with the log between them; here the record is the log. GitHub's webhook
delivery id as the dedup key, and its distinction between a review's state and a branch protection rule's
required approvals, are the identity and CI rules above, stated by the host itself.

## Beyond the sources

The four-outcome rule, the five adapter rules, and the tables are this document's, consolidating the
operator's decision on PR #745; the prior art named above is cited from general knowledge, not from the
prior-art entity the other documents cite.
