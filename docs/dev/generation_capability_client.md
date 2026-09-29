# Media-generation capability client

## Purpose

Developer and operator note for `lib/capabilities/` (ateles#1189): how a host
process generates a vector mark, a raster image, or a video through a
vendor-bound, spend-capped client, and why credentials never reach an agent.
This is infrastructure documentation, not marketing copy.

## Scope

In scope: the capability client, its spend cap and ledger, credential handling,
refusal codes, the render-and-critique helper, and the `generation_record`
schema registration. Out of scope: minting or rotating provider keys
(operator-only), mark concept authorship and selection (#1163), and publication
consent.

**Design basis:** `docs/foundation/principles.md#5-fail-closed-on-the-field-that-carries-the-safety-meaning`
(a call may spend money only when the cap is readable, set, and sufficient),
and decision 35 / `docs/foundation/adapters.md` (`vendor_binding` is the single
binding type; there is no second binding type for generation and nothing goes
through `harness_router`).

**Single public surface:** `lib.capabilities.generation`
(`generate`, `render_and_critique`, `GenerationRefused`). There is no CLI, MCP
or HTTP entry point. A later surface must inherit every refuse code below and
ship a parity test per surface.

## NEVER put generation keys in an agent environment

Generation vendor keys (`GEMINI_API_KEY`, `GOOGLE_API_KEY`, `RECRAFT_API_KEY`,
and anything with a `GEMINI_`, `GOOGLE_GENERATIVE_AI_`, `GOOGLE_GENAI_`,
`RECRAFT_` or `VEO_` prefix) belong to the capability client only.

* Never export one into a shell, daemon environment file, launchd plist, or
  the file a daemon loads for its own environment (for example the Neotoma
  `.env`). Daemons pass their environment to dispatched agents; a key there
  would let any agent spend without a cap.
* `skill_runner._subscription_only_env` strips these names from every
  dispatched agent's child environment, and marks the child with
  `ATELES_DISPATCHED_AGENT=1`. The client refuses to run when it sees the
  marker. This is defence in depth, not the primary control: do not rely on it.
* Agents do not import and run the client. They ask the operator session or a
  host-side runner to make the call. The client runs only in a process that is
  allowed to hold the key.

## Trust boundary

```
 operator shell / host script / daemon parent        dispatched agent child
 ┌──────────────────────────────────────┐            ┌────────────────────┐
 │ lib.capabilities.generate()          │            │ env scrubbed by    │
 │  binding → cap → credential → vendor │   never    │ _subscription_only │
 │  reads generation.env itself (0600)  │ ─────────▶ │ _env; marker set   │
 └──────────────────────────────────────┘            └────────────────────┘
```

Known residual risk: the key file is readable by the operator's OS user, and a
dispatched agent runs as that user. The scrubbed environment and the
credential-read guard hook stop the accidental and the in-band paths; they are
not OS-level isolation. Running the client under a separate OS user or service
is a tracked follow-up.

## Slots and bindings

| Slot | Default vendor id | Adapter |
|---|---|---|
| `vector_mark_generation` | `recraft` | refuses `CREDENTIAL_UNRESOLVED` until a host-held OAuth route exists |
| `image_generation` | `google_image` | Gemini image models via `generateContent` |
| `video_generation` | `veo` | Veo 3.1 via `predictLongRunning`, polled |

Slot strings are exact. Aliases (`svg_gen`, `veo`, ...) are rejected with
`BINDING_MISSING`.

A slot is filled by a `vendor_binding` entity with `capability = <slot>`
(`vendor`, `tool_namespace`, `credential_location`, `fallback`, `constraints`).
`credential_location` is a reference (a key name, or `oauth:<route>`), never a
value. `constraints` is a JSON string:

```json
{
  "model_tier": "gemini-3-pro-image",
  "draft_model_tier": "gemini-3.1-flash-lite-image",
  "monthly_cap_usd": 50,
  "cap_group": "google_generation",
  "cap_group_total_usd": 50,
  "credential_env_file": "~/.config/ateles/generation.env"
}
```

`tier=` in `generate` selects `draft`, `standard` or `lite` by reading
`<tier>_model_tier`. The client only reads bindings under the authenticated
operator's Neotoma scope and never accepts a caller-supplied entity id.

## Spend caps are configuration; the client refuses when they are not set

The dollar figure is operator config in the binding, not code. The mechanism
works with any figure and **refuses when the cap is unset, unreadable, or
exhausted, without contacting the vendor**.

* `monthly_cap_usd` missing: `CAP_UNSET`. Unparseable constraints, a
  non-numeric or negative cap, or an unreadable or tampered ledger:
  `CAP_UNREADABLE`. Unknown is never read as zero and never as "allowed".
* Slots sharing a `cap_group` also share `cap_group_total_usd`. A call must fit
  both the slot cap and the group total (the operator's USD 50 is combined
  across the two Google slots).
* A call whose cost cannot be estimated (unknown model, unsupported
  resolution) is refused `CAP_UNREADABLE`, never treated as free.
* `spent + estimate == cap` is allowed; only `>` refuses. Money is compared in
  integer micro-dollars.
* Spend is recorded only after the artifact is received. Two callers cannot
  both authorize against the same remaining balance: the ledger lock is held
  from authorization to recording, so generation is serialized per ledger.
* Recorded cost is the list-price estimate (an upper bound); vendors return no
  billed amount. Reconcile against the provider's invoice. Provider-side
  prepaid balance with auto-reload off remains the real backstop.

The ledger is local and operator-owned: `~/.cache/ateles/generation_spend/<slot>/<YYYY-MM>.jsonl`
(override `ATELES_GENERATION_SPEND_PATH`), directories 0700, files 0600, rows
keyed by `generation_id` and deduplicated before summing. It makes no network
call. Artifacts default to `~/.cache/ateles/generation_artifacts`
(override `ATELES_GENERATION_ARTIFACT_PATH`).

## Credentials

The client parses `credential_env_file` itself (default
`~/.config/ateles/generation.env`, mode 0600). It never writes the process
environment and never starts a subprocess with the key. The file must sit under
`~/.config/ateles` (override `ATELES_GENERATION_CREDENTIAL_DIR`) and be
unreadable by group and other; the key name must be one the vendor adapter is
allowed to read. That stops a poisoned binding from steering an unrelated
secret into a vendor request.

Materialize the key for the host with the private secrets tooling
(`secrets_materialize.py generation`). Creating and rotating provider keys is
operator-only.

## Refusals (`GenerationRefused`)

Branch on `.code`. Fields: `code`, `slot`, `message`, `hint`, and when known
`spent_usd`, `cap_usd`, `remaining_usd`, `vendors_tried`, `retryable`.

| Code | Meaning |
|---|---|
| `BINDING_MISSING` | no (or ambiguous, or unreadable) `vendor_binding` for the slot; unknown slot; no adapter for the vendor |
| `CAP_UNSET` | no monthly cap configured |
| `CAP_UNREADABLE` | cap, ledger, or cost estimate could not be read |
| `CAP_EXHAUSTED` | the call would exceed the slot or group budget |
| `CREDENTIAL_UNRESOLVED` | no host-held credential for the binding's route |
| `VENDOR_ERROR` | upstream failure after authorization; message says retryable or operator-fix |
| `FALLBACK_EXHAUSTED` | primary failed and the configured fallback could not serve it |
| `EMPTY_RESULT` | the vendor answered with no usable artifact; nothing was recorded |
| `CRITIQUE_ROUND_LIMIT` | recorded on a concept that hit the round limit |

The first five are raised before any vendor request.

Fallback happens only after a vendor failure or an empty result, once, and
through the fallback vendor's own binding, cap and credential. Cap, credential
and binding refusals never fall through to a different paid vendor. The result
names the vendor and model that actually ran (`vendor`, `model_tier`,
`requested_vendor`, `fallback_used`).

## Render and critique

`render_and_critique(slot, brief, criteria=..., critique_fn=..., max_rounds=3)`
generates, passes the artifact (and an SVG raster preview) to an injected
critic, and revises. `ready_for_operator_selection` becomes `True` in exactly
one place: the critic passed the concept. A failed critique, the round limit
(hard maximum 10), a refusal mid-loop, a critic error, or a missing critic all
leave it `False`. `summarize_concepts` returns an explicit `ready` list (empty
when nothing passed) and reason counts that account for every concept.

SVG previews use `rsvg-convert` or `cairosvg` if the host has one; no new
dependency is added. Without one the paid SVG is kept and the result carries a
`raster_preview_unavailable` warning.

## Persisted record

Each successful call stores a private Neotoma `generation_record`
(`generation_id`, `slot`, `prompt`, `vendor`, `model_tier`, `cost_usd`,
`artifact_ref`, `binding_entity_id`, `created_at`, `visibility`), with
idempotency key `generation-<generation_id>`, then reads it back and checks the
fields, including `visibility`. If the store fails the artifact and ledger row
are kept and the result carries a warning.

Register the schema **before** the first live call (operator-run; dry run by
default):

```bash
python3 execution/scripts/register_generation_record_schema.py          # prints the payload
python3 execution/scripts/register_generation_record_schema.py --apply  # registers and reads back
```

Field names come from `lib/capabilities/records.py`, the same source the client
writes from.

## Data handling and terms

* Prompts and artifacts may contain personal data. Keep third-party personal
  data (names, likeness references) out of prompts unless the brief needs it.
  Stored prompts are potentially personal data under the GDPR; they are private
  and operator-controlled. Nothing is sold or analysed, and nothing is
  published automatically.
* Calls send prompts to US vendors under the operator's own vendor account
  relationship. Ateles is not a processor for third parties on the operator's
  behalf in this single-operator setup. The operator must accept the current
  vendor terms and data-processing terms before placing live keys, and should
  prefer any available training opt-out on those accounts.
* Commercial use of outputs depends on those vendor terms. Ateles does not
  warrant exclusive IP ownership or trademark or likeness clearance for any
  published mark. Publication consent is a separate gate (#1163).
* Operator remains liable for charges on their vendor accounts regardless of
  the cap.

## Known limits of this change

* Recraft has no API key and no API units; its route is a subscription MCP
  session with OAuth. The adapter refuses `CREDENTIAL_UNRESOLVED` until a
  host-held OAuth adapter exists (follow-up).
* The Gemini image request shape is implemented from Google's documented
  `generateContent` shape and tested with fixtures only; it needs one live
  smoke call. The Veo shape was verified live.
* `_subscription_only_env` is still a denylist (now extended, with a prefix
  rule and a marker). Inverting it to an allowlist is a tracked follow-up.
