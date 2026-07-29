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

**Advisory-mode disclosure:** while `authorize_caller()` runs in advisory mode
(current phase), a request may be **allowed even when the grant check itself
fails or is unreachable** — the check logs a warning and lets the request
through rather than hard-blocking. When this happens, the accepted-task
response text says so explicitly:

```
Task accepted (id=apis-a2a-…, neotoma=ent_…, routed_to=cicada). Note:
authorization check was unavailable; this request was allowed under
advisory policy.
```

A normal, fully-enforced acceptance never carries this note. If you are
building on this integration today, treat the trust boundary as provisional
until advisory mode is hardened into a hard block.

**Requesting a grant:** `a2a:task:create` grants are issued by the Ateles
operator. [TODO: operator contact process — no self-serve or documented
request flow exists yet; until this lands, `missing_capability` rejections
require an out-of-band conversation with the operator.]

---

## Example: `message/send`

**Accepted request:**

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "parts": [
        {"kind": "text", "text": "Fix the failing CI build on the docker step.\n\nThe pytest job has been red since the last merge to main."}
      ]
    }
  }
}
```

**Accepted response** (caller held an active grant with `a2a:task:create`):

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "result": {
    "parts": [
      {"kind": "text", "text": "Task accepted (id=apis-a2a-3f9c2b1a, neotoma=ent_7a2e1c, routed_to=cicada)."}
    ]
  }
}
```

**Rejected response** (caller's grant lacks the capability):

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "result": {
    "parts": [
      {"kind": "text", "text": "Rejected [missing_capability]: Your grant doesn't include a2a:task:create. — Ask the Ateles operator to add the a2a:task:create capability to your grant."}
    ]
  }
}
```

`a2a-sdk`'s `AgentExecutor.execute(context, event_queue) -> None` contract has
no structured-error return channel distinct from `cancel()`'s — both use
`event_queue.enqueue_event(new_agent_text_message(...))`. So today, both
acceptance and rejection are delivered as plain text over the same channel,
formatted as `code` + `message` + `hint` (see table below) rather than a
JSON-RPC `error` object. A future `a2a-sdk` upgrade may add a structured-error
path; this is tracked as a follow-up, not implemented here.

## Error-code reference

| `code` | `message` | `hint` |
|---|---|---|
| `missing_caller_identity` | No verified caller identity on this request. | Include a Bearer token issued by the Ateles operator. See docs/a2a.md#authorization. |
| `grant_not_active` | Your agent_grant exists but is not active. | Ask the Ateles operator to activate your grant, or check its expiry. |
| `missing_capability` | Your grant doesn't include a2a:task:create. | Ask the Ateles operator to add the a2a:task:create capability to your grant. |
| `neotoma_store_failed` | The task could not be recorded. | This is a transient or configuration issue on the Ateles side, not a problem with your request. Retry, or contact the operator if it persists. |

Rendered as `f"Rejected [{code}]: {message} — {hint}"` for authorization
failures, or `f"Task submission failed [{code}]: {message} — {hint}"` for
post-authorization submission failures.

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

**`APIS_A2A_ENABLE` defaults to `0` (off).** The gateway does not start unless
this is explicitly set to `1` — it ships default-off until arch/security signs
off on hardening `authorize_caller()`'s advisory-only enforcement into a hard
block (see [Authorization](#authorization) above). The shipped
`com.ateles.apis-a2a.plist` sets `APIS_A2A_ENABLE=0`; flip it to `1` once that
decision lands.

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
