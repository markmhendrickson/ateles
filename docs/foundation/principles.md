# Principles: the invariants

**Kernel document:** read on every review (`conformance.md`). **Kind:** foundation; states the design and
never the state of a checkout. **Derived from:** synthesis `ent_b0ce322f768e4fc676b73139` (phase 0 of plan
`ent_533d4ec2f7bfb60f66fb3fce`), prior art `ent_08460968e6f49dac21510f4a` (phase 3), ateles#727, and the
decision keys cited per invariant. Which mechanisms exist on a given checkout, and where nothing fires, is
measured in `status.md`, not here.

## Purpose

State the invariants issue-based work in this repository conforms to, and for each the kind of mechanism
that makes it bind. An invariant with no enforcing mechanism is applied by a reviewer by hand, and the first
invariant says why that difference must never be papered over. The arch gate and the review lenses read
this document on every review.

## Scope

Ten invariants. The first six are the rules ateles#727 put in `CLAUDE.md` for interactive sessions
(throughput plan `ent_18b902cf72822373f9da8ced`, decision
`verification_discipline_principles_and_where_they_bind`), which do not bind dispatched agents; this is the
copy the gates read. The last four are consolidated from stored decisions. Authority and delegation
invariants are `authority_model.md`; the posture when the record is unreachable is `failure_posture.md`.

**Enforced by** names the class of mechanism that makes the invariant a control. Whether an instance of
that class exists, and which instances are missing, is `status.md`.

## The invariants

### 1. A mechanism that does not bind is not a control

Before treating a linter, gate, review, verdict, or status as enforcement, name the thing that fails when it
is violated. If nothing fails, it is reporting. A queue of approval requests that nothing consumes is a
report. Sources: #727 rule 1; synthesis PR-14 (analysis `ent_8104c890c581ccf9094eab25`).

**Enforced by:** a blocking CI step per linter (`continue-on-error` is the anti-pattern); a review that
names, for every proposed control, what it stops. The foundation binding (`conformance.md`) applies this to
documents: a listed document not on disk is reported as not yet written, never assumed to bind.

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

**Enforced by:** review. No mechanical form is known.

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
`keep_the_name_workflow_definition`; agent_policy `ent_4d34c6f96312be686f572add`.

**Enforced by:** the prior-art contract on the dispatched-prompt path (`SWARM_PRIOR_ART_CONTRACT`) and the
design-basis check on every issue and PR (`conformance.md`).

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
in `status.md` carries its as-of date and instrument, and that document is regenerated, not maintained.

### 9. One source, defined once; a comment claiming parity is not parity

A value the swarm reads has one home. When a copy is needed for import hygiene, derive it at import time or
assert equality in a test. Sources: gate-state `four_divergent_copies_of_the_gate_set`; synthesis PR-07;
throughput `positive_design_rules_strengthen_existing_ones_not_new_ones`; the no-transition-log and
no-dispatch-log decisions are the same rule applied to history.

**Enforced by:** a hardcoded-config linter in the lint path; a parity test wherever a copy is unavoidable;
the direction-of-truth table in `conformance.md`.

### 10. Handing work to the swarm is not completion

Whoever hands work to the swarm owns it through merge and release; "shipped" is an open PR, "landed" is on
main and in the deployed checkout. Sources: agent_policy `ent_5456a8a2224d8211ef33749c`; throughput
`shipped_is_not_landed_for_this_plan`; synthesis PR-18, PR-19.

**Enforced by:** the checkout-drift check at daemon start; the read-back of terminal release status
(invariant 2). A PR's path from open to deployed has no single tracker; the owning session is the mechanism.

## Contradictions this document settles

**C7, where rules live.** Four homes, chosen for four audiences: `agent_policy` entities (agent behaviour,
synced to skills), `CLAUDE.md` (re-injected after compaction, so it binds interactive sessions), this
directory (diffable, PR-reviewed, read by the gates), and a warning against a second document on one
subject. Resolved: these are different audiences, not copies. The #727 section states in its first line
that it does not bind dispatched agents; this document is what the gates read; the derivation is shared and
cited. Direction of truth per artifact class is in `conformance.md`. When the two diverge, this document is
wrong until a PR corrects it, and that PR is the review.

## Beyond the sources

Invariants 8, 9, and 10 are not in #727; they are consolidated from the decisions cited under each. The
grouping, the one-sentence statements, and the "Enforced by" mechanism classes are this document's.
