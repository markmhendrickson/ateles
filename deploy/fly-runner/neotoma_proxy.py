"""A local forwarder that signs every request to Neotoma's MCP endpoint.

A Fly job's agent reaches Neotoma through this forwarder instead of holding
the role's AAuth key. The forwarder listens on 127.0.0.1, accepts only the
MCP endpoint (``/mcp``), forwards a fixed set of request headers, adds the
HTTP message signature headers produced by a signing function the caller
supplies, and streams the upstream response back as it arrives.

Not wired into ``run_job.py`` yet: a later change starts it as root inside
the runner and points the agent's MCP configuration at it. The signing
function closes over the key, so the key never enters this module's state
and never reaches the agent process.

Standard library only. Logs method, path, status and byte counts; never
headers, bodies or key material.
"""

from __future__ import annotations

import http.client
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Mapping, Optional
from urllib.parse import urlsplit

log = logging.getLogger("neotoma_proxy")

MCP_PATH = "/mcp"
MAX_BODY_BYTES = 8 * 1024 * 1024
READ_CHUNK = 65536
ALLOWED_METHODS = ("GET", "POST", "DELETE")
DEFAULT_CONTENT_TYPE = "application/json"

# Request headers the agent may pass through. Everything else is dropped,
# notably authorization, x-connection-id, cookie, host and x-forwarded-*.
FORWARDED_REQUEST_HEADERS = (
    "accept",
    "content-type",
    "mcp-session-id",
    "mcp-protocol-version",
    "last-event-id",
    "user-agent",
)
# Response headers passed back to the agent.
FORWARDED_RESPONSE_HEADERS = (
    "content-type",
    "mcp-session-id",
    "mcp-protocol-version",
    "cache-control",
)

Signer = Callable[[str, str, Optional[bytes], str], Mapping[str, str]]


class NeotomaSigningProxy:
    """Forward ``/mcp`` on a loopback port to ``upstream_base + "/mcp"``."""

    def __init__(
        self,
        upstream_base: str,
        sign: Signer,
        *,
        connect: Optional[Callable[[], http.client.HTTPConnection]] = None,
        allowed_peer_uid: Optional[int] = None,
        allow_any_peer_for_tests: bool = False,
    ) -> None:
        parts = urlsplit(upstream_base)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.path
            or parts.query
            or parts.fragment
            or upstream_base.endswith("/")
        ):
            raise ValueError("upstream_base must be an https:// origin with no path or trailing slash")
        if allowed_peer_uid is None and not allow_any_peer_for_tests:
            raise ValueError("allowed_peer_uid is required")
        self.upstream_base = upstream_base
        self.upstream_url = upstream_base + MCP_PATH
        self._sign = sign
        # Stored for the slice that enforces the peer check; not enforced here.
        self.allowed_peer_uid = allowed_peer_uid
        if connect is None:
            host, port = parts.hostname, parts.port

            def connect() -> http.client.HTTPConnection:
                return http.client.HTTPSConnection(host, port)

        self._connect = connect
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> int:
        """Serve on 127.0.0.1 on an ephemeral port; return the port."""
        proxy = self

        class Handler(_ProxyHandler):
            pass

        Handler.proxy = proxy
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="neotoma-proxy", daemon=True)
        self._thread.start()
        return server.server_address[1]

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    proxy: NeotomaSigningProxy

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    def do_PATCH(self) -> None:
        self._handle()

    def do_HEAD(self) -> None:
        self._handle()

    def do_OPTIONS(self) -> None:
        self._handle()

    # BaseHTTPRequestHandler logs request lines and errors on its own; this
    # module logs its own summary line instead.
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    def _handle(self) -> None:
        method = self.command
        path, sep, _query = self.path.partition("?")
        if path != MCP_PATH:
            return self._reject(404, method, path)
        if sep:
            return self._reject(400, method, path)
        if method not in ALLOWED_METHODS:
            return self._reject(405, method, path, extra={"Allow": ", ".join(ALLOWED_METHODS)})

        length_header = self.headers.get("Content-Length")
        if length_header is None:
            if self.headers.get("Transfer-Encoding"):
                return self._reject(411, method, path)
            body = b""
        else:
            try:
                length = int(length_header)
            except ValueError:
                return self._reject(400, method, path)
            if length < 0:
                return self._reject(400, method, path)
            if length > MAX_BODY_BYTES:
                return self._reject(413, method, path)
            body = self.rfile.read(length) if length else b""
            if len(body) != length:
                return self._reject(400, method, path)

        headers = {}
        for name in FORWARDED_REQUEST_HEADERS:
            value = self.headers.get(name)
            if value is not None:
                headers[name] = value
        content_type = headers.get("content-type") or DEFAULT_CONTENT_TYPE
        if method == "POST":
            headers["content-type"] = content_type
        signed_body = body if method == "POST" and body else None
        signature = self.proxy._sign(method, self.proxy.upstream_url, signed_body, content_type)
        for name, value in signature.items():
            headers[name] = value

        conn = None
        try:
            conn = self.proxy._connect()
            conn.request(method, MCP_PATH, body=body if method == "POST" else None, headers=headers)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException):
            if conn is not None:
                conn.close()
            log.warning("neotoma proxy %s %s upstream unreachable", method, path)
            return self._reject(502, method, path, bytes_in=len(body))

        try:
            self._relay(resp, method, path, len(body))
        finally:
            conn.close()

    def _relay(self, resp: http.client.HTTPResponse, method: str, path: str, bytes_in: int) -> None:
        status = resp.status
        self.send_response(status)
        for name in FORWARDED_RESPONSE_HEADERS:
            value = resp.getheader(name)
            if value is not None:
                self.send_header(name, value)
        if status in (204, 304) or 100 <= status < 200:
            self.send_header("Content-Length", "0")
            self.end_headers()
            log.info("neotoma proxy %s %s %d in=%d out=0", method, path, status, bytes_in)
            return
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.flush()

        bytes_out = 0
        try:
            while True:
                try:
                    chunk = resp.read1(READ_CHUNK)
                except (OSError, http.client.HTTPException):
                    # The upstream failed mid-stream; the client cannot be told
                    # with a status any more, so end the connection unterminated.
                    self.close_connection = True
                    log.warning("neotoma proxy %s %s upstream stream broke after %d bytes", method, path, bytes_out)
                    return
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
                bytes_out += len(chunk)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            log.info("neotoma proxy %s %s client went away after %d bytes", method, path, bytes_out)
            return
        log.info("neotoma proxy %s %s %d in=%d out=%d", method, path, status, bytes_in, bytes_out)

    def _reject(
        self,
        status: int,
        method: str,
        path: str,
        *,
        extra: Optional[Mapping[str, str]] = None,
        bytes_in: int = 0,
    ) -> None:
        # The request body may be unread, so this connection cannot be reused.
        self.close_connection = True
        self.send_response(status)
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        log.info("neotoma proxy %s %s %d in=%d out=0", method, path, status, bytes_in)
