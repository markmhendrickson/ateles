import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { preflightDirtyTree, runGhCreatePr } from "../src/pr_manager.mjs";

describe("pr_manager", () => {
  it("preflight clean", async () => {
    const execGit = async () => ({ stdout: "", stderr: "" });
    const r = await preflightDirtyTree("/wt", "abort", { execGit });
    assert.equal(r.ok, true);
  });

  it("preflight abort on dirty", async () => {
    const execGit = async () => ({ stdout: " M file.txt\n", stderr: "" });
    const r = await preflightDirtyTree("/wt", "abort", { execGit });
    assert.equal(r.ok, false);
    assert.equal(r.mode, "dirty");
  });

  it("runGhCreatePr dryRun", async () => {
    const r = await runGhCreatePr({
      worktreePath: "/wt",
      branch: "b",
      title: "t",
      body: "b",
      prBaseBranch: "main",
      dryRun: true,
    });
    assert.equal(r.ok, true);
    assert.equal(r.dryRun, true);
  });
});
