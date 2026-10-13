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
  type Loadable,
  type ParsedArea,
  EXPECTED_AREA_COUNT,
  areaHasFields,
  decisionsWaiting,
  groupByAltitude,
  hasDuplicateMissions,
  missionStatement,
  parseCharter,
  resolveProjectRefs,
} from "./consoleData";
import { type FacetsResponse } from "./facets";
import { countText } from "./taskCount";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { AlertTriangle, Info } from "lucide-react";

const REFRESH_MS = 60_000;

/** Everything the page draws, so it can be rendered from fixtures without a network. */
export interface ConsoleModel {
  charter: Loadable<RecordsSection>;
  missions: Loadable<RecordsSection>;
  strategies: Loadable<RecordsSection>;
  retired: Loadable<CountSection>;
  projects: Loadable<RecordsSection>;
  /** `null` = the facets read failed or has not landed. */
  facets: FacetsResponse | null;
}

type Open = (entityId: string) => void;

// ── small shared pieces ─────────────────────────────────────────────────────

function Hint({ children }: { children: React.ReactNode }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          className="inline-flex text-muted-foreground hover:text-foreground"
          aria-label="More about this section"
        >
          <Info className="h-[13px] w-[13px]" aria-hidden />
        </button>
      </TooltipTrigger>
      <TooltipContent>{children}</TooltipContent>
    </Tooltip>
  );
}

function SectionTitle({
  children,
  hint,
  aside,
}: {
  children: React.ReactNode;
  hint?: React.ReactNode;
  aside?: React.ReactNode;
}) {
  return (
    <div className="mb-[8px] mt-5 flex items-center gap-[8px]">
      <h2 className="text-[14px] font-[650] tracking-[-0.01em]">{children}</h2>
      {hint && <Hint>{hint}</Hint>}
      {aside && <span className="ml-auto text-[12px] text-muted-foreground">{aside}</span>}
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

function DecisionsWaiting({ facets }: { facets: FacetsResponse | null }) {
  const count = decisionsWaiting(facets);
  return (
    <div className="flex items-center gap-[8px] rounded-lg border bg-card px-3 py-[8px] text-[12.5px]">
      <Badge variant={count.kind === "exact" && count.value > 0 ? "warn" : "muted"} caps>
        decisions waiting
      </Badge>
      <span className="text-[15px] font-[650] tabular-nums" data-testid="decisions-waiting">
        {countText(count)}
      </span>
      <Hint>
        Tasks in <code className="font-mono text-[11px]">awaiting_approval</code> or{" "}
        <code className="font-mono text-[11px]">awaiting_input</code>, counted across every open
        task. This is the global figure; per-area counts need the planning ascent and are not
        attributed yet.
      </Hint>
    </div>
  );
}

// ── mission ─────────────────────────────────────────────────────────────────

function MissionRecord({ mission, onOpen }: { mission: SpineRecord; onOpen: Open }) {
  const statement = missionStatement(mission.content);
  return (
    <div className="min-w-0">
      <div className="text-[14px] font-[650]">
        <RecordButton record={mission} onOpen={onOpen}>
          {recordLabel(mission)}
        </RecordButton>
      </div>
      <p className="mt-[5px] whitespace-pre-line text-[13px] leading-[1.5]">
        {statement ?? mission.description ?? "This record has no readable statement."}
      </p>
      {mission.content && statement && (
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
                console does not choose between them; both are shown in full. One record should
                be retired or merged.
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
  onOpen,
}: {
  strategies: Loadable<RecordsSection>;
  retired: Loadable<CountSection>;
  onOpen: Open;
}) {
  const groups = strategies.state === "ready" ? groupByAltitude(strategies.data.records) : [];
  return (
    <>
      <SectionTitle
        hint="Grouped by the altitude field on each strategy. The graph has no strategy-to-mission edge, so this order comes from that field alone."
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
      <div className="mt-2 text-[12px] text-muted-foreground">
        {retired.state === "ready"
          ? `${retired.data.total.toLocaleString()} retired or reference strategies not shown.`
          : retired.state === "error"
            ? "Retired strategy count could not be read."
            : "Counting retired strategies…"}
      </div>
    </>
  );
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
  const projectsKnown = projects.state === "ready";
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
              title={
                projectsKnown
                  ? "No project record has this title or name."
                  : "Project records could not be read, so this name was not looked up."
              }
            >
              {r.name}: {projectsKnown ? "no record" : "not looked up"}
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
        hint="Parsed from the responsibility charter's prose. Open work and decisions per area are not attributed yet: that needs the planning ascent from each area's projects."
        aside={record ? <RecordButton record={record} onOpen={onOpen}>The charter</RecordButton> : undefined}
      >
        What the swarm owns
      </SectionTitle>
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
      <Strategies strategies={model.strategies} retired={model.retired} onOpen={onOpenEntity} />
      <Areas charter={model.charter} projects={model.projects} onOpen={onOpenEntity} />
    </div>
  );
}

/**
 * Read one console section. A failure and a slow read are different states: the
 * previous good data is kept on screen across a refresh that fails, and the
 * error shows only when there is nothing better to show.
 */
function useSection<T>(section: string): Loadable<T> {
  const [value, setValue] = useState<Loadable<T>>({ state: "loading" });
  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const res = await fetch(`/api/console?section=${section}`);
        const body = await res.json();
        if (!res.ok || body.error || body.ok === false) {
          throw new Error(body.error ?? `HTTP ${res.status}`);
        }
        if (alive) setValue({ state: "ready", data: body as T });
      } catch (err) {
        if (alive) {
          setValue((prev) =>
            prev.state === "ready" ? prev : { state: "error", error: (err as Error).message },
          );
        }
      }
    };
    void load();
    const id = setInterval(() => void load(), REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [section]);
  return value;
}

export function Console({ onOpenEntity }: { onOpenEntity: Open }) {
  const charter = useSection<RecordsSection>("charter");
  const missions = useSection<RecordsSection>("missions");
  const strategies = useSection<RecordsSection>("strategies");
  const retired = useSection<CountSection>("retired");
  const projects = useSection<RecordsSection>("projects");

  const [facets, setFacets] = useState<FacetsResponse | null>(null);
  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const res = await fetch("/api/facets");
        const body = await res.json();
        if (!res.ok || body.error) throw new Error(body.error ?? `HTTP ${res.status}`);
        if (alive) setFacets(body);
      } catch {
        // Leave the last good figure; with none, the badge reads "not measured".
      }
    };
    void load();
    const id = setInterval(() => void load(), REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  return (
    <ConsoleView
      model={{ charter, missions, strategies, retired, projects, facets }}
      onOpenEntity={onOpenEntity}
    />
  );
}
