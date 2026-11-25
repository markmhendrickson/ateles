#!/usr/bin/env node
/**
 * Verify a RUNNING local-only console (the LaunchAgent install, or any
 * `DASHBOARD_LOCAL_ONLY=1 node server-dist/serve.js`).
 *
 *   node deploy/verify_console.mjs [--port 5273] [--timeout-s 420]
 *
 * Three checks, each against what the running process actually answers:
 *   1. GET /healthz answers ok and reports local_only: true.
 *   2. GET /api/planning-tree reaches `ready` with a non-empty tree (the build
 *      runs in the background, so this polls until ready or the timeout).
 *   3. A request with a foreign Host header is refused with 403 and no body.
 *
 * Exits 0 only when all three pass. Read-only: it sends GETs and nothing else.
 * `verifyConsole` is exported so the same checks run in unit tests against the
 * real request handler, with no network.
 */
import { request } from "node:http";
import { pathToFileURL } from "node:url";
import { realpathSync } from "node:fs";

export const FOREIGN_HOST = "rebind.attacker.example";

/**
 * @param {object} o
 * @param {(path: string, host: string) => Promise<{status: number, body: string}>} o.get
 * @param {number} o.port
 * @param {number} [o.timeoutMs]
 * @param {number} [o.pollMs]
 * @param {(ms: number) => Promise<void>} [o.sleep]
 * @returns {Promise<{ok: boolean, checks: {name: string, ok: boolean, detail: string}[]}>}
 */
export async function verifyConsole({ get, port, timeoutMs = 420_000, pollMs = 5_000, sleep }) {
  const wait = sleep ?? ((ms) => new Promise((r) => setTimeout(r, ms)));
  const host = `127.0.0.1:${port}`;
  const checks = [];
  const record = (name, ok, detail) => checks.push({ name, ok, detail });

  // 1. Liveness, and that this IS the local-only build.
  try {
    const r = await get("/healthz", host);
    const j = JSON.parse(r.body);
    record(
      "healthz",
      r.status === 200 && j.ok === true && j.local_only === true,
      `status ${r.status}, ok=${j.ok}, local_only=${j.local_only}, git_sha=${j.git_sha || "unknown"}`,
    );
  } catch (err) {
    record("healthz", false, `no usable answer: ${err?.message ?? err}`);
  }

  // 2. The planning tab's data route returns a non-empty tree.
  try {
    let waited = 0;
    let last = "";
    for (;;) {
      const r = await get("/api/planning-tree", host);
      const j = JSON.parse(r.body);
      last = `status ${r.status}, state=${j.state}`;
      if (j.state === "ready") {
        const nodes = Array.isArray(j.tree?.nodes) ? j.tree.nodes.length : 0;
        record("planning-tree", nodes > 0, `${last}, ${nodes} nodes`);
        break;
      }
      if (j.state !== "building") {
        record("planning-tree", false, `${last}${j.error ? `: ${j.error}` : ""}`);
        break;
      }
      if (waited >= timeoutMs) {
        record("planning-tree", false, `${last} after ${Math.round(waited / 1000)}s (timed out)`);
        break;
      }
      await wait(pollMs);
      waited += pollMs;
    }
  } catch (err) {
    record("planning-tree", false, `no usable answer: ${err?.message ?? err}`);
  }

  // 3. The DNS-rebinding defence: a foreign Host is refused, with no body.
  try {
    const r = await get("/api/tasks", FOREIGN_HOST);
    record("foreign-host-refused", r.status === 403 && r.body === "", `status ${r.status}, body ${r.body.length} bytes`);
  } catch (err) {
    record("foreign-host-refused", false, `no usable answer: ${err?.message ?? err}`);
  }

  return { ok: checks.every((c) => c.ok), checks };
}

/** A GET to 127.0.0.1:<port> with an explicit Host header (fetch forbids setting Host). */
export function httpGet(port) {
  return (path, host) =>
    new Promise((resolve, reject) => {
      const req = request(
        { host: "127.0.0.1", port, path, method: "GET", headers: { Host: host }, timeout: 30_000 },
        (res) => {
          let body = "";
          res.setEncoding("utf8");
          res.on("data", (c) => (body += c));
          res.on("end", () => resolve({ status: res.statusCode ?? 0, body }));
        },
      );
      req.on("timeout", () => req.destroy(new Error("request timed out")));
      req.on("error", reject);
      req.end();
    });
}

function arg(argv, name, fallback) {
  const i = argv.indexOf(name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : fallback;
}

async function main(argv) {
  const port = Number(arg(argv, "--port", "5273"));
  const timeoutMs = Number(arg(argv, "--timeout-s", "420")) * 1000;
  const result = await verifyConsole({ get: httpGet(port), port, timeoutMs });
  for (const c of result.checks) console.log(`${c.ok ? "PASS" : "FAIL"}  ${c.name}: ${c.detail}`);
  console.log(result.ok ? "console verified" : "console NOT verified");
  process.exit(result.ok ? 0 : 1);
}

const isMain = (() => {
  try {
    return import.meta.url === pathToFileURL(realpathSync(process.argv[1] ?? "")).href;
  } catch {
    return false;
  }
})();
if (isMain) await main(process.argv.slice(2));
