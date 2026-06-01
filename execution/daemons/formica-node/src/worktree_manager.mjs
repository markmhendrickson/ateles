import fs from "node:fs/promises";
import path from "node:path";
import { git } from "./exec.mjs";

/**
 * @param {string} worktreeBase
 * @param {number} maxAgeMs
 */
export async function cleanupOldWorktrees(worktreeBase, maxAgeMs = 7 * 24 * 60 * 60 * 1000) {
  try {
    const entries = await fs.readdir(worktreeBase, { withFileTypes: true });
    const now = Date.now();
    for (const ent of entries) {
      if (!ent.isDirectory()) continue;
      const p = path.join(worktreeBase, ent.name);
      const st = await fs.stat(p);
      if (now - st.mtimeMs > maxAgeMs) {
        // Caller must remove git worktree registration; best-effort rmdir log
        console.error(`[formica] stale worktree candidate (manual cleanup): ${p}`);
      }
    }
  } catch {
    /* missing dir */
  }
}

/**
 * @param {{
 *   repoPath: string;
 *   worktreeBase: string;
 *   issueNumber: string | number;
 *   slug: string;
 *   baseCommit: string;
 *   execGit?: typeof git;
 * }} opts
 */
export async function createIssueWorktree(opts) {
  const execGit = opts.execGit || git;
  const issueNumber = String(opts.issueNumber);
  const slug = opts.slug.replace(/[^a-z0-9_-]/gi, "_");
  await fs.mkdir(opts.worktreeBase, { recursive: true });
  const worktreePath = path.join(opts.worktreeBase, `${slug}-issue-${issueNumber}`);
  const branch = `fix/issue-${issueNumber}`;
  await execGit(opts.repoPath, ["worktree", "add", "-b", branch, worktreePath, opts.baseCommit]);
  return { worktreePath, branch };
}

/**
 * @param {string} repoPath
 * @param {string} worktreePath
 * @param {{ execGit?: typeof git }} [x]
 */
export async function removeWorktree(repoPath, worktreePath, x = {}) {
  const execGit = x.execGit || git;
  try {
    await execGit(repoPath, ["worktree", "remove", "--force", worktreePath]);
  } catch (e) {
    console.error("[formica] worktree remove failed:", e?.message || e);
  }
}

/**
 * @param {string} worktreePath
 * @param {string} patchText
 * @param {{ execGit?: typeof git }} [x]
 */
export async function applyPatchInWorktree(worktreePath, patchText, x = {}) {
  const execGit = x.execGit || git;
  const tmp = path.join(worktreePath, ".formica_reporter.patch");
  await fs.writeFile(tmp, patchText, "utf8");
  try {
    const { stdout, stderr } = await execGit(worktreePath, ["apply", "--verbose", tmp]);
    await fs.unlink(tmp).catch(() => {});
    return { ok: true, stdout, stderr };
  } catch (e) {
    await fs.unlink(tmp).catch(() => {});
    return { ok: false, error: String(e?.message || e) };
  }
}
