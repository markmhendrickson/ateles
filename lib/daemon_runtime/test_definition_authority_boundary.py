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
