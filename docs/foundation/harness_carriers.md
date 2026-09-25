# Harness carriers: how Ateles gets each payload into each harness it is installed in

**Kind:** foundation; an authored companion that states the design and never the state of a checkout. Not
keyed and not inlined into a review prompt (`conformance.md#scope`). **Derived from:** foundation plan
`ent_81aadb43caf2fa493361e8ed` decisions `carrier_design_docs`, `harness_delivery_ladder`,
`agent_skills_canonical_rule_index`, `e2_render_identically_every_harness`,
`mcp_instructions_not_a_delivery_channel`, `mcp_2026_07_28_is_stateless_target_dual_era`,
`e2_session_transport_leg_moves_to_hook`, `ateles_state_already_externalized`,
`neotoma_conformance_hypothesis_result`, `rule_delivery_is_a_resolution_problem_not_a_payload_problem`,
`agent_identity_is_carried_per_transport_not_by_one_mechanism`, `always_applies_sentinel`,
`standing_rule_entities_carry_pii_and_must_not_be_rendered_to_a_public_repo`,
`rule_trust_operator_approved_only`, and `cross_harness_mcp_first_objective`; analyses
`ent_449b6a3a82c8c45544f3c2ea` (the MCP 2026-07-28 specification reference),
`ent_7f6f202378eb37c0c6aaca9d` (the Ateles mechanisms map, re-verified against main), and
`ent_433b471ef04351c34625db53` (the Neotoma MCP conformance review), all 2026-09-25; issues #1243, #1253,
#1254, #1257, #1261, and #1270, and pull requests #1255 and #1268; register decisions 42, 86, 87, 96, and 97,
which this document applies and does not restate. The operator requested the document on 2026-09-25 (#1262).
What is built is `status.md`.
Amendment history: `revisions.md#harness_carriersmd`.

## Purpose

State, in one place, how the swarm gets each thing it needs inside a harness — its tools, its procedures,
its rules, its orientation at session start, its pre-action checks, its identity, its interface, its
long-lived work, and the operator's decisions — into every harness it is installed in, through the
carriers each harness offers: the protocol, integrations, bundles, local instruction files, and hooks.
The core of the document is one matrix: for each payload and each carrier, whether the carrier can deliver
the payload and whether that delivery is guaranteed or left to the model's discretion. Rules delivery is
one row of that matrix, not the subject of the document.

The design this states existed before this document only as decision entries on the foundation plan,
three analyses, and a dozen issue threads, none of it reviewed as design. A harness-targeting question
cites this document's matrix rather than re-deriving the answer in each issue.

## Scope

**In scope:** the operator's own harness sessions and the harnesses the swarm launches for its own agents
— what each carrier can carry into them, which carrier each harness gets, and where each payload binds.
**Out of scope:** third-party end-user data flows. Nothing here evaluates a harness that a person other
than the operator uses against an integration; a build that exposes a payload on such a surface takes a
fresh data-handling review at that time, and this matrix is not evidence that the case was considered.

**How a Neotoma instance reaches its own clients is a companion document's subject, not this one's.** The
same framework applied to Neotoma as a product — including hosted instances whose users connect only
through an integration, with no hooks and no local files — is specified in
[neotoma#2494](https://github.com/markmhendrickson/neotoma/issues/2494). This document links it and does
not restate it; where Ateles depends on Neotoma's delivery (the record's own admission check, below), it
says so and stops there.

**A launched agent's prompt is not delivered through a harness carrier.** When the swarm starts a runner,
it composes that runner's prompt itself, live from the record, before the harness exists; the harness
carries nothing Ateles did not already hand it at launch (`work_model.md#the-four-execution-mechanisms`).
This document therefore concerns the two paths the swarm does not compose: the **interactive session**
(`vocabulary.md#interactive-session`), which the operator opens, and the carriers a launched harness adds
on its own account. The launch path is named here only as the comparison point. A provider that
refuses a launch for lack of quota is a capability temporarily unavailable, deferred to the reset time the
provider states, never a verdict on the work (decision 60; `failure_posture.md`). Sources: #1257, and plan
decision `a_provider_quota_failure_is_a_retry_not_an_outage`.

**Implementation detail is out of scope.** Hook file paths, matcher strings, and environment variable
names belong in build issues; commands belong in the install guide ([`docs/install.md`](../install.md)),
which reads its per-harness steps from this document's matrix. Vendors are named only in
`#carriers`, in the harness table and traced example of `#targeting`, and in
`#measured-constraints-and-open-questions`; the payload, matrix, and enforcement sections are
harness-neutral.

**No instruction layer points at this document yet.** No agent instruction, skill, or server instruction
tells an agent to consult this matrix before assuming a payload arrived. Until one does, the document is a
reference a reader chooses to open, not a control (`principles.md#1-a-mechanism-that-does-not-bind-is-not-a-control`).

### Terms this document uses

- **Payload**: one kind of thing the swarm needs inside a harness (`#payloads`).
- **Carrier**: one kind of vehicle a harness offers for getting a payload in (`#carriers`).
- **Guaranteed delivery**: the carrier places the payload in the model's context, or runs the check,
  whether or not the model asks. **Discretionary delivery**: the payload reaches the model only when the
  model chooses to fetch it — a tool it decides to call, a file it decides to open. The foundation plan
  and the originating issue name these two with the pair of delivery words the vocabulary reserves
  against the claim (`vocabulary.md#claim`); this document uses guaranteed and discretionary instead, so
  that neither word can be read as how work reaches an agent, which is always a claim (`work_model.md`).
- **Rung**: one step of the delivery ladder (`#targeting`). The foundation plan's decision records these as
  tiers; this document says rung because tier is the blast tier's word (`vocabulary.md`), and two senses of
  one word in one corpus is the overlap principle 12 forbids.

## Payloads

Each row is what Ateles needs inside a harness, and where the design holds it. Every payload's content is
authored once, in the record; a carrier holds a rendering of it, never a second home
(`principles.md#9-one-source-defined-once-a-comment-claiming-parity-is-not-parity`).

| Payload | What it is | Where the content lives |
|---|---|---|
| Reach | The tools and data a model can act with: the swarm's routing, checkpoint, and observability operations, and the record itself. | The Ateles MCP server's tools; the record's own interface. |
| Method | How recurring work is done: procedures a harness loads by name. | A skill is a harness's source state and never a design type (`vocabulary.md#skill`); what a skill carries has its design target in a declaration, an adapter's operation, or a policy. |
| Rules | The standing rules that govern an agent, as an index of when each applies plus the authority to fetch the full text. | `agent_policy` rows in the record. Rules that hold in every situation carry `applies_when = always` and render as a preamble above the conditional index. |
| Session-start context | What a session must know before its first turn: which agent it is, the consent gate, the operating contract. | The operator-facing agent's definition in the record. |
| Guards | Checks that run before an action the session itself takes — a shell command, a file write, a message sent — and can refuse it. | The check's logic is harness plumbing (decision 42); the rule a guard enforces is a record entity like any other rule. |
| Identity and credentials | The principal a harness presents to the record and to the Ateles server, and the credential that proves it. | The principal and its grant are in the record (`authority_model.md#where-a-harness-reaches-the-record-and-what-admits-the-request`); the credential is held by the host, never by a payload. |
| Interface | Interactive surfaces rendered inside a harness (MCP Apps). | None built; the carrier is named so the matrix has a row for it. |
| Long-lived work | Work that outlives one tool call and is polled for its result (MCP Tasks). | The swarm's `task` in the record; a protocol task would be a view of it, never a second record of its status. |
| Operator decisions | A held action waiting on the operator, and the operator's answer. | The `checkpoint` (`gates_and_workflows.md#the-checkpoint`). |

**Identity is the one payload whose material must never ride a guaranteed-delivery carrier into a model's context.** A
carrier can *hold* a credential in three ways — an MCP registration's environment or header, a hook that
inherits the host's environment, an integration's OAuth grant — and each is the host presenting the
credential on the model's behalf. None of them *should* place credential material where the model reads
it: a local instruction file never carries a credential, and a hook's printed output never includes one.
Where a carrier's only path would put credential material into context, the carrier is refused for that
payload, and an absent or malformed credential resolves to no access, never to a permissive default
(`principles.md#5-fail-closed-on-the-field-that-carries-the-safety-meaning`). How the principal is carried
differs per transport, and a new transport is asked that question on its own rather than assumed covered
by an existing credential path (`#identity-is-carried-per-transport`).

## Carriers

Five carriers, with each vendor's current name as of 2026-09-25. A carrier is what a harness offers; the
design chooses among them per harness (`#targeting`).

| Carrier | What it is | Current vendor names |
|---|---|---|
| Protocol | An MCP server the harness connects to, and the primitives it serves: `instructions` (a free-text field returned at connection, with no size limit in the specification), tools (model-invoked), resources (application-read), prompts (user-selected), and the official extensions — Skills (skills listed and read over resources), Tasks (a polled result that outlives the call), and Apps (interactive interfaces declared as resources); elicitation asks the user for input in the middle of a call. | Model Context Protocol, specification 2025-11-25 (legacy era) and 2026-07-28 (modern era). |
| Integration | A remote MCP endpoint registered once in a consumer application's own directory or settings, reached over HTTPS with OAuth. It is the protocol carrier with no local process and no local files. | Claude connectors (Claude web and desktop chat); ChatGPT apps. |
| Bundle | One installable package carrying several carriers together — an MCP server registration, skills, and, where the harness supports them, hooks. A bundle delivers no more than the carriers inside it: a bundle that carries no hook guarantees nothing a bare integration could not. | Claude Code plugins (can carry hooks); ChatGPT and Codex plugins, and Agent Plugins 1.0 (carry no hooks). |
| Local instruction file | A file the harness reads from disk into the model's context at session start. | `CLAUDE.md` (Claude Code), `AGENTS.md` (Codex and others), Cursor rules files. |
| Hook | A command the harness runs at a lifecycle point — session start, before a tool call, at compaction, at stop — whose printed output the harness can place in context and whose exit can refuse an action. | Claude Code hooks; Cursor hooks; Codex hooks. |

## Capability Matrix

**Legend.** Every cell holds exactly one token.

- `guaranteed` — the carrier can deliver the payload, and delivery does not depend on the model choosing it.
- `discretionary` — the carrier can deliver the payload only when the model (or, for an interface, the user)
  chooses to fetch or invoke it.
- `n/a` — the carrier cannot deliver this payload.
- `unverified` — not a fourth delivery state: no source this document derives from establishes the cell,
  so it is read as `n/a` for design purposes until measured (`principles.md#7-unknown-stays-distinct-from-a-conclusion`),
  and never as a quiet `discretionary`.

A cell states how the payload *as Ateles needs it* reaches the model through that carrier. Where a carrier
can guarantee a pointer but not the payload itself, the cell is `discretionary`: the pointer's arrival is not the
payload's.

| Payload \ Carrier | [Protocol](#carriers) | [Integration](#carriers) | [Bundle](#carriers) | [Local file](#carriers) | [Hook](#carriers) |
|---|---|---|---|---|---|
| [Reach](#payloads) | `discretionary` [1] | `discretionary` [1] | `discretionary` [1] | `n/a` [2] | `n/a` [2] |
| [Method](#payloads) | `discretionary` [3] | `discretionary` [3] | `discretionary` [4] | `guaranteed` [5] | `guaranteed` [6] |
| [Rules](#payloads) | `discretionary` [7] | `discretionary` [7] | `discretionary` [8] | `guaranteed` [5] | `guaranteed` [6] |
| [Session-start context](#payloads) | `discretionary` [7] | `discretionary` [7] | `discretionary` [8] | `guaranteed` [5] | `guaranteed` [6] |
| [Guards](#payloads) | `n/a` [9] | `n/a` [9] | `n/a` [8] | `discretionary` [10] | `guaranteed` [11] |
| [Identity and credentials](#payloads) | `guaranteed` [12] | `guaranteed` [12] | `guaranteed` [12] | `n/a` [13] | `guaranteed` [14] |
| [Interface](#payloads) | `discretionary` [15] | `unverified` [16] | `unverified` [16] | `n/a` [2] | `n/a` [2] |
| [Long-lived work](#payloads) | `discretionary` [17] | `unverified` [16] | `unverified` [16] | `n/a` [2] | `n/a` [2] |
| [Operator decisions](#payloads) | `discretionary` [18] | `discretionary` [18] | `discretionary` [18] | `n/a` [2] | `guaranteed` [19] |

**Why each cell reads as it does.**

1. Tools are model-invoked: a tool reaches the model's work only when the model decides to call it. An
   integration and a bundle serve the same tools through the same protocol. Sources: analysis
   `ent_449b6a3a82c8c45544f3c2ea` (tools, resources, prompts); the channel table in the #1243 investigation.
2. The carrier has no mechanism for this payload: a local file carries text and exposes no callable tool,
   interface, or polled result; a hook runs at lifecycle points and serves neither tools nor interfaces; a
   local file cannot hold an operator decision that changes after it was written.
3. Skills over the protocol are listed and read by the host, and the specification leaves the interaction
   model to the host; a host that never lists skills gives the model nothing. Prompts are user-selected,
   not model-read. Sources: analysis `ent_449b6a3a82c8c45544f3c2ea` (Skills, SEP-2640); analysis
   `ent_433b471ef04351c34625db53` ("necessary but not sufficient").
4. A bundle's skills surface their descriptions at startup and load their bodies only on activation, which
   the model or user chooses. Sources: the Agent Skills load model as recorded in the #1243 investigation.
5. A local instruction file is read whole into context at session start, so everything it holds arrives.
   The design renders nothing into one where a hook exists (`#contradictions-this-document-settles`).
   Sources: the #1243 investigation (vendor guidance naming the instruction file as the channel for
   standing content).
6. A session-start hook's printed output is placed in context without the model asking, at startup,
   resume, clear, and compaction — the only carrier with guaranteed delivery at the compaction boundary. It delivers only
   within its output cap: above it, the harness replaces the output with a file pointer and a short
   preview, which is `discretionary` for everything past the preview. Sources: #1254; the correction in PR #1255
   that a resumed session does receive session-start hook output; plan decision
   `agent_skills_canonical_rule_index` (hooks stay the only channel with guaranteed delivery).
7. The protocol's one guaranteed-delivery primitive, `instructions`, is capped by the client near 2,048 characters across
   every connected server combined and truncated silently, so it carries a pointer to the rules and not the
   rules; the payload is then fetched by a tool call, which is `discretionary`. Sources: plan decision
   `mcp_instructions_not_a_delivery_channel`; `#1-mcp-instructions-share-one-client-budget`.
8. A bundle guarantees delivery only through the hook it carries, and that delivery is the hook's column, not the
   bundle's. A bundle whose harness runs no hooks — every bundle format but one, as of 2026-09-25 —
   delivers what an integration would. Sources: plan decision `harness_delivery_ladder` (refinement 1).
9. A protocol server sees only the calls made to it. It can refuse a write to itself — and the record's
   admission check does exactly that (`#enforcement`) — but it cannot see or refuse the harness's other
   actions, which is what a guard is for.
10. A rule written in a local file asks the model not to act; whether it complies is the model's
    discretion. That is guidance, not a check.
11. A pre-action hook runs before the tool call and its exit status can refuse it, whatever the model
    intended. Sources: the pre-action hooks this repository wires (`status.md`).
12. The harness presents the credential on the transport, on every request, without the model's
    involvement: a header or environment on a local server, OAuth on an integration, the bundle's own
    server registration. Sources: plan decision `agent_identity_is_carried_per_transport_not_by_one_mechanism`.
13. A local instruction file is read into the model's context and must never hold a credential (`#payloads`).
14. A hook inherits the host's environment, so it holds whatever the host holds, and a hook that reads
    the record presents the host's principal. That makes a hook's reach a grant question
    (`authority_model.md#a-harness-provides-only-what-a-grant-names-and-a-provider-that-does-not-enforce-is-a-capability-the-grant-names`),
    and its printed output must never carry the credential it used.
15. An MCP App renders when a tool that references its declared interface is called, so it reaches the
    user only through a call the model made. Sources: analysis `ent_449b6a3a82c8c45544f3c2ea` (Apps,
    SEP-1865).
16. No source establishes which consumer applications or bundle formats render MCP Apps or honour the
    Tasks extension for a server Ateles would register. Measure before designing on it.
17. A protocol task is created at the server's discretion and polled by the client for its result; only
    `tools/call` is task-augmentable, so the task begins inside a call the model chose. Sources: analysis
    `ent_449b6a3a82c8c45544f3c2ea` (Tasks, SEP-2663).
18. Pending decisions are read by a tool the model calls; elicitation asks the user only inside a call the
    model already made. The decision's real notification travels off the harness, on the operator's chat
    channel (`telegram.md`). Sources: analysis `ent_7f6f202378eb37c0c6aaca9d` (checkpoint read side);
    analysis `ent_449b6a3a82c8c45544f3c2ea` (elicitation under multi round-trip requests).
19. A session-start hook can print the pending-decision queue into context at every session start and
    compaction. Nothing prints it today; the cell states what the carrier can do.

## Targeting

**The rules in this section.**

- A harness gets the highest rung its capabilities support, chosen by whether it can run a session-start hook, never by the vendor's label for the carrier.
- Every rung is rendered from one source in the record, live where a hook can render it and generated only where no hook exists.
- Enforcement never degrades down the ladder; only delivery does.
- A harness absent from this matrix has an unknown rung, not the lowest one.

**A harness gets the highest rung its capabilities support, chosen by whether it can run a session-start
hook, never by the vendor's label for the carrier.** The ladder, highest first:

1. **Bundle with a hook** — one install carrying reach, method, and guaranteed delivery: an MCP server
   registration, the skills, and a session-start hook. This is the only rung with guaranteed delivery of
   rules, session-start context, and guards.
2. **Integration** — one connected service in a consumer application's directory, carrying reach and the
   protocol's pointer, and nothing delivered beyond it without the model asking.
3. **Protocol** — a raw MCP server configured by hand, carrying the same as an integration.

**Beside the ladder, not on it:** a **local instruction file** for a harness that reads one and runs no
hook. It is the only guaranteed delivery of rules or session-start context into such a harness, and it is generated,
never hand-copied (the next rule). A bundle whose format carries no hook sits at the integration rung,
whatever its vendor calls it: it guarantees nothing an integration could not. Sources: plan decision
`harness_delivery_ladder`, operator-proposed 2026-09-25, with its four refinements.

**Every rung is rendered from one source in the record, live where a hook can render it and generated only
where no hook exists.** The rule index, the session-start context, and the skills are rendered from the
record at the moment of delivery wherever a hook can do the rendering, so no copy exists to drift. Where no
hook exists, a local instruction file is generated from the same source by the installer and regenerated on
every install, never edited by hand — the one place a generated copy is admitted, because the alternative
there is no guaranteed delivery at all. The index renders identically on every harness, including one with room to carry
the full corpus, so that the index technique is proven rather than papered over where headroom exists.
Sources: plan decisions `agent_skills_canonical_rule_index`, `e2_render_identically_every_harness`, and
`rule_delivery_is_a_resolution_problem_not_a_payload_problem`.

**The mechanism that renders from the record is itself a read of the record, and inherits its bounds.** It
reads only the operator's own instance, with a credential scoped to read what it renders and nothing wider:
never a guest credential scoped for sharing a page, never a write credential where a read suffices
(`authority_model.md#a-proxy-passes-through-the-agents-own-credential-and-holds-none-of-its-own`). Only
rules whose operator approval is proven by provenance render into a session; a rule an agent wrote is a
proposal until approved, because write access to a rendered rule would otherwise be the ability to
instruct every future session. Sources: plan decision `rule_trust_operator_approved_only` (the operator's
ruling of 2026-09-25 on #1270). A rendering that would place operator-specific content — a payee, an
account, an amount — into a public repository is refused at the boundary, which is one more reason the
live render writes no file. Sources: plan decision
`standing_rule_entities_carry_pii_and_must_not_be_rendered_to_a_public_repo`.

**Enforcement never degrades down the ladder; only delivery does.** Every rung bottoms out at the same
MCP server and the same record, and the record refuses a write its admission check does not admit whatever
the harness delivered to the model. A harness on a lower rung is less well informed, never less bound, for
everything the record and the server enforce (`#enforcement`). Sources: plan decision
`harness_delivery_ladder` (refinement 4).

**A harness absent from this matrix has an unknown rung, not the lowest one.** An unlisted harness has not
been evaluated. Do not infer a rung for it, and do not treat its cells as `n/a`; evaluate it against the
carriers first.

**A new cell that needs a new interface goes through the contract first.** Adding a payload or carrier
here that requires a new MCP tool, a new response field, or a new refusal the protocol has not defined does not authorize building
it: the interface is specified and reviewed before the handler, as any other interface change is. This
document is upstream of that ordering, not exempt from it.

### The harnesses evaluated

| Harness | Hook whose output reaches context | Local instruction file | Integration | Derived rung |
|---|---|---|---|---|
| Claude Code (terminal and desktop code sessions) | yes | yes | not needed | bundle with a hook |
| Claude chat (web and desktop) | no | `unverified` (a project's instructions may act as one) | yes | integration |
| ChatGPT | no | no | yes | integration |
| Codex | `unverified` | yes | not needed | protocol, beside a generated local file |
| Cursor | `unverified` (session-start hooks exist; whether their output reaches context is unmeasured) | yes | not needed | protocol, beside a generated local file |

The Claude Code row is measured: session-start hook output was observed in context at startup, resume, and
compaction, and its cap was observed. The ChatGPT and Claude chat rows follow from their
carriers offering no hook. The Codex and Cursor hook cells are `unverified`: a hook package for each exists
in the Neotoma repository, but no source establishes that a session-start hook's output reaches the model's
context in either. If it does, the harness moves up to the first rung and the generated local file is
retired for it. Sources: #1254 and PR #1255 (the Claude Code observations); plan decision
`harness_delivery_ladder`; the Neotoma repository's harness hook packages, read 2026-09-25.

### Example: Claude Code traced through the matrix

Walk the Claude Code row through the matrix. It runs hooks, so the **Hook** column applies in full: rules,
session-start context, and method are `guaranteed` [6]; guards are `guaranteed` [11]; operator decisions are `guaranteed` [19].
Its tools come through the protocol, so reach is `discretionary` [1], as on every harness — no carrier makes a tool
call for the model. Nothing in its row needs the local-file column, so no file is generated for it; the rule index is
rendered live by the session-start hook. The highest rung it supports is therefore **bundle with a hook**:
one install carrying the MCP server registration, the skills, the session-start hook, and the pre-action
hooks. The one limit the matrix carries into that rung is the hook's output cap [6]: everything the hook
delivers — the always-applies preamble, the conditional index, the session-start context — must fit under
the measured cap together, or the part past the preview silently becomes `discretionary`
(`#2-session-start-hook-output-is-capped`).

## Enforcement

**The rules in this section.**

- Stored data binds at the record's own admission check, on every rung.
- A swarm action binds at the action gate, which no harness carrier bypasses.
- An interactive session's own actions bind only where a hook can refuse them, and nowhere else.

**Stored data binds at the record's own admission check, on every rung.** Every write any harness makes
reaches the record, and the record admits it against the requesting principal's grant; a proxy in the path
is permitted and is never the enforcement point
(`authority_model.md#where-a-harness-reaches-the-record-and-what-admits-the-request`). This is what makes
enforcement independent of delivery: a rule the model never saw still binds a write the record refuses. How
a Neotoma instance conveys its own policy to a client, and how its write enforcement reads, is the
companion document's (`#scope`).

**A swarm action binds at the action gate, which no harness carrier bypasses.** An action the swarm takes —
a merge, a payment, a message sent — goes through the gate and, where held, a checkpoint
(`gates_and_workflows.md#the-action-gate-is-pr-independent`). Delivering a decision into a harness
([Operator decisions](#payloads)) is a way to show it to the operator; resolving it is the checkpoint's
one protocol, with its own read-back, and no carrier offers a second path to the resolution.

**An interactive session's own actions bind only where a hook can refuse them, and nowhere else.** A
session's shell commands, file writes, and messages sent through a harness's own tools pass through no gate
and touch the record only where they write to it. A pre-action hook is the only carrier that can refuse one
[11]. On a harness with no hook — every rung below the first — nothing refuses the session's own actions
against a system other than the record; a rule in a local file or a pointer in `instructions` asks, and does
not bind. This is the named gap of the interactive session (`work_model.md#the-four-execution-mechanisms`),
stated here per carrier so that no harness row is read as closing it.

**Which binding point each payload reaches:**

| Payload | Binds at |
|---|---|
| [Reach](#payloads) | The record's admission check, for every read and write the tools make; the server's own refusals for its one mutating operation. |
| [Method](#payloads) | Nowhere by itself: a procedure binds through the step, grant, or policy its content targets (`vocabulary.md#skill`). |
| [Rules](#payloads) | The record for any write a rule governs; a pre-action hook for the session's other actions; otherwise delivery only. |
| [Session-start context](#payloads) | Delivery only. It informs; the three points above bind. |
| [Guards](#payloads) | The hook itself, on a harness that runs one; on any other harness, the gap above. |
| [Identity and credentials](#payloads) | The record's admission check, matched on the credential actually presented. |
| [Interface](#payloads) | The record and the action gate, for whatever an interface's calls write or take. |
| [Long-lived work](#payloads) | The record, where the `task` lives; a protocol task is a view and binds nothing. |
| [Operator decisions](#payloads) | The checkpoint's resolution protocol and its read-back. |

### Identity is carried per transport

A server launched as a local command and a server reached over HTTP carry a principal differently, and
neither mechanism subsumes the other: an HTTP session that authenticates once with a static header cannot
carry a per-request signature, and a harness that launches a command has no static-header slot. When a
transport is added — an integration for Ateles, a new launch path — the question asked is how the principal
is carried on that transport, never whether an existing credential path already covers it. A credential
passes through as the principal's own; nothing in the path holds one of its own
(`authority_model.md#a-proxy-passes-through-the-agents-own-credential-and-holds-none-of-its-own`). Sources:
plan decision `agent_identity_is_carried_per_transport_not_by_one_mechanism`, ruled 2026-09-18.

## Measured Constraints and Open Questions

Each entry is updated in place when it changes, never appended to. Each carries the date it was last
verified, its evidence, and what would invalidate it (`principles.md#8-every-figure-carries-its-date-and-its-instrument-re-measure-before-acting`).
A figure here constrains the design; it is never evidence about the state of a checkout, which is
`status.md`'s.

### 1. MCP instructions share one client budget

A client caps the `instructions` text of all connected MCP servers combined near 2,048 characters and
truncates the excess silently; a server can neither detect the cap nor choose what survives. The
specification states no limit, so the channel degrades as more servers are connected, independent of what
any one server sends. The Ateles server's `instructions` therefore carry a fixed pointer under a budget
well below the shared cap, and nothing else.

- **Last verified:** 2026-09-25.
- **Invalidated by:** a client release that documents or removes the cap, or a spec revision that bounds
  or prioritises the field.
- Sources: anthropics/claude-code#43474 (the cap spans all servers, truncation is silent); a local client
  entry reading "Server instructions truncated from 4989 to 2048 chars", recorded in #1253 and PR #1255;
  the MCP 2025-11-25 and 2026-07-28 specification pages, neither of which bounds the field (analysis
  `ent_433b471ef04351c34625db53`); PR #1255 set the Ateles budget at 1,200 characters, and the block measured
  1,106 characters on the deployment checkout on 2026-09-25 (#1253).

### 2. Session-start hook output is capped

Hook output above a threshold is replaced in context by a pointer to a saved file and a preview of about
2 KB, so everything past the preview becomes `discretionary`. The threshold lies below 16.3 KB; several secondary
reports converge on about 10,000 characters, which no first-party source confirms. Whether the cap applies
per hook or to all session-start hooks together is unmeasured. The rule index is held under a budget with a
guard that refuses to render rather than cutting a rule in half.

- **Last verified:** 2026-09-25.
- **Invalidated by:** a controlled hook emitting a known size and observed at the boundary, or a client
  release that documents the figure.
- Sources: #1254 (a 16.3 KB output replaced by a pointer and a 2 KB preview at a compaction boundary);
  PR #1268 (the secondary reports and a working budget of 8,000 characters; its first commit's live render of
  the active rule corpus measured 12,633 characters, over that budget, before a same-PR follow-up added
  tiered rendering — a re-render of the same live corpus against the tiered renderer measured tier B, about
  5,093 characters, the full index reaching the session rather than the fail-open notice; a later follow-up
  narrowed session scoping to the single session principal, which may change the row count, but no fresh live
  measurement exists as of this PR's head — see #1268's PR body for the current figures and status.md's
  entry on the rule-index hook for the head commit); the correction in PR #1255 that session-start hooks do
  fire on resume and compaction.

### 3. Guaranteed-delivery channels and broader visibility

Two observed paths put delivered content where a reader other than the session can see it. A hook output over
its cap is written whole to a file on local disk, which persists what the preview withheld. And
rule rows can carry operator-specific payment details, so a rendering into a public repository would
publish them. The design's answers are the ones above: the live render writes no file and logs no rule
body, and only operator-approved rules render. **Open:** no source establishes whether an MCP `instructions`
block or a hook's context injection is echoed into a transcript, a shared log, or a synced history with
broader visibility than the session. Until measured, treat every guaranteed-delivery carrier as possibly persisted, and
put nothing in one that could not be.

- **Last verified:** 2026-09-25.
- **Invalidated by:** a measured account of where each client persists injected context.
- Sources: #1254; #1261 (rule bodies are never logged); plan decision
  `standing_rule_entities_carry_pii_and_must_not_be_rendered_to_a_public_repo`.

### 4. Skills-extension client support

The MCP Skills extension is final in the specification, but as of this date only one consumer client
implements it, partially, alongside two developer tools. A skill served only over the extension reaches
almost no harness; the extension is an added transport for the same rendered skills, never a replacement
for the hook.

- **Last verified:** 2026-09-25.
- **Invalidated by:** the extension's published client support table listing a harness in
  `#the-harnesses-evaluated`.
- Sources: plan decision `agent_skills_canonical_rule_index`; #1261; analysis
  `ent_449b6a3a82c8c45544f3c2ea` (SEP-2640, merged 2026-09-13).

### 5. The 2026-07-28 protocol has no SDK, and every known client speaks the legacy era

The 2026-07-28 specification removes the connection handshake and protocol sessions; a legacy client
against a modern-only server fails, with no fall-forward. No published MCP SDK implements the revision, and
the clients the swarm is known to serve connect through the legacy handshake. Any upgrade of the Ateles or
Neotoma server is therefore dual-era — serving both — and never modern-only. The swarm keeps no per-session
state in its server and externalizes all durable state to the record, so the stateless revision is aligned
with the design rather than disruptive to it.

- **Last verified:** 2026-09-25 (SDK checked to version 1.30.1, published 2026-09-23).
- **Invalidated by:** an SDK release declaring 2026-07-28, or a known client moving to discovery-only
  connection.
- Sources: plan decisions `mcp_2026_07_28_is_stateless_target_dual_era` and
  `ateles_state_already_externalized`; analysis `ent_433b471ef04351c34625db53` (the versioning page's
  compatibility table, and the SDK check); the pending upgrade tasks `ent_0b2da0b1a2d228ce3d981525`
  (Neotoma, neotoma#2070) and `ent_2ef66f349c8c1c670ead1582` (Ateles, queued behind it).

### 6. A provider that cannot deny one tool

On the launch path, only one of the three command-line adapters the swarm starts can deny a specific MCP
tool; the other two either receive no tool bound or approve every MCP call. A run that must not reach a
given tool is therefore refused on those two rather than started unbounded, which is decision 87 applied:
routing to a provider that enforces no bound is a capability the grant must name.

- **Last verified:** 2026-09-25.
- **Invalidated by:** either adapter gaining a per-tool deny.
- Sources: the launch adapters' own comments in the code that starts a runner, read 2026-09-25; decision 87.

### 7. Open questions

Each is a measurement, not a design decision; the design above holds whichever way it lands, and says
which cells move.

- **The session-start hook's exact cap, and whether it is per hook or shared.** Moves the rule-index
  budget; no cell changes.
- **Whether the client's `instructions` cap applies to the field when served by the 2026-07-28 discovery
  call.** No cell changes until a known client connects that way.
- **Whether Codex and Cursor place session-start hook output in the model's context.** If yes, each moves
  to the first rung (`#the-harnesses-evaluated`).
- **Whether a Claude chat project's instructions act as a local instruction file.** If yes, rules and
  session-start context become `guaranteed` there through a generated file.
- **Which integrations and bundle formats render MCP Apps and honour the Tasks extension** (the
  `unverified` cells).
- **Where each client persists injected context** (`#3-guaranteed-delivery-channels-and-broader-visibility`).

## Contradictions this document settles

**Generated files, or none.** The rule-index decision says the hook renders live with no generated files,
because a copy per checkout is how one instruction file drifted into 31 versions; the ladder decision puts
a generated instruction file beside the ladder, and the operator's scope addition on #1262 gives Codex a
generated `AGENTS.md`. Both hold, for different harnesses: where a hook can render, nothing is generated;
where none can, a generated file is the only guaranteed delivery there is, and it is regenerated from the record by the
installer rather than maintained (`#targeting`). Sources: plan decisions `agent_skills_canonical_rule_index`,
`harness_delivery_ladder`, and `claude_md_divergence_remeasured_vs_main`.

**The ground the first channel choice stood on.** The session-transport leg of the rules exit gate was first
routed through MCP `instructions`, on three premises: that the field has no cap, that a session-start hook
is skipped on resume, and that the hook caps at 10,000 characters. The first is false (the cap is real and
shared), the second is false (resumed sessions receive hook output), and the third is unconfirmed (a cap
exists below 16.3 KB). The leg moves to the session-start hook. Sources: #1243; PR #1255; plan decision
`e2_session_transport_leg_moves_to_hook`.

**Vendor guidance against the ruling.** One vendor's guidance names the local instruction file, not
skills, as the channel for standing rules. The operator ruled that the rule index takes the Agent Skills
format and is delivered by a hook, rendered identically on every harness. The two do not conflict on delivery —
the hook places the index in context exactly as the instruction file would — and the ruling's grounds are
the drift the file cannot avoid and the proof the identical render forces. Sources: the #1243 investigation;
plan decisions `agent_skills_canonical_rule_index` and `e2_render_identically_every_harness`.

**Protocol conformance as the fix for delivery.** The hypothesis that conforming to the current
specification would fix policy and skill delivery did not hold: of nine delivery failures reviewed, seven
were first-party defects, one was fixed by conformance, and one partly. Conformance is necessary for the
dual-era upgrade and not a delivery strategy. Sources: plan decision `neotoma_conformance_hypothesis_result`;
analysis `ent_433b471ef04351c34625db53`.

## Prior art

- The Neotoma repository already ships the per-harness path this design asks Ateles to build: one command
  that configures the MCP server, instruction files, and permissions for a named harness, a separate opt-in
  hook installer per harness, and a Claude Code bundle carrying its hooks. The Ateles installer reuses its
  shape rather than inventing a second one (`principles.md#6-extend-the-mechanism-that-already-generalizes-do-not-build-a-parallel-one`).
  The install guide names the commands ([`docs/install.md`](../install.md)).
- The Agent Skills format (name and description loaded at start, body on activation) is the canonical form
  of the rule index, and the MCP Skills extension serves the same format over the protocol. Sources: plan
  decision `agent_skills_canonical_rule_index`; analysis `ent_449b6a3a82c8c45544f3c2ea`.
- Decision 42 already ruled where a harness's own mechanics live: the tools a principal may invoke are a
  dimension of its grant, harness preference is a vendor binding, and hook wiring stays in the harness's
  configuration (`migration.md#where-a-skills-harness-mechanics-live`). This document adds which carrier
  delivers which payload; it does not move any of the three.
