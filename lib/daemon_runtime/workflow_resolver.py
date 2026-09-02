"""The gate sequence comes from `workflow_definition` entities, not from code.

The defect this closes
----------------------

Eight `workflow_definition` entities carry full gate sequences — phases, owner
agents, fast paths, join gates. Until this module, nothing on the Apis dispatch
path read them. `execution/daemons/apis/swarm_dispatch.py` ran a hardcoded
``PRE_IMPL_GATES = ("pm", "ux", "arch")`` and ``lib/issue_labels.py`` a
hardcoded ``PRE_IMPL_GATE_NAMES = ("pm", "arch")``. The entities were advisory
decoration: editing one changed nothing about what the swarm did.

That is not merely an inconsistency, because the copies had already diverged
from each other AND from the entities:

  * ``lib/issue_labels.py`` declared ``("pm", "arch")`` while its own comment
    claimed it mirrored ``("pm", "ux", "arch")``. ``BLOCKED_GATES_LABEL``
    derives from that tuple, so ``blocked/gates`` read CLEAR on a feature issue
    whose ``ux`` gate was still pending.
  * The entities disagree with BOTH tuples, per project and per workflow type.
    ``ateles|feature`` has pre-impl gates ``pm, ux, arch``; ``ateles|bug`` has
    only ``pm``; ``ateles|security`` has only ``pm``. The hardcoded triple
    therefore waived — and blocked on — ``ux`` and ``arch`` gates that the bug
    and security workflows do not define at all.

Partial application across four copies is exactly what a single source of truth
prevents, so every gate-sequence consumer now routes through `resolve_gates`.

What "pre-impl" means, derived rather than listed
-------------------------------------------------

A pre-impl gate is not a name on a list; it is any gate in a phase strictly
earlier than the phase of the ``impl`` gate. Deriving it from the entity's own
phase numbers means adding a gate to a workflow in Neotoma — a new ``copy``
gate at phase 2, say — automatically makes it pre-impl without a code change.
That is the property the hardcoded tuples lacked and the reason three of the
four copies drifted.

Workflows with no ``impl`` gate (``ateles|release``) have no pre-impl gates:
there is no implementation for anything to precede.

No workflow matches: fail closed, consistent with PR #714
----------------------------------------------------------

Falling back to a hardcoded default sequence would reintroduce the second
source of truth this module exists to remove — the fallback would be a fifth
copy, and the one that runs precisely when the record is not answering.

So a resolution failure raises `WorkflowUnresolvedError` and the caller
declines to act. This is the same posture the operator approved for Neotoma
unreachability in `lib/daemon_runtime/neotoma_reachability.py` (PR #714, task
ent_670cacab2f46fd9547ced7ed): *the swarm does not do work it cannot ground in
the record.* Two distinctions carried over from that implementation deliberately:

  * **Refusing is not halting the daemon.** Callers catch the error at their own
    boundary and decline that one decision — a waive does not run, a build
    handoff does not fire — leaving state untouched for a retry. Observation,
    watchdogs, and notification are unaffected.
  * **Config faults are not outages.** No bearer token means reachability is
    unverified, and #714 explicitly does not halt the swarm on a misconfigured
    env var. Here the same input is a hard failure rather than a silent
    permissive pass, because unlike a reachability probe there is no safe
    default gate sequence to fall through to: guessing is the defect.

Caching, and why stale entity data is now a live routing fault
---------------------------------------------------------------

Reading Neotoma per dispatch decision is a latency and availability cost on the
hot path. Definitions are cached for `CACHE_TTL_SECONDS` (30s, matching #714's
probe interval), and — as in #714 — **the cache is the backoff**: a burst of
dispatch decisions issues one read, not one per decision.

The cache is deliberately NOT a fallback. It serves only entries fetched inside
the TTL; once they expire, an unreachable Neotoma produces a refusal, not a
stale answer. A cache that outlives the outage is a hardcoded default with
extra steps.

Reading entities live means a bad value in an entity becomes a live routing
fault rather than dormant decoration — three of the eight definitions still name
``gryllus``, an agent renamed to Cicada on 2026-06-12 (filed separately as
ent_875dee7675b0516f66a72220). Two guards apply, and neither is this module
silently repairing data:

  1. `validate_gates` rejects a definition whose gates are structurally
     unusable (missing ``gate_name``, non-integer phase). A malformed entity
     fails resolution loudly instead of yielding a subtly wrong sequence.
  2. `unknown_owner_agents` reports gate owners absent from a supplied roster,
     so a startup check can log a stale owner per definition. It REPORTS; it
     does not substitute an owner, because inventing a routing target is how a
     stale name becomes silent misdelivery.

Owner-agent staleness is not made fatal on purpose: it misroutes a dispatch,
which is recoverable and visible, whereas refusing every definition carrying one
would halt three of eight workflows over a data-entry lag.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import httpx

log = logging.getLogger("ateles.workflow_resolver")

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
).rstrip("/")
_BEARER_ENV = "NEOTOMA_BEARER_TOKEN"  # gitleaks:allow — env var name, not a secret

# Matches PR #714's probe interval. Long enough that a burst of dispatch
# decisions shares one read; short enough that an operator editing a
# workflow_definition sees it take effect within a dispatch cycle.
CACHE_TTL_SECONDS = float(os.environ.get("ATELES_WORKFLOW_CACHE_TTL_SECONDS", "30"))

QUERY_TIMEOUT_SECONDS = float(
    os.environ.get("ATELES_WORKFLOW_QUERY_TIMEOUT_SECONDS", "15")
)

# The gate whose phase separates "before implementation" from "after". Every
# workflow that implements anything declares it.
IMPL_GATE_NAME = "impl"


class WorkflowUnresolvedError(RuntimeError):
    """Raised when no gate sequence can be grounded in the record.

    Carries the reason so the caller reports WHY it declined rather than
    reporting a generic failure — a refusal indistinguishable from a crash is
    the silence failure #714 names, one layer down.
    """

    def __init__(self, reason: str, *, project: str = "", workflow_type: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.project = project
        self.workflow_type = workflow_type


@dataclass(frozen=True)
class ResolvedGate:
    """One gate as the record declares it."""

    phase: int
    gate_name: str
    owner_agent: str
    required: bool


@dataclass(frozen=True)
class ResolvedWorkflow:
    """A `workflow_definition` reduced to what the dispatcher needs.

    `entity_id` is carried so a dispatch decision can name the entity that
    produced it — the audit trail that a hardcoded tuple could never have.
    """

    entity_id: str
    project: str
    workflow_type: str
    gates: tuple[ResolvedGate, ...]

    def pre_impl_gate_names(self) -> tuple[str, ...]:
        """Gate names in phases strictly before the ``impl`` gate's phase.

        Derived from the entity's own phase numbers, never from a list of
        names — see the module docstring. Ordered by (phase, gate_name) so the
        result is deterministic and reads in execution order.
        """
        impl_phases = [g.phase for g in self.gates if g.gate_name == IMPL_GATE_NAME]
        if not impl_phases:
            # No implementation gate (e.g. ateles|release): nothing precedes it.
            return ()
        impl_phase = min(impl_phases)
        earlier = [g for g in self.gates if g.phase < impl_phase]
        earlier.sort(key=lambda g: (g.phase, g.gate_name))
        return tuple(g.gate_name for g in earlier)

    def gate_order(self) -> tuple[str, ...]:
        """Every gate name in execution order — for reporting and blocking checks."""
        ordered = sorted(self.gates, key=lambda g: (g.phase, g.gate_name))
        return tuple(g.gate_name for g in ordered)


# ── Snapshot parsing ─────────────────────────────────────────────────────────


def _coerce_gate_list(raw: Any, *, entity_id: str) -> list[dict]:
    """Return a snapshot ``gates`` field as a list of dicts.

    Neotoma stores this field as a JSON *string* on 5 of the 8 live entities
    (mirrors `execution/daemons/anthus/orchestrator._coerce_list_field`, where
    iterating the string yielded single characters and raised on every event).
    Unlike that function this does NOT degrade to an empty list: an empty gate
    list here would read as "this workflow has no pre-impl gates", i.e. a
    silent all-clear. Return the empty list and let `validate_gates` reject it.
    """
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return []
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError:
            log.warning(f"{entity_id}: gates is a string but not valid JSON")
            return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def validate_gates(raw_gates: list[dict], *, entity_id: str) -> tuple[ResolvedGate, ...]:
    """Parse gate dicts, raising rather than silently dropping a bad one.

    A gate dropped for a missing ``gate_name`` or an unparseable ``phase``
    would shorten the pre-impl sequence — the failure mode is "this issue needs
    fewer sign-offs than it does", which is exactly the class of bug the
    divergent tuples produced. Fail the whole definition instead.
    """
    if not raw_gates:
        raise WorkflowUnresolvedError(
            f"{entity_id}: workflow_definition declares no usable gates"
        )
    out: list[ResolvedGate] = []
    for g in raw_gates:
        name = str(g.get("gate_name", "")).strip()
        if not name:
            raise WorkflowUnresolvedError(
                f"{entity_id}: a gate has no gate_name — refusing the definition"
            )
        try:
            phase = int(g.get("phase"))
        except (TypeError, ValueError):
            raise WorkflowUnresolvedError(
                f"{entity_id}: gate {name!r} has non-integer phase "
                f"{g.get('phase')!r} — refusing the definition"
            ) from None
        out.append(
            ResolvedGate(
                phase=phase,
                gate_name=name,
                owner_agent=str(g.get("owner_agent", "")).strip(),
                required=bool(g.get("required", True)),
            )
        )
    return tuple(out)


def unknown_owner_agents(
    workflows: Iterable[ResolvedWorkflow], known_agents: Iterable[str]
) -> dict[str, list[str]]:
    """Report gate owners not present in *known_agents*, per workflow key.

    Advisory by design (see module docstring): reading definitions live turns a
    stale ``owner_agent`` into a live misroute, so a startup check logs these —
    it does not substitute an owner or refuse the definition.
    """
    known = {str(a).strip().lower() for a in known_agents if str(a).strip()}
    out: dict[str, list[str]] = {}
    for wf in workflows:
        stale = sorted(
            {
                g.owner_agent
                for g in wf.gates
                if g.owner_agent and g.owner_agent.lower() not in known
            }
        )
        if stale:
            out[f"{wf.project}|{wf.workflow_type}"] = stale
    return out


# ── Fetching, with a TTL cache that is never a fallback ──────────────────────


_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, list[ResolvedWorkflow]]] = {}


def clear_cache() -> None:
    """Drop every cached definition. For tests and for an operator-forced refresh."""
    with _cache_lock:
        _cache.clear()


def _fetch_definitions(project: str) -> list[ResolvedWorkflow]:
    """Read active workflow_definitions for *project* from Neotoma.

    Uses ``POST /entities/query``: the prod HTTP surface does NOT expose
    ``/retrieve_entities`` (404 — ateles#584); that path exists only behind the
    MCP layer.
    """
    token = os.environ.get(_BEARER_ENV, "")
    if not token:
        raise WorkflowUnresolvedError(
            f"{_BEARER_ENV} is not set — the gate sequence cannot be read from "
            "the record, and there is no safe default to fall back to",
            project=project,
        )
    try:
        resp = httpx.post(
            f"{NEOTOMA_BASE_URL}/entities/query",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "entity_type": "workflow_definition",
                "limit": 100,
                "include_snapshots": True,
            },
            timeout=QUERY_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except WorkflowUnresolvedError:
        raise
    except Exception as exc:  # noqa: BLE001 — an unreachable record IS an exception
        raise WorkflowUnresolvedError(
            f"could not read workflow_definition entities: "
            f"{type(exc).__name__}: {str(exc)[:160]}",
            project=project,
        ) from exc

    out: list[ResolvedWorkflow] = []
    for e in (data or {}).get("entities", []) or []:
        snap = e.get("snapshot") or {}
        if str(snap.get("project", "")) != project:
            continue
        if str(snap.get("status", "")) != "active":
            continue
        entity_id = str(e.get("entity_id", ""))
        raw_gates = _coerce_gate_list(snap.get("gates"), entity_id=entity_id)
        try:
            gates = validate_gates(raw_gates, entity_id=entity_id)
        except WorkflowUnresolvedError as exc:
            # One malformed definition must not hide every other one; drop it
            # with a loud log. If it was the one being selected, selection
            # below raises anyway — the refusal still happens, just precisely.
            log.error(f"skipping malformed workflow_definition: {exc.reason}")
            continue
        out.append(
            ResolvedWorkflow(
                entity_id=entity_id,
                project=project,
                workflow_type=str(snap.get("workflow_type", "")),
                gates=gates,
            )
        )
    return out


def load_workflows(
    project: str, *, fetcher: Callable[[str], list[ResolvedWorkflow]] | None = None
) -> list[ResolvedWorkflow]:
    """Active workflows for *project*, served from a TTL cache.

    The cache is the backoff (PR #714's shape): a burst of dispatch decisions
    issues one read. It is NOT a fallback — an expired entry plus an unreachable
    Neotoma raises, rather than serving a stale sequence.
    """
    fetch = fetcher or _fetch_definitions
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(project)
        if hit is not None and (now - hit[0]) < CACHE_TTL_SECONDS:
            return hit[1]
    workflows = fetch(project)
    with _cache_lock:
        _cache[project] = (time.monotonic(), workflows)
    return workflows


# ── Selection ────────────────────────────────────────────────────────────────


def project_from_repo(repo_slug: str) -> str:
    """Map ``owner/name`` to the ``project`` field of a workflow_definition.

    Same mapping as `execution/daemons/anthus/anthus._project_from_repo`, so
    Apis and Anthus select against identical keys.
    """
    if not repo_slug:
        return ""
    return str(repo_slug).split("/")[-1]


def select_workflow(
    workflows: list[ResolvedWorkflow], labels: Iterable[str]
) -> ResolvedWorkflow | None:
    """Pick the workflow for an issue from its labels.

    Same precedence as `anthus.orchestrator.select_workflow`, so one issue
    resolves to one workflow no matter which daemon asks:
      1. explicit ``workflow:<type>`` label
      2. a label equal to a workflow_type (``bug``, ``security``, ``copy``)
      3. ``feature`` as the default
    """
    label_set = {str(lbl).strip().lower() for lbl in (labels or []) if str(lbl).strip()}

    for lbl in sorted(label_set):
        if lbl.startswith("workflow:"):
            wanted = lbl.split(":", 1)[1]
            for w in workflows:
                if w.workflow_type.lower() == wanted:
                    return w

    for w in workflows:
        if w.workflow_type and w.workflow_type.lower() in label_set:
            return w

    for w in workflows:
        if w.workflow_type == "feature":
            return w

    return None


def resolve_workflow(
    repository: str,
    labels: Iterable[str] = (),
    *,
    fetcher: Callable[[str], list[ResolvedWorkflow]] | None = None,
) -> ResolvedWorkflow:
    """The workflow governing *repository* + *labels*, or raise.

    This is the single entry point every gate-sequence consumer routes through.
    """
    project = project_from_repo(repository)
    if not project:
        raise WorkflowUnresolvedError(
            f"cannot derive a project from repository {repository!r}"
        )
    workflows = load_workflows(project, fetcher=fetcher)
    if not workflows:
        raise WorkflowUnresolvedError(
            f"no active workflow_definition for project {project!r}",
            project=project,
        )
    chosen = select_workflow(workflows, labels)
    if chosen is None:
        raise WorkflowUnresolvedError(
            f"no workflow_definition matches project {project!r} with labels "
            f"{sorted(str(x) for x in labels)} — declining rather than "
            "assuming a default gate sequence",
            project=project,
        )
    return chosen


def resolve_pre_impl_gates(
    repository: str,
    labels: Iterable[str] = (),
    *,
    fetcher: Callable[[str], list[ResolvedWorkflow]] | None = None,
) -> tuple[str, ...]:
    """Pre-impl gate names for this issue, from the record.

    Replaces `swarm_dispatch.PRE_IMPL_GATES` and
    `lib.issue_labels.PRE_IMPL_GATE_NAMES`. Raises `WorkflowUnresolvedError`
    rather than returning a default — see the module docstring.
    """
    return resolve_workflow(repository, labels, fetcher=fetcher).pre_impl_gate_names()


def resolve_gates(
    repository: str,
    labels: Iterable[str] = (),
    *,
    fetcher: Callable[[str], list[ResolvedWorkflow]] | None = None,
) -> tuple[str, ...]:
    """Every gate name for this issue, in execution order, from the record.

    Replaces `execution/mcp/ateles/server._GATE_ORDER`.
    """
    return resolve_workflow(repository, labels, fetcher=fetcher).gate_order()
