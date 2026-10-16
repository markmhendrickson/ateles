/**
 * WHO OWNS THIS TASK — one rendering, used everywhere a task appears.
 *
 * The task list, the session page, the entity sheet, and the full entity page
 * all have to answer the same question, and answering it three different ways
 * is how three different answers appear. So this component is the single
 * renderer, and every surface imports it.
 *
 * THREE STATES, and the middle one is the point:
 *
 *   - UNOWNED — `assigned_to` is unset. Rendered as the word "undispatched" in
 *     amber, never as a blank cell: an empty space reads as missing data rather
 *     than as work nobody has picked up.
 *   - OWNED BY A ROLE APIS CAN SPAWN — green, and a LINK to that agent's page.
 *   - OWNED BY A ROLE NOTHING CAN SPAWN — red, flagged. This is a real defect
 *     that is currently invisible: the task looks dispatched, and no daemon will
 *     ever pick it up. Measured on a 500-task sample, 11 of the 32 assigned
 *     tasks name a role outside Apis's route table — `corvus`, `pavo`,
 *     `Bombycilla`, `waxwing`, `ciconia`, `Luscinia`, `operator` — so this is
 *     the common case among assigned work, not a corner.
 *
 * THE LINK IS CONDITIONAL ON THE ROSTER, not on spawnability. `assigned_to`
 * holds a role NAME, not an entity id, so the link only exists where an
 * `agent_definition` carries that name. `operator` has none and is correctly
 * rendered as plain text — a dead link would be worse than no link.
 */
import { agentIdForRole, DISPATCHABLE_ROLES, isSpawnable } from "./taskState";
import { cn } from "@/lib/utils";

/** The minimum an agent must supply to be linkable. */
export interface AgentRef {
  id: string;
  name: string;
}

interface Props {
  /** Raw `assigned_to` as stored. Null or empty means unowned. */
  assignedTo: string | null;
  /** The roster, for name -> entity id resolution. Empty disables linking. */
  agents: AgentRef[];
  /** Open an agent's detail page. Omit to render the name unlinked. */
  onOpenAgent?: (id: string) => void;
  /** Compact form for dense table rows: no "unspawnable" suffix, colour only. */
  compact?: boolean;
  className?: string;
}

export function AssignedTo({ assignedTo, agents, onOpenAgent, compact, className }: Props) {
  const owner = assignedTo?.trim();

  if (!owner) {
    return (
      <span
        className={cn("text-warn", className)}
        title="assigned_to is empty — nothing can dispatch this task"
      >
        undispatched
      </span>
    );
  }

  const spawnable = isSpawnable(owner);
  const agentId = agentIdForRole(owner, agents);

  const tone = spawnable ? "text-live" : "text-bad";
  const title = spawnable
    ? `${owner} is one of the ${DISPATCHABLE_ROLES.length} roles Apis can spawn`
    : `${owner} is not one of the ${DISPATCHABLE_ROLES.length} roles in Apis's route table ` +
      `(${DISPATCHABLE_ROLES.join(", ")}), so nothing will pick this task up`;

  const label = (
    <>
      {owner}
      {!spawnable && !compact && (
        <span className="ml-[4px] text-[10.5px] uppercase tracking-[.04em]">unspawnable</span>
      )}
    </>
  );

  // Linked only where the roster actually resolves the name. The click is
  // stopped from propagating because these rows are themselves click targets
  // that open the TASK — without this, opening the agent would also open the
  // task's sheet behind it.
  if (agentId && onOpenAgent) {
    return (
      <button
        type="button"
        title={title}
        onClick={(e) => {
          e.stopPropagation();
          onOpenAgent(agentId);
        }}
        className={cn("cursor-pointer border-none bg-transparent p-0 hover:underline", tone, className)}
      >
        {label}
      </button>
    );
  }

  return (
    <span className={cn(tone, className)} title={title}>
      {label}
    </span>
  );
}
