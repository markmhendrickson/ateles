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

The per-daemon plists (`com.ateles.{apis,formica,neotoma-agent}`) are
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

## Setup

```bash
~/repos/ateles/execution/scripts/install_rc_autodeploy.sh
# then repoint the per-daemon plists per "Per-daemon plist requirements" above
```
