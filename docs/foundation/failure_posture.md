# Failure posture: what the swarm does when its record is unreachable

**Keyed document:** read when the reachability, drift, forensics, readiness, or Neotoma client paths change
(`conformance.md`). **Kind:** foundation; states the design and never the state of a checkout. **Derived
from:** synthesis `ent_b0ce322f768e4fc676b73139` (PR-12 to PR-15, C5, C17), prior art
`ent_08460968e6f49dac21510f4a`, task `ent_670cacab2f46fd9547ced7ed` (operator-approved), gate-state plan
`ent_4222e5d52edd9bdba7b78cc1` decisions `neotoma_is_a_hard_dependency_swarm_halts`,
`halt_work_but_never_stop_observing`, `the_halt_must_announce_itself_off_neotoma`,
`reachability_check_belongs_at_dispatch_with_mid_task_writes_failing_closed`,
`deferral_must_be_bounded_and_escalate_off_neotoma`, `unknown_must_stay_distinct_from_a_verdict`,
`nyctea_635_becomes_load_bearing`, PR #745 operator review (2026-09-04), and the operator memos of
2026-09-05 (the `undetermined_scope` reason class), and the operator's 2026-09-05 terminology review (revision 17: the one boundary and the term `external system`, the `action series` rename, `subject` defined, and the two-part `checkpoint`), and the operator's 2026-09-05 review of review relevance (revision 19: the `applies_when` condition on an optional step, and two terms retired in favour of `review step`), and PR #745 operator review (2026-09-05, rulings 13–14, 16–18, 23–29: a hold on a discovered condition is a deferral under rule 5; the `dependency_cycle` reason class). What is built is `status.md`;Revised by the consistency pass of 2026-09-06 (revision 35: the merge action's class named `merge_pr` in the recovery table). What is built is `status.md`;
how a checkpoint is recorded is `data_model.md`. Revised by the simplification pass of 2026-09-05 (revision 29: `claimant` retired for lease holder). Revised by the memo-gap pass of 2026-09-06 (revision 31: a condition of a batch is raised on one of its tasks, never on the batch). Revised by the workflow-format pass of 2026-09-06 (revision 34: rule 5's ceiling for a holding step, and the unclaimed-step interval, each named as a field on the step). Revised by the testability pass of 2026-09-06 (revision 37: the announcement path's own outage and the capture of last resort; the window observation; `action_policy.recoveries`; recovery paths and their cadence on the binding; `lapse_cap`; which checkpoints hold a task from claim; `AWAITS` names principals). Revised by the model-and-harness-routing pass of 2026-09-06 (revision 43: open decision 60 — a runner's lease-held step losing its model or harness mid-execution; the tier-eligibility half ruled as a consequence of decision 59). Revised by the rulings pass of 2026-09-06 (revision 44: decision 60 ruled in full — unavailability holds and raises under the existing `lapse_cap`, no second clock; `capability_unavailable` named as a reason class distinct from `capability_denied`). Revised by the underdetermined-inputs pass of 2026-09-06 (revision 45: decision 71 ruled — a required read that resolved to no instance raises `underdetermined_inputs` at hydration, before the step opens, and an input found but too thin to act on stays the step owner's judgement under decision 13).

## Purpose

State the operator's decision that Neotoma is a hard dependency and the eight rules that follow from it, so
the posture lives where the review steps read it rather than in a task description; state which
failures the swarm escalates as checkpoints on a task, and which it does not; and state the halt the
operator invokes and what undoes an action already taken. `durable_execution_substrate.md`
records the position on execution engines and replay that this document relies on; it stays.

## Scope

Every daemon and every agent runner, on the task path and off it.

## The decision

**Neotoma is the swarm's foundational level of truth. If it cannot be accessed, the swarm does not do
work.** Not degraded operation, not a hardcoded fallback. The reasoning is stronger than availability: a
swarm that operates while its record is unreachable produces work with no record. Across a fleet of
unattended daemons that is unaccountable work, worse than the work not happening, because the swarm then
acts on a history it cannot reconstruct. This decision superseded degrade-on-capability,
refuse-and-requeue-as-fallback, and the hardcoded step-list floor (C5, below).

## The rules

1. **Halt work; never stop observing.** No claim of a task or a step, no assignment, no step opening, no
   gate decision, nothing claimed complete without a record. Watchdogs, forensic capture, health checks, and alerting stay live. A diagnostic capture asserts
   nothing about the record, so it does not require the record; it writes to local disk, always, because
   writing evidence of an outage into the thing that is out is the wrong move, and a hard dependency that
   stops the thing diagnosing it makes recovery impossible.

2. **Announce the halt off-Neotoma, on entering and on leaving.** A silently halted swarm is
   indistinguishable from an idle one, this codebase's signature failure. The announcement travels a path
   that survives the outage, aggregated per window, never one page per blocked claim.

   **The announcement path is itself an external system, and it can be down.** Then rule 1's capture is the
   announcement of last resort: it holds what would have been announced — the halt entering, each window's
   aggregate, the halt leaving — and when the path returns every captured window is announced with its
   original time, so the path shows no gap and a reader of it cannot mistake the outage of the path for a
   quiet swarm. No second channel is declared: it would be a second binding to keep current, and it fails
   the same way; the capture is read by hand when nothing else answers. The window is the one declared on
   the binding that names the path (`channel_config`), and while the record is reachable every daemon
   writes one observation per window on its own `agent_session`, with zeros where nothing happened
   (`adapters.md#what-the-adapter-does-with-every-event`) — so a daemon silent past its window is a derived
   read, and an idle daemon and a dead one are distinguishable whenever the record is up.

3. **The reachability check is a real read at claim time, never `/health`.** One probe when a task or a
   step is claimed, not per operation. A health endpoint can return green while every read hangs on a wedged database, so the probe
   reads what the work will read.

4. **A mid-task write failure leaves the task in its prior state.** The agent is already at work when the
   record goes away. It does not abandon work already under way, and it never claims a completion it
   cannot record:
   finish the reasoning, attempt the write, and on failure leave the task as it was: the lease lapses on
   its own and the task is claimable again (`work_model.md`), with no process needed to return it. A
   sign-off whose write failed is the unaccountable work this posture exists to prevent. Silence beats a
   false verdict.

   **The step owner does not post its verdict anywhere else.** A verdict that cannot reach the record is
   not a verdict. The step stays open; the condition is announced on the off-record path of rule 2, along
   with every other blocked claim in the window; and the work is re-claimed when the record returns,
   because the lease lapses on its own and publishes the step again. The verdict is re-derived then, not
   replayed from wherever it was parked — the artifact may have moved under it, and a sign-off is pinned
   to the artifact state it judged (`data_model.md#concepts`).

   The anti-pattern, named so it is recognizable: the step owner writes the verdict to the code host as a
   review or a comment, treats that comment as the authoritative record because it is the only surface
   that accepted the write, and a later reader — the steward, the engine, or the owner itself on a second
   pass — replays the comment as though it were the sign-off. Every part of that is forbidden. A verdict
   that reached only the artifact is an **observation** on the artifact and never a sign-off
   (`adapters.md#no-external-event-advances-a-step-by-itself`); the code host is not a write-ahead log
   for the record; and a step is closed by a sign-off or it is open. The same holds for a verdict parked
   on local disk, in a chat message, or in the runner's own scratch state: those are diagnostic capture
   under rule 1, which asserts nothing about the record.

5. **Deferral is bounded; exhaustion escalates.** Backoff is mandatory: a slow instance under retry
   pressure is how slow becomes unreachable, and unreachable stays distinguishable from slow. A task requeued
   indefinitely against a store that never returns is a silent stall, so every deferral has a ceiling. When
   the ceiling is reached and the record is reachable, the task is escalated: one checkpoint with reason
   `rounds_exhausted` (below). When the record is not reachable, nothing can be written, and the exhaustion
   is announced on the path that survives the outage (rule 2). The drain is the lapse rule below (backoff,
   lapse cap, checkpoint on exhaustion), connected to the gating path rather than rebuilt. A step **holding**
   on a condition discovered mid-flight that owes nobody a decision
   (`work_model.md#a-batch-may-hold-on-a-condition-discovered-mid-flight`) is a deferral under this rule and
   takes its bound: re-evaluation with backoff, a ceiling, and at the ceiling one checkpoint,
   `rounds_exhausted`, carrying the finding that names what the step was waiting on. The ceiling is the
   `hold_bound` the step's declaration carries (`gates_and_workflows.md#declaration-batch-projection`); a
   step declaring none has no ceiling, and the absence is a declaration defect, as an undeclared
   unclaimed-step interval is (below).

6. **Every write is read back.** Principle 2 of `principles.md`, restated here because an outage is when a
   write most plausibly reports success without landing: a store can return 200 with a warning and persist
   the row without the field, and an idempotency-mismatch error is stronger proof of a prior commit than a
   success response (digest `ent_b31cad6074f79e8adfa6b2aa`).

7. **Unknown stays distinct from a verdict.** A failed read is not a negative result. Any reader of gate,
   grant, or drift state carries a third value; an error is never coerced to pending or to clear. Principle
   7 of `principles.md`; at a policy enforcement point the third value resolves to deny (`authority_model.md`).

8. **A failure that left no effect is retried; one that states its own retry time is deferred to it.** Rule
   5 bounds a task's deferral. This rule covers the path rule 5 does not: the mechanism that starts an
   agent failing, rather than the work. A misconfiguration that matched no provider, a transport reset, a
   rate limit that names its own reset time — each left no effect at all, so none of them is a failure of
   the work, and recording one as a task failure writes a false terminal state (principle 7). A failure
   that left no effect is retried with backoff. A failure that states its own retry time is deferred to
   **that** time rather than discarded, because a stated bound is better information than a guessed one and
   discarding it spends a retry on a failure that said exactly when to try again. Exhaustion escalates
   exactly as rule 5 says: one checkpoint, reason `rounds_exhausted`. The runner's exit is read before a
   retry is spent, so a failure of the mechanism stays distinguishable from a failure of the work. Which
   providers exist, and in what order they are tried, is a `vendor_binding` and not a foundation concern —
   the classification is what outlives the vendors.

   **The same classification governs an adapter's read of an external system.** A failed read during
   hydration (`adapters.md#the-adapter-runs-before-and-after-a-step-never-during-it`) left no effect
   either, so it is the same kind of failure and takes the same rule rather than a retry policy of its
   own: retried with backoff, or deferred to the retry time the system stated, and never re-requested
   immediately or on a fixed interval. Backoff is not optional here for a reason this rule's other cases
   do not have: the thing being retried is **someone else's system**, and an adapter that re-requests on
   every failure turns one unreachable dependency into sustained load on a system the swarm does not own
   and may be rate-limited or blocked for. Where the system states its own reset — as a rate limit
   commonly does — that stated time governs, being better information than any interval the adapter would
   guess. Retries are per system rather than per step, so many steps waiting on one unreachable system
   produce one backoff and not one each. What exhaustion does is not this rule's to say: an adapter read
   is a step's declared read, so it holds the step, bounded, and the bound raises the checkpoint that
   names the dependency (`gates_and_workflows.md#declaration-batch-projection`) — the retry schedule lives
   here and the escalation lives there, one mechanism each.

### A runner's model or harness going unavailable mid-step

Rule 8 already classifies this failure — "the mechanism that starts an agent failing, rather than the
work" — and a model rate limit or a harness outage is exactly that mechanism, not the work: no effect was
left, so it is retried with backoff, or deferred to a stated reset time where the failure names one, and
exhaustion escalates as `rounds_exhausted` the same as any other bounded deferral. That much rule 8 already
settles for the case where the runner has not yet produced an effect. What it does not settle is the case
rule 8 was not written for: a runner that already holds the lease, has been executing the step, and loses
the model or harness it started with partway through — the case that killed three runners in one night to
one rate limit, one of them mid-rebase.

**Ruled (2026-09-06).** Registered in `conformance.md#the-register-of-open-design-decisions`. Unknown stays
distinct from a verdict (principle 7), and this is squarely an unknown: the step is neither confirmed
advanced nor confirmed failed when the runner under it disappears. Three shapes, and the design must pick
one rather than let each runner's crash handler decide for itself:

1. **Unavailability is an `unknown` that holds the step and raises.** The lease is left to lapse on its
   ordinary schedule (no special-cased early release), the watchdog counts it as it counts any lapse, and
   at `lapse_cap` the checkpoint raised is `repeated_lapse` — the reason class rule 8's own escalation
   already uses, naming the runner's model or harness failure as the finding. This treats the mid-step case
   as continuous with rule 8's pre-effect case: the mechanism failed, the work is unknown, and the existing
   lapse-and-cap machinery is the one queue principle 6 already forbids duplicating.
2. **A lapse that returns the lease immediately** rather than waiting out the ordinary interval, on the
   reasoning that a runner known to be unable to continue (a rate limit with a stated reset far past any
   reasonable hold) should not occupy a lease for the ordinary `unclaimed_after` window while nothing can
   act on it. This is faster recovery at the cost of a second lapse rule for one cause among several, which
   principle 6 weighs against.
3. **A re-claim at a different tier.** The task becomes reclaimable and a runner bound to a different
   `vendor_binding` — a different harness, a different model tier — claims it next. **This must not happen
   silently below the class's required tier once decision 59 is written**: a class with a `min_tier`
   floor is exactly as ineligible for a lower-tier runner on re-claim as on first claim, because the
   eligibility check decision 59 settles is evaluated at the take, not once per task. A re-claim at or
   above the required tier is unremarkable — it is the ordinary claim predicate finding a new eligible
   runner. A re-claim silently below it is the reporting-without-binding shape principle 1 names: the step
   completes, a sign-off is written, and nothing in the record shows the work was done under a floor the
   policy says it should not have cleared.

**Why this is not decided by the shape above alone.** Options 1 and 2 differ only in the lapse timing and
are within the design's existing machinery either way — a value question about how eagerly to release,
not a structural one. The tier question is structural: it determines whether `min_tier` (decision 59) is a
real gate or a value nobody can rely on, since a floor that a re-claim can silently duck under is not a
floor. **What is ruled regardless of which lapse timing is chosen:** the claim predicate that decides
whether a runner is eligible for a class's action reads the same `min_tier` value on first claim and on
every re-claim, so a step's tier requirement cannot be met once and forgotten.

**Ruled.** Option 1: unavailability is an `unknown` that holds the step and raises, and lapse timing is
governed by the existing `lapse_cap` rather than a second clock — no early-release rule, no second lapse
schedule. Reason: principle 6 forbids a parallel queue for one cause among several when the existing
lapse-and-cap machinery already treats "the mechanism failed, the work is unknown" as its general case;
option 2's faster recovery was weighed and set aside for exactly the cost the design already flags — a
lapse rule that exists to serve a single cause. **Cost accepted:** a runner known to be unable to continue
(a rate limit with a stated reset far past any reasonable hold) still occupies its lease for the ordinary
`unclaimed_after` window before the task becomes reclaimable, rather than releasing the moment the failure
is known.

**The reason class, ruled.** Unavailability is **its own reason class, `capability_unavailable`**, distinct
from `capability_denied`. `capability_denied` is a permission answer — a grant boundary refusing a runner
outright, a class the runner is not eligible for regardless of when it asks. `capability_unavailable` is
transient — the runner was eligible and had claimed the step in good standing when the model or harness it
started under stopped answering mid-step, with no judgement that the runner should not have held the lease
at all. Collapsing the two would blur a checkpoint the operator reads differently: `capability_denied` asks
whether to grant something; `capability_unavailable` asks whether to wait, switch vendor, or accept the
lapse. The tier-eligibility half from option 3 stands as already ruled above regardless of this class
question: a re-claim reads the same `min_tier` floor a first claim does, so the reason class distinguishing
"ineligible" from "unavailable" governs how the *first* failure is named, not whether a later re-claim can
duck under a floor.

**What reopens it.** An instance finding `repeated_lapse` and `capability_unavailable` need to be told
apart at the checkpoint itself, rather than `capability_unavailable` riding as the named finding on a
`repeated_lapse` checkpoint as it does today.

## The operator-invoked halt, and what undoes an action already taken

The halt of *The decision* above is automatic and has one cause: the record is unreachable. This section
states the halt the operator invokes, and the recovery for an action already taken — the two things the
action gate's decision to permit an action does not cover, because the gate decides whether an effect is
taken and says nothing about stopping the swarm or undoing the effect afterwards.

**The operator-invoked halt is verified to have stopped, by a read-back.** The operator may halt the swarm
on their own word, at any time, for any reason or none. The halt takes effect the way the automatic one
does — no claim of a task or a step, no step opening, no gate decision, nothing claimed complete — and
observation continues. What makes it a control rather than a request is the confirmation: **the halt is
confirmed stopped by a read of the swarm's state, never by the command returning.** A command that returns
is a write reporting success, and principle 2 applies here more forcefully than anywhere else in this
document, because the operator invokes this one precisely when they believe the swarm is doing something
wrong. The read-back is the absence of live leases and open claims, read from the record; the absence of
new activity is not the read-back, since an idle swarm and a halted one are indistinguishable by that
measure — which is rule 2's failure restated. Until the read-back confirms it, the swarm is not halted,
and the operator is told so.

**Every action class names its recovery, even where the recovery is only a forward fix.** An action is an
intended effect on an external system, and the classes with the largest blast are the ones the design has been
silent on. Each is named here so that the answer exists before it is needed rather than being improvised
under pressure:

| Action class | Recovery |
|---|---|
| `merge_pr` | a revert — the inverse change, taken as its own action |
| publish | deprecate and supersede; unpublishing is barred after a window, so the recovery is forward-only and the superseded version stays readable |
| release tag | delete and retag |
| deploy | a rollback to the prior release |

A recovery is itself an [action](vocabulary.md#action): it goes through the [action gate](vocabulary.md#action-gate),
under its own class, and is recorded like any other. There is no privileged undo path that bypasses the
gate, because an undo taken in haste is exactly the shape the gate exists to hold. Where a class's recovery
is forward-only, the design says so plainly rather than implying a reversal that does not exist. The
outbound operation each recovery takes is the adapter's (`adapters.md`), which is where the systems these
classes reach are tabled.

**The record's home for the map is the policy.** `action_policy.recoveries` names, for every class listed
in either tier, the class its recovery is taken under, or `forward_only` where the system offers no
reversal, or `none` where the class leaves nothing to undo (`data_model.md#concepts`); a policy write that
lists a class in a tier and no recovery for it is refused at the write, so a class cannot be granted before
its undo is named. The table above is the design's statement for the four classes it had been silent on;
the project's full list is the policy's, and it is what a reader — or a test — reads.

**A restore obligation, with a stated cadence.** A backup nobody has restored is indistinguishable from no
backup. Every recovery path this document names — a record snapshot, a repository bundle, a rollback target
— is **exercised on a declared cadence**, and the exercise is a real restore, not an inspection of the
artifact. This is principle 4 applied to recovery: a restore that has never been run cannot fail, so until
it is run it is decoration. The paths and their cadence are declared where the system that holds the path
is bound: `recovery_paths[]` on the binding entity for that system's instance (`vendor_binding`, or the one
binding type decision 35 settles — `adapters.md#scope`), each naming the path and its `cadence` — the code
host's binding for a repository bundle, the deploy target's for a rollback target, the record host's for a
snapshot. Each exercise is an observation on that binding, dated by the exercise and carrying what the
restore read back, and "overdue" is a derived read — the last exercise older than the cadence — never a
maintained flag (principle 11). A path with no stated cadence is an unexercised path and reads as one.

**Whatever detects does not remediate.** A mechanism that observes the swarm — a watchdog, an external
prober, a health check — raises and escalates, and holds no authority to act on what it found. It performs
no restart, no scale, no rollback, no re-claim, and no infrastructure mutation of any kind. This is the
rule `vocabulary.md#watchdog` already states for the lease watchdog, generalized: the value of a detector
is that its judgement is independent of the thing it watches, and a detector that also remediates has both
an opinion and a hand on the lever. Remediation is an action, taken by a principal through the gate, on the
detector's report.

## Repeated lapse raises a checkpoint

A lease that lapses is not returned by anything; the task is simply claimable again (`work_model.md`).
The rule that survives the retired reaper is about repetition: the watchdog counts lapses per task,
with bounded backoff between re-claims, and when one task's count reaches `lapse_cap` — declared on the
project's `action_policy` (`data_model.md#concepts`), never inferred; an undeclared cap raises no
checkpoint, and the absence is visible in the policy, a defect caught where the policy is written and never
a default supplied at runtime, as the unclaimed-step interval is treated (below) — it escalates the task:
one checkpoint, subject the task, reason `repeated_lapse`, carrying the count and the last lease holders,
rather than letting the task be re-claimed forever. The watchdog observes and escalates; it holds no
authority over any lease and never chooses the next lease holder. During a halt the count still accrues,
because a lapse during an outage is still a lapse; the checkpoint is written when the record returns, and
until then the condition is announced on the off-Neotoma path (rule 2). This is OTP's supervisor rule
(prior art, below) applied to a lease.

## Checkpoints on tasks: one queue, one protocol

To escalate is to raise a checkpoint on a task the swarm cannot advance. The checkpoint is the same
entity the action gate writes when it holds an action (`gates_and_workflows.md#the-checkpoint`); only
its subject and its reason class differ. The reason classes the design names: `gate_hold` (an action held
at the gate), `repeated_lapse` (above), `unreadable_workflow` (`gates_and_workflows.md`, no step opened),
`rounds_exhausted` (rule 5, or the `rounds_cap` a step declares on its `on_fail` loop —
`gates_and_workflows.md#declaration-batch-projection`), `unspawnable_assignee`
(`work_model.md`, an `assigned_to` nobody can run, and a declared `owner_role` the roster resolves to
nobody), `unclaimed_step` (above), `undeclared_dependency` (a step could not read a type it declared, and
the hold reached its bound — `gates_and_workflows.md#declaration-batch-projection`), `capability_denied`
(a principal was denied a capability its step needed — `authority_model.md#grants`), and
`lossy_record_mutation` (a write to the record whose blast exceeds the declared count —
`gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken`), and `undetermined_scope` (a
standing finding whose right scope — the agent, the workflow, or one step — cannot be determined from the
finding, so it is put to the operator rather than guessed —
`gates_and_workflows.md#a-finding-is-one-off-or-standing-and-a-standing-one-obliges-a-change-to-what-produced-it`),
and `dependency_cycle` (two or more batches each holding on a task attached to another, found after the
writes were made; each batch's tasks are escalated with one, naming the batches and edges in the loop, and
every step owner in it holds until a principal breaks it —
`work_model.md#a-batch-may-depend-on-a-task-it-created`), and `underdetermined_inputs` (a step's required
read resolved to nothing — the record holds no instance and no adapter could import one — so the step
cannot be entered on what the task states and what the record holds together, and the operator is asked
for the missing input rather than an agent inventing it — below);
a policy may declare more. A checkpoint on a task
carries the reason, the needed input, the options, and whom it awaits, and is presented and resolved
through the one decision queue, by the one resolution protocol, that checkpoints on actions use. Do not
build a second gate, a second queue, or a second notification path for task-level failure (principle 6):
a queue nobody consumes is a report, not a control (principle 1).

**A condition of a batch is raised on one of its tasks, never on the batch.** A checkpoint has exactly one
subject, an action or a task (`gates_and_workflows.md#the-checkpoint`), and a batch is neither. Where the
condition stops a whole batch — no step of its workflow opened (`unreadable_workflow`), a step nobody has
claimed (`unclaimed_step`), a loop of dependencies (`dependency_cycle`) — one checkpoint is raised, its
subject is one task of the batch, and it names the batch and the step in its `needed_input`. One suffices:
the batch's other tasks are read as held through their `ADDRESSED_BY` edge to the batch the subject is
attached to, so the queue carries one row per stopped batch rather than one per task, and a second
checkpoint with the same reason on any task of a batch that already has one open is not raised — the raiser
reads the open checkpoints on the batch's tasks first, which is the bounded retrieval every write that
creates an entity already makes (`data_model.md#record-conventions`). Which of the batch's tasks is the
subject carries no meaning, and nothing reads it as one. "The batch's tasks are escalated with one
checkpoint", wherever the documents say it, means this.

**Escalation reorders; it never signs.** A checkpoint raised on a step nobody has claimed, or on a task
the swarm cannot advance, changes the order in which claimable work is offered and what a principal
attends to first. It changes no verdict and closes no step. A step blocked on a step owner who never
acted is a liveness condition, not an ordering one, and reordering the queue cannot release it: the step
is closed by that step owner's sign off or it stays open. **No step is closed by elapsed time.** A gate that expires into
a pass is a gate that fails open on a timer, which is the shape this model exists to remove — the whole
value of a required step is that its absence is visible, and a timer converts that absence into a silent
permit. So a checkpoint raised on an unclaimed step names the step, its owner role, and how long it has
been open, and stops there.

**An open checkpoint whose subject is a task holds that task from claim, and `unclaimed_step` is the one
class that does not.** A task with an open checkpoint on it — `repeated_lapse`, `rounds_exhausted`,
`undeclared_dependency`, `capability_denied`, `undetermined_scope`, `dependency_cycle`,
`underdetermined_inputs`, or a class a policy
declares — is not claimable until the checkpoint is terminal, because each of those is raised where a
re-claim would restart the condition the checkpoint exists to stop; on resolution the task is re-claimed or
closed (`gates_and_workflows.md#the-checkpoint`). `unclaimed_step` is the exception by its own rule above:
it reorders, and the step it names stays claimable by its role, since a step owner arriving late is the
resolution. `unreadable_workflow` and `unspawnable_assignee` hold too, though nothing could claim under
them anyway. A checkpoint whose subject is an action holds the action and not the task, whose lease holder
carries the resolution. This is the whole of what a `blocked` status meant, read from the edge instead
(`work_model.md#what-a-claim-predicate-treats-as-claimable`); a status beside the checkpoint was a second
held state that needed a process to clear, and `blocked` is retired as one (`vocabulary.md#retired-names`).

**An open step nobody has claimed raises a checkpoint after a declared interval, against its owner role.**
Nothing else bounds it: no lease exists on an unclaimed step, so nothing lapses, and pending is a
legitimate state, so no reader errors. The checkpoint's subject is one task of the step's batch (above) and
its reason class is `unclaimed_step`; it names the batch, the step, the `owner_role` declared for it, and how
long the step has been open,
and its `AWAITS` edge names the operator principal — an edge's target is a principal, and a role is not one
(`data_model.md#relationships`) — with the role named in its `needed_input`. It is routed against the
**owner role** rather than the batch because the
condition is an owner who is absent or does not exist, not work that is unimportant — routing it at the
role, which is what naming the role in `needed_input` does, is what makes an absent step owner legible as
one. The interval is declared on the step, as
`unclaimed_after` (`gates_and_workflows.md#declaration-batch-projection`), not inferred; an undeclared
interval raises no checkpoint. And the constraint above holds without exception:
the checkpoint alerts, and **it never signs**. It changes no verdict, closes no step, and its resolution is
the operator's decision about the role, not a clearance of the step — a step is closed by its owner's
sign-off or by the operator's `waived` sign-off, and by nothing else.

**A role that resolves to no principal is a declaration error, not only a claim-time failure.** A step
whose `owner_role` the roster resolves to nobody raises a checkpoint with reason `unspawnable_assignee`
(`work_model.md`) when a claim is attempted. That fires too late: whether a role resolves is knowable when
the workflow is **declared**, so the same check runs at declaration and the same reason class is raised
there. A workflow naming a role no roster resolves is a defect in the declaration, caught in the pull
request that introduces it, rather than a step that becomes permanently unsignable the first time someone
tries to claim it and errors nowhere in between.

## A task whose inputs cannot be resolved is put to the operator, not executed on a guess

**Ruled (decision 71, 2026-09-06).** Registered in `conformance.md#the-register-of-open-design-decisions`.
The operator asked what happens to a task that states what to do but not enough to do it, where the record
does not supply the remainder either — the illustration was an instruction to buy a television, carrying
neither which television nor what it must do, with nothing in the record that settles it. The design had the
mechanism and not the name: a checkpoint carrying `needed_input` already asks the operator for something and
holds the task, which is exactly the "sent back to the operator" the question describes; what no reason class
said was *this*, so a task in this condition had no legible way to be raised and would be raised, if at all,
under a class that means something else.

**This is not `undetermined_scope`, and the two must not be collapsed.** `undetermined_scope` is about a
[finding](vocabulary.md#finding): the swarm knows what the defect is and cannot tell how far the lesson
reaches — the batch alone, one step, the workflow, or the agent
(`gates_and_workflows.md#a-finding-is-one-off-or-standing-and-a-standing-one-obliges-a-change-to-what-produced-it`).
The question it puts to the operator is *how widely should this bind*. `underdetermined_inputs` is about a
task: the swarm knows exactly how far the work reaches and cannot tell **what to do inside it**. Its question
is *what am I acting on*. Run the swap test invariant 12 requires, on the corpus's own sentences rather than
on invented ones, and every one changes meaning: "a finding whose scope is not determinable from the finding
itself … is put to the operator as a checkpoint, reason `undetermined_scope`" becomes false under the
substitution, because that finding's inputs were never in doubt — it was recorded, it is legible, and only
its reach is open. In the other direction, a task whose required read resolved to nothing has a scope that is
not in question at all: buy a television is a perfectly determinate scope, and no amount of settling it
supplies the model. The two classes also hold different subjects in practice — one is raised on the
classification of something the swarm already produced, the other before the work is entered — and a reader
routing on the class needs them apart, because the operator answers them with different things: a scope, or a
fact.

**Sufficiency is judged at hydration, and what makes it mechanical is the declaration.** The phase that
resolves a step's declared reads is the phase positioned to see they came back with nothing
(`gates_and_workflows.md#declaration-batch-projection`), and it runs **before** the step opens, so the task
is stopped before a runner claims it, holds a lease, and discovers at the end of its reasoning that it never
had the inputs. That ordering is the reason for putting the judgement here rather than in the step owner: a
step owner's discovery is a real path and stays available, but making it the *only* path spends a claim and a
lease on work that was never enterable, and it makes the condition arrive as a runner's mid-step judgement
rather than as a property of the declaration any reader can check.

The crux is that hydration resolves reads mechanically, while "is this enough to buy the right television" is
a judgement, and the design must not pretend the first can perform the second. It does not. **What hydration
decides is not adequacy; it is presence.** A step declares the types it must read to enter, and hydration
resolves them from the task's anchors and through adapters
(`workflows.md#what-link-attaches-and-what-it-leaves-to-hydration`). A required read that resolves to no
instance — nothing in the record, and no adapter able to import one — is mechanically detectable, needs no
taste, and is the case the memo's illustration actually is: a task naming a purchase whose declared
`purchase specification` read resolves to nothing is missing a stated input, not a subtle one. That, and only
that, is what hydration raises this class on. Adequacy proper — the specification exists and is too thin to
act on — is a judgement, it belongs to the principal doing the work, and it reaches the operator by the path
the design already has: the step owner holds on a condition discovered mid-flight, records the finding naming
what it cannot judge, and the hold ends at its `hold_bound` in a checkpoint
(`work_model.md#a-batch-may-hold-on-a-condition-discovered-mid-flight`, decision 13). So the answer is both
cases, split on a line that is real rather than convenient: **hydration catches the absent input, the step
owner catches the inadequate one**, and neither is asked to do the other's work.

The distinction that makes this enforceable is one the design already insists on. An empty read and a failed
read are different, and both are distinct from a read that returned something
(`gates_and_workflows.md#declaration-batch-projection`). A required read that **could not be made** is
`unknown`, holds the step bounded, and escalates as `undeclared_dependency` — the dependency is unreachable.
A required read that **was made and returned nothing** is not a failure of the record; it is the record
answering truthfully that the input does not exist, and no amount of retrying changes it. Routing the second
into `undeclared_dependency` would tell the operator a dependency is broken when nothing is broken, and would
attach a retry schedule to a condition no retry resolves. That is why this is its own class and not a
widening of that one.

**What the checkpoint carries, and what it must not do.** Its subject is the task, it holds it from claim
(above), and its `needed_input` names the read that resolved to nothing, the step that declared it, and what
would satisfy it. The options it offers are the operator's real ones — supply the input, narrow the task, or
close it — and it offers no option that lets the swarm proceed on an assumption. **The class never resolves
by an agent supplying the missing input from its own judgement.** An agent that infers which television the
operator meant has not resolved an underdetermined input; it has substituted its own preference for the
operator's and then acted on it with the operator's money, which is precisely the unaccountable work this
posture exists to prevent — and it would be recorded as though the operator had specified it. Where the swarm
can defensibly *propose*, it proposes: the checkpoint may carry candidate values as options, which the
operator's resolution then selects among, and the record shows the operator chose. Proposing an option and
taking it are different acts, and only the first is the swarm's here.

**Marking a read required is what arms this, and that is deliberate.** A step that declares a read it can
genuinely proceed without should not declare it required, and one that cannot should. The class therefore
inherits the declaration's own discipline: an undeclared read is a declaration error caught in the pull
request that introduced it, and a read declared required that turns out to be optional is a declaration
defect of the same kind, visible in the same place. No default supplies a requirement at runtime, because a
supplied default here would fail open — it would let a step enter with an input the declaration said it
needed (principle 5).

**The cost accepted** is that a task whose input the record does not hold reaches the operator, and the
operator's attention is the scarce input this design protects
(`principles.md#where-the-human-sits-what-it-protects-and-why-the-record-is-owned`). That is the right cost:
the alternative spends it worse, because an agent that guesses produces an outcome the operator must then
review, undo, or live with, and an action already taken is more expensive than a question asked. **What would
reopen it:** a class of task whose required reads resolve to nothing routinely and whose missing inputs the
operator answers the same way every time — which would argue for a `task_policy` the step declares as a read
of its own, so the standing answer is on the record instead of asked again, and not for letting the step
proceed without one.

## What a checkpoint does not absorb

Two things look like escalation and are not checkpoints.

**The halt.** A checkpoint is written to the record. The halt is the state in which nothing can be
written, so it cannot be a checkpoint; it is announced off-Neotoma (rule 2), and the conditions that
would have raised checkpoints during it (a lapse count reaching its cap, a deferral exhausted) are
announced the same way and become checkpoints when the record returns.

**Operator-only tasks.** A task whose action classes include `operator_only` is an ordinary task, claimed
by the operator-facing agent and carried to the operator on the task path
(`work_model.md#operator-only-tasks-are-claimed-by-the-operator-facing-agent`). Being operator-only is
not a failure to advance; the task raises a checkpoint only when an action inside it reaches the action
gate, which resolves `operator_only` to `NEVER` and holds the action (reason `gate_hold`).

## Refuse resume-by-replay where actions are consent-gated

A task whose lease lapsed, in a halt or otherwise, is re-claimed, not replayed. Re-executing pre-interrupt code repeats every outbound
effect that ran before the interrupt; with consent-gated sends and payments, that is a repeat send.
`durable_execution_substrate.md` already refuses deterministic replay for this reason; prior art adds
LangGraph's resume semantics (the runtime restarts the whole node) as the pattern to refuse. The
consequence for the work model is that at-least-once delivery is the assumption and effect dedup is
mandatory (`work_model.md`).

## Contradictions this document settles

**C5, the gate-state plan's body versus its decisions.** The plan body argues for a hardcoded step list as
a floor the data may add to; the decisions map records `hardcoded_floor_proposal_is_retired`. Resolved: the
retraction stands. Under a hard dependency an unreadable workflow means no step of it is claimed and the
tasks are checkpointed, so a code-side fallback has nothing to fall back for. The constants are consolidated as a correctness fix
(`gates_and_workflows.md`), not as an availability fallback. A reader of the plan body gets the retracted
design; this document is the corrected statement.

**C17, hard dependency versus advisory grant checks.** A grant checker that permits when the policy source
is unreachable, or when no grant is recorded, and a tool proxy that passes every call through when no
identity is configured, are each wrong under the operator decision. An unreachable policy source is an
`Indeterminate` decision, and the posture for `Indeterminate` is deny (prior art: XACML, Cedar). Any
docstring calling such a path "advisory" is superseded by this document; `authority_model.md` states the
enforcement-point rule, and `status.md` lists the paths that still fail open.

## Prior art

OTP supervision supplies the rule for repeated failure: more than `MaxR` restarts in `MaxT` seconds and
the supervisor stops itself; Ateles shares bounded-retry-then-checkpoint, not the in-process tree.
XACML's `Indeterminate` and Cedar's default-deny are the posture for an unreachable policy source. SQS and
`SKIP LOCKED` both assume at-least-once, which is why rule 4 leaves the prior state rather than writing a
guess. Sources: `ent_08460968e6f49dac21510f4a`.
