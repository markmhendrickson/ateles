/**
 * THE CONSOLE'S SPINE — mission, strategies, charter, projects.
 *
 * Design: console_design.md, section 2.1 (slice 1). Every section here is a
 * read of records that already exist; nothing is written and no new record
 * type is needed.
 *
 * WHY SECTIONS, NOT ONE AGGREGATE. Hosted Neotoma reads run from 5 to 80
 * seconds under load (ateles#576). One aggregate response would be as slow as
 * its slowest type and would blank the whole page when any one read failed. So
 * each section is its own request (`/api/console?section=<name>`), the client
 * renders each as it lands, and a failed or slow section says so on its own
 * while the others stand.
 *
 * TWO GUARDS AGAINST LOADING THE HOSTED INSTANCE:
 *
 *   - a short TTL cache of SUCCESSFUL sections. It is deliberately NOT the
 *     proxy's `entityCache`, which never expires by age: planning records are
 *     edited by the operator, and a stale draft would read as current.
 *   - at most `MAX_UPSTREAM_READS` (2) upstream reads in flight at once,
 *     across all sections and all viewers. Bulk reads above that have crashed
 *     the hosted instance.
 *
 * A failure is never cached and never becomes an empty result: the section
 * answers `{ ok: false, error }` and the page prints that, not "none".
 *
 * Dependencies are injected (`query`, `now`) so the contract is testable
 * without a network.
 */

export const SPINE_SECTIONS = [
  "charter",
  "missions",
  "strategies",
  "retired",
  "allstrategies",
  "projects",
] as const;
export type SpineSection = (typeof SPINE_SECTIONS)[number];

export function isSpineSection(value: string): value is SpineSection {
  return (SPINE_SECTIONS as readonly string[]).includes(value);
}

/** How long a successful section is served from memory. */
export const SPINE_TTL_MS = 60_000;
/** Upstream reads in flight at once, across every section and viewer. */
export const MAX_UPSTREAM_READS = 2;
/** Upstream timeout for a console read; reads of 5 to 80s are normal under load. */
export const SPINE_READ_TIMEOUT_MS = 90_000;

/** The statuses that mean "no longer an active strategy". Counted, not fetched. */
export const RETIRED_STRATEGY_STATUSES = ["demoted_to_plan", "superseded", "reference_artifact"];

/** Cap on prose carried to the browser, so one runaway record cannot bloat a response. */
export const MAX_CONTENT_CHARS = 60_000;
/** Cap on the short text fields (title, name, description) of one record. */
export const MAX_FIELD_CHARS = 2_000;

/** What the client is told when a read fails. The real error stays in the server log. */
export const READ_FAILED_MESSAGE = "Read failed.";

/** The `/entities/query` response, as far as the spine reads it. */
export interface QueryResponse {
  entities?: {
    entity_id?: string;
    canonical_name?: string | null;
    snapshot?: Record<string, unknown> | null;
  }[];
  total?: unknown;
}

export interface SpineDeps {
  query(body: Record<string, unknown>): Promise<unknown>;
  now?(): number;
  ttlMs?: number;
  /** Where the real error of a failed read goes. Defaults to `console.error`. */
  log?(...args: unknown[]): void;
}

export type SectionResult<T> = ({ ok: true; fetchedAt: string } & T) | { ok: false; error: string };

export interface SpineRecord {
  id: string;
  name: string | null;
  title: string | null;
  status: string | null;
  altitude: string | null;
  domain: string | null;
  role: string | null;
  description: string | null;
  /** Only the charter and missions carry prose. */
  content: string | null;
}

export interface RecordsSection {
  records: SpineRecord[];
  /** What upstream reported as the total for the filter, or null if it did not say. */
  total: number | null;
}

export interface CountSection {
  total: number;
}

/** Read a snapshot field as a trimmed string, or null when absent or not text. */
function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function shape(entity: NonNullable<QueryResponse["entities"]>[number], withContent: boolean): SpineRecord | null {
  if (typeof entity.entity_id !== "string") return null;
  const s = entity.snapshot ?? {};
  const content = withContent ? text(s.content) : null;
  const short = (value: unknown) => text(value)?.slice(0, MAX_FIELD_CHARS) ?? null;
  return {
    id: entity.entity_id,
    name: short(entity.canonical_name),
    title: short(s.title),
    status: text(s.status),
    altitude: text(s.altitude),
    domain: text(s.domain),
    role: text(s.role),
    description: short(s.description),
    content: content ? content.slice(0, MAX_CONTENT_CHARS) : null,
  };
}

function totalOf(body: QueryResponse): number | null {
  return typeof body.total === "number" && Number.isFinite(body.total) ? body.total : null;
}

/** The query for each section. Fixed here: the route cannot be pointed at a filter. */
export function queryFor(section: SpineSection): Record<string, unknown> {
  switch (section) {
    case "charter":
      // Resolved by what it IS, never by a hardcoded id (fork test).
      return {
        entity_type: "strategy",
        snapshot_filters: {
          role: { op: "eq", value: "responsibility_charter" },
          status: { op: "eq", value: "active" },
        },
        include_snapshots: true,
        sort_by: "last_observation_at",
        sort_order: "desc",
        limit: 5,
      };
    case "missions":
      return {
        entity_type: "mission",
        snapshot_filters: { status: { op: "eq", value: "active" } },
        include_snapshots: true,
        limit: 10,
      };
    case "strategies":
      return {
        entity_type: "strategy",
        snapshot_filters: { status: { op: "eq", value: "active" } },
        include_snapshots: true,
        limit: 100,
      };
    case "retired":
      // A count, asked as a count: no rows are fetched.
      return {
        entity_type: "strategy",
        snapshot_filters: { status: { op: "in", value: RETIRED_STRATEGY_STATUSES } },
        include_snapshots: false,
        limit: 1,
      };
    case "allstrategies":
      // Every strategy whatever its status, counted: with the active and retired
      // counts it yields the strategies in any other status. No filter, so
      // upstream runs a real aggregate rather than a clamped one.
      return { entity_type: "strategy", include_snapshots: false, limit: 1 };
    case "projects":
      return { entity_type: "project", include_snapshots: true, limit: 200 };
  }
}

/** Turn an upstream response into the section's payload. Pure, for testing. */
export function shapeSection(
  section: SpineSection,
  raw: unknown,
): RecordsSection | CountSection {
  const body = (raw ?? {}) as QueryResponse;
  if (section === "retired" || section === "allstrategies") {
    const total = totalOf(body);
    // No total means we do not know. That is a failure of the read, not zero.
    if (total === null) throw new Error(`Neotoma did not report a total for ${section}.`);
    return { total };
  }
  if (!Array.isArray(body.entities)) {
    throw new Error("Neotoma returned no entity list.");
  }
  const withContent = section === "charter" || section === "missions";
  const records = body.entities
    .map((e) => shape(e, withContent))
    .filter((r): r is SpineRecord => r !== null);
  return { records, total: totalOf(body) };
}

/** A counting gate: at most `max` bodies running, the rest wait their turn. */
export function createLimiter(max: number) {
  let running = 0;
  const waiting: (() => void)[] = [];
  return async function limit<T>(fn: () => Promise<T>): Promise<T> {
    if (running >= max) await new Promise<void>((resolve) => waiting.push(resolve));
    running++;
    try {
      return await fn();
    } finally {
      running--;
      waiting.shift()?.();
    }
  };
}

/**
 * Build the section reader. One instance per process: the cache and the read
 * limiter are shared by every viewer, which is the point of both.
 */
export function createSpine(deps: SpineDeps) {
  const now = deps.now ?? Date.now;
  const ttl = deps.ttlMs ?? SPINE_TTL_MS;
  const limit = createLimiter(MAX_UPSTREAM_READS);
  const cache = new Map<SpineSection, { at: number; value: SectionResult<object> }>();
  const inFlight = new Map<SpineSection, Promise<SectionResult<object>>>();

  async function fetchSection(section: SpineSection): Promise<SectionResult<object>> {
    try {
      const raw = await limit(() => deps.query(queryFor(section)));
      const value: SectionResult<object> = {
        ok: true,
        fetchedAt: new Date(now()).toISOString(),
        ...shapeSection(section, raw),
      };
      cache.set(section, { at: now(), value });
      return value;
    } catch (err) {
      // Never cached: the next request retries. The client gets a fixed short
      // message; the real error (which can name hosts or paths) stays in the log.
      (deps.log ?? console.error)(`console section "${section}" read failed:`, err);
      return { ok: false, error: READ_FAILED_MESSAGE };
    }
  }

  return function readSection(section: SpineSection): Promise<SectionResult<object>> {
    const hit = cache.get(section);
    if (hit && now() - hit.at < ttl) return Promise.resolve(hit.value);
    // Concurrent viewers share one upstream read rather than queueing duplicates.
    const pending = inFlight.get(section);
    if (pending) return pending;
    const started = fetchSection(section).finally(() => inFlight.delete(section));
    inFlight.set(section, started);
    return started;
  };
}
