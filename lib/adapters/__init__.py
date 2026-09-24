"""Shared adapter runtime (lib.adapters).

Implements admission obligations 1 (drop counter), 4 (coverage on every
observation; no sync log / cursor table / local artifact cache), and 5
(read-back before ack; write nothing and ack nothing during halt) by
construction. See docs/foundation/adapters.md#the-admission-contract.
"""

from .runtime import DropCounter, admit, commit, current_window, write_observation
from .types import (
    AdapterBoundaryError,
    Coverage,
    DeliveryIdError,
    Disposition,
    DropReason,
    HaltError,
    Obligation4Error,
    Observation,
    ProvenanceError,
    ReadbackError,
    UserIdWidenError,
)

__all__ = [
    "AdapterBoundaryError",
    "Coverage",
    "DeliveryIdError",
    "Disposition",
    "DropCounter",
    "DropReason",
    "HaltError",
    "Obligation4Error",
    "Observation",
    "ProvenanceError",
    "ReadbackError",
    "UserIdWidenError",
    "admit",
    "commit",
    "current_window",
    "write_observation",
]
