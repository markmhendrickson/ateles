# Authority model: who may act, on what, under which conditions

**Keyed document:** read when the loader, grant checker, signer, approval, notify, checkpoint, grant proxy,
or A2A paths change (`conformance.md`). **Kind:** foundation; covers the authority model in full and marks each
undecided question **open** with its options, rather than resolving one to make the document look complete.
**Derived from:** the README's Vision section (the tuple, the object set), ateles#378 (the operator-authored
section as decision; the swarm-spec section as proposal), synthesis `ent_b0ce322f768e4fc676b73139` (PR-20
to PR-28, PR-34 to PR-38, C8, C9, C10, C13, C14, C17), prior art `ent_08460968e6f49dac21510f4a` (Track 2),
the P4 brief `ent_683200acfb3ff5f03add966c`, `docs/multi_tenant.md`, and PR #745 operator review
(2026-09-04). What is built, and where the substrate fails open, is `status.md`.
Amendment history: `revisions.md#authority_modelmd`.
## Purpose

Define the authority model in full: the tuple, what a principal is, how capability is granted, how it is
delegated, how an action is approved, and the structural checks and initiative objects above them. One
design, so the roadmap in `status.md` is a roadmap over it rather than a partition of it.

## Scope

Every policy decision point and enforcement point, and the entities `agent_grant`, `action_policy`,
`agent_policy`, `checkpoint`, the principal entity, and the edges below. The action gate's decision
function is `gates_and_workflows.md`; the posture for an unreachable policy source is `failure_posture.md`;
how each is recorded is `data_model.md`.

## The tuple

Authority is `principal + domain + scope + action + conditions + time` ([domain](vocabulary.md#domain) and
[permission scope](vocabulary.md#permission-scope) are the vocabulary's names for the second and third
terms; this table keeps the tuple's own short spelling).

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

### Two examples: the same three decisions, at each of the two decision points

The two decision points are asked different questions, and a reader who has only the paragraph above cannot
tell which one answers which. The grant checker answers *may this principal do this at all* — the tuple's
`domain` and `scope` terms, read from the `agent_grant` matched on the credential — and its enforcement
point is the write, ahead of any effect (`#grants`). The gate answers *may this action be taken* — the
tuple's `action` and `conditions` terms, read from the `action_policy`
(`gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken`) — and its
enforcement point is the take. A write that is also an action passes both, in that order, and the two
examples below are one of each.

**One: the grant checker, on a write to a `contact`.** An `agent` bound to an `operator` principal holds the
`extract` step of a meeting-processing batch, and would write a `contact` the transcript named. It presents
its AAuth `sub`; the checker matches the `agent_grant` on (`sub`, `iss`), reads `capabilities[]` and
`param_constraints`, and reads `expires_at` against the clock. The enforcement point is the write itself,
which is what makes the three answers different things a reader can see:

| The checker returns | Because | What the enforcement point does |
|---|---|---|
| `Permit` | a capability names the write operation on `contact`, the fields the write carries are inside the capability's field allowlist, and `now < expires_at` | the write lands, carrying the agent that made it and the principal it acted for (`#attribution`) |
| `Deny` | no grant matches the credential; or no capability names `contact`; or the write carries a field the allowlist does not (`#grants`) | no write is attempted — the decision precedes the effect — and the refusal names the principal, the capability, and what was refused; the agent raises one checkpoint on the task, reason `capability_denied`, and does not ask another principal to make the write for it |
| `Indeterminate` | the record holding the grant is unreachable, or the load timed out | the enforcement point treats it as `Deny`. Nothing is written and nothing is guessed: a failed read never synthesizes a wildcard capability set, because that would grant more than success would have (`#grants`). Where the unreachable source is the record itself, this is not one denied write but the halt — no claim, no step opening, no gate decision (`failure_posture.md#the-decision`) |

Two properties of this example are the ones the prose above states abstractly. The grant is read *here*, at
this write, and not from a cache — which is why revoking the credential reaches this write and not the one
after a restart (`#grants`). And a `Deny` here is not the end of the matter: the step stays open, and the
checkpoint is a request the operator resolves, never a grant the agent obtained by raising it.

**Two: the gate, on a `merge_pr` action from the same batch.** The `impl` step's work produced an `action`
of class `merge_pr`, and a principal asks the gate whether it may be taken. The gate reads the class, the
action's `confidence`, the `action_policy`, and the class's action series — no repository and no pull
request (`gates_and_workflows.md#the-action-gate-is-pr-independent`):

| The gate returns | Because | What the enforcement point does |
|---|---|---|
| `Permit` | the policy lists the class in `low_blast_action_types[]` and confidence is at `confidence_threshold`, or the series has cleared `recurrence_count` | the adapter takes the action, and the confirmation is the `result_ref` read back from the external system (`data_model.md#concepts`) |
| `Deny` | the class is `operator_only`, or is listed in neither tier, or is a governance class with no policy value — each resolves to `NEVER` ahead of the confidence axis (`gates_and_workflows.md#confidence-and-three-blast-tiers`) | the action is held, not dropped: a checkpoint is written on the action, reason `gate_hold`, awaiting the principals `AWAITS` names. Resolution is not itself the permit — the gate is asked again at the take (`gates_and_workflows.md#the-checkpoint-is-written-where-the-gate-first-holds-the-action-and-the-permit-is-decided-at-the-take`) |
| `Indeterminate` | the `action_policy` cannot be read | deny, and the unreachable source is the record, so the swarm halts rather than falling back to a policy with an empty low-blast set (`failure_posture.md#the-decision`) |

The contrast the two tables are for: the checker's `Deny` and the gate's `Deny` are both refusals, and
neither ends the work. The checker's leaves an open step and a request for a capability the principal does
not have; the gate's leaves an action the principal *may* be permitted to take, held for a decision only a
required approver can make. And on `Indeterminate` both collapse to the same place, because the source they
could not read is the same record the swarm's every other decision needs.

## Principals

**The rules in this section.**

- The human principal is an `operator` entity (C9, settled).
- What stays open, and it is not this document's to close.
- [What owning confers: the required seat](#what-owning-confers-the-required-seat).
- [Whether one operator's several instances of the record are one record or several](#whether-one-operators-several-instances-of-the-record-are-one-record-or-several) — ruled, decision 76: several records, one identity per instance, an explicit binding that fails closed when ambiguous, and a stated non-merge rule.

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
entity is `multi_tenant.md` section 7's decision 1, registered as decision 79 in `conformance.md`. It is
the operator's and is not settled here. Until it is, the mapping above states which credential binds to
which principal, and does not state the identifier's form. The second of that pair — whether a tenant is
derived from the `sub` or matched on the grant — **is now settled**: decision 80 rules it **matched on the
grant**, as `match_tenant`, so nothing is read out of a subject to reach a tenant, and the mapping above is
unaffected because the `sub` binds an agent to its principal and never to a tenant
(`multi_tenant.md#the-tenant-is-matched-on-the-grant-not-derived-from-the-subject`). Every other statement
in this document is written against "the principal entity" and is unchanged by this ruling.

**Tenant.** The isolation boundary; `tenant_id` and `user_id` are separate fields; default-deny tenant
scoping at the access layer; per-tenant AAuth namespacing; no cross-tenant read, write, routing, or key
reuse (`multi_tenant.md` sections 2 and 3). A grant carries the tenant it is scoped to, as `match_tenant`,
and no tenant is derived from a subject (decision 80). Open: section 7's other four decisions, registered
as decisions 79 and 81 to 83 in `conformance.md` since that document joined the set (decision 77).

### Identity is answered here, and it is not a document of its own

**Ruled (decision 75, 2026-09-07): the corpus needs no `identity_model.md`. Identity is one question —
which principal an actor resolves to — and this document is where it is answered; the concepts that appear
to want a document of their own are not asking it.** Registered as ruled in
`conformance.md#the-register-of-open-design-decisions`.

**The question.** Whether the design owes a keyed document for the identity model beside this one, on the
argument that identity serves consumers this document does not cover: `provenance`, `idempotency`,
`signature`, and `attribution` are each cited across nine or more foundation documents, and *who wrote this
record*, *is this the same write twice*, and *did this arrive from where it claims* are not authorization
questions.

**Why not, and it turns on what those four concepts actually are.** Three of the four are not identity
questions, and the corpus says so in its own definitions rather than by inference:

- **Provenance names sources, not principals.** The record's provenance links an observation to the source
  it was interpreted from, and where the value was extracted rather than transcribed, to the interpretation
  that produced it (`adapters.md#what-the-record-supplies-and-what-an-adapter-therefore-never-builds`). What
  the corpus writes into it is an adapter, an external system, a delivery id, a rule, a change, and a read's
  coverage — never a principal. Its wide citation count measures how much of the design is auditable to what
  was actually read, which is principle 2's reach, not identity's.
- **An idempotency key is a write's identity, not an actor's.** It names the intent of a write so a retry
  lands once (`data_model.md#record-conventions`), and an adapter's is the external system's delivery id.
  Nothing resolves through it to a principal.
- **A signature is a per-system authenticity check on a delivery**, performed by the adapter before a
  disposition is decided (`vocabulary.md#signature`, decision 16). Its own vocabulary entry already states
  it is **not** a substitute for identity: resolving the signed actor to a principal is "a separate step
  after verification passes". The two are adjacent and deliberately distinct.

Only **attribution** is genuinely identity's consumer, and `#attribution` is a section of this document.

**What the boundary would have had to be, and why it does not hold.** The proposed line — identity answers
*who is this*, authority answers *what may they do* — cannot be drawn against the actual text, because the
statements it would move are the same statements the authority rules turn on. The credential-to-principal
mapping is what `#grants` matches on and what `#attribution` requires; the `principal_binding` exists to
carry one rule, and that rule is the counting rule for quorum and separation of duties
(`#the-counting-rule-an-agent-counts-as-its-bound-principal`). A document that stated them would either
restate those rules — the overlap invariant 12 forbids between two terms, applied to two documents — or
state them once and leave this document citing outward for its own premises.

**Where identity is already stated whole, across four homes with no gap between them.** This is the answer
to "is it findable", which is the real force of the question:

| The question | Where it is answered |
|---|---|
| what a principal is, and that a credential is never one | `#principals` |
| which credential kind binds to which principal, including AAuth's `sub` | `#principals`; `adapters.md#aauth-is-the-internal-credential-not-a-second-identity-system` |
| the agent→principal edge and the rule it carries | `#the-counting-rule-an-agent-counts-as-its-bound-principal` |
| how identity reaches a write | `#attribution` |
| how an inbound actor is resolved, and what an unresolved one yields | `adapters.md#what-the-adapter-does-with-every-event` |
| an agent's identity across external systems, and the cardinality of each binding | `adapters.md#per-agent-credentials-where-the-system-issues-them-a-shared-credential-where-it-does-not` |

**Cost accepted.** A reader asking "where is identity" finds no file named for it and follows
`vocabulary.md`'s [principal](vocabulary.md#principal) and [credential](vocabulary.md#credential) entries
to this section instead. That is the cost of every concept the design states where its rules are rather
than where its name is, and the alternative was a keyed document owing a conformance-matrix row per
rule-bearing heading, a reading-list key, a revision entry, and a row in five binding inventory
assertions — paid for a document whose content is the pointer table above.

**What would reopen it.** An identity rule with a consumer outside authority and outside the adapter
boundary — one that resolved an actor to something other than a principal in the record, or matched a
grant somewhere else. `adapters.md` names that shape as where a genuine second identity system would be,
and the design has none.

**The asymmetry inbound and outbound is deliberate, and is not a gap.** Outbound, an operation with no
credential is a **denial** (`adapters.md#what-the-adapter-does-with-every-event`). Inbound, a verdict from
a credential that binds to no principal is **an observation on the artifact, never a verdict** — not a
denial, because the delivery is still evidence about an artifact and dropping it would lose what the
external system reported. Both are the same fail-closed rule (principle 5) applied to what each direction
can safely fall back to: outbound has no safe fallback, inbound has one that claims less. Obligation 2
tests exactly this fallthrough (`adapters.md#the-admission-contract`).


**Ownership.** Named accountability for a workflow, [domain](vocabulary.md#domain), queue, or configuration
entity, as an edge from
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

### Whether one operator's several instances of the record are one record or several

**Ruled (decision 76, 2026-09-07, on the operator's answer).** **Several records, not one.** One operator
holding several instances of the record — a personal one, and a second shared with a client engagement,
kept apart by sensitivity rather than by tenancy — holds several records, and they must not merge. The
ground is **accountability**: not sensitivity, which is why they are separated in practice, and not
transport, which is what would make it an adapter question. It is the same ground decision 55 was ruled
on, and reading the two together is what keeps them from arguing one question twice (invariant 12):

- **Peering (decision 55)** — instances that share an accountable principal. Replication. One record,
  extended. A synced entity carries no `external_id`, has no adapter, and enters through the same
  observation machinery as a local write.
- **Several records (this decision)** — instances whose accountable principals *differ*. Not replication.
  They must not merge.

The operator is a different principal in an instance he shares with a client engagement than in his own,
which is what puts his case on this side of the line rather than 55's. `multi_tenant.md` does not reach
it either: its axis is `tenant_id`, and every guarantee it states is keyed to a boundary that here has one
value across all the stores — one tenant cannot separate stores it does not distinguish.

**Why a non-goal was not available.** The row offered "an explicit non-goal for P1–P2, revisited at P3" as
a disposition, and it was the right offer while the several-instance case was anticipated rather than
actual. It is actual: the operator answered that he already runs two instances separated by high
sensitivity and expects more as further engagements arrive. A non-goal would rest the design on a premise
his own setup contradicts, and laying the foundation would be planned against a fiction. That the question turned
on a fact rather than on a preference is why it was his to answer and not a reviewer's to derive.

**One identity per instance.** A principal holds a distinct identity in each instance, with the reads and
writes under each staying separate. This extends `#principals`' credential-to-principal mapping rather
than replacing it: that mapping is many-to-one and rules that a shared instance's `user_id` collapses
writers and resolves to no principal. A distinct identity per instance is the legitimate case the mapping
did not state — the collapse it forbids is many *writers* behind one credential, not one writer holding
one credential per record he is accountable in.

**The binding is explicit, and an ambiguous binding fails closed.** Which instance a task's reads and
writes belong to is carried by the task or the step, declared. Today an instance is a property of where a
session happened to be launched, which is not a declaration and cannot be reviewed. Where the binding is
absent or ambiguous, the resolution **fails closed** — the work is put to the operator, and never resolved
to whichever instance is configured first. This is not a new posture: it is principle 5 (fail closed on
the field that carries the safety meaning) at the enforcement point this document's `#scope` owns, and it
is the posture
`failure_posture.md#a-task-whose-inputs-cannot-be-resolved-is-put-to-the-operator-not-executed-on-a-guess`
already takes for a read that resolves to no instance — asked here of the store the read is *of* rather
than of the value read. What a default would cost is specific: a read bound by configuration order rather
than by declaration crosses between the operator's personal data and a client's, which is the exact
boundary the sensitivity separation exists to hold. ateles#624 is the same ambiguity one layer down — the
same operation resolved differently depending on which configured server carried it, and the divergence
taught routing around a denial rather than surfacing it.

**The rule against merging is stated, not left to a component's discipline.** An orchestrator that reads
across two of the stores to write one brief has crossed the separation, and the design refuses it here
rather than relying on that orchestrator's care. Principle 1 is what settles the form: a mechanism that
does not bind is not a control, and a separation guaranteed by a component's own diligence is reporting
without binding — the defect class this corpus has repeatedly found in itself. The reads are partitioned
the way the writes are.

**What is still open.** The mechanism is not chosen here. A binding field's name and where it sits on a
task or a step, the form of a per-instance credential, and how the read partition is enforced are each a
mechanism, and none is named in this ruling (invariant 12). What is ruled is the shape the mechanism must
satisfy: several records, one identity per instance, an explicit binding, a fail-closed ambiguity, and a
stated non-merge rule. `multi_tenant.md#7-settled-decisions-were-the-operators` decision 1 —
registered as decision 79 — bears on the credential form and is ruled. Its decision 2, registered
as decision 80, is ruled: the tenant is matched on the grant and not derived from the subject, which leaves
the credential form here untouched, since the several-instance case this ruling concerns is one tenant.


## Grants

**The rules in this section.**

- A degraded read never synthesizes a value more permissive than success would have returned.
- Write admission per entity type is default-deny, and the grant is the allowlist (ruled, decision 41, 2026-09-06).
- A harness provides only the capabilities the principal's grant names, and a non-enforcing provider is one of them (ruled, decision 87, 2026-09-08).
- A parameter constraint on a write capability is a field allowlist.
- The tenant a grant is scoped to is carried on the grant, and is never derived from the credential's subject (ruled, decision 80, 2026-09-07).
- The grant is read at every enforcement point.
- The decision precedes the effect.
- A denial raises a checkpoint, and the denied principal does not route around it.
- Custody by revocability.
- Rotation is staged, never a flag day.
- Revocation's reach is every grant that matched the credential, and it is only as fast as the check that reads it.

An `agent_grant` is matched on the credential (`sub`, `iss`) and lists capabilities as operation × entity
types × repositories with parameter constraints; a human's grant is bound to a principal and a tenant, never
a wildcard. The tenant is a term of the grant for every principal, agent and human alike, and is not read
out of the subject: decision 80 rules it matched on the grant as `match_tenant`, on the ground that a
grant is the whole statement of what a principal may do, so a tenant taken from the subject would give one
authorization question a second home (invariant 9) and would leave a grant's tool half — which carries no
subject — outside the tenant boundary
(`multi_tenant.md#the-tenant-is-matched-on-the-grant-not-derived-from-the-subject`). The per-agent pattern is the template a principal dimension extends: a loader keyed on the
agent name, a grant checker and a tool proxy keyed on the `sub`, a per-agent keypair threaded into signed
writes, a per-agent policy override, per-agent GitHub logins, a workflow resolved per declaration scope. A failed
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
would reopen it:** an instance whose grants prove to be ceremony — every role granted every type on its first
day — which is the finding decision 18 names for its own default, and would argue for coarser capabilities,
not for default-allow.
Whether the same default-deny governs the capability surface a harness provides, rather than entity-type
writes alone, is ruled by decision 87: it does, and the harness's configuration is closed by default
(`#a-harness-provides-only-what-a-grant-names-and-a-provider-that-does-not-enforce-is-a-capability-the-grant-names`).

**Read admission per entity type is default-deny, and the grant is the allowlist, read at the read
(ruled, decision 94, 2026-09-08).** Registered in `conformance.md#the-register-of-open-design-decisions`.
Decision 41 ruled the write side and said the read side "has the same shape, stated where reads are",
pointing at the record-usage contract (`data_model.md#what-each-actor-reads-and-writes`). That pointer
described a boundary; it did not rule one, and nothing in the design admitted or refused a read. This rules
it: a principal reads an entity type only where a capability on its grant names that type, the check runs
at the read rather than at the step's declaration, and zero grants denies.

**Why `context_entity_types[]` is not already this rule — and why it is still a bound.** An `agent`'s
`context_entity_types[]` declares what the agent is *given*: the types a runner resolves and puts in front
of it when a step opens. It is authored to make an agent useful, and nothing consults it at a read, so a
type absent from it is still fetched if the agent asks, and a type present on it is not thereby something
the agent's grant admits. A field that says what to hand over is not a field that says what to refuse, and
the corpus measured the consequence — `status.md`'s revision 8 row records that `context_entity_types[]`
exists on the agent's registered type and "nothing bounds a runtime read to it". Reading it as *the*
admission rule would also put the boundary on the wrong record: admission is matched on the credential
(`#grants`), and the field is on the `agent`, so a second credential bound to the same agent would inherit
a boundary nobody granted it.

**What this corrects, and what it leaves standing (amended 2026-09-08, revision 71).** The ruling as first
written went further than the argument above carries, calling `context_entity_types[]` "a provisioning list,
not an admission rule" full stop — which reads as retiring the inner bound this document states three lines
above under decision 41 ("an agent reads only the types its definition names, within what its grant
admits") and `data_model.md#what-each-actor-reads-and-writes` states in full ("the grant is the outer bound
and the definition the inner one: a read must satisfy both"). That was a reversal presented as a
clarification, and it is not kept. **Both bounds stand.** They answer different questions and fail in
different directions: the grant is what the record *refuses*, enforced at the read against the credential;
the definition is what the agent's role *declares it needs*, an owned and reviewed statement of an agent's
information diet, corrected by amending the definition rather than circumvented at runtime. A read outside
the definition but inside the grant is a declaration defect, caught where declarations are caught, and the
suite marks its runtime half accordingly (`conformance_suite.md`, U-9). What decision 94 correctly denies
is only that the definition is the *admission* mechanism — it is not, and treating it as one is what left
reads unadmitted for as long as it did.

**Reads and writes are admitted symmetrically, and the asymmetry argument does not survive the exposure it leaves.**
The case against symmetry is real and worth stating: a denied read and an absent entity are often
indistinguishable to the caller, so a per-type read refusal leaks the existence of types a principal may
not read, and an enforcement point that leaks on denial is a poor one. But that objection prices the wrong
thing. What a per-type denial discloses is that *the instance registers a type* — the registry is design
data, enumerated in `data_model.md` and readable by anyone reading this corpus — and not that any
particular row exists. The disclosure is a schema fact already public, which is why the leak is small.
What symmetry buys is not small, and this ruling states it narrowly, because an earlier draft of it did
not. A principal that can read every type reads every type **of its own owner's data** — the `contact`,
`payment_profile`, and `conversation_message` of the operator whose grant it holds — and that is the
exposure symmetry prevents. It is not a cross-operator exposure: reads are scoped to the owner the grant
resolves to, and a principal asking for another owner's rows is refused on that ground and not this one.
Whether several operators share one instance at all is open (decisions 81 and 83, and 82 for the fork
case), so no ruling here may rest on the answer. The corrected claim is smaller than the one first written
and still carries the ruling: an agent that needs one type and can read forty reads thirty-nine it has no
reason to hold, about the person whose record it serves, and no later write retracts a fact once read. The
special-category mark (`data_model.md#record-conventions`) already states that a marked type is admitted
only to roles whose declarations give a reason to read it — which is this rule, stated for one class of
type and left unstated for the rest. Principle 5 settles the direction: the safe way to be wrong about a
read is to refuse it, and the cost of a wrong refusal is a grant, while the cost of a wrong admission is a
fact about a person reaching a principal with no reason to hold it, which no later write can retract.
Asymmetric admission would also be a second mechanism where the tuple already supplies one — a capability
is operation × entity types × repositories, and *operation* is where read and write differ, so ruling them
symmetric adds nothing to the model and ruling them asymmetric would require inventing a reason the tuple
does not carry.

**What the rule does not reach.** It admits a type, not a row: which rows of an admitted type a principal
sees is the tenancy and ownership question decisions 81 and 83 open — with decision 82 having retired the
adjacent fork question rather than answering it, since multi-operator is a property every instance has
whoever hosts it — and this ruling neither answers those nor depends on them. And a step's declared reads (`reads_to_enter[]`, `reads_to_close[]`) stay what they are —
they narrow what an admitted principal reads for that step, and they never widen a grant, which the
retrieval contract already states. **What would reopen it:** an enforcement point where the existence
disclosure is not a schema fact — a per-row read refusal that leaks whether a named person is in the
record — which is the tenancy question above and would be argued there, not here.

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
make the write on its behalf — no principal signs for another, and a conclusion attributed to a principal that
did not reach it is a false record. It does not park the result on an artifact — a conclusion that reached
only the artifact is an observation and never a verdict (`failure_posture.md` rule 4). And it does not act
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

### Where a credential comes from, and what happens when that source cannot be read

**Open.** Registered in `conformance.md#the-register-of-open-design-decisions` as decision 105. The rules
above govern a credential the swarm already holds: how it is kept out of a resident process, how it is
returned as a value rather than written into an environment, how it is resolved once per invocation, and
which grant it matches. None of them says where it came from.

That silence is total rather than partial. Counting every occurrence, including the ones in this sentence,
the verb *materialize* appears seven times across the whole directory — five in this document, of which
two are the custody rule's own statement that a long-lived credential is never materialized into a
resident process and a revocable one may be, two are this sentence counting them, and one is this
section's later use below; plus one each in `conformance.md` and `conformance_suite.md`. Not one of the
seven names a source. The design has no term for the store, no
statement of who may write one, no rule for how a credential reaches the process that holds it, and no
posture for the interval when the source is unreachable.

Four things bound an answer, and each is already stated elsewhere. Principle 5 makes an unreadable store
the safety field's own case — a credential that cannot be fetched is `unknown`, and a principal that
proceeds without one has failed open. Custody by revocability already divides credentials in two, and the
credential that is never materialized cannot share a lifecycle with the one that may be. Invariant 9
refuses a second home, so a credential whose origin could be read from a deployment *and* from a
`vendor_binding` has two answers and no authority. And the dual-admit window binds the source as much as
the grant: a store that cannot hold two live values for one principal makes the staged rotation above
unimplementable.

What this does not reopen: G17 and decision 101 ask what the credential-to-principal binding carries, and
decisions 96 and 97 rule what a check reads at the moment of the action. This asks what exists before any
of those apply.

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

### What the credential binding carries, and what a check reads to resolve a credential to a principal

**Ruled** (decision 101, 2026-09-10): **`principal_binding` carries `credential_kind`, `credential_value`,
`credential_issuer`, and `expires_at`, and a principal holding several credentials holds several edges.**
One edge per credential, properties on the edge, and no separate credential entity. Field vocabulary:

| Field | Holds | AAuth map | Host / operator map |
|---|---|---|---|
| `credential_kind` | credential family / system class | e.g. `aauth_sub` | e.g. `github_login`, `store_user_id`, `email_address`, `chat_id` |
| `credential_value` | the presented subject value | `sub` | the login, user id, address, or chat id |
| `credential_issuer` | the issuer or namespace that makes the value unique | `iss` | the external system or host namespace; absent only when the kind's namespace is already unique without one |
| `expires_at` | optional expiry bound read at check time | as the issuer states | as the issuer states |

**Endpoint / source.** No credential entity is introduced; the relationship's endpoint is the principal
reached by the presented credential, with the credential identity carried in those edge fields. For an
AAuth credential, the binding resolves `credential_value` + `credential_issuer` (`sub` + `iss`) to the
`agent`; the human operator is then reached through that agent's own `principal_binding`. For an operator
credential, the binding resolves the credential directly to the `operator`.

**Expiry / end.** `expires_at` is a read-time liveness bound, not maintained state. A binding is live when
the edge is unended and `now < expires_at` when present; retiring a credential writes or ends the edge,
exactly like other ended relationships. No sweeper is required to flip a status when the clock passes
(invariant 11).

A check resolving a credential to a principal reads the unended, unexpired `principal_binding` edges whose
`credential_kind`, `credential_value`, and `credential_issuer` (when the kind requires one) match what was
presented, and takes the principal at the other end. Where several match — which the rotation rule requires
during the dual-admit window — every one of them resolves to the same principal, because the many-to-one is
a property of the shape rather than of the read. Grant match and binding resolution use the same comparison:
AAuth `(sub, iss)` is `credential_value` + `credential_issuer` under `credential_kind` `aauth_sub`.

**Resolver outcomes.** The check returns one of the three values the grant checker already uses
(`Permit` / `Deny` / `Indeterminate`). Enforcement maps `Indeterminate` to deny; the recorded decision keeps
the third value so unknown stays distinct from a conclusion (principles 5 and 7). Diagnostics may name the
principal id and `credential_kind`; they never echo raw `credential_value`.

| Resolver outcome | Because | What the enforcement point does |
|---|---|---|
| `Permit` (resolved) | ≥1 live (unended, unexpired) `principal_binding` matches the presented `credential_kind` + `credential_value` (+ `credential_issuer` when required), and every match targets the **same** principal | Continue to grant match / attribution on that principal. Diagnostic may name principal id + credential kind; never echo `credential_value`. |
| `Deny` (no binding) | No matching edge for the presented key | Refuse the write / admission. Record as **unattributed** (`#principals`) — never default to the operator. Surface: `[COPY: no principal_binding for this credential kind — create or restore the binding]`. |
| `Deny` (expired-only) | Matching edges exist but all are ended or `now ≥ expires_at` | Distinct from no-binding. Surface expiry/retirement, not "unknown credential". Hint: `[COPY: binding expired or ended — rotate / dual-admit a live edge before presenting]`. |
| `Indeterminate` (binding source unreadable / partial) | Relationship store unreachable, timed out, or read incomplete | Treat as Deny at the enforcement point (same posture as grant-load `Indeterminate` above). Keep the third value in logs/diagnostics — do not coerce to a plain `Deny` in the recorded decision. Hint: `[COPY: binding source unavailable — retry after record health; do not guess a principal]`. |
| `Deny` (ambiguous / conflicting) | ≥2 live matching edges target **different** principals | Fail closed; do not pick first edge. Surface conflict with redacted fingerprints / principal ids only. Hint: `[COPY: conflicting principal_binding edges — operator must end the wrong edge(s)]`. |
| `Deny` (malformed presentation) | Presented credential missing required key parts (`credential_kind` / `credential_value` / issuer when the kind requires it), or kind unrecognized | Restrictive default (principle 5). Hint: `[COPY: credential presentation malformed or kind unrecognized — fix caller shape]`. |

**Examples.**

- `[COPY: dual-admit success — two live principal_binding edges with different credential_value under the same credential_kind and credential_issuer both target principal P; resolution returns Permit on P and grant match continues.]`
- `[COPY: conflict failure — two live matching edges target different principals; resolution returns Deny (ambiguous), records both principal ids with redacted fingerprints, and refuses first-edge-wins.]`

**Why this and not the alternatives, and why the choice is structural rather than a preference.** The two
candidates differ in what a wrong answer costs, and the design's own rule on that is what selects this one.
`principal_binding` is a type stage 1 registers regardless, and G26 makes registration one-way: a schema
loses fields and gains them, but a registered type is not retired. So a credential entity with its own edge
is a fourteenth type that stands forever if it proves unnecessary, where properties on the existing edge can
be superseded by adding that entity later and migrating the edges. Narrowing later is a breaking change and
widening later is not — decision 91's reasoning, applied to a type rather than to a scope.

The dual-admit window rules out the third candidate: `#grants` requires two credentials
to bind to one principal at once with the set of matching grants never empty, so a shape holding one
binding per principal is refused on that ground alone. The grant cannot be the binding, because a grant
holding a second credential becomes two grants and reopens the question one level out. And a credential
entity of its own would give the value a second home during rotation, with nothing saying which the check
reads — invariant 9's objection — where properties on the edge extend the type that already joins the two
credential systems, which is what invariant 6 asks.

**What travels with this.** The value sits on an edge, so it is read wherever the edge is read, and custody
by revocability binds that reader exactly as it binds any other holder: a credential that is the asset is
not materialized by reading its edge. That obligation now travels with the edge rather than with a separate
entity that might have carried its own access rule.

**What this does not settle.** Where the value comes from before any of these rules reach it is decision
105, and nothing here answers it: an edge carrying a value says nothing about the store that produced it.

The paragraphs below state the question as it stood before the ruling, and the constraints that bound it. `#principals` states that a
credential binds to a principal **many-to-one**, and names `principal_binding` as what joins the two
credential systems — the store's `user_id` and a host login to the `operator`, an AAuth `sub` to the
`agent`, reaching the human principal through that agent's binding. What neither that section nor
`data_model.md#relationships` states is what the binding **is**: the relationships table carries
`principal_binding` as an edge from agent to principal with no fields at all, and the credential-to-principal
side of the many-to-one has no edge type of its own. `migration.md`'s G17 is the same finding taken from the
other direction — the instance holds the bindings as a scalar field on the `agent` and on the
`agent_grant`, which it calls a credential binding living on the wrong entity.

**Why the question cannot wait for the type to be registered.** `principal_binding` is one of the thirteen
relationship types stage 1 registers
(`migration.md#ordering-dependencies-and-what-each-stage-depends-on`), and G26 states that registration
is one-way: a schema loses fields and gains them, and a registered type is not retired. So the shape
registered in stage 1 is the shape the design lives with, and registering before this is answered is a
decision taken by the write rather than by the design — the failure decision 80 names, an implementation
choosing the design because the existing keys already have its shape. The count is part of the question and
not incidental to it: a credential-to-principal edge distinct from `principal_binding` is a **fourteenth**
type stage 1 must register, so an answer that adds one changes what stage 1 writes, and an answer that
loads the existing type changes what that type carries. Either way the stage is not writable as currently
enumerated, and both are irreversible once written.

**What the answer has to carry, and what makes each of these binding rather than a preference.**

The **many-to-one is a requirement and not an observation**. A principal holds several credentials at
once — a store identity, a host login, an address on an external system, a chat identity — each of which
binds to exactly one principal, and `adapters.md#per-agent-credentials-where-the-system-issues-them-a-shared-credential-where-it-does-not`
already states that the cardinality of an agent's identity per external system is a property of the
binding. A scalar field on the agent expresses one of those and drops the rest, which is what makes the
present state a defect and not merely an unfinished field: the record cannot hold what the design says the
principal has.

**Rotation makes the many-to-one simultaneous rather than merely plural.** `#grants` rules that rotation
is staged and never a flag day: the new credential is admitted alongside the old one, the grant matching it
is written and read back before the agent presents it, and the old credential is retired only after
read-back shows admissions arriving on the new one. The dual-admit window is exactly a moment at which two
credentials bind to one principal, both live, and the design already states that at no moment is the set of
matching grants empty. An edge that cannot express two simultaneous bindings does not merely lose a
credential — it makes the rotation rule unimplementable, so this is the sharpest of the constraints and any
candidate that fails it is refused on that ground alone.

**What a check reads is a separate half of the question from what the edge carries.** Decision 96 rules
credential **pass-through**: a proxy presents the agent's own credential and holds none of its own, so the
instance matches grants on the `sub` actually presented. Decision 97 puts the enforcement point at the
record's own admission check against the requesting principal's grant, and decision 41 states that the
grant's allowlist is read at every enforcement point. Grants are matched on `(sub, iss)` — the credential —
and not on the principal, so the enforcement point reaches the principal only where something resolves the
presented credential to one. Today that resolution is the capped table scan G17 names. Whether it stays a
read over the edge, or whether the grant match makes it unnecessary at admission and needed only at
attribution, is what this decision has to say rather than leave to whichever caller first needs it.

**The candidates.**

1. **A credential entity with an edge to the principal.** The credential becomes a record of its own —
   kind, issuer, expiry, whatever identifies it — and the binding is an edge from it to the principal, one
   edge per credential, many edges per principal. The many-to-one and the dual-admit window are both direct
   consequences of the cardinality rather than conditions on a field. What it costs is a second entity type
   in a design that already resolves credentials at the grant, and it has to say what the credential entity
   holds that the grant's `sub` and `iss` do not.

2. **Properties on the binding edge itself, one edge per credential.** *(Historical candidate — chosen.)*
   `principal_binding` stays the edge type and carries the credential's identifying values — its kind, its
   issuer, and an expiry where the issuer states one — so a principal has as many edges as it has
   credentials and rotation writes a second edge before ending the first. This is the shape
   `conformance_suite.md#what-the-documents-leave-unspecified-here-and-how-each-is-recorded` already
   derives — an edge type carrying the credential kind and value, the principal, and an expiry — and that
   derivation is recorded there as proposed rather than assumed, which is the vacancy this row registers.
   Candidate 2 owed two answers the ruling above closes: the edge's source is not a credential entity —
   credential identity lives in edge fields (`credential_kind`, `credential_value`, `credential_issuer`)
   and the endpoint is the principal reached — and `expires_at` is a read-time liveness bound ended by
   writing the edge, not maintained state invariant 11 forbids.

3. **The grant is the binding, and no separate edge exists.** A grant already carries `sub`, `iss`, and
   `expires_at`, and is already matched on the credential; adding the principal to it would make the grant
   the one place a credential and a principal meet. The objection is that a grant is an authorization
   statement and a binding is an identity fact, so the two would share a lifetime that decision 41's
   default-deny does not intend — revoking a capability would retract an identity — and a credential
   presented under no grant would resolve to no principal rather than to an unattributed write, which
   `#principals` states as the state a reader can see. It also inverts a property of the grant row the
   design states deliberately: the grant carries no edges at all in `data_model.md#concepts`, which is
   what keeps an authorization statement out of the graph the authority chain is read along.

**What any answer has to survive.** Invariant 11 is the argument **for** the edge and also the test of its
fields: state that would need a watchdog, a sweeper, or a reconciler to stay correct belongs in a
relationship and not in a field, which is why the scalar the instance holds is wrong; but an edge carrying
an expiry the design does not end by writing is the same maintained state one level out, so the row has to
say which of the edge's values are written once and which are derived at a read. Invariant 9 asks the
answer to leave one home for which principal a credential belongs to, which candidate 3 satisfies by
construction and candidates 1 and 2 satisfy only if the resolution is read from the edge and nowhere else.
And principle 5 governs the resolution's failure: a credential that resolves to no principal is a write
recorded as unattributed and never one defaulted to the operator, which `#principals` already rules and
which any answer here inherits rather than restates.

**What this does not reach.** The tenant a grant is scoped to is not a term of this question: decision 80
rules it matched on the grant as `match_tenant`, and nothing is derived from a credential's subject to
reach a tenant. And the identifier's form on the `operator` entity is decision 79, ruled to a human slug
with an immutable UUID behind it; a binding names whatever that identifier is, and does not restate it.

### A capability names a tool as `tool:<surface>:<operation>`, and that is what a harness allowlist is compared against

**Ruled (2026-09-08, decision 86).** A capability names a tool the way it names everything else: as one
entry in `capabilities[]`, of the operation form `tool:<surface>:<operation>`, with `param_constraints`
carrying whatever bound the operation's arguments take. **One grammar, not five.** The five things the word
"tool" spans — a record operation, an MCP tool, a shell command, filesystem reach, and a harness's own
built-in — differ in **what mediates them and how tightly a bound on them can be enforced**, and they do not
differ in **how a grant states that a principal holds them**. Only the second is this decision's question,
and there is one answer to it.

**What the grammar is.** The `<surface>` is the domain half of the tuple — what the principal may act on —
and the `<operation>` is the permission-scope half — what it may do there. Both halves match
`[A-Za-z0-9_.-]+` or the single character `*`. The mapping to a harness's own four recognized allowlist
forms is total and lives in one normalizer that both sides of the parity test import, never a
reimplementation on each side (invariant 9): `mcp__<server>__<tool>` ⇄ `tool:<server>:<tool>`; a bare
harness tool name ⇄ `tool:harness:<name>`; `Bash(<command>)` and `Bash(<command>:*)` ⇄
`tool:shell:<command>`; bare `Bash` ⇄ `tool:shell:*`. The mapping is stated once, in full, in
[`docs/tool_grant_grammar.md`](../tool_grant_grammar.md), which this ruling ratifies as the mapping's home;
what follows is the rule, and that document is its mechanics.

**Why one grammar and not several.** Three arguments, each from a rule already standing.

*A second grammar would be a second home for one bound.* Decision 42 ruled that the tools a principal may
invoke are a dimension of its grant's `capabilities[]`. A separate field, or a separate entry form, for the
tools with no server behind them would put one question — what reach does this principal hold — in two
places, which invariant 9 forbids and which would need its own parity test, its own validator, and its own
migration. Invariant 6 says the same thing from the other side: `capabilities[]` already generalizes over
domain and scope, and a shell command is a domain and a scope.

*The differences that are real are differences of enforcement, and enforcement is not the grammar.* An MCP
call can be refused with its arguments read; a shell command cannot, because nothing mediates it; a
filesystem read is bounded by where a process runs and not by a name it is called by. Each of those is a
fact about the enforcement point, and the design already carries the term for it: a capability whose bound
no enforcement point can read is **recorded and reporting-only**, which is the state decision 42's cost
clause chose to name rather than conceal, and which principle 1 requires be named rather than counted as a
control.
Splitting the grammar to track that difference would encode an implementation fact — which mediator exists
today — into the design's vocabulary, which is the direction the corpus forbids.

*And a record operation is already named, so it is not renamed here.* `store`, `retrieve`, and `correct`
over entity types and repositories stay exactly the capabilities decision 41 made them. They are **not**
re-expressed as `tool:` operations, and a grant naming both forms for the same reach would be the overlap
invariant 12 forbids — two terms the design does not distinguish. The `tool:` prefix names reach the record
does not mediate; what the record mediates, the record's own operation names. The swap test settles the
pair: substituting `tool:mcpsrv_neotoma:store` for the `store` capability changes what is enforced — the
first bounds which tool appears in a child's menu, the second bounds which type is admitted at the write —
so the two are distinct and both earn their place.

**Filesystem reach is not a sixth thing; it is `tool:harness:<Tool>` and `tool:shell:<command>` seen from
the other end.** A process reaches a path because a tool it was given reads or writes one — `Read`,
`Write`, `Edit`, a shell command. The path space is what those capabilities *do*, not a domain a grant names
beside them. Naming paths in the grant would be a second bound on the same reach, and the argument bound
belongs where every other argument bound belongs, in `param_constraints` — enforceable through the proxy
where a proxy mediates the call, and recorded and unenforced where none does.

**Two reserved surfaces, and what makes them domains.** `harness` names the tools the harness itself
provides, whose membership is the harness's own tool list; `shell` names one command reachable through a
shell. Both are surfaces in the ordinary sense — a domain with an operation in it — and neither is a new
kind of entry. Reserving the two names is the whole cost of admitting the reach that most needs bounding
into the record, and the alternative — a grammar whose `<surface>` is an MCP server and nothing else —
leaves shell and filesystem reach permanently unnameable, which is the status quo and which decision 42
ruled against without saying how.

**A wildcard is a domain's shorthand, never a grammar's escape hatch.** `tool:*` — every operation on every
surface — is **expressible and refused**: the parser accepts it, because a grammar that cannot write down
the fail-open shape cannot describe the state a migration is migrating away from or report a wildcard as a
wildcard rather than as a parse error; and a validator refuses it **at the write**, so it never appears in a
stored grant. The ground is decision 41 in terms — a capability naming every type "is not an allowlist but
the default-allow this rule rejects" — and the tool dimension is a capability of the same array.
`tool:<surface>:*` is admissible, on three bounds: the surface's membership must be **enumerable at check
time**, and where the tool list cannot be read the capability resolves `Indeterminate`, which denies
(principle 7); it never spans surfaces, which is the domain term doing its work; and it is widened by a
governance write like any other. This is the same tiering decision 41 draws for entity types — a named type
is an allowlist entry, every type is not — applied one level down.

**`tool:shell:*` is the one surface wildcard whose enumerability bound cannot be met, and it is admitted
anyway, deliberately.** The commands reachable through a shell are not a list anyone can read back. The
design nonetheless admits it, because a build agent genuinely needs a shell and refusing to let a grant say
so leaves that reach outside the record entirely, where no reviewer sees it and no verdict attests it — the
worse of the two failures. It carries the standing consequence that it is the capability a reviewer should
always find someone's argument behind, and its `param_constraints` are recorded and unenforced until
something mediates the shell.

**What the parity test compares, now that it has two sides.** Both sides normalize to the same set of
`tool:<surface>:<operation>` strings, and the comparison is set equality over the normalized forms. The
grant side reads the parsed tool capabilities directly and treats **an empty set as a failure, never as
permission** — a checker that fails open when no grant declares a tool would score every unmigrated
principal as conforming, which is invariant 4's decoration. The harness side reads **the arguments actually
built for the child process**, not the field a definition declares, because the two disagree structurally:
a declared wildcard passes no flag at all, and an unconditionally appended server surface is in the
arguments and never in the field. A divergence in **either** direction is a failure — wider than the grant
is unauthorized reach, and narrower is a grant that does not describe what ran. A definition that failed to
load is `unknown` with its error, never a wildcard grant, because a failed read never synthesizes a value
more permissive than success would have returned (invariant 5); and a harness that passes no allowlist at
all is `unknown` and reporting-only, with a run in which every principal is `unknown` failing rather than
passing empty.

**An unnamed tool is denied.** A capability the grant does not name is not held — the same default-deny
decision 41 ruled for entity types, and the same rule read at every enforcement point rather than from a
cache. This does not narrow what an operator may grant. **Breadth is written by enumeration, not by
absence**: a principal intended to hold everything its tooling can do holds a surface wildcard for each
surface it reaches, `tool:shell:*` included, and the grant then says so — dated, attributed, and readable
by the reviewer and by the parity test. What default-deny removes is not the wide grant but the *unwritten*
one: reach a principal has because nothing bounded it, which no governance write authorized and which no
verdict can attest. The operator's stated intent to grant any possible access is expressible in this
grammar in full; what is not expressible is holding that access without having granted it.

**What this left to decision 87, since ruled the same day.** This grammar makes the harness's reach
nameable; it did not rule that the harness may not exceed what is named. Decision 87 has since ruled all
three of the questions this ruling handed it (`#a-harness-provides-only-what-a-grant-names-and-a-provider-that-does-not-enforce-is-a-capability-the-grant-names`):
the harness configuration is closed by default and opened only by a grant; a harness that cannot bound its
non-enumerable reach may carry granted work, with its use itself a capability the grant names through
these reserved surfaces; and routing a principal to a provider that enforces nothing provides that
principal a capability, which the grant must therefore name, so the router escalates nothing and an
unnamed routing is `capability_denied`. It also leaves
open, as decision 42 permitted and this ruling does not close, whether the harness's list is eventually
**derived** from the grant at load — which removes the drift class — or only **held equal** by the parity
test, which is cheaper and leaves the copy in place. And it settles nothing about *which* tools any
principal should hold: the grammar states how a reach is written down, never which reach is appropriate,
and the authoring pass is separate work.

**What would reopen it.** A tool whose reach is neither a domain with an operation in it nor an argument
bound on one — a capability that a surface and an operation genuinely cannot name — would be evidence the
single grammar is too narrow, and would be argued here.

### A harness provides only what a grant names, and a provider that does not enforce is a capability the grant names

**Ruled (decision 87, 2026-09-08): a harness provides a principal only the capabilities its grant names.
The configuration is closed by default and a grant is the only thing that opens anything. A reach that no
enforcement point can bound is admitted only where a grant names it — `tool:shell:*` and its kin are held,
never inherited — and a provider that enforces no bound is itself such a reach: routing a principal to one
requires a capability naming it, and routing to one the grant does not name is a denial before the effect,
not a quieter execution.** Registered in `conformance.md#the-register-of-open-design-decisions`. Decision
86 ruled the grammar and left this rule; the three questions it named are answered in the three
subsections below.

**The rules in this section.**

- A harness provides only the capabilities the principal's grant names; the configuration is closed by default.
- A reach whose bound no enforcement point can read is held by a grant that names it, never inherited from where a process runs.
- Routing a principal to a provider that enforces no bound is a capability the grant names, and its absence is `capability_denied`.

#### Harness configuration is closed by default, and a grant is the only thing that opens it

The corpus already points three ways at this and stops short of stating it. Decision 41 ruled write
admission per entity type default-deny with the grant as the allowlist, and its argument is not about
entity types in the part that binds here: the asymmetry it turns on is that "a denied write costs a grant"
while an admitted wrong one costs a recovery, and "the safe direction to be unmeasured in is the closed
one". Nothing in that sentence is a property of entity types. Principle 5 gives the direction for any field
carrying a safety meaning, and a harness's capability surface carries the whole of it. Principle 7 makes an
unreadable answer `Indeterminate`, which at a policy enforcement point resolves to deny. Decision 42's
parity test is the measurement, and principle 1 is why the measurement is not the rule: a test that detects
a copy has diverged names no thing that fails when the divergence is the harness being wider, so parity
alone is reporting. This ruling supplies what the test was measuring against.

**Default-deny over a surface, not over a list.** The rule is stated over the *surface* and not over the
enumerable part of it, because a rule scoped to what can be listed is the wildcard shape decision 41
rejected, one level up: a statement that appears to bound and does not. What a harness starts with is
nothing. Every capability it puts in a principal's hands — an MCP server's tools, its own `Read` and
`Write`, a shell — is present because a capability on that principal's grant names it in decision 86's
grammar, and absent otherwise. An unconditionally appended server surface with no grant behind it is now a
violation of a stated rule rather than an unmeasured gap, which is the whole difference this row was opened
to settle.

**What this does not require.** It does not require that a harness be *able* to enforce every bound it
carries. Decision 86 already ruled that a capability whose bound no enforcement point can read is recorded
and reporting-only, and named that state rather than concealing it. The rule here is about what a
principal *holds*, not about what mediates the holding: a grant that names `tool:shell:*` has authorized
the shell, and the absence of a mediator makes its `param_constraints` unenforced without making the
capability unheld. The two failures are distinct and the design keeps them distinct — unenforced is a
recorded weakness, unwritten is an unauthorized reach.

**What the operator's stated intent costs under this rule, which is nothing.** He intends to grant
everything the tooling can do, widened to any possible access. Decision 86 kept that expressible by
enumeration: a principal meant to hold everything holds a surface wildcard for each surface it reaches,
`tool:shell:*` included. This ruling does not narrow that by a single capability. It removes exactly one
thing, and it is not the wide grant: it removes the *unwritten* one. A principal that holds every surface
wildcard under this rule holds precisely what it held before, with the difference that the holding is
dated, attributed, readable by a reviewer, and attestable by the parity test. A rule forbidding a harness
to exceed its grant and a grant naming everything are not in tension, because the rule constrains the
relation between the two and never the breadth of either.

**Cost accepted.** Every harness configuration in the instance becomes non-conforming on the day this
lands, and no grant in the instance names a tool at all, so the conforming set is empty. That is the
measurement decision 86 cites, and it is the expected consequence of ruling a vacancy into a violation
rather than an argument against doing so: the alternative reading — that the gap is an implementation
failure against a rule nobody wrote — is the status quo this row exists to test, and it leaves the closed
default unstated, which means the next harness added starts open again. Migration is `migration.md`'s, and
a stated rule with a migration behind it is the shape this corpus uses everywhere else.

#### A harness whose reach cannot be bounded may carry granted work, and its use is a capability

This is the harder half, and it is the case decision 86 deliberately created. 86 admitted `tool:shell:*`
knowing its enumerability bound cannot be met, on the ground that refusing it "leaves that reach outside
the record entirely, where no reviewer sees it and no verdict attests it — the worse of the two failures."
The question here is whether that reasoning scales from one capability to a whole harness.

**The case for refusing it.** A harness whose reach cannot be enumerated cannot be shown non-exceeding.
Admitting it makes the rule above hold over the part that happens to be listable and lets everything else
through, which is exactly the appearing-to-bound defect the rule was written over. Read that way, the
non-exceedance rule is violated by construction the moment such a harness carries granted work, and the
consistent conclusion is that it does not carry it — the reach a process has by ambient configuration is a
reach nobody granted, and no amount of recording makes it granted.

**The case for admitting it, which is what governs.** The argument above proves too much, and its
over-reach is visible in what it would forbid. Every harness the swarm actually runs reaches a filesystem
by where its process runs and a shell by having one; a rule refusing non-enumerable harnesses refuses all
of them, and a design rule that no conforming implementation can satisfy is not a bound but a prohibition
on the activity. What would follow is not stricter harnesses but the same harnesses run outside the rule's
scope — the reach relocated rather than removed, which is precisely the failure 86 named one capability
down. The asymmetry 86 identified holds at this scale for the same reason it held at that one: a bounded-in-name-only
harness inside the record is reviewable, attributable, and revocable, and an unrecorded one is none of the
three.

**So the ruling takes 86's disposition and completes it, rather than choosing between the two.** A harness
whose non-enumerable reach a grant names may carry granted work; one whose reach no grant names may not.
The use of such a harness is **itself a capability the grant names**, in decision 86's grammar and with no
new entry form — the reserved surfaces are what name it, and holding `tool:shell:*` *is* the statement that
this principal may run somewhere with a shell. That collapses the two halves of the disposition list into
one rule: refusing the non-enumerable harness and naming its use as a capability are the same act, because
the capability is what does the refusing. A principal whose grant names no shell surface does not run in a
harness that provides one, and the check that fails is decision 42's parity test reading the harness's
built arguments — the same mechanism, over the same normalized set, with no second control (principle 6).

**What this costs, stated.** The bound on such a capability stays unenforced until something mediates the
shell, and this ruling does not pretend otherwise. It carries 86's standing consequence forward: the
surface wildcard is the capability a reviewer should always find someone's argument behind. What changes is
that the argument now has to exist, because the capability now has to be written.

#### Routing to a provider that does not enforce widens reach, and the router is not where it is refused

The condition is real and measured. `status.md` records that a provider chosen by capacity when a runner
starts gives the same grant different reach on different days, and it records the enforcement points that
map `Indeterminate` to `Permit`: `a2a_gateway.authorize_caller()` returning `(True,
"grant_check_unavailable_advisory")` when the grant checker raises, and the tool proxy's `enforce()`
returning permit when no identity is configured. A router that can move a principal onto such a path has
widened that principal's effective reach with no governance write anywhere in the sequence.

**It widens a principal's reach with no governance write, and naming it that is the point.** The
alternative reading — that reach is whatever the chosen provider happens to enforce — makes a routing
decision into an authority decision made by a
component with no authority to make one, which is the two-homes defect invariant 9 forbids and the
report-not-a-control defect principle 1 forbids, at once. The reach a principal has must be answerable from
its grant; if it is answerable only from which provider had capacity, the grant is not the statement of
what a principal may do.

**But it is not a new mechanism, and the router is not where it is caught.** Three candidate treatments
were weighed. Treating it as something the parity test already catches is false as stated and true after a
correction: the test compares the grant's capabilities against the harness's built arguments, so a
*non-enforcing provider* — one that receives the arguments and ignores them — passes a comparison of two
lists that agree. What the test catches is a divergence of statements, not a failure to honour one.
Treating it as `capability_unavailable` is the closest fit and is still wrong: decision 60 gave that class
to a capability that exists and cannot be had right now, and asks the operator whether to wait, switch
vendor, or accept the loss. A provider that will not enforce is not a capability temporarily missing; it is
a bound the design requires and this path cannot supply, and asking whether to wait misdescribes it.

**The ruling.** A provider's enforcement of the bounds a grant states is a property of the provider, and
where it is absent, routing a principal there **provides that principal a capability** — the capability of
executing unbounded — which under the rule above the grant must name. So the router widens nothing on its
own account, because there is nothing for it to widen *to* that a grant has not already opened: a
principal whose grant names the non-enforcing path may be routed there and the reach is on the record; one whose
grant does not may not, and the attempted routing is a denial before the effect, `capability_denied` on the
task, the same class `#grants` already gives a refused capability. The widening is real and the router
is not the place it is caught, because catching it there would put the authority question in the routing
component, which is what made the widening a governance failure in the first place.

**What follows for the fail-open paths, and what does not.** The five paths `status.md` records are each a
degraded read synthesizing a permit, and each is already forbidden by the rule three sections above this
one — a degraded read never synthesizes a value more permissive than success would have returned — read
together with principle 7. This ruling adds nothing to their disposition; it adds the reason a *correctly
functioning* non-enforcing provider is also a governance question and not merely a capability gap. The two
are distinguishable and the design distinguishes them: a checker that raises is `Indeterminate` and denies;
a provider that enforces nothing by design is a capability, granted or not.

**What this ruling does not settle.** It states what a principal may hold and what a harness may provide,
and no mechanism (invariant 12). Whether the harness's list is **derived** from the grant at load or only
**held equal** by the parity test is decision 42's open half and stays open — this rule binds under either,
since it constrains what the two may contain and not how the second comes to contain it. Which tools any
principal should actually hold is the authoring pass decision 86 named as separate work and is untouched.
How a provider declares whether it enforces, and where that declaration sits, is a mechanism this ruling
requires and does not choose; a `vendor_binding` capability slot on decision 42's pattern is the shape
decision 69 used for the parallel case, and naming it here would be the mechanism this ruling is barred
from picking. The migration from an instance where the conforming set is empty is `migration.md`'s.

**What would reopen it.** A harness that can be shown to bound its reach fully — a sandbox whose filesystem
and process reach are enumerable at check time — would remove the concession in the second subsection and
argue for the stricter reading it declines. So would evidence in the other direction: an instance where
every principal's grant names every surface wildcard on its first day, which would make the enumeration
ceremony rather than authorization and argue that the rule buys nothing the parity test did not.

### Where a harness reaches the record, and what admits the request

**Ruled (decision 97, 2026-09-08): the enforcement point for an agent's read and write to the record is
the record's own admission check against the requesting principal's grant. A proxy in front of the record
is permitted and is never the enforcement point.** Registered as ruled in
`conformance.md#the-register-of-open-design-decisions`. Decision 95 had ruled the same day that a harness
reaches the record only through the swarm's own proxy, on the ground that a proxy is the only place in the
path where a request can be refused; that ground is false, and it contradicts decision 56, ruled two days
earlier. What follows states the ruling's grounds, then the contradiction it resolves and what survives it.

**Why the record.** Three grounds, in the order they bind.

*Decision 56 already ruled this.* Its question was where the enforcement point sits that ties an admitted
governance write to a permitted action, and it named the same three candidates this row names: the record's
admission check reading the action, a proxy in front of the record, or a sole-writer grant. It ruled the
third and located every other write's refusal at the record — "every other principal's write to a
governance type is refused at the record's admission under decision 41". Its reasoning rejected the proxy
by name and on principle 6: a proxy in front of the record is a second gate on the write path to the
swarm's own record, where the grant is already read at the write by the record itself
(`gates_and_workflows.md#where-the-enforcement-point-for-a-governance-write-sits`). Decision 95 placed the
enforcement point at a proxy without citing 56, and so restated a rejected candidate as the only one; a
ruling that did the same here would leave two rulings two days apart disagreeing about the same point in
the same path.

*The record-side check needs no component alive for a refusal.* A check in a proxy is a check that is
absent whenever the proxy is — down, restarting, or bypassed by a harness that connects to the instance
directly — and a request that reaches the instance around it meets nothing. The record's check holds
however a harness connects, which is what principle 1 asks of a mechanism before it counts as a control:
a refusal that depends on a component's liveness is a refusal that a failure removes. This is also the
weaker property the design already relies on everywhere else, since decision 41's allowlist is "read at
every enforcement point" and decision 94 puts the read check at the read.

*A proxy is a single point of compromise for every operator's data.* Every request and every credential
passes one hop, so its compromise reaches everything the swarm can read and write, and its credential is
the one whose theft is not scoped to a principal. That is the hazard decision 96 names, and it is a reason
not to make the proxy the thing the boundary rests on, whatever else the proxy is for.

**The "no enforcement point" premise does not hold.** Decision 95 argued that under per-harness credentials
decisions 41 and 94 are "unenforceable by construction… because the request reaches the record without
passing anything that reads a grant". A record that reads the requesting principal's grant at the write
passes exactly that, which is the topology decision 56 ruled and decision 41 assumes when it says the
allowlist is "read at every enforcement point". The hazard 95 reached for besides — a bearer credential
whose blast radius is the whole grant — is real and is answered under *Cost accepted* below, but it is an
argument about custody, not about the existence of a check, and 95 spent it to reach a conclusion only the
first argument supports. What the substrate does today is recorded in `status.md`, and is evidence about
what is built, never a ground for the design.

**What a proxy is still for.** Permitting a proxy is not damning it with the word convenience: it holds
real value, and the value is real precisely because none of it is enforcement. It is one place to rotate a
credential rather than one per harness. It is one place to resolve which instance of the record a request
reaches, which is the routing need decision 91's several-instance case creates. And it is one place from which requests are
observable. A deployment that wants any of these puts a proxy in the path and loses nothing, because the
record refuses what it would have refused anyway; a deployment that does not is no less bound.

**What survives of decision 95.** Nothing in decision 94 depended on it: read admission per entity type is
default-deny against the reading principal's grant wherever the check runs. Its three consequences survive
as conditionals on a proxy existing — if a proxy is in the path, then an unresolved caller identity is
refused there rather than passed through (principle 5), the read boundary is checked there, and
multi-instance routing resolves there from the deployment's binding. Each is a correct statement *about* a
proxy and none is an argument *for* having one, which is the distinction 95 did not draw.

**Cost accepted.** A bearer credential held by a harness is a real hazard: its blast radius is the whole
grant, and a stolen one is a principal. This ruling does not reduce that; it declines to answer it with a
second gate, and answers it where the design already answers custody — grants kept narrow and revocable
under `#grants`, and attribution on every write. A deployment that wants the credential held in one place
may put a proxy there, and decision 96 — ruled below — is what that deployment then answers: the proxy
passes the agent's own credential through and holds none of its own, so one place to rotate is the one thing
it forfeits.

**What would reopen it.** A record whose admission check cannot read the requesting principal's grant —
which would first be a gap in the record, not a reason for a proxy — or a deployment in which a harness
cannot hold a credential at all. How a proxy authenticates on an agent's behalf is decision 96, ruled
immediately below on this ruling's own consequences.

### A proxy passes through the agent's own credential, and holds none of its own

**Ruled (decision 96, 2026-09-08): a proxy in the path authenticates to the instance by passing through
the agent's own credential. It holds no credential of its own and acts for no agent, so the instance
matches grants on the `sub` actually presented and attribution is the agent's directly.** Registered as
ruled in `conformance.md#the-register-of-open-design-decisions`. The rejected alternative was a proxy
holding its own credential and acting for the agent, with the A-for-B attribution `#attribution` and
`#delegation` already require carried in the write.

**Why pass-through.** Three grounds, in the order they bind.

*It is what makes the record's own check run on the principal the check is for.* Decision 97 ruled the
enforcement point at the record and never at the proxy: the record admits or refuses a request by matching
the requesting principal's grant. A pass-through presents the agent's `sub`, so the grant the record reads
is the agent's, which is the grant the admission decision is about. A proxy-held credential presents the
proxy's, so the record would match the proxy's grant against a request made for an agent — which either
defeats the check, if the proxy's grant is wide enough to cover every agent it fronts, or forces a second
identity resolution inside the request so that the record can recover the principal it was supposed to have
been given. The first is the enforcement point failing open at the moment it is reached; the second is the
proxy becoming load-bearing for admission, which is exactly what 97 ruled it is not.

*One source, defined once.* Invariant 9 gives a value the swarm reads one home. Under pass-through, the
answer to which principal made a request is the credential presented, and there is nowhere else to look.
Under a proxy credential, the answer is in two places — the credential the record authenticates, and the
A-for-B attribution the write carries — and the two can disagree. A disagreement there is not a data defect
that a later read repairs; it is the record having admitted a request as one principal and recorded it as
another, with nothing in the path obliged to notice.

*It keeps every credential scoped to one principal.* Decision 97 named the proxy as "a single point of
compromise for every operator's data", whose "credential is the one whose theft is not scoped to a
principal", and declined to rest the boundary on it. A proxy holding its own credential is that hazard
realized rather than merely present: the credential exists, it is the one every request travels under, and
its blast radius is the union of everything the proxy fronts. Pass-through leaves the blast radius of any
one stolen credential exactly one principal's grant, which is the bound `#grants` already states.

**Cost accepted.** Decision 97 lists as a real benefit of a proxy that it is "one place to rotate a
credential rather than one per harness". Pass-through forfeits that benefit and no other: credentials are
issued and rotated per principal, so a rotation is per principal too, and a deployment fronting many agents
rotates many. This is accepted rather than answered, because the alternative buys the convenience by making
one credential authoritative for many principals, which is the property the third ground refuses. The two
other benefits 97 names survive untouched — a proxy is still one place to resolve which instance a request
reaches (decision 91's routing need) and still one place from which requests are observable, and neither
requires the proxy to hold a credential.

**What would reopen it.** A deployment topology in which a harness genuinely cannot hold its own credential
— not one where holding it is inconvenient, but one where the credential cannot reach the harness at all —
since that would make pass-through unimplementable rather than merely costly, and a rule nothing can satisfy
relocates the reach rather than bounding it.

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

**The rules in this section.**

- A required approver is a principal, a role the roster resolves, or the principal an `ownership_grant` names on an entity the subject concerns.
- A resolution on an `operator_only` action is the operator's decision, never the confirmation that the effect happened.
- [The raiser of a checkpoint does not resolve it, and the operator's self-resolution is marked](#the-raiser-of-a-checkpoint-does-not-resolve-it-and-the-operators-self-resolution-is-marked).

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
reconciled against `RAISED_BY` and `RESOLVED_BY` by the refusal, in the shape decision 32 gave the `conclusion`:
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
a field of it, the name deferred to a vocabulary pass under invariant 12
(`adapters.md#whether-one-binding-type-or-two-names-an-external-systems-instance`).

## Structural checks: quorum and separation of duties

**The rules in this section.**

- [The counting rule: an agent counts as its bound principal](#the-counting-rule-an-agent-counts-as-its-bound-principal).
- [Structural checks are reads over the checkpoint's principal edges](#structural-checks-are-reads-over-the-checkpoints-principal-edges).
- [The threshold's home is the `action_policy`, per class](#the-thresholds-home-is-the-action_policy-per-class).

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
unmeasured instance should be in; and every value is a governance write to the `action_policy` with an author
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

**The rules in this section.**

- [Initiative approval is the checkpoint](#initiative-approval-is-the-checkpoint).
- [What stops is a task, the owner seat confirms it through the checkpoint, and proposing is a grant capability](#what-stops-is-a-task-the-owner-seat-confirms-it-through-the-checkpoint-and-proposing-is-a-grant-capability).
- [Budget is a scope term that attenuates](#budget-is-a-scope-term-that-attenuates).
- [Credit is a read model over attribution](#credit-is-a-read-model-over-attribution).

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
resolution confirms is a decision; the stop is then made and read back (principle 2) — the closing verdict
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
verdicts, actions, and observations with the principals they carry — and is never stored.** Registered as
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
