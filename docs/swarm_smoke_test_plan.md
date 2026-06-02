# Swarm smoke-test plan

*Mirror of Neotoma plan `ent_fb1d31034a3bca24fe66a1f0`. Do not edit manually — update via Neotoma.*

## Purpose

Define the phased smoke-test suite that validates the Ateles agent swarm before each capability tier is considered shippable. Each tier is a gate: failures feed back into SKILL.md tuning, orchestrator fixes, or schema work, and tier N+1 does not begin until tier N passes.

## Scope

Covers the operator-driven and Anthus-driven smoke-test procedures, the test issues and labels that drive each tier, pass criteria, and dependencies that gate each tier's start. Does not cover production workflows — see `swarm_orchestration.md` for those. The full lifecycle smoke run against `harness-sandbox` is documented in `smoke_test_runbook.md`; this plan supersedes it as a multi-tier programme.

## Design principle

Each tier is a gate on swarm maturity. We don't run tier N+1 until tier N passes cleanly. Failures inside a tier feed back into SKILL.md tuning, orchestrator bugs, or schema gaps — those get fixed before advancing.

Each tier maps to a `workflow_definition` entity (existing or new) so Anthus can drive it. Each test is an issue filed against `markmhendrickson/swarm-smoke` with the labels that select its workflow.

## Test repo: `markmhendrickson/swarm-smoke` (new)

Dedicated to swarm smoke tests. Isolated from `harness-sandbox` (graveyard of closed issues) and the operator's real backlog. Easier to grep, replay, and reset.

## Tier 0 — Daemon health checks (new)

**Question:** Are all active T3 daemons reachable, processing events, and persisting entities correctly?

**Mode:** operator-driven (manual trigger or dry-run invocation). Run before Tier 1.

**Tests:** One test per active T3 daemon.

| # | Daemon | How to trigger | Pass signal |
|---|---|---|---|
| D1 | **Turdus** | Send a test email to Gmail; wait ≤5 min | `email_message` entity visible in Neotoma inspector within 5 min |
| D2 | **Sylvia** | Create a recurring task entity in Neotoma; mark done; wait one poll cycle | `task.due_date` rolled forward on next poll; GCal event updated |
| D3 | **Cotinga** | `python cotinga.py --dry-run` (or trigger Phase 1 manually) | Telegram briefing received with today's events; Neotoma entities created for attendees |
| D4 | **Tyto** | Drop a screenshot into `$TYTO_SCREENSHOTS_DIR` | `screenshot` entity appears in Neotoma within 10s |
| D5 | **Apus** | POST a test mirror-profile webhook to `apus.markmhendrickson.com` | Git commit appears in target repo via `ateles-agent` identity (blocked until `ateles-agent` machine account exists) |
| D6 | **Anthus** | Call `orchestrator.compute_ready_gates` directly on test workflow + synthetic issue dict | Ready gates returned without exception; no dispatch attempted (Phase 6 deferred) |
| D7 | **Monedula** | Create a payment task entity manually in Neotoma | Telegram notification with payment details sent; no real transfer executed |
| D8 | **Formica** | Open an issue in `markmhendrickson/ateles` | Neotoma `issue` entity created within 60s; task dispatched to Apis |
| D9 | **Neotoma-agent** | Open an issue in `markmhendrickson/neotoma` | Neotoma `issue` entity created (daemon status=planned — may not pass yet; document result either way) |
| D10 | **Apis** | Create a task entity with `auto_executable=true` in Neotoma | Task picked up and routed to correct T4 agent within one poll cycle |

**Pass criteria for each daemon:**

1. Daemon is running (visible in `launchctl list`)
2. Expected Neotoma entity is created with correct entity_type and fields
3. No error logged to Telegram system bus (CyphorhinusBot)

**Blockers:**
- D5 (Apus) blocked until `ateles-agent` GitHub machine account + PAT exist
- D9 (Neotoma-agent) is in `planned` status — run as a negative/baseline test for now

---

## Tier 1 — Single-agent gate satisfaction

**Question:** Does each individual agent produce a recognisable artifact when invoked on a relevant work item?

**Workflow:** `single_gate_smoke` (new — one phase, one gate)

**Prerequisite:** Create `markmhendrickson/swarm-smoke` repo (`gh repo create markmhendrickson/swarm-smoke --public`).

**Tests:** 14 issues, one per invocable T4 worker agent. Each tests only that agent's artifact-header convention.

| # | Agent | Status | Issue title | Expected artifact |
|---|---|---|---|---|
| T1.1 | Pavo | planned | Decide: feature A vs feature B | `[pavo] acceptance_criteria:` |
| T1.2 | Paradisaea | planned | UX flow for subscribe form | `[paradisaea] copy_and_ux_flow:` |
| T1.3 | Bombycilla | planned | Schema proposal for X | `[bombycilla] schema_or_api_proposal:` |
| T1.4 | Phoenicurus | planned | Test plan for migration | `[phoenicurus] test_plan:` |
| T1.5 | Buteo | planned | Legal review of T&Cs change | `[buteo] compliance_review:` |
| T1.6 | Luscinia | planned | Compliance verdict on data flow | `[luscinia] compliance_verdict:` |
| T1.7 | Struthio | planned | Release notes for v0.1 | `[struthio] release_note:` |
| T1.8 | Accipiter | planned | Launch brief for X | `[accipiter] launch_brief:` |
| T1.9 | Corvus | planned | Social post draft for new feature | `[corvus] social_post_draft:` |
| T1.10 | Regulus | planned | Docs diff for new SDK | `[regulus] docs_diff_or_no_change_note:` |
| T1.11 | Gryllus | planned | Fix typo in README | `[gryllus] pull_request_link: #N` |
| T1.12 | Vanellus | planned | Review PR #N | `[vanellus] merge_decision:` |
| T1.13 | Hirundo | planned | ICP signal summary for Q2 | `[hirundo] icp_synthesis:` |
| T1.14 | Gorilla | active | Log my workout: squat 5×5 120kg | `[gorilla] workout_logged:` |

**Held (no artifact-header convention yet):** Columba, Ciconia, Aythya. Add to T1 after their SKILL.md artifact headers are defined.

**Pass criteria:**

1. Every agent produces a comment with the correct artifact header
2. The artifact content is substantive (not boilerplate or refusal)
3. No generic preamble unrelated to the issue

**Mode:** operator-driven (`claude --print --skill <agent>`)

## Tier 1b — Public agent checks (new)

**Question:** Do the T2 public/resident agents respond correctly within their access scope?

**Tests:** 2 checks (no swarm-smoke issues needed — these test live endpoints).

| # | Agent | How to trigger | Expected |
|---|---|---|---|
| P1 | **Menura** | `GET markmhendrickson.com/agent/` with a `visibility=public` entity ID | Returns entity data; a `visibility=private` entity returns 403 or empty |
| P2 | **Onychomys** | Send a Telegram message to OnychomysBot | Routes, responds coherently; does not surface private data in a public reply |

**Pass criteria:**
1. Menura only exposes `visibility=public` entities — no private data leak
2. Onychomys responds within 30s

**Blocker:** P1 requires Menura to be deployed (currently `planned`).

---

## Tier 2 — Phase ordering and parallelism

**Question:** When multiple agents participate, does phase ordering hold and do parallel groups fire concurrently?

**Workflow:** `harness-sandbox|smoke_test_full_lifecycle` (existing 6-phase workflow, adapted to `swarm-smoke` repo)

**Tests:** 3 issues.

| # | Type | Labels | Expected |
|---|---|---|---|
| T2.1 | Full lifecycle | `smoke-test, customer-facing, needs-product-review, triage-full-lifecycle` | All 12 agents fire in correct phase order; parallel groups dispatch within same tick |
| T2.2 | Internal-only fast-path | `internal-only, triage-full-lifecycle` | Phase 6 skipped entirely |
| T2.3 | Docs-only fast-path | `docs-only, triage-full-lifecycle` | Phase 1 Pavo, Phase 2 Bombycilla, Phase 4 Buteo+Luscinia all skipped |

**Pass criteria:**

1. Phase N-1 satisfaction strictly precedes any Phase N dispatch
2. Parallel groups dispatch in same tick
3. Skip conditions suppress correct gates
4. `participation_record` entities accurately reflect dispatched / satisfied / skipped state

**Mode:** operator-driven

## Tier 3 — Multi-repo + identity correctness

**Question:** Does the harness map AAuth identity → correct PAT → correct GitHub identity for cross-repo work?

**Workflow:** `cross_repo_smoke` (new — Gryllus/Vanellus targeting two different repos)

**Tests:** 2 issues + 1 negative test.

| # | Scenario | Expected |
|---|---|---|
| T3.1 | Issue+PR in `swarm-smoke` | Gryllus uses `GITHUB_PAT_MARKMHENDRICKSON_SWARM_SMOKE`; PR author = `ateles-agent` |
| T3.2 | Issue+PR in `neotoma` | Vanellus uses `GITHUB_PAT_MARKMHENDRICKSON_NEOTOMA`; reviewer = `neotoma-agent` |
| T3.3 | Negative: Gryllus on `neotoma` | Returns `wrong_capability` (Gryllus grant is ateles-only) |

**Pass criteria:**

1. PRs authored by correct machine account
2. `agent_action_observation` entities show `agent_sub` ≠ `pat_attribution` (trust boundary enforced)
3. Under-scoped grant returns `wrong_capability`

**Mode:** operator-driven

## Tier 4 — Real product work

**Question:** Does the swarm handle real backlog work, not just synthetic tests?

**Tests:** 3 real backlog items.

| # | Character | Repo | Tests |
|---|---|---|---|
| T4.1 | Bug with reproduction | ateles | Gryllus produces a fix that actually fixes the bug |
| T4.2 | Schema migration | neotoma | Bombycilla surfaces real risks; Phoenicurus catches breakage |
| T4.3 | Customer-facing feature | either | Phase 6 produces shippable launch artifacts |

**Pass criteria:**

1. Operator merges PR without rewriting >20%
2. No phase-6 marketing copy the operator wouldn't ship
3. End-to-end <4 hours

**Mode:** operator-driven

## Tier 5 — Anthus autonomy

**Question:** Does Anthus run the workflow without operator intervention beyond filing the issue and replying to BLOCKERs?

**Tests:** Re-run Tier 2 and Tier 4 with Anthus driving.

**Pass criteria:**

1. Anthus dispatches every gate within 60s of predecessor satisfaction
2. Operator only intervenes on BLOCKER / OPERATOR_DECISION notifications
3. No spurious notifications

**Mode:** Anthus-driven

## Dependencies

### Already satisfied

- workflow_definition seed entities (ateles#12)
- artifact-header convention in all SKILL.md (ateles#22)
- AgentGrant Neotoma lookup (ateles#15)
- AAuth keypairs for Gryllus, Vanellus
- agent_grant entities for Gryllus (`ent_8e3101e9c7895abe93735c22`), Vanellus (`ent_09762f11c9ba947edea5d901`)
- `HARNESS_GRANTS_JSON` fallback removed (neotoma#934)

### Tier 0 prerequisites

- All active daemons installed as launchagents (`launchctl list | grep ateles`)
- D5 (Apus) blocked until `ateles-agent` GitHub machine account exists
- D9 (Neotoma-agent) run as baseline/negative until daemon reaches `active` status

### Tier 1 prerequisites

- Tier 0 passes (daemons healthy)
- Create `markmhendrickson/swarm-smoke` repo
- Register `single_gate_smoke` workflow_definition entity
- File 14 test issues with single-agent labels (T1.1–T1.14)
- Define artifact-header conventions for Columba, Ciconia, Aythya before adding T1.15–T1.17

### Tier 2 prerequisites

- Tier 1 passes
- Adapt `harness-sandbox|smoke_test_full_lifecycle` to `swarm-smoke` repo
- Verify `internal-only` and `docs-only` skip conditions wired in orchestrator

### Tier 3 prerequisites

- AAuth signed JWTs in `claude --print --append-system-prompt <gryllus|vanellus>` invocations
- Under-scoped grant test path documented
- Note: per-entity-type grant tightening and MCP tool-level authorization tracked in [ateles#26](https://github.com/markmhendrickson/ateles/issues/26) — not a Tier 3 gate but informs the under-scoped test path design

### Tier 4 prerequisites

- Tier 2 + Tier 3 pass
- Real backlog items prioritised and labelled

### Tier 5 prerequisites (Anthus)

- Anthus daemon installed as launchagent (plist exists but never loaded)
- Anthus Neotoma auth env var provisioned (the standard Neotoma API auth env var used by the other daemons)
- `_dispatch_gate` wires `subprocess.run([claude, --print, --skill, owner_agent])` (currently only logs/notifies per `anthus.py:194-202`)
- NVM-aware `claude` binary path in plist (currently `/opt/homebrew/bin:...` lacks NVM)
- Anthus `agent_grant` entity for `participation_record` writes

## Phased rollout cadence

| Phase | Run when | Gate to advance |
|---|---|---|
| Now | Tier 0 — daemon health checks (D1–D10) | All active daemons pass; D5/D9 documented as blocked |
| Now (parallel) | Operator-driven Tier 1 (T1.1–T1.12) | All 12 produce correct artifact headers with substantive content |
| Now (parallel) | T1.13–T1.14 (Hirundo, Gorilla) | Both produce correct artifact headers |
| +1 day | Operator-driven Tier 2 (T2.1–T2.3) | Phase ordering correct on synthetic issues |
| +2 days | Operator-driven Tier 3 (T3.1–T3.3) | Identity boundary enforced |
| +1 week | Operator-driven Tier 4 (T4.1–T4.3) | Operator-acceptable artifact quality |
| +2 weeks | Anthus dispatch wiring + Tier 5 | Full autonomous loop |
| After Menura deployed | Tier 1b — public agent checks (P1–P2) | No private data leak; Onychomys responds |
| After Columba/Ciconia/Aythya SKILL.md written | T1.15–T1.17 | Artifact headers defined and passing |

## Critical findings from audit

1. **Anthus is not deployed.** `launchctl list` shows only Formica running. Plist exists but never loaded.
2. **Anthus doesn't dispatch — only notifies.** `anthus.py:194-202` explicitly defers real dispatch to Phase 6.
3. **claude binary path** needs to be NVM-aware in launchagent.
4. **AAuth keys for non-harness agents** not yet needed — only Gryllus/Vanellus call `github_harness`.
5. **No daemon smoke tests existed (2026-05-27).** T3 daemons (Turdus, Sylvia, Cotinga, Tyto, Apus, Monedula, Formica, Neotoma-agent, Anthus, Apis) were all active/planned with no runbook for end-to-end health validation. Tier 0 added to address this.
6. **Agent roster grew to 30 agents** but T1 only covered 12. Tier 1 expanded to 14 (Hirundo + Gorilla added); Columba, Ciconia, Aythya held pending artifact-header conventions.
7. **T2 public agents (Menura, Onychomys) have no smoke tests.** Tier 1b added to cover the public access-control boundary once Menura is deployed.
