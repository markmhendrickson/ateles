# Authority model: who may act, on what, under which conditions

**Keyed document:** read when the loader, grant checker, signer, approval, notify, checkpoint, grant proxy,
or A2A paths change (`conformance.md`). **Kind:** foundation; defines the model whole and marks each
undecided question **open** with its options, never resolving one to make the document complete.
**Derived from:** the README's Vision section (the tuple, the object set), ateles#378 (the operator-authored
section as decision; the swarm-spec section as proposal), synthesis `ent_b0ce322f768e4fc676b73139` (PR-20
to PR-28, PR-34 to PR-38, C8, C9, C10, C13, C14, C17), prior art `ent_08460968e6f49dac21510f4a` (Track 2),
the P4 brief `ent_683200acfb3ff5f03add966c`, `docs/multi_tenant.md`, and PR #745 operator review
(2026-09-04). What is built, and where the substrate fails open, is `status.md`. Revised by the simplification pass of 2026-09-05 (revision 29: `claimant` retired for lease holder). Revised by the memo-gap pass of 2026-09-06 (revision 31: decision 41 ruled here — write admission per entity type is default-deny, and the grant is the allowlist). Revised by the workflow-format pass of 2026-09-06 (revision 34: a required approver may be named by ownership of an entity the checkpoint's subject concerns). Revised by the consistency pass of 2026-09-06 (revision 35: the brief's Q1–Q8 and the raiser question registered as decisions 46 to 54; C13 marked settled by C9 and decision 37). Revised by the second workflow-format pass of 2026-09-06 (revision 36: a resolution on an `operator_only` action is the operator's decision and never the confirmation; the shared-instance approver cites decision 55). Revised by the testability pass of 2026-09-06 (revision 37: a parameter constraint on a write capability as a field allowlist — the mechanical half of minimization at capture; `AWAITS` resolves a role to principals). Revised by the rulings pass of 2026-09-06 (revision 38: decisions 46, 48, 49, 51, and 54 ruled here, and 50 and 53 in one half each — what owning confers; the counting rule; structural checks as reads over the checkpoint's principal edges, with the thresholds' home on the `action_policy`; initiative approval as the checkpoint; budget as an attenuating scope term; credit as a read model). Revised by the second rulings pass of 2026-09-06 (revision 39: decisions 47 and 52 ruled here, and the second halves of 50 and 53 — the raiser does not resolve, the operator's self-resolution marked; what stops is a task, confirmed through the checkpoint by the owner seat, proposing a grant capability; which checks and which metered resources are `action_policy` values, fail-closed where unwritten).

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
one step of one workflow; a lease holder is not an owner (`vocabulary.md`). What owning confers is decision
46, ruled below.

### What owning confers: the required seat

**Ruled (decision 46, the brief's Q7, 2026-09-06): owning an object makes the principal its
`ownership_grant` names the required approver on any checkpoint whose subject concerns the object — the seat
— and confers nothing else.** Registered as ruled in `conformance.md#the-register-of-open-design-decisions`.
Exclusivity below the domain's blast tier is not a property of owning; where an operator wants a domain acted
in by one principal alone, that is the grants, configured as narrowly as wanted.

**The question.** Q7 asked what owning confers: sole decision below the domain's blast tier; a required seat
above it and on cross-domain actions; or both. Revision 34 wrote the seat into `#approval` as "the narrowest
of Q7's options" and left the wider ones open.

**Why the seat alone.** Below the tier the gate takes the action without a checkpoint — that is what a low
tier at threshold means (`gates_and_workflows.md#confidence-and-three-blast-tiers`) — so "sole decision below
the tier" either adds nothing, because nobody is asked, or is a standing permit the accountable principal
holds over the gate's answer, and a standing approval is not a policy
(`gates_and_workflows.md#the-checkpoint-is-written-where-the-gate-first-holds-the-action-and-the-permit-is-decided-at-the-take`).
A below-tier say-so would be a second permit path beside the gate, the parallel mechanism principle 6
forbids. What the first option was reaching for — that only the accountable principal acts in its domain — is
already the grants: ruled decision 41 makes the `agent_grant` the allowlist read at every enforcement point,
and a domain in which one principal's grant names a type and no other's does is an exclusive domain, written
as grant configuration and reviewable as one. So exclusivity is available, and it is not what owning is. What
owning is, the relationships table already says in one line: `ownership_grant` is "who is asked when the
object needs a decision" (`data_model.md#relationships`), and the seat is that sentence made a rule —
resolved when the checkpoint is raised, named by the `AWAITS` edge, on every checkpoint whose subject concerns
the object, whatever tier raised it and whether or not the action crosses domains.

**Cost accepted.** An accountable principal is asked and is never sole; an operator who wants exclusive
domains writes exclusive grants, and reads exclusivity from the grants rather than from ownership edges.

**What would reopen it.** A decision an object needs that no checkpoint carries — a below-tier action whose
taking should have asked the accountable principal — which would argue for the tier, or for the
`always_checkpoint_boundaries` the policy already has, and not for a right on the edge.

**Matrix.** AU-16 and the approver-by-ownership rows already test the seat; no row is added.

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

**A parameter constraint on a write capability is a field allowlist.** The grant that admits a principal's
writes to a type may name the fields it may write, and a write carrying a field outside them is denied at
admission exactly as a write to a type the grant does not name is — read at the write, structured, and never
after the fact. That is the mechanical half of minimization at capture
(`gmail.md#what-this-adapter-refuses`, refusal 1; `calendar.md#what-this-adapter-refuses`, refusal 1;
`workflows.md#meeting-processing`, `extract`): what a `contact` may hold from a transcript, a mailbox, or a
calendar is the allowlist on the grant of the step owner or adapter that writes it, declared where every
other capability is (`data_model.md#concepts`, `param_constraints`), and what within an admitted field is
incidental or sensitive stays the writer's judgement, reviewed as one. Nothing is added to the tuple:
`param_constraints` is the scope term, and a field list is one of its forms.

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
names on an entity the subject concerns.** In every case the `AWAITS` edge names principals: a role is
resolved through the roster when the checkpoint is raised, and the role itself is carried in `needed_input`,
since an edge's target is a principal and never a role (`data_model.md#relationships`). The third is what lets a workflow name an approver it cannot know
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
is what reaches the operator. What owning confers beyond being asked — Q7, decision 46 — is ruled at
`#what-owning-confers-the-required-seat`: being the required approver on a checkpoint about one's object is
the whole of it, and exclusivity is grant configuration.

**A resolution on an `operator_only` action is the operator's decision, never the confirmation that the
effect happened.** The approver of such an action is also the principal who takes it, by hand, on a system
the swarm may not reach; `approved` records that decision, and what confirms the effect is a read-back, or
the operator's report written as one (`gates_and_workflows.md#an-operator_only-action-is-taken-by-the-operator-and-the-step-that-carries-it-closes-on-the-confirmation-never-on-the-resolution`). Principle 2 holds for the operator as for
any principal.

### The raiser of a checkpoint does not resolve it, and the operator's self-resolution is marked

**Ruled (decision 47, 2026-09-06, with 43): the principal that raised a checkpoint may not resolve it, with
one exception — the operator may resolve a checkpoint the operator raised, and the resolution is marked
self-resolved on the record.** Registered as ruled in `conformance.md#the-register-of-open-design-decisions`.
A resolution is authorized against `AWAITS` and refused where the resolver, under the counting rule
(`#the-counting-rule-an-agent-counts-as-its-bound-principal`), is the principal `RAISED_BY` names — an agent
bound to the operator raising a checkpoint the operator resolves is the operator resolving its own. The
operator's resolution of the operator's own checkpoint is admitted only where it carries the `self_resolved`
mark (`data_model.md#concepts`); a self-resolution written without the mark is refused, as every other
principal's is refused with or without one.

**Why.** Fail closed (principle 5): `gates_and_workflows.md#the-checkpoint` already makes the raiser and
the resolver distinct roles on the object, and the payment's disjoint payer and verifier (AU-18) is the same
check ruled for one class; the general rule is the one those two already are, and the prior art under
`#prior-art` — prevent-self-review, dynamic separation of duty — is the smallest structural check for the
same reason. The exception follows from decision 43 rather than softening it: the operator's own governance
write after bootstrap is gated
(`conformance_suite.md#what-the-bootstrap-set-is-and-whether-the-operators-later-governance-writes-are-gated`),
so the operator raises checkpoints only the operator can resolve, and a rule with no exception would deadlock
a solo operator on every change to the swarm. Clark-Wilson's caveat, named under prior art since this
document's first revision, says why the exception is marked and not silent: one interest cannot be separated
from itself, so the check is recorded as unsatisfied rather than pretended — a marked self-resolution is
inspectable, a reader counting the operator's self-approvals reads the mark, and an unmarked one would be the
side door 43 closes, reopened at the resolution. The mark is the resolver's own assertion at the write,
reconciled against `RAISED_BY` and `RESOLVED_BY` by the refusal, in the shape decision 32 gave the `verdict`:
not derived state a process keeps true, but a claim the record checks once, at the write.

**Cost accepted.** For a solo operator the check is ceremony on the operator's own writes, made readable
rather than blocking; every self-resolution is one more row a reader can count, which is the cost 43 accepted
and the reason it is countable.

**What would reopen it.** A second principal who must self-resolve — an owner seat that is the only awaited
principal on a checkpoint it raised, and nobody else who could be — which would argue for the exception per
seat rather than per operator, and would first have to say why `AWAITS` named nobody else.

**Matrix.** AU-17 is mechanical; decision 43's governance cell reads the mark (`conformance_suite.md`).

**C13, settled by rulings made since it was opened.** The question was which entities carry the routing
table: `swarm_roster` with `channel_config` (`multi_tenant.md`), or `operator` with `team` (the swarm-spec
proposal the header cites). Two
rulings answer it without naming it. C9, above, makes the `operator` entity one "carrying identity and
nothing descriptive", which leaves it nothing to carry a routing table on. Decision 37 (2026-09-06,
`gates_and_workflows.md#work-is-reviewed-on-the-record-and-a-channel-carries-only-what-awaits-the-operator-or-cannot-wait`)
rules that "which reason classes and which deliveries a given operator wants carried, and to which chat,
is data on the binding that names the channel" — the `channel_config` binding — while the roster is the
governance type that resolves a role to a principal: "`swarm_roster` (which principal fills a role)"
(`gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken`). So the
table is carried by the roster and the channel binding, as the paragraph above already says, and no
`team` entity exists in the design. The binding's type is decision 35, ruled 2026-09-06: one type, routing
a field of it, the name deferred to the condensation pass
(`adapters.md#whether-one-binding-type-or-two-names-an-external-systems-instance`).

## Structural checks: quorum and separation of duties

Rights scope what a principal may do; structural checks make an outcome depend on more than one interest,
and both are required (README). Decided: the design is multi-principal in earnest, with real separation of
duties, real quorum, and real attenuating delegation, and without enterprise-scale machinery (policy
administration consoles, role mining, certification campaigns, hierarchy-shaped approval routing); the risk
is scale, not applicability (`prior_art_for_p2_plus_is_governance_and_authorization`). The brief's three
questions here are decisions 48, 49, and 50, each registered in
`conformance.md#the-register-of-open-design-decisions` with the brief's options: **Q1, decision 48** — the
counting rule (an agent counts as its bound principal for quorum and separation of duties, or as itself, or
as itself for attribution only); **Q2, decision 49** — whether structural checks are count and disjointness
over the one approval object above or a second mechanism; **Q3, decision 50** — which checks at a dozen
principals, and the threshold's home (Safe's shape: on the governed object). 48, 49, and 50 are ruled below.

### The counting rule: an agent counts as its bound principal

**Ruled (decision 48, the brief's Q1, 2026-09-06): for a structural check, an agent counts as the principal
its `principal_binding` names — one interest; for attribution, it is recorded as itself, A-for-B.**
Registered as ruled in `conformance.md#the-register-of-open-design-decisions`. Two agents bound to one
operator are one interest on a quorum and one party to a separation-of-duties check, and each is still the
agent that acted on the record.

**Why.** Principle 5 chooses the restrictive branch: counting an agent as its principal yields fewer
distinct interests, so a quorum is harder to reach and a separation stricter to satisfy, and the failure of
counting the other way is the one this section exists to prevent — a single interest satisfying a check meant
to require two by acting through two agents. Delegation's rule keeps attribution where it was: A acting for B is
recorded as A-for-B and never as B (`#delegation`; AU-14), so who acted stays per agent while whose interest
it was is per principal. Clark-Wilson's caveat, named under prior art since this document's first revision,
is the same statement — an operator and the agents they built may count as one interest — and the ruling
makes it the rule rather than a caveat.

**Cost accepted.** A solo operator's swarm can never satisfy a quorum of two, and a separation between an
agent and its own operator is unsatisfiable; that is true of such a swarm, not a defect in the rule, and
decision 47 (the raiser resolving) is where the one-interest case is made readable rather than pretended.

**What would reopen it.** An agent with no `principal_binding` that the design nonetheless admits as a
principal in its own right — the design has none, and admitting one would reopen `#principals` before it
reopened this.

### Structural checks are reads over the checkpoint's principal edges

**Ruled (decision 49, the brief's Q2, 2026-09-06): quorum and separation of duties are count and
disjointness over the checkpoint's own principal edges — `AWAITS`, `RESOLVED_BY`, and `RAISED_BY` — under
the counting rule above; there is no second mechanism and no second object.** Registered as ruled in
`conformance.md#the-register-of-open-design-decisions`. A quorum is met when the principals the
`RESOLVED_BY` edges count to, under 48, reach the class's threshold among those `AWAITS` names; a
separation holds when the roles a class requires disjoint resolve, under 48, to distinct principals.

**Why.** One queue, one protocol (principle 6): the checkpoint is the one held-decision object, and
`data_model.md#concepts` already lists "quorum and separation of duties over its principals" as a derived
read of that one row. A second mechanism — a vote entity, a tally, an approval set beside the checkpoint —
would be a second decision-carrying held-state type, which is what DM-19 and GW-39 turn red on, and it would
need a process to keep its count true where the edges need none (principle 11).

**Cost accepted.** None beyond the reads; a check is answered from edges the checkpoint already writes.

**What would reopen it.** A structural check whose inputs are not on the checkpoint — one over principals
who never resolved and were never awaited — which would first have to say what object it was a check on.

### The threshold's home is the `action_policy`, per class

**Ruled (decision 50, the brief's Q3, 2026-09-06): the thresholds a structural check reads live on the
`action_policy`, per action class, beside `confidence_threshold` and `consent_tolerance` — `quorum`, the
count of awaited principals whose resolution the class needs, and `disjoint_roles[]`, the role pairs on one
checkpoint that must resolve to distinct principals; absent a value the check is the fail-closed one, every
awaited principal and every named pair; and which checks apply to which classes is a value of those two
fields, policy data and not a rule of the design.** Registered as ruled in
`conformance.md#the-register-of-open-design-decisions`.

**Why the policy.** Ruled decision 28 gave the shape: a per-class policy value with a fail-closed default,
written by the operator, and the strictest reading where absent. Principle 9 sends the tuple's `conditions`
to the `action_policy` (`#the-tuple`), and a threshold is a condition on an action's approval; a threshold on
the governed object — Safe's shape, which the brief named — would be a second home for one condition, and an
`ownership_grant` carries at most one principal per object (`data_model.md#record-conventions`), so it cannot
name an m-of-n. The payment workflow's disjoint payer and verifier (AU-18) is one value of `disjoint_roles[]`
already ruled for one class, and this ruling gives it the field it was always a value of.

**Why the values are policy data, and why that closes the question.** How many interests a dozen
principals should require on which classes, and which roles must never coincide beyond the pair the payment
names, are organizational values — a judgement about how much of its own friction an organization wants — and
the design's job is to make them expressible and enforceable, as
`work_model.md#changing-the-swarm-is-work-and-it-goes-through-a-workflow-like-any-other` says of the reserved
classes, not to make them. So the design rules the shape and the default and no number. The default is ruled
decision 18's `NEVER`-until-written, extended from a class's permission to a check's parameters: until a class
carries a value it requires every awaited principal and every named pair, the strictest reading and the one an
unmeasured project should be in; and every value is a governance write to the `action_policy` with an author
and a date, class by class, the way every other value on that policy is set. A question whose whole residue is
a policy value is not an open decision — the register would otherwise hold a row for every number an operator
has yet to write, and it holds none for `confidence_threshold` or `consent_tolerance` — which is why the row
closes without a number being supplied.

**Cost accepted.** The policy grows two per-class fields; an organization of a dozen
principals writes its thresholds or runs under the fail-closed default, which asks everyone.

**What would reopen it.** A threshold that varies per object within one class — one repository
needing two approvers and another one — which would argue for the value on the object after all, and would
have to say why the class was the wrong grain.

**Matrix.** AU-19 is mechanical for 48, 49, and 50; which checks a class carries is read from the
`action_policy` under test, and the fail-closed default is the row's second case (`conformance_suite.md`).

## Initiative, proposal, reprioritization

Decided (README; the operator-authored section of the issue the header cites): initiative, proposal,
approval, ownership, and reprioritization
are first-class objects; proposal rights are distinct from execution rights; accepting an initiative
records an explicit "what stops?" confirmed by a principal; contribution attribution and credit are in
scope; approval is risk-tiered, and a sandbox tier carries the rights to investigate and experiment without
a per-act yes. Order of first moves: the entity-model delta, then the initiative, proposal, and
reprioritization types with the tiered flow, then one bounded two-principal proving ground. The brief's
questions here are decisions 51 to 54, each registered in
`conformance.md#the-register-of-open-design-decisions` with the brief's options: **Q4, decision 51** — one
approval object or two; **Q5, decision 52** — the unit that stops, who confirms it, who may propose; **Q6,
decision 53** — budget as a scope term that attenuates or as a blast tier, and over which resources; **Q8,
decision 54** — credit as a stored object or a read model. 51 to 54 are ruled below. 52 was held as the
operator's — what displaces what, who has standing to propose, and whether an initiative may stop another
principal's work read as organizational values — until each of its three parts was found to follow from a
ruling already made (51, 46, and 41), and its section says how; the README's "what stops? confirmed by a
principal" is kept as the operator's decision and given its mechanism.

### Initiative approval is the checkpoint

**Ruled (decision 51, the brief's Q4, 2026-09-06): the approval object for an initiative is the checkpoint,
and there is no second one.** Registered as ruled in `conformance.md#the-register-of-open-design-decisions`.
An initiative — a proposed change to what the organization pursues — enters intake as a task, like any ask;
the change it proposes is an action of its class, a governance write or a re-prioritization, and accepting the
initiative is the resolution of the checkpoint on that action, by the principals it awaits. No `initiative`,
`proposal`, or `approval` entity type is registered.

**Why.** Principle 6, and the design's own statement of it at the gate — do not build a second gate
(`gates_and_workflows.md#the-action-gate-is-pr-independent`); GW-39 turns red on a second decision-carrying
held-state type, and an approval object beside the checkpoint would be one. The checkpoint's subject rule
closes the other door: a subject is one of "exactly two things: an action … or a task"
(`gates_and_workflows.md#the-checkpoint`), so an initiative object held for a decision would be a third kind
of subject, and the design has said why there is no third. Ruled decision 38 already routes work that changes
what is pursued through intake as a task, and
`work_model.md#changing-the-swarm-is-work-and-it-goes-through-a-workflow-like-any-other` already makes a
change to the swarm a task whose writes are actions — an initiative is that rule at the organization's scale.
What the README decided — that initiative, proposal, and approval are first-class objects — is satisfied by
entities the record already has: the task that carries the initiative, with its text, its priority, and its
`REFERS_TO` edges; the checkpoint that carries its acceptance, with whom it awaited and who resolved it;
and, for the "what stops", the writes decision 52 names. The order of first moves above names "the
initiative, proposal, and reprioritization types"; under this ruling the entity-model delta those words
anticipated is nil for the approval object, and the first move is the task class and the checkpoint the
design has, and decision 52 rules what stops. That reading of a decided sentence is recorded here
rather than made silently.

**Cost accepted.** "Initiative" is a class of task and a vocabulary entry, not an entity type; a reader who
wants every initiative reads tasks by class and checkpoints by subject, not a table of its own.

**What would reopen it.** An initiative whose acceptance is not a decision on any action or task — a change
to what is pursued that changes no priority and writes no governance type — which would be a change the
record cannot see, and the question would be what it changed.

### What stops is a task, the owner seat confirms it through the checkpoint, and proposing is a grant capability

**Ruled (decision 52, the brief's Q5, 2026-09-06): the unit that stops when an initiative is accepted is a
task; what confirms it stopped is the owner seat on the stopped task, or the operator, through the checkpoint
whose resolution is the confirmation, with the stop read back; and who may propose an initiative is a
capability of a grant.** Registered as ruled in `conformance.md#the-register-of-open-design-decisions`.

**What stops.** An initiative is a task (`#initiative-approval-is-the-checkpoint`), so what it displaces is a
task, and there is no stop primitive beside the ones a task already has (principle 6): a batch closing naming
no successor where its declaration permits that end, or otherwise a correction to the task's `priority` — its
own field (`data_model.md#concepts`) — each an observation the initiative task `REFERS_TO`. "Stopped by
initiative X" is a read over those edges and never a status: a stopped or reopened status would be a second
held state (principle 11) and the lifecycle `work_model.md#there-is-no-task-lifecycle-there-are-batches` has
none of.

**Who confirms.** The initiative task `REFERS_TO` each task it would stop, so the checkpoint on the action it
implies concerns those tasks, and the principal an `ownership_grant` names on each is a required approver on
that checkpoint by decision 46 (`#what-owning-confers-the-required-seat`) — the seat is asked precisely
because the object it owns is what the decision concerns — and the operator is that seat where nobody else
holds it. The resolution is the confirmation: the README's "what stops? confirmed by a principal" is a
resolver recorded on a checkpoint whose subject names the stops, and nothing else records it. What the
resolution confirms is a decision; the stop is then made and read back (principle 2) — the closing sign-off
or the priority correction retrieved and asserted before the initiative's own batch proceeds — so a stop
asserted and not read back is not a stop, in the shape revision 36 gave the `operator_only` action
(`#approval`).

**Who may propose.** Creating a task of the initiative class is a capability — a parameter constraint on the
write capability for `task`, naming the class (`data_model.md#concepts`, `param_constraints`) — and it is
default-deny under decision 41 (`#grants`): a principal proposes only where a grant names it, and the grant is
widened by a governance write like every other. Proposal rights are distinct from execution rights because
they are distinct capabilities, and no second rights model is built.

**Why.** Each of the three follows from a ruling already made rather than from a value the operator holds:
51 makes the initiative a task, so the unit is a task's; 46 makes the owner the required seat on a decision
about its object, so the confirming principal is the one the design already asks; 41 makes every right a
grant, so standing to propose is a capability and not a role. What the question framed as organizational
values is answered by the seat: an initiative stops another principal's work only where that principal's seat
resolves the checkpoint that names the stop, and displacement is a priority correction that seat approved.

**Cost accepted.** No new type; one read-back per stop before the initiative's batch proceeds; a grant per
principal who may propose.

**What would reopen it.** A stop that is neither the end of a task's chain nor a priority correction — a
change to what is pursued that no task carries — which would first reopen 51.

### Budget is a scope term that attenuates

**Ruled (decision 53, the brief's Q6, 2026-09-06): a budget is a scope term — a parameter constraint on a
capability, or a term of a delegation's `scope` — that attenuates down the chain, a delegate's budget a
subset of its delegator's; consumption against it is a derived read over confirmed actions and never a
maintained balance; and which resources are metered is a value of the `action_policy`, per action class —
`metered_resources[]` — with none metered until the operator writes one.** Registered as ruled in
`conformance.md#the-register-of-open-design-decisions`.

**Why a scope term.** Delegation attenuates — restrictions only added, enforced by reading the chain
(`#delegation`) — and a budget is the canonical attenuating caveat (macaroons): "no more than *n* of *x*" is
a restriction a delegate can narrow and never widen, which is the property a tier lacks. A blast tier is a
classification of an action, not a bound on a principal, and the tier axis has three values, with the
reserved posture a resolution and not a fourth tier
(`gates_and_workflows.md#confidence-and-three-blast-tiers`); a budget as a tier would be a fourth. Principle
11 and `payments.md#reading-a-balance-an-observation-and-not-an-artifact` settle the consumption half: a
balance is an observation, never a held ledger, and what has been spent against a budget is read from the
actions confirmed under it, so nothing decrements and nothing needs a process to stay true.

**Why the resources are policy data, and why the default runs the other way from the tier's.** Money per
class is in reach through the payment classes; compute, tokens, and tasks per window are resources the design
can bound only where their consumption is read from confirmed actions, and whether a class's actions are
counted in one of them is a fact about the class the operator sets, as `consent_tolerance` and `quorum` are
set — a governance write with an author and a date, not a rule of the design, which is why the row closes
without a resource being named. The default is fail-closed on the **limit** and not on the **permission**,
and the asymmetry is the right one: an unmetered class is still gated — its every action is evaluated at the
action gate under its tier, and a reserved class resolves to `NEVER` whether or not anything meters it — so
leaving a resource unmetered loosens nothing; whereas a metered class whose budget term is written on no grant
has a limit of nothing, and a capability carrying a budget of nothing is `NEVER` for that class until a term
is written (`#grants`; ruled decision 18). Metering is the read a check needs, the permission is the gate's,
and neither stands in for the other.

**Cost accepted.** `param_constraints` and `delegation_edge.scope` gain a budget shape, and the
`action_policy` a per-class list of what is metered (`data_model.md#concepts`); a check at the gate reads
confirmed actions to answer it.

**What would reopen it.** A resource whose consumption cannot be read from confirmed actions —
one spent outside any action — which would be a resource the design cannot bound, and the question would be
why it is spent off the record.

### Credit is a read model over attribution

**Ruled (decision 54, the brief's Q8, 2026-09-06): credit is a read model over attribution — the
sign-offs, actions, and observations with the principals they carry — and is never stored.** Registered as
ruled in `conformance.md#the-register-of-open-design-decisions`.

**Why.** Principle 11, and a precedent this document already set: the `authority_chain` is "a derived read
model over delegation edges, grants, and checkpoints … never stored" (`#delegation`), and credit has less of
its own than the chain does. `#attribution` states the dependency: "a credit model on attribution that does
not hold credits the wrong principal" — credit is downstream of attribution, every write already carries the
agent that made it and the principal it acted for, and a stored credit would be a copy of that record kept
true by a process, or wrong.

**Cost accepted.** Credit is recomputed on read.

**What would reopen it.** A credit that is not a function of attribution — one assigned by a principal's
judgement rather than read from who did what — which would be a decision, and would enter the record as one.

**Matrix.** AU-20 is mechanical for 51, 52, 53, and 54; DM-19 protects 54 (no `credit` type) as it
protects 51 (no second approval type).

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

The phase-agnostic statement of the tuple and the "open" markers are this document's; every question the
brief posed is a numbered row of the register (decisions 46 to 54), with its options as the brief states
them, ruled or open as the register says; the rulings of 2026-09-06 are this document's.
