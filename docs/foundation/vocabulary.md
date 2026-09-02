# Vocabulary: canonical terms, by vision phase

**Vision phase:** P1 for the terms marked P1; each later term carries the phase that introduces it.
**Kind:** consolidation, not design. **Derived from:** synthesis `ent_b0ce322f768e4fc676b73139` (PR-03, PR-08,
C10), prior art `ent_08460968e6f49dac21510f4a` (A2A `TaskState`, RFC 8693, Camunda), task
`ent_da60df3beccb675ef8c8c0c5`, the #378 glossary (ux section, signed off), and
`docs/multi_tenant.md` section 5. Format follows Neotoma's `docs/vocabulary/canonical_terms.md`.

## Purpose

One list of the terms the swarm's documents, schemas, prompts, and error messages use, each with a
definition, a use, and the synonyms it forbids. P1 and P2+ terms are one list with phase markers, so the
two vocabularies cannot drift apart.

## Scope

Terms of the work model, the gate model, the failure posture, and the authority model as implemented (P1),
plus the ten #378 glossary terms (P2 to P4). A P2+ term here is a definition, not a claim that anything
implements it; `authority_model.md` says what exists.

## P1 terms

### task
**Definition:** the atomic unit of accountable work; a Neotoma `task` entity. **Use:** "work lives in Neotoma
as task entities." **Forbidden:** "chip", "ticket" (a GitHub issue is an `issue`; a task may refer to one).

### claim
**Definition:** the act by which one agent takes a task and holds its lease; atomic among concurrent
claimants, keyed on the task, confirmed by reading back the holder. **Use:** "Corvus claims content-shaped
work with `assigned_to == me`." **Forbidden:** "assign" (a push from a router; Camunda's `setAssignee`, which
overrides without a check), "pick up", "dispatch".

### lease
**Definition:** the time-bounded half of a claim: an expiry that lapses without cooperation from the holder.
The claim and the lease are one primitive. **Use:** "a killed runner's lease lapses and the task is claimable
again." **Forbidden:** "lock" (a lock outlives its holder), "heartbeat" alone (renewal is the heartbeat; the
lease is what it renews).

### created / claimed / running / released
**Definition:** the transition vocabulary. `created`: the task exists and is claimable (publication is
creation). `claimed`: one agent holds the lease. `running`: derived at read time as claim held and
`last_activity_at` within the lease window; never stored. `released`: the lease returned, by completion,
failure, or expiry. **Forbidden:** `executing` as a liveness assertion (retired; still a status value on
main), `routed`, `in flight` for `claimed`.

### dispatch
**Definition:** reserved for the push exception: named ownership where the owner is the point (an
operator-only task, a gate assigned to a specific reviewer). **Use:** "a gate is dispatched to its
`owner_agent`; a task is claimed." **Forbidden:** using it for publication, claim, or execution, the three
jobs it formerly did at once.

### release
**Definition:** returning a lease to the queue; also the reaper's action on an expired claim. Distinct from a
software release, which is the `release` entity and the Phoenicurus workflow. **Forbidden:** "re-route" (the
reaper releases; it does not choose an owner).

### gate
**Definition:** a named checkpoint in a workflow that a declared owner signs before the next phase runs;
named `pm`, `ux`, `arch`, `impl`, `pr_review`, `qa`, `legal`, `release`. **Forbidden:** "stage" (a pipeline
step), "check" (a CI status), "gate" for the execution gate below when the blast-radius decision is meant.

### execution gate
**Definition:** `evaluate_gate()`: the decision whether an action auto-executes or writes a blocking
`checkpoint_brief`, from `action_type`, blast radius, confidence, and recurrences; PR-independent.
**Forbidden:** "merge gate" as a synonym (merge is one boundary among several).

### workflow_definition
**Definition:** the declaration: a per-(project, workflow type) template of ordered gates, instantiated many
times. **Forbidden:** "workflow" alone (promises execution the entity does not perform), "pipeline".

### participation_record
**Definition:** the instance: a gate's state on one work item, keyed (work entity, gate). **Forbidden:** "gate
status" (that is the projection), "audit row".

### gate_status
**Definition:** the map on the issue entity projecting gate state for the hot path; a projection of
`participation_record`, not a second source of truth. **Forbidden:** treating it as history.

### checkpoint_brief
**Definition:** the artifact the execution gate writes when an action cannot auto-execute; the operator's
decision-queue item, resolved by a status change. **Forbidden:** "approval request" without the entity name,
"checkpoint" for a gate.

### blast radius
**Definition:** the tier an `action_type` resolves to under an `execution_policy`: `LOW` (auto-executes),
`HIGH` (checkpoints until a recurring series clears), `NEVER` (no confidence or recurrence clears it).
**Forbidden:** "risk level" (unbounded), "severity".

### action_type
**Definition:** the declared kind of thing a task does (`build`, `docs`, `publish`, `send_external_comms`,
`operator_only`, ...); the field blast radius keys on; fails closed when declared and unclassified.
**Forbidden:** inferring it from the handling agent (ateles#682).

### operator_only
**Definition:** the `action_type` marking work an agent structurally cannot do; resolves to `NEVER`.
**Forbidden:** "high blast" (a louder `HIGH` delays the wrong outcome rather than preventing it).

### escalation
**Definition:** an `escalation` entity recording a moment the swarm needs a human: reason, needed input,
options, status; today dominated by daemon-health advisories. **Forbidden:** "page" for the entity (a page is
one delivery of it), "alert".

### unknown
**Definition:** the third state of any gate, grant, or drift reader: the value could not be determined.
Never coerced to pending or to clear. **Forbidden:** "pending" or "clear" for a failed read; "legacy
fail-open" (a category one agent invented that exists in no source).

### design basis
**Definition:** the foundation document and section an issue or PR conforms to, or the statement `no design
applies` with a reason; checked mechanically, judged by reading. **Forbidden:** "reference", "see also".

## Owner: five meanings, one word forbidden alone

`owner` on its own is forbidden. Sources use it for five things (C10); each has its own term:

| Meaning | Term | Where it appears today |
|---|---|---|
| the agent that executes a gate | **gate owner** (`owner_agent`) | `workflow_definition.gates` |
| the gate currently holding an issue | **current gate** (`current_owner`) | issue entity |
| the agent a finding is routed to | **routed agent** (`owning_agent`) | `proposed_skill_update` |
| the operator with the book of business for a customer | **book-of-business owner** (P2) | `multi_tenant.md` section 5 |
| named accountability for a workflow, domain, or queue | **ownership** (P2, below) | #378 |

## P2 terms (multi-operator identity and ownership)

### tenant
**Definition:** the isolation boundary (an organization or a solo operator). **Forbidden:** "account",
"workspace" alone.

### operator
**Definition:** a human principal who directs agents. **Forbidden:** "user" when authority is meant (Neotoma's
`user_id` is the authenticated principal of the store, which collapses to one value on a shared instance;
see C9 in `authority_model.md`).

### principal
**Definition:** any actor in an authority tuple, human or agent. **Forbidden:** "owner" unless ownership is
meant, "identity" (the credential, not the actor).

### ownership
**Definition:** named accountability for a workflow, domain, or queue, carried as an edge to a principal.
**Forbidden:** "assignee" alone.

## P3 terms (delegation and approval)

### delegation
**Definition:** a scoped, time-bounded transfer of action rights; each hop attenuates (a subset of the
delegator's authority, restrictions only added). RFC 8693's distinction applies: delegation is A acting for
B and recorded as such; impersonation is A indistinguishable from B. ateles#561 describes impersonation
where delegation was intended. **Forbidden:** "assign", "handoff" without scope.

### approval
**Definition:** an explicit yes, no, or veto by a required principal, with terminal states; a timeout is an
explicit state that never continues. **Forbidden:** "LGTM", silent continuation, "resolved" without who.

### authority_chain
**Definition:** the readable path from a principal through each agent hop to the approver for one action;
RFC 8693's nested actor chain, replacing the prose `delegated via A2A by:` in `a2a_executor.py`.
**Forbidden:** "audit log" alone.

## P4 terms (distributed authority and initiative)

### initiative
**Definition:** a proposed change to what the organization pursues. **Forbidden:** "project", "epic".

### proposal
**Definition:** the ask to accept an initiative; proposal rights are distinct from execution rights.
**Forbidden:** "PR", "RFC" alone.

### reprioritization
**Definition:** the explicit "what stops?" recorded when an initiative is accepted. **Forbidden:** "priority
bump".

## Beyond the sources

The A2A comparison is the prior-art entity's: A2A's `input-required` and `auth-required` are interrupted
states distinct from terminal ones, and a `checkpoint_brief` is interrupted, not terminal; Ateles does not
share A2A's agent-asserted `working`, which has no owner and no expiry. The five-way split of `owner` is this
document's, from the sources C10 names.
