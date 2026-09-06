# Authority model: who may act, on what, under which conditions

**Keyed document:** read when the loader, grant checker, signer, approval, notify, checkpoint, grant proxy,
or A2A paths change (`conformance.md`). **Kind:** foundation; defines the model whole and marks each
undecided question **open** with its options, never resolving one to make the document complete.
**Derived from:** the README's Vision section (the tuple, the object set), ateles#378 (the operator-authored
section as decision; the swarm-spec section as proposal), synthesis `ent_b0ce322f768e4fc676b73139` (PR-20
to PR-28, PR-34 to PR-38, C8, C9, C10, C13, C14, C17), prior art `ent_08460968e6f49dac21510f4a` (Track 2),
the P4 brief `ent_683200acfb3ff5f03add966c`, `docs/multi_tenant.md`, and PR #745 operator review
(2026-09-04). What is built, and where the substrate fails open, is `status.md`. Revised by the simplification pass of 2026-09-05 (revision 29: `claimant` retired for lease holder). Revised by the memo-gap pass of 2026-09-06 (revision 31: decision 41 ruled here — write admission per entity type is default-deny, and the grant is the allowlist). Revised by the workflow-format pass of 2026-09-06 (revision 34: a required approver may be named by ownership of an entity the checkpoint's subject concerns). Revised by the consistency pass of 2026-09-06 (revision 35: the brief's Q1–Q8 and the raiser question registered as decisions 46 to 54; C13 marked settled by C9 and decision 37). Revised by the second workflow-format pass of 2026-09-06 (revision 36: a resolution on an `operator_only` action is the operator's decision and never the confirmation; the shared-instance approver cites decision 55).

## Purpose

Define authority whole: the tuple, what a principal is, how capability is granted, how it is delegated,
how an action is approved, and the structural checks and initiative objects above them. One design, so the
roadmap in `status.md` is a roadmap over it rather than a partition of it.

## Scope

Every policy decision point and enforcement point, and the entities `agent_grant`, `action_policy`,
`agent_policy`, `checkpoint`, the principal entity, and the edges below. The action gate's decision
function is `gates_and_workflows.md`; the posture for an unreachable policy source is `failure_posture.md`;
how each is recorded is `data_model.md`.

## The tuple

Authority is `principal + domain + scope + action + conditions + time`.

| Term | Meaning | Carried by |
|---|---|---|
| principal | the actor the authority belongs to (below) | the principal entity; an agent's `principal_binding` |
| domain | the region the authority covers: entity types, repositories, a workflow, a queue | `agent_grant.capabilities`; `ownership_grant` |
| scope | the operations within the domain, with per-tool parameter constraints | `agent_grant.capabilities`, `param_constraints`; `action_policy.permission_scope` |
| action | the class of an `action` entity, its `action_type`, resolved to a blast tier | `gating` (`gates_and_workflows.md`) |
| conditions | confidence threshold, recurrence graduation, per-boundary checkpoints, `operator_only` | `action_policy` |
| time | an expiry on every grant and delegation, evaluated at check time; the lease's `expires_at` is the same term on work | `agent_grant.expires_at`; `delegation_edge.expires_at` |

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

**The human principal is an `operator` entity (C9, settled).** The type whose only job is to be a
principal is the human principal: an `operator` entity, carrying identity and nothing descriptive, and
holding the ownership and delegation edges. It is the simplest of the candidates, and it pairs
symmetrically with the agent side — an `agent` is the non-human principal, an `operator` the human one, and
each is a type that exists to be pointed at. `operator_profile` remains what its name says, a **descriptive
record** — identity details, locale, preferences — and it carries no authority edges: an `ownership_grant`,
a `delegation_edge` endpoint, a quorum seat, and a separation-of-duties constraint attach to the `operator`
entity, never to the profile. A descriptive record that also carries authority is a record two unrelated
changes can touch, and one of them changes who may act.

**The mapping down.** A [credential](vocabulary.md#credential) binds to the `operator`, many-to-one, as
every credential binds to a principal. Two of them are already excluded from being the principal
themselves, and this is where they attach instead: the store's `user_id` is an authenticated credential
that binds to the `operator` acting through it, and it collapses every writer onto one value on a shared
instance — so on such an instance `user_id` identifies the instance's account and not the principal, and a
write whose only identity is that value **resolves to no principal and is recorded as unattributed**,
which is a state a reader can see rather than a silent default to the operator. The AAuth `sub` is an
agent's credential: it binds to the `agent` that presented it, and reaches the human principal only
through that agent's `principal_binding` — which is what joins the two credential systems, and what was
missing while no type sat above them.

**What stays open, and it is not this document's to close.** The shape of the identifier on the `operator`
entity, and whether a tenant is derived from the `sub` or matched on the grant, are `multi_tenant.md`
section 7's decisions 1 and 2. They are the operator's and are not settled here. Until they are, the
mapping above states which credential binds to which principal, and does not state the identifier's form
or the tenant derivation. Every other statement in this document is written against "the principal entity"
and is unchanged by this ruling.

**Tenant.** The isolation boundary; `tenant_id` and `user_id` are separate fields; default-deny tenant
scoping at the access layer; per-tenant AAuth namespacing; no cross-tenant read, write, routing, or key
reuse (`multi_tenant.md` sections 2 and 3). Open: section 7's five decisions.

**Ownership.** Named accountability for a workflow, domain, queue, or configuration entity, as an edge from
the object to a principal (`ownership_grant`), never over a routing keyword. A step owner is ownership of
one step of one workflow; a lease holder is not an owner (`vocabulary.md`). **Open (decision 46; the
brief's Q7).** Registered in `conformance.md#the-register-of-open-design-decisions`. What owning confers: sole decision below the domain's blast
tier, a required seat above it and on cross-domain actions, or both.

## Grants

An `agent_grant` is matched on the credential (`sub`, `iss`) and lists capabilities as operation × entity
types × repositories with parameter constraints; a human's grant is bound to a principal and a tenant, never
a wildcard. The per-agent pattern is the template a principal dimension extends: a loader keyed on the
agent name, a grant checker and a tool proxy keyed on the `sub`, a per-agent keypair threaded into signed
writes, a per-agent policy override, per-agent GitHub logins, a workflow resolved per project. A failed
agent load is a stub: the loader marks it, and no caller starts a runner from one (principle
5); a stub with a wildcard tool allowlist is the fail-open shape.

**A degraded read never synthesizes a value more permissive than success would have returned.** The stub
above is the case, and it is not a posture choice: a failed read that yields a wildcard capability set
inverts the direction of authority, granting *more* than the successful read would have. Principle 5 —
fail closed on the field that carries the safety meaning — forbids it outright, whatever the posture for
that read otherwise is.

**Write admission per entity type is default-deny, and the grant is the allowlist (ruled, decision 41,
2026-09-06).** Registered in `conformance.md#the-register-of-open-design-decisions`. The question was
whether every principal may write every entity type, with attribution as the control that catches a wrong
write, or whether a principal writes a type only where a grant names it. The second. It is already what the
tuple says — a capability is operation × entity types × repositories, and zero grants is deny — stated here
as the rule for the write side of the record, because the two answers are not symmetric in the direction
they fail. Attribution makes a wrong write recoverable and is required of every write (`#attribution`); it
prevents nothing, and a control that only records is a report (principle 1). A denied write costs a grant;
an admitted wrong write to a `payment_profile`, a `workflow`, or a `contact` costs a recovery through the
gate, or a fact about a person that should not have been written, and the safe direction to be unmeasured
in is the closed one (principle 5). The maintenance an allowlist costs is an author, not a process: a grant
is a declaration, not derived state, and principle 11's objection is to state that needs a watchdog to stay
true — a grant stays true until someone changes it, a stale grant fails closed by denying, and a stale
default-allow fails open by admitting. The governance types add a second control above admission — a
granted write to one of them is still an action at the gate
(`gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken`) — and
every other type is admitted by grant alone. The allowlist is not a second mechanism: it is the
`agent_grant` that already exists, read at every enforcement point (below), widened by a governance write
that decision 18 reserves to the operator by default, so the cost is the one that ruling already accepted —
a role that needs a new type waits on a grant — and not a new one. A capability naming every type is not an
allowlist but the default-allow this rule rejects, written as a grant; it is the fail-open shape the stub
paragraph above names, and the migration counts the instance's wildcard grants as a hazard for the same
reason. The read side has the same shape, stated where reads are: an agent reads only the types its
definition names, within what its grant admits (`data_model.md#what-each-actor-reads-and-writes`). **What
would reopen it:** a project whose grants prove to be ceremony — every role granted every type on its first
day — which is the finding decision 18 names for its own default, and would argue for coarser capabilities,
not for default-allow.

**The grant is read at every enforcement point.** Not from a cache: a checker answering from state it can
no longer confirm is enforcing a snapshot, and a revocation then waits for however long the cache holds,
silently, because the checker keeps answering confidently. Reading at the check is what makes revocation's
reach immediate rather than eventual. This settles the disjunction the revocation paragraph below leaves
open in favour of its first branch.

**The decision precedes the effect.** A call outside a principal's grant is refused before any effect is
taken, not after — the denial is a decision the enforcement point reaches first, and the refusal is
structured: it names the principal, the capability, and what was refused.

**A denial raises a checkpoint, and the denied principal does not route around it.** A denial ends the
attempt, and until now nothing followed it — so a denied agent improvised, and the improvisations were
each already forbidden somewhere else. The successor is the queue that exists: the principal raises one
**checkpoint on the task**, reason `capability_denied`, naming itself, the exact capability it was denied,
and the step the denial blocked. The step stays open. The checkpoint is a **request, never a grant**:
provisioning remains operator-only and out of band — joining a workspace, issuing a token, widening a
grant are the operator's actions and an agent neither performs them nor is empowered by raising the
checkpoint to have them performed. Resolving it is the operator deciding, and the grant change that may
follow is their write, not the checkpoint's effect.

And the negative, collected here because a denied principal reading one rule should find all three:
**an agent denied a capability does not route around the denial.** It does not ask another principal to
make the write on its behalf — no principal signs for another, and a verdict attributed to a principal that
did not reach it is a false record. It does not park the result on an artifact — a verdict that reached
only the artifact is an observation and never a sign-off (`failure_posture.md` rule 4). And it does not act
under another principal's credential — that is impersonation, which delegation forbids by name (below). Each
of the three is forbidden elsewhere; what was missing is one place a denied agent would actually read them.

**Custody by revocability.** A credential's custody follows from whether revoking it is possible. A
credential that *is* the asset — a wallet seed, a signing key whose compromise cannot be undone by
withdrawing it — is never materialized into a resident process: not in a daemon's environment, not in a
long-lived runner, not in a variable that outlives the operation. It is loaded inside the short-lived
subprocess that takes the one action, and that subprocess ends with the action. A revocable credential — a
token, a scoped key, anything whose reach ends when the issuer withdraws it — may be materialized, because
the recovery from its exposure exists. Two rules apply to both kinds. A credential read from a file is
returned as a value and never written into the process environment, since an environment is inherited by
every child process a runner starts, and an outbound credential so placed becomes an inbound admission
secret for anything below it. And a credential is resolved once per invocation and reused for every
retry of that invocation, because idempotency is scoped per principal: re-resolving mid-retry can present
a different credential and make the retry a second first attempt.

**Rotation is staged, never a flag day.** Because a grant is matched on the credential (`sub`, `iss`), a
credential replaced in one step is a principal whose grants stop matching. So the new credential is
admitted alongside the old one — the grant matching it is written and read back — *before* the agent
presents it, and the old credential is retired only after read-back shows admissions arriving on the new
one. The dual-admit window is the whole point: at no moment is the set of matching grants empty.

**Revocation's reach is every grant that matched the credential, and it is only as fast as the check that
reads it.** Withdrawing a credential withdraws every capability any grant conferred on it, across every
entity type and repository those grants named — a credential shared between two purposes cannot be revoked
for one of them. Reach is therefore a reason to keep credentials narrow. And revocation takes effect only
where the grant is read: a checker that loads grants once at startup and never re-reads them enforces a
snapshot, so a revocation waits for a restart, and the failure is silent because the checker keeps
answering confidently from stale data. Grants are read at every check, or from a cache whose staleness
bound is declared and whose expiry resolves to `Indeterminate` — which denies — rather than to the last
value it held.

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
as A-for-B (RFC 8693), never as B. A delegate acting on its own full standing grant is the failure this
section forbids. The `authority_chain` is a derived read model over delegation edges, grants, and
checkpoints, tenant-filtered per hop, never stored. The acceptance test for any design here is the
hardest-problem chain: A delegates to X, X assigns a task that Y claims, Y's action needs B's approval,
using C's state under D's policy, and every hop is reconstructible.

## Approval

An approval is an explicit yes, no, or veto by a required principal on a `checkpoint`, whose subject is
an action held at the gate or a task the swarm cannot advance (`gates_and_workflows.md#the-checkpoint`),
ending in a terminal state; a timeout is a terminal state that never continues. The checkpoint records
whom it awaits and who resolved it; resolution is authorized against the required approvers, not accepted from whoever
writes the status; the queue is scoped to the principals whose decision it awaits; a decline is
attributed. Notification routes to a principal or a role through the roster and channel configuration
within the tenant, never to one address for the whole swarm. No cross-principal auto-approve. Silence never
accepts.

**A required approver is a principal, a role the roster resolves, or the principal an `ownership_grant`
names on an entity the subject concerns.** The third is what lets a workflow name an approver it cannot know
at declaration: the maintainer of another plan whose field a step would correct, the principal accountable
for a registered type a step would extend, the principal accountable for the data a shared instance pools (whether that instance is itself an external system is decision 55, `adapters.md#whether-a-second-instance-of-the-record-is-an-external-system`). Each is named in
the declaration as a relation — whoever holds the `ownership_grant` on the entity the task `REFERS_TO`, or on
the type the write lands in — and resolved when the checkpoint is raised, to the principal the object's `ownership_grant` points at
(`data_model.md#relationships`: who is asked when the object needs a decision), which is what the `AWAITS`
edge then names. Nothing is added to the tuple: ownership is the edge the design already has for named
accountability (above), and this is the one thing that table says it is for. An object with no
`ownership_grant` resolves to no approver, and a checkpoint that awaits nobody is not raised as one that
awaits everybody: the raiser holds the step and records the missing ownership as a finding, which is the
`unknown`-holds shape and never a fallthrough to the operator
(`gates_and_workflows.md#declaration-batch-projection`); the hold is bounded like every hold, and its bound
is what reaches the operator. What owning confers beyond being asked — Q7 above — is unchanged: being the
required approver on a checkpoint about one's object is the narrowest of Q7's options, and the wider ones
stay open.

**A resolution on an `operator_only` action is the operator's decision, never the confirmation that the
effect happened.** The approver of such an action is also the principal who takes it, by hand, on a system
the swarm may not reach; `approved` records that decision, and what confirms the effect is a read-back, or
the operator's report written as one (`gates_and_workflows.md#an-operator_only-action-is-taken-by-the-operator-and-the-step-that-carries-it-closes-on-the-confirmation-never-on-the-resolution`). Principle 2 holds for the operator as for
any principal.

**Open (decision 47).** Registered in `conformance.md#the-register-of-open-design-decisions`. Whether the raiser of a checkpoint may resolve it
(the minimal separation of duties; prior art and brief Q2 and Q3 recommend forbidding it, and it applies
at one operator between an agent and its human — and to the operator approving their own governance
write, which decision 43 names as bearing on it).

**C13, settled by rulings made since it was opened.** The question was which entities carry the routing
table: `swarm_roster` with `channel_config` (`multi_tenant.md`), or `operator` with `team` (#378). Two
rulings answer it without naming it. C9, above, makes the `operator` entity one "carrying identity and
nothing descriptive", which leaves it nothing to carry a routing table on. Decision 37 (2026-09-06,
`gates_and_workflows.md#work-is-reviewed-on-the-record-and-a-channel-carries-only-what-awaits-the-operator-or-cannot-wait`)
rules that "which reason classes and which deliveries a given operator wants carried, and to which chat,
is data on the binding that names the channel" — the `channel_config` binding — while the roster is the
governance type that resolves a role to a principal: "`swarm_roster` (which principal fills a role)"
(`gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken`). So the
table is carried by the roster and the channel binding, as the paragraph above already says, and no
`team` entity exists in the design. What stays open about the binding is its type's name — whether
`channel_config` and `vendor_binding` are one type or two — and that is decision 35, registered.

## Structural checks: quorum and separation of duties

Rights scope what a principal may do; structural checks make an outcome depend on more than one interest,
and both are required (README). Decided: the design is multi-principal in earnest, with real separation of
duties, real quorum, and real attenuating delegation, and without enterprise-scale machinery (policy
administration consoles, role mining, certification campaigns, hierarchy-shaped approval routing); the risk
is scale, not applicability (`prior_art_for_p2_plus_is_governance_and_authorization`). Open, each
registered in `conformance.md#the-register-of-open-design-decisions` with the brief's options: **Q1, decision 48** — the counting rule (an agent
counts as its bound principal for quorum and separation of duties, or as itself, or as itself for
attribution only); **Q2, decision 49** — whether structural checks are count and disjointness over the
one approval object above or a second mechanism; **Q3, decision 50** — which checks at a dozen
principals, and the threshold's home (Safe's shape: on the governed object).

## Initiative, proposal, reprioritization

Decided (README; #378 operator section): initiative, proposal, approval, ownership, and reprioritization
are first-class objects; proposal rights are distinct from execution rights; accepting an initiative
records an explicit "what stops?" confirmed by a principal; contribution attribution and credit are in
scope; approval is risk-tiered, and a sandbox tier carries the rights to investigate and experiment without
a per-act yes. Order of first moves: the entity-model delta, then the initiative, proposal, and
reprioritization types with the tiered flow, then one bounded two-principal proving ground. Open, each
registered in `conformance.md#the-register-of-open-design-decisions` with the brief's options: **Q4, decision 51** — one approval object or two;
**Q5, decision 52** — the unit that stops, who confirms it, who may propose; **Q6, decision 53** — budget
as a scope term that attenuates or as a blast tier, and over which resources; **Q8, decision 54** —
credit as a stored object or a read model.

## Contradictions this document settles

**C9**: settled, above — the human principal is an `operator` entity; `operator_profile` stays
descriptive and carries no authority edges. **C14** and **C17**: delegation attenuates; `Indeterminate`
is deny.
**C8**: attribution is per agent by design; whether a write is traced to a per-agent signature is
`status.md`. **C13**: settled, above, by C9 and decision 37. **C19** (#378's step map says implement while the plan says design) is
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
is the brief's, with its options as the brief states them, and each is a numbered row of the register
(decisions 46 to 54).
