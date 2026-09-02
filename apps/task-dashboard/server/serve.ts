/**
 * The DEPLOYED dashboard server.
 *
 * Serves the built SPA from `dist/` plus the same `/api/*` routes the Vite dev
 * server exposes — from the SAME definition (`registerApiRoutes`), so the two
 * cannot drift. `npm run dev` is untouched: it still runs Vite with HMR on
 * 5273, and this file is not in that path at all.
 *
 * ORDER IS THE SECURITY PROPERTY HERE. The middleware stack is, strictly:
 *
 *   1. /healthz         unauthenticated, on purpose — Fly's health check has
 *                       no Google account. Reveals only liveness and the
 *                       build's commit; no graph data.
 *   2. auth routes      the sign-in redirect and its callback.
 *   3. requireAuth      EVERYTHING below this line is gated.
 *   4. /api/*           the read-only Neotoma proxy.
 *   5. dist/            the built assets.
 *
 * The gate sits ABOVE both the API and the static assets. Gating only `/api/*`
 * would still ship the app shell to anonymous visitors, and — more to the
 * point — a single route added later outside the `/api` prefix would be
 * silently public. Putting the gate above everything makes exposure require
 * deliberately moving a route above it, rather than merely forgetting a prefix.
 */
import { createServer } from "node:http";
import { readFileSync, existsSync, statSync } from "node:fs";
import { dirname, join, normalize, extname } from "node:path";
import { fileURLToPath } from "node:url";
import {
  SESSION_COOKIE,
  authConfigured,
  buildAuthorizeUrl,
  consumeState,
  createState,
  issueSession,
  parseCookies,
  readSession,
  verifyGoogleCode,
} from "./auth";
import { registerApiRoutes, type MiddlewareHost } from "./neotomaProxy";

const HERE = dirname(fileURLToPath(import.meta.url));
const DIST = join(HERE, "..", "dist");
const PORT = Number(process.env.PORT) || 8080;

/**
 * The commit this build was made from, injected at build time. Reported by
 * /healthz so a deploy can be PROVEN to have shipped the intended code rather
 * than assumed from `flyctl deploy` exiting 0 — the defect that shipped a
 * month-old Neotoma image twice while reporting success.
 */
const GIT_SHA = process.env.DASHBOARD_GIT_SHA || "";

// ── A minimal Connect-compatible stack ──────────────────────────────────────
// `registerApiRoutes` wants `.use(path, handler)` with prefix matching, which
// is all Vite's middleware stack gives it. Reproducing that here keeps the dev
// and production hosts behaviourally identical without pulling in a framework.

type Handler = (req: any, res: any, next: () => void) => void;
const stack: { path: string; handler: Handler }[] = [];

const app: MiddlewareHost & { use(path: string, handler: Handler): void } = {
  use(path: string, handler: Handler) {
    stack.push({ path, handler });
  },
};

function runStack(req: any, res: any) {
  const url = (req.url || "/").split("?")[0];
  let i = 0;
  const next = () => {
    while (i < stack.length) {
      const layer = stack[i++];
      const p = layer.path;
      const matches = p === "/" || url === p || url.startsWith(p + "/") || url.startsWith(p + "?");
      if (!matches) continue;
      try {
        return layer.handler(req, res, next);
      } catch {
        if (!res.headersSent) {
          res.statusCode = 500;
          res.end("Internal error");
        }
        return;
      }
    }
    res.statusCode = 404;
    res.end("Not found");
  };
  next();
}

// ── 1. Liveness, unauthenticated ────────────────────────────────────────────
app.use("/healthz", (_req, res) => {
  res.setHeader("Content-Type", "application/json");
  res.end(
    JSON.stringify({
      ok: true,
      git_sha: GIT_SHA,
      auth_configured: authConfigured().ok,
    }),
  );
});

// ── 2. The Google sign-in legs ──────────────────────────────────────────────

/** The exact callback URL, which must match the one registered with Google. */
function redirectUriFor(req: any): string {
  const host = req.headers["x-forwarded-host"] || req.headers.host;
  const proto = req.headers["x-forwarded-proto"] || "https";
  return `${proto}://${host}/auth/callback`;
}

app.use("/auth/login", (req, res) => {
  const cfg = authConfigured();
  if (!cfg.ok) {
    res.statusCode = 503;
    res.end(`Sign-in is not configured (missing: ${cfg.missing.join(", ")}).`);
    return;
  }
  // Only ever resume to a path on this origin — never to an absolute URL a
  // caller supplied, which would make this an open redirect.
  const raw = new URL(req.url || "/", "http://x").searchParams.get("next") || "/";
  const next = raw.startsWith("/") && !raw.startsWith("//") ? raw : "/";
  const state = createState(next);
  res.statusCode = 302;
  res.setHeader("Location", buildAuthorizeUrl(redirectUriFor(req), state));
  res.end();
});

app.use("/auth/callback", async (req, res) => {
  const params = new URL(req.url || "/", "http://x").searchParams;
  const code = params.get("code") || "";
  const state = params.get("state") || "";

  const resumed = consumeState(state);
  if (!resumed || !code) {
    res.statusCode = 400;
    res.end("Sign-in failed. Start again from the app.");
    return;
  }

  try {
    const email = await verifyGoogleCode(code, redirectUriFor(req));
    // Secure + HttpOnly + SameSite=Lax: not readable from JS, not sent
    // cross-site, and never over plain HTTP.
    res.setHeader("Set-Cookie", [
      `${SESSION_COOKIE}=${issueSession(email)}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${7 * 24 * 60 * 60}`,
    ]);
    res.statusCode = 302;
    res.setHeader("Location", resumed.next);
    res.end();
  } catch {
    // Deliberately does not distinguish "not on the allowlist" from a bad
    // token: a precise message would let a stranger enumerate who has access.
    res.statusCode = 403;
    res.end("This Google account is not authorized for this dashboard.");
  }
});

// ── 3. The gate ─────────────────────────────────────────────────────────────
app.use("/", (req, res, next) => {
  // Fails CLOSED. An unconfigured deploy serves nobody rather than serving
  // the operator's graph to everybody.
  const cfg = authConfigured();
  if (!cfg.ok) {
    res.statusCode = 503;
    res.end("Dashboard auth is not configured. Refusing to serve.");
    return;
  }

  if (readSession(parseCookies(req.headers.cookie)[SESSION_COOKIE])) {
    next();
    return;
  }

  const url = req.url || "/";
  // An unauthenticated API call gets a 401, not a redirect to Google — a
  // fetch() cannot follow that hop, and an HTML login page parsed as JSON is
  // a confusing failure. The browser reloads and gets the redirect below.
  if (url.startsWith("/api/")) {
    res.statusCode = 401;
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ error: "Not signed in." }));
    return;
  }

  res.statusCode = 302;
  res.setHeader("Location", `/auth/login?next=${encodeURIComponent(url)}`);
  res.end();
});

// ── 4. The read-only Neotoma proxy, shared with the dev server ──────────────
registerApiRoutes(app);

// ── 5. Static assets ────────────────────────────────────────────────────────

const MIME: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".woff2": "font/woff2",
};

app.use("/", (req, res) => {
  const raw = (req.url || "/").split("?")[0];

  // Resolve inside dist/ and verify containment, so `..` cannot escape it.
  const candidate = normalize(join(DIST, decodeURIComponent(raw)));
  const file =
    candidate.startsWith(DIST) && existsSync(candidate) && statSync(candidate).isFile()
      ? candidate
      : join(DIST, "index.html");

  // Hashed build assets are immutable; index.html must never be cached, or a
  // deploy would not reach a browser that already has it.
  const isAsset = file !== join(DIST, "index.html");
  res.setHeader(
    "Cache-Control",
    isAsset ? "public, max-age=31536000, immutable" : "no-store",
  );
  res.setHeader("Content-Type", MIME[extname(file)] || "application/octet-stream");
  res.end(readFileSync(file));
});

createServer(runStack).listen(PORT, "0.0.0.0", () => {
  const cfg = authConfigured();
  console.log(`task-dashboard listening on 0.0.0.0:${PORT} (sha ${GIT_SHA || "unknown"})`);
  if (!cfg.ok) {
    console.error(
      `REFUSING TO SERVE: auth not configured — missing ${cfg.missing.join(", ")}`,
    );
  }
});
