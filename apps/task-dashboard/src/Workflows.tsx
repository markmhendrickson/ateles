/**
 * WORKFLOWS
 * ---------
 * The eight `workflow_definition` entities, and — the actual point of this view
 * — the fact that NOTHING RUNS THEM.
 *
 * The finding banner is deliberately the first thing on the page and is not
 * collapsible. A viewer who reads only the gate tables would come away
 * believing this is the live pipeline; it is not, and a caveat further down the
 * page would not reliably reach them. Everything below the banner is therefore
 * framed as "declared", never as "runs".
 *
 * Every claim in the banner is verified in `workflowData.ts` (`EXECUTION_FACTS`),
 * each against a named file and line.
 *
 * READ-ONLY, like every view here: one GET to /api/workflows, no mutation.
 */
import { useEffect, useMemo, useState } from "react";
import {
  type Gate,
  type LifecycleStage,
  type Workflow,
  type WorkflowEntity,
  EXECUTION_FACTS,
  LIFECYCLE_STAGES,
  STAGE_BLURBS,
  STAGE_LABELS,
  inferStage,
  parseWorkflow,
} from "./workflowData";
import { WorkflowListSkeleton } from "@/components/Skeletons";
import { showSkeleton } from "@/lib/loading";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { AlertTriangle, GitBranch, Info } from "lucide-react";

/**
 * The banner. States the finding in plain words before any gate table, so it
 * cannot be scrolled past on the way to the data.
 */
function NotExecutedBanner() {
  const [open, setOpen] = useState(false);

  return (
    /* NOT COMPRESSED INTO A FOOTNOTE. Everything around it got tighter; this
       banner keeps its full size, its icon, its colour, and both paragraphs,
       because the finding it carries is the entire point of the tab — a reader
       who takes the gate tables for the live pipeline has been misled. */
    <section className="mb-3 rounded-lg border border-[hsl(var(--bad)/0.35)] bg-[hsl(var(--bad)/0.10)] p-3">
      <div className="flex items-start gap-[10px]">
        <AlertTriangle
          className="mt-[2px] h-4 w-4 flex-none text-[hsl(var(--bad))]"
          aria-hidden
        />
        <div className="min-w-0">
          <h2 className="text-[14px] font-[650] tracking-[-0.01em]">
            These workflows are declared, not executed.
          </h2>
          <p className="mt-[5px] text-[12.5px] leading-[1.45] text-muted-foreground">
            The live dispatcher reads <strong>none</strong> of these entities. The pipeline
            that actually runs is hardcoded in three Python literals, and the only thing
            connecting these definitions to running code is a drift{" "}
            <em>detector</em> that warns when the two disagree. The component that would
            genuinely execute them exists and is unreachable.
          </p>
          <p className="mt-[6px] text-[12.5px] leading-[1.45] text-muted-foreground">
            Read the gate tables below as{" "}
            <strong>the sequence someone intended</strong> — not as what happens when work
            moves through the swarm.
          </p>

          <Button
            variant="outline"
            size="sm"
            className="mt-[10px]"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
          >
            {open ? "Hide evidence" : `Show evidence (${EXECUTION_FACTS.length} checks)`}
          </Button>

          {open && (
            <ul className="mt-[10px] space-y-[10px] border-t border-[hsl(var(--bad)/0.25)] pt-[10px]">
              {EXECUTION_FACTS.map((f) => (
                <li key={f.where}>
                  <div className="text-[13px] font-[600]">{f.claim}</div>
                  <code className="mt-[2px] block break-all font-mono text-[11px] text-muted-foreground">
                    {f.where}
                  </code>
                  <div className="mt-[3px] text-[12px] leading-[1.5] text-muted-foreground">
                    {f.detail}
                  </div>
                </li>
              ))}
              <li className="pt-[2px] text-[12px] italic leading-[1.5] text-muted-foreground">
                Each row verified against the named file and line on 2026-08-31.
              </li>
            </ul>
          )}
        </div>
      </div>
    </section>
  );
}

/**
 * THE POINTER THAT REPLACED THE TWO-AXES BOX.
 *
 * This page used to carry a five-paragraph explanation of how the generic task
 * lifecycle differs from these gate sequences. A box whose entire job is to say
 * "these two things are not the same" is a symptom of two subjects sharing one
 * surface, so the lifecycle now has its own tab and this is the one line that
 * remains — a pointer, not a summary.
 *
 * The ONE claim kept here is the one a reader of THIS page needs: the stage
 * grouping offered below is the dashboard's own inference from gate names, not
 * stored data. That caveat has to travel with the grouping it qualifies; moving
 * it to another tab would leave an invented mapping on screen unlabelled.
 */
function LifecyclePointer({ grouped }: { grouped: boolean }) {
  return (
    <section className="mb-3 flex items-start gap-[8px] rounded-lg border bg-card px-3 py-[9px] text-[12.5px] leading-[1.45] text-muted-foreground">
      <Info className="mt-[2px] h-[14px] w-[14px] flex-none" aria-hidden />
      <p className="min-w-0">
        Gates are <strong className="text-foreground">who signs off on a kind of work</strong>.
        For the generic lifecycle every task moves through, see the{" "}
        <a
          href="#/lifecycle"
          className="font-[600] text-live underline underline-offset-2 hover:opacity-80"
        >
          Lifecycle
        </a>{" "}
        tab.
        {grouped && (
          <>
            {" "}
            The stage grouping below is{" "}
            <strong className="text-foreground">this dashboard's inference</strong> from gate
            names — no <code className="font-mono text-[11px]">lifecycle_stage</code> field
            exists on any stored gate.
          </>
        )}
      </p>
    </section>
  );
}

function GateRow({ gate, showStage }: { gate: Gate; showStage: boolean }) {
  const stage = inferStage(gate.gateName);
  return (
    <li className="flex flex-wrap items-baseline gap-x-[7px] gap-y-[2px] py-[2px]">
      <span className="w-[20px] flex-none font-mono text-[11px] text-muted-foreground">
        {gate.phase ?? "—"}
      </span>
      <span className="w-[104px] flex-none truncate font-mono text-[11.5px] font-[600]">
        {gate.gateName}
      </span>
      <span className="w-[92px] flex-none truncate text-[11.5px] text-muted-foreground">
        {gate.ownerAgent ?? "no owner"}
      </span>

      {!gate.required && (
        <Badge variant="muted" className="text-[10px]">
          optional
        </Badge>
      )}
      {gate.parallelGroup && (
        <Tooltip>
          <TooltipTrigger asChild>
            <Badge variant="live" className="text-[10px]">
              ∥ {gate.parallelGroup}
              {gate.joinGate ? ` → ${gate.joinGate}` : ""}
            </Badge>
          </TooltipTrigger>
          <TooltipContent>
            Runs in parallel group “{gate.parallelGroup}”
            {gate.joinGate ? `, joining on “${gate.joinGate}”.` : "."}
          </TooltipContent>
        </Tooltip>
      )}
      {gate.kind && (
        <Tooltip>
          <TooltipTrigger asChild>
            <Badge variant="purple" className="text-[10px]">
              {gate.kind}
            </Badge>
          </TooltipTrigger>
          <TooltipContent className="max-w-[380px]">
            {gate.script ? <code className="font-mono text-[11px]">{gate.script}</code> : null}
            {gate.description ? <div className="mt-1">{gate.description}</div> : null}
          </TooltipContent>
        </Tooltip>
      )}
      {showStage && (
        <span className="ml-auto text-[11px] italic text-muted-foreground">
          {stage ? STAGE_LABELS[stage] : "unclassified"}
        </span>
      )}
    </li>
  );
}

function WorkflowCard({ wf, groupByStage }: { wf: Workflow; groupByStage: boolean }) {
  // Group into the inferred stages, preserving declared order within each, and
  // keep anything unrecognized in its own trailing bucket rather than dropping
  // it or guessing.
  const byStage = useMemo(() => {
    const buckets = new Map<LifecycleStage | "unclassified", Gate[]>();
    for (const g of wf.gates) {
      const key = inferStage(g.gateName) ?? "unclassified";
      const list = buckets.get(key) ?? [];
      list.push(g);
      buckets.set(key, list);
    }
    const ordered: Array<[LifecycleStage | "unclassified", Gate[]]> = [];
    for (const s of LIFECYCLE_STAGES) {
      const list = buckets.get(s);
      if (list?.length) ordered.push([s, list]);
    }
    const rest = buckets.get("unclassified");
    if (rest?.length) ordered.push(["unclassified", rest]);
    return ordered;
  }, [wf.gates]);

  return (
    <article className="rounded-lg border bg-card p-3">
      <header className="flex flex-wrap items-baseline gap-x-[8px] gap-y-[4px]">
        <h3 className="text-[14px] font-[650] tracking-[-0.01em]">
          <span className="text-muted-foreground">{wf.project}</span>
          <span className="text-muted-foreground"> / </span>
          {wf.workflowType}
        </h3>
        <Badge variant={wf.status === "active" ? "ok" : "muted"} className="text-[10px]">
          {wf.status}
        </Badge>
        {wf.legalRequired && (
          <Badge variant="warn" className="text-[10px]">
            legal required
          </Badge>
        )}
        <span className="ml-auto text-[11px] text-muted-foreground">
          {wf.staleThresholdByType
            ? Object.entries(wf.staleThresholdByType)
                .map(([k, v]) => `${k} ${v}d`)
                .join(" · ")
            : wf.staleThresholdDays !== null
              ? `stale after ${wf.staleThresholdDays}d`
              : "no stale threshold"}
        </span>
      </header>

      {wf.description && (
        <p className="mt-[5px] text-[12px] leading-[1.4] text-muted-foreground">
          {wf.description}
        </p>
      )}

      <div className="mt-[8px]">
        <div className="flex items-baseline gap-[6px]">
          <h4 className="text-[12px] font-[600]">
            Declared gates{" "}
            <span className="font-[400] text-muted-foreground">({wf.gates.length})</span>
          </h4>
          {groupByStage && (
            <span className="text-[11px] italic text-muted-foreground">
              grouped by inferred stage — this dashboard's inference, not stored
            </span>
          )}
        </div>

        {wf.gatesUnparsed ? (
          <p className="mt-[6px] rounded border border-[hsl(var(--bad)/0.35)] bg-[hsl(var(--bad)/0.10)] px-[10px] py-[7px] text-[12px]">
            <strong>Gates could not be parsed.</strong> The field is present on this entity but
            is neither an array nor JSON-decodable — showing nothing here would misreport it
            as having no gates.
          </p>
        ) : wf.gates.length === 0 ? (
          <p className="mt-[6px] text-[12px] text-muted-foreground">No gates declared.</p>
        ) : groupByStage ? (
          <div className="mt-[4px] space-y-[10px]">
            {byStage.map(([stage, gates]) => (
              <div key={stage}>
                <div className="flex items-baseline gap-[6px] border-b pb-[3px]">
                  <span className="text-[11px] font-[650] uppercase tracking-[0.04em]">
                    {stage === "unclassified" ? "Unclassified" : STAGE_LABELS[stage]}
                  </span>
                  <span className="text-[11px] text-muted-foreground">
                    {stage === "unclassified"
                      ? "gate name not in the inference table"
                      : STAGE_BLURBS[stage]}
                  </span>
                </div>
                <ul className="divide-y">
                  {gates.map((g) => (
                    <GateRow key={`${g.phase}-${g.gateName}`} gate={g} showStage={false} />
                  ))}
                </ul>
              </div>
            ))}
          </div>
        ) : (
          <ul className="mt-[4px] divide-y border-t">
            {wf.gates.map((g) => (
              <GateRow key={`${g.phase}-${g.gateName}`} gate={g} showStage />
            ))}
          </ul>
        )}
      </div>

      {wf.fastPaths.length > 0 && (
        <div className="mt-[8px]">
          <h4 className="text-[12px] font-[600]">Fast paths</h4>
          <ul className="mt-[4px] space-y-[3px]">
            {wf.fastPaths.map((f) => (
              <li key={f.condition} className="text-[12px] text-muted-foreground">
                <code className="font-mono text-[11px] text-foreground">{f.condition}</code>
                {" skips "}
                {f.skipGates.length ? (
                  f.skipGates.map((g, i) => (
                    <span key={g}>
                      {i > 0 && ", "}
                      <code className="font-mono text-[11px] text-foreground">{g}</code>
                    </span>
                  ))
                ) : (
                  <span>nothing</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </article>
  );
}

export function Workflows() {
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [firstLoadDone, setFirstLoadDone] = useState(false);
  const [groupByStage, setGroupByStage] = useState(false);

  // One fetch on mount. These entities are configuration that changes a few
  // times a year, so there is nothing for a poll to catch.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch("/api/workflows?limit=100");
        const body = await res.json();
        if (!res.ok || body.error) throw new Error(body.error ?? `HTTP ${res.status}`);
        const parsed = (body.entities as WorkflowEntity[]).map(parseWorkflow);
        parsed.sort(
          (a, b) =>
            a.project.localeCompare(b.project) || a.workflowType.localeCompare(b.workflowType),
        );
        if (!cancelled) {
          setWorkflows(parsed);
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

  const pending = showSkeleton(!firstLoadDone, workflows.length > 0);

  return (
    <div>
      <header className="mb-2 flex flex-wrap items-center gap-x-[8px] gap-y-[4px]">
        <GitBranch className="h-4 w-4 text-muted-foreground" aria-hidden />
        <h1 className="text-[16px] font-[650] tracking-[-0.02em]">Workflows</h1>
        <span className="text-[12px] text-muted-foreground">
          {workflows.length} declared {workflows.length === 1 ? "definition" : "definitions"}
        </span>

        <div className="ml-auto flex gap-[4px]">
          <Button
            variant={groupByStage ? "outline" : "default"}
            size="sm"
            onClick={() => setGroupByStage(false)}
          >
            Declared phases
          </Button>
          <Button
            variant={groupByStage ? "default" : "outline"}
            size="sm"
            onClick={() => setGroupByStage(true)}
          >
            Lifecycle stage
          </Button>
        </div>
      </header>

      <NotExecutedBanner />
      <LifecyclePointer grouped={groupByStage} />

      {error && (
        <div className="my-4 rounded-lg border border-[hsl(var(--bad)/0.35)] bg-[hsl(var(--bad)/0.12)] px-3 py-[10px] text-[13px]">
          <strong>Cannot load workflows.</strong> {error}
        </div>
      )}

      {pending ? (
        <WorkflowListSkeleton />
      ) : (
        <div className={cn("space-y-2")}>
          {workflows.map((wf) => (
            <WorkflowCard key={wf.id} wf={wf} groupByStage={groupByStage} />
          ))}
          {!workflows.length && !error && (
            <p className="text-[13px] text-muted-foreground">
              No workflow definitions found.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
