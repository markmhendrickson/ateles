# Telegram: the operator's chat channel, and why a message in it is not an instruction

**Keyed document:** read when the chat adapter, the notification path, the operator-facing agent's
presentation of a checkpoint, or this document changes (`conformance.md`). **Kind:** foundation; states the
design and never the state of a checkout. **Derived from:** `adapters.md` (the two invariants, the four
outcomes, and the five adapter rules, which this document applies and does not restate),
`work_model.md` (artifacts, intake, the four execution mechanisms), `gates_and_workflows.md` (the action
gate; the checkpoint, its subject, and its one resolution protocol), `authority_model.md` (credentials bind
to principals; approval is authorized against the required approvers), `workflows.md` (the operator-only
workflow, and the consent step of every workflow that has one), `failure_posture.md` (the halt; the
off-record announcement path; retry classification), and Telegram's own chat-platform API documentation, read
2026-09-05, and PR #745 operator review (2026-09-05, rulings 13–14, 16–18, 23–29: decisions 25 and 26 ruled
here). What is built is `status.md`. Revised by the testability pass of 2026-09-06 (revision 37: every uncorrelated message from a bound principal is a task; the start-time binding is a cache with a declared staleness bound).

## Purpose

Be the chat adapter in full: every kind of update the chat API can deliver, mapped to one of the four
outcomes `adapters.md` defines or to `dropped` with a reason; every outbound operation a step takes on the
channel, with its action class and what confirms it landed. And, because this is the boundary where the
rule is under the most pressure, state precisely how an operator's chat reply becomes an approval on a
checkpoint — and how it never becomes anything else.

`adapters.md` keeps the general adapter rules and carries a pointer here; those rules are cited from this
document and never restated in it (principle 9, one home).

## Scope

Every update the chat API can deliver, the outbound methods a step's action takes, and the identity path
from a chat credential to a principal. In scope: the inbound mapping per update kind, the outbound
operation per step, the callback-payload trust distinction, and what the adapter refuses. Out of scope: the
general adapter rules (`adapters.md`), the workflows whose steps take these operations (`workflows.md`),
the gate's decision function (`gates_and_workflows.md`), what the adapter is granted
(`authority_model.md#grants`), and the per-instance binding of a chat to an operator, which is a
`channel_config` context entity resolved at runtime and never named here.

## Why this system gets its own document

Two systems in this design carry a hazard the others do not, and they are the two this pair of documents
covers. The payment rail's is that its effects are irreversible. This one's is subtler and, for that
reason, more likely to be eroded: **a human typing into a chat feels like commanding the swarm, and the
design's whole position is that it is not.**

Every other adapter is protected from that confusion by the shape of its own system. Nobody mistakes a CI
result for an instruction; a calendar event does not read as a command. But a chat is a channel built for
telling someone to do something, and it is the one channel where the operator — the principal with the most
authority in the system — is the party at the other end. So the confusion is not merely available here, it
is what the medium is *for*, and an adapter written without the rule stated in full will drift back toward
treating messages as commands, one convenient special case at a time.

`adapters.md` states the rule in general: an inbound event is a signal about an artifact, never an
instruction to a workflow. This document is where that rule is written out at the length the hazard
warrants.

## A chat message is not an instruction

State the rule first, then what it costs, then the one narrow path by which a message legitimately changes
what the swarm does.

**The rule.** A message arriving in the operator's chat is a delivery like any other. It resolves to one of
the four outcomes or to `dropped` with a reason, and the outcomes are the same four everywhere:
a sign-off by a named principal, an observation on an artifact, an action confirmation, or a task for
intake. There is no fifth outcome for "the operator asked for something", because a chat message asking for
something **is** the fourth outcome: it becomes a task, and the task enters intake like every other task.

**What follows, stated as negatives, because each is a thing an adapter would plausibly do.** A message
never opens a step. It never claims one. It never closes one. It never names a successor workflow, never
sets a task's status, never advances a batch, never marks a task done, never cancels a batch, never
reprioritizes a queue, never halts the swarm, and never takes an action. A message saying "ship it" opens
no merge. A message saying "cancel that" closes no batch. A message saying "do X" is a task for intake, and
what happens to it next is intake's classification, not the message's phrasing.

**Why the strong form rather than a pragmatic one.** The permissive alternative is obvious and it is what
gets built by default: parse the message, recognize an intent, do the thing. Three properties of this
design forbid it, and each fails in a way worth naming.

- **The claim predicate has no fallthrough** (`work_model.md#pull-is-the-only-delivery-assignment-constrains-eligibility`).
  A message that handed work to a principal would be a router, and a router is the thing the work model does not have:
  a 1:N choice with a fallthrough, made by an actor that neither acts nor answers for the misroute. An
  operator's chat message is exactly where a fallthrough would be most tempting and least visible.
- **Step state is derived from edges** (`gates_and_workflows.md#declaration-batch-projection`). A message
  that closed a step would be step state living in a chat log — a second source of truth (principle 9), in
  a system the swarm does not own, that cannot be read back in a halt and does not carry `unknown`.
- **An intent parse is an inference, and inference is where authority leaks.** Reading a message and
  deciding what it means is a judgement. Made by an adapter, it is a judgement by a component whose entire
  job is translation and which answers for nothing — and it is made on free text a third party can put in
  front of it. What "ship it" refers to, whether it is an approval, and which of three open checkpoints it
  answers are all questions an adapter would have to guess at, and a wrong guess takes an effect nobody
  authorized.

**The one narrow path, and its two conditions.** A message *can* change what the swarm does, by exactly one
route: it resolves a checkpoint. That path is the first of the four outcomes, in the form
`authority_model.md#approval` gives it, and it is gated on two conditions that the adapter checks and does
not infer.

1. **The credential resolves to a principal.** The chat id is a credential, never a principal
   (`authority_model.md#principals`). The adapter resolves it through the credential binding that already
   exists. It never invents a binding, and it never resolves an unrecognized chat id to the operator — the
   fallthrough forbidden everywhere is forbidden most consequentially here, because the operator is the
   principal whose approval carries the most weight.
2. **That principal is a required approver on the checkpoint in question, and the checkpoint is open.**
   Resolution is authorized against the required approvers, not accepted from whoever writes it
   (`authority_model.md#approval`). A principal who is not a required approver on that checkpoint cannot
   resolve it, however senior, and a checkpoint already terminal cannot be resolved again.

**A reply that fails either condition is an observation.** Not an error, not a refusal, not a retry — an
observation on an artifact, which is the second outcome and the honest one: something was said in a chat,
the record notes it, and no step moved. This is where the pressure is greatest, so it is worth being
explicit about the two cases. A message from a chat id bound to no principal is an observation (or a drop,
where it names no tracked artifact) — never a task and never a resolution. A message from a principal who
is real but is not a required approver on this checkpoint is an observation: the record holds that this
principal said this, and the checkpoint stays open awaiting whoever it actually awaits.

**And silence never accepts.** A checkpoint nobody answers does not resolve; it reaches its terminal state
by the timeout `gates_and_workflows.md#the-checkpoint` gives it, which is a terminal state that never
continues. No message is required for that, and no absence of a message is read as one.

### Which checkpoint a reply answers is decided by correlation, not by reading the text

The rule above says a reply may resolve a checkpoint. It does not say *which*, and this is the seam where
an intent parse would sneak back in. A chat is a linear stream; the swarm may have several checkpoints open
at once; and "yes" carries no reference to anything.

**The correlation is structural, and the adapter reads it rather than inferring it.** Two mechanisms carry
it, in order of preference:

- **A reply-to relation.** The presentation of a checkpoint was itself a message, whose id the adapter
  confirmed and recorded on the action that sent it. A reply carrying that message id as its parent is
  correlated to that checkpoint by the record, with nothing parsed.
- **A callback payload the swarm authored.** The presentation carried an inline keyboard whose buttons the
  swarm composed; the payload that comes back is the swarm's own. See below, where this is a different
  trust posture and not merely a second mechanism.

**A reply that correlates to no open checkpoint is a task.** Text arriving on no correlation, from a bound
principal, is the fourth outcome — a task entering intake — whether or not it reads as an ask. Deciding that
a message asks nothing is the intent read this document refuses, made by the component that answers for
nothing, and the design's answer for every other surface is the same: a stranger's issue and a stranger's
mail are tasks (`adapters.md#what-the-adapter-does-with-every-event`). Where the message asked nothing,
intake says so — its closing sign-off names no successor and the task ends there, recorded by a step owner
(`workflows.md#intake`). It is never a resolution.

**A reply that correlates to a checkpoint but carries no readable decision is `unknown`, and unknown
holds.** A checkpoint records the options it offers (`gates_and_workflows.md#the-checkpoint`). A reply
correlated to it whose content matches none of them has not decided it: the adapter writes an observation
saying so, the checkpoint stays open, and no option is selected as the nearest match. Coercing an
ambiguous reply to the nearest option is principle 7's failure at the boundary where it costs the most —
it manufactures a decision the operator did not make, on an action the gate held precisely because the
decision mattered.

**Ambiguity across several open checkpoints is not resolved by recency.** Where a reply carries no
correlation and more than one checkpoint awaits this principal, the adapter does not pick the most recent.
It writes the observation, and the checkpoints stay open. Recency is an inference, and an inference that
is right most of the time is worse than one that is never made, because the times it is wrong are
indistinguishable from the times it is right.

## The callback payload is the swarm's own text, and free text is not

An inline keyboard is a set of buttons the swarm composed and attached to a message it sent. Pressing one
delivers a callback carrying a payload **the swarm itself authored**. That is a materially different trust
posture from a message a person typed, and the difference is worth stating precisely, because it is easy to
overstate in either direction.

**What the distinction is.** Free text is authored by whoever is at the keyboard, and its meaning must be
interpreted. A callback payload is authored by the swarm, stored by the channel, and handed back
unchanged; its meaning was fixed at composition time. So the adapter reading a callback is not interpreting
a message — it is recognizing a token it minted. There is no intent parse, no natural-language
understanding, no nearest-match, and therefore none of the failure modes the section above is written
against.

**What it licenses.** Exactly one thing: the callback payload may carry the correlation and the selected
option, so that a button press is unambiguously "this principal chose option B on checkpoint C". That is
the mechanism the rule above named as the second correlation path, and it is the better of the two —
correlation and decision arrive together, in the swarm's own vocabulary, with nothing parsed.

**What it does not license, which is the more important half.** The payload's provenance says something
about the *content* of the callback and nothing whatever about *who pressed the button*. The two conditions
of the narrow path above apply to a callback exactly as they apply to free text:

- The callback carries the chat credential of whoever pressed it, and **that credential is resolved through
  the binding like any other**. A button the swarm composed, pressed by a principal who is not a required
  approver on that checkpoint, is an observation. The swarm authoring the payload does not authorize the
  presser.
- A message with an inline keyboard is visible to everyone in the chat it was sent to. In a group, that is
  every member. So "the swarm authored this payload" and "the required approver pressed this button" are
  independent facts, and only the second is an authorization. Conflating them is the specific failure this
  paragraph exists to prevent, and it is a plausible one: the payload *feels* trusted, and the feeling
  attaches to the wrong half of the event.
- A payload the adapter does not recognize is not trusted for being payload-shaped. It resolves to
  `dropped` with a reason, like any unmappable delivery.

**A stale payload is a payload for a checkpoint that is no longer open.** Buttons persist on a message
after the checkpoint they belong to has resolved, timed out, or been superseded, and a press arriving then
is not a late decision — the checkpoint is terminal, and a terminal checkpoint is not resolved again. The
adapter writes an observation and, where the channel permits, tells the presser so on the channel itself,
because a refusal the operator cannot see is indistinguishable from a delivery that never arrived
(`adapters.md#what-the-adapter-does-with-every-event`).

**So the distinction is real and narrow.** It removes the interpretation problem and it removes nothing
else. Both halves matter: an adapter that treated free text as equivalent to a callback would be parsing
intent, and an adapter that treated a callback as self-authorizing would be accepting an approval from
whoever was in the room.

**The payload is small, so it carries a token and not a description.** The channel caps a callback payload
at a few dozen bytes, which is far too little to carry a checkpoint's identity, the option chosen, and
anything else in readable form. The payload therefore holds an opaque token the adapter minted and recorded
on the action that sent the presentation, and the correlation is a **read of the record** for that token,
not a parse of the payload. Two properties follow, and the second is the one that matters.

The token is meaningless outside the record, so a payload that reaches the adapter and resolves to no
recorded token is `dropped` with a reason — which is the *unrecognized payload* row above, now with its
mechanism stated. And the token is **not a capability**: resolving it tells the adapter which checkpoint
and which option, and nothing about who may decide. Both conditions of the narrow path are still checked
against the pressing credential. A design that let an unguessable token stand in for authorization would
have built a bearer secret out of a correlation handle, and put it on a button visible to everyone in the
chat.

**Acknowledging a callback is not answering it.** The channel expects a callback to be acknowledged, and
shows the presser a pending indicator until it is. That acknowledgement is a display concern: it tells the
channel to stop spinning and carries no meaning about the decision. It is listed as its own outbound row
below precisely so it is not mistaken for the resolution — the resolution is written to the record and read
back, and the acknowledgement happens whether the press resolved anything or was an observation.

## What the channel holds, and what an artifact is here

The channel holds artifacts of kind `message`, identified by the `system`/`external_id` pair every artifact
carries (`adapters.md#what-the-adapter-does-with-every-event`). The external id is the chat's identifier
together with the message's identifier within it, since a message id is unique only within its chat.

Three things that are **not** artifacts here, named because each is a plausible mistake:

- **A chat is not an artifact.** It is where artifacts live, and which chat is bound to which principal is
  the `channel_config` entity's, resolved at runtime. A chat has no batch and no workflow.
- **A checkpoint is not an artifact.** It is an entity in the record, and the message presenting it is the
  artifact. This distinction carries weight: the checkpoint is resolved on the record, and the message is
  the thing that happened in a system the swarm does not own.
- **A conversation is not an artifact.** The channel has no thread the record mirrors; correlation is the
  record's, through the reply relation and the callback payload, and never a chat-shaped structure the
  adapter maintains beside the record (`adapters.md#what-the-record-supplies-and-what-an-adapter-therefore-never-builds`).

## Inbound: every update the chat API can deliver

The chat API delivers an `Update`, of which exactly one content field is set. This section enumerates from
the API's own list rather than from what a receiver happens to subscribe to, and marks each row **handled**,
**deliberately ignored**, or **unhandled** — the third being a gap, which is a `status.md` row and not a
silent omission here. The disposition rule (`adapters.md#what-the-adapter-does-with-every-event`) is what
makes an omission observable: an update matching no row resolves to `dropped` with a reason, counted per
window and surfaced on the off-record announcement path.

### Messages

| Update and case | Status | Outcome in the record |
|---|---|---|
| `message`, text, from a chat credential bound to a principal, correlated to an open checkpoint that principal is a required approver on, carrying a readable option | handled | **resolution of that checkpoint by that principal**, authorized against the required approvers and read back on the checkpoint (`authority_model.md#approval`) |
| `message`, text, correlated to an open checkpoint, from a principal who is **not** a required approver on it | handled | an observation on the artifact. The checkpoint stays open, awaiting whoever it awaits |
| `message`, text, correlated to an open checkpoint, content matching none of its options | handled | an observation recording what was said; the checkpoint stays open and **no option is selected as a nearest match** |
| `message`, text, from a bound principal, correlated to nothing | handled | **a task**, with the message as its artifact, entering intake — always, whatever the text asks or does not ask: whether it asks anything is intake's judgement, and intake's closing sign-off names no successor where it asks nothing (`workflows.md#intake`); the adapter reads no intent, and classification is never the message's phrasing |
| `message`, text, from a bound principal, correlated by reply to the presentation of an operator-only task's `present` step | handled | an observation on the task's artifact that the `record` step owner reads and signs on (`workflows.md#operator-only`); the sign-off is the step owner's, not the message's, and an uncorrelated report is the task row above |
| `message`, from a chat credential bound to **no** principal | handled | an observation where it names a tracked artifact; otherwise `dropped`, reason `unbound_credential`. **Never a task, never a resolution** |
| `message` carrying a platform command | handled | see *Commands*, below |
| `message` carrying media (photo, document, audio, video, voice, video note, animation, sticker) | handled | an observation on the artifact, recording that media of that kind arrived, its identifier at the channel, and its stated type and size. See *What the adapter does not fetch* |
| `message` carrying a contact, location, or venue | handled, **narrowed** | an observation recording that a datum of that kind arrived and never its content. See *What the adapter does not write* |
| `message` carrying a poll, a dice, a game, a checklist, or a gift | deliberately ignored | `dropped`, reason `presentation_only` |
| `message` that is a service message (a member joined or left, the title or photo changed, a message was pinned, a forum topic opened or closed, a video chat started) | handled | an observation on the artifact for the chat's binding, where one is tracked; otherwise `dropped`, reason `untracked_chat`. **A membership change never alters a credential binding**, which is `authority_model.md`'s to write and never an adapter's to infer |
| `message` that is the chat-migrated service message | handled | an observation, **and a correction the adapter must not make on its own**: the chat's identifier has changed, and every artifact keyed on the old one is affected at once. See *Identity that moves under the record* |
| `message` carrying an invoice, a successful payment, a refunded payment, or paid media | **unhandled** | the channel's own payment surface is a second payment rail beside the ones `payments.md` covers, reached through a system whose posture this design has not taken. Until decided, `dropped`, reason `unmapped_payment_surface`. See `status.md` |
| `edited_message` | handled | see *Edits*, below |
| `channel_post`, `edited_channel_post` | deliberately ignored | `dropped`, reason `out_of_scope_class`: a broadcast channel is not an operator's decision channel, and nothing in this design presents a checkpoint to one |
| `business_connection`, `business_message`, `edited_business_message`, `deleted_business_messages` | deliberately ignored | `dropped`, reason `out_of_scope_class`: the business surface represents a different account model than the operator-facing channel this design uses. It is the one surface that *would* report a deletion, which is noted under *Conditions that are not updates* and changes no rule |
| `guest_message` | deliberately ignored | `dropped`, reason `out_of_scope_class`: a guest surface admits a party the binding does not name, and answering one is a synchronous reply to an unbound credential |
| `managed_bot`, `subscription`, `stopped_message_generation` | deliberately ignored | `dropped`, reason `out_of_scope_class`: each concerns the channel's own account and product surfaces rather than an artifact the work model names |

### Edits, deletions, and reactions

| Update and case | Status | Outcome in the record |
|---|---|---|
| `edited_message`, on any message | handled | an observation on the artifact, recording that the message was edited and when. **An edit never revises a resolution already written**: a resolution is terminal, and a principal reaching a different decision needs a new checkpoint, not an edited message |
| `edited_message` the channel raised for a field nobody changed | handled | the channel documents that it may deliver an edit for changes to fields the swarm does not use — a link preview resolving, for instance. An edit is therefore **not evidence that a person edited anything**, and the adapter compares against what the record already holds before writing an observation that says a person did. Where the content is unchanged, the disposition is `dropped`, reason `no_change` |
| `edited_message`, on a message the adapter read as a checkpoint resolution | handled | an observation, and **a defect to surface**: the record holds a resolution attributed to this principal from text that no longer says what it said. The resolution stands, because it was read back when it was written, and the edit is recorded beside it so both are readable |
| `message_reaction` | handled | an observation on the artifact, and **never a resolution** (decision 25, ruled: `#a-reaction-never-carries-a-decision`). A reaction is the cheapest possible gesture in the channel and therefore the most tempting to read as an approval, which it is not. It arrives only where the swarm holds an administrator role in the chat and has named the kind explicitly, which is a coverage fact and not a design one |
| `message_reaction_count` | deliberately ignored | `dropped`, reason `presentation_only`: an anonymous aggregate attributes to no credential, so it cannot reach the identity rule at all, and the channel delivers it with a delay of minutes |
| a message deleted by a person | **unhandled** at the channel, and unhandleable | the chat API delivers no update when a user deletes a message in an ordinary chat, so the record cannot observe it at all. What follows is stated under *Conditions that are not updates* |

### Callbacks and queries

| Update and case | Status | Outcome in the record |
|---|---|---|
| `callback_query`, payload the adapter minted, correlated to an open checkpoint, from a credential bound to a required approver on it | handled | **resolution of that checkpoint by that principal**. The payload carries the correlation and the option; nothing is parsed |
| `callback_query`, payload the adapter minted, from a credential bound to a principal who is not a required approver | handled | an observation. **The swarm authoring the payload does not authorize the presser** |
| `callback_query`, payload the adapter minted, from a credential bound to no principal | handled | an observation, or `dropped` with reason `unbound_credential` where it names no tracked artifact |
| `callback_query`, payload correlated to a checkpoint already terminal | handled | an observation; the terminal checkpoint is not resolved again, and the reason goes back to the channel so the presser can see it |
| `callback_query`, payload the adapter does not recognize | handled | `dropped`, reason `unrecognized_payload`. A payload is not trusted for being payload-shaped |
| `inline_query`, `chosen_inline_result` | deliberately ignored | `dropped`, reason `out_of_scope_class`: inline mode is a composition surface for a user writing elsewhere, and it asks the swarm to author content on a keystroke, which no workflow does |
| `shipping_query`, `pre_checkout_query` | **unhandled** | the channel's payment surface again, and these two are worse than the message-borne case: each is a **synchronous** question the channel expects an answer to within a short window, which is a shape this design has nowhere to put — an action is gated at the moment it would be taken, and a gate cannot be evaluated inside a countdown. Until decided, `dropped`, reason `unmapped_payment_surface` |
| `purchased_paid_media` | **unhandled** | the same surface |

### Membership, polls, and the rest

| Update and case | Status | Outcome in the record |
|---|---|---|
| `my_chat_member` (the swarm's own status in a chat changed) | handled | an observation, **and announced on the off-record path**. This is the row that looks ignorable and is not: the swarm being removed from a chat, or restricted in it, silently ends the delivery of every update this document maps for that chat — which is indistinguishable from a chat where nothing is happening, `failure_posture.md` rule 2's failure at the boundary |
| `chat_member` (another member's status changed) | handled | an observation; it never alters a credential binding |
| `chat_join_request` | deliberately ignored | `dropped`, reason `out_of_scope_class`: admitting a member is a decision about who can see the channel, which is the operator's to take on the channel and not an action the swarm has a workflow for |
| `poll`, `poll_answer` | deliberately ignored | `dropped`, reason `presentation_only`. **A poll is not a decision queue**: the design has one, it is the checkpoint, and a second one built out of chat polls would be the second gate principle 6 forbids |
| `chat_boost`, `removed_chat_boost` | deliberately ignored | `dropped`, reason `out_of_scope_class` |
| Every update field not named above | deliberately ignored | `dropped`, reason `out_of_scope_class`. The drop is counted like every other, so a class becoming relevant appears as a rising count rather than as silence |

### Commands

A platform command is text with a leading marker that the channel formats specially. It is worth its own
section because the channel's own affordance — a menu of commands the user picks from — is the strongest
invitation in the whole system to treat a message as an instruction, and the design declines it.

**A command is a task for intake, and nothing else.** It is text, from a bound principal, correlated to
nothing: the fourth outcome. What the command names does not become what the swarm does; it becomes what
the task says, and intake classifies it. So a command naming a workflow does not enter that workflow — it
enters intake, which decides (`work_model.md#intake-is-every-tasks-first-workflow`). A command naming an
action does not take that action; if the task intake produces reaches a step that produces such an action,
the action passes the action gate on its own like every other.

**Two commands are exceptions in form and not in substance**, and both are reads:

| Command class | Status | Outcome in the record |
|---|---|---|
| a command asking what is awaiting this principal | handled | **no write to the record at all**: the adapter reads the principal's open checkpoints and answers on the channel. It is a read, so it is not an action, and it produces no task. What it may show is bounded by what that principal may see. **During a halt the answer is the halt itself and never data** (decision 26, ruled: `#during-a-halt-a-read-on-the-channel-is-answered-with-the-halt-and-never-with-data`) |
| a command asking the state of a batch or task the principal may see | handled | the same: a read, answered on the channel, writing nothing; during a halt, answered with the halt |
| every other command | handled | a task with the message as its artifact, entering intake |

**The halt is not a command, and this is the sharpest case.** An operator may halt the swarm on their own
word at any time (`failure_posture.md#the-operator-invoked-halt-and-what-undoes-an-action-already-taken`),
and a chat message is the most natural way to say so — which makes it exactly the case where the rule must
hold rather than bend. Two reasons it does not become a special path here. First, the halt is confirmed by
a read-back of the swarm's state and never by the command returning, so a chat message could at most
*begin* it, and the confirming read is what makes it a control. Second, a halt is the state in which
nothing can be written to the record, so an adapter that took a halt from a chat message would be acting on
the one instruction it cannot record having received. Where a halt is invocable from this channel at all,
it is invocable as the operator's own action through a path that confirms it, and the message that asked
for it is a task like any other.

## Chats, groups, and who can see what

The chat API distinguishes a direct chat from a group, a supergroup, and a broadcast channel, and the
difference matters for two separate reasons the design must not conflate.

**Visibility.** Everything the swarm sends to a group is visible to every member of it, and everything a
member sends is visible to the swarm. A checkpoint presented in a group discloses whatever the presentation
carries — the action, its subject, and its options — to everyone in the room. That is a disclosure
decision, and it belongs to the `channel_config` binding rather than to the adapter: the adapter sends
where it is told and never chooses a chat to reach a principal in.

**Authorization, which visibility does not imply.** Membership in a chat is not a credential binding, and
nothing about being in the room makes a member a principal. The two conditions of the narrow path apply in
a group exactly as in a direct chat, and the failure mode a group creates is precise: a checkpoint
presented in a group is answerable, in the channel's own affordances, by anyone in it, and only one of them
may be a required approver. So the adapter resolves the *sender's* credential on every delivery and never
the chat's, and a group whose chat id is bound to a principal does not make its members that principal.

**The swarm's own visibility in a group is partial, and partial is not empty.** By the channel's default
privacy setting the swarm receives only some of what is said in a group — broadly, commands addressed to it
and replies to its own messages — and it receives everything only where it holds an administrator role or
the setting has been changed. That is a **coverage** fact and it is recorded as one
(`adapters.md#what-the-adapter-does-with-every-event`): an observation from a group states the coverage of
the read that produced it, so "the swarm saw nothing" and "the swarm was not shown it" stay distinguishable.
An adapter that recorded a partial view as a complete one would produce exactly the undetectable gap the
coverage rule exists to prevent.

**And the partiality is load-bearing rather than incidental, in one direction.** What the swarm reliably
does see in a group is a reply to its own message — which is precisely the correlation the narrow path
depends on. So a checkpoint presented in a group is answerable by reply and by button under the default
setting, and the design needs no widened visibility to work. Widening it would mean the swarm receiving
everything said in the room, which is more of the operator's life in the record for no capability the
design uses: the narrowing on what the adapter writes exists for that reason, and a narrower read is better
than a narrowed write over a wide read.

| Chat kind | Status | Disposition |
|---|---|---|
| direct chat with a bound principal | handled | the ordinary case; every inbound row above applies |
| group or supergroup whose chat is named by the binding | handled | the same rows, with the sender's credential resolved per delivery and coverage recorded on every observation |
| group or supergroup not named by any binding | handled | `dropped`, reason `untracked_chat` |
| broadcast channel | deliberately ignored | `dropped`, reason `out_of_scope_class` |

## Delivery: webhooks, long polling, and what the dedup rule keys on

Where inbound delivery lands is ruled in `adapters.md` (decision 16,
`adapters.md#where-inbound-delivery-lands-the-adapter-verifies-and-identifies-it-and-the-records-own-subscriptions-are-not-it`):
the listener may be shared plumbing, and verifying a delivery and extracting its identifier are this adapter's.
What this section states is what the two mechanisms this channel offers give that rule — the secret this
adapter checks, the identifier it keys on, and the acknowledgement it owns — because each is specific to this
system.

**Both mechanisms deliver the same updates, each carrying the channel's own update identifier.** That
identifier is what `adapters.md`'s dedup rule keys on: every inbound event carries the external system's
delivery id as the idempotency key of the write it produces, so a redelivered update lands once. The
identifier is assigned by the channel and is per credential rather than per chat, which is what makes it usable —
a key the adapter derived from the payload's contents would collide on two identical messages, which are a
thing a person can genuinely send.

**The identifier is sequential but not permanently monotonic, and this is a design constraint rather than a
detail.** The channel documents that after a period of quiet the next identifier is chosen at random rather
than continuing the sequence. So dedup is a **membership test over a window of identifiers seen**, never a
comparison against a high-water mark. The distinction is not fussiness: a high-water-mark rule would
silently discard every delivery after a quiet period, and it would do so in exactly the failure shape the
disposition rule exists to eliminate — receipt with no disposition, indistinguishable from a channel with
nothing happening. A backward jump in the identifier is a normal event to be handled, not an anomaly to be
rejected.

**Long polling and webhooks differ in what happens when the receiver is unavailable**, which is a
failure-posture fact rather than a design choice:

| Mechanism | What it gives | What it costs |
|---|---|---|
| long polling | the receiver asks for updates and acknowledges by asking from a later offset; an unavailable receiver simply does not ask, and updates wait at the channel | the swarm holds a request open, and only one consumer per credential may hold it — a second consumer on the same credential is refused, so two components polling one credential is a conflict rather than a share |
| webhook | the channel delivers to an endpoint the swarm exposes, retrying on a non-success status, and a secret the swarm sets is returned in a header the receiver checks | the endpoint must be reachable from outside, which is a surface the swarm otherwise does not have; the secret is the **only** authenticity check the channel offers, there being no signature over the body |

**Undelivered updates are retained for a bounded period and then lost, and there is no backfill.** The
channel states the bound; what matters to the design is that it is finite and that no read exists to
recover what fell outside it. An outage longer than the retention is therefore a **coverage gap**, and it
is recorded as one: the observations written after such an outage state the coverage of the read that
produced them, so "nothing was said in the channel" and "the swarm was not given what was said" stay
distinguishable (`adapters.md#what-the-adapter-does-with-every-event`). This is not a hazard the adapter
can engineer away, so the design's response is to make it legible rather than to pretend it does not exist.

**Two rules hold whichever mechanism is chosen, and both are already the design's.** During a halt the
adapter writes nothing and acknowledges nothing, letting the channel redeliver
(`adapters.md#what-the-adapter-does-with-every-event`) — which under long polling means not advancing the
offset, and under webhooks means not returning success. And a failed read of the channel left no effect, so
it is retried with backoff, or deferred to the reset time the channel states where it states one
(`failure_posture.md`, rule 8). The channel does state one, on the response that reports a rate limit, and
that stated time governs over any interval the adapter would choose.

**One property of this channel makes the halt rule sharper than elsewhere.** Acknowledging an update is
irreversible and, under long polling, implicit in asking from a later offset. So an adapter that reads a
batch of updates, fails to write them, and asks for the next has discarded them — there is no redelivery to
fall back on. The rule is therefore not merely "do not acknowledge during a halt" but that the
acknowledgement **follows the confirmed write and never precedes it**, and never rides on the same request
that fetches the next batch.

**A note on which updates the channel will deliver at all.** Several kinds are withheld unless the swarm
names them explicitly when it configures delivery, and one of them is the reaction kind marked unhandled
above. That is a fact about the channel's configuration rather than about this design, but it has a design
consequence worth stating: what the swarm is configured to receive is part of an observation's **coverage**,
because a kind never requested and a kind that never occurred produce the same silence. A change to that
configuration narrows or widens what every row above can see, which is why the corresponding update on the
swarm's own status in a chat is announced off-record.

## Identity that moves under the record

An artifact is keyed on `system` and `external_id`, and this channel has two ways for that key to stop
resolving. Both are named here because a key that silently stops resolving is the failure `github.md`
records under the same heading, and the response is the same: the design names the gap rather than
improvising a re-identification rule.

**A group that is upgraded acquires a new chat identifier.** The channel reports it twice — as a service
message in the old chat, and as a field on the error returned when the swarm next addresses the old
identifier. Every artifact keyed on the old chat is affected at once, exactly as a repository rename affects
every artifact at a code host. **Unhandled**: whether an artifact's external identity may be corrected in
place, or whether a moved record is a new artifact with an edge to the old, is a decision the record's
conventions owe and this document does not take. Until it is taken, the adapter writes the observation and
resolves subsequent deliveries on the old identifier to `dropped` with reason `identity_moved` — which is
at least counted, where the failure would be that it was not. See `status.md`.

**A message identifier is not always usable, and is not always unique over time.** The channel returns a
placeholder identifier in two circumstances — a message the channel scheduled rather than sent, and an
ephemeral message — and it states that an ephemeral identifier may be reused once the message is gone. Two
rules follow, and both are narrow. A send whose read-back returns a placeholder identifier is **not
confirmed**: the action reads `unknown`, which holds whatever step declared it, rather than minting an
artifact around a handle that names nothing. And the swarm does not send its checkpoint presentations as
ephemeral messages, because an artifact whose external id may later belong to a different record is not an
identity at all — which is a constraint on what the outbound rows below may do, and is stated there.

## Conditions that are not updates

Some state a reader would expect the channel to report is not delivered at all. Each is named because the
absence is load-bearing, and because an adapter that quietly synthesized any of them would be manufacturing
a fact.

| Condition | What the channel offers | What the design does |
|---|---|---|
| whether the operator has **read** a message | **nothing.** The chat API exposes no read receipt, no delivery state, and no view count for a message the swarm sent. The one method whose name suggests it runs the other way — marking an incoming message read on a business account's behalf — tells the swarm nothing | **the design needs none, and would not use one if it existed.** Delivery is not decision and reading is not decision; the checkpoint is resolved on the record or it is open. A read receipt would be a tempting proxy for attention and a false one — the sharpest illustration is that a checkpoint the operator has read and not answered is in exactly the same state as one they have not seen, because in both cases no principal has decided. **This absence is why acknowledgement in this design is always explicit**: a press, a reply, a resolution on the record — something a principal did, never something the transport inferred |
| whether a person **deleted** a message | **nothing**, in every chat kind this design uses. The one surface that reports deletions is the business-account surface, which the binding does not name | the record keeps the artifact and the observations written about it. A message the record holds and the channel no longer shows is not a contradiction to repair: the record is the record of what was received, and it does not follow the channel's current display. **Nothing is retracted, because nothing was ever authorized by the message's continued existence** |
| whether a message was **delivered** to a person who has the chat muted or archived | nothing meaningful | the same as a read receipt: not a signal the design consumes |
| whether the swarm still has permission to post in a chat | only indirectly, through the update on the swarm's own status and through a failed send. In a direct chat that update fires only when the person blocks or unblocks the swarm, so it is a narrow signal rather than a general one | both are handled: the status update is an observation announced off-record, and a failed send leaves the action unconfirmed, which reads as `unknown` (principle 7) |
| whether a message the swarm sent can still be edited or deleted | only by attempting it. The channel bounds both by time, and a deletion past its bound simply fails | the bound is a **recovery constraint** and is stated as one under *Recovery*: past it, the only forward fix is a new message, which is the shape every recovery on this channel already takes |

**The deletion case deserves one more sentence, because it is where a reader most expects a problem.** If a
person deletes a message the adapter read as a checkpoint resolution, the resolution stands. That is not an
oversight: the resolution was written to the record and read back at the moment it was made, by a principal
authorized to make it, and a record whose entries could be retracted by deleting the thing that caused them
would be a record with a second, external source of truth over its own history. What the deletion costs is
that the *evidence* in the channel is gone, which is why the observation the adapter wrote — carrying the
content, the sender's credential, and the sourced time — is the durable half and the channel's copy is not.

## What the adapter does not write, and what it does not fetch

Two narrowings, each on the write rather than on the read, and each stated here because this channel is the
one an operator's whole life passes through.

**Incidental personal content is not persisted.** A chat carries whatever a person types, and a great deal
of it is not about any artifact the work model names. The adapter's writes are bounded by what the outcome
needs: a resolution needs the option and the resolver; an observation needs what the message said about the
artifact it concerns; a task needs the ask. A message that concerns no tracked artifact and is not a new
ask resolves to `dropped`, and a drop writes the reason and not the content. Concretely, a shared contact,
location, or venue is recorded as *that a datum of that kind arrived* and never as its content — the same
narrowing `github.md` applies to advisory detail, applied here for the same reason: material the record
never held cannot be copied out of it by a later step, a summary, a digest, or a rendered page.

**Media is observed, not ingested.** The channel offers a path by which the swarm can fetch the bytes of a file
a message carried. The adapter records that media of a stated kind, size, and channel identifier arrived,
and fetching the bytes is a **separate read a step declares** rather than something the adapter does on
receipt (`adapters.md#the-adapter-runs-before-and-after-a-step-never-during-it`). Two reasons, and the
second is the load-bearing one. Fetching on receipt would make the volume of what the swarm stores a
function of what anyone sends it. And a file arriving in a chat is untrusted content from outside the
system, so the moment it enters the record is a decision a step makes under its own declaration, not a
reflex of the boundary.

**Both narrowings state their coverage.** A narrowed observation is not a complete one, and recording it as
complete would be the failure `adapters.md`'s coverage rule exists to prevent. So the observation says that
fields were withheld by policy — distinguishing "the adapter did not read this" from "the adapter read it
and did not write it", which are different facts about the record.

## Outbound: the operations a step takes on the channel

Every row is an `action`, created when the effect becomes known and evaluated at the action gate at the
moment it would be taken (`gates_and_workflows.md#actions-are-entities-only-actions-are-taken`). The adapter
performs the operation on permit, performs nothing on a checkpoint, and **confirms by reading the channel
back** — never by the operation's return code.

| Step or purpose | Operation on the channel | Action class | What the action gate does with the class | What confirms it landed |
|---|---|---|---|---|
| `present` (operator-only), `consent` (payment, outreach), any step carrying a checkpoint | send the checkpoint to the chat the binding names, with its reason, its options, and what it needs | `notify_operator` | as the policy lists it; a policy keeps it low-blast so that carrying a decision to the operator is not itself gated behind a decision | the message read back by its id in that chat. **The checkpoint stays open until resolved on the record, whatever the delivery status** |
| `present`, `deliver` | send the same with an inline keyboard whose payloads the adapter mints and records against the checkpoint | `notify_operator` | the same | the message read back by id, and the payloads recorded on the action that sent it — which is what later correlates a callback |
| `deliver` | send a digest, a result, or a report | `notify_operator` | the same | the message read back by id |
| any step | send a document or an image a step produced | `send_external_comms` where the content leaves the operator's own chats; `notify_operator` where it does not | as the policy lists it | the message read back by id, carrying the channel's identifier for the file |
| the off-record announcement path (`failure_posture.md`, rule 2) | announce a halt, entering and leaving, and the window's aggregated drops and blocked claims | `notify_operator` | see below — this row is the exception | the send's own result only, because the record may be unreachable |
| any step | edit a message the swarm sent, to reflect that its checkpoint is now terminal | `external_api_write` | as the policy lists it | the message read back at its new content |
| any step | remove an inline keyboard from a message whose checkpoint is terminal | `external_api_write` | as the policy lists it | the message read back without the keyboard |
| a callback's receipt | acknowledge a callback so the channel stops showing it as pending | `external_api_write` | as the policy lists it | the acknowledgement's own result; it carries no meaning beyond the channel's display |

**The announcement path is the one row that cannot follow the ordinary rule, and it is an exception the
design already carries.** `failure_posture.md` rule 2 requires the halt to be announced on a path that
survives the outage, which means announced when the record is unreachable — and an action is an entity in
the record. So the announcement is not gated at the moment of a halt, because the gate cannot be evaluated
without the record, and it is not confirmed on the record either. It is sent to the chat the adapter's copy
of the binding names, within that copy's declared staleness bound
(`#during-a-halt-a-read-on-the-channel-is-answered-with-the-halt-and-never-with-data`). What holds it in bounds is that it is
**strictly one-way and carries no decision**: it tells the operator that the swarm has halted or resumed,
and aggregates the window's drops and blocked claims. It never asks for a decision, never presents a
checkpoint, and never carries an inline keyboard — because a decision made against an unreachable record
could not be recorded, and an approval the record cannot hold is not one the engine may act on
(`adapters.md#what-the-adapter-does-with-every-event`). When the record returns, the halt's entering and
leaving are written to it like any other observation.

**Being per window is a property of this path and not a courtesy.** The announcement is per window, never one
message per blocked claim, for the reason rule 2 gives: a channel that pages once per event during an
outage is a channel the operator mutes, and a muted announcement path is the silent failure the rule exists
to close.

## How the five rules apply here

`adapters.md` states the five; this section says what each means at this boundary, and nothing here restates
their reasoning.

**Identity.** The chat id is the credential, resolved through the binding
(`authority_model.md#principals`). Resolved to a required approver on an open checkpoint: the delivery may
be that approval. Resolved to a principal in neither role, or to no principal: an observation. **This is
the rule the whole document turns on**, and its negative form is the one to carry: the adapter never
invents a binding and never resolves an unrecognized chat id to the operator. A chat is an easy place to
appear to be someone — a forwarded message, a group where anyone may type, a display name a person chooses
— and none of those reaches the binding, which is `authority_model.md`'s to write.

**Linkage.** An update names a message; the adapter finds the artifact by `system` and `external_id`, where
the external id is the chat's identifier with the message's identifier within it. A message on no tracked
artifact that is a new ask from a bound principal yields a task for intake; any other update on it is
dropped with that reason. The adapter never attaches an artifact to a batch on its own guess.

**Dedup.** Inbound: the channel's update identifier is the idempotency key of the write the delivery
produces, so a redelivered update lands once. Outbound: every action carries its `dedup_key`, and the
adapter refuses an action whose key it has already confirmed. The key is keyed on the intended effect —
this checkpoint, presented to this principal — and never on the message, which by construction does not
exist until the send has been confirmed
(`adapters.md#an-artifact-exists-only-once-its-external-record-does-and-the-interval-before-that-belongs-to-the-action`).
A send whose confirmation does not come back leaves the action `unknown`; the recovery is stated below.

**Unknown, and every delivery's disposition.** An update the adapter cannot map, a payload it does not
recognize, a reply matching none of a checkpoint's options — each is `unknown` or a `dropped` with a
reason, and never coerced to the nearest outcome (principle 7). Every delivery reaches a disposition, and
drops are counted per window and surfaced on the off-record path.

**Provenance and read-back.** Every write names the adapter, the channel, and the update identifier; every
write carrying a decision — a resolution above all — is read back before the adapter acknowledges the
delivery (principle 2). The read-back matters more here than anywhere: a resolution is what releases an
action the gate held, so a resolution that reported success without landing would release nothing while the
operator believes they have decided, and the checkpoint would sit open behind an answer that was given.

## What this document refuses, and why

Each refusal is a thing the channel makes easy and the design declines. Grouped by what would break.

**It refuses to read intent from free text.** No natural-language command routing, no keyword that opens a
workflow, no phrase that closes a step, no nearest-match against a checkpoint's options. Text from a
principal is a resolution only through correlation and only into an option the checkpoint declared;
otherwise it is a task, an observation, or a drop. Reason: an intent parse is a judgement made by a
component that answers for nothing, on input a third party controls.

**It refuses to treat a gesture as a decision.** A reaction, a read receipt (which does not exist), a
message viewed, a poll vote, a member joining — none is an approval. Reason: an approval is attributed to a
principal and authorized against the required approvers, and a gesture carries neither the attribution the
record needs nor a statement of what was decided. The reaction row above is handled as an observation, and
decision 25 rules that it stays one; it is the row a future reader will most want to make an exception for,
and the ruling below records why not.

**It refuses to let the presence of a person in a chat stand in for a binding.** Group membership is not a
credential binding; a display name is not an identity; a forwarded message's original author is not its
sender. Reason: the fallthrough that resolves an unknown actor to somebody is the failure this design
removes everywhere, and here the somebody would be the operator.

**It refuses to act on a message during a halt.** It writes nothing, acknowledges nothing, and lets the
channel redeliver. Reason: a signal the record cannot hold is not a signal the engine may act on. The one
carve-out is the read command answered with the halt itself (decision 26, below), which writes nothing,
decides nothing, and rides the announcement path that already runs without the record.

**It refuses to build chat-shaped state beside the record.** No conversation store, no per-chat map of
which checkpoints are outstanding, no cursor table standing in for coverage, no cache of what a message
used to say. Reason: each is a second copy of something the record already reconstructs, needing a process
to keep it true (principle 11) — and an adapter holding a map of which checkpoints are outstanding has
become a second engine.

**It refuses to send a decision-bearing message it cannot record.** The one-way announcement path above is
the exception that proves it: the only messages sent without the record are those that ask for nothing.

**It refuses to delete or edit away a message to make the record look clean.** A superseded presentation is
edited to say it is superseded, and both readings stay readable — the same rule the `publish` class's
forward-only recovery states (`adapters.md#outbound-steps-produce-actions-adapters-take-them`).

## Recovery: what undoes an effect on this channel

`failure_posture.md` requires every action class to name its recovery, even where the recovery is only a
forward fix. This channel's classes name theirs here, and the shape is forward-only throughout for a reason
worth stating: **a message a person has already seen cannot be unsent.** The channel offers deletion, and
deletion changes what is displayed rather than what was read.

| What was taken | Recovery | Why this and not a reversal |
|---|---|---|
| a notification sent in error | send a correction; where the original is still editable, edit it to say it is superseded | the operator may have read it. A deletion that leaves them acting on what they read is worse than a correction they can see |
| a checkpoint presented to the wrong chat | present it correctly, and edit the original to say it was withdrawn | the disclosure already happened; a deletion does not undo it, and pretending otherwise would leave the disclosure unrecorded |
| an inline keyboard offering options that are no longer valid | remove the keyboard, and edit the message to say the checkpoint is terminal | a stale button is a live-looking control over a terminal checkpoint, which is the thing to remove; the message's history is not |
| a send whose confirmation never came back | **read the channel back for the action's `dedup_key` before sending again** | this is `failure_posture.md` rule 6's shape at this boundary: the effect may have landed. The action reads `unknown` until the read resolves it, and `unknown` holds |
| anything past the channel's edit or delete window | **a new message that states the correction**, and nothing else | the channel bounds editing and deletion by time. Past the bound the forward fix is the only fix — which is not a degradation, because it is what every row above already does: the operator may have read the original, so a correction they can see beats a retraction they cannot |

**Where a recovery is itself an action, it goes through the gate under its own class.** There is no
privileged path by which the adapter unsends something because it judged the send wrong.

**A stale button outlives the ability to remove it, and that is why a callback is checked and not trusted.**
The recovery rows above remove a keyboard where they can, and past the edit window they cannot. So a
terminal checkpoint may keep a live-looking button indefinitely, and the design's protection against that is
not the removal — it is the rule that a press is resolved against the record's current state, where the
checkpoint is terminal and is not resolved again. The cleanup is a courtesy to the reader; the correctness
is in the check.

## What this document does not decide

The general adapter rules are `adapters.md`'s and are cited here, not restated: the four outcomes, the five
rules, the sourcing and coverage contract, and the rule that a recovery is an outbound operation like any
other. Where inbound delivery lands is ruled there (decision 16), and *Delivery* above states what this
channel's two mechanisms give that ruling. Whether adapters live in a repository of their own is **open
decision 15**, untouched. The steps that take these operations are `workflows.md`'s; the gate's decision
function and the checkpoint's protocol are `gates_and_workflows.md`'s; whom a checkpoint may await, and
whether its raiser may resolve it, are `authority_model.md`'s open questions. Which rows have a built path is
`status.md`'s — and every row marked **unhandled** above has one there. Two decisions this document opened
are ruled in the two sections that follow.

## A reaction never carries a decision

**Ruled (decision 25, 2026-09-05): no.** Registered in `conformance.md#the-register-of-open-design-decisions`.
A reaction on any message, the swarm's own presentation included, is an observation on the artifact and never
a resolution, an approval, or any other decision. The row above is handled as such, and the refusal above — a
gesture is not a decision — holds without the exception this question asked about.

**Reason, in two parts, conceding a third.** The case for allowing it rested on correlation: a reaction on
the swarm's presentation message is correlated to that checkpoint as precisely as a reply-to is, and it
arrives from a credential like any other delivery. That is conceded — correlation is what a reaction *can*
do. It fails on the other two properties a decision must have, and either would suffice. First, **a reaction
can be silently removed.** Removing one is not destroying evidence of a statement, the way deleting a message
is; it is the reaction's ordinary use — the channel treats a reaction as a toggled state and delivers its
removal as a new state, not as an event about a decision. A resolution taken from one would be a resolution
whose evidence the approver can withdraw as a matter of routine, with no record of having decided and no
record of having reversed, and an approval that can be retracted without a record is not a verdict
(`authority_model.md#approval`: an approval is explicit and terminal). The design's answer to a *deleted*
resolving message — the resolution stands, the observation is the durable half — does not carry over,
because it depends on the message having been a statement the principal made; a reaction is a state the
principal set. Second, **a reaction's meaning is not the swarm's.** The emoji set is the channel's, fixed and
small; a thumbs-up reads as approve, as acknowledge, as seen, and as agreement with the previous message, and
the reader who decides which is the adapter — which is the intent parse this document refuses, made on the
cheapest gesture the channel offers. A callback payload avoids that because the swarm minted its meaning at
composition (`#the-callback-payload-is-the-swarms-own-text-and-free-text-is-not`); a published
reaction-to-option mapping would try to give a reaction the same property and cannot, because the token is
the channel's and not the swarm's, and the mapping becomes a vocabulary the operator has to remember and the
adapter has to hope they did. A decision must be **explicit** — a yes, a no, or a veto, stated
(`authority_model.md#approval`) — and correlated to one checkpoint by reply-to or by a callback token; a
reaction can do the second and not the first.

**The cost accepted** is that the operator must reply or press a button rather than react, on every
checkpoint, the routine ones included. That is the cost of a decision being a thing the operator did rather
than a thing the transport inferred, which is the position the read-receipt row already takes for the same
reason. **What would reopen it:** nothing about the channel's reaction update, which is not where the ruling
rests. It would reopen only if a decision were redefined to admit retractable, unstated approvals, which is an
authority-model change and not a chat one.

## During a halt, a read on the channel is answered with the halt, and never with data

**Ruled (decision 26, 2026-09-05): the swarm answers with its own state, and nothing else.** Registered in
`conformance.md#the-register-of-open-design-decisions`. A command asking what awaits a principal, or the
state of a batch or task, arriving during a halt, is answered on the channel with the one fact the swarm
holds without the record: that it is halted, since when, and why — *halted since T, because X* — and no
more. Not a cached queue, not the last state it read, not a guess about what has changed. When the record
returns, the redelivered command is answered from the record like any other read.

**Reason.** The two halves of the open question were each half right, and the ruling takes the half of each
that survives. Against answering at all: the answer to "what awaits me" is read from the record, which is
what is unreachable, so there is nothing to answer *from*, and an adapter answering from a cache would be
holding the state `adapters.md#what-the-record-supplies-and-what-an-adapter-therefore-never-builds` forbids
— a picture of the queue that may be wrong, offered with the confidence of an answer, which is reporting
without binding at the moment the operator most needs to trust what they are told. For answering: refusing
entirely makes a halted swarm indistinguishable from a dead process, which is `failure_posture.md` rule 2's
signature failure turned toward the one person asking. The halt's own state is the one fact the adapter holds
without the record — it is what the off-record announcement path already carries (rule 2), and the difference
between announcing it and answering with it is direction, not capability. So the swarm says what it can say
truthfully and nothing it cannot.

**How the answer is bounded, and why it is not a second path.** The answer travels the off-record
announcement path (`#outbound-the-operations-a-step-takes-on-the-channel`, the announcement row), which
already exists, already runs without the record, and already carries exactly this content; it is addressed
to the asker rather than broadcast, and that is the whole of the difference. It is sent **only in a chat the
announcement path already reaches** — the adapter's copy of the `channel_config` binding, refreshed on every
successful read of the record and carrying the staleness bound the binding itself declares — because the
adapter cannot resolve a credential during a halt, and answering an unknown asker anywhere else would
disclose the swarm's state to whoever asked. The copy is a cache, and it takes the rule every cache takes
(`authority_model.md#grants`): past its declared bound with no refresh it resolves to `Indeterminate`, and
the adapter answers no one and announces to no one — the capture of last resort holds what would have been
sent (`failure_posture.md#the-rules`, rule 2) — so a chat unbound since the copy was taken is answered for
at most the bound. An operator who wants a longer outage to stay answered declares a longer bound, which is
a value on the binding and theirs, never a default the adapter supplies. In that chat, the answer
discloses nothing the announcement did not. It carries no decision, no options, and no keyboard, for the
reason the announcement path carries none: a decision taken against an unreachable record cannot be recorded
(`adapters.md#what-the-adapter-does-with-every-event`). And the command's delivery is **not acknowledged**:
the adapter writes nothing during a halt and lets the channel redeliver, so when the record returns the same
command arrives again and is answered properly; a read command has no write to deduplicate, so the
redelivery costs nothing. The refusal above — the adapter does not act on a message during a halt — has this
one carve-out, and it is the same carve-out the announcement path already is: the only messages sent without
the record are those that ask for nothing and decide nothing.

**The cost accepted** is that the operator cannot query state during a halt. That is correct rather than
regrettable: there is no trustworthy state to query, and a design that offered some would be offering the
operator a figure to act on that the swarm itself could not stand behind. **What would reopen it:** the swarm
coming to hold, without the record, some second fact about itself that is true by construction — as its halt
state is — rather than read. None is known; a candidate would have to be defended as such, and the last thing
the record said is not one.

## Freshness

**Written against Telegram's chat-platform API as documented on 2026-09-05.** The enumeration of update kinds,
message content variants, chat kinds, the callback-query surface, and the two delivery mechanisms is the
channel's; the status and disposition columns are this document's.

**What would make it stale**, in the order it is likely to happen:

- **A new update kind.** The channel adds them regularly, and a kind absent from the tables above resolves
  to `dropped` with reason `out_of_scope_class` — counted, so its arrival is readable as a rising number
  rather than as silence. That is the disposition rule working as intended, and it is also the signal to
  re-read this section. The channel's own list grew between revisions while this document was being
  written, which is the ordinary case rather than the exceptional one.
- **A change to the update identifier's properties.** The dedup rule keys on it, and this document's
  membership-window rule exists because the identifier is sequential but not permanently monotonic. A
  change there is the one that would invalidate a rule rather than merely add a row.
- **A change to the retention bound for undelivered updates, or the arrival of a backfill read.** The
  first changes how large a coverage gap an outage produces; the second would remove the gap entirely and
  is the change most worth watching for.
- **A read receipt, or a deletion update for an ordinary chat, becoming available.** The design's position
  is that it would use neither, and that position is stated above with its reasoning rather than as a
  consequence of the API's current shape — so their arrival adds rows to *Conditions that are not updates*
  and changes no rule.
- **A change to the callback payload's size limit**, which is why the payload carries a token rather than a
  description. A larger limit would not change the rule, because the token's opacity is doing work the size
  does not.
- **A change to the edit and delete windows**, which bound what a recovery can do in place.
- **A change to what the swarm sees in a group** under the channel's privacy setting, which is the coverage
  fact the group section records, and which the channel ties to how the swarm was added to the chat.

**What is load-bearing and what is not, for the condensation pass.** Load-bearing: the four-outcome
mapping, the two conditions on the narrow path, the callback-payload distinction and both halves of what it
licenses, the correlation rules, the halt and acknowledgement interaction, and the two narrowings on what is
written. Not load-bearing and safe to compress: the per-update-kind enumeration below the level of the
groups it falls into, the chat-kind table, and the delivery-mechanism comparison, all of which restate the
channel's own documentation in the design's vocabulary.

## What the API offers that this design does not use

Named so the condensation pass knows what is deliberately absent rather than overlooked. Each is a real
capability of the chat API that no workflow in this design reaches for.

| Capability | Why it is unused |
|---|---|
| inline mode (answering a query as the user types elsewhere) | it asks the swarm to author content on a keystroke; no workflow produces content without a step and a sign-off |
| the payments surface (invoices, checkout queries, paid media) | a second payment rail beside `payments.md`'s, and its checkout queries are synchronous, which the action gate's timing cannot accommodate. Marked unhandled above rather than ignored, because a decision is owed |
| polls and quizzes | a second decision queue beside the checkpoint (principle 6) |
| games | nothing in the work model |
| stickers, dice, and reactions as expressive content | carried inbound as observations at most; never authored outbound, because a notification's job is to be read |
| chat administration (promoting, restricting, banning, approving join requests) | who may see the channel is the operator's decision on the channel, and the swarm holds no workflow for it |
| forum topics as a structure the record mirrors | correlation is the record's, through the reply relation and the callback payload; a chat-shaped structure beside the record is what the adapter never builds |
| message scheduling, and auto-deleting messages | the record decides when an effect is taken; a channel-side timer would be a second sequencer |
| link previews, formatting beyond what a notification needs, and message effects | presentation, which the design does not legislate |
| pinning, forwarding, and copying messages | available and unused; a pinned message is not state, for the same reason a label at a code host is not (`github.md`) |
| platform command menus | the affordance is available; what a command produces is a task for intake, so the menu is a convenience over the fourth outcome and never a control surface |

## Drift: what the built path does that this design does not say

Recorded as drift, not as design justification. An existing implementation is never a reason the design
must accommodate it; these rows say what would have to change, not what should be written differently.

Read 2026-09-05 on this branch by enumerating every chat call site under `execution/` and `lib/` and
reading each module. Counts are actual.

| What the design says | What the branch does | Where |
|---|---|---|
| a resolution is authorized by resolving a credential through the binding to a principal who is a required approver | **there is no binding and no principal.** Inbound is gated by literal equality against a configured chat identifier, and in one daemon a configured user identifier as well. There is no credential table, no principal lookup, and no resolution step — equality against a constant is the entire authorization model for everything driven inbound, including a payment approval | the two inbound pollers |
| a checkpoint is presented, and its resolution is written to the record and read back | **the payment approval creates no checkpoint at all.** One daemon sends a preview, blocks on a bounded read of the channel, parses the reply as a string, and proceeds. No checkpoint entity, no gate consulted, no resolution recorded. A timeout, an unparseable reply, and an explicit refusal collapse to one outcome — proceed with nothing — which is at least fail-safe | `execution/daemons/monedula/monedula.py` |
| correlation is structural: a reply-to relation, or a callback payload the swarm minted | **the reply-to half exists and the callback half does not.** The general poller correlates by scanning a recent window of the record's own activity entities for one stamped with the replied-to message identifier, which is the reply-to mechanism this document names. **Zero occurrences repo-wide** of an inline keyboard, a callback payload, or a callback acknowledgement — so the better of the two correlation paths has no code at all, and every interaction is free text | `execution/daemons/cyphorhinus/cyphorhinus.py` |
| a message from a bound principal that correlates to nothing and reads as an ask is a task for intake | **it is discarded.** The general poller ignores any message that is not a reply to one of the swarm's own, and ignores empty text. The channel is deliberately passive, so the fourth outcome has no path: an unsolicited ask reaches nothing | as above |
| every delivery resolves to an outcome or to `dropped` with a counted reason | **the discards above are silent** — no counter, no disposition, no record. This is the receipt-without-disposition shape the disposition rule exists to close, and it is the same finding the other adapters carry | as above |
| an adapter keeps no history of its own | **two adapter-local cursor files**, one per poller, each persisting the channel's offset beside the daemon so replies are not reprocessed across restarts | the two inbound pollers |
| a send is confirmed by reading the message back by its identifier | **twelve send sites, and one of them captures an identifier.** The activity emitter parses a message identifier out of the shared helper's output and stamps it onto the entity it wrote, which is what makes the reply correlation above possible; every other site returns success on an exit code or a library call. There is no read-back anywhere, and **no `artifact` or `action` entity type exists on the branch** for a confirmation to land on | `execution/lib/telegram/send.mjs`, `lib/notify/notifier.py`, `lib/activity/__init__.py`, and nine daemon sites |
| the design uses one adapter per system | **four independent chat clients**: a shared send helper, a notifier reaching the channel through a general notification library, and two daemons written directly against the channel's HTTP surface. They do not share a credential — and the channel admits one long-polling consumer per credential, so a period when two pollers shared one credential produced a conflict on every read, which is recorded in the code as the reason they were split | the modules above |
| the swarm is deliberate about which chat a presentation reaches | **an argument-name mismatch silently drops thread routing.** Two callers pass a thread argument the shared helper does not parse, and its argument loop ignores what it does not recognize, so those messages land in the chat's default thread rather than the configured one. It fails silently in both directions: the sender reports success and the operator sees the message, in the wrong place | `execution/lib/telegram/send.mjs` and its two callers |

One further finding, recorded because it bears on `authority_model.md#grants` rather than on this document:
the shared send helper reads environment files from several hard-coded sibling paths and injects **every**
key it finds into the process environment, not only the ones it needs. That widens the credential reach of a
send far beyond the send.

## Prior art

Telegram's own chat-platform API reference is the source of the enumeration, read 2026-09-05; the update kinds, the
message content variants, and the two delivery mechanisms are the channel's, and the status and disposition
columns are this document's. The anti-corruption layer (Evans) is the shape of the whole: the channel's
model — a conversation, a command menu, a button that looks like it does something — never becomes the
domain's. The distinction between a message the swarm authored and text a person typed is the same
distinction a capability-bearing token draws, and it is why a callback payload is recognized rather than
parsed.

## Beyond the sources

The per-update mapping, the handled / deliberately ignored / unhandled marking, the statement of the
chat-message-is-not-an-instruction rule at length, the callback-payload trust distinction and both halves
of what it licenses, the correlation rules, the two narrowings on what the adapter writes, and the
treatment of the conditions the channel does not report are this document's, applying `adapters.md`'s rules
to the channel's full update list. Decisions 25 and 26 were opened here and are ruled here.
