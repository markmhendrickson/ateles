"""A loopback proxy that sits between a candidate model's CLI and its endpoint.

It does three jobs, all of which exist so the candidate's own process never
needs more than a throwaway token:

1. It adds the real endpoint credential on the way out. The CLI is given a
   placeholder token and a loopback base URL, so the credential never enters
   the candidate's environment, argv or filesystem view.
2. It records what the PROVIDER reports for every response (token counts and,
   when present, the provider's own ``usage.cost``). Nothing is estimated from a
   price list: if a response carries no cost the run's cost stays unknown.
3. It can rewrite the request body (used to keep a local model's thinking
   channel off, which a local model needs to answer at all in some cases).

Streaming responses are passed through chunk by chunk and parsed on the side.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-encoding",
    "host",
}
_AUTH_HEADERS = {"authorization", "x-api-key", "anthropic-auth-token"}

TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


@dataclass
class RequestRecord:
    path: str
    status: int = 0
    message_id: str = ""
    model: str = ""
    usage: dict = field(default_factory=dict)
    error: str = ""


def _merge_usage(into: dict, usage: object) -> None:
    if not isinstance(usage, dict):
        return
    for key, value in usage.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            into[key] = value
        elif key == "cost_details" and isinstance(value, dict):
            into[key] = value


def parse_sse_events(text: str) -> list[dict]:
    """Return the JSON payloads of every ``data:`` line in an SSE body."""
    events: list[dict] = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def record_from_events(record: RequestRecord, events: list[dict]) -> None:
    """Fold usage / ids from streamed (or single) response objects into *record*."""
    for ev in events:
        message = ev.get("message")
        if isinstance(message, dict):
            record.message_id = record.message_id or str(message.get("id") or "")
            record.model = record.model or str(message.get("model") or "")
            _merge_usage(record.usage, message.get("usage"))
        if ev.get("type") == "message" or "usage" in ev:
            record.message_id = record.message_id or str(ev.get("id") or "")
            record.model = record.model or str(ev.get("model") or "")
        _merge_usage(record.usage, ev.get("usage"))
        if ev.get("type") == "error" and isinstance(ev.get("error"), dict):
            record.error = str(ev["error"].get("message") or "error")[:200]


@dataclass
class ProxySummary:
    requests: int
    ok_requests: int
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    cost_usd: float | None
    cost_requests_reported: int
    models: list[str]
    errors: list[str]

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def summarize(records: list[RequestRecord]) -> ProxySummary:
    """Totals across requests. A total is ``None`` unless the provider reported
    it on every successful request; a partial sum would read as measured."""
    good = [r for r in records if 200 <= r.status < 300]

    def total(key: str) -> int | None:
        if not good or any(key not in r.usage for r in good):
            return None
        return int(sum(r.usage[key] for r in good))

    costed = [r for r in good if isinstance(r.usage.get("cost"), (int, float))]
    cost = (
        round(float(sum(r.usage["cost"] for r in costed)), 6)
        if good and len(costed) == len(good)
        else None
    )
    return ProxySummary(
        requests=len(records),
        ok_requests=len(good),
        input_tokens=total("input_tokens"),
        output_tokens=total("output_tokens"),
        cache_read_tokens=total("cache_read_input_tokens"),
        cache_write_tokens=total("cache_creation_input_tokens"),
        cost_usd=cost,
        cost_requests_reported=len(costed),
        models=sorted({r.model for r in good if r.model}),
        errors=[r.error for r in records if r.error][:5],
    )


class MeteringProxy:
    """Single-run proxy. Use as a context manager: ``with MeteringProxy(...) as p``."""

    def __init__(
        self,
        upstream: str,
        *,
        api_key: str | None = None,
        rewrite_request: Callable[[dict], dict] | None = None,
        generation_lookup: Callable[[str], float | None] | None = None,
        timeout: float = 1800.0,
    ) -> None:
        parsed = urllib.parse.urlparse(upstream)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"bad upstream URL {upstream!r}")
        self.upstream = upstream.rstrip("/")
        self._scheme = parsed.scheme
        self._host = parsed.hostname
        self._port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self._base_path = parsed.path.rstrip("/")
        self._api_key = api_key
        self._rewrite = rewrite_request
        self._generation_lookup = generation_lookup
        self._timeout = timeout
        self.records: list[RequestRecord] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------------

    def __enter__(self) -> "MeteringProxy":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def base_url(self) -> str:
        assert self._server is not None, "proxy not started"
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> str:
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_a: object) -> None:  # silence request logging
                return

            def _handle(self) -> None:
                proxy._forward(self)

            do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _handle

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # -- forwarding --------------------------------------------------------------

    def _forward(self, h: BaseHTTPRequestHandler) -> None:
        length = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(length) if length else b""
        ctype = (h.headers.get("Content-Type") or "").lower()
        if body and self._rewrite and "json" in ctype:
            try:
                body = json.dumps(self._rewrite(json.loads(body))).encode()
            except ValueError:
                pass
        headers = {
            k: v
            for k, v in h.headers.items()
            if k.lower() not in _HOP_BY_HOP and k.lower() not in _AUTH_HEADERS
        }
        headers["Accept-Encoding"] = "identity"
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
            headers["x-api-key"] = self._api_key
        if body:
            headers["Content-Length"] = str(len(body))
        record = RequestRecord(path=h.path.split("?")[0])
        conn_cls = (
            http.client.HTTPSConnection
            if self._scheme == "https"
            else http.client.HTTPConnection
        )
        try:
            conn = conn_cls(self._host, self._port, timeout=self._timeout)
            conn.request(
                h.command, self._base_path + h.path, body=body or None, headers=headers
            )
            resp = conn.getresponse()
        except Exception as exc:  # upstream unreachable
            record.status = 502
            record.error = f"upstream: {type(exc).__name__}"
            with self._lock:
                self.records.append(record)
            h.send_response(502)
            h.end_headers()
            return
        record.status = resp.status
        h.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in _HOP_BY_HOP:
                h.send_header(k, v)
        h.send_header("Connection", "close")
        h.end_headers()
        streamed = "event-stream" in (resp.getheader("Content-Type") or "").lower()
        captured = bytearray()
        try:
            while True:
                chunk = resp.read1(8192) if hasattr(resp, "read1") else resp.read(8192)
                if not chunk:
                    break
                if len(captured) < 64 * 1024 * 1024:
                    captured.extend(chunk)
                h.wfile.write(chunk)
                h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            record.error = record.error or "client closed early"
        finally:
            conn.close()
        text = captured.decode("utf-8", "replace")
        try:
            if streamed:
                record_from_events(record, parse_sse_events(text))
            elif text.strip().startswith("{"):
                record_from_events(record, [json.loads(text)])
        except ValueError:
            pass
        with self._lock:
            self.records.append(record)

    # -- results -----------------------------------------------------------------

    def summary(self) -> ProxySummary:
        """Totals for this run. Costs missing from a response are looked up once
        by generation id (the provider's own accounting), never estimated."""
        with self._lock:
            records = list(self.records)
        if self._generation_lookup:
            for r in records:
                if (
                    200 <= r.status < 300
                    and not isinstance(r.usage.get("cost"), (int, float))
                    and r.message_id
                ):
                    try:
                        cost = self._generation_lookup(r.message_id)
                    except Exception:
                        cost = None
                    if isinstance(cost, (int, float)):
                        r.usage["cost"] = float(cost)
        return summarize(records)


def wait_for(predicate: Callable[[], bool], seconds: float, step: float = 0.2) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()
