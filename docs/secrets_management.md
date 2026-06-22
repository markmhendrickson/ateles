# Secrets management (SOPS + age, sourced from 1Password)

Ateles runs across multiple machines and unattended daemons. This is how secrets
reach all of them **without a live 1Password session** at runtime.

## Design (Design B — 1Password canonical)

```
1Password (CANONICAL values)
        │  publish  (you, when a secret changes; needs `op signin`)
        ▼
secrets/<name>.sops.env   ── age-encrypted snapshot, committed to git ──┐
        │  git pull                                                     │
        ▼                                                               │
every machine / CI                                                      │
        │  materialize  (offline; uses machine-local age key)          │
        ▼                                                               │
~/.config/neotoma/.env  ──▶  daemons read it (no 1Password session) ◀──┘
```

- **1Password** is the source of truth and stores **one extra item**: the age
  **private key**. 1Password Family sync puts that item on every machine.
- **git** carries the encrypted snapshots (`secrets/*.sops.env`). Values are
  encrypted; keys stay readable. Safe to commit.
- **age** does the crypto. The **public** key (in `.sops.yaml`) only encrypts;
  only the **private** key (in 1Password) decrypts.

Why not a 1Password service account? Those require a Business/Teams plan. This
keeps everything on a Family plan: 1Password stores + syncs one root key; git
handles distribution; daemons decrypt offline.

## Prerequisites (every machine)

```bash
brew install sops age          # macOS; see getsops.io / age docs for others
```

## One-time setup (do once, on your primary machine)

1. **Generate the age keypair:**
   ```bash
   age-keygen -o ateles-age.key      # prints the PUBLIC key to stderr
   age-keygen -y ateles-age.key      # re-print the public key any time
   ```
2. **Store the PRIVATE key in 1Password** as a new item, e.g.
   `op://Private/ateles-sops-age/key`, pasting the full contents of
   `ateles-age.key`. Then delete the local file:
   ```bash
   rm ateles-age.key
   ```
3. **Put the PUBLIC key in `.sops.yaml`**, replacing the placeholder `age:` line.
   Commit it.
4. **Publish the first snapshot** (see below).

## Per-machine bootstrap (once per machine, ~10s)

1Password Family already synced the key item to this machine, so:

```bash
mkdir -p ~/.config/sops/age
op read "op://Private/ateles-sops-age/key" > ~/.config/sops/age/keys.txt
chmod 600 ~/.config/sops/age/keys.txt
```

The Ateles scripts and daemons standardize on `~/.config/sops/age/keys.txt`:
they auto-set `SOPS_AGE_KEY_FILE` to it when present, so no shell config is
needed for them. **But raw `sops` CLI on macOS defaults to a different path**
(`~/Library/Application Support/sops/age/keys.txt`), so for manual `sops`
commands, export the path in your shell profile:

```bash
echo 'export SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt' >> ~/.zshrc
```

After this, decryption is fully offline — no `op signin` needed again on this machine.

## Routine operations

**Publish** — after changing a secret value in the 1Password app
(needs a live `op` session):
```bash
python execution/scripts/secrets_publish.py            # all files in the manifest
python execution/scripts/secrets_publish.py neotoma    # one file
ENVIRONMENT=production python execution/scripts/secrets_publish.py
git add secrets/*.sops.env && git commit -m "chore(secrets): rotate <var>"
```

**Materialize** — on any machine, to refresh its local `.env` (offline):
```bash
git pull
python execution/scripts/secrets_materialize.py
```
Daemons also self-materialize at startup (see below), so often a restart is enough.

**Add a new secret:**
1. Create the item/field in 1Password.
2. Add the `env_var → op://…` entry to `secrets/manifest.env-map.json` (and the
   Neotoma `env_var_mapping` registry, which it mirrors).
3. `secrets_publish.py` → commit → `secrets_materialize.py` on each machine.

## How daemons consume secrets

`execution/daemons/{cyphorhinus,piculet}/watch.py` refresh
`NEOTOMA_BEARER_TOKEN` at startup by, in order:

1. **Offline SOPS decrypt** of `secrets/neotoma.sops.env` (repo-relative) or
   `~/.config/neotoma/secrets/neotoma.sops.env` — no 1Password session.
2. **Fallback: live `op read`** — only if SOPS/age is unavailable or the snapshot
   is missing (kept for migration; remove once every machine is bootstrapped).

If neither yields a token, the daemon proceeds with whatever is already in
`.env`. This makes the migration safe: nothing breaks before bootstrap.

> Deployment note: isolated daemon checkouts must include `secrets/*.sops.env`,
> **or** you place the snapshot at `~/.config/neotoma/secrets/neotoma.sops.env`
> (the second candidate path). Otherwise daemons silently fall back to `op read`.

## CI (GitHub Actions)

Store the age **private** key as a single repo secret `SOPS_AGE_KEY`, then:

```yaml
- run: |
    echo "$SOPS_AGE_KEY" > /tmp/age.key
    export SOPS_AGE_KEY_FILE=/tmp/age.key
    python execution/scripts/secrets_materialize.py --env-file .env
```

No 1Password in CI at all.

## Key rotation

- **Rotate a secret value:** change it in 1Password → `secrets_publish.py` →
  commit → materialize everywhere.
- **Rotate the age key:** `age-keygen` a new key, update the private item in
  1Password and the public key in `.sops.yaml`, run
  `sops updatekeys secrets/*.sops.env`, commit, then re-run the per-machine
  bootstrap (step above) on each box. To revoke a machine, rotate the age key so
  its old `keys.txt` can no longer decrypt new snapshots.

## Security properties

- Secret values never live in git in plaintext, never printed by these scripts.
- Only the age private key (in 1Password + bootstrapped per machine) can decrypt.
- Forkability: a forker generates their own age key, stores it in their own
  vault, supplies their own `manifest.env-map.json`, and never touches your
  secrets.
