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

The agents' MCP clients take only a TCP URL, so the forwarder cannot use a
Unix socket. Instead it reads the connecting peer's uid from
``/proc/net/tcp`` and refuses every uid but the agent's, so no other process
on the machine can borrow the role's signature. ``health_check`` confirms,
before a job starts, that Neotoma admits the key on ``/mcp``.

Standard library only. Logs method, path, status and byte counts; never
headers, bodies or key material.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Mapping, Optional, Sequence
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

PROC_NET_TCP_PATHS = ("/proc/net/tcp", "/proc/net/tcp6")
SESSION_PATH = "/session"
HEALTH_INITIALIZE_BODY = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "ateles-fly-runner-health", "version": "1"},
        },
    },
    separators=(",", ":"),
).encode()
HEALTH_ACCEPT = "application/json, text/event-stream"
MAX_HEALTH_BODY_BYTES = 65536
PROC_NET_ESTABLISHED = "01"


def _parse_upstream_base(upstream_base: str):
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
    return parts


def _decode_proc_net_address(hex_address: str):
    """Decode a ``/proc/net/tcp`` address (host-order hex words) or return None.

    IPv4-mapped IPv6 addresses are normalised to IPv4.
    """
    try:
        raw = bytes.fromhex(hex_address)
    except ValueError:
        return None
    if len(raw) not in (4, 16):
        return None
    # The kernel prints each 32-bit word in host (little-endian) byte order.
    packed = b"".join(raw[i : i + 4][::-1] for i in range(0, len(raw), 4))
    if len(packed) == 4:
        return ipaddress.IPv4Address(packed)
    return _normalise_address(ipaddress.IPv6Address(packed))


def _normalise_address(address):
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped if mapped is not None else address


def _parse_host(host) -> Optional[object]:
    try:
        return _normalise_address(ipaddress.ip_address(host))
    except ValueError:
        return None


def proc_net_peer_uid(
    client_host: str,
    client_port: int,
    server_host: str,
    server_port: int,
    *,
    paths: Sequence[str] = PROC_NET_TCP_PATHS,
) -> Optional[int]:
    """Return the uid owning the client end of one accepted TCP connection.

    The client's socket appears in ``/proc/net/tcp`` (or ``tcp6``) with its own
    address and port as the local end and the server's as the remote end;
    column 8 of that row is the owning uid. A row counts only when both of its
    endpoints equal the given identity (IPv4-mapped IPv6 addresses are read as
    IPv4) and its state is ESTABLISHED. Returns None when no row matches, when
    matching rows disagree on the uid, or when no table can be read; the
    caller treats None as a refusal. Malformed rows are skipped.
    """
    want_client = _parse_host(client_host)
    want_server = _parse_host(server_host)
    if want_client is None or want_server is None:
        return None
    uids = set()
    for path in paths:
        try:
            with open(path, encoding="ascii", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        for line in lines[1:]:
            fields = line.split()
            if len(fields) < 8 or fields[3] != PROC_NET_ESTABLISHED:
                continue
            local_host, _, local_port = fields[1].rpartition(":")
            remote_host, _, remote_port = fields[2].rpartition(":")
            try:
                if int(local_port, 16) != client_port or int(remote_port, 16) != server_port:
                    continue
                uid = int(fields[7])
            except ValueError:
                continue
            if (
                _decode_proc_net_address(local_host) == want_client
                and _decode_proc_net_address(remote_host) == want_server
            ):
                uids.add(uid)
    return uids.pop() if len(uids) == 1 else None


_SAFE_REASON = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def _safe_reason(reason) -> str:
    if isinstance(reason, str) and _SAFE_REASON.fullmatch(reason):
        return reason
    return "unrecognized"


def health_check(
    upstream_base: str,
    sign: Signer,
    *,
    connect: Optional[Callable[[], http.client.HTTPConnection]] = None,
    timeout: float = 20.0,
) -> Optional[str]:
    """Return None when Neotoma admits the signing key on ``/mcp``, else a reason.

    Every request is signed. The reason never contains a header, body or key
    material beyond the server's own ``admission_reason`` when it is a short safe label.
    """
    parts = _parse_upstream_base(upstream_base)
    if connect is None:
        host, port = parts.hostname, parts.port

        def connect() -> http.client.HTTPConnection:
            return http.client.HTTPSConnection(host, port, timeout=timeout)

    def request(method: str, path: str, body: Optional[bytes], headers: Mapping[str, str]):
        content_type = headers.get("content-type", DEFAULT_CONTENT_TYPE)
        all_headers = dict(headers)
        all_headers.update(sign(method, upstream_base + path, body, content_type))
        conn = connect()
        conn.timeout = timeout
        try:
            conn.request(method, path, body=body, headers=all_headers)
            resp = conn.getresponse()
            status = resp.status
            session_id = resp.getheader("mcp-session-id")
            # A 200 from /mcp may be an event stream; only its status matters.
            read_body = not (method == "POST" and status == 200) and method != "DELETE"
            payload = resp.read(MAX_HEALTH_BODY_BYTES) if read_body else b""
        finally:
            conn.close()
        return status, session_id, payload

    try:
        status, _, payload = request("GET", SESSION_PATH, None, {"accept": DEFAULT_CONTENT_TYPE})
        if status != 200:
            return f"Neotoma /session answered {status}"
        try:
            data = json.loads(payload)
        except ValueError:
            data = None
        aauth = data.get("aauth") if isinstance(data, dict) else None
        reason = None
        if isinstance(aauth, dict):
            reason = aauth.get("admission_reason")
        if reason is None and isinstance(data, dict):
            reason = data.get("admission_reason")
        admitted = isinstance(aauth, dict) and aauth.get("admitted") is True
        explicit_denial = reason is not None or (
            isinstance(aauth, dict) and "admitted" in aauth and aauth["admitted"] is not True
        )
        if not admitted:
            if not explicit_denial:
                return "Neotoma /session returned no readable admission decision"
            return f"no active agent_grant admits this key (admission_reason={_safe_reason(reason)})"

        status, session_id, payload = request(
            "POST",
            MCP_PATH,
            HEALTH_INITIALIZE_BODY,
            {"content-type": DEFAULT_CONTENT_TYPE, "accept": HEALTH_ACCEPT},
        )
        if status == 401 and b"Encryption is enabled" in payload:
            return "Neotoma has encryption on; it refuses AAuth on /mcp"
        if status != 200:
            return f"Neotoma /mcp answered {status}"
    except (OSError, http.client.HTTPException):
        return "Neotoma unreachable"

    if session_id:
        try:
            request("DELETE", MCP_PATH, None, {"mcp-session-id": session_id})
        except (OSError, http.client.HTTPException):
            log.info("neotoma health check could not close its MCP session")
    return None


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
        parts = _parse_upstream_base(upstream_base)
        if allowed_peer_uid is None and not allow_any_peer_for_tests:
            raise ValueError("allowed_peer_uid is required")
        self.upstream_base = upstream_base
        self.upstream_url = upstream_base + MCP_PATH
        self._sign = sign
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

    def _connection_identity(self):
        """The accepted socket's (client host, client port, server host, server port), or None."""
        try:
            client = self.connection.getpeername()
            server = self.connection.getsockname()
            return (str(client[0]), int(client[1]), str(server[0]), int(server[1]))
        except (OSError, IndexError, TypeError, ValueError, AttributeError):
            return None

    def _handle(self) -> None:
        method = self.command
        path, sep, _query = self.path.partition("?")
        allowed_uid = self.proxy.allowed_peer_uid
        if allowed_uid is not None:
            # Checked before anything else, the body included: a refused peer
            # learns nothing and costs no upstream call.
            identity = self._connection_identity()
            peer_uid = proc_net_peer_uid(*identity) if identity is not None else None
            if peer_uid != allowed_uid:
                log.warning("neotoma proxy refused a connection from uid %s", peer_uid)
                return self._reject(403, method, path)
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
                    if resp.length is not None and resp.length > 0:
                        self.close_connection = True
                        log.warning(
                            "neotoma proxy %s %s upstream truncated with %d bytes outstanding",
                            method,
                            path,
                            resp.length,
                        )
                        return
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
