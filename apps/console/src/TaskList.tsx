/**
 * TASK VIEW
 * ---------
 * The task queue, mounted under the Tasks route.
 *
 * Questions are filtered out of the table: they are `task` entities too, and
 * counting them among the work items would inflate every bucket. They are
 * counted OUT LOUD in the header rather than silently dropped — the gap between
 * "rows this query returned" and "work items on screen" is exactly the kind of
 * unexplained arithmetic this file now exists to prevent.
 *
 * Read-only: rows OPEN the entity sheet in this app; nothing here writes.
 *
 * Rows used to link out to `https://neotoma.…/entities/<id>`, which threw away
 * the queue the operator was scanning and did not even work — entity URLs 401
 * without an access_token, and those exist only in publish-time responses. The
 * in-app detail (sheet plus `#/entities/<id>` full page) already renders these
 * through the shared `EntityDetail`, so the row opens that instead. The
 * external Neotoma link survives as a clearly-labelled secondary action inside
 * the detail view, which is where it belongs.
 *
 * ─────────────────────────────────────────────────────────────────────────────
 * WHY THIS VIEW FILTERS AT THE QUERY, AND DOES NOT PAGE
 * ─────────────────────────────────────────────────────────────────────────────
 * This page used to render whatever `/api/tasks?limit=200` returned and label
 * it "N most-recent tasks". There are 20,991 task entities. So the operator was
 * shown 189 rows and a number that read as a complete inventory, with nothing
 * on screen to say that 20,802 tasks were missing. That is the bug this file
 * was rewritten to fix, and the count display below is the fix — the missing
 * rows are the smaller half of the problem.
 *
 * PAGING WAS EVALUATED AND REJECTED, on three measurements rather than taste:
 *
 *   1. `offset` MAY NOT EXCEED 2000. Neotoma rejects a deeper offset outright
 *      ("offset must not exceed 2000; use cursor for deep pagination"). Offset
 *      paging therefore cannot reach past row 2000 of 20,991 — ten pages, then
 *      a wall. A pager whose later pages do not exist is a worse lie than the
 *      truncation it replaced.
 *   2. EVERY PAGE COSTS A FULL QUERY, and these queries run 5–19 seconds
 *      (measured; identical queries varied 7.3s → 13.2s minutes apart, and the
 *      unfiltered one intermittently exceeds the proxy's 30s budget and fails).
 *      Infinite scroll would put that wait on the scrollbar.
 *   3. NOBODY READS 20,991 ROWS. The useful question is never "show me all
 *      tasks", it is "show me open work", "show me what Cicada owns".
 *
 * So the narrowing happens SERVER-SIDE, in the query, via `/api/tasks`'s
 * `scope`/`status`/`assigned_to` parameters. A filter both answers the real
 * question and is CHEAPER than the unfiltered fetch it replaces: open work
 * owned by one agent came back in 4.9s and 86 rows — complete, no truncation to
 * disclose. Narrowing until the answer fits is the paging mechanism here.
 *
 * The bucket chips COUNT server-side and FILTER client-side, and the two are
 * deliberately different scopes. Their counts come from `/api/facets`, which
 * aggregates across every open task, so "Pending 4,912" is a fact about the
 * backlog. Clicking one still narrows the loaded page only — it is a fast
 * second cut within a server-narrowed set, not a way to reach rows the query
 * never returned — which is why the count and the filtered row total differ
 * and the page says so rather than hiding it.
 *
 * NO 10s POLL ON THIS VIEW, deliberately. `App` polls `/api/tasks` every 10
 * seconds for the sidebar, but these queries take 5–19s: a 10s interval would
 * stack overlapping requests on an instance whose reader pool is already the
 * documented bottleneck (ateles#576, neotoma#2217). This view fetches when the
 * filter changes and when the operator asks, and stamps what it shows with the
 * time it was read.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { foldFacets, type FacetCounts } from "./facets";
import {
  BUCKETS,
  BUCKET_LABELS,
  type Bucket,
  type Task,
  type TaskEntity,
  parseTask,
  relativeTime,
  toBucket,
} from "./tasks";
import { AssignedTo } from "./AssignedTo";
import {
  type Count,
  FILTERED_TOTAL_CEILING,
  countFrom,
  countText,
  isExact,
  isTruncated,
  missingRows,
} from "./taskCount";
import {
  MIN_QUERY_LENGTH,
  OPEN_TASK_STATUSES,
  SEARCH_LIMIT,
  type SearchResult,
  applyFilters,
  runSearch,
} from "./taskSearch";
import { useRoster } from "./useRoster";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { FilterChipsSkeleton, TaskListSkeleton } from "@/components/Skeletons";
import { showSkeleton } from "@/lib/loading";
import { cn } from "@/lib/utils";

type Filter = Bucket | "all" | "undispatched";

interface Props {
  /**
   * The 200 most recently updated tasks, any status, from `App`'s shared poll,
   * with questions already split out. Serves the "Recent — any status" scope
   * exactly: that scope IS this query, so selecting it costs no new request.
   */
  work: Task[];
  /** True once `App`'s first poll has settled. Drives skeletons; see App.tsx. */
  firstLoadDone: boolean;
  lastSync: Date | null;
  /** Ids that appeared since the last poll, highlighted briefly. */
  fresh: Set<string>;
  /** Opens the entity sheet, exactly as the session view does. */
  onOpenEntity: (id: string) => void;
  /** Opens one agent's detail page, from the assigned-to cell. */
  onOpenAgent: (id: string) => void;
}

/* ────────────────────────────────────────────────────────────────────────────
 * THE SERVER-SIDE FILTER
 * ──────────────────────────────────────────────────────────────────────────── */

/**
 * Which status rule the query applies.
 *
 * `open` is the default. It resolves server-side to the eleven non-terminal
 * status spellings production actually holds (the proxy owns that list — see
 * `OPEN_TASK_STATUSES` in `server/neotomaProxy.ts`, which is the definition of
 * "open"; the menu below is only a menu).
 */
type StatusSel = "open" | "any" | (string & {});

/**
 * The statuses offered in the dropdown, grouped as the operator thinks of them.
 *
 * Counts are NOT shown here. A count per option would cost one upstream query
 * each (11 statuses × ~1–5s), and a cached one would go stale silently — the
 * header already prints the live count for whichever option is selected.
 */
const STATUS_MENU: { group: string; values: string[] }[] = [
  { group: "Open", values: ["pending", "open", "todo", "in_progress", "active"] },
  {
    group: "Waiting",
    values: ["blocked", "awaiting_input", "awaiting_approval", "awaiting_release_confirmation"],
  },
  { group: "Closed", values: ["done", "completed", "canceled", "cancelled", "declined", "superseded"] },
];

/** How many rows one query asks for. */
const PAGE_LIMIT = 200;

/** A settled page of server-filtered tasks, plus the provenance to describe it. */
interface TaskPage {
  /** Work items — questions removed, as the table's contract requires. */
  rows: Task[];
  /** Rows the query actually returned, questions INCLUDED. */
  received: number;
  /** Open questions inside those rows, routed to the sidebar instead. */
  questions: number;
  /** How many tasks match this filter upstream, however well that is known. */
  total: Count;
  readAt: Date;
}

/**
 * Cache the last page per filter, so returning to the Tasks tab is instant.
 *
 * Module-level and cleared by a reload, the same contract `useRoster` uses for
 * the roster. It matters more here: a cold fetch on this view is 5–19 seconds,
 * so without this, switching tabs and back would re-pay that every time. A
 * cached page always renders with the timestamp it was READ at, never with
 * "now", so a stale page cannot pass for a fresh one.
 */
/**
 * THE SAVED VIEWS — the few questions this page is actually opened to answer.
 *
 * Each is expressed purely as server-side query parameters, so a view reaches
 * the whole backlog rather than re-cutting the loaded page. `keepOwner` marks
 * the one view that is ABOUT an owner and so must not clear the owner select;
 * every other view clears it, because leaving a stale owner set would silently
 * answer a narrower question than the view's own label claims.
 *
 * Deliberately short. A long list of views is a worse version of the controls
 * below it; these earn their place by being the queries that recur.
 */
const VIEWS: ReadonlyArray<{
  label: string;
  hint: string;
  status: StatusSel;
  priority: string;
  staleDays: string;
  keepOwner?: boolean;
}> = [
  {
    label: "Open work",
    hint: "Every non-terminal task, newest first.",
    status: "open",
    priority: "",
    staleDays: "",
  },
  {
    label: "High priority",
    hint: "Open tasks marked high priority.",
    status: "open",
    priority: "high",
    staleDays: "",
  },
  {
    label: "Stalled 30d+",
    hint: "Open tasks untouched for at least 30 days.",
    status: "open",
    priority: "",
    staleDays: "30",
  },
  {
    label: "Blocked",
    hint: "Tasks blocked on something — check blocked_reason.",
    status: "blocked",
    priority: "",
    staleDays: "",
  },
  {
    label: "Awaiting input",
    hint: "Tasks waiting on the operator.",
    status: "awaiting_input",
    priority: "",
    staleDays: "",
  },
  {
    label: "This owner's open work",
    hint: "Open work for whoever is selected in the owner control.",
    status: "open",
    priority: "",
    staleDays: "",
    keepOwner: true,
  },
];

const pageCache = new Map<string, TaskPage>();

/** The query string for a filter, and its cache key — one function, so they cannot diverge. */
function queryKey(
  status: StatusSel,
  assignedTo: string,
  priority: string,
  staleDays: string,
): string {
  const params = new URLSearchParams({ limit: String(PAGE_LIMIT) });
  if (status === "open") params.set("scope", "open");
  else if (status !== "any") params.set("status", status);
  if (assignedTo) params.set("assigned_to", assignedTo);
  if (priority) params.set("priority", priority);
  if (staleDays) params.set("stale_days", staleDays);
  return params.toString();
}

/**
 * Fetch one server-filtered page.
 *
 * Returns a page or an error string; never throws, and never yields a partial
 * page with an invented total. `reload` is a counter the caller bumps to force
 * a refetch of a filter that is already cached.
 */
function useTaskPage(
  status: StatusSel,
  assignedTo: string,
  priority: string,
  staleDays: string,
  enabled: boolean,
  reload: number,
): { page: TaskPage | null; loading: boolean; error: string | null } {
  const key = queryKey(status, assignedTo, priority, staleDays);
  const [page, setPage] = useState<TaskPage | null>(() => pageCache.get(key) ?? null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Guards a slow response for a filter the operator has already moved off.
  const wanted = useRef(key);

  useEffect(() => {
    wanted.current = key;
    if (!enabled) {
      // Clear the last filter's failure on the way out. Leaving it set showed
      // "this filter could not be read" over the shared-poll view, which had
      // not run a query at all and had nothing wrong with it.
      setError(null);
      setLoading(false);
      return;
    }

    /**
     * A cache HIT is served without a request.
     *
     * Refresh does not need a special case here, and must not have one: it
     * EVICTS this key before bumping `reload`, so a refreshed filter simply
     * misses. An earlier version tested `reload === 0` instead, which meant
     * that after the operator's first Refresh the cache was bypassed for the
     * rest of the session — every subsequent filter change re-paid a 5–19s
     * query that was already in hand.
     */
    const cached = pageCache.get(key);
    if (cached) {
      setPage(cached);
      setError(null);
      setLoading(false);
      return;
    }

    let alive = true;
    setLoading(true);
    // The previous filter's rows are dropped immediately: showing them under a
    // new filter's heading would attribute one query's rows to another's count.
    setPage(null);

    void (async () => {
      try {
        const res = await fetch(`/api/tasks?${key}`);
        const body = await res.json();
        if (!res.ok || body.error) throw new Error(body.error ?? `HTTP ${res.status}`);

        const parsed = (body.entities as TaskEntity[]).map(parseTask);
        const next: TaskPage = {
          rows: parsed.filter((t) => !t.question),
          received: parsed.length,
          questions: parsed.filter((t) => t.question).length,
          total: countFrom(body),
          readAt: new Date(),
        };
        pageCache.set(key, next);
        if (alive && wanted.current === key) {
          setPage(next);
          setError(null);
        }
      } catch (err) {
        if (alive && wanted.current === key) {
          // No page and no total: the header prints "not measured" rather than
          // falling back to a number from a filter this is not.
          setPage(null);
          setError((err as Error).message);
        }
      } finally {
        if (alive && wanted.current === key) setLoading(false);
      }
    })();

    return () => {
      alive = false;
    };
  }, [key, enabled, reload]);

  return { page, loading, error };
}

/** Badge tone per status bucket, preserving the original colour mapping. */
const BUCKET_TONE: Record<Bucket, "live" | "bad" | "ok" | "muted"> = {
  in_progress: "live",
  blocked: "bad",
  done: "ok",
  pending: "muted",
  other: "muted",
};

/** Priority colour: the top band reads as urgent, medium as a warning. */
function priorityClass(priority: string): string {
  switch (priority.toLowerCase()) {
    case "high":
    case "critical":
    case "urgent":
      return "text-bad";
    case "medium":
      return "text-warn";
    default:
      return "";
  }
}

export function TaskList({
  work,
  firstLoadDone,
  lastSync,
  fresh,
  onOpenEntity,
  onOpenAgent,
}: Props) {
  const [filter, setFilter] = useState<Filter>("all");
  const [status, setStatus] = useState<StatusSel>("open");
  const [assignedTo, setAssignedTo] = useState("");
  const [priority, setPriority] = useState("");
  const [staleDays, setStaleDays] = useState("");
  const [reload, setReload] = useState(0);
  const roster = useRoster();

  /**
   * SEARCH STATE.
   *
   * `draft` is what the box holds; `query` is what was actually SUBMITTED.
   * They are separate because search runs ON SUBMIT, never per keystroke: one
   * query is a 5–30s upstream read against a reader pool that is the
   * documented bottleneck (ateles#576, neotoma#2217), so a query per character
   * would queue dozens of them and take the app down with it. Enter or the
   * button runs it; typing alone does not.
   */
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState<SearchResult | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  /** Import residue is SHOWN by default — see `isImported` in taskSearch.ts. */
  const [hideImported, setHideImported] = useState(false);

  const searchActive = query.length >= MIN_QUERY_LENGTH;

  useEffect(() => {
    if (!searchActive) {
      setSearch(null);
      setSearchError(null);
      setSearching(false);
      return;
    }
    const controller = new AbortController();
    let alive = true;
    setSearching(true);
    // Drop the previous query's rows immediately: showing them under a new
    // query's heading would attribute one search's results to another's count.
    setSearch(null);
    setSearchError(null);

    void (async () => {
      try {
        const result = await runSearch(query, controller.signal);
        if (alive) setSearch(result);
      } catch (err) {
        if ((err as Error).name === "AbortError") return;
        // No rows and no count: a failed search must not render as "nothing
        // matched" against a 21,285-task backlog.
        if (alive) setSearchError((err as Error).message);
      } finally {
        if (alive) setSearching(false);
      }
    })();

    return () => {
      alive = false;
      controller.abort();
    };
  }, [query]);

  /**
   * "Recent — any status" with no owner filter is precisely `App`'s poll, so it
   * is served from the `work` prop: instant, and one fewer identical query
   * against a loaded instance.
   */
  const usesSharedPoll = status === "any" && !assignedTo && !priority && !staleDays;

  const { page, loading, error } = useTaskPage(
    status,
    assignedTo,
    priority,
    staleDays,
    !usesSharedPoll,
    reload,
  );

  /** The grand denominator: every task entity, filter or no filter. */
  const [grandTotal, setGrandTotal] = useState<Count>({ kind: "unmeasured" });
  const [totalPending, setTotalPending] = useState(true);
  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const res = await fetch("/api/task-total");
        const body = await res.json();
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        // The proxy sends `{total: null}` when the count could not be read;
        // `countFrom` turns that into `unmeasured`, never 0.
        if (alive) setGrandTotal(countFrom(body));
      } catch {
        if (alive) setGrandTotal({ kind: "unmeasured" });
      } finally {
        if (alive) setTotalPending(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  /** Rows on screen before the client-side chips narrow them further. */
  const loaded: Task[] = usesSharedPoll ? work : (page?.rows ?? []);

  /**
   * TRUE bucket counts, fetched once and independent of what the page loaded.
   *
   * Null while in flight, so the chips show nothing rather than briefly
   * showing the old page-scoped numbers. Fetched once on mount: these are
   * counts over the whole backlog and do not change with the filter, and the
   * query costs eleven upstream counts on an instance whose reader pool is the
   * documented bottleneck (ateles#576).
   */
  const [facets, setFacets] = useState<FacetCounts | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await fetch("/api/facets");
        const body = await res.json();
        if (alive) setFacets(foldFacets(body));
      } catch {
        // Leave `facets` null: the chips then read "not measured", which is
        // the truth, rather than falling back to the page-scoped count this
        // whole change exists to remove.
        if (alive) setFacets(null);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  /**
   * Rows PER CHIP within the loaded page — what clicking the chip will show.
   *
   * Kept separate from the facet counts above because they answer different
   * questions, and the earlier bug was precisely that one number was made to
   * answer both. This one is honest about its scope by construction: it is
   * only ever rendered next to the loaded-row disclosure.
   */
  const loadedCounts = useMemo(() => {
    const c: Record<string, number> = { all: loaded.length, undispatched: 0 };
    for (const b of BUCKETS) c[b] = 0;
    for (const t of loaded) {
      c[t.bucket]++;
      if (t.undispatched) c.undispatched++;
    }
    return c;
  }, [loaded]);

  /**
   * SEARCH ROWS, COMPOSED WITH THE PAGE'S FILTERS.
   *
   * The filters are applied HERE rather than upstream because upstream drops
   * them when `search` is present — it accepts `status: pending` beside a
   * search, ignores it, and returns `completed` and `in_progress` rows anyway
   * (finding 5 in `taskSearch.ts`). Applying them client-side is what makes
   * "undispatched, high priority, matching X" answerable at all.
   *
   * This is NOT the client-side filtering the search box was forbidden to do.
   * The TEXT match ran across all 21,285 tasks upstream; only the narrowing of
   * that result set happens here, and the disclosure below states both figures
   * so the filtered count is never mistaken for the backlog count.
   */
  const searchRows = useMemo(() => {
    if (!search) return [];
    return applyFilters(
      search.rows,
      { status, assignedTo, priority, staleDays, chip: filter, hideImported },
      OPEN_TASK_STATUSES,
    );
  }, [search, status, assignedTo, priority, staleDays, filter, hideImported]);

  /** How many of the fetched search rows are Asana import residue. */
  const searchImported = useMemo(
    () => (search ? search.rows.filter((r) => r.imported).length : 0),
    [search],
  );

  const visible = useMemo(() => {
    if (filter === "all") return loaded;
    if (filter === "undispatched") return loaded.filter((t) => t.undispatched);
    return loaded.filter((t) => t.bucket === filter);
  }, [loaded, filter]);

  /**
   * THE ROWS THE TABLE ACTUALLY DRAWS.
   *
   * A search REPLACES the page's rows rather than re-cutting them, because the
   * two sets answer different questions: `visible` is a page of the backlog
   * narrowed by the query controls, while a search result is drawn from all
   * 21,285 tasks. Intersecting them would silently restrict the search to the
   * 200 rows already loaded — the exact defect this feature exists to avoid.
   *
   * Only the fields the table renders are needed, so both sources are mapped to
   * one minimal shape. `bucket` is derived for search rows via the same
   * `toBucket` the page uses, so a status badge means the same thing either way.
   */
  const rendered = useMemo(() => {
    const source = searchActive
      ? searchRows.map((r) => ({
          id: r.id,
          title: r.title,
          status: r.status,
          bucket: toBucket(r.status),
          priority: r.priority,
          assignedTo: r.assignedTo,
          updatedAt: r.updatedAt,
          undispatched: r.undispatched,
        }))
      : visible.map((t) => ({
          id: t.id,
          title: t.title,
          status: t.status,
          bucket: t.bucket,
          priority: t.priority,
          assignedTo: t.assignedTo,
          updatedAt: t.updatedAt,
          undispatched: t.undispatched,
        }));
    return source;
  }, [searchActive, searchRows, visible]);

  const chips: Filter[] = ["all", "undispatched", ...BUCKETS];

  /**
   * How many tasks match the current filter upstream.
   *
   * On the shared-poll scope this is the UNFILTERED total — the same number
   * `/api/task-total` reports, and exact for the same reason (no
   * `snapshot_filters`, so upstream runs a real aggregate).
   */
  const matching: Count = usesSharedPoll ? grandTotal : (page?.total ?? { kind: "unmeasured" });

  /** Rows the query returned, questions included — the honest numerator. */
  const received = usesSharedPoll ? work.length : (page?.received ?? 0);
  const questionsHere = usesSharedPoll ? 0 : (page?.questions ?? 0);

  /**
   * Is the list truncated, and by how much?
   *
   * `missing` is null whenever the shortfall would be a guess — see
   * `missingRows`. The banner then says the remainder is unknown rather than
   * printing a number derived from a figure that is not a count.
   */
  const missing = missingRows(matching, received);
  const truncated = isTruncated(matching, received);

  const pending = usesSharedPoll ? showSkeleton(!firstLoadDone, work.length > 0) : loading && !page;
  const readAt = usesSharedPoll ? lastSync : (page?.readAt ?? null);

  /** One line naming the set on screen, for the header. */
  const scopeLabel =
    status === "open" ? "open" : status === "any" ? "most recently updated" : status;

  return (
    <>
      {/* Title and count on ONE line: a stacked header cost a row of tasks for
          two facts that fit side by side. */}
      <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h1 className="m-0 text-[16px] font-[650] tracking-[-0.01em]">Swarm tasks</h1>
        <p className="m-0 text-[12px] text-muted-foreground">
          {pending ? (
            "Loading…"
          ) : (
            <>
              {/*
               * THE NUMBER THAT USED TO LIE. It read "189 most-recent tasks",
               * which the operator reasonably took for the whole backlog. Both
               * halves of the fraction are now on screen, and the denominator
               * carries its own provenance: an exact count, a `≥` lower bound,
               * or the words "not measured".
               */}
              <span className="tabular-nums">{rendered.length.toLocaleString()}</span>
              {" shown · "}
              {/*
               * DURING A SEARCH the rows come from the search query, not from
               * this page's query, so the page's "loaded of N open" fraction
               * would be describing a list that is not on screen. The search
               * banner below carries the figures that DO describe these rows.
               */}
              {searchActive ? (
                <>matching this search · </>
              ) : (
                <>
                  <span className="tabular-nums">{received.toLocaleString()}</span>
                  {" loaded of "}
                  <span className={cn("tabular-nums", !isExact(matching) && "text-warn")}>
                    {countText(matching)}
                  </span>{" "}
                  {scopeLabel}
                  {" · "}
                </>
              )}
              <span className="tabular-nums">
                {totalPending ? "measuring…" : countText(grandTotal)}
              </span>
              {" tasks in Neotoma"}
              {readAt && <> · read {relativeTime(readAt)}</>}
            </>
          )}
        </p>

        {!usesSharedPoll && (
          <Button
            variant="outline"
            size="sm"
            className="ml-auto"
            disabled={loading}
            onClick={() => {
              // Evict, then retrigger: the fetch path treats a cache miss as
              // "go and ask", so refresh needs no separate branch there.
              pageCache.delete(queryKey(status, assignedTo, priority, staleDays));
              setReload((n) => n + 1);
            }}
          >
            {loading ? "Refreshing…" : "Refresh"}
          </Button>
        )}
      </header>

      {/*
       * SERVER-SIDE FILTERS. Everything in this row changes the QUERY, not the
       * rendering — which is what lets the operator reach a task that is not in
       * the first 200 rows. The chips below it re-cut whatever came back.
       */}
      {/*
       * SAVED VIEWS — the handful of questions actually asked, as one click.
       *
       * The banner used to say "narrow the query above" without saying narrow
       * it toward WHAT, which left the operator to invent a query against a
       * 5,565-task backlog. These are the queries the swarm is actually run
       * with; each one sets the same server-side parameters the controls below
       * expose, so a view is a shortcut rather than a separate mechanism, and
       * the controls still show exactly what a view selected.
       */}
      <div className="mt-[10px] flex flex-wrap items-center gap-[6px]">
        <span className="text-[10px] uppercase tracking-[.06em] text-muted-foreground">
          Views
        </span>
        {VIEWS.map((v) => {
          const active =
            status === v.status &&
            priority === v.priority &&
            staleDays === v.staleDays &&
            (v.keepOwner ? true : assignedTo === "");
          return (
            <Button
              key={v.label}
              variant="chip"
              size="chip"
              active={active}
              onClick={() => {
                setStatus(v.status);
                setPriority(v.priority);
                setStaleDays(v.staleDays);
                if (!v.keepOwner) setAssignedTo("");
                setFilter("all");
              }}
              title={v.hint}
            >
              {v.label}
            </Button>
          );
        })}
      </div>

      <div className="mt-[6px] flex flex-wrap items-center gap-[6px]">
        <span className="text-[10px] uppercase tracking-[.06em] text-muted-foreground">
          Query
        </span>

        <select
          value={status}
          onChange={(e) => setStatus(e.target.value)}
          aria-label="Filter by status, server-side"
          className="h-[26px] rounded-[6px] border bg-background px-[6px] text-[12.5px] outline-none"
        >
          <option value="open">Open work (non-terminal)</option>
          <option value="any">Any status — most recent</option>
          {STATUS_MENU.map((g) => (
            <optgroup key={g.group} label={g.group}>
              {g.values.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </optgroup>
          ))}
        </select>

        <select
          value={assignedTo}
          onChange={(e) => setAssignedTo(e.target.value)}
          aria-label="Filter by assignee, server-side"
          className="h-[26px] rounded-[6px] border bg-background px-[6px] text-[12.5px] outline-none"
        >
          <option value="">Anyone</option>
          {/* `assigned_to` is matched EXACTLY and stored as written, so the
              roster's own spelling is what goes upstream. Unassigned work is
              not a server-side filter — there is no is-null operator — so it
              stays on the Undispatched chip below. */}
          {roster.map((a) => (
            <option key={a.id} value={a.name}>
              {a.name}
            </option>
          ))}
        </select>

        <select
          value={priority}
          onChange={(e) => setPriority(e.target.value)}
          aria-label="Filter by priority, server-side"
          className="h-[26px] rounded-[6px] border bg-background px-[6px] text-[12.5px] outline-none"
        >
          <option value="">Any priority</option>
          <option value="high">high</option>
          <option value="medium">medium</option>
          <option value="low">low</option>
        </select>

        {/* Staleness is a DAY COUNT, not a date: the cutoff is computed
            server-side so the value crossing the wire is always well-formed,
            and "untouched for 30 days" is how the question is actually asked. */}
        <select
          value={staleDays}
          onChange={(e) => setStaleDays(e.target.value)}
          aria-label="Filter by staleness, server-side"
          className="h-[26px] rounded-[6px] border bg-background px-[6px] text-[12.5px] outline-none"
        >
          <option value="">Any age</option>
          <option value="7">Untouched 7d+</option>
          <option value="30">Untouched 30d+</option>
          <option value="90">Untouched 90d+</option>
          <option value="180">Untouched 180d+</option>
        </select>

        {(status !== "open" || assignedTo || priority || staleDays) && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setStatus("open");
              setAssignedTo("");
              setPriority("");
              setStaleDays("");
            }}
          >
            Reset
          </Button>
        )}
      </div>

      {/*
       * THE SEARCH BOX.
       *
       * Runs the text match UPSTREAM, across all 21,285 tasks. It deliberately
       * does NOT filter the loaded page: this list holds 200 rows of a 5,566-task
       * open set, so a client-side box would search 3.6% of the backlog while
       * looking like it searched everything, and its empty result would read as
       * "no such task". That is the defect #691 removed from the facet chips.
       *
       * A form, so Enter submits — search runs on submit, never per keystroke,
       * because each query is a multi-second upstream read.
       */}
      <form
        className="mt-[6px] flex flex-wrap items-center gap-[6px]"
        onSubmit={(e) => {
          e.preventDefault();
          setQuery(draft.trim());
        }}
      >
        <span className="text-[10px] uppercase tracking-[.06em] text-muted-foreground">
          Search
        </span>
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          type="search"
          placeholder="Text across every task — title, description, notes…"
          aria-label="Search all tasks, server-side"
          className="h-[26px] min-w-[260px] flex-1 rounded-[6px] border bg-background px-[8px] text-[12.5px] outline-none"
        />
        <Button type="submit" variant="outline" size="sm" disabled={searching}>
          {searching ? "Searching…" : "Search"}
        </Button>
        {(query || draft) && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => {
              setDraft("");
              setQuery("");
            }}
          >
            Clear
          </Button>
        )}
        {searchActive && searchImported > 0 && (
          <label className="flex items-center gap-[4px] text-[11.5px] text-muted-foreground">
            <input
              type="checkbox"
              checked={hideImported}
              onChange={(e) => setHideImported(e.target.checked)}
            />
            Hide {searchImported.toLocaleString()} imported
          </label>
        )}
      </form>

      {/* A query too short to send, explained rather than silently ignored. */}
      {draft.trim().length > 0 && draft.trim().length < MIN_QUERY_LENGTH && (
        <p className="my-[6px] text-[11.5px] text-muted-foreground">
          Enter at least {MIN_QUERY_LENGTH} characters — a single character matches as a
          substring across {countText(grandTotal)} tasks and answers nothing.
        </p>
      )}

      {searchError && (
        <div className="my-[10px] rounded-lg border border-[hsl(var(--bad)/0.35)] bg-[hsl(var(--bad)/0.12)] px-3 py-[8px] text-[12.5px]">
          <strong>The search did not answer.</strong> {searchError} — no rows and no count are
          shown. This is NOT "nothing matched": with {countText(grandTotal)} tasks in the
          backlog, an unanswered query and an empty result mean opposite things.
        </div>
      )}

      {/*
       * WHAT THE SEARCH ACTUALLY FOUND — the two figures that must both appear.
       *
       * `total` is backlog-wide and real (a search-only total does not saturate).
       * `matched` is what survived the page's filters, applied here because
       * upstream drops them. Printing only one of them would either understate
       * the backlog or label a filtered list with an unfiltered count.
       */}
      {searchActive && search && !searching && (
        <div className="my-[10px] rounded-lg border bg-muted/40 px-3 py-[8px] text-[12.5px]">
          <div>
            <strong>
              <span className="tabular-nums">{countText(search.total)}</span>
            </strong>{" "}
            {isExact(search.total) && search.total.value === 1 ? "task matches" : "tasks match"}{" "}
            “{query}” across the whole backlog
            {searchRows.length !== search.rows.length && (
              <>
                {" · "}
                <span className="tabular-nums">{searchRows.length.toLocaleString()}</span> shown
                after the filters above
              </>
            )}
            {" · read "}
            {relativeTime(search.readAt)}
          </div>

          {/*
           * The result set is a SAMPLE whenever the fetch filled its page. The
           * filters then ran over the newest N rows, not over every match, so
           * the filtered figure is a lower bound and says so.
           */}
          {search.capped && (
            <div className="mt-[4px] text-muted-foreground">
              Only the {SEARCH_LIMIT.toLocaleString()} most relevant rows were fetched, so the
              filtered count above is a lower bound — narrow the query or the filters to see
              the rest.
            </div>
          )}

          {/*
           * ACCENT FOLDING IS NOT DONE UPSTREAM (measured — see taskSearch.ts).
           * When a variant spelling was sent, say so; when none was available,
           * say THAT, because the query may then be missing rows written the
           * other way and the operator cannot tell from the result.
           */}
          {search.variants.length > 1 ? (
            <div className="mt-[4px] text-muted-foreground">
              Upstream does not fold accents, so this searched{" "}
              {search.variants.map((v) => `“${v}”`).join(" and ")} and merged the results.
            </div>
          ) : (
            /\p{Diacritic}/u.test(query.normalize("NFD")) === false && (
              <div className="mt-[4px] text-muted-foreground">
                Upstream matches the spelling exactly and does not fold accents: rows written
                with accented characters will not appear here.
              </div>
            )
          )}

          {search.failedVariants.length > 0 && (
            <div className="mt-[4px] text-warn">
              The spelling{search.failedVariants.length === 1 ? "" : "s"}{" "}
              {search.failedVariants.map((v) => `“${v}”`).join(", ")} could not be read, so
              matching rows are missing from this list and the count is incomplete.
            </div>
          )}

          {/*
           * Import residue is disclosed, never hidden by default. A third of
           * the backlog is an Asana import and it dominates most searches.
           */}
          {searchImported > 0 && (
            <div className="mt-[4px] text-muted-foreground">
              <span className="tabular-nums">{searchImported.toLocaleString()}</span> of the{" "}
              <span className="tabular-nums">{search.rows.length.toLocaleString()}</span> fetched
              rows are Asana import residue rather than swarm work
              {hideImported ? " and are hidden" : ""}.
            </div>
          )}
        </div>
      )}

      {/*
       * THE ONE QUERY UPSTREAM ANSWERS WITH A MEANINGLESS ZERO.
       * Searching type `task` for the term "task" returns 0 — reproducible on
       * `plan`, `issue` and `project` too. Rendering that as "no matches" would
       * be a confident wrong answer about the data.
       */}
      {searchActive && search?.typeNameCollision && (
        <div className="my-[10px] rounded-lg border border-warn bg-[hsl(var(--warn)/0.12)] px-3 py-[8px] text-[12.5px]">
          <strong>This query cannot be answered upstream.</strong> Searching tasks for the word
          “{query}” returns zero rows because the term is the entity type's own name — a known
          upstream defect, not a fact about the backlog. Add another word, or search for a
          distinctive part of the title instead.
        </div>
      )}

      {error && (
        <div className="my-[10px] rounded-lg border border-[hsl(var(--bad)/0.35)] bg-[hsl(var(--bad)/0.12)] px-3 py-[8px] text-[12.5px]">
          <strong>This filter could not be read.</strong> {error} — no rows and no count are
          shown for it, rather than a stale page under a new heading.
        </div>
      )}

      {/*
       * THE TRUNCATION NOTICE — the actual fix.
       *
       * Rendered whenever rows are missing, and it names how many. The old page
       * had nothing here, which is why 189 rows read as a complete backlog.
       * When the total is a lower bound the shortfall is described as "at
       * least" rather than subtracted into a confident figure.
       */}
      {!pending && !searchActive && truncated && (
        <div className="my-[10px] flex flex-wrap items-baseline gap-x-2 rounded-lg border border-warn bg-[hsl(var(--warn)/0.12)] px-3 py-[8px] text-[12.5px]">
          <strong>This list is partial.</strong>
          <span>
            {missing !== null ? (
              <>
                <span className="tabular-nums">{missing.toLocaleString()}</span> more{" "}
                {scopeLabel} {missing === 1 ? "task is" : "tasks are"} not shown.
              </>
            ) : (
              <>
                More {scopeLabel} tasks match than are shown, but how many more is unknown:
                upstream stops counting a filtered set at{" "}
                <span className="tabular-nums">
                  {FILTERED_TOTAL_CEILING.toLocaleString()}
                </span>
                , so the shortfall cannot be measured — only that it exists.
              </>
            )}{" "}
            One query returns at most {PAGE_LIMIT.toLocaleString()} rows, newest first, so the
            rows below over-represent recent work — pick a view above (High priority, Stalled
            30d+, an owner) to ask a narrower question and bring the rest into view. The chip
            counts are unaffected: they are measured across every open task, not this page.
          </span>
        </div>
      )}

      {/* Questions are counted rather than silently subtracted, so the drop
          from "loaded" to "shown" never looks like arithmetic going wrong. */}
      {!pending && !searchActive && questionsHere > 0 && (
        <p className="my-[6px] text-[11.5px] text-muted-foreground">
          {questionsHere} of the loaded rows{" "}
          {questionsHere === 1 ? "is an open question" : "are open questions"} and{" "}
          {questionsHere === 1 ? "sits" : "sit"} in the sidebar instead of this table.
        </p>
      )}

      {pending ? (
        <>
          <FilterChipsSkeleton count={chips.length} />
          <TaskListSkeleton rows={8} />
        </>
      ) : (
        <>
          <nav className="my-[10px] flex flex-wrap items-center gap-[5px]">
            {chips.map((f) => (
              <Button
                key={f}
                variant="chip"
                size="chip"
                active={filter === f}
                onClick={() => setFilter(f)}
                className={cn(
                  f === "undispatched" &&
                    filter === f &&
                    "border-warn bg-[hsl(var(--warn)/0.14)]",
                )}
              >
                {f === "all" ? "All" : f === "undispatched" ? "Undispatched" : BUCKET_LABELS[f]}
                {/*
                 * The count is the BACKLOG figure, not the page figure.
                 * "Undispatched" has no server-side count — it is derived from
                 * an absent `assigned_to`, which `snapshot_filters` cannot
                 * express as a negation (`ne`/`nin` are rejected 400) — so it
                 * shows its loaded-page count with an explicit scope marker
                 * rather than borrowing the authority of the others.
                 */}
                <span className="tabular-nums opacity-80">
                  {f === "undispatched"
                    ? `${loadedCounts.undispatched} of ${received.toLocaleString()}`
                    : facets
                      ? countText(facets[f])
                      : "…"}
                </span>
              </Button>
            ))}
            {/*
             * Says which set those numbers describe. These are counts across
             * every open task in Neotoma, which is why clicking a chip can
             * show far fewer rows than its own count: the count is the
             * backlog, the rows are this page of it. Saying so here is what
             * keeps the two from being read as one number.
             */}
            {/*
             * The chip counts are ALWAYS backlog-wide, which is right, but
             * during a search the rows beside them are a search result — so the
             * note has to say that clicking narrows the SEARCH, not the page,
             * or the two numbers read as one scope again.
             */}
            <span className="text-[11px] text-muted-foreground">
              {facets
                ? searchActive
                  ? "across all open tasks — clicking narrows the search results only"
                  : "across all open tasks — clicking narrows the loaded page only"
                : "counting all open tasks…"}
            </span>
          </nav>

          {/*
           * TABLE, not cards. The data here IS tabular — status, title,
           * priority, owner, age — so the card chrome was paying a border, a
           * radius, and 22px of padding per row to say nothing. Column headers
           * carry the field names ONCE instead of every row repeating them,
           * which is what lets the per-row labels go.
           */}
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
              {/*
               * The WHOLE ROW opens the entity sheet — the click target is the
               * row, not just the title, now that this is a table. Kept
               * keyboard-reachable and announced as a button; the pointer
               * cursor plus the existing hover tint are the only affordance,
               * since a chevron or a link colour per row would put back the
               * chrome the density pass just removed.
               */}
              {rendered.map((t) => (
                <tr
                  key={t.id}
                  role="button"
                  tabIndex={0}
                  aria-label={`Open ${t.title}`}
                  onClick={() => onOpenEntity(t.id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      onOpenEntity(t.id);
                    }
                  }}
                  className={cn(
                    "cursor-pointer border-b border-border/60 hover:bg-accent/60",
                    "focus-visible:outline focus-visible:outline-1 focus-visible:-outline-offset-1 focus-visible:outline-ring",
                    // Freshness is only known for the shared poll, which is the
                    // only query here that runs twice and can diff itself.
                    usesSharedPoll && fresh.has(t.id) && "animate-fresh-in",
                  )}
                >
                  <td className="py-[4px] pr-2 align-baseline">
                    {/* The undispatched flag rides the leading edge of the row,
                        where the card's amber left border used to sit. */}
                    <span
                      className={cn(
                        "flex items-baseline gap-[6px]",
                        t.undispatched &&
                          "-ml-[7px] border-l-[3px] border-l-warn pl-[4px]",
                      )}
                    >
                      <Badge variant={BUCKET_TONE[t.bucket]} caps>
                        {t.status}
                      </Badge>
                    </span>
                  </td>
                  <td className="py-[4px] pr-2 align-baseline">{t.title}</td>
                  <td
                    className={cn(
                      "py-[4px] pr-2 align-baseline text-[10.5px] uppercase tracking-[.04em]",
                      t.priority ? priorityClass(t.priority) : "",
                    )}
                  >
                    {t.priority || ""}
                  </td>
                  <td className="py-[4px] pr-2 align-baseline">
                    {/* "Undispatched" survives the compression as its own word,
                        in amber. A blank cell would read as merely missing data
                        rather than as work nobody picked up. */}
                    {/* Shared renderer: the owner is a LINK to the agent's
                        page, and a role Apis cannot spawn is flagged red
                        rather than reading as ordinary green assignment. */}
                    <AssignedTo
                      assignedTo={t.assignedTo}
                      agents={roster}
                      onOpenAgent={onOpenAgent}
                    />
                  </td>
                  <td className="py-[4px] text-right align-baseline tabular-nums text-muted-foreground">
                    {relativeTime(t.updatedAt)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!rendered.length && !error && !searching && (
            <p className="py-6 text-center text-muted-foreground">
              {/* An empty QUERY result, an empty CHIP cut and an empty SEARCH
                  are different facts, and the fix for each is a different
                  control. A search that matched upstream but kept nothing after
                  the filters says so, rather than reading as "no such task". */}
              {searchActive ? (
                searchError ? (
                  ""
                ) : search && search.rows.length > 0 ? (
                  <>
                    {search.rows.length.toLocaleString()} tasks matched “{query}”, but none of
                    them pass the filters above — widen the query controls to see them.
                  </>
                ) : search?.typeNameCollision ? (
                  ""
                ) : (
                  <>No task in the backlog matches “{query}”.</>
                )
              ) : loaded.length ? (
                "No loaded tasks match this chip."
              ) : (
                `No tasks match this query${isExact(matching) && matching.value === 0 ? "" : " in the rows returned"}.`
              )}
            </p>
          )}
        </>
      )}
    </>
  );
}
