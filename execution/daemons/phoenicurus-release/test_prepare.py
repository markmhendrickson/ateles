"""
Effect tests for prepare.py — ateles#243 (NEOTOMA_BASE_URL fail-fast / fail-loud).

prepare.py resolves NEOTOMA_BASE_URL via execution.lib.neotoma_config at
import time, so NEOTOMA_BASE_URL must be set in the environment BEFORE the
module is imported. Tests that want to exercise the "unset" path re-import
the module fresh under a patched environment (see
TestPrepareModuleImportFailsLoudWithoutNeotomaBaseUrl).

Run with: pytest execution/daemons/phoenicurus-release/test_prepare.py -v
"""

from __future__ import annotations

import importlib
import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import os as _os  # noqa: E402

_os.environ.setdefault("NEOTOMA_BASE_URL", "https://neotoma.example.com:9180")

import prepare  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────


class _FakeHTTPResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


# ── existing_release_status: connection failure must raise, not return None ──


class TestExistingReleaseStatusConnectionFailureDoesNotSilentlySkip:
    def test_url_error_raises_neotoma_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*args: object, **kwargs: object) -> None:
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(prepare.urllib.request, "urlopen", _boom)

        with pytest.raises(prepare.NeotomaUnavailableError):
            prepare.existing_release_status("v1.2.3")

    def test_os_error_raises_neotoma_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*args: object, **kwargs: object) -> None:
            raise OSError("network unreachable")

        monkeypatch.setattr(prepare.urllib.request, "urlopen", _boom)

        with pytest.raises(prepare.NeotomaUnavailableError):
            prepare.existing_release_status("v1.2.3")

    def test_json_decode_error_raises_neotoma_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _BadJSON:
            def read(self) -> bytes:
                return b"not json"

            def __enter__(self) -> "_BadJSON":
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        monkeypatch.setattr(
            prepare.urllib.request, "urlopen", lambda *a, **k: _BadJSON()
        )

        with pytest.raises(prepare.NeotomaUnavailableError):
            prepare.existing_release_status("v1.2.3")

    def test_success_with_no_matching_release_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            prepare.urllib.request,
            "urlopen",
            lambda *a, **k: _FakeHTTPResponse({"entities": []}),
        )
        assert prepare.existing_release_status("v1.2.3") is None

    def test_success_with_pending_release_returns_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            prepare.urllib.request,
            "urlopen",
            lambda *a, **k: _FakeHTTPResponse(
                {"entities": [{"snapshot": {"status": "pending_approval"}}]}
            ),
        )
        assert prepare.existing_release_status("v1.2.3") == "pending_approval"


class TestExistingReleaseStatusUsesConfiguredBaseUrl:
    def test_queries_configured_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}

        def _capture(req: object, timeout: int = 0) -> _FakeHTTPResponse:
            captured["url"] = req.full_url  # type: ignore[attr-defined]
            return _FakeHTTPResponse({"entities": []})

        monkeypatch.setattr(prepare.urllib.request, "urlopen", _capture)
        prepare.existing_release_status("v1.2.3")
        assert captured["url"].startswith(prepare.NEOTOMA_BASE_URL)


# ── main(): behavioral effect — must NOT proceed to spawn when unreachable ──


class TestMainDoesNotProceedToPrepareWhenNeotomaUnreachable:
    def test_main_exits_nonzero_and_does_not_spawn_prepare_agent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(prepare, "_already_ran_today", lambda: False)
        monkeypatch.setattr(prepare.subprocess, "run", lambda *a, **k: MagicMock(returncode=0))
        monkeypatch.setattr(prepare, "latest_tag", lambda: "v1.2.3")
        monkeypatch.setattr(prepare, "unreleased_commit_count", lambda tag: 5)
        monkeypatch.setattr(prepare, "main_ci_green", lambda: True)

        def _boom(*args: object, **kwargs: object) -> None:
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(prepare.urllib.request, "urlopen", _boom)

        spawn_mock = MagicMock(return_value=True)
        monkeypatch.setattr(prepare, "spawn_prepare_agent", spawn_mock)
        telegram_mock = MagicMock()
        monkeypatch.setattr(prepare, "telegram_send", telegram_mock)

        exit_code = prepare.run_prepare(dry_run=False, force=False)

        assert exit_code != 0
        spawn_mock.assert_not_called()
        assert telegram_mock.called
        assert "unreachable" in telegram_mock.call_args[0][0].lower()

    def test_main_proceeds_to_spawn_when_neotoma_reachable_and_no_inflight_release(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(prepare, "_already_ran_today", lambda: False)
        monkeypatch.setattr(prepare.subprocess, "run", lambda *a, **k: MagicMock(returncode=0))
        monkeypatch.setattr(prepare, "latest_tag", lambda: "v1.2.3")
        monkeypatch.setattr(prepare, "unreleased_commit_count", lambda tag: 5)
        monkeypatch.setattr(prepare, "main_ci_green", lambda: True)
        monkeypatch.setattr(
            prepare.urllib.request,
            "urlopen",
            lambda *a, **k: _FakeHTTPResponse({"entities": []}),
        )

        spawn_mock = MagicMock(return_value=True)
        monkeypatch.setattr(prepare, "spawn_prepare_agent", spawn_mock)

        exit_code = prepare.run_prepare(dry_run=False, force=False)

        assert exit_code == 0
        spawn_mock.assert_called_once()


# ── Module import: fail loud when NEOTOMA_BASE_URL is unset ─────────────────


class TestPrepareModuleImportFailsLoudWithoutNeotomaBaseUrl:
    def test_import_raises_neotoma_config_error_when_unset(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from execution.lib.neotoma_config import NeotomaConfigError

        # prepare.py's own bootstrap reloads NEOTOMA_BASE_URL from
        # ~/.config/neotoma/.env via os.environ.setdefault() before it calls
        # resolve_neotoma_base_url() — re-home HOME to an empty tmp dir for
        # this (re-)import so the test observes a genuinely unset var,
        # independent of the operator's real ~/.config/neotoma/.env on this
        # machine.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
        sys.modules.pop("prepare", None)
        try:
            with pytest.raises(NeotomaConfigError):
                importlib.import_module("prepare")
        finally:
            monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.com:9180")
            sys.modules.pop("prepare", None)
            importlib.import_module("prepare")
