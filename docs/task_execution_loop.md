# Task Execution Loop — Design

**Status:** design (2026-06-23) · **Plan:** Task-spine loop + cloud-hosted swarm (`ent_aff87747b49e338790568af6`) · **Scope:** ateles (`daemon_runtime`, `execution/daemons`) + Neotoma (`task`, `conversation`, `task_readiness_assessment`).

**Goal:** turn a Neotoma `task` into a fully self-driving unit of work: it is **picked up automatically** by the agent that can execute it, **assessed for readiness before execution**, **runs with email as the operator's two-way transport** (progress out, steering in via replies), is **tracked as one conversation per execution run**, and closes by **verifying, persisting, and recording impact**. Roll it out **one task at a time** until each stage is trusted.

This is **mostly wiring + two new primitives**, not a new system. The task-spine plan already shipped the lifecycle state machine, watchdog, routing, the confidence×blast execution gate, session finalize, and the outbound email pipeline. The genuinely new pieces are: (1) **email-inbound ingestion** (replacing the Telegram reply path), (2) a **pre-execution readiness gate** with a `task_readiness_assessment` artifact, and (3) **conversation-per-run** binding.

---

## What already exists (build on, don't replace)

- **Autonomous pickup** — Apis subscribes to `task.created` (SSE) and routes via `execution/daemons/apis/routing.py`: explicit `task.assigned_to` wins, else domain-keyword inference (`DOMAIN_ROUTES` → T4 skill).
- **Lifecycle state machine** — `lib/daemon_runtime/task_lifecycle.py`: `pending → routed → executing → verified → done`, with `failed`/`blocked`/`awaiting_approval`/`declined`/`superseded`, retry with capped exponential backoff, fail-open status writes.
- **Stall watchdog** — `execution/daemons/apis/task_watchdog.py`: out-of-band sweep that re-dispatches stalled `routed`/`executing` tasks and escalates exhausted `failed` tasks.
- **Execution gate (confidence × blast radius)** — `lib/daemon_runtime/gating.py` + `execution_policy`: high-confidence + low-blast → AUTO_EXECUTE, else file a blocking `checkpoint_brief` and await the operator. Per-agent thresholds (Monedula always checkpoints). Fails CLOSED.
- **Session finalize** — `lib/daemon_runtime/session_finalize.py`: builds `conversation` + `agent_message` rows, links `PART_OF` plan and/or `task_id`, optional `learning`, idempotency-keyed.
- **Outbound operator/beneficiary email** — `dispatch_report → publish_rendered_page → gws gmail`, gated by operator approval (the `owner_beneficiary_for_whom_tracking` decision).
- **Reply-routing pattern** — `execution/daemons/cyphorhinus/cyphorhinus.py`: long-polls a transport, maps each operator reply back to its originating entity, writes a follow-up entity linked to it, and lets the target agent pick it up. **Transport is decoupled from execution** (`decision:reply-routing-sse-neotoma-2026-05-27`). The email-inbound daemon is this exact pattern with Gmail as the transport instead of Telegram.

The gaps: **no inbound email** (replies come via Telegram today), **no pre-execution readiness check** (the only gate is the execution gate, which asks "is my action safe?", not "is this task well-specified enough to start?"), and **no per-run conversation** (finalize binds a conversation at the *end*; nothing owns the thread *during* the run).

---

## The end-to-end loop

```
  DETECT ──▶ MATERIALIZE task ──▶ READINESS GATE ──▶ KICKOFF email ──▶ EXECUTE
                                       │ (not ready)        ▲ (operator reply)
                                       ▼                    │
                                 awaiting_input ──email──▶ operator
                                                                │
  EXECUTE ──▶ EXECUTION GATE ──▶ act ──▶ VERIFY ──▶ FINALIZE ──▶ IMPACT
               │ (checkpoint)                          │
               ▼                                        ▼
         checkpoint_brief ──email──▶ operator     conversation + learning
```

Each task execution is one **run**. A retry or reopen starts a **new run** (new conversation, new email thread). The run is the spine of everything below.

### Two gates, in sequence (do not conflate them)

| | **Readiness gate** (new) | **Execution gate** (exists, `gating.py`) |
|---|---|---|
| Question | *Is the task well-specified enough to start?* | *Is my planned action safe to auto-run?* |
| When | before routing/execution | before each side-effecting action |
| Inputs | task fields + relationships | action type + blast radius + agent confidence |
| Below bar | park in `awaiting_input`, email operator the **specific** gaps | file `checkpoint_brief`, email operator, block |
| Artifact | `task_readiness_assessment` | `checkpoint_brief` |

Readiness runs **first**. A task that isn't ready never reaches the execution gate.

---

## Primitive 1 — Readiness gate + `task_readiness_assessment`

Before an agent executes, it scores whether the task captured sufficient context to succeed. The score is computed by the **responsible agent** (it has the role context to judge) and persisted as a `task_readiness_assessment` entity linked `REFERS_TO` the task.

**Score axes** (0–1 each; reuse/extend the confidence rubric `ent_22fd6f25159f1f2689726780`):

- `goal_clarity` — is the desired outcome unambiguous and singular?
- `constraints_present` — are limits/guardrails/acceptance criteria captured (fields or linked policy)?
- `tooling_identified` — are the skills/permissions/vendor bindings the task needs known and granted?
- `context_density` — are the entities the work depends on linked (not just named in prose)?
- `acceptance_criteria` — is "done" checkable?

**Hard floors** (mirroring the existing rubric): missing goal or acceptance criteria caps the score low regardless of the other axes.

**Outcome:**
- `ready` (≥ threshold, default **0.75**) → proceed to kickoff + execution gate.
- `not_ready` → set `task.status = awaiting_input`, write the assessment with a `missing[]` list, and **email the operator a targeted request** naming exactly what's missing (not a generic "need more info"). The operator's reply (Primitive 2) fills the gap; the agent re-scores on the next cycle.

The threshold is configurable per agent/boundary via `execution_policy`, so well-understood recurring tasks can run at a lower bar and high-blast domains at a higher one.

---

## Primitive 2 — Email as the operator transport (replaces Telegram)

Per the `email_replaces_telegram_as_transport` decision, **email is the swarm's preferred operator I/O transport**. Telegram (Cyphorhinus) is deprecated and retired once the inbound daemon is live (retained only as an optional break-glass channel if Gmail delivery fails).

### Outbound (mostly exists)
Progress updates, readiness requests, and checkpoint briefs are sent as a **threaded Gmail conversation** via `gws gmail`, reusing the `publish_rendered_page` rendering for rich reports. Every message in a run shares one thread.

### Inbound (new daemon — successor to Cyphorhinus)
A long-polling daemon (working name **Riparia**; final genus name TBD with the operator) polls Gmail for operator replies and threads each reply back to its run:

1. **Match** the reply to its run conversation by RFC822 `In-Reply-To` / `References` / `Message-ID` header chain. **Fallback:** a `[#ent_<task_id>]` token in the subject line (stamped on every outbound message) for when header chains break.
2. **Write** the reply as an `agent_message` (`role=user`, `sender_kind=operator`) `PART_OF` the run conversation — the direct analogue of Cyphorhinus's `operator_followup REFERS_TO activity_log`.
3. **Surface** the directive to the responsible agent, which picks it up on its next cycle (scheduled) or via SSE (always-on). The daemon **routes; it does not execute** — same invariant as Cyphorhinus.

Inbound replies serve double duty: they answer readiness requests (`awaiting_input` → re-score) **and** resolve checkpoints (an emailed `/approve` / `/reject` verdict, mirroring the GitHub-comment verbs in the HITL checkpoints design).

**Threading is the load-bearing risk.** Outbound must always set a stable thread identity and the subject token, and inbound must never silently drop an unmatched reply (echo back "couldn't match this — reply to a more recent message", exactly as Cyphorhinus does today).

---

### Provisioning note (the operator's mail account)

A *truly dedicated* mailbox requires a Google Workspace **domain** the operator administers — on a **consumer `@gmail.com` account you cannot create sub-mailboxes**, and a brand-new Google account can't be created headlessly. So on a consumer account the practical "dedicated address" is a **plus-alias** (`<operator>+swarm@gmail.com`): it needs zero provisioning, the existing `gws` auth already covers it, and Riparia filters inbound on `to:<operator>+swarm`. `ATELES_SWARM_EMAIL` holds whichever address is chosen; everything downstream is identical.

**Confirmed config (live-validated against gws 0.22.5, 2026-06-24):**
- `ATELES_GMAIL_SEND_CMD='gws gmail users messages send --params '"'"'{"userId":"me"}'"'"' --upload {eml} --upload-content-type message/rfc822'` — the raw RFC822 path. The `+send` helper can't set `Message-ID`/`In-Reply-To`/`References`, so it can't thread; only `users messages send` with an uploaded `.eml` preserves headers. (`gws --upload` is cwd-sandboxed, so `run_email` stages the `.eml` under cwd.)
- Inbound body via `gws gmail +read --id <id> --format json` → `body_text`; poll via `gws gmail +triage` (returns `labels`).
- **Gmail rewrites the `Message-ID` on send**, so a reply's `References` chain usually won't carry our synthetic root — the **subject `[#ent_…]` token is the load-bearing inbound matcher**; References parsing is best-effort fallback.

## Primitive 3 — Conversation per execution run

Each run gets exactly one `conversation` entity, anchored `PART_OF` the task (and `PART_OF` the plan when known), created at **dispatch** (not at finalize). This matches the anchoring `session_finalize` already uses and satisfies the session-integrity invariant (a conversation must be `PART_OF` a plan **or** a task) — so a later `finalize_session(conversation_id=…)` appends to it rather than opening a second conversation. Everything in the run appends `agent_message` rows to it: kickoff, progress, the readiness request, operator replies, checkpoint briefs, the outcome.

- A retry/reopen opens a **new** conversation (new run) — runs never share a thread, so history stays legible.
- The Gmail thread maps **1:1** to the conversation: thread ↔ conversation ↔ run.
- This **extends** `session_finalize.py`'s existing `task_id` binding — finalize keeps closing the run, it just no longer *creates* the conversation from scratch.

Implementation touch points: `execution/daemons/apis/swarm_dispatch.py` (create/resolve the run conversation at dispatch) and `lib/daemon_runtime/session_finalize.py` (append + close rather than create).

---

## Impact (closing the loop to strategy)

On `verified → done`, finalize records an `outcome` linked to the task and, where the task rolls up to a strategy (swarm-wide strategy-spine plan `ent_d10ad28dffb8c6604a4151c2`), to the relevant `metric_contract` — so "did it work" feeds impact tracking, not just turn storage. This is the VERIFY + PERSIST loop-body completion the task-spine plan already commits to; the strategy link makes it measurable.

---

## Rollout: one task at a time (the canary)

No swarm-wide email-driven autonomous execution until a **single real, low-blast, well-owned task** has run the full loop end-to-end — readiness → kickoff → execute → progress → operator reply → verify → finalize → impact — under observation, with learnings captured and adjustments made. This mirrors the warn-only posture of the session-integrity hooks: prove the path on one before trusting it on many.

### Build phases (each shippable + verifiable on a live task)

- **E1 — Conversation per run.** Create the run conversation at dispatch; finalize appends/closes. Verify on one task that all turns land in one conversation. *(no behavior change to the operator yet — safest first step)*
- **E2 — Outbound email on the run thread.** Send kickoff + progress on a stable Gmail thread with the `[#ent_…]` subject token. Operator sees the thread; replies still go to Telegram for now.
- **E3 — Inbound daemon (Riparia).** Stand up the Gmail reply-router; match replies to the run conversation; write operator `agent_message` rows. **Do not retire Telegram until E3 is proven** (don't deadlock — every channel that asks for input must have a working return path).
- **E4 — Readiness gate.** Register `task_readiness_assessment`; score before execution; park `not_ready` tasks in `awaiting_input` with a targeted email; re-score on reply.
- **E5 — Canary.** Run one real task through E1–E4 end-to-end, observed. Capture learnings, adjust.
- **E6 — Retire Telegram + widen.** Flip email to sole transport, decommission the Cyphorhinus Telegram path, and graduate from one-at-a-time to broader autonomy per domain as each earns trust.

---

## Why this is the right shape

- **Reuses the primitives already built** — lifecycle, watchdog, gating, finalize, the outbound email pipeline, and the Cyphorhinus reply-routing pattern. Two new artifacts (`task_readiness_assessment`, the run conversation) and one new daemon (an email clone of an existing one).
- **Two gates with distinct jobs** — readiness (well-specified?) before execution (safe to act?) — so under-specified tasks get *clarified*, not *failed*, and the operator is asked precise questions.
- **One thread per run** — email thread = conversation = run, so the operator's inbox is the audit trail.
- **Transport decoupled from execution** — keeps the `2026-05-27` invariant; swapping Telegram→email touches only the transport layer, not how agents act.
- **Graduation built in** — start at one task, widen per domain as trust accrues, exactly the operator-steered rhythm the HITL checkpoints design already uses.

## Risks / notes

- **Don't deadlock.** Never retire Telegram (E6) before the email return path (E3) is proven. Any message that requests operator input must have a working, tested reply ingestion path — same lesson as the `/confirm-gates-clear` dead-end.
- **Threading fragility.** Header chains break (forwarding, client quirks); the `[#ent_…]` subject token is the fallback, and unmatched replies must be surfaced, never dropped.
- **PII in email.** Operator emails may carry operator data; this is the operator's own inbox (fine), but rendered reports to **beneficiaries** stay behind the existing per-delivery approval + PII-scrub guardrail.
- **Readiness false-negatives.** Too high a threshold stalls good tasks in `awaiting_input`; start at 0.75, tune per domain, and make the watchdog escalate tasks parked too long so nothing silently rots.
- **Readiness ≠ execution confidence.** Keep the two scores and their thresholds separate in config and in code; collapsing them reintroduces the "is it safe?" / "is it ready?" ambiguity this design removes.
```
