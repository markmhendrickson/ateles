# AAuth in Ateles

## Purpose

Documents how AAuth agent authentication is used across the Ateles repo: identity topology, signing implementations, daemon keypair status, wire format, Neotoma grant configuration, and the checklist to activate per-daemon attribution.

## Scope

Covers all AAuth-related files in the repo (`execution/scripts/aauth_*.py`, `lib/daemon_runtime/aauth_signer.py`, the MCP proxy layer, and the published `.well-known/` endpoints), the current activation state for each daemon, and next steps. Does not cover Neotoma's server-side verifier implementation — see the Neotoma repo for `aauthVerify` middleware details.

---

AAuth is the agent authentication protocol Ateles uses to give every daemon and invocable agent a verifiable identity. This document maps where AAuth is used across the repo, how each component fits together, and what still needs to be done before the full trust chain is active.

---

## What AAuth does here

AAuth solves one problem: **which agent wrote this observation?** Without it, every Neotoma write comes from the operator bearer token, making attribution coarse-grained ("a Claude session did this"). With AAuth, each daemon signs its requests with its own EC keypair, so Neotoma records `agent_sub: anthus@ateles-swarm` (or `cursor@markmhendrickson.com` for IDE sessions) on every observation — provenance down to the agent, not just the operator.

Neotoma's AAuth pipeline:
1. **Signature verification** — checks the RFC 9421 HTTP Message Signature
2. **Tier resolution** — ES256 software key → `tier=software`; FIDO2-attested key → `tier=hardware`
3. **Grant admission** — matches the resolved `(sub, iss)` against an `agent_grant` entity; gates `eligible_for_trusted_writes`

---

## Identity topology

All agent identities share one issuer (`iss = https://markmhendrickson.com`). Each distinct agent role gets its own subject and keypair:

| Agent context | `sub` | `kid` | Key file | Status |
|---|---|---|---|---|
| Cursor IDE proxy | `cursor@markmhendrickson.com` | `sw-cursor-1` | `.creds/aauth_agent_cursor.private.jwk` | **active** |
| Ateles daemons (planned) | `<daemon>@ateles-swarm` | `sw-<daemon>-1` | `ateles-private/keys/<daemon>.json` | stub mode |
| YubiKey hardware tier (planned) | `cursor@markmhendrickson.com` | `hw-cursor-yk-1` | YubiKey PIV slot | not started |

Public keys are published at:
- JWKS: `https://markmhendrickson.com/.well-known/jwks.json` — currently contains `sw-cursor-1` only
- Agent metadata: `https://markmhendrickson.com/.well-known/aauth-agent.json` — `subjects_supported: [cursor@markmhendrickson.com]`

Source in repo: `execution/website/markmhendrickson/react-app/public/.well-known/`

---

## Files and their roles

### Signing logic

| File | Role |
|---|---|
| `execution/scripts/aauth_signer.py` | Full RFC 9421 signer. Produces `Signature-Key`, `Signature-Input`, `Signature`, `Content-Digest` headers. Used by the Cursor MCP proxy. |
| `lib/daemon_runtime/aauth_signer.py` | Simplified signer for T3 daemons. Produces `X-AAuth-Token` (JWT-only, not full httpsig). Falls back gracefully to stub mode when keypair is absent. |

These are two distinct implementations for two distinct contexts:
- `execution/scripts/aauth_signer.py` — implements the **full AAuth wire format** (`@hellocoop/httpsig` compatible): signs `@method @authority @path content-type content-digest signature-key`. This is what Neotoma's verifier expects from an external MCP client.
- `lib/daemon_runtime/aauth_signer.py` — implements a **lighter JWT-only path** for daemons that talk to Neotoma directly over a trusted connection. Used today while daemon keypairs are unminted.

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

Current daemon status:

| Daemon | `aauth_sub` | Keypair minted | Grant entity |
|---|---|---|---|
| Anthus (orchestrator) | `anthus@ateles-swarm` | stub | pending |
| Formica (issue triage) | `formica@ateles-swarm` | stub | pending |
| Apus (mirror/webhook) | `apus@ateles-swarm` | stub | pending |
| neotoma-agent | `neotoma-agent@ateles-swarm` | stub | pending |
| Tyto, Turdus, Apis | `<name>@ateles-swarm` | stub | pending |

"Stub" means the daemon runs without per-agent signing. Neotoma attributes its observations to the operator bearer token. The behaviour is identical — only the provenance granularity is coarser.

The Cursor IDE proxy is the only **fully active** AAuth identity:

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

### Mint daemon keypairs (one-time per daemon)

```bash
.venv/bin/python execution/scripts/aauth_provision_identity.py \
    --sub anthus@ateles-swarm --kid sw-anthus-1
# repeat for: formica, apus, neotoma-agent, tyto, turdus, apis
```

Then copy the generated `.creds/aauth_agent_<role>.private.jwk` files to `ateles-private/keys/<daemon>.json`. After that, daemons will sign automatically — no code change needed.

### Create grant entities for daemon subs

One `agent_grant` per daemon `(sub, iss)` pair, scoped to the operations that daemon needs:

```bash
neotoma store agent_grant \
  --match_sub anthus@ateles-swarm \
  --match_iss https://markmhendrickson.com \
  --capabilities '{"spawn_agent": "*", "store_structured": "*"}'
```

### Deploy the JWKS endpoint

The public JWKS at `https://markmhendrickson.com/.well-known/jwks.json` currently serves `sw-cursor-1` only. After minting daemon keys, redeploy the website submodule to add their public JWKs.

### YubiKey hardware tier (Phase 6)

Same identity (`cursor@markmhendrickson.com`), second keypair (`kid: hw-cursor-yk-1`), `cnf.attestation` from a WebAuthn ceremony, `tier=hardware` in Neotoma. Same grant admits it — no grant update needed.

---

## Key files quick-reference

```
ateles/
├── .creds/
│   └── aauth_agent_cursor.private.jwk     ← gitignored, mode 600
├── execution/
│   ├── scripts/
│   │   ├── aauth_provision_identity.py    ← generate keypair + publish JWKS
│   │   ├── aauth_signer.py                ← full RFC 9421 signer (Cursor proxy)
│   │   ├── mcp_identity_proxy.py          ← Cursor → Neotoma proxy with AAuth
│   │   └── verify_neotoma_identity_proxy.py ← end-to-end smoke test
│   └── website/markmhendrickson/react-app/public/.well-known/
│       ├── aauth-agent.json               ← agent metadata endpoint
│       └── jwks.json                      ← public keys endpoint
├── lib/
│   └── daemon_runtime/
│       ├── aauth_signer.py                ← daemon signer (stub-capable)
│       ├── agent_loader.py                ← loads agent_definition incl. aauth_sub
│       └── __init__.py                    ← re-exports AAuthSigner
└── execution/daemons/
    ├── anthus/anthus.py                   ← uses AAuthSigner.from_key_file("anthus")
    ├── formica/formica.py                 ← uses AAuthSigner.from_key_file("formica")
    ├── apus/apus.py                       ← uses AAuthSigner.from_key_file("apus")
    └── ...                                ← same pattern in all T3 daemons

ateles-private/           ← private repo, checked out alongside ateles
└── keys/
    └── <daemon>.json                      ← per-daemon private JWK (not yet minted)
```

---

## Related

- [`docs/architecture.md`](architecture.md) — system layers and Neotoma integration overview
- [`execution/reports/aauth/brief_for_dick_hardt_2026-04-27.md`](../execution/reports/aauth/brief_for_dick_hardt_2026-04-27.md) — implementation notes from the Cursor cutover
- [`execution/reports/aauth/phase4_cutover_2026-04-27.md`](../execution/reports/aauth/phase4_cutover_2026-04-27.md) — validation evidence with live Neotoma response
- AAuth spec: [aauth.fyi](https://aauth.fyi)
