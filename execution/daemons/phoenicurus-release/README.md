# Phoenicurus-Release

Operator-approved release executor for Neotoma. Two halves:

- **`publish.py`** (this directory) — the **deterministic** publish core. Takes an
  already-prepared, operator-**approved** release and ships it: merge RC PR → tag →
  push → `npm publish` → GitHub Release → sandbox deploy → verify → publish draft →
  post-deploy probes → mark published → Telegram confirmation. No LLM. Invoked
  **on demand** after approval, not on a schedule.
- **`prepare.py`** — the prep run, triggered on every merge to Neotoma's main
  (with the Mon–Thu schedule kept as a safety net). Two-phase (like Cotinga):
  Phase 1 is a fast preflight gate (unreleased commits since the last tag ≥
  `PHOENICURUS_MIN_COMMITS`? main CI green? no release already in flight?); if it
  passes, Phase 2 spawns a headless `claude --print` agent that runs the
  `/release` PREPARE phase up to the RC PR, stores the `release_result` as
  `status=pending_approval`, and Telegrams the operator the full notes + RC PR
  link + advisory flags. `prepare.py` exits immediately; the agent sends its own
  Telegram. It NEVER tags, publishes, or deploys.

This split exists because release approval can take hours — a launchd daemon
cannot block in-process that long (unlike Monedula's 120 s payment approval).
Prepare runs and exits; publish fires later when the operator approves.

## Triggering (auto-release)

A merge to Neotoma's `main` prepares a release candidate immediately, instead of
waiting for the next scheduled sweep:

```
merge to main → GitHub `push` webhook → Apis gateway (github_gateway)
  → swarm_dispatch._handle_push_main → prepare.py --on-merge
```

`--on-merge` changes **only** the rate limit — from once per calendar day to once
per `origin/main` commit (state file `.phoenicurus_prepare_last_sha`). Every other
gate is unchanged: `PHOENICURUS_MIN_COMMITS`, main CI green, and the in-flight
`release_result` check all still apply, so a burst of merges cannot stack up
release candidates. The two locks are independent — a merge-triggered run never
consumes the daily lock, so the scheduled Mon–Thu run still fires as a safety net
if the webhook path is down.

**The approval gate is unchanged.** This only removes the schedule lag before the
operator is asked; publishing still requires `approve <version>`.

Tag pushes are deliberately ignored by the gateway — publishing a release pushes
a tag, which would otherwise re-trigger prepare in a loop.

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
# Publish a specific approved release (normal path, invoked by Ateles on approval)
python3 publish.py --version v0.16.0

# Plan only — no irreversible actions (safe to run anytime)
python3 publish.py --version v0.16.0 --dry-run

# By entity id
python3 publish.py --entity-id ent_xxx

# Publish even if status != approved (manual override)
python3 publish.py --version v0.16.0 --force

# Resume from a specific step after a manual fix (e.g. after resolving a
# merge_rc_pr/insufficient_permissions failure — see Troubleshooting below)
python3 publish.py --version v0.16.0 --resume-from=tag_and_push
```

`--resume-from` skips every step before the named one (`preflight`,
`merge_rc_pr`, `tag_and_push`, `npm_publish`, `github_release`,
`deploy_sandbox`, `publish_github_release_draft`, `post_release`) — useful
when an earlier step already completed (e.g. the RC PR was merged manually)
and re-running it would be redundant or unsafe.

### Safety properties

- **Approval gate**: won't publish unless the `release_result` is `approved`
  (or `--force`).
- **Clean-tree guard**: refuses to publish if the Neotoma working tree has
  uncommitted non-release files.
- **No-clobber**: aborts if the git tag already exists.
- **`preflight/version-match`**: after the RC PR is merged and before tagging,
  confirms the checked-out `package.json` version matches the target release
  version — hard-refuses (no continue-anyway path) if the version-bump commit
  is missing from the merge. See Troubleshooting for the exact failure/fix.
- **Detached-HEAD checkout**: `merge_rc_pr` checks out `origin/main` via
  `git fetch` + `git checkout --detach FETCH_HEAD` instead of `git checkout
  main`, so it is safe to run even when the operator's primary checkout
  already has `main` checked out in another worktree.
- **No redundant push**: since the RC PR is always merged server-side via
  `gh pr merge`, `tag_and_push` no longer runs a follow-up `git push origin
  main` — only the release tag is pushed.
- **npm publish runs in CI** (neotoma#2015): the tag push fires
  `.github/workflows/npm-publish.yml`, which builds on a pinned Node 20 and
  publishes with **provenance** (a signed attestation binding the tarball to the
  commit — not obtainable from a local publish). `publish.py` then polls the
  registry until the version appears.
  - **Bounded, loud wait**: moving the publish off-box turns a synchronous
    failure into an asynchronous one, so the poll has a hard timeout
    (`PHOENICURUS_NPM_PUBLISH_WAIT_S`, default 900s) that **fails the release**
    with a Telegram alert rather than falling through to `github_release` with
    nothing on npm.
  - **Local fallback**: set `PHOENICURUS_NPM_PUBLISH_MODE=local` to publish from
    this host instead (keeps the old `npm whoami` preflight + registry verify).
- **Registry verify**: confirms `npm view neotoma version` matches after publish.
- **Sandbox verify**: confirms `version` + `mode: sandbox` on the live host
  before publishing the GitHub Release draft.

## Configuration (env, from `~/.config/neotoma/.env`)

| Var | Purpose |
|-----|---------|
| `NPM_TOKEN` | npm granular automation token. Only needed for `PHOENICURUS_NPM_PUBLISH_MODE=local`; the default CI path uses the `NPM_TOKEN` **repo secret** instead. |
| `PHOENICURUS_NPM_PUBLISH_MODE` | `ci` (default — await the Actions publish) or `local` (publish from this host). |
| `PHOENICURUS_NPM_PUBLISH_WAIT_S` | How long to wait for CI to land the version (default 900). Timeout fails the release loudly. |
| `NEOTOMA_BEARER_TOKEN` | Neotoma API auth (omitted automatically on loopback). |
| `NEOTOMA_BASE_URL` | Neotoma API base (default `http://localhost:3180`). |
| `NEOTOMA_REPO_ROOT` | Neotoma source checkout to release from (default `~/repos/neotoma`). |
| `NEOTOMA_SANDBOX_URL` | Sandbox host to verify (default `https://neotoma-sandbox.fly.dev`). |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram push (via shared `send.mjs`). |
| `TELEGRAM_TOPIC_PHOENICURUS` | Optional Telegram topic/thread id for release messages. |
| `OPERATOR_EMAIL` | Where release RC notifications are emailed (in addition to Telegram). Unset → email is skipped, Telegram only. |
| `ATELES_SWARM_EMAIL` | Optional `From:` for release email (the shared swarm address, matching the other daemons). |

## Notification channels

A prepared release candidate is sent to the operator on **both Telegram and
email**. The spawned prepare agent (step 11–12 of its prompt) posts the full
rendered notes + RC PR link + `approve/skip` instruction to Telegram, and emails
the same via `gws gmail +send` (subject `🚀 Release <TAG> ready to approve`) using
the same `OPERATOR_EMAIL`/`ATELES_SWARM_EMAIL` the shared `lib/notify` Notifier
uses — so release mail matches every other swarm daemon. Email is **fail-open**:
if `OPERATOR_EMAIL` is unset or the send fails, the run logs it and continues;
Telegram is the guaranteed channel and the release is never blocked on email.

## Approval routing — two channels

A prepared release can be approved on **either channel**:

**Telegram** — the operator replies `approve <version>`; Ateles flips the
`release_result` to `approved` and runs `python3 publish.py --version <version>`.
See the Ateles SOUL.md "Release approval" section.

**Email reply** — the operator replies `approve <version>` to the RC email. The
routing mirrors the swarm PR-approve flow:

```
reply "approve <version>" to the RC email
  → Turdus (Gmail poll) verifies: sender == operator, subject has "ready to
    approve", body has `release-approve: <version>` token AND unquoted
    `approve <that exact version>`
  → POST /approve-release (Apis, loopback + X-Approve-Secret)
  → swarm_dispatch._handle_release_approve
  → publish.py --version <version> --from-email-approval
     (flips pending_approval → approved, refusing any other state, then ships)
```

Safety properties (match the PR-approve flow + one extra):
- **Operator-sender check** — only replies from `OPERATOR_EMAIL` count.
- **Loopback + shared secret** — `/approve-release` is 127.0.0.1-only and
  requires `APIS_APPROVE_EMAIL_SECRET`; unset → 503, wrong → 401. An email alone
  can't drive a publish without the secret on both daemons.
- **Unquoted-text guard** — the word `approve` must be the operator's own text,
  not the quoted `Reply approve …` from the original email.
- **Exact-version match** (operator ruling 2026-07-27) — the reply must name
  *this* release's version, so a stale `approve` from an old release thread can
  never trigger the wrong publish.
- **State gate** — `--from-email-approval` publishes only from
  `pending_approval`; a duplicate or stale reply on an already-published version
  is refused.

Config: set `APIS_APPROVE_EMAIL_SECRET` (shared, Turdus + Apis) and
`APIS_APPROVE_RELEASE_URL` (default `http://127.0.0.1:8742/approve-release`).

## Troubleshooting

### Worked example: missing version-bump commit (the v0.18.8 incident)

During the v0.18.8 release, the RC PR merged without the `chore(release): bump
version to v0.18.8 + supplement` commit. Without the `preflight/version-match`
guard, `publish.py` would have tagged and published the *previous* version's
code as `v0.18.8`. This was caught manually and required reverting main to
v0.18.7 to re-run the pipeline cleanly (neotoma#1920).

With the guard in place, the operator now sees this instead:

```
Preflight FAILED: package.json version (0.18.7) does not match target release version (0.18.8).
This means the version-bump commit is missing from the merged RC PR — publishing now would tag/publish the WRONG version (this is the exact defect that caused the v0.18.8 incident).
Fix: verify the RC PR included a `chore(release): bump version to v0.18.8` commit. If missing, run `npm version 0.18.8 --no-git-tag-version`, commit as `chore(release): bump version to v0.18.8 + supplement`, push, and re-merge before re-running publish.py.
```

**Fix steps:**
1. Confirm the RC PR is missing the bump commit (`git log release/v0.18.8`).
2. On the RC branch: `npm version 0.18.8 --no-git-tag-version`.
3. Commit as `chore(release): bump version to v0.18.8 + supplement` (matching
   precedent commit `01344fec9`).
4. Push, re-merge the RC PR, and re-run `python3 publish.py --version v0.18.8`.

### `merge_rc_pr/insufficient_permissions`

If the `gh` account running `publish.py` lacks merge rights on the RC PR, the
pipeline fails loud with a distinct, named failure class instead of a generic
error:

```
merge_rc_pr FAILED: the gh account running this command does not have permission to merge pull request 9 on owner/repo (GitHub returned: "does not have the correct permissions to execute MergePullRequest").
This is an operator action, not a retryable pipeline error.
Fix: have an operator with merge rights run `gh pr merge 9 --merge` (or merge via the GitHub UI), then re-run: python publish.py --resume-from=tag_and_push --version=v0.18.8
```

This does not retry automatically and does not fall through to `tag_and_push`
against an unmerged PR.

**Known limitation**: `ateles-agent` cannot merge PRs today, so
`merge_rc_pr/insufficient_permissions` is expected/normal until
[ateles#202](https://github.com/markmhendrickson/ateles/issues/202) lands
standing merge-rights/approval-routing changes — it is not a regression.

## Install

`publish.py` is invoked on demand, so it does **not** need a scheduled launchd
agent. `install.sh` only verifies prerequisites (node, npm, gh, flyctl, the env
vars) and prints the invocation Ateles should use. Run it once to validate the
environment:

```bash
bash install.sh
```

## Logs

`~/Library/Logs/ateles/phoenicurus-release.log`
