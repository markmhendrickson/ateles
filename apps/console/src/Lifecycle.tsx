/**
 * LIFECYCLE
 * ---------
 * The generic state machine every task moves through, whatever the task is.
 *
 * WHY IT IS ITS OWN TAB. This used to be half of a box on the Workflows page
 * whose only job was to explain that the lifecycle and the gate sequences are
 * different things. A box that exists to say "these two are not the same" is a
 * symptom of two subjects sharing a surface, so they were separated and the box
 * removed; Workflows now carries a one-line pointer here instead.
 *
 * THE TWO SOURCES, KEPT VISIBLY APART. The stage DEFINITIONS are transcribed
 * from a Python state machine, because that is where they live — Neotoma types
 * `task.status` as a bare string with no enum, and no entity defines the
 * vocabulary (see `lifecycleData.ts`). The COUNTS are live Neotoma totals. The
 * page says which is which rather than letting the reader assume both came from
 * the graph.
 *
 * TWO KINDS OF STATE, NOT ELEVEN STEPS. This page previously rendered all
 * eleven states as one flat column of identical cards, which presented them as
 * a single sequence. They are not one sequence, and the flat list made a
 * question askable that should not be: does `done` come before or after
 * `awaiting_approval`? It has no answer — the two are not on the same axis.
 *
 * So the page is partitioned by `Stage.kind`:
 *
 *   THE PATH   pending → routed → executing → (verified) → done, rendered as an
 *              ordered, numbered progression, because position is what it means.
 *   THE HOLDS  awaiting_approval, awaiting_input, blocked, failed — rendered
 *              BESIDE the path, each showing which path states enter it and how
 *              a task returns. A hold is a mode, not a step.
 *   THE ENDINGS  done, declined, superseded — the three members of `TERMINAL`,
 *              grouped so that "terminal" reads as a property rather than as
 *              one state called done.
 *
 * READ-ONLY, like every view here: one GET to /api/lifecycle, no mutation.
 */
import { useEffect, useMemo, useState } from "react";
import {
  type LifecyclePayload,
  type Stage,
  type StageCount,
  ACTOR_LABELS,
  LIFECYCLE_SOURCE,
  OWNERSHIP_NOTE,
  PATH_ORDER,
  REVIEW_NOTE,
  STAGES,
  VALIDATION_NOTE,
  byKind,
  countsByStatus,
  stageCount,
  sumMeasured,
} from "./lifecycleData";
import { StageCountSkeleton } from "@/components/Skeletons";
import { showSkeleton } from "@/lib/loading";
import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import {
  ArrowRight,
  CircleDot,
  CornerDownLeft,
  Flag,
  GitPullRequest,
  Info,
  PauseCircle,
  ShieldAlert,
} from "lucide-react";

/**
 * Where the definition came from, stated up front.
 *
 * NOT a caveat buried at the bottom. The app's standing rule is that data comes
 * from Neotoma, and this page partly departs from it — so the departure is the
 * first thing on the page, with the reason, rather than something a careful
 * reader might discover.
 */
function SourceNote({ totalTasks }: { totalTasks: number | null }) {
  return (
    <section className="mb-3 rounded-lg border bg-card p-3">
      <div className="flex items-start gap-[10px]">
        <Info className="mt-[2px] h-4 w-4 flex-none text-muted-foreground" aria-hidden />
        <div className="min-w-0 text-[12.5px] leading-[1.45] text-muted-foreground">
          <h2 className="text-[14px] font-[650] tracking-[-0.01em] text-foreground">
            Definitions from code; counts from Neotoma.
          </h2>
          <p className="mt-[6px]">
            The stage names, transitions, and terminal/active sets below are transcribed
            from{" "}
            <code className="font-mono text-[11px] text-foreground">
              {LIFECYCLE_SOURCE.file}
            </code>{" "}
            (<code className="font-mono text-[11px]">{LIFECYCLE_SOURCE.symbols}</code>) —{" "}
            <strong className="text-foreground">not from the graph</strong>. Neotoma types{" "}
            <code className="font-mono text-[11px]">task.status</code> as a bare string with
            no enum, and no entity defines this vocabulary: the{" "}
            <code className="font-mono text-[11px]">lifecycle_stage</code> type exists but
            holds Neotoma's own user-onboarding stages, a different subject.
          </p>
          <p className="mt-[8px]">
            The <strong className="text-foreground">counts are live</strong>, one filtered
            query per status against{" "}
            {totalTasks === null ? (
              "the task table"
            ) : (
              <>
                <span className="tabular-nums text-foreground">
                  {totalTasks.toLocaleString()}
                </span>{" "}
                task entities
              </>
            )}
            . Verified against the named file on {LIFECYCLE_SOURCE.verifiedOn}.
          </p>
        </div>
      </div>
    </section>
  );
}

/**
 * THE COUNT, RENDERED HONESTLY, IN ONE PLACE.
 *
 * `null` is UNMEASURED and must never render as "0". Both facts are real and
 * they are different: a state holding no tasks is a measurement, a state whose
 * query failed is the absence of one. Every count on this page goes through
 * here so the distinction cannot be lost at one call site while holding at the
 * others — which is exactly how the Schemas tab shipped this bug three times.
 */
function Count({ count, pending }: { count: number | null; pending: boolean }) {
  if (pending) return <StageCountSkeleton />;
  if (count === null) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="text-[11px] italic text-muted-foreground">not measured</span>
        </TooltipTrigger>
        <TooltipContent>
          This count could not be read — the query failed or timed out. It is not
          zero; zero would be a measurement.
        </TooltipContent>
      </Tooltip>
    );
  }
  return (
    <span className="flex items-baseline gap-[6px]">
      <span
        className={cn(
          "text-[15px] font-[650] tabular-nums",
          count === 0 && "text-muted-foreground",
        )}
      >
        {count.toLocaleString()}
      </span>
      <span className="text-[11px] text-muted-foreground">
        {count === 1 ? "task" : "tasks"}
      </span>
    </span>
  );
}

/**
 * "ZERO TASKS" vs "NOTHING CAN EVER WRITE THIS".
 *
 * A count of 0 is ambiguous on its face and the two readings are opposites: an
 * idle step, or a state nothing reaches. `neverWritten` resolves it from the
 * code, so the badge must appear wherever a count does — otherwise the zero
 * argues for a fast pipeline while actually reporting dead vocabulary.
 *
 * `writtenOutOfBandOnly` is the weaker sibling: the state IS reached, just
 * never by the dispatch loop that claims to own it.
 */
function WriterBadge({ stage }: { stage: Stage }) {
  if (stage.neverWritten) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <Badge variant="bad" className="text-[10px]" caps>
            never written
          </Badge>
        </TooltipTrigger>
        <TooltipContent>
          Declared in <code className="font-mono text-[11px]">TaskStatus</code> and present
          in <code className="font-mono text-[11px]">_TRANSITIONS</code>, but nothing writes
          it — not one{" "}
          <code className="font-mono text-[11px]">set_task_status</code> call site passes it.
          Its count of zero means the state is unreachable, NOT that the pipeline is idle
          here.
        </TooltipContent>
      </Tooltip>
    );
  }
  if (stage.writtenOutOfBandOnly) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <Badge variant="warn" className="text-[10px]" caps>
            no daemon writer
          </Badge>
        </TooltipTrigger>
        <TooltipContent>
          Reached in practice, but by sessions and agents out of band — no daemon in the
          dispatch loop writes it, so the state machine does not drive tasks here.
        </TooltipContent>
      </Tooltip>
    );
  }
  return null;
}

/**
 * IS THERE A REVIEW STAGE? Answered explicitly, because the flat list made the
 * question askable and the intuitive answer is wrong.
 *
 * `awaiting_approval` looks like review and is not: it gates a PLAN before the
 * work runs, on blast radius and confidence, with nobody reading any code. Code
 * review is a different gate on a different entity type in a different state
 * machine. The absence is stated as an absence rather than papered over.
 */
function ReviewNote() {
  return (
    <section className="mb-3 rounded-lg border bg-card p-3">
      <div className="flex items-start gap-[10px]">
        <GitPullRequest
          className="mt-[2px] h-4 w-4 flex-none text-muted-foreground"
          aria-hidden
        />
        <div className="min-w-0 text-[12.5px] leading-[1.45] text-muted-foreground">
          <h2 className="text-[14px] font-[650] tracking-[-0.01em] text-foreground">
            There is no review stage. <code className="font-mono text-[12px]">awaiting_approval</code>{" "}
            is not one.
          </h2>
          <dl className="mt-[7px] space-y-[6px]">
            <div>
              <dt className="inline font-mono text-[11.5px] font-[600] text-foreground">
                awaiting_approval
              </dt>
              <dd className="inline"> — {REVIEW_NOTE.approvalIs}</dd>
            </div>
            <div>
              <dt className="inline text-[12.5px] font-[600] text-foreground">Code review</dt>
              <dd className="inline"> — {REVIEW_NOTE.codeReviewIs}</dd>
            </div>
          </dl>
          <p className="mt-[8px]">
            <strong className="text-foreground">{REVIEW_NOTE.absence}</strong>
          </p>
          <p className="mt-[7px]">
            Making merge and deploy visible to the task spine is open as{" "}
            <a
              href={REVIEW_NOTE.issue.url}
              target="_blank"
              rel="noreferrer"
              className="text-live underline decoration-dotted underline-offset-2"
            >
              ateles#{REVIEW_NOTE.issue.number}
            </a>{" "}
            — {REVIEW_NOTE.issue.title}.
          </p>
        </div>
      </div>
    </section>
  );
}

/**
 * THE VALIDATION IS ADVISORY. Surfaced on the page because it bears directly on
 * how far the counts below can be trusted: they record what was written, not
 * what was checked.
 */
function ValidationNote({
  inVocabulary,
  offVocabulary,
  totalTasks,
}: {
  inVocabulary: number | null;
  /** Tasks whose status the graph does not define — the ones validation skips. */
  offVocabulary: number | null;
  totalTasks: number | null;
}) {
  const share =
    inVocabulary !== null && totalTasks !== null && totalTasks > 0
      ? (inVocabulary / totalTasks) * 100
      : null;

  return (
    <section className="mt-3 rounded-lg border border-[hsl(var(--warn)/0.35)] bg-[hsl(var(--warn)/0.10)] p-3">
      <div className="flex items-start gap-[10px]">
        <ShieldAlert
          className="mt-[2px] h-4 w-4 flex-none text-[hsl(var(--warn))]"
          aria-hidden
        />
        <div className="min-w-0 text-[12.5px] leading-[1.45] text-muted-foreground">
          <h2 className="text-[14px] font-[650] tracking-[-0.01em] text-foreground">
            The transition graph fails open, so most transitions are never validated.
          </h2>
          <p className="mt-[6px]">
            <code className="font-mono text-[11px]">can_transition()</code>{" "}
            {VALIDATION_NOTE.behaviour.replace("can_transition() ", "")} {VALIDATION_NOTE.rationale}
          </p>
          <p className="mt-[7px]">
            {share !== null ? (
              <>
                Only{" "}
                <span className="tabular-nums text-foreground">{share.toFixed(1)}%</span> of
                tasks carry one of the eleven statuses below, so for the other{" "}
                <span className="tabular-nums text-foreground">
                  {(100 - share).toFixed(1)}%
                </span>{" "}
                every transition passes unchecked — including ones the graph forbids.
                {offVocabulary !== null && (
                  <>
                    {" "}
                    That is{" "}
                    <span className="tabular-nums text-foreground">
                      {offVocabulary.toLocaleString()}
                    </span>{" "}
                    tasks carrying a spelling the state machine does not define —{" "}
                    <code className="font-mono text-[11px]">open</code>,{" "}
                    <code className="font-mono text-[11px]">todo</code>,{" "}
                    <code className="font-mono text-[11px]">completed</code>,{" "}
                    <code className="font-mono text-[11px]">in_progress</code> and others,
                    which the schema accepts because{" "}
                    <code className="font-mono text-[11px]">status</code> is an
                    unconstrained string.
                  </>
                )}
              </>
            ) : (
              VALIDATION_NOTE.consequence
            )}{" "}
            <strong className="text-foreground">
              The counts are what is stored, not what was validated:
            </strong>{" "}
            a task reading <code className="font-mono text-[11px]">done</code> did not
            necessarily pass through the path to get there.
          </p>
        </div>
      </div>
    </section>
  );
}

/**
 * THE PATH, AS A PATH.
 *
 * Numbered and ordered, because position is the entire meaning of a path state —
 * this is the one axis on the page where "before" and "after" are real. The
 * connector between cards carries the arrow; `verified` carries the fact that
 * the path may skip it, since a required-looking optional step is what made
 * `executing → done` invisible.
 */
function PathRail({
  counts,
  pending,
}: {
  counts: Map<string, number>;
  pending: boolean;
}) {
  const path = byKind("path");

  return (
    <section className="mb-3">
      <header className="mb-[7px] flex flex-wrap items-baseline gap-x-[8px] gap-y-[3px]">
        <h2 className="text-[14px] font-[650] tracking-[-0.01em]">The path</h2>
        <span className="text-[12px] text-muted-foreground">
          Ordered. A task advances along it — this is the only axis where “before” and
          “after” mean anything.
        </span>
      </header>

      <ol className="space-y-[6px]">
        {path.map((stage, i) => (
          <li key={stage.key}>
            <article
              className={cn(
                "rounded-lg border bg-card p-3",
                stage.optional && "border-dashed",
              )}
            >
              <header className="flex flex-wrap items-baseline gap-x-[8px] gap-y-[4px]">
                <span className="flex h-[18px] w-[18px] flex-none items-center justify-center rounded-full bg-muted-foreground/[.18] text-[10px] font-[650] tabular-nums text-muted-foreground">
                  {i + 1}
                </span>
                <h3 className="text-[14px] font-[650] tracking-[-0.01em]">{stage.label}</h3>
                <code className="font-mono text-[11px] text-muted-foreground">
                  {stage.key}
                </code>

                {stage.optional && (
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Badge variant="warn" className="text-[10px]" caps>
                        optional
                      </Badge>
                    </TooltipTrigger>
                    <TooltipContent>
                      <code className="font-mono text-[11px]">executing</code> transitions
                      to <code className="font-mono text-[11px]">done</code> directly. The
                      path may skip this state entirely.
                    </TooltipContent>
                  </Tooltip>
                )}
                {stage.terminal && (
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Badge variant="ok" className="text-[10px]" caps>
                        terminal
                      </Badge>
                    </TooltipTrigger>
                    <TooltipContent>
                      In <code className="font-mono text-[11px]">TERMINAL</code> — one of
                      three endings, alongside <code className="font-mono text-[11px]">declined</code>{" "}
                      and <code className="font-mono text-[11px]">superseded</code>.
                    </TooltipContent>
                  </Tooltip>
                )}
                <WriterBadge stage={stage} />

                <span className="ml-auto">
                  <Count count={stageCount(counts, stage.key)} pending={pending} />
                </span>
              </header>

              <p className="mt-[5px] text-[12px] leading-[1.4] text-muted-foreground">
                {stage.meaning}
              </p>

              <dl className="mt-[8px] grid gap-x-[14px] gap-y-[6px] min-[720px]:grid-cols-2">
                <div>
                  <dt className="text-[10px] font-[650] uppercase tracking-[0.04em] text-muted-foreground">
                    Enters when
                  </dt>
                  <dd className="mt-[2px] text-[12px] leading-[1.45]">
                    {stage.entry}
                    <span className="mt-[3px] block text-[11px] text-muted-foreground">
                      Acted by:{" "}
                      <strong className="text-foreground">{ACTOR_LABELS[stage.entryBy]}</strong>
                    </span>
                  </dd>
                </div>
                <div>
                  <dt className="text-[10px] font-[650] uppercase tracking-[0.04em] text-muted-foreground">
                    Leaves when
                  </dt>
                  <dd className="mt-[2px] text-[12px] leading-[1.45]">
                    {stage.exit}
                    <span className="mt-[3px] block text-[11px] text-muted-foreground">
                      Acted by:{" "}
                      <strong className="text-foreground">{ACTOR_LABELS[stage.exitBy]}</strong>
                    </span>
                  </dd>
                </div>
              </dl>

              {/* Where the path can leave for a hold or an ending. Named as
                  departures rather than as "next", because a hold is not the
                  next step — it is off to the side. */}
              {stage.next.filter((n) => !PATH_ORDER.includes(n as never)).length > 0 && (
                <div className="mt-[8px] flex flex-wrap items-center gap-[6px] border-t pt-[7px]">
                  <span className="text-[10px] font-[650] uppercase tracking-[0.04em] text-muted-foreground">
                    Can leave the path for
                  </span>
                  {stage.next
                    .filter((n) => !PATH_ORDER.includes(n as never))
                    .map((n) => (
                      <code key={n} className="font-mono text-[11px] text-[hsl(var(--warn))]">
                        {n}
                      </code>
                    ))}
                </div>
              )}
            </article>

            {/* The connector: the arrow lives BETWEEN cards, so the ordering is
                carried by the layout rather than only by the numbers. */}
            {i < path.length - 1 && (
              <div className="flex items-center gap-[6px] py-[3px] pl-[9px]">
                <ArrowRight
                  className="h-[12px] w-[12px] rotate-90 text-muted-foreground"
                  aria-hidden
                />
                <span className="text-[11px] text-muted-foreground">
                  {path[i + 1].optional
                    ? "then, or skip straight to done"
                    : `then ${path[i + 1].key}`}
                </span>
              </div>
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}

/**
 * THE HOLDS, RENDERED BESIDE THE PATH.
 *
 * A hold is a MODE, entered from wherever the task happens to be and (for the
 * recoverable ones) returned from to the same progression. So each card leads
 * with `enteredFrom` — the path states `_TRANSITIONS` shows entering it — and
 * with how the task gets back out. No numbers, no order, and deliberately a
 * different card treatment from the path, so the two never read as one list.
 */
function HoldsGrid({
  counts,
  pending,
}: {
  counts: Map<string, number>;
  pending: boolean;
}) {
  const holds = byKind("hold");

  return (
    <section className="mb-3">
      <header className="mb-[7px] flex flex-wrap items-baseline gap-x-[8px] gap-y-[3px]">
        <h2 className="text-[14px] font-[650] tracking-[-0.01em]">The holds</h2>
        <span className="text-[12px] text-muted-foreground">
          Unordered. Each is a mode a task enters <em>from</em> the path and returns to it —
          not a position further along it.
        </span>
      </header>

      <div className="grid gap-2 min-[880px]:grid-cols-2">
        {holds.map((stage) => (
          <article
            key={stage.key}
            className="rounded-lg border border-[hsl(var(--warn)/0.35)] bg-[hsl(var(--warn)/0.06)] p-3"
          >
            <header className="flex flex-wrap items-baseline gap-x-[8px] gap-y-[4px]">
              <PauseCircle
                className="h-[13px] w-[13px] flex-none translate-y-[2px] text-[hsl(var(--warn))]"
                aria-hidden
              />
              <h3 className="text-[14px] font-[650] tracking-[-0.01em]">{stage.label}</h3>
              <code className="font-mono text-[11px] text-muted-foreground">{stage.key}</code>
              {stage.active && (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Badge variant="muted" className="text-[10px]">
                      dispatchable
                    </Badge>
                  </TooltipTrigger>
                  <TooltipContent>
                    In <code className="font-mono text-[11px]">ACTIVE</code> — work may still
                    be dispatched from here. No hold is terminal.
                  </TooltipContent>
                </Tooltip>
              )}
              <WriterBadge stage={stage} />
              <span className="ml-auto">
                <Count count={stageCount(counts, stage.key)} pending={pending} />
              </span>
            </header>

            <p className="mt-[5px] text-[12px] leading-[1.4] text-muted-foreground">
              {stage.meaning}
            </p>

            {/* WHICH PATH STATES ENTER IT — the fact a flat list cannot show. */}
            <div className="mt-[8px] border-t border-[hsl(var(--warn)/0.25)] pt-[7px]">
              <span className="text-[10px] font-[650] uppercase tracking-[0.04em] text-muted-foreground">
                Entered from
              </span>
              <div className="mt-[3px] flex flex-wrap items-center gap-[5px]">
                {stage.enteredFrom.map((n) => (
                  <code
                    key={n}
                    className={cn(
                      "font-mono text-[11px]",
                      PATH_ORDER.includes(n as never)
                        ? "text-foreground"
                        : "text-muted-foreground",
                    )}
                  >
                    {n}
                  </code>
                ))}
              </div>
              <p className="mt-[4px] text-[11px] leading-[1.4] text-muted-foreground">
                Bold names are path states; the rest are other holds. A task can enter this
                mode from any of them.
              </p>
            </div>

            <div className="mt-[7px] flex items-start gap-[6px] border-t border-[hsl(var(--warn)/0.25)] pt-[7px]">
              <CornerDownLeft
                className="mt-[2px] h-[12px] w-[12px] flex-none text-muted-foreground"
                aria-hidden
              />
              <div className="min-w-0">
                <span className="text-[10px] font-[650] uppercase tracking-[0.04em] text-muted-foreground">
                  Leaves by
                </span>
                <p className="mt-[2px] text-[12px] leading-[1.45]">
                  {stage.exit}
                  <span className="mt-[3px] block text-[11px] text-muted-foreground">
                    Acted by:{" "}
                    <strong className="text-foreground">{ACTOR_LABELS[stage.exitBy]}</strong>
                    {stage.next.includes("routed") && (
                      <>
                        {" "}
                        · rejoins the path at{" "}
                        <code className="font-mono text-[11px]">routed</code>
                      </>
                    )}
                  </span>
                </p>
              </div>
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}

/**
 * THE THREE ENDINGS.
 *
 * `TERMINAL` has three members and the old page showed only `done` as an
 * obvious ending, with `declined` and `superseded` sitting in the same flat
 * column as the holds — which is where "is done before or after awaiting
 * approval?" comes from. Grouping them states the property directly: these
 * three are where a task stops, and nothing returns from any of them.
 */
function EndingsGroup({
  counts,
  pending,
}: {
  counts: Map<string, number>;
  pending: boolean;
}) {
  const endings = STAGES.filter((s) => s.terminal);

  return (
    <section className="mb-3">
      <header className="mb-[7px] flex flex-wrap items-baseline gap-x-[8px] gap-y-[3px]">
        <h2 className="text-[14px] font-[650] tracking-[-0.01em]">The endings</h2>
        <span className="text-[12px] text-muted-foreground">
          All three members of <code className="font-mono text-[11.5px]">TERMINAL</code>. A
          task stops here; no automatic transition leaves any of them.
        </span>
      </header>

      <div className="grid gap-2 min-[880px]:grid-cols-3">
        {endings.map((stage) => (
          <article key={stage.key} className="rounded-lg border bg-card p-3">
            <header className="flex flex-wrap items-baseline gap-x-[7px] gap-y-[4px]">
              <Flag
                className={cn(
                  "h-[13px] w-[13px] flex-none translate-y-[2px]",
                  stage.key === "done" ? "text-[hsl(var(--ok))]" : "text-muted-foreground",
                )}
                aria-hidden
              />
              <h3 className="text-[14px] font-[650] tracking-[-0.01em]">{stage.label}</h3>
              <code className="font-mono text-[11px] text-muted-foreground">{stage.key}</code>
              <WriterBadge stage={stage} />
              <span className="ml-auto">
                <Count count={stageCount(counts, stage.key)} pending={pending} />
              </span>
            </header>
            <p className="mt-[5px] text-[12px] leading-[1.4] text-muted-foreground">
              {stage.meaning}
            </p>
            <p className="mt-[7px] border-t pt-[6px] text-[11.5px] leading-[1.4] text-muted-foreground">
              <strong className="text-foreground">
                {stage.kind === "path" ? "End of the path." : "An exit, not a hold."}
              </strong>{" "}
              {stage.kind === "path"
                ? "Reached from executing, or from verified when it ran."
                : "Reached from a hold or from early on the path. Nothing returns from it."}
            </p>
          </article>
        ))}
      </div>
    </section>
  );
}

/**
 * OWNER vs ASSIGNEE. A distinct section because the two fields are routinely
 * conflated and the difference decides whether a task can move at all.
 */
function OwnershipNote() {
  return (
    <section className="mt-3 rounded-lg border bg-card p-3">
      <h2 className="text-[14px] font-[650] tracking-[-0.01em]">
        Who owns a task, and who acts on it
      </h2>
      <dl className="mt-[7px] space-y-[7px] text-[12.5px] leading-[1.45] text-muted-foreground">
        <div>
          <dt className="inline font-mono text-[11.5px] font-[600] text-foreground">
            assigned_to
          </dt>
          <dd className="inline"> — {OWNERSHIP_NOTE.assignedTo}</dd>
        </div>
        <div>
          <dt className="inline font-mono text-[11.5px] font-[600] text-foreground">owner</dt>
          <dd className="inline"> — {OWNERSHIP_NOTE.owner}</dd>
        </div>
        <div>
          <dt className="inline text-[12.5px] font-[600] text-foreground">Per stage</dt>
          <dd className="inline"> — {OWNERSHIP_NOTE.perStage}</dd>
        </div>
      </dl>
    </section>
  );
}

export function Lifecycle() {
  const [counts, setCounts] = useState<StageCount[]>([]);
  const [totalTasks, setTotalTasks] = useState<number | null>(null);
  const [offVocabulary, setOffVocabulary] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [firstLoadDone, setFirstLoadDone] = useState(false);

  // One fetch on mount. These are aggregate counts over the whole task table;
  // they move slowly and a poll would re-run eleven queries for nothing.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch("/api/lifecycle");
        const body = (await res.json()) as LifecyclePayload;
        if (!res.ok || body.error) throw new Error(body.error ?? `HTTP ${res.status}`);
        if (!cancelled) {
          setCounts(body.counts ?? []);
          setTotalTasks(body.totalTasks ?? null);
          setOffVocabulary(body.offVocabulary ?? null);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) setError((err as Error).message);
      } finally {
        if (!cancelled) setFirstLoadDone(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const byStatus = useMemo(() => countsByStatus(counts), [counts]);
  const pending = showSkeleton(!firstLoadDone, counts.length > 0);

  /**
   * How much of the task table the state machine covers at all.
   *
   * `sumMeasured` returns null if ANY of the eleven counts is missing, so this
   * share is either measured over all eleven or not stated. A partial sum here
   * would understate coverage and make the fail-open problem look smaller than
   * it is — a fabricated number arguing the wrong way.
   */
  const inVocabulary = useMemo(
    () => sumMeasured(byStatus, STAGES.map((s) => s.key)),
    [byStatus],
  );

  return (
    <div>
      <header className="mb-2 flex flex-wrap items-center gap-x-[8px] gap-y-[4px]">
        <CircleDot className="h-4 w-4 text-muted-foreground" aria-hidden />
        <h1 className="text-[16px] font-[650] tracking-[-0.02em]">Lifecycle</h1>
        <span className="text-[12px] text-muted-foreground">
          {byKind("path").length}-state path, {byKind("hold").length} holds beside it, and{" "}
          {STAGES.filter((s) => s.terminal).length} endings
        </span>
      </header>

      <SourceNote totalTasks={totalTasks} />
      <ReviewNote />

      {error && (
        <div className="my-4 rounded-lg border border-[hsl(var(--bad)/0.35)] bg-[hsl(var(--bad)/0.12)] px-3 py-[10px] text-[13px]">
          <strong>Cannot load task counts.</strong> {error} The stage definitions below are
          code-sourced and unaffected; every count reads “not measured” rather than zero.
        </div>
      )}

      <PathRail counts={byStatus} pending={pending} />
      <HoldsGrid counts={byStatus} pending={pending} />
      <EndingsGroup counts={byStatus} pending={pending} />

      {!pending && (
        <ValidationNote
          inVocabulary={inVocabulary}
          offVocabulary={offVocabulary}
          totalTasks={totalTasks}
        />
      )}

      <OwnershipNote />
    </div>
  );
}
