/**
 * TASK SEARCH — WHAT THE SERVER ACTUALLY DOES
 * ===========================================
 * Searching the task queue has one hard requirement: it must reach the whole
 * backlog. The Tasks page holds 200 rows out of 5,566 open tasks (21,285
 * total), so a box that filtered the loaded page would search 3.6% of the
 * backlog while looking like it searched all of it — and a search that finds
 * nothing reads as "this does not exist", which is worse than no search at
 * all. This is the same defect the facet chips had before #691 ("Pending 105"
 * against a true 4,912), and it must not come back in the search box.
 *
 * So the query runs upstream, via `/api/task-search` -> `/entities/query`
 * with a `search` parameter. Everything below exists because that parameter
 * does not behave the way its name suggests.
 *
 * ─────────────────────────────────────────────────────────────────────────────
 * MEASURED AGAINST THE LIVE INSTANCE, 2026-09-02 (21,285 task entities)
 * ─────────────────────────────────────────────────────────────────────────────
 *
 * 1. IT MATCHES MORE THAN THE TITLE — and that is a feature, not a hazard.
 *    Of the 53 tasks returned for "theodore", 9 matched on `description` with
 *    the token absent from both `title` and `canonical_name`; 3 more matched
 *    on neither, via other snapshot fields (`area`, `notes`, `related_url`,
 *    `project_id`, `source` all carried hits). Treat it as a full-text match
 *    over the snapshot, not a title search.
 *
 * 2. IT IS SUBSTRING, NOT WHOLE-WORD. "eleas" returns the same 334 tasks as
 *    "release"; "releas" likewise. There is no word boundary and no stemming
 *    ("releases" is a different, smaller set of 61).
 *
 * 3. IT IS **AND** ACROSS TOKENS. "release" -> 334, "dashboard" -> 74,
 *    "release dashboard" -> 3. An OR would have returned roughly 405. Multi-word
 *    search narrows, which is what the operator asked for after the sessions
 *    search shipped with OR and he caught it. `matchesAllTokens` re-checks this
 *    client-side so a change upstream shows up as fewer rows, never wrong ones.
 *
 * 4. IT DOES **NOT** FOLD ACCENTS. This contradicts the note in
 *    `searchQuery.ts`, which was written from a plan-type probe; on `task` the
 *    measurement is unambiguous and symmetric:
 *
 *      search "theodore" -> 53 rows: 36 hold both spellings, 17 hold ONLY the
 *                           unaccented one, and 0 hold only "Theodóre".
 *      search "theodóre" -> 48 rows: 36 both, 12 accented-only, 0 plain-only.
 *
 *    Neither set contains the other. The query spelling must match the stored
 *    spelling, so each phrasing silently misses the rows written the other way
 *    — exactly the "theodóre doesn't find theodore" bug the operator hit.
 *
 *    THE CLIENT COMPENSATES, because it can do so without a second query:
 *    `accentVariants` sends BOTH spellings when a term contains foldable
 *    characters or when a plain term has accented forms in the data, and the
 *    union is de-duplicated by entity id. See `searchVariants`. Where a variant
 *    query fails, the result says so rather than quietly returning the half it
 *    got.
 *
 * 5. `search` AND `snapshot_filters` DO NOT COMPOSE — the filter is DROPPED.
 *    This is the finding that shaped the whole feature:
 *
 *      status=pending alone                -> total 3,597
 *      search="release" alone              -> total   334
 *      search="release" + status=pending   -> total   334   (not ~50)
 *
 *    And the rows come back unfiltered: of 40 returned for that last query,
 *    only 9 were `pending` — the rest were `completed`, `open`, `in_progress`,
 *    `requested`. Upstream accepts the filter, ignores it, and answers with
 *    something plausible, the same way it does for `sort_by` (see
 *    `/api/search` in the proxy).
 *
 *    CONSEQUENCE: "undispatched, high priority, matching X" CANNOT be asked as
 *    one upstream query. Sending it would print a count belonging to a strictly
 *    wider question than the one on screen. So search fetches by text alone and
 *    the page's own filters are applied HERE, over the returned set, with both
 *    figures shown: how many the backlog holds for the text, and how many of
 *    those the filters kept. `SearchResult.matched` vs `.total` is that pair,
 *    and the UI is required to render both.
 *
 * 6. THE TOTAL DOES NOT SATURATE. Unlike a filtered total, which clamps at
 *    `FILTERED_TOTAL_CEILING` (10,000) and is a lower bound, a search-only
 *    total is a real aggregate: "a" reports 21,369, well above the ceiling.
 *    A search total may therefore be printed as a figure — but only while the
 *    request carries no `snapshot_filters`, which is why this module never
 *    sends any and why `searchSaturated` still guards the case.
 *
 * 7. A TERM EQUAL TO THE ENTITY TYPE NAME RETURNS ZERO. Reproducible across
 *    four types: search "task" on type `task` -> 0, while `plan` -> 112 and
 *    `issue` -> 435 for the same term; "plan" on `plan` -> 0; "issue" on
 *    `issue` -> 0; "project" on `project` -> 0. It is not a stopword —
 *    "subtask" (10), "tasked" (2) and "multitask" (2) all match on type `task`.
 *    Searching the task queue for the word "task" therefore returns an empty
 *    result that means nothing about the data. `typeNameCollision` detects it
 *    so the UI can say so instead of rendering a truthful-looking zero.
 */
import { FILTERED_TOTAL_CEILING, type Count, countFrom } from "./taskCount";
import { type Task, type TaskEntity, parseTask } from "./tasks";

/** The entity type this page searches. Also the term that triggers finding 7. */
export const SEARCHED_TYPE = "task";

/**
 * THE NON-TERMINAL STATUS VOCABULARY — what "open work" means to a query.
 *
 * Lives here, in a module with no DOM and no imports beyond `taskCount`, so
 * that the dev server and the browser share ONE definition — the same
 * arrangement `FILTERED_TOTAL_CEILING` already uses. It used to be private to
 * `neotomaProxy.ts`, which was fine while only the server needed it; search
 * applies the open-status rule CLIENT-side (upstream drops filters sent
 * alongside `search` — finding 5), so a second copy in the browser would be a
 * copy that drifts, and a status missing from one side would silently hide
 * open work on that side only.
 *
 * Expressed as a POSITIVE list because it has to be: `snapshot_filters` accepts
 * only `eq | in | gt | lt | gte | lte | contains | contains_word`, and BOTH
 * `ne` and `nin` are rejected 400. "Everything except done" is therefore not
 * expressible upstream, and the open set must be enumerated instead.
 *
 * The list is the union of the dispatcher's state machine
 * (`lib/daemon_runtime/task_lifecycle.py`) and the legacy spellings production
 * actually holds, because `status` is typed as a bare string with no enum and
 * both vocabularies are live. Measured 2026-08-31, one count per value:
 * pending 3547, open 1088, in_progress 394, todo 252, awaiting_input 90,
 * blocked 74, executing 3, active 1, awaiting_release_confirmation 1,
 * awaiting_approval 1, routed 0 — summing to 5451, which is EXACTLY the total
 * the combined `in` query reports. That agreement is the check that this list
 * is complete: a missing spelling would show up as an unexplained shortfall
 * against the 20,989-task denominator.
 *
 * A value here that no task carries costs nothing. A value MISSING from here
 * silently hides open work, so err toward including a spelling.
 */
export const OPEN_TASK_STATUSES = [
  "pending",
  "open",
  "todo",
  "in_progress",
  "active",
  "routed",
  "executing",
  "blocked",
  "awaiting_input",
  "awaiting_approval",
  "awaiting_release_confirmation",
] as const;

/**
 * Shortest query sent upstream.
 *
 * A one-character search is a substring match against 21,285 entities: it
 * returns thousands of rows, costs a slow query, and tells the operator
 * nothing. Two is the floor at which the result is worth the wait.
 */
export const MIN_QUERY_LENGTH = 2;

/** NFD-normalize and strip diacritics: "Theodóre" -> "theodore". */
export function fold(value: string): string {
  return value
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .toLowerCase();
}

/** True when folding changes the string — i.e. it carries accents. */
export function hasAccents(value: string): boolean {
  return fold(value) !== value.toLowerCase();
}

/**
 * Does this row contain EVERY token, in any field?
 *
 * AND across tokens, OR across fields. Applied over the rows a query actually
 * returned, so it costs nothing, and compared on FOLDED text so the guard does
 * not itself reintroduce the accent problem it exists beside.
 */
export function matchesAllTokens(haystack: string, text: string): boolean {
  const tokens = text.split(/\s+/).filter(Boolean).map(fold);
  if (!tokens.length) return true;
  const hay = fold(haystack);
  return tokens.every((t) => hay.includes(t));
}

/**
 * Is this query the one upstream answers with a meaningless zero (finding 7)?
 *
 * Only an EXACT match on the type name collides — "subtask" and "tasked" both
 * return rows — so the test is equality against the folded, trimmed query, not
 * a substring test that would suppress legitimate searches.
 */
export function typeNameCollision(query: string): boolean {
  return fold(query.trim()) === SEARCHED_TYPE;
}

/**
 * The spellings to ask upstream for, given one operator query.
 *
 * Upstream does not fold accents (finding 4), so a single query silently
 * misses every row written the other way. Where a term has an accented and an
 * unaccented form, both are asked and the answers are unioned by entity id.
 *
 * `known` supplies accented spellings observed in the data for an unaccented
 * query — the direction that cannot be derived from the query text alone.
 * "theodore" cannot be turned into "theodóre" by any rule, so the variant has
 * to come from somewhere; without a match here the plain query is sent alone
 * and the UI discloses that accented spellings may be missed.
 *
 * Always returns the operator's own query FIRST, so the primary result set is
 * the one they asked for and variants only ever add.
 */
export function searchVariants(query: string, known: Record<string, string[]> = {}): string[] {
  const raw = query.trim();
  if (!raw) return [];

  const out = [raw];
  const push = (v: string) => {
    if (v && !out.some((existing) => existing === v)) out.push(v);
  };

  // Accented query -> also ask for the folded spelling.
  if (hasAccents(raw)) push(fold(raw));

  // Unaccented query -> ask for any accented spellings the data is known to
  // hold. Matched on the folded key so "Theodore" and "theodore" both hit.
  for (const variant of known[fold(raw)] ?? []) push(variant);

  return out;
}

/**
 * Accented spellings known to exist in the task backlog for a folded term.
 *
 * Deliberately tiny and explicit rather than derived. There is no upstream
 * index of "what accented forms exist", and guessing them (by decorating every
 * vowel) would multiply every search into a dozen slow queries to chase rows
 * that mostly do not exist. These are the ones measured in production; the UI
 * tells the operator when a query has no known variant, so the list being
 * incomplete is disclosed rather than hidden.
 */
export const KNOWN_ACCENT_VARIANTS: Record<string, string[]> = {
  theodore: ["theodóre"],
};

/** One task row, as the search result renders it. Mirrors `Task` loosely. */
export interface SearchRow {
  id: string;
  title: string;
  status: string;
  priority: string | null;
  assignedTo: string | null;
  updatedAt: Date | null;
  /** True when no agent owns this task — the "undispatched" filter's subject. */
  undispatched: boolean;
  /** True when this row is an open question, not a unit of work. */
  question: boolean;
  /** True when this row came from an Asana import rather than the swarm. */
  imported: boolean;
  /** Everything searchable about the row, folded — the client-side AND guard. */
  haystack: string;
}

/**
 * WHAT A SEARCH CAME BACK WITH.
 *
 * `total` and `matched` are separate because they answer different questions
 * and finding 5 makes them genuinely different numbers:
 *
 *   total   — how many tasks in the WHOLE backlog match the text. A real
 *             aggregate (finding 6), so it is a figure, not a bound.
 *   matched — how many of the rows we fetched survived the page's own filters,
 *             applied client-side because upstream drops them.
 *
 * Showing only `matched` would understate the backlog; showing only `total`
 * would label a filtered list with an unfiltered count. The UI shows both.
 */
export interface SearchResult {
  rows: SearchRow[];
  /** Backlog-wide count for the text, however well it is known. */
  total: Count;
  /** Rows fetched before the client-side filters ran. */
  fetched: number;
  /** Rows left after them — `rows.length`, named for the disclosure text. */
  matched: number;
  /** True when the fetch hit its row cap, so `rows` is a partial sample. */
  capped: boolean;
  /** Spellings actually queried upstream (finding 4). */
  variants: string[];
  /** Variants that failed or timed out — their rows are missing from `rows`. */
  failedVariants: string[];
  /** Set when the query is the type-name collision of finding 7. */
  typeNameCollision: boolean;
  readAt: Date;
}

/**
 * Is a search total a lower bound rather than a count?
 *
 * A search-only total does not saturate (finding 6), so this is false in the
 * normal case. It exists because that property holds only while the request
 * sends no `snapshot_filters`: if a future change adds one, the total silently
 * becomes a clamp, and this keeps the display honest without the caller having
 * to remember why.
 */
export function searchSaturated(total: number | null | undefined, sentFilters: boolean): boolean {
  return sentFilters && total === FILTERED_TOTAL_CEILING;
}

/**
 * Merge per-variant row sets, keeping each entity once.
 *
 * First occurrence wins, and variants are ordered with the operator's own
 * spelling first, so a row found by their query keeps that provenance.
 */
export function unionRows(sets: SearchRow[][]): SearchRow[] {
  const seen = new Set<string>();
  const out: SearchRow[] = [];
  for (const set of sets) {
    for (const row of set) {
      if (seen.has(row.id)) continue;
      seen.add(row.id);
      out.push(row);
    }
  }
  return out;
}

/**
 * Combine per-variant totals into one backlog-wide count.
 *
 * Variant result sets OVERLAP (36 of the "theodore"/"theodóre" rows are in
 * both), so the totals cannot be summed — that would double-count. The honest
 * combined figure is a LOWER BOUND: at least as many as the largest single
 * variant matched. With one variant it is exactly that variant's count and
 * stays `exact`; with several it becomes `atLeast`, which `countText` renders
 * with its `≥`.
 *
 * Any unmeasured variant makes the whole thing unmeasured rather than a
 * confident undercount, matching `foldFacets`.
 */
export function combineTotals(totals: Count[]): Count {
  if (!totals.length) return { kind: "unmeasured" };
  if (totals.some((t) => t.kind === "unmeasured")) return { kind: "unmeasured" };

  const values = totals.map((t) => (t as { value: number }).value);
  const max = Math.max(...values);
  const bounded = totals.length > 1 || totals.some((t) => t.kind === "atLeast");
  return bounded ? { kind: "atLeast", value: max } : { kind: "exact", value: max };
}

/**
 * The client-side filter predicates, applied because upstream drops them.
 *
 * Mirrors the controls the Tasks page already exposes so that search COMPOSES
 * with them rather than replacing them — "undispatched, high priority,
 * matching X" is the query worth asking, and it is assembled here.
 *
 * `openStatuses` is passed in rather than imported so the client cannot drift
 * from the proxy's `OPEN_TASK_STATUSES`, which is the definition of "open".
 */
export interface SearchFilters {
  /** "open" | "any" | an exact status string. */
  status: string;
  assignedTo: string;
  priority: string;
  /** Untouched for at least this many days; "" for no staleness filter. */
  staleDays: string;
  /** Bucket/undispatched chip, or "all". */
  chip: string;
  /** Hide Asana import residue. Never the default — see `IMPORT_SOURCE`. */
  hideImported: boolean;
}

export function applyFilters(
  rows: SearchRow[],
  filters: SearchFilters,
  openStatuses: readonly string[],
  now: Date = new Date(),
): SearchRow[] {
  const open = new Set(openStatuses);
  const staleDays = Number(filters.staleDays);
  const staleCutoff =
    filters.staleDays && Number.isFinite(staleDays) && staleDays > 0
      ? new Date(now.getTime() - staleDays * 86_400_000)
      : null;

  return rows.filter((row) => {
    if (filters.status === "open") {
      if (!open.has(row.status)) return false;
    } else if (filters.status !== "any" && row.status !== filters.status) {
      return false;
    }
    if (filters.assignedTo && row.assignedTo !== filters.assignedTo) return false;
    if (filters.priority && (row.priority ?? "") !== filters.priority) return false;
    if (staleCutoff && !(row.updatedAt && row.updatedAt < staleCutoff)) return false;
    if (filters.chip === "undispatched" && !row.undispatched) return false;
    if (filters.hideImported && row.imported) return false;
    return true;
  });
}

/* ────────────────────────────────────────────────────────────────────────────
 * FETCHING
 * ──────────────────────────────────────────────────────────────────────────── */

/**
 * IMPORT RESIDUE — visible by default, never hidden silently.
 *
 * A third of the backlog is an Asana import: 1,897 tasks carry
 * `import_source_file`, most also an `asana_source_gid`, and only 760 of 5,566
 * open tasks were touched in the last month. They dominate search results —
 * for the query "review" (1,310 hits) 194 of the first 200 rows returned were
 * imported, 192 of them from `asana_api_direct`.
 *
 * That makes suppressing them tempting and makes doing it silently wrong. A
 * hidden default would be another partial view presented as complete, which is
 * the exact failure this page keeps fixing. So residue is SHOWN by default,
 * counted out loud, and the operator gets a toggle whose effect is stated in
 * rows. `hideImported` starts false everywhere.
 */
export function isImported(snapshot: Record<string, unknown>): boolean {
  return Boolean(snapshot.import_source_file) || Boolean(snapshot.asana_source_gid);
}

/** Unwrap the doubly-nested snapshot shape `/entities/query` returns. */
function snapshotOf(row: TaskEntity): Record<string, unknown> {
  const snap = (row.snapshot ?? {}) as Record<string, unknown>;
  return ((snap.snapshot as Record<string, unknown>) ?? snap) as Record<string, unknown>;
}

/**
 * Build the folded text the client-side AND guard matches against.
 *
 * Upstream matches fields beyond the title (finding 1), so the guard has to
 * look at the same breadth or it would drop legitimate description-only hits —
 * 9 of 53 rows in the "theodore" measurement were exactly that. Serializing
 * the whole snapshot is the cheap way to be at least as broad as the server.
 */
function haystackOf(row: TaskEntity, task: Task): string {
  return `${task.title} ${row.canonical_name ?? ""} ${JSON.stringify(snapshotOf(row))}`;
}

/** Turn one upstream row into the shape the results table renders. */
export function toSearchRow(row: TaskEntity): SearchRow {
  const task = parseTask(row);
  return {
    id: task.id,
    title: task.title,
    status: task.status,
    priority: task.priority,
    assignedTo: task.assignedTo,
    updatedAt: task.updatedAt,
    undispatched: task.undispatched,
    question: task.question,
    imported: isImported(snapshotOf(row)),
    haystack: haystackOf(row, task),
  };
}

/** What one variant's query came back with, before the union. */
interface VariantResult {
  variant: string;
  rows: SearchRow[];
  total: Count;
  fetched: number;
  ok: boolean;
}

/** Rows one upstream request asks for. Filtering happens over this page. */
export const SEARCH_LIMIT = 200;

async function fetchVariant(
  variant: string,
  signal: AbortSignal,
): Promise<VariantResult> {
  const params = new URLSearchParams({ q: variant, limit: String(SEARCH_LIMIT) });
  try {
    const res = await fetch(`/api/task-search?${params}`, { signal });
    const body = await res.json();
    if (!res.ok || body.error) {
      // A failed variant is NOT an empty one. It contributes no rows and makes
      // the combined total unmeasured rather than a confident undercount.
      return { variant, rows: [], total: { kind: "unmeasured" }, fetched: 0, ok: false };
    }
    const entities = (body.entities ?? []) as TaskEntity[];
    return {
      variant,
      rows: entities.map(toSearchRow),
      total: countFrom(body),
      fetched: entities.length,
      ok: true,
    };
  } catch (err) {
    if ((err as Error).name === "AbortError") throw err;
    return { variant, rows: [], total: { kind: "unmeasured" }, fetched: 0, ok: false };
  }
}

/**
 * Run one operator query against the whole backlog and assemble the result.
 *
 * Variants are fetched IN PARALLEL — there are at most two, and they are
 * independent — then unioned by entity id, because their result sets overlap
 * heavily (36 of the theodore/theodóre rows are in both).
 *
 * The client-side AND guard runs over the union. The server already ANDs
 * (finding 3); this leg means that if upstream ever switches to OR, the
 * operator sees FEWER rows rather than wrong ones. That regression has already
 * happened once on the sessions search and was caught by the operator, not by
 * the code.
 *
 * Filters are applied by the CALLER, not here, so the count of what the text
 * matched and the count of what survived the filters stay separable.
 */
export async function runSearch(
  query: string,
  signal: AbortSignal,
  known: Record<string, string[]> = KNOWN_ACCENT_VARIANTS,
): Promise<SearchResult> {
  const variants = searchVariants(query, known);
  const results = await Promise.all(variants.map((v) => fetchVariant(v, signal)));

  const union = unionRows(results.map((r) => r.rows));
  const rows = union.filter((row) => matchesAllTokens(row.haystack, query));
  const fetched = results.reduce((n, r) => n + r.fetched, 0);

  return {
    rows,
    total: combineTotals(results.map((r) => r.total)),
    fetched,
    matched: rows.length,
    // Any variant that filled its page means the backlog holds rows this
    // result does not, so the filtered view below is a sample, not a census.
    capped: results.some((r) => r.fetched >= SEARCH_LIMIT),
    variants,
    failedVariants: results.filter((r) => !r.ok).map((r) => r.variant),
    typeNameCollision: typeNameCollision(query),
    readAt: new Date(),
  };
}
