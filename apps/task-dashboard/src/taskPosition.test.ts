/**
 * Lifecycle placement, issue-ref parsing, and gate-disagreement evals for
 * ateles#695 — including the critical non-fabrication of off-vocabulary
 * statuses onto on_lifecycle/done.
 */
import { describe, expect, it } from "vitest";
import {
  gateDisagreements,
  parseIssueRef,
  placeOnLifecycle,
  type WorkflowLink,
} from "./taskPosition";

describe("placeOnLifecycle", () => {
  it("places on-path statuses with a numeric pathIndex", () => {
    const pending = placeOnLifecycle("pending");
    expect(pending.kind).toBe("on_lifecycle");
    if (pending.kind === "on_lifecycle") {
      expect(pending.stage.key).toBe("pending");
      expect(typeof pending.pathIndex).toBe("number");
      expect(pending.pathIndex).toBeGreaterThanOrEqual(0);
    }

    const done = placeOnLifecycle("done");
    expect(done.kind).toBe("on_lifecycle");
    if (done.kind === "on_lifecycle") {
      expect(done.stage.key).toBe("done");
      expect(typeof done.pathIndex).toBe("number");
    }
  });

  it("places hold statuses on_lifecycle with pathIndex null", () => {
    const hold = placeOnLifecycle("awaiting_approval");
    expect(hold.kind).toBe("on_lifecycle");
    if (hold.kind === "on_lifecycle") {
      expect(hold.stage.key).toBe("awaiting_approval");
      expect(hold.pathIndex).toBeNull();
    }
  });

  it.each(["completed", "todo"] as const)(
    "marks %s off_vocabulary and never fabricates on_lifecycle/done",
    (raw) => {
      const pos = placeOnLifecycle(raw);
      expect(pos).toEqual({ kind: "off_vocabulary", raw, ungoverned: true });
      expect(pos.kind).not.toBe("on_lifecycle");
      if (pos.kind === "on_lifecycle") {
        // Guard for the exact fabrication the docstring forbids.
        expect(pos.stage.key).not.toBe("done");
      }
    },
  );

  it.each(["", null, undefined] as const)("maps empty/missing %j to no_status", (raw) => {
    expect(placeOnLifecycle(raw)).toEqual({ kind: "no_status" });
  });
});

describe("parseIssueRef", () => {
  it("parses a valid issue URL", () => {
    const ref = parseIssueRef("https://github.com/markmhendrickson/ateles/issues/695");
    expect(ref).toEqual({
      repo: "markmhendrickson/ateles",
      number: 695,
      url: "https://github.com/markmhendrickson/ateles/issues/695",
      isPullRequest: false,
    });
  });

  it("parses a valid PR URL with isPullRequest true", () => {
    const ref = parseIssueRef("https://github.com/markmhendrickson/ateles/pull/695");
    expect(ref).toEqual({
      repo: "markmhendrickson/ateles",
      number: 695,
      url: "https://github.com/markmhendrickson/ateles/pull/695",
      isPullRequest: true,
    });
  });

  it("returns null for non-github and malformed github URLs", () => {
    expect(parseIssueRef("https://example.com/issues/1")).toBeNull();
    expect(parseIssueRef("https://github.com/markmhendrickson/ateles/issues/")).toBeNull();
    expect(parseIssueRef(null)).toBeNull();
  });
});

describe("gateDisagreements", () => {
  const resolved = (
    gates: { gateName: string; status: string }[],
    participation: { gateName: string; status: string }[],
  ): WorkflowLink => ({
    kind: "resolved",
    ref: {
      repo: "markmhendrickson/ateles",
      number: 1,
      url: "https://github.com/markmhendrickson/ateles/issues/1",
      isPullRequest: false,
    },
    issueId: "ent_abc",
    workflowType: null,
    currentOwner: null,
    gates,
    participation,
  });

  it("returns shared gate names where statuses differ", () => {
    expect(
      gateDisagreements(
        resolved([{ gateName: "pm", status: "signed_off" }], [
          { gateName: "pm", status: "dispatched" },
        ]),
      ),
    ).toEqual(["pm"]);
  });

  it("returns [] when both sources agree", () => {
    expect(
      gateDisagreements(
        resolved([{ gateName: "pm", status: "signed_off" }], [
          { gateName: "pm", status: "signed_off" },
        ]),
      ),
    ).toEqual([]);
  });

  it("returns [] for one-sided gates (present only in gates)", () => {
    expect(
      gateDisagreements(resolved([{ gateName: "pm", status: "signed_off" }], [])),
    ).toEqual([]);
  });

  it("returns [] without throwing for non-resolved links", () => {
    expect(gateDisagreements({ kind: "none" })).toEqual([]);
    expect(
      gateDisagreements({
        kind: "unsupported_ref",
        ref: {
          repo: "markmhendrickson/ateles",
          number: 695,
          url: "https://github.com/markmhendrickson/ateles/pull/695",
          isPullRequest: true,
        },
        reason: "pull_request_not_supported",
      }),
    ).toEqual([]);
  });
});
