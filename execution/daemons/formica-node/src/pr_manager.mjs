import { git } from "./exec.mjs";
import { runCommand } from "./exec.mjs";

/**
 * @param {string} worktreePath
 * @param {{ execGit?: typeof git }} [x]
 */
export async function gitStatusPorcelain(worktreePath, x = {}) {
  const execGit = x.execGit || git;
  const { stdout } = await execGit(worktreePath, ["status", "--porcelain"]);
  return stdout;
}

/**
 * @param {string} worktreePath
 * @param {string} policy abort | auto_commit_bot
 * @param {{ execGit?: typeof git }} [x]
 */
export async function preflightDirtyTree(worktreePath, policy, x = {}) {
  const porcelain = (await gitStatusPorcelain(worktreePath, x)).trim();
  if (!porcelain) return { ok: true, mode: "clean" };
  if (policy === "auto_commit_bot") {
    const execGit = x.execGit || git;
    await execGit(worktreePath, ["add", "-A"]);
    await execGit(worktreePath, [
      "commit",
      "-m",
      "chore(formica): auto-commit bot preflight",
    ]);
    return { ok: true, mode: "auto_committed" };
  }
  return { ok: false, mode: "dirty", porcelain };
}

/**
 * @param {string} worktreePath
 * @param {string} defaultBranch
 * @param {boolean} align
 * @param {{ execGit?: typeof git }} [x]
 */
export async function maybeRebaseOntoDefault(worktreePath, defaultBranch, align, x = {}) {
  if (!align) return { ok: true, skipped: true };
  const execGit = x.execGit || git;
  try {
    await execGit(worktreePath, ["fetch", "origin", defaultBranch]);
    await execGit(worktreePath, ["rebase", `origin/${defaultBranch}`]);
    return { ok: true, skipped: false };
  } catch (e) {
    return { ok: false, error: String(e?.message || e) };
  }
}

/**
 * @param {{
 *   worktreePath: string;
 *   branch: string;
 *   title: string;
 *   body: string;
 *   prBaseBranch: string;
 *   dryRun?: boolean;
 *   runGh?: typeof runGhCreatePr;
 * }} opts
 */
export async function runGhCreatePr(opts) {
  if (opts.dryRun) {
    return { ok: true, dryRun: true, pr_url: null, pr_number: null };
  }
  const run = opts.runGh || runGhCreatePrImpl;
  return run(opts);
}

async function runGhCreatePrImpl(opts) {
  const args = [
    "pr",
    "create",
    "--head",
    opts.branch,
    "--title",
    opts.title,
    "--body",
    opts.body,
    "--base",
    opts.prBaseBranch,
  ];
  try {
    const { stdout } = await runCommand("gh", args, { cwd: opts.worktreePath });
    const m = stdout.match(/https:\/\/github\.com\/[^\s]+\/pull\/(\d+)/);
    const pr_number = m ? Number(m[1]) : null;
    return { ok: true, pr_url: stdout.trim().split("\n").pop() || stdout.trim(), pr_number };
  } catch (e) {
    return { ok: false, error: String(e?.message || e) };
  }
}

/**
 * @param {string} worktreePath
 * @param {string} branch
 * @param {{ execGit?: typeof git }} [x]
 */
export async function gitPushBranch(worktreePath, branch, x = {}) {
  const execGit = x.execGit || git;
  await execGit(worktreePath, ["push", "-u", "origin", branch]);
}

/**
 * @param {string} worktreePath
 * @param {string} baseCommit
 * @param {{ execGit?: typeof git }} [x]
 */
export async function countCommitsAheadOfBase(worktreePath, baseCommit, x = {}) {
  const execGit = x.execGit || git;
  try {
    const { stdout } = await execGit(worktreePath, [
      "rev-list",
      "--count",
      `${baseCommit}..HEAD`,
    ]);
    return Number(stdout.trim()) || 0;
  } catch {
    return 0;
  }
}
