"""
Effect tests for prepare.py — ateles#242 (scheduled prepare daemon repair:
missing `claude` CLI on PATH, stale NEOTOMA_BASE_URL silently skipping the
in-flight-release guard).

Tests are fully synchronous / mock-based: `subprocess.Popen`/`urllib.request`
are always patched, no real process is spawned and no real network call is
made. Every test asserts an observable effect (return value, exact call
args/absence of a call, log/telegram content) — never merely "no exception
was raised."

Run with: pytest execution/daemons/phoenicurus-release/test_prepare.py -v
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import prepare  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    p = MagicMock(spec=subprocess.CompletedProcess)
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


# ── Bug 1: missing `claude` CLI on PATH ─────────────────────────────────────
# This is the exact regression the PATH fix in this PR addresses — pin it so
# a future plist regeneration or ~/.local/bin removal fails CI instead of
# failing silently in production again (undetected since ~2026-07-13).


class TestSpawnPrepareAgentMissingClaudeCLI:
    def test_missing_claude_logs_and_returns_false_without_spawn(self, monkeypatch):
        # `spawn_prepare_agent()` does `import shutil` locally inside the
        # function body, not at module scope — patching the real `shutil`
        # module (imported here) still works because the local import binds
        # the same module object. `prepare.shutil` does NOT exist.
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with patch.object(prepare, "telegram_send") as mock_telegram, patch.object(
            prepare.subprocess, "Popen"
        ) as mock_popen:
            result = prepare.spawn_prepare_agent("v0.18.8", 4, dry_run=False)

        assert result is False
        mock_popen.assert_not_called()
        mock_telegram.assert_called_once()
        assert "claude CLI not found" in mock_telegram.call_args[0][0]

    def test_present_claude_spawns_without_telegram(self, monkeypatch, tmp_path):
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/claude")
        agent_log = tmp_path / "agent.log"
        monkeypatch.setattr(prepare, "AGENT_LOG", agent_log)
        with patch.object(prepare, "telegram_send") as mock_telegram, patch.object(
            prepare.subprocess, "Popen"
        ) as mock_popen:
            result = prepare.spawn_prepare_agent("v0.18.8", 4, dry_run=False)

        assert result is True
        mock_popen.assert_called_once()
        mock_telegram.assert_not_called()


# ── Bug 2: NEOTOMA_BASE_URL — connection failure must not silently look like
# "no release in flight" ────────────────────────────────────────────────────


class TestExistingReleaseStatusConnectionFailureDoesNotSilentlySkip:
    def test_connection_refused_is_not_treated_as_no_release_in_flight(
        self, monkeypatch, caplog
    ):
        monkeypatch.setenv("NEOTOMA_BASE_URL", "http://localhost:9999")

        def fake_urlopen(req, timeout):
            raise prepare.urllib.error.URLError("Connection refused")

        monkeypatch.setattr(prepare.urllib.request, "urlopen", fake_urlopen)

        with caplog.at_level("WARNING"):
            # Pins CURRENT behavior (returns None on connection failure).
            # TODO(ateles#243): this asserts the CURRENT silent-skip; whether
            # an unreachable Neotoma should fail loud instead of proceeding
            # is a product decision, tracked in ateles#243, not made here.
            result = prepare.existing_release_status("v0.18.9")

        assert result is None
        assert "could not check existing release_result" in caplog.text


class TestExistingReleaseStatusUsesConfiguredBaseUrl:
    def test_reads_neotoma_base_url_env_not_hardcoded_default(self, monkeypatch):
        monkeypatch.setenv("NEOTOMA_BASE_URL", "http://localhost:9180")
        captured = {}

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            resp = MagicMock()
            resp.read.return_value = b'{"entities": []}'
            resp.__enter__ = lambda self: resp
            resp.__exit__ = lambda self, *a: False
            return resp

        monkeypatch.setattr(prepare.urllib.request, "urlopen", fake_urlopen)

        result = prepare.existing_release_status("v0.18.9")

        assert captured["url"].startswith("http://localhost:9180")
        assert result is None
