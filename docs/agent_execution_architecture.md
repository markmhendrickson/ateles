# Agent Execution Architecture (Local First, Cloud Standby)

## Purpose

Define a production-grade architecture for always-on agent execution with immediate local latency, strong security isolation, and cloud continuity when the local host is unavailable.

## Workload Profile

- Availability target: always on
- Throughput target: medium concurrency
- Latency target: immediate response for interactive actions
- Integration surface: browser, email, calendar, plus additional connectors
- Preference: local-first runtime with optional local LLM support

## Architecture Summary

Primary execution happens on the local Mac host. High-risk workloads are isolated in separate runtime boundaries. A cloud standby plane mirrors critical state and can be promoted during local outages.

## Logical Components

1. Agent control plane
   - Scheduler and workflow orchestrator
   - Job queue and retry policies
   - Policy and permission enforcement
2. Connector plane
   - Browser automation worker
   - Email connector worker
   - Calendar connector worker
   - Generic connector worker pool
3. Memory and state plane
   - Truth-layer persistence (system of record)
   - Local cache for low-latency reads
   - Artifact/object storage for large payloads
4. Model and inference plane
   - Primary model routing (API and/or local inference)
   - Optional local LLM runtime for private tasks
5. Observability plane
   - Structured logs
   - Health checks and heartbeats
   - Metrics and alerting
6. Reliability plane
   - Supervisor/restart manager
   - Snapshot/backup jobs
   - Cloud standby promotion path

## Deployment Topology

### Primary: Local Host

- Host: macOS (Apple Silicon)
- Runtime pattern:
  - Core agent process in one isolated runtime
  - Connector workers in separate isolated runtimes
  - Truth-layer and local cache isolated from workers
- Startup manager: `launchd` (or equivalent process supervisor)
- Secrets source: environment + secret manager (no plaintext in repo)

Implementation mapping in this repo:

- stack control script: `execution/scripts/agent_execution/agent_stack.sh`
- service dispatcher: `execution/scripts/agent_execution/run_service.sh`
- core launch agent: `execution/scripts/agent_execution/com_openclaw_agent_core.plist`
- workers launch agent: `execution/scripts/agent_execution/com_openclaw_agent_workers.plist`
- watchdog script + LaunchAgent: `execution/scripts/agent_execution/watchdog_health.sh`, `execution/scripts/agent_execution/com_openclaw_agent_watchdog.plist`
- env template: `execution/scripts/agent_execution/env_agent_example`

Runtime split:

- `com.openclaw.agent.core` runs the long-lived gateway runtime from `OPENCLAW_AGENT_CORE_CMD` (default template uses Docker Compose `openclaw-gateway`)
- `com.openclaw.agent.workers` is optional and only started when `OPENCLAW_AGENT_WORKERS_CMD` is set
- `com.openclaw.agent.watchdog` runs scheduled health checks (default every 300 seconds) and optional webhook alerts on failure

### Secondary: Cloud Standby

- Minimal mirror environment with same service contracts
- Warm standby for:
  - local host reboot
  - travel/disconnect
  - sustained local degradation
- Promotion trigger:
  - repeated local healthcheck failures
  - manual operator promotion

## Security Model

### Isolation Boundaries

- Boundary A: control plane separated from connector workers
- Boundary B: untrusted browser automation separated from secrets-heavy connectors
- Boundary C: persistence layer inaccessible directly from untrusted workers

### Security Controls

- Least-privilege service identities per worker
- Egress allowlist for connector network calls
- No shared write access between unrelated workers
- Read/write scopes separated for memory and artifacts
- Token rotation and short-lived credentials where possible
- Immutable audit logs for sensitive actions
- Separate launchd service labels for core and worker blast-radius isolation

## Data Flow

1. Trigger enters scheduler.
2. Scheduler writes job intent to queue.
3. Worker claims job and requests scoped credentials.
4. Worker executes connector action and emits events.
5. Result is normalized and persisted to truth layer.
6. Observability pipeline records metrics and logs.
7. If local path fails repeatedly, failover controller promotes cloud standby.

## Reliability and SLO Targets

- Service availability: 99.5% monthly target
- Interactive command p95: under 2 seconds for local operations
- Job success rate: 99% for retriable tasks
- Recovery objective:
  - RTO: 15 minutes (cloud promotion)
  - RPO: 5 minutes (state sync cadence)

Operational control points:

- preflight: `agent_stack.sh preflight`
- lifecycle: `agent_stack.sh start|stop|restart|status`
- health: `agent_stack.sh smoke`
- failover: `agent_stack.sh failover_to_cloud`
- failback: `agent_stack.sh failback_to_local`

## Capacity Guidance (Current Mac Profile)

- Suitable for medium always-on execution with connector mix and moderate parallelism
- Reserve headroom for local LLM workloads and browser automation spikes
- Scale pattern:
  - scale worker count before scaling model complexity
  - offload burst or heavy batch jobs to cloud workers

## Failure Domains and Mitigation

- Local host reboot/sleep
  - Mitigation: auto-restart + cloud standby promotion
- Connector API outages
  - Mitigation: circuit breakers + exponential backoff + queue retries
- Credential expiry
  - Mitigation: proactive refresh and pre-expiry validation
- Browser worker instability
  - Mitigation: isolate browser workers and restart independently

## Decision Rationale

- Local-first gives lowest latency and best local LLM proximity.
- Process isolation reduces blast radius without full VM overhead.
- Cloud standby provides resilience without sacrificing local performance.

## Out of Scope

- Exact vendor choices for queue, metrics, or secret manager
- Connector-specific schema definitions
- Full SOC2 control mapping
