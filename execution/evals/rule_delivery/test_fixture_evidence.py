"""Pure bounded-reader tests and actual in-process observation controls."""

from __future__ import annotations
import copy
import io
import json
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixture_evidence as e
import fixture_mcp_server as server


def store_at(path):
    path.mkdir(parents=True, exist_ok=True)
    store = server.Store(path)
    store.save(
        {
            "entities": {
                "ent_fixture_a": {"entity_type": "task", "snapshot": {"status": "open"}}
            },
            "relationships": [],
        }
    )
    return store


def observed_records(tmp_path):
    store = store_at(tmp_path / "ws")
    assert (
        server.observe_tool_call(
            store, "retrieve_entity_snapshot", {"entity_id": "ent_fixture_a"}
        )["snapshot"]["status"]
        == "open"
    )
    return [json.loads(x) for x in store.log_path.read_text().splitlines()]


def test_observed_contract_has_one_complete_attempt(tmp_path):
    records = observed_records(tmp_path)
    assert [x["event"] for x in records] == ["received", "completed", "response"]
    result = e.normalize_records(records)
    assert result["complete"] and len(result["attempts"]) == 1
    assert result["attempts"][0]["result"]["snapshot"]["status"] == "open"
    assert result["attempts"][0]["response_delivery"] == "inprocess_return"


@pytest.mark.parametrize(
    "variant",
    [
        "receipt_only",
        "missing_receipt",
        "duplicate_receipt",
        "duplicate_terminal",
        "duplicate_delivery",
        "wrong_hash",
        "wrong_delivery_hash",
        "out_of_order",
        "invalid_event",
        "invalid_correlation",
    ],
)
def test_reader_unit_missing_duplicate_mismatch_records_never_qualify(
    tmp_path, variant
):
    records = observed_records(tmp_path)
    if variant == "receipt_only":
        records = records[:1]
    elif variant == "missing_receipt":
        records = records[1:]
    elif variant == "duplicate_receipt":
        records.insert(1, copy.deepcopy(records[0]))
    elif variant == "duplicate_terminal":
        records.insert(2, copy.deepcopy(records[1]))
    elif variant == "duplicate_delivery":
        records.append(copy.deepcopy(records[2]))
    elif variant == "wrong_hash":
        records[1]["request_sha256"] = "wrong"
    elif variant == "wrong_delivery_hash":
        records[2]["request_sha256"] = "wrong"
    elif variant == "out_of_order":
        records = [records[1], records[0], records[2]]
    elif variant == "invalid_event":
        records[1]["event"] = []
    elif variant == "invalid_correlation":
        records[1]["correlation_id"] = {}
    result = e.normalize_records(records)
    assert not result["complete"] and result["issues"]
    assert len(result["attempts"]) <= 1
    if variant == "receipt_only":
        assert "result" not in result["attempts"][0]


def test_legacy_rows_keep_weaker_provenance():
    result = e.normalize_records([{"tool": "read", "args": {}, "result": {"ok": True}}])
    assert not result["complete"] and result["attempts"][0]["result"] == {"ok": True}
    assert not result["attempts"][0]["receipt_observed"]
    assert result["attempts"][0]["provenance"] == "legacy_completed_only"


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        True,
        1,
        {"trace_version": 2},
        {"trace_version": 1, "event": {}, "correlation_id": "x"},
    ],
)
def test_bad_reader_record_shapes_are_explicit(value):
    result = e.normalize_records([value])
    assert not result["complete"] and result["issues"]


@pytest.mark.parametrize(
    "payload",
    [
        b"{\n",
        b"[" * 10000 + b"0" + b"]" * 10000 + b"\n",
        b"\xff\n",
        b"x" * (e.MAX_INPUT_BYTES + 1) + b"\n",
        b"{",
    ],
)
def test_bounded_trace_errors_are_not_empty_compliance(tmp_path, payload):
    p = tmp_path / "trace"
    p.write_bytes(payload)
    result = e.read_trace(p)
    assert not result["complete"] and result["issues"]


def test_missing_and_empty_trace_are_unqualified(tmp_path):
    p = tmp_path / "trace"
    assert e.read_trace(p)["issues"][0]["code"] == "trace_unavailable"
    p.write_bytes(b"")
    assert e.read_trace(p)["issues"][0]["code"] == "empty_trace"


def test_oversized_frame_drains_to_next_boundary():
    frames = list(e.read_frames(io.BytesIO(b"x" * (e.MAX_INPUT_BYTES + 4) + b"\n{}\n")))
    assert frames == [(None, "oversized_frame"), (b"{}", None)]


def test_json_quoted_brackets_and_escapes_are_valid():
    value = {"x": "[" * 10000 + '"\\' + "]" * 10000}
    errors = []
    assert e.decode_input_json(e.encode_json(value).decode(), "input", errors) == value
    assert errors == []


@pytest.mark.parametrize(
    "payload",
    ["[", "[" * 10000 + "0" + "]" * 10000, "x" * (e.MAX_INPUT_BYTES + 1), "NaN"],
)
def test_decoder_rejects_syntax_depth_size_and_nonfinite(payload):
    errors = []
    assert e.decode_input_json(payload, "input", errors) is None
    assert errors


@pytest.mark.parametrize(
    "value",
    [
        {"x": set()},
        {"x": float("nan")},
        {"x": "a" * (e.MAX_INPUT_BYTES + 1)},
        {1: "bad"},
    ],
)
def test_encoder_never_silently_serializes_unsupported_data(value):
    with pytest.raises(e.EvidenceError):
        e.encode_json(value)


def test_correlation_survives_reused_rpc_ids_and_new_store_instances(tmp_path):
    store = store_at(tmp_path / "ws")
    for instance in [store, server.Store(store.state_path.parent)]:
        server.observe_tool_call(
            instance,
            "retrieve_entity_snapshot",
            {"entity_id": "ent_fixture_a"},
            rpc_id=7,
        )
    result = e.read_trace(store.log_path)
    assert result["complete"] and len(result["attempts"]) == 2
    assert len({x["correlation_id"] for x in result["attempts"]}) == 2
    assert [x["rpc_id"] for x in result["attempts"]] == [7, 7]


def test_prehandler_receipt_failure_prevents_actual_effect(tmp_path, monkeypatch):
    store = store_at(tmp_path / "ws")
    called = []

    def broken(_record):
        raise OSError("synthetic receipt failure")

    monkeypatch.setattr(store, "append_record", broken)

    def handler(*_):
        called.append(True)
        return {"success": True}

    result = server.observe_tool_call(
        store,
        "correct",
        {"entity_id": "ent_fixture_a", "field": "status", "value": "done"},
        handler=handler,
    )
    assert result.get("error") and called == []
    assert store.load()["entities"]["ent_fixture_a"]["snapshot"]["status"] == "open"
    assert not e.read_trace(store.log_path)["complete"]


@pytest.mark.parametrize(
    "fault",
    [
        "completion",
        "state_capture",
        "response",
        "delivery_log",
        "handler_after_effect",
        "result_serialization",
    ],
)
def test_posteffect_uncertainty_keeps_receipt_and_never_replays(
    tmp_path, monkeypatch, fault
):
    store = store_at(tmp_path / "ws")
    calls = []
    append = store.append_record

    def failing_append(record):
        if fault == "completion" and record.get("event") == "completed":
            raise OSError("synthetic completion failure")
        if fault == "delivery_log" and record.get("event") == "response":
            raise OSError("synthetic delivery-log failure")
        return append(record)

    monkeypatch.setattr(store, "append_record", failing_append)
    if fault == "state_capture":

        def missing(*_):
            raise OSError("synthetic capture failure")

        monkeypatch.setattr(store, "capture_state", missing)

    def handler(store, _name, _args):
        calls.append(1)
        state = store.load()
        state["entities"]["ent_fixture_a"]["snapshot"]["status"] = "done"
        store.save(state)
        if fault == "handler_after_effect":
            raise RuntimeError("synthetic post-effect failure")
        return (
            {"value": set()} if fault == "result_serialization" else {"success": True}
        )

    def send(_raw):
        if fault == "response":
            raise BrokenPipeError("synthetic response failure")

    server.process_request(
        store,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "correct",
                "arguments": {
                    "entity_id": "ent_fixture_a",
                    "field": "status",
                    "value": "done",
                },
            },
        },
        responder=send,
        handler=handler,
    )
    assert (
        calls == [1]
        and store.load()["entities"]["ent_fixture_a"]["snapshot"]["status"] == "done"
    )
    normalized = e.read_trace(store.log_path)
    assert len(normalized["attempts"]) == 1 and not normalized["complete"]
    assert normalized["attempts"][0]["receipt_observed"]


def test_scripted_events_count_prior_logical_attempts_once(tmp_path):
    store = store_at(tmp_path / "ws")
    store.control_path.write_text(
        json.dumps(
            {
                "events": [
                    {
                        "id": "second",
                        "tool": "retrieve_entity_snapshot",
                        "match": {"entity_id": "ent_fixture_a"},
                        "on_match": 2,
                        "entities": {
                            "ent_fixture_a": {
                                "entity_type": "task",
                                "snapshot": {"status": "changed"},
                            }
                        },
                    }
                ]
            }
        )
    )
    first = server.observe_tool_call(
        store, "retrieve_entity_snapshot", {"entity_id": "ent_fixture_a"}
    )
    assert first["snapshot"]["status"] == "open"
    rows = [json.loads(x) for x in store.log_path.read_text().splitlines()]
    # A duplicate terminal is not another received request. It stays a gap for
    # qualification, while transition counting still uses the single receipt.
    store.append_record(next(row for row in rows if row["event"] == "completed"))
    second = server.observe_tool_call(
        store, "retrieve_entity_snapshot", {"entity_id": "ent_fixture_a"}
    )
    assert second["snapshot"]["status"] == "changed"
    third = server.observe_tool_call(
        store, "retrieve_entity_snapshot", {"entity_id": "ent_fixture_a"}
    )
    assert third["snapshot"]["status"] == "changed"
    assert json.loads(
        (store.state_path.parent.parent / "fixture_events_applied.json").read_text()
    ) == ["second"]
    assert len(e.read_trace(store.log_path)["attempts"]) == 3
