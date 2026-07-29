"""Regression + effect tests for hooks/logic/spawn_task_neotoma_pairing.py.

Covers QA §1 (P0/P1/P2 baseline parity + no-op) and QA §4 (P0 adapter
exception handling) for the pilot hook.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[3]
ADAPTER = REPO_ROOT / ".claude" / "hooks" / "spawn_task_neotoma_pairing.py"

sys.path.insert(0, str(REPO_ROOT))
from hooks.logic.spawn_task_neotoma_pairing import handle  # noqa: E402


def _load_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_baseline_parity():
    """Effect test: the pinned baseline event produces the pinned reminder
    text, byte-for-byte. This is the reported effect — a durable reminder to
    create the paired Neotoma task — not merely 'the function returns
    something'."""
    event = _load_json("spawn_task_baseline_event.json")
    expected = (FIXTURES / "spawn_task_baseline_reminder.txt").read_text().rstrip("\n")
    assert handle(event) == expected


def test_no_task_chip_signal_is_noop():
    """Negative case: an event with no spawn_task signal must not start
    emitting a reminder where none existed before."""
    assert handle({"tool_name": "Bash", "tool_input": {"command": "ls"}}) is None
    assert handle({}) is None


def test_missing_title_and_task_id_still_reminds():
    event = {"tool_name": "mcp__ccd_session__spawn_task", "tool_input": {}, "tool_response": {}}
    result = handle(event)
    assert result is not None
    assert "spawned a task chip" in result


def test_adapter_catches_logic_exception():
    """Adapter-level: a malformed event that makes the logic module raise must
    be caught by the adapter and surfaced as a single-line additionalContext
    naming the hook + exception — never a bare traceback."""
    malformed = (FIXTURES / "malformed_event.json").read_text()
    proc = subprocess.run(
        [sys.executable, str(ADAPTER)],
        input=malformed,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0  # fail open
    assert "Traceback (most recent call last)" not in proc.stdout
    assert "Traceback (most recent call last)" not in proc.stderr
    payload = json.loads(proc.stdout)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "spawn_task_neotoma_pairing" in ctx
    assert "hook error" in ctx


def test_adapter_stdout_no_credential_leakage():
    """Security: adapter output must never contain bearer-token/key-like
    substrings, since hook stdout is Claude Code transcript content the
    operator may paste elsewhere."""
    event = _load_json("spawn_task_baseline_event.json")
    proc = subprocess.run(
        [sys.executable, str(ADAPTER)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0
    lowered = proc.stdout.lower()
    for needle in ("bearer ", "authorization:", "sk-", "neotoma_bearer_token"):
        assert needle not in lowered
