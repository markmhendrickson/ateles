# Vocabulary: canonical terms

**Keyed document:** read when a skill, an agent document, or the agent-doc renderer changes
(`conformance.md`). **Kind:** foundation; defines terms by what they are in the design, never by what a
checkout implements. **Derived from:** synthesis `ent_b0ce322f768e4fc676b73139` (PR-03, PR-08, C10), prior
art `ent_08460968e6f49dac21510f4a` (A2A `TaskState`, RFC 8693, Camunda), task
`ent_da60df3beccb675ef8c8c0c5`, the ateles#378 glossary (operator section, and the ux-signed swarm section
cited as proposal), and `docs/multi_tenant.md` section 5. Format follows Neotoma's
`docs/vocabulary/canonical_terms.md`.

## Purpose

One list of the terms the swarm's documents, schemas, prompts, and error messages use, each with a
definition and the synonyms it forbids, grouped by the document that owns it.

## Scope

A term that names an entity type is written as the entity type (`checkpoint_brief`). Terms carry no phase
marker: the roadmap is `status.md`, and a definition does not change when its implementation lands.

## Work model (`work_model.md`)

### task
**Definition:** the atomic unit of accountable work; a Neotoma `task` entity. **Forbidden:** "chip", "ticket" (a GitHub issue is an `issue`; a task may refer to one).

### claim
**Definition:** one agent takes a task and holds its lease; atomic among concurrent claimants, keyed on the
task (`work_model.md`). **Use:** "Corvus claims content-shaped work with `assigned_to == me`." **Forbidden:** "assign" (a push from a router; Camunda's `setAssignee`, which
overrides without a check), "pick up", "dispatch".

### lease
**Definition:** the time-bounded half of a claim: an expiry that lapses without cooperation from the holder.
The claim and the lease are one primitive; renewal is the heartbeat. **Forbidden:** "lock" (a lock outlives its holder), "heartbeat"
alone (the heartbeat renews the lease; it is not the lease).

### created / claimed / running / released
**Definition:** the four transition words, one event each. `created`: the task exists in the record
(publication is creation). `claimed`: one agent holds the lease. `running`: derived at read time as claim
held and `last_activity_at` within the lease window; never stored. `released`: the lease is returned
(completion, failure, or expiry); claimable again unless the status is terminal. **Forbidden:**
`executing` as a liveness assertion, `routed`, "in flight", "stuck" for a lapsed lease.

### release (of a lease)
**Definition:** returning a lease to the queue; also the reaper's action on an expired claim. The bare word
collides with the `release` gate and the `release` entity (a software release); a sentence says which.
**Forbidden:** "re-route" (the reaper
releases; it does not choose an owner).

### dispatch
**Definition:** the push exception: handing work to a named principal where that is the point (an
operator-only task, a gate handed to its `owner_agent`, the GitHub pipeline spawning a role).
**Forbidden:** using it for publication, claim, or execution on the task path, the three jobs it once did.

### reaper
**Definition:** the watchdog's role of releasing an expired lease, on expiry and nothing else.
**Forbidden:** "router", "retry loop" (repeated expiry escalates).

## Gate model (`gates_and_workflows.md`)

### gate
**Definition:** a named step in a workflow that a declared owner signs before the next phase runs; the
names are data on the `workflow_definition` (`pm`, `ux`, `arch`, `impl`, `pr_review`, `qa`, `legal`,
`release`, and any a workflow declares). Distinct from the execution gate: a gate is a phase in a sequence;
the execution gate is a decision at the moment of action. **Forbidden:** "stage" (a pipeline step), "check" (a CI status),
"checkpoint" (that is the execution gate's artifact).

### workflow_definition
**Definition:** the declaration: a per-(project, workflow type) template of ordered gates, instantiated many
times. **Forbidden:** "workflow" alone (promises execution the entity does not perform), "pipeline".

### participation_record
**Definition:** the instance: a gate's state on one work item, keyed (work entity, gate). **Forbidden:** "gate
status" (that is the projection), "audit row".

### gate_status
**Definition:** the map on the issue entity projecting gate state for the hot path; a projection of
`participation_record`, not a second source of truth. **Forbidden:** treating it as history.

### execution gate
**Definition:** `evaluate_gate()`: the decision whether an action auto-executes or writes a
`checkpoint_brief`, from `action_type`, blast radius, confidence, and recurrences; PR-independent.
**Forbidden:** "merge gate" as a synonym (merge is one boundary among several), "gate" alone when this
decision is meant.

### execution_policy
**Definition:** the entity the execution gate reads: low and high blast action types, the confidence
threshold, the recurrence count that graduates a series, the always-checkpoint boundaries, the permission
scope. **Forbidden:** "config", "settings".

### action_type
**Definition:** the declared kind of thing a task does (`build`, `docs`, `publish`, `send_external_comms`,
`operator_only`, ...), set when the task is created from what it does; the field blast radius keys on;
fails closed when declared and unclassified. **Forbidden:** inferring it from the handling agent.

### blast radius
**Definition:** the tier an `action_type` resolves to under an `execution_policy`: `LOW` (auto-executes at
or above the confidence threshold, or once a recurring series graduates), `HIGH` (checkpoints until a
recurring series graduates), `NEVER` (no confidence or recurrence clears it). **Forbidden:** "risk level"
(unbounded), "severity".

### confidence
**Definition:** the proposing agent's score that the action is right, compared with the policy's threshold.
**Forbidden:** a default of zero standing in for a score.

### operator_only
**Definition:** the `action_type` marking work an agent structurally cannot do; resolves to `NEVER`.
**Forbidden:** "high blast" (a louder `HIGH` delays the wrong outcome rather than preventing it).

### checkpoint_brief
**Definition:** the artifact the execution gate writes when an action cannot auto-execute: an interrupted
state awaiting a principal's decision, recording whom it awaits and who resolved it. **To checkpoint** an
action is to write one and hold. **Forbidden:** "approval request" without the entity name, "checkpoint"
for a gate.

## Authority model (`authority_model.md`)

### authority
**Definition:** the right to take an action: `principal + domain + scope + action + conditions + time`.
**Forbidden:** "permission" alone (a scope term), "access".

### principal
**Definition:** an actor authority is attributed to, human or agent; an entity in the record that edges
point to. **Forbidden:** "owner" unless ownership is meant, "identity" (the credential, not the actor),
"user" (the store's authenticated credential).

### operator
**Definition:** a human principal who directs agents. **Forbidden:** "user" when authority is meant, "admin".

### agent
**Definition:** a non-human principal defined by an `agent_definition`, acting as a bound principal.
**Forbidden:** "bot", "worker" (the process running an agent is a runner).

### tenant
**Definition:** the isolation boundary (an organization or a solo operator); no read, write, routing, or key
crosses it. **Forbidden:** "account", "workspace" alone.

### grant
**Definition:** an `agent_grant`: the domain and scope a principal holds, matched on its credential, as
operation × entity types × repositories with parameter constraints and an expiry. Zero grants is deny.
**Forbidden:** "permissions" (a capability is one row of a grant), "allowlist" (one enforcement of it).

### ownership
**Definition:** named accountability for a workflow, domain, queue, or configuration entity, carried as an
edge to a principal (`ownership_grant`). **Forbidden:** "assignee" alone.

### delegation
**Definition:** a scoped, time-bounded transfer of action rights, recorded as an edge from delegator to
delegate; each hop attenuates (a subset of the delegator's authority, restrictions only added). Delegation
is A acting for B and recorded as such; impersonation is A indistinguishable from B (RFC 8693).
**Forbidden:** "assign", "handoff" without scope.

### authority_chain
**Definition:** the readable path from a principal through each delegation hop to the approver for one
action; a derived read model over delegation edges, grants, and checkpoints, never stored (RFC 8693's
nested actor chain). **Forbidden:** "audit log" alone.

### approval
**Definition:** an explicit yes, no, or veto by a required principal on a `checkpoint_brief`, with terminal
states; a timeout is a terminal state that never continues. **Forbidden:** "LGTM", silent continuation,
"resolved" without who.

### quorum / separation of duties
**Definition:** the structural checks: a quorum is m-of-n required principals on one approval object;
separation of duties is a disjointness rule between its roles (raiser and resolver, proposer and approver).
They make an outcome depend on more than one interest. **Forbidden:** "required reviewers" (1-of-n is not a quorum), "sign-off" for either.

### initiative / proposal / reprioritization
**Definition:** an initiative is a proposed change to what the organization pursues; a proposal is the ask
to accept one, and proposal rights are distinct from execution rights; a reprioritization is the explicit
"what stops?" recorded when an initiative is accepted, confirmed by a principal. **Forbidden:** "project",
"epic", "PR", "RFC" alone, "priority bump".

## Failure posture (`failure_posture.md`)

### halt
**Definition:** the state in which the swarm does no work because its record is unreachable, while it
keeps observing and announces itself off-Neotoma. **Forbidden:** "degraded mode",
"fallback", "offline mode".

### unknown
**Definition:** the third state of any gate, grant, drift, or reachability reader: the value could not be
determined. Never coerced to pending or to clear; at an enforcement point it resolves to deny.
**Forbidden:** "pending" or "clear" for a failed read, "legacy fail-open" (no such category exists).

### escalation
**Definition:** an `escalation` entity recording a moment the swarm needs a human: reason, needed input,
options, status. **Forbidden:** "page" (one delivery of it), "alert".

## Conformance (`conformance.md`)

### kernel / keyed document
**Definition:** a kernel document is read on every review; a keyed document when a changed path matches
its key. Each header says which. **Forbidden:** "core docs", "the P1 docs".

### lens
**Definition:** one reviewing perspective on the panel (pm, ux, arch, qa, ...), run by its gate owner.
**Forbidden:** "reviewer" unqualified.

### design basis
**Definition:** the foundation document and section an issue or PR conforms to, or the statement `no design
applies` with a reason; checked mechanically, judged by reading. **Forbidden:** "reference", "see also".

### status
**Definition:** the dated measurement of the gap between the foundation and a checkout (`status.md`);
regenerated, never maintained. **Forbidden:** citing it from a foundation document.

## Owner: five meanings, one word forbidden alone

`owner` on its own is forbidden. Sources use it for five things (C10); each has its own term:

| Meaning | Term | Field |
|---|---|---|
| the agent that executes a gate | **gate owner** | `workflow_definition.gates[].owner_agent` |
| the gate currently holding an issue | **current gate** | issue `current_owner` |
| the agent a finding is routed to | **routed agent** | `proposed_skill_update.owning_agent` |
| the operator with the book of business for a customer | **book-of-business owner** | `multi_tenant.md` section 5 |
| named accountability for a workflow, domain, or queue | **ownership** (above) | `ownership_grant` |
