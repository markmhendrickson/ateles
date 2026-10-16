/**
 * CONSOLE DATA — pure logic behind the Console overview.
 *
 * Kept free of React so the parts that can silently go wrong are testable: the
 * charter parse, the strategy grouping, the mission-duplicate check and the
 * project-name resolution.
 *
 * THE CHARTER IS PROSE (operator choice A, 2026-10-13). The seven areas exist
 * today only as `### N. <Area>` headings inside one strategy record's
 * `content`, so this module parses text. The parser is built to FAIL VISIBLY,
 * never to throw and never to drop an area quietly:
 *
 *   - a heading that does not match the `### N. <Area>` form is kept as an
 *     "unparsed" area carrying its raw text, rather than being skipped;
 *   - a number from 1 to 7 with no heading at all is reported as missing;
 *   - a label the parser could not find inside an area (`Owns:`, `Projects:`,
 *     ...) is `null`, which the view prints as "unparsed", never as "none".
 *
 * "none" and "unparsed" are different facts. A charter that says `Project: none
 * yet` is a statement; a charter whose project line was reworded past
 * recognition is an instrument failure, and the view must not report it as the
 * first.
 *
 * A structured `areas` field replaces this later, through the population
 * manifest (console_design.md, open choice 1).
 */
import type { SpineRecord } from "../server/consoleSpine";
import type { FacetsResponse } from "./facets";
import type { Count } from "./taskCount";

/** The charter lists seven areas. A different count is surfaced, not hidden. */
export const EXPECTED_AREA_COUNT = 7;

/** A project name as the charter writes it, and what (if anything) it resolved to. */
export interface ProjectRef {
  name: string;
  /** Entity id of the matching project record, or null when none matches. */
  resolvedId: string | null;
}

export interface ParsedArea {
  /** The number in the heading, or null for an unparsed heading. */
  number: number | null;
  name: string;
  /** False when the heading did not match the expected form. */
  parsed: boolean;
  /** The "Owns" sentence, or null when it could not be found. */
  owns: string | null;
  /** Project names written as `P_NAME`. */
  projectNames: string[];
  /** Project text that is not a `P_` name ("the client projects"). */
  projectOther: string[];
  /** `null` when the Project(s) label was not found; true for "none yet". */
  projectsNone: boolean | null;
  strategies: string[] | null;
  workflows: string[] | null;
  /** True when the Workflows label says "none". */
  workflowsNone: boolean | null;
  /** Items after `Missing:`; empty when the area lists none, null when unlabeled. */
  missing: string[] | null;
  /** The area's raw text, kept for areas whose heading did not parse. */
  raw: string;
}

/** Did the parser recognise at least one label in this area's text? */
export function areaHasFields(a: ParsedArea): boolean {
  return (
    a.owns !== null ||
    a.projectsNone !== null ||
    a.strategies !== null ||
    a.workflows !== null ||
    a.missing !== null
  );
}

export interface CharterParse {
  areas: ParsedArea[];
  /** Area numbers 1..7 with no heading in the text. */
  missingNumbers: number[];
  /** Whole-charter problems, in words: no content, no Areas section, wrong count. */
  problems: string[];
}

/** The exact heading the charter uses. */
const AREA_HEADING = /^###\s+(\d+)\.\s+(.+?)\s*$/;
/** Any heading at all, to find reworded ones. */
const ANY_HEADING = /^(#{1,6})\s+(.*?)\s*$/;

const LABELS = ["Owns", "Projects", "Project", "Strategies", "Strategy", "Workflows", "Workflow", "Missing"] as const;
type Label = (typeof LABELS)[number];

/**
 * Where each label starts. A label counts only at the start of the body or after
 * a sentence end, so prose such as "the personal-life strategy: protect" (lower
 * case, mid-sentence) is not mistaken for a `Strategy:` label.
 */
function findLabels(body: string): { label: Label; start: number; valueStart: number }[] {
  const found: { label: Label; start: number; valueStart: number }[] = [];
  const re = new RegExp(`(^\\s*(?:[-*\u2022]\\s+)?|[.!?]\\s+|\\n\\s*(?:[-*\u2022]\\s+)?)(${LABELS.join("|")}):\\s*`, "g");
  let m: RegExpExecArray | null;
  while ((m = re.exec(body))) {
    const start = m.index + m[1].length;
    found.push({ label: m[2] as Label, start, valueStart: m.index + m[0].length });
  }
  return found;
}

/** Split a list on `;` when present, else on top-level commas (not those inside parentheses). */
export function splitList(value: string): string[] {
  const trimmed = value.replace(/\.\s*$/, "").trim();
  if (!trimmed) return [];
  if (trimmed.includes(";")) {
    return trimmed.split(";").map((s) => s.trim()).filter(Boolean);
  }
  const out: string[] = [];
  let depth = 0;
  let current = "";
  for (const ch of trimmed) {
    if (ch === "(") depth++;
    if (ch === ")") depth = Math.max(0, depth - 1);
    if (ch === "," && depth === 0) {
      out.push(current.trim());
      current = "";
    } else {
      current += ch;
    }
  }
  out.push(current.trim());
  return out.filter(Boolean);
}

/** True for "none", "none yet", "none specified yet" and the like. */
function isNone(value: string): boolean {
  return /^none\b/i.test(value.replace(/\.\s*$/, "").trim());
}

function emptyArea(name: string, number: number | null, parsed: boolean, raw: string): ParsedArea {
  return {
    number,
    name,
    parsed,
    owns: null,
    projectNames: [],
    projectOther: [],
    projectsNone: null,
    strategies: null,
    workflows: null,
    workflowsNone: null,
    missing: null,
    raw,
  };
}

function parseAreaBody(area: ParsedArea, body: string): ParsedArea {
  const labels = findLabels(body);
  const out = { ...area };
  labels.forEach((l, i) => {
    const end = i + 1 < labels.length ? labels[i + 1].start : body.length;
    const value = body.slice(l.valueStart, end).replace(/\s+/g, " ").trim().replace(/\s*[-*\u2022]$/, "");
    switch (l.label) {
      case "Owns":
        out.owns = value.replace(/\s+/g, " ") || null;
        break;
      case "Projects":
      case "Project": {
        if (isNone(value)) {
          out.projectsNone = true;
          break;
        }
        out.projectsNone = false;
        const names = value.match(/\bP_[A-Z0-9_]+\b/g) ?? [];
        out.projectNames = [...new Set(names)];
        // What is left once the P_ names and connectors are removed is text the
        // charter wrote about projects it did not name. Keep it, unresolved.
        const rest = value
          .replace(/\bP_[A-Z0-9_]+\b/g, "|")
          .split("|")
          .map((s) => s.replace(/^[\s,]*(and\b)?[\s,]*/i, "").replace(/[\s,.]+$/, "").trim())
          .filter(Boolean);
        out.projectOther = rest;
        break;
      }
      case "Strategies":
      case "Strategy":
        out.strategies = isNone(value) ? [] : splitList(value);
        break;
      case "Workflows":
      case "Workflow":
        out.workflowsNone = isNone(value);
        out.workflows = isNone(value) ? [] : splitList(value);
        break;
      case "Missing":
        out.missing = splitList(value);
        break;
    }
  });
  return out;
}

/**
 * Parse the charter prose into areas. Total: it returns a result for ANY input,
 * including null, an empty string or a charter with no Areas section.
 */
export function parseCharter(content: string | null | undefined): CharterParse {
  const problems: string[] = [];
  const everyNumber = Array.from({ length: EXPECTED_AREA_COUNT }, (_, i) => i + 1);

  if (typeof content !== "string" || !content.trim()) {
    return {
      areas: [],
      missingNumbers: everyNumber,
      problems: ["The charter record has no readable content."],
    };
  }

  const lines = content.split(/\r?\n/);

  // Only headings after an "Areas" heading are areas; the cross-cutting rules
  // above it carry their own numbering that must not be read as areas.
  let areasFrom = lines.findIndex((l) => {
    const m = ANY_HEADING.exec(l);
    return m !== null && m[1].length <= 3 && /^areas\b/i.test(m[2]);
  });
  if (areasFrom < 0) {
    problems.push('No "Areas" heading was found; reading every heading in the charter.');
    areasFrom = -1;
  }

  type Section = { heading: string; level: number; lines: string[] };
  const sections: Section[] = [];
  for (const line of lines.slice(areasFrom + 1)) {
    const h = ANY_HEADING.exec(line);
    // A heading shallower than the area level that is not an area (a new "##"
    // section after the areas) ends the area list.
    if (h && h[1].length <= 2 && sections.length > 0) break;
    if (h && h[1].length >= 3) {
      sections.push({ heading: line.trim(), level: h[1].length, lines: [] });
    } else if (sections.length > 0) {
      sections[sections.length - 1].lines.push(line);
    }
  }

  const areas: ParsedArea[] = sections.map((s) => {
    const raw = s.lines.join("\n").trim();
    const m = AREA_HEADING.exec(s.heading);
    if (!m) {
      // Reworded heading: keep it, labelled unparsed, with its text.
      const name = s.heading.replace(/^#+\s*/, "");
      return emptyArea(name, null, false, raw);
    }
    const base = emptyArea(m[2], Number(m[1]), true, raw);
    return parseAreaBody(base, s.lines.join("\n").trim());
  });

  const seen = new Set(areas.filter((a) => a.number !== null).map((a) => a.number as number));
  const missingNumbers = everyNumber.filter((n) => !seen.has(n));

  if (areas.length === 0) problems.push("No area headings were found in the charter.");
  else if (areas.length !== EXPECTED_AREA_COUNT) {
    problems.push(
      `Expected ${EXPECTED_AREA_COUNT} areas; the charter text yields ${areas.length}.`,
    );
  }

  return { areas, missingNumbers, problems };
}

/** Normalise a name for exact comparison: case and surrounding whitespace only. */
function norm(s: string): string {
  return s.trim().toLowerCase();
}

/**
 * Resolve a charter project name against project records by EXACT title or
 * canonical name (case-insensitive). No fuzzy matching: the charter's `P_`
 * names match no record today, and a guessed match would attach an area to the
 * wrong project. Unmatched stays unresolved.
 */
export function resolveProjectRefs(names: string[], projects: SpineRecord[]): ProjectRef[] {
  return names.map((name) => {
    const hit = projects.find(
      (p) => (p.title !== null && norm(p.title) === norm(name)) || (p.name !== null && norm(p.name) === norm(name)),
    );
    return { name, resolvedId: hit?.id ?? null };
  });
}

/** Altitude order, keystone first. Unlisted altitudes follow alphabetically. */
export const ALTITUDE_ORDER = ["keystone", "synthesis", "domain_strategy", "vertical", "tenet"];
export const NO_ALTITUDE = "(no altitude recorded)";

export interface AltitudeGroup {
  altitude: string;
  strategies: SpineRecord[];
}

/** Group strategies by their `altitude` field, keystone first. Never drops a record. */
export function groupByAltitude(strategies: SpineRecord[]): AltitudeGroup[] {
  const groups = new Map<string, SpineRecord[]>();
  for (const s of strategies) {
    const key = s.altitude ?? NO_ALTITUDE;
    groups.set(key, [...(groups.get(key) ?? []), s]);
  }
  const rank = (a: string) => {
    if (a === NO_ALTITUDE) return Number.MAX_SAFE_INTEGER;
    const i = ALTITUDE_ORDER.indexOf(a);
    return i >= 0 ? i : ALTITUDE_ORDER.length;
  };
  return [...groups.entries()]
    .sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b))
    .map(([altitude, list]) => ({
      altitude,
      strategies: [...list].sort((x, y) => (x.title ?? x.name ?? "").localeCompare(y.title ?? y.name ?? "")),
    }));
}

/**
 * The part of a mission's prose headed THE MISSION, or null when no such
 * heading exists (the view then falls back to `missionOpening`).
 */
export function missionStatement(content: string | null): string | null {
  if (!content) return null;
  const lines = content.split(/\r?\n/);
  const start = lines.findIndex((l) => /^#{1,6}\s+THE MISSION\b/i.test(l));
  if (start < 0) return null;
  const rest = lines.slice(start + 1);
  const end = rest.findIndex((l) => /^#{1,6}\s/.test(l));
  const body = (end < 0 ? rest : rest.slice(0, end)).join("\n").trim();
  return body || null;
}

/**
 * The first paragraph of a mission's prose that is not only headings, or null
 * when there is none. Used when no THE MISSION heading exists, so a reworded
 * record still shows its own words rather than "no readable statement".
 */
export function missionOpening(content: string | null): string | null {
  if (!content) return null;
  const blocks = content.split(/\r?\n\s*\r?\n/).map((b) => b.trim()).filter(Boolean);
  for (const block of blocks) {
    const lines = block.split(/\r?\n/).filter((l) => l.trim());
    const body = lines.filter((l) => !/^#{1,6}\s/.test(l)).join("\n").trim();
    if (body) return body;
  }
  return null;
}

/**
 * Do the active mission records disagree about being the mission? More than one
 * active record is a data defect the view shows in full; it does not choose.
 */
export function hasDuplicateMissions(missions: SpineRecord[]): boolean {
  return missions.length > 1;
}

/**
 * One section of the page as the browser holds it: never conflates "not yet" with "none".
 *
 * A `ready` section whose latest refresh failed keeps its last good data and is
 * marked `refreshFailed`, with `fetchedAt` saying how old that data is, so the
 * page can say so rather than pass old data off as current.
 */
export type Loadable<T> =
  | { state: "loading" }
  | { state: "error"; error: string }
  | { state: "ready"; data: T; fetchedAt?: string; refreshFailed?: boolean };

/**
 * The only failure texts the page shows for a section or the figure. Whatever
 * the server or the browser says is mapped onto one of these; the detail stays
 * in the server log.
 */
export const SECTION_FAILURE = {
  /** The request itself failed: no answer from the dashboard server. */
  unreachable: "The dashboard server did not answer.",
  /** An answer came back that is not JSON (for example a proxy's error page). */
  unreadable: "The dashboard server sent a reply the page could not read.",
  /** The server answered and said the read failed. */
  failed: "The read failed.",
} as const;

/** What a fetch produced, reduced to what the loader needs. `body` is undefined when it was not JSON. */
export interface FetchOutcome {
  status: number;
  ok: boolean;
  body: unknown;
}

/**
 * Turn one fetch outcome into the next state of a section. Pure, so the rules
 * that keep a failure from reading as data are testable without a browser:
 *
 *   - `outcome` null = the request itself failed (network);
 *   - a non-2xx status, a body that is not JSON, `ok: false` or an `error` key
 *     is a failure whatever else the body holds;
 *   - a failure never replaces good data: it keeps it, marked `refreshFailed`;
 *   - with no good data to keep, the failure becomes the section's error state.
 */
export function interpretSectionResponse<T>(
  outcome: FetchOutcome | null,
  prev: Loadable<T>,
  now: () => string = () => new Date().toISOString(),
): Loadable<T> {
  const fail = (message: string): Loadable<T> =>
    prev.state === "ready" ? { ...prev, refreshFailed: true } : { state: "error", error: message };

  if (outcome === null) return fail(SECTION_FAILURE.unreachable);
  const body = outcome.body;
  if (typeof body !== "object" || body === null) return fail(SECTION_FAILURE.unreadable);
  const b = body as Record<string, unknown>;
  if (!outcome.ok || b.ok === false || "error" in b) return fail(SECTION_FAILURE.failed);
  return {
    state: "ready",
    data: body as T,
    fetchedAt: typeof b.fetchedAt === "string" ? b.fetchedAt : now(),
  };
}

/** "14:05" in the viewer's own time, or null when the timestamp is not a date. */
export function formatAsOf(fetchedAt: string | undefined): string | null {
  if (!fetchedAt) return null;
  const d = new Date(fetchedAt);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

/**
 * The oldest data among the sections that are showing old data after a failed
 * refresh, as `{ asOf }` (asOf null = age unknown), or null when none is stale.
 */
export function staleness(sources: Loadable<unknown>[]): { asOf: string | null } | null {
  const stale = sources.filter(
    (s): s is Extract<Loadable<unknown>, { state: "ready" }> => s.state === "ready" && s.refreshFailed === true,
  );
  if (stale.length === 0) return null;
  const dated = stale
    .map((s) => s.fetchedAt)
    .filter((f): f is string => typeof f === "string" && !Number.isNaN(new Date(f).getTime()))
    .sort();
  // Any stale source with an unknown age makes the age unknown.
  if (dated.length < stale.length) return { asOf: null };
  return { asOf: formatAsOf(dated[0]) };
}

/** What is known about the project records the charter's names are matched against. */
export type ProjectLookup =
  /** Still reading. Say "loading", not that the names were not found. */
  | "loading"
  /** The read failed: the names were not looked up. */
  | "failed"
  /** Fewer records were read than exist (or the total is unknown): a missing name proves nothing. */
  | "partial"
  /** Every project record was read: a name with no match has no record. */
  | "complete";

export function projectLookup(projects: Loadable<{ records: unknown[]; total: number | null }>): ProjectLookup {
  if (projects.state === "loading") return "loading";
  if (projects.state === "error") return "failed";
  const { records, total } = projects.data;
  return total !== null && total <= records.length ? "complete" : "partial";
}

/**
 * Strategies in a status other than active or retired: all of them, minus the
 * active ones, minus the retired ones. Null when any of the three is unknown or
 * the arithmetic goes negative (the three counts disagree), because a guessed
 * figure would read as a measurement.
 */
export function otherStrategyCount(
  all: number | null,
  active: number | null,
  retired: number | null,
): number | null {
  if (all === null || active === null || retired === null) return null;
  const other = all - active - retired;
  return other >= 0 ? other : null;
}

/**
 * Tasks waiting on the operator: `awaiting_approval` plus `awaiting_input`,
 * from the existing `/api/facets` per-status counts across every open task.
 *
 * Both statuses must be measured. One unreadable status makes the figure
 * `unmeasured` rather than the sum of the one that answered, which would read
 * as fewer decisions waiting than there are.
 */
export function decisionsWaiting(body: FacetsResponse | null): Count {
  const statuses = body?.statuses;
  if (!statuses || typeof statuses !== "object") return { kind: "unmeasured" };
  const parts = [statuses.awaiting_approval, statuses.awaiting_input];
  if (!parts.every((v) => typeof v === "number" && Number.isFinite(v))) {
    return { kind: "unmeasured" };
  }
  return { kind: "exact", value: (parts as number[]).reduce((a, b) => a + b, 0) };
}
