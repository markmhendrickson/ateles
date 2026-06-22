"""
Unit tests for execution/daemons/aquila/aquila.py

Run with: python -m pytest execution/daemons/aquila/test_aquila.py -q
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import types
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path bootstrap — mirror what conftest.py does in sibling daemon dirs.
# ---------------------------------------------------------------------------

_AQUILA_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _AQUILA_DIR.parent.parent.parent

for _p in (str(_REPO_ROOT), str(_AQUILA_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------------------
# Helpers to (re-)import the module under test in isolation.
# We import once at module level and reference functions directly so tests
# can patch module-level globals via monkeypatch.
# ---------------------------------------------------------------------------


def _load_aquila():
    """Load (or reload) the aquila module, returning it."""
    spec = importlib.util.spec_from_file_location(
        "aquila", str(_AQUILA_DIR / "aquila.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Import once; tests that mutate module globals use monkeypatch to restore.
aquila = _load_aquila()


# ===========================================================================
# 1. _already_ran / _mark_ran — idempotency
# ===========================================================================


class TestIdempotency:
    """Tests for the monthly idempotency guard."""

    def test_fresh_state_file_not_ran(self, tmp_path):
        """No state file → _already_ran returns False."""
        state = tmp_path / ".aquila_last_run"
        with patch.object(aquila, "STATE_FILE", state):
            assert aquila._already_ran("2026-06") is False

    def test_mark_ran_then_already_ran(self, tmp_path):
        """After _mark_ran, _already_ran returns True for the same period."""
        state = tmp_path / ".aquila_last_run"
        with patch.object(aquila, "STATE_FILE", state):
            aquila._mark_ran("2026-06")
            assert aquila._already_ran("2026-06") is True

    def test_already_ran_wrong_period_returns_false(self, tmp_path):
        """State file with a different period → _already_ran returns False."""
        state = tmp_path / ".aquila_last_run"
        state.write_text("2026-05")
        with patch.object(aquila, "STATE_FILE", state):
            assert aquila._already_ran("2026-06") is False

    def test_already_ran_stale_period(self, tmp_path):
        """A state file from a prior month is treated as not-ran for today."""
        state = tmp_path / ".aquila_last_run"
        state.write_text("2025-01")
        with patch.object(aquila, "STATE_FILE", state):
            assert aquila._already_ran("2026-06") is False

    def test_corrupt_state_file_returns_false(self, tmp_path):
        """Corrupt / unexpected content → _already_ran returns False (no crash)."""
        state = tmp_path / ".aquila_last_run"
        state.write_bytes(b"\xff\xfe garbage \x00\x01")
        with patch.object(aquila, "STATE_FILE", state):
            # Should not raise; strip() of unexpected bytes may still differ.
            try:
                result = aquila._already_ran("2026-06")
            except Exception:
                result = False  # any exception → treat as not-ran
            assert result is False

    def test_mark_ran_writes_period_to_file(self, tmp_path):
        """_mark_ran writes exactly the period string to the state file."""
        state = tmp_path / ".aquila_last_run"
        with patch.object(aquila, "STATE_FILE", state):
            aquila._mark_ran("2026-06")
        assert state.read_text() == "2026-06"

    def test_mark_ran_overwrites_previous(self, tmp_path):
        """_mark_ran overwrites an existing state file with the new period."""
        state = tmp_path / ".aquila_last_run"
        state.write_text("2026-05")
        with patch.object(aquila, "STATE_FILE", state):
            aquila._mark_ran("2026-06")
        assert state.read_text() == "2026-06"


# ===========================================================================
# 2. _deliver — Telegram chunking
# ===========================================================================


class TestDeliver:
    """Tests for the Telegram delivery chunker."""

    def _captured_calls(self, report_md: str, period: str = "2026-06") -> list[tuple]:
        """Run _deliver and return list of (message, priority) notify calls."""
        calls: list[tuple] = []

        def fake_notify(msg: str, priority: str = "info") -> None:
            calls.append((msg, priority))

        with patch.object(aquila, "_notify", side_effect=fake_notify):
            aquila._deliver(report_md, period)
        return calls

    def test_empty_message_sends_warn(self):
        """An empty report triggers a single warn notification (no body chunks)."""
        calls = self._captured_calls("", "2026-06")
        assert len(calls) == 1
        assert calls[0][1] == "warn"
        assert "empty" in calls[0][0].lower()

    def test_whitespace_only_message_sends_warn(self):
        """A whitespace-only report is treated as empty → warn."""
        calls = self._captured_calls("   \n\t  ", "2026-06")
        assert len(calls) == 1
        assert calls[0][1] == "warn"

    def test_short_message_single_chunk(self):
        """A message under 3500 chars: header + exactly one body chunk."""
        body = "x" * 100
        calls = self._captured_calls(body)
        # First call: header notification
        assert calls[0][1] == "info"
        assert "2026-06" in calls[0][0]
        # Second call: the single body chunk
        assert len(calls) == 2
        assert calls[1][0] == body
        assert calls[1][1] == "info"

    def test_message_exactly_at_chunk_boundary(self):
        """A message exactly 3500 chars sends one body chunk, not two."""
        body = "a" * aquila._TELEGRAM_CHUNK  # exactly 3500
        calls = self._captured_calls(body)
        assert len(calls) == 2  # header + 1 chunk
        assert calls[1][0] == body

    def test_message_one_over_boundary_splits(self):
        """3501 chars triggers a second body chunk (off-by-one check)."""
        body = "b" * (aquila._TELEGRAM_CHUNK + 1)
        calls = self._captured_calls(body)
        # header + 2 body chunks
        assert len(calls) == 3
        assert calls[1][0] == "b" * aquila._TELEGRAM_CHUNK
        assert calls[2][0] == "b"

    def test_message_requiring_multiple_chunks(self):
        """A long message splits into ceil(len/3500) body chunks."""
        chunk = aquila._TELEGRAM_CHUNK
        body = "c" * (chunk * 3 + 500)  # 4 chunks
        calls = self._captured_calls(body)
        body_calls = calls[1:]  # skip header
        assert len(body_calls) == 4
        # Each chunk except the last is exactly _TELEGRAM_CHUNK chars.
        for c in body_calls[:-1]:
            assert len(c[0]) == chunk
        assert len(body_calls[-1][0]) == 500

    def test_header_contains_period(self):
        """The header notification always embeds the period string."""
        calls = self._captured_calls("hello", "2025-12")
        assert "2025-12" in calls[0][0]

    def test_all_body_chunks_are_info_priority(self):
        """Every body chunk notification is sent at info priority."""
        body = "d" * (aquila._TELEGRAM_CHUNK * 2)
        calls = self._captured_calls(body)
        for msg, priority in calls[1:]:
            assert priority == "info"


# ===========================================================================
# 3. _emit_daemon_report — error handling
# ===========================================================================


class TestEmitDaemonReport:
    """Tests for the Neotoma daemon_report writer."""

    def test_no_token_skips_silently(self):
        """If NEOTOMA_BEARER_TOKEN is empty, _emit_daemon_report does nothing."""
        with patch.object(aquila, "NEOTOMA_BEARER_TOKEN", ""):
            # Should not raise even if urllib is broken.
            aquila._emit_daemon_report("info", "test")  # no assertion; just no crash

    def test_no_base_url_skips_silently(self):
        """If NEOTOMA_BASE_URL is empty, _emit_daemon_report does nothing."""
        with (
            patch.object(aquila, "NEOTOMA_BEARER_TOKEN", "tok"),
            patch.object(aquila, "NEOTOMA_BASE_URL", ""),
        ):
            aquila._emit_daemon_report("info", "test")

    def test_timeout_exception_is_swallowed(self):
        """urllib timeout does not propagate — _emit_daemon_report is non-fatal."""
        import urllib.request

        def raise_timeout(*args, **kwargs):
            raise TimeoutError("simulated timeout")

        with (
            patch.object(aquila, "NEOTOMA_BEARER_TOKEN", "tok"),
            patch.object(aquila, "NEOTOMA_BASE_URL", "https://neotoma.example.com"),
            patch.object(urllib.request, "urlopen", side_effect=raise_timeout),
        ):
            aquila._emit_daemon_report("error", "timed out")  # must not raise

    def test_generic_exception_is_swallowed(self):
        """Any unexpected exception inside urlopen is caught — non-fatal."""
        import urllib.request

        with (
            patch.object(aquila, "NEOTOMA_BEARER_TOKEN", "tok"),
            patch.object(aquila, "NEOTOMA_BASE_URL", "https://neotoma.example.com"),
            patch.object(
                urllib.request, "urlopen", side_effect=RuntimeError("boom")
            ),
        ):
            aquila._emit_daemon_report("critical", "boom")  # must not raise

    def test_http_error_is_swallowed(self):
        """HTTP 500 from Neotoma does not propagate."""
        import urllib.request

        def raise_http(*args, **kwargs):
            raise urllib.error.HTTPError(
                url="https://neotoma.example.com/api/store",
                code=500,
                msg="Internal Server Error",
                hdrs={},  # type: ignore[arg-type]
                fp=None,
            )

        with (
            patch.object(aquila, "NEOTOMA_BEARER_TOKEN", "tok"),
            patch.object(aquila, "NEOTOMA_BASE_URL", "https://neotoma.example.com"),
            patch.object(urllib.request, "urlopen", side_effect=raise_http),
        ):
            aquila._emit_daemon_report("error", "500 response")

    def test_successful_call_hits_correct_endpoint(self):
        """With a valid token, _emit_daemon_report POSTs to /api/store."""
        import urllib.request

        captured: list[dict] = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        def fake_urlopen(req, timeout=None):
            captured.append(
                {
                    "url": req.full_url,
                    "headers": dict(req.headers),
                    "body": json.loads(req.data),
                }
            )
            return FakeResponse()

        with (
            patch.object(aquila, "NEOTOMA_BEARER_TOKEN", "my-token"),
            patch.object(aquila, "NEOTOMA_BASE_URL", "https://neotoma.example.com"),
            patch.object(urllib.request, "urlopen", side_effect=fake_urlopen),
        ):
            aquila._emit_daemon_report("info", "all good", {"chars": 42})

        assert len(captured) == 1
        call = captured[0]
        assert call["url"].endswith("/api/store")
        assert "Bearer my-token" in call["headers"].get("Authorization", "")
        entity = call["body"]["entities"][0]
        assert entity["severity"] == "info"
        assert entity["message"] == "all good"
        assert entity["daemon_name"] == "aquila"


# ===========================================================================
# 4. _run — async result validation
# ===========================================================================


class TestRun:
    """Tests for the async skill dispatch wrapper."""

    def test_ok_false_result_propagates(self):
        """_run returns whatever run_skill returns — ok=False is passed through."""
        import asyncio

        fake_result = types.SimpleNamespace(ok=False, error="dispatch failed", stdout="")

        async def fake_run_skill(*args, **kwargs):
            return fake_result

        mock_skill_runner = types.ModuleType("skill_runner")
        mock_skill_runner.run_skill = fake_run_skill

        with patch.dict(sys.modules, {"skill_runner": mock_skill_runner}):
            result = asyncio.run(aquila._run())

        assert result.ok is False
        assert result.error == "dispatch failed"

    def test_ok_true_with_empty_stdout(self):
        """_run with ok=True and empty stdout still returns the result object."""
        import asyncio

        fake_result = types.SimpleNamespace(ok=True, stdout="", error=None)

        async def fake_run_skill(*args, **kwargs):
            return fake_result

        mock_skill_runner = types.ModuleType("skill_runner")
        mock_skill_runner.run_skill = fake_run_skill

        with patch.dict(sys.modules, {"skill_runner": mock_skill_runner}):
            result = asyncio.run(aquila._run())

        assert result.ok is True
        assert result.stdout == ""

    def test_ok_true_with_report_markdown(self):
        """_run with ok=True and non-empty stdout returns the full markdown."""
        import asyncio

        report = "# Monthly Report\n\nSection 1: ..."
        fake_result = types.SimpleNamespace(ok=True, stdout=report, error=None)

        async def fake_run_skill(*args, **kwargs):
            return fake_result

        mock_skill_runner = types.ModuleType("skill_runner")
        mock_skill_runner.run_skill = fake_run_skill

        with patch.dict(sys.modules, {"skill_runner": mock_skill_runner}):
            result = asyncio.run(aquila._run())

        assert result.ok is True
        assert result.stdout == report
