import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileP = promisify(execFile);

/**
 * @param {string} cmd
 * @param {string[]} args
 * @param {import('node:child_process').ExecFileOptionsWithStringEncoding} [opts]
 */
export async function runCommand(cmd, args, opts = {}) {
  const { stdout, stderr } = await execFileP(cmd, args, {
    maxBuffer: 20 * 1024 * 1024,
    encoding: "utf8",
    ...opts,
  });
  return { stdout: stdout || "", stderr: stderr || "" };
}

/**
 * @param {string} repoPath
 * @param {string[]} args
 */
export async function git(repoPath, args) {
  return runCommand("git", args, { cwd: repoPath });
}
