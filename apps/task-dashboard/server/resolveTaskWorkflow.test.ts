/**
 * Contract tests for `/api/task-workflow` via the extracted `resolveTaskWorkflow`
 * core + the malformed-id guard the route uses. Mocks Neotoma accessors; no
 * live network.
 */
import { describe, expect, it, vi } from "vitest";
import {
  isMalformedEntityId,
  resolveTaskWorkflow,
  type NeotomaAccess,
} from "../server/resolveTaskWorkflow";

function taskEntity(fields: Record<string, unknown>) {
  return { entity_id: "ent_task1", snapshot: fields };
}

function issueEntity(
  entityId: string,
  fields: Record<string, unknown>,
): Record<string, unknown> {
  return { entity_id: entityId, snapshot: fields };
}

function access(opts: {
  task?: Record<string, unknown> | null;
  issues?: Record<string, unknown>[];
  participation?: Record<string, unknown>[] | (() => never);
}): NeotomaAccess {
  return {
    get: vi.fn(async () => opts.task ?? null),
    post: vi.fn(async (_path: string, body: unknown) => {
      const b = body as { entity_type?: string };
      if (b.entity_type === "issue") {
        return { entities: opts.issues ?? [] };
      }
      if (b.entity_type === "participation_record") {
        if (typeof opts.participation === "function") {
          opts.participation();
        }
        return { entities: (opts.participation as Record<string, unknown>[] | undefined) ?? [] };
      }
      return { entities: [] };
    }),
  };
}

describe("isMalformedEntityId", () => {
  it("rejects non-entity and uppercase ids (route → 400)", () => {
    expect(isMalformedEntityId("not-an-entity")).toBe(true);
    expect(isMalformedEntityId("ent_")).toBe(true);
    expect(isMalformedEntityId("ent_ABCDEF")).toBe(true);
    expect(isMalformedEntityId("ent_abc123")).toBe(false);
  });
});

describe("resolveTaskWorkflow", () => {
  it("returns none when the task has no source_url / permalink_url", async () => {
    const link = await resolveTaskWorkflow(
      "ent_task1",
      access({ task: taskEntity({ title: "lonely" }) }),
    );
    expect(link).toEqual({ kind: "none" });
  });

  it("returns unsupported_ref for /pull/ URLs without querying issues", async () => {
    const a = access({
      task: taskEntity({
        source_url: "https://github.com/markmhendrickson/ateles/pull/695",
      }),
      issues: [
        issueEntity("ent_should_not_hit", {
          issue_number: 695,
          repo: "markmhendrickson/ateles",
        }),
      ],
    });
    const link = await resolveTaskWorkflow("ent_task1", a);
    expect(link.kind).toBe("unsupported_ref");
    if (link.kind === "unsupported_ref") {
      expect(link.reason).toBe("pull_request_not_supported");
      expect(link.ref.isPullRequest).toBe(true);
      expect(link.ref.number).toBe(695);
    }
    expect(a.post).not.toHaveBeenCalled();
  });

  it("returns dangling when the issue regex matches but no entity is found", async () => {
    const link = await resolveTaskWorkflow(
      "ent_task1",
      access({
        task: taskEntity({
          source_url: "https://github.com/markmhendrickson/ateles/issues/410",
        }),
        issues: [],
      }),
    );
    expect(link.kind).toBe("dangling");
    if (link.kind === "dangling") {
      expect(link.ref.repo).toBe("markmhendrickson/ateles");
      expect(link.ref.number).toBe(410);
      expect(link.ref.isPullRequest).toBe(false);
    }
  });

  it("matches repo+number, not number alone (cross-link guard)", async () => {
    const atelesId = "ent_ateles410";
    const link = await resolveTaskWorkflow(
      "ent_task1",
      access({
        task: taskEntity({
          source_url: "https://github.com/markmhendrickson/ateles/issues/410",
        }),
        issues: [
          issueEntity("ent_neotoma410", {
            issue_number: 410,
            repo: "markmhendrickson/neotoma",
            gate_status: { pm: "signed_off" },
          }),
          issueEntity(atelesId, {
            issue_number: 410,
            repo: "markmhendrickson/ateles",
            gate_status: { arch: "pending" },
          }),
        ],
      }),
    );
    expect(link.kind).toBe("resolved");
    if (link.kind === "resolved") {
      expect(link.issueId).toBe(atelesId);
      expect(link.gates).toEqual([{ gateName: "arch", status: "pending" }]);
    }
  });

  it("returns issue_without_gates when issue exists but both writers are empty", async () => {
    const link = await resolveTaskWorkflow(
      "ent_task1",
      access({
        task: taskEntity({
          source_url: "https://github.com/markmhendrickson/ateles/issues/99",
        }),
        issues: [
          issueEntity("ent_issue99", {
            issue_number: 99,
            repo: "markmhendrickson/ateles",
          }),
        ],
        participation: [],
      }),
    );
    expect(link).toEqual({
      kind: "issue_without_gates",
      ref: {
        repo: "markmhendrickson/ateles",
        number: 99,
        url: "https://github.com/markmhendrickson/ateles/issues/99",
        isPullRequest: false,
      },
      issueId: "ent_issue99",
    });
  });

  it("returns resolved with gates and participation kept unmerged", async () => {
    const link = await resolveTaskWorkflow(
      "ent_task1",
      access({
        task: taskEntity({
          source_url: "https://github.com/markmhendrickson/neotoma/issues/2266",
        }),
        issues: [
          issueEntity("ent_issue2266", {
            issue_number: 2266,
            repo: "markmhendrickson/neotoma",
            workflow_type: "feature",
            current_owner: "cicada",
            gate_status: { pm: "signed_off", qa: "pending" },
          }),
        ],
        participation: [
          {
            entity_id: "ent_part1",
            snapshot: { gate_name: "pm", status: "dispatched", work_entity_id: "ent_issue2266" },
          },
        ],
      }),
    );
    expect(link.kind).toBe("resolved");
    if (link.kind === "resolved") {
      expect(link.gates).toEqual([
        { gateName: "pm", status: "signed_off" },
        { gateName: "qa", status: "pending" },
      ]);
      expect(link.participation).toEqual([{ gateName: "pm", status: "dispatched" }]);
      // Unmerged: both arrays present and distinct — not reconciled into one.
      expect(link.gates).not.toEqual(link.participation);
      expect(link.gates.find((g) => g.gateName === "pm")?.status).toBe("signed_off");
      expect(link.participation.find((g) => g.gateName === "pm")?.status).toBe("dispatched");
    }
  });

  it("falls back to empty participation when the participation query throws", async () => {
    const link = await resolveTaskWorkflow(
      "ent_task1",
      access({
        task: taskEntity({
          source_url: "https://github.com/markmhendrickson/ateles/issues/50",
        }),
        issues: [
          issueEntity("ent_issue50", {
            issue_number: 50,
            repo: "markmhendrickson/ateles",
            gate_status: { impl: "pending" },
          }),
        ],
        participation: () => {
          throw new Error("Neotoma returned HTTP 500");
        },
      }),
    );
    expect(link.kind).toBe("resolved");
    if (link.kind === "resolved") {
      expect(link.gates).toEqual([{ gateName: "impl", status: "pending" }]);
      expect(link.participation).toEqual([]);
    }
  });
});
