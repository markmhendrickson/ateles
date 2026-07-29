"""Regression fixture pinning a KNOWN pre-existing drift found during #214's
manual policy-vs-behavior check (QA §2): `.claude/hooks/agent_auto_invocation.py`
is fully implemented but is NOT wired into `.claude/settings.json`'s
UserPromptSubmit block, so it never fires.

This test intentionally asserts the CURRENT (drifted) state so the drift is
captured, not silently compounded or "fixed" under #214's scope (out of
scope per PM/QA). Fixing the wiring is tracked as a separate followup issue.
If this test starts failing because someone wires the hook up, that's
good — update/delete this fixture as part of that followup, don't just
patch it to keep passing.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SETTINGS_PATH = REPO_ROOT / ".claude" / "settings.json"
ADAPTER_PATH = REPO_ROOT / ".claude" / "hooks" / "agent_auto_invocation.py"


def test_agent_auto_invocation_file_exists_but_is_unwired():
    assert ADAPTER_PATH.exists(), "agent_auto_invocation.py should still exist on disk"
    settings = json.loads(SETTINGS_PATH.read_text())
    prompt_submit_hooks = settings.get("hooks", {}).get("UserPromptSubmit", [])
    commands = [
        h.get("command", "")
        for entry in prompt_submit_hooks
        for h in entry.get("hooks", [])
    ]
    wired = any("agent_auto_invocation.py" in c for c in commands)
    assert not wired, (
        "agent_auto_invocation.py is now wired into settings.json — this "
        "pins a KNOWN drift; if it's been fixed, close the followup issue "
        "and delete this fixture instead of leaving it stale."
    )
