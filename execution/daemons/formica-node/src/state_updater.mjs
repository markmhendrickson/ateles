import { createRelationship, storeEntities } from "./neotoma.mjs";

/**
 * @param {unknown} res
 * @param {string} [entityType]
 */
export function extractEntityId(res, entityType) {
  const list = res && typeof res === "object" && "entities" in res ? res.entities : null;
  if (!Array.isArray(list)) return null;
  const row = entityType ? list.find((e) => e && e.entity_type === entityType) : list[0];
  const id = row?.entity_id;
  return id ? String(id) : null;
}

/**
 * @param {{ baseUrl: string; token: string }} ctx
 * @param {Record<string, unknown>} fields
 * @param {string} idempotencyKey
 */
export async function storeDaemonSession(ctx, fields, idempotencyKey) {
  const res = await storeEntities(ctx.baseUrl, ctx.token, {
    idempotency_key: idempotencyKey,
    entities: [{ entity_type: "daemon_session", ...fields }],
  });
  return { raw: res, entity_id: extractEntityId(res, "daemon_session") };
}

/**
 * @param {{ baseUrl: string; token: string }} ctx
 * @param {string} issueEntityId
 * @param {Record<string, unknown>} fields
 * @param {string} idempotencyKey
 */
export async function patchIssue(ctx, issueEntityId, fields, idempotencyKey) {
  return storeEntities(ctx.baseUrl, ctx.token, {
    idempotency_key: idempotencyKey,
    entities: [{ entity_type: "issue", entity_id: issueEntityId, ...fields }],
  });
}

/**
 * @param {{ baseUrl: string; token: string }} ctx
 * @param {string} entityType
 * @param {string} entityId
 * @param {Record<string, unknown>} fields
 * @param {string} idempotencyKey
 */
export async function patchEntity(ctx, entityType, entityId, fields, idempotencyKey) {
  return storeEntities(ctx.baseUrl, ctx.token, {
    idempotency_key: idempotencyKey,
    entities: [{ entity_type: entityType, entity_id: entityId, ...fields }],
  });
}

/**
 * @param {{ baseUrl: string; token: string }} ctx
 * @param {{
 *   content: string;
 *   turn_key: string;
 *   issue_entity_id?: string;
 * }} msg
 * @param {string} idempotencyKey
 */
export async function postAgentMessage(ctx, msg, idempotencyKey) {
  const res = await storeEntities(ctx.baseUrl, ctx.token, {
    idempotency_key: idempotencyKey,
    entities: [
      {
        entity_type: "conversation_message",
        role: "assistant",
        sender_kind: "agent",
        sender_agent_id: "issue-processing-daemon",
        content: msg.content,
        turn_key: msg.turn_key,
      },
    ],
  });
  const mid = res?.entities?.[0]?.entity_id;
  if (mid && msg.issue_entity_id) {
    await createRelationship(ctx.baseUrl, ctx.token, "REFERS_TO", String(mid), msg.issue_entity_id).catch(
      () => {},
    );
  }
  return res;
}
