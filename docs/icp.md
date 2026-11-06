# Ideal Customer Profile (ICP)

## Scope

Covers the intended operator, the distinction between operating Ateles and benefiting from its work,
current adoption limits, jobs-to-be-done, alternatives, and historical functionality-audit evidence.
Out of scope: pricing, go-to-market tactics, or new deployment and authority capabilities.

## Purpose

Define whom Ateles is for without treating the current reference implementation as the limit of its
organizational design. Audience describes who has the need; implementation evidence establishes which
uses a particular deployment can safely support.

## The one-line answer

**Ateles is for accountable technical operators who need humans and AI agents to carry out ongoing work
under explicit authority and durable, inspectable state.**

They may operate it for their own organization or on behalf of client organizations. Constara buys and
operates the infrastructure; its clients buy managed outcomes and need not install or administer Ateles.
Who runs the infrastructure and whose work it serves are separate questions.

## Audience and deployment readiness

Technical self-adoption and the current reference implementation's supported deployment shapes are
separate from the intended audience. Teams and organizational beneficiaries are not excluded by design.
A client who directs work or approves actions may be a user or principal without administering Ateles.
Operating the infrastructure does not give the operator authority over all client work.

The current proving ground is a **single-operator reference implementation**. The multi-operator design
intent and its implementation limits are stated in the [README](../README.md#vision-and-execution-status)
and [foundation status](foundation/status.md). Multiple principals, client authority, and isolation are
supported only where the actual deployment has been qualified. This audience statement makes no
team-readiness or cross-client-isolation claim and grants no new authority.

Direct adopters must be able to evaluate and operate the reference implementation, including adapting
code, provisioning identities and secrets, and maintaining the required infrastructure. A nontechnical
beneficiary receiving managed outcomes need not do those things. Managed operation remains subject to
the same qualified capabilities and explicit authority boundaries.

## Defining characteristics of the primary operator

- **Accountable:** Responsible for ongoing work in their own organization or for client organizations,
  within explicitly established authority.
- **Technical:** Able to evaluate, adapt, and operate the infrastructure, or to provide that operational
  capability as part of a managed service.
- **Delegation-minded:** Needs work to continue across humans, agents, tools, and time, with clear ownership
  and approvals.
- **Governance-minded:** Needs attributable actions, scoped capabilities, inspectable evidence, and
  recoverable execution.
- **Record-minded:** Needs durable state that supports continuity and reconstruction across work, rather
  than relying solely on the context of an individual assistant session.

A solo founder is one example. Running a software company, managing personal finances, using crypto,
tracking health, living in a particular place, and working in particular languages are not requirements.

## Jobs they are hiring Ateles to do

1. **Operate**: dispatch agents across work domains from one surface, without manual sequencing or
   re-explaining context each time.
2. **Govern**: keep every agent identifiable, in scope, and auditable; escalate decisions to the appropriate
   principal.
3. **Remember**: maintain a durable, queryable record of work and its evidence.
4. **Extend**: add agents or workflows through the design's declarations and grants, subject to the
   implementation's supported capabilities.
5. **Trust the dangerous parts**: distinguish work permitted to run unattended from actions requiring
   explicit approval under the applicable authority.

These are the operator's jobs and the design's aims, not a claim that every deployment implements each
one fully. [Foundation status](foundation/status.md) records the implementation boundary.

## Nearest substitute and when Ateles earns its overhead

For a technical operator, the closest substitute may be a **vendor-native coordinator with connectors
and memory**. Where that meets the job, additional infrastructure is overhead.

Ateles is relevant when work across agents, tools, and time requires explicit authority, persistent
ownership, reconstructable evidence, and qualified recovery under an inspectable record. Memory used to
assemble context is not by itself a contract for authoritative state or permitted action. Compare what
each alternative actually persists and enforces rather than infer this from the word *memory*.

This is a conditional comparison of the job and the actual capabilities. Vendors can provide those
properties too; their presence or absence must be evaluated rather than assumed.

## Adjacent users

- **Agent-infrastructure engineers** evaluating patterns such as attribution, capability grants,
  observation logs, and workflow gating to borrow into their own systems.
- **AI/agent-operations researchers** studying long-running work across agents, tools, and sessions.
- **Operators needing easier packaging** who have the underlying need but cannot yet adopt and maintain
  the reference implementation directly. Managed outcomes and direct self-adoption have different
  operational requirements.

## Poor fits and current adoption limits

- **A simpler coordinator already satisfies the job.** One assistant or a vendor coordinator with
  connectors and memory can be sufficient; running Ateles then adds unnecessary infrastructure.
- **Turnkey self-service adoption is required today.** The reference implementation requires technical
  evaluation and operation. This is a readiness limit, not a claim that organizational beneficiaries
  must be technical.
- **A ready-made team or hosted-resale platform is required.** Organizational design intent is not proof
  of a qualified multi-operator deployment or client isolation. Establish the required capabilities
  before committing to such use.
- **Unbounded action without accountable authority is desired.** That conflicts with the purpose of
  explicit authority, attribution, and approval.

## Historical functionality-audit evidence

The original profile, last revised on **2026-06-25**, was reverse-engineered from the repository's
functionality rather than the README or positioning. It inferred a solo founder running company and
personal operations from the breadth of the configured workloads.

The following six clusters retain that audit's examples. Present-tense descriptions within the clusters
refer to the audited repository, not a freshly verified inventory or current deployment guidance. They
show why the reference implementation developed as it did; they neither define universal audience
requirements nor prove that only a solo operator can benefit. Read the strategic audience and readiness
sections above for the intended use and its limits.

### 1. Software product engineering

The audit identified a software-delivery organization expressed as agents and daemons:

| Function | Where it lives |
| --- | --- |
| Code implementation from issues | `cicada` skill; dispatched by `formica`/`neotoma-agent` daemons |
| PR review & stewardship | `vanellus`, `lanius` skills; `loxia_review.py` + `loxia-pr-review.yml` GHA |
| QA / release-readiness | `phoenicurus` skill; `phoenicurus-release` daemon |
| Autonomous releases | `struthio` skill; `create-release` / `publish` skills |
| Product management & architecture | `pavo`, `waxwing` skills |
| Issue/PR triage off webhooks | `formica`, `neotoma-agent` daemons; `apis` GitHub gateway |
| Dev-rel & docs quality | `regulus` skill |

The original profile inferred an operator shipping software products, including **Neotoma** and
**Ateles**, who wanted attributable engineering work from the fleet.

### 2. Go-to-market, content, and customer development

`corvus` (content/social voice), `manucode` (copy/positioning), `ciconia` (marketing strategy), `aythya`
+ `accipiter` (brand/UX design), `hirundo` + `analyze-neotoma-feedback` + `process-feedback` +
`interview-admin` (customer development), and `write` / `write-blog-post` / `social` / `deploy-website` /
`create-website` (publishing). Social scheduling runs through a Typefully MCP.

The original profile inferred a solo founder handling developer relations and go-to-market without a
marketing hire. This describes the reference workload, not a requirement to work alone or in public.

### 3. Financial operations and cross-border tax

`monedula` executes recurring payments in **both fiat (Wise) and Bitcoin**; `fringilla` and `run-scorecard`
do portfolio/liquidity analysis; `extract-amazon-order` and `quarterly-portfolio-review` handle expense and
review hygiene; `picus` prepares **multi-jurisdiction taxes** and `buteo` covers legal/GDPR.

The original profile inferred personal financial operations, crypto holdings, and cross-border tax
exposure. Those are examples of configured domains, not audience qualifications.

### 4. Calendar, communications, and personal operations

`gorilla` + `scrape-chatgpt-workout` (workout logging, progression analysis, inactivity nudges);
`cotinga` (05:00 meeting-prep briefings), `sylvia` + `remember-calendar` (recurring-task ↔ calendar sync),
`find-technician-slot` (scheduling around workouts); `strix` + `record_meeting` + `tyto` + `piculet`
(meeting recording and transcription with consent tracking); `turdus` + `email-triage*` + `riparia`
(email triage, drafting, reply routing); `language` (English / Spanish / Catalan, Barcelona usage).

The original profile inferred a busy calendar, email, fitness tracking, and a multilingual local context.
Those choices describe the audited configuration, not required habits, languages, or geography.

### 5. Shared memory and an audit trail

Every daemon and skill reads and writes **Neotoma** as the canonical entity store; `tyto`/`piculet` capture
screenshots and audio into it; `sturnus` + `intake-relationship` maintain a CRM inside it. The runtime
substrate adds **AAuth-signed identity** (RFC 9421 HTTP signatures, `lib/daemon_runtime/aauth_httpsig.py`),
**capability grants** (`grant_checker.py` + `mcp_tool_grant_proxy`), an **observation log** of every tool
call, **session-integrity enforcement** (`.claude/hooks/`), and **execution gating** by confidence ×
blast-radius (`gating.py`).

The original profile identified a need to trust the fleet: to know which agent did what, on whose
authority, with what scope, and built on which evidence. The audited gates illustrate that need; the
permitted actions and approvals of any deployment still require their own qualification.

### 6. Operator-controlled infrastructure

Scheduling is `launchd` on macOS plus a `docker-compose` cloud option on a €4/mo ARM VPS over Tailscale;
secrets are SOPS+age snapshots decrypted offline; GitHub PATs and AAuth keypairs are the operator's own;
the config-sourcing linter (`check_hardcoded_config.py`) and the public/PII discipline exist precisely so
**any operator can fork and supply their own context entities.** There is no installer and no hosted tier.

The original profile inferred a technically self-sufficient operator who would adapt the code and manage
the infrastructure. Its deployment inventory is historical evidence, not a current installation guide.


## Implications for documentation and positioning

Explain the accountable operator's work first, then distinguish the governance substrate from the
reference implementation's configured operations. Documentation should let a technical operator evaluate
the pattern, establish the supported substrate, and adopt or replace agents and skills for work within
their authority. Examples from personal operations and a company of one illustrate the reference
implementation; they do not define the boundary of organizational use.

Keep the operator, the beneficiary, and the holder of authority explicit in managed-service examples.
Neither a strategic audience statement nor operational custody supplies deployment readiness or client
authority.

## Related Documents

- [README](../README.md): category, organizational design, and vision versus implementation status.
- [Foundation charter](foundation/charter.md): purpose and the objectives served by the jobs above.
- [Foundation status](foundation/status.md): implementation evidence and remaining design work.
- [Setup](setup.md): installation and operation requirements.
