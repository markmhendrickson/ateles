# Swarm trigger layer — GitHub webhooks → Apis → gate agents

Implements ateles#80, ateles#81, ateles#82, and neotoma#1640. This is the
layer that makes the shift-left gate pipeline run autonomously on GitHub
events instead of requiring an operator to invoke Lanius/Pavo/Vanellus by
hand.

## Topology

```
GitHub webhook (issues, pull_request)
        │  X-Hub-Signature-256 (APIS_GITHUB_WEBHOOK_SECRET)
        ▼
Apis github_gateway (127.0.0.1:8742, expose via Cloudflare Tunnel)
        │  SwarmTrigger
        ▼
swarm_dispatch.SwarmDispatcher
        ├─ issue.opened ──► Lanius (init gates, assign Pavo, label)
        │                   ├─► review-expectation pass (ateles#81):
        │                   │     relevant lenses post what they WILL check
        │                   │     at PR time, as issue comments + Neotoma
        │                   │     plan_contributions
        │                   └─► Pavo (Phase 1 pm scoping)
        │
        └─ pull_request.opened/reopened/synchronize
                            ├─► pipeline-bypass guard: product-code PR with
                            │     NO parent issue → visible PR comment +
                            │     operator_decision ping (review still
                            │     proceeds; merge stays gated)
                            ├─► Lanius (PR gate inheritance; emits
                            │     GATE_INHERITANCE: clear|blocked)
                            ├─► review panel (neotoma#1640): pre-registered
                            │     agents ∪ diff-surface lenses ∪ Corvus on
                            │     non-trivial PRs; each reviews ONLY through
                            │     its lens, against its own expectations
                            ├─► learning pass (ateles#82): systemic
                            │     [BLOCKING] findings → operator-gated
                            │     proposed_skill_update entities
                            ├─► check_suite.completed (ateles#197):
                            │     CI failed → route to Cicada; CI green +
                            │     review clear → readiness gate → notify
                            ├─► Vanellus (aggregate verdicts; NO merge)
                            └─► verdict router (ateles#179):
                                  • REQUEST_CHANGES/BLOCKED → per-lens agents
                                    author fix guidance → Cicada implements +
                                    pushes → synchronize re-runs the panel
                                    (bounded by APIS_MAX_FIX_ROUNDS, default 2;
                                    then escalate to operator)
                                  • APPROVE/COMMENT → readiness gate: file the
                                    merge checkpoint + operator_decision ping
                                    ONLY when required CI is also green (else
                                    hold quietly; red CI → route to Cicada)
```

Apus stays the Neotoma→git mirror webhook daemon (port 8741) — it does not
dispatch swarm work. The Claude GHA reviewer stays as the always-on
correctness/security baseline; the panel adds domain and forward-looking
lenses on top.

## Modules (execution/daemons/apis/)

| Module | Issue | Responsibility |
|---|---|---|
| `github_gateway.py` | #80 | aiohttp receiver, HMAC verify, payload → SwarmTrigger |
| `swarm_dispatch.py` | #80/#81/#1640/#82 | the two pipelines, harness_event + checkpoint_brief writes |
| `review_panel.py` | #1640/#81 | lens registry, panel selection, expectation relevance filter |
| `review_learning.py` | #82 | finding parser, systemic classifier, proposed_skill_update payloads |
| `skill_runner.py` | — | shared `claude --print --append-system-prompt` spawn |

## Autonomy guardrail (ateles#80)

Read-only gates (triage, pm scoping, review comments, expectation comments)
run unattended — Neotoma metadata and GitHub comments only, reversible.
Side-effecting steps stay operator-gated:

- **Merge**: Vanellus is instructed not to merge; a blocking
  `checkpoint_brief` (`checkpoint_kind: pr_merge`) is filed and the operator
  is notified at `operator_decision` priority. Set
  `APIS_AUTONOMY_AUTO_MERGE=1` to lift once the pipeline has a track record.
- **Skill changes** (#82): review findings only ever produce
  `proposed_skill_update` entities with `approval_required: true`; the
  operator approves, then `learn`/`neotoma-learn` applies the correction to
  the agent_definition `prompt_markdown` (never by editing SKILL.md).

## Review contract lifecycle (#81 ↔ #1640)

1. Issue opens → each relevant lens posts a
   `**review_expectation (<lens>)** — what <agent> will verify …` comment
   (tight checklist, 3–6 items) and stores a `plan_contribution`
   (`contribution_type: review_expectation`) PART_OF the issue.
2. Gryllus treats those comments as binding definition-of-done.
3. PR opens → the dispatcher recovers the expectation comments from the
   parent issue via the GitHub API; each pre-registered agent joins the
   panel and reviews against exactly what it promised to check.

## Operational setup

1. Configure a GitHub webhook on each pipeline repo (ateles, neotoma):
   payload URL → the tunnel in front of `127.0.0.1:8742/github/webhook`,
   content type `application/json`, secret = `APIS_GITHUB_WEBHOOK_SECRET`,
   events: Issues, Pull requests, Pull request reviews, **Check suites**
   (the last drives CI-completion → readiness/route, ateles#197). `status`
   events are intentionally not consumed — `check_suite:completed` is the
   single terminal per-commit signal, so subscribing to `status` would only
   add duplicate deliveries.
2. Ensure `~/.config/neotoma/.env` carries `APIS_GITHUB_WEBHOOK_SECRET`,
   `NEOTOMA_BEARER_TOKEN`, and `GITHUB_TOKEN` (or `ATELES_AGENT_PAT`,
   classic `repo` scope — fine-grained PATs 403 on public-repo writes).
3. `launchctl load execution/daemons/apis/com.ateles.apis.plist`.
4. Acceptance check (per #80): open a test issue → gates init + Pavo
   assigned with no manual invocation; open a PR with `closes #N` →
   panel reviews land and merge halts on the checkpoint.

## Manual / backfill dispatch

`execution/scripts/trigger_swarm_pr.py <owner/repo> <pr|issue> <number>`
reconstructs the webhook payload from the GitHub REST API and feeds the
pipeline directly — no webhook or gateway needed. Use it to backfill events
that fired before the webhook existed or to re-run a review after a
pipeline change. `APIS_DRY_RUN=1` validates wiring without dispatching.

## Hardening from the PR-87 self-dogfood run (2026-06-12)

The pipeline was first exercised by manually dispatching PR #87 — the PR
that introduced it — against itself. Fixes that came out of that run:

- **Lanius verdict retry** — if Lanius replies without the mandatory
  `GATE_INHERITANCE:` line, the dispatcher retries once with an explicit
  reminder before failing open (panel proceeds, merge stays gated).
- **Dispatcher-posted review comments** — headless `claude --print`
  panelists often cannot run `gh` to post their `review:<lens>` comment
  (no way to answer permission prompts). After the panel completes, the
  dispatcher fetches the PR comments and posts any captured review whose
  comment is missing, so Vanellus always has the full panel to aggregate.
- **Review persistence** — every captured panel review is stored as a
  `harness_event` (`event_type: github.panel_review`), so review text
  survives even if comment posting fails.
- **Content-digest idempotency keys** — store keys append a digest of the
  entity payload; re-dispatching the same delivery with fresh timestamps
  creates a new observation instead of failing with
  `ERR_IDEMPOTENCY_MISMATCH`.
- **In-comment agent attribution** — every dispatched prompt instructs the
  agent to open GitHub comments with
  `**🤖 <Agent> — Ateles swarm, <role>**`, and dispatcher-posted fallback
  comments carry the same header plus an on-behalf-of footer.
- **Legacy-issue gate rule** — Lanius distinguishes "gates never
  initialized" (an issue that predates the pipeline, with no `gate_status`
  metadata at all) from "gates evaluated and still pending". A legacy issue
  is *not* hard-blocked: Lanius initializes the gates retroactively, notes
  the legacy status, and emits `GATE_INHERITANCE: clear` so review proceeds
  (merge stays operator-gated regardless). `blocked` is reserved for issues
  whose gates exist and are genuinely unsigned. Surfaced when PR #87's own
  parent issue (#80) predated the pipeline and was hard-blocked. To run the
  full issue pipeline on a legacy issue, backfill with
  `trigger_swarm_pr.py issue <n>`.
- **Pipeline-bypass guard** — the legacy-issue rule above is fail-open by
  design, which means a *hand-authored* product PR with **no parent issue**
  (no `Closes #N`) would otherwise sail through with its gates silently
  back-filled. The guard makes that case loud instead: on a
  `pull_request.opened` whose diff `touches_product_code()` (source / API /
  schema / migration / CLI surfaces, excluding docs, tests, CI, and config)
  and whose body has no parent issue, the dispatcher posts a visible
  `<!-- pipeline-bypass-notice -->` PR comment and sends an
  `operator_decision` notification. It does **not** block: review still runs
  and merge stays operator-gated. It only removes the silence, so a bypass is
  a deliberate, visible choice rather than an accident. Recovery: file the
  issue, add `Closes #N` to the PR, and re-run the pipeline. Added after an
  operator/Ateles session hand-authored PR #1880 (global-install `.mcp.json`
  fix) directly, bypassing issue → pm/arch → Cicada; the bypass was only
  noticed because Lanius labelled it a "legacy independent fix" retroactively.
- **Verdict router — close the review→fix→ready loop (ateles#179).** Before
  this, `_handle_pr` filed a merge checkpoint + operator ping
  *unconditionally* after Vanellus aggregation — the verdict was never parsed,
  so a REQUEST_CHANGES PR and an APPROVE PR produced the identical "held for
  approval" signal, and blocking findings never routed anywhere despite the
  Vanellus prompt saying "route back to Gryllus" (prompt-only, unwired). Now
  `parse_review_verdict` reads the `**APPROVE|REQUEST_CHANGES|COMMENT|BLOCKED**`
  token and branches:
  - **not clear** (REQUEST_CHANGES/BLOCKED/unparseable) → `_route_blocking_findings`
    groups blocking `[BLOCKING]` findings by the `review:<lens>` that raised
    them, asks each lens's own reviewing agent (arch→Waxwing, ux→Accipiter,
    qa→Phoenicurus, …) to author domain-specific fix guidance (model a:
    reviewer proposes, Cicada implements — no agent grades its own fix), then
    hands the consolidated guidance to Cicada to implement + push. Cicada's
    push fires `synchronize`, re-running the whole panel. Bounded by
    `APIS_MAX_FIX_ROUNDS` (default 2, counted via a hidden `apis-fix-round:N`
    PR comment so it survives daemon restarts and is head-SHA-agnostic); at the
    cap it escalates to the operator instead of looping.
  - **clear** (APPROVE/COMMENT) → `_gate_merge_readiness` files the merge
    checkpoint + `OPERATOR_DECISION` ping **only when required CI is also
    green** (`_required_ci_state` polls the combined commit status + check-runs
    for the head SHA; fail-open to "unknown" so a transient API error never
    fabricates a green). Red CI → route back to Cicada (bounded, same counter);
    pending/unknown → hold quietly (digest note, not a merge page). So the
    operator's "READY TO MERGE" email is now a truthful signal.
  Merge stays operator-gated throughout (`APIS_AUTONOMY_AUTO_MERGE=0`). An
  auth/billing-expired Cicada or lens call is reframed as an infra page, not a
  false "fix applied". *Not yet handled (Phase 4 / follow-up):* a check
  completing does not itself re-invoke the readiness gate, and formal
  `pull_request_review` / `check_suite` webhook events are not yet ingested — a
  re-push is still what re-runs the panel.

  **Re-push against an OPEN pre-impl gate (`ATELES_SWARM_AUTO_REREVIEW`, ateles#230).**
  The "a re-push re-runs the panel" contract above has one hole: when the PR's
  parent issue still has an unsigned pre-impl gate, Lanius returns
  `GATE_INHERITANCE: blocked` and `_handle_pr` returns *before* the panel. On the
  first look that is correct — nothing has been reviewed, so there is no finding
  to re-evaluate. On a **re-push** it strands the PR: an author can push exactly
  the fix a lens asked for and nothing re-evaluates it, because the gate that
  blocks the panel is the same gate the fix was meant to unblock. The PR waits
  for a human to manually re-invoke the reviewer (`/swarm-run`).

  Set `ATELES_SWARM_AUTO_REREVIEW=1` so a `synchronize` push to a gate-blocked PR
  re-invokes the lens agents against the new head; they re-affirm the block,
  raise a new finding, or sign off on their own. Default OFF = today's skip.

  This does **not** weaken the self-certification boundary: a push is not a
  sign-off. The gate still flips only on a lens agent's own verdict — the
  dispatcher never writes `gate_status` (Lanius/the lens own that) and never
  infers approval from the fact that code changed. Auto-re-*review* ≠
  auto-*approve*; merge remains behind `APIS_AUTONOMY_AUTO_MERGE`.

## Agent identity on GitHub

GitHub comments are posted through a shared per-repo machine identity
(`ateles-agent` / `neotoma-agent`), so the **comment body is the agent
identity**: every swarm comment opens with the attribution header above.
This follows the architecture decision that agent identity lives in AAuth
(`sub` per role) and Neotoma provenance, while GitHub accounts are a
per-repo actor concern — one machine account per repo, not per agent.
Per-agent GitHub identities (separate accounts or a GitHub App per agent)
are deferred to Layer-3 graduation (`ateles-agents/<genus>`), where an
agent gains an independent lifecycle that justifies the account, PAT
rotation, and collaborator-management overhead. If stronger visual
separation is wanted before then, a single GitHub App ("Ateles Swarm")
posting with the same in-body attribution gives the `[bot]` badge without
multiplying accounts.

Note: the machine accounts need **classic** repo-scope PATs — fine-grained
PATs 403 on public repos they collaborate on but do not own.
