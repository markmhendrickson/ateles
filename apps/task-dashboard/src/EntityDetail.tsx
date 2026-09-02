/**
 * ENTITY DETAIL — ONE RENDERER, TWO PRESENTATIONS
 * -----------------------------------------------
 * This component is the whole of what an entity looks like. The full page at
 * `#/entities/<id>` and the slide-over sheet both render THIS, differing only
 * in the frame around it. That is deliberate and load-bearing: two
 * presentations of a dozen entity types is exactly where a second, quietly
 * diverging renderer would grow.
 *
 * WHY THE VIEWS ARE IN-APP
 * ------------------------
 * Entity links used to leave for Neotoma, which lost the operator's session
 * context and 401'd on arrival anyway (entities and rendered pages need an
 * access_token that only exists in publish-time responses). The dev proxy holds
 * a bearer token, so `/api/entity` reads the same entity server-side.
 *
 * The external Neotoma link is KEPT on every detail view as a secondary
 * action — clearly labelled, and honest that it needs a signed-in session.
 *
 * SPECIAL-CASED TYPES
 * -------------------
 * `task`, `rendered_page`, `plan`, `rendered_page_template`, `conversation`,
 * and `agent_definition` get views that order their declared fields the way
 * they are actually read. Every other type — and every field a special-cased
 * view does not name — falls through to `GenericFields`, which renders whatever
 * the snapshot holds as a definition list. Nothing is ever dropped for want of
 * a case, and no type ever renders blank.
 *
 * READ-ONLY: renders stored data. No form, no control that writes anything.
 */
import { useEffect, useState } from "react";
import {
  type EntityEdge,
  type EntityPayload,
  type Field,
  entityTitle,
  fields,
  str,
  typeLabel,
  unwrapSnapshot,
} from "./entity";
import {
  type HistoryEntry,
  type ObservationsPayload,
  DISPATCHABLE_ROLES,
  dispatchability,
  provenanceCoverage,
  toHistory,
  writtenOnce,
} from "./taskState";
import {
  type WorkflowLink,
  type WorkflowLinkPayload,
  WORKFLOW_LINKAGE_FACTS,
  gateDisagreements,
  placeOnLifecycle,
} from "./taskPosition";
import { PATH_ORDER, VALIDATION_NOTE } from "./lifecycleData";
import { AssignedTo } from "./AssignedTo";
import { useRoster } from "./useRoster";
import { absoluteTime, entityUrl, toBucket } from "./tasks";
import { Markdown } from "./Markdown";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { ScrollArea } from "@/components/ui/scroll-area";
import { EntityDetailSkeleton } from "@/components/Skeletons";
import { showSkeleton } from "@/lib/loading";
import { ExternalLink } from "lucide-react";

const REFRESH_MS = 10_000;

/**
 * Fetch one entity, keeping it fresh on the shared 10s cadence.
 *
 * `firstLoadDone` follows the app-wide rule: it flips once the first request
 * SETTLES, so the skeleton shows only before there is anything to draw and the
 * poll never strobes a skeleton back over what the operator is reading.
 *
 * The proxy serves this from the same hydration cache the session view fills in
 * the background, so an entity clicked from a session page is usually already
 * warm; a cold id (a pasted URL) costs one upstream GET and then caches.
 */
export function useEntity(id: string | null): {
  payload: EntityPayload | null;
  error: string | null;
  firstLoadDone: boolean;
} {
  const [payload, setPayload] = useState<EntityPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [firstLoadDone, setFirstLoadDone] = useState(false);

  useEffect(() => {
    if (!id) return;

    // A different entity is a different subject: clear rather than showing the
    // previous entity's fields under the new one's heading.
    setPayload(null);
    setError(null);
    setFirstLoadDone(false);

    let alive = true;
    const load = async () => {
      try {
        const res = await fetch(`/api/entity?id=${encodeURIComponent(id)}`);
        const body: EntityPayload = await res.json();
        if (!alive) return;
        if (!res.ok || body.error) throw new Error(body.error ?? `HTTP ${res.status}`);
        setPayload(body);
        setError(null);
      } catch (err) {
        if (alive) setError((err as Error).message);
      } finally {
        if (alive) setFirstLoadDone(true);
      }
    };

    void load();
    const timer = setInterval(() => void load(), REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [id]);

  return { payload, error, firstLoadDone };
}

/** Colour for a raw `status`, via the same bucket mapping the task view uses. */
function statusTone(status: string): "ok" | "bad" | "warn" | "live" | "muted" {
  switch (toBucket(status)) {
    case "done":
      return "ok";
    case "blocked":
      return "bad";
    case "in_progress":
      return "live";
    case "pending":
      return "warn";
    default:
      return "muted";
  }
}

/** One labelled scalar, omitted when unset. Mirrors the agent view's `Fact`. */
function Fact({ label, value }: { label: string; value: string | null }) {
  if (!value) return null;
  return (
    <div className="flex items-baseline gap-[5px]">
      <span className="text-[10px] uppercase tracking-[.06em] text-muted-foreground">{label}</span>
      <span className="text-[12px] tabular-nums">{value}</span>
    </div>
  );
}

/**
 * The `assigned_to` fact, as a LINK to the owning agent.
 *
 * Shown on every entity that carries the field, in both the sheet and the full
 * page, so the operator can reach the agent from wherever a task is open.
 * Renders nothing when unset — the task's State panel already says "no owner
 * assigned" in words, and a second blank row beside it would add nothing.
 */
function AssignedFact({ assignedTo }: { assignedTo: string | null }) {
  const roster = useRoster();
  if (!assignedTo) return null;

  return (
    <div className="flex items-baseline gap-[5px]">
      <span className="text-[10px] uppercase tracking-[.06em] text-muted-foreground">
        Assigned to
      </span>
      <span className="text-[12px]">
        <AssignedTo
          assignedTo={assignedTo}
          agents={roster}
          onOpenAgent={(id) => {
            window.location.hash = `#/agents/${id}`;
          }}
        />
      </span>
    </div>
  );
}

/** A labelled block of prose. */
function Prose({
  label,
  text,
  tone,
  note,
}: {
  label: string;
  text: string;
  /** Amber marks an agent SUGGESTION; green marks the operator's own answer. */
  tone?: "amber" | "green";
  note?: string;
}) {
  const toneClass =
    tone === "amber"
      ? "border-[hsl(var(--warn)/0.26)] bg-[hsl(var(--warn)/0.08)]"
      : tone === "green"
        ? "border-[hsl(var(--ok)/0.26)] bg-[hsl(var(--ok)/0.08)]"
        : "border-border bg-card";

  return (
    <section className="my-[10px]">
      <span className="mb-[3px] block text-[10px] uppercase tracking-[.06em] text-muted-foreground">
        {label}
      </span>
      {note && (
        <p className="m-0 mb-[3px] text-[11.5px] leading-[1.4] text-muted-foreground">{note}</p>
      )}
      {/* The amber/green tone survives the density pass untouched: it is the
          suggestion-vs-decision distinction, not decoration. */}
      <div className={`rounded-[7px] border px-[10px] py-[3px] ${toneClass}`}>
        <Markdown source={text} />
      </div>
    </section>
  );
}

/** A list-valued field, as chips. */
function Chips({ label, values }: { label: string; values: string[] }) {
  if (!values.length) return null;
  return (
    <section className="my-[10px]">
      <span className="mb-[3px] block text-[10px] uppercase tracking-[.06em] text-muted-foreground">
        {label}
      </span>
      <div className="flex flex-wrap gap-[4px]">
        {values.map((v, i) => (
          <Badge key={`${v}-${i}`} variant="tag">
            {v}
          </Badge>
        ))}
      </div>
    </section>
  );
}

/**
 * Whatever the snapshot holds, as a definition list.
 *
 * This is the fallback for unrecognized entity types AND the tail of every
 * special-cased view — a type-specific view names the fields it orders
 * deliberately and passes them as `omit`, so a field added to a schema later
 * still appears here instead of vanishing because no one updated the component.
 */
function GenericFields({ list }: { list: Field[] }) {
  if (!list.length) return null;

  return (
    <>
      {list.map((f) => {
        if (f.kind === "list") return <Chips key={f.name} label={f.label} values={f.items ?? []} />;

        if (f.kind === "json") {
          return (
            <section key={f.name} className="my-[10px]">
              <span className="mb-[3px] block text-[10px] uppercase tracking-[.06em] text-muted-foreground">
                {f.label}
              </span>
              {/* Structured values keep their structure. Flattening a map of
                  decisions or todos into prose would misrepresent it. */}
              <ScrollArea className="max-h-[340px] rounded-[10px] border bg-card">
                <pre className="m-0 overflow-x-auto px-[14px] py-3 font-mono text-[12px] leading-[1.55] text-muted-foreground">
                  {f.json}
                </pre>
              </ScrollArea>
            </section>
          );
        }

        if (f.kind === "markdown") {
          return <Prose key={f.name} label={f.label} text={f.text ?? ""} />;
        }

        return (
          <section key={f.name} className="my-[8px]">
            <span className="mb-[3px] block text-[10px] uppercase tracking-[.06em] text-muted-foreground">
              {f.label}
            </span>
            <p className="m-0 text-[12.5px] leading-[1.45]">{f.text}</p>
          </section>
        );
      })}
    </>
  );
}

/* ------------------------------------------------------------------ *
 * Type-specific views. Each names the fields it renders itself, and
 * hands the rest to GenericFields so nothing is lost.
 * ------------------------------------------------------------------ */

/**
 * WHAT IS ACTUALLY GOING ON WITH THIS TASK.
 *
 * Two questions the stored fields do not answer on their own: can anything pick
 * this up, and what has actually happened to it so far.
 *
 * The rule throughout is NO EMPTY FIELDS. For most tasks the data does not
 * exist — `assigned_to` is unset on the large majority, and no task carries a
 * dispatch or run record anywhere in Neotoma — so where nothing is stored this
 * states the finding in words instead of rendering a blank row. "No owner
 * assigned — nothing can dispatch this" is useful; an empty `Assigned to:` is
 * not, and worse, reads as a display bug rather than as the defect it is.
 *
 * Deliberately NOT shown: any "agent currently working on it" display. No task
 * carries that data — subagent sessions are not persisted at all — so the panel
 * says the tracking does not exist rather than inventing a field for it.
 */
function TaskStatePanel({ snap, id }: { snap: Record<string, unknown>; id: string }) {
  const [history, setHistory] = useState<HistoryEntry[] | null>(null);
  /** Distinguishes "not read yet" from "read, and there are none". */
  const [historyFailed, setHistoryFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    setHistory(null);
    setHistoryFailed(false);
    (async () => {
      try {
        const res = await fetch(`/api/observations?id=${encodeURIComponent(id)}`);
        const body: ObservationsPayload = await res.json();
        if (!alive) return;
        if (!res.ok || body.error) throw new Error(body.error ?? `HTTP ${res.status}`);
        setHistory(toHistory(body.observations ?? []));
      } catch {
        // Degrade silently: the task's own fields are unaffected by a history
        // that could not be read, and a broken panel would be worse than none.
        if (alive) setHistoryFailed(true);
      }
    })();
    return () => {
      alive = false;
    };
  }, [id]);

  const roster = useRoster();
  const dispatch = dispatchability(str(snap.assigned_to));

  /**
   * Navigate to the agent by writing the hash directly.
   *
   * This panel renders from both the full page and the slide-over sheet, and
   * threading a callback down through every layer of both would be more
   * plumbing than the one line it replaces. `useRoute` listens for
   * `hashchange`, so this takes the identical path as any other navigation.
   */
  const openAgent = (agentId: string) => {
    window.location.hash = `#/agents/${agentId}`;
  };

  return (
    <section className="my-[10px] rounded-[7px] border bg-card px-[10px] py-[7px]">
      <h3 className="m-0 mb-[4px] text-[10px] font-[650] uppercase tracking-[.06em] text-muted-foreground">
        State
      </h3>

      {/* DISPATCHABILITY. Amber and red here are findings about the task, not
          status colours — an unspawnable owner is a defect, not a stage. */}
      {dispatch.kind === "unassigned" && (
        <p className="m-0 text-[12px] leading-[1.45] text-warn">
          <strong>No owner assigned</strong>{" "}
          <span className="text-muted-foreground">
            — <code className="text-[11px]">assigned_to</code> is empty, so nothing can dispatch
            this. It stays here until someone picks it up by hand.
          </span>
        </p>
      )}
      {dispatch.kind === "dispatchable" && (
        <p className="m-0 text-[12px] leading-[1.45]">
          <span className="text-live">
            Assigned to{" "}
            <AssignedTo
              assignedTo={dispatch.owner}
              agents={roster}
              onOpenAgent={openAgent}
              compact
              className="font-[650]"
            />
          </span>{" "}
          <span className="text-muted-foreground">— a role Apis can spawn.</span>
        </p>
      )}
      {dispatch.kind === "unspawnable" && (
        <p className="m-0 text-[12px] leading-[1.45] text-bad">
          <strong>
            Assigned to{" "}
            <AssignedTo
              assignedTo={dispatch.owner}
              agents={roster}
              onOpenAgent={openAgent}
              compact
            />
            , which nothing can spawn
          </strong>{" "}
          <span className="text-muted-foreground">
            — not one of the {DISPATCHABLE_ROLES.length} roles in Apis's route table (
            {DISPATCHABLE_ROLES.join(", ")}), so this names an owner that will never pick it up.
          </span>
        </p>
      )}

      {/* Said plainly rather than left as an empty section: no task in Neotoma
          records a run, and subagent sessions are not persisted at all. */}
      <p className="m-0 mt-[3px] text-[11px] leading-[1.4] text-muted-foreground">
        No execution tracking exists for tasks — there is no run or dispatch record on any task
        entity, and subagent sessions are not stored — so whether an agent is working this right
        now is not something Neotoma can answer.
      </p>

      {/* HISTORY — the append-only observation log, WITH the values. */}
      <div className="mt-[6px] border-t pt-[5px]">
        <div className="mb-[2px] flex items-baseline gap-[8px]">
          <span className="text-[10px] uppercase tracking-[.06em] text-muted-foreground">
            History
          </span>
          <span className="text-[11px] text-muted-foreground">
            every field change and what it changed to, newest first — Neotoma is append-only
          </span>
        </div>

        {historyFailed ? (
          <p className="m-0 text-[11.5px] text-muted-foreground">
            History could not be read for this entity. Its fields above are unaffected.
          </p>
        ) : history === null ? (
          <p className="m-0 text-[11.5px] text-muted-foreground">Reading history…</p>
        ) : history.length === 0 ? (
          <p className="m-0 text-[11.5px] text-muted-foreground">
            No observations recorded for this entity.
          </p>
        ) : (
          <>
            {writtenOnce(history) && (
              <p className="m-0 mb-[2px] text-[11.5px] text-warn">
                Written once and never touched since — this task has had no activity beyond being
                filed.
              </p>
            )}
            <ProvenanceNote history={history} />
            <table className="w-full border-collapse text-[11.5px]">
              <tbody>
                {history.map((h, i) => (
                  <tr key={i} className="border-b border-border/50 align-baseline last:border-b-0">
                    <td className="w-[122px] py-[3px] pr-2 align-baseline tabular-nums text-muted-foreground">
                      {h.at ? absoluteTime(h.at) : "unknown"}
                    </td>
                    <td className="py-[3px] pr-2 align-baseline">
                      {h.changes.length ? (
                        <div className="flex flex-col gap-[2px]">
                          {h.changes.map((c) => (
                            <div key={c.name} className="leading-[1.4]">
                              <span className="font-mono text-[11px] text-muted-foreground">
                                {c.name}
                              </span>
                              <span className="text-muted-foreground"> = </span>
                              {/* `title` on the value gives the untruncated
                                  string on hover, so a long description is
                                  summarised without being lost. */}
                              <span
                                className="font-mono text-[11px]"
                                title={c.truncated ? c.full : undefined}
                              >
                                {c.preview}
                              </span>
                            </div>
                          ))}
                        </div>
                      ) : (
                        <span className="text-muted-foreground">no fields</span>
                      )}
                    </td>
                    <td className="w-[152px] py-[3px] text-right align-baseline">
                      <WriterCell entry={h} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>
    </section>
  );
}

/**
 * WHO WROTE THIS CHANGE — and how much the answer is worth.
 *
 * Three cases, deliberately styled differently because they carry different
 * amounts of evidence:
 *
 *   real provenance   — the server recorded a client name and tier. Shown plain.
 *   convention only   — `provenance` was null, but the idempotency key names a
 *                       component by convention. Shown MUTED and marked "by
 *                       convention", because anything can write any key. It is
 *                       a useful hint, not an attribution, and rendering it as
 *                       though it were attribution is the failure to avoid.
 *   nothing           — neither. Said plainly rather than left blank.
 */
function WriterCell({ entry }: { entry: HistoryEntry }) {
  const { clientName, attributionTier, conventionName } = entry.writer;

  if (clientName) {
    return (
      <span className="text-[10px] leading-[1.35]">
        <span className="font-mono">{clientName}</span>
        {attributionTier && (
          <span className="block text-muted-foreground">{attributionTier.replace(/_/g, " ")}</span>
        )}
      </span>
    );
  }

  if (conventionName) {
    return (
      <span
        className="text-[10px] leading-[1.35] text-muted-foreground"
        title={entry.idempotencyKey ?? undefined}
      >
        <span className="font-mono">{conventionName}?</span>
        <span className="block">by convention, unverified</span>
      </span>
    );
  }

  return (
    <span className="text-[10px] text-muted-foreground">
      {entry.sourced ? "imported" : "unattributed"}
    </span>
  );
}

/**
 * ONE LINE ON HOW MUCH OF THIS HISTORY IS ACTUALLY ATTRIBUTABLE.
 *
 * Stated once at the top rather than repeated per row. The measured reality on
 * a task the daemons have touched: the creation observation carries provenance
 * and every subsequent daemon write carries `provenance: null`. That gap is
 * shown because it is the identity problem made visible on the page instead of
 * something found by audit — and when the identity work lands, this line
 * improves on its own.
 */
function ProvenanceNote({ history }: { history: HistoryEntry[] }) {
  const { attributed, total } = provenanceCoverage(history);
  if (total === 0 || attributed === total) return null;

  return (
    <p className="m-0 mb-[3px] text-[11px] leading-[1.4] text-muted-foreground">
      <span className="text-warn">
        {attributed} of {total}
      </span>{" "}
      {total - attributed === 1 ? "change carries" : "changes carry"} recorded provenance
      {attributed === 0
        ? " — none of these writes is attributable to a verified client."
        : "; the rest were written with none."}{" "}
      Where a writer is guessed below it comes from the{" "}
      <code className="text-[10.5px]">idempotency_key</code> naming convention, which is a hint
      rather than an authenticated identity.
    </p>
  );
}

/**
 * WHERE THIS TASK SITS ON THE LIFECYCLE.
 *
 * The forward path is rendered as a sequence with the current position marked,
 * because "where along the lifecycle" is a question about ORDER and a list of
 * badges does not answer it.
 *
 * Holds (`awaiting_approval`, `blocked`, …) are NOT rendered as further along
 * the path — they sit beside it. A hold is a mode a task enters FROM a path
 * position and returns to, so showing it as step six would answer "does done
 * come before or after awaiting_approval?", which has no answer.
 *
 * OFF-VOCABULARY STATUSES ARE THE MAJORITY, not an edge case: 17,251 of 21,285
 * tasks (81%) carry a status the state machine never declared. Those render as
 * the raw value, explicitly outside the lifecycle, with no position guessed.
 */
function LifecyclePanel({ snap }: { snap: Record<string, unknown> }) {
  const position = placeOnLifecycle(snap.status);

  return (
    <section className="my-[10px] rounded-[7px] border bg-card px-[10px] py-[7px]">
      <div className="mb-[5px] flex items-baseline gap-[8px]">
        <h3 className="m-0 text-[10px] font-[650] uppercase tracking-[.06em] text-muted-foreground">
          Lifecycle
        </h3>
        <span className="text-[11px] text-muted-foreground">
          vocabulary from <code className="text-[10.5px]">task_lifecycle.py</code>; this task's
          position from its stored <code className="text-[10.5px]">status</code>
        </span>
      </div>

      {position.kind === "no_status" && (
        <p className="m-0 text-[12px] leading-[1.45] text-warn">
          <strong>No status stored</strong>{" "}
          <span className="text-muted-foreground">
            — this task has no position on the lifecycle at all, and nothing will pick it up on the
            strength of its status.
          </span>
        </p>
      )}

      {position.kind === "off_vocabulary" && (
        <>
          <p className="m-0 text-[12px] leading-[1.45] text-warn">
            <strong>
              Status <code className="text-[11px]">{position.raw}</code> is outside the lifecycle
            </strong>{" "}
            <span className="text-muted-foreground">
              — it is not one of the eleven states <code className="text-[10.5px]">TaskStatus</code>{" "}
              declares, so this task has no position on the path below and none is guessed for it.
            </span>
          </p>
          {/* The ungoverned consequence, surfaced rather than left in a
              docstring: an unknown origin state is exactly the case the
              transition check waves through. */}
          <p className="m-0 mt-[3px] text-[11px] leading-[1.4] text-muted-foreground">
            It is also <strong className="text-bad">ungoverned</strong>: {VALIDATION_NOTE.behaviour}{" "}
            So this task can move to any status at all, unchecked.
          </p>
          <p className="m-0 mt-[3px] text-[11px] leading-[1.4] text-muted-foreground">
            Not unusual — {WORKFLOW_LINKAGE_FACTS.measuredOn}, 17,251 of 21,285 tasks (81%) carried
            a status outside the eleven, mostly from imports. Reconciling the vocabulary is tracked
            separately.
          </p>
        </>
      )}

      {position.kind === "on_lifecycle" && (
        <>
          {/* THE PATH. Rendered in order, current position marked. */}
          <div className="flex flex-wrap items-center gap-[3px]">
            {PATH_ORDER.map((key, i) => {
              const here = position.stage.key === key;
              const passed = position.pathIndex !== null && i < position.pathIndex;
              return (
                <span key={key} className="flex items-center gap-[3px]">
                  {i > 0 && <span className="text-[10px] text-muted-foreground">→</span>}
                  <span
                    className={
                      here
                        ? "rounded-[4px] bg-[hsl(var(--live)/0.16)] px-[6px] py-[2px] font-mono text-[11px] font-[650] text-live"
                        : passed
                          ? "px-[3px] font-mono text-[11px] text-muted-foreground line-through"
                          : "px-[3px] font-mono text-[11px] text-muted-foreground"
                    }
                  >
                    {key}
                  </span>
                </span>
              );
            })}
          </div>

          {/* A hold or exit is BESIDE the path, so it is stated separately
              rather than drawn as another step in the sequence above. */}
          {position.pathIndex === null && (
            <p className="m-0 mt-[4px] text-[12px] leading-[1.45]">
              <span className="rounded-[4px] bg-[hsl(var(--warn)/0.16)] px-[6px] py-[2px] font-mono text-[11px] font-[650] text-warn">
                {position.stage.key}
              </span>{" "}
              <span className="text-muted-foreground">
                — {position.stage.kind === "hold" ? "a hold beside the path" : "a terminal ending"},
                not a step further along it.
                {position.stage.enteredFrom.length > 0 && (
                  <> Entered from {position.stage.enteredFrom.join(", ")}.</>
                )}
              </span>
            </p>
          )}

          <p className="m-0 mt-[4px] text-[12px] leading-[1.45]">
            <span className="text-muted-foreground">{position.stage.meaning}</span>
          </p>

          <p className="m-0 mt-[3px] text-[11px] leading-[1.4] text-muted-foreground">
            {position.next.length ? (
              <>
                From here it may move to{" "}
                {position.next.map((n, i) => (
                  <span key={n}>
                    {i > 0 && ", "}
                    <code className="text-[10.5px]">{n}</code>
                  </span>
                ))}
                . Moved out by: {position.stage.exit}
              </>
            ) : (
              <>
                Terminal — nothing moves a task out of{" "}
                <code className="text-[10.5px]">{position.stage.key}</code> automatically.
              </>
            )}
          </p>
        </>
      )}
    </section>
  );
}

/** Fetch which workflow, if any, relates to this task. */
function useWorkflowLink(id: string): { link: WorkflowLink | null; failed: boolean } {
  const [link, setLink] = useState<WorkflowLink | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    setLink(null);
    setFailed(false);
    (async () => {
      try {
        const res = await fetch(`/api/task-workflow?id=${encodeURIComponent(id)}`);
        const body: WorkflowLinkPayload = await res.json();
        if (!alive) return;
        if (!res.ok || body.error) throw new Error(body.error ?? `HTTP ${res.status}`);
        setLink(body.link ?? { kind: "none" });
      } catch {
        if (alive) setFailed(true);
      }
    })();
    return () => {
      alive = false;
    };
  }, [id]);

  return { link, failed };
}

/**
 * WHICH WORKFLOW RELATES TO THIS TASK — and, usually, the honest answer that
 * none does.
 *
 * THE RULE: no relationship is ever inferred to make this panel look complete.
 * A workflow guessed from tags or routing would render a confident answer that
 * nothing durably backs. Each dead end below is therefore a NAMED outcome
 * carrying its own reason, because "no issue was ever referenced" and "the
 * referenced issue was never stored" need different fixes.
 *
 * Showing the gap is the point. Measured 2026-09-02: `participation_record`,
 * the per-gate instance type, points at ZERO tasks (its 135 work entities are
 * 132 issues and 3 PRs), and no task in a 40-task sample carried any edge to an
 * issue. So an operator seeing "no workflow linked" on task after task is
 * seeing the actual state of the graph, which is what the gates work exists to
 * change.
 */
function WorkflowPanel({ id }: { id: string }) {
  const { link, failed } = useWorkflowLink(id);

  return (
    <section className="my-[10px] rounded-[7px] border bg-card px-[10px] py-[7px]">
      <div className="mb-[4px] flex items-baseline gap-[8px]">
        <h3 className="m-0 text-[10px] font-[650] uppercase tracking-[.06em] text-muted-foreground">
          Workflow
        </h3>
        <span className="text-[11px] text-muted-foreground">
          followed from stored links only — never inferred
        </span>
      </div>

      {failed ? (
        <p className="m-0 text-[11.5px] text-muted-foreground">
          Workflow linkage could not be read. The task's own fields are unaffected.
        </p>
      ) : link === null ? (
        <p className="m-0 text-[11.5px] text-muted-foreground">Resolving workflow…</p>
      ) : link.kind === "none" ? (
        <>
          <p className="m-0 text-[12px] leading-[1.45] text-warn">
            <strong>No workflow linked</strong>{" "}
            <span className="text-muted-foreground">
              — this task carries no reference to an issue or pull request, and nothing else
              connects a task to a workflow.
            </span>
          </p>
          <p className="m-0 mt-[3px] text-[11px] leading-[1.4] text-muted-foreground">
            This is the normal case rather than a fault in this task. As of{" "}
            {WORKFLOW_LINKAGE_FACTS.measuredOn}, all{" "}
            {WORKFLOW_LINKAGE_FACTS.distinctWorkEntities} work entities tracked by{" "}
            <code className="text-[10.5px]">participation_record</code> were issues and pull
            requests — <strong>{WORKFLOW_LINKAGE_FACTS.workEntitiesThatAreTasks} were tasks</strong>
            .
          </p>
        </>
      ) : link.kind === "dangling" ? (
        <>
          <p className="m-0 text-[12px] leading-[1.45] text-warn">
            <strong>
              Linked to {link.ref.repo}#{link.ref.number}, which has no entity
            </strong>{" "}
            <span className="text-muted-foreground">
              — the task stores this reference, but no {link.ref.isPullRequest ? "PR" : "issue"}{" "}
              entity exists for it, so its gates cannot be read.
            </span>
          </p>
          <p className="m-0 mt-[3px] text-[11px] leading-[1.4]">
            <a
              className="underline decoration-dotted underline-offset-2"
              href={link.ref.url}
              target="_blank"
              rel="noreferrer"
            >
              {link.ref.url}
            </a>
          </p>
          <p className="m-0 mt-[3px] text-[11px] leading-[1.4] text-muted-foreground">
            The link is real and stored; the target was never captured. Backfilling the issue
            entity would make this resolve without changing the task.
          </p>
        </>
      ) : link.kind === "issue_without_gates" ? (
        <p className="m-0 text-[12px] leading-[1.45]">
          <span className="text-muted-foreground">
            Linked to{" "}
            <a
              className="underline decoration-dotted underline-offset-2"
              href={link.ref.url}
              target="_blank"
              rel="noreferrer"
            >
              {link.ref.repo}#{link.ref.number}
            </a>
            , which exists but declares no gates — so there is no workflow position to show.
          </span>
        </p>
      ) : (
        <ResolvedWorkflow link={link} />
      )}
    </section>
  );
}

/**
 * A task whose workflow DID resolve.
 *
 * BOTH GATE-STATE SOURCES ARE SHOWN, and never merged. The issue's own
 * `gate_status` map and the `participation_record` rows are written by two
 * engines that do not read each other — graph-wide, participation reads
 * `dispatched` on 135 of 136 rows and `satisfied` on exactly one. Picking a
 * winner would present a settled answer where the graph holds two, so any gate
 * they disagree about is called out explicitly.
 */
function ResolvedWorkflow({ link }: { link: Extract<WorkflowLink, { kind: "resolved" }> }) {
  const disagreements = gateDisagreements(link);
  const participationByGate = new Map(link.participation.map((g) => [g.gateName, g.status]));
  const names = [
    ...new Set([...link.gates.map((g) => g.gateName), ...link.participation.map((g) => g.gateName)]),
  ];

  return (
    <>
      <p className="m-0 text-[12px] leading-[1.45]">
        <a
          className="underline decoration-dotted underline-offset-2"
          href={link.ref.url}
          target="_blank"
          rel="noreferrer"
        >
          {link.ref.repo}#{link.ref.number}
        </a>
        {link.workflowType && (
          <span className="text-muted-foreground">
            {" "}
            · <code className="text-[11px]">{link.workflowType}</code>
          </span>
        )}
        {link.currentOwner && (
          <span className="text-muted-foreground"> · owner {link.currentOwner}</span>
        )}
      </p>

      <table className="mt-[4px] w-full border-collapse text-[11.5px]">
        <thead>
          <tr className="text-[10px] uppercase tracking-[.05em] text-muted-foreground">
            <th className="py-[2px] pr-2 text-left font-[600]">Gate</th>
            <th className="py-[2px] pr-2 text-left font-[600]">issue.gate_status</th>
            <th className="py-[2px] text-left font-[600]">participation_record</th>
          </tr>
        </thead>
        <tbody>
          {names.map((name) => {
            const fromIssue = link.gates.find((g) => g.gateName === name)?.status ?? null;
            const fromPart = participationByGate.get(name) ?? null;
            const conflict = disagreements.includes(name);
            return (
              <tr key={name} className="border-b border-border/50 last:border-b-0">
                <td className="py-[2px] pr-2 align-baseline font-mono text-[11px]">{name}</td>
                <td
                  className={`py-[2px] pr-2 align-baseline font-mono text-[11px] ${conflict ? "text-bad" : ""}`}
                >
                  {fromIssue ?? <span className="text-muted-foreground">—</span>}
                </td>
                <td
                  className={`py-[2px] align-baseline font-mono text-[11px] ${conflict ? "text-bad" : ""}`}
                >
                  {fromPart ?? <span className="text-muted-foreground">—</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {disagreements.length > 0 && (
        <p className="m-0 mt-[3px] text-[11px] leading-[1.4] text-bad">
          Two engines write gate state and neither reads the other's, so{" "}
          {disagreements.length === 1 ? "this gate is" : "these gates are"} recorded differently by
          each: {disagreements.join(", ")}. Both are shown rather than one being chosen.
        </p>
      )}
    </>
  );
}

/**
 * `task`.
 *
 * `details` and `result` are deliberately distinguished, and the distinction is
 * shown rather than left to the reader: `details` holds an agent's
 * RECOMMENDATION (amber, labelled a suggestion) and `result` holds the
 * OPERATOR'S OWN ANSWER (green). Rendering a suggestion where an answer belongs
 * would read downstream as a decision the operator made. See `tasks.ts`.
 */
function TaskView({ snap, id }: { snap: Record<string, unknown>; id: string }) {
  const recommendation = str(snap.details)?.replace(/^\s*RECOMMENDATION:\s*/i, "") || null;
  const answer = str(snap.result);
  const description = str(snap.description);
  const context = str(snap.context);

  return (
    <>
      {/* State before stored prose: "can anything pick this up, and what has
          actually happened to it" is the question the operator opens a task
          with, and the description does not answer it. */}
      <TaskStatePanel snap={snap} id={id} />
      {/* WHERE it is, then WHAT WORKFLOW it belongs to — the operator's two
          questions, in the order he asked them, and both above the stored prose
          because neither is answerable from the description. */}
      <LifecyclePanel snap={snap} />
      <WorkflowPanel id={id} />
      {context && <Prose label="Context" text={context} />}
      {description && <Prose label="Description" text={description} />}
      {recommendation && (
        <Prose
          label="Recommendation"
          text={recommendation}
          tone="amber"
          note="An agent's suggested resolution — not a decision the operator has made."
        />
      )}
      {answer && (
        <Prose label="Result" text={answer} note="The operator's own answer." tone="green" />
      )}
      <GenericFields
        list={fields(snap, [
          "title",
          "status",
          "priority",
          "category",
          "description",
          "details",
          "result",
          "context",
          "assigned_to",
          "domain",
          "updated_date",
          "task_id",
        ])}
      />
    </>
  );
}

/**
 * `rendered_page`.
 *
 * The body IS retrievable here — `html_body` comes back through the proxy's
 * authenticated read, which is the whole reason these links stopped pointing at
 * Neotoma. It is rendered in a sandboxed iframe rather than injected into the
 * page: this is stored HTML from many different authors, and
 * `dangerouslySetInnerHTML` would let it restyle or script the dashboard around
 * it. `sandbox` with no `allow-scripts` renders the document inert.
 *
 * When no body is stored, that is stated plainly rather than shown as an empty
 * frame — see the fallback below.
 */
function RenderedPageView({ snap, id }: { snap: Record<string, unknown>; id: string }) {
  const html = str(snap.html_body);
  const css = str(snap.custom_css);
  const description = str(snap.meta_description) ?? str(snap.description);

  /**
   * COMPOSE THE DOCUMENT, don't display the parts.
   *
   * Verified against three real entities: `html_body` always holds the markup,
   * and `custom_css` is a SEPARATE field on some of them (ent_a7ca3c…, 6.5KB)
   * while others inline their own `<style>` at the top of `html_body`
   * (ent_aaabfb…, ent_9e2de5…). Rendering the two fields as separate blocks
   * showed the operator raw markup next to a wall of CSS instead of the page.
   *
   * So: wrap in a real document, put `custom_css` in a `<style>` in the head,
   * and let an inline `<style>` inside `html_body` apply on top of it. A page
   * that already carries its own styles is unaffected by the empty head.
   */
  const doc = html
    ? `<!doctype html><html><head><meta charset="utf-8">` +
      `<meta name="viewport" content="width=device-width,initial-scale=1">` +
      (css ? `<style>\n${css}\n</style>` : "") +
      `</head><body>${html}</body></html>`
    : null;

  return (
    <>
      {description && (
        <section className="my-[10px]">
          <span className="mb-[3px] block text-[10px] uppercase tracking-[.06em] text-muted-foreground">
            {snap.meta_description ? "Meta description" : "Description"}
          </span>
          <p className="m-0 text-[12.5px] leading-[1.45]">{description}</p>
        </section>
      )}

      {doc ? (
        <section className="my-[10px]">
          <div className="mb-[3px] flex items-baseline gap-[8px]">
            <span className="text-[10px] uppercase tracking-[.06em] text-muted-foreground">
              Page
            </span>
            <span className="text-[11px] text-muted-foreground">
              rendered from <code className="text-[11px]">html_body</code>
              {css ? (
                <>
                  {" + "}
                  <code className="text-[11px]">custom_css</code>
                </>
              ) : null}
              {" · the page supplies its own light/dark theming"}
            </span>
          </div>
          {/* Sandbox KEPT at `sandbox=""`: this is stored HTML from many
              different agent authors, and without the sandbox it could script
              or restyle the dashboard around it. CSS works fine under it — only
              scripts are blocked, and these pages ship none that runs anyway.

              Tall on purpose. These documents run 20–58KB; the density pass
              deliberately does NOT apply here, because a short frame around a
              long page is the failure this was fixing. It scrolls internally. */}
          <iframe
            title={`Rendered page ${id}`}
            srcDoc={doc}
            sandbox=""
            className="h-[82vh] min-h-[620px] w-full rounded-[7px] border bg-white"
          />
        </section>
      ) : (
        <div className="my-[18px] rounded-[10px] border border-[hsl(var(--warn)/0.26)] bg-[hsl(var(--warn)/0.08)] px-[14px] py-3">
          <p className="m-0 text-[13px] font-[550]">No page body stored on this entity</p>
          <p className="m-0 mt-[6px] text-[12px] leading-[1.55] text-muted-foreground">
            This <code>rendered_page</code> carries no <code>html_body</code> field, so there is
            nothing to display here. The published copy is viewable in Neotoma, which requires a
            signed-in session — an unauthenticated visit returns 401.
          </p>
        </div>
      )}

      {/* `custom_css` is omitted deliberately — it is now COMPOSED into the
          iframe above, and listing it again as a field would put the wall of
          raw CSS back on the page, which is exactly the complaint. */}
      <GenericFields
        list={fields(snap, [
          "title",
          "html_body",
          "custom_css",
          "meta_description",
          "description",
        ])}
      />
    </>
  );
}

/**
 * `plan`.
 *
 * The narrative lives in `body` (see the repo convention that plans always use
 * it), so that leads. `decisions`, `todos`, and `next_steps` are maps and lists
 * that keep their structure through GenericFields rather than being flattened.
 */
function PlanView({ snap }: { snap: Record<string, unknown> }) {
  const body = str(snap.body);
  const summary = str(snap.summary) ?? str(snap.overview);

  return (
    <>
      {summary && <Prose label={snap.summary ? "Summary" : "Overview"} text={summary} />}
      {body && <Prose label="Body" text={body} />}
      <GenericFields
        list={fields(snap, [
          "title",
          "status",
          "priority",
          "body",
          "summary",
          "overview",
          "repository_name",
          "repository_root",
        ])}
      />
    </>
  );
}

/** `agent_definition` — mirrors the directory's detail view ordering. */
function AgentView({ snap }: { snap: Record<string, unknown> }) {
  const description = str(snap.description);
  const notes = str(snap.notes);
  const prompt = str(snap.prompt_markdown);

  return (
    <>
      {description && (
        <section className="my-[10px]">
          <span className="mb-[3px] block text-[10px] uppercase tracking-[.06em] text-muted-foreground">
            Description
          </span>
          <p className="m-0 text-[12.5px] leading-[1.45]">{description}</p>
        </section>
      )}
      {notes && <Prose label="Notes" text={notes} />}
      {prompt && (
        <section className="my-[10px]">
          <span className="mb-[3px] block text-[10px] uppercase tracking-[.06em] text-muted-foreground">
            Prompt
          </span>
          {/* Runs to ~10k characters; scrolls in its own region so the metadata
              above it stays put. */}
          <ScrollArea className="max-h-[60vh] rounded-[10px] border bg-card">
            <div className="px-4">
              <Markdown source={prompt} />
            </div>
          </ScrollArea>
        </section>
      )}
      <GenericFields
        list={fields(snap, [
          "name",
          "title",
          "status",
          "tier",
          "genus",
          "version",
          "description",
          "notes",
          "prompt_markdown",
        ])}
      />
    </>
  );
}

/** `conversation` — a session's own record. */
function ConversationView({ snap }: { snap: Record<string, unknown> }) {
  const scope = str(snap.scope_summary);
  return (
    <>
      {scope && <Prose label="Scope summary" text={scope} />}
      <GenericFields
        list={fields(snap, [
          "title",
          "status",
          "scope_summary",
          "harness",
          "conversation_id",
          "session_id",
          "repository_name",
          "start_timestamp",
          "last_updated",
        ])}
      />
    </>
  );
}

/** `rendered_page_template` — the authoring rules for a page type. */
function TemplateView({ snap }: { snap: Record<string, unknown> }) {
  const purpose = str(snap.purpose);
  const coreRule = str(snap.core_rule);

  return (
    <>
      {purpose && <Prose label="Purpose" text={purpose} />}
      {coreRule && <Prose label="Core rule" text={coreRule} />}
      <GenericFields list={fields(snap, ["name", "title", "status", "purpose", "core_rule"])} />
    </>
  );
}

/* ------------------------------------------------------------------ */

/** Related entities, grouped by direction. Each row opens in the sheet. */
function Edges({
  outgoing,
  incoming,
  failed,
  onOpen,
}: {
  outgoing: EntityEdge[];
  incoming: EntityEdge[];
  failed: boolean;
  onOpen?: (id: string) => void;
}) {
  if (failed) {
    return (
      <section className="my-[10px]">
        <p className="m-0 text-[12px] leading-[1.5] text-muted-foreground">
          Relationships could not be read for this entity. Its own fields above are unaffected.
        </p>
      </section>
    );
  }

  if (!outgoing.length && !incoming.length) return null;

  const group = (label: string, edges: EntityEdge[], note: string) =>
    edges.length ? (
      <section className="my-[10px]">
        <div className="mb-[2px] flex items-baseline gap-[8px]">
          <span className="text-[10px] uppercase tracking-[.06em] text-muted-foreground">
            {label}
          </span>
          <span className="text-[11px] text-muted-foreground">{note}</span>
        </div>
        <ul className="m-0 list-none p-0">
          {edges.map((e) => {
            const s = unwrapSnapshot(e.snapshot);
            const name =
              str(s.title) ??
              str(s.name) ??
              (e.canonical_name
                ? e.canonical_name.replace(new RegExp(`^${e.entity_type}:`), "").trim()
                : null) ??
              e.entity_id;

            return (
              <li
                key={`${e.direction}-${e.entity_id}`}
                className="border-b border-border/60 last:border-b-0"
              >
                <button
                  type="button"
                  onClick={() => onOpen?.(e.entity_id)}
                  className="flex w-full items-baseline gap-[8px] px-[2px] py-[3px] text-left hover:bg-accent/60"
                >
                  <Badge variant="muted" className="flex-none font-mono text-[11px]">
                    {e.relationship_type}
                  </Badge>
                  <span className="min-w-0 flex-1 truncate text-[12.5px]">{name}</span>
                  {/* An unhydrated neighbour is a real, temporary state — the
                      edge is known, its target is not yet. Say so rather than
                      guessing at a type. */}
                  <span className="flex-none text-[11px] text-muted-foreground">
                    {e.entity_type ?? "loading…"}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      </section>
    ) : null;

  /*
   * BOTH DIRECTIONS, and the difference spelled out. Outbound says what this
   * entity is part of and depends on; inbound says what is part of IT (its
   * subtasks) and what depends on it (what it blocks) — which is the half a
   * reader is most likely to assume is missing rather than absent.
   *
   * Sourced from `GET /entities/<id>/relationships` via the proxy, never from
   * `list_relationships`: that returns empty for edges that demonstrably exist,
   * in both directions.
   */
  return (
    <>
      {group("References", outgoing, "what this is part of, depends on, or refers to")}
      {group("Referenced by", incoming, "what is part of this, or depends on it")}
    </>
  );
}

/**
 * THE SHARED DETAIL BODY.
 *
 * Both the full page and the sheet render this. `onOpenEntity` lets a related
 * row swap the sheet's subject in place (inspection without navigation); the
 * full page passes a navigate instead.
 */
export function EntityDetail({
  payload,
  error,
  firstLoadDone,
  onOpenEntity,
}: {
  payload: EntityPayload | null;
  error: string | null;
  firstLoadDone: boolean;
  onOpenEntity?: (id: string) => void;
}) {
  const record = payload?.entity ?? null;

  if (showSkeleton(!firstLoadDone, Boolean(record))) return <EntityDetailSkeleton />;

  if (error && !record) {
    return (
      <div className="my-4 rounded-lg border border-[hsl(var(--bad)/0.35)] bg-[hsl(var(--bad)/0.12)] px-3 py-[10px] text-[13px]">
        <strong>Cannot load this entity.</strong> {error}
      </div>
    );
  }

  if (!record) {
    return (
      <div className="my-4 rounded-[10px] border bg-card px-[14px] py-3 text-[13px] text-muted-foreground">
        No entity with that id.
      </div>
    );
  }

  const snap = unwrapSnapshot(record.snapshot);
  const type = record.entity_type;
  const status = str(snap.status);
  const priority = str(snap.priority);

  const body = (() => {
    switch (type) {
      case "task":
        return <TaskView snap={snap} id={record.entity_id} />;
      case "rendered_page":
        return <RenderedPageView snap={snap} id={record.entity_id} />;
      case "plan":
        return <PlanView snap={snap} />;
      case "agent_definition":
        return <AgentView snap={snap} />;
      case "conversation":
        return <ConversationView snap={snap} />;
      case "rendered_page_template":
        return <TemplateView snap={snap} />;
      default:
        // Never a blank page for an unrecognized type: every populated field
        // renders, humanized from its declared name.
        return <GenericFields list={fields(snap, ["title", "name", "status", "priority"])} />;
    }
  })();

  const populated = Object.keys(snap).filter((k) => k !== "canonical_name").length;

  return (
    <article>
      <header>
        <div className="flex flex-wrap items-center gap-[8px]">
          <Badge variant="muted" caps className="font-[650]">
            {typeLabel(type)}
          </Badge>
          {status && (
            <Badge variant={statusTone(status)} caps>
              {status}
            </Badge>
          )}
          {priority && <Badge variant="tag">{priority}</Badge>}
        </div>
        <h1 className="m-0 mt-[4px] text-[16px] font-[650] leading-[1.25] tracking-[-0.01em]">
          {entityTitle(record)}
        </h1>
        <p className="mb-0 mt-[2px] text-[11px] text-muted-foreground">
          <code className="text-[11px]">{record.entity_id}</code>
        </p>
      </header>

      <div className="my-[8px] flex flex-wrap gap-x-[18px] gap-y-[3px] rounded-[7px] border bg-card px-[10px] py-[6px]">
        <AssignedFact assignedTo={str(snap.assigned_to)} />
        <Fact label="Category" value={str(snap.category)} />
        <Fact label="Domain" value={str(snap.domain)} />
        <Fact label="Updated" value={str(snap.updated_date) ?? str(snap.last_updated)} />
      </div>

      {/* A snapshot with nothing in it is a real state — several tasks store
          only a canonical_name — and saying so beats an empty column. */}
      {populated === 0 && (
        <p className="my-[10px] text-[12.5px] text-muted-foreground">
          This entity's snapshot holds no populated fields beyond its name.
        </p>
      )}

      {body}

      <Edges
        outgoing={payload?.outgoing ?? []}
        incoming={payload?.incoming ?? []}
        failed={Boolean(payload?.relationshipsFailed)}
        onOpen={onOpenEntity}
      />

      <Separator className="my-[12px]" />

      {/* Kept as a SECONDARY action. It is no longer the way in, but removing
          it would strand the operator when he does want the canonical record —
          and it is honest about needing a signed-in session, since an
          unauthenticated visit returns 401. */}
      <p className="m-0 text-[13px]">
        <a
          href={entityUrl(record.entity_id)}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-[6px] text-muted-foreground no-underline hover:text-foreground hover:underline"
        >
          Open in Neotoma
          <ExternalLink className="h-[13px] w-[13px]" aria-hidden />
        </a>
        <span className="ml-2 text-[12px] text-muted-foreground">
          External · requires a signed-in Neotoma session
        </span>
      </p>
    </article>
  );
}

/** The full page at `#/entities/<id>`. Fetches its own entity, so a pasted URL loads cold. */
export function EntityPage({
  id,
  onOpenEntity,
}: {
  id: string;
  onOpenEntity?: (id: string) => void;
}) {
  const { payload, error, firstLoadDone } = useEntity(id);
  return (
    <EntityDetail
      payload={payload}
      error={error}
      firstLoadDone={firstLoadDone}
      onOpenEntity={onOpenEntity}
    />
  );
}
