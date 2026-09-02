/**
 * THE ONE TRUNCATION NOTICE.
 *
 * Rendered by every list view that can be cut short by its own page limit, so
 * that "this list is partial" is worded, styled, and — most importantly —
 * DECIDED the same way everywhere. The tasks page keeps its own richer banner
 * (it has filters and a saturation ceiling to explain); this is the general
 * form for the lists that have neither.
 *
 * A banner that only appears when rows are missing is a banner nobody ever
 * proofreads, so the same module also renders the ordinary case: `CoverageCount`
 * is the label a list prints beside its name, and it goes through the same
 * `Coverage` value. That is what keeps the healthy path honest — the count and
 * the warning cannot describe different sets, because there is only one set.
 */
import { type Coverage, coverageText, isPartial } from "./listTotal";

/**
 * The count beside a list's name — "344 sessions", "200 of 5,566 tasks".
 *
 * `failed` suppresses the figure entirely. A failed read leaves zero rows in
 * hand, and "0 sessions" is a fabricated measurement of exactly the kind this
 * dashboard exists to catch.
 */
export function CoverageCount({
  coverage,
  noun,
  nounPlural,
  failed = false,
}: {
  coverage: Coverage;
  noun: string;
  nounPlural?: string;
  failed?: boolean;
}) {
  return (
    <span className="text-[12px] tabular-nums text-muted-foreground">
      {failed ? "count unavailable" : coverageText(coverage, noun, nounPlural)}
    </span>
  );
}

/**
 * The banner. Renders nothing at all when the list is whole.
 *
 * `sortNote` names the bias the cut introduces. Every list here is ordered
 * `last_observation_at desc`, so a truncated page is not a random sample of the
 * set but systematically the most recent part of it — which is the property
 * that turns a short list into a wrong impression. A page-limited list of old
 * work shows as almost nothing, and the reader has no way to know unless the
 * page says so.
 */
export function CoverageNotice({
  coverage,
  noun,
  nounPlural = `${noun}s`,
  sortNote = "Rows are ordered newest-first, so what is shown over-represents recent work.",
}: {
  coverage: Coverage;
  noun: string;
  nounPlural?: string;
  sortNote?: string;
}) {
  if (!isPartial(coverage)) return null;

  return (
    <div className="my-[10px] flex flex-wrap items-baseline gap-x-2 rounded-lg border border-warn bg-[hsl(var(--warn)/0.12)] px-3 py-[8px] text-[12.5px]">
      <strong>This list is partial.</strong>
      <span>
        {coverage.missing !== null ? (
          <>
            <span className="tabular-nums">{coverage.missing.toLocaleString()}</span> more{" "}
            {coverage.missing === 1 ? noun : nounPlural}{" "}
            {coverage.missing === 1 ? "is" : "are"} not shown.
          </>
        ) : (
          <>
            More {nounPlural} exist than are shown, but how many more is unknown — upstream
            stopped counting, so the shortfall can be asserted but not measured.
          </>
        )}{" "}
        {sortNote}
      </span>
    </div>
  );
}
