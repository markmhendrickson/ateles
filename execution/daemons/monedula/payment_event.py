"""The one store path for ``payment_event`` records written on Monedula's behalf.

``payment_event`` identity is declared in ``execution/schemas/payment_event.json``:
``approval_id + payment_date``, falling back to ``primary_session_key +
payment_date``. Without it Neotoma guessed a canonical name from
``btc_address``, so two payments to one address resolved to one entity and the
second overwrote the first.

Every store built here therefore:

* carries ``approval_id`` (the ``payment_approval`` entity id) and
  ``session_keys`` (``payment_journal.session_key`` values) when known, plus
  ``primary_session_key`` = ``session_keys[0]`` for the fallback rule;
* is refused locally when it carries neither, since no identity rule could
  match it (the schema's ``canonical_name_strict`` refuses it server-side too);
* is sent with ``strict: true``, so a write that would merge into an existing
  entity without a deterministic rule is refused instead of merged.
"""

from __future__ import annotations

from typing import Any

try:
    from . import payment_approval
except ImportError:
    import payment_approval  # type: ignore[no-redef]

ENTITY_TYPE = "payment_event"


class PaymentEventError(ValueError):
    """A payment_event store could not be built with a declared identity."""


def store_body(
    fields: dict[str, Any],
    *,
    payment_date: str,
    approval_id: str | None = None,
    session_keys: list[str] | None = None,
) -> dict[str, Any]:
    """The ``POST /store`` body for one payment_event."""
    if not payment_date:
        raise PaymentEventError("payment_event needs payment_date")
    keys = [str(k) for k in (session_keys or []) if str(k).strip()]
    approval = (approval_id or "").strip()
    if not approval and not keys:
        raise PaymentEventError(
            "payment_event needs approval_id or at least one session key; "
            "refusing to store with no declared identity"
        )
    entity: dict[str, Any] = {
        **fields,
        "entity_type": ENTITY_TYPE,
        "payment_date": payment_date,
    }
    if approval:
        entity["approval_id"] = approval
    if keys:
        entity["session_keys"] = keys
        entity["primary_session_key"] = keys[0]
    identity = approval or keys[0]
    return {
        "entities": [entity],
        "strict": True,
        "idempotency_key": f"monedula-payment-event-{identity}-{payment_date}",
    }


def store(fields: dict[str, Any], **identity: Any) -> Any:
    """Store one payment_event. Raises on a missing identity or a failed request."""
    return payment_approval._request("POST", "/store", store_body(fields, **identity))
