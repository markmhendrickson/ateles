# AAuth in Ateles

## Purpose

Documents how AAuth agent authentication is used across the Ateles repo: identity topology, signing implementations, daemon keypair status, wire format, Neotoma grant configuration, and the checklist to activate per-daemon attribution.

## Scope

Covers the AAuth-related files in this repo (`execution/scripts/mint_daemon_keypair.py`, `execution/scripts/verify_aauth_signer.py`, `lib/daemon_runtime/aauth_signer.py`, `aauth_httpsig.py` and `neotoma_signed.py`), the current activation state for each daemon, and next steps. The Cursor MCP proxy scripts and the published `.well-known/` files are described here for context but are **not present in this checkout** (see "Proxy layer" below). Does not cover Neotoma's server-side verifier implementation — see the Neotoma repo for `aauthVerify` middleware details.

---

AAuth is the agent authentication protocol Ateles uses to attribute every daemon and invocable agent's requests to a named agent (a `sub` the request claims, backed by proof of possession of a key; see "What AAuth does here" below). This document maps where AAuth is used across the repo, how each component fits together, and what still needs to be done before the full trust chain is active.

---

## What AAuth does here

AAuth solves two intertwined problems:

1. **Attribution — which agent wrote this observation?** Without AAuth, every Neotoma write comes from the operator-scoped auth, making attribution coarse-grained ("a Claude session did this"). With AAuth, a daemon that signs its requests on the RFC 9421 path with its own EC keypair is verified by Neotoma, which then records `agent_sub` (for example `anthus@ateles-swarm`, or `cursor@markmhendrickson.com` for IDE sessions) on the observations from that verified request — provenance down to the agent, not just the operator. A daemon that only sends the lighter `X-AAuth-Token` JWT (`lib/daemon_runtime/aauth_signer.py`) is **not** verified by Neotoma today (nothing in Neotoma consumes that header), so it gets no `agent_sub` from it.

2. **Authorization — what is this agent allowed to do, and to which entities and tools?** The `(sub, iss)` a verified request presents is matched against an `agent_grant` entity whose `capabilities` map declares which Neotoma operations the agent can perform. Capabilities can be scoped:
   - by **operation** (`store_structured`, `create_relationship`, `correct`, `retrieve`, …)
   - by **entity type** (`store_structured: ["agent_action_observation", "participation_record"]` instead of `*`)
   - by **field, scope, or external resource** (e.g. `github_harness:write` scoped to specific repos for Cicada/Vanellus)
   - by **MCP tool and parameter** — capability ops of the form `tool:<server>:<tool>` with an optional `param_constraints` map (e.g. `tool:btc-wallet:btc_send_transfer` with `{ "max_amount_sats": 500000 }`). Enforced at runtime by the `mcp_tool_grant_proxy` (see [Tool-level authorization](#tool-level-authorization-issue-26) and [ateles#26](https://github.com/markmhendrickson/ateles/issues/26)).

   The grant is the per-agent policy boundary. Monedula's grant lets it write `transaction` and `payment_profile` but not `agent_definition`; Cicada's lets it write `agent_action_observation` but not `business_strategy`; a future read-only auditor agent could have a grant that allows `retrieve: *` and nothing else. Wrong-capability writes fail at admission, before any side effect — the boundary lives in Neotoma, not in agent code.

So AAuth is both **who the request claims to be** (a signed, self-asserted identity) and **what that claimed identity is allowed to touch** (grant-driven capability scope). The two halves are inseparable, but signature verification does not prove who the agent is. It proves the sender holds the private key matching the public key the request itself carries (`cnf.jwk`). The `sub` and `iss` inside that request are **self-asserted**: Neotoma decodes the agent-token JWT without checking the JWT's own signature (the key is taken from the JWT's `cnf.jwk`), and matches grants on `(sub, iss)`. `match_thumbprint` on a grant is optional, and only a grant with a `match_thumbprint` pin ties an identity to a specific key. Grant admission then decides whether that claimed `(sub, iss)` may perform this specific operation on this specific entity type or call this specific tool. Today only Cursor, Cicada, and Vanellus have grants populated, and most grants use `*` rather than explicit per-entity-type allowlists — tightening this is in the to-do list below.

Neotoma's AAuth pipeline:
1. **Signature verification** — checks the RFC 9421 HTTP Message Signature
2. **Tier resolution** — ES256 software key → `tier=software`; FIDO2-attested key → `tier=hardware`
3. **Grant admission** — matches the resolved `(sub, iss)` against an `agent_grant` entity; checks requested operation against `capabilities`; gates `eligible_for_trusted_writes`

---

## Identity topology

All agent identities share one issuer (`iss = https://markmhendrickson.com`). Each distinct agent role gets its own subject and keypair.

### Three key envelopes

The repo currently has **three key envelopes** across two signing contexts (Cursor's MCP proxy, and Ateles daemons/dispatcher):

1. **JWK format** (`.creds/aauth_agent_*.private.jwk`) — used by the Cursor IDE MCP proxy, consumed by the Cursor proxy's full RFC 9421 signer (that signer is not in this checkout; the same wire format is implemented for daemons in `lib/daemon_runtime/aauth_httpsig.py`). Public keys publish to `markmhendrickson.com/.well-known/jwks.json`. ES256 P-256 only. **No provisioning script for this flavor exists on `main`** — it is not what `execution/scripts/mint_daemon_keypair.py` (below) provisions.

2. **PEM format** (`ateles-private/keys/<daemon>.json`, with `sub`, `key_id`, `algorithm`, and PEM-encoded private/public material) — used by some T3 daemons (e.g. `a2a_executor.py`, `a2a_gateway.py`) via `lib/daemon_runtime/aauth_signer.py`, which produces a lighter `X-AAuth-Token` JWT (not full RFC 9421). `docs/aauth/keys.md` calls this the "legacy" format, still supported but superseded by (3) below on next rotation. `lib/daemon_runtime/aauth_signer.py` probes `<daemon>.jwk.json` first and falls back to this `<daemon>.json`, so it reads both (2) and (3).

3. **JWK format, T3/T4 flavor (canonical)** (`ateles-private/keys/<role>.jwk.json`) — used via `lib/daemon_runtime/aauth_httpsig.py`, a full RFC 9421 signer that matches Neotoma's `aauthVerify` wire format (verified end-to-end in `execution/scripts/verify_aauth_signer.py`), and also loaded by `lib/daemon_runtime/neotoma_signed.py`'s `agent_identity()` for the dispatcher-signed gate-writeback path (see below). **`agent_identity()` loads ONLY `<agent>.jwk.json` — it has no fallback to the legacy `<agent>.json`**, so a role that has only a legacy PEM key has no identity for that path (`agent_identity()` returns None and the signed write fails closed). **Not published to JWKS, and it does not need to be for Neotoma:** an RFC 9421 request carries the public key inline (the `Signature-Key` header holds an `aa-agent+jwt` whose `cnf.jwk` binds the signing key) and Neotoma verifies against that embedded key. Neotoma does not read `ateles-private/keys`. Verification is separate from admission, which still needs an active `agent_grant`. Provisioned by `execution/scripts/mint_daemon_keypair.py --name <role>` (see below and `docs/aauth/keys.md`, the canonical doc for this format's layout and rotation).

Unifying these formats and publishing all public keys to the same JWKS is on the to-do list below.

### The dispatched child's MCP session does NOT carry gate attribution — the dispatcher signs on the lens's behalf instead

A **dispatched agent** (a review lens, or any role Apis spawns via `skill_runner`) reaches Neotoma over
**HTTP MCP**, and an MCP session authenticates **once**, with a static `Authorization` header — there is no
per-request signature for a signer to supply from inside that session. So neither keypair format above
governs what principal a dispatched child's OWN MCP writes land under, and no signing proxy sits in front of
that session. This was true before [ateles#795](https://github.com/markmhendrickson/ateles/issues/795) and
remains true after it: [ateles#1181](https://github.com/markmhendrickson/ateles/pull/1181) does not add a
per-request signer to the dispatched child's transport.

Instead, **#1181 removes the lens's own gate write entirely** and moves it to a party that CAN sign: the
Apis dispatcher process itself, acting on the lens's behalf.

- A seated review lens (or the `pm` gate in the Phase-1 additive-spec pipeline) states its verdict as a
  plain reply — a fixed-position `**SIGNED_OFF**`/`**APPROVE**` header, or a `[BLOCKING]` finding — and
  never calls `correct()` on `gate_status` itself. `mcp__mcpsrv_neotoma__correct` is removed from the
  seated lens's tool allowlist (`GATE_OWNER_DENIED_TOOLS`, enforced via `--disallowed-tools`), so an
  unsolicited attempt is not a pre-approved clearance path.
- After the dispatcher parses a CLEAN verdict, `execution/daemons/apis/swarm_dispatch.py` calls
  `execution/daemons/apis/gate_waive.py`'s `IssueGateStore.sign_off(repo, issue_number, gate, lens_agent,
  head_sha)`. `sign_off` re-reads `gate_status` fresh, then writes `gate_status.<gate> = "signed_off"`
  through `lib/daemon_runtime/neotoma_signed.py`'s `signed_request(..., sub=lens_agent)` — passing the
  **lens's own `sub`** explicitly (never the dispatcher's ambient identity, never the shared bearer) — and
  reads the write back to confirm it landed as claimed. `signed_request` resolves the lens's signing key
  via `agent_identity(lens_agent, sub=...)`, which loads `ateles-private/keys/<lens_agent>.jwk.json` — the
  SAME path `mint_daemon_keypair.py` (below) provisions. `sign_off` never falls back to the shared
  bearer on any failure (a missing key, a signing error, a non-2xx response): it returns
  `SignOffOutcome(ok=False, ...)` and the gate stays `pending` — the same fail-closed *outcome*
  `gate_writeback_identity_error` used to enforce, now produced at write time instead of launch time.
  `skill_runner.run_skill`'s launch-time call to that check is removed as part of #1181 — a gate-owning
  lens is no longer refused at DISPATCH for lacking `<ROLE>_NEOTOMA_TOKEN`, because the lens no longer
  writes the gate itself, so a missing token there no longer implies a doomed write. The functions
  `gate_writeback_identity_error` and `neotoma_token_for_agent` themselves are NOT deleted — they stay
  on `main`, deliberately, as reusable primitives for any other caller that needs to refuse a write
  attempted as the wrong principal (see the comment at `skill_runner.py`'s launch-preflight call site
  and `swarm_dispatch.review_failure_class`, both explicit that this is kept-not-dead code); they are
  simply no longer wired into the review-panel launch path.

So `<ROLE>_NEOTOMA_TOKEN` and the dispatched child's `--mcp-config` `Authorization` header remain what they
always were — a bearer credential Neotoma resolves to a human `user_id`, never to an agent `sub`, per
`src/services/mcp_auth.ts` on the Neotoma side — and they are now **entirely uninvolved** in gate
attribution. The credential that matters for a gate write is the `ateles-private/keys/<role>.jwk.json`
keypair this doc's provisioning script mints, consumed by the DISPATCHER's `sign_off` call, not by anything
the dispatched child itself presents over its own MCP session.

**Residual, tracked separately, not fixed by #1181 or this doc:** `sign_off`'s signed write still competes
with the underlying admission condition it works around — any process holding the shared bearer plus
`correct` admission on `issue` could still write `gate_status` unattributed if instructed to. #1181's fix
closes every currently-instructed path to that write, but does not add server-side enforcement that only an
AAuth-signed, authorized principal may write `gate_status.<gate>`. That is filed as
`markmhendrickson/neotoma#2485`.

### Per-agent status (ground truth, May 2026)

> This table is a **May 2026 snapshot** and has drifted: a `ls ateles-private/keys/` (names only) now shows a canonical `<name>.jwk.json` for most of the roster, sometimes alongside the legacy `<name>.json`. Treat `ls` as authoritative for what is on disk, and the grant listing (below) for what is admitted.

| Agent | `sub` | `kid` | Keypair on disk | Published in JWKS | `agent_grant` entity |
|---|---|---|---|---|---|
| Cursor IDE | `cursor@markmhendrickson.com` | `sw-cursor-1` | ✅ `.creds/aauth_agent_cursor.private.jwk` | ✅ | ✅ `ent_36b1ccf3...` |
| Apus | `apus@ateles-swarm` | `apus-edfb838b` | ✅ `ateles-private/keys/apus.json` | ❌ | ❌ |
| Formica | `formica@ateles-swarm` | `formica-f536eae6` | ✅ `ateles-private/keys/formica.json` | ❌ | ❌ |
| Cicada | `cicada@ateles-swarm` | `cicada-1534bccd` | ✅ `ateles-private/keys/cicada.json` | ❌ | ✅ `ent_8e3101e9...` (github_harness:write on ateles) |
| Monedula | `monedula@ateles-swarm` | `monedula-e128133c` | ✅ `ateles-private/keys/monedula.json` | ❌ | ❌ |
| neotoma-agent | `neotoma-agent@ateles-swarm` | `castor-c50f03d8` | ✅ `ateles-private/keys/neotoma_agent.json` | ❌ | ❌ |
| Sylvia | `sylvia@ateles-swarm` | — | ✅ `ateles-private/keys/sylvia.json` | ❌ | ❌ |
| Ateles | `ateles@ateles-swarm` | `ateles-854d78fb` | ✅ `ateles-private/keys/ateles.json` | ❌ | ❌ |
| Vanellus | `vanellus@ateles-swarm` | `vanellus-d919a64c` | ✅ `ateles-private/keys/vanellus.json` | ❌ | ✅ `ent_09762f11...` (github_harness:write on ateles+neotoma) |
| Anthus | `anthus@ateles-swarm` | — | ❌ | ❌ | ❌ |
| Tyto, Turdus, Apis | `<name>@ateles-swarm` | — | ❌ | ❌ | ❌ |
| Menura, Piculet, Strix | `<name>@ateles-swarm` | — | ❌ | ❌ | ❌ |
| YubiKey hardware tier — Cursor (planned)     | `cursor@markmhendrickson.com`  | `hw-cursor-yk-1`     | not started | not started | covered by existing cursor grant |
| YubiKey hardware tier — Operator (planned)   | `mark@markmhendrickson.com`    | `hw-operator-yk-1`   | not started | not started | new grant, full capability set |
| YubiKey hardware tier — Ateles (planned)  | `ateles@ateles-swarm`       | `hw-ateles-yk-1`  | not started | not started | upgrade existing (TBD) |
| YubiKey hardware tier — Monedula (planned)   | `monedula@ateles-swarm`        | `hw-monedula-yk-1`   | not started | not started | upgrade existing (TBD) |
| YubiKey hardware tier — Apus (planned)       | `apus@ateles-swarm`            | `hw-apus-yk-1`       | not started | not started | upgrade existing (TBD) |

### What "active" means per row

- **Keypair on disk + JWKS publish + agent_grant** → fully active, end-to-end attribution and admission. Only Cursor reaches this today. (JWKS publication is not what makes Neotoma verify: an RFC 9421 request carries its public key inline.)
- **Keypair on disk only, RFC 9421-signed** → Neotoma verifies the signature from the inline key but does not admit without a matching active `agent_grant`: "verified but unadmitted".
- **Keypair on disk only, `X-AAuth-Token` JWT only** (the `lib/daemon_runtime/aauth_signer.py` path) → Neotoma has no consumer for that header, so the request is **not verified at all**; it is attribution metadata sent alongside the operator bearer token. This is the case for the daemons recorded with legacy PEM keys (Apus, Formica, Monedula, neotoma-agent, Sylvia, Ateles).
- **Keypair + grant** → admission needs both an RFC 9421-signed request and an active matching `agent_grant`. Cicada and Vanellus have grants.
- **No keypair** → daemon falls back to stub mode (logs a warning, sends no AAuth headers, attribution defaults to operator-scoped auth).

The JWKS and `aauth-agent.json` files are served from the website, whose source is **not in this checkout** (no `execution/website/` on `main`).

---

## Files and their roles

### Signing logic

| File | Role |
|---|---|
| `lib/daemon_runtime/aauth_httpsig.py` | Full RFC 9421 signer (`HttpSigSigner`, `load_http_sig_signer`). Produces `Signature-Key`, `Signature-Input`, `Signature`, `Content-Digest` headers matching Neotoma's verifier. Loads one exact `<role>.jwk.json`. Interop-checked by `execution/scripts/verify_aauth_signer.py`. |
| `lib/daemon_runtime/neotoma_signed.py` | `agent_identity()` resolves `<agent>.jwk.json`; `signed_request()` signs through the node helper (`signed_fetch.mjs`). Used by the dispatcher's gate write-back. |
| `lib/daemon_runtime/aauth_signer.py` | Simplified signer for T3 daemons. Produces `X-AAuth-Token` (JWT-only, not full httpsig). Falls back gracefully to stub mode when no keypair file is found. |

These are distinct implementations for distinct contexts:
- `aauth_httpsig.py` / `neotoma_signed.py` implement the **full AAuth wire format** (`@hellocoop/httpsig` compatible): signs `@method @authority @path content-type content-digest signature-key`. This is what Neotoma's verifier (`src/middleware/aauth_verify.ts`) checks. They consume JWK-format keys only.
- `aauth_signer.py` implements a **lighter JWT-only path** for daemons. It consumes `ateles-private/keys/<daemon>.jwk.json` (canonical) or the legacy PEM `<daemon>.json`, probing the former first. Per the May 2026 status table above, the daemons recorded with keypairs (Apus, Formica, Monedula, Cicada, neotoma-agent, Sylvia, Ateles, Vanellus) held legacy PEM `<daemon>.json` files and attach an `X-AAuth-Token` JWT locally (which Neotoma does not verify); a daemon with neither file falls back to stub mode.

### Keys directory environment variables

Two variables name the keys directory, read by different code:

| Variable | Read by | Default |
|---|---|---|
| `ATELES_PRIVATE_KEYS_DIR` | including `mint_daemon_keypair.py`, `lib/daemon_runtime/aauth_signer.py`, `skill_runner.py`, `secrets_lib.py`, `ateles/config.py` | `<ateles>/../ateles-private/keys` |
| `ATELES_AAUTH_KEYS_DIR` | `neotoma_signed.agent_identity()` (the dispatcher's gate write-back) | `~/repos/ateles-private/keys` |

They must point at the **same directory**. If they diverge, the script mints into one place and the dispatcher looks in another, so `agent_identity()` returns None and the signed write fails closed with no obvious cause. Set both (or neither, on a host where both defaults resolve to the same path). `mint_daemon_keypair.py --keys-dir <dir>` overrides `ATELES_PRIVATE_KEYS_DIR` for one mint; if you use it, `<dir>` must be the directory the dispatcher's `ATELES_AAUTH_KEYS_DIR` names, or the dispatcher will not see the key.

### Identity provisioning

| File | Role |
|---|---|
| `execution/scripts/mint_daemon_keypair.py` | **Canonical** minting script for one T3/T4 role's ES256 P-256 keypair, written to `ateles-private/keys/<role>.jwk.json` (mode 0600, written that way from creation, no window at a looser mode) — the flavor `lib/daemon_runtime/aauth_httpsig.py` and `lib/daemon_runtime/neotoma_signed.py`'s `agent_identity()` both consume. Never prints the private scalar or public coordinates. Does **not** touch `.creds/`, `jwks.json`, or `aauth-agent.json` — those belong to the separate Cursor-proxy flavor, which has no provisioning script on `main` today. Refuses to overwrite an existing key unless `--force` is passed (rotation, which atomically replaces the key via a same-directory temp file, so a failed rotation leaves the old key intact). Creation is `O_EXCL|O_NOFOLLOW` at 0600 (a symlink at the target is refused, never followed) and the keys directory is created 0700. `--name` must match `^[a-z][a-z0-9_-]{0,63}$`. Full layout and rotation procedure: `docs/aauth/keys.md`. There is deliberately only ONE script that writes this format — see that file's module docstring for the "extend, don't parallel" rule this follows. |

**Canonical order (the same in every doc and in the script's closing hint after a first mint; after a `--force` rotation the existing grant still applies, so the hint says to re-verify and restart, and to re-register only if the sub or capabilities changed):**

1. **Mint** the key.
2. **Register** the `agent_grant` (operator).
3. **Check** the grant exists.
4. **Verify** the signer.
5. **Restart** the daemon.

```bash
# 1. Mint (or rotate with --force; read docs/aauth/keys.md before rotating).
#    --keys-dir <dir> overrides the default keys directory for this run.
python3 execution/scripts/mint_daemon_keypair.py --name <role>

# 2. As the OPERATOR (own authenticated Neotoma session, not this script and
#    not an unattended agent), register the matching agent_grant:
neotoma request --operation createAgentGrant --body '{
  "label": "<role>",
  "match_sub": "<role>@ateles-swarm",
  "match_iss": "https://markmhendrickson.com",
  "capabilities": [
    {"op": "retrieve", "entity_types": ["issue"]},
    {"op": "correct", "entity_types": ["issue"]}
  ]
}'

# 3. Confirm it exists and is active (expect one grant whose match_sub is the role's sub):
neotoma request --operation listAgentGrants \
    --query '{"q": "<role>@ateles-swarm", "status": "active"}'

# 4. Verify the key signs in a way Neotoma accepts (prints no key material):
python3 execution/scripts/verify_aauth_signer.py \
    --jwk ~/repos/ateles-private/keys/<role>.jwk.json --live <neotoma-base-url>

# 5. Restart the daemon so it picks up the new key.
```

This keypair is what `execution/daemons/apis/gate_waive.py`'s `IssueGateStore.sign_off` loads (via
`lib/daemon_runtime/neotoma_signed.py`'s `agent_identity(lens_agent, sub=...)`) to sign a gate verdict as
this role — see "The dispatched child's MCP session does NOT carry gate attribution" above for the full
mechanism. Provisioning the keypair is necessary but not sufficient: `sign_off` also needs the resolved
`sub` to be admitted server-side, which means an active `agent_grant` matching `<role>@ateles-swarm`
with the needed capabilities. `NEOTOMA_STRICT_AAUTH_SUBS`, where an instance sets it, only pins identity
(it makes the server refuse an unsigned request claiming a listed sub via `X-Agent-Label`; it does not
admit anything and does not promote a tier); without an active `agent_grant` the signed write still lands as an
unadmitted signature.

**What step 4 does and does not prove.** `verify_aauth_signer.py --live` prints only `status`,
`signature_present`, `signature_verified`, an error code and `tier`, and exits non-zero unless
`signature_verified` is true. It proves that the **Python signer** (`HttpSigSigner`, signing with `iss`
set to the role's `sub`) produces a signature Neotoma verifies with this key. It does **not** prove that
a grant matches, and it does not exercise the path the dispatcher uses: gate write-back signs through the
node helper in `signed_request`, with the issuer taken from `NEOTOMA_AAUTH_ISS` (or the Neotoma CLI's configured
issuer), not from the Python signer. Step 3 is what shows a grant exists, and the first real gate write-back is the
end-to-end proof.

### Proxy layer (Cursor IDE → Neotoma)

The Cursor MCP proxy and its full RFC 9421 signer are **not in this checkout**: `execution/scripts/aauth_signer.py`, `mcp_identity_proxy.py`, `run_neotoma_identity_proxy.sh`, `mcp_authenticated_proxy.py` and `verify_neotoma_identity_proxy.py` do not exist on `main` (`mcp-servers/IDENTITY_PROXY.md` describes the design, and `.gitleaks.toml` still allowlists the old paths). Where the proxy is deployed, it is configured with:
```
MCP_PROXY_AAUTH=1
NEOTOMA_AAUTH_SUB=cursor@markmhendrickson.com
NEOTOMA_AAUTH_ISS=https://markmhendrickson.com
NEOTOMA_AAUTH_KID=sw-cursor-1
NEOTOMA_AAUTH_AUTHORITY_OVERRIDE=neotoma.markmhendrickson.com
```
and its health check is `GET /session` returning `signature_verified: true` and `admitted: true`.

### Daemon runtime

| File | Role |
|---|---|
| `lib/daemon_runtime/aauth_signer.py` | `AAuthSigner` class. `from_key_file(agent_name)` probes `ateles-private/keys/<name>.jwk.json` first, then the legacy `<name>.json`, and returns a stub if neither exists. `headers(method, path)` returns `{"X-AAuth-Token": "<jwt>"}` or `{}` (stub). |
| `lib/daemon_runtime/agent_loader.py` | `AgentLoader` loads `agent_definition` from Neotoma, including `aauth_sub` and `agent_grant` fields. `AgentDefinition.aauth_sub` feeds the daemon's identity string. |
| `lib/daemon_runtime/__init__.py` | Re-exports `AAuthSigner`, `AgentLoader`, `SSEClient` as the daemon startup API. |

### Agent definitions (Neotoma)

Each `agent_definition` entity in Neotoma carries:
- `aauth_sub` — the agent's subject claim (e.g. `anthus@ateles-swarm`)
- `agent_grant` — capability tier: `operator` | `service` | `public_read`

Daemons load these at startup via `AgentLoader` and use `aauth_sub` as the (self-asserted) identity claimed in signed requests.

---

## Daemons using AAuth

All T3 daemons follow the same startup pattern from `lib/daemon_runtime`:

```python
from lib.daemon_runtime import AgentLoader, AAuthSigner

agent_def = AgentLoader(DAEMON_NAME).load()
# → agent_def.aauth_sub = "anthus@ateles-swarm"
# → agent_def.agent_grant = "service"

signer = AAuthSigner.from_key_file(DAEMON_NAME)
# → loads ateles-private/keys/anthus.jwk.json, else the legacy anthus.json, if present
# → returns stub signer if not (with logged warning)
```

Daemon status as recorded in May 2026 (a snapshot; the topology table above carries the same caveat, and `ls ateles-private/keys/` is authoritative for what is on disk):

| Daemon | `aauth_sub` | Keypair minted | JWKS published | Grant entity |
|---|---|---|---|---|
| Apus (mirror/webhook) | `apus@ateles-swarm` | ✅ | ❌ | ❌ |
| Formica (issue triage) | `formica@ateles-swarm` | ✅ | ❌ | ❌ |
| Monedula (payments) | `monedula@ateles-swarm` | ✅ | ❌ | ❌ |
| neotoma-agent | `neotoma-agent@ateles-swarm` | ✅ | ❌ | ❌ |
| Sylvia | `sylvia@ateles-swarm` | ✅ | ❌ | ❌ |
| Ateles (T2 operator) | `ateles@ateles-swarm` | ✅ | ❌ | ❌ |
| Cicada (T4 code worker) | `cicada@ateles-swarm` | ✅ | ❌ | ✅ (github_harness:write on ateles) |
| Vanellus (T4 PR steward) | `vanellus@ateles-swarm` | ✅ | ❌ | ✅ (github_harness:write on ateles + neotoma) |
| Anthus (orchestrator) | `anthus@ateles-swarm` | ❌ stub | ❌ | ❌ |
| Tyto, Turdus, Apis | `<name>@ateles-swarm` | ❌ stub | ❌ | ❌ |

"Stub" means the daemon runs without per-agent signing — Neotoma attributes its observations to the operator-scoped auth instead. "No JWKS publish" means the public key is not available at a well-known endpoint for other verifiers. It does not stop Neotoma verifying an RFC 9421 request, because that request carries its public key inline; Neotoma does not read `ateles-private/keys`.

Only the Cursor IDE proxy is **fully end-to-end active** (RFC 9421-signed, JWKS published, and a grant):

```
GET /session → {
  "aauth": {
    "verified": true,
    "admitted": true,
    "grant_id": "ent_36b1ccf3efe5905bd75aca3c"
  },
  "attribution": {
    "tier": "software",
    "agent_sub": "cursor@markmhendrickson.com",
    "agent_iss": "https://markmhendrickson.com"
  },
  "eligible_for_trusted_writes": true
}
```

---

## Wire format (RFC 9421 + AAuth)

When fully active, each outbound request from the Cursor proxy carries four headers:

```http
Content-Digest:  sha-256=:<base64(sha256(body))>:
Signature-Key:   aasig=jwt;jwt="<aa-agent+jwt>"
Signature-Input: aasig=("@method" "@authority" "@path" "content-type"
                         "content-digest" "signature-key");created=<unix>;
                         keyid="<jkt>";alg="ecdsa-p256-sha256"
Signature:       aasig=:<base64(ecdsa-sig)>:
```

The `aa-agent+jwt` inside `Signature-Key` carries:
```json
{
  "iss": "https://markmhendrickson.com",
  "sub": "cursor@markmhendrickson.com",
  "iat": 1714214400,
  "exp": 1714214700,
  "jkt": "<RFC7638-thumbprint>",
  "cnf": { "jwk": { /* public key inline */ } }
}
```

The `cnf.jwk` is the public key Neotoma verifies the signature against; the request carries it inline, so no JWKS fetch and no read of `ateles-private/keys` is involved.

---

## Neotoma grant entity

One `agent_grant` entity gates admission:

```
entity_id:   ent_36b1ccf3efe5905bd75aca3c
match_sub:   cursor@markmhendrickson.com
match_iss:   https://markmhendrickson.com
capabilities:
  store_structured:   *
  create_relationship: *
  correct:            *
  retrieve:           *
```

(Shown as shorthand. The stored and `createAgentGrant` shape is an array of `{"op": ..., "entity_types": [...]}` entries, as in the examples below.)

No thumbprint pin — any valid ES256 key that presents the same `(sub, iss)` is admitted, because `sub` and `iss` are self-asserted and only a `match_thumbprint` pin binds a grant to a specific key. This lets software and hardware keys rotate without updating the grant, and it also means a party holding *any* ES256 key can be admitted by claiming that `(sub, iss)`. Whether to pin grants is an operator policy call and is not changed here.

---

## Tool-level authorization (issue #26)

Entity-level grants gate Neotoma operations. **Tool-level grants** extend the
same `agent_grant` entity to gate arbitrary MCP tool calls — across any MCP
server, not just Neotoma. This is what physically stops a Monedula invocation
from calling `github_harness` tools even if that server is connected at dispatch.

### Grant shape

Tool capabilities are ordinary entries in the grant's `capabilities` array, with
an `op` of the form `tool:<server>:<tool>` and an optional `param_constraints`
map:

```jsonc
{
  "match_sub": "monedula@ateles-swarm",
  "match_iss": "https://markmhendrickson.com",
  "status": "active",
  "capabilities": [
    { "op": "store_structured", "entity_types": ["transaction", "payment_profile"] },
    { "op": "retrieve", "entity_types": ["*"] },

    { "op": "tool:parquet:read_parquet",
      "param_constraints": { "tables": ["transactions", "accounts"] } },
    { "op": "tool:btc-wallet:btc_send_transfer",
      "param_constraints": { "max_amount_sats": 500000, "to_allowlist": true } },
    { "op": "tool:btc-wallet:btc_wallet_get_balance" }
    // github_harness: explicitly absent → Monedula cannot touch GitHub
  ]
}
```

Rules:
- **Absent = denied.** A tool with no matching `tool:<server>:<tool>` entry is blocked.
- **`{}` (no `param_constraints`) = allowed, unconstrained.**
- **Wildcards:** `tool:<server>:*` grants every tool on a server; `tool:*` grants all MCP tools.

Supported `param_constraints` keys (extensible; unknown keys are ignored for
forward-compatibility): `tables`, `max_amount_sats`, `to_allowlist`,
`max_<field>`, `allowed_<field>`.

### Enforcement: `mcp_tool_grant_proxy`

`execution/mcp/mcp_tool_grant_proxy/proxy.py` is a generic stdio interceptor that
sits between `claude --print` and a downstream MCP server. It forwards all MCP
JSON-RPC traffic except `tools/call`, which it gates:

1. Reads `ATELES_AGENT_SUB` (and optional `ATELES_AGENT_GRANT_ID`) from env.
2. Loads the `agent_grant` via `lib/daemon_runtime.GrantChecker`.
3. `check_tool(server, tool)` + `check_param_constraints(constraints, args)`.
4. **Allowed** → forwards to downstream; **Denied** → returns an MCP `isError`
   result *without forwarding*, so the side-effecting tool is never reached.
5. Emits a `tool_call_observation` to Neotoma (`result: allowed | denied`) for a
   unified cross-MCP audit trail.

Launch (in `.mcp.json` or Anthus dispatch config):

```jsonc
{
  "command": "python",
  "args": [
    "execution/mcp/mcp_tool_grant_proxy/proxy.py",
    "--server-name", "parquet",
    "--", "python", "path/to/parquet_mcp/server.py"
  ]
}
```

**Permissive fallback:** if Neotoma is unreachable, or the agent has *no* grant
declaring *any* tool capability, calls pass through (advisory mode). This lets
un-migrated agents keep working while migrated agents get hard enforcement —
the boundary tightens per-agent as `tool:` capabilities are added to each grant.

For owned MCP servers (`github_harness`, `mcpsrv_neotoma`, `parquet`),
per-server enforcement (option A) can be layered in for defence-in-depth;
`github_harness` already does this for its `op`-based repo scoping.

---

## What to do next

### 1. Mint missing daemon keypairs

The dispatcher's signed gate write needs `ateles-private/keys/<role>.jwk.json` for the lens role
(`agent_identity()` reads no other file). The keypairs recorded in the status table above are legacy PEM
`<daemon>.json` files, which that path cannot use; check `ls ateles-private/keys/` for which roles
already have a `.jwk.json`, and mint the rest with:

```bash
python3 execution/scripts/mint_daemon_keypair.py --name <role>
```

This writes directly to the format `lib/daemon_runtime/aauth_httpsig.py` already loads — no PEM/JWK
conversion step is needed for this flavor. (The separate `.creds/`-based JWK flavor used by the Cursor
proxy is a different identity and has its own, currently unimplemented, provisioning path — see
"Three key envelopes" above.) Then follow the canonical order under "Identity provisioning" (mint, register the grant, check it, verify, restart).

### 2. Create `agent_grant` entities for remaining subs

Today only Cursor, Cicada, and Vanellus have grants. Apus, Formica, Monedula, neotoma-agent, and Ateles hold keys but are not admitted — Neotoma attributes their writes at operator level. Create one grant per sub, scoped to the operations that daemon needs, via the operator's own authenticated Neotoma CLI session (`agent_grant` is a protected entity type — see `docs/subsystems/aauth.md` on the Neotoma side — so it is created through the `createAgentGrant` operation, not the generic `store` verb):

```bash
neotoma request --operation createAgentGrant --body '{
  "label": "apus",
  "match_sub": "apus@ateles-swarm",
  "match_iss": "https://markmhendrickson.com",
  "capabilities": [
    {"op": "store_structured", "entity_types": ["*"]},
    {"op": "create_relationship", "entity_types": ["*"]}
  ]
}'
```

### 3. Publish daemon public keys to the JWKS endpoint

Today `https://markmhendrickson.com/.well-known/jwks.json` serves `sw-cursor-1` only. To make daemon public keys available to verifiers other than Neotoma (Neotoma itself does not need this for RFC 9421 requests), each daemon's public PEM needs to be converted to JWK form and merged into the website's `.well-known/jwks.json` (website source is not in this checkout), then the website redeployed. Subjects also need to be added to `aauth-agent.json` `subjects_supported`.

### 4. Reconcile the two keypair formats

The split between `.creds/*.jwk` and `ateles-private/keys/*.json` is incidental — both encode the same EC P-256 keypair in different envelopes. Picking one (likely JWK, since that's what the JWKS endpoint serves natively) and updating both signers to consume it would simplify the system and remove the conversion step in (3).

### 5. Tighten grants to per-entity-type capabilities (and per-tool — see [ateles#26](https://github.com/markmhendrickson/ateles/issues/26))

Today all populated grants use `*` for `store_structured` and `correct` capabilities — meaning any verified agent can write any entity type. The grant schema already supports finer-grained allowlists:

```jsonc
{
  "match_sub": "monedula@ateles-swarm",
  "match_iss": "https://markmhendrickson.com",
  "capabilities": [
    { "op": "store_structured",    "entity_types": ["transaction", "payment_profile", "daemon_report"] },
    { "op": "correct",             "entity_types": ["payment_profile"] },
    { "op": "retrieve",            "entity_types": ["transaction", "recurring_expense", "account_balance", "contact"] },
    { "op": "create_relationship", "entity_types": ["transaction->contact", "payment_profile->contact"] }
  ]
}
```

Per-agent allowlists turn the AAuth admission gate into a real policy layer: Monedula physically cannot write an `agent_definition` even if its prompt is hijacked. This is where AAuth shifts from "attribution-only" to "attribution + capability containment."

Mapping work needed:
- Per agent, list the entity types it legitimately reads (from `context_entity_types` on `agent_definition`)
- Per agent, list the entity types it legitimately writes (from `operational_entity_types`)
- Convert `*` grants to allowlists derived from those declarations
- Add Neotoma-side enforcement test cases that confirm out-of-allowlist writes fail with structured `wrong_capability` errors

The same grant entity will also carry a `tools` capability map covering MCP tool calls once [ateles#26](https://github.com/markmhendrickson/ateles/issues/26) lands — same allowlist principle, extended to `"parquet:read_parquet": { "tables": [...] }` style entries.

### 6. YubiKey hardware tier (Phase 6) — multiple agents

Hardware-attested keys produce `tier=hardware` in Neotoma rather than `tier=software`. Any agent that touches money, mutates global state, or speaks on the operator's behalf in public is a candidate. Same `(sub, iss)` as the software keypair, second `kid`, `cnf.attestation` from a WebAuthn ceremony, no grant update needed (existing grants admit any key under the matched `(sub, iss)` tuple).

Planned hardware-tier agents in priority order:

| Agent     | Why hardware                                                                                       | Suggested kid          |
| --------- | -------------------------------------------------------------------------------------------------- | ---------------------- |
| Cursor IDE | Operator's direct authoring surface — corrections, deletions, grants, schema changes               | `hw-cursor-yk-1`       |
| Operator   | New subject `mark@markmhendrickson.com` — first-party operator writes outside the IDE              | `hw-operator-yk-1`     |
| Monedula   | Touches money (Wise transfers, BTC sends) — hardware attestation raises bar for compromise         | `hw-monedula-yk-1`     |
| Ateles  | Speaks for the operator on Telegram and routes pages — public-facing identity surface              | `hw-ateles-yk-1`    |
| Apus       | Mirror pipeline that rewrites disk artifacts from Neotoma — chokepoint for behaviour propagation   | `hw-apus-yk-1`         |

For T4 invocable agents (Cicada, Vanellus, Pavo, Corvus, etc.), hardware tier is less urgent — they're scoped by `agent_grant` to specific repos/operations, and they don't run as resident services that could be compromised long-term. Software tier remains appropriate for them.

Hardware-tier rollout per agent:
1. Mint a second keypair on a YubiKey via WebAuthn ceremony for the same `(sub, iss)`
2. Publish the FIDO2 attestation alongside the public key in JWKS
3. Verify Neotoma admits with `tier=hardware`
4. Optionally: add `tier_required: hardware` to high-trust capabilities in `agent_grant` (e.g. Monedula's `store_structured: ["transaction"]` could require hardware while `retrieve: *` accepts software)

---

## Key files quick-reference

```
ateles/
├── .creds/                                ← gitignored
│   └── aauth_agent_cursor.private.jwk     ← JWK format, mode 600 (Cursor IDE only)
├── execution/
│   └── scripts/
│       ├── mint_daemon_keypair.py         ← mint a T3/T4 role's ateles-private/keys/<role>.jwk.json (canonical)
│       └── verify_aauth_signer.py         ← interop proof for lib/daemon_runtime/aauth_httpsig.py
├── lib/
│   └── daemon_runtime/
│       ├── aauth_signer.py                ← daemon signer (<name>.jwk.json or legacy PEM <name>.json, X-AAuth-Token, stub-capable)
│       ├── aauth_httpsig.py               ← full RFC 9421 signer (ateles-private/keys/<role>.jwk.json)
│       ├── neotoma_signed.py              ← agent_identity() (<role>.jwk.json only) + signed_request()
│       ├── agent_loader.py                ← loads agent_definition incl. aauth_sub
│       └── __init__.py                    ← re-exports AAuthSigner
└── execution/daemons/
    ├── anthus/anthus.py                   ← uses AAuthSigner.from_key_file("anthus") — stub if no key file
    ├── formica/formica.py                 ← uses AAuthSigner.from_key_file("formica")
    ├── apus/apus.py                       ← uses AAuthSigner.from_key_file("apus")
    └── ...                                ← same pattern in all T3 daemons

ateles-private/           ← private repo, checked out alongside ateles
└── keys/
    ├── <role>.jwk.json                    ← canonical JWK, minted by mint_daemon_keypair.py (mode 0600)
    └── <daemon>.json                      ← legacy PEM (8 recorded May 2026); still read by lib/daemon_runtime/aauth_signer.py
```

---

## Related

- [`docs/architecture.md`](architecture.md) — system layers and Neotoma integration overview
- [`execution/reports/aauth/phase4_cutover_2026-04-27.md`](../execution/reports/aauth/phase4_cutover_2026-04-27.md) — validation evidence with live Neotoma response
- AAuth spec: [aauth.fyi](https://aauth.fyi)
