# A2A Inbound Gateway (Apis)

## Purpose

Exposes the Ateles swarm as an [A2A](https://a2a-protocol.org/) (Agent2Agent)
server so external, A2A-compatible agents — from other stacks, vendors, or
future Claude agents with native A2A support — can **discover** the swarm's
capabilities and **delegate tasks** into it through an open standard, without
needing Neotoma credentials or a bespoke integration.

A2A (donated to the Linux Foundation, v1.0 shipped early 2026, 150+ adopting
organizations) defines three things this gateway implements: an **Agent Card**
advertising capabilities, **Tasks** as the unit of exchanged work, and an HTTP +
JSON-RPC + SSE transport for them.

## Scope

Covers the inbound, server-side surface only: Apis as an A2A task receiver. Out
of scope (tracked as follow-ups): a Menura read-only discovery endpoint, an
outbound A2A client so Apis can delegate to *external* agents, OAuth2/JWKS
inbound auth, and streaming Neotoma task-status transitions back over A2A.

---

## Design: A2A is another mouth on the same queue

The gateway does **not** reimplement dispatch. It creates a Neotoma `task`
entity; the existing Apis SSE path (`apis.py`: `handle_event → dispatch_task`)
picks it up unchanged and routes it to the right T4 worker. This keeps a single
dispatch pipeline and preserves the "every agent action is an attributed
observation" invariant — each inbound task is a Neotoma observation attributed
to `apis@ateles-swarm`.

```
External A2A client
   │  message/send (JSON-RPC 2.0 over HTTP, optional SSE)
   ▼
a2a_gateway.serve()  ──authorize_caller()──▶ grant_checker (a2a:task:create)
   │  ApisTaskBridge.submit(text, caller)
   ▼
a2a_executor.create_neotoma_task()  ──Bearer + X-AAuth-Token──▶ POST /api/store
   │  new task entity (attributed apis@ateles-swarm, source="a2a")
   ▼
Neotoma ──task.created SSE──▶ Apis handle_event → dispatch_task (UNCHANGED)
                                       │
                                       ▼  claude --print --append-system-prompt <skill>
                                  T4 worker (cicada / monedula / gorilla / …)
```

---

## Components

| File | Role | SDK dependency |
|---|---|---|
| `execution/daemons/apis/routing.py` | Domain → T4-skill routing table + tag inference, shared with the SSE path | none |
| `execution/daemons/apis/a2a_executor.py` | `ApisTaskBridge`: message → Neotoma task; A2A-id ↔ entity-id map | none |
| `execution/daemons/apis/a2a_gateway.py` | Agent Card build + JWS signing + caller authorization + SDK transport (`serve()`) | only inside `serve()` |
| `execution/daemons/apis/com.ateles.apis-a2a.plist` | launchd unit (long-running server) | — |
| `execution/daemons/apis/requirements.txt` | `a2a-sdk`, `uvicorn`, `cryptography`, `PyJWT` | — |

**Layering principle:** everything except `serve()` is pure-stdlib (plus
`cryptography`, already a `lib/daemon_runtime` dependency) and is unit-testable
without `a2a-sdk` installed. The SDK is imported lazily inside `serve()`, so SDK
version churn never breaks the testable core. If the SDK is absent, `serve()`
raises an actionable install error rather than failing at import.

---

## Agent Card

Served at `/.well-known/agent.json`. Built by `build_agent_card()` and signed by
`sign_agent_card()`.

- Advertises a single coarse skill, **`delegate-task`**, whose `tags` list the
  supported domains (derived from `routing.DOMAIN_ROUTES`:
  finance, health, ops, engineering, agents, neotoma, product, comms). Internal
  domain→worker routing is intentionally *not* exposed, so the external
  contract is stable as routing evolves.
- **Signed** with Apis's existing AAuth P-256 keypair as a JWS (`ES256`) over
  the canonicalized card (sorted-keys, compact JSON), under a `signatures`
  array — the A2A v1.0 signed-card feature, letting receivers verify Ateles
  domain ownership. If the keypair is not yet minted, an unsigned (still valid)
  card is served and a warning logged.

---

## Authorization

Two independent layers, both reusing `lib/daemon_runtime`:

1. **Caller → gateway** (`authorize_caller()`): the Agent Card declares a
   `bearer` security scheme. A verified caller identity is looked up via
   `GrantChecker`; the caller must hold an **active `agent_grant`** carrying the
   `a2a:task:create` capability. Enforcement is **advisory** in the current
   phase (an unreachable checker logs and allows), mirroring `grant_checker`'s
   own staging; it hard-blocks once the PS-layer AAuth integration lands.
2. **Gateway → Neotoma** (`_aauth_headers()`): the task-creation `POST
   /api/store` carries `Authorization: Bearer <NEOTOMA_BEARER_TOKEN>` plus an
   `X-AAuth-Token` signed by Apis's keypair, so the created task is attributed
   to `apis@ateles-swarm`.

Inbound tasks default to `visibility: private` — an external caller cannot make
swarm work public without an explicit scope grant.

---

## Configuration

The gateway reads these environment variables (add them to `.env.example` and
`~/.config/neotoma/.env`; the launchd plist sets the server-side ones):

| Variable | Default | Purpose |
|---|---|---|
| `APIS_A2A_ENABLE` | `0` | `1` to allow `serve()` to start |
| `APIS_A2A_HOST` | `127.0.0.1` | bind host |
| `APIS_A2A_PORT` | `8788` | bind port |
| `APIS_A2A_PUBLIC_URL` | `http://<host>:<port>/` | URL advertised in the Agent Card `url` |
| `APIS_A2A_REQUIRE_AUTH` | `1` | require a verified caller + grant |
| `APIS_A2A_TASK_VISIBILITY` | `private` | visibility of tasks created from A2A |
| `NEOTOMA_BASE_URL` | `https://neotoma.markmhendrickson.com` | Neotoma API base |
| `NEOTOMA_BEARER_TOKEN` | — | Neotoma write token |

> **Operator note:** `.env.example` is permission-protected from automated edits;
> add the `APIS_A2A_*` rows above to it by hand.

---

## Running

```bash
pip install -r execution/daemons/apis/requirements.txt
APIS_A2A_ENABLE=1 APIS_A2A_PORT=8788 \
  .venv/bin/python execution/daemons/apis/a2a_gateway.py
# discover:
curl http://127.0.0.1:8788/.well-known/agent.json | jq .
```

Install as a launchd service:

```bash
cp execution/daemons/apis/com.ateles.apis-a2a.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.ateles.apis-a2a.plist
```

---

## How A2A maps onto the swarm

| A2A concept | Ateles mapping |
|---|---|
| Agent Card | `build_agent_card()` — capability manifest of the swarm's task intake |
| Agent Card signature (JWS) | Apis AAuth P-256 keypair |
| Skill | `delegate-task` (one coarse skill; domains as tags) |
| Task | Neotoma `task` entity (`source="a2a"`) |
| `message/send` | `ApisTaskBridge.submit()` → `POST /api/store` |
| Caller auth | `agent_grant` + `a2a:task:create` capability |
| Task routing | existing Apis SSE `dispatch_task` → T4 worker |

A2A is the **inbound transport**; Neotoma stays the **canonical store**; AAuth +
`agent_grant` stay the **authorization layer**.
