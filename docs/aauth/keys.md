# AAuth keypair layout and rotation

Each daemon has its own ES256 P-256 keypair stored in `ateles-private/keys/`.
The keypair is used by `lib/daemon_runtime/aauth_signer.py` to sign outbound
Neotoma API requests, establishing per-daemon attribution on all observations.

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

The `sub` value is `<genus>@ateles-swarm`. It is stamped into every Neotoma
observation as the `agent_sub` provenance field, giving full per-agent audit.

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

Restart the daemon after minting so it picks up the new file.

### Verify the new key

```bash
python execution/scripts/verify_aauth_signer.py \
    --jwk ~/repos/ateles-private/keys/<name>.jwk.json --live <neotoma-base-url>
```

Prints only `status`, `signature_present`, `signature_verified`, an error code,
and `tier` (no key material) and exits non-zero unless the signature verified.
A verified signature is not the same as admission: the role also needs an
active `agent_grant` matching `<name>@ateles-swarm`.

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

Migrate to canonical JWK on next rotation (see below).

## Rotation

1. Run `mint_daemon_keypair.py --name <daemon>` — this writes `<name>.jwk.json`.
   If a `<name>.jwk.json` already exists (rotating a canonical key), add
   `--force`; that destroys the old private key, so only do it once you intend
   to retire it. Then run the verify step above.
2. Add the new `kid` to the Neotoma JWKS endpoint (pending — see phase plan).
3. Restart the daemon. `aauth_signer.py` probes `<name>.jwk.json` first so the
   new key is picked up automatically.
4. After confirming the daemon signs correctly, delete the old `<name>.json`.
5. Remove the old `kid` from JWKS after the observation expiry window (5 min).

Rotate keypairs at least quarterly or immediately on suspected compromise.

## JWKS endpoint (planned)

A future `ateles-private/keys/jwks.json` will aggregate all public keys so
Neotoma and other verifiers can validate incoming JWTs without requiring
individual key distribution. The endpoint will be served at
`https://ateles.markmhendrickson.com/.well-known/jwks.json` (Phase 6).

Until then, Neotoma trusts the operator-configured bearer token; AAuth JWTs
provide per-agent attribution but are not yet cryptographically verified
server-side.

## Security notes

- Keys are stored in `ateles-private` (private repo), never in `ateles` (public).
- Files must be mode 0600; `aauth_signer.py` does not enforce this at load time
  but `mint_daemon_keypair.py` creates the file at 0600 (and the keys directory at 0700).
- Never commit key files to any repo. `ateles-private/.gitignore` should
  exclude `keys/*.json` and `keys/*.jwk.json` (verify this is in place).
