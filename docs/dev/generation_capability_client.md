# Media-generation capability client

## Purpose

Developer and operator note for `lib/capabilities/` (ateles#1189): how a host
process generates a vector mark, a raster image, or a video through a
vendor-bound, spend-capped client, and why credentials never reach an agent.
This is infrastructure documentation, not marketing copy.

## Scope

In scope: the capability client, its spend cap and ledger, credential handling,
refusal codes, the render-and-critique helper, the `generation_record` schema
registration, and the first live smoke call. Out of scope: minting or rotating
provider keys (operator-only), mark concept authorship and selection (#1163),
and publication consent.

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
* `skill_runner._subscription_only_env` strips these names from the child
  environment of every agent it launches, and marks the child with
  `ATELES_DISPATCHED_AGENT=1`. The client refuses to run when it sees the
  marker. The name list lives in `lib/credential_scrub.py`, which imports
  nothing so the dispatch process never loads the client.
* **The scrub covers `skill_runner` children only.** The Formica and
  neotoma-agent daemons launch agent children with the raw daemon environment;
  that is one more reason a key must never sit in a daemon environment. Covering
  those launchers is part of the allowlist follow-up (#1351).
* Agents have no way to reach the client today. That is deliberate for this
  change and a gap for agent-driven concept work: the narrow interface that
  lets an agent request a generation belongs to #1163 / #1353, and it must carry
  every refuse code and the cap unchanged.

## Trust boundary

```
 operator shell / host script / daemon parent        dispatched agent child
 ┌──────────────────────────────────────┐            ┌────────────────────┐
 │ lib.capabilities.generate()          │            │ env scrubbed by    │
 │  binding → cap → credential → vendor │   never    │ _subscription_only │
 │  reads generation.env itself (0600)  │ ─────────▶ │ _env; marker set   │
 └──────────────────────────────────────┘            └────────────────────┘
```

Known residual risk: the key file and the spend ledger are readable and
writable by the operator's OS user, and a dispatched agent runs as that user.
The scrubbed environment and the credential-read guard hook stop the accidental
and the in-band paths; they are not OS-level isolation, and an agent could
delete or rewrite the ledger. The ledger's manifest catches a deleted or
tampered file, not a deleted directory. Running the client under a separate OS
user or service, with the ledger and key owned by it, is tracked in #1353.

## Slots and bindings

| Slot | Default vendor id | Adapter |
|---|---|---|
| `vector_mark_generation` | `recraft` | refuses `CREDENTIAL_UNRESOLVED` until a host-held OAuth route exists (#1352) |
| `image_generation` | `google_image` | Gemini image models via `generateContent` |
| `video_generation` | `veo` | Veo 3.1 via `predictLongRunning`, polled |

Slot strings are exact. Aliases (`svg_gen`, `veo`, ...) are rejected with
`BINDING_MISSING`.

A slot is filled by a `vendor_binding` entity with `capability = <slot>`
(`vendor`, `tool_namespace`, `credential_location`, `fallback`, `constraints`).
`credential_location` is a reference (a key name, or `oauth:<route>`), never a
value. `constraints` may be stored as a JSON string or as a native object; the
client parses both. Example values:

```json
{
  "model_tier": "gemini-3-pro-image",
  "draft_model_tier": "gemini-3.1-flash-lite-image",
  "monthly_cap_usd": 25,
  "cap_group": "google_generation",
  "cap_group_total_usd": 25,
  "credential_env_file": "~/.config/ateles/generation.env"
}
```

`tier=` in `generate` selects `draft`, `standard` or `lite` by reading
`<tier>_model_tier`. The client only reads bindings under the authenticated
operator's Neotoma scope and never accepts a caller-supplied entity id.

## Spend caps are configuration; the client refuses when they are not set

**To change a cap, edit `monthly_cap_usd` (and `cap_group_total_usd` for a
shared cap) in the `constraints` of the `vendor_binding` entity for that slot.**
The dollar figure is operator config in the binding, not code. The mechanism
works with any figure and **refuses when the cap is unset, unreadable, or
exhausted, without contacting the vendor**. The month is the UTC calendar
month: a new budget starts at 00:00 UTC on the 1st.

* `monthly_cap_usd` missing: `CAP_UNSET`. Unparseable constraints, a
  non-numeric or negative cap, or an unreadable or tampered ledger:
  `CAP_UNREADABLE`. Unknown is never read as zero and never as "allowed".
* Slots sharing a `cap_group` also share `cap_group_total_usd`. A call must fit
  both the slot cap and the group total.
* A call whose cost cannot be estimated (unknown model, unsupported
  resolution, non-finite estimate) is refused `CAP_UNREADABLE`, never treated as
  free.
* `spent + estimate == cap` is allowed; only `>` refuses. Money is compared in
  integer micro-dollars.
* Whoever can edit the binding can raise the cap; the cap is only as
  trustworthy as the store's write access.
* Provider-side prepaid balance with auto-reload off remains the real backstop.

### The ledger records spend before the vendor is asked

Under an exclusive lock, the client writes a `pending` row at the estimate
BEFORE any vendor request, then a final row afterwards. Row states:

| State | Meaning | Counts toward the cap |
|---|---|---|
| `pending` | written before the request; a crash leaves it here | yes |
| `completed` | artifact received, final cost recorded | yes |
| `accepted_unfinished` | the vendor may have billed but no usable artifact came back (a Veo job that started and then timed out, a failed download, an empty answer) | yes, at the estimate |
| `voided` | the vendor definitively did not bill (a 4xx before any job existed, or a connection that never reached it) | no |

Only a definitive no-charge outcome frees the estimate. A held call is not
retryable, and the fallback vendor is not tried, because a retry can bill
again. If an operator confirms a held call was not billed, they can change that
row's `state` to `voided` in the ledger file by hand.

The ledger is local and operator-owned:
`$XDG_STATE_HOME/ateles/generation_spend/<slot>/<YYYY-MM>.jsonl` (default
`~/.local/state/ateles/...`; override `ATELES_GENERATION_SPEND_PATH`),
directories 0700, files 0600, opened without following symlinks, with a
`manifest.json` listing every ledger file. A file the manifest lists that is now
missing, or ledger files with no manifest, refuse `CAP_UNREADABLE`: a deleted
ledger must not read as zero spend. Later rows for the same `generation_id`
supersede earlier ones. Artifacts default to
`~/.local/state/ateles/generation_artifacts` (`ATELES_GENERATION_ARTIFACT_PATH`).
The ledger makes no network call.

Recorded cost is the list-price estimate (an upper bound); vendors return no
billed amount. Reconcile against the provider's invoice.

## Credentials

The client parses the credential file named by the binding's
`constraints.credential_env_file` (default location
`~/.config/ateles/generation.env`, mode 0600) itself. That file is the **only**
source: the client does not fall back to its own process environment, so there
is one place a key can live. It never writes the process environment and never
starts a subprocess with the key. The file must sit under `~/.config/ateles`
(override `ATELES_GENERATION_CREDENTIAL_DIR`) and be unreadable by group and
other; the key name must be one the vendor adapter is allowed to read. That
stops a poisoned binding from steering an unrelated secret into a vendor
request. Refusals name the key name and the file path (never the value) and give
the `chmod 600 <path>` command.

Materialize the key for the host from the private secrets snapshot:
`python3 execution/scripts/secrets_materialize.py generation` (the `generation`
entry is defined in the private secrets manifest, not in this repo; see
`docs/secrets_management.md`). Creating and rotating provider keys is
operator-only.

## First live smoke call (operator session, after merge)

1. **Register the schema** (writes to the Neotoma instance you point at; safe to
   re-run):

   ```bash
   set -a; source <your Neotoma env file>; set +a     # provides the two variables below; do not echo them
   export NEOTOMA_BASE_URL=<https URL of your Neotoma instance>
   export NEOTOMA_BEARER_TOKEN=<that instance's token, from the sourced file>
   python3 execution/scripts/register_generation_record_schema.py            # dry run: prints the payload
   python3 execution/scripts/register_generation_record_schema.py --apply
   ```

   Expected last line: `registered and verified: generation_record v1.0`.
   The exports above only name variables the sourced file already defines; the
   file that holds your Neotoma token must NOT be the file that holds the
   generation key.

2. **Check the key after materializing it** (count and mode only; never cat it):

   ```bash
   ls -l ~/.config/ateles/generation.env            # expect -rw-------
   grep -c '^GEMINI_API_KEY' ~/.config/ateles/generation.env   # expect 1
   ```

3. **One cheap silent clip** (Veo Lite, 4 seconds, 1080p, about $0.32). The
   client process needs `NEOTOMA_BASE_URL` and `NEOTOMA_BEARER_TOKEN` (from
   step 1) to read the binding and store the record; it needs no other variable.
   Run from the repository root so `lib` imports:

   ```bash
   python3 - <<'PY'
   from lib.capabilities import generate
   r = generate("video_generation", "A slow dolly across an empty wooden desk, soft daylight",
                tier="lite", duration_seconds=4, resolution="1080p")
   print(r.cost_usd, r.remaining_cap_usd, r.vendor, r.model_tier, r.record_persisted, r.warnings)
   print(r.artifact_ref)
   PY
   ```

   Good output: `0.32 <cap minus 0.32> veo veo-3.1-lite-generate-preview True ()`
   and a path ending in `.mp4`. Then confirm the clip is silent:
   `ffprobe -v error -show_entries stream=codec_type -of csv=p=0 <path>` prints
   only `video`. `record_persisted False` with a warning means the schema is not
   registered or the store rejected the record; the clip and ledger rows are
   kept.

4. For the image path, run the same snippet with
   `generate("image_generation", "...", tier="draft")` once; the image request
   shape has not been exercised live yet.

If a call is refused, the message names the cause and the hint says whether
money may have moved. A missing token or base URL surfaces as
`BINDING_MISSING` whose hint says it is a client configuration problem, not a
missing binding.

## Refusals (`GenerationRefused`)

Branch on `.code`. Fields: `code`, `slot`, `message`, `hint`, and when known
`spent_usd`, `cap_usd`, `remaining_usd`, `vendors_tried`, `retryable`,
`generation_id`, `artifact_ref`.

| Code | Meaning | Money moved? |
|---|---|---|
| `BINDING_MISSING` | no (or ambiguous) `vendor_binding` for the slot; unknown slot; no adapter for the vendor; the binding store or token is unreachable or rejected | no |
| `CAP_UNSET` | no monthly cap configured | no |
| `CAP_UNREADABLE` | cap, ledger, or cost estimate could not be read | no |
| `CAP_EXHAUSTED` | the call would exceed the slot or group budget (pending and held rows count) | no |
| `CREDENTIAL_UNRESOLVED` | no host-held credential for the binding's route | no |
| `VENDOR_ERROR` | `retryable=True`: the vendor definitively did not bill and the failure is transient. `retryable=False` with a `held` message: the vendor may have billed; the estimate stays counted; do not retry blindly | maybe |
| `FALLBACK_EXHAUSTED` | the primary did not bill and the configured fallback could not serve it | maybe (see message) |
| `EMPTY_RESULT` | the vendor answered with no usable artifact; the estimate stays held | maybe |
| `CRITIQUE_ROUND_LIMIT` | recorded on a concept that hit the round limit | n/a |
| `SPENT_UNRECORDED` | a paid call whose final ledger row could not be written; the pending row still counts. Names the saved artifact and `generation_id`. **Do not retry** | yes |
| `ARTIFACT_UNSAVED` | a paid call whose spend is recorded but whose bytes could not be written to disk. **Do not retry** | yes |

The first five are raised before any vendor request. `SPENT_UNRECORDED` and
`ARTIFACT_UNSAVED` are the post-spend codes (`errors.POST_SPEND_CODES`).

Fallback happens only when the primary DEFINITIVELY did not bill, once, and
through the fallback vendor's own binding, cap and credential. Cap, credential
and binding refusals never fall through to a different paid vendor, and a held
(possibly billed) primary never falls back. The result names the vendor and
model that actually ran (`vendor`, `model_tier`, `requested_vendor`,
`fallback_used`).

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
`artifact_ref`, `binding_entity_id`, `created_at`, `requested_vendor`,
`fallback_used`, `billing_slot`, `cap_group`, `remaining_cap_usd`,
`visibility`), with idempotency key `generation-<generation_id>`, then reads it
back and checks the fields, including `visibility`. If the store fails the
artifact and ledger rows are kept and the result carries a warning that points
at schema registration.

Field names come from `lib/capabilities/records.py`, the same source the schema
script registers from.

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
  host-held OAuth adapter exists (#1352).
* The Gemini image request shape is implemented from Google's documented
  `generateContent` shape and tested with fixtures only; it needs one live
  smoke call. The Veo shape was verified live.
* `_subscription_only_env` is still a denylist (now extended, with a prefix
  rule and a marker), and covers `skill_runner` children only. Inverting it to
  an allowlist and covering the other launchers is #1351.
* The key file, ledger and marker are protected against accident, not against a
  hostile process running as the operator's user (#1353).
* Generation is serialized per ledger root: the lock is held across the vendor
  call (default lock timeout 1200s, `ATELES_GENERATION_LOCK_TIMEOUT_S`).
