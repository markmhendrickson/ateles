"""Real helper/loader controls for completed responses and undeclared authority."""
from __future__ import annotations

import shutil
import subprocess
import time
from io import StringIO
from pathlib import Path

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


@pytest.mark.parametrize("selector", ["default", "--dangerously-skip-permissions"])
def test_daemon_startup_refuses_invalid_capability(monkeypatch, caplog, selector):
    definition = loader.AgentDefinition(
        entity_id="ent_fixture", name="anthus", status="active",
        prompt_markdown="A defined role", tool_allowlist=[selector],
    )
    monkeypatch.setattr(loader.AgentLoader, "load", lambda self: definition)
    with pytest.raises(SystemExit) as refused:
        loader.load_agent_definition_or_exit("anthus")
    assert refused.value.code == 1
    assert selector not in caplog.text


def test_anthus_entry_refuses_invalid_capability_before_signer(monkeypatch):
    import asyncio
    anthus_dir = Path(__file__).resolve().parents[2] / "execution/daemons/anthus"
    monkeypatch.syspath_prepend(str(anthus_dir))
    import anthus

    definition = loader.AgentDefinition(
        entity_id="ent_fixture", name="anthus", status="active",
        prompt_markdown="A defined role", tool_allowlist=["default"],
    )
    monkeypatch.setattr(loader.AgentLoader, "load", lambda self: definition)
    signer_calls = []
    monkeypatch.setattr(anthus.AAuthSigner, "from_key_file",
                        lambda *args: signer_calls.append(args))
    with pytest.raises(SystemExit) as refused:
        asyncio.run(anthus.main())
    assert refused.value.code == 1
    assert not signer_calls


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
    assert raised.value.response_received is None
    assert raised.value.http_status is None


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


@pytest.mark.parametrize("unread_response", [
    '{"response_received":true,"status":200}\n{"status":200,"body":"{}"}',
    "",
])
def test_deadline_timeout_never_retries_as_bearer(
    monkeypatch, unread_response,
):
    class Sender:
        def __init__(self):
            self.messages = []

        def send(self, message):
            self.messages.append(message)

        def poll(self, timeout):
            return True

        def recv(self):
            return ("run", time.monotonic() + 5)

        def close(self):
            pass

    class Process:
        def __init__(self):
            self.communicate_calls = 0
            self.stdout = StringIO(unread_response)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def communicate(self, **kwargs):
            # A complete result can be waiting in the pipe while the timeout
            # exception reports no output consumed by communicate().
            self.communicate_calls += 1
            raise subprocess.TimeoutExpired("helper", 1, output="")

        def kill(self):
            pass

    sender = Sender()
    process = Process()
    monkeypatch.setattr(signed.os, "setsid", lambda: None)
    signed._prepare_signed_request(
        sender, lambda *args, **kwargs: {"key": "fixture", "sub": "pavo", "kid": "kid"},
        "pavo", None, "", "GET", "https://fixture.invalid", None,
        time.monotonic() + 5, "node", "helper", "rc", {},
        lambda *args, **kwargs: process,
    )
    assert process.communicate_calls == 1
    assert process.stdout.read() == unread_response
    errors = [message for message in sender.messages if message[0] == "error"]
    assert errors == [("error", "helper_timeout", None, None)]
    error = signed.SignedRequestError("timed out", errors[0][1],
                                      response_received=errors[0][2])
    monkeypatch.setattr(signed, "via_cli_enabled", lambda: True)
    def timed_out(*args, **kwargs):
        raise error

    monkeypatch.setattr(signed, "signed_request", timed_out)
    bearer = []
    monkeypatch.setattr(loader.httpx, "get", lambda *args, **kwargs: bearer.append(True))
    with pytest.raises(loader.AgentDefinitionUnavailable):
        loader.AgentLoader("pavo")._neotoma("GET", "https://fixture.invalid")
    assert not bearer
