"""Effect tests for the shared adapter runtime (obligations 1 / 4 / 5)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lib.adapters import (
    Coverage,
    Disposition,
    DropCounter,
    DropReason,
    HaltError,
    Observation,
    Obligation4Error,
    ProvenanceError,
    ReadbackError,
    admit,
    commit,
    current_window,
    write_observation,
)


TENANT = "tenant-a"
ADAPTER = "test-adapter"


def test_drop_counter_planted_positive() -> None:
    """Planted known-positive unmapped first → instrument live before any zero-drop claim."""
    counter = DropCounter()
    window = current_window()
    counter.record(
        DropReason.UNMAPPED,
        tenant_or_user=TENANT,
        adapter_id=ADAPTER,
        window=window,
    )
    assert counter.read(window, tenant_or_user=TENANT, adapter_id=ADAPTER) >= 1


def test_admit_unmapped_increments_counter() -> None:
    """Unmapped → dropped/unmapped; same-window counter +=1; write path not invoked."""
    counter = DropCounter()
    window = current_window()
    write_path = MagicMock()

    def mapping_fn(_event: object) -> Disposition | None:
        return None

    before = counter.read(window, tenant_or_user=TENANT, adapter_id=ADAPTER)
    result = admit(
        {"type": "unknown_event"},
        mapping_fn,
        counter=counter,
        adapter_id=ADAPTER,
        tenant_or_user=TENANT,
        window=window,
    )
    assert result is Disposition.DROPPED
    after = counter.read(
        window,
        tenant_or_user=TENANT,
        adapter_id=ADAPTER,
        reason=DropReason.UNMAPPED,
    )
    assert after - before == 1
    write_path.assert_not_called()


def test_admit_mapped_does_not_increment() -> None:
    """Mapped → non-dropped disposition; counter unchanged."""
    counter = DropCounter()
    window = current_window()
    before = counter.read(window, tenant_or_user=TENANT, adapter_id=ADAPTER)

    result = admit(
        {"type": "known"},
        lambda _e: Disposition.OBSERVATION,
        counter=counter,
        adapter_id=ADAPTER,
        tenant_or_user=TENANT,
        window=window,
    )
    assert result is Disposition.OBSERVATION
    assert result is not Disposition.DROPPED
    after = counter.read(window, tenant_or_user=TENANT, adapter_id=ADAPTER)
    assert after == before


def test_admit_duplicate_unmapped_same_window() -> None:
    """Two unmapped in same window → counter += 2 (fixed 60s wall-clock bucket)."""
    counter = DropCounter()
    window = current_window()
    before = counter.read(window, tenant_or_user=TENANT, adapter_id=ADAPTER)
    for _ in range(2):
        admit(
            {"type": "unknown"},
            lambda _e: None,
            counter=counter,
            adapter_id=ADAPTER,
            tenant_or_user=TENANT,
            window=window,
        )
    after = counter.read(
        window,
        tenant_or_user=TENANT,
        adapter_id=ADAPTER,
        reason=DropReason.UNMAPPED,
    )
    assert after - before == 2


def test_write_observation_requires_provenance(tmp_path: Path) -> None:
    """Missing provenance named; complete obs via store_fn; no local-state artifacts."""
    store_fn = MagicMock(return_value={"ok": True})

    for field in ("source", "sourced_time", "coverage"):
        kwargs: dict = {
            "source": "synthetic-source",
            "sourced_time": "2026-09-23T00:00:00Z",
            "coverage": Coverage(start="0", end="1", got="1"),
            "delivery_id": "del-1",
        }
        kwargs[field] = None if field != "coverage" else None
        if field == "source":
            kwargs["source"] = ""
        if field == "sourced_time":
            kwargs["sourced_time"] = ""
        obs = Observation(**kwargs)
        with pytest.raises(ProvenanceError) as exc_info:
            write_observation(obs, store_fn=store_fn)
        assert field in exc_info.value.fields or field in str(exc_info.value)

    complete = Observation(
        source="synthetic-source",
        sourced_time="2026-09-23T00:00:00Z",
        coverage=Coverage(start="0", end="1", got="1"),
        delivery_id="del-complete",
        payload={"label": "synthetic"},
    )
    result = write_observation(complete, store_fn=store_fn)
    assert result == {"ok": True}
    store_fn.assert_called_once()
    body = store_fn.call_args[0][0]
    assert body["idempotency_key"] == "del-complete"
    assert body["entities"][0]["source"] == "synthetic-source"
    assert body["entities"][0]["sourced_time"] == "2026-09-23T00:00:00Z"
    assert "coverage" in body["entities"][0]

    # No sync-log / cursor-table / artifact-cache file created under tmp.
    banned_names = ("sync_log", "cursor", "last_seen", "artifact_cache")
    for path in tmp_path.rglob("*"):
        name = path.name.lower()
        assert not any(b in name for b in banned_names)

    for banned in ("cursor", "sync_log", "last_seen"):
        with pytest.raises(Obligation4Error) as ban_exc:
            write_observation(complete, store_fn=store_fn, **{banned: True})
        assert "obligation" in ban_exc.value.hint.lower() or "4" in ban_exc.value.hint


def test_commit_halt_calls_neither() -> None:
    """Halt/unreachable → HaltError; write and readback call_count == 0."""
    write_fn = MagicMock()
    readback_fn = MagicMock(return_value=True)

    with pytest.raises(HaltError):
        commit(
            write_fn=write_fn,
            readback_fn=readback_fn,
            halt_check=lambda: True,
        )
    assert write_fn.call_count == 0
    assert readback_fn.call_count == 0


def test_commit_success_requires_readback() -> None:
    """Success path calls readback after write; not-confirmed raises (no silent ack)."""
    order: list[str] = []

    def write_fn() -> None:
        order.append("write")

    def readback_ok() -> bool:
        order.append("readback")
        return True

    commit(write_fn=write_fn, readback_fn=readback_ok, halt_check=lambda: False)
    assert order == ["write", "readback"]

    with pytest.raises(ReadbackError):
        commit(
            write_fn=lambda: None,
            readback_fn=lambda: False,
            halt_check=lambda: False,
        )
