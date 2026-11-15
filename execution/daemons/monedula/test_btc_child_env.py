"""The BTC payment child must bill the subscription, not a pay-per-token API key.

Red before: the handler passed the daemon's whole environment, so with both
credentials set the Claude CLI used the API key and, with no credit, failed the
payment before any tool ran ("Credit balance is too low", 2026-11-15).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from handlers import btc_transfer  # noqa: E402

OAUTH = "CLAUDE_CODE_OAUTH_TOKEN"
API = "ANTHROPIC_API_KEY"


def test_drops_api_key_when_both_are_set(monkeypatch):
    monkeypatch.setenv(OAUTH, "o" * 20)
    monkeypatch.setenv(API, "a" * 20)
    env = btc_transfer._claude_child_env()
    assert API not in env
    assert env[OAUTH] == "o" * 20
    assert os.environ[API] == "a" * 20  # the daemon's own environment is untouched


def test_keeps_api_key_when_it_is_the_only_credential(monkeypatch):
    monkeypatch.delenv(OAUTH, raising=False)
    monkeypatch.setenv(API, "a" * 20)
    assert btc_transfer._claude_child_env()[API] == "a" * 20


def test_payment_subprocess_uses_the_child_env(monkeypatch):
    seen = {}

    class Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        seen["env"] = kwargs.get("env")
        return Done()

    monkeypatch.setenv(OAUTH, "o" * 20)
    monkeypatch.setenv(API, "a" * 20)
    monkeypatch.setattr(btc_transfer.subprocess, "run", fake_run)
    monkeypatch.setattr(btc_transfer, "_find_claude", lambda: "/usr/local/bin/claude")
    monkeypatch.setattr(btc_transfer, "_build_claude_prompt", lambda profile: "prompt")

    class Profile:
        btc_address = "bc1-test-address"
        prefix = "TEST"
        name = "test"

    handler = btc_transfer.BtcTransferHandler.__new__(btc_transfer.BtcTransferHandler)
    handler.profile = Profile()

    handler.execute({})
    assert seen["env"] is not None and API not in seen["env"]
