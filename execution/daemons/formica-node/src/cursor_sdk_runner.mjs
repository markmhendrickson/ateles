/**
 * Multi-turn Cursor TypeScript SDK agent with `local.cwd` = worktree.
 * @param {{
 *   worktreePath: string;
 *   prompt: string;
 *   apiKey?: string;
 *   modelId?: string;
 *   maxTurns?: number;
 *   pollOperator?: () => Promise<string | null>;
 * }} opts
 */
export async function runConversationalCursorSdk(opts) {
  const apiKey = (
    opts.apiKey ||
    process.env.CURSOR_API_KEY ||
    process.env.CURSOR_CLOUD_API_KEY ||
    ""
  ).trim();
  if (!apiKey) {
    return {
      ok: false,
      mode: "conversational.sdk",
      error: "CURSOR_API_KEY_or_CURSOR_CLOUD_API_KEY_required",
    };
  }

  let Agent;
  let CursorAgentError;
  try {
    const mod = await import("@cursor/sdk");
    Agent = mod.Agent;
    CursorAgentError = mod.CursorAgentError;
  } catch (e) {
    return {
      ok: false,
      mode: "conversational.sdk",
      error: "cursor_sdk_import_failed",
      stderr: String(e?.message || e),
    };
  }

  const modelId =
    opts.modelId ||
    process.env.FORMICA_CURSOR_MODEL?.trim() ||
    process.env.ISSUE_PROCESSOR_CURSOR_MODEL?.trim() ||
    "composer-2";
  const agent = Agent.create({
    apiKey,
    model: { id: modelId },
    local: { cwd: opts.worktreePath, settingSources: [] },
  });

  let transcript = "";
  /** @param {AsyncIterable<unknown>} stream */
  async function consumeStream(stream) {
    for await (const event of stream) {
      const ev = /** @type {Record<string, unknown>} */ (event);
      if (ev.type === "assistant" && ev.message && typeof ev.message === "object") {
        const content = /** @type {Record<string, unknown>} */ (ev.message).content;
        if (Array.isArray(content)) {
          for (const block of content) {
            const b = /** @type {Record<string, unknown>} */ (block);
            if (b.type === "text" && typeof b.text === "string") transcript += b.text;
          }
        }
      }
    }
  }

  try {
    const run1 = await agent.send(opts.prompt);
    if (typeof run1.stream === "function") {
      try {
        await consumeStream(run1.stream());
      } catch {
        /* stream unsupported or empty */
      }
    }
    const result1 = await run1.wait();
    if (result1.status === "error") {
      return {
        ok: false,
        mode: "conversational.sdk",
        stdout: transcript,
        error: "cursor_run_error",
        run_id: result1.id,
        cursor_agent_id: /** @type {{ agentId?: string }} */ (agent).agentId,
      };
    }

    const maxTurns = opts.maxTurns ?? 24;
    for (let turn = 1; turn < maxTurns && opts.pollOperator; turn++) {
      const msg = await opts.pollOperator();
      if (!msg) continue;
      if (/^\/(shipit|done)\b/i.test(msg.trim())) {
        return {
          ok: true,
          mode: "conversational.sdk",
          stdout: transcript,
          operator_stop: msg.trim(),
          cursor_agent_id: /** @type {{ agentId?: string }} */ (agent).agentId,
          turns: turn + 1,
        };
      }
      const next = [
        "Operator follow-up (via queue):",
        msg,
        "",
        "Continue the same task in this repo. When finished, output a line containing only DONE.",
      ].join("\n");
      const run2 = await agent.send(next);
      if (typeof run2.stream === "function") {
        try {
          transcript += "\n---\n";
          await consumeStream(run2.stream());
        } catch {
          /* ignore */
        }
      }
      const r2 = await run2.wait();
      if (r2.status === "error") {
        return {
          ok: false,
          mode: "conversational.sdk",
          stdout: transcript,
          error: "cursor_run_error_followup",
          run_id: r2.id,
          cursor_agent_id: /** @type {{ agentId?: string }} */ (agent).agentId,
        };
      }
      if (/\bDONE\b/m.test(transcript)) {
        return {
          ok: true,
          mode: "conversational.sdk",
          stdout: transcript,
          cursor_agent_id: /** @type {{ agentId?: string }} */ (agent).agentId,
          turns: turn + 1,
        };
      }
    }

    return {
      ok: true,
      mode: "conversational.sdk",
      stdout: transcript,
      cursor_agent_id: /** @type {{ agentId?: string }} */ (agent).agentId,
      note: "conversational_sdk_stopped_without_done",
    };
  } catch (e) {
    if (CursorAgentError && e instanceof CursorAgentError) {
      return {
        ok: false,
        mode: "conversational.sdk",
        error: "cursor_agent_error",
        stderr: String(e.message),
        isRetryable: Boolean(e.isRetryable),
      };
    }
    return {
      ok: false,
      mode: "conversational.sdk",
      error: "cursor_sdk_exception",
      stderr: String(e?.message || e),
    };
  } finally {
    try {
      if (typeof agent[Symbol.asyncDispose] === "function") {
        await agent[Symbol.asyncDispose]();
      }
    } catch {
      /* ignore */
    }
  }
}
