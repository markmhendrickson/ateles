"""Tests for the harness_usage recording CLI (ent_0c213a308b74c1216b241a6e)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import harness_usage  # noqa: E402
from harness_usage import harness_router  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("APIS_HARNESS_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("APIS_HARNESS_HEADROOM_FILE", str(tmp_path / "absent.json"))
    monkeypatch.delenv("APIS_HARNESS_HEADROOM", raising=False)
    monkeypatch.delenv("APIS_HARNESS_PROVIDERS", raising=False)


def test_usage_command_makes_selection_use_live_headroom(capsys) -> None:
    resets = harness_router._iso_from_wall(time.time() + 86400)
    assert (
        harness_usage.main(
            [
                "usage",
                "claude",
                "--window",
                f"weekly=3@{resets}",
                "--window",
                "five_hour=8",
            ]
        )
        == 0
    )
    shown = json.loads(capsys.readouterr().out)
    assert shown["claude"]["headroom"] == pytest.approx(0.92)
    assert harness_router.configured_headroom()["claude"] == pytest.approx(0.92)


def test_exhausted_command_holds_provider_out(capsys) -> None:
    until = harness_router._iso_from_wall(time.time() + 3 * 86400)
    assert harness_usage.main(["exhausted", "codex", "--until", until]) == 0
    assert json.loads(capsys.readouterr().out)["codex"]["headroom"] == 0.0
    available = {"claude": "/bin/claude", "codex": "/bin/codex", "cursor": "/bin/c"}
    assert "codex" not in harness_router.usable_provider_names(available)


def test_malformed_window_is_rejected() -> None:
    with pytest.raises(SystemExit):
        harness_usage.main(["usage", "claude", "--window", "weekly=lots"])
    with pytest.raises(SystemExit):
        harness_usage.main(["exhausted", "claude", "--until", "next tuesday"])
