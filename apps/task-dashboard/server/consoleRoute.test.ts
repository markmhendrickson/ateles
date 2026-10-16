/**
 * Contract test for the `/api/console` ROUTE itself.
 *
 * `consoleSpine.test.ts` covers the section reader; nothing there touches the
 * HTTP layer the browser depends on: the 400 for a section outside the fixed
 * list, the 502 that makes a failed read look like a failure, and the
 * `Cache-Control: no-store` that stops a failure being cached by a browser.
 * Each of those is one line in `neotomaProxy.ts` and each can be broken while
 * every other test stays green, so they are pinned here by calling the real
 * handler with a fake host, a fake response and a stubbed `fetch`. No network.
 *
 * The module is re-imported per test (`vi.resetModules`) because the route owns
 * a process-wide cache: a section cached by one test would answer the next
 * without ever reaching the stubbed upstream.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { IncomingMessage, ServerResponse } from "node:http";

type Handler = (req: IncomingMessage, res: ServerResponse, next: (err?: unknown) => void) => unknown;

interface Captured {
  status: number;
  headers: Record<string, string>;
  body: string;
}

/** Register every route on a fake host and hand back the `/api/console` handler. */
async function consoleHandler(): Promise<Handler> {
  const { registerApiRoutes } = await import("./neotomaProxy");
  const routes = new Map<string, Handler>();
  registerApiRoutes({
    use(path, fn) {
      routes.set(path, fn);
    },
  });
  const handler = routes.get("/api/console");
  if (!handler) throw new Error("/api/console was not registered");
  return handler;
}

/** Call a handler like the server would, and capture what it wrote. */
async function call(handler: Handler, url: string): Promise<Captured> {
  const headers: Record<string, string> = {};
  const out: Captured = { status: 200, headers, body: "" };
  const res = {
    get statusCode() {
      return out.status;
    },
    set statusCode(v: number) {
      out.status = v;
    },
    setHeader(name: string, value: string) {
      headers[name.toLowerCase()] = String(value);
    },
    end(chunk?: string) {
      out.body = chunk ?? "";
    },
  } as unknown as ServerResponse;
  await handler({ url } as IncomingMessage, res, () => {});
  return out;
}

function stubUpstream(response: Response | (() => Response)) {
  const fetchStub = vi.fn(async () => (typeof response === "function" ? response() : response));
  vi.stubGlobal("fetch", fetchStub);
  return fetchStub;
}

beforeEach(() => {
  vi.resetModules();
  // Both set, so the proxy never falls back to reading a dotenv from disk.
  vi.stubEnv("NEOTOMA_BEARER_TOKEN", "test-token-not-real");
  vi.stubEnv("NEOTOMA_BASE_URL", "http://neotoma.invalid");
  // The spine logs the real error of a failed read; keep the test output clean.
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("GET /api/console", () => {
  it("answers 400 with an error body for a section outside the fixed list, and never asks upstream", async () => {
    const upstream = stubUpstream(new Response("{}"));
    const r = await call(await consoleHandler(), "/api/console?section=task");
    expect(r.status).toBe(400);
    expect(JSON.parse(r.body)).toHaveProperty("error");
    expect(r.headers["cache-control"]).toBe("no-store");
    expect(upstream).not.toHaveBeenCalled();
  });

  it("answers 400 when the section is missing", async () => {
    stubUpstream(new Response("{}"));
    const r = await call(await consoleHandler(), "/api/console");
    expect(r.status).toBe(400);
    expect(r.headers["cache-control"]).toBe("no-store");
  });

  it("answers 502 with ok:false and a fixed message when upstream fails", async () => {
    stubUpstream(new Response("upstream says: secret detail", { status: 503 }));
    const r = await call(await consoleHandler(), "/api/console?section=missions");
    expect(r.status).toBe(502);
    const body = JSON.parse(r.body);
    expect(body.ok).toBe(false);
    expect(body.error).toBe("Read failed.");
    expect(r.body).not.toContain("secret detail");
    expect(r.body).not.toContain("503");
    expect(r.headers["cache-control"]).toBe("no-store");
  });

  it("answers 200 with the section when upstream answers", async () => {
    const upstream = stubUpstream(
      new Response(
        JSON.stringify({ entities: [{ entity_id: "ent_1", snapshot: { title: "T", status: "active" } }], total: 1 }),
        { status: 200 },
      ),
    );
    const r = await call(await consoleHandler(), "/api/console?section=missions");
    expect(r.status).toBe(200);
    const body = JSON.parse(r.body);
    expect(body.ok).toBe(true);
    expect(body.records).toHaveLength(1);
    expect(r.headers["content-type"]).toBe("application/json");
    expect(r.headers["cache-control"]).toBe("no-store");
    expect(upstream).toHaveBeenCalledTimes(1);
  });
});
