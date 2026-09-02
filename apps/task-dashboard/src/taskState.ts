/**
 * TASK STATE — what is actually going on with a task, as opposed to what
 * fields it happens to store.
 *
 * THE PROBLEM THIS SOLVES. A task detail view can show `status: pending` and
 * still leave the operator with no idea what that means in practice: nobody is
 * named on it, nothing is scheduled to pick it up, and the status word alone
 * does not distinguish "queued and moving" from "filed in May and untouched
 * since".
 *
 * THE HONEST COMPLICATION, which shapes everything below. For most tasks the
 * data does not exist: `assigned_to` is empty on the large majority of recent
 * tasks, subagent sessions are not persisted at all, and no task carries a run
 * or dispatch record. So this module never renders an empty field. Where
 * nothing is stored it produces a FINDING — "no owner assigned, so nothing can
 * pick this up" — because that sentence is useful where a blank `Assigned to:`
 * row is not.
 *
 * READ-ONLY, like the rest of the app: this parses what Neotoma already holds.
 */

/** One append-only observation, as `/api/observations` returns it. */
export interface Observation {
  id: string;
  observed_at: string;
  source_id: string | null;
  fields: Record<string, unknown> | null;
  /**
   * Server-recorded attribution, when there is any. Frequently `null` on
   * daemon writes — see `Writer` below, where that gap is the point rather
   * than a case to be tidied away.
   */
  provenance?: Record<string, unknown> | null;
  /** Client-supplied replay key. By convention it names the writer. */
  idempotency_key?: string | null;
}

export interface ObservationsPayload {
  observations?: Observation[];
  total?: number;
  error?: string;
}

/**
 * The roles Apis can actually spawn.
 *
 * MIRRORS `ASSIGNED_TO_ROUTES` in `execution/daemons/apis/routing.py`. It is
 * hardcoded here deliberately and WILL DRIFT if that map changes — the
 * dashboard has no route to read it. The check it powers is worth the drift
 * risk: a task assigned to one of the other roster roles names an owner nothing
 * can spawn, which is a real and common defect that otherwise looks identical
 * to a correctly-dispatched task.
 */
export const DISPATCHABLE_ROLES = [
  "cicada",
  "fringilla",
  "gorilla",
  "monedula",
  "sturnus",
] as const;

export type Dispatchability =
  | { kind: "unassigned" }
  | { kind: "dispatchable"; owner: string }
  | { kind: "unspawnable"; owner: string };

/** Can anything actually pick this task up? */
export function dispatchability(assignedTo: string | null): Dispatchability {
  const owner = assignedTo?.trim();
  if (!owner) return { kind: "unassigned" };
  const normalized = owner.toLowerCase();
  const match = DISPATCHABLE_ROLES.find(
    (r) => normalized === r || normalized.includes(r),
  );
  return match ? { kind: "dispatchable", owner } : { kind: "unspawnable", owner };
}

/** One field written by one observation: the name AND what it was set to. */
export interface FieldChange {
  name: string;
  /**
   * The value as stored, rendered for display. Kept as a string here rather
   * than as `unknown` so the view never has to decide how to stringify a
   * nested object mid-render; `preview` is the truncated form and `full` the
   * whole thing, so a long description can be summarised without being lost.
   */
  preview: string;
  full: string;
  /** True when the stored value is longer than the preview shows. */
  truncated: boolean;
}

/**
 * WHO WROTE AN OBSERVATION — and how much that claim is worth.
 *
 * The two are deliberately separate fields rather than one "author" string,
 * because they are not the same kind of evidence and collapsing them would
 * present a guess as an attribution.
 */
export interface Writer {
  /**
   * From the observation's own `provenance` block. This is the real thing:
   * server-recorded, carrying an `attribution_tier`.
   *
   * NULL ON MOST DAEMON WRITES. Verified on prod 2026-09-02: a task's creation
   * observation carries provenance `{attribution_tier: "unverified_client",
   * client_name: "local-agent-mode-mcpsrv_neotoma"}`, while every subsequent
   * daemon write on the same entity carries `provenance: null`. That gap is
   * shown on the page rather than papered over — it is exactly what the
   * identity work (ent_e25ca40bd0c7c986c9e18ac4) closes, and when it lands the
   * improvement appears here with no change to this file.
   */
  clientName: string | null;
  attributionTier: string | null;
  /**
   * Inferred from the `idempotency_key`, which BY CONVENTION names the writing
   * component: `taskstatus-apis-<entity>-awaiting_approval-created`.
   *
   * This is how a component was identified when `provenance` was null, so it
   * is worth surfacing — but it is a NAMING CONVENTION, not an authenticated
   * identity. Anything can write any key. The view must label it as such and
   * must never render it as though it were attribution.
   */
  conventionName: string | null;
}

/** A single history entry, flattened for display. */
export interface HistoryEntry {
  at: Date | null;
  /** Every field this observation wrote, WITH the value it wrote. */
  changes: FieldChange[];
  /** True when the observation carries a source — i.e. an import/tool wrote it. */
  sourced: boolean;
  writer: Writer;
  /** The raw idempotency key, shown as the evidence behind `conventionName`. */
  idempotencyKey: string | null;
}

/** How much of a stored value to show inline before truncating. */
const PREVIEW_CHARS = 120;

/**
 * Render a stored value for display WITHOUT losing what it was.
 *
 * Observation fields hold whatever the schema allows: strings, numbers,
 * booleans, nulls, and nested objects/arrays (`gate_status` is a map). A
 * `String(value)` would turn every object into "[object Object]", which is the
 * same information loss this whole module exists to undo.
 */
function renderValue(value: unknown): { preview: string; full: string; truncated: boolean } {
  let full: string;
  if (value === null) full = "null";
  else if (typeof value === "string") full = value;
  else if (typeof value === "number" || typeof value === "boolean") full = String(value);
  else {
    try {
      full = JSON.stringify(value);
    } catch {
      // Circular or otherwise unserialisable. Say so rather than crashing the
      // row or silently dropping the change.
      full = "[unserialisable value]";
    }
  }

  // Collapse newlines for the inline preview only. `full` keeps them, so the
  // expanded view still shows the value as stored.
  const flat = full.replace(/\s+/g, " ").trim();
  const truncated = flat.length > PREVIEW_CHARS;
  return {
    preview: truncated ? `${flat.slice(0, PREVIEW_CHARS)}…` : flat,
    full,
    truncated,
  };
}

/**
 * Pull the writing component out of an idempotency key by convention.
 *
 * The observed shape is `<what>-<who>-<entity>-<detail>`, e.g.
 * `taskstatus-apis-ent_46362b73…-awaiting_approval-created`. The second
 * hyphen-separated segment is the writer. Anything that does not match the
 * shape returns null rather than guessing — a key like `update-plan-todos-…`
 * would otherwise yield "plan" as an author, which is worse than no answer.
 */
export function writerFromIdempotencyKey(key: string | null): string | null {
  if (!key) return null;
  const parts = key.split("-");
  if (parts.length < 3) return null;
  const candidate = parts[1];
  // Must look like a component name, and must not be the entity id itself.
  if (!/^[a-z][a-z0-9_]{2,}$/.test(candidate)) return null;
  if (candidate.startsWith("ent")) return null;
  return candidate;
}

/**
 * Newest-first history, WITH the value each field was set to.
 *
 * WHY THE VALUES ARE THE POINT. This function used to return
 * `Object.keys(o.fields)` — the field NAMES, with the values dropped. That
 * showed *which* fields changed and threw away *what they changed to*, which
 * is the useful half. The operator's words, 2026-09-02: "it shows the history
 * of the fields that have changed, but we don't see the actual historical
 * values of those fields?"
 *
 * Because Neotoma is append-only, keeping the values makes the full sequence
 * reconstructable: for any field, every value it ever held, when, and in what
 * order. That is what makes "when did this become blocked, and what was it
 * before" answerable at all — a question that cost a manual investigation on
 * 2026-09-02 when the answer was already sitting in the observation log.
 *
 * API NOTE, deliberately recorded here because the tool whose NAME suggests it
 * does this job is the one that fails: do NOT use `retrieve_field_provenance`.
 * It errors with "Observation does not have source_id" on daemon corrections,
 * which set `source_id: null` — i.e. on exactly the writes you most want to
 * attribute. `list_observations` (`GET /entities/<id>/observations`, what
 * `/api/observations` proxies) returns the full payload and works.
 */
export function toHistory(observations: Observation[]): HistoryEntry[] {
  return observations
    .map((o) => {
      const at = o.observed_at ? new Date(o.observed_at) : null;
      const prov = (o.provenance ?? null) as Record<string, unknown> | null;
      const clientName =
        prov && typeof prov.client_name === "string" ? prov.client_name : null;
      const attributionTier =
        prov && typeof prov.attribution_tier === "string" ? prov.attribution_tier : null;

      return {
        at: at && !Number.isNaN(at.getTime()) ? at : null,
        changes: Object.entries(o.fields ?? {}).map(([name, value]) => {
          const { preview, full, truncated } = renderValue(value);
          return { name, preview, full, truncated };
        }),
        sourced: Boolean(o.source_id),
        writer: {
          clientName,
          attributionTier,
          conventionName: writerFromIdempotencyKey(o.idempotency_key ?? null),
        },
        idempotencyKey: o.idempotency_key ?? null,
      };
    })
    .sort((a, b) => (b.at?.getTime() ?? 0) - (a.at?.getTime() ?? 0));
}

/**
 * Does ANY observation on this entity carry real provenance?
 *
 * Drives the one-line note above the history table. When the answer is no —
 * the common case for a task the daemons have written — saying so once at the
 * top is more honest and less noisy than repeating "unattributed" on every row.
 */
export function provenanceCoverage(history: HistoryEntry[]): {
  attributed: number;
  total: number;
} {
  return {
    attributed: history.filter((h) => h.writer.clientName !== null).length,
    total: history.length,
  };
}

/**
 * Was this entity written once and never touched?
 *
 * A one-observation history is a real and meaningful state — it means the task
 * was filed and nothing has happened to it since — and saying so is far more
 * informative than rendering a one-row timeline without comment.
 */
export function writtenOnce(history: HistoryEntry[]): boolean {
  return history.length === 1;
}

/**
 * ROLE NAME -> AGENT ENTITY, so an owner can be a link.
 *
 * `assigned_to` stores a ROLE NAME ("cicada"), not an entity id, so linking a
 * task to its agent's page means resolving that name against the roster. The
 * match is case-insensitive because production holds both `cicada` and
 * `Bombycilla`, written by different agents at different times.
 *
 * Returns null when no agent_definition carries the name — which is a real
 * case, not a lookup bug: `operator` is a valid `assigned_to` value and has no
 * agent entity. Callers render the name as plain text rather than a dead link.
 */
export function agentIdForRole(
  role: string | null,
  agents: { id: string; name: string }[],
): string | null {
  const key = role?.trim().toLowerCase();
  if (!key) return null;
  return agents.find((a) => a.name.trim().toLowerCase() === key)?.id ?? null;
}

/**
 * Is this role one Apis can actually spawn?
 *
 * The thin wrapper exists so callers asking only this question do not have to
 * destructure a `Dispatchability`, and so the answer comes from ONE place. See
 * `DISPATCHABLE_ROLES` above for why the list is duplicated from routing.py at
 * all, and why it is drift-prone.
 */
export function isSpawnable(role: string | null): boolean {
  return dispatchability(role).kind === "dispatchable";
}
