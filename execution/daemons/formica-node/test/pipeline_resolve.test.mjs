import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  buildAgentPrompt,
  issueNumberForBranch,
  resolveRepoEntry,
} from "../src/pipeline.mjs";

describe("pipeline repo resolution", () => {
  it("resolveRepoEntry matches key", () => {
    const r = resolveRepoEntry({ repository: "neotoma" }, {
      neotoma: { path: "/p/neotoma", worktree_base: "/tmp/n" },
    });
    assert.equal(r?.key, "neotoma");
    assert.equal(r?.cfg.path, "/p/neotoma");
  });

  it("issueNumberForBranch prefers github_number", () => {
    assert.equal(issueNumberForBranch({ github_number: 42 }), "42");
  });

  it("buildAgentPrompt prefers process-issues skill when available", async () => {
    const tmp = await fs.mkdtemp(path.join(os.tmpdir(), "formica-process-issues-"));
    try {
      const skillDir = path.join(tmp, ".cursor", "skills", "process-issues");
      await fs.mkdir(skillDir, { recursive: true });
      await fs.writeFile(path.join(skillDir, "SKILL.md"), "# process-issues\n", "utf8");

      const prompt = buildAgentPrompt({
        entityId: "ent_issue_42",
        workspacePath: tmp,
        issue: { title: "Race condition", body: "Reproduce under load." },
        classification: { classification: "bug_fix", notes: "auto" },
      });

      assert.match(prompt, /\/process-issues/);
      assert.match(prompt, /Scope the run to issue entity ent_issue_42 only/);
    } finally {
      await fs.rm(tmp, { recursive: true, force: true });
    }
  });
});
