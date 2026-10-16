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

    monkeypatch.setattr(reg.neotoma_http, "request_json", boom)
    assert reg.main([]) == 0


def test_apply_without_a_token_or_host_refuses_with_a_remedy(monkeypatch, capsys):
    monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
    assert reg.main(["--apply"]) == 2
    err = capsys.readouterr().err
    assert "NEOTOMA_BASE_URL" in err and "source them without printing" in err


def test_apply_prints_the_target_host_and_never_the_token(monkeypatch, capsys):
    token = "tok" + "-abcdef-123456"
    monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.net")
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", token)
    monkeypatch.setattr(reg, "apply", lambda: 0)
    assert reg.main(["--apply"]) == 0
    out = capsys.readouterr()
    assert "https://neotoma.example.net" in out.out and token not in out.out + out.err


def test_http_failure_shows_the_server_error_body_and_says_rerun_is_safe(capsys):
    def reject(method, path, body):
        raise reg.neotoma_http.NeotomaRequestError(400, '{"error":"SCHEMA_VALIDATION_FAILED: bad field"}')

    assert reg.apply(reject) == 1
    err = capsys.readouterr().err
    assert "SCHEMA_VALIDATION_FAILED: bad field" in err and "re-running is safe" in err


def test_help_lists_the_environment_variables():
    proc = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30)
    assert "NEOTOMA_BASE_URL" in proc.stdout and "NEOTOMA_BEARER_TOKEN" in proc.stdout
    assert "Safe to re-run" in proc.stdout


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

    wrong_type = {**full, "schema_definition": {"fields": {**full["schema_definition"]["fields"], "cost_usd": {"type": "string"}}}}
    assert reg.apply(lambda m, p, b: wrong_type if m == "GET" else {}) == 1
    partial = {"active": True, "schema_definition": {"fields": {"slot": {}}}}
    assert reg.apply(lambda m, p, b: partial if m == "GET" else {}) == 1
    assert reg.apply(lambda m, p, b: {**full, "active": False} if m == "GET" else {}) == 1
