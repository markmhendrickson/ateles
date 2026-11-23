/**
 * PLANNING TREE — client-side shapes and pure helpers (console screen S5).
 *
 * Design basis: docs/product/console.md#s5--the-hierarchy--the-planning-records-and-the-ascent
 *
 * The tree arrives already derived: the ascent, the buckets, the "working now"
 * rule and the per-project counts are computed by
 * `execution/mcp/ateles/planning_tree.py`, which calls the workboard's own
 * reads. Nothing here re-derives them. This file only orders what arrived for
 * drawing and words the coverage honestly.
 *
 * No ladder of type names lives here: a node is labelled by its own entity
 * type, and its level only when the registry supplied one. A fork whose
 * planning types differ renders the same way.
 */

export interface Working {
  lease_holder: string | null;
  runner: string | null;
}

export interface SessionLink {
  entity_id: string | null;
  title: string | null;
  harness: string | null;
  state: string | null;
  last_activity_at: string | null;
}

export interface NodeCounts {
  tasks: number;
  open: number;
  done: number;
  blocked: number;
  operator: number;
  working: number;
}

export interface TreeNode {
  entity_id: string;
  entity_type: string;
  title: string;
  /** Registry level, or null when the registry read did not qualify. */
  level: number | null;
  parent_id: string | null;
  children: string[];
  counts: NodeCounts;
  sessions: SessionLink[];
  /** Task leaves only. */
  status?: string;
  bucket?: string;
  working?: Working | null;
}

export interface Customer {
  entity_id: string;
  name: string;
  entity_type: string;
}

export interface ProjectSummary extends NodeCounts {
  entity_id: string;
  title: string;
  customers: Customer[];
  sessions_linked: number;
}

export interface Unplaced {
  entity_id: string;
  title: string;
  status: string;
  bucket: string;
  /** unplanned is a design word, not an error; held = two parents; unknown = a read gap. */
  placement: "unplanned" | "held" | "unknown";
  reason: string;
}

export interface Coverage {
  open_total: number | null;
  open_read: number;
  done_total: number | null;
  done_read: number;
  tasks_walked: number;
  truncated: boolean;
  registry_complete: boolean;
  operator_checkpoints_complete: boolean;
  gaps: string[];
  elapsed_s: number;
}

export interface PlanningTree {
  ok: true;
  nodes: TreeNode[];
  roots: string[];
  projects: ProjectSummary[];
  unplanned: Unplaced[];
  conflicts: string[];
  levels_known: boolean;
  coverage: Coverage;
  working_rule: string;
}

export type TreeResponse =
  | { state: "ready"; fetchedAt: string; tree: PlanningTree }
  | { state: "building"; startedAt: string; previous: { fetchedAt: string; tree: PlanningTree } | null }
  | { state: "error"; error: string; retryAfterMs: number }
  | { state: "unavailable"; error: string };

export interface FlatRow {
  node: TreeNode;
  depth: number;
}

/**
 * The tree flattened depth-first for drawing. Guards against a cycle in the
 * data (it should not occur; the read reports conflicts) so a bad payload can
 * never hang the page.
 */
export function flatten(tree: Pick<PlanningTree, "nodes" | "roots">): FlatRow[] {
  const byId = new Map(tree.nodes.map((n) => [n.entity_id, n]));
  const out: FlatRow[] = [];
  const seen = new Set<string>();
  const visit = (id: string, depth: number) => {
    const node = byId.get(id);
    if (!node || seen.has(id)) return;
    seen.add(id);
    out.push({ node, depth });
    for (const child of node.children) visit(child, depth + 1);
  };
  for (const root of tree.roots) visit(root, 0);
  return out;
}

/** "Showing 60 of 8,393 open tasks" — the window, never implied to be everything. */
export function windowText(c: Coverage): string {
  const of = (read: number, total: number | null, noun: string) =>
    total !== null && total > read
      ? `${read} of ${total.toLocaleString("en-US")}${total >= 10_000 ? "+" : ""} ${noun}`
      : `${read} ${noun}`;
  return `Window: the ${of(c.open_read, c.open_total, "open tasks")} and ${of(c.done_read, c.done_total, "done tasks")} most recently touched. Counts are within this window, not whole-instance totals.`;
}

/** Who holds the work, for a task that is genuinely working now. */
export function workingText(w: Working): string {
  const runner = w.runner ?? "unknown runner";
  const holder = w.lease_holder ?? "unknown holder";
  return `${runner} (lease ${holder})`;
}

/** The label under which a node's level is shown, only when the registry gave one. */
export function levelText(node: Pick<TreeNode, "level">): string | null {
  return node.level === null ? null : `level ${node.level}`;
}
