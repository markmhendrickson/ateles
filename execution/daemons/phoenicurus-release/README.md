# Phoenicurus-Release

Operator-approved release executor for Neotoma. Two halves:

- **`publish.py`** (this directory) — the **deterministic** publish core. Takes an
  already-prepared, operator-**approved** release and ships it: merge RC PR → tag →
  push → `npm publish` → GitHub Release → sandbox deploy → verify → publish draft →
  post-deploy probes → mark published → Telegram confirmation. No LLM. Invoked
  **on demand** after approval, not on a schedule.
- **`prepare.py`** (future) — the scheduled Mon–Thu prep run that authors the
  supplement, runs the security + `/review` lanes, opens the RC PR, renders the
  notes, stores the `release_result` as `status=pending_approval`, and Telegrams
  the operator. Halts; does not publish.

This split exists because release approval can take hours — a launchd daemon
cannot block in-process that long (unlike Monedula's 120 s payment approval).
Prepare runs and exits; publish fires later when the operator approves.

## State model (`release_result` entity)

The release moves through `status` values on a single `release_result` entity
(identity = `version`, so transitions coalesce):

```
prepared → pending_approval → approved → publishing → published
                                       ↘ failed (with reason)
```

`publish.py` refuses to act unless `status == approved` (override with `--force`).

## publish.py

```bash
# Publish a specific approved release (normal path, invoked by Onychomys on approval)
python3 publish.py --version v0.16.0

# Plan only — no irreversible actions (safe to run anytime)
python3 publish.py --version v0.16.0 --dry-run

# By entity id
python3 publish.py --entity-id ent_xxx

# Publish even if status != approved (manual override)
python3 publish.py --version v0.16.0 --force
```

### Safety properties

- **Approval gate**: won't publish unless the `release_result` is `approved`
  (or `--force`).
- **Clean-tree guard**: refuses to publish if the Neotoma working tree has
  uncommitted non-release files.
- **No-clobber**: aborts if the git tag already exists.
- **npm auth preflight**: runs `npm whoami` with the automation token before
  publishing; a missing/expired token fails **loud** (Telegram) rather than
  producing a tagged-but-unpublished release.
- **Registry verify**: confirms `npm view neotoma version` matches after publish.
- **Sandbox verify**: confirms `version` + `mode: sandbox` on the live host
  before publishing the GitHub Release draft.

## Configuration (env, from `~/.config/neotoma/.env`)

| Var | Purpose |
|-----|---------|
| `NPM_TOKEN` | npm granular automation token (Publish scope, `neotoma` only, bypass-2FA). Operator-managed; never echoed. |
| `NEOTOMA_BEARER_TOKEN` | Neotoma API auth (omitted automatically on loopback). |
| `NEOTOMA_BASE_URL` | Neotoma API base (default `http://localhost:3180`). |
| `NEOTOMA_REPO_ROOT` | Neotoma source checkout to release from (default `~/repos/neotoma`). |
| `NEOTOMA_SANDBOX_URL` | Sandbox host to verify (default `https://neotoma-sandbox.fly.dev`). |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram push (via shared `send.mjs`). |
| `TELEGRAM_TOPIC_PHOENICURUS` | Optional Telegram topic/thread id for release messages. |

## Approval routing (Onychomys)

`publish.py` is invoked by Onychomys when the operator replies `approve <version>`
on Telegram: Onychomys flips the `release_result` to `approved`, then runs
`python3 publish.py --version <version>`. See the Onychomys SOUL.md
"Release approval" section.

## Install

`publish.py` is invoked on demand, so it does **not** need a scheduled launchd
agent. `install.sh` only verifies prerequisites (node, npm, gh, flyctl, the env
vars) and prints the invocation Onychomys should use. Run it once to validate the
environment:

```bash
bash install.sh
```

## Logs

`~/Library/Logs/ateles/phoenicurus-release.log`
