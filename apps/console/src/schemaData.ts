/**
 * SCHEMA TAB — TYPES AND SHAPING
 * ------------------------------
 * The wire shape of `/api/schemas`, plus the small amount of derivation the
 * view needs. Everything numeric here comes from Neotoma; the only non-Neotoma
 * input is the static repo scan in `codeUsage.ts`, which is kept in a separate
 * module precisely so its provenance stays obvious at every call site.
 *
 * The honesty rules this module encodes, because they are easy to lose in a
 * component:
 *
 *   - `analyzed: false` is PENDING, never "clean". A type whose analysis has
 *     not hydrated yet must not render as a type without drift.
 *   - Populated-field counts are SAMPLE-derived. `sampled` travels with every
 *     one of them so the view can print the denominator it was measured over.
 *   - Drift is only computed for fields that DECLARE a value set. A field with
 *     a prose description yields no drift finding, which is not the same as a
 *     field that was checked and found clean.
 */

/** One enum-ish field whose live values were compared to its declaration. */
export interface FieldDrift {
  field: string;
  declared: string[];
  observed: { value: string; count: number }[];
  undeclared: { value: string; count: number }[];
}

export interface SchemaAnalysis {
  entityType: string;
  declaredFields: number;
  populatedFields: number;
  deadFields: string[];
  undeclaredFields: string[];
  /** How many entities were actually sampled — the denominator, always shown. */
  sampled: number;
  drift: FieldDrift[];
  lastTouched: string | null;
  valueSummary: { field: string; value: string }[];
  schemaVersion: string | null;
  description: string | null;
  note: string | null;
}

export interface SchemaRow {
  entityType: string;
  count: number;
  /** False = not hydrated yet. NOT the same as "no drift". */
  analyzed: boolean;
  analysis: SchemaAnalysis | null;
}

export interface TailBucket {
  key: string;
  label: string;
  blurb: string;
  types: number;
  entities: number;
  sample: { entityType: string; count: number }[];
}

export interface SchemasPayload {
  sampleSize: number;
  totals: {
    entities: number;
    relationships: number;
    observations: number;
    types: number;
    lastUpdated: string | null;
  };
  canonical: SchemaRow[];
  config: SchemaRow[];
  buckets: TailBucket[];
}

/**
 * Did this row's sample actually come back?
 *
 * THE DISTINCTION THAT MATTERS: `sampled === 0` on a type that HAS entities
 * means the query failed or timed out — every sample-derived figure for that
 * row (populated fields, drift) is unknown and must render as such. A type with
 * `count === 0` is genuinely empty, and zero populated fields is then a real
 * measurement rather than a missing one.
 *
 * This existed as a bug before it existed as a function: a timed-out `task`
 * query cached `populatedFields: 0` and the table rendered "0 of 83" against
 * 21,066 live entities — a confident wrong answer, which is the failure class
 * this whole tab is meant to expose.
 */
export function measuredSample(row: {
  count: number;
  analysis: SchemaAnalysis | null;
}): boolean {
  if (!row.analysis) return false;
  if (row.count === 0) return true;
  return row.analysis.sampled > 0;
}

/** Total undeclared values across a row's drift findings. */
export function driftCount(a: SchemaAnalysis | null): number {
  if (!a) return 0;
  return a.drift.reduce((n, d) => n + d.undeclared.length, 0);
}

/**
 * How many entities carry a value the schema does not declare.
 *
 * This is the number that says how much damage the drift is doing: one stray
 * value on one entity is a typo, 130 of them is a contract nobody is honouring.
 */
export function driftedEntities(a: SchemaAnalysis | null): number {
  if (!a) return 0;
  return a.drift.reduce((n, d) => n + d.undeclared.reduce((m, u) => m + u.count, 0), 0);
}

/** Fraction of declared fields ever seen populated, or null when unmeasurable. */
export function fieldFill(a: SchemaAnalysis | null): number | null {
  if (!a || !a.declaredFields || !a.sampled) return null;
  return a.populatedFields / a.declaredFields;
}

/** `2026-07-07T…` → `2026-07-07`. Dates only; the time is noise at this scale. */
export function day(iso: string | null): string {
  return iso ? iso.slice(0, 10) : "—";
}

/** Whole days since `iso`, or null when there is no timestamp to measure. */
export function ageDays(iso: string | null): number | null {
  if (!iso) return null;
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return null;
  return Math.floor((Date.now() - then) / 86_400_000);
}

/**
 * Staleness bands for configuration.
 *
 * Config is not supposed to change often, so age alone is not a defect — the
 * bands exist to make a value that has gone quietly unmaintained visible, not
 * to imply that recent is correct. Both real incidents behind this tab (a
 * harness-headroom file untouched since 2026-08-01, an Anthus binding still
 * pointing at a pre-migration localhost URL) were months stale.
 */
export function staleness(days: number | null): "fresh" | "aging" | "stale" | "unknown" {
  if (days === null) return "unknown";
  if (days <= 30) return "fresh";
  if (days <= 90) return "aging";
  return "stale";
}
