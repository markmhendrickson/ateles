"""Adapter runtime types — admissions dispositions, provenance, and boundary errors.

Disposition vocabulary matches docs/foundation/adapters.md (four outcomes + dropped).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Disposition(str, Enum):
    """Inbound delivery disposition (adapters.md four outcomes + dropped)."""

    VERDICT = "verdict"
    OBSERVATION = "observation"
    ACTION_CONFIRMATION = "action_confirmation"
    INTAKE_TASK = "intake_task"
    DROPPED = "dropped"


class DropReason(str, Enum):
    """Why a delivery was dropped. Extensible: new reasons may be added as str values."""

    UNMAPPED = "unmapped"


@dataclass(frozen=True)
class Coverage:
    """Window/page bounds the read asked for and got (obligation 4 provenance)."""

    start: str
    end: str
    got: str | None = None


@dataclass(frozen=True)
class Observation:
    """Observation payload the runtime will persist.

    ``delivery_id`` becomes the store ``idempotency_key`` (obligation 3 inbound).
    ``source``, ``sourced_time``, and ``coverage`` are required provenance (obligation 4).
    """

    source: str
    sourced_time: str
    coverage: Coverage
    delivery_id: str
    payload: dict[str, Any] | None = None


class AdapterBoundaryError(Exception):
    """Structured boundary failure with a stable ``code`` and human ``hint``."""

    def __init__(self, code: str, hint: str, *, message: str | None = None) -> None:
        self.code = code
        self.hint = hint
        super().__init__(message or f"{code}: {hint}")


class HaltError(AdapterBoundaryError):
    """Unreachable record / halt — write nothing and acknowledge nothing (obligation 5)."""

    def __init__(
        self,
        hint: str = (
            "Record unreachable or halt active: call neither write nor ack "
            "(adapters.md admission obligation 5)."
        ),
        *,
        code: str = "halt",
    ) -> None:
        super().__init__(code, hint)


class ProvenanceError(AdapterBoundaryError):
    """Missing required provenance field(s) on an observation write."""

    def __init__(self, fields: list[str]) -> None:
        named = ", ".join(fields)
        super().__init__(
            "missing_provenance",
            (
                f"Observation requires source, sourced_time, and coverage; "
                f"missing: {named}. Pass a complete Observation to "
                f"lib.adapters.write_observation."
            ),
            message=f"missing provenance field(s): {named}",
        )
        self.fields = fields


class Obligation4Error(AdapterBoundaryError):
    """Banned local-state mechanism rejected at the write boundary (obligation 4)."""

    def __init__(self, banned: str) -> None:
        super().__init__(
            "obligation_4_local_state",
            (
                f"Rejected '{banned}': adapters must not invent a sync log, "
                f"last-seen cursor table, or local artifact cache beside the "
                f"record (adapters.md admission obligation 4). Persist only "
                f"via write_observation → auth-scoped POST /store."
            ),
            message=f"banned local-state kwarg: {banned}",
        )


class ReadbackError(AdapterBoundaryError):
    """Decision-carrying write was not confirmed on read-back (obligation 5)."""

    def __init__(
        self,
        hint: str = (
            "Read-back did not confirm the write; do not acknowledge the "
            "external delivery (adapters.md admission obligation 5)."
        ),
    ) -> None:
        super().__init__("readback_unconfirmed", hint)


class DeliveryIdError(AdapterBoundaryError):
    """Mutating write refused because delivery_id / idempotency_key is missing."""

    def __init__(self) -> None:
        super().__init__(
            "missing_delivery_id",
            (
                "Observation.delivery_id is required and becomes store "
                "idempotency_key; refuse the write rather than invent a key."
            ),
        )


class UserIdWidenError(AdapterBoundaryError):
    """Caller attempted to widen store scope beyond the authenticated principal."""

    def __init__(self) -> None:
        super().__init__(
            "user_id_widen_forbidden",
            (
                "write_observation never accepts caller user_id (or equivalent) "
                "that widens write/read scope beyond process auth."
            ),
        )
