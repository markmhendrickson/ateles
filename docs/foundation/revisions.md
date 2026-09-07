# Revisions: the amendment history of the foundation documents

**Kind:** foundation companion; provenance, never argument. Not keyed, not in the kernel, and never
inlined into a review prompt: the tables below state no rule about how the swarm works, so no reviewer
reads them to judge a change. *How to add a revision* is the exception that proves the shape — it is
housekeeping for this file, and the design obligation it serves is stated and enforced at
`conformance.md#amending-a-foundation-document`, not here.
**Derived from:** decision 74 — the revision chains that had accumulated in each document's front matter,
moved here so what a reader hits first is the document's own claim.

## What this file is for

Each foundation document records who amended it, when, and what the amendment changed. That record is an
obligation (`conformance.md#amending-a-foundation-document`) and it is worth keeping. It is not worth
reading before the document's argument, which is what the front-matter form made unavoidable: on
`conformance.md` the chain had reached 28 clauses and 9,462 characters — more than a fifth of the reader's
budget for the whole kernel — before a single rule was stated.

The obligation is unchanged. The location is not: a document's revision entries live in the table for that
document below, one row per revision, and the document's front matter carries a pointer here instead of the
chain.

## How to add a revision

A PR that amends a foundation document adds one row to that document's table below, in the same change —
the same "in the same change" obligation `conformance.md#amending-a-foundation-document` already states for
a decision, a term, and a registered type. A row is three fields: the revision number, the pass that made
it, and what it changed. A row whose field the source clause never carried reads `—` rather than an
invention. If the change opened or ruled a decision, the register row in `conformance.md` is still the
index; this table says only that the document moved and why.

Rows are append-only and chronological. A revision number is never reused. A pass that amends several
documents adds a row to each document's table, under one number.

**The next free revision number is established by a sweep, never by reading this file.** The sweep is over
the remotes — the maximum `revision N` on any open branch's copy of any document here — for the same reason
`conformance.md`'s decision numbering states it: concurrent branches assign numbers the file on your own
branch cannot see. As of 2026-09-07 that sweep over all 92 open pull-request branches gives 68, so the next
free number is 69, which this pass takes.

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
| 69 | the front-matter rollout of 2026-09-07 | decision 74 applied to this document — the revision chain moved verbatim into `revisions.md`, and each section stating three or more full-sentence rules opened with the list of them; no argument changed |


## `adapters.md`

| Revision | Pass | What changed |
|---|---|---|
| 29 | the simplification pass of 2026-09-05 | open decision 35 |
| 31 | the memo-gap pass of 2026-09-06 | the source is kept, not only named |
| 34 | the workflow-format pass of 2026-09-06 | a system whose delivery surface is a local filesystem is admitted through the same contract; open decision 45 — whether the host a daemon runs on is an external system |
| 36 | the second workflow-format pass of 2026-09-06 | a merchant is a system of its own and a purchase its class, under *Admitting a new adapter*; open decision 55, whether a second instance of the record is an external system |
| 37 | the testability pass of 2026-09-06 | the window declared on the binding and the per-window observation on the adapter's `agent_session`; a credential-less outbound operation is a denial, never a drop; the linkage section states what a sign-off pins per kind |
| 38 | the rulings pass of 2026-09-06 | decision 35 ruled as settled by the conformance suite — one binding type per external system, routing a field of it, the name and the substitution deferred to a vocabulary pass; decision 45 ruled — the host a daemon runs on is an external system |
| 49 | the event/signal/delivery pass of 2026-09-06 | `vocabulary.md#event` cited where this document already used the word; one stray `gmail.md` anchor updated to the renamed section |
| 56, rebased onto the checker-mechanism and self-awareness passes | the peering pass of 2026-09-06 | decision 55 ruled — a peer instance is the record, extended by replication, not an external system; the interim `operator_only` rule retired for eligibility, replaced by `sync_peers`; a pointer added to the governance-write question decision 55 does not settle |
| 61 | the rendered-interface pass of 2026-09-06 | a system reached only through a rendered interface — no event API, no stable record identifier — admitted under *Admitting a new adapter*; identity and linkage answered by obligation 3, extended from the dedup key to `external_id`; coverage answered by revision 34's filesystem finding, transferred without change; read-back argued as real but partial, naming what it cannot establish; freshness needing no new mechanism; a read-time planted-positive instrument named for the case a delivery-based drop counter cannot catch, a layout change that returns zero rows and reports nothing; the outbound default left to the existing fail-closed rule rather than special-cased; no decision opened |
| 65 | the host-configuration pass of 2026-09-06 | a seventh obligation for an external system's own configuration considered and rejected — the contract judges the mapping, and configuration extends obligations 1 and 6 instead, read at the admission task's arch review step; the case carried through in full is `github.md`'s required host state |
| 66, **derived from** the operator's 2026-09-06 14:44 memo on agent identities across external systems | the agent-identity pass of 2026-09-06 | the general rule that an agent's identity lives in the record and an external system holds at most a credential that binds to it; the asymmetry between a system that issues a per-agent credential and one that does not; the outbound mark required where attribution cannot be external; decision 69 opened and ruled — a per-agent credential is an obligation where the system issues one; the binding declared on the `vendor_binding` on decision 42's pattern; AAuth established from the corpus as one of the credential kinds `authority_model.md#principals` already enumerates, not a second identity system |
| 69 | the front-matter rollout of 2026-09-07 | decision 74 applied to this document — the revision chain moved verbatim into `revisions.md`, and each section stating three or more full-sentence rules opened with the list of them; no argument changed |


## `calendar.md`

| Revision | Pass | What changed |
|---|---|---|
| 29 | the simplification pass of 2026-09-05 | `calendar_routing_config` replaced by `channel_config`, the binding type `adapters.md` names |
| 37 | the testability pass of 2026-09-06 | refusal 1's mechanical half, the field allowlist on the grant |
| 38 | the event/signal/delivery pass of 2026-09-06 | "Every inbound signal, and what it becomes" and its table headers, Linkage, Identity, and the disposition sentence renamed to `event`, matching `github.md`'s and `gmail.md`'s precedent; `signal` kept only in its ordinary-English sense |
| 69 | the front-matter rollout of 2026-09-07 | decision 74 applied to this document — the revision chain moved verbatim into `revisions.md`, and each section stating three or more full-sentence rules opened with the list of them; no argument changed |


## `conformance.md`

| Revision | Pass | What changed |
|---|---|---|
| 29 | the simplification pass of 2026-09-05 | the `feedback` entity removed from the direction-of-truth table in favour of the finding; `scenarios_extended.md` merged into `scenarios.md`; decisions 32 to 35 opened |
| 30, 2026-09-06 | the operator's 2026-09-05 22:02–22:13 memos on how tasks come into existence | `intake_rule` registered in the direction-of-truth table, and decision 36 opened |
| 31 | the memo-gap pass of 2026-09-06 | decisions 37 to 41 registered as ruled |
| 33 | — | 2026-09-06: the conformance-suite design — `conformance_suite.md`, its keyed row, the rule-coverage check named as a contract, its direction-of-truth row, and decisions 43 and 44 opened |
| 34 | the workflow-format pass of 2026-09-06 | decision 45 registered |
| 35 | the consistency pass of 2026-09-06 | decisions 46 to 54 registered from `authority_model.md`'s long-open questions |
| 36 | the second workflow-format pass of 2026-09-06 | decision 56 registered |
| 37 | the testability pass of 2026-09-06 | decision 56 registered; the decision-citations lint named as a contract and its syntactic rule stated under *Phases and implementation state* |
| 38 | the rulings pass of 2026-09-06 | decisions 31, 32, 35, 42, 44, 45, 46, 48, 49, 51, and 54 moved to ruled; 43, 50, and 53 marked ruled in part, each with its open half stated; the **ruled in part** status value |
| 39 | the second rulings pass of 2026-09-06 | 36, 47, 52, and 56 moved to ruled; 43, 50, and 53 from ruled in part to ruled; the operator's review of 37 to 41 recorded; three rows open — 33, 34, and 55 |
| 40 | the planning pass of 2026-09-06 | `planning_model.md` keyed, with its direction-of-truth row; decision 57 opened, and 58 opened and ruled with 43, 47, and 56 |
| 44 | the Human Inversion mapping pass of 2026-09-06 | decision 40's register row flagged, not reopened, against a public essay series' contradicting claim; no decision opened or ruled |
| 45 | the ancestry pass of 2026-09-06 | decision 61 opened, on where a planning level's ancestry expectation is declared |
| 46 | the session-reconciliation pass of 2026-09-06 | decision 40's register row restated — the essay-contradiction flag narrowed to the real question, what a session-family entity is for and whether a sign-off may reference one — reading (a) held, no decision opened or ruled |
| 47 | the priority pass of 2026-09-06 | decision 61 reframed from a per-level ancestry expectation to a per-class default ancestor's storage location; decision 62 opened, on whether an instance may bind a claiming principal to "must" take the highest-standing claimable task |
| 48 | the rulings pass of 2026-09-06 | decisions 57, 59, 60, 33, and 34 moved to ruled, on the operator's word; 40 and 55 left untouched — 40 under active reconciliation, ruled but flagged rather than open; 55 the operator's own product decision; decision 61's ruling drafted against the pre-reversal framing was superseded by the priority pass's reversal, before landing, and is not applied — 61 stays open as revision 47 reframed it; open rows after this pass are 55, 61, and 62 |
| 49 | the vocabulary-standard pass of 2026-09-06 | the amendment obligation for `vocabulary.md` added under *Amending a foundation document*, parallel to the decision-registration rule, cited from `principles.md`'s new invariant 12 |
| 50 | the minimization-recalibration pass of 2026-09-06 | decision 63 opened and ruled — the harness transcript store registered; decision 64 opened, on its owner and shape; open rows after this pass are 55, 61, 62, and 64 |
| — | the rulings-61-62-64 pass of 2026-09-06 | decisions 61 and 62 moved to ruled; 64 moved to ruled in part, its field-by-field-shape remainder kept open; 40 and 55 left untouched, both turning on the operator's own judgement; open rows after this pass: 40, 55, and 64 in part |
| 55 | the self-awareness pass of 2026-09-06 | the kernel read as self-description, distinct from review, stated as a declared read a step may make; no decision opened — the reading is a use of the existing declared-read mechanism, not a new one |
| 56, rebased onto the rulings-61-62-64, checker-mechanism, and self-awareness passes | the peering pass of 2026-09-06 | decision 55 moved to ruled — a peer instance is the record, extended by replication, not an external system; decision 65 opened, on what surfaces an inert governance-type sync write; open rows after this pass are 64 in part and 65 |
| — | the close-out pass of 2026-09-06, rebased onto the peering pass | decision 64 moved from ruled in part to fully ruled: the field-by-field shape is schema authoring for the runner, not a register question, and its home is stated as `migration.md#the-work-model`, which already carried the `conversation`, `conversation_message`, and `session_digest` rows this closes into; no new decision opened; open rows after this pass: 40 and 65 |
| — | the register-reconciliation pass of 2026-09-06, rebased onto the close-out pass | no decision opened or ruled; decision 40's status label corrected from **ruled** to **ruled in part** to match its own row text, and its dangling "carried as F27 below" pointer — no such finding exists in `gates_and_workflows.md` — repointed to the actual paragraph; open rows after this pass: 40 in part and 65 |
| 59, 2026-09-06, rebased onto the register-reconciliation pass | the decision-65 ruling | decision 65 moved from open to ruled — the engine's existing write-back read extended by one field, the `no_credential` reason class named, no new carrier invented; open rows after this pass: 40 in part |
| 61 | the rendered-interface pass of 2026-09-06 | `adapters.md#a-system-reached-only-through-a-rendered-interface-is-admitted-the-same-way-and-three-of-its-five-rules-were-already-answered-by-the-filesystem-case` — a rendered-interface adapter checked against the admission contract and found already covered by obligation 3, revision 34's filesystem coverage finding, and the existing fail-closed outbound default; no decision opened; open rows after this pass: 40 in part |
| 62, on the operator's 12:57 memo — *"it seems to me best that we have as detailed as possible documentation and only a condensed version for use cases that require it"* | the projection pass of 2026-09-06 | decision 66 opened and ruled — what a review reads is a generated **reading projection** of these documents, extracted from `conformance_suite.md`'s matrix and held equal by `render_reading_projection.py --check`; the two caps are re-aimed from the canonical documents onto the projection; `projection` reused under invariant 12 and "condensation" retired as a name for the work |
| 63, 2026-09-06, **derived from** the operator's 2026-09-06 12:56 memo asking whether Ateles or Neotoma should prevent schema drift | the schema-drift pass | decisions 67 and 68 opened and ruled — four distinct failure modes named, two assigned to the substrate, one to the design, one with no design side; the type-registration amendment obligation added under *Amending a foundation document*, parallel to the decision and term rules; three testable substrate requirements stated under *What the design requires of the substrate, regardless of who builds it*; the ~70,000-row and `raw_fragments` carryover pointed at `migration.md` rather than restated; open rows after this pass: 40 in part |
| 66, **derived from** the operator's 2026-09-06 14:44 memo | the agent-identity pass of 2026-09-06 | decision 69 opened and ruled — a per-agent credential is an obligation where the external system issues one, and the outbound artifact carries a mark where it does not; argued in `adapters.md` |
| 67 | the project-term pass of 2026-09-06 | the possessive "the project's `X`" resolved to the owner each rule actually names — the `action_policy` to the instance, `release_criteria` to the context entity retrieved by type, a `record_migration` declaration to its scoped id — and decision 70 opened, on whether the `project` scoping key on `workflow` and `batch` is decision 57's planning level or a separate term sharing the word; no term added and none retired, `instance` and `project` both already carrying their senses |
| 67 | the underdetermined-inputs pass of 2026-09-06 | decision 71 registered as ruled; the next-number line corrected to 72 and made an obligation of adding a row |
| 67 | the naming pass of 2026-09-06 | : decisions 72 and 73 opened and deliberately left **open** — 72, whether `sign-off` names its record neutrally when `signed` is one of its own three verdict values, argued in `vocabulary.md`; 73, whether a term should prefer a single word and what exception admits the qualified compounds invariant 12's no-overlap half depends on, argued under invariant 12 in `principles.md`. Both would execute as a corpus-wide rename of files PRs #766, #767, and #770 are amending, so both are to be ruled and executed as one pass after those merge; 70 and 71 are assigned on those branches and left unused here. The stale "next number" line, wrong on each of the four occasions it has been checked, is corrected to 74 and given a standing caveat to read the table instead. Open rows after this pass: 72 and 73. |
| 68 | the decision-70 ruling of 2026-09-06 | decision 70 moved from open to ruled — the `project` scoping key on `workflow` and `batch` and the planning level of decision 57 are two concepts sharing one word; the planning level keeps `project`, the key is renamed `declaration_scope`, and **declaration scope** is added to `vocabulary.md` under invariant 12's overlap half, swap-tested against `instance`, `domain`, and `tenant`; a `project` entry added beside it stating the planning sense and its Never for the scoping sense; `migration.md`'s G20 restated — the word collision closed, the remaining question narrowed to whether an instance runs one declaration scope or several; the derivation of a task's scope left as a gap in `conformance_suite.md` with the ascent removed as a candidate answer |
| 69 | the front-matter rollout of 2026-09-07 | decision 74 applied to this document — the revision chain moved verbatim into `revisions.md`, and each section stating three or more full-sentence rules opened with the list of them; no argument changed |
| 72 | the decision-73 ruling of 2026-09-07 | register row 73 moved from **open** to **ruled**, with the rejected single-word preference and the three grounds recorded on the row; the naming-pass narrative's closing count of open rows corrected from 72 and 73 to 72 alone, and its sequencing note narrowed, the ruling having turned out to need no rename |


## `conformance_suite.md`

| Revision | Pass | What changed |
|---|---|---|
| 38 | the rulings pass of 2026-09-06 | decision 44 ruled — a `signed` or blocking sign-off requires a held lease by its signer; decision 43 ruled in its enumeration half — the bootstrap set is the thirteen-record table, every member read back; the rows that waited on decisions 31, 32, 35, 42, 43, 44, 48 to 51, 53, and 54 updated |
| 39 | the second rulings pass of 2026-09-06 | decision 43 ruled in its second half — the operator's later governance write gated, self-resolved and marked, with 47; the rows that waited on 36, 43, 47, 50, 52, 53, and 56 turned mechanical; U-14 closed by 56 |
| 50 | the minimization-recalibration pass of 2026-09-06 | WF-16 updated for `disclosure_lint`; U-34 opened and closed in the same pass |
| 57 | the sign-off-provenance pass of 2026-09-06 | DM-28 added, the new `session_digest` edge decision 40 rules, required on an agent's sign-off |
| 58, 2026-09-06, **derived from** the operator's 2026-09-06 12:56 memo, via `conformance.md`'s decision 67 | the schema-drift pass | DM-10b added — the substrate refuses a write naming an undeclared field, expected-failing until Neotoma satisfies it, distinct from DM-10's client-side read-back check |
| 66, **derived from** the operator's 2026-09-06 14:44 memo | the agent-identity pass of 2026-09-06 | AD-39, AD-40, and AD-41 added — decision 69's per-agent-credential obligation and the shared-credential verdict's disposition, the outbound mark and its refusal, and the credential's home on the `vendor_binding` |
| 69 | the front-matter rollout of 2026-09-07 | decision 74 applied to this document — the revision chain moved verbatim into `revisions.md`, and each section stating three or more full-sentence rules opened with the list of them; no argument changed |


## `data_model.md`

| Revision | Pass | What changed |
|---|---|---|
| 29 | the simplification pass of 2026-09-05 | `claimant` and `workflow policy` retired; the checkpoint's reason classes cited from their one home |
| 31 | the memo-gap pass of 2026-09-06 | `REFERS_TO` from a task to a record entity it concerns and from a sign-off to what it read; the source kept in the record; what an `agent_session` is not |
| 34 | the workflow-format pass of 2026-09-06 | the `workflow` row carries every field the declaration declares — `applies_when`, the read dependencies, `freshness`, and the two intervals |
| 36 | the second workflow-format pass of 2026-09-06 | a step's two intervals may each be the task's `due_date`; the special-category mark on a registered type, and the three mechanisms that read it |
| 37, the conformance suite's findings carried back into their homes | the testability pass of 2026-09-06 | the `finding` row; `sign_off` carrying its findings by edge, `tasks_attached[]`, and the per-kind pinned state, `SIGNED_BY` → principal; `DUPLICATE_OF`; `acceptance_criteria[]` on the task; `recoveries` and `lapse_cap` on `action_policy`; `rounds_cap` and `none_permitted` on the declaration; the daemon's window observation; `blocked` retired as a status |
| 38 | the rulings pass of 2026-09-06 | the `verdict` as the sign-off's reconciled projection; a sign-off's lease check as a derived read; the tool dimension on a grant's capabilities and the budget shape on `param_constraints` and `delegation_edge.scope`; `quorum` and `disjoint_roles[]` per class on `action_policy`; a host's `process` and `checkout` artifact kinds; what an `ownership_grant` confers; no `credit`, `initiative`, or `proposal` type |
| 39 | the second rulings pass of 2026-09-06 | `self_resolved` on the checkpoint, decision 47; `metered_resources[]` per class on `action_policy`, decision 53; the governance types on the engine's grant alone, decision 56; the initiative-class constraint on a `task` write capability as the right to propose, decision 52; a work-model type in `subject_types[]` refused at the write, decision 36 |
| 40 | the planning pass of 2026-09-06 | the planning record and the `decision` rows; `PART_OF` from a task to its planning record and between levels; `SUPERSEDES`; the ascent as a derived read on the task |
| 44 | the Human Inversion mapping pass of 2026-09-06 | a sign-off's attribution stops at the agent and its version — the model or harness observed at the write is absent, and named as such beside `vendor_binding`, decision 42, and open decision 59, rather than added as a field |
| 50 | the minimization-recalibration pass of 2026-09-06 | the broadened capture purpose stated in record conventions, and the special-category bullet marked as not reached by it |
| 51 | the transport-and-delivery pass of 2026-09-06 | a paragraph added under Concepts stating why `observation` has no row of its own — not the `finding`/G15 shape, since nothing references it by edge — and why `delivery` is deliberately never stored, both already implied by existing rules and now stated in the one place a reader of this table would look |
| — | the rulings-61-62-64 pass of 2026-09-06 | decision 64 ruled in part — the runner as writer, any declaring step as reader, and the special-category mark's reach to the type per revision 36's F23; the field-by-field shape kept open as the row's remainder |
| 57 | the sign-off-provenance pass of 2026-09-06 | `sign_off` and `REFERS_TO` gain a `session_digest` target, decision 40; the decision-64 note updated for the close-out pass's full ruling — `session_digest`'s field list is schema authoring, not an open decision |
| 58 | the decision-70 ruling of 2026-09-06 | the `project` field on the `workflow` and `batch` rows renamed `declaration_scope`, the scoping key distinguished from decision 57's planning level of the same name — `vocabulary.md#declaration-scope` |
| 69 | the front-matter rollout of 2026-09-07 | decision 74 applied to this document — the revision chain moved verbatim into `revisions.md`, and each section stating three or more full-sentence rules opened with the list of them; no argument changed |


## `failure_posture.md`

| Revision | Pass | What changed |
|---|---|---|
| 35 | the consistency pass of 2026-09-06 | the merge action's class named `merge_pr` in the recovery table |
| 29 | the simplification pass of 2026-09-05 | `claimant` retired for lease holder |
| 31 | the memo-gap pass of 2026-09-06 | a condition of a batch is raised on one of its tasks, never on the batch |
| 34 | the workflow-format pass of 2026-09-06 | rule 5's ceiling for a holding step, and the unclaimed-step interval, each named as a field on the step |
| 37 | the testability pass of 2026-09-06 | the announcement path's own outage and the capture of last resort; the window observation; `action_policy.recoveries`; recovery paths and their cadence on the binding; `lapse_cap`; which checkpoints hold a task from claim; `AWAITS` names principals |
| 43 | the model-and-harness-routing pass of 2026-09-06 | open decision 60 — a runner's lease-held step losing its model or harness mid-execution; the tier-eligibility half ruled as a consequence of decision 59 |
| 44 | the rulings pass of 2026-09-06 | decision 60 ruled in full — unavailability holds and raises under the existing `lapse_cap`, no second clock; `capability_unavailable` named as a reason class distinct from `capability_denied` |
| 45 | the underdetermined-inputs pass of 2026-09-06 | decision 71 ruled — a required read that resolved to no instance raises `underdetermined_inputs` at hydration, before the step opens, and an input found but too thin to act on stays the step owner's judgement under decision 13 |
| 69 | the front-matter rollout of 2026-09-07 | decision 74 applied to this document — the revision chain moved verbatim into `revisions.md`, and each section stating three or more full-sentence rules opened with the list of them; no argument changed |


## `gates_and_workflows.md`

| Revision | Pass | What changed |
|---|---|---|
| 29 | the simplification pass of 2026-09-05 | `workflow policy` retired and its section renamed; `hot path` retired; the reason classes cited from their one home; open decision 32 |
| 31 | the memo-gap pass of 2026-09-06 | decisions 37, 38, and 40 ruled here — work reviewed on the record, closed work redone through intake, and what a step leaves at close; the governance types stated as one list in one home; the finding's `unknown` classification; the `task_policy` home for an operator-specific standing finding |
| 34 | the workflow-format pass of 2026-09-06 | two declared intervals on every step, `unclaimed_after` and `hold_bound`; a planned wait as a step's own close condition held under decision 13; required coverage as the value of `freshness`; consent over several like actions as one presentation of several checkpoints; the governance class with no policy value named among what resolves to `NEVER` — .consistency pass of 2026-09-06 (revision 35: when the checkpoint a `consent` step carries is written, and what the gate evaluates at the take; the merge action's class named `merge_pr`) |
| 36 | the second workflow-format pass of 2026-09-06 | a step's bound may be declared as the task's `due_date`; an `operator_only` action inside a workflow is taken by the operator and its step closes on the confirmation, never on the resolution; a read dependency on a special-category type carries no marker of its own |
| 37 | the testability pass of 2026-09-06 | the finding as an entity the rules bind on; `rounds_cap`; the recorded amendment; an optional step's condition reads what exists; `none_permitted`; the floor-list sentence retired; open decision 56 — where the enforcement point for a governance write sits |
| 38 | the rulings pass of 2026-09-06 | decision 32 ruled — the `verdict` stays a stored field, the sign-off's own projection of its findings and its author, reconciled at the write |
| 39 | the second rulings pass of 2026-09-06 | decision 56 ruled here — the sole-writer grant, the engine acting for the gate; the checkpoint's raiser and resolver never one principal save the operator's marked self-resolution, decision 47; the finding-as-entity settlement marked reviewed and upheld |
| 40 | the planning pass of 2026-09-06 | the amendment to a planning record as the third named class of internal writes that are actions, `amend_<level>`; a lesson at a planning record's scope filed one level up |
| 43 | the model-and-harness-routing pass of 2026-09-06 | open decision 59 — where a minimum model tier per action class or blast radius would live, should an instance choose to declare one |
| 46 | the session-reconciliation pass of 2026-09-06 | two paragraphs added to the decision-40 section — "no session is persisted" is not what the ruling says, and a sign-off may carry a `REFERS_TO` edge to the session or digest that produced it, proposed and not ruled |
| 47 | the rulings pass of 2026-09-06 | decision 59 ruled in full — `min_tier` on `action_policy`, evaluated at the same take as blast, with the operator's values setting a floor only for irreversible classes: payments, sends, merges, governance writes; every other class unset |
| 48 | the event/signal/delivery pass of 2026-09-06 | the host's review-token sentence renamed `signal` to `event`, matching `adapters.md`'s own Identity rule wording; one `gmail.md` anchor updated to the renamed section |
| 56, rebased onto the checker-mechanism and self-awareness passes | the peering pass of 2026-09-06 | a synced observation on a governance type is recorded and never takes effect — the reducer's tie-break ranking and decisions 41/56's admission together, no new mechanism; open decision 65, what surfaces an inert governance-type sync write, since neither a checkpoint nor a finding fits without stretching |
| 58 | the underdetermined-inputs pass of 2026-09-06 | decision 71 ruled — a required read that resolved to no instance is not a failed read and raises `underdetermined_inputs` at hydration; sufficiency judged as presence here and as adequacy by the step owner under decision 13 |
| 59, 2026-09-06, rebased onto the close-out and register-reconciliation passes | the decision-65 ruling | decision 65 ruled — the engine's existing write-back read (principle 2) extended by one field to check for an unreconciled `sync`-sourced observation on the governance type it just confirmed, carrying the new reason class `no_credential`, written aggregated per window on the engine's `agent_session` in the same shape `adapters.md`'s disposition rule already gives a drop; no new carrier invented |
| 57 | the sign-off-provenance pass of 2026-09-06 | decision 40 narrowed to a sign-off's shape at close; the sentence claiming a session's turns are "not an entity the design has" corrected against decision 63; a sign-off's `REFERS_TO` → `session_digest` ruled, required where the signer is an agent |
| 69 | the front-matter rollout of 2026-09-07 | decision 74 applied to this document — the revision chain moved verbatim into `revisions.md`, and each section stating three or more full-sentence rules opened with the list of them; no argument changed |


## `github.md`

| Revision | Pass | What changed |
|---|---|---|
| 37 | the testability pass of 2026-09-06 | the host's assignment never writes `assigned_to`; `impl` closes on a mergeable pull request, stated in `workflows.md` |
| 65 | the host-configuration pass of 2026-09-06 | what the host must be configured to be, ruled an extension of obligations 1 and 6 rather than a seventh obligation — the subscription reconciliation obligation 1's drop counter structurally cannot perform, and the host's merge-permitting configuration as the standing form of the permit the gate never issued; the required state per repository, and the reporting-permission row that belongs to neither obligation; open decision 69, whether a difference at admission blocks the grant or is a finding on the review step |
| 69 | the front-matter rollout of 2026-09-07 | decision 74 applied to this document — the revision chain moved verbatim into `revisions.md`, and each section stating three or more full-sentence rules opened with the list of them; no argument changed |


## `gmail.md`

| Revision | Pass | What changed |
|---|---|---|
| 37 | the testability pass of 2026-09-06 | refusal 1's mechanical half, the field allowlist on the grant |
| 38 | the event/signal/delivery pass of 2026-09-06 | "Every inbound signal, and what it becomes" and its table headers, Linkage, Identity, and the disposition sentence renamed to `event`, matching `github.md`'s "Event and action" precedent; `signal` kept only where the design means the record's reading of an event |
| 50 | the minimization-recalibration pass of 2026-09-06 | refusal 1 narrowed to a third party's Article 9 data, capture generous otherwise under the broadened purpose |
| 69 | the front-matter rollout of 2026-09-07 | decision 74 applied to this document — the revision chain moved verbatim into `revisions.md`, and each section stating three or more full-sentence rules opened with the list of them; no argument changed |


## `migration.md`

| Revision | Pass | What changed |
|---|---|---|
| 29 | the simplification pass of 2026-09-05 | gap G14 closed; `workflow policy` retired |
| 31 | the memo-gap pass of 2026-09-06 | gaps G1 and G12 closed |
| 32 | 2026-09-06 | the skills leg — five classes of skill, the mapping of each to its target, stage 11, gaps G28–G31, and open decision 42 |
| 37 | the testability pass of 2026-09-06 | gaps G6, G7, G8, G11, G13, and G15 closed |
| 38 | the rulings pass of 2026-09-06 | decisions 31 and 42 ruled — the merge form for a re-type, and a skill's harness mechanics split among the grant, a `vendor_binding`, and the harness's own configuration; gap G19 closed |
| 39 | the second rulings pass of 2026-09-06 | leg one grants the governance types to the engine alone, decision 56 |
| 40 | the planning pass of 2026-09-06 | gaps G9, G10, and G31 closed by `planning_model.md`; the planning types' dispositions; the plan family mapped to `workflows.md#planning` |
| 50 | the minimization-recalibration pass of 2026-09-06 | `conversation`, `conversation_message` introduced and registered, decision 63; `session_digest`'s row marked registered and drift-carrying |
| — | the close-out pass of 2026-09-06 | decision 64 closed in `conformance.md`; the `conversation`, `conversation_message` row repointed from decision 64 "still open" to its ruled writer/reader/mark and its authored remainder; a new subsection, *Session types: the field-by-field shape decision 64 left to the schema*, states what must be authored, who owns it, what constrains it, and the existing rows it carries forward without restating their figures |
| 57 | the sign-off-provenance pass of 2026-09-06 | `session_digest`'s row carries the zero-existing-edges drift decision 40's new `REFERS_TO` ← sign-off edge leaves behind |
| 58, 2026-09-06, **derived from** the operator's 2026-09-06 12:56 memo, via `conformance.md`'s decision 67 | the schema-drift pass | G25 cross-referenced as the design-side symptom of the same missing registry read neotoma#1972's diverging `relationship_type` copies show; no gap number added, no figure restated |
| 60 | the undefined-term pass of 2026-09-06 | a new section, *Substrate field names the design reads and never adopts as terms*, records `user_id`, `sub`, `iss`, `conversation`, `raw_fragments`, and `reducer_config` — six names cited across five or more foundation documents whose concepts the design already names, kept out of `vocabulary.md` so the design outlives the substrate's field names; no gap number added, since none of the six is a place the foundation fails to say what the migration needs |
| 69 | the front-matter rollout of 2026-09-07 | decision 74 applied to this document — the revision chain moved verbatim into `revisions.md`, and each section stating three or more full-sentence rules opened with the list of them; no argument changed |


## `payments.md`

| Revision | Pass | What changed |
|---|---|---|
| 36 | the second workflow-format pass of 2026-09-06 | a purchase or a booking with a merchant is out of scope here, being an effect on the merchant's system and not on a rail |
| 39 | the second rulings pass of 2026-09-06 | the raiser-resolves and quorum pointers cite decisions 47 and 50 as ruled |
| 40 | the event/signal/delivery pass of 2026-09-06 | the Purpose sentence, "Inbound: every signal a rail can produce," its two subsection headings, its table headers, and the Linkage rule renamed to `event`; `signal` kept only in its ordinary-English sense |
| 69 | the front-matter rollout of 2026-09-07 | decision 74 applied to this document — the revision chain moved verbatim into `revisions.md`, and each section stating three or more full-sentence rules opened with the list of them; no argument changed |


## `planning_model.md`

| Revision | Pass | What changed |
|---|---|---|
| 45 | the ancestry pass of 2026-09-06 | the operator's question on missing ancestry — whether the swarm derives an absent parent, or finds the gap — settled against `#authority-per-level-an-amendment-is-an-action-and-its-class-is-the-levels` and decisions 17, 41, 51, and 52; `judge`'s and `route`'s defect lists extended by one item each, with no new step or type; decision 61 opened |
| 47 | the priority pass of 2026-09-06 | the permissive orphan default reversed on the operator's argument that explicit ancestry serves both execution and prioritization — every task now expects an ancestor by default, satisfied at near-zero noise by a default ancestor per intake class rather than a finding on every orphan; the no-authoring refusal restated as the line the default and the refusal meet at; decision 61 reframed from a per-level expectation to a per-class default, still open and still blocked on decision 57 |
| 48 | the rulings pass of 2026-09-06 | decision 57 ruled — five levels, task → plan → project → strategy → mission, on the operator's word; objective settled as content within a strategy record and not a sixth level. Decision 61 not ruled: the priority pass (revision 47) reframed the question after this ruling was drafted against the pre-reversal framing, from "where a per-level ancestry expectation is declared" to "where an intake class's default ancestor is declared," and the reframed question is left as revision 47 stated it, still open and still blocked on 57, since ruling on the superseded framing would rule a question that no longer exists in that shape |
| — | the rulings-61-62-64 pass of 2026-09-06 | decision 61 ruled — the default ancestor named on the `intake_rule`, unblocked now that decision 57 fixes the levels |
| 67 | the project-term pass of 2026-09-06 | decision 70 opened — `project` names decision 57's planning level and is also the scoping key on `workflow` and `batch`, and nothing yet says whether the two are one thing; the possessives that read the key as ownership are resolved to the owners the rules name, in this document and across the corpus |
| 68 | the decision-70 ruling of 2026-09-06 | decision 70 ruled — two concepts share one word; the planning level keeps `project`, the scoping key on `workflow` and `batch` is renamed `declaration_scope`, and the section that posed the question now argues the ruling, with G20's global roster as the evidence a planning record cannot be the key and invariant 12 as what obliges the rename rather than a note; three existing terms swap-tested and rejected before the compound was coined |
| 69 | the front-matter rollout of 2026-09-07 | decision 74 applied to this document — the revision chain moved verbatim into `revisions.md`, and each section stating three or more full-sentence rules opened with the list of them; no argument changed |


## `principles.md`

| Revision | Pass | What changed |
|---|---|---|
| 37 | the testability pass of 2026-09-06 | invariants 3, 6, 8, and 10 given their mechanical form — the named instruments, the singletons' closure, the citation lint, and "landed" as a derived read |
| 44 | the Human Inversion mapping pass of 2026-09-06 | where the human sits, operator attention as the protected constraint, and the sovereignty reason for the owned record, stated beside the invariants; the cross-disciplinary rubric named as out of scope until P4 |
| 49 | the vocabulary-standard pass of 2026-09-06 | invariant 12 added, stating the operator's own standard for the vocabulary's size and overlap as a design invariant, generalizing the substitution test `status.md` revision 29 already applied to the term retired for `review step` in revision 19 |
| 57 | the naming pass of 2026-09-06 | decision 73 opened under invariant 12 — whether a term should prefer a single word, and what exception admits the qualified compounds the no-overlap half currently depends on, `step owner` and the three `scope` compounds among them; the parenthesized-object verb form and a structural defect in `vocabulary.md`'s heading levels recorded with it. Opened, not ruled). Which mechanisms exist on a given checkout, and where nothing fires, is measured in `status.md`, not here. |
| 69 | the front-matter rollout of 2026-09-07 | decision 74 applied to this document — the revision chain moved verbatim into `revisions.md`, and each section stating three or more full-sentence rules opened with the list of them; no argument changed |
| 72 | the decision-73 ruling of 2026-09-07 | decision 73 ruled — no single-word preference: a term takes as many words as the design's distinction requires, and no more, and the qualifier must be load-bearing, tested by dropping it and seeing whether what remains is already bound or no longer names the entry. Stated as the existing swap test applied to the qualifier rather than to the whole term, so the ruling names no exception and renames nothing. Invariant 12's closing block, which had carried the question open with its candidate collapses, replaced by the ruled test and the three grounds the preference was rejected on — hidden kinship across the `task` compounds, collision with bound `point` and `gate` and `separation of duties`, and the four `scope` compounds the no-overlap half itself depends on. The `vocabulary.md` heading-level defect kept as a recorded defect, with its counts remeasured: 136 `###` headings, 41 multi-word, one non-term among them, so 135 terms and 40 multi-word |


## `scenarios.md`

| Revision | Pass | What changed |
|---|---|---|
| 29 | the simplification pass of 2026-09-05 | walkthroughs (e)–(j) merged back from `scenarios_extended.md`, whose only reason to exist — a reading-block budget — no longer applied to a document that is not on the reading list |
| 35 | the consistency pass of 2026-09-06 | scenario (j)'s garbled clause, left when `lens` was retired, repaired |
| 69 | the front-matter rollout of 2026-09-07 | decision 74 applied to this document — the revision chain moved verbatim into `revisions.md`, and each section stating three or more full-sentence rules opened with the list of them; no argument changed |


## `telegram.md`

| Revision | Pass | What changed |
|---|---|---|
| 37 | the testability pass of 2026-09-06 | every uncorrelated message from a bound principal is a task; the start-time binding is a cache with a declared staleness bound |
| 39 | the second rulings pass of 2026-09-06 | the raiser-resolves pointer cites decision 47 as ruled |
| 69 | the front-matter rollout of 2026-09-07 | decision 74 applied to this document — the revision chain moved verbatim into `revisions.md`, and each section stating three or more full-sentence rules opened with the list of them; no argument changed |


## `vocabulary.md`

| Revision | Pass | What changed |
|---|---|---|
| 29 | the simplification pass of 2026-09-05 | `claimant`, `workflow policy`, and `hot path` retired; the [checkpoint](vocabulary.md#checkpoint) reason classes cited from their one home; a code-era field removed from the Owner table |
| 31 | the memo-gap pass of 2026-09-06 | the finding's `unknown` scope; what an `agent_session` is not for |
| 34 | the workflow-format pass of 2026-09-06 | the two intervals on the step entry; where the set of `action_type` values lives |
| 35 | the consistency pass of 2026-09-06 | the [operator-facing agent](vocabulary.md#operator-facing-agent) defined by [role](vocabulary.md#role); `merge` as an action-class name retired for `merge_pr` |
| 36 | the second workflow-format pass of 2026-09-06 | a bound as the task's `due_date` on the step entry; the `operator_only` step; a rule keying on a field a step wrote; no marker on a read for a special-category type; decision 55 on the [external-system](vocabulary.md#external-system) entry |
| 37 | the testability pass of 2026-09-06 | `blocked` retired as a task status; the terminal set declared on the type; the `finding` and `sign_off` fields; `rounds_cap` and `none_permitted` |
| 38 | the rulings pass of 2026-09-06 | the [verdict](vocabulary.md#verdict) as the [sign-off](vocabulary.md#sign-off)'s reconciled [projection](vocabulary.md#projection); a `signed` or blocking sign-off written under a held [lease](vocabulary.md#lease); what owning confers; the counting rule and the thresholds' home on the [quorum](vocabulary.md#quorum) and separation entries; an initiative as a task by class; the host as an external system; a budget as a grant's and a [delegation](vocabulary.md#delegation)'s scope term |
| 39 | the second rulings pass of 2026-09-06 | the raiser never the resolver save the operator's marked self-resolution, on the [approval](vocabulary.md#approval) entry; the right to propose as a grant capability and what stops as a task, on the proposal and [reprioritization](vocabulary.md#reprioritization) entries; metered resources and the engine's sole governance grant, on the [grant](vocabulary.md#grant) entry; a rule naming a work-model type refused at the write |
| 40 | the planning pass of 2026-09-06 | the planning-model section — [planning record](vocabulary.md#planning-record), [planning level](vocabulary.md#planning-level), [ascent](vocabulary.md#ascent), [unplanned](vocabulary.md#unplanned), [planning decision](vocabulary.md#planning-decision), [planner](vocabulary.md#planner), and the [amend](vocabulary.md#amend-a-planning-record) verb |
| 41 | the record-sense pass of 2026-09-06 | the [artifact](vocabulary.md#artifact) definition and five other sentences that read "record" against an external system rephrased to "an entry an external system holds," closing the [record](vocabulary.md#record) Not-for collision; the Not-for scoped to a Never for `docs/foundation/adapters.md` and the five per-system [adapter](vocabulary.md#adapter) documents |
| 44 | the Human Inversion mapping pass of 2026-09-06 | two term collisions with a public essay series disambiguated — [reconciler](vocabulary.md#reconciler) as this design's projection-parity check, never the essay's adjudicating role, and [replay](vocabulary.md#replay) as the essay's [as-of-read](vocabulary.md#as-of-read) sense, never this design's refused re-execution sense |
| 47 | the priority pass of 2026-09-06 | the [`priority`](vocabulary.md#priority) entry added, giving the term a home beside [`claimable`](vocabulary.md#claimable) as a [derived read](vocabulary.md#derived-read) a [principal](vocabulary.md#principal) consults and is never bound to obey |
| 48 | the rulings pass of 2026-09-06 | decision 34 ruled — the [pipeline](vocabulary.md#retired-names) entry replaced by [engine](vocabulary.md#engine), defined once; decision 33 ruled — the [stage](vocabulary.md#stage) entry rewritten to authored prose only, with no `steps[].phase` field |
| 49 | the event/signal/delivery pass of 2026-09-06 | the [event](vocabulary.md#event) entry added — the delivery's payload, distinct from the [delivery](vocabulary.md#delivery) that carried it and the [signal](vocabulary.md#signal) the adapter reads it into — and the [delivery](vocabulary.md#delivery) and [signal](vocabulary.md#signal) entries cross-referenced against it; the operator's question of whether events, signals, and deliveries are properly distinguished |
| 50 | the transport-and-delivery pass of 2026-09-06 | [mapping](vocabulary.md#mapping), [receiver](vocabulary.md#receiver), [signature](vocabulary.md#signature), and [idempotency key](vocabulary.md#idempotency-key) entries added; [effect dedup](vocabulary.md#effect-dedup) tightened to name the key by reference; [cursor](vocabulary.md#freshness) and capability left as prose on the [grant](vocabulary.md#grant) entry, the latter checked for collision and found none |
| 52 | the operator's 2026-09-06 terminology review of role, domain, and scope | the [role](vocabulary.md#role) entry added, beside [step owner](vocabulary.md#step-owner); the [domain](vocabulary.md#domain) and [permission scope](vocabulary.md#permission-scope) entries added for the [authority](vocabulary.md#authority) tuple's second and third terms; [finding scope](vocabulary.md#finding-scope) and [waiver scope](vocabulary.md#waiver-scope) added as qualified compounds rather than one entry for bare `scope`'s four senses, with bare scope left to the author and the `## Scope` heading untouched |
| 53 | the adversarial term audit of 2026-09-06 | the [governance write](vocabulary.md#governance-write) entry added — cited by name across six documents with no entry of its own, its definition already stated verbatim in `gates_and_workflows.md`; ten Not-for/Never violations fixed by unambiguous rephrasing, the `record`-for-external-system pattern found again in three new sites and a stale `steps[].phase` field removed from `data_model.md` |
| 54 | the checker-mechanism pass of 2026-09-06 | `check_foundation_vocabulary.py`'s `record` ban rebuilt as a structural, corpus-wide check — a bound term possessed by, or governed by a preposition or relative clause pointing at, a foreign-system noun — after the literal-phrase, six-document-scoped version missed two more rounds of paraphrase; 28 further Not-for/Never violations this rebuilt check found across `adapters.md`, `calendar.md`, `conformance.md`, `data_model.md`, `gmail.md`, `migration.md`, `payments.md`, `work_model.md`, and `workflows.md` fixed by the same unambiguous rephrasing; one `adapters.md` heading renamed with its four cross-references, its anchor confirmed unused outside `docs/foundation`; `as_read` extended to strip `**bold**`/`*italic*` emphasis, closing a second escape the fix found live in `data_model.md` |
| 56, rebased onto the checker-mechanism and self-awareness passes | the peering pass of 2026-09-06 | decision 55 ruled on the [external system](vocabulary.md#external-system) entry — a second instance of the record is not one; the [governance write](vocabulary.md#governance-write) entry's admission sentence extended to name a synced write's own, unattributed shape |
| 57 | the underdetermined-inputs pass of 2026-09-06 | the [`underdetermined_inputs`](vocabulary.md#underdetermined_inputs) entry added, swap-tested against `undetermined_scope` and `undeclared_dependency` |
| 57 | the naming pass of 2026-09-06 | decision 72 opened — whether `sign-off` names its record neutrally, given that the record carries three [verdict](vocabulary.md#verdict) values of which only `signed` is an approval and `signed` is itself one of them; argued in a section of its own beside the [Owner](vocabulary.md#owner-five-meanings-one-word-forbidden-alone) section, which records the same kind of finding about a different word. Opened, not ruled: the rename is the operator's and would touch files three open PRs are amending |
| 60 | the undefined-term pass of 2026-09-06 | [reason class](vocabulary.md#reason-class) added as the category entry a reader meeting `repeated_lapse` had no way to resolve, with [`repeated_lapse`](vocabulary.md#repeated_lapse), [`rounds_exhausted`](vocabulary.md#rounds_exhausted), [`unreadable_workflow`](vocabulary.md#unreadable_workflow), [`capability_denied`](vocabulary.md#capability_denied), and [`capability_unavailable`](vocabulary.md#capability_unavailable) defined beneath it; [principal binding](vocabulary.md#principal-binding) and [session_digest](vocabulary.md#session_digest) added, each carrying a rule no existing term could hold — the counting rule and the agent sign-off's required reference; `lapse_cap`, `min_tier`, `confidence_threshold`, `external_api_write`, and `verify_deployed` deliberately NOT given entries, each named instead on the entry that already owns its set — [`action_policy`](vocabulary.md#action_policy), [`action_type`](vocabulary.md#action_type), and [`step`](vocabulary.md#step) — since a value is not a term (principle 9); the drop reason named on the [dropped](vocabulary.md#dropped) entry as the [adapter](vocabulary.md#adapter)'s, not the design's; six substrate field names recorded in `migration.md#substrate-field-names-the-design-reads-and-never-adopts-as-terms` rather than promoted |
| 68 | the decision-70 ruling of 2026-09-06 | [declaration scope](vocabulary.md#declaration-scope) added — the key that selects which [workflow](vocabulary.md#workflow) declaration a [batch](vocabulary.md#batch) runs under, swap-tested against `instance`, [domain](vocabulary.md#domain), and [tenant](vocabulary.md#tenant) and distinguished from the planning level; [project](vocabulary.md#project) added beside it, defining the planning sense and banning the scoping sense; `project` retired as the name of the scoping key |
| 69 | the front-matter rollout of 2026-09-07 | decision 74 applied to this document — the revision chain moved verbatim into `revisions.md`, and each section stating three or more full-sentence rules opened with the list of them; no argument changed |


## `work_model.md`

| Revision | Pass | What changed |
|---|---|---|
| 29 | the simplification pass of 2026-09-05 | `claimant` retired for lease holder; open decision 34 |
| 31 | the memo-gap pass of 2026-09-06 | the governance list cited from its one home rather than counted; pointers to the closed-work and intake-linkage rulings |
| 34 | the workflow-format pass of 2026-09-06 | the declared case of decision 13, bounded by `hold_bound`; the unclaimed-step interval named as `unclaimed_after` |
| 36 | the second workflow-format pass of 2026-09-06 | an intake rule may key on a field a step wrote on a type it may name, with the writer in its provenance predicate; decision 36 untouched |
| 37 | the testability pass of 2026-09-06 | `blocked` retired as a status and claimability read from the checkpoint; the declared terminal set; two moments open a batch; `tasks_attached[]`; the next recurring instance created and read back before the closing sign-off; a terminal status only where the declaration permits none; the writer as the cross-type cycle check's enforcement point; C2 settled by the write contract |
| 38 | the rulings pass of 2026-09-06 | a `signed` or blocking sign-off is written under a held lease, cited from decision 44's ruling; the bootstrap set as the closed list decision 43 rules |
| 39 | the second rulings pass of 2026-09-06 | decision 36 ruled here — a rule keys on no work-model record type, the operator's lean toward every type considered and set aside; decision 43's second half cited as ruled; the C2 and `blocked` settlements marked reviewed and upheld |
| 40 | the planning pass of 2026-09-06 | a task's one `PART_OF` edge targets its parent task or a planning record; the ascent as a derived read distinct from the chain; unplanned work admitted |
| 43 | the model-and-harness-routing pass of 2026-09-06 | `runner`, already defined in `vocabulary.md`, settled as the seat a step's outcome depends on — no new type introduced |
| 47 | the priority pass of 2026-09-06 | ordering within the claimable pool given a home beside `claimable`, argued as a derived read over the ascent, `due_date`, workflow urgency, and blast radius rather than a maintained field, on the operator's connection from the ancestry reversal; a principal's "may" rather than "must" toward the highest-standing task, with decision 62 opened on whether an instance may bind the stronger form |
| 48 | the rulings pass of 2026-09-06 | decision 34 ruled — `engine` defined, `pipeline` retired for the step-path publisher; the count of four execution mechanisms unchanged |
| 49 | the event/signal/delivery pass of 2026-09-06 | one `calendar.md` anchor updated to its renamed section |
| — | the rulings-61-62-64 pass of 2026-09-06 | decision 62 ruled — "must" per class as `action_policy` data, default "may", on the shape `min_tier` and `metered_resources[]` already carry |
| 69 | the front-matter rollout of 2026-09-07 | decision 74 applied to this document — the revision chain moved verbatim into `revisions.md`, and each section stating three or more full-sentence rules opened with the list of them; no argument changed |


## `workflows.md`

| Revision | Pass | What changed |
|---|---|---|
| 29 | the simplification pass of 2026-09-05 | `operator_preview` renamed `consent`; open decision 33 |
| 31 | the memo-gap pass of 2026-09-06 | decision 39 ruled here — what intake's `link` attaches and what hydration resolves; the payment `consent` row aligned with decision 27 |
| 34 | the workflow-format pass of 2026-09-06 | the two declared intervals and the planned wait, cited in *How to read a workflow section*, `outreach`, and `operator-only`; a standing constraint on an entity a task names, read at intake |
| 35 | the consistency pass of 2026-09-06 | the three `consent`-carrying workflows cite when their checkpoint is written and what the take re-evaluates |
| 36 | the second workflow-format pass of 2026-09-06 | a bound declared as the task's `due_date` and the `operator_only` step, cited in *How to read a workflow section* and `operator-only`; a matter, a case, or a filing as a record entity the task names, under *What `link` attaches* |
| 37 | the testability pass of 2026-09-06 | `DUPLICATE_OF` at `dedupe`; `impl` closes on a mergeable pull request; `none_permitted` on `feature` and `security`; a bug needing a design choice becomes a new task; the transcript is a source, not an artifact; the `contact` allowlist at `extract` |
| 40 | the planning pass of 2026-09-06 | the `planning` workflow, the `planner` role, and the thirteenth core workflow |
| 44 | the Human Inversion mapping pass of 2026-09-06 | the `postmortem` workflow, the fourteenth core workflow; one sentence on `feature`'s `legal` placement as a choice `applies_when` already makes, not a chronological default |
| 45 | the rulings pass of 2026-09-06 | decision 33 ruled — the `steps[].phase` field dropped; the Stages line kept as authored prose, its wording an editorial matter for this document |
| 50 | the minimization-recalibration pass of 2026-09-06 | a `disclosure_lint` step added to `outreach`, the mechanical half of what `review` judged alone |
| 55 | the self-awareness pass of 2026-09-06 | `postmortem`'s entry condition extended to a `capability_denied` or `capability_unavailable` checkpoint resolved without clearing the gap; a proposed architecture change routed at `merge` like any other finding, `planning`'s `amend` step named as the closest shape, applied reflexively and not reused as a class; the bootstrapping limitation extended to a gap that prevents the proposal itself |
| 69 | the front-matter rollout of 2026-09-07 | decision 74 applied to this document — the revision chain moved verbatim into `revisions.md`, and each section stating three or more full-sentence rules opened with the list of them; no argument changed |


## Documents not yet migrated

None. Decision 74's rollout is complete: every authored document in
`docs/foundation/` carries a pointer to its table above instead of a chain in its own front matter.
`status.md` is the one document with no table — it is a dated report, not a foundation design
document, it is never keyed and never inlined, and its revisions are its own sections rather than a
front-matter chain.
