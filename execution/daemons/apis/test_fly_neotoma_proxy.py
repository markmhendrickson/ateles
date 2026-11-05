"""The Fly runner's signing forwarder for Neotoma MCP (deploy/fly-runner/neotoma_proxy.py).

Lives beside the Apis suite so the required ``Run swarm daemon (apis) tests``
CI lane collects it, like test_fly_runner_image.py.
"""

from __future__ import annotations

import http.client
import importlib.util
import io
import json
import sys
import threading
import time
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


# --- Peer uid check -------------------------------------------------------

_PROC_NET_TCP = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1001 1 0 100 0 0 10 0
   1: 0100007F:1F90 0100007F:D431 01 00000000:00000000 00:00000000 00000000     0        0 1002 1 0 20 4 30 10 -1
   2: 0100007F:D431 0100007F:1F90 01 00000000:00000000 00:00000000 00000000 10001        0 1003 1 0 20 4 30 10 -1
"""
_PROC_NET_TCP6 = """\
  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000000000000000000001000000:C350 00000000000000000000000001000000:1F90 01 00000000:00000000 00:00000000 00000000  4242        0 2001 1 0 20 4 30 10 -1
"""


def test_proc_net_peer_uid_reads_the_client_row(tmp_path):
    tcp = tmp_path / "tcp"
    tcp.write_text(_PROC_NET_TCP)
    tcp6 = tmp_path / "tcp6"
    tcp6.write_text(_PROC_NET_TCP6)
    paths = (str(tcp), str(tcp6))
    # 0xD431 = 54321 is the client's port, 0x1F90 = 8080 the proxy's. The
    # server-side row (uid 0) has the ports the other way round.
    assert neotoma_proxy.proc_net_peer_uid("127.0.0.1", 54321, "127.0.0.1", 8080, paths=paths) == 10001
    assert neotoma_proxy.proc_net_peer_uid("127.0.0.1", 8080, "127.0.0.1", 54321, paths=paths) == 0
    assert neotoma_proxy.proc_net_peer_uid("::1", 50000, "::1", 8080, paths=paths) == 4242
    assert neotoma_proxy.proc_net_peer_uid("127.0.0.1", 54322, "127.0.0.1", 8080, paths=paths) is None
    assert neotoma_proxy.proc_net_peer_uid("127.0.0.1", 54321, "127.0.0.1", 8080, paths=(str(tmp_path / "missing"),)) is None


def test_proc_net_peer_uid_ignores_non_loopback_rows_and_refuses_ambiguity(tmp_path):
    tcp = tmp_path / "tcp"
    # Row 0 reuses the agent's port pair on a non-loopback address (10.0.0.5)
    # under another uid; only the loopback row 1 may answer.
    tcp.write_text(
        _PROC_NET_TCP.splitlines()[0]
        + "\n   0: 0500000A:D431 0500000A:1F90 01 00000000:00000000 00:00000000 00000000  1000 0 1 1 0 20 4 30 10 -1"
        + "\n   1: 0100007F:D431 0100007F:1F90 01 00000000:00000000 00:00000000 00000000 10001 0 2 1 0 20 4 30 10 -1\n"
    )
    assert neotoma_proxy.proc_net_peer_uid("127.0.0.1", 54321, "127.0.0.1", 8080, paths=(str(tcp),)) == 10001
    # An IPv4-mapped loopback row under a different uid makes the answer ambiguous.
    tcp6 = tmp_path / "tcp6"
    tcp6.write_text(
        _PROC_NET_TCP6.splitlines()[0]
        + "\n   0: 0000000000000000FFFF00000100007F:D431 0000000000000000FFFF00000100007F:1F90 01"
        " 00000000:00000000 00:00000000 00000000  1000 0 3 1 0 20 4 30 10 -1\n"
    )
    assert neotoma_proxy.proc_net_peer_uid("127.0.0.1", 54321, "127.0.0.1", 8080, paths=(str(tcp6),)) == 1000
    assert neotoma_proxy.proc_net_peer_uid("127.0.0.1", 54321, "127.0.0.1", 8080, paths=(str(tcp), str(tcp6))) is None



def _row(local, remote, state, uid):
    return f"   0: {local} {remote} {state} 00000000:00000000 00:00000000 00000000 {uid:>5} 0 1 1 0 20 4 30 10 -1"


def test_proc_net_peer_uid_requires_exact_connection_identity(tmp_path):
    header = _PROC_NET_TCP.splitlines()[0]

    def lookup(paths):
        return neotoma_proxy.proc_net_peer_uid("127.0.0.1", 54321, "127.0.0.1", 8080, paths=paths)

    tcp = tmp_path / "tcp"
    # Same ports on another loopback address at either end, and a lingering
    # (TIME_WAIT, 06) row for the exact pair: none may contribute a uid.
    tcp.write_text(
        "\n".join(
            [
                header,
                _row("0200007F:D431", "0100007F:1F90", "01", 1111),
                _row("0100007F:D431", "0200007F:1F90", "01", 2222),
                _row("0100007F:D431", "0100007F:1F90", "06", 3333),
            ]
        )
        + "\n"
    )
    assert lookup((str(tcp),)) is None
    # A dual-stack row for the same pair (IPv4-mapped IPv6) matches; one with
    # another mapped client address does not.
    tcp6 = tmp_path / "tcp6"
    tcp6.write_text(
        "\n".join(
            [
                header,
                _row("0000000000000000FFFF00000200007F:D431", "0000000000000000FFFF00000100007F:1F90", "01", 4444),
                _row("0000000000000000FFFF00000100007F:D431", "0000000000000000FFFF00000100007F:1F90", "01", 5555),
            ]
        )
        + "\n"
    )
    assert lookup((str(tcp6),)) == 5555
    # The exact IPv4 row wins alongside the ignored ones.
    tcp.write_text(tcp.read_text() + _row("0100007F:D431", "0100007F:1F90", "01", 5555) + "\n")
    assert lookup((str(tcp), str(tcp6))) == 5555
    # Malformed addresses are skipped rather than raised.
    bad = tmp_path / "bad"
    bad.write_text(header + "\n" + _row("ZZ:D431", "0100007F:1F90", "01", 1) + "\n")
    assert lookup((str(bad),)) is None
    assert neotoma_proxy.proc_net_peer_uid("not-an-ip", 54321, "127.0.0.1", 8080, paths=(str(tcp),)) is None


@pytest.fixture
def uid_checked_proxy_port(upstream, sign_calls):
    def sign(method, url, body, content_type):
        sign_calls.append((method, url, body, content_type))
        return dict(SIGNATURE)

    proxy = neotoma_proxy.NeotomaSigningProxy(
        UPSTREAM_BASE,
        sign,
        connect=lambda: http.client.HTTPConnection("127.0.0.1", upstream.port, timeout=10),
        allowed_peer_uid=4242,
    )
    port = proxy.start()
    yield port
    proxy.stop()


@pytest.mark.parametrize(("peer_uid", "status"), [(1000, 403), (None, 403), (4242, 200)])
def test_only_the_allowed_peer_uid_is_forwarded(
    peer_uid, status, monkeypatch, upstream, sign_calls, uid_checked_proxy_port
):
    lookups = []

    def fake_peer_uid(client_host, client_port, server_host, server_port):
        lookups.append((client_host, client_port, server_host, server_port))
        return peer_uid

    monkeypatch.setattr(neotoma_proxy, "proc_net_peer_uid", fake_peer_uid)
    conn = _client(uid_checked_proxy_port)
    conn.connect()
    client_port = conn.sock.getsockname()[1]
    conn.request("POST", "/mcp", body=b"{}", headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == status
    assert lookups == [("127.0.0.1", client_port, "127.0.0.1", uid_checked_proxy_port)]
    if status == 403:
        assert upstream.requests == [] and sign_calls == []
    else:
        assert len(upstream.requests) == 1 and len(sign_calls) == 1



def test_failed_attribution_is_refused_without_signing_or_forwarding(
    monkeypatch, upstream, sign_calls, uid_checked_proxy_port
):
    monkeypatch.setattr(neotoma_proxy, "proc_net_peer_uid", lambda *a, **k: None)
    conn = _client(uid_checked_proxy_port)
    conn.request("POST", "/mcp", body=b"{}", headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 403
    assert upstream.requests == [] and sign_calls == []


@pytest.mark.parametrize("failing", ["getpeername", "getsockname"])
def test_unreadable_socket_addresses_are_refused(failing, monkeypatch, upstream, sign_calls, uid_checked_proxy_port):
    import socket

    lookups = []
    monkeypatch.setattr(neotoma_proxy, "proc_net_peer_uid", lambda *a, **k: lookups.append(a) or 4242)

    def boom(self):
        raise OSError("socket gone")

    monkeypatch.setattr(socket.socket, failing, boom)
    conn = _client(uid_checked_proxy_port)
    conn.request("POST", "/mcp", body=b"{}", headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 403
    assert lookups == []
    assert upstream.requests == [] and sign_calls == []


# --- Health check ---------------------------------------------------------


class _HealthUpstream:
    """A fake Neotoma answering /session and /mcp with configured responses."""

    def __init__(self, session=(200, b""), mcp=(200, b"{}"), hang=False):
        self.requests = []
        self.session = session
        self.mcp = mcp
        self.stop_hanging = threading.Event()
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                return

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                upstream.requests.append(
                    {"method": self.command, "path": self.path, "headers": _lower(dict(self.headers.items())), "body": body}
                )
                if hang:
                    upstream.stop_hanging.wait(timeout=10)
                    return
                if self.path == "/session":
                    status, payload = upstream.session
                elif self.command == "DELETE":
                    status, payload = 200, b""
                else:
                    status, payload = upstream.mcp
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Mcp-Session-Id", "health-session")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_DELETE = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.stop_hanging.set()
        self.server.shutdown()
        self.server.server_close()


def _admitted(admitted=True, reason=None):
    aauth = {"admitted": admitted}
    if reason is not None:
        aauth["admission_reason"] = reason
    return json.dumps({"aauth": aauth}).encode()


def _run_health(upstream, timeout=5.0):
    calls = []

    def sign(method, url, body, content_type):
        calls.append((method, url, body, content_type))
        return dict(SIGNATURE)

    result = neotoma_proxy.health_check(
        UPSTREAM_BASE,
        sign,
        connect=lambda: http.client.HTTPConnection("127.0.0.1", upstream.port),
        timeout=timeout,
    )
    return result, calls


def test_health_check_passes_when_admitted_and_mcp_initializes():
    upstream = _HealthUpstream(session=(200, _admitted()))
    try:
        result, calls = _run_health(upstream)
    finally:
        upstream.close()
    assert result is None
    assert [(r["method"], r["path"]) for r in upstream.requests] == [
        ("GET", "/session"),
        ("POST", "/mcp"),
        ("DELETE", "/mcp"),
    ]
    for seen in upstream.requests:
        assert seen["headers"]["signature"] == SIGNATURE["signature"]
    initialize = json.loads(upstream.requests[1]["body"])
    assert initialize["method"] == "initialize"
    assert initialize["params"]["protocolVersion"] == "2025-06-18"
    assert upstream.requests[1]["headers"]["accept"] == "application/json, text/event-stream"
    assert upstream.requests[2]["headers"]["mcp-session-id"] == "health-session"
    assert [(m, u) for m, u, _, _ in calls] == [
        ("GET", UPSTREAM_BASE + "/session"),
        ("POST", UPSTREAM_BASE + "/mcp"),
        ("DELETE", UPSTREAM_BASE + "/mcp"),
    ]
    assert calls[1][2] == upstream.requests[1]["body"]


def test_health_check_reports_an_unadmitted_key():
    upstream = _HealthUpstream(session=(200, _admitted(False, "no_match")))
    try:
        result, _ = _run_health(upstream)
    finally:
        upstream.close()
    assert result is not None and "no_match" in result
    assert result.startswith("no active agent_grant admits this key")
    assert [r["path"] for r in upstream.requests] == ["/session"]


def test_health_check_reports_encryption_refusing_aauth():
    upstream = _HealthUpstream(
        session=(200, _admitted()), mcp=(401, b'{"error":"Encryption is enabled; use a bearer token"}')
    )
    try:
        result, _ = _run_health(upstream)
    finally:
        upstream.close()
    assert result == "Neotoma has encryption on; it refuses AAuth on /mcp"


def test_health_check_reports_other_mcp_failures():
    upstream = _HealthUpstream(session=(200, _admitted()), mcp=(500, b"boom"))
    try:
        result, _ = _run_health(upstream)
    finally:
        upstream.close()
    assert result == "Neotoma /mcp answered 500"


def test_health_check_reports_an_upstream_that_never_answers():
    upstream = _HealthUpstream(hang=True)
    started = time.monotonic()
    try:
        result, _ = _run_health(upstream, timeout=0.5)
    finally:
        upstream.close()
    assert result == "Neotoma unreachable"
    assert time.monotonic() - started < 5


def test_health_check_bounds_and_sanitizes_admission_reason():
    cases = [
        ("no_match", "admission_reason=no_match)"),
        ("x" * 64, "admission_reason=" + "x" * 64 + ")"),
        ("x" * 65, "admission_reason=unrecognized)"),
        ("bad reason with spaces", "admission_reason=unrecognized)"),
        ("line\nbreak", "admission_reason=unrecognized)"),
        ("Bearer abc=", "admission_reason=unrecognized)"),
        (["no_match"], "admission_reason=unrecognized)"),
        ({"a": 1}, "admission_reason=unrecognized)"),
        (12345, "admission_reason=unrecognized)"),
        ("", "admission_reason=unrecognized)"),
    ]
    for reason, expected in cases:
        upstream = _HealthUpstream(session=(200, _admitted(False, reason)))
        try:
            result, _ = _run_health(upstream)
        finally:
            upstream.close()
        assert result is not None and result.startswith("no active agent_grant admits this key"), reason
        assert result.endswith(expected), (reason, result)
    # A top-level reason with no aauth block is still an explicit non-admission.
    upstream = _HealthUpstream(session=(200, json.dumps({"admission_reason": "no_match"}).encode()))
    try:
        result, _ = _run_health(upstream)
    finally:
        upstream.close()
    assert result == "no active agent_grant admits this key (admission_reason=no_match)"
    # A non-true admitted value without a reason is explicit too.
    upstream = _HealthUpstream(session=(200, json.dumps({"aauth": {"admitted": False}}).encode()))
    try:
        result, _ = _run_health(upstream)
    finally:
        upstream.close()
    assert result.startswith("no active agent_grant admits this key")


@pytest.mark.parametrize(
    ("session", "expected"),
    [
        ((500, b"secret-body"), "Neotoma /session answered 500"),
        ((403, b'{"aauth":{"admitted":false}}'), "Neotoma /session answered 403"),
        ((200, b""), "Neotoma /session returned no readable admission decision"),
        ((200, b"{not json"), "Neotoma /session returned no readable admission decision"),
        ((200, b"[1,2]"), "Neotoma /session returned no readable admission decision"),
        ((200, b'"text"'), "Neotoma /session returned no readable admission decision"),
        ((200, b"{}"), "Neotoma /session returned no readable admission decision"),
        ((200, b'{"aauth":{"tier":"x"}}'), "Neotoma /session returned no readable admission decision"),
        ((200, b'{"aauth":"nope"}'), "Neotoma /session returned no readable admission decision"),
    ],
)
def test_health_check_rejects_malformed_session_payload(session, expected):
    upstream = _HealthUpstream(session=session)
    try:
        result, _ = _run_health(upstream)
    finally:
        upstream.close()
    assert result == expected
    assert "secret-body" not in result
    assert [r["path"] for r in upstream.requests] == ["/session"]
