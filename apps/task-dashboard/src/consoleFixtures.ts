/**
 * SYNTHETIC FIXTURES for the Console tests.
 *
 * Every name, project and sentence here is invented. The shapes mirror the real
 * records (a charter whose areas are `### N. <Area>` headings with `Owns:`,
 * `Projects:`, `Strategies:`, `Workflows:` and `Missing:` labels), but no value
 * is copied from a live record.
 */
import type { SpineRecord } from "../server/consoleSpine";

export function record(over: Partial<SpineRecord> & { id: string }): SpineRecord {
  return {
    name: null,
    title: null,
    status: "active",
    altitude: null,
    domain: null,
    role: null,
    description: null,
    content: null,
    ...over,
  };
}

/** A well-formed charter: cross-cutting rules, then seven areas. */
export const CHARTER_TEXT = `# Example responsibility charter

Intro prose that mentions Projects: nothing in particular.

## Cross-cutting rules
1. **Measure.** Every item states a metric first.
2. **Reporting.** One brief a day.

## Areas

### 1. Product building
Owns: turning signals into a ranked roadmap, then building and releasing it. Experiments: copy tests run weekly. Projects: P_ALPHA, P_BETA. Strategies: Adoption and revenue; two-stage test. Workflows: merge path (feature, bug, release), intake, research and analysis. Missing: weekly roadmap proposal; outcome scoring.

### 2. Content and distribution
Owns: turning shipped work into drafts. Pattern from the personal strategy: protect, then feed. Projects: P_CONTENT. Strategy: content and distribution. Workflows: none specified yet. Missing: content production, outcome scoring.

### 3. Money operations
Owns: paying obligations and reconciling accounts. Projects: P_MONEY, and the client projects. Workflows: payment. Missing: reconciliation and runway reporting, tax preparation, invoicing.

### 4. Delivery
Owns: partner delivery and follow-up. Projects: P_GTM and operations. Strategies: Adoption and revenue. Workflows: outreach, meeting processing.

### 5. Substrate stewardship
Owns: security, infrastructure and deploys. Projects: P_ALPHA. Strategy: Substrate stewardship. Workflows: merge path (release), resume.

### 6. Life operations
Owns: household logistics and scheduling. Projects: P_HOME, P_FAMILY. Workflows: calendar hold, payment. Missing: household purchasing.

### 7. Personal enrichment
Owns: a weekly curated feed matched to interests. Strategy: personal-life verticals. Project: none yet. Workflows: none yet. Missing: project, curated-feed workflow.
`;

/** The same charter with area 7's heading reworded past recognition. */
export const CHARTER_REWORDED_7 = CHARTER_TEXT.replace(
  "### 7. Personal enrichment",
  "### Seven - Personal enrichment",
);

export const MISSION_TEXT = `# MISSION (example)

## THE MISSION
Help people reclaim time by giving them a staff they can delegate to.

## BOUNDARY
Out of scope here.
`;

export const CHARTER = record({
  id: "ent_c0ffee01",
  title: "Example responsibility charter",
  altitude: "domain_strategy",
  domain: "swarm",
  role: "responsibility_charter",
  content: CHARTER_TEXT,
});

export const MISSION_A = record({
  id: "ent_a1a1a1a1",
  title: "Mission (example)",
  altitude: "mission",
  role: "mission",
  content: MISSION_TEXT,
});

export const MISSION_B = record({
  id: "ent_b2b2b2b2",
  title: "Mission (example)",
  altitude: "mission",
  role: "mission",
  content: MISSION_TEXT,
});

export const STRATEGIES: SpineRecord[] = [
  record({ id: "ent_00000001", title: "Domain strategy B", altitude: "domain_strategy", domain: "product" }),
  record({ id: "ent_00000002", title: "Keystone strategy", altitude: "keystone", domain: "all" }),
  record({ id: "ent_00000003", title: "Domain strategy A", altitude: "domain_strategy", domain: "gtm" }),
  record({ id: "ent_00000004", title: "Standards without altitude" }),
  CHARTER,
];

export const PROJECTS: SpineRecord[] = [
  record({ id: "ent_90000001", name: "P_ALPHA", title: null }),
  record({ id: "ent_90000002", name: "Unrelated project", title: "Unrelated project" }),
];
