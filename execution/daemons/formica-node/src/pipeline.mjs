import fs from "node:fs";
import path from "node:path";
import { runAgent } from "./agent_runner.mjs";
import { resolveBaseCommit } from "./base_resolver.mjs";
import { classifyIssue, flattenEntitySnapshot, heuristicClassify } from "./classifier.mjs";
import { isDaemonProcessingEnabled } from "./kill_switch.mjs";
import { getEntitySnapshotJson, getSourceText, createRelationship } from "./neotoma.mjs";
import {
  countCommitsAheadOfBase,
  gitPushBranch,
  maybeRebaseOntoDefault,
  preflightDirtyTree,
  runGhCreatePr,
} from "./pr_manager.mjs";
import { applyPatchInWorktree, createIssueWorktree } from "./worktree_manager.mjs";
import { HourlyRateLimiter } from "./rate_limit.mjs";
import { patchEntity, patchIssue, postAgentMessage, storeDaemonSession } from "./state_updater.mjs";
import { telegramSendMessage } from "./operator_transport.mjs";

/**
 * @param {Record<string, unknown>} issue
 * @param {Record<string, unknown>} repos
 */
export function resolveRepoEntry(issue, repos) {
  if (!repos || typeof repos !== "object") return null;
  const keys = Object.keys(repos);
  if (keys.length === 0) return null;
  const r = issue.repository != null ? String(issue.repository) : "";
  if (r && repos[r] && typeof repos[r] === "object") {
    return { key: r, cfg: /** @type {Record<string, unknown>} */ (repos[r]) };
  }
  for (const k of keys) {
    if (r.includes(k)) return { key: k, cfg: /** @type {Record<string, unknown>} */ (repos[k]) };
  }
  const first = keys[0];
  return { key: first, cfg: /** @type {Record<string, unknown>} */ (repos[first]) };
}

/**
 * @param {Record<string, unknown>} issue
 */
export function issueNumberForBranch(issue) {
  const n =
    issue.github_number ??
    issue.github_issue_number ??
    issue.number ??
    issue.issue_number;
  if (n != null && String(n).trim()) return String(n).replace(/\D/g, "") || "0";
  const m = String(issue.title || "").match(/#(\d+)/);
  return m ? m[1] : String(issue.entity_id || "0").slice(-6);
}

/**
 * @param {string} workspacePath
 */
export function hasProcessIssuesSkill(workspacePath) {
  if (!workspacePath || workspacePath.startsWith("(")) return false;
  const skillPaths = [
    path.join(workspacePath, ".cursor", "skills", "process-issues", "SKILL.md"),
    path.join(workspacePath, ".claude", "skills", "process_issues", "SKILL.md"),
  ];
  return skillPaths.some((skillPath) => fs.existsSync(skillPath));
}

/**
 * @param {{
 *   entityId: string;
 *   issue: Record<string, unknown>;
 *   classification: { classification: string; confidence?: number; notes?: string };
 *   workspacePath: string;
 * }} params
 */
export function buildAgentPrompt({ entityId, issue, classification, workspacePath }) {
  const useProcessIssues = hasProcessIssuesSkill(workspacePath);
  return [
    useProcessIssues
      ? [
          "This repository exposes Neotoma's `/process-issues` skill.",
          "Use `/process-issues` as the primary workflow for this issue instead of handling it ad hoc.",
          `Scope the run to issue entity ${entityId} only and do not triage unrelated issues from the broader queue.`,
        ].join("\n\n")
      : "",
    `Fix the following issue in this repository worktree.`,
    `Issue entity: ${entityId}`,
    `Title: ${issue.title || issue.subject || ""}`,
    `Description:\n${issue.body || issue.description || ""}`,
    `Classification: ${classification.classification}`,
    classification.notes ? `Classifier notes: ${classification.notes}` : "",
    `When finished, add a single line DONE on its own line if changes are complete.`,
  ]
    .filter(Boolean)
    .join("\n\n");
}

/**
 * @param {Record<string, unknown>} ctx
 * @param {Record<string, unknown>} pending
 */
export async function resumePendingPrAutomation(ctx, pending) {
  const proc = ctx.cfg.processing || {};
  const dryRun = proc.dry_run !== false;
  const prBase = String(proc.default_branch || "main");
  if (dryRun) {
    console.error("[formica] dry-run: skip resume PR");
    return;
  }
  if (ctx.rateLimiter && !ctx.rateLimiter.canConsume()) {
    console.error("[formica] resume PR blocked: hourly rate limit");
    return;
  }
  await gitPushBranch(pending.worktreePath, pending.branch, {});
  const prBody = `Resolves automated issue.\n\nBase: ${pending.baseCommit}\nPolicy: ${pending.rebasePolicy}\n\n---\nAutomated by issue-processing daemon`;
  const pr = await runGhCreatePr({
    worktreePath: pending.worktreePath,
    branch: pending.branch,
    title: `Fix: ${pending.issueTitle} (#${pending.issueNumber})`,
    body: prBody,
    prBaseBranch: prBase,
    dryRun: false,
  });
  if (pr.ok && pr.pr_url) {
    await patchIssue(
      ctx,
      pending.issueEntityId,
      {
        status: "in_progress",
        pr_url: pr.pr_url,
        pr_number: pr.pr_number,
        branch: pending.branch,
        daemon_action: "pr_created",
      },
      `issue-pr-${pending.issueEntityId}-${Date.now()}`,
    );
    ctx.rateLimiter?.consume();
  }
}

/**
 * @param {Record<string, unknown>} ctx
 * @param {{ reason: string; issueEntityId: string; sessionId?: string | null; worktreePath?: string; branch?: string; baseCommit?: string; rebasePolicy?: string; prUrl?: string | null }} payload
 */
async function notifyHumanNeeded(ctx, payload) {
  const ot = ctx.cfg.operator_transport || {};
  const backend = String(ot.backend || "none");
  if (backend === "telegram" && ctx.telegram?.token && ctx.telegram.chatId) {
    const text = [
      `Issue processor — HUMAN_NEEDED`,
      `Reason: ${payload.reason}`,
      `issue_entity_id=${payload.issueEntityId}`,
      payload.sessionId ? `daemon_session_id=${payload.sessionId}` : "",
      payload.baseCommit ? `BASE_COMMIT=${payload.baseCommit}` : "",
      payload.rebasePolicy ? `rebase_policy=${payload.rebasePolicy}` : "",
      payload.branch ? `branch=${payload.branch}` : "",
      payload.worktreePath ? `worktree=${payload.worktreePath}` : "",
      payload.prUrl ? `PR=${payload.prUrl}` : "PR=(none yet)",
      "",
      "Open the worktree path in Cursor or run your agent CLI there.",
      "Reply /shipit to open PR after local commits (when auto_fix is off).",
    ]
      .filter(Boolean)
      .join("\n");
    const threadId =
      ctx.telegram.defaultMessageThreadId != null
        ? Number(ctx.telegram.defaultMessageThreadId)
        : undefined;
    const res = await telegramSendMessage({
      token: ctx.telegram.token,
      chatId: ctx.telegram.chatId,
      text,
      threadId,
    });
    const tid = Number(res?.message_thread_id ?? 0);
    const pending = {
      issueEntityId: payload.issueEntityId,
      worktreePath: payload.worktreePath,
      branch: payload.branch,
      baseCommit: payload.baseCommit,
      issueTitle: payload.issueTitle,
      issueNumber: payload.issueNumber,
      rebasePolicy: payload.rebasePolicy,
    };
    ctx.pendingPrByThread?.set(tid, pending);
    ctx.pendingPrByIssue?.set(payload.issueEntityId, pending);
  }
  ctx.operatorQueue?.enqueue?.(payload.issueEntityId, `[human_needed] ${payload.reason}`);
}

/**
 * @param {Record<string, unknown>} ev
 * @param {Record<string, unknown>} ctx
 */
export async function processIssueSubstrateEventFull(ev, ctx) {
  const entityId = ev.entity_id ? String(ev.entity_id) : "";
  if (!entityId) return;

  const proc = ctx.cfg.processing || {};
  const dryRun = proc.dry_run !== false;
  const autoClassify = proc.auto_classify !== false;
  const autoFix = proc.auto_fix === true;
  const rebasePolicyDefault = String(proc.rebase_policy || "strict_reporter");
  const defaultBranch = String(proc.default_branch || "main");
  const alignPr = proc.align_pr_to_main === true;
  const dirtyPolicy = String(proc.dirty_tree_policy || "abort");
  const agentMode = String(proc.agent_mode || "oneshot");
  const agentRuntime = String(proc.agent_runtime || "cursor");
  const maxPrPerHour = Number(proc.max_prs_per_hour) || 5;

  if (!(await isDaemonProcessingEnabled(ctx.baseUrl, ctx.token))) {
    console.error("[formica] kill_switch: daemon_config active=false — skipping event");
    return;
  }

  if (!ctx.rateLimiter) ctx.rateLimiter = new HourlyRateLimiter(maxPrPerHour);

  console.error(`[formica] pipeline start entity_id=${entityId} dry_run=${dryRun}`);

  const snapRaw = await getEntitySnapshotJson(ctx.baseUrl, ctx.token, entityId);
  const issue = flattenEntitySnapshot(snapRaw);
  const repos = ctx.cfg.repos || {};
  const repoEntry = resolveRepoEntry(issue, repos);
  if (!repoEntry) {
    console.error("[formica] No repos.* entry matched issue.repository — configure config.yaml repos");
    return;
  }

  const repoPath = String(repoEntry.cfg.path || "");
  const worktreeBase = String(repoEntry.cfg.worktree_base || `/tmp/${repoEntry.key}-worktrees`);
  if (!repoPath) {
    console.error("[formica] repo path missing for", repoEntry.key);
    return;
  }

  const base = await resolveBaseCommit({
    repoPath,
    defaultBranch,
    policy: rebasePolicyDefault,
    issue,
  });

  const basePreview = base.ok ? `BASE_COMMIT=${base.baseCommit} (${base.rebase_policy_used})` : `BASE unresolved: ${base.reason}`;

  let classification = { classification: "bug_fix", confidence: 0, notes: "" };
  if (autoClassify) {
    classification = await classifyIssue({
      issue,
      basePreview,
      openaiApiKey: process.env.OPENAI_API_KEY,
    });
  } else {
    classification = heuristicClassify(issue);
  }

  if (!base.ok && rebasePolicyDefault === "strict_reporter") {
    classification = {
      classification: "needs_repro",
      confidence: 0.9,
      notes: `base_resolver:${base.reason}`,
    };
  }

  if (!dryRun) {
    await patchIssue(
      ctx,
      entityId,
      {
        daemon_classification: classification.classification,
        daemon_confidence: classification.confidence,
        daemon_notes: classification.notes,
        ...(classification.classification === "needs_repro" ? { status: "needs_repro" } : {}),
      },
      `issue-classify-${entityId}-${Date.now()}`,
    );
  }

  const nonWork = ["question", "duplicate", "out_of_scope", "needs_repro"];
  if (nonWork.includes(classification.classification)) {
    if (!dryRun) {
      await postAgentMessage(
        ctx,
        {
          content: `Classification: **${classification.classification}** (${classification.confidence}). ${classification.notes}`,
          turn_key: `${entityId}:daemon-${Date.now()}`,
          issue_entity_id: entityId,
        },
        `conv-${entityId}-${Date.now()}`,
      );
    }
    if (classification.classification === "needs_repro") {
      await notifyHumanNeeded(ctx, {
        reason: "needs_repro_or_unreachable_base",
        issueEntityId: entityId,
        baseCommit: base.ok ? base.baseCommit : undefined,
        rebasePolicy: base.rebase_policy_used,
        issueTitle: String(issue.title || issue.subject || "issue"),
        issueNumber: issueNumberForBranch(issue),
      });
    }
    return;
  }

  if (!base.ok) {
    await notifyHumanNeeded(ctx, {
      reason: `base_resolve_failed:${base.reason}`,
      issueEntityId: entityId,
      rebasePolicy: base.rebase_policy_used,
      issueTitle: String(issue.title || issue.subject || "issue"),
      issueNumber: issueNumberForBranch(issue),
    });
    return;
  }

  const issueNumber = issueNumberForBranch(issue);
  const slug = repoEntry.key;
  let worktreePath = "";
  let branch = "";
  let patchStatus = "skipped";
  let sessionId = null;

  if (!dryRun) {
    const wt = await createIssueWorktree({
      repoPath,
      worktreeBase,
      issueNumber,
      slug,
      baseCommit: base.baseCommit,
    });
    worktreePath = wt.worktreePath;
    branch = wt.branch;

    if (issue.reporter_patch_source_id) {
      const pid = String(issue.reporter_patch_source_id);
      const patchText = await getSourceText(ctx.baseUrl, ctx.token, pid);
      const applied = await applyPatchInWorktree(worktreePath, patchText, {});
      patchStatus = applied.ok ? "applied" : `failed:${applied.error}`;
    }

    const ds = await storeDaemonSession(
      ctx,
      {
        issue_entity_id: entityId,
        resolved_base_commit: base.baseCommit,
        rebase_policy_used: base.rebase_policy_used,
        worktree_path: worktreePath,
        preflight_status: "pending",
        patch_apply_status: patchStatus,
        operator_transport: String(ctx.cfg.operator_transport?.backend || "none"),
      },
      `daemon-session-${entityId}-${Date.now()}`,
    );
    sessionId = ds.entity_id;
    if (sessionId) {
      await createRelationship(ctx.baseUrl, ctx.token, "REFERS_TO", sessionId, entityId).catch(() => {});
    }
  } else {
    worktreePath = `(dry-run-worktree-${slug}-issue-${issueNumber})`;
    branch = `fix/issue-${issueNumber}`;
    console.error(`[formica] dry-run: would create worktree at ${worktreeBase} from ${base.baseCommit}`);
  }

  const prompt = buildAgentPrompt({
    entityId,
    issue,
    classification,
    workspacePath: dryRun ? repoPath : worktreePath,
  });

  const pollOperator =
    ctx.operatorQueue && agentMode.startsWith("conversational")
      ? () => ctx.operatorQueue.waitLine(entityId, 60_000)
      : undefined;

  const agentResult = dryRun
    ? { ok: true, skipped: true, stdout: "" }
    : await runAgent({
        mode: agentMode,
        runtime: agentRuntime,
        worktreePath,
        prompt,
        pollOperator,
        maxTurns: Number(proc.conversational_max_turns) || undefined,
        cursorSdkModel: proc.cursor_sdk_model ? String(proc.cursor_sdk_model) : undefined,
        anthropicModel: proc.anthropic_model ? String(proc.anthropic_model) : undefined,
        cursorApiKey: process.env.CURSOR_API_KEY,
        anthropicApiKey: process.env.ANTHROPIC_API_KEY,
      });

  if (!dryRun && sessionId) {
    await patchEntity(
      ctx,
      "daemon_session",
      sessionId,
      {
        preflight_status: "agent_finished",
        agent_session_id: String(
          agentResult.cursor_agent_id ||
            agentResult.anthropic_message_id ||
            agentResult.run_id ||
            "",
        ),
      },
      `daemon-session-patch-${sessionId}-${Date.now()}`,
    ).catch(() => {});
  }

  const pre = dryRun
    ? { ok: true, mode: "clean" }
    : await preflightDirtyTree(worktreePath, dirtyPolicy, {});
  if (!pre.ok) {
    await notifyHumanNeeded(ctx, {
      reason: `dirty_tree:${pre.porcelain?.slice(0, 200)}`,
      issueEntityId: entityId,
      sessionId,
      worktreePath,
      branch,
      baseCommit: base.baseCommit,
      rebasePolicy: base.rebase_policy_used,
      issueTitle: String(issue.title || issue.subject || "issue"),
      issueNumber,
    });
    return;
  }

  if (!dryRun) {
    const rb = await maybeRebaseOntoDefault(worktreePath, defaultBranch, alignPr, {});
    if (!rb.ok) {
      await notifyHumanNeeded(ctx, {
        reason: `rebase_failed:${rb.error}`,
        issueEntityId: entityId,
        sessionId,
        worktreePath,
        branch,
        baseCommit: base.baseCommit,
        rebasePolicy: base.rebase_policy_used,
        issueTitle: String(issue.title || issue.subject || "issue"),
        issueNumber,
      });
      return;
    }
  }

  const commitsAhead = dryRun
    ? 0
    : await countCommitsAheadOfBase(worktreePath, base.baseCommit, {});

  if (!autoFix && commitsAhead > 0) {
    await notifyHumanNeeded(ctx, {
      reason: "pending_approval_auto_fix_false",
      issueEntityId: entityId,
      sessionId,
      worktreePath,
      branch,
      baseCommit: base.baseCommit,
      rebasePolicy: base.rebase_policy_used,
      issueTitle: String(issue.title || issue.subject || "issue"),
      issueNumber,
    });
    return;
  }

  if (commitsAhead === 0 && !dryRun) {
    await postAgentMessage(
      ctx,
      {
        content: `No local commits ahead of ${base.baseCommit}; agent finished with ok=${agentResult.ok}.`,
        turn_key: `${entityId}:daemon-nocommit-${Date.now()}`,
        issue_entity_id: entityId,
      },
      `conv-${entityId}-nocommit-${Date.now()}`,
    );
    return;
  }

  if (!ctx.rateLimiter.canConsume()) {
    await notifyHumanNeeded(ctx, {
      reason: "rate_limit_max_prs_per_hour",
      issueEntityId: entityId,
      sessionId,
      worktreePath,
      branch,
      baseCommit: base.baseCommit,
      rebasePolicy: base.rebase_policy_used,
      issueTitle: String(issue.title || issue.subject || "issue"),
      issueNumber,
    });
    return;
  }

  if (dryRun) {
    console.error("[formica] dry-run: would push + gh pr create");
    return;
  }

  await gitPushBranch(worktreePath, branch, {});
  const prBody = `Automated PR for issue entity ${entityId}.\n\nBase: ${base.baseCommit}\nPolicy: ${base.rebase_policy_used}\nCommits ahead: ${commitsAhead}\n\n---\nissue-processing daemon`;
  const pr = await runGhCreatePr({
    worktreePath,
    branch,
    title: `Fix: ${issue.title || issue.subject || "issue"} (#${issueNumber})`,
    body: prBody,
    prBaseBranch: defaultBranch,
    dryRun: false,
  });

  if (pr.ok && pr.pr_url) {
    ctx.rateLimiter.consume();
    await patchIssue(
      ctx,
      entityId,
      {
        status: "in_progress",
        pr_url: pr.pr_url,
        pr_number: pr.pr_number,
        branch,
        daemon_action: "pr_created",
      },
      `issue-pr-${entityId}-${Date.now()}`,
    );
    await postAgentMessage(
      ctx,
      {
        content: `Opened PR: ${pr.pr_url}`,
        turn_key: `${entityId}:daemon-pr-${Date.now()}`,
        issue_entity_id: entityId,
      },
      `conv-${entityId}-pr-${Date.now()}`,
    );
  } else {
    await notifyHumanNeeded(ctx, {
      reason: `gh_pr_create_failed:${pr.error || "unknown"}`,
      issueEntityId: entityId,
      sessionId,
      worktreePath,
      branch,
      baseCommit: base.baseCommit,
      rebasePolicy: base.rebase_policy_used,
      issueTitle: String(issue.title || issue.subject || "issue"),
      issueNumber,
    });
  }
}
