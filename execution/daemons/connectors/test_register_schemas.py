"""CLI tests for register_schemas.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from lib.connectors.schema_registration import RegistrationSummary, SchemaVerdict

_REPO = Path(__file__).resolve().parents[3]
_CLI = _REPO / "execution" / "daemons" / "connectors" / "register_schemas.py"


def _load_cli():
    name = f"register_schemas_cli_{id(object())}"
    spec = importlib.util.spec_from_file_location(name, _CLI)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    if str(_REPO) not in sys.path:
        sys.path.insert(0, str(_REPO))
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_cli_json_exit_1_when_token_missing(monkeypatch, capsys):
    cli = _load_cli()

    class Store:
        configured = False

    monkeypatch.setattr(cli, "ConnectorStore", lambda: Store())
    code = cli.main(["--json"])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "preflight" in payload["error"]


def test_cli_json_exit_0_when_summary_ok(monkeypatch, capsys):
    cli = _load_cli()

    class Store:
        configured = True

    summary = RegistrationSummary(
        ok=True,
        verdicts=[
            SchemaVerdict(
                entity_type="connector_status",
                action="already_registered",
                verified=True,
                identity="['connector_name']",
                mutable="mutable",
                reducer="last_write",
                read_back_at="2026-09-02T00:00:00+00:00",
                schema_version="2.0",
            ),
            SchemaVerdict(
                entity_type="deployment_observation",
                action="registered",
                verified=True,
                identity="composite",
                mutable="immutable (append-only)",
                reducer="empty",
                read_back_at="2026-09-02T00:00:00+00:00",
                schema_version="1.0",
            ),
        ],
        empty_records_note="schema registered, no connector records observed yet",
    )
    monkeypatch.setattr(cli, "ConnectorStore", lambda: Store())
    monkeypatch.setattr(cli, "register_connector_schemas", lambda store: summary)
    code = cli.main(["--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert len(payload["schemas"]) == 2
