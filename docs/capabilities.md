# Capabilities — the operational surface

Ateles is often described by its *substrate* (AAuth identity, grants, observation, gating). This document
covers the other half: **what the swarm actually does**, day to day, for its operator. The surface is large
because the [ideal operator](icp.md) delegates both their company's work and their personal life to the
fleet.

Two kinds of components do the work:

- **Daemons** (`execution/daemons/`) — long-running T3 processes that subscribe to Neotoma's SSE event
  stream or run on a launchd schedule, and dispatch T4 agents (`claude --print`) or act directly.
- **Skills** (`.claude/skills/`) — 87 prompt-defined capabilities. ~39 are generated agent-definition
  mirrors; ~48 are hand-authored workflows the operator invokes directly.

Maturity is stated honestly: several daemons are complete, others are skeletons or have flag-gated features.
The [README daemon table](../README.md#daemons) and [taxonomy](taxonomy.md) are the companion references.

## Purpose

Give an accurate, single-place reference to *what the swarm does* — the daemons and skills that automate the
operator's company and life — so the operational half of the repo is documented in proportion to its size.

## Scope

Covers all 18 daemons (role, trigger, maturity), the 87-skill catalog by domain, and the external systems
the fleet integrates with. The governance *substrate* that records and gates these actions is covered in
[architecture.md](architecture.md); per-agent prompts live in [agents/](agents/).

---

## Daemons (18)

### Coordination & dispatch

| Daemon | What it does | Trigger | Maturity |
| --- | --- | --- | --- |
| **anthus** | Swarm coordinator. Matches issues/PRs to `workflow_definition`s, opens `participation_record`s, dispatches gate agents, surfaces escalations to the operator. | SSE (task/issue/PR/escalation/grant) | Coordination + escalation complete; full gate orchestration phasing in |
| **apis** | Universal task dispatcher. Routes tasks to T4 skills by tag, applies the readiness gate (is the task well-specified?) and the execution gate (confidence × blast-radius), runs an A2A + GitHub webhook gateway (`:8742`), and retries stalled tasks. | SSE (task/checkpoint_brief) + HTTP | Core complete; run-thread features (E1/E2) flag-gated |
| **formica** | Issue/PR triage for the **ateles** repo. On a triage label, dispatches Cicada (issues) or Vanellus (PRs). | SSE | Dispatch complete; full PR automation phasing in |
| **neotoma-agent** | Same pattern for the **neotoma** repo, plus task due-date hygiene. | SSE | Dispatch works; hygiene flag-gated |

### Mirror & sync

| Daemon | What it does | Trigger | Maturity |
| --- | --- | --- | --- |
| **apus** | Receives Neotoma mirror webhooks (HMAC-verified), writes the mirrored files (agent SKILL.md, architecture docs) into the repo, commits, and pushes. This is how Neotoma → disk stays canonical. | HTTP (`:8741`) | Complete |

### Briefings & operator digest

| Daemon | What it does | Trigger | Maturity |
| --- | --- | --- | --- |
| **cotinga** | 05:00 meeting-prep briefings. Pulls the day's calendar, cross-references attendees against Neotoma contacts, and spawns async deep-prep agents for unknown attendees. | launchd (daily 05:00) | Phase 1 complete; deep-prep fire-and-forget |
| **morning-brief** | 05:30 digest in the Ateles voice. Waits for Cotinga's briefs, then composes a single operator-facing message. | launchd (daily 05:30) | Depends on Cotinga; fallbacks present |
| **aquila** | Monthly cofounder/strategy report. Thin scheduler that spawns the `aquila` skill to interrogate progress against plans, feedback, and meetings. | launchd (monthly) | Scheduler complete; reasoning in skill |

### Finance

| Daemon | What it does | Trigger | Maturity |
| --- | --- | --- | --- |
| **monedula** | Daily recurring payments. Scans yesterday's calendar + declared payment tasks, sends a Telegram preview, waits for operator approval, then executes via Wise (fiat) or Bitcoin. | launchd (daily) | Calendar→preview→approve→execute flow solid; handlers per payment profile |

### Email

| Daemon | What it does | Trigger | Maturity |
| --- | --- | --- | --- |
| **turdus** | Email triage. Polls Gmail, classifies messages, stores `email_message` entities, and creates tasks for actionable mail. | poll (~5m) | Keyword classification; LLM triage deferred |
| **riparia** | Email reply router. Polls the swarm mailbox for operator replies to execution-run threads and routes them back into the run conversation (the transport that succeeds Cyphorhinus). | poll (~60s) | Core route logic present |

### Capture (audio, screenshots, recordings)

| Daemon | What it does | Trigger | Maturity |
| --- | --- | --- | --- |
| **piculet** | Voice Memos + meeting-recording import & transcription, with entity extraction (people, tasks, decisions) into Neotoma. | poll (~60s) | Fully fleshed |
| **tyto** | Watches screenshot and recording directories; transcribes recordings with a `capture_method` consent stamp; stores screenshot/transcription entities. | poll (~10s) | Watch + dispatch; OCR/analysis deferred |
| **strix** | macOS menu-bar toggle to start/stop meeting/ambient recording (and mute control). | click handler | Minimal but functional |
| **cyphorhinus** | *(Deprecated)* Telegram reply router; superseded by `riparia` (email). Kept as break-glass. | Telegram long-poll | Deprecated |

### Health & lifecycle

| Daemon | What it does | Trigger | Maturity |
| --- | --- | --- | --- |
| **gorilla** | Weekly fitness summaries (sessions, volume, locations) and inactivity nudges from `workout_session` entities. | poll | Complete; read-only |
| **sylvia** | Recurring-task lifecycle. Rolls due dates, syncs tasks ↔ Google Calendar, reminds on human-owned tasks. | launchd (daily) | Core scan present; recurrence logic phasing in |

### Release

| Daemon | What it does | Trigger | Maturity |
| --- | --- | --- | --- |
| **phoenicurus-release** | Mon–Thu release-candidate preparation. Checks for unreleased commits + green CI, then runs the `/release` skill up to the RC-PR stop point and pages the operator. | launchd (weekdays) | Preflight complete; release logic in skill |

---

## Skills (87)

### Agent-persona mirrors (~39)

Generated from Neotoma `agent_definition` entities — the prompts a harness loads to *be* an agent. Do not
edit; corrections go through Neotoma. They cover the product org (`pavo` PM, `waxwing` architect,
`cicada` code, `vanellus`/`lanius` PR stewardship, `phoenicurus` QA, `struthio` release, `regulus` devrel),
GTM/brand (`corvus`, `manucode`, `ciconia`, `aythya`, `accipiter`), customer/relationships (`hirundo`,
`sturnus`), finance/tax/legal (`fringilla`, `picus`, `buteo`, `monedula`), health (`gorilla`), governance
(`columba` constitution, `robin` session compliance, `aquila` cofounder), and the daemon personas. See
[taxonomy.md](taxonomy.md) and [agents/](agents/).

### Hand-authored operator skills (~48)

| Category | Skills | Touches |
| --- | --- | --- |
| **Engineering workflow** | `commit`, `push`, `pull`, `debug`, `fix-feature-bug`, `create-feature-unit`, `create-prototype`, `run-feature-workflow`, `final-review`, `manage-error-debugging`, `report-error`, `create-release`, `publish`, `verify-deployment`, `report` | Git, test runners, GitHub Actions |
| **Meetings & audio** | `record_meeting`, `analyze-meeting`, `import-audio` | Audio, Whisper/ElevenLabs, Neotoma, Gmail, Calendar |
| **Email & calendar** | `email-triage`, `email-triage-auto`, `find-technician-slot`, `remember-calendar` | Gmail (`gws`), Google Calendar (`gws`), Neotoma |
| **Finance** | `run-scorecard`, `extract-amazon-order`, `quarterly-portfolio-review` | Neotoma, parquet, price feeds |
| **Health** | `scrape-chatgpt-workout` (+ the `gorilla` persona) | ChatGPT scrape, Neotoma |
| **Content & GTM** | `write`, `write-blog-post`, `social`, `draft-comparative-neotoma-post`, `draft-rendered-page`, `deploy-website`, `create-website` | Neotoma, websites, Typefully, X API |
| **Customer development** | `analyze`, `analyze-neotoma-feedback`, `process-feedback`, `interview-admin`, `intake`, `intake-relationship` | Neotoma, web research, GitHub |
| **Language** | `language` | English / Spanish / Catalan (Barcelona usage) |
| **Neotoma / plan / meta** | `update-plan`, `update-tasks`, `store-neotoma`, `create-execution-plan`, `learn`, `neotoma-learn`, `loop-start`, `loop-status`, `loop-stop` | Neotoma |
| **Infra / setup** | `sync-env-from-1password`, `setup-cursor-copies`, `disk-cleanup` | 1Password, filesystem |

---

## External systems the swarm integrates with

| System | Used for | Via |
| --- | --- | --- |
| **Neotoma** | Canonical memory/state for every entity | `mcpsrv_neotoma` MCP, signed HTTP |
| **GitHub** | Issues, PRs, reviews, commits | `github_harness` MCP (AAuth + PAT), GitHub Actions |
| **Telegram** | Operator paging + approvals | Bot API, Apprise |
| **Gmail** | Email triage, drafts, run-thread transport | `gws gmail` CLI |
| **Google Calendar** | Briefings, payment triggers, recurring sync | `gws calendar` CLI |
| **Wise + Bitcoin** | Recurring payments (fiat + crypto) | Wise API, BTC tooling |
| **Whisper / ElevenLabs** | Transcription + diarization | `transcribe_audio.py` |
| **Typefully / X API** | Social scheduling + analysis | `typefully` MCP, X API |
| **parquet stores** | Bulk finance/transcription/messaging data | `parquet` MCP |
| **1Password + SOPS/age** | Secret sourcing + offline materialization | `op` CLI, `sops` |

> Operator-specific values (calendar IDs, recipients, profiles, entity IDs) are **never** hardcoded — they
> are read from env / Neotoma at runtime so the swarm stays portable. See [forking.md](forking.md).
