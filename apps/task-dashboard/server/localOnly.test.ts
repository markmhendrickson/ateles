/**
 * Local-only mode: the server drives the REAL request stack from serve.ts, in
 * both modes, with no network. The planning tree's Python read is replaced by a
 * fake upstream; nothing reaches a real Neotoma.
 *
 * serve.ts reads its mode once at import, so each test sets the environment and
 * re-imports it (`vi.resetModules`). A module that throws on import is a server
 * that refuses to start: the built bundle exits before binding anything.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  LOCAL_DEFAULT_PORT,
  hostAllowed,
  isLoopbackAddress,
  resolveServeMode,
} from "./localOnly";
// @ts-expect-error plain .mjs script, no type declarations
import { FOREIGN_HOST, verifyConsole } from "../deploy/verify_console.mjs";

const fakeTree = { ok: true, version: 1, nodes: [{ entity_id: "ent_x", parent_id: null, entity_type: "plan" }], roots: ["ent_x"] };

vi.mock("./planningTree", async (importOriginal) => {
  const real = await importOriginal<typeof import("./planningTree")>();
  return { ...real, runPython: vi.fn(async () => "log line\n" + JSON.stringify(fakeTree) + "\n") };
});

const ENV_KEYS = [
  "DASHBOARD_LOCAL_ONLY",
  "DASHBOARD_BIND_HOST",
  "PORT",
  "DASHBOARD_GOOGLE_CLIENT_ID",
  "DASHBOARD_GOOGLE_CLIENT_SECRET",
  "DASHBOARD_APPROVED_EMAILS",
  "DASHBOARD_SESSION_KEY",
];
let saved: Record<string, string | undefined> = {};

beforeEach(() => {
  saved = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
  for (const k of ENV_KEYS) delete process.env[k];
  vi.resetModules();
});
afterEach(() => {
  for (const k of ENV_KEYS) {
    if (saved[k] === undefined) delete process.env[k];
    else process.env[k] = saved[k];
  }
});

interface Answer {
  status: number;
  headers: Record<string, string>;
  body: string;
}

async function loadHandler(env: Record<string, string>) {
  Object.assign(process.env, env);
  const mod = await import("./serve");
  return mod.handle as (req: unknown, res: unknown) => void;
}

/** One request through the real stack, the way node:http would deliver it. */
async function request(handle: (req: unknown, res: unknown) => void, url: string, headers: Record<string, string>): Promise<Answer> {
  const out: Answer = { status: 200, headers: {}, body: "" };
  await new Promise<void>((resolve) => {
    const res = {
      headersSent: false,
      get statusCode() {
        return out.status;
      },
      set statusCode(v: number) {
        out.status = v;
      },
      setHeader(name: string, value: unknown) {
        out.headers[name.toLowerCase()] = String(value);
      },
      end(chunk?: string | Buffer) {
        out.body = chunk === undefined ? "" : String(chunk);
        resolve();
      },
    };
    handle({ url, headers, method: "GET" }, res);
  });
  return out;
}

describe("resolveServeMode", () => {
  it("is the unchanged deployed mode when the variable is unset", () => {
    expect(resolveServeMode({})).toEqual({ localOnly: false, host: "0.0.0.0", port: 8080 });
    expect(resolveServeMode({ PORT: "9000" })).toEqual({ localOnly: false, host: "0.0.0.0", port: 9000 });
  });

  it("binds 127.0.0.1 on the documented port in local-only mode", () => {
    expect(resolveServeMode({ DASHBOARD_LOCAL_ONLY: "1" })).toEqual({
      localOnly: true,
      host: "127.0.0.1",
      port: LOCAL_DEFAULT_PORT,
    });
    expect(LOCAL_DEFAULT_PORT).toBe(5273);
  });

  it("refuses to start rather than bind a non-loopback address", () => {
    for (const bind of ["0.0.0.0", "::", "192.168.1.10", "localhost", "::1", ""]) {
      expect(() => resolveServeMode({ DASHBOARD_LOCAL_ONLY: "1", DASHBOARD_BIND_HOST: bind })).toThrow(/non-loopback/);
    }
  });

  it("refuses an ambiguous flag value instead of guessing a mode", () => {
    for (const v of ["true", "yes", "0", " 1"]) {
      expect(() => resolveServeMode({ DASHBOARD_LOCAL_ONLY: v })).toThrow(/exactly "1"/);
    }
  });

  it("refuses a port the Host check could not name exactly", () => {
    for (const p of ["0", "", "abc", "70000", "5273.5"]) {
      expect(() => resolveServeMode({ DASHBOARD_LOCAL_ONLY: "1", PORT: p })).toThrow(/fixed PORT/);
    }
  });

  it("checks the actually-bound address, not the requested one", () => {
    expect(isLoopbackAddress("127.0.0.1")).toBe(true);
    for (const a of ["0.0.0.0", "::", "::ffff:127.0.0.1", "10.0.0.2"]) expect(isLoopbackAddress(a)).toBe(false);
  });
});

describe("hostAllowed", () => {
  it("accepts exactly 127.0.0.1:<port> and localhost:<port>", () => {
    expect(hostAllowed("127.0.0.1:5273", 5273)).toBe(true);
    expect(hostAllowed("localhost:5273", 5273)).toBe(true);
  });

  it("refuses everything else, including near misses", () => {
    for (const h of [
      undefined,
      "",
      "127.0.0.1",
      "localhost",
      "127.0.0.1:5274",
      "LOCALHOST:5273",
      "localhost.:5273",
      " localhost:5273",
      "evil.example:5273",
      "127.0.0.1.nip.io:5273",
      "[::1]:5273",
      "localhost:5273@evil.example",
    ]) {
      expect(hostAllowed(h, 5273)).toBe(false);
    }
  });
});

describe("serve.ts in local-only mode", () => {
  it("refuses to start (throws at load, before listen) on a non-loopback bind", async () => {
    await expect(loadHandler({ DASHBOARD_LOCAL_ONLY: "1", DASHBOARD_BIND_HOST: "0.0.0.0" })).rejects.toThrow(/non-loopback/);
  });

  it("refuses a foreign Host with 403 and no body, on every route including /healthz", async () => {
    const handle = await loadHandler({ DASHBOARD_LOCAL_ONLY: "1" });
    for (const url of ["/", "/healthz", "/api/tasks", "/api/planning-tree", "/auth/login"]) {
      for (const host of ["evil.example", "evil.example:5273", "127.0.0.1:8080"]) {
        const r = await request(handle, url, { host });
        expect(r.status, `${url} with Host ${host}`).toBe(403);
        expect(r.body).toBe("");
      }
    }
    const missing = await request(handle, "/api/tasks", {});
    expect(missing.status).toBe(403);
  });

  it("serves the planning tree without any Google session to a correct Host", async () => {
    const handle = await loadHandler({ DASHBOARD_LOCAL_ONLY: "1" });
    const first = await request(handle, "/api/planning-tree", { host: "localhost:5273" });
    expect(first.status).toBe(200);
    expect(JSON.parse(first.body).state).toBe("building");
    await new Promise((r) => setTimeout(r, 0));
    const ready = JSON.parse((await request(handle, "/api/planning-tree", { host: "127.0.0.1:5273" })).body);
    expect(ready.state).toBe("ready");
    expect(ready.tree.nodes.length).toBeGreaterThan(0);
  });

  it("reports local_only on /healthz", async () => {
    const handle = await loadHandler({ DASHBOARD_LOCAL_ONLY: "1" });
    const j = JSON.parse((await request(handle, "/healthz", { host: "127.0.0.1:5273" })).body);
    expect(j).toMatchObject({ ok: true, local_only: true });
  });

  it("passes the verification script's three checks against the real stack", async () => {
    const handle = await loadHandler({ DASHBOARD_LOCAL_ONLY: "1" });
    const result = await verifyConsole({
      port: 5273,
      pollMs: 1,
      timeoutMs: 1000,
      sleep: () => new Promise((r) => setTimeout(r, 0)),
      get: (path: string, host: string) => request(handle, path, { host }),
    });
    expect(result.checks.map((c: { name: string }) => c.name)).toEqual(["healthz", "planning-tree", "foreign-host-refused"]);
    expect(result.ok).toBe(true);
  });

  it("the verification script FAILS when the Host check is absent (Google mode answers a foreign Host)", async () => {
    const handle = await loadHandler({});
    const result = await verifyConsole({
      port: 5273,
      pollMs: 1,
      timeoutMs: 1,
      sleep: async () => {},
      get: (path: string, host: string) => request(handle, path, { host }),
    });
    expect(result.ok).toBe(false);
    const foreign = result.checks.find((c: { name: string }) => c.name === "foreign-host-refused");
    expect(foreign.ok).toBe(false);
    expect(FOREIGN_HOST).not.toMatch(/localhost|127\.0\.0\.1/);
  });
});

describe("serve.ts in the deployed (Google) mode is unchanged", () => {
  const configured = {
    DASHBOARD_GOOGLE_CLIENT_ID: "client-id",
    DASHBOARD_GOOGLE_CLIENT_SECRET: "client-secret",
    DASHBOARD_APPROVED_EMAILS: "someone@example.com",
    DASHBOARD_SESSION_KEY: "k".repeat(32),
  };

  it("fails closed when auth is unconfigured, whatever the Host", async () => {
    const handle = await loadHandler({});
    for (const host of ["127.0.0.1:5273", "evil.example"]) {
      const r = await request(handle, "/api/planning-tree", { host });
      expect(r.status).toBe(503);
      expect(r.body).toBe("Dashboard auth is not configured. Refusing to serve.");
    }
  });

  it("gates /api with 401 and pages with a redirect to Google sign-in", async () => {
    const handle = await loadHandler(configured);
    const api = await request(handle, "/api/planning-tree", { host: "dash.example" });
    expect(api.status).toBe(401);
    const page = await request(handle, "/#/planning", { host: "dash.example" });
    expect(page.status).toBe(302);
    expect(page.headers.location).toBe(`/auth/login?next=${encodeURIComponent("/#/planning")}`);
    const login = await request(handle, "/auth/login", { host: "dash.example" });
    expect(login.status).toBe(200);
    expect(login.body).toMatch(/accounts\.google\.com/);
  });

  it("healthz keeps its exact shape (no local_only key)", async () => {
    const handle = await loadHandler(configured);
    const j = JSON.parse((await request(handle, "/healthz", { host: "dash.example" })).body);
    expect(Object.keys(j)).toEqual(["ok", "git_sha", "auth_configured"]);
    expect(j.auth_configured).toBe(true);
  });

  it("cannot be switched to local-only by anything a request carries", async () => {
    const handle = await loadHandler(configured);
    const r = await request(handle, "/api/planning-tree?DASHBOARD_LOCAL_ONLY=1&local_only=1", {
      host: "127.0.0.1:5273",
      "x-dashboard-local-only": "1",
      cookie: "DASHBOARD_LOCAL_ONLY=1",
    });
    expect(r.status).toBe(401);
  });
});
