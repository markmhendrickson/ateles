# Charter: what Ateles is for

**Authored companion (not on the review reading list):** the purpose the rest of this directory serves, and
the objectives that purpose decomposes into. States no rule the swarm executes, so no reviewer reads it to
judge a change; it is what a reader reads to know what the rules are *for*, and what an author checks a
proposed rule against when asking whether it belongs here at all.

**Kind:** foundation; states the design's purpose and never the state of a checkout. Which objectives a
given checkout serves, and where each is unbuilt, is `status.md`. **Derived from:** the README —
its opening statement of the category, its *Why this exists*, *Vision and execution status*, and *What the
design commits to* sections — and `docs/icp.md`, whose *Jobs they are hiring Ateles to do* is the source
of the five objectives below. Nothing here is new: each clause points at the sentence in one of those two
documents it restates. Amendment history: `revisions.md#chartermd`.

## Purpose

State, in one paragraph, what Ateles exists to do, and decompose it into the objectives that say what
achieving it would mean. The corpus states many rules and, before this document, nowhere stated what they
are collectively for. That gap is what this closes.

## Scope

The purpose, five objectives, and the three properties of delegation the design must enable. Out of scope:
how any objective is achieved (that is the rest of this directory), which of them a checkout has reached
(`status.md`), and the sequencing of the work (`docs/phases.md`, and the vision phases in the README).

**On the words "mission" and "objective".** Both are used here in their ordinary English sense and neither
is a term of this design. Both are already spoken for: decision 57 rules `mission` the topmost of five
planning levels — task → plan → project → strategy → mission — and rules an objective to be authored
content within a strategy record rather than a sixth level
(`planning_model.md#which-levels-an-instance-declares-and-what-it-calls-them`). Those name records inside
an operator's instance, planning the operator's own work; this document is about the software project that
instance runs on. Minting a second, overlapping sense of either word is exactly what principle 12 forbids,
so the document is titled a charter, adds no entry to `vocabulary.md`, and takes on the reader's ordinary
understanding of both words instead. Where the distinction could be missed, "the operator's mission" names
the planning level and "this charter" names the document.

## The purpose

**Ateles exists to make delegated action legitimate: to let work be initiated, executed, and approved by
humans and agents together, under authority that is explicit, scoped, and revocable, with every action
attributed to the principal it was taken for and reconstructable afterwards from one record.**

Every clause restates the README. *Delegated action* and the joint human-agent set are its opening
sentence: "where humans and AI agents jointly initiate, delegate, execute, review, approve, and reconcile
work." *Legitimate* is its own word for the distinction it draws: "Generic agent orchestration coordinates
*computation*. Ateles is built to coordinate **legitimate action**: who or what may act, on whose
authority, against which state, through which workflow, with which approvals, and under whose
accountability." *Explicit, scoped, revocable* is the authority commitment: "structured, scoped, delegable,
revocable, and inspectable." *Attributed to the principal* is the attribution row of *What the design
commits to*: "the agent that wrote and the principal it acted for; a shared bearer is not attribution."
*One record* is the substrate commitment the same section makes and `failure_posture.md` enforces.

**The problem it answers, in the README's words:** as execution becomes abundant, the binding constraint
stops being production and becomes **agency fragmentation** — "many humans and many agents can initiate and
execute work, but authority, accountability, approval, and reconciliation stay implicit — scattered across
chat threads, managerial memory, and convention." The purpose above is that sentence turned positive.

## The objectives

**The rules in this section.**

- Work reaches the swarm from anywhere the operator works, and is executed the same way whatever its kind.
- A principal acts only within an authority that is named, scoped, and readable at the moment it acts.
- What happened is reconstructable from one record, without a second log.
- Work that is safe to run unattended runs unattended; what is not stops at a decision the operator owns.
- A new capability is added by declaring an entity, not by writing bespoke glue.

Five, taken from `docs/icp.md#jobs-they-are-hiring-ateles-to-do`, restated as properties of the design
rather than as jobs a buyer hires it for. Each names the documents that carry it, so a reader can go from
the purpose to the rule that serves it.

### 1. Work reaches the swarm from anywhere, and is executed the same way whatever its kind

The ICP's *Operate*: to reach agents across products and life domains from one surface, "without manual
sequencing or re-explaining context each time." The design's commitment is stronger than one surface — it
is that the *kind* of work does not change the machinery. The README states it directly: "A blog post and a
code merge are gated by the same mechanism; non-code work is not a second system." Carried by
`work_model.md` (intake first; a task is executed only through a workflow), `workflows.md` (the declared
set), and `adapters.md` (the admission contract every inbound event passes).

**Where the objective outruns the rules, stated rather than assumed.** Intake is open at its source — no
source filters on subject matter, and the design writes no task-type registry a novel kind of work could
fail to match. `adapters.md#admitting-a-new-adapter` is written for "a sixth adapter," and a new workflow
is a governance write like any other. What has no stated behaviour is the joint between them: `route`
selects a successor from `workflow.successors`, and `workflows.md#intake` bounds that choice to "any
workflow in this document except intake; or none; or operator-only." The corpus names no finding, no
reason class, and no checkpoint for the case where none of them fits, though it defines both for
structurally similar dead ends. Work the design did not anticipate therefore reaches `operator-only` or
closes with none on a step owner's judgement rather than on a stated rule — which serves the objective's
letter, since the operator can always delegate it, and not its intent, since the swarm cannot execute it.

### 2. A principal acts only within an authority that is named, scoped, and readable where it acts

The ICP's *Govern*: "keep every agent identifiable, in-scope, and auditable." The authority tuple is the
README's: `principal + domain + scope + action + conditions + time`. Carried by `authority_model.md`
(grants, default-deny, the enforcement point) and `gates_and_workflows.md` (the action gate, taken per
action at the moment the action would be taken). Principle 5 supplies the failure direction — the
restrictive branch is the default of every classifier that carries a safety meaning — and principle 7
keeps `unknown` from resolving to permit.

**This objective is stated as a property of the design, and it is not a claim about any checkout.** The
distinction matters more here than anywhere else in this document, because the model is the most complete
thing in the corpus and the least enforced. `status.md` measures the gap per row, with its as-of date, and
the objective is met by the design regardless of what it reads; a reader who wants to know whether an
agent's authority is enforced today reads `status.md`, never this document, and the answer there is not
the same answer.

### 3. What happened is reconstructable from one record, without a second log

The ICP's *Remember*: "one durable, queryable record of everything the swarm and the operator know."
Carried by `data_model.md` (append-only observations with provenance) and principle 9 (one source, defined
once). The README's commitment row states the negative half that makes it binding: "append-only
observations with provenance; **no parallel log**." Principle 11 is the same rule applied to state — what
needs a watchdog to stay correct belongs in a relationship, not a field.

### 4. Work that is safe to run unattended runs unattended; what is not stops at a decision the operator owns

The ICP's *Trust the dangerous parts*: "let low-blast work run unattended while high-blast actions
checkpoint for explicit approval." Carried by `gates_and_workflows.md` (blast radius selects the gate; the
checkpoint) and `failure_posture.md` (what the swarm does when it cannot reach its own record). The
operator's attention is the protected input, stated at `principles.md`'s *Attention as the protected
constraint*: "operator attention is a finite, non-renewable input."

**This objective is the one the corpus states least directly, and the gap is named rather than papered
over.** The design says thoroughly what *stops* — the reason classes, every gate outcome, the halt. It
nowhere states the converse as a goal: that the number of stops should be as small as the risk allows, or
that a stop which is not necessary is a defect. Both halves of this objective are asserted here from the
ICP's job and the attention section; only the first half — what stops — has rules behind it. Note what the
attention section protects, precisely: the cost of each operator interaction, not the number of them.
Under principle 1's own test, nothing fails when the count of returns rises.

Two consequences the corpus states as accepted costs, not as oversights, and this document does not argue
past them. A class with no value in the project's `action_policy` "is not the policy default and not a high
tier, it is `operator_only`" (`work_model.md#changing-the-swarm-is-work-and-it-goes-through-a-workflow-like-any-other`,
decision 18), and the planning model accepts the same cost per level: "until a level is listed, every
amendment at it is a checkpoint the operator resolves." So an instance begins at its highest operator load
and is loosened by hand, per class. Whether the design should state an autonomy goal, against which an
unnecessary return to the operator could be judged a defect, is a question for the operator and is not
settled here.

### 5. A new capability is added by declaring an entity, not by writing bespoke glue

The ICP's *Extend*: "add a new agent or workflow declaratively (an entity + a grant), not as bespoke glue
code." Carried by `data_model.md` (agents, grants, and workflows are entities), `gates_and_workflows.md`
(a `workflow` declares), and principle 6 (extend the mechanism that already generalizes; do not build a
parallel one).

## The three properties of delegation

The operator states three properties the delegation this design enables must have: it must be possible to
delegate **comprehensively**, **securely**, and **autonomously**. They are not a fourth, fifth, and sixth
objective — they are adverbs on all five, and their use is as a test: a proposed rule that serves an
objective while working against one of the three is a defect the objective alone would not catch.

| Property | What it asks of a rule | Where the design answers it |
|---|---|---|
| Comprehensively | that the operator can delegate any kind of work, not only the kinds already built | objective 1; `work_model.md`, `workflows.md`, `adapters.md` — open at intake and at admission, unstated at `route` |
| Securely | that authority is explicit and enforced where it is exercised, not asserted | objective 2; `authority_model.md`, `gates_and_workflows.md` — stated whole; enforcement is `status.md`'s |
| Autonomously | that a return to the operator happens when it must and not otherwise | objective 4; `failure_posture.md`, `gates_and_workflows.md` — the returns are enumerated; their necessity is not |

**What the design is silent on, stated rather than filled.** The three are not silent in the same way, and
collapsing them would hide the one that matters. *Securely* is stated whole and enforced partly — a gap
between the design and a checkout, which `status.md` exists to measure and which no change to this
directory would close. *Comprehensively* is stated at both ends and unstated in the middle, at `route`.
*Autonomously* is the one genuine silence: the corpus enumerates what forces a return to the operator
without ever stating that an unnecessary return is a defect, so a proposal that adds a checkpoint can be
checked against no rule. This charter names the three; closing any of them is the operator's decision, not
this document's.

## Where this document sits

**Not in the kernel.** The kernel is three documents, and `conformance.md#always-read` states why the
number is what it is: "Three is a budget, not a count of what matters: Neotoma's reading list records that
a six-document always-read set consumed the reviewer's turn before any diff was read." A fourth would spend
that budget on a document stating no rule a reviewer applies to a diff, which is the trade the budget
exists to refuse.

**An authored companion, beside the kernel**, on the same footing as `scenarios.md` and `migration.md`:
read to orient, not to judge a change. That placement is what `conformance.md`'s own distinction already
provides for — the kernel read "as a description of what the swarm *currently is and can do*, independent
of any change in front of it — orienting before acting." This document is the first thing such a read
should reach, and the last thing a review needs.

**When it is read.** By a reader new to the directory, before the kernel. By an author asking whether a
proposed rule belongs in this corpus at all — the question the objectives above make answerable. Neither is
a per-change read, so this document is not keyed to a path in
`conformance.md#read-when-these-paths-changed`.
