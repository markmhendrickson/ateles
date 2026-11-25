/**
 * The LaunchAgent installer (deploy/install_console.sh), run for real against
 * a throwaway HOME with fake `node`, `npm` and `launchctl` on PATH. Each fake
 * records its invocation to a log; nothing touches the real launchd or builds.
 *
 * Pins: a dry run writes nothing and invokes nothing; a real install is
 * idempotent; the plist carries no credential and runs the local-only mode.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { execFileSync } from "node:child_process";
import { chmodSync, existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const APP_DIR = join(dirname(fileURLToPath(import.meta.url)), "..");
const SCRIPT = join(APP_DIR, "deploy", "install_console.sh");
const TEMPLATE = join(APP_DIR, "deploy", "com.ateles.console.plist.template");

let root: string;
let home: string;
let bin: string;
let callLog: string;

function fake(name: string) {
  const p = join(bin, name);
  writeFileSync(p, `#!/bin/sh\necho "${name} $*" >> "${callLog}"\nexit 0\n`);
  chmodSync(p, 0o755);
}

function install(...args: string[]): string {
  return execFileSync("bash", [SCRIPT, ...args], {
    encoding: "utf8",
    env: { HOME: home, PATH: `${bin}:/usr/bin:/bin` },
  });
}

const calls = () => (existsSync(callLog) ? readFileSync(callLog, "utf8").trim().split("\n") : []);
const plistPath = () => join(home, "Library", "LaunchAgents", "com.ateles.console.plist");

beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), "console-install-"));
  home = join(root, "home");
  bin = join(root, "bin");
  callLog = join(root, "calls.log");
  mkdirSync(home);
  mkdirSync(bin);
  for (const n of ["node", "npm", "launchctl"]) fake(n);
});
afterEach(() => rmSync(root, { recursive: true, force: true }));

describe("install_console.sh --dry-run", () => {
  it("prints the plan and the rendered plist, and writes and invokes nothing", () => {
    const out = install("--dry-run");
    expect(readdirSync(home)).toEqual([]);
    expect(calls()).toEqual([]);
    expect(out).toMatch(/\[dry-run\] would run: npm --prefix .* ci/);
    expect(out).toMatch(/\[dry-run\] would run: npm --prefix .* run build:deploy/);
    expect(out).toMatch(/\[dry-run\] would run: launchctl load .*com\.ateles\.console\.plist/);
    expect(out).toMatch(/<key>DASHBOARD_LOCAL_ONLY<\/key>\s*<string>1<\/string>/);
    expect(out).toMatch(/<key>PORT<\/key>\s*<string>5273<\/string>/);
    expect(out).toMatch(/http:\/\/127\.0\.0\.1:5273\//);
  });

  it("previews an uninstall without removing anything", () => {
    const out = install("--uninstall", "--dry-run");
    expect(out).toMatch(/\[dry-run\] would run: rm -f .*com\.ateles\.console\.plist/);
    expect(calls()).toEqual([]);
  });
});

describe("install_console.sh", () => {
  it("installs a plist that runs the built server local-only, with KeepAlive and per-user logs", () => {
    install();
    const plist = readFileSync(plistPath(), "utf8");
    expect(plist).toMatch(/<string>[^<]*\/node<\/string>\s*<string>[^<]*\/server-dist\/serve\.js<\/string>/);
    expect(plist).toMatch(/<key>KeepAlive<\/key>\s*<true\/>/);
    expect(plist).toMatch(/<key>RunAtLoad<\/key>\s*<true\/>/);
    expect(plist).toContain(`${home}/Library/Logs/ateles/com.ateles.console.log`);
    expect(existsSync(join(home, "Library", "Logs", "ateles"))).toBe(true);
    expect(plist).not.toMatch(/\{\{/);
    expect(calls()).toEqual([
      expect.stringMatching(/^npm --prefix .* ci/),
      expect.stringMatching(/^npm --prefix .* run build:deploy$/),
      "launchctl list",
      expect.stringMatching(/^launchctl load .*com\.ateles\.console\.plist$/),
    ]);
  });

  it("carries no secret: no credential key in the template or the installed plist", () => {
    install();
    for (const text of [readFileSync(TEMPLATE, "utf8"), readFileSync(plistPath(), "utf8")]) {
      expect(text).not.toMatch(/<key>[^<]*(NEOTOMA|TOKEN|SECRET)[^<]*<\/key>/);
      expect(text).not.toMatch(/Bearer/);
    }
  });

  it("is idempotent: a second run writes the same plist and reloads", () => {
    install();
    const first = readFileSync(plistPath(), "utf8");
    install();
    expect(readFileSync(plistPath(), "utf8")).toBe(first);
    expect(calls().filter((c) => c.startsWith("launchctl load"))).toHaveLength(2);
  });

  it("uninstalls: unloads and removes the plist", () => {
    install();
    install("--uninstall");
    expect(existsSync(plistPath())).toBe(false);
    expect(calls()).toContain(`launchctl unload ${plistPath()}`);
  });
});
