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

`aauth_signer.py` auto-detects the format: if the file has `kty` and `d`
fields it is loaded as JWK; otherwise it is loaded as legacy PEM. Legacy files
at the old path continue to work during migration.

## Minting a new keypair

```bash
python execution/scripts/mint_daemon_keypair.py --name <daemon-name>
```

This writes `ateles-private/keys/<name>.jwk.json` with mode 0600 and exits
with an error if the file already exists (prevents accidental overwrite).

Restart the daemon after minting so it picks up the new file.

## Existing keypairs

| Daemon | File | Format |
|--------|------|--------|
| monedula | `monedula.json` | legacy PEM |
| neotoma_agent | `neotoma_agent.json` | legacy PEM |
| sylvia | `sylvia.json` | legacy PEM |
| onychomys | `onychomys.json` | legacy PEM |
| gryllus | `gryllus.json` | legacy PEM |
| apus | `apus.json` | legacy PEM |
| vanellus | `vanellus.json` | legacy PEM |
| formica | `formica.json` | legacy PEM |

Migrate to canonical JWK on next rotation (see below).

## Rotation

1. Run `mint_daemon_keypair.py --name <daemon>` — this writes `<name>.jwk.json`.
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
  but `mint_daemon_keypair.py` sets it on creation.
- Never commit key files to any repo. `ateles-private/.gitignore` should
  exclude `keys/*.json` and `keys/*.jwk.json` (verify this is in place).
