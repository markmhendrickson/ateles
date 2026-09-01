# Configuration as entities

Status: proposed (proof implemented for the SSE subscription id).

## The defect

Every configuration failure the swarm has suffered was an env var, and every one
shared a single property: **wrong configuration was indistinguishable from right
configuration.**

| Incident | Variable | Cost |
|---|---|---|
| Apis consumed zero task events | `NEOTOMA_SSE_SUBSCRIPTION_ID_APIS` missing from plist | 88 days, 67,450 skipped events, ~100 stranded tasks |
| Stale-issue sweep never ran | `NEOTOMA_BEARER_TOKEN` absent as repo secret | 12+ weeks; the only drain on a 396-issue backlog |
| Operator instance never deployed | `OPERATOR_INSTANCE_HOST` had no consumer at all | Instance served 0.22.1 since 2026-08-27 |
| Deploy hit the wrong instance | `CLIENT_INSTANCE_APP` silently resolved to a client's app | Green workflow, wrong target, no signal |
| Three env files disagree | `NEOTOMA_BEARER_TOKEN` | One works, two are rejected |

An env var has no provenance, no history, and nothing to query. You cannot ask
"which subscription is Apis consuming, and who set it, and when." The absence of
that question is what let 88 days pass.

Note the repo/installed split that caused the Apis outage: the repo plist
`execution/daemons/apis/com.ateles.apis.plist` declares no subscription id while
the *installed* plist does. Two files, no reconciliation, no way to detect drift.
`neotoma-agent` has a live variant of the same bug — its launchd key is
`NEOTOMA_SSE_SUBSCRIPTION_ID_NEOTOMA-AGENT`, which contains a hyphen and is not a
valid POSIX env-var name, so it likely never resolves.

## Inventory

~293 distinct variables across 18 daemons, launchd plists, GitHub Actions, and
three `.env` files.

| Class | Count | Disposition |
|---|---|---|
| **CONFIG** — non-secret facts: app names, hosts, entity/subscription IDs, paths, thresholds, flags | ~250 | **Move to entities** |
| **SECRET** — credential values: tokens, PATs, keys, mnemonic | 34 | **Stay in SOPS/1Password.** Never in Neotoma. |
| **BOTH** — config that names or points at a secret | 9 | **Config entity names the secret; value stays in SOPS** |

The BOTH class is the interesting one: `ATELES_PRIVATE_KEYS_DIR`,
`SOPS_AGE_KEY_FILE`, `ATELES_AGENT_SUB`, `SEARCH_CONSOLE_*_PATH` and friends are
*pointers*, not credentials. Storing the pointer as config and the target as a
secret is what makes a missing credential detectable.

## Mechanism

A `daemon_configuration` entity per daemon, holding a `config` map and a
`daemon_name`. This extends the pattern already established by
`deployment_configuration` — which CLAUDE.md already says to retrieve rather than
deploy from memory. The pattern is proven; it simply was never applied to daemon
config or to the operator's own instance.

### Schema (Phase 1 — map-based, one entity per daemon)

This PR deliberately uses a **map-based** entity (one row per daemon, `config`
object holding all keys) rather than the per-key entity shape sketched in the
issue's Engineering section (`daemon_label` + `config_key` + `value` per row).
That finer granularity remains valid for Phase 2 migration (#680); the resolver
in this PR reads the map shape. Schema definition:
[`docs/schemas/daemon_configuration.json`](schemas/daemon_configuration.json).

| Field | Required | Purpose |
|---|---|---|
| `daemon_name` | yes | Short daemon id (`apis`, `neotoma-agent`) — lookup key for `config_resolver` |
| `config` | yes | Map of logical keys → CONFIG values (never secrets) |
| `set_by` | no | Author identity for provenance |
| `notes` | no | Drift/history notes |

Register on Neotoma (once per instance):

```bash
neotoma schemas register daemon_configuration \
  --fields '{"schema_version":{"type":"string","required":true},"daemon_name":{"type":"string","required":true},"config":{"type":"object","required":true},"set_by":{"type":"string","required":false},"notes":{"type":"string","required":false}}' \
  --schema-version 1.0.0 --activate
```

Create or update a daemon's config via `store` (example — Apis SSE subscription):

```python
store(entities=[{
  "entity_type": "daemon_configuration",
  "daemon_name": "apis",
  "schema_version": "1.0.0",
  "set_by": "operator",
  "config": {
    "sse_subscription_id": "<uuid-from-subscribe-tool>"
  },
  "notes": "Migrated from installed plist 2026-09-01"
}], idempotency_key="daemon_configuration:apis:v1")
```

Implementation: [`lib/daemon_runtime/config_resolver.py`](../lib/daemon_runtime/config_resolver.py)
(fetches via `POST /entities/query`, exact `daemon_name` match). Loud failure:
[`MissingSubscriptionError`](../lib/daemon_runtime/sse_client.py) in
[`sse_client.py`](../lib/daemon_runtime/sse_client.py). Parity check:
[`execution/scripts/check_daemon_config_parity.py`](../execution/scripts/check_daemon_config_parity.py).

Resolution order in `lib/daemon_runtime/config_resolver.py`:

```
1. environment variable   — operator override; the escape hatch
2. Neotoma entity         — authoritative, versioned, queryable, with provenance
3. local cache            — last-known-good, written on every successful fetch
4. declared default       — only when the spec permits one
5. LOUD FAILURE           — ConfigResolutionError naming every source tried
```

**Env wins deliberately.** Adopting the resolver cannot break a daemon that works
today, and it leaves a working path when Neotoma is unreachable and no cache
exists.

### Degradation when Neotoma is down

This is the design's load-bearing decision. Neotoma has served 19-60s reads,
intermittent 502s, and was fully unreachable during this very session.

**A daemon that cannot start because its config store is degraded is a strictly
worse failure than a daemon running slightly stale config.** A stale subscription
id still delivers events; a daemon that refused to boot delivers nothing. So:

- the fetch is **time-boxed** (`ATELES_CONFIG_FETCH_TIMEOUT_S`, default 5s) — a
  slow Neotoma costs seconds at startup, never a hang;
- every success writes a **last-known-good cache**, atomically;
- on timeout or error the daemon **starts on cache** and logs at WARNING with the
  cache's age, so running-on-stale is visible rather than assumed;
- we fail hard **only** when a value resolves from nowhere — which is precisely
  the case that used to be silent.

Startup emits one provenance line naming where every value came from
(`sse_subscription_id<-neotoma`, `[DEGRADED: … cache_age=31.4h]`), so "which
config is this daemon actually running" is answerable from the log alone.

### Making absence loud

`MissingSubscriptionError` replaces the warn-and-return. It names the daemon,
every source tried, and the remedy. Critically, `stream()` **re-raises it
explicitly** past its own broad `except Exception` retry loop — without that, the
guard would be swallowed into a warning and an infinite quiet reconnect,
reproducing the exact defect it exists to prevent. The test drives the real
`stream()` entrypoint rather than the private method, so the swallow cannot
regress unnoticed.

## Should SOPS ciphertext move to Neotoma?

**Recommendation: yes for distribution, with the local materialized cache kept as
the fallback — but it is a second phase, after config.**

The case for is stronger than it first sounds. Ciphertext is safe anywhere; that
is what encryption is for, so the private-repo boundary buys little beyond
defense in depth. And the current arrangement has a concrete cost:
`lanius-stale-issues.yml` has failed every scheduled run for 12+ weeks partly
because a CI runner has neither an `ateles-private` checkout nor the age key.
Neotoma-distributed ciphertext is reachable by any authenticated caller, with
versioning and provenance for free.

The case against is the bootstrap dependency, and it must be answered rather than
waved past:

- **Neotoma's own bearer token cannot be fetched from Neotoma.** This is
  irreducible. A small bootstrap set (`NEOTOMA_BASE_URL`, `NEOTOMA_BEARER_TOKEN`,
  and the age key) must always come from the local environment. Any design
  claiming otherwise is circular.
- **The age private key never goes to Neotoma.** Non-negotiable. It stays in
  1Password and `~/.config/sops/age/keys.txt`.
- **Offline decrypt must survive.** `docs/secrets_management.md` records that the
  snapshot exists precisely so daemons decrypt offline, fixing an
  `op read`-needs-live-session bug. Fetching ciphertext from Neotoma on every
  start would reintroduce that bug in a new costume.

So the shape is Neotoma as the **distribution point**, never the runtime
dependency: fetch ciphertext on start, write it to the existing materialized
cache, and on any failure decrypt the last-known-good snapshot already on disk.
That removes the repo-checkout requirement (fixing the CI case) while preserving
offline decrypt. It is strictly additive to `secrets_materialize.py` — a second
source for the same ciphertext, not a new mechanism.

### Why an agent would actually use it

Worth stating plainly, because it is the reason this came up: the SOPS path was
bypassed all session in favour of sourcing `~/.config/neotoma/.env` directly.
Four snapshots and a working `secrets_materialize.py` existed and went unused.

That is the same pattern as every incident above — a working mechanism, unused,
because nothing prompted its use. ateles#593 established that only code, a hook,
MCP point-of-use, or an agent prompt actually binds behaviour; documentation does
not. So the config resolver is **code on the startup path**, not a convention. If
phase 2 proceeds, secret distribution should bind the same way — materialize
invoked from the daemon entrypoint, not from a runbook step someone remembers.

## Phasing

1. **Config for daemons** (proof implemented) — SSE subscription ids first, since
   absence cost 88 days and correctness is checkable at startup.
2. **Deploy targets** — extend `deployment_configuration` to the operator's own
   instance, closing the `OPERATOR_INSTANCE_HOST`/`CLIENT_INSTANCE_APP` gap.
3. **Secret distribution via Neotoma ciphertext**, cache-backed, age key local.
4. **A drift check** — reconcile repo plists against entities, so the
   installed/repo split that caused the Apis outage becomes a detectable
   condition rather than a discovery.
