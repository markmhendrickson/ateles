/**
 * GLOBAL SEARCH — nav-mounted, available from every route.
 *
 * Opens with Cmd/Ctrl-K from anywhere, closes with Escape. Read-only: it makes
 * GETs to `/api/search` and `/api/entity` and has no write path, like the rest
 * of this app.
 *
 * SEARCH RUNS ON SUBMIT, NOT ON KEYSTROKE. This is the whole design, not a
 * simplification. Neotoma reads currently take 25-81s (ateles#576), so a query
 * per character would queue dozens of 30s reads against an already-starved
 * reader pool. The input is uncontrolled-feeling for that reason: type freely,
 * press Enter, then the work starts.
 *
 * THE THREE OUTCOMES ARE THREE RENDERINGS
 * ---------------------------------------
 * Per entity type, independently:
 *
 *   - results        — rows, grouped under the type
 *   - "no matches"   — upstream answered, and the answer was nothing
 *   - "timed out"    — upstream did NOT answer; nothing may be concluded
 *
 * A timeout is NOT no-results, and rendering it as one tells the operator his
 * data does not contain something it may well contain. Same rule for an id
 * lookup: "no entity with that id" (a 404 — a real answer) is a different line
 * from a timeout.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Search, X, Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

import {
  DEFAULT_TYPES,

  EXTRA_TYPES,
  type SearchHit,
  type TypeResult,
  parseQuery,
  searchType,
} from "./searchQuery";

/** How an id lookup ended. 404 and timeout are different answers. */
type IdLookup =
  | { kind: "loading" }
  | { kind: "found"; entityType: string; name: string }
  | { kind: "missing" }
  | { kind: "timeout" };

interface Props {
  /** Opens the entity — the same sheet every other surface in the app uses. */
  onOpenEntity: (entityId: string) => void;
}

/** `2026-08-31T…` -> `2026-08-31`. Times are noise at this granularity. */
function day(iso: string | null): string {
  return iso ? iso.slice(0, 10) : "—";
}

export function GlobalSearch({ onOpenEntity }: Props) {
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState("");
  const [wide, setWide] = useState(false);
  /** The query actually SUBMITTED — what the results on screen describe. */
  const [submitted, setSubmitted] = useState<string | null>(null);
  const [results, setResults] = useState<Map<string, TypeResult>>(new Map());
  const [pending, setPending] = useState<Set<string>>(new Set());
  const [idLookup, setIdLookup] = useState<IdLookup | null>(null);
  const [elapsed, setElapsed] = useState(0);

  const inputRef = useRef<HTMLInputElement>(null);
  const abort = useRef<AbortController | null>(null);

  // Cmd/Ctrl-K opens from anywhere; Escape closes. Bound at the document so it
  // works on every route without each view knowing this component exists.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen(true);
      }
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  // A running seconds counter while queries are in flight. With 25-81s reads,
  // a spinner alone reads as a hang; the number is what says "still working".
  useEffect(() => {
    if (!pending.size && idLookup?.kind !== "loading") return;
    const started = Date.now();
    setElapsed(0);
    const id = setInterval(() => setElapsed(Math.round((Date.now() - started) / 1000)), 1000);
    return () => clearInterval(id);
  }, [pending.size, idLookup?.kind]);

  const run = useCallback(async () => {
    const raw = input.trim();
    if (!raw) return;

    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;

    const parsed = parseQuery(raw);
    setSubmitted(raw);
    setResults(new Map());
    setIdLookup(null);

    /**
     * THE ID FAST PATH. An `ent_` id is an exact match on one row, so it skips
     * the search entirely — no `search` query, one direct fetch. The operator
     * reads ids out of agent reports and daemon logs constantly, and pasting
     * one should never pay the 25-81s search cost.
     */
    if (parsed.entityId) {
      setIdLookup({ kind: "loading" });
      setPending(new Set());
      try {
        const res = await fetch(`/api/entity?id=${parsed.entityId}`, {
          signal: controller.signal,
        });
        const body = await res.json();
        /**
         * A MISSING ENTITY IS NOT A TIMEOUT, and the proxy does not make that
         * easy to see: `/api/entity` wraps every upstream failure as its own
         * 502, so a genuine "no such entity" arrives as
         * `502 {error: "Neotoma returned HTTP 404"}` rather than a 404 status.
         * Verified against the live route — checking `res.status === 404`
         * alone silently classified every missing id as a timeout, which is
         * the exact confusion this view exists to prevent.
         */
        const missing =
          res.status === 404 ||
          (typeof body.error === "string" && /\b404\b|not found/i.test(body.error));
        if (missing) {
          setIdLookup({ kind: "missing" });
          return;
        }
        if (!res.ok || body.error || !body.entity) {
          setIdLookup({ kind: "timeout" });
          return;
        }
        const entity = body.entity;
        const canonical: string = entity.canonical_name ?? parsed.entityId;
        const colon = canonical.indexOf(":");
        setIdLookup({
          kind: "found",
          entityType: entity.entity_type ?? "entity",
          name: colon > 0 ? canonical.slice(colon + 1).trim() : canonical,
        });
      } catch (err) {
        if ((err as Error).name !== "AbortError") setIdLookup({ kind: "timeout" });
      }
      return;
    }

    // Fan out one request PER TYPE, in parallel. Each settles on its own, so
    // the fast types render while the slow ones are still running rather than
    // the whole view waiting on the slowest.
    const types = wide ? [...DEFAULT_TYPES, ...EXTRA_TYPES] : [...DEFAULT_TYPES];
    setPending(new Set(types));

    await Promise.all(
      types.map(async (type) => {
        try {
          const result = await searchType(type, parsed, controller.signal);
          if (controller.signal.aborted) return;
          setResults((prev) => new Map(prev).set(type, result));
        } catch {
          // Aborted by a newer query — its results are the ones that matter.
        } finally {
          if (!controller.signal.aborted) {
            setPending((prev) => {
              const next = new Set(prev);
              next.delete(type);
              return next;
            });
          }
        }
      }),
    );
  }, [input, wide]);

  const openHit = (entityId: string) => {
    setOpen(false);
    onOpenEntity(entityId);
  };

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="ml-auto flex h-[24px] items-center gap-[6px] rounded-[6px] border border-border bg-background px-[8px] text-[12px] text-muted-foreground transition-colors hover:bg-accent"
        aria-label="Search all entities"
      >
        <Search className="h-[13px] w-[13px]" aria-hidden />
        <span className="max-[720px]:hidden">Search</span>
        <kbd className="ml-[2px] font-mono text-[11px] opacity-70 max-[720px]:hidden">⌘K</kbd>
      </button>
    );
  }

  const anyPending = pending.size > 0 || idLookup?.kind === "loading";
  const groups = [...results.entries()].filter(
    ([, r]) => r.kind !== "ok" || r.hits.length > 0,
  );
  const totalHits = [...results.values()].reduce(
    (n, r) => n + (r.kind === "ok" ? r.hits.length : 0),
    0,
  );
  // Types that answered definitively with nothing — a real finding, kept
  // separate from the ones that never answered.
  const empty = [...results.entries()].filter(
    ([, r]) => r.kind === "ok" && r.hits.length === 0,
  );

  return (
    <>
      {/* Click-away closes, same as Escape. */}
      <div
        className="fixed inset-0 z-40 bg-black/30"
        onClick={() => setOpen(false)}
        aria-hidden
      />
      <div className="fixed left-1/2 top-[52px] z-50 w-[min(760px,calc(100vw-32px))] -translate-x-1/2 overflow-hidden rounded-[10px] border border-border bg-card shadow-2xl">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void run();
          }}
          className="flex items-center gap-[8px] border-b border-border px-[12px] py-[9px]"
        >
          <Search className="h-[15px] w-[15px] flex-none text-muted-foreground" aria-hidden />
          <input
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Search entities, or paste an ent_ id — Enter to run"
            className="min-w-0 flex-1 bg-transparent text-[13px] outline-none placeholder:text-muted-foreground"
            aria-label="Search all entities"
          />
          {input && (
            <button
              type="button"
              onClick={() => {
                setInput("");
                inputRef.current?.focus();
              }}
              aria-label="Clear"
            >
              <X className="h-[14px] w-[14px] text-muted-foreground" aria-hidden />
            </button>
          )}
          <Button type="submit" size="chip" variant="chip" disabled={!input.trim()}>
            Search
          </Button>
        </form>

        <div className="flex items-center gap-[10px] border-b border-border px-[12px] py-[6px] text-[11px] text-muted-foreground">
          <span>
            {wide
              ? `${DEFAULT_TYPES.length + EXTRA_TYPES.length} types`
              : `${DEFAULT_TYPES.length} working types`}
          </span>
          <button
            type="button"
            className="underline underline-offset-2 hover:text-foreground"
            onClick={() => setWide((w) => !w)}
          >
            {wide ? "Narrow to working set" : "Widen scope"}
          </button>
          <span className="ml-auto font-mono opacity-70">field:value supported</span>
        </div>

        <div className="max-h-[min(60vh,520px)] overflow-y-auto">
          {submitted === null ? (
            <div className="px-[12px] py-[14px] text-[12px] text-muted-foreground">
              <p className="mb-[6px]">
                Searches run when you press Enter — never while typing. Reads from Neotoma
                currently take 25-81s, so a query per keystroke would overwhelm it.
              </p>
              <p>
                Multiple words must ALL match. Accents fold, so{" "}
                <span className="font-mono">theodore</span> finds{" "}
                <span className="font-mono">Theodóre</span>. Narrow with{" "}
                <span className="font-mono">status:pending</span> or{" "}
                <span className="font-mono">assigned_to:cicada</span>.
              </p>
            </div>
          ) : (
            <>
              {anyPending && (
                <div className="flex items-center gap-[8px] px-[12px] py-[10px] text-[12px] text-muted-foreground">
                  <Loader2 className="h-[13px] w-[13px] animate-spin" aria-hidden />
                  <span>
                    Searching{pending.size ? ` ${pending.size} more type${pending.size > 1 ? "s" : ""}` : ""}
                    … {elapsed}s
                  </span>
                  <span className="opacity-70">Neotoma reads take 25-81s.</span>
                </div>
              )}

              {/* THE ID FAST PATH — three distinct renderings. */}
              {idLookup?.kind === "found" && (
                <button
                  type="button"
                  onClick={() => openHit(parseQuery(submitted).entityId!)}
                  className="flex w-full items-center gap-[8px] px-[12px] py-[10px] text-left hover:bg-accent"
                >
                  <Badge variant="live" caps>
                    {idLookup.entityType}
                  </Badge>
                  <span className="min-w-0 flex-1 truncate text-[13px]">{idLookup.name}</span>
                  <span className="font-mono text-[11px] text-muted-foreground">exact id</span>
                </button>
              )}
              {idLookup?.kind === "missing" && (
                <p className="px-[12px] py-[12px] text-[12px]">
                  <strong>No entity with that id.</strong>{" "}
                  <span className="text-muted-foreground">
                    Neotoma answered — the id does not exist.
                  </span>
                </p>
              )}
              {idLookup?.kind === "timeout" && (
                <p className="px-[12px] py-[12px] text-[12px] text-[hsl(var(--warn))]">
                  <strong>The lookup did not come back.</strong>{" "}
                  <span className="text-muted-foreground">
                    This is not the same as the id not existing — nothing was learned. Try again.
                  </span>
                </p>
              )}

              {groups.map(([type, result]) => (
                <div key={type} className="border-t border-border first:border-t-0">
                  <div className="flex items-center gap-[8px] bg-muted/40 px-[12px] py-[5px]">
                    <span className="font-mono text-[11px] font-semibold">{type}</span>
                    {result.kind === "ok" && (
                      <span className="text-[11px] text-muted-foreground">
                        {result.hits.length}
                        {result.total !== null && result.total > result.hits.length
                          ? ` of ${result.saturated ? "at least " : ""}${result.total}`
                          : ""}
                      </span>
                    )}
                  </div>

                  {result.kind === "timeout" && (
                    <p className="px-[12px] py-[8px] text-[12px] text-[hsl(var(--warn))]">
                      Timed out — <span className="text-muted-foreground">
                        this type was not searched. It may or may not contain matches.
                      </span>
                    </p>
                  )}
                  {result.kind === "failed" && (
                    <p className="px-[12px] py-[8px] text-[12px] text-[hsl(var(--bad))]">
                      Query failed —{" "}
                      <span className="text-muted-foreground">{result.message}</span>
                    </p>
                  )}
                  {result.kind === "ok" &&
                    result.hits.map((hit: SearchHit) => (
                      <button
                        key={hit.entityId}
                        type="button"
                        onClick={() => openHit(hit.entityId)}
                        className="flex w-full items-center gap-[8px] px-[12px] py-[7px] text-left hover:bg-accent"
                      >
                        <span className="min-w-0 flex-1 truncate text-[13px]">{hit.name}</span>
                        {hit.status && (
                          <Badge variant="muted" caps>
                            {hit.status}
                          </Badge>
                        )}
                        <span className="flex-none font-mono text-[11px] tabular-nums text-muted-foreground">
                          {day(hit.updated)}
                        </span>
                      </button>
                    ))}
                </div>
              ))}

              {/* Types that answered with nothing. Rolled into one line so a
                  wide scope does not bury the actual hits under empty groups —
                  but still SHOWN, because "asked and found nothing" is a
                  finding the operator is entitled to see. */}
              {!anyPending && empty.length > 0 && (
                <p className="border-t border-border px-[12px] py-[8px] text-[11px] text-muted-foreground">
                  No matches in {empty.map(([t]) => t).join(", ")}.
                </p>
              )}

              {!anyPending && totalHits === 0 && !idLookup && empty.length > 0 && (
                <p className="border-t border-border px-[12px] py-[10px] text-[12px]">
                  <strong>No results for “{submitted}”.</strong>{" "}
                  <span className="text-muted-foreground">
                    Every type answered; none matched.
                  </span>
                </p>
              )}

              {/* `status` has no enum and most tasks carry ad-hoc spellings, so
                  a status-filtered count is a floor rather than a total. Say so
                  rather than presenting it as exhaustive. */}
              {parseQuery(submitted).filters.some((f) => f.field === "status") && (
                <p className="border-t border-border px-[12px] py-[8px] text-[11px] text-muted-foreground">
                  <strong>Note:</strong> <span className="font-mono">status</span> has no declared
                  enum and most tasks use ad-hoc spellings, so this is a lower bound, not every
                  matching entity.
                </p>
              )}
            </>
          )}
        </div>

        <div className="flex items-center gap-[10px] border-t border-border px-[12px] py-[5px] text-[11px] text-muted-foreground">
          <span>
            <kbd className="font-mono">Enter</kbd> search
          </span>
          <span>
            <kbd className="font-mono">Esc</kbd> close
          </span>
          <span className="ml-auto">Read-only</span>
        </div>
      </div>
    </>
  );
}
