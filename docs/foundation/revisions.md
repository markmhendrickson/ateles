# Revisions: the amendment history of the foundation documents

**Kind:** foundation companion; provenance, never argument. Not keyed, not in the kernel, and never
inlined into a review prompt: nothing here states a rule, so nothing here is read to judge a change.
**Derived from:** decision 74 — the revision chains that had accumulated in each document's front matter,
moved here so what a reader hits first is the document's own claim.

## What this file is for

Each foundation document records who amended it, when, and what the amendment changed. That record is an
obligation (`conformance.md#amending-a-foundation-document`) and it is worth keeping. It is not worth
reading before the document's argument, which is what the front-matter form made unavoidable: on
`conformance.md` the chain had reached 28 clauses and 10,099 characters — more than a fifth of the reader's
budget for the whole kernel — before a single rule was stated.

The obligation is unchanged. The location is not: a document's revision entries live in the table for that
document below, one row per revision, and the document's front matter carries a pointer here instead of the
chain.

## How to add a revision

A PR that amends a foundation document adds one row to that document's table below, in the same change —
the same "in the same change" obligation `conformance.md#amending-a-foundation-document` already states for
a decision, a term, and a registered type. A row is three fields: the revision number, the pass that made
it, and what it changed. If the change opened or ruled a decision, the register row in `conformance.md` is
still the index; this table says only that the document moved and why.

Rows are append-only and chronological. A revision number is never reused. A pass that amends several
documents adds a row to each document's table.

## `authority_model.md`

| Revision | Pass | What changed |
|---|---|---|
| 29 | the simplification pass of 2026-09-05 | `claimant` retired for lease holder |
| 31 | the memo-gap pass of 2026-09-06 | decision 41 ruled here — write admission per entity type is default-deny, and the grant is the allowlist |
| 34 | the workflow-format pass of 2026-09-06 | a required approver may be named by ownership of an entity the checkpoint's subject concerns |
| 35 | the consistency pass of 2026-09-06 | the brief's Q1–Q8 and the raiser question registered as decisions 46 to 54; C13 marked settled by C9 and decision 37 |
| 36 | the second workflow-format pass of 2026-09-06 | a resolution on an `operator_only` action is the operator's decision and never the confirmation; the shared-instance approver cites decision 55 |
| 37 | the testability pass of 2026-09-06 | a parameter constraint on a write capability as a field allowlist — the mechanical half of minimization at capture; `AWAITS` resolves a role to principals |
| 38 | the rulings pass of 2026-09-06 | decisions 46, 48, 49, 51, and 54 ruled here, and 50 and 53 in one half each — what owning confers; the counting rule; structural checks as reads over the checkpoint's principal edges, with the thresholds' home on the `action_policy`; initiative approval as the checkpoint; budget as an attenuating scope term; credit as a read model |
| 39 | the second rulings pass of 2026-09-06 | decisions 47 and 52 ruled here, and the second halves of 50 and 53 — the raiser does not resolve, the operator's self-resolution marked; what stops is a task, confirmed through the checkpoint by the owner seat, proposing a grant capability; which checks and which metered resources are `action_policy` values, fail-closed where unwritten |

## Documents not yet migrated

Every other document in `docs/foundation/` still carries its revision chain in its own front matter.
Decision 74 settles the pattern; the rollout is per-document, one document per PR, so the move never
collides with a substantive change already open against the same file. The order is by front-matter weight,
largest first: `conformance.md` (10,099 characters), `vocabulary.md` (8,297), `gates_and_workflows.md`
(7,169), `status.md` (6,039), `adapters.md` (5,997), then the rest.
