import { storeEntities } from "./neotoma.mjs";

/**
 * Persist every allowlisted inbound Telegram text to Neotoma for audit (when mirror enabled).
 * @param {{ baseUrl: string; token: string; telegramInboundMirror?: boolean }} ctx
 * @param {Record<string, unknown>} msg Telegram `message` or `edited_message` object
 * @param {string} text Same text used for routing (trimmed optional — we store raw-ish)
 */
export async function mirrorTelegramInboundToNeotoma(ctx, msg, text) {
  if (!ctx.telegramInboundMirror) return;
  if (!ctx.baseUrl || !ctx.token) return;
  const chatId = msg.chat?.id;
  const messageId = msg.message_id;
  if (chatId == null || messageId == null) return;

  const idem = `telegram-inbound-${chatId}-${messageId}`;
  const threadId = msg.message_thread_id;
  const fromId = msg.from?.id;
  const username = msg.from?.username ? String(msg.from.username) : "";
  const prefix = `[telegram chat=${chatId} thread=${threadId ?? ""} from_user_id=${fromId}${username ? ` @${username}` : ""}]`;
  const full = `${prefix}\n${String(text)}`;
  const issueIdMatch = String(text).match(/\b(ent_[a-f0-9]{8,})\b/i);
  const turnKey = `telegram:${chatId}:${messageId}`;

  /** @type {Record<string, unknown>[]} */
  const relationships = [];
  if (issueIdMatch) {
    relationships.push({
      relationship_type: "REFERS_TO",
      source_index: 0,
      target_entity_id: issueIdMatch[1],
    });
  }

  try {
    await storeEntities(ctx.baseUrl, ctx.token, {
      idempotency_key: idem,
      entities: [
        {
          entity_type: "conversation_message",
          role: "user",
          sender_kind: "user",
          content: full,
          turn_key: turnKey,
          data_source: `Telegram Bot API getUpdates message_id=${messageId} chat_id=${chatId}`,
        },
      ],
      relationships,
    });
  } catch (e) {
    console.error("[formica] telegram mirror store failed:", e?.message || e);
  }
}
