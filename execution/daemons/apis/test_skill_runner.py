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
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── Path bootstrap (mirrors conftest.py) ──────────────────────────────────────
_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib.daemon_runtime import AgentDefinition  # noqa: E402

# Import module-level objects so we can patch them in-place
import skill_runner  # noqa: E402


# ── Helpers ────────────────────────────────────────────────────────────────────


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
    def test_composite_prompt_when_definition_loaded(self, MockLoader, mock_write_harness) -> None:
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
    def test_skill_md_only_when_no_definition(self, MockLoader, mock_write_harness) -> None:
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
    def test_degraded_produces_harness_event_with_marker(self, MockLoader, mock_write_harness) -> None:
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
    def test_tool_allowlist_applied_when_restricted(self, MockLoader, mock_write_harness) -> None:
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
    def test_wildcard_allowlist_omits_allowed_tools_flag(self, MockLoader, mock_write_harness) -> None:
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
    def test_harness_events_written_on_success(self, MockLoader, mock_write_harness) -> None:
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
            c for c in mock_write_harness.call_args_list if c.kwargs.get("success") == "true"
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
            c for c in mock_write_harness.call_args_list if c.kwargs.get("success") == "false"
        ]
        assert len(fail_calls) >= 1

    @patch("skill_runner._write_harness_event")
    @patch("skill_runner.AgentLoader")
    def test_harness_event_failure_does_not_crash_dispatch(self, MockLoader, mock_write_harness) -> None:
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
        fake_def = _make_def(prompt_markdown="Role: Gryllus.", aauth_sub="gryllus@ateles-swarm")
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

        assert captured_env.get("NEOTOMA_AAUTH_PRIVATE_JWK_PATH") == "/secrets/keys/gryllus.jwk.json", (
            "Expected NEOTOMA_AAUTH_PRIVATE_JWK_PATH='/secrets/keys/gryllus.jwk.json'"
        )
        assert captured_env.get("NEOTOMA_AAUTH_SUB") == "gryllus@ateles-swarm", (
            "Expected NEOTOMA_AAUTH_SUB='gryllus@ateles-swarm' (agent_def.aauth_sub)"
        )
        assert captured_env.get("NEOTOMA_AAUTH_ISS") == "https://markmhendrickson.com", (
            "Expected NEOTOMA_AAUTH_ISS default 'https://markmhendrickson.com'"
        )
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
        fake_def = _make_def(prompt_markdown="Role: Gryllus.", aauth_sub="gryllus@ateles-swarm")
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
            patch("asyncio.create_subprocess_exec", side_effect=self._make_exec_capturer(captured_cmd)),
            patch("os.path.exists", return_value=False),  # no JWK file
        ):
            result = self._run(
                skill_runner.run_skill(
                    "gryllus", "work prompt", role="gryllus", task_entity_id="ent_abc"
                )
            )

        assert result.ok
        assert "--mcp-config" in captured_cmd, "Expected --mcp-config in spawned command"
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
        assert len(written_contents) == 1, "Expected MCP config to be read during communicate"
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
        assert len(written_contents) == 1, "Expected MCP config to be read during communicate"
        cfg = written_contents[0]
        neotoma_cfg = cfg["mcpServers"]["mcpsrv_neotoma"]
        assert neotoma_cfg["url"] == "http://localhost:9180/mcp", (
            f"Expected url 'http://localhost:9180/mcp', got {neotoma_cfg['url']!r}"
        )
        assert neotoma_cfg.get("headers", {}).get("Authorization") == "Bearer secret-bearer-abc", (
            "Expected Authorization header with bearer token"
        )

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
            patch("asyncio.create_subprocess_exec", side_effect=self._make_exec_capturer(captured_cmd)),
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
            patch("asyncio.create_subprocess_exec", side_effect=self._make_exec_capturer(captured_cmd)),
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
        assert degraded, "Degraded flag must still be True when prompt_markdown is empty"
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
