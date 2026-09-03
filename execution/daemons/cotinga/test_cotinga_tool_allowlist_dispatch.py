"""
Call-site effect test for Cotinga's deep-prep dispatch argv (ateles#696 review
round 1, pavo lens — "bypass direct" category).

Cotinga's `_agent_argv` always passes --dangerously-skip-permissions (a
headless launchd dispatch has no TTY to answer permission prompts). Per the
CLI's own permission-flow ordering documented in tool_allowlist.py, that
bypass short-circuits the allow-rule check BEFORE it is ever read — so
--allowed-tools would be a silent no-op here even under ENFORCE. The claim
under test is exactly that: under ENFORCE, Cotinga's argv still carries NO
--allowed-tools (would be defeated, not fake-confined) AND still carries
--dangerously-skip-permissions (load-bearing for headless dispatch, left in
place deliberately) — i.e. plan_enforcement's "defeated" status is honoured
at this call site, not merely returned in isolation by tool_allowlist.py's
own unit tests.

Run with: pytest execution/daemons/cotinga/test_tool_allowlist_dispatch.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cotinga  # noqa: E402


class _FakeLoader:
    def __init__(self, name):
        self.name = name

    def load(self):
        class _D:
            tools = ["Bash", "Read", "Write"]

        return _D()


def test_cotinga_dispatch_stays_unconfined_and_bypassed_under_enforce(monkeypatch):
    """Even with ENFORCE on, a restricted allowlist must NOT reach argv here
    (bypass defeats it) — and --dangerously-skip-permissions must still be
    present, since it is load-bearing for headless dispatch and is not
    removed just because enforcement is on."""
    monkeypatch.setattr(cotinga, "AgentLoader", _FakeLoader)
    monkeypatch.setenv("ATELES_ENFORCE_TOOL_ALLOWLIST", "1")

    argv = cotinga._agent_argv("/usr/bin/claude", "the prompt")

    assert "--allowed-tools" not in argv, (
        f"expected no --allowed-tools under bypass (defeated), got: {argv}"
    )
    assert "--dangerously-skip-permissions" in argv, (
        f"expected --dangerously-skip-permissions to remain in argv, got: {argv}"
    )
    assert argv[-1] == "the prompt"


def test_cotinga_dispatch_stays_unconfined_by_default(monkeypatch):
    """Default posture (LOG_ONLY) — argv must also carry no --allowed-tools,
    and the bypass flag is present either way."""
    monkeypatch.setattr(cotinga, "AgentLoader", _FakeLoader)
    monkeypatch.delenv("ATELES_ENFORCE_TOOL_ALLOWLIST", raising=False)

    argv = cotinga._agent_argv("/usr/bin/claude", "the prompt")

    assert "--allowed-tools" not in argv
    assert "--dangerously-skip-permissions" in argv
