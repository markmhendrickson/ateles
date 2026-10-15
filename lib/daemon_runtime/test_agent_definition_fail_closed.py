"""An agent definition that cannot be loaded now fails closed.

The loader retries a transient failure briefly and then refuses with a controlled
error that carries only a fixed reason code. It never returns a stand-in for a
failed load. A caller that runs a role with no definition on purpose must opt in,
and the opt-in covers only a definite "Neotoma has none".

Run: pytest lib/daemon_runtime/test_agent_definition_fail_closed.py -v
"""

from __future__ import annotations

import logging

import httpx
import pytest

import agent_loader as al
from agent_loader import AgentLoader

# Resolved by name so that, run against code without the change, each test FAILS
# on its assertion rather than the whole file failing to import.
AgentDefinitionUnavailable = getattr(al, "AgentDefinitionUnavailable", RuntimeError)

REAL = {
    "entities": [
        {
            "entity_id": "ent_real",
            "snapshot": {
                "name": "pavo",
                "prompt_markdown": "# the real role prompt",
                "tool_allowlist": ["Read", "Bash(gh pr:*)"],
                "status": "active",
            },
        }
    ]
}


class _Resp:
    def __init__(self, payload=None, status=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "http error", request=httpx.Request("POST", "http://x"), response=self
            )

    def json(self):
        return self._payload


@pytest.fixture
def neotoma(monkeypatch):
    """A scripted Neotoma: each call pops one outcome (the last repeats)."""
    state = {"script": [], "calls": 0, "sleeps": []}
    monkeypatch.setattr(al.ns, "via_cli_enabled", lambda: False)
    monkeypatch.setattr(al, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(al, "_sleep", lambda s: state["sleeps"].append(s), raising=False)

    def post(url, **kwargs):
        state["calls"] += 1
        outcome = state["script"].pop(0) if len(state["script"]) > 1 else state["script"][0]
        if outcome == "timeout":
            raise httpx.ReadTimeout("timed out")
        if outcome == "connect":
            raise httpx.ConnectError("refused")
        if outcome == "real":
            return _Resp(REAL)
        if outcome == "none":
            return _Resp({"entities": []})
        if outcome == "garbage":
            resp = _Resp()
            resp.json = lambda: (_ for _ in ()).throw(ValueError("not json"))
            return resp
        return _Resp(status=int(outcome))

    monkeypatch.setattr(al.httpx, "post", post)
    return state


# ── the observed failure ─────────────────────────────────────────────────────


def test_a_timeout_then_success_loads_the_real_definition(neotoma):
    neotoma["script"] = ["timeout", "real"]

    d = AgentLoader("pavo").load()

    assert d.is_stub is False
    assert d.prompt_markdown == "# the real role prompt"
    assert d.tools == ["Read", "Bash(gh pr:*)"]
    assert neotoma["calls"] == 2
    assert neotoma["sleeps"] == [0.5]


def test_a_persistent_timeout_is_refused_with_no_stand_in(neotoma):
    neotoma["script"] = ["timeout"]

    with pytest.raises(AgentDefinitionUnavailable) as raised:
        AgentLoader("pavo").load()

    assert raised.value.code == "timeout"
    assert raised.value.attempts == 3
    assert raised.value.transient is True
    assert neotoma["calls"] == 3
    assert neotoma["sleeps"] == [0.5, 1.5]


def test_the_refusal_carries_only_a_name_a_code_and_a_count(neotoma, caplog):
    neotoma["script"] = ["timeout"]

    with caplog.at_level(logging.INFO), pytest.raises(AgentDefinitionUnavailable) as raised:
        AgentLoader("pavo").load()

    texts = [str(raised.value)] + [r.getMessage() for r in caplog.records]
    for text in texts:
        assert "timed out" not in text and "http" not in text.lower()
    assert "pavo" in str(raised.value) and "timeout" in str(raised.value)


@pytest.mark.parametrize("status", [500, 502, 503, 504, 429])
def test_a_server_side_failure_is_retried(neotoma, status):
    neotoma["script"] = [str(status), "real"]
    assert AgentLoader("pavo").load().prompt_markdown
    assert neotoma["calls"] == 2


def test_a_connection_failure_is_retried(neotoma):
    neotoma["script"] = ["connect", "connect", "real"]
    assert AgentLoader("pavo").load().prompt_markdown
    assert neotoma["sleeps"] == [0.5, 1.5]


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_another_4xx_is_final_at_once(neotoma, status):
    neotoma["script"] = [str(status), "real"]
    with pytest.raises(AgentDefinitionUnavailable) as raised:
        AgentLoader("pavo").load()
    assert raised.value.code == "rejected"
    assert neotoma["calls"] == 1 and neotoma["sleeps"] == []


def test_a_response_that_is_not_json_is_final_at_once(neotoma):
    neotoma["script"] = ["garbage", "real"]
    with pytest.raises(AgentDefinitionUnavailable) as raised:
        AgentLoader("pavo").load()
    assert raised.value.code == "bad_response" and neotoma["calls"] == 1


def test_the_wall_clock_cap_ends_the_retries(neotoma, monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(al, "_monotonic", lambda: clock["now"], raising=False)
    monkeypatch.setattr(al, "_sleep", lambda s: clock.__setitem__("now", clock["now"] + s), raising=False)
    monkeypatch.setattr(al, "LOAD_TOTAL_CAP_S", 1.0, raising=False)
    neotoma["script"] = ["timeout"]

    with pytest.raises(AgentDefinitionUnavailable) as raised:
        AgentLoader("pavo").load()

    # The first 0.5 s wait fits inside 1.0 s; the next 1.5 s wait would not.
    assert raised.value.attempts == 2


# ── "there is no such definition" is a different fact ─────────────────────────


def test_no_such_definition_is_refused_by_default(neotoma):
    neotoma["script"] = ["none"]
    with pytest.raises(AgentDefinitionUnavailable) as raised:
        AgentLoader("ghost").load()
    assert raised.value.code == "not_found"
    assert raised.value.transient is False
    assert neotoma["calls"] == 1, "a definite answer is not retried"


def test_the_opt_in_returns_the_undefined_definition_only_for_not_found(neotoma):
    neotoma["script"] = ["none"]
    d = AgentLoader("ghost").load(allow_undefined=True)
    assert d.is_stub is True and d.load_error == "not_found"
    assert d.status == al.UNDEFINED_STATUS


@pytest.mark.parametrize("failure", ["timeout", "connect", "503", "403", "garbage"])
def test_the_opt_in_never_turns_a_failed_load_into_a_stand_in(neotoma, failure):
    neotoma["script"] = [failure]
    with pytest.raises(AgentDefinitionUnavailable):
        AgentLoader("pavo").load(allow_undefined=True)


def test_the_explicit_id_path_fails_closed_too(neotoma, monkeypatch):
    monkeypatch.setenv("PAVO_AGENT_DEFINITION_ID", "ent_x")
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(al.httpx, "get", get)
    with pytest.raises(AgentDefinitionUnavailable) as raised:
        AgentLoader("pavo").load(allow_undefined=True)
    assert raised.value.code == "timeout" and len(calls) == 3


def test_a_404_on_the_explicit_id_is_not_found(neotoma, monkeypatch):
    monkeypatch.setenv("PAVO_AGENT_DEFINITION_ID", "ent_x")
    monkeypatch.setattr(al.httpx, "get", lambda url, **kw: _Resp(status=404))
    with pytest.raises(AgentDefinitionUnavailable) as raised:
        AgentLoader("pavo").load()
    assert raised.value.code == "not_found"


# ── a daemon's own startup ───────────────────────────────────────────────────


def test_a_daemon_that_cannot_read_its_definition_exits_non_zero(neotoma):
    """The status check is what stops a retired agent; it must not be skipped."""
    neotoma["script"] = ["timeout"]
    with pytest.raises(SystemExit) as raised:
        al.load_agent_definition_or_exit("pavo")
    assert raised.value.code == 1


def test_a_daemon_survives_one_timeout_at_startup(neotoma):
    neotoma["script"] = ["timeout", "real"]
    assert al.load_agent_definition_or_exit("pavo").entity_id == "ent_real"


def test_an_unregistered_daemon_still_starts_with_a_warning(neotoma):
    neotoma["script"] = ["none"]
    d = al.load_agent_definition_or_exit("ghost")
    assert d.is_stub is True


# ── planted red ──────────────────────────────────────────────────────────────


def test_planted_red_reinstating_the_stand_in_is_caught(neotoma, monkeypatch):
    """If a failed load were turned back into a stand-in, the tests above would not
    pass: this shows the reinstated behaviour returns an empty prompt and wildcard."""
    real_load = AgentLoader.load
    if not hasattr(AgentLoader, "_undefined_definition"):
        pytest.skip("nothing to reinstate")

    def reinstated(self, *, allow_undefined=False):
        try:
            return real_load(self, allow_undefined=allow_undefined)
        except AgentDefinitionUnavailable as exc:
            return self._undefined_definition(getattr(exc, "code", "unexpected"))

    monkeypatch.setattr(AgentLoader, "load", reinstated)
    neotoma["script"] = ["timeout"]

    d = AgentLoader("pavo").load()

    assert d.prompt_markdown == "" and d.tools == ["*"], "the reinstated stand-in is exactly the defect"
