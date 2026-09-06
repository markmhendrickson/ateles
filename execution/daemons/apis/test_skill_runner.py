"""
Unit tests for skill_runner.py — Stages 1, 2, 5 of ateles#94.

Tests are fully synchronous / mock-based:
  - AgentLoader.load() is monkeypatched to return a fake AgentDefinition
  - _write_harness_event is patched so no real Neotoma calls happen
  - No `claude` subprocess is spawned

Run with: pytest execution/daemons/apis/test_skill_runner.py -v
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Path bootstrap (mirrors conftest.py) ──────────────────────────────────────
_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib.daemon_runtime import AgentDefinition  # noqa: E402

# Import module-level objects so we can patch them in-place
import skill_runner  # noqa: E402
import harness_router  # noqa: E402


# ── Helpers ────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _claude_only_test_router(monkeypatch, tmp_path):
    """Keep legacy command-shape tests pinned to the Claude adapter."""
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "claude")
    monkeypatch.setenv("APIS_HARNESS_HEADROOM", '{"claude": 1.0}')
    monkeypatch.setenv(
        "APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "missing-headroom.json")
    )
    monkeypatch.delenv("APIS_ALLOW_METERED_HARNESS", raising=False)
    harness_router.reset_state()
    yield
    harness_router.reset_state()


def _make_def(
    *,
    prompt_markdown: str = "You are Gryllus, an issue worker.",
    tool_allowlist: str = "*",
    aauth_sub: str = "gryllus@ateles-swarm",
    name: str = "gryllus",
) -> AgentDefinition:
    return AgentDefinition(
        entity_id="ent_test123",
        name=name,
        prompt_markdown=prompt_markdown,
        tool_allowlist=tool_allowlist,
        aauth_sub=aauth_sub,
    )


def _stub_def(name: str = "gryllus") -> AgentDefinition:
    """Stub: empty prompt_markdown — simulates missing/unreachable definition."""
    return AgentDefinition(
        name=name,
        aauth_sub=f"{name}@ateles-swarm",
        tool_allowlist="*",
    )


# ── build_system_prompt ────────────────────────────────────────────────────────


class TestBuildSystemPrompt:
    def test_definition_prepended_to_skill_md(self) -> None:
        agent_def = _make_def(prompt_markdown="Agent identity block.")
        skill_md = "Do the task."
        prompt, degraded = skill_runner.build_system_prompt(agent_def, skill_md)
        assert not degraded
        assert "Agent identity block." in prompt
        assert "Do the task." in prompt
        # Identity block must come first
        assert prompt.index("Agent identity block.") < prompt.index("Do the task.")

    def test_separator_present_between_layers(self) -> None:
        agent_def = _make_def(prompt_markdown="Identity.")
        skill_md = "Task instructions."
        prompt, _ = skill_runner.build_system_prompt(agent_def, skill_md)
        assert "---" in prompt

    def test_empty_prompt_markdown_returns_skill_md_only(self) -> None:
        agent_def = _stub_def()
        skill_md = "Fallback instructions."
        prompt, degraded = skill_runner.build_system_prompt(agent_def, skill_md)
        assert degraded
        assert prompt == skill_md

    def test_whitespace_only_prompt_markdown_treated_as_empty(self) -> None:
        agent_def = _make_def(prompt_markdown="   \n\n  ")
        skill_md = "Task instructions."
        prompt, degraded = skill_runner.build_system_prompt(agent_def, skill_md)
        assert degraded
        assert prompt == skill_md


# ── _load_agent_def caching ────────────────────────────────────────────────────


class TestAgentDefCache:
    def setup_method(self) -> None:
        # Clear the module-level cache before each test
        skill_runner._agent_def_cache.clear()

    def test_cache_populated_on_first_load(self) -> None:
        fake_def = _make_def(name="monedula")
        with patch("skill_runner.AgentLoader") as MockLoader:
            instance = MagicMock()
            instance.load.return_value = fake_def
            MockLoader.return_value = instance

            result = skill_runner._load_agent_def("monedula")
            assert result is fake_def
            assert MockLoader.call_count == 1

    def test_second_call_uses_cache(self) -> None:
        fake_def = _make_def(name="monedula")
        with patch("skill_runner.AgentLoader") as MockLoader:
            instance = MagicMock()
            instance.load.return_value = fake_def
            MockLoader.return_value = instance

            skill_runner._load_agent_def("monedula")
            skill_runner._load_agent_def("monedula")
            # AgentLoader should only have been instantiated once
            assert MockLoader.call_count == 1


# ── run_skill — full integration (mocked subprocess + Neotoma) ────────────────


class TestRunSkill:
    """
    Tests for the main run_skill coroutine.
    We mock:
      - skill_runner.CLAUDE_BIN so it appears available
      - AgentLoader so it returns a controlled AgentDefinition
      - The skill SKILL.md path so reads succeed without disk
      - _write_harness_event so no Neotoma calls happen
      - asyncio.create_subprocess_exec so no real subprocess runs
    """

    def setup_method(self) -> None:
        skill_runner._agent_def_cache.clear()

    def _run(self, coro):
        return asyncio.run(coro)

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_composite_prompt_when_definition_loaded(
        self, MockLoader, mock_write_harness
    ) -> None:
        """When agent_definition has prompt_markdown, the spawned system prompt
        must contain BOTH the definition text and the SKILL.md text."""
        fake_def = _make_def(prompt_markdown="Role: Gryllus. You are an issue worker.")
        instance = MagicMock()
        instance.load.return_value = fake_def
        MockLoader.return_value = instance

        captured_cmd: list = []

        async def fake_exec(*cmd, **kwargs):
            captured_cmd.extend(cmd)
            proc = MagicMock()
            proc.returncode = 0

            async def _communicate(input=None):
                return b"output", b""

            proc.communicate = _communicate
            return proc

        skill_md_content = "Do the issue task now."
        with (
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=skill_md_content),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            result = self._run(
                skill_runner.run_skill(
                    "gryllus",
                    "work prompt",
                    role="gryllus",
                    task_entity_id="ent_abc",
                )
            )

        assert result.ok
        # The --append-system-prompt argument should contain BOTH texts
        sys_prompt_idx = captured_cmd.index("--append-system-prompt") + 1
        system_prompt_arg = captured_cmd[sys_prompt_idx]
        assert "Role: Gryllus" in system_prompt_arg
        assert skill_md_content in system_prompt_arg

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_skill_md_only_when_no_definition(
        self, MockLoader, mock_write_harness
    ) -> None:
        """When prompt_markdown is empty, the system prompt is SKILL.md alone."""
        stub = _stub_def()
        instance = MagicMock()
        instance.load.return_value = stub
        MockLoader.return_value = instance

        captured_cmd: list = []

        async def fake_exec(*cmd, **kwargs):
            captured_cmd.extend(cmd)
            proc = MagicMock()
            proc.returncode = 0

            async def _communicate(input=None):
                return b"output", b""

            proc.communicate = _communicate
            return proc

        skill_md_content = "Fallback skill instructions."
        with (
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=skill_md_content),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            result = self._run(
                skill_runner.run_skill(
                    "gryllus",
                    "work prompt",
                    role="gryllus",
                    task_entity_id="ent_abc",
                )
            )

        assert result.ok
        sys_prompt_idx = captured_cmd.index("--append-system-prompt") + 1
        system_prompt_arg = captured_cmd[sys_prompt_idx]
        assert system_prompt_arg == skill_md_content

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_degraded_produces_harness_event_with_marker(
        self, MockLoader, mock_write_harness
    ) -> None:
        """Empty prompt_markdown must produce a harness_event with
        output_summary containing 'degraded_generic_subagent'."""
        stub = _stub_def()
        instance = MagicMock()
        instance.load.return_value = stub
        MockLoader.return_value = instance

        async def fake_exec(*cmd, **kwargs):
            proc = MagicMock()
            proc.returncode = 0

            async def _communicate(input=None):
                return b"output", b""

            proc.communicate = _communicate
            return proc

        with (
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value="skill content"),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            self._run(
                skill_runner.run_skill(
                    "gryllus",
                    "work prompt",
                    role="gryllus",
                    task_entity_id="ent_abc",
                )
            )

        # Find the degraded harness_event call
        degraded_calls = [
            call
            for call in mock_write_harness.call_args_list
            if "degraded_generic_subagent" in (call.kwargs.get("output_summary") or "")
        ]
        assert len(degraded_calls) >= 1, (
            "Expected at least one harness_event with output_summary='degraded_generic_subagent'"
        )

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_tool_allowlist_applied_when_restricted(
        self, MockLoader, mock_write_harness
    ) -> None:
        """When tool_allowlist is restricted (not '*'), --allowed-tools must appear in the command."""
        restricted_def = _make_def(
            prompt_markdown="Restricted agent.",
            tool_allowlist="Bash,Read,Write",
        )
        instance = MagicMock()
        instance.load.return_value = restricted_def
        MockLoader.return_value = instance

        captured_cmd: list = []

        async def fake_exec(*cmd, **kwargs):
            captured_cmd.extend(cmd)
            proc = MagicMock()
            proc.returncode = 0

            async def _communicate(input=None):
                return b"output", b""

            proc.communicate = _communicate
            return proc

        with (
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value="skill md"),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            result = self._run(
                skill_runner.run_skill(
                    "gryllus",
                    "work prompt",
                    role="gryllus",
                    task_entity_id="ent_abc",
                )
            )

        assert result.ok
        assert "--allowed-tools" in captured_cmd
        tools_idx = captured_cmd.index("--allowed-tools") + 1
        assert "Bash" in captured_cmd[tools_idx]
        assert "Read" in captured_cmd[tools_idx]

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_wildcard_allowlist_omits_allowed_tools_flag(
        self, MockLoader, mock_write_harness
    ) -> None:
        """When tool_allowlist is '*', --allowed-tools must NOT appear."""
        wide_def = _make_def(prompt_markdown="Full-tool agent.", tool_allowlist="*")
        instance = MagicMock()
        instance.load.return_value = wide_def
        MockLoader.return_value = instance

        captured_cmd: list = []

        async def fake_exec(*cmd, **kwargs):
            captured_cmd.extend(cmd)
            proc = MagicMock()
            proc.returncode = 0

            async def _communicate(input=None):
                return b"output", b""

            proc.communicate = _communicate
            return proc

        with (
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value="skill md"),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            self._run(
                skill_runner.run_skill(
                    "gryllus",
                    "work prompt",
                    role="gryllus",
                    task_entity_id="ent_abc",
                )
            )

        assert "--allowed-tools" not in captured_cmd

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_harness_events_written_on_success(
        self, MockLoader, mock_write_harness
    ) -> None:
        """A successful dispatch must produce at least start + completion harness_events."""
        full_def = _make_def()
        instance = MagicMock()
        instance.load.return_value = full_def
        MockLoader.return_value = instance

        async def fake_exec(*cmd, **kwargs):
            proc = MagicMock()
            proc.returncode = 0

            async def _communicate(input=None):
                return b"123B output", b""

            proc.communicate = _communicate
            return proc

        with (
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value="skill md"),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            result = self._run(
                skill_runner.run_skill(
                    "gryllus",
                    "work prompt",
                    role="gryllus",
                    task_entity_id="ent_test",
                )
            )

        assert result.ok
        # At least 2 calls: start (partial) + completion (true)
        success_calls = [
            c
            for c in mock_write_harness.call_args_list
            if c.kwargs.get("success") == "true"
        ]
        assert len(success_calls) >= 1

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_harness_event_on_failure(self, MockLoader, mock_write_harness) -> None:
        """A failing dispatch (non-zero rc) must produce a harness_event with success='false'."""
        full_def = _make_def()
        instance = MagicMock()
        instance.load.return_value = full_def
        MockLoader.return_value = instance

        async def fake_exec(*cmd, **kwargs):
            proc = MagicMock()
            proc.returncode = 1

            async def _communicate(input=None):
                return b"", b"something went wrong"

            proc.communicate = _communicate
            return proc

        with (
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value="skill md"),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            result = self._run(
                skill_runner.run_skill(
                    "gryllus",
                    "work prompt",
                    role="gryllus",
                    task_entity_id="ent_test",
                )
            )

        assert not result.ok
        fail_calls = [
            c
            for c in mock_write_harness.call_args_list
            if c.kwargs.get("success") == "false"
        ]
        assert len(fail_calls) >= 1

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_harness_event_failure_does_not_crash_dispatch(
        self, MockLoader, mock_write_harness
    ) -> None:
        """A harness_event write failure must not propagate and crash the dispatch."""
        full_def = _make_def()
        instance = MagicMock()
        instance.load.return_value = full_def
        MockLoader.return_value = instance

        mock_write_harness.side_effect = RuntimeError("Neotoma unreachable")

        async def fake_exec(*cmd, **kwargs):
            proc = MagicMock()
            proc.returncode = 0

            async def _communicate(input=None):
                return b"output", b""

            proc.communicate = _communicate
            return proc

        with (
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value="skill md"),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            # Must not raise despite harness write failures
            result = self._run(
                skill_runner.run_skill(
                    "gryllus",
                    "work prompt",
                    role="gryllus",
                    task_entity_id="ent_test",
                )
            )

        assert result.ok


# ── resolve_role (routing.py) ─────────────────────────────────────────────────


class TestResolveRole:
    """resolve_role should mirror resolve_skill in all cases."""

    def test_resolve_role_matches_resolve_skill(self) -> None:
        from routing import resolve_role, resolve_skill

        cases = [
            (["health"], None),
            (["finance"], None),
            (["ops"], None),
            (["health"], "monedula"),
            ([], "gorilla"),
            ([], None),
        ]
        for tags, assigned_to in cases:
            assert resolve_role(tags, assigned_to=assigned_to) == resolve_skill(
                tags, assigned_to=assigned_to
            ), f"resolve_role({tags!r}, {assigned_to!r}) != resolve_skill(...)"

    def test_resolve_role_returns_expected_roles(self) -> None:
        from routing import resolve_role

        assert resolve_role(["health"]) == "gorilla"
        assert resolve_role(["finance"]) == "monedula"
        assert resolve_role(["ops"]) == "cicada"
        assert resolve_role(["agents"]) == "cicada"
        assert resolve_role([], assigned_to="fringilla") == "fringilla"
        assert resolve_role([], assigned_to="sturnus") == "sturnus"
        assert resolve_role([]) is None

    def test_resolve_role_assigned_to_wins(self) -> None:
        from routing import resolve_role

        assert resolve_role(["health"], assigned_to="monedula") == "monedula"

    def test_resolve_role_apis_self_falls_back_to_tags(self) -> None:
        from routing import resolve_role

        assert resolve_role(["finance"], assigned_to="apis") == "monedula"


# ── Stage 3: role-signing env injection (ateles#94) ───────────────────────────


class TestRoleSigningEnvInjection:
    """Stage 3 of ateles#94: when a real agent_def is loaded and the role JWK
    file exists, subprocess_env must carry the three Neotoma AAuth client signer
    vars (NEOTOMA_AAUTH_PRIVATE_JWK_PATH, NEOTOMA_AAUTH_SUB, NEOTOMA_AAUTH_ISS).
    When the JWK file is absent or the agent_def is degraded, none are injected."""

    def setup_method(self) -> None:
        skill_runner._agent_def_cache.clear()

    def _run(self, coro):
        return asyncio.run(coro)

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_real_def_with_jwk_injects_signer_vars(
        self, MockLoader, mock_write_harness, monkeypatch
    ) -> None:
        """When agent_def is real, keys_dir is set, and the JWK file exists:
        subprocess_env must contain NEOTOMA_AAUTH_PRIVATE_JWK_PATH (correct path),
        NEOTOMA_AAUTH_SUB (== agent_def.aauth_sub), NEOTOMA_AAUTH_ISS (default),
        and must NOT contain NEOTOMA_AAUTH_ROLE."""
        fake_def = _make_def(
            prompt_markdown="Role: Gryllus.", aauth_sub="gryllus@ateles-swarm"
        )
        instance = MagicMock()
        instance.load.return_value = fake_def
        MockLoader.return_value = instance

        captured_env: dict = {}

        async def fake_exec(*cmd, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            proc = MagicMock()
            proc.returncode = 0

            async def _communicate(input=None):
                return b"output", b""

            proc.communicate = _communicate
            return proc

        monkeypatch.setenv("ATELES_PRIVATE_KEYS_DIR", "/secrets/keys")

        with (
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value="skill md"),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch("os.path.exists", return_value=True),
        ):
            self._run(
                skill_runner.run_skill(
                    "gryllus",
                    "work prompt",
                    role="gryllus",
                    task_entity_id="ent_abc",
                )
            )

        assert (
            captured_env.get("NEOTOMA_AAUTH_PRIVATE_JWK_PATH")
            == "/secrets/keys/gryllus.jwk.json"
        ), "Expected NEOTOMA_AAUTH_PRIVATE_JWK_PATH='/secrets/keys/gryllus.jwk.json'"
        assert captured_env.get("NEOTOMA_AAUTH_SUB") == "gryllus@ateles-swarm", (
            "Expected NEOTOMA_AAUTH_SUB='gryllus@ateles-swarm' (agent_def.aauth_sub)"
        )
        assert (
            captured_env.get("NEOTOMA_AAUTH_ISS") == "https://markmhendrickson.com"
        ), "Expected NEOTOMA_AAUTH_ISS default 'https://markmhendrickson.com'"
        assert "NEOTOMA_AAUTH_ROLE" not in captured_env, (
            "NEOTOMA_AAUTH_ROLE must not be present — it is superseded by the real signer vars"
        )

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_real_def_jwk_absent_does_not_inject_signer_vars(
        self, MockLoader, mock_write_harness, monkeypatch
    ) -> None:
        """When the JWK file does not exist at <keys_dir>/<role>.jwk.json,
        none of the three signer vars should be injected."""
        fake_def = _make_def(
            prompt_markdown="Role: Gryllus.", aauth_sub="gryllus@ateles-swarm"
        )
        instance = MagicMock()
        instance.load.return_value = fake_def
        MockLoader.return_value = instance

        captured_env: dict = {}

        async def fake_exec(*cmd, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            proc = MagicMock()
            proc.returncode = 0

            async def _communicate(input=None):
                return b"output", b""

            proc.communicate = _communicate
            return proc

        monkeypatch.setenv("ATELES_PRIVATE_KEYS_DIR", "/secrets/keys")

        with (
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value="skill md"),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch("os.path.exists", return_value=False),
        ):
            self._run(
                skill_runner.run_skill(
                    "gryllus",
                    "work prompt",
                    role="gryllus",
                    task_entity_id="ent_abc",
                )
            )

        assert "NEOTOMA_AAUTH_PRIVATE_JWK_PATH" not in captured_env, (
            "NEOTOMA_AAUTH_PRIVATE_JWK_PATH must not be injected when JWK file is absent"
        )
        assert "NEOTOMA_AAUTH_SUB" not in captured_env, (
            "NEOTOMA_AAUTH_SUB must not be injected when JWK file is absent"
        )
        assert "NEOTOMA_AAUTH_ISS" not in captured_env, (
            "NEOTOMA_AAUTH_ISS must not be injected when JWK file is absent"
        )

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_degraded_def_does_not_inject_signer_vars(
        self, MockLoader, mock_write_harness, monkeypatch
    ) -> None:
        """When agent_def is degraded (empty prompt_markdown), none of the signer
        vars should be injected regardless of whether the JWK file exists."""
        stub = _stub_def()
        instance = MagicMock()
        instance.load.return_value = stub
        MockLoader.return_value = instance

        captured_env: dict = {}

        async def fake_exec(*cmd, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            proc = MagicMock()
            proc.returncode = 0

            async def _communicate(input=None):
                return b"output", b""

            proc.communicate = _communicate
            return proc

        monkeypatch.setenv("ATELES_PRIVATE_KEYS_DIR", "/secrets/keys")

        with (
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value="skill md"),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch("os.path.exists", return_value=True),
        ):
            self._run(
                skill_runner.run_skill(
                    "gryllus",
                    "work prompt",
                    role="gryllus",
                    task_entity_id="ent_abc",
                )
            )

        assert "NEOTOMA_AAUTH_PRIVATE_JWK_PATH" not in captured_env, (
            "NEOTOMA_AAUTH_PRIVATE_JWK_PATH must not be injected when agent_def is degraded"
        )
        assert "NEOTOMA_AAUTH_SUB" not in captured_env, (
            "NEOTOMA_AAUTH_SUB must not be injected when agent_def is degraded"
        )
        assert "NEOTOMA_AAUTH_ISS" not in captured_env, (
            "NEOTOMA_AAUTH_ISS must not be injected when agent_def is degraded"
        )
        assert "NEOTOMA_AAUTH_ROLE" not in captured_env, (
            "NEOTOMA_AAUTH_ROLE must not be injected (it is superseded and was never real)"
        )


# ── Stage 6: Neotoma MCP config injection (ateles#1687) ──────────────────────


class TestNeotomaMcpConfigInjection:
    """Stage 6 of ateles#94: run_skill must inject --mcp-config pointing the
    dispatched child at the local Neotoma HTTP MCP endpoint so role agents
    (Lanius/Pavo) can load workflow_definition, init gate_status, and store
    plan_contribution without requiring the ambient Claude MCP config.

    MCP tool allowlist syntax finding:
      The --allowed-tools flag accepts "mcp__<servername>__*" as a wildcard that
      permits all tools from the named MCP server. The server name must exactly
      match the key in mcpServers (here: "mcpsrv_neotoma" — the convention used
      across all 31 agent SKILLs and 24 agent_definitions). So for a restricted
      tool list, we append "mcp__mcpsrv_neotoma__*" to allow all neotoma MCP tools.

    Security:
      The bearer token is written to a mode-0600 temp file; the file path (not
      the token) is passed to --mcp-config to avoid argv exposure via `ps`.
      The temp file is cleaned up in a try/finally after the subprocess exits.
    """

    def setup_method(self) -> None:
        skill_runner._agent_def_cache.clear()

    def _run(self, coro):
        return asyncio.run(coro)

    def _make_exec_capturer(self, captured_cmd: list, returncode: int = 0):
        async def fake_exec(*cmd, **kwargs):
            captured_cmd.extend(cmd)
            proc = MagicMock()
            proc.returncode = returncode

            async def _communicate(input=None):
                return b"output", b""

            proc.communicate = _communicate
            return proc

        return fake_exec

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_mcp_config_injected_with_token(
        self, MockLoader, mock_write_harness, monkeypatch
    ) -> None:
        """When NEOTOMA_BASE_URL and NEOTOMA_BEARER_TOKEN are set, the spawned
        command must include --mcp-config, and the config file must contain the
        neotoma http server pointing at <base>/mcp with the Authorization header."""
        fake_def = _make_def(prompt_markdown="Role: Gryllus.", tool_allowlist="*")
        instance = MagicMock()
        instance.load.return_value = fake_def
        MockLoader.return_value = instance

        monkeypatch.setenv("NEOTOMA_BASE_URL", "http://localhost:9180")
        monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "test-bearer-xyz")

        captured_cmd: list = []

        with (
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value="skill md"),
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=self._make_exec_capturer(captured_cmd),
            ),
            patch("os.path.exists", return_value=False),  # no JWK file
        ):
            result = self._run(
                skill_runner.run_skill(
                    "gryllus", "work prompt", role="gryllus", task_entity_id="ent_abc"
                )
            )

        assert result.ok
        assert "--mcp-config" in captured_cmd, (
            "Expected --mcp-config in spawned command"
        )
        mcp_idx = captured_cmd.index("--mcp-config") + 1
        mcp_file = captured_cmd[mcp_idx]
        # Temp file is cleaned up after subprocess; we check content was correct by
        # verifying the path was passed and reading it during the call is not feasible
        # post-cleanup. Instead, verify the path was a string (not inline JSON).
        assert isinstance(mcp_file, str), "Expected file path string for --mcp-config"
        assert not mcp_file.startswith("{"), (
            "Expected a file path, not inline JSON (security: avoid argv exposure)"
        )

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_mcp_config_no_auth_header_without_token(
        self, MockLoader, mock_write_harness, monkeypatch
    ) -> None:
        """When NEOTOMA_BEARER_TOKEN is absent/empty, --mcp-config is still injected
        but the config must omit the Authorization header (local dev-mode Neotoma
        accepts no-bearer).

        Strategy: intercept tempfile.mkstemp so we get the path, read the content
        immediately after the fd is opened and written (before cleanup), then verify.
        We do NOT patch os.path.exists here so skill_runner can stat the real temp
        file — only Path.exists is patched (for the SKILL.md check).
        """
        import json as _json
        import tempfile as _tempfile

        fake_def = _make_def(prompt_markdown="Role: Gryllus.", tool_allowlist="*")
        instance = MagicMock()
        instance.load.return_value = fake_def
        MockLoader.return_value = instance

        monkeypatch.setenv("NEOTOMA_BASE_URL", "http://localhost:9180")
        monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)

        captured_cmd: list = []
        written_contents: list[dict] = []

        # Intercept mkstemp to record the path; also wrap os.fdopen to capture content.
        _real_mkstemp = _tempfile.mkstemp
        captured_paths: list[str] = []

        def _capturing_mkstemp(**kwargs):
            fd, path = _real_mkstemp(**kwargs)
            captured_paths.append(path)
            return fd, path

        async def fake_exec(*cmd, **kwargs):
            captured_cmd.extend(cmd)
            proc = MagicMock()
            proc.returncode = 0

            async def _communicate(input=None):
                # Read the file content from the known path while proc is "running".
                # os.path.exists is NOT patched so the real file is accessible.
                if captured_paths:
                    import os as _real_os

                    fpath = captured_paths[-1]
                    if _real_os.path.isfile(fpath):
                        with open(fpath) as f:
                            written_contents.append(_json.load(f))
                return b"output", b""

            proc.communicate = _communicate
            return proc

        with (
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value="skill md"),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch("skill_runner.tempfile.mkstemp", side_effect=_capturing_mkstemp),
            # Patch os.path.exists only for the JWK file check (return False = no JWK).
            patch("skill_runner.os.path.exists", return_value=False),
        ):
            result = self._run(
                skill_runner.run_skill(
                    "gryllus", "work prompt", role="gryllus", task_entity_id="ent_abc"
                )
            )

        assert result.ok
        assert "--mcp-config" in captured_cmd
        assert len(written_contents) == 1, (
            "Expected MCP config to be read during communicate"
        )
        cfg = written_contents[0]
        neotoma_cfg = cfg["mcpServers"]["mcpsrv_neotoma"]
        assert neotoma_cfg["url"].endswith("/mcp"), (
            f"Expected url ending in /mcp, got {neotoma_cfg['url']!r}"
        )
        # No Authorization header when no token.
        headers = neotoma_cfg.get("headers", {})
        assert "Authorization" not in headers, (
            "Expected no Authorization header when NEOTOMA_BEARER_TOKEN is unset"
        )

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_mcp_config_with_token_has_auth_header(
        self, MockLoader, mock_write_harness, monkeypatch
    ) -> None:
        """When NEOTOMA_BEARER_TOKEN is set, the injected config must include
        Authorization: Bearer <token> in the headers.

        Strategy: intercept tempfile.mkstemp to get the path, then read the file
        during proc.communicate() before cleanup.
        """
        import json as _json
        import tempfile as _tempfile

        fake_def = _make_def(prompt_markdown="Role: Gryllus.", tool_allowlist="*")
        instance = MagicMock()
        instance.load.return_value = fake_def
        MockLoader.return_value = instance

        monkeypatch.setenv("NEOTOMA_BASE_URL", "http://localhost:9180")
        monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "secret-bearer-abc")

        captured_cmd: list = []
        written_contents: list[dict] = []

        _real_mkstemp = _tempfile.mkstemp
        captured_paths: list[str] = []

        def _capturing_mkstemp(**kwargs):
            fd, path = _real_mkstemp(**kwargs)
            captured_paths.append(path)
            return fd, path

        async def fake_exec(*cmd, **kwargs):
            captured_cmd.extend(cmd)
            proc = MagicMock()
            proc.returncode = 0

            async def _communicate(input=None):
                if captured_paths:
                    import os as _real_os

                    fpath = captured_paths[-1]
                    if _real_os.path.isfile(fpath):
                        with open(fpath) as f:
                            written_contents.append(_json.load(f))
                return b"output", b""

            proc.communicate = _communicate
            return proc

        with (
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value="skill md"),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch("skill_runner.tempfile.mkstemp", side_effect=_capturing_mkstemp),
            patch("skill_runner.os.path.exists", return_value=False),
        ):
            result = self._run(
                skill_runner.run_skill(
                    "gryllus", "work prompt", role="gryllus", task_entity_id="ent_abc"
                )
            )

        assert result.ok
        assert len(written_contents) == 1, (
            "Expected MCP config to be read during communicate"
        )
        cfg = written_contents[0]
        neotoma_cfg = cfg["mcpServers"]["mcpsrv_neotoma"]
        assert neotoma_cfg["url"] == "http://localhost:9180/mcp", (
            f"Expected url 'http://localhost:9180/mcp', got {neotoma_cfg['url']!r}"
        )
        assert (
            neotoma_cfg.get("headers", {}).get("Authorization")
            == "Bearer secret-bearer-abc"
        ), "Expected Authorization header with bearer token"

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_restricted_allowlist_adds_neotoma_wildcard(
        self, MockLoader, mock_write_harness, monkeypatch
    ) -> None:
        """When the role has a restricted tool allowlist (not ['*']), the neotoma
        MCP wildcard 'mcp__mcpsrv_neotoma__*' must be added to --allowed-tools so
        the dispatched agent can call neotoma MCP tools under the canonical name."""
        restricted_def = _make_def(
            prompt_markdown="Restricted agent.",
            tool_allowlist="Bash,Read,Write",
        )
        instance = MagicMock()
        instance.load.return_value = restricted_def
        MockLoader.return_value = instance

        monkeypatch.setenv("NEOTOMA_BASE_URL", "http://localhost:9180")
        monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "tok")

        captured_cmd: list = []

        with (
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value="skill md"),
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=self._make_exec_capturer(captured_cmd),
            ),
            patch("os.path.exists", return_value=False),
        ):
            result = self._run(
                skill_runner.run_skill(
                    "gryllus", "work prompt", role="gryllus", task_entity_id="ent_abc"
                )
            )

        assert result.ok
        assert "--allowed-tools" in captured_cmd
        tools_idx = captured_cmd.index("--allowed-tools") + 1
        allowed_str = captured_cmd[tools_idx]
        assert "mcp__mcpsrv_neotoma__*" in allowed_str, (
            f"Expected 'mcp__mcpsrv_neotoma__*' in --allowed-tools, got: {allowed_str!r}"
        )
        # Original tools must still be present.
        assert "Bash" in allowed_str
        assert "Read" in allowed_str

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_wildcard_allowlist_not_modified(
        self, MockLoader, mock_write_harness, monkeypatch
    ) -> None:
        """When tool_allowlist is ['*'] (all tools), --allowed-tools must NOT
        appear in the command (wildcard means no restriction to pass through)."""
        wide_def = _make_def(prompt_markdown="Full-tool agent.", tool_allowlist="*")
        instance = MagicMock()
        instance.load.return_value = wide_def
        MockLoader.return_value = instance

        monkeypatch.setenv("NEOTOMA_BASE_URL", "http://localhost:9180")
        monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "tok")

        captured_cmd: list = []

        with (
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value="skill md"),
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=self._make_exec_capturer(captured_cmd),
            ),
            patch("os.path.exists", return_value=False),
        ):
            self._run(
                skill_runner.run_skill(
                    "gryllus", "work prompt", role="gryllus", task_entity_id="ent_abc"
                )
            )

        assert "--allowed-tools" not in captured_cmd, (
            "Expected no --allowed-tools flag when tool_allowlist is ['*']"
        )
        # But --mcp-config is still injected.
        assert "--mcp-config" in captured_cmd, (
            "Expected --mcp-config even when tool_allowlist is ['*']"
        )


# ── ateles#109 — github_token injection ──────────────────────────────────────


class TestGithubTokenInjection:
    """Per-agent GitHub token injection (#109).

    When github_token is passed to run_skill, both GITHUB_TOKEN and GH_TOKEN
    must be overridden in subprocess_env so the child's gh calls authenticate as
    the correct agent identity.

    When github_token is None (all SSE / non-GitHub call sites), the env is
    unchanged — existing ambient GITHUB_TOKEN is preserved.  This is the NO-OP
    property: callers that do not pass github_token observe zero behaviour change.
    """

    def setup_method(self) -> None:
        skill_runner._agent_def_cache.clear()

    def _run(self, coro):
        return asyncio.run(coro)

    def _make_exec_capturer_env(self, captured_envs: list) -> object:
        async def fake_exec(*cmd, **kwargs):
            captured_envs.append(dict(kwargs.get("env", {})))
            proc = MagicMock()
            proc.returncode = 0

            async def _communicate(input=None):
                return b"output", b""

            proc.communicate = _communicate
            return proc

        return fake_exec

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_github_token_injected_when_passed(
        self, MockLoader, mock_write_harness, monkeypatch
    ) -> None:
        """When github_token='ghp_agent_pat' is supplied, subprocess_env must carry
        GITHUB_TOKEN=<token> and GH_TOKEN=<token>."""
        fake_def = _make_def(prompt_markdown="Role: Pavo.", tool_allowlist="*")
        instance = MagicMock()
        instance.load.return_value = fake_def
        MockLoader.return_value = instance

        captured_envs: list = []

        monkeypatch.setenv("GITHUB_TOKEN", "ghp_ambient_daemon_token")
        monkeypatch.setenv("GH_TOKEN", "ghp_ambient_gh_token")

        with (
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value="skill md"),
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=self._make_exec_capturer_env(captured_envs),
            ),
            patch("os.path.exists", return_value=False),
        ):
            result = self._run(
                skill_runner.run_skill(
                    "pavo",
                    "work prompt",
                    role="pavo",
                    task_entity_id="ent_abc",
                    github_token="ghp_pavo_own_pat",
                )
            )

        assert result.ok
        assert len(captured_envs) == 1
        env = captured_envs[0]
        assert env.get("GITHUB_TOKEN") == "ghp_pavo_own_pat", (
            "GITHUB_TOKEN must be overridden to the per-agent token"
        )
        assert env.get("GH_TOKEN") == "ghp_pavo_own_pat", (
            "GH_TOKEN must be overridden to the per-agent token"
        )

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_github_token_not_injected_when_none(
        self, MockLoader, mock_write_harness, monkeypatch
    ) -> None:
        """NO-OP: when github_token is not passed (None), GITHUB_TOKEN and GH_TOKEN
        in subprocess_env must match the ambient daemon env — no override."""
        fake_def = _make_def(prompt_markdown="Role: Gryllus.", tool_allowlist="*")
        instance = MagicMock()
        instance.load.return_value = fake_def
        MockLoader.return_value = instance

        captured_envs: list = []

        monkeypatch.setenv("GITHUB_TOKEN", "ghp_ambient_daemon_token")
        monkeypatch.setenv("GH_TOKEN", "ghp_ambient_gh_token")

        with (
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value="skill md"),
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=self._make_exec_capturer_env(captured_envs),
            ),
            patch("os.path.exists", return_value=False),
        ):
            result = self._run(
                skill_runner.run_skill(
                    "gryllus",
                    "work prompt",
                    role="gryllus",
                    task_entity_id="ent_abc",
                    # github_token intentionally not passed (default None)
                )
            )

        assert result.ok
        assert len(captured_envs) == 1
        env = captured_envs[0]
        # Ambient tokens must be preserved unchanged.
        assert env.get("GITHUB_TOKEN") == "ghp_ambient_daemon_token", (
            "GITHUB_TOKEN must not be modified when github_token is not passed"
        )
        assert env.get("GH_TOKEN") == "ghp_ambient_gh_token", (
            "GH_TOKEN must not be modified when github_token is not passed"
        )

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_github_token_not_injected_when_empty_string(
        self, MockLoader, mock_write_harness, monkeypatch
    ) -> None:
        """When github_token='' (falsy), the env override must NOT happen.
        This guards against passing an unresolved empty token and clobbering a
        valid ambient GITHUB_TOKEN with an empty string."""
        fake_def = _make_def(prompt_markdown="Role: Gryllus.", tool_allowlist="*")
        instance = MagicMock()
        instance.load.return_value = fake_def
        MockLoader.return_value = instance

        captured_envs: list = []

        monkeypatch.setenv("GITHUB_TOKEN", "ghp_ambient_daemon_token")

        with (
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value="skill md"),
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=self._make_exec_capturer_env(captured_envs),
            ),
            patch("os.path.exists", return_value=False),
        ):
            result = self._run(
                skill_runner.run_skill(
                    "gryllus",
                    "work prompt",
                    role="gryllus",
                    task_entity_id="ent_abc",
                    github_token="",
                )
            )

        assert result.ok
        env = captured_envs[0]
        assert env.get("GITHUB_TOKEN") == "ghp_ambient_daemon_token", (
            "Empty github_token must not clobber a valid ambient GITHUB_TOKEN"
        )


# ── Phase 1 / Layer A: SWARM_GITHUB_CONTRACT injection ───────────────────────


class TestSwarmGithubContractInjection:
    """Phase 1 / Layer A (docs/swarm_github_interaction_design.md).

    build_system_prompt gains include_github_contract: bool = False.
    When True, SWARM_GITHUB_CONTRACT is injected between agent_def and skill_md.
    When False (default), prompt is byte-identical to pre-contract behaviour.

    run_skill threads the flag through to build_system_prompt.
    """

    def setup_method(self) -> None:
        skill_runner._agent_def_cache.clear()

    def _run(self, coro):
        return asyncio.run(coro)

    # ── build_system_prompt unit tests ─────────────────────────────────────────

    def test_contract_not_in_prompt_by_default(self) -> None:
        """Default (include_github_contract=False) must produce a prompt that
        does NOT contain SWARM_GITHUB_CONTRACT — byte-identical to pre-contract."""
        agent_def = _make_def(prompt_markdown="Agent identity.")
        skill_md = "Do the task."
        prompt, degraded = skill_runner.build_system_prompt(agent_def, skill_md)
        assert not degraded
        assert skill_runner.SWARM_GITHUB_CONTRACT not in prompt

    def test_contract_absent_when_false_explicit(self) -> None:
        """Explicit include_github_contract=False: contract must be absent."""
        agent_def = _make_def(prompt_markdown="Agent identity.")
        skill_md = "Do the task."
        prompt, _ = skill_runner.build_system_prompt(
            agent_def, skill_md, include_github_contract=False
        )
        assert skill_runner.SWARM_GITHUB_CONTRACT not in prompt

    def test_contract_present_when_true_with_definition(self) -> None:
        """include_github_contract=True with a real agent_def: SWARM_GITHUB_CONTRACT
        must appear in the prompt, along with both definition and skill_md."""
        agent_def = _make_def(prompt_markdown="Agent identity.")
        skill_md = "Do the task."
        prompt, degraded = skill_runner.build_system_prompt(
            agent_def, skill_md, include_github_contract=True
        )
        assert not degraded
        assert skill_runner.SWARM_GITHUB_CONTRACT in prompt
        assert "Agent identity." in prompt
        assert "Do the task." in prompt

    def test_contract_order_definition_then_contract_then_skill(self) -> None:
        """Order must be: definition → contract → skill_md (contract is a bridge layer)."""
        agent_def = _make_def(prompt_markdown="DEFINITION_ANCHOR")
        skill_md = "SKILL_ANCHOR"
        prompt, _ = skill_runner.build_system_prompt(
            agent_def, skill_md, include_github_contract=True
        )
        def_pos = prompt.index("DEFINITION_ANCHOR")
        contract_pos = prompt.index(skill_runner.SWARM_GITHUB_CONTRACT)
        skill_pos = prompt.index("SKILL_ANCHOR")
        assert def_pos < contract_pos < skill_pos, (
            "Order must be: definition → SWARM_GITHUB_CONTRACT → skill_md"
        )

    def test_contract_present_when_true_degraded(self) -> None:
        """Degraded (empty prompt_markdown) + contract=True: contract + skill_md
        both present; degraded=True still returned."""
        agent_def = _stub_def()
        skill_md = "Fallback instructions."
        prompt, degraded = skill_runner.build_system_prompt(
            agent_def, skill_md, include_github_contract=True
        )
        assert degraded, (
            "Degraded flag must still be True when prompt_markdown is empty"
        )
        assert skill_runner.SWARM_GITHUB_CONTRACT in prompt
        assert "Fallback instructions." in prompt

    def test_degraded_no_contract_returns_skill_md_only(self) -> None:
        """Degraded + contract=False: prompt is exactly skill_md (original behaviour)."""
        agent_def = _stub_def()
        skill_md = "Fallback instructions."
        prompt, degraded = skill_runner.build_system_prompt(
            agent_def, skill_md, include_github_contract=False
        )
        assert degraded
        assert prompt == skill_md
        assert skill_runner.SWARM_GITHUB_CONTRACT not in prompt

    # ── run_skill threads the flag ──────────────────────────────────────────────

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_run_skill_threads_contract_flag_true(
        self, MockLoader, mock_write_harness
    ) -> None:
        """When run_skill is called with include_github_contract=True, the
        spawned system prompt arg must contain SWARM_GITHUB_CONTRACT."""
        fake_def = _make_def(prompt_markdown="Role: Gryllus.")
        instance = MagicMock()
        instance.load.return_value = fake_def
        MockLoader.return_value = instance

        captured_cmd: list = []

        async def fake_exec(*cmd, **kwargs):
            captured_cmd.extend(cmd)
            proc = MagicMock()
            proc.returncode = 0

            async def _communicate(input=None):
                return b"output", b""

            proc.communicate = _communicate
            return proc

        skill_md_content = "GitHub task skill."
        with (
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=skill_md_content),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            result = self._run(
                skill_runner.run_skill(
                    "gryllus",
                    "work prompt",
                    role="gryllus",
                    task_entity_id="ent_abc",
                    include_github_contract=True,
                )
            )

        assert result.ok
        sys_prompt_idx = captured_cmd.index("--append-system-prompt") + 1
        system_prompt_arg = captured_cmd[sys_prompt_idx]
        assert skill_runner.SWARM_GITHUB_CONTRACT in system_prompt_arg, (
            "SWARM_GITHUB_CONTRACT must appear in system prompt when include_github_contract=True"
        )
        assert "Role: Gryllus." in system_prompt_arg
        assert skill_md_content in system_prompt_arg

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_run_skill_contract_absent_by_default(
        self, MockLoader, mock_write_harness
    ) -> None:
        """Default run_skill call (no include_github_contract): SWARM_GITHUB_CONTRACT
        must NOT appear — preserves byte-identical pre-contract behaviour."""
        fake_def = _make_def(prompt_markdown="Role: Gryllus.")
        instance = MagicMock()
        instance.load.return_value = fake_def
        MockLoader.return_value = instance

        captured_cmd: list = []

        async def fake_exec(*cmd, **kwargs):
            captured_cmd.extend(cmd)
            proc = MagicMock()
            proc.returncode = 0

            async def _communicate(input=None):
                return b"output", b""

            proc.communicate = _communicate
            return proc

        skill_md_content = "SSE task skill."
        with (
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=skill_md_content),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            result = self._run(
                skill_runner.run_skill(
                    "gryllus",
                    "work prompt",
                    role="gryllus",
                    task_entity_id="ent_abc",
                    # include_github_contract intentionally not passed (default False)
                )
            )

        assert result.ok
        sys_prompt_idx = captured_cmd.index("--append-system-prompt") + 1
        system_prompt_arg = captured_cmd[sys_prompt_idx]
        assert skill_runner.SWARM_GITHUB_CONTRACT not in system_prompt_arg, (
            "SWARM_GITHUB_CONTRACT must NOT appear when include_github_contract=False (default)"
        )

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_run_skill_degraded_with_contract(
        self, MockLoader, mock_write_harness
    ) -> None:
        """Degraded + include_github_contract=True: contract + skill_md in prompt,
        dispatch still proceeds (degraded=True returned by build_system_prompt)."""
        stub = _stub_def()
        instance = MagicMock()
        instance.load.return_value = stub
        MockLoader.return_value = instance

        captured_cmd: list = []

        async def fake_exec(*cmd, **kwargs):
            captured_cmd.extend(cmd)
            proc = MagicMock()
            proc.returncode = 0

            async def _communicate(input=None):
                return b"output", b""

            proc.communicate = _communicate
            return proc

        skill_md_content = "Fallback skill content."
        with (
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=skill_md_content),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            result = self._run(
                skill_runner.run_skill(
                    "gryllus",
                    "work prompt",
                    role="gryllus",
                    task_entity_id="ent_abc",
                    include_github_contract=True,
                )
            )

        # Dispatch still succeeds despite degraded.
        assert result.ok
        sys_prompt_idx = captured_cmd.index("--append-system-prompt") + 1
        system_prompt_arg = captured_cmd[sys_prompt_idx]
        assert skill_runner.SWARM_GITHUB_CONTRACT in system_prompt_arg, (
            "Contract must still be injected even in degraded mode"
        )
        assert skill_md_content in system_prompt_arg
        # The degraded harness_event is also emitted (the degraded branch ran).
        degraded_calls = [
            call
            for call in mock_write_harness.call_args_list
            if "degraded_generic_subagent" in (call.kwargs.get("output_summary") or "")
        ]
        assert len(degraded_calls) >= 1


# ── Phase 1 / Layer A: tightened attribution spec (neotoma#1686 follow-up) ───


class TestSwarmGithubContractAttributionSpec:
    """Verify SWARM_GITHUB_CONTRACT contains the tightened attribution spec.

    Live test on neotoma#1686 showed Pavo posting two different header forms in
    one thread — the old loose skeleton (`🤖 <Agent> — <role> · <repo>#<n>`)
    allowed agents to improvise capitalization, role wording, and repo suffixes.

    These tests assert the contract now contains an exact reproduce-verbatim spec,
    a worked example, and no longer instructs appending repo#<n> to the header.
    """

    def test_contract_contains_exact_ateles_swarm_em_dash_form(self) -> None:
        """The attribution header spec must contain '— Ateles swarm,' (em-dash + literal prefix)."""
        assert "— Ateles swarm," in skill_runner.SWARM_GITHUB_CONTRACT, (
            "Contract must specify '— Ateles swarm,' as the exact attribution prefix"
        )

    def test_contract_contains_verbatim_reproduction_instruction(self) -> None:
        """The contract must instruct agents to reproduce the header format verbatim."""
        contract = skill_runner.SWARM_GITHUB_CONTRACT
        # The key instruction phrase must be present.
        assert "Reproduce this header format EXACTLY" in contract, (
            "Contract must contain a 'Reproduce this header format EXACTLY' instruction"
        )

    def test_contract_contains_worked_example(self) -> None:
        """A worked example must be present so agents have a concrete target to mimic."""
        contract = skill_runner.SWARM_GITHUB_CONTRACT
        # The worked example section heading must appear.
        assert "Worked example" in contract, (
            "Contract must contain a 'Worked example' section"
        )
        # The example must show a real compliant header.
        assert "**🤖 Pavo — Ateles swarm, pm gate owner**" in contract, (
            "Contract must contain a concrete worked-example header in the exact format"
        )

    def test_contract_does_not_instruct_repo_issue_suffix_in_header(self) -> None:
        """The old prescriptive skeleton '🤖 <Agent> — <role> · <repo>#<n>' must be gone.

        The old form presented '· <repo>#<n>' as a template to follow, which caused
        inconsistency (neotoma#1686). The new contract prohibits it instead. We verify
        that the old prescriptive skeleton line is not present — i.e. the header template
        no longer includes '<role> · <repo>#<n>' as something to reproduce.
        """
        contract = skill_runner.SWARM_GITHUB_CONTRACT
        # The old skeleton line combined '<role>' with '· <repo>#<n>' in a way that
        # told agents to append the repo suffix. Check the combined prescriptive form is absent.
        assert "<role> · <repo>#<n>" not in contract, (
            "Contract must not present '<role> · <repo>#<n>' as a header template — "
            "the old loose skeleton suffix that caused header inconsistency (neotoma#1686)"
        )
        # Additionally, the contract must actively forbid appending the suffix.
        assert "Do NOT append" in contract, (
            "Contract must explicitly forbid appending the repo/issue suffix to the header"
        )

    def test_contract_specifies_title_case_agent_name(self) -> None:
        """The spec must require agent name in Title Case (not lowercase)."""
        contract = skill_runner.SWARM_GITHUB_CONTRACT
        assert "Title Case" in contract, (
            "Contract must specify agent name in Title Case"
        )

    def test_contract_specifies_em_dash_not_hyphen(self) -> None:
        """The spec must call out the em-dash requirement (U+2014)."""
        contract = skill_runner.SWARM_GITHUB_CONTRACT
        assert "em-dash" in contract, (
            "Contract must specify em-dash (—, U+2014), not a hyphen"
        )


class TestAnthropicAuthPrecedence:
    """The spawned `claude --print` must prefer the operator's Claude
    subscription (CLAUDE_CODE_OAUTH_TOKEN) over metered ANTHROPIC_API_KEY."""

    def setup_method(self) -> None:
        skill_runner._agent_def_cache.clear()

    def _spawn_and_capture_env(self, env_overrides: dict) -> dict:
        instance = MagicMock()
        instance.load.return_value = _stub_def()
        captured: dict = {}

        async def fake_exec(*cmd, **kwargs):
            captured.update(kwargs.get("env") or {})
            proc = MagicMock()
            proc.returncode = 0

            async def _communicate(input=None):
                return b"output", b""

            proc.communicate = _communicate
            return proc

        with (
            patch("skill_runner.AgentLoader", return_value=instance),
            patch("skill_runner._write_harness_event"),
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value="Fallback skill."),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch.dict("os.environ", env_overrides, clear=False),
        ):
            asyncio.run(
                skill_runner.run_skill(
                    "gryllus", "p", role="gryllus", task_entity_id="ent_x"
                )
            )
        return captured

    def test_oauth_token_present_drops_api_key(self) -> None:
        env = self._spawn_and_capture_env(
            {
                "CLAUDE_CODE_OAUTH_TOKEN": "sk-oauth-xyz",
                "ANTHROPIC_API_KEY": "sk-ant-metered",
            }
        )
        assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "sk-oauth-xyz"
        assert "ANTHROPIC_API_KEY" not in env, (
            "ANTHROPIC_API_KEY must be removed when the subscription token is present, "
            "else claude bills metered credits instead of the Max plan"
        )

    def test_no_oauth_token_still_drops_api_key_by_default(self) -> None:
        env = self._spawn_and_capture_env(
            {"ANTHROPIC_API_KEY": "sk-ant-metered", "CLAUDE_CODE_OAUTH_TOKEN": ""}
        )
        assert "ANTHROPIC_API_KEY" not in env, (
            "Without subscription auth, dispatch must fail/queue instead of billing "
            "metered Anthropic credits"
        )

    def test_explicit_metered_override_keeps_api_key(self) -> None:
        env = self._spawn_and_capture_env(
            {
                "ANTHROPIC_API_KEY": "sk-ant-metered",
                "CLAUDE_CODE_OAUTH_TOKEN": "",
                "APIS_ALLOW_METERED_HARNESS": "1",
            }
        )
        assert env.get("ANTHROPIC_API_KEY") == "sk-ant-metered"


# ── Dropped-allowlist-rule notification (ateles#255) ────────────────────────────


class TestFindDroppedAllowlistRules:
    def test_single_rule_extracted(self) -> None:
        stderr = (
            "ERROR apis.skill_runner [apis] cicada dispatch failed (rc=1): Ignoring "
            '--allowedTools rule "pr*": Wildcard tool name "pr*" is not supported.'
        )
        assert skill_runner._find_dropped_allowlist_rules(stderr) == ["pr*"]

    def test_multiple_distinct_rules_extracted_in_order(self) -> None:
        stderr = (
            'Ignoring --allowedTools rule "pr*": ...\n'
            'Ignoring --allowedTools rule "issue*": ...\n'
        )
        assert skill_runner._find_dropped_allowlist_rules(stderr) == ["pr*", "issue*"]

    def test_duplicate_rule_deduplicated(self) -> None:
        stderr = (
            'Ignoring --allowedTools rule "pr*": ...\n'
            'Ignoring --allowedTools rule "pr*": ...\n'
        )
        assert skill_runner._find_dropped_allowlist_rules(stderr) == ["pr*"]

    def test_clean_stderr_returns_empty(self) -> None:
        assert skill_runner._find_dropped_allowlist_rules("") == []
        assert skill_runner._find_dropped_allowlist_rules("no problems here") == []

    def test_real_cli_line_wrapped_format_extracted(self) -> None:
        """Regression: the real Claude Code CLI line-wraps this message with a
        newline (not a space) between "Ignoring" and "--allowedTools" — the
        exact text quoted in ateles#255's repro. A plain-space-only regex
        never matches this and the whole notification feature goes silently
        inert against real dispatch output."""
        stderr = (
            "ERROR apis.skill_runner [apis] cicada dispatch failed (rc=1): Ignoring\n"
            '--allowedTools rule "pr*": Wildcard tool name "pr*" is not supported in allow\n'
            "rules. An allow pattern must name the scope it widens — globs are permitted\n"
            "only in the tool position after a literal mcp__<server>__ prefix. Deny and ask\n"
            "rules accept wildcards anywhere."
        )
        assert skill_runner._find_dropped_allowlist_rules(stderr) == ["pr*"]


class TestNotifyDroppedAllowlistRules:
    def test_no_rules_sends_nothing(self) -> None:
        notifier = MagicMock()
        skill_runner._notify_dropped_allowlist_rules(
            notifier, role="cicada", rules=[], returncode=0
        )
        notifier.send.assert_not_called()

    def test_none_notifier_does_not_raise(self) -> None:
        # Must not raise even though there's nothing to call .send on.
        skill_runner._notify_dropped_allowlist_rules(
            None, role="cicada", rules=["pr*"], returncode=1
        )

    def test_single_rule_sends_one_notification(self) -> None:
        notifier = MagicMock()
        skill_runner._notify_dropped_allowlist_rules(
            notifier, role="cicada", rules=["pr*"], returncode=1
        )
        assert notifier.send.call_count == 1
        msg = notifier.send.call_args[0][0]
        assert "cicada" in msg
        assert "pr*" in msg

    def test_multi_rule_batches_into_one_notification(self) -> None:
        """Two dropped rules in one dispatch -> ONE notification naming both,
        not two separate alerts (avoids duplicate paging)."""
        notifier = MagicMock()
        skill_runner._notify_dropped_allowlist_rules(
            notifier, role="cicada", rules=["pr*", "issue*"], returncode=1
        )
        assert notifier.send.call_count == 1
        msg = notifier.send.call_args[0][0]
        assert "pr*" in msg
        assert "issue*" in msg

    def test_notifier_send_failure_does_not_raise(self) -> None:
        notifier = MagicMock()
        notifier.send.side_effect = RuntimeError("apprise down")
        # Must not propagate — a notification failure must not crash dispatch.
        skill_runner._notify_dropped_allowlist_rules(
            notifier, role="cicada", rules=["pr*"], returncode=1
        )


class TestRunSkillDroppedAllowlistNotification:
    """End-to-end (mocked subprocess) coverage: run_skill must invoke the
    notifier exactly once when the CLI stderr reports dropped rules, and must
    not invoke it at all on a clean dispatch."""

    def setup_method(self) -> None:
        skill_runner._agent_def_cache.clear()

    @staticmethod
    def _allowlist_alerts(notifier) -> list[str]:
        """The subset of notifier messages that are ateles#255 dropped-rule
        alerts.

        A FAILING dispatch also raises the independent ateles#257
        dispatch-failure alert on the same notifier, so a bare
        ``send.call_count`` no longer isolates this feature. Select by content
        instead: the assertions below are about the dropped-rule alert being
        emitted exactly ONCE per dispatch (batched, never one per rule), which
        is what #255 is actually specifying.

        Matched on this alert's own wording rather than on ``--allowedTools
        rule``: the #257 alert embeds a stderr preview, so on these fixtures
        the raw CLI phrase appears in BOTH messages.
        """
        return [
            c.args[0]
            for c in notifier.send.call_args_list
            if "silently dropped by the CLI" in c.args[0]
        ]

    def _run_with_stderr(self, stderr_bytes: bytes, notifier, tmp_path):
        fake_def = _make_def(prompt_markdown="Role prompt.")
        instance = MagicMock()
        instance.load.return_value = fake_def

        async def fake_exec(*cmd, **kwargs):
            proc = MagicMock()
            proc.returncode = 1 if stderr_bytes else 0

            async def _communicate(input=None):
                return b"", stderr_bytes

            proc.communicate = _communicate
            return proc

        # A failing dispatch now also writes an ateles#257 diagnostics file;
        # keep it inside tmp_path so these tests never touch ~/Library/Logs.
        failure_dir = tmp_path / "dispatch-failures"

        with (
            patch("skill_runner.AgentLoader", return_value=instance),
            patch("skill_runner._write_harness_event"),
            patch("skill_runner.DISPATCH_FAILURE_LOG_DIR", failure_dir),
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value="Skill instructions."),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            return asyncio.run(
                skill_runner.run_skill(
                    "cicada",
                    "work prompt",
                    role="cicada",
                    task_entity_id="ent_x",
                    notifier=notifier,
                )
            )

    def test_notification_fires_on_dropped_rule(self, tmp_path) -> None:
        """Uses the REAL CLI line-wrapped format (newline, not space, between
        "Ignoring" and "--allowedTools") — the exact text from ateles#255's
        repro — so this e2e test actually exercises the wrap the regex must
        handle, not a simplified single-line stand-in."""
        notifier = MagicMock()
        stderr = (
            b"ERROR apis.skill_runner [apis] cicada dispatch failed (rc=1): Ignoring\n"
            b'--allowedTools rule "pr*": Wildcard tool name "pr*" is not supported.'
        )
        result = self._run_with_stderr(stderr, notifier, tmp_path)
        assert not result.ok
        assert len(self._allowlist_alerts(notifier)) == 1

    def test_no_notification_on_clean_dispatch(self, tmp_path) -> None:
        notifier = MagicMock()
        result = self._run_with_stderr(b"", notifier, tmp_path)
        assert result.ok
        # A clean dispatch raises neither the #255 nor the #257 alert.
        notifier.send.assert_not_called()

    def test_multi_rule_drop_in_one_dispatch_batches_single_notification(
        self, tmp_path
    ) -> None:
        notifier = MagicMock()
        stderr = (
            b'Ignoring --allowedTools rule "pr*": ...\n'
            b'Ignoring --allowedTools rule "issue*": ...\n'
        )
        result = self._run_with_stderr(stderr, notifier, tmp_path)
        assert not result.ok
        alerts = self._allowlist_alerts(notifier)
        assert len(alerts) == 1
        assert "pr*" in alerts[0]
        assert "issue*" in alerts[0]


# ── ateles#257: dispatch-failure diagnostics capture + operator notification ───


class TestDispatchFailureDiagnostics:
    """
    ateles#257 — a failed dispatch must persist the COMPLETE child stdout AND
    stderr to a file, surface that path in the ERROR log line and in the
    harness_event, notify the operator (rate-limited), and never let a
    diagnostics failure break the dispatch itself.
    """

    def setup_method(self) -> None:
        skill_runner._agent_def_cache.clear()
        skill_runner._dispatch_failure_notified_at.clear()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _run_dispatch(
        self,
        tmp_path,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 1,
        notifier=None,
        log_dir=None,
    ):
        """Run run_skill against a fake child with the given exit status.

        Returns (result, harness_calls, log_dir).
        """
        fake_def = _make_def(prompt_markdown="Role: Gryllus.")
        loader_instance = MagicMock()
        loader_instance.load.return_value = fake_def

        async def fake_exec(*cmd, **kwargs):
            proc = MagicMock()
            proc.returncode = returncode

            async def _communicate(input=None):
                return stdout, stderr

            proc.communicate = _communicate
            return proc

        harness_calls: list = []

        def fake_harness(**kwargs):
            harness_calls.append(kwargs)

        target_dir = log_dir if log_dir is not None else tmp_path / "dispatch-failures"

        with (
            patch("skill_runner.AgentLoader", return_value=loader_instance),
            patch("skill_runner._write_harness_event", side_effect=fake_harness),
            patch("skill_runner.DISPATCH_FAILURE_LOG_DIR", target_dir),
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value="skill body"),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            result = asyncio.run(
                skill_runner.run_skill(
                    "gryllus",
                    "work prompt",
                    role="gryllus",
                    task_entity_id="ent_fail",
                    notifier=notifier,
                )
            )
        return result, harness_calls, target_dir

    @staticmethod
    def _logs(log_dir) -> list:
        d = Path(log_dir)
        return sorted(d.glob("*.log")) if d.is_dir() else []

    # ── A. full stdout + stderr persisted on failure ──────────────────────────

    def test_failed_dispatch_writes_file_with_full_stdout_and_stderr(
        self, tmp_path
    ) -> None:
        """Both streams must survive in full — past every old truncation point.

        Before this fix the log kept stderr[:500], the harness_event kept
        stderr[:200], and stdout was discarded outright.
        """
        long_stdout = "S" * 3000 + "REAL_CAUSE_IN_STDOUT"
        long_stderr = (
            "warning: --allowedTools wildcard ignored\n" * 40
        ) + "REAL_CAUSE_IN_STDERR"

        result, _, log_dir = self._run_dispatch(
            tmp_path, stdout=long_stdout.encode(), stderr=long_stderr.encode()
        )

        assert result.ok is False
        files = self._logs(log_dir)
        assert len(files) == 1, "exactly one diagnostics file per failed dispatch"

        body = files[0].read_text(encoding="utf-8")
        assert long_stdout in body, (
            "full stdout must be persisted (it was dropped before)"
        )
        assert long_stderr in body, "full stderr must be persisted, not stderr[:500]"
        assert "REAL_CAUSE_IN_STDOUT" in body
        assert "REAL_CAUSE_IN_STDERR" in body

    def test_failure_log_delimits_streams_and_records_context(self, tmp_path) -> None:
        _, _, log_dir = self._run_dispatch(
            tmp_path, stdout=b"the stdout", stderr=b"the stderr"
        )
        body = self._logs(log_dir)[0].read_text(encoding="utf-8")

        # Clearly delimited streams
        assert "===== STDOUT (complete) =====" in body
        assert "===== END STDOUT =====" in body
        assert "===== STDERR (complete) =====" in body
        assert "===== END STDERR =====" in body
        # Dispatch context
        assert "skill: gryllus" in body
        assert "role: gryllus" in body
        assert "returncode: 1" in body
        assert "task_entity_id: ent_fail" in body

    def test_failure_log_records_command_without_system_prompt_blob(
        self, tmp_path
    ) -> None:
        """Command context is captured, but the multi-KB system prompt is elided."""
        _, _, log_dir = self._run_dispatch(tmp_path, stdout=b"o", stderr=b"e")
        body = self._logs(log_dir)[0].read_text(encoding="utf-8")
        header = body.split("===== STDOUT")[0]

        assert "command:" in header
        assert "--append-system-prompt" in header
        assert "system-prompt elided" in header
        assert "skill body" not in header, "the system prompt must not be inlined"

    def test_successful_dispatch_writes_no_failure_log(self, tmp_path) -> None:
        result, _, log_dir = self._run_dispatch(tmp_path, stdout=b"fine", returncode=0)
        assert result.ok is True
        assert self._logs(log_dir) == []

    def test_each_failure_gets_its_own_file(self, tmp_path) -> None:
        for i in range(3):
            self._run_dispatch(tmp_path, stderr=f"boom {i}".encode())
        assert len(self._logs(tmp_path / "dispatch-failures")) == 3

    # ── B. path surfaces in the harness_event and the ERROR log ───────────────

    def test_failure_path_appears_in_harness_event_summary(self, tmp_path) -> None:
        _, harness_calls, log_dir = self._run_dispatch(
            tmp_path, stdout=b"stdout body", stderr=b"stderr body"
        )
        written = self._logs(log_dir)
        assert written, "a diagnostics file must have been written"
        expected_path = str(written[0])

        failures = [c for c in harness_calls if c.get("success") == "false"]
        assert failures, "a failure harness_event must be written"
        summary = failures[-1]["output_summary"]

        assert expected_path in summary, (
            "the harness_event must carry the diagnostics path so a failure is "
            "traceable from Neotoma without log-diving"
        )
        assert "rc=1" in summary
        assert "stderr body" in summary, "inline preview retained for triage"

    def test_failure_path_appears_in_error_log_line(self, tmp_path, caplog) -> None:
        import logging as _logging

        with caplog.at_level(_logging.ERROR, logger="apis.skill_runner"):
            _, _, log_dir = self._run_dispatch(tmp_path, stdout=b"o", stderr=b"e")

        expected_path = str(self._logs(log_dir)[0])
        errors = [r.getMessage() for r in caplog.records if r.levelno >= _logging.ERROR]
        assert any(expected_path in m for m in errors), (
            f"the ERROR log must name the diagnostics file; got {errors}"
        )

    # ── C. a diagnostics failure must NOT break dispatch ──────────────────────

    def test_write_helper_returns_empty_string_instead_of_raising(self) -> None:
        with patch.object(Path, "mkdir", side_effect=OSError("read-only filesystem")):
            path = skill_runner.write_dispatch_failure_log(
                skill="gryllus",
                role="gryllus",
                returncode=1,
                stdout="out",
                stderr="err",
            )
        assert path == "", "a failed diagnostics write returns '' rather than raising"

    def test_diagnostics_write_failure_does_not_break_dispatch(self, tmp_path) -> None:
        """An unwritable diagnostics dir must still yield a normal SkillResult."""
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("I am a file, so mkdir beneath me fails")

        result, harness_calls, _ = self._run_dispatch(
            tmp_path,
            stdout=b"stdout body",
            stderr=b"stderr body",
            log_dir=blocker / "sub",
        )

        assert result.ok is False
        assert result.returncode == 1
        assert result.stdout == "stdout body"
        assert result.stderr == "stderr body"

        failures = [c for c in harness_calls if c.get("success") == "false"]
        assert failures, "the harness_event is still written when diagnostics fail"
        assert "diagnostics file unavailable" in failures[-1]["output_summary"]

    def test_diagnostics_failure_still_notifies_operator(self, tmp_path) -> None:
        blocker = tmp_path / "not-a-dir-2"
        blocker.write_text("x")
        notifier = MagicMock()

        self._run_dispatch(
            tmp_path, stderr=b"boom", notifier=notifier, log_dir=blocker / "sub"
        )

        assert notifier.send.call_count == 1
        assert "diagnostics file unavailable" in notifier.send.call_args.args[0]

    # ── D. operator notification + dedup ──────────────────────────────────────

    def test_failed_dispatch_notifies_operator(self, tmp_path) -> None:
        notifier = MagicMock()
        _, _, log_dir = self._run_dispatch(
            tmp_path, stdout=b"o", stderr=b"boom", notifier=notifier
        )

        assert notifier.send.call_count == 1, (
            "a dispatch failure must reach the operator"
        )
        message = notifier.send.call_args.args[0]
        assert "gryllus" in message
        assert "rc=1" in message
        assert "ent_fail" in message
        assert str(self._logs(log_dir)[0]) in message, (
            "the notification must point at the full-output file"
        )

    def test_notification_priority_is_blocker(self, tmp_path) -> None:
        from lib.notify import Priority

        notifier = MagicMock()
        self._run_dispatch(tmp_path, stderr=b"boom", notifier=notifier)
        assert notifier.send.call_args.kwargs["priority"] == Priority.BLOCKER
        assert notifier.send.call_args.kwargs["handler"] == "apis"

    def test_identical_failures_are_deduped(self, tmp_path) -> None:
        notifier = MagicMock()
        for _ in range(5):
            self._run_dispatch(tmp_path, stderr=b"same boom", notifier=notifier)
        assert notifier.send.call_count == 1, (
            "a burst of identical failures must produce one signal, not five"
        )

    def test_different_failures_still_notify(self, tmp_path) -> None:
        notifier = MagicMock()
        self._run_dispatch(tmp_path, stderr=b"auth token expired", notifier=notifier)
        self._run_dispatch(
            tmp_path, stderr=b"worktree checkout conflict", notifier=notifier
        )
        assert notifier.send.call_count == 2, (
            "dedup must key on the failure signature, not suppress everything"
        )

    def test_dedup_ignores_volatile_ids_and_numbers(self, tmp_path) -> None:
        notifier = MagicMock()
        self._run_dispatch(
            tmp_path,
            stderr=b"failed at 2026-07-23T10:00:00 run abc123def456",
            notifier=notifier,
        )
        self._run_dispatch(
            tmp_path,
            stderr=b"failed at 2026-07-24T11:22:33 run fed654cba321",
            notifier=notifier,
        )
        assert notifier.send.call_count == 1, (
            "timestamps/run ids must not defeat dedup for the same systemic failure"
        )

    def test_dedup_window_expiry_allows_renotification(self) -> None:
        skill_runner._dispatch_failure_notified_at.clear()
        sig = skill_runner._failure_signature("gryllus", 1, "boom")
        assert skill_runner._should_notify_dispatch_failure(sig, now=1000.0) is True
        assert skill_runner._should_notify_dispatch_failure(sig, now=1010.0) is False
        later = 1000.0 + skill_runner.DISPATCH_FAILURE_NOTIFY_WINDOW_SECONDS + 1
        assert skill_runner._should_notify_dispatch_failure(sig, now=later) is True

    def test_notifier_exception_does_not_break_dispatch(self, tmp_path) -> None:
        notifier = MagicMock()
        notifier.send.side_effect = RuntimeError("telegram down")
        result, _, _ = self._run_dispatch(
            tmp_path, stdout=b"o", stderr=b"boom", notifier=notifier
        )
        assert result.ok is False, (
            "a notifier blowup must not change the dispatch outcome"
        )

    def test_no_notifier_is_a_no_op_but_diagnostics_still_written(
        self, tmp_path
    ) -> None:
        result, _, log_dir = self._run_dispatch(
            tmp_path, stdout=b"o", stderr=b"boom", notifier=None
        )
        assert result.ok is False
        assert self._logs(log_dir), "diagnostics are written even without a notifier"

    def test_successful_dispatch_does_not_notify(self, tmp_path) -> None:
        notifier = MagicMock()
        self._run_dispatch(tmp_path, stdout=b"fine", returncode=0, notifier=notifier)
        assert notifier.send.call_count == 0

    def test_timeout_notifies_operator(self, tmp_path) -> None:
        """A timed-out dispatch is the same silent-failure class."""
        fake_def = _make_def(prompt_markdown="Role: Gryllus.")
        loader_instance = MagicMock()
        loader_instance.load.return_value = fake_def
        notifier = MagicMock()

        async def fake_exec(*cmd, **kwargs):
            proc = MagicMock()
            proc.returncode = None
            calls = {"n": 0}

            async def _communicate(input=None):
                # First call (inside wait_for) hangs past the timeout; the
                # second is the post-kill drain, which must return normally.
                calls["n"] += 1
                if calls["n"] == 1:
                    await asyncio.sleep(5)
                return b"", b""

            proc.communicate = _communicate
            proc.kill = MagicMock()
            return proc

        with (
            patch("skill_runner.AgentLoader", return_value=loader_instance),
            patch("skill_runner._write_harness_event"),
            patch("skill_runner.DISPATCH_FAILURE_LOG_DIR", tmp_path / "df"),
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value="skill body"),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            result = asyncio.run(
                skill_runner.run_skill(
                    "gryllus",
                    "p",
                    role="gryllus",
                    task_entity_id="ent_to",
                    timeout=1,
                    notifier=notifier,
                )
            )

        assert result.ok is False
        assert "timed out" in result.error
        assert notifier.send.call_count == 1
        assert "timed out" in notifier.send.call_args.args[0]

    # ── E. secret redaction in persisted output ───────────────────────────────

    def test_secrets_are_redacted_from_the_failure_log(self, tmp_path) -> None:
        # Deliberately not a real token shape, and not bound to a name the
        # repo's gitleaks `protected-patterns` rule treats as a credential.
        fake_value = "FAKE-TEST-TOKEN-VALUE-0000"
        with (
            patch("skill_runner.DISPATCH_FAILURE_LOG_DIR", tmp_path / "d"),
            patch.dict("os.environ", {"GITHUB_TOKEN": fake_value}, clear=False),
        ):
            path = skill_runner.write_dispatch_failure_log(
                skill="gryllus",
                role="gryllus",
                returncode=1,
                stdout=f"leaked {fake_value} in stdout",
                stderr=f"leaked {fake_value} in stderr",
            )

        assert path
        body = Path(path).read_text(encoding="utf-8")
        assert fake_value not in body
        assert body.count("<redacted:GITHUB_TOKEN>") == 2

    def test_skill_name_is_slugified_into_the_filename(self, tmp_path) -> None:
        with patch("skill_runner.DISPATCH_FAILURE_LOG_DIR", tmp_path / "d"):
            path = skill_runner.write_dispatch_failure_log(
                skill="weird/../name with spaces",
                role="gryllus",
                returncode=1,
                stdout="",
                stderr="",
            )
        assert path
        name = Path(path).name
        assert "/" not in name
        assert ".." not in name
        assert Path(path).parent == tmp_path / "d"


class TestCrossHarnessRouting:
    def test_codex_adapter_uses_noninteractive_subscription_cli(self) -> None:
        cmd, stdin = skill_runner._provider_command(
            "codex",
            "/bin/codex",
            "SYSTEM",
            "WORK",
            cwd="/repo",
        )
        # ateles#590 added the network grant, but #601's review scoped it to
        # delivery-bearing dispatches: the default carries NO network flag.
        # `/repo` is not a git repo here, so no --add-dir appears;
        # TestCodexSandboxGitRoots covers that against a real linked worktree.
        assert cmd == [
            "/bin/codex",
            "exec",
            "--sandbox",
            "workspace-write",
            "--ephemeral",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--cd",
            "/repo",
            "-",
        ]
        assert stdin is not None
        assert b"SYSTEM" in stdin
        assert b"WORK" in stdin

    def test_cursor_adapter_uses_headless_agent(self) -> None:
        cmd, stdin = skill_runner._provider_command(
            "cursor",
            "/bin/cursor-agent",
            "SYSTEM",
            "WORK",
            cwd="/repo",
        )
        assert cmd[:7] == [
            "/bin/cursor-agent",
            "--print",
            "--force",
            "--trust",
            "--approve-mcps",
            "--output-format",
            "text",
        ]
        assert "--workspace" in cmd
        assert "SYSTEM" in cmd[-1]
        assert "WORK" in cmd[-1]
        assert stdin is None

    def test_all_metered_credentials_are_removed_by_default(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-metered")
        monkeypatch.setenv("OPENAI_API_KEY", "openai-metered")
        monkeypatch.setenv("CURSOR_API_KEY", "cursor-metered")
        child = skill_runner._subscription_only_env()
        assert "ANTHROPIC_API_KEY" not in child
        assert "OPENAI_API_KEY" not in child
        assert "CURSOR_API_KEY" not in child

    def test_cursor_headless_login_failure_is_safe_to_fail_over(self) -> None:
        assert (
            skill_runner._provider_failure_kind(
                "Authentication required. Please run 'agent login' first"
            )
            == "auth"
        )

    def test_capacity_failure_fails_over_to_next_provider(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "claude,codex,cursor")
        monkeypatch.setenv(
            "APIS_HARNESS_HEADROOM",
            '{"claude": 1.0, "codex": 1.0, "cursor": 1.0}',
        )
        harness_router.reset_state()
        attempts: list[str] = []

        async def fake_once(skill, prompt, *, provider, **kwargs):
            attempts.append(provider)
            if provider == "claude":
                return skill_runner.SkillResult(
                    skill,
                    False,
                    1,
                    "",
                    "You've hit your weekly usage limit; resets in 2 hours",
                    provider=provider,
                )
            return skill_runner.SkillResult(
                skill, True, 0, "done", "", provider=provider
            )

        with (
            patch(
                "skill_runner._provider_binaries",
                return_value={
                    "claude": "/bin/claude",
                    "codex": "/bin/codex",
                    "cursor": "/bin/cursor-agent",
                },
            ),
            patch("skill_runner._run_skill_once", side_effect=fake_once),
        ):
            result = asyncio.run(skill_runner.run_skill("gryllus", "work"))

        assert result.ok
        assert result.provider == "codex"
        assert result.attempted_providers == ("claude", "codex")
        assert attempts == ["claude", "codex"]

    def test_ordinary_task_failure_does_not_replay_on_another_provider(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "claude,codex")
        harness_router.reset_state()
        attempts: list[str] = []

        async def fake_once(skill, prompt, *, provider, **kwargs):
            attempts.append(provider)
            return skill_runner.SkillResult(
                skill,
                False,
                2,
                "",
                "tests failed",
                provider=provider,
            )

        with (
            patch(
                "skill_runner._provider_binaries",
                return_value={
                    "claude": "/bin/claude",
                    "codex": "/bin/codex",
                    "cursor": None,
                },
            ),
            patch("skill_runner._run_skill_once", side_effect=fake_once),
        ):
            result = asyncio.run(skill_runner.run_skill("gryllus", "work"))

        assert not result.ok
        assert attempts == ["claude"]

    def test_successful_answer_that_mentions_limits_is_not_replayed(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "claude,codex")
        harness_router.reset_state()
        attempts: list[str] = []

        async def fake_once(skill, prompt, *, provider, **kwargs):
            attempts.append(provider)
            return skill_runner.SkillResult(
                skill,
                True,
                0,
                "The implementation documents its usage limit policy.",
                "",
                provider=provider,
            )

        with (
            patch(
                "skill_runner._provider_binaries",
                return_value={
                    "claude": "/bin/claude",
                    "codex": "/bin/codex",
                    "cursor": None,
                },
            ),
            patch("skill_runner._run_skill_once", side_effect=fake_once),
        ):
            result = asyncio.run(skill_runner.run_skill("gryllus", "work"))

        assert result.ok
        assert attempts == ["claude"]


# ── ateles#590: codex sandbox must reach the gitdir, and a denied delivery ─────
#    must be reported as a failure rather than as ok:true over nothing.


class TestCodexSandboxGitRoots:
    """`codex exec --sandbox workspace-write` in a LINKED WORKTREE.

    The swarm dispatches every agent into its own worktree (ateles#572, and the
    repo-isolation guard). A linked worktree keeps its real gitdir in the
    parent clone, outside the sandbox, so without an explicit grant the child
    writes correct code and then cannot commit it.
    """

    def _worktree(self, tmp_path):
        """A real main clone plus a real linked worktree off it."""
        main = tmp_path / "main-clone"
        main.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
        subprocess.run(
            ["git", "-C", str(main), "config", "user.email", "t@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(main), "config", "user.name", "T"], check=True
        )
        (main / "seed.txt").write_text("seed\n")
        subprocess.run(["git", "-C", str(main), "add", "seed.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(main), "commit", "-q", "-m", "seed"], check=True
        )
        wt = tmp_path / "linked-wt"
        subprocess.run(
            ["git", "-C", str(main), "worktree", "add", "-q", str(wt), "-b", "wt"],
            check=True,
        )
        return main, wt

    def test_linked_worktree_git_roots_are_granted(self, tmp_path) -> None:
        """Both the per-worktree gitdir AND the common dir must be granted.

        Granting only one is not enough and this is verified, not assumed:
        index.lock lives under the per-worktree gitdir while loose objects are
        written under the common dir, so `git add` fails without the latter and
        `git commit` fails without the former.
        """
        main, wt = self._worktree(tmp_path)
        roots = skill_runner._git_roots_for_sandbox(str(wt))

        common = (main / ".git").resolve()
        per_wt = (common / "worktrees" / "linked-wt").resolve()
        assert str(per_wt) in roots, f"per-worktree gitdir missing from {roots}"
        assert str(common) in roots, f"common gitdir missing from {roots}"

    def test_codex_command_carries_add_dir_for_each_git_root(self, tmp_path) -> None:
        """The grants must actually reach the codex argv as --add-dir flags."""
        main, wt = self._worktree(tmp_path)
        cmd, _ = skill_runner._provider_command(
            "codex", "/bin/codex", "system", "work", cwd=str(wt)
        )

        granted = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--add-dir"]
        common = str((main / ".git").resolve())
        per_wt = str((main / ".git" / "worktrees" / "linked-wt").resolve())
        assert common in granted, f"--add-dir missing common gitdir: {cmd}"
        assert per_wt in granted, f"--add-dir missing worktree gitdir: {cmd}"

    def test_codex_command_enables_network_only_when_asked(self, tmp_path) -> None:
        """workspace-write denies network by default, so git push / gh cannot
        resolve github.com. Delivery needs it enabled — but ONLY for a dispatch
        that delivers.

        #590 asks for this "without granting blanket network access to every
        dispatch", and the PR body's own alternatives table rejects the
        widening; granting it unconditionally contradicted both (#601 pm lens).
        """
        _, wt = self._worktree(tmp_path)
        flag = "sandbox_workspace_write.network_access=true"

        off, _ = skill_runner._provider_command(
            "codex", "/bin/codex", "system", "work", cwd=str(wt)
        )
        assert flag not in off, "network must be denied by default"

        on, _ = skill_runner._provider_command(
            "codex", "/bin/codex", "system", "work", cwd=str(wt), network=True
        )
        assert flag in on, "a delivery-bearing dispatch still gets network"

    def test_sandbox_is_not_widened_to_full_access(self, tmp_path) -> None:
        """The fix must stay inside workspace-write. Reaching for
        --sandbox danger-full-access would buy the same commit at the cost of
        all filesystem confinement, everywhere on the operator's machine."""
        _, wt = self._worktree(tmp_path)
        cmd, _ = skill_runner._provider_command(
            "codex", "/bin/codex", "system", "work", cwd=str(wt)
        )
        assert "workspace-write" in cmd
        assert "danger-full-access" not in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" not in cmd

    def test_plain_clone_needs_no_extra_grant(self, tmp_path) -> None:
        """A plain clone's .git is inside the workdir, already writable. Do not
        widen the sandbox for a path the sandbox already contains."""
        main, _ = self._worktree(tmp_path)
        assert skill_runner._git_roots_for_sandbox(str(main)) == []

    def test_non_repo_cwd_yields_no_roots(self, tmp_path) -> None:
        """A dispatch into a non-repo is legitimate and needs no git roots."""
        plain = tmp_path / "not-a-repo"
        plain.mkdir()
        assert skill_runner._git_roots_for_sandbox(str(plain)) == []

    def test_no_cwd_yields_no_roots(self) -> None:
        assert skill_runner._git_roots_for_sandbox(None) == []


class TestDeliveryFailureIsReportedAsFailure:
    """The false `ok: true` (ateles#590).

    A child denied its commit or push exits 0 — it did everything it was
    permitted to do. Judging the run on the exit code alone reports success
    over an undelivered change. Same class as ateles#585 (envelope never
    written) and ateles#566 (401 reported ok).
    """

    def setup_method(self) -> None:
        skill_runner._agent_def_cache.clear()

    # The exact strings a real sandboxed codex child emitted, captured from a
    # live reproduction against a linked worktree.
    INDEX_LOCK = (
        "fatal: Unable to create '/Users/x/repos/ateles/.git/worktrees/wt/"
        "index.lock': Operation not permitted"
    )
    OBJECT_STORE = (
        "error: unable to create temporary file: Operation not permitted\n"
        "error: unable to index file 'f.txt'\nfatal: adding files failed"
    )
    NO_NETWORK = (
        "fatal: unable to access 'https://github.com/markmhendrickson/ateles/': "
        "Could not resolve host: github.com"
    )

    @pytest.mark.parametrize(
        "blob",
        [INDEX_LOCK, OBJECT_STORE, NO_NETWORK],
        ids=["index_lock", "object_store", "no_network"],
    )
    def test_denial_signature_is_recognised(self, blob) -> None:
        assert skill_runner._delivery_failure_reason(blob) is not None

    def test_ordinary_output_is_not_a_denial(self) -> None:
        """No false positives on a run that simply never tried to commit."""
        assert (
            skill_runner._delivery_failure_reason(
                "Wrote 3 files. 12 passed in 1.2s. Done."
            )
            is None
        )

    def _dispatch_with_child_output(
        self, stdout: bytes, returncode: int = 0, stderr: bytes = b""
    ):
        """run_skill against a fake child that prints `stdout` and exits 0.

        `stderr` is where git actually writes its denial lines, and is the only
        stream the detector reads — see test_quoted_denial_on_stdout_is_not_a_denial.
        """
        instance = MagicMock()
        instance.load.return_value = _make_def()

        async def fake_exec(*cmd, **kwargs):
            proc = MagicMock()
            proc.returncode = returncode

            async def _communicate(input=None):
                return stdout, stderr

            proc.communicate = _communicate
            return proc

        with (
            patch("skill_runner._write_harness_event"),
            patch("skill_runner.AgentLoader", return_value=instance),
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch("skill_runner.write_dispatch_failure_log", return_value=None),
            patch("skill_runner.notify_dispatch_failure"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value="SKILL"),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            return asyncio.run(
                skill_runner.run_skill("gryllus", "work", role="gryllus")
            )

    def test_rc_zero_with_denied_commit_reports_not_ok(self) -> None:
        """THE defect: rc=0, real work done, nothing delivered, ok:true."""
        result = self._dispatch_with_child_output(
            b"Added the function and the tests; 12 passed.\n",
            stderr=self.INDEX_LOCK.encode(),
        )
        assert result.ok is False, "a dispatch that could not commit reported ok"
        assert result.returncode == 0
        assert "commit" in result.error.lower()

    def test_rc_zero_with_denied_push_reports_not_ok(self) -> None:
        result = self._dispatch_with_child_output(
            b"Committed locally.\n", stderr=self.NO_NETWORK.encode()
        )
        assert result.ok is False
        assert result.error, "ok:false must always carry a reason"

    def test_quoted_denial_on_stdout_is_not_a_denial(self) -> None:
        """An agent that READS about a denial has not suffered one.

        The detector searched a joined stdout+stderr blob for the signatures
        wherever they appeared, so an agent dispatched to read ateles#601, its
        diff, or an issue quoting the reproduction was reported as a failed
        delivery — and, because that flips ok to False on an rc=0 run, its whole
        stdout then reached the capacity/auth classifier, which could cool a
        provider and replay side-effecting work on the operator's other quota
        (#601, two lenses compounding).

        git writes these lines to stderr; quoted prose arrives on stdout.
        """
        quoted = (
            b"I read the PR. It reports:\n"
            + self.INDEX_LOCK.encode()
            + b"\nand explains the --add-dir fix. Nothing to change.\n"
        )
        result = self._dispatch_with_child_output(quoted, stderr=b"")

        assert result.ok is True, "quoting a denial is not being denied"
        assert result.error == ""

    def test_clean_run_still_reports_ok(self) -> None:
        """The guard must not turn every successful dispatch into a failure."""
        result = self._dispatch_with_child_output(b"All done. 12 passed.\n")
        assert result.ok is True
        assert result.error == ""


# ── Per-dispatch usage attribution (model + tokens) ───────────────────────────


class TestDispatchUsageRecording:
    """The harness_event written per dispatch must carry model/provider/token
    attribution, so a spend can be traced to the role and task that caused it.

    These are integration tests through run_skill: the parsing itself is
    covered against real captured CLI output in test_dispatch_usage.py.
    """

    def setup_method(self) -> None:
        skill_runner._agent_def_cache.clear()

    def _run(self, coro):
        return asyncio.run(coro)

    def _dispatch(self, mock_write_harness, MockLoader, stdout: bytes, rc: int = 0):
        instance = MagicMock()
        instance.load.return_value = _make_def()
        MockLoader.return_value = instance

        async def fake_exec(*cmd, **kwargs):
            proc = MagicMock()
            proc.returncode = rc

            async def _communicate(input=None):
                return stdout, b""

            proc.communicate = _communicate
            return proc

        with (
            patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value="skill md"),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            return self._run(
                skill_runner.run_skill(
                    "gryllus",
                    "work prompt",
                    role="gryllus",
                    task_entity_id="ent_test",
                )
            )

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_usage_is_attached_to_the_success_event(
        self, MockLoader, mock_write_harness
    ) -> None:
        json_out = (
            b'{"usage":{"input_tokens":1200,"output_tokens":340,'
            b'"cache_read_input_tokens":50},"total_cost_usd":0.04,'
            b'"modelUsage":{"claude-opus-5":{"outputTokens":340}},"type":"result"}'
        )
        result = self._dispatch(mock_write_harness, MockLoader, json_out)
        assert result.ok

        success = [
            c
            for c in mock_write_harness.call_args_list
            if c.kwargs.get("success") == "true"
        ]
        assert success, "expected a success harness_event"
        usage = success[-1].kwargs.get("usage")
        assert usage is not None, "success event must carry usage"
        fields = usage.as_event_fields()
        assert fields["model"] == "claude-opus-5"
        assert fields["model_source"] == "reported"
        assert fields["input_tokens"] == 1200
        assert fields["output_tokens"] == 340
        assert fields["provider"] == "claude"

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_usage_is_exposed_on_the_result_for_callers(
        self, MockLoader, mock_write_harness
    ) -> None:
        json_out = (
            b'{"usage":{"input_tokens":10,"output_tokens":2},'
            b'"modelUsage":{"claude-opus-5":{"outputTokens":2}},"type":"result"}'
        )
        result = self._dispatch(mock_write_harness, MockLoader, json_out)
        assert result.usage is not None
        assert result.usage.model == "claude-opus-5"
        assert result.usage.total_tokens == 12

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_text_mode_dispatch_records_provider_without_fabricating_tokens(
        self, MockLoader, mock_write_harness
    ) -> None:
        """The swarm currently runs text mode. Provider attribution must still
        be recorded, and token fields must stay absent rather than read zero."""
        result = self._dispatch(
            mock_write_harness, MockLoader, b"Opened PR #12 as requested.\n"
        )
        assert result.ok
        usage = [
            c
            for c in mock_write_harness.call_args_list
            if c.kwargs.get("success") == "true"
        ][-1].kwargs["usage"]
        fields = usage.as_event_fields()
        assert fields["provider"] == "claude"
        assert "input_tokens" not in fields
        assert "total_tokens" not in fields

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_failed_dispatch_still_records_usage(
        self, MockLoader, mock_write_harness
    ) -> None:
        """A failed dispatch still spent tokens; recording only successes would
        under-count exactly the runs most likely to have burned a retry loop."""
        json_out = (
            b'{"usage":{"input_tokens":900,"output_tokens":5},'
            b'"modelUsage":{"claude-opus-5":{"outputTokens":5}},"type":"result"}'
        )
        result = self._dispatch(mock_write_harness, MockLoader, json_out, rc=1)
        assert not result.ok
        failures = [
            c
            for c in mock_write_harness.call_args_list
            if c.kwargs.get("success") == "false"
        ]
        assert failures
        usage = failures[-1].kwargs.get("usage")
        assert usage is not None
        assert usage.input_tokens == 900


class TestRequestedModelFromArgv:
    """`_requested_model` reads argv so it stays correct regardless of which
    layer sets the model (nothing does today; ateles#667 adds `--model`)."""

    def test_reads_double_dash_model(self) -> None:
        assert (
            skill_runner._requested_model(
                "cursor", ["cursor-agent", "--model", "composer-2.5", "--print"]
            )
            == "composer-2.5"
        )

    def test_reads_short_flag(self) -> None:
        assert (
            skill_runner._requested_model("codex", ["codex", "exec", "-m", "gpt-5.3"])
            == "gpt-5.3"
        )

    def test_reads_equals_spelling(self) -> None:
        assert (
            skill_runner._requested_model("cursor", ["cursor-agent", "--model=composer-2.5"])
            == "composer-2.5"
        )

    def test_returns_none_when_unpinned(self) -> None:
        """Today's actual behaviour: no model flag, so the provider default runs."""
        assert skill_runner._requested_model("claude", ["claude", "--print"]) is None

    def test_handles_trailing_flag_without_value(self) -> None:
        assert skill_runner._requested_model("cursor", ["cursor-agent", "--model"]) is None
# ── Prior-art contract (check existing context before building) ───────────────
#
# The contract exists because dispatched agents rebuilt work that already
# existed (provider load balancing already in harness_router.py; an issue filed
# against a clone 139 commits behind main). It rides the same injection path as
# SWARM_GITHUB_CONTRACT deliberately — no separate flag, because a second flag
# would just relocate the forgetting it exists to prevent.


class TestPriorArtContract:
    def test_absent_by_default(self) -> None:
        """Default path must not carry the contract — the non-GitHub/SSE task
        path stays byte-identical to before."""
        prompt, degraded = skill_runner.build_system_prompt(
            _make_def(prompt_markdown="Agent identity."), "Do the task."
        )
        assert not degraded
        assert skill_runner.SWARM_PRIOR_ART_CONTRACT not in prompt

    def test_present_when_contract_flag_true(self) -> None:
        """Rides the SAME flag as the GitHub contract: one flag, both contracts.

        This is the load-bearing assertion of the whole change. If someone later
        gives the prior-art contract its own opt-in flag, this test fails — and
        it should, because an opt-in prior-art check is one a brief author can
        forget to request, which is the exact failure being fixed.
        """
        prompt, degraded = skill_runner.build_system_prompt(
            _make_def(prompt_markdown="Agent identity."),
            "Do the task.",
            include_github_contract=True,
        )
        assert not degraded
        assert skill_runner.SWARM_PRIOR_ART_CONTRACT in prompt
        assert skill_runner.SWARM_GITHUB_CONTRACT in prompt
        assert "Agent identity." in prompt
        assert "Do the task." in prompt

    def test_order_definition_then_contracts_then_skill(self) -> None:
        """Order: definition → github contract → prior-art contract → skill_md."""
        prompt, _ = skill_runner.build_system_prompt(
            _make_def(prompt_markdown="DEFINITION_ANCHOR"),
            "SKILL_ANCHOR",
            include_github_contract=True,
        )
        assert (
            prompt.index("DEFINITION_ANCHOR")
            < prompt.index(skill_runner.SWARM_GITHUB_CONTRACT)
            < prompt.index(skill_runner.SWARM_PRIOR_ART_CONTRACT)
            < prompt.index("SKILL_ANCHOR")
        )

    def test_present_when_degraded(self) -> None:
        """Degraded (no definition loaded) still gets the contract: checking for
        existing work is useful regardless of which definition loaded."""
        prompt, degraded = skill_runner.build_system_prompt(
            _stub_def(), "Fallback instructions.", include_github_contract=True
        )
        assert degraded
        assert skill_runner.SWARM_PRIOR_ART_CONTRACT in prompt
        assert "Fallback instructions." in prompt

    def test_names_all_three_checks_and_asks_for_a_report(self) -> None:
        """Content assertions on the three checks that each caught a wasted run.

        Asserted by substance rather than by exact wording so the prose can be
        edited, but the checks themselves cannot be silently dropped.
        """
        contract = skill_runner.SWARM_PRIOR_ART_CONTRACT.lower()
        assert "gh issue list" in contract and "gh pr list" in contract
        assert "grep" in contract
        assert "task" in contract and "plan" in contract
        # The reporting requirement is what makes a skipped check visible in the
        # transcript, and is what the measurement task samples for.
        assert "report what you found" in contract
        # Correcting a wrong brief must read as success, or an agent told to
        # build something will build the duplicate anyway.
        assert "premise is wrong" in contract


# ── Design-basis contract (docs/foundation/conformance.md) ────────────────────
#
# Rides the same flag and injection point as SWARM_PRIOR_ART_CONTRACT (#686).
# The load-bearing assertion is that the DISPATCHED PROMPT changes when a
# kernel document lands in docs/foundation/: the binding is built against an
# empty directory and the documents land into a slot that already fires.


_FOUNDATION_FIXTURE = """\
# Conformance

## Always read

| Doc | What it states |
|-----|----------------|
| `docs/foundation/principles.md` | The invariants. |
| `docs/foundation/work_model.md` | How work moves. |

## Read when these paths changed

| Changed path | Read |
|---|---|
| `skill_runner` | `docs/foundation/work_model.md` |
"""


@pytest.fixture
def foundation_root(tmp_path, monkeypatch):
    fdir = tmp_path / "docs" / "foundation"
    fdir.mkdir(parents=True)
    (fdir / "conformance.md").write_text(_FOUNDATION_FIXTURE)
    monkeypatch.setenv("ATELES_FOUNDATION_ROOT", str(tmp_path))
    return tmp_path


class TestFoundationContract:
    def test_absent_by_default(self, foundation_root) -> None:
        """Default path stays byte-identical: no contracts at all."""
        prompt, _ = skill_runner.build_system_prompt(
            _make_def(prompt_markdown="Agent identity."), "Do the task."
        )
        assert skill_runner.SWARM_FOUNDATION_CONTRACT not in prompt
        assert "Kernel documents on this checkout" not in prompt

    def test_present_on_the_same_flag_after_prior_art(self, foundation_root) -> None:
        prompt, degraded = skill_runner.build_system_prompt(
            _make_def(prompt_markdown="DEFINITION_ANCHOR"),
            "SKILL_ANCHOR",
            include_github_contract=True,
        )
        assert not degraded
        assert skill_runner.SWARM_FOUNDATION_CONTRACT in prompt
        assert (
            prompt.index("DEFINITION_ANCHOR")
            < prompt.index(skill_runner.SWARM_GITHUB_CONTRACT)
            < prompt.index(skill_runner.SWARM_PRIOR_ART_CONTRACT)
            < prompt.index(skill_runner.SWARM_FOUNDATION_CONTRACT)
            < prompt.index("SKILL_ANCHOR")
        )
        # The dynamic half names what is actually on the checkout.
        assert "`docs/foundation/principles.md` — not yet written" in prompt
        assert "`docs/foundation/work_model.md` — not yet written" in prompt

    def test_fires_when_a_kernel_doc_lands(self, foundation_root) -> None:
        """THE test: same call, before and after a document appears."""
        (foundation_root / "docs" / "foundation" / "principles.md").write_text(
            "# Principles\n\n## Purpose\n\nSENTINEL-PRINCIPLES-PURPOSE.\n"
        )
        prompt, _ = skill_runner.build_system_prompt(
            _make_def(prompt_markdown="Agent identity."),
            "Do the task.",
            include_github_contract=True,
        )
        assert "`docs/foundation/principles.md` — SENTINEL-PRINCIPLES-PURPOSE." in prompt
        assert "`docs/foundation/work_model.md` — not yet written" in prompt

    def test_present_when_degraded(self, foundation_root) -> None:
        prompt, degraded = skill_runner.build_system_prompt(
            _stub_def(), "Fallback instructions.", include_github_contract=True
        )
        assert degraded
        assert skill_runner.SWARM_FOUNDATION_CONTRACT in prompt

    def test_absent_on_a_checkout_with_no_reading_list(self, tmp_path, monkeypatch) -> None:
        """No conformance.md → nothing to bind to → the contract is not
        injected, and the prior-art contract is unaffected."""
        monkeypatch.setenv("ATELES_FOUNDATION_ROOT", str(tmp_path))
        prompt, _ = skill_runner.build_system_prompt(
            _make_def(prompt_markdown="Agent identity."),
            "Do the task.",
            include_github_contract=True,
        )
        assert skill_runner.SWARM_FOUNDATION_CONTRACT not in prompt
        assert skill_runner.SWARM_PRIOR_ART_CONTRACT in prompt

    def test_real_checkout_carries_the_contract(self, monkeypatch) -> None:
        """This repo now has the reading list, so a real dispatch carries the
        contract naming it (the kernel documents are not yet written, and the
        prompt says so rather than pretending)."""
        monkeypatch.delenv("ATELES_FOUNDATION_ROOT", raising=False)
        prompt, _ = skill_runner.build_system_prompt(
            _make_def(prompt_markdown="Agent identity."),
            "Do the task.",
            include_github_contract=True,
        )
        assert "docs/foundation/conformance.md" in prompt
        assert "Kernel documents on this checkout" in prompt

    def test_contract_states_the_rule(self) -> None:
        text = skill_runner.SWARM_FOUNDATION_CONTRACT
        assert "Design basis:" in text
        assert "no design applies" in text
        assert "docs/foundation/conformance.md" in text
