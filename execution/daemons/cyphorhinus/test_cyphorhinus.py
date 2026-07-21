"""
Startup-config tests for cyphorhinus.py — ateles#243.

cyphorhinus.py's _neotoma_query is a best-effort lookup feeding
_find_job_by_message_id (a benign UX-degradation site, not a safety guard —
see the engineering classification in ateles#243). It intentionally keeps its
existing catch-and-log-empty-list behavior; the only change is removing the
hardcoded :3180 default for NEOTOMA_BASE_URL.

Run with: pytest execution/daemons/cyphorhinus/test_cyphorhinus.py -v
"""

from __future__ import annotations

import importlib
import sys
import urllib.error
from pathlib import Path

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import os as _os  # noqa: E402

_os.environ.setdefault("NEOTOMA_BASE_URL", "https://neotoma.example.com:9180")
_os.environ.setdefault("NEOTOMA_BEARER_TOKEN", "test-token")

import cyphorhinus  # noqa: E402


class TestCyphorhinusModuleImportFailsLoudWithoutNeotomaBaseUrl:
    def test_import_raises_neotoma_config_error_when_unset(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from execution.lib.neotoma_config import NeotomaConfigError

        # cyphorhinus.py's own bootstrap reloads env vars (setdefault-guarded
        # by an "unset or __placeholder__" check) from ~/.config/neotoma/.env
        # before resolve_neotoma_base_url() runs — re-home HOME so this test
        # observes a genuinely unset var, independent of the operator's real
        # ~/.config/neotoma/.env on this machine.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
        sys.modules.pop("cyphorhinus", None)
        try:
            with pytest.raises(NeotomaConfigError):
                importlib.import_module("cyphorhinus")
        finally:
            monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.com:9180")
            sys.modules.pop("cyphorhinus", None)
            importlib.import_module("cyphorhinus")


class TestCyphorhinusUsesConfiguredBaseUrlNoHardcodedDefault:
    def test_module_constant_matches_env_no_3180(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.com:9180")
        sys.modules.pop("cyphorhinus", None)
        try:
            fresh = importlib.import_module("cyphorhinus")
            assert fresh.NEOTOMA_BASE_URL == "https://neotoma.example.com:9180"
            assert "3180" not in fresh.NEOTOMA_BASE_URL
        finally:
            sys.modules.pop("cyphorhinus", None)
            importlib.import_module("cyphorhinus")


class TestNeotomaQueryStaysBestEffort:
    """Lock in the pre-existing (already correct, per PM's classification)
    best-effort behavior: _neotoma_query feeds a benign UX-linkage lookup, not
    a safety guard, so it must keep returning [] on failure rather than
    raising — the :3180 default swap must not regress this into a crash."""

    def test_connection_failure_returns_empty_list_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*args: object, **kwargs: object) -> None:
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(cyphorhinus.urllib.request, "urlopen", _boom)
        result = cyphorhinus._neotoma_query("activity_log", search="job-1")
        assert result == []

    def test_missing_bearer_token_returns_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cyphorhinus, "NEOTOMA_BEARER_TOKEN", "")
        result = cyphorhinus._neotoma_query("activity_log", search="job-1")
        assert result == []
