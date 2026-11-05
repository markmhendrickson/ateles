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
    ids=lambda value: type(value).__name__ + "-" + str(len(value)),
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
    ids=lambda value: type(value).__name__ + "-" + str(len(value)),
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


def test_frame_payload_exact_bound_does_not_count_delimiter_as_payload():
    payload = b'"' + b"a" * (e.MAX_INPUT_BYTES - 2) + b'"'
    assert list(e.read_frames(io.BytesIO(payload + b"\n"))) == [(payload, None)]


def test_incomplete_prior_receipt_counts_once_without_replaying_it(tmp_path):
    store = store_at(tmp_path / "ws")
    store.control_path.write_text(
        json.dumps(
            {
                "events": [
                    {
                        "id": "second",
                        "tool": "retrieve_entity_snapshot",
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
    count = []

    def stopped(*_):
        count.append(1)
        raise SystemExit(17)

    with pytest.raises(SystemExit):
        server.observe_tool_call(
            store,
            "retrieve_entity_snapshot",
            {"entity_id": "ent_fixture_a"},
            handler=stopped,
        )
    result = server.observe_tool_call(
        store, "retrieve_entity_snapshot", {"entity_id": "ent_fixture_a"}
    )
    assert result["snapshot"]["status"] == "changed" and count == [1]
    trace = e.read_trace(store.log_path)
    assert len(trace["attempts"]) == 2 and not trace["complete"]
    assert "result" not in trace["attempts"][0]


@pytest.mark.parametrize("where", ["handler_outcome", "delivery"])
@pytest.mark.parametrize("value", [None, [], {}, 1, True])
def test_reader_metadata_enum_shape_class_never_raises_or_claims_complete(
    tmp_path, where, value
):
    records = observed_records(tmp_path)
    records[1 if where == "handler_outcome" else 2][where] = value
    result = e.normalize_records(records)
    assert not result["complete"] and result["issues"]
    assert isinstance(result["attempts"][0]["handler_outcome"], str)
    assert isinstance(result["attempts"][0]["response_delivery"], str)


def test_handler_result_serialization_keeps_actual_state_evidence(tmp_path):
    store = store_at(tmp_path / "ws")

    def unsupported(store, name, args):
        state = store.load()
        state["entities"]["ent_fixture_a"]["snapshot"]["status"] = "done"
        store.save(state)
        return {"unsupported": set()}

    response = server.observe_tool_call(
        store,
        "correct",
        {"entity_id": "ent_fixture_a", "field": "status", "value": "done"},
        handler=unsupported,
    )
    assert response.get("error")
    trace = e.read_trace(store.log_path)
    attempt = trace["attempts"][0]
    assert (
        not trace["complete"]
        and attempt["persisted_entities"]["ent_fixture_a"]["snapshot"]["status"]
        == "done"
    )
    assert "result" not in attempt and "outcome_error" in attempt


def test_truncated_last_record_retains_known_facts_without_qualifying(tmp_path):
    records = observed_records(tmp_path)
    p = tmp_path / "trace"
    p.write_bytes(b"\n".join(e.encode_json(row) for row in records))
    result = e.read_trace(p)
    assert (
        not result["complete"]
        and result["attempts"][0]["result"]["snapshot"]["status"] == "open"
    )
    assert "truncated_frame" in result["attempts"][0]["issues"]


def test_known_failed_receipt_is_counted_once_for_scripted_boundary(
    tmp_path, monkeypatch
):
    store = store_at(tmp_path / "ws")
    store.control_path.write_text(
        json.dumps(
            {
                "events": [
                    {
                        "id": "second",
                        "tool": "retrieve_entity_snapshot",
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
    original = store.append_record
    failed = False

    def fail_once(row):
        nonlocal failed
        if row.get("event") == "received" and not failed:
            failed = True
            raise OSError("synthetic initial observation failure")
        return original(row)

    monkeypatch.setattr(store, "append_record", fail_once)
    assert server.observe_tool_call(
        store, "retrieve_entity_snapshot", {"entity_id": "ent_fixture_a"}
    ).get("error")
    assert (
        server.observe_tool_call(
            store, "retrieve_entity_snapshot", {"entity_id": "ent_fixture_a"}
        )["snapshot"]["status"]
        == "changed"
    )
    trace = e.read_trace(store.log_path)
    assert len(trace["attempts"]) == 2 and not trace["complete"]
    assert (
        trace["attempts"][0]["handler_outcome"] == "not_executed"
        and not trace["attempts"][0]["receipt_observed"]
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("state_sha256", None),
        ("state_sha256", []),
        ("state_sha256", "bad"),
        ("planning_sha256", {}),
        ("planning_sha256", True),
        ("persisted_entities", []),
        ("persisted_entities", None),
        ("state_error", []),
        ("outcome_error", {}),
    ],
)
def test_reader_malformed_completion_metadata_is_not_complete(tmp_path, field, value):
    records = observed_records(tmp_path)
    records[1][field] = value
    result = e.normalize_records(records)
    assert not result["complete"] and result["issues"]
    assert result["attempts"][0]["result"]["snapshot"]["status"] == "open"


def test_reader_non_call_receipt_is_not_complete(tmp_path):
    import hashlib

    records = observed_records(tmp_path)
    records[0]["request"]["method"] = "ping"
    digest = hashlib.sha256(e.encode_json(records[0]["request"])).hexdigest()
    for row in records:
        row["request_sha256"] = digest
    result = e.normalize_records(records)
    assert (
        not result["complete"] and "invalid_receipt" in result["attempts"][0]["issues"]
    )


@pytest.mark.parametrize("rpc_id", [None, True, [], {}, "request-id", 1, 1.5])
def test_common_entrance_validates_id_before_handler_and_retains_request(
    tmp_path, rpc_id
):
    store = store_at(tmp_path / "ws")
    calls = []

    def handler(*_):
        calls.append(True)
        return {"ok": True}

    reply = server.observe_tool_call(
        store,
        "retrieve_entity_snapshot",
        {"entity_id": "ent_fixture_a"},
        rpc_id=rpc_id,
        handler=handler,
    )
    valid = type(rpc_id) in (str, int, float)
    assert bool(calls) == valid
    trace = e.read_trace(store.log_path)
    assert trace["complete"] and len(trace["attempts"]) == 1
    assert trace["attempts"][0]["rpc_id"] == rpc_id
    assert bool(reply.get("error")) != valid


@pytest.mark.parametrize("raw", [b"\xff\n", b'{"trace_version":2}\n'])
def test_scripted_events_hold_on_unidentifiable_prior_evidence(tmp_path, raw):
    store = store_at(tmp_path / "ws")
    store.control_path.write_text(
        json.dumps(
            {
                "events": [
                    {
                        "id": "boundary",
                        "tool": "retrieve_entity_snapshot",
                        "on_match": 1,
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
    store.log_path.write_bytes(raw)
    result = server.observe_tool_call(
        store, "retrieve_entity_snapshot", {"entity_id": "ent_fixture_a"}
    )
    assert result.get("error")
    assert store.load()["entities"]["ent_fixture_a"]["snapshot"]["status"] == "open"
    assert not e.read_trace(store.log_path)["complete"]


@pytest.mark.parametrize("value", [None, True, 1, "malformed", []])
def test_actual_malformed_handler_result_remains_visible_and_unqualified(
    tmp_path, value
):
    store = store_at(tmp_path / "ws")
    server.observe_tool_call(
        store,
        "retrieve_entity_snapshot",
        {"entity_id": "ent_fixture_a"},
        handler=lambda *_: value,
    )
    trace = e.read_trace(store.log_path)
    assert not trace["complete"] and len(trace["attempts"]) == 1
    assert "invalid_result_shape" in trace["attempts"][0]["issues"]
    assert trace["attempts"][0]["result"] == value


def test_unsupported_surrogate_string_is_explicit_before_handler(tmp_path):
    with pytest.raises(e.EvidenceError, match="Unicode"):
        e.encode_json({"value": "\ud800"})
    store = store_at(tmp_path / "ws")
    called = []
    reply = server.observe_tool_call(
        store,
        "retrieve_entity_snapshot",
        {"entity_id": "\ud800"},
        handler=lambda *_: called.append(True),
    )
    assert "unsupported bounded input" in reply["error"] and not called
    assert not e.read_trace(store.log_path)["complete"]


@pytest.mark.parametrize("bound", ["bytes", "rows"])
def test_reader_and_direct_normalizer_stop_at_trace_budget(
    tmp_path, monkeypatch, bound
):
    records = observed_records(tmp_path)
    trace = tmp_path / "trace"
    trace.write_bytes(b"".join(e.encode_json(row) + b"\n" for row in records))
    monkeypatch.setattr(
        e, "MAX_TRACE_BYTES" if bound == "bytes" else "MAX_TRACE_RECORDS", 1
    )
    for result in [e.read_trace(trace), e.normalize_records(records)]:
        assert not result["complete"]
        assert any(item["code"] == "trace_limit" for item in result["issues"])


def test_direct_normalizer_rejects_oversized_record_without_false_completeness():
    result = e.normalize_records(
        [
            {
                "tool": "read",
                "args": {},
                "result": {"value": "x" * (e.MAX_INPUT_BYTES + 1)},
            }
        ]
    )
    assert not result["complete"] and result["issues"][0]["code"] == "invalid_record"
