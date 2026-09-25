# `ateles` MCP server

Swarm routing, checkpoint management, and read-only swarm observability, exposed
over MCP (stdio) to any connected agent.

Registered in `~/.claude.json` as the `ateles` server, launched via
[`run_ateles_mcp.sh`](run_ateles_mcp.sh).

## Tools

| Tool | Writes? | Purpose |
|---|---|---|
| `get_swarm_roster` | no | Full roster: roles → agent names |
| `route_task` | no | Resolve owning agent + definition + execution policy from a task description |
| `list_checkpoints` | no | Pending `checkpoint_brief`s awaiting the operator |
| `resolve_checkpoint` | **yes** | Approve/reject a checkpoint (the only mutating tool) |
| `get_gate_status` | no | An issue's `gate_status`, `current_owner`, blocking gates, recent `owner_history`, and pipeline state |
| `list_pipeline_queue` | no | What holds the issue-pipeline slot, what is queued, and how long each has waited |
| `get_dispatch_health` | no | Dispatcher liveness, recent pipeline activity, recent dispatch failures |
| `get_task_timeline` | no | One task's history, merged and time-ordered from every record the swarm writes, with each entry's source, plus what could not be joined |
| `watch_swarm` | no | Bounded long-poll (≤45 s): what changed since a cursor for given tasks and checkpoints, and a new cursor |

### Read-only by construction

The observability tools, `get_task_timeline` and `watch_swarm` included, never write gate state. A session advancing its own
gate is the self-certification boundary the dispatcher already maintains
(ateles#230 arch §4, and the `SELF-CERTIFICATION BOUNDARY` comment in
`execution/daemons/apis/swarm_dispatch.py`, where even an auto-re-review never
flips a gate — only the lens agent that owns it does).

`test_observability_tools_are_read_only` asserts by source inspection that no
observability handler can reach `_correct`. **Treat any diff that weakens or
removes that test as a blocking architectural concern, not a QA nit.** A future
mutating tool belongs behind the same operator-approval path as
`resolve_checkpoint`, never as a free-form gate setter.

### Reads fail closed

`get_gate_status` and `list_pipeline_queue` distinguish *"no data"* from *"could
not read the data"*, and never report the second as the first:

- a failed marker read yields `stage: "unknown"` with the reason, not "no pipeline running";
- a failed issue listing yields an `error`, not an empty all-clear;
- partial failures appear under `unreadable` / `listing_errors` rather than being dropped.

An empty queue therefore means the queue is genuinely empty. This is control #9
of the Agentic SDLC security enforcement plan: a monitor that under-reports on
auth failure is worse than no monitor, because it produces confident silence
exactly when something is wrong. Its acceptance test is to revoke the credential
and confirm the tool reports unknown-with-reason.

## Watching swarm work (ateles#1275, slice 1)

A session that hands work to the swarm should be able to follow it as easily as
a subagent it started: a handle, a notice when it finishes, and readable output.
The handle is the Neotoma `task` entity id. `get_task_timeline` is the readable
history, `watch_swarm` is the change feed, and [`watch.py`](watch.py) is the same
watch code as a blocking command for a background notice. All three are
read-only; nothing here writes, pauses, or stops anything.

**Watch task X** — available now.

```text
get_task_timeline(task_entity_id="ent_18cb45736689441229b2b7f4")
  → current.phase "ended", agent "corvus"; timeline ends
    runner_started codex:corvus → runner_ended "timeout after 1800s" → status failed
```

In Claude Code, start the watcher with `run_in_background`; the harness tells the
session when it exits, and the session then reads the timeline:

```bash
.mcp-venv/bin/python execution/mcp/ateles/watch.py --task ent_18cb45736689441229b2b7f4 --until change
# prints e.g. "ent_18cb… status: failed -> routed (by apis) at …", then "cursor: w1.…", and exits 0
```

`--until terminal` waits for done/declined/superseded instead of any change;
`--checkpoints` also stops on any checkpoint raised or resolved; `--timeout`
(default 3600 s) exits 0 saying nothing changed. Exit 3 means the record could not
be read three times running; exit 2 means an id is not a task or the cursor is too
old to resume. On a harness with no background path, poll instead:
`watch_swarm(task_ids=["ent_…"])` once for a cursor, then
`watch_swarm(cursor=<cursor>, task_ids=["ent_…"], wait_seconds=45)` repeatedly.
Checkpoint changes come back ordered by when each checkpoint was raised.

**What is agent Y doing** — not available in this slice. The record has no
per-runner row and no lease: runner events are observations of one shared
`harness_event` entity per agent, a start and its end share no id, and most
issue and PR runs carry no task reference. That is record fix W1 in the [design](https://github.com/markmhendrickson/ateles/issues/1275#issuecomment-5832018502).

**Show workflow Z by step** — not available in this slice. Step state is split
across `issue.gate_status`, `participation_record` rows that are mostly never
closed, and GitHub markers. That is record fix W2 in the same design.

What `get_task_timeline` joins today, and what it states it could not:

- joined: the task's own observations (status, reason, result, assignment, with
  the writer where the record names one); its checkpoints (raised, resolved,
  released); runner start and end from `harness_event` observations carrying the
  task id; `participation_record` rows for the task; escalations about it; and,
  when the task is related to an issue or pull request, that entity's
  `gate_status` and the PR and review events naming it.
- bounded: runner and GitHub events are found by scanning shared
  `harness_event` observations in a window around the task's own history
  (at most `ATELES_TIMELINE_WINDOW_HOURS`, default 72 h, and
  `ATELES_TIMELINE_MAX_SCAN`, default 2,000 rows). The response's `window` says
  what was scanned and `gaps` says what was not.
- not in the record: the runner's output (a host-local file; a timed-out run
  keeps none).

Scope of what both tools will read and report:

- **Tasks only.** An id whose entity is not a `task`, or whose type the record
  does not state, is refused. `watch_swarm` re-checks on every call, not only
  the first, so a hand-built cursor cannot skip the check.
- **Task fields from an allowlist.** Only lifecycle, ownership, scheduling and
  routing fields (`status`, `blocked_reason`, `result`, `assigned_to`, `owner`,
  `title`, `priority`, `due_date`, timestamps and the like; the set is
  `_TASK_REPORTED_FIELDS` in `server.py`) are reported. Free text and payment-,
  contact- and email-shaped fields are withheld, value and name.
- **Cursors expire.** A cursor older than `ATELES_WATCH_MAX_CURSOR_AGE_HOURS`
  (default 7 days) is refused with a message to start a fresh watch, so one
  resumed poll cannot re-read an unbounded span. `watch.py` exits 2 on it
  rather than retrying.

## Operator provisioning

### Required

| Variable | Used by | Notes |
|---|---|---|
| `NEOTOMA_BEARER_TOKEN` | all Neotoma-backed tools | Loaded from `~/.config/neotoma/.env` by the wrapper |
| `NEOTOMA_BEARER_TOKEN_PROD` | all Neotoma-backed tools | **Promoted over the local token whenever `NEOTOMA_BASE_URL` is remote.** The shared env file's `NEOTOMA_BEARER_TOKEN` is local-scoped and 401s against prod, so without this the server connects and every call fails auth |
| `APIS_CHECKPOINT_REQUIRED_APPROVER_JKT` | checkpoint creation and resolution | RFC 7638 thumbprint of the resolver key. There is no default: missing or malformed configuration refuses authority creation and resolution. Configure the same value for the checkpoint producer, Apis consumer, and MCP server. |
| `APIS_CHECKPOINT_PRODUCER_JKT` | checkpoint creation and authorization read-back | RFC 7638 thumbprint derived from the public members of the existing `apis.jwk.json`. There is no default: missing configuration, a different signing key, or a creation observation attributed to any other key refuses authority. Configure the same value for the Apis producer, Apis consumer, and MCP server. |

### Required for queue visibility

| Variable | Used by | Notes |
|---|---|---|
| `APIS_GITHUB_TOKEN` / `GITHUB_TOKEN` / `GH_TOKEN` | `list_pipeline_queue`, `get_gate_status` pipeline leg | First match wins. Needs read access to issues + issue comments. **Missing or expired token → an explicit error, never an empty queue** — a missing token reads as *unconfigured*, not as *nothing queued* |

### Optional

| Variable | Default | Effect |
|---|---|---|
| `NEOTOMA_BASE_URL` | `https://neotoma.markmhendrickson.com` | Neotoma instance; a local/loopback host disables prod-token promotion |
| `APIS_CHECKPOINT_REQUIRED_APPROVER_SUB` | `ateles@ateles-swarm` | AAuth subject allowed to resolve execution checkpoints. The MCP service never loads this principal's private key. |
| `APIS_CHECKPOINT_PRODUCER_ISS` | `https://markmhendrickson.com` | Issuer placed in the Apis producer's RFC 9421 agent token. The producer loads only `apis.jwk.json`; it never loads the resolver key. |
| `APIS_RESUME_REPOSITORIES` | `<owner>/ateles,<owner>/neotoma` | Repos scanned for pipeline markers (mirrors the dispatcher's own key) |
| `APIS_MAX_CONCURRENT_ISSUE_PIPELINES` | `3` | Reported as `slot_capacity` |
| `ATELES_LOG_DIR` | `~/Library/Logs/ateles` | Where `get_dispatch_health` reads `apis.log` |
| `DISPATCH_FAILURE_LOG_DIR` | `<ATELES_LOG_DIR>/dispatch-failures` | Recent dispatch-failure files |
| `ATELES_APIS_LAUNCHD_LABEL` | `com.ateles.apis` | launchd label checked for daemon liveness |
| `ATELES_PIPELINE_MARKER_STALE_SECONDS` | `21600` (6h) | Older markers report as `stale`, not running |
| `ATELES_PIPELINE_QUEUE_SCAN_LIMIT` | `60` | Bounds the queue sweep; truncation is reported, never silent |
| `ATELES_PIPELINE_QUEUE_WORKERS` | `12` | Parallelism of the sweep |
| `ATELES_TIMELINE_WINDOW_HOURS` | `72` | Most hours of a task's history `get_task_timeline` scans for runner events |
| `ATELES_TIMELINE_MAX_SCAN` | `2000` | Most `harness_event` observations one timeline or watch poll reads |
| `ATELES_WATCH_POLL_SECONDS` | `5` | Interval between polls inside one `watch_swarm` wait |
| `ATELES_WATCH_MAX_CURSOR_AGE_HOURS` | `168` (7 days) | Oldest cursor `watch_swarm` resumes from; older ones must start a fresh watch |
| `ATELES_MCP_VENV` | `<repo>/.mcp-venv` | Override the interpreter environment |

The wrapper reads `~/.config/neotoma/.env` itself (the path is overridable with
`NEOTOMA_ENV_FILE`), so in the normal case **no variable needs exporting by hand**
— existing environment always wins, so an explicit override still applies.

## Environment

The server needs `mcp`, `httpx`, `cryptography`, and `PyJWT`. The latter two
verify caller-signed approval and rejection corrections. The repo-root `.venv` used by the daemons does
**not** carry `mcp`; `.mcp-venv` does, and is what CI builds in
`.github/workflows/ateles-tests.yml`. The wrapper prefers `.mcp-venv`,
deliberately does not fall back to `.venv` (that would reintroduce a silent
no-tools start), and bootstraps a missing venv with CI's own recipe:

```bash
uv venv .mcp-venv && VIRTUAL_ENV=.mcp-venv uv pip install -r execution/mcp/ateles/requirements.txt
```

The `mcp<2` pin is deliberate: 2.0 renamed `Tool.inputSchema` and removed
`Server.list_tools`, both of which this server still uses.

## Resolving a checkpoint

`resolve_checkpoint` does not possess the resolver's private key. A separate
trusted caller uses
`lib.daemon_runtime.checkpoint_resolution.sign_checkpoint_resolution` with the
existing resolver JWK path, expected subject, issuer, and the JKT derived from
that key's public companion. The helper signs the exact compact, sorted
`POST <NEOTOMA_BASE_URL>/correct` body in memory and returns the five
`resolver_aauth_headers`; it never prints or forwards the private JWK. The body
fields are:

```json
{"entity_id":"<checkpoint>","entity_type":"checkpoint_brief","field":"status","idempotency_key":"resolve-checkpoint-<checkpoint>-<approved-or-rejected>","value":"<approved-or-rejected>"}
```

The server verifies the HTTP signature, exact body, short lifetime, subject,
and pinned key thumbprint before forwarding the correction. Neotoma verifies
the same RFC 9421 signature before writing and records the resolver identity on
the immutable status observation. Apis checks that observation again before
either releasing or declining the linked task. Missing policy state, resolver
headers, key pin, or authenticated read-back leaves the task held.

Checkpoint creation uses the same RFC 9421 wire mechanism for `POST /store`,
but a different key: Apis loads only its existing `apis.jwk.json`, signs the
exact canonical store bytes, proves that key's RFC 7638 thumbprint equals
`APIS_CHECKPOINT_PRODUCER_JKT`, records the pin inside the immutable authority
envelope, and requires the creation observation's authenticated subject and
thumbprint to match on read-back. A merely non-empty or otherwise trusted key
does not qualify. The resolver key remains outside both Apis and the MCP
server.

## Tests

```bash
.mcp-venv/bin/python execution/mcp/ateles/test_server.py
.mcp-venv/bin/python execution/mcp/ateles/test_server_smoke.py
.mcp-venv/bin/python -m pytest execution/mcp/ateles/ -q   # includes test_swarm_watch.py
```

## Troubleshooting

**The tools are absent from a session and nothing errored.** This server fails
silently by nature: if the launcher cannot start, the tools simply are not there.
Check that `run_ateles_mcp.sh` exists at the path in `~/.claude.json`, is
executable, and runs standalone:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' | execution/mcp/ateles/run_ateles_mcp.sh
```

Diagnostics go to stderr; stdout is the protocol channel. A tool list in reply
means the launcher is healthy. Restart the host afterwards — a session's tool
list is fixed at startup, so a newly-fixed server does not appear mid-session.
