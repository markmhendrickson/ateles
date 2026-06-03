# Session Integrity — Mechanical Plan-Link, Turn Storage, and Artifact Linkage

**Status:** Design (Phase 6)
**Tasks:** `ent_1e11760c9761d67cc66baa86` (enforcement design, p1) + `ent_872bf00817aba111cdda441a` (canonical artifact taxonomy, p2)
**Plan:** `ent_99ace4dd6673aa36ed08b1fe` (Ateles Agent Swarm Architecture)

> This document specifies how **every write-bearing session** — whether human-in-the-loop (Claude Code, Cursor, Codex) or autonomous (ateles daemons/agents) — is *mechanically required* to (a) link to at least one plan that it keeps current, (b) store its turns as `conversation` + `agent_message` entities related `PART_OF` the plan, and (c) link any derived artifacts it produces back to both the conversation (`REFERS_TO`) and the plan (`PART_OF`). It also defines the **canonical artifact taxonomy** the linkage invariant consumes (satisfying the dependency task in one place).

---

## 1. Problem statement

All three behaviors are **instruction-only** today:

- The `CLAUDE.md` "apply on every turn" rules and the Neotoma MCP `[TURN LIFECYCLE]` block are **prompt-level guidance**, not enforced. A model that ignores them produces no error.
- The only mechanical session gate that exists is the global `~/.claude/stop-hook-git-check.sh` (blocks exit on uncommitted/unpushed git). It says nothing about plans, turns, or artifacts.
- No in-repo `.claude/settings.json` exists; there are no `SessionStart` / `UserPromptSubmit` / `SessionEnd` hooks.
- The `harness_event` schema (`689230f4-cd83-49b6-baa7-a752cf70629d`) is defined but **unused**.
- Daemons write `participation_record` (see `execution/daemons/anthus/participation.py`), `agent_action_observation`, and `daemon_report` — but **never turn entities** (`conversation` / `agent_message`).

**Consequence:** sessions and the artifacts they produce float free of the plan they advance. A prod census (`get_entity_type_counts`, 2026-05-30, 60,429 entities / ~700 type names) shows durable artifacts already exist in volume — `analysis(156)`, `report(103)`, `technical_research(102)`, `transcription(769)`, `note(728)`, `standing_rule(35)`, `architectural_decision(21)` — but most carry no edge back to the conversation or plan that produced them, and the same concept is fragmented across dozens of synonym types. Memory exists; provenance and recoverability do not.

---

## 2. Definitions

### 2.1 Write-bearing session

A session is **write-bearing** if, during its lifetime, it issues **any** Neotoma write (`store`, `correct`, `create_relationship`, `submit_entity`, `submit_issue`) **other than** the chat-bookkeeping pair (`conversation`, `conversation_message`/`agent_message`) for the session itself.

- A session that only answers a question and writes nothing is **not** write-bearing → exempt (grace path, §6).
- A session that stores any domain entity, corrects any field, files any issue, or mutates any plan **is** write-bearing → subject to the full invariant.

This definition is decidable at the MCP/proxy layer by inspecting the write stream, which is why it anchors the server-side enforcement (§5.2).

### 2.2 Session → plan binding

Every write-bearing session MUST be bound to **≥1 `plan` entity** via a stable `plan_id`. The binding is established at session start (client hook) or lazily at first domain write (server invariant), and recorded on the session's `conversation` entity in a `plan_ids: string[]` field. The bound plan(s) MUST be **updated during the session** (`todos`, `next_steps`, `decisions`, or `body`) when the session completes or settles work — not only at the end.

A session with genuinely no home plan MUST either (a) create one, or (b) attach to a standing catch-all plan for its repo (e.g. the Ateles plan for ateles-repo work). "No applicable plan" is not an exemption from binding; it is a prompt to create/choose one.

### 2.3 Turn

One `(user_input, assistant_reply)` exchange (HITL) or one `(trigger, agent_action_batch)` exchange (autonomous). Each turn produces:
- one `agent_message` (role=user / sender_kind=user, or the autonomous trigger record), and
- one `agent_message` (role=assistant / sender_kind=assistant) capturing the reply/outcome,
both related `PART_OF` the session `conversation`, which is `PART_OF` the bound plan(s).

### 2.4 Derived session artifact

Any durable, multi-turn-relevant entity the session **produces** that is not itself a turn or the conversation — an analysis, a finding, a decision, a transcript, a rule, etc. These are the entities that today float free; §4 defines their canonical buckets and §3.3 the linkage they must carry.

---

## 3. The three invariants

A write-bearing session is **integral** iff all three hold at session end:

### 3.1 Plan-link invariant
`conversation.plan_ids` is non-empty, every referenced plan exists, and at least one bound plan received an update observation during the session window.

### 3.2 Turn-storage invariant
For every turn that issued a write, both `agent_message` rows exist and are `PART_OF` the `conversation`; the `conversation` is `PART_OF` each bound plan. (Reuse the `store-neotoma` skill conventions for entity shape and `turn_key` idempotency.)

### 3.3 Artifact-linkage invariant
Every derived session artifact (a canonical bucket per §4) produced during the session carries **both**:
- `REFERS_TO` → the session `conversation`, and
- `PART_OF` → at least one bound `plan`.

Artifacts that are themselves plan-structural (the plan, its tasks) are exempt from the `PART_OF`-plan leg (they *are* the plan tree) but still take `REFERS_TO`-conversation.

---

## 4. Canonical session-artifact taxonomy

*(Satisfies task `ent_872bf00817aba111cdda441a` acceptance criteria 1–3.)*

### 4.1 Canonical buckets (closed set)

The linkage invariant operates over a **closed set of 8 buckets**. New artifact writes SHOULD normalize to one of these `entity_type` values; the long tail of synonyms maps in via §4.2. Buckets are reconciled with existing registered schemas rather than duplicating them.

| Bucket | Canonical `entity_type` | Reconciles with existing | Meaning |
|---|---|---|---|
| Analysis | `analysis` | competitive/partnership/relevance/strategic_analysis, technical_research, report | A reasoned assessment of a target (product, repo, content, market). |
| Finding | `finding` | research_finding, analysis_finding, insight | A discrete, citable observation extracted during work. |
| Preference | `preference` | ui_/retrieval_/instruction_/user_preference | An operator preference about how work should be done. |
| Rule or policy | `agent_policy` *(keep)* | standing_rule, rule_update, core_principle, life_tenets | A durable rule governing agent/operator behavior. Keep `agent_policy` as canonical (already enforced in consultation protocols); `standing_rule` is an accepted alias. |
| Decision | `decision_record` *(keep)* | architectural_decision, decision, decision_note, agent_decision, product_decision | A settled choice with rationale. |
| Transcript | `transcript` | transcription, meeting_transcription, thread_summary | Verbatim or near-verbatim capture of a conversation/meeting/audio. |
| Note | `note` | note + ad-hoc variants | Free-form durable text that is not one of the above. |
| Escalation | `escalation` *(keep)* | — | A surfaced blocker/decision needing a higher authority (already a registered schema). |

`checkpoint_brief` and `daemon_report` remain their own registered types (operational, not "derived knowledge artifacts") and are **out of scope** for the artifact-linkage invariant, but daemons SHOULD still link them `REFERS_TO` their originating work entity where one exists.

### 4.2 Synonym → bucket mapping (long-tail collapse)

Store-time normalization (§4.3) maps observed synonyms to canonical buckets:

```
analysis, competitive_analysis, partnership_analysis, relevance_analysis,
strategic_analysis, technical_research, report                 -> analysis
research_finding, analysis_finding, insight                    -> finding
preference, ui_preference, retrieval_preference,
instruction_preference, user_preference                        -> preference
standing_rule, rule_update, core_principle, life_tenets        -> agent_policy
architectural_decision, decision, decision_note,
agent_decision, product_decision                               -> decision_record
transcription, meeting_transcription, thread_summary           -> transcript
note (+ ad-hoc one-off note-like types)                        -> note
```

### 4.3 Canonicalization mechanism

Two-pronged, to avoid a disruptive mass migration:

1. **Store-time normalization (forward-looking):** the MCP `store` path maps an incoming `entity_type` through §4.2 before persisting. New writes land in canonical buckets immediately. Original requested type is preserved in `raw_fragments.requested_entity_type` for audit.
2. **Periodic reconciliation (backward-looking):** a scheduled job (candidate: a Cathartes or Otus sweep) merges existing singleton synonyms into their canonical bucket via `merge_entities`, oldest-first, lowest-volume types first. This is **non-urgent** and does not block enforcement — enforcement validates *linkage*, not *bucket purity*.

---

## 5. Enforcement layers

Hooks are the obvious primitive, but **only Claude Code exposes lifecycle hooks** — Cursor and Codex do not. The one chokepoint **all** harnesses share is the Neotoma MCP / AAuth proxy. The design therefore uses **two complementary layers**:

### 5.1 Client-side hooks (Claude Code only — best UX, partial coverage)

Add an in-repo `.claude/settings.json` with:

- **`SessionStart`** — record session start; if the session declares intent to write, prompt for / resolve a `plan_id` and stamp it on the `conversation`.
- **`UserPromptSubmit`** / **`PostToolUse`** — lightweight turn accounting (ensures `agent_message` rows are being written; warns if a domain write occurred with no turn record).
- **`SessionEnd` / `Stop`** — run the **integrity check** (§3) and **block exit** if a write-bearing session is non-integral, mirroring the existing git stop-hook pattern. Emit one `harness_event` per turn as the audit record (consuming the dormant `harness_event` schema `689230f4`).

Covers: Claude Code (the primary HITL harness and all autonomous ateles agents that run under it).

### 5.2 Server-side invariant (Neotoma MCP / proxy — uniform cross-harness coverage)

The cross-harness chokepoint is the identity/grant proxy that every harness's Neotoma traffic passes through:

- `execution/scripts/mcp_identity_proxy.py` — AAuth identity proxy (stamps agent identity on every call).
- `execution/mcp/mcp_tool_grant_proxy/proxy.py` — the #26 tool-grant enforcement proxy (already intercepts `tools/call`, denies ungranted tools, emits `tool_call_observation`).

The session-integrity invariant rides the **same interceptor**:

1. **Track** per-session writes in the proxy. The first non-bookkeeping domain write flips the session to *write-bearing*.
2. **Lazy plan-binding:** on that first domain write, require the session's `conversation` to carry `plan_ids`; if absent, the proxy returns a structured `ERR_SESSION_NOT_BOUND` hint instructing the caller to bind a plan (it does **not** silently invent one).
3. **Artifact-linkage check:** when a write creates a canonical-bucket artifact (§4), the proxy verifies (or, in warn mode, records the absence of) the `REFERS_TO`-conversation + `PART_OF`-plan edges within the same session window.
4. **Emit `harness_event`** per intercepted turn for the audit trail — giving Cursor/Codex the same audit record Claude Code gets from its hooks.

Covers: **all** harnesses (Claude Code, Cursor, Codex, autonomous daemons), because all route through the proxy.

### 5.3 Layer split summary

| Concern | Claude Code hook | MCP/proxy invariant |
|---|---|---|
| Plan binding at start | ✅ proactive prompt | ✅ lazy, enforced at first write |
| Turn storage | ✅ accounting + warn | ✅ `harness_event` audit |
| Artifact linkage | ✅ end-of-session check | ✅ per-write check |
| Block on violation | ✅ `Stop` hook | ⚠️ see §6 (warn→block ramp) |
| Cursor / Codex coverage | ❌ | ✅ |

---

## 6. Failure behavior, grace path, and rollout

- **Grace path (no-op sessions):** a session that issues no non-bookkeeping write is exempt — never blocked. This protects pure Q&A and read-only sessions.
- **Warn → block ramp:** ship the proxy invariant in **warn mode** first (records `harness_event` with `integrity_status: violated`, does not deny), so we measure the real violation rate and false-positive surface before turning on blocking. The Claude Code `Stop` hook may block earlier since it has richer session context and an interactive operator to correct course.
- **Penalty semantics:** `block` = refuse session exit (hook) / return structured error on the offending write (proxy); `warn` = allow but record the violation for later reconciliation by Otus/Cathartes.
- **Genuine-exception escape hatch:** a session may set `integrity_waived: true` with a one-line reason on its `conversation` (operator-initiated only) — recorded, audited, never silent.

---

## 7. Acceptance-criteria coverage

Design task `ent_1e11760c9761d67cc66baa86`:
1. ✅ Defines write-bearing session + session→plan_id binding (§2.1, §2.2).
2. ✅ Specifies conversation/agent_message shape + PART_OF-plan linkage, reusing `store-neotoma` conventions (§2.3, §3.2).
3. ✅ Specifies derived-artifact linkage invariant referencing the taxonomy (§3.3, §4).
4. ✅ Specifies the two enforcement layers and per-harness coverage (§5).
5. ✅ Defines warn-vs-block + grace path for no-op sessions (§6).
6. ✅ Specifies `harness_event` emission per turn as the audit record (§5.1, §5.2).

Taxonomy task `ent_872bf00817aba111cdda441a`:
1. ✅ Canonical bucket set, reconciled with existing schemas (§4.1).
2. ✅ Synonym→bucket mapping table (§4.2).
3. ✅ Linkage invariant the enforcement consumes (§3.3, §4.3).
4. ✅ Canonicalization mechanism — store-time normalization + periodic merge (§4.3).
5. ✅ Feeds taxonomy into the enforcement layers (§5).

---

## 8. Implementation follow-ups (not part of this design doc)

These become tracked tasks once the design is accepted:

- **CC hooks** (`ent_7205d6c75761b7d12f2a51c9`, p2, DEPENDS_ON this doc): author `.claude/settings.json` with the four lifecycle hooks (§5.1).
- **Proxy invariant** (`ent_f3cd6161d03316e440916933`, p2, DEPENDS_ON this doc): add the session-integrity interceptor to the MCP/proxy and wire `harness_event` emission (§5.2).
- **Store-time normalization:** implement the §4.2 mapping in the Neotoma `store` path (neotoma repo).
- **Periodic reconciliation sweep:** Otus/Cathartes merge job for existing synonym singletons (§4.3).
