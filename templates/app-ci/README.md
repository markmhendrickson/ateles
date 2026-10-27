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
| `deploy-guard.yml` | `.github/workflows/deploy.yml` (or merge into yours) | Refuses off-trunk deploys, stamps the commit, records a GitHub Deployment |

The reusable guard itself is the composite action
[`.github/actions/deployed-on-trunk`](../../.github/actions/deployed-on-trunk/action.yml). The after-the-fact audit is
[`execution/scripts/check_deployed_on_trunk.py`](../../execution/scripts/check_deployed_on_trunk.py).

## The 15-minute budget

Per-PR CI targets **under 15 minutes end to end**. Every job carries a `timeout-minutes` (5 for `fast`, 12 for each
`test` shard) so a hang fails loudly instead of burning an hour.

**The rule: a suite that would push PR CI past the budget moves to nightly / release-candidate.** Do not raise a
timeout to make a slow suite fit. Split it into more shards if it parallelizes, otherwise move it to `nightly.yml`.
Typical nightly candidates are performance audits, native or integration suites, and evals. Nightly also runs on
pushes of `rc-*` tags and `release/*` branches, so a release candidate gets the full set before promotion.

How the template stays fast:

- `fast` (lint, format, typecheck, hygiene) runs first; `test` jobs `needs: fast`, so cheap failures surface early.
- `test` is a matrix of parallel shards with `fail-fast: true`: one red shard cancels the rest.
- Dependencies are cached (`cache: pip` / `cache: npm` on the setup actions).
- A new push to a PR cancels the superseded run. `cancel-in-progress` is `${{ github.event_name == 'pull_request' }}`,
  so **push-to-main runs are never cancelled**: they gate deploys and every one must finish.
- Top-level `permissions: contents: read`; widen per job only where a step needs it.

## Run affected tests locally before pushing

CI is the backstop, not the first place a failure should appear. Before pushing, run the fast checks and the tests
for the code you touched (for example `pytest path/to/changed_module`, or `vitest related <changed files>`). If a
repo has a pre-push hook or `make check`, point it at the same commands `fast` runs, so local and CI agree.

## Branch protection: require the one summary job

The summary job (`ci-summary` in the template) is the **only** check that branch protection or an approval tool
should require. It runs with `if: always()` and fails unless every job it `needs` succeeded (a skipped or cancelled
needed job counts as failure). Because protection names only this job, you can add, rename, or split `fast`/`test`
jobs without editing protection settings.

**The summary job's name is whatever the repo's protection or approval tool already requires.** If a repo already
requires an aggregate check (for example a final job named `unit` in a workflow named `tests`), keep that exact job
name and workflow name when adopting the template: rename the template's `ci-summary` job and the workflow `name:`
to match, rather than changing the repo's protection to `ci-summary`. Use `ci-summary` only in a repo that has no
existing required name. The deploy guard and the audit script never depend on that name.

**Private repos on GitHub's free plan have no branch protection.** There the summary job is advisory: nothing stops a
merge or a deploy on red. In that case **the deploy guard is the binding check**, so the deploy workflow must run the
guard (`deployed-on-trunk`) before it deploys, and nothing else should be able to deploy (no manual deploy path
that skips the workflow).

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
means it is not. Set `GH_TOKEN` for private repos.

## Adopt it in an existing repo

1. **Measure first.** Note today's per-PR CI time and which required check names protection or your approval tool
   uses. Those names are fixed points; the template adapts to them.
2. **Copy the workflows.** Copy `ci.yml` and `nightly.yml` into `.github/workflows/`. If you already have a workflow
   with the same name, merge rather than replace.
3. **Fill the slots.** Put lint, format, typecheck and hygiene commands in `fast`; put the test command in `test`
   with a shard flag; delete the placeholder steps. Pick a shard count that keeps each shard under about 10 minutes.
4. **Name the summary job to match.** Rename the summary job (and workflow `name:`) to the aggregate check your
   protection already requires; otherwise keep `ci-summary`. Keep `needs:` listing every job it should gate.
5. **Move heavy suites to nightly.** Anything that would push a PR past 15 minutes goes to `nightly.yml`
   (performance audits, native or integration suites, evals). Remove it from `ci.yml`. Trigger it once by hand
   (`workflow_dispatch`) to prove it runs.
6. **Wire the deploy.** Add the `guard` job from `deploy-guard.yml` to the deploy workflow and make the deploy job
   `needs: guard`, or copy the file whole. Add the `GIT_SHA` build arg, the `/version` endpoint, and the Deployment
   record steps. Deploy only from the default branch (or a commit that is on it).
7. **Require only the summary job.** In branch protection, replace the individual required checks with the single
   summary job. On a free-plan private repo (no protection), skip this step: the guard in step 6 is what binds.
8. **Verify the audit.** After the first deploy, run `check_deployed_on_trunk.py --from-deployments <env>` and
   `--from-url`; both should report ON TRUNK and name the same commit. Then deploy a throwaway branch commit in a
   scratch environment (or run the guard via `workflow_dispatch` with that sha) and confirm the guard refuses it.

### Pin the guard to a SHA

The deploy guard is `markmhendrickson/ateles/.github/actions/deployed-on-trunk@main`. A private repo can use an
action from a public repo, and `@main` works, but `@main` means a later change to the action runs in your deploy
without review. **Recommended: pin to a full commit SHA** (`...deployed-on-trunk@<40-char sha>`) and bump it
deliberately; the guard only reads git history, so it changes rarely. Use `@main` only while evaluating.

## Watch CI in the background

Do not sit in the foreground polling a run. Start the watcher in the background and get a notification when it ends:

```bash
gh run watch --exit-status   # launch as a background command; its exit fires the notification
```

In an agent session, launch it as a background command (or a monitor) and carry on; for a PR use
`gh pr checks --watch` the same way. Report on any state change, not only success, so a failure is not mistaken
for "still running".
