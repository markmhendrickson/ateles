# Failure posture: what the swarm does when its record is unreachable

**Vision phase:** P1 (governed execution for one principal). **Kind:** consolidation, not design.
**Derived from:** synthesis `ent_b0ce322f768e4fc676b73139` (PR-12 to PR-15, C5, C17), prior art
`ent_08460968e6f49dac21510f4a`, task `ent_670cacab2f46fd9547ced7ed` (approved; implemented as ateles#714,
open), gate-state plan `ent_4222e5d52edd9bdba7b78cc1` decisions `neotoma_is_a_hard_dependency_swarm_halts`,
`halt_work_but_never_stop_observing`, `the_halt_must_announce_itself_off_neotoma`,
`reachability_check_belongs_at_dispatch_with_mid_task_writes_failing_closed`,
`deferral_must_be_bounded_and_escalate_off_neotoma`, `unknown_must_stay_distinct_from_a_verdict`, and
`nyctea_635_becomes_load_bearing`. Code read on `origin/main` at `496bab3`, 2026-09-02.

## Purpose

State the operator's decision that Neotoma is a hard dependency and the seven rules that follow from it, so
the posture lives where the gates read it rather than in a task description. `durable_execution_substrate.md`
records the position on execution engines and replay that this document relies on; it stays.

## Scope

Every daemon and every dispatched agent, on the task path and off it. What is on main is stated as such:
the forensic capture and the drift check are on main; the halt itself is ateles#714, open.

## The decision

**Neotoma is the swarm's foundational level of truth. If it cannot be accessed, the swarm does not do
work.** Not degraded operation, not a hardcoded fallback. The reasoning is stronger than availability: a
swarm that operates while its record is unreachable produces work with no record. On 2026-09-01/02 agents
completed real work whose task entities never landed, salvaged only because a human relayed it into GitHub
by hand. Across 18 unattended daemons that is unaccountable work, worse than the work not happening, because
the swarm then acts on a history it cannot reconstruct. This decision superseded degrade-on-capability,
refuse-and-requeue-as-fallback, and the hardcoded gate floor (C5, below).

## The rules

1. **Halt work; never stop observing.** No dispatch, no gate decisions, nothing claimed complete without a
   record. Watchdogs, forensic capture, health checks, and alerting stay live. A diagnostic capture asserts
   nothing about the record, so it does not require the record; a hard dependency that stops the thing
   diagnosing it makes recovery impossible. On main: `lib/neotoma_forensics.py` writes to local disk
   "always, unconditionally", its docstring noting that writing evidence of a Neotoma outage into Neotoma is
   the wrong move.

2. **Announce the halt off-Neotoma, on entering and on leaving.** A silently halted swarm is
   indistinguishable from an idle one, this codebase's signature failure. The announcement travels a path
   that survives the outage (the Telegram path `lib/notify/notifier.py` already carries), aggregated per
   window, never one page per blocked dispatch (`lib/notify` has no rate limiting of its own; ateles#645).

3. **The reachability check is a real read at dispatch, never `/health`.** One probe per work item, not per
   operation. A health endpoint has returned green while every read hung on a wedged database. ateles#635
   (judge Neotoma by a real read) is open and load-bearing under this posture, since it detects the
   condition that halts everything.

4. **A mid-task write failure leaves the task in its prior state.** The agent is already running when the
   record goes away. It does not abandon in-flight work, and it never claims a completion it cannot record:
   finish the reasoning, attempt the write, and on failure leave the task for the watchdog to requeue. A
   sign-off whose write failed is the unaccountable work this posture exists to prevent. Silence beats a
   false verdict.

5. **Deferral is bounded and escalates off-Neotoma.** Backoff is mandatory: the instance answered in 20 to
   30 seconds with intermittent 502s precisely under retry pressure, and retrying harder is how slow becomes
   unreachable. Unreachable stays distinguishable from slow. A task requeued indefinitely against a store
   that never returns is a silent stall, so the deferral has a ceiling and its terminal escalation travels
   Telegram or local disk. The drain is the existing `TaskWatchdog` (backoff, `MAX_ATTEMPTS`,
   escalate-on-exhaustion), connected to the gating path rather than rebuilt.

6. **Every write is read back.** Principle 2 of `principles.md`, restated here because an outage is when a
   write most plausibly reports success without landing: `/store` returns 200 with a warning and stores the
   row without the field; `ERR_IDEMPOTENCY_MISMATCH` is stronger proof of a prior commit than a success
   response (digest `ent_b31cad6074f79e8adfa6b2aa`).

7. **Unknown stays distinct from a verdict.** A failed read is not a negative result. Any reader of gate,
   grant, or drift state carries a third value; an error is never coerced to pending or to clear. Principle 7
   of `principles.md`; the fail-open paths that violate it are in `authority_model.md`.

## Refuse resume-by-replay where actions are consent-gated

A halted or expired task is re-claimed, not replayed. Re-executing pre-interrupt code repeats every outbound
effect that ran before the interrupt; with consent-gated sends and payments, that is a repeat send.
`durable_execution_substrate.md` already refuses deterministic replay for this reason; prior art adds
LangGraph's resume semantics (the runtime restarts the whole node) as the pattern to refuse. The
consequence for the work model is that at-least-once delivery is the assumption and effect dedup is
mandatory (`work_model.md`).

## Contradictions this document touches

**C5, the gate-state plan's body versus its decisions.** The plan body argues for `PRE_IMPL_GATES` as a floor
the data may add to; the decisions map records `hardcoded_floor_proposal_is_retired`. Resolved: the
retraction stands. Under a hard dependency an unreadable workflow means no dispatch, so a code-side fallback
has nothing to fall back for. The constants still need consolidating as a correctness fix
(`gates_and_workflows.md`), not as an availability fallback. A reader of the plan body gets the retracted
design; this document is the corrected statement.

**C17, hard dependency versus advisory grant checks.** `grant_checker.py` documents itself as permissive when
Neotoma is unreachable and when no grants are recorded ("advisory in Phase 5", ateles#560), and
`mcp_tool_grant_proxy/proxy.py` passes through when no identity is configured. Resolved: under the operator
decision both are wrong. An unreachable policy source is an `Indeterminate` decision, and the posture for
`Indeterminate` is deny (prior art: XACML, Cedar). The "Phase 5 advisory" docstring is superseded by this
document; the code is not yet changed. `authority_model.md` carries the full list of paths.

## Prior art

OTP supervision supplies the escalation rule for repeated failure: more than `MaxR` restarts in `MaxT`
seconds and the supervisor stops itself; Ateles shares bounded-retry-then-escalate, not the in-process tree.
XACML's `Indeterminate` and Cedar's default-deny are the posture for an unreachable policy source. SQS and
`SKIP LOCKED` both assume at-least-once, which is why rule 4 leaves the prior state rather than writing a
guess. Sources: `ent_08460968e6f49dac21510f4a`.

## Status on main

`lib/neotoma_forensics.py` and `lib/daemon_runtime/checkout_drift.py` are on main. The halt, the
reachability probe (`lib/daemon_runtime/neotoma_reachability.py`), and the off-Neotoma announcement are
ateles#714, open. ateles#635 is open. The grant paths in C17 are unchanged. `conformance.md` keys
`neotoma_reachability` to this document so the review that lands #714 reads it.
