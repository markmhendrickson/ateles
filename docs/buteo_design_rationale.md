# Buteo — design rationale

A working note on how the legal-review agent in my swarm (Buteo) is being
built to address the failure modes Nick raised in our 2026-05-28 thread:
drift, non-determinism, overly-conservative-and-complex first passes that
compound downstream. Sharing this because the agreement is real and worth
making explicit in the design.

## The agreement we both already share

Human sign-off on legal is non-negotiable. Buteo never sends, never signs,
never enters anything into an enforceable artifact. Every redline lands as
a draft for operator review. That's not aspirational — it's a hard guardrail
in the agent's system prompt and in the dispatch chain (`operator_signoff_required`
is always `true` on Buteo's output).

What's contested isn't "should a human read every legal doc" — it's "can
the first-pass-to-near-final ping-pong be compressed without compounding
risk." This note is the answer.

## Nick's three failure modes, addressed concretely

### 1. Drift and non-determinism

**Concern:** LLMs drift across versions; the same input today gives a
different review next quarter. For legal, that's intolerable without
operator awareness.

**Design:**

- **Model pin, not tier.** Most agents in the swarm declare a *capability
  tier* (`triage` / `synthesis` / `reasoning`) and the framework resolves
  the tier to whatever the current best model is — auto-bump on each
  release. **Buteo opts out.** Its `agent_definition.model_pin` field
  carries an exact model ID (`claude-opus-4-7` today) and the resolver
  honours pins over tiers. Bumping the pin is a deliberate operator
  decision logged as a Neotoma correction observation, with attribution
  and timestamp.
- **Frozen system prompt + version stamp.** Buteo's system prompt is
  versioned (`prompt_version = "2026-05-28.1"` currently) and hashed.
  Every `RedlineReport` stamps `prompt_version`, `model_id`, and
  `playbook_id` as provenance. Diffing two runs of the same input
  immediately shows whether anything changed in the agent's substrate.
- **Temperature = 0.** Deterministic decoding. Same input + same prompt
  version + same pinned model + same playbook = byte-identical output.
- **Eval suite.** Past contract reviews + the operator's edits during
  sign-off form a regression eval set. Promoting a new prompt version
  requires passing the eval. Promoting a new pinned model does too.

The combination means a Buteo run is reproducible, auditable, and any
silent drift trips the eval before reaching production.

### 2. First-pass is overly conservative and complex (and that compounds)

**Concern:** Fair. An LLM with no prior context tends to flag everything,
propose maximalist redlines, and produce a draft that's worse than
useful because it generates more rounds of negotiation.

**Design — the playbook layer:**

A new entity type, `playbook`, carries the accumulated negotiation memory
for a relationship or deal type. Each playbook has:

- `standard_positions` — operator-approved positions on recurring topics.
  Buteo's prompt instructs it to *anchor on these without re-litigating*.
- `non_negotiables` — lines the operator has already decided not to
  cross. Buteo's job is to reject counterparty clauses attempting to
  weaken these, not to "explore alternatives".
- `accepted_redlines` — language operator approved in past contracts.
  Buteo reuses the language verbatim instead of re-inventing it.
- `rejected_positions` — counterparty proposals operator previously
  declined, with rationale. Buteo doesn't re-concede them.

The playbook is loaded as context at the start of every Buteo run. The
first-pass redline arrives at the operator already anchored on positions
they've already settled — which is exactly the "ping-pong" the loop is
trying to remove. The operator's read isn't smaller; the *redundant
re-derivation work* on each round is smaller.

The first playbook (`Bottega8 partnership`, `ent_416966d0c0f8ce0708eb52d0`)
already encodes the agreements you and I reached in the May 2026 thread:
8% sourcing fee, lead-pool priority belongs to Bottega8 / Tech Leaders,
Neotoma/Ateles core remains OSS, and the non-negotiables on my side
around the core framework and generalized methods.

Crucially: the playbook is **operator-authored**, not LLM-derived. When
you and I agree something on a thread, *I* (the operator) add it to the
playbook through a Neotoma correction. Buteo never writes to its own
playbook. The agent's prior decisions don't become its own future ground
truth — only my decisions do.

### 3. Compounding downstream problems

**Concern:** If automated agentic action sits in the middle of any chain,
errors compound; legal is especially unforgiving.

**Design:**

- **Buteo never produces enforceable artifacts.** Its output is a
  structured `RedlineReport`, never a signed document, never a sent
  email, never a committed contract draft.
- **Downstream isolation.** Buteo's output feeds Pavo (commercial-framing
  agent) which produces a *reply draft*. Pavo's draft annotates every
  clause it pulled from Buteo so the operator can audit provenance per
  paragraph. The draft is held until operator sign-off; nothing flows to
  Gmail's `Send` button without explicit human action.
- **Provenance per artifact.** Each `RedlineReport` is linked to the
  `email_message` it reviewed (`REFERS_TO`), the playbook it consumed,
  the prompt version, and the model ID. Each `CommercialFraming` draft
  is linked to the `RedlineReport` it framed (`DEPENDS_ON`). If
  something is wrong in the final reply, the lineage shows exactly which
  upstream artifact introduced the error.
- **Operator corrections as first-class.** When the operator edits
  Buteo's redline during sign-off, the diff is captured as a Neotoma
  `correction` observation linked back to the original clause review.
  Over time this becomes the eval set in (1) and the playbook updates
  in (2).

## What this lets me argue back to the original concern

It's not "I'll trust the agent more over time and pull back HITL." It's
"Buteo's role is to make the operator's read shorter and structured by
what we've already decided. Sign-off doesn't shrink; redundant
re-derivation across contract rounds does."

The reframe addresses the "you just gotta read it" point directly: I do
read it. The improvement is that I'm reading from a starting position
that bakes in prior learnings, with deterministic config, auditable
provenance, and a non-deterministic-but-pinned model. The HITL surface
area stays the same; the per-pass cost goes down.

## Implementation status

Wired today on `claude/email-routing-agent-EQcLJ`:

- `playbook` entity type registered in Neotoma (schema 1.0.0)
- `agent_definition.model_pin` and `agent_definition.prompt_version`
  fields added (agent_definition schema 1.7.0)
- Buteo's playbook loader + provenance stamping in
  `lib/agents/buteo.py`
- Resolver in `lib/model_tiers.py` honours `model_pin` over tier
- Bottega8 playbook entity seeded with positions from the May 2026 thread
- Buteo's `agent_definition` set: `model_pin = claude-opus-4-7`,
  `prompt_version = 2026-05-28.1`, `temperature = 0`

Not yet wired (next slices):

- Operator-correction → eval suite + playbook-update feedback loop
- Pavo reply-draft annotations per clause
- A standing regression eval bank from real prior reviews

## TL;DR

> Buteo isn't trying to replace human legal review. It's a deterministic,
> playbook-anchored first pass with full provenance and zero downstream
> autonomy. The operator's reading still happens on every contract —
> what shrinks is the re-derivation of positions we have already settled.

Happy to walk through any of this in more depth.
