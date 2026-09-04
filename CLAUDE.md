# Ateles — Claude Code Project Instructions

## Plan and task maintenance (automatic)

Each session maintains the Neotoma `plan` entity that matches **its own workstream** — never a fixed, hardcoded plan. The swarm-architecture plan `ent_99ace4dd6673aa36ed08b1fe` ("Ateles Agent Swarm Architecture") is the plan for swarm-architecture work **only**. Unrelated workstreams (tax prep, Neotoma release engineering, website, cloud hosting, etc.) each have their own plan and MUST NOT write into the swarm plan. Writing one workstream's `decisions`/`todos` into another's plan is the collision that corrupted this plan in June 2026.

**Select the bound plan once per session, as soon as the workstream is clear:**
1. Resolve the matching plan: `mcp__mcpsrv_neotoma__retrieve_entities` with `entity_type: plan` and a `search` for the workstream; pick the closest match.
2. If no existing plan fits, create one (`/update-plan` skill) and bind to it.
3. Maintain only that bound plan for the rest of the session.

**Apply these rules on every turn, without being asked — to the bound plan:**

- **Before correcting `decisions` or `todos`, RE-READ the current field and MERGE.** `correct` replaces the *entire* field, so add or update only the keys/items you authored and preserve every entry already present. NEVER rebuild a field from a stale in-memory copy — that silently deletes other sessions' entries. If the field changed since you last read it, re-read and re-merge before writing.
- **When a todo item is completed** — correct its `status` to `"done"` in `todos`, with relevant entity IDs, file paths, or PR numbers in a `notes` field.
- **Never mark a task or todo `done` while citing a commit, branch, file, or PR that does not resolve.** Verify the artifact exists first (git `cat-file`/`ls-remote`, GitHub). Unverifiable completion claims are the second failure mode that corrupted this plan in June 2026.
- **When a decision is settled** — add or correct one entry in the `decisions` map. One sentence, snake_case key.
- **When blockers change** (something unblocked, something newly blocked) — correct `next_steps` to reflect the current state.
- **When a new actionable task is identified** — create a `task` entity and link it `PART_OF` the bound plan. Use the `update-tasks` skill for field values and priority mapping.
- **When a daemon, entity, or file is renamed** — correct any stale references in `body`, `decisions`, and `todos` in the same turn the rename happens.

Do not wait until end of session. Apply corrections in the same turn as the work, after the work completes.

Use `mcp__mcpsrv_neotoma__correct` with idempotency keys in the form `update-plan-<field>-<YYYY-MM-DD>`. Use Neotoma prod (`mcp__mcpsrv_neotoma__*`) always.

For full step-by-step guidance: `/update-plan` and `/update-tasks` skills.

## Session-integrity hooks (mechanical enforcement)

`.claude/settings.json` wires three Claude Code lifecycle hooks (in `.claude/hooks/`) that mechanically enforce the plan-and-task contract above. They implement layer 1 of `docs/session_integrity.md`:

- **`session_start.py`** (SessionStart) — initializes per-session state, binds to the default plan (`ent_99ace4dd6673aa36ed08b1fe`), and injects a one-line reminder of the bind/turn/artifact contract. Always exits 0.
- **`user_prompt_submit.py`** (UserPromptSubmit) — lightweight per-turn counter. Exits 0.
- **`stop_finalizer.py`** (Stop) — the enforcement gate. Scans the transcript; classifies the session as **exempt** (no domain writes — grace path), **integral** (domain writes + a plan link + stored turns), or **violated** (domain writes but no plan link or zero turns). Emits a `harness_event` audit row each time.

All hooks are **fail-open** (stdlib-only Python; any error or missing `NEOTOMA_BEARER_TOKEN` → exit 0, never crash a session). Plan binding is judged from the **transcript** (an actual plan touch/link), not the SessionStart default intent.

**Rollout posture:** defaults to **WARN** (logs the violation, exits 0). Set `ATELES_SESSION_INTEGRITY_ENFORCE=1` to switch the Stop hook to **BLOCK** (exit 2 + `{"decision":"block"}`), preventing a clean stop until the session binds a plan and stores its turns. Per-session state lives in `.claude/.session_state/` (gitignored).

## Session conduct — how the operator wants to be worked with

These are standing operator instructions, stated repeatedly across sessions. They live here because **CLAUDE.md is re-injected from disk after every compaction**, so unlike an instruction given in conversation, they do not age out of a long session.

- **Dispatch, don't work inline.** Create a Neotoma `task` entity and let an agent claim it; use a subagent only where no swarm path exists. This binds to *all* work — research, analysis, design, payments, investigation — not only code. Work you recommend is work you file, in the same turn you recommend it, without waiting to be asked; dispatch proactively and in parallel. Reserve the session for judgement and conversation. Durable work never goes into a harness task chip (`spawn_task`) — a chip is not an entity, so it is unclaimable and invisible to the swarm.
- **Summarize what the operator said at the top of each reply,** cleaned up. Most operator input arrives as live voice transcription, which garbles names and can fabricate whole sentences; echoing what was heard is how the operator catches it. Do this even when the turn seems routine.
- **Give status updates unprompted** — what moved, what is blocked, and one recommended next step *per workstream*, so the operator can confirm whether to stop that workstream for now.
- **Name the related task entities whenever discussing work,** and link each by id into the Ateles task dashboard so the operator can open it. Work discussed without a task id cannot be tracked or found again.
- **Proceed on your recommendation instead of stopping to ask.** Ask only at a genuine fork you cannot resolve from the request, the data, or Neotoma. If you asked something and it went unanswered, re-surface it each turn until it is answered rather than dropping it.
- **Fix the swarm rather than routing around it.** Repair a broken daemon, gate, or dispatch path rather than doing its job by hand, and do so without asking. When the defect belongs to a dependency, file an issue against that repo with a test rather than writing an instruction that works around it.
- **Contextualize before executing.** Dispatched work must first check existing tasks, issues, PRs, and the codebase, so the swarm neither duplicates work nor re-decides settled questions.

### Standing instructions must survive compaction

An instruction that has to be repeated is not persisted. Compaction replaces the conversation with a summary, so anything stated only in chat is lost unless it happens to make that summary — which is why the two rules above about dispatching and summarizing had to be restated four times and twice respectively in one 2026-09-02 session.

Two mechanisms re-inject after a compaction boundary, and only these two:

| Mechanism | Fires | Use for |
|---|---|---|
| CLAUDE.md (this file) | Re-injected from disk on every compaction | Standing constraints, repo-wide |
| `SessionStart` hook, matcher `compact` | After auto- and manual `/compact` | Short restatement of interaction rules |

`PreCompact` cannot do this — its stdout goes to the debug log, never into context. `UserPromptSubmit` does inject, but fires every turn and so pays the cost on turns that never needed it.

`.claude/hooks/reinject_working_method.py` carries the short restatement. Note the failure it fixes: `session_start.py` was registered against `startup|resume|clear`, which **excludes `compact`** — so the hook meant to keep context alive was silent at precisely the moment context was lost. When adding a SessionStart hook, include `compact` in the matcher, or state why it is deliberately excluded.

## Repo-isolation hook (mechanical enforcement)

- **`sibling_repo_worktree_guard.py`** (PreToolUse: `Edit|Write|NotebookEdit|Bash`) — a distinct concern from the session-integrity hooks above: it protects **other repos**, not this session's audit trail. When operating from the Ateles repo, it **hard-blocks** any mutation of a *sibling* repo's **shared main clone** (e.g. `~/repos/neotoma`): file edits, and git-mutating Bash (commit, checkout/switch, reset, merge, rebase, cherry-pick, push, …). It allows the Ateles repo itself, any dedicated **linked worktree**, read-only git, and `git worktree add` (the remedy). On a hit it directs you to `git worktree add ~/repos/<repo>-wt-<slug> origin/main` first. Detection: `git rev-parse --git-dir` == `--git-common-dir` ⇒ main clone; also honors `git -C <path>` / `--git-dir=<path>`. Fail-open (stdlib-only; any error → exit 0). Override a deliberate case with `ATELES_ALLOW_SHARED_REPO_WRITES=1`. Motivated by a 2026-07-21 incident where a stray write + commit landed on another session's branch in the shared checkout.

## Deployment-checkout freshness (daemons)

`lib/daemon_runtime/checkout_drift.py` reports, at daemon startup, whether the checkout a daemon was launched from is **behind, diverged, or dirty** relative to its upstream. A daemon runs the working tree it was started in, not `origin/main` — so when that checkout drifts, a merged fix silently never reaches it.

**Advisory by default**: logs at ERROR and continues. These daemons are the swarm's release, payment, and dispatch path, and a guard that hard-stops eighteen of them on a stale checkout would cause a larger outage than the drift it prevents.

| Env var | Effect |
|---|---|
| `ATELES_ENFORCE_CHECKOUT_FRESHNESS=1` | Make drift **fatal** — the daemon raises `CheckoutDriftError` instead of warning |
| `ATELES_CHECKOUT_DRIFT_NO_FETCH=1` | Skip the remote-ref refresh (tests, deliberately offline hosts) |
| `ATELES_CHECKOUT_DRIFT_ROOT=<path>` | Force the path the guard inspects (entrypoint tests; production leaves unset) |

Two deliberate non-verdicts: a **failed fetch** reports `unknown`, not drift (offline must not look identical to unpushed commits, or the warning gets ignored), and **untracked files** are not drift (daemon checkouts accumulate logs and state files). **Ahead-only counts as drift** — unpushed commits in a deployment checkout are both invisible to review and one power-cycle from being lost.

Motivated by three occurrences on the same checkout: ateles#339 and #361 (both recovered by hand; the PR titles read "stranded in the deploy checkout"), then 2026-08-09, when `~/ateles-rc-src` sat on a 2026-07-28 local merge commit — 3 ahead, 14 behind — so ateles#401 merged to main and never reached the running daemon while `git pull --ff-only` refused without changing HEAD.

## Deployment-plist config drift (daemons)

`lib/daemon_runtime/plist_drift.py` reports, at daemon startup, whether the **live launchd environment** matches the **reviewed plist** in the repo (`execution/daemons/<name>/com.ateles.<name>.plist`). Config that lives only in a deployment artifact drifts invisibly: on 2026-08-25 `ATELES_SWARM_AUTO_BUILD` was absent from the repo plist while Apis ran with `=0`, so the swarm stopped before every build for four days after the bug justifying that rollback (ateles#460) was fixed, and no reviewed file recorded the decision.

**Advisory by default**: logs at ERROR and continues. A dispatch daemon that refuses to boot over a config difference would cause a larger outage than the drift it reports.

| Env var | Effect |
|---|---|
| `ATELES_ENFORCE_PLIST_CONFIG=1` | Make drift **fatal** — the daemon raises `PlistConfigDriftError` instead of warning |

Two deliberate non-verdicts: a **missing or unparseable live plist** reports `unknown`, not drift (hand-launched daemons and CI have nothing to compare against), and **`HOME`/`PATH`** are ignored as legitimately machine-specific. See `docs/runbooks/apis_autonomy_flags.md` for the autonomy-flag table and rollback rules.

Motivated by ateles#506: the reviewed Apis plist declared 9 of 14 live environment keys, pointed paths at the shared session clone instead of the deployment checkout, and omitted autonomy flags whose values matched code defaults — indistinguishable from unmade decisions.

### Which checkout a daemon runs from

Daemons run **dedicated checkouts**, never the shared main clone where interactive sessions work:

| Daemon | Checkout |
|---|---|
| Ateles daemons (Apis, Phoenicurus prepare, …) | `~/ateles-rc-src` |
| Phoenicurus **release** (`NEOTOMA_REPO_ROOT`) | `~/neotoma-rc-src` |

The shared clones (`~/repos/ateles`, `~/repos/neotoma`) are for sessions and are dirty most of the time. `publish.py` refuses to tag atop a dirty tree — correctly, since publishing ships whatever is in the working tree — so a release pointed at the shared clone blocks on whoever last left it dirty. That happened on 2026-08-10: an approved v0.21.5 publish refused over 12 modified files belonging to an unrelated session (ateles#412).

**Two consequences worth remembering when debugging a daemon:** verify against the checkout the daemon actually runs from, not the worktree you are editing in; and a merged fix does nothing until that checkout is updated.

---

## Gmail send-gate hook (mechanical enforcement)

- **`gmail_send_gate.py`** (PreToolUse: `Bash`) — blocks Gmail operations that can deliver mail without a per-message operator approval: `gws gmail users drafts update` (the misfiring call), `drafts send`, `messages send`, and the `+send`/`+reply`/`+reply-all`/`+forward` helpers. Staging and reads stay allowed (`drafts create`/`get`/`list`, `messages list`/`get`, `+read`), as does every non-Gmail `gws` service. Compound commands are split on `&&`/`;`/`|`/newlines so a send hidden after an innocuous segment is still caught. Approved sends run with the override prefixed inline (`ATELES_ALLOW_GMAIL_SEND=1 …`, or the `env VAR=1 …` form) — the override is scoped to the segment it prefixes, so it cannot vouch for a different send later in the chain. An **exported/ambient** `ATELES_ALLOW_GMAIL_SEND` is deliberately ignored: honouring it would let one `export` silently approve every send for the rest of the session, which is the carries-forward failure the gate exists to close. The inline prefix is the only approval path, re-typed per command. Segments led by a text-bearing command (`git commit`, `echo`, `grep`, `gh pr create`) carry the pattern as prose rather than invoking it and are skipped; a real invocation chained after one is still judged on its own. That allowlist must never include an interpreter — `python -c`, `node -e`, and `cat` were in an early revision and were live bypasses, since the gated command rode through as the exempt leader's argument. `.claude/hooks/test_gmail_send_gate.py` covers 51 cases, including 18 evasion vectors (wrappers, command substitution, subshells, line continuations, path and quoting variants). Fail-open (stdlib-only; any error → exit 0). Motivated by 2026-07-08 and again 2026-07-31, when a reply staged as an unsent draft was delivered to an external contact by a later `drafts update` — no send command was ever issued and the operator had given no approval. `drafts update` is not a safe staging operation: re-supplying `raw` + `threadId` can consume the draft into a sent message. To edit a staged draft, build a NEW draft rather than updating in place. See memory `feedback_gws_draft_update_can_send`.

## Standing constraints

- **Plan-mirrored docs are render targets, not source files.** `docs/taxonomy.md`, `docs/phases.md`, and `docs/architecture.md` mirror plan `ent_99ace4dd6673aa36ed08b1fe` fields (`taxonomy_markdown`, `phases_markdown`, `architecture_markdown`). Never edit these files directly: correct the plan field via `mcp__mcpsrv_neotoma__correct`, then run `python3 execution/scripts/render_plan_docs.py`; run `--check` before committing them. For an operator-approved local edit, `--push` writes the files back as plan corrections.
- **Agent prompts are always public and PII-free** (agent_policy `ent_c3c5e4a9350250cbf69e08bf`). `agent_definition.prompt_markdown` and its `.claude/skills/<name>/SKILL.md` mirror describe how an agent reasons and acts — never operator data. No payee names, IBANs, BTC addresses, contact names/emails, phone numbers, addresses, health facts, or financial figures in a prompt. Operator-specifics live in Neotoma entities (`payment_profile`, `contact`, `workout_session`, …) retrieved at runtime via the agent's `context_entity_types`. If a prompt can't be made public without leaking, move the data into an entity and reference it by type. There is one mirror flow (Neotoma → public ateles); no per-agent prompt `visibility` gating.
- **Agent prompts describe a role generically; specifics come from context entities** (agent_policy `ent_f2e21d651669c24183b2b4eb`). A prompt states what the agent *does* (role, method, protocol), not who it does it for. Operator/locale/vendor/swarm/tax peculiars are resolved at runtime from context entities, not inlined: identity from `operator_profile`, jurisdiction/timezone/currency/language from `locale_profile`, products/taglines from `product_profile`, sibling agents + AAuth subs from `swarm_roster` (by role, not hardcoded name), third-party tools from `vendor_binding` (capability slots), channels from `channel_config`, hosted-instance deploy targets from `deployment_configuration`, plus `tax_profile`/`tax_preparer`, `task_policy`, `constitution`, `payment_profile`, `brand_voice`, `calendar_routing_config`. Always give a missing-entity fallback (surface a blocker or degrade safely; never invent). Goal: any operator can fork and supply their own context entities. Renamed/removed agents leave no stale mirror — `render_agent_docs.py` prunes orphans and `--check` flags them. **This rule is not limited to prompts — it applies to any code path whose behaviour varies by operator.** `execution/scripts/session_language.py` resolves the session's spoken languages from `locale_profile` at runtime, citing this rule as its authority, and `hallucination_filter.py` consumes them rather than hardcoding a language list (ateles#707, #721); hosted-instance deploy targets live in `deployment_configuration` rather than a repo doc; a skill resolves its base URL from an entity rather than pinning a host. The test is the fork test: if another operator cloning this repo would have to edit code rather than supply an entity, the specifics are in the wrong place. Always give a missing-entity fallback that degrades safely or surfaces a blocker — never one that silently substitutes a default.
- **Never deploy a hosted client instance from memory or from a repo doc alone — retrieve its `deployment_configuration` first.** The repo doc (`docs/infrastructure/client_instance_deployment.md`) is the *generic* method and deliberately names no client; the per-instance binding (Fly app, domain, region, build args, secret names, gotchas, verified deploy command) lives in a `deployment_configuration` entity and is the canonical source. Retrieve it, run its `deploy_command` verbatim, then verify all four post-deploy checks in its `gotchas`. Rationale: on 2026-07-23 a hosted client instance was found serving a two-week-old feature-branch build because no shared source of truth existed — the knowledge lived only in one operator's private session memory and an unmerged PR. Never add client-identifying deploy config (app names, domains, codenames) to a public repo — both `neotoma` and `ateles` are public; see neotoma PR #2001, closed for that reason.
- **Never hardcode secrets, IBANs, or contact details** — always read from env or parquet.
- **Operator-specific config is env/Neotoma-sourced, never baked into code.** Operator identity (name, email), calendar IDs, recipients, and entity IDs that vary per operator must be read from env (or parquet / Neotoma) at runtime so the swarm stays portable and operator-agnostic — not literals in daemon code. This is a *sourcing* rule distinct from the public-repo PII scan (`.gitleaks.toml` deliberately allowlists the operator's own identity). Enforced by `scripts/linters/check_hardcoded_config.py` (runs in `scripts/lint.sh`); suppress a reviewed env-default with `# config-source-ok: <reason>`. **This extends past operator identity to any config the swarm reads: prefer the Neotoma entity as the single source, and never copy a set of values into code where it can drift.** Four failures of exactly this shape, all live on 2026-09-02: 8 `workflow_definition` entities carried full gate sequences while the dispatcher ran a hardcoded tuple and ignored them (ateles#719) — and the entities disagreed with *both* tuples, since `ateles|bug` and `ateles|security` declare `pm` alone, so bug issues could never hand off to build; `lib/issue_labels.py` declares `PRE_IMPL_GATE_NAMES = ("pm", "arch")` directly under a comment saying it mirrors `swarm_dispatch.PRE_IMPL_GATES`, which is `("pm", "ux", "arch")` — so `blocked/gates` can read clear while `ux` is still pending; three of eight `workflow_definition` gate sequences still named `gryllus` months after the June rename, two updated and one not; and `ASSIGNED_TO_ROUTES` holds 5 names in code against a 37-role roster, which is the dispatch cliff. When a copy is genuinely needed for import hygiene, derive it from the single source at import time or assert equality in a test — a comment claiming two constants match is not a mechanism that keeps them matching.
- **A renamed agent leaves no reference behind.** When an agent is renamed, every reference must move in the same change — a retired name in routing code sets an owner no dispatcher can resolve, and a retired name in a prompt points the operator at a command that does not exist. Real instance: `review_learning.py` filed every systemic review finding against `gryllus` for seven weeks after the 2026-06-12 rename to `cicada`. Enforced by `scripts/linters/check_agent_roster.py`, which runs as a blocking step in the `ateles package tests` workflow (and in `scripts/lint.sh`) and derives the live roster from `docs/agents/*.md` — so the one manual step per rename is adding a `RETIRED_AGENTS` entry. Rename notes ("formerly Gryllus"), rename examples ("gryllus -> cicada"), and genus names (`Bombycilla garrulus`) are allowed; suppress a reviewed exception with `# roster-ok: <reason>`. Prompts and `.claude/skills` mirrors are generated — fix a stale name with `correct()` on the `agent_definition` entity, never on disk.
- **Yoga payments: never include memo/OP_RETURN** — do not pass `memo` parameter.
- **Yoga/therapy tasks: never mark as completed** — only update `due_date`.
- **Always use Neotoma prod** (`mcp__mcpsrv_neotoma__*`), never the dev instance.
- **Google Calendar**: always use `gws` CLI with `Europe/Madrid` timezone.
- **Gmail**: always use `gws gmail ...` commands, not the Gmail MCP server.
- **Strip PII before filing issues** — scrub usernames, worktree names, platform names; use `visibility: private` for session-derived issues.
- **Never bypass the pre-commit hook with `--no-verify`.** When a commit must land with tests skipped, use the hook's own escape hatch: `SKIP_TESTS=1 SKIP_TESTS_REASON="<why>"`. `--no-verify` silently skips every check, so a self-inflicted failure arrives later looking environmental. The named reason keeps the skip attributable.

---

## Verification discipline — standing engineering rules (session-only)

**Session-only — these rules do not bind dispatched agents.** `CLAUDE.md` is loaded by interactive sessions in this checkout; Cicada/Pavo/etc. at spawn never see it. Dispatch-facing verify / refusal / dispatch-and-report duties remain on `ateles.prompt_markdown` (and code / point-of-use homes) per ateles#593 D1–D3. This section is authorized by ateles#731 as compaction-surviving session guidance, not as a #593 deliverable.

Each rule below was derived from **three or more independent failures on a single day (2026-09-02)**. They are stated here because CLAUDE.md is re-injected from disk after compaction, so they bind every *interactive session* in this repo rather than aging out of one.

Where a rule can be enforced mechanically it says so. **A rule with no enforcement is one you must apply by hand** — that is the honest posture, not a defect to paper over.

- **A mechanism that does not bind is not a control.** **Enforcement: Partial** (`check_agent_roster.py` blocking in `ateles-tests.yml`; nothing catches a new `continue-on-error`). Before treating a linter, gate, review, or status as enforcement, check that something actually fails when it is violated. Four ways this repo has produced non-binding controls: a linter registered only in `scripts/lint.sh`, which for a long time **no workflow invoked** — fixed for `check_agent_roster.py` by wiring it as a blocking step in `ateles-tests.yml`, but the trap remains for any linter added to `lint.sh` alone; a workflow step carrying `continue-on-error: true`, or a lane triggered on `pull_request` paths only and so never on push to main (both live today in `agent-config-validation.yml`); a verdict posted as an **issue comment**, which GitHub does not enforce — neotoma #2278 merged 7 seconds after a REQUEST_CHANGES, #2284 4 seconds after "Do not merge."; and a status field asserting liveness with no lease behind it — `EXECUTING` is written in `apis.py` with **zero `finally` blocks in the file**, so a killed runner pins a task permanently. When you add a control, name the thing that fails. If nothing fails, you have written documentation.

- **A write that reports success has not necessarily happened. Read it back.** **Enforcement: Partial** (`check_neotoma_rest_paths.py`; undeclared-field case **Nothing**). Neotoma `/store` accepts undeclared fields and silently routes them to `raw_fragments`: `body` and `owning_agent` are not declared on the `task` schema, so stores succeed and drop them — **four separate agents hit this in one day**. Turdus nesting its fields under a `"snapshot"` key produced 68 task and 497 `email_message` shells before #698. `record_satisfied`/`record_skipped` in `execution/daemons/anthus/participation.py` never pass the required `agent` field, and `_upsert` is documented "fire-and-forget on error" — a 4xx becomes a `log.warning` and the caller proceeds, leaving **143 rows stranded**. `publish_rendered_page` has returned success with a new token while continuing to serve the previous bytes. So: after any write that matters, retrieve it and assert the specific field you wrote is present with the value you wrote. Never treat a 2xx or `success: true` as evidence. Partially enforced by `scripts/linters/check_neotoma_rest_paths.py` (ateles#606), which catches MCP tool names used as REST routes — the 404-swallowed-as-empty-result case. The undeclared-field case has **no linter and no runtime check**; it is on you.

- **Validate the instrument before believing the measurement.** **Enforcement: Nothing** (manual). A zero, an empty result, or a silent pass is a claim about your tooling before it is a claim about the world, and a surprising number is usually the tool. Same day, same issue-resolution data, **three independent false zeros**: one resolver queried only `github_number` while other rows carry `issue_number`/`number`/`github_issue_number` (reported 0%, truth 94.8%); one read a markdown string as a dict; one keyed on a bare repo name while `repo` stores `owner/repo` (reported 0.9%, truth 96.8%). Also: a `timeout` command that **does not exist on macOS** returned empty output, read as "microphone dead"; a `pgrep -f stream_transcript.py` pattern matched a sibling agent's `test_stream_transcript.py`, and a `pkill -9` on it killed their tests; and a health counter stamping "speech" on any single 0.1s frame above threshold reported 26 stray frames across 125 silent seconds as 101.9 seconds of unanswered speech. Practice: prove the instrument non-zero on a case you know is positive before reporting a zero, and anchor process-matching patterns so they exclude `test_*` and sibling sessions.

- **A test that cannot fail on the thing it watches is decoration.** **Enforcement: Nothing, not mechanizable** (belongs in review). Before trusting a test as coverage, revert the fix and confirm the test goes red. Failures of this shape, same day: a test asserting `owning_agent == "gryllus"` — pinning the renamed-agent bug **as expected behaviour**; a Turdus test asserting only `entity_type` and `canonical_name`, the two fields that happened to survive the broken write; a replay of 15 real fabrication sites that reproduced **zero** of them, making the replay inconclusive rather than evidence; and two tests rewritten in #724 that had pinned the `operator_only` fail-open. A test written against current behaviour ratifies the bug. This is **not mechanically enforceable** — no linter distinguishes an assertion that pins a bug from one that pins a fix — so it belongs in review: for any test offered as proof of a fix, the PR should say what it looked like red.

- **Fail closed on the field that carries the safety meaning.** **Enforcement: Nothing** (manual; #724 fixes one instance, not the class). When a value is absent, unrecognized, or malformed, the default must be the *restrictive* branch, and it must be so for the field that encodes the risk. Live on main: `operator_only` appears in neither `low_blast_action_types` nor `high_blast_action_types`, so `blast_radius_for()` falls through to `blast_radius_default` = LOW — **the more confident an agent was that a task required a human, the more certainly it auto-executed** (#724 fixes this). The same fallthrough makes any *unrecognized* `action_type` LOW. `has_owner=bool(assigned_to)` in `apis.py` once accepted the string `"unassigned"` as an owner; since fixed by `SENTINEL_ASSIGNEES` in `routing.py`, which is the pattern to copy — give absence a single spelling and normalize to it. And agent subprocesses inherit the daemon's full environment: `_subscription_only_env` strips only metered API credentials, which is a **billing** control, not containment — do not read it as a sandbox. Practice: when adding a value to a safety vocabulary, add it to *both* sides of the classification, and add a test that the default branch is the restrictive one.

- **Extend the mechanism that already generalizes; do not build a parallel one.** **Enforcement: Manual** until `SWARM_PRIOR_ART_CONTRACT` / PR #686 merges. Before building, look for the thing that already does this — and search the code, not your memory of it. Three times in one day work was commissioned that already existed: `execution/scripts/hallucination_filter.py` already implemented the output-side transcription screen, with six signals **and two more that were built, measured, and rejected**, documented so they are not rediscovered; `execution/daemons/apis/dispatch_role.py` already runs any role with a `SKILL.md` through the harness router with no role allowlist, so a non-code agent is already executable by name and only the decision to run it is missing; and the gate is already PR-independent — `evaluate_gate` takes confidence and `action_type` with no PR, `write_checkpoint_brief` keys on `task_entity_id`, and the approval loop closes over Telegram, so a second approval path for non-code work would have been wasted. The converse is equally costly: ateles#341 and #700 raced on the same constant because neither checked. This includes *types* — reuse the existing relationship or entity type rather than minting one (`REFERS_TO` for task→issue edges, chosen from precedent), and keep a name that is already accurate rather than renaming for tidiness. A mechanism for this exists and is built, green, and unmerged: `SWARM_PRIOR_ART_CONTRACT` in **PR #686**, injected into every dispatched agent's system prompt. Until it lands, prior-art checking is manual — merging #686 is what converts this rule from prose into enforcement.


---

## People-data processing (RGPD legitimate-interest basis)

Neotoma's storage of third-party personal data (contacts, meeting transcripts, enrichment) for relationship management runs under **RGPD Art. 6(1)(f) legitimate interest**, NOT the household exemption — because the data drives professional action toward those people (CJEU *Lindqvist* / *Ryneš*: locally-held, unshared data still falls under the RGPD once it's used to act on people outside the household sphere). Apply these as standing discipline:

- **Minimize at capture.** When storing a person from a transcript or meeting, retain what serves the relationship (role, context, commitments, follow-ups). Do NOT persist incidental sensitive disclosures — health, finances, family situations, political/religious views (RGPD Art. 9 categories) — into durable contact profiles unless directly relevant to a stored task. Summarize, don't transcribe verbatim, when the detail is sensitive and incidental.
- **Purpose-bind.** Enrichment is for managing the operator's actual relationships. Do not build profiles on people with no relationship to the operator.
- **Honor objection.** If a person asks not to be tracked, or asks what's held, treat it as an Art. 21 objection / Art. 15 access request: stop enrichment on that entity and surface it to the operator. Never argue the person down.
- **No external publication of person-data** without the operator's explicit per-case approval (overlaps the PII-scrubbing rule for issues above).

This is the EU counterpart to the recording-disclosure guardrail in the `record_meeting` skill (US all-party-consent + Spain Art. 197). Recording calls the operator is **not** a party to is a hard refusal — it loses both the US one-party basis and the Art. 197 safe harbor.

---

## Key entity IDs

| Entity | ID |
|---|---|
| Ateles Agent Swarm Architecture plan (swarm work only — not a catch-all) | `ent_99ace4dd6673aa36ed08b1fe` |
| priority_rubric | `ent_29ca079940c1e996a8c782f2` |
| Apus webhook subscription | `ent_6ba1914462908f682f206b56` |
| update-plan skill | `ent_5d7f84290f290383e53d1a42` |
| update-tasks skill | `ent_c21f9fb84691f43f45e6cd55` |
| agent_definition: Apis | `ent_acdb65a8c5dccc1c5f6c7171` |
| agent_definition: Turdus | `ent_138a463654de2b1d46cec0db` |
| agent_definition: Anthus | `ent_887e8fd74d79eb63344df63e` |
| agent_definition: Tyto | `ent_affecbbecf52edb633c534f8` |
| agent_definition: Cicada | `ent_900b8c9589145fde47787fe5` |
| agent_definition: Vanellus | `ent_fedc0fbabef6ef203f8029c9` |
| agent_definition: Formica | `ent_d62f1df8784b7f4fcadc7d74` |
| Neotoma schema: payment_profile | `8f10fe72-2924-422c-b2ee-d537d9952576` |
| Neotoma schema: escalation | `c005dcb3-d9fb-4791-a154-fdb09ab9da12` |
| Neotoma schema: daemon_report | `a9ea8131-502f-44e7-87a6-8149bab7d55c` |
| Neotoma schema: harness_event | `689230f4-cd83-49b6-baa7-a752cf70629d` |
| Neotoma schema: execution_policy | `0e61f23f-b1bd-46a3-8824-9dde710db9e6` |
| Neotoma schema: checkpoint_brief | `b0bfcfab-1f07-4526-8fa5-d5ace343b004` |
| exec policy: Resolve #262 mirror bug | `ent_8b5f56d611bfa01b7efae973` |
| exec policy: Resolve #158 pull_request schema | `ent_76e195b7dc9b5f22432fd12c` |
| exec policy: #174/#175/#176 instructions batch | `ent_47061cdf3bf4609db806e495` |
| exec policy: FU-2026-05-004 Turn Summary widget | `ent_dd00928c59a2a73bff756325` |
| exec policy: CI security gates GHA | `ent_5002905df344d74b01de30a0` |
| exec policy: Influencer Research | `ent_3a4bbff3f1a0f17558756ec6` |
| exec policy: SEO/SERP Copy | `ent_7e32fd9ebec7907673363737` |

## Current phase blockers (Phase 5–6)

**GPG-blocked items resolved 2026-05-24** (operator pushed from Mac Studio with GPG key loaded):
- ✅ ateles: committed + pushed to origin/main
- ✅ neotoma feat/seed-pull-request-schema: branch pushed (PR pending `gh auth login`)
- ✅ neotoma fix/262-content-field-heading-entity-mode: branch pushed (PR pending)
- ✅ neotoma docs/cicada-174-175-176-instructions: branch pushed (PR pending)
- ✅ openclaw feat/neotoma-soul-override: committed (`c1e814610c`) + pushed (PR pending)
- ⚠️ openclaw main push rejected — local diverged from fork; needs `git pull --rebase origin main` or `--force-with-lease`

**Remaining manual operator steps:**
- Run `gh auth login` on Mac Studio, then open the 4 pending PRs (neotoma × 3, openclaw × 1)
- ✅ `ateles-agent` + `neotoma-agent` GitHub machine accounts created; PATs provisioned in the private env (see private notes) — unblocks Apus auto-mirror + Cicada PRs (verified 2026-06-11)
- Add `ANTHROPIC_API_KEY` secret to ateles repo settings — activates Loxia GHA
- Add `NEOTOMA_PROBE_HOSTS` secret to neotoma repo settings — activates CI security gates
- Configure neotoma main branch protection after CI gates PR merges
- Deploy separate OpenClaw instance for Menura

**Requires manual operator action**:
- Add `ANTHROPIC_API_KEY` secret to ateles GitHub repo (for Loxia GHA)
- Deploy separate OpenClaw instance for Menura

(✅ `ateles-agent` / `neotoma-agent` accounts + PATs done — see "Remaining manual operator steps" above; PAT→private-env wiring captured as `env_var_mapping` entities for `/sync-env-from-1password` (entity IDs in private notes).)

## Recently resolved

- **Secrets management — SOPS+age, snapshots in the PRIVATE `ateles-private` repo (Design B)** — 1Password Family stays canonical; values ride an age-encrypted snapshot (`ateles-private/secrets/*.sops.enc`) that daemons/CI/other machines decrypt **offline**, fixing the daemon `op read`-needs-live-session token-refresh bug. 1Password service accounts rejected (Business/Teams-only; Family suffices by storing just the age private key). **`ateles` is PUBLIC** — encrypted snapshots must NOT live here; this repo keeps only the no-secret tooling (`execution/scripts/secrets_{lib,publish,materialize}.py`, default `ATELES_SECRETS_DIR=~/repos/ateles-private`). Public repo originally held the snapshot (PR #142) → relocated to `ateles-private` (encryption held; age private key was never committed; `NEOTOMA_BEARER_TOKEN` rotation pending as precaution). age key at `~/.config/sops/age/keys.txt` + `op://Private/ateles-sops-age/key`; CI secret `SOPS_AGE_KEY` set; runbook `docs/secrets_management.md`. Same private-repo rule applies to neotoma (public product) + openclaw (public fork): their operator secrets materialize from `ateles-private`, never committed to those repos. Follow-up tasks PART_OF the plan: shared-checkout isolation (`ent_52d2317ce6ad181a8676c004`, high) and daemon redeploy label gap (`ent_22ed087bb7ef1b906cd4ad64`, med).
- **Issues sync runaway** — root cause: `ops.correct()` passed `{corrections:map}` but server expects `{field,value,idempotency_key}`; Zod rejected silently causing repeated corrections. Fix: use `ops.executeTool("correct", {field, value, idempotency_key})` in sync_issues_from_github.ts. 35 orphaned Neotoma issue entities corrected; 520+ duplicate GitHub issues closed; 30 unique open issues remain (#368–#416). Push leg disabled pending GPG commit.
- **Post-checkout hook in worktrees** — added `[ -d ".git" ]` guard before `touch .git/hooks/.hooks-installed` in scripts/git-hooks/post-checkout and .git/hooks/post-checkout to prevent failure in git worktrees.

## Swarm governance layer

`execution_policy` + `checkpoint_brief` schemas define how any plan can be swarm-executed with permission scopes, quality criteria, blocking checkpoints, and fallback instructions. Replaces binary swarm/human split with per-plan autonomy calibration. 7 execution_policy entities created (see Key entity IDs above).
