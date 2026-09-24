"""Reference ("sixth") adapter effect test — unmapped / mapped / halt + hooks."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from lib.adapters.reference import (
    dedup_key,
    handle_inbound,
    outbound_action_class,
    resolve_identity,
)
from lib.adapters.runtime import DropCounter, current_window
from lib.adapters.types import Disposition, DropReason, HaltError


def test_unmapped_mapped_halt() -> None:
    """Reference adapter: (a) unmapped drop+counter (b) mapped provenance write (c) halt."""
    counter = DropCounter()
    window = current_window()
    tenant = "ref-tenant"
    adapter_id = "reference"

    # (a) planted unmapped → dropped/unmapped + counter++
    before = counter.read(window, tenant_or_user=tenant, adapter_id=adapter_id)
    dropped = handle_inbound(
        {"type": "totally_unknown"},
        counter=counter,
        adapter_id=adapter_id,
        tenant_or_user=tenant,
    )
    assert dropped is Disposition.DROPPED
    after = counter.read(
        window,
        tenant_or_user=tenant,
        adapter_id=adapter_id,
        reason=DropReason.UNMAPPED,
    )
    assert after - before == 1

    # (b) mapped write carries source / sourced_time / coverage
    store_fn = MagicMock(return_value={"ok": True})
    mapped = handle_inbound(
        {
            "type": "known_observation",
            "source": "reference-system",
            "sourced_time": "2026-09-23T12:00:00Z",
            "coverage_start": "page-0",
            "coverage_end": "page-1",
            "coverage_got": "page-1",
            "delivery_id": "ref-del-mapped-1",
        },
        counter=counter,
        adapter_id=adapter_id,
        tenant_or_user=tenant,
        store_fn=store_fn,
    )
    assert mapped is Disposition.OBSERVATION
    store_fn.assert_called_once()
    body = store_fn.call_args[0][0]
    entity = body["entities"][0]
    assert entity["source"] == "reference-system"
    assert entity["sourced_time"] == "2026-09-23T12:00:00Z"
    assert entity["coverage"]["start"] == "page-0"
    assert body["idempotency_key"] == "ref-del-mapped-1"

    # (c) halt → no write, no ack
    store_halt = MagicMock()
    with pytest.raises(HaltError):
        handle_inbound(
            {
                "type": "known_observation",
                "source": "reference-system",
                "sourced_time": "2026-09-23T12:00:00Z",
                "delivery_id": "ref-del-halt",
            },
            counter=counter,
            adapter_id=adapter_id,
            tenant_or_user=tenant,
            store_fn=store_halt,
            halt_check=lambda: True,
        )
    store_halt.assert_not_called()

    # Obligation 2 / 3 / 6 hooks present and callable
    assert resolve_identity(None) is None
    assert resolve_identity({"principal": "synth-user"})["principal"] == "synth-user"
    assert dedup_key("d1") == "reference:d1"
    assert callable(outbound_action_class)
    assert outbound_action_class("notify") == "notify_operator"
    assert outbound_action_class("unknown_op") == "NEVER"
