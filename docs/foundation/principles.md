# Principles: the invariants

**Kernel document:** read on every review (`conformance.md`). **Kind:** foundation; states the design and
never the state of a checkout. **Derived from:** synthesis `ent_b0ce322f768e4fc676b73139` (phase 0 of plan
`ent_533d4ec2f7bfb60f66fb3fce`), prior art `ent_08460968e6f49dac21510f4a` (phase 3), ateles#727, the
decision keys cited per invariant, and PR #745 operator review (2026-09-04), and the operator's 2026-09-05 review of review relevance (revision 19: the `applies_when` condition on an optional step, and two terms retired in favour of `review step`). Revised by the testability pass of 2026-09-06 (revision 37: invariants 3, 6, 8, and 10 given their mechanical form — the named instruments, the singletons' closure, the citation lint, and "landed" as a derived read). Which mechanisms exist on a given checkout, and where nothing fires, is
measured in `status.md`, not here.

## Purpose

State the invariants issue-based work in this repository conforms to, and for each the kind of mechanism
that makes it bind. An invariant with no enforcing mechanism is applied by a reviewer by hand, and the first
invariant says why that difference must never be papered over. The arch review step and the other review steps read
this document on every review.

## Scope

Eleven invariants. The first six are the rules ateles#727 put in `CLAUDE.md` for interactive sessions
(throughput plan `ent_18b902cf72822373f9da8ced`, decision
`verification_discipline_principles_and_where_they_bind`), which do not bind agents the swarm runs; this
is the copy the review steps read. The last five are consolidated from stored decisions and the operator review of
this foundation. Authority and delegation
invariants are `authority_model.md`; the posture when the record is unreachable is `failure_posture.md`.

**Enforced by** names the class of mechanism that makes the invariant a control. Whether an instance of
that class exists, and which instances are missing, is `status.md`.

## The invariants

### 1. A mechanism that does not bind is not a control

Before treating a linter, gate, review, verdict, or status as enforcement, name the thing that fails when it
is violated. If nothing fails, it is reporting. A queue of checkpoints that nothing consumes is a
report. Sources: #727 rule 1; synthesis PR-14 (analysis `ent_8104c890c581ccf9094eab25`).

**Enforced by:** a blocking CI step per linter (`continue-on-error` is the anti-pattern); a review that
names, for every proposed control, what it stops. The foundation binding (`conformance.md`) applies this to
documents: a listed document not on disk is reported as not yet written, never assumed to bind. And the
placement test for a rule itself: a rule binds where it is read at the moment of the action it governs
(C7, below), so a control written to a surface nobody reads at that moment is reporting, however
prominently it is placed.

### 2. A write that reports success has not necessarily happened; read it back

After any write that matters, retrieve it and assert the field holds the value written. A 2xx or
`success: true` is not evidence. Sources: #727 rule 2; architecture plan `ent_99ace4dd6673aa36ed08b1fe`
`release_terminal_status_must_be_read_back`; throughput
`acceptance_criteria_must_name_the_evidence_not_the_intent`; synthesis PR-15. The claim primitive's own
correction (task `ent_da60df3beccb675ef8c8c0c5`): two stores against one canonical key both returned success,
and only a read-back showed the second overwrote the first.

**Enforced by:** a read-back after every write that carries a decision, asserting the field holds the value;
a linter for each known success-without-landing shape (an MCP tool name used as a REST route; an undeclared
field the store accepts into `raw_fragments` with a 200).

### 3. Validate the instrument before believing the measurement

A zero, an empty result, or a silent pass is a claim about the tooling before it is a claim about the world.
Prove the instrument non-zero on a known-positive case; segment the population before attributing a rate;
state the timezone of a search window. Sources: #727 rule 3; synthesis PR-16.

**Enforced by:** a planted positive per instrument, and the instruments are named. The swarm's instruments
are the counters and coverages the design already defines — drops per window, with the dispositions counted
beside them (`adapters.md#what-the-adapter-does-with-every-event`); lapses per task
(`failure_posture.md#repeated-lapse-raises-a-checkpoint`); blocked claims per window
(`failure_posture.md#the-rules`, rule 2); and the coverage on every adapter observation
(`data_model.md#record-conventions`) — and each is proved non-zero on a known-positive case before a zero
read from it is believed. An instrument with no planted positive is one whose zero is not evidence; the
conformance suite applies the same rule to its own three
(`conformance_suite.md#the-suite-validates-its-own-instruments`), and a counter the design adds is added to
this list or it is not an instrument.

### 4. A test that cannot fail on the thing it watches is decoration

Before offering a test as proof of a fix, revert the fix and confirm the test goes red; say in the PR what
red looked like. A test written against current behaviour ratifies the bug. Sources: #727 rule 4; throughput
`acceptance_criteria_must_name_the_evidence_not_the_intent` (b).

**Enforced by:** review; the practice is the revert result recorded in the PR body. No linter distinguishes
an assertion that pins a bug from one that pins a fix.

### 5. Fail closed on the field that carries the safety meaning

When a value is absent, unrecognized, or malformed, the default is the restrictive branch, for the field that
encodes the risk. Add a safety value to both sides of a classification and test that the default branch is
restrictive. Sources: #727 rule 5; architecture plan
`operator_only_is_never_auto_executable_not_merely_high_blast`, `unclassified_action_type_fails_closed_and_loudly`;
synthesis PR-20. Prior art: Cedar (zero permits is deny; forbid wins).

**Enforced by:** the restrictive branch as the default of every classifier that carries a safety meaning
(`gates_and_workflows.md`: `operator_only` resolves to `NEVER` ahead of any policy; a declared but
unclassified action type resolves to `NEVER`; a parity test holds the advisory and enforcing copies equal);
default-deny at every policy enforcement point (`authority_model.md`).

### 6. Extend the mechanism that already generalizes; do not build a parallel one

Search the code, not memory of it, before building; reuse the existing entity or relationship type; keep a
name that is already accurate. Sources: #727 rule 6; throughput `gate_machinery_is_already_pr_independent`
(the consent gate for outbound content already existed); gate-state plan `ent_4222e5d52edd9bdba7b78cc1`
`keep_the_name_workflow_definition` (cited for the rule, keep an accurate name; its particular example is
reversed in `gates_and_workflows.md`, because the name was not accurate); agent_policy
`ent_4d34c6f96312be686f572add`.

**Enforced by:** the prior-art contract in every runner's prompt (`SWARM_PRIOR_ART_CONTRACT`) and the
design-basis check on every issue and PR (`conformance.md`) — both of which bind at review, weakly. What
binds mechanically is the registry's closure over the design's singletons, named once, here: one decision
queue (`checkpoint`), one gate (the action gate), one lease primitive (`LEASE`), one succession edge
(`FOLLOWS`), one engine that opens steps, one home for step state (the sign-off, projected as
`step_status`), and one record of an intended effect (`action`). A registered second type for any of them —
a held-decision type beside `checkpoint`, a claim-history type, a per-step status row, a transition-event
type — is the parallel mechanism this invariant forbids, and a census of the registry
(`data_model.md#concepts`) is the check that fails on it.

### 7. Unknown stays distinct from a verdict

"We could not tell" and "we can tell and it is bad" are different claims. A reader of gate, grant, drift, or
reachability state carries a third value and never coerces an error to pending or to clear. Sources:
gate-state `unknown_must_stay_distinct_from_a_verdict`; synthesis PR-12 (a failed read resolved three
different ways across the swarm). Prior art: XACML's `Indeterminate` decision.

**Enforced by:** a third value in every such reader; at a policy enforcement point, `Indeterminate` resolves
to deny (`authority_model.md`), never to permit.

### 8. Every figure carries its date and its instrument; re-measure before acting

Stored diagnoses and counts decay. A principle is checked against `origin/main`, not session notes; an open
issue is not evidence a defect is live until main and the deployed checkout are read; a merged PR is not
evidence it is fixed until the deployed checkout moves. Sources: synthesis PR-17, C15, C16; throughput
`three_principle_candidates_rejected_on_verification`.

**Enforced by:** this foundation's own split: no implementation state in a foundation document; every figure
in `status.md` carries its as-of date and instrument, and that document is regenerated, not maintained. The
split has a syntactic form a lint reads, stated once in `conformance.md#phases-and-implementation-state`: a
commit hash appears in no foundation document but `status.md`, and an issue or pull-request number appears
only where a document names what it derived from.

### 9. One source, defined once; a comment claiming parity is not parity

A value the swarm reads has one home. When a copy is needed for import hygiene, derive it at import time or
assert equality in a test. Sources: gate-state `four_divergent_copies_of_the_gate_set`; synthesis PR-07;
throughput `positive_design_rules_strengthen_existing_ones_not_new_ones`; the no-transition-log and
no-assignment-log decisions are the same rule applied to history.

**Enforced by:** a hardcoded-config linter in the lint path; a parity test wherever a copy is unavoidable;
the direction-of-truth table in `conformance.md`.

### 10. Handing work to the swarm is not completion

Whoever hands work to the swarm owns it through merge and release; "shipped" is an open PR, "landed" is on
main and in the deployed checkout. Sources: agent_policy `ent_5456a8a2224d8211ef33749c`; throughput
`shipped_is_not_landed_for_this_plan`; synthesis PR-18, PR-19.

**Enforced by:** the read-back of terminal release status (invariant 2). The checkout-drift check at daemon
start is **reporting, not enforcement**, and is named here as such: it logs the divergence and the daemon
runs anyway, so nothing fails when the invariant is violated, and by invariant 1's own test a check that
cannot fail is a report. That is a deliberate choice rather than a defect — a guard that halts every daemon
on a stale checkout causes a larger outage than the drift it prevents — but it means drift is *detected*
and never *prevented*, and a report only binds a reader who acts on it. What binds is the release
read-back, which fails on a release that did not reach its terminal state. A PR's path from open to
deployed has one tracker, the task's chain: "landed" is a derived read — the chain ended under a declaration
that permits its ending there, a `release` batch's `verify_deployed` signed or a code workflow whose
declaration permits closing with none because the project deploys its default branch
(`work_model.md#a-task-is-executed-only-through-a-workflow`) — and a task that reads terminal after a
`merge_pr` under a declaration that permits no such end is the failing artefact. The owning session is still
who acts on that read; it is not what makes it true.

### 11. State that needs a watchdog belongs in a relationship, not a field

Prefer representing state as a relationship to another entity over a field on the entity, and insist on it
wherever a field would need a watchdog, a sweeper, or a reconciler to stay correct. A field asserts; an edge
with its own timestamps is read, and what is read cannot go stale because the process that would have
updated it died. The lease is the canonical case: an edge from principal to task with `expires_at`, whose
`held` or `lapsed` state is derived at read time, needs no process to expire it; a `claimed_by` field on
the task needs one. A task's attachment to a batch, parent and child, the attachment of an action or an
artifact to its task, a checkpoint's link to its subject, and a step's state within a batch (open,
claimed, or signed, read from the batch, a lease, and a sign-off) are edges for the same reason
(`work_model.md`, `gates_and_workflows.md`, `data_model.md`). Sources: PR #745 operator review;
synthesis PR-02 and PR-05 (liveness derived, no assignment log) are earlier instances of the same rule.

**Enforced by:** review of any schema change that adds a field whose correctness depends on a process
staying alive; the design-basis check names this invariant when such a field is proposed.

## Contradictions this document settles

**C7, where rules live.** Four homes, chosen for four audiences: `agent_policy` entities (agent behaviour,
synced to skills), `CLAUDE.md` (re-injected after compaction, so it binds interactive sessions), this
directory (diffable, PR-reviewed, read by the review steps), and a warning against a second document on one
subject. Resolved: these are different audiences, not copies. The #727 section states in its first line
that it does not bind agents the swarm runs; this document is what the review steps read; the derivation is shared and
cited. Direction of truth per class of record is in `conformance.md`. When the two diverge, this document is
wrong until a PR corrects it, and that PR is the review.

**The axis that separates the four homes, and it decides which of them binds:** *a rule binds where it is
read at the moment of the action it governs.* Four homes are four audiences, but they are not four equal
controls, and the difference is when the text is read against when the thing it governs happens. Read the
four against that axis:

| Surface | When it is read | What that makes it |
|---|---|---|
| Code and hooks | at the action, every time | binding — the rule and the action are the same event |
| A tool's point of use | in the result the caller is reading, on the condition that fires it | binding — it arrives in the loop, at the moment the caller can still act on it |
| An agent's prompt | once, when the agent starts | weakly binding — identity and judgement, not enforcement; distance grows with every turn after |
| `CLAUDE.md` | re-injected after compaction, in interactive sessions only | binding for that surface alone; it reaches no agent the swarm runs and no daemon |

Always-present instruction text at the top of a context window is not an exception to this, it is the
clearest case of it: a rule can sit in context for a whole session, be violated repeatedly, and be
corrected by hand each time — the text was present and was never read *at the action*. That is principle 1
turned on the foundation's own surfaces, and principle 4 with it: a control that could not have failed the
session it was meant to govern is decoration. So a rule that must bind goes where it will be read when the
action is taken, and putting it somewhere more visible instead is not a substitute.

The one deliberate exception, recorded as the narrow thing it is: **a rule may live in `CLAUDE.md` when
its audience is interactive sessions and compaction survival is the binding property.** That is a real
reason on the axis above — for that surface, re-injection is when the rule is read — and it is not a
general licence, because the same text reaches no agent the swarm runs.

## Beyond the sources

Invariants 8, 9, 10, and 11 are not in #727; they are consolidated from the decisions cited under each. The
grouping, the one-sentence statements, and the "Enforced by" mechanism classes are this document's.
