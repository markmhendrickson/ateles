---
name: analyze
description: Analyze codebase or context per foundation analyze command. Produces a comparative analysis (competitive/partnership/relevance) of a target product, content, or repo against all known repos in Neotoma; stores the full analysis plus sub-entities (tasks, findings, identified repos, proposed issues) in Neotoma; and, opt-in, opens public GitHub issues for repo-touching tasks with the competitive analysis sections redacted.
triggers:
  - analyze
  - /analyze
user_invocable: true
entity_id: ent_2abe100df424dbe78fd3c5f5
---

## Notes

- This command is generic and works for any repo using foundation as submodule.
- **Comparative analysis:** All analysis is relative to **all** repos. Load repos from Neotoma (`repository` entities). If empty, run `execution/scripts/sync_repos_to_neotoma.py`.
- Output documents are confidential and stored in the private docs submodule. Persistence of findings to Neotoma (Step P below) is **always** performed; opening public issues (Step I) is opt-in.
- **Resource Type Detection:** Command automatically detects if resource is a product/project (competitive/partnership analysis) or content/thought leadership (relevance analysis).
- Templates ensure consistent, thorough analysis across all assessments.
- For content/thought leadership, analysis focuses on extracting insights applicable to current repo rather than competitive positioning.
- **Web Scraper Integration:** ChatGPT and Twitter/X URLs are automatically handled via web scraper MCP server when configured (see MCP configuration).
- **Browser Tools Fallback:** Non-scraper URLs and search terms use browser tools for research.

## Step P: Persist findings to Neotoma (required, same turn)

After producing the markdown analysis document (the existing foundation-template output), persist a structured record to Neotoma in the **same turn** per the Neotoma MCP turn lifecycle and `[PROVENANCE]` rules. This applies regardless of resource type (competitive, partnership, relevance) and regardless of whether public issues are also opened.

### Entity-type reuse check (before first store in a fresh session)

Call `list_entity_types` with keywords `analysis`, `task`, `issue`, `finding`, `repository` to check for existing types. Reuse exact strings when present. Default types:

- `analysis` — the overall analysis (one per invocation). Subtype the analysis kind via a `kind` field rather than minting separate top-level types.
- `task` — Mark-internal follow-up actions extracted from the analysis.
- `proposed_github_issue` — drafted public issues for repo-touching tasks (same type as used by `/analyze-meeting`).
- `analysis_finding` — discrete findings inside the analysis (one entity per finding so they can be cross-referenced and aggregated independently). Reuse `finding`, `insight`, or similar if already present in the type registry.
- `repository` — never created here; only retrieved via `retrieve_entities` for the comparative scope.

### Retrieve first

- All `repository` entities for the comparative scope (paginated; cap at the full set unless the analysis explicitly narrows scope).
- Any existing `analysis` for the same target (idempotency — by `target_identifier` + `kind`). When one exists, prefer updating it (add observations) over creating a duplicate.
- Existing `task` and `proposed_github_issue` entities referring to the same target, to avoid duplicates.

### Store

Single `store` call when batchable. Entities (typical order):

1. `conversation` — current chat conversation (turn lifecycle).
2. `agent_message` — current user message, `turn_key: "{conversation_id}:{turn_id}"`.
3. `analysis` with fields:
   - `title` — short identifier (`<target> — <kind> analysis`).
   - `kind` — `competitive` | `partnership` | `relevance` | `mixed`.
   - `target_identifier` — canonical reference: URL, file path, repo name, product name. The idempotency key.
   - `target_type` — `product` | `repo` | `content` | `thought_leadership` | `mixed`.
   - `source_type` — `url` | `file` | `text` | `entity`.
   - `source_reference` — path, URL, or entity_id.
   - `report_path` — absolute path to the written markdown analysis (the foundation-template output).
   - `summary_bullets` — 3–5 highest-signal bullets (mirrors the report's headline section).
   - `repositories_compared_against` — array of `repository` entity_ids included in the comparison.
   - `competitive_section_text` — full competitive-analysis section verbatim (private; never expose in issues).
   - `partnership_section_text` — full partnership section verbatim (private; safe-ish but treated as private by default).
   - `relevance_section_text` — full relevance/insights section.
   - `task_descriptions` — array of strings, one per identified follow-up task (mirrors the `task` entities below).
   - `repo_touching_task_indices` — array of integers indexing into `task_descriptions` for tasks that involve repo work.
   - `pii_inventory` — object with `names`, `emails`, `customers`, `internal_projects`, `other`. Drives redaction in Step I.
   - `data_source` — e.g. `analyze.skill {timestamp} target=<…>`.
4. One `task` per identified follow-up action, with `description`, `due_date?`, `status: open`, `source: analysis`, `repo?` (when the task touches a specific repo).
5. One `analysis_finding` per discrete finding (a finding is an atomic claim or observation worth surfacing independently — typically 3–10 per analysis). Fields: `claim`, `evidence`, `confidence` (`high`|`medium`|`low`), `kind` (`competitive`|`partnership`|`relevance`|`market`|`technology`|`other`).
6. One `proposed_github_issue` per repo-touching task that warrants a public issue (see Step I for the redaction filter). Fields:
   - `repo` (`owner/repo`), `title`, `labels`, `body_redacted`, `confidence`, `backed_by_task_index`, `competitive_content_stripped` (boolean — true iff at least one competitive sentence was redacted), `opened_url` (null until Step I runs).

Attach the source artifact (URL scrape payload, file content) via the combined store's `file_path` / `file_content` + `mime_type` so the raw artifact is preserved per `[PROVENANCE]`.

### Relationships

Batch in the same `store` call via `relationships`:

- `PART_OF`: `agent_message` → `conversation`.
- `REFERS_TO`: `agent_message` → `analysis`.
- `REFERS_TO`: each `task` → `analysis` (and to the relevant `repository` when the task is repo-scoped).
- `REFERS_TO`: each `analysis_finding` → `analysis`.
- `REFERS_TO`: each `proposed_github_issue` → `analysis` AND to the backing `task`.
- `REFERS_TO`: `analysis` → each `repository` in `repositories_compared_against` (batched; if the comparison set exceeds ~25, link only the repos cited in the report body and keep the full id list inside `repositories_compared_against`).

### Idempotency

- Turn idempotency key: `conversation-{conversation_id}-{turn_id}-analyze-{timestamp_ms}`.
- Per-analysis idempotency key: `analysis-<sha256(target_identifier + kind)[:12]>`. Lets reruns on the same target update the existing entity rather than duplicating.

## Step I: Open public issues for repo-touching tasks (opt-in)

Default is **off** — issues stage as `proposed_github_issue` entities only. Opening real issues is enabled by:
- `--open-issues` flag on the `/analyze` invocation, OR
- `ANALYZE_OPEN_GH_ISSUES=1` in env.

When opt-in is active, for each `proposed_github_issue` whose `repo` is in the allowlist:

### Allowlist

Allowlisted repos default to the union of:
- `markmhendrickson/neotoma`
- `markmhendrickson/personal`
- Any `repository` entity in Neotoma with `is_public: true` AND `owner: markmhendrickson` (when that field is populated).

Override via `ANALYZE_ALLOWED_REPOS` env (comma-separated `owner/repo`). Repos outside the allowlist stay as drafts and the report notes `_Out-of-scope repo: <name> — issue not opened._`.

### Redaction (mandatory before any public issue body)

Before opening any issue, the body MUST pass these redactions. A draft that fails any check is **demoted** to an internal `task` (not opened) and noted in the report.

1. **Competitive analysis stripped.** Remove all sentences that reference or compare to other repos / products / vendors:
   - Strip explicit mentions of comparator names (e.g. `Mem0`, `OpenAI Memory`, `Neotoma vs X`, `unlike Y`, `competitive with Z`).
   - Strip relative judgments ("we lead on X", "they're ahead on Y", "they don't yet support Z").
   - Strip framing language that exposes our positioning ("our wedge", "our differentiation", "our moat").
   - Keep only the *neutral problem statement* and the *acceptance criteria* phrased as a feature/bug request — the issue should read like a fresh user request, not an internal strategy memo.
   - Mark `competitive_content_stripped: true` on the stored `proposed_github_issue` entity when any of the above triggered.
2. **PII scrubbed** (same rules as `/analyze-meeting` Step 3):
   - Participant / customer names → roles (`an evaluator`, `a customer in <vertical>`).
   - Emails / phone numbers → removed (rewrite the sentence, never `[redacted]` mid-sentence).
   - Internal URLs / dashboards / prod links → removed.
   - Internal project names not already published → generalized.
3. **Source attribution.** Issue body ends with: `Surfaced via internal analysis on YYYY-MM-DD; full context kept privately.` Never link the analysis report or the source URL publicly.

### Open the issue

- Call the GitHub MCP `issue_write` (create) with `owner`, `repo`, `title`, `body` (redacted), `labels`.
- Capture the returned issue URL into the `proposed_github_issue` entity's `opened_url` field via a follow-up narrow store call.
- On failure: leave `opened_url` null and record the error in `open_error`. Continue with remaining drafts.

When opt-in is **off**: issues stay as drafts. The report surfaces the count and titles; nothing is published.

## Step S: Surface to the user

Reply with:
- One-line headline (e.g. `Analysis: <target> — <kind> — N findings, M follow-up tasks, K proposed issues.`).
- Top 3 findings (verbatim from the report's headline bullets).
- Follow-up tasks — short list with repo flag where applicable.
- Proposed issues — one line per issue: `<owner/repo>: <title> — <opened-URL | draft>`. Mark `competitive_content_stripped: true` ones with a `[stripped]` tag.
- Absolute report path.
- `analysis` entity id + counts of created `task` / `analysis_finding` / `proposed_github_issue` entities.

Render the `Neotoma` section per the `[COMMUNICATION & DISPLAY]` display rule (created and retrieved entities).

## Behavior rules

- **Persistence is non-optional.** Step P runs on every `/analyze` invocation. If Neotoma is unreachable, fail loudly with the error rather than silently writing only the markdown report — partial states create stale graphs.
- **Competitive content never leaks.** Any sentence that compares our position to another repo / product / vendor is stripped before a public issue body is generated. Better to demote to internal task than to leak. When demoted, the report notes `_Demoted: competitive content could not be cleanly stripped._`.
- **No invented findings.** Every `analysis_finding` traces to specific evidence — a verbatim quote from the source, an observed code pattern, a measured metric. Speculation is labeled `confidence: low` and flagged in the report.
- **Issues opt-in.** Default is staging, not opening. The skill writes drafts and stops. The user opts in explicitly per run.
- **Reuse repositories.** Never create `repository` entities here — only retrieve. If the comparative set is empty, prompt the user to run `execution/scripts/sync_repos_to_neotoma.py` first; do not proceed with a degraded comparison.
- **Stable shape.** Sections with no content render as `_None._` in the report AND as empty arrays in the stored entities, so cross-analysis queries remain reliable.
- **Backfill-friendly.** When invoked as part of a batch backfill (`ANALYZE_BACKFILL=1` in env, or via `execution/scripts/backfill_analyze_private_docs.py`), skip the user-facing reply formatting and emit a single-line JSON status (`{"target": ..., "analysis_entity_id": ..., "tasks": N, "findings": N, "issues_drafted": N}`) so the driver can aggregate progress.

## Out of scope

- Sending public messages / posts about the analysis — kept private.
- Auto-publishing analysis reports — output documents stay in the private docs submodule.
- Modifying the comparator repos — they are read-only inputs to the comparison.
