"""`inference_only` — a run that can only produce text (ateles#1142 follow-up).

The producer scorer reads untrusted task text, so its child must not be able to
do anything but answer. The confinement is a property of the command line and
environment `skill_runner` builds, so these tests capture the REAL command and
environment from `run_skill`/`_run_skill_once` and assert on them, with canaries
seeded in the ambient environment:

* an ambient MCP server configuration naming a canary server must not be
  reachable (the child gets an empty strict MCP configuration and an isolated
  HOME, never the ambient one);
* an ambient Codex home must not be inherited;
* no tool may be enabled at all, so a write outside the scratch directory has
  no tool to be made with;
* any provider other than the one that can run tool-free is refused before a
  process is created.

Limit: this proves what we ask the CLI to do, not what the CLI does with it; it
does not launch a model.

Run: pytest execution/daemons/apis/test_inference_only_isolation.py -v
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness_router  # noqa: E402
import skill_runner  # noqa: E402
from lib.daemon_runtime import AgentDefinition  # noqa: E402


@pytest.fixture(autouse=True)
def _router(monkeypatch, tmp_path):
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "claude,codex,cursor")
    monkeypatch.setenv(
        "APIS_HARNESS_HEADROOM", '{"claude": 1.0, "codex": 1.0, "cursor": 1.0}'
    )
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "none.json"))
    harness_router.reset_state()
    yield
    harness_router.reset_state()


def _agent_def() -> AgentDefinition:
    return AgentDefinition(
        entity_id="ent_test",
        name="apis",
        prompt_markdown="Dispatcher.",
        tool_allowlist="*",  # the broadest allowlist: confinement must not rely on it
        aauth_sub="apis@ateles-swarm",
    )


@pytest.fixture
def canaries(monkeypatch, tmp_path):
    """Seed the ambient environment with things the child must never see."""
    ambient_home = tmp_path / "ambient-home"
    ambient_home.mkdir()
    marker = tmp_path / "canary-mcp-was-started"
    (ambient_home / ".claude.json").write_text(
        json.dumps(
            {"mcpServers": {"canary": {"command": "touch", "args": [str(marker)]}}}
        )
    )
    monkeypatch.setenv("HOME", str(ambient_home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "ambient-codex"))
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", "canary-token")
    monkeypatch.setenv("GITHUB_TOKEN", "canary-gh")
    return {"home": ambient_home, "marker": marker, "codex": tmp_path / "ambient-codex"}


def _run(tmp_path, **kw):
    """Run `run_skill` with the process launch captured; return (result, cap)."""
    cap: dict = {"launched": False}
    review_home = tmp_path / "scratch"
    review_home.mkdir(exist_ok=True)

    async def fake_exec(*cmd, **kwargs):
        cap["launched"] = True
        cap["cmd"] = list(cmd)
        cap["env"] = kwargs["env"]
        cap["cwd"] = kwargs.get("cwd")
        mcp = Path(cmd[cmd.index("--mcp-config") + 1])
        with mcp.open(encoding="utf-8") as handle:  # Path.read_text is patched
            cap["mcp"] = json.load(handle)
        proc = MagicMock()
        proc.returncode = 0

        async def _communicate(input=None):
            return b'{"confidence": 0.5}', b""

        proc.communicate = _communicate
        return proc

    instance = MagicMock()
    instance.load.return_value = _agent_def()
    skill_runner._agent_def_cache.clear()
    args = dict(
        provider="claude",
        local_review=True,
        inference_only=True,
        env_extra={"ATELES_LOCAL_REVIEW_HOME": str(review_home)},
        cwd=str(review_home),
    )
    args.update(kw)
    with (
        patch("skill_runner.AgentLoader", return_value=instance),
        patch("skill_runner._write_harness_event"),
        patch("skill_runner.CLAUDE_BIN", "/usr/bin/claude"),
        patch("skill_runner.CODEX_BIN", "/usr/bin/codex"),
        patch("skill_runner.CURSOR_BIN", "/usr/bin/cursor-agent"),
        patch.object(Path, "exists", return_value=True),
        patch.object(Path, "read_text", return_value="skill content"),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
    ):
        result = asyncio.run(skill_runner.run_skill("apis", "score this", role="apis", **args))
    return result, cap, review_home


def test_no_tool_of_any_kind_is_enabled(tmp_path, canaries):
    result, cap, _ = _run(tmp_path)
    assert result.ok
    cmd = cap["cmd"]
    # `--tools ""` is the whole built-in tool set switched off.
    assert cmd[cmd.index("--tools") + 1] == ""
    # Nothing re-enables a tool: no allowlist, however broad the role's own is.
    assert "--allowed-tools" not in cmd and "--allowedTools" not in cmd
    assert "--dangerously-skip-permissions" not in cmd


def test_the_ambient_mcp_canary_is_not_reachable(tmp_path, canaries):
    _, cap, review_home = _run(tmp_path)
    assert "--strict-mcp-config" in cap["cmd"]
    assert cap["mcp"] == {"mcpServers": {}}  # the canary server is not listed
    assert cap["env"]["HOME"] == str(review_home)
    assert str(canaries["home"]) not in json.dumps(cap["env"])
    assert not canaries["marker"].exists()


def test_ambient_codex_home_and_credentials_are_not_inherited(tmp_path, canaries):
    _, cap, _ = _run(tmp_path)
    env = cap["env"]
    for key in ("CODEX_HOME", "NEOTOMA_BEARER_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        assert key not in env


def test_the_child_runs_in_the_scratch_directory(tmp_path, canaries):
    _, cap, review_home = _run(tmp_path)
    assert cap["cwd"] == str(review_home)


@pytest.mark.parametrize("provider", ["codex", "cursor"])
def test_a_provider_that_cannot_run_tool_free_is_refused_before_launch(
    tmp_path, canaries, provider
):
    result, cap, _ = _run(tmp_path, provider=provider)
    assert result.ok is False
    assert cap["launched"] is False, "a provider with tools must never be launched"
    assert "inference_only" in (result.error or "")


def test_inference_only_without_local_review_is_refused(tmp_path, canaries):
    result, cap, _ = _run(tmp_path, local_review=False, env_extra=None)
    assert result.ok is False
    assert cap["launched"] is False


def test_unpinned_inference_only_never_reaches_another_provider(tmp_path, canaries):
    """With codex and cursor both eligible, an unpinned inference-only run must
    still not launch either of them."""
    result, cap, _ = _run(tmp_path, provider=None)
    if cap["launched"]:
        assert cap["cmd"][0] == "/usr/bin/claude"
    else:
        assert result.ok is False


def test_ordinary_runs_are_unchanged(tmp_path, canaries):
    """Without the flag the command is built exactly as before."""
    result, cap, _ = _run(tmp_path, inference_only=False)
    assert result.ok
    assert "--tools" not in cap["cmd"]
    assert "--allowed-tools" in cap["cmd"]
