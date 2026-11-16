"""The BTC handler's claude CLI runs on the subscription login, not a metered key.

Regression: the handler passed the daemon's whole environment to `claude
--print`. Monedula loads the shared dotenv, which defines ANTHROPIC_API_KEY, so
the CLI billed an exhausted metered account, printed only "Credit balance is too
low", and the payment failed with no classification of whether money moved.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest
from handlers import btc_transfer
from handlers.payment_profile import PaymentProfile

SKILL_RUNNER = Path(__file__).resolve().parents[1] / "apis" / "skill_runner.py"


def _handler() -> btc_transfer.BtcTransferHandler:
    return btc_transfer.BtcTransferHandler(
        PaymentProfile(
            prefix="YOGA",
            label="Yoga",
            calendar_keywords=["yoga"],
            payment_type="btc",
            amount_eur=60,
            btc_address="bc1qtestaddressxxxxxxxxxxxxxxxxxxxxxxxx",
        )
    )


@pytest.fixture
def fake_cli(monkeypatch):
    """Capture the subprocess call; never launch a real CLI or touch a wallet."""
    calls: list[dict] = []
    reply = {"stdout": "", "stderr": ""}

    def run(args, **kwargs):
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(
            args, 0, stdout=reply["stdout"], stderr=reply["stderr"]
        )

    monkeypatch.setattr(btc_transfer, "_find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr(btc_transfer.subprocess, "run", run)
    return calls, reply


@pytest.mark.parametrize("name", btc_transfer._METERED_CREDENTIALS)
def test_child_env_drops_each_metered_key(monkeypatch, fake_cli, name):
    calls, _ = fake_cli
    for key in btc_transfer._METERED_CREDENTIALS:
        monkeypatch.setenv(key, "metered-test-value")

    _handler().execute({"summary": "Yoga"})

    assert len(calls) == 1
    assert name not in calls[0]["env"]


def test_child_env_keeps_unrelated_variables(monkeypatch, fake_cli):
    calls, _ = fake_cli
    monkeypatch.setenv("ANTHROPIC_API_KEY", "metered-test-value")
    monkeypatch.setenv("BTC_WALLET_TEST_SETTING", "keep-me")

    _handler().execute({"summary": "Yoga"})

    assert calls[0]["env"]["BTC_WALLET_TEST_SETTING"] == "keep-me"


def test_credit_balance_output_is_an_unknown_outcome(fake_cli):
    # --print shows no tool calls, so the refusal cannot prove nothing was sent.
    _, reply = fake_cli
    reply["stdout"] = "Credit balance is too low\n"

    result = _handler().execute({"summary": "Yoga"})

    assert result["status"] == "failed"
    assert "failure_stage" not in result
    assert "Credit balance is too low" in result["error"]
    assert "outcome unknown" in result["error"]


def test_credit_balance_on_stderr_is_an_unknown_outcome(fake_cli):
    _, reply = fake_cli
    reply["stderr"] = "Credit balance is too low\n"

    result = _handler().execute({"summary": "Yoga"})

    assert result["status"] == "failed"
    assert "failure_stage" not in result
    assert "outcome unknown" in result["error"]


def test_payment_result_wins_over_credit_balance_text(fake_cli):
    _, reply = fake_cli
    reply["stdout"] = 'PAYMENT_RESULT: {"status": "failed", "error": "synthetic"}\n'
    reply["stderr"] = "Credit balance is too low\n"

    result = _handler().execute({"summary": "Yoga"})

    assert result["error"] == "synthetic"
    assert "failure_stage" not in result


def test_metered_names_match_skill_runner():
    """The local tuple must cover the Apis runner's list, plus the auth token.

    Read from source rather than imported: importing skill_runner pulls in the
    whole Apis runner.
    """
    tree = ast.parse(SKILL_RUNNER.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_METERED_CREDENTIALS"
            for t in node.targets
        ):
            runner = set(ast.literal_eval(node.value))
            assert runner <= set(btc_transfer._METERED_CREDENTIALS)
            assert "ANTHROPIC_AUTH_TOKEN" in btc_transfer._METERED_CREDENTIALS
            return
    pytest.fail("_METERED_CREDENTIALS not found in skill_runner.py")
