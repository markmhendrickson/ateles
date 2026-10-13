/**
 * Render tests for the Console overview.
 *
 * The page is drawn from a model by `ConsoleView`, so these render it to static
 * markup from synthetic fixtures: no network, no browser, no Neotoma.
 */
import { describe, it, expect } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ConsoleView, type ConsoleModel } from "./Console";
import {
  CHARTER,
  CHARTER_REWORDED_7,
  CHARTER_TEXT,
  MISSION_A,
  MISSION_B,
  MISSION_TEXT,
  PROJECTS,
  STRATEGIES,
} from "./consoleFixtures";

const ready = <T,>(data: T) => ({ state: "ready" as const, data });

function model(over: Partial<ConsoleModel> = {}): ConsoleModel {
  return {
    charter: ready({ records: [CHARTER], total: 1 }),
    missions: ready({ records: [MISSION_A], total: 1 }),
    strategies: ready({ records: STRATEGIES, total: STRATEGIES.length }),
    retired: ready({ total: 4 }),
    // 5 active (STRATEGIES.length) + 4 retired + 2 in another status.
    allStrategies: ready({ total: STRATEGIES.length + 4 + 2 }),
    projects: ready({ records: PROJECTS, total: PROJECTS.length }),
    facets: ready({ statuses: { awaiting_approval: 2, awaiting_input: 3 } }),
    ...over,
  };
}

function render(m: ConsoleModel): string {
  return renderToStaticMarkup(
    <TooltipProvider>
      <ConsoleView model={m} onOpenEntity={() => {}} />
    </TooltipProvider>,
  );
}

const count = (html: string, needle: string) => html.split(needle).length - 1;

describe("ConsoleView with everything loaded", () => {
  const html = render(model());

  it("shows the mission statement and the single record", () => {
    expect(html).toContain("Help people reclaim time");
    expect(html).not.toContain("active mission records");
  });

  it("groups strategies by altitude, keystone first, and counts the retired ones", () => {
    expect(html.indexOf("keystone")).toBeGreaterThan(-1);
    expect(html.indexOf("keystone")).toBeLessThan(html.indexOf("domain strategy"));
    expect(html).toContain("Not shown: 4 retired or reference, 2 other.");
  });

  it("renders seven area cards from the charter", () => {
    expect(count(html, 'data-testid="area"')).toBe(7);
    expect(html).toContain("Product building");
    expect(html).toContain("Personal enrichment");
  });

  it("resolves a project that has a record and shows the others as unresolved", () => {
    expect(html).toContain("P_ALPHA");
    expect(html).toContain("P_BETA: no record");
    expect(html).toContain("the client projects: unnamed");
  });

  it("flags the area with no project", () => {
    expect(html).toContain("no project");
  });

  it("shows the global decisions-waiting count", () => {
    expect(html).toMatch(/data-testid="decisions-waiting">5</);
  });

  it("carries no info-icon explanations", () => {
    expect(html).not.toContain("More about this");
    expect(html).not.toContain("lucide-info");
  });

  it("shows no stale marker when every refresh worked", () => {
    expect(html).not.toContain('data-testid="stale"');
  });
});

describe("ConsoleView honesty", () => {
  it("shows both records of a duplicated mission and does not pick one", () => {
    const html = render(model({ missions: ready({ records: [MISSION_A, MISSION_B], total: 2 }) }));
    expect(html).toContain("2 active mission records.");
    expect(html).toContain("all 2");
    expect(html).not.toContain("both");
    expect(html).toContain(MISSION_A.id);
    expect(html).toContain(MISSION_B.id);
  });

  it("marks a reworded area heading unparsed instead of crashing or dropping it", () => {
    const reworded = { ...CHARTER, content: CHARTER_REWORDED_7 };
    const html = render(model({ charter: ready({ records: [reworded], total: 1 }) }));
    expect(count(html, 'data-testid="area"')).toBe(6);
    expect(count(html, 'data-testid="area-unparsed"')).toBe(1);
    expect(html).toContain("Seven - Personal enrichment");
    expect(count(html, 'data-testid="area-missing"')).toBe(1);
    expect(html).toContain("Area 7 of 7 was not found under a recognised heading.");
  });

  it("shows an area whose body lost every label as unparsed, not as a card of 'not listed'", () => {
    const body = CHARTER_TEXT.replace(/### 5\.[^#]*/, "### 5. Substrate stewardship\nDescribed in free prose now.\n\n");
    const html = render(model({ charter: ready({ records: [{ ...CHARTER, content: body }], total: 1 }) }));
    expect(count(html, 'data-testid="area"')).toBe(6);
    expect(count(html, 'data-testid="area-unparsed"')).toBe(1);
    expect(html).toContain("Described in free prose now.");
  });

  it("calls an absent line 'not listed', which is a statement about the charter, not a parse failure", () => {
    const html = render(model());
    expect(html).toContain("not listed");
    // Only area-level failure shows the 'unparsed' badge; the intact charter has none.
    expect(html).not.toContain("unparsed");
  });

  it("says a failed read failed, rather than printing an empty section", () => {
    const html = render(
      model({
        missions: { state: "error", error: "The read failed." },
        charter: { state: "error", error: "The read failed." },
        facets: { state: "error", error: "The read failed." },
      }),
    );
    expect(html).toContain("The mission could not be read.");
    expect(html).toContain("The responsibility charter could not be read.");
    expect(html).not.toContain("No active mission record");
    expect(html).toContain("not measured");
  });

  it("leaves the other sections rendering when two fail", () => {
    const html = render(
      model({
        missions: { state: "error", error: "The read failed." },
        charter: { state: "error", error: "The read failed." },
      }),
    );
    // Strategies still draw: groups, a record in each, and the footer.
    expect(html).toContain("Keystone strategy");
    expect(html).toContain("Domain strategy A");
    expect(html).toContain("Not shown: 4 retired or reference, 2 other.");
    // The decisions figure still reads.
    expect(html).toMatch(/data-testid="decisions-waiting">5</);
    // And a failure in strategies leaves the mission and areas standing.
    const other = render(model({ strategies: { state: "error", error: "The read failed." } }));
    expect(other).toContain("The strategies could not be read.");
    expect(other).toContain("Help people reclaim time");
    expect(count(other, 'data-testid="area"')).toBe(7);
    expect(other).toContain("P_ALPHA");
  });

  it("says the data is old, and from when, when a refresh failed on data it still shows", () => {
    const stale = <T,>(data: T) => ({
      state: "ready" as const,
      data,
      fetchedAt: "2026-10-13T09:05:00.000Z",
      refreshFailed: true,
    });
    const html = render(
      model({
        missions: stale({ records: [MISSION_A], total: 1 }),
        facets: stale({ statuses: { awaiting_approval: 2, awaiting_input: 3 } }),
      }),
    );
    expect(count(html, 'data-testid="stale"')).toBe(2);
    expect(html).toMatch(/as of \d\d:\d\d/i);
    expect(html).toContain("The latest refresh failed.");
    // The old data is still drawn, not replaced by an error.
    expect(html).toContain("Help people reclaim time");
  });

  it("says 'an unknown time' rather than inventing one when the age is unknown", () => {
    const html = render(
      model({ missions: { state: "ready", data: { records: [MISSION_A], total: 1 }, refreshFailed: true } }),
    );
    expect(html).toContain("as of an unknown time");
  });

  it("names the shortfall when strategies in other statuses cannot be counted", () => {
    const wrong = render(model({ allStrategies: ready({ total: 1 }) }));
    expect(wrong).toContain("other count could not be worked out");
    const failed = render(model({ allStrategies: { state: "error", error: "The read failed." } }));
    expect(failed).toContain("other count could not be read");
    expect(failed).not.toContain("0 other");
  });

  it("shows a mission with no THE MISSION heading by its first paragraph, with the full text reachable", () => {
    const reworded = { ...MISSION_A, content: "# MISSION (example)\n\n## THE PURPOSE\nReclaim time for people.\n\nSecond paragraph." };
    const html = render(model({ missions: ready({ records: [reworded], total: 1 }) }));
    expect(html).toContain("Reclaim time for people.");
    expect(html).toContain("Opening of the record text");
    expect(html).toContain("Full record text");
    expect(html).toContain("Second paragraph.");
    expect(html).not.toContain("no readable statement");
    // With the heading present, the opening label is not shown.
    expect(render(model())).not.toContain("Opening of the record text");
    expect(MISSION_TEXT).toContain("THE MISSION");
  });

  it("shows placeholders while sections load and no false 'none' text", () => {
    const loading = { state: "loading" as const };
    const html = render(
      model({
        charter: ready({ records: [CHARTER], total: 1 }),
        missions: loading,
        strategies: loading,
        retired: loading,
        allStrategies: loading,
        projects: loading,
        facets: loading,
      }),
    );
    expect(count(html, 'data-testid="section-loading"')).toBe(2);
    expect(html).not.toContain("No active");
    expect(html).not.toContain("0 retired");
    expect(html).not.toContain("not measured");
    expect(html).not.toContain("not looked up");
    expect(html).toMatch(/data-testid="decisions-waiting">loading</);
    expect(html).toContain("retired count loading");
    expect(html).toContain("other count loading");
    expect(html).toContain("P_ALPHA: loading");
  });

  it("states an absent charter instead of showing seven empty areas", () => {
    const html = render(model({ charter: ready({ records: [], total: 0 }) }));
    expect(html).toContain("No active responsibility charter was found");
    expect(count(html, 'data-testid="area"')).toBe(0);
  });
});

describe("project chips", () => {
  it("say 'no record' only when every project record was read", () => {
    const html = render(model());
    expect(html).toContain("P_BETA: no record");
    expect(html).not.toContain("not checked");
  });

  it("say 'not checked', not 'no record', when more project records exist than were read", () => {
    const html = render(model({ projects: ready({ records: PROJECTS, total: PROJECTS.length + 40 }) }));
    expect(html).toContain("P_BETA: not checked");
    expect(html).not.toContain("no record");
    // A name that did resolve still links.
    expect(html).toContain("P_ALPHA");
    expect(html).toContain("2 of 42 project records were read");
  });

  it("say 'not looked up' when the project read failed", () => {
    const html = render(model({ projects: { state: "error", error: "The read failed." } }));
    expect(html).toContain("P_BETA: not looked up");
    expect(html).not.toContain("no record");
  });
});
