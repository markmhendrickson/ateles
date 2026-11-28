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
| Lens reviews (`lens_review:<lens>`) | Vetted for Fly and opted in per lens through `APIS_FLY_RUNNER_CLASSES`; nothing moves until a class is listed. Limits: the lens holds a read-only GitHub token and has no Neotoma tools on the run (a seated run ships no MCP server); it returns its review between `<<<DISPATCHER_REVIEW>>>` markers and the Mac posts it as the lens's own App when it has one (otherwise under the existing shared identity, like any fallback post), failing closed unless exactly one well-formed block came back; a lost or unknown run is a failed lens and is re-run by the bounded missing-lens sweep. The qa lens needs a writable worktree, so it stays on the Mac and is not listed. |
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

### Requiring one provider for a task

A PR-delivery task that Apis dispatches may carry the tag
`harness-provider:claude`, `harness-provider:codex` or
`harness-provider:cursor` (the existing `tags` field). Every attempt then uses
that provider only: first dispatch, watchdog retry, deferred or reconciled
dispatch, release of an approved checkpoint, the Fly job and the local run when
Fly declines it. The pin only narrows the router's choice. The provider must be
enabled and pass the same binary, cooling, usage, headroom and Fly checks;
otherwise the run is refused with the reason and nothing fails over. A near
miss (other spelling, case or value), two different pins, or a pin on a task
that is not a PR delivery refuses the dispatch. Checkpoint, action and approval
gates are unchanged, and changing the tags of a held task still requires fresh
approval. Tags are read from the task only on this path: `dispatch_role.py`
takes `--provider`, and the issue and pull-request pipelines ignore task tags.

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

A login claim Fly refuses (409 or 412), or accepts and then does not keep (the
settled, stopped machine carries no job), is tried again from a fresh read
after each of a bounded series of waits that never reach the run's deadline,
and then refused with a retry time and a reason label (`reverted` when the
update can no longer apply, `not_applied` when it still may). A machine that
never settles, or that holds another run's or an unattributable job, fails the
launch with no retry. When the claim is given up for host memory or because
the update was not kept, and the clean-up verifies that nothing is left and the
update can no longer apply, a run that went to Fly by its action class, with no
pinned or preferred provider, no pinned model and no `runner="fly"`, tries the
next provider on Fly without cooling this one. Forced runs, including every
repository delivery, keep the refusal.

A login claim whose update got no clear answer can be applied minutes later,
so a machine that still reads clean at the old version keeps its reservation
until the update is seen to land, the version moves, or 15 minutes pass on a
settled machine. The start that follows an accepted update is sent again, a
bounded number of times, only for Fly's 412 "machine build not ready". The
reaper and Apis's periodic step also clear a job config nothing owns: the
machine not running, the job naming its run, no ledger reservation for the
machine or run, no holder of the provider's login lock, and no holder of the
machine's lease, which the dispatcher then takes for the clear. A job that
names no run, or whose ownership cannot be read, stays for an operator.

The dispatcher retains Fly API authority and App private keys. Job credentials
arrive as machine-level files under `/run/job/`, not Fly app secrets that would
be injected into every machine. The runner consumes and removes those files
before starting the CLI, constructs its child environment from the shared
allowlist, and keeps signing/result-publishing authority out of the child.
A role needs its own Neotoma identity; the shared daemon bearer is not a Fly
fallback. Probe reports contain names and presence checks, never values.
A Fly job reaches Neotoma through the runner's root-owned signing proxy, which
signs each request with the role's AAuth key; the child never holds the key or
a bearer. The role needs an active `agent_grant` that pins that key. Before a
role granted Neotoma tools is sent to Fly, the dispatcher asks Neotoma, with a
signed `GET /session`, whether the key is admitted; a refused key, or a
provider the proxy does not serve yet (only Claude today), keeps the job off
Fly rather than running it without its tools. The only other way such a role
runs on Fly is the legacy path of its own Neotoma token on Claude; no new
tokens are minted for it. A seated run still gets no Neotoma tools on Fly.

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

Fly jobs may have their own execution timeout, `APIS_FLY_DISPATCH_TIMEOUT`
(seconds), without widening `APIS_DISPATCH_TIMEOUT`, which every local run
keeps. Unset, a Fly job uses the shared timeout exactly as before. Set, a Fly
job with no timeout of its own runs with it, a caller's shorter timeout (a lens
review, for example) is kept, and a caller's longer one is lowered to it, so no
Fly job runs past it. It is resolved only once a job is bound for Fly: a job
that falls back to the Mac keeps the shared timeout. An invalid value (zero,
negative, not a plain decimal, or out of range) disables Fly like any other
invalid limit: every launch is refused before a token is minted or a machine
requested, with no local fallback, and the error names the setting, never the
value. A delivering job must still end within its one-hour GitHub token, so a
45-minute timeout fits and a 60-minute one is refused before the token is
minted. The task watchdog waits out the queue wait, the longer of the two
timeouts, the delivery reserve and the Apis grace before it treats a Fly run as
stalled.

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

## Pausing every Fly call from this host

A production deploy can need a window in which this host makes no Fly API
call at all, without unloading Apis. One command sets, shows and clears that
pause:

```bash
python3 execution/scripts/fly_pause.py set --reason "neotoma deploy" [--minutes 60]
python3 execution/scripts/fly_pause.py show    # exit 1 while paused
python3 execution/scripts/fly_pause.py clear
```

The default is 60 minutes, and a pause over 12 hours is refused. The record
is a small file at one fixed path, `~/.config/ateles/fly-runner-pause.json` in
the account's home directory. No environment, dotenv or ledger setting
(including `APIS_FLY_LEDGER_FILE`) moves it, so the file the command writes is
the file Apis, `dispatch_role.py` and `fly_lens_review.py` read; `set` prints
the path. It holds the expiry time and the reason. The reason is shown by
`show` and in the logs only: refusals carry the expiry, never the reason, so
no reason text can read as a provider failure. Once the expiry passes, the pause is over, and
nobody needs to clear it.

While the pause is in force:

- **No Machines API request leaves this host through the Fly runner.** Every
  request goes through `FlyMachinesClient._call`, which refuses before any
  request is built. That covers the create, get, update, start, stop, lease,
  destroy, list and wait calls, so it covers launches, polls, clean-up, the
  reaper, Apis's periodic settlement and `fly_machine_status.py`.
- **New launches refuse up front** with `Fly paused until <time>`.
  This covers Apis dispatch, `dispatch_role.py --runner fly` and
  `fly_lens_review.py`. Nothing is minted, reserved or admitted, and the work
  never runs locally instead. For a task Apis dispatches, the result reads
  `fly runner refused: Fly paused until ...`. `fly_lens_review.py` exits 5 and
  stops before its next lens. Do not run those lenses locally instead.
- **Apis's periodic step and the reaper skip.** Each logs `periodic step
  skipped` or `reaper skipped`, with the pause message. Marked reservations
  stay as they are and are settled by the first periodic step after the pause
  ends. The rest of Apis's work carries on as normal.
- **A job already running on Fly is left alone.** It is not stopped, and
  nothing asks Fly about it. The caller waiting on it checks the pause every 30
  seconds, without calling Fly, and goes back to polling once the pause ends.
  If the job's deadline passes during the pause, the caller sees a timeout
  with an unknown outcome and `remote_state` `unconfirmed`. The job's
  reservation and lease are kept, marked, and are settled after the pause.
  The task's own `fly_runner_result` in Neotoma is still the record of what
  the job did.
- **Nothing is released while paused.** No path releases a lease, drops a
  reservation or reports an outcome it could not read.

**An unreadable pause record counts as a pause.** A file that exists but cannot
be read or parsed blocks every Fly call, and its message says to clear it with
`python3 execution/scripts/fly_pause.py clear`.

The pause covers code that goes through the Fly runner. A deploy you run
yourself with `fly deploy` or `flyctl` is not blocked by it: that deploy is
the reason for the window.

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
`codex` or `cursor`, and defaults to `all` when omitted; `--help` is static.
There are no other options. The token may be a plain token or a Fly macaroon
(`FlyV1 ` plus a token, as `fly_runner` sends it); control characters and
stray spaces are refused.

It checks every requested binding before any request: if one is missing,
malformed or shared with another provider, it reads no machine at all and
reports the faulty bindings. Otherwise it makes one `GET` per requested login
machine through the existing Machines client. The whole run, including
cancelling unfinished reads and closing the client, ends within one 10-second
deadline; a read still unfinished then reports `timed_out`. It takes no lease,
starts, stops, updates or destroys nothing, and touches no ledger, executor or
Neotoma record. It only calls the official Machines API base, so a configured
override is refused. The HTTP client's own loggers, including their child
loggers, are silenced while it runs.

The output is always one JSON object of at most 4 KiB:

- `machines`: per provider, `machine_state` (a Fly machine state, `absent` for
  an explicit 404, or `unknown`) and whether a run marker or any job config is
  present (`true`/`false`, or `null` when it could not be read).
- `errors`: per provider (or `null` for the whole command), a fixed `code` and,
  for an HTTP failure, its numeric status. The codes are `missing_configuration`,
  `invalid_configuration`, `missing_binding`, `invalid_binding`,
  `duplicate_binding`, `request_failed`, `malformed_response`,
  `unrecognized_state`, `timed_out`, `invalid_arguments`, `interrupted` and
  `unexpected_error`. A well-formed machine whose state the command does not
  recognize reads `unknown` with `unrecognized_state`, not
  `malformed_response`: the response was fine, the state is new to the command.
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
