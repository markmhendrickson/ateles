/**
 * Contract tests for the Console's section reader.
 *
 * What they pin: a failed read is an error and never an empty result, a failure
 * is never cached, the cache expires, upstream reads never exceed two at once,
 * and one section failing leaves the others standing.
 */
import { describe, it, expect } from "vitest";
import {
  MAX_UPSTREAM_READS,
  SPINE_SECTIONS,
  createSpine,
  isSpineSection,
  queryFor,
  shapeSection,
} from "./consoleSpine";

const ok = (entities: unknown[], total = entities.length) => ({ entities, total });

describe("queryFor", () => {
  it("resolves the charter by role and status, with no hardcoded entity id", () => {
    const q = queryFor("charter");
    expect(q.entity_type).toBe("strategy");
    expect(q.snapshot_filters).toMatchObject({
      role: { op: "eq", value: "responsibility_charter" },
      status: { op: "eq", value: "active" },
    });
    expect(JSON.stringify(SPINE_SECTIONS.map(queryFor))).not.toMatch(/ent_[0-9a-f]{6,}/);
  });

  it("counts retired strategies without fetching rows", () => {
    const q = queryFor("retired");
    expect(q.include_snapshots).toBe(false);
    expect(q.limit).toBe(1);
  });
});

describe("isSpineSection", () => {
  it("accepts the fixed list and nothing else", () => {
    expect(isSpineSection("charter")).toBe(true);
    expect(isSpineSection("")).toBe(false);
    expect(isSpineSection("task")).toBe(false);
    expect(isSpineSection("__proto__")).toBe(false);
  });
});

describe("shapeSection", () => {
  it("keeps only the named fields, and prose only for charter and missions", () => {
    const raw = ok([
      {
        entity_id: "ent_1",
        canonical_name: "n",
        snapshot: { title: "T", status: "active", content: "body", secret_field: "x", altitude: "keystone" },
      },
    ]);
    const strategies = shapeSection("strategies", raw) as unknown as { records: Record<string, unknown>[] };
    expect(strategies.records[0].content).toBeNull();
    expect(strategies.records[0]).not.toHaveProperty("secret_field");
    expect(strategies.records[0].altitude).toBe("keystone");
    const charter = shapeSection("charter", raw) as unknown as { records: Record<string, unknown>[] };
    expect(charter.records[0].content).toBe("body");
  });

  it("treats a response with no total (retired) or no entity list as a failure, not zero", () => {
    expect(() => shapeSection("retired", {})).toThrow();
    expect(() => shapeSection("strategies", {})).toThrow();
    expect(shapeSection("retired", { total: 0 })).toEqual({ total: 0 });
  });
});

describe("createSpine", () => {
  it("returns a section error, not an empty list, when the read fails", async () => {
    const spine = createSpine({ query: async () => { throw new Error("Neotoma returned HTTP 503"); } });
    const r = await spine("missions");
    expect(r).toEqual({ ok: false, error: "Neotoma returned HTTP 503" });
  });

  it("does not cache a failure: the next request reads again", async () => {
    let calls = 0;
    const spine = createSpine({
      query: async () => {
        calls++;
        if (calls === 1) throw new Error("timeout");
        return ok([]);
      },
    });
    expect((await spine("projects")).ok).toBe(false);
    expect((await spine("projects")).ok).toBe(true);
    expect(calls).toBe(2);
  });

  it("serves a success from cache inside the TTL and re-reads after it", async () => {
    let t = 1_000;
    let calls = 0;
    const spine = createSpine({
      now: () => t,
      ttlMs: 60_000,
      query: async () => {
        calls++;
        return ok([]);
      },
    });
    await spine("missions");
    t += 59_000;
    await spine("missions");
    expect(calls).toBe(1);
    t += 2_000;
    await spine("missions");
    expect(calls).toBe(2);
  });

  it("shares one upstream read between concurrent viewers", async () => {
    let calls = 0;
    const spine = createSpine({
      query: async () => {
        calls++;
        await new Promise((r) => setTimeout(r, 5));
        return ok([]);
      },
    });
    await Promise.all([spine("charter"), spine("charter"), spine("charter")]);
    expect(calls).toBe(1);
  });

  it("never has more than two upstream reads in flight across sections", async () => {
    let running = 0;
    let peak = 0;
    const spine = createSpine({
      query: async (body) => {
        running++;
        peak = Math.max(peak, running);
        await new Promise((r) => setTimeout(r, 10));
        running--;
        return body.entity_type === "strategy" && body.limit === 1 ? { total: 3 } : ok([]);
      },
    });
    const results = await Promise.all(SPINE_SECTIONS.map((s) => spine(s)));
    expect(results.every((r) => r.ok)).toBe(true);
    expect(peak).toBeLessThanOrEqual(MAX_UPSTREAM_READS);
    expect(peak).toBe(MAX_UPSTREAM_READS);
  });

  it("lets one failing section fail alone", async () => {
    const spine = createSpine({
      query: async (body) => {
        if (body.entity_type === "mission") throw new Error("slow type timed out");
        return body.limit === 1 ? { total: 2 } : ok([]);
      },
    });
    const [missions, strategies, retired] = await Promise.all([
      spine("missions"),
      spine("strategies"),
      spine("retired"),
    ]);
    expect(missions.ok).toBe(false);
    expect(strategies.ok).toBe(true);
    expect(retired).toMatchObject({ ok: true, total: 2 });
  });
});
