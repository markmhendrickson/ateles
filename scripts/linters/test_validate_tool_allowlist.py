"""
Unit tests for validate_tool_allowlist.py (ateles#255).

Covers the edge-case table from the issue's QA section: reintroduced bash:
grants are caught with an actionable message, legitimate Bash(...)/MCP/bare
grants never false-positive, empty allowlists don't crash, and a malformed
grant with no recognizable prefix is flagged as a distinct warning class
rather than silently accepted.

Run with: pytest scripts/linters/test_validate_tool_allowlist.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "validate_tool_allowlist.py"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate_tool_allowlist as lint  # noqa: E402


def _run_fixture(tmp_path: Path, agents: list[dict]) -> subprocess.CompletedProcess:
    fixture_path = tmp_path / "agents.json"
    fixture_path.write_text(json.dumps(agents))
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--fixture", str(fixture_path)],
        capture_output=True,
        text=True,
    )


class TestCheckAgent:
    """Unit-level checks against the pure check_agent() function."""

    def test_bash_prefix_flagged_with_agent_rule_and_fix(self) -> None:
        blocking, warnings = lint.check_agent("vanellus", ["bash:gh pr*"])
        assert len(blocking) == 1
        assert "vanellus" in blocking[0]
        assert "bash:gh pr*" in blocking[0]
        assert "Bash(gh pr*:*)" in blocking[0]
        assert not warnings

    def test_legitimate_bash_scoped_grant_passes(self) -> None:
        blocking, warnings = lint.check_agent(
            "cicada", ["Bash(git:*)", "Bash(gh pr:*)"]
        )
        assert not blocking
        assert not warnings

    def test_non_bash_tool_grants_pass(self) -> None:
        blocking, warnings = lint.check_agent(
            "robin", ["mcp__mcpsrv_neotoma__store", "Read", "Edit"]
        )
        assert not blocking
        assert not warnings

    def test_empty_allowlist_does_not_crash(self) -> None:
        blocking, warnings = lint.check_agent("empty_agent", [])
        assert blocking == []
        assert warnings == []

    def test_none_allowlist_does_not_crash(self) -> None:
        blocking, warnings = lint.check_agent("no_allowlist_agent", None)
        assert blocking == []
        assert warnings == []

    def test_malformed_grant_no_prefix_flagged_as_distinct_warning(self) -> None:
        blocking, warnings = lint.check_agent("weird_agent", ["gh pr*"])
        assert not blocking, (
            "a missing-prefix grant is a warning, not a blocking failure"
        )
        assert len(warnings) == 1
        assert "gh pr*" in warnings[0]
        assert "not a recognized grant form" in warnings[0]

    def test_wildcard_grant_passes(self) -> None:
        blocking, warnings = lint.check_agent("ateles", ["*"])
        assert not blocking
        assert not warnings

    def test_snake_case_capability_slot_passes(self) -> None:
        blocking, warnings = lint.check_agent(
            "sylvia", ["gws_calendar", "telegram_notify"]
        )
        assert not blocking
        assert not warnings

    def test_hyphenated_mcp_server_name_passes(self) -> None:
        blocking, warnings = lint.check_agent(
            "accipiter", ["mcp__computer-use__screenshot"]
        )
        assert not blocking
        assert not warnings


class TestMissingNeotomaCredentials:
    """No NEOTOMA_BEARER_TOKEN configured (e.g. a fork, or this repo today —
    the secret isn't provisioned) must skip cleanly, not fail the build on
    infrastructure the PR under test didn't touch."""

    def test_fetch_returns_none_without_token(self, monkeypatch, tmp_path) -> None:
        # Sandbox HOME so a real ~/.config/neotoma/.env on the machine running
        # this test can't leak a live token in through _load_env()'s fallback.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
        monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.invalid")
        assert lint.fetch_agents_from_neotoma() is None

    def test_fetch_returns_none_without_base_url(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
        monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "some-token")
        assert lint.fetch_agents_from_neotoma() is None

    def test_cli_skips_without_token_or_fixture(self, monkeypatch, tmp_path) -> None:
        env = dict(os.environ)
        env["HOME"] = str(tmp_path)
        env.pop("NEOTOMA_BEARER_TOKEN", None)
        env["NEOTOMA_BASE_URL"] = "https://neotoma.example.invalid"
        result = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "SKIP" in result.stdout


class TestFixtureCLI:
    """End-to-end CLI checks driven through --fixture (subprocess), matching
    the QA table's fixture-file cases exactly."""

    def test_reintroduced_bash_grant_fails_and_names_agent_and_fix(
        self, tmp_path
    ) -> None:
        result = _run_fixture(
            tmp_path, [{"name": "vanellus", "tool_allowlist": ["bash:gh pr*"]}]
        )
        assert result.returncode != 0
        assert "vanellus" in result.stdout
        assert "bash:gh pr*" in result.stdout
        assert "Bash(gh pr*:*)" in result.stdout

    def test_all_nine_corrected_agents_pass(self, tmp_path) -> None:
        agents = [
            {
                "name": "cicada",
                "tool_allowlist": [
                    "Bash(gh pr:*)",
                    "Bash(gh issue:*)",
                    "Bash(git:*)",
                    "Bash",
                ],
            },
            {"name": "vanellus", "tool_allowlist": ["Bash(gh pr:*)", "Bash(gh api:*)"]},
            {
                "name": "corvus",
                "tool_allowlist": [
                    "Bash(scripts/sync_posts_to_neotoma.py:*)",
                    "Bash(scripts/generate_cover_image.py:*)",
                ],
            },
            {"name": "robin", "tool_allowlist": ["Bash(rg:*)"]},
            {
                "name": "phoenicurus",
                "tool_allowlist": [
                    "Bash(pytest:*)",
                    "Bash(npm test:*)",
                    "Bash(npm run eval:tier1:*)",
                    "Bash(gh pr checks:*)",
                ],
            },
            {
                "name": "pavo",
                "tool_allowlist": ["Bash(gh issue list:*)", "Bash(gh pr list:*)"],
            },
            {
                "name": "struthio",
                "tool_allowlist": [
                    "Bash(gh release create:*)",
                    "Bash(gh workflow run:*)",
                    "Bash(git tag:*)",
                ],
            },
            {"name": "waxwing", "tool_allowlist": ["Bash(rg:*)", "Bash(gh:*)"]},
            {"name": "regulus", "tool_allowlist": ["Bash(gh repo view:*)"]},
        ]
        result = _run_fixture(tmp_path, agents)
        assert result.returncode == 0, result.stdout
        assert "OK" in result.stdout

    def test_legitimate_bash_grants_only_zero_findings(self, tmp_path) -> None:
        result = _run_fixture(
            tmp_path,
            [{"name": "cicada", "tool_allowlist": ["Bash(git:*)", "Bash(gh pr:*)"]}],
        )
        assert result.returncode == 0
        assert "FAIL" not in result.stdout
        assert "WARN" not in result.stdout

    def test_non_bash_tool_grants_zero_findings(self, tmp_path) -> None:
        result = _run_fixture(
            tmp_path,
            [
                {
                    "name": "robin",
                    "tool_allowlist": ["mcp__mcpsrv_neotoma__store", "Read", "Edit"],
                }
            ],
        )
        assert result.returncode == 0
        assert "FAIL" not in result.stdout
        assert "WARN" not in result.stdout

    def test_empty_tool_allowlist_passes_without_crash(self, tmp_path) -> None:
        result = _run_fixture(tmp_path, [{"name": "quiet_agent", "tool_allowlist": []}])
        assert result.returncode == 0
        assert "OK" in result.stdout

    def test_malformed_grant_no_prefix_flagged_not_silently_valid(
        self, tmp_path
    ) -> None:
        result = _run_fixture(
            tmp_path, [{"name": "weird_agent", "tool_allowlist": ["gh pr*"]}]
        )
        # Exit 0 (not blocking) but the warning must be visible in output.
        assert result.returncode == 0
        assert "WARN" in result.stdout
        assert "gh pr*" in result.stdout


class TestUnreachableNeotomaDegradesToSkip:
    """Live (non-fixture) path: Neotoma unreachable/unauthorized must SKIP with
    exit 0, not fail the build — this is an infra gap (missing/invalid
    NEOTOMA_BEARER_TOKEN in the running environment), not a grant-grammar
    defect. Regression test for the CI failure where this lint step hard-
    failed with 'Neotoma unreachable after 5 tries: HTTP Error 401:
    Unauthorized' because NEOTOMA_BEARER_TOKEN was unset in GitHub Actions."""

    def test_live_path_skips_cleanly_when_neotoma_unauthorized(self, tmp_path) -> None:
        env = dict(os.environ)
        env["NEOTOMA_BASE_URL"] = "https://neotoma.markmhendrickson.com"
        env["NEOTOMA_BEARER_TOKEN"] = "invalid-token-for-regression-test"
        env["HOME"] = str(tmp_path)  # no ~/.config/neotoma/.env fallback
        result = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "SKIP" in result.stdout
        assert "FAIL" not in result.stdout
