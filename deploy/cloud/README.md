# Ateles cloud hosting — deploy artifacts & runbook

Task #5 of plan `ent_aff87747b49e338790568af6`. Design rationale + the bucket
classification (which daemon moves vs stays device-side) live in
[`docs/cloud_hosting.md`](../../docs/cloud_hosting.md). This directory is the
**executable** side of that design.

## Accepted decisions (2026-06-23)

| Decision | Choice |
|---|---|
| Host | **Hetzner Cloud, EU** (Falkenstein/Nuremberg), `CAX11` (2 vCPU ARM, 4 GB, ~€4/mo) |
| Topology | Containers on one small VM (Option 2 image + Option 1 host); code-touching T4 work → GitHub Actions (Bucket C) |
| Network | **Tailscale** (private mesh; no public SSH/ports) |
| age key | **Per-host** key (host compromise is contained + revocable without rotating the primary key) |
| `claude` auth | `ANTHROPIC_API_KEY` materialized from the SOPS+age snapshot |
| Google (`gws`) | **B-thin first** — Monedula/Sylvia/Cotinga stay device-side until a Google service account (A-svc) is provisioned |

## Files

| File | Purpose |
|---|---|
| `Dockerfile` | Multi-arch Bucket-A daemon image (Python + `sops` + Claude CLI + repo). Secrets never baked in. |
| `entrypoint.sh` | Materializes secrets (SOPS+age) → `~/.config/neotoma/.env`, then `exec`s the daemon. |
| `docker-compose.yml` | One container per Bucket-A daemon, shared image. `APIS_DRY_RUN=1` by default. |
| `bootstrap-host.sh` | Idempotent VM bootstrap: Docker, Tailscale, repos, age key, build+up. |

## Runbook

### M1 — provision the host (operator)
1. Create the Hetzner `CAX11` (Ubuntu 24.04 arm64), EU region.
2. From your machine: `tailscale up` is done by the bootstrap; generate a
   Tailscale **auth key** in the admin console first.
3. SSH in (over Tailscale once joined) and run:
   ```bash
   ATELES_PRIVATE_REMOTE=<read-only clone URL for ateles-private> \
   TS_AUTHKEY=tskey-... \
   bash /opt/ateles/ateles/deploy/cloud/bootstrap-host.sh
   ```
   (clone this repo first, or `curl` the script.) It pauses with instructions at
   any step that needs a secret you haven't supplied.

### Provision the per-host age key (operator — the one genuinely new secret)
On the host:
```bash
mkdir -p /opt/ateles/age && age-keygen -o /opt/ateles/age/keys.txt && chmod 600 /opt/ateles/age/keys.txt
grep '^# public key:' /opt/ateles/age/keys.txt   # copy the age1... value
```
Then, in `ateles-private`: add that `age1...` as a recipient in `.sops.yaml`,
re-encrypt the snapshot (`execution/scripts/secrets_publish.py`), commit, and
`git -C /opt/ateles/ateles-private pull` on the host. Confirm
`ANTHROPIC_API_KEY` and `NEOTOMA_BEARER_TOKEN_PROD` are in the published
snapshot (the latter is auto-promoted for the remote `NEOTOMA_BASE_URL`).

### M0 — validate in dry-run (no cutover)
```bash
cd /opt/ateles/ateles/deploy/cloud && docker compose up -d
docker compose logs -f apis          # expect: secrets materialized, SSE subscribe,
                                     #         "[watchdog] starting", clean sweeps
```
With `APIS_DRY_RUN=1` nothing dispatches — you're confirming the container
materializes secrets, authenticates to **prod** Neotoma (no 401s), and connects
SSE.

### M2 — move Apus first (safest), then M3 the rest, one at a time
For each daemon: confirm it's healthy in the cloud, **then** stop its Studio copy
(`launchctl bootout gui/$(id -u)/com.ateles.<name>`), **then** set
`APIS_DRY_RUN=0` (for Apis) / remove dry-run and `docker compose up -d <svc>`.
**Single-consumer rule:** never run the same daemon on the Studio *and* the cloud
— they double-consume SSE and double-act. Rollback = stop the cloud service,
re-enable the Studio plist.

### M4 — code-touching work → Bucket C
Redirect Cicada/Vanellus dispatch to GitHub Actions ephemeral runners (Loxia is
already there). Tracked separately; not required to get Bucket A off the Studio.

### Redeploy a new revision
Rebuild the image (it carries the code): `docker compose build && docker compose up -d`.
Optionally add a `systemd` timer that `git pull`s + rebuilds on `origin/main`
(the container analog of `rc-autodeploy`).

## Notes
- Secrets: the host is just another **materialize target** for the existing
  SOPS+age snapshot — no new distribution mechanism. The age key + the
  `ateles-private` clone are mounted **read-only**; plaintext exists only in the
  running container's env.
- Device-bound daemons (Tyto, Strix, Cyphorhinus, computer-use, the `gws`
  bridge) stay as launchd agents on the operator's device(s) — Bucket B.
