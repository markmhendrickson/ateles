/**
 * Contract tests for the deploy path.
 *
 * WHY THESE EXIST AS TESTS AND NOT COMMENTS: every failure they cover is one
 * that still lets `flyctl deploy` exit 0. A deploy missing `-c` succeeds and
 * silently reapplies the wrong machine shape (neotoma#2289); a gate that stops
 * covering a route serves fine and simply stops protecting it. Damage of this
 * kind surfaces later as "the dashboard is slow" or, worse, never surfaces at
 * all — which is exactly how it survives review the first time.
 *
 * The parser reads the real TOML tables with comment lines stripped: this
 * config discusses bad sizes in prose, and a naive scan lands in a comment and
 * asserts against the very value the comment warns about.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { dirname } from "node:path";

const APP_DIR = join(dirname(fileURLToPath(import.meta.url)), "..");
const REPO_ROOT = join(APP_DIR, "..", "..");

const workflow = readFileSync(
  join(REPO_ROOT, ".github", "workflows", "deploy-console.yml"),
  "utf8",
);
const flyConfig = readFileSync(join(APP_DIR, "fly.console.toml"), "utf8");
const serveSrc = readFileSync(join(APP_DIR, "server", "serve.ts"), "utf8");

/** Strip full-line comments so prose about bad values is never asserted on. */
const stripComments = (s: string) =>
  s
    .split("\n")
    .filter((l) => !/^\s*#/.test(l))
    .join("\n");

describe("deploy workflow", () => {
  it("deploys from the console's own Fly config, not the default fly.toml", () => {
    // The bug this pins: `flyctl deploy` with no -c falls back to fly.toml and
    // reapplies ITS [[vm]] block over the running machine.
    expect(workflow).toMatch(/flyctl deploy -c fly\.console\.toml/);
  });

  it("passes the app and region explicitly, since the config names neither", () => {
    expect(workflow).toMatch(/--app "\$APP"/);
    expect(workflow).toMatch(/--primary-region "\$REGION"/);
  });

  it("injects the deployed commit so the running build can be identified", () => {
    expect(workflow).toMatch(/--build-arg CONSOLE_GIT_SHA="\$SHA"/);
  });

  it("names no app or hostname — this repo is public", () => {
    // Values come from secrets; the canonical binding lives in Neotoma.
    expect(workflow).toMatch(/secrets\.CONSOLE_APP/);
    expect(workflow).toMatch(/secrets\.CONSOLE_HOST/);
    expect(stripComments(workflow)).not.toMatch(/\.fly\.dev/);
  });

  it("queues concurrent deploys rather than cancelling one mid-flight", () => {
    expect(workflow).toMatch(/cancel-in-progress:\s*false/);
  });

  it("refuses to deploy a commit whose CI is not green", () => {
    expect(workflow).toMatch(/Wait for CI to pass on this commit/);
    // A timeout must fail, never fall through into a deploy.
    expect(workflow).toMatch(/timed out after 30m waiting for CI[\s\S]*?exit 1/);
  });
});

describe("post-deploy verification", () => {
  it("compares the RUNNING build's commit against the one just deployed", () => {
    // Neotoma shipped a month-old image twice while reporting deploy success.
    // Checking that the deploy command exited 0 would not have caught it.
    expect(workflow).toMatch(/STALE BUILD/);
    expect(workflow).toMatch(/j\.git_sha !== exp/);
  });

  it("fails when the deployed instance reports auth is not configured", () => {
    expect(workflow).toMatch(/auth_configured !== true/);
  });

  it("proves the gate is closed, not merely that the code shipped", () => {
    // Deploying the gate and the gate working are different claims.
    expect(workflow).toMatch(/anonymous GET \/ ->/);
    expect(workflow).toMatch(/the dashboard is PUBLIC/);
    expect(workflow).toMatch(/the graph is EXPOSED/);
  });
});

describe("fly config", () => {
  const vmBlock = stripComments(
    flyConfig.split(/^\[\[vm\]\]/m)[1] ?? "",
  );

  it("serves on the port the server binds", () => {
    expect(stripComments(flyConfig)).toMatch(/internal_port\s*=\s*8080/);
  });

  it("forces https, since the session cookie is Secure-only", () => {
    expect(stripComments(flyConfig)).toMatch(/force_https\s*=\s*true/);
  });

  it("declares a guest size, so a deploy cannot silently resize the machine", () => {
    expect(vmBlock).toMatch(/memory\s*=\s*"512mb"/);
    expect(vmBlock).toMatch(/cpus\s*=\s*1/);
  });

  it("health-checks the one unauthenticated route", () => {
    // Fly's checker has no Google account; pointing the check at a gated path
    // would fail every check and roll every deploy back.
    expect(stripComments(flyConfig)).toMatch(/path\s*=\s*"\/healthz"/);
  });

  it("names no app or hostname — this repo is public", () => {
    expect(stripComments(flyConfig)).not.toMatch(/^\s*app\s*=/m);
    expect(stripComments(flyConfig)).not.toMatch(/\.fly\.dev/);
  });
});

describe("the auth gate covers everything below it", () => {
  it("puts the gate above BOTH the api routes and the static assets", () => {
    // Gating only /api/* would still ship the app shell to anonymous
    // visitors, and any future route outside that prefix would be public by
    // default. Order is the security property.
    const gate = serveSrc.indexOf("// ── 3. The gate");
    const api = serveSrc.indexOf("registerApiRoutes(app)");
    const statics = serveSrc.indexOf("// ── 5. Static assets");
    expect(gate).toBeGreaterThan(0);
    expect(api).toBeGreaterThan(gate);
    expect(statics).toBeGreaterThan(gate);
  });

  it("leaves only /healthz reachable without a session", () => {
    const gate = serveSrc.indexOf("// ── 3. The gate");
    const before = serveSrc.slice(0, gate);
    const routes = [...before.matchAll(/app\.use\("([^"]+)"/g)].map((m) => m[1]);
    expect(new Set(routes)).toEqual(new Set(["/healthz", "/auth/login", "/auth/callback"]));
  });

  it("fails closed when auth is unconfigured", () => {
    expect(serveSrc).toMatch(/Refusing to serve/);
  });

  it("answers an unauthenticated API call with 401 rather than an HTML redirect", () => {
    expect(serveSrc).toMatch(/url\.startsWith\("\/api\/"\)/);
  });

  it("never redirects to an absolute URL supplied by the caller", () => {
    // Otherwise /auth/login?next=https://evil is an open redirect.
    expect(serveSrc).toMatch(/raw\.startsWith\("\/"\) && !raw\.startsWith\("\/\/"\)/);
  });
});
