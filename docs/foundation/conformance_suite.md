# Conformance suite: the acceptance suite the foundation is judged by, from zero

**Keyed document:** read when this document, the suite that implements it, or the rule-coverage check
changes (`conformance.md`). **Kind:** foundation; designs a suite — what each test sets up, does, and
observes — and never states what a checkout passes; that is `status.md`. **Derived from:** every other
document in this directory, read as the source of the rules the matrix rows point at; `principles.md`
invariant 4 (the revert test) and invariant 3 (validate the instrument), which the suite applies to itself;
`adapters.md#admitting-a-new-adapter`, the model for the whole method — six obligations, each with the
artefact that fails when it is violated, and two candidates dropped for having none; `conformance.md`
(how design binds to work; the register of open decisions); `data_model.md#record-conventions` (tests never
register into the shared registry); `migration.md` (the bootstrap leg this document orders against, and
the gaps G1 to G27, which this document cross-references by number rather than re-finds); the operator's
2026-09-05 direction for this design: derive from first principles and from the documents, treat the code
as the thing the suite will judge and never as design guidance, and let a rule with no failing artefact
stand as a finding rather than be given a weak one; the rulings of 2026-09-05 (decisions 13–18 and 23–30),
against which every row that had been designed per candidate ruling was rewritten as a test of the rule;
and the simplification pass (revision 29), whose removals ran without this matrix and were marked unverified
against it, and which this document verifies in a section of its own; and the intake-rule revision and the
memo-gap pass (revisions 30 and 31), which added the sources index, the intake rule, and decisions 36 to 41,
and whose corrections close five of this document's findings and contradictions, marked closed where they
stand rather than removed; and the testability pass (revision 37), which carried the thirty open findings
and fifteen open contradictions below back into the documents that own them — each row marked with what its
home now says, or with why no wording could make it mechanical, and kept where it stands rather than
removed. The operator's standing instruction
that this document applies to itself: *the code is not established design guidance.* Revised by the rulings pass of 2026-09-06 (revision 38: decision 44 ruled — a `signed` or blocking sign-off requires a held lease by its signer; decision 43 ruled in its enumeration half — the bootstrap set is the thirteen-record table, every member read back; the rows that waited on decisions 31, 32, 35, 42, 43, 44, 48 to 51, 53, and 54 updated). Revised by the second rulings pass of 2026-09-06 (revision 39: decision 43 ruled in its second half — the operator's later governance write gated, self-resolved and marked, with 47; the rows that waited on 36, 43, 47, 50, 52, 53, and 56 turned mechanical; U-14 closed by 56).

## Purpose

State, for every rule the foundation carries, the test that goes red when the rule is violated: what must
exist in an empty record first, what the test does, and what it reads back. Two purposes, in order.

**Now: surface gaps and contradictions in the foundation.** A rule that cannot be given a failing artefact
is a rule that is not yet a control (principle 1), and writing the test for every rule is the fastest way
to find those. This document records each one as a finding — a rule untestable as written, with what the
rule would need to say to become testable — and never fills the cell with an observable that would not
actually go red. It also records every pair of rules whose observables contradict: a state one test
requires and another forbids. **The list of rules with no failing artefact is this document's most valuable
output**, and it is stated before the matrix that produced it.

**Later: be the acceptance suite a code branch is written against.** The suite runs from zero, against a
disposable record instance created empty for the run, and it is **all red against the current code by
construction** — the same posture `test_foundation.py`'s reading-budget test takes, marked as the
expected failure that records a decision the operator owns. The branch that turns it green is the
implementation of the foundation; a row that is green before that branch exists is a row that tests nothing
(principle 4).

## Scope

In scope: the conformance matrix over every rule in `docs/foundation/` — every invariant heading,
every rule stated in bold inside a section, every **Never** item in `vocabulary.md`, and every ruled
decision; the bootstrap sequence a from-zero run needs, derived where the documents settle it and
opened as a decision where they do not; the permutation axes, with the cross-products that are load-
bearing and the argument for dropping the rest; the isolation mechanism that makes it impossible,
and not merely unlikely, for the suite to touch the production record; the suite's own invariants;
and its relationship to the code it will judge. Out of scope: test code, which this document never
contains; provisioning of the disposable instance, which is an infrastructure item named here and
built elsewhere; the open decisions (31 to 36 and 42 to 56; 43 and 44 are argued below), whose rows are designed per candidate and held
pending, and the authority model's open questions, held the same way; and the condensation of this
document, which follows content as it has since revision 6.

## How the suite judges, and what a row is

### The method: every rule gets its failing artefact

`adapters.md#the-admission-contract` is the model. It takes five rules written as *what an adapter does*
and restates each as *what fails when it does not*, because conformance to a description is asserted and a
failure is checked. This document does that to the whole foundation. For every rule, one row states:

- **the rule**, as a link to the section that owns it — the matrix never restates a rule (principle 9), so a
  row's text is a pointer and its cells are the test;
- **the setup**, as the records that must exist in an otherwise empty instance before the action — every
  row starts from the bootstrap set below and names what it adds;
- **the action** the test takes;
- **the observable that goes red** when the rule is violated: a counter, an entity that must or must not
  exist, an edge, a state that must read `unknown`, a write that must be refused. Every observable is a
  read-back of the record or of an instrument the suite owns, never a return code (principle 2);
- **the class**: **M** for mechanical, a test asserts it; **R** for review-only, a person must check it;
  **U** for untestable as written, a finding; **P** for pending an open decision, designed per candidate
  ruling and held; **D** for definitional — a rule that states what a thing is rather than what happens,
  whose violation leaves no artefact by its nature, recorded as such so that the count of **U** is not
  inflated by rules no wording could make mechanical.

Review-only rows are a weaker class and are flagged as one. A rule whose only observable is a person
reading prose binds only when that person reads at the moment of the action (principle 1, the placement
test), and the suite cannot make a person read. The count of **R**, **U**, and **D** rows is a measure of how much
of the foundation is not yet a control, and it is reported in `status.md`, not here.

### Every mechanical row names its mutant

Principle 4 applied to the suite itself: a row is decoration until it has gone red. So every **M** row
names, in its observable or its note, the **mutant** — the shape of code that violates the rule — and the
suite carries, for each, a meta-test that introduces the mutant into a reference implementation and asserts
the row goes red. A row whose mutant cannot be named is not mechanical; it is moved to **R** or **U**. This
is mutation testing at the level of design rules rather than of statements (prior art, below), and it is
the only way the suite can claim that its greens mean anything.

### The suite validates its own instruments

Principle 3 applied to the suite. Every counter and every log the suite reads — the drop counter, the
lapse count, the record proxy's request log, a fake system's call log, the announcement channel's message
list — has a **planted-positive** test that proves it non-zero on a known case before any zero read from it
is believed. A suite whose instrument is broken reports green everywhere, which is the failure the
principle names, and the planted positives are what make an all-red suite distinguishable from a suite
whose observables are disconnected.

### What the suite reads, and what it never asserts on

The suite has exactly three instruments, and every observable in the matrix is a read of one of them:

| Instrument | What it is | What it gives the suite |
|---|---|---|
| **the record** | the disposable instance, read back after every write | every entity, edge, observation, and provenance the design names; `unknown` as a distinct value |
| **the record proxy** (`RP`) | a reverse proxy the suite places in front of the disposable instance | a log of every request with the presenting credential and its time; the ability to fail reads, fail writes, or hang reads while the health endpoint stays green |
| **the fake systems** (`X(sys)`) and **the fake channel** (`CH`) | one stand-in per external system the design names (code host, mail, chat, calendar, rail), and one for the off-record announcement path | a log of every call with the presenting credential and its time; programmable responses (success, lost, contradictory, a rate limit stating its reset); deliveries the suite composes, redelivers, reorders, or malforms |

The suite never asserts on a return code, a success flag, or a daemon's own report of what it did. It never
reads a daemon's local state as evidence of a design property; where the design forbids local state, the
suite's observable is that the state does not exist. And it never asserts on the absence of activity as
proof of a halt or a hold — an idle swarm and a halted one look the same by that measure
(`failure_posture.md#the-rules`, rule 2) — but on the presence of the announcement that the design
requires.

### What the rule-coverage check reads

`conformance.md#mechanical-checks-on-this-directory` names a rule-coverage check as a contract: a heading
with no row is the failure. For the check to be mechanical, what counts as a rule-bearing heading has to be
stated, and it is stated here once. A **rule-bearing heading** is every `###` heading in a kernel or keyed
document, and every `##` heading whose section states a rule in bold, **except** the classes that state no
rule of their own and are excluded by name: *Purpose*, *Scope*, *Prior art*, *Beyond the sources*, and
*Contradictions this document settles* (each names a rule stated elsewhere); *Freshness* in the adapter
documents (the date and instrument of an enumeration); *Drift* (state, which is `status.md`'s); *What this
document does not decide*; *What the API offers that this design does not use* and its siblings
(inventory — the refusals among their rows are tested under each document's refusal row); *Why this system
gets its own document*; and *What this document does not provision*. Vocabulary entries are covered by
class (VO-1 to VO-4) rather than by entry, because the checker's own parse of the document is the list, and
a term added there is tested without a row being added here. The check fails on a rule-bearing heading with
no row whose pointer resolves to it, and on a row whose pointer resolves to nothing, which the anchor check
already fails. Where a heading covers several rules, one row per rule points at the same heading, and the
row's second cell says which rule after the colon.
### Named fixtures

Every row's setup is written against these, so a row states only what it adds.

| Fixture | What exists |
|---|---|
| `B0` | the bootstrap set of *The bootstrap sequence* below: the registry, the operator principal and its credential binding, the policy, the roster, the first agents with grants and bindings, the intake declaration, the context entities intake reads |
| `B0+pol(...)` | `B0` with the project's `action_policy` set as stated: which classes are listed low, which high, which carry `operator_only`, and the declared lossy-mutation count |
| `T1` | `B0` plus one task created by the operator principal; its intake batch is open at `classify` |
| `T-routed(W)` | a task whose intake batch closed naming workflow `W`; a batch of `W` is open at its first step |
| `T-at(W, s)` | a batch of `W` with every step before `s` signed and `s` open |
| `X(sys)` | the fake external system `sys`, with four credentials bound in the record: `cred-owner` (binds to the step owner of the open step), `cred-op` (binds to the operator), `cred-other` (binds to a principal in neither role), `cred-none` (binds to no principal) |
| `RP`, `CH` | the record proxy and the fake announcement channel, both logging |
| `CLK` | every declared interval set short — lease duration, hold bound, unclaimed-step interval, deferral ceiling, lapse cap — so that rows about elapsed time complete in seconds. No clock is faked; the design derives lease state from `expires_at` against real time, and the suite waits |
| `LEG` | `B0` plus a legacy-shaped population written under the retired type names (`migration.md#the-mapping`): declarations keyed `gates[]` with agent names as step owners, held decisions with and without a subject, step records at terminal and non-terminal statuses, grants naming retired types, tasks carrying the retired liveness value — every row written by the suite, on the disposable instance, in the shapes `status.md` reports and never with any instance's contents. The fixture the `migration.md` rows run against; nothing in it is anyone's data |

## From zero: the disposable instance, and why it cannot be the production one

The suite runs against a **disposable record instance created empty per run and destroyed after it**.
Nothing in this design is tested against an instance that holds anyone's data, for two reasons that are
the same reason. The suite writes governance entities, registers types, raises and resolves checkpoints,
and exercises the halt, and every one of those on a shared instance is either a real change to a real
swarm or a test-shaped row that nothing distinguishes from a designed one
(`data_model.md#record-conventions`, the type-registration convention). And the suite's from-zero property
is the test: a suite that finds a `workflow` already declared cannot tell whether the bootstrap sequence
produced it.

**It must be mechanically impossible for the suite to touch the production instance — not by convention.**
Four layers, each of which fails closed on its own, so that a misconfiguration of any one is caught by the
others:

1. **A credential the production instance does not know.** The suite presents a credential minted for the
   run, whose issuer is a dev-only issuer, and whose `agent_grant` exists only on the disposable instance.
   The production instance holds no grant matching that `(sub, iss)`, so every request from the suite is
   denied there by the rule that already binds every principal: zero grants is deny
   (`authority_model.md#grants`). This layer is enforced by the production instance and not by the suite,
   which is what makes it the strongest: it holds however wrong the suite's configuration is.
2. **An instance assertion at startup, by positive identity.** Before its first write, the suite reads the
   instance's own statement of what it is and refuses to proceed unless that statement carries the
   **run nonce the suite minted when the instance was created**. The check is positive — the instance must
   prove it is this run's — and never negative, because a list of production hostnames to avoid is
   operator-identifying data that a public repository does not carry and that goes stale silently.
3. **A refusal on a non-empty store.** The suite's second read is the entity-type census. Anything beyond
   the registry's built-ins is a refusal: the suite does not run against a store that has been written to.
   A production instance is never empty, so this layer refuses it even if the first two were somehow
   satisfied — and it is the layer that also catches a disposable instance reused across runs.
4. **No production credential is materialized where the suite runs.** The custody rule
   (`authority_model.md#grants`) applied to the harness: the process environment the suite starts in
   carries the run-minted credential and no other record credential, checked by inspection before the
   first request. A credential the suite cannot read is one it cannot present by mistake.

Each layer has its planted positive: a run against a non-empty fixture instance must refuse at layer 3; a
run with a second credential in its environment must refuse at layer 4; a run whose nonce does not match
must refuse at layer 2. Those refusals are tested, because an isolation that has never refused anything is
decoration (principle 4).

**The instance must admit the design's edge types before the first bootstrap write, and that is a
dependency the suite shares with the migration.** Step 1 of the bootstrap registers relationship types the
record's exposed vocabulary does not hold and offers no primitive to add (`migration.md`, G25). A
disposable instance on which they cannot be written cannot complete step 1, and nothing below it can run:
the suite's first dependency is the record's project's, exactly as the migration's stage 1 is, and until
it is shipped the suite has one row that can go red — the census after step 1 — and no other. The suite
does not work around it. An edge type simulated as a field on an entity is the maintained state principle
11 forbids, and a row green against the simulation would be green against the wrong thing.
**What this document does not provision.** Creating an empty instance per run, minting its nonce and its
credential, placing the proxy in front of it, and destroying it afterward are infrastructure items. This
document states the requirement — empty, per run, nonce-identified, run-credentialed, destroyed — and
marks the provisioning as work for the branch that builds the suite.

## The bootstrap sequence: what must exist before a swarm exists

`work_model.md#changing-the-swarm-is-work-and-it-goes-through-a-workflow-like-any-other` settles that the
first workflow declaration is an operator act, out of band, and states bootstrapping as a limitation rather
than a mechanism. A from-zero suite cannot leave it there: it has to write the first records in some order,
and every order it could choose is a claim about the design. So this section derives the minimal record set
and its order from the documents, marks each step as **derived** (the documents settle it) or **open** (the
documents leave it unspecified, and choosing silently would be inventing design), and records the gaps.

### The circularities the documents leave

Three, and they decide what is derivable and what is not.

**Registry before principal, principal before registry.** A registered type carries at most one
`ownership_grant`, an edge to a principal, and type registration is "made by or on behalf of the type's
owner" (`data_model.md#record-conventions`). The `operator` type is itself registered. So the first
registration has no owner to be made on behalf of, and the first `operator` entity has no type to be an
instance of. The documents do not say which comes first or how the first ownership edge is written.

**Governance before the gate that governs it.** Every write to `agent`, `action_policy`, `agent_grant`,
`swarm_roster`, or the schema registry is an action evaluated at the action gate
(`gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken`). The gate needs an `action`
type, a `checkpoint` type, a policy to evaluate against, and a principal to resolve the hold. All four are
themselves governance writes. The documents settle only that *the first workflow declaration* is an
operator act; they do not say whether the registry, the first policy, the first roster, the first agent,
and the first grant are too.

**Attribution before the binding that attributes.** Every write carries the principal it acted for, and a
write whose only identity is the store's credential "resolves to no principal and is recorded as
unattributed" (`authority_model.md#principals`). The credential binding that would attribute the operator's
writes is itself a write, made before it exists. The documents do not say whether the bootstrap writes are
attributed retroactively, recorded as unattributed by design, or made by a provisioning credential the
design names.

### The minimal record set, in order

| # | Record | Why it is needed before the next | Status | What the suite reads back |
|---|---|---|---|---|
| 0 | an empty instance, nonce-identified, run-credentialed | the isolation layers above | infrastructure | the nonce; the empty census |
| 1 | the registry: every entity type and relationship type the concept tables name (`data_model.md#concepts`, `data_model.md#relationships`), each with a version and a `reducer_config` | nothing can be written until its type exists; a type registered later leaves earlier writes in `raw_fragments` forever (`data_model.md#record-conventions`) | **derived** — the first member of the closed set (decision 43, ruled in its enumeration half below): an operator act by membership, read back | every type present with its declared fields; a write of an undeclared field lands in `raw_fragments` |
| 2 | the `operator` entity: the human principal | every authority edge attaches here; nothing else can own, bind, or resolve until it exists (`authority_model.md#principals`) | **derived** as the second record, **open** as to attribution | the entity, identity only; `operator_profile` absent or separate, carrying no edge |
| 3 | the credential binding: the run credential → the `operator` | from here every write attributes; before it, every write is unattributed | **derived** that it comes third; **gap** that the binding has no type (below) | the binding resolves the credential to the operator; a write after it carries the principal |
| 4 | one `ownership_grant` per registered type → the operator | at most one per type, and a later version of the type is "made by or on behalf of the owner" | **derived** from step 1's convention, once a principal exists | exactly one edge per type |
| 5 | the project's `action_policy`: the low and high sets, the classes carrying `operator_only`, `confidence_threshold`, `recurrence_count`, `always_checkpoint_boundaries`, the lossy-mutation count | without it every class is unclassified and resolves to `NEVER`, which is the safe posture and also one in which nothing can be taken (`gates_and_workflows.md#confidence-and-three-blast-tiers`) | **derived** as a member of the closed set (decision 43): the first policy is the bootstrap write ruling 18's "by writing the policy themselves" describes; the operator's *later* writes to it are held and resolved by the operator as marked self-resolutions (43's second half, ruled with 47) | the policy, read back field by field; a governance write after it raises a checkpoint or is permitted, per its class |
| 6 | the first agents: at least the `pm` step owner's agent and the operator-facing agent, each with `principal_binding` → the operator | intake's every step is the `pm` step owner's; operator-only work and every checkpoint the operator is carried is the operator-facing agent's (`workflows.md#roles-named-in-this-document`) | **derived** as to which two, and as members of the closed set (decision 43); later writes to them are gated as step 5's are (decision 43, ruled) | each `agent` with `prompt_markdown`, `context_entity_types[]`, a version; the binding edge |
| 7 | one `agent_grant` per agent, matched on that agent's `(sub, iss)`, with an expiry | zero grants is deny; the agent cannot claim, read, or write until one matches | **derived** | permit on a declared capability; deny on an undeclared one; `Indeterminate` on an unreadable grant |
| 8 | the credential binding per agent: its `sub` → the `agent` | the AAuth `sub` binds to the agent and reaches the operator through the agent's `principal_binding` | **derived**; same type gap as step 3 | a write by the agent attributes A-for-B |
| 9 | the `swarm_roster` for the project: role → agent, by role | a workflow's `owner_role` is resolved against it at claim, and — by `failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol` — at declaration, so the roster must precede the first declaration | **derived** | every role the intake declaration names resolves |
| 10 | the context entities intake reads: `priority_rubric` (`prioritize` reads it by type) | a step whose declared read is unreadable does not open; a swarm with no `priority_rubric` cannot route any task | **derived** from the read declaration; **gap** that the entity has no row in `data_model.md` | the entity present; its absence makes `prioritize` hold (a row below) |
| 11 | the intake declaration: `workflow` for `(*, intake)` with its five steps, `owner_role` values, `reads_to_enter` and `reads_to_close`, `fast_paths` (`inherits`), `successors` | the first declaration, and the one every task needs; the design names it the operator act | **derived** as an operator act (`work_model.md`); the `*` project is a wording gap (below) | the declaration; a role it names resolves against step 9; a step reading a type not in step 10 raises `undeclared_dependency` at first use |
| 12 | the successor declarations the run exercises (`feature`, `bug`, `payment`, …), each per `(project, type)` | named in intake's `successors`, or the closing sign-off cannot select them | **derived** | each declaration; `successors` naming intake is refused as a declaration error |
| 13 | per adapter: the adapter `agent`, its grant and binding, the `channel_config` or `vendor_binding` context entity, the credential bindings of the actors it will meet (`cred-owner`, `cred-op`, `cred-other`), and the adapter's action classes listed in the policy | the six admission obligations (`adapters.md#the-admission-contract`); an adapter whose class is unlisted is inert, which is the right failure direction | **derived** from the admission section | the grant; the policy classes; a delivery from `cred-none` resolves to no principal |

Intake rules (`work_model.md#an-intake-rule-turns-a-described-change-in-the-record-into-a-task-and-nothing-else`)
are deliberately not in the set: no rule is needed for the first task, which the operator creates, and the
class covering rule writes is reserved to the operator under ruling 18 like every governance class, so the
first rules are the operator's later writes and take the same question as step 5.

The order has five hard edges the documents impose: types before instances (1 before everything);
the principal before any edge to it (2 before 3, 4, 6, 8); the roster before the first declaration (9
before 11), because the declaration-time role check needs something to resolve against; the read
declaration's entities before the first task (10 before the first `T1`), or the first task holds at
`prioritize`; and the policy before any governance write the run wants permitted rather than held (5
before 6, 9, 11, 13). Every other adjacency is a choice the suite may make and must state.

### What the documents leave unspecified here, and how each is recorded

**The bootstrap set was unbounded, and that was the finding; decision 43 closes it, in both halves.** The design named one out-of-band act — the
first declaration — and likened it to "issuing a credential or widening a grant". It did not enumerate the
set. Steps 1, 2, 5, 6, 7, 9, and 13 above are each a governance write, and each has to happen before the
gate can hold anything, so each is either an operator act or a write held at a gate against an absent
policy. A set that is not enumerated is a side door with no boundary: any later governance write can call
itself provisioning. What the design would need to say is a **closed list** of the records that constitute
bootstrap, the rule that every member is read back (principle 2), and the rule that a write to any of those
types *after* the set exists is an action like any other — including the operator's own. That last clause is
the half the design has not faced, and ruling 18 sharpens it by saying the operator grants a class "by
writing the policy themselves": once the gate exists, is the operator's write to `action_policy` gated
(held for the operator to approve, raising the raiser-resolves question of decision 47) or is "operator act"
a standing exemption? Opened as decision 43 below, and ruled there on 2026-09-06 in both halves: the closed
list is the table above, every member read back, and a write to any of those thirteen kinds of record after
the set exists is not provisioning; and the operator's own write after bootstrap is gated, its checkpoint
resolved by the operator and marked self-resolved (decision 47).

**The intake batch's opener is derivable, and the wording that hides it is a contradiction.**
`work_model.md#how-a-batch-is-formed-and-what-chooses-its-workflow` states that a batch comes into
existence when a closing sign-off names a successor "and at no other moment", then names another moment:
the intake batch "opened on the task's creation". The principles settle who opens it — "every arrow into a
batch is a principal's recorded verdict", and the daemon write contract says the tasks a poll produces are
"each entering intake" (`data_model.md#write-contract`) — so the creating principal's store of the task
**is** the act, and the intake batch and its `ADDRESSED_BY` edge are part of what creation writes. The suite
asserts the invariant (every task has exactly one intake batch, opened at creation) and the wording is
recorded under *Contradictions* below.

**The credential binding has no type.** `authority_model.md#principals` defines it — many-to-one,
credential → principal — and `data_model.md#concepts` lists it in the principal row's edges column as
"credentials → principal" with no relationship type name. Step 3 cannot be written without one. Recorded
as a data-model gap (`migration.md`, G17); the derivation is an edge type carrying the credential kind and value, the principal,
and an expiry, and it is proposed rather than assumed.

**A `project` has no home.** `workflow` and `action_policy` and `swarm_roster` are per project; a batch
carries `project`; a task's row in `data_model.md#concepts` carries no `project` and no rule says how a
task acquires one, so the intake batch opened at creation has a `project` from nowhere. The derivation
that fits the model is that `classify` writes it, as it writes `action_type`; recorded as a gap.

**The context entities have no rows.** `priority_rubric`, `release_criteria`, `brand_voice`,
`payment_profile`, `channel_config`, `vendor_binding` are read by declared steps
and none has a row in `data_model.md#concepts`, whose own rule is that "a concept with no row here is a
concept the design does not persist". A from-zero run cannot create what has no declared shape. Recorded
as a gap; the roster is the same gap under its own number (`migration.md`, G21), and the migration lists
the rest as **keep** because on a populated instance they already exist — from zero, they do not.

**Intake's `*` project.** `workflow` declares "one entity per (project, workflow type)"; the intake and
operator-only tables render as `workflow=*|intake` and `*|operator_only`. Whether `*` is a wildcard the
resolver understands or a rendering convenience is unstated; the suite needs to know which declaration a
task's intake batch cites. Recorded as a wording gap.

**The first checkpoint's resolver.** Bootstrap under the fail-closed reading (step 5 as a gated write)
raises a checkpoint before any channel exists to carry it. The design permits the operator principal to
resolve directly on the record — resolution is "authorized against the required approvers, not accepted
from whoever writes the status" (`authority_model.md#approval`) — so the suite resolves as the operator,
by the credential of step 3. Derived.

### Against `migration.md`'s bootstrap leg

`migration.md#how-the-migration-is-governed` derives the same leg from the other direction — what a
populated instance must gain before any stage of the migration can open — and states that where the two
orders disagree, that document says so. Read side by side:

| This document | `migration.md` | Agreement |
|---|---|---|
| steps 1, 2, and 4: the registry, every type and edge type with `reducer_config` and version; the `operator`; one `ownership_grant` per type | stage 1: the same three, in the same order | the same. One difference of scope inside it: the migration counts `DEPENDS_ON`, `PART_OF`, `REFERS_TO`, and `DUPLICATE_OF` as edge types the record already has; from zero they are registered like the rest |
| step 3: the credential binding, run credential → `operator`, before any attributed write | leg one names "the credential bindings that let a later write resolve to a principal" and places them in no stage | the same set; this document fixes the position (third, before the first ownership edge is written on the operator's behalf) and the migration leaves it loose. Both hit G17, the binding's missing edge type |
| steps 5 to 9: the policy, the first agents, their grants and bindings, the roster | stage 2: the declarations, the policy, the roster bindings, and the grants, listed in that order | the same set and the same dependency graph: stage 2's own verification requires every `owner_role` in both declarations to resolve to an agent with a credential, which is the declaration-time check this document derives its order from. The migration lists the declarations first; this document places the roster, and the agents it resolves to, before the first declaration, because the check needs something to resolve against |
| step 10: the context entities intake's steps read (`priority_rubric`) | absent from leg one | a difference of scope and not a conflict: on a populated instance the context entities exist and are **keep** (`migration.md#context-entities-the-design-retrieves-and-never-migrates`); from zero they do not, and a swarm without them cannot route its first task |
| steps 11 and 12: the intake declaration and the successor declarations the run exercises | stage 2: the intake declaration and the project's `record_migration` declaration | the same kind of act — the first declarations are operator acts — over different sets: the migration needs its own workflow declared and no successor; the suite needs successors and no migration workflow. Both sets are what decision 43 asks to be enumerated |
| step 13: per adapter, the agent, its grant and binding, the context entity, the policy classes | absent from leg one; each adapter's redeployment is admission (`adapters.md#admitting-a-new-adapter`), the migration's stage 9 | the same, once stage 9 is read as admission from zero |
| — | stage 3 (grant widening) before any daemon's first new-type write; stage 4a's halt, confirmed by read-back, before the first declaration merge | the migration's two additions, both derived from what is live on an instance and neither from the design; they do not arise from zero, and the `LEG` fixture's rows (MG-6) test them where a retired engine is stood in |

**One dependency the two share, and it comes first for both.** The registry step registers the design's
edge types, and the record exposes no primitive that adds a relationship type (`migration.md`, G25). The
migration names it the first dependency of the whole plan and not this repository's to resolve; the suite
inherits it exactly (*From zero*, above).

**One disagreement, stated as the migration asks.** It is one of listing and not of dependency: the
migration's stage 2 names the declarations before the roster and the grants, and this document requires
the roster, and the agents it resolves to, before the first declaration. The migration's own stage-2
verification implies this document's order, so this document's order stands for a fresh instance, as that
document's stands for one with a retired engine live — which is the division that document itself draws.
## Findings: rules with no failing artefact

Each row below is a rule the matrix could not give an observable that would go red, and what the rule
would need to say for one to exist. These are findings against the foundation, not against any code.
The matrix rows they came from carry the class **U**.

**Carried back into the homes (the testability pass, revision 37).** Of the thirty rows that stood open after
revision 33, twenty-four are closed below by a change to what the rule says — the largest by the `finding`
row in `data_model.md`, which closes U-6 and with it U-5, U-7, U-25, and contradiction X-14, and lifts the
blocker the matrix had named on decision 32; three point at decisions already open and stay (U-13 → 43,
U-24 → 44, U-30 → 33); two are marked **D**, definitional, with the reason (U-9's runtime half, U-12); and
one, U-14, became decision 56, ruled 2026-09-06. Each closed row keeps its
number and says what its home now says, so the row stays true and a reader can check it against the home.

| # | Rule | Why it has no failing artefact as written | What it would need to say |
|---|---|---|---|
| U-1 | `principles.md#3-validate-the-instrument-before-believing-the-measurement` | **closed** by the testability pass (revision 37): `principles.md#3-validate-the-instrument-before-believing-the-measurement` names the swarm's instruments — drops per window with the dispositions counted beside them, lapses per task, blocked claims per window, the coverage on every adapter observation — and requires a planted positive for each; PR-3's swarm-level half reads them | — |
| U-2 | `principles.md#6-extend-the-mechanism-that-already-generalizes-do-not-build-a-parallel-one` | **closed** by the testability pass (revision 37): `principles.md#6-extend-the-mechanism-that-already-generalizes-do-not-build-a-parallel-one` names the singletons once — one decision queue, one gate, one lease primitive, one succession edge, one engine, one home for step state, one record of an effect — and the registry census is the check; PR-6 and DM-19 read it | — |
| U-3 | `principles.md#8-every-figure-carries-its-date-and-its-instrument-re-measure-before-acting`, and `conformance.md#phases-and-implementation-state`'s rule that a foundation document never carries state | **closed** by the testability pass (revision 37): `conformance.md#phases-and-implementation-state` states the syntactic form — a commit hash in no document but `status.md`; an issue or pull-request number only in the header, a `Sources:` clause, or the *Scope*, *Contradictions*, *Prior art*, and *Beyond the sources* sections — and `conformance.md#mechanical-checks-on-this-directory` names the lint as a contract; the three citations that stood outside those positions were rephrased in the same change. PR-8 and CF-6 read it | — |
| U-4 | `principles.md#10-handing-work-to-the-swarm-is-not-completion` | **closed** by the testability pass (revision 37): "landed" is a derived read over the chain (`work_model.md#a-task-is-executed-only-through-a-workflow`) — a terminal status is written only where the declaration permits closing with no successor (`none_permitted`, U-22), so a `merge_pr` under a declaration that permits no such end is not a chain's end and a task reading terminal there is the failing artefact; PR-10 reads it | — |
| U-5 | `gates_and_workflows.md#findings-verdicts-and-what-a-blocking-finding-obliges`: a verdict carries no condition | **closed** for the mechanical half, **R** for the prose, by the testability pass (revision 37): neither `sign_off` nor `finding` declares a field a condition could be written in, so none can be written as one, and what a finding obliges is a task `REFERS_TO` it or an amendment to the acceptance criteria (U-8); the rule itself now says which half is reviewed (`gates_and_workflows.md#findings-verdicts-and-what-a-blocking-finding-obliges`). GW-22 reads it | — |
| U-6 | the same section: a blocking verdict names its evidence | **closed** by the testability pass (revision 37): the `finding` row (`data_model.md#concepts`) — `severity`, `kind`, `scope`, `evidence`, `text`; `PART_OF` the sign-off that carries it, `REFERS_TO` the batch it judges; evidence required on a blocking finding, whose write is refused without it (`migration.md`, G15 closed). GW-19, GW-23, GW-24, GW-25, and DM-27 read it; ruling 13's hold finding has a home (X-14 closed); the blocker the matrix named on decision 32 is gone, and the decision stands open on its own question | — |
| U-7 | `gates_and_workflows.md#a-finding-is-one-off-or-standing-and-a-standing-one-obliges-a-change-to-what-produced-it`: where a standing finding lands is decided by its specificity, narrowest first | **closed** for attribution, **R** for the judgement, by the testability pass (revision 37): the scope chosen is the finding's `scope`, the proposed change is a task that `REFERS_TO` the finding, and the type the change's write lands in is checkable against the scope (`gates_and_workflows.md#a-finding-is-one-off-or-standing-and-a-standing-one-obliges-a-change-to-what-produced-it`). GW-28 reads it | — |
| U-8 | `gates_and_workflows.md#declaration-batch-projection`: scope amendment versus scope creep, three conditions | **closed** by the testability pass (revision 37): an amendment is a finding plus a correction to the task's `acceptance_criteria[]` whose idempotency key names that finding, attributed to a step owner of the batch, and a correction by anyone else or naming no finding is refused (`gates_and_workflows.md#declaration-batch-projection`; `migration.md`, G6 closed). GW-18 reads it: M for the record, R for whether a change exceeds the criteria | — |
| U-9 | the same section: a step that reads a type it did not declare is a declaration error, caught in the pull request | **D**, definitional, for the runtime half (the testability pass, revision 37): the rule is by its own words a declaration-time rule, and GW-6 tests it there; at runtime the only enforcement point that could see a read is the record, which knows the principal and not the step, and the design keeps no read log by rule (`adapters.md#what-the-record-supplies-and-what-an-adapter-therefore-never-builds`). What binds a read at runtime is the grant, the outer bound (AU-21), and a read within the grant but outside the declaration leaves no artefact by the nature of a read | — |
| U-10 | `failure_posture.md#the-operator-invoked-halt-and-what-undoes-an-action-already-taken`: every action class names its recovery | **closed** by the testability pass (revision 37): `action_policy.recoveries` — class → the recovery's class, `forward_only`, or `none` — with a policy write listing a class and no entry refused at the write (`failure_posture.md#the-operator-invoked-halt-and-what-undoes-an-action-already-taken`; `data_model.md#concepts`). FP-11 reads it | — |
| U-11 | the same section: the restore obligation with a stated cadence | **closed** by the testability pass (revision 37): `recovery_paths[]`, each with a `cadence`, on the binding entity of the system that holds the path; an exercise is a dated observation on that binding carrying what the restore read back; overdue is a derived read (`failure_posture.md#the-operator-invoked-halt-and-what-undoes-an-action-already-taken`). FP-13 reads it | — |
| U-12 | `work_model.md#the-four-execution-mechanisms`: the interactive session holds no lease | **D**, definitional (the testability pass, revision 37): the rule states what an interactive session is — a work source that holds no lease — and the document says a claim requirement would be bypassed in practice; its violation leaves no artefact by its nature. The recovery half is WF-21. WM-37 is marked D | — |
| U-13 | `work_model.md#changing-the-swarm-is-work-and-it-goes-through-a-workflow-like-any-other`: the first declaration is an operator act | the set of operator acts is unbounded (the bootstrap section), and ruling 18 adds to it without bounding it | **closed** in its enumeration half by the ruling of decision 43 (2026-09-06): the set is the thirteen-record table (`#the-minimal-record-set-in-order`), every member read back, and a write to those types after the set exists is not provisioning; the operator's own later governance write is gated — held, self-resolved, marked (43's second half, ruled with 47 on 2026-09-06) — tested under the governance axis |
| U-14 | `gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken`: governance writes are actions | **closed** by the ruling of decision 56 (2026-09-06, `gates_and_workflows.md#where-the-enforcement-point-for-a-governance-write-sits`): a sole-writer grant — the engine acting for the gate holds the only write capability on each governance type and writes on a permit, and every other write is refused at admission under ruling 41 — so both controls have one enforcement point, the grant read at the write; opened by the testability pass (revision 37) and put beside 43, whose gated half decides it | the rows that tested the permitted-action half (WM-22, GW-29, GW-33, WM-40b, AD-27, MG-4, and GW-33a) turn from audit to refusal |
| U-15 | `workflows.md#meeting-processing` `extract`; `gmail.md#what-this-adapter-refuses` refusal 1; `calendar.md#what-this-adapter-refuses` refusal 1: minimization at capture, nothing incidental or sensitive | **closed** for shape, **R** for content, by the testability pass (revision 37): a parameter constraint on a write capability is a field allowlist, denied at admission (`authority_model.md#grants`); refusal 1 of `gmail.md` and `calendar.md` and `extract` in `workflows.md#meeting-processing` cite it. WF-19 reads it | — |
| U-16 | `telegram.md#messages`: a message "reading as a new ask" is a task; one that "asks nothing" is an observation | **closed** by the testability pass (revision 37): every uncorrelated free-text message from a bound principal is a task, and intake's closing sign-off names no successor where it asks nothing (`telegram.md#messages`; `telegram.md#which-checkpoint-a-reply-answers-is-decided-by-correlation-not-by-reading-the-text`). TG-1 reads it | — |
| U-17 | `workflows.md#intake` `link`: every existing record the task names is attached; finding none is a valid close | **closed** by ruling 39 (`workflows.md#what-link-attaches-and-what-it-leaves-to-hydration`): the attachment is bounded to what the task **names**, so none-found and none-searched are no longer one record — a named entity that exists and has no edge is the failing artefact (WF-25). What remains untestable is the pre-ruling form, a search for relevance with no stopping rule, which the ruling refuses | — |
| U-18 | `failure_posture.md#the-rules` rule 2: the announcement travels a path that survives the outage | **closed** by the testability pass (revision 37): rule 1's capture is the announcement of last resort, and on the path's return every captured window is announced with its original time so the path shows no gap (`failure_posture.md#the-rules`, rule 2). FP-23 reads it | — |
| U-19 | `adapters.md#what-the-adapter-does-with-every-event`: drops are counted per window | **closed** by the testability pass (revision 37): the window is declared on `channel_config`, and while the record is reachable the adapter writes one observation per window on its own `agent_session` carrying the dispositions counted (`adapters.md#what-the-adapter-does-with-every-event`). FP-3 and AD-12 read it | — |
| U-20 | `failure_posture.md#repeated-lapse-raises-a-checkpoint`; `gates_and_workflows.md#declaration-batch-projection` (the hold is bounded); rule 5 (every deferral has a ceiling) | **closed**: the hold bound by revision 34 (`hold_bound` on the step); the `on_fail` cap and the lapse cap by the testability pass (revision 37) — `rounds_cap` on the step (`gates_and_workflows.md#declaration-batch-projection`) and `lapse_cap` on the `action_policy` (`failure_posture.md#repeated-lapse-raises-a-checkpoint`), each undeclared treated as the unclaimed-step interval is. FP-6, FP-15, WM-17, and WM-31a read them | — |
| U-21 | `data_model.md#record-conventions`: a sign-off is pinned to the artifact state it judged, by `head` | **closed** by the testability pass (revision 37): `artifact_refs[]` carries a per-kind pinned state, the kinds listed once in `data_model.md#record-conventions` — `head`; the message itself; the message set with its coverage; the dated fact; the declaration as read; the rail state as read — and a kind admitted later states its own under linkage. DM-6 reads it | — |
| U-22 | `workflows.md#security`: the closing sign-off's only permitted successor is `release` | **closed** by the testability pass (revision 37): `none_permitted` beside `successors`; a closing sign-off naming none under a declaration that does not permit it is refused at the write, and `security` does not permit it (`gates_and_workflows.md#sequencing-is-data-successors-and-the-chain`; `workflows.md#security`). WF-12 reads it | — |
| U-23 | `workflows.md#intake` `dedupe`: a duplicate closes terminal with an edge to the task it duplicates | **closed** by the testability pass (revision 37): `DUPLICATE_OF`, task → task (`data_model.md#relationships`; `workflows.md#intake`). WF-5 reads it | — |
| U-24 | `work_model.md#the-claim-and-the-lease-are-one-primitive`, read against the sign-off rule | nothing says whether a sign-off written by a step owner whose lease has lapsed closes the step; two runners of one role can each hold "the step owner's" verdict, and the latest stands | **closed** by the ruling of decision 44 (2026-09-06): a `signed` or blocking sign-off requires a held lease by its signer at the write, and a late one is refused (`#whether-a-sign-off-from-a-step-owner-whose-lease-has-lapsed-closes-the-step`); the work-model axis's cell is mechanical |
| U-25 | `work_model.md#how-a-batch-is-formed-and-what-chooses-its-workflow`: attaching a task part-way through is a step owner's judgement written into that step's sign-off | **closed** by the testability pass (revision 37): `tasks_attached[]` on `sign_off` (`data_model.md#concepts`; `work_model.md#how-a-batch-is-formed-and-what-chooses-its-workflow`). WM-28 reads it | — |
| U-26 | `gates_and_workflows.md#declaration-batch-projection`: an optional step's `applies_when` is evaluated against what the change touches | **closed** by the testability pass (revision 37): an optional step's condition reads what exists when the step would open — the task set where no artifact does — and a condition naming an artifact type no earlier step's `reads_to_close` names is refused at declaration (`gates_and_workflows.md#declaration-batch-projection`). GW-14 and WF-13 read it | — |
| U-27 | `telegram.md#during-a-halt-a-read-on-the-channel-is-answered-with-the-halt-and-never-with-data`: the answer goes only to the chat the adapter's binding names, read at start and not from the record | **closed** by the testability pass (revision 37): the start-time binding is a cache with the staleness bound `channel_config` declares, refreshed on every successful read and `Indeterminate` past it, when the adapter answers no one (`telegram.md#during-a-halt-a-read-on-the-channel-is-answered-with-the-halt-and-never-with-data`). TG-12a reads it | — |
| U-28 | `work_model.md#a-recurring-task-is-one-live-instance-and-its-completion-creates-the-next`: the next instance is created "as part of" the closing sign-off | **closed** by the testability pass (revision 37): the creation precedes the sign-off and is read back first, the bounded retrieval makes a re-claim idempotent, and the closing interval is the one moment two instances are live (`work_model.md#a-recurring-task-is-one-live-instance-and-its-completion-creates-the-next`). WM-35a reads it | — |
| U-29 | `gates_and_workflows.md#a-finding-is-one-off-or-standing-and-a-standing-one-obliges-a-change-to-what-produced-it`: a finding standing on the agent lands in that agent's prompt or the `agent_policy` it renders from | **closed** by the memo-gap pass (revision 31): the section now names `task_policy` as the home for a standing finding whose content is operator-specific, with the prompt gaining at most the instruction to read the type; GW-29a tests it. Kept so the number is not reused | — |
| U-30 | `gates_and_workflows.md#declaration-batch-projection`: a contiguous named group of steps is a stage, and each step carries `phase`; `workflows.md#whether-a-stage-names-anything-a-step-does-not` | no rule reads a stage or a step's `phase` — no gate, verdict, fast path, condition, successor, or checkpoint keys on either — so no state changes when one is wrong, and by principle 4's own test the field is decoration; the matrix confirms what open decision 33 argues | either a reader that keys on it, stated as a rule, or the retirement the decision proposes; the matrix is neutral between them, having nothing to observe under either |
| U-31 | `work_model.md#what-a-claim-predicate-treats-as-claimable`: the predicate reads the record's live status vocabulary onto `open`, `blocked`, or terminal | **closed** by the testability pass (revision 37): the registered `task` type declares its terminal set, one spelling per meaning; a closing sign-off writes from it and a value outside it is refused; the reader stays tolerant permanently (`work_model.md#what-a-claim-predicate-treats-as-claimable`; `migration.md`, G7 closed). WM-11, WM-16, and WM-20 read it | — |
| U-32 | `data_model.md#write-contract`: a self-triggering daemon writes the tasks its poll produces and observations carrying its provenance, and nothing else; `failure_posture.md#the-rules` rule 2: a silently halted swarm is indistinguishable from an idle one | **closed** by the testability pass (revision 37): the window observation of U-19 is what a successful empty poll writes, so a daemon silent past its window is a derived read (`data_model.md#write-contract`; `migration.md`, G13 closed). DM-22 reads it | — |
| U-33 | `workflows.md#meeting-processing`, the entry condition; `workflows.md#research-and-analysis` `persist` | **closed** by the memo-gap pass (revision 31): `REFERS_TO` is widened to task → a record entity it concerns, and to sign-off → a record entity it read (`data_model.md#relationships`; `migration.md`, G12 closed); WF-19 and WF-25 read the edge. Kept so the number is not reused | — |

## Contradictions: a state one test requires and another forbids

Of the eighteen, fifteen stood open after revision 33. Every one is now closed in the document that owns
the rule, cited from the other (the testability pass, revision 37), and each row below says where; the
numbers are kept so that none is reused. X-8's settlement is the one that changes a rule rather than a
wording — a daemon takes no action of its own — and it is marked as such in `work_model.md`'s own
contradictions section, where C2 had stood open since the first revision.

| # | The two rules | The conflicting observables | What the suite does until it is corrected |
|---|---|---|---|
| X-1 | `gates_and_workflows.md#the-checkpoint` — a subject is an action or a task, never a batch — against `failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol` — the `unclaimed_step` checkpoint's subject, formerly the step's batch | **closed** by the memo-gap pass (revision 31): a condition of a batch is raised on one of its tasks, never on the batch, one checkpoint per stopped batch naming the batch and the step in `needed_input`, and no second with the same reason on another task of the batch while one is open. GW-50 and FP-24 test the ruling. Kept so the number is not reused | — |
| X-2 | `data_model.md#concepts` `checkpoint.reason` against `failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol` — a data-model copy of the reason classes that had drifted from the one enumeration | **closed** by the simplification pass (revision 29): the data model now cites the one list, and a registry built from it refuses nothing the failure posture requires. The number is kept so it is not reused | FP-16 tests the one enumeration; nothing remains to hold |
| X-3 | `data_model.md#relationships` `SIGNED_BY` sign-off → agent against `gates_and_workflows.md#declaration-batch-projection` — only the operator principal writes `waived` | **closed** by the testability pass (revision 37): `SIGNED_BY` is sign-off → principal — the step owner's agent, or the operator principal on `waived` (`data_model.md#relationships`); the sign-off's `agent` and `agent_version` are absent on the operator's waiver. DM-1 reads the registry it implies | — |
| X-4 | `work_model.md#how-a-batch-is-formed-and-what-chooses-its-workflow` — a batch opens on a closing sign-off "and at no other moment" — against the same section's intake batch "opened on the task's creation" | **closed** by the testability pass (revision 37): a batch comes into existence at one of two moments and no other — a task's creation, which opens its intake batch, and a closing sign-off naming a successor (`work_model.md#how-a-batch-is-formed-and-what-chooses-its-workflow`). WM-13 and WM-27 read both | — |
| X-5 | `workflows.md#bug` — a bug needing a design choice closes without a successor "and [has] the task enter intake again" — against `workflows.md#intake`'s entry condition — "no task meets it twice" — and now against ruling 38 (`gates_and_workflows.md#closed-work-is-reviewed-on-the-record-and-redone-through-intake-never-reopened`), under which further work on closed work is a **new** task through intake | **closed** by the testability pass (revision 37): the bug section is rewritten under ruling 38 — the batch closes with no successor, its closing sign-off carrying the finding that names the design choice, and the design work is a new task through intake referring to this batch's artifacts (`workflows.md#bug`). WF-11 reads it | — |
| X-6 | `github.md#issues` — `assigned_to` "is written only by intake's `classify`" — against `work_model.md#assignment-restricts-eligibility-it-never-creates-a-lease` and `scenarios.md#c-assignment-then-the-named-principal-claims` — a principal writes `assigned_to`, "a field write like any other" | **closed** by the testability pass (revision 37): `assigned_to` is a principal's write — at intake's `classify`, or later — and never an adapter's from a host's assignment (`work_model.md#assignment-restricts-eligibility-it-never-creates-a-lease`; `github.md#issues`, the assigned row). WM-4 reads the work model's rule, and the disposition test reads the host's row | — |
| X-7 | `data_model.md#write-contract` — an adapter writes observations, confirmations, tasks, and drops — against `adapters.md#no-external-event-advances-a-step-by-itself`, `github.md#reviews-review-comments-and-threads`, `gmail.md#messages`, `telegram.md#callbacks-and-queries` — an adapter writes a checkpoint resolution attributed to the operator | **closed** by the testability pass (revision 37): the adapter's write-contract row names the sign-off and the checkpoint resolution it carries in, attributed to the principal the credential binds to and to itself as carrier, A-for-B (`data_model.md#write-contract`). DM-21 reads the column as it now stands | — |
| X-8 | `data_model.md#concepts` — an `action` is `PRODUCES` ← task and `gates_and_workflows.md#the-action-gate-is-pr-independent` — the checkpoint "keys on the action and its task" — against `work_model.md#contradictions-this-document-settles` C2 — a daemon's effect is an action through the same gate and passes through no task | **closed** by the testability pass (revision 37): C2 is settled by the write contract — a daemon writes tasks and observations and nothing else, so it takes no action of its own; every effect is an action `PRODUCES` from a task, and a daemon that wants one creates the task (`work_model.md#contradictions-this-document-settles`; `work_model.md#a-task-is-executed-only-through-a-workflow`; `data_model.md#write-contract`). The announcement path is the one write with no action behind it and is not an action. No row was held pending it in the end; WM-36 gains the negative | — |
| X-9 | `adapters.md#what-the-adapter-does-with-every-event` — an outbound operation the adapter cannot take for want of a credential resolves to `dropped` — against the same document's rule that an unconfirmed effect reads `unknown`, and `authority_model.md#grants` — a denied capability raises a checkpoint `capability_denied` | **closed** by the testability pass (revision 37): an outbound operation the adapter cannot take for want of a credential is a denial before the effect — `capability_denied` on the task, the action untaken — and never a drop, since `dropped` is a delivery's disposition (`adapters.md#what-the-adapter-does-with-every-event`). AU-9 reads it | — |
| X-10 | `gates_and_workflows.md#one-step-set-defined-once-tested-for-parity` — "a data-sourced list may add steps and never remove one" — against `gates_and_workflows.md#an-unreadable-workflow-is-unknown-and-unknown-holds` and `failure_posture.md#contradictions-this-document-settles` C5 — nothing proceeds on an empty sequence, no floor | **closed** by the testability pass (revision 37): the floor-list sentence is retired from `gates_and_workflows.md#one-step-set-defined-once-tested-for-parity` — there is no base list in code that data adds to; an unreadable declaration opens nothing, and a copy that differs from the declaration fails parity. GW-30 and GW-42 read it | — |
| X-11 | `data_model.md#concepts` `workflow` key fields (no `reads_to_enter`, `reads_to_close`, `freshness`, `applies_when`) and `sign_off` (no findings) against `gates_and_workflows.md#declaration-batch-projection`, which declares all of them, and the data model's own rule that a concept with no row is not persisted | **closed** by the workflow-format pass (revision 34): the `workflow` row carries `applies_when`, the read dependencies, `freshness`, and the two intervals; and by the testability pass (revision 37): the sign-off carries its findings by edge and `tasks_attached[]` (`data_model.md#concepts`). DM-1 reads the table | — |
| X-12 | `workflows.md#feature` `impl` closes on a pull request existing with CI green, against `github.md#conditions-that-are-not-events` — the design's position is that `impl` closes on the pull request "existing and being mergeable" | **closed** by the testability pass (revision 37): `impl` closes on a pull request existing, its CI green at the pinned head, and it being mergeable as read, stated in `workflows.md#feature` and cited from `github.md#conditions-that-are-not-events`. WF-9 reads it | — |
| X-13 | `failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol` — the `unclaimed_step` checkpoint is "routed against the owner role" and "awaits the operator" — against `data_model.md#concepts` — `AWAITS` → principal | **closed** by the testability pass (revision 37): the `unclaimed_step` checkpoint's `AWAITS` edge names the operator principal, and the role is named in its `needed_input`, which is what "routed against the role" means (`failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol`; `authority_model.md#approval`; `data_model.md#relationships`). FP-18 reads it | — |
| X-14 | `work_model.md#a-batch-may-hold-on-a-condition-discovered-mid-flight` — a hold is a non-blocking finding written with no sign-off and renewed by observations on it — against `vocabulary.md#sign-off` and `gates_and_workflows.md#findings-verdicts-and-what-a-blocking-finding-obliges` — findings are what a sign-off carries — and `data_model.md#concepts`, which gives a finding no row | **closed** by the testability pass (revision 37): the finding is an entity of its own, `PART_OF` the sign-off that carries it where one is written and `REFERS_TO` the batch it judges, so a hold's finding stands with no sign-off and is renewed by observations on it (`data_model.md#concepts`; `gates_and_workflows.md#findings-verdicts-and-what-a-blocking-finding-obliges`; U-6 closed). WM-31 and DM-27 read it | — |
| X-15 | `work_model.md#a-batch-may-depend-on-a-task-it-created` — "the record refuses a `DEPENDS_ON` write that would close a cycle", its hierarchical types being cycle-checked at write — against the same section's own walk, which runs `DEPENDS_ON` then `ADDRESSED_BY` and is "a read the writer makes before it writes" | **closed** by the testability pass (revision 37): the record refuses the cycles its per-type check can see, and the writer is the enforcement point for the loop through `ADDRESSED_BY`, stated as such (`work_model.md#a-batch-may-depend-on-a-task-it-created`); it is the shape decision 56 ruled for governance writes — the writer is the enforcement point, and it checks before it writes. WM-32a reads it | — |
| X-16 | `gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken` — the governance types — against `work_model.md#changing-the-swarm-is-work-and-it-goes-through-a-workflow-like-any-other` and the standing-finding section, which named a different membership | **closed** by the memo-gap pass (revision 31): one closed list of eight, in one home, with the admission test stated, and the other two sections citing it (`migration.md`, G1 closed). WM-22 tests the eight. Kept so the number is not reused | — |
| X-17 | `work_model.md#what-a-claim-predicate-treats-as-claimable` and `work_model.md#the-transition-vocabulary` — `blocked` is a task status and a `blocked` task is not claimable — against `failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol` and `gates_and_workflows.md#the-checkpoint` — a task the swarm cannot advance is held by a checkpoint, and on resolution "the task is re-claimed or closed" | **closed** by the testability pass (revision 37): `blocked` is retired as a task status; an open checkpoint whose subject is a task holds it from claim, `unclaimed_step` excepted, and claimability is read from that edge (`failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol`; `work_model.md#what-a-claim-predicate-treats-as-claimable`; `vocabulary.md#retired-names`; `migration.md`, G8 closed). WM-11 and WM-15 read it | — |
| X-18 | `workflows.md#meeting-processing` — *Artifacts: the transcript file* — against `work_model.md#artifacts-are-records-a-batch-leaves-never-its-subject` — a local file is reached through no adapter and has no `system` | **closed** by the testability pass (revision 37): the transcript is a source in the record — the `transcription` entity, or the uploaded file with its provenance — and the Artifacts line names the calendar event alone (`workflows.md#meeting-processing`; `migration.md`, G11 closed). WM-33 reads it | — |

## The conformance matrix

Rows are grouped by the document that owns the rule. A row's first cell is its id; its second is the rule,
as a pointer. Where a section carries several rules, each gets a row. Setup is written against the fixtures
above. **M / R / U / P / D** is the class.

### `principles.md`

| # | Rule | Setup | Action | Red when | Class |
|---|---|---|---|---|---|
| PR-1 | `principles.md#1-a-mechanism-that-does-not-bind-is-not-a-control` | the suite's own manifest | for every **M** row, the meta-test introduces the row's mutant | any **M** row stays green under its mutant; any check the suite names has no test that runs it | M |
| PR-2 | `principles.md#2-a-write-that-reports-success-has-not-necessarily-happened-read-it-back` | `B0`, `RP` | every decision-carrying write in every other row | `RP` shows a decision write (sign-off, resolution, confirmation, governance write) with no retrieve of the written entity from the same credential before its next write | M |
| PR-3 | `principles.md#3-validate-the-instrument-before-believing-the-measurement` | the three instruments, and the swarm's four the invariant names | plant one drop, one lapse, one blocked claim, one partial read; one request, one call, one announcement | any instrument reads zero on its planted positive; a counter the design names has no planted positive | M (U-1 closed) |
| PR-4 | `principles.md#4-a-test-that-cannot-fail-on-the-thing-it-watches-is-decoration` | the suite's manifest | remove each rule's enforcement in the reference implementation | the row's test stays green | M (suite-level); R for tests a code branch adds, whose revert result is recorded in the PR body |
| PR-5 | `principles.md#5-fail-closed-on-the-field-that-carries-the-safety-meaning` | `B0+pol(low: [docs])` | an action of class `operator_only`; one of an unknown class; one with class absent; a grant read that fails; a policy read that fails | any of the first two is taken; the third does not take the policy default; either read yields anything but `Indeterminate` → deny | M |
| PR-6 | `principles.md#6-extend-the-mechanism-that-already-generalizes-do-not-build-a-parallel-one` | `B0` | the registry census after the full run | a second type for a listed singleton (a held-decision type beside `checkpoint`, a claim-history type, a per-step status type) | M — the singletons are named once in the invariant, and DM-19 is the census (U-2 closed) |
| PR-7 | `principles.md#7-unknown-stays-distinct-from-a-verdict` | `B0`, `RP` failing reads | read gate, grant, drift, reachability, CI state under a failed read | any reader returns pending, clear, empty, or the zero value instead of `unknown` | M |
| PR-8 | `principles.md#8-every-figure-carries-its-date-and-its-instrument-re-measure-before-acting` | the documents | run the citation lint | a commit hash in any document but `status.md`; an issue or pull-request number outside the positions `conformance.md#phases-and-implementation-state` names | M (contract; U-3 closed) |
| PR-9 | `principles.md#9-one-source-defined-once-a-comment-claiming-parity-is-not-parity` | `B0` | read the step set, the never-set, the gate set from every place a copy could live | two copies differ, or a copy exists with no parity test naming it | M (parity), R (that no unlisted copy exists) |
| PR-10 | `principles.md#10-handing-work-to-the-swarm-is-not-completion` | `T-at(feature, merge)` signed naming `release` | read the task's terminal status before `verify_deployed` is signed; declare a `feature` workflow for a project that does not deploy its default branch with `none_permitted` | the task reads terminal; the declaration lands | M (U-4 closed: the declaration permits none, or it does not) |
| PR-11 | `principles.md#11-state-that-needs-a-watchdog-belongs-in-a-relationship-not-a-field` | `B0` | inspect the registered `task`, `batch`, `artifact`, `action` types | a field named `claimed_by`, `claimed_at`, `lease_*`, `active`, `last_synced_at`, `current_workflow`, `stale`, a liveness or execution-state field, or a per-step status field exists | M |

### `work_model.md`

| # | Rule | Setup | Action | Red when | Class |
|---|---|---|---|---|---|
| WM-1 | `work_model.md#pull-is-the-only-delivery-assignment-constrains-eligibility` | `T1`, two agents A and B with grants | A writes a `LEASE` edge naming B as holder | the write is accepted; or `RP` shows a lease whose writing credential is not the holder's | M |
| WM-2 | the same: an `assigned_to` naming a principal nobody can run | `T1` with `assigned_to` = a name not in the roster | any agent evaluates claimability | no checkpoint `unspawnable_assignee` on the task; or a lease from any principal | M |
| WM-3 | the same: subscriptions wake, never deliver | `T1`, an agent subscribed to task creation | the subscription fires | a lease exists whose `claimed_at` precedes any claim request from the agent in `RP` | M |
| WM-4 | `work_model.md#assignment-restricts-eligibility-it-never-creates-a-lease` | `T1` | a principal writes `assigned_to` = B | a `LEASE` edge exists; or B's claimability read is anything but true and A's anything but false; an adapter writes it from a host's assignment | M (X-6 closed) |
| WM-5 | `work_model.md#the-claim-and-the-lease-are-one-primitive` | `T1`, `CLK`, twenty runners of one role | all claim at once | more than one `LEASE` edge reads `held`; or a runner proceeds whose read-back does not name its own `runner_id` | M |
| WM-6 | the same: atomicity proven, never assumed | as WM-5, with the store's write path mutated to last-writer-wins | as WM-5 | the mutant does not turn WM-5 red | M (meta) |
| WM-7 | `work_model.md#the-lease-is-a-relationship-not-a-set-of-task-fields` | `T1` claimed | read the task's observations | any lease field on the task; or a lease state written as a value rather than read from `expires_at` | M |
| WM-8 | the same: renewal moves `expires_at`; the clock lapses it | `T1` claimed, `CLK` | renew twice; stop; wait past `expires_at` | a write to the lease edge after the last renewal; or the read is not `lapsed` after expiry | M |
| WM-9 | `work_model.md#liveness-is-derived-from-activity-at-read-time-never-declared` | `T1` claimed, runner writing `agent_session` and observations | read `active`; stop the activity; read again | a stored liveness value anywhere; `active` unchanged after activity stops within the window | M |
| WM-10 | `work_model.md#no-assignment-log-history-is-the-tasks-own-observations` | `B0` | census after a full run | any entity type whose rows are claim, assignment, or transition history | M |
| WM-11 | `work_model.md#the-transition-vocabulary` | `T1` through a full life | read every status observation | a status value outside `open` or the terminal set the registered type declares — a liveness word, a routing word, a queue word, `in_progress`, `blocked`; a closing sign-off writing a terminal value outside that set lands (U-31 closed) | M |
| WM-12 | `work_model.md#there-is-no-task-lifecycle-there-are-batches` | `T-routed(feature)` | read the task | a field stating the workflow, the step, or "in review" on the task | M |
| WM-13 | `work_model.md#intake-is-every-tasks-first-workflow` | `T1`; a task created by a batch; a task from a daemon | read each task's batches | any batch of a workflow other than intake with no intake batch before it on the chain; or a task with two intake batches (X-5) | M |
| WM-14 | the same: a child may take intake's fast path and never skips intake | a child created by a routed batch | read the child's intake batch | `link` or `dedupe` open; or no intake batch | M |
| WM-15 | `work_model.md#what-a-claim-predicate-treats-as-claimable` | tasks in every status the record holds, one with a lapsed lease, one assigned, one under an open `repeated_lapse` checkpoint, one of a batch under an open `unclaimed_step` | each agent evaluates claimability | a terminal task reads claimable; the task under `repeated_lapse` reads claimable; the step under `unclaimed_step` reads as not claimable to its role; a lapsed lease blocks; an assigned task reads claimable to another; a `blocked` status anywhere (X-17 closed) | M |
| WM-16 | the same: the predicate is written against the record, not an enum | a task with a terminal status value the predicate does not enumerate | evaluate | it reads claimable | M |
| WM-17 | `work_model.md#a-lapsed-lease-is-not-reaped-repeated-lapse-raises-a-checkpoint` | `T1`, `CLK`, lapse cap = 3 | let the lease lapse three times by killing the runner | any write to a lease edge by anything but its lease holder; no checkpoint `repeated_lapse` carrying the count and the last lease holders after the third; the watchdog chooses the next lease holder | M |
| WM-18 | `work_model.md#at-least-once-implies-effect-dedup` | `T-at(feature, merge)`, `X(github)`; the merge action confirmed; the lease lapses | a second runner re-claims | `X(github)` logs a second merge; or the action's `dedup_key` is not the key the first attempt wrote | M |
| WM-19 | `work_model.md#operator-only-tasks-are-claimed-by-the-operator-facing-agent` | a task with `operator_only` in `action_type` | every agent evaluates claimability | any but the operator-facing agent reads it claimable; a checkpoint exists before an action reaches the gate; the operator-facing agent's lease is not renewed while the checkpoint is open | M |
| WM-20 | `work_model.md#a-task-is-executed-only-through-a-workflow` | every terminal task after a full run | read the chain | a terminal task with no batch whose closing sign-off carries a terminal-writing verdict; a status write to `done` with no sign-off behind it | M |
| WM-21 | the same: a daemon's task enters intake like any other | `X(gmail)` delivers an untracked message | read the task | no intake batch; a batch of another workflow; a lease held by the daemon that created it | M |
| WM-22 | `work_model.md#changing-the-swarm-is-work-and-it-goes-through-a-workflow-like-any-other` | `B0+pol(high: [governance classes])` | an agent writes to each of the eight governance types — `agent`, `agent_policy`, `workflow`, `action_policy`, `agent_grant`, `swarm_roster`, the registry, `intake_rule` — and to a `task_policy` | any of the eight lands with no `action` of that class `PRODUCES` from a task in a batch, or with no checkpoint under a `HIGH` tier; the `task_policy` write is gated (it is an input type, not a governance type) | M (decision 56, ruled 2026-09-06: a write by any credential but the engine's is refused at admission, and the engine's names the permitted action it is taken under) |
| WM-22a | the same, ruling 18: a governance class with no policy value is `operator_only`, never the policy default and never a high tier; the loosening is a grant class by class, reversed by removing the value; an ungranted class makes the batch an operator-only one | `B0` with no governance values; then one class listed high; then the value removed | an action of each governance class | a class with no value taken, or held at `HIGH` rather than `NEVER`; the listed one not held then taken; after removal, taken; a batch producing an ungranted class claimed by anyone but the operator-facing agent | M |
| WM-22b | the same: the class covering `action_policy` writes is itself reserved | `B0` | an agent writes a policy value granting a class | it lands with no checkpoint | M |
| WM-23 | the same: an agent cannot widen its own grant | agent A | A writes an `agent_grant` naming A | the grant is read as in force with no permitted action behind it | M |
| WM-24 | the same: `operator_only` on a governance class cannot be demoted | `B0+pol(low: [agent_policy_write], operator_only: [agent_policy_write])` | an action of that class | it is taken | M |
| WM-25 | the same: bootstrapping — the set is the thirteen-record table, every member read back | `B0` | write the thirteen records as the operator principal, reading each back; route the first task | the first task does not route with all thirteen written; a member not read back; a record outside the table needed before the first task routes | M (decision 43, ruled in both halves: the set, and the operator's later governance write held and resolved as a marked self-resolution — the governance axis; AU-17) |
| WM-26 | `work_model.md#what-goes-through-a-workflow-is-a-batch-of-tasks` | three tasks routed together | read the batch | more or fewer than one batch; a task with no `ADDRESSED_BY`; a batch-of-one taking a path a batch-of-three does not | M |
| WM-27 | `work_model.md#how-a-batch-is-formed-and-what-chooses-its-workflow`: opened only by a closing sign-off | every batch after a full run | read `FOLLOWS` and the predecessor's closing sign-off | a non-intake batch with no `FOLLOWS`; a `FOLLOWS` target whose closing sign-off's `successor` is not this batch's `workflow_type`; a batch opened by a daemon, an adapter, or a sweeper credential in `RP` | M |
| WM-28 | the same: attach part-way is a step owner's judgement in a sign-off; the task inherits the sign-offs | a batch at step 3; attach a task | read the edge and the sign-offs | an `ADDRESSED_BY` edge created after open that no sign-off's `tasks_attached[]` names; the attached task's step state differs from the batch's | M (U-25 closed) |
| WM-29 | the same: the workflow is fixed at open | any batch | attempt a correction of `workflow_type` | the correction lands | M |
| WM-30 | the same: a daemon noticing eligible tasks, an adapter on an event, a sweeper, a label — none opens a batch or chooses a workflow | `X(github)` delivers a labeled issue; a daemon runs | read batches | a batch whose opening write in `RP` is by an adapter or daemon credential; a `workflow_type` equal to a label | M |
| WM-31 | `work_model.md#a-batch-may-hold-on-a-condition-discovered-mid-flight`: a hold is a finding naming the condition, no sign-off, the lease renewed; no held state and no field | `T-at(payment, pay)`, `X(rail)` returning `unknown`, `CLK` | the step owner holds | no finding naming the condition and what would resolve it; a sign-off written; the lease not renewed; a held, paused, or waiting value on the batch or the task; the hold's duration not readable from observations on the finding; the finding not a `finding` entity `REFERS_TO` the batch with no `PART_OF` to any sign-off | M (X-14 closed) |
| WM-31a | the same: a hold ends by sign-off, checkpoint, or lapse, never by elapsed time into a pass; a hold owing nobody a decision is a rule-5 deferral | four holds: the condition resolves; it owes a decision; it never resolves and owes none; the holder dies | wait | the first does not end in a sign-off; the second raises no checkpoint; the third raises no `rounds_exhausted` carrying the finding at the ceiling; the fourth does not lapse and read claimable; any closes by time | M (`hold_bound`; U-20 closed) |
| WM-32 | `work_model.md#a-batch-may-depend-on-a-task-it-created`: a `DEPENDS_ON` edge, never a field; the sign-off is refused while it is unended and the task non-terminal; ending it is a recorded act | a batch whose step creates a task and records the dependency | write the sign-off before the task is terminal; end the edge; write again | the sign-off lands unended; a `blocked_by` field or list exists; the edge lacks `created_at`; the end lacks `ended_at` | M |
| WM-32a | the same: a cycle is refused at write and at attach; one found later escalates every batch in it as `dependency_cycle` | two batches whose dependencies would form a loop through `ADDRESSED_BY`; a task attached part-way that joins two chains; a loop planted directly | write; attach; read | the edge or the attach lands; the planted loop raises fewer checkpoints than batches in it, or one that does not name the batches and edges; any step owner in the loop signs | M — the cross-type walk is the writer's (X-15), so the mutant is a writer that skips it |
| WM-32b | the same: `DEPENDS_ON` is not on the chain; the created task is a peer with its own intake and its own priority | as WM-32 | read the chain and the task | the chain follows a `DEPENDS_ON` edge; the created task has no intake batch; its priority moved with the dependency | M |
| WM-33 | `work_model.md#artifacts-are-records-a-batch-leaves-never-its-subject` | every artifact after a full run | read `system`, `external_id`, and every `CLOSES` edge | an artifact with a null or pending `external_id`; an artifact whose thing lives in the record (a draft, an analysis, a sign-off); a sign-off `CLOSES` an artifact | M |
| WM-34 | `work_model.md#a-task-is-in-at-most-one-batch-at-a-time` | a task in a live batch | attach it to a second live batch | the second edge lands | M |
| WM-35 | `work_model.md#parent-and-child-tasks` | a parent with three children | claim the parent; complete two children | the parent reads claimable or gets an `ADDRESSED_BY`; the parent reads complete before the third; a stored parent status | M |
| WM-35a | `work_model.md#a-recurring-task-is-one-live-instance-and-its-completion-creates-the-next`: one live instance, never zero and never two; the closing sign-off creates the next, `FOLLOWS` task to task, the rule copied | a task with a `recurrence` rule, routed and completed; `RP` failing the creation write; `RP` failing the sign-off after the creation, then a re-claim | read after the closing sign-off | zero non-terminal instances, or two outside the closing interval; the new one lacks `FOLLOWS` to the completed one or its `recurrence`; the completed one's status ever leaves terminal; the new one has no intake batch; under the failed creation the sign-off landed; under the failed sign-off the re-claim creates a second successor (U-28 closed: create, read back, then sign) | M |
| WM-35b | the same: `due_date` is computed from the schedule, never from completion; a missed point is owed unless the rule says otherwise | a weekly instance completed two weeks late; one whose rule forgives a missed occurrence | read the next instance | its `due_date` is the completion time plus an interval; the first is not created already late; the second is not dated at the first grid point after the close | M |
| WM-35c | the same: no series entity, count, or live marker; the rule lives on the instance; ending the series is a correction to the live instance's rule; postponing is a `due_date` correction and creates nothing | the series above | census; correct the rule to end, complete; postpone a live instance | a series type, series id, occurrence count, or stored live marker; an instance created after the rule ended; a postponement creating one | M |
| WM-35d | the same: a stopped series is one overdue instance, and its batch reaches the queue | a live instance whose batch stops advancing, `CLK`, its workflow declaring an unclaimed-step interval; another whose workflow declares none | wait | no non-terminal instance with a past `due_date` is readable; no `unclaimed_step` on the first; the second raises one (the ruling names an undeclared interval a declaration defect that raises nothing) | M |
| WM-35e | the same: a recurring task and an action series meet only at the gate | a recurring task whose instances take an action of a class that has graduated; an instance that took none | read | the instance's `consent`-shaped step absent because of graduation; the recurrence altered by graduation; the actionless instance counted in a series | M |
| WM-36 | `work_model.md#the-four-execution-mechanisms`: a daemon receives no task and takes no action of its own; the engine never writes task status | a full run | `RP` per credential, `X(*)` | a daemon credential holds a lease; the engine credential writes `task.status`; a daemon credential takes an action `PRODUCES` from no task, the announcement path excepted (X-8 closed) | M |
| WM-37 | the same: the interactive session | — | — | D (U-12: definitional — the session holds no lease by what it is; the recovery half is WF-21) |
| WM-38 | `work_model.md#whether-the-step-path-is-a-mechanism-of-its-own-and-what-the-engine-is-called` | — | — | P (open decision 34; WM-36's observables are per credential and do not depend on the count or the name, so no row changes under either option) |
| WM-39 | `work_model.md#where-tasks-come-from-every-source-indexed`: nine sources, every one ending in a task with no intake batch; the creating principal holds no privilege over the task | a full run exercising each source | read every task's provenance and first batch | a task whose provenance names none of the nine kinds; a task whose first batch is not intake; a lease on a created task held by the credential that created it | M |
| WM-40 | `work_model.md#an-intake-rule-turns-a-described-change-in-the-record-into-a-task-and-nothing-else`: a rule is subject types, change kinds, a predicate, a provenance predicate, the task's text, and a ceiling; it produces one task per rule per change, with provenance naming the rule and the change, and nothing else | `B0` plus one `intake_rule` on `contact`; a matching change; the same change delivered twice | evaluate | two tasks for one change; a task with no provenance to the rule and the change; a batch opened, a task attached, a workflow named, an action taken, or an `assigned_to` written by the evaluator; a `task_type` entity in the census | M |
| WM-40a | the same: an unevaluable predicate does not fire and is not silent (`dropped`, reason `unevaluable`, counted); the ceiling stops the rule for the window (`dropped`, reason `ceiling`, counted) | a rule reading a field its subject type lacks; a rule with a ceiling of two and three matching changes | evaluate | the unevaluable rule fires, or its change has no disposition; the third change fires, or is dropped uncounted; `CH` shows no aggregated count | M |
| WM-40b | the same: a rule write is a governance write, reserved by default; a rule is ended by a correction, never deleted; the evaluator is a daemon under the write contract — during a halt it writes nothing, and on return it evaluates from the record's change log along ingestion time, never from a cursor of its own | an agent writes a rule with no policy value; the operator ends a rule; `RP` failing then restoring | write; halt; return | the agent's rule lands with no checkpoint; a rule deleted; tasks the ended rule created losing their provenance target; a task written during the halt; a cursor file or last-evaluated field; a change made during the halt never evaluated after it | M (decision 56, ruled 2026-09-06: the rule write lands only through the engine's sole grant, on a permit) |
| WM-41 | `work_model.md#whether-an-intake-rule-may-key-on-the-work-models-own-records`: a rule keys on no work-model record type | a rule naming `task` in `subject_types[]`; one naming `checkpoint`; one naming an artifact type and a field a step wrote | write each; census after a run | either work-model rule lands; a task carrying provenance from one; the artifact-type rule refused | M (decision 36, ruled 2026-09-06: refused at the write, the operator's lean toward every type considered and set aside) |

### `gates_and_workflows.md`

| # | Rule | Setup | Action | Red when | Class |
|---|---|---|---|---|---|
| GW-1 | `gates_and_workflows.md#declaration-batch-projection`: one `workflow` per (project, type) | `B0` | declare a second `(project, feature)` | it lands | M |
| GW-2 | the same: `owner_role` holds a role, never an agent name | declare a workflow with an agent's name as `owner_role` | — | it lands; or a claim resolves it without the roster | M |
| GW-3 | the same: step state is derived; no step entity | `T-at(feature, arch)` | census; read step state | a per-step entity or status row; state that does not change when the lease or sign-off does | M |
| GW-3a | the same: `step_status` is the projection of the batch's sign-offs, proved equal to them by a reconciler; neither is deleted, neither is a second source of truth; no transition event type | `T-at(feature, arch)` signed; the projection mutated to disagree | read both; run the reconciler | the reconciler reports agreement, or nothing runs it; a reader takes the projection where it disagrees with the sign-offs; a transition-event type in the census | M |
| GW-4 | the same: a rejected sign-off write is an error, never swallowed | a sign-off missing a required field | write it | the step reads signed; or the writer proceeds with no error in `RP` | M |
| GW-5 | the same: a step does not proceed when a declared read is unreadable | `T-routed(feature)`, `RP` failing reads of one declared type | the step would open | it opens; or the step owner claims | M |
| GW-6 | the same: a step that reads an undeclared type is a declaration error | a declaration whose step's `reads_to_enter` names a type outside the owner's `context_entity_types[]` | declare it | it lands | M (declaration); D at runtime (U-9: the grant is the outer bound, AU-21, and the design keeps no read log) |
| GW-7 | the same: freshness for adapter-sourced types is derived | `X(github)` observations with coverage | read freshness | a stored `last_synced_at` or freshness field; freshness that ignores coverage | M |
| GW-8 | the same: hydration before the step, never during | `T-routed(feature)`, `X(github)` logging | the step opens, is claimed, closes | any `X` call from the step owner's credential between its lease `claimed_at` and the `reads_to_close` phase | M |
| GW-9 | the same: a hydration failure holds, bounded, then escalates `undeclared_dependency` | `X(github)` unreachable, `CLK` hold bound | the step would open | it opens; no announcement on `CH` during the hold; no checkpoint `undeclared_dependency` naming the type at the bound; more than one | M |
| GW-10 | the same: a degraded read never synthesizes a permissive value | `RP` failing the grant read, the policy read, the workflow read; `X(github)` failing the check read | evaluate each reader | a wildcard capability set, an empty finding list read as clear, a missing policy read as no restriction, a check read as passing | M |
| GW-11 | the same: `unknown` distinct from empty at every read | a type with zero rows; the same type unreadable | read both | the two reads are equal | M |
| GW-12 | the same: only a sign-off closes a required step; no principal signs for another | `T-at(feature, arch)` | the `pr_review` owner writes a sign-off on `arch` | it lands | M |
| GW-13 | the same: only the operator may waive; one `waived` per step; scoped to one batch's unsigned required steps | a batch with two unsigned required steps; a second batch | the operator waives; a step owner writes `waived`; the operator waives a signed step | the step owner's lands; fewer than two `waived` sign-offs each naming its step and reason; a batch-level flag; the second batch changes; the signed step's verdict changes | M |
| GW-14 | the same: `applies_when` has three values; unevaluable opens; an optional step's condition reads what exists when it would open | `copy` at `legal` with an evaluable condition true, false, and unreadable; a declaration whose optional step names in its condition an artifact type no earlier step's `reads_to_close` names | the step would open; declare | false does not record inapplicable with the condition; unreadable does not open; inapplicable reads as signed anywhere; the declaration lands (U-26 closed) | M |
| GW-15 | the same: the condition is declared on the workflow and never read from the artifact | `X(github)` PR carrying a label, a title token, a body line, a checkbox naming `legal` | the step would open | any of them seats or unseats the step | M |
| GW-16 | the same: evaluated once at open, recorded with the head | `legal` ruled inapplicable at head h1; the head moves to h2 that would seat it | read | the recorded evaluation lacks h1; or the step is silently re-evaluated without the invalidation the pinning rule states | M |
| GW-17 | the same: no step is closed by elapsed time | a required step open, `CLK` past every declared interval | wait | the step reads anything but open; a checkpoint `unclaimed_step` carries a verdict | M |
| GW-18 | the same: scope amendment versus scope creep | a batch whose `impl` exceeds a task's `acceptance_criteria[]` | correct the criteria as a step owner of the batch, citing a finding in the key; as another principal; citing none | the second or third lands; the first is refused; the shipped change exceeds the criteria with no correction on the record | M for the record; R for whether a change exceeds the criteria (U-8 closed) |
| GW-19 | `gates_and_workflows.md#findings-verdicts-and-what-a-blocking-finding-obliges`: findings bind; a contradictory verdict is refused at submission | a sign-off carrying a `finding` of severity `blocking` under `signed` | write it | it lands; the step closes | M (U-6 closed) |
| GW-20 | the same: three verdict values | a sign-off with `APPROVE` as its verdict | write it | it lands | M |
| GW-21 | the same: a verdict is terminal, never revised | a signed step | correct the verdict in place; write a new sign-off | the correction lands; the new sign-off does not stand as latest per owner per head; the earlier is not readable | M |
| GW-22 | the same: a verdict carries no condition | a sign-off and a finding each written with a `conditions` field; a blocking finding whose remedy is neither a task `REFERS_TO` it nor a criteria correction | write; read | the field reads as declared on either (it lands in `raw_fragments`); the remedy exists on the record in no readable form | M for shape; R for a condition written in prose (U-5 closed) |
| GW-23 | the same: routing a remedy never transfers the verdict; a decision-kind finding is not routable | a blocking `finding` of kind `decision_or_attestation` | a task is created from it; an implementer's artifact arrives | a task exists `REFERS_TO` the decision-kind finding; the step reads signed on the implementer's work with no owner sign-off | M (U-6 closed) |
| GW-24 | the same: a blocking verdict names its evidence | a blocking `finding` with no `evidence`; one naming a mechanism and the result it read | write | the first lands; the second is refused | M (U-6 closed) |
| GW-25 | `gates_and_workflows.md#a-finding-is-one-off-or-standing-and-a-standing-one-obliges-a-change-to-what-produced-it`: a standing finding produces a proposed change with provenance | a `finding` with scope `workflow` recorded | read tasks | no task `REFERS_TO` the finding; the batch's work corrected and nothing else | M (U-6 closed) |
| GW-25a | the same, ruling 17: institutionalizing is a workflow, and the raising batch does not wait | as GW-25 | read the raising batch and the created task | a `DEPENDS_ON` edge from the raising batch to the institutionalization task; the raising batch open after its own steps are signed; the task with no intake batch | M |
| GW-26 | the same: the operator's input is a finding on both axes | the operator records input at `consent` | read | a second entity type or queue carries it | M |
| GW-27 | the same: the classifier has five values — the batch (one-off), the step, the workflow, the agent, and `unknown` — and `unknown` raises `undetermined_scope`, never a guess and never the one-off default | a finding whose scope cannot be determined; one that is plainly one-off | read checkpoints and the finding's classification | no checkpoint with that reason naming the candidate scopes, the batch alone among them; the undeterminable one classified one-off; a proposed change with a chosen scope for it; a second reason class for the one-off-or-standing axis | M |
| GW-28 | the same: where the scope lands | a `finding` with scope `step`; the task it produces writes to an `agent`; a finding with scope `unknown` | read | the write's type differs from the finding's scope with no correction to the scope; a task produced from the `unknown` finding before its checkpoint is resolved | M for attribution; R for whether the scope is right (U-7 closed) |
| GW-29 | the same: a proposed change is a proposal until the gate lets it through, never a self-mutation | an agent's own standing finding on itself | read the agent entity | a write to `agent` or `agent_policy` by that agent's credential with no permitted action behind it | M (decision 56, ruled 2026-09-06: a write by any credential but the engine's is refused at admission, and the engine's names the permitted action it is taken under) |
| GW-29a | the same: a standing finding whose content is operator-specific lands in a `task_policy`, never in a public prompt or an `agent_policy`; the prompt gains at most the instruction to read the type; the `task_policy` write is an internal operational write | a standing finding carrying an operator's name, figure, or locale | read what the institutionalization task writes | an operator-identifying string in `prompt_markdown` or in an `agent_policy`; no `task_policy` written; the `task_policy` write held at the gate as governance | M |
| GW-30 | `gates_and_workflows.md#one-step-set-defined-once-tested-for-parity` | `B0` | read the step set from the declaration and from every code copy the reference implementation admits | a copy differs; a copy has no parity test; a base list in code that the declaration adds to | M (X-10 closed) |
| GW-31 | `gates_and_workflows.md#sequencing-is-data-successors-and-the-chain`: the last step is singular; one successor, or none where `none_permitted`; parallel successors forbidden; `successors` naming intake is a declaration error | declarations with a parallel last step; a closing sign-off naming two; a `successors` list naming intake; a closing sign-off naming none under a declaration that does not permit it (U-22 closed) | declare; sign | any lands | M |
| GW-32 | the same: the chain is derived, never stored | a task through three batches | census; read the chain | an entity above the batches holding a sequence; a task field listing batches | M |
| GW-33 | `gates_and_workflows.md#two-questions-who-may-claim-a-step-and-whether-an-action-may-be-taken`: governance writes are actions | as WM-22 | — | as WM-22 | M (decision 56, ruled 2026-09-06: a write by any credential but the engine's is refused at admission, and the engine's names the permitted action it is taken under) |
| GW-33a | `gates_and_workflows.md#where-the-enforcement-point-for-a-governance-write-sits`: the sole-writer grant | as WM-22, and a step owner whose grant is written to name a governance type | the step owner writes a `workflow`; the operator's bare credential writes an `action_policy` after bootstrap; census of the grants | either write lands; a grant naming a governance type for any credential but the engine's admitted; a governance write with no `action` of the type's class `PRODUCES` from a task in a live batch that the engine's credential made | M (decision 56, ruled 2026-09-06 — WM-22, GW-29, GW-33, WM-40b, AD-27, and MG-4 are the rows it turned from audit to refusal) |
| GW-34 | the same: lossy record mutations are actions; the count is declared | `B0+pol(lossy count: 10)` | a merge of two entities; a correction touching 11 entities; one touching 9 | the merge or the 11 proceed with no action and no checkpoint `lossy_record_mutation`; the 9 raises one | M |
| GW-35 | the same: the declared step owners, with the grants in force, decide who claims a step | agent A not the `arch` owner | A claims `arch` | the lease lands | M |
| GW-36 | `gates_and_workflows.md#actions-are-entities-only-actions-are-taken`: an action is created when known, gated when taken | a task declared `docs`; part-way through, it needs a send | read | no `action` of class `send_external_comms` `PRODUCES` from the task; the task's declaration amended; the gate consulted at creation rather than at taking | M |
| GW-37 | the same: the dedup key lives on the action | any action | read | `dedup_key` absent, or on the artifact or the task instead | M |
| GW-38 | `gates_and_workflows.md#the-action-gate-is-pr-independent` | an action with no artifact, no issue, no repository | evaluate the gate | the gate requires any of them; the checkpoint keys on anything but the action and its task | M |
| GW-39 | the same: do not build a second gate | `B0` | census | a second decision-carrying held-state type | M |
| GW-40 | `gates_and_workflows.md#confidence-and-three-blast-tiers`: the order is load-bearing | `B0+pol(low: [x], high: [y], operator_only: [z])` | actions of class z, of an unlisted class, of absent class, of x below threshold, of y with and without a graduated series, of z with a graduated series | z taken; unlisted taken or defaulted; absent not defaulted; x below threshold taken; y taken ungraduated or held graduated; z taken graduated | M |
| GW-41 | the same: advisory and enforcing paths resolve identically | the same policy | evaluate both | they differ on any class | M |
| GW-42 | `gates_and_workflows.md#an-unreadable-workflow-is-unknown-and-unknown-holds` | `T-routed(feature)`, `RP` failing the workflow read | the engine would open a step | any step opens or is claimed; no checkpoint `unreadable_workflow`; more than one; an empty step tuple proceeds | M |
| GW-43 | the same: an unreadable issue, an unreadable CI state | `X(github)` failing the reads | read | anything but `unknown`; a step proceeds | M |
| GW-44 | `gates_and_workflows.md#non-code-deliverables-go-through-the-same-gate` | `T-at(social_content, post)`, `T-at(outreach, send)`, `T-at(payment, pay)` | the effect would be taken | any is taken with no `action` evaluated at the gate | M |
| GW-45 | `gates_and_workflows.md#external-systems-are-reached-only-through-adapters` | a full run, `X(*)` logging | — | any `X` call from a non-adapter credential | M |
| GW-46 | `gates_and_workflows.md#the-checkpoint`: subject exactly one, an action or a task | write checkpoints with no subject, two subjects, a step, a batch, an artifact | — | any lands | M (X-1) |
| GW-47 | the same: reason, needed input, options, awaited, resolver recorded | a checkpoint raised and resolved | read | any field absent; the resolver a bare status write | M |
| GW-48 | the same: deferral bounded; timeout terminal, never continues | a checkpoint deferred past its bound, `CLK` | wait | it is not terminal; the action is taken or the task re-claimed after timeout | M |
| GW-49 | the same: one queue, one protocol | a checkpoint on an action and one on a task | present and resolve both | a second presentation path or resolution protocol | M |
| GW-50 | the same, and `failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol`: a condition that stops a whole batch is raised on one of its tasks, never on the batch; one per stopped batch; it names the batch and the step | a batch with three tasks whose workflow is unreadable; one with a step nobody claimed past its interval | read checkpoints | a checkpoint whose `CHECKPOINTS` edge names the batch; more than one open with the same reason on the batch's tasks; `needed_input` naming neither the batch nor the step; the other tasks not readable as held through their `ADDRESSED_BY` edge | M (formerly P on X-1, closed) |
| GW-51 | `gates_and_workflows.md#whether-the-verdict-is-a-stored-field-or-a-read-over-the-findings-and-the-author` | — | — | closed: decision 32 ruled 2026-09-06 — the field stays as the sign-off's own projection of its findings and its author, reconciled at the write (`gates_and_workflows.md#whether-the-verdict-is-a-stored-field-or-a-read-over-the-findings-and-the-author`); GW-19, GW-20, and GW-21 stand as its rows, unchanged |
| GW-52 | `gates_and_workflows.md#what-a-step-leaves-at-close-what-it-produced-and-a-reference-to-what-it-read` (ruling 40): what a step produced is written as the entities it is; what it read is named on the sign-off (`REFERS_TO` → entity, `artifact_refs[]` with heads) and reproduced by an as-of read at `signed_at`; its reasoning is not written; `agent_session` gains no transcript | a step that read two record entities and one artifact; an observation on one of them corrected after `signed_at` | read the sign-off; as-of read at `signed_at` along ingestion time | a read entity absent from the sign-off's references; a copy of a read entity's state stored on the sign-off or the session; a transcript or reasoning field anywhere; the as-of read returning the corrected value | M |
| GW-53 | `gates_and_workflows.md#work-is-reviewed-on-the-record-and-a-channel-carries-only-what-awaits-the-operator-or-cannot-wait` (ruling 37): the operator's view is a read under the operator's grant; a channel carries a declared subset — a checkpoint awaiting the operator, the announcement path, a declared delivery — and completed work is not carried unless the binding or a `deliver` step says so | a batch closing; a finding filed; a checkpoint raised; a `deliver` step naming a channel; `channel_config` declaring one reason class | `X(chat)`, `RP` | the closing or the finding reaches the channel; the checkpoint or the declared delivery does not; a message of an undeclared class sent; a dashboard read under any credential but the operator's; a stored picture of the queue beside the record | M |
| GW-54 | `gates_and_workflows.md#closed-work-is-reviewed-on-the-record-and-redone-through-intake-never-reopened` (ruling 38): a closed batch is never reopened; the input is a finding on it; the redo is a new task through intake referring to the closed batch's artifacts, with provenance to the finding | a closed batch; the operator records a finding on it | read | a terminal task returned to open; a sign-off written into the closed batch; a batch reopened or a step of it re-claimed; the redo task with no `REFERS_TO` to the closed batch's artifacts or no provenance to the finding; the redo entering any workflow but through intake | M |

### `failure_posture.md`

| # | Rule | Setup | Action | Red when | Class |
|---|---|---|---|---|---|
| FP-1 | `failure_posture.md#the-decision` | `T1`, `RP` failing all reads | a runner would claim | any write to the record; a lease; a gate decision; a step opened | M |
| FP-2 | `failure_posture.md#the-rules` rule 1: halt work, never stop observing; capture to local disk | as FP-1 | — | no capture on local disk; a capture written to the record | M |
| FP-3 | rule 2: announce entering and leaving, aggregated per window | as FP-1, then restore; three blocked claims in one window | — | `CH` shows no announcement on entering; none on leaving; three messages for one window; the window not the one `channel_config` declares | M (U-19 closed) |
| FP-4 | rule 3: the probe is a real read at claim, never the health endpoint | `RP` with health green and reads hanging | a runner would claim | a lease exists; `RP` shows a health request and no read of the task before the claim; a probe per operation rather than per claim | M |
| FP-5 | rule 4: a mid-task write failure leaves the prior state; the lease lapses; no verdict posted elsewhere | a step claimed; `RP` failing writes; `X(github)` logging | the owner's sign-off write fails | the task's status changed; a comment or review on `X(github)` carrying the verdict; a re-claim replays the verdict rather than re-deriving it | M |
| FP-6 | rule 5: deferral is bounded; exhaustion escalates `rounds_exhausted` | a step whose `on_fail` loop has a declared cap; `CLK` | fail the step past the cap | no checkpoint `rounds_exhausted`; the loop continues; a checkpoint raised under a step declaring no `rounds_cap` | M (`rounds_cap` on the step; U-20 closed) |
| FP-7 | rule 6: every write is read back | as PR-2 | — | as PR-2 | M |
| FP-8 | rule 7: unknown stays distinct | as PR-7 | — | as PR-7 | M |
| FP-9 | rule 8: a failure that left no effect is retried with backoff; a stated retry time is honoured; per system | `X(github)` returning a transport reset, then a rate limit stating a reset time; two steps waiting | — | a retry before the stated time; two retry schedules for one system; the failure recorded as a task failure | M |
| FP-10 | `failure_posture.md#the-operator-invoked-halt-and-what-undoes-an-action-already-taken`: the halt is confirmed by a read-back of no live leases | a live lease; the operator halts | read the swarm's report | it reports halted while a lease reads `held`; it reports halted on the command's return | M |
| FP-11 | the same: every class names its recovery | `B0+pol(low: [x])` with no `recoveries` entry for x; then with `forward_only` | write the policy | the first lands | M (U-10 closed) |
| FP-12 | the same: a recovery is an action through the gate, under its own class | a merge confirmed; a revert | — | the revert reaches `X(github)` with no action of class `revert_merge` evaluated at the gate | M |
| FP-13 | the same: the restore obligation | a binding declaring two `recovery_paths[]` with a cadence and one with none, `CLK` | exercise one path; let another pass its cadence | the exercise is not an observation on the binding carrying what it read back; the overdue path is not readable as overdue; the path with no cadence reads as exercised | M (U-11 closed) |
| FP-14 | the same: whatever detects does not remediate | the watchdog, an external prober, a health check; `RP` per credential | run | any of their credentials writes to a lease, a task, or an infrastructure surface; any restart, scale, or rollback from them | M |
| FP-15 | `failure_posture.md#repeated-lapse-raises-a-checkpoint` | as WM-17, the cap being `lapse_cap` on the policy; a policy declaring none | — | as WM-17; during a halt the count does not accrue; the checkpoint is not written when the record returns; a checkpoint raised under the policy declaring no cap (U-20 closed) | M |
| FP-16 | `failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol`: reason classes | raise each | read | a reason outside the one enumeration in `failure_posture.md#checkpoints-on-tasks-one-queue-one-protocol` and the policy's declared set | M |
| FP-17 | the same: escalation reorders, never signs | a checkpoint on an unclaimed step | read the step | any sign-off attributed to the escalating mechanism; the step closed | M |
| FP-18 | the same: `unclaimed_step` after the declared interval, subject one task of the step's batch, naming the batch, the step, the role, and the duration; undeclared raises none | a step open past its declared interval; another with no interval declared, `CLK` | wait | the first raises none, or one naming fewer than the four, or one whose subject is the batch; the second raises one | M |
| FP-19 | the same: a role resolving to nobody is caught at declaration | a declaration naming a role the roster lacks | declare | no checkpoint `unspawnable_assignee` at declaration | M |
| FP-20 | `failure_posture.md#what-a-checkpoint-does-not-absorb`: the halt is not a checkpoint | as FP-1 | — | a checkpoint written during the halt | M |
| FP-21 | the same: operator-only tasks are not checkpoints | as WM-19 | — | as WM-19 | M |
| FP-22 | `failure_posture.md#refuse-resume-by-replay-where-actions-are-consent-gated` | a task with two actions, one confirmed, interrupted | re-claim | the confirmed action's effect reaches `X` again; the runner restarts from the first instruction | M |
| FP-23 | rule 2: the announcement path itself unreachable — the capture is the announcement of last resort, and the path shows no gap on return | as FP-1 with `CH` down for two windows, then restored | — | the local capture lacks a window's aggregate; on `CH`'s return the two windows are not announced, or are announced without their original times; a second channel is used | M (U-18 closed) |
| FP-24 | the `unclaimed_step` checkpoint's subject | as GW-50 | — | as GW-50 | M (X-1 closed) |

### `authority_model.md`

| # | Rule | Setup | Action | Red when | Class |
|---|---|---|---|---|---|
| AU-1 | `authority_model.md#the-tuple`: `Indeterminate` is deny; zero grants deny; a raising check denies | an agent with no grant; `RP` failing the grant read; a checker mutated to raise | evaluate | any permit | M |
| AU-2 | the same: time is read by the checker | a grant with `expires_at` in the past | evaluate | permit | M |
| AU-3 | `authority_model.md#principals`: a credential is a binding, never the principal; a magic value compared as `"operator"` | a write presenting a bare `user_id`; a login string | read attribution | it resolves to the operator by comparison rather than through a binding; an unbound one resolves to anyone | M |
| AU-4 | the same: `operator_profile` carries no authority edge | `B0` | write an `ownership_grant` to the profile | it lands | M |
| AU-5 | the same: a write whose only identity is `user_id` on a shared instance is unattributed | a write with no binding | read | attributed to anyone | M |
| AU-6 | the same: tenant | — | — | P (`multi_tenant.md` section 7) |
| AU-7 | the same: at most one `ownership_grant` per type | a type | write a second | it lands | M |
| AU-8 | `authority_model.md#grants`: a failed load is a stub nobody starts a runner from; no wildcard from a degraded read | `RP` failing the agent load | a runner would start | it starts; the allowlist reads `*` | M |
| AU-9 | the same: a denial precedes the effect, is structured, raises `capability_denied`, and is a request never a grant | an agent whose grant excludes a capability its step needs; `X` logging | the step runs | `X` receives the call; no checkpoint naming principal, capability, and step; the checkpoint's resolution writes a grant by itself; a `dropped` disposition for the untaken operation | M (X-9 closed) |
| AU-10 | the same: the denied principal does not route around | as AU-9 | — | another principal's sign-off on the step; a verdict on `X`; a call under another credential | M |
| AU-11 | the same: custody by revocability; no credential in the process environment; resolved once per invocation | a rail action; the runner's process | inspect the environment; retry the action | a non-revocable credential in a resident process's environment; any credential in the environment; a retry presenting a different credential in `X` | M (process inspection is the harness's; the design's rule is about processes it does not own) |
| AU-12 | the same: rotation is staged | rotate an agent's credential | — | any moment with zero matching grants; the old retired before admissions arrive on the new | M |
| AU-13 | the same: the grant is read at every enforcement point; revocation's reach | revoke a credential | the next check | permit; `RP` shows no grant read at the check | M |
| AU-14 | `authority_model.md#attribution` | every write in a full run | `RP` | a write with no agent and no principal | M |
| AU-15 | `authority_model.md#delegation`: attenuation; A-for-B; the chain derived; the hardest chain reconstructible | A delegates to X (scoped); X assigns; Y claims; Y's action needs B's approval | — | X acts beyond A's scope; a write recorded as B; a stored chain; any hop unreconstructible | M |
| AU-16 | `authority_model.md#approval`: yes/no/veto by a required principal; authorized against required approvers; timeout terminal; silence never accepts; no cross-principal auto-approve; notification via roster and channel config | a checkpoint awaiting P; Q resolves; nobody resolves past the bound | — | Q's resolution lands; the action is taken with no resolution; the timeout continues; `CH` shows one address for the whole swarm | M |
| AU-17 | `authority_model.md#the-raiser-of-a-checkpoint-does-not-resolve-it-and-the-operators-self-resolution-is-marked`: the raiser does not resolve; the operator's self-resolution is marked | a checkpoint raised by P awaiting P and Q; one raised by the operator awaiting the operator; one raised by an agent bound to the operator, awaiting the operator | P resolves the first; the operator resolves the second with the `self_resolved` mark, then a like one without it; the operator resolves the third without the mark | P's resolution lands; the unmarked self-resolution lands; the third lands unmarked (the agent counts as the operator under 48); a resolution by Q carrying the mark admitted | M (decision 47, ruled 2026-09-06, with 43) |
| AU-18 | `authority_model.md#structural-checks-quorum-and-separation-of-duties`: payment's disjoint verifier | `T-at(payment, verify)` | the payer signs `verify` | it lands | M |
| AU-19 | the same: the counting rule; count and disjointness over the checkpoint's principal edges; the thresholds on the `action_policy` per class | a checkpoint awaiting a quorum of two under a policy declaring `quorum` for the class, resolved by two agents bound to one principal; a class with no `quorum` value awaiting three, two resolving; a class naming a `disjoint_roles[]` pair, both roles resolving to one principal's agents | read | the two agents' resolutions count as two interests; the class with no value resolves on two of three; the pair passes; the check reads anything but `AWAITS`, `RESOLVED_BY`, and `RAISED_BY`; a vote, tally, or approval-set type in the census | M (48, 49, and 50 ruled 2026-09-06 — `authority_model.md#structural-checks-quorum-and-separation-of-duties`; which checks a class carries is read from the `action_policy` under test, and the class with no value is the row's fail-closed case) |
| AU-20 | `authority_model.md#initiative-proposal-reprioritization`: initiative approval is the checkpoint; what stops is a task, confirmed by the owner seat through the checkpoint and read back, proposing a grant capability; a budget attenuates, consumption is derived, and what is metered is per class on the policy; credit is a read model | an initiative entering intake as a task, `REFERS_TO` a task it would stop whose `ownership_grant` names S; a principal with no initiative-class capability; a delegate whose `scope` carries a budget wider than its delegator's; a class with no `metered_resources[]` under a grant carrying a budget term; a full run | read; the checkpoint resolved without S; the ungranted principal creates an initiative-class task; census | an `initiative`, `proposal`, or `approval` type beside the task and the checkpoint; the acceptance recorded anywhere but a checkpoint's resolution; the resolution landing with S neither awaited nor resolving; the stop taken as made with no read-back of the closing sign-off or the priority correction; the ungranted initiative landing; the wider budget admitted; a stored balance or consumed amount; the unmetered class's action refused or counted on budget; a `credit` type or a stored credit field | M (51 to 54 ruled 2026-09-06 — `authority_model.md#initiative-proposal-reprioritization`) |
| AU-21 | `authority_model.md#grants` (ruling 41): write admission per entity type is default-deny; the grant is the allowlist, read at every enforcement point; a capability naming every type is the fail-open shape; attribution is required besides and prevents nothing | an agent whose grant names `contact` and not `payment_profile`; a grant carrying a wildcard over types | write each type; register the wildcard grant | the `payment_profile` write lands; the wildcard grant is read as in force for a human or admits a type the agent's definition does not name; a write admitted on attribution alone | M |

### `data_model.md`

| # | Rule | Setup | Action | Red when | Class |
|---|---|---|---|---|---|
| DM-1 | `data_model.md#concepts`: every type, field, and edge the table names | `B0` | census | a type absent; a "deliberately not a field" present | M (X-3 and X-11 closed) |
| DM-2 | `data_model.md#record-conventions`: observations are the history; no parallel log | a task through a life | census | a transition or assignment log type | M |
| DM-3 | the same: corrections re-read and merge | two concurrent corrections to one map field | read | one entry lost | M |
| DM-4 | the same: idempotency keys; mismatch refused | a write retried with its key; a different write with the same key | — | the retry lands twice; the mismatch lands | M |
| DM-5 | the same: read-back after every decision write | as PR-2 | — | as PR-2 | M |
| DM-6 | the same: a sign-off is pinned; a later head does not invalidate automatically; staleness is derived | a signed step; the head moves | read | `artifact_refs[]` lacks the pinned state the kind names — the head, the message, the message set with coverage, the dated fact, the declaration, the rail state; the sign-off changes; a stored stale flag | M for every kind (U-21 closed) |
| DM-7 | the same: tolerant readers, canonical writers | rows under two spellings | read; write | the reader misses one; a write uses the old spelling | M |
| DM-8 | the same: a registered type declares `reducer_config` | `B0` | register a type without one | it lands | M |
| DM-9 | the same: schema versions; a sign-off pins the agent version | a sign-off | read | `agent_version` absent | M |
| DM-10 | the same: `raw_fragments` for undeclared fields; the read-back asserts the declared field | a write with an undeclared field | read back | the field reads as declared; the writer treats the 200 as landing | M |
| DM-11 | the same: adapter writes keyed on the delivery id, provenance naming adapter and system | `X(github)` redelivers | read | two observations; provenance lacking either | M |
| DM-12 | the same: sourcing rides on provenance; coverage on every adapter observation; freshness never a field | an adapter observation | read | no source, no sourced time, no coverage; a freshness field | M |
| DM-13 | the same: edges carry timestamps and explicit ends | a returned lease | read the edge | no `returned_at`; a status the edge was transitioned to | M |
| DM-14 | the same: one `ownership_grant` per type | as AU-7 | — | — | M |
| DM-15 | the same: tests never register into the shared registry | the suite | — | the isolation layers fail their planted positives | M |
| DM-16 | the same: a schema version does not migrate `raw_fragments` | a field declared after a write carried it | read back the old entity | it reads the field | M |
| DM-17 | the same: key on what the source says, never the clock | derive every key twice | — | the two differ; a key with a wall-clock component; a key that refuses a value the field held before | M |
| DM-18 | the same: merging is a write carrying decisions; edges repointed | merge two entities with edges | read | an edge lost; a field not read back against both sources | M |
| DM-19 | the registry-closure form of principle 6 | `B0` | census | a second type for a listed singleton | M |
| DM-20 | `data_model.md#retrieval-contract`: the adapter never reads a `workflow`; the engine never reads an external system; the daemon reads no step state; the review step owner reads no host review state as sign-off | a full run, `RP` and `X` per credential | — | any read outside the actor's column | M |
| DM-21 | `data_model.md#write-contract`: each actor's writes ⊆ its column | a full run, `RP` per credential | — | any write outside the column (X-7 closed: the adapter's carried resolution and sign-off are in its column, A-for-B) | M |
| DM-22 | the same: the daemon reads back each task it created, and writes one observation per window | a daemon poll that finds nothing; one that finds work; `CLK` past a window | `RP` | a create with no retrieve after it; a window with no observation on the daemon's `agent_session`; an observation without the poll's coverage or the dispositions counted (U-32 closed) | M |
| DM-23 | the same: bounded retrieval before creating | any create | `RP` | a create with no retrieve naming the type and identifying values before it | M |
| DM-24 | the same: context entity types from the agent's own definition; grant outer, definition inner | an agent whose grant admits a type its definition lacks | it reads that type | the read lands | M |
| DM-25 | `data_model.md#rendering` | `B0`, the table | run the renderer's `--check` | a table on disk differs from the registry and the check passes | M (contract; existence is `status.md`'s) |
| DM-26 | `data_model.md#concepts` `intake_rule`: subject types never a work-model record type; change kinds; the two predicates; the task text; ceiling and window; `ended_at`; no edge; deliberately no cursor, fired count, successor, workflow, step, action class, or `assigned_to` | `B0` | register; write a rule naming `task`; census after a run | the `task`-subject rule lands (P on decision 36 for the other branch); a `last_evaluated` or fired-count field; a successor or workflow on the rule | M |
| DM-27 | `data_model.md#concepts` `finding`: severity, kind, scope, evidence, text; `PART_OF` the sign-off, `REFERS_TO` the batch; a hold's finding with no sign-off; a task `REFERS_TO` the finding it was produced from; deliberately no verdict, no condition, no discharged flag | `B0`, `T-at(feature, pr_review)` | write a blocking finding with no evidence; a blocking one with kind absent; a hold finding, then a sign-off on the same step; a finding carrying a `discharged` field | either of the first two lands; the hold finding is not readable with no sign-off, or its renewals are not observations on it; the `discharged` field reads as declared | M (U-6 and X-14 closed) |

### `adapters.md`

| # | Rule | Setup | Action | Red when | Class |
|---|---|---|---|---|---|
| AD-1 | `adapters.md#the-workflow-engine-never-reads-an-external-system-it-reads-the-record` | as GW-45 | — | as GW-45; a step's state changes with no write to the record between the external change and the step | M |
| AD-2 | `adapters.md#no-external-event-advances-a-step-by-itself`: the four outcomes and nothing else | per adapter, every delivery kind from `cred-other` and `cred-none` | deliver | any step opened, claimed, or closed; a successor named; a batch advanced; a fifth outcome type | M |
| AD-3 | the same: a verdict from `cred-owner` on its open step is a sign-off; from `cred-other` or `cred-none` an observation; a CI result is never a sign-off | per adapter, a verdict-shaped delivery from each credential; a check result | deliver | the wrong outcome for any of the four; a check closes a step | M |
| AD-4 | the same: the adapter never invents a binding or resolves an unrecognized credential to the operator | `cred-none` delivers an approval | deliver | a resolution or sign-off attributed to the operator | M |
| AD-5 | `adapters.md#the-adapter-runs-before-and-after-a-step-never-during-it` | as GW-8 | — | as GW-8 | M |
| AD-6 | the same: the declaration is what the adapter is asked for | a step declaring types A and B | hydrate | `X` logs a read for a type not declared | M |
| AD-7 | the same: an unfulfillable read fails the phase; hold, bounded, escalate; rule 8 retry | as GW-9, FP-9 | — | as those; an empty result standing in for the failure | M |
| AD-8 | `adapters.md#where-inbound-delivery-lands-the-adapter-verifies-and-identifies-it-and-the-records-own-subscriptions-are-not-it`: a record subscription is never an inbound receiver | a subscription over entity changes | an external event occurs with no adapter | any write about the external system | M |
| AD-8a | the same, ruling 16: the adapter owns verification, the delivery id, and the acknowledgement; a shared listener verifies, deduplicates, acknowledges, and parses nothing | a shared listener in front of two adapters; a delivery with a bad signature; a redelivery; `RP` failing writes | deliver | the bad signature reaches the record as anything but the adapter's `dropped`; the listener's log shows a verification, a dedup, or a parse; the listener answers the external system before the adapter's read-back; the listener returns success during the write failure | M |
| AD-9 | `adapters.md#what-the-adapter-does-with-every-event`: identity | as AD-3 | — | — | M |
| AD-10 | the same: linkage by `system` and `external_id`; a new-record event on an untracked record is a task; any other event on it is dropped; the adapter never attaches to a batch | an untracked record: a new-record event, then an edit event | deliver | no task with the artifact `REFERS_TO`; the edit yields anything but `dropped` with reason; an `ADDRESSED_BY` written by the adapter | M |
| AD-11 | the same: dedup inbound on the delivery id; outbound on `dedup_key`; a confirmed key refused | redeliver; re-take a confirmed action | — | two writes; a second effect in `X` | M |
| AD-12 | the same: unknown; no silent branch; every delivery has a disposition; drops counted and announced; the reason back to the person | a malformed payload; an unmapped event type; a command | deliver | an outcome coerced; a delivery with no disposition; `CH` shows no aggregated count; no window observation on the adapter's `agent_session` carrying the drops by reason; `X` shows no observation to the requester where the drop concerned their request | M (U-19 closed) |
| AD-13 | the same: provenance and read-back before acknowledging; during a halt nothing is acknowledged | a decision-carrying delivery; the same during `RP` failure | deliver | `X` receives the acknowledgement before `RP` shows the read-back; any acknowledgement during the halt | M |
| AD-14 | the same: sourcing, coverage, freshness derived | as DM-12 | — | — | M |
| AD-15 | `adapters.md#an-artifact-exists-only-once-its-external-record-does-and-the-interval-before-that-belongs-to-the-action` | a send whose confirmation is lost | read | an artifact with null `external_id`; the action reads anything but `unknown`; on re-claim the adapter submits without reading `X` for the key | M |
| AD-16 | `adapters.md#what-the-record-supplies-and-what-an-adapter-therefore-never-builds`: as-of reads on two axes | an observation about t1 arriving at t3 | read as of t2 on each axis | event time excludes it; ingestion time includes it | M |
| AD-17 | the same: the adapter keeps no history | the adapter's state surface | inspect | a sync log, cursor table, or artifact cache; behaviour that depends on it | M |
| AD-18 | `adapters.md#outbound-steps-produce-actions-adapters-take-them`: on permit perform, read back, confirm; on checkpoint nothing; an unlisted class is `NEVER` | a permitted action; a held one; one of an unlisted class | — | no `taken_at`/`result_ref` from a read-back; `X` receives the held one; the unlisted one is taken | M |
| AD-19 | the same: a recovery is an action, forward-only where the system offers no reversal | a `publish` recovery | — | the published surface deleted; no `deprecate_publication` action | M |
| AD-20 | `adapters.md#what-an-adapter-never-does` | a full run | `RP`, `X` | any of the seven | M |
| AD-21 | `adapters.md#the-admission-contract` obligation 1: coverage is a counter | a sixth adapter's fake; an event outside its mapping | deliver | no `dropped` with reason `unmapped`, counted | M |
| AD-22 | obligation 2: identity, with the negative test that goes red when the fallthrough returns | the sixth adapter; its identity rule mutated to resolve unknown credentials to the operator | deliver a verdict from `cred-none` | the mutant does not turn AD-4 red | M (meta) |
| AD-23 | obligation 3: dedup both ways | as AD-11 | — | — | M |
| AD-24 | obligation 4: no maintained freshness state | as AD-17 | — | — | M |
| AD-25 | obligation 5: read-back before acknowledge; unacknowledged during a halt | as AD-13 | — | — | M |
| AD-26 | obligation 6: every outbound class listed, or `NEVER` | the sixth adapter's class unlisted | act | taken | M |
| AD-27 | `adapters.md#who-may-admit-an-adapter-and-through-what`: admission is a task through a workflow; the grant is a governance write; the class write is governance | admit the sixth adapter | read | a grant with no permitted action behind it; no intake batch for the admission task; the adapter takes an action before its class is listed | M (decision 56, ruled 2026-09-06: a write by any credential but the engine's is refused at admission, and the engine's names the permitted action it is taken under) |
| AD-28 | the same: the arch review step checks the six obligations before the grant | as AD-27 | — | the grant permitted with no `arch` sign-off citing the adapter's document | M |
| AD-29 | `adapters.md#when-an-adapter-is-wrong`: a wrong sign-off is findable by provenance; corrected by a new sign-off, never deleted; withdrawal is revocation | a mis-mapped sign-off | query provenance by adapter and window; revoke | not found; the sign-off deleted; a capability survives revocation | M |
| AD-30 | `adapters.md#the-adapter-and-the-engine-are-two-roles`: they meet only in the record | as AD-1 | — | — | M |
| AD-31 | `adapters.md#the-obligations-are-the-five-rules-restated-as-failures`: the contract adds no rule | this matrix | map AD-21 to AD-26 onto AD-9 to AD-14 and AD-18 | an obligation row with no five-rules row behind it, or a five-rules row that no obligation makes fail | M (a row over this document; the mapping is the admission axis below) |
| AD-32 | `adapters.md#what-an-adapters-document-must-contain`: eight required parts; a row marked unhandled is a named gap | the sixth adapter's document | lint it for the eight parts and the three-way marking on every inbound row | a part absent; an inbound row with no status; an unhandled row with no `status.md` row | M for structure (the lint is a contract, as the rule-coverage check is); R for whether the parts say anything true |
| AD-33 | `adapters.md#degrees-of-trust-the-design-distinguishes-and-grants-already-express-it`: no trust level on the adapter; scope is the grant, risk is the class | the registered `agent` type; two adapters' grants | census; read | a trust, tier, or risk field on `agent`; a read-only adapter whose grant confers a write; an action class resolved from the adapter's identity rather than from the policy | M |
| AD-34 | `adapters.md#the-relationship-to-decision-15-which-this-section-did-not-resolve` and `adapters.md#the-adapter-and-the-engine-are-two-roles` (ruling 15): the obligations hold wherever the code lives; the roles meet only in the record | as AD-1, AD-30 | — | as those; no row in this matrix names a repository | M (ruling 15 changes no observable, which is what the ruling says of itself) |
| AD-35 | `adapters.md#whether-one-binding-type-or-two-names-an-external-systems-instance` | — | — | closed: decision 35 ruled 2026-09-06 as settled by this document's reading — one binding type, routing a field of it (`adapters.md#whether-one-binding-type-or-two-names-an-external-systems-instance`); TG-6, CA-6, and PY-3a are its rows and are unchanged; the name and the substitution are the condensation pass's |
| AD-36 | `adapters.md#github`, `adapters.md#gmail`, `adapters.md#telegram`, `adapters.md#calendar`, `adapters.md#payments`: each section points at its document and names the two rules its system erodes hardest; an artifact with no batch never receives retroactive step state | `X(github)` delivers a pull request opened before any task exists | read the artifact; route the task it yields | any step state, `not_required`, `not_applicable`, or clear written on the artifact; the batch that later addresses it skipping a step on the strength of the artifact's age; its `impl` sign-off not citing the existing pull request in `artifact_refs[]` | M |
| AD-37 | `adapters.md#continual-inbound-is-the-inbound-side-and-an-intake-rule-evaluates-downstream-of-it`: a tracked artifact is kept current by every delivered event whether or not a step waits; the scope is the inbound table intersected with the binding; freshness is never a policy; the evaluator sits downstream of the adapter, fed by the record's subscriptions; outcome 4 is not suppressible by a rule | `X(mail)` delivers on a tracked thread with no step waiting, and on a mailbox the binding does not name; an untracked new message with a rule that would exclude it | deliver | the tracked thread's observation absent; the unbound mailbox's message anything but `dropped`, reason `untracked_mailbox`; a scheduled read the binding did not set and no step declared; the adapter reading an `intake_rule`; the untracked message not reaching intake | M |
| AD-38 | `adapters.md#what-the-record-supplies-and-what-an-adapter-therefore-never-builds`: the source is kept, not only named — the raw delivery or page, identified by the delivery id or the read's coverage, with the observations interpreted from it linking back; a corrected mapping is re-applied to the source, producing observations with the source's event time and a new ingestion time; no payload cache in the adapter | a delivery; its mapping corrected afterward | read provenance; re-interpret; inspect the adapter's state | an observation with no source it links to; the source not the raw thing read; a re-interpretation that asks `X` again or that overwrites the first reading; a payload table beside the record | M |

### `workflows.md`

The step tables are render targets of the `workflow` entities; the suite reads the declarations and checks
each section's stated conditions against them. A close-on condition that is a judgement ("the full diff is
read and judged") is **R**: the suite checks that the sign-off exists, from the right owner, at the right
step, with its fields — never that the judgement was sound.

| # | Rule | Setup | Action | Red when | Class |
|---|---|---|---|---|---|
| WF-1 | `workflows.md#how-to-read-a-workflow-section`: a failing verdict does not advance; `on_fail` reopens; the cap escalates | a blocking verdict at `pr_review` | read | the batch advances; `impl` not open again; past the cap no `rounds_exhausted` | M |
| WF-2 | `workflows.md#roles-named-in-this-document`: every role resolves against the roster per project | `B0` | declare each | any role resolving to nobody without a checkpoint | M |
| WF-3 | `workflows.md#intake`: entry once, at creation; five steps; `classify` writes `action_type` from what the task does | `T1` | run intake | a second intake; `action_type` absent or set from the handling agent | M |
| WF-4 | `link` | as WF-25 | — | as WF-25 | M (U-17 closed by ruling 39; WF-25 is the row) |
| WF-5 | `dedupe` | two tasks alike | run | the duplicate not terminal; no `DUPLICATE_OF` edge to the original; the batch continues to a successor | M (U-23 closed) |
| WF-6 | `prioritize` from `priority_rubric`, never the classifier's sense | `B0` without `priority_rubric`; with it | run | without: the step opens; with: priority set by anything but the rubric | M |
| WF-7 | `route`: one successor, none, or operator-only; intake makes no action | run | read | two successors; an `action` from intake | M |
| WF-8 | `inherits` fast path | a child | run | `prioritize` or `route` skipped | M |
| WF-9 | `workflows.md#feature`: entry names a repository; `pm` states scope, evidence, basis; `ux`+`arch` parallel and joined; `impl` closes on a pull request artifact with checks passing; `pr_review`, `qa` judgements; `legal` optional by `applies_when`; `merge` is an action, read back, names the successor | `T-routed(feature)`, `X(github)` | run | `impl` signed with no artifact, with `checks` not `passing` at the pinned head, or with mergeability not read as true (X-12 closed); `merge` signed with no confirmed `merge_pr` action; `qa` and `legal` not joined | M for structure; R for judgements |
| WF-10 | feature fast paths: taken only where the project declares no workflow of that type | a project declaring `bug`; a bug-class task | route | the fast path taken | M |
| WF-11 | `workflows.md#bug` | `T-routed(bug)` | run | `ux` or `arch` opens; a second intake batch on the task, or the design work not a new task `REFERS_TO` this batch's artifacts (X-5 closed) | M |
| WF-12 | `workflows.md#security`: `release` always; no exploit detail on public artifacts | `T-routed(security)`, `X(github)` | close naming none; write a PR body | none lands (`none_permitted` false; U-22 closed); the body carries the fields the adapter withholds | M for the successor; R for detail |
| WF-13 | `workflows.md#copy`: `copy` before `impl`, against `brand_voice`; `ux` optional | `T-routed(copy)` | run | `impl` before `copy`; no `brand_voice` read in `RP`; `ux` never inapplicable, or its condition naming an artifact type no earlier step produces landing at declaration (U-26 closed) | M |
| WF-14 | `workflows.md#social-content`: `draft_lint` deterministic; `consent` carries the `publish` checkpoint; `approved` only where the gate would not checkpoint | `T-routed(social_content)`, `X(chat)` | run | the lint's sign-off differs from the script's result; the `publish` action created before lint passes; `approved` taken with a `HIGH` ungraduated class | M |
| WF-15 | `workflows.md#release`: `criteria` from `release_criteria`, unknown holds; `release` read back at terminal state; `verify_deployed` reads the deployed checkout | `T-routed(release)`, `X(github)`, a fake deploy target | run | `criteria` signed with the entity unreadable; `release` signed on the operation's return; `verify_deployed` signed with the target reporting another version | M |
| WF-16 | `workflows.md#outreach`: the draft is in the record; `review` before `consent`; the checkpoint carries the full draft; `send` read back by message id; a change after consent is a new draft; `follow_up` through the same gate | `T-routed(outreach)`, `X(mail)` | run; change the draft after consent | `send` confirmed from the call's return; the `dedup_key` unchanged after the change so no new checkpoint; a follow-up send with no action | M; R for the review's judgement |
| WF-17 | `workflows.md#payment`: a task naming payee or amount inline fails `classify`; `prepare` from the profile; `verify` disjoint; `consent` carries payee, amount, reference; `pay` keyed; `reconcile` reads terminal state, writes `transaction`, is the verifier's | `T-routed(payment)`, `X(rail)` | run | any: an inline payee passes `classify`; `verify` by the payer; `pay` twice on re-claim; `reconcile` by the payer or the adapter; no `transaction` | M |
| WF-18 | `workflows.md#research-and-analysis`: unread sources recorded as unread; `persist` before `deliver`, read back | run | read | a source omitted rather than marked unread; `deliver` before `persist` | M for order; R for content |
| WF-19 | `workflows.md#meeting-processing`: a recording the operator was not party to fails `classify`; `extract` minimizes; `persist` creates tasks entering their own intake; `deliver` creates outreach tasks and never sends | run | read | a non-party recording routed; a `contact` write carrying a field outside the analyst's grant's allowlist landing (U-15 closed); an extracted task with no intake; a send from this batch; a transcript minted as an artifact (X-18 closed) | M for shape; R for content |
| WF-20 | `workflows.md#operator-only`: only the operator-facing agent claims; `present` through `channel_config`; `await` bounded to `rounds_exhausted`; `record` read back; the redirect is by this batch's close, not a new intake | run, `CLK` | read | a lease held by any principal but the operator-facing agent; no `CH` presentation; no `rounds_exhausted` at the bound; a second intake | M |
| WF-21 | `workflows.md#session-digestion`: unread never empty; unverifiable never confirmed; `file` creates and completes none | a transcript that cannot be read; a claim with no system of record | run | read as empty; promoted; a filed task marked done | M |
| WF-22 | `workflows.md#what-no-workflow-in-this-document-does` | every declaration | read | any names two successors, names intake as successor, names an agent, an operator, a payee, a contact, or a channel | M |
| WF-23 | `workflows.md#rendering`: the step tables are render targets of the `workflow` entities; a table edited by hand fails `--check`; an undeclared workflow's table is hand-authored under the same marker and becomes the check's expected content when the entity is declared | the declarations of `B0`; one table edited by hand; one section whose workflow is undeclared | run the renderer's `--check` | it passes on the edited table; a declared workflow's section lacks its marker; the undeclared section's table is not what the check expects on the day the entity lands | M (contract; whether `render_workflow_docs.py` exists is `status.md`'s) |
| WF-24 | `workflows.md#whether-a-stage-names-anything-a-step-does-not` | — | — | P (open decision 33; finding U-30 points at it, and the matrix observes nothing under either option) |
| WF-25 | `workflows.md#what-link-attaches-and-what-it-leaves-to-hydration` (ruling 39): `link` attaches every record the task names — an external one as an artifact, one in the record by `REFERS_TO` — and nothing on relevance alone; a name resolving to nothing creates nothing; hydration resolves a step's declared reads from those anchors; context a step discovers is written back as the same edge | `T1` naming a transcription, a contact, and an issue, and one identifier that resolves to nothing; a step that discovers a further entity | run intake; run the step | a named entity that exists with no edge; an edge to an entity the task did not name; an entity created for the unresolved name; hydration reading past the anchors where the declared type is reachable from them; the discovered entity not written back as `REFERS_TO` | M |
| WF-26 | `workflows.md#planning`: entry by the record's one live instance; `survey` names its reads; `judge` writes only findings; `amend` writes through `amend_<level>` actions, creates tasks under the record and the parent, creates the next instance before its sign-off, and names no successor; optional review steps by `applies_when` carry a role's findings | `PL1` with its live instance; a project declaring a `finance` review step on plans whose criteria name a figure | run the batch on `PL1` | a second live instance; a statement write at `judge`; an amendment with no `amend_<level>` action; the sign-off before the next instance is read back; a successor named; the review step never inapplicable | M for structure; R for judgements |

### `planning_model.md`

The planning records are context entities of the operator's, so the fixtures register one planning type
per level a row needs (`P-lvl(plan)`, `P-lvl(objective)`, each marked with its rank) and one record of
each (`PL1` under `OB1`), and never a record's text; a row reads edges, counts, actions, and sign-offs.

| # | Rule | Setup | Action | Red when | Class |
|---|---|---|---|---|---|
| PM-1 | `planning_model.md#the-hierarchy-is-edges-and-a-task-has-one-line-upward`: one `PART_OF` per task and per record, upward in level, cycle-checked; the ascent derived | `T1` under `PL1` under `OB1` | write a second `PART_OF` from `T1` to a second plan; write `PART_OF` from `OB1` to `PL1`; read `T1`'s ascent | either write lands; the ascent is anything but `PL1`, `OB1`; a `plan_id` field on the task | M |
| PM-2 | the same: a second membership is `REFERS_TO` or `DEPENDS_ON`, never a second line | `T1` under `PL1`, naming `PL2` | run intake | no `REFERS_TO` `T1` → `PL2`; a `PART_OF` to it; `PL2`'s derived counts include `T1` | M |
| PM-3 | `planning_model.md#upward-context-is-a-declared-read-resolved-along-the-ascent-at-hydration`: a step declaring a planning type reads it along the ascent at hydration; an ascent ending before the type is an empty read; unplanned is derived and admitted | a step declaring `objective`; `T1` under `PL1` under `OB1`; `T2` under nothing | open the step on each | the step opens on `T1` with `OB1` unread, or reads a record not on the ascent; the step on `T2` holds as `unknown`, or `T2` is refused at intake; unplanned stored as a status | M |
| PM-4 | the same: `classify` writes the `PART_OF` where the task names a record; `prioritize` may read the ascent | `T1` naming `PL1`; a `priority_rubric` weighting by objective | run intake | no `PART_OF` `T1` → `PL1`; the priority set without the rubric reading `OB1` | M for the edge; R for the weighting |
| PM-5 | `planning_model.md#downward-state-is-derived-upward-content-is-authored-as-entities`: no stored progress; completion, counts, blockers, next steps derived; decisions as entities; a todo is a task | `PL1` with three tasks, one terminal, one held by a checkpoint | inspect the registered planning types; read `PL1`; write a decision | a field named `status`, `progress`, `todos`, `todos_pending`, `next_steps`, or `decisions` on the type; completion not derived as false, the held count not one; the decision landing anywhere but a `decision` entity `PART_OF` `PL1` | M |
| PM-6 | the same: a decision reversed is a new `decision` `SUPERSEDES` the old; nothing is merged | two `planning` batches under `PL1` recording two decisions at once; a reversal | write both; reverse one | either decision missing after both writes; the reversed one edited in place; no `SUPERSEDES` edge | M |
| PM-7 | `planning_model.md#maintenance-is-work-the-planning-workflow`: one live `planning` instance per record; a descendant's close pulls its `due_date` forward; a record with none is unmaintained; the parent's statement is never written by the child's batch | `PL1` with its live instance dated next week; `T1` under it closes | read the instance; run the `planning` batch; have `amend` write `OB1`'s statement | the instance's `due_date` not corrected to the close; two live instances; a record with none not readable as unmaintained; the write to `OB1` landing (it is a task `PART_OF` `OB1`, or refused) | M |
| PM-8 | the same: a lesson at the record's scope is a task `PART_OF` the parent, and the raising batch does not wait | `judge` records a finding whose remedy is the parent's | run `amend` | no task `PART_OF` `OB1` `REFERS_TO` the finding; a `DEPENDS_ON` from the batch to it | M |
| PM-9 | `planning_model.md#authority-per-level-an-amendment-is-an-action-and-its-class-is-the-levels`: an amendment is an `amend_<level>` action through the gate; a level not listed resolves to `NEVER`; a planning type is not a governance type; an `ownership_grant` on the record seats its principal | `B0+pol(low: [amend_plan])`, `OB1` owned by principal `O` | amend `PL1`; amend `OB1` | the plan amendment held; the objective amendment taken without a checkpoint, or its checkpoint's `AWAITS` not naming `O`; a write to a planning type carrying no action; the type in the governance list of eight | M — the permitted-action half under decision 56's shape: the planning types' authored fields on the engine's grant alone, a `planner`'s write to a statement refused at admission; and the operator's own amendment at the reserved level held, self-resolved and marked, and written by the engine (decision 58) |
| PM-10 | `planning_model.md#binding-dissolves-a-tasks-ascent-is-its-binding`: a session binds nothing and corrects no planning field; its output is tasks under records | a session-mechanism credential | correct `PL1`'s statement from it; create a task naming `PL1` | the correction admitted; the task with no `PART_OF` to `PL1` | M for admission (by grant, ruling 41); D for the session's own conduct, as U-12 |
| PM-11 | `planning_model.md#the-mechanism-against-cross-record-collision-is-the-subject`: a planning write lands only on the record the batch's task is `PART_OF` | a `planning` batch under `PL1` | its `amend` writes a decision `REFERS_TO` `PL2` | the write lands, or lands under any credential but the engine's | M — the engine checks the producing task's ascent before it writes, and no other grant admits the type (decision 56's shape) |
| PM-12 | `planning_model.md#which-levels-an-instance-declares-and-what-it-calls-them` | — | — | P (open decision 57; the design reads the mark and never a level by name, so no row goes red under any answer); decision 58 is ruled and tested under PM-9's operator case |

### `github.md`, `gmail.md`, `calendar.md`, `telegram.md`, `payments.md`

The five system documents are inbound and outbound tables. Their rows fall into three kinds, and the suite
treats each kind once. **Generic rows** — identity, linkage, dedup, disposition, provenance, sourcing — are
AD-9 to AD-14 instantiated per system (the inbound axis below says which combinations are load-bearing).
**Disposition rows** — every `handled`, `deliberately ignored`, and `unhandled` row — are one parameterized
test per system: deliver the row's event and assert the row's stated outcome or its stated drop reason,
with the drop counted. That test is generated from the table, so a row added to the document without a
fixture fails, and an event delivered that matches no row lands in `dropped` with `unmapped` (AD-21). The
rows below are the **load-bearing rows**: the ones whose observable is particular to the system and would
not be produced by the generic tests.

| # | Rule | Setup | Action | Red when | Class |
|---|---|---|---|---|---|
| GH-1 | `github.md#reviews-review-comments-and-threads`: `APPROVE` is a sign-off only through identity; an automated account's never stands in | `T-at(feature, pr_review)`, `X(github)` | `APPROVE` from `cred-owner`, `cred-other`, `cred-none`, an automated account | any but the first yields a sign-off; the first yields none | M |
| GH-2 | the same: the operator's `APPROVE` while a merge checkpoint awaits is a resolution, not a sign-off | a held `merge_pr` action | `APPROVE` from `cred-op` | no resolution read back; a sign-off | M (X-7) |
| GH-3 | `github.md#pull-requests`: `closed` merged with no action is an observation and a defect to surface, never a confirmation | a PR merged by a person | deliver | a `merge_pr` confirmation appears; no defect surfaced on `CH` | M |
| GH-4 | the same: auto-merge enabled is a blocking condition the steward reads | deliver `auto_merge_enabled` | the steward would merge | the merge taken; the swarm's credential ever enables auto-merge in `X` | M (unhandled row; the rule is stated) |
| GH-5 | `github.md#issues`, `github.md#pull-requests`: a label is never step state | deliver `labeled` naming `qa` | read step state | any change | M |
| GH-6 | `github.md#the-transitions-the-mining-found-unhandled`: base retargeting sets `checks` to `unknown`; ambiguity fails toward `unknown` | a green check; deliver `edited` with `changes.base` | read | `checks` still `passing`; the step proceeds | M |
| GH-7 | the same: a force-update leaves sign-offs pinned and readable as stale; nothing unsigns | a signed `pr_review`; force-update | read | the sign-off changed; staleness not derivable; the steward merges without reading it | M |
| GH-8 | the same: a dismissed review is an observation, never an unsigning | dismiss | read | the sign-off changed | M |
| GH-9 | `github.md#security-advisories-and-what-the-adapter-does-not-surface`: the adapter writes identifier, package, range, fixed version, and nothing else; coverage says withheld | deliver an advisory with a description and a proof of concept | read every observation and the task | any withheld field present anywhere in the record; coverage silent on the withholding | M |
| GH-10 | `github.md#checks-and-statuses`: `unknown` on an unreadable payload, never `pending` or `failing` | a malformed `check_run` | deliver | anything but `unknown` | M |
| GH-11 | `github.md#repository-level-events-the-swarms-operations-produce-or-depend-on`: `repository_dispatch` and `workflow_dispatch` are `dropped` with `inbound_command` and the reason goes back | deliver | read `X` | no observation to the requester; anything runs | M |
| GH-12 | the same: `installation` and `meta` announced off-record | deliver | `CH` | no announcement | M |
| GH-13 | `github.md#conditions-that-are-not-events`: mergeability is a read with coverage, never a fifth outcome | the steward needs it | `X` | no read; an outcome type for it | M |
| GH-14 | `github.md#outbound-the-operations-the-code-workflows-take-on-the-host`: each row's confirmation is a read-back; the security release notes are narrowed | each outbound row | take | a confirmation with no read in `X`; notes carrying exploit detail | M |
| GH-15 | the same: what the adapter never does at this host | a full run | `X` | auto-merge enabled, a self-approval, a force-update on a pinned branch, a deletion, a recovery on its own initiative | M |
| GH-16 | `github.md#the-property-that-makes-this-a-control-and-not-a-list`, `github.md#what-the-outcomes-are-and-the-rule-against-a-fifth`: an event outside the tables is `dropped` with reason `unmapped` and counted; no fifth outcome; an event that invalidates a decision is an observation, and the pinned sign-off reads stale | deliver an event type the tables do not name; a force-update after a sign-off | read | the delivery has no disposition, or its drop is uncounted; an outcome type outside the four; the sign-off changed | M (AD-21 and GH-7 at this host; kept because the document states it as its own control) |
| GH-17 | `github.md#releases-and-tags`: `release.published` confirms the `release`-class action and `verify_deployed` still reads the deployed checkout; `release.deleted` and `release.unpublished` are observations and never a recovery having happened; a tag deletion is half a `retag_release` confirmation | deliver each | read | `verify_deployed` signed on `release.published`; a `retag_release` or `deprecate_publication` action confirmed by the deletion or the unpublish alone; the retag confirmed before the tag resolves to the intended commit | M |
| GH-18 | `github.md#everything-else-the-host-can-deliver`: every unnamed class is `dropped`, reason `out_of_scope_class`, counted; `installation` and `member` events are observations, the first announced off-record, and none alters a binding | deliver a sponsorship event; an installation suspended; a member added | read; `CH` | no drop with that reason; no announcement for the installation change; a credential binding written or ended by the member event | M |
| GM-1 | `gmail.md#what-arrives-and-what-must-be-asked-for`: the notification wakes; the history entry is the signal; an expired marker is a gap, not a resync; the marker is derived, never stored | `X(mail)` with a coalesced notification; an expired marker | deliver | an outcome keyed on the notification; a re-list standing in for the gap; a stored cursor | M |
| GM-2 | `gmail.md#messages`: a sent message matching no action on a tracked thread is a defect; on an untracked thread an ordinary record; spam yields no task; a reply from the addressed party closes nothing by itself | the four deliveries | deliver | the defect not surfaced; the untracked one surfaced; spam a task; `follow_up` closed by the message | M |
| GM-3 | `gmail.md#threads-and-labels`, `gmail.md#mailbox-settings-which-change-what-the-adapter-can-see-or-do`: a label is never step state; archive is not completion; a star is not priority; forwarding, filter, delegate changes are announced | deliver each | read | step or task state moved; no `CH` announcement for the three | M |
| GM-4 | `gmail.md#conditions-that-are-not-delivered-at-all`: a bounce is an observation, never a confirmation reversal | a bounce | deliver | the send's confirmation changed | M |
| GM-5 | `gmail.md#outbound-the-operations-the-workflows-take-on-the-mail-system`: send read back by message id; the key on the reviewed body's digest; a follow-up's thread id equals the batch's; no recovery row | send; follow up | `X` | confirmed on the call's return; a changed body under the same key; a follow-up on another thread; any retract operation | M |
| GM-6 | `gmail.md#how-the-five-rules-apply-to-this-system`: identity resolves an address with authentication; unreadable authentication fails closed to an observation; a mailbox's many addresses resolve to the mailbox's principal and no further | a message from the operator's address with, without, and with unreadable authentication; from a delegate | deliver as a checkpoint answer | a resolution from the second or third; a resolution attributed to a human behind a shared mailbox | M |
| GM-7 | the same: an unconfirmed key is reconciled by reading the sent folder before any resend | a lost confirmation; re-claim | `X` | a second send before a sent-folder read | M |
| GM-8 | `gmail.md#what-this-adapter-refuses` 2–7 | a full run | `X` | a send with no action; a draft updated; a filter, forwarding rule, or responder written; a permanent deletion; a label written as state; a delegate or alias added | M |
| GM-9 | `gmail.md#a-thread-and-its-messages-are-each-artifacts-related-by-part_of`: both levels are artifacts; a history entry lands on the message, minted `PART_OF` its thread; a send's confirmation mints the message `PART_OF` the thread; a sign-off on a thread pins the message set the read returned with its coverage; a regrouped message ends one edge and writes another, never a re-identification | a delivery naming a message; a send; a sign-off on a thread; the system regrouping a message | deliver; read | the message not `PART_OF`; the thread's sign-off lacking the message set or its coverage; the regrouped message's `external_id` changed, or its old edge deleted rather than ended | M |
| GM-10 | `gmail.md#what-artifacts-this-system-holds`: an artifact's `external_id` is the system's message id, never the RFC 5322 header id, which is an observation; a mail-system draft is never what a step closes on; an attachment's id is scoped to its message | a message carrying both ids; a batch at `draft` | read | an artifact keyed on the header id; the header id absent as an observation (it is what matches an operator-sent message to a `dedup_key`); a `draft` step signed on a mail-system draft; an attachment artifact with no `PART_OF` to its message | M |
| GM-11 | `gmail.md#what-the-design-uses-and-what-the-api-offers-that-it-does-not`, `gmail.md#everything-else-the-mail-system-exposes`: the refused and the unused operation families | a full run | `X(mail)` | any call to an operation the table marks refused or not used — batch modify, import, insert, a label definition, a mailbox setting, an encryption key | M (the outbound half of GM-8, stated over the whole surface) |
| CA-1 | `calendar.md#outbound-the-operations-the-workflows-take-on-the-calendar`: the class depends on the attendees, read first; unreadable attendees take the higher class | create and update a solo event; one with attendees; one whose attendee read fails | take | the attendee-bearing write classed `external_api_write`; the unreadable one classed low; no read before the classification in `X` | M |
| CA-2 | `calendar.md#conditions-that-are-not-delivered-at-all`: begun and ended are derived from a stored time against the clock, with a declared freshness; an unreadable timezone is not a known instant | an event moved after the last read; a step depending on it having ended | the step would open | it opens without the declared re-read; a time with an unresolved timezone treated as an instant | M |
| CA-3 | `calendar.md#events`: an event moved never rewrites a task's priority; cancelled closes no step; a response change closes nothing; an event by the swarm's credential matching no action is a defect | deliver each | read | any moves task or step state; the defect not surfaced | M |
| CA-4 | `calendar.md#how-the-five-rules-apply-to-this-system`: the client-supplied id is the dedup key's identity | a create; re-claim | `X` | a second create with a different id; a duplicate succeeds at the system | M |
| CA-5 | the same: an occurrence read's coverage states the window | read occurrences | read | an observation over an unbounded series with no window | M |
| CA-6 | `calendar.md#what-this-adapter-refuses` | a full run | `X` | a `contact` per invitee; an attendee-bearing write outside a checkpoint; a response without the gate; a clear or delete of a calendar; an access rule written; an entry as step state; a read of an unnamed calendar; a reminder as a trigger | M |
| CA-7 | `calendar.md#a-series-and-its-occurrences-are-each-artifacts-related-by-part_of`: both levels are artifacts, the occurrence `PART_OF` the series; a signal lands on the unit whose id it carries; a rule change is an observation on the series and every held occurrence is re-read before a step depends on its time; a sign-off on an occurrence pins a dated fact, on the series the declaration as read | an occurrence modified independently; a rule changed; a task against one occurrence; a step depending on that occurrence's time | deliver; read | the occurrence lands on the series or is not minted `PART_OF`; the rule change lands on an occurrence; the step opens without the re-read; the task's reference moves | M |
| CA-8 | `calendar.md#what-the-design-uses-and-what-the-api-offers-that-it-does-not`, `calendar.md#calendars-sharing-and-settings`, `calendar.md#everything-else-the-calendar-exposes`: quick-add, import, move, calendar create and delete, calendar-list writes, colours, and reminders are unused; a calendar-list or timezone change is an observation that alters what every other read means | a full run; a calendar's timezone changed after a stored time was read | `X(calendar)`; the step would open | any unused or refused call; the stored time treated as an instant after the change with no re-read | M |
| TG-1 | `telegram.md#a-chat-message-is-not-an-instruction`: a message never opens, claims, closes, names, sets, advances, cancels, reprioritizes, halts, or takes | from `cred-op`: "ship it", "cancel that", "do X" with no correlation | deliver | any step, batch, task, lease, or action changes; anything but a task for intake (U-16 closed: an uncorrelated message from a bound principal is always a task) | M |
| TG-2 | the same: the two conditions of the narrow path; a reply failing either is an observation | a checkpoint awaiting P; replies correlated to it from `cred-op` (P), `cred-other`, `cred-none` | deliver | the second or third resolves; the first does not | M |
| TG-3 | `telegram.md#which-checkpoint-a-reply-answers-is-decided-by-correlation-not-by-reading-the-text`: correlation is structural; no readable decision is `unknown` and holds; recency never decides | two open checkpoints; "yes" with no reply-to; a reply-to carrying text matching no option | deliver | either checkpoint resolves; the nearest option selected | M |
| TG-4 | `telegram.md#the-callback-payload-is-the-swarms-own-text-and-free-text-is-not`: a callback from a non-approver is an observation; an unrecognized payload is dropped; a stale payload is an observation with the reason back; the token is not a capability; acknowledgement is not resolution | callbacks from `cred-other`; a forged payload; a press on a terminal checkpoint | deliver | a resolution from the first; the forged one trusted; the terminal one resolved; `X` acknowledged before `RP` shows the read-back | M |
| TG-5 | `telegram.md#commands`: a command is a task except the two reads; the halt is not a command | commands naming a workflow, an action, the halt; the two read commands | deliver | a workflow entered directly; an action taken; a halt begun from the message with no confirming read; a read command writing anything | M |
| TG-6 | `telegram.md#chats-groups-and-who-can-see-what`: the sender's credential is resolved per delivery, never the chat's; coverage records the partial view | a group whose chat id binds to P; a member who is not P answers | deliver | a resolution attributed to P; a group observation with no coverage | M |
| TG-7 | `telegram.md#delivery-webhooks-long-polling-and-what-the-dedup-rule-keys-on`: dedup is a membership test over a window, never a high-water mark; acknowledgement follows the confirmed write | update ids that jump backward after a quiet period; `RP` failing writes | deliver | a delivery after the jump discarded; the offset advanced before the write is read back | M |
| TG-8 | `telegram.md#identity-that-moves-under-the-record`: a placeholder message id is not a confirmation | a send returning a placeholder | read | an artifact minted; the action anything but `unknown` | M |
| TG-9 | `telegram.md#conditions-that-are-not-updates`: a deleted or edited message retracts nothing | delete the message a resolution came from | read | the resolution changed | M |
| TG-10 | `telegram.md#outbound-the-operations-a-step-takes-on-the-channel`: the announcement path is one-way and carries no keyboard; the checkpoint stays open whatever the delivery status | a halt; a presentation whose send fails | read | an announcement with options or a keyboard; a checkpoint resolved or closed by a delivery failure | M |
| TG-11 | `telegram.md#what-this-document-refuses-and-why` | a full run | `X`, census | an intent parse; a gesture as a decision; a chat-shaped state store; a decision-bearing message during a halt; a deletion to clean the record | M |
| TG-12 | `telegram.md#a-reaction-never-carries-a-decision` | a reaction from `cred-op` on the presentation of a checkpoint awaiting the operator; then its removal | deliver | a resolution; anything but an observation; the removal treated as a reversal of anything | M |
| TG-12a | `telegram.md#during-a-halt-a-read-on-the-channel-is-answered-with-the-halt-and-never-with-data` | `RP` failing all reads; a read command from the bound chat; one from an unbound chat | deliver | the answer carries a queue, a state, or any data; it names no since-when and no reason; it is sent anywhere but the chat the announcement path reaches; it carries options or a keyboard; the command's delivery is acknowledged; after the record returns, the redelivered command is not answered from the record; past the copy's declared staleness bound with no refresh, an answer or an announcement is sent | M (U-27 closed) |
| TG-13 | `telegram.md#what-the-channel-holds-and-what-an-artifact-is-here`: a chat, a checkpoint, and a conversation are not artifacts; the message presenting a checkpoint is | a presented checkpoint | census | an artifact for a chat, a checkpoint, or a conversation; the presentation message with no artifact once its send is confirmed | M |
| TG-14 | `telegram.md#what-the-adapter-does-not-write-and-what-it-does-not-fetch`: incidental content is not persisted; a shared contact, location, or venue is recorded as its kind and never its content; media is observed, and fetched only by a read a step declares; both narrowings state their coverage | deliver a message carrying a location, a contact, and a file | read; `X` | the content of any of the three in the record; a fetch of the file's bytes with no step declaring the read; an observation that does not say fields were withheld | M |
| TG-15 | `telegram.md#recovery-what-undoes-an-effect-on-this-channel`: every recovery is forward-only and its own action through the gate; a send with a lost confirmation is read back by `dedup_key` before any resend; a stale button is checked, not trusted | a notification sent in error; a lost send confirmation; a press on a terminal checkpoint's button past the edit window | take; read | a deletion as the recovery; a second send before the read-back; the terminal checkpoint resolved by the press | M |
| TG-16 | `telegram.md#inbound-every-update-the-chat-api-can-deliver`, `telegram.md#edits-deletions-and-reactions`, `telegram.md#membership-polls-and-the-rest`, `telegram.md#what-the-api-offers-that-this-design-does-not-use`: the three-way marking on every update kind; a poll is never a decision queue; a membership change alters no binding | the generated disposition test; a poll answer from `cred-op`; a membership change | read | an update kind with no status; a poll answer resolving anything; a binding written or ended by the membership update | M |
| PY-1 | `payments.md#the-dedup-key-and-what-it-is-keyed-on`: keyed on the obligation, written before the first attempt; a confirmed key refused | `T-at(payment, pay)`, `X(rail)` | read the action before submission; submit; re-take | no key before the first call in `X`; a key per attempt; a key from the reference field; a second submission on a confirmed key | M |
| PY-2 | `payments.md#the-unknown-case-a-transfer-submitted-whose-confirmation-never-returned` and its five parts (`payments.md#the-two-questions-which-must-not-be-conflated`, `payments.md#what-the-record-holds-while-it-is-unknown`, `payments.md#how-the-read-resolves-it-per-rail-class`, `payments.md#when-the-read-cannot-resolve-it`, `payments.md#the-complete-rule-stated-once`): a timeout is `unknown`; no artifact; `reconcile` stays open; the read precedes any resubmission; crypto rebroadcasts the identical bytes; bank adopts what the window finds; exhaustion raises one checkpoint with what was read and three options; the adapter never chooses | a lost response on each rail class; the read finding present-confirmed, present-unconfirmed, absent, and unresolvable | run | the action reads failed or confirmed; an artifact with no id; `reconcile` signed; a submission before a read; a newly constructed transaction; a re-issue where the window found one; no checkpoint at the bound; the adapter retries, marks failed, or advises a manual payment | M |
| PY-3 | `payments.md#terminal-is-not-permanent-and-the-design-must-not-assume-it-is`: the terminal condition is declared; a reversal is an observation and a defect, never a silent correction or a resubmission | a rail state undone after confirmation | deliver | the confirmation changed; a resubmission; no defect surfaced | M |
| PY-3a | `payments.md#terminal-is-declared-in-the-rails-adapter-document-and-the-value-is-bound-per-instance`: settled, never sent, on a bank rail; *N* confirmations on a chain; the value in the `vendor_binding`; a profile may deepen and never shallow | a bank transfer at released; a chain transaction at depth *N*−1 and at *N* under a binding with *N*; a profile declaring *N*+2; one declaring *N*−1 | read | a confirmation at released; one at *N*−1; none at *N*; the deeper profile ignored; the shallower honoured; a depth the binding does not carry used anywhere | M |
| PY-4 | `payments.md#reading-a-balance-an-observation-and-not-an-artifact`: on the account's artifact, with point and confirmed-versus-pending; never a permit | read a balance; a sufficient one | run | a balance artifact; no height or point; a payment taken on sufficiency | M |
| PY-5 | `payments.md#fees-rates-and-what-the-operator-consented-to`: the adapter never widens what was approved; a fee is disclosed | an expired quote re-pricing outside what the checkpoint named | take | it is taken; the checkpoint carried one figure where the rail distinguishes two | M |
| PY-5a | `payments.md#tolerance-is-an-action_policy-value-and-its-default-is-zero`: absent reads as zero; a change outside it is an observation, `consent`'s `on_fail` opens `verify` again, and the operator decides on the new figures; a profile may tighten and never widen | no tolerance declared and a re-quote moving the amount by the smallest unit; a tolerance declared, one change inside and one outside; a profile tighter than the policy; one looser | take | the smallest change taken under no value; the inside change held or the outside taken; `verify` not reopened; the looser profile honoured | M |
| PY-6 | `payments.md#the-reference-field-and-a-policy-that-suppresses-it`: omission, never a placeholder; a property of what is submitted; verified on read-back; the record narrowed too | a profile suppressing metadata | take; read back | `X` shows a field, even empty; the read-back does not check it; payee identifying details copied into the record | M |
| PY-7 | `payments.md#events-about-a-transfer-the-swarm-submitted`: a non-terminal state is not a confirmation; a notification is never a confirmation on its own; a transfer with no action is a defect | deliver each | read | a confirmation from a non-terminal state or from a notification with no read; the defect not surfaced | M |
| PY-8 | `payments.md#outbound-the-operations-a-step-takes-on-a-rail`: `payment` is in the never-set and never graduates; the adapter never writes `reconcile` | a hundred confirmed payments | the hundred-and-first | taken without a checkpoint; a `reconcile` sign-off from the adapter's credential | M |
| PY-9 | `payments.md#what-the-adapter-refuses-and-why` | a full run | `X`, `RP` | any of the ten | M |
| PY-10 | `payments.md#a-payments-approver-is-shown-exactly-what-the-verifier-signed` | `T-at(payment, consent)`; then parameters changed before `pay` | read the checkpoint; take | `needed_input` lacks payee, amount, currency, period, rail, or the verifier's identity, differs from the `verify` sign-off, or does not refer to it; `pay` taken on parameters differing from what the checkpoint carried | M |
| PY-11 | `payments.md#what-the-rails-hold-and-what-an-artifact-is-here`: a bank transfer's id exists only when the response returns and a chain transaction's before submission; a receipt is an artifact; the `transaction` entity is not | a transfer on each rail class; a rail that issues a receipt | read | a bank-rail artifact minted before the response; a chain id computed anywhere but from the signed transaction the swarm holds; a `transaction` entity carrying `system` and `external_id`; a receipt with no `PRODUCES` from the batch | M |
| PY-12 | `payments.md#what-gates-a-payment-and-why-it-is-not-a-new-mechanism`: four existing mechanisms compose; no second gate; `payment` never graduates | as PY-8, AU-18, PY-10 | census | a second approval surface, a cooling-off timer, or a value ceiling built as a mechanism beside the gate rather than as a policy value or a quorum | M |
| PY-13 | `payments.md#events-about-money-the-swarm-did-not-send`, `payments.md#delivery-and-what-the-dedup-rule-keys-on-inbound`: an incoming payment is matched to an obligation by a step's signed judgement, never by the adapter reading the reference text; an outgoing payment with no action is a defect; the inbound key is the per-delivery id on a bank rail and the transaction with its height on a chain; a notification is never a confirmation | deliver an incoming payment whose reference names a tracked obligation; an outgoing transfer with no action; one chain transaction at two heights; a notification of a terminal state with no read | read | the adapter wrote the match; no defect surfaced; the second height deduplicated away; a confirmation with no read in `X(rail)` | M |
| PY-14 | `payments.md#recovery-what-undoes-a-payment`, `payments.md#what-the-rails-offer-that-this-design-does-not-use`: cancel is a reversal only before funding; a recall is a request that never returns success; a chain confirmation has no recovery; a replacement is a new action through the gate; scheduled, bulk, and rail-side approval features are unused | a released bank transfer; an unconfirmed chain transaction | take the recovery | a recall recorded as succeeded on the rail's acceptance; a replacement broadcast with no gate evaluation; any call to a rail's scheduled-payment, bulk, or dual-authorization surface | M |
| PY-15 | `payments.md#how-the-five-rules-apply-here`: the payee is resolved by reference to the profile, never by matching a name, an address, or a reference string; a rail's indeterminate state is `unknown`, never pending or failed | a task whose payee name matches two profiles; a rail returning an indeterminate state | run | a payee resolved by name; the state read as anything but `unknown` | M |

### `conformance.md`

| # | Rule | Setup | Action | Red when | Class |
|---|---|---|---|---|---|
| CF-1 | `conformance.md#design-basis`: the mechanical check; `no design applies` judged | a PR body with a missing basis; one citing a path not on disk; one declaring `no design applies` where a kernel document governs | check | the first two pass; the third passes review | M for the first two; R for the third |
| CF-2 | `conformance.md#an-issue-that-conforms-to-nothing`: five dispositions under operator approval | — | — | R |
| CF-3 | `conformance.md#mechanical-checks-on-this-directory`: each check runs and fails on a planted case | plant a Never word, a broken anchor, an unlinked first mention, a hand-edited table | run | any passes | M (exists for three; contract for two) |
| CF-4 | `conformance.md#direction-of-truth-per-class-of-record`: merge upward then render downward | a mirror richer than its source | regenerate | the mirror's content lost with no correction to the source first | R |
| CF-5 | `conformance.md#the-register-of-open-design-decisions`: a decision opened is registered in the same change; 19 and 22 stay unused | the documents | lint every "open decision N" against the register | a number in a document and not in the register; 19 or 22 assigned | M (contract: a rule-coverage check, below) |
| CF-6 | `conformance.md#phases-and-implementation-state` | the documents; one with a planted hash in a body paragraph, one with a planted issue number there | run the citation lint | either passes; a `Sources:` clause fails | M (contract; U-3 closed) |
| CF-7 | this document's own rule: every rule has a row | the documents and this matrix | lint every `###` heading and every bold-led rule sentence in the foundation against the matrix's pointers | a heading with no row; a row whose pointer resolves to nothing (the anchor check already fails that) | M (contract) |
| CF-8 | `conformance.md#always-read`, `conformance.md#read-when-these-paths-changed` and its per-domain tables (`conformance.md#work-and-workflows`, `conformance.md#failure-posture`, `conformance.md#authority`, `conformance.md#vocabulary-and-agent-instructions`, `conformance.md#the-data-model`, `conformance.md#adapters`, `conformance.md#the-foundation-itself`, `conformance.md#the-conformance-suite`): the kernel is three; each keyed row's paths select its document; `status.md` and the companions are never selected | the reading list | select readings for each keyed path, for a foundation path, and for a path no row keys | a fourth kernel document; a keyed path that fails to select its document; `status.md` or a companion selected; an unkeyed path selecting anything but the kernel | M (`test_foundation.py`'s existing assertions, named here so that the rows over `conformance.md` are complete) |

### `migration.md`

The companion the bootstrap is ordered against (above). Its rules about a populated instance run on the
`LEG` fixture, and every row below is an instance of a kernel rule applied to a carrying write; the
headings that carry no rule — the name, the two legs, the mapping tables (each disposition is tested by
class through MG-1 to MG-3), the policy postures — have no rows. The document is never keyed, so the
rule-coverage check does not read it; the rows exist because a from-zero suite that cannot also judge the
migration's writes would leave the largest lossy mutation the swarm will ever make untested.

| # | Rule | Setup | Action | Red when | Class |
|---|---|---|---|---|---|
| MG-1 | `migration.md#dispositions-and-the-primitive-that-carries-each`: no entity deleted and recreated; no verdict written for a principal that did not write it; no field bulk-rewritten to a new spelling; no write to an entity a process that does not know the new shape is still writing | `LEG` | run leg two | a legacy id that stops resolving; a `sign_off` attributed to the agent a step record names; a bulk correction of a field's spelling; a write to a type whose retired writer is live (MG-8) | M |
| MG-2 | the same: re-type is three primitives — register, interpret over the same source, merge — and the old id redirects; every inbound edge is repointed by the merge | `LEG` with one retired declaration | re-type it | the survivor lacks provenance to the source; the retired id does not resolve through a merge pointer; an as-of read on either id before the merge differs from the other; an inbound edge lost | M (decision 31 ruled 2026-09-06: the merge form is the mechanism — `migration.md#how-a-registered-entity-type-is-renamed-on-a-live-record`) |
| MG-3 | the same: retire means freeze — no writer produces the type after its stage, nothing is deleted, and the freeze is verified by the count not moving | `LEG` after every writer's redeployment | wait a window | the count of a retired type moves; an entity deleted | M |
| MG-4 | `migration.md#how-the-migration-is-governed`: leg one is the enumerated operator act and ends when this document's bootstrap read-backs hold; leg two is tasks through `record_migration`, each write an action of its class, each class reserved until the operator writes a value | `LEG`, `B0` | run stage 3 with no policy value for a migration class; then with one | a stage-3 write with no action behind it; a class with no value taken rather than held `NEVER`; a leg-two write by the operator's bare credential outside a batch; a batch producing an ungranted class claimed by anyone but the operator-facing agent | M (decision 56, ruled 2026-09-06: the enforcement point is the engine's sole grant, so a leg-two governance write lands only through it) |
| MG-5 | the same: the `record_migration` declaration — five steps with their owner roles, `on_fail` on `verify` opening `apply`, `reads_to_enter` naming the retired types, `reads_to_close` naming the targets, no successor | `B0` | declare it; declare a variant whose `reads_to_close` names an unregistered type | the variant lands; `apply` opens before `map` is signed; `verify` signed by the `apply` step's owner; a successor named | M |
| MG-6 | `migration.md#ordering-dependencies-and-what-each-stage-depends-on`: each stage names the stage it cannot precede; inside stage 4 the order is halt confirmed by read-back, then merge, then enable; stage 3 precedes any daemon's first new-type write | `LEG` with a retired-engine stand-in that substitutes a literal step list when its declaration is absent | run stage 4 with the halt unconfirmed; write a new type before stage 3 | a declaration merged while the stand-in is live, so its literal list appears; the new-type write permitted, or denied with no `capability_denied`; a stage started before the stage it names | M |
| MG-7 | the same: every write's idempotency key is derived from the source's stable identity and the stage's name, never from a clock; an interrupted stage re-run lands once | `LEG` | interrupt a stage; re-run it | two survivors for one source; a key carrying a clock value | M |
| MG-8 | `migration.md#the-live-daemons-and-how-the-plan-sequences-around-them`: the tolerant claim predicate counts the retired liveness value within its age window as held, permanently; one engine cutover per instance; the operator-facing agent reads both queues through the dual-write window; the date-moving daemon stops before the first recurring task is routed | `LEG` with a task carrying the retired value; two queues; a recurring task | claim; resolve; complete | two runners on the one task; one workflow cut over alone; a decision answered in one queue and open in the other; two live instances after the first completion | M |
| MG-9 | `migration.md#reversibility`: a registration is not reversible and is checked first; a merge is a lossy record mutation held at the gate; a derivation is reversed by ending its rows | `LEG` | reverse each | reversing a merge and leaving the survivor's edges pointing nowhere; a merge taken with no checkpoint; a registration attempted with no `ownership_grant` | M |
| MG-10 | `migration.md#verification`: each stage has its read-back, and the `verify` step re-reads independently of `apply`'s own | `LEG` | run a stage whose `apply` read-back is faked green | `verify` signs on `apply`'s read-back; any stage's proof read from a response code | M |
| MG-11 | `migration.md#gaps-and-contradictions-the-mapping-exposed` | — | — | findings, not rules: the ones this suite's rows also hit are cross-referenced by number above (G1, G7, G8, G11, G12, G13, G15, G17, G21, G25); G1, G6, G7, G8, G11, G12, G13, and G15 are closed on both sides |
| MG-12 | `migration.md#the-decisions-this-document-opened-and-how-each-was-ruled`, `migration.md#how-a-registered-entity-type-is-renamed-on-a-live-record`, `migration.md#where-a-skills-harness-mechanics-live`, `migration.md#the-policy-values-leg-two-needs-and-the-two-postures` | — | — | decision 31 ruled 2026-09-06 (the merge form; MG-2 is its row) and decision 42 ruled 2026-09-06 (MG-13 is its row); the policy values are the operator's under ruling 18, and MG-4 tests the reserved default |
| MG-13 | `migration.md#the-skills-source-state-the-harnesses-hold-and-where-each-kind-goes` and its parts (`migration.md#five-classes-of-skill`, `migration.md#role-skill--agent-and-what-the-file-becomes`, `migration.md#procedure-skill--workflow-declaration-step-or-adapter-operation`, `migration.md#standing-rules-inside-skills-go-to-task_policy-by-kind-and-never-by-value`, `migration.md#the-duplicated-procedure-and-what-the-roles-collapse-to`, `migration.md#ordering-and-the-cutover-for-the-skills`): a skill is source state and never a target; a role skill rides the `agent` row and its file becomes a render target; a procedure skill maps to a declaration, a step, or an adapter operation; standing rules go to `task_policy` by kind, never by value; the duplicated per-role procedures collapse to the one declared review step, predicates becoming `applies_when`; the cutover is declaration, dual availability, one closed batch, then retirement | `LEG` plus a skill inventory of the five classes | run stage 11 | a runner loading a role from a skill file after the cutover; a `workflow` or `agent_policy` entity minted from a skill with no governance write; an operator value written into an `agent_policy` where a `task_policy` kind was the target; two declared review steps for one collapsed procedure; a skill retired before its target was read back as available; after the cutover, a runner invoking a tool its grant does not name, or a harness preference or model tier read from an `agent` entity or a skill file rather than a `vendor_binding` | M (decision 42 ruled 2026-09-06 — `migration.md#the-format-gap-where-a-skills-harness-mechanics-go`, `migration.md#where-a-skills-harness-mechanics-live`) |
### `vocabulary.md`

Every **Never** item is a rule, and there are ninety-two as the checker parses them. They fall into four
classes by what fails, and each item is tested by its class; the item list is the checker's own parse of
the document, so a term added there is tested without a row being added here.

| # | Class of Never item | Members | Red when | Class |
|---|---|---|---|---|
| VO-1 | retired **type or field names** that must not exist in the record | `workflow_definition`, `participation_record`, `step_run`, `workflow_run`, `checkpoint_brief`, `execution_policy`, `agent_definition`, `gate_status`, the `escalation` type name, and every name in `vocabulary.md#retired-names` marked as an entity | the disposable instance's census after a full run holds any of them; any field named `gate_status` or `owner` alone on a registered type | M |
| VO-2 | retired **status and state values** | `executing`, `running`, `in flight`, `routed`, `expired and released`, `surrendered`, `stuck`, `stranded` as values | any observation carries one as a status, lease state, or liveness value | M |
| VO-3 | words banned in **prose the swarm emits**: prompts, error messages, checkpoint text, announcements | the remainder of the ninety-two, plus the checker's regex bans | the vocabulary checker, pointed at rendered agent prompts, at error strings in the reference implementation, and at the text of every checkpoint and announcement the run produced, reports a hit | M (the checker exists for documents; its extension to emitted text is a contract) |
| VO-4 | the **Verbs** table and the **Owner** table | the phrases in their "Not" columns; `owner` alone | as VO-3 | M (same contract) |

The vocabulary checker's own zero-hit assertion over this directory is `test_foundation.py`'s, and it
covers this document too.

### `scenarios.md`

The walkthroughs are not rules and get no rows of their own: a scenario is the kernel in motion, and the
rows below are the ones that go red when the walk it describes is violated. A scenario no row explains
would be a gap in this matrix; a ruling no scenario walks is a gap in that document, and five are listed
after the table.

| Walkthrough | Rows exercised |
|---|---|
| `scenarios.md#a-create-claim-execute-return-complete` | WM-5, WM-7, WM-8, WM-9, WM-11, PR-2, PR-11 |
| `scenarios.md#b-lapse-re-claim-repeated-lapse-checkpoint` | WM-17, WM-18, FP-15, FP-16, FP-22, GW-49 |
| `scenarios.md#c-assignment-then-the-named-principal-claims` | WM-1, WM-2, WM-4, WM-15 (X-6 closed: a principal writes `assigned_to`) |
| `scenarios.md#d-several-tasks-enter-one-workflow-as-a-batch-review-through-release` | WM-26, WM-27, WM-33, WM-34, GW-3, GW-3a, GW-12, GW-31, GW-35, GW-36, WF-9 |
| `scenarios.md#e-a-task-detached-from-a-batch` | WM-26, WM-33, WM-34, PR-11 |
| `scenarios.md#f-a-parent-task-with-children-in-independent-batches` | WM-14, WM-35, PR-11 |
| `scenarios.md#g-an-operator-only-task-claimed-by-the-operator-facing-agent` | WM-19, FP-21, GW-40, GW-46, GW-47, AU-16, PR-5 |
| `scenarios.md#h-an-action-discovered-mid-workflow-at-never-high-and-low` | GW-36, GW-38, GW-40, GW-44, PR-5 |
| `scenarios.md#i-neotoma-unreachable-halt` | FP-1 to FP-5, FP-15, FP-20, WM-18, AU-1, PR-2, PR-7 |
| `scenarios.md#j-a-task-created-routed-by-intake-and-entering-its-successor` | WM-12, WM-13, WM-27, GW-31, GW-32, WF-3, WF-5, WF-6, WF-7 |
| `scenarios.md#what-the-scenarios-do-not-show` | each absence is a row's red condition: WM-1 (a router), WM-3 (delivery by any path but a claim), WM-17 (a process returning a lease), WM-33 (an artifact as a step's subject), GW-3 (a per-step status row), WM-35 (a parent claimed), GW-44 (an action outside the gate), WM-9 (a stored liveness flag), GW-38 (a gate consulted on anything but an action), WM-13 (a non-intake batch with no intake before it), GW-31 (two successors), GW-49 (a second queue), GW-32 (an entity above the batches) |

**Rulings no walkthrough shows**, each a scenario that document could add and each already a row here: a
step holding on a discovered condition and the three ways the hold ends (decision 13; WM-31, WM-31a); a
batch depending on a task it created, and a cycle refused (14; WM-32, WM-32a); a recurring task's
completion creating the next instance, dated from the schedule (30; WM-35a, WM-35b); an inbound delivery
resolving through identity to each of the four outcomes and to `dropped` (AD-2, AD-3); and a governance
write held at the gate under the reserved default (18; WM-22a). Every walkthrough but (b) and (i) is a
happy path; the axes below are where the unhappy ones are argued.
## The permutation axes

The matrix is one row per rule. Many rules hold across a space of conditions, and the space is a product
the suite cannot enumerate. This section names each axis, argues which cross-products are load-bearing —
the combinations where the design's answer *differs* from a neighbouring combination, or where the failure
would be silent — and says why the rest are dropped. A combination is dropped when the design's answer to
it is the same as to one already kept and the mechanism producing the answer is the same; testing it
again would ratify the same code path twice.

### Inbound

Five adapters × five outcomes (the four, and `dropped`) × six delivery conditions (first delivery,
redelivery, out of order, malformed, an unbound credential, a credential bound to a principal in neither
role) is one hundred and fifty combinations. Kept, about forty:

- **Identity × the verdict-shaped delivery, for every adapter, from all four credentials** (twenty). The
  sign-off is the only dangerous outcome and identity is the only thing that decides it, and each system
  presents identity differently — a login, an address with authentication, a chat id in a group, an
  organizer's address, a rail's approver. The same test on five systems is not repetition, because the
  binding is resolved from five different credential kinds (rows AD-3, GH-1, GM-6, TG-2, TG-6).
- **Redelivery × the decision-carrying outcomes, for every adapter** (ten): a redelivered sign-off and a
  redelivered resolution must land once, and each system's delivery id is different in kind — a webhook
  delivery id, a history marker plus a message id, an update id that jumps, a sync token, a per-delivery
  id or a transaction at a height (AD-11, GM-1, TG-7, CA-4, PY-7). Redelivery × observation is kept once
  (one adapter), because the idempotency mechanism is the same write path.
- **Out of order, one per system, at its own ordering hazard** (five): a `synchronize` arriving after the
  review it invalidates (GH-7); a history interval read twice at different granularities (GM-1's dedup
  note); an update id lower than the last seen (TG-7); a sync token invalidated (CA calendars table); a
  transaction observed at a lower height after a higher one (PY-3). Every other reordering the design
  answers with "observations are the history and the current value is the latest by the source's time",
  which is one mechanism.
- **Malformed × the field the mapping keys on, one per system** (five): the outcome must be `unknown` or
  `dropped` with a reason and never the nearest outcome (AD-12, GH-10). Malformed payloads on other fields
  reach the same branch.
- **An unbound credential × a new-record event, for every adapter** (five), because the design's answer
  *differs by system*: a stranger's issue is a task for intake (GH issues table); a stranger's mail is a
  task (GM-2); a stranger's chat message is never a task (TG messages table, `unbound_credential`); a
  stranger's calendar invitation is a task; a stranger's incoming payment is a task where no obligation is
  tracked. The chat channel's answer is the one a reader would get wrong.
- **A credential in neither role × every outcome that is not a sign-off**: dropped, because the identity
  rule's answer is "observation" regardless of the payload and AD-3 covers it once per system.

Dropped in total: about one hundred and ten, each because its branch is one already kept.

### Outbound

Action class {low-listed, high-listed, `operator_only`, unclassified, absent, a governance class, a lossy
mutation} × policy {present, absent, `operator_only` set, a policy attempting to demote} × confirmation
{returned, lost, contradictory}. Kept:

- **The whole class × policy product at the gate** (GW-40, GW-41, WM-24): every cell has a distinct
  answer and the order among them is load-bearing by the design's own statement. That is about twenty
  cells and none is dropped.
- **Confirmation lost, once per adapter** (AD-15, GM-7, TG-8, CA-4, PY-2): the reconciliation read differs
  per system, and on the payment rails it differs per rail class. Confirmation lost × class is dropped:
  the class decided whether the action was taken, and reconciliation begins after that decision.
- **Confirmation contradictory, twice**: the operation returned success and the read-back finds nothing —
  the action reads `unknown`, never confirmed (AD-15); the operation returned failure and the read-back
  finds the effect — confirmed, because a refusal on an existing key is stronger evidence than a success
  response (`failure_posture.md#the-rules`, rule 6; PY-2's "adopted, never re-issued"). Two, on the two
  systems where the second case is most likely: the rail and the mail system.
- **Policy unreachable**: one test, a halt and not a fallback policy (GW-40's last clause, FP-1).

### Failure

Each failure mode × the mechanism that must make it loud × the silent variant the design forbids. The
silent variant is the negative test and every mode has one:

| Mode | Loud mechanism | Silent variant, which must go red |
|---|---|---|
| the record unreachable at claim | no claim; `CH` announcement per window (FP-1, FP-3) | a claim from a cached read; a halt with no announcement |
| the record unreachable mid-task | the prior state; the lease lapses; re-derived on return (FP-5) | a verdict parked on the host, on disk, in a chat, and replayed |
| health green, reads hanging | no claim; the probe is a read (FP-4) | a claim after a green health check |
| the policy source unreachable | halt (GW-40) | an empty low-blast set as the fallback policy |
| the workflow unreadable | one checkpoint `unreadable_workflow`; zero steps (GW-42) | steps from a code list; an empty tuple proceeding |
| a role resolving to nobody | a checkpoint at declaration and at claim (FP-19, WM-2) | fallthrough to any available agent |
| an adapter read failing: transient, persistent, rate-limited with a stated reset | backoff per system; the stated time honoured; hold then `undeclared_dependency` (FP-9, GW-9) | an empty result; a fixed-interval retry; a retry before the stated reset; two schedules for one system |
| a mechanism failure: no provider matched, a transport reset | retry with backoff; never a task failure (FP-9) | a false terminal state on the task |
| repeated lapse | one checkpoint at the cap carrying the count and the last lease holders (WM-17) | a re-claim forever; a lease returned by a process |
| a deferral exhausted | one checkpoint `rounds_exhausted` (FP-6) | the loop continues |
| an unclaimed step | one checkpoint after the declared interval, against the role (FP-18) | the step closed by time; a checkpoint with no interval declared |
| the operator's halt | confirmed by absence of live leases (FP-10) | confirmed by the command's return |
| the announcement path down | the local capture holds every window; on the path's return each is announced with its original time (FP-23) | a gap on the path after return; a capture without the windows; a second channel |

### Work model

Lease {claimed, renewed, lapsed, returned} × step {open, closed} × batch {one task, many, a dependency the
batch created, a condition discovered mid-flight, a cycle}. Kept:

- **Lapse while a step is claimed**: the step reads open (claimable again); a second runner of the same
  role claims; the first runner's late sign-off is refused — decision 44, ruled 2026-09-06 (U-24 closed):
  the late write does not land, the second runner's verdict is the one that can stand, and two verdicts from
  two runners on one head is the failing artefact.
- **Return with the step still open**: the lease holder ends the lease without signing; the step is claimable
  and unsigned; nothing closes it (GW-17).
- **Renewal past the step's close**: a sign-off written, then the lease renewed — the lease on a signed
  step is meaningless and the renewal is an observation with no effect on step state; kept as the negative
  that a renewal never reopens a step.
- **A batch of one versus many**: the design says there is no separate single-task path, so every row
  above is run once with a batch of one and once with three (WM-26), and the observables must be identical
  in kind. The remaining lease × step cells are dropped for many-task batches: the lease is on the step of
  the batch, not on a task, so the task count does not enter the mechanism.
- **A discovered condition, a dependency the batch created, a cycle** (rulings 13 and 14): a hold × each of
  its three ends (WM-31, WM-31a); a dependency × a sign-off attempted before and after the edge is ended
  (WM-32); a cycle × the three ways it arises — at the dependency write, at an attach, and planted after
  both (WM-32a). A hold × a batch of many is dropped: the hold is on the step, and the task count does not
  enter it. A dependency chain deeper than two is dropped as a positive test — the ruling names depth as
  what would reopen it and gives it no bound — and kept as the negative that no bound is silently applied.

### Multiplicity

Thread versus message; series versus occurrence (rulings 23 and 24: both levels are artifacts, the
contained one `PART_OF` the containing one). Kept: linkage at each level in each direction — a signal to the
unit whose id it carries, an action to the unit its operation needs, a task to the unit it names (GM-9,
CA-7); what a sign-off pins at each level — a message outright, a thread's message set with its coverage, an
occurrence's dated fact, a series' declaration as read; and the system moving a member between containers —
a regrouped message, an occurrence moved to another calendar (which stays unhandled) — as an edge ended and
another written, never a re-identification. Dropped: the cross-product with every inbound row, because the
level a row lands on is stated per row in the system documents and the generated disposition test asserts
it; and the transfer and chat-message kinds, which have one level and whose pinned state — the rail state as
read, the message itself — is tested under DM-6 with the rest (U-21 closed).

### Governance

Five classes × reserved/granted × self-initiated/operator-initiated. Kept:

- **Every class × no policy value** (five): each resolves to `NEVER` as `operator_only` (ruling 18;
  WM-22a). The five are kept separately because the five types are the closed list and a mutant that
  forgets one is the realistic one. The recursion — the class covering `action_policy` writes is itself
  reserved (WM-22b) — is kept as its own cell because it is the one the ruling says to grant last, if ever.
- **`operator_only` × an attempted demotion** (one): WM-24.
- **Granted as high × self-initiated** (one per type is dropped to one): a checkpoint, then taken; and the
  self-widening grant (WM-23) as the case the rule most exists for.
- **Operator-initiated after bootstrap** (one): decision 43's second half, ruled 2026-09-06 with 47 — the
  operator's own write to `action_policy` once the gate exists is held, and resolved by the operator as a
  marked self-resolution through the engine's sole grant (decision 56); the mutants are the write landing
  with no checkpoint, the resolution landing without the mark, and the write made by the operator's own
  credential rather than the engine's (WM-25; AU-17; GW-33a).
- Reserved × operator-initiated and reserved × self-initiated resolve identically (`NEVER`, held, the
  operator resolves), so they are one test, not two.

### Recurrence

Ruling 30: one live instance; completion creates the next; `FOLLOWS` task to task; the date from the
schedule. Kept: **duplicate-on-completion** — the closing sign-off of the last batch creates exactly one
instance, with the edge and the copied rule (WM-35a), and its failure modes, a write that fails between the
creation and the sign-off in either order (U-28 closed: create, read back, then sign); **the schedule-computed due date** across three cases — on time, late within the
period, late past the next grid point — with and without a rule that forgives a missed occurrence (WM-35b);
**the silent-stop negative** — an instance whose batch stops advancing is readable as one overdue task and
reaches the queue through `unclaimed_step`, and a workflow with no declared interval is the declaration
defect the ruling names (WM-35d); the series' end and a postponement, each of which must create nothing
(WM-35c); and the seam with an action series (WM-35e). Dropped: recurrence × every workflow — the instance
is an ordinary task and its workflow is intake's choice, so one code workflow and one payment workflow
suffice; and recurrence × a batch of many — instances are peers and never share a batch, by the
one-live-instance rule itself.

### Adapter admission

One reference adapter — the "sixth" — that satisfies all six obligations (AD-21 to AD-26 green), and six
negative variants, each failing exactly one obligation, each of which must turn exactly one row red:

| Variant | Obligation it violates | The row that must go red |
|---|---|---|
| no drop counter wired; an unmapped event logged and discarded | 1 | AD-21: no `dropped` with `unmapped` counted |
| an unrecognized credential resolved to the operator | 2 | AD-4 and AD-22 |
| a per-attempt token as the inbound key; a fresh key per outbound attempt | 3 | AD-11: two writes on redelivery; a second effect on re-take |
| a `last_seen` cursor file | 4 | AD-17 |
| acknowledges before the read-back; acknowledges during a halt | 5 | AD-13 |
| an outbound class absent from the policy, and the action taken anyway | 6 | AD-26 |

A variant that turns more than one row red is a finding about the rows (two observables for one rule); a
variant that turns none red is a finding about the obligation (no failing artefact) — and the admission
section's claim that every obligation has one is thereby itself under test.

## The simplification pass, verified against the matrix

The simplification pass (revision 29) removed eight things and proposed four, and marked every removal
unverified against this matrix because the matrix had not landed. Its mechanism test was: a mechanism is
redundant if removing it changes no row's failing artefact. This section runs that test now, per removal,
by reading every row that had cited the removed thing and asking whether the row's observable still exists
through something else. Where a removed word must be named below, it is named as retired.

| Removed | Rows that cited it | Verdict |
|---|---|---|
| `claimant`, retired for lease holder | WM-5, WM-17, FP-15, WF-20, and the work-model axis | **lossless.** Every observable is a read of the `LEASE` edge — which principal it names, whether it reads `held` — or of the `repeated_lapse` checkpoint's payload, the count and the last lease holders. No row keyed on the word, and the claim primitive already made the principal that claimed and the principal the lease names one referent |
| `workflow policy`, retired for the declared step owners with the grants in force | GW-35, GW-33, WM-22, U-14, and the bootstrap's circularity paragraph | **lossless.** GW-35's artefact is a lease landing for a principal the roster does not resolve to the step's role, or a grant check permitting it; both existed before the term and neither read it. The section rename changed the anchor those rows point at and nothing else |
| `hot path`, retired for the projection's own definition | none, and that was the finding | **lossless, and it exposed a missing row.** No row had cited the term, and the rule the term sat beside — the projection is proved equal to the sign-offs by a reconciler, and neither is a second source of truth — had no row at all; GW-3a is added, and its artefact (a reconciler that reports agreement on a projection mutated to disagree) never depended on the term |
| the checkpoint reason-class list, stated in four places, now one | FP-16, X-2, DM-1 | **lossless, and it closed X-2.** The four copies were recorded as a contradiction: a registry built from the data model's copy refused five classes the failure posture required. With the data model citing the one enumeration, FP-16's artefact is a reason value outside that enumeration and the policy's declared set, and X-2 has nothing left to hold; its number is kept and marked closed |
| the `feedback` entity's row in the direction-of-truth table | GW-26, CF-4 | **lossless, and it closed a contradiction this matrix had missed.** GW-26 turns red on a second entity type carrying the operator's input beside the finding; the table row named exactly such a type as a home. The pair was never listed under *Contradictions*; removing the row resolved it in the finding's favour, which is the direction GW-26 already tested (`migration.md`, G14) |
| `operator_preview`, retired as a step name for `consent` | WF-14, GW-26 | **lossless.** A step name is data on the declaration; the observable is the `publish` action's checkpoint carried at the step and resolved by the operator, and the `approved` fast path skipping the step only where the gate would not checkpoint. Two workflows each declaring a `consent` step do not collide, since a step is named per declaration |
| `calendar_routing_config`, retired as a binding type name for `channel_config` | CA-6, and the bootstrap's context-entity gap | **lossless.** CA-6's artefact is a read in the fake calendar of a calendar the binding does not name; which type the binding is does not enter it. The context-entity gap is one entry shorter |
| `scenarios_extended.md`, merged into `scenarios.md` | none | **lossless.** Walkthroughs are not rules and no row's observable lives in one; the scenario table above maps every walkthrough to the rows it exercises, and the mapping is indifferent to which file holds it |
| a checkout's field name in the Owner table | VO-4 | **lossless.** The ban tested is `owner` alone, and the table's other rows carry it |

**The four proposals, read against the matrix.** Each was opened as a decision because the pass judged
that its guarantee coverage would shift rather than be exactly preserved. The matrix can now say what
would shift.

- **32, the `verdict` field.** GW-19 (a contradictory verdict refused at submission) and GW-20 (three
  values; a host's token refused) are the rows a retirement would delete: a value that cannot be written
  cannot be refused, and a rule true by construction has no mutant. GW-21 (terminal, never revised) keeps
  its artefact through the sign-off's immutability. The guarantee moves to two places: DM-1 would refuse a
  `verdict` field on the registered type, and a derived verdict would rest on the finding's shape — which
  the data model now declares (U-6, closed by the testability pass; `migration.md`, G15 closed). The
  matrix's reading was that the retirement was blocked on U-6 and not on the field; that blocker is gone,
  the refusal at submission (GW-19) is no longer the only mechanical control the findings have (DM-27 is
  another), and the decision was ruled on its own question on 2026-09-06: the field stays
  (`gates_and_workflows.md#whether-the-verdict-is-a-stored-field-or-a-read-over-the-findings-and-the-author`);
  GW-51 is closed above.
- **33, the stage and the `phase` field.** No row observes either; U-30 records it. By principle 4's own
  test the field is decoration, which is the pass's argument stated as a finding. Ruled on 2026-09-06: the
  `steps[].phase` field is dropped and loses no row here; the Stages line stays as authored prose.
- **34, the step path as a mechanism, and the engine's name.** WM-36's observables are per
  credential — the engine's credential never writes `task.status`, a daemon's never holds a lease — and
  neither depended on whether the mechanisms are counted as three or four, or on which name the publisher
  carried. WM-38 held the decision as pending; the matrix was indifferent to it, and stays indifferent, since
  the ruling on 2026-09-06 (`engine` defined, `pipeline` retired for this sense, the count of four
  unchanged) renames the publisher without changing what any row observes. What would not be indifferent is
  the sentence that a roster role reachable by none of the mechanisms cannot receive work, which is a
  measurement in `status.md` and not a row here.
- **35, one binding type or two.** Four rows read a binding — TG-6 (which chat receives what), CA-6 (which
  calendars may be read), PY-3a (the terminal depth), and the bootstrap's step 13 — and every one reads it
  as the binding entity, with no observable that differs by type. The matrix finds no rule that reads
  `channel_config` and `vendor_binding` differently, which is the condition the pass named as deciding it.
  Decision 35 was ruled settled by this reading on 2026-09-06
  (`adapters.md#whether-one-binding-type-or-two-names-an-external-systems-instance`); AD-35 is closed above.

**The defects the pass noticed and left, placed.** The governance list stated three ways was X-16
(`migration.md`, G1), closed by revision 31. The `unclaimed_step` checkpoint's subject was X-1, closed by
the same. The standing-finding ruling routing an operator-specific preference to an agent's prompt was
U-29, closed by the same.
## Relationship to the code

**The suite is all red against the current code by construction.** It is the acceptance suite for the
branch that implements the foundation, in the same way `test_foundation.py`'s reading-budget test is the
expected failure recording a decision the operator owns. A row that passes before that branch exists is a
row that tests nothing, and it is treated as a defect in the row (principle 4), not as progress.

Nothing in this document is shaped by what the code does. Where the code most obviously cannot pass is
drift, and drift is state: it is recorded in `status.md` under this revision's section, dated and with
its instrument, never here. What this document says about the code is one sentence: the suite judges it,
and the design the suite is derived from does not consult it.

**How a code branch uses the suite.** A row's setup is what the branch must be able to create from zero;
its action is what the branch must do; its observable is what the branch must make readable. A branch
turns rows green one document at a time — the bootstrap set first, because nothing else can run without it
— and a row that cannot be turned green without changing a rule is a proposed change to the foundation,
made through a PR that says so (`conformance.md#amending-a-foundation-document`), never a change to the
row.

## The decisions this document opened, and how each was ruled

Two, registered in `conformance.md#the-register-of-open-design-decisions` in the change that opened them, in
the idiom the ruled decisions were opened in, and numbered from the register's then next free number, which
is why the bootstrap question and the lapsed-lease question carry 43 and 44 and not the numbers the register
has since assigned to other questions. Both are ruled below — 44 whole, and 43 in both halves, the second with 47 — and
the rows that waited on them are updated in the matrix above.

### What the bootstrap set is, and whether the operator's later governance writes are gated

**Ruled (decision 43, 2026-09-06, in two halves): the bootstrap set is closed and enumerated — the list is
the thirteen-record table in `#the-minimal-record-set-in-order` — and the operator's own governance write
after bootstrap is gated like any other.** Registered as ruled in
`conformance.md#the-register-of-open-design-decisions`. Every member of the set is written as the operator
principal and read back (principle 2); a write to any of those thirteen kinds of record *after* the set
exists is not provisioning, whoever makes it, and may not call itself so; and the operator's write to a
governance type once the gate can hold is an action of its class — held, resolved by the operator, and marked
as a self-resolution on the checkpoint (decision 47,
`authority_model.md#the-raiser-of-a-checkpoint-does-not-resolve-it-and-the-operators-self-resolution-is-marked`)
— so that what the record holds afterwards is an audit trail and never a block. The enumeration half was
ruled first and the gated half with 47, each with its reasons below.

**The question.** `work_model.md` settles that the first workflow declaration is an operator act, out of
band, and stops. A from-zero run has to write thirteen kinds of record before the first task can be
routed, and seven of them are governance writes. Which of them are provisioning — operator acts outside
the gate — and which are gated writes held against an absent policy? And once the set exists and the gate
can hold, is a governance write by the operator principal an action like any other, or is "operator act" a
standing exemption?

**If the set is enumerated and closed, and the operator's later writes are gated**, every governance write
after bootstrap has an action, a class, and a checkpoint, and the record of how the swarm was changed is
complete from the first task onward. The operator approving their own write raises the raiser-resolves
question `authority_model.md#approval` leaves open (decision 47), and a solo operator approving their own policy changes
is ceremony with one interest — Clark-Wilson's caveat, already named there. The cost is that ceremony, and
the risk that the checkpoint queue becomes where the operator approves themselves in bulk.

**If the set is enumerated and closed, and the operator's writes are exempt**, the operator changes the
swarm directly and the record holds the write with its attribution but no action, so the audit of
governance changes has two shapes: gated for agents, bare for the operator. The cost is that the exemption
is a class of write with no gate, which is the side door the no-side-door rule closes for tasks; the
argument for it is that the operator is the principal every checkpoint already resolves to, so gating
their write adds a step whose approver is its author.

**If the set is not enumerated**, the design has what it has now: an unbounded class of writes that may
call themselves provisioning, and a suite that cannot say which of its bootstrap writes conform.

**What would decide it:** whether the record of the operator's own governance writes is wanted in the
same shape as the agents' — which is a question about what the operator wants to be able to read back
later, and is theirs. The enumeration half has no second candidate: an unbounded set is the defect, so the
only open part is what the list contains, and this document's bootstrap table is the proposal.

**Why the list, and why this list.** The enumeration half had no second candidate, as the question said
when it was opened: an unbounded set is a side door with no boundary (principle 1; finding U-13), so the only
open part was the list's contents, and the table above is the list the documents derive — each row marked by
whether the documents settle it or the suite had to choose, each with the dependency that places it and the
read-back that proves it. Ruling the set closed turns three of the table's marks from open to derived: step
1, the registry, is the first member of the set and an operator act by membership; step 5, the first
`action_policy`, is a member, and ruled decision 18's "by writing the policy themselves" describes the
bootstrap write of it; step 6, the first agents, is a member for the same reason. `migration.md`'s leg one
enumerates the same set for a populated instance, and the two enumerations were read side by side above
(`#against-migrationmds-bootstrap-leg`) and agree on membership. The attribution mark on step 2 and the type
gap on steps 3 and 8 are unchanged, since they are gaps and not this decision's.

**The gated half, and why it is the design's and not the operator's.** Once the set exists and the gate can
hold, the operator's write to `action_policy` — or to any governance type — is an action of its class, held at
the gate and resolved by the operator in the shape revision 36 gave the `operator_only` action, where the
resolution is the decision and never the confirmation. The no-side-door rule "is not conditioned on who
created the task or why" (`work_model.md#changing-the-swarm-is-work-and-it-goes-through-a-workflow-like-any-other`),
and the operator writing governance directly is the side door: the one class of write with no gate, which is
the fail-open direction (principle 5) and a second audit shape for one class of record — gated for agents,
bare for the operator (principle 9). Ruled decision 18 makes the loosening a deliberate write with an author
and a date, and a gated write is exactly that record; the governance axis below already reads reserved ×
operator-initiated and reserved × self-initiated as one cell. What the question had left to the operator —
whether he wants his own writes readable in the agents' shape — is answered by what the alternative would cost
him to read back: an exempt write is one he could not later tell from a write nobody decided. The gate's
checkpoint on such a write is resolved by the operator and marked self-resolved (decision 47), which is what
turns the ceremony into a countable row rather than a wall. The enforcement point through which the operator's
write then passes is decision 56's
(`gates_and_workflows.md#where-the-enforcement-point-for-a-governance-write-sits`): the engine's sole grant,
never a second key on the operator's credential.

**Cost accepted, for the gated half.** One extra step on the operator's own governance changes — the
self-resolution, presented in the queue's set form so that several like changes are one decision and one row
each (`gates_and_workflows.md#the-checkpoint`) — and the risk the question named, that the queue becomes where
the operator approves himself in bulk, which the mark makes countable rather than invisible.

**What would reopen the gated half.** A governance write the operator must make while the gate cannot hold —
the record reachable and the engine not — which is a halt-shaped condition, and its answer is the announcement
path and the bootstrap table, never an exemption.

**Cost accepted, for the enumeration half.** The thirteen records are the whole of provisioning: a fourteenth kind
of record that must exist before the first task can be routed is a change to this table through a pull
request, never an act that calls itself bootstrap. A populated instance's leg one (`migration.md`) carries two
additions derived from what is live there, and those are the migration's, tested on its legacy fixture
(MG-6), not members of the set.

**What would reopen the enumeration half.** A from-zero run that cannot route its first task with exactly these
thirteen written — a missing member, added to the table by the change that finds it.

**The suite** treats the thirteen records of the bootstrap table as the set, writes them as the operator
principal, reads each back, and tests the operator's later governance write under the governance axis as a
held action resolved by a marked self-resolution (WM-25; AU-17).

### Whether a sign-off from a step owner whose lease has lapsed closes the step

**Ruled (decision 44, 2026-09-06): a sign-off carrying `signed` or a blocking verdict requires a held lease
by its signer at the moment of the write.** Registered as ruled in
`conformance.md#the-register-of-open-design-decisions`. A sign-off written on a step whose lease the signer
does not hold — lapsed, returned, or held by another runner — is refused at submission, an error and never
swallowed, and the current lease holder's verdict is the one that can stand. The record cannot hold two
verdicts from two runners on one step at one head. The operator principal's `waived` is the one sign-off
written without a lease: it closes a step the operator does not own and never claimed, and it is already the
one verdict a principal other than the step owner may write
(`gates_and_workflows.md#declaration-batch-projection`), so its rule is its own and this one does not reach
it.

**The question.** A step is claimed with a lease and closed by its owner's sign-off, and the two are
stated separately. Nothing says the second depends on the first. So a runner whose lease lapsed, and whose
step a second runner of the same role has since claimed, may still write a sign-off — and under "the latest
per step owner per artifact head stands", both verdicts are the step owner's and the later one wins,
whichever runner wrote it.

**If a sign-off requires a held lease by the signer at the moment of the write**, the lease becomes
load-bearing for correctness and not only for exclusion: the late runner's write is refused, the second
runner's stands, and the record cannot hold two verdicts from two runners on one step at one head. The cost
is that a slow but honest sign-off written a second after expiry is refused and its work re-derived, which
is the at-least-once cost the design already accepts for actions.

**If a sign-off is accepted regardless of lease state**, the lease is purely about who works and never
about whose verdict counts; two runners' verdicts on one head are both the owner's and the latest stands.
The cost is a zombie writer: a runner the swarm believed dead writes a verdict on work it read before the
head moved, and the pinning rule catches the head but not the runner.

**What would decide it:** whether the design wants the lease to be the thing that makes a verdict the
*current* owner's, or whether attribution to the role is enough. The claim primitive's own rationale —
"two agents must not both take a task" — reads as the first; the sign-off rule's silence reads as the
second.

**Why the lease is load-bearing for the verdict.** The claim primitive's own rationale is the first
reason: "two agents must not both take a task", and "an agent holds only if the persisted lease names its
runner id" (`work_model.md#the-claim-and-the-lease-are-one-primitive`; principle 2, the read-back of the
holder). A sign-off is the close of work held under a lease; a close from a lease not held is a close from
nothing held, and a runner that was not holding the step was not its step owner at the write, whatever role
it resolved to. `failure_posture.md#the-rules`, rule 4, already forbids this writer under another name: the
step owner whose write failed does not replay its verdict later — "the work is re-claimed when the record
returns … The verdict is re-derived then, not replayed" — because the artifact may have moved under it; a
late sign-off from a lapsed lease is that replayed verdict arriving by another road, and the pinning rule
catches the head but not the runner. Ruled decision 13 ties holding a step to a renewed lease. And principle
11 supplies the reason this costs no new state: the lease is already the edge that says who is working, so
making the verdict depend on it is a read at the write — is the signer's lease `held` on this step now — and
not a field, a flag, or a process. The other branch would have left the lease as exclusion only, and let a
runner the swarm believed dead write a verdict on work it read before the head moved.

**Cost accepted.** An honest sign-off written a second after its lease expired is refused and its work
re-derived by whoever claims next — the at-least-once cost the design already accepts for actions
(`work_model.md#at-least-once-implies-effect-dedup`), and the cost the question named. A step owner that
wants its sign-off to land renews its lease until the write returns, which is what every claimed step already
does between claim and sign-off.

**What would reopen it.** A step whose sign-off must be writable by a principal that cannot hold its lease.
None is known: the operator's `waived` is the one such write, and it is excepted above by the rule that
already governs it.

**Matrix.** U-24 closes and the work-model axis's one missing cell fills: a late sign-off refused is a
mechanical row, and two verdicts on one head from two runners is its failing artefact.

## Prior art

Mutation testing (DeMillo, Lipton, and Sayward's original framing; PIT and Stryker as the tools) is the
revert test at the level of statements: a test suite is judged by which mutants it kills, and a rule whose
mutant survives is untested. This document lifts the unit from a statement to a design rule and names the
mutant per row. Conformance suites in the sense the word is used here — the Web Platform Tests, the OCI
runtime and distribution conformance suites, Kubernetes' conformance run through Sonobuoy — are the shape
of "an implementation is judged by a suite derived from a specification and not from an implementation",
and their all-red-against-nothing property at authoring time is the property this document claims.
Jepsen's method is the failure axis: partition the record, hang it, return stale reads, and assert on what
the system wrote rather than on what it said. Contract testing (Pact) is the fake-system half: each
external system is a stand-in with a log, and the adapter is judged against the log. Chaos engineering's
principle that a hypothesis must be falsifiable before an experiment is run is principle 4 stated by
another field. None of these is cited from the prior-art entity the other documents cite; each is general
knowledge, named as such.

## Beyond the sources

The whole of this document is the suite's, applying the six-obligation method of
`adapters.md#the-admission-contract` to every rule in the directory: the matrix, its four classes and the
rule that a rule with no observable is a finding and never a filled cell, the suite's three invariants
(every mechanical row names its mutant; every instrument has a planted positive; every observable is a
read-back), the three instruments, the four isolation layers and the requirement that each has refused
something, the bootstrap table with its derived and open markings, the findings, the
contradictions, the axes and their arguments for what is dropped, and the two decisions opened. The
bootstrap derivation and the two decisions are proposals to the operator and are marked as such; nothing in
them is ruled here. The reconciliation of the bootstrap with `migration.md`'s leg, the rows over that
document's writes on the `LEG` fixture, the scenario mapping, and the verification of the simplification
pass against the matrix are this revision's, and the verdicts in that section are read from the rows and
not from the pass's own reasoning.