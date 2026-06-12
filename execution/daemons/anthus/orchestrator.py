"""
Anthus orchestrator — workflow_definition-driven gate dispatcher.

Reads workflow_definition entities from Neotoma. When work entities
(issues, pull_requests, plans, tasks) match a workflow, Anthus tracks
which gates have been satisfied and dispatches the owner_agent for the
next ready gate(s).

Gate readiness:
  - phase N is ready when all required gates in phase N-1 have status
    "satisfied" OR have been skipped by a fast_path
  - within a phase, gates with the same parallel_group dispatch in parallel
  - a gate is "satisfied" when the owner_agent posts a satisfying
    artifact (comment, review, or completed sub-task) — what counts varies
    per gate_name and is encoded in GATE_SATISFACTION_RULES

This is a thin first pass. Phase 6 will replace gate sequencing with
contract-based emergent participation; see
docs/swarm_orchestration_emergent.md.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("anthus.orchestrator")

NEOTOMA_BASE_URL = os.environ.get("NEOTOMA_BASE_URL", "").rstrip("/")
_BEARER_ENV = "NEOTOMA_BEARER_TOKEN"  # gitleaks:allow — env var name, not a secret
NEOTOMA_BEARER = os.environ.get(_BEARER_ENV, "")  # gitleaks:allow


# ── Data shapes ───────────────────────────────────────────────────────────────


@dataclass
class Gate:
    """One step in a workflow_definition."""

    phase: int
    gate_name: str
    owner_agent: str
    parallel_group: str | None
    join_gate: str | None
    required: bool
    precondition: dict[str, Any] | None = None
    """
    Optional precondition for the gate to be considered dispatchable.
    Shape: `{"entity_type": "release_criteria", "scope_field": "project"}`
    means "there must exist an entity of type release_criteria whose
    `<scope_field>` value matches the project of the work entity."
    If the precondition is unmet, the gate is auto-skipped (analogous to
    a fast_path skip). Honored by `compute_ready_gates`.
    """


@dataclass
class WorkflowDefinition:
    entity_id: str
    project: str
    workflow_type: str
    description: str
    gates: list[Gate]
    fast_paths: list[
        dict[str, Any]
    ]  # e.g. [{"condition": "label:bug", "skip_gates": ["ux"]}]
    legal_required: bool

    def gates_by_phase(self) -> dict[int, list[Gate]]:
        phases: dict[int, list[Gate]] = {}
        for g in self.gates:
            phases.setdefault(g.phase, []).append(g)
        return phases

    def fast_path_skips(
        self,
        labels: set[str],
        work_entity: dict[str, Any] | None = None,
    ) -> set[str]:
        """
        Return gate_names skipped by any fast_path whose condition matches.

        Supported condition syntaxes:
          - `label:<name>` — true if `<name>` is in `labels`
          - `impact_score<N` / `<=` / `>=` / `>` / `==` — compares the work
            entity's `impact_score` field (0 if absent) against N
          - `audience:<value>` — true if work_entity.audience == value
            (e.g. "audience:internal" skips publicity gates)
        """
        if work_entity is None:
            work_entity = {}
        skips: set[str] = set()
        impact_score = _as_number(work_entity.get("impact_score"), default=0)
        audience = str(work_entity.get("audience", "")).lower()

        for fp in self.fast_paths:
            cond = str(fp.get("condition", "")).strip()
            matched = False
            if cond.startswith("label:"):
                matched = cond.split(":", 1)[1] in labels
            elif cond.startswith("audience:"):
                matched = audience == cond.split(":", 1)[1].lower()
            elif cond.startswith("impact_score"):
                matched = _eval_numeric_condition(cond, impact_score)
            if matched:
                skips.update(fp.get("skip_gates", []))
        return skips


def _as_number(value: Any, default: float = 0) -> float:
    """Coerce a value to float for comparison; default if uncoerceable."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


_NUMERIC_RE = re.compile(
    r"^(?P<field>[a-z_]+)\s*(?P<op><=|>=|==|<|>)\s*(?P<value>-?\d+(?:\.\d+)?)$"
)


def _eval_numeric_condition(cond: str, value: float) -> bool:
    """Evaluate a numeric fast_path condition like `impact_score < 5`."""
    m = _NUMERIC_RE.match(cond.replace(" ", ""))
    if not m:
        return False
    rhs = float(m.group("value"))
    op = m.group("op")
    if op == "<":
        return value < rhs
    if op == "<=":
        return value <= rhs
    if op == ">":
        return value > rhs
    if op == ">=":
        return value >= rhs
    if op == "==":
        return value == rhs
    return False


@dataclass
class GateState:
    """Tracks satisfaction of one gate for one work entity."""

    gate_name: str
    status: str  # "pending" | "dispatched" | "satisfied" | "skipped" | "failed"
    dispatched_at: str | None = None
    satisfied_at: str | None = None
    artifact_refs: list[str] = field(default_factory=list)


# ── Gate satisfaction rules ───────────────────────────────────────────────────
# Each rule maps gate_name to a predicate that examines comments/reviews on
# the work entity. Returns True if the owner_agent has produced a satisfying
# artifact. These are deliberately conservative — over-strict is recoverable
# by an operator nudge; under-strict creates false satisfaction.


def _comment_from_agent(comments: list[dict], agent_sub: str) -> dict | None:
    """Return the most recent comment whose author matches the agent's sub."""
    # Comments come via github_harness; we'll later record agent_sub directly.
    # For now, match against a soft heuristic on author name or comment body.
    for c in reversed(comments):
        author = str(c.get("author", "")).lower()
        body = str(c.get("body", "")).lower()
        if agent_sub.split("@")[0] in author or f"[{agent_sub.split('@')[0]}]" in body:
            return c
    return None


GATE_SATISFACTION_RULES: dict[str, str] = {
    # Maps gate_name → required artifact_type produced in a comment header.
    # The comment body is expected to begin with "[<agent>] <artifact_type>:".
    #
    # Two gate-name vocabularies coexist and BOTH must resolve, or the
    # orchestrator stalls forever at phase 1 (gate_name absent from this
    # map → _gate_satisfied_by_comment returns None → gate never satisfies):
    #   • short names — used by the ateles|* and swarm-smoke|*
    #     workflow_definitions (pm, ux, copy)
    #   • verbose names — used by the harness-sandbox
    #     smoke_test_full_lifecycle workflow (pm_scope, ux_design,
    #     growth_announce, social_draft, devrel_docs)
    # ── short names (ateles|*, swarm-smoke|*) ──
    "pm": "acceptance_criteria",
    "ux": "copy_and_ux_flow",
    "copy": "copy_and_ux_flow",
    # ── verbose names (harness-sandbox smoke_test_full_lifecycle) ──
    "pm_scope": "acceptance_criteria",
    "ux_design": "copy_and_ux_flow",
    "growth_announce": "launch_brief",
    "social_draft": "social_post_draft",
    "devrel_docs": "docs_diff_or_no_change_note",
    # ── shared names (identical in both vocabularies) ──
    "arch": "schema_or_api_proposal",
    "impl": "pull_request_link",
    "qa": "test_plan",
    "legal": "compliance_review",
    "compliance_supervisor": "compliance_verdict",
    "pr_review": "merge_decision",
    "release": "release_note",
}


def _gate_satisfied_by_comment(gate: Gate, comments: list[dict]) -> str | None:
    """
    Inspect comments for one that satisfies this gate. Returns the comment
    URL/id if satisfied, else None.

    Dual recognition (preferred → fallback):

    1. **Canonical header** — comment body starts with `[<agent>] <artifact_type>:`.
       Encoded in each agent's prompt_markdown per ateles#5. Strong signal
       because it includes the expected artifact type.

    2. **Author-only match** — comment author matches the agent's name (e.g.
       comment authored by `cicada-bot`, `ateles-agent`, or any string
       containing the agent's name). Weaker signal — confirms the agent
       commented, but not what artifact was produced.

    The fallback lets the orchestrator make progress even when agents
    drift from the canonical convention. To require the strong signal
    (e.g. for high-stakes gates), set the gate's `require_canonical_header`
    field to True in the workflow_definition (not yet schema-supported;
    Phase 6).
    """
    expected_artifact = GATE_SATISFACTION_RULES.get(gate.gate_name)
    if expected_artifact is None:
        return None

    agent_name = gate.owner_agent.lower()

    # 1. Canonical header — preferred.
    header_re = re.compile(
        rf"^\s*\[{re.escape(agent_name)}\]\s+{re.escape(expected_artifact)}\s*:",
        re.IGNORECASE | re.MULTILINE,
    )
    for c in comments:
        body = str(c.get("body", ""))
        if header_re.search(body):
            return str(c.get("url") or c.get("id") or "")

    # 2. Author-only fallback. Match `cicada`, `cicada-agent`, `cicada-bot`,
    #    `ateles-cicada`, etc. Avoid false positives by requiring the agent
    #    name to be a whole-word match in the author string.
    author_re = re.compile(rf"\b{re.escape(agent_name)}\b", re.IGNORECASE)
    for c in comments:
        author = str(c.get("author", ""))
        if author_re.search(author):
            return str(c.get("url") or c.get("id") or "")

    return None


# ── Neotoma fetchers ──────────────────────────────────────────────────────────


async def fetch_workflow_definitions(project: str) -> list[WorkflowDefinition]:
    """Fetch active workflow_definitions for a given project (e.g. 'ateles')."""
    if not NEOTOMA_BEARER:
        log.warning(f"{_BEARER_ENV} not set; orchestrator disabled.")
        return []

    headers = {
        "Authorization": f"Bearer {NEOTOMA_BEARER}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(headers=headers, timeout=15) as client:
        # NOTE: the entity-read route is POST /entities/query. The bare prod
        # HTTP server (localhost:3180) does NOT expose /retrieve_entities
        # (404) — that path only exists behind the MCP layer. /entities/query
        # returns the same {entities, total, limit, offset} shape with the
        # entity_type filter applied server-side and snapshots included.
        resp = await client.post(
            f"{NEOTOMA_BASE_URL}/entities/query",
            json={
                "entity_type": "workflow_definition",
                "limit": 100,
                "include_snapshots": True,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    out: list[WorkflowDefinition] = []
    for e in data.get("entities", []):
        snap = e.get("snapshot") or {}
        if snap.get("project") != project:
            continue
        if snap.get("status") != "active":
            continue
        gates = [
            Gate(
                phase=int(g.get("phase", 0)),
                gate_name=str(g.get("gate_name", "")),
                owner_agent=str(g.get("owner_agent", "")),
                parallel_group=g.get("parallel_group"),
                join_gate=g.get("join_gate"),
                required=bool(g.get("required", True)),
                precondition=g.get("precondition"),
            )
            for g in snap.get("gates", [])
        ]
        out.append(
            WorkflowDefinition(
                entity_id=str(e.get("entity_id", "")),
                project=str(snap.get("project", "")),
                workflow_type=str(snap.get("workflow_type", "")),
                description=str(snap.get("description", "")),
                gates=gates,
                fast_paths=list(snap.get("fast_paths", [])),
                legal_required=bool(snap.get("legal_required", False)),
            )
        )
    return out


async def resolve_unmet_preconditions(
    workflow: WorkflowDefinition,
    project: str,
) -> set[str]:
    """
    For each gate in `workflow` that declares a `precondition`, check whether
    the precondition is met by querying Neotoma. Returns the set of gate_names
    whose preconditions are NOT met (those gates will be skipped).

    Precondition shape (see Gate.precondition docstring):
      {"entity_type": "release_criteria", "scope_field": "project"}

    Meaning: an entity of `entity_type` must exist whose `<scope_field>`
    equals `project`.
    """
    unmet: set[str] = set()
    bearer = os.environ.get(_BEARER_ENV, "")
    if not bearer:
        log.warning(f"{_BEARER_ENV} not set; preconditions cannot be evaluated.")
        return unmet

    headers = {
        "Authorization": f"Bearer {bearer}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(headers=headers, timeout=15) as client:
        for g in workflow.gates:
            if not g.precondition:
                continue
            entity_type = g.precondition.get("entity_type")
            scope_field = g.precondition.get("scope_field", "project")
            if not entity_type:
                continue
            try:
                resp = await client.post(
                    f"{NEOTOMA_BASE_URL}/retrieve_entities",
                    json={
                        "entity_type": entity_type,
                        "limit": 50,
                        "include_snapshots": True,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                log.warning(f"Precondition check failed for gate {g.gate_name}: {exc}")
                unmet.add(g.gate_name)
                continue

            entities = data.get("entities", [])
            matched = any(
                str((e.get("snapshot") or {}).get(scope_field, "")).lower()
                == project.lower()
                for e in entities
            )
            if not matched:
                log.info(
                    f"Gate {g.gate_name} precondition unmet: no {entity_type} "
                    f"with {scope_field}={project}"
                )
                unmet.add(g.gate_name)

    return unmet


# ── Orchestration logic ───────────────────────────────────────────────────────


def select_workflow(
    work_entity: dict, workflows: list[WorkflowDefinition]
) -> WorkflowDefinition | None:
    """
    Pick the workflow_definition that applies to this work entity.

    Selection heuristic:
      1. Explicit override via label "workflow:<workflow_type>"
      2. workflow_type matches a label on the entity (e.g. label "bug" → workflow_type "bug")
      3. workflow_type "feature" as default for issues/plans with no other signal
    """
    labels: set[str] = set()
    raw_labels = work_entity.get("labels", [])
    if isinstance(raw_labels, str):
        raw_labels = [s.strip() for s in raw_labels.split(",") if s.strip()]
    for lbl in raw_labels or []:
        labels.add(str(lbl).lower())

    # 1. Explicit override
    for lbl in labels:
        if lbl.startswith("workflow:"):
            wt = lbl.split(":", 1)[1]
            for w in workflows:
                if w.workflow_type == wt:
                    return w

    # 2. Label-name match
    for w in workflows:
        if w.workflow_type.lower() in labels:
            return w

    # 3. Default to "feature" for issues
    for w in workflows:
        if w.workflow_type == "feature":
            return w

    return None


def compute_ready_gates(
    workflow: WorkflowDefinition,
    work_entity: dict,
    comments: list[dict],
    existing_state: dict[str, GateState] | None = None,
    unmet_preconditions: set[str] | None = None,
) -> tuple[dict[str, GateState], list[Gate]]:
    """
    Walk the workflow's phases. For each gate, determine status by checking
    comments. Return (updated_state, gates_ready_to_dispatch).

    A gate is "ready" iff:
      - it is currently "pending"
      - it is not skipped by a fast_path
      - its precondition is met (if declared); unmet preconditions auto-skip
      - every required gate in earlier phases is "satisfied" or "skipped"
      - its join_gate (if any) is already "satisfied"

    `unmet_preconditions` is the set of gate_names whose precondition the
    caller (anthus.py) has determined is unmet by querying Neotoma. Those
    gates are marked "skipped" so the workflow advances past them.
    """
    labels: set[str] = set()
    raw_labels = work_entity.get("labels", [])
    if isinstance(raw_labels, str):
        raw_labels = [s.strip() for s in raw_labels.split(",") if s.strip()]
    for lbl in raw_labels or []:
        labels.add(str(lbl).lower())

    skips = workflow.fast_path_skips(labels, work_entity=work_entity)
    if unmet_preconditions:
        skips = skips | unmet_preconditions

    state = dict(existing_state or {})
    # Initialize state for any unseen gates.
    for g in workflow.gates:
        if g.gate_name in state:
            continue
        if g.gate_name in skips:
            state[g.gate_name] = GateState(gate_name=g.gate_name, status="skipped")
        else:
            state[g.gate_name] = GateState(gate_name=g.gate_name, status="pending")

    # Update from comment evidence.
    for g in workflow.gates:
        gs = state[g.gate_name]
        if gs.status in ("satisfied", "skipped", "failed"):
            continue
        ref = _gate_satisfied_by_comment(g, comments)
        if ref:
            gs.status = "satisfied"
            gs.artifact_refs.append(ref)

    # Compute readiness phase-by-phase.
    ready: list[Gate] = []
    by_phase = workflow.gates_by_phase()
    for phase in sorted(by_phase.keys()):
        prior_satisfied = all(
            state[g.gate_name].status in ("satisfied", "skipped") or not g.required
            for p in by_phase
            if p < phase
            for g in by_phase[p]
        )
        if not prior_satisfied:
            break  # Don't look at later phases until earlier ones are done.
        for g in by_phase[phase]:
            gs = state[g.gate_name]
            if gs.status != "pending":
                continue
            # Honor join_gate within the same phase.
            if g.join_gate:
                # If join_gate is in the same phase and also pending, dispatch
                # both anyway (parallel group fires in parallel). We do NOT
                # block on a same-phase sibling; we DO block on a prior-phase
                # join.
                join = next(
                    (
                        x
                        for x in workflow.gates
                        if x.gate_name == g.join_gate and x.phase < phase
                    ),
                    None,
                )
                if join and state[join.gate_name].status not in (
                    "satisfied",
                    "skipped",
                ):
                    continue
            ready.append(g)

    return state, ready
