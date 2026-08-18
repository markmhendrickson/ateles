# AAuth in Ateles

## Purpose

Documents how AAuth agent authentication is used across the Ateles repo: which
parts of the protocol we implement, where the signing code lives, the current
per-agent activation state, and what conformance work remains.

## Scope

Covers the AAuth code in this repo (`lib/daemon_runtime/aauth_*.py`,
`lib/daemon_runtime/grant_checker.py`, `execution/scripts/mint_daemon_keypair.py`,
the MCP tool-grant proxy) and the published `.well-known/` endpoints. Does not
cover Neotoma's server-side verifier — see the Neotoma repo for `aauthVerify`.

**Spec version tracked: `draft-hardt-oauth-aauth-protocol-10` (6 August 2026).**
Home: [aauth.dev](https://www.aauth.dev/). Note the draft was renamed — the
earlier `draft-hardt-aauth-protocol` series expired at -02 and was replaced by
the `-oauth-` series, which resets the numbering. It is an individual
Internet-Draft with no IETF standing, revising roughly monthly; pin to a
version and re-check rather than chasing head.

---

## What we implement, and what we don't

AAuth as specified is a large protocol: five resource access modes, four token
types, an orthogonal agent-governance layer, and multi-hop call chaining.

**Ateles implements the first access mode only — identity-based access.** An
agent signs each request with its own key; the resource (Neotoma) verifies the
signature and applies its own policy. Everything else in our stack — which
entity types an agent may write, which MCP tools it may call — is *our* policy
layer (`agent_grant`), not the spec's.

| Spec capability | Status here |
|---|---|
| Identity-based access (agent token + HTTP signature) | ✅ implemented |
| Resource-managed / PS-asserted / federated access | ❌ not implemented |
| Person Server, auth tokens, resource tokens | ❌ none; we have no PS |
| Missions, clarification chat, mission log | ❌ not implemented (see below) |
| Sub-agents (`parent_agent`, `subagent_token`) | ◐ identifiers only, no token flow |
| Call chaining | ❌ not implemented |
| `AAuth-Requirement` / `AAuth-Access` / `AAuth-Capabilities` headers | ❌ not parsed or emitted |
| R3 per-call tool authorization | ❌ our `mcp_tool_grant_proxy` is a local analogue |

Two of our homegrown mechanisms have standard counterparts we deliberately have
not adopted yet:

- **`agent_grant` ≈ PS-managed permission + audit.** The spec puts per-action
  permission and audit at a person server the operator chooses. Ours lives in
  Neotoma as an entity.
- **`execution_policy` ≈ missions.** A mission is a scoped authorization
  context with a Markdown intent description, an approver, an `s256` identity
  hash, and an append-only mission log. That is close to what `execution_policy`
  does for swarm dispatch.

Converging on the standard would mean giving up control of that policy surface
to a PS; keeping ours means staying non-standard on the governance half. That
is an open decision, not an oversight.

---

## What AAuth does here

1. **Attribution — which agent wrote this observation?** Each daemon signs with
   its own keypair, so Neotoma records `agent_sub` per observation rather than
   collapsing everything to the operator's bearer token.

2. **Authorization — what may this agent touch?** Each verified `(sub, iss)` is
   matched against an `agent_grant` whose `capabilities` array declares the
   permitted operations, scoped by operation, entity type, and — via
   `tool:<server>:<tool>` entries — by MCP tool and parameter.

Neotoma's pipeline: signature verification → tier resolution (`software` for a
plain key, `hardware` for FIDO2-attested) → grant admission.

---

## Wire format

Label `aasig`, RFC 9421 HTTP Message Signatures:

```http
Content-Digest:  sha-256=:<std-b64(sha256(body))>:
Signature-Key:   aasig=jwt;jwt="<aa-agent+jwt>"
Signature-Input: aasig=("@method" "@authority" "@path" "content-type"
                        "content-digest" "signature-key");created=<unix>
Signature:       aasig=:<std-b64(raw signature)>:
```

Draft-10 §12.8.3.1 mandates exactly four covered components — `@method`,
`@authority`, `@path`, `signature-key` — each closing a request-substitution
attack. We always cover those, and add `content-type` + `content-digest` on
requests with a body, which the spec permits (servers MAY require extras).

**Not yet handled:** a server that demands additional covered components
replies `invalid_input` with a `required_input` list. We do not parse that
response, so we would fail rather than retry with the wider component set.

### Agent token claims (§5.2.2)

```json
{
  "iss": "https://markmhendrickson.com",
  "dwk": "aauth-agent.json",
  "sub": "anthus@ateles-swarm",
  "jti": "<128-bit base64url>",
  "cnf": { "jwk": { "kty": "OKP", "crv": "Ed25519", "alg": "Ed25519", "x": "…" } },
  "iat": 1787068036,
  "exp": 1787068336,
  "jkt": "<RFC 7638 thumbprint>"
}
```

`iss`, `dwk`, `sub`, `jti`, `cnf`, `iat`, `exp` are all REQUIRED. `dwk` is the
key-discovery pointer — a verifier resolves `{iss}/.well-known/{dwk}` and
selects the key matching the JWS header `kid`.

`jkt` is **not** part of draft-10; it predates the current draft and Neotoma's
deployed verifier still reads it, so we keep emitting it (`include_jkt=True`).
Drop it once Neotoma no longer needs it.

`cnf.jwk` carries the public key inline, which lets a verifier check the
signature without a JWKS fetch. We strip key-file bookkeeping (`sub`, etc.) so
only real JWK members ride along, and always set a fully-specified `alg` —
§12.8.1 requires a verifier to **reject a key whose `alg` is absent**.

### Algorithms (§12.8.1)

Agents and resources **MUST support Ed25519**; ES256 is only a SHOULD. Newly
minted keys are Ed25519 by default; `--alg ES256` remains available for
verifiers that do not yet accept Ed25519.

Also enforced: the polymorphic `EdDSA` identifier MUST NOT be used (RFC 9864
deprecated it in favour of `Ed25519`), and a key whose `kty`/`crv` disagrees
with its `alg` is rejected at signing time rather than emitted for a verifier
to reject.

> **Implementation note.** PyJWT 2.13 registers Ed25519 only under the
> deprecated `EdDSA` name. Since the protected header is part of the JWS
> signing input, it must already say `Ed25519` when the signature is computed —
> rewriting it afterwards invalidates the token. Those tokens are therefore
> assembled and signed directly rather than through `pyjwt.encode`.

---

## Agent identifiers (§5.1)

An identifier is a URI: `aauth:local@domain`, where `domain` is the **agent
provider's** domain (for us, `markmhendrickson.com`, derived from `iss`). The
local part allows `a-z 0-9 - _ + .`, must be non-empty, and is capped at 255
characters. `+` is reserved as the sub-agent delimiter, so a top-level agent's
local part must not contain one. Comparison is exact and case-sensitive.

Our historical subjects — `anthus@ateles-swarm` — are invalid twice over: no
`aauth:` scheme, and `ateles-swarm` is not a domain.

**The migration is gated and currently OFF.** Neotoma admits an agent by
matching the presented `sub` against `agent_grant.match_sub`, and 25 of the 28
live grants still carry the legacy value. Emitting the new form before those
grants are migrated would fail admission for every daemon simultaneously.

```bash
# Off by default; opt in per environment once grants are migrated.
ATELES_AAUTH_SPEC_IDENTIFIERS=1
```

Migration order:

1. Add draft-10 `match_sub` values to the 25 legacy grants (dual-match during
   the transition, if Neotoma's verifier supports it).
2. Set `ATELES_AAUTH_SPEC_IDENTIFIERS=1` and confirm admission still succeeds.
3. Retire the legacy `match_sub` values.

### Sub-agents (§10.2)

`aauth:parent+discriminator@domain`, single level only — a sub-agent cannot
have its own sub-agents. `lib/daemon_runtime/aauth_identifier.py` builds and
validates these, but the *authorization* flow (a parent obtaining tokens via
`subagent_token`) is not implemented; this maps onto our T3→T4 dispatch
(Anthus invoking Cicada) if we adopt it.

Per the spec, the local part is for operational readability only — parties MUST
NOT parse it for protocol decisions. The `parent_agent` claim is authoritative,
and we do not emit it yet.

---

## Files and their roles

| File | Role |
|---|---|
| `lib/daemon_runtime/aauth_httpsig.py` | RFC 9421 signer: builds the signature base, mints the `aa-agent+jwt`, handles Ed25519 + ES256. |
| `lib/daemon_runtime/aauth_signer.py` | `AAuthSigner` — loads `ateles-private/keys/<name>.jwk.json` (or legacy PEM) and returns headers; falls back to a logged stub when no key exists. |
| `lib/daemon_runtime/aauth_identifier.py` | Builds, validates, and normalizes agent identifiers; owns the migration flag. |
| `lib/daemon_runtime/grant_checker.py` | Loads `agent_grant` from Neotoma; checks operations, entity types, and tool/param constraints. |
| `execution/scripts/mint_daemon_keypair.py` | Mints a keypair (Ed25519 default, `--alg ES256` available) into `ateles-private/keys/`, mode 0600. |
| `execution/scripts/verify_aauth_signer.py` | Signer smoke test. |
| `execution/mcp/mcp_tool_grant_proxy/proxy.py` | stdio MCP interceptor gating `tools/call` against the grant. |
| `docs/aauth/keys.md` | Key layout and rotation. |

Published endpoints (served from the website repo, not this one):

- `https://markmhendrickson.com/.well-known/aauth-agent.json` — agent metadata
- `https://markmhendrickson.com/.well-known/jwks.json` — public keys

---

## Per-agent status

26 agents have a keypair in `ateles-private/keys/`: anthus, apis, apus, aquila,
buteo, cicada, corvus, cotinga, cyphorhinus, formica, fringilla, gorilla,
gryllus, lanius, monedula, neotoma-agent, onychomys, pavo, phoenicurus, picus,
sturnus, sylvia, turdus, tyto, vanellus, waxwing. Several also have a legacy
PEM-format file alongside the canonical `.jwk.json`.

**All existing keys are ES256, and only two (apus, buteo) carry an `alg`
member.** Our signer backfills `alg` into `cnf.jwk` at signing time, so emitted
tokens are conformant; but the key *files* are pre-draft-10, and anything that
publishes them to a JWKS must add `alg` — §12.8.1 makes a JWKS key without
`alg` unusable.

| Surface | Keypair | Published in JWKS | `agent_grant` |
|---|---|---|---|
| Cursor IDE (`cursor@markmhendrickson.com`) | ✅ | ✅ `sw-cursor-1` | ✅ `ent_36b1ccf3…` |
| 26 swarm daemons/agents | ✅ | ❌ | ◐ 25 grants, legacy `match_sub` |
| air.local laptop | ✅ | ❌ | ✅ `ent_9e3edbfd…` (thumbprint-pinned) |

Grants and keypairs do not line up one-to-one: some agents have a key but no
grant, and one grant (`apus@ateles-swarm` against `https://neotoma.cursor.local`)
is revoked. Reconciling the two lists is part of the migration below.

Only the Cursor IDE identity is end-to-end published. The JWKS currently serves
one key, so external verifiers cannot check daemon signatures; Neotoma can
because it resolves keys in-network.

---

## What to do next

1. **Publish daemon public keys to JWKS.** Convert each public key to JWK form
   **with `alg`**, merge into the website's `jwks.json`, and extend
   `subjects_supported` in `aauth-agent.json`.

2. **Migrate identifiers.** Follow the three-step order above. This is the
   prerequisite for turning on `ATELES_AAUTH_SPEC_IDENTIFIERS`.

3. **Re-mint on Ed25519.** New keys default to it; the 24 existing ES256 keys
   still satisfy the SHOULD, so this is rotation-at-leisure rather than urgent.
   Ed25519 is the MUST that makes us interoperable with resources that only
   implement the required algorithm.

4. **Handle `required_input`.** Parse the `invalid_input` error and retry with
   the server's requested covered components instead of failing.

5. **Decide on the person-server model.** Dick Hardt is rolling out a live PS at
   `person.hello.coop`, which makes evaluating missions and PS-asserted access
   concrete rather than theoretical. The decision is whether to converge
   `agent_grant`/`execution_policy` onto the standard governance layer or keep
   ours and document the divergence.

6. **Tighten grants.** Several still use `*` for `store_structured`/`correct`.
   Per-entity-type allowlists turn admission into real containment.

7. **Hardware tier.** Unchanged from before: FIDO2-attested keys yield
   `tier=hardware`. Priority order is Cursor, operator, Monedula (money),
   Ateles (speaks publicly), Apus (rewrites disk artifacts).

---

## Related

- Spec: [aauth.dev](https://www.aauth.dev/) ·
  [draft-10](https://www.ietf.org/archive/id/draft-hardt-oauth-aauth-protocol-10.html) ·
  [datatracker](https://datatracker.ietf.org/doc/draft-hardt-oauth-aauth-protocol/)
- Companion drafts, not yet evaluated: AAuth Bootstrapping (enrollment, key
  refresh), AAuth R3 (per-call tool authorization), AAuth Events.
- [`docs/aauth/keys.md`](aauth/keys.md) — key layout and rotation
- [`docs/architecture.md`](architecture.md) — system layers
