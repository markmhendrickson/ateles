/**
 * Regression locks for task history values, writer convention parsing, and
 * provenance counts — the behaviors ateles#695 added and phoenicurus required
 * as evals before qa sign-off.
 */
import { describe, expect, it } from "vitest";
import {
  provenanceCoverage,
  toHistory,
  writerFromIdempotencyKey,
  type HistoryEntry,
  type Observation,
} from "./taskState";

function obs(partial: Partial<Observation> & { fields?: Observation["fields"] }): Observation {
  return {
    id: partial.id ?? "obs_1",
    observed_at: partial.observed_at ?? "2026-09-02T00:00:00.000Z",
    source_id: partial.source_id ?? null,
    fields: partial.fields ?? {},
    provenance: partial.provenance ?? null,
    idempotency_key: partial.idempotency_key ?? null,
  };
}

describe("toHistory", () => {
  it("keeps field values, not just keys (the values-not-keys regression)", () => {
    const history = toHistory([obs({ fields: { status: "blocked" } })]);
    expect(history).toHaveLength(1);
    expect(history[0].changes).toHaveLength(1);
    expect(history[0].changes[0].name).toBe("status");
    expect(history[0].changes[0].full).toBe("blocked");
    expect(history[0].changes[0].preview).toBe("blocked");
  });

  it("never emits [object Object] for nested field values", () => {
    const history = toHistory([
      obs({ fields: { gate_status: { pm: "signed_off" } } }),
    ]);
    const full = history[0].changes[0].full;
    expect(full).not.toBe("[object Object]");
    expect(full).not.toContain("[object Object]");
    expect(JSON.parse(full)).toEqual({ pm: "signed_off" });
  });
});

describe("writerFromIdempotencyKey", () => {
  it.each([
    ["taskstatus-apis-ent_abc-awaiting_approval-created", "apis"],
    ["a-b", null],
    ["update-ent_abc123-detail", null],
    [null, null],
  ] as const)("%s → %s", (key, expected) => {
    expect(writerFromIdempotencyKey(key)).toBe(expected);
  });
});

describe("provenanceCoverage", () => {
  it("returns attributed/total counts, not booleans", () => {
    const withClient = (clientName: string | null): HistoryEntry => ({
      at: null,
      changes: [],
      sourced: false,
      writer: { clientName, attributionTier: null, conventionName: null },
      idempotencyKey: null,
    });
    const history = [withClient("local-agent"), withClient(null), withClient(null)];
    expect(provenanceCoverage(history)).toEqual({ attributed: 1, total: 3 });
  });
});
