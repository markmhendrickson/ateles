import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { pickMinReleaseTag, parseRemoteSemverTags, resolveBaseCommit } from "../src/base_resolver.mjs";

describe("base_resolver", () => {
  it("parses semver tags from ls-remote", () => {
    const out = parseRemoteSemverTags(`
abc refs/tags/v1.0.0
def refs/tags/2.1.0
`);
    assert.ok(out.includes("v1.0.0"));
    assert.ok(out.includes("2.1.0"));
  });

  it("picks latest semver tag >= reporter", () => {
    const tags = ["1.0.0", "1.1.0", "2.0.0"];
    const best = pickMinReleaseTag(tags, "1.0.5");
    assert.equal(best, "2.0.0");
  });

  it("strict_reporter fails closed without sha", async () => {
    const execGit = async () => ({ stdout: "", stderr: "" });
    const r = await resolveBaseCommit({
      repoPath: "/tmp",
      defaultBranch: "main",
      policy: "strict_reporter",
      issue: {},
      execGit,
    });
    assert.equal(r.ok, false);
    assert.equal(r.reason, "missing_sha");
  });

  it("mainline resolves origin/main", async () => {
    const execGit = async (_cwd, args) => {
      if (args[0] === "fetch") return { stdout: "", stderr: "" };
      if (args[0] === "rev-parse") return { stdout: "deadbeef\n", stderr: "" };
      return { stdout: "", stderr: "" };
    };
    const r = await resolveBaseCommit({
      repoPath: "/tmp",
      defaultBranch: "main",
      policy: "mainline",
      issue: {},
      execGit,
    });
    assert.equal(r.ok, true);
    assert.equal(r.baseCommit, "deadbeef");
  });
});
