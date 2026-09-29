import json
import subprocess
import sys
from pathlib import Path

import register_generation_record_schema as reg

SCRIPT = Path(reg.__file__)


def test_default_is_a_dry_run_that_prints_the_payload_and_needs_no_credentials(monkeypatch):
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
    proc = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                          env={"PATH": "/usr/bin:/bin"})
    assert proc.returncode == 0
    assert "DRY RUN" in proc.stdout
    body = proc.stdout.split("POST /register_schema:", 1)[1].split("\n\nRe-run", 1)[0]
    payload = json.loads(body)
    assert payload["entity_type"] == "generation_record" and payload["activate"] is True


def test_dry_run_makes_no_request(monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("dry run must not touch the network")

    monkeypatch.setattr(reg, "_default_requester", boom)
    assert reg.main([]) == 0


def test_apply_without_a_token_refuses(monkeypatch):
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
    try:
        reg.main(["--apply"])
    except SystemExit as exc:
        assert "NEOTOMA_BEARER_TOKEN" in str(exc)
    else:
        raise AssertionError("expected refusal")


def test_payload_fields_come_from_the_single_record_source():
    from lib.capabilities import records

    fields = reg.build_payload()["schema_definition"]["fields"]
    assert set(fields) == set(records.GENERATION_RECORD_FIELDS)
    assert reg.build_payload()["schema_definition"]["canonical_name_fields"] == ["generation_id"]


def test_apply_reads_back_and_fails_on_a_missing_field():
    from lib.capabilities import records

    calls = []
    full = {"active": True, "schema_version": "1.0", "schema_definition": {"fields": dict.fromkeys(records.GENERATION_RECORD_FIELDS, {})}}

    def ok(method, path, body):
        calls.append((method, path))
        return full if method == "GET" else {}

    assert reg.apply(ok) == 0 and calls == [("POST", "/register_schema"), ("GET", "/schemas/generation_record")]

    partial = {"active": True, "schema_definition": {"fields": {"slot": {}}}}
    assert reg.apply(lambda m, p, b: partial if m == "GET" else {}) == 1
    assert reg.apply(lambda m, p, b: {**full, "active": False} if m == "GET" else {}) == 1
