"""Tests for the loopback normalizer in front of Ollama (ollama_shim.py)."""

from __future__ import annotations

import http.server
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import ollama_shim  # noqa: E402

BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "Bash",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}},
        },
    },
}


def _resp(content, tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"id": "x", "created": 1, "model": "m",
            "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}


# ── normalize ──────────────────────────────────────────────────────────────


def test_all_system_messages_fold_into_one_leading_message():
    body = {"messages": [
        {"role": "system", "content": "first"},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": [{"type": "text", "text": "hook context"}]},
        {"role": "assistant", "content": "ok"},
    ]}
    out = ollama_shim.normalize(body)["messages"]
    assert [m["role"] for m in out] == ["system", "user", "assistant"]
    assert out[0]["content"] == "first\n\nhook context"


def test_temperature_defaults_to_zero_but_caller_value_wins():
    assert ollama_shim.normalize({"messages": []})["temperature"] == 0
    assert ollama_shim.normalize({"messages": [], "temperature": 0.7})["temperature"] == 0.7


# ── repair ─────────────────────────────────────────────────────────────────


def test_text_tool_call_for_declared_tool_becomes_tool_calls():
    resp = _resp("Running it.\n<function=Bash><parameter=command>\nls -la\n</parameter>"
                 "<parameter=timeout>30</parameter></function>")
    assert ollama_shim.repair(resp, [BASH_TOOL]) is True
    choice = resp["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    call = choice["message"]["tool_calls"][0]
    assert call["function"]["name"] == "Bash"
    assert json.loads(call["function"]["arguments"]) == {"command": "ls -la", "timeout": 30}
    assert choice["message"]["content"] == "Running it."


def test_text_tool_call_for_undeclared_tool_is_left_alone():
    text = "<function=Write><parameter=path>/etc/x</parameter></function>"
    resp = _resp(text)
    assert ollama_shim.repair(resp, [BASH_TOOL]) is False
    assert "tool_calls" not in resp["choices"][0]["message"]
    assert resp["choices"][0]["message"]["content"] == text


def test_existing_tool_calls_and_undeclared_requests_are_not_touched():
    real = [{"id": "c", "type": "function", "function": {"name": "Bash", "arguments": "{}"}}]
    assert ollama_shim.repair(_resp("<function=Bash></function>", real), [BASH_TOOL]) is False
    assert ollama_shim.repair(_resp("<function=Bash></function>"), []) is False


# ── SSE re-emission ────────────────────────────────────────────────────────


def test_sse_reemits_content_tool_calls_finish_usage_and_done():
    resp = _resp("hi")
    resp["choices"][0]["message"]["tool_calls"] = [
        {"id": "c1", "type": "function", "function": {"name": "Bash", "arguments": "{}"}}]
    resp["choices"][0]["finish_reason"] = "tool_calls"
    frames = ollama_shim.to_sse(resp).decode().split("\n\n")
    payloads = [f[len("data: "):] for f in frames if f.startswith("data: ")]
    assert payloads[-1] == "[DONE]"
    chunks = [json.loads(p) for p in payloads[:-1]]
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": "hi"}
    assert chunks[1]["choices"][0]["delta"]["tool_calls"][0]["index"] == 0
    assert chunks[2]["choices"][0]["finish_reason"] == "tool_calls"
    assert chunks[3]["usage"]["total_tokens"] == 12


# ── context ceiling ────────────────────────────────────────────────────────


def test_prompt_within_ceiling_is_accepted():
    body = {"messages": [{"role": "user", "content": "x" * 300}]}
    assert ollama_shim.ceiling_error(body, 2048, chars_per_token=3.0, output_reserve=1024) is None


def test_prompt_over_ceiling_is_refused_with_context_length_code():
    body = {"messages": [{"role": "user", "content": "x" * 30_000}], "tools": [BASH_TOOL]}
    err = ollama_shim.ceiling_error(body, 8192, chars_per_token=3.0, output_reserve=1024)
    assert err is not None
    assert err["error"]["code"] == "context_length_exceeded"
    assert ollama_shim.CEILING_ERROR_MARKER in err["error"]["message"]


def test_output_reserve_counts_against_the_ceiling():
    body = {"messages": [{"role": "user", "content": "x" * 2_900}]}
    tokens = ollama_shim.estimate_prompt_tokens(body, 3.0)
    assert ollama_shim.ceiling_error(body, tokens, chars_per_token=3.0, output_reserve=0) is None
    assert ollama_shim.ceiling_error(body, tokens, chars_per_token=3.0, output_reserve=1) is not None


def test_tool_schemas_count_toward_the_estimate():
    base = {"messages": [{"role": "user", "content": "hi"}]}
    assert (ollama_shim.estimate_prompt_tokens(dict(base, tools=[BASH_TOOL] * 20))
            > ollama_shim.estimate_prompt_tokens(base))


def test_missing_ceiling_refuses_to_start():
    with pytest.raises(ValueError, match="SHIM_CONTEXT_CEILING"):
        ollama_shim.load_limits({})
    with pytest.raises(ValueError):
        ollama_shim.load_limits({"SHIM_CONTEXT_CEILING": "1000", "SHIM_OUTPUT_RESERVE": "1000"})
    assert ollama_shim.load_limits({"SHIM_CONTEXT_CEILING": "32768"}) == (32768, 3.0, 1024)


# ── end to end over HTTP, against a fake upstream ──────────────────────────


class _FakeUpstream(http.server.BaseHTTPRequestHandler):
    received: list[dict] = []
    reply: dict = {}

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).received.append(body)
        out = json.dumps(type(self).reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


@pytest.fixture
def shim(monkeypatch):
    _FakeUpstream.received = []
    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    class Handler(ollama_shim.Handler):
        pass

    Handler.upstream = f"http://127.0.0.1:{upstream.server_address[1]}"
    Handler.limits = (4096, 3.0, 1024)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    upstream.shutdown()


def _post(url, body):
    req = urllib.request.Request(url + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=10)


def test_over_ceiling_request_never_reaches_the_backend(shim):
    """The effect the ceiling exists for: Ollama never sees a prompt it would truncate."""
    with pytest.raises(urllib.error.HTTPError) as caught:
        _post(shim, {"model": "m", "messages": [{"role": "user", "content": "x" * 50_000}]})
    assert caught.value.code == 400
    assert ollama_shim.CEILING_ERROR_MARKER in caught.value.read().decode()
    assert _FakeUpstream.received == []


def test_streamed_request_is_normalized_repaired_and_reemitted_as_sse(shim):
    _FakeUpstream.reply = _resp("<function=Bash><parameter=command>pwd</parameter></function>")
    resp = _post(shim, {"model": "m", "stream": True, "stream_options": {"include_usage": True},
                        "tools": [BASH_TOOL],
                        "messages": [{"role": "user", "content": "where"},
                                     {"role": "system", "content": "late"}]})
    assert resp.headers["Content-Type"] == "text/event-stream"
    sent = _FakeUpstream.received[0]
    assert sent["stream"] is False and "stream_options" not in sent
    assert sent["messages"][0] == {"role": "system", "content": "late"}
    assert sent["temperature"] == 0
    sse = resp.read().decode()
    assert '"tool_calls"' in sse and '"finish_reason": "tool_calls"' in sse
