/**
 * WHAT A QUESTION IS ABOUT
 * ------------------------
 * A question is raised BECAUSE of something — a task, an issue, a PR. The
 * detail view should say what that something is and link to it, instead of
 * leaving the operator to read a paragraph and work it out.
 *
 * WHAT THE DATA ACTUALLY CARRIES (checked, not assumed)
 * ----------------------------------------------------
 * Every one of the eight live questions was read through `/entities/<id>/
 * relationships` and again through `list_relationships`, both agreeing:
 *
 *   - PART_OF   -> the plan (`ent_99ace…`, Ateles Agent Swarm Architecture)
 *   - REFERS_TO <- the conversation that raised the question, and/or the
 *                  session digest
 *   - three questions carry NO edges at all
 *
 * There is NOT ONE edge from a question to the task or issue it is about. The
 * subject linkage was never written as a relationship; it exists only as prose
 * ("Merge PR #558", "blocks ateles#552"). So this module renders TWO distinct
 * things and never blurs them:
 *
 *   1. RELATED ENTITIES — real stored edges, from `/api/entity`. Clickable
 *      through to `#/entities/<id>`, because the entity provably exists.
 *
 *   2. REFERENCES IN THE TEXT — issue and PR numbers scraped from the stored
 *      prose. Labelled as coming from the text, and linked to GitHub rather
 *      than to an in-app entity route: a `#558` in a sentence is evidence that
 *      the author typed that number, and nothing more. Minting an in-app link
 *      from it would assert an entity that may not exist, which is the exact
 *      invention this app must not commit.
 *
 * The distinction is the whole point. Presenting a scraped number as a stored
 * relationship would make the graph look richer than it is, and the gap is
 * precisely what needs fixing at the write boundary — see the module note in
 * QuestionDetail.tsx.
 */
import { useEffect, useState } from "react";
import type { EntityEdge, EntityPayload } from "./entity";

/**
 * Edges that are BOOKKEEPING rather than subject matter.
 *
 * Every question is PART_OF the same plan and REFERS_TO the conversation that
 * raised it. Both are true and neither tells the operator anything he does not
 * already know — he is reading this inside that session, under that plan. They
 * are kept, but demoted below the edges that actually vary.
 */
const AMBIENT_TYPES = new Set(["conversation", "conversation_message", "session_digest"]);

export interface QuestionLink {
  id: string;
  /** `task`, `plan`, … as stored. Null while the proxy hydrates a neighbour. */
  entityType: string | null;
  /** null until the proxy has hydrated this neighbour's name. */
  label: string | null;
  relationship: string;
  direction: "outgoing" | "incoming";
  /** True for the plan/conversation edges every question carries alike. */
  ambient: boolean;
}

/** A GitHub issue or PR named in the stored prose. Never an entity link. */
export interface TextReference {
  /** `ateles#558` — always fully qualified, so the repo is never guessed at. */
  label: string;
  url: string;
}

/**
 * The repo an unqualified `#558` belongs to.
 *
 * The questions are filed by agents working this repo and every bare number
 * checked resolves here, but that is an inference rather than something the
 * text states. It is why a bare number is rendered fully qualified — the
 * operator sees which repo was assumed, instead of following a silent guess.
 */
const DEFAULT_REPO = "ateles";

/**
 * `ateles#558` or a bare `#558`.
 *
 * The repo prefix is the optional part; `#` is NOT a word character, so a `\b`
 * in front of it fails after a space and silently drops every bare number —
 * which is most of them.
 */
const ISSUE_RE = /(?:\b(ateles|neotoma|openclaw))?#(\d{2,6})\b/g;

/**
 * Issue and PR numbers named anywhere in a question's stored text.
 *
 * Deduplicated by qualified name: a question that argues about #558 four times
 * has one subject, not four. Order follows first appearance, which is the
 * order the author introduced them.
 */
export function textReferences(parts: (string | null)[]): TextReference[] {
  const seen = new Set<string>();
  const out: TextReference[] = [];

  for (const part of parts) {
    if (!part) continue;
    ISSUE_RE.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = ISSUE_RE.exec(part))) {
      const repo = m[1] ?? DEFAULT_REPO;
      const label = `${repo}#${m[2]}`;
      if (seen.has(label)) continue;
      seen.add(label);
      // `/issues/<n>` redirects to the PR when the number is a PR, so one form
      // covers both without claiming to know which it is.
      out.push({
        label,
        url: `https://github.com/markmhendrickson/${repo}/issues/${m[2]}`,
      });
    }
  }
  return out;
}

/**
 * Some neighbours come back with a null `canonical_name` — the proxy hydrates
 * them in the background and the first response can arrive before that lands.
 * The id is a poor label but it is the truth; `null` here means "not known
 * yet", and the UI says that rather than printing a bare hex string.
 */
function edgeLabel(e: EntityEdge): string | null {
  const name = e.canonical_name?.trim();
  if (!name) return null;
  // `canonical_name` arrives type-prefixed on several types ("plan:Ateles …").
  const prefix = e.entity_type ? `${e.entity_type}:` : null;
  const stripped = prefix && name.startsWith(prefix) ? name.slice(prefix.length).trim() : name;
  if (!stripped) return null;
  return stripped.length > 90 ? `${stripped.slice(0, 87)}…` : stripped;
}

function toLinks(payload: EntityPayload): QuestionLink[] {
  const edges = [...(payload.outgoing ?? []), ...(payload.incoming ?? [])];
  const seen = new Set<string>();
  const links: QuestionLink[] = [];

  for (const e of edges) {
    if (!e.entity_id || seen.has(e.entity_id)) continue;
    seen.add(e.entity_id);
    links.push({
      id: e.entity_id,
      entityType: e.entity_type,
      label: edgeLabel(e),
      relationship: e.relationship_type,
      direction: e.direction,
      ambient: AMBIENT_TYPES.has(e.entity_type ?? "") || e.relationship_type === "PART_OF",
    });
  }

  // Subject-matter edges first, bookkeeping after — see AMBIENT_TYPES.
  return links.sort((a, b) => Number(a.ambient) - Number(b.ambient));
}

export interface LinksState {
  links: QuestionLink[];
  loading: boolean;
  /** True when the relationships read failed, as opposed to returning none. */
  failed: boolean;
}

/**
 * One question's stored relationships.
 *
 * Fetched on demand from the detail view rather than folded into the 10s task
 * poll: this is one extra request when a question is OPENED, against eight
 * relationship reads on every tick for a panel that is usually collapsed.
 *
 * "No edges" and "the read failed" are kept apart. Reporting a failure as an
 * empty graph would quietly tell the operator this question is unconnected,
 * which is a claim about his data rather than about the request.
 */
export function useQuestionLinks(id: string | null): LinksState {
  const [state, setState] = useState<LinksState>({ links: [], loading: true, failed: false });

  useEffect(() => {
    if (!id) {
      setState({ links: [], loading: false, failed: false });
      return;
    }
    // Guards against a slow response for a question the operator has already
    // navigated away from overwriting the one now on screen.
    let live = true;
    setState({ links: [], loading: true, failed: false });

    fetch(`/api/entity?id=${encodeURIComponent(id)}`)
      .then((r) => r.json() as Promise<EntityPayload>)
      .then((payload) => {
        if (!live) return;
        if (payload.error) {
          setState({ links: [], loading: false, failed: true });
          return;
        }
        setState({
          links: toLinks(payload),
          loading: false,
          failed: Boolean(payload.relationshipsFailed),
        });
      })
      .catch(() => {
        if (live) setState({ links: [], loading: false, failed: true });
      });

    return () => {
      live = false;
    };
  }, [id]);

  return state;
}
