/**
 * OPEN QUESTIONS SIDEBAR
 * ----------------------
 * The operator works by voice. A question the agent asks in chat scrolls away
 * under transcript chunks and status lines within a turn or two, so questions
 * either get re-asked every turn or get quietly dropped. This panel is the
 * durable surface: every outstanding question stays visible until answered.
 *
 * It is a persistent right-hand sidebar rather than a nav destination, and that
 * is the whole point — a question is most useful precisely while the operator
 * is doing something else. Raised while they are reading an agent definition,
 * it appears right there and can be answered in passing, instead of waiting in
 * a page someone has to remember to visit.
 *
 * Collapsing is safe only because the collapsed rail still carries the count of
 * unanswered questions; without that badge, collapsing would just be a way to
 * forget. With zero outstanding, the sidebar gets out of the way entirely.
 *
 * Questions are `task` entities marked with `category: "open_question"`, so
 * they arrive on the same poll as everything else — no second fetch and no
 * second refresh clock.
 *
 * THE QUEUE, NOT THE READING SURFACE
 * ----------------------------------
 * Each card carries only what it takes to CHOOSE one: reference number, name,
 * priority, answered state, a single clamped line of teaser, and — when the
 * agent recorded one — a single clamped line of its RECOMMENDATION. The full
 * text — descriptions run to several hundred words, with lettered options —
 * lives on the detail view at `#/questions/<id>`, which a card opens. Rendering
 * that text in a 360px rail is what made the queue unreadable: one long
 * question buried every other question below it.
 *
 * THE RECOMMENDATION IS ON THE CARD ON PURPOSE
 * --------------------------------------------
 * It used to be a bare "Rec" flag, which told the operator a recommendation
 * existed but not what it said — so acting on it still cost opening the
 * question and reading several hundred words to find one line. One clamped line
 * here lets him work down the queue and answer several questions without
 * opening any of them, which is the entire value of the field being separate.
 *
 * It is clamped to ONE line and truncated by CSS, never by cutting the stored
 * string: a long recommendation must not be able to grow the card and push the
 * rest of the queue off-screen, which is what long descriptions did before.
 *
 * READ-ONLY BY CONSTRUCTION
 * -------------------------
 * Questions are answered by talking to the orchestrating agent, never from this
 * page. There is deliberately no answer box and no write route behind it: a
 * dashboard that can write is a dashboard an agent can write through, and a
 * fabricated answer landing on a question is then a mistake the UI is capable
 * of making. Removing the path makes that class of error impossible rather than
 * merely discouraged. Answered questions still DISPLAY their stored answer.
 */
import { useMemo, useState } from "react";
import { type Task, isAnswered, priorityRank, relativeTime } from "./tasks";
import { splitRecommendation } from "./questionText";
import { type QuestionRef, questionRefs } from "./questionRefs";
import type { QuestionTally } from "./questionCount";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { QuestionsSkeleton } from "@/components/Skeletons";
import { showSkeleton } from "@/lib/loading";
import { cn } from "@/lib/utils";
import { ChevronLeft, ChevronRight } from "lucide-react";

/** Per-viewer UI state only — never domain data, which lives in Neotoma. */
const STORAGE_KEY = "ateles.questions.expanded";

function loadExpanded(): boolean | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw === null ? null : raw === "1";
  } catch {
    // Private mode or blocked site data — fall back to the count-based default.
    return null;
  }
}

interface Props {
  questions: Task[];
  /**
   * How many open questions exist, against how many arrived on the task page.
   * The rail's own rows come from the newest-200 task poll, so this is the only
   * thing that can tell it whether the queue it renders is the whole queue.
   */
  coverage: QuestionTally;
  /** True once the first poll has settled. Drives skeletons; see App.tsx. */
  firstLoadDone: boolean;
  /** Opens one question's detail view. Navigation only — never a write. */
  onOpen: (id: string) => void;
  /** Id of the question currently open, so the queue marks the reader's place. */
  openId: string | null;
}

export function Questions({ questions, coverage, firstLoadDone, onOpen, openId }: Props) {
  // Highest priority first, then newest — the question just asked is usually
  // the one being spoken about, but an urgent older one outranks it.
  const sorted = [...questions].sort(
    (a, b) =>
      priorityRank(a.priority) - priorityRank(b.priority) ||
      (b.updatedAt?.getTime() ?? 0) - (a.updatedAt?.getTime() ?? 0),
  );
  const open = sorted.filter((q) => !isAnswered(q));
  const answered = sorted.filter(isAnswered);

  /**
   * The figure the rail states — the measured total where it is known, and the
   * loaded count otherwise.
   *
   * Preferring the measured total means the badge stays correct even when a
   * question has aged out of the task window, which is the whole point. Falling
   * back to the loaded count when it is unmeasured means a failed count route
   * degrades the rail to its old behaviour rather than blanking a queue the
   * operator can plainly see rows in.
   */
  const railCount = coverage.totalOpen ?? open.length;

  // Reference numbers are keyed by entity id and derived from creation order,
  // NOT from the sorted order above — see questionRefs.ts. Sorting by priority
  // must never renumber a question the operator is about to answer by number.
  const refs = useMemo(() => questionRefs(questions), [questions]);

  // First load only — a poll must never flash the sidebar back to skeletons.
  const pending = showSkeleton(!firstLoadDone, questions.length > 0);

  // Null means "no stored preference": default to expanded only when something
  // is actually waiting, so an idle sidebar never steals space. While the first
  // load is still in flight, default to expanded so the skeleton is visible
  // rather than the panel snapping open once data lands.
  const [stored, setStored] = useState<boolean | null>(loadExpanded);
  const expanded = stored ?? (pending || open.length > 0);

  // A question arriving while collapsed should not silently expand the panel —
  // the badge is what surfaces it. Only an explicit toggle changes the state.
  const setExpanded = (next: boolean) => {
    setStored(next);
    try {
      localStorage.setItem(STORAGE_KEY, next ? "1" : "0");
    } catch {
      // Preference simply does not persist; the panel still works this session.
    }
  };

  return (
    <Collapsible
      open={expanded}
      onOpenChange={setExpanded}
      asChild
      // Horizontal collapse: Radix's height animation does not apply, so the
      // rail is rendered as the trigger's collapsed state instead.
    >
      <aside
        className={cn(
          // Offsets track the 38px header; they are the same number in two
          // places and drift silently apart if only one is edited.
          "sticky top-[38px] flex flex-none self-start border-l bg-card",
          "max-h-[calc(100vh-38px)]",
          expanded ? "w-[326px] flex-col" : "w-[36px]",
          // Narrow viewports: the sidebar drops below the content rather than
          // squeezing it into an unreadable column.
          "max-[860px]:static max-[860px]:max-h-none max-[860px]:w-full max-[860px]:border-l-0 max-[860px]:border-t",
          !expanded && "max-[860px]:flex-row",
        )}
      >
        <CollapsibleTrigger asChild>
          <button
            className={cn(
              "flex w-full flex-none cursor-pointer items-center gap-2 border-none bg-transparent px-[10px] py-[6px] text-[12px] text-muted-foreground hover:text-foreground",
              expanded
                ? "border-b border-border"
                : "h-full flex-col justify-start px-0 py-[6px] max-[860px]:h-auto max-[860px]:flex-row max-[860px]:p-2",
            )}
            title={expanded ? "Collapse questions" : "Expand questions"}
          >
            {expanded ? (
              <>
                <span className="text-[12.5px] font-semibold tracking-[-0.01em] text-foreground">
                  Open questions
                </span>
                <ChevronRight className="ml-auto h-4 w-4" aria-hidden />
              </>
            ) : (
              <>
                <ChevronLeft className="h-4 w-4" aria-hidden />
                {/* The badge is what makes collapsing safe rather than a way to
                    forget: outstanding work stays visible even when the panel
                    is not. It survives the loading state too — a count of zero
                    during first load would read as "nothing waiting". */}
                {/* The badge counts every open question that EXISTS, not the
                    ones this page loaded — a question aged out of the newest-200
                    task window is exactly the one that has waited longest, and a
                    collapsed rail showing nothing is indistinguishable from an
                    empty queue. `railCount` falls back to the loaded figure when
                    the total could not be read. */}
                {railCount > 0 && (
                  <Badge variant="counter">
                    {railCount}
                    {coverage.missing !== null && coverage.missing > 0 && "*"}
                  </Badge>
                )}
                <span className="qrail mt-[10px] max-[860px]:mt-0" aria-hidden>
                  QUESTIONS
                </span>
              </>
            )}
          </button>
        </CollapsibleTrigger>

        <CollapsibleContent asChild>
          <ScrollArea className="max-h-[calc(100vh-76px)] flex-1 max-[860px]:max-h-[60vh]">
            <div className="px-[10px] pb-4 pt-[8px]">
              <div className="mb-[6px] flex items-baseline gap-[10px]">
                {/*
                 * "Nothing awaiting you" is the sentence with the most to lose:
                 * said while a question sits outside the loaded window, it is
                 * not a short list but a false all-clear. It is only ever
                 * printed when the total was measured and is genuinely zero.
                 */}
                <span className="text-[11px] text-muted-foreground">
                  {pending
                    ? "Loading…"
                    : railCount > 0
                      ? `${railCount} awaiting you`
                      : coverage.totalOpen === null
                        ? "None loaded — the open-question count could not be read"
                        : "Nothing awaiting you"}
                </span>
              </div>

              {/*
               * THE ONE THING THE RAIL COULD NOT SAY BEFORE. Rows here are
               * whatever the newest-200 task page happened to contain; a
               * question older than that is unreachable from any surface in
               * this app. Naming the shortfall is the difference between a
               * queue that is short and a queue that is lying.
               */}
              {!pending && coverage.missing !== null && coverage.missing > 0 && (
                <p className="mb-[8px] rounded-[7px] border border-warn bg-[hsl(var(--warn)/0.12)] px-[8px] py-[6px] text-[11.5px] leading-[1.5]">
                  <strong>
                    {coverage.missing} open{" "}
                    {coverage.missing === 1 ? "question is" : "questions are"} not shown.
                  </strong>{" "}
                  This rail lists questions found in the most recent 200 tasks, so the ones
                  missing are the oldest — and the longest unanswered. Open the Tasks tab and
                  filter to reach them.
                </p>
              )}

              {pending ? (
                <QuestionsSkeleton cards={3} />
              ) : (
                <>
                  {!sorted.length && (
                    <p className="p-6 text-center text-muted-foreground">
                      No questions outstanding.
                    </p>
                  )}

                  <ul className="m-0 flex list-none flex-col gap-[4px] p-0">
                    {open.map((q) => (
                      <QuestionCard
                        key={q.id}
                        q={q}
                        qref={refs.get(q.id) ?? null}
                        onOpen={onOpen}
                        active={q.id === openId}
                      />
                    ))}
                  </ul>

                  {answered.length > 0 && (
                    <>
                      <div className="mb-[4px] mt-[10px] text-[10px] uppercase tracking-[.06em] text-muted-foreground">
                        Answered
                      </div>
                      <ul className="m-0 flex list-none flex-col gap-[4px] p-0">
                        {answered.map((q) => (
                          <QuestionCard
                            key={q.id}
                            q={q}
                            qref={refs.get(q.id) ?? null}
                            onOpen={onOpen}
                            active={q.id === openId}
                          />
                        ))}
                      </ul>
                    </>
                  )}
                </>
              )}
            </div>
          </ScrollArea>
        </CollapsibleContent>
      </aside>
    </Collapsible>
  );
}

/**
 * The recommendation as ONE LINE of plain text, or null when there is none.
 *
 * Two sources, in order of authority: the declared `details` field (parsed in
 * tasks.ts), then an inline `RECOMMENDATION:` marker inside the description —
 * the fallback for questions filed before `details` became the convention. The
 * fallback is a READ, not a backfill: nothing is written back to the entity.
 *
 * Markdown emphasis is stripped rather than rendered. The card is a queue row,
 * not a reading surface, and half-rendered bold in a clamped line reads as
 * noise; the detail view renders the same text properly.
 */
function cardRecommendation(q: Task): string | null {
  const source =
    q.recommendation ??
    (q.description ? splitRecommendation(q.description).recommendation : null);
  if (!source) return null;

  const flat = source
    .replace(/\s+/g, " ")
    // Leading list markers and emphasis carry no meaning once flattened.
    .replace(/^[-*+]\s+/, "")
    .replace(/[*_`]/g, "")
    .trim();
  return flat || null;
}

/**
 * One question in the QUEUE. Read-only, and deliberately terse: reference
 * number, name, priority, answered state, and at most one clamped line of
 * teaser. Everything else — the full description, the recommendation, the
 * stored answer — is on the detail view, one click away.
 *
 * The card is a button because its whole job is to open that view. Nothing
 * here writes; navigating is the only side effect it has.
 */
function QuestionCard({
  q,
  qref,
  onOpen,
  active,
}: {
  q: Task;
  qref: QuestionRef | null;
  onOpen: (id: string) => void;
  active: boolean;
}) {
  const answered = isAnswered(q);

  // One line of teaser, and only when it adds something the name does not.
  // `context` is the situation the agent recorded; `description` is the body,
  // whose first line is often what the title was already derived from.
  const teaser = q.context && q.context !== q.title ? q.context : null;

  // The recommendation, flattened to one line. `details` is the declared home;
  // an inline "RECOMMENDATION:" inside the description is the fallback for
  // questions filed before that convention (see questionText.ts). Newlines
  // collapse to spaces because a clamped line renders them as gaps otherwise.
  const rec = useMemo(() => cardRecommendation(q), [q]);

  return (
    <li>
      <Card
        className={cn(
          "overflow-hidden p-0 transition-colors hover:border-live",
          // Unanswered questions are the live edge of the panel; answered ones
          // recede.
          answered ? "opacity-[.62]" : "border-l-[3px] border-l-live",
          active && "border-live ring-1 ring-live",
        )}
      >
        <button
          type="button"
          onClick={() => onOpen(q.id)}
          aria-current={active ? "true" : undefined}
          className="block w-full cursor-pointer border-none bg-transparent px-[9px] py-[6px] text-left text-inherit"
        >
          <div className="flex items-baseline gap-[7px] text-[12.5px] font-medium">
            {/* The reference number is the point of contact with the voice
                channel: the operator says "do the recommendation on 2" rather
                than reading a title back. It comes from Neotoma, never from
                list position. */}
            {qref && (
              <Badge
                variant={answered ? "refDone" : "ref"}
                // A derived number is a display aid this app computed, not a
                // value Neotoma holds. The dotted underline distinguishes the
                // two without spending a colour on it.
                className={cn(!qref.stored && "border-dashed")}
                title={
                  qref.stored
                    ? `Question ${qref.n} — stored reference`
                    : `Question ${qref.n} — numbered by creation order; no reference stored on the entity`
                }
              >
                #{qref.n}
              </Badge>
            )}
            <span
              className={cn(
                "h-[7px] w-[7px] flex-none -translate-y-px rounded-full",
                answered ? "bg-ok" : "bg-live",
              )}
              aria-hidden
            />
            <span className="min-w-0 flex-1">{q.title}</span>
          </div>

          {teaser && (
            <p className="mb-0 mt-[3px] line-clamp-1 text-[11.5px] text-muted-foreground">
              {teaser}
            </p>
          )}

          {/* THE RECOMMENDATION, one line, so the queue can be worked without
              opening anything. Amber matches the detail view's treatment: an
              agent's suggestion, never the operator's decision — which is why
              it is labelled "Rec" in words rather than left to read as a
              conclusion. `line-clamp-1` truncates in CSS, so an arbitrarily
              long stored recommendation cannot grow the card.

              Hidden once answered: at that point the operator's own decision is
              the live fact, and a stale suggestion beside it invites acting on
              the wrong one. */}
          {rec && !answered && (
            <p className="mb-0 mt-[3px] flex items-baseline gap-[5px] text-[11.5px]">
              <span className="flex-none text-[10px] uppercase tracking-[.04em] text-warn">
                Rec
              </span>
              <span className="line-clamp-1 min-w-0 text-warn/85">{rec}</span>
            </p>
          )}

          <div className="mt-[4px] flex flex-wrap items-center gap-[8px] text-[11px] text-muted-foreground">
            {q.priority && (
              <span className="text-[10px] uppercase tracking-[.04em]">{q.priority}</span>
            )}
            <span className="ml-auto tabular-nums">{relativeTime(q.updatedAt)}</span>
          </div>
        </button>
      </Card>
    </li>
  );
}
