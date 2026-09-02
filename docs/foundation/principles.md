# Principles: the invariants, and what fires each one

**Vision phase:** P1 (governed execution for one principal). **Kind:** consolidation, not design.
**Derived from:** synthesis `ent_b0ce322f768e4fc676b73139` (phase 0 of plan `ent_533d4ec2f7bfb60f66fb3fce`),
prior art `ent_08460968e6f49dac21510f4a` (phase 3), ateles#727, and the decision keys cited per invariant.
Code claims checked on `origin/main` at `496bab3`, 2026-09-02.

## Purpose

State the invariants issue-based work in this repository conforms to, each with the mechanism that fires
when it is violated. Where nothing fires, the entry says "nothing": that invariant is applied by a reviewer
by hand, and implying otherwise would violate the first invariant. The arch gate and the review lenses read
this document on every review (`conformance.md`).

## Scope

The P1 substrate. Ten invariants: the first six are the rules ateles#727 put in `CLAUDE.md` for interactive
sessions (throughput plan `ent_18b902cf72822373f9da8ced`, decision
`verification_discipline_principles_and_where_they_bind`), which do not bind dispatched agents; this is the
copy the gates read. The last four are what the synthesis added from stored decisions. Authority and
delegation invariants are in `authority_model.md`.

**Fires:** names what fails on `origin/main` today. "Partial" means one instance of the class is enforced,
not the class. "Nothing" means review.

## The invariants

### 1. A mechanism that does not bind is not a control

Before treating a linter, gate, review, verdict, or status as enforcement, name the thing that fails when it
is violated. If nothing fails, it is reporting. Sources: #727 rule 1; synthesis PR-14 (analysis
`ent_8104c890c581ccf9094eab25`); 49 of 49 sampled merge-approval `checkpoint_brief`s from June to July 2026
still `open`, a queue nothing consumed.

**Fires:** partial. `check_agent_roster.py` blocks in `ateles-tests.yml`. Nothing catches a new
`continue-on-error: true` step (two live in `agent-config-validation.yml`). The foundation binding (#744)
applies this invariant to documents: a listed document not on disk is reported as "not yet written".

### 2. A write that reports success has not necessarily happened; read it back

After any write that matters, retrieve it and assert the field holds the value written. A 2xx or
`success: true` is not evidence. Sources: #727 rule 2; architecture plan `ent_99ace4dd6673aa36ed08b1fe`
`release_terminal_status_must_be_read_back`; throughput `acceptance_criteria_must_name_the_evidence_not_the_intent`;
synthesis PR-15. The claim primitive's own correction (task `ent_da60df3beccb675ef8c8c0c5`): two stores
against one canonical key both returned success, and only a read-back showed the second overwrote the first.

**Fires:** partial. `scripts/linters/check_neotoma_rest_paths.py` (#606) catches MCP tool names used as
REST routes. The undeclared-field case (`/store` routes unknown fields to `raw_fragments` and returns
success) has no linter and no runtime check.

### 3. Validate the instrument before believing the measurement

A zero, an empty result, or a silent pass is a claim about the tooling before it is a claim about the world.
Prove the instrument non-zero on a known-positive case; segment the population before attributing a rate;
state the timezone of a search window. Sources: #727 rule 3; synthesis PR-16.

**Fires:** nothing.

### 4. A test that cannot fail on the thing it watches is decoration

Before offering a test as proof of a fix, revert the fix and confirm the test goes red; say in the PR what
red looked like. A test written against current behaviour ratifies the bug. Sources:
#727 rule 4; throughput `acceptance_criteria_must_name_the_evidence_not_the_intent` (b).

**Fires:** nothing mechanical; no linter distinguishes an assertion that pins a bug from one that pins a
fix. Review does this; #744 recorded its revert results in the PR body, which is the practice.

### 5. Fail closed on the field that carries the safety meaning

When a value is absent, unrecognized, or malformed, the default is the restrictive branch, for the field that
encodes the risk. Add a safety value to both sides of a classification and test that the default branch is
restrictive. Sources: #727 rule 5; architecture plan `operator_only_is_never_auto_executable_not_merely_high_blast`,
`unclassified_action_type_fails_closed_and_loudly`; synthesis PR-20. Prior art: Cedar (zero permits is deny;
forbid wins).

**Fires:** partial. `gating.py` `blast_radius_for()` (#724, merged): `operator_only` resolves to `NEVER`
ahead of every policy set; a declared-but-unclassified action type resolves to `NEVER` with a warning; a test
asserts the never-set is identical in `gating.py` and `execution/mcp/ateles/server.py`. Not firing:
`grant_checker.py` permissive on no grants and on unreachable Neotoma (#560); `a2a_gateway.authorize_caller()`
allows when the checker raises; the grant proxy passes through with no identity. See `authority_model.md`.

### 6. Extend the mechanism that already generalizes; do not build a parallel one

Search the code, not memory of it, before building; reuse the existing entity or relationship type; keep a
name that is already accurate. Sources: #727 rule 6; throughput `gate_machinery_is_already_pr_independent`
(the consent gate for outbound content already existed); gate-state plan `ent_4222e5d52edd9bdba7b78cc1`
`keep_the_name_workflow_definition`; agent_policy `ent_4d34c6f96312be686f572add`.

**Fires:** nothing today. `SWARM_PRIOR_ART_CONTRACT` (#686) injects the rule into every dispatched prompt
and is open; merging it converts this invariant from prose into a mechanism.

### 7. Unknown stays distinct from a verdict

"We could not tell" and "we can tell and it is bad" are different claims. A reader of gate, grant, or drift
state carries a third value and never coerces an error to pending or to clear. Sources: gate-state
`unknown_must_stay_distinct_from_a_verdict`; synthesis PR-12 (a failed read resolved three different ways
across the swarm). Prior art: XACML's `Indeterminate` decision.

**Fires:** partial. `checkout_drift.py` reports `unknown` on a failed fetch; `_required_ci_state` in
`swarm_dispatch.py` returns `"unknown"`; `foundation.py` distinguishes listed-but-absent from empty. Not
firing: the three grant paths under invariant 5, each mapping an unreachable policy source to allow.

### 8. Every figure carries its date and its instrument; re-measure before acting

Stored diagnoses and counts decay. A principle is checked against `origin/main`, not session notes; an open
issue is not evidence a defect is live until main and the deployed checkout are read. Sources: synthesis
PR-17, C15, C16 (stranded `participation_record` counts of 129, 143, and 163 across three sources); throughput
`three_principle_candidates_rejected_on_verification`.

**Fires:** nothing. This document's header is the practice.

### 9. One source, defined once; a comment claiming parity is not parity

A value the swarm reads has one home. When a copy is needed for import hygiene, derive it at import time or
assert equality in a test. Sources: gate-state `four_divergent_copies_of_the_gate_set`; synthesis PR-07;
throughput `positive_design_rules_strengthen_existing_ones_not_new_ones`; the no-transition-log and
no-dispatch-log decisions are the same rule applied to history.

**Fires:** partial. `scripts/linters/check_hardcoded_config.py` runs in `scripts/lint.sh`; the #724 parity
test covers the never-set only. The four gate-set copies are still four on main (`gates_and_workflows.md`).

### 10. Dispatch is not completion

Whoever hands work to the swarm owns it through merge and release; "shipped" is an open PR, "landed" is on
main and in the deployed checkout. Sources: agent_policy `ent_5456a8a2224d8211ef33749c`; throughput
`shipped_is_not_landed_for_this_plan`; synthesis PR-18, PR-19.

**Fires:** partial. `checkout_drift.py` reports drift at daemon start, advisory unless
`ATELES_ENFORCE_CHECKOUT_FRESHNESS=1`. Nothing tracks a PR from open to deployed.

## Contradictions this document touches

**C7, where rules live.** Four homes were chosen on 2026-09-02: `agent_policy` entities (agent behaviour,
synced to skills), `CLAUDE.md` (re-injected after compaction, so it binds interactive sessions), this
directory (diffable, PR-reviewed, read by the gates), and a warning against a second document on one
subject. Resolved: these are different audiences, not copies. The #727 section states in its first line
that it does not bind dispatched agents; this document is what the gates read; the derivation is shared and
cited. Direction of truth per artifact class is in `conformance.md`. When the two diverge, this document is
wrong until a PR corrects it, and that PR is the review.

## Beyond the sources

Invariants 8, 9, and 10 are not in #727; they are consolidated from the decisions cited under each. The
grouping and the one-sentence statements are this document's.
