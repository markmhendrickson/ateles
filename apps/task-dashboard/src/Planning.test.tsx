/** Render tests for the S5 planning view, from synthetic data: no network. */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { TooltipProvider } from "@/components/ui/tooltip";
import { PlanningView } from "./Planning";
import { flatten, windowText, type PlanningTree, type TreeNode } from "./planningData";

const counts = (o: Partial<TreeNode["counts"]> = {}) => ({ tasks: 2, open: 1, done: 1, blocked: 0, operator: 0, working: 0, ...o });
const node = (id: string, type: string, parent: string | null, children: string[], extra: Partial<TreeNode> = {}): TreeNode => ({
  entity_id: id, entity_type: type, title: id, level: null, parent_id: parent, children, counts: counts(), sessions: [], ...extra,
});

const TREE: PlanningTree = {
  ok: true,
  roots: ["ent_m"],
  nodes: [
    node("ent_m", "mission", null, ["ent_p"]),
    node("ent_p", "project", "ent_m", ["ent_t1", "ent_t2"]),
    node("ent_t1", "task", "ent_p", [], { status: "open", bucket: "active", working: { lease_holder: "ent_h", runner: "run1" } }),
    node("ent_t2", "task", "ent_p", [], { status: "pending", bucket: "recorded", working: null, sessions: [{ entity_id: "ent_s", title: "a session", harness: "codex", state: null, last_activity_at: null }] }),
  ],
  projects: [
    { entity_id: "ent_p", title: "Pilot", customers: [{ entity_id: "ent_o", name: "Acme", entity_type: "organization" }], tasks: 2, open: 2, done: 3, blocked: 1, operator: 1, working: 1, sessions_linked: 1 },
    { entity_id: "ent_q", title: "Solo", customers: [], tasks: 0, open: 0, done: 0, blocked: 0, operator: 0, working: 0, sessions_linked: 0 },
  ],
  unplanned: [
    { entity_id: "ent_u", title: "loose", status: "open", bucket: "recorded", placement: "unplanned", reason: "No planning parent" },
    { entity_id: "ent_k", title: "gap", status: "open", bucket: "recorded", placement: "unknown", reason: "budget" },
  ],
  conflicts: [],
  levels_known: false,
  coverage: { open_total: 8393, open_read: 60, done_total: 10000, done_read: 20, tasks_walked: 80, truncated: false, registry_complete: false, operator_checkpoints_complete: false, gaps: ["a gap"], elapsed_s: 40 },
  working_rule: "Working now only from a held lease plus recent runner activity.",
};

const render = (response: Parameters<typeof PlanningView>[0]["response"]) =>
  renderToStaticMarkup(
    <TooltipProvider>
      <PlanningView response={response} onOpenEntity={() => {}} />
    </TooltipProvider>,
  );

describe("PlanningView ready", () => {
  const html = render({ state: "ready", fetchedAt: "t", tree: TREE });
  it("shows the customer name on the project row and a placeholder when none", () => {
    expect(html).toContain("Acme");
    expect(html).toContain("none recorded");
    expect(html.split('data-testid="project-row"').length - 1).toBe(2);
  });
  it("labels working-now only for the leased task; the other shows recorded state", () => {
    expect(html.split('data-testid="working-now"').length - 1).toBe(1);
    expect(html).toContain("Working now: run1");
    expect(html).toContain("Recorded state: pending");
  });
  it("draws a parent task as a container with counts, not as an empty leaf", () => {
    const tree: PlanningTree = {
      ...TREE,
      roots: ["ent_pt"],
      nodes: [node("ent_pt", "task", null, ["ent_c"]), node("ent_c", "task", "ent_pt", [], { status: "open", bucket: "recorded", working: null })],
    };
    const out = render({ state: "ready", fetchedAt: "t", tree });
    expect(out.split('data-testid="tree-task"').length - 1).toBe(1);
    expect(out).toContain('data-testid="node-counts"');
  });
  it("states the window, unknown levels and partial operator coverage", () => {
    expect(html).toContain("60 of 8,393 open tasks");
    expect(html).toContain("Planning levels are unknown");
    expect(html).toContain("may be low");
  });
  it("separates unplanned from unknown and says a session link is not a lease", () => {
    expect(html).toContain("unplanned");
    expect(html).toContain("unknown");
    expect(html).toContain("A link is not a held lease");
  });
});

describe("PlanningView other states", () => {
  it("a failed read is an alert that says it is not empty", () => {
    const html = render({ state: "error", error: "The planning tree read failed.", retryAfterMs: 1 });
    expect(html).toContain('role="alert"');
    expect(html).toContain("not an empty tree");
  });
  it("unavailable is also an error, not an empty tree", () => {
    expect(render({ state: "unavailable", error: "no python" })).toContain("not an empty tree");
  });
  it("building without a previous read shows nothing to show yet", () => {
    const html = render({ state: "building", startedAt: "t", previous: null });
    expect(html).toContain("Nothing to show yet");
    expect(html).not.toContain("project-summary");
  });
  it("building with a previous read keeps drawing it", () => {
    const html = render({ state: "building", startedAt: "t", previous: { fetchedAt: "p", tree: TREE } });
    expect(html).toContain("previous read");
    expect(html).toContain("Acme");
  });
  it("loading before the first response is a skeleton", () => {
    expect(render(null)).toContain("planning-loading");
  });
});

describe("planningData", () => {
  it("flattens depth-first and survives a cycle", () => {
    const rows = flatten(TREE);
    expect(rows.map((r) => [r.node.entity_id, r.depth])).toEqual([["ent_m", 0], ["ent_p", 1], ["ent_t1", 2], ["ent_t2", 2]]);
    const a = node("a", "x", "b", ["b"]);
    const b = node("b", "x", "a", ["a"]);
    expect(flatten({ nodes: [a, b], roots: ["a"] })).toHaveLength(2);
  });
  it("windowText never implies whole-instance totals", () => {
    expect(windowText(TREE.coverage)).toContain("not whole-instance totals");
  });
});
