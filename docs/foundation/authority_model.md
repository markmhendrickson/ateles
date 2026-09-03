# Authority model: who may act, on what, under which conditions

**Keyed document:** read when the loader, grant checker, signer, approval, notify, checkpoint, grant proxy,
or A2A paths change (`conformance.md`). **Kind:** foundation; defines the model whole and marks each
undecided question **open** with its options, never resolving one to make the document complete.
**Derived from:** the README's Vision section (the tuple, the object set), ateles#378 (the operator-authored
section as decision; the swarm-spec section as proposal), synthesis `ent_b0ce322f768e4fc676b73139` (PR-20
to PR-28, PR-34 to PR-38, C8, C9, C10, C13, C14, C17), prior art `ent_08460968e6f49dac21510f4a` (Track 2),
the P4 brief `ent_683200acfb3ff5f03add966c`, and `docs/multi_tenant.md`. What is built, and where the
substrate fails open, is `status.md`.

## Purpose

Define authority whole: the tuple, what a principal is, how capability is granted, how it is delegated,
how an action is approved, and the structural checks and initiative objects above them. One design, so the
roadmap in `status.md` is a roadmap over it rather than a partition of it.

## Scope

Every policy decision point and enforcement point, and the entities `agent_grant`, `execution_policy`,
`agent_policy`, `checkpoint_brief`, the principal entity, and the edges below. The execution gate's decision
function is `gates_and_workflows.md`; the posture for an unreachable policy source is `failure_posture.md`.

## The tuple

Authority is `principal + domain + scope + action + conditions + time`.

| Term | Meaning | Carried by |
|---|---|---|
| principal | the actor the authority belongs to (below) | the principal entity; an agent's `principal_binding` |
| domain | the region the authority covers: entity types, repositories, a workflow, a queue | `agent_grant.capabilities`; `ownership_grant` |
| scope | the operations within the domain, with per-tool parameter constraints | `agent_grant.capabilities`, `param_constraints`; `execution_policy.permission_scope` |
| action | the declared `action_type`, resolved to a blast tier | `gating` (`gates_and_workflows.md`) |
| conditions | confidence threshold, recurrence graduation, per-boundary checkpoints, `operator_only` | `execution_policy` |
| time | an expiry on every grant and delegation, evaluated at check time; the lease is the same term on work | `agent_grant.expires_at`; `delegation_edge.expires_at` |

The shape is ABAC (XACML; Cedar). The gate and the grant checker are the policy decision points; their call
sites are enforcement points. Every decision is `Permit`, `Deny`, or `Indeterminate`, and an enforcement
point treats `Indeterminate` (unreachable policy source, no policy found, timed-out load) as `Deny`
(`failure_posture.md`, principles 5 and 7). Zero grants is deny; a grant that declares no such tool is deny
for that tool; a policy check that raises is deny. `time` needs no engine: `now < granted_at + duration`
read by the checker (OpenFGA's form).

## Principals

A principal is any actor authority is attributed to: a human (an operator) or an agent. A principal is an
entity in the record, so an ownership or delegation edge has somewhere to point (prior art: ReBAC as a data
model, not Zanzibar as a system). A credential (the store's `user_id`, an AAuth `sub`, a GitHub login, an
email address, a chat id) is a binding to a principal, many-to-one, never the principal itself; a login
string, an address, or a magic value compared as `"operator"` is a credential standing in for a principal.
An agent carries a `principal_binding`: the principal it acts as; it is recorded as itself for attribution.

**Open, the one identity decision (C9): which entity type is the human principal.** Two candidates:
`operator_profile` (exists; named by the agent policies; kept by `multi_tenant.md`) and the `operator`
entity #378 proposes (`operator_id`, `principal_id`); with it, the mapping from that entity to `user_id`
and to the AAuth `sub`. Not open: `user_id` is the store's authenticated credential and collapses every
writer onto one value on a shared instance, and the AAuth `sub` is an agent's credential; neither is a
human principal. Left open because picking here would hand the identity design two models, and
`multi_tenant.md` section 7's decisions 1 and 2 (slug or UUID; tenant derived from the `sub` or matched on
the grant) are the operator's. Every other statement in this document is written against "the principal
entity" and holds under either answer.

**Tenant.** The isolation boundary; `tenant_id` and `user_id` are separate fields; default-deny tenant
scoping at the access layer; per-tenant AAuth namespacing; no cross-tenant read, write, routing, or key
reuse (`multi_tenant.md` sections 2 and 3). Open: section 7's five decisions.

**Ownership.** Named accountability for a workflow, domain, queue, or configuration entity, as an edge from
the object to a principal (`ownership_grant`), never over a routing keyword. Open (brief Q7): what owning
confers, sole decision below the domain's blast tier, a required seat above it and on cross-domain
actions, or both.

## Grants

An `agent_grant` is matched on the credential (`sub`, `iss`) and lists capabilities as operation × entity
types × repositories with parameter constraints; a human's grant is bound to a principal and a tenant, never
a wildcard. The per-agent pattern is the template a principal dimension extends: a loader keyed on the
agent name, a grant checker and a tool proxy keyed on the `sub`, a per-agent keypair threaded into signed
writes, a per-agent policy override, per-agent GitHub logins, a workflow resolved per project. A failed
agent-definition load is a stub: the loader marks it, and no caller dispatches one (principle 5); a stub
with a wildcard tool allowlist is the fail-open shape.

## Attribution

Every write carries the agent that made it (a per-agent signature) and the principal it acted for; a shared
bearer that never identifies its caller is not attribution. Input attribution (what was read, at which
version, from how trusted a source) is part of the record. Output attribution is the precondition for
credit (below); a credit model on attribution that does not hold credits the wrong principal.

## Delegation

A delegation is a scoped, time-bounded transfer of action rights, recorded as an edge (`delegation_edge`:
delegator, delegate, scope, expiry) so the chain is readable. Each hop attenuates: the delegate's authority
is a subset of the delegator's, restrictions only added (macaroons), enforced by reading the chain in the
record while the record is single and central. Delegation is not impersonation: A acting for B is recorded
as A-for-B (RFC 8693), never as B. A delegate running on its own full standing grant is the failure this
section forbids. The `authority_chain` is a derived read model over delegation edges, grants, and
checkpoints, tenant-filtered per hop, never stored. The acceptance test for any design here is the
hardest-problem chain: A delegates to X, X dispatches Y, Y's action needs B's approval, using C's state
under D's policy, and every hop is reconstructible.

## Approval

An approval is an explicit yes, no, or veto by a required principal on a `checkpoint_brief`, ending in a
terminal state; a timeout is a terminal state that never continues. The checkpoint records whom it awaits
and who resolved it; resolution is authorized against the required approvers, not accepted from whoever
writes the status; the queue is scoped to the principals whose decision it awaits; a decline is
attributed. Notification routes to a principal or a role through the roster and channel configuration
within the tenant, never to one address for the whole swarm. No cross-principal auto-approve. Silence never
accepts.

Open: whether the raiser of a checkpoint may resolve it (the minimal separation of duties; prior art and
brief Q2 and Q3 recommend forbidding it, and it applies at one operator between an agent and its human).
Open (C13): which entities carry the routing table, `swarm_roster` with `channel_config`
(`multi_tenant.md`) or `operator` with `team` (#378).

## Structural checks: quorum and separation of duties

Rights scope what a principal may do; structural checks make an outcome depend on more than one interest,
and both are required (README). Decided: the design is multi-principal in earnest, with real separation of
duties, real quorum, and real attenuating delegation, and without enterprise-scale machinery (policy
administration consoles, role mining, certification campaigns, hierarchy-shaped approval routing); the risk
is scale, not applicability (`prior_art_for_p2_plus_is_governance_and_authorization`). Open, each with the
brief's options: **Q1** the counting rule (an agent counts as its bound principal for quorum and separation
of duties, or as itself, or as itself for attribution only); **Q2** whether structural checks are count and
disjointness over the one approval object above or a second mechanism; **Q3** which checks at a dozen
principals, and the threshold's home (Safe's shape: on the governed object).

## Initiative, proposal, reprioritization

Decided (README; #378 operator section): initiative, proposal, approval, ownership, and reprioritization
are first-class objects; proposal rights are distinct from execution rights; accepting an initiative
records an explicit "what stops?" confirmed by a principal; contribution attribution and credit are in
scope; approval is risk-tiered, and a sandbox tier carries the rights to investigate and experiment without
a per-act yes. Order of first moves: the entity-model delta, then the initiative, proposal, and
reprioritization types with the tiered flow, then one bounded two-principal proving ground. Open (brief):
**Q4** one approval object or two; **Q5** the unit that stops, who confirms it, who may propose; **Q6**
budget as a scope term that attenuates or as a blast tier, and over which resources; **Q8** credit as a
stored object or a read model.

## Contradictions this document settles

**C9**: open, deliberately, above. **C14** and **C17**: delegation attenuates; `Indeterminate` is deny.
**C8**: attribution is per agent by design; whether a write is traced to a per-agent signature is
`status.md`. **C13**: open, above. **C19** (#378's gate map says implement while the plan says design) is
a state of two records, not a design question: `status.md`.

## Prior art

XACML's four decisions name the failure class an enforcement point must not have: `Indeterminate` treated
as `Permit`. Cedar's rule (zero permits is deny; forbid wins) is the fix. Attenuation by construction
(macaroons) is the invariant delegation carries. RFC 8693's nested `act` claim is the shape of
`authority_chain`. Clark-Wilson's caveat, that separation of duties fails under collusion, is why an
operator and the agents they built may count as one interest (Q1). GitHub's prevent-self-review and NIST
dynamic separation of duty are the smallest structural check. Sources: `ent_08460968e6f49dac21510f4a`.

## Beyond the sources

The phase-agnostic statement of the tuple and the "open" markers are this document's; every open question
is the brief's, with its options as the brief states them.
