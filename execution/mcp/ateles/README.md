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
- partial failures appear under `unreadable` / `listing_errors` rather than being dropped.

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
