/**
 * AGENT DIRECTORY
 * ---------------
 * A browsable roster of every agent in the swarm, grouped by tier — the swarm's
 * primary structural axis. Clicking an agent opens its full definition.
 *
 * Tier grouping rather than a flat list because tier is what tells you how an
 * agent runs: a T3 daemon is always on, a T4 is a subprocess that exists for
 * one task. Two agents with similar descriptions behave completely differently
 * depending on which tier they sit in.
 *
 * Data comes from `/api/agents` — the same token-stays-in-Node proxy the task
 * view uses. Nothing here reads a local file or a fixture, and nothing writes.
 */
import { useEffect, useMemo, useState } from "react";
import {
  type Agent,
  type AgentEntity,
  type TierGroup,
  TIERS,
  TIER_BLURBS,
  TIER_LABELS,
  parseAgent,
  statusTone,
  truncate,
} from "./agents";
import { type Task, type TaskEntity, entityUrl, parseTask, relativeTime, toBucket } from "./tasks";
import { isSpawnable } from "./taskState";
import { Markdown } from "./Markdown";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { AgentDetailSkeleton, AgentDirectorySkeleton } from "@/components/Skeletons";
import { showSkeleton } from "@/lib/loading";
import { ArrowLeft, ExternalLink } from "lucide-react";

type TierFilter = TierGroup | "all";

interface Props {
  /** Entity id of the agent to show in detail, or null for the list. */
  selected: string | null;
  onSelect: (id: string | null) => void;
  /** Opens one task in the entity sheet, from the assigned-tasks section. */
  onOpenEntity: (id: string) => void;
}

/** Tier badge tone, preserving the original per-tier colours. */
const TIER_TONE: Record<TierGroup, "purple" | "live" | "ok" | "warn" | "muted"> = {
  T1: "purple",
  T2: "live",
  T3: "ok",
  T4: "warn",
  other: "muted",
};

const STATUS_TONE = { ok: "ok", pending: "warn", off: "muted" } as const;

export function AgentDirectory({ selected, onSelect, onOpenEntity }: Props) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [error, setError] = useState<string | null>(null);
  /**
   * True until the first fetch settles. As on the tasks side, skeletons key off
   * first-load-only; this view fetches once on mount rather than polling, but
   * the same rule keeps a remount from flashing over data already rendered.
   */
  const [firstLoadDone, setFirstLoadDone] = useState(false);
  const [tier, setTier] = useState<TierFilter>("all");

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const res = await fetch("/api/agents?limit=200");
        const body = await res.json();
        if (!res.ok || body.error) throw new Error(body.error ?? `HTTP ${res.status}`);
        if (!live) return;
        const parsed = (body.entities as AgentEntity[]).map(parseAgent);
        // Alphabetical: the server returns entity-id order, which is arbitrary
        // to a reader scanning for a name.
        parsed.sort((a, b) => a.name.localeCompare(b.name));
        setAgents(parsed);
        setError(null);
      } catch (err) {
        if (live) setError((err as Error).message);
      } finally {
        if (live) setFirstLoadDone(true);
      }
    })();
    return () => {
      live = false;
    };
  }, []);

  const counts = useMemo(() => {
    const c: Record<string, number> = { all: agents.length };
    for (const t of TIERS) c[t] = 0;
    for (const a of agents) c[a.tierGroup]++;
    return c;
  }, [agents]);

  const current = useMemo(
    () => (selected ? (agents.find((a) => a.id === selected) ?? null) : null),
    [agents, selected],
  );

  const pending = showSkeleton(!firstLoadDone, agents.length > 0);

  if (selected) {
    // A long definition deserves a document-shaped placeholder rather than a
    // bare "Loading…" that then drops ~10k characters onto the page.
    if (pending) return <AgentDetailSkeleton />;
    if (!current) {
      return (
        <div className="p-6 text-center text-muted-foreground">
          <p>No agent with that id in the roster.</p>
          <Button variant="chip" size="chip" onClick={() => onSelect(null)}>
            Back to directory
          </Button>
        </div>
      );
    }
    return (
      <AgentDetail agent={current} onBack={() => onSelect(null)} onOpenEntity={onOpenEntity} />
    );
  }

  // Only render groups that have members, so empty tiers do not pad the page.
  const groups = (tier === "all" ? TIERS : [tier]).filter((t) => counts[t] > 0);

  return (
    <>
      <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h1 className="m-0 text-[16px] font-[650] tracking-[-0.01em]">Agent directory</h1>
        <p className="m-0 text-[12px] text-muted-foreground">
          {pending ? "Loading…" : `${agents.length} agents in the swarm`}
        </p>
      </header>

      {error && (
        <div className="my-4 rounded-lg border border-[hsl(var(--bad)/0.35)] bg-[hsl(var(--bad)/0.12)] px-3 py-[10px] text-[13px]">
          <strong>Cannot reach Neotoma.</strong> {error}
        </div>
      )}

      {pending ? (
        <AgentDirectorySkeleton />
      ) : (
        <>
          <nav className="my-[10px] flex flex-wrap gap-[5px]">
            {(["all", ...TIERS] as TierFilter[])
              .filter((t) => t === "all" || counts[t] > 0)
              .map((t) => (
                <Tooltip key={t}>
                  <TooltipTrigger asChild>
                    <Button
                      variant="chip"
                      size="chip"
                      active={tier === t}
                      onClick={() => setTier(t)}
                    >
                      {t === "all" ? "All" : TIER_LABELS[t]}
                      <span className="tabular-nums opacity-80">{counts[t] ?? 0}</span>
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>
                    {t === "all" ? "Every agent" : TIER_BLURBS[t]}
                  </TooltipContent>
                </Tooltip>
              ))}
          </nav>

          {/*
           * One table per tier. The roster is tabular — tier, name, genus,
           * status, description — so the per-agent card became a row, and the
           * field names moved into column headers instead of being implied
           * again on every line.
           */}
          {groups.map((t) => (
            <section key={t} className="mt-[14px]">
              <div className="mb-[3px] flex flex-wrap items-baseline gap-[8px]">
                <h2 className="m-0 text-[12.5px] font-[650] tracking-[-0.01em]">
                  {TIER_LABELS[t]}
                </h2>
                <span className="text-[11px] text-muted-foreground">{TIER_BLURBS[t]}</span>
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
                  {agents
                    .filter((a) => a.tierGroup === t)
                    .map((a) => (
                      <tr
                        key={a.id}
                        className="cursor-pointer border-b border-border/60 hover:bg-accent/60"
                        onClick={() => onSelect(a.id)}
                      >
                        <td className="py-[4px] pr-2 align-baseline">
                          <Badge variant={TIER_TONE[a.tierGroup]} className="font-[650]">
                            {a.tier || "—"}
                          </Badge>
                        </td>
                        <td className="py-[4px] pr-2 align-baseline">
                          <button
                            type="button"
                            className="cursor-pointer border-none bg-transparent p-0 text-left font-medium text-inherit hover:text-live hover:underline"
                            onClick={() => onSelect(a.id)}
                          >
                            {a.name}
                          </button>
                        </td>
                        <td className="py-[4px] pr-2 align-baseline italic text-muted-foreground">
                          {a.genus ?? ""}
                        </td>
                        <td className="max-w-0 truncate py-[4px] pr-2 align-baseline text-muted-foreground">
                          {truncate(a.description) || "No description."}
                        </td>
                        <td className="py-[4px] align-baseline">
                          <Badge variant={STATUS_TONE[statusTone(a.status)]} caps>
                            {a.status}
                          </Badge>
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </section>
          ))}

          {!agents.length && !error && (
            <p className="p-6 text-center text-muted-foreground">No agent definitions found.</p>
          )}
        </>
      )}
    </>
  );
}

/** One labelled list of values, omitted entirely when the field is empty. */
function Chips({ label, values }: { label: string; values: string[] }) {
  if (!values.length) return null;
  return (
    <div className="my-[18px]">
      <span className="mb-[6px] block text-[10px] uppercase tracking-[.06em] text-muted-foreground">
        {label}
      </span>
      <div className="flex flex-wrap gap-[6px]">
        {values.map((v) => (
          <Badge key={v} variant="tag">
            {v}
          </Badge>
        ))}
      </div>
    </div>
  );
}

/** One labelled scalar, omitted when unset. */
function Fact({ label, value }: { label: string; value: string | null }) {
  if (!value) return null;
  return (
    /* Label and value on ONE line rather than stacked: two lines per fact made
       the strip twice as tall as the facts needed. */
    <div className="flex items-baseline gap-[5px]">
      <span className="text-[10px] uppercase tracking-[.06em] text-muted-foreground">
        {label}
      </span>
      <span className="text-[12px] tabular-nums">{value}</span>
    </div>
  );
}

function AgentDetail({
  agent,
  onBack,
  onOpenEntity,
}: {
  agent: Agent;
  onBack: () => void;
  onOpenEntity: (id: string) => void;
}) {
  return (
    <article>
      <Button variant="outline" size="sm" className="mb-[14px]" onClick={onBack}>
        <ArrowLeft className="h-[14px] w-[14px]" aria-hidden />
        Directory
      </Button>

      <header className="flex items-start justify-between gap-4">
        <div>
          <h1 className="m-0 text-[20px] tracking-[-0.01em]">{agent.name}</h1>
          <p className="mb-0 mt-[2px] flex items-center gap-2 text-[13px] text-muted-foreground">
            <Badge variant={TIER_TONE[agent.tierGroup]} className="font-[650]">
              {agent.tier || "—"}
            </Badge>
            {TIER_BLURBS[agent.tierGroup]}
          </p>
        </div>
        <Badge variant={STATUS_TONE[statusTone(agent.status)]} caps>
          {agent.status}
        </Badge>
      </header>

      <div className="my-[8px] flex flex-wrap gap-x-[18px] gap-y-[3px] rounded-[7px] border bg-card px-[10px] py-[6px]">
        <Fact label="Genus" value={agent.genus} />
        <Fact label="Version" value={agent.version} />
        <Fact label="Grant" value={agent.agentGrant} />
        <Fact label="AAuth sub" value={agent.aauthSub} />
        <Fact label="Updated" value={relativeTime(agent.updatedAt)} />
      </div>

      {agent.description && (
        <div className="my-[18px]">
          <span className="mb-[6px] block text-[10px] uppercase tracking-[.06em] text-muted-foreground">
            Description
          </span>
          <p className="m-0 text-[13px] leading-[1.6]">{agent.description}</p>
        </div>
      )}

      {agent.notes && (
        <div className="my-[18px]">
          <span className="mb-[6px] block text-[10px] uppercase tracking-[.06em] text-muted-foreground">
            Notes
          </span>
          <p className="m-0 text-[13px] leading-[1.6] text-muted-foreground">{agent.notes}</p>
        </div>
      )}

      {/* The allowlist coercer in agents.ts is what makes these render at all:
          `tool_allowlist` arrives as an array on most entities, a JSON-encoded
          string on one, and null on others. */}
      <Chips label="Tool allowlist" values={agent.toolAllowlist} />
      <Chips label="Context entity types" values={agent.contextEntityTypes} />
      <Chips label="Operational entity types" values={agent.operationalEntityTypes} />

      {agent.promptMarkdown && (
        <div className="my-[18px]">
          <span className="mb-[6px] block text-[10px] uppercase tracking-[.06em] text-muted-foreground">
            Prompt
          </span>
          {/* Long — up to ~10k chars — so it scrolls in its own region rather
              than pushing the metadata above it off the top of the page. */}
          <ScrollArea className="max-h-[60vh] rounded-[10px] border bg-card">
            <div className="px-4">
              <Markdown source={agent.promptMarkdown} />
            </div>
          </ScrollArea>
        </div>
      )}

      <AssignedTasks agent={agent} onOpenEntity={onOpenEntity} />

      <Separator className="my-[22px]" />

      <p className="m-0 text-[13px]">
        <a
          href={entityUrl(agent.id)}
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

/**
 * WHAT THIS AGENT IS RESPONSIBLE FOR RIGHT NOW.
 *
 * Every `task` whose `assigned_to` names this agent, newest first, fetched from
 * `/api/assigned` — filtered SERVER-SIDE, because there are 20,922 tasks and
 * pulling them all to filter in the browser is not an option.
 *
 * ZERO IS THE COMMON ANSWER AND IS STATED AS ONE. Only ~32 tasks in a 500-task
 * sample carry any `assigned_to`, so most agents genuinely own nothing. That is
 * rendered as the sentence "No tasks assigned", never as an empty region a
 * reader would take for a failed load.
 *
 * THE RATIO IS THE POINT. `assigned_to` is a free-text role name that nothing
 * validates, so an agent outside Apis's route table can accumulate tasks that
 * look dispatched and will never run. Where this agent is not spawnable, the
 * count of its tasks IS the count of stranded work, and the panel says so.
 *
 * Read-only, like every other surface here.
 */
function AssignedTasks({
  agent,
  onOpenEntity,
}: {
  agent: Agent;
  onOpenEntity: (id: string) => void;
}) {
  const [tasks, setTasks] = useState<Task[] | null>(null);
  /** Distinguishes "not loaded" from "loaded, and there are none". */
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    setTasks(null);
    setFailed(false);
    (async () => {
      try {
        const res = await fetch(`/api/assigned?to=${encodeURIComponent(agent.name)}&limit=100`);
        const body = await res.json();
        if (!alive) return;
        if (!res.ok || body.error) throw new Error(body.error ?? `HTTP ${res.status}`);
        setTasks((body.entities as TaskEntity[]).map(parseTask));
      } catch {
        // Non-fatal: the definition above is unaffected by a task list that
        // could not be read, and a broken panel would be worse than a caveat.
        if (alive) setFailed(true);
      }
    })();
    return () => {
      alive = false;
    };
  }, [agent.name]);

  const spawnable = isSpawnable(agent.name);
  const open = tasks?.filter((t) => t.bucket !== "done") ?? [];

  return (
    <section className="my-[18px]">
      <span className="mb-[6px] block text-[10px] uppercase tracking-[.06em] text-muted-foreground">
        Assigned tasks
      </span>

      {failed && (
        <p className="m-0 text-[13px] text-muted-foreground">
          Could not read this agent's tasks. Nothing above is affected.
        </p>
      )}

      {!failed && tasks === null && (
        <p className="m-0 text-[13px] text-muted-foreground">Reading…</p>
      )}

      {tasks !== null && tasks.length === 0 && (
        <p className="m-0 text-[13px] text-muted-foreground">
          No tasks assigned. <code>assigned_to</code> names this agent on no task in Neotoma —
          which is the norm rather than a gap: almost every task is filed with no owner at all.
        </p>
      )}

      {tasks !== null && tasks.length > 0 && (
        <>
          {/* THE RATIO. Not decoration: where the agent is unspawnable, every
              one of these tasks is stranded, and the sentence says which. */}
          <p className="m-0 mb-[6px] text-[12px] leading-[1.5]">
            <strong>{tasks.length}</strong> assigned, <strong>{open.length}</strong> not yet done.{" "}
            {spawnable ? (
              <span className="text-muted-foreground">
                <span className="text-live">{agent.name}</span> is in Apis's route table, so these
                can be dispatched.
              </span>
            ) : (
              <span className="text-bad">
                {agent.name} is NOT in Apis's route table, so nothing can spawn it — these{" "}
                {open.length === 1 ? "task is" : `${open.length} unfinished tasks are`} assigned to
                an owner that will never pick them up.
              </span>
            )}
          </p>

          {/* Same dense table geometry as the task list: status, title, date. */}
          <table className="w-full border-collapse text-[12.5px]">
            <thead>
              <tr className="border-b text-[10px] uppercase tracking-[.06em] text-muted-foreground">
                <th className="w-[104px] py-[4px] pr-2 text-left font-[600]">Status</th>
                <th className="py-[4px] pr-2 text-left font-[600]">Task</th>
                <th className="w-[62px] py-[4px] text-right font-[600]">Updated</th>
              </tr>
            </thead>
            <tbody>
              {tasks.map((t) => (
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
                  className="cursor-pointer border-b border-border/60 hover:bg-accent/60 focus-visible:outline focus-visible:outline-1 focus-visible:-outline-offset-1 focus-visible:outline-ring"
                >
                  <td className="py-[4px] pr-2 align-baseline">
                    <Badge variant={ASSIGNED_TONE[toBucket(t.status)]} caps>
                      {t.status}
                    </Badge>
                  </td>
                  <td className="max-w-0 py-[4px] pr-2 align-baseline">
                    <span className="block truncate">{t.title}</span>
                  </td>
                  <td className="py-[4px] text-right align-baseline tabular-nums text-muted-foreground">
                    {relativeTime(t.updatedAt)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}

/** Status tone for the assigned table, matching the task view's mapping. */
const ASSIGNED_TONE = {
  in_progress: "live",
  blocked: "bad",
  done: "ok",
  pending: "muted",
  other: "muted",
} as const;
