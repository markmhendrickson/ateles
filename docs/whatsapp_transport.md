# WhatsApp as a Transport for Ateles

**Status:** design · 2026-07-03
**Plan:** objective B2 of *Life + business strategy elicitation and swarm mapping* (`ent_06d3193fbafe44106349bcfb`) — "accept input via ANY transport → swarm converts to tasks/plans"
**Motivating use case:** the intake-relationship pilot (Manel / Elefun Yoga) needs a *le mandas / te devuelve* loop on the channel the prospect already lives in.

---

## TL;DR

Ateles does **not** need to build a WhatsApp gateway. The resident-agent host — **OpenClaw** — already ships a **production-ready WhatsApp channel** (WhatsApp Web via Baileys, QR-linked, `extensions/whatsapp/`, `docs/channels/whatsapp.md`). The gateway owns the linked session, enforces a pairing/allowlist DM policy, and normalizes inbound (text + media) into the same internal path Telegram already uses. The Ateles-side work is therefore **integration and routing**, not protocol implementation:

1. **Reactivate** the logged-out WhatsApp session on the existing gateway (`openclaw channels login --channel whatsapp`).
2. **Generalize the outbound shim** (`execution/lib/telegram/send.mjs`) into a channel-agnostic `send(channel, target, message)` so daemons can push to WhatsApp the same way they push to Telegram.
3. **Model a `channel_config` Neotoma entity** so an agent resolves its channel + recipient binding at runtime (per the "config from Neotoma/env, never hardcoded" rule) — this is what lets Manel's agent be on WhatsApp while the operator digest stays on Telegram, with no code change.
4. **Route WhatsApp inbound** through the existing conversational router (Onychomys) / task dispatcher (Apis), exactly where Telegram inbound lands.

This is markedly cheaper and better than the Meta Cloud API path first considered: no Business verification, no public webhook infra, no message-template approval, no 24-hour-session-window constraint, no per-message BSP cost.

---

## Why not the Meta Cloud API (the path first considered)

An earlier sketch assumed WhatsApp = Meta WhatsApp Business Cloud API: webhook-only inbound, a public HTTPS endpoint, signature verification, media-ID fetches, Business verification, and pre-approved message *templates* to send outside a 24-hour session window. That is a real amount of infrastructure and a genuine set of product constraints.

OpenClaw sidesteps all of it by using **WhatsApp Web (Baileys)** — the same protocol WhatsApp's own web client uses. Consequences:

| Concern | Meta Cloud API | OpenClaw / Baileys (chosen) |
|---|---|---|
| Inbound | webhook-only; needs public URL + signature verify | gateway owns the linked session; inbound arrives in-process |
| Auth | Business verification + phone-number ID + token | QR link once (`channels login`); session persisted in an auth dir |
| Outbound outside 24h | pre-approved templates only | free-form (it's a linked client) |
| Media | fetch by media-ID via Graph API | handled by the channel plugin (`channel-inbound-roots`) |
| Cost | per-message (via BSP or direct) | none beyond the number |
| Ops caveat | managed service | unofficial client — a linked session can be logged out; **run a dedicated number**, expect occasional re-link |

The tradeoff is that Baileys is an **unofficial** client: WhatsApp can log the session out, and running it on the operator's personal number risks that number. OpenClaw's own docs recommend a **dedicated number** — which is also exactly right for a per-tenant agent (Manel's agent = its own number = its own linked session).

---

## Current state (what already exists)

**OpenClaw (the T1 host / gateway):**
- Full channel abstraction: `src/channels/`, `src/channels/transport/`, `channel-config.ts`, a bundled channel catalog, `media/channel-inbound-roots.ts`, `infra/transport-ready.ts`.
- A WhatsApp extension: `extensions/whatsapp/` (Baileys-based), `src/config/types.whatsapp.ts`, `zod-schema.providers-whatsapp.ts`, `docs/channels/whatsapp.md`, `docs/plugins/reference/whatsapp.md`.
- DM policy is **pairing** by default for unknown senders; **allowlist** for groups. Pairing requests expire in 1h, capped at 3/channel.
- The Ateles resident agent already runs through this gateway with the Neotoma soul-override active (task `ent_f613187a6d645d796cd5c1e0`, done 2026-06-10). That task's closing note records the one open item: **"WhatsApp session logged out (401) — needs `openclaw channels login`."** So the channel is wired; the session is just stale.

**Ateles (the swarm side):**
- **Outbound** is a thin shared shim: `execution/lib/telegram/send.mjs` — one HTTPS POST to the Telegram Bot API, token + chat-id from env (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`). Daemons push outbound directly (per the daemon-Telegram-routing rule); conversational inbound routes via Onychomys.
- **Inbound routing** is centralized in Apis: `execution/daemons/apis/routing.py` maps task-domain → T4 skill; gateway adapters (`github_gateway.py`, `a2a_gateway.py`) already establish the pattern of "external protocol → internal event/task."
- There is **no channel abstraction on the Ateles side yet** — `send.mjs` is Telegram-specific and single-target. That is the seam to generalize.

---

## Target architecture

Keep the swarm core (Apis routing, the intake-relationship skill, Neotoma) **channel-agnostic**. Introduce one thin seam on each side of the message.

### 1. `channel_config` entity (the abstraction seam)

One entity per channel binding, resolved at runtime:

```
channel_config {
  channel_type: "whatsapp" | "telegram"
  account: string            # OpenClaw account label (e.g. "manel", "operator")
  credentials_ref: string    # env var / SOPS key / OpenClaw auth-dir — NEVER an inline secret
  recipient_binding: string  # phone number (whatsapp) or chat/topic id (telegram)
  contact_entity_id?: string # the person this channel talks to (Manel)
  direction: "inbound" | "outbound" | "both"
  policy?: { dm: "pairing"|"allowlist", allow_from: string[] }
}
```

Agents read their channel from this instead of `TELEGRAM_*` directly. This is what makes the transport swappable per agent/tenant. Register the schema before first store (schema-check rule).

### 2. Outbound: generalize the send shim

`execution/lib/telegram/send.mjs` → `execution/lib/messaging/send.mjs` exposing `send(channel, target, message, opts)`. Branch by `channel`:
- `telegram` → existing Bot API POST (unchanged behavior; keep back-compat shim).
- `whatsapp` → deliver via the **OpenClaw gateway's local API** (the gateway already holds the linked session — Ateles should hand it the message rather than opening its own Baileys session, so there is exactly one owner of the WhatsApp session). Target = phone number from `channel_config.recipient_binding`.

Call site for daemons is unchanged in spirit: they still "push outbound directly," now with a channel argument resolved from `channel_config`.

### 3. Inbound: reuse the OpenClaw gateway → Onychomys/Apis

The gateway already receives WhatsApp messages (text + media) and routes them to the resident agent. For the swarm, WhatsApp inbound should follow the **same rule as Telegram**: conversational messages route through **Onychomys**; task-bearing messages become Neotoma `task` entities dispatched by **Apis**. No new inbound daemon is required for the pilot — the gateway is the receive path. (A dedicated normalizer daemon only becomes worthwhile if we later leave OpenClaw or need channels OpenClaw doesn't support.)

Media handling is already solved by the channel plugin (`channel-inbound-roots.ts`): voice notes / photos land as files the agent can read — which is precisely the intake-relationship input (voice note / assist video / class photo → manual entry).

### 4. Per-tenant isolation (the Manel case)

A per-tenant agent = its own OpenClaw account + its own linked WhatsApp number + its own Neotoma instance. `channel_config` binds the three. This is the same instance-per-tenant pattern the Elsa/Manuel plans already track; WhatsApp just becomes the channel field.

---

## Constraints & risks

- **Unofficial client.** Baileys sessions get logged out; plan for re-link (`channels login`) and surface a health signal when a session drops (the 401 that's currently open is exactly this). A cloudflared/watchdog-style monitor on session health is worth filing.
- **Dedicated number per agent.** Do not run a tenant agent on the operator's personal WhatsApp — both for the number-ban risk and for clean per-tenant isolation.
- **One session owner.** Ateles daemons must deliver *through* the OpenClaw gateway, not open a second Baileys session against the same number (that would fight over the link).
- **Pairing friction.** First contact from an unknown number requires a pairing approval (or an allowlist entry). For a pilot with a known contact (Manel), pre-seed `allowFrom` with his number so the loop is frictionless.
- **Media size / formats.** Confirm voice-note (opus) and video handling limits through the gateway before promising the full loop; transcription still runs Ateles-side.

---

## Pilot vs. product

- **Pilot (this week, operator-in-the-loop):** reactivate WhatsApp on the existing gateway; pre-allowlist Manel's number; the intake-relationship skill runs on the operator's machine when processing a batch; replies go out over WhatsApp via the generalized shim. Same experience for Manel (WhatsApp in, manual-link out) with near-zero new infra.
- **Product (post-pilot-gate):** provision a dedicated number + OpenClaw account + Neotoma instance per tenant; `channel_config` per agent; session-health watchdog; then the loop runs without the operator in the middle.

This sequencing matches the plan's existing pilot-gate discipline (prove on 2–3 contacts before automating).

---

## Task breakdown

Filed PART_OF `ent_06d3193fbafe44106349bcfb` (objective B2):

1. **Reactivate the WhatsApp session on the OpenClaw gateway** — `openclaw channels login --channel whatsapp`, pre-seed `allowFrom` with the pilot contact's number; verify inbound text + media reach the resident agent. (unblocks the pilot; small)
2. **Model + register the `channel_config` schema and seed entities** — one for the operator Telegram binding, one for the pilot WhatsApp binding. (the abstraction seam)
3. **Generalize the outbound send shim** to `send(channel, target, message)` with a `whatsapp` branch that delivers through the gateway; keep the Telegram path back-compatible. (afternoon)
4. **Wire WhatsApp inbound → Onychomys/Apis routing** so conversational vs task-bearing messages route like Telegram does. (integration)
5. **Session-health watchdog** — detect a logged-out WhatsApp session (the 401 signature) and surface a re-link alert; mirrors the cloudflared-watchdog need. (reliability)

Dependencies: 3 and 4 depend on 2 (the config seam). 1 is independent and unblocks the pilot immediately.
