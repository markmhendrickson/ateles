# AAuth Phase 6 scoping — hardware-tier path

**Date:** 2026-04-27
**Plan:** `Agent-side AAuth signing for Neotoma MCP` (plan_document `ent_5050972e79a4e5203ad38197`)
**Status:** Phase 6 — **scoped, deferred**. No code changes. Reconnaissance only.

## TL;DR

Phase 6 was scoped as "YubiKey FIDO2 hardware-tier path." Reconnaissance shows:

1. **No YubiKey is present on this machine** and the supporting tooling (`yubico-piv-tool` / `libykcs11`) is not installed. The path needs a hardware purchase + tooling install + native binding build (1–2 days work) before any code can run.
2. **Apple SE is platform-native and the binding loads cleanly on this M4 Max**, but on **plain macOS** Apple's `SecKeyCreateAttestation` cannot mint a verifiable attestation envelope (the `kSecKeyAttestationKeyTypeGID` constant is iOS / Mac Catalyst only). Neotoma's native binding deliberately refuses to produce one rather than emit a self-signed envelope the verifier would reject. SE can still **sign** (key isolation), but the resulting tier stays `software`.
3. **Hardware tier has no functional gain today.** Local Neotoma is `policy.anonymous_writes: allow`. The Cursor agent's identity already resolves at `tier: software` with `eligible_for_trusted_writes: true` via `agent_grant ent_36b1ccf3efe5905bd75aca3c` (see Phase 4 cutover report).

Recommendation: **defer Phase 6**, persist the findings, file follow-up tasks for the real prerequisites, and keep the production proxy at software tier. Hardware tier becomes a real conversation when (a) production Neotoma flips `anonymous_writes` to `deny` and tightens grants, (b) we want a portable hardware identity for non-macOS hosts, or (c) Apple ships macOS-friendly attestation for SE-resident keys.

## Reconnaissance evidence

### Host capabilities

```
arch              : arm64
cpu               : Apple M4 Max
os                : macOS 26.3.1 (build 25D2128)
ykman             : not installed
yubico-piv-tool   : not installed
libykcs11.dylib   : absent from /opt/homebrew/lib and /usr/local/lib
yubikey usb       : (no YubiKey-shaped USB device detected)
secure enclave    : present (Apple Silicon → SE always available)
```

### Neotoma's hardware backends (status here)

| Package | Source | Native build | Loadable here | Tier-eligible on this Mac? |
|---|---|---|---|---|
| `@neotoma/aauth-mac-se` | shipped (`packages/aauth-mac-se/`) | yes (`build/Release/aauth_mac_se.node` already compiled, 80 KB) | **yes** (`isSupported() => { supported: true }`) | **no** — `attest()` returns `SE_ATTESTATION_NOT_AVAILABLE_ON_MACOS`; macOS public SDK does not declare `kSecKeyAttestationKeyTypeGID` |
| `@neotoma/aauth-yubikey` | shipped (`packages/aauth-yubikey/`) | source ships, `binding.gyp` present, `prebuilds/` not populated in this checkout | not loaded — optional dep not in `node_modules/@neotoma/` | requires YubiKey 5 series + `libykcs11` + native build |
| `@neotoma/aauth-tpm2` | shipped | linux-only | n/a on darwin | n/a |
| `@neotoma/aauth-win-tbs` | shipped | win32-only | n/a on darwin | n/a |

### Verifier wiring is complete

`src/services/aauth_attestation_verifier.ts` dispatches three `cnf.attestation.format` values (`apple-secure-enclave`, `webauthn-packed`, `tpm2`) and the per-format verifiers exist (`aauth_attestation_apple_se.ts`, `aauth_attestation_webauthn_packed.ts`, `aauth_attestation_tpm2.ts`). Verifier wiring is **not** the constraint.

### CLI wiring is incomplete

`neotoma auth keygen` today only accepts `--alg / --sub / --iss / --force` — there is **no `--hardware` flag** in the deployed CLI even though `src/cli/aauth_yubikey_attestation.ts` and the equivalent SE/TPM2/TBS helpers reference such a flag. The optional native packages are present in the monorepo but not wired into a runnable user command. This is upstream Neotoma work, not ateles work.

```
$ node /Users/markmhendrickson/repos/neotoma/dist/cli/bootstrap.js auth keygen --help
Usage: neotoma auth keygen [options]
…
Options:
  --alg <alg>  Signing algorithm: ES256 (default) or EdDSA (default: "ES256")
  --sub <sub>  AAuth subject (self-reported identity)
  --iss <iss>  AAuth issuer (defaults to https://neotoma.cli.local)
  --force      Overwrite an existing keypair (default: false)
```

## Why Apple SE on this Mac cannot reach `tier: hardware`

From `packages/aauth-mac-se/src/native/binding.mm`, `Attest()`:

```objc
// App Attestation via `SecKeyCreateAttestation` is iOS / Mac Catalyst only
// — `kSecKeyAttestationKeyTypeGID` is `API_UNAVAILABLE(macos)` in the
// public SDK. On plain macOS we surface a stable error so the CLI falls
// back to `software` rather than emitting a self-signed envelope the
// server would reject anyway.
#if (TARGET_OS_IPHONE && !TARGET_OS_OSX) || TARGET_OS_MACCATALYST
  // … real attestation …
#else
  ThrowCoded(env, "SE_ATTESTATION_NOT_AVAILABLE_ON_MACOS", nil);
#endif
```

The `verifyAppleSecureEnclaveAttestation` server path expects a chain that terminates at Apple's attestation root and a signature over `SHA-256(challenge || jkt)` produced by an Apple-attested SE key. There is no documented public macOS path that produces such a chain today. (`DCAppAttestService` exists but only for code-signed iOS / macOS apps with a Team ID and embedded provisioning profile; an unsigned developer Node process cannot use it.)

Consequence: an SE-resident key on this Mac signs requests **with no `cnf.attestation`**, which Neotoma's `attribution.decision` block resolves as `tier: software` (same as our current file-resident JWK). The only delta from today is **key isolation** — the private key never lives on disk — at the cost of writing a Node sign helper and routing all signing through it.

## What hardware tier would actually require, by path

### Path A — Apple SE for `tier: hardware` on this Mac

**Blocked.** Would need either:

- Apple to expose `kSecKeyAttestationKeyTypeGID` on plain macOS (out of our hands), **or**
- Re-host the agent inside a code-signed mac app bundle that can use `DCAppAttestService` (large architectural change for an IDE proxy), **or**
- A different macOS-native attestation primitive that Neotoma's verifier learns to accept (upstream Neotoma + spec work).

### Path B — Apple SE for key isolation only (still `tier: software`)

Modest scope:

- Provision SE key via `@neotoma/aauth-mac-se.generateKey({tag})` — done in seconds, no touch.
- Replace `aauth_signer.py`'s file-resident JWK with a Node sign helper subprocess that calls `se.sign({tag, message})` per signature.
- Update `mcp_identity_proxy.py` to spawn the helper at startup, hold a long-lived stdin/stdout pipe to it, and route signature computations through it.
- Net effect: same `tier: software`, but private key cannot be exfiltrated from the disk.

Effort: ~half a day. Value: real but small (defense in depth) and orthogonal to AAuth tier.

### Path C — YubiKey FIDO2 / PIV for `tier: hardware`

Real deal, requires:

1. Hardware: YubiKey 5 series (firmware ≥ 5.0 for `YKPIV_INS_ATTEST`).
2. Tooling: `brew install yubico-piv-tool` to get `libykcs11.dylib`.
3. Build: `cd /Users/markmhendrickson/repos/neotoma/packages/aauth-yubikey && npm install && npm run build && npm run build:native` (or rely on prebuilds when published).
4. Provision: PIV slot 9c keygen (one PIN entry; `PIN ONCE` policy means the slot is unlocked for the process lifetime — fine for a long-lived MCP proxy).
5. Per-request signing: PKCS#11 sign over RFC 9421 base. Either via Node helper using the `@neotoma/aauth-yubikey` binding, or via Python `pyKCS11` directly.
6. Mint attestation envelope (`webauthn-packed` format, x5c chain to Yubico PIV CA roots, AAGUID). Done once per `aa-agent+jwt` mint (cheap; no PIN re-entry within the session).
7. Add `kid: hw-cursor-yk-1` to `https://markmhendrickson.com/.well-known/jwks.json` alongside the existing `sw-cursor-1`.

Effort: ~1–2 days from a new YubiKey to verified `tier: hardware`.
Value: portable across darwin / linux / win32 hosts; same identity works on a Mac, a Linux dev box, or a Windows laptop without re-provisioning per platform.

### Path D — Wait for upstream Neotoma `--hardware` flag

`neotoma auth keygen --hardware` is the canonical entry point referenced by the design docs (`docs/subsystems/aauth_cli_attestation.md` per the YubiKey CLI helper) but not yet wired into `src/cli/index.ts`. Once it lands, it auto-probes the ladder (SE → TPM2 → TBS → YubiKey → software) and uses the first available backend. After that, ateles either calls the Neotoma CLI for hardware-tier flows or copies the integration pattern.

This unlocks Path C *and* simplifies our Python-side work (we'd just shell out to `neotoma auth ...` for sign/mint operations rather than maintain our own Node helper). It is upstream Neotoma project work, not ateles work, and the right home for it.

## Recommendation

1. **Mark Phase 6 of the AAuth signing plan as scoped + deferred.** Production proxy stays at `tier: software` with the working `agent_grant`. The Phase 4/5 outcomes (signed Cursor MCP, brief for Dick Hardt) are unaffected.
2. **File a follow-up task in Neotoma project space** (not ateles): "Add `--hardware` flag to `neotoma auth keygen`, auto-probing the SE/YubiKey/TPM2/TBS ladder and minting `cnf.attestation`." This is the most leveraged single piece of work because it lights up every downstream agent at once.
3. **Optional smaller follow-up in ateles**: replace the file-resident `cursor@markmhendrickson.com` private JWK with an SE-backed key for key isolation (still `tier: software`). Worth doing only if disk-resident key material is a real threat model concern — otherwise the existing 1Password backup path is sufficient.
4. **Do not buy a YubiKey** speculatively for this. Buy one when (a) we have a non-macOS host that needs the same identity, or (b) Neotoma policy starts requiring `tier: hardware` for trusted writes.

## Outstanding from prior phases

These are still the highest-value next steps for the AAuth thread, independent of Phase 6:

- **Deploy `markmhendrickson.com` Netlify build** so `https://markmhendrickson.com/.well-known/jwks.json` and `…/aauth-agent.json` are publicly resolvable (task `ent_84b2c90de869eea7efc00178`). This converts our identity from "Neotoma-only-verifiable via inline `cnf.jwk`" to "any AAuth resource on the internet can verify us via JWKS."
- **Back up `.creds/aauth_agent_cursor.private.jwk` to 1Password** (task `ent_1e66983dc07df9ae91e043fb`) so the key survives a local disk loss.
- **Send the brief to Dick Hardt** (task `ent_5596305f0d50c7fc2fb7c56f`).

## Phases status (current)

| Phase | Status |
|------|--------|
| 1. Reconnaissance | done |
| 2. Identity provisioning (ES256 + JWKS + agent metadata) | done |
| 3. Wire signing logic into proxy | done |
| 4. Cutover and validation | done (`tier: software`, admitted via grant) |
| 5. Brief Dick Hardt | drafted (`execution/reports/aauth/brief_for_dick_hardt_2026-04-27.md`) |
| 6. (Optional) YubiKey FIDO2 hardware-tier path | **scoped, deferred** (this report) |
