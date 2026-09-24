# AAuth in Ateles

## Purpose

Documents how AAuth agent authentication is used across the Ateles repo: identity topology, signing implementations, daemon keypair status, wire format, Neotoma grant configuration, and the checklist to activate per-daemon attribution.

## Scope

Covers all AAuth-related files in the repo (`execution/scripts/aauth_*.py`, `lib/daemon_runtime/aauth_signer.py`, the MCP proxy layer, and the published `.well-known/` endpoints), the current activation state for each daemon, and next steps. Does not cover Neotoma's server-side verifier implementation — see the Neotoma repo for `aauthVerify` middleware details.

---

AAuth is the agent authentication protocol Ateles uses to give every daemon and invocable agent a verifiable identity. This document maps where AAuth is used across the repo, how each component fits together, and what still needs to be done before the full trust chain is active.

---

## What AAuth does here

AAuth solves two intertwined problems:

1. **Attribution — which agent wrote this observation?** Without AAuth, every Neotoma write comes from the operator-scoped auth, making attribution coarse-grained ("a Claude session did this"). With AAuth, each daemon signs its requests with its own EC keypair, so Neotoma records `agent_sub: anthus@ateles-swarm` (or `cursor@markmhendrickson.com` for IDE sessions) on every observation — provenance down to the agent, not just the operator.

2. **Authorization — what is this agent allowed to do, and to which entities and tools?** Each verified `(sub, iss)` is matched against an `agent_grant` entity whose `capabilities` map declares which Neotoma operations the agent can perform. Capabilities can be scoped:
   - by **operation** (`store_structured`, `create_relationship`, `correct`, `retrieve`, …)
   - by **entity type** (`store_structured: ["agent_action_observation", "participation_record"]` instead of `*`)
   - by **field, scope, or external resource** (e.g. `github_harness:write` scoped to specific repos for Cicada/Vanellus)
   - by **MCP tool and parameter** — capability ops of the form `tool:<server>:<tool>` with an optional `param_constraints` map (e.g. `tool:btc-wallet:btc_send_transfer` with `{ "max_amount_sats": 500000 }`). Enforced at runtime by the `mcp_tool_grant_proxy` (see [Tool-level authorization](#tool-level-authorization-issue-26) and [ateles#26](https://github.com/markmhendrickson/ateles/issues/26)).

   The grant is the per-agent policy boundary. Monedula's grant lets it write `transaction` and `payment_profile` but not `agent_definition`; Cicada's lets it write `agent_action_observation` but not `business_strategy`; a future read-only auditor agent could have a grant that allows `retrieve: *` and nothing else. Wrong-capability writes fail at admission, before any side effect — the boundary lives in Neotoma, not in agent code.

So AAuth is both **who** (signed identity) and **what they're allowed to touch** (grant-driven capability scope). The two halves are inseparable: signature verification proves who the agent is; grant admission decides whether that agent is allowed to perform this specific operation on this specific entity type or call this specific tool. Today only Cursor, Cicada, and Vanellus have grants populated, and most grants use `*` rather than explicit per-entity-type allowlists — tightening this is in the to-do list below.

Neotoma's AAuth pipeline:
1. **Signature verification** — checks the RFC 9421 HTTP Message Signature
2. **Tier resolution** — ES256 software key → `tier=software`; FIDO2-attested key → `tier=hardware`
3. **Grant admission** — matches the resolved `(sub, iss)` against an `agent_grant` entity; checks requested operation against `capabilities`; gates `eligible_for_trusted_writes`

---

## Identity topology

All agent identities share one issuer (`iss = https://markmhendrickson.com`). Each distinct agent role gets its own subject and keypair.

### Two identity flavors

The repo currently maintains **two parallel keypair formats** for two contexts:

1. **JWK format** (`.creds/aauth_agent_*.private.jwk`) — used by the Cursor IDE MCP proxy, consumed by the full RFC 9421 signer (`execution/scripts/aauth_signer.py`). Public keys publish to `markmhendrickson.com/.well-known/jwks.json`. ES256 P-256 only. **No provisioning script for this flavor exists on `main`** — it is not what `execution/scripts/aauth_provision_identity.py` (below) provisions.

2. **PEM format** (`ateles-private/keys/<daemon>.json`, with `sub`, `key_id`, `algorithm`, and PEM-encoded private/public material) — used by some T3 daemons (e.g. `a2a_executor.py`, `a2a_gateway.py`) via `lib/daemon_runtime/aauth_signer.py`, which produces a lighter `X-AAuth-Token` JWT (not full RFC 9421).

3. **JWK format, T3/T4 flavor** (`ateles-private/keys/<role>.jwk.json`) — used via `lib/daemon_runtime/aauth_httpsig.py`, a full RFC 9421 signer that matches Neotoma's `aauthVerify` wire format (verified end-to-end in `execution/scripts/verify_aauth_signer.py`). **Not yet published to JWKS** — only Neotoma can verify these today (via local key resolution or because the daemon talks to Neotoma over a trusted connection). Provisioned by `execution/scripts/aauth_provision_identity.py --role <role>` (see below).

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
  SAME path `aauth_provision_identity.py` (below) provisions. `sign_off` never falls back to the shared
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

| Agent | `sub` | `kid` | Keypair on disk | Published in JWKS | `agent_grant` entity |
|---|---|---|---|---|---|
| Cursor IDE | `cursor@markmhendrickson.com` | `sw-cursor-1` | ✅ `.creds/aauth_agent_cursor.private.jwk` | ✅ | ✅ `ent_36b1ccf3...` |
| Apus | `apus@ateles-swarm` | `apus-edfb838b` | ✅ `ateles-private/keys/apus.json` | ❌ | ❌ |
| Formica | `formica@ateles-swarm` | `formica-f536eae6` | ✅ `ateles-private/keys/formica.json` | ❌ | ❌ |
| Cicada | `cicada@ateles-swarm` | `cicada-1534bccd` | ✅ `ateles-private/keys/cicada.json` | ❌ | ✅ `ent_8e3101e9...` (github_harness:write on ateles) |
| Monedula | `monedula@ateles-swarm` | `monedula-e128133c` | ✅ `ateles-private/keys/monedula.json` | ❌ | ❌ |
| neotoma-agent | `neotoma-agent@ateles-swarm` | `castor-c50f03d8` | ✅ `ateles-private/keys/neotoma_agent.json` | ❌ | ❌ |
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

- **Keypair on disk + JWKS publish + agent_grant** → fully active, end-to-end attribution and admission. Only Cursor reaches this today.
- **Keypair on disk only** → daemon can mint AAuth JWTs locally, but external verifiers can't fetch the public key, and Neotoma will verify but won't admit unless a grant matches. Effectively "signs but unadmitted." This covers Apus, Formica, Monedula, neotoma-agent, Ateles.
- **Keypair + grant, no JWKS publish** → Cicada and Vanellus can be admitted by Neotoma for github_harness writes, but only over the local network where Neotoma already has the key. Publishing to JWKS would extend trust to any AAuth resource.
- **No keypair** → daemon falls back to stub mode (logs a warning, sends no AAuth headers, attribution defaults to operator-scoped auth).

Source for the JWKS file in repo: `execution/website/markmhendrickson/react-app/public/.well-known/`

---

## Files and their roles

### Signing logic

| File | Role |
|---|---|
| `execution/scripts/aauth_signer.py` | Full RFC 9421 signer. Produces `Signature-Key`, `Signature-Input`, `Signature`, `Content-Digest` headers. Used by the Cursor MCP proxy. |
| `lib/daemon_runtime/aauth_signer.py` | Simplified signer for T3 daemons. Produces `X-AAuth-Token` (JWT-only, not full httpsig). Falls back gracefully to stub mode when keypair is absent. |

These are two distinct implementations for two distinct contexts:
- `execution/scripts/aauth_signer.py` — implements the **full AAuth wire format** (`@hellocoop/httpsig` compatible): signs `@method @authority @path content-type content-digest signature-key`. This is what Neotoma's verifier expects from an external MCP client. Consumes JWK-format keys.
- `lib/daemon_runtime/aauth_signer.py` — implements a **lighter JWT-only path** for daemons. Consumes PEM-format keys from `ateles-private/keys/<daemon>.json`. Today the daemons that have keypairs (Apus, Formica, Monedula, Cicada, neotoma-agent, Ateles, Vanellus) sign locally; daemons without keypairs (Anthus, Tyto, Turdus, Apis) fall back to stub mode.

### Identity provisioning

| File | Role |
|---|---|
| `execution/scripts/aauth_provision_identity.py` | Generates an ES256 P-256 keypair for one **T3/T4 role** and writes the private JWK to `ateles-private/keys/<role>.jwk.json` (mode 0600) — the flavor `lib/daemon_runtime/aauth_httpsig.py` consumes. Never prints the private key. Does **not** touch `.creds/`, `jwks.json`, or `aauth-agent.json` — those belong to the separate Cursor-proxy flavor, which has no provisioning script on `main` today. Run once per new agent role, or with `--force` to rotate. |

Usage:
```bash
# Provision a new agent identity (or rotate with --force):
python3 execution/scripts/aauth_provision_identity.py --role accipiter

# Then, as the OPERATOR (own authenticated Neotoma session — not this script,
# not an unattended agent), register the matching agent_grant:
neotoma request --operation createAgentGrant --body '{
  "label": "accipiter",
  "match_sub": "accipiter@ateles-swarm",
  "match_iss": "https://markmhendrickson.com",
  "capabilities": [
    {"op": "retrieve", "entity_types": ["issue"]},
    {"op": "correct", "entity_types": ["issue"]}
  ]
}'
```

This keypair is what `execution/daemons/apis/gate_waive.py`'s `IssueGateStore.sign_off` loads (via
`lib/daemon_runtime/neotoma_signed.py`'s `agent_identity(lens_agent, sub=...)`) to sign a gate verdict as
this role — see "The dispatched child's MCP session does NOT carry gate attribution" above for the full
mechanism. Provisioning the keypair is necessary but not sufficient: `sign_off` also needs the resolved
`sub` to be admitted server-side (an `agent_grant` matching `<role>@ateles-swarm`, or the role's `sub`
present in `NEOTOMA_STRICT_AAUTH_SUBS` if that's how the target instance is configured) for the signed
write to land as anything other than an unadmitted signature.

### Proxy layer (Cursor IDE → Neotoma)

| File | Role |
|---|---|
| `execution/scripts/mcp_identity_proxy.py` | `stdio` MCP proxy between Cursor and Neotoma. With `--aauth` / `MCP_PROXY_AAUTH=1`, calls `aauth_signer.py` to add signature headers to every forwarded request. |
| `execution/scripts/run_neotoma_identity_proxy.sh` | Launcher that prefers the local venv, dependency-checks AAuth libs, and passes `--aauth`. |
| `execution/scripts/mcp_authenticated_proxy.py` | Alternate proxy variant with OAuth support. |
| `execution/scripts/verify_neotoma_identity_proxy.py` | Smoke-tests the proxy end-to-end: checks that `GET /session` returns `signature_verified: true` and `admitted: true`. |

Cursor picks up the proxy via `.cursor/mcp.json` → `neotoma-proxy` server entry, which sets:
```
MCP_PROXY_AAUTH=1
NEOTOMA_AAUTH_SUB=cursor@markmhendrickson.com
NEOTOMA_AAUTH_ISS=https://markmhendrickson.com
NEOTOMA_AAUTH_KID=sw-cursor-1
NEOTOMA_AAUTH_AUTHORITY_OVERRIDE=neotoma.markmhendrickson.com
```

### Daemon runtime

| File | Role |
|---|---|
| `lib/daemon_runtime/aauth_signer.py` | `AAuthSigner` class. `from_key_file(agent_name)` loads the keypair from `ateles-private/keys/<name>.json`. `headers(method, path)` returns `{"X-AAuth-Token": "<jwt>"}` or `{}` (stub). |
| `lib/daemon_runtime/agent_loader.py` | `AgentLoader` loads `agent_definition` from Neotoma, including `aauth_sub` and `agent_grant` fields. `AgentDefinition.aauth_sub` feeds the daemon's identity string. |
| `lib/daemon_runtime/__init__.py` | Re-exports `AAuthSigner`, `AgentLoader`, `SSEClient` as the daemon startup API. |

### Agent definitions (Neotoma)

Each `agent_definition` entity in Neotoma carries:
- `aauth_sub` — the agent's subject claim (e.g. `anthus@ateles-swarm`)
- `agent_grant` — capability tier: `operator` | `service` | `public_read`

Daemons load these at startup via `AgentLoader` and use `aauth_sub` as the identity in signed requests.

---

## Daemons using AAuth

All T3 daemons follow the same startup pattern from `lib/daemon_runtime`:

```python
from lib.daemon_runtime import AgentLoader, AAuthSigner

agent_def = AgentLoader(DAEMON_NAME).load()
# → agent_def.aauth_sub = "anthus@ateles-swarm"
# → agent_def.agent_grant = "service"

signer = AAuthSigner.from_key_file(DAEMON_NAME)
# → loads ateles-private/keys/anthus.json if present
# → returns stub signer if not (with logged warning)
```

Current daemon status (May 2026 — see the full topology table above for ground truth):

| Daemon | `aauth_sub` | Keypair minted | JWKS published | Grant entity |
|---|---|---|---|---|
| Apus (mirror/webhook) | `apus@ateles-swarm` | ✅ | ❌ | ❌ |
| Formica (issue triage) | `formica@ateles-swarm` | ✅ | ❌ | ❌ |
| Monedula (payments) | `monedula@ateles-swarm` | ✅ | ❌ | ❌ |
| neotoma-agent | `neotoma-agent@ateles-swarm` | ✅ | ❌ | ❌ |
| Ateles (T2 operator) | `ateles@ateles-swarm` | ✅ | ❌ | ❌ |
| Cicada (T4 code worker) | `cicada@ateles-swarm` | ✅ | ❌ | ✅ (github_harness:write on ateles) |
| Vanellus (T4 PR steward) | `vanellus@ateles-swarm` | ✅ | ❌ | ✅ (github_harness:write on ateles + neotoma) |
| Anthus (orchestrator) | `anthus@ateles-swarm` | ❌ stub | ❌ | ❌ |
| Tyto, Turdus, Apis | `<name>@ateles-swarm` | ❌ stub | ❌ | ❌ |

"Stub" means the daemon runs without per-agent signing — Neotoma attributes its observations to the operator-scoped auth instead. "No JWKS publish" means external resources can't verify the signature without out-of-band key distribution; Neotoma in-network can verify because it has access to the same `ateles-private/keys/` directory or has the key cached.

Only the Cursor IDE proxy is **fully end-to-end active** (keypair + JWKS publish + grant):

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

The `cnf.jwk` lets Neotoma verify the signature inline without a JWKS fetch, which matters during local development before the website is deployed.

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

No thumbprint pin — any valid ES256 key under the same `(sub, iss)` is admitted. This allows software and hardware keys to rotate without updating the grant.

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

Most T3/T4 roles already have a `ateles-private/keys/<role>.jwk.json` (the `aauth_httpsig.py` flavor).
For a role that does not yet have one, mint with:

```bash
python3 execution/scripts/aauth_provision_identity.py --role <role>
```

This writes directly to the format `lib/daemon_runtime/aauth_httpsig.py` already loads — no PEM/JWK
conversion step is needed for this flavor. (The separate `.creds/`-based JWK flavor used by the Cursor
proxy is a different identity and has its own, currently unimplemented, provisioning path — see
"Two identity flavors" above.)

### 2. Create `agent_grant` entities for remaining subs

Today only Cursor, Cicada, and Vanellus have grants. Apus, Formica, Monedula, neotoma-agent, and Ateles sign locally but are not admitted — Neotoma falls back to operator-level attribution. Create one grant per sub, scoped to the operations that daemon needs, via the operator's own authenticated Neotoma CLI session (`agent_grant` is a protected entity type — see `docs/subsystems/aauth.md` on the Neotoma side — so it is created through the `createAgentGrant` operation, not the generic `store` verb):

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

Today `https://markmhendrickson.com/.well-known/jwks.json` serves `sw-cursor-1` only. To extend trust to external verifiers, each daemon's public PEM needs to be converted to JWK form and merged into `execution/website/markmhendrickson/react-app/public/.well-known/jwks.json`, then the website redeployed. Subjects also need to be added to `aauth-agent.json` `subjects_supported`.

### 4. Reconcile the two keypair formats

The split between `.creds/*.jwk` and `ateles-private/keys/*.json` is incidental — both encode the same EC P-256 keypair in different envelopes. Picking one (likely JWK, since that's what the JWKS endpoint serves natively) and updating both signers to consume it would simplify the system and remove the conversion step in (3).

### 5. Tighten grants to per-entity-type capabilities (and per-tool — see [ateles#26](https://github.com/markmhendrickson/ateles/issues/26))

Today all populated grants use `*` for `store_structured` and `correct` capabilities — meaning any verified agent can write any entity type. The grant schema already supports finer-grained allowlists:

```jsonc
{
  "match_sub": "monedula@ateles-swarm",
  "match_iss": "https://markmhendrickson.com",
  "capabilities": {
    "store_structured":   ["transaction", "payment_profile", "daemon_report"],
    "correct":            ["payment_profile"],
    "retrieve":           ["transaction", "recurring_expense", "account_balance", "contact"],
    "create_relationship": ["transaction->contact", "payment_profile->contact"]
  }
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
│   ├── scripts/
│   │   ├── aauth_provision_identity.py    ← mint a T3/T4 role's ateles-private/keys/<role>.jwk.json
│   │   ├── aauth_signer.py                ← full RFC 9421 signer (Cursor proxy)
│   │   ├── verify_aauth_signer.py         ← interop proof for lib/daemon_runtime/aauth_httpsig.py
│   │   ├── mcp_identity_proxy.py          ← Cursor → Neotoma proxy with AAuth
│   │   └── verify_neotoma_identity_proxy.py ← end-to-end smoke test
│   └── website/markmhendrickson/react-app/public/.well-known/
│       ├── aauth-agent.json               ← agent metadata endpoint
│       └── jwks.json                      ← public keys endpoint (sw-cursor-1 only today)
├── lib/
│   └── daemon_runtime/
│       ├── aauth_signer.py                ← daemon signer (PEM keys, X-AAuth-Token, stub-capable)
│       ├── aauth_httpsig.py               ← full RFC 9421 signer (ateles-private/keys/<role>.jwk.json)
│       ├── agent_loader.py                ← loads agent_definition incl. aauth_sub
│       └── __init__.py                    ← re-exports AAuthSigner
└── execution/daemons/
    ├── anthus/anthus.py                   ← uses AAuthSigner.from_key_file("anthus") — stub today
    ├── formica/formica.py                 ← uses AAuthSigner.from_key_file("formica") — keypair present
    ├── apus/apus.py                       ← uses AAuthSigner.from_key_file("apus") — keypair present
    └── ...                                ← same pattern in all T3 daemons

ateles-private/           ← private repo, checked out alongside ateles
└── keys/                                  ← PEM format, 7 keypairs present
    └── <daemon>.json                      ← per-daemon private JWK (not yet minted)
```

---

## Related

- [`docs/architecture.md`](architecture.md) — system layers and Neotoma integration overview
- [`execution/reports/aauth/brief_for_dick_hardt_2026-04-27.md`](../execution/reports/aauth/brief_for_dick_hardt_2026-04-27.md) — implementation notes from the Cursor cutover
- [`execution/reports/aauth/phase4_cutover_2026-04-27.md`](../execution/reports/aauth/phase4_cutover_2026-04-27.md) — validation evidence with live Neotoma response
- AAuth spec: [aauth.fyi](https://aauth.fyi)
