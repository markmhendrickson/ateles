"""The metering proxy: forwards, adds the real credential, and records what the
provider reports, never an estimate."""

from __future__ import annotations

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from review_replay.metering_proxy import MeteringProxy, RequestRecord, summarize

# Assembled at runtime so no literal in this file looks like a credential.
FAKE_UPSTREAM_CREDENTIAL = "-".join(["fake", "upstream", "credential", "for", "tests"])

SSE = (
    'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","model":"vendor/m",'
    '"usage":{"input_tokens":120,"cache_read_input_tokens":30,"output_tokens":1}}}\n\n'
    'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":42,"cost":0.0123}}\n\n'
    'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


class _Upstream:
    def __init__(self, with_cost: bool = True):
        self.seen: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                return

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.seen.append(
                    {
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": json.loads(body or b"{}"),
                        "path": self.path,
                    }
                )
                text = SSE if with_cost else SSE.replace(',"cost":0.0123', "")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(text.encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _post(proxy: MeteringProxy, body: dict, headers: dict | None = None):
    host, port = proxy.base_url.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    conn.request(
        "POST",
        "/v1/messages",
        body=json.dumps(body),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    return resp.status, data


def test_proxy_forwards_stream_adds_key_and_records_provider_cost():
    up = _Upstream()
    try:
        with MeteringProxy(up.url, api_key=FAKE_UPSTREAM_CREDENTIAL) as proxy:
            status, data = _post(
                proxy,
                {"messages": []},
                {"x-api-key": "placeholder", "Authorization": "Bearer placeholder"},
            )
            summary = proxy.summary()
    finally:
        up.close()
    assert status == 200 and "message_stop" in data
    seen = up.seen[0]
    assert seen["headers"]["authorization"] == f"Bearer {FAKE_UPSTREAM_CREDENTIAL}"
    assert "placeholder" not in json.dumps(seen["headers"])
    assert (summary.requests, summary.input_tokens, summary.output_tokens) == (
        1,
        120,
        42,
    )
    assert summary.cache_read_tokens == 30
    assert summary.cost_usd == pytest.approx(0.0123)
    assert summary.models == ["vendor/m"]


def test_cost_is_unknown_not_estimated_when_provider_reports_none():
    up = _Upstream(with_cost=False)
    try:
        with MeteringProxy(up.url) as proxy:
            _post(proxy, {})
            summary = proxy.summary()
    finally:
        up.close()
    assert summary.cost_usd is None
    assert summary.input_tokens == 120


def test_generation_lookup_fills_a_missing_cost_from_the_provider():
    up = _Upstream(with_cost=False)
    asked: list[str] = []

    def lookup(gen_id):
        asked.append(gen_id)
        return 0.5

    try:
        with MeteringProxy(up.url, generation_lookup=lookup) as proxy:
            _post(proxy, {})
            summary = proxy.summary()
    finally:
        up.close()
    assert asked == ["msg_1"] and summary.cost_usd == 0.5


def test_request_rewrite_is_applied_to_the_body():
    up = _Upstream()
    try:
        with MeteringProxy(
            up.url, rewrite_request=lambda b: {**b, "thinking": {"type": "disabled"}}
        ) as proxy:
            _post(
                proxy,
                {"model": "x", "thinking": {"type": "enabled", "budget_tokens": 9}},
            )
    finally:
        up.close()
    assert up.seen[0]["body"]["thinking"] == {"type": "disabled"}


def test_total_is_none_when_one_request_lacks_a_figure():
    a = RequestRecord(path="/", status=200, usage={"input_tokens": 5, "cost": 0.1})
    b = RequestRecord(path="/", status=200, usage={"input_tokens": 7})
    s = summarize([a, b])
    assert s.cost_usd is None and s.cost_requests_reported == 1 and s.input_tokens == 12
    c = RequestRecord(path="/", status=200, usage={})
    assert summarize([a, c]).input_tokens is None
