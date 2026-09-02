/**
 * SKELETON PLACEHOLDERS
 * ---------------------
 * Shown on the FIRST load only. This app polls every 10 seconds, so swapping
 * real content for skeletons on each tick would strobe; the rule throughout is
 * "first load, no data yet" (see `showSkeleton` below), never "a request is in
 * flight". During a refresh the current data stays on screen untouched.
 *
 * Each placeholder mirrors the geometry of the real row or card it stands in
 * for — same paddings, same border radius, same two-line structure — so the
 * content that arrives does not shift the layout under the operator.
 */
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

/**
 * One task row. Height is matched to the real row deliberately: an 11px/13px
 * padded box with a title line and a meta line, so the list does not jump when
 * the tasks land.
 */
export function TaskRowSkeleton() {
  return (
    /* Geometry re-measured against the TABLE row that replaced the card: 4px
       vertical padding around a 17px badge line inside a bottom-bordered tr —
       ~26px, not the old 69px. A skeleton still built for the card would shift
       the table down by ~43px per row when the tasks land. */
    <tr className="border-b border-border/60">
      <td className="py-[4px] pr-2">
        <Skeleton className="h-[17px] w-[68px] rounded-[5px]" />
      </td>
      <td className="py-[4px] pr-2">
        <Skeleton className="h-[13px] w-[min(72%,340px)]" />
      </td>
      <td className="py-[4px] pr-2">
        <Skeleton className="h-[12px] w-[44px]" />
      </td>
      <td className="py-[4px] pr-2">
        <Skeleton className="h-[12px] w-[92px]" />
      </td>
      <td className="py-[4px]">
        <Skeleton className="ml-auto h-[12px] w-[42px]" />
      </td>
    </tr>
  );
}

/** A run of task rows, in the same table shell the real list uses. */
export function TaskListSkeleton({ rows = 8 }: { rows?: number }) {
  return (
    <table className="w-full border-collapse text-[12.5px]">
      <thead>
        <tr className="border-b text-[10px] uppercase tracking-[.06em] text-muted-foreground">
          <th className="w-[86px] py-[5px] pr-2 text-left font-[600]">Status</th>
          <th className="py-[5px] pr-2 text-left font-[600]">Title</th>
          <th className="w-[64px] py-[5px] pr-2 text-left font-[600]">Priority</th>
          <th className="w-[150px] py-[5px] pr-2 text-left font-[600]">Assigned to</th>
          <th className="w-[62px] py-[5px] text-right font-[600]">Updated</th>
        </tr>
      </thead>
      <tbody>
        {Array.from({ length: rows }, (_, i) => (
          <TaskRowSkeleton key={i} />
        ))}
      </tbody>
    </table>
  );
}

/** Filter chips, so the chip row does not pop in above the list. */
export function FilterChipsSkeleton({ count = 6 }: { count?: number }) {
  return (
    <div className="my-[10px] flex flex-wrap gap-[5px]">
      {Array.from({ length: count }, (_, i) => (
        <Skeleton key={i} className="h-[26px] w-[86px] rounded-full" />
      ))}
    </div>
  );
}

/** One agent ROW, matching the directory's table geometry. */
export function AgentCardSkeleton() {
  return (
    <tr className="border-b border-border/60">
      <td className="py-[4px] pr-2">
        <Skeleton className="h-[17px] w-[30px] rounded-[5px]" />
      </td>
      <td className="py-[4px] pr-2">
        <Skeleton className="h-[13px] w-[104px]" />
      </td>
      <td className="py-[4px] pr-2">
        <Skeleton className="h-[12px] w-[78px]" />
      </td>
      <td className="py-[4px] pr-2">
        <Skeleton className="h-[12px] w-[min(80%,360px)]" />
      </td>
      <td className="py-[4px]">
        <Skeleton className="h-[17px] w-[58px] rounded-[5px]" />
      </td>
    </tr>
  );
}

/** The directory: tier groups, each a heading and a table of rows. */
export function AgentDirectorySkeleton() {
  return (
    <>
      <FilterChipsSkeleton count={5} />
      {[3, 4].map((n, g) => (
        <section key={g} className="mt-[14px]">
          <div className="mb-[3px] flex flex-wrap items-baseline gap-[8px]">
            <Skeleton className="h-[13px] w-[108px]" />
            <Skeleton className="h-[12px] w-[210px]" />
          </div>
          <table className="w-full border-collapse text-[12.5px]">
            <thead>
              <tr className="border-b text-[10px] uppercase tracking-[.06em] text-muted-foreground">
                <th className="w-[44px] py-[4px] pr-2 text-left font-[600]">Tier</th>
                <th className="w-[150px] py-[4px] pr-2 text-left font-[600]">Name</th>
                <th className="w-[110px] py-[4px] pr-2 text-left font-[600]">Genus</th>
                <th className="py-[4px] pr-2 text-left font-[600]">Description</th>
                <th className="w-[84px] py-[4px] text-left font-[600]">Status</th>
              </tr>
            </thead>
            <tbody>
              {Array.from({ length: n }, (_, i) => (
                <AgentCardSkeleton key={i} />
              ))}
            </tbody>
          </table>
        </section>
      ))}
    </>
  );
}

/**
 * Agent detail. `prompt_markdown` runs to ~10k characters, so the placeholder
 * is deliberately long and paragraph-shaped — a couple of short bars would
 * promise a stub and then drop a document on the operator.
 */
export function AgentDetailSkeleton() {
  return (
    <article>
      <Skeleton className="mb-[14px] h-[29px] w-[104px] rounded-[7px]" />

      <div className="flex items-start justify-between gap-4">
        <div className="w-full">
          <Skeleton className="h-[24px] w-[190px]" />
          <Skeleton className="mt-[6px] h-[15px] w-[min(60%,380px)]" />
        </div>
        <Skeleton className="h-[17px] w-[58px] rounded-[5px]" />
      </div>

      {/* Facts strip */}
      <div className="my-[8px] flex flex-wrap gap-x-[18px] gap-y-[3px] rounded-[7px] border bg-card px-[10px] py-[6px]">
        {Array.from({ length: 5 }, (_, i) => (
          <div key={i} className="flex items-baseline gap-[5px]">
            <Skeleton className="h-[10px] w-[56px]" />
            <Skeleton className="h-[12px] w-[84px]" />
          </div>
        ))}
      </div>

      {/* Description + chip lists */}
      <div className="my-[18px]">
        <Skeleton className="mb-[6px] h-[11px] w-[72px]" />
        <Skeleton className="h-[14px] w-full" />
        <Skeleton className="mt-[6px] h-[14px] w-[82%]" />
      </div>
      <div className="my-[18px]">
        <Skeleton className="mb-[6px] h-[11px] w-[96px]" />
        <div className="flex flex-wrap gap-[6px]">
          {[70, 96, 58, 112, 84, 64].map((w, i) => (
            <Skeleton key={i} className="h-[23px] rounded-full" style={{ width: w }} />
          ))}
        </div>
      </div>

      {/* The long prompt body. */}
      <div className="my-[18px]">
        <Skeleton className="mb-[6px] h-[11px] w-[54px]" />
        <div className="rounded-[10px] border bg-card px-4 py-[14px]">
          <PromptBodySkeleton />
        </div>
      </div>
    </article>
  );
}

/**
 * A long-document placeholder: headings, paragraph runs of varying width, and
 * a code block — the shape `prompt_markdown` actually has.
 */
export function PromptBodySkeleton() {
  const paragraph = (widths: number[], key: string) => (
    <div key={key} className="mb-[14px] flex flex-col gap-[6px]">
      {widths.map((w, i) => (
        <Skeleton key={i} className="h-[11px]" style={{ width: `${w}%` }} />
      ))}
    </div>
  );

  return (
    <div className="py-1">
      <Skeleton className="mb-[10px] h-[15px] w-[42%]" />
      {paragraph([100, 97, 92, 68], "p1")}
      <Skeleton className="mb-[10px] mt-[18px] h-[13px] w-[34%]" />
      {paragraph([98, 100, 88], "p2")}
      {/* bullet run */}
      <div className="mb-[14px] ml-[22px] flex flex-col gap-[7px]">
        {[86, 74, 90, 63].map((w, i) => (
          <Skeleton key={i} className="h-[11px]" style={{ width: `${w}%` }} />
        ))}
      </div>
      <Skeleton className="mb-[10px] mt-[18px] h-[13px] w-[28%]" />
      {paragraph([100, 94, 99, 71], "p3")}
      {/* fenced code block */}
      <Skeleton className="mb-[14px] h-[76px] w-full rounded-[8px]" />
      {paragraph([96, 100, 82], "p4")}
    </div>
  );
}

/**
 * Question detail. Mirrors QuestionDetail's geometry: back button, a title row
 * with the reference badge, the facts strip, two prose blocks, and the amber
 * recommendation panel. A question description runs to ~2,700 characters, so
 * the prose placeholders are paragraph-shaped rather than a couple of bars —
 * a short stub followed by a long document is its own kind of layout shift.
 */
export function QuestionDetailSkeleton() {
  const paragraph = (widths: number[], key: string) => (
    <div key={key} className="mb-[14px] flex flex-col gap-[6px]">
      {widths.map((w, i) => (
        <Skeleton key={i} className="h-[11px]" style={{ width: `${w}%` }} />
      ))}
    </div>
  );

  return (
    <article>
      <Skeleton className="mb-[14px] h-[29px] w-[128px] rounded-[7px]" />

      <div className="flex items-start justify-between gap-4">
        <div className="w-full">
          <div className="flex items-center gap-[10px]">
            <Skeleton className="h-[21px] w-[34px] rounded-[5px]" />
            <Skeleton className="h-[24px] w-[min(58%,320px)]" />
          </div>
          <Skeleton className="mt-[8px] h-[15px] w-[168px]" />
        </div>
        <div className="flex flex-none gap-[6px]">
          <Skeleton className="h-[17px] w-[46px] rounded-[5px]" />
          <Skeleton className="h-[17px] w-[52px] rounded-[5px]" />
        </div>
      </div>

      {/* Facts strip — same six-column box as the real one. */}
      <div className="my-[8px] flex flex-wrap gap-x-[18px] gap-y-[3px] rounded-[7px] border bg-card px-[10px] py-[6px]">
        {Array.from({ length: 5 }, (_, i) => (
          <div key={i} className="flex items-baseline gap-[5px]">
            <Skeleton className="h-[10px] w-[62px]" />
            <Skeleton className="h-[12px] w-[92px]" />
          </div>
        ))}
      </div>

      {/* Context, then the long question body. */}
      <div className="my-[18px]">
        <Skeleton className="mb-[6px] h-[11px] w-[58px]" />
        {paragraph([100, 88], "ctx")}
      </div>
      <div className="my-[18px]">
        <Skeleton className="mb-[6px] h-[11px] w-[66px]" />
        {paragraph([100, 96, 91, 72], "q1")}
        {paragraph([98, 100, 94, 66], "q2")}
        {paragraph([96, 89], "q3")}
      </div>

      {/* Amber recommendation panel. */}
      <div className="my-[18px] rounded-[10px] border border-[hsl(var(--warn)/0.26)] bg-[hsl(var(--warn)/0.08)] px-[14px] py-3">
        <Skeleton className="mb-[8px] h-[11px] w-[120px]" />
        {paragraph([100, 93, 74], "rec")}
      </div>
    </article>
  );
}

/**
 * One session row in the list. Same 13px/11px padded two-line box as a task
 * row, plus a trailing claim-count chip.
 */
export function SessionRowSkeleton() {
  return (
    <tr className="border-b border-border/60">
      <td className="py-[4px] pr-2">
        <Skeleton className="h-[12px] w-[52px]" />
      </td>
      <td className="py-[4px] pr-2">
        <Skeleton className="h-[13px] w-[min(70%,320px)]" />
      </td>
      <td className="py-[4px] pr-2">
        <Skeleton className="h-[12px] w-[72px]" />
      </td>
      <td className="py-[4px] pr-2">
        <Skeleton className="h-[12px] w-[92px]" />
      </td>
      <td className="py-[4px]">
        <Skeleton className="ml-auto h-[12px] w-[38px]" />
      </td>
    </tr>
  );
}

/** The session list: coverage banner, then the rows table. */
export function SessionListSkeleton({ rows = 9 }: { rows?: number }) {
  return (
    <>
      {/* Coverage panel — a fixed-height block so the histogram does not shove
          the list down when it lands. Matches the tightened panel. */}
      <Skeleton className="mb-[10px] h-[104px] w-full rounded-[7px]" />
      <table className="w-full border-collapse text-[12.5px]">
        <thead>
          <tr className="border-b text-[10px] uppercase tracking-[.06em] text-muted-foreground">
            <th className="w-[62px] py-[4px] pr-2 text-left font-[600]">Ran</th>
            <th className="py-[4px] pr-2 text-left font-[600]">Title</th>
            <th className="w-[96px] py-[4px] pr-2 text-left font-[600]">Harness</th>
            <th className="w-[112px] py-[4px] pr-2 text-left font-[600]">Method</th>
            <th className="w-[76px] py-[4px] text-right font-[600]">Claims</th>
          </tr>
        </thead>
        <tbody>
          {Array.from({ length: rows }, (_, i) => (
            <SessionRowSkeleton key={i} />
          ))}
        </tbody>
      </table>
    </>
  );
}

/**
 * Session detail. Mirrors the real view: back link, title, the facts strip
 * (which now carries the plan too, hence five facts), topic chips, the clamped
 * summary, the `Needs you` strip, and the TASK TABLE.
 *
 * GEOMETRY FOLLOWS THE TABLE, not the card list the claims used to render as.
 * The real task row is `py-[3px]` on 13px/1.4 text — 26.9px measured — so the
 * placeholder rows use the same padding and a 13px bar rather than the 41px
 * bordered cards that preceded them. A skeleton taller than its content shoves
 * the whole page down when the data lands, which on the app's homepage is the
 * one flash this first-load-only rule exists to prevent.
 */
export function SessionDetailSkeleton() {
  return (
    <article>
      <Skeleton className="mb-[8px] h-[29px] w-[132px] rounded-[7px]" />

      <div className="flex items-start justify-between gap-4">
        <div className="w-full">
          <Skeleton className="h-[24px] w-[min(56%,340px)]" />
          <Skeleton className="mt-[6px] h-[15px] w-[190px]" />
        </div>
        <Skeleton className="h-[17px] w-[64px] rounded-[5px]" />
      </div>

      {/* Five facts: started, last updated, harness, repository, status/plan. */}
      <div className="my-[8px] flex flex-wrap gap-x-[18px] gap-y-[3px] rounded-[7px] border bg-card px-[10px] py-[6px]">
        {Array.from({ length: 5 }, (_, i) => (
          <div key={i} className="flex items-baseline gap-[5px]">
            <Skeleton className="h-[10px] w-[58px]" />
            <Skeleton className="h-[12px] w-[92px]" />
          </div>
        ))}
      </div>

      {/* Topic chips. */}
      <div className="my-[8px] flex flex-wrap gap-[4px]">
        {[168, 132, 186, 154].map((w) => (
          <Skeleton key={w} className="h-[17px] rounded-[5px]" style={{ width: w }} />
        ))}
      </div>

      {/* Scope summary, clamped to the same 86px the real one is. */}
      <div className="my-[10px]">
        <Skeleton className="mb-[3px] h-[11px] w-[96px]" />
        <Skeleton className="h-[14px] w-full" />
        <Skeleton className="mt-[6px] h-[14px] w-[94%]" />
        <Skeleton className="mt-[6px] h-[14px] w-[76%]" />
      </div>

      {/* `Needs you` — one bordered strip, not a stack. */}
      <Skeleton className="my-[10px] h-[31px] w-full rounded-[7px]" />

      {/* The task table: heading row, then rows at the real 26.9px pitch. */}
      <div className="my-[10px]">
        <Skeleton className="mb-[3px] h-[11px] w-[220px]" />
        <table className="w-full border-collapse text-[13px] leading-[1.4]">
          <thead>
            <tr className="border-b">
              <th className="w-[104px] py-[3px] pr-2 text-left">
                <Skeleton className="h-[10px] w-[42px]" />
              </th>
              <th className="py-[3px] pr-2 text-left">
                <Skeleton className="h-[10px] w-[34px]" />
              </th>
              <th className="w-[150px] py-[3px]">
                <Skeleton className="ml-auto h-[10px] w-[40px]" />
              </th>
            </tr>
          </thead>
          <tbody>
            {Array.from({ length: 12 }, (_, i) => (
              <tr key={i} className="border-b border-border/60">
                <td className="py-[3px] pr-2">
                  <Skeleton className="h-[13px] w-[58px] rounded-[5px]" />
                </td>
                <td className="py-[3px] pr-2">
                  <Skeleton className="h-[13px] w-[min(76%,420px)]" />
                </td>
                <td className="py-[3px]">
                  <Skeleton className="ml-auto h-[13px] w-[74px]" />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </article>
  );
}

/**
 * One entity detail — the shared body used by BOTH the full page at
 * `#/entities/<id>` and the slide-over sheet, so both get the same placeholder.
 *
 * Geometry follows `EntityDetail`'s real header: a type/status badge row, a
 * 19px title, the id line, then the four-fact strip and two prose blocks.
 */
export function EntityDetailSkeleton() {
  return (
    <article>
      <div className="flex flex-wrap items-center gap-[8px]">
        <Skeleton className="h-[17px] w-[86px] rounded-[5px]" />
        <Skeleton className="h-[17px] w-[64px] rounded-[5px]" />
      </div>
      <Skeleton className="mt-[8px] h-[25px] w-[min(62%,380px)]" />
      <Skeleton className="mt-[6px] h-[15px] w-[210px]" />

      <div className="my-[8px] flex flex-wrap gap-x-[18px] gap-y-[3px] rounded-[7px] border bg-card px-[10px] py-[6px]">
        {Array.from({ length: 4 }, (_, i) => (
          <div key={i} className="flex items-baseline gap-[5px]">
            <Skeleton className="h-[10px] w-[58px]" />
            <Skeleton className="h-[12px] w-[92px]" />
          </div>
        ))}
      </div>

      {/* Two prose blocks — a description and a body — in the bordered card
          geometry `Prose` uses, so the text lands without shifting anything. */}
      {Array.from({ length: 2 }, (_, i) => (
        <div key={i} className="my-[18px]">
          <Skeleton className="mb-[6px] h-[11px] w-[78px]" />
          <div className="rounded-[10px] border bg-card px-[14px] py-3">
            <Skeleton className="h-[14px] w-full" />
            <Skeleton className="mt-[7px] h-[14px] w-[93%]" />
            <Skeleton className="mt-[7px] h-[14px] w-[71%]" />
          </div>
        </div>
      ))}

      <div className="my-[18px]">
        <Skeleton className="mb-[10px] h-[11px] w-[92px]" />
        <ul className="m-0 flex list-none flex-col gap-[6px] p-0">
          {Array.from({ length: 4 }, (_, i) => (
            <li key={i} className="rounded-[10px] border bg-card px-[13px] py-[10px]">
              <div className="flex items-center gap-[10px]">
                <Skeleton className="h-[17px] w-[74px] rounded-[5px]" />
                <Skeleton className="h-[14px] w-[min(46%,280px)]" />
              </div>
            </li>
          ))}
        </ul>
      </div>
    </article>
  );
}

/** One question card in the sidebar. */
export function QuestionCardSkeleton() {
  return (
    <li>
      <Card className="px-[13px] py-3">
        <div className="flex items-baseline gap-[9px]">
          <Skeleton className="h-[17px] w-[26px] rounded-[5px]" />
          <Skeleton className="h-[7px] w-[7px] rounded-full" />
          <Skeleton className="h-[15px] w-[min(58%,168px)]" />
        </div>
        <Skeleton className="mt-[9px] h-[13px] w-full" />
        <Skeleton className="mt-[5px] h-[13px] w-[74%]" />
        <div className="mt-[9px] flex items-center gap-3">
          <Skeleton className="h-[13px] w-[44px]" />
          <Skeleton className="h-[13px] w-[96px]" />
          <Skeleton className="ml-auto h-[13px] w-[40px]" />
        </div>
      </Card>
    </li>
  );
}

/** The sidebar's question list. */
export function QuestionsSkeleton({ cards = 3 }: { cards?: number }) {
  return (
    <ul className="m-0 flex list-none flex-col gap-2 p-0">
      {Array.from({ length: cards }, (_, i) => (
        <QuestionCardSkeleton key={i} />
      ))}
    </ul>
  );
}

/**
 * One workflow card: header line, description, and a run of gate rows. The
 * banners above the list are NOT skeletonized — they are static prose that is
 * available immediately, and the finding they carry is the reason the page
 * exists, so it should be readable while the gates are still loading.
 */
export function WorkflowCardSkeleton() {
  return (
    <div className="rounded-lg border bg-card p-4">
      <div className="flex items-center gap-[8px]">
        <Skeleton className="h-[15px] w-[180px]" />
        <Skeleton className="h-[17px] w-[48px] rounded-[5px]" />
        <Skeleton className="ml-auto h-[13px] w-[96px]" />
      </div>
      <Skeleton className="mt-[10px] h-[13px] w-[min(88%,560px)]" />
      <Skeleton className="mt-[5px] h-[13px] w-[min(62%,400px)]" />
      <div className="mt-[14px] flex flex-col gap-[9px]">
        {Array.from({ length: 5 }, (_, i) => (
          <div key={i} className="flex items-center gap-[8px]">
            <Skeleton className="h-[12px] w-[20px]" />
            <Skeleton className="h-[12px] w-[76px]" />
            <Skeleton className="h-[12px] w-[92px]" />
          </div>
        ))}
      </div>
    </div>
  );
}

export function WorkflowListSkeleton({ cards = 4 }: { cards?: number }) {
  return (
    <div className="flex flex-col gap-4">
      {Array.from({ length: cards }, (_, i) => (
        <WorkflowCardSkeleton key={i} />
      ))}
    </div>
  );
}

/**
 * One SCHEMAS row. Geometry matched to the real row: 4px vertical padding
 * around a 13px code line inside a bottom-bordered tr — ~27px, the same as the
 * task and agent tables. A skeleton built to a different height would shift the
 * whole table when the counts land.
 *
 * Seven columns, because the canonical table has seven; the config table's five
 * are a subset of the same widths, so one placeholder serves both rather than
 * two nearly-identical ones drifting apart.
 */
export function SchemaRowSkeleton() {
  return (
    <tr className="border-b border-border/60">
      <td className="py-[4px] pr-2">
        <Skeleton className="h-[13px] w-[min(60%,150px)]" />
      </td>
      <td className="py-[4px] pr-2">
        <Skeleton className="ml-auto h-[12px] w-[46px]" />
      </td>
      <td className="py-[4px] pr-2">
        <Skeleton className="h-[12px] w-[48px]" />
      </td>
      <td className="py-[4px] pr-2">
        <Skeleton className="ml-auto h-[12px] w-[28px]" />
      </td>
      <td className="py-[4px] pr-2">
        <Skeleton className="h-[17px] w-[112px] rounded-[5px]" />
      </td>
      <td className="py-[4px] pr-2">
        <Skeleton className="ml-auto h-[12px] w-[34px]" />
      </td>
      <td className="py-[4px]">
        <Skeleton className="ml-auto h-[12px] w-[24px]" />
      </td>
    </tr>
  );
}

/**
 * One LIFECYCLE stage card. Only the live COUNT is a skeleton — the stage
 * definitions are code-sourced constants that render immediately, so
 * placeholdering them would fake a load that never happens. What the operator
 * actually waits for is the count, and that is the only thing standing in.
 */
export function StageCountSkeleton() {
  return <Skeleton className="h-[19px] w-[52px] rounded-[5px]" />;
}

/** A run of schema rows, in the same table shell the real sections use. */
export function SchemaTableSkeleton({ rows = 8 }: { rows?: number }) {
  return (
    <table className="w-full border-collapse text-[12.5px]">
      <thead>
        <tr className="border-b text-[10px] uppercase tracking-[.06em] text-muted-foreground">
          <th className="py-[5px] pr-2 text-left font-[600]">Type</th>
          <th className="w-[74px] py-[5px] pr-2 text-right font-[600]">Entities</th>
          <th className="w-[92px] py-[5px] pr-2 text-left font-[600]">Fields used</th>
          <th className="w-[58px] py-[5px] pr-2 text-right font-[600]">Sample</th>
          <th className="w-[168px] py-[5px] pr-2 text-left font-[600]">Value drift</th>
          <th className="w-[86px] py-[5px] pr-2 text-right font-[600]">Readers</th>
          <th className="w-[64px] py-[5px] text-right font-[600]">Writers</th>
        </tr>
      </thead>
      <tbody>
        {Array.from({ length: rows }, (_, i) => (
          <SchemaRowSkeleton key={i} />
        ))}
      </tbody>
    </table>
  );
}
