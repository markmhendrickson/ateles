/**
 * CONSOLE — OVERVIEW (slice 1)
 * ----------------------------
 * Mission, then the strategies under it, then the seven areas the swarm owns.
 * Read top to bottom it answers "what is this for, what is it pursuing, and what
 * is it responsible for". Design: console_design.md, section 2.1.
 *
 * EVERYTHING COMES FROM RECORDS THAT EXIST TODAY: the mission and strategy
 * records, the responsibility charter (a strategy), and project records. The
 * seven areas are parsed from the charter's prose (`consoleData.ts`), so each
 * limit of that is shown rather than papered over: a project name that matches
 * no record renders unresolved, an area the parser could not read says
 * "unparsed", and nothing is ever shown as "none" that is really "unread".
 *
 * EACH SECTION LOADS ON ITS OWN. Hosted reads run 5 to 80 seconds under load,
 * so the page renders each section as it lands and a failed one states its
 * failure while the rest stand.
 *
 * READ-ONLY, like every view here: GETs to /api/console and /api/facets only.
 */
import { useEffect, useState } from "react";
import type { CountSection, RecordsSection, SpineRecord } from "../server/consoleSpine";
import {
  type FetchOutcome,
  type Loadable,
  type ParsedArea,
  EXPECTED_AREA_COUNT,
  areaHasFields,
  decisionsWaiting,
  groupByAltitude,
  hasDuplicateMissions,
  interpretSectionResponse,
  missionOpening,
  missionStatement,
  otherStrategyCount,
  parseCharter,
  projectLookup,
  resolveProjectRefs,
  staleness,
} from "./consoleData";
import { type FacetsResponse } from "./facets";
import { countText } from "./taskCount";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { AlertTriangle } from "lucide-react";

const REFRESH_MS = 60_000;

/** Everything the page draws, so it can be rendered from fixtures without a network. */
export interface ConsoleModel {
  charter: Loadable<RecordsSection>;
  missions: Loadable<RecordsSection>;
  strategies: Loadable<RecordsSection>;
  retired: Loadable<CountSection>;
  /** Every strategy whatever its status, so the ones in other statuses can be counted. */
  allStrategies: Loadable<CountSection>;
  projects: Loadable<RecordsSection>;
  facets: Loadable<FacetsResponse>;
}

type Open = (entityId: string) => void;

// ── small shared pieces ─────────────────────────────────────────────────────

function SectionTitle({ children, aside }: { children: React.ReactNode; aside?: React.ReactNode }) {
  return (
    <div className="mb-[8px] mt-5 flex items-center gap-[8px]">
      <h2 className="text-[14px] font-[650] tracking-[-0.01em]">{children}</h2>
      {aside && <span className="ml-auto text-[12px] text-muted-foreground">{aside}</span>}
    </div>
  );
}

/** Shown when a refresh failed and the section is still drawing its last good data. */
function StaleNote({ sources }: { sources: Loadable<unknown>[] }) {
  const stale = staleness(sources);
  if (!stale) return null;
  return (
    <div role="status" data-testid="stale" className="mb-[6px] text-[12px] text-[hsl(var(--warn))]">
      Showing data as of {stale.asOf ?? "an unknown time"}. The latest refresh failed.
    </div>
  );
}

function SectionSkeleton({ lines = 3 }: { lines?: number }) {
  return (
    <div className="rounded-lg border bg-card p-3" aria-busy="true" data-testid="section-loading">
      {Array.from({ length: lines }, (_, i) => (
        <Skeleton key={i} className="mb-[7px] h-[13px] w-[min(90%,520px)] last:mb-0" />
      ))}
    </div>
  );
}

/** A failed read. Says so; never renders as an empty section. */
function SectionError({ what, error }: { what: string; error: string }) {
  return (
    <div
      role="alert"
      className="rounded-lg border border-[hsl(var(--bad)/0.35)] bg-[hsl(var(--bad)/0.10)] px-3 py-[9px] text-[12.5px]"
    >
      <strong>{what} could not be read.</strong>{" "}
      <span className="text-muted-foreground">{error} This is not an empty result.</span>
    </div>
  );
}

function RecordButton({
  record,
  onOpen,
  children,
}: {
  record: Pick<SpineRecord, "id">;
  onOpen: Open;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={() => onOpen(record.id)}
      className="text-left font-[600] text-live underline decoration-dotted underline-offset-2 hover:opacity-80"
    >
      {children}
    </button>
  );
}

const recordLabel = (r: SpineRecord) => r.title ?? r.name ?? r.id;

// ── decisions waiting ───────────────────────────────────────────────────────

function DecisionsWaiting({ facets }: { facets: Loadable<FacetsResponse> }) {
  const count = facets.state === "ready" ? decisionsWaiting(facets.data) : ({ kind: "unmeasured" } as const);
  const text = facets.state === "loading" ? "loading" : countText(count);
  const stale = staleness([facets]);
  return (
    <div className="flex items-center gap-[8px] rounded-lg border bg-card px-3 py-[8px] text-[12.5px]">
      <Badge variant={count.kind === "exact" && count.value > 0 ? "warn" : "muted"} caps>
        decisions waiting
      </Badge>
      <span className="text-[15px] font-[650] tabular-nums" data-testid="decisions-waiting">
        {text}
      </span>
      {stale && (
        <span role="status" data-testid="stale" className="text-[12px] text-[hsl(var(--warn))]">
          as of {stale.asOf ?? "an unknown time"}, refresh failed
        </span>
      )}
    </div>
  );
}

// ── mission ─────────────────────────────────────────────────────────────────

function MissionRecord({ mission, onOpen }: { mission: SpineRecord; onOpen: Open }) {
  const statement = missionStatement(mission.content);
  // No THE MISSION heading: show the record's own first paragraph, and say it is
  // that, rather than "no readable statement" over a record that holds text.
  const opening = statement === null ? missionOpening(mission.content) : null;
  return (
    <div className="min-w-0">
      <div className="text-[14px] font-[650]">
        <RecordButton record={mission} onOpen={onOpen}>
          {recordLabel(mission)}
        </RecordButton>
      </div>
      {opening !== null && (
        <div className="mt-[5px] text-[11px] text-muted-foreground">Opening of the record text</div>
      )}
      <p className="mt-[5px] whitespace-pre-line text-[13px] leading-[1.5]">
        {statement ?? opening ?? mission.description ?? "This record has no readable statement."}
      </p>
      {mission.content && (
        <details className="mt-[6px] text-[12px] text-muted-foreground">
          <summary className="cursor-pointer">Full record text</summary>
          <p className="mt-[6px] whitespace-pre-line leading-[1.5]">{mission.content}</p>
        </details>
      )}
      <div className="mt-[4px] font-mono text-[11px] text-muted-foreground">{mission.id}</div>
    </div>
  );
}

function Mission({ missions, onOpen }: { missions: Loadable<RecordsSection>; onOpen: Open }) {
  return (
    <>
      <SectionTitle>Mission</SectionTitle>
      <StaleNote sources={[missions]} />
      {missions.state === "loading" ? (
        <SectionSkeleton />
      ) : missions.state === "error" ? (
        <SectionError what="The mission" error={missions.error} />
      ) : missions.data.records.length === 0 ? (
        <div className="rounded-lg border bg-card p-3 text-[13px] text-muted-foreground">
          No active mission record exists.
        </div>
      ) : (
        <div className="rounded-lg border bg-card p-3">
          {hasDuplicateMissions(missions.data.records) && (
            <div
              role="alert"
              className="mb-3 flex items-start gap-[8px] rounded-md border border-[hsl(var(--warn)/0.4)] bg-[hsl(var(--warn)/0.10)] px-[10px] py-[7px] text-[12.5px]"
            >
              <AlertTriangle className="mt-[2px] h-[14px] w-[14px] flex-none text-[hsl(var(--warn))]" aria-hidden />
              <span>
                <strong>{missions.data.records.length} active mission records.</strong> The
                console does not choose between them; all {missions.data.records.length} are shown
                in full. Only one should be active.
              </span>
            </div>
          )}
          <div className="flex flex-col gap-4">
            {missions.data.records.map((m) => (
              <MissionRecord key={m.id} mission={m} onOpen={onOpen} />
            ))}
          </div>
        </div>
      )}
    </>
  );
}

// ── strategies ──────────────────────────────────────────────────────────────

function Strategies({
  strategies,
  retired,
  allStrategies,
  onOpen,
}: {
  strategies: Loadable<RecordsSection>;
  retired: Loadable<CountSection>;
  allStrategies: Loadable<CountSection>;
  onOpen: Open;
}) {
  const groups = strategies.state === "ready" ? groupByAltitude(strategies.data.records) : [];
  return (
    <>
      <SectionTitle
        aside={
          strategies.state === "ready"
            ? `${strategies.data.records.length} active${
                strategies.data.total !== null && strategies.data.total > strategies.data.records.length
                  ? ` of ${strategies.data.total}`
                  : ""
              }`
            : undefined
        }
      >
        Strategies
      </SectionTitle>
      <StaleNote sources={[strategies, retired, allStrategies]} />
      {strategies.state === "loading" ? (
        <SectionSkeleton lines={5} />
      ) : strategies.state === "error" ? (
        <SectionError what="The strategies" error={strategies.error} />
      ) : groups.length === 0 ? (
        <div className="rounded-lg border bg-card p-3 text-[13px] text-muted-foreground">
          No active strategy records exist.
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          {groups.map((g) => (
            <div key={g.altitude} className="rounded-lg border bg-card p-3">
              <div className="mb-[6px] flex items-center gap-[8px]">
                <Badge variant="muted" caps>
                  {g.altitude.replace(/_/g, " ")}
                </Badge>
                <span className="text-[11px] text-muted-foreground">{g.strategies.length}</span>
              </div>
              <ul className="space-y-[7px]">
                {g.strategies.map((s) => (
                  <li key={s.id} className="text-[13px]">
                    <RecordButton record={s} onOpen={onOpen}>
                      {recordLabel(s)}
                    </RecordButton>
                    {s.role === "responsibility_charter" && (
                      <Badge variant="live" className="ml-[6px] text-[10px]" caps>
                        charter
                      </Badge>
                    )}
                    {s.domain && (
                      <span className="ml-[6px] text-[11px] text-muted-foreground">{s.domain}</span>
                    )}
                    {s.description && (
                      <p className="mt-[2px] text-[12px] leading-[1.45] text-muted-foreground">
                        {s.description}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
      <div className="mt-2 text-[12px] text-muted-foreground" data-testid="strategy-footer">
        Not shown: {retiredText(retired)}, {otherText(strategies, retired, allStrategies)}.
      </div>
    </>
  );
}

function retiredText(retired: Loadable<CountSection>): string {
  if (retired.state === "ready") return `${retired.data.total.toLocaleString()} retired or reference`;
  return retired.state === "error" ? "retired count could not be read" : "retired count loading";
}

/** Strategies in a status other than active or retired. Never a guess: loading, failed, or a number. */
function otherText(
  strategies: Loadable<RecordsSection>,
  retired: Loadable<CountSection>,
  all: Loadable<CountSection>,
): string {
  if (strategies.state === "error" || retired.state === "error" || all.state === "error") {
    return "other count could not be read";
  }
  if (strategies.state !== "ready" || retired.state !== "ready" || all.state !== "ready") {
    return "other count loading";
  }
  const other = otherStrategyCount(all.data.total, strategies.data.total, retired.data.total);
  return other === null ? "other count could not be worked out" : `${other.toLocaleString()} other`;
}

// ── areas ───────────────────────────────────────────────────────────────────

/**
 * A labelled list. Three states, kept apart: items listed, "none yet" (the
 * charter says so), and not listed (the charter's text for this area has no such
 * line). The last is a fact about the charter, not a parse failure; a parse
 * failure of the whole area is flagged on the card, see `AreaCard`.
 */
function ListField({
  label,
  items,
  none,
}: {
  label: string;
  items: string[] | null;
  none?: boolean | null;
}) {
  return (
    <div className="text-[12.5px]">
      <span className="mr-[6px] font-[600] text-muted-foreground">{label}</span>
      {items === null ? (
        <span className="text-muted-foreground">not listed</span>
      ) : items.length === 0 ? (
        <span className="text-muted-foreground">{none ? "none yet" : "none listed"}</span>
      ) : (
        <ul className="mt-[3px] list-disc space-y-[2px] pl-[18px]">
          {items.map((i) => (
            <li key={i}>{i}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ProjectChips({
  area,
  projects,
  onOpen,
}: {
  area: ParsedArea;
  projects: Loadable<RecordsSection>;
  onOpen: Open;
}) {
  if (area.projectsNone === null) {
    return (
      <div className="text-[12.5px]">
        <span className="mr-[6px] font-[600] text-muted-foreground">Projects</span>
        <span className="text-muted-foreground">not listed</span>
      </div>
    );
  }
  if (area.projectsNone) {
    return (
      <div className="text-[12.5px]">
        <span className="mr-[6px] font-[600] text-muted-foreground">Projects</span>
        <Badge variant="bad" className="text-[10px]" caps>
          no project
        </Badge>
      </div>
    );
  }
  const refs = resolveProjectRefs(
    area.projectNames,
    projects.state === "ready" ? projects.data.records : [],
  );
  const lookup = projectLookup(projects);
  const chipNote = {
    loading: "loading",
    failed: "not looked up",
    partial: "not checked",
    complete: "no record",
  }[lookup];
  const chipTitle = {
    loading: "Project records are still loading.",
    failed: "Project records could not be read, so this name was not looked up.",
    partial:
      projects.state === "ready"
        ? `Only ${projects.data.records.length} of ${projects.data.total ?? "an unknown number of"} project records were read, so this name was not checked.`
        : "",
    complete: "No project record has this title or name.",
  }[lookup];
  return (
    <div className="text-[12.5px]">
      <span className="mr-[6px] font-[600] text-muted-foreground">Projects</span>
      <span className="inline-flex flex-wrap gap-[5px] align-middle">
        {refs.map((r) =>
          r.resolvedId ? (
            <button
              key={r.name}
              type="button"
              onClick={() => onOpen(r.resolvedId as string)}
              className="rounded-full border px-[8px] py-[1px] font-mono text-[11px] text-live hover:opacity-80"
            >
              {r.name}
            </button>
          ) : (
            <span
              key={r.name}
              className="rounded-full border border-dashed px-[8px] py-[1px] font-mono text-[11px] text-muted-foreground"
              title={chipTitle}
            >
              {r.name}: {chipNote}
            </span>
          ),
        )}
        {area.projectOther.map((t) => (
          <span
            key={t}
            className="rounded-full border border-dashed px-[8px] py-[1px] text-[11px] text-muted-foreground"
            title="Written in the charter without a project name, so it cannot be matched."
          >
            {t}: unnamed
          </span>
        ))}
      </span>
    </div>
  );
}

function AreaCard({
  area,
  projects,
  onOpen,
}: {
  area: ParsedArea;
  projects: Loadable<RecordsSection>;
  onOpen: Open;
}) {
  if (!area.parsed) {
    return (
      <article className="rounded-lg border border-[hsl(var(--warn)/0.4)] bg-card p-3" data-testid="area-unparsed">
        <div className="flex items-center gap-[8px]">
          <h3 className="text-[13.5px] font-[650]">{area.name}</h3>
          <Badge variant="warn" className="text-[10px]" caps>
            unparsed
          </Badge>
        </div>
        <p className="mt-[5px] text-[12px] text-muted-foreground">
          This heading does not match the expected form (<code className="font-mono">### N. Area</code>),
          so its fields were not read. The text is kept below.
        </p>
        {area.raw && (
          <details className="mt-[6px] text-[12px] text-muted-foreground">
            <summary className="cursor-pointer">Raw text</summary>
            <p className="mt-[6px] whitespace-pre-line leading-[1.5]">{area.raw}</p>
          </details>
        )}
      </article>
    );
  }
  // A heading that parsed but whose text yielded no recognised label at all: the
  // body was reworded, not empty. Say so and keep the text, rather than showing
  // a card of "not listed" lines that reads as a statement about the area.
  if (!areaHasFields(area)) {
    return (
      <article className="rounded-lg border border-[hsl(var(--warn)/0.4)] bg-card p-3" data-testid="area-unparsed">
        <div className="flex items-center gap-[8px]">
          <span className="font-mono text-[12px] text-muted-foreground">{area.number}</span>
          <h3 className="text-[14px] font-[650] tracking-[-0.01em]">{area.name}</h3>
          <Badge variant="warn" className="text-[10px]" caps>
            unparsed
          </Badge>
        </div>
        <p className="mt-[5px] text-[12px] text-muted-foreground">
          None of this area&apos;s labels (Owns, Projects, Strategies, Workflows, Missing) were
          recognised, so its fields were not read. The text is kept below.
        </p>
        {area.raw && (
          <details className="mt-[6px] text-[12px] text-muted-foreground">
            <summary className="cursor-pointer">Raw text</summary>
            <p className="mt-[6px] whitespace-pre-line leading-[1.5]">{area.raw}</p>
          </details>
        )}
      </article>
    );
  }
  return (
    <article className="rounded-lg border bg-card p-3" data-testid="area">
      <div className="flex items-center gap-[8px]">
        <span className="font-mono text-[12px] text-muted-foreground">{area.number}</span>
        <h3 className="text-[14px] font-[650] tracking-[-0.01em]">{area.name}</h3>
      </div>
      <div className="mt-[6px] text-[13px] leading-[1.5]">
        {area.owns ? (
          <>
            <span className="mr-[6px] text-[12.5px] font-[600] text-muted-foreground">Owns</span>
            {area.owns}
          </>
        ) : (
          <>
            <span className="mr-[6px] text-[12.5px] font-[600] text-muted-foreground">Owns</span>
            <Badge variant="warn" className="text-[10px]" caps>
              unparsed
            </Badge>
          </>
        )}
      </div>
      <div className="mt-[8px] flex flex-col gap-[6px]">
        <ProjectChips area={area} projects={projects} onOpen={onOpen} />
        <ListField label="Strategies" items={area.strategies} />
        <ListField label="Workflows" items={area.workflows} none={area.workflowsNone} />
        <ListField label="Missing" items={area.missing} />
      </div>
    </article>
  );
}

function Areas({
  charter,
  projects,
  onOpen,
}: {
  charter: Loadable<RecordsSection>;
  projects: Loadable<RecordsSection>;
  onOpen: Open;
}) {
  const record = charter.state === "ready" ? (charter.data.records[0] ?? null) : null;
  const parse = record ? parseCharter(record.content) : null;
  const extraCharters = charter.state === "ready" ? charter.data.records.length - 1 : 0;

  return (
    <>
      <SectionTitle
        aside={record ? <RecordButton record={record} onOpen={onOpen}>The charter</RecordButton> : undefined}
      >
        What the swarm owns
      </SectionTitle>
      <StaleNote sources={[charter, projects]} />
      {charter.state === "loading" ? (
        <SectionSkeleton lines={6} />
      ) : charter.state === "error" ? (
        <SectionError what="The responsibility charter" error={charter.error} />
      ) : !record || !parse ? (
        <div className="rounded-lg border bg-card p-3 text-[13px] text-muted-foreground" role="alert">
          No active responsibility charter was found, so the areas cannot be shown.
        </div>
      ) : (
        <>
          {(parse.problems.length > 0 || extraCharters > 0) && (
            <div
              role="alert"
              className="mb-3 flex items-start gap-[8px] rounded-md border border-[hsl(var(--warn)/0.4)] bg-[hsl(var(--warn)/0.10)] px-[10px] py-[7px] text-[12.5px]"
            >
              <AlertTriangle className="mt-[2px] h-[14px] w-[14px] flex-none text-[hsl(var(--warn))]" aria-hidden />
              <div>
                {extraCharters > 0 && (
                  <div>
                    {extraCharters + 1} active charters exist; the most recently changed is read.
                  </div>
                )}
                {parse.problems.map((p) => (
                  <div key={p}>{p}</div>
                ))}
              </div>
            </div>
          )}
          <div className="grid gap-3 min-[900px]:grid-cols-2">
            {parse.areas.map((a, i) => (
              <AreaCard key={`${a.number ?? "x"}-${i}`} area={a} projects={projects} onOpen={onOpen} />
            ))}
            {parse.missingNumbers.map((n) => (
                <article
                  key={`missing-${n}`}
                  className="rounded-lg border border-dashed p-3 text-[12.5px] text-muted-foreground"
                  data-testid="area-missing"
                >
                  Area {n} of {EXPECTED_AREA_COUNT} was not found under a recognised heading.
                </article>
              ))}
          </div>
        </>
      )}
    </>
  );
}

// ── page ────────────────────────────────────────────────────────────────────

/** The page, drawn from a model. Pure of effects so it renders from fixtures. */
export function ConsoleView({ model, onOpenEntity }: { model: ConsoleModel; onOpenEntity: Open }) {
  return (
    <div>
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-[16px] font-[650] tracking-[-0.01em]">Console</h1>
        <span className="text-[12px] text-muted-foreground">Read-only. Mission to responsibility.</span>
        <div className="ml-auto">
          <DecisionsWaiting facets={model.facets} />
        </div>
      </div>
      <Mission missions={model.missions} onOpen={onOpenEntity} />
      <Strategies
        strategies={model.strategies}
        retired={model.retired}
        allStrategies={model.allStrategies}
        onOpen={onOpenEntity}
      />
      <Areas charter={model.charter} projects={model.projects} onOpen={onOpenEntity} />
    </div>
  );
}

/**
 * Fetch one JSON endpoint and reduce what came back to a `FetchOutcome`. A
 * network failure is `null`; a body that is not JSON is `body: undefined`.
 */
async function fetchOutcome(url: string): Promise<FetchOutcome | null> {
  try {
    const res = await fetch(url);
    let body: unknown;
    try {
      body = await res.json();
    } catch {
      body = undefined;
    }
    return { status: res.status, ok: res.ok, body };
  } catch {
    return null;
  }
}

/**
 * Read one endpoint on load and every minute. A failure and a slow read are
 * different states: `interpretSectionResponse` keeps the previous good data on
 * screen across a refresh that fails (marked stale), and shows the error only
 * when there is nothing better to show.
 */
function useEndpoint<T>(url: string): Loadable<T> {
  const [value, setValue] = useState<Loadable<T>>({ state: "loading" });
  useEffect(() => {
    let alive = true;
    const load = async () => {
      const outcome = await fetchOutcome(url);
      if (alive) setValue((prev) => interpretSectionResponse<T>(outcome, prev));
    };
    void load();
    const id = setInterval(() => void load(), REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [url]);
  return value;
}

export function Console({ onOpenEntity }: { onOpenEntity: Open }) {
  const charter = useEndpoint<RecordsSection>("/api/console?section=charter");
  const missions = useEndpoint<RecordsSection>("/api/console?section=missions");
  const strategies = useEndpoint<RecordsSection>("/api/console?section=strategies");
  const retired = useEndpoint<CountSection>("/api/console?section=retired");
  const allStrategies = useEndpoint<CountSection>("/api/console?section=allstrategies");
  const projects = useEndpoint<RecordsSection>("/api/console?section=projects");
  const facets = useEndpoint<FacetsResponse>("/api/facets");

  return (
    <ConsoleView
      model={{ charter, missions, strategies, retired, allStrategies, projects, facets }}
      onOpenEntity={onOpenEntity}
    />
  );
}
