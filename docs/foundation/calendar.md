# Calendar: the calendar system's full surface, mapped to the work model

**Keyed document:** read when the calendar adapter, the scheduling or meeting-processing roles, the
calendar-driven daemons, or this document changes (`conformance.md`). **Kind:** foundation; states the
design and never the state of a checkout. **Derived from:** `adapters.md` (the two invariants, the four
outcomes, and the five adapter rules, which this document applies and does not restate), `work_model.md`
(artifacts, intake, the four execution mechanisms), `gates_and_workflows.md` (step state from edges;
actions and the action gate), `workflows.md` (meeting processing, outreach, operator-only),
`failure_posture.md` (the halt, the recovery per action class), `gmail.md` (the sibling system, whose
identity and minimization rules this document shares), and the Google Calendar REST API v3 surface as
exposed by the `gws` CLI, read 2026-09-05. What is built, and which rows have no code path, is
`status.md`.

## Purpose

Be the calendar adapter in full: every state change the calendar can deliver or that a reader can discover
there, mapped to exactly one of the four outcomes `adapters.md` defines or to `dropped` with a reason;
every outbound operation a step takes on the calendar, with its action class and what confirms it landed.
`adapters.md` keeps the general adapter rules and carries a pointer here; those rules are cited from this
document and never restated in it (principle 9, one home).

Three properties distinguish this system from the code host and from the mail system, and each decides
several rows below.

**A calendar event is simultaneously a record and a message to other people.** An event with attendees is
not a private note that happens to be shared — creating, moving, or cancelling it **sends mail** to
everyone on it, and does so as a consequence of a write that looks like an ordinary update. This is the
single most consequential fact about the outbound half of this document: an operation whose action class
would be `external_api_write` on a solo event is `send_external_comms` on an event with attendees, and the
adapter cannot tell which it is without reading the event first. The rule that follows is under *Outbound*.

**An event exists in time, and time passes without any event being delivered.** The most important thing a
calendar event ever does — begin, and end — produces no signal at all. A meeting that ended is a fact the
record learns by reading a clock against a stored time, not by receiving anything. So the transitions that
matter most to the workflows here are in *Conditions that are not delivered*, and they are the majority
rather than the exception.

**A recurring event is one declaration and many occurrences, and the API lets you address either.** This is
the question the operator's brief flags and this document opens as a numbered decision rather than
resolving, because it is genuinely undecided and both answers cost something. It is **open decision 24**
below. It is not the same thing as an `action series`, which is a term about graduated autonomy over
repeated actions and has no relation to a repeating calendar entry; the two are kept apart deliberately.

## Scope

Every state the calendar holds about the artifact kinds the work model names, plus the calendar-level and
sharing surfaces whose change alters what the adapter can see or do. In scope: the inbound mapping per
state change, the outbound operation per step, the recurrence question, and the refusals. Out of scope: the
general adapter rules (`adapters.md`), the workflows whose steps take these operations (`workflows.md`),
the gate's decision function (`gates_and_workflows.md`), what the adapter is granted
(`authority_model.md#grants`), and the per-instance binding of a calendar to an operator, which is a
`calendar_routing_config` context entity resolved at runtime and never named here.

## What artifacts this system holds

| Kind | Identified by | What it is | Notes |
|---|---|---|---|
| `event` | `system` = the calendar system, `external_id` = the pair (calendar id, event id) | one entry on one calendar | the pair is the identity, not the event id alone: the same event on two calendars has two ids, and moving it between calendars changes it. See *Identity* |
| `event`, an occurrence of a recurring series | `system`, `external_id` = the pair above plus the occurrence's start instant | one dated instance of a repeating declaration | whether this is an artifact at all is **open decision 24** |
| `calendar` | `system`, `external_id` = the calendar id | the container events live on | an artifact only in that the adapter reads its metadata; the design writes none |
| `attendee` | **not an artifact.** A person on an event is a `contact` entity in the record, referenced from the event's observations | — | an attendee has no independent existence at the calendar; the design does not mint an artifact per invitee, which would be a per-person record built from someone else's meeting |

**The iCalendar id is the third id, and it is not the identity either.** An event carries an interoperable
`iCalUID` that survives being copied between calendars and between calendar systems, alongside the
system's own event id. As in `gmail.md`, the artifact's `external_id` is the **system's** id, because an
artifact is by definition a record in that system; the interoperable id is a field the adapter writes as an
observation, and it is what lets the same meeting on the operator's calendar and on a counterparty's be
recognized as one meeting. It is never the identity, because it is not unique per calendar: an event on
two calendars shares it.

## Open decision 24: whether a recurring series is one artifact or many

The brief that commissioned this document flagged the question; writing the tables confirmed it is real and
that the design has not answered it. It is opened here rather than resolved.

**The question.** The calendar holds a recurring event as **one** record — a declaration carrying a
recurrence rule — and computes its occurrences on demand. It also lets any single occurrence be modified or
cancelled independently, at which point that occurrence becomes a real stored record of its own, with its
own id, pointing back at the series. So the system itself holds a series as one-record-until-someone-edits-an-instance,
at which point it is one-plus-N. The design must say what an artifact is here, and the two answers are:

**The series is the artifact.** One `event` artifact per declaration; occurrences are derived by reading the
recurrence rule, and are not artifacts. This matches the calendar's own primary representation, keeps one
row for a standing weekly obligation rather than an unbounded stream of them, and means a change to the
series is one observation rather than a hundred. The cost is that nothing a batch does can be pinned to a
particular occurrence: a task created because *this Thursday's* meeting needs preparing refers to an
artifact whose state is "every Thursday", and a sign-off on it pins a declaration rather than a dated fact.
It also makes the meeting-processing workflow awkward, since a transcript belongs to one occurrence and the
artifact it links to would cover all of them.

**Each occurrence is the artifact.** One `event` artifact per dated instance the swarm actually touches,
with the series as a relationship among them. Every artifact then pins something concrete, which is the
property `data_model.md#record-conventions` relies on, and meeting processing links a transcript to the
meeting it is a transcript of. The cost is that occurrences are unbounded — a daily standing event has no
last occurrence — so the adapter must decide *which* occurrences become artifacts, and the honest answer is
"the ones a batch addresses", which makes artifact existence depend on the swarm's own attention rather
than on the external system's contents. That is a genuine departure from how every other artifact in this
design comes to exist.

**A third answer the design should consider and this document does not adopt:** the series is the artifact
*and* an occurrence the swarm touches is minted as its own artifact `PART_OF` the series, so the common
case costs one row and the touched case pins a date. It is not adopted here because it means one external
record maps to two artifact kinds depending on history, and whether that is a clean modelling of the
calendar's own one-plus-N behaviour or an unnecessary complication is exactly what should be decided rather
than assumed.

**What would decide it:** whether any step needs to pin an occurrence, which meeting processing appears to
need and nothing else clearly does; and whether a standing obligation is better read as one artifact the
record holds or as a rule the record evaluates. Nothing else in this document depends on the resolution —
every row below states which of the two it writes to where the distinction arises, and the tables stand
either way.

**This is the calendar's form of the question `gmail.md` opens as decision 23** about a thread and its
messages. Both ask whether a thing with internal multiplicity is one artifact or many. They should be
decided together: a different answer in each would be a distinction every reader of the record has to carry
with no reason behind it.

**Drift bearing on the decision, recorded as drift and not as an argument.** Every calendar read on the
branch passes the flag that expands a series into its occurrences server-side, so the built path never sees
a recurrence rule at all and has effectively taken the second answer without stating it. The count and the
citations are in *Drift* below. That a built path chose one does not rule the question — the operator's
standing instruction is that the code is not established design guidance — but it does mean the decision has
a de facto answer in force today, which is worth knowing when it is taken.

## Every inbound signal, and what it becomes

The calendar offers a **watch** on events, on the calendar list, on access rules, and on settings, which
posts a notification when something in the watched collection changes. As in `gmail.md`, the notification is
a wake-up rather than the event: it carries a resource identifier and a change token, not the changed
content, and the adapter learns what changed by reading with a sync token that returns entries changed since
the last read. Rows are keyed on what that read discovers.

Rows are marked **handled**, **deliberately ignored**, or **unhandled** — the third being a gap, which is a
`status.md` row and not a silent omission here.

### Events

| Signal | Status | Outcome in the record |
|---|---|---|
| an event created on a tracked calendar, that the record does not track | handled | an artifact; **and a task for intake where the event carries an ask** — a meeting to prepare for, a recurring obligation the calendar drives. An event carrying no ask is an artifact and no task, which is a valid outcome and not a drop |
| an invitation received (an event created on the operator's calendar by someone else) | handled | an artifact, and a task for intake. It is distinguished from the row above by the organizer not being the operator, and the distinction matters because the response is an outward-facing act — see the outbound table |
| an event updated: title, description, location | handled | an observation on the artifact |
| an event moved in time | handled | an observation on the artifact's start and end. **A task whose due date follows the event reads it at `prioritize` or at claim, never through the event** — no event rewrites a task's priority, which is the `priority_rubric` entity's (`workflows.md#intake`) |
| an event cancelled | handled | an observation (`state: cancelled`). It closes no step and completes no task: a batch's steps close on sign-offs, and a meeting that will not happen is a fact its step owner reads |
| an event deleted outright | handled | an observation (`state: deleted`). The record keeps the artifact and its observations |
| an occurrence of a recurring series modified or cancelled independently | **unhandled** | this is the case that turns one record into one-plus-N, and what the record should hold depends on **open decision 24**. Until decided, an observation on the series artifact naming the affected instant, and never a new artifact. See above |
| a recurrence rule changed on a series | **unhandled** | every future occurrence moved at once, with no signal per occurrence. Until decision 24 is taken, an observation on the series artifact. The practical hazard is that a task created against one occurrence now refers to a time that no longer exists |
| an event moved to another calendar | **unhandled** | the `external_id` pair is (calendar id, event id) and the calendar half just changed, so the record's pair stops resolving — the identity-moved case `github.md` names for a transferred issue. Until built, `dropped` with reason `identity_moved` |
| an attendee's response changed (accepted, declined, tentative) | handled | an observation on the artifact's attendee set. **It closes no step**: whether a meeting is on is a condition its step owner reads, and a declined invitation is information, not a cancellation |
| an attendee added or removed | handled | an observation. Where the attendee is a person the record holds a `contact` for, the observation references it; the adapter does not create a `contact` for every invitee it sees — see refusal 1 |
| an event's conferencing details created or changed | handled | an observation on the artifact. It is what a joining step reads and it is also the row where the most incidental data arrives (dial-in numbers, personal room links), so the minimization rule applies |
| an event's attachments changed | handled | an observation naming the attachment and its location; the adapter does not fetch the file unless a step declares the read |
| a reminder or notification override changed on an event | deliberately ignored | `dropped`, reason `presentation_only`: a reminder is the calendar's own alerting for a human reader and says nothing about the work. **The swarm's own timing comes from the record**, never from a calendar reminder — a reminder that fires is not a trigger |
| an event's colour, visibility, or transparency changed | deliberately ignored | `dropped`, reason `presentation_only` |
| an event on a calendar the routing config does not name | handled | `dropped`, reason `untracked_calendar` |
| an event created by the swarm's own credential | handled | an action confirmation on the batch's `external_api_write`- or `send_external_comms`-class action whose `dedup_key` matches; the event is `PRODUCES` from the batch. **An event created by the swarm's credential matching no action is an observation and a defect to surface** |

### Calendars, sharing, and settings

| Signal | Status | Outcome in the record |
|---|---|---|
| a calendar added to, or removed from, the operator's calendar list | handled | an observation, **and announced on the off-record path**: the set of calendars the adapter sees just changed, and a calendar silently absent is indistinguishable from a calendar with nothing on it |
| a calendar's access rules changed (a share added, changed, or revoked) | handled | an observation, **and announced**. Someone gaining write access to the operator's calendar can create events that the adapter will read as records; someone revoking the swarm's access ends every read this document depends on |
| a calendar's metadata changed: name, description, **timezone** | handled | an observation. The timezone is not cosmetic — every floating time on that calendar is interpreted against it, so a timezone change silently moves what the record believes about times it already read |
| the watch subscription expiring or being stopped | handled | an observation, **and announced**. A watch expires on a fixed horizon; an adapter whose watch lapsed looks exactly like a calendar with nothing happening |
| the sync token being invalidated by the system | handled | an observation recording that an interval was **never read**, and the coverage says so. The system invalidates a token when too much has changed or too much time has passed, and the honest reading is a gap, not an occasion to silently re-list everything as though it were new (principle 7) |
| a user setting changed (default reminders, working hours, week start) | deliberately ignored | `dropped`, reason `client_configuration` |
| a calendar deleted, or a primary calendar cleared | handled | an observation on every tracked artifact on it. The clearing operation deletes every event on a primary calendar at once, and the record's artifacts survive it — the observations record that the external records are gone |

### Everything else the calendar exposes

| Class | Status | Outcome in the record |
|---|---|---|
| every remaining surface — the colour definitions, the free/busy query's own results as a subscription, channel bookkeeping | deliberately ignored | `dropped`, reason `out_of_scope_class`: the class says nothing about an artifact the work model names. The drop is counted like every other, so a class becoming relevant appears as a rising count rather than as silence |

## Conditions that are not delivered at all

More of this system's important state is here than in either sibling document, for the reason the opening
gives: time passing is the calendar's central fact and it is delivered by nothing.

| Condition | How it reaches the record | Why it is not delivered |
|---|---|---|
| **an event has begun**, or has ended | it does not arrive; it is derived by reading the artifact's stored time against the clock | nothing is delivered when a moment passes. Every workflow that depends on a meeting having happened depends on this derivation |
| a meeting ended **and produced a recording** | a task for meeting processing with the event and the recording as its artifacts (`workflows.md#meeting-processing`), created when the recording is discovered — by the recording system, not by the calendar | the calendar knows nothing about recordings; the linkage between a recording and an event is made by matching times, and it is a read with coverage, not a delivered fact |
| whether a meeting **actually happened** | it does not, ever | attendance is not reported. An event that was not cancelled and a meeting that occurred are different facts, and the record must not conflate them |
| whether the operator is free at a time | an observation from a free/busy read, carrying the window queried and the calendars covered | it is a query, not a subscription; and its answer is only as good as the calendars the query named, which is what coverage records |
| which occurrences a recurring series actually has | an observation from an instances read over a stated window | occurrences are computed on demand; an unbounded series has no complete answer, only an answer over a window — so the coverage **must** state the window, or the observation asserts a completeness that cannot exist |
| whether an attendee outside the operator's organization is free | it does not | free/busy across organizations returns nothing usable, and inferring availability from silence is the fabrication the unknown rule forbids |
| whether an invitation the swarm sent was **seen** | it does not | the response status stays at its default until the person acts, and a default is not a decline. This value must stay `unknown`-shaped rather than being read as "no" |

**The clock is a derivation and not a source, and that is what keeps it honest.** "The meeting has ended" is
computed from the artifact's stored end time and the current time, which means it is only as correct as the
last read of that event: an event moved after the adapter last read it will be believed at its old time.
So a step that depends on a meeting having ended declares a read of the event with a stated freshness, and
the hydration phase resolves it (`adapters.md`) rather than the step trusting a time the record has held
since last week. This is the same shape as the code host's stale-check problem and it has the same answer:
the value is re-read, and where it cannot be, the condition is `unknown` and unknown holds.

## Outbound: the operations the workflows take on the calendar

Every row is an `action`, created when the effect becomes known and evaluated at the action gate at the
moment it would be taken (`gates_and_workflows.md#actions-are-entities-only-actions-are-taken`). The
adapter performs the operation on permit, performs nothing on a checkpoint, and **confirms by reading the
calendar back** — never by the operation's return code.

**The class depends on the attendees, and the adapter reads before it classifies.** This is the rule the
opening promised and it governs every row below. An event with no attendee but the operator is a private
record, and writing it is `external_api_write`. The same write on an event with attendees **sends mail to
those people**, which is `send_external_comms` — the same class as an outreach send, and gated as such.
Because the adapter cannot know which without reading the event's attendee set, the classification is made
from a read taken as part of the same step, and **where the attendee set cannot be read the class is the
higher one** (principle 5, fail closed on the field carrying the safety meaning). An adapter that defaulted
to `external_api_write` on an unreadable attendee set would mail an unknown number of people under a class
chosen for a private note.

| Step or workflow | Operation | Action class | What the action gate does with the class | What confirms it landed | `dedup_key` keyed on |
|---|---|---|---|---|---|
| `record`, `deliver` | create an event with no attendees | `external_api_write` | as the policy lists it | the event read back **by its (calendar id, event id) pair**, at the intended time | the batch, the step, and the intended (calendar, title, start) |
| `record`, `deliver` | create an event **with attendees** | `send_external_comms` | ordinarily a checkpoint; the invitation is a message to people | the event read back by its pair, **and its attendee set read back as sent** | as above, plus the recipient set |
| `record`, `deliver` | move or update an event with no attendees | `external_api_write` | as the policy lists it | the event read back at its new time | the batch, the step, the event pair, and the intended new value |
| `record`, `deliver` | move, update, or cancel an event **with attendees** | `send_external_comms` | ordinarily a checkpoint; every one of these mails the attendees | the event read back at its new state | as above |
| any step | respond to an invitation (accept, decline, tentative) | `send_external_comms` | ordinarily a checkpoint — a response is a message to the organizer, and it is a commitment made in the operator's name | the attendee entry for the operator read back at the intended response | the event pair and the intended response |
| any step | query free/busy | **not an action.** A read | — | — | — |
| recovery of a created event | delete the event, or cancel it | `external_api_write`, or `send_external_comms` where it has attendees | evaluated on its own; there is no privileged undo path around the gate | the event read back as cancelled or absent | the event pair |
| recovery of a **sent invitation** | **there is none that unsends.** See below | — | — | — | — |

**The recovery rows divide in two, and the division is the point.** Deleting an event the swarm created recovers the
*record* cleanly: the event is gone, read back as gone. It does not recover the *message* — the attendees
were mailed when it was created, and cancelling mails them again. So for an event with attendees the
recovery is forward-only in the same sense `gmail.md` describes: the effect that reached people cannot be
withdrawn, only followed. The adapter performs the cancellation and does not pretend it is an undo, which
is the simulated-reversal `adapters.md` forbids. The consequence is the same as the mail system's: the gate
is the only real control, which is why the attendee-bearing rows are checkpoints.

**A calendar entry the swarm writes for its own bookkeeping is a smell, not a design.** Stated here because
it is the tempting use of a calendar: putting a due date on it so something visible reminds someone. A
task's due date, priority, and claimability live in the record (`work_model.md`), and a calendar entry
mirroring them is a second copy needing a process to keep it true (principle 11) that an external actor can
edit. The design writes events that are **genuinely meetings or genuinely obligations with a time**, and
derives everything else. Where the operator wants a visible entry, that is an operator preference met by
writing one deliberately, not by the adapter mirroring task state.

## How the five rules apply to this system

**Identity.** The actor of an inbound signal is an **email address** — the organizer's, or an attendee's —
and the adapter resolves it through the credential binding (`authority_model.md#principals`). Everything
`gmail.md` says about addresses applies here unchanged and is cited rather than restated: an address is not
an account, a mailbox has many addresses, and an unrecognized address is never resolved to the operator.

Two points specific to this system. The calendar reports, per event, whether a given attendee entry **is
the authenticated user**, which is a stronger signal than address matching and is what the adapter uses to
decide whether an event is the operator's own; where it is absent the resolution falls back to the binding
and carries lower coverage. And an event's **organizer** is the principal whose calendar governs it: a
change made by an attendee to their own copy does not change the organizer's event, so an observation from
a non-organizer copy is an observation about that copy and says so.

**Linkage.** An inbound signal names a calendar id and an event id; the adapter finds the artifact by
`system` and that pair. Two system-specific wrinkles. A recurring occurrence names the series through a
parent id and its own original start instant, and how that resolves is **open decision 24**. And a meeting
recording is linked to an event by **matching times**, not by any id the two systems share — which makes it
a heuristic link whose observation carries the match's basis and its uncertainty, never a bare edge. The
adapter never attaches an event to a batch on its own guess: intake's `link` step does that
(`workflows.md#intake`).

**Dedup.** Inbound: writes are keyed on the event pair and the change token or updated-time the read
returned, so a redelivered notification causing the same interval to be re-read lands once. Outbound: every
action carries its `dedup_key` as tabled, and the adapter refuses to take an action whose key it has
already confirmed. The calendar offers one facility the other systems do not — a **client-supplied event
id** on creation — and the design's position is that this is the right place to put the dedup key's
identity: an event created with a deterministic id derived from the action's `dedup_key` makes a duplicate
creation fail at the system rather than succeed twice, which is the strongest form of the rule available at
any system in this design. Where an action's key is present but unconfirmed, the adapter reconciles by
reading that id before creating again.

**Unknown, and every delivery's disposition.** The values that must stay `unknown` rather than defaulting:
an attendee set the adapter could not read (which decides the action class, above); an attendee's response
that has not been given, which is not a decline; a free/busy answer over calendars the query could not
cover; an occurrence set over an unbounded series, which has no complete value; and an event whose
timezone the adapter could not resolve, whose time is therefore not a known instant. Each has an obvious
wrong default and each wrong default is indistinguishable downstream from the true version.

Every signal resolves to one of the four outcomes or to `dropped` with a reason, counted per window and
surfaced on the off-record announcement path. The reasons this document introduces: `untracked_calendar`,
`presentation_only`, `client_configuration`, `identity_moved`, and `out_of_scope_class`.

**Provenance and read-back.** Every write names the adapter, the calendar system, the calendar id, and the
change token or updated-time it derives from. Coverage does heavy work here for a reason peculiar to this
system: nearly every calendar read is **bounded by a time window** the adapter chose, so an observation
that does not state its window asserts nothing checkable. "The operator has no meetings" is meaningless
without "between these two instants, on these calendars", and the difference between those two records is
the whole of whether a later reader can trust it.

## What this adapter refuses

**1. It does not build a profile of anyone from their presence on a meeting.** An attendee list is a list
of people who have not consented to being recorded by a swarm, and the minimization rule `gmail.md` states
applies here in its sharpest form (`CLAUDE.md`, people-data processing; RGPD Art. 6(1)(f) and Art. 9). The
adapter records an attendee as a reference where the record already holds a `contact` for them, and as an
address otherwise; it does not create a `contact` for every invitee it observes, does not accumulate
meeting-frequency statistics about people, and does not persist what an event's description discloses about
anyone's health, finances, or family. Purpose-binding is the test: enrichment serves the operator's actual
relationships, and a person who appears once on someone else's invitation is not one.

**2. It never creates, moves, or cancels an event with attendees outside a checkpoint.** Each mails people.
The class rule above is the mechanism; this is the statement that it is not to be evaded by
classifying from anything other than a read.

**3. It never responds to an invitation on the operator's behalf without the gate.** A response is a
commitment made in the operator's name to a person who will act on it. It is `send_external_comms` and it
is a checkpoint.

**4. It never deletes a calendar, and never clears a primary calendar.** The clearing operation destroys
every event on a calendar in one call, with no per-event confirmation to read back and no recovery of any
kind. Nothing in this design has a use for it, and its presence in the API is the strongest argument for
stating refusals explicitly rather than trusting that nobody would.

**5. It never adds, changes, or revokes an access rule, and never transfers ownership of a calendar.** Each
changes who can read or write the operator's calendar, which is `authority_model.md`'s to decide and never
an adapter's to infer. The adapter observes such changes and announces them.

**6. It never treats a calendar entry as step state, and never writes one to communicate one.** The mirror
of `gmail.md`'s label refusal: a swarm that writes a "blocked" event onto a calendar has built a second
place step state lives, editable by anyone with write access and backed by no sign-off.

**7. It never reads a calendar the routing config does not name.** The operator's calendar list may include
calendars belonging to other people and organizations; being able to read one is not authority to.

**8. It never treats a calendar reminder as a trigger.** The swarm's timing comes from the record. A
reminder is the calendar's alerting for a human, and a design that woke on one would have moved its
scheduling into an external system it does not control.

## What the design uses, and what the API offers that it does not

The condensation pass needs this distinction, so it is stated as a table. The right column is inventory:
capabilities that exist and that this design has no step reaching for.

| API capability | Design's use |
|---|---|
| list events over a window; read one event | **used**, and it is the primary inbound path |
| the events watch, with a sync token | **used** — as a wake-up, with the changed-entry read as the signal |
| create an event | **used**, at `record` and `deliver`, with the attendee-dependent class |
| update or move an event in time; cancel one | **used**, same classing |
| read an event's attendees and their responses | **used**, and it is what decides the action class |
| **respond** to an invitation | **used**, gated; refusal 3 |
| read occurrences of a recurring series over a window | **used**, and its coverage must state the window. What the occurrences *are* in the record is open decision 24 |
| free/busy query | **used** as a read; never an action |
| read calendar metadata and the calendar list | **used, read-only**, because a timezone or a list change alters what every other read means |
| read access rules | **used, read-only**; refusal 5 |
| the client-supplied event id on creation | **used**, as the outbound dedup key's identity — the strongest dedup available at any system here |
| **quick-add** an event from a text string | **not used.** It asks the vendor to parse an intention into a time; the design composes the event explicitly, because an action's effect must be knowable before the gate evaluates it |
| **import** an event (a private copy of an existing one) | **not used.** No case for it; it also manufactures a record with an interoperable id the swarm did not originate |
| **move** an event between calendars | **not used**, and the inbound side of it is unhandled — it changes the identity pair. See the events table |
| create, update, or delete a **calendar** | **not used.** The design writes events onto calendars the operator maintains |
| **clear** a primary calendar | **refused.** Refusal 4 |
| **transfer ownership** of a calendar | **refused.** Refusal 5 |
| insert, update, or delete an **access rule** | **refused.** Refusal 5 |
| add, update, or remove a calendar from the calendar list | **not used.** Which calendars the operator keeps is theirs |
| read or watch **user settings** | **not used.** `client_configuration` |
| the **colour** definitions, and per-event colour | **not used.** Inventory only |
| per-event **reminder overrides** | **not used**, and refused as a trigger. Refusal 8 |
| channel bookkeeping (stopping a watch) | **used** only as the mechanics of the watch itself |

Of the surface the CLI exposes, the design reaches for event reads and writes, occurrence reads,
free/busy, attendee reads, invitation responses, and metadata reads, and leaves the rest either refused
(four operation families) or unused (nine). The proportion is close to `gmail.md`'s and for the same
reason: both systems are built for a human managing their own life, and most of what they offer is
personalization the swarm has no business touching.

## Freshness

**Written against** the Google Calendar REST API v3, as exposed by the `gws` CLI's `calendar` command tree,
enumerated **2026-09-05** by reading the CLI's own help for every resource: `acl` (seven methods),
`calendarList` (seven), `calendars` (seven, including `clear` and `transferOwnership`), `channels` (one),
`colors` (one), `events` (eleven, including `instances`, `move`, `quickAdd`, and `import`), `freebusy`
(one), and `settings` (three). The change-notification behaviour, the sync-token invalidation, and the
mail-on-write behaviour for events with attendees are the API's documented behaviour, not measured here.

**What would make this stale, in order of likelihood.** A change to which write operations notify attendees,
or to the parameter governing it — this is the premise the entire action-class rule rests on, and it is the
one row that should be re-verified rather than assumed. A new method on `events`, which is where the design
concentrates. A change to sync-token invalidation, which changes how large a gap an invalidated token
implies. A change to recurring-event representation, which would bear directly on open decision 24. And a
change to whether a client-supplied event id is honoured, which is the outbound dedup rule's mechanism.

**A stale table reports without binding.** The dispositions are only as good as the enumeration behind
them, so the enumeration is dated and its instrument named (principle 8). Re-run it before relying on the
completeness claim; the per-row rulings do not depend on it, but the claim that the rows are exhaustive
does.

## Drift: what the built path does that this design does not say

Recorded as drift, not as design justification. An existing implementation is never a reason the design
must accommodate it. Read 2026-09-05 on this branch by enumerating every `gws calendar` call site under
`execution/` and `lib/` and reading each module. Counts are actual.

| What the design says | What the branch does | Where |
|---|---|---|
| the calendar is reached for reads and for gated writes | **six read sites and one write site, out of the whole surface.** The reads are all event lists; the single write is one event creation. Nothing else exists — no event read by id, no update, no move, no cancel, no delete, no occurrence read, no free/busy, no calendar-list or access-rule read | `sylvia.py`, `monedula.py`, `cotinga.py`, two `monedula/handlers/` modules |
| an event write is confirmed by reading the event back by its identity pair | **the one write site returns success on the subprocess exit code and captures no event id.** The same "a response code is not evidence" shape as the mail system's send path (principle 2) | `execution/daemons/sylvia/sylvia.py` |
| whether a series is one artifact or many is **open decision 24** | **every read passes the flag that expands a series into occurrences server-side**, at three sites, so no code path ever sees a recurrence rule. Zero occurrences repo-wide of the recurring-event id, of the iCal rule, or of the occurrence-listing method. The built path has taken the per-occurrence answer without stating it | `sylvia.py`, `monedula.py`, `cotinga.py` |
| recurrence, where the record drives it, is the record's | one daemon carries a recurrence field on its **task entity** and states in its own header that the record is authoritative for recurrence and the calendar is an output surface only. That is consistent with this document; it is noted because the term collides with the calendar's own recurrence and the two are different things | `sylvia.py` |
| an artifact is found by its identity pair | **event matching is by title and date proximity** — same title, case-insensitively, within a day — rather than by any id. A heuristic link where the design specifies an identity | `sylvia.py` |
| an attendee's response is read, and responses the swarm sends are gated | **attendees are read at one site and no response is ever written.** Zero occurrences of the response-status field and of the parameter that controls attendee notification — which means the built path has never had to face the action-class question the design's central outbound rule is about | `cotinga.py` |
| an adapter keeps no history of its own | a once-per-day marker file gates the calendar leg of one daemon | `execution/daemons/monedula/.monedula_last_run` |

The design's refusals are, as in `gmail.md`, **largely true by absence** here — no calendar deletion, no
access-rule write, no ownership transfer, no quick-add, no import — because the built surface is seven call
sites. The substantive drift is the unstated per-occurrence answer to decision 24, the identity-by-title
matching, and the unconfirmed write.

## What this document does not decide

The general adapter rules are `adapters.md`'s and are cited here, not restated. The step lists that take
these operations are `workflows.md`'s. The gate's decision function is `gates_and_workflows.md`'s. Whether
a recurring series is one artifact or many is **open decision 24** above, and it should be decided with
`gmail.md`'s decision 23. The identity rules for email addresses are `gmail.md`'s, shared by both systems
and written once there. Open decisions 15 and 16 (`adapters.md`) apply to this adapter as to every other,
and nothing above depends on their resolution. Which rows have a built path is `status.md`'s — and every
row marked **unhandled** above has one there.

## Prior art

The Google Calendar API's own resource and method reference is the source of the enumeration, read
2026-09-05 through the `gws` CLI; the resource names and their methods are the vendor's, and the status and
disposition columns are this document's. The iCalendar specification's separation of a recurring
declaration from its occurrences, and its provision for an occurrence that overrides its series, is the
structure open decision 24 is about, and it is the vendor's inheritance rather than its invention. The
anti-corruption layer (Evans) is the shape of the whole: the calendar's model — an entry as a reminder, an
invitation as a task, a colour as a category — never becomes the domain's.

## Beyond the sources

The per-signal mapping, the handled / deliberately ignored / unhandled marking, the eight refusals, the
attendee-dependent action class and its fail-closed default, the treatment of a meeting's beginning and
ending as derivations rather than events, the use of the client-supplied event id as the outbound dedup
key's identity, and open decision 24 are this document's, applying `adapters.md`'s rules to the calendar's
full surface. The observation that an event with attendees is simultaneously a record and a message is the
vendor's behaviour; making it decide the action class is this document's.
