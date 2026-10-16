/**
 * TESTS FOR THE TASK SEARCH CONTRACT.
 *
 * These cover the properties that, if they broke, would put a confident wrong
 * answer on screen — which is the failure mode this page has had repeatedly.
 * Each block names the production measurement it is defending, so a future
 * change that contradicts one has to argue with the data rather than the test.
 *
 * The upstream FACTS (measured 2026-09-02 against 21,285 live task entities)
 * are asserted in `taskSearch.ts`'s header and re-checked here where they are
 * expressible client-side. What cannot be unit-tested — that upstream ANDs
 * tokens, drops filters beside `search`, and does not fold accents — is
 * exercised by the guards below, which exist precisely so that a change in
 * upstream behaviour degrades safely instead of silently.
 */
import { describe, expect, it } from "vitest";
import {
  KNOWN_ACCENT_VARIANTS,
  MIN_QUERY_LENGTH,
  OPEN_TASK_STATUSES,
  type SearchRow,
  applyFilters,
  combineTotals,
  fold,
  hasAccents,
  isImported,
  matchesAllTokens,
  searchSaturated,
  searchVariants,
  typeNameCollision,
  unionRows,
} from "./taskSearch";
import { FILTERED_TOTAL_CEILING, type Count, countText } from "./taskCount";

function row(over: Partial<SearchRow> = {}): SearchRow {
  return {
    id: "ent_1",
    title: "A task",
    status: "pending",
    priority: null,
    assignedTo: null,
    updatedAt: new Date("2026-09-01T00:00:00Z"),
    undispatched: true,
    question: false,
    imported: false,
    haystack: "a task",
    ...over,
  };
}

describe("fold / hasAccents", () => {
  it("strips diacritics and lowercases", () => {
    expect(fold("Theodóre")).toBe("theodore");
    expect(fold("ÁÉÍÓÚñ")).toBe("aeioun");
    expect(fold("albarán")).toBe("albaran");
  });

  it("detects accented input", () => {
    expect(hasAccents("Theodóre")).toBe(true);
    expect(hasAccents("theodore")).toBe(false);
  });
});

describe("matchesAllTokens — AND across tokens, never OR", () => {
  /**
   * The operator caught the sessions search matching ANY token when he
   * expected ALL. Upstream ANDs today ("release" 334, "dashboard" 74,
   * "release dashboard" 3), and this guard means that if it ever switches to
   * OR the operator sees FEWER rows rather than wrong ones.
   */
  it("requires every token to be present", () => {
    expect(matchesAllTokens("release the dashboard", "release dashboard")).toBe(true);
    expect(matchesAllTokens("release notes", "release dashboard")).toBe(false);
    expect(matchesAllTokens("dashboard only", "release dashboard")).toBe(false);
  });

  it("matches tokens in any order and across fields", () => {
    expect(matchesAllTokens("dashboard … release", "release dashboard")).toBe(true);
  });

  it("folds accents on both sides, so the guard does not re-break search", () => {
    expect(matchesAllTokens("Theodóre project", "theodore")).toBe(true);
    expect(matchesAllTokens("Theodore project", "theodóre")).toBe(true);
  });

  it("is substring-based, matching upstream's behaviour", () => {
    // Upstream: "eleas" returns the same 334 rows as "release".
    expect(matchesAllTokens("release", "eleas")).toBe(true);
  });

  it("treats an empty query as matching everything", () => {
    expect(matchesAllTokens("anything", "")).toBe(true);
  });
});

describe("searchVariants — the client compensates for no accent folding", () => {
  /**
   * Measured: search "theodore" -> 53 rows, 0 of which hold only the accented
   * spelling; search "theodóre" -> 48 rows, 0 of which hold only the plain one.
   * Neither set contains the other, so one query always misses rows.
   */
  it("sends the folded spelling alongside an accented query", () => {
    expect(searchVariants("theodóre")).toEqual(["theodóre", "theodore"]);
  });

  it("sends known accented spellings for an unaccented query", () => {
    expect(searchVariants("theodore", KNOWN_ACCENT_VARIANTS)).toEqual([
      "theodore",
      "theodóre",
    ]);
  });

  it("matches the known-variant key case-insensitively", () => {
    expect(searchVariants("Theodore", KNOWN_ACCENT_VARIANTS)).toEqual([
      "Theodore",
      "theodóre",
    ]);
  });

  it("always puts the operator's own spelling first", () => {
    expect(searchVariants("theodóre")[0]).toBe("theodóre");
    expect(searchVariants("Theodore", KNOWN_ACCENT_VARIANTS)[0]).toBe("Theodore");
  });

  it("sends one query when there is no variant to add", () => {
    expect(searchVariants("release")).toEqual(["release"]);
  });

  it("never duplicates a spelling", () => {
    const v = searchVariants("theodore", { theodore: ["theodore", "theodóre"] });
    expect(new Set(v).size).toBe(v.length);
  });

  it("returns nothing for an empty query", () => {
    expect(searchVariants("   ")).toEqual([]);
  });
});

describe("typeNameCollision — the upstream zero that means nothing", () => {
  /**
   * Reproducible on four types: search "task" on type `task` -> 0, while the
   * same term returns 112 on `plan` and 435 on `issue`. Not a stopword —
   * "subtask" (10), "tasked" (2), "multitask" (2) all match.
   */
  it("flags the exact type name", () => {
    expect(typeNameCollision("task")).toBe(true);
    expect(typeNameCollision("  Task  ")).toBe(true);
  });

  it("does not flag terms that merely contain it", () => {
    expect(typeNameCollision("subtask")).toBe(false);
    expect(typeNameCollision("tasked")).toBe(false);
    expect(typeNameCollision("task list")).toBe(false);
  });
});

describe("combineTotals — overlapping variants must not be summed", () => {
  /**
   * 36 of the theodore/theodóre rows appear in BOTH result sets, so 53 + 48
   * would be a fabricated 101. The honest combined figure is a lower bound.
   */
  it("returns an exact count for a single measured variant", () => {
    expect(combineTotals([{ kind: "exact", value: 334 }])).toEqual({
      kind: "exact",
      value: 334,
    });
  });

  it("returns a lower bound when several variants were merged", () => {
    const merged = combineTotals([
      { kind: "exact", value: 53 },
      { kind: "exact", value: 48 },
    ]);
    expect(merged).toEqual({ kind: "atLeast", value: 53 });
    // And it renders with its bound marker, never as a bare numeral.
    expect(countText(merged)).toBe("≥53");
  });

  it("never sums overlapping totals", () => {
    const merged = combineTotals([
      { kind: "exact", value: 53 },
      { kind: "exact", value: 48 },
    ]);
    expect((merged as { value: number }).value).not.toBe(101);
  });

  it("is unmeasured when any variant failed, rather than an undercount", () => {
    expect(
      combineTotals([{ kind: "exact", value: 53 }, { kind: "unmeasured" }]),
    ).toEqual({ kind: "unmeasured" });
  });

  it("propagates an existing lower bound", () => {
    expect(combineTotals([{ kind: "atLeast", value: 10000 }])).toEqual({
      kind: "atLeast",
      value: 10000,
    });
  });

  it("is unmeasured with no variants at all", () => {
    expect(combineTotals([])).toEqual({ kind: "unmeasured" });
  });
});

describe("searchSaturated — the 10,000 ceiling", () => {
  /**
   * A search-only total does NOT saturate: "a" reports 21,369, above the
   * ceiling. The clamp applies only when `snapshot_filters` are sent, which
   * this feature never does — but the guard keeps that from silently changing.
   */
  it("is false for a search-only total, even at the ceiling value", () => {
    expect(searchSaturated(FILTERED_TOTAL_CEILING, false)).toBe(false);
    expect(searchSaturated(21_369, false)).toBe(false);
  });

  it("is true only when filters were sent AND the total sits at the ceiling", () => {
    expect(searchSaturated(FILTERED_TOTAL_CEILING, true)).toBe(true);
    expect(searchSaturated(9_999, true)).toBe(false);
  });

  it("is false for an absent total", () => {
    expect(searchSaturated(null, true)).toBe(false);
    expect(searchSaturated(undefined, true)).toBe(false);
  });
});

describe("unionRows — variant result sets overlap", () => {
  it("keeps each entity once, first occurrence winning", () => {
    const a = [row({ id: "ent_a", title: "from plain" })];
    const b = [row({ id: "ent_a", title: "from accented" }), row({ id: "ent_b" })];
    const merged = unionRows([a, b]);
    expect(merged.map((r) => r.id)).toEqual(["ent_a", "ent_b"]);
    expect(merged[0].title).toBe("from plain");
  });

  it("handles empty sets", () => {
    expect(unionRows([[], []])).toEqual([]);
  });
});

describe("applyFilters — search COMPOSES with the page's filters", () => {
  /**
   * Applied client-side because upstream DROPS `snapshot_filters` when
   * `search` is present: search="release" + status=pending returned the same
   * 334 total as the unfiltered search, and of 40 rows only 9 were `pending`.
   */
  const base = { assignedTo: "", priority: "", staleDays: "", chip: "all", hideImported: false };

  it("keeps only open statuses under the open scope", () => {
    const rows = [
      row({ id: "a", status: "pending" }),
      row({ id: "b", status: "completed" }),
      row({ id: "c", status: "in_progress" }),
    ];
    const kept = applyFilters(rows, { ...base, status: "open" }, OPEN_TASK_STATUSES);
    expect(kept.map((r) => r.id)).toEqual(["a", "c"]);
  });

  it("keeps everything under the any scope", () => {
    const rows = [row({ id: "a", status: "pending" }), row({ id: "b", status: "completed" })];
    expect(applyFilters(rows, { ...base, status: "any" }, OPEN_TASK_STATUSES)).toHaveLength(2);
  });

  it("matches an exact status", () => {
    const rows = [row({ id: "a", status: "blocked" }), row({ id: "b", status: "pending" })];
    const kept = applyFilters(rows, { ...base, status: "blocked" }, OPEN_TASK_STATUSES);
    expect(kept.map((r) => r.id)).toEqual(["a"]);
  });

  it("filters by owner and by priority", () => {
    const rows = [
      row({ id: "a", assignedTo: "Cicada", priority: "high" }),
      row({ id: "b", assignedTo: "Apis", priority: "high" }),
      row({ id: "c", assignedTo: "Cicada", priority: "low" }),
    ];
    expect(
      applyFilters(rows, { ...base, status: "any", assignedTo: "Cicada" }, OPEN_TASK_STATUSES)
        .map((r) => r.id),
    ).toEqual(["a", "c"]);
    expect(
      applyFilters(rows, { ...base, status: "any", priority: "high" }, OPEN_TASK_STATUSES)
        .map((r) => r.id),
    ).toEqual(["a", "b"]);
  });

  it("composes owner, priority and the undispatched chip together", () => {
    // "Undispatched, high priority, matching X" — the query worth asking.
    const rows = [
      row({ id: "a", priority: "high", assignedTo: null, undispatched: true }),
      row({ id: "b", priority: "high", assignedTo: "Apis", undispatched: false }),
      row({ id: "c", priority: "low", assignedTo: null, undispatched: true }),
    ];
    const kept = applyFilters(
      rows,
      { ...base, status: "any", priority: "high", chip: "undispatched" },
      OPEN_TASK_STATUSES,
    );
    expect(kept.map((r) => r.id)).toEqual(["a"]);
  });

  it("applies the staleness cutoff against a fixed now", () => {
    const now = new Date("2026-09-02T00:00:00Z");
    const rows = [
      row({ id: "fresh", updatedAt: new Date("2026-09-01T00:00:00Z") }),
      row({ id: "stale", updatedAt: new Date("2026-06-01T00:00:00Z") }),
      row({ id: "undated", updatedAt: null }),
    ];
    const kept = applyFilters(
      rows,
      { ...base, status: "any", staleDays: "30" },
      OPEN_TASK_STATUSES,
      now,
    );
    // An undated row is NOT assumed stale — that would invent a fact.
    expect(kept.map((r) => r.id)).toEqual(["stale"]);
  });

  it("shows import residue by default and hides it only on request", () => {
    const rows = [row({ id: "swarm" }), row({ id: "asana", imported: true })];
    expect(
      applyFilters(rows, { ...base, status: "any" }, OPEN_TASK_STATUSES).map((r) => r.id),
    ).toEqual(["swarm", "asana"]);
    expect(
      applyFilters(rows, { ...base, status: "any", hideImported: true }, OPEN_TASK_STATUSES)
        .map((r) => r.id),
    ).toEqual(["swarm"]);
  });
});

describe("isImported — Asana residue detection", () => {
  it("recognizes both markers the import left behind", () => {
    expect(isImported({ import_source_file: "asana_api_direct" })).toBe(true);
    // Truthiness-only check — use a non-numeric fixture so gitleaks does not
    // treat a 16-digit Asana GID shape as `asana-client-id`.
    expect(isImported({ asana_source_gid: "fixture-asana-source-gid" })).toBe(true);
  });

  it("does not flag swarm-filed tasks", () => {
    expect(isImported({ status: "pending", title: "Do the thing" })).toBe(false);
  });
});

describe("open-status vocabulary", () => {
  it("is shared with the proxy rather than duplicated", () => {
    // The list the server filters `scope=open` by must be the list the client
    // filters search rows by, or the two views disagree about "open work".
    expect(OPEN_TASK_STATUSES).toContain("pending");
    expect(OPEN_TASK_STATUSES).toContain("awaiting_release_confirmation");
    expect(OPEN_TASK_STATUSES).not.toContain("completed");
    expect(OPEN_TASK_STATUSES).not.toContain("done");
  });
});

describe("MIN_QUERY_LENGTH", () => {
  it("refuses a single character, which matches thousands of rows", () => {
    expect(MIN_QUERY_LENGTH).toBeGreaterThanOrEqual(2);
  });
});

describe("countText contract holds for search totals", () => {
  it("never renders a lower bound as a bare numeral", () => {
    const bound: Count = { kind: "atLeast", value: 10_000 };
    expect(countText(bound)).toBe("≥10,000");
    expect(countText({ kind: "unmeasured" })).toBe("not measured");
  });
});
