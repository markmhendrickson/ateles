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

Order of operations, every provider: create/accept the new key → verify it
with a harmless read-only call → only then write it to 1Password → only if
you pass `--revoke-old`/`--archive-old`, revoke the old one → run
`secrets_publish.py` + `secrets_materialize.py` for **every** manifest file
block that reads the rotated variable from the 1Password item you passed as
`--op-item-ref` (derived from `manifest.env-map.json`, e.g. both `neotoma` and
`openclaw` for `ANTHROPIC_API_KEY`; a block reading the same variable from a
different item is named but not republished) → print the
required commit-and-push of the updated snapshots in `ateles-private`, and the
daemons/plists that read that var (restart those, not all of them). If
verification fails (after a few short retries for transient errors while a
fresh key propagates), nothing is written to 1Password, nothing downstream runs,
and the old key is left alone — so 1Password never disagrees with the SOPS
snapshot and the running services. For OpenAI and ElevenLabs the key already
minted at the provider is left there, and its id is printed so you can delete
it in the provider's dashboard (re-running the script would mint another).

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
mint the key yourself in the Anthropic console first — **inside a workspace**.
A key minted outside any workspace fails verification with HTTP 400 ("not
scoped to a workspace … must include the anthropic-workspace-id header"), and
the services that read `ANTHROPIC_API_KEY` (Claude Code, the SDK, LiteLLM)
never send that header, so the script refuses it and tells you to re-mint
rather than writing it anywhere. Then run:

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

Every run ends by publishing and materializing each snapshot that carries the
var, printing the `git add … && git commit … && git push` you must run in
`ateles-private` (the script does not push for you), and
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

## Swarm-run rotation of shared internal secrets

Operator ruling (`conformance.md` register row 118 / `authority_model.md#grants`,
2026-09-26, decision `credential_rotation_split_by_issuer`): a credential the
swarm both issues and consumes — a shared secret internal to the record, an
agent's signing key — is rotated by the swarm **unattended**, through the
dual-admit staging `authority_model.md#grants` already requires, plus a live
verification before the old value retires. A failed (or unrunnable) check is
not a hold: the rotation rolls back automatically, and either outcome is
written for the operator to read rather than approved in advance.

`execution/scripts/rotate_swarm_secret.py` covers the two shared secrets named
as first users of that ruling — `APIS_GITHUB_WEBHOOK_SECRET` (issuer: the
GitHub webhook on each pipeline repo) and `APIS_APPROVE_EMAIL_SECRET`
(internal-only: `apis` checks it, `turdus` presents it) — both checked by
`apis`'s `execution/daemons/apis/github_gateway.py`.

### Where these two secrets live

They are ordinary members of this pipeline, in their **own manifest block,
`apis`**: canonical in 1Password, mapped by `manifest.env-map.json`'s `apis`
block (target `~/.config/neotoma/.env`), snapshotted to `secrets/apis.sops.enc`,
and materialized into the dotenv `apis` and `turdus` load at startup. They are
not hand-maintained lines in that dotenv: materialize rewrites those lines in
place, so each is defined once. A block of their own keeps a rotation from ever
decrypting or re-encrypting the `neotoma` snapshot, which also carries provider
keys and a root secret (the Neotoma mnemonic). The script reads the 1Password
reference from the manifest at run time; nothing in this repo names the item.

One-time onboarding (already done on the operator's machine — repeat only for a
fork): create a 1Password item with one field per secret holding the current
value, add the `apis` block to the manifest, then `secrets_publish.py apis` and
`secrets_materialize.py apis` (0 changed means the dotenv already agreed).

### Sequence

`--dry-run` prints the same plan with every value shown as `<redacted>` and no
write, HTTP call, or `launchctl` call.

0. **Preflight** — refuse (exit `3`, nothing changed) unless
   `ATELES_ALLOW_DAEMON_RESTART=1` is set in the process's own environment, the
   manifest maps the target, the snapshot, the dotenv, and 1Password all hold
   the same current value, no `_NEXT` slot is left over from an earlier run,
   and — webhook only — `--i-know-this-calls-real-github`, a token, and at
   least one `--github-hook` are given.
1. **Generate** a new value in memory.
2. **Stage** it in the `_NEXT` slot (`APIS_GITHUB_WEBHOOK_SECRET_NEXT` /
   `APIS_APPROVE_EMAIL_SECRET_NEXT`) in the snapshot **and materialize it**, so
   the dotenv the daemon reads carries it. `github_gateway.make_app` admits
   both values during this overlap window.
3. **Restart `apis`** and read back a new pid that is still the same process
   after a settle delay. A restart is never reported from the `kickstart`
   exit code alone.
4. **Live check under dual-admit** — a probe the script signs itself (a signed
   `/github/webhook` ping, or an `/approve-email` call with a deliberately
   invalid body, where 400 means the secret passed). GitHub is not involved.
   Failure: **roll back** (exit `1`) — `_NEXT` removed from the snapshot and the
   dotenv (materializing alone only adds, so the line is removed explicitly),
   `apis` restarted.
5. **Write the canonical store** — 1Password, through the same template-file
   helper `rotate_provider_key.py` uses, then read back and compared in memory.
   Without this the next `secrets_publish.py` would restore the retired value.
   Failure: the old value is written back and the rotation rolls back.
6. **Update the issuer** (webhook only) — PATCH every `--github-hook`. Failure
   is a **checkpoint** (exit `2`), not a rollback: dual-admit stays on so no
   delivery is rejected whichever value GitHub holds, and 1Password is restored
   to the old value.
7. **Promote** — the new value becomes primary and `_NEXT` is removed, in the
   snapshot and the dotenv. Daemons that **present** the secret restart first
   (`turdus`, for the approve secret), then `apis`, each with a pid read-back,
   so there is no window in which `apis` admits only the new value while
   `turdus` still sends the old one.
8. **Post-promotion checks** — `apis` accepts the new value and **rejects the
   old one with 401** (it is genuinely retired); `turdus`, imported from the
   daemon checkout (`~/ateles-rc-src`) so it loads its configuration exactly as
   the daemon does, presents a value `apis` accepts; and, webhook only, a ping
   GitHub itself signs is delivered with 200 (read back from the hook's
   deliveries). Any failure puts the retired value back in `_NEXT`, restarts
   `apis`, and raises a checkpoint, so a consumer still on the old value keeps
   working while someone looks.

```bash
python execution/scripts/rotate_swarm_secret.py approve_email_secret --dry-run
ATELES_ALLOW_DAEMON_RESTART=1 \
  python execution/scripts/rotate_swarm_secret.py approve_email_secret
ATELES_ALLOW_DAEMON_RESTART=1 \
  python execution/scripts/rotate_swarm_secret.py github_webhook_secret \
  --github-hook <owner>/<repo>:<hook_id> --github-hook <owner>/<repo>:<hook_id> \
  --github-token-gh-user <login> --i-know-this-calls-real-github
```

`--github-token-gh-user` reads that login's `gh` keyring token in-process and
refuses it unless `GET /user` resolves to the same login; `--github-token-ref`
takes an `op://` reference instead. Hook ids come from
`gh api repos/<owner>/<repo>/hooks` (GitHub never returns the secret itself).

**Verify it worked.** The outcome line reads `rolled forward`, `rolled back`,
`CHECKPOINT`, or `refused at preflight`, matching exit codes `0`/`1`/`2`/`3`.
After any run that changed the snapshot, commit `secrets/apis.sops.enc` in the
private repo (the script prints the command). Then record the rotation on the
`credential` registry row (`last_rotated_at`). Nothing this script does prints
a secret value.

**Ordering, kept from the 2026-09-26 review of PR #1306:** the issuer is
updated only after the self-signed dual-admit probe passes, so a failed probe
can never leave GitHub and the daemon disagreeing — GitHub was never told.

The agent AAuth signing keys named in the same ruling are **not** covered by
this script; the credential registry entry for
`AAUTH_SIGNING_KEY_APUS`/`AAUTH_SIGNING_KEY_FORMICA` records the manual
dual-admit procedure proven 2026-09-25.

## Credential registry (Neotoma)

The `credential` entity type (registered additively, schema v1.0, activated
2026-09-26) is the record of which credentials exist, never their values:
`env_var_name`, `issuer_class` (`swarm_issued_and_consumed` /
`provider_minted` / `root`, per conformance.md row 118's three-way split),
`consumers`, `rotation_owner`, `rotation_method`, `last_rotated_at`. Rows
seeded so far: `APIS_GITHUB_WEBHOOK_SECRET`, `APIS_APPROVE_EMAIL_SECRET`,
`AAUTH_SIGNING_KEY_APUS`, `AAUTH_SIGNING_KEY_FORMICA` — the credentials named
as first users of the swarm-run rotation ruling. Query it with
`retrieve_entities(entity_type="credential")` on the operator Neotoma
instance; add a row whenever a new swarm-issued-and-consumed credential is
minted, never a value.

### Prior-art check (why a new type, not an existing one)

Before registering, five existing Neotoma schemas were checked for fit. None
carries an issuer class, a consumer list, or a rotation method together, which
is the specific gap this registry exists to close — each comparison below
states the actual fields checked, not just the verdict, per arch review on
PR #1306 (the reasoning belongs in this doc, not only in a PR thread that
scrolls away — CLAUDE.md: "a hand-maintained view of a moving register is
wrong by construction," applied here to the durability of a design decision
rather than to a plan field).

- **`credential_health`** — fields: `service`, `kind`, `used_by`,
  `check_method`, `status`, `last_verified_at`, `last_checked_at`,
  `expires_at`, `reauth_action`, `visibility`. This is a **liveness monitor**
  for external sessions (OAuth tokens, browser sessions) — it answers "is
  this credential currently working" via a probe (`check_method`) and what to
  do if not (`reauth_action`), for a single `used_by` consumer. It has no
  field for who is *authorized* to rotate a credential (`rotation_owner`),
  no field for the rotation *procedure* (`rotation_method`), no field for
  *multiple* consumers, and no `issuer_class` distinguishing swarm-issued
  from provider-minted from root. Ruled out: this type answers "is it alive
  right now", the registry answers "who may replace it and how."
- **`credential_reference`** — fields: `canonical_name`, `role`,
  `sender_kind`, `content`, `turn_key`, `service`, `purpose`,
  `storage_location`, `bot_username`, `bot_id`, `chat_id`,
  `env_var_token`, `env_var_chat_id`. Despite the name, this is a
  **Telegram-bot-specific** shape — `bot_id`, `chat_id`, `turn_key`,
  `sender_kind` are all chat-bot concepts with no analog for a webhook
  secret or a signing key. It has no `issuer_class`, `consumers` (plural),
  `rotation_owner`, or `rotation_method` field either. Ruled out: not a
  general credential shape at all, and even setting that aside, missing
  every field this registry needs.
- **`vendor_binding`** — fields: `capability`, `vendor`, `tool_namespace`,
  `credential_location`, `fallback`, `constraints`, `notes`, `visibility`.
  This is the closest of the five, and the one CLAUDE.md itself names as the
  home for "third-party tools" and "channels" config, so it deserved the
  most scrutiny. `credential_location` states *where* a credential lives
  ("generic", per its own description — e.g. an env-key name), which
  overlaps `env_var_name`, but the type's whole shape is oriented around
  **a capability slot filled by a vendor** (`capability` + `vendor` +
  `tool_namespace` is the identity), not around a credential's own
  lifecycle. It has no `issuer_class`, no `rotation_owner`, no
  `rotation_method`, no `last_rotated_at`. Overloading it would mean adding
  four rotation-specific fields to a type whose canonical identity
  (`capability`) is not a credential at all — `APIS_GITHUB_WEBHOOK_SECRET`
  and `APIS_APPROVE_EMAIL_SECRET` are not "vendors filling a capability
  slot," they are internal shared secrets with no vendor on either end.
  Ruled out: right neighborhood, wrong identity axis — a `vendor_binding`
  row answers "which tool did we pick for this job," not "who may rotate
  this credential and how."
- **`deployment_configuration`** — fields: `system`, `project`,
  `environment`, `variables_added`, `variables_pending`, `fly_app`,
  `public_domain`, `region`, `deploy_branch`, `build_args`,
  `deploy_command`, plus (per its own doc) `gotchas`. `variables_added` /
  `variables_pending` record env var **names** already configured for a
  hosted instance, which is the same "names only, never values" discipline
  this registry follows, but the type's identity is a **deployment target**
  (`system` + `project` + `environment`), not a credential — a row exists
  per Fly app / environment, not per credential, and there is no
  `issuer_class`, `consumers` list, `rotation_owner`, or `rotation_method`
  anywhere in it. `APIS_GITHUB_WEBHOOK_SECRET` is not scoped to one
  deployment; it is consumed by `apis` wherever that daemon runs. Ruled out:
  answers "what does this deployment need configured," not "who owns
  rotating this specific credential."
- **`env_var_mappings`** — fields: `env_var`, `op_reference`, `vault`,
  `item_name`, `service`, `is_optional`, `notes`. This is the mirror of
  `ateles-private/secrets/manifest.env-map.json` — it maps an env var name to
  **where its value is sourced from in 1Password**, for the publish/
  materialize flow this same doc describes above. It is the type structurally
  closest to "just an env var name," which made it worth checking carefully,
  but it has no `issuer_class`, `consumers`, `rotation_owner`, or
  `rotation_method` field, and its purpose (source-of-truth location) is
  orthogonal to this registry's purpose (who may rotate it and how) — a
  credential can appear in `env_var_mappings` and still need a `credential`
  row, and vice versa (an AAuth signing key has no 1Password `op_reference`
  at all; it lives in `ateles-private/keys/`). Ruled out: answers "where do I
  read this value from," not "who may replace it."

**Conclusion:** no existing type carries `issuer_class` + `consumers` +
`rotation_owner` + `rotation_method` together, which is the minimum a
rotation script needs to know before it acts. Registering `credential`
additively (no existing schema modified) closes that gap without duplicating
any of the five above.

## Security properties

- Secret values never live in git in plaintext, never printed by these scripts.
- Only the age private key (in 1Password + bootstrapped per machine) can decrypt.
- Forkability: a forker generates their own age key, stores it in their own
  vault, supplies their own `manifest.env-map.json`, and never touches your
  secrets.
