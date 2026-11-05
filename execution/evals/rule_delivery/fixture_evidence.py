"""Bounded JSON and normalized cooperative fixture evidence (stdlib only).

This pure module reads the existing trace; it creates no ledger or authority.
"""

from __future__ import annotations
import json
import math
from pathlib import Path

# Engineering bounds for this synthetic evaluator, not worker authority limits.
MAX_INPUT_BYTES = 1_048_576
MAX_INPUT_DEPTH = 64
MAX_INPUT_NODES = 1_000_000


def decode_input_json(text: str, path: str, errors: list[str]):
    """Contain bounded malformed JSON before decoding can exhaust recursion."""
    import json

    if not isinstance(text, str):
        errors.append(path + ": expected JSON text")
        return None
    try:
        if len(text) > MAX_INPUT_BYTES or len(text.encode("utf-8")) > MAX_INPUT_BYTES:
            errors.append(f"{path}: JSON input exceeds {MAX_INPUT_BYTES} bytes")
            return None
    except UnicodeError:
        errors.append(path + ": invalid Unicode in JSON input")
        return None
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_INPUT_DEPTH:
                errors.append(f"{path}: JSON nesting exceeds {MAX_INPUT_DEPTH} levels")
                return None
        elif char in "]}":
            depth -= 1
    try:

        def invalid_constant(_value):
            raise ValueError("nonfinite JSON constant")

        return json.loads(text, parse_constant=invalid_constant)
    except (ValueError, TypeError, RecursionError, MemoryError, OverflowError):
        errors.append(path + ": invalid JSON or unsupported decoder resource shape")
        return None


def _structure_errors(value: object, path: str) -> list[str]:
    # Iterative traversal protects direct scorer callers as well as decoded data.
    pending, visited = [(value, path, 0)], 0
    while pending:
        item, here, depth = pending.pop()
        visited += 1
        if depth > MAX_INPUT_DEPTH or visited > MAX_INPUT_NODES:
            return [here + ": unsupported input depth or collection size"]
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    return [here + ": object keys must be strings"]
                pending.append((child, here + "." + key, depth + 1))
        elif isinstance(item, list):
            pending.extend(
                (child, f"{here}[{i}]", depth + 1) for i, child in enumerate(item)
            )
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            return [here + ": expected JSON-compatible value"]
    return []


class EvidenceError(ValueError):
    """Unsupported bounded data, not a successful empty observation."""


def encode_json(value, path="record", *, limit=MAX_INPUT_BYTES):
    errors = _structure_errors(value, path)
    if errors:
        raise EvidenceError(errors[0])
    # Bound strings before the encoder can allocate their escaped form.
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if len(item) > limit or len(item.encode("utf-8")) > limit:
                raise EvidenceError(path + ": string exceeds serialization bound")
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise EvidenceError(path + ": nonfinite number")
    output, size = [], 0
    try:
        for chunk in json.JSONEncoder(
            sort_keys=True, allow_nan=False, separators=(",", ":")
        ).iterencode(value):
            raw = chunk.encode("utf-8")
            size += len(raw)
            if size > limit:
                raise EvidenceError(path + ": serialized data exceeds bound")
            output.append(raw)
    except (
        TypeError,
        ValueError,
        UnicodeError,
        RecursionError,
        MemoryError,
        OverflowError,
    ) as exc:
        raise EvidenceError(
            path + ": unsupported serialization: " + type(exc).__name__
        ) from None
    return b"".join(output)


def read_frames(stream, *, limit=MAX_INPUT_BYTES):
    """Read/drain one bounded LF frame at a time; never dispatch fragments."""
    while True:
        raw = stream.readline(limit + 1)
        if not raw:
            return
        oversized = len(raw) > limit
        if not raw.endswith(b"\n"):
            if len(raw) <= limit:
                yield None, "truncated_frame"
                return
            # Discard an unsupported frame in bounded chunks, then recover at LF.
            while raw and not raw.endswith(b"\n"):
                raw = stream.readline(limit + 1)
            yield None, "oversized_frame"
            if not raw:
                return
        elif oversized:
            yield None, "oversized_frame"
        else:
            yield raw[:-1], None


def read_json_file(path, label="state"):
    with Path(path).open("rb") as handle:
        raw = handle.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise EvidenceError(label + ": file exceeds bound")
    errors = []
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise EvidenceError(label + ": invalid UTF-8") from None
    value = decode_input_json(text, label, errors)
    if errors:
        raise EvidenceError(errors[0])
    return value, raw


def normalize_records(records, *, exclude_correlation=None, initial_issues=None):
    """One logical attempt per receipt, with explicit gaps and legacy provenance.

    `complete` means the observed-v1 producer contract, not client acknowledgment,
    power-loss durability, tamper resistance or scenario acceptance.
    """
    issues = list(initial_issues or [])
    attempts, correlated = [], {}

    def issue(code, correlation=None, **detail):
        issues.append(
            {
                "code": code,
                **({"correlation_id": correlation} if correlation else {}),
                **detail,
            }
        )

    for index, row in enumerate(records):
        if _structure_errors(row, f"records[{index}]") or not isinstance(row, dict):
            issue("invalid_record", index=index)
            continue
        cid = row.get("correlation_id")
        if exclude_correlation is not None and cid == exclude_correlation:
            continue
        if "trace_version" in row and (
            type(row["trace_version"]) is not int or row["trace_version"] != 1
        ):
            issue("unsupported_trace_version", index=index)
            continue
        if "trace_version" not in row:
            # Completed-only historical rows are readable, never exhaustive proof.
            if (
                isinstance(row.get("tool"), str)
                and isinstance(row.get("args"), dict)
                and isinstance(row.get("result"), dict)
            ):
                attempts.append(
                    {
                        **row,
                        "correlation_id": None,
                        "provenance": "legacy_completed_only",
                        "receipt_observed": False,
                        "complete": False,
                        "handler_outcome": "legacy_returned",
                        "response_delivery": "unknown",
                    }
                )
                issue("legacy_completed_only", index=index)
            else:
                issue("invalid_record", index=index)
            continue
        if not isinstance(cid, str) or not cid:
            issue("invalid_correlation", index=index)
            continue
        event = row.get("event")
        if event == "protocol":
            issue("protocol_gap", cid, diagnostic=row.get("diagnostic"))
            continue
        if not isinstance(event, str) or event not in {
            "received",
            "completed",
            "response",
        }:
            issue("invalid_event", cid)
            continue
        entry = correlated.setdefault(
            cid, {"received": [], "completed": [], "response": [], "order": []}
        )
        entry[event].append(row)
        entry["order"].append(event)
    for cid, entry in correlated.items():
        receipts, terminals, deliveries = (
            entry[k] for k in ("received", "completed", "response")
        )
        if len(receipts) != 1:
            issue("missing_receipt" if not receipts else "duplicate_receipt", cid)
            # An orphan terminal never fabricates a received request.
            if not receipts:
                continue
        receipt = receipts[0]
        request = receipt.get("request")
        if not isinstance(request, dict):
            issue("invalid_receipt", cid)
            continue
        params = request.get("params")
        tool = params.get("name") if isinstance(params, dict) else None
        args = params.get("arguments", {}) if isinstance(params, dict) else None
        attempt = {
            "correlation_id": cid,
            "rpc_id": request.get("id"),
            "tool": tool if isinstance(tool, str) else None,
            "received_tool": tool,
            "args": args,
            "request": request,
            "receipt_observed": True,
            "provenance": "observed_v1",
            "complete": False,
            "handler_outcome": "unknown",
            "response_delivery": "unknown",
            "request_sha256": receipt.get("request_sha256"),
        }
        local = []

        def gap(code):
            local.append(code)
            issue(code, cid)

        try:
            import hashlib

            expected = hashlib.sha256(encode_json(request, "request")).hexdigest()
        except EvidenceError:
            expected = None
        if expected is None or receipt.get("request_sha256") != expected:
            gap("request_digest_mismatch")
        if entry["order"][:1] != ["received"]:
            gap("record_order")
        if len(terminals) != 1:
            gap("missing_completion" if not terminals else "duplicate_completion")
        if terminals:
            terminal = terminals[0]
            if terminal.get("request_sha256") != receipt.get("request_sha256"):
                gap("completion_mismatch")
            outcome = terminal.get("handler_outcome")
            if not isinstance(outcome, str) or outcome not in {
                "returned",
                "refused",
                "validation_error",
                "exception",
                "not_executed",
            }:
                gap("invalid_handler_outcome")
            attempt["handler_outcome"] = outcome
            if terminal.get("result_status") == "available" and isinstance(
                terminal.get("result"), dict
            ):
                attempt["result"] = terminal["result"]
            else:
                gap("result_unavailable")
            if outcome == "exception":
                gap("handler_exception")
            if terminal.get("state_capture") != "complete":
                gap("state_capture_incomplete")
            for key in (
                "state_sha256",
                "planning_sha256",
                "persisted_entities",
                "state_error",
                "outcome_error",
            ):
                if key in terminal:
                    attempt[key] = terminal[key]
        if len(deliveries) != 1:
            gap("missing_delivery" if not deliveries else "duplicate_delivery")
        if deliveries:
            delivery = deliveries[0]
            if delivery.get("request_sha256") != receipt.get("request_sha256"):
                gap("delivery_mismatch")
            attempt["response_delivery"] = delivery.get("delivery")
            if not isinstance(delivery.get("delivery"), str) or delivery[
                "delivery"
            ] not in {"written", "inprocess_return"}:
                gap("response_failed")
            if entry["order"] != ["received", "completed", "response"]:
                gap("record_order")
        if len(receipts) != 1:
            local.append("duplicate_receipt")
        attempt["complete"] = not local
        attempts.append(attempt)
    return {
        "attempts": attempts,
        "complete": bool(attempts)
        and not issues
        and all(x["complete"] for x in attempts),
        "issues": issues,
        "provenance": "observed_v1"
        if attempts and all(x["provenance"] == "observed_v1" for x in attempts)
        else "legacy_or_mixed"
        if attempts
        else "unavailable",
    }


def read_trace(path, *, exclude_correlation=None):
    records, issues, total = [], [], 0
    try:
        with Path(path).open("rb") as handle:
            for index, (raw, error) in enumerate(read_frames(handle)):
                if index >= 100_000 or total > 64 * MAX_INPUT_BYTES:
                    issues.append({"code": "trace_limit"})
                    break
                if error:
                    issues.append({"code": error, "index": index})
                    continue
                total += len(raw)
                if not raw.strip():
                    continue
                errors = []
                try:
                    text = raw.decode("utf-8")
                except UnicodeError:
                    issues.append({"code": "invalid_utf8", "index": index})
                    continue
                value = decode_input_json(text, f"trace[{index}]", errors)
                if errors:
                    issues.append(
                        {"code": "invalid_json", "index": index, "diagnostics": errors}
                    )
                else:
                    records.append(value)
    except OSError as exc:
        issues.append({"code": "trace_unavailable", "error": type(exc).__name__})
    if not records and not issues:
        issues.append({"code": "empty_trace"})
    return normalize_records(
        records, exclude_correlation=exclude_correlation, initial_issues=issues
    )
