# `secrets/` — encrypted secret snapshots

Files here are **SOPS-encrypted** snapshots of secrets whose canonical values
live in **1Password**. They exist so daemons, CI, and other machines can read
secrets **offline** (no live `op signin` session required).

| File | Committed? | Contents |
|------|-----------|----------|
| `manifest.env-map.json` | yes | env var → 1Password reference map (no values) |
| `*.sops.env` | yes | age-encrypted dotenv snapshots — values encrypted, keys readable |
| anything plaintext (`*.plain.env`, `*.decrypted`) | **no** (gitignored) | never commit |

## Lifecycle

```
1Password (canonical)  ──publish──▶  secrets/<name>.sops.env (git)  ──materialize──▶  ~/.config/neotoma/.env
```

- **Publish** (you, when a secret changes — needs a live 1P session):
  `python execution/scripts/secrets_publish.py`
- **Materialize** (each machine / daemon start — offline, no 1P session):
  `python execution/scripts/secrets_materialize.py`

See [docs/secrets_management.md](../docs/secrets_management.md) for the full
runbook including one-time per-machine bootstrap and key rotation.
