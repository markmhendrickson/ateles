# Cloud execution for the Ateles swarm

The current runner target is Fly. Apis can execute an eligible CLI job there
while keeping policy, provider selection and orchestration on its dispatcher
host. Moving a CLI job does not move the daemon fleet or make the dispatcher
independent of its current host.

The earlier VM + Tailscale recommendation and GitHub Actions routing for
Bucket C are superseded. The historical Bucket-A container utilities remain
in [`deploy/cloud/`](../deploy/cloud/README.md); they are not the Fly job image.
In particular, a runner must never use that directory's full-snapshot secret
materialization entrypoint.

## Implementation and activation are separate

| Surface | Implemented boundary |
|---|---|
| [Fly executor](../execution/daemons/apis/fly_runner.py) and [job entrypoint](../deploy/fly-runner/run_job.py) | Merged in [#1460](https://github.com/markmhendrickson/ateles/pull/1460). Supports explicitly forced Fly dispatch and vetted classes listed in `APIS_FLY_RUNNER_CLASSES`; an unset list opts in no classes. |
| [Repository delivery inside a job](../deploy/fly-runner/repo_delivery.py) | Merged in [#1473](https://github.com/markmhendrickson/ateles/pull/1473). A job with a delivery specification can clone, commit, push and open or update a PR. This alone does not route normal builder work to Fly. |
| Dispatcher routing for builder delivery | Merged in [#1475](https://github.com/markmhendrickson/ateles/pull/1475) (2026-10-20, `8656c9a2`). It adds the explicit `open_or_merge_pr` opt-in; merging it does not enable it, and this guide does not enable it. A delivery counts as verified only after the pull request is read back from GitHub and matches the runner's reported repository, base, branch and commit. |

The design starts at [#1456](https://github.com/markmhendrickson/ateles/issues/1456).
Later accepted rulings use a single login machine per provider, stopped between
jobs, including Claude. Earlier per-job Claude and API-key recommendations
must not be treated as the current activation instructions.

A merge, an image build, a successful CLI process or an earlier pilot receipt
does not establish that an instance is deployed or that repository delivery
works. Retrieve its current `deployment_configuration` before any deployment.
It must bind the target, region, admitted source and image, command, credentials
by reference, budget, action gates and verification. Missing bindings remain
unknown; this generic guide cannot supply them.

## What runs where

- **Bucket A: daemon loop bodies.** Network-facing services can be hosted away
  from a device, but moving their SSE loops, state and webhook endpoints is a
  separate deployment. The retained VM scaffold is historical, not a current
  Fly daemon-fleet runbook. Preserve one consumer per daemon during a cutover.
- **Bucket B: device-bound work.** Screen, microphone, desktop control and local
  Google-session capabilities remain on the device that owns them. Moving a
  runner does not move those permissions or sessions.
- **Bucket C: CLI jobs.** Use the existing Fly executor and job image below.
  Each job receives its declared credentials and returns an attributed result.
  GitHub Actions still runs repository CI and its configured review workflow;
  it is not the replacement executor for these Fly jobs.

## Existing runner entrypoints

| File | Responsibility |
|---|---|
| [`deploy/fly-runner/Dockerfile`](../deploy/fly-runner/Dockerfile) | Pinned CLI/runtime image with a narrow build context; no SOPS, age key, private secrets repository or daemon snapshot entrypoint. |
| [`deploy/fly-runner/fly.toml`](../deploy/fly-runner/fly.toml) | Generic image-build configuration with placeholder app and region, no public services or app-level secrets. |
| [`deploy/fly-runner/run_job.py`](../deploy/fly-runner/run_job.py) | Validates one job, prepares its constrained child, records the outcome and clears job material. |
| [`execution/daemons/apis/fly_runner.py`](../execution/daemons/apis/fly_runner.py) | Eligibility, Fly API lifecycle, login queues, budget ledger and result collection. |
| [`execution/daemons/apis/dispatch_role.py`](../execution/daemons/apis/dispatch_role.py) | Existing role-dispatch CLI, including `--runner fly`, `--provider` and `--repository`. |

An image build must use the runner Dockerfile and its adjacent
`Dockerfile.dockerignore`. After the instance binding and build authority are
verified, the existing build-only invocation can be parameterized from that
binding, from the repository root:

```bash
fly deploy \
  --app "${APIS_FLY_APP:?retrieve the instance binding}" \
  --primary-region "${APIS_FLY_REGION:?retrieve the instance binding}" \
  --config deploy/fly-runner/fly.toml \
  --dockerfile deploy/fly-runner/Dockerfile \
  --build-only --push \
  --image-label "${RUNNER_IMAGE_LABEL:?use the admitted source revision}" .
```

This builds and pushes an image; it does not deploy the dispatcher, update the
login machines or authorize running a job. Do not replace `--build-only` with
an ordinary `fly deploy`: the job lifecycle belongs to the existing Machines
API executor. The exact instance command and build arguments come from its
binding. Keep the dispatcher and runner implementation compatible; inspect the
actual machine image digest rather than inferring it from a tag or local HEAD.

### When a runner file changes

Files under `deploy/fly-runner/`, such as `run_job.py` and `repo_delivery.py`,
run inside the job image, not in the dispatcher. Merging a change to these
files does not change what jobs run. After the merge, use the build-only command
above to build and push a new image labelled with the merged commit's short SHA.
Update the dispatcher's configured image reference (`APIS_FLY_IMAGE` in the
instance binding), restart the dispatcher, and confirm that the running process
loaded the new reference. On the first job after the switch, verify that the
machine's image digest matches the pushed image and that the job's signed result
reflects the change.

Changes only to the dispatcher under `execution/daemons/apis/` need a dispatcher
restart, not an image rebuild.

For an already admitted read-only pilot task, the existing dispatcher command
is:

```bash
uv run --python 3.13 --extra dev python execution/daemons/apis/dispatch_role.py \
  --role "${ROLE:?use the task's assigned role}" \
  --task-file "${TASK_FILE:?use the admitted task brief}" \
  --provider "${PROVIDER:?use an enabled provider}" \
  --runner fly --action-class carry_forward_check \
  --repository "${REPOSITORY:?use the admitted owner/repository}" --json
```

This is a real dispatch, not a dry-run or authentication check. It still needs
the role identity, provider login, result grants, budget and action gates.
Forcing the executor does not grant an otherwise ineligible role access.
Provider activation remains a separate setting and ruling: configuring a Claude
login does not reactivate Claude for automatic swarm work.

## Provider login and job credentials

Each provider uses its own single login machine and persistent login volume;
jobs queue for that provider and the machine stops between jobs. Do not copy
one login across concurrent machines. Codex retains its own ChatGPT login,
Cursor its own plan login, and Claude its supplied OAuth token. Claude's token
belongs only on its Fly login volume as a mode-0600 file at
`/data/claude/oauth_token`; it must not be copied into the dispatcher's env file.
No metered API-key fallback is implied by a missing or failed login.

The login volume is the declared persistent exception. Job scratch and the
child's temporary home are separate from that login state. The lifecycle must
confirm the machine stopped and job material was cleared before another job
can use it; a provider-auth failure is not permission to skip that boundary.

The dispatcher retains Fly API authority and App private keys. Job credentials
arrive as machine-level files under `/run/job/`, not Fly app secrets that would
be injected into every machine. The runner consumes and removes those files
before starting the CLI, constructs its child environment from the shared
allowlist, and keeps signing/result-publishing authority out of the child.
A role needs its own Neotoma identity; the shared daemon bearer is not a Fly
fallback. Probe reports contain names and presence checks, never values.
A key-only Fly job signs its result record with AAuth, but its child has no
agent-side Neotoma MCP tools because it has no role bearer for the MCP config.

Repository delivery additionally needs a distinct PR-opener App configuration.
The [existing App helper](../lib/github_app_token.py) resolves that identity;
the delivery path requests a short-lived installation token for exactly one
repository with `contents:write` and `pull_requests:write` (plus GitHub's implicit
metadata read). Do not substitute the approving App or a shared PAT. The
delivery record's schema, the role's store grant and the dispatcher's retrieve
grant must be verified before enabling dispatcher delivery routing.

## Budget and failure readback

The [existing executor](../execution/daemons/apis/fly_runner.py) enforces numeric
configuration bounds and a local dollar ledger. Admission counts completed
spend, login-volume and stopped-rootfs storage, outstanding reservations and the
incoming job's maximum-duration cost against `APIS_FLY_MONTHLY_BUDGET_USD`
(default 20). Daily launch and concurrent-run limits also apply. Invalid or
unreadable ledger state refuses admission. The ledger is an estimate for this
dispatcher, not a provider invoice or proof that a billing alert exists.
A provider billing alert is a separate backstop, not the admission mechanism.

The accepted Fly usage ruling exempts remote jobs from the dispatcher's local
subscription pace and weekly-usage ceilings because the Fly logins are separate.
Local runs retain their local usage rules. The Fly dollar cap still applies;
this does not enable a deactivated provider or authorize a new budget.

Treat launch failure, provider authentication failure, missing result and
deadline expiry as distinct evidence. The pilot's local fallback is controlled
by `APIS_FLY_LOCAL_FALLBACK`; do not describe a fallback as cloud success.
A successful run requires its exact `fly_runner_result` in Neotoma, with the
run, role, provider, machine, exit status and credential-absence report checked.
Machine exit zero alone is insufficient. After repository delivery, also
resolve the recorded remote branch, commit and PR; a missing or undeclared
delivery field is not a successful publication.

Read back the actual machine state, image and volume attachment, and verify
that only the declared provider login persists. Preserve any live review,
credential, schema, grant or activation gate. These checks establish an
instance's behavior; this document and its build template do not assert that
any instance has been deployed.

## Login-machine status for supervision

Supervise from the canonical records first: the task, its `fly_runner_result`
and the dispatcher's ledger. When the live state of a provider's login machine
is needed, use the bounded status command rather than printing a machine
response. A full response carries the job's mounted files and environment, so
serializing it can put job credentials into a transcript or log.

```bash
ATELES_SKIP_DOTENV=1 .venv/bin/python execution/daemons/apis/fly_machine_status.py --provider all
```

Run it from the dispatcher's dedicated checkout with that process's existing
`APIS_FLY_APP`, `APIS_FLY_API_TOKEN` and `APIS_FLY_<PROVIDER>_MACHINE_ID`
bindings. It loads no env or key file. `--provider` takes `all`, `claude`,
`codex` or `cursor`; `--help` is static. There are no other options.

It makes one `GET` per requested login machine through the existing Machines
client, under one 10-second deadline. It takes no lease, starts, stops, updates
or destroys nothing, and touches no ledger, executor or Neotoma record. It only
calls the official Machines API base, so a configured override is refused.

The output is always one JSON object of at most 4 KiB:

- `machines`: per provider, `machine_state` (a Fly machine state, `absent` for
  an explicit 404, or `unknown`) and whether a run marker or any job config is
  present (`true`/`false`, or `null` when it could not be read).
- `errors`: per provider (or `null` for the whole command), a fixed `code` and,
  for an HTTP failure, its numeric status. The codes are `missing_configuration`,
  `invalid_configuration`, `missing_binding`, `invalid_binding`,
  `duplicate_binding`, `request_failed`, `malformed_response`, `timed_out`,
  `invalid_arguments`, `interrupted` and `unexpected_error`.
- `complete`: every requested machine was read. Exit 0 only when complete;
  otherwise exit 2.

It never prints machine or app names, machine or run IDs, versions, files, env,
metadata, error text, URLs or argument values. A complete status is an
observation, not a release: `stopped` with no job config does not mean the
machine is free or may be reused, `absent` does not mean capacity is available,
and nothing in the output authorizes a release or clears a receipt. `absent` is
read from the Machines API's 404, as the lifecycle client reads it; a misnamed
app binding could also answer 404, so an unexpected `absent` means checking
the binding, not concluding the machine is gone. The command
offers a safe path; it does not stop other tools from reading a machine.
