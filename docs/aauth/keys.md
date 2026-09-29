# AAuth keypair layout and rotation

Each daemon has its own ES256 P-256 keypair stored in `ateles-private/keys/`.
Two signers use it. `lib/daemon_runtime/aauth_httpsig.py` (and the node helper
behind `neotoma_signed.signed_request`) sign RFC 9421 requests for which Neotoma
verifies key possession and stamps the sub the request claims as `agent_sub`
(a self-asserted sub, stamped whether or not a grant admits it; see "What
verification proves" below). `lib/daemon_runtime/aauth_signer.py`
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
RFC 9421 path and Neotoma verifies key possession, Neotoma stamps the `sub` the
request claims as the `agent_sub` provenance field, whether or not a grant admits
the request. That value is a self-asserted claim on a request whose key possession
was proven, not a proof of which agent sent it; admission is a separate step that
binds the key (its thumbprint), not the sub. The JWT-only `aauth_signer.py` path is not verified by Neotoma and does
not produce an `agent_sub`.

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
- never prints the private scalar `d` or the public coordinates; it does print the
  key's RFC 7638 **thumbprint** (a SHA-256 hash of the public key, public
  information), which is the value a grant pins.

### Order of steps

The same order as `docs/aauth.md` ("Identity provisioning"), which has the
exact commands:

1. **Mint** the key (this script).
2. **Register** the `agent_grant` matching `<name>@ateles-swarm` **and pinning the
   key's thumbprint** (`match_thumbprint`), which the script prints (operator). On
   current Neotoma a grant without the thumbprint admits nothing.
3. **Check** the pin exists: `neotoma request --operation listAgentGrants --query '{"q": "<thumbprint>", "status": "active"}'`.
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
grant pins this key, and it does not exercise the dispatcher's gate write-back, which
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
   `--force`. **A rotation needs a grant update.** Admission is key-bound (see
   "What each grant shape admits" in `docs/aauth.md`): the existing grant pins the
   OLD key's thumbprint, so it does not admit the new key, and signed writes as
   this role are refused until a grant pins the new thumbprint. Either update the
   existing grant (`updateAgentGrant`, body `{"match_thumbprint": "<new
   thumbprint>"}`, path `id` from `listAgentGrants`) or create a new pinned grant
   and revoke the old one. The script's closing hint prints both. Then run the
   pin check (step 3 of the order above) and the verify step.
2. JWKS: **nothing to do today.** No JWKS is published for daemon keys and
   Neotoma verifies from the inline key, so there is nothing to add. Once daemon
   keys are published to a JWKS, add the new `kid` here.
3. Restart the daemon. `aauth_signer.py` probes `<name>.jwk.json` first so the
   new key is picked up automatically.
4. After confirming the daemon signs correctly, delete the old `<name>.json`.
5. Once daemon keys are published to a JWKS, remove the old `kid` after the
   observation expiry window (5 min). Until then there is nothing to remove.

Rotate keypairs at least quarterly or immediately on suspected compromise.

### If a key is compromised

Rotating the key is not enough by itself, and the order matters. On current
Neotoma the old key stops being admitted only when the grant that pins it stops
admitting it:

1. **Revoke the grant that pins the compromised key.** Find it with
   `neotoma request --operation listAgentGrants --query '{"q": "<old thumbprint>", "status": "active"}'`
   (each result has a `grant_id`), then revoke it with the `revokeAgentGrant`
   operation, whose path parameter is `id`. CLI shape:
   `neotoma request --operation revokeAgentGrant --path '{"id": "<grant_id>"}'`.
   (Shape taken from the CLI source and `openapi.yaml`, not run against a live
   instance, so check it on a non-critical grant first.)
2. **Then register a grant pinning the fresh key** (mint with `--force`, then
   `createAgentGrant` with the new thumbprint, or `updateAgentGrant` on a grant
   you have not revoked).
3. **Between steps 1 and 2 signed writes as this role fail closed** (they are
   refused as unadmitted). That is the safe direction: finish step 2 promptly.
4. **The old key still verifies.** Revoking the grant stops admission, not
   signature verification: a request signed with the old key still verifies, and
   Neotoma still stamps its claimed `sub` on it as unadmitted attribution. Do not
   treat a stamped `agent_sub` as proof the current key wrote something.

Releases up to and including `v0.23.1` also matched a grant on the sub alone, so
on those a replacement grant that names only the sub would readmit the old key.
Always pinning the thumbprint is correct on both.

## JWKS endpoint (planned)

A future `ateles-private/keys/jwks.json` is planned to aggregate all public
keys, to be served at `https://ateles.markmhendrickson.com/.well-known/jwks.json`
(Phase 6). It is not needed for Neotoma to verify RFC 9421 requests, which carry
their public key inline (see below).

### What Neotoma verifies today

- Requests signed with the **RFC 9421 signer** (`lib/daemon_runtime/aauth_httpsig.py`
  and the node helper behind `neotoma_signed.signed_request`, using the canonical
  `<name>.jwk.json`) **are cryptographically verified server-side** by Neotoma's
  AAuth middleware (`src/middleware/aauth_verify.ts`), and are then admitted only
  by an active `agent_grant` that pins the signing key's thumbprint.
  `verify_aauth_signer.py --live` checks the signature path only.
- The lighter `X-AAuth-Token` JWT produced by `lib/daemon_runtime/aauth_signer.py`
  has no consumer in Neotoma's source or docs, so treat it as attribution
  metadata, not a verified credential. Requests without an RFC 9421 signature
  authenticate with the operator-configured bearer token.
- Neotoma verifies an RFC 9421 request against the public key the request
  carries inline (the `Signature-Key` header holds an `aa-agent+jwt` whose
  `cnf.jwk` binds the signing key). It does not read `ateles-private/keys` and
  needs no published JWKS to do so. Verification is separate from admission:
  admission needs an active `agent_grant` whose `match_thumbprint` equals the
  signing key's thumbprint.
- **What verification proves.** It proves the sender holds the private key
  matching the public key the request carries. It does **not** prove who the
  agent is: `sub` and `iss` are self-asserted (Neotoma decodes the agent-token JWT
  without checking the JWT's own signature and takes the key from its `cnf.jwk`),
  and a verified request is stamped with its claimed `agent_sub` whether or not
  any grant admits it. What admits a request is the key: on current Neotoma a
  grant admits only the key its `match_thumbprint` names, and a grant that names
  only a sub (or a sub and an iss) admits nothing (`grant_key_unbound`). Releases
  up to `v0.23.1` also matched on sub. See "What each grant shape admits" in
  `docs/aauth.md`, which is the single statement of this and cites the Neotoma
  change (#2506) and its version state.
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
