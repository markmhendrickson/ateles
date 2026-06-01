import { processIssueSubstrateEventFull } from "../pipeline.mjs";

/**
 * @param {Record<string, unknown>} ev
 * @param {Record<string, unknown>} ctx
 */
export async function handleIssueSubstrateEvent(ev, ctx) {
  const et = String(ev.entity_type || "");
  const line = `[formica] ${ev.event_type} action=${ev.action} entity_id=${ev.entity_id} type=${et}`;
  console.error(line);

  if (et === "issue") {
    await processIssueSubstrateEventFull(ev, ctx);
    return;
  }
  if (et === "product_feedback") {
    console.error("[formica] product_feedback received — pipeline is issue-only for now");
  }
}
