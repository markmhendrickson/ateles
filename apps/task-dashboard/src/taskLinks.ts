/**
 * A TASK'S ISSUES AND PULL REQUESTS — from stored edges, never inferred.
 * ---------------------------------------------------------------------
 * The operator's ask: "All tasks in Neotoma should be related to their issues
 * and PRs, and I want to see links to those issues and PRs in the actual UI."
 *
 * The data half landed first (2026-09-02): 209 tasks whose `source_url` names a
 * GitHub issue or PR now carry a real `REFERS_TO` edge to that entity. This
 * module is the read side — it pulls those edges out of the generic relationship
 * list so the task page can give them their own section, with the title and a
 * link out to GitHub, instead of leaving them as one more row in a mixed list.
 *
 * WHY `REFERS_TO`, AND WHY OUT OF THE TASK
 * ----------------------------------------
 * Chosen from what the graph already used, not minted: of the task→issue edges
 * that existed before the backfill, every one was `REFERS_TO`. `PART_OF` was
 * unavailable on its merits — it already means task→plan here, and an issue is
 * not a container the task is a component of. Direction follows the assertion:
 * the task's own `source_url` is what names the issue, so the task points out.
 *
 * BOTH DIRECTIONS ARE READ ANYWAY. The pre-existing edges ran both ways
 * (whichever agent happened to author one decided its direction), so reading
 * only `outgoing` would hide the older links. Deduped by entity id, because a
 * reciprocal pair would otherwise render the same issue twice.
 *
 * STORED EDGES ONLY — the same boundary `questionLinks.ts` draws. A number
 * scraped from a task's title or description is NOT turned into a link here: it
 * would assert an entity that may not exist. Where the referenced issue was
 * never stored, the honest answer is the gap, and `sourceUrlOnly` below carries
 * it — 7 of the 216 GitHub URLs measured had no entity behind them.
 */
import type { EntityEdge, EntityPayload } from "./entity";

/** The entity types this section is about. */
const WORK_TYPES = new Set(["issue", "pull_request"]);

export interface TaskWorkLink {
  id: string;
  /** `issue` or `pull_request`, as stored. Null while the proxy hydrates it. */
  entityType: string | null;
  /**
   * The issue's own title. Null means NOT HYDRATED YET, which the UI must
   * render as pending rather than falling back to a bare number — see the
   * standing rule in `linkLabel`.
   */
  title: string | null;
  /** `https://github.com/owner/repo/issues/123`, when the edge or entity has one. */
  url: string | null;
  /** `ateles#714`, for the muted qualifier beside the title. Null if unknown. */
  ref: string | null;
  relationship: string;
  direction: "outgoing" | "incoming";
}

/**
 * WHAT THIS SECTION KNOWS, AND WHAT IT ONLY SUSPECTS.
 *
 * The app's `Coverage` contract: an unreadable total is `unknown`, never
 * `complete`. The same rule applies to a task's links. Three states, because
 * collapsing them would let the worst one masquerade as the best:
 *
 *   - `failed`   — relationships could not be read. NOT "no linked PRs".
 *   - `hydrating`— edges are known, at least one target has not loaded yet.
 *   - `read`     — the edge list was read in full.
 *
 * `sourceUrlOnly` is separate from all three: the task names a GitHub URL that
 * no stored entity matches. That is a real finding (the issue was never stored),
 * not an empty list, and it is why "no linked issues" is never printed for a
 * task that plainly references one.
 */
export type LinkCoverage = "failed" | "hydrating" | "read";

export interface TaskLinks {
  links: TaskWorkLink[];
  coverage: LinkCoverage;
  /**
   * The task's stored `source_url` when no edge resolves to it — the dangling
   * case, surfaced rather than swallowed.
   */
  sourceUrlOnly: string | null;
}

/** `.../owner/repo/(issues|pull)/123` -> `repo#123`. Null when it is not one. */
export function refFromUrl(url: string | null | undefined): string | null {
  const m = /github\.com\/[^/\s]+\/([^/\s?#]+)\/(?:issues|pull|pulls)\/(\d+)/i.exec(url ?? "");
  return m ? `${m[1]}#${parseInt(m[2], 10)}` : null;
}

function firstString(o: Record<string, unknown>, keys: string[]): string | null {
  for (const k of keys) {
    const v = o[k];
    if (typeof v === "string" && v.trim()) return v.trim();
    if (typeof v === "number" && Number.isFinite(v)) return String(v);
  }
  return null;
}

/** Neotoma sometimes double-nests a snapshot; tolerate both shapes. */
function snap(e: EntityEdge): Record<string, unknown> {
  const s = e.snapshot;
  if (!s || typeof s !== "object") return {};
  const inner = (s as Record<string, unknown>).snapshot;
  return inner && typeof inner === "object" ? (inner as Record<string, unknown>) : s;
}

/**
 * The GitHub URL for one linked issue or PR.
 *
 * Preferred from the entity's own stored URL. Falling back to repo+number means
 * rebuilding a URL from two fields, and the number lives under FOUR different
 * field names across these entities (`github_number` on 2,707, `issue_number`,
 * `github_issue_number`, `number`) — the fragmentation filed as
 * `ent_b8387e7c5756e9a2f178f088`. All four are read here because reading only
 * the first is exactly the bug that made `source_url` look broken; a resolver
 * that queried `github_number` alone measured 0% where the true figure is ~97%.
 */
function urlOf(e: EntityEdge): string | null {
  const s = snap(e);
  const direct = firstString(s, ["github_url", "html_url", "url"]);
  if (direct && /^https?:\/\//i.test(direct)) return direct;

  const repo = firstString(s, ["repo", "repository", "repository_name"]);
  const num = firstString(s, ["github_number", "issue_number", "github_issue_number", "number"]);
  if (!repo || !num || !/^\d+$/.test(num)) return null;
  // `repo` is usually "owner/repo" but is sometimes a bare name, which cannot
  // be turned into a URL without guessing an owner — so it is not guessed.
  if (!repo.includes("/")) return null;
  const kind = e.entity_type === "pull_request" ? "pull" : "issues";
  return `https://github.com/${repo}/${kind}/${num}`;
}

function titleOf(e: EntityEdge): string | null {
  const s = snap(e);
  const title = firstString(s, ["title", "name"]);
  if (title) return title;
  // `canonical_name` arrives type-prefixed ("issue:347|markmhendrickson/neotoma"),
  // and that tail is a repo path rather than a title — so it is not used as one.
  const cn = e.canonical_name?.trim();
  if (!cn) return null;
  const m = /^(?:issue|pull_request|pr)[: ]#?\d+\|/i.exec(cn);
  if (m) return null;
  const prefix = e.entity_type ? `${e.entity_type}:` : null;
  const stripped = prefix && cn.startsWith(prefix) ? cn.slice(prefix.length).trim() : cn;
  return stripped || null;
}

/**
 * Pull the issue/PR edges out of a task's relationship list.
 *
 * `sourceUrl` is the task's own stored field, passed in so the dangling case can
 * be told apart from the absent one.
 */
export function taskLinks(payload: EntityPayload | null, sourceUrl: string | null): TaskLinks {
  if (!payload || payload.relationshipsFailed) {
    return { links: [], coverage: "failed", sourceUrlOnly: null };
  }

  const edges = [...(payload.outgoing ?? []), ...(payload.incoming ?? [])];
  const seen = new Set<string>();
  const links: TaskWorkLink[] = [];
  // An edge whose target has not hydrated has a null entity_type, so it cannot
  // yet be known to be an issue — which is why hydration is reported rather
  // than a short list being presented as the whole one.
  let pending = false;

  for (const e of edges) {
    if (e.entity_type === null) {
      pending = true;
      continue;
    }
    if (!WORK_TYPES.has(e.entity_type)) continue;
    if (seen.has(e.entity_id)) continue;
    seen.add(e.entity_id);

    const url = urlOf(e);
    links.push({
      id: e.entity_id,
      entityType: e.entity_type,
      title: titleOf(e),
      url,
      ref: refFromUrl(url) ?? refFromUrl(e.canonical_name),
      relationship: e.relationship_type,
      direction: e.direction,
    });
  }

  // Issues before PRs, then by ref so the order is stable across polls rather
  // than following whatever order the two edge lists happened to concatenate in.
  links.sort((a, b) => {
    if (a.entityType !== b.entityType) return a.entityType === "issue" ? -1 : 1;
    return (a.ref ?? a.id).localeCompare(b.ref ?? b.id, undefined, { numeric: true });
  });

  // The task names a GitHub URL, but nothing linked resolves to it: the issue
  // was never stored. Reported, never fabricated into a link.
  const stated = refFromUrl(sourceUrl);
  const covered = stated !== null && links.some((l) => l.ref === stated);
  const sourceUrlOnly = stated && !covered ? (sourceUrl as string) : null;

  return {
    links,
    coverage: pending ? "hydrating" : "read",
    sourceUrlOnly,
  };
}
