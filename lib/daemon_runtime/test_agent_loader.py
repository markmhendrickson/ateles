"""Tests for AgentDefinition.tools parsing.

Regression coverage for the tool_allowlist shape mismatch: agent_definition
entities store tool_allowlist as a JSON array, but the loader historically only
handled a comma-separated string (.split(",")), which mangled array values.
The .tools property must accept array, comma-string, and wildcard shapes.
"""

import httpx

import agent_loader as al
from agent_loader import AgentDefinition


def _tools(value):
    return AgentDefinition(name="t", tool_allowlist=value).tools


def test_array_shape_canonical_storage():
    assert _tools(["a", "b", "c"]) == ["a", "b", "c"]


def test_array_with_whitespace_and_blanks():
    assert _tools([" a ", "", "  ", "b"]) == ["a", "b"]


def test_comma_string_legacy_shape():
    assert _tools("a, b ,c") == ["a", "b", "c"]


def test_json_array_string_shape():
    """Neotoma returns tool_allowlist as a JSON-array STRING, not a parsed list.

    A naive comma-split keeps the surrounding brackets/quotes on each token
    ('["a"', '"b"', '"c"]'), which the CLI rejects as malformed --allowedTools
    rules and fails the whole dispatch. This was a live swarm outage: the
    Bash(...:*) grammar makes the rejection fatal rather than silently ignored.
    """
    assert _tools('["a", "b", "c"]') == ["a", "b", "c"]


def test_json_array_string_preserves_parenthesized_bash_grants():
    """The exact production shape: parenthesized Bash command-scope grants must
    survive JSON parsing intact, not arrive wrapped in literal quotes."""
    raw = '["Bash", "Bash(gh pr:*)", "Bash(gh issue:*)", "Bash(git:*)", "Read"]'
    assert _tools(raw) == [
        "Bash",
        "Bash(gh pr:*)",
        "Bash(gh issue:*)",
        "Bash(git:*)",
        "Read",
    ]


def test_json_array_string_with_blanks():
    assert _tools('["a", "", "  ", "b"]') == ["a", "b"]


def test_bracketed_non_json_falls_back_to_comma_split():
    """A bracketed string that isn't valid JSON must not crash; it falls back
    to the legacy comma-split rather than raising."""
    assert _tools("[a, b, c]") == ["[a", "b", "c]"]


def test_wildcard_string():
    assert _tools("*") == ["*"]
    assert _tools("  *  ") == ["*"]


def test_empty_and_none_default_to_wildcard():
    assert _tools("") == ["*"]
    assert _tools(None) == ["*"]
    assert _tools([]) == ["*"]


def test_default_is_wildcard():
    assert AgentDefinition(name="t").tools == ["*"]


# ─────────────────────────────────────────────────────────────────────────────
# Neotoma REST endpoint + failure-visibility regression coverage (ateles#606).
#
# Two defects, both of which presented as a HEALTHY daemon:
#
#   1. load_active_policies() POSTed to /retrieve_entities — an MCP TOOL name,
#      not a REST route. Verified live against prod 2026-08-31: that path
#      returns 404, /entities/query returns 200. The 404 was caught and logged
#      at WARNING, so every agent dispatched with NO learned policies while the
#      loader reported nothing wrong.
#
#   2. A failed agent_definition load returned a stub with an EMPTY
#      prompt_markdown and a WILDCARD tool_allowlist, indistinguishable from a
#      successful load. An agent could run with no role instructions and
#      unrestricted tools while the daemon reported success.
#
# Both of these are "the suite is green and the feature never worked" shaped
# (cf. ateles#602), so these tests assert the URL actually requested and the
# observable difference between success and failure — not just a return value.
# ─────────────────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "http://x"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return self._payload


def _no_signing(monkeypatch):
    """Force the plain-httpx path so we observe the real URL."""
    monkeypatch.setattr(al.ns, "via_cli_enabled", lambda: False)


def test_load_active_policies_posts_to_entities_query(monkeypatch):
    """The policy read must hit /entities/query, never /retrieve_entities.

    FAILS on origin/main: the URL is ".../retrieve_entities", which 404s live.
    """
    _no_signing(monkeypatch)
    monkeypatch.setattr(al, "NEOTOMA_BEARER_TOKEN", "tok")
    seen = {}

    def fake_post(url, **kwargs):
        seen["url"] = url
        return _Resp({"entities": []})

    monkeypatch.setattr(al.httpx, "post", fake_post)
    al.AgentLoader("apis").load_active_policies()

    assert seen["url"].endswith("/entities/query"), seen["url"]
    assert "retrieve_entities" not in seen["url"], (
        "retrieve_entities is an MCP tool name, not a REST path — it 404s"
    )


def test_load_active_policies_returns_matching_agent_policy(monkeypatch):
    """A successful query yields the agent's own active/provisional policies."""
    _no_signing(monkeypatch)
    monkeypatch.setattr(al, "NEOTOMA_BEARER_TOKEN", "tok")
    payload = {
        "entities": [
            {"snapshot": {"agent_sub": "apis@ateles-swarm", "status": "active",
                          "rule": "mine"}},
            {"snapshot": {"agent_sub": "other@ateles-swarm", "status": "active",
                          "rule": "theirs"}},
            {"snapshot": {"agent_sub": "apis@ateles-swarm", "status": "retired",
                          "rule": "old"}},
        ]
    }
    monkeypatch.setattr(al.httpx, "post", lambda url, **kw: _Resp(payload))

    out = al.AgentLoader("apis").load_active_policies()
    assert [p["rule"] for p in out] == ["mine"]


def test_policy_404_does_not_present_as_no_policies(monkeypatch, caplog):
    """A 404 must be logged at ERROR, not silently look like 'no policies'.

    FAILS on origin/main: the failure is logged at WARNING, so a dead endpoint
    is indistinguishable from an agent that genuinely has no policies.
    """
    _no_signing(monkeypatch)
    monkeypatch.setattr(al, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(al.httpx, "post", lambda url, **kw: _Resp({}, status=404))

    with caplog.at_level("ERROR"):
        out = al.AgentLoader("apis").load_active_policies()

    assert out == []
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors, "a failed policy load must be logged at ERROR, not WARNING"


def test_successful_load_is_not_a_stub(monkeypatch):
    """A real definition carries its prompt and is not flagged as a stub."""
    _no_signing(monkeypatch)
    monkeypatch.setattr(al, "NEOTOMA_BEARER_TOKEN", "tok")
    payload = {
        "entities": [
            {
                "entity_id": "ent_real",
                "snapshot": {"name": "apis", "prompt_markdown": "# real prompt",
                             "tool_allowlist": ["Bash"]},
            }
        ]
    }
    monkeypatch.setattr(al.httpx, "post", lambda url, **kw: _Resp(payload))

    d = al.AgentLoader("apis").load()
    assert d.is_stub is False
    assert d.load_error == ""
    assert d.prompt_markdown == "# real prompt"


def test_failed_load_is_marked_as_a_stub_not_a_success(monkeypatch):
    """A failed load must be DISTINGUISHABLE from a successful one.

    FAILS on origin/main: AgentDefinition has no is_stub/load_error field, so a
    stub with an empty prompt is indistinguishable from a real definition. A
    caller cannot tell it dispatched an agent with no prompt.

    The tools assertion was `== ["*"]` until ateles#669, describing the wildcard
    the stub used to grant. That grant was the defect, not a property worth
    pinning: it let a network timeout hand an agent unrestricted tools. The stub
    is now read-only, so the assertion checks that instead.
    """
    _no_signing(monkeypatch)
    monkeypatch.setattr(al, "NEOTOMA_BEARER_TOKEN", "tok")

    def boom(url, **kwargs):
        raise httpx.ConnectError("neotoma unreachable")

    monkeypatch.setattr(al.httpx, "post", boom)

    d = al.AgentLoader("apis").load()
    # The stub still runs blind (no prompt)...
    assert d.prompt_markdown == ""
    # ...but must not be handed unrestricted tools for it.
    assert d.tools != ["*"]
    # ...and must be flagged, or a caller reports success while running blind.
    assert d.is_stub is True
    assert d.load_error, "a stub must record WHY the load failed"


def test_failed_load_is_logged_at_error(monkeypatch, caplog):
    """Falling back to an empty prompt is an ERROR, not a WARNING.

    FAILS on origin/main: the fallback is logged at WARNING.
    """
    _no_signing(monkeypatch)
    monkeypatch.setattr(al, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(
        al.httpx, "post",
        lambda url, **kw: (_ for _ in ()).throw(httpx.ConnectError("down")),
    )

    with caplog.at_level("ERROR"):
        al.AgentLoader("apis").load()

    msgs = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("FAILED" in m for m in msgs), msgs


def test_no_matching_definition_is_also_a_stub(monkeypatch):
    """A 200 with no matching name is still a failed load, not a definition."""
    _no_signing(monkeypatch)
    monkeypatch.setattr(al, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(
        al.httpx, "post",
        lambda url, **kw: _Resp({"entities": [{"entity_id": "e",
                                               "snapshot": {"name": "someone-else"}}]}),
    )

    d = al.AgentLoader("apis").load()
    assert d.is_stub is True
    assert "no agent_definition" in d.load_error


# ── Stub least-privilege + timeout (ateles#669) ────────────────────────────────
#
# The 10s httpx timeout expired against a production Neotoma answering in
# 20-32s, so nearly every load fell back to _stub(). That stub granted
# tool_allowlist="*", which made a NETWORK CONDITION widen an agent's authority:
# `tools` returned ["*"], skill_runner then skipped --allowed-tools entirely,
# and the child ran unrestricted.

import agent_loader
import httpx
import pytest


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("ATELES_NEOTOMA_TIMEOUT", raising=False)
    monkeypatch.setattr(agent_loader, "_escalate", lambda *a, **k: None)


def _stub_for(name="pavo", reason="ReadTimeout"):
    return agent_loader.AgentLoader(name)._stub(reason)


def test_stub_never_grants_wildcard_tools():
    """The regression this whole change exists for.

    A failed read must not produce a wider grant than a successful one. If this
    assertion ever fails, a Neotoma timeout is again spawning unrestricted
    agents.
    """
    assert _stub_for().tools != ["*"]
    assert "*" not in _stub_for().tools


def test_stub_tools_are_read_only():
    """No tool in the stub allowlist may write, push, send, or delete."""
    tools = _stub_for().tools
    assert tools, "a stub must still grant something, or dispatch cannot diagnose"
    forbidden = ("write", "edit", "push", "commit", "send", "delete", "merge", "rm ")
    for tool in tools:
        low = tool.lower()
        assert not any(f in low for f in forbidden), f"stub grants a mutating tool: {tool}"


def test_stub_grants_no_neotoma_mcp_tools():
    """A stub must not self-authorize against the store it could not reach."""
    assert not [t for t in _stub_for().tools if "neotoma" in t.lower()]


def test_stub_bash_grants_are_scoped_never_bare():
    """A bare `Bash` grant is an arbitrary-command grant. Every Bash rule in the
    stub allowlist must carry a command scope."""
    for tool in _stub_for().tools:
        if tool.startswith("Bash"):
            assert tool.startswith("Bash("), f"unscoped Bash grant in stub: {tool}"


def test_stub_is_flagged_and_carries_reason():
    stub = _stub_for(reason="ReadTimeout")
    assert stub.is_stub is True
    assert "ReadTimeout" in stub.load_error
    assert stub.prompt_markdown == ""
    assert stub.status == agent_loader.UNDEFINED_STATUS


def test_stub_escalates_not_just_logs(monkeypatch):
    """A degraded load must reach the operator, not only a log nobody reads."""
    seen = []
    monkeypatch.setattr(agent_loader, "_escalate", lambda msg: seen.append(msg))
    agent_loader.AgentLoader("pavo")._stub("ReadTimeout")
    assert len(seen) == 1
    assert "pavo" in seen[0]


def test_escalate_swallows_notifier_failure(monkeypatch):
    """Notification is best-effort: a broken notifier must never turn a degraded
    load into a crashed daemon."""
    import builtins

    real_import = builtins.__import__

    def boom(name, *args, **kwargs):
        if name == "lib.notify":
            raise RuntimeError("notifier exploded")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", boom)
    agent_loader._escalate("anything")  # must not raise


def test_timeout_read_from_shared_helper(monkeypatch):
    """The loader must use neotoma_timeout(), not a hardcoded literal."""
    monkeypatch.setenv("ATELES_NEOTOMA_TIMEOUT", "123")
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["timeout"] = timeout

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"entities": []}

        return R()

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(agent_loader.ns, "via_cli_enabled", lambda: False)
    agent_loader.AgentLoader("pavo")._neotoma("POST", "http://x/entities/query", {})
    assert captured["timeout"] == 123.0


def test_timeout_default_would_not_have_expired_on_measured_reads(monkeypatch):
    """Guards the actual production numbers: the old 10s budget expired on all
    three measured reads (32.3s, 19.0s, 11.2s); the new default clears them."""
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["timeout"] = timeout

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"snapshot": {"name": "pavo"}}

        return R()

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(agent_loader.ns, "via_cli_enabled", lambda: False)
    agent_loader.AgentLoader("pavo")._neotoma("GET", "http://x/entities/abc")
    for measured in (32.3, 19.0, 11.2):
        assert captured["timeout"] > measured


def test_read_timeout_produces_least_privilege_stub_end_to_end(monkeypatch):
    """The live failure, reproduced: a ReadTimeout on the name search must yield
    a flagged, read-only stub — not an unrestricted one."""

    def fake_post(url, json=None, headers=None, timeout=None):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(agent_loader.ns, "via_cli_enabled", lambda: False)
    definition = agent_loader.AgentLoader("accipiter").load()
    assert definition.is_stub is True
    assert definition.tools != ["*"]
    assert "ReadTimeout" in definition.load_error or "timed out" in definition.load_error
