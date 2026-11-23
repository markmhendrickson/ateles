/**
 * PLANNING — the tree (console screen S5)
 * ---------------------------------------
 * "What is this work for, and what is under this record?"
 * Design basis: docs/product/console.md#s5--the-hierarchy--the-planning-records-and-the-ascent
 *
 * Two views of one read: a one-row-per-project summary (open, done, blocked,
 * waiting on the operator, working now, and the customer the project refers
 * to), and the tree itself, built by reading PART_OF upward from tasks, with a
 * per-node overlay of live work.
 *
 * HONEST BY CONSTRUCTION:
 *   - "Working now" appears only where the server found a held lease plus recent
 *     runner activity. Every other task shows its recorded status.
 *   - Counts are within a bounded window of recently touched tasks, and the
 *     window is stated on the page.
 *   - A task with no planning parent is "unplanned", not an error; one with two
 *     parents is "held" for judgement; a read gap is "unknown". Three words for
 *     three facts.
 *   - Levels are shown only when the registry supplied them; no ladder of type
 *     names is assumed.
 *
 * READ-ONLY: one GET to /api/planning-tree, polled while the server builds.
 */
import { useEffect, useRef, useState } from "react";
import {
  type FlatRow,
  type PlanningTree,
  type ProjectSummary,
  type TreeResponse,
  type Unplaced,
  flatten,
  levelText,
  windowText,
  workingText,
} from "./planningData";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { AlertTriangle } from "lucide-react";

type Open = (entityId: string) => void;

const BUILDING_POLL_MS = 4_000;
const READY_POLL_MS = 60_000;

function EntityButton({ id, onOpen, children }: { id: string; onOpen: Open; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={() => onOpen(id)}
      className="text-left font-[600] text-live underline decoration-dotted underline-offset-2 hover:opacity-80"
    >
      {children}
    </button>
  );
}

function Num({ value, tone }: { value: number; tone?: "warn" | "bad" | "live" | "ok" }) {
  if (value === 0) return <span className="text-muted-foreground">0</span>;
  return (
    <Badge variant={tone ?? "muted"} className="tabular-nums">
      {value}
    </Badge>
  );
}

export function ProjectTable({ projects, onOpen }: { projects: ProjectSummary[]; onOpen: Open }) {
  if (projects.length === 0) {
    return (
      <p data-testid="no-projects" className="text-[12.5px] text-muted-foreground">
        No project appears above any task in this window. That is a statement about the window, not
        about the instance.
      </p>
    );
  }
  return (
    <div className="overflow-x-auto rounded-lg border bg-card">
      <table className="w-full text-[12.5px]" data-testid="project-summary">
        <thead>
          <tr className="border-b text-left text-muted-foreground">
            <th className="px-3 py-[6px] font-[600]">Project</th>
            <th className="px-3 py-[6px] font-[600]">Customer</th>
            <th className="px-3 py-[6px] font-[600]">Open</th>
            <th className="px-3 py-[6px] font-[600]">Done</th>
            <th className="px-3 py-[6px] font-[600]">Blocked</th>
            <th className="px-3 py-[6px] font-[600]">Waiting on you</th>
            <th className="px-3 py-[6px] font-[600]">Working now</th>
          </tr>
        </thead>
        <tbody>
          {projects.map((p) => (
            <tr key={p.entity_id} className="border-b last:border-b-0" data-testid="project-row">
              <td className="px-3 py-[6px]">
                <EntityButton id={p.entity_id} onOpen={onOpen}>
                  {p.title}
                </EntityButton>
              </td>
              <td className="px-3 py-[6px]" data-testid="project-customer">
                {p.customers.length === 0 ? (
                  <span className="text-muted-foreground">none recorded</span>
                ) : (
                  p.customers.map((c, i) => (
                    <span key={c.entity_id}>
                      {i > 0 && ", "}
                      <EntityButton id={c.entity_id} onOpen={onOpen}>
                        {c.name}
                      </EntityButton>
                    </span>
                  ))
                )}
              </td>
              <td className="px-3 py-[6px]"><Num value={p.open} /></td>
              <td className="px-3 py-[6px]"><Num value={p.done} tone="ok" /></td>
              <td className="px-3 py-[6px]"><Num value={p.blocked} tone="bad" /></td>
              <td className="px-3 py-[6px]"><Num value={p.operator} tone="warn" /></td>
              <td className="px-3 py-[6px]"><Num value={p.working} tone="live" /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TreeRow({ row, onOpen }: { row: FlatRow; onOpen: Open }) {
  const { node, depth } = row;
  // A task can also sit ABOVE other tasks (a parent task). Only a task the read
  // walked itself carries a bucket; a parent task is drawn as a container.
  const isTask = node.entity_type === "task" && node.bucket !== undefined;
  const level = levelText(node);
  return (
    <li
      className="flex flex-wrap items-center gap-[6px] border-b py-[5px] text-[12.5px] last:border-b-0"
      style={{ paddingLeft: `${depth * 18 + 8}px` }}
      data-testid={isTask ? "tree-task" : "tree-node"}
    >
      <Badge variant="muted" caps>
        {node.entity_type}
      </Badge>
      {level && <span className="text-[11px] text-muted-foreground">{level}</span>}
      <EntityButton id={node.entity_id} onOpen={onOpen}>
        {node.title}
      </EntityButton>
      {isTask ? (
        node.working ? (
          <Badge variant="live" data-testid="working-now">
            Working now: {workingText(node.working)}
          </Badge>
        ) : (
          <Badge variant={node.bucket === "operator-needed" ? "warn" : node.bucket === "blocked" ? "bad" : node.bucket === "done" ? "ok" : "muted"}>
            Recorded state: {node.status}
            {node.bucket === "operator-needed" ? " (waiting on you)" : ""}
          </Badge>
        )
      ) : (
        <span className="ml-auto text-[11.5px] text-muted-foreground" data-testid="node-counts">
          {node.counts.open} open · {node.counts.done} done
          {node.counts.blocked > 0 && ` · ${node.counts.blocked} blocked`}
          {node.counts.operator > 0 && ` · ${node.counts.operator} waiting on you`}
          {node.counts.working > 0 && ` · ${node.counts.working} working now`}
        </span>
      )}
      {node.sessions.length > 0 && (
        <span className="basis-full text-[11.5px] text-muted-foreground" data-testid="node-sessions">
          Recorded sessions linked: {node.sessions.map((s) => s.title ?? s.entity_id).join(", ")}. A link
          is not a held lease.
        </span>
      )}
    </li>
  );
}

const PLACEMENT_WORDS: Record<Unplaced["placement"], string> = {
  unplanned: "unplanned",
  held: "held: two planning parents, needs a planning batch to judge",
  unknown: "unknown: the read did not complete",
};

function Unplanned({ items, onOpen }: { items: Unplaced[]; onOpen: Open }) {
  if (items.length === 0) return null;
  return (
    <section data-testid="unplaced" className="mt-5">
      <h2 className="mb-[8px] text-[14px] font-[650] tracking-[-0.01em]">
        Not placed in the tree ({items.length})
      </h2>
      <p className="mb-[6px] text-[12px] text-muted-foreground">
        Unplanned means the task has no planning parent. It is a fact about the task, not a failed read.
      </p>
      <ul className="rounded-lg border bg-card">
        {items.map((u) => (
          <li key={u.entity_id} className="flex flex-wrap items-center gap-[6px] border-b px-3 py-[5px] text-[12.5px] last:border-b-0">
            <Badge variant={u.placement === "unknown" ? "warn" : "muted"} caps>
              {PLACEMENT_WORDS[u.placement].split(":")[0]}
            </Badge>
            <EntityButton id={u.entity_id} onOpen={onOpen}>
              {u.title}
            </EntityButton>
            <span className="text-[11.5px] text-muted-foreground">
              Recorded state: {u.status}
              {u.placement !== "unplanned" ? `. ${u.reason}` : ""}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function PlanningTreeView({
  tree,
  note,
  onOpenEntity,
}: {
  tree: PlanningTree;
  note?: React.ReactNode;
  onOpenEntity: Open;
}) {
  const rows = flatten(tree);
  const c = tree.coverage;
  return (
    <div data-testid="planning-ready">
      {note}
      <div className="mb-[8px] rounded-lg border bg-card px-3 py-[8px] text-[12px] text-muted-foreground" data-testid="coverage">
        {windowText(c)}{" "}
        {!tree.levels_known && "Planning levels are unknown: the registry read did not qualify them, so nodes are labelled by type only. "}
        {!c.operator_checkpoints_complete && "Waiting-on-you counts may be low: the open checkpoint list was not read in full. "}
        {c.truncated && "The read stopped at its deadline; some tasks in the window are missing. "}
        {tree.working_rule}
        {tree.conflicts.length > 0 && ` ${tree.conflicts.length} record(s) were reached under two different parents; the first parent read is shown.`}
      </div>
      {c.gaps.length > 0 && (
        <ul className="mb-[8px] text-[11.5px] text-muted-foreground" data-testid="gaps">
          {c.gaps.map((g) => (
            <li key={g}>{g}</li>
          ))}
        </ul>
      )}

      <h2 className="mb-[8px] mt-4 text-[14px] font-[650] tracking-[-0.01em]">Projects</h2>
      <ProjectTable projects={tree.projects} onOpen={onOpenEntity} />

      <h2 className="mb-[8px] mt-5 text-[14px] font-[650] tracking-[-0.01em]">Planning tree</h2>
      {rows.length === 0 ? (
        <p className="text-[12.5px] text-muted-foreground" data-testid="empty-tree">
          No task in this window has a planning ascent.
        </p>
      ) : (
        <ul className="rounded-lg border bg-card" data-testid="tree">
          {rows.map((r) => (
            <TreeRow key={r.node.entity_id} row={r} onOpen={onOpenEntity} />
          ))}
        </ul>
      )}
      <Unplanned items={tree.unplanned} onOpen={onOpenEntity} />
    </div>
  );
}

/** What the page draws for each state of the server's background build. */
export function PlanningView({ response, onOpenEntity }: { response: TreeResponse | null; onOpenEntity: Open }) {
  if (response === null) {
    return (
      <div aria-busy="true" data-testid="planning-loading" className="rounded-lg border bg-card p-3">
        <Skeleton className="mb-[7px] h-[13px] w-[min(90%,520px)]" />
        <Skeleton className="h-[13px] w-[min(70%,400px)]" />
      </div>
    );
  }
  if (response.state === "error" || response.state === "unavailable") {
    return (
      <div
        role="alert"
        data-testid="planning-error"
        className="flex items-start gap-[8px] rounded-lg border border-[hsl(var(--bad)/0.35)] bg-[hsl(var(--bad)/0.10)] px-3 py-[9px] text-[12.5px]"
      >
        <AlertTriangle className="mt-px h-[14px] w-[14px] flex-none" aria-hidden />
        <span>
          <strong>The planning tree could not be read.</strong>{" "}
          <span className="text-muted-foreground">{response.error} This is not an empty tree.</span>
        </span>
      </div>
    );
  }
  if (response.state === "building") {
    const prev = response.previous;
    return (
      <div>
        <div role="status" data-testid="planning-building" className="mb-[8px] rounded-lg border bg-card px-3 py-[8px] text-[12.5px]">
          Building the tree from hosted Neotoma. Each task's ascent is its own read, so this can take a
          minute or two.{" "}
          {prev ? `Showing the previous read from ${prev.fetchedAt} meanwhile.` : "Nothing to show yet."}
        </div>
        {prev && <PlanningTreeView tree={prev.tree} onOpenEntity={onOpenEntity} />}
      </div>
    );
  }
  return <PlanningTreeView tree={response.tree} onOpenEntity={onOpenEntity} note={
    <p className="mb-[6px] text-[11.5px] text-muted-foreground">Read at {response.fetchedAt}.</p>
  } />;
}

export function Planning({ onOpenEntity }: { onOpenEntity: Open }) {
  const [response, setResponse] = useState<TreeResponse | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      let next = READY_POLL_MS;
      try {
        const res = await fetch("/api/planning-tree");
        const body = (await res.json()) as TreeResponse;
        if (cancelled) return;
        setResponse(body);
        setFailed(null);
        if (body.state === "building") next = BUILDING_POLL_MS;
        if (body.state === "error") next = Math.max(body.retryAfterMs, BUILDING_POLL_MS);
      } catch {
        if (cancelled) return;
        setFailed("The dashboard server did not answer.");
        next = BUILDING_POLL_MS * 2;
      }
      timer.current = window.setTimeout(load, next);
    };
    void load();
    return () => {
      cancelled = true;
      window.clearTimeout(timer.current);
    };
  }, []);

  return (
    <div className="mx-auto w-full max-w-[1100px] px-4 py-4">
      <h1 className="text-[16px] font-[650] tracking-[-0.01em]">Planning</h1>
      <p className="mb-2 text-[12.5px] text-muted-foreground">
        What this work is for, and what is under each record. Read upward from tasks along PART_OF.
      </p>
      {failed && response === null ? (
        <PlanningView response={{ state: "error", error: failed, retryAfterMs: 0 }} onOpenEntity={onOpenEntity} />
      ) : (
        <PlanningView response={response} onOpenEntity={onOpenEntity} />
      )}
    </div>
  );
}
