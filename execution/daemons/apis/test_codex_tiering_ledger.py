"""Codex dispatch tiering and the per-dispatch spend rows on the tier ledger.

Tasks ent_13bd6d4cb981cace028c5a25 (Codex tiers) and ent_2fd021f648bfb08fd7729aab
(record what each dispatch costs), Phase A3.

Codex already resolves a model through the same action_policy / vendor_binding
as Claude (``skill_runner`` passes ``--model`` for every provider); these tests
pin that at the real subprocess boundary for the cheaper tiers, pin what happens
when a tier has no Codex model, and pin the ledger's usage rows.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

import dispatch_role  # noqa: E402
import dispatch_usage  # noqa: E402
import model_tiering  # noqa: E402
from test_dispatch_role import captured_codex_dispatches, fake_repo  # noqa: E402,F401

CODEX_BINDING = {"codex": {"top": "model-top", "mid": "model-mid", "mechanical": "model-cheap"}}
POLICY = {
    "build": "top",
    "lens_review:pm": "mid",
    "ci_log_triage": "mechanical",
}


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    path = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("APIS_TIER_LEDGER_FILE", str(path))
    return path


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _configure(monkeypatch, binding=CODEX_BINDING) -> None:
    monkeypatch.setenv("APIS_ACTION_POLICY", json.dumps(POLICY))
    monkeypatch.setenv("APIS_VENDOR_BINDING", json.dumps(binding))


def _model_of(invocation: dict) -> str | None:
    cmd = invocation["cmd"]
    return cmd[cmd.index("--model") + 1] if "--model" in cmd else None


# ── a Codex dispatch gets the model its tier maps to ────────────────────────


@pytest.mark.parametrize(
    "action_class,extra,model,tier",
    [
        ("ci_log_triage", [], "model-cheap", "mechanical"),
        ("lens_review:pm", [], "model-mid", "mid"),
        # A signal raises the tier, and the model with it.
        ("lens_review:pm", ["--diff-lines", "900"], "model-top", "top"),
        ("build", [], "model-top", "top"),
    ],
)
def test_codex_dispatch_runs_the_model_its_tier_maps_to(
    captured_codex_dispatches, ledger, monkeypatch, action_class, extra, model, tier
) -> None:
    _configure(monkeypatch)
    rc = dispatch_role.main(
        ["--role", "cicada", "--task", "t", "--action-class", action_class, *extra]
    )
    assert rc == 0
    assert _model_of(captured_codex_dispatches[0]) == model
    start = [r for r in _rows(ledger) if r["event"] == "dispatch"]
    assert [(r["provider"], r["tier"], r["model"]) for r in start] == [("codex", tier, model)]


# ── a missing mapping keeps today's behaviour, and says so ──────────────────


def test_a_tier_with_no_codex_model_refuses_rather_than_running_the_ambient_default(
    captured_codex_dispatches, ledger, monkeypatch, caplog
) -> None:
    """Codex is bound for top only: a mechanical dispatch is refused (today's
    behaviour for a bound-but-incomplete provider), logged with the missing
    tier named, and never spawns Codex on its ambient default model."""
    _configure(monkeypatch, {"codex": {"top": "model-top"}})
    with caplog.at_level("ERROR"):
        rc = dispatch_role.main(
            ["--role", "cicada", "--task", "t", "--action-class", "ci_log_triage"]
        )
    assert rc != 0
    assert captured_codex_dispatches == []
    assert "no model bound for tier 'mechanical'" in caplog.text


def test_no_vendor_binding_at_all_keeps_the_ambient_default_for_codex(
    captured_codex_dispatches, ledger, monkeypatch
) -> None:
    monkeypatch.setenv("APIS_ACTION_POLICY", json.dumps(POLICY))
    rc = dispatch_role.main(
        ["--role", "cicada", "--task", "t", "--action-class", "ci_log_triage"]
    )
    assert rc == 0
    assert _model_of(captured_codex_dispatches[0]) is None


# ── the ledger carries what each dispatch spent ─────────────────────────────

CODEX_TEXT_STDERR = (
    b"workdir: /tmp/x\nsession id: 01a1\n--------\ncodex\ndone\ntokens used\n31,255\n"
)


@pytest.fixture
def codex_reports_tokens(monkeypatch):
    async def _spawn(*cmd, **kwargs):
        class _Process:
            returncode = 0

            async def communicate(self, input=None):
                return b"done", CODEX_TEXT_STDERR

        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)


def test_a_dispatch_appends_a_usage_row_tied_to_its_start_row(
    captured_codex_dispatches, codex_reports_tokens, ledger, monkeypatch
) -> None:
    _configure(monkeypatch)
    dispatch_role.main(
        ["--role", "cicada", "--task", "t", "--action-class", "ci_log_triage"]
    )
    start, usage = _rows(ledger)
    assert (start["event"], usage["event"]) == ("dispatch", "usage")
    assert start["dispatch_id"] and start["dispatch_id"] == usage["dispatch_id"]
    assert usage["provider"] == "codex"
    assert usage["model"] == "model-cheap" and usage["model_source"] == "requested"
    assert usage["tier"] == "mechanical" and usage["action_class"] == "ci_log_triage"
    assert usage["ok"] is True
    # Codex's text mode prints one total and no split: only the total is known,
    # and every other count is an explicit null, never an estimate.
    assert usage["total_tokens"] == 31255
    for unreported in (
        "input_tokens", "output_tokens", "cache_read_tokens",
        "cache_write_tokens", "reasoning_tokens", "total_cost_usd",
    ):
        assert usage[unreported] is None
    # The usage row does not make the dispatch count twice.
    assert model_tiering.tier_counts()["total"] == 1


def test_a_dispatch_that_reports_nothing_records_nulls_not_zeros(
    captured_codex_dispatches, ledger, monkeypatch
) -> None:
    _configure(monkeypatch)
    dispatch_role.main(["--role", "cicada", "--task", "t", "--action-class", "build"])
    usage = _rows(ledger)[1]
    assert usage["total_tokens"] is None and usage["input_tokens"] is None


def test_usage_row_carries_reported_counts_and_cost(ledger) -> None:
    usage = dispatch_usage.parse_dispatch_usage(
        "claude",
        '{"total_cost_usd":0.0421,"usage":{"input_tokens":1200,"output_tokens":340,'
        '"cache_read_input_tokens":27456,"cache_creation_input_tokens":88},'
        '"modelUsage":{"claude-opus-5":{"outputTokens":340}},"type":"result"}',
    )
    model_tiering.record_dispatch_usage(
        dispatch_id="d1", skill="pavo", provider="claude",
        resolved=model_tiering.ResolvedTier("top", "policy", "build"),
        requested_model="opus", usage=usage, ok=True,
    )
    row = _rows(ledger)[0]
    assert (row["input_tokens"], row["output_tokens"]) == (1200, 340)
    assert (row["cache_read_tokens"], row["cache_write_tokens"]) == (27456, 88)
    assert row["total_cost_usd"] == 0.0421
    assert (row["model"], row["model_source"]) == ("claude-opus-5", "reported")


def test_spend_report_sums_only_what_was_reported_and_says_how_much(ledger) -> None:
    resolved = model_tiering.ResolvedTier("mid", "policy", "lens_review:pm")

    def row(provider, tokens, cost=None):
        model_tiering.record_dispatch_usage(
            dispatch_id="x", skill="s", provider=provider, resolved=resolved,
            requested_model="m",
            usage=dispatch_usage.DispatchUsage(
                provider=provider, reported_total_tokens=tokens, total_cost_usd=cost
            ),
        )

    row("codex", 1000)
    row("codex", 500)
    row("codex", None)
    row("claude", None, 0.25)
    report = model_tiering.usage_totals(group_by="provider")
    codex, claude = report["groups"]["codex"], report["groups"]["claude"]
    assert codex["dispatches"] == 3
    assert (codex["reported_token_rows"], codex["unreported_token_rows"]) == (2, 1)
    assert codex["total_tokens"] == 1500
    assert codex["total_cost_usd"] is None  # nobody reported a cost: null, not 0
    assert claude["total_cost_usd"] == 0.25 and claude["unreported_token_rows"] == 1
    with pytest.raises(ValueError):
        model_tiering.usage_totals(group_by="nonsense")


def test_ledger_write_failure_never_reaches_the_dispatch(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("APIS_TIER_LEDGER_FILE", str(tmp_path / "a-file" / "x" / "ledger"))
    (tmp_path / "a-file").write_text("not a directory")
    model_tiering.record_dispatch_usage(
        dispatch_id="d", skill="s", provider="codex", resolved=None,
        requested_model=None, usage=dispatch_usage.DispatchUsage(provider="codex"),
    )


def test_spend_report_groups_by_skill_and_the_cost_alias_works(ledger, capsys) -> None:
    model_tiering.record_dispatch_usage(
        dispatch_id="x", skill="pavo", provider="codex", resolved=None,
        requested_model="m", usage=dispatch_usage.DispatchUsage(provider="codex"),
    )
    import harness_usage

    assert harness_usage.main(["cost", "--by", "skill"]) == 0
    assert json.loads(capsys.readouterr().out)["groups"]["pavo"]["dispatches"] == 1
