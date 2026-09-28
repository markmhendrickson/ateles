"""Loopback request normalizer between LiteLLM and Ollama's OpenAI-compatible API.

127.0.0.1:11435 -> 127.0.0.1:11434 by default. Makes Qwen3-Coder tool calls
reliable when driven by Claude Code through LiteLLM (the ``claude-local``
provider in ``execution/daemons/apis/local_provider.py``):

1. Folds every system-role message into one leading system message. Claude Code (via
   LiteLLM) sends system messages after the first user turn (hook context), and Qwen's
   renderer only handles a leading one.
2. Defaults temperature to 0 unless the caller set one (SHIM_TEMPERATURE overrides).
3. Repairs a tool call the model wrote as text. Qwen3-Coder's native call syntax is
   <function=NAME><parameter=K>V</parameter></function>; Ollama only parses it when the
   model also emits the <tool_call> opener, which it sometimes omits. The shim asks
   upstream for a non-streamed completion, converts such text into a real tool_calls
   entry (only for tools the request declared), and re-emits SSE if the caller streamed.
4. Refuses a request whose prompt would exceed the model's context window
   (SHIM_CONTEXT_CEILING, in tokens). Ollama silently drops the start of a prompt
   longer than its num_ctx and answers anyway, so an over-limit request would
   otherwise return a confident answer to a prompt the model never fully saw. The
   refusal is an HTTP 400 whose error code is ``context_length_exceeded`` and whose
   message carries CEILING_ERROR_MARKER, so the caller can fail over rather than
   trust the answer.

The token count is an estimate (serialized characters / SHIM_CHARS_PER_TOKEN),
deliberately biased high: across 94 captured Claude Code requests to
qwen3-coder the observed ratio was 3.34-4.68 characters per reported prompt
token, so the default of 3.0 over-counts and a request is refused early rather
than truncated late. SHIM_OUTPUT_RESERVE tokens are held back for the reply.

SHIM_CONTEXT_CEILING is required: without it the shim cannot know the window it
is protecting, and starting anyway would reopen the silent truncation. Set it to
the same value as Ollama's num_ctx (OLLAMA_CONTEXT_LENGTH) for the served model.

Optional capture of request/response bodies to SHIM_CAPTURE_DIR.
"""
import http.server
import itertools
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

UPSTREAM = os.environ.get("SHIM_UPSTREAM", "http://127.0.0.1:11434")
PORT = int(os.environ.get("SHIM_PORT", "11435"))
TEMP = float(os.environ.get("SHIM_TEMPERATURE", "0"))
CAP = os.environ.get("SHIM_CAPTURE_DIR")
_n = itertools.count(1)

# Substring every ceiling refusal carries. The claude-local provider matches it
# in the child's output to classify the failure (local_provider.py).
CEILING_ERROR_MARKER = "ateles-local context ceiling exceeded"
DEFAULT_CHARS_PER_TOKEN = 3.0
DEFAULT_OUTPUT_RESERVE = 1024

_FUNC = re.compile(r"(?:<tool_call>\s*)?<function=([A-Za-z0-9_.\-]+)>(.*?)</function>\s*(?:</tool_call>)?", re.DOTALL)
_PARAM = re.compile(r"<parameter=([A-Za-z0-9_.\-]+)>\n?(.*?)\n?</parameter>", re.DOTALL)


def _text(content):
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))


def normalize(body: dict) -> dict:
    msgs = body.get("messages")
    if isinstance(msgs, list) and msgs:
        systems = [_text(m.get("content", "")) for m in msgs if m.get("role") == "system"]
        rest = [m for m in msgs if m.get("role") != "system"]
        if systems:
            body["messages"] = [{"role": "system", "content": "\n\n".join(s for s in systems if s)}] + rest
    body.setdefault("temperature", TEMP)
    return body


def estimate_prompt_tokens(body: dict, chars_per_token: float = DEFAULT_CHARS_PER_TOKEN) -> int:
    """Upper-biased token estimate for everything the model must read."""
    serialized = json.dumps(body.get("messages") or []) + json.dumps(body.get("tools") or [])
    return math.ceil(len(serialized) / chars_per_token)


def ceiling_error(body: dict, ceiling: int, *, chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
                  output_reserve: int = DEFAULT_OUTPUT_RESERVE) -> dict | None:
    """Return an OpenAI-shaped error body when the prompt would not fit, else None."""
    estimate = estimate_prompt_tokens(body, chars_per_token)
    if estimate + output_reserve <= ceiling:
        return None
    return {"error": {
        "message": (f"{CEILING_ERROR_MARKER}: estimated {estimate} prompt tokens + "
                    f"{output_reserve} reserved for output exceeds the {ceiling}-token "
                    "context window; refusing rather than letting the backend truncate"),
        "type": "invalid_request_error",
        "code": "context_length_exceeded",
    }}


def _coerce(value: str, schema: dict):
    t = (schema or {}).get("type")
    if t in ("integer", "number", "boolean", "array", "object"):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def repair(resp: dict, tools: list) -> bool:
    """Convert text-form tool calls in the first choice into tool_calls. Returns True if repaired."""
    declared = {t["function"]["name"]: t["function"].get("parameters", {}) for t in tools or [] if t.get("function")}
    msg = resp.get("choices", [{}])[0].get("message", {})
    content = msg.get("content") or ""
    if msg.get("tool_calls") or not declared or "<function=" not in content:
        return False
    calls = []
    for m in _FUNC.finditer(content):
        name = m.group(1)
        if name not in declared:
            continue
        props = declared[name].get("properties", {})
        args = {k: _coerce(v, props.get(k)) for k, v in _PARAM.findall(m.group(2))}
        calls.append({"id": "call_" + uuid.uuid4().hex[:24], "type": "function",
                      "function": {"name": name, "arguments": json.dumps(args)}})
    if not calls:
        return False
    msg["tool_calls"] = calls
    msg["content"] = _FUNC.sub("", content).strip() or None
    resp["choices"][0]["finish_reason"] = "tool_calls"
    return True


def to_sse(resp: dict) -> bytes:
    base = {"id": resp.get("id", "chatcmpl-shim"), "object": "chat.completion.chunk",
            "created": resp.get("created", int(time.time())), "model": resp.get("model", "")}
    ch = resp["choices"][0]
    msg = ch.get("message", {})
    out = []

    def emit(delta, finish=None, **extra):
        out.append("data: " + json.dumps(dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": finish}], **extra)) + "\n\n")

    emit({"role": "assistant", "content": msg.get("content") or ""})
    for i, tc in enumerate(msg.get("tool_calls") or []):
        emit({"tool_calls": [dict(tc, index=i)]})
    emit({}, ch.get("finish_reason") or "stop")
    if resp.get("usage"):
        out.append("data: " + json.dumps(dict(base, choices=[], usage=resp["usage"])) + "\n\n")
    out.append("data: [DONE]\n\n")
    return "".join(out).encode()


def load_limits(env=None) -> tuple[int, float, int]:
    """Read (ceiling, chars_per_token, output_reserve); raise ValueError when unusable."""
    env = os.environ if env is None else env
    raw = (env.get("SHIM_CONTEXT_CEILING") or "").strip()
    if not raw:
        raise ValueError("SHIM_CONTEXT_CEILING is not set; set it to the served model's num_ctx")
    ceiling = int(raw)
    cpt = float(env.get("SHIM_CHARS_PER_TOKEN") or DEFAULT_CHARS_PER_TOKEN)
    reserve = int(env.get("SHIM_OUTPUT_RESERVE") or DEFAULT_OUTPUT_RESERVE)
    if ceiling <= 0 or cpt <= 0 or reserve < 0 or reserve >= ceiling:
        raise ValueError(f"unusable limits: ceiling={ceiling} chars_per_token={cpt} output_reserve={reserve}")
    return ceiling, cpt, reserve


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    upstream = UPSTREAM
    limits: tuple[int, float, int] = (0, DEFAULT_CHARS_PER_TOKEN, DEFAULT_OUTPUT_RESERVE)

    def _send(self, code, body: bytes, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _passthrough(self, method, raw):
        req = urllib.request.Request(self.upstream + self.path, data=raw, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            r = urllib.request.urlopen(req, timeout=900)
            self._send(r.status, r.read(), r.headers.get("Content-Type", "application/json"))
        except urllib.error.HTTPError as e:
            self._send(e.code, e.read(), "application/json")

    def do_GET(self):
        self._passthrough("GET", None)

    def do_POST(self):
        k = next(_n)
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if not self.path.endswith("/chat/completions"):
            return self._passthrough("POST", raw)
        try:
            body = normalize(json.loads(raw))
        except ValueError:
            return self._passthrough("POST", raw)
        ceiling, cpt, reserve = self.limits
        refusal = ceiling_error(body, ceiling, chars_per_token=cpt, output_reserve=reserve)
        if refusal is not None:
            return self._send(400, json.dumps(refusal).encode(), "application/json")
        want_stream = bool(body.pop("stream", False))
        body.pop("stream_options", None)
        body["stream"] = False
        if CAP:
            with open(os.path.join(CAP, f"{k:04d}_req.json"), "w") as fh:
                fh.write(json.dumps(body))
        req = urllib.request.Request(self.upstream + self.path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            resp = json.loads(urllib.request.urlopen(req, timeout=900).read())
        except urllib.error.HTTPError as e:
            return self._send(e.code, e.read(), "application/json")
        repaired = repair(resp, body.get("tools"))
        if CAP:
            with open(os.path.join(CAP, f"{k:04d}_resp.json"), "w") as fh:
                fh.write(json.dumps(dict(resp, _shim_repaired=repaired)))
        if want_stream:
            self._send(200, to_sse(resp), "text/event-stream")
        else:
            self._send(200, json.dumps(resp).encode(), "application/json")

    def log_message(self, *args):
        pass


def main() -> int:
    try:
        Handler.limits = load_limits()
    except ValueError as exc:
        print(f"ollama_shim: refusing to start: {exc}", file=sys.stderr)
        return 2
    if CAP:
        os.makedirs(CAP, exist_ok=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
