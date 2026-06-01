/**
 * @param {string} baseUrl
 * @param {string} bearerToken
 * @param {string} path
 * @param {unknown} [body]
 */
async function api(baseUrl, bearerToken, path, body = undefined) {
  const url = `${baseUrl.replace(/\/$/, "")}${path.startsWith("/") ? path : `/${path}`}`;
  const init = {
    method: body === undefined ? "GET" : "POST",
    headers: {
      Authorization: `Bearer ${bearerToken}`,
      Accept: "application/json",
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  };
  const res = await fetch(url, init);
  const text = await res.text();
  let json;
  try {
    json = text ? JSON.parse(text) : {};
  } catch {
    json = { _raw: text };
  }
  if (!res.ok) {
    const err = new Error(`Neotoma API ${res.status}: ${text.slice(0, 500)}`);
    err.status = res.status;
    err.body = json;
    throw err;
  }
  return json;
}

/**
 * @param {string} baseUrl
 * @param {string} bearerToken
 */
export async function listSubscriptions(baseUrl, bearerToken) {
  return api(baseUrl, bearerToken, "/list_subscriptions", {});
}

/**
 * @param {string} baseUrl
 * @param {string} bearerToken
 * @param {Record<string, unknown>} input
 */
export async function subscribe(baseUrl, bearerToken, input) {
  return api(baseUrl, bearerToken, "/subscribe", input);
}

/**
 * Find an active SSE subscription matching entity_types / event_types from config.
 * @param {unknown[]} subscriptions
 * @param {string[]} entityTypes
 * @param {string[]} eventTypes
 */
export function findReusableSseSubscription(subscriptions, entityTypes, eventTypes) {
  if (!Array.isArray(subscriptions)) return null;
  const wetNeed = entityTypes.map(String);
  const evNeed = eventTypes.map(String);
  for (const s of subscriptions) {
    if (!s || typeof s !== "object") continue;
    if (s.active === false) continue;
    if (s.delivery_method !== "sse") continue;
    const wet = Array.isArray(s.watch_entity_types) ? s.watch_entity_types.map(String) : [];
    const wty = Array.isArray(s.watch_event_types) ? s.watch_event_types.map(String) : [];
    const wetOk = wetNeed.every((t) => wet.includes(t));
    const evOk = evNeed.every((t) => wty.includes(t));
    if (wetOk && evOk && s.subscription_id) {
      return String(s.subscription_id);
    }
  }
  return null;
}

/**
 * @param {string} baseUrl
 * @param {string} bearerToken
 * @param {string} entityId
 */
export async function getEntitySnapshotJson(baseUrl, bearerToken, entityId) {
  return api(baseUrl, bearerToken, "/get_entity_snapshot", {
    entity_id: entityId,
    format: "json",
  });
}

/**
 * @param {string} baseUrl
 * @param {string} bearerToken
 * @param {Record<string, unknown>} body
 */
export async function storeEntities(baseUrl, bearerToken, body) {
  return api(baseUrl, bearerToken, "/store", body);
}

/**
 * @param {string} baseUrl
 * @param {string} bearerToken
 * @param {Record<string, unknown>} queryBody
 */
export async function queryEntities(baseUrl, bearerToken, queryBody) {
  return api(baseUrl, bearerToken, "/entities/query", queryBody);
}

/**
 * @param {string} baseUrl
 * @param {string} bearerToken
 * @param {string} sourceId
 */
/**
 * @param {string} baseUrl
 * @param {string} bearerToken
 * @param {string} relationshipType
 * @param {string} sourceEntityId
 * @param {string} targetEntityId
 */
export async function createRelationship(
  baseUrl,
  bearerToken,
  relationshipType,
  sourceEntityId,
  targetEntityId,
) {
  return api(baseUrl, bearerToken, "/create_relationship", {
    relationship_type: relationshipType,
    source_entity_id: sourceEntityId,
    target_entity_id: targetEntityId,
  });
}

export async function getSourceText(baseUrl, bearerToken, sourceId) {
  const url = `${baseUrl.replace(/\/$/, "")}/sources/${encodeURIComponent(sourceId)}/content`;
  const res = await fetch(url, {
    headers: { Authorization: `Bearer ${bearerToken}`, Accept: "application/octet-stream,*/*" },
  });
  const text = await res.text();
  if (!res.ok) {
    const err = new Error(`Neotoma sources/${sourceId}/content ${res.status}: ${text.slice(0, 300)}`);
    err.status = res.status;
    throw err;
  }
  return text;
}
