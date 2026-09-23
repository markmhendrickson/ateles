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
    stub with an empty prompt and wildcard tools is indistinguishable from a
    real definition. A caller cannot tell it dispatched an agent with no prompt.
    """
    _no_signing(monkeypatch)
    monkeypatch.setattr(al, "NEOTOMA_BEARER_TOKEN", "tok")

    def boom(url, **kwargs):
        raise httpx.ConnectError("neotoma unreachable")

    monkeypatch.setattr(al.httpx, "post", boom)

    d = al.AgentLoader("apis").load()
    # The dangerous shape the stub actually has:
    assert d.prompt_markdown == ""
    assert d.tools == ["*"]
    # ...must be flagged, or a caller reports success while running blind.
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


RULE_1 = (
    "Pose every open decision through the harness questions tool "
    "(`AskUserQuestion`): one call, N labeled options, each with what it "
    "implies, what is settled, and a recommendation. Zero open decisions: "
    "no call and no trailer. If the tool is unavailable, print "
    "`[decisions-unposed]` and each question's text; do not use a numbered "
    "list. An unanswered item is restated in full next turn; the word "
    "unchanged is not a carrier. An answered item is dropped."
)
RULE_2 = (
    "Give a full URL for every pull request that needs the operator's "
    "approval, in the turn that needs the approval."
)
RULE_6 = (
    "Dispatch a subagent on every pulled email. Zero messages pulled: say "
    "zero pulled. A failed pull: say the pull failed, not that zero were "
    "pulled. A pull that succeeded whose dispatch failed: name the message "
    "and say that no subagent ran."
)
UNBOUND_LINE = (
    "[rules-unbound] missing=1,2,6 hint=resolve the related entity on the "
    "agent; do not paste rule text into prompt_markdown or CLAUDE.md — "
    "docs/operator_rules.md"
)
INCOMPLETE_2 = (
    "[rules-incomplete] missing=2 hint=resolve the related entity on the "
    "agent; do not paste rule text into prompt_markdown or CLAUDE.md — "
    "docs/operator_rules.md"
)


def _rule_row(number: int, text: str, **overrides) -> dict:
    fields = {
        "entity_type": "standing_rule",
        "title": f"{number}. rule",
        "rule_text": text,
        "scope": "ateles",
        "enabled": True,
    }
    fields.update(overrides)
    return {"entity_type": fields["entity_type"], "snapshot": fields}


def _related(agent_id: str, rules: list[tuple[str, dict]], *, incoming=None) -> dict:
    outgoing = []
    related = {}
    for target_id, row in rules:
        outgoing.append({
            "relationship_type": "REFERS_TO",
            "source_entity_id": agent_id,
            "target_entity_id": target_id,
        })
        related[target_id] = row
    payload = {"outgoing": outgoing, "related_entities": related, "incoming": incoming or []}
    return payload


def _query(prompt: str = "You are ateles.", entity_id: str = "ent_ateles") -> dict:
    return {
        "entities": [
            {
                "entity_id": entity_id,
                "snapshot": {"snapshot": {"name": "ateles", "prompt_markdown": prompt}},
            }
        ]
    }


class TestResolveOperatorRules:
    def _patch(self, monkeypatch, query, related=None, *, get_error=None):
        _no_signing(monkeypatch)
        calls = {"post": [], "get": [], "stub": 0}

        def post(url, **kwargs):
            calls["post"].append((url, kwargs.get("json") or kwargs.get("body")))
            return _Resp(query)

        def get(url, **kwargs):
            calls["get"].append((url, kwargs))
            if get_error is not None:
                raise get_error
            return _Resp(related or {"outgoing": [], "related_entities": {}})

        def stub(self, reason="unknown"):
            calls["stub"] += 1
            raise AssertionError(f"_stub must not run ({reason})")

        monkeypatch.setattr(al.httpx, "post", post)
        monkeypatch.setattr(al.httpx, "get", get)
        monkeypatch.setattr(al.AgentLoader, "_stub", stub)
        return calls

    def test_bound_from_edges_ignores_prompt_markdown(self, monkeypatch):
        agent_id = "ent_ateles"
        related = _related(agent_id, [
            ("ent_6", _rule_row(6, RULE_6)),
            ("ent_1", _rule_row(1, RULE_1)),
            ("ent_2", _rule_row(2, RULE_2)),
            ("ent_7", _rule_row(7, "Maintain a live session workboard.")),
        ])
        calls = self._patch(
            monkeypatch,
            _query("You are ateles."),
            related,
        )
        result = al.resolve_operator_rules()
        assert result.status == "bound"
        assert result.missing == []
        assert result.block.index(RULE_1) < result.block.index(RULE_2) < result.block.index(RULE_6)
        assert "[rules-unbound]" not in result.block
        assert "[rules-incomplete]" not in result.block
        assert "rules bound" not in result.block
        assert "Maintain a live session workboard." not in result.block
        assert getattr(result, "is_stub", False) is False
        assert calls["stub"] == 0
        assert calls["post"][0][0].endswith("/entities/query")
        assert "/related" in calls["get"][0][0]
        assert "/entities/" in calls["get"][0][0]
        assert "retrieve_related" not in calls["get"][0][0]
        assert calls["get"][0][1]["timeout"] == 10

    def test_no_edges_is_unbound(self, monkeypatch):
        self._patch(monkeypatch, _query(RULE_1 + RULE_2 + RULE_6), {"outgoing": [], "related_entities": {}})
        result = al.resolve_operator_rules()
        assert result.status == "unbound"
        assert result.missing == [1, 2, 6]
        assert result.block == UNBOUND_LINE

    def test_prompt_markdown_cannot_bind(self, monkeypatch):
        self._patch(
            monkeypatch,
            _query(f"{RULE_1}\n{RULE_2}\n{RULE_6}"),
            {"outgoing": [], "related_entities": {}},
        )
        result = al.resolve_operator_rules()
        assert result.status == "unbound"
        assert RULE_1 not in result.block

    def test_empty_rule_text_is_incomplete_not_unbound(self, monkeypatch):
        agent_id = "ent_ateles"
        related = _related(agent_id, [
            ("ent_1", _rule_row(1, RULE_1)),
            ("ent_2", _rule_row(2, "   ")),
            ("ent_6", _rule_row(6, RULE_6)),
        ])
        self._patch(monkeypatch, _query(), related)
        result = al.resolve_operator_rules()
        assert result.status == "incomplete"
        assert result.missing == [2]
        assert result.block == INCOMPLETE_2
        assert RULE_1 not in result.block
        assert RULE_6 not in result.block

    def test_get_error_is_unbound_not_stub(self, monkeypatch):
        self._patch(monkeypatch, _query(), get_error=httpx.ConnectError("down"))
        result = al.resolve_operator_rules()
        assert result.status == "unbound"
        assert result.missing == [1, 2, 6]
        assert getattr(result, "is_stub", False) is False

    def test_post_error_is_unbound(self, monkeypatch):
        _no_signing(monkeypatch)

        def boom(url, **kwargs):
            raise httpx.ConnectError("down")

        monkeypatch.setattr(al.httpx, "post", boom)
        monkeypatch.setattr(al.AgentLoader, "_stub", lambda *a, **k: (_ for _ in ()).throw(AssertionError("stub")))
        result = al.resolve_operator_rules()
        assert result.status == "unbound"
        assert result.block == UNBOUND_LINE

    def test_only_two_rules_is_incomplete(self, monkeypatch):
        agent_id = "ent_ateles"
        related = _related(agent_id, [
            ("ent_6", _rule_row(6, RULE_6)),
            ("ent_1", _rule_row(1, RULE_1)),
        ])
        self._patch(monkeypatch, _query(), related)
        result = al.resolve_operator_rules()
        assert result.status == "incomplete"
        assert result.missing == [2]
        assert "missing=2" in result.block

    def test_missing_numbers_are_ascending(self, monkeypatch):
        agent_id = "ent_ateles"
        related = _related(agent_id, [("ent_2", _rule_row(2, RULE_2))])
        self._patch(monkeypatch, _query(), related)
        result = al.resolve_operator_rules()
        assert result.status == "incomplete"
        assert "missing=1,6" in result.block
        assert "missing=6,1" not in result.block

    def test_filters_drop_all_three_is_unbound(self, monkeypatch):
        agent_id = "ent_ateles"
        related = _related(agent_id, [
            ("a", _rule_row(1, RULE_1, enabled=False)),
            ("b", _rule_row(2, RULE_2, scope="other")),
            ("c", _rule_row(6, RULE_6, title="six without prefix")),
            ("d", {"entity_type": "note", "snapshot": {"title": "1. x", "rule_text": RULE_1, "scope": "ateles"}}),
        ])
        # Incoming edge must not count: agent is the target.
        related["outgoing"].append({
            "relationship_type": "REFERS_TO",
            "source_entity_id": "ent_other",
            "target_entity_id": "ent_in",
        })
        related["related_entities"]["ent_in"] = _rule_row(1, RULE_1)
        related["outgoing"].append({
            "relationship_type": "RELATES_TO",
            "source_entity_id": agent_id,
            "target_entity_id": "ent_wrong_edge",
        })
        related["related_entities"]["ent_wrong_edge"] = _rule_row(2, RULE_2)
        self._patch(monkeypatch, _query(), related)
        result = al.resolve_operator_rules()
        assert result.status == "unbound"
        assert result.missing == [1, 2, 6]

    def test_enabled_absent_counts_as_enabled(self, monkeypatch):
        agent_id = "ent_ateles"
        row1 = _rule_row(1, RULE_1)
        del row1["snapshot"]["enabled"]
        related = _related(agent_id, [
            ("ent_1", row1),
            ("ent_2", _rule_row(2, RULE_2)),
            ("ent_6", _rule_row(6, RULE_6)),
        ])
        self._patch(monkeypatch, _query(), related)
        assert al.resolve_operator_rules().status == "bound"

    def test_one_of_two_rule_2_texts_binds(self, monkeypatch):
        agent_id = "ent_ateles"
        related = _related(agent_id, [
            ("ent_1", _rule_row(1, RULE_1)),
            ("ent_2a", _rule_row(2, "")),
            ("ent_2b", _rule_row(2, RULE_2)),
            ("ent_6", _rule_row(6, RULE_6)),
        ])
        self._patch(monkeypatch, _query(), related)
        assert al.resolve_operator_rules().status == "bound"

    def test_both_rule_2_empty_is_incomplete(self, monkeypatch):
        agent_id = "ent_ateles"
        related = _related(agent_id, [
            ("ent_1", _rule_row(1, RULE_1)),
            ("ent_2a", _rule_row(2, "")),
            ("ent_2b", _rule_row(2, "  ")),
            ("ent_6", _rule_row(6, RULE_6)),
        ])
        self._patch(monkeypatch, _query(), related)
        result = al.resolve_operator_rules()
        assert result.status == "incomplete"
        assert result.missing == [2]

    def test_related_404_falls_back_to_relationships(self, monkeypatch):
        _no_signing(monkeypatch)
        seen = []

        def post(url, **kwargs):
            return _Resp(_query())

        def get(url, **kwargs):
            seen.append(url)
            if url.endswith("/related?expand_entities=true") or "/related?" in url:
                return _Resp({}, status=404)
            return _Resp(_related("ent_ateles", [
                ("ent_1", _rule_row(1, RULE_1)),
                ("ent_2", _rule_row(2, RULE_2)),
                ("ent_6", _rule_row(6, RULE_6)),
            ]))

        monkeypatch.setattr(al.httpx, "post", post)
        monkeypatch.setattr(al.httpx, "get", get)
        result = al.resolve_operator_rules()
        assert result.status == "bound"
        assert any("/relationships" in url for url in seen)
