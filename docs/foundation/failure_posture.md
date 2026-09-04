# Failure posture: what the swarm does when its record is unreachable

**Keyed document:** read when the reachability, drift, forensics, readiness, or Neotoma client paths change
(`conformance.md`). **Kind:** foundation; states the design and never the state of a checkout. **Derived
from:** synthesis `ent_b0ce322f768e4fc676b73139` (PR-12 to PR-15, C5, C17), prior art
`ent_08460968e6f49dac21510f4a`, task `ent_670cacab2f46fd9547ced7ed` (operator-approved), gate-state plan
`ent_4222e5d52edd9bdba7b78cc1` decisions `neotoma_is_a_hard_dependency_swarm_halts`,
`halt_work_but_never_stop_observing`, `the_halt_must_announce_itself_off_neotoma`,
`reachability_check_belongs_at_dispatch_with_mid_task_writes_failing_closed`,
`deferral_must_be_bounded_and_escalate_off_neotoma`, `unknown_must_stay_distinct_from_a_verdict`,
`nyctea_635_becomes_load_bearing`, and PR #745 operator review (2026-09-04). What is built is `status.md`.

## Purpose

State the operator's decision that Neotoma is a hard dependency and the seven rules that follow from it, so
the posture lives where the review lenses read it rather than in a task description. `durable_execution_substrate.md`
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
   record goes away. It does not abandon in-flight work, and it never claims a completion it cannot record:
   finish the reasoning, attempt the write, and on failure leave the task as it was: the lease lapses on
   its own and the task is claimable again (`work_model.md`), with no process needed to return it. A
   sign-off whose write failed is the unaccountable work this posture exists to prevent. Silence beats a
   false verdict.

5. **Deferral is bounded and escalates off-Neotoma.** Backoff is mandatory: a slow instance under retry
   pressure is how slow becomes unreachable, and unreachable stays distinguishable from slow. A task requeued
   indefinitely against a store that never returns is a silent stall, so the deferral has a ceiling and its
   terminal escalation travels a path that survives the outage. The drain is the lapse rule below
   (backoff, lapse cap, escalate-on-exhaustion), connected to the gating path rather than rebuilt.

6. **Every write is read back.** Principle 2 of `principles.md`, restated here because an outage is when a
   write most plausibly reports success without landing: a store can return 200 with a warning and persist
   the row without the field, and an idempotency-mismatch error is stronger proof of a prior commit than a
   success response (digest `ent_b31cad6074f79e8adfa6b2aa`).

7. **Unknown stays distinct from a verdict.** A failed read is not a negative result. Any reader of gate,
   grant, or drift state carries a third value; an error is never coerced to pending or to clear. Principle
   7 of `principles.md`; at a policy enforcement point the third value resolves to deny (`authority_model.md`).

## Repeated lapse escalates

A lease that lapses is not returned by anything; the task is simply claimable again (`work_model.md`).
The rule that survives the reaper's retirement is about repetition: the watchdog counts lapses per task,
with bounded backoff between re-claims, and when one task's count reaches the cap it raises one
`escalation` rather than letting the task be re-claimed forever. The watchdog observes and escalates; it
holds no authority over any lease and never chooses the next claimant. During a halt the count still
accrues, because a lapse during an outage is still a lapse, and the escalation travels the off-Neotoma
path (rule 2). This is OTP's supervisor rule (prior art, below) applied to a lease.

## Refuse resume-by-replay where actions are consent-gated

A task whose lease lapsed, in a halt or otherwise, is re-claimed, not replayed. Re-executing pre-interrupt code repeats every outbound
effect that ran before the interrupt; with consent-gated sends and payments, that is a repeat send.
`durable_execution_substrate.md` already refuses deterministic replay for this reason; prior art adds
LangGraph's resume semantics (the runtime restarts the whole node) as the pattern to refuse. The
consequence for the work model is that at-least-once delivery is the assumption and effect dedup is
mandatory (`work_model.md`).

## Contradictions this document settles

**C5, the gate-state plan's body versus its decisions.** The plan body argues for a hardcoded gate list as
a floor the data may add to; the decisions map records `hardcoded_floor_proposal_is_retired`. Resolved: the
retraction stands. Under a hard dependency an unreadable workflow means no step of it is claimed, so a
code-side fallback has nothing to fall back for. The constants are consolidated as a correctness fix
(`gates_and_workflows.md`), not as an availability fallback. A reader of the plan body gets the retracted
design; this document is the corrected statement.

**C17, hard dependency versus advisory grant checks.** A grant checker that permits when the policy source
is unreachable, or when no grant is recorded, and a tool proxy that passes every call through when no
identity is configured, are each wrong under the operator decision. An unreachable policy source is an
`Indeterminate` decision, and the posture for `Indeterminate` is deny (prior art: XACML, Cedar). Any
docstring calling such a path "advisory" is superseded by this document; `authority_model.md` states the
enforcement-point rule, and `status.md` lists the paths that still fail open.

## Prior art

OTP supervision supplies the escalation rule for repeated failure: more than `MaxR` restarts in `MaxT`
seconds and the supervisor stops itself; Ateles shares bounded-retry-then-escalate, not the in-process tree.
XACML's `Indeterminate` and Cedar's default-deny are the posture for an unreachable policy source. SQS and
`SKIP LOCKED` both assume at-least-once, which is why rule 4 leaves the prior state rather than writing a
guess. Sources: `ent_08460968e6f49dac21510f4a`.
