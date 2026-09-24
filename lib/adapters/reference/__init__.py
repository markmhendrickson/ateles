"""Reference ("sixth") adapter — exercises the shared runtime APIs.

Demonstrates admission obligations 1/4/5 via the runtime and thin callable
hooks for obligations 2/3/6. Full AD-21–AD-34 harness is ateles#1191.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from lib.adapters.runtime import DropCounter, admit, commit, write_observation
from lib.adapters.types import (
    Coverage,
    Disposition,
    DropReason,
    HaltError,
    Observation,
)

# Trivial in-memory mapping: event type label → disposition.
_DEFAULT_MAP: dict[str, Disposition] = {
    "known_observation": Disposition.OBSERVATION,
    "known_verdict": Disposition.VERDICT,
    "known_action_confirmation": Disposition.ACTION_CONFIRMATION,
    "known_intake": Disposition.INTAKE_TASK,
}


def resolve_identity(credential: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Obligation 2 hook: resolve credential → principal; unbound → None (observation path).

    Unrecognized / unbound credential must never fall through to the operator.
    """
    if not credential:
        return None
    principal = credential.get("principal")
    if principal is None:
        return None
    return {"principal": principal, "system": credential.get("system", "reference")}


def dedup_key(delivery_id: str, *, adapter_id: str = "reference") -> str:
    """Obligation 3 hook: stable inbound dedup key from the delivery id."""
    return f"{adapter_id}:{delivery_id}"


def outbound_action_class(operation: str) -> str:
    """Obligation 6 hook: name the action class for an outbound operation.

    Unlisted classes remain gate/NEVER territory — this runtime does not auto-execute.
    """
    classes = {
        "notify": "notify_operator",
        "open_issue": "open_issue",
    }
    return classes.get(operation, "NEVER")


def map_event(
    event: Mapping[str, Any],
    *,
    mapping: Mapping[str, Disposition] | None = None,
) -> Disposition | None:
    """Return a mapped disposition or None when the event type is unmapped."""
    table = mapping if mapping is not None else _DEFAULT_MAP
    event_type = event.get("type")
    if event_type is None:
        return None
    return table.get(str(event_type))


def handle_inbound(
    event: Mapping[str, Any],
    *,
    counter: DropCounter,
    adapter_id: str = "reference",
    tenant_or_user: str = "synthetic-tenant",
    store_fn: Callable[[dict], Any] | None = None,
    halt_check: Callable[[], bool] | None = None,
    mapping: Mapping[str, Disposition] | None = None,
) -> Disposition:
    """Admit, and on a mapped observation path write+commit through the runtime."""
    disposition = admit(
        event,
        lambda e: map_event(e, mapping=mapping),
        counter=counter,
        adapter_id=adapter_id,
        tenant_or_user=tenant_or_user,
    )
    if disposition is Disposition.DROPPED:
        return disposition

    if disposition is Disposition.OBSERVATION:
        obs = Observation(
            source=str(event.get("source", "reference")),
            sourced_time=str(event.get("sourced_time", "1970-01-01T00:00:00Z")),
            coverage=Coverage(
                start=str(event.get("coverage_start", "0")),
                end=str(event.get("coverage_end", "1")),
                got=str(event.get("coverage_got", "1")),
            ),
            delivery_id=str(event.get("delivery_id", "ref-delivery-missing")),
            payload={"type": event.get("type")},
        )

        def _write() -> Any:
            return write_observation(obs, store_fn=store_fn)

        written: dict[str, Any] = {"ok": False}

        def _write_and_mark() -> None:
            result = _write()
            written["ok"] = True
            written["result"] = result

        def _readback() -> bool:
            return bool(written.get("ok"))

        commit(
            write_fn=_write_and_mark,
            readback_fn=_readback,
            halt_check=halt_check,
        )
    elif halt_check is not None:
        # Non-observation mapped paths still respect halt for write/ack discipline
        # when a caller intends a decision-carrying write via commit.
        commit(
            write_fn=lambda: None,
            readback_fn=lambda: True,
            halt_check=halt_check,
        )

    return disposition


__all__ = [
    "DropCounter",
    "DropReason",
    "HaltError",
    "admit",
    "commit",
    "dedup_key",
    "handle_inbound",
    "map_event",
    "outbound_action_class",
    "resolve_identity",
    "write_observation",
]
