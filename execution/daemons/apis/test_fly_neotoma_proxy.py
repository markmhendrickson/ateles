"""The Fly runner's signing forwarder for Neotoma MCP (deploy/fly-runner/neotoma_proxy.py).

Lives beside the Apis suite so the required ``Run swarm daemon (apis) tests``
CI lane collects it, like test_fly_runner_image.py.
"""

from __future__ import annotations

import http.client
import importlib.util
import io
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_RUNNER_DIR = _REPO_ROOT / "deploy" / "fly-runner"

UPSTREAM_BASE = "https://neotoma.example"
SIGNATURE = {"signature": "sig1=:AAAA:", "signature-input": "sig1=()", "signature-key": "sig1=jwt"}


def _load_proxy():
    spec = importlib.util.spec_from_file_location("fly_neotoma_proxy", _RUNNER_DIR / "neotoma_proxy.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["fly_neotoma_proxy"] = module
    spec.loader.exec_module(module)
    return module


neotoma_proxy = _load_proxy()


class _Upstream:
    """A fake Neotoma MCP endpoint that records every request it receives."""

    def __init__(self):
        self.requests = []
        self.release = threading.Event()
        self.released_in_time = None
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                return

            def _record(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                upstream.requests.append(
                    {"method": self.command, "path": self.path, "headers": dict(self.headers.items()), "body": body}
                )
                return body

            def do_POST(self):
                body = self._record()
                if body == b"stream":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    first = b"data: a\n\n"
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(first), first))
                    self.wfile.flush()
                    upstream.released_in_time = upstream.release.wait(timeout=5)
                    second = b"data: b\n\n"
                    self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(second), second))
                    self.wfile.flush()
                    return
                self._reply(b'{"ok":true}')

            def do_GET(self):
                self._record()
                self._reply(b"data: hello\n\n", content_type="text/event-stream")

            def do_DELETE(self):
                self._record()
                self._reply(b"")

            def _reply(self, payload, content_type="application/json"):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Mcp-Session-Id", "session-from-upstream")
                self.send_header("Set-Cookie", "leak=1")
                self.send_header("X-Internal", "secret-ish")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def upstream():
    up = _Upstream()
    yield up
    up.close()


@pytest.fixture
def sign_calls():
    return []


@pytest.fixture
def proxy_port(upstream, sign_calls):
    def sign(method, url, body, content_type):
        sign_calls.append((method, url, body, content_type))
        return dict(SIGNATURE)

    proxy = neotoma_proxy.NeotomaSigningProxy(
        UPSTREAM_BASE,
        sign,
        connect=lambda: http.client.HTTPConnection("127.0.0.1", upstream.port, timeout=10),
        allow_any_peer_for_tests=True,
    )
    port = proxy.start()
    yield port
    proxy.stop()


def _client(port):
    return http.client.HTTPConnection("127.0.0.1", port, timeout=10)


def _lower(headers):
    return {k.lower(): v for k, v in headers.items()}


def test_requires_a_peer_uid_unless_testing():
    with pytest.raises(ValueError):
        neotoma_proxy.NeotomaSigningProxy(UPSTREAM_BASE, lambda *a: {})
    with pytest.raises(ValueError):
        neotoma_proxy.NeotomaSigningProxy(UPSTREAM_BASE + "/", lambda *a: {}, allowed_peer_uid=10001)
    proxy = neotoma_proxy.NeotomaSigningProxy(UPSTREAM_BASE, lambda *a: {}, allowed_peer_uid=10001)
    assert proxy.allowed_peer_uid == 10001


def test_post_forwards_exact_body_with_signature_and_strips_client_credentials(upstream, sign_calls, proxy_port):
    body = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
    conn = _client(proxy_port)
    conn.request(
        "POST",
        "/mcp",
        body=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer client-token",
            "X-Connection-Id": "conn-123",
            "Cookie": "a=b",
            "X-Forwarded-For": "10.0.0.1",
            "Mcp-Session-Id": "session-from-client",
        },
    )
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.read() == b'{"ok":true}'
    assert resp.getheader("Mcp-Session-Id") == "session-from-upstream"
    assert resp.getheader("Set-Cookie") is None
    assert resp.getheader("X-Internal") is None
    conn.close()

    assert len(upstream.requests) == 1
    seen = upstream.requests[0]
    assert seen["method"] == "POST" and seen["path"] == "/mcp"
    assert seen["body"] == body
    headers = _lower(seen["headers"])
    for name, value in SIGNATURE.items():
        assert headers[name] == value
    assert headers["mcp-session-id"] == "session-from-client"
    assert headers["content-type"] == "application/json"
    for dropped in ("authorization", "x-connection-id", "cookie", "x-forwarded-for"):
        assert dropped not in headers
    assert "client-token" not in repr(seen["headers"])

    assert sign_calls == [("POST", UPSTREAM_BASE + "/mcp", body, "application/json")]
    assert "127.0.0.1" not in sign_calls[0][1]


def test_post_without_content_type_signs_and_sends_application_json(upstream, sign_calls, proxy_port):
    conn = _client(proxy_port)
    conn.request("POST", "/mcp", body=b"{}", headers={"Content-Type": ""})
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert sign_calls[0][3] == "application/json"
    assert _lower(upstream.requests[0]["headers"])["content-type"] == "application/json"


def test_response_streams_before_upstream_finishes(upstream, proxy_port):
    conn = _client(proxy_port)
    conn.request("POST", "/mcp", body=b"stream", headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "text/event-stream"
    received = b""
    while b"data: a" not in received:
        chunk = resp.read1(65536)
        assert chunk, "stream ended before the first event"
        received += chunk
    # Only now may the upstream send the rest; a buffering proxy would never
    # have delivered 'a' while the upstream was still waiting here.
    upstream.release.set()
    received += resp.read()
    conn.close()
    assert upstream.released_in_time is True
    assert received == b"data: a\n\ndata: b\n\n"


class _MemorySocket:
    def __init__(self, response):
        self.response = response

    def makefile(self, mode):
        return io.BytesIO(self.response)


def _relay_memory_response(payload, declared_length):
    upstream_wire = (
        b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nContent-Type: text/event-stream\r\n\r\n"
        % declared_length
        + payload
    )
    upstream = http.client.HTTPResponse(_MemorySocket(upstream_wire))
    upstream.begin()
    handler = neotoma_proxy._ProxyHandler.__new__(neotoma_proxy._ProxyHandler)
    handler.wfile = io.BytesIO()
    handler.request_version = "HTTP/1.1"
    handler.requestline = "GET /mcp HTTP/1.1"
    handler.close_connection = False
    handler._relay(upstream, "GET", "/mcp", 0)
    downstream = http.client.HTTPResponse(_MemorySocket(handler.wfile.getvalue()))
    downstream.begin()
    assert downstream.status == 200
    assert downstream.getheader("Transfer-Encoding") == "chunked"
    return downstream, handler


def test_truncated_content_length_response_is_not_cleanly_terminated(caplog):
    downstream, handler = _relay_memory_response(b"ab", 50)
    with pytest.raises(http.client.IncompleteRead) as error:
        downstream.read()
    assert error.value.partial == b"ab"
    assert handler.close_connection is True
    assert "neotoma proxy GET /mcp upstream truncated with 48 bytes outstanding" in caplog.text


def test_zero_byte_truncated_content_length_response_is_not_cleanly_terminated(caplog):
    downstream, handler = _relay_memory_response(b"", 50)
    with pytest.raises(http.client.IncompleteRead) as error:
        downstream.read()
    assert error.value.partial == b""
    assert handler.close_connection is True
    assert "neotoma proxy GET /mcp upstream truncated with 50 bytes outstanding" in caplog.text


def test_complete_content_length_response_is_cleanly_terminated():
    downstream, handler = _relay_memory_response(b"ab", 2)
    assert downstream.read() == b"ab"
    assert handler.close_connection is False


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_get_and_delete_are_signed_without_body_and_round_trip_session(method, upstream, sign_calls, proxy_port):
    conn = _client(proxy_port)
    conn.request(method, "/mcp", headers={"Mcp-Session-Id": "session-from-client", "Authorization": "Bearer x"})
    resp = conn.getresponse()
    resp.read()
    assert resp.status == 200
    assert resp.getheader("Mcp-Session-Id") == "session-from-upstream"
    conn.close()

    assert sign_calls == [(method, UPSTREAM_BASE + "/mcp", None, "application/json")]
    seen = upstream.requests[0]
    assert seen["method"] == method
    headers = _lower(seen["headers"])
    assert headers["mcp-session-id"] == "session-from-client"
    assert headers["signature"] == SIGNATURE["signature"]
    assert "authorization" not in headers


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [("POST", "/other", 404), ("GET", "/mcp/", 404), ("POST", "/mcp?x=1", 400), ("PUT", "/mcp", 405)],
)
def test_rejections_never_reach_upstream(method, path, status, upstream, sign_calls, proxy_port):
    conn = _client(proxy_port)
    conn.request(method, path, body=b"{}" if method in ("POST", "PUT") else None)
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == status
    assert upstream.requests == []
    assert sign_calls == []


def test_chunked_request_body_gets_411(upstream, sign_calls, proxy_port):
    conn = _client(proxy_port)
    conn.putrequest("POST", "/mcp")
    conn.putheader("Transfer-Encoding", "chunked")
    conn.endheaders()
    conn.send(b"2\r\n{}\r\n0\r\n\r\n")
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 411
    assert upstream.requests == [] and sign_calls == []


def test_oversized_body_gets_413(upstream, sign_calls, proxy_port):
    conn = _client(proxy_port)
    conn.putrequest("POST", "/mcp")
    conn.putheader("Content-Length", str(neotoma_proxy.MAX_BODY_BYTES + 1))
    conn.endheaders()
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 413
    assert upstream.requests == [] and sign_calls == []


def test_unreachable_upstream_gets_502():
    proxy = neotoma_proxy.NeotomaSigningProxy(
        UPSTREAM_BASE, lambda *a: {}, connect=lambda: _Refusing(), allow_any_peer_for_tests=True
    )
    port = proxy.start()
    try:
        conn = _client(port)
        conn.request("POST", "/mcp", body=b"{}")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        assert resp.status == 502
    finally:
        proxy.stop()


class _Refusing(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("127.0.0.1", 9)

    def connect(self):
        raise ConnectionRefusedError("no upstream")
