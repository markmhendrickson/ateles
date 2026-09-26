"""
lib/workflow_engine/record.py — the record seam the engine reads through.

The engine never talks HTTP directly. It calls a `RecordReader` for reads
and a `CheckpointWriter` for the one write this slice performs. So the same
declaration-reading and hydration logic runs against:

  * `FakeRecordClient` — an in-memory double, used by every test in this
    package. No dev Neotoma instance is configured anywhere in this repo
    (only `NEOTOMA_ENV=production` exists), and the conformance suite's own
    disposable instance (#921) is itself unbuilt, so this is the only
    reachable target for a first slice that must not write prod.
  * a real client added later (#921's disposable instance, or a genuine dev
    instance if one is ever provisioned) that implements the same protocols
    against Neotoma's HTTP API.

`Unknown` is a real return value, never a stand-in for empty. Conflating the
two is exactly GW-11's row ("`unknown` distinct from empty at every read")
and GW-42's row (an unreadable workflow must not be read as an empty step
tuple). Python's `None` is not used for this because `None` is what a
present-but-empty read would also produce in careless code — a distinct
sentinel type makes the two cases impossible to confuse by accident.

Read/write split (pm + qa review on PR #1310, 2026-09-26): `RecordClient`
used to bundle reads and the checkpoint write in one protocol, which meant
"this PR only has a fake implementation" was the entire production-write
boundary — nothing in the type system stopped a caller from handing
`open_steps()` a client wired to a real, production base URL. That is now
structural: `RecordReader` carries no write method at all, and the only
write path (`CheckpointWriter.raise_checkpoint`) must be reached through
`NonProductionCheckpointWriter`, which refuses — by raising, before any
write is attempted — unless the wrapped writer declares a non-production
`instance_label`. `open_steps()` (engine.py) type-hints its `checkpoints`
parameter as `NonProductionCheckpointWriter`, not as any writer, so passing
a bare writer is a type error as well as a runtime refusal.
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
    """
    What the engine recorded when it raised a checkpoint on a task.

    `idempotency_key` names the logical halt this checkpoint represents —
    e.g. "unreadable_workflow:<task_id>:<declaration_scope>:<workflow_type>".
    A second `raise_checkpoint` call with the same key must return the
    already-open checkpoint rather than create a second one (pm/qa
    finding 1 on PR #1310: repeated polling of a still-unreadable workflow
    must not multiply checkpoints).
    """

    task_id: str
    reason: str
    idempotency_key: str
    needed_input: str | None = None


class RecordReader(Protocol):
    """
    The read-only half of the seam. Carries no write method — structurally,
    not by convention, so nothing that only holds a `RecordReader` can
    write anything, checkpoints included. Every method returns `UNKNOWN` on
    an unreadable target rather than raising or returning an empty
    collection — the caller (the engine) is the only place that decides
    what an unreadable read means.
    """

    def get_workflow_declaration(
        self, declaration_scope: str, workflow_type: str
    ) -> WorkflowDeclaration | Unknown:
        ...

    def read_rows(self, entity_type: str, *, scope: str) -> list[dict] | Empty | Unknown:
        ...


class CheckpointWriter(Protocol):
    """
    The one write this slice performs. Never handed to `open_steps()`
    directly — always wrapped in `NonProductionCheckpointWriter` below,
    which is the actual parameter type `open_steps()` declares. A raw
    `CheckpointWriter` is for a test double or a future real adapter to
    implement; it is not itself a safe thing to give the engine.
    """

    def raise_checkpoint(
        self,
        task_id: str,
        reason: str,
        *,
        idempotency_key: str,
        needed_input: str | None = None,
    ) -> CheckpointRaised:
        ...

    def open_checkpoints_for_task(self, task_id: str) -> list[CheckpointRaised]:
        ...


class ProductionWriteRefused(RuntimeError):
    """
    Raised by `NonProductionCheckpointWriter` when the wrapped writer's
    `instance_label` is not on the allowed (non-production) list, or is
    missing. This is the structural half of pm/qa finding 2: "cannot write
    to prod" must hold even if a real `CheckpointWriter` implementation is
    later added and someone constructs it against a production URL — the
    guard raises before the underlying `raise_checkpoint` is ever called,
    it does not rely on the fake being the only implementation that exists.
    """


# Labels a writer may declare that this guard accepts. Deliberately an
# allow-list, not a deny-list keyed on "not literally containing the word
# production" — the fail-closed posture the corpus's verification-
# discipline section requires for any field carrying safety meaning
# (CLAUDE.md "Fail closed on the field that carries the safety meaning").
# An unrecognized or absent label refuses, it does not default-permit.
_ALLOWED_INSTANCE_LABELS = frozenset({"test-double", "disposable"})


@dataclass
class NonProductionCheckpointWriter:
    """
    Wraps a `CheckpointWriter` and refuses every write unless the wrapped
    writer's `instance_label` is on `_ALLOWED_INSTANCE_LABELS`. This is the
    only way `open_steps()` is willing to accept a checkpoint writer
    (engine.py type-hints its parameter as this class, not as
    `CheckpointWriter`), so a caller cannot route around the check by
    skipping the wrapper — there is no other constructor path into
    `open_steps()`'s write side.

    The check runs once at construction (fail fast, not at first write) and
    again is not re-derived per call — `instance_label` is read once from
    the wrapped writer and frozen, so a writer that mutates its own label
    after construction cannot flip an already-approved wrapper into an
    unapproved one or vice versa; a new label requires a new wrapper.
    """

    writer: CheckpointWriter
    instance_label: str

    def __post_init__(self) -> None:
        if self.instance_label not in _ALLOWED_INSTANCE_LABELS:
            raise ProductionWriteRefused(
                f"refusing to accept a checkpoint writer labelled "
                f"{self.instance_label!r}: not one of "
                f"{sorted(_ALLOWED_INSTANCE_LABELS)}. This slice must not "
                f"write to production or any unrecognized instance."
            )

    def raise_checkpoint(
        self,
        task_id: str,
        reason: str,
        *,
        idempotency_key: str,
        needed_input: str | None = None,
    ) -> CheckpointRaised:
        return self.writer.raise_checkpoint(
            task_id, reason, idempotency_key=idempotency_key, needed_input=needed_input
        )

    def open_checkpoints_for_task(self, task_id: str) -> list[CheckpointRaised]:
        return self.writer.open_checkpoints_for_task(task_id)


def guess_instance_label(base_url: str | None) -> str:
    """
    Classify a base URL as an `instance_label` for the guard above. Used by
    a future real `CheckpointWriter` implementation to self-label rather
    than trusting a caller-supplied string — but the guard itself never
    calls this; it only ever trusts the label the writer declares at
    construction, so this helper is offered for that writer to use on
    itself, not a bypass path.

    Fail-closed: anything not recognisably a disposable/test target reads
    as "production" — including `None`, an empty string, and localhost,
    since a local dev server can still be pointed at a copied-down
    production database. There is deliberately no "unknown -> allowed"
    branch.
    """
    if not base_url:
        return "production"
    lowered = base_url.lower()
    if "disposable" in lowered or "conformance" in lowered:
        return "disposable"
    return "production"


@dataclass
class FakeRecordClient:
    """
    In-memory double implementing `RecordReader` and `CheckpointWriter`.
    Used by every test in this package; never points at a real Neotoma
    instance of any kind. `instance_label` defaults to `"test-double"`, the
    only label an in-memory dict-backed double could honestly claim — a
    test that wants to exercise `NonProductionCheckpointWriter`'s refusal
    constructs a second instance with `instance_label="production"` (see
    `test_engine.py`'s production-boundary tests) rather than this class
    silently accepting any label.

    Failure injection: set `unreadable_workflows` to a set of
    `(declaration_scope, workflow_type)` pairs whose declaration read should
    return `UNKNOWN` (simulating `RP` failing the workflow read per GW-42's
    own fixture, `T-routed(feature)` with `RP` failing reads). Set
    `unreadable_read_types` to a set of entity_type strings whose
    `read_rows` call should return `UNKNOWN` (simulating an unreadable
    `reads_to_enter` dependency).
    """

    instance_label: str = "test-double"
    declarations: dict[tuple[str, str], WorkflowDeclaration] = field(default_factory=dict)
    rows: dict[str, list[dict]] = field(default_factory=dict)
    unreadable_workflows: set[tuple[str, str]] = field(default_factory=set)
    unreadable_read_types: set[str] = field(default_factory=set)
    checkpoints: list[CheckpointRaised] = field(default_factory=list)
    _checkpoints_by_key: dict[str, CheckpointRaised] = field(default_factory=dict)
    write_call_count: int = 0

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
        self,
        task_id: str,
        reason: str,
        *,
        idempotency_key: str,
        needed_input: str | None = None,
    ) -> CheckpointRaised:
        """
        Idempotent on `idempotency_key`: a second call with a key already
        seen returns the existing checkpoint unchanged and performs no new
        write (`write_call_count` does not advance) — this is what makes
        the "exactly one checkpoint" acceptance criterion hold across
        repeated polls, not only within a single `open_steps()` call.
        """
        existing = self._checkpoints_by_key.get(idempotency_key)
        if existing is not None:
            return existing
        cp = CheckpointRaised(
            task_id=task_id,
            reason=reason,
            idempotency_key=idempotency_key,
            needed_input=needed_input,
        )
        self.checkpoints.append(cp)
        self._checkpoints_by_key[idempotency_key] = cp
        self.write_call_count += 1
        return cp

    def open_checkpoints_for_task(self, task_id: str) -> list[CheckpointRaised]:
        return [c for c in self.checkpoints if c.task_id == task_id]
