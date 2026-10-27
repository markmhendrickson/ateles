# App CI template

A copyable starter for the CI and deploy workflows of any application repo. It exists because an
application's delivery must never depend on swarm health or on a slow, fragile pipeline
([foundation decision 148](../../docs/foundation/adapters.md), operator ruling 2026-10-27). In one audit of a
client app, per-PR CI grew from 2 minutes to 70-100 minutes (a single serial job of about 170 steps, no
cancellation of superseded runs, late avoidable failures, heavy suites on every push), and production was
deployed from an unmerged integration branch, so reviewers reviewed heads that were not live.

The template is stack-agnostic. Python and Node slots are shown as commented examples.

| File | Copy to | Purpose |
|---|---|---|
| `ci.yml` | `.github/workflows/ci.yml` | Per-PR and push-to-main checks: `fast`, sharded `test`, one summary job |
| `nightly.yml` | `.github/workflows/nightly.yml` | Heavy suites on a schedule, on demand, and on release candidates |
| `deploy-guard.yml` | `.github/workflows/deploy.yml` (or merge into yours) | Refuses off-trunk deploys, optionally waits for green CI, stamps the commit, records a GitHub Deployment |
| `trunk-audit.yml` | `.github/workflows/trunk-audit.yml` | Daily independent audit: the latest successful production deployment must be on trunk |
| [`.github/actions/deployed-on-trunk/action.yml`](../../.github/actions/deployed-on-trunk/action.yml) | `.github/actions/deployed-on-trunk/action.yml` | The guard, vendored into the app repo (the default; see below) |
| [`execution/scripts/check_deployed_on_trunk.py`](../../execution/scripts/check_deployed_on_trunk.py) | `scripts/check_deployed_on_trunk.py` | The audit script that `trunk-audit.yml` runs (standard library only) |

The template workflows ship with **placeholder slots that fail until you fill them**: a copied, unfilled template
cannot go green or record a deploy. Each failing step says what to replace.

## What the guard does and does not stop

The deploy guard stops **accidents and drift**: someone deploying a branch by hand, a stale workflow, an unmerged
integration branch reaching production. It does **not** stop a deliberate bypass by someone with write access, who
can edit the workflow, change the action, or deploy by another route. The independent backstop is the scheduled
audit (`trunk-audit.yml`): it runs from the default branch, reads the GitHub Deployment history rather than
trusting the deploy job, and fails the run when the latest production deployment is off trunk or cannot be
checked. Treat a failing audit as an incident, not as flakiness.

**The audit is a weaker, later check, not proof of review.** Ancestry only grows, so the audit reads ancestry **as of
now** and cannot tell *deployed-then-merged* from *merged-then-deployed*: a commit deployed from an unmerged branch
and merged afterwards reads as on trunk. A pass therefore does not show that what was deployed had been reviewed on
trunk at the time. The **deploy-time guard is the check that binds**; the audit only catches what the guard missed
while it is still off trunk.

## The 15-minute budget

Per-PR CI targets **under 15 minutes end to end**. Every job carries a `timeout-minutes` (5 for `fast`, 12 for each
`test` shard) so a hang fails loudly instead of burning an hour. These timeouts are **hang guards, not the
15-minute budget**: the budget is the whole run's wall time (`fast` plus the slowest shard), which nothing
enforces automatically. Measure it, and move work to nightly when it is exceeded; do not tune a timeout to it.

**The rule: a suite that would push PR CI past the budget moves to nightly / release-candidate.** Do not raise a
timeout to make a slow suite fit. Split it into more shards if it parallelizes, otherwise move it to `nightly.yml`.
Typical nightly candidates are performance audits, native or integration suites, and evals. Nightly also runs on
pushes of `rc-*` tags and `release/*` branches, so a release candidate gets the full set before promotion.

How the template stays fast:

- `fast` (lint, format, typecheck, hygiene) runs first; `test` jobs `needs: fast`, so cheap failures surface early.
- `test` is a matrix of parallel shards with `fail-fast: true`: one red shard cancels the rest.
- Dependencies are cached (`cache: pip` / `cache: npm` on the setup actions).
- A new push to a PR cancels the superseded run. `cancel-in-progress` is `${{ github.event_name == 'pull_request' }}`,
  and the concurrency group is `<PR number> || <sha>`, so **a push to main gets its own group (its sha)**: it is
  never cancelled and never replaced as a pending run by a later push. Every main commit gets a full CI result.
  (A shared group name would let GitHub drop an older pending run.)
- Top-level `permissions: contents: read`; widen per job only where a step needs it.

## Run affected tests locally before pushing

CI is the backstop, not the first place a failure should appear. Before pushing, run the fast checks and the tests
for the code you touched (for example `pytest path/to/changed_module`, or `vitest related <changed files>`). If a
repo has a pre-push hook or `make check`, point it at the same commands `fast` runs, so local and CI agree.

## Branch protection: require the one summary job

The summary job (`ci-summary` in the template) is the **only** check that branch protection or an approval tool
should require. It runs with `if: always()` and passes only when every job it `needs` reports exactly `success`
(a positive check, so a failed, cancelled, skipped, or any unrecognised result fails it). Because protection names only this job, you can add, rename, or split
`fast`/`test` jobs without editing protection settings.

**The summary job's name is whatever the repo's protection or approval tool already requires.** If a repo already
requires an aggregate check (for example a final job named `unit` in a workflow named `tests`), keep that exact job
name when adopting the template: rename the template's `ci-summary` job to match, rather than changing the repo's
protection to `ci-summary`. The **workflow** `name:` needs to match too only if some tooling keys on the workflow
name (for example an approval tool that looks checks up by workflow); otherwise leave it. Use `ci-summary` only in
a repo that has no existing required name. The audit script never depends on that name; the optional wait-for-CI
step in the deploy guard reads it from the `CI_CHECK_NAME` variable.

**Private repos on GitHub's free plan have no branch protection.** There the summary job is advisory: nothing stops a
merge or a deploy on red. In that case **the deploy guard is the binding check**, so the deploy workflow must run the
guard (`deployed-on-trunk`) before it deploys, and nothing else should be able to deploy (no manual deploy path
that skips the workflow). Also enable the optional **wait-for-CI gate** in `deploy-guard.yml` (set the repo
variable `WAIT_FOR_CI=true`): the guard job then polls the CI summary check on the exact deploy commit and refuses
to deploy unless it is green within a timeout (default 25 minutes), failing closed on a missing, failed, or
pending check. It is polling rather than a `workflow_run` trigger because that works identically for a push and
a manual deploy of a specific sha, and it only needs `checks: read` on the guard job.

## Know which commit is live

Three pieces make the deployed commit visible, so the audit has something to read:

1. **Stamp the image.** Pass the commit as a build arg (`--build-arg GIT_SHA=$DEPLOY_SHA`), and in the Dockerfile
   `ARG GIT_SHA=unknown`, `ENV GIT_SHA=$GIT_SHA`, `LABEL org.opencontainers.image.revision=$GIT_SHA`.
2. **Serve it.** An unauthenticated `GET /version` returns `{"commit": "<sha>"}`. A commit hash is not a secret;
   keep `/health` for liveness only.
3. **Record it.** The deploy workflow creates a GitHub Deployment for the environment with that sha, then posts
   `in_progress`, then `success` or `failure` statuses (`gh api repos/{repo}/deployments` and `.../statuses`; the job
   needs `deployments: write`). One Deployment per environment per deploy, so staging and production each have their
   own history.

Audit any time, from anywhere with read access to the repo:

```bash
# From the latest successful GitHub Deployment of an environment
python3 execution/scripts/check_deployed_on_trunk.py --repo OWNER/NAME --from-deployments production
# From the live version endpoint
python3 execution/scripts/check_deployed_on_trunk.py --repo OWNER/NAME --from-url https://app.example/version --json-field commit
# From a sha you already have
python3 execution/scripts/check_deployed_on_trunk.py --repo OWNER/NAME --sha <sha>
```

It prints one line. Exit `0` means on trunk, `1` means **not** on trunk, `2` means the check could not run (network,
auth, unknown repo or sha, no successful deployment). Exit 2 is "unknown" and must never be read as a pass. It uses
the GitHub compare API: `behind` or `identical` means the sha is an ancestor of the branch; `ahead` or `diverged`
means it is not. Set `GH_TOKEN` for private repos. The script never follows a redirect on its GitHub calls, so the
token cannot be forwarded to another host.

**What each source proves.** `--from-deployments` checks the commit the *deploy workflow recorded*. `--from-url`
checks the commit the *running service reports about itself*: it verifies a self-reported commit, **not the image
or the code**, so a service that reports a stale or wrong value passes. Use both when you can; the deployment
record is the independent one.

**The audit judges ancestry as of now.** Both sources are checked against the trunk tip at the moment the audit runs
(`check_deployed_on_trunk.py --from-deployments`, `--from-url`, `--sha`, and the scheduled `trunk-audit.yml`). It
cannot tell *deployed-then-merged* from *merged-then-deployed*, so it is a weaker, later check than the deploy-time
guard and is not proof that what was deployed had been reviewed on trunk. Do not cite a passing audit as evidence
that a past deploy was on trunk when it happened; the guard's run for that deploy is that evidence.

The GitHub Deployment record's `ref` is written as the commit sha (`DEPLOY_SHA`), so the record is pinned to the
commit rather than to a branch name that can move. It is **provenance only**: it says what the workflow claims it
deployed, not that the commit was on trunk then.

## Adopt it in an existing repo

1. **Measure first.** Note today's per-PR CI time and which required check names protection or your approval tool
   uses. Those names are fixed points; the template adapts to them.
2. **Copy the workflows.** Copy `ci.yml` and `nightly.yml` into `.github/workflows/`. If you already have a workflow
   with the same name, merge rather than replace.
3. **Fill the slots.** Put lint, format, typecheck and hygiene commands in `fast`; put the test command in `test`
   with a shard flag; replace the failing placeholder steps (an unfilled slot fails on purpose). Pick a shard count that keeps each shard under about 10 minutes.
4. **Name the summary job to match.** Rename the summary job to the aggregate check your protection already
   requires (rename the workflow `name:` only if tooling keys on it); otherwise keep `ci-summary`. Keep `needs:` listing every job it should gate.
5. **Move heavy suites to nightly.** Anything that would push a PR past 15 minutes goes to `nightly.yml`
   (performance audits, native or integration suites, evals). Remove it from `ci.yml`. Trigger it once by hand
   (`workflow_dispatch`) to prove it runs.
6. **Wire the deploy.** Vendor the guard action (see below), add the `guard` job from `deploy-guard.yml` to the
   deploy workflow and make the deploy job `needs: guard`, or copy the file whole. Add the `GIT_SHA` build arg, the `/version` endpoint, and the Deployment
   record steps. Deploy only from the default branch (or a commit that is on it).
7. **Require only the summary job.** In branch protection, replace the individual required checks with the single
   summary job. On a free-plan private repo (no protection), skip this step: the guard in step 6 is what binds.
8. **Schedule the audit.** Copy `check_deployed_on_trunk.py` to `scripts/` and `trunk-audit.yml` to
   `.github/workflows/`; run it once with `workflow_dispatch`.
9. **Verify the audit.** After the first deploy, run `check_deployed_on_trunk.py --from-deployments <env>` and
   `--from-url`; both should report ON TRUNK and name the same commit. Then deploy a throwaway branch commit in a
   scratch environment (or run the guard via `workflow_dispatch` with that sha) and confirm the guard refuses it.

### Vendor the guard; pin everything else

**Default: vendor the guard action into the app repo.** Copy
[`action.yml`](../../.github/actions/deployed-on-trunk/action.yml) to `.github/actions/deployed-on-trunk/action.yml`
and use `uses: ./.github/actions/deployed-on-trunk`, as `deploy-guard.yml` does. That removes the cross-repo
dependency: the deploy path no longer runs code from another repository at deploy time, and updating the guard
becomes an ordinary reviewed change in your repo. The guard only reads git history, so it changes rarely.

**Alternative: use it from the ateles repo, pinned by SHA.**
`markmhendrickson/ateles/.github/actions/deployed-on-trunk@<40-char sha>`. A private repo can use an action from a
public repo. Never use `@main` outside a short evaluation: a later change to the action would run in your deploy
without review.

**Pin every third-party action, not only the guard.** `actions/checkout`, `actions/setup-python`,
`actions/setup-node` and every other `uses:` in these workflows are shown with movable tags (`@v4`) for
readability. A tag can be re-pointed. In a real repo pin each one to a full commit SHA, keep the tag in a trailing
comment, and let a bot (Dependabot's `github-actions` ecosystem, or a pin-updater) propose bumps for review.
Checkouts also set `persist-credentials: false` so the token is not left in the working tree for later steps. The
single exception is the deploy `guard` job, whose action fetches trunk from origin and needs the credentials on a
private repo (that job is read-only).

## Watch CI in the background

Do not sit in the foreground polling a run. Start the watcher in the background and get a notification when it ends:

```bash
gh run watch --exit-status   # launch as a background command; its exit fires the notification
```

In an agent session, launch it as a background command (or a monitor) and carry on; for a PR use
`gh pr checks --watch` the same way. Report on any state change, not only success, so a failure is not mistaken
for "still running".
