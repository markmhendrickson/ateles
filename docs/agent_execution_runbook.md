# Agent Execution Runbook

## Purpose

Operate the agent platform safely and reliably in a local-first deployment with cloud standby failover.

## Scope

This runbook covers:

- provisioning and startup
- daily operations
- health monitoring
- incident response
- failover and failback
- backup and recovery checks

## Service Inventory

- `agent-core`: scheduler, orchestrator, policy checks
- `worker-browser`: browser automation tasks
- `worker-email`: email connector tasks
- `worker-calendar`: calendar connector tasks
- `worker-generic`: long-tail integrations
- `truth-layer`: system of record
- `cache`: low-latency reads
- `observability`: logs/metrics/alerts pipeline
- `sync-agent`: local-cloud state sync

## Runtime Assumptions

- Primary host is always-on local Mac
- Cloud standby exists and is deployable at any time
- Services run with isolated identities and scoped credentials
- No secrets stored in source control

## Local Control Scripts

Use the repo-managed scripts under:

- `execution/scripts/agent_execution/agent_stack.sh`
- `execution/scripts/agent_execution/run_service.sh`
- `execution/scripts/agent_execution/com_openclaw_agent_core.plist`
- `execution/scripts/agent_execution/com_openclaw_agent_workers.plist`
- `execution/scripts/agent_execution/env_agent_example`
- `execution/scripts/agent_execution/watchdog_health.sh`
- `execution/scripts/agent_execution/com_openclaw_agent_watchdog.plist`

One-time setup:

1. Prepare env file:
   - copy `execution/scripts/agent_execution/env_agent_example` to `execution/scripts/agent_execution/.env.agent`
   - fill `OPENCLAW_AGENT_CORE_CMD`
   - set `OPENCLAW_AGENT_WORKERS_CMD` only if you run a separate long-lived worker process
2. Make scripts executable:
   - `chmod +x execution/scripts/agent_execution/agent_stack.sh execution/scripts/agent_execution/run_service.sh execution/scripts/agent_execution/watchdog_health.sh`
3. Create logs directory:
   - `mkdir -p tmp/agent_logs`

OpenClaw Docker prerequisites (used by default command template):

1. Clone OpenClaw:
   - `git clone https://github.com/openclaw/openclaw.git /Users/markmhendrickson/repos/openclaw`
2. Create OpenClaw runtime dirs:
   - `mkdir -p /Users/markmhendrickson/.openclaw /Users/markmhendrickson/.openclaw/workspace`
3. Create `/Users/markmhendrickson/repos/openclaw/.env` with:
   - `OPENCLAW_IMAGE=ghcr.io/openclaw/openclaw:latest`
   - `OPENCLAW_GATEWAY_TOKEN=<random-hex-token>`
   - `OPENCLAW_CONFIG_DIR=/Users/markmhendrickson/.openclaw`
   - `OPENCLAW_WORKSPACE_DIR=/Users/markmhendrickson/.openclaw/workspace`
   - `OPENCLAW_GATEWAY_BIND=lan`
   - **LLM keys (required for Chat/agents):** `ANTHROPIC_API_KEY=<key>` if your default model uses Anthropic (e.g. `anthropic/claude-*`). Docker Compose passes this into the gateway (`docker-compose.yml` in the OpenClaw repo). Alternatively put the same line in `~/.openclaw/.env` for non-Docker or Node `loadDotEnv` fallback. Without it, Chat shows *No API key found for provider "anthropic"*.
4. Ensure minimal gateway config exists:
   - `/Users/markmhendrickson/.openclaw/openclaw.json`
   - set `gateway.mode=local`, `gateway.bind=lan`, and token auth enabled
5. **Control UI auth (two places):**
   - **Gateway config:** `gateway.auth.token` in `~/.openclaw/openclaw.json` must match `OPENCLAW_GATEWAY_TOKEN` in `openclaw/.env` (non-empty). Restart the gateway after editing (`docker compose restart openclaw-gateway` in the OpenClaw repo).
   - **Browser session (required for the UI):** The dashboard does **not** read that token automatically. Open **Control → Overview**, find **Access**, paste the **same** token into the **Token** field (placeholder `OPENCLAW_GATEWAY_TOKEN`), then click **Connect**. Settings persist in browser `localStorage` (`openclaw.control.settings.v1`). Until this is set, every page (including Config) can show `gateway token missing` and **Schema unavailable** even when the server config is correct.
   - Optional: a one-time `?token=...` query on the dashboard URL is applied and then stripped (see OpenClaw Control UI behavior); prefer Overview paste for routine use. CLI: `openclaw dashboard --no-open` may print a tokenized URL.

Persistent login startup (LaunchAgent):

1. Install plist:
   - `cp execution/scripts/agent_execution/com_openclaw_agent_core.plist ~/Library/LaunchAgents/com.openclaw.agent.core.plist`
2. Reload service:
   - `launchctl bootout "gui/${UID}/com.openclaw.agent.core" 2>/dev/null || true`
   - `launchctl bootstrap "gui/${UID}" ~/Library/LaunchAgents/com.openclaw.agent.core.plist`
   - `launchctl kickstart -k "gui/${UID}/com.openclaw.agent.core"`
3. Verify:
   - `launchctl print "gui/${UID}/com.openclaw.agent.core"`

### Health watchdog (optional)

Runs every 5 minutes (`StartInterval` 300) and on login (`RunAtLoad`). Checks the same URLs as smoke tests; appends successes to `tmp/agent_logs/watchdog.log` and failures to `tmp/agent_logs/watchdog.error.log`.

1. Set optional alert webhook in `execution/scripts/agent_execution/.env.agent`:
   - `OPENCLAW_WATCHDOG_WEBHOOK_URL` — Slack-compatible JSON webhook (body includes `text` and `source`)
2. Install:
   - `./execution/scripts/agent_execution/agent_stack.sh install_watchdog`
3. One-off test:
   - `./execution/scripts/agent_execution/agent_stack.sh watchdog_run`
4. Remove:
   - `./execution/scripts/agent_execution/agent_stack.sh uninstall_watchdog`

## 1) Preflight Checklist

Run before first launch and before major updates.

1. Verify host capacity
   - CPU load is stable
   - memory headroom is available
   - disk free space exceeds 20%
2. Verify credential validity
   - connector auth tokens valid
   - API keys present in secret source
3. Verify network reachability
   - outbound access to connector APIs
   - outbound access to model endpoints
4. Verify storage health
   - truth-layer writable
   - cache reachable
5. Verify cloud standby state
   - deployment healthy
   - last sync age under 5 minutes

Preflight command:

- `./execution/scripts/agent_execution/agent_stack.sh preflight`

## 2) Startup Procedure

1. Start stateful services first
   - truth-layer
   - cache
2. Start control plane
   - `agent-core`
3. Start workers in order
   - `worker-email`
   - `worker-calendar`
   - `worker-generic`
   - `worker-browser` (last, highest risk surface)
4. Start observability and sync services
   - `observability`
   - `sync-agent`
5. Run smoke tests
   - enqueue one test job per connector type
   - verify end-to-end persistence and completion

Startup commands:

1. `./execution/scripts/agent_execution/agent_stack.sh start`
2. `./execution/scripts/agent_execution/agent_stack.sh status`
3. `./execution/scripts/agent_execution/agent_stack.sh smoke`

## 3) Daily Operations

### Health Signals

- queue depth trend
- worker success/failure rate
- p95 job latency
- auth error rates per connector
- sync lag between local and cloud

Daily command set:

- status: `./execution/scripts/agent_execution/agent_stack.sh status`
- recent logs: `./execution/scripts/agent_execution/agent_stack.sh logs 150`
- smoke: `./execution/scripts/agent_execution/agent_stack.sh smoke`

### Operational Thresholds

- queue depth > 3x baseline for 15 minutes: investigate
- failure rate > 5% for 10 minutes: incident
- sync lag > 10 minutes: degrade mode and repair sync
- repeated auth failures: rotate credentials and re-auth

### Routine Cadence

- every morning:
  - review overnight failures
  - confirm sync lag and backup status
- weekly:
  - rotate high-sensitivity credentials
  - patch dependencies and restart during low traffic
- monthly:
  - run failover drill and recovery validation

## 4) Incident Playbooks

### A. Worker Crash Loop

1. Isolate failing worker (do not restart all services).
2. Pause new tasks for that worker queue.
3. Inspect recent logs and last successful job.
4. Roll back most recent config/dependency change.
5. Restart worker and process canary jobs.
6. Resume queue if canary jobs pass.

Immediate commands:

- inspect logs: `./execution/scripts/agent_execution/agent_stack.sh logs 300`
- restart stack safely: `./execution/scripts/agent_execution/agent_stack.sh restart`

### B. Connector Authentication Failure

1. Confirm scope and token expiry.
2. Rotate or refresh connector credentials.
3. Re-run connector-specific smoke test.
4. Re-enable queue consumption.
5. Document root cause and expiry timeline.

After re-auth:

- `./execution/scripts/agent_execution/agent_stack.sh smoke`

### C. Truth-Layer Write Failures

1. Enter controlled degraded mode (stop noncritical writes).
2. Validate storage connectivity and permissions.
3. Check for schema/data contract regressions.
4. Restore write path and replay queued idempotent writes.
5. Verify no data gaps from replay window.

Validation command:

- `./execution/scripts/agent_execution/agent_stack.sh smoke`

### D. Local Host Resource Saturation

1. Reduce worker concurrency.
2. Pause noncritical batch jobs.
3. Offload heavy workloads to cloud workers.
4. If saturation persists, trigger planned failover.

Failover command:

- `./execution/scripts/agent_execution/agent_stack.sh failover_to_cloud`

## 5) Failover Procedure (Local -> Cloud)

Trigger when any of:

- local health checks fail for 10 continuous minutes
- repeated unrecoverable worker failures
- host maintenance/reboot window

Steps:

1. Freeze new local queue intake.
2. Confirm final state sync checkpoint.
3. Promote cloud standby control plane.
4. Start cloud workers in standard order.
5. Run canary jobs (browser, email, calendar).
6. Switch traffic/routing to cloud endpoint.
7. Announce degraded/local-offline state in ops channel.

Runbook commands:

1. `./execution/scripts/agent_execution/agent_stack.sh failover_to_cloud`
2. `./execution/scripts/agent_execution/agent_stack.sh stop`

## 6) Failback Procedure (Cloud -> Local)

1. Stabilize local host and pass full preflight.
2. Sync cloud delta back to local truth layer.
3. Run local canary jobs and compare outputs.
4. Drain cloud queue to safe checkpoint.
5. Move routing back to local endpoint.
6. Keep cloud warm for 30-60 minutes.
7. Return cloud to standby.

Runbook commands:

1. `./execution/scripts/agent_execution/agent_stack.sh start`
2. `./execution/scripts/agent_execution/agent_stack.sh smoke`
3. `./execution/scripts/agent_execution/agent_stack.sh failback_to_local`

## 7) Backup and Recovery

### Backup Policy

- truth-layer snapshots every 15 minutes
- daily encrypted backup retention
- configuration and policy backups on each change

### Recovery Validation

- weekly restore test into isolated environment
- verify data integrity and replay consistency
- track restore duration against RTO

## 8) Security Operations

- run workers as non-admin identities
- enforce network egress allowlists
- separate browser worker from sensitive connectors
- rotate secrets and revoke stale tokens
- maintain immutable audit trail for sensitive actions

## 9) Change Management

For any production-impacting change:

1. create change record with rollback steps
2. deploy in low-traffic window
3. run connector smoke tests
4. monitor 30 minutes before declaring stable

## 10) Quick Triage Commands (Template)

Use these concrete commands:

- service status check: `./execution/scripts/agent_execution/agent_stack.sh status`
- recent logs: `./execution/scripts/agent_execution/agent_stack.sh logs 200`
- connector/truth health: `./execution/scripts/agent_execution/agent_stack.sh smoke`
- preflight verification: `./execution/scripts/agent_execution/agent_stack.sh preflight`
- controlled restart: `./execution/scripts/agent_execution/agent_stack.sh restart`

Keep command aliases in your ops shell profile so incident handling is repeatable and fast.

## Escalation Matrix

- Sev 1 (full outage): immediate failover and live incident channel
- Sev 2 (partial connector outage): isolate connector and continue core operations
- Sev 3 (degradation): schedule remediation, keep service live

## Definition of Done for Operational Readiness

- all services pass startup smoke tests
- monitoring and alerts are active
- failover drill completed successfully
- restore drill completed successfully
- on-call notes updated with current known risks
