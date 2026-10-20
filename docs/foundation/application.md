# An application on an instance of the record: what it is, where its parts live, and how it is composed per instance

**Kind:** foundation; an authored companion that states the design and never the state of a checkout. Not
keyed and not inlined into a review prompt (`conformance.md#scope`). **Derived from:** the operator's
rulings of 2026-10-18, 2026-10-19, and 2026-10-20 on the application question and its sub-questions (decisions 145 to 157 in
`conformance.md#the-register-of-open-design-decisions`), the 2026-07-09 placement rule for the four layers
(substrate in the record's public repository, generic use-case skills in the record's bundles, swarm
operations in this repository, proprietary vertical logic in a company's own repository), decision 15
(adapter packaging), decisions 76 and 82 (several records; several operators per instance), decision 97
(enforcement at the record), decision 37 (the console is the read of the record), and the principle that
operator specifics come from context entities (the fork test, applied here to applications). Nothing in
this document is built; what a checkout implements is `status.md`.
Amendment history: `revisions.md#applicationmd`.

## Purpose

Name the application itself. Two staff-facing applications have been built by hand on client instances of
the record: a customer-service application on a client's own instance, and an operations dashboard over a
shared client instance. Both have the same five parts, and the foundation already designed four of them
(adapters, workflows with the action gate, instance settings, and the record's own types) without ever
saying what an application is, where its parts live, or how one is customised for an instance. Without
that statement each application is a new codebase and every fix lands once per client. This document is
that statement. It decides placement and composition; the mechanisms it relies on are each stated in the
document that owns them, and this document points there rather than restating them.

## Scope

In scope: what an application is, where each of its parts lives, how per-instance customisation composes,
and what an application never holds. Out of scope: the screens themselves, which are the product
specification of the console (`docs/product/console.md`, a product document that decides nothing about the
design); the record's bundle format, which is the record's own foundation to state; and each part's own
rules, which are `adapters.md`, `workflows.md`, `gates_and_workflows.md`, and `authority_model.md`.

## What an application is

**The rules in this section.**

- An application is the console, deployed once per instance and composed from that instance's entities.
- An application holds no state that is a source of truth.
- An application is not a principal, an adapter, or a second gate.

**An application is a staff-facing surface over one instance of the record: the console, deployed against
that instance, composed at runtime from entities on it.** It is not a second program beside the console.
The operator ruled on 2026-10-18 that the application is the existing console
(`docs/product/console.md`): one codebase, deployed once per instance against that instance's record, so
the operator's own instance, a client's customer-service instance, a client's operations dashboard on a
shared instance, and an operated-service company's own instance all run the same program. What differs per
instance is composition, read from entities on that instance: which modules show, which labels and
language and brand apply, which bundles' screens appear, and who may see and write what. Code changes only
for a genuinely new kind of screen element. The two hand-built applications contribute patterns to the
console and retire their own screens; they are not a seed codebase.

**The composition has four sources, and nothing else.** An application's screens and record types come from
the record's bundles; its workflows, action gate, checkpoints, and adapters come from this foundation; its
settings are entities on the instance; and the bootstrap needed to reach the record at all lives outside
the record (below). Each source is named in the next section, and a part with no source is a part the
design does not have.

**An application holds no state that is a source of truth.** It inherits decision 37: a dashboard is the
read of the record rendered for a principal and nothing more, so a copy the application keeps beside the
record is stale in the direction that matters (principle 11). The sharpest case is approval, and the rule
for it is stated where approval is, in
`authority_model.md#an-application-resolves-checkpoints-and-holds-no-approval-state-of-its-own`.

**An application is not a principal, not an adapter, and not a second gate.** It carries whichever
principal is signed in and is subject to that principal's grants (decision 97); it crosses no boundary,
because the record is on the inside of the one boundary the design has; and it never takes an outward
effect outside the action gate. A write a person makes through it is a write under that person's grant,
attributed to that person.

## Where each part lives

**Ruled (decision 145, 2026-10-19, the operator's): split.** Registered as ruled in
`conformance.md#the-register-of-open-design-decisions`. Record types, their skills, and their declarative
screen definitions ship in the record's bundles; workflows, the action gate, checkpoints, and adapters stay
in this foundation; per-instance settings are entities on the instance.

| Part | Lives in | Why there |
|---|---|---|
| Business record types, their skills, and their declarative screen definitions (**views**) | the record's **bundles** | A reseller that runs no swarm still gets the screens, and a screen keeps working when the swarm does not. The record's own foundation designs the bundle; this one states what it requires of it (below) |
| Workflows, the action gate, checkpoints, the lint that precedes a consent | this foundation (`workflows.md`, `gates_and_workflows.md`) | The approval gate stays single. Approval and workflow concepts in the record would fail its own upstream test (would every adopter want this?) and would put a second gate beside the action gate |
| Adapters to external systems | this foundation (`adapters.md`) | Decision 15: bundled in this repository until a second consumer exists |
| Per-instance settings: bindings, deployment configuration, locale, brand voice, policies, display labels, module layout | entities on the instance | The fork test: if another operator would have to edit code rather than supply an entity, the specifics are in the wrong place |
| The bootstrap needed to reach the record: its address, the sign-in client registration, and secrets | outside the record | A console must reach the record before it can read anything from it. `deployment_configuration` on the record still names the bootstrap for deploys |
| A genuinely new kind of screen element | the console's code | Everything else is composition |

**A bundle is the record's deliverable unit: the types, the record-type documentation, the skills, and the
declarative views an instance enables.** The word is the record's, and this corpus uses it only in that
sense. (Harness delivery has its own sense in `harness_carriers.md`, and it is a different thing; a batch is
never a bundle, `vocabulary.md#batch`.) A **view** is a declarative description of how a record type is
shown: which fields, in what order, under what default labels, with what edit affordances. It is data, not
code.

**What this design requires of the record's bundles, and does not design.** The bundle format belongs to the
record, on the same allocation `multi_tenant.md#what-this-design-requires-of-the-records-sharing-model-and-of-an-operators-reach-within-it`
makes for visibility: this corpus states what it requires of the record, and the record's foundation commits
to providing it. Four requirements:

1. A bundle ships types, skills, and views, and **no approval state and no workflow**. A bundle that carried
   either would be a second gate (decision 145 rejects it).
2. A bundle's views are **declarative**: they name types, fields, defaults, and edit affordances, and the
   console renders them. A view that ran code would move the application back into per-client programs.
3. Enabling a bundle is an act on the instance's own record. A swarm's absence does not stop a screen from
   rendering, and a bundle's absence does not remove workflows or the gate.
4. Enabling or disabling a bundle is one write in an installation's series, admitted at the record under the
   installer's grant and gated like any other write; it has no class of its own (decision 155,
   `#installing-and-upgrading-an-application`).

**The two rejected placements.** All in the record's bundles would make an application independent of this
foundation, but the record would gain approval and workflow concepts and a second gate would appear beside
the action gate. All in this foundation would give one authority, but every client application would depend
on the swarm even to show a record. **Cost accepted:** the application's behaviour is composed from two
repositories' releases, so a view and the workflow that writes the record it shows can be at different
versions on one instance; which pairs are compatible is conformance work, not a rule here. **What would reopen it:** a bundle concept that carries approval and is shown to
pass the record's upstream test, which would first be a change to the record.

## How per-instance customisation composes

**The rules in this section.**

- Every per-instance value is read from an entity on the instance, never from code.
- Display labels default in the bundle's views and are overridden per instance by a display profile.
- Each console module's layout is an entity on the instance, with defaults shipped in bundles.
- Customer-facing language is decided by one function per instance, distinct from staff display language.

**The order of resolution is fixed: the instance's entity, then the bundle's default, then nothing.** A value
the instance supplies wins; a value it does not supply falls back to the bundle's default, so every instance
gets sensible screens for free and a client's own words are one entity edit. Where neither exists the
application shows the generic type or field name and says so; it never substitutes an invented default. For a
value that carries a safety meaning (a policy, a grant, an approver), absence is not a display fallback: the
read fails closed and the work is put to the operator (`failure_posture.md#the-rules`).

### Display labels

**Ruled (decision 149, 2026-10-19, the operator's): display labels default in the bundle's declarative views
and are overridden per instance by a profile entity, the display profile.** The record keeps the generic type
and field names. A label is presentation, resolved per instance and per locale, and a type is never renamed
to change how it reads: a generic customer-matter type can read as the client's own word in the client's own
language while remaining one type in the record, found by its generic name. The display
profile is a context entity of the same standing as `locale_profile`, which carries jurisdiction, time zone,
currency, and language and is not extended with labels (the rejected alternative would make each instance
supply every label from scratch). Its type name is provisional: it is the record's to register with the
bundle that ships the default, and this document uses `display_profile` until it does.

**Customer-facing language is not display language.** What a draft addressed to a customer is written in is
decided by one deterministic function per instance, whose inputs come from the instance's context entities
(`locale_profile` and the customer's own latest message); it is distinct from the language staff see their
screens in. The two vary independently: a staff screen in one language may carry a reply in another. The
function's exact inputs and fallback are not designed here; its existence as one function, not as a rule
repeated in each draft step, is the requirement (principle 9).

**Cost accepted.** A label lives in two places in the sense that a default exists in a bundle and an override
on an instance; the resolution order above is what keeps that from being a second source of truth. **What
would reopen it:** a label the view cannot express (a plural form, a gendered form) that forces text into
code.

### Module layout

**Ruled (decision 154, 2026-10-19, the operator's): a `console_module` entity per module on the instance
names the module's blocks and the record types or metrics each reads, with defaults shipped in bundles.**
The alternative, a fixed module set in code toggled by a profile flag, is simpler and makes every new layout
a release. Under the ruling a new client's console is entity edits, and an instance can hide or reorder
modules without a deploy. A `console_module` is composition and never data of record: it holds no approval
state and no copy of what its blocks read. Which modules and blocks exist by default, and the bundle-views
mechanism that ships them, are the record's and the console's to design; `docs/product/console.md` is the
specification those designs land in, and its admission of domain screens, writes by an instance's own
operators to business records, charts, and per-instance composition is product work the ruling calls for and
this document does not perform.

**Writes by an instance's own staff to business records are internal operational writes, not actions.** A
client's staff edit their own master data (invoices, company records, policy text, price lists) through the
application. Each is a write to the record under that person's grant, attributed to that person, bounded by
the grant, and made with a read-before-write concurrency check. It is not an action and needs no gate,
because it crosses no boundary (`gates_and_workflows.md#actions-are-entities-only-actions-are-taken`). An
outward effect that the same edit would cause, such as a message to a customer, is an action and goes through
the gate as always.

## Where the rest of the design lives

Each of the following is stated once, in the document that owns its mechanism, and cited from here.

| Question | Ruled | Home |
|---|---|---|
| A client's staff member is an operator of the instance, scoped by grants; no new principal kind | decision 146 | `authority_model.md#a-clients-staff-member-is-an-operator-of-the-instance-scoped-by-grants` |
| An application resolves checkpoints and holds no approval state; an edit becomes a new draft and the checkpoint is resolved on that exact draft | decision 147 | `authority_model.md#an-application-resolves-checkpoints-and-holds-no-approval-state-of-its-own` |
| A client's delivery depends on a minimal runtime of its own and never on swarm coordination health | decision 148 | `adapters.md#a-client-instance-runs-a-minimal-runtime-of-its-own-and-its-delivery-never-depends-on-swarm-coordination` |
| An environment is a separate instance of the record; promotion replays the same governed writes in order | decision 150 | `adapters.md#an-environment-is-a-separate-instance-of-the-record-and-promotion-replays-the-same-governed-writes-in-order` |
| Direction of truth for a business record class lives on its source's `vendor_binding`, with a one-way cutover event | decision 151 | `adapters.md#the-direction-of-truth-for-a-business-record-class-is-a-field-of-its-sources-binding-and-moving-it-is-a-one-way-event` |
| A business matter is a record that tasks refer to, not itself a task | decision 152 | `work_model.md#a-business-matter-is-a-record-that-tasks-refer-to-and-is-not-itself-a-task` |
| `customer_reply` is a reference workflow | decision 153 | `workflows.md#customer_reply` |
| A business matter's open or closed state is derived from its tasks; a state is stored only where no task stands behind it | decision 156 | `work_model.md#a-business-matter-is-a-record-that-tasks-refer-to-and-is-not-itself-a-task` |
| A held write is applied at a cutover only if the row it was made against is unchanged, otherwise returned to its writer | decision 157 | `adapters.md#the-direction-of-truth-for-a-business-record-class-is-a-field-of-its-sources-binding-and-moving-it-is-a-one-way-event` |

## Installing and upgrading an application

**Ruled (decision 155, 2026-10-20, the operator's): installation is a series of ordinary governance writes,
each gated on its own, and an upgrade is the replay of the same set per instance.** Registered as ruled in
`conformance.md#the-register-of-open-design-decisions`.

**The rules in this section.**

- Installing an application on an instance is a series of ordinary governance writes, each gated on its own.
- No commissioned install unit and no install-specific authority exist.
- An upgrade is the replay of the same set of writes to each instance in turn.

**Installation has no unit of its own.** Every part an application is composed of already has a governed way in:
a declaration is a governance write the action gate judges, a binding or a profile is a context entity written
under a grant, and enabling a bundle is a write on the instance's own record admitted at the record
(decisions 41 and 97). Installing is doing those writes, one after another, and each is gated as the write it is.
Nothing commissions the set as a whole, and nobody holds authority to install that they do not already hold to
make each write: who may start an installation is whoever holds the grants the writes need, attributed to them.
The alternative, a single commissioned unit in the shape of
`planning_model.md#commissioning-a-planning-record-drives-one-dependency-ready-frontier`, would approve the set
once and dependency-order its parts, at the price of a concept the gate does not need to judge a write.

**An upgrade is replay.** The set of writes that installed or changed an application on one instance reaches the
next instance as the same writes, in order, through that instance's own gate, exactly as a governed change is
promoted between environments
(`adapters.md#an-environment-is-a-separate-instance-of-the-record-and-promotion-replays-the-same-governed-writes-in-order`).
An instance at a different bundle version receives the set as written; a write that fails there stops the
replay at that write and raises its checkpoint on that instance, and the writes after it are not applied.

**Cost accepted.** A large installation is many approvals, each at its own gate. **What would reopen it:** an
installation whose number of writes makes per-write approval a burden, which would argue for the commissioned
unit.
