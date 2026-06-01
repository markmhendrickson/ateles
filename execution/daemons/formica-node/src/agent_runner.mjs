import { spawn } from "node:child_process";
import { runCommand } from "./exec.mjs";
import { runConversationalAnthropic } from "./anthropic_runner.mjs";
import { runConversationalCursorSdk } from "./cursor_sdk_runner.mjs";

/**
 * @param {string} command
 * @param {string[]} args
 * @param {string} worktreePath
 * @param {{ onStdout?: (s: string) => void }} [opts]
 */
function spawnOneshot(command, args, worktreePath, opts = {}) {
  return new Promise((resolve) => {
    const child = spawn(command, args, {
      cwd: worktreePath,
      env: { ...process.env, CI: process.env.CI || "1" },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let out = "";
    let err = "";
    child.stdout?.on("data", (c) => {
      out += c.toString();
      opts.onStdout?.(c.toString());
    });
    child.stderr?.on("data", (c) => {
      err += c.toString();
    });
    child.on("close", (code) => {
      resolve({ code: code ?? 1, stdout: out, stderr: err });
    });
    child.on("error", (e) => {
      resolve({ code: 127, stdout: out, stderr: err + String(e.message) });
    });
  });
}

/**
 * @param {{
 *   runtime: string;
 *   worktreePath: string;
 *   prompt: string;
 * }} opts
 */
async function runOneshotInner(opts) {
  const runtime = opts.runtime || "cursor";
  const prompt = opts.prompt;

  if (runtime === "claude_code") {
    const r = await spawnOneshot("claude", ["--print", prompt], opts.worktreePath);
    return { ok: r.code === 0, code: r.code, stdout: r.stdout, stderr: r.stderr };
  }

  if (runtime === "cursor") {
    try {
      const r = await runCommand(
        "cursor-agent",
        ["--workspace", opts.worktreePath, "--print", prompt],
        { cwd: opts.worktreePath },
      );
      return { ok: true, code: 0, stdout: r.stdout, stderr: r.stderr };
    } catch (e) {
      try {
        const r = await runCommand("cursor", ["agent", "--print", prompt], {
          cwd: opts.worktreePath,
        });
        return { ok: true, code: 0, stdout: r.stdout, stderr: r.stderr };
      } catch {
        return {
          ok: false,
          code: 127,
          stdout: "",
          stderr: String(e?.message || e),
          error: "cursor_cli_missing",
        };
      }
    }
  }

  return { ok: false, error: `unknown_runtime:${runtime}` };
}

/**
 * @param {{
 *   mode: string;
 *   runtime: string;
 *   worktreePath: string;
 *   prompt: string;
 *   maxTurns?: number;
 *   pollOperator?: () => Promise<string | null>;
 *   cursorSdkModel?: string;
 *   anthropicModel?: string;
 *   cursorApiKey?: string;
 *   anthropicApiKey?: string;
 * }} opts
 */
export async function runAgent(opts) {
  const mode = opts.mode || "oneshot";

  if (mode === "human_handoff") {
    return { ok: true, skipped: true, reason: "human_handoff" };
  }

  if (mode === "conversational.sdk") {
    return runConversationalCursorSdk({
      worktreePath: opts.worktreePath,
      prompt: opts.prompt,
      modelId: opts.cursorSdkModel,
      maxTurns: opts.maxTurns,
      pollOperator: opts.pollOperator,
      apiKey: opts.cursorApiKey,
    });
  }

  if (mode === "conversational.claude_api") {
    return runConversationalAnthropic({
      worktreePath: opts.worktreePath,
      prompt: opts.prompt,
      model: opts.anthropicModel,
      maxTurns: opts.maxTurns,
      pollOperator: opts.pollOperator,
      apiKey: opts.anthropicApiKey,
    });
  }

  return runOneshotInner(opts);
}
