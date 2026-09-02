/**
 * FOLLOW A TASK'S STORED LINK TO A WORKFLOW — and report how far it got.
 *
 * Extracted from the Vite proxy so the contract is unit-testable without a
 * live Neotoma or a standing HTTP server. The proxy wires real `neotomaGet` /
 * `neotomaPost`; tests inject mocks.
 *
 * Option 3 (arch, ateles#695 / ADR ent_951b1dedd6304a41f5a9866e): `/pull/`
 * refs return `unsupported_ref` and never run the issue query. `pull_request`
 * entities have no `gate_status`, so completing a PR→workflow path here would
 * invent another false outcome.
 */

import type { WorkflowLink } from "../src/taskPosition";

/** Injectable Neotoma accessors — production wires the bearer-token helpers. */
export interface NeotomaAccess {
  get: (path: string) => Promise<unknown>;
  post: (path: string, body: unknown) => Promise<unknown>;
}

/** Same regex the `/api/task-workflow` route uses for the `id` query param. */
export const ENTITY_ID_RE = /^ent_[0-9a-f]+$/;

export function isMalformedEntityId(id: string): boolean {
  return !ENTITY_ID_RE.test(id);
}

/** Read one snapshot field as a trimmed string, or null. */
function snapStr(snap: Record<string, unknown>, key: string): string | null {
  const v = snap[key];
  return typeof v === "string" && v.trim() ? v.trim() : null;
}

/** Unwrap the double-nesting some rows arrive with, as the client parsers do. */
function innerSnapshot(entity: Record<string, unknown> | null): Record<string, unknown> {
  const outer = (entity?.snapshot ?? {}) as Record<string, unknown>;
  return ((outer.snapshot as Record<string, unknown> | undefined) ?? outer) ?? {};
}

/**
 * Resolve which workflow (if any) a task relates to, following only stored links.
 *
 * Returns the same `WorkflowLink` discriminated union the client renders.
 */
export async function resolveTaskWorkflow(
  taskId: string,
  access: NeotomaAccess,
): Promise<WorkflowLink> {
  const task = (await access.get(`/entities/${taskId}`)) as Record<string, unknown> | null;
  const snap = innerSnapshot(task);

  // The stored reference. `source_url` is the field issue-derived tasks carry;
  // `permalink_url` is checked too because imported tasks use that name.
  const raw = snapStr(snap, "source_url") ?? snapStr(snap, "permalink_url");
  const m = raw ? /github\.com\/([^/\s]+\/[^/\s]+)\/(issues|pull)\/(\d+)/.exec(raw) : null;
  if (!m) return { kind: "none" };

  const ref = {
    repo: m[1],
    number: Number(m[3]),
    url: raw as string,
    isPullRequest: m[2] === "pull",
  };

  // Option 3: refuse PR refs explicitly. Do not query `issue` for `/pull/` URLs
  // — that path would return `dangling` with UI copy claiming "no PR entity"
  // while never looking up `pull_request` at all. PR entities also lack
  // `gate_status`, so a partial "just change entity_type" path stays wrong.
  if (ref.isPullRequest) {
    return { kind: "unsupported_ref", ref, reason: "pull_request_not_supported" };
  }

  // Find the issue entity by number, then confirm the repo matches. Matching on
  // number alone would cross-link neotoma#410 to ateles#410 — two different
  // pieces of work that happen to share an integer.
  const found = (await access.post("/entities/query", {
    entity_type: "issue",
    limit: 10,
    include_snapshots: true,
    snapshot_filters: { issue_number: { op: "eq", value: ref.number } },
  })) as { entities?: Record<string, unknown>[] };

  const hit = (found.entities ?? []).find((e) => {
    const s = innerSnapshot(e);
    return (snapStr(s, "repo") ?? snapStr(s, "repository")) === ref.repo;
  });

  // The common case as measured: the task names an issue that was never stored
  // as an entity. Reported as `dangling` rather than as "no workflow", because
  // the link EXISTS and it is the target that is missing — a backfill fixes
  // this, whereas `kind: "none"` needs the link written in the first place.
  if (!hit) return { kind: "dangling", ref };

  const issueId = String(hit.entity_id ?? "");
  const isnap = innerSnapshot(hit);
  const gateMap = isnap.gate_status;

  // SOURCE ONE: the issue's own gate_status map.
  const gates =
    gateMap && typeof gateMap === "object" && !Array.isArray(gateMap)
      ? Object.entries(gateMap as Record<string, unknown>).map(([gateName, status]) => ({
          gateName,
          status: typeof status === "string" ? status : String(status),
        }))
      : [];

  // SOURCE TWO: participation_record rows for this work entity. Fetched
  // separately and NEVER merged into `gates` — the two writers disagree
  // systematically, and the client renders the disagreement.
  let participation: { gateName: string; status: string }[] = [];
  try {
    const prs = (await access.post("/entities/query", {
      entity_type: "participation_record",
      limit: 50,
      include_snapshots: true,
      snapshot_filters: { work_entity_id: { op: "eq", value: issueId } },
    })) as { entities?: Record<string, unknown>[] };
    participation = (prs.entities ?? []).flatMap((e) => {
      const s = innerSnapshot(e);
      const gateName = snapStr(s, "gate_name");
      const status = snapStr(s, "status");
      return gateName && status ? [{ gateName, status }] : [];
    });
  } catch {
    // A participation lookup that fails must not blank the gate_status we
    // already read. Left empty; the client shows what it has.
    participation = [];
  }

  if (!gates.length && !participation.length) {
    return { kind: "issue_without_gates", ref, issueId };
  }

  return {
    kind: "resolved",
    ref,
    issueId,
    workflowType: snapStr(isnap, "workflow_type"),
    currentOwner: snapStr(isnap, "current_owner"),
    gates,
    participation,
  };
}
