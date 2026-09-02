/**
 * SCHEMAS
 * -------
 * The tab that makes the other tabs trustworthy.
 *
 * Neotoma holds ~167,000 entities across ~739 populated types, and the registry
 * describing them has drifted from the data. That drift is the finding, and it
 * is only ever discovered by one-off audits months apart — `checkpoint_brief`
 * declares four status values while production holds nine, and the two the
 * dispatcher and the MCP server actually use (`open`, `awaiting_operator`) are
 * both undeclared, which is why 141 blocking briefs could be neither listed nor
 * resolved. A live view turns that from a rediscovery into a glance.
 *
 * WHAT IS MEASURED, AND WHAT IS ONLY SAMPLED
 * ------------------------------------------
 * Entity counts are exact (from `/stats`). Populated-field counts and drift are
 * SAMPLE-derived — there is no aggregate endpoint for either, and `task` alone
 * has 21,065 entities. So the sample size is printed next to every number that
 * depends on it, and a field is reported as "unpopulated in the sample" rather
 * than "dead". The distinction is the whole point: this tab exists to expose
 * confident claims that outran their evidence, so it must not make one.
 *
 * THE READER COLUMN COMES FROM THE SOURCE, AND SHOWS WHEN IT HAS GONE STALE
 * -------------------------------------------------------------------------
 * "Does anything read this type" is not answerable by any Neotoma query —
 * Neotoma knows what was written, never what reads it. It is computed by a grep
 * over the ateles repo (`codeUsage.ts`), so it is a snapshot taken at a commit.
 *
 * That it is a snapshot was once announced by a permanent banner. A warning
 * that is always on reports nothing and goes unread, so it is gone. In its
 * place: the scan's commit is compared to the repo's live HEAD, and the page
 * speaks up only when they have diverged — with the regenerate command, which
 * is an action the reader can take.
 *
 * The method's blindness is real but NARROW: it cannot prove a negative. It
 * independently reproduced findings a separate Neotoma-side audit had made, so
 * a nonzero count is evidence, not a guess. The caveat therefore hangs on the
 * ZEROS alone — where "none found" could be mistaken for "none exists" — and
 * the full account lives behind the Method disclosure.
 *
 * READ-ONLY, like every view here: GETs to /api/schemas and
 * /api/scan-freshness, no mutation.
 */
import { Fragment, useEffect, useMemo, useState } from "react";
import {
  type SchemaRow,
  type SchemasPayload,
  type TailBucket,
  ageDays,
  day,
  driftCount,
  driftedEntities,
  measuredSample,
  staleness,
} from "./schemaData";
import { CODE_USAGE, SCAN_META } from "./codeUsage";
import { SchemaTableSkeleton } from "@/components/Skeletons";
import { showSkeleton } from "@/lib/loading";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { AlertTriangle, ChevronRight, Database, Info } from "lucide-react";

const num = (n: number) => n.toLocaleString();

/**
 * The header note. States what is exact, what is sampled, and what is static —
 * before any table, so a reader cannot take a sampled figure for a measured one
 * on the way to the data.
 */
function ProvenanceNote({ sampleSize, types }: { sampleSize: number; types: number }) {
  return (
    <section className="mb-3 rounded-lg border bg-card p-3">
      <div className="flex items-start gap-[10px]">
        <Info className="mt-[2px] h-4 w-4 flex-none text-muted-foreground" aria-hidden />
        <div className="min-w-0 text-[12.5px] leading-[1.45] text-muted-foreground">
          <h2 className="text-[14px] font-[650] tracking-[-0.01em] text-foreground">
            Three different kinds of number on this page
          </h2>
          <p className="mt-[6px]">
            <strong className="text-foreground">Entity counts are exact</strong>, straight from{" "}
            <code className="font-mono text-[11px]">/stats</code> — all {num(types)} populated
            types.
          </p>
          <p className="mt-[5px]">
            <strong className="text-foreground">Field population and drift are sampled.</strong>{" "}
            Neotoma has no aggregate for either, so each type is measured over its most recent
            entities — up to {sampleSize}, fewer for a couple of very large types that cannot be
            read that deeply in time. The{" "}
            <strong className="text-foreground">Sample column gives the real figure per row</strong>
            , and every number on that row is derived from it. A field shown as unpopulated is
            unpopulated <em>in that sample</em> — a weaker claim than dead, deliberately, and
            sample size changes the answer: <code className="font-mono text-[11px]">task</code>{" "}
            surfaces 17 populated fields at 150 and 49 at 400, because its rarest fields sit on a
            single entity each.
          </p>
          <p className="mt-[5px]">
            <strong className="text-foreground">Readers and Writers are read from the source</strong>{" "}
            — no Neotoma query can report which code reads a type, so those two columns are grepped
            out of the repo. A count is what the scan found. A <em>zero</em> is the only figure the
            method can get wrong in the misleading direction, and each one carries its own caveat.
          </p>
        </div>
      </div>
    </section>
  );
}

/**
 * IS THE CODE SCAN STALE?
 *
 * This replaced a permanent banner that read "Readers and Writers come from a
 * code scan, not from Neotoma". That was true, and useless: it was present on
 * every load regardless of the world's state, so it reported nothing and — as
 * warnings that never change do — stopped being read.
 *
 * A caveat earns a place on screen only if it can turn off. This one can: the
 * scan's commit is compared against the repo's actual HEAD, and the row is
 * loud only when the two have diverged, at which point it names the fix. When
 * they agree there is nothing to act on and it says so in one quiet line.
 *
 * The METHOD's limits did not disappear — they moved to where they bite, which
 * is a zero (see `ZeroUsage`), plus this panel for anyone who wants the whole
 * account. What went away is the assertion of them above rows they do not
 * apply to: a count of 4 readers is positive evidence and needs no hedge.
 */
type Freshness = {
  status: "current" | "behind" | "unknown";
  head: string | null;
  scanCommit: string;
  behindBy: number | null;
  reason?: string;
};

const REGEN_CMD = "python3 apps/task-dashboard/scripts/generate_code_usage.py";

function ScanFreshness() {
  const [open, setOpen] = useState(false);
  const [fresh, setFresh] = useState<Freshness | null>(null);

  /**
   * Asked once per mount, not polled. HEAD moves when the operator commits,
   * which is not a thing that happens while they stare at this table — and a
   * git call every 15s to re-answer a question whose answer almost never
   * changes would be cost without information.
   */
  useEffect(() => {
    let cancelled = false;
    fetch(`/api/scan-freshness?commit=${encodeURIComponent(SCAN_META.commit)}`)
      .then((r) => r.json())
      .then((body) => {
        if (!cancelled) setFresh(body as Freshness);
      })
      .catch(() => {
        // The freshness check failing is not itself a finding about the scan.
        // Fall back to stating the scan's own provenance, which is always true.
        if (!cancelled)
          setFresh({
            status: "unknown",
            head: null,
            scanCommit: SCAN_META.commit,
            behindBy: null,
            reason: "The freshness check could not be reached.",
          });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const behind = fresh?.status === "behind";

  return (
    <div
      className={cn(
        "mb-[6px] rounded-lg border px-[10px] py-[7px]",
        behind
          ? "border-[hsl(var(--warn)/0.35)] bg-[hsl(var(--warn)/0.08)]"
          : "border-border/60 bg-muted/[.25]",
      )}
    >
      <div className="flex flex-wrap items-baseline gap-x-[7px] gap-y-[3px] text-[12px] leading-[1.45]">
        {behind ? (
          <>
            <Badge variant="warn" className="text-[10px]" caps>
              stale
            </Badge>
            <span className="text-muted-foreground">
              <strong className="text-foreground">
                The code scan is {fresh?.behindBy} commit{fresh?.behindBy === 1 ? "" : "s"} behind.
              </strong>{" "}
              Readers and Writers describe the repo at{" "}
              <code className="font-mono text-[11px]">{SCAN_META.commit}</code>; it is now at{" "}
              <code className="font-mono text-[11px]">{fresh?.head}</code>. Regenerate with{" "}
              <code className="font-mono text-[11px]">{REGEN_CMD}</code>.
            </span>
          </>
        ) : fresh?.status === "current" ? (
          <span className="text-muted-foreground">
            <span className="text-[hsl(var(--ok))]">✓</span> Code scan current — generated at{" "}
            <code className="font-mono text-[11px]">{SCAN_META.commit}</code>, which is HEAD.{" "}
            {SCAN_META.filesScanned} files, {day(SCAN_META.generatedAt)}.
          </span>
        ) : (
          /* Unknown is NOT stale. Git could not answer, so report the scan's
             own provenance and let the reader judge, rather than manufacturing
             a drift number — an unanswerable question that looks like a known
             bad answer teaches people to ignore the real one. */
          <span className="text-muted-foreground">
            Code scan generated at{" "}
            <code className="font-mono text-[11px]">{SCAN_META.commit}</code>,{" "}
            {SCAN_META.filesScanned} files, {day(SCAN_META.generatedAt)}. Age against the current
            checkout is unknown{fresh?.reason ? ` — ${fresh.reason.toLowerCase()}` : ""}
          </span>
        )}
        <Button
          variant="chip"
          size="chip"
          className="ml-auto"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
        >
          {open ? "Hide method" : "Method"}
        </Button>
      </div>
      {open && (
        <div
          className={cn(
            "mt-[7px] border-t pt-[7px] text-[12px] leading-[1.45] text-muted-foreground",
            behind ? "border-[hsl(var(--warn)/0.25)]" : "border-border/60",
          )}
        >
          <p>
            <strong className="text-foreground">Where these two columns come from.</strong> No
            Neotoma query can report which code <em>reads</em> a type — Neotoma records what was
            written, never what reads it. So Readers and Writers are computed by grep over the{" "}
            <code className="font-mono text-[11px]">ateles</code> repo. Every other column on this
            page is live.
          </p>
          <p className="mt-[6px]">
            <strong className="text-foreground">It reproduced known findings independently</strong>{" "}
            — <code className="font-mono text-[11px]">harness_event</code> at 1 writer and no
            reader, <code className="font-mono text-[11px]">agent_message</code> at 2 and none —
            which a separate Neotoma-side audit had found on its own. It works; it just cannot prove
            a negative.
          </p>
          <ul className="mt-[6px] list-disc space-y-[3px] pl-[18px]">
          <li>
            <strong className="text-foreground">Proximity, not call graph.</strong> A query call
            within 6 lines of a string literal naming the type. A type read through a variable is
            invisible to it.
          </li>
          <li>
            <strong className="text-foreground">A zero means "none found by this method"</strong>,
            never "none exists". Treat it as a lead to check, not a verdict — which is why the
            caveat is attached to the zeros themselves and to nothing else.
          </li>
          <li>
            <strong className="text-foreground">One repo.</strong> A reader in{" "}
            <code className="font-mono text-[11px]">neotoma</code>,{" "}
            <code className="font-mono text-[11px]">openclaw</code>, or an operator script is out of
            scope and uncounted.
          </li>
          <li>
            <strong className="text-foreground">Tests excluded from the counts.</strong> A type
            whose only reader is its own test is not read in production; those rows are marked{" "}
            <em>test only</em>.
          </li>
          <li>
            Regenerate with <code className="font-mono text-[11px]">{REGEN_CMD}</code>.
          </li>
          </ul>
        </div>
      )}
    </div>
  );
}

/**
 * The zero cell — where the method's blindness actually matters.
 *
 * A nonzero count is positive evidence: the scan FOUND those call sites, and
 * hedging it would be false modesty. A zero is the only reading the method can
 * get wrong in the direction that misleads, because it cannot distinguish "no
 * reader exists" from "no reader written in a form I can see". So the caveat
 * lives here, on the cells it describes, rather than above every row.
 */
function ZeroUsage({
  kind,
  testOnly,
}: {
  kind: "reader" | "writer";
  testOnly: boolean;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge variant="warn" className="text-[10.5px]">
          none found
        </Badge>
      </TooltipTrigger>
      <TooltipContent className="max-w-[340px]">
        <strong>None found by this method</strong> — not proof none exists. The scan looks for a
        call within 6 lines of a string literal naming the type, so a {kind} that reaches this type
        through a variable is invisible to it, as is one living in{" "}
        <code className="font-mono text-[11px]">neotoma</code>,{" "}
        <code className="font-mono text-[11px]">openclaw</code>, or an operator script.
        {testOnly && " Readers exist here, but only inside tests — none in production."} Confirm in
        the source before treating this type as {kind === "reader" ? "write-only" : "read-only"}.
      </TooltipContent>
    </Tooltip>
  );
}

/** The drift detail for one type, expanded under its row. */
function DriftDetail({ row }: { row: SchemaRow }) {
  const a = row.analysis;
  if (!a) return null;

  return (
    <tr className="border-b border-border/60 bg-muted/[.25]">
      <td colSpan={7} className="px-2 py-[8px]">
        {a.note && <p className="text-[12px] text-muted-foreground">{a.note}</p>}

        {a.description && (
          <p className="mb-[7px] text-[12px] leading-[1.45] text-muted-foreground">
            {a.description}
          </p>
        )}

        {a.drift.map((d) => (
          <div key={d.field} className="mb-[8px]">
            <div className="text-[12px] font-[600]">
              <code className="font-mono text-[11.5px]">{d.field}</code>
              {d.undeclared.length > 0 ? (
                <span className="ml-[6px] font-[400] text-[hsl(var(--bad))]">
                  {d.undeclared.length} undeclared{" "}
                  {d.undeclared.length === 1 ? "value" : "values"}
                </span>
              ) : (
                <span className="ml-[6px] font-[400] text-muted-foreground">
                  all observed values declared
                </span>
              )}
            </div>
            <div className="mt-[3px] text-[12px] leading-[1.5] text-muted-foreground">
              <span className="text-foreground">Declared:</span>{" "}
              {d.declared.map((v) => (
                <code key={v} className="mr-[5px] font-mono text-[11px]">
                  {v}
                </code>
              ))}
            </div>
            <div className="mt-[3px] flex flex-wrap items-baseline gap-[5px] text-[12px]">
              <span className="text-foreground">Observed:</span>
              {d.observed.map((o) => {
                const bad = !d.declared.includes(o.value);
                return (
                  <Tooltip key={o.value}>
                    <TooltipTrigger asChild>
                      <Badge variant={bad ? "bad" : "muted"} className="text-[10.5px]">
                        <code className="font-mono">{o.value}</code>
                        <span className="ml-[4px] tabular-nums opacity-80">{o.count}</span>
                      </Badge>
                    </TooltipTrigger>
                    <TooltipContent>
                      {bad
                        ? `"${o.value}" on ${o.count} sampled ${
                            o.count === 1 ? "entity" : "entities"
                          } — the schema does not declare it.`
                        : `Declared value, ${o.count} sampled.`}
                    </TooltipContent>
                  </Tooltip>
                );
              })}
            </div>
          </div>
        ))}

        {a.deadFields.length > 0 && (
          <div className="mt-[7px] text-[12px] leading-[1.5]">
            <span className="font-[600]">
              Declared but unpopulated in the sample ({a.deadFields.length})
            </span>
            <span className="text-muted-foreground"> — measured over {a.sampled} entities:</span>
            <div className="mt-[3px] text-muted-foreground">
              {a.deadFields.map((f) => (
                <code key={f} className="mr-[6px] font-mono text-[11px]">
                  {f}
                </code>
              ))}
            </div>
          </div>
        )}

        {a.undeclaredFields.length > 0 && (
          <div className="mt-[7px] text-[12px] leading-[1.5]">
            <span className="font-[600] text-[hsl(var(--warn))]">
              Populated but NOT declared ({a.undeclaredFields.length})
            </span>
            <span className="text-muted-foreground">
              {" "}
              — written by code, absent from the schema:
            </span>
            <div className="mt-[3px] text-muted-foreground">
              {a.undeclaredFields.map((f) => (
                <code key={f} className="mr-[6px] font-mono text-[11px]">
                  {f}
                </code>
              ))}
            </div>
          </div>
        )}

        {(CODE_USAGE[row.entityType]?.readers.length ||
          CODE_USAGE[row.entityType]?.writers.length) && (
          <div className="mt-[8px] border-t pt-[6px] text-[12px] leading-[1.5]">
            <span className="font-[600]">Code sites</span>
            <span className="text-muted-foreground"> — from the code scan; see Method above.</span>
            {CODE_USAGE[row.entityType].readers.length > 0 && (
              <div className="mt-[3px] text-muted-foreground">
                <span className="text-foreground">reads: </span>
                {CODE_USAGE[row.entityType].readers.map((r) => (
                  <code key={r} className="mr-[7px] font-mono text-[11px]">
                    {r}
                  </code>
                ))}
              </div>
            )}
            {CODE_USAGE[row.entityType].writers.length > 0 && (
              <div className="mt-[2px] text-muted-foreground">
                <span className="text-foreground">writes: </span>
                {CODE_USAGE[row.entityType].writers.map((w) => (
                  <code key={w} className="mr-[7px] font-mono text-[11px]">
                    {w}
                  </code>
                ))}
              </div>
            )}
          </div>
        )}
      </td>
    </tr>
  );
}

/** One canonical-type row. */
function CanonicalRow({
  row,
  expanded,
  onToggle,
}: {
  row: SchemaRow;
  expanded: boolean;
  onToggle: () => void;
}) {
  const a = row.analysis;
  const drifted = driftCount(a);
  const affected = driftedEntities(a);
  const usage = CODE_USAGE[row.entityType];
  /**
   * Did the sample actually come back?
   *
   * A type with entities but `sampled === 0` was NOT measured — the query
   * failed or timed out. Every figure derived from the sample (populated
   * fields, drift) is meaningless for that row and must say so rather than
   * print a zero. A genuinely empty type (`count === 0`) IS measured: zero
   * entities is a real answer, not a missing one.
   */
  const measured = measuredSample(row);

  return (
    <>
      <tr
        className="cursor-pointer border-b border-border/60 hover:bg-muted/40"
        onClick={onToggle}
        aria-expanded={expanded}
      >
        <td className="py-[4px] pr-2">
          <div className="flex items-center gap-[4px]">
            <ChevronRight
              className={cn(
                "h-[13px] w-[13px] flex-none text-muted-foreground transition-transform",
                expanded && "rotate-90",
              )}
              aria-hidden
            />
            <code className="font-mono text-[12px] font-[600]">{row.entityType}</code>
          </div>
        </td>

        <td className="py-[4px] pr-2 text-right tabular-nums">{num(row.count)}</td>

        {/* Declared vs populated — always with the sample it was measured over.

            A populated-field count is only meaningful if a sample was actually
            READ. `sampled === 0` on a type that has entities means the query
            did not come back, and printing "0 / 83" there would be a confident
            wrong answer rather than a measurement — see `measured` below. */}
        <td className="py-[4px] pr-2 text-[12px] text-muted-foreground">
          {!row.analyzed ? (
            <span className="opacity-60">reading…</span>
          ) : a && a.declaredFields && !measured ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="text-[11.5px] text-[hsl(var(--warn))]">not measured</span>
              </TooltipTrigger>
              <TooltipContent className="max-w-[340px]">
                {a.declaredFields} fields declared, but no entities could be sampled — so how many
                are populated is unknown. This type has {num(row.count)} entities, so it is not
                empty; the sample query did not return. Retried on the next poll.
              </TooltipContent>
            </Tooltip>
          ) : a && a.declaredFields ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="tabular-nums">
                  <span
                    className={cn(
                      "text-foreground",
                      a.populatedFields < a.declaredFields / 2 && "text-[hsl(var(--warn))]",
                    )}
                  >
                    {a.populatedFields}
                  </span>
                  <span className="opacity-70"> / {a.declaredFields}</span>
                </span>
              </TooltipTrigger>
              <TooltipContent className="max-w-[340px]">
                {a.populatedFields} of {a.declaredFields} declared fields carried a value in the{" "}
                {a.sampled} most recent entities. The rest are unpopulated in that sample — not
                proven dead across all {num(row.count)}.
              </TooltipContent>
            </Tooltip>
          ) : (
            <span className="opacity-60">—</span>
          )}
        </td>

        {/* Sample denominator, never hidden. */}
        <td className="py-[4px] pr-2 text-right text-[11.5px] tabular-nums text-muted-foreground">
          {row.analyzed && a ? (a.sampled === 0 ? "—" : a.sampled) : ""}
        </td>

        {/* Drift. Pending must not read as clean. */}
        <td className="py-[4px] pr-2">
          {!row.analyzed ? (
            <span className="text-[11.5px] text-muted-foreground opacity-60">—</span>
          ) : !measured ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="text-[11.5px] text-[hsl(var(--warn))]">not measured</span>
              </TooltipTrigger>
              <TooltipContent className="max-w-[340px]">
                Drift is computed from sampled values, and no entities could be sampled for this
                type. Unknown, not clean.
              </TooltipContent>
            </Tooltip>
          ) : drifted > 0 ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <Badge variant="bad" className="text-[10.5px]">
                  {drifted} undeclared
                  <span className="ml-[4px] tabular-nums opacity-80">
                    · {num(affected)} rows
                  </span>
                </Badge>
              </TooltipTrigger>
              <TooltipContent className="max-w-[340px]">
                Code writes {drifted} value{drifted === 1 ? "" : "s"} the schema does not declare,
                on {num(affected)} sampled {affected === 1 ? "entity" : "entities"}. Expand for the
                values.
              </TooltipContent>
            </Tooltip>
          ) : a && a.drift.length > 0 ? (
            <Badge variant="ok" className="text-[10.5px]">
              declared
            </Badge>
          ) : (
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="text-[11.5px] text-muted-foreground">no enum declared</span>
              </TooltipTrigger>
              <TooltipContent className="max-w-[340px]">
                No field on this type declares a closed value set, so there is nothing to compare
                live values against. Not the same as "checked and clean".
              </TooltipContent>
            </Tooltip>
          )}
        </td>

        {/* Static columns. A count is stated plainly; only a ZERO carries the
            method caveat, because only a zero can be wrong in the misleading
            direction. */}
        <td className="py-[4px] pr-2 text-right text-[11.5px] tabular-nums">
          {usage ? (
            usage.readerCount === 0 ? (
              <ZeroUsage kind="reader" testOnly={usage.testOnlyReaders} />
            ) : (
              <span className="text-muted-foreground">{usage.readerCount}</span>
            )
          ) : (
            <span className="text-muted-foreground opacity-60">—</span>
          )}
        </td>
        <td className="py-[4px] text-right text-[11.5px] tabular-nums text-muted-foreground">
          {usage ? (
            usage.writerCount === 0 ? (
              <ZeroUsage kind="writer" testOnly={false} />
            ) : (
              usage.writerCount
            )
          ) : (
            <span className="opacity-60">—</span>
          )}
        </td>
      </tr>

      {expanded && <DriftDetail row={row} />}
    </>
  );
}

/** Section 1 — the canonical types, ordered by significance. */
function CanonicalSection({ rows, pending }: { rows: SchemaRow[]; pending: boolean }) {
  const withDrift = rows.filter((r) => driftCount(r.analysis) > 0).length;
  const [open, setOpen] = useState<Set<string>>(new Set());
  const toggle = (t: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(t)) next.delete(t);
      else next.add(t);
      return next;
    });

  return (
    <section className="mt-[14px]">
      <div className="mb-[3px] flex flex-wrap items-baseline gap-[8px]">
        <h2 className="text-[13px] font-[650] tracking-[-0.01em]">Canonical types</h2>
        <span className="text-[12px] text-muted-foreground">
          {rows.length} types the swarm runs on
          {withDrift > 0 && (
            <>
              {" · "}
              <span className="text-[hsl(var(--bad))]">{withDrift} with drift</span>
            </>
          )}
        </span>
      </div>

      <ScanFreshness />

      {pending ? (
        <SchemaTableSkeleton rows={8} />
      ) : (
        <table className="w-full border-collapse text-[12.5px]">
          <thead>
            <tr className="border-b text-[10px] uppercase tracking-[.06em] text-muted-foreground">
              <th className="py-[5px] pr-2 text-left font-[600]">Type</th>
              <th className="w-[74px] py-[5px] pr-2 text-right font-[600]">Entities</th>
              <th className="w-[92px] py-[5px] pr-2 text-left font-[600]">Fields used</th>
              <th className="w-[58px] py-[5px] pr-2 text-right font-[600]">Sample</th>
              <th className="w-[168px] py-[5px] pr-2 text-left font-[600]">Value drift</th>
              <th className="w-[86px] py-[5px] pr-2 text-right font-[600]">
                Readers<span className="ml-[3px] normal-case opacity-70">(static)</span>
              </th>
              <th className="w-[64px] py-[5px] text-right font-[600]">
                Writers<span className="ml-[3px] normal-case opacity-70">(static)</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <CanonicalRow
                key={r.entityType}
                row={r}
                expanded={open.has(r.entityType)}
                onToggle={() => toggle(r.entityType)}
              />
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

/** Section 2 — configuration types, ordered by staleness (oldest first). */
function ConfigSection({ rows, pending }: { rows: SchemaRow[]; pending: boolean }) {
  const [open, setOpen] = useState<Set<string>>(new Set());
  const toggle = (t: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(t)) next.delete(t);
      else next.add(t);
      return next;
    });

  // Oldest first: the whole reason this section is separate is that a config
  // value nobody has touched in months is the one doing quiet damage.
  const sorted = useMemo(
    () =>
      [...rows].sort((a, b) => {
        const ta = a.analysis?.lastTouched ?? "";
        const tb = b.analysis?.lastTouched ?? "";
        if (!ta && !tb) return 0;
        if (!ta) return 1;
        if (!tb) return -1;
        return ta.localeCompare(tb);
      }),
    [rows],
  );

  return (
    <section className="mt-[18px]">
      <div className="mb-[3px] flex flex-wrap items-baseline gap-[8px]">
        <h2 className="text-[13px] font-[650] tracking-[-0.01em]">Configuration types</h2>
        <span className="text-[12px] text-muted-foreground">
          these govern behaviour rather than record work — oldest first
        </span>
      </div>
      <p className="mb-[6px] text-[12px] leading-[1.45] text-muted-foreground">
        A stale value here does quiet damage: it keeps working, so nothing fails loudly. Two real
        cases behind this section — a harness-headroom config untouched since 2026-08-01 that
        excluded a working provider, and an agent still bound to a pre-migration{" "}
        <code className="font-mono text-[11px]">localhost</code> URL after Neotoma moved to hosted.{" "}
        <strong className="text-foreground">Age is not itself a defect</strong> — config is supposed
        to be stable — but an unmaintained value is invisible without it.
      </p>

      {pending ? (
        <SchemaTableSkeleton rows={6} />
      ) : (
        <table className="w-full border-collapse text-[12.5px]">
          <thead>
            <tr className="border-b text-[10px] uppercase tracking-[.06em] text-muted-foreground">
              <th className="py-[5px] pr-2 text-left font-[600]">Type</th>
              <th className="w-[58px] py-[5px] pr-2 text-right font-[600]">Count</th>
              <th className="py-[5px] pr-2 text-left font-[600]">Current value summary</th>
              <th className="w-[104px] py-[5px] pr-2 text-left font-[600]">Last touched</th>
              <th className="w-[66px] py-[5px] text-right font-[600]">Age</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((row) => {
              const a = row.analysis;
              const days = ageDays(a?.lastTouched ?? null);
              const band = staleness(days);
              const expanded = open.has(row.entityType);
              return (
                <Fragment key={row.entityType}>
                  <tr
                    className="cursor-pointer border-b border-border/60 hover:bg-muted/40"
                    onClick={() => toggle(row.entityType)}
                    aria-expanded={expanded}
                  >
                    <td className="py-[4px] pr-2">
                      <div className="flex items-center gap-[4px]">
                        <ChevronRight
                          className={cn(
                            "h-[13px] w-[13px] flex-none text-muted-foreground transition-transform",
                            expanded && "rotate-90",
                          )}
                          aria-hidden
                        />
                        <code className="font-mono text-[12px] font-[600]">{row.entityType}</code>
                      </div>
                    </td>
                    <td className="py-[4px] pr-2 text-right tabular-nums">{num(row.count)}</td>
                    <td className="max-w-0 py-[4px] pr-2">
                      {!row.analyzed ? (
                        <span className="text-[12px] text-muted-foreground opacity-60">
                          reading…
                        </span>
                      ) : a && a.valueSummary.length ? (
                        <div className="truncate text-[12px] text-muted-foreground">
                          {a.valueSummary.map((v, i) => (
                            <span key={v.field}>
                              {i > 0 && " · "}
                              <span className="text-foreground">{v.field}</span>
                              {": "}
                              {v.value}
                            </span>
                          ))}
                        </div>
                      ) : row.count === 0 ? (
                        <span className="text-[12px] text-muted-foreground">
                          no entities of this type
                        </span>
                      ) : (
                        /* Entities exist but none were sampled: the value is
                           unknown, not absent. Saying "—" here would read as
                           "nothing configured", which is a different claim. */
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <span className="text-[12px] text-[hsl(var(--warn))]">
                              not measured
                            </span>
                          </TooltipTrigger>
                          <TooltipContent className="max-w-[340px]">
                            {num(row.count)} entities exist, but the sample query did not return, so
                            the current value could not be read. Retried on the next poll.
                          </TooltipContent>
                        </Tooltip>
                      )}
                    </td>
                    <td className="py-[4px] pr-2 text-[12px] tabular-nums text-muted-foreground">
                      {/* Same rule: an unread sample has no last-touched date to
                          report, and a dash would imply one was looked for and
                          not found. */}
                      {!row.analyzed ? "" : measuredSample(row) ? day(a?.lastTouched ?? null) : "?"}
                    </td>
                    <td className="py-[4px] text-right">
                      {row.analyzed && days !== null ? (
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <Badge
                              variant={
                                band === "stale" ? "bad" : band === "aging" ? "warn" : "muted"
                              }
                              className="text-[10.5px] tabular-nums"
                            >
                              {days}d
                            </Badge>
                          </TooltipTrigger>
                          <TooltipContent className="max-w-[320px]">
                            Newest observation across the sample is {days} days old. Config is meant
                            to be stable, so this is a prompt to check the value, not a defect on
                            its own.
                          </TooltipContent>
                        </Tooltip>
                      ) : null}
                    </td>
                  </tr>
                  {expanded && a && (
                    <tr className="border-b border-border/60 bg-muted/[.25]">
                      <td colSpan={5} className="px-2 py-[8px]">
                        {a.description && (
                          <p className="mb-[6px] text-[12px] leading-[1.45] text-muted-foreground">
                            {a.description}
                          </p>
                        )}
                        {a.valueSummary.length > 0 && (
                          <dl className="grid grid-cols-[130px_1fr] gap-x-[10px] gap-y-[2px] text-[12px]">
                            {a.valueSummary.map((v) => (
                              <div key={v.field} className="contents">
                                <dt className="truncate font-mono text-[11px] text-muted-foreground">
                                  {v.field}
                                </dt>
                                <dd className="break-words">{v.value}</dd>
                              </div>
                            ))}
                          </dl>
                        )}
                        <p className="mt-[6px] text-[11.5px] text-muted-foreground">
                          Summary is the newest entity of this type, over a sample of {a.sampled}.
                          {a.declaredFields > 0 && (
                            <>
                              {" "}
                              {a.populatedFields} of {a.declaredFields} declared fields populated in
                              the sample.
                            </>
                          )}
                        </p>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

/** Section 3 — the tail, bucketed. Nothing hidden; the counts partition it. */
function TailSection({ buckets }: { buckets: TailBucket[] }) {
  const [open, setOpen] = useState<string | null>(null);
  const types = buckets.reduce((n, b) => n + b.types, 0);
  const entities = buckets.reduce((n, b) => n + b.entities, 0);

  return (
    <section className="mt-[18px]">
      <div className="mb-[3px] flex flex-wrap items-baseline gap-[8px]">
        <h2 className="text-[13px] font-[650] tracking-[-0.01em]">Everything else</h2>
        <span className="text-[12px] text-muted-foreground">
          {num(types)} further types, {num(entities)} entities — bucketed, not hidden
        </span>
      </div>
      <p className="mb-[6px] text-[12px] leading-[1.45] text-muted-foreground">
        The buckets partition the whole remainder, so the tail is visible without 900 rows. The
        single-entity buckets are the registry pollution: a schema registered per correction and
        never reused again.
      </p>

      <table className="w-full border-collapse text-[12.5px]">
        <thead>
          <tr className="border-b text-[10px] uppercase tracking-[.06em] text-muted-foreground">
            <th className="py-[5px] pr-2 text-left font-[600]">Bucket</th>
            <th className="w-[58px] py-[5px] pr-2 text-right font-[600]">Types</th>
            <th className="w-[74px] py-[5px] pr-2 text-right font-[600]">Entities</th>
            <th className="py-[5px] text-left font-[600]">What it is</th>
          </tr>
        </thead>
        <tbody>
          {buckets.map((b) => {
            const expanded = open === b.key;
            return (
              <Fragment key={b.key}>
                <tr
                  className="cursor-pointer border-b border-border/60 hover:bg-muted/40"
                  onClick={() => setOpen(expanded ? null : b.key)}
                  aria-expanded={expanded}
                >
                  <td className="py-[4px] pr-2">
                    <div className="flex items-center gap-[4px]">
                      <ChevronRight
                        className={cn(
                          "h-[13px] w-[13px] flex-none text-muted-foreground transition-transform",
                          expanded && "rotate-90",
                        )}
                        aria-hidden
                      />
                      <span className="font-[600]">{b.label}</span>
                    </div>
                  </td>
                  <td className="py-[4px] pr-2 text-right tabular-nums">{num(b.types)}</td>
                  <td className="py-[4px] pr-2 text-right tabular-nums">{num(b.entities)}</td>
                  <td className="py-[4px] text-[12px] text-muted-foreground">{b.blurb}</td>
                </tr>
                {expanded && (
                  <tr className="border-b border-border/60 bg-muted/[.25]">
                    <td colSpan={4} className="px-2 py-[8px]">
                      <div className="flex flex-wrap gap-x-[10px] gap-y-[3px] text-[12px]">
                        {b.sample.map((t) => (
                          <span key={t.entityType} className="text-muted-foreground">
                            <code className="font-mono text-[11px] text-foreground">
                              {t.entityType}
                            </code>{" "}
                            <span className="tabular-nums">{num(t.count)}</span>
                          </span>
                        ))}
                      </div>
                      {b.types > b.sample.length && (
                        <p className="mt-[6px] text-[11.5px] text-muted-foreground">
                          Showing the {b.sample.length} largest of {num(b.types)}. The counts in the
                          row above cover all of them.
                        </p>
                      )}
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

export function Schemas() {
  const [data, setData] = useState<SchemasPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [firstLoadDone, setFirstLoadDone] = useState(false);

  /**
   * Polled, unlike Workflows: the analysis hydrates in the background upstream,
   * so a second and third poll are what fill in the drift columns. 15s rather
   * than the app's 10s because this response is heavier and nothing on it moves
   * minute to minute once hydrated.
   */
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch("/api/schemas");
        const body = await res.json();
        if (!res.ok || body.error) throw new Error(body.error ?? `HTTP ${res.status}`);
        if (!cancelled) {
          setData(body as SchemasPayload);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) setError((err as Error).message);
      } finally {
        if (!cancelled) setFirstLoadDone(true);
      }
    };
    void load();
    const id = setInterval(() => void load(), 15_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  const pending = showSkeleton(!firstLoadDone, data !== null);
  const analyzing = useMemo(() => {
    if (!data) return 0;
    return [...data.canonical, ...data.config].filter((r) => !r.analyzed).length;
  }, [data]);

  return (
    <div>
      <header className="mb-2 flex flex-wrap items-center gap-x-[8px] gap-y-[4px]">
        <Database className="h-4 w-4 text-muted-foreground" aria-hidden />
        <h1 className="text-[16px] font-[650] tracking-[-0.02em]">Schemas</h1>
        {data && (
          <span className="text-[12px] text-muted-foreground">
            {num(data.totals.entities)} entities · {num(data.totals.types)} populated types ·{" "}
            {num(data.totals.observations)} observations
          </span>
        )}
        {analyzing > 0 && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Badge variant="live" className="ml-auto text-[10.5px]">
                analyzing {analyzing}
              </Badge>
            </TooltipTrigger>
            <TooltipContent className="max-w-[340px]">
              {analyzing} type{analyzing === 1 ? "" : "s"} still being sampled in the background.
              Their drift columns read “—” until then, which means not yet measured — not clean.
            </TooltipContent>
          </Tooltip>
        )}
      </header>

      {/* The finding, stated before the tables — same posture as Workflows. */}
      <section className="mb-3 rounded-lg border border-[hsl(var(--bad)/0.35)] bg-[hsl(var(--bad)/0.10)] p-3">
        <div className="flex items-start gap-[10px]">
          <AlertTriangle
            className="mt-[2px] h-4 w-4 flex-none text-[hsl(var(--bad))]"
            aria-hidden
          />
          <div className="min-w-0">
            <h2 className="text-[14px] font-[650] tracking-[-0.01em]">
              The registry has drifted from the data.
            </h2>
            <p className="mt-[5px] text-[12.5px] leading-[1.45] text-muted-foreground">
              Schemas declare what an entity type should hold; nothing enforces it. The clearest
              case is <code className="font-mono text-[11px]">checkpoint_brief</code>, whose{" "}
              <code className="font-mono text-[11px]">status</code> declares four values while the
              dispatcher writes <code className="font-mono text-[11px]">open</code> and the MCP
              server filters on <code className="font-mono text-[11px]">awaiting_operator</code> —
              neither declared. Briefs written under those values can be neither listed nor
              resolved.
            </p>
            <p className="mt-[6px] text-[12.5px] leading-[1.45] text-muted-foreground">
              Every finding of that kind so far came from a one-off audit months apart. The tables
              below recompute them on each load, so drift stays visible instead of being
              rediscovered.
            </p>
          </div>
        </div>
      </section>

      {error && (
        <div className="my-4 rounded-lg border border-[hsl(var(--bad)/0.35)] bg-[hsl(var(--bad)/0.12)] px-3 py-[10px] text-[13px]">
          <strong>Cannot load schemas.</strong> {error}
        </div>
      )}

      {data && <ProvenanceNote sampleSize={data.sampleSize} types={data.totals.types} />}

      <CanonicalSection rows={data?.canonical ?? []} pending={pending} />
      <ConfigSection rows={data?.config ?? []} pending={pending} />
      {data && <TailSection buckets={data.buckets} />}
    </div>
  );
}
