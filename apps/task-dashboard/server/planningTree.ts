/**
 * PLANNING TREE (console screen S5) — the server side.
 *
 * Design basis: docs/product/console.md#s5--the-hierarchy--the-planning-records-and-the-ascent
 *
 * THE ASCENT IS NOT RE-IMPLEMENTED HERE. Walking PART_OF upward, deciding what
 * "working now" means (a held lease plus recent runner activity, nothing less),
 * and bucketing a task already live in `execution/mcp/ateles/server.py` and
 * `workboard_planning.py`, which the session workboard uses. This module only
 * runs `execution/mcp/ateles/planning_tree.py` — a thin read that CALLS those —
 * and serves its JSON. A second copy of the algorithm in TypeScript would drift
 * from the workboard in exactly the way the repo has been bitten by before.
 *
 * WHY A BACKGROUND JOB. The read is a bounded window of tasks, each ascent a
 * relationship read against hosted Neotoma, so it takes tens of seconds to a
 * few minutes. A request therefore never waits on it: the first call starts the
 * job and answers `{state: "building"}`, the client polls, and a finished result
 * is served from memory for a while. One job runs at a time across every
 * viewer, and the job itself reads sequentially (one read in flight, under the
 * two-read ceiling that has protected hosted Neotoma since ateles#576).
 *
 * A failure is never cached as data: it answers `{state: "error"}` and the next
 * request after the retry delay starts over. The deployed image carries no
 * Python or repo checkout; there the answer is an explicit `unavailable`, never
 * an empty tree. READ-ONLY: nothing here writes to Neotoma.
 */
import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { OPEN_TASK_STATUSES } from "../src/taskSearch";

/** How long a finished tree is served before the next request rebuilds it. */
export const TREE_TTL_MS = 5 * 60_000;
/** After a failed build, how long before another attempt is allowed. */
export const TREE_RETRY_MS = 30_000;
/** Hard ceiling on one build, above the Python read's own deadline. */
export const TREE_JOB_TIMEOUT_MS = 330_000;
/** Window sizes; the Python module also clamps them. */
export const OPEN_WINDOW = 60;
export const DONE_WINDOW = 20;

export type TreeResult =
  | { state: "ready"; fetchedAt: string; tree: unknown }
  | { state: "building"; startedAt: string; previous: { fetchedAt: string; tree: unknown } | null }
  | { state: "error"; error: string; retryAfterMs: number }
  | { state: "unavailable"; error: string };

export interface TreeDeps {
  /** Run the Python read and resolve with its stdout. */
  run(args: string[]): Promise<string>;
  now?(): number;
  ttlMs?: number;
  retryMs?: number;
  log?(...args: unknown[]): void;
}

export function treeArgs(): string[] {
  return [
    "-m",
    "execution.mcp.ateles.planning_tree",
    "--open-statuses",
    OPEN_TASK_STATUSES.join(","),
    "--open-limit",
    String(OPEN_WINDOW),
    "--done-limit",
    String(DONE_WINDOW),
  ];
}

export function createPlanningTree(deps: TreeDeps): () => TreeResult {
  const now = deps.now ?? Date.now;
  const ttl = deps.ttlMs ?? TREE_TTL_MS;
  const retry = deps.retryMs ?? TREE_RETRY_MS;
  const log = deps.log ?? console.error;
  let ready: { at: number; tree: unknown } | null = null;
  let building: { startedAt: number } | null = null;
  let failed: { at: number; error: string; unavailable: boolean } | null = null;

  const previous = () =>
    ready ? { fetchedAt: new Date(ready.at).toISOString(), tree: ready.tree } : null;

  function start() {
    building = { startedAt: now() };
    deps
      .run(treeArgs())
      .then((stdout) => {
        // The module prints one JSON document on its last line.
        const line = stdout.trim().split("\n").filter(Boolean).pop() ?? "";
        const tree = JSON.parse(line) as { ok?: boolean; error?: string };
        if (!tree || tree.ok !== true) throw new Error(tree?.error ?? "The tree read reported no result.");
        ready = { at: now(), tree };
        failed = null;
      })
      .catch((err: unknown) => {
        // The real error can name hosts or paths: log it, tell the client little.
        log("planning tree read failed:", err);
        const code = (err as { code?: unknown })?.code;
        failed = {
          at: now(),
          error: "The planning tree read failed.",
          unavailable: code === "ENOENT",
        };
      })
      .finally(() => {
        building = null;
      });
  }

  return function read(): TreeResult {
    if (ready && now() - ready.at < ttl) {
      return { state: "ready", fetchedAt: new Date(ready.at).toISOString(), tree: ready.tree };
    }
    if (building) {
      return { state: "building", startedAt: new Date(building.startedAt).toISOString(), previous: previous() };
    }
    if (failed && now() - failed.at < retry) {
      if (failed.unavailable) {
        return {
          state: "unavailable",
          error: "The planning tree needs the repo checkout and Python; this deployment has neither.",
        };
      }
      return { state: "error", error: failed.error, retryAfterMs: retry - (now() - failed.at) };
    }
    start();
    return { state: "building", startedAt: new Date(now()).toISOString(), previous: previous() };
  };
}

/** The repo root, from this file: apps/task-dashboard/server -> repo. */
export function repoRoot(): string {
  return resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
}

/** The interpreter for the planning module: the installer pins one that can import `mcp`. */
export function pythonBin(env: NodeJS.ProcessEnv = process.env): string {
  const pinned = env.DASHBOARD_PYTHON?.trim();
  return pinned ? pinned : "python3";
}

export function runPython(args: string[]): Promise<string> {
  return new Promise((resolveRun, reject) => {
    execFile(
      pythonBin(),
      args,
      { cwd: repoRoot(), timeout: TREE_JOB_TIMEOUT_MS, maxBuffer: 32 * 1024 * 1024, env: process.env },
      (err, stdout) => (err ? reject(err) : resolveRun(stdout)),
    );
  });
}
