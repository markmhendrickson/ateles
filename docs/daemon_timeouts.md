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
  → dispatch outcome pending provider selection; no role output established
  → likely cause: Neotoma POST /entities/query is slow
  → fix: check Neotoma availability or raise ATELES_NEOTOMA_TIMEOUT (current: 45s) after checking latency
  → any later restricted output is unverified until the role reloads successfully
```

Check Neotoma latency and availability first. Adjust the timeout only when
the service can answer inside a larger bounded interval. The loader alert
reports the missing definition before a provider is selected; it does not
claim a child ran. A local degraded Claude child can run with restricted
tools and no MCP server; its completion notice says whether output exists.
Codex, Cursor, and Fly degraded children are refused before launch, with no
role output. Review notices keep the gate pending and require a new PR push
(synchronize event) or reopen after the role definition reloads successfully;
this failure does not schedule an automatic retry.
