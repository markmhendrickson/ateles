# AAuth keypair layout and rotation

Each daemon has its own ES256 P-256 keypair stored in `ateles-private/keys/`.
Two signers use it. `lib/daemon_runtime/aauth_httpsig.py` (and the node helper
behind `neotoma_signed.signed_request`) sign RFC 9421 requests that Neotoma
verifies and attributes to the daemon (`agent_sub`). `lib/daemon_runtime/aauth_signer.py`
attaches an `X-AAuth-Token` JWT that Neotoma does not verify today, so it gives
no verified attribution. See "What Neotoma verifies today" below.

## Canonical format (preferred)

File path: `ateles-private/keys/<name>.jwk.json`
File mode: `0600`

```json
{
    "sub": "monedula@ateles-swarm",
    "kid": "<16-byte base64url random>",
    "kty": "EC",
    "crv": "P-256",
    "x": "<base64url encoded public x>",
    "y": "<base64url encoded public y>",
    "d": "<base64url encoded private scalar>"
}
```

The `sub` value is `<genus>@ateles-swarm`. When a request is signed on the
RFC 9421 path and Neotoma verifies it, Neotoma records the `sub` as the
`agent_sub` provenance field. The JWT-only `aauth_signer.py` path is not verified
by Neotoma and does not produce a verified `agent_sub`.

## Legacy format (still supported)

File path: `ateles-private/keys/<name>.json`

```json
{
    "sub": "monedula@ateles-swarm",
    "key_id": "<kid>",
    "algorithm": "ES256",
    "private_key_pem": "-----BEGIN EC PRIVATE KEY-----\n...",
    "public_key_pem": "-----BEGIN PUBLIC KEY-----\n..."
}
```

`lib/daemon_runtime/aauth_signer.py` probes `<name>.jwk.json` first and falls
back to the legacy `<name>.json`; the file's shape (`kty` and `d` for JWK,
otherwise PEM) selects the loader. Legacy files at the old path continue to work
for that signer during migration.

Not every consumer falls back: `lib/daemon_runtime/neotoma_signed.py`'s
`agent_identity()` (used for the dispatcher-signed gate write) reads **only**
`<name>.jwk.json`. A role with only a legacy `<name>.json` has no identity there
and that signed write fails closed, so mint the canonical file for any role that
needs it.

## Minting a new keypair

```bash
python execution/scripts/mint_daemon_keypair.py --name <daemon-name>
```

`<name>` must match `^[a-z][a-z0-9_-]{0,63}$` (lowercased first). The script:

- writes `ateles-private/keys/<name>.jwk.json`, created with mode 0600 at
  creation time via `O_CREAT|O_EXCL|O_NOFOLLOW` (no check-then-create race, and
  a symlink at the target is refused, never written through);
- creates the keys directory with mode 0700 if it does not exist;
- exits with an error if the file already exists. Deleting the file **or**
  passing `--force` destroys the previous private key irrecoverably, so neither
  is done casually;
- with `--force` (rotation) writes the new key to a temp file in the same
  directory (0600, fsync'd) and atomically `os.replace`s it onto the target: the
  old key survives a failed rotation, and the replacement is a new inode so a
  looser mode on the old file (e.g. 0644) is not inherited;
- never prints the private scalar `d` or the public coordinates.

### Order of steps

The same order as `docs/aauth.md` ("Identity provisioning"), which has the
exact commands:

1. **Mint** the key (this script).
2. **Register** the `agent_grant` matching `<name>@ateles-swarm` (operator).
3. **Check** the grant exists: `neotoma request --operation listAgentGrants --query '{"q": "<name>@ateles-swarm", "status": "active"}'`.
4. **Verify** the signer (below).
5. **Restart** the daemon so it picks up the new file.

### Verify the new key

```bash
python execution/scripts/verify_aauth_signer.py \
    --jwk ~/repos/ateles-private/keys/<name>.jwk.json --live <neotoma-base-url>
```

Prints only `status`, `signature_present`, `signature_verified`, an error code,
and `tier` (no key material) and exits non-zero unless the signature verified.
This proves the **Python signer and this key** produce a signature Neotoma
verifies (it signs with `iss` set to the role's `sub`). It does **not** prove a
grant matches, and it does not exercise the dispatcher's gate write-back, which
signs through the node helper in `neotoma_signed.signed_request`.

### Keys directory variables

`ATELES_PRIVATE_KEYS_DIR` is read by (including) this script, by
`lib/daemon_runtime/aauth_signer.py`, `skill_runner.py`, `secrets_lib.py` and
`ateles/config.py`. This script's `--keys-dir <dir>` flag overrides it for one
run; `<dir>` must be the directory `ATELES_AAUTH_KEYS_DIR` names.
`ATELES_AAUTH_KEYS_DIR` is read by `neotoma_signed.agent_identity()` (the
dispatcher's gate write-back). **They must point at the same directory**, or the
script mints where the dispatcher does not look and the signed write fails
closed.

### Directory and file-system caveats

- The keys directory is created 0700 **only when the script creates it**. A
  pre-existing directory keeps whatever mode it has; check it yourself.
- `O_NOFOLLOW` guards only the final path component. If the keys directory
  itself (or a parent) is a symlink, it is followed.
- A rotation leaves a `.<name>.jwk.json.<hex>.tmp` file only if the process is
  killed mid-write. Ignore `keys/.*.tmp` and `keys/*.tmp` in `ateles-private`
  (`.gitignore`) so a stray temp key cannot be committed.

## Existing keypairs

| Daemon | File | Format |
|--------|------|--------|
| monedula | `monedula.json` | legacy PEM |
| neotoma_agent | `neotoma_agent.json` | legacy PEM |
| sylvia | `sylvia.json` | legacy PEM |
| ateles | `ateles.json` | legacy PEM |
| cicada | `cicada.json` | legacy PEM |
| apus | `apus.json` | legacy PEM |
| vanellus | `vanellus.json` | legacy PEM |
| formica | `formica.json` | legacy PEM |

This table is the **May 2026 record of legacy PEM files** (eight, including
`sylvia.json`; `docs/aauth.md`'s status table lists the same eight). It has
drifted: `ls ateles-private/keys/` (names only, never file contents) now shows a
canonical `<name>.jwk.json` for most of the roster, in some cases alongside the
legacy file. Treat `ls` as authoritative. Migrate to canonical JWK on next
rotation (see below), because `agent_identity()` reads only `<name>.jwk.json`.

## Rotation

0. **Only if you may need the old key** (for example, to roll back), copy the
   existing `<name>.jwk.json` aside first, to a mode-0600 file outside any git
   repo and outside any cloud-synced folder, and delete the copy once the new
   key is confirmed. For example:
   `(umask 077; cp -p ~/repos/ateles-private/keys/<name>.jwk.json <private-dir>/<name>.jwk.json.old)`
   or `install -m 600 ~/repos/ateles-private/keys/<name>.jwk.json <private-dir>/<name>.jwk.json.old`.
   `--force` destroys the old private key irrecoverably; there is no undo.
1. Run `mint_daemon_keypair.py --name <daemon>` — this writes `<name>.jwk.json`.
   If a `<name>.jwk.json` already exists (rotating a canonical key), add
   `--force`. An unpinned grant matches `(sub, iss)` (no `match_thumbprint`), so
   a rotation does not need a new grant; still run the check (step 3 of the order
   above) and then the verify step.
2. JWKS: **nothing to do today.** No JWKS is published for daemon keys and
   Neotoma verifies from the inline key, so there is nothing to add. Once daemon
   keys are published to a JWKS, add the new `kid` here.
3. Restart the daemon. `aauth_signer.py` probes `<name>.jwk.json` first so the
   new key is picked up automatically.
4. After confirming the daemon signs correctly, delete the old `<name>.json`.
5. Once daemon keys are published to a JWKS, remove the old `kid` after the
   observation expiry window (5 min). Until then there is nothing to remove.

Rotate keypairs at least quarterly or immediately on suspected compromise.

If the **sub itself is suspect** (compromise, or the key may have been copied),
rotating the key is not enough: an unpinned grant admits any key that presents
the same `(sub, iss)`. Revoke or replace the `agent_grant` for that sub (via the
operator's Neotoma session), not just the key.

## JWKS endpoint (planned)

A future `ateles-private/keys/jwks.json` is planned to aggregate all public
keys, to be served at `https://ateles.markmhendrickson.com/.well-known/jwks.json`
(Phase 6). It is not needed for Neotoma to verify RFC 9421 requests, which carry
their public key inline (see below).

### What Neotoma verifies today

- Requests signed with the **RFC 9421 signer** (`lib/daemon_runtime/aauth_httpsig.py`
  and the node helper behind `neotoma_signed.signed_request`, using the canonical
  `<name>.jwk.json`) **are cryptographically verified server-side** by Neotoma's
  AAuth middleware (`src/middleware/aauth_verify.ts`), and can then be admitted
  by a matching active `agent_grant`. `verify_aauth_signer.py --live` checks this
  signature path.
- The lighter `X-AAuth-Token` JWT produced by `lib/daemon_runtime/aauth_signer.py`
  has no consumer in Neotoma's source or docs, so treat it as attribution
  metadata, not a verified credential. Requests without an RFC 9421 signature
  authenticate with the operator-configured bearer token.
- Neotoma verifies an RFC 9421 request against the public key the request
  carries inline (the `Signature-Key` header holds an `aa-agent+jwt` whose
  `cnf.jwk` binds the signing key). It does not read `ateles-private/keys` and
  needs no published JWKS to do so. Verification is separate from admission:
  admission still needs an active `agent_grant` matching `(sub, iss)`.
- **What verification proves.** It proves the sender holds the private key
  matching the public key the request carries. It does **not** prove who the
  agent is: `sub` and `iss` are self-asserted (Neotoma decodes the agent-token JWT
  without checking the JWT's own signature and takes the key from its `cnf.jwk`),
  and grants match on `(sub, iss)`. Only a grant's optional `match_thumbprint` ties
  an identity to a specific key; an unpinned grant admits any ES256 key that
  presents the matching `(sub, iss)`.
- A daemon that only sends the JWT-only `X-AAuth-Token` is therefore **not
  verified** by Neotoma today.

## Security notes

- Keys are stored in `ateles-private` (private repo), never in `ateles` (public).
- Files must be mode 0600. `lib/daemon_runtime/aauth_signer.py` does not refuse
  a looser mode at load time, but it logs a warning
  (`_warn_if_key_world_readable`) and keeps signing; `mint_daemon_keypair.py`
  creates the file at 0600 (and the keys directory at 0700 when it creates it).
- Never commit key files to any repo. `ateles-private/.gitignore` should
  exclude `keys/*.json` and `keys/*.jwk.json` (verify this is in place).
