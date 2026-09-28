"""Evals for Tyto's process-meeting skill path + prompt contract (ateles#1029).

Production path resolution in tyto.py is already correct; these tests lock the
default/legacy/alias/missing behaviour and assert the assembled Claude prompt
does not ask for Gmail draft staging.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tyto  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_signing_identity(monkeypatch):
    monkeypatch.setattr(
        tyto,
        "agent_identity",
        lambda _name: {
            "key": "/fixture/tyto.jwk.json",
            "sub": "tyto@ateles-swarm",
            "kid": "fixture",
        },
        raising=False,
    )
    monkeypatch.setattr(tyto, "_verify_transcription_cli_runtime", lambda _env: None)


def _reload_tyto_with_env(monkeypatch, **env: str | None):
    """Re-import tyto after clearing/setting skill-path env vars."""
    for key in ("TYTO_PROCESS_MEETING_SKILL", "TYTO_ANALYZE_MEETING_SKILL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    return importlib.reload(tyto)


def test_default_process_meeting_skill_path(monkeypatch):
    mod = _reload_tyto_with_env(monkeypatch)
    expected = mod._REPO_ROOT / ".claude" / "skills" / "process-meeting" / "SKILL.md"
    assert mod.PROCESS_MEETING_SKILL_PATH == expected
    assert str(mod.PROCESS_MEETING_SKILL_PATH).endswith(
        ".claude/skills/process-meeting/SKILL.md"
    )


def test_legacy_analyze_meeting_env_wins_when_new_unset(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy-skill.md"
    legacy.write_text("# legacy")
    mod = _reload_tyto_with_env(monkeypatch, TYTO_ANALYZE_MEETING_SKILL=str(legacy))
    assert mod.PROCESS_MEETING_SKILL_PATH == legacy


def test_analyze_alias_is_process_meeting_path(monkeypatch):
    mod = _reload_tyto_with_env(monkeypatch)
    assert mod.ANALYZE_MEETING_SKILL_PATH is mod.PROCESS_MEETING_SKILL_PATH


def test_missing_skill_warns_and_returns_without_subprocess(tmp_path, monkeypatch, caplog):
    missing = tmp_path / "missing-skill.md"
    monkeypatch.setattr(tyto, "PROCESS_MEETING_SKILL_PATH", missing)
    monkeypatch.setattr(tyto, "ANALYZE_MEETING_SKILL_PATH", missing)
    monkeypatch.setattr(tyto, "_find_claude_bin", lambda: "/usr/bin/claude")
    run = MagicMock()
    monkeypatch.setattr(tyto.subprocess, "run", run)
    notifier = MagicMock()

    with caplog.at_level(logging.WARNING):
        tyto._run_analysis(tmp_path / "20260101 1200 remote.mp4", None, notifier)

    assert run.call_count == 0
    assert any("process-meeting skill not found" in r.message for r in caplog.records)


def test_run_analysis_prompt_has_process_meeting_not_gmail_drafts(tmp_path, monkeypatch):
    skill = tmp_path / "SKILL.md"
    skill.write_text("# process-meeting fixture\n")
    monkeypatch.setattr(tyto, "PROCESS_MEETING_SKILL_PATH", skill)
    monkeypatch.setattr(tyto, "ANALYZE_MEETING_SKILL_PATH", skill)
    # Avoid injecting the real analyze-neotoma-feedback body into the prompt.
    monkeypatch.setattr(tyto, "ANALYZE_NEOTOMA_SKILL_PATH", tmp_path / "absent.md")
    monkeypatch.setattr(tyto, "_find_claude_bin", lambda: "/usr/bin/claude")

    captured: dict[str, str] = {}

    def _fake_run(cmd, input=None, **kwargs):
        captured["prompt"] = input or ""
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(tyto.subprocess, "run", _fake_run)
    notifier = MagicMock()

    tyto._run_analysis(tmp_path / "20260101 1200 remote.mp4", "ent_fixture", notifier)

    prompt = captured["prompt"]
    assert "/process-meeting" in prompt
    assert "stage Gmail recap drafts" not in prompt
    assert "gws gmail draft" not in prompt.lower()
