# Failure posture: what the swarm does when its record is unreachable

**Keyed document:** read when the reachability, drift, forensics, readiness, or Neotoma client paths change
(`conformance.md`). **Kind:** foundation; states the design and never the state of a checkout. **Derived
from:** synthesis `ent_b0ce322f768e4fc676b73139` (PR-12 to PR-15, C5, C17), prior art
`ent_08460968e6f49dac21510f4a`, task `ent_670cacab2f46fd9547ced7ed` (operator-approved), gate-state plan
`ent_4222e5d52edd9bdba7b78cc1` decisions `neotoma_is_a_hard_dependency_swarm_halts`,
`halt_work_but_never_stop_observing`, `the_halt_must_announce_itself_off_neotoma`,
`reachability_check_belongs_at_dispatch_with_mid_task_writes_failing_closed`,
`deferral_must_be_bounded_and_escalate_off_neotoma`, `unknown_must_stay_distinct_from_a_verdict`,
`nyctea_635_becomes_load_bearing`, and PR #745 operator review (2026-09-04). What is built is `status.md`;
how a checkpoint is recorded is `data_model.md`.

## Purpose

State the operator's decision that Neotoma is a hard dependency and the seven rules that follow from it, so
the posture lives where the review lenses read it rather than in a task description; state which
failures the swarm escalates as checkpoints on a task, and which it does not. `durable_execution_substrate.md`
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
   lapse cap, checkpoint on exhaustion), connected to the gating path rather than rebuilt.

6. **Every write is read back.** Principle 2 of `principles.md`, restated here because an outage is when a
   write most plausibly reports success without landing: a store can return 200 with a warning and persist
   the row without the field, and an idempotency-mismatch error is stronger proof of a prior commit than a
   success response (digest `ent_b31cad6074f79e8adfa6b2aa`).

7. **Unknown stays distinct from a verdict.** A failed read is not a negative result. Any reader of gate,
   grant, or drift state carries a third value; an error is never coerced to pending or to clear. Principle
   7 of `principles.md`; at a policy enforcement point the third value resolves to deny (`authority_model.md`).

## Repeated lapse raises a checkpoint

A lease that lapses is not returned by anything; the task is simply claimable again (`work_model.md`).
The rule that survives the retired reaper is about repetition: the watchdog counts lapses per task,
with bounded backoff between re-claims, and when one task's count reaches the cap it escalates the task:
one checkpoint, subject the task, reason `repeated_lapse`, carrying the count and the last claimants,
rather than letting the task be re-claimed forever. The watchdog observes and escalates; it holds no
authority over any lease and never chooses the next claimant. During a halt the count still accrues,
because a lapse during an outage is still a lapse; the checkpoint is written when the record returns, and
until then the condition is announced on the off-Neotoma path (rule 2). This is OTP's supervisor rule
(prior art, below) applied to a lease.

## Checkpoints on tasks: one queue, one protocol

To escalate is to raise a checkpoint on a task the swarm cannot advance. The checkpoint is the same
entity the action gate writes when it holds an action (`gates_and_workflows.md#the-checkpoint`); only
its subject and its reason class differ. The reason classes the design names: `gate_hold` (an action held
at the gate), `repeated_lapse` (above), `unreadable_workflow` (`gates_and_workflows.md`, no step opened),
`rounds_exhausted` (rule 5, or a declared cap on an `on_fail` loop), `unspawnable_assignee`
(`work_model.md`, an `assigned_to` nobody can run); a policy may declare more. A checkpoint on a task
carries the reason, the needed input, the options, and whom it awaits, and is presented and resolved
through the one decision queue, by the one resolution protocol, that checkpoints on actions use. Do not
build a second gate, a second queue, or a second notification path for task-level failure (principle 6):
a queue nobody consumes is a report, not a control (principle 1).

**Escalation reorders; it never signs.** A checkpoint raised on a step nobody has claimed, or on a task
the swarm cannot advance, changes the order in which claimable work is offered and what a principal
attends to first. It changes no verdict and closes no step. A step blocked on a step owner who never
acted is a liveness condition, not an ordering one, and reordering the queue cannot release it: the step
is closed by that step owner's sign off or it stays open. **No step is closed by elapsed time.** A gate that expires into
a pass is a gate that fails open on a timer, which is the shape this model exists to remove — the whole
value of a required step is that its absence is visible, and a timer converts that absence into a silent
permit. So a checkpoint raised on an unclaimed step names the step, its owner role, and how long it has
been open, and stops there; whether an interval raises such a checkpoint at all is an open decision, but
no form of it may advance the step.

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
