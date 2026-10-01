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
  10. A row that is `scope="global"`/`"swarm"` but carries a live `GOVERNS`
      edge to a DIFFERENT agent's `agent_definition` is withheld — an edge
      overrides scope (`policy_binds_agent_by_edge`'s own doc), so this must
      not fall through to the swarm-wide branch just because the hook never
      fetched the edge map. This is the live QA finding at head 42fdf432:
      `_fetch_rows_by_id` called the bare `_session_scope_ok(row)` with no
      `agent_definition_id`/`governs`, which the function's own docstring
      says treats every row as edgeless — so a row governed only to another
      agent was scored purely on `scope` and wrongly injected. The
      companion positive case proves a row edged to THIS session's own
      resolved agent identity still renders.
  11. Read-only Bash commands and PR-comment prose that merely MENTION a
      harness_config path or the words "security-advisory" do not match —
      only an actual mutation shape (harness_config) or an actual `gh api`
      call to the advisories endpoint (advisory) does. Both classifiers use
      the same segment-split + affirmative-shape pattern (Falco/Accipiter,
      PR #1320 round 2 — the advisory fix closes the identical false-
      positive shape the harness_config fix closed one category earlier).
  12. A Codex `apply_patch` payload naming a harness_config path (Add/
      Update/Delete File, or Move to) matches harness_config the same way an
      Edit/Write does under Claude Code — parsed via `_apply_patch_paths`,
      delegated to `sibling_repo_worktree_guard.py`'s parser of the same
      name rather than re-implemented (Falco, PR #1320 round 2 non-blocking
      finding, now closed).
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
    stored_requests: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        request = json.loads(raw.decode()) if raw else {}
        if self.path.endswith("/store"):
            self.stored_requests.append(request)
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
    handler = type(
        "Handler", (_FakeNeotomaHandler,), {"rows": [], "stored_requests": []}
    )
    port = _free_port()
    server = http.server.HTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", handler
    finally:
        server.shutdown()
        thread.join(timeout=5)


# ent_ateles_def / ent_cicada_def below are fixture-only ids — no relation to
# any real Neotoma entity — standing in for "this session's own
# agent_definition" and "a different agent's agent_definition" respectively.
_ATELES_DEFINITION_ID = "ent_ateles_def"
_CICADA_DEFINITION_ID = "ent_cicada_def"


class _EdgeAwareNeotomaHandler(http.server.BaseHTTPRequestHandler):
    """Unlike `_FakeNeotomaHandler` (which answers every POST identically
    with `{"entities": self.rows}`), this handler routes by path so the
    gate's full `render_skills` path — `/entities/query` for both the
    `agent_policy` row fetch AND the `agent_definition` name-search
    (`resolve_agent_definition_id`), plus `/list_relationships` for the
    `GOVERNS` edge map (`fetch_governs_edges`) — gets a real, distinct
    response instead of silently being served rows shaped for a different
    endpoint. Needed because the bug under test is specifically that the old
    code never made the GOVERNS/agent_definition calls at all; a
    path-blind fake could not tell the two code paths apart.
    """

    rows: list[dict] = []
    governs_relationships: list[dict] = []
    agent_definition_rows: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            req_body = json.loads(raw.decode()) if raw else {}
        except json.JSONDecodeError:
            req_body = {}

        if self.path.endswith("/list_relationships"):
            payload = {"relationships": self.governs_relationships}
        elif req_body.get("entity_type") == "agent_definition":
            payload = {"entities": self.agent_definition_rows}
        else:
            payload = {"entities": self.rows}

        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # noqa: A003
        pass


@pytest.fixture
def edge_aware_fake_neotoma():
    handler = type(
        "EdgeAwareHandler",
        (_EdgeAwareNeotomaHandler,),
        {"rows": [], "governs_relationships": [], "agent_definition_rows": []},
    )
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
        env["NEOTOMA_BEARER_TOKEN"] = "fixture-bearer"  # gitleaks:allow
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

    def test_bash_command_mutating_neotoma_aauth_matches_harness_config(self):
        """`cat` is a read, not a mutation — moved to
        `TestBashReadOnlyMentionsDoNotInject` below, which is the point of
        this PR. This positive case uses an actual mutation shape (`cp` as
        destination) so the category still has Bash coverage for a real
        write to the path."""
        cats = gate.matched_categories(
            "Bash", {"command": "cp /tmp/keys.json ~/.neotoma/aauth/keys.json"}
        )
        assert cats == ["harness_config"]

    def test_apply_patch_update_file_matches_harness_config(self):
        """Codex's native file-edit tool — the coverage gap Falco's PR #1320
        round-2 non-blocking finding named: harness_config was file-path-
        based by design, but no apply_patch parser read this tool's payload
        shape (one string under `command`, not `file_path`), so this exact
        edit was reproducible under Codex with no rule injected. Confirmed
        RED before `_apply_patch_paths` was wired in (matched_categories had
        no apply_patch branch at all)."""
        patch = (
            "*** Begin Patch\n"
            "*** Update File: .claude/settings.json\n"
            "@@\n"
            "-old\n"
            "+new\n"
            "*** End Patch"
        )
        cats = gate.matched_categories("apply_patch", {"command": patch})
        assert cats == ["harness_config"]

    def test_apply_patch_add_file_matches_harness_config(self):
        patch = (
            "*** Begin Patch\n"
            "*** Add File: ~/.neotoma/aauth\n"
            "+new content\n"
            "*** End Patch"
        )
        cats = gate.matched_categories("apply_patch", {"command": patch})
        assert cats == ["harness_config"]

    def test_apply_patch_move_to_harness_config_matches(self):
        patch = (
            "*** Begin Patch\n"
            "*** Update File: /tmp/scratch.json\n"
            "*** Move to: /Users/op/.cursor/mcp.json\n"
            "@@\n"
            "-old\n"
            "+new\n"
            "*** End Patch"
        )
        cats = gate.matched_categories("apply_patch", {"command": patch})
        assert cats == ["harness_config"]

    def test_apply_patch_unrelated_file_matches_nothing(self):
        patch = (
            "*** Begin Patch\n"
            "*** Update File: src/foo.py\n"
            "@@\n"
            "-old\n"
            "+new\n"
            "*** End Patch"
        )
        assert gate.matched_categories("apply_patch", {"command": patch}) == []

    def test_bash_gh_security_advisory_matches_advisory(self):
        cats = gate.matched_categories(
            "Bash", {"command": "gh api /repos/o/r/security-advisories"}
        )
        assert cats == ["advisory"]

    def test_bash_gh_advisory_write_matches_advisory(self):
        cats = gate.matched_categories(
            "Bash",
            {"command": "gh api -X PATCH /repos/o/r/security-advisories/GHSA-xxxx"},
        )
        assert cats == ["advisory"]

    def test_bash_gh_api_graphql_advisory_field_matches_advisory(self):
        cats = gate.matched_categories(
            "Bash",
            {
                "command": (
                    "gh api graphql -f query="
                    "'query { repository(owner:\"o\",name:\"r\") "
                    "{ securityAdvisories(first:10) { nodes { id } } } }'"
                )
            },
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


class TestBashReadOnlyMentionsDoNotInject:
    """Accipiter's current-head UX review on PR #1320 (task
    ent_70038a4a54c9bbc82d606774) reproduced full rule-body injection from
    read-only Bash commands that merely mention a harness-config path —
    `git diff`, `git show`, `git log`, `cat`, and a `gh` comment body quoting
    the path. Each of these must fail red against 42fdf432's bare
    `_HARNESS_CONFIG_PATH_RE.search(command)` and pass green after the
    bounded command-shape classifier lands."""

    def test_git_diff_with_path_pathspec_does_not_match(self):
        assert (
            gate.matched_categories(
                "Bash",
                {"command": "git diff HEAD~1 --stat -- .claude/settings.json"},
            )
            == []
        )

    def test_git_show_of_path_does_not_match(self):
        assert (
            gate.matched_categories(
                "Bash", {"command": "git show HEAD:.claude/settings.json"}
            )
            == []
        )

    def test_git_log_with_path_pathspec_does_not_match(self):
        assert (
            gate.matched_categories(
                "Bash",
                {"command": "git log --oneline -- .claude/settings.json"},
            )
            == []
        )

    def test_cat_of_path_does_not_match(self):
        assert (
            gate.matched_categories("Bash", {"command": "cat .claude/settings.json"})
            == []
        )

    def test_rg_search_mentioning_path_does_not_match(self):
        assert (
            gate.matched_categories(
                "Bash", {"command": "rg harness_config .claude/settings.json"}
            )
            == []
        )

    def test_gh_pr_comment_body_quoting_path_does_not_match(self):
        assert (
            gate.matched_categories(
                "Bash",
                {
                    "command": (
                        "gh pr comment 1320 --body "
                        "\"see .claude/settings.json for the wiring\""
                    )
                },
            )
            == []
        )

    def test_gh_pr_view_mentioning_path_does_not_match(self):
        assert (
            gate.matched_categories(
                "Bash",
                {"command": "gh pr view 1320 --json body | grep settings.json"},
            )
            == []
        )

    def test_git_blame_of_path_does_not_match(self):
        assert (
            gate.matched_categories(
                "Bash", {"command": "git blame .claude/settings.json"}
            )
            == []
        )

    def test_ls_of_dot_claude_dir_does_not_match(self):
        assert gate.matched_categories("Bash", {"command": "ls -la .claude/"}) == []

    def test_head_of_path_does_not_match(self):
        assert (
            gate.matched_categories(
                "Bash", {"command": "head -50 .claude/settings.json"}
            )
            == []
        )

    def test_commit_message_mentioning_path_does_not_match(self):
        """A commit-message string quoting the path is prose, not a write —
        the `git commit` leader itself never touches `.claude/settings.json`
        as an argument."""
        assert (
            gate.matched_categories(
                "Bash",
                {
                    "command": (
                        "git commit -m 'fix(hooks): guard .claude/settings.json path'"
                    )
                },
            )
            == []
        )

    def test_gh_comment_body_describing_a_mutation_shape_does_not_match(self):
        """A self-review (regression case): checking the general mutation-
        shape allowlist BEFORE the read-only-leader exemption was tried and
        reverted because it reintroduced exactly this false positive — a
        `gh pr comment` body that merely DESCRIBES a `sed -i` fix in prose,
        rather than running one, must not match. Redirection is the only
        mutation shape allowed to override the leader exemption (see the
        module-level comment on `_bash_touches_harness_config`), and `gh pr
        comment` carries no redirect operator here."""
        assert (
            gate.matched_categories(
                "Bash",
                {
                    "command": (
                        "gh pr comment 1320 --body "
                        "\"the fix uses sed -i to patch .claude/settings.json\""
                    )
                },
            )
            == []
        )

    def test_commit_message_describing_a_mutation_shape_does_not_match(self):
        """Same regression class as above, via `git commit` rather than
        `gh pr comment`."""
        assert (
            gate.matched_categories(
                "Bash",
                {
                    "command": (
                        "git commit -m "
                        "'cp fallback for .claude/settings.json restore'"
                    )
                },
            )
            == []
        )

    def test_python_interpreter_mutation_is_a_documented_residual_gap(self):
        """NOT a claim of full coverage: an opaque interpreter invocation
        (`python3 -c ...`, a custom script) that mutates a harness-config
        path with no recognized shell mutation keyword (no `>`, `sed -i`,
        `cp`, ...) is not caught by this bounded heuristic — same posture as
        `gmail_send_gate.py`'s `TEXT_BEARING_LEADERS` docstring, which
        explicitly excludes interpreters from its exemption list rather than
        attempting to parse their payloads. This test documents the
        boundary rather than asserting a fix: the classifier trades this
        rare, already-present gap for eliminating the FREQUENT false
        positives (task ent_70038a4a54c9bbc82d606774) on ordinary
        inspection commands, which is the tradeoff the task asked for."""
        assert (
            gate.matched_categories(
                "Bash",
                {
                    "command": (
                        "python3 -c \"import json; "
                        "d=json.load(open('.claude/settings.json')); "
                        "json.dump(d, open('.claude/settings.json','w'))\""
                    )
                },
            )
            == []
        )


class TestBashAdvisoryProseMentionsDoNotInject:
    """Accipiter's current-head UX review on PR #1320 reproduced the SAME
    false-positive SHAPE the harness_config classifier above was built to
    eliminate, live on the `advisory` category one class over: a bare
    `security[-_]advisor|/security-advisories\\b` substring search matched a
    `gh pr comment`/`gh issue create` body or title that merely MENTIONS
    "security-advisory" in prose, never reading or writing an actual GitHub
    security advisory. The prior test suite asserted this as CORRECT
    behavior (`test_bash_gh_advisory_prose_matches_advisory`) — the "test
    that cannot fail on the thing it watches" pattern; these fail red
    against that bare substring search and pass green after
    `_bash_touches_advisory`'s `gh api`-shape classifier lands."""

    def test_gh_issue_create_title_mentioning_advisory_does_not_match(self):
        assert (
            gate.matched_categories(
                "Bash",
                {
                    "command": (
                        "gh issue create --title 'security-advisory follow-up'"
                    )
                },
            )
            == []
        )

    def test_gh_pr_comment_body_mentioning_advisory_does_not_match(self):
        """The exact reproduction from Accipiter's UX review."""
        assert (
            gate.matched_categories(
                "Bash",
                {
                    "command": (
                        "gh pr comment 1320 --body "
                        '"this PR touches security-advisory handling '
                        'in the linter"'
                    )
                },
            )
            == []
        )

    def test_gh_pr_view_mentioning_advisory_path_as_prose_does_not_match(self):
        assert (
            gate.matched_categories(
                "Bash",
                {
                    "command": (
                        "gh pr view 1320 --json body "
                        "| grep /security-advisories"
                    )
                },
            )
            == []
        )

    def test_commit_message_mentioning_advisory_does_not_match(self):
        assert (
            gate.matched_categories(
                "Bash",
                {
                    "command": (
                        "git commit -m 'docs: note security-advisories "
                        "workflow in README'"
                    )
                },
            )
            == []
        )

    def test_gh_repo_view_readme_mentioning_advisory_does_not_match(self):
        """A `gh` subcommand other than `api` never matches, regardless of
        what its output happens to contain — only `gh api` can actually
        reach the advisories endpoint."""
        assert (
            gate.matched_categories(
                "Bash", {"command": "gh repo view --json description,name"}
            )
            == []
        )


class TestBashMutationShapesStillInject:
    """Positive companion to `TestBashReadOnlyMentionsDoNotInject` — the
    classifier must still catch genuine mutation-capable operations against
    the same paths, or the fix would have traded false positives for false
    negatives."""

    def test_redirect_write_to_settings_json_matches(self):
        assert gate.matched_categories(
            "Bash", {"command": "echo '{}' > .claude/settings.json"}
        ) == ["harness_config"]

    def test_append_redirect_matches(self):
        assert gate.matched_categories(
            "Bash", {"command": "echo extra >> .claude/settings.local.json"}
        ) == ["harness_config"]

    def test_sed_in_place_matches(self):
        assert gate.matched_categories(
            "Bash",
            {"command": "sed -i '' 's/foo/bar/' .claude/settings.json"},
        ) == ["harness_config"]

    def test_tee_matches(self):
        assert gate.matched_categories(
            "Bash", {"command": "echo '{}' | tee .claude/settings.json"}
        ) == ["harness_config"]

    def test_cp_as_destination_matches(self):
        assert gate.matched_categories(
            "Bash", {"command": "cp settings.json.bak .claude/settings.json"}
        ) == ["harness_config"]

    def test_mv_as_destination_matches(self):
        assert gate.matched_categories(
            "Bash", {"command": "mv /tmp/mcp.json ~/.cursor/mcp.json"}
        ) == ["harness_config"]

    def test_rm_of_path_matches(self):
        assert gate.matched_categories(
            "Bash", {"command": "rm ~/.neotoma/aauth/keys.json"}
        ) == ["harness_config"]

    def test_git_checkout_of_path_matches(self):
        assert gate.matched_categories(
            "Bash", {"command": "git checkout HEAD~1 -- .claude/settings.json"}
        ) == ["harness_config"]

    def test_mutation_hidden_after_innocuous_first_segment_still_matches(self):
        """Compound commands split on `&&` — a mutation must still be caught
        even when it's the second segment (mirrors gmail_send_gate's
        equivalent coverage)."""
        assert gate.matched_categories(
            "Bash",
            {"command": "echo starting && echo '{}' > .claude/settings.json"},
        ) == ["harness_config"]

    def test_line_continuation_mutation_still_matches(self):
        assert gate.matched_categories(
            "Bash",
            {"command": "echo '{}' \\\n  > .claude/settings.json"},
        ) == ["harness_config"]


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


class TestRuleInjectionAuditEvent:
    """Effect coverage: removing `_emit_injection_audit` makes these fail red.

    The fake server captures the exact persisted `/store` request, proving
    that the action-linked record exists and contains no raw tool material.
    """

    def test_correlates_injection_to_governed_tool_call(self, fake_neotoma):
        base_url, handler = fake_neotoma
        rule_id = "ent_c4d33237ff2d12b4aaec71af"
        handler.rows = [_row(rule_id, rule="FULL_RULE_CANARY")]
        result = _run(
            {
                "session_id": "session-safe-1",
                "turn_id": "turn-safe-1",
                "tool_use_id": "call-safe-1",
                "tool_name": "Edit",
                "tool_input": {"file_path": "/tmp/.cursor/mcp.json"},
            },
            base_url=base_url,
        )

        assert result.returncode == 0, result.stderr
        events = [
            req["entities"][0]
            for req in handler.stored_requests
            if req.get("entities", [{}])[0].get("event_type") == "rule_injection"
        ]
        assert len(events) == 1
        event = events[0]
        assert event["trigger_action_classes"] == ["harness_config"]
        assert event["rule_entity_ids"] == [rule_id]
        assert event["expected_rule_entity_ids"] == [
            "ent_663888501a290e9aaf60270c",
            rule_id,
        ]
        assert event["missing_rule_entity_ids"] == [
            "ent_663888501a290e9aaf60270c"
        ]
        assert event["delivery_status"] == "partial"
        assert event["delivery_policy"] == "fail_open"
        assert event["correlation_basis"] == "tool_use_id"
        assert event["governed_call_correlation"].startswith("sha256:")
        assert "call-safe-1" not in json.dumps(event)
        assert event["injected_at"].endswith("+00:00")

    def test_never_persists_secret_pii_path_or_raw_prompt(self, fake_neotoma):
        base_url, handler = fake_neotoma
        rule_id = "ent_c4d33237ff2d12b4aaec71af"
        secret = "fixture-sensitive-value"
        private_path = f"/Users/{secret}/.cursor/mcp.json"
        handler.rows = [_row(rule_id, rule=f"RULE_BODY_{secret}")]
        result = _run(
            {
                "session_id": f"session {secret}",
                "turn_id": f"turn {secret}",
                "tool_use_id": f"call {secret}",
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": private_path,
                    "prompt": f"raw prompt {secret}",
                    "token": secret,
                },
            },
            base_url=base_url,
        )

        assert result.returncode == 0, result.stderr
        request = next(
            req
            for req in handler.stored_requests
            if req.get("entities", [{}])[0].get("event_type") == "rule_injection"
        )
        persisted = json.dumps(request, sort_keys=True)
        assert secret not in persisted
        assert private_path not in persisted
        assert "raw prompt" not in persisted
        assert "RULE_BODY" not in persisted
        event = request["entities"][0]
        assert event["correlation_basis"] == "tool_use_id"
        assert event["governed_call_correlation"].startswith("sha256:")
        assert event["session_id"].startswith("sha256:")

    def test_missing_tool_call_id_uses_turn_and_names_limitation(
        self, fake_neotoma
    ):
        base_url, handler = fake_neotoma
        handler.rows = [
            _row("ent_c4d33237ff2d12b4aaec71af", rule="FULL_RULE_CANARY")
        ]
        result = _run(
            {
                "session_id": "session-safe-2",
                "turn_id": "turn-safe-2",
                "tool_name": "Edit",
                "tool_input": {"file_path": "/tmp/.cursor/mcp.json"},
            },
            base_url=base_url,
        )
        assert result.returncode == 0, result.stderr
        event = next(
            req["entities"][0]
            for req in handler.stored_requests
            if req.get("entities", [{}])[0].get("event_type") == "rule_injection"
        )
        assert event["correlation_basis"] == "turn_id"
        assert event["governed_call_correlation"].startswith("sha256:")
        assert "turn-safe-2" not in json.dumps(event)

    def test_retrieval_failure_is_observable_and_stays_fail_open(
        self, fake_neotoma
    ):
        base_url, handler = fake_neotoma
        handler.rows = []
        result = _run(
            {
                "session_id": "session-safe-3",
                "tool_use_id": "call-safe-3",
                "tool_name": "Edit",
                "tool_input": {"file_path": "/tmp/.cursor/mcp.json"},
            },
            base_url=base_url,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ""
        event = next(
            req["entities"][0]
            for req in handler.stored_requests
            if req.get("entities", [{}])[0].get("event_type") == "rule_injection"
        )
        assert event["delivery_status"] == "retrieval_failed_or_unavailable"
        assert event["delivery_policy"] == "fail_open"
        assert event["rule_entity_ids"] == []
        assert event["missing_rule_entity_ids"] == event["expected_rule_entity_ids"]


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

    def test_bash_mutation_shape_injects_end_to_end(self, fake_neotoma):
        """Full-subprocess companion to
        `TestBashMutationShapesStillInject.test_redirect_write_to_settings_json_matches`
        — proves the real mutation shape still injects through the actual
        hook process, not just the pure matcher."""
        base_url, handler = fake_neotoma
        handler.rows = [_row("ent_c4d33237ff2d12b4aaec71af", rule="CURSOR_HTTP_MCP_CANARY")]
        result = _run(
            {"tool_name": "Bash", "tool_input": {"command": "echo '{}' > .claude/settings.json"}},
            base_url=base_url,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert "CURSOR_HTTP_MCP_CANARY" in ctx


class TestBashReadOnlyMentionDoesNotInjectEndToEnd:
    """Full-subprocess companion to `TestBashReadOnlyMentionsDoNotInject` —
    proves Accipiter's exact reproduction (a read-only `git diff` mentioning
    `.claude/settings.json`) prints nothing through the real hook process,
    even when Neotoma has a mapped rule row ready to serve. Fails red at
    42fdf432 (prints the full rule body); passes green after the fix."""

    def test_git_diff_mentioning_path_prints_nothing(self, fake_neotoma):
        base_url, handler = fake_neotoma
        handler.rows = [_row("ent_c4d33237ff2d12b4aaec71af", rule="CURSOR_HTTP_MCP_CANARY")]
        result = _run(
            {
                "tool_name": "Bash",
                "tool_input": {
                    "command": "git diff HEAD~1 --stat -- .claude/settings.json"
                },
            },
            base_url=base_url,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ""

    def test_cat_of_path_prints_nothing(self, fake_neotoma):
        base_url, handler = fake_neotoma
        handler.rows = [_row("ent_c4d33237ff2d12b4aaec71af", rule="CURSOR_HTTP_MCP_CANARY")]
        result = _run(
            {"tool_name": "Bash", "tool_input": {"command": "cat .claude/settings.json"}},
            base_url=base_url,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ""


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


class TestGovernsEdgeToAnotherAgentIsWithheld:
    """The live QA finding at head 42fdf432: a `scope="global"`/`"swarm"`
    row that carries a real `GOVERNS` edge to a DIFFERENT agent must be
    withheld from this session, because an edge overrides scope
    (`policy_binds_agent_by_edge`'s own doc: "an edge is more specific than
    a scope value, so it overrides rather than adds to it"). The old
    `_fetch_rows_by_id` called the bare `_session_scope_ok(row)` with no
    `agent_definition_id`/`governs`, so it never learned the edge existed
    and fell through to the swarm-wide `scope` branch, wrongly injecting a
    row meant only for another agent. `render_skills` resolves this
    session's own identity (`ATELES_SESSION_PRINCIPAL`, default
    `ateles@ateles-swarm`) and fetches the live edge map before scoping —
    exercised here via the path-aware fake server so both the
    `agent_definition` resolve query and the `/list_relationships` GOVERNS
    fetch are real network round-trips, not skipped.
    """

    def test_global_scoped_row_edged_only_to_another_agent_is_withheld(
        self, edge_aware_fake_neotoma
    ):
        base_url, handler = edge_aware_fake_neotoma
        handler.rows = [
            _row(
                "ent_1c0cbb99d2c8011358ff1dc3",
                rule="SHOULD_NOT_LEAK_TO_ATELES_CANARY",
                scope="global",
            )
        ]
        # This session's own agent_definition (resolved by name from the
        # default session principal "ateles@ateles-swarm").
        handler.agent_definition_rows = [
            {"entity_id": _ATELES_DEFINITION_ID, "snapshot": {"name": "ateles"}}
        ]
        # The row is edged ONLY to a different agent (cicada), never to
        # ateles's own agent_definition id.
        handler.governs_relationships = [
            {
                "source_entity_id": "ent_1c0cbb99d2c8011358ff1dc3",
                "target_entity_id": _CICADA_DEFINITION_ID,
            }
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
        # policy_write has no probe reminder, so a withheld row means
        # nothing was printed at all — same shape as the edgeless case.
        assert "SHOULD_NOT_LEAK_TO_ATELES_CANARY" not in result.stdout

    def test_global_scoped_row_edged_to_this_sessions_own_agent_is_injected(
        self, edge_aware_fake_neotoma
    ):
        """Companion positive case: a row edged to THIS session's own
        resolved `agent_definition` id must still render — proving the fix
        discriminates by resolved identity rather than simply withholding
        every edged row."""
        base_url, handler = edge_aware_fake_neotoma
        handler.rows = [
            _row(
                "ent_1c0cbb99d2c8011358ff1dc3",
                rule="SHOULD_REACH_ATELES_CANARY",
                scope="global",
            )
        ]
        handler.agent_definition_rows = [
            {"entity_id": _ATELES_DEFINITION_ID, "snapshot": {"name": "ateles"}}
        ]
        handler.governs_relationships = [
            {
                "source_entity_id": "ent_1c0cbb99d2c8011358ff1dc3",
                "target_entity_id": _ATELES_DEFINITION_ID,
            }
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
        payload = json.loads(result.stdout)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert "SHOULD_REACH_ATELES_CANARY" in ctx


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
