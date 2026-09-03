"""
Call-site effect test for Formica's T4 dispatch argv (ateles#696 review round 1,
pavo lens — "no-bypass direct" category).

Formica is one of the direct spawners that builds its own `claude --print`
argv rather than going through skill_runner. This pins the actual behavioural
claim the PR makes for this call site: under ENFORCE, `--allowed-tools`
appears in argv for a restricted allowlist; under the default (LOG_ONLY),
it does not; and Formica never passes --dangerously-skip-permissions at all
(unlike cotinga/monedula), so an allowlist here can genuinely bind once
enforcement is switched on.

Asserting on the built argv — not on a log line or on tool_allowlist.py's own
unit tests — is the point: those pin the shared module's behaviour in
isolation, not that Formica's call site actually wires it in.

Run with: pytest execution/daemons/formica/test_tool_allowlist_dispatch.py -v
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import formica  # noqa: E402


class _FakeLoader:
    """Stands in for AgentLoader(skill).load().tools without touching Neotoma."""

    def __init__(self, name):
        self.name = name

    def load(self):
        class _D:
            tools = ["Bash", "Read", "Write"]

        return _D()


class _FakeProc:
    returncode = 0

    async def communicate(self, input=None):
        return b"", b""


def _capture_dispatch_argv(monkeypatch, tmp_path, *, enforce: bool) -> list[str]:
    """Drive _spawn_claude_skill far enough to capture the argv it builds."""
    monkeypatch.setattr(formica.shutil, "which", lambda _bin: None)
    monkeypatch.setattr(formica, "CLAUDE_BIN", "/usr/bin/claude")
    monkeypatch.setattr(formica, "AgentLoader", _FakeLoader)

    skill_dir = tmp_path / ".claude" / "skills" / "cicada"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("SKILL")
    monkeypatch.setenv("ATELES_REPO_PATH", str(tmp_path))
    if enforce:
        monkeypatch.setenv("ATELES_ENFORCE_TOOL_ALLOWLIST", "1")
    else:
        monkeypatch.delenv("ATELES_ENFORCE_TOOL_ALLOWLIST", raising=False)

    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured["cmd"] = list(args)
        return _FakeProc()

    monkeypatch.setattr(formica.asyncio, "create_subprocess_exec", fake_exec)

    class _Notifier:
        def send(self, *a, **k):
            pass

    asyncio.run(
        formica._spawn_claude_skill(
            "cicada", "ent_issue1", {"title": "T", "repo": "markmhendrickson/ateles"}, _Notifier()
        )
    )
    return captured.get("cmd", [])


def test_formica_dispatch_binds_allowlist_under_enforce(monkeypatch, tmp_path):
    """Under ENFORCE, Formica's argv must carry --allowed-tools for a
    restricted skill allowlist — the effect the PR claims to fix, not just
    that plan_enforcement() reports 'enforced' in isolation."""
    cmd = _capture_dispatch_argv(monkeypatch, tmp_path, enforce=True)
    assert "--allowed-tools" in cmd, f"expected --allowed-tools in argv, got: {cmd}"
    assert "--dangerously-skip-permissions" not in cmd


def test_formica_dispatch_omits_allowlist_by_default(monkeypatch, tmp_path):
    """Default posture (no env var set) is LOG_ONLY: argv must NOT carry
    --allowed-tools even though the skill has a restricted allowlist."""
    cmd = _capture_dispatch_argv(monkeypatch, tmp_path, enforce=False)
    assert "--allowed-tools" not in cmd, f"expected no --allowed-tools in argv, got: {cmd}"
