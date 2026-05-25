# AAuth in Ateles

## Purpose

Documents how AAuth agent authentication is used across the Ateles repo: identity topology, signing implementations, daemon keypair status, wire format, Neotoma grant configuration, and the checklist to activate per-daemon attribution.

## Scope

Covers all AAuth-related files in the repo (`execution/scripts/aauth_*.py`, `lib/daemon_runtime/aauth_signer.py`, the MCP proxy layer, and the published `.well-known/` endpoints), the current activation state for each daemon, and next steps. Does not cover Neotoma's server-side verifier implementation — see the Neotoma repo for `aauthVerify` middleware details.

---

AAuth is the agent authentication protocol Ateles uses to give every daemon and invocable agent a verifiable identity. This document maps where AAuth is used across the repo, how each component fits together, and what still needs to be done before the full trust chain is active.

---

## What AAuth does here

AAuth solves one problem: **which agent wrote this observation?** Without it, every Neotoma write comes from the operator-scoped auth, making attribution coarse-grained ("a Claude session did this"). With AAuth, each daemon signs its requests with its own EC keypair, so Neotoma records `agent_sub: anthus@ateles-swarm` (or `cursor@markmhendrickson.com` for IDE sessions) on every observation — provenance down to the agent, not just the operator.

Neotoma's AAuth pipeline:
1. **Signature verification** — checks the RFC 9421 HTTP Message Signature
2. **Tier resolution** — ES256 software key → `tier=software`; FIDO2-attested key → `tier=hardware`
3. **Grant admission** — matches the resolved `(sub, iss)` against an `agent_grant` entity; gates `eligible_for_trusted_writes`

---

## Identity topology

All agent identities share one issuer (`iss = https://markmhendrickson.com`). Each distinct agent role gets its own subject and keypair.

### Two identity flavors

The repo currently maintains **two parallel keypair formats** for two contexts:

1. **JWK format** (`.creds/aauth_agent_*.private.jwk`) — used by the Cursor IDE MCP proxy. Provisioned by `aauth_provision_identity.py`. Public keys publish to `markmhendrickson.com/.well-known/jwks.json`. ES256 P-256 only.

2. **PEM format** (`ateles-private/keys/<daemon>.json`, with `sub`, `key_id`, `algorithm`, and PEM-encoded private/public material) — used by T3 daemons via `lib/daemon_runtime/aauth_signer.py`. **Not yet published to JWKS** — only Neotoma can verify these today (via local key resolution or because the daemon talks to Neotoma over a trusted connection).

Unifying these formats and publishing all public keys to the same JWKS is on the to-do list below.

### Per-agent status (ground truth, May 2026)

| Agent | `sub` | `kid` | Keypair on disk | Published in JWKS | `agent_grant` entity |
|---|---|---|---|---|---|
| Cursor IDE | `cursor@markmhendrickson.com` | `sw-cursor-1` | ✅ `.creds/aauth_agent_cursor.private.jwk` | ✅ | ✅ `ent_36b1ccf3...` |
| Apus | `apus@ateles-swarm` | `apus-edfb838b` | ✅ `ateles-private/keys/apus.json` | ❌ | ❌ |
| Formica | `formica@ateles-swarm` | `formica-f536eae6` | ✅ `ateles-private/keys/formica.json` | ❌ | ❌ |
| Gryllus | `gryllus@ateles-swarm` | `gryllus-1534bccd` | ✅ `ateles-private/keys/gryllus.json` | ❌ | ✅ `ent_8e3101e9...` (github_harness:write on ateles) |
| Monedula | `monedula@ateles-swarm` | `monedula-e128133c` | ✅ `ateles-private/keys/monedula.json` | ❌ | ❌ |
| neotoma-agent | `neotoma-agent@ateles-swarm` | `castor-c50f03d8` | ✅ `ateles-private/keys/neotoma_agent.json` | ❌ | ❌ |
| Onychomys | `onychomys@ateles-swarm` | `onychomys-854d78fb` | ✅ `ateles-private/keys/onychomys.json` | ❌ | ❌ |
| Vanellus | `vanellus@ateles-swarm` | `vanellus-d919a64c` | ✅ `ateles-private/keys/vanellus.json` | ❌ | ✅ `ent_09762f11...` (github_harness:write on ateles+neotoma) |
| Anthus | `anthus@ateles-swarm` | — | ❌ | ❌ | ❌ |
| Tyto, Turdus, Apis | `<name>@ateles-swarm` | — | ❌ | ❌ | ❌ |
| Menura, Piculet, Strix | `<name>@ateles-swarm` | — | ❌ | ❌ | ❌ |
| YubiKey hardware tier (planned) | `cursor@markmhendrickson.com` | `hw-cursor-yk-1` | not started | not started | covered by existing cursor grant |

### What "active" means per row

- **Keypair on disk + JWKS publish + agent_grant** → fully active, end-to-end attribution and admission. Only Cursor reaches this today.
- **Keypair on disk only** → daemon can mint AAuth JWTs locally, but external verifiers can't fetch the public key, and Neotoma will verify but won't admit unless a grant matches. Effectively "signs but unadmitted." This covers Apus, Formica, Monedula, neotoma-agent, Onychomys.
- **Keypair + grant, no JWKS publish** → Gryllus and Vanellus can be admitted by Neotoma for github_harness writes, but only over the local network where Neotoma already has the key. Publishing to JWKS would extend trust to any AAuth resource.
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
- `lib/daemon_runtime/aauth_signer.py` — implements a **lighter JWT-only path** for daemons. Consumes PEM-format keys from `ateles-private/keys/<daemon>.json`. Today the daemons that have keypairs (Apus, Formica, Monedula, Gryllus, neotoma-agent, Onychomys, Vanellus) sign locally; daemons without keypairs (Anthus, Tyto, Turdus, Apis) fall back to stub mode.

### Identity provisioning

| File | Role |
|---|---|
| `execution/scripts/aauth_provision_identity.py` | Generates an ES256 P-256 keypair, writes the private JWK to `.creds/`, updates `jwks.json` and `aauth-agent.json` additively. Run once per new agent subject. |

Usage:
```bash
# Provision a new agent identity (or rotate with --force)
.venv/bin/python execution/scripts/aauth_provision_identity.py \
    --sub cursor@markmhendrickson.com \
    --kid sw-cursor-1 \
    --force
```

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
| Onychomys (T2 operator) | `onychomys@ateles-swarm` | ✅ | ❌ | ❌ |
| Gryllus (T4 code worker) | `gryllus@ateles-swarm` | ✅ | ❌ | ✅ (github_harness:write on ateles) |
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

## What to do next

### 1. Mint missing daemon keypairs

Anthus, Tyto, Turdus, Apis, Menura, Piculet, and Strix currently have no keypair on disk. To mint:

```bash
.venv/bin/python execution/scripts/aauth_provision_identity.py \
    --sub anthus@ateles-swarm --kid sw-anthus-1
```

Note: this script writes a JWK-format key to `.creds/`. The daemon runtime currently expects a PEM-format key at `ateles-private/keys/<daemon>.json`. Either:
- Add a converter step to translate JWK → PEM for `ateles-private/keys/`, OR
- Update `lib/daemon_runtime/aauth_signer.py` to also load JWK format (eliminating the format split)

### 2. Create `agent_grant` entities for remaining subs

Today only Cursor, Gryllus, and Vanellus have grants. Apus, Formica, Monedula, neotoma-agent, and Onychomys sign locally but are not admitted — Neotoma falls back to operator-level attribution. Create one grant per sub, scoped to the operations that daemon needs:

```bash
neotoma store agent_grant \
  --match_sub apus@ateles-swarm \
  --match_iss https://markmhendrickson.com \
  --capabilities '{"store_structured": "*", "create_relationship": "*"}'
```

### 3. Publish daemon public keys to the JWKS endpoint

Today `https://markmhendrickson.com/.well-known/jwks.json` serves `sw-cursor-1` only. To extend trust to external verifiers, each daemon's public PEM needs to be converted to JWK form and merged into `execution/website/markmhendrickson/react-app/public/.well-known/jwks.json`, then the website redeployed. Subjects also need to be added to `aauth-agent.json` `subjects_supported`.

### 4. Reconcile the two keypair formats

The split between `.creds/*.jwk` and `ateles-private/keys/*.json` is incidental — both encode the same EC P-256 keypair in different envelopes. Picking one (likely JWK, since that's what the JWKS endpoint serves natively) and updating both signers to consume it would simplify the system and remove the conversion step in (3).

### 5. YubiKey hardware tier (Phase 6)

Same identity (`cursor@markmhendrickson.com`), second keypair (`kid: hw-cursor-yk-1`), `cnf.attestation` from a WebAuthn ceremony, `tier=hardware` in Neotoma. Same grant admits it — no grant update needed.

---

## Key files quick-reference

```
ateles/
├── .creds/                                ← gitignored
│   └── aauth_agent_cursor.private.jwk     ← JWK format, mode 600 (Cursor IDE only)
├── execution/
│   ├── scripts/
│   │   ├── aauth_provision_identity.py    ← generate JWK keypair + publish to JWKS
│   │   ├── aauth_signer.py                ← full RFC 9421 signer (Cursor proxy)
│   │   ├── mcp_identity_proxy.py          ← Cursor → Neotoma proxy with AAuth
│   │   └── verify_neotoma_identity_proxy.py ← end-to-end smoke test
│   └── website/markmhendrickson/react-app/public/.well-known/
│       ├── aauth-agent.json               ← agent metadata endpoint
│       └── jwks.json                      ← public keys endpoint (sw-cursor-1 only today)
├── lib/
│   └── daemon_runtime/
│       ├── aauth_signer.py                ← daemon signer (PEM keys, stub-capable)
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
