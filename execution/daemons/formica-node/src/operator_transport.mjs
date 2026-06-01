/**
 * @param {Record<string, unknown>} cfg
 */
export function expandEnvInString(s) {
  if (typeof s !== "string") return s;
  return s.replace(/\$\{([A-Z0-9_]+)\}/g, (_, name) => process.env[name] || "");
}

/**
 * @param {Record<string, unknown>} transportCfg
 */
export function normalizeTelegramConfig(transportCfg) {
  const t = transportCfg && typeof transportCfg === "object" ? transportCfg : {};
  const token = String(expandEnvInString(t.telegram_bot_token) || "").trim();
  const chatId = String(expandEnvInString(t.telegram_chat_id) || "").trim();
  const allowed = Array.isArray(t.telegram_allowed_user_ids)
    ? t.telegram_allowed_user_ids.map((x) => Number(x))
    : [];
  return {
    token,
    chatId,
    allowedUserIds: new Set(allowed.filter((n) => Number.isFinite(n))),
    useThreads: Boolean(t.use_message_threads),
    mirror: Boolean(t.mirror_to_neotoma),
  };
}

/**
 * @param {{
 *   token: string;
 *   chatId: string;
 *   text: string;
 *   threadId?: number;
 * }} opts
 */
export async function telegramSendMessage(opts) {
  const body = {
    chat_id: opts.chatId,
    text: opts.text.slice(0, 3500),
    disable_web_page_preview: true,
  };
  const tid = Number(opts.threadId);
  if (Number.isFinite(tid) && tid > 0) {
    body.message_thread_id = tid;
  }
  const res = await fetch(`https://api.telegram.org/bot${opts.token}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const j = await res.json().catch(() => ({}));
  if (!res.ok || !j.ok) {
    throw new Error(`telegram sendMessage failed: ${res.status} ${JSON.stringify(j).slice(0, 400)}`);
  }
  return j.result;
}

/**
 * @param {{
 *   token: string;
 *   allowedUserIds: Set<number>;
 *   chatId: string;
 *   onUpdate: (u: Record<string, unknown>) => void;
 *   signal?: AbortSignal;
 * }} opts
 */
export async function telegramLongPollLoop(opts) {
  let offset = 0;
  while (!opts.signal?.aborted) {
    try {
      const url = new URL(`https://api.telegram.org/bot${opts.token}/getUpdates`);
      url.searchParams.set("timeout", "25");
      url.searchParams.set("offset", String(offset));
      const res = await fetch(url, { signal: opts.signal });
      const j = await res.json();
      if (!j.ok) {
        await new Promise((r) => setTimeout(r, 3000));
        continue;
      }
      for (const u of j.result || []) {
        offset = u.update_id + 1;
        const msg = u.message || u.edited_message;
        if (!msg) continue;
        if (String(msg.chat?.id) !== String(opts.chatId)) continue;
        const fromId = msg.from?.id;
        if (fromId == null || !opts.allowedUserIds.has(Number(fromId))) continue;
        opts.onUpdate(u);
      }
    } catch (e) {
      if (opts.signal?.aborted) break;
      if (e?.name === "AbortError") break;
      await new Promise((r) => setTimeout(r, 4000));
    }
  }
}
