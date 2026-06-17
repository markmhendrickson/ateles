# Runbook — Activate the role-faithful swarm (ateles#94 follow-through)

**Purpose:** with PR ateles#96 merged (Stage 1/2/5 of #94 — dispatch now loads each role's `agent_definition`, stamps `harness_event`s, and flags degraded fallback), this runbook turns on the parts that need host / GitHub / key-material actions. Do it in one sitting; each step says how to verify before moving on.

**Status (updated 2026-06-17, after the live Step 0 pass):**
- ✅ #96 merged to `origin/main` (commit `45c3afb`).
- ✅ **Daemon running merged code** — `com.ateles.apis` restarted (PID cycled), running from `~/ateles-rc-src` @ `45c3afb`, SSE connected, **webhook gateway already listening on :8742 (local)**.
- ✅ `ateles-agent` GitHub machine account already exists (see the "Github Ateles Agent" LOGIN item in 1Password for its username).
- ✅ `ATELES_AGENT_PAT` **and** `NEOTOMA_AGENT_PAT` already present in `ateles-private/.env`.
- ✅ Webhook secret already in 1Password: the **"Ateles Apis GitHub webhook secret"** item.
- ⬜ **Tunnel + GitHub webhook registration** not yet done (the gateway listens locally, but no public route / no webhook on the repos). ← biggest remaining external step.
- ⬜ PATs not yet verified classic/repo-scope; comment-poster not yet pointed at the bot token.
- ⬜ Daemon AAuth keypairs not yet minted (software ES256 — **no YubiKey needed**).

So Step 0 is DONE and the gateway is up locally. Remaining: verify the two PATs, mint keypairs, point the comment-poster at the machine identity, expose + register the webhook, then run a pilot.

---

## Step 0 — Update the running daemon checkout to main, then RESTART it
The live Apis daemon must run the merged code **and** be restarted to load it (a running daemon holds the old code in memory until cycled).

**First find the directory the daemon actually runs from — it is NOT necessarily `~/repos/ateles`.** On this host the launchd job runs from `~/ateles-rc-src` (a stable checkout pinned to origin/main, per the daemon-deployment-fragility note), while `~/repos/ateles` is the shared dev checkout that flips branches. Confirm the real path from the running process, not the plist `WorkingDirectory` (which may be stale):
```
PID=$(launchctl list | awk '/com.ateles.apis/{print $1}')
ps -o command= -p "$PID" | grep -oE '/[^ ]+/apis\.py'   # the dir before /execution/... is the live checkout
```
Update **that** checkout (here `~/ateles-rc-src`) to main. Do NOT `git checkout main` in a directory where another worktree already holds `main` — it fails with "'main' is already used by worktree …". If that happens you're in the wrong checkout, or a stale worktree is squatting on the branch: `git worktree list` to find it, `git worktree remove --force <path>` if abandoned.
```
DAEMON_SRC=~/ateles-rc-src          # the path you just confirmed
git -C "$DAEMON_SRC" fetch origin main
git -C "$DAEMON_SRC" checkout main && git -C "$DAEMON_SRC" pull origin main --ff-only
git -C "$DAEMON_SRC" log -1 --oneline       # expect 45c3afb (or later)
```
Then **restart the daemon** so it loads the new code (modern one-shot; replaces unload+load):
```
launchctl kickstart -k "gui/$(id -u)/com.ateles.apis"
```
Verify it came back on the merged code:
```
NEWPID=$(launchctl list | awk '/com.ateles.apis/{print $1}'); echo "pid=$NEWPID"
ps -o lstart= -p "$NEWPID"                                   # start time should be ~now
grep -c 'build_system_prompt' "$DAEMON_SRC/execution/daemons/apis/skill_runner.py"   # 5 = new code on disk
lsof -nP -iTCP:8742 -sTCP:LISTEN | grep "$NEWPID"            # webhook gateway listening (local)
```

---

## Step 1 — Verify the two machine-account PATs are valid + classic
Per the standing rule, machine accounts opening PRs on **public** repos need **classic repo-scope** PATs (fine-grained 403s).
```
# names only — do NOT echo values
grep -oE '^(ATELES|NEOTOMA)_AGENT_PAT' ~/repos/ateles-private/.env

# validate each (expect 200 + the bot login)
ATELES_AGENT_PAT=$(grep '^ATELES_AGENT_PAT=' ~/repos/ateles-private/.env | cut -d= -f2-)
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: token $ATELES_AGENT_PAT" https://api.github.com/user
curl -s -H "Authorization: token $ATELES_AGENT_PAT" https://api.github.com/user | python3 -c 'import sys,json;u=json.load(sys.stdin);print("login:",u.get("login"))'
```
Repeat for `NEOTOMA_AGENT_PAT`. If either 401s or is fine-grained, regenerate as **classic, scope=repo** under the respective bot account and update `ateles-private/.env`.
Confirm the bot is a collaborator on both `markmhendrickson/ateles` and `markmhendrickson/neotoma` (so it can comment + open PRs).

---

## Step 2 — Mint daemon AAuth keypairs (Stage 3 prerequisite — software, no YubiKey)
The mint script writes `ateles-private/keys/<role>.jwk.json` (private + public coords in one canonical JWK; `aauth_signer.py` loads it directly).
```
cd ~/repos/ateles
for role in apis lanius pavo vanellus gryllus monedula fringilla gorilla sturnus; do
  python execution/scripts/mint_daemon_keypair.py --name "$role"
done
ls -la ~/repos/ateles-private/keys/*.jwk.json
```
(Adjust the roster to the agents you actually dispatch.) These are gitignored in `ateles-private`; never commit them. After minting, `AAuthSigner.from_key_file(role)` stops returning a stub, so dispatched-agent writes carry `sub`/`thumbprint`. The small code change to inject the signer into the spawned subprocess is the remaining Stage-3 code task (assistant-doable once keys exist).

---

## Step 3 — Point the swarm comment-poster at the machine identity (#95)
`swarm_dispatch.py` already posts role-attributed comments (`attribution_header(agent, role)`) but reads `GITHUB_TOKEN` — which, if it's your token, posts as **you**. Set the daemon's environment so it posts as the bot:
```
# in the daemon's env (ateles-private/.env or the plist EnvironmentVariables):
GITHUB_TOKEN=<value of ATELES_AGENT_PAT>     # for ateles-repo comments
# neotoma-repo comments should use NEOTOMA_AGENT_PAT — if the poster doesn't yet
# select per-repo, that's the small #95 code task (assistant-doable).
```
Verify after activation that a comment shows the **bot** as author, not you.

---

## Step 4 — Activate the GitHub webhook → gateway
**The gateway already listens on :8742 locally** (confirmed in Step 0). What's left is the *external* plumbing: a public route to it, the webhook secret in the daemon env, and the webhook registered on the repos. Apus owns 8741. Public hostname: `apis.markmhendrickson.com`.

1. **Tunnel:** ensure the cloudflared ingress maps `apis.markmhendrickson.com → http://localhost:8742`, then restart cloudflared to pick it up.
   ```
   # confirm the CNAME exists in the correct CF zone, then:
   # (edit ~/.cloudflared/config.yml ingress if needed)
   sudo launchctl kickstart -k system/com.cloudflare.cloudflared   # or your restart mechanism
   curl -s -o /dev/null -w '%{http_code}\n' https://apis.markmhendrickson.com/health   # expect 200 once the daemon is up
   ```
2. **Daemon env:** confirm `APIS_GITHUB_WEBHOOK_SECRET` is exported for the daemon (value = the 1Password "Ateles Apis GitHub webhook secret" item).
   ```
   # look up the item id, then read its password field into ateles-private/.env as APIS_GITHUB_WEBHOOK_SECRET
   op item get "Ateles Apis GitHub webhook secret" --fields label=password
   ```
3. **Load the daemon:**
   ```
   launchctl unload ~/Library/LaunchAgents/com.ateles.apis.plist 2>/dev/null
   launchctl load   ~/Library/LaunchAgents/com.ateles.apis.plist
   # (plist source: execution/daemons/apis/com.ateles.apis.plist — symlink/copy into ~/Library/LaunchAgents if not already)
   ```
4. **Create the webhooks** on both repos (Settings → Webhooks → Add):
   - Payload URL: `https://apis.markmhendrickson.com/github/webhook`
   - Content type: `application/json`
   - Secret: the same `APIS_GITHUB_WEBHOOK_SECRET`
   - Events: **Issues** + **Pull requests**
   - Repos: `markmhendrickson/ateles` **and** `markmhendrickson/neotoma`
5. **Verify delivery:** GitHub's webhook page shows a green ✓ for the ping; daemon log shows the HMAC-verified receipt.

---

## Step 5 — Genuine swarm pilot (the run #1603 wasn't)
With the webhook live and definitions loading:
1. Open a small, low-risk test issue on `markmhendrickson/ateles` (e.g. a doc typo or a tiny additive helper).
2. Watch `swarm_dispatch` drive it: `issue.opened` → Lanius (gate init) → expectation agents → Pavo (Phase 1). On a follow-up PR: Lanius gate inheritance → review panel lenses → Vanellus aggregation.
3. **Confirm fidelity** (this is the validation #1603 lacked):
   - Thread comments appear **signed by role** and authored by the **bot**, not you.
   - `harness_event` entities exist in Neotoma for the run (grep none carry `degraded_generic_subagent`).
   - The dispatched agents ran with their `agent_definition.prompt_markdown` (check the daemon log for the role-load line, not a degraded warning).
4. If any agent logs `degraded_generic_subagent`, its `agent_definition` didn't load — check the role name matches the entity `name` in Neotoma.

Only after this end-to-end pass should the swarm be described as "validated."

---

## Quick verification matrix
| Check | Command / where | Pass |
|---|---|---|
| #96 on main | `git -C ~/repos/ateles log -1 --oneline` | shows `45c3afb`+ |
| PATs valid + classic | curl `api.github.com/user` per token | 200 + bot login |
| Keypairs minted | `ls ateles-private/keys/*.jwk.json` | one per role |
| Gateway reachable | `curl …apis.markmhendrickson.com/health` | 200 |
| Webhook delivering | GitHub repo → Webhooks → Recent Deliveries | green ✓ |
| Comments as bot | any swarm comment author | the bot, not you |
| Pilot fidelity | Neotoma `harness_event`s for the run | no `degraded_*` |

---

## What stays assistant-doable (ping me after the host steps)
- Stage 3 code: inject the now-minted `AAuthSigner` into the dispatched subprocess so writes are stamped.
- #95 code: per-repo token selection in the comment-poster (ateles-agent vs neotoma-agent) + extend role-attributed comments to the SSE task path (today they live in the GitHub-webhook path).
