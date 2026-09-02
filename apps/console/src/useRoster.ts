/**
 * THE SWARM ROSTER, fetched once and shared.
 *
 * Four surfaces need to turn an `assigned_to` role name into a link to that
 * agent's page — the task list, the session page, the entity sheet, and the
 * full entity page — and each fetching `/api/agents` for itself would be four
 * requests for one small, rarely-changing table.
 *
 * MODULE-LEVEL CACHE, deliberately. The roster is 40 rows of names and ids that
 * change a few times a year; re-reading it on every mount would be pure cost.
 * A page reload clears it, which is the same contract the proxy's entity cache
 * has.
 *
 * FAILURE IS NON-FATAL AND SILENT. This resolves links, nothing more: if it
 * cannot load, `AssignedTo` renders the owner as plain text and every other
 * fact on screen is unaffected. A visible error here would be louder than the
 * thing it degrades.
 */
import { useEffect, useState } from "react";
import { type AgentEntity, parseAgent } from "./agents";
import type { AgentRef } from "./AssignedTo";

let cache: AgentRef[] | null = null;
let inFlight: Promise<AgentRef[]> | null = null;

async function load(): Promise<AgentRef[]> {
  if (cache) return cache;
  if (inFlight) return inFlight;

  inFlight = (async () => {
    try {
      const res = await fetch("/api/agents?limit=200");
      const body = await res.json();
      if (!res.ok || body.error) throw new Error(body.error ?? `HTTP ${res.status}`);
      const roster = (body.entities as AgentEntity[])
        .map(parseAgent)
        .map((a) => ({ id: a.id, name: a.name }));
      cache = roster;
      return roster;
    } catch {
      // Leave the cache unset so a later mount retries.
      return [];
    } finally {
      inFlight = null;
    }
  })();

  return inFlight;
}

/** The roster, or an empty array until it arrives. Never throws. */
export function useRoster(): AgentRef[] {
  const [agents, setAgents] = useState<AgentRef[]>(() => cache ?? []);

  useEffect(() => {
    if (cache) return;
    let alive = true;
    void load().then((r) => {
      if (alive) setAgents(r);
    });
    return () => {
      alive = false;
    };
  }, []);

  return agents;
}
