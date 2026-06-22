# Credential health — proactive re-auth for the swarm

*Status: spec (build pending). Design for a credential-health layer that detects expiring/expired session credentials before they break a workflow and pages the operator with an exact re-auth action.*

## Purpose

The swarm depends on a mix of credentials. Most are long-lived tokens (revoked/rotated, not "expired"). A few are **session-bearing** and lapse on their own cadence — and when one lapses today, the operator finds out only when something silently breaks. The canonical example: the Apis daemon spawns the Vanellus review panel as `claude --print`; when its Anthropic OAuth session expires, every PR aggregation posts `401 Invalid authentication credentials` instead of a verdict ([surfaced legibly as of the panel auth-failure detection in `swarm_dispatch.py`], but the underlying lapse still has to be fixed by hand).

This layer makes credential lapse a **proactively surfaced, actionable event**: detect early, page the operator, hand them the exact re-auth URL or command. It never auto-fixes — completing an OAuth flow is inherently a human step — its job is early detection and a one-click handoff.

## Scope

In scope: session-bearing credentials whose failure mode is *expiry* —
- **Anthropic / Claude OAuth** (the Apis daemon's `claude --print` session) — the recurring one.
- **Substack** session token (drafting; flagged operator-private).
- **Chrome-MCP / X** authenticated browser session.
- **Google / `gws`** refresh-token lapse (calendar/Gmail).

Out of scope: long-lived tokens that don't expire on a session cadence (Telegram bot tokens, GitHub PATs, `NEOTOMA_BEARER_TOKEN`, npm, ElevenLabs, webhook secrets). Those are a rotation concern, not an expiry one, and are covered by AAuth keypair rotation + secret management. A credential may be *added* to the registry later if it turns out to expire.

## Model

### `credential_health` entity (one per monitored credential)

| Field | Meaning |
|---|---|
| `service` | Stable key: `anthropic_claude`, `substack`, `x_session`, `google_gws`. Canonical identity. |
| `kind` | `oauth_session` \| `api_key` \| `refresh_token` \| `browser_session`. |
| `used_by` | Which daemon/agent depends on it (roster role or daemon name), so a failure names the blast radius. |
| `check_method` | How liveness is probed — see *Probe* below. Declarative, not code-inlined. |
| `status` | `healthy` \| `degraded` \| `expired` \| `unknown`. |
| `last_verified_at` | Timestamp of the last successful probe. |
| `last_checked_at` | Timestamp of the last probe attempt (success or fail). |
| `expires_at` | When known (e.g. JWT `exp`), enables *near-expiry* warning before hard failure. |
| `reauth_action` | The exact remediation handed to the operator: a re-auth URL, or a command (e.g. the `ANTHROPIC_API_KEY` plist steps). |
| `visibility` | `private` for operator-bound credentials. |

Stored in Neotoma so status is queryable and the page is reproducible; never stores the secret value itself.

### Probe (cheap liveness, off the hot path)

One inexpensive check per credential, on a schedule, **before** the credential is needed in a real workflow:
- **Anthropic**: a 1-token `claude --print "ok"` in the *daemon's* environment (must probe the same env the panel uses, not the interactive session) → non-401 = healthy.
- **Substack / X**: a whoami / session-validity call.
- **Google**: a token-info / refresh check via `gws`.

Probes are debounced and batched; a transient network failure is retried before declaring `degraded`. The probe records `status` + `last_verified_at` on the entity.

### Proactive page

On a probe that returns `expired`/`degraded`, or when `expires_at` is within a warning window, send the operator a notification (BLOCKER for expired, OPERATOR_DECISION for near-expiry) that includes:
- the `service` and its `used_by` blast radius ("PR review aggregation is down"),
- the `reauth_action` verbatim — the re-auth URL to click or the command to run.

This is the proactive complement to the reactive `detect_auth_failure` path already in `swarm_dispatch.py` (which catches a lapse at use-time and pages with remediation). The monitor catches it *before* use.

## Where it runs

A lightweight probe folded into an existing scheduled daemon (the morning-brief run, or Anthus) rather than a new always-on daemon — credential checks are periodic, not event-driven. One probe pass per scheduled tick; results to `credential_health` entities; pages only on state change to `degraded`/`expired` (no daily nag while healthy).

## Non-goals / principles

- **Never auto-re-auth.** Surface + hand off; the human completes the flow.
- **Never store secret values** in `credential_health` — only status + the action to fix.
- **Probe the real environment.** An Anthropic probe must run in the daemon's env (where the panel runs), or it will report healthy while the daemon is 401ing — the exact trap that hid the Vanellus lapse.
- **Registry-driven, not hardcoded.** Adding a credential = storing a `credential_health` entity with a `check_method` + `reauth_action`, not editing probe code per service.

## Build plan (deferred)

1. Register `credential_health` schema (canonical_name_fields: `service`).
2. Seed the four in-scope credentials with `check_method` + `reauth_action`.
3. Implement the probe pass (one function per `kind`) + wire into a scheduled daemon.
4. Page on state-change to degraded/expired with the `reauth_action`.
5. Backfill: the Anthropic entry's `reauth_action` is the host-side `ANTHROPIC_API_KEY` fix already documented for the Apis daemon.
