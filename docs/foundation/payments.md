# Payments: the least reversible boundary, and what the design does when confirmation never returns

**Keyed document:** read when a payment adapter, a rail client, the payment workflow's steps, or this
document changes (`conformance.md`). **Kind:** foundation; states the design and never the state of a
checkout. **Derived from:** `adapters.md` (the two invariants, the four outcomes, and the five adapter
rules, which this document applies and does not restate; and the rule that an artifact exists only once its
external record does), `work_model.md` (artifacts; at-least-once implies effect dedup), `gates_and_workflows.md`
(actions are entities and only actions are taken; the action gate; blast tiers and the never-set; the
checkpoint), `authority_model.md` (credential custody by revocability; separation of duties; approval),
`workflows.md` (the payment workflow's five steps and its two disjoint roles), `failure_posture.md`
(recovery per action class; the rules on read-back, unknown, and bounded deferral), and the published API
surfaces of bank-transfer and crypto rails, read 2026-09-05, and PR #745 operator review (2026-09-05,
rulings 13–14, 16–18, 23–29: decisions 27, 28, and 29 ruled here). What is built is `status.md`. Revised by the second workflow-format pass of 2026-09-06 (revision 36: a purchase or a booking with a merchant is out of scope here, being an effect on the merchant's system and not on a rail).

## Purpose

Be the payment adapter in full, across the rail classes the design contemplates: what the rails hold as
artifacts, every inbound signal mapped to one of the four outcomes `adapters.md` defines or to `dropped`
with a reason, every outbound operation with its action class and what confirms it, and — the question this
document exists to answer — **what the design does with a submitted transfer whose confirmation never
returns**, where the effect may or may not have landed and re-submitting could pay twice.

`adapters.md` keeps the general adapter rules and carries a pointer here; those rules are cited from this
document and never restated in it (principle 9, one home).

## Scope

Every boundary where the swarm moves or reads money. In scope: the two rail classes (bank transfer and
crypto), what each holds as an artifact, the inbound mapping, the outbound operations, what `dedup_key` is
keyed on and why it differs per class, the unknown case in full, and what the adapter refuses. Out of scope:
the general adapter rules (`adapters.md`), the payment workflow's step list (`workflows.md#payment`), the
gate's decision function (`gates_and_workflows.md`), what the adapter is granted
(`authority_model.md#grants`), and the per-instance binding of a rail, a payee, or an obligation, which is
a `payment_profile` or `vendor_binding` context entity resolved at runtime and **never named here**. Also out
of scope: a purchase or a booking with a merchant, which moves money but on the merchant's system and not on
a rail — it is that system's action class, admitted with its adapter, and never `payment` or `transfer`
(`adapters.md#admitting-a-new-adapter`).

**Nothing in this document names a payee, an account, an address, an amount, or an obligation.** Every
example is synthesized. That is not only a repository-hygiene rule; it is the same rule the design applies
to the record, stated under *What the adapter does not write*.

## Why this system gets its own document

Two systems in this design carry a hazard the others do not. The chat channel's is that a message reads as
an instruction (`telegram.md`). This one's is simpler to state and harder to live with:

**A payment is the least reversible action in the system, and it is the only one where the design's
ordinary recovery — take another action that undoes the first — does not reliably exist.**

Every other action class names a recovery that works. A merge is reverted. A release tag is deleted and
retagged. A deploy is rolled back. A publication is superseded, forward-only, and the design says so plainly
rather than implying a reversal that does not exist (`failure_posture.md`). Payments are the class where
that honesty has to go furthest: on one rail the recovery is a **request the receiving party may lawfully
refuse**, and on the other there is **no recovery at all**. Money that has left is gone unless someone
chooses to send it back.

That single fact reorganizes the design at this boundary. Where every other adapter can be correct by
detecting an error and correcting it, this one has to be correct **before** the effect crosses, because
afterwards there is nothing to correct with. So the weight moves forward: onto the gate, onto verification
by a second principal, onto what `dedup_key` is keyed on, and above all onto the rule that a timeout is not
a failure.

## What the rails hold, and what an artifact is here

Both classes hold artifacts of kind `transfer`, identified by the `system`/`external_id` pair every artifact
carries. What supplies the external id differs per class, and the difference is the root of everything else
in this document.

| Rail class | What the artifact is | What supplies its `external_id` | When that id first exists |
|---|---|---|---|
| bank transfer | the rail's record of the instructed transfer | the rail's own transfer identifier, assigned when the rail accepts the instruction | **only when the rail's response returns** — so a lost response leaves the swarm without it |
| crypto | the transaction | the transaction's own hash, determined by its contents | **before it is submitted at all** — it is computed from the signed transaction the swarm holds |

**That asymmetry is the most consequential fact in this document**, and it is stated here rather than
buried in the unknown case, because it is why the two classes need different answers to the same question.
On a bank rail, the identifier the swarm needs in order to ask "did it happen?" is the very thing a lost
response withholds. On a crypto rail, the identifier is in the swarm's hands before it takes any risk at
all. The design's response is the same in shape on both — read the rail back before submitting again — but
what it reads back by is different, and a design that assumed one shape would be unrecoverable on the other.

**A receipt or confirmation document is an artifact of its own kind** where the rail issues one, `PRODUCES`
from the batch. **A balance is not an artifact**, for reasons given in its own section below. And the
`transaction` entity the `reconcile` step writes is **a record in the record, not an artifact**
(`workflows.md#payment`) — it is the swarm's own bookkeeping, and only what an external system holds gets a
`system` and an `external_id`.

## What gates a payment, and why it is not a new mechanism

The question this section answers: a payment is the least reversible action in the system, so is the action
gate plus a checkpoint enough, or does it need something stronger?

**The answer is that the existing mechanisms already compose into something stronger, and building a second
gate would make the boundary weaker rather than safer** (principle 6). Four mechanisms bear on a payment,
each already in the design, and their composition is what a payment gets:

| Mechanism | What it contributes | Where it is stated |
|---|---|---|
| the action gate, on a class the policy places in the never-set | the effect is not taken without a principal's decision; `NEVER` short-circuits ahead of confidence and ahead of any recurrence graduation, so **no series of successful payments ever graduates into taking one unattended** | `gates_and_workflows.md#confidence-and-three-blast-tiers` |
| the checkpoint the gate writes, resolved by a required approver | the decision is attributed, authorized against the required approvers, and terminal; silence never accepts | `gates_and_workflows.md#the-checkpoint`, `authority_model.md#approval` |
| separation of duties: `verify` and `reconcile` belong to a principal disjoint from the payer | one principal never both proposes and confirms a movement of money — the smallest structural check the authority model names, applied where it matters most | `workflows.md#payment`, `authority_model.md#structural-checks-quorum-and-separation-of-duties` |
| the workflow's own ordering: `prepare`, `verify`, `consent`, `pay`, `reconcile` | the payee and amount are checked against the profile **before** the operator is asked, so the operator consents to something already verified rather than to the payer's own assertion | `workflows.md#payment` |

**Read together, a payment is gated more heavily than any other class**, and none of it is new machinery:
an action a policy can never demote, a checkpoint a named approver must resolve, and a verification by a
second principal that must have happened first. The recurrence path that lets a `HIGH`-blast class graduate
into being taken without a checkpoint is unavailable to it, which is the specific property that matters —
a payment that has been made correctly a hundred times is gated exactly as the hundred-and-first.

**What a second gate would cost, stated because the temptation is real.** The instinct at this boundary is
to add something: a second approver, a cooling-off period, a value ceiling above which a different path
applies. Each is expressible in the mechanisms above — a second required approver is the quorum the
authority model already contemplates, a ceiling is the policy resolving a class by its parameters, a
cooling-off is a checkpoint that is not yet resolved. Built as a *separate* mechanism, each would be a
second queue with its own resolution protocol and its own notification path, and the design's experience is
that a second queue is one nobody consumes (principle 1). So: no second gate, and a payment's extra
strictness is expressed in the policy and the workflow rather than in new machinery.

**One thing was missing rather than composed, and it is ruled below (decision 27)**: the approver who
resolves a payment's checkpoint is shown exactly what the `verify` step signed — payee, amount, currency,
period — and the consent is bound to those figures, so that the checkpoint carries the verification as a fact
the approval rests on rather than as a step that happened.

## The dedup key, and what it is keyed on

`work_model.md#at-least-once-implies-effect-dedup` places the `dedup_key` on the action and keys it on the
intended effect. This section says what "the intended effect" means for a payment, because getting it wrong
is how a payment is made twice.

**The key is keyed on the obligation being settled, not on the submission attempt.** Concretely: the
obligation the task references, the payee resolved from the profile, the amount, the currency, and the
period or instance the payment settles. What it is emphatically **not** keyed on:

- **Not on the artifact.** The artifact may not exist yet, and on a bank rail its identifier is exactly what
  a lost response withholds. `adapters.md` states the general reason —
  dedup must answer "did this effect already land" at the moment the external id may be unknown — and this
  boundary is the case that reasoning was written for.
- **Not on a per-attempt token.** A key generated fresh on each attempt is not a dedup key at all; it
  deduplicates one retried request and does nothing about a second full attempt at the same obligation,
  which is the case that pays twice.
- **Not on the rail's own idempotency mechanism alone.** Where the rail offers one, the adapter supplies it
  and derives it from the key above — but the rail's mechanism protects one call and typically expires,
  and on a multi-call rail it may not cover the call that actually moves the money (below). The record's
  key is the durable one; the rail's is an optimization layered on it.
- **Not on the reference field.** It is uncontrolled free text, corridor-dependent in length, and may be
  truncated in transit. It is a reconciliation *hint* and never an identifier.

**The key is written before the first attempt, and this is load-bearing.** An action is created when the
effect becomes known, and it carries its `dedup_key` from creation
(`gates_and_workflows.md#actions-are-entities-only-actions-are-taken`). So the key exists in the record
before anything is submitted — which is what makes recovery possible at all, because a key generated at the
moment of submission is lost by exactly the failure it exists to survive. The order is: create the action
with its key, gate it, and only then submit.

**A payment whose key the adapter has already confirmed is refused.** That is `adapters.md`'s rule, and at
this boundary the refusal is the whole point.

**A key present but unconfirmed is the unknown case**, and it is not a refusal. It is the next section.

## The unknown case: a transfer submitted whose confirmation never returned

This is the hardest dedup case in the design, and it deserves the space. The situation: the adapter
submitted a transfer, and no confirmation came back. The connection reset, the process ended, the rail
timed out. The effect may have landed. Re-submitting may pay twice; not re-submitting may pay nobody.

### The two questions, which must not be conflated

**"Did my request arrive?"** is a question about the transport. It is answered by the response, and when
the response is lost it is **permanently unanswerable from the swarm's side**. No amount of retrying
answers it, because every retry has the same failure mode as the original.

**"Did the effect happen?"** is a question about the rail's state. It is answerable at any later time, by
reading the rail back — provided the swarm can name what to look for.

**The design's whole answer follows from separating them**: the first question is abandoned, and the second
is asked. Concretely, and this is the rule:

> **A timeout is not a failure. It is `unknown`, and the only thing done with an `unknown` is to resolve it
> by reading — never by retrying blind.**

That is principle 7 at the boundary where coercing `unknown` costs the most. Coerced to failure, the design
pays twice. Coerced to success, the design pays nobody and closes the batch saying it did. Both errors are
silent, and the second is worse, because a duplicate payment is at least visible in a statement while a
payment that never happened looks exactly like one that did.

### What the record holds while it is unknown

**The action is created and reads `unknown`.** It carries its `dedup_key` and no confirmation: no
`taken_at`, no `result_ref` (`adapters.md#an-artifact-exists-only-once-its-external-record-does-and-the-interval-before-that-belongs-to-the-action`).

**No artifact is minted.** There is no transfer artifact with a null external id standing in for the one
that may exist, because a placeholder is maintained state whose correctness depends on a later process
arriving to fill it in (principle 11), and it would be indistinguishable to every reader downstream from an
artifact for a transfer that genuinely exists. The artifact is minted from the confirmation, with its
external id already known, or it is not minted.

**The `reconcile` step does not close.** `reconcile` closes on the transfer being read back from the rail at
its terminal status (`workflows.md#payment`); an unconfirmed action does not satisfy that, so the step stays
open and the batch is visibly incomplete. This is the design working: a payment whose outcome nobody knows
is a batch missing a sign-off, which is a readable state, rather than a task marked done.

**The unknown holds, and the hold is bounded.** `failure_posture.md` rule 5's shape applies: the condition
is announced on the off-record path while it may be transient, and the bound raises one checkpoint. What
the checkpoint says is the point — see *When the read cannot resolve it* below.

### How the read resolves it, per rail class

The adapter reconciles by reading the rail for the action's key **before submitting anything again**. What
it reads by differs, and this is where the asymmetry named at the top of this document pays off.

**Crypto: read for the transaction the swarm holds.** The transaction's identifier was determined when it
was signed, before submission, so the swarm has it regardless of what the broadcast returned. The question
is answerable directly: does the chain, or its mempool, hold that transaction? Three outcomes, and none of
them requires guessing.

- **Present and confirmed to the required depth**: the effect landed. The adapter writes the confirmation,
  mints the artifact, and `reconcile` proceeds.
- **Present but not yet confirmed**: still `unknown`, still holding. It is not failed — it is unresolved,
  and the same read answers it later.
- **Absent**: the transaction is not known to the network. The adapter **rebroadcasts the identical stored
  transaction** — the same bytes, never a newly constructed one. Rebroadcasting an identical transaction is
  naturally idempotent: the network either already has it or accepts it once, and either way the money moves
  once. Constructing a *new* transaction for the same obligation is the failure mode, and it is the
  crypto-specific one worth naming precisely, because the chain will not catch it: a newly constructed
  transaction may spend different funds, so both it and the original are valid, both confirm, and the payee
  is paid twice with the ledger perfectly consistent. **The chain protects against spending the same funds
  twice; it does not protect against the swarm intending one payment and issuing two.** Which is why the
  rule is *sign once, store the signed transaction, and rebroadcast those bytes* — the idempotency is the
  design's, built on the record, and the rail supplies none.

**Bank transfer: read for the key, and where that fails, read the window.** The rail's own idempotency
mechanism, where it offers one, is supplied from the record's `dedup_key`, and a read for that key is the
first attempt. Where the rail offers no such read, the fallback is to enumerate the rail's recent activity
over a window that covers the submission and match on what the record holds — which is why *reading a
window of recent transfers* is a **required capability of a bank-rail adapter and not an optional one**. A
transfer found in the window whose key matches is adopted: the adapter writes the confirmation against the
existing action and mints the artifact from what it read. **It is adopted and never re-issued.**

**A bank rail's submission is more than one call, and the risk sits in the last one.** These rails typically
separate instructing a transfer from funding it, and the rail's idempotency protection commonly attaches to
the instruction rather than to the funding. So the interval where a lost response is most dangerous is
precisely the one the rail protects least. The design's response is that the record's key spans the whole
effect rather than any one call: the action is one intended effect — this obligation, settled — and the
adapter's read-back asks whether *that* happened, not whether a particular call was received. An adapter
that keyed on the rail's per-call mechanism would inherit the rail's seam as its own.

### Terminal is not permanent, and the design must not assume it is

One property of both classes defeats a state machine with absorbing terminal states, and it is stated here
because it changes what `reconcile` may assume.

**On a bank rail, a transfer that reached a sent state can leave it.** The receiving institution may return
it days or weeks later, and the rail reports that as a further state change. So "sent" is the rail's report
that it released funds — not that they were credited — and a design that closed the file at "sent" would
miss every return.

**On a crypto rail, a confirmation can be undone.** Confirmation is depth, and depth is a risk parameter
rather than an event: a reorganization can return a confirmed transaction to unconfirmed. There is no
finality event to wait for, only a depth at which reversal becomes implausible — and what depth suffices is
a policy decision about the amount, not a fact about the chain.

**Two consequences for this design.** The depth (or the rail state) that counts as terminal is **declared**,
not assumed: the criterion is stated in this document per rail class, and the value is bound per rail
instance in the `vendor_binding` (decision 29, below) — an adapter that hardcodes a depth has made a risk
decision that was never its own to make. And a later inbound signal reporting that a terminal state was undone is
an **observation and a defect to surface**, never a silent correction of the record: the confirmation
stands, because it was true when it was read, and the reversal is recorded beside it so both are readable.
What follows from a reversal is a new decision, taken as its own action through the gate, and never the
adapter re-submitting.

### When the read cannot resolve it

Some cases are unresolvable by reading. A rail may report its own state as indeterminate — several do, which
is worth noting: the rails themselves acknowledge this state exists, and an adapter with no representation
for it cannot model the rail faithfully. A crypto transaction may be absent from the network long after
broadcast and past the point where it could still be mined. A bank transfer may be blocked pending a
document nobody has supplied.

**The design's answer is bounded automation, then escalate — and never automated action.** The read is
retried with backoff, or deferred to the reset time the rail states where it states one
(`failure_posture.md`, rule 8). When the bound is reached, **one checkpoint**, on the task, through the one
decision queue. What the checkpoint carries is what makes it useful:

- that a transfer for this obligation was **submitted and is unconfirmed**, stated as unknown rather than as
  failed;
- what the adapter read and what it found, so the operator is not asked to do the diagnosis
  (`failure_posture.md`'s report-then-decide shape);
- and the options, which are the operator's to choose among: submit again, having established by their own
  read that nothing landed; abandon the attempt; or wait.

**The adapter does not choose among those, and this is the strongest refusal in this document.** It does not
retry the payment on exhaustion, because the exhausting condition is precisely the one where a retry may
pay twice. It does not mark the action failed, because that is an assertion nobody verified. And it does not
advise a manual payment as a fallback — the plausible-looking recovery that is the most dangerous of all,
since the transfer it is proposing to work around may still be moving, and a manual payment made against
a transfer still moving is a duplicate the operator was told to make.

### The complete rule, stated once

> A payment action carries a `dedup_key` derived from the obligation, written before the first attempt. The
> adapter refuses an action whose key it has already confirmed. Where the key is present and unconfirmed,
> the adapter **reads the rail for that key before submitting anything** — by the transaction the swarm
> holds on a crypto rail, by the key or the recent window on a bank rail — and adopts what it finds rather
> than re-issuing. Where the read finds nothing, a crypto rail is resolved by rebroadcasting the identical
> stored transaction and a bank rail by submitting under the same key. Where the read cannot resolve it, the
> action stays `unknown`, `unknown` holds `reconcile` open, and the bound raises one checkpoint that reports
> what is known and lets a principal decide. **At no point does the adapter submit a payment twice for one
> obligation, and at no point does it assert an outcome it did not read.**

## Reading a balance: an observation, and not an artifact

Reading a balance is an inbound observation. The question worth settling is what it is an observation *on*,
and the answer is not the obvious one.

**A balance is not an artifact.** An artifact is a record living in an external system, identified by
`system` and `external_id` and referred to over time. A balance has no identity of its own: it is a
**derived aggregate over an account's history**, computed at the moment of the read, and the same account
yields a different value a second later. Minting an artifact for it would create a record whose external id
names no external record, and whose content is stale the instant it is written — maintained state of exactly
the kind principle 11 forbids.

**So what is it an observation on?** On the artifact for the **account** at the rail, where one is tracked
— the account being a durable record with an identity, which the balance is a reading of. Where no account
artifact is tracked, the balance read is a hydration input a step declared, and its disposition is a drop
with that reason rather than a write with nowhere to go.

**And it carries sourcing and coverage like every other observation**, which at this boundary means
something specific and unusually important:

- **The time is the rail's own time for the reading**, not the time the write landed.
- **The coverage states what the reading covers**, and both classes make this non-trivial. A bank balance
  may or may not reflect transfers instructed and not yet settled. A crypto balance is relative to a chain
  height that can reorganize, and the confirmed and unconfirmed portions are different facts. So a balance
  observation states the height or the point it was read at, and states confirmed and pending separately
  where the rail distinguishes them. **A balance without that is not a meaningful assertion**, and recording
  one as if it were is how a sufficiency check passes against funds that are already committed.

**A balance is never a permit.** It is a condition a step owner reads before signing — the same relationship
a CI result has to a review step (`adapters.md#no-external-event-advances-a-step-by-itself`). A sufficient
balance does not authorize a payment and an insufficient one does not by itself block a step; a step owner
reads it and, ordinarily, blocks with a verdict that names what it read. This is worth stating because the
inverse is tempting: a balance check *feels* like a safety mechanism, and building it as one would put a
gate in the adapter, which is the second gate principle 6 forbids and the wrong place besides.

## Fees, rates, and what the operator consented to

Fees deserve their own treatment because they are where the amount a principal approved and the amount that
moves come apart, and neither rail class makes them a simple number.

**On a bank rail the fee and rate are properties of a quote, and a quote expires.** The design's consequence
is not about fees at all — it is about consent. The `consent` step carries a checkpoint naming the payee,
the amount, and the reference (`workflows.md#payment`), and the operator resolves it. If the quote the
amount was derived from has expired by the time the action is taken, the adapter is holding a permit for one
figure and about to submit another.

**The rule: the adapter never widens what was approved, and the boundary is a policy value whose default is
zero.** A re-quote that leaves the effect within what the checkpoint named is taken; one that does not is
**not** taken, and the difference is a new decision. Where the boundary lies is the `action_policy`'s
consent tolerance for the class, and where the policy declares none it is zero — an exact match — because a
tolerance this document invented would be an amount nobody approved (decision 28, below).

**On a crypto rail there is no quote and no lock.** The fee is chosen at construction against a market that
moves, and its consequence is not a different amount to the payee but a different *outcome*: too low, and
the transaction waits, then is dropped from the network's memory pool entirely — which is the "absent" case
in the unknown resolution above, arrived at slowly. The remedies the rails offer (replacing the transaction
with a higher-fee version, or spending its output to incentivize inclusion) each **produce a different
transaction than the one the swarm signed**, which means a different identifier and, on the replace path, a
different effect than the one the record holds a confirmation-in-waiting for. So each is a **new action**,
of its own class, through the gate — never something the adapter does to rescue an unconfirmed payment on its own
initiative.

**A fee is disclosed, not absorbed.** Whatever the class, the checkpoint the operator resolves states what
leaves the account and what reaches the payee, where the rail distinguishes them, and states that they may
differ where intermediaries deduct along the way and the rail cannot say in advance. Presenting only one
figure would be presenting the operator a number the design knows may not be the one that matters.

## The reference field, and a policy that suppresses it

Both rail classes offer a way to attach text to a payment, and the two are not equivalent in who can read
it. This section states the general rule, and then the refusal it produces.

**What the field is, per class.**

| Rail class | The field | Who can read it | Permanent? |
|---|---|---|---|
| bank transfer | a short free-text remittance or reference field, carried to the payee and shown on their statement | the payee, both institutions, and any intermediary in the chain | for as long as the parties keep their records |
| crypto | a data-carrying output or a native note field on the transaction | **everyone, forever** — it is in a public ledger, permanently linked to the amount, the addresses, and the time | **irrevocably** |

**The general principle: a payment's metadata is visible to third parties, and the design must let a policy
suppress it.** The reference field exists to help a payee reconcile a credit, which is a real use. But it
travels to parties who are not the payer and not the payee, it is retained by them, and on a crypto rail it
is published to the world with no redaction, no expiry, and no deletion. So what a payment *says about
itself* is disclosure, and disclosure is a policy decision rather than an adapter convenience.

**The refusal, stated as a refusal and not as a capability.** Where the profile governing a payment declares
that no metadata is to be attached, **the adapter attaches none** — the field is omitted entirely, not
filled with a neutral value. A neutral placeholder is not suppression: it is a different disclosure, and on
a public ledger an unexplained constant attached to a recurring payment to one recipient discloses the
pattern it was meant to hide. Omission is the only suppression that suppresses.

Three properties of this rule are worth stating, because each is a way it would be eroded:

1. **It is a refusal, not a default.** The adapter does not decide to omit metadata when it judges the
   payment sensitive; it omits when the policy says to, and it attaches when the policy says to. An adapter
   inferring sensitivity would be making a disclosure judgement, which is the same class of inference this
   design refuses everywhere.
2. **A suppression that cannot be enforced is not one.** Where the operation is performed through a layer
   the adapter does not fully control, "the field was not requested" is weaker than "the field could not be
   set". The design's requirement is the second: the suppression is a property of what the adapter submits,
   verifiable from what it submitted, and not an instruction it passed along and hoped was followed.
3. **The suppression is verified on read-back like anything else.** The transfer is read back at its
   terminal state (below), and what it carries is part of what is read. A payment that went out carrying
   metadata a policy suppressed is a **defect to surface**, and on a public ledger it is one that cannot be
   corrected — which is the strongest possible argument for the second property.

**The mirror rule: what the adapter writes about a payment into the record is narrowed too.** This is the
same shape `github.md` applies to advisory detail. Material the record never held cannot be copied out of it
by a later step, a summary, a digest, a notification, or a rendered page. So the record holds what the
obligation and the reconciliation need — the profile the payment was drawn from, the action, the confirmed
transfer's identifier, the amount — and the payee's own identifying details stay in the context entity that
holds them and are referenced rather than copied. A checkpoint presented to the operator carries what the
operator needs to decide, which is not the same as everything the adapter read.

## Inbound: every signal a rail can produce

The rails deliver fewer distinct event kinds than a code host or a chat channel, and most of what matters
arrives by a read rather than by a notification the rail sends. The disposition rule holds regardless: every delivery resolves to one
of the four outcomes or to `dropped` with a reason, counted per window and surfaced on the off-record
announcement path.

### Signals about a transfer the swarm submitted

| Signal | Status | Outcome in the record |
|---|---|---|
| a transfer reaches its declared terminal state, read back or notified | handled | an **action confirmation** on the `payment`-class action whose `dedup_key` matches (`taken_at`, `result_ref` naming the transfer); the transfer is an artifact `PRODUCES` from the batch. The `reconcile` step owner reads it and signs, and the `transaction` entity is that step's write, **never the adapter's** |
| a transfer moves through a non-terminal state (accepted, converting, processing, awaiting funds, broadcast but unconfirmed) | handled | an observation on the artifact. **It is not a confirmation**: the effect is not yet what the action intended, and folding a non-terminal state into "sent" is how a pending payment is reported as complete |
| a transfer fails, is rejected, or is returned by the rail | handled | an action confirmation carrying the failing result; `reconcile`'s `on_fail` opens `pay` again **through the gate**, never a second submission by the adapter |
| a transfer that had reached a terminal state is later reversed, returned, or reorganized out | handled | an observation, **and a defect to surface**. The confirmation stands, because it was read back when it was written; the reversal is recorded beside it, and what follows is a new decision through the gate |
| a transfer the record holds no action for | handled | an observation on the artifact, **and a defect to surface**. Money left an account the swarm has a credential for, and no action of the record's intended it. It is never a confirmation, because there is nothing to confirm |
| a rail-side issue blocking a transfer (a compliance hold, a document required, a verification pending) | handled | an observation on the artifact; the transfer is non-terminal and the step stays open. Supplying what is asked for is **operator-only**: it is out-of-band identity or documentation work no agent performs (`workflows.md#operator-only`) |
| a transfer submitted whose confirmation never returned | handled | **no write beyond the action's own unknown state.** See *The unknown case* |

### Signals about money the swarm did not send

| Signal | Status | Outcome in the record |
|---|---|---|
| an incoming payment received | handled | an observation on the artifact for the obligation it settles, where one is tracked; otherwise a task for intake with the transfer as its artifact. **The adapter never matches an incoming payment to an obligation by its reference text** — the reference is a hint, and the matching is a step's judgement, signed |
| an outgoing payment the swarm did not make | handled | an observation, **and a defect to surface**, as above |
| a balance read | handled | an observation on the account's artifact, with sourcing, height or point, and confirmed-versus-pending stated. See *Reading a balance* |
| a rate or fee schedule read | handled | an observation where a tracked artifact depends on it; otherwise a hydration input a step declared, and `dropped` with that reason where nothing declared it |
| a statement or activity read over a window | handled | observations on the artifacts it covers, **carrying the window as coverage** — this is the read the unknown case's recovery depends on, so a truncated or rate-limited page recorded as complete would defeat the recovery it exists to serve |

### Delivery, and what the dedup rule keys on inbound

| Concern | Bank transfer | Crypto |
|---|---|---|
| does the rail notify on state changes? | commonly yes, carrying the resource and its previous and current state, with a per-delivery identifier and a signature over the body | not natively; state is read from the chain or from an indexer that watches it |
| what the inbound dedup rule keys on | the rail's per-delivery identifier, as `adapters.md`'s rule requires | the transaction identifier together with the height at which it was observed, since the same transaction is legitimately observed many times at increasing depth |
| authenticity | the signature the rail provides, verified against the rail's published key. **The scheme varies between rails**, so an adapter that hardcodes one verification method has a rail it cannot be pointed at | the chain itself, read from a source the swarm chooses to trust; an indexer is a party whose answer is trusted, and that trust is a `vendor_binding` decision |

**A notification is an optimization; the read is the truth.** Both classes deliver notifications
at-least-once, unordered, and without guarantee. So the design's correctness never depends on one arriving:
the confirmation that closes `reconcile` is a **read of the rail at the transfer's terminal state**, and a
notification at most tells the
adapter when it is worth reading. Stated as a rule, because it is the one an implementation erodes first:
**a notification is never a confirmation on its own.** It is an observation that a read may follow.

## Outbound: the operations a step takes on a rail

Every row is an `action`, created when the effect becomes known and evaluated at the action gate at the
moment it would be taken. The adapter performs the operation on permit, performs nothing on a checkpoint,
and **confirms by reading the rail back at the transfer's declared terminal state** — never by the
submission's return.

| Step | Operation on the rail | Action class | What the action gate does with the class | What confirms it landed |
|---|---|---|---|---|
| `pay` | submit the transfer, under the action's `dedup_key` | `payment`, or `transfer` where the policy distinguishes them | **the never-set**: it resolves to `NEVER` ahead of confidence and ahead of recurrence, so it is never taken without a principal's decision, and no series of successes graduates it | the transfer read back from the rail **at its declared terminal state**, matched to the action's key, with the metadata policy's suppression verified in what was submitted |
| `pay`, on a multi-call rail | the calls that precede the movement of money (pricing, addressing, instructing) | `external_api_write` | as the policy lists it; these leave no money movement, so they are not the payment | each read back; **none of them is the payment's confirmation**, and an instruction accepted is not funds sent |
| `prepare` | read the rail for what the effect needs (a rate, a fee, an account's state) | a read, not an action | not gated; a read takes no effect | not applicable; the result is an observation with sourcing and coverage |
| `prepare` | record a payee at the rail where the rail holds payees as durable records | `external_api_write` | as the policy lists it | the payee record read back by its identifier. **Recording a payee is not paying one**, and the adapter reuses an existing record rather than creating a second for the same payee |
| `reconcile` | read the transfer at its terminal state; read a statement or activity window | a read, not an action | not gated | not applicable; the observations carry the window as coverage |
| recovery of a payment | see below | — | — | — |

**What the adapter never does outbound, at this boundary specifically.** It never submits a transfer whose
`dedup_key` it has already confirmed. It never submits again on an unconfirmed key without reading the rail
first. It never constructs a second transaction for an obligation whose signed transaction it already holds.
It never attaches metadata a policy suppressed. It never widens an amount, a payee, or a rail beyond what
the checkpoint named. It never takes a payment because a balance was sufficient, a schedule came due, or a
previous payment in a series succeeded. It never supplies identity or compliance documentation to a rail —
that is operator-only. And it never writes the `reconcile` sign-off, whatever it read: the adapter that took
the `pay` action is on the payer's side of a separation of duties the workflow declares, and a confirmation
it wrote is an observation for the verifier to read, not the verifier's judgement.

**The separation of duties reaches the adapter, and this is worth stating plainly.** `workflows.md#payment`
places `verify` and `reconcile` with a principal disjoint from the payer, so that one principal never both
proposes and confirms a movement of money. An adapter is the principal's hands at the boundary
(`adapters.md#outbound-steps-produce-actions-adapters-take-them`), so an adapter that submitted the transfer
and then wrote the sign-off saying it settled would have collapsed the two roles into one process — the
structural check defeated not by anyone deciding to defeat it, but by the boundary having only one component
in it. What the adapter writes is the **confirmation**, which is an observation on the action; what closes
the step is the verifier's **sign-off**, which cites it. A confirmation is not a sign-off
(`vocabulary.md`), and this is the boundary where the distinction earns its keep.

## Recovery: what undoes a payment

`failure_posture.md` requires every action class to name its recovery, even where the recovery is only a
forward fix. The payment class's answer is the least satisfying in the design, and the design's obligation
is to say so plainly rather than to imply a reversal that does not exist.

| Situation | Recovery | What it actually is |
|---|---|---|
| a transfer instructed and **not yet funded** at a bank rail | cancel it | **a genuine reversal.** Nothing moved. This is the only case in this document where an undo simply works, and it is available only inside a narrow window |
| a transfer already released at a bank rail | request a recall through the rail | **a request the receiving side may lawfully refuse.** It is asynchronous, human-mediated, bounded by the scheme's own windows, and frequently declined — the beneficiary's institution generally needs the beneficiary's consent. It is never exposed as an operation that returns success |
| a transfer confirmed on a crypto rail | **none exists** | there is no recall, no return, no chargeback, and no authority to appeal to. The only path is the recipient voluntarily sending funds back, which is a **new payment they choose to make** — a social and legal remedy, not a rail feature, and not something the design may plan on |
| a payment made to the wrong payee, or twice | an obligation to recover, pursued out of band, and a **new** payment where the recipient returns funds | recorded as what it is. The record shows the payment, the error, and whatever follows, in order — never a corrected end state that reads as though the error had not happened |
| a transfer left unconfirmed on a crypto rail | replace it at a higher fee, or spend its output to incentivize inclusion | **a new action of its own class through the gate**, because each produces a different transaction than the one the record holds. Never an adapter's own initiative |

**So the payment class's recovery is forward-only in the cases that matter**, which is the same shape the
`publish` class takes and for a stronger reason. `failure_posture.md`'s table is extended by this document
rather than contradicted: where it names a recovery per class, this is the row for `payment`, and the row
says that beyond a narrow pre-funding window there is no reversal — only a request, or nothing.

**Two things follow, and they are the reason this document's weight sits before the boundary rather than
after it.** First, **the gate is the control, and there is no second chance behind it**: for every other
class the gate is one of two protections, and here it is the only one. Second, **a recovery being
unavailable is not a reason to lower the bar for taking the action** — it is the reason the class sits in
the never-set, the reason a second principal verifies before the operator consents, and the reason a timeout
is read as unknown rather than retried.

## How the five rules apply here

`adapters.md` states the five; this section says what each means at this boundary.

**Identity.** Two identities matter and they must not be confused. The **principal** whose approval resolves
the payment's checkpoint is resolved through the credential binding like any other
(`authority_model.md#principals`) — and this boundary is where the fallthrough that resolves an unknown
credential to the operator would be most expensive. The **payee** is not a principal at all: it is a party
named by a context entity, and the adapter resolves it by reference and never by matching a name, an
address, or a reference string it read somewhere. A payee resolved by anything other than the profile is a
payee nobody verified, and `verify` exists precisely to check that the payee and amount match the profile
before the operator is asked.

**Linkage.** A signal names a transfer at a rail; the adapter finds the artifact by `system` and
`external_id`. A transfer the record holds no action for is an artifact with no batch, and — unlike
elsewhere in the design, where such a record yields a task for intake — an *outgoing* one is a defect to
surface, because money left an account the swarm holds a credential for and nothing in the record intended
it. An incoming one does yield a task where no obligation is tracked.

**Dedup.** Stated in full above, in its own section, because at this boundary it is the design's central
question rather than a rule to apply.

**Unknown, and every delivery's disposition.** Also stated in full above. The one addition here: a rail's
own indeterminate state is carried into the record as `unknown` and never flattened into pending or failed —
the rail is telling the swarm something true, and coercing it discards the one honest signal available.

**Provenance and read-back.** Every write names the adapter, the rail, and the delivery identifier; every
write carrying a decision is read back before the adapter acknowledges. The read-back at this boundary is
not a formality — it is the *definition* of the confirmation, because the submission's return says only that
a request was received (`failure_posture.md`, rule 6: a refusal on an existing key is stronger evidence of a
prior commit than a success response is of the present one).

## What the adapter refuses, and why

Collected, each with what would break if it did not hold.

**It refuses to submit twice for one obligation.** Any second submission is preceded by a read of the rail
for the action's key. Breaks otherwise: a duplicate payment, which is the failure this whole document is
organized around.

**It refuses to construct a second transaction where it holds a signed one.** It rebroadcasts the same
bytes. Breaks otherwise: two valid transactions, both confirmed, with the ledger perfectly consistent and
the payee paid twice.

**It refuses to treat a timeout as a failure, or as a success.** Breaks otherwise: paying twice, or closing
a batch that says money moved when nobody knows.

**It refuses to confirm from a submission's return, or from a notification.** The confirmation is a read at the
declared terminal state. Breaks otherwise: a pending transfer reported as complete, and a returned one never
noticed.

**It refuses to advise a manual payment as a fallback for an unresolved submission.** Breaks otherwise: a
duplicate the operator was instructed to make, against a transfer that may still be moving.

**It refuses to attach metadata a policy suppressed, or a neutral placeholder in its stead.** Breaks
otherwise: a permanent public disclosure that cannot be corrected.

**It refuses to write the `reconcile` sign-off.** It writes confirmations; the verifier signs. Breaks
otherwise: the separation of duties collapsed into a single component, silently.

**It refuses to take a payment on any signal that is not a resolved checkpoint.** Not a due date, not a
sufficient balance, not a schedule, not a chat message, not a successful predecessor in a series. Breaks
otherwise: a payment nobody decided.

**It refuses to supply identity or compliance material to a rail.** That is operator-only. Breaks
otherwise: an agent acting as the operator's identity to a financial institution.

**It refuses to hold rail state of its own.** No local ledger of what was paid, no cursor table standing in
for coverage, no cache of a balance. Breaks otherwise: a second source of truth about money, diverging
silently from both the record and the rail.

## What this document does not decide

The general adapter rules are `adapters.md`'s and are cited here, not restated. The payment workflow's step
list, its two disjoint roles, and its stages are `workflows.md#payment`'s. The gate's decision function and
the checkpoint's protocol are `gates_and_workflows.md`'s. Whether the raiser of a checkpoint may resolve it,
and what quorum a payment might require, are `authority_model.md`'s open questions and bear directly here.
Open decision 15 (adapter packaging) is `adapters.md`'s and untouched; decision 16 (where inbound delivery
lands) is ruled there, and for this system it means the process that receives a rail's notification may be
shared plumbing while verifying the rail's signature against its published key, and extracting the
per-delivery identifier, are this adapter's. Which rows have a built path is `status.md`'s. Three decisions
this document opened are ruled in the three sections that follow.

## A payment's approver is shown exactly what the verifier signed

**Ruled (decision 27, 2026-09-05): yes.** Registered in `conformance.md#the-register-of-open-design-decisions`.
The checkpoint the `consent` step carries to the operator carries the obligation **as the `verify` sign-off
recorded it** — the payee as the profile names it, the amount, the currency, the period or instance being
settled, and the rail — together with the verifier's identity and the fact that these are the figures it
matched against the profile. Not a summary, not the payer's restatement, not a rounded figure: the values the
verifier signed, carried verbatim from that sign-off into the checkpoint's `needed_input`, with the checkpoint
referring to the sign-off it carries them from. And the consent is **bound** to them: the `pay` action is
taken only on parameters equal to what the checkpoint carried, and a difference between the two — a re-quote,
a corrected payee, a changed period — is a new decision, which is decision 28's rule with its tolerance at
zero by default.

**Reason.** Consent is to a specific obligation. An approval given on a presentation that differs from what
will be taken is approval of a different thing, and the action then goes out on authority nobody gave —
fabricated authority, on the least reversible action in the system, at the one boundary where the design has
said the gate is the only control (`#recovery-what-undoes-a-payment`). Principle 2 is the shape: an approver
reading a summary is reading a write's report of itself, and a report is not evidence; what the approver must
read is the thing that was checked, which is the sign-off. `authority_model.md#approval` says an approval is
authorized against the required approvers and attributed; this adds that it is authorized *on* a stated
subject, and states the subject. The open question asked whether the existing pinning rule already covered
this, since a sign-off is pinned to the artifact state it judged. It does not, and the reason is worth being
precise about: at `consent` there is no artifact — the transfer does not exist until `pay` is confirmed
(`#what-the-rails-hold-and-what-an-artifact-is-here`) — so the pinning rule has nothing to pin the verifier's
judgement to. What `verify` judged is the **action's parameters**, and this ruling pins those: the checkpoint
carries them as signed, and `pay` is refused on anything else. It is not a second gate (principle 6): it is
the content of the one checkpoint the gate already writes, and the one comparison the adapter already makes
before taking an action on its `dedup_key` — the key is derived from the obligation, so parameters that
differ from what was consented to are a different key, and the adapter's refusal to take an action under a key
the checkpoint did not cover is the same refusal it makes for every action.

**The cost accepted** is longer checkpoint messages on the chat channel: every payment checkpoint states the
payee, the amount, the currency, the period, the rail, and who verified them, where a shorter message would
say "pay the invoice". Accepted without reservation; the channel's message limits are far above this, and the
length is the operator reading what they are approving. **What would reopen it:** nothing about payments. It
is the general rule for consent stated at the boundary where getting it wrong is paid in money, and if it
were found wanting here it would be found wanting everywhere.

## Tolerance is an `action_policy` value, and its default is zero

**Ruled (decision 28, 2026-09-05): the boundary is a policy value, per action class, and absent a value it is
zero.** Registered in `conformance.md#the-register-of-open-design-decisions`. Any rail-side change to what the
payee receives or to what the operator pays, relative to the figures the checkpoint carried (decision 27),
requires a new checkpoint. A re-quote that moves either figure by any amount is outside a zero tolerance and
is not taken; the adapter records the re-quote as an observation, `consent`'s `on_fail` opens `verify` again
on the new figures, and the operator decides again on what the verifier signed the second time. The operator
may later set a non-zero tolerance for a class of action in the project's `action_policy`, and from then on a
change within it is taken and one outside it is a new checkpoint. What the number is, per class, is policy
data; that the shape is a per-class tolerance whose absence reads as zero is the design
(`data_model.md#concepts`, the `action_policy` row).

**Reason.** Fail-closed is the default posture on the field that carries the safety meaning (principle 5),
and the field here is the difference between what was consented to and what will be taken. A tolerance the
design invented — a percentage, a rounding — would be an amount nobody approved, written by whoever wrote the
document rather than by the principal whose money moves. A tolerance the operator writes is the operator's,
per class, with a record of having written it, and reversible by deleting it. The design owns the shape and
the operator owns the number, which is the same division `work_model.md` draws for the governance classes
(decision 18) and `gates_and_workflows.md` draws for the bulk-mutation count. The open question feared that
exact match makes routine payments unresolvable on a rail whose prices expire. It does not make them
unresolvable; it makes each expiry a decision, and a project for which that is too many decisions writes a
tolerance, once, and has recorded that it did.

**Where the value lives, and why not the other two candidates.** On the `action_policy`, keyed by action
class — not on the checkpoint, where it would be a number the payer proposes to the approver each time and
the approver has to check; and not on the `payment_profile`, where it would be per payee and therefore a
disclosure judgement mixed with a risk one. A profile may still constrain more tightly than the policy — a
profile that says exact for one obligation is honoured — and a profile may not widen what the policy allows,
because the direction of composition is the fail-closed one.

**The cost accepted** is that, under the default, a rail whose quote expired between `consent` and `pay`
returns the batch to `verify` and asks the operator again. **What would reopen it:** nothing but a rail whose
prices expire, and for that rail the remedy is a value, not a change of shape.

## Terminal is declared in the rail's adapter document, and the value is bound per instance

**Ruled (decision 29, 2026-09-05): the criterion is stated here, per rail class; the value is bound per rail
instance in the `vendor_binding`.** Registered in `conformance.md#the-register-of-open-design-decisions`.
Terminal means **the state after which the rail itself treats the transfer as irreversible** — the point
past which the rail's own model offers no cancel and no unwind, and changes the transfer's state on its own
initiative only by a return that arrives as a new event. On a bank rail that is *settled* — the funds
credited at the receiving institution, as the rail reports it — and never *sent*, *released*, or
*processing*, each of which is the rail's report that it did something and none of which is the effect the
action intended. On a chain it is a confirmation depth *N*, with *N* per rail, because a reorganization's
reach differs per chain and the depth at which reversal becomes implausible is a property of that chain and
not of the swarm. Which state name a given bank rail uses for *settled*, and what *N* is for a given chain,
is bound per instance in the `vendor_binding` entity that binds that rail to this operator, resolved at
runtime and never named in this document — where every per-instance property of an external system already
lives (`#scope`; `adapters.md#scope`). The `reconcile` step's read is a read of the rail at that state, and
`pay`'s confirmation is minted from it.

**Reason.** *Sent* is a response code, and a response code is not evidence (principle 2): a rail reporting
that it released funds is reporting its own operation, not the effect, and the design's confirmation is the
effect read back. Stating the criterion in the adapter document rather than in a policy puts it where the
rail class's own behaviour is described — this document already states, for each class, when the external id
first exists and why terminal is not permanent, and the terminal criterion is the third fact of the same kind
about the same systems. Binding the value per instance puts the number where every other per-instance fact
about a rail is, and keeps this document free of any rail's name. The open question offered the
`payment_profile` and the action policy as the other candidates. The profile would make terminality per
obligation, which is the wrong grain for a property of the rail — two obligations on one chain do not
experience different reorganization risk — and the policy would make it a class property, which is the wrong
grain the other way, because one class of action reaches several rails. **The amount-sensitivity the open
question rightly raised is preserved without splitting the criterion:** a `payment_profile` may declare a
deeper depth for its obligation than the binding's, and the deeper governs; it may not declare a shallower
one. So the binding is the floor the design reads as terminal, and an obligation whose amount warrants more
waits longer, by its own declaration.

**What follows for the rules above.** The unknown case resolves against this criterion: *present but not yet
confirmed* means below the bound depth, and *reached its declared terminal state* means at or past it.
Terminal is still not permanent (`#terminal-is-not-permanent-and-the-design-must-not-assume-it-is`) — a
return after settlement and a reorganization past *N* are observations and defects to surface, and the
criterion is the point at which the confirmation is written, not a promise that nothing follows. An adapter
that hardcodes a depth, or reads a released state as settled, has made a risk decision that was never its own
to make, and the drift table below records that the built path does both.

**The cost accepted** is one more value in every rail's `vendor_binding`, and a `reconcile` step that waits
— hours on a bank rail, minutes to hours on a chain — where a read of the submission's return would have
closed it at once. **What would reopen it:** a rail class for which irreversible has no state the rail itself
names — a settlement layer with only probabilistic finality and no depth convention — which would need its
own criterion stated here, per the freshness note's list of what a new rail class must answer.

## Freshness

**Written against the published API surfaces of bank-transfer and crypto rails as documented on
2026-09-05**, read for capability rather than for any one vendor: the four-phase shape of a bank transfer,
its lifecycle states and their non-absorbing terminality, its idempotency and reference fields, its recall
windows; and the crypto rails' construct-sign-broadcast separation, probabilistic finality, memory-pool
eviction, fee replacement, and public data fields. **This document names no vendor**, and the per-instance
binding of a rail is a `vendor_binding` entity resolved at runtime.

**What would make it stale:**

- **A bank rail offering idempotency that spans the whole effect rather than one call.** This document's
  treatment of the multi-call seam exists because the protection commonly attaches to the instruction rather
  than to the funding; a rail that closed that seam would simplify the bank half of the unknown case, though
  not remove the record's own key, which is durable where a rail's expires.
- **A change in the recall or return windows, or a scheme that makes a reversal binding rather than
  requestable.** That would change the recovery table's second row, which is currently the design's most
  uncomfortable statement.
- **A crypto rail offering an application-level idempotency key.** The design builds idempotency itself
  because none is offered; one that was offered would still not replace the record's key, but it would
  change what the rebroadcast rule is protecting against.
- **A change in memory-pool retention or fee-replacement policy**, which changes how an unconfirmed transaction
  resolves and therefore how long "absent" takes to become meaningful.
- **A change in what a public data field costs or how much it carries.** The suppression rule is about
  visibility rather than size, so a size change alters nothing; a change in *visibility* — which is not
  something these rails offer — would.
- **A new rail class**: a card rail, a payment-service intermediary, a settlement layer with different
  finality. Each would need its own rows, and the questions this document asks of a class are the ones to
  ask: when does the external id first exist, what is terminal, what is reversible, and what does the
  metadata field disclose.

**What is load-bearing and what is not, for the condensation pass.** Load-bearing: the asymmetry in when the
external id exists, the complete unknown-case rule, what `dedup_key` is keyed on and what it is not, the
composition of existing mechanisms rather than a second gate, terminal-is-not-permanent, the metadata
suppression as a refusal, the separation of duties reaching the adapter, and the recovery table's honesty
about what does not exist. Not load-bearing and safe to compress: the per-class delivery comparison, the fee
discussion below the level of its consent consequence, and the enumeration of non-terminal state names,
which are the rails' vocabulary rather than the design's.

## What the rails offer that this design does not use

Named so the condensation pass knows what is deliberately absent rather than overlooked.

| Capability | Why it is unused |
|---|---|
| a rail's scheduled or recurring payment feature | a schedule at the rail would take payments without an action, a gate, or a checkpoint — the auto-merge failure (`github.md`) with money. **The design's recurrence is a task that comes due and goes through the whole workflow each time** |
| a rail's own approval or dual-authorization workflow | a second approval surface beside the checkpoint (principle 6). Where a rail requires one for its own reasons, satisfying it is operator-only and never an agent's action |
| batch or bulk payment submission | one action is one intended effect; a batch would give many effects one gate decision and one `dedup_key`, which is precisely what the dedup rule must not have |
| a rail's cancel operation, outside the pre-funding window | it does not do what its name suggests past that window. Named in the recovery table as a request rather than offered as an operation |
| debiting a counterparty (pulling funds) | nothing in the work model does this; it is a different consent relationship entirely |
| holding a balance for FX timing, or converting between currencies to await a rate | a position taken on a market is an act nothing in this design authorizes, and an adapter is the last component that should hold one |
| a rail's spending limits and card controls | policy expressed at the rail rather than in the record. The design's limit is the gate, and a second limit at the rail would be a control the record cannot read or reason about |
| crypto: multi-output transactions paying several payees at once | the batch objection above, on-chain |
| crypto: replace-by-fee and child-pays-for-parent as automatic behaviours | available, and each is a **new action through the gate** rather than an adapter behaviour |
| crypto: smart-contract or programmable payment conditions | the design's conditions are the gate's and the workflow's; a condition enforced on-chain is a second gate the record cannot resolve, and one that is irreversible |
| a rail's receipt-document generation | available and unused as a confirmation; the confirmation is the read at terminal state. A receipt is an artifact where the rail issues one |

## Drift: what the built path does that this design does not say

Recorded as drift, not as design justification. An existing implementation is never a reason the design
must accommodate it; these rows say what would have to change, not what should be written differently. This
is the adapter where the gap between the design and the built path is widest, and the rows are ordered by
what they would cost.

Read 2026-09-05 on this branch by reading the payment daemon and its per-rail handlers. Counts are actual.

| What the design says | What the branch does | Where |
|---|---|---|
| the confirmation is a read of the rail at the transfer's declared terminal state, never the submission's return | **there is no read-back on either rail.** The bank handler inspects the status on the funding call's own return value and treats three values as sent — one terminal and two explicitly non-terminal — so a pending transfer is reported to the operator as complete, and a transfer accepted and later returned is recorded as sent permanently. The crypto handler's "sent" means a subprocess printed a line claiming a transaction identifier; no node, index, or memory pool is consulted, and the only verification is a link the operator may click | the two rail handlers |
| `dedup_key` is derived from the obligation and written before the first attempt | **no dedup key exists on a payment.** The rail is given a **freshly generated identifier on every attempt**, which deduplicates one retried call and does nothing about a second full attempt at the same obligation — the case that pays twice. What actually prevents a repeat is bookkeeping: a once-per-day file marker gating one leg, and archiving a profile after a one-off. The code notes that if the archiving write fails the profile may match again, and that the remaining protection is the operator noticing | `handlers/wise_transfer.py`; `monedula.py` |
| a timeout is `unknown`, and unknown holds; the adapter reads the rail before submitting again | **there is no unknown state.** Outcomes are three values, assigned immediately from the submission response. An exception anywhere in the bank flow becomes "manual required" — **including an exception raised after the transfer was instructed and accepted** — and that path tells the operator to pay by hand. So the design's single most dangerous outcome, advising a manual payment against a transfer that may still be moving, is the built path's default on a mid-flow failure. There is no reconciliation pass and no later sweep that would catch it | `handlers/wise_transfer.py` |
| `verify` and `reconcile` belong to a principal disjoint from the payer | **one process, one identity, one code path** loads the profile, previews, reads the approval, submits, declares the outcome, and writes the bookkeeping. Submitter and reconciler are the same, and the confirmation written is the submitter's own account of what it did. **No `reconcile` step and no transaction write by a disjoint principal exists**, and no payment workflow entity exists to declare them | `monedula.py` |
| the `payment` class resolves to `NEVER` at the action gate, and the checkpoint is what the operator resolves | **the gate is not consulted on the payment path at all.** The preview-and-wait is a chat exchange standing in for the consent checkpoint, with no action entity, no class, and no gate evaluation. The general gate exists and is used elsewhere | `monedula.py`; `lib/daemon_runtime/gating.py` |
| where a policy suppresses a payment's metadata, the adapter attaches none, and the suppression is a property of what it submits | **on the crypto rail the suppression is an instruction repeated three times in a prompt to a subprocess invoked with permission checks bypassed** — it rests on the subprocess complying, and nothing validates what was actually submitted. This is exactly the second property this document names: an instruction passed along is not a suppression. **On the bank rail there is no suppression at all**, and two fixed classification strings are attached to every transfer regardless of what the payment is | `handlers/btc_transfer.py`, `handlers/wise_transfer.py` |
| a balance is read, carries coverage, and is a condition a step owner reads | **nothing reads a balance.** The funding call names the balance as the source, but no sufficiency check precedes a submission and no balance is ever reconciled against | the two rail handlers |
| a fee is disclosed in what the operator resolves | **no fee is read, checked against a ceiling, or shown.** The quote prices the transfer implicitly and the figure never reaches the operator | as above |
| the terminal condition is declared, not assumed | **no depth, confirmation count, or terminal state is declared anywhere**; the crypto path treats a printed identifier as the end of the matter | `handlers/btc_transfer.py` |

**One part of the built path meets the design's bar, and it is worth naming** because it shows the standard
is reachable: the module that detects profiles which are active but cannot be acted on writes its escalation
to the record **first** and to the notification channel second, precisely because the notification path was
observed failing during the periods that most needed it; it deduplicates per profile and reason rather than
per occurrence, so the operator is not trained to ignore it; a changed reason re-escalates as new
information and an unchanged one re-escalates after a long interval so it cannot fade; and a run that could
not pay exits non-zero, so it no longer looks identical to a run with nothing to do. That is the quality bar
the confirmation path does not meet.

## Prior art

The published surfaces of bank-transfer and crypto rails are the source of the capability enumeration, read
2026-09-05; the lifecycle shapes, the idempotency mechanisms, and the finality models are the rails', and
the status and disposition columns are this document's. The distinction between "did my request arrive" and
"did the effect happen" is the standard framing of the exactly-once problem in distributed systems, and the
design's answer — a client-controlled key written before the attempt, plus reconciliation by reading the
ledger back — is the conventional one, applied here to a boundary where the cost of getting it wrong is
paid in money rather than in duplicated work. The anti-corruption layer (Evans) is the shape of the whole:
a rail's model — a status that sounds terminal, an idempotency key that covers one call, a reference field
that looks like an identifier — never becomes the domain's. Clark-Wilson's separation of duties is the
structural check the payment workflow applies, and this document extends it to the adapter, which is the
component that would otherwise collapse it by being the only one at the boundary.

## Beyond the sources

The mapping of rail signals to the four outcomes, the statement of what `dedup_key` is keyed on and the
per-class resolution of the unknown case, the ruling that a balance is an observation on an account's
artifact rather than an artifact of its own, the composition argument against a second gate, the
metadata-suppression refusal and its three properties, and the extension of the separation of duties to the
adapter are this document's, applying `adapters.md`'s rules to the rail classes the design contemplates.
Decisions 27, 28, and 29 were opened here and are ruled here; the reconciliation of decision 29's
amount-sensitivity with a per-rail criterion — the profile may deepen and never shallow — is this document's.
