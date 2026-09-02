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

/** A single history entry, flattened for display. */
export interface HistoryEntry {
  at: Date | null;
  /** The field names this observation wrote. */
  fields: string[];
  /** True when the observation carries a source — i.e. an import/tool wrote it. */
  sourced: boolean;
}

/**
 * Newest-first history. Each observation names the fields it wrote, which is
 * what turns "status: done" into "status changed, on this date, after the title
 * was set and before the result was written".
 */
export function toHistory(observations: Observation[]): HistoryEntry[] {
  return observations
    .map((o) => {
      const at = o.observed_at ? new Date(o.observed_at) : null;
      return {
        at: at && !Number.isNaN(at.getTime()) ? at : null,
        fields: Object.keys(o.fields ?? {}),
        sourced: Boolean(o.source_id),
      };
    })
    .sort((a, b) => (b.at?.getTime() ?? 0) - (a.at?.getTime() ?? 0));
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
