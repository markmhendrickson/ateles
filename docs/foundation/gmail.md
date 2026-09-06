# Gmail: the mail system's full surface, mapped to the work model

**Keyed document:** read when the mail adapter, the mail poller or receiver, the outreach or triage roles,
the send path, or this document changes (`conformance.md`). **Kind:** foundation; states the design and
never the state of a checkout. **Derived from:** `adapters.md` (the two invariants, the four outcomes, and
the five adapter rules, which this document applies and does not restate), `work_model.md` (artifacts,
intake, the four execution mechanisms), `gates_and_workflows.md` (step state from edges; actions and the
action gate; the three verdict values), `workflows.md` (outreach, intake, operator-only),
`failure_posture.md` (the halt, the recovery per action class, the checkpoint reason classes), and the
Gmail REST API v1 surface as exposed by the `gws` CLI, read 2026-09-05, and PR #745 operator review
(2026-09-05, rulings 13–14, 16–18, 23–29: decision 23 ruled here). What is built, and which rows have
no code path, is `status.md`. Revised by the testability pass of 2026-09-06 (revision 37: refusal 1's mechanical half, the field allowlist on the grant). Revised by the event/signal/delivery pass of 2026-09-06 (revision 38: "Every inbound signal, and what it becomes" and its table headers, Linkage, Identity, and the disposition sentence renamed to `event`, matching `github.md`'s "Event and action" precedent; `signal` kept only where the design means the record's reading of an event).

## Purpose

Be the Gmail adapter in full: every state change the mail system can deliver or that a reader can discover
there, mapped to exactly one of the four outcomes `adapters.md` defines or to `dropped` with a reason;
every outbound operation a step takes on the mail system, with its action class and what confirms it
landed. `adapters.md` keeps the general adapter rules and carries a pointer here; those rules are cited
from this document and never restated in it (principle 9, one home).

The mail system differs from the code host in three ways that shape everything below, and they are stated
first because each one decides several rows.

**Mail carries the operator's personal correspondence, not a project's public record.** Every artifact in
this system is presumptively about identifiable people, much of it about people who never agreed to be
processed by a swarm. Where `github.md` narrows one class of inbound event for disclosure reasons, this
document narrows the **default** and widens by exception: the adapter's writes are minimized at capture, on
the RGPD Art. 6(1)(f) basis the project operates under (`CLAUDE.md`, people-data processing). That rule is
stated once, under *What this adapter refuses*, and cited from the rows it governs.

**A send is irreversible and reaches a person.** There is no unsend, no revert, and no forward-only
supersession that undoes the read. This makes the outbound half of this document unlike the code host's,
where a merge has a revert and a release has a supersession. The consequence is stated under *Outbound*
and it is the reason the outreach workflow spends three steps before the send.

**The system offers no delivery of most of what changes.** Gmail's change notification carries a mailbox
history marker and nothing else — not the message, not what changed about it. Everything the adapter knows
it knows from a **read**, which means the coverage half of the provenance rule does almost all the work
here, and a much larger share of this document's rows are conditions discovered by reading rather than
events delivered. That asymmetry is the subject of *What arrives, and what must be asked for*.

## Scope

Every state the mail system holds about the artifact kinds the work model names, plus the mailbox-level
settings whose change alters what the adapter can see or do. In scope: the inbound mapping per state
change, the outbound operation per step, the identity question the mailbox raises that the code host does
not, and the refusals. Out of scope: the general adapter rules (`adapters.md`), the workflows whose steps
take these operations (`workflows.md`), the gate's decision function (`gates_and_workflows.md`), what the
adapter is granted (`authority_model.md#grants`), and the per-instance binding of a mailbox to an operator,
which is a `channel_config` context entity resolved at runtime and never named here.

## What artifacts this system holds

| Kind | Identified by | What it is | Notes |
|---|---|---|---|
| `thread` | `system` = the mail system, `external_id` = the thread id | the conversation a batch attaches to | the unit intake links, and the unit `follow_up` watches |
| `message` | `system`, `external_id` = the immutable message id | one message within a thread, `PART_OF` it | the unit a send's confirmation mints; both levels are artifacts (decision 23, below) |
| `draft` | `system`, `external_id` = the draft id | a composition staged **in the mail system** | **the design does not use this artifact.** The draft that matters is the entity in the record (`workflows.md#outreach`); a mail-system draft is an optional convenience and never what a step closes on. See *The draft hazard* |
| `attachment` | `system`, `external_id` = the attachment id, which is scoped to its message | a file carried by a message | not independently addressable: the id is meaningful only with its message id, which is the identity wrinkle below |
| `label` | `system`, `external_id` = the label id | a mailbox-level classification | an artifact only in the sense that the adapter reads it; **never step state**, and the design writes at most a small closed set of them |

**The message id is stable and the thread id is not, quite.** A message's id does not change. A thread's
id is the id of its first message, and a message moved between threads by the system's own conversation
grouping changes which thread contains it without any event. So the linkage rule's pair resolves reliably
for a `message` and resolves for a `thread` with one caveat worth stating: a thread the record tracks may
gain messages the record has never seen, and the only way to know is to read the thread, which is the
coverage point again.

**Two ids for the same message, and only one of them is the record's.** The mail system's message id is
the system's own; the RFC 5322 `Message-ID` header is the wider mail world's and travels between systems.
The artifact's `external_id` is the **system's** id, because the artifact is by definition a record in that
system. The header id is a field the adapter may write onto the artifact as an observation, and it is what
lets a message the operator sent from another client be matched to an action's `dedup_key`. It is never the
identity.

## What arrives, and what must be asked for

This section exists because a reader carrying `github.md`'s model into this document will expect an event
stream and there is not one. The distinction decides how nearly every row below is worded, so it is drawn
before the tables rather than inside them.

Gmail offers one delivery mechanism: a **watch** on a mailbox, which posts a notification carrying the
mailbox's address and a **history id** — a marker in a per-mailbox change log — and nothing else. The
notification does not say what changed, does not carry the message, and is coalesced, so several changes
may produce one notification and one change may produce several. To learn what happened the adapter reads
the history log from its last known marker, and that read returns the changes as typed entries: messages
added, messages deleted, labels added, labels removed.

Three consequences, each of which is a design position rather than an implementation note:

**The notification is not the event; the history entry is.** So every inbound row below is keyed on a
history entry type or on a condition a read discovers, never on the notification. The notification's only
role is to wake the adapter, which is exactly the role `adapters.md` gives the record's own subscriptions —
a mechanism for waking a consumer, downstream of the thing that actually knows what happened.

**The history log expires, and an expired cursor is `unknown` rather than a resynchronization.** The system
retains the change log for a limited period and returns an error when asked for a marker older than it
keeps. The honest reading of that error is that an interval was **never read**, and the adapter's response
is to record coverage saying so — not to silently fall back to listing recent messages, which would produce
a record indistinguishable from one where nothing happened in the gap (principle 7). What the adapter does
next is read the mailbox for the interval it can still see and leave the gap readable as a gap. A gap that
matters is a `status.md` row, not a value the adapter invents.

**The marker is not the adapter's to keep.** A last-seen history id is exactly the "last-seen cursor table
standing in for coverage" that `adapters.md` forbids: a second copy of something the record already
reconstructs, needing a process to keep it true. The marker is derivable from the provenance of the
observations the adapter has written — the highest marker any observation on this mailbox cites — and it is
derived, not stored. This is the same ruling `adapters.md` makes about freshness, applied to the one place
in this system where an adapter is most tempted to break it. **The drift note below records that the built
path does keep such state.**

## Every inbound event, and what it becomes

The `system`/`external_id` pair identifies each artifact as the table above states. Rows are marked
**handled**, **deliberately ignored**, or **unhandled** — the third being a gap, which is a `status.md` row
and not a silent omission here.

### Messages

| Event | Status | Outcome in the record |
|---|---|---|
| a message added, on a thread the record does not track | handled | a task with the thread as its artifact, entering intake; the message is minted `PART_OF` the thread. The mail poller is the daemon mechanism of `work_model.md#the-four-execution-mechanisms`, which produces tasks and never receives one |
| a message added, on a thread attached to an open outreach batch, from the party the batch addresses | handled | an observation on the thread artifact; the `follow_up` step owner reads it as the reply the step closes on (`workflows.md#outreach`). **It closes no step by itself** — the step owner reads it and signs |
| a message added, on a thread attached to an open batch, from a third party the batch does not address | handled | an observation on the thread artifact. A thread gaining a participant is a fact about the thread, and it is one the `follow_up` step owner reads before signing, because a reply from someone else is not the reply the step waits for |
| a message added, from the operator's own address, answering a checkpoint the operator-facing agent carried by mail | handled | resolution of that checkpoint by the operator principal (`authority_model.md#approval`), read back on the checkpoint. From any other address, an observation — the identity rule, and the adapter never resolves an unrecognized address to the operator |
| a message added, in the sent folder, matching an action's `dedup_key` | handled | an action confirmation on the `send_external_comms` action, `taken_at` and `result_ref` naming the message id |
| a message added, in the sent folder, matching no action | handled | an observation, **and a defect to surface**: the mailbox sent something the record did not intend. It is the mail system's exact analogue of `github.md`'s merge-with-no-action row, and it is the signal that a send escaped the gate |
| a message added, that the system classified as spam or as a promotion | handled | an observation on the thread; **it yields no task for intake**. The exclusion is stated as a rule under *What this adapter refuses* rather than as a silent filter, because a filter nobody declared is the silent branch the disposition rule exists to close |
| a message added, to a mailbox the `channel_config` does not name | handled | `dropped`, reason `untracked_mailbox` |
| a message deleted (the history entry) | handled | an observation on the artifact (`state: deleted`). The record keeps the artifact and its observations; a message removed at the system is not a message the record never held |
| a message trashed or untrashed | handled | an observation on `labels[]`, trash being a label. It is not deletion and is reversible |
| a draft's message added or removed | deliberately ignored | `dropped`, reason `draft_not_used`: a mail-system draft is not a record the design tracks, and its message-level churn says nothing about any artifact. See *The draft hazard* |

### Threads and labels

| Event | Status | Outcome in the record |
|---|---|---|
| a label added to, or removed from, a message or thread | handled | an observation on `labels[]`. **A label naming a step is not that step's state**, and no label opens, claims, or closes anything. This is `adapters.md`'s rule and the mail system erodes it harder than the code host does, because a mailbox's labels are the operator's own working vocabulary and read as status to a human eye |
| a thread archived (the inbox label removed) | handled | an observation. Archiving is a label change and is not a completion: a task's status is written by its batch's sign-offs |
| a thread's read/unread state changed | deliberately ignored | `dropped`, reason `presentation_only`: read state is the reader's, changes without anyone acting on the work, and belongs to whichever client happened to render the thread |
| a thread marked important, or starred | deliberately ignored | `dropped`, reason `presentation_only`. **Priority is the `priority_rubric` entity's**, set at intake's `prioritize`, and a star is an external actor's opinion that the second invariant forbids reading as the record's |
| a message reported as spam, or as not-spam, by the operator | handled | an observation. It changes what later messages the adapter will see and is worth holding for that reason |
| a thread gaining a message while a `follow_up` step is open | handled | the message rows above; named here because it is the one thread-level condition a step owner actually waits on |

### Mailbox settings, which change what the adapter can see or do

These have no analogue in `github.md`'s tables and they matter more here, because several of them can
redirect or suppress mail without touching a single message.

| Event | Status | Outcome in the record |
|---|---|---|
| the watch subscription expiring, or being stopped | handled | an observation, **and announced on the off-record path**. A watch expires on a fixed horizon and must be renewed; an adapter whose watch lapsed looks exactly like a mailbox with no mail, which is `failure_posture.md` rule 2's failure at the boundary |
| an auto-forwarding rule created, changed, or enabled | handled | an observation, **and announced on the off-record path**. A forwarding rule sends the operator's mail somewhere else, standing, without any further action — the mail system's nearest thing to the code host's auto-merge, and read the same way: a permit nobody asked the gate for. The design's position is that the swarm never creates one; see *What this adapter refuses* |
| a filter created, changed, or deleted | handled | an observation, **and announced**. A filter can label, archive, forward, or delete matching mail before the adapter ever sees it, so a filter change silently alters the population of everything above |
| a vacation responder enabled or disabled | handled | an observation. It sends mail on the operator's behalf without the gate, which makes it worth recording even though the swarm never sets one |
| a send-as alias added, verified, or removed | handled | an observation. It changes which addresses the mailbox may send as, which is an input to the identity rule |
| a delegate added or removed | handled | an observation, **and announced**. A delegate can read and send as this mailbox, so the set of credentials that can produce a message from this address just changed — which bears directly on whether a sent message matching no action is a defect or a person |
| a forwarding address added or verified | handled | an observation; the same class as auto-forwarding, one step earlier |
| IMAP, POP, or language settings changed | deliberately ignored | `dropped`, reason `client_configuration`: these govern how other clients reach the mailbox and say nothing about an artifact |
| S/MIME or client-side-encryption settings changed | **unhandled** | an encrypted message is one the adapter can receive and not read, so the coverage of every observation on it is different in kind from a truncated page. Until decided, an observation, and the message body reads `unknown` rather than empty. See `status.md` |

### Everything else the mail system exposes

| Class | Status | Outcome in the record |
|---|---|---|
| every remaining surface — the profile, quota and mailbox statistics, per-message classification labels, the history log's own metadata | deliberately ignored | `dropped`, reason `out_of_scope_class`: the class says nothing about an artifact the work model names. The drop is counted like every other, so a class becoming relevant appears as a rising count rather than as silence |

## Conditions that are not delivered at all

As in `github.md`, some state the swarm depends on is not delivered and is **read**; a read produces an
observation with sourcing and coverage like any other. This system has proportionally far more of these,
for the reason *What arrives* gives.

| Condition | How it reaches the record | Why it is not delivered |
|---|---|---|
| the full content of a message — body, headers, recipients | an observation on the artifact, from a read the hydration phase makes | the notification carries a history marker only; nothing about a message arrives with it |
| whether a thread has messages the record has never seen | an observation carrying the thread's message set and the coverage of the read | no per-thread completeness signal exists; only a read of the thread answers it |
| whether a sent message was **delivered**, as opposed to sent | it does not; the record holds that the message was sent and nothing more | the system reports acceptance, not delivery. A bounce arrives later as a *new message* from a mailer daemon, which is an ordinary inbound message and not a status on the original. See the row below |
| a bounce or a delivery-failure report | an observation on the thread the bounce concerns, where the adapter can match it, **and never an action confirmation reversal** | it is a new message that happens to be about an old one; matching it is a read and a heuristic, and where the match is uncertain the observation says so |
| whether an address is still valid, or a person still reads it | it does not | nothing reports it, and inferring it from silence is the fabrication `adapters.md`'s unknown rule forbids |
| whether the operator has read a thread the swarm is waiting on | it does not, and the design does not ask | read state is `presentation_only` above; a step waiting on the operator waits on a checkpoint, not on a read receipt |

**A bounce is the clearest case where the mail system's shape breaks the confirmation model, and the design
does not paper over it.** The `send` step closes on the message read back from the sent folder, which
confirms the action's effect exists: a message was sent. It does not confirm arrival, and no read of this
system ever will. So `send`'s confirmation means exactly what it says, and the follow-up step is where a
bounce is noticed — by its owner, reading an observation, and signing. The design's position is that
promoting a bounce to an automatic retraction of the action's confirmation would assert a fact
("this was not delivered") that the adapter cannot establish and that a mailer's report only suggests.

## A thread and its messages are each artifacts, related by `PART_OF`

**Ruled (decision 23, 2026-09-05, together with decision 24): both levels are artifacts.** Registered in
`conformance.md#the-register-of-open-design-decisions`. A `thread` is an artifact and each `message` in it is
an artifact, and a message is `PART_OF` its thread (`data_model.md#relationships`). The question as it was
opened — which of the two is *the* unit a batch addresses — was a false dichotomy: the mail system gives an
id to both levels, the design already identifies an artifact by `system` and `external_id`
(`adapters.md#what-the-adapter-does-with-every-event`, linkage), so each qualifies, and the tables above were
already writing to both. What was undecided was only how the two relate and which one a given read, write, or
task points at, and that is what is ruled. The general rule — where an external system gives ids to two
levels of one thing, each level is an artifact and the contained one is `PART_OF` the containing one — is
stated once, under linkage in `adapters.md`; this section applies it to the mail system.

**Linkage, per direction.** Inbound, an event links to the artifact whose id it carries: a history entry
naming a message id lands as an observation on that message, minted `PART_OF` its thread where the record
does not yet hold it, and the thread is reachable from it by the edge; an event about the thread as a whole
— a label applied to it, its archival — lands on the thread. Outbound, an action refers to the unit whose id
its operation needs: `send` creates a message, and the confirmation mints that message `PART_OF` the thread
the send began or joined; `follow_up` replies on the thread, and its confirmation mints the reply the same
way; a label or an archive operates on the thread. A task refers to whichever unit it names — intake's `link`
step attaches the thread for a correspondence, and a task about one message attaches that message — and a
step owner reading either reaches the other along the edge. So `follow_up`'s closing condition, "a reply is
linked as an artifact" (`workflows.md#outreach`), names exactly what the record holds: a message artifact,
from the party the batch addresses, `PART_OF` the thread the batch attached.

**What a sign-off pins.** The pinning rule (`data_model.md#record-conventions`) relies on each unit pinning
what it is. A message is immutable, and a sign-off that judged one pins it outright. A thread's state is its
membership, and a sign-off that judged a thread pins the message set the read returned, with the coverage of
that read — so "what did this sign-off judge" resolves to a named set of messages, and a thread that has
since gained one reads as changed by the same derived comparison a moved head does. The weakness the open
question feared — a container whose membership changes without notice — is real, and it is answered by
coverage rather than by choosing the message as the only artifact: the record states which messages the
thread held when it was read, and a thread read at a truncated page is distinguishable from a thread with
nothing further.

**The id caveat, resolved by the edge.** A message the system regroups into another thread changes which
thread contains it without any event (above). Under this ruling that is not a re-identification: the message
keeps its `external_id`, its `PART_OF` edge to the old thread is ended, a new one is written to the thread
the read found it in, and both stay readable — an observation about containment, never a change of identity.

**Why both, rather than one.** The message alone would have made a correspondence many artifacts with no
single one a batch is about, and `follow_up` watching a relationship among messages rather than a thing; the
thread alone would have made every sign-off pin a set and every send's confirmation mint nothing with an id
of its own. Each answer was right about the level it chose and wrong to exclude the other, and the same rule
serves every future system with nesting — a pull request and its review threads, a channel and the messages
in it — so the record's readers carry one rule rather than one per system. **The cost accepted** is two
artifacts where a flatter model would hold one, and an edge the system's own regrouping can move. **What
would reopen it:** an external system that gives an id to only one level — then only that level is an
artifact, which the rule already implies, and nothing reopens.

## Outbound: the operations the workflows take on the mail system

Every row is an `action`, created when the effect becomes known and evaluated at the action gate at the
moment it would be taken (`gates_and_workflows.md#actions-are-entities-only-actions-are-taken`). The
adapter performs the operation on permit, performs nothing on a checkpoint, and **confirms by reading the
mail system back** — never by the operation's return code.

| Step or workflow | Operation | Action class | What the action gate does with the class | What confirms it landed | `dedup_key` keyed on |
|---|---|---|---|---|---|
| `send` (outreach) | send a message | `send_external_comms` | ordinarily a checkpoint; `consent` is the step that carries it, and the operator resolves it | the sent message read back **by its message id**, from the sent folder, never inferred from the send call's return | the batch, the step, the recipient set, and a digest of the reviewed body — so that a re-claimed task recomposing the same message produces the same key |
| `follow_up` (outreach) | send a reply on the thread | `send_external_comms` | the same; a follow-up passes the same gate as the first message (`workflows.md#outreach`) | the reply read back by message id, **and its thread id equal to the thread the batch addresses** | the batch, the step, the thread, and the follow-up's ordinal |
| `present`, `consent` where the channel is mail | carry a checkpoint or an operator-only task to the operator by mail | `notify_operator` | low blast under a policy that lists it; unlisted resolves to `NEVER` | the message read back by id; **the checkpoint stays open until resolved on the record**, whatever the delivery status | the checkpoint id |
| `persist`, `record` | apply or remove a label on a thread | `external_api_write` | as the policy lists it | the thread's labels, read back | the thread and the label set intended |
| `persist`, `record` | archive a thread (remove the inbox label) | `external_api_write` | as the policy lists it | the thread's labels, read back, without the inbox label | the thread |
| any step | stage a draft in the mail system | `external_api_write` | as the policy lists it | the draft exists, read back by draft id | the batch and the step |
| recovery of a send | **there is none.** See below | — | — | — | — |

**The recovery row is empty and that is the design's answer, not an omission.** `failure_posture.md` names,
per action class, what undoes an effect already taken; for `send_external_comms` at this system there is
nothing that does. A sent message cannot be recalled, and the mail system offers no forward-only
supersession that changes what the recipient read. What exists is a **new message** correcting the first,
which is a new outreach task through its own workflow, with its own draft, review, consent, and send. The
design states this explicitly because the alternative — an adapter with a "retract" operation — would be
the simulated reversal `adapters.md` forbids: performing something that looks like an undo for a system
that does not offer one. The practical consequence is that the gate is the only control, which is why
`consent` is a required step and why the class is ordinarily a checkpoint.

**The draft hazard, and why the design does not stage in the mail system.** The mail system's draft-update
operation, given a draft id together with a message body and a thread id, **can send the draft** rather
than replace its content — the effect depends on what the request carries, and an edit and a send are not
distinct operations at the boundary. That makes editing a staged draft in place an operation whose class
cannot be determined from the operator's intent, which is the property `gates_and_workflows.md` requires an
action to have: the gate evaluates a class, and a class that may turn out to be `send_external_comms`
cannot be gated as `external_api_write`.

The design's response is not to gate the operation more carefully; it is to **not depend on it**. The
staging that matters is the draft in the record (`workflows.md#outreach`), which is where `review` and
`consent` do their work and which no mail-system operation can accidentally deliver. A mail-system draft is
therefore optional, write-once, and never updated: where a staged draft must change, the adapter **creates
a new draft** and the superseded one stays where it is. Stated as a rule under *What this adapter refuses*.

This is the same shape as the code host's auto-merge and is worth naming as such: in both cases the
external system offers an operation that crosses the boundary with an effect the gate did not authorize.
The code host's version is a standing arming; the mail system's is an ambiguity in a single call. The
design's answer is the same in both — the swarm does not use the operation.

## How the five rules apply to this system

**Identity.** The actor of an inbound event is an **email address**, and the adapter resolves it through
the credential binding (`authority_model.md#principals`). This system makes the rule harder than the code
host does, in three ways worth stating because each is a way it fails quietly:

An address is **not an account**. Anyone can put any address in a `From` header, and the mail system's own
authentication results — whether the sending domain authorized the message — are a field the adapter reads
and records, not a fact the transport guarantees. So a message from the operator's address resolves to the
operator principal only where the binding matches **and** the message's authentication results carry the
domain's authorization; where they do not, or where the adapter cannot read them, the resolution is
`unknown` and the outcome is an observation. This is the fail-closed direction (principle 5): the field
carrying the safety meaning is the one that decides whether an inbound message can resolve a checkpoint,
and an unreadable value there is not a pass.

A mailbox has **many addresses**. Aliases, send-as addresses, and delegates all produce mail from the same
mailbox, and a delegate is a different person with the same outward address. The binding is to a
credential, and where the credential the mail system reports is the mailbox rather than the human, an
inbound message resolves to the mailbox's principal and no further. The adapter never infers which human
behind a shared mailbox wrote a message.

**The operator's own mail is the largest source of messages the swarm did not send.** The operator sends
mail by hand constantly, from clients the swarm knows nothing about. Every such message appears in the sent
folder matching no action. The row above calls that a defect to surface, and it would be the wrong reading
here if taken literally at every occurrence — so the rule is narrowed by its purpose: the defect is a sent
message **on a thread a batch addresses**, where the record intended a send and cannot account for the one
that happened. A sent message on an untracked thread is an ordinary new record, and it takes the
untracked-thread path.

**Linkage.** An inbound event names a message id and a thread id; the adapter finds the artifact by
`system` and `external_id`. The system-specific point is the one the identity section already touched: a
reply arriving on a tracked thread links by thread id, which the mail system supplies, and does **not**
depend on parsing the `In-Reply-To` and `References` headers — those are the fallback for a message whose
thread id the system did not group as expected, and a link made from them carries lower coverage and says
so. The adapter never attaches a thread to a batch on its own guess: intake's `link` step does that
(`workflows.md#intake`).

**Dedup.** Inbound: each history entry carries its own marker, and the write it produces is keyed on the
mailbox and that marker, so a redelivered notification that causes the same interval to be read twice lands
once. This is the system's substitute for a delivery id, and it is weaker in one specific way — the marker
identifies a **position in a log**, not a delivery, so two adapters reading the same interval produce the
same keys, which is the desired behaviour, while one adapter reading an interval twice at different
granularities may produce keys at different positions. Where that is possible the write is additionally
keyed on the message id it concerns, which is stable.

Outbound: every action carries its `dedup_key` as tabled above, and the adapter refuses to take an action
whose key it has already confirmed (`work_model.md#at-least-once-implies-effect-dedup`). This system is the
one where the rule matters most, because the effect is irreversible: an action whose key is present but
**unconfirmed** is reconciled by reading the sent folder for a message matching the intended effect
*before* submitting again, which is `failure_posture.md` rule 6's shape — a refusal on an existing key is
stronger evidence of a prior commit than a success response is of the present one. An adapter that resends
on an unconfirmed key has sent the message twice to a person.

**Unknown, and every delivery's disposition.** An event the adapter cannot map is an observation that says
so and is never coerced to the nearest outcome (principle 7). The system-specific values that must stay
`unknown` rather than defaulting: a message body the adapter could not decode or decrypt; an authentication
result it could not read; a history interval it could not fetch because the marker expired; and a
thread read whose page was truncated. Each of these has an obvious wrong default — empty body, unauthenticated,
nothing happened, no more messages — and each wrong default is indistinguishable, downstream, from the true
version of the same value.

Every event resolves to one of the four outcomes or to `dropped` with a reason, counted per window and
surfaced on the off-record announcement path. The reasons this document introduces: `untracked_mailbox`,
`draft_not_used`, `presentation_only`, `client_configuration`, `out_of_scope_class`, and the refusal
reasons below.

**Provenance and read-back.** Every write names the adapter, the mail system, the mailbox, and the history
marker or message id it derives from. Coverage carries more weight here than at any other system in this
design, for the reason *What arrives* gives: almost everything is a read, and a read of a mailbox is
paginated, filtered by a query the adapter chose, and subject to the system's own rate limiting. So an
observation on a thread states which messages the read returned and whether the page was complete; an
observation from a list states the query and the window. Without that, a thread read at a truncated page
and a thread with nothing further are the same record.

## What this adapter refuses

Seven refusals. The first is a standing constraint on every write; the rest are operations the adapter does
not perform.

**1. It minimizes at capture, and the minimization is on the write.** The record holds what serves the
relationship and the work: who wrote, when, on what thread, what the message concerns, and what it commits
anyone to. It does not durably persist incidental sensitive disclosures — health, finances, family
situations, political or religious views — into contact profiles or observations, and it summarizes rather
than transcribes where the detail is sensitive and incidental (`CLAUDE.md`, people-data processing;
RGPD Art. 9). As with `github.md`'s security narrowing, this is a constraint on what the adapter **writes**,
not on what it reads, and it is the stricter of the two available places to put it: material the record
never held cannot be copied out of it by a later summary, digest, notification, or rendered page. The
observation's coverage states that fields were withheld by policy, distinguishing "the adapter did not read
this" from "the adapter read it and did not write it" — which are different facts about the record. The
mechanical half is the grant: the fields this adapter may write on a `contact` are the allowlist its grant's
parameter constraints name, and a write outside them is denied at admission (`authority_model.md#grants`);
what within an admitted field is incidental or sensitive is the judgement this refusal states.

**2. It never sends without an action that passed the gate.** No convenience path, no retry that resends,
no "just acknowledging receipt". Every message leaving the mailbox by the swarm's hand is a
`send_external_comms` action with a `dedup_key`, and the sent-message-matching-no-action row exists to make
a violation of this readable.

**3. It never updates a draft in place.** The operation can send. Where a staged draft must change, a new
draft is created and the old one is left. See *The draft hazard*.

**4. It never creates, changes, or enables a forwarding rule, a filter, or a vacation responder.** Each is
standing configuration that acts on mail with no action, no gate, and no confirmation — the permit-the-gate-did-not-issue
shape. The adapter observes them and announces changes; it does not write them. An operator who wants one
sets it by hand, which is an operator-only task and not an adapter capability.

**5. It never permanently deletes a message, a thread, or a label.** The system offers immediate permanent
deletion distinct from trashing, and it is not an operation this design has any use for: a superseded
effect stays readable, which is the same rule the code host's `publish` recovery states. Where mail must
leave the inbox, the operation is archive, which is a label change and is reversible.

**6. It never treats a label as step state, and never writes a label to communicate one.** Reading is
covered by the tables; the writing half is stated here because it is the tempting error. A swarm that
labels a thread `awaiting-reply` has built a second place where step state lives
(`gates_and_workflows.md#declaration-batch-projection`), one that an external actor can edit and that no
sign-off backs. The small closed set of labels the design does write are the operator's own filing
conventions, applied at `persist` or `record`, and nothing derives from them.

**7. It never adds a delegate, a send-as alias, or a forwarding address.** Each widens the set of
credentials that can produce mail from this mailbox, which is `authority_model.md`'s to decide and never an
adapter's to infer.

## What the design uses, and what the API offers that it does not

The condensation pass needs this distinction, so it is stated as a table rather than left implicit. The
right column is inventory: capabilities that exist and that this design has no step reaching for.

| API capability | Design's use |
|---|---|
| read a message, a thread; list messages, list threads | **used**, and it is the primary inbound path |
| the mailbox watch and its history log | **used** — as a wake-up, with the history entries as the event |
| send a message; send a reply on a thread | **used**; the outreach workflow's whole outbound half |
| read the sent folder | **used**, for confirmation and for the escaped-send defect |
| modify labels on a message or thread; archive | **used**, narrowly, at `persist` and `record` |
| read an attachment | **used** where a step declares it; the attachment is an artifact of the message |
| read mailbox settings — filters, forwarding, delegates, aliases, vacation | **used, read-only**, because each changes what the adapter can see; never written |
| create a draft | **available and not depended on**: the design's staging is the draft in the record. Permitted as a convenience; no step closes on it |
| **update a draft** | **refused.** It can send. Refusal 3 |
| **send a draft** | **not used.** A send is composed and sent as a message; routing it through a staged draft adds a step and a hazard with no benefit |
| **permanently delete** a message, thread, or draft | **refused.** Refusal 5 |
| batch-modify and batch-delete many messages at once | **not used.** The design's writes are per-artifact and confirmed per-artifact; a batch operation's partial failure has no per-item confirmation to read back |
| import, and insert, a message into the mailbox | **not used.** Each writes a message into the mailbox without sending it, and the design has no case for manufacturing mail the mailbox appears to have received |
| create, update, or delete a **label definition** | **not used.** The design writes label *applications* from a closed set the operator maintains; defining new labels is the operator's taxonomy |
| create or delete a **filter** | **refused.** Refusal 4 |
| update auto-forwarding; create or verify a forwarding address | **refused.** Refusal 4 |
| update the vacation responder | **refused.** Refusal 4 |
| add or remove a **delegate**; create or verify a **send-as alias** | **refused.** Refusal 7 |
| update IMAP, POP, or language settings | **not used.** `client_configuration`; no bearing on any artifact |
| S/MIME and client-side-encryption key management | **not used**, and the read side is unhandled — see the settings table |
| the mailbox profile, and quota statistics | **not used.** Inventory only |

Roughly half the surface is inventory: of the operations the CLI exposes, the design reaches for reads,
send, reply, label application, archive, attachment read, and settings reads, and leaves the rest either
refused (seven operation families) or unused (nine).

## Freshness

**Written against** the Gmail REST API v1, as exposed by the `gws` CLI's `gmail` command tree, enumerated
**2026-09-05** by reading the CLI's own help for every resource and sub-resource: `users` and its
`getProfile`, `watch`, and `stop`; `drafts` (six methods), `history` (one), `labels` (six), `messages`
(eleven plus `attachments.get`), `threads` (six), and `settings` (ten methods plus the `filters`,
`forwardingAddresses`, `sendAs`, `delegates`, and `cse` sub-resources). The change-notification behaviour and
the history log's retention are the API's documented behaviour, not measured here.

**What would make this stale, in order of likelihood.** A new method or resource on any of those trees, which
would appear as a row this document does not name — and would surface as a rising `out_of_scope_class` drop
count only if it produced inbound events, so an outbound capability added by the vendor is the case this
document's own controls do **not** catch. A change to the history log's retention, which changes how large a
gap an expired marker implies. A change to what the change notification carries: if it ever carried the change
itself, the whole of *What arrives* would be rewritten and many rows would become events rather than reads.
A change to whether `drafts update` can send, which is the premise of refusal 3 — if the vendor separated the
two operations, refusal 3 would become unnecessary, and it should be re-verified rather than assumed
permanent. And any change to the authentication-results field the identity rule fails closed on.

**A stale table reports without binding.** The dispositions above are only as good as the enumeration
behind them, so the enumeration is dated and its instrument named (principle 8). Re-run the same
enumeration before relying on the completeness claim; the per-row design rulings do not depend on it, but
the claim that the rows are exhaustive does.

## Drift: what the built path does that this design does not say

Recorded as drift, not as design justification. An existing implementation is never a reason the design
must accommodate it; these rows say what would have to change, not what should be written differently.

Read 2026-09-05 on this branch by enumerating every `gws gmail` and `gws calendar` call site under
`execution/` and `lib/`, and reading each module. Counts are actual.

| What the design says | What the branch does | Where |
|---|---|---|
| a send is confirmed by reading the sent message back by its message id | **four send call sites, zero read-backs.** Each returns success on the subprocess exit code and captures no message id — the "a response code is not evidence" shape principle 2 names. One module compensates for the system rewriting the outgoing header id by never reading it back at all, deriving threading from a synthetic deterministic id plus a subject token instead | `lib/approval/email_channel.py` (two sites), `lib/notify/notifier.py`, `execution/daemons/phoenicurus-release/prepare.py`, `lib/daemon_runtime/run_email.py` |
| the artifact is minted from the confirmation, with its `external_id` already known | there is no id to mint one from, and **no `artifact` entity type exists on the branch** — the migration cost the revision 8 rows already carry | as above |
| a label is never step state | **two daemons read a processed-label as the done condition.** One excludes `-label:<name>/processed` in its poll query and writes the label to stop re-notification, mutating the unread flag in the same write; the other refuses a message carrying its own processed label. Each is a second place step state lives, editable by any external actor and backed by no sign-off | `execution/daemons/turdus/turdus.py`, `execution/daemons/riparia/riparia.py` |
| an adapter keeps no history of its own | **three adapter-local state files.** One holds a `last_message_id` cursor, a 500-entry dedup set, a processed count, and a last-poll timestamp; a second holds a once-per-day marker gating the calendar leg; a third holds a chat cursor | `execution/daemons/turdus/.turdus_state.json`, `execution/daemons/monedula/.monedula_last_run`, `.monedula_tg_offset` |
| every delivery resolves to an outcome or to `dropped` with a counted reason | **four silent branches.** An inbound message failing the process test is skipped with no counter, log, or record; a partially matched message is logged and abandoned; a message classified as noise is marked handled and dropped uncounted; an informational message is stored with no task, no label, and no notification. Each is the receipt-without-disposition shape the disposition rule exists to close | `riparia.py`, `turdus.py` |
| inbound arrives by the mailbox watch, and the history log is the event | **neither is used.** Zero occurrences of the watch, of Pub/Sub, or of the history log repo-wide; inbound is six polling sites issuing list-and-read queries. This is not a defect against a built design — it is the built path having chosen the other of the two the vendor offers, and it means the expired-marker reasoning above has no code it corresponds to | `turdus.py`, `riparia.py`, `email_channel.py` |
| the design does not depend on a mail-system draft, and never updates one | **consistent, and by absence:** zero production call sites for any draft method. Drafts are staged only by agent instruction. The send-gate hook blocks the update, send, and helper shapes at the tool boundary with a per-command inline override — but the daemons' own send paths run as launchd subprocesses and are not subject to it | `.claude/hooks/gmail_send_gate.py`; the send sites above |

The refusals this document states are therefore **partly already true by absence** (drafts, filters,
forwarding, delegates, permanent deletion: no call sites) and **partly contradicted** (labels as state,
local cursors, uncounted drops). Nothing above argues for changing a ruling; it names which behaviour has
to change when the `artifact` and `action` entity types are built.

## What this document does not decide

The general adapter rules are `adapters.md`'s and are cited here, not restated: the four outcomes, the five
rules that decide among them, the sourcing and coverage contract, and the rule that a recovery is an
outbound operation like any other. The step lists that take these operations are `workflows.md`'s. The
gate's decision function is `gates_and_workflows.md`'s. Whether adapters live in a repository of their own is
open decision 15 (`adapters.md`). Where inbound deliveries land is ruled (decision 16,
`adapters.md#where-inbound-delivery-lands-the-adapter-verifies-and-identifies-it-and-the-records-own-subscriptions-are-not-it`),
and for this system it means: the process that receives the mailbox watch's notification may be shared
plumbing, and verifying its envelope and extracting the history marker from it are this adapter's. Which
rows have a built path is `status.md`'s.

## Prior art

The Gmail REST API's own resource and method reference is the source of the enumeration, read 2026-09-05
through the `gws` CLI; the resource names and their methods are the vendor's, and the status and
disposition columns are this document's. The distinction between a mailbox's change log and the content of
what changed — a notification that carries a cursor rather than a payload — is the vendor's own design and
is what *What arrives* is about. The anti-corruption layer (Evans) is the shape of the whole: the mail
system's model — labels as state, a starred thread as a priority, a draft as staging — never becomes the
domain's.

## Beyond the sources

The per-event mapping, the handled / deliberately ignored / unhandled marking, the seven refusals, the
capture-minimization rule stated as an adapter constraint rather than a workflow one, the treatment of a
bounce as an ordinary inbound message rather than a status on the original action, the empty recovery row
and its justification, and the ruling of decision 23 are this document's, applying `adapters.md`'s rules to
the mail system's full surface. The draft-update hazard is the vendor's documented behaviour; reading it as an
action whose class cannot be determined at the gate, and refusing the operation rather than gating it more
carefully, is this document's.
