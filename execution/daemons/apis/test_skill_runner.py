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
