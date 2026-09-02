/**
 * HASH ROUTING
 * ------------
 * A handful of sections and detail views do not justify a routing dependency.
 * The hash carries the whole route, so it survives a reload and a copied URL
 * without any dev-server rewrite rules:
 *
 *   #/                       THE CURRENT SESSION — the app root (see below)
 *   #/tasks                  the task view
 *   #/agents                 the agent directory
 *   #/agents/ent_<hex>       one agent's definition
 *   #/questions/ent_<hex>    one open question, in full
 *   #/sessions               the INDEX — every session, newest first
 *   #/sessions/ent_<hex>     one session's digest
 *   #/workflows              the declared workflow definitions (no detail route)
 *   #/lifecycle              the generic task state machine (no detail route)
 *   #/schemas                the entity-type registry and its drift (no detail route)
 *   #/entities/ent_<hex>     ANY entity, in full — the canonical address
 *
 * ROOT AND THE INDEX ARE DIFFERENT PLACES. This is the distinction the earlier
 * model collapsed, and collapsing it cost the app both:
 *
 *   - ROOT (`#/`) is the CURRENT session — what is happening right now. The
 *     operator lands here because the live session already links to everything
 *     he wants: its tasks, its open questions, what it produced, the plan it
 *     runs under.
 *   - `#/sessions` is a proper INDEX — every session, scannable and sortable,
 *     something you choose FROM. It is not a redirect to root and not a second
 *     rendering of the current session.
 *
 * Previously root and `#/sessions` resolved to the identical view and the list
 * was exiled to `#/sessions/all`. That made the index reachable only by knowing
 * a reserved word existed, and it meant the Sessions nav tab pointed at a
 * session rather than at sessions. `SESSIONS_ALL` is gone with it: an index
 * does not need a reserved-word escape hatch to reach itself.
 *
 * `home` is therefore its own `Section`, NOT `sessions` with a null id. Making
 * root a distinct section is what lets the nav highlight honestly — see
 * NAV_SECTIONS below and `sectionOfNav`.
 *
 * When the current session cannot be identified, root degrades to the most
 * recent session with the reason stated on screen — see `Sessions.tsx`. That is
 * strictly more useful than an error, and keeps root from ever being blank.
 *
 * Questions have no list route of their own: the sidebar IS the list, and it is
 * present on every route. `#/questions` with no id therefore falls back to the
 * task view rather than rendering an empty second copy of the sidebar.
 *
 * ENTITIES have a full page of their own at `#/entities/<id>`, for EVERY type.
 * That is the canonical address for an entity: it is linkable, survives a
 * reload, and can be pasted between sessions. The slide-over sheet shows the
 * same entity through the same component without a URL, for inspection that
 * keeps the operator's place; see `EntitySheet.tsx`.
 */
import { useEffect, useState } from "react";

export type Section =
  | "home"
  | "tasks"
  | "agents"
  | "sessions"
  | "workflows"
  | "lifecycle"
  | "schemas"
  | "questions"
  | "entities";

/**
 * The nav destinations, in nav order.
 *
 * `home` leads because it is the root, and it is a SEPARATE tab from
 * `sessions`: "Now" is the live session, "Sessions" is the index. A question or
 * entity detail is opened from context and is not a nav destination.
 */
export const NAV_SECTIONS = [
  "home",
  "tasks",
  "agents",
  "sessions",
  "workflows",
  "lifecycle",
  "schemas",
] as const;
export type NavSection = (typeof NAV_SECTIONS)[number];

export function isNavSection(section: Section): section is NavSection {
  return (NAV_SECTIONS as readonly string[]).includes(section);
}

export interface Route {
  section: Section;
  /** Entity id when an agent detail view is open. */
  agentId: string | null;
  /** Entity id when a question detail view is open. */
  questionId: string | null;
  /**
   * Which session is open: an `ent_` id for one specific session's digest, or
   * null for the index itself. Root is `section: "home"`, never a null id here.
   */
  sessionId: string | null;
  /** Entity id when the full entity page is open. */
  entityId: string | null;
}

/** The route with every detail slot empty — the base every branch starts from. */
const EMPTY: Omit<Route, "section"> = {
  agentId: null,
  questionId: null,
  sessionId: null,
  entityId: null,
};

/** Entity ids are `ent_` + hex. Anything else is a typo or a hand-edited hash. */
const ENTITY_ID = /^ent_[0-9a-f]+$/;

function entityId(segment: string | undefined): string | null {
  return segment && ENTITY_ID.test(segment) ? segment : null;
}

export function parseHash(hash: string): Route {
  const path = hash.replace(/^#\/?/, "").split("/").filter(Boolean);

  if (path[0] === "tasks") {
    return { ...EMPTY, section: "tasks" };
  }

  if (path[0] === "agents") {
    // Validate the id shape rather than trusting the URL: anything else is a
    // typo or a hand-edited hash, and should land on the directory.
    return { ...EMPTY, section: "agents", agentId: entityId(path[1]) };
  }

  if (path[0] === "sessions") {
    // Bare `#/sessions` => sessionId null => THE INDEX. A trailing segment must
    // be a valid entity id or it falls back to the index rather than rendering
    // a blank detail view for an id that cannot exist.
    return { ...EMPTY, section: "sessions", sessionId: entityId(path[1]) };
  }

  if (path[0] === "workflows") {
    // A flat list with no detail route: the gate tables are already the full
    // content of a definition, so there is nothing further to drill into.
    return { ...EMPTY, section: "workflows" };
  }

  if (path[0] === "lifecycle") {
    // A flat page like workflows. The eleven stages ARE the content; a single
    // status is a string value, not an entity with a detail page.
    return { ...EMPTY, section: "lifecycle" };
  }

  if (path[0] === "schemas") {
    // A flat list like workflows: the drift tables are the whole content of the
    // view, and a single entity type is not an entity with a detail page.
    return { ...EMPTY, section: "schemas" };
  }

  if (path[0] === "questions") {
    const id = entityId(path[1]);
    // No id means no question to show. There is no question LIST route — the
    // sidebar already is one — so fall back to tasks rather than a blank page.
    if (!id) return { ...EMPTY, section: "tasks" };
    return { ...EMPTY, section: "questions", questionId: id };
  }

  if (path[0] === "entities") {
    const id = entityId(path[1]);
    // A malformed or missing id has no entity to show. Fall back to root rather
    // than rendering a detail view with nothing in it.
    if (!id) return { ...EMPTY, section: "home" };
    return { ...EMPTY, section: "entities", entityId: id };
  }

  // ROOT (and any unrecognized hash) => the CURRENT session. See the header:
  // the session the operator is working from already links to the tasks,
  // questions, pages, and plan he would otherwise navigate for.
  return { ...EMPTY, section: "home" };
}

export function toHash(route: Route): string {
  if (route.section === "home") {
    return "#/";
  }
  if (route.section === "agents") {
    return route.agentId ? `#/agents/${route.agentId}` : "#/agents";
  }
  if (route.section === "sessions") {
    return route.sessionId ? `#/sessions/${route.sessionId}` : "#/sessions";
  }
  if (route.section === "workflows") {
    return "#/workflows";
  }
  if (route.section === "lifecycle") {
    return "#/lifecycle";
  }
  if (route.section === "schemas") {
    return "#/schemas";
  }
  if (route.section === "questions" && route.questionId) {
    return `#/questions/${route.questionId}`;
  }
  if (route.section === "entities" && route.entityId) {
    return `#/entities/${route.entityId}`;
  }
  return "#/tasks";
}

/** Current route, kept in sync with the address bar in both directions. */
export function useRoute(): [Route, (next: Route) => void] {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));

  useEffect(() => {
    const onChange = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  // Navigate by writing the hash; the listener above is what updates state, so
  // programmatic and manual navigation follow the identical path.
  //
  // The guard compares the RESOLVED routes rather than the literal strings.
  // Root ("" or "#/") and "#/sessions" are the same destination but not the
  // same text, so a string comparison would write the hash, fire no
  // `hashchange` for an unchanged view... or worse, suppress a legitimate
  // navigation. Comparing what each hash parses TO is what actually decides
  // whether anything would move.
  const navigate = (next: Route) => {
    const hash = toHash(next);
    const current = parseHash(window.location.hash);
    const target = parseHash(hash);
    const same =
      current.section === target.section &&
      current.agentId === target.agentId &&
      current.questionId === target.questionId &&
      current.sessionId === target.sessionId &&
      current.entityId === target.entityId;
    if (same) return;
    window.location.hash = hash;
  };

  return [route, navigate];
}
