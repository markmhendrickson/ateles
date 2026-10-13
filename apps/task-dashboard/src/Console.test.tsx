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
    projects: ready({ records: PROJECTS, total: PROJECTS.length }),
    facets: { statuses: { awaiting_approval: 2, awaiting_input: 3 } },
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
    expect(html).toContain("4 retired or reference strategies not shown.");
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
});

describe("ConsoleView honesty", () => {
  it("shows both records of a duplicated mission and does not pick one", () => {
    const html = render(model({ missions: ready({ records: [MISSION_A, MISSION_B], total: 2 }) }));
    expect(html).toContain("2 active mission records.");
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
        missions: { state: "error", error: "Neotoma returned HTTP 502" },
        charter: { state: "error", error: "timeout" },
        facets: null,
      }),
    );
    expect(html).toContain("The mission could not be read.");
    expect(html).toContain("The responsibility charter could not be read.");
    expect(html).not.toContain("No active mission record");
    expect(html).toContain("not measured");
  });

  it("shows placeholders while sections load and no false 'none' text", () => {
    const loading = { state: "loading" as const };
    const html = render(
      model({ charter: loading, missions: loading, strategies: loading, retired: loading, projects: loading, facets: null }),
    );
    expect(count(html, 'data-testid="section-loading"')).toBe(3);
    expect(html).not.toContain("No active");
    expect(html).not.toContain("0 retired");
  });

  it("states an absent charter instead of showing seven empty areas", () => {
    const html = render(model({ charter: ready({ records: [], total: 0 }) }));
    expect(html).toContain("No active responsibility charter was found");
    expect(count(html, 'data-testid="area"')).toBe(0);
  });
});
