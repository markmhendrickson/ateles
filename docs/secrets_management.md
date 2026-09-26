# Secrets management (SOPS + age, sourced from 1Password)

Ateles runs across multiple machines and unattended daemons. This is how secrets
reach all of them **without a live 1Password session** at runtime.

> **The encrypted snapshots live in the PRIVATE `ateles-private` repo, NOT here.**
> This `ateles` repo is public, so it holds only the no-secret tooling
> (`execution/scripts/secrets_*.py`). The `.sops.yaml`, manifest, and
> `secrets/*.sops.enc` snapshots live in `ateles-private` (cloned to
> `~/repos/ateles-private`; override with `ATELES_SECRETS_DIR`). Snapshots stay
> age-encrypted even in the private repo for defense-in-depth.

## Design (Design B — 1Password canonical)

```
1Password (CANONICAL values)
        │  publish  (you, when a secret changes; needs `op signin`)
        ▼
ateles-private/secrets/<name>.sops.enc  ── age-encrypted, committed to PRIVATE git ──┐
        │  git pull                                                                  │
        ▼                                                                            │
every machine / CI                                                                   │
        │  materialize  (offline; uses machine-local age key)                        │
        ▼                                                                            │
<block target, e.g. ~/.config/neotoma/.env>  ──▶  daemons read it (no 1P session) ◀──┘
```

- **1Password** is the source of truth and stores **one extra item**: the age
  **private key**. 1Password Family sync puts that item on every machine.
- **git** carries the encrypted snapshots (`ateles-private/secrets/*.sops.enc`).
  Values are encrypted; keys stay readable. The private repo + encryption are
  belt-and-suspenders.
- **age** does the crypto. The **public** key (in `ateles-private/.sops.yaml`)
  only encrypts; only the **private** key (in 1Password) decrypts.

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

**a. Clone the private secrets repo** (where the snapshots live):

```bash
git clone git@github.com:markmhendrickson/ateles-private.git ~/repos/ateles-private
```

**b. Place the age key.** 1Password Family already synced the key item to this
machine (or, if `op` isn't installed here, `scp` `~/.config/sops/age/keys.txt`
from a machine that has it):

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
git add secrets/*.sops.enc && git commit -m "chore(secrets): rotate <var>"
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

1. **Offline SOPS decrypt** of `secrets/neotoma.sops.enc` (repo-relative) or
   `~/.config/neotoma/secrets/neotoma.sops.enc` — no 1Password session.
2. **Fallback: live `op read`** — only if SOPS/age is unavailable or the snapshot
   is missing (kept for migration; remove once every machine is bootstrapped).

If neither yields a token, the daemon proceeds with whatever is already in
`.env`. This makes the migration safe: nothing breaks before bootstrap.

> Deployment note: isolated daemon checkouts must include `secrets/*.sops.enc`,
> **or** you place the snapshot at `~/.config/neotoma/secrets/neotoma.sops.enc`
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
  `sops updatekeys secrets/*.sops.enc`, commit, then re-run the per-machine
  bootstrap (step above) on each box. To revoke a machine, rotate the age key so
  its old `keys.txt` can no longer decrypt new snapshots.

### Rotating a provider API key (openai / elevenlabs / anthropic)

Operator ruling (master plan `ent_81aadb43caf2fa493361e8ed`, decision
`credential_rotation_split_by_issuer`): the operator mints provider API keys —
everything downstream (1Password write, verification, publish, materialize,
daemon restarts) belongs to the swarm. `execution/scripts/rotate_provider_key.py`
is the operator-run script that does both halves in one pass, but it is
**operator-run, not swarm-dispatched** — it talks to your 1Password vault and
to the provider's admin API, so run it yourself rather than handing it to an
agent.

It never accepts a secret value on the command line — admin keys are read
from a 1Password **reference** (`op://vault/item/field`) you pass as an
argument, and the new value is written back to 1Password by piping a JSON
template to `op item edit`'s stdin, never as a CLI assignment. The Anthropic
path (no create endpoint exists) prompts for the new key with `getpass`
(hidden, not echoed) instead.

Order of operations, every provider: create/accept the new key → write it to
1Password → verify it with a harmless read-only call → only then, and only if
you pass `--revoke-old`/`--archive-old`, revoke the old one → run
`secrets_publish.py` + `secrets_materialize.py` → print the daemons/plists
that read that var (restart those, not all of them). If verification fails,
nothing downstream runs and the old key is left alone.

**OpenAI** — creates a new project service account + key
(`POST /v1/organization/projects/{project_id}/service_accounts`), verifies
with `GET /v1/models`:

```bash
python execution/scripts/rotate_provider_key.py openai \
  --admin-key-ref op://Private/<openai-admin-item>/<field> \
  --project-id proj_abc123 \
  --op-item-ref op://Private/<item-that-holds-OPENAI_API_KEY>/<field>
```

Verify it worked: the command's own `GET /v1/models` check must print
"new key verified." before anything else runs; independently, `op item get
<item> --fields label=<field>` (no `--reveal` needed to confirm the edit
timestamp changed) or check the 1Password item's modified time.

Once you've confirmed the new key works, revoke the OLD service account on a
second run:

```bash
python execution/scripts/rotate_provider_key.py openai \
  --admin-key-ref op://Private/<openai-admin-item>/<field> \
  --project-id proj_abc123 \
  --op-item-ref op://Private/<item-that-holds-OPENAI_API_KEY>/<field> \
  --revoke-old <OLD_SERVICE_ACCOUNT_ID>
```

**ElevenLabs** — creates a new service-account API key
(`POST /v1/service-accounts/{service_account_user_id}/api-keys`), verifies
with `GET /v1/user`:

```bash
python execution/scripts/rotate_provider_key.py elevenlabs \
  --admin-key-ref op://Private/<elevenlabs-admin-item>/<field> \
  --service-account-user-id user_abc123 \
  --op-item-ref op://Private/<item-that-holds-ELEVENLABS_API_KEY>/<field> \
  --permission speech_to_text
```

Verify it worked: same "new key verified." line, backed by the `GET /v1/user`
call; the response's account details should match your ElevenLabs
account. `--permission` is repeatable; omit it to default to
`speech_to_text` only (least privilege for tyto's diarization use).

Once confirmed, delete the OLD key on a second run:

```bash
python execution/scripts/rotate_provider_key.py elevenlabs \
  --admin-key-ref op://Private/<elevenlabs-admin-item>/<field> \
  --service-account-user-id user_abc123 \
  --op-item-ref op://Private/<item-that-holds-ELEVENLABS_API_KEY>/<field> \
  --revoke-old <OLD_KEY_ID>
```

**Anthropic** — no create endpoint exists in the Admin API docs
(`platform.claude.com/docs/en/api/admin-api/apikeys`, checked 2026-09-26), so
mint the key yourself in the Anthropic console first, then run:

```bash
python execution/scripts/rotate_provider_key.py anthropic \
  --op-item-ref op://Private/<item-that-holds-ANTHROPIC_API_KEY>/<field>
# prompts: Paste the new ANTHROPIC key (input hidden, not echoed):
```

Verify it worked: the same "new key verified." line, backed by `GET
/v1/models` using the pasted key. Once confirmed, archive the OLD key (visible
in the Anthropic console as `api_key_id`) in a second run:

```bash
python execution/scripts/rotate_provider_key.py anthropic \
  --op-item-ref op://Private/<item>/<field> \
  --admin-key-ref op://Private/<anthropic-admin-item>/<field> \
  --archive-old apikey_01XXXXXXXXXXXXXXXXXXXXXXXX
```

Anthropic has no delete endpoint — archive/inactive is the only lifecycle
action — so this is the terminal step for that provider.

Every run ends by publishing the encrypted snapshot, materializing it, and
printing the daemons/plists known to consume that var (e.g. `com.ateles.apis`
for `ANTHROPIC_API_KEY`) so you know what to restart; pass `--no-downstream`
to skip that and do it by hand. Nothing this script does prints a key value —
its final summary reports only ids/hints.

## Agent signing keys (encrypted backup)

Each swarm agent signs its Neotoma requests with its own AAuth key,
`ateles-private/keys/<agent>.jwk.json`. Those plaintext files are gitignored and
exist only on the machine that runs the daemons, and `agent_grant` entities pin
each key's thumbprint, so losing a file means minting a new key and re-pinning
the grant. The backup is an age-encrypted copy of every `keys/*.json` file,
committed to `ateles-private/keys/encrypted/<stem>.sops.json` with the same age
recipient as the secrets snapshots (the `keys/encrypted/` rule in
`ateles-private/.sops.yaml`). Files are encrypted in sops binary mode, so a
restore reproduces the original bytes exactly.

```bash
python execution/scripts/secrets_keys.py backup     # after minting or rotating a key
python execution/scripts/secrets_keys.py verify     # decrypt in memory, compare SHA-256
python execution/scripts/secrets_keys.py restore    # on a new machine (0600, no overwrite)
python execution/scripts/secrets_keys.py prune      # drop copies of keys removed from disk
cd ~/repos/ateles-private && git add keys/encrypted && git commit -m "chore(keys): back up <agent> key"
```

`backup` leaves an encrypted copy alone when it still decrypts to the current
bytes, so committed ciphertext only changes when a key does. `restore --force`
overwrites an existing plaintext file. Like the snapshots, decryption needs only
the machine-local age key; no script here prints key material.

## Security properties

- Secret values never live in git in plaintext, never printed by these scripts.
- Only the age private key (in 1Password + bootstrapped per machine) can decrypt.
- Forkability: a forker generates their own age key, stores it in their own
  vault, supplies their own `manifest.env-map.json`, and never touches your
  secrets.
