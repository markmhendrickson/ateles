/**
 * GLOBAL SEARCH — QUERY PARSING AND FETCHING
 * ------------------------------------------
 * Kept separate from the component so the parsing rules are testable by
 * reading them, and so the honesty rules below live next to the code that
 * enforces them rather than inside a render function.
 *
 * THE CONSTRAINT THAT SHAPED ALL OF THIS
 * --------------------------------------
 * A Neotoma entity read currently takes 25-81 SECONDS (ateles#576; root cause
 * neotoma#2217, reader-pool starvation, with zero indexes on
 * `entity_snapshots`). There are ~166,600 entities across ~2,500 types. Three
 * consequences, none of them optional:
 *
 *   1. SEARCH ON SUBMIT, never per keystroke. A query per character would
 *      queue dozens of 30s reads against a starved pool and take the app down
 *      with it.
 *   2. SERVER-SIDE matching only. Fetching a broad page to filter in the
 *      browser does not return at this scale — it is the shape that times out.
 *   3. SCOPE BY TYPE. A type-scoped search measured 5-10s where the same
 *      search across six types in one query took 47.5s.
 *
 * WHAT THE SERVER ALREADY DOES — measured, not assumed
 * ----------------------------------------------------
 * Verified against the live instance on 2026-08-31:
 *
 *   - `search` is AND-ACROSS-TOKENS. "theodore elsa" on `plan` returns 4
 *     entities; "theodore" alone returns 23. All 4 were confirmed to contain
 *     both tokens.
 *   - `search` is ACCENT-FOLDING. "theodore" matches "Theodóre" upstream, with
 *     no client normalization involved.
 *
 * Both are re-checked client-side anyway — see `matchesAllTokens`. That is not
 * redundancy for its own sake: the app shipped a sessions search with
 * OR-across-tokens and the operator caught it, so this leg refuses to be the
 * one that regresses silently if upstream behaviour ever changes.
 */

/** The types the operator actually works with — the default scope. */
export const DEFAULT_TYPES = [
  "task",
  "project",
  "plan",
  "issue",
  "conversation",
  "agent_definition",
] as const;

/** Offered when the operator widens the scope beyond the working set. */
export const EXTRA_TYPES = [
  "contact",
  "session_digest",
  "rendered_page",
  "escalation",
  "daemon_report",
  "workflow_definition",
] as const;

/** An `ent_` id and nothing else — the fast path. */
export const ENTITY_ID = /^ent_[0-9a-f]+$/;

export interface ParsedQuery {
  /** Free-text tokens, AND-ed together. */
  text: string;
  /** `field:value` terms, sent as server-side `snapshot_filters`. */
  filters: { field: string; value: string }[];
  /** Set when the whole input is an entity id — take the direct-fetch path. */
  entityId: string | null;
}

/**
 * Split raw input into an id, `field:value` terms, and free text.
 *
 * `field:value` is supported because the operator asked to search "by
 * field-based values or entity IDs", and because these are the terms that
 * actually discriminate: `status:pending`, `assigned_to:cicada`,
 * `repository_name:ateles`, `priority:high`, `project_id:theodore`. They map to
 * `snapshot_filters` so the narrowing happens upstream.
 *
 * A bare colon inside a word (a URL, a timestamp, `plan:Theodóre`) is NOT a
 * filter — the field name must look like an identifier, or the whole token
 * stays free text. Without that rule, pasting a canonical name like
 * `plan:Theodóre — …` would silently become a filter on a field named "plan"
 * and return nothing.
 */
export function parseQuery(raw: string): ParsedQuery {
  const trimmed = raw.trim();
  if (ENTITY_ID.test(trimmed)) {
    return { text: "", filters: [], entityId: trimmed };
  }

  const filters: { field: string; value: string }[] = [];
  const words: string[] = [];

  for (const token of trimmed.split(/\s+/).filter(Boolean)) {
    const at = token.indexOf(":");
    const field = at > 0 ? token.slice(0, at) : "";
    const value = at > 0 ? token.slice(at + 1) : "";
    // Both halves must be present, and the field must read as a field name.
    if (at > 0 && value && /^[a-z][a-z0-9_]{1,59}$/i.test(field)) {
      filters.push({ field, value });
    } else {
      words.push(token);
    }
  }

  return { text: words.join(" "), filters, entityId: null };
}

/** NFD-normalize and strip diacritics: "Theodóre" -> "theodore". */
export function fold(value: string): string {
  return value
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase();
}

/**
 * Does this row contain EVERY token, in any field?
 *
 * AND across tokens, OR across fields — the rule the operator specified, and
 * the one the sessions search got wrong. Applied over the handful of rows a
 * type actually returned, so it costs nothing; the server has already done the
 * real narrowing.
 */
export function matchesAllTokens(haystack: string, text: string): boolean {
  const tokens = text.split(/\s+/).filter(Boolean).map(fold);
  if (!tokens.length) return true;
  const hay = fold(haystack);
  return tokens.every((t) => hay.includes(t));
}

export interface SearchHit {
  entityId: string;
  entityType: string;
  name: string;
  status: string | null;
  updated: string | null;
}

/**
 * What became of ONE type's query. The three states are distinct on purpose:
 * `ok` with no rows is a real finding, `timeout` is the absence of one, and
 * `failed` is a different absence. Collapsing any two of them produces the
 * confident wrong answer this app has spent the day removing — a timeout
 * rendered as "no results" tells the operator something false about his data.
 */
export type TypeResult =
  | { kind: "ok"; hits: SearchHit[]; total: number | null; saturated: boolean }
  | { kind: "timeout" }
  | { kind: "failed"; message: string };

/** Pull a display name out of whatever shape a snapshot happens to have. */
function nameOf(entity: Record<string, unknown>): string {
  const snapshot = (entity.snapshot ?? {}) as Record<string, unknown>;
  const inner = ((snapshot.snapshot as Record<string, unknown>) ?? snapshot) as Record<
    string,
    unknown
  >;
  for (const key of ["name", "title", "summary", "subject"]) {
    const v = inner[key];
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  const canonical = entity.canonical_name;
  if (typeof canonical === "string" && canonical.trim()) {
    // Canonical names are stored as `type:Name`; the type is already a column.
    const colon = canonical.indexOf(":");
    return colon > 0 ? canonical.slice(colon + 1).trim() : canonical.trim();
  }
  return String(entity.entity_id ?? "");
}

function stringField(entity: Record<string, unknown>, keys: string[]): string | null {
  const snapshot = (entity.snapshot ?? {}) as Record<string, unknown>;
  const inner = ((snapshot.snapshot as Record<string, unknown>) ?? snapshot) as Record<
    string,
    unknown
  >;
  for (const key of keys) {
    const v = inner[key];
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  return null;
}

/**
 * Search ONE entity type.
 *
 * One type per request so the six default types settle independently: the
 * client renders `plan` at ~10s rather than waiting on `task` at ~28s, and a
 * type that times out costs its own group instead of the whole result set.
 */
export async function searchType(
  entityType: string,
  parsed: ParsedQuery,
  signal: AbortSignal,
): Promise<TypeResult> {
  const params = new URLSearchParams({ type: entityType, limit: "12" });
  if (parsed.text) params.set("q", parsed.text);
  for (const f of parsed.filters) params.set(f.field, f.value);

  try {
    const res = await fetch(`/api/search?${params}`, { signal });
    const body = await res.json();
    if (!res.ok || body.error) {
      // The proxy flags upstream timeouts explicitly so this stays a distinct
      // state rather than collapsing into a generic failure.
      if (body.timedOut) return { kind: "timeout" };
      return { kind: "failed", message: body.error ?? `HTTP ${res.status}` };
    }

    const rows = (body.entities ?? []) as Record<string, unknown>[];
    const hits: SearchHit[] = rows
      .map((e) => ({
        entityId: String(e.entity_id ?? ""),
        entityType: String(e.entity_type ?? entityType),
        name: nameOf(e),
        status: stringField(e, ["status", "state"]),
        updated:
          (typeof e.last_observation_at === "string" ? e.last_observation_at : null) ??
          stringField(e, ["updated_at", "last_updated"]),
        raw: JSON.stringify(e),
      }))
      // The client-side AND guard. The server already does this; if it ever
      // stops, the operator sees fewer rows rather than wrong ones.
      .filter((h) => matchesAllTokens(h.raw, parsed.text))
      .map(({ raw: _raw, ...h }) => h);

    return {
      kind: "ok",
      hits,
      total: typeof body.total === "number" ? body.total : null,
      saturated: Boolean(body.total_saturated),
    };
  } catch (err) {
    if ((err as Error).name === "AbortError") throw err;
    // A fetch that never came back is a timeout, not an empty result.
    return { kind: "timeout" };
  }
}
