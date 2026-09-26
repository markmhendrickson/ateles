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
GitHub webhook config on `markmhendrickson/ateles` and
`markmhendrickson/neotoma`) and `APIS_APPROVE_EMAIL_SECRET` (internal-only,
between `apis` and `turdus`) — both consumed by `apis`'s
`execution/daemons/apis/github_gateway.py`. Sequence: generate a new value in
memory → stage it in the consumer's `_NEXT` slot (`APIS_GITHUB_WEBHOOK_SECRET_NEXT`
/ `APIS_APPROVE_EMAIL_SECRET_NEXT`), which `github_gateway.make_app` accepts
alongside the current value during the overlap window → restart the one
consuming daemon → send a real signed probe under the NEW value, which the
script signs **itself** (no GitHub involvement at all — dual-admit already
lets the daemon accept it) → **only if that probe passes**, and for the
webhook secret only, update the GitHub issuer to the new value → on success,
promote the new value to primary, retire the old one, clear `_NEXT`, restart
again; on a failed probe, clear `_NEXT` and leave the old value as primary
(rollback), restart again so the daemon returns to exactly its pre-rotation
state.

**Ordering fix (2026-09-26 review of PR #1306):** an earlier revision updated
the GitHub issuer *before* verifying, so a failed verification rolled the
daemon back to the old value while GitHub was left signing with the new
one — every real delivery would then fail signature verification (401) until
someone noticed and fixed GitHub's config by hand. Verifying first, with a
probe the script signs itself, means the daemon-side half of the rotation is
proven safe with zero GitHub involvement, so a failed probe can never leave
GitHub and the daemon disagreeing — GitHub was simply never told about the
new value. The one case this does not eliminate — the issuer update itself
failing (or timing out) *after* the probe already passed — is not a rollback
(the daemon-side change is proven good) and not a promotion (GitHub's actual
state is now unconfirmed): `run_rotation` raises a **checkpoint** instead,
leaving dual-admit ON (so no delivery is ever rejected either way) and
stopping rather than guessing. `RotationReport.checkpoint_raised` and the
CLI's exit code `2` both surface this distinctly from a rollback (exit `1`)
or a successful rotation (exit `0`).

```bash
python execution/scripts/rotate_swarm_secret.py github_webhook_secret --dry-run
python execution/scripts/rotate_swarm_secret.py approve_email_secret --dry-run
```

`--dry-run` is the only mode this repo's tests and examples exercise: it
prints the full plan — which files, vars, and daemon get touched, and what the
live probe will POST — with every value shown as `<redacted>`, and makes no
write, no HTTP call, no `launchctl` call. A real run additionally requires
`--i-know-this-calls-real-github` (webhook secret only, tripwire for the
GitHub write) and `ATELES_ALLOW_DAEMON_RESTART=1` in the *process's own*
environment (never accepted as a CLI value) before it restarts anything; the
agent AAuth signing keys named in the same ruling are **not** covered by this
script yet — the credential registry entry for
`AAUTH_SIGNING_KEY_APUS`/`AAUTH_SIGNING_KEY_FORMICA` records the current path
as the manual dual-admit procedure proven 2026-09-25, pending a follow-up
task to script it.

Verify it worked: the printed outcome line reads `rolled forward (new value
promoted, old retired)`, `rolled back (old value retained)`, or `CHECKPOINT —
dual-admit left ON, manual resolution required` — never silent, and the exit
code (`0` / `1` / `2` respectively) distinguishes the same three outcomes for
scripting. Independently, `secrets_keys.py`-style verification applies:
decrypt `secrets/neotoma.sops.env` and confirm the `_NEXT` slot is absent
after a rolled-forward or rolled-back outcome (present only mid-rotation, or
deliberately still present after a checkpoint), and that `apis`'s live
process picked up the change (`launchctl print gui/$(id -u)/com.ateles.apis |
grep -A2 EnvironmentVariables`, or simply that a subsequent live probe
against the daemon still returns 200/400 as expected). A checkpoint outcome
means GitHub's actual webhook secret is unconfirmed — resolve it by hand
(check the webhook config in GitHub's UI or via `gh api
repos/<owner>/<repo>/hooks/<id>`, no `--reveal` of a secret involved since
GitHub never returns the secret value itself) before running the script
again. Nothing this script does prints a secret value at any point.

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
