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

### Read-only by construction

The three observability tools never write gate state. A session advancing its own
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
- partial failures appear under `unreadable` / `listing_errors` rather than being dropped;
- `get_gate_status` always emits `gates_evaluated` as a boolean (never omit it —
  absence collapses success into unevaluable under ordinary falsy checks).
  `true` means the record was read and interpreted, including **never-triaged**;
  `false` is reserved for **unreadable** only (wrong entity type / malformed
  `gate_status` → `reason_codes` / `unreadable[]`, **no** `blocking_gates`).
  Never-triaged additionally carries `gates_initialised: false` and
  `reason_codes: ["uninitialised.never_triaged"]` with **no** fabricated pending
  list. **Genuine unsigned** (`gates_evaluated: true`, `gates_initialised: true`)
  uses `blocking_gates` + `all_gates_cleared` for withheld sign-offs. Do not treat
  `blocking_gates` / `all_gates_cleared` as pending when `gates_evaluated` is
  false or gates were never initialised.

An empty queue therefore means the queue is genuinely empty. This is control #9
of the Agentic SDLC security enforcement plan: a monitor that under-reports on
auth failure is worse than no monitor, because it produces confident silence
exactly when something is wrong. Its acceptance test is to revoke the credential
and confirm the tool reports unknown-with-reason.

## Operator provisioning

### Required

| Variable | Used by | Notes |
|---|---|---|
| `NEOTOMA_BEARER_TOKEN` | all Neotoma-backed tools | Loaded from `~/.config/neotoma/.env` by the wrapper |
| `NEOTOMA_BEARER_TOKEN_PROD` | all Neotoma-backed tools | **Promoted over the local token whenever `NEOTOMA_BASE_URL` is remote.** The shared env file's `NEOTOMA_BEARER_TOKEN` is local-scoped and 401s against prod, so without this the server connects and every call fails auth |

### Required for queue visibility

| Variable | Used by | Notes |
|---|---|---|
| `APIS_GITHUB_TOKEN` / `GITHUB_TOKEN` / `GH_TOKEN` | `list_pipeline_queue`, `get_gate_status` pipeline leg | First match wins. Needs read access to issues + issue comments. **Missing or expired token → an explicit error, never an empty queue** — a missing token reads as *unconfigured*, not as *nothing queued* |

### Optional

| Variable | Default | Effect |
|---|---|---|
| `NEOTOMA_BASE_URL` | `https://neotoma.markmhendrickson.com` | Neotoma instance; a local/loopback host disables prod-token promotion |
| `APIS_RESUME_REPOSITORIES` | `<owner>/ateles,<owner>/neotoma` | Repos scanned for pipeline markers (mirrors the dispatcher's own key) |
| `APIS_MAX_CONCURRENT_ISSUE_PIPELINES` | `3` | Reported as `slot_capacity` |
| `ATELES_LOG_DIR` | `~/Library/Logs/ateles` | Where `get_dispatch_health` reads `apis.log` |
| `DISPATCH_FAILURE_LOG_DIR` | `<ATELES_LOG_DIR>/dispatch-failures` | Recent dispatch-failure files |
| `ATELES_APIS_LAUNCHD_LABEL` | `com.ateles.apis` | launchd label checked for daemon liveness |
| `ATELES_PIPELINE_MARKER_STALE_SECONDS` | `21600` (6h) | Older markers report as `stale`, not running |
| `ATELES_PIPELINE_QUEUE_SCAN_LIMIT` | `60` | Bounds the queue sweep; truncation is reported, never silent |
| `ATELES_PIPELINE_QUEUE_WORKERS` | `12` | Parallelism of the sweep |
| `ATELES_MCP_VENV` | `<repo>/.mcp-venv` | Override the interpreter environment |

The wrapper reads `~/.config/neotoma/.env` itself (the path is overridable with
`NEOTOMA_ENV_FILE`), so in the normal case **no variable needs exporting by hand**
— existing environment always wins, so an explicit override still applies.

## Environment

The server needs `mcp` + `httpx`. The repo-root `.venv` used by the daemons does
**not** carry `mcp`; `.mcp-venv` does, and is what CI builds in
`.github/workflows/ateles-tests.yml`. The wrapper prefers `.mcp-venv`,
deliberately does not fall back to `.venv` (that would reintroduce a silent
no-tools start), and bootstraps a missing venv with CI's own recipe:

```bash
uv venv .mcp-venv && VIRTUAL_ENV=.mcp-venv uv pip install "mcp>=1.1.0,<2" httpx
```

The `mcp<2` pin is deliberate: 2.0 renamed `Tool.inputSchema` and removed
`Server.list_tools`, both of which this server still uses.

## Tests

```bash
.mcp-venv/bin/python execution/mcp/ateles/test_server.py
.mcp-venv/bin/python execution/mcp/ateles/test_server_smoke.py
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
