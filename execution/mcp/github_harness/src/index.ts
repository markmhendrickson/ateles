#!/usr/bin/env node
/**
 * github_harness — MCP server for Ateles T4 agents.
 *
 * Provides GitHub operations (issues, PRs, branches) via MCP tools.
 * Identity model: PAT is loaded per-repo from env vars; agent passes
 * AAuth-signed context in the tool call's `aauth_context` parameter.
 *
 * Environment variables:
 *   GITHUB_TOKEN           Default PAT (fallback when no repo-specific token)
 *   ATELES_AGENT_PAT       PAT for the ateles-agent GitHub identity (ateles repo)
 *   NEOTOMA_AGENT_PAT      PAT for the neotoma-agent GitHub identity (neotoma repo)
 *   GITHUB_HARNESS_DEBUG   Set to "1" to log tool calls to stderr
 *
 * Transport: stdio (launched by claude CLI as an MCP server subprocess).
 *
 * Tool list:
 *   Issues:   list_issues, get_issue, create_issue, add_comment, update_labels
 *   PRs:      create_pr, get_pr, review_pr, merge_pr
 *   Branches: create_branch, list_branches
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { Octokit } from "@octokit/rest";

const DEBUG = process.env.GITHUB_HARNESS_DEBUG === "1";

function debug(msg: string) {
  if (DEBUG) process.stderr.write(`[github_harness] ${msg}\n`);
}

// ── PAT resolution ────────────────────────────────────────────────────────────

function resolveToken(owner: string, repo: string): string {
  const slug = `${owner}/${repo}`.toLowerCase();
  if (slug.includes("ateles")) {
    const pat = process.env.ATELES_AGENT_PAT;
    if (pat) return pat;
  }
  if (slug.includes("neotoma")) {
    const pat = process.env.NEOTOMA_AGENT_PAT;
    if (pat) return pat;
  }
  const fallback = process.env.GITHUB_TOKEN;
  if (!fallback) {
    throw new Error(
      `No GitHub token found for ${slug}. ` +
        "Set ATELES_AGENT_PAT, NEOTOMA_AGENT_PAT, or GITHUB_TOKEN."
    );
  }
  return fallback;
}

function octokit(owner: string, repo: string): Octokit {
  return new Octokit({ auth: resolveToken(owner, repo) });
}

// ── Tool helpers ──────────────────────────────────────────────────────────────

function requireString(params: Record<string, unknown>, key: string): string {
  const v = params[key];
  if (typeof v !== "string" || !v) throw new Error(`Missing required param: ${key}`);
  return v;
}

function optString(
  params: Record<string, unknown>,
  key: string,
  def = ""
): string {
  const v = params[key];
  return typeof v === "string" ? v : def;
}

function optNumber(
  params: Record<string, unknown>,
  key: string,
  def: number
): number {
  const v = params[key];
  return typeof v === "number" ? v : def;
}

function optStringArray(
  params: Record<string, unknown>,
  key: string
): string[] {
  const v = params[key];
  if (!Array.isArray(v)) return [];
  return v.filter((x): x is string => typeof x === "string");
}

/** One inline review comment anchored to a line of the PR diff. */
export interface ReviewComment {
  path: string;
  body: string;
  line: number;
  side?: "LEFT" | "RIGHT";
  start_line?: number;
}

/**
 * Coerce and validate an array of inline review comments.
 *
 * Anchors use `line` (a line number in the file) rather than the deprecated
 * `position` (an offset into the diff hunk), because callers have the file/line
 * from their findings but would have to recompute hunk offsets for `position`.
 *
 * Malformed entries are DROPPED rather than thrown: GitHub rejects the whole
 * review with 422 if any single anchor is invalid, so silently discarding a bad
 * anchor degrades one comment instead of losing the entire review. Callers that
 * need to know use the returned `dropped` count.
 */
export function parseReviewComments(
  params: Record<string, unknown>,
  key: string
): { comments: ReviewComment[]; dropped: number } {
  const v = params[key];
  if (!Array.isArray(v)) return { comments: [], dropped: 0 };

  const comments: ReviewComment[] = [];
  let dropped = 0;

  for (const raw of v) {
    if (typeof raw !== "object" || raw === null) {
      dropped++;
      continue;
    }
    const o = raw as Record<string, unknown>;
    const path = typeof o.path === "string" ? o.path.trim() : "";
    const body = typeof o.body === "string" ? o.body.trim() : "";
    const line = typeof o.line === "number" ? o.line : NaN;

    // path and body must be non-empty; line must be a positive integer.
    if (!path || !body || !Number.isInteger(line) || line < 1) {
      dropped++;
      continue;
    }

    const comment: ReviewComment = { path, body, line };

    if (o.side === "LEFT" || o.side === "RIGHT") {
      comment.side = o.side;
    }
    // A multi-line anchor must start at or before the end line.
    if (
      typeof o.start_line === "number" &&
      Number.isInteger(o.start_line) &&
      o.start_line >= 1 &&
      o.start_line <= line
    ) {
      comment.start_line = o.start_line;
    }

    comments.push(comment);
  }

  return { comments, dropped };
}

// ── MCP server ────────────────────────────────────────────────────────────────

const server = new Server(
  { name: "github_harness", version: "1.0.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    // ── Issues ──────────────────────────────────────────────────────────────
    {
      name: "list_issues",
      description:
        "List open issues in a GitHub repository. Returns number, title, state, labels, created_at.",
      inputSchema: {
        type: "object",
        properties: {
          owner: { type: "string", description: "GitHub owner (user or org)" },
          repo: { type: "string", description: "Repository name" },
          state: {
            type: "string",
            enum: ["open", "closed", "all"],
            description: "Filter by state (default: open)",
          },
          labels: {
            type: "string",
            description: "Comma-separated label names to filter by",
          },
          limit: {
            type: "number",
            description: "Max results (default 30, max 100)",
          },
        },
        required: ["owner", "repo"],
      },
    },
    {
      name: "get_issue",
      description:
        "Get a single GitHub issue by number. Returns full body, labels, comments count, state.",
      inputSchema: {
        type: "object",
        properties: {
          owner: { type: "string" },
          repo: { type: "string" },
          issue_number: { type: "number", description: "Issue number" },
        },
        required: ["owner", "repo", "issue_number"],
      },
    },
    {
      name: "create_issue",
      description: "Create a new GitHub issue.",
      inputSchema: {
        type: "object",
        properties: {
          owner: { type: "string" },
          repo: { type: "string" },
          title: { type: "string" },
          body: { type: "string", description: "Issue body (Markdown)" },
          labels: {
            type: "array",
            items: { type: "string" },
            description: "Labels to apply",
          },
          assignees: {
            type: "array",
            items: { type: "string" },
            description: "GitHub usernames to assign",
          },
        },
        required: ["owner", "repo", "title"],
      },
    },
    {
      name: "add_comment",
      description: "Add a comment to an issue or PR.",
      inputSchema: {
        type: "object",
        properties: {
          owner: { type: "string" },
          repo: { type: "string" },
          issue_number: { type: "number", description: "Issue or PR number" },
          body: { type: "string", description: "Comment body (Markdown)" },
        },
        required: ["owner", "repo", "issue_number", "body"],
      },
    },
    {
      name: "update_labels",
      description: "Set the labels on an issue or PR (replaces existing labels).",
      inputSchema: {
        type: "object",
        properties: {
          owner: { type: "string" },
          repo: { type: "string" },
          issue_number: { type: "number" },
          labels: {
            type: "array",
            items: { type: "string" },
            description: "Full label list to set",
          },
        },
        required: ["owner", "repo", "issue_number", "labels"],
      },
    },
    // ── Pull Requests ────────────────────────────────────────────────────────
    {
      name: "create_pr",
      description: "Create a pull request.",
      inputSchema: {
        type: "object",
        properties: {
          owner: { type: "string" },
          repo: { type: "string" },
          title: { type: "string" },
          body: { type: "string", description: "PR description (Markdown)" },
          head: {
            type: "string",
            description: "Head branch name (source of changes)",
          },
          base: {
            type: "string",
            description: "Base branch to merge into (default: main)",
          },
          draft: { type: "boolean", description: "Create as draft PR" },
        },
        required: ["owner", "repo", "title", "head"],
      },
    },
    {
      name: "get_pr",
      description:
        "Get a pull request by number. Returns title, body, state, head, base, mergeable, checks.",
      inputSchema: {
        type: "object",
        properties: {
          owner: { type: "string" },
          repo: { type: "string" },
          pull_number: { type: "number" },
        },
        required: ["owner", "repo", "pull_number"],
      },
    },
    {
      name: "review_pr",
      description: "Submit a review on a pull request.",
      inputSchema: {
        type: "object",
        properties: {
          owner: { type: "string" },
          repo: { type: "string" },
          pull_number: { type: "number" },
          event: {
            type: "string",
            enum: ["APPROVE", "REQUEST_CHANGES", "COMMENT"],
            description: "Review event type",
          },
          body: {
            type: "string",
            description: "Review comment body",
          },
          comments: {
            type: "array",
            description:
              "Optional inline review comments anchored to lines of the PR " +
              "diff. Each anchor MUST reference a line present in the diff, " +
              "or GitHub rejects the entire review (422). Use a ```suggestion " +
              "block in `body` to offer a one-click applicable fix.",
            items: {
              type: "object",
              properties: {
                path: {
                  type: "string",
                  description: "File path, relative to the repo root",
                },
                body: {
                  type: "string",
                  description:
                    "Comment text; may contain a ```suggestion fenced block",
                },
                line: {
                  type: "number",
                  description:
                    "Line number in the file (not a diff position). For a " +
                    "multi-line anchor this is the END line.",
                },
                side: {
                  type: "string",
                  enum: ["LEFT", "RIGHT"],
                  description:
                    "LEFT = pre-image (deleted/context), RIGHT = post-image " +
                    "(added/context). Defaults to RIGHT.",
                },
                start_line: {
                  type: "number",
                  description: "First line of a multi-line anchor",
                },
              },
              required: ["path", "body", "line"],
            },
          },
        },
        required: ["owner", "repo", "pull_number", "event"],
      },
    },
    {
      name: "merge_pr",
      description: "Merge a pull request (squash merge by default).",
      inputSchema: {
        type: "object",
        properties: {
          owner: { type: "string" },
          repo: { type: "string" },
          pull_number: { type: "number" },
          merge_method: {
            type: "string",
            enum: ["squash", "merge", "rebase"],
            description: "Merge strategy (default: squash)",
          },
          commit_title: { type: "string", description: "Squash commit title" },
          commit_message: {
            type: "string",
            description: "Squash commit message",
          },
        },
        required: ["owner", "repo", "pull_number"],
      },
    },
    // ── Branches ─────────────────────────────────────────────────────────────
    {
      name: "create_branch",
      description: "Create a new branch from a base ref.",
      inputSchema: {
        type: "object",
        properties: {
          owner: { type: "string" },
          repo: { type: "string" },
          branch: { type: "string", description: "New branch name" },
          from_ref: {
            type: "string",
            description: "Source branch or SHA (default: main)",
          },
        },
        required: ["owner", "repo", "branch"],
      },
    },
    {
      name: "list_branches",
      description: "List branches in a repository.",
      inputSchema: {
        type: "object",
        properties: {
          owner: { type: "string" },
          repo: { type: "string" },
          limit: { type: "number", description: "Max results (default 30)" },
        },
        required: ["owner", "repo"],
      },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  const params = (args ?? {}) as Record<string, unknown>;
  debug(`tool=${name} params=${JSON.stringify(params)}`);

  try {
    const result = await dispatch(name, params);
    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    debug(`error in ${name}: ${msg}`);
    return {
      content: [{ type: "text", text: `Error: ${msg}` }],
      isError: true,
    };
  }
});

// ── Tool dispatch ─────────────────────────────────────────────────────────────

async function dispatch(
  name: string,
  p: Record<string, unknown>
): Promise<unknown> {
  switch (name) {
    // ── Issues ──────────────────────────────────────────────────────────────
    case "list_issues": {
      const owner = requireString(p, "owner");
      const repo = requireString(p, "repo");
      const state = (optString(p, "state", "open") || "open") as
        | "open"
        | "closed"
        | "all";
      const labels = optString(p, "labels");
      const per_page = Math.min(optNumber(p, "limit", 30), 100);
      const oct = octokit(owner, repo);
      const { data } = await oct.rest.issues.listForRepo({
        owner,
        repo,
        state,
        labels: labels || undefined,
        per_page,
      });
      return data
        .filter((i) => !("pull_request" in i))
        .map((i) => ({
          number: i.number,
          title: i.title,
          state: i.state,
          labels: i.labels.map((l) => (typeof l === "string" ? l : l.name)),
          created_at: i.created_at,
          html_url: i.html_url,
        }));
    }

    case "get_issue": {
      const owner = requireString(p, "owner");
      const repo = requireString(p, "repo");
      const issue_number = optNumber(p, "issue_number", 0);
      if (!issue_number) throw new Error("Missing required param: issue_number");
      const oct = octokit(owner, repo);
      const { data } = await oct.rest.issues.get({ owner, repo, issue_number });
      return {
        number: data.number,
        title: data.title,
        body: data.body,
        state: data.state,
        labels: data.labels.map((l) => (typeof l === "string" ? l : l.name)),
        comments: data.comments,
        created_at: data.created_at,
        updated_at: data.updated_at,
        html_url: data.html_url,
        user: data.user?.login,
      };
    }

    case "create_issue": {
      const owner = requireString(p, "owner");
      const repo = requireString(p, "repo");
      const title = requireString(p, "title");
      const body = optString(p, "body");
      const labels = optStringArray(p, "labels");
      const assignees = optStringArray(p, "assignees");
      const oct = octokit(owner, repo);
      const { data } = await oct.rest.issues.create({
        owner,
        repo,
        title,
        body: body || undefined,
        labels: labels.length ? labels : undefined,
        assignees: assignees.length ? assignees : undefined,
      });
      return { number: data.number, html_url: data.html_url };
    }

    case "add_comment": {
      const owner = requireString(p, "owner");
      const repo = requireString(p, "repo");
      const issue_number = optNumber(p, "issue_number", 0);
      if (!issue_number) throw new Error("Missing required param: issue_number");
      const body = requireString(p, "body");
      const oct = octokit(owner, repo);
      const { data } = await oct.rest.issues.createComment({
        owner,
        repo,
        issue_number,
        body,
      });
      return { id: data.id, html_url: data.html_url };
    }

    case "update_labels": {
      const owner = requireString(p, "owner");
      const repo = requireString(p, "repo");
      const issue_number = optNumber(p, "issue_number", 0);
      if (!issue_number) throw new Error("Missing required param: issue_number");
      const labels = optStringArray(p, "labels");
      const oct = octokit(owner, repo);
      const { data } = await oct.rest.issues.setLabels({
        owner,
        repo,
        issue_number,
        labels,
      });
      return { labels: data.map((l) => l.name) };
    }

    // ── Pull Requests ────────────────────────────────────────────────────────
    case "create_pr": {
      const owner = requireString(p, "owner");
      const repo = requireString(p, "repo");
      const title = requireString(p, "title");
      const head = requireString(p, "head");
      const base = optString(p, "base", "main");
      const body = optString(p, "body");
      const draft = p.draft === true;
      const oct = octokit(owner, repo);
      const { data } = await oct.rest.pulls.create({
        owner,
        repo,
        title,
        head,
        base,
        body: body || undefined,
        draft,
      });
      return {
        number: data.number,
        html_url: data.html_url,
        state: data.state,
      };
    }

    case "get_pr": {
      const owner = requireString(p, "owner");
      const repo = requireString(p, "repo");
      const pull_number = optNumber(p, "pull_number", 0);
      if (!pull_number) throw new Error("Missing required param: pull_number");
      const oct = octokit(owner, repo);
      const { data } = await oct.rest.pulls.get({ owner, repo, pull_number });
      return {
        number: data.number,
        title: data.title,
        body: data.body,
        state: data.state,
        draft: data.draft,
        mergeable: data.mergeable,
        head: data.head.ref,
        base: data.base.ref,
        html_url: data.html_url,
        user: data.user?.login,
        created_at: data.created_at,
        updated_at: data.updated_at,
      };
    }

    case "review_pr": {
      const owner = requireString(p, "owner");
      const repo = requireString(p, "repo");
      const pull_number = optNumber(p, "pull_number", 0);
      if (!pull_number) throw new Error("Missing required param: pull_number");
      const event = requireString(p, "event") as
        | "APPROVE"
        | "REQUEST_CHANGES"
        | "COMMENT";
      const body = optString(p, "body");
      const { comments, dropped } = parseReviewComments(p, "comments");
      const oct = octokit(owner, repo);

      const base = {
        owner,
        repo,
        pull_number,
        event,
        body: body || undefined,
      };

      if (comments.length === 0) {
        const { data } = await oct.rest.pulls.createReview(base);
        return { id: data.id, state: data.state, inline_comments: 0, dropped };
      }

      // GitHub rejects the ENTIRE review with 422 when any anchor falls outside
      // the diff — and callers often cannot know that ahead of time (a lens
      // agent reviewing a diff without a checkout is inferring line numbers).
      // Degrade to a body-only review rather than losing the review outright,
      // and report which path was taken so a silent fallback is distinguishable
      // from a successful inline review.
      try {
        const { data } = await oct.rest.pulls.createReview({
          ...base,
          comments,
        });
        return {
          id: data.id,
          state: data.state,
          inline_comments: comments.length,
          dropped,
        };
      } catch (err) {
        const status = (err as { status?: number })?.status;
        if (status !== 422) throw err;
        const { data } = await oct.rest.pulls.createReview(base);
        return {
          id: data.id,
          state: data.state,
          inline_comments: 0,
          dropped,
          degraded: "inline anchors rejected by GitHub (422); posted body-only",
        };
      }
    }

    case "merge_pr": {
      const owner = requireString(p, "owner");
      const repo = requireString(p, "repo");
      const pull_number = optNumber(p, "pull_number", 0);
      if (!pull_number) throw new Error("Missing required param: pull_number");
      const merge_method = (optString(p, "merge_method", "squash") || "squash") as
        | "squash"
        | "merge"
        | "rebase";
      const commit_title = optString(p, "commit_title");
      const commit_message = optString(p, "commit_message");
      const oct = octokit(owner, repo);
      const { data } = await oct.rest.pulls.merge({
        owner,
        repo,
        pull_number,
        merge_method,
        commit_title: commit_title || undefined,
        commit_message: commit_message || undefined,
      });
      return { merged: data.merged, sha: data.sha, message: data.message };
    }

    // ── Branches ─────────────────────────────────────────────────────────────
    case "create_branch": {
      const owner = requireString(p, "owner");
      const repo = requireString(p, "repo");
      const branch = requireString(p, "branch");
      const from_ref = optString(p, "from_ref", "main");
      const oct = octokit(owner, repo);
      // Resolve the base ref to a SHA
      const { data: refData } = await oct.rest.git.getRef({
        owner,
        repo,
        ref: `heads/${from_ref}`,
      });
      const sha = refData.object.sha;
      const { data } = await oct.rest.git.createRef({
        owner,
        repo,
        ref: `refs/heads/${branch}`,
        sha,
      });
      return { ref: data.ref, sha: data.object.sha };
    }

    case "list_branches": {
      const owner = requireString(p, "owner");
      const repo = requireString(p, "repo");
      const per_page = Math.min(optNumber(p, "limit", 30), 100);
      const oct = octokit(owner, repo);
      const { data } = await oct.rest.repos.listBranches({
        owner,
        repo,
        per_page,
      });
      return data.map((b) => ({ name: b.name, sha: b.commit.sha }));
    }

    default:
      throw new Error(`Unknown tool: ${name}`);
  }
}

// ── Start ─────────────────────────────────────────────────────────────────────

const transport = new StdioServerTransport();
await server.connect(transport);
debug("github_harness MCP server running on stdio");
