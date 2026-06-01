# AAuth in production: a Cursor → Neotoma signing report

**To:** Dick Hardt
**From:** Mark Hendrickson
**Date:** 2026-04-27
**Status:** working artifact, not a pitch

## TL;DR

I wired AAuth signing into a Cursor IDE agent talking to a Neotoma resource server. Every MCP request from Cursor now carries an RFC 9421 HTTP Message Signature with an `aa-agent+jwt` in `Signature-Key`. Neotoma verifies the signature, resolves a stable agent identity (`sub=cursor@markmhendrickson.com`, `iss=https://markmhendrickson.com`), assigns `tier=software`, matches an `agent_grant`, and gates writes on `eligible_for_trusted_writes`. End-to-end works on a single laptop today; the same agent identity is positioned to pick up YubiKey FIDO2 attestation for `tier=hardware` next.

If useful, this is a concrete data point on what an early adopter does when AAuth lands in a real agent stack.

## What's running

```
[Cursor IDE]
  ↓ stdio MCP (JSON-RPC)
[mcp_identity_proxy.py with AAuth signer]
  ↓ HTTPS, RFC 9421 signed, Signature-Key=aasig=jwt;jwt="<aa-agent+jwt>"
[Neotoma MCP server]
  → @hellocoop/httpsig verify
  → fetch JWKS (inline cnf.jwk today; .well-known JWKS once site deploys)
  → resolve attribution (tier=software)
  → match agent_grant (sub+iss)
  → admit + execute MCP action
```

Identity is hosted at `https://markmhendrickson.com/.well-known/aauth-agent.json` + `/jwks.json` (apex Netlify deploy).

Agent subject convention I landed on:
- One `iss` per identity host (`https://markmhendrickson.com`).
- One `sub` per agent product on that host (`cursor@…`, future `mcp@…`, `cli@…`).
- `kid` namespaced by backing (`sw-cursor-1`, future `hw-cursor-yk-1`).

## What worked smoothly

- The spec and the reference TS verifier (`@hellocoop/httpsig`) were enough to port a working signer to Python in an afternoon.
- The `aa-agent+jwt` shape (with `cnf.jwk` inline) let me ship and verify before the JWKS endpoint was even live. That's a meaningful UX win for early integrators — you don't need a public domain to test.
- `signature-key` being part of the signed components means a stolen JWT is useless without the private key, even for the same `sub`. That property maps cleanly onto how I'd want to deploy YubiKey-backed and software-backed keys side by side under one identity.

## Friction points worth knowing about

1. **Authority canonicalization.** The Resource has to agree with the agent on `@authority`. I had to add an `authority_override` knob in the agent so `localhost:3180` matches what Neotoma puts in `NEOTOMA_AUTH_AUTHORITY`. Worth flagging in the spec or the reference doc; first-time integrators will hit this.
2. **`structured field` quoting in `Signature-Key`.** Constructing `aasig=jwt;jwt="<token>"` correctly with Python's `http_sfv` took two iterations (the `Item(Token("jwt"))` / inner-list shape isn't obvious from the RFC alone). A worked Python example in the AAuth dev guide would have saved me a debugging cycle.
3. **Tier semantics for ES256 software keys.** I initially expected ES256 in Secure Enclave to give `tier=hardware`; in practice that requires Apple-attested `cnf.attestation`. ES256 stored as a JWK on disk lands at `software`. That's correct, but worth being explicit about in the resource-implementer guide so verifiers don't accidentally call ES256 "hardware".
4. **Grants vs verification.** Verification (`tier`) and admission (`grant`) are two different gates. Both Neotoma and AAuth get this right architecturally, but it took me a probe to realize "verified=true, admitted=false" simply meant "I haven't told the resource that this agent is allowed yet." Could be a one-liner in the docs: "the resource decides who's verified; the operator decides who's admitted."

## What I want to try next on this stack

- **Hardware tier via YubiKey FIDO2.** The same agent identity, a second `kid` (`hw-cursor-yk-1`), `cnf.attestation: { format: "packed", … }` from a WebAuthn ceremony, optional thumbprint pin on the grant. I think Neotoma's verifier already supports `packed`; I want to confirm and write up the YubiKey-on-Mac path.
- **Person Server proxy on top of Neotoma.** Separate thread, but worth flagging: Neotoma's primitives (append-only observations, hash-addressed sources, schema-typed entities, provenance traversal) line up with what a PS needs from its storage layer. If you ever want a reference PS to lean on a substrate rather than rebuild event sourcing, I'd be happy to scope that out.

## Asks (none required)

- Sanity check on the `iss` / `sub` / `kid` convention above for an individual operator running multiple agents.
- A pointer to the canonical "resource implementer hardware-tier checklist" if there's a more recent one than what's on the playground.
- Whether you'd want this writeup to live anywhere public (blog post on `markmhendrickson.com` or PR against an AAuth examples repo).

— Mark
