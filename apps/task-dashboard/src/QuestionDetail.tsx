/**
 * QUESTION DETAIL
 * ---------------
 * One open question, read in full. The sidebar is the QUEUE — it exists to let
 * the operator scan what is waiting and pick one — and this is the READING
 * surface. Question #5's description alone runs to ~2,700 characters with three
 * lettered options and a recommendation; in a 360px rail that is unreadable, so
 * the card carries a one-line teaser and the whole text lives here.
 *
 * READ-ONLY, like every other surface in this app. There is no answer box and
 * no write route behind one. Answers arrive through the operator's voice
 * conversation with the orchestrating agent; a stored answer is DISPLAYED here
 * but can only ever have been written there. See Questions.tsx and the proxy.
 *
 * AMBER vs GREEN is load-bearing, not decoration. The recommendation is an
 * agent's suggestion; the answer is the operator's decision. They must never be
 * confusable, so the recommendation keeps the sidebar's amber and the answer
 * keeps its green, and the recommendation is labelled as a suggestion in words
 * as well as in colour.
 */
import { type Task, absoluteTime, entityUrl, isAnswered, relativeTime } from "./tasks";
import { Markdown } from "./Markdown";
import { splitRecommendation, toMarkdown } from "./questionText";
import { type QuestionRef } from "./questionRefs";
import { textReferences, useQuestionLinks } from "./questionLinks";
import { entityHash, typeLabel } from "./entity";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { cn } from "@/lib/utils";
import { ArrowLeft, ExternalLink } from "lucide-react";

/** Priority pill tone, matching the task view's urgency reading. */
function priorityTone(priority: string | null): "bad" | "warn" | "live" | "muted" {
  switch ((priority ?? "").toLowerCase()) {
    case "critical":
    case "urgent":
      return "bad";
    case "high":
      return "warn";
    case "medium":
      return "live";
    default:
      return "muted";
  }
}

/** One labelled scalar, omitted when unset — as on the agent detail view. */
function Fact({ label, value }: { label: string; value: string | null }) {
  if (!value) return null;
  return (
    <div className="flex items-baseline gap-[5px]">
      <span className="text-[10px] uppercase tracking-[.06em] text-muted-foreground">
        {label}
      </span>
      <span className="text-[12px] tabular-nums">{value}</span>
    </div>
  );
}

/**
 * A block of stored prose, rendered through the app's markdown renderer.
 *
 * The text is PLAIN TEXT with newlines rather than markdown, so it goes through
 * `toMarkdown` first — see questionText.ts for why a markdown renderer alone
 * would collapse the paragraph structure into a wall.
 */
function Prose({ label, source }: { label: string; source: string }) {
  return (
    <div className="my-[18px]">
      <span className="mb-[6px] block text-[10px] uppercase tracking-[.06em] text-muted-foreground">
        {label}
      </span>
      <Markdown source={toMarkdown(source)} />
    </div>
  );
}

interface Props {
  question: Task;
  /** The spoken handle for this question. See questionRefs.ts. */
  qref: QuestionRef | null;
  /** Returns to wherever the operator opened this from. */
  onBack: () => void;
  /** Label for the back control, naming that origin. */
  backLabel: string;
  /** Opens a related entity in the slide-over, keeping the operator's place. */
  onOpenEntity?: (id: string) => void;
}

export function QuestionDetail({ question: q, qref, onBack, backLabel, onOpenEntity }: Props) {
  const answered = isAnswered(q);

  // Stored relationships, fetched when this question is opened.
  const { links, loading: linksLoading, failed: linksFailed } = useQuestionLinks(q.id);
  // Issue/PR numbers named in the prose — evidence the author typed them, and
  // deliberately NOT presented as stored edges. See questionLinks.ts.
  const refs = textReferences([q.title, q.context, q.description, q.recommendation]);

  // #5 carries its recommendation inline at the end of the description as well
  // as in `details`. Split it out so it gets the amber treatment rather than
  // reading as the last paragraph of the operator's brief.
  const split = q.description ? splitRecommendation(q.description) : null;
  const body = split?.body ?? null;
  // `details` is the declared home for the recommendation; an inline one is the
  // fallback. Showing both when they duplicate each other would just be noise.
  const inline = split?.recommendation ?? null;
  const recommendation =
    q.recommendation ?? (inline && inline !== q.recommendation ? inline : null);
  const alsoInline = q.recommendation && inline && inline !== q.recommendation ? inline : null;

  return (
    <article>
      <Button variant="outline" size="sm" className="mb-[14px]" onClick={onBack}>
        <ArrowLeft className="h-[14px] w-[14px]" aria-hidden />
        {backLabel}
      </Button>

      <header className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-[10px]">
            {/* The reference number is the point of contact with the voice
                channel: the operator says "do the recommendation on 5". It
                comes from Neotoma, never from list position. */}
            {qref && (
              <Badge
                variant={answered ? "refDone" : "ref"}
                className={cn("text-[13px]", !qref.stored && "border-dashed")}
                title={
                  qref.stored
                    ? `Question ${qref.n} — stored reference`
                    : `Question ${qref.n} — numbered by creation order; no reference stored on the entity`
                }
              >
                #{qref.n}
              </Badge>
            )}
            <h1 className="m-0 text-[20px] tracking-[-0.01em]">{q.title}</h1>
          </div>
          <p className="mb-0 mt-[6px] flex flex-wrap items-center gap-[10px] text-[13px] text-muted-foreground">
            <span
              className={cn(
                "h-[7px] w-[7px] flex-none rounded-full",
                answered ? "bg-ok" : "bg-live",
              )}
              aria-hidden
            />
            {answered ? "Answered" : "Awaiting your answer"}
          </p>
        </div>
        <div className="flex flex-none flex-wrap items-center gap-[6px]">
          {q.priority && (
            <Badge variant={priorityTone(q.priority)} caps>
              {q.priority}
            </Badge>
          )}
          <Badge variant={answered ? "ok" : "live"} caps>
            {q.status}
          </Badge>
        </div>
      </header>

      {/* `computed_at` is labelled for what it is. `/entities/query` exposes no
          creation timestamp and the task schema has no such field, so calling
          it "Created" would assert something the data does not say. */}
      <div className="my-[8px] flex flex-wrap gap-x-[18px] gap-y-[3px] rounded-[7px] border bg-card px-[10px] py-[6px]">
        <Fact label="Reference" value={q.ref !== null ? `#${q.ref}` : null} />
        <Fact label="Priority" value={q.priority} />
        <Fact label="First computed" value={absoluteTime(q.computedAt)} />
        <Fact label="Updated" value={absoluteTime(q.updatedAt)} />
        <Fact label="Last change" value={relativeTime(q.updatedAt)} />
        <Fact label="Updated date" value={q.updatedDate} />
      </div>

      {/* The situation the agent recorded alongside the question. */}
      {q.context && <Prose label="Context" source={q.context} />}

      {/* The full description — never clamped here; that is the whole point of
          this view existing. */}
      {body && body !== q.context && <Prose label="Question" source={body} />}

      {/* WHAT THIS QUESTION IS ABOUT.
          Two kinds of link, never blurred together — stored relationships are
          navigable because the entity provably exists; issue numbers scraped
          from the prose go to GitHub and are labelled as coming from the text.
          See questionLinks.ts for what the graph actually holds today. */}
      {(linksLoading || links.length > 0 || refs.length > 0 || linksFailed) && (
        <section className="my-[18px]">
          <span className="mb-[6px] block text-[10px] uppercase tracking-[.06em] text-muted-foreground">
            Related
          </span>

          {linksLoading && (
            <p className="m-0 text-[12px] text-muted-foreground">Loading relationships…</p>
          )}

          {linksFailed && (
            /* A failed read is not an empty graph. Saying "no related entities"
               here would be a claim about his data rather than the request. */
            <p className="m-0 text-[12px] text-muted-foreground">
              Relationships could not be read. The question's own fields are unaffected.
            </p>
          )}

          {links.length > 0 && (
            <ul className="m-0 flex list-none flex-col gap-[3px] p-0">
              {links.map((l) => (
                <li key={l.id}>
                  <a
                    href={entityHash(l.id)}
                    onClick={(e) => {
                      // The slide-over keeps the operator on the question he is
                      // reading; the href stays a real link so it is copyable
                      // and middle-clickable.
                      if (onOpenEntity && !e.metaKey && !e.ctrlKey && e.button === 0) {
                        e.preventDefault();
                        onOpenEntity(l.id);
                      }
                    }}
                    className={cn(
                      "flex items-baseline gap-[7px] rounded-[6px] border px-[9px] py-[5px] text-[12.5px] no-underline",
                      "hover:border-live",
                      l.ambient ? "opacity-[.66]" : "",
                    )}
                  >
                    <Badge variant="muted" className="flex-none text-[10px]">
                      {typeLabel(l.entityType)}
                    </Badge>
                    <span
                      className={cn(
                        "min-w-0 flex-1 truncate",
                        l.label ? "text-foreground" : "italic text-muted-foreground",
                      )}
                    >
                      {/* A neighbour whose name has not been hydrated yet: say
                          so, rather than printing a bare hex id as if it were
                          a title. The link still works. */}
                      {l.label ?? "Unnamed entity"}
                    </span>
                    <span className="flex-none text-[10px] uppercase tracking-[.04em] text-muted-foreground">
                      {l.relationship}
                    </span>
                  </a>
                </li>
              ))}
            </ul>
          )}

          {refs.length > 0 && (
            <div className="mt-[8px]">
              <span className="mb-[4px] block text-[10px] uppercase tracking-[.06em] text-muted-foreground">
                Named in the text
              </span>
              <div className="flex flex-wrap gap-[5px]">
                {refs.map((r) => (
                  <a
                    key={r.label}
                    href={r.url}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-[5px] rounded-[6px] border px-[8px] py-[3px] text-[12px] text-live no-underline hover:border-live"
                  >
                    {r.label}
                    <ExternalLink className="h-[11px] w-[11px]" aria-hidden />
                  </a>
                ))}
              </div>
              {/* Stated rather than left to be inferred: these are numbers the
                  author typed, not relationships Neotoma stores. */}
              <p className="mb-0 mt-[5px] text-[11px] text-muted-foreground">
                Read from the question text — not stored relationships.
              </p>
            </div>
          )}

          {!linksLoading && !linksFailed && links.length === 0 && refs.length === 0 && (
            <p className="m-0 text-[12px] text-muted-foreground">
              No related entities recorded.
            </p>
          )}
        </section>
      )}

      {recommendation && (
        /* AMBER — an agent's suggestion. Deliberately distinct from the green
           answer block below: a suggestion must never read as a decision. */
        <section className="my-[18px] rounded-[10px] border border-[hsl(var(--warn)/0.26)] bg-[hsl(var(--warn)/0.08)] px-[14px] py-3">
          <div className="mb-[2px] flex items-baseline gap-[8px]">
            <span className="text-[10px] uppercase tracking-[.06em] text-warn">
              Recommendation
            </span>
            <span className="text-[11px] text-muted-foreground">
              agent suggestion — not a decision
            </span>
          </div>
          <Markdown source={toMarkdown(recommendation)} />
          {alsoInline && (
            <>
              <Separator className="my-[10px] bg-[hsl(var(--warn)/0.22)]" />
              <span className="mb-[2px] block text-[10px] uppercase tracking-[.06em] text-warn">
                Also recorded in the question text
              </span>
              <Markdown source={toMarkdown(alsoInline)} />
            </>
          )}
        </section>
      )}

      {q.answer ? (
        /* GREEN — the operator's own decision, given through the voice
           conversation. This view only ever displays it. */
        <section className="my-[18px] rounded-[10px] border border-[hsl(var(--ok)/0.28)] bg-[hsl(var(--ok)/0.10)] px-[14px] py-3">
          <div className="mb-[2px] flex items-baseline gap-[8px]">
            <span className="text-[10px] uppercase tracking-[.06em] text-ok">Answer</span>
            <span className="text-[11px] text-muted-foreground">your decision</span>
          </div>
          <Markdown source={toMarkdown(q.answer)} />
        </section>
      ) : (
        <p className="my-[18px] rounded-[10px] border border-dashed px-[14px] py-3 text-[13px] text-muted-foreground">
          No answer recorded yet. Questions are answered by talking to the agent —
          this dashboard only ever displays them.
        </p>
      )}

      <Separator className="my-[22px]" />

      <p className="m-0 text-[13px]">
        <a
          href={entityUrl(q.id)}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-[6px] text-live no-underline hover:underline"
        >
          Open in Neotoma
          <ExternalLink className="h-[13px] w-[13px]" aria-hidden />
        </a>
      </p>
    </article>
  );
}
