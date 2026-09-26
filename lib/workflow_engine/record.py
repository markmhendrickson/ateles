"""
lib/workflow_engine/record.py — the record seam the engine reads through.

The engine never talks HTTP directly. It calls a `RecordClient`, so the same
declaration-reading and hydration logic runs against:

  * `FakeRecordClient` — an in-memory double, used by every test in this
    package. No dev Neotoma instance is configured anywhere in this repo
    (only `NEOTOMA_ENV=production` exists), and the conformance suite's own
    disposable instance (#921) is itself unbuilt, so this is the only
    reachable target for a first slice that must not write prod.
  * a real client added later (#921's disposable instance, or a genuine dev
    instance if one is ever provisioned) that implements the same protocol
    against Neotoma's HTTP API.

`Unknown` is a real return value, never a stand-in for empty. Conflating the
two is exactly GW-11's row ("`unknown` distinct from empty at every read")
and GW-42's row (an unreadable workflow must not be read as an empty step
tuple). Python's `None` is not used for this because `None` is what a
present-but-empty read would also produce in careless code — a distinct
sentinel type makes the two cases impossible to confuse by accident.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class Unknown:
    """
    Sentinel: the read could not be performed (unreachable record, failing
    proxy, etc.) — as opposed to a read that succeeded and found nothing.

    A single shared instance (`UNKNOWN`) is exported below; identity
    comparison (`is UNKNOWN`) is how callers test for it, the same pattern
    Python's own `None` uses.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "UNKNOWN"


UNKNOWN = Unknown()


class Empty:
    """Sentinel: the read succeeded and found zero rows."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "EMPTY"


EMPTY = Empty()


@dataclass(frozen=True)
class WorkflowDeclaration:
    """
    The declared shape of a workflow: its steps, in order, each naming the
    types it reads to enter. This is deliberately narrower than Anthus's
    `WorkflowDefinition` (orchestrator.py) — that dataclass carries the
    old gate/phase/fast_path model this engine does not use. A later slice
    that needs gate-parity fields extends this rather than importing the
    Anthus one, per the spec's "extends, not replaces" framing applied at
    the type level too: the *reader* moves into this package, not the old
    shape.
    """

    entity_id: str
    declaration_scope: str
    workflow_type: str
    steps: tuple["StepDeclaration", ...]

    def step_by_name(self, name: str) -> "StepDeclaration | None":
        for step in self.steps:
            if step.name == name:
                return step
        return None


@dataclass(frozen=True)
class StepDeclaration:
    name: str
    owner_role: str
    reads_to_enter: tuple[str, ...] = ()
    successors: tuple[str, ...] = ()
    none_permitted: bool = False


@dataclass(frozen=True)
class CheckpointRaised:
    """What the engine recorded when it raised a checkpoint on a task."""

    task_id: str
    reason: str
    needed_input: str | None = None


class RecordClient(Protocol):
    """
    The seam. A real implementation performs each of these against Neotoma
    (or a disposable/dev instance); the fake below performs them against a
    dict. Every method returns `UNKNOWN` on an unreadable target rather than
    raising or returning an empty collection — the caller (the engine) is
    the only place that decides what an unreadable read means.
    """

    def get_workflow_declaration(
        self, declaration_scope: str, workflow_type: str
    ) -> WorkflowDeclaration | Unknown:
        ...

    def read_rows(self, entity_type: str, *, scope: str) -> list[dict] | Empty | Unknown:
        ...

    def raise_checkpoint(
        self, task_id: str, reason: str, *, needed_input: str | None = None
    ) -> CheckpointRaised:
        ...

    def open_checkpoints_for_task(self, task_id: str) -> list[CheckpointRaised]:
        ...


@dataclass
class FakeRecordClient:
    """
    In-memory double implementing `RecordClient`. Used by every test in this
    package; never points at a real Neotoma instance of any kind.

    Failure injection: set `unreadable_workflows` to a set of
    `(declaration_scope, workflow_type)` pairs whose declaration read should
    return `UNKNOWN` (simulating `RP` failing the workflow read per GW-42's
    own fixture, `T-routed(feature)` with `RP` failing reads). Set
    `unreadable_read_types` to a set of entity_type strings whose
    `read_rows` call should return `UNKNOWN` (simulating an unreadable
    `reads_to_enter` dependency).
    """

    declarations: dict[tuple[str, str], WorkflowDeclaration] = field(default_factory=dict)
    rows: dict[str, list[dict]] = field(default_factory=dict)
    unreadable_workflows: set[tuple[str, str]] = field(default_factory=set)
    unreadable_read_types: set[str] = field(default_factory=set)
    checkpoints: list[CheckpointRaised] = field(default_factory=list)
    _checkpoint_seq: int = 0

    def register_declaration(self, decl: WorkflowDeclaration) -> None:
        self.declarations[(decl.declaration_scope, decl.workflow_type)] = decl

    def seed_rows(self, entity_type: str, rows: list[dict]) -> None:
        self.rows[entity_type] = rows

    def get_workflow_declaration(
        self, declaration_scope: str, workflow_type: str
    ) -> WorkflowDeclaration | Unknown:
        key = (declaration_scope, workflow_type)
        if key in self.unreadable_workflows:
            return UNKNOWN
        decl = self.declarations.get(key)
        if decl is None:
            return UNKNOWN
        return decl

    def read_rows(self, entity_type: str, *, scope: str) -> list[dict] | Empty | Unknown:
        if entity_type in self.unreadable_read_types:
            return UNKNOWN
        found = self.rows.get(entity_type, [])
        if not found:
            return EMPTY
        return list(found)

    def raise_checkpoint(
        self, task_id: str, reason: str, *, needed_input: str | None = None
    ) -> CheckpointRaised:
        cp = CheckpointRaised(task_id=task_id, reason=reason, needed_input=needed_input)
        self.checkpoints.append(cp)
        return cp

    def open_checkpoints_for_task(self, task_id: str) -> list[CheckpointRaised]:
        return [c for c in self.checkpoints if c.task_id == task_id]
