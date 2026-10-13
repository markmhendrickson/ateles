import { describe, it, expect } from "vitest";
import {
  EXPECTED_AREA_COUNT,
  areaHasFields,
  NO_ALTITUDE,
  decisionsWaiting,
  groupByAltitude,
  hasDuplicateMissions,
  missionStatement,
  parseCharter,
  resolveProjectRefs,
  splitList,
} from "./consoleData";
import {
  CHARTER_REWORDED_7,
  CHARTER_TEXT,
  MISSION_A,
  MISSION_B,
  MISSION_TEXT,
  PROJECTS,
  STRATEGIES,
  record,
} from "./consoleFixtures";

describe("parseCharter on a well-formed charter", () => {
  const parse = parseCharter(CHARTER_TEXT);

  it("finds exactly seven areas, in order, with no problems", () => {
    expect(parse.areas.map((a) => a.number)).toEqual([1, 2, 3, 4, 5, 6, 7]);
    expect(parse.areas).toHaveLength(EXPECTED_AREA_COUNT);
    expect(parse.missingNumbers).toEqual([]);
    expect(parse.problems).toEqual([]);
    expect(parse.areas.every((a) => a.parsed)).toBe(true);
  });

  it("does not read the cross-cutting numbered rules as areas", () => {
    expect(parse.areas.map((a) => a.name)).not.toContain("Measure.");
    expect(parse.areas[0].name).toBe("Product building");
  });

  it("reads the Owns sentence up to the next label, keeping inner colons", () => {
    expect(parse.areas[0].owns).toBe(
      "turning signals into a ranked roadmap, then building and releasing it. Experiments: copy tests run weekly.",
    );
  });

  it("does not take lower-case mid-sentence 'strategy:' for a label", () => {
    const a2 = parse.areas[1];
    expect(a2.owns).toContain("Pattern from the personal strategy: protect, then feed.");
    expect(a2.strategies).toEqual(["content and distribution"]);
  });

  it("reads project names, the unnamed remainder, and the no-project case", () => {
    expect(parse.areas[0].projectNames).toEqual(["P_ALPHA", "P_BETA"]);
    expect(parse.areas[2].projectNames).toEqual(["P_MONEY"]);
    expect(parse.areas[2].projectOther).toEqual(["the client projects"]);
    expect(parse.areas[3].projectOther).toEqual(["operations"]);
    expect(parse.areas[6].projectsNone).toBe(true);
    expect(parse.areas[6].projectNames).toEqual([]);
    expect(parse.areas[0].projectsNone).toBe(false);
  });

  it("splits lists on semicolons, or on commas outside parentheses", () => {
    expect(parse.areas[0].strategies).toEqual(["Adoption and revenue", "two-stage test"]);
    expect(parse.areas[0].workflows).toEqual([
      "merge path (feature, bug, release)",
      "intake",
      "research and analysis",
    ]);
    expect(parse.areas[0].missing).toEqual(["weekly roadmap proposal", "outcome scoring"]);
    expect(parse.areas[2].missing).toEqual([
      "reconciliation and runway reporting",
      "tax preparation",
      "invoicing",
    ]);
  });

  it("separates 'none yet' from 'label not found'", () => {
    expect(parse.areas[1].workflowsNone).toBe(true);
    expect(parse.areas[1].workflows).toEqual([]);
    // Area 4 has no Missing line at all: null (unparsed), not an empty list.
    expect(parse.areas[3].missing).toBeNull();
    // Area 3 has no Strategies line.
    expect(parse.areas[2].strategies).toBeNull();
  });
});

describe("parseCharter degrades instead of crashing", () => {
  it("keeps a reworded heading as an unparsed area and reports the number missing", () => {
    const parse = parseCharter(CHARTER_REWORDED_7);
    expect(parse.areas).toHaveLength(7);
    const bad = parse.areas.filter((a) => !a.parsed);
    expect(bad).toHaveLength(1);
    expect(bad[0].number).toBeNull();
    expect(bad[0].name).toContain("Seven - Personal enrichment");
    expect(bad[0].raw).toContain("curated feed");
    // The fields of an unparsed area are not guessed.
    expect(bad[0].owns).toBeNull();
    expect(bad[0].projectsNone).toBeNull();
    expect(parse.missingNumbers).toEqual([7]);
    expect(parse.areas.filter((a) => a.parsed)).toHaveLength(6);
  });

  it.each([null, undefined, "", "   ", 42 as unknown as string])(
    "returns a result with a problem for content %p",
    (content) => {
      const parse = parseCharter(content as string | null);
      expect(parse.areas).toEqual([]);
      expect(parse.missingNumbers).toHaveLength(7);
      expect(parse.problems.length).toBeGreaterThan(0);
    },
  );

  it("reports a charter with no Areas heading and still reads the headings", () => {
    const parse = parseCharter("### 1. Only one\nOwns: a thing. Projects: P_X.\n");
    expect(parse.areas).toHaveLength(1);
    expect(parse.problems.join(" ")).toMatch(/Areas/);
    expect(parse.problems.join(" ")).toMatch(/Expected 7 areas/);
    expect(parse.missingNumbers).toEqual([2, 3, 4, 5, 6, 7]);
  });

  it("reports an area count other than seven", () => {
    const six = CHARTER_TEXT.replace(/### 7\.[\s\S]*$/, "");
    const parse = parseCharter(six);
    expect(parse.areas).toHaveLength(6);
    expect(parse.missingNumbers).toEqual([7]);
    expect(parse.problems.join(" ")).toMatch(/Expected 7 areas.*yields 6/);
  });

  it("never throws on garbage", () => {
    for (const junk of ["###", "### ", "## Areas\n###\n### 1.", "Owns: Projects: Missing:", "\u0000\n\n\n"]) {
      expect(() => parseCharter(junk)).not.toThrow();
    }
  });

  it("marks an area whose body has no recognised label as having no fields", () => {
    const parse = parseCharter("## Areas\n### 1. Reworded body\nThis area is described in free prose only.\n");
    expect(parse.areas[0].parsed).toBe(true);
    expect(areaHasFields(parse.areas[0])).toBe(false);
    expect(areaHasFields(parseCharter(CHARTER_TEXT).areas[0])).toBe(true);
  });

  it("reads labels written as list items on their own lines", () => {
    const parse = parseCharter(
      "## Areas\n### 1. Listed\n- Owns: the thing\n- Projects: P_ONE\n- Missing: one; two\n",
    );
    expect(parse.areas[0].owns).toBe("the thing");
    expect(parse.areas[0].projectNames).toEqual(["P_ONE"]);
    expect(parse.areas[0].missing).toEqual(["one", "two"]);
  });
});

describe("splitList", () => {
  it("handles empty, trailing period and nested commas", () => {
    expect(splitList("")).toEqual([]);
    expect(splitList("a, b (c, d), e.")).toEqual(["a", "b (c, d)", "e"]);
    expect(splitList("a, b; c")).toEqual(["a, b", "c"]);
  });
});

describe("resolveProjectRefs", () => {
  it("resolves an exact name or title, case-insensitively, and nothing else", () => {
    const refs = resolveProjectRefs(["P_ALPHA", "p_alpha", "P_BETA", "Unrelated"], PROJECTS);
    expect(refs).toEqual([
      { name: "P_ALPHA", resolvedId: "ent_90000001" },
      { name: "p_alpha", resolvedId: "ent_90000001" },
      { name: "P_BETA", resolvedId: null },
      // "Unrelated" is a prefix of a title, not a match: no fuzzy resolution.
      { name: "Unrelated", resolvedId: null },
    ]);
  });
});

describe("groupByAltitude", () => {
  it("puts keystone first, loses no record, and sorts the unlabeled last", () => {
    const groups = groupByAltitude(STRATEGIES);
    expect(groups.map((g) => g.altitude)).toEqual(["keystone", "domain_strategy", NO_ALTITUDE]);
    expect(groups.flatMap((g) => g.strategies)).toHaveLength(STRATEGIES.length);
    expect(groups[1].strategies.map((s) => s.title)).toEqual([
      "Domain strategy A",
      "Domain strategy B",
      "Example responsibility charter",
    ]);
  });

  it("keeps an altitude it has never heard of", () => {
    const groups = groupByAltitude([record({ id: "ent_1", title: "X", altitude: "brand_new" })]);
    expect(groups.map((g) => g.altitude)).toEqual(["brand_new"]);
  });
});

describe("mission helpers", () => {
  it("extracts the section headed THE MISSION, or null", () => {
    expect(missionStatement(MISSION_TEXT)).toBe(
      "Help people reclaim time by giving them a staff they can delegate to.",
    );
    expect(missionStatement("no headings here")).toBeNull();
    expect(missionStatement(null)).toBeNull();
  });

  it("flags more than one active mission and does not for one", () => {
    expect(hasDuplicateMissions([MISSION_A, MISSION_B])).toBe(true);
    expect(hasDuplicateMissions([MISSION_A])).toBe(false);
    expect(hasDuplicateMissions([])).toBe(false);
  });
});

describe("decisionsWaiting", () => {
  it("sums awaiting_approval and awaiting_input", () => {
    expect(decisionsWaiting({ statuses: { awaiting_approval: 3, awaiting_input: 4, pending: 99 } })).toEqual({
      kind: "exact",
      value: 7,
    });
  });

  it("is unmeasured, never a partial sum, when either status is unreadable", () => {
    expect(decisionsWaiting({ statuses: { awaiting_approval: 3, awaiting_input: null } }).kind).toBe("unmeasured");
    expect(decisionsWaiting({ statuses: { awaiting_approval: 3 } }).kind).toBe("unmeasured");
    expect(decisionsWaiting(null).kind).toBe("unmeasured");
  });

  it("reports a real zero as zero", () => {
    expect(decisionsWaiting({ statuses: { awaiting_approval: 0, awaiting_input: 0 } })).toEqual({
      kind: "exact",
      value: 0,
    });
  });
});
