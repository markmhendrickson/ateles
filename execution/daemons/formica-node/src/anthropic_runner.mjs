/**
 * Multi-turn Anthropic Messages API (no tools) with operator queue between turns.
 * @param {{
 *   worktreePath: string;
 *   prompt: string;
 *   apiKey?: string;
 *   model?: string;
 *   maxTurns?: number;
 *   pollOperator?: () => Promise<string | null>;
 *   fetchImpl?: typeof fetch;
 * }} opts
 */
export async function runConversationalAnthropic(opts) {
  const apiKey = (opts.apiKey || process.env.ANTHROPIC_API_KEY || "").trim();
  if (!apiKey) {
    return { ok: false, mode: "conversational.claude_api", error: "ANTHROPIC_API_KEY_required" };
  }
  const model =
    opts.model ||
    process.env.FORMICA_ANTHROPIC_MODEL?.trim() ||
    process.env.ISSUE_PROCESSOR_ANTHROPIC_MODEL?.trim() ||
    "claude-sonnet-4-20250514";
  const fetchFn = opts.fetchImpl || fetch;
  const system = [
    "You are an autonomous coding agent.",
    `Working copy root (read-only context for you): ${opts.worktreePath}`,
    "Describe concrete file edits as clearly as possible; the host shell applies them outside this chat.",
    "When the assigned task is complete, output a single line containing only the word DONE.",
  ].join(" ");

  /** @type {{ role: string; content: unknown }[]} */
  const messages = [{ role: "user", content: opts.prompt }];
  const transcriptParts = [];
  const maxRounds = opts.maxTurns ?? 16;
  let lastMessageId = "";

  for (let round = 0; round < maxRounds; round++) {
    const res = await fetchFn("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "x-api-key": apiKey,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
      },
      body: JSON.stringify({
        model,
        max_tokens: 16384,
        system,
        messages,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      return {
        ok: false,
        mode: "conversational.claude_api",
        error: "anthropic_http",
        status: res.status,
        stderr: JSON.stringify(data).slice(0, 800),
        stdout: transcriptParts.join("\n---\n"),
        anthropic_message_id: lastMessageId || undefined,
      };
    }
    lastMessageId = typeof data.id === "string" ? data.id : "";
    const blocks = Array.isArray(data.content) ? data.content : [];
    const text = blocks
      .filter((b) => b && b.type === "text")
      .map((b) => b.text)
      .join("");
    transcriptParts.push(text);
    messages.push({ role: "assistant", content: text });

    if (/\bDONE\b/m.test(text)) {
      return {
        ok: true,
        mode: "conversational.claude_api",
        stdout: transcriptParts.join("\n---\n"),
        turns: round + 1,
        anthropic_message_id: lastMessageId,
      };
    }

    if (!opts.pollOperator) {
      return {
        ok: true,
        mode: "conversational.claude_api",
        stdout: transcriptParts.join("\n---\n"),
        note: "no_operator_poll_configured",
        anthropic_message_id: lastMessageId,
      };
    }

    const op = await opts.pollOperator();
    if (!op) {
      return {
        ok: true,
        mode: "conversational.claude_api",
        stdout: transcriptParts.join("\n---\n"),
        note: "operator_poll_empty",
        anthropic_message_id: lastMessageId,
      };
    }
    if (/^\/(shipit|done)\b/i.test(op.trim())) {
      return {
        ok: true,
        mode: "conversational.claude_api",
        stdout: transcriptParts.join("\n---\n"),
        operator_stop: op.trim(),
        anthropic_message_id: lastMessageId,
      };
    }
    messages.push({ role: "user", content: op });
  }

  return {
    ok: true,
    mode: "conversational.claude_api",
    stdout: transcriptParts.join("\n---\n"),
    note: "max_rounds",
    anthropic_message_id: lastMessageId,
  };
}
