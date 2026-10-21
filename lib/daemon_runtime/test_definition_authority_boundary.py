"""Real helper/loader controls for completed responses and undeclared authority."""
from __future__ import annotations

import shutil

import httpx
import pytest

from lib.daemon_runtime import agent_loader as loader
from lib.daemon_runtime import neotoma_signed as signed


def _canonical(**overrides):
    return {"name": "pavo", "status": "active", "prompt_markdown": "A defined role",
            "tool_allowlist": ["Read"], **overrides}


@pytest.fixture
def real_helper(monkeypatch, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("the real signed helper requires Node")
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "pavo.jwk.json").write_text('{"kid":"synthetic"}')
    rc = tmp_path / "signer"
    (rc / "dist/cli").mkdir(parents=True)
    (rc / "package.json").write_text('{"type":"module"}')
    monkeypatch.setattr(signed, "AAUTH_KEYS_DIR", str(keys))
    monkeypatch.setattr(signed, "NEOTOMA_RC_DIR", str(rc))
    monkeypatch.setattr(signed, "NODE_BIN", node)
    monkeypatch.setenv("NEOTOMA_AAUTH_VIA_CLI", "1")
    monkeypatch.setenv("PAVO_AGENT_DEFINITION_ID", "ent_fixture")
    monkeypatch.setattr(loader, "_sleep", lambda seconds: None)
    return rc / "dist/cli/aauth_signer.js"


@pytest.mark.parametrize("status", [200, 401, 503])
@pytest.mark.parametrize("body_read_fails", [False, True])
def test_real_helper_completed_response_never_uses_bearer(
    real_helper, monkeypatch, status, body_read_fails,
):
    # Exercise signed_fetch.mjs itself with a fake HTTP Response from its
    # signer import. No tuple mock and no external HTTP/credential is involved.
    read = "throw new Error('synthetic body read failure')" if body_read_fails else "return '<not-json>'"
    real_helper.write_text(
        "export async function cliSignedFetch() { return {"
        f"status:{status}, ok:{str(status == 200).lower()}, async text() {{ {read}; }}"
        "}; }\n"
    )
    bearer = []

    def unsigned(*args, **kwargs):
        bearer.append(True)
        return httpx.Response(200, json={"snapshot": _canonical()},
                              request=httpx.Request("GET", "https://fixture.invalid"))

    monkeypatch.setattr(loader.httpx, "get", unsigned)
    with pytest.raises(loader.AgentDefinitionUnavailable):
        loader.AgentLoader("pavo").load()
    assert not bearer, "an observed signed HTTP response must remain final"


@pytest.mark.parametrize("overrides", [
    {"prompt_markdown": None}, {"prompt_markdown": ""}, {"prompt_markdown": "  "},
    {"tool_allowlist": None}, {"tool_allowlist": []}, {"tool_allowlist": ""},
    {"tool_allowlist": "[]"}, {"tool_allowlist": "[invalid"}, {"status": None},
    {"status": ""}, {"status": "unrecognized"},
])
def test_found_definition_missing_authority_is_refused(monkeypatch, overrides):
    monkeypatch.setattr(signed, "via_cli_enabled", lambda: False)
    monkeypatch.setattr(loader.httpx, "post", lambda *a, **k: httpx.Response(
        200, json={"entities": [{"entity_id": "ent_fixture", "snapshot": _canonical(**overrides)}]},
        request=httpx.Request("POST", "https://fixture.invalid")))
    with pytest.raises(loader.AgentDefinitionUnavailable):
        loader.AgentLoader("pavo").load(allow_undefined=True)


def test_unregistered_daemon_refuses_start(monkeypatch):
    monkeypatch.setattr(signed, "via_cli_enabled", lambda: False)
    monkeypatch.setattr(loader.httpx, "post", lambda *a, **k: httpx.Response(
        200, json={"entities": []}, request=httpx.Request("POST", "https://fixture.invalid")))
    with pytest.raises(SystemExit) as refused:
        loader.load_agent_definition_or_exit("unregistered")
    assert refused.value.code != 0


@pytest.mark.parametrize("raw", [None, "", [], "[]", "[invalid", [None]])
def test_unknown_tool_set_never_becomes_a_wildcard(raw):
    assert loader.AgentDefinition(tool_allowlist=raw).tools == []


def test_explicit_registered_capabilities_remain_available(monkeypatch):
    monkeypatch.setattr(signed, "via_cli_enabled", lambda: False)
    monkeypatch.setattr(loader.httpx, "post", lambda *a, **k: httpx.Response(
        200, json={"entities": [{"entity_id": "ent_fixture", "snapshot": _canonical()}]},
        request=httpx.Request("POST", "https://fixture.invalid")))
    definition = loader.AgentLoader("pavo").load()
    assert definition.prompt_markdown == "A defined role"
    assert definition.tools == ["Read"]


def test_real_helper_pre_response_failure_preserves_bearer_fallback(real_helper, monkeypatch):
    real_helper.write_text("export async function cliSignedFetch() { throw new Error('fetch failed'); }\n")
    bearer = []
    def unsigned(*args, **kwargs):
        bearer.append(True)
        return httpx.Response(200, json={"snapshot": _canonical()},
                              request=httpx.Request("GET", "https://fixture.invalid"))
    monkeypatch.setattr(loader.httpx, "get", unsigned)
    definition = loader.AgentLoader("pavo").load()
    assert definition.tools == ["Read"]
    assert len(bearer) == 1


@pytest.mark.parametrize("status", [200, 403])
def test_real_helper_body_timeout_preserves_response_boundary(real_helper, status):
    real_helper.write_text(
        "export async function cliSignedFetch() { return {"
        f"status:{status}, ok:false, async text() {{ "
        "return new Promise(resolve => setTimeout(() => resolve('{}'), 10000)); } }; }\n"
    )
    with pytest.raises(signed.SignedRequestError) as raised:
        signed.signed_request("GET", "https://fixture.invalid", agent_name="pavo", timeout=1)
    assert raised.value.response_received is True
    assert raised.value.http_status == status


def test_planted_response_boundary_regression_is_detected(real_helper, monkeypatch, tmp_path):
    # Restore the former helper's behavior after headers: only an untyped error
    # survives a stream failure. The real-helper control above must go red.
    original = signed._HELPER.read_text()
    marker = '  process.stdout.write(JSON.stringify({ response_received: true, status: resp.status }) + "\\n");\n'
    mutant = original.replace(marker, "").replace(
        "{ status: resp.status, ok: resp.ok, body_error: true }",
        "{ error: 'synthetic stream failure' }",
    )
    assert mutant != original and "response_received: true" not in mutant
    helper = tmp_path / "old-helper.mjs"
    helper.write_text(mutant)
    monkeypatch.setattr(signed, "_HELPER", helper)
    with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
        test_real_helper_completed_response_never_uses_bearer(
            real_helper, monkeypatch, 401, True,
        )


@pytest.mark.parametrize("status", [200, 403])
def test_real_helper_body_timeout_never_retries_as_bearer(real_helper, monkeypatch, status):
    real_helper.write_text(
        "export async function cliSignedFetch() { return {"
        f"status:{status}, ok:false, async text() {{ "
        "return new Promise(resolve => setTimeout(() => resolve('{}'), 10000)); } }; }\n"
    )
    real_signed_request = signed.signed_request
    def short_request(*args, **kwargs):
        kwargs["timeout"] = min(kwargs.get("timeout", 1), 1)
        return real_signed_request(*args, **kwargs)
    monkeypatch.setattr(signed, "signed_request", short_request)
    bearer = []
    def unsigned(*args, **kwargs):
        bearer.append(True)
        return httpx.Response(200, json={"snapshot": _canonical()},
                              request=httpx.Request("GET", "https://fixture.invalid"))
    monkeypatch.setattr(loader.httpx, "get", unsigned)
    with pytest.raises(loader.AgentDefinitionUnavailable):
        loader.AgentLoader("pavo").load()
    assert not bearer


def test_planted_late_status_record_is_detected(real_helper, monkeypatch, tmp_path):
    original = signed._HELPER.read_text()
    marker = '  process.stdout.write(JSON.stringify({ response_received: true, status: resp.status }) + "\\n");\n'
    mutant = original.replace(marker, "")
    assert mutant != original
    helper = tmp_path / "late-status-helper.mjs"
    helper.write_text(mutant)
    monkeypatch.setattr(signed, "_HELPER", helper)
    # The final signed-request helper reports a bounded error to its caller;
    # without the early record, the response-boundary assertion must fail.
    with pytest.raises(AssertionError):
        test_real_helper_body_timeout_preserves_response_boundary(real_helper, 200)
