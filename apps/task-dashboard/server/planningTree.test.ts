/**
 * The planning-tree job: one build at a time, never an empty tree on failure,
 * and the Python read is called with the dashboard's own open-status list.
 */
import { describe, expect, it, vi } from "vitest";
import { OPEN_TASK_STATUSES } from "../src/taskSearch";
import { createPlanningTree, treeArgs } from "./planningTree";

const ok = JSON.stringify({ ok: true, nodes: [], roots: [] });
const tick = () => new Promise((r) => setTimeout(r, 0));

describe("createPlanningTree", () => {
  it("answers building at once, then ready, and runs a single job for many viewers", async () => {
    const run = vi.fn().mockResolvedValue("log line\n" + ok + "\n");
    const read = createPlanningTree({ run, log: () => {} });
    expect(read().state).toBe("building");
    expect(read().state).toBe("building");
    expect(run).toHaveBeenCalledTimes(1);
    await tick();
    const done = read();
    expect(done.state).toBe("ready");
    expect(run).toHaveBeenCalledTimes(1);
  });

  it("serves the previous tree while rebuilding after the TTL", async () => {
    let t = 0;
    const run = vi.fn().mockResolvedValue(ok);
    const read = createPlanningTree({ run, now: () => t, ttlMs: 1000, log: () => {} });
    read();
    await tick();
    expect(read().state).toBe("ready");
    t = 5000;
    const r = read();
    expect(r.state).toBe("building");
    expect(r.state === "building" && r.previous).not.toBeNull();
  });

  it("a failed read is an error, never an empty tree, and is retried after the delay", async () => {
    let t = 0;
    const run = vi.fn().mockRejectedValueOnce(new Error("boom host=x")).mockResolvedValue(ok);
    const read = createPlanningTree({ run, now: () => t, retryMs: 1000, log: () => {} });
    read();
    await tick();
    const failed = read();
    expect(failed.state).toBe("error");
    // The real error stays in the log, not the response.
    expect(JSON.stringify(failed)).not.toContain("boom");
    expect(run).toHaveBeenCalledTimes(1);
    t = 2000;
    expect(read().state).toBe("building");
    await tick();
    expect(read().state).toBe("ready");
  });

  it("a result that is not ok is a failure", async () => {
    const run = vi.fn().mockResolvedValue(JSON.stringify({ error: "Open tasks could not be read" }));
    const read = createPlanningTree({ run, log: () => {} });
    read();
    await tick();
    expect(read().state).toBe("error");
  });

  it("a missing interpreter is reported unavailable", async () => {
    const run = vi.fn().mockRejectedValue(Object.assign(new Error("spawn"), { code: "ENOENT" }));
    const read = createPlanningTree({ run, log: () => {} });
    read();
    await tick();
    expect(read().state).toBe("unavailable");
  });
});

describe("treeArgs", () => {
  it("passes the dashboard's open statuses so the two never drift", () => {
    const args = treeArgs();
    expect(args[args.indexOf("--open-statuses") + 1]).toBe(OPEN_TASK_STATUSES.join(","));
    expect(args).toContain("execution.mcp.ateles.planning_tree");
  });
});
