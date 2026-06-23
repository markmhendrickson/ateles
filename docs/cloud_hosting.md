# Cloud Hosting for the Ateles Swarm

**Status: Design** (task #5 of plan `ent_aff87747b49e338790568af6` — "Task-spine loop + cloud-hosted swarm")

Decision under execution: `cloud_hosted_device_agnostic_swarm` — move the swarm's
loop bodies to always-on cloud/self-hosting so the swarm is device-agnostic. The
operator's devices become clients/channels. **Exception:** device-bound work
(computer-use, Tyto screenshots, local `gws`/Gmail/Calendar auth) runs via thin
per-device agents. Code-touching work runs in ephemeral cloud runners (GitHub
Actions / cloud agents) triggered by Apis.

This document is decision-forcing. The genuinely-blocked items requiring the
operator are listed in [§6 Open decisions](#6-open-decisions-operator-required) as
a checklist. Everything above that line is a recommendation the operator can
accept or amend.

---

## 1. Current state — device-coupled launchd on Mac Studio

Today every T3 daemon (`apis`, `anthus`, `apus`, `formica`, `monedula`,
`turdus`, `tyto`, `sylvia`, `cotinga`, `cyphorhinus`, `gorilla`,
`neotoma-agent`, `morning-brief`, `strix`) runs as a `launchd` LaunchAgent on the
operator's Mac Studio. Each daemon:

- is launched from a clean RC checkout `~/ateles-rc-src` (kept at `origin/main`
  by `com.ateles.rc-autodeploy` polling every 120s — see
  `execution/scripts/redeploy_daemons_from_main.sh`),
- bootstraps env from `~/.config/neotoma/.env` (see the env-loader block at the
  top of `execution/daemons/apis/apis.py`),
- loads its `agent_definition` + AAuth keypair from Neotoma at startup,
- subscribes to Neotoma SSE and, on each event, spawns a T4 agent by shelling
  out to a headless `claude --print` (Apis' `CLAUDE_BIN` /
  `_spawn_claude_skill`).

**Why this couples the swarm to one device — the concrete friction:**

1. **Availability is the Mac Studio's availability.** The swarm only runs while
   that machine is awake, online, and logged in. Sleep, reboot, OS update,
   network drop, or a move to another room takes the whole swarm offline. There
   is no second machine that picks up the SSE stream.
2. **Moving between devices does not move the swarm.** A laptop the operator
   travels with does not run the daemons. To make a second machine "the swarm
   host" the operator must replay the full host-side setup: clone
   `~/ateles-rc-src`, build the venv, lay down `~/.config/neotoma/.env`,
   provision every AAuth keypair under `ateles-private/keys/`, install ~14
   per-daemon plists, run `install_rc_autodeploy.sh`, then
   `isolate_daemons_to_rc_src.sh`. None of it is portable; it is a per-machine
   re-provisioning.
3. **Two hosts cannot both run, so there is no failover and no clean migration.**
   If both the Studio and a laptop ran the daemons, both would consume the same
   SSE events and double-dispatch tasks, double-pay, double-comment. So the
   operator runs exactly one host and accepts it as a single point of failure;
   cutting over to a new host is a manual "stop here, start there".
4. **Operator-presence ergonomics leak into infrastructure.** `launchd` user
   agents are tied to the GUI login session; the swarm's "always-on" is really
   "on while the operator is logged in on that Mac".
5. **Code-touching work already wants to be elsewhere.** Apis spawns
   `claude --print` subprocesses on the host for T4 agents like Cicada/Vanellus.
   That is the most resource-hungry and most security-sensitive work (it writes
   code, opens PRs) and it is pinned to the operator's personal workstation.

What is **not** coupled and should stay as-is: Neotoma is already
network-reachable at `https://neotoma.markmhendrickson.com` and is the canonical
store + event bus + identity plane. Apus already terminates a public HTTPS
endpoint via Cloudflare Tunnel (`apus.markmhendrickson.com`). GitHub Actions
already host Loxia (PR review) and CI security gates. The spine is cloud; only the
loop bodies are stuck on the Studio.

---

## 2. Target architecture — what moves, what stays device-side

The principle: **the loop body runs where it has no device dependency; only work
that physically needs a device stays on that device.** Each daemon is classified
into one of three buckets.

### Bucket A — Move to always-on cloud host (device-agnostic loop bodies)

These daemons only talk to Neotoma (SSE in, observations out), GitHub, Telegram,
and other network APIs. They have no dependency on the operator's screen,
keyboard, local OS keychain, or local browser. They are the core of the
cloud-hosted swarm.

| Daemon | Why it can move | Notes |
|---|---|---|
| **Apis** | Pure SSE→dispatch; also runs a GitHub webhook gateway (port 8742) | The dispatch of *code-touching* T4 work should be redirected to ephemeral cloud runners (Bucket C), not run inline on the host. |
| **Anthus** | SSE workflow dispatch + `participation_record` writes | No device dependency. |
| **Apus** | HTTPS webhook receiver + Neotoma→git mirror; already public via Cloudflare Tunnel | Strong candidate to move first — it is *already* network-shaped; cloud removes the "tunnel terminates on the Studio" coupling. |
| **Formica** | GitHub issue/PR automation via API | Dispatches code work → redirect to Bucket C. |
| **neotoma-agent** | neotoma-repo GitHub automation via API | Same as Formica. |
| **Monedula** | Payment daemon: Wise/BTC via API + Neotoma `payment_profile` + calendar keywords; operator approval over Telegram | Moves *if* its calendar read is via an API path, not local `gws` auth (see Bucket B caveat). Approval is already a Telegram round-trip, which is location-independent. |
| **Sylvia** | Recurring-task lifecycle + Neotoma↔Calendar sync | Moves only when Calendar access is service-credentialed, not local `gws` (Bucket B caveat). |
| **Cotinga** | Daily event-prep briefings; reads Calendar + Neotoma | Same Calendar caveat. |
| **Gorilla** | Weekly training summaries from `workout_session` data | No device dependency. |
| **Morning-brief** | 05:30 digest from `checkpoint_brief` entities → Telegram | No device dependency. |

### Bucket B — Must stay device-side (thin per-device agents)

These have a hard physical or session dependency. They run as a **thin per-device
agent**: a minimal launchd job on whichever device owns the capability, doing
only the device-bound capture/auth and writing the result to Neotoma. The
heavy reasoning is still done by cloud daemons reacting to those Neotoma writes.

| Component | Hard dependency | Thin-agent shape |
|---|---|---|
| **Tyto** (screenshot watcher) | Reads a local screenshots directory on the operator's machine; OS screen access | Watcher stays on-device; it captures + transcribes locally and writes the extracted entity to Neotoma. Any downstream reasoning is a cloud daemon. |
| **Strix** (mic/ambient recorder, menu-bar toggle) | Local microphone + GUI menu-bar app | Device-only by nature. Uploads audio / writes a transcription entity to Neotoma. |
| **Cyphorhinus** (audio-import watcher) | Watches local Voice Memos / Desktop import folders | Watcher stays on-device; transcription + entity extraction can run locally or hand the file to a cloud transcription path. |
| **computer-use / "Ateles on-device"** | Drives the operator's actual desktop (mouse/keyboard/screen) | Inherently device-bound. Stays a per-device agent invoked on demand. |
| **Local `gws` / Gmail / Calendar auth** | Today these use the operator's locally-authenticated `gws` CLI session | See decision below — this is the swing factor for Turdus/Sylvia/Cotinga/Monedula. |

**The `gws` swing factor.** Turdus (Gmail triage), Sylvia/Cotinga (Calendar), and
Monedula (calendar keyword detection) are *logically* Bucket A — they only touch
network APIs. They are pinned to the device today **only because** `gws` is
authenticated against the operator's local Google session. There are two ways to
unpin them:

- **(B-thin)** Keep a thin on-device "Google bridge": a small daemon holding the
  `gws` session that exposes Gmail/Calendar reads to the cloud (e.g. writes the
  relevant items to Neotoma as ingestion entities — the Phase 7 Cygnus pattern).
  Cloud daemons then react to Neotoma, never touching Google directly.
- **(A-svc)** Re-credential Google access for the cloud host with a Google OAuth
  service account / refresh token stored in `ateles-private` and materialized to
  the cloud host (see §4). Then Turdus/Sylvia/Cotinga/Monedula move fully to
  Bucket A.

Recommendation: **start with B-thin** (lowest blast radius, no new Google
credential to provision and secure) and graduate to A-svc once the cloud host is
proven, since A-svc is the cleaner end state.

### Bucket C — Ephemeral cloud runners (code-touching work)

T4 agents that **write code** — Cicada (issue worker), Vanellus (PR steward),
Loxia (already GHA) — should not run as long-lived subprocesses on any always-on
host. They are bursty, resource-hungry, and the highest-blast (they open PRs and
merge). Run them as **ephemeral runners**: Apis/Formica/neotoma-agent (now in the
cloud, Bucket A) trigger a GitHub Actions workflow (or equivalent ephemeral cloud
agent) per unit of code work; the runner spins up, does the job in a fresh
checkout with a scoped PAT, records observations to Neotoma, and tears down.

This is the existing direction of travel: Loxia and the CI security gates already
run on GHA, the `github_harness` MCP server already exists, and the architecture
doc already names worktrees as "temporary scaffolding until the harness servers
are live." Bucket C makes that the default for all code-touching dispatch instead
of the inline `claude --print` subprocess.

### Resulting topology

```
                       Neotoma (canonical store + SSE bus + AAuth)
                       https://neotoma.markmhendrickson.com
                                   ▲   │
                       observations │   │ SSE events
                                    │   ▼
   ┌──────────────────────── Always-on cloud host (Bucket A) ───────────────────────┐
   │  Apis · Anthus · Apus · Formica · neotoma-agent · Monedula · Sylvia ·          │
   │  Cotinga · Gorilla · Morning-brief    (systemd, headless `claude --print`)      │
   └───────┬───────────────────────────────────────────────────────────┬────────────┘
           │ triggers                                                    │ reads/writes
           ▼                                                             ▼
   Ephemeral cloud runners (Bucket C)                          Thin per-device agents (Bucket B)
   GitHub Actions: Cicada · Vanellus · Loxia                   Tyto · Strix · Cyphorhinus ·
   (code-touching, fresh checkout, scoped PAT, teardown)       computer-use · gws bridge
                                                               (on the operator's device(s))

   Operator devices = clients/channels (Telegram, etc.), NOT swarm hosts.
```

---

## 3. Hosting options compared

The question is *where Bucket A runs*. Bucket C is GitHub Actions regardless;
Bucket B stays on the operator's devices regardless.

### Option 1 — Small cloud VM (e.g. a 2 vCPU / 4 GB VPS), daemons as systemd units

- **What it is.** One always-on Linux VM. Each Bucket-A daemon runs as a
  `systemd` user/service unit (the Linux analog of today's launchd plists). Reuse
  the RC pattern: clone `~/ateles-rc-src`, a `systemd` timer or path-poll runs
  the existing `redeploy_daemons_from_main.sh` logic, units `Restart=always`.
- **Pros.** Closest 1:1 port of today's model — the RC autodeploy script, the
  per-daemon launch pattern, and the env-from-file bootstrap all translate
  directly (launchd plist → systemd unit; `kickstart -k` → `systemctl restart`).
  Lowest conceptual delta. Full control. Cheap.
- **Cons.** A pet server: OS patching, the box is now a long-lived secret-bearing
  host to harden, and `claude --print` must be installed + authenticated there.
  Single VM = single point of failure (better than a personal laptop, but not HA).
- **Cost.** Low (single small VPS).

### Option 2 — Container host (Docker + systemd), one image, per-daemon containers

- **What it is.** Containerize the daemon set (the pending/superseded
  "containerize minimal agent set (systemd + headless claude)" task). One image
  carrying the repo + venv + `claude` CLI; each daemon is a container; `systemd`
  (or `docker compose` + a restart policy) supervises them. Deploy on the same
  VM as Option 1 or any container host.
- **Pros.** Reproducible, portable artifact — the same image runs on the VM, a
  different provider, or the operator's spare machine, which directly serves the
  device-agnostic goal. Clean dependency isolation (no host Python/venv drift).
  Redeploy = pull new image tag instead of fast-forwarding a checkout. Natural
  fit for a future managed container runtime.
- **Cons.** New build/publish pipeline (image registry, tagging) replacing the
  git-fast-forward autodeploy. `claude --print` auth + secret injection must be
  designed into the image lifecycle. Slightly more moving parts than a bare VM.
- **Cost.** Low–moderate (VM + registry).

### Option 3 — GitHub-hosted ephemeral runners for everything

- **What it is.** No always-on host; every daemon's work becomes a scheduled or
  webhook-triggered GHA job.
- **Pros.** Zero servers to own; ephemeral = nothing long-lived to harden;
  already in use for Loxia/CI.
- **Cons.** **Wrong shape for the always-on SSE loop.** The core daemons
  (Apis/Anthus/Apus) hold a *persistent* SSE subscription and react in seconds;
  GHA is batch/cron/webhook, not a long-lived subscriber. Forcing the SSE loop
  into polling cron loses latency and event fidelity, and Apus is literally an
  inbound HTTPS receiver. This is **correct for Bucket C (code-touching) and
  wrong for Bucket A (the always-on loop).**
- **Cost.** Pay-per-minute; fine for bursty Bucket C.

### Recommendation

**Hybrid: Option 2 for Bucket A + Option 3 for Bucket C.**

- Run the always-on loop bodies (Bucket A) as **containers on a single small
  cloud VM** (Option 2's image, Option 1's host to start). Containerizing is the
  already-identified next step and gives a portable artifact that serves the
  device-agnostic goal far better than a hand-provisioned VM. Start single-VM
  (cheap, simple); the image makes a later move to a managed container runtime or
  a second region a config change, not a re-provisioning.
- Run **code-touching T4 work (Bucket C) as GitHub-hosted ephemeral runners**,
  triggered by the cloud Apis/Formica — extending the existing Loxia/CI-gates
  pattern. This keeps the heavy, high-blast work off the always-on host.
- Keep **Bucket B** as thin launchd agents on the operator's device(s).

If the operator wants the absolute minimum delta to *get off the Studio this
week*, fall back to **Option 1** (bare VM + systemd port of the RC scripts) and
containerize as a fast follow — the migration plan in §5 is written so Option 1
is a strict subset of Option 2.

---

## 4. Secrets reaching the cloud host

Ground truth (decision `credentials_management`): 1Password Family is canonical;
values ride an **age-encrypted SOPS snapshot** in the **private** `ateles-private`
repo (`ateles-private/secrets/*.sops.enc`), and are **materialized offline** by
`execution/scripts/secrets_materialize.py` with `ATELES_SECRETS_DIR` defaulting to
`~/repos/ateles-private`. The public `ateles` repo must never carry the encrypted
snapshot or the age key.

Provisioning the cloud host follows the same offline-materialize model — the host
becomes just another consumer of the snapshot:

1. **Provision the age private key on the host.** The host needs the age private
   key to decrypt the snapshot. Place it at `~/.config/sops/age/keys.txt` (the
   same path used elsewhere) with `600` perms, owned by the daemon user.
   *Where the key comes from is an open operator decision — see §6.*
2. **Get the encrypted snapshot onto the host.** Clone/pull `ateles-private`
   (private repo, deploy key or fine-grained read-only PAT) to the host so
   `ATELES_SECRETS_DIR` resolves; the snapshot itself stays encrypted at rest on
   the host.
3. **Materialize on boot / on deploy.** Run `secrets_materialize.py` to decrypt
   into the runtime env (`~/.config/neotoma/.env`, the file the daemons already
   read at startup). In the container model, materialize at container start so the
   plaintext lives only in the container's process env, never in the image.
4. **CI / Bucket C** continue to use the existing `SOPS_AGE_KEY` GitHub Actions
   secret to materialize per-run — no change.
5. **Rotation.** `NEOTOMA_BEARER_TOKEN` rotation is already pending as a
   precaution; rotating it (and re-publishing the snapshot via
   `secrets_publish.py`) is the clean way to invalidate anything that may have
   been over-exposed before the host existed.

Net: **no new secret-distribution mechanism.** The cloud host is a new
materialize target for the existing SOPS+age snapshot. The only genuinely new
secret-handling question is *how the age private key gets onto the host* (§6).

---

## 5. Migration plan (concrete steps)

Ordered for minimum blast radius: prove containerization, move **one** safe
daemon, validate, then cut the rest over and finally introduce the thin
device-side agents. Reuse the existing RC scripts at every step.

**Phase M0 — Containerize the minimal Bucket-A set (no cutover).**
- Write a `Dockerfile` carrying the repo at `origin/main`, the daemon venv
  (`execution/scripts/daemon-requirements.txt`), and the `claude` CLI. Entry
  point materializes secrets (§4) then `exec`s the daemon.
- Compose/systemd unit set for the Bucket-A daemons. This closes the pending
  "containerize minimal agent set (systemd + headless claude)" task.
- Validate locally: container starts, materializes env, loads its
  `agent_definition` + AAuth keypair from Neotoma, connects SSE — **in dry-run**
  (`APIS_DRY_RUN=1` and equivalents) so nothing dispatches.

**Phase M1 — Stand up the cloud host.**
- Provision the VM, the daemon user, the age key (§6), and a read-only
  `ateles-private` clone. Install the container runtime.
- Port the RC autodeploy: a `systemd` timer running the
  `redeploy_daemons_from_main.sh` logic (or, in the container model, an image-tag
  pull) so `origin/main` continues to be the rolling RC — reuse
  `install_rc_autodeploy.sh`'s shape, swapping launchctl for systemctl/docker.

**Phase M2 — Move ONE daemon first: Apus.**
- Apus is the safest first mover: it is already network-shaped (public HTTPS
  receiver) and its failure mode is "mirror lag," not "double-pay" or
  "double-PR." Deploy Apus to the cloud host, repoint the
  `apus.markmhendrickson.com` Cloudflare Tunnel / Neotoma webhook subscription at
  the cloud host, and **stop the Studio's Apus** (`launchctl bootout`) so exactly
  one Apus consumes webhooks.
- Validate: a Neotoma change triggers a mirror commit from the cloud Apus.

**Phase M3 — Cut over the rest of Bucket A, one daemon at a time.**
- For each daemon: deploy to cloud (still dry-run if it has side effects), confirm
  SSE reconnect in logs, then **atomically** stop it on the Studio and disable
  dry-run on the cloud. The single-consumer rule is the invariant — never let the
  Studio and the cloud run the same daemon live, or they double-dispatch
  (mirrors the SSE double-consume hazard called out in §1).
- Recommended order: Apis + Anthus (dispatch core) → Formica + neotoma-agent
  (GitHub) → Gorilla + Morning-brief (low-risk) → Monedula + Sylvia + Cotinga
  (only once the `gws`/Google access decision in §6 is made; until then they stay
  on-device as B-thin).
- The Studio's `isolate_daemons_to_rc_src.sh` and per-daemon plists are the
  rollback surface: to revert a daemon, re-enable its Studio plist and stop the
  cloud copy.

**Phase M4 — Redirect code-touching dispatch to ephemeral runners (Bucket C).**
- Change Apis/Formica/neotoma-agent so dispatching Cicada/Vanellus triggers a GHA
  workflow (scoped PAT, fresh checkout, observation recording, teardown) instead
  of an inline `claude --print` subprocess on the host. Loxia is already there;
  this generalizes the pattern.
- This also lets the Bucket-A host be sized small — it no longer runs heavy code
  agents.

**Phase M5 — Formalize the thin device-side agents (Bucket B).**
- Keep Tyto, Strix, Cyphorhinus, computer-use, and (until A-svc) the `gws` bridge
  as minimal launchd agents on the operator's device(s), each doing only its
  device-bound capture/auth and writing to Neotoma. Document which device owns
  which capability so a second device can take over a capability without taking
  over the whole swarm.

**Phase M6 — Decommission the Studio as the swarm host.**
- Once all Bucket-A daemons run in the cloud and Bucket C is on GHA, the Studio
  retains only its Bucket-B thin agents. The operator's devices are now
  clients/channels, not the swarm. Goal state reached.

---

## 6. Open decisions (operator-required)

These are the genuinely-blocked items. Each needs an operator answer before the
corresponding migration phase can proceed.

- [ ] **Hosting provider & region.** Which cloud VM / container host (provider,
      region, instance size)? Region affects latency to Neotoma and any data-
      residency preference (operator is EU/Barcelona — RGPD legitimate-interest
      basis already applies to people-data; an EU region is the conservative
      choice). *Blocks M1.*
- [ ] **Budget ceiling.** Monthly spend cap for the always-on VM + GHA
      ephemeral-runner minutes (Bucket C). Sets the instance size and whether
      Bucket C uses GitHub-hosted vs. self-hosted runners. *Blocks M1, M4.*
- [ ] **Network / reachability — Tailscale or public?** Does the cloud host join
      the operator's Tailscale tailnet (so Bucket-B device agents and the host
      talk over a private mesh, and admin access is keyless), or is it reached
      over public internet with SSH-key + firewall only? Neotoma is already public
      HTTPS, so the host does not *need* the tailnet to reach the spine — this is
      about admin access and device↔host links. **Recommendation: Tailscale.**
      *Blocks M1, M5.*
- [ ] **Where the age private key lives on the host.** The host must hold the age
      private key to materialize secrets (§4). Options, lowest-trust first:
      (a) operator pastes it into `~/.config/sops/age/keys.txt` once, by hand,
      over SSH/Tailscale (no key ever in cloud metadata);
      (b) cloud provider secret manager / instance metadata injects it at boot;
      (c) a dedicated age key *per host* (so a host compromise is contained and
      revocable without rotating the operator's primary key — **recommended**,
      pairs with re-encrypting the snapshot to multiple age recipients).
      *Blocks M1.*
- [ ] **`gws` / Google access model for the cloud host** — B-thin (on-device
      Google bridge writing to Neotoma) vs. A-svc (Google service-account /
      refresh-token materialized to the cloud host). Determines whether Turdus,
      Sylvia, Cotinga, and Monedula's calendar read move fully to the cloud or
      stay on-device. **Recommendation: B-thin first, A-svc later.** *Blocks the
      Monedula/Sylvia/Cotinga part of M3.*
- [ ] **`claude --print` auth on the cloud host.** The daemons spawn headless
      `claude` for any Bucket-A T4 work that is *not* redirected to Bucket C.
      Which credential authenticates `claude` on the host (and is it materialized
      via the same SOPS+age path)? *Blocks M0/M1 for any non-Bucket-C dispatch.*
- [ ] **Single-consumer enforcement during cutover.** Confirm the operational
      rule (and ideally a guard) that a given daemon runs in exactly one place at
      a time, so Studio and cloud never double-consume SSE / double-act. *Governs
      M2–M3 safety.*

---

*Constraints honored: PII-free; operator-specific config (provider, region, key
locations, recipients) is left as operator decisions and is env/`ateles-private`-
sourced, never to be baked into daemon code. This is a render-target-free design
note (not a plan-mirrored doc), safe to edit directly.*
