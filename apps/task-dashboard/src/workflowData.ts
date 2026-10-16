/**
 * WORKFLOW DEFINITIONS
 * --------------------
 * `workflow_definition` entities declare, per project and workflow type, the
 * ordered gates a piece of work is supposed to pass through: who owns each
 * gate, which gates run in parallel, and which gate each parallel branch joins
 * back on.
 *
 * THE IMPORTANT CAVEAT, which the view states plainly rather than burying:
 * these entities are DECLARED but NOT EXECUTED by the live dispatcher. See
 * `EXECUTION_FACTS` below — every claim there was verified against the files it
 * names, at the lines it names, on 2026-08-31.
 *
 * SHAPE DRIFT: `gates` and `fast_paths` are stored inconsistently across the
 * eight entities — a real JSON array on some, a JSON-encoded STRING on others
 * (5 of 8 for `gates` at time of writing). Same field, same schema, two
 * encodings, because different writers stored them at different times. Parsing
 * only one shape silently renders three workflows blank, which reads as "this
 * workflow has no gates" rather than "the dashboard cannot parse this". So
 * `asArray` below accepts both, and `parseWorkflow` records a `gatesUnparsed`
 * flag for anything it genuinely could not read — a parse failure must surface
 * as a defect, never as an empty list.
 *
 * This mirrors the same tolerance `agents.ts` applies to `tool_allowlist`.
 */

/** Shape of the rows Neotoma returns from POST /entities/query. */
export interface WorkflowEntity {
  entity_id: string;
  canonical_name?: string;
  snapshot?: Record<string, unknown> | null;
  last_observation_at?: string | null;
}

export interface Gate {
  /** Phase number as stored. Gates sharing a phase run together. */
  phase: number | null;
  gateName: string;
  ownerAgent: string | null;
  /** Non-null when this gate runs in parallel with others in the same group. */
  parallelGroup: string | null;
  /** The sibling gate this branch joins back on, when parallel. */
  joinGate: string | null;
  required: boolean;
  /**
   * Present on a few gates only: a deterministic script gate rather than an
   * agent judgement (e.g. social_content's `draft_lint`). Kept because it
   * changes what the gate IS, not merely how it is described.
   */
  kind: string | null;
  script: string | null;
  description: string | null;
}

export interface FastPath {
  condition: string;
  skipGates: string[];
}

export interface Workflow {
  id: string;
  project: string;
  workflowType: string;
  status: string;
  description: string | null;
  gates: Gate[];
  /** True when `gates` was present but could not be parsed — a defect to show. */
  gatesUnparsed: boolean;
  fastPaths: FastPath[];
  /**
   * Days before the workflow is considered stale. Stored as a NUMBER on most
   * entities but as a per-type MAP on neotoma|feature
   * (`{feature: 5, bug: 2, security: 1}`), so both are preserved rather than
   * coercing the map to NaN.
   */
  staleThresholdDays: number | null;
  staleThresholdByType: Record<string, number> | null;
  legalRequired: boolean;
  updatedAt: Date | null;
}

/**
 * Accept a value that is either a real array or a JSON-encoded string holding
 * one. Returns null (not []) when the value was present but unparseable, so
 * callers can distinguish "no gates declared" from "gates we failed to read".
 */
function asArray(value: unknown): unknown[] | null {
  if (Array.isArray(value)) return value;
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) return [];
    try {
      const parsed = JSON.parse(trimmed);
      return Array.isArray(parsed) ? parsed : null;
    } catch {
      return null;
    }
  }
  if (value == null) return [];
  return null;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function parseGate(raw: unknown): Gate | null {
  if (!raw || typeof raw !== "object") return null;
  const g = raw as Record<string, unknown>;
  const gateName = str(g.gate_name);
  if (!gateName) return null;
  return {
    phase: num(g.phase),
    gateName,
    ownerAgent: str(g.owner_agent),
    parallelGroup: str(g.parallel_group),
    joinGate: str(g.join_gate),
    // Only an explicit `false` makes a gate optional; anything else is
    // required, which is the safer default to display.
    required: g.required !== false,
    kind: str(g.kind),
    script: str(g.script),
    description: str(g.description),
  };
}

function parseFastPath(raw: unknown): FastPath | null {
  if (!raw || typeof raw !== "object") return null;
  const f = raw as Record<string, unknown>;
  const condition = str(f.condition);
  if (!condition) return null;
  const skip = asArray(f.skip_gates) ?? [];
  return {
    condition,
    skipGates: skip.map((s) => (typeof s === "string" ? s : "")).filter(Boolean),
  };
}

export function parseWorkflow(entity: WorkflowEntity): Workflow {
  // Same double-nesting tolerance the other parsers apply: some rows arrive
  // with the fields one level deeper than the schema suggests.
  const outer = (entity.snapshot ?? {}) as Record<string, unknown>;
  const inner = (outer.snapshot as Record<string, unknown> | undefined) ?? outer;

  const rawGates = asArray(inner.gates);
  const gates = (rawGates ?? [])
    .map(parseGate)
    .filter((g): g is Gate => g !== null)
    // Order by phase so the declared sequence reads top to bottom. Gates within
    // a phase keep their stored order, which is the order they were declared.
    .sort((a, b) => (a.phase ?? 99) - (b.phase ?? 99));

  const stale = inner.stale_threshold_days;
  const staleMap =
    stale && typeof stale === "object" && !Array.isArray(stale)
      ? Object.fromEntries(
          Object.entries(stale as Record<string, unknown>)
            .map(([k, v]) => [k, num(v)])
            .filter((pair): pair is [string, number] => pair[1] !== null),
        )
      : null;

  return {
    id: entity.entity_id,
    project: str(inner.project) ?? "—",
    workflowType: str(inner.workflow_type) ?? "—",
    status: str(inner.status) ?? "unknown",
    description: str(inner.description),
    gates,
    // `gates` was there in some form but produced nothing readable.
    gatesUnparsed: rawGates === null || (rawGates.length > 0 && gates.length === 0),
    fastPaths: (asArray(inner.fast_paths) ?? [])
      .map(parseFastPath)
      .filter((f): f is FastPath => f !== null),
    staleThresholdDays: num(stale),
    staleThresholdByType: staleMap && Object.keys(staleMap).length ? staleMap : null,
    legalRequired: inner.legal_required === true,
    updatedAt: entity.last_observation_at ? new Date(entity.last_observation_at) : null,
  };
}

/**
 * LIFECYCLE STAGE — AN INFERENCE MADE HERE, NOT A STORED FIELD.
 * ------------------------------------------------------------
 * The operator's reconciliation question: there is a generic task lifecycle
 * (`task_lifecycle.py` declares pending, routed, executing, verified, done,
 * failed, blocked, awaiting_approval, awaiting_input, declined, superseded) and
 * separately these domain gate sequences. They are DIFFERENT AXES — the
 * lifecycle is how any task moves; gates are who signs off on a kind of work.
 *
 * The stored gates mix those axes into one flat list: `pm`/`ux`/`arch` are
 * specification, `impl` is execution, `pr_review`/`qa` are review, `release` is
 * reporting — with NOTHING in the data marking which lifecycle stage a gate
 * belongs to. No `lifecycle_stage` field exists on any gate in any of the eight
 * entities.
 *
 * So this mapping is a dashboard-side inference over `gate_name`, and the UI
 * labels it as such wherever it is used. A gate whose name is not in the table
 * gets `null` and is shown as unclassified rather than being forced into a
 * bucket — inventing a stage for an unrecognized gate is exactly the failure
 * this comment exists to prevent.
 */
export const LIFECYCLE_STAGES = [
  "specification",
  "execution",
  "review",
  "reporting",
] as const;
export type LifecycleStage = (typeof LIFECYCLE_STAGES)[number];

export const STAGE_LABELS: Record<LifecycleStage, string> = {
  specification: "Specification",
  execution: "Execution",
  review: "Review",
  reporting: "Reporting",
};

export const STAGE_BLURBS: Record<LifecycleStage, string> = {
  specification: "Deciding what to build and how.",
  execution: "Making the change.",
  review: "Checking the change before it ships.",
  reporting: "Shipping and recording the outcome.",
};

/** Gate name → inferred stage. Inference, not stored data. See above. */
const GATE_STAGE: Record<string, LifecycleStage> = {
  pm: "specification",
  ux: "specification",
  arch: "specification",
  copy: "specification",
  draft: "specification",
  impl: "execution",
  post: "execution",
  draft_lint: "review",
  pr_review: "review",
  qa: "review",
  legal: "review",
  operator_preview: "review",
  release: "reporting",
};

export function inferStage(gateName: string): LifecycleStage | null {
  return GATE_STAGE[gateName] ?? null;
}

/**
 * THE CENTRAL FINDING, as verified facts rather than assertion.
 *
 * Each row was checked against the named file at the named line on 2026-08-31.
 * They are held here as data so the UI renders them uniformly and so a future
 * reader can re-verify each one individually.
 */
export interface ExecutionFact {
  claim: string;
  where: string;
  detail: string;
}

export const EXECUTION_FACTS: ExecutionFact[] = [
  {
    claim: "The issue pipeline order is a code literal",
    where: "execution/daemons/apis/issue_spec.py:81",
    detail:
      "SECTIONS is a fixed tuple, commented “Order here IS the pipeline order”. " +
      "The sequence comes from that tuple, not from any workflow_definition.",
  },
  {
    claim: "The review panel's lenses are a code literal",
    where: "execution/daemons/apis/review_panel.py:69",
    detail:
      "LENSES is a fixed tuple whose order sets panel priority. Gate owners here " +
      "are hardcoded, not read from the entity that declares them.",
  },
  {
    claim: "The pre-impl gates are a code literal",
    where: "execution/daemons/apis/swarm_dispatch.py:230",
    detail:
      'PRE_IMPL_GATES = ("pm", "ux", "arch") — a tuple in source, independent of ' +
      "whatever gates a workflow_definition declares for the project.",
  },
  {
    claim: "The only data-to-code link is a drift DETECTOR, not a driver",
    where: "execution/daemons/apis/swarm_dispatch.py:4217, apis.py:902",
    detail:
      "Apis reads workflow_definition solely to compare declared gate owners " +
      "against the hardcoded ones and warn on divergence. It warns; it never " +
      "dispatches from them.",
  },
  {
    claim: "The declared executor exists but is unreachable",
    where: "execution/daemons/anthus/orchestrator.py",
    detail:
      "522 lines implementing real phase ordering and join gates — the thing that " +
      "would actually run these workflows. It is reachable only via Anthus, which " +
      "is exit-127 under launchd on a missing launcher script.",
  },
  {
    claim: "The README's claim is false as of 2026-08-31",
    where: "README.md:146",
    detail:
      "“Anthus reads them and dispatches accordingly — no hardcoded sequencing.” " +
      "The sequencing is hardcoded in the three literals above, and Anthus is down.",
  },
];
