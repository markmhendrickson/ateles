/**
 * Dev-only Neotoma proxy (Vite dev-server middleware).
 *
 * WHY THIS EXISTS
 * ---------------
 * The dashboard needs a Neotoma bearer token to read `task` entities. A browser
 * app cannot read ~/.config/neotoma/.env, and shipping the token to client-side
 * JS would leak it to anyone with devtools (and into any bundle we ever build).
 *
 * So the token stays in the Node process: the browser calls the SAME-ORIGIN
 * route `/api/tasks` with no credentials at all, and this middleware attaches
 * `Authorization: Bearer …` server-side before forwarding to Neotoma.
 *
 * The token is never logged, never echoed in a response, and never sent to the
 * client — error paths below deliberately return generic text rather than the
 * upstream body, which can contain the request we sent.
 *
 * ROUTES — READ-ONLY, DELIBERATELY
 * --------------------------------
 *   GET  /api/tasks           one page of `task` entities, newest first,
 *                             optionally narrowed server-side by status or owner
 *   GET  /api/task-total      how many `task` entities exist — the denominator
 *                             for the page above, and a REAL count rather than
 *                             the saturating filtered kind
 *   GET  /api/agents          every `agent_definition` entity (the swarm roster)
 *   GET  /api/sessions        every `session_digest` entity (the session history)
 *   GET  /api/conversation    the `conversation` entity for the live session,
 *                             plus every entity it points at
 *   GET  /api/workflows       every `workflow_definition` entity (declared gates)
 *   GET  /api/facets          true bucket counts across every OPEN task —
 *                             what the chips on the Tasks page show, so they
 *                             describe the backlog rather than the loaded page
 *   GET  /api/lifecycle       how many tasks sit in each lifecycle state —
 *                             eleven count-only queries, no snapshots
 *   GET  /api/entity?id=…     ONE entity of any type, plus its relationships —
 *                             what the in-app detail views read
 *   GET  /api/scan-freshness  how far the committed static code scan has fallen
 *                             behind the repo's HEAD — the one thing about that
 *                             scan that changes, and the only one worth showing
 *
 * There is no write route, and adding one would be a regression rather than a
 * feature. The dashboard is a view; every mutation goes through the operator's
 * conversation with the orchestrating agent. This matters because the token
 * here is a full-privilege Neotoma credential: any write route exposed on
 * localhost is a route an agent driving a browser can also take, and a
 * fabricated answer written onto a question is indistinguishable downstream
 * from one the operator gave. With no such route the failure is impossible by
 * construction, not merely discouraged.
 *
 * Every helper below therefore reaches Neotoma only via `/entities/query`.
 */
import type { Plugin } from "vite";
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
/**
 * ONE definition of the filtered-total ceiling, shared with the client that
 * renders it. Both sides must agree on what 10000 means — the server decides
 * whether a total saturated, the client decides how to say so — and two copies
 * of the number would let them disagree silently. `taskCount.ts` imports
 * nothing and touches no DOM, so it is safe to pull into the dev server.
 */
import { FILTERED_TOTAL_CEILING } from "../src/taskCount";

const DEFAULT_BASE_URL = "https://neotoma.markmhendrickson.com";

/**
 * The repo whose HEAD the code scan is compared against.
 *
 * `process.cwd()` is the dev server's directory (`apps/task-dashboard`), and
 * `git -C` resolves upward from there — which lands on the WORKTREE this server
 * was started in, not the shared main clone. That is the correct target: the
 * scan generator walks up from its own path to the same root, so both sides
 * measure one tree. Reading a different checkout's HEAD would report drift that
 * has nothing to do with the file on screen.
 */
const REPO_ROOT = process.cwd();

/**
 * Neotoma sits behind Cloudflare, which 1010-blocks Node/undici's default
 * User-Agent. Every daemon in this repo sends this exact UA; match it.
 */
const NEOTOMA_USER_AGENT = "ateles-neotoma-sync/1.0";

/** Where the operator's SOPS materialization puts the token. */
const ENV_PATH = join(homedir(), ".config", "neotoma", ".env");

/**
 * Read NEOTOMA_BEARER_TOKEN from the process env, falling back to the
 * operator's materialized dotenv. Only the two keys we need are extracted —
 * that file also holds unrelated secrets, so we never load it wholesale into
 * process.env.
 */
function loadEnv(): { baseUrl: string; token: string } {
  let token = process.env.NEOTOMA_BEARER_TOKEN ?? "";
  let baseUrl = process.env.NEOTOMA_BASE_URL ?? "";

  if (!token || !baseUrl) {
    try {
      for (const line of readFileSync(ENV_PATH, "utf8").split("\n")) {
        const m = /^\s*(?:export\s+)?(NEOTOMA_BEARER_TOKEN|NEOTOMA_BASE_URL)\s*=\s*(.*)$/.exec(line);
        if (!m) continue;
        // Strip surrounding quotes and any trailing comment.
        const value = m[2].trim().replace(/^["']|["']$/g, "");
        if (m[1] === "NEOTOMA_BEARER_TOKEN" && !token) token = value;
        if (m[1] === "NEOTOMA_BASE_URL" && !baseUrl) baseUrl = value;
      }
    } catch {
      // No dotenv on this machine — handled by the empty-token check below.
    }
  }

  return { baseUrl: (baseUrl || DEFAULT_BASE_URL).replace(/\/+$/, ""), token };
}

/**
 * POST `body` to a Neotoma path with the bearer token attached server-side.
 *
 * `timeoutMs` defaults to the 30s every interactive route uses. The schema
 * sampler passes a longer budget because its queries are deliberately the
 * heaviest ones here — 400 snapshots of the largest types — and a timeout
 * there costs a whole type's analysis rather than one row of a list.
 */
async function neotomaPost(
  path: string,
  body: unknown,
  timeoutMs = 30_000,
): Promise<unknown> {
  const { baseUrl, token } = loadEnv();
  if (!token) {
    throw new Error(
      `NEOTOMA_BEARER_TOKEN not set and not found in ${ENV_PATH}. ` +
        `Materialize it via SOPS, or export it before running \`npm run dev\`.`,
    );
  }

  const res = await fetch(`${baseUrl}${path}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      Accept: "application/json",
      "User-Agent": NEOTOMA_USER_AGENT,
    },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(timeoutMs),
  });

  if (!res.ok) {
    // Deliberately generic: the upstream body can echo our request.
    throw new Error(`Neotoma returned HTTP ${res.status}`);
  }
  return res.json();
}

/** GET a Neotoma path with the bearer token attached server-side. */
async function neotomaGet(path: string): Promise<unknown> {
  const { baseUrl, token } = loadEnv();
  if (!token) {
    throw new Error(
      `NEOTOMA_BEARER_TOKEN not set and not found in ${ENV_PATH}. ` +
        `Materialize it via SOPS, or export it before running \`npm run dev\`.`,
    );
  }

  const res = await fetch(`${baseUrl}${path}`, {
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/json",
      "User-Agent": NEOTOMA_USER_AGENT,
    },
    signal: AbortSignal.timeout(30_000),
  });

  if (!res.ok) throw new Error(`Neotoma returned HTTP ${res.status}`);
  return res.json();
}

/**
 * A registered schema, its confirmed ABSENCE, or a failure to find out which.
 *
 * These are three different states and collapsing any two of them produces a
 * confident wrong answer. `absent` is a real finding — upstream answered, and
 * the answer was 404. `failed` is the absence of a finding: a timeout, a 5xx,
 * or a transport error, about which nothing may be concluded.
 */
type SchemaFetch =
  | { kind: "ok"; schema: unknown }
  | { kind: "absent" }
  | { kind: "failed" };

/**
 * Fetch one type's declared schema, keeping "no such schema" separate from
 * "could not ask".
 *
 * WHY THIS IS NOT `neotomaGet(...).catch(() => null)`:
 * that is what it used to be, and the null it produced meant BOTH "upstream
 * says this type has no schema" and "the request fell over". The caller could
 * only read it one way, chose the first, and cached `declaredFields: 0` with
 * the note "No schema registered for this type." against `plan` (99 declared
 * fields), `project` (27), and `escalation` (10) — all three registered, all
 * three fetchable, one transient failure away from being reported as
 * schemaless forever, because the analysis cache never retries a hit.
 *
 * This is the same class of bug `measuredSample()` fixed on the sample leg, on
 * the leg that was left. A 404 is the ONLY thing that may be read as absence.
 */
async function fetchSchema(entityType: string): Promise<SchemaFetch> {
  const { baseUrl, token } = loadEnv();
  if (!token) return { kind: "failed" };

  try {
    const res = await fetch(`${baseUrl}/schemas/${encodeURIComponent(entityType)}`, {
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/json",
        "User-Agent": NEOTOMA_USER_AGENT,
      },
      signal: AbortSignal.timeout(30_000),
    });

    // Upstream answered definitively: there is no schema for this type.
    if (res.status === 404) return { kind: "absent" };
    // Anything else non-2xx is a failure to measure, not a measurement.
    if (!res.ok) return { kind: "failed" };
    return { kind: "ok", schema: await res.json() };
  } catch {
    return { kind: "failed" };
  }
}

/**
 * THE NON-TERMINAL STATUS VOCABULARY — what "open work" means to a query.
 *
 * Expressed as a POSITIVE list because it has to be: `snapshot_filters` accepts
 * only `eq | in | gt | lt | gte | lte | contains | contains_word`, and BOTH
 * `ne` and `nin` are rejected 400. "Everything except done" is therefore not
 * expressible upstream, and the open set must be enumerated instead.
 *
 * The list is the union of the dispatcher's state machine
 * (`lib/daemon_runtime/task_lifecycle.py`) and the legacy spellings production
 * actually holds, because `status` is typed as a bare string with no enum and
 * both vocabularies are live. Measured 2026-08-31, one count per value:
 * pending 3547, open 1088, in_progress 394, todo 252, awaiting_input 90,
 * blocked 74, executing 3, active 1, awaiting_release_confirmation 1,
 * awaiting_approval 1, routed 0 — summing to 5451, which is EXACTLY the total
 * the combined `in` query reports. That agreement is the check that this list
 * is complete: a missing spelling would show up as an unexplained shortfall
 * against the 20,989-task denominator.
 *
 * A value here that no task carries costs nothing. A value MISSING from here
 * silently hides open work, so err toward including a spelling.
 */
const OPEN_TASK_STATUSES = [
  "pending",
  "open",
  "todo",
  "in_progress",
  "active",
  "routed",
  "executing",
  "blocked",
  "awaiting_input",
  "awaiting_approval",
  "awaiting_release_confirmation",
] as const;

/**
 * Turn a "stalled for N days" request into the ISO cutoff the query needs.
 *
 * Computed SERVER-SIDE from a day count rather than accepting a date from the
 * client, so the value in the filter is always a well-formed ISO timestamp and
 * the route keeps its property that no client string reaches Neotoma
 * unvalidated. Null for anything not a positive finite number — an unparseable
 * `stale_days` drops the filter rather than inventing a cutoff, which would
 * silently answer a different question than the one asked.
 */
function staleBefore(days: string | null): string | null {
  if (!days) return null;
  const n = Number(days);
  if (!Number.isFinite(n) || n <= 0) return null;
  return new Date(Date.now() - n * 86_400_000).toISOString();
}

/** Server-side narrowing for the task list. Null/false means "do not filter". */
interface TaskFilters {
  /** Exact `status`. Takes precedence over `openOnly` when both are set. */
  status: string | null;
  /** Restrict to `OPEN_TASK_STATUSES` — the default view's rule. */
  openOnly: boolean;
  /** Exact `assigned_to`, matched as stored (production holds mixed case). */
  assignedTo: string | null;
  /** Exact `priority` — "high" is the one people actually ask for. */
  priority: string | null;
  /**
   * Untouched since this ISO date — the "stalled" query.
   *
   * Applied as `updated_since`'s COMPLEMENT: Neotoma's top-level
   * `updated_since` selects rows touched AFTER a date, and there is no
   * `updated_before`. So staleness is expressed on the snapshot's own
   * `updated_at` with `lt`, which sorts as an ISO string and therefore
   * compares correctly without a date type.
   */
  staleBefore: string | null;
}

/**
 * One page of `task` entities, newest first, NARROWED SERVER-SIDE.
 *
 * WHY THE FILTER GOES UPSTREAM. There are 20,989 tasks and this route can
 * return at most a few hundred, so an unfiltered query is always a truncation —
 * it just used to be a silent one. Filtering at the query makes the returned
 * page the ANSWER to a question ("open work, newest first") instead of an
 * arbitrary prefix of everything, and it is also cheaper: a narrow filter is
 * the fastest thing here (`open` + one assignee: 86 rows in 4.9s) while the
 * bare type query is the slowest.
 *
 * Client-side filtering cannot substitute. Narrowing the 200 rows this route
 * already returned only ever shows a subset of a set that was already wrong;
 * the 5,251 open tasks past the cutoff are not in the response to filter.
 *
 * NOTE: `sort_by`/`sort_order` are the parameter names that actually work.
 * `sort`/`order_by` are accepted and then SILENTLY IGNORED — the response comes
 * back in entity-id order, which looks plausible but is not recent-first.
 */
async function fetchTasks(limit: number, filters: TaskFilters): Promise<unknown> {
  const snapshotFilters: Record<string, { op: string; value: unknown }> = {};

  // An explicit status wins: picking "pending" from the status control is a
  // narrowing of the open set, so it must not be re-widened by the `in` list.
  if (filters.status) {
    snapshotFilters.status = { op: "eq", value: filters.status };
  } else if (filters.openOnly) {
    snapshotFilters.status = { op: "in", value: [...OPEN_TASK_STATUSES] };
  }
  if (filters.assignedTo) {
    snapshotFilters.assigned_to = { op: "eq", value: filters.assignedTo };
  }
  if (filters.priority) {
    snapshotFilters.priority = { op: "eq", value: filters.priority };
  }
  if (filters.staleBefore) {
    snapshotFilters.updated_at = { op: "lt", value: filters.staleBefore };
  }

  const filtered = Object.keys(snapshotFilters).length > 0;

  const body = (await neotomaPost("/entities/query", {
    entity_type: "task",
    limit,
    include_snapshots: true,
    sort_by: "last_observation_at",
    sort_order: "desc",
    ...(filtered ? { snapshot_filters: snapshotFilters } : {}),
  })) as Record<string, unknown>;

  /**
   * Say which kind of `total` this is, so the client never has to guess.
   *
   * Only a FILTERED total can saturate; the unfiltered path is a real
   * aggregate, which is why the 20,989 denominator can be trusted while a
   * filtered 10000 cannot.
   */
  return {
    ...body,
    total_saturated: filtered && body.total === FILTERED_TOTAL_CEILING,
    filters_applied: filtered ? snapshotFilters : null,
  };
}

/**
 * The full swarm roster: every `agent_definition` entity.
 *
 * Unlike tasks this is a small, bounded set (40 at time of writing) and the
 * directory wants all of it, so there is no pagination — one request, whole
 * roster. Sorted by name client-side: the useful order here is alphabetical
 * within a tier, not recency, and `sort_by: "name"` is a snapshot field rather
 * than a top-level column, so the server cannot order by it.
 */
function fetchAgents(limit: number): Promise<unknown> {
  return neotomaPost("/entities/query", {
    entity_type: "agent_definition",
    limit,
    include_snapshots: true,
    sort_by: "last_observation_at",
    sort_order: "desc",
  });
}

/**
 * WHICH SESSION ID IS THIS?
 * -------------------------
 * The filesystem answers the narrow question "what is the harness session id of
 * the session driving this dev server": Claude Code writes a transcript at
 * ~/.claude/projects/<project-slug>/<session-id>.jsonl and appends to it as the
 * session runs, so the most-recently-modified transcript under the slug for THIS
 * checkout names the session.
 *
 * That id is only a LOOKUP KEY. It is not the session record — the record is the
 * `conversation` entity whose `conversation_id` equals this uuid, fetched by
 * `fetchConversation` below. When that entity exists, the session's identity and
 * its work come from Neotoma; the filesystem contributed nothing but the key.
 *
 * The id itself is a strong but not certain inference: two sessions open on the
 * same worktree would both be writing, and the newest would win. So the evidence
 * travels with it (`basis`, `mtime`) for the UI to show in the case where no
 * conversation entity backs it up.
 */
interface LiveSession {
  sessionId: string;
  /** `<harness>:<session-id>`, the shape `session_digest.session_key` uses. */
  sessionKey: string;
  projectSlug: string;
  /** Last write to the transcript — how recently this session was active. */
  mtime: string;
  /** How the id was determined, surfaced verbatim in the UI. */
  basis: string;
}

function liveSession(): LiveSession | null {
  try {
    // Claude Code keys transcripts by the directory the SESSION opened, which
    // is the worktree root — while this dev server's cwd is the app subdir
    // (apps/task-dashboard). Walk up from cwd and take the first ancestor that
    // has a transcript dir, so both layouts resolve.
    const projects = join(homedir(), ".claude", "projects");
    let dir = process.cwd();
    let slug = "";

    for (let up = 0; up < 6; up++) {
      const candidate = dir.replace(/[^a-zA-Z0-9]/g, "-");
      if (existsSync(join(projects, candidate))) {
        slug = candidate;
        break;
      }
      const parent = dirname(dir);
      if (parent === dir) break;
      dir = parent;
    }
    if (!slug) return null;

    const newest = readdirSync(join(projects, slug))
      .filter((f) => f.endsWith(".jsonl"))
      .map((f) => ({ f, m: statSync(join(projects, slug, f)).mtimeMs }))
      .sort((a, b) => b.m - a.m)[0];
    if (!newest) return null;

    const sessionId = newest.f.replace(/\.jsonl$/, "");
    return {
      sessionId,
      sessionKey: `claude-code:${sessionId}`,
      projectSlug: slug,
      mtime: new Date(newest.m).toISOString(),
      basis: "most recently written transcript for this worktree",
    };
  } catch {
    // No transcript dir (a different harness, or a checkout never opened in
    // Claude Code). The UI handles null by falling back, clearly labelled.
    return null;
  }
}

/**
 * THE SESSION'S OWN RECORD IN NEOTOMA
 * ----------------------------------
 * A `conversation` entity carries `conversation_id`, and when an agent sets that
 * to the harness session uuid the join is exact — no heuristic, no proximity
 * guess. `snapshot_filters` does that match server-side.
 *
 * FILTER SHAPE, verified against the live API rather than assumed: each field
 * takes an OBJECT, `{op, value}`. A bare string is rejected 400
 * (`Expected object, received string`), and `{eq: …}` / `{equals: …}` are also
 * 400 — `op` is the required key. Getting this wrong is not a silent no-op, so
 * the failure is loud, but it is worth writing down because three plausible
 * shapes are all wrong.
 *
 * Historically these ids were agent-authored slugs ("lanius-pr-558-synchronize")
 * and no conversation could be matched to a running session at all. That is why
 * a miss here is expected rather than exceptional, and why the caller keeps a
 * labelled fallback for it.
 */
function fetchConversation(sessionId: string): Promise<unknown> {
  return neotomaPost("/entities/query", {
    entity_type: "conversation",
    limit: 5,
    include_snapshots: true,
    snapshot_filters: { conversation_id: { op: "eq", value: sessionId } },
  });
}

/**
 * WHAT THE SESSION TOUCHED — its edges in BOTH directions, hydrated.
 *
 * DIRECTION IS NOT MEANING HERE. An edge `conversation -REFERS_TO-> task` and an
 * edge `task -REFERS_TO-> conversation` both say the same thing: that task
 * belongs to this session. Which side authored the edge is an artifact of which
 * agent happened to write it — the swarm produces both, within a single session.
 * On `ent_723e275edb0bacc8f1e0d44e` the split was 32 outgoing / 27 incoming, the
 * 27 being tasks filed later by a different agent, and reading only `outgoing`
 * hid every one of them from the operator.
 *
 * So both directions are merged and deduped by entity id. Deduping is required,
 * not defensive: a reciprocal pair would otherwise render the same task twice.
 *
 * Two calls, because Neotoma's REST surface has no single one that does both:
 *
 *   1. GET /entities/<id>/relationships — returns `outgoing` AND `incoming`,
 *      in ~1.3s. This is the endpoint that WORKS. `list_relationships` filtered
 *      by source/target returns empty for edges that demonstrably exist, in both
 *      directions, so it is deliberately not used here.
 *   2. GET /entities/<id> per target, to learn each one's `entity_type`,
 *      `canonical_name`, and snapshot. The relationship rows carry ids only.
 *
 * WHY STEP 2 IS NOT AWAITED
 * -------------------------
 * A single entity GET costs ~5s upstream. Thirty-one of them, even six at a
 * time, measured 29-43s end to end — longer than the client's own 10s poll
 * interval, so every request would pile onto the one before it and the view
 * would never leave its skeleton. Measured, not assumed; that is what the first
 * cut of this function did.
 *
 * There is no batch alternative: `/entities/query` has no `entity_ids` filter
 * (passing one is silently IGNORED, which turns the call into a full scan that
 * times out) and `entity_id` is not a snapshot field, so `snapshot_filters`
 * matches nothing. `?include_entities=true` on the relationships endpoint is
 * accepted and ignored.
 *
 * So hydration happens OFF the request path. A response is assembled from
 * whatever the cache already holds; anything missing is fetched in the
 * background and is there for the next poll, seconds later. The first poll after
 * a fresh start returns edges with `entity_type: null`, and the client renders
 * those as pending rather than pretending they are absent — an edge whose target
 * has not loaded yet is not the same as an edge that does not exist.
 *
 * The cache is keyed by entity id and never invalidated by age: these are
 * `entity_type` and `canonical_name`, which do not churn, and a dashboard poll
 * every 10 seconds must not re-fetch 31 entities to re-learn that a task is
 * still a task. A restart of the dev server clears it.
 */
const MAX_RELATED = 120;

interface RelatedEntity {
  entity_id: string;
  /** null while this target is still being hydrated in the background. */
  entity_type: string | null;
  canonical_name: string | null;
  relationship_type: string;
  snapshot: Record<string, unknown> | null;
  /**
   * This entity's own outgoing edges, for `task` targets only.
   *
   * `null` means NOT READ YET, which is deliberately distinct from an empty
   * edge list — the client must not report "no dependencies" on the strength of
   * a request that has not come back. See `hydrateEdges`.
   */
  edges: CachedEdges | null;
}

/**
 * One hydrated entity as the cache holds it.
 *
 * No `relationship_type` (that belongs to an edge, not an entity) and no
 * `edges` — those live in `edgeCache`, which is refreshed on its own schedule
 * because edges change as a session works while entity_type and canonical_name
 * do not.
 */
type CachedEntity = Omit<RelatedEntity, "relationship_type" | "edges">;

/** Hydrated entities, by id. See the note above on why this is never expired. */
const entityCache = new Map<string, CachedEntity>();
/** Ids with a hydration already in flight, so concurrent polls do not duplicate it. */
const hydrating = new Set<string>();

/**
 * ONE TASK'S OWN EDGES — what makes `pending` legible.
 *
 * `status: pending` on its own does not say whether a task is inert or waiting
 * on something. The difference is stored, but on the task's EDGES rather than
 * its fields: an outgoing `DEPENDS_ON` to a task that is not yet done means
 * blocked, and an outgoing `PART_OF` to another `task` names the topic it rolls
 * up under. Neither is in the snapshot, so neither can be read from
 * `/entities/query`.
 *
 * MEASURED, NOT ASSUMED, on this session's 53 tasks: 5 carry a real
 * `DEPENDS_ON`, every one of them pointing at a target still `pending`. So the
 * blocked state is populated, not theoretical, and collapsing it into "inert"
 * would be wrong about five tasks.
 *
 * Hydrated OFF the request path for the same reason entity hydration is: one
 * relationships GET costs ~1.3s, and 53 of them inline would outrun the
 * client's 10s poll. The first poll reports `edgesKnown: false` for a task and
 * the client says so rather than asserting "no dependencies" from data it does
 * not have — an unread edge list is not an empty one.
 */
interface CachedEdges {
  /** Ids of tasks this task DEPENDS_ON (outgoing). */
  dependsOn: string[];
  /** Ids of `task` entities this task is PART_OF — its topic parents. */
  partOfTasks: string[];
  /** Ids of `plan` entities this task is PART_OF. */
  partOfPlans: string[];
}

const edgeCache = new Map<string, CachedEdges>();
const hydratingEdges = new Set<string>();

/**
 * Read one entity's edges into `edgeCache`.
 *
 * Unlike `entityCache`, this IS refreshable — edges are added as a session
 * works — but it is never invalidated on a timer either, because a 10s poll
 * must not re-read 53 relationship lists. A dev-server restart clears it, which
 * is the same contract the entity cache already has.
 */
function hydrateEdges(id: string): void {
  if (edgeCache.has(id) || hydratingEdges.has(id)) return;
  hydratingEdges.add(id);
  void (async () => {
    try {
      const rels = (await neotomaGet(`/entities/${id}/relationships`)) as {
        outgoing?: { relationship_type?: string; target_entity_id?: string; entity_type?: string }[];
      };
      const dependsOn: string[] = [];
      const partOfTasks: string[] = [];
      const partOfPlans: string[] = [];
      for (const r of rels.outgoing ?? []) {
        const target = r.target_entity_id;
        if (!target) continue;
        if (r.relationship_type === "DEPENDS_ON") dependsOn.push(target);
        else if (r.relationship_type === "PART_OF") {
          // The edge row does not always name the target's type, so resolve it
          // from the cache where possible and hydrate it otherwise.
          const hit = entityCache.get(target);
          if (!hit) hydrate(target);
          if (hit?.entity_type === "plan") partOfPlans.push(target);
          else if (hit?.entity_type === "task") partOfTasks.push(target);
        }
      }
      edgeCache.set(id, { dependsOn, partOfTasks, partOfPlans });
    } catch {
      // Left uncached so a later poll retries. The client shows "not read yet"
      // rather than an empty dependency list it cannot vouch for.
    } finally {
      hydratingEdges.delete(id);
    }
  })();
}

function hydrate(id: string): void {
  if (entityCache.has(id) || hydrating.has(id)) return;
  hydrating.add(id);
  void (async () => {
    try {
      const e = (await neotomaGet(`/entities/${id}`)) as {
        entity_id?: string;
        entity_type?: string;
        canonical_name?: string | null;
        snapshot?: Record<string, unknown> | null;
      };
      if (e.entity_id && e.entity_type) {
        entityCache.set(id, {
          entity_id: e.entity_id,
          entity_type: e.entity_type,
          canonical_name: e.canonical_name ?? null,
          snapshot: e.snapshot ?? null,
        });
      }
    } catch {
      // Left uncached, so a later poll retries. One unreadable target must not
      // blank the rest of the session's work.
    } finally {
      hydrating.delete(id);
    }
  })();
}

async function fetchRelated(conversationId: string): Promise<RelatedEntity[]> {
  const rels = (await neotomaGet(`/entities/${conversationId}/relationships`)) as {
    outgoing?: { relationship_type?: string; target_entity_id?: string }[];
    incoming?: { relationship_type?: string; source_entity_id?: string }[];
  };

  // The neighbour id is on the far end of the edge, whichever end that is.
  const neighbours: { id: string; relationship_type: string }[] = [
    ...(rels.outgoing ?? []).map((r) => ({
      id: r.target_entity_id,
      relationship_type: r.relationship_type ?? "REFERS_TO",
    })),
    ...(rels.incoming ?? []).map((r) => ({
      id: r.source_entity_id,
      relationship_type: r.relationship_type ?? "REFERS_TO",
    })),
  ].filter((n): n is { id: string; relationship_type: string } => Boolean(n.id));

  // Dedupe by entity id BEFORE the cap, so a reciprocal pair costs one row
  // rather than two and cannot push a distinct entity past MAX_RELATED. The
  // conversation itself is excluded: a self-edge is not work the session did.
  const seen = new Set<string>([conversationId]);
  const unique: { id: string; relationship_type: string }[] = [];
  for (const n of neighbours) {
    if (seen.has(n.id)) continue;
    seen.add(n.id);
    unique.push(n);
    if (unique.length >= MAX_RELATED) break;
  }

  return unique.map((edge) => {
    const cached = entityCache.get(edge.id);
    if (!cached) hydrate(edge.id);

    // Only tasks need their own edges read: they are the rows whose ownership
    // state and topic membership the list has to compute. Reading edges for
    // every rendered_page and plan too would triple the background fetches to
    // answer a question nothing on screen asks.
    let edges: CachedEdges | null = null;
    if (cached?.entity_type === "task") {
      edges = edgeCache.get(edge.id) ?? null;
      if (!edges) hydrateEdges(edge.id);
    }

    return {
      entity_id: edge.id,
      entity_type: cached?.entity_type ?? null,
      canonical_name: cached?.canonical_name ?? null,
      relationship_type: edge.relationship_type,
      snapshot: cached?.snapshot ?? null,
      edges,
    };
  });
}

/**
 * ONE ENTITY, FOR THE IN-APP DETAIL VIEWS
 * ---------------------------------------
 * The dashboard's detail views (`#/entities/<id>`, and the slide-over sheet)
 * read this. It exists so an entity link stays INSIDE the app: the operator was
 * previously sent to `https://neotoma.…/entities/<id>`, which loses the session
 * he is working from and, without an access_token, 401s anyway. The bearer
 * token this middleware already holds reads the same entity server-side.
 *
 * THE CACHE IS THE POINT. `entityCache` is populated in the background by
 * `hydrate()` for every entity the current session refers to, so by the time the
 * operator clicks one it is usually already here and the response is instant.
 * A cold id — someone pasted `#/entities/ent_…` into the address bar, which the
 * route must support — falls through to the ~5s upstream GET, and lands in the
 * same cache so a reload or a second visit is instant too.
 *
 * `hydrate()` stores the full snapshot precisely so this route can serve a
 * complete detail view from cache rather than re-fetching what it already read.
 *
 * Relationships are fetched separately and NOT cached: unlike entity_type and
 * canonical_name, edges are added as a session works, and a detail view showing
 * a stale edge list would be quietly wrong about what an entity connects to.
 */
async function fetchEntity(id: string): Promise<unknown> {
  const cached = entityCache.get(id);

  // ~1.3s, and the only part that must be fresh. Failure here is not fatal:
  // an entity with unreadable edges still renders its own fields.
  const relationships = neotomaGet(`/entities/${id}/relationships`).catch(() => null);

  const entity = cached
    ? Promise.resolve(cached)
    : neotomaGet(`/entities/${id}`).then((raw) => {
        const e = raw as {
          entity_id?: string;
          entity_type?: string;
          canonical_name?: string | null;
          snapshot?: Record<string, unknown> | null;
        };
        const shaped: CachedEntity = {
          entity_id: e.entity_id ?? id,
          entity_type: e.entity_type ?? null,
          canonical_name: e.canonical_name ?? null,
          snapshot: e.snapshot ?? null,
        };
        // Feed the shared cache, so the session view's rows and any later visit
        // to this same id are both served without another 5s round trip.
        if (e.entity_id && e.entity_type) entityCache.set(id, shaped);
        return shaped;
      });

  const [resolved, rels] = await Promise.all([entity, relationships]);

  const edges = rels as {
    outgoing?: { relationship_type?: string; target_entity_id?: string }[];
    incoming?: { relationship_type?: string; source_entity_id?: string }[];
  } | null;

  // Edge targets are hydrated in the background exactly as the session view
  // does it, so a detail view's related rows name themselves on the next poll
  // rather than blocking this response for 5s per neighbour.
  const outgoing = (edges?.outgoing ?? [])
    .filter((r) => Boolean(r.target_entity_id))
    .slice(0, MAX_RELATED)
    .map((r) => {
      const target = r.target_entity_id as string;
      const hit = entityCache.get(target);
      if (!hit) hydrate(target);
      return {
        entity_id: target,
        entity_type: hit?.entity_type ?? null,
        canonical_name: hit?.canonical_name ?? null,
        relationship_type: r.relationship_type ?? "REFERS_TO",
        snapshot: hit?.snapshot ?? null,
        direction: "outgoing" as const,
      };
    });

  const incoming = (edges?.incoming ?? [])
    .filter((r) => Boolean(r.source_entity_id))
    .slice(0, MAX_RELATED)
    .map((r) => {
      const source = r.source_entity_id as string;
      const hit = entityCache.get(source);
      if (!hit) hydrate(source);
      return {
        entity_id: source,
        entity_type: hit?.entity_type ?? null,
        canonical_name: hit?.canonical_name ?? null,
        relationship_type: r.relationship_type ?? "REFERS_TO",
        snapshot: hit?.snapshot ?? null,
        direction: "incoming" as const,
      };
    });

  return { entity: resolved, outgoing, incoming, relationshipsFailed: edges === null };
}

/**
 * Every `session_digest` entity — the swarm's session history.
 *
 * Fetched whole (344 at time of writing) rather than paged, because the view's
 * most important job is showing the COVERAGE GAP: digests were written in a
 * burst and then stopped. A page of the most recent N would show an unbroken
 * run of activity and hide exactly the thing worth seeing.
 *
 * Ordered here by `last_observation_at` — when the digest ROW was written —
 * which is not the same as when the session RAN (`time_span_end`, a snapshot
 * field the server cannot sort on). The list re-sorts by coverage date client
 * side; see `sessions.ts`.
 */
/**
 * Every `workflow_definition` entity — the declared gate sequences.
 *
 * A small bounded set (8 at time of writing) like the agent roster, so there is
 * no pagination. Ordered client-side by project then workflow type; recency is
 * not the useful axis for a config table that changes a few times a year.
 */
function fetchWorkflows(limit: number): Promise<unknown> {
  return neotomaPost("/entities/query", {
    entity_type: "workflow_definition",
    limit,
    include_snapshots: true,
    sort_by: "last_observation_at",
    sort_order: "desc",
  });
}

/**
 * HOW MANY TASKS SIT IN EACH LIFECYCLE STATE.
 *
 * The eleven statuses the dispatcher's state machine writes
 * (`lib/daemon_runtime/task_lifecycle.py`). The list is FIXED here rather than
 * client-supplied, so this route cannot be pointed at an arbitrary filter.
 *
 * COUNTS WITHOUT ROWS. Each query asks for `limit: 1` and
 * `include_snapshots: false` and reads only the `total` the server reports for
 * the whole filtered set. That is what makes this cheap enough to be worth
 * showing: fetching 21,000 task snapshots to count them client-side is what
 * `/api/tasks?limit=200` already struggles with (it times out upstream at 30s
 * when Neotoma is loaded), whereas eleven count-only queries return in about a
 * second each and carry no snapshot payload at all.
 *
 * ONE FAILED COUNT IS NOT ELEVEN FAILED COUNTS. Each settles independently and
 * a rejection becomes `total: null` for that status only, which the view
 * renders as unknown rather than as zero. A status with genuinely no tasks and
 * a status whose count could not be read are different facts, and collapsing
 * the second into "0" would be a fabricated measurement.
 */
const LIFECYCLE_STATUSES = [
  "pending",
  "routed",
  "executing",
  "verified",
  "done",
  "failed",
  "blocked",
  "awaiting_approval",
  "awaiting_input",
  "declined",
  "superseded",
] as const;

/** Read the `total` for one status. Null on any failure — never a fake zero. */
async function countByStatus(status: string): Promise<number | null> {
  try {
    const body = (await neotomaPost("/entities/query", {
      entity_type: "task",
      limit: 1,
      include_snapshots: false,
      snapshot_filters: { status: { op: "eq", value: status } },
    })) as { total?: unknown };
    return typeof body.total === "number" ? body.total : null;
  } catch {
    return null;
  }
}

/**
 * The denominator: every `task` entity, however its status is spelled.
 *
 * MEASURABLY the slowest query here, and counter-intuitively so — the eleven
 * FILTERED counts return in about a second each while this UNFILTERED one
 * reliably exceeds the 30s interactive budget on a loaded instance. A status
 * filter appears to hit an index the bare type query does not. So this one gets
 * a longer budget of its own, the same way the schema sampler does, rather than
 * dropping the denominator and leaving the off-vocabulary remainder permanently
 * unknown.
 *
 * Still fails to `null` rather than throwing: the eleven per-status counts are
 * the substance of the view, and losing the denominator must not cost them.
 */
async function countAllTasks(): Promise<number | null> {
  try {
    const body = (await neotomaPost(
      "/entities/query",
      { entity_type: "task", limit: 1, include_snapshots: false },
      90_000,
    )) as { total?: unknown };
    return typeof body.total === "number" ? body.total : null;
  } catch {
    return null;
  }
}

/**
 * TRUE FACET COUNTS — the bucket chips as a fact about the BACKLOG.
 *
 * THE BUG THIS EXISTS TO KILL. The chips used to count the rows the page had
 * loaded: `counts` in `TaskList.tsx` folded over `loaded`, so "Pending 105"
 * described the newest 200 rows and read as a property of the 5,566 open
 * tasks. Measured against production on 2026-09-02 the real figures are
 * Pending 4,912 and Blocked 213 — the Pending chip understated by 47x. The
 * qualifier "within the 200 loaded" was present and true, and still the
 * numbers beside it were the part anyone acted on.
 *
 * It compounds with the sort. The page is `last_observation_at desc`, so the
 * sample is not merely small but BIASED TOWARD RECENT WORK: only 760 of the
 * 5,566 open tasks were touched since 2026-08-01, which means a backlog that
 * is overwhelmingly stale renders as a tidy queue of current work. A count
 * over that sample is not a small-sample estimate of the backlog; it is a
 * measurement of a different population.
 *
 * WHY PER-STATUS AND SUMMED, rather than one query per bucket. Buckets are a
 * CLIENT vocabulary — `toBucket()` in `src/tasks.ts` folds eleven live status
 * spellings onto five labels — and `snapshot_filters` has no OR across values
 * beyond `in`. Counting each status once and summing here keeps ONE definition
 * of the mapping (the client's) and costs one cheap count-only query per
 * status, the same shape `/api/lifecycle` already uses. Duplicating the fold
 * server-side would let the two drift, and a chip whose count disagrees with
 * its own filter is worse than no count.
 *
 * WHY NOT the filtered `total` on the list query: that saturates at
 * FILTERED_TOTAL_CEILING (see `src/taskCount.ts`), so it cannot be trusted for
 * any bucket that might exceed 10,000. These are per-status counts well under
 * the ceiling, and the open-set sum is checked against it below.
 */
const FACET_STATUSES = [
  "pending",
  "open",
  "todo",
  "in_progress",
  "active",
  "routed",
  "executing",
  "blocked",
  "awaiting_input",
  "awaiting_approval",
  "awaiting_release_confirmation",
] as const;

/**
 * Per-status counts across ALL open tasks, plus the open-set total.
 *
 * `statuses` is a map of status -> count-or-null; the client folds it through
 * its own `toBucket()`. A status that failed to count is null, never 0, for
 * the reason `countByStatus` documents: "no tasks" and "could not ask" are
 * different facts and collapsing them fabricates a measurement.
 *
 * `total` is the real open-set aggregate — an `in` query over the same
 * vocabulary, which upstream answers with a genuine count (5,566 measured, and
 * exactly equal to the sum of the parts, which is the check that the
 * vocabulary is complete). `complete` says whether every part was measured, so
 * the client knows whether the chips add up to the whole or merely to what
 * could be read.
 */
async function fetchFacets(): Promise<unknown> {
  const [entries, openTotal] = await Promise.all([
    Promise.all(
      FACET_STATUSES.map(async (status) => [status, await countByStatus(status)] as const),
    ),
    (async () => {
      try {
        const body = (await neotomaPost("/entities/query", {
          entity_type: "task",
          limit: 1,
          include_snapshots: false,
          snapshot_filters: { status: { op: "in", value: [...FACET_STATUSES] } },
        })) as { total?: unknown };
        return typeof body.total === "number" ? body.total : null;
      } catch {
        return null;
      }
    })(),
  ]);

  const statuses = Object.fromEntries(entries);
  const complete = entries.every(([, v]) => typeof v === "number");

  return {
    statuses,
    total: openTotal,
    complete,
    /**
     * Whether the parts reconcile with the whole. When both are known and
     * agree, the chips provably account for every open task; when they differ,
     * a status spelling exists that `FACET_STATUSES` does not name, and the
     * client must not present the chips as exhaustive.
     */
    reconciled:
      complete && typeof openTotal === "number"
        ? entries.reduce((sum, [, v]) => sum + (v ?? 0), 0) === openTotal
        : null,
  };
}

async function fetchLifecycle(): Promise<unknown> {
  const [counts, totalTasks] = await Promise.all([
    Promise.all(
      LIFECYCLE_STATUSES.map(async (status) => ({
        status,
        total: await countByStatus(status),
      })),
    ),
    countAllTasks(),
  ]);

  /**
   * Tasks whose status is none of the eleven — `open`, `todo`, `completed`,
   * `in_progress` and other legacy spellings the graph accepts because the
   * schema types `status` as a bare string with no enum.
   *
   * Derived by subtraction rather than enumerated, since the set of ad-hoc
   * values is open-ended. Null unless every input is known: subtracting a
   * partial sum would UNDERSTATE nothing and OVERSTATE the remainder, which is
   * the direction that invents a problem.
   */
  const allKnown = counts.every((c) => typeof c.total === "number");
  const offVocabulary =
    allKnown && typeof totalTasks === "number"
      ? Math.max(0, totalTasks - counts.reduce((sum, c) => sum + (c.total ?? 0), 0))
      : null;

  return { counts, totalTasks, offVocabulary };
}

/**
 * EVERY TASK ASSIGNED TO ONE AGENT.
 *
 * Answers "what is this agent responsible for right now" on the agent's own
 * page. Filtered SERVER-SIDE on `assigned_to`: the alternative is pulling the
 * task table and filtering here, and there are 20,922 tasks, so that is not an
 * alternative.
 *
 * SORTING IS REAL, NOT DECORATIVE. `sort`/`order_by` are accepted by Neotoma and
 * then SILENTLY IGNORED — the rows come back in entity-id order, which looks
 * plausibly sorted and is not. Only `sort_by`/`sort_order` actually order the
 * result. Verified on this query: the first rows come back 2026-08-31, descending.
 *
 * CASE MATTERS. `assigned_to` is stored as written, and production holds both
 * `cicada` and `Bombycilla` — so the filter is exact and the caller passes the
 * value as stored rather than a normalized one.
 */
function fetchAssignedTasks(assignedTo: string, limit: number): Promise<unknown> {
  return neotomaPost("/entities/query", {
    entity_type: "task",
    limit,
    include_snapshots: true,
    snapshot_filters: { assigned_to: { op: "eq", value: assignedTo } },
    sort_by: "last_observation_at",
    sort_order: "desc",
  });
}

function fetchSessions(limit: number): Promise<unknown> {
  return neotomaPost("/entities/query", {
    entity_type: "session_digest",
    limit,
    include_snapshots: true,
    sort_by: "last_observation_at",
    sort_order: "desc",
  });
}

/* ────────────────────────────────────────────────────────────────────────────
 * SCHEMA DRIFT — what the registry DECLARES vs what production actually HOLDS
 * ────────────────────────────────────────────────────────────────────────────
 *
 * Three upstream sources, each doing one job:
 *
 *   GET /stats            every type's entity count, plus the instance totals,
 *                         in ONE call. 739 types with entities at time of
 *                         writing. This is the only cheap source of counts —
 *                         there is no per-type count endpoint.
 *   GET /schemas/<type>   the DECLARED schema for one type: its fields, their
 *                         types, and their descriptions.
 *   POST /entities/query  a SAMPLE of real entities, which is the only way to
 *                         learn which declared fields are actually populated
 *                         and which values a status-ish field really holds.
 *
 * WHY A SAMPLE, AND WHY IT IS LABELLED AS ONE
 * -------------------------------------------
 * There is no aggregate endpoint for "which fields of this type are non-null".
 * `task` alone has 21,061 entities and no amount of paging is going to happen
 * inside a dashboard request. So the drift engine reads the most recent N and
 * says so on screen, every time, next to the number it derived. A field
 * reported dead is dead IN THE SAMPLE — which is a much weaker claim than
 * "dead", and the UI must never round it up to one.
 *
 * WHY NONE OF THIS IS ON THE REQUEST PATH
 * ---------------------------------------
 * Each type needs a schema GET plus a sample query, and the canonical set is 16
 * types — call it 32 upstream calls at ~1-5s each. Awaited inline, that is a
 * request measured in minutes, well past the client's poll interval, so every
 * poll would pile onto the one before it and the view would never leave its
 * skeleton. This is the exact failure the session view already documents.
 *
 * So it follows the pattern the rest of this file established: a response is
 * assembled from whatever the cache holds, missing types are hydrated in the
 * BACKGROUND, and the client renders a type with no analysis yet as pending
 * rather than as a type with no drift. `analyzed: false` is not `drift: none`.
 */

/**
 * How many entities are sampled per type. Surfaced in the UI verbatim.
 *
 * SAMPLE SIZE CHANGES THE ANSWER, so it is stated on screen next to every
 * figure derived from it. Measured on `task`: 150 entities surface 17 distinct
 * populated fields, 400 surface 49 — because rare fields (`amount`, `repo`,
 * `payment_approved`) sit on a single entity each and a small window simply
 * does not contain them. Neither number is wrong; they answer "populated in
 * the most recent N", which is the only question a sample can answer.
 *
 * 400 is chosen against the API's hard ceiling: `limit` may not exceed 500
 * when `include_snapshots` is true (verified — 600 is rejected 400
 * VALIDATION_INVALID_FORMAT). It leaves headroom under that cap and still
 * costs ~3s per type.
 */
const SCHEMA_SAMPLE = 400;

/**
 * A SMALLER sample for the few types where 400 snapshots is too slow to finish.
 *
 * MEASURED, on an otherwise idle connection: `conversation_message` (57,600
 * entities) took 28.4s for 400 snapshots and `task` (21,066) took 13.2s, and
 * upstream latency for both is variable — an earlier run of the same
 * `conversation_message` query returned in 14.5s. Queued behind other large
 * types they repeatedly failed to finish, so these two were the ones the tab
 * could never analyse: the biggest, most interesting types.
 *
 * Cutting their window is the honest trade. Sample size is a STATED parameter
 * printed beside every figure derived from it, so a smaller window for these
 * types is visible on screen rather than hidden — whereas a type that never
 * completes shows nothing at all. Everything else keeps the full 400.
 */
const SCHEMA_SAMPLE_OVERRIDE: Record<string, number> = {
  conversation_message: 150,
  task: 250,
};

function sampleSizeFor(entityType: string): number {
  return SCHEMA_SAMPLE_OVERRIDE[entityType] ?? SCHEMA_SAMPLE;
}

/**
 * Values that look like a lifecycle state, whose drift is worth computing.
 *
 * Restricted deliberately. Comparing free-text fields against their
 * descriptions would produce noise; these are the fields whose declared value
 * set is a closed list, so a value outside it is a real finding rather than a
 * formatting difference.
 */
const ENUMISH_FIELDS = ["status", "state", "priority", "severity", "verification_state"];

interface FieldDrift {
  field: string;
  /** Values the schema description declares, parsed from `a | b | c`. */
  declared: string[];
  /** Values actually present in the sample, with how many carry each. */
  observed: { value: string; count: number }[];
  /** Observed values that no declared value covers — the drift itself. */
  undeclared: { value: string; count: number }[];
}

interface SchemaAnalysis {
  entityType: string;
  /** Declared field names, from the registry. */
  declaredFields: number;
  /**
   * Declared fields carrying a non-empty value in at least one sampled entity.
   * Everything else is dead IN THE SAMPLE — see the header.
   */
  populatedFields: number;
  /** Declared but unpopulated, named so the finding is inspectable. */
  deadFields: string[];
  /** Populated fields that the schema does not declare at all. */
  undeclaredFields: string[];
  /** How many entities the sample actually returned. */
  sampled: number;
  /** Enum-ish fields whose live values escape their declaration. */
  drift: FieldDrift[];
  /** Most recent `last_observation_at` across the sample — staleness, for config. */
  lastTouched: string | null;
  /** A compact value summary for config types: the newest entity's key fields. */
  valueSummary: { field: string; value: string }[];
  schemaVersion: string | null;
  description: string | null;
  /** Set when the type has a schema row but no entities, or the fetch failed. */
  note: string | null;
}

const schemaCache = new Map<string, SchemaAnalysis>();
const hydratingSchema = new Set<string>();

/**
 * SCHEMA HYDRATION RUNS IN A QUEUE, NOT ALL AT ONCE.
 *
 * MEASURED, NOT ASSUMED. A `task` sample takes ~7s on its own. Firing all 27
 * canonical+config types concurrently — which is what an unqueued
 * `hydrateSchema` per type does — inflates every one of them to 19-25s through
 * upstream contention, and adding the other views' polls on top pushes the
 * biggest types past the 30s timeout entirely. That is what made `task`,
 * `issue`, `conversation`, `conversation_message`, and `harness_event` — the
 * five HIGHEST-COUNT types, i.e. the most interesting ones — the exact five
 * that came back unmeasured.
 *
 * Three at a time keeps each request near its standalone latency, so the whole
 * set hydrates in a couple of polls without any single query starving. The
 * queue is FIFO and deduped by `hydratingSchema`, so a repeated poll does not
 * enqueue the same type twice.
 */
const SCHEMA_CONCURRENCY = 3;
const schemaQueue: (() => Promise<void>)[] = [];
let schemaActive = 0;

function pumpSchemaQueue(): void {
  while (schemaActive < SCHEMA_CONCURRENCY && schemaQueue.length) {
    const job = schemaQueue.shift() as () => Promise<void>;
    schemaActive++;
    void job().finally(() => {
      schemaActive--;
      pumpSchemaQueue();
    });
  }
}

/**
 * Parse a declared value set out of a field description.
 *
 * Neotoma has no enum type: a closed value set is written into the DESCRIPTION,
 * as `pending_review | approved | rejected | redirected`. That is the only
 * machine-readable declaration there is, so it is what drift is measured
 * against. A description that is prose rather than a pipe list yields nothing
 * and the field is skipped — no declaration means no drift to compute, which is
 * different from "no drift".
 */
function declaredValues(description: string | undefined): string[] {
  if (!description || !description.includes("|")) return [];
  const parts = description
    .split("|")
    .map((p) => p.trim().replace(/^["'`]|["'`]$/g, ""))
    .filter(Boolean);
  // Every part must look like an identifier, or this was prose containing a
  // pipe rather than a value list.
  if (parts.length < 2) return [];
  if (!parts.every((p) => /^[a-z0-9_]+$/i.test(p))) return [];
  return parts;
}

/** Non-empty means the field carries information — `[]`, `{}`, and "" do not. */
function populated(v: unknown): boolean {
  if (v === null || v === undefined || v === "") return false;
  if (Array.isArray(v)) return v.length > 0;
  if (typeof v === "object") return Object.keys(v as object).length > 0;
  return true;
}

/** Short, printable rendering of a snapshot value for the config summary. */
function summarize(v: unknown): string {
  if (typeof v === "string") return v.length > 120 ? `${v.slice(0, 120)}…` : v;
  if (Array.isArray(v)) return `${v.length} item${v.length === 1 ? "" : "s"}`;
  if (v && typeof v === "object") {
    const keys = Object.keys(v as object);
    return `{${keys.slice(0, 4).join(", ")}${keys.length > 4 ? ", …" : ""}}`;
  }
  return String(v);
}

/**
 * Analyze ONE entity type: its declared schema against a sample of live rows.
 *
 * Runs in the background only. A failure is left uncached so a later poll
 * retries, rather than caching an empty analysis that would render as "no
 * drift found" — a claim this function would not have earned.
 */
function hydrateSchema(entityType: string): void {
  if (schemaCache.has(entityType) || hydratingSchema.has(entityType)) return;
  hydratingSchema.add(entityType);

  schemaQueue.push(async () => {
    try {
      /**
       * The two legs fail INDEPENDENTLY and are reported independently.
       *
       * They used to share one `.catch(() => null)` each, and the sample's null
       * was then indistinguishable from "this type has no entities" — so a
       * timed-out query on `task` cached `populatedFields: 0` against 21,066
       * live entities and the table rendered a confident "0 of 83". A failed
       * measurement that renders as a real value is the exact failure this tab
       * exists to expose, so the null now travels as a FAILURE and the client
       * prints "not measured" instead of a number derived from nothing.
       */
      const [schemaFetch, sampleRaw] = await Promise.all([
        fetchSchema(entityType),
        neotomaPost(
          "/entities/query",
          {
            entity_type: entityType,
            limit: sampleSizeFor(entityType),
            include_snapshots: true,
            sort_by: "last_observation_at",
            sort_order: "desc",
          },
          // Measured: `conversation_message` (57,600 entities) takes ~14.5s for
          // 400 snapshots on an idle connection, and queued behind two other
          // large types it lands well past 30s. 90s is chosen to cover the
          // worst type under contention rather than to be generous.
          90_000,
        ).catch(() => null),
      ]);

      // A null sample means the query did not come back — NOT that the type is
      // empty. Leave the whole analysis uncached so the next poll retries it,
      // rather than caching a zero that looks measured.
      if (sampleRaw === null) {
        return;
      }

      // Same rule on the schema leg. A fetch that FAILED tells us nothing about
      // whether a schema exists, and caching it would freeze that non-answer as
      // `declaredFields: 0` — the cache is write-once, so the next poll would
      // find a hit and never retry. Only a 404 may be recorded as absence.
      if (schemaFetch.kind === "failed") {
        return;
      }

      const schema = (schemaFetch.kind === "ok" ? schemaFetch.schema : null) as {
        schema_version?: string;
        schema_definition?: {
          description?: string;
          fields?: Record<string, { type?: string; description?: string }>;
        };
      } | null;

      const sample = sampleRaw as {
        entities?: {
          snapshot?: Record<string, unknown> | null;
          last_observation_at?: string | null;
        }[];
      } | null;

      const fields = schema?.schema_definition?.fields ?? {};
      const declaredNames = Object.keys(fields);
      const entities = sample?.entities ?? [];

      // Which declared fields ever carry a value, and which values the
      // enum-ish ones actually take.
      const seenFields = new Set<string>();
      const values = new Map<string, Map<string, number>>();
      let lastTouched: string | null = null;

      for (const e of entities) {
        if (e.last_observation_at && (!lastTouched || e.last_observation_at > lastTouched)) {
          lastTouched = e.last_observation_at;
        }
        for (const [k, v] of Object.entries(e.snapshot ?? {})) {
          if (!populated(v)) continue;
          seenFields.add(k);
          if (ENUMISH_FIELDS.includes(k) && typeof v === "string") {
            const bucket = values.get(k) ?? new Map<string, number>();
            bucket.set(v, (bucket.get(v) ?? 0) + 1);
            values.set(k, bucket);
          }
        }
      }

      const drift: FieldDrift[] = [];
      for (const [field, counts] of values) {
        const declared = declaredValues(fields[field]?.description);
        if (!declared.length) continue;
        const observed = [...counts.entries()]
          .map(([value, count]) => ({ value, count }))
          .sort((a, b) => b.count - a.count);
        const undeclared = observed.filter((o) => !declared.includes(o.value));
        drift.push({ field, declared, observed, undeclared });
      }

      // A compact "what is this configured to" line for the config section:
      // the newest entity's own populated fields, minus the bulky prose ones.
      const newest = entities[0]?.snapshot ?? {};
      const valueSummary = Object.entries(newest)
        .filter(([k, v]) => populated(v) && !["body", "prompt_markdown"].includes(k))
        .slice(0, 6)
        .map(([field, v]) => ({ field, value: summarize(v) }));

      schemaCache.set(entityType, {
        entityType,
        declaredFields: declaredNames.length,
        populatedFields: declaredNames.filter((f) => seenFields.has(f)).length,
        deadFields: declaredNames.filter((f) => !seenFields.has(f)),
        undeclaredFields: [...seenFields].filter((f) => !declaredNames.includes(f)),
        sampled: entities.length,
        drift,
        lastTouched,
        valueSummary,
        schemaVersion: schema?.schema_version ?? null,
        description: schema?.schema_definition?.description ?? null,
        note: !schema
          ? "No schema registered for this type."
          : entities.length === 0
            ? "No entities of this type — nothing to sample."
            : null,
      });
    } catch {
      // Left uncached deliberately: a cached empty analysis would render as
      // "no drift", which is precisely the false clean bill this tab exists to
      // prevent. The next poll retries.
    } finally {
      hydratingSchema.delete(entityType);
    }
  });
  pumpSchemaQueue();
}

/** The types the tab analyzes in full. Order is significance, not alphabet. */
const CANONICAL_TYPES = [
  "task",
  "checkpoint_brief",
  "issue",
  "conversation",
  "conversation_message",
  "harness_event",
  "agent_definition",
  "session_digest",
  "plan",
  "project",
  "escalation",
  "daemon_report",
  "execution_policy",
  "workflow_definition",
  "rendered_page",
  "rendered_page_template",
];

/** Behaviour-governing types, where a stale value does quiet damage. */
const CONFIG_TYPES = [
  "execution_policy",
  "agent_policy",
  "task_policy",
  "locale_profile",
  "vendor_binding",
  "channel_config",
  "deployment_configuration",
  "swarm_roster",
  "operator_profile",
  "priority_rubric",
  "brand_voice",
  "payment_profile",
];

/**
 * THE SCHEMAS RESPONSE.
 *
 * `/stats` is awaited — it is one call, it carries every count, and without it
 * there is no page. Everything else is served from cache and hydrated behind
 * the response, so the first poll returns counts with `analyzed: false` and the
 * analysis fills in over the next few polls.
 */
async function fetchSchemas(): Promise<unknown> {
  const stats = (await neotomaGet("/stats")) as {
    entities_by_type?: Record<string, number>;
    total_entities?: number;
    total_relationships?: number;
    total_observations?: number;
    last_updated?: string;
  };

  const counts = stats.entities_by_type ?? {};
  const wanted = [...new Set([...CANONICAL_TYPES, ...CONFIG_TYPES])];
  for (const t of wanted) hydrateSchema(t);

  const shape = (t: string) => {
    const a = schemaCache.get(t) ?? null;
    return { entityType: t, count: counts[t] ?? 0, analyzed: Boolean(a), analysis: a };
  };

  /**
   * THE TAIL — every type NOT in the two curated lists, bucketed rather than
   * hidden. ~870 of them, and the point of the section is that they exist at
   * all, so nothing here is dropped: the buckets partition the whole remainder
   * and their counts add up to it.
   */
  const rest = Object.entries(counts)
    .filter(([t]) => !wanted.includes(t))
    .map(([entityType, count]) => ({ entityType, count }));

  const singles = rest.filter((r) => r.count === 1);
  const cliCorrections = singles.filter((r) => r.entityType.startsWith("cli_correction_"));
  const crossLayer = singles.filter((r) => r.entityType.startsWith("cross_layer_schema_"));
  const otherSingles = singles.filter(
    (r) =>
      !r.entityType.startsWith("cli_correction_") && !r.entityType.startsWith("cross_layer_schema_"),
  );
  const low = rest.filter((r) => r.count > 1 && r.count < 10);
  const mid = rest.filter((r) => r.count >= 10 && r.count < 1000);
  const high = rest.filter((r) => r.count >= 1000);

  const bucket = (
    key: string,
    label: string,
    blurb: string,
    rows: { entityType: string; count: number }[],
  ) => ({
    key,
    label,
    blurb,
    types: rows.length,
    entities: rows.reduce((n, r) => n + r.count, 0),
    // Enough to inspect the bucket without shipping 900 rows to the client.
    sample: [...rows].sort((a, b) => b.count - a.count).slice(0, 40),
  });

  return {
    sampleSize: SCHEMA_SAMPLE,
    totals: {
      entities: stats.total_entities ?? 0,
      relationships: stats.total_relationships ?? 0,
      observations: stats.total_observations ?? 0,
      types: Object.keys(counts).length,
      lastUpdated: stats.last_updated ?? null,
    },
    canonical: CANONICAL_TYPES.map(shape),
    config: CONFIG_TYPES.map(shape),
    buckets: [
      bucket(
        "cli_correction",
        "cli_correction_* — one entity each",
        "Registry pollution: a schema registered per correction, never reused.",
        cliCorrections,
      ),
      bucket(
        "cross_layer",
        "cross_layer_schema_* — one entity each",
        "Machine-generated schema probes, same pattern as above.",
        crossLayer,
      ),
      bucket(
        "singleton",
        "Other single-entity types",
        "One entity, one type — a schema that was registered and then never used again.",
        otherSingles,
      ),
      bucket("low", "Low-count types (2–9)", "Small enough that a type may be premature.", low),
      bucket("mid", "Mid-count types (10–999)", "Types in real but modest use.", mid),
      bucket("high", "High-count types (1000+)", "Bulk types outside the canonical set.", high),
    ],
  };
}

/**
 * HOW STALE IS THE COMMITTED CODE SCAN?
 *
 * `src/codeUsage.ts` is generated at a commit and then sits in the tree. That
 * it is static is a permanent property and therefore says nothing — a banner
 * repeating it every load conveys no state and stops being read. What DOES
 * change, and is actionable, is whether the repo has moved on since: if HEAD
 * has advanced past the scan commit, the reader/writer columns describe code
 * that no longer exists and the fix is one command.
 *
 * Only the dev server can answer that, because only it can run git — hence a
 * route rather than a build-time constant. Read-only, like everything here:
 * `rev-list --count` and `rev-parse` observe the repo and cannot alter it.
 *
 * Verdicts, and why each is distinct:
 *   current  — HEAD is the scan commit. Show nothing louder than a tick.
 *   behind   — HEAD moved; `behindBy` commits of drift, regeneration is due.
 *   unknown  — git could not answer (no repo, commit garbage-collected, git
 *              absent). NOT reported as stale: an unanswerable question must
 *              not look identical to a known-bad answer, or the real staleness
 *              signal gets discounted the first time this misfires.
 */
function scanFreshness(scanCommit: string): {
  status: "current" | "behind" | "unknown";
  head: string | null;
  scanCommit: string;
  behindBy: number | null;
  reason?: string;
} {
  const base = { scanCommit, head: null as string | null, behindBy: null as number | null };

  // The scan commit is interpolated into a git argument. It comes from a file
  // in this repo rather than from a request, but validate anyway: anything but
  // a hex sha is not a commit and must not reach git.
  if (!/^[0-9a-f]{7,40}$/.test(scanCommit)) {
    return { ...base, status: "unknown", reason: "Scan commit is not a valid revision." };
  }

  const git = (args: string[]): string =>
    execFileSync("git", ["-C", REPO_ROOT, ...args], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
      timeout: 5_000,
    }).trim();

  try {
    const head = git(["rev-parse", "--short", "HEAD"]);
    // Does the scan commit still exist here? A rebased or garbage-collected
    // commit cannot be compared, and guessing "behind" from its absence would
    // be inventing a number.
    git(["rev-parse", "--verify", `${scanCommit}^{commit}`]);
    const behindBy = Number(git(["rev-list", "--count", `${scanCommit}..HEAD`]));
    if (!Number.isFinite(behindBy)) {
      return { ...base, head, status: "unknown", reason: "Commit distance was not a number." };
    }
    return {
      scanCommit,
      head,
      behindBy,
      status: behindBy === 0 ? "current" : "behind",
    };
  } catch {
    // Generic on purpose: git's stderr can carry absolute paths, and this
    // response goes to the browser.
    return { ...base, status: "unknown", reason: "Could not read the repository." };
  }
}

export function neotomaProxy(): Plugin {
  return {
    name: "neotoma-task-proxy",
    configureServer(server) {
      /**
       * GET /api/tasks — one page of tasks, optionally narrowed server-side.
       *
       *   ?limit=200            page size
       *   ?scope=open           restrict to OPEN_TASK_STATUSES
       *   ?status=pending       exact status (overrides scope)
       *   ?assigned_to=cicada   exact owner, as stored
       *   ?priority=high        exact priority
       *   ?stale_days=30        untouched for at least N days — "stalled"
       *
       * NO PARAMS = THE ORIGINAL BEHAVIOUR, byte-for-byte on the keys that
       * existed before. Other callers already depend on this route's shape, so
       * narrowing is strictly opt-in and the two new keys (`total_saturated`,
       * `filters_applied`) are additive.
       */
      server.middlewares.use("/api/tasks", async (req, res) => {
        const params = new URL(req.url ?? "/", "http://localhost").searchParams;
        const limit = Math.min(Number(params.get("limit")) || 200, 1000);

        res.setHeader("Content-Type", "application/json");
        res.setHeader("Cache-Control", "no-store");

        /**
         * Allowlist rather than escape. These values are forwarded to Neotoma
         * inside a JSON body, so there is no quoting hazard to escape — the
         * point is to refuse junk here instead of spending a 10s upstream
         * query on it, and to keep this route from being usable as a generic
         * proxy for arbitrary filter values.
         *
         * A REJECTED FILTER IS AN ERROR, NOT A NO-OP. Dropping an unparseable
         * value and answering anyway would return the UNFILTERED 20,990-task
         * set under a heading naming the filter the caller asked for — a
         * silently widened result read as a narrowed one. That is the same
         * failure `sort`/`order_by` cause upstream (accepted, ignored, answered
         * with something plausible), and it is worse here because the count
         * shown beside the rows would be the count of a different question.
         */
        const bad = ["status", "assigned_to", "priority"].filter((key) => {
          const value = params.get(key);
          return value !== null && !/^[A-Za-z0-9_.:-]{1,64}$/.test(value);
        });
        /**
         * `stale_days` is validated separately because it is a NUMBER, and the
         * allowlist regex above would accept "007" and "1-2" alike. Rejecting
         * here keeps the rule this route already holds: a filter that cannot be
         * honoured is a 400, never a silently widened answer.
         */
        if (params.get("stale_days") !== null && staleBefore(params.get("stale_days")) === null) {
          bad.push("stale_days");
        }
        if (bad.length) {
          res.statusCode = 400;
          res.end(JSON.stringify({ error: `Malformed filter value: ${bad.join(", ")}.` }));
          return;
        }

        try {
          res.end(
            JSON.stringify(
              await fetchTasks(limit, {
                status: params.get("status"),
                openOnly: params.get("scope") === "open",
                assignedTo: params.get("assigned_to"),
                priority: params.get("priority"),
                staleBefore: staleBefore(params.get("stale_days")),
              }),
            ),
          );
        } catch (err) {
          res.statusCode = 502;
          res.end(JSON.stringify({ error: (err as Error).message }));
        }
      });

      /**
       * GET /api/task-total — how many `task` entities exist, full stop.
       *
       * The honest denominator for "N of what?", and the one total on this
       * route set that is a REAL aggregate: no `snapshot_filters`, so Neotoma
       * answers with `countVisibleEntities` rather than the saturating
       * fetch-and-length path (see `FILTERED_TOTAL_CEILING`).
       *
       * Separate from `/api/tasks` on purpose. It is count-only
       * (`include_snapshots: false`) and needs the longer 90s budget, because
       * the UNFILTERED count is reliably the slowest query here — a status
       * filter hits an index the bare type query does not. Folding it into the
       * page fetch would put the slowest query on the critical path of the
       * fastest one.
       *
       * `{ total: null }` on any failure — `countAllTasks` never invents a
       * zero, and the client prints "not measured" rather than a number.
       */
      server.middlewares.use("/api/task-total", async (_req, res) => {
        res.setHeader("Content-Type", "application/json");
        res.setHeader("Cache-Control", "no-store");
        res.end(JSON.stringify({ total: await countAllTasks() }));
      });

      server.middlewares.use("/api/sessions", async (req, res) => {
        const limit = Math.min(
          Number(new URL(req.url ?? "/", "http://localhost").searchParams.get("limit")) || 400,
          1000,
        );
        res.setHeader("Content-Type", "application/json");
        res.setHeader("Cache-Control", "no-store");
        try {
          const body = (await fetchSessions(limit)) as Record<string, unknown>;
          // `live` is filesystem-derived, not Neotoma data, and the browser
          // cannot read ~/.claude. It is attached here as a SEPARATE key so the
          // client never mistakes it for a digest — see `liveSession()`.
          res.end(JSON.stringify({ ...body, live: liveSession() }));
        } catch (err) {
          res.statusCode = 502;
          res.end(JSON.stringify({ error: (err as Error).message }));
        }
      });

      /**
       * The live session AS NEOTOMA HOLDS IT: the `conversation` entity whose
       * `conversation_id` is the harness session uuid, plus every entity that
       * conversation points at.
       *
       * `live` still rides along, but only as the lookup key and as the
       * fallback's evidence. When `conversation` comes back non-null the client
       * shows a Neotoma-sourced session and drops the filesystem caveat.
       */
      server.middlewares.use("/api/conversation", async (_req, res) => {
        res.setHeader("Content-Type", "application/json");
        res.setHeader("Cache-Control", "no-store");
        try {
          const live = liveSession();
          if (!live) {
            res.end(JSON.stringify({ live: null, conversation: null, related: [] }));
            return;
          }

          const found = (await fetchConversation(live.sessionId)) as {
            entities?: { entity_id: string; snapshot?: Record<string, unknown> | null }[];
          };
          const conversation = found.entities?.[0] ?? null;
          const related = conversation ? await fetchRelated(conversation.entity_id) : [];

          res.end(JSON.stringify({ live, conversation, related }));
        } catch (err) {
          res.statusCode = 502;
          res.end(JSON.stringify({ error: (err as Error).message }));
        }
      });

      /**
       * One entity, for the in-app detail views. READ-ONLY like every route
       * here: a GET, forwarded as a GET, with no body accepted.
       *
       * The id is validated against the `ent_<hex>` shape before it reaches
       * the upstream path — it is interpolated into a URL, so anything else is
       * rejected here rather than forwarded.
       */
      server.middlewares.use("/api/entity", async (req, res) => {
        const id = new URL(req.url ?? "/", "http://localhost").searchParams.get("id") ?? "";
        res.setHeader("Content-Type", "application/json");
        res.setHeader("Cache-Control", "no-store");

        if (!/^ent_[0-9a-f]+$/.test(id)) {
          res.statusCode = 400;
          res.end(JSON.stringify({ error: "Malformed entity id." }));
          return;
        }

        try {
          res.end(JSON.stringify(await fetchEntity(id)));
        } catch (err) {
          res.statusCode = 502;
          res.end(JSON.stringify({ error: (err as Error).message }));
        }
      });

      /**
       * GET /api/observations?id=… — one entity's APPEND-ONLY HISTORY.
       *
       * Neotoma records every field change as a timestamped observation, so
       * this is the only place the dashboard can show what actually happened to
       * a task rather than just its current values. A task that went
       * created -> status -> result shows those three moments in order; a task
       * written once and never touched shows exactly that, which is itself the
       * finding.
       *
       * Verified against the live API: `/entities/<id>/observations` returns
       * `{observations, total, limit, offset}`. Read-only, like every route here.
       */
      server.middlewares.use("/api/observations", async (req, res) => {
        const id = new URL(req.url ?? "/", "http://localhost").searchParams.get("id") ?? "";
        res.setHeader("Content-Type", "application/json");
        res.setHeader("Cache-Control", "no-store");

        // Same allowlist as /api/entity: the id is interpolated into a URL.
        if (!/^ent_[0-9a-f]+$/.test(id)) {
          res.statusCode = 400;
          res.end(JSON.stringify({ error: "Malformed entity id." }));
          return;
        }

        try {
          res.end(JSON.stringify(await neotomaGet(`/entities/${id}/observations?limit=60`)));
        } catch (err) {
          res.statusCode = 502;
          res.end(JSON.stringify({ error: (err as Error).message }));
        }
      });

      server.middlewares.use("/api/workflows", async (req, res) => {
        const limit = Math.min(
          Number(new URL(req.url ?? "/", "http://localhost").searchParams.get("limit")) || 100,
          1000,
        );
        res.setHeader("Content-Type", "application/json");
        res.setHeader("Cache-Control", "no-store");
        try {
          res.end(JSON.stringify(await fetchWorkflows(limit)));
        } catch (err) {
          res.statusCode = 502;
          res.end(JSON.stringify({ error: (err as Error).message }));
        }
      });

      /**
       * GET /api/lifecycle — how many tasks sit in each lifecycle state.
       *
       * Takes no parameters: the eleven statuses are fixed in
       * `LIFECYCLE_STATUSES` rather than client-supplied, so this cannot be
       * pointed at an arbitrary filter. Count-only queries, no snapshots.
       */
      /**
       * GET /api/facets — true bucket counts across every open task.
       *
       * Takes no parameters: the status vocabulary is fixed in
       * `FACET_STATUSES`, so this cannot be pointed at an arbitrary filter.
       * Count-only queries, no snapshots.
       */
      server.middlewares.use("/api/facets", async (_req, res) => {
        res.setHeader("Content-Type", "application/json");
        res.setHeader("Cache-Control", "no-store");
        try {
          res.end(JSON.stringify(await fetchFacets()));
        } catch (err) {
          res.statusCode = 502;
          res.end(JSON.stringify({ error: (err as Error).message }));
        }
      });

      server.middlewares.use("/api/lifecycle", async (_req, res) => {
        res.setHeader("Content-Type", "application/json");
        res.setHeader("Cache-Control", "no-store");
        try {
          res.end(JSON.stringify(await fetchLifecycle()));
        } catch (err) {
          res.statusCode = 502;
          res.end(JSON.stringify({ error: (err as Error).message }));
        }
      });

      /**
       * GET /api/assigned?to=<role> — one agent's assigned tasks.
       *
       * READ-ONLY like every route here. The role is a snapshot VALUE sent in a
       * JSON body rather than interpolated into a path, so it needs no id-shape
       * allowlist; it is length-capped and required non-empty so a stray call
       * cannot turn into an unfiltered scan of 20,922 tasks.
       */
      server.middlewares.use("/api/assigned", async (req, res) => {
        const params = new URL(req.url ?? "/", "http://localhost").searchParams;
        const to = (params.get("to") ?? "").trim();
        const limit = Math.min(Number(params.get("limit")) || 100, 200);
        res.setHeader("Content-Type", "application/json");
        res.setHeader("Cache-Control", "no-store");

        if (!to || to.length > 80) {
          res.statusCode = 400;
          res.end(JSON.stringify({ error: "Missing or overlong `to`." }));
          return;
        }

        try {
          res.end(JSON.stringify(await fetchAssignedTasks(to, limit)));
        } catch (err) {
          res.statusCode = 502;
          res.end(JSON.stringify({ error: (err as Error).message }));
        }
      });

      /**
       * GET /api/schemas — the registry, and how far it has drifted from the
       * data. READ-ONLY: one GET plus background reads, no mutation anywhere.
       *
       * Takes no parameters. The type lists are fixed above rather than
       * client-supplied, so this route cannot be pointed at an arbitrary
       * upstream path or turned into an unbounded scan.
       */
      server.middlewares.use("/api/schemas", async (_req, res) => {
        res.setHeader("Content-Type", "application/json");
        res.setHeader("Cache-Control", "no-store");
        try {
          res.end(JSON.stringify(await fetchSchemas()));
        } catch (err) {
          res.statusCode = 502;
          res.end(JSON.stringify({ error: (err as Error).message }));
        }
      });

      /**
       * GET /api/scan-freshness?commit=<sha> — is the committed code scan
       * still describing the current tree?
       *
       * The client passes the commit baked into `codeUsage.ts` rather than the
       * server re-reading that file, so the answer is about the exact module
       * the browser loaded. Touches git only through read-only plumbing.
       */
      server.middlewares.use("/api/scan-freshness", (req, res) => {
        const commit =
          new URL(req.url ?? "/", "http://localhost").searchParams.get("commit") ?? "";
        res.setHeader("Content-Type", "application/json");
        res.setHeader("Cache-Control", "no-store");
        res.end(JSON.stringify(scanFreshness(commit)));
      });

      server.middlewares.use("/api/agents", async (req, res) => {
        const limit = Math.min(
          Number(new URL(req.url ?? "/", "http://localhost").searchParams.get("limit")) || 200,
          1000,
        );
        res.setHeader("Content-Type", "application/json");
        res.setHeader("Cache-Control", "no-store");
        try {
          res.end(JSON.stringify(await fetchAgents(limit)));
        } catch (err) {
          res.statusCode = 502;
          res.end(JSON.stringify({ error: (err as Error).message }));
        }
      });

      /**
       * GET /api/search?type=<entity_type>&q=<text>&<field>=<value> — ONE type's
       * matches for a query. READ-ONLY, like every route here.
       *
       * ONE TYPE PER REQUEST, DELIBERATELY. Neotoma accepts `entity_types`
       * (plural) and will search several types in a single query, but measured
       * against the live instance on 2026-08-31 that is both slower in total
       * and strictly worse to render:
       *
       *   entity_types: [task, project, plan, issue, conversation,
       *                  agent_definition], search "theodore"  ->  47.5s
       *   the same six as six parallel single-type queries        ->  27.6s wall
       *     (agent_definition 5.6s, project 7.1s, plan 9.7s, issue 14.1s,
       *      conversation 17.5s, task 27.6s)
       *
       * The wall-clock win is real but secondary. The point is that six
       * requests SETTLE INDEPENDENTLY: the client renders `plan` at 9.7s
       * instead of showing nothing until the slowest type in the set comes
       * back, and one type timing out costs that type's group rather than the
       * entire result set. With reads in the 25-81s range (ateles#576, root
       * cause neotoma#2217) that difference decides whether the feature is
       * usable at all.
       *
       * The reader-pool starvation upstream is also why the timeout here is
       * generous: a slow answer is the normal case, not a fault.
       */
      server.middlewares.use("/api/search", async (req, res) => {
        const params = new URL(req.url ?? "/", "http://localhost").searchParams;
        const entityType = (params.get("type") ?? "").trim();
        const q = (params.get("q") ?? "").trim();
        const limit = Math.min(Number(params.get("limit")) || 12, 50);

        res.setHeader("Content-Type", "application/json");
        res.setHeader("Cache-Control", "no-store");

        // The type is a VALUE in a JSON body, not a path segment, so it needs
        // no id-shape allowlist — but it is still length-capped and required,
        // so a stray call cannot become an unbounded scan.
        if (!entityType || entityType.length > 80) {
          res.statusCode = 400;
          res.end(JSON.stringify({ error: "Missing or overlong `type`." }));
          return;
        }

        /**
         * `field:value` terms arrive as extra query parameters and become
         * `snapshot_filters`, so the narrowing happens SERVER-SIDE. Filtering
         * client-side would mean fetching a broad page first, which at these
         * latencies does not return.
         *
         * Only `eq` is offered. `snapshot_filters` also accepts `in`, `gt`,
         * `lt`, `gte`, `lte`, `contains` and `contains_word`, but `ne`/`nin`
         * are rejected 400 upstream — see OPEN_TASK_STATUSES above.
         */
        const snapshotFilters: Record<string, { op: "eq"; value: string }> = {};
        for (const [key, value] of params) {
          if (key === "type" || key === "q" || key === "limit") continue;
          if (!/^[a-z0-9_]{1,60}$/i.test(key)) continue;
          if (!value || value.length > 120) continue;
          snapshotFilters[key] = { op: "eq", value };
        }
        const filtered = Object.keys(snapshotFilters).length > 0;

        // Nothing to ask: no free text and no field terms would be an
        // unfiltered scan of the whole type.
        if (!q && !filtered) {
          res.statusCode = 400;
          res.end(JSON.stringify({ error: "Empty query." }));
          return;
        }

        try {
          const body = (await neotomaPost(
            "/entities/query",
            {
              entity_type: entityType,
              ...(q ? { search: q } : {}),
              ...(filtered ? { snapshot_filters: snapshotFilters } : {}),
              limit,
              include_snapshots: true,
              // NO `sort_by` HERE. Every other query in this file pairs
              // `sort_by: "last_observation_at"` with `sort_order: "desc"`, but
              // upstream rejects that combination with HTTP 400 the moment
              // `search` is also present (verified against the live instance —
              // the identical body minus `sort_by` returns 200 in 5.3s).
              // Relevance ordering is the server's to choose on a search; the
              // client sorts each group by its own timestamp for display.
            },
            // Deliberately longer than the 30s interactive default: `task`
            // alone measured 27.6s, and cutting at 30s would turn the common
            // case into a timeout.
            75_000,
          )) as { entities?: unknown[]; total?: number };

          res.end(
            JSON.stringify({
              entities: body.entities ?? [],
              total: body.total,
              // A filtered `total` saturates at 10000 — it is a lower bound,
              // not a count. See FILTERED_TOTAL_CEILING.
              total_saturated: filtered && body.total === FILTERED_TOTAL_CEILING,
            }),
          );
        } catch (err) {
          // A timeout is NOT an empty result, and the client renders the two
          // differently. Say which this was so it can.
          const message = (err as Error).message;
          const timedOut =
            (err as Error).name === "TimeoutError" || /timeout|aborted/i.test(message);
          res.statusCode = 502;
          res.end(JSON.stringify({ error: message, timedOut }));
        }
      });

    },
  };
}
