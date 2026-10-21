# Ateles MCP surface for supervised SDLC work

**Status:** proposed design, 2026-10-21. **Scope:** Ateles control plane for local sessions and Fly jobs. **Bound to:** A3, “Keep the swarm operable, supervised and affordable” (`ent_c2fa995ec1058a3e7b8b5b20`, task `ent_49496b3a8c35c2ff18e0ca1a`).

## 1. Summary and non-goals

Expose one small MCP vocabulary for reading the operator's workboard, dispatching a task, reading usage, planning and running a PR review panel, posting validated verdicts, and approving and merging a fully cleared head. A Claude, Codex or Cursor session should use the same tool shapes. The path should also work from a Fly runner once a trusted MCP transport reaches the control plane. Swarm dispatch should be the easy session default, without a Claude subagent or a token-heavy status read.

The present stdio server (`execution/mcp/ateles/server.py`, `TOOLS`, `TOOL_HANDLERS`, `main`) provides `get_dispatch_health`, `get_gate_status`, `get_swarm_roster`, `get_task_timeline`, `list_checkpoints`, `list_pipeline_queue`, `resolve_checkpoint`, `route_task` and `watch_swarm`. Its README calls `resolve_checkpoint` the only mutating tool. The new surface adds *bounded, named actions*; it does not add a generic shell, GitHub, gate-write or Neotoma-write tool. Existing read-only handlers remain read-only.

The design replaces these manual operator-session steps:

| Observed step | Tool or change |
|---|---|
| `harness_usage.py refresh`, export `APIS_USAGE_GATED_PROVIDERS=claude`, then `dispatch_role.py --role cicada --provider codex --action-class build --cwd <worktree> --task-file <brief> --json` | `dispatch_task` (with build inputs), then `dispatch_status`; provider policy lives in the service environment, never in a session export |
| Scratch scripts run `harness_lens_runner.py` two lenses at a time with `--action-class lens_review:<lens>`, inspect verdicts, then post them | `review_plan`, `run_panel`, `panel_status`, `post_verdicts` |
| Dry-run `approve_pr_as_app.py`, then `gh pr merge --squash --match-head-commit` with an App token, after manually checking the pipeline queue | `approve_and_merge` |
| Read a full checkpoint queue, or search a task timeline for a runner without a task link | `get_workboard` plus filtered `list_checkpoints`; mandatory task link on new dispatches and `get_task_timeline` join |

Non-goals: replacing GitHub CI, reimplementing `dispatch_role.py` or `skill_runner.py`, creating a general workflow engine, adding provider credentials to MCP arguments, activating Claude, bypassing a PR review or consent gate, or moving the Apis daemon fleet to Fly. `docs/cloud_hosting.md` identifies the Fly CLI job path and the distinct PR-opener App; it does not say that every live instance has been deployed or that delivery routing is enabled. The Fly delivery work bound to `ent_707130a714f113ebf525d66b` remains the critical path.

## 2. Principles and common contract

1. **Small results.** Default responses contain IDs, state, counts, the pinned head and a `detail_ref`; `verbose: true` adds bounded diagnostics, never a full prompt, agent definition, command output, raw verdict or secret. This corrects the token-heavy read pattern seen in Ateles and Neotoma MCP results. `detail_ref` is a stable opaque handle for an authorized detail read, not an embedded file path or bearer token.
2. **Authority is explicit.** Tool availability is not permission. The server checks the caller principal, task/PR scope, live policy, checkpoint status and action consent at execution time. A client-supplied `consent_checkpoint_id` is a reference to a resolved, action-bound authorization; a boolean `approved: true` is never authority. Existing `resolve_checkpoint` alone accepts body-bound `resolver_aauth_headers` (`server.py`, `_authenticate_checkpoint_resolver`); the server never holds the operator's resolver key.
3. **No credential in an input or output.** No token, private key, raw signed header, environment block, or credential path appears in a new schema or result. App tokens are minted server-side for one repository and action. Errors are redacted to codes and safe hints. A `detail_ref` is authorized on dereference.
4. **Idempotent and head-pinned actions.** A stable `idempotency_key` plus canonicalized scope identifies a build, panel, publication or merge request. Replay returns the same dispatch/result; reuse with different parameters refuses. PR actions take a full 40-hex `expected_head`; they re-fetch before every external write and fail if it moved. A carried review records both the original verdict head and the current head.
5. **Long work is asynchronous.** `dispatch_task` and `run_panel` return a durable ID immediately. A supervised worker owns execution; a stdio client disconnect must not kill it or leave a fake `running` state. Record start/end and process or Fly run identity in the existing task-linked event and runner records, recover interrupted jobs as `unknown_outcome`, and never automatically retry a write-capable job with an unknown outcome. Reads use the same record from local or Fly execution.
6. **One review plan.** `review_plan`, the daemon's review dispatch and the approval gate consume one planner built from `review_panel.select_panel`, `review_carry.select_rerun` and `review_delta`. The plan states required, fresh, carried and re-run lenses with reasons; an unreadable interdiff carries nothing. Approval recomputes the plan at the live head rather than trusting a prior MCP response. `approve_pr_as_app.py` already derives the required floor, carries earlier sign-offs and verifies checks; the new planner must converge those paths instead of adding a third ruleset.
7. **No shadow state.** A workboard is a read projection of Neotoma tasks, their planning ascent, leases, activity, checkpoints, events and artifact links. Dispatch writes the task and its existing execution records; the overlay, MCP response and any protocol task add no independent work status. If a join fails, name the gap. Render both card and text fallback on the server from one structured result, with identical content and ordering.

### Relation to the 2026-10-15 ruling

The early slice of the Ateles-in-harnesses plan (`ent_f56cffadfc8c0a0303b368de`) is `get_workboard`, `dispatch_task`, their task/event join, and a compact `dispatch_status`. `get_workboard` applies the P1 evidence rules while P1 repairs continue: “working now” requires a held lease and recent activity; “awaiting operator” requires a real open checkpoint read back from the record. Dispatch creates a task under its plan, routes it through the existing router and local or eligible Fly runner, and links its result and PR through task-linked evidence. The early view reads only; it creates no workboard store. This slice moves ahead of the plan's phase order under decision `early_slice_workboard_and_dispatch_2026_10_15`, without moving merge, release, deployment, credential or grant authorization.

The wider surface here—`run_panel`, `panel_status`, `post_verdicts`, `approve_and_merge`, `usage_status`, `review_plan` and `pr_status`—is phased **after** that early slice. `usage_refresh` and the detailed review and delivery views follow with those tools. Neotoma owns generic context, provenance and activity UI; Ateles supplies workboard, checkpoint, delegation, review and delivery overlays on the generic Neotoma MCP App and its events/actions. The Neotoma-in-harnesses umbrella (`ent_bccc013e81358f4f2a83ad7c`) does not make an MCP App repair a disconnected connector. Claude Code mods are delivery layers, never the authoritative record.

### Wire rules

Every example below is the exact proposed JSON shape. All fields shown are required unless explicitly described as optional; `verbose` defaults to `false`; unknown fields are refused. IDs are opaque strings, `repo` is `owner/name`, `pr` is a positive integer, times are UTC ISO-8601, and `expected_head` is a full SHA. An ordinary failure has this common output shape in place of a success result:

```json
{"ok":false,"code":"HEAD_MOVED","message":"PR head changed; read pr_status and retry against the new head","retryable":false,"detail_ref":"err_01"}
```

The server must not echo input briefs, command stdout, exception strings or HTTP response bodies in that envelope. Detail reads are separately authorized and size capped. A read failure uses `UNKNOWN`, never an empty queue, clear verdict, zero usage or successful merge.

## 3. Tool contracts

**Classification:** READ-ONLY means no GitHub or Neotoma decision write; a usage refresh may update the local usage cache and a status read may emit ordinary telemetry. CONSENT-GATED means an admitted task or action-bound checkpoint must authorize the effect; it need not require a fresh human prompt when standing authority already covers the action. OPERATOR-SIGNED means the operator's body-bound AAuth proof is required, as on existing `resolve_checkpoint`. The new tools take no signing material; they may consume only the read-back result of an operator-signed checkpoint. No tool can forge one.

### `get_workboard` — READ-ONLY

Project tasks from the bound operator's Neotoma instance, their `PART_OF` plan ascent, held lease plus recent activity, open checkpoint read-back, last task-linked event, linked PR and runner output pointer. No workboard entity or status cache is written. A task is `working_now` only when both lease and activity are observed; an unresolved checkpoint makes it `awaiting_operator` only when the checkpoint is open on read-back. `done` requires a verifiable result artifact; otherwise use `held` (including queued, blocked or stale work) or `unknown` when the relevant source is unreadable. Do not treat a large checkpoint queue as a workboard. Default to a small page, include real counts and a cursor, and name missing joins per task rather than inventing state.

```json
{"plan_entity_id":"ent_plan","session_id":null,"states":["working_now","awaiting_operator","held","done"],"limit":20,"cursor":null,"verbose":false}
```

```json
{"ok":true,"as_of":"2026-10-21T12:00:00Z","counts":{"working_now":1,"awaiting_operator":0,"held":0,"done":0,"unknown":0},"tasks":[{"task_entity_id":"ent_task","title":"Build the issue","plan_ascent":[{"entity_id":"ent_plan","entity_type":"plan","title":"Ateles in harnesses"}],"task_status":"in_progress","work_state":"working_now","lease":{"held":true,"holder":"cicada"},"last_activity_at":"2026-10-21T11:59:00Z","open_checkpoint_ids":[],"last_event":{"event_id":"ent_event","at":"2026-10-21T11:59:00Z","state":"running"},"linked_pr":null,"runner_output_ref":"result:disp_01","gaps":[]}],"next_cursor":null,"detail_ref":"workboard:ent_plan"}
```

`plan_entity_id` is required; `session_id`, `states`, `cursor` and `verbose` are optional (the shown `null`s mean no filter). A `session_id` filter reads the task's recorded origin-session link; it never infers membership from a runner timestamp. The server validates the planning ascent and source binding for every task. `counts` covers the filtered result before paging. `gaps` uses safe codes such as `PLAN_ASCENT_MISSING`, `SESSION_LINK_MISSING`, `LEASE_UNREADABLE`, `ACTIVITY_MISSING`, `CHECKPOINT_UNREADABLE`, `PR_LINK_MISSING` and `RUNNER_OUTPUT_UNLINKED`; unavailable data becomes `null` and a gap, never fabricated completion or approval. Refuse an unreadable source with `SOURCE_UNREADABLE` when the projection cannot be trusted.

### `dispatch_task` — CONSENT-GATED

Create one task under the validated plan, assign its executor, record the optional origin session, then call the existing `route_task` policy and `execution/daemons/apis/dispatch_role.py` with the **new task entity ID**. Read back the task, its `PART_OF` link, executor, origin-session link when supplied, and start event before returning `queued`; link the eventual result and PR through task/event relationships. An idempotent replay finds the same task and dispatch. The `brief` is stored on the task under the record's declared task fields, not in an MCP job registry. A caller may provide an already admitted `task_entity_id` only for an explicit resume of that same task; default dispatch creates one. Validate plan binding, role, action gate and runner source before launch. Use `dispatch_role.py` `dispatch` or its `main --json` adapter with `--task-entity-id`, selected `--action-class`, `--cwd` for local builds, and service-configured provider policy; never construct shell text. Refresh local usage before selection and return a specific usage failure. Pass `--github-delivery` only when task authority and a scoped PR-opener identity allow it. Fly follows the existing runner eligibility and dollar cap; a local worktree path is invalid there.

```json
{"plan_entity_id":"ent_plan","session_id":"ent_session","repo":"markmhendrickson/ateles","worktree_path":"/Users/worker/repos/ateles-wt-issue","brief":"Implement the admitted issue and report the result.","role":"cicada","action_class":"build","runner":"local","idempotency_key":"dispatch:ent_plan:issue-1"}
```

```json
{"ok":true,"dispatch_id":"disp_01","task_entity_id":"ent_task","plan_entity_id":"ent_plan","session_id":"ent_session","state":"queued","runner":"local","provider":"codex","detail_ref":"disp_01"}
```

`session_id` is optional only for dispatch outside an interactive session; when supplied, the server records a declared origin-session relationship and includes it in the idempotency scope. `runner` is optional (`local`/`fly`; service policy decides when omitted); `provider` is an optional named override subject to active-provider policy, never an ambient fallback. For `runner:"fly"`, omit `worktree_path` and add `source_ref` (a full admitted commit SHA); a local build requires `worktree_path`. Other action classes take their registered runner inputs, without widening this tool into a shell. Refuse `PLAN_UNREADABLE`, `TASK_CREATE_UNVERIFIED`, `TASK_NOT_ADMITTED`, `WORKTREE_DIRTY_OR_SHARED`, `SOURCE_UNVERIFIED`, `USAGE_UNKNOWN`, `PROVIDER_INACTIVE`, `CAPACITY_EXHAUSTED` or `POLICY_HOLD`; none may launch a worker. If task creation succeeds but launch fails, leave the task linked and held with an explicit failure event, so the operator sees it. `skill_runner._write_harness_event` and `dispatch_role.py --task-entity-id` already support task links; make that input mandatory and verify the written event.

### `dispatch_status` — READ-ONLY

Joins the task with its task-linked harness events and a Fly result when applicable (`server.py`, `_get_task_timeline`, `_is_runner_event`; `skill_runner._write_harness_event`). The loaded-definition line is a safe status such as `role=cicada definition=loaded`, never `prompt_markdown` or `aauth_sub`. Cost is measured from runner evidence when available, otherwise `null` with a named gap.

```json
{"dispatch_id":"disp_01","verbose":false}
```

```json
{"ok":true,"dispatch_id":"disp_01","task_entity_id":"ent_task","state":"running","loaded_definition":"role=cicada definition=loaded","provider":"codex","model_tier":"top","cost_usd":null,"result_summary":null,"linked_pr":null,"runner_output_ref":"result:disp_01","gaps":["COST_UNREPORTED"],"detail_ref":"disp_01"}
```

States: `queued`, `running`, `succeeded`, `failed`, `refused`, `unknown_outcome`. A missing run row yields `RUN_UNOBSERVED`, never inferred success. `verbose` may add safe timestamps, return code, attempted providers and Fly machine state. A stale task link is an error. When a PR or result is linked, report its ID and pointer; do not expand output by default.

### `usage_status` — READ-ONLY; `usage_refresh` — READ-ONLY external effect, local cache write

Wrap `harness_router.configured_providers`, `usage_gate`, `live_headroom`, `persisted_cooling`, and the safe view in `execution/scripts/harness_usage.py` `_gate_view`. Status shows reading age, gating, cooldown and reason codes; unknown is distinct from zero. Refresh invokes `usage_probe.refresh_usage_if_stale` under the service's configured login, not the caller's environment. The Codex probe must select a known supported probe model rather than the machine's rejected ambient default (`usage_probe.provider_probe_command`); failure preserves the last reading with `stale:true` and an explicit probe code. Honor `APIS_USAGE_GATED_PROVIDERS=claude` from service configuration, never a tool argument. The current local `usage_gate` threshold defaults to 1,800 seconds in `harness_usage.py`; Fly's distinct login and budget rules in `docs/cloud_hosting.md` are separate.

```json
{"provider":"codex","verbose":false}
```

```json
{"ok":true,"provider":"codex","headroom":0.72,"observed_at":"2026-10-21T12:00:00Z","age_seconds":120,"stale":false,"dispatch_allowed":true,"gate_code":"AVAILABLE","cooldown_until":null,"detail_ref":"usage:codex"}
```

```json
{"provider":"codex","force":false,"idempotency_key":"usage:codex:2026-10-21T12:00Z"}
```

```json
{"ok":true,"provider":"codex","refresh_state":"refreshed","observed_at":"2026-10-21T12:02:00Z","dispatch_allowed":true,"detail_ref":"usage:codex"}
```

`force` is optional and may bypass freshness, not cooldown or provider activation. A refresh is bounded and deduplicated by provider lock; return `PROBE_TIMEOUT`, `PROBE_MODEL_UNSUPPORTED`, `AUTH_UNAVAILABLE`, `READING_STALE` or `PROVIDER_INACTIVE` without raw CLI output. A 20–40 second refresh may be handled as an async operation if the MCP caller deadline is shorter; the response then has `refresh_state:"queued"`, with `detail_ref` polling the same status contract.

### `review_plan` — READ-ONLY

Replaces hand selection of five lenses each round. Resolve the PR's linked issue, diff-derived floor and bootstrap rule with `review_panel.select_panel` and `approve_pr_as_app.resolve_lenses`; fetch trusted, head-marked verdicts and interdiffs with `swarm_dispatch.lens_records`, `review_carry.candidate_heads`, `fetch_interdiff`, `select_rerun`. The dispatcher (`swarm_dispatch._rereview_selection`) and approval gate (`approve_pr_as_app.carry_earlier_signoffs`) must call the same shared planner. Preserve the required floor and always re-run a security lens on a security-surface delta.

```json
{"repo":"markmhendrickson/ateles","pr":1475,"expected_head":"0123456789abcdef0123456789abcdef01234567","verbose":false}
```

```json
{"ok":true,"repo":"markmhendrickson/ateles","pr":1475,"head":"0123456789abcdef0123456789abcdef01234567","plan_id":"rplan_01","required":["pm","arch","ux","qa","security"],"run":[{"lens":"qa","reason":"fix touched tests"}],"carried":[{"lens":"pm","from_head":"fedcba9876543210fedcba9876543210fedcba98","reason":"area untouched"}],"blocked":[],"detail_ref":"rplan_01"}
```

The sample arrays illustrate shape, not a completed five-lens verdict set. `plan_id` binds the repo, PR, head, policy version and interdiff evidence. Refuse `HEAD_MOVED`, `DIFF_UNREADABLE`, `VERDICT_UNTRUSTED` or `PANEL_EMPTY`; none can produce a carry. `verbose` adds bounded per-lens rationale and source IDs.

### `run_panel` — CONSENT-GATED

Starts only `review_plan.run` lenses, at most two Codex lens processes concurrently, using `execution/scripts/harness_lens_runner.py` `LensTarget`, `run_one`, `validate_verdict` with `post=False` and `action_class="lens_review:<lens>"`. Pin every worktree and verdict to `expected_head`. Fix its `Worktree.create`/`_fetch_origin_serialized` repository-root assumption before claiming cross-repo support: resolve the trusted checkout from a registered repo mapping, not `~/repos/<repo_name>`; test Theodore and Ateles independently. A panel run never posts to GitHub. The existing lens sandbox denies network/loopback, so integration acceptance runs in a separate, constrained process from the same clean head, without credentials.

`acceptance_command` is optional only when the repo has no declared acceptance command and the review plan does not require one. It is a repo-relative tracked executable path, with no shell, arguments or arbitrary command text; the server verifies it matches the repo's allowlisted review-acceptance declaration *at the pinned head*. Theodore plans `tools/review_acceptance.sh` (Theodore issue 292). The command runs once outside the lens sandbox in an isolated clean worktree from `expected_head`, under an allowlisted environment with no credential files or keys, bounded time, output size and network policy. It emits a small machine-readable report: suites run/skipped, pass/fail counts and reason codes. If a required approval, delivery or first-draft suite cannot run, the panel is `incomplete`, not clear. Attach the command path, head, exit code, counts and skipped suites to every affected verdict's evidence before validation; the raw log remains in a protected detail artifact. A failing exit cannot be described as a pass even when the lens prose says “approved”.

```json
{"repo":"markmhendrickson/theodore","pr":292,"expected_head":"0123456789abcdef0123456789abcdef01234567","task_entity_id":"ent_review_task","plan_id":"rplan_02","acceptance_command":"tools/review_acceptance.sh","idempotency_key":"panel:theodore:292:0123456"}
```

```json
{"ok":true,"panel_run_id":"panel_02","head":"0123456789abcdef0123456789abcdef01234567","state":"queued","lenses":["pm","arch","qa"],"acceptance_state":"queued","detail_ref":"panel_02"}
```

Refuse `HEAD_MOVED`, `PLAN_STALE`, `COMMAND_NOT_DECLARED`, `COMMAND_NOT_TRACKED`, `WORKTREE_UNCLEAN`, `SANDBOX_UNBOUND`, `USAGE_UNKNOWN` or `TASK_NOT_ADMITTED`. A subprocess failure records a per-lens failed state; it does not silently drop the lens. The repo path in the sample is a target shape, not a claim that Theodore's script already exists.

### `panel_status` — READ-ONLY

Read durable run state plus trusted head-marked verdict records (`harness_lens_runner.validate_verdict`, `approve_pr_as_app.evaluate_lens`, `review_carry.select_rerun`). Distinguish fresh from carried and missing; report acceptance evidence by count.

```json
{"panel_run_id":"panel_02","verbose":false}
```

```json
{"ok":true,"panel_run_id":"panel_02","head":"0123456789abcdef0123456789abcdef01234567","state":"complete","fresh":[{"lens":"qa","verdict":"clear","verdict_ref":"verdict_qa_02"}],"carried":[{"lens":"pm","from_head":"fedcba9876543210fedcba9876543210fedcba98"}],"missing":[],"acceptance":{"command":"tools/review_acceptance.sh","head":"0123456789abcdef0123456789abcdef01234567","exit_code":0,"passed":18,"failed":0,"skipped_suites":[]},"detail_ref":"panel_02"}
```

`PANEL_UNKNOWN`, `VERDICT_UNTRUSTED`, `ACCEPTANCE_INCOMPLETE` and `HEAD_MOVED` are explicit. `verbose` can add bounded reason codes and source references, not full verdict prose.

### `post_verdicts` — CONSENT-GATED

Publishes only validated fresh verdicts from a completed `panel_run_id`, with an action-bound authorization for external GitHub writes. `harness_lens_runner.post_verdict` already edits the same lens comment and reads it back, but currently calls ambient `gh`; replace its authentication boundary with a scoped swarm App client before exposing this tool. Pin and recheck the head before *each* comment, reject a changed head or an untrusted verdict, and verify the posted body/author by read-back. Carried verdicts are evidence for approval, not newly posted comments.

```json
{"repo":"markmhendrickson/ateles","pr":1475,"expected_head":"0123456789abcdef0123456789abcdef01234567","panel_run_id":"panel_02","consent_checkpoint_id":"ent_checkpoint","idempotency_key":"post:panel_02"}
```

```json
{"ok":true,"head":"0123456789abcdef0123456789abcdef01234567","posted":[{"lens":"qa","comment_id":123456,"url":"https://github.com/markmhendrickson/ateles/issues/1475#issuecomment-123456"}],"skipped_existing":[],"detail_ref":"panel_02"}
```

`consent_checkpoint_id` is optional only if a live standing authorization explicitly covers this exact PR/head/publication action; the server records which authority applied. Refuse `CONSENT_REQUIRED`, `HEAD_MOVED`, `VERDICT_UNVALIDATED`, `APP_IDENTITY_UNAVAILABLE`, `POST_READBACK_FAILED` or `PARTIAL_PUBLICATION`. On partial publication, return posted IDs and do not retry already verified comments blindly.

### `pr_status` — READ-ONLY

One small status view of PR head, required CI, mergeability, trusted verdicts, open checkpoints and the queue (`approve_pr_as_app.evaluate_checks`, `evaluate_lens`; `server.py` `_list_pipeline_queue`, `_list_checkpoints`). It must identify pending, skipped, failed and unknown checks separately and never turn an unreadable query into green. Checkpoints are filtered to the PR's bound task/issue, not the entire operator queue.

```json
{"repo":"markmhendrickson/ateles","pr":1475,"verbose":false}
```

```json
{"ok":true,"repo":"markmhendrickson/ateles","pr":1475,"head":"0123456789abcdef0123456789abcdef01234567","ci":{"required_green":true,"pending":0,"failed":0,"unknown":0},"mergeable":"CLEAN","verdict":{"required_clear":5,"required_total":5,"live_blockers":0},"open_checkpoint_ids":[],"pipeline_queue":"idle","detail_ref":"pr:1475:0123456"}
```

Refuse or report `unknown` for GitHub read failure, untrusted author, stale head, missing required check and checkpoint read failure. `verbose` gives bounded check names, lens reasons and checkpoint IDs.

### `approve_and_merge` — CONSENT-GATED

Replace manual `approve_pr_as_app.py` dry-run/apply and `gh pr merge`. Require an action-bound consent checkpoint or an existing standing merge authorization that names the repo/PR class. Recompute the shared review plan, validate every required lens at this head or a warranted carry, reject every live blocking finding, require CI green, open PR, clean merge state, and idle `list_pipeline_queue` (a merge restarts the live dispatcher). Refuse while a review run for the head is in flight. Use `approve_pr_as_app.run`/`submit_approvals` for panel-gated App approval, then re-read head, CI, queue and blocking findings. Use a scoped merge-capable App token for a squash merge with GitHub's `--match-head-commit <expected_head>` equivalent; do not delete the branch. Read back the merged PR, its merge SHA and the queue/restart state. Approval and merge are separate recorded steps: a merge failure after an approval returns `approved_not_merged`, never “merged”.

```json
{"repo":"markmhendrickson/ateles","pr":1475,"expected_head":"0123456789abcdef0123456789abcdef01234567","consent_checkpoint_id":"ent_checkpoint","idempotency_key":"merge:ateles:1475:0123456"}
```

```json
{"ok":true,"state":"merged","repo":"markmhendrickson/ateles","pr":1475,"head":"0123456789abcdef0123456789abcdef01234567","approval_review_ids":[98765],"merge_commit":"fedcba9876543210fedcba9876543210fedcba98","dispatcher_restart":"expected; verify get_dispatch_health and list_pipeline_queue","detail_ref":"merge:1475:0123456"}
```

Refuse `CONSENT_REQUIRED`, `PANEL_INCOMPLETE`, `LIVE_BLOCKER`, `CI_NOT_GREEN`, `REVIEW_IN_FLIGHT`, `QUEUE_BUSY`, `MERGE_STATE_UNKNOWN`, `HEAD_MOVED`, `APP_IDENTITY_UNAVAILABLE` or `MERGE_READBACK_FAILED`. A queue read failure is not idle. An idempotent replay of a verified merge returns the same SHA; an unverified GitHub outcome is `unknown_outcome` and requires reconciliation before retry.

### Changes to existing tools

`list_checkpoints` (`server.py`, `_list_checkpoints`) gains `task_entity_id`, `created_since`, `sort:"newest"` and `verbose:false` input fields, with task + date filters applied by the Neotoma query before paging. Default to newest first; retain cursor, real `total`, `returned` and `truncated`. A new session can see its own checkpoint among approximately 1,590 pending without scanning oldest-first. Return a filter-specific count and a safe checkpoint summary; do not return full proposed alternatives by default. Missing or unsupported filter must fail, never silently fall back to the global queue.

`route_task` (`server.py`, `_route_task`) gains an explicit `action_class` before keyword matching. `build` routes to code/cicada and `lens_review:<lens>` resolves the registered lens agent; only otherwise use text keywords. This fixes a review of email-delivery code landing with email triage. Preserve `action_type` for blast-radius classification; return `matched_via:"action_class"` and refuse unknown classes instead of guessing. The route preview remains read-only and does not claim or dispatch a task.

`get_task_timeline` (`server.py`, `_TimelineBuilder.join_harness_events`, `_is_runner_event`) joins `dispatch_id` to task-linked `harness_event` and Fly result rows. It reports a dispatch as `unlinked` or `unknown_outcome` when evidence is missing, not as running. Existing historical events without task links remain a stated gap; new `dispatch_task` and `run_panel` require the link and verify the written event. `watch_swarm` can expose the same IDs without expanding full event payloads.

## 4. Consent, identity and transport

| Effect | Identity and check |
|---|---|
| Usage read/refresh and PR/task reads | MCP caller is authenticated for the operator's instance; service reads with its existing scoped credentials. No credential is echoed. |
| Build execution | Cicada's own Neotoma agent identity for task/events; local subscription login or Fly's per-provider login; repository delivery uses the separate, repository-scoped PR-opener App (`docs/cloud_hosting.md`, `lib/github_app_token.py`). |
| Lens inference and verdict publication | Lens agent identity for review evidence; publication uses the authorized swarm App, not ambient `gh` identity or the PR-opener App (`harness_lens_runner.post_verdict` needs this boundary). |
| Formal approval | `approve_pr_as_app.py` uses each lens App where configured or the reviewer App, checks that it differs from the PR author, pins the review to the head and reads it back. |
| Merge | A separately scoped merge-capable App identity, checked against the actual GitHub user before the write. Do not assume the reviewer or PR-opener App has merge authority. **UNVERIFIED:** exact installed App grant for merging on each target repo. |
| Checkpoint decision | Existing `resolve_checkpoint` takes operator-signed, body-bound AAuth proof. New tools accept only a checkpoint ID and verify its immutable read-back; no resolver key or signed headers are stored by the Ateles server. |

The existing MCP transport is stdio (`server.py`, `main`). “Any session/Fly runner” requires a trusted endpoint or local stdio adapter with caller authentication, per-repo authorization and job-state access; merely exposing the stdio process on a network socket would expand authority. Keep the control plane on the dispatcher host; Fly jobs receive only their declared job identity and can report results through the current Fly path. **UNVERIFIED:** a deployed, authenticated remote MCP transport for Fly. Do not advertise remote availability until an end-to-end probe succeeds.

## 5. Error handling and result storage

Use the existing Neotoma task, harness-event, lease, checkpoint and artifact relationships as the durable dispatch record; add declared fields to those source entities only where their current schemas cannot carry a required ID, idempotency key, runner identity or result reference. Do not add a separate job registry or workboard entity. Store the brief in the task's declared description field; never store a prompt, token or raw stdout in a status row. Large artifacts are separately access-controlled and bounded. Mark `running` only while a held worker lease and recent activity exist; on service restart reconcile process/Fly evidence, otherwise surface `unknown_outcome`. Schema changes require a declared-field and read-back test before use.

Every external write records its authority reference and read-back evidence. A retry checks live head and existing artifact before acting. Distinguish `not_found`, `unreachable`, `unauthorized`, `stale`, `refused`, `failed` and `unknown_outcome`; failures never masquerade as empty results. Redact subprocess stderr and HTTP bodies before logs or MCP output. Existing `server.py` `_request` records transport errors; new callers must not pass raw exception text through to the model. A failed usage probe cannot overwrite a valid observed window with zero headroom or a misleading “limit reached” reason.

## 6. Phased implementation

Build the thin workboard and dispatch slice first, ahead of the remaining phase order; continue P1 record repairs such as ateles#1402 in parallel and expose the gaps they leave. The later review and publication steps follow only after that slice. While other PRs touching `execution/daemons/apis/skill_runner.py` or `dispatch_role.py` are open, leave those files alone; this is a time-bound integration constraint, not a permanent freeze. Use their existing task-ID and CLI contracts first. Keep the Fly delivery path and its PR/credential grants ahead of the later convenience surface; none of these PRs should block its review or activation.

1. **Early workboard and dispatch.** Add `get_workboard`, `dispatch_task` and compact `dispatch_status` in `execution/mcp/ateles/server.py`, `README.md` and `test_server.py`; improve `route_task` and `get_task_timeline` joins. Render the Neotoma MCP App card and structured-text fallback from one server-side projection. Task creation, plan ascent, executor assignment, start event and eventual result/PR linkage read back from the existing record; no separate view store. Test the held-lease plus activity and open-checkpoint predicates, missing links, replay, task creation without launch on failure, local/Fly inputs, and low-token default results. Exercise a real design dispatch on an eligible Fly runner when its transport and grants are verified; if not, report the missing condition without claiming the Fly test passed.
2. **Later read spine and planner.** Add compact `usage_status`, `usage_refresh`, `review_plan`, `pr_status`; improve filtered `list_checkpoints`. Touch `execution/mcp/ateles/server.py`, `README.md`, `test_server.py`, and a shared planner module under `execution/daemons/apis/` used by `swarm_dispatch.py` and `execution/scripts/approve_pr_as_app.py`. Touch `usage_probe.py` only for a supported Codex probe model and safe failure code. Test newest task-filtered checkpoint query, old-head carry refusal, unreadable CI/queue, usage stale/failed-probe behavior and planner parity across all three callers.
3. **Panel execution and acceptance.** Add `run_panel` and `panel_status` in `execution/mcp/ateles/`; adapt `execution/scripts/harness_lens_runner.py` `Worktree.create` and `run_one` for a trusted repository mapping and clean head; add the credential-free acceptance runner and report parser. Require task ID and verify harness-event read-back through existing `dispatch_role.py`/`skill_runner.py` interfaces. Exercise Ateles and Theodore repo fixtures, an unsupported acceptance command, missing suites, Codex 429 concurrency, client disconnect and Fly/local identity distinctions. Do not implement a competing Fly delivery route.
4. **Publication and merge.** Add `post_verdicts` and `approve_and_merge` with scoped App clients, consent read-back, idempotency, queue/CI/panel checks and head-matched squash merge. Touch `execution/mcp/ateles/`, `execution/scripts/harness_lens_runner.py` publication adapter, `execution/scripts/approve_pr_as_app.py` only to expose the shared gate cleanly, their tests and the MCP README. Evaluate head movement before each write, reviewer/author separation, queue becoming busy, partial publication, approval without merge and merge read-back. Verify the dispatcher resumes after merge; do not claim a restarted dispatcher from merge success alone.

Each PR updates docs and generated catalogs only when their source files change, runs targeted tests and the matching repository validation, and avoids writing credentials into fixtures. Contract changes to existing tools preserve current callers or version the changed behavior explicitly. The fourth PR starts only after the earlier contracts and the Fly delivery path's relevant grant/runner state are checked.

## 7. Tests and evaluations

Add `execution/evals/mcp_sdlc_surface/` with scenario fixtures and a harness that calls the real `server.py` MCP list/call handlers, stubbing only external Neotoma, GitHub, provider and process boundaries. Follow the planted-red style used by `execution/evals/dispatch_gate/` and `execution/evals/build_delivery/`: for every claimed guard, remove or invert it in a controlled fixture and prove the eval goes red. These are effect tests, not JSON-schema-only checks.

| Tool | Observable pass and planted red |
|---|---|
| `get_workboard` | A held lease plus recent activity is working; a real open checkpoint read-back is awaiting; missing evidence becomes a named gap. Disabling each predicate makes the eval fail. |
| `dispatch_task` | One new plan-linked task, one task-linked event and one run on replay; wrong plan, dirty/shared worktree, stale usage or missing definition starts zero workers. Result and PR resolve from the task. |
| `dispatch_status` | Correct loaded-definition status and end result; missing event yields `RUN_UNOBSERVED`, killed worker `unknown_outcome`. |
| `usage_status`, `usage_refresh` | Real gate view changes after a successful probe; rejected default model, stale probe and timeout never become false exhaustion or a fresh reading. |
| `review_plan` | Dispatcher, session and approval gate select the same lenses on one head; touched/security or unreadable delta cannot carry a clear. |
| `run_panel` | Actual clean-head acceptance command contributes exit/count/skips to verdict evidence; omitted approval-path suite or command failure prevents clear; cross-repo worktree resolves the intended repo; third Codex review waits. |
| `panel_status` | Fresh, carried, missing and acceptance state agree with durable evidence; forged/old-head verdict is not clear. |
| `post_verdicts` | App-authored, head-pinned comment reads back once; wrong identity, moved head and replay cannot duplicate or publish. |
| `approve_and_merge` | Approved current-head PR squash-merges exactly once after green CI, clear panel and idle queue; each planted red prevents both approval and merge, or reports `approved_not_merged` accurately. |
| `pr_status` | CI, mergeability, verdict and checkpoint counts reflect a planted live PR; failed GitHub/Neotoma read reports unknown, not green/zero. |
| `list_checkpoints`, `route_task`, `get_task_timeline` | Task/date filter finds a newly created checkpoint amid old pages; email-delivery code review routes by action class; dispatch event joins by task ID, while an unlinked event is named as a gap. |

For the acceptance runner, test the exact executable at the exact SHA with a synthetic credential file denied, a planted loopback/network requirement declared, and deterministic skip counts. Run the eval once with its enforcement disabled to confirm it fails. For publication and merge, verify the remote artifact by read-back, not by subprocess exit status. CI remains the authoritative repository suite; a sandboxed lens process is not expected to run networked integration tests.

## 8. Risks and operator questions

1. **Remote MCP transport.** Options: authenticated control-plane endpoint for remote callers, or local stdio only until the Fly runner can use a trusted adapter. Recommend local stdio first and a separately reviewed authenticated bridge; this keeps Fly delivery work on its critical path. Until proven, remote tool use is **UNVERIFIED**.
2. **Acceptance command declaration.** Options: a tracked repo policy file naming one executable, or a central allowlist. Recommend a tracked repo declaration plus central admission of allowed repositories; it binds the command to the reviewed head and prevents arbitrary shell through MCP. Theodore's `tools/review_acceptance.sh` remains pending its issue 292; its exact report format is **UNVERIFIED** and should be fixed with that issue.
3. **Merge identity and consent record.** Options: grant a dedicated merge App repository-scoped permission, or reuse an existing App only if its grant and separation from author/reviewer are verified. Recommend the dedicated identity where the current grants do not suffice; do not create a grant as a side effect of these tools. The existing App's merge permission is **UNVERIFIED**.
4. **Execution evidence completeness.** The no-shadow-state decision rules out a second job or workboard registry. Verify that task, harness-event, lease and Fly-result records can carry idempotency, dispatch ID, source/head and result/PR links; any missing declared field is a P1 repair. Until those joins and their read-back work across hosts, show a named gap or `unknown_outcome`, not a fabricated durable status.
5. **Early slice versus Neotoma P3.** The operator's 2026-10-15 ruling advances Ateles' thin workboard read and dispatch ahead of its own phase order while P1 repairs continue. It does not accelerate Neotoma's generic P3 UI or authorize Ateles to own generic context/provenance/activity UI. **UNVERIFIED:** the full live text of either in-harnesses plan (not present in this checkout's docs), the current Neotoma P3 implementation schedule, and the live MCP App's overlay extension contract; probe the host and confirm the overlay interface before promising an embedded card. The plain structured read is the supported fallback.
6. **First Fly design dispatch.** The operator asked that this design be exercised as the first real swarm dispatch of design work on a Fly runner. The task, plan link, Fly eligibility, remote transport, grants and task-linked result must be verified end to end before calling that exercise complete. **UNVERIFIED:** whether this document's current authoring run used Fly; the repository does not establish its runner provenance. A local authoring pass cannot be described as that exercise.

These questions do not authorize a credential grant, remote endpoint, public write or merge by themselves. Each action still follows its live task, review, consent and release gates.

## 9. In-harness views

Every view is an Ateles overlay on Neotoma's generic MCP App, not a new store. The server returns one compact structured result, then renders both card and structured-text fallback from it. Text repeats the card's fields in the **same order**; no client-specific summary may silently omit a blocker, gap or authority field. Expand details only on an explicit read. The overlay can appear only when the connector is reachable and the host renders the declared MCP App interface; a disconnected connector cannot be repaired by an App. A Claude Code mod or pane is a delivery layer over the same result, never the lock or the source of state.

| View and user need | Data and default ordering | Neotoma MCP App overlay | Claude Code tab | Structured-text fallback |
|---|---|---|---|---|
| **Workboard:** what is working, awaiting me, held or done, and under which plan? | `get_workboard`: `counts`, then `tasks` grouped `working_now`, `awaiting_operator`, `held`, `done`, `unknown`; each shows `plan_ascent`, `last_event`, `linked_pr`, `runner_output_ref`, `gaps`. One small page. | Workboard card with state bands and a task detail action. | Persistent pane with the same bands; a compact status band and instant command to open a task. | Counts, then the same state bands and task fields, including gaps. |
| **Session work:** what did this session dispatch, and what happened? | `get_workboard(session_id=...)` plus `dispatch_status`, `get_task_timeline`; later `panel_status` and `pr_status`. Show task, dispatch state, result, PR, then verdicts pinned to each head; omit absent later-stage data with a gap. | Session work card linking each dispatch to its task and result. | Session pane plus instant command for the selected dispatch. | Same task → dispatch → result → PR → head/verdict order. |
| **Checkpoint triage:** what actually needs my decision? | `list_checkpoints(task_entity_id, created_since, sort="newest")` with open checkpoint read-back; task and time filters, small page. Each row shows task/plan, action, created time, current resolution, authority and gap. | Checkpoint card with one action per open checkpoint; after action, read the checkpoint back. | Awaiting-me band and instant command that invokes the existing signed `resolve_checkpoint` flow. | Same fields and order; offer the signed action through a tool call, never by treating prose as approval. |
| **Review and release:** can this head proceed? | Later `review_plan`, `panel_status`, `pr_status`: head first, fresh versus carried verdicts and source heads, acceptance-test counts/skips, CI, live blockers, merge readiness. One PR/head by default. | Review card with head and blocker status prominent; detail action for evidence. | PR pane and status band, with instant command for a scoped read or authorized action. | Same head → lenses → acceptance → CI → blockers → readiness order. |
| **Usage and headroom:** which provider can run now? | Later `usage_status`: provider, observed time/age, headroom, cooldown, gate code; unknown is explicit. One provider by default. | Usage card with freshness and availability. | Compact status band; instant refresh command uses `usage_refresh` when authorized. | Same provider → freshness → headroom → gate order. |
| **Dispatch detail:** which definition, provider, tier and cost drove this run? | `dispatch_status`: task/dispatch IDs, `loaded_definition`, runner, provider, `model_tier`, `cost_usd`, state, result/PR pointer and gaps. One dispatch. | Dispatch card with safe definition status and output link. | Dispatch pane and instant command to read bounded detail. | Same identity → definition → runner/provider/tier/cost → state → result/PR → gaps order. |

Checkpoint resolution is one recorded resolution per checkpoint. The operator signs the existing body-bound AAuth request; Ateles passes and verifies that proof through `resolve_checkpoint` and reads the resolved checkpoint back. The server never holds the operator's key, and a card click alone cannot authorize a decision. Later `approve_and_merge` still applies its own action-bound authority and current-head checks.

### Surface matrix

“Guaranteed” means the contract this design can enforce when its MCP tool is callable; host rendering is measured separately. Each carrier shows the same server-rendered content and order when it is available.

| Surface | Guaranteed contract | Discretionary or UNVERIFIED presentation |
|---|---|---|
| Chat with Neotoma MCP App | Tool result and structured-text fallback on an authorized call; same fields and ordering. | Embedded overlay card support and exact action host behavior are **UNVERIFIED**. |
| Cowork with Neotoma MCP App | Tool result and structured-text fallback on an authorized call; same fields and ordering. | Embedded overlay card support and exact action host behavior are **UNVERIFIED**. |
| Claude Code tab | MCP result and text fallback; signed checkpoint semantics stay server-enforced. | Pane, band and instant-command placement depend on tab/mod integration; their live availability is **UNVERIFIED**. |
| Codex via hooks | MCP result and text fallback when the connector is configured; hooks may surface a compact pointer. | Hook delivery timing, App rendering and native pane support are **UNVERIFIED** here. |
| Cursor via hooks | MCP result and text fallback when the connector is configured; hooks may surface a compact pointer. | Hook delivery timing, App rendering and native pane support are **UNVERIFIED** here. |
| Fly runner with no UI | Same task IDs, tool contracts and task-linked events/results; compact text is available to its logs/caller. | No card or pane is promised. Remote MCP reachability remains **UNVERIFIED** until probed. |

Capability equivalence means equivalent outcomes across harnesses, with native presentation where measured and structured text everywhere else. The text fallback imitates the card and is rendered by the server from the same result, so a host's discretionary UI cannot change a task's state, hide an open checkpoint or become a second authority. Keep default responses small to protect the operator's Claude allowance and make swarm dispatch cheaper and easier than creating a session subagent; request larger detail only for a selected task, checkpoint or PR.
