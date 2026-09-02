/**
 * SESSIONS
 * --------
 * What each agent session claimed to do, and whether anyone checked.
 *
 * TWO VIEWS, NOT ONE. This module serves both, and `view` selects:
 *
 *   "current" — ROOT (`#/`). The live session: what is happening right now,
 *     what it has produced, what it is waiting on. The operator lands here
 *     because this session already links to everything else worth reaching.
 *   "index"   — `#/sessions`. A proper index of every session: searchable,
 *     filterable by archived state and by harness sidebar group, sortable by
 *     recency, with enough on each row to choose one.
 *
 * These used to be the SAME view — root and `#/sessions` both rendered the
 * current session, and the list hid behind `#/sessions/all`. The app therefore
 * had no index anybody would find, and the Sessions nav tab pointed at a
 * session rather than at sessions. See `route.ts`.
 *
 * NAMING A SESSION. Only 59 of 344 digests store a `session_title`; the rest
 * previously displayed their `worktree` PATH, and 75 of those are the identical
 * string `/Users/markmhendrickson/repos/ateles`. `displayName()` in
 * `sessionDigest.ts` derives a name from `topics` instead, which is populated
 * on 342 of 344 and yields 338 distinct names. A derived name is LABELLED as
 * derived and the path is demoted to secondary context — the app is read-only,
 * so a display name must never be passed off as a stored one.
 *
 * WHERE REFERENCED ENTITIES GO
 * ----------------------------
 * Into the slide-over sheet, NOT out to Neotoma. Leaving cost the operator this
 * page, and the destination 401s without an access_token anyway (those exist
 * only in publish-time responses). A row opens `EntityDetail` in an overlay, so
 * he keeps his place; the sheet then offers the full page at `#/entities/<id>`
 * — the canonical, linkable address — and the external Neotoma link as a
 * clearly-secondary action.
 *
 * WHERE THE CURRENT SESSION COMES FROM
 * ------------------------------------
 * From Neotoma: the `conversation` entity whose `conversation_id` equals the
 * harness session uuid. That join is exact, so the session's identity and its
 * work are stored data, not an inference. The filesystem supplies only the uuid
 * to look up — see `liveSession()` in the proxy.
 *
 * What the session touched is its OUTBOUND EDGES: `REFERS_TO` the entities it
 * worked on, `PART_OF` the plan it ran under. Every referenced entity is shown,
 * split by what the operator asks of it rather than by type alone: tasks go in
 * ONE table (the work), everything else goes under `Produced` (the outputs),
 * and the plan rides in the facts strip as the thing the session runs UNDER.
 * See `ConversationBody` for the five questions that split drives.
 *
 * Traversal uses `GET /entities/<id>/relationships`. `list_relationships`
 * filtered by source or target returns empty for edges that demonstrably exist,
 * so it is deliberately not used.
 *
 * WHEN NO CONVERSATION MATCHES
 * ----------------------------
 * Older sessions have none — `conversation` ids were agent-authored slugs
 * ("lanius-pr-558-synchronize") before this convention — and there is then no
 * per-session work list at all: no `task` entity carries a session reference,
 * and `task.conversation_id`, where set, holds hand-written slugs rather than
 * session ids. That case falls back to the digest, explicitly labelled. An
 * unlabelled empty list would read as "this session did no work", a confident
 * wrong answer of the kind this dashboard exists to catch.
 *
 * VOCABULARY
 * ----------
 * Every label here traces to an entity_type, a field name, or a stored field
 * value. Types are named as types ("Rendered pages") in `Produced`, where the
 * rows actually differ by type. The task table drops the per-row type label —
 * every row in it is a `task`, so repeating it 50 times said nothing — and
 * heads its groups with the `category: topic` task each one is `PART_OF`,
 * never renaming that into a concept Neotoma does not store.
 *
 * READ-ONLY: GETs to /api/sessions and /api/conversation. No write route exists
 * behind either.
 */
import { Fragment, useEffect, useMemo, useState } from "react";
import {
  type Claim,
  type LiveSessionHint,
  type NameSource,
  type SessionDigest,
  type SessionEntity,
  type SessionsPayload,
  byRecency,
  claimMix,
  claimStatusTone,
  durationHours,
  formatDuration,
  matchesQuery,
  measureCoverage,
  parseSession,
  shortDate,
  verificationTone,
} from "./sessionDigest";
import {
  type Conversation,
  type ConversationPayload,
  type Grouping,
  type Ownership,
  type RelatedEntity,
  type TaskGroup,
  groupRelated,
  groupTasks,
  isOpenEntity,
  isClosedStatus,
  ownership,
  parseConversation,
  relatedStatus,
  relatedTitle,
  sessionPlans,
} from "./conversation";
import { DISPATCHABLE_ROLES, dispatchability } from "./taskState";
/**
 * Aliased: this file already has a local `Coverage` component, which reports
 * the date range the digests span. Different question, same good word.
 */
import { type Coverage as ListCoverage, coverageOf } from "./listTotal";
import { CoverageCount, CoverageNotice } from "./ListCoverage";
import { type AgentRef, AssignedTo } from "./AssignedTo";
import { useRoster } from "./useRoster";
import { relativeTime, toBucket } from "./tasks";
import { Markdown } from "./Markdown";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { SessionDetailSkeleton, SessionListSkeleton } from "@/components/Skeletons";
import { showSkeleton } from "@/lib/loading";
import { cn } from "@/lib/utils";
import { ArrowLeft } from "lucide-react";

interface Props {
  /**
   * Which of the two views this is.
   *
   *   "current" — ROOT: the live session, what is happening now.
   *   "index"   — `#/sessions`: every session, scannable and filterable.
   *
   * They were the same view until now, and conflating them is what left the
   * app with no real index. See `route.ts`.
   */
  view: "current" | "index";
  /** An `ent_` id when one session's digest is open, else null. */
  selected: string | null;
  onSelect: (id: string | null) => void;
  /** Navigate to root — the current session. */
  onOpenCurrent: () => void;
  /**
   * Open one entity in the slide-over sheet.
   *
   * Referenced entities used to link OUT to Neotoma, which lost this page and
   * 401'd on arrival. They now open in an overlay over this view, so the
   * operator keeps the session he is reading from; the sheet offers the full
   * page at `#/entities/<id>` for anything he wants a real URL to.
   */
  onOpenEntity: (id: string) => void;
  /** Open one agent's detail page, from a task's `assigned_to`. */
  onOpenAgent: (id: string) => void;
}

const REFRESH_MS = 10_000;

export function Sessions({
  view,
  selected,
  onSelect,
  onOpenCurrent,
  onOpenEntity,
  onOpenAgent,
}: Props) {
  const [sessions, setSessions] = useState<SessionDigest[]>([]);
  /**
   * How much of the digest store the rows above actually represent.
   *
   * `sessions.length` used to be printed directly as "344 digested" and "344
   * sessions". Both were right only because 344 happens to be under the 400-row
   * request limit — the figure was a page size wearing a total's clothes, and
   * the 401st digest would have made it silently wrong with nothing on screen
   * to say so. `total` has always been in the response; it was simply dropped.
   */
  const [coverage, setCoverage] = useState<ListCoverage>({ kind: "unknown", received: 0 });
  const [live, setLive] = useState<LiveSessionHint | null>(null);
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [related, setRelated] = useState<RelatedEntity[]>([]);
  const [error, setError] = useState<string | null>(null);
  /**
   * Did the DIGEST read specifically fail on the last poll?
   *
   * Tracked apart from `error`, which only fires when BOTH reads fail. The
   * index is built from the digest read alone, so a digest-only failure would
   * otherwise reach the table as an empty array and render as "no sessions" —
   * a timeout presented as a measurement. Neotoma is currently answering in
   * 25-81s (ateles#576), so this is the common case, not the exotic one.
   */
  const [digestFailed, setDigestFailed] = useState(false);
  const [firstLoadDone, setFirstLoadDone] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      // The two reads are independent, so they run together — the digest list
      // must not wait on an edge traversal that fans out to ~30 upstream gets.
      const [digests, current] = await Promise.allSettled([
        fetch("/api/sessions?limit=400").then(async (res) => {
          const body: SessionsPayload = await res.json();
          if (!res.ok || body.error) throw new Error(body.error ?? `HTTP ${res.status}`);
          return body;
        }),
        fetch("/api/conversation").then(async (res) => {
          const body: ConversationPayload = await res.json();
          if (!res.ok || body.error) throw new Error(body.error ?? `HTTP ${res.status}`);
          return body;
        }),
      ]);

      if (!alive) return;

      if (digests.status === "fulfilled") {
        const rows = digests.value.entities as SessionEntity[];
        setSessions(rows.map(parseSession).sort(byRecency));
        // Coverage is measured against what UPSTREAM returned, before any
        // client-side sort or filter, so it describes the query rather than
        // whatever the table happens to be showing.
        setCoverage(coverageOf(digests.value, rows.length));
        setLive(digests.value.live ?? null);
        setDigestFailed(false);
      } else {
        // Keep whatever sessions are already on screen: stale rows the operator
        // can still read beat blanking the index on one slow poll. The banner
        // says the refresh failed, so nothing on screen is claimed as current.
        setDigestFailed(true);
      }
      if (current.status === "fulfilled") {
        const row = current.value.conversation ?? null;
        setConversation(row ? parseConversation(row) : null);
        setRelated(current.value.related ?? []);
        // The conversation route carries the same live hint; prefer it, since
        // it is the one the lookup actually used.
        if (current.value.live) setLive(current.value.live as LiveSessionHint);
      }

      // Only a failure of BOTH is a failure of the view — either alone still
      // leaves the operator with something true on screen.
      const failed = [digests, current].filter((r) => r.status === "rejected");
      setError(
        failed.length === 2 ? ((failed[0] as PromiseRejectedResult).reason as Error).message : null,
      );
      setFirstLoadDone(true);
    };
    void load();
    // Same 10s cadence as the task poll. `firstLoadDone` latches true, so the
    // skeleton never comes back over data the operator is reading.
    const id = setInterval(() => void load(), REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  /**
   * Does the live session have a digest? Almost certainly not — digests are
   * written after the fact, and none has been written since 28 August — but
   * this resolves it rather than assuming.
   */
  const liveDigest = useMemo(
    () => (live ? (sessions.find((s) => s.sessionId === live.sessionId) ?? null) : null),
    [sessions, live],
  );

  const pending = showSkeleton(!firstLoadDone, sessions.length > 0);

  if (error && !sessions.length) {
    return (
      <div className="my-4 rounded-lg border border-[hsl(var(--bad)/0.35)] bg-[hsl(var(--bad)/0.12)] px-3 py-[10px] text-[13px]">
        <strong>Cannot load sessions.</strong> {error}
      </div>
    );
  }

  // One specific session's digest, opened from the index.
  if (selected) {
    if (pending) return <SessionDetailSkeleton />;
    const session = sessions.find((s) => s.id === selected);
    if (!session) {
      return (
        <article>
          <BackToIndex onSelect={onSelect} />
          {/* A missing row after a FAILED read is not a missing session. */}
          {digestFailed ? (
            <p className="text-[13px] text-muted-foreground">
              The session list could not be loaded, so this digest could not be found in it. It may
              well exist — retrying on the next poll.
            </p>
          ) : (
            <p className="text-[13px] text-muted-foreground">
              No session digest with id <code>{selected}</code>.
            </p>
          )}
        </article>
      );
    }
    return (
      <SessionDetail session={session} onBack={() => onSelect(null)} onOpenEntity={onOpenEntity} />
    );
  }

  // THE INDEX — `#/sessions`.
  if (view === "index") {
    if (pending) return <SessionListSkeleton />;
    return (
      <SessionList
        sessions={sessions}
        coverage={coverage}
        live={live}
        failed={digestFailed}
        onSelect={onSelect}
        onOpenCurrent={onOpenCurrent}
      />
    );
  }

  // ROOT — the current session.
  if (pending) return <SessionDetailSkeleton />;
  return (
    <CurrentSession
      conversation={conversation}
      related={related}
      live={live}
      digest={liveDigest}
      fallback={sessions[0] ?? null}
      coverage={coverage}
      failed={digestFailed}
      onOpenIndex={() => onSelect(null)}
      onOpenEntity={onOpenEntity}
      onOpenAgent={onOpenAgent}
    />
  );
}

function BackToIndex({ onSelect }: { onSelect: (id: string | null) => void }) {
  return (
    <Button variant="outline" size="sm" className="mb-[8px]" onClick={() => onSelect(null)}>
      <ArrowLeft className="mr-1 h-[13px] w-[13px]" />
      All sessions
    </Button>
  );
}

/**
 * THE LANDING VIEW — the session the operator is in right now.
 *
 * Sourced from Neotoma: the `conversation` entity whose `conversation_id` is
 * this harness session's uuid. When it resolves, the title, topics, and scope
 * summary are stored fields, and the work is the conversation's own outbound
 * edges — no filesystem heuristic is involved and none is claimed on screen.
 *
 * Two fallbacks, each labelled for what it is:
 *   - a session id but no matching `conversation` entity — the pre-convention
 *     case, where nothing has been recorded about this session yet;
 *   - no session id at all — no transcript for this checkout, so the newest
 *     DIGESTED session is shown instead of the current one.
 */
function CurrentSession({
  conversation,
  related,
  live,
  digest,
  fallback,
  coverage,
  failed,
  onOpenIndex,
  onOpenEntity,
  onOpenAgent,
}: {
  conversation: Conversation | null;
  related: RelatedEntity[];
  live: LiveSessionHint | null;
  digest: SessionDigest | null;
  fallback: SessionDigest | null;
  coverage: ListCoverage;
  /** The digest read failed on the last poll — the count is then not a count. */
  failed: boolean;
  onOpenIndex: () => void;
  onOpenEntity: (id: string) => void;
  onOpenAgent: (id: string) => void;
}) {
  // No session id could be determined: show the most recent DIGESTED session,
  // clearly labelled as a fallback rather than presented as "current".
  if (!live) {
    return (
      <article>
        <ListLink coverage={coverage} failed={failed} onOpenIndex={onOpenIndex} />
        <div className="mb-[18px] rounded-[10px] border border-[hsl(var(--warn)/0.26)] bg-[hsl(var(--warn)/0.08)] px-[14px] py-3 text-[13px]">
          <strong>Showing the most recent session, not the current one.</strong> No Claude Code
          transcript was found for this checkout, so there is no session id to look up a{" "}
          <code>conversation</code> entity by.
        </div>
        {fallback ? (
          <SessionDetail session={fallback} embedded onOpenEntity={onOpenEntity} />
        ) : (
          <p className="text-[13px] text-muted-foreground">No session digests to show.</p>
        )}
      </article>
    );
  }

  return (
    <article>
      <ListLink coverage={coverage} failed={failed} onOpenIndex={onOpenIndex} />

      <header className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h2 className="m-0 text-[19px] font-[650] tracking-[-0.01em]">
            {conversation?.title ?? digest?.title ?? "Current session"}
          </h2>
          <p className="mt-[5px] text-[13px] text-muted-foreground">
            <code className="text-[12px]">{live.sessionId.slice(0, 8)}</code> · active{" "}
            {relativeTime(new Date(live.mtime))}
          </p>
          {/* HOW SURE ARE WE THIS IS THE CURRENT SESSION?
              Stated on screen rather than left implicit, because the answer is
              "very, but not certainly". The id is the most recently written
              transcript for this worktree; when a `conversation` entity matches
              it the join is exact and the identification is stored data. The
              one ambiguity is two sessions open on the same worktree, where the
              most recently active wins — so it is named.
              NOT used: `is_running`, which is absent on 99.9% of conversation
              entities and true on none of the 2,600 measured, so it would
              identify no session at all. */}
          <p className="mt-[3px] text-[11px] leading-[1.45] text-muted-foreground/80">
            {conversation ? (
              <>
                Matched to a stored <code>conversation</code> by <code>conversation_id</code> — an
                exact join.
              </>
            ) : (
              <>Identified from the {live.basis}.</>
            )}{" "}
            If two sessions are open on this worktree, the most recently active one is shown.
          </p>
        </div>
        <div className="flex flex-none items-center gap-[10px]">
          {conversation && (
            <button
              type="button"
              onClick={() => onOpenEntity(conversation.id)}
              className="flex items-center gap-1 rounded-[6px] px-[6px] py-[2px] text-[12px] text-muted-foreground hover:bg-accent hover:text-foreground"
            >
              conversation
            </button>
          )}
          <Badge variant="live">live</Badge>
        </div>
      </header>

      {conversation ? (
        <ConversationBody
          conversation={conversation}
          related={related}
          onOpenEntity={onOpenEntity}
          onOpenAgent={onOpenAgent}
        />
      ) : (
        <NoConversationYet live={live} />
      )}

      {digest && (
        <>
          <Separator className="my-[10px]" />
          <h3 className="mb-[3px] text-[10px] font-[650] uppercase tracking-[0.06em] text-muted-foreground">
            Session digest
          </h3>
          <SessionBody session={digest} onOpenEntity={onOpenEntity} />
        </>
      )}
    </article>
  );
}

/**
 * THE SESSION, AS ONE VIEW.
 *
 * This page answers five questions, in this order, and each fact appears in
 * exactly ONE of them:
 *
 *   1. What is this session working on?  — the header, facts strip, topics,
 *      scope summary, and the plan it runs under.
 *   2. What needs the operator?          — `Needs you`, the only place ownership
 *      is stated as a headline rather than a column value.
 *   3. What is in flight vs done?        — the task table's `Status` column and
 *      the open/all filter's counts.
 *   4. What is blocked, and on what?     — the `Owner` column's `blocked on N`,
 *      resolved against this session's own entities.
 *   5. What did it produce?              — `Produced`, the non-task outputs.
 *
 * WHAT WAS CONSOLIDATED, and why. Six incremental additions had each grown its
 * own section, so the same task rendered TWICE — once under a topic group and
 * again under a "Referenced entities > Tasks" group — with its ownership state
 * shown as a trailing badge in one and inferable from the status badge in the
 * other. There is now ONE task table. Topic is a group heading inside it; the
 * old per-type list keeps only the types that are NOT tasks, since those are
 * outputs rather than work and are the only rows the type heading still
 * distinguishes.
 *
 * The per-section explanatory prose is gone with it. Those notes explained a
 * mechanism (why grouping is by topic, what the status filter hides, that a
 * rendered page needs a signed-in session) that the counts and column headers
 * now carry directly. The two notes that state an INFERENCE rather than a
 * mechanism survive, because a reader cannot recover them from what is on
 * screen: the plan-collapse note and the current-session identification basis.
 */
function ConversationBody({
  conversation,
  related,
  onOpenEntity,
  onOpenAgent,
}: {
  conversation: Conversation;
  related: RelatedEntity[];
  onOpenEntity: (id: string) => void;
  onOpenAgent: (id: string) => void;
}) {
  /**
   * OPEN BY DEFAULT. A third of this session's referenced entities are
   * finished, and a list that opens on completed work buries what is still
   * live. The count of what is hidden rides in the filter control, so the
   * filter is never silently subtractive.
   */
  const [showAll, setShowAll] = useState(false);
  const roster = useRoster();

  const closedCount = useMemo(
    () => related.filter((e) => isClosedStatus(relatedStatus(e))).length,
    [related],
  );
  const visible = useMemo(
    () => (showAll ? related : related.filter(isOpenEntity)),
    [related, showAll],
  );

  const plans = useMemo(() => sessionPlans(related), [related]);

  /**
   * Is a blocker still open? Resolved against the session's OWN entities, and a
   * dependency on a task outside this session is treated as still blocking — an
   * unknown blocker is not a cleared one.
   */
  const isBlockerOpen = useMemo(() => {
    const byId = new Map(related.map((e) => [e.entity_id, e]));
    return (id: string) => {
      const hit = byId.get(id);
      return hit ? isOpenEntity(hit) : true;
    };
  }, [related]);

  const spawnable = (assignedTo: string | null) =>
    dispatchability(assignedTo).kind === "dispatchable";

  /** Every task, grouped by topic where topics partition anything. */
  const grouping = useMemo(() => {
    const tasks = visible.filter((e) => e.entity_type === "task");
    const titleOf = (id: string) => {
      const hit = related.find((e) => e.entity_id === id);
      return hit ? relatedTitle(hit) : null;
    };
    return groupTasks(tasks, titleOf);
  }, [visible, related]);

  /**
   * WHAT NEEDS THE OPERATOR. Ownership is computed once, here, and the two
   * states that are a call on the operator's time — a task nothing owns, and a
   * task owned by a role Apis cannot spawn — are counted for the callout. The
   * per-row `Owner` column reads from this same classification, so the callout
   * and the table can never disagree.
   */
  const ownerships = useMemo(() => {
    const map = new Map<string, Ownership>();
    for (const e of visible) {
      if (e.entity_type !== "task") continue;
      map.set(e.entity_id, ownership(e, isBlockerOpen, spawnable));
    }
    return map;
  }, [visible, isBlockerOpen]);

  const attention = useMemo(() => {
    let unowned = 0;
    let unspawnable = 0;
    let blocked = 0;
    for (const state of ownerships.values()) {
      if (state.kind === "inert") unowned += 1;
      else if (state.kind === "owned" && !state.spawnable) unspawnable += 1;
      else if (state.kind === "blocked") blocked += 1;
    }
    return { unowned, unspawnable, blocked };
  }, [ownerships]);

  /**
   * The session's OUTPUTS: everything referenced that is not a task. Reports,
   * templates, and anything else it produced or worked from. Plans are excluded
   * — they head their own line above, as the thing the session runs UNDER
   * rather than something it made.
   */
  const outputs = useMemo(
    () => visible.filter((e) => e.entity_type !== "task" && e.entity_type !== "plan"),
    [visible],
  );

  const taskCount = grouping.groups.reduce((n, g) => n + g.entities.length, 0);

  return (
    <>
      <dl className="my-[8px] flex flex-wrap gap-x-[18px] gap-y-[3px] rounded-[7px] border bg-card px-[10px] py-[6px]">
        {conversation.startTimestamp && (
          <Fact label="Started">{shortDate(conversation.startTimestamp)}</Fact>
        )}
        {conversation.lastUpdated && (
          <Fact label="Last updated">{relativeTime(new Date(conversation.lastUpdated))}</Fact>
        )}
        {conversation.harness && <Fact label="Harness">{conversation.harness}</Fact>}
        {conversation.repositoryName && (
          <Fact label="Repository">{conversation.repositoryName}</Fact>
        )}
        {conversation.status && <Fact label="Status">{conversation.status}</Fact>}
        {/* The plan the session runs under, inline: one plan is a real answer to
            "which plan am I on", and it did not need a section of its own. */}
        {plans.length > 0 && (
          <Fact label={plans.length > 1 ? "Plans" : "Plan"}>
            <span className="flex flex-wrap items-baseline gap-x-[8px]">
              {plans.map((p) => (
                <button
                  key={p.entity_id}
                  type="button"
                  onClick={() => onOpenEntity(p.entity_id)}
                  className="cursor-pointer border-none bg-transparent p-0 text-[12px] hover:underline"
                >
                  {relatedTitle(p)}
                </button>
              ))}
            </span>
          </Fact>
        )}
      </dl>

      {conversation.topics.length > 0 && (
        <div className="my-[8px] flex flex-wrap gap-[4px]">
          {conversation.topics.map((t) => (
            <Badge key={t} variant="muted">
              {t}
            </Badge>
          ))}
        </div>
      )}

      {conversation.scopeSummary && (
        <ClampedProse label="Scope summary" source={conversation.scopeSummary} />
      )}

      <NeedsYou {...attention} />

      {related.length === 0 ? (
        <p className="my-[18px] text-[13px] text-muted-foreground">
          This <code>conversation</code> has no outbound relationships yet, so there is nothing to
          list as the session's work.
        </p>
      ) : (
        <>
          <TaskTable
            grouping={grouping}
            ownerships={ownerships}
            roster={roster}
            taskCount={taskCount}
            showAll={showAll}
            onToggle={() => setShowAll((v) => !v)}
            shown={visible.length}
            total={related.length}
            closed={closedCount}
            onOpenEntity={onOpenEntity}
            onOpenAgent={onOpenAgent}
          />
          <Produced outputs={outputs} onOpenEntity={onOpenEntity} />
        </>
      )}
    </>
  );
}

/**
 * WHAT NEEDS THE OPERATOR — the one question the page previously made him
 * count for himself.
 *
 * The three ownership states were only ever visible as per-row badges, so
 * answering "what needs me" meant scanning 50 rows. Each number here is a
 * headline for a state whose per-row rendering is unchanged; nothing is stated
 * twice, because the row shows WHICH task and this shows HOW MANY.
 *
 * `blocked` is counted but deliberately worded as orderly rather than as a
 * defect: a task waiting on a dependency is the system working.
 */
function NeedsYou({
  unowned,
  unspawnable,
  blocked,
}: {
  unowned: number;
  unspawnable: number;
  blocked: number;
}) {
  if (unowned === 0 && unspawnable === 0 && blocked === 0) return null;

  return (
    <section className="my-[10px] flex flex-wrap items-baseline gap-x-[16px] gap-y-[2px] rounded-[7px] border border-[hsl(var(--warn)/0.26)] bg-[hsl(var(--warn)/0.08)] px-[10px] py-[6px] text-[12px]">
      <h3 className="m-0 text-[10px] font-[650] uppercase tracking-[0.06em] text-muted-foreground">
        Needs you
      </h3>
      {unowned > 0 && (
        <span title="assigned_to is empty and no unmet DEPENDS_ON — filed, and nothing will pick it up">
          <strong className="tabular-nums text-warn">{unowned}</strong> undispatched
        </span>
      )}
      {unspawnable > 0 && (
        <span
          title={`Owned by a role outside Apis's route table (${DISPATCHABLE_ROLES.join(", ")}), so nothing will pick it up`}
        >
          <strong className="tabular-nums text-bad">{unspawnable}</strong> owned but unspawnable
        </span>
      )}
      {blocked > 0 && (
        <span className="text-muted-foreground" title="Waiting on an unfinished DEPENDS_ON target">
          <strong className="tabular-nums">{blocked}</strong> waiting on a dependency
        </span>
      )}
    </section>
  );
}

/**
 * THE TASKS — one table, replacing the two lists that each rendered every task.
 *
 * Three columns, each carrying a fact shown nowhere else on this page:
 *   STATUS — the RAW stored value (`pending`, `todo`, `open`, `done` are all
 *     real in this data and are never collapsed into one another). Only the
 *     colour uses the bucket mapping.
 *   TASK   — the title. The entity TYPE is not repeated per row: every row in
 *     this table is a `task`, which the heading already says.
 *   OWNER  — the derived ownership state. `status` says where the task is;
 *     this says whether anything will move it.
 *
 * Topic is a group heading rather than a column, so the 22-task topic costs one
 * line instead of 22 repetitions of its name.
 */
function TaskTable({
  grouping,
  ownerships,
  roster,
  taskCount,
  showAll,
  onToggle,
  shown,
  total,
  closed,
  onOpenEntity,
  onOpenAgent,
}: {
  grouping: Grouping;
  ownerships: Map<string, Ownership>;
  roster: AgentRef[];
  taskCount: number;
  showAll: boolean;
  onToggle: () => void;
  shown: number;
  total: number;
  closed: number;
  onOpenEntity: (id: string) => void;
  onOpenAgent: (id: string) => void;
}) {
  return (
    <section className="my-[10px]">
      <div className="mb-[3px] flex flex-wrap items-baseline gap-x-[10px] gap-y-[2px]">
        <h3 className="m-0 text-[10px] font-[650] uppercase tracking-[0.06em] text-muted-foreground">
          Tasks <span className="font-[450] tabular-nums">({taskCount})</span>
        </h3>
        <StatusFilter
          showAll={showAll}
          onToggle={onToggle}
          shown={shown}
          total={total}
          closed={closed}
        />
        {/* THE ONE SURVIVING GROUPING NOTE. It states an INFERENCE the reader
            cannot recover from the screen — that grouping by plan was computed,
            collapsed, and replaced — rather than restating a visible mechanism.
            Reduced from a paragraph to a clause. */}
        {grouping.basis === "topic" && grouping.planCollapsed && (
          <span className="text-[11px] text-muted-foreground">
            by topic — all{" "}
            {grouping.planCount === 1 ? <>tasks share one plan</> : <>tasks carry no plan</>}, so
            plan grouping separates nothing
          </span>
        )}
      </div>

      {taskCount === 0 ? (
        <p className="m-0 text-[13px] text-muted-foreground">
          {closed > 0 ? (
            <>
              Every task is finished. {closed} closed{" "}
              {closed === 1 ? "entity is" : "entities are"} hidden — show all to read them.
            </>
          ) : (
            <>This session references no tasks.</>
          )}
        </p>
      ) : (
        <table className="w-full border-collapse text-[13px] leading-[1.4]">
          <thead>
            <tr className="border-b text-[10px] uppercase tracking-[.06em] text-muted-foreground">
              <th className="w-[104px] py-[3px] pr-2 text-left font-[600]">Status</th>
              <th className="py-[3px] pr-2 text-left font-[600]">Task</th>
              <th className="w-[150px] py-[3px] text-right font-[600]">Owner</th>
            </tr>
          </thead>
          <tbody>
            {grouping.groups.map((g) => (
              <TopicRows
                key={g.id ?? "__ungrouped__"}
                group={g}
                grouped={grouping.basis === "topic"}
                ownerships={ownerships}
                roster={roster}
                onOpenEntity={onOpenEntity}
                onOpenAgent={onOpenAgent}
              />
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

/**
 * One topic's tasks, as a heading row followed by its task rows.
 *
 * The heading row is suppressed when topics did not partition anything
 * (`basis: "none"`): a single group under a heading that promises grouping is a
 * worse lie than a flat list.
 */
function TopicRows({
  group,
  grouped,
  ownerships,
  roster,
  onOpenEntity,
  onOpenAgent,
}: {
  group: TaskGroup;
  grouped: boolean;
  ownerships: Map<string, Ownership>;
  roster: AgentRef[];
  onOpenEntity: (id: string) => void;
  onOpenAgent: (id: string) => void;
}) {
  return (
    <>
      {grouped && (
        <tr className="border-b border-border/60">
          <th colSpan={3} className="pb-[2px] pt-[9px] text-left text-[12px] font-[650]">
            {group.id ? (
              <button
                type="button"
                onClick={() => onOpenEntity(group.id as string)}
                className="cursor-pointer border-none bg-transparent p-0 text-[12px] font-[650] hover:underline"
              >
                {group.label}
              </button>
            ) : (
              <span className="text-muted-foreground">{group.label}</span>
            )}{" "}
            <span className="font-[450] tabular-nums text-muted-foreground">
              ({group.entities.length})
            </span>
          </th>
        </tr>
      )}
      {group.entities.map((e) => (
        <TaskRow
          key={`${group.id ?? "u"}:${e.entity_id}`}
          entity={e}
          state={ownerships.get(e.entity_id) ?? { kind: "unknown" }}
          roster={roster}
          onOpenEntity={onOpenEntity}
          onOpenAgent={onOpenAgent}
        />
      ))}
    </>
  );
}

/**
 * One task: raw status, title, derived ownership.
 *
 * The stored status is never replaced — `pending` still reads `pending`. The
 * owner column says what `pending` means for THIS task, which the word alone
 * does not: undispatched, owned, or waiting on a dependency.
 */
function TaskRow({
  entity,
  state,
  roster,
  onOpenEntity,
  onOpenAgent,
}: {
  entity: RelatedEntity;
  state: Ownership;
  roster: AgentRef[];
  onOpenEntity: (id: string) => void;
  onOpenAgent: (id: string) => void;
}) {
  const status = relatedStatus(entity);

  return (
    <tr
      onClick={() => onOpenEntity(entity.entity_id)}
      className="cursor-pointer border-b border-border/60 hover:bg-accent/60"
    >
      <td className="py-[3px] pr-2 align-baseline">
        {status && <Badge variant={statusTone(status)}>{status}</Badge>}
      </td>
      <td className="max-w-0 py-[3px] pr-2 align-baseline">
        <span className="block truncate">{relatedTitle(entity)}</span>
      </td>
      <td className="py-[3px] text-right align-baseline text-[11.5px]">
        <OwnershipCell state={state} roster={roster} onOpenAgent={onOpenAgent} />
      </td>
    </tr>
  );
}

/**
 * THE DERIVED STATE, in the Owner column.
 *
 * Colour carries the finding, matching the detail view's State panel: amber for
 * an undispatched task nothing will pick up, red for an owner Apis cannot
 * spawn, green for one it can, muted for a dependency wait (which is orderly,
 * not a defect) and for the not-yet-read case.
 */
function OwnershipCell({
  state,
  roster,
  onOpenAgent,
}: {
  state: Ownership;
  roster: AgentRef[];
  onOpenAgent: (id: string) => void;
}) {
  switch (state.kind) {
    case "unknown":
      return (
        <span className="text-muted-foreground" title="Edges not read yet">
          …
        </span>
      );
    case "blocked":
      return (
        <span
          className="text-muted-foreground"
          title={`Waiting on ${state.blockers.length} unfinished DEPENDS_ON target(s): ${state.blockers.join(", ")}`}
        >
          blocked on {state.blockers.length}
        </span>
      );
    case "owned":
      // The shared renderer, so the owner links to its agent page and an
      // unspawnable role is flagged identically to every other surface.
      return (
        <AssignedTo
          assignedTo={state.owner}
          agents={roster}
          onOpenAgent={onOpenAgent}
          className="text-[11.5px]"
        />
      );
    case "inert":
      return (
        <span
          className="text-warn"
          title="No assigned_to and no unmet DEPENDS_ON — filed, and nothing is working on it"
        >
          undispatched
        </span>
      );
  }
}

/**
 * WHAT THE SESSION PRODUCED — the non-task entities it references.
 *
 * This is what remains of the old "Referenced entities" section once tasks
 * moved into the table above. Its type headings now earn their place: these
 * rows genuinely differ by type (a rendered report is not a template), which
 * was never true of the task rows the headings used to label.
 *
 * The `rendered_page` auth note is gone. It explained that these links need a
 * signed-in Neotoma session — which stopped being true of THIS surface when the
 * rows started opening the in-app sheet instead of leaving for Neotoma. The
 * caveat still belongs where the external link actually is, in the sheet.
 */
function Produced({
  outputs,
  onOpenEntity,
}: {
  outputs: RelatedEntity[];
  onOpenEntity: (id: string) => void;
}) {
  const groups = useMemo(() => groupRelated(outputs), [outputs]);
  if (groups.length === 0) return null;

  return (
    <section className="my-[10px]">
      <h3 className="mb-[3px] text-[10px] font-[650] uppercase tracking-[0.06em] text-muted-foreground">
        Produced <span className="font-[450] tabular-nums">({outputs.length})</span>
      </h3>
      <table className="w-full border-collapse text-[13px] leading-[1.4]">
        <tbody>
          {groups.map((g) => (
            <Fragment key={g.key}>
              <tr className="border-b border-border/60">
                <th colSpan={2} className="pb-[2px] pt-[9px] text-left text-[12px] font-[650]">
                  {g.label}{" "}
                  <span className="font-[450] tabular-nums text-muted-foreground">
                    ({g.entities.length})
                  </span>
                </th>
              </tr>
              {g.entities.map((e) => {
                const status = relatedStatus(e);
                return (
                  <tr
                    key={e.entity_id}
                    onClick={() => onOpenEntity(e.entity_id)}
                    className="cursor-pointer border-b border-border/60 hover:bg-accent/60"
                  >
                    <td className="w-[104px] py-[3px] pr-2 align-baseline">
                      {status && <Badge variant={statusTone(status)}>{status}</Badge>}
                    </td>
                    <td className="max-w-0 py-[3px] align-baseline">
                      <span className="block truncate">{relatedTitle(e)}</span>
                    </td>
                  </tr>
                );
              })}
            </Fragment>
          ))}
        </tbody>
      </table>
    </section>
  );
}

/**
 * THE OPEN/ALL TOGGLE, with the hidden count beside it.
 *
 * One click either way, and the number of closed entities is stated whether or
 * not the filter is on — a filter whose effect the operator has to infer from a
 * shrinking list is worse than no filter. This is now the ONLY place the
 * filter's behaviour is described; the paragraph that restated it under the
 * heading is gone.
 */
function StatusFilter({
  showAll,
  onToggle,
  shown,
  total,
  closed,
}: {
  showAll: boolean;
  onToggle: () => void;
  shown: number;
  total: number;
  closed: number;
}) {
  return (
    <span className="flex items-baseline gap-[8px] text-[11px] text-muted-foreground">
      <button
        type="button"
        onClick={onToggle}
        aria-pressed={showAll}
        className="cursor-pointer rounded-[5px] border px-[6px] py-[1px] text-[11px] hover:bg-accent hover:text-foreground"
        title={
          showAll
            ? "Showing every referenced entity regardless of status"
            : "Hiding only statuses recognized as finished; an unfamiliar status stays visible"
        }
      >
        {showAll ? "Open only" : "Show all"}
      </button>
      <span className="tabular-nums">
        {shown} of {total}
        {closed > 0 && <> · {closed} closed</>}
      </span>
    </span>
  );
}

/** Colour for a raw `status`, via the same bucket mapping the task view uses. */
function statusTone(status: string): "ok" | "bad" | "warn" | "live" | "muted" {
  switch (toBucket(status)) {
    case "done":
      return "ok";
    case "blocked":
      return "bad";
    case "in_progress":
      return "live";
    case "pending":
      return "warn";
    default:
      return "muted";
  }
}

/**
 * A session id resolved, but no `conversation` entity carries it.
 *
 * Stating this precisely matters: the session is not unrecorded because it did
 * nothing, but because nothing has been written about it yet. Rendering an
 * empty list here would assert the former.
 */
function NoConversationYet({ live }: { live: LiveSessionHint }) {
  return (
    <div className="my-[18px] rounded-[10px] border border-[hsl(var(--warn)/0.26)] bg-[hsl(var(--warn)/0.08)] px-[14px] py-3">
      <p className="m-0 text-[13px] font-[550]">No conversation entity for this session</p>
      <p className="m-0 mt-[6px] text-[12px] leading-[1.55] text-muted-foreground">
        No <code>conversation</code> entity has <code>conversation_id</code>{" "}
        <code>{live.sessionId}</code>, so nothing about this session is stored yet and its work
        cannot be listed. The session id itself comes from the {live.basis} — if two sessions are
        open on this worktree, the most recently active one is shown.
      </p>
    </div>
  );
}


/**
 * The link from root out to the index.
 *
 * The count is suppressed when the digest read failed: `sessions.length` is 0
 * on a failed fetch, and "0 digested" is a fabricated measurement of exactly
 * the kind this dashboard exists to catch.
 */
function ListLink({
  coverage,
  failed,
  onOpenIndex,
}: {
  coverage: ListCoverage;
  failed: boolean;
  onOpenIndex: () => void;
}) {
  return (
    <div className="mb-[8px] flex items-center gap-2">
      <Button variant="outline" size="sm" onClick={onOpenIndex}>
        All sessions
      </Button>
      {/* "344 digested" was `sessions.length` — the request's page size, not the
          store's total. It goes through `Coverage` now, so once digests pass
          the 400-row limit this reads "400 of N" instead of quietly capping. */}
      <CoverageCount coverage={coverage} noun="digest" failed={failed} />
    </div>
  );
}

/**
 * THE INDEX — every session, newest first, filterable down to one.
 *
 * WHAT EACH ROW CARRIES, and why: when it ran, how long for, what it was about,
 * where it ran, how much it claimed. Those are the five things that let an
 * operator pick a session out of 344 without opening any of them.
 *
 * NAME vs PATH. The name column shows the DERIVED name (see `displayName`) and
 * the worktree beneath it as secondary context. A name that had to be derived
 * carries a marker, so a reader can always tell a stored title from one this
 * app assembled — the app is read-only and must not launder a guess into data.
 *
 * WHAT IS NOT HERE, deliberately: archived state and harness sidebar groups.
 * Those fields (`is_archived`, `sidebar_group_name`, `is_running`) live on the
 * `conversation` entity, NOT on `session_digest`, and this index is built from
 * digests — `/api/sessions` queries `session_digest` and nothing else. Adding
 * an "Unarchived" filter here would silently filter on a field none of these
 * rows have, which is worse than not offering it. See the note rendered under
 * the filters, and the report accompanying this change.
 *
 * FAILURE IS NOT EMPTINESS. `failed` is threaded in so that a digest read which
 * timed out renders as a failure, never as "no sessions" and never as a search
 * that matched nothing. Neotoma is answering in 25-81s (ateles#576), so this
 * path is routine.
 */
function SessionList({
  sessions,
  coverage,
  live,
  failed,
  onSelect,
  onOpenCurrent,
}: {
  sessions: SessionDigest[];
  coverage: ListCoverage;
  live: LiveSessionHint | null;
  failed: boolean;
  onSelect: (id: string | null) => void;
  onOpenCurrent: () => void;
}) {
  const [query, setQuery] = useState("");
  /** Which harness the operator narrowed to, or "all". Stored field, so real. */
  const [harness, setHarness] = useState("all");

  /** Harness values actually present, so the filter never offers an empty set. */
  const harnesses = useMemo(() => {
    const seen = new Map<string, number>();
    for (const s of sessions) {
      if (!s.harness) continue;
      seen.set(s.harness, (seen.get(s.harness) ?? 0) + 1);
    }
    return [...seen.entries()].sort((a, b) => b[1] - a[1]);
  }, [sessions]);

  const filtered = useMemo(
    () =>
      sessions.filter(
        (s) => (harness === "all" || s.harness === harness) && matchesQuery(s, query),
      ),
    [sessions, harness, query],
  );

  const narrowed = query.trim().length > 0 || harness !== "all";

  return (
    <>
      <div className="mb-[8px] flex flex-wrap items-center gap-2">
        <Button variant="outline" size="sm" onClick={onOpenCurrent}>
          <ArrowLeft className="mr-1 h-[13px] w-[13px]" />
          Current session
        </Button>

        {/* Client-side over the already-loaded set: a per-keystroke round trip
            against a 25-81s backend would be unusable. */}
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search name, topic, repo, date…"
          aria-label="Search sessions"
          className="h-[26px] min-w-[220px] flex-1 rounded-[6px] border bg-background px-[8px] text-[12.5px] outline-none placeholder:text-muted-foreground focus:border-[hsl(var(--live)/0.5)]"
        />

        {harnesses.length > 1 && (
          <select
            value={harness}
            onChange={(e) => setHarness(e.target.value)}
            aria-label="Filter by harness"
            className="h-[26px] rounded-[6px] border bg-background px-[6px] text-[12.5px] outline-none"
          >
            <option value="all">All harnesses</option>
            {harnesses.map(([h, n]) => (
              <option key={h} value={h}>
                {h} ({n})
              </option>
            ))}
          </select>
        )}

        {/*
         * THREE DIFFERENT DENOMINATORS, and conflating any two of them states a
         * cap as a count:
         *
         *   filtered.length — rows surviving the search box
         *   sessions.length — rows this request LOADED
         *   coverage total  — digests that exist
         *
         * Narrowed, the honest phrasing is "N of the M loaded", because the
         * search ran over the loaded rows only and cannot speak for the rest.
         * Unnarrowed, the figure is the store's total via `Coverage` — which is
         * what "344 sessions" was pretending to be while meaning page size.
         */}
        {failed ? (
          <span className="text-[12px] tabular-nums text-muted-foreground">
            count unavailable
          </span>
        ) : narrowed ? (
          <span className="text-[12px] tabular-nums text-muted-foreground">
            {filtered.length} of {sessions.length} loaded
          </span>
        ) : (
          <CoverageCount coverage={coverage} noun="session" />
        )}
      </div>

      {/* The digest read is capped at 400 rows. At 344 stored digests nothing
          renders here; past 400 it is the only thing that would say so. */}
      <CoverageNotice
        coverage={coverage}
        noun="session digest"
        nounPlural="session digests"
        sortNote="Digests are ordered newest-first, so the oldest sessions are the ones missing."
      />
      <Coverage sessions={sessions} live={live} failed={failed} />
      <Verification sessions={sessions} failed={failed} />

      {/* THE THREE EMPTY-LOOKING STATES, kept distinct. A failed fetch, a
          genuinely empty store, and a search that matched nothing look
          identical in a naive table and mean completely different things. */}
      {failed && sessions.length === 0 ? (
        <p className="rounded-[7px] border border-[hsl(var(--bad)/0.35)] bg-[hsl(var(--bad)/0.10)] px-[10px] py-[8px] text-[12.5px]">
          <strong>Could not load sessions.</strong> The read failed — this is not an empty list.
          Retrying every 10s.
        </p>
      ) : sessions.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">
          The read succeeded and returned no <code>session_digest</code> entities.
        </p>
      ) : filtered.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">
          No sessions match{query.trim() && <> “{query.trim()}”</>}
          {harness !== "all" && <> in {harness}</>}. {sessions.length} loaded.
        </p>
      ) : (
        <table className="w-full border-collapse text-[12.5px]">
          <thead>
            <tr className="border-b text-[10px] uppercase tracking-[.06em] text-muted-foreground">
              <th className="w-[62px] py-[4px] pr-2 text-left font-[600]">Ran</th>
              <th className="w-[52px] py-[4px] pr-2 text-right font-[600]">For</th>
              <th className="py-[4px] pr-2 text-left font-[600]">Session</th>
              <th className="w-[108px] py-[4px] pr-2 text-left font-[600]">Method</th>
              {/* "Self-reported" and "Verified" are two different measurements
                  and used to be one column showing only the first. */}
              <th className="w-[74px] py-[4px] text-right font-[600]" title="Claims the session marked complete, out of all it made. Its own word.">
                Self-rep.
              </th>
              <th className="w-[64px] py-[4px] text-right font-[600]" title="Claims checked against a system of record — confirmed, refuted, or unverifiable.">
                Verified
              </th>
              <th className="w-[70px] py-[4px] pl-2 text-right font-[600]">Produced</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((s) => {
              const mix = claimMix(s.claims);
              const isLive = live?.sessionId === s.sessionId;
              return (
                <tr
                  key={s.id}
                  onClick={() => onSelect(s.id)}
                  className="cursor-pointer border-b border-border/60 align-top hover:bg-accent/60"
                >
                  <td className="py-[5px] pr-2 tabular-nums text-muted-foreground">
                    {shortDate(s.spanEnd)}
                  </td>
                  {/* "—" when the span is date-only: a duration that was never
                      measurable must not print as a number. */}
                  <td className="py-[5px] pr-2 text-right tabular-nums text-muted-foreground">
                    {formatDuration(durationHours(s))}
                  </td>
                  <td className="max-w-0 py-[5px] pr-2">
                    <span className="flex items-baseline gap-[6px]">
                      <span className="min-w-0 truncate font-[550]">{s.title}</span>
                      {isLive && (
                        <Badge variant="live" className="flex-none">
                          live
                        </Badge>
                      )}
                      <DerivedNameMark source={s.titleSource} />
                    </span>
                    {/* Secondary context — the path, explicitly demoted. */}
                    {s.worktreeLabel && (
                      <span className="mt-px block truncate text-[11px] text-muted-foreground">
                        {s.worktreeLabel}
                      </span>
                    )}
                  </td>
                  <td className="py-[5px] pr-2 text-muted-foreground">
                    {s.digestMethod ? s.digestMethod.replace(/_/g, " ") : "—"}
                  </td>
                  {/* What the session said about itself. */}
                  <td className="py-[5px] text-right tabular-nums text-muted-foreground">
                    {mix.total > 0 ? `${mix.claimedComplete}/${mix.total}` : "—"}
                  </td>
                  {/* What anyone checked. Zero is the common case and must read
                      as a zero, not as an absence — a blank here would let an
                      entirely unverified session look the same as one with no
                      claims at all. */}
                  <td className="py-[5px] pl-2 text-right tabular-nums">
                    {mix.total === 0 ? (
                      <span className="text-muted-foreground">—</span>
                    ) : mix.confirmed + mix.refuted + mix.unverifiable === 0 ? (
                      <span className="text-muted-foreground/70" title="No claim in this session was ever checked">
                        none
                      </span>
                    ) : (
                      <span className="text-muted-foreground">
                        {mix.confirmed + mix.refuted + mix.unverifiable}/{mix.total}
                      </span>
                    )}
                  </td>
                  <td className="py-[5px] pl-2 text-right tabular-nums text-muted-foreground">
                    {s.artifacts.length > 0 ? s.artifacts.length : "—"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </>
  );
}

/**
 * Marks a name this app DERIVED rather than read from `session_title`.
 *
 * 285 of 344 digests store no title. Deriving one from `topics` makes the index
 * navigable, but an unmarked derived name would present the app's own
 * assembly as stored data. The marker is the same honesty rule the counts
 * follow: show the value, and show that it is not a measurement.
 */
function DerivedNameMark({ source }: { source: NameSource }) {
  if (source === "stored") return null;

  const label: Record<Exclude<NameSource, "stored">, string> = {
    topics: "named from topics — no session_title is stored",
    summary: "named from the summary's first line — no session_title is stored",
    path: "no name stored; this is the worktree path",
    id: "no name stored and nothing to derive one from; this is the session id",
  };

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          className="flex-none cursor-default text-[10px] leading-none text-muted-foreground/70"
          aria-label={label[source]}
        >
          {source === "path" || source === "id" ? "unnamed" : "derived"}
        </span>
      </TooltipTrigger>
      <TooltipContent>{label[source]}</TooltipContent>
    </Tooltip>
  );
}

/**
 * VERIFICATION ACROSS THE WHOLE CORPUS — "who checked?"
 *
 * The per-session badges answer the question one session at a time. This
 * answers it for the corpus, which is where the real finding lives: over the
 * 344 digests loaded here, roughly four claims in five are `intent` — asserted
 * by the session and never checked by anything. A reader looking at a wall of
 * completed-looking sessions should be told that up front.
 *
 * EVERY FIGURE IS COUNTED FROM THE LOADED CLAIMS. Nothing here is a constant
 * and nothing is defaulted: if the digest read fails the panel says so rather
 * than printing a plausible zero, exactly as `Coverage` and `measuredSample()`
 * do. The percentage is computed, so it stays true on any other corpus.
 */
function Verification({ sessions, failed }: { sessions: SessionDigest[]; failed: boolean }) {
  const mix = useMemo(
    () => claimMix(sessions.flatMap((s) => s.claims)),
    [sessions],
  );

  if (failed && sessions.length === 0) {
    return (
      <section className="mb-[10px] rounded-[7px] border border-[hsl(var(--bad)/0.35)] bg-[hsl(var(--bad)/0.10)] px-[10px] py-[7px]">
        <h3 className="m-0 text-[10px] font-[650] uppercase tracking-[0.06em] text-muted-foreground">
          Claim verification
        </h3>
        <p className="m-0 mt-[4px] text-[12px] leading-[1.45]">
          <strong>Not measured.</strong> The digest read failed, so no claim can be counted — this
          is not a report of zero claims.
        </p>
      </section>
    );
  }

  if (mix.total === 0) return null;

  const checked = mix.confirmed + mix.refuted + mix.unverifiable;
  const uncheckedPct = Math.round((mix.unchecked / mix.total) * 100);

  return (
    <section className="mb-[10px] rounded-[7px] border bg-card px-[10px] py-[7px]">
      <h3 className="m-0 text-[10px] font-[650] uppercase tracking-[0.06em] text-muted-foreground">
        Claim verification
      </h3>
      <p className="m-0 mt-[4px] text-[12px] leading-[1.45]">
        These sessions made <strong>{mix.total}</strong> claims about their own work.{" "}
        <strong>
          {mix.unchecked} ({uncheckedPct}%)
        </strong>{" "}
        were never checked against a system of record. A claim is a session's own account of what
        it did — not a <code>task</code> entity, and not evidence the work happened.
      </p>
      <p className="m-0 mt-[4px] text-[11px] leading-[1.45] text-muted-foreground">
        {checked === 0 ? (
          <>Nothing in this corpus has been verified at all.</>
        ) : (
          <>
            Of the {checked} that were checked, {mix.confirmed} held up and{" "}
            <strong>{mix.refuted}</strong> turned out to be false — so the unchecked majority
            cannot be assumed true.
          </>
        )}{" "}
        Only <code>/verify-work</code> upgrades a claim past <code>intent</code>.
      </p>

      {/* One bar, proportional to the real counts — the unchecked share should
          be visible as a share, not only readable as a number. */}
      <div className="mt-[6px] flex h-[6px] overflow-hidden rounded-[3px] bg-muted-foreground/[.18]">
        {(
          [
            { key: "confirmed", n: mix.confirmed, cls: "bg-ok/70" },
            { key: "refuted", n: mix.refuted, cls: "bg-bad/70" },
            { key: "unverifiable", n: mix.unverifiable, cls: "bg-warn/70" },
            { key: "unchecked", n: mix.unchecked, cls: "bg-muted-foreground/40" },
          ] as const
        )
          .filter((seg) => seg.n > 0)
          .map((seg) => (
            <div
              key={seg.key}
              className={seg.cls}
              title={`${seg.n} ${seg.key}`}
              style={{ width: `${(seg.n / mix.total) * 100}%` }}
            />
          ))}
      </div>

      {/* Every stored state, counted — including any the schema does not
          declare. `narrative` occurs on this corpus and is not in the declared
          value set; showing it rather than folding it away is the point. */}
      <p className="m-0 mt-[5px] flex flex-wrap gap-x-[10px] gap-y-0 text-[11px] tabular-nums text-muted-foreground">
        {mix.states.map((s) => (
          <span key={s.state}>
            {s.state} {s.count}
          </span>
        ))}
      </p>
    </section>
  );
}

/**
 * THE COVERAGE GAP — the most valuable thing on this screen.
 *
 * 344 digests look like diligent record-keeping until you plot when they were
 * WRITTEN: 307 of them on two days in a retroactive sweep, and none since. The
 * histogram bars are sized from real counts and the gap is stated in words too,
 * because a viewer should not have to infer it from bar heights.
 *
 * CSS-only: bar heights are inline percentages, no chart library and no script.
 */
function Coverage({
  sessions,
  live,
  failed,
}: {
  sessions: SessionDigest[];
  live: LiveSessionHint | null;
  /** The digest read itself failed — NOT the same as there being no digests. */
  failed: boolean;
}) {
  const cov = useMemo(() => measureCoverage(sessions, failed), [sessions, failed]);

  // THE PANEL NEVER DISAPPEARS. It used to early-return null whenever there
  // were no days to plot, so a failed digest read removed the whole section and
  // the operator saw no panel AND no error — absence reading as "nothing to
  // report", which is the opposite of the truth. Each state now renders.
  if (cov.state === "failed") {
    return (
      <section className="mb-[10px] rounded-[7px] border border-[hsl(var(--bad)/0.35)] bg-[hsl(var(--bad)/0.10)] px-[10px] py-[7px]">
        <h3 className="m-0 text-[10px] font-[650] uppercase tracking-[0.06em] text-muted-foreground">
          Digest coverage
        </h3>
        <p className="m-0 mt-[4px] text-[12px] leading-[1.45]">
          <strong>Not measured.</strong> The <code>{cov.subject}</code> read failed, so coverage is
          unknown — this is not a report of zero digests. Retrying on the next poll.
        </p>
      </section>
    );
  }

  if (cov.state === "empty") {
    return (
      <section className="mb-[10px] rounded-[7px] border bg-card px-[10px] py-[7px]">
        <h3 className="m-0 text-[10px] font-[650] uppercase tracking-[0.06em] text-muted-foreground">
          Digest coverage
        </h3>
        <p className="m-0 mt-[4px] text-[12px] leading-[1.45]">
          The read succeeded and returned <strong>no {cov.subject} entities</strong>. This is a
          measured zero, not a failure.
        </p>
      </section>
    );
  }

  const peak = Math.max(...cov.days.map((d) => d.count));

  return (
    /* Tightened like everything else, but NOT demoted: the coverage gap is the
       most valuable thing on this screen, so it keeps its panel, its full
       sentence, and its histogram rather than shrinking to a stat line. */
    <section className="mb-[10px] rounded-[7px] border bg-card px-[10px] py-[7px]">
      <h3 className="m-0 text-[10px] font-[650] uppercase tracking-[0.06em] text-muted-foreground">
        Digest coverage
      </h3>
      <p className="m-0 mt-[4px] text-[12px] leading-[1.45]">
        <strong>
          {cov.burstCount} of {cov.total}
        </strong>{" "}
        digests were written on{" "}
        {cov.burst.length === 1 ? "a single day" : `${cov.burst.length} days`} (
        {cov.burst.map((d) => shortDate(d.date)).join(", ")}).{" "}
        {cov.gapDays !== null && cov.gapDays > 0 && (
          <>
            Nothing has been digested in <strong>{cov.gapDays} days</strong>. Sessions since then,
            including today's, have no digest and cannot appear in this list.
          </>
        )}
        {live && !sessions.some((s) => s.sessionId === live.sessionId) && (
          <> The session you are in right now is one of them.</>
        )}
      </p>
      {/* WHAT THIS GAP IS NOT. Nothing writes `session_digest` on a schedule —
          no daemon, no launchd, no cron; only a human running /status,
          /review-sessions, or /end. So the gap measures when someone last ran
          one of those by hand, and reading it as a swarm health metric that
          "dipped" would be wrong. Stated because a coverage chart with a gap
          invites exactly that misreading. */}
      <p className="m-0 mt-[4px] text-[11px] leading-[1.45] text-muted-foreground">
        Digests are written only when an operator runs <code>/status</code>,{" "}
        <code>/review-sessions</code>, or <code>/end</code> — nothing writes them on a schedule. This
        tracks how recently one of those was run by hand, not swarm health.
      </p>

      {/* Bars are counts by WRITE date. Height is a percentage of the peak day. */}
      <div className="mt-[6px] flex h-[34px] items-end gap-[2px]" aria-hidden="true">
        {cov.days.map((d) => (
          <div
            key={d.date}
            title={`${d.date}: ${d.count}`}
            className="min-w-[3px] flex-1 rounded-t-[2px] bg-[hsl(var(--live)/0.55)]"
            style={{ height: `${Math.max((d.count / peak) * 100, 3)}%` }}
          />
        ))}
      </div>
      <div className="mt-[4px] flex justify-between text-[11px] text-muted-foreground">
        <span>{shortDate(cov.days[0].date)}</span>
        <span>digests written per day</span>
        <span>{shortDate(cov.days[cov.days.length - 1].date)}</span>
      </div>
    </section>
  );
}

/** One session's digest, in full. */
function SessionDetail({
  session,
  onBack,
  embedded,
  onOpenEntity,
}: {
  session: SessionDigest;
  onBack?: () => void;
  embedded?: boolean;
  onOpenEntity?: (id: string) => void;
}) {
  return (
    <article>
      {onBack && (
        <Button variant="outline" size="sm" className="mb-[8px]" onClick={onBack}>
          <ArrowLeft className="mr-1 h-[13px] w-[13px]" />
          All sessions
        </Button>
      )}

      {!embedded && (
        <header className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h2 className="m-0 text-[19px] font-[650] tracking-[-0.01em]">{session.title}</h2>
            <p className="mt-[5px] text-[13px] text-muted-foreground">
              <code className="text-[12px]">{session.sessionId.slice(0, 8)}</code>
              {session.spanStart && session.spanEnd && (
                <>
                  {" "}
                  · {shortDate(session.spanStart)} – {shortDate(session.spanEnd)}
                </>
              )}
            </p>
          </div>
          {onOpenEntity && (
            <button
              type="button"
              onClick={() => onOpenEntity(session.id)}
              className="flex flex-none items-center gap-1 rounded-[6px] px-[6px] py-[2px] text-[12px] text-muted-foreground hover:bg-accent hover:text-foreground"
            >
              digest
            </button>
          )}
        </header>
      )}

      <SessionBody session={session} onOpenEntity={onOpenEntity} />
    </article>
  );
}

/** Facts, summary, claims, decisions, questions, artifacts. */
function SessionBody({
  session,
  onOpenEntity,
}: {
  session: SessionDigest;
  onOpenEntity?: (id: string) => void;
}) {
  const mix = claimMix(session.claims);

  return (
    <>
      <dl className="my-[8px] flex flex-wrap gap-x-[18px] gap-y-[3px] rounded-[7px] border bg-card px-[10px] py-[6px]">
        <Fact label="Ran">
          {session.spanStart && session.spanEnd
            ? `${shortDate(session.spanStart)} – ${shortDate(session.spanEnd)}`
            : shortDate(session.spanEnd)}
        </Fact>
        <Fact label="Digest written">
          {session.writtenAt ? relativeTime(session.writtenAt) : "unknown"}
        </Fact>
        {session.harness && <Fact label="Harness">{session.harness}</Fact>}
        {session.digestMethod && (
          <Fact label="Method">{session.digestMethod.replace("_", " ")}</Fact>
        )}
        {/* Two facts, not one. The old single "Claims: 3 of 5 complete" read as
            though five tasks existed and three were done; both halves were the
            session's own word. Self-report and verification are now separate
            rows so neither can be mistaken for the other. */}
        {mix.total > 0 && (
          <Fact label="Self-reported">
            {mix.claimedComplete} of {mix.total} complete
          </Fact>
        )}
        {mix.total > 0 && (
          <Fact label="Verified">
            {mix.confirmed + mix.refuted + mix.unverifiable === 0 ? (
              <span title="No claim in this session was checked against a system of record">
                none of {mix.total}
              </span>
            ) : (
              <>
                {mix.confirmed} confirmed
                {mix.refuted > 0 && <>, {mix.refuted} refuted</>}
                {mix.unverifiable > 0 && <>, {mix.unverifiable} unverifiable</>}
                {mix.unchecked > 0 && <>, {mix.unchecked} unchecked</>}
              </>
            )}
          </Fact>
        )}
      </dl>

      {session.topics.length > 0 && (
        <div className="my-[8px] flex flex-wrap gap-[4px]">
          {session.topics.map((t) => (
            <Badge key={t} variant="muted">
              {t}
            </Badge>
          ))}
        </div>
      )}

      {session.summary && <ClampedProse label="Summary" source={session.summary} />}

      {session.claims.length > 0 && (
        <section className="my-[10px]">
          {/* Was "Claimed tasks", which read as a list of task entities. These
              are sentences from `tasks_claimed` on the digest — self-report.
              None of them is a `task` in the graph. */}
          <h3 className="mb-[3px] text-[10px] font-[650] uppercase tracking-[0.06em] text-muted-foreground">
            What this session said it did
          </h3>
          <p className="mb-[10px] mt-0 text-[12px] leading-[1.5] text-muted-foreground">
            Each line is the session's own account of its work — not a{" "}
            <code className="text-[11.5px]">task</code> entity, and not evidence that the work
            happened. The first badge is the status it gave itself; the second is whether anyone
            checked, where <strong>intent</strong> means nobody has.
          </p>
          <ul className="m-0 flex list-none flex-col gap-[6px] p-0">
            {session.claims.map((c, i) => (
              <ClaimRow key={i} claim={c} />
            ))}
          </ul>
        </section>
      )}

      {session.openQuestions.length > 0 && (
        <ListSection title="Open questions" items={session.openQuestions} />
      )}
      {session.decisions.length > 0 && <ListSection title="Decisions" items={session.decisions} />}

      {session.artifacts.length > 0 && (
        <section className="my-[10px]">
          <h3 className="mb-[3px] text-[10px] font-[650] uppercase tracking-[0.06em] text-muted-foreground">
            Artifacts
          </h3>
          <ul className="m-0 flex list-none flex-col gap-[6px] p-0">
            {session.artifacts.map((a, i) => {
              // Bound to a const so the `ent_` check narrows it for the click
              // handler below. Entity refs are the only artifact kind that
              // resolves to an entity; PR/issue/file refs are not openable.
              const ref = a.ref;
              const openable = ref?.startsWith("ent_") ? ref : null;

              return (
                <li
                  key={i}
                  className="flex items-baseline gap-[8px] border-b border-border/60 py-[3px] text-[12px] last:border-b-0"
                >
                  {a.kind && (
                    <Badge variant="muted" className="flex-none">
                      {a.kind}
                    </Badge>
                  )}
                  <span className="min-w-0 flex-1">
                    <code className="text-[12px]">{a.ref}</code>
                    {a.description && (
                      <span className="ml-2 text-muted-foreground">{a.description}</span>
                    )}
                  </span>
                  {openable && onOpenEntity && (
                    <button
                      type="button"
                      onClick={() => onOpenEntity(openable)}
                      className="flex-none rounded-[6px] px-[6px] py-[2px] text-[11px] text-muted-foreground hover:bg-accent hover:text-foreground"
                    >
                      Open
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      )}
    </>
  );
}

function ClaimRow({ claim }: { claim: Claim }) {
  return (
    /* Both badges move ONTO the claim's own line — they were a separate row
       above it, costing a line per claim to say two words. The pair is
       meaning-bearing (claimed status, then whether anyone verified it), so
       both survive; only their placement changed. */
    <li className="border-b border-border/60 py-[4px] last:border-b-0">
      <div className="flex flex-wrap items-baseline gap-[6px]">
        <Badge variant={claimStatusTone(claim.statusClaimed)} className="flex-none">
          {claim.statusClaimed ?? "unstated"}
        </Badge>
        <Badge variant={verificationTone(claim.verificationState)} className="flex-none">
          {claim.verificationState ?? "intent"}
        </Badge>
        <span className="min-w-0 flex-1 text-[12.5px] leading-[1.4]">{claim.claim}</span>
      </div>
      {claim.verificationNote && (
        <p className="m-0 mt-[2px] text-[11.5px] leading-[1.4] text-muted-foreground">
          {claim.verificationNote}
        </p>
      )}
      {claim.evidence.length > 0 && (
        <p className="m-0 mt-[2px] flex flex-wrap gap-x-[10px] gap-y-0 text-[11px] text-muted-foreground">
          {claim.evidence.slice(0, 6).map((e, i) => (
            <code key={i} className="text-[11px]">
              {e}
            </code>
          ))}
        </p>
      )}
    </li>
  );
}

function ListSection({ title, items }: { title: string; items: string[] }) {
  return (
    <section className="my-[10px]">
      <h3 className="mb-[3px] text-[10px] font-[650] uppercase tracking-[0.06em] text-muted-foreground">
        {title}
      </h3>
      <ul className="m-0 list-none p-0">
        {items.map((t, i) => (
          <li
            key={i}
            className="border-b border-border/60 py-[3px] text-[12.5px] leading-[1.4] last:border-b-0"
          >
            {t}
          </li>
        ))}
      </ul>
    </section>
  );
}

/**
 * A long prose field, CLAMPED BY DEFAULT with an expander.
 *
 * `scope_summary` on the current session runs to roughly 2,500 words. Rendered
 * in full it pushed the session's actual work — its tasks, questions, and
 * artifacts — entirely below the fold, on what is now the app's landing view.
 * The summary is worth keeping and worth reading; it is not worth the top
 * screenful every time the operator opens the page.
 *
 * Collapsed by default, and the control says which way it goes. The clamp is a
 * max-height with a fade rather than a line-clamp, because the content is
 * rendered markdown — headings and lists — not a single text run.
 */
function ClampedProse({ label, source }: { label: string; source: string }) {
  const [open, setOpen] = useState(false);

  return (
    <section className="my-[10px]">
      <div className="mb-[3px] flex items-baseline gap-[8px]">
        <h3 className="m-0 text-[10px] font-[650] uppercase tracking-[0.06em] text-muted-foreground">
          {label}
        </h3>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="cursor-pointer border-none bg-transparent p-0 text-[11px] text-live hover:underline"
        >
          {open ? "Collapse" : "Expand"}
        </button>
      </div>
      <div className={cn("relative", !open && "max-h-[86px] overflow-hidden")}>
        <Markdown source={source} />
        {/* Fade only while collapsed, so it reads as "there is more" rather
            than as the text simply ending. */}
        {!open && (
          <div
            className="pointer-events-none absolute inset-x-0 bottom-0 h-[30px] bg-gradient-to-b from-transparent to-background"
            aria-hidden
          />
        )}
      </div>
    </section>
  );
}

/** One label/value pair in a facts strip. The label is the stored field name. */
function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline gap-[5px]">
      <dt className="text-[10px] uppercase tracking-[0.05em] text-muted-foreground">{label}</dt>
      <dd className="m-0 text-[12px]">{children}</dd>
    </div>
  );
}
