/**
 * IS THIS LIST THE WHOLE LIST?
 * ----------------------------
 * Every view in this app that renders rows asks Neotoma for at most N of them
 * and then prints `entities.length` somewhere as a figure. When N exceeds the
 * store the two numbers coincide and the figure is right; when it does not, the
 * page states a cap as a count and nothing on screen says so.
 *
 * That is the defect PR #691 fixed on the tasks page, where "Pending 105"
 * described the newest 200 rows and read as a property of 5,566 open tasks.
 * This module is the same contract generalised, because the tasks page was not
 * the only list — the sessions index, the agent directory, the workflow table,
 * and the open-questions rail each print a page-scoped figure the same way.
 *
 * WHY A SHARED MODULE RATHER THAN A FIX PER PAGE. Three of the four are
 * currently BELOW their cap (344 digests under a 400 limit, 43 agents under
 * 200, 8 workflows under 100), so a per-page fix would be four copies of a
 * banner nobody can see, and four opportunities to drift. This codebase already
 * carries four divergent copies of one gate set; the honesty contract should
 * not become the fifth. One `Coverage` value, computed one way, rendered by one
 * component.
 *
 * WHAT MAKES A LIST "COMPLETE" HERE is not that it looks full. It is that
 * upstream reported a total and the rows in hand account for all of it. A list
 * whose total could not be read is `unknown` — never `complete`, because
 * "as many rows as I asked for" is exactly what a truncated page looks like.
 *
 * RELATIONSHIP TO `taskCount.ts`. That module models a COUNT (exact / at-least
 * / unmeasured) for the tasks page's chips and header. This one models the
 * narrower question every list asks: are rows missing, and how many. It reuses
 * `Count` rather than inventing a second vocabulary for the same uncertainty.
 */
import { type Count, countFrom, isExact } from "./taskCount";

/**
 * What a list knows about its own completeness.
 *
 * Three states, and the middle one is the reason the type exists: a list can
 * fail to learn its total without failing to load, and that is neither
 * "complete" nor a measured shortfall.
 */
export type Coverage =
  /** Upstream's total equals the rows in hand. Nothing is missing. */
  | { kind: "complete"; total: number }
  /** Rows are missing. `missing` is null when upstream's total is a lower bound. */
  | { kind: "partial"; received: number; total: Count; missing: number | null }
  /** No usable total came back. Truncation can be neither asserted nor ruled out. */
  | { kind: "unknown"; received: number };

/**
 * Read a list response's coverage.
 *
 * `received` is passed separately rather than read off the body because callers
 * routinely filter rows out after parsing (the tasks page pulls questions into
 * the sidebar), and the count that matters for truncation is how many rows
 * UPSTREAM sent, not how many survived a client-side split.
 *
 * The unknown case is deliberately not collapsed into `complete`. A response
 * with no total looks identical whether it holds every row or the first page of
 * many, and guessing in the reassuring direction is the failure this whole
 * module exists to prevent.
 */
export function coverageOf(
  body: { total?: unknown; total_saturated?: unknown },
  received: number,
): Coverage {
  const total = countFrom(body);

  if (total.kind === "unmeasured") return { kind: "unknown", received };

  if (isExact(total) && total.value <= received) {
    return { kind: "complete", total: total.value };
  }

  return {
    kind: "partial",
    received,
    total,
    // A saturated total bounds the shortfall without fixing it, so subtracting
    // would print a specific number of missing rows derived from a clamp.
    missing: isExact(total) ? total.value - received : null,
  };
}

/**
 * The figure a list prints beside its own name, as text.
 *
 * The single place a coverage becomes a countable phrase, so "N sessions" can
 * never be produced without going through the check that N is the whole set.
 * A partial list always prints BOTH numbers — "200 of 5,566" — because the
 * received count on its own is precisely the misleading form.
 *
 * `noun` is the singular ("session"), pluralised by adding "s"; every noun this
 * app needs is regular. Pass `nounPlural` where it is not.
 */
export function coverageText(
  coverage: Coverage,
  noun: string,
  nounPlural = `${noun}s`,
): string {
  const word = (n: number) => (n === 1 ? noun : nounPlural);

  switch (coverage.kind) {
    case "complete":
      return `${coverage.total.toLocaleString()} ${word(coverage.total)}`;
    case "partial": {
      const shown = coverage.received.toLocaleString();
      // `unmeasured` cannot reach here — `coverageOf` routes it to `unknown` —
      // but the compiler is right that the union permits it, and a fallback
      // that invented a denominator would be the exact bug this module exists
      // to prevent. So it degrades to the honest phrase instead.
      if (coverage.total.kind === "unmeasured") {
        return `${shown} ${nounPlural} loaded, total unknown`;
      }
      return coverage.total.kind === "atLeast"
        ? `${shown} of at least ${coverage.total.value.toLocaleString()} ${nounPlural}`
        : `${shown} of ${coverage.total.value.toLocaleString()} ${nounPlural}`;
    }
    case "unknown":
      return `${coverage.received.toLocaleString()} ${word(coverage.received)} loaded, total unknown`;
  }
}

/** True when the operator is looking at less than the whole set. */
export function isPartial(coverage: Coverage): coverage is Extract<Coverage, { kind: "partial" }> {
  return coverage.kind === "partial";
}
