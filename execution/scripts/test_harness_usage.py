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


def test_tiers_command_reports_dispatch_counts_per_tier(capsys, monkeypatch, tmp_path) -> None:
    """`harness_usage.py tiers` reads the ledger every dispatch writes, so the
    per-tier spend can be read against the weekly budget."""
    import model_tiering

    monkeypatch.setenv("APIS_TIER_LEDGER_FILE", str(tmp_path / "ledger.jsonl"))
    for tier, klass in (("top", "build"), ("mid", "lens_review:pm"), ("mid", "lens_review:qa")):
        model_tiering.record_dispatch(
            skill="x", provider="claude",
            resolved=model_tiering.ResolvedTier(tier, "policy", klass), model="m",
        )
    model_tiering.record_dispatch(skill="x", provider="claude", resolved=None, model=None)

    assert harness_usage.main(["tiers"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["total"] == 4
    assert report["by_tier"] == {"top": 1, "mid": 2, "untiered": 1}
    assert report["by_class"]["lens_review:pm"] == {"mid": 1}

    assert harness_usage.main(["tiers", "--since-hours", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 4


def test_tiers_command_on_a_missing_ledger_is_an_empty_report(capsys, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("APIS_TIER_LEDGER_FILE", str(tmp_path / "absent.jsonl"))
    assert harness_usage.main(["tiers"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 0


def test_show_surfaces_a_spent_session_window(capsys) -> None:
    """Weekly headroom reads healthy while the 5-hour window is spent."""
    until = time.time() + 2 * 3600
    harness_router.record_usage("claude", [{"name": "weekly", "used_percent": 35}])
    harness_router.record_cooling("claude", until, reason="session_limit")

    assert harness_usage.main(["show"]) == 0
    claude = json.loads(capsys.readouterr().out)["claude"]

    assert claude["headroom"] == pytest.approx(0.65)
    assert claude["cooling"]["reason"] == "session_limit"
    assert claude["cooling"]["until"] == harness_router._iso_from_wall(until)
    assert 7100 < claude["cooling"]["remaining_seconds"] <= 7200
    assert claude["windows"][0]["name"] == "weekly"


def test_show_reports_no_cooling_once_the_window_has_passed(capsys) -> None:
    harness_router.record_cooling("claude", time.time() - 10, reason="session_limit")
    assert harness_usage.main(["show"]) == 0
    assert json.loads(capsys.readouterr().out)["claude"]["cooling"] is None


def test_tiers_reasons_flag_reports_why_dispatches_were_raised(capsys, monkeypatch, tmp_path) -> None:
    """`--reasons` is how a change to the escalation signals is measured."""
    import model_tiering

    monkeypatch.setenv("APIS_TIER_LEDGER_FILE", str(tmp_path / "ledger.jsonl"))
    model_tiering.record_dispatch(
        skill="x", provider="claude", model="m",
        resolved=model_tiering.ResolvedTier(
            "top", "escalated", "lens_review:pm", ("diff_lines_changed=1054>400",)
        ),
    )
    assert harness_usage.main(["tiers"]) == 0
    assert "by_reason" not in json.loads(capsys.readouterr().out)

    assert harness_usage.main(["tiers", "--reasons"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["by_reason"] == {
        "diff_lines_changed": {"total": 1, "by_class": {"lens_review:pm": 1}}
    }
