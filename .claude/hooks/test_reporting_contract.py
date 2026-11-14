"""Delivery tests for the shared reporting contract."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import reporting_contract


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_contract_names_material_updates_plan_meaning_and_self_contained_final(capsys):
    assert reporting_contract.main() == 0
    text = capsys.readouterr().out
    assert "MATERIAL STATE CHANGE" in text
    assert "WHY IT MATTERS" in text
    assert "NEXT OWNER/ACTION" in text
    assert "Do not narrate each tool call" in text
    assert "final answer self-contained" in text


def test_claude_delivers_and_blocks_with_shared_scripts():
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())
    start = json.dumps(settings["hooks"]["SessionStart"])
    stop = json.dumps(settings["hooks"]["Stop"])
    assert "reporting_contract.py" in start
    assert "startup|resume|clear|compact" in start
    assert "report_quality_gate.py" in stop
    assert settings["env"]["ATELES_REPORTING_QUALITY_ENFORCE"] == "1"


def test_codex_delivers_and_blocks_with_shared_scripts():
    hooks = json.loads((REPO_ROOT / ".codex" / "hooks.json").read_text())["hooks"]
    assert "reporting_contract.py" in json.dumps(hooks["SessionStart"])
    assert "startup|resume|clear|compact" in json.dumps(hooks["SessionStart"])
    assert "report_quality_gate.py" in json.dumps(hooks["Stop"])
