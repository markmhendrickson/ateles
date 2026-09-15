# Daemon RC auto-deploy ("rolling main = RC")

The Ateles T3 daemons (**Apis**, **Formica**, **neotoma-agent**) must run from a
clean, stable checkout — never from the development checkout (`~/repos/ateles`),
whose branch and working tree churn during day-to-day work. Otherwise the
running swarm silently executes whatever code happens to be checked out for
dev, including half-finished feature branches.

This mirrors Neotoma's `~/neotoma-rc-src` + `com.neotoma.rc-autodeploy` pattern.

## Components

| Piece | Location | Role |
|---|---|---|
| Deploy checkout | `~/ateles-rc-src` | Clean clone pinned to `origin/main`; the only tree the daemons run from. |
| Daemon venv | `~/ateles-rc-src/.venv` | Provisioned from `execution/scripts/daemon-requirements.txt`. |
| Redeploy script | `execution/scripts/redeploy_daemons_from_main.sh` | Fast-forwards the deploy checkout to `origin/main`, refreshes the venv when deps change, hard-restarts the three daemons. |
| Autodeploy agent | `com.ateles.rc-autodeploy` | Runs the redeploy script every 120s. |
| Installer | `execution/scripts/install_rc_autodeploy.sh` | Provisions the checkout, venv, and autodeploy agent. |

## Flow

```
merge to main ──> (≤120s) com.ateles.rc-autodeploy fires
  ──> redeploy_daemons_from_main.sh
        ├─ git fetch origin main; abort if deploy HEAD diverged (non-ff)
        ├─ git merge --ff-only origin/main
        ├─ refresh .venv iff daemon-requirements.txt changed
        └─ launchctl kickstart -k each daemon  (fresh module import)
```

The redeploy script is idempotent (no-ops when already at `origin/main`),
single-flighted via an atomic lockdir, and fail-soft (daemons stay on the prior
build on any error).

## Per-daemon plist requirements

The per-daemon plists (`com.ateles.{apis,formica,neotoma-agent,cotinga,cyphorhinus,piculet,sylvia,phoenicurus-prepare}`) are
machine-local (gitignored) and must:

1. Point `ProgramArguments` at `~/ateles-rc-src/.venv/bin/python3` and the
   daemon script under `~/ateles-rc-src/execution/daemons/...`.
2. Set **`ATELES_PRIVATE_KEYS_DIR`** to the operator's real
   `~/repos/ateles-private/keys`. The deploy checkout has no sibling
   `ateles-private` overlay, and `aauth_signer.py` otherwise resolves the keys
   dir relative to the checkout — so without this override the daemons fall back
   to stub signers and attribute observations to the operator token.
3. Carry the daemon's `NEOTOMA_BASE_URL`, SSE subscription id, and (Apis) claude
   binary path, as before.

## Checkout identity enforcement

Deploy-bound daemons (`cotinga`, `cyphorhinus`, `piculet`, `sylvia`,
`phoenicurus-prepare`) refuse to start unless their git checkout root is the
documented deploy tree. This is **checkout identity** ("right tree?") — distinct
from **checkout drift** ("current?") in `lib/daemon_runtime/checkout_drift.py`.

Incidents: ateles#339, #361, #412, #515.

Startup enforcement is **fail-closed** for those five daemons. There is no
production bypass env. Tests may set `ATELES_CHECKOUT_IDENTITY_ROOT` (actual
root override) or `ATELES_DEPLOY_CHECKOUT` (expected root; default
`~/ateles-rc-src`).

### Exit codes

| Code | Condition | FATAL line 1 prefix |
|---|---|---|
| `78` | Wrong tree (`actual ≠ expected`) | `FATAL: wrong checkout — refusing to start.` |
| `79` | Cannot verify (no `.git`, unreadable HEAD, …) | `FATAL: cannot determine checkout identity` |
| `80` | Expected deploy root path does not exist | `FATAL: deploy checkout not provisioned` |
| `81` | Unexpected I/O/permission during check | `FATAL: checkout identity check failed` |

### FATAL message (verbatim shape)

```
FATAL: wrong checkout — refusing to start.

  daemon:       phoenicurus-prepare
  running from: /Users/markmhendrickson/repos/ateles  (branch: feature/example)
  expected:     /Users/markmhendrickson/ateles-rc-src  (must track origin/main)

  plist:        com.ateles.phoenicurus-prepare

This daemon drives releases. Running from a session clone risks silent
wrong-code execution (see ateles#339, #361, #412, #515).

Fix:
  bash ~/ateles-rc-src/execution/scripts/isolate_daemons_to_rc_src.sh --apply
Docs: docs/daemon_rc_autodeploy.md
```

### Cutover (co-located state)

`execution/scripts/isolate_daemons_to_rc_src.sh --apply` inventories daemon-local
state in both trees, snapshots hashed rollback copies under
`~/.config/ateles/daemon-state-backups/`, merges losslessly into the RC tree
(or refuses), rewrites/reloads **only** the five labels above, then proves a
**new PID** and executable/script paths under `~/ateles-rc-src`. On failure it
restores state (including files that were absent before reconciliation),
restores all five prior plist configurations, reloads the prior fleet, and does
not claim cutover complete. Both rollback sets are retained. The shared session
clone is left untouched until all five pass. XDG relocation is out of scope.

Credential boundary: the deploy checkout holds **code only**. Secrets stay in
`~/.config/neotoma/.env`, `ateles-private/keys`, and plist `EnvironmentVariables`
— never copied into `~/ateles-rc-src`.

## Setup

```bash
~/repos/ateles/execution/scripts/install_rc_autodeploy.sh
# then repoint the per-daemon plists per "Per-daemon plist requirements" above
# for the five wrong-tree daemons (or any still on ~/repos/ateles):
bash ~/ateles-rc-src/execution/scripts/isolate_daemons_to_rc_src.sh --apply
```
