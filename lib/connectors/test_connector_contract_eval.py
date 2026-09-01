"""Contract eval for connector status persistence.

Eval id: ateles-connector-status-contract
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from lib.connectors.base import ConnectorResult, ConnectorStatus, Freshness, stale_after_for
from lib.connectors.runner import run_connector
from lib.connectors.store import ConnectorStore, NeotomaUnavailable

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DAEMON_PATH = _REPO_ROOT / "execution" / "daemons" / "connectors" / "connectors_daemon.py"

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_spec = importlib.util.spec_from_file_location("connectors_daemon_eval", _DAEMON_PATH)
daemon = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(daemon)

EVAL_ID = "ateles-connector-status-contract"


class EvalConnector:
    name = "eval-stub"
    poll_interval_seconds = 300

    def __init__(self, result: ConnectorResult) -> None:
        self.result = result

    def observe(self) -> ConnectorResult:
        return self.result


class StubNeotoma:
    """Small Neotoma-like HTTP surface for the connector contract."""

    def __init__(self) -> None:
        self.entities: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.keys: dict[str, list[str]] = {}
        self.drop_query_fields: set[str] = set()
        self._next = 1
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "StubNeotoma":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size).decode() or "{}")
                outer.requests.append({"path": self.path, "body": body})
                if self.path == "/store":
                    response = outer._store(body)
                elif self.path == "/correct":
                    response = outer._correct(body)
                elif self.path == "/entities/query":
                    response = outer._query(body)
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                raw = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()
        assert self._thread is not None
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def store(self, fields: dict[str, Any]) -> str:
        entity_id = f"ent_{self._next}"
        self._next += 1
        self.entities.append({"entity_id": entity_id, "fields": dict(fields)})
        return entity_id

    def entity_fields(self, entity_type: str, name: str) -> dict[str, Any]:
        matches = [
            entity["fields"]
            for entity in self.entities
            if entity["fields"].get("entity_type") == entity_type
            and entity["fields"].get("connector_name") == name
        ]
        assert len(matches) == 1
        return matches[0]

    def entities_of_type(self, entity_type: str) -> list[dict[str, Any]]:
        return [
            entity for entity in self.entities
            if entity["fields"].get("entity_type") == entity_type
        ]

    def request_count(self, path: str, entity_type: str | None = None) -> int:
        count = 0
        for request in self.requests:
            if request["path"] != path:
                continue
            body = request["body"]
            if entity_type is None:
                count += 1
                continue
            if path == "/store":
                count += sum(
                    1
                    for entity in body.get("entities", [])
                    if entity.get("entity_type") == entity_type
                )
            elif body.get("entity_type") == entity_type:
                count += 1
        return count

    def _store(self, body: dict[str, Any]) -> dict[str, Any]:
        key = str(body.get("idempotency_key") or "")
        if key and key in self.keys:
            return {"entities": [{"entity_id": eid} for eid in self.keys[key]]}

        ids: list[str] = []
        for entity in body.get("entities", []):
            ids.append(self.store(entity))
        if key:
            self.keys[key] = ids
        return {"entities": [{"entity_id": eid} for eid in ids]}

    def _correct(self, body: dict[str, Any]) -> dict[str, Any]:
        for entity in self.entities:
            if entity["entity_id"] == body.get("entity_id"):
                entity["fields"][str(body["field"])] = body.get("value")
                break
        return {"success": True}

    def _query(self, body: dict[str, Any]) -> dict[str, Any]:
        entity_type = body.get("entity_type")
        search = body.get("search")
        matches = []
        for entity in self.entities:
            fields = entity["fields"]
            if fields.get("entity_type") != entity_type:
                continue
            if search and str(search) not in json.dumps(fields, sort_keys=True):
                continue
            visible = {
                key: value
                for key, value in fields.items()
                if key not in self.drop_query_fields
            }
            matches.append(
                {"entity_id": entity["entity_id"], "snapshot": {"snapshot": visible}}
            )
        return {"entities": matches[: int(body.get("limit") or 100)]}


@pytest.fixture
def stub_neotoma() -> StubNeotoma:
    with StubNeotoma() as server:
        yield server


def test_eval_id_is_stable():
    assert EVAL_ID == "ateles-connector-status-contract"


def test_success_writes_one_connector_status_with_expected_fields(stub_neotoma):
    store = ConnectorStore(base_url=stub_neotoma.url, token="token")

    result = run_connector(EvalConnector(ConnectorResult.success(records_written=2)), store)

    fields = stub_neotoma.entity_fields("connector_status", "eval-stub")
    assert result.ok
    assert len(stub_neotoma.entities_of_type("connector_status")) == 1
    assert fields["status"] == "ok"
    assert fields["last_attempt_at"]
    assert fields["last_success_at"] == fields["last_attempt_at"]
    assert fields["poll_interval_seconds"] == 300
    assert fields["stale_after_seconds"] == stale_after_for(300)
    assert fields["consecutive_failures"] == 0
    assert store.read_status("eval-stub") is not None
    assert store.verify_stored("connector_status", "status", "ok", search="eval-stub")


def test_idempotent_success_rerun_corrects_existing_status(stub_neotoma):
    store = ConnectorStore(base_url=stub_neotoma.url, token="token")
    connector = EvalConnector(ConnectorResult.success(records_written=2))

    run_connector(connector, store)
    run_connector(connector, store)

    assert len(stub_neotoma.entities_of_type("connector_status")) == 1
    assert stub_neotoma.request_count("/store", "connector_status") == 1
    assert stub_neotoma.request_count("/correct", "connector_status") > 0


def test_failure_preserves_last_success_and_freshness_uses_success(stub_neotoma):
    store = ConnectorStore(base_url=stub_neotoma.url, token="token")

    run_connector(EvalConnector(ConnectorResult.success(records_written=2)), store)
    first = dict(stub_neotoma.entity_fields("connector_status", "eval-stub"))
    run_connector(EvalConnector(ConnectorResult.failure("HTTP 502")), store)

    fields = stub_neotoma.entity_fields("connector_status", "eval-stub")
    assert fields["status"] == "failing"
    assert fields["last_success_at"] == first["last_success_at"]
    assert fields["last_attempt_at"] != fields["last_success_at"]
    assert fields["consecutive_failures"] == 1
    status = store.read_status("eval-stub")
    assert status is not None
    last_success = datetime.fromisoformat(status.last_success_at)
    assert status.freshness(now=last_success + timedelta(seconds=60)).state == "fresh"


def test_status_write_fails_when_readback_drops_a_field(stub_neotoma):
    store = ConnectorStore(base_url=stub_neotoma.url, token="token")
    stub_neotoma.drop_query_fields.add("status")

    with pytest.raises(NeotomaUnavailable, match="read-back verification failed"):
        store.write_status(
            ConnectorStatus(
                connector_name="eval-stub",
                status="ok",
                last_attempt_at="2026-09-01T12:00:00+00:00",
                last_success_at="2026-09-01T12:00:00+00:00",
                poll_interval_seconds=300,
                stale_after_seconds=900,
            )
        )


def test_alarm_policy_suppresses_stale_data_and_reports_threshold(
    monkeypatch, stub_neotoma
):
    assert Freshness(state="stale").alarms_allowed is False
    assert Freshness(state="unknown").alarms_allowed is False

    store = ConnectorStore(base_url=stub_neotoma.url, token="token")
    stub_neotoma.store(
        {
            "entity_type": "connector_status",
            "connector_name": "below-threshold",
            "consecutive_failures": 2,
            "last_success_at": "2026-09-01T11:00:00+00:00",
        }
    )
    monkeypatch.setenv("NEOTOMA_BASE_URL", stub_neotoma.url)
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "token")

    daemon.alert_on_failures(
        store, {"below-threshold": ConnectorResult.failure("timeout")}
    )
    assert stub_neotoma.request_count("/store", "daemon_report") == 0

    stub_neotoma.store(
        {
            "entity_type": "connector_status",
            "connector_name": "at-threshold",
            "consecutive_failures": 3,
            "last_success_at": "2026-09-01T10:00:00+00:00",
        }
    )
    daemon.alert_on_failures(
        store, {"at-threshold": ConnectorResult.failure("HTTP 502")}
    )

    reports = stub_neotoma.entities_of_type("daemon_report")
    assert len(reports) == 1
    report = reports[0]["fields"]
    assert report["severity"] == "error"
    assert "connector 'at-threshold' has failed 3 consecutive runs" in report["message"]
    assert "going stale" in report["message"]
    assert "behind" not in report["message"]
