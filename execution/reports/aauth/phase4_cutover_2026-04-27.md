# AAuth Phase 4 cutover — execution report

**Date:** 2026-04-27
**Plan:** `Agent-side AAuth signing for Neotoma MCP` (plan_document `ent_5050972e79a4e5203ad38197`)
**Operator agent:** Cursor (`cursor@markmhendrickson.com`)
**Resource server:** Local Neotoma at `http://localhost:3180/mcp`

## Outcome

Phase 4 (cutover/validation) is **complete**. The Cursor agent now signs every Neotoma MCP request with AAuth (RFC 9421 HTTP Message Signatures + `aa-agent+jwt`) and Neotoma resolves the agent at the `software` tier with `eligible_for_trusted_writes: true` and grant-based admission.

## Validation evidence (live, against local Neotoma)

`GET /session` (signed):

```json
{
  "attribution": {
    "tier": "software",
    "decision": {
      "signature_present": true,
      "signature_verified": true,
      "resolved_tier": "software"
    },
    "agent_thumbprint": "EUKACtuRIpVbruzwmSlA3jTjXfX6_m3kgWrX6bFdQb0",
    "agent_sub": "cursor@markmhendrickson.com",
    "agent_iss": "https://markmhendrickson.com",
    "agent_algorithm": "ES256"
  },
  "aauth": {
    "verified": true,
    "admitted": true,
    "grant_id": "ent_36b1ccf3efe5905bd75aca3c",
    "admission_reason": "admitted",
    "agent_label": "Cursor agent (mark@markmhendrickson.com)"
  },
  "eligible_for_trusted_writes": true
}
```

`POST /mcp` `initialize` (signed via launcher script): returns the full Neotoma MCP `serverInfo` and instructions block; no `400`, no anonymous fallback.

## Components shipped this phase

- `execution/scripts/aauth_signer.py` — Python port of Neotoma's `aauth_signer.ts`. Mints `aa-agent+jwt` (`iss`, `sub`, `iat`, `exp`, `jkt`, `cnf.jwk`); signs `@method @authority @path content-type content-digest signature-key` with ES256; emits `Signature-Key: aasig=jwt;jwt="…"`.
- `execution/scripts/mcp_identity_proxy.py` — gained `--aauth` (or `MCP_PROXY_AAUTH=1`), lazy AAuth signer load, `X-Agent-Label` injection, fail-closed option.
- `execution/scripts/run_neotoma_identity_proxy.sh` — prefers `.venv/bin/python3`, dependency-checks the AAuth deps when AAuth is on, forwards `--aauth`, fixed for Python 3.14's `importlib.util` resolution.
- `.cursor/mcp.json` — `neotoma-proxy` now defaults to `MCP_PROXY_AAUTH=1`, `MCP_PROXY_DOWNSTREAM_URL=http://localhost:3180/mcp`, and pins the AAuth `sub`/`iss`/`kid`/`authority` envs.
- `agent_grant ent_36b1ccf3efe5905bd75aca3c` — created in Neotoma. Matches `match_sub=cursor@markmhendrickson.com`, `match_iss=https://markmhendrickson.com`, no thumbprint pin, capabilities = `*` for `store_structured` / `create_relationship` / `correct` / `retrieve` so future YubiKey FIDO2 keys admit through the same grant.

## Phases status

| Phase | Status |
|------|--------|
| 1. Reconnaissance | done |
| 2. Identity provisioning (ES256 + JWKS + agent metadata) | done |
| 3. Wire signing logic into proxy | done |
| 4. Cutover and validation | **done** |
| 5. Brief Dick Hardt | brief drafted (this turn) |
| 6. (Optional) YubiKey FIDO2 hardware-tier path | pending |

## Outstanding tasks

- Deploy the website submodule so `https://markmhendrickson.com/.well-known/jwks.json` and `…/.well-known/aauth-agent.json` are publicly resolvable. (Neotoma admits us today via `cnf.jwk` inline; deployment matters for the "any AAuth resource on the internet can verify us" promise.) — Task `ent_84b2c90de869eea7efc00178`.
- Back up `.creds/aauth_agent_cursor.private.jwk` to 1Password. — Task `ent_1e66983dc07df9ae91e043fb`.

## Notes

- AAuth verification happens regardless of grant; grant gates `admitted`. Even before the grant existed, `signature_verified: true, tier: software` was already populated on every signed request.
- `policy.anonymous_writes: allow` is the local-dev default; once that flips to `deny`, this grant is what keeps Cursor working.
