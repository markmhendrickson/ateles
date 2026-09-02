/**
 * TRUE FACET COUNTS FOR THE BUCKET CHIPS.
 *
 * The chips used to count the rows the page had loaded. "Pending 105" was a
 * count of the newest 200 rows and read as a count of the 5,566 open tasks;
 * the true figure is 4,912. This module turns `/api/facets` — per-status
 * counts over the WHOLE open set — into the same `Count` values the rest of
 * the page already uses, so a chip either shows a measured figure or says it
 * was not measured, and never shows a fabricated zero.
 *
 * The status -> bucket fold stays in `toBucket()` and is applied HERE, on the
 * client, for the reason `fetchFacets` documents: one definition of the
 * mapping, shared by the chip's count and the chip's filter. If the two ever
 * disagreed, the count would be describing a different set than the one
 * clicking the chip produces.
 */
import { BUCKETS, type Bucket, toBucket } from "./tasks";
import type { Count } from "./taskCount";

/** Shape of `/api/facets`. Every count may be null — see `fetchFacets`. */
export interface FacetsResponse {
  statuses?: Record<string, number | null> | null;
  total?: unknown;
  complete?: unknown;
  reconciled?: unknown;
}

/** Chip key -> the count to print beside it. */
export type FacetCounts = Record<Bucket | "all", Count>;

/**
 * Fold per-status counts onto bucket counts.
 *
 * A bucket is `exact` only when EVERY status folding into it was measured.
 * One unreadable status makes the whole bucket `unmeasured` rather than a
 * silent undercount: printing the sum of the statuses that happened to answer
 * would be a specific number that is wrong in the direction of "less work than
 * there is", which is exactly the failure this page keeps having.
 */
export function foldFacets(body: FacetsResponse): FacetCounts {
  const out = {} as FacetCounts;
  const statuses = body.statuses;

  // `all` is the open-set aggregate, measured directly rather than summed.
  out.all =
    typeof body.total === "number" && Number.isFinite(body.total)
      ? { kind: "exact", value: body.total }
      : { kind: "unmeasured" };

  if (!statuses || typeof statuses !== "object") {
    for (const b of BUCKETS) out[b] = { kind: "unmeasured" };
    return out;
  }

  const sums = new Map<Bucket, number>();
  const failed = new Set<Bucket>();

  for (const [status, value] of Object.entries(statuses)) {
    const bucket = toBucket(status);
    if (typeof value !== "number" || !Number.isFinite(value)) {
      failed.add(bucket);
      continue;
    }
    sums.set(bucket, (sums.get(bucket) ?? 0) + value);
  }

  for (const b of BUCKETS) {
    // A bucket no open status maps onto — `done` — is a real, measured zero:
    // the query covers only open work, so no done task can be in scope.
    out[b] = failed.has(b)
      ? { kind: "unmeasured" }
      : { kind: "exact", value: sums.get(b) ?? 0 };
  }

  return out;
}
