import { flattenEntitySnapshot } from "./classifier.mjs";
import { queryEntities } from "./neotoma.mjs";

/**
 * When a `daemon_config` entity exists with `active: false`, stop processing new issues.
 * @param {string} baseUrl
 * @param {string} token
 */
export async function isDaemonProcessingEnabled(baseUrl, token) {
  try {
    const res = await queryEntities(baseUrl, token, {
      entity_type: "daemon_config",
      limit: 5,
      include_snapshots: true,
      sort_by: "last_observation_at",
      sort_order: "desc",
    });
    const rows = res?.entities;
    if (!Array.isArray(rows) || rows.length === 0) return true;
    const snap = flattenEntitySnapshot(rows[0]);
    if (snap.active === false) return false;
    return true;
  } catch {
    return true;
  }
}
