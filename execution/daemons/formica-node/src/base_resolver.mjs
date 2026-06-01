import { git } from "./exec.mjs";

/** @param {string} v */
function parseSemver(v) {
  if (!v || typeof v !== "string") return null;
  const m = v.trim().replace(/^v/i, "").match(/^(\d+)\.(\d+)\.(\d+)/);
  if (!m) return null;
  return [Number(m[1]), Number(m[2]), Number(m[3])];
}

/** @param {[number,number,number]} a @param {[number,number,number]} b */
function cmpSemver(a, b) {
  for (let i = 0; i < 3; i++) {
    if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1;
  }
  return 0;
}

/**
 * @param {string} lsRemoteOut
 * @returns {string[]}
 */
export function parseRemoteSemverTags(lsRemoteOut) {
  const tags = [];
  for (const line of lsRemoteOut.split("\n")) {
    const m = line.match(/refs\/tags\/(v?\d+\.\d+\.\d+(?:[-+].*)?)$/);
    if (m) tags.push(m[1]);
  }
  return [...new Set(tags)];
}

/**
 * @param {string[]} tags
 * @param {string} reporterVersion
 */
export function pickMinReleaseTag(tags, reporterVersion) {
  const base = parseSemver(reporterVersion);
  if (!base) return null;
  let best = null;
  let bestTuple = /** @type {[number,number,number] | null} */ (null);
  for (const t of tags) {
    const tuple = parseSemver(t);
    if (!tuple) continue;
    if (cmpSemver(tuple, base) < 0) continue;
    if (!bestTuple || cmpSemver(tuple, bestTuple) > 0) {
      best = t;
      bestTuple = tuple;
    }
  }
  return best;
}

/**
 * @param {{
 *   repoPath: string;
 *   defaultBranch: string;
 *   policy: string;
 *   issue: Record<string, unknown>;
 *   execGit?: typeof git;
 * }} opts
 */
export async function resolveBaseCommit(opts) {
  const execGit = opts.execGit || git;
  const issue = opts.issue;
  const defaultBranch = opts.defaultBranch || "main";
  const policy = String(issue.rebase_policy || opts.policy || "strict_reporter");
  const repoPath = opts.repoPath;

  await execGit(repoPath, ["fetch", "origin", "--tags"]);

  if (policy === "mainline") {
    const { stdout } = await execGit(repoPath, ["rev-parse", `origin/${defaultBranch}`]);
    const baseCommit = stdout.trim();
    return { ok: true, baseCommit, rebase_policy_used: policy };
  }

  if (policy === "strict_reporter") {
    const sha = issue.reporter_git_sha ? String(issue.reporter_git_sha).trim() : "";
    if (!sha) {
      return { ok: false, reason: "missing_sha", rebase_policy_used: policy };
    }
    try {
      await execGit(repoPath, ["rev-parse", "--verify", `${sha}^{commit}`]);
    } catch {
      try {
        await execGit(repoPath, ["fetch", "origin", sha]);
      } catch {
        /* shallow fetch may still fail */
      }
      try {
        await execGit(repoPath, ["rev-parse", "--verify", `${sha}^{commit}`]);
      } catch {
        return { ok: false, reason: "sha_unreachable", rebase_policy_used: policy };
      }
    }
    return { ok: true, baseCommit: sha, rebase_policy_used: policy };
  }

  if (policy === "min_release_ge_reporter") {
    const reporterVer = issue.reporter_app_version ? String(issue.reporter_app_version) : "";
    const { stdout } = await execGit(repoPath, ["ls-remote", "--tags", "origin"]);
    const tag = pickMinReleaseTag(parseRemoteSemverTags(stdout), reporterVer);
    if (!tag) {
      return { ok: false, reason: "no_suitable_tag", rebase_policy_used: policy };
    }
    const { stdout: rev } = await execGit(repoPath, ["rev-parse", `refs/tags/${tag}^{}`]);
    const baseCommit = rev.trim();
    if (!baseCommit) {
      return { ok: false, reason: "tag_not_resolved", rebase_policy_used: policy };
    }
    return { ok: true, baseCommit, rebase_policy_used: policy };
  }

  return { ok: false, reason: "unknown_policy", rebase_policy_used: policy };
}
