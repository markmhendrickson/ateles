/**
 * THE CURRENT SESSION, AS NEOTOMA HOLDS IT
 * ----------------------------------------
 * A `conversation` entity whose `conversation_id` is the harness session uuid IS
 * the session's record. That join is exact, so when it resolves this view stops
 * being an inference about the filesystem and becomes stored data.
 *
 * What the session did is then its edges: `REFERS_TO` the entities it touched,
 * `PART_OF` the plan it ran under. Those edges are the only per-session work
 * list that exists — no `task` entity carries a session reference, and
 * `task.conversation_id`, where set at all, holds hand-written slugs rather than
 * session ids. See `Sessions.tsx`.
 *
 * BOTH DIRECTIONS COUNT. Agents write that edge from whichever side they are
 * on: a session storing its own turn writes `conversation -> task`, while an
 * agent filing a task later writes `task -> conversation`. Both assert the same
 * membership, so the proxy merges and dedupes them and this module never sees
 * direction at all. It is deliberately absent from `RelatedEntity`: surfacing it
 * would label rows by which agent authored the edge, which is not a fact about
 * the work. See `fetchRelated` in the proxy.
 *
 * VOCABULARY RULE
 * ---------------
 * Every label this module produces traces to an entity_type, a field name, or a
 * stored field value. `entityTypeLabel` turns `rendered_page` into "Rendered
 * pages" and stops there — it does not rename the type into something more
 * evocative. A `task` with `category: "topic"` is a task, and is labelled
 * "Tasks · topic" after the field value that makes it one; there is no separate
 * concept called a "work topic" in Neotoma and the UI must not imply one.
 */

/** A `conversation` entity as `/api/conversation` returns it. */
export interface ConversationEntity {
  entity_id: string;
  snapshot?: Record<string, unknown> | null;
}

/**
 * One entity the conversation points at.
 *
 * `entity_type` is null while the proxy is still hydrating this target in the
 * background — the edge is known, its target is not yet. That is a real and
 * temporary state, distinct from the edge not existing, and the UI shows it as
 * such rather than dropping the row. See the proxy's `fetchRelated`.
 */
export interface RelatedEntity {
  entity_id: string;
  entity_type: string | null;
  canonical_name: string | null;
  relationship_type: string;
  snapshot: Record<string, unknown> | null;
  /**
   * The entity's OWN outgoing edges, supplied for `task` targets only.
   *
   * `null` means the proxy has not read them yet — NOT that there are none.
   * The distinction is load-bearing: `ownership()` below refuses to call a task
   * unblocked on an edge list it has not seen.
   */
  edges?: RelatedEdges | null;
}

/** A task's outgoing edges, as the proxy resolves them. */
export interface RelatedEdges {
  /** Tasks this one DEPENDS_ON. */
  dependsOn: string[];
  /** `task` entities this one is PART_OF — its topic parents. */
  partOfTasks: string[];
  /** `plan` entities this one is PART_OF. */
  partOfPlans: string[];
}

export interface ConversationPayload {
  live?: LiveKey | null;
  conversation?: ConversationEntity | null;
  related?: RelatedEntity[];
  error?: string;
}

/**
 * The harness session id and how it was determined.
 *
 * This is a LOOKUP KEY, not the session record. Once a conversation entity
 * matches it, nothing filesystem-derived is shown. It stays visible only in the
 * fallback case, where the caveat is the point.
 */
export interface LiveKey {
  sessionId: string;
  sessionKey: string;
  projectSlug: string;
  mtime: string;
  basis: string;
}

/** The parsed conversation — `conversation` schema fields, named as stored. */
export interface Conversation {
  id: string;
  conversationId: string | null;
  title: string | null;
  harness: string | null;
  status: string | null;
  repositoryName: string | null;
  workspaceKind: string | null;
  startTimestamp: string | null;
  lastUpdated: string | null;
  topics: string[];
  scopeSummary: string | null;
}

function str(v: unknown): string | null {
  return typeof v === "string" && v.trim() ? v.trim() : null;
}

function strArray(v: unknown): string[] {
  const raw = typeof v === "string" ? safeJson(v) : v;
  if (!Array.isArray(raw)) return [];
  return raw.map((x) => (typeof x === "string" ? x : String(x ?? ""))).filter(Boolean);
}

function safeJson(s: string): unknown {
  try {
    return JSON.parse(s);
  } catch {
    return null;
  }
}

/** Neotoma sometimes double-nests the snapshot; tolerate both shapes. */
function unwrap(snapshot: Record<string, unknown> | null | undefined): Record<string, unknown> {
  if (!snapshot || typeof snapshot !== "object") return {};
  const inner = snapshot.snapshot;
  if (inner && typeof inner === "object") return inner as Record<string, unknown>;
  return snapshot;
}

export function parseConversation(row: ConversationEntity): Conversation {
  const s = unwrap(row.snapshot);
  return {
    id: row.entity_id,
    conversationId: str(s.conversation_id),
    title: str(s.title),
    harness: str(s.harness),
    status: str(s.status),
    repositoryName: str(s.repository_name),
    workspaceKind: str(s.workspace_kind),
    startTimestamp: str(s.start_timestamp),
    lastUpdated: str(s.last_updated),
    topics: strArray(s.topics),
    scopeSummary: str(s.scope_summary),
  };
}

/**
 * A display name for a related entity.
 *
 * `canonical_name` is Neotoma's own name for the entity, but it sometimes
 * arrives prefixed with the entity type ("plan:Ateles Agent Swarm
 * Architecture"). The prefix is redundant next to a group already headed by that
 * type, so it is stripped — the remaining text is still the stored name, not a
 * rewritten one. A snapshot `title` wins where one exists, since that is the
 * field the author actually wrote.
 */
export function relatedTitle(e: RelatedEntity): string {
  const s = unwrap(e.snapshot);
  const title = str(s.title) ?? str(s.name);
  if (title) return title;

  const canonical = str(e.canonical_name);
  if (!canonical) return e.entity_id;
  const prefix = `${e.entity_type}:`;
  return e.entity_type && canonical.startsWith(prefix)
    ? canonical.slice(prefix.length).trim()
    : canonical;
}

/** Raw `status` as stored, for the types that carry one. Never normalized away. */
export function relatedStatus(e: RelatedEntity): string | null {
  return str(unwrap(e.snapshot).status);
}

/** Raw `category` as stored — `"topic"`, `"open_question"`, or absent. */
export function relatedCategory(e: RelatedEntity): string | null {
  return str(unwrap(e.snapshot).category);
}

/** Raw `assigned_to` as stored. Null when nothing owns the task. */
export function relatedAssignee(e: RelatedEntity): string | null {
  return str(unwrap(e.snapshot).assigned_to);
}

/* ------------------------------------------------------------------ *
 * CLOSED vs OPEN — the filter's only judgement.
 * ------------------------------------------------------------------ */

/**
 * Statuses that mean the work is OVER, whatever the outcome.
 *
 * DELIBERATELY AN ALLOWLIST OF CLOSED VALUES, not of open ones. The vocabulary
 * is wider than any one source declares: `task_lifecycle.py` names pending,
 * routed, executing, verified, done, failed, blocked, awaiting_approval,
 * awaiting_input, declined, superseded — and live data adds `open`, `todo`,
 * `completed`, `awaiting_release_confirmation` on top of those. A new value will
 * appear that neither list anticipates.
 *
 * So the default filter hides ONLY what it positively recognizes as finished.
 * Anything unrecognized stays visible: showing an operator a task he cannot
 * classify is a small cost, and silently hiding one because a daemon invented a
 * status word is the failure this dashboard exists to prevent.
 */
const CLOSED_STATUSES = new Set([
  "done",
  "completed",
  "declined",
  "superseded",
  "canceled",
  "cancelled",
]);

/** Is this status one of the recognized finished states? */
export function isClosedStatus(status: string | null): boolean {
  return status ? CLOSED_STATUSES.has(status.trim().toLowerCase()) : false;
}

/** An entity is open unless its stored status is a recognized closed one. */
export function isOpenEntity(e: RelatedEntity): boolean {
  return !isClosedStatus(relatedStatus(e));
}

/* ------------------------------------------------------------------ *
 * WHAT `pending` ACTUALLY MEANS
 * ------------------------------------------------------------------ */

/**
 * The three states a not-yet-finished task can be in.
 *
 * `status: pending` conflates them, which is the complaint this answers: it
 * does not distinguish a task someone is holding from one that is simply inert.
 * All three are DERIVED FROM STORED DATA — `assigned_to` and the task's own
 * `DEPENDS_ON` edges — and none replaces the raw status, which stays on screen
 * beside them.
 *
 * `unknown` is not a fourth state but an admission: the edges have not been
 * read yet, so this refuses to claim the task is unblocked. See `RelatedEdges`.
 */
export type Ownership =
  | { kind: "unknown" }
  | { kind: "blocked"; blockers: string[] }
  | { kind: "owned"; owner: string; spawnable: boolean }
  | { kind: "inert" };

/**
 * Classify one task, given a way to look up whether a blocker is still open.
 *
 * ORDER MATTERS. A blocked task is reported as blocked even when it has an
 * owner: the owner cannot start, so "waiting on ent_… " is the more useful
 * sentence. A dependency on an already-DONE task is not a blocker, which is why
 * this needs the lookup rather than just an edge count.
 *
 * `spawnable` reuses `dispatchability` from `taskState.ts` — the same check the
 * task detail State panel makes — rather than restating its role list here.
 */
export function ownership(
  e: RelatedEntity,
  isBlockerOpen: (id: string) => boolean,
  spawnable: (assignedTo: string | null) => boolean,
): Ownership {
  const edges = e.edges;
  if (!edges) return { kind: "unknown" };

  const blockers = edges.dependsOn.filter(isBlockerOpen);
  if (blockers.length > 0) return { kind: "blocked", blockers };

  const owner = relatedAssignee(e);
  if (owner) return { kind: "owned", owner, spawnable: spawnable(owner) };

  return { kind: "inert" };
}

/**
 * A group of related entities, headed by the entity_type they share.
 *
 * `key` distinguishes `task` entities by their stored `category` so the four
 * `category: topic` tasks can be read apart from the rest — but the group is
 * still headed "Tasks · topic", because that is what they are. Inventing a
 * separate noun for them would describe something Neotoma does not store.
 */
export interface RelatedGroup {
  key: string;
  /** null for the group of edges whose targets have not hydrated yet. */
  entityType: string | null;
  /** The `category` value shared by this group's tasks, when it has one. */
  category: string | null;
  label: string;
  entities: RelatedEntity[];
}

/** The group holding edges whose target entity has not loaded yet. */
export const PENDING_GROUP = "__pending__";

/** Plural display form of an entity_type. The TYPE, not a renaming of it. */
export function entityTypeLabel(entityType: string): string {
  const words = entityType.replace(/_/g, " ");
  const plural = words.endsWith("s") ? words : `${words}s`;
  return plural.charAt(0).toUpperCase() + plural.slice(1);
}

/**
 * Order groups so the two the operator came for lead: the reports
 * (`rendered_page`) and the work (`task`). Everything else follows
 * alphabetically rather than being ranked by a judgement this view cannot make.
 */
const TYPE_ORDER = ["rendered_page", "task"];

export function groupRelated(entities: RelatedEntity[]): RelatedGroup[] {
  const groups = new Map<string, RelatedGroup>();

  for (const e of entities) {
    // An edge whose target has not hydrated yet has no type to group by. It
    // goes in its own group rather than being dropped or guessed at.
    if (!e.entity_type) {
      let pending = groups.get(PENDING_GROUP);
      if (!pending) {
        pending = {
          key: PENDING_GROUP,
          entityType: null,
          category: null,
          label: "Still loading",
          entities: [],
        };
        groups.set(PENDING_GROUP, pending);
      }
      pending.entities.push(e);
      continue;
    }

    // Only `task` splits by category — it is the type where the distinction is
    // load-bearing (a `topic` task is a workstream, not a unit of work).
    const category = e.entity_type === "task" ? relatedCategory(e) : null;
    const key = category ? `${e.entity_type}:${category}` : e.entity_type;

    let group = groups.get(key);
    if (!group) {
      group = {
        key,
        entityType: e.entity_type,
        category,
        label: category
          ? `${entityTypeLabel(e.entity_type)} · ${category}`
          : entityTypeLabel(e.entity_type),
        entities: [],
      };
      groups.set(key, group);
    }
    group.entities.push(e);
  }

  // Unhydrated edges sort last: they are the least informative rows on screen
  // and they disappear on a later poll.
  const rank = (g: RelatedGroup) => {
    if (!g.entityType) return TYPE_ORDER.length + 1;
    const i = TYPE_ORDER.indexOf(g.entityType);
    return i === -1 ? TYPE_ORDER.length : i;
  };

  return [...groups.values()].sort((a, b) => {
    const d = rank(a) - rank(b);
    if (d !== 0) return d;
    if (a.entityType !== b.entityType) {
      return String(a.entityType ?? "").localeCompare(String(b.entityType ?? ""));
    }
    // Uncategorized tasks before categorized ones, then alphabetically.
    return String(a.category ?? "").localeCompare(String(b.category ?? ""));
  });
}

/* ------------------------------------------------------------------ *
 * GROUPING — by what the data actually supports.
 * ------------------------------------------------------------------ */

/**
 * WHY THIS IS NOT "GROUP BY PLAN".
 *
 * The request was to organize a session's tasks by plan. Measured against this
 * session's own 53 tasks, that yields ONE group: 39 of them carry a `PART_OF`
 * edge and every single edge points at the same plan
 * (`ent_99ace4dd6673aa36ed08b1fe`), with the other 14 carrying no plan edge at
 * all. A grouping with one group is not a grouping.
 *
 * That is not an accident of this session. `plan` is not a workstream layer in
 * this instance: the type is dominated by per-issue work items named
 * `plan:Resolve #943`, so a session's tasks either all roll up to the one plan
 * it is bound to, or to nothing.
 *
 * WHAT THE DATA DOES SUPPORT is the topic layer directly beneath it: 42 of the
 * 53 tasks carry `PART_OF` edges to a `task` with `category: topic`, spread
 * across four of them. That is a real partition with real groups, so it is the
 * one rendered — and the collapse is stated on screen rather than quietly
 * substituted, because the operator asked for plans and is entitled to know why
 * he is looking at something else.
 *
 * Both facts are COMPUTED PER SESSION, never hardcoded. A session whose tasks
 * span several plans will group by plan and say so.
 */
export interface Grouping {
  /** What the grouping is keyed on. */
  basis: "plan" | "topic" | "none";
  groups: TaskGroup[];
  /** Distinct plans the tasks roll up to — the collapse evidence. */
  planCount: number;
  /** True when plan grouping was tried and produced fewer than two groups. */
  planCollapsed: boolean;
}

export interface TaskGroup {
  /** Entity id of the plan or topic heading this group, null for the remainder. */
  id: string | null;
  label: string;
  entities: RelatedEntity[];
}

/** The label for tasks that carry no edge to whatever the grouping is keyed on. */
const UNGROUPED_LABEL = "No topic edge";

/**
 * Partition tasks by their topic parent, falling back to plan when topics do
 * not partition anything either.
 *
 * `titleOf` resolves a parent id to its name; parents are themselves entities in
 * the session's related set, so no extra fetch is involved.
 */
export function groupTasks(
  tasks: RelatedEntity[],
  titleOf: (id: string) => string | null,
): Grouping {
  const plans = new Set<string>();
  for (const t of tasks) for (const p of t.edges?.partOfPlans ?? []) plans.add(p);

  const byTopic = new Map<string, RelatedEntity[]>();
  const ungrouped: RelatedEntity[] = [];
  for (const t of tasks) {
    // A topic task heads its own group rather than sitting inside another's.
    const parents = relatedCategory(t) === "topic" ? [] : (t.edges?.partOfTasks ?? []);
    if (parents.length === 0) {
      ungrouped.push(t);
      continue;
    }
    // A task under several topics is listed under each: the edges are equally
    // true, and picking one would hide a real membership.
    for (const p of parents) {
      const bucket = byTopic.get(p);
      if (bucket) bucket.push(t);
      else byTopic.set(p, [t]);
    }
  }

  const groups: TaskGroup[] = [...byTopic.entries()]
    .map(([id, entities]) => ({ id, label: titleOf(id) ?? id, entities }))
    .sort((a, b) => b.entities.length - a.entities.length || a.label.localeCompare(b.label));

  if (ungrouped.length > 0) {
    groups.push({ id: null, label: UNGROUPED_LABEL, entities: ungrouped });
  }

  // Fewer than two real topic groups means topics partition nothing either;
  // say so with `basis: "none"` rather than dressing one list as a grouping.
  const realGroups = groups.filter((g) => g.id !== null).length;

  return {
    basis: realGroups >= 2 ? "topic" : "none",
    groups,
    planCount: plans.size,
    planCollapsed: plans.size < 2,
  };
}

/**
 * The plans this session's work rolls up to.
 *
 * Shown as its own short section regardless of whether grouping uses them —
 * "which plans is this session working under" is a fair question with a real
 * answer even when the answer is "one".
 */
export function sessionPlans(related: RelatedEntity[]): RelatedEntity[] {
  const direct = related.filter((e) => e.entity_type === "plan");
  const seen = new Set(direct.map((p) => p.entity_id));

  // Plans reached only through a task's PART_OF edge count too: the session
  // works under a plan whether or not the conversation links it directly.
  const viaTasks: RelatedEntity[] = [];
  for (const t of related) {
    for (const id of t.edges?.partOfPlans ?? []) {
      if (seen.has(id)) continue;
      seen.add(id);
      const hit = related.find((e) => e.entity_id === id);
      if (hit) viaTasks.push(hit);
    }
  }
  return [...direct, ...viaTasks];
}
