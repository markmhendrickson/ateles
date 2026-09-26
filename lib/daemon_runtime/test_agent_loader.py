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

    `load_active_policies` also issues an `/entities/query` for
    `agent_definition` (decision 114's id resolution) and a
    `/list_relationships` for `GOVERNS` edges — this test tracks calls by
    URL rather than asserting on the single last one, so it isn't sensitive
    to how many other reads the edge resolver makes.
    """
    _no_signing(monkeypatch)
    monkeypatch.setattr(al, "NEOTOMA_BEARER_TOKEN", "tok")
    seen_urls = []

    def fake_post(url, **kwargs):
        seen_urls.append(url)
        return _Resp({"entities": [], "relationships": []})

    monkeypatch.setattr(al.httpx, "post", fake_post)
    al.AgentLoader("apis").load_active_policies()

    query_urls = [u for u in seen_urls if u.endswith("/entities/query")]
    assert query_urls, seen_urls
    assert not any("retrieve_entities" in u for u in seen_urls), (
        "retrieve_entities is an MCP tool name, not a REST path — it 404s"
    )


def test_load_active_policies_returns_matching_agent_policy(monkeypatch):
    """A successful query yields the agent's own active/provisional policies,
    bound by a `GOVERNS` edge (decision 114) rather than by `agent_sub`
    equality — `agent_sub` is superseded and is populated on these fixture
    rows only to show it is NOT what the resolver reads.
    """
    _no_signing(monkeypatch)
    monkeypatch.setattr(al, "NEOTOMA_BEARER_TOKEN", "tok")
    policy_payload = {
        "entities": [
            {"entity_id": "ent_mine",
             "snapshot": {"agent_sub": "apis@ateles-swarm", "status": "active",
                          "rule": "mine"}},
            {"entity_id": "ent_theirs",
             "snapshot": {"agent_sub": "other@ateles-swarm", "status": "active",
                          "rule": "theirs"}},
            {"entity_id": "ent_old",
             "snapshot": {"agent_sub": "apis@ateles-swarm", "status": "retired",
                          "rule": "old"}},
        ]
    }
    definition_payload = {
        "entities": [
            {"entity_id": "ent_apis_def", "snapshot": {"name": "apis"}},
        ]
    }
    relationships_payload = {
        "relationships": [
            {"source_entity_id": "ent_mine", "target_entity_id": "ent_apis_def"},
        ]
    }

    def fake_post(url, json=None, **kw):
        if url.endswith("/list_relationships"):
            return _Resp(relationships_payload)
        if url.endswith("/entities/query") and (json or {}).get("entity_type") == "agent_definition":
            return _Resp(definition_payload)
        return _Resp(policy_payload)

    monkeypatch.setattr(al.httpx, "post", fake_post)

    out = al.AgentLoader("apis").load_active_policies()
    assert [p["rule"] for p in out] == ["mine"]


def test_load_active_policies_ignores_agent_sub_with_no_edge(monkeypatch):
    """A row scoped by `agent_sub` alone, with no `GOVERNS` edge, binds
    NOBODY under decision 114 — `agent_sub`/`scope: agent` are superseded,
    not a fallback when no edge exists. This is the behavior change the
    ruling makes: before, this fixture (`agent_sub` populated, matching)
    would have bound; now it must not, until migrated to an edge.
    """
    _no_signing(monkeypatch)
    monkeypatch.setattr(al, "NEOTOMA_BEARER_TOKEN", "tok")
    policy_payload = {
        "entities": [
            {"entity_id": "ent_unmigrated",
             "snapshot": {"agent_sub": "apis@ateles-swarm", "status": "active",
                          "rule": "unmigrated"}},
        ]
    }

    def fake_post(url, **kw):
        if url.endswith("/list_relationships"):
            return _Resp({"relationships": []})
        return _Resp(policy_payload)

    monkeypatch.setattr(al.httpx, "post", fake_post)

    out = al.AgentLoader("apis").load_active_policies()
    assert out == []


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


class TestPolicyScoping:
    """ateles#1118 — `agent_sub` was populated 0 of 25 rows, so every agent
    loaded ZERO policies. The filter required `agent_sub` equality
    unconditionally, which also meant the 12 `scope: global` rows reached NO
    agent rather than every agent — the opposite of what global means.

    The fixture is the PRODUCTION shape: empty `agent_sub`, `scope: global`.
    A test whose fixture already sets `agent_sub` passes against the defect.
    """

    def test_global_scope_with_no_agent_sub_reaches_every_agent(self):
        snap = {"scope": "global", "rule": "always read a write back"}
        assert al.policy_binds_agent(snap, "corvus@ateles-swarm")
        assert al.policy_binds_agent(snap, "pavo@ateles-swarm")

    def test_swarm_scope_reaches_every_agent(self):
        snap = {"scope": "swarm"}
        assert al.policy_binds_agent(snap, "anyone@ateles-swarm")

    def test_agent_scope_binds_only_its_own_agent(self):
        snap = {"scope": "agent", "agent_sub": "corvus@ateles-swarm"}
        assert al.policy_binds_agent(snap, "corvus@ateles-swarm")
        assert not al.policy_binds_agent(snap, "pavo@ateles-swarm")

    def test_agent_scope_with_empty_agent_sub_binds_nobody(self):
        # The design refuses this row at the write; a reader that meets one
        # anyway must not broadcast it.
        assert not al.policy_binds_agent({"scope": "agent"}, "corvus@ateles-swarm")

    def test_unrecognised_scope_fails_closed(self):
        # principles.md#5 — `scope` carries the REACH of a rule, so an
        # unreadable value takes the narrowest reading, never the widest.
        assert not al.policy_binds_agent({"scope": "everyone"}, "corvus@ateles-swarm")
        assert not al.policy_binds_agent({}, "corvus@ateles-swarm")

    def test_scope_match_is_case_insensitive(self):
        assert al.policy_binds_agent({"scope": "GLOBAL"}, "corvus@ateles-swarm")

    def test_exact_agent_sub_binds_even_with_no_scope(self):
        # Tolerant on the row the old filter DID accept: no regression for
        # rows that already worked.
        snap = {"agent_sub": "corvus@ateles-swarm"}
        assert al.policy_binds_agent(snap, "corvus@ateles-swarm")

    def test_still_has_a_live_production_caller_in_generalizer(self):
        """Decision 114 (2026-09-25) superseded `policy_binds_agent` for the
        two readers it names (`AgentLoader.load_active_policies`,
        `policy_skill_renderer._session_scope_ok` — both now call
        `policy_binds_agent_by_edge` instead), but did NOT remove or
        deprecate this function, because `generalizer.py`'s
        `fetch_agent_policies` still calls it directly and decision 114's
        scope is only the two named readers. This test pins that fact
        mechanically: if `generalizer.py` is ever migrated off it too, this
        assertion should be updated in the SAME change that does the
        migration — not left to silently pass on a function nothing calls.
        """
        import ast
        import inspect

        import generalizer as gz

        source = inspect.getsource(gz)
        tree = ast.parse(source)
        calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "policy_binds_agent" in calls, (
            "generalizer.py no longer calls policy_binds_agent — if this is "
            "an intentional migration to policy_binds_agent_by_edge, remove "
            "or deprecate policy_binds_agent in the same change rather than "
            "leaving it an orphaned function"
        )


class TestPolicyBindsAgentByEdge:
    """Decision 114 (2026-09-25, operator ruling on the master plan's
    `decisions.agent_policy_binds_agent_by_graph_edge`): an agent-specific
    `agent_policy` is tied to the agent(s) it governs by a `GOVERNS` edge to
    their `agent_definition`, resolved by traversal — `scope`/`agent_sub`
    are superseded, not consulted as a fallback. These tests fail if the
    edge traversal is reverted to the field-based predicate.
    """

    def test_edge_bound_row_reaches_only_its_agent(self):
        # A row with a GOVERNS edge to corvus's agent_definition binds
        # corvus and NO other agent — including one that would have matched
        # under the old field-based predicate had `agent_sub` named it.
        snap = {"_entity_id": "ent_rule", "scope": "swarm"}  # scope ignored: edge wins
        governs = {"ent_rule": frozenset({"ent_corvus_def"})}
        assert al.policy_binds_agent_by_edge(snap, "ent_corvus_def", governs) is True
        assert al.policy_binds_agent_by_edge(snap, "ent_pavo_def", governs) is False

    def test_edge_bound_row_with_scope_swarm_still_doesnt_reach_others(self):
        # The task's own emphasis: an edge overrides a swarm-wide scope
        # rather than adding to it. A row with BOTH an edge and
        # `scope: swarm` must still be refused for an agent the edge does
        # not name — the edge is more specific and wins outright.
        snap = {"_entity_id": "ent_rule", "scope": "swarm"}
        governs = {"ent_rule": frozenset({"ent_corvus_def"})}
        assert al.policy_binds_agent_by_edge(snap, "ent_corvus_def", governs) is True
        assert al.policy_binds_agent_by_edge(snap, "ent_lanius_def", governs) is False

    def test_swarm_row_without_edge_reaches_all(self):
        snap = {"_entity_id": "ent_swarm_rule", "scope": "swarm"}
        assert al.policy_binds_agent_by_edge(snap, "ent_corvus_def", {}) is True
        assert al.policy_binds_agent_by_edge(snap, "ent_pavo_def", {}) is True

    def test_global_row_without_edge_reaches_all(self):
        snap = {"_entity_id": "ent_global_rule", "scope": "global"}
        assert al.policy_binds_agent_by_edge(snap, "ent_corvus_def", {}) is True

    def test_edgeless_agent_sub_row_binds_nobody(self):
        # The superseded shape: scope=agent + agent_sub, no edge. Binds
        # NOBODY — agent_sub is not read as a fallback.
        snap = {
            "_entity_id": "ent_unmigrated",
            "scope": "agent",
            "agent_sub": "corvus@ateles-swarm",
        }
        assert al.policy_binds_agent_by_edge(snap, "ent_corvus_def", {}) is False

    def test_edgeless_unrecognised_scope_binds_nobody(self):
        snap = {"_entity_id": "ent_bad_scope", "scope": "everyone"}
        assert al.policy_binds_agent_by_edge(snap, "ent_corvus_def", {}) is False

    def test_edgeless_absent_scope_binds_nobody(self):
        snap = {"_entity_id": "ent_no_scope"}
        assert al.policy_binds_agent_by_edge(snap, "ent_corvus_def", {}) is False

    def test_empty_agent_definition_id_matches_no_edge(self):
        # An unresolvable session principal (agent_definition_id="") must
        # not accidentally match an edge target — fail closed, never a
        # wildcard.
        snap = {"_entity_id": "ent_rule"}
        governs = {"ent_rule": frozenset({"ent_corvus_def"})}
        assert al.policy_binds_agent_by_edge(snap, "", governs) is False


class TestFetchGovernsEdges:
    """`fetch_governs_edges` batches ALL `GOVERNS` edges in one
    `/list_relationships` call and returns {agent_policy_id: {agent_definition_id, ...}}.
    """

    def test_batches_into_one_call_and_groups_by_source(self):
        calls = []

        def fake_request(url, body, timeout):
            calls.append((url, body))
            return {
                "relationships": [
                    {"source_entity_id": "ent_a", "target_entity_id": "ent_x"},
                    {"source_entity_id": "ent_a", "target_entity_id": "ent_y"},
                    {"source_entity_id": "ent_b", "target_entity_id": "ent_z"},
                ]
            }

        out = al.fetch_governs_edges("https://neotoma.example", _request=fake_request)
        assert len(calls) == 1, "must issue exactly ONE relationships query per load"
        assert calls[0][1]["relationship_type"] == "GOVERNS"
        assert out == {
            "ent_a": frozenset({"ent_x", "ent_y"}),
            "ent_b": frozenset({"ent_z"}),
        }

    def test_malformed_rows_are_skipped_not_fatal(self):
        def fake_request(url, body, timeout):
            return {
                "relationships": [
                    {"source_entity_id": "ent_a"},  # no target
                    {"target_entity_id": "ent_x"},  # no source
                    "not-a-dict",
                    {"source_entity_id": "ent_ok", "target_entity_id": "ent_ok2"},
                ]
            }

        out = al.fetch_governs_edges("https://neotoma.example", _request=fake_request)
        assert out == {"ent_ok": frozenset({"ent_ok2"})}

    def test_raises_on_transport_failure(self):
        def boom(url, body, timeout):
            raise ConnectionError("down")

        try:
            al.fetch_governs_edges("https://neotoma.example", _request=boom)
            raise AssertionError("expected the transport error to propagate")
        except ConnectionError:
            pass


class TestResolveAgentDefinitionId:
    def test_prefers_aauth_sub_match_over_name_match(self):
        def fake_request(url, body, timeout):
            return {
                "entities": [
                    {"entity_id": "ent_by_name", "snapshot": {"name": "corvus"}},
                    {
                        "entity_id": "ent_by_aauth_sub",
                        "snapshot": {"name": "corvus-old", "aauth_sub": "corvus@ateles-swarm"},
                    },
                ]
            }

        resolved = al.resolve_agent_definition_id(
            "corvus@ateles-swarm", "https://neotoma.example", _request=fake_request
        )
        assert resolved == "ent_by_aauth_sub"

    def test_falls_back_to_name_match_when_no_aauth_sub_matches(self):
        def fake_request(url, body, timeout):
            return {"entities": [{"entity_id": "ent_named", "snapshot": {"name": "corvus"}}]}

        resolved = al.resolve_agent_definition_id(
            "corvus@ateles-swarm", "https://neotoma.example", _request=fake_request
        )
        assert resolved == "ent_named"

    def test_no_match_returns_none(self):
        def fake_request(url, body, timeout):
            return {"entities": []}

        resolved = al.resolve_agent_definition_id(
            "nobody@ateles-swarm", "https://neotoma.example", _request=fake_request
        )
        assert resolved is None

    def test_transport_failure_returns_none_rather_than_raising(self):
        def boom(url, body, timeout):
            raise ConnectionError("down")

        resolved = al.resolve_agent_definition_id(
            "corvus@ateles-swarm", "https://neotoma.example", _request=boom
        )
        assert resolved is None


class TestRenderedPromptCarriesTheRule:
    """The acceptance test ateles#1118 names: assert the RULE TEXT is in the
    string `render_policy_prompt()` returns, from the PRODUCTION fixture.

    A query returning 200, or a list assertion against a fixture that already
    sets `agent_sub`, does not satisfy this — both pass against the defect.
    """

    @staticmethod
    def _loader_with(monkeypatch, rows):
        monkeypatch.setattr(al.ns, "via_cli_enabled", lambda: False)
        monkeypatch.setattr(al, "NEOTOMA_BEARER_TOKEN", "tok")
        monkeypatch.setattr(
            al.httpx, "post", lambda url, **kw: _Resp({"entities": rows})
        )
        return al.AgentLoader("corvus")

    def test_global_policy_reaches_the_rendered_prompt(self, monkeypatch):
        # RED ON MAIN: `agent_sub` is empty — the production shape — so the
        # old unconditional equality filter dropped it and the prompt was "".
        rows = [
            {
                "snapshot": {
                    "scope": "global",
                    "status": "active",
                    "rule_kind": "mandatory",
                    "rule": "Read a write back before reporting success.",
                }
            }
        ]
        prompt = self._loader_with(monkeypatch, rows).render_policy_prompt()
        assert "Read a write back before reporting success." in prompt

    def test_another_agents_policy_stays_out_of_the_prompt(self, monkeypatch):
        rows = [
            {
                "snapshot": {
                    "scope": "agent",
                    "agent_sub": "pavo@ateles-swarm",
                    "status": "active",
                    "rule": "Pavo-only rule.",
                }
            }
        ]
        assert self._loader_with(monkeypatch, rows).render_policy_prompt() == ""

    def test_empty_scoping_field_on_every_row_is_logged_loudly(
        self, monkeypatch, caplog
    ):
        # The zero-agents clause: rows came back and the filter matched none
        # BECAUSE the field is empty. That must not look like "no policies".
        rows = [
            {"snapshot": {"status": "active", "rule": "r1"}},
            {"snapshot": {"status": "active", "rule": "r2"}},
        ]
        loader = self._loader_with(monkeypatch, rows)
        with caplog.at_level("ERROR"):
            out = loader.load_active_policies()
        assert out == []
        assert any(
            "no row is agent-scoped by either mechanism" in r.message
            for r in caplog.records
        ), [r.message for r in caplog.records]
