# Daemon Neotoma timeouts

| Environment variable | Default | Guards | Symptom when too low |
| --- | ---: | --- | --- |
| `ATELES_NEOTOMA_TIMEOUT` (seconds) | 45 | Neotoma REST reads and writes in `lib/daemon_runtime`, including agent definitions, grants, policy reads, and signed requests | Definition loads become degraded; the dispatcher records a blocked or incomplete review instead of trusting that role's output. |

The setting accepts a finite positive number of seconds. An invalid explicit
value stops daemon startup and names the variable and value. An unset value
uses 45 seconds. SSE subscriptions deliberately use their streaming timeout.

When Neotoma is slow, the operator sees an escalation similar to:

```text
[ESCALATION] agent_loader: role 'reviewer' loaded DEGRADED — Neotoma definition fetch timed out after 45s
  → dispatched with a minimal read-only allowlist; no write or command tools
  → likely cause: Neotoma POST /entities/query is slow
  → fix: check Neotoma availability or raise ATELES_NEOTOMA_TIMEOUT (current: 45s) after checking latency
  → this role's output is unverified until it reloads successfully
```

Check Neotoma latency and availability first. Adjust the timeout only when
the service can answer inside a larger bounded interval. A degraded Claude
child runs in restricted mode with an explicit set of read tools and no MCP
server; adapters that cannot bind that floor refuse the degraded child.
