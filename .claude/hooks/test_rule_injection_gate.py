#!/usr/bin/env python3
"""Tests for rule_injection_gate.py (rule delivery audit ent_b66293f0dcc8c887d4fdbeae,
recommendation 5: inject the full rule text at the point of use rather than
trust a prose "fetch before acting" instruction the audit found followed in
1/17 applicable cases).

Runs the hook both as a pure function (matched_categories — no subprocess,
no network) and as a REAL subprocess against a fake local Neotoma HTTP server
(matching this directory's convention: test_session_rule_delivery.py,
test_session_rule_index.py).

Cases:
  1. An agent_grant `correct` call matches grant_write and injects the
     mapped rule's full text, PLUS the same-turn probe reminder.
  2. An agent_policy `store` call (single entity and combined entities[])
     matches policy_write.
  3. An Edit/Write to ~/.cursor/mcp.json matches harness_config.
  4. A Bash command touching ~/.claude/settings.json matches harness_config.
  5. A `gh api .../security-advisories` Bash call matches advisory.
  6. An unrelated tool call (e.g. Read, or a Neotoma read tool) matches
     nothing and injects nothing.
  7. Fail-open: Neotoma unreachable -> exit 0, no crash, categories with no
     row still get the grant probe reminder (it does not depend on Neotoma).
  8. A single call matching two categories injects both categories' rules.
  9. A row whose id is mapped but whose scope withholds it from this session
     (agent-scoped, no GOVERNS edge) is not injected — same predicate
     session_rule_index.py applies before rendering (decision 114) — and a
     grant write's probe reminder still fires regardless.
"""
from __future__ import annotations

import http.server
import json
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
HOOK = str(HERE / "rule_injection_gate.py")
REPO_ROOT = HERE.parents[1]

sys.path.insert(0, str(HERE))
import rule_injection_gate as gate  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _row(entity_id, rule="Do the thing.", applies_when="always", scope="global",
         status="active", domain="test", rule_kind="mandatory", title="T"):
    return {
        "entity_id": entity_id,
        "last_observation_at": "2026-01-01T00:00:00.000Z",
        "snapshot": {
            "rule": rule,
            "title": title,
            "applies_when": applies_when,
            "scope": scope,
            "status": status,
            "domain": domain,
            "rule_kind": rule_kind,
        },
    }


class _FakeNeotomaHandler(http.server.BaseHTTPRequestHandler):
    rows: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps({"entities": self.rows}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # noqa: A003
        pass


@pytest.fixture
def fake_neotoma():
    handler = type("Handler", (_FakeNeotomaHandler,), {"rows": []})
    port = _free_port()
    server = http.server.HTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", handler
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _run(event: dict, base_url: str | None = None):
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin"}
    if base_url:
        env["NEOTOMA_BASE_URL"] = base_url
    return subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=15,
    )


# --------------------------------------------------------------------- pure matcher


class TestMatchedCategories:
    def test_correct_targeting_agent_grant_matches_grant_write(self):
        cats = gate.matched_categories(
            "mcp__mcpsrv_neotoma__correct",
            {"entity_id": "ent_x", "entity_type": "agent_grant", "field": "entity_types"},
        )
        assert cats == ["grant_write"]

    def test_correct_targeting_agent_policy_matches_policy_write(self):
        cats = gate.matched_categories(
            "mcp__mcpsrv_neotoma__correct",
            {"entity_id": "ent_x", "entity_type": "agent_policy", "field": "rule"},
        )
        assert cats == ["policy_write"]

    def test_store_with_entities_list_reads_each_entity_type(self):
        cats = gate.matched_categories(
            "mcp__mcpsrv_neotoma__store",
            {"entities": [{"entity_type": "task"}, {"entity_type": "relationship_type"}]},
        )
        assert cats == ["policy_write"]

    def test_store_with_both_grant_and_policy_entities_matches_both(self):
        cats = gate.matched_categories(
            "mcp__mcpsrv_neotoma__store",
            {"entities": [{"entity_type": "agent_grant"}, {"entity_type": "agent_policy"}]},
        )
        assert set(cats) == {"grant_write", "policy_write"}

    def test_edit_to_cursor_mcp_json_matches_harness_config(self):
        cats = gate.matched_categories(
            "Edit", {"file_path": "/Users/op/.cursor/mcp.json"}
        )
        assert cats == ["harness_config"]

    def test_write_to_claude_settings_matches_harness_config(self):
        cats = gate.matched_categories(
            "Write", {"file_path": "/Users/op/repo/.claude/settings.json"}
        )
        assert cats == ["harness_config"]

    def test_bash_command_touching_neotoma_aauth_matches_harness_config(self):
        cats = gate.matched_categories(
            "Bash", {"command": "cat ~/.neotoma/aauth/keys.json"}
        )
        assert cats == ["harness_config"]

    def test_bash_gh_security_advisory_matches_advisory(self):
        cats = gate.matched_categories(
            "Bash", {"command": "gh api /repos/o/r/security-advisories"}
        )
        assert cats == ["advisory"]

    def test_bash_gh_advisory_prose_matches_advisory(self):
        cats = gate.matched_categories(
            "Bash", {"command": "gh issue create --title 'security-advisory follow-up'"}
        )
        assert cats == ["advisory"]

    def test_unrelated_read_matches_nothing(self):
        assert gate.matched_categories("Read", {"file_path": "/tmp/x.txt"}) == []

    def test_unrelated_neotoma_read_matches_nothing(self):
        assert (
            gate.matched_categories(
                "mcp__mcpsrv_neotoma__retrieve_entity_snapshot", {"entity_id": "ent_x"}
            )
            == []
        )

    def test_correct_targeting_unrelated_entity_type_matches_nothing(self):
        assert (
            gate.matched_categories(
                "mcp__mcpsrv_neotoma__correct",
                {"entity_id": "ent_x", "entity_type": "task", "field": "status"},
            )
            == []
        )

    def test_bash_unrelated_command_matches_nothing(self):
        assert gate.matched_categories("Bash", {"command": "ls -la"}) == []


# --------------------------------------------------------------------- effect (subprocess)


class TestGrantWriteInjectsRuleAndProbeReminder:
    def test_effect(self, fake_neotoma):
        base_url, handler = fake_neotoma
        handler.rows = [_row("ent_1c0cbb99d2c8011358ff1dc3", rule="STOP_BEFORE_HIGH_RISK_CANARY")]
        result = _run(
            {
                "tool_name": "mcp__mcpsrv_neotoma__correct",
                "tool_input": {
                    "entity_id": "ent_58ecd30ea709bece07025df1",
                    "entity_type": "agent_grant",
                    "field": "entity_types",
                    "value": [],
                },
            },
            base_url=base_url,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert payload["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert "STOP_BEFORE_HIGH_RISK_CANARY" in ctx
        assert "ent_1c0cbb99d2c8011358ff1dc3" in ctx
        assert "get_session_identity" in ctx  # the probe reminder
        assert "BOTH before and after" in ctx


class TestPolicyWriteInjectsMappedRules:
    def test_effect(self, fake_neotoma):
        base_url, handler = fake_neotoma
        handler.rows = [
            _row("ent_1c0cbb99d2c8011358ff1dc3", rule="STOP_CANARY"),
            _row("ent_82b64b6c4104843e43853666", rule="MERGE_DONT_DUPLICATE_CANARY"),
        ]
        result = _run(
            {
                "tool_name": "mcp__mcpsrv_neotoma__store",
                "tool_input": {"entities": [{"entity_type": "agent_policy", "rule": "x"}]},
            },
            base_url=base_url,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert "STOP_CANARY" in ctx
        assert "MERGE_DONT_DUPLICATE_CANARY" in ctx
        assert "get_session_identity" not in ctx  # not a grant write


class TestHarnessConfigInjectsMappedRule:
    def test_effect(self, fake_neotoma):
        base_url, handler = fake_neotoma
        handler.rows = [_row("ent_c4d33237ff2d12b4aaec71af", rule="CURSOR_HTTP_MCP_CANARY")]
        result = _run(
            {"tool_name": "Edit", "tool_input": {"file_path": "/Users/op/.cursor/mcp.json"}},
            base_url=base_url,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert "CURSOR_HTTP_MCP_CANARY" in ctx


class TestOutOfScopeRowIsWithheld:
    def test_agent_scoped_row_with_no_governs_edge_is_not_injected(self, fake_neotoma):
        """session_rule_index.py filters every row through `_session_scope_ok`
        (decision 114) before rendering; this hook must apply the identical
        predicate before matching by id, or a row re-scoped away from this
        session in Neotoma would stop appearing in the SessionStart index
        while still being injected here — the two delivery paths silently
        disagreeing about who is allowed to see the row. `ent_1c0cbb99d2c8011358ff1dc3`
        is a real mapped id (see _CATEGORY_RULE_IDS); this fixture makes ITS
        content agent-scoped with no GOVERNS edge, which `_session_scope_ok`
        withholds from every session (edgeless agent-scoped rows bind
        nobody — fail-closed, principles.md #5)."""
        base_url, handler = fake_neotoma
        handler.rows = [
            _row(
                "ent_1c0cbb99d2c8011358ff1dc3",
                rule="SHOULD_NOT_LEAK_CANARY",
                scope="agent",
            )
        ]
        result = _run(
            {
                "tool_name": "mcp__mcpsrv_neotoma__correct",
                "tool_input": {
                    "entity_id": "ent_x",
                    "entity_type": "agent_policy",
                    "field": "rule",
                },
            },
            base_url=base_url,
        )
        assert result.returncode == 0, result.stderr
        # policy_write has no probe reminder, so a withheld row means no
        # matching categories fetched anything -> nothing printed at all.
        assert "SHOULD_NOT_LEAK_CANARY" not in result.stdout

    def test_grant_write_still_gets_probe_reminder_when_its_row_is_out_of_scope(
        self, fake_neotoma
    ):
        """grant_write's probe reminder does not depend on Neotoma at all
        (TestFailOpenOnUnreachableNeotoma covers the down-Neotoma case); this
        proves it also survives the row being fetched but scope-withheld —
        the reminder is unconditional on the category matching, not on any
        particular row rendering."""
        base_url, handler = fake_neotoma
        handler.rows = [
            _row(
                "ent_1c0cbb99d2c8011358ff1dc3",
                rule="SHOULD_NOT_LEAK_CANARY",
                scope="agent",
            )
        ]
        result = _run(
            {
                "tool_name": "mcp__mcpsrv_neotoma__correct",
                "tool_input": {
                    "entity_id": "ent_58ecd30ea709bece07025df1",
                    "entity_type": "agent_grant",
                    "field": "entity_types",
                    "value": [],
                },
            },
            base_url=base_url,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert "SHOULD_NOT_LEAK_CANARY" not in ctx
        assert "get_session_identity" in ctx


class TestNoMatchInjectsNothing:
    def test_effect(self, fake_neotoma):
        base_url, _handler = fake_neotoma
        result = _run(
            {"tool_name": "Read", "tool_input": {"file_path": "/tmp/x.txt"}},
            base_url=base_url,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ""


class TestFailOpenOnUnreachableNeotoma:
    def test_policy_write_with_dead_neotoma_prints_nothing_and_exits_zero(self):
        result = _run(
            {
                "tool_name": "mcp__mcpsrv_neotoma__correct",
                "tool_input": {"entity_id": "ent_x", "entity_type": "agent_policy", "field": "rule"},
            },
            base_url="http://127.0.0.1:1",  # nothing listens here
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_grant_write_with_dead_neotoma_still_gets_probe_reminder(self):
        """The probe instruction does not depend on fetching a rule row, so it
        must survive a Neotoma outage — the exact condition under which the
        audited failure (an unverified grant write) is most likely to recur."""
        result = _run(
            {
                "tool_name": "mcp__mcpsrv_neotoma__correct",
                "tool_input": {"entity_id": "ent_x", "entity_type": "agent_grant", "field": "entity_types"},
            },
            base_url="http://127.0.0.1:1",
        )
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert "get_session_identity" in ctx


class TestMalformedInputFailsOpen:
    def test_no_tool_name_prints_nothing(self):
        result = _run({"tool_input": {"entity_type": "agent_grant"}})
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_non_dict_tool_input_prints_nothing(self):
        result = _run({"tool_name": "mcp__mcpsrv_neotoma__correct", "tool_input": "oops"})
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_garbage_stdin_exits_zero(self):
        result = subprocess.run(
            [sys.executable, HOOK],
            input="not json{{{",
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env={"PATH": "/usr/bin:/bin:/usr/local/bin"},
            timeout=15,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""
