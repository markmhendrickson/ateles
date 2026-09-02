/**
 * COUNTS THAT CANNOT LIE
 * ----------------------
 * A count shown on the Tasks page is one of three things, and collapsing any
 * two of them produces a confident wrong answer:
 *
 *   - a MEASUREMENT — upstream ran a real aggregate;
 *   - a LOWER BOUND — upstream stopped counting at a ceiling;
 *   - a FAILURE — the query did not answer, so nothing is known.
 *
 * The third is the dangerous one, because the natural JavaScript spelling of
 * "no count" is `0`, and `0` renders as a fact. This module exists so that
 * there is no `number` to reach for without first saying which kind of count it
 * is, and so the sole conversion to display text lives in one function.
 *
 * WHY A SEPARATE MODULE rather than helpers inside `TaskList.tsx`: a mixed
 * component/non-component export invalidates React Fast Refresh for the file
 * (the same reason `showSkeleton` lives in `lib/loading.ts`), and these
 * functions carry the page's honesty contract, so they are worth being able to
 * exercise on their own.
 *
 * PRIOR ART AND PRIOR INCIDENTS. `measuredSample()` in `schemaData.ts` is the
 * same contract on the Schemas tab, written after a timed-out `task` sample
 * cached `populatedFields: 0` and the table printed "0 of 83" against 21,066
 * live entities. This app has now had three separate fixes of that one bug
 * class; the type below is the attempt to make the fourth unrepresentable.
 */

/**
 * THE CEILING ON A FILTERED TOTAL — documented here because the number is not
 * self-explanatory at the call site.
 *
 * Neotoma computes `total` two different ways. With no `snapshot_filters` it
 * calls `countVisibleEntities`, a real aggregate. WITH filters it re-runs the
 * query at `limit: 10000` and reports the array length
 * (`queryEntitiesHandler`, neotoma `src/shared/action_handlers/entity_handlers.ts`).
 *
 * A filtered total is therefore `min(true count, 10000)`, and the value 10000
 * means "at least 10000". Measured against the live instance on 2026-08-31:
 * `status: eq completed` reports exactly 10000, and `status: contains "e"` — a
 * strict superset of it plus `pending` (3,547) and `open` (1,088) — also
 * reports exactly 10000. Two nested sets cannot both hold 10,000 when one
 * exceeds the other by thousands, so the figure is a clamp, not a count. Taken
 * as a count it understates `completed` by roughly 5,000 tasks.
 *
 * The proxy decides saturation (only it knows whether filters were sent) and
 * ships the verdict as `total_saturated`; this module only has to honour it.
 */
export const FILTERED_TOTAL_CEILING = 10_000;

export type Count =
  /** Upstream returned a real aggregate. Safe to print as a figure. */
  | { kind: "exact"; value: number }
  /** The true figure is AT LEAST `value`; upstream stopped counting there. */
  | { kind: "atLeast"; value: number }
  /** The query failed or sent no usable total. Emphatically not zero. */
  | { kind: "unmeasured" };

/**
 * Render a count as text.
 *
 * The only place a `Count` becomes something a person reads, which is what
 * makes "never print a fabricated number" checkable rather than a convention.
 * Neither non-exact case returns a bare numeral: a lower bound always carries
 * its `≥`, and a failure is words.
 */
export function countText(c: Count): string {
  switch (c.kind) {
    case "exact":
      return c.value.toLocaleString();
    case "atLeast":
      return `≥${c.value.toLocaleString()}`;
    case "unmeasured":
      return "not measured";
  }
}

/** True when a count may be used in arithmetic against another number. */
export function isExact(c: Count): c is { kind: "exact"; value: number } {
  return c.kind === "exact";
}

/**
 * Read a `Count` out of a `/api/tasks` or `/api/task-total` response.
 *
 * Anything that is not a finite number becomes `unmeasured` — including `null`,
 * which `/api/task-total` sends deliberately when the count could not be read,
 * and `undefined`, which is what a 502 error body has instead of a total.
 * `NaN`/`Infinity` are excluded too: they would otherwise pass a `typeof`
 * check and render as "NaN" beside real figures.
 */
export function countFrom(body: { total?: unknown; total_saturated?: unknown }): Count {
  if (typeof body.total !== "number" || !Number.isFinite(body.total)) {
    return { kind: "unmeasured" };
  }
  return body.total_saturated === true
    ? { kind: "atLeast", value: body.total }
    : { kind: "exact", value: body.total };
}

/**
 * How many rows are missing from a page, or null when that cannot be said.
 *
 * Null is returned whenever the shortfall would be a GUESS: an unmeasured total
 * says nothing about whether rows are missing, and a saturated one bounds the
 * shortfall without fixing it. Subtracting either would print a specific number
 * of missing tasks derived from a figure that is not a count.
 */
export function missingRows(total: Count, received: number): number | null {
  return isExact(total) && total.value > received ? total.value - received : null;
}

/**
 * Is the page a partial view of its own query?
 *
 * True on a saturated total as well as an exact shortfall — "at least 10,000
 * match and 200 are shown" is a truncation even though the exact remainder is
 * unknown. Unmeasured totals yield false: with no denominator there is no
 * evidence of truncation to assert, and claiming one would be inventing a
 * finding in the opposite direction.
 */
export function isTruncated(total: Count, received: number): boolean {
  if (total.kind === "unmeasured") return false;
  return total.value > received;
}
