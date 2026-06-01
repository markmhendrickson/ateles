import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import EventSource from "eventsource";
import { loadConfig } from "./config.mjs";
import {
  findReusableSseSubscription,
  listSubscriptions,
  subscribe,
} from "./neotoma.mjs";
import { handleIssueSubstrateEvent } from "./handlers/issue.mjs";
import { OperatorQueue } from "./operator_queue.mjs";
import { normalizeTelegramConfig, telegramLongPollLoop } from "./operator_transport.mjs";
import { mirrorTelegramInboundToNeotoma } from "./telegram_mirror.mjs";
import { resumePendingPrAutomation } from "./pipeline.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const RUN_DIR = path.join(__dirname, "..", ".run");

function requireToken() {
  const t = process.env.NEOTOMA_BEARER_TOKEN?.trim();
  if (!t) {
    console.error(
      "NEOTOMA_BEARER_TOKEN is required (e.g. output of `neotoma auth mcp-token` or server NEOTOMA_BEARER_TOKEN).",
    );
    process.exit(1);
  }
  return t;
}

/**
 * @param {string} baseUrl
 * @param {string} token
 * @param {Record<string, unknown>} cfg
 */
async function ensureSubscriptionId(baseUrl, token, cfg) {
  const existing =
    process.env.NEOTOMA_FORMICA_SUBSCRIPTION_ID?.trim() ||
    process.env.NEOTOMA_ISSUE_PROCESSOR_SUBSCRIPTION_ID?.trim();
  if (existing) {
    console.error(
      `[formica] Using subscription id from NEOTOMA_FORMICA_SUBSCRIPTION_ID or NEOTOMA_ISSUE_PROCESSOR_SUBSCRIPTION_ID=${existing}`,
    );
    return existing;
  }

  const subCfg = cfg.subscription || {};
  const entityTypes = Array.isArray(subCfg.entity_types) ? subCfg.entity_types : ["issue"];
  const eventTypes = Array.isArray(subCfg.event_types) ? subCfg.event_types : ["entity.created"];
  const deliveryMethod = subCfg.delivery_method === "webhook" ? "webhook" : "sse";

  const listed = await listSubscriptions(baseUrl, token);
  const rows = listed.subscriptions ?? listed;
  const reuse = findReusableSseSubscription(
    Array.isArray(rows) ? rows : [],
    entityTypes.map(String),
    eventTypes.map(String),
  );
  if (reuse) {
    console.error(`[formica] Reusing existing SSE subscription_id=${reuse}`);
    return reuse;
  }

  const body = {
    entity_types: entityTypes,
    event_types: eventTypes,
    delivery_method: deliveryMethod,
    ...(deliveryMethod === "webhook" && subCfg.webhook_url
      ? { webhook_url: String(subCfg.webhook_url) }
      : {}),
  };

  const created = await subscribe(baseUrl, token, body);
  const sid = created.subscription_id;
  if (!sid) {
    console.error("Subscribe response missing subscription_id:", created);
    process.exit(1);
  }
  console.error(
    `[formica] Created subscription_id=${sid} — set NEOTOMA_FORMICA_SUBSCRIPTION_ID (or legacy NEOTOMA_ISSUE_PROCESSOR_SUBSCRIPTION_ID) to reuse on next start.`,
  );
  return String(sid);
}

/**
 * @param {string} baseUrl
 * @param {string} token
 * @param {string} subscriptionId
 * @param {Record<string, unknown>} cfg
 * @param {Record<string, unknown>} ctx
 */
function connectStream(baseUrl, token, subscriptionId, cfg, ctx) {
  const url = `${baseUrl}/events/stream?subscription_id=${encodeURIComponent(subscriptionId)}`;
  const checkpointPath =
    process.env.FORMICA_SSE_CHECKPOINT?.trim() ||
    process.env.ISSUE_PROCESSOR_SSE_CHECKPOINT?.trim() ||
    path.join(RUN_DIR, "last_event_id.txt");
  let lastEventId = "";
  try {
    if (fs.existsSync(checkpointPath)) {
      lastEventId = fs.readFileSync(checkpointPath, "utf8").trim();
    }
  } catch {
    /* ignore */
  }

  const headers = {
    Authorization: `Bearer ${token}`,
    ...(lastEventId ? { "Last-Event-ID": lastEventId } : {}),
  };

  const es = new EventSource(url, { headers });

  const subCfg = cfg.subscription || {};
  const eventTypes = Array.isArray(subCfg.event_types)
    ? subCfg.event_types.map(String)
    : ["entity.created", "entity.updated"];

  let chain = Promise.resolve();

  for (const et of eventTypes) {
    es.addEventListener(et, (e) => {
      try {
        if (e && "lastEventId" in e && e.lastEventId) {
          lastEventId = String(e.lastEventId);
          try {
            fs.mkdirSync(path.dirname(checkpointPath), { recursive: true });
            fs.writeFileSync(checkpointPath, lastEventId, "utf8");
          } catch {
            /* ignore */
          }
        }
        const data = JSON.parse(e.data);
        chain = chain
          .then(() => handleIssueSubstrateEvent(data, ctx))
          .catch((err) => console.error("[formica] handler error", err));
      } catch (err) {
        console.error("[formica] Failed to parse event", et, err);
      }
    });
  }

  es.addEventListener("ping", () => {
    if (process.env.DEBUG_FORMICA === "1" || process.env.DEBUG_ISSUE_PROCESSOR === "1") {
      console.error("[formica] sse ping");
    }
  });

  es.onerror = (err) => {
    console.error("[formica] EventSource error", err?.message || err);
  };

  es.onopen = () => {
    console.error("[formica] SSE connected");
  };

  return es;
}

/**
 * @param {Record<string, unknown>} cfg
 * @param {Record<string, unknown>} ctx
 */
function startTelegramSidecar(cfg, ctx) {
  const ot = cfg.operator_transport || {};
  if (String(ot.backend || "none") !== "telegram") return null;
  const t = normalizeTelegramConfig(ot);
  if (!t.token || !t.chatId) {
    console.error("[formica] Telegram backend enabled but token/chat_id missing");
    return null;
  }
  ctx.telegramInboundMirror = Boolean(t.mirror);
  ctx.telegram = {
    token: t.token,
    chatId: t.chatId,
    allowedUserIds: t.allowedUserIds,
    useThreads: t.useThreads,
    defaultMessageThreadId: ot.telegram_message_thread_id
      ? Number(ot.telegram_message_thread_id)
      : undefined,
  };

  const ac = new AbortController();
  void telegramLongPollLoop({
    token: t.token,
    allowedUserIds: t.allowedUserIds,
    chatId: t.chatId,
    signal: ac.signal,
    onUpdate: (u) => {
      const msg = u.message || u.edited_message;
      const text = msg?.text ? String(msg.text) : "";
      if (!text.trim()) return;
      void mirrorTelegramInboundToNeotoma(ctx, msg, text);
      const threadId = Number(msg?.message_thread_id ?? 0);
      const trimmed = text.trim();
      if (/^\/(shipit|approve)\b/i.test(trimmed)) {
        let job = ctx.pendingPrByThread.get(threadId) || ctx.pendingPrByThread.get(0);
        if (!job && ctx.pendingPrByIssue.size === 1) {
          job = [...ctx.pendingPrByIssue.values()][0];
        }
        if (!job) {
          const m = text.match(/ent_[a-f0-9]+/i);
          if (m) job = ctx.pendingPrByIssue.get(m[0]);
        }
        if (job) {
          void resumePendingPrAutomation(ctx, job).then(() => {
            for (const [k, v] of [...ctx.pendingPrByThread.entries()]) {
              if (v === job) ctx.pendingPrByThread.delete(k);
            }
            ctx.pendingPrByIssue.delete(job.issueEntityId);
          });
        } else {
          console.error("[formica] /shipit with no pending PR job for this thread");
        }
        return;
      }
      const m = text.match(/ent_[a-f0-9]+/i);
      if (m) ctx.operatorQueue.enqueue(m[0], text);
    },
  }).catch((e) => console.error("[formica] telegram loop died", e));

  return ac;
}

async function main() {
  const cfg = loadConfig();
  const token = requireToken();
  const baseUrl = String(cfg.neotoma?.base_url || "http://localhost:3080");

  await fs.promises.mkdir(RUN_DIR, { recursive: true });
  const pidPath = path.join(RUN_DIR, "daemon.pid");
  fs.writeFileSync(pidPath, String(process.pid), "utf8");

  const ctx = {
    cfg,
    baseUrl,
    token,
    operatorQueue: new OperatorQueue(),
    pendingPrByThread: new Map(),
    pendingPrByIssue: new Map(),
    rateLimiter: null,
    telegram: null,
    telegramInboundMirror: false,
  };

  const telegramAbort = startTelegramSidecar(cfg, ctx);

  const subscriptionId = await ensureSubscriptionId(baseUrl, token, cfg);
  connectStream(baseUrl, token, subscriptionId, cfg, ctx);

  console.error("[formica] Running. Ctrl+C to exit.");

  const shutdown = () => {
    try {
      telegramAbort?.abort();
    } catch {
      /* ignore */
    }
    try {
      fs.unlinkSync(pidPath);
    } catch {
      /* ignore */
    }
    process.exit(0);
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
}

void main().catch((e) => {
  console.error(e);
  process.exit(1);
});
