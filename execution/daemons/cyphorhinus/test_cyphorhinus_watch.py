"""
Startup-config tests for cyphorhinus/watch.py — ateles#243.

watch.py already fails loud on runtime connection failures via
NeotomaUnavailableError (retry-with-grace-period, Telegram alert). The only
defect this issue fixes here is the hardcoded :3180 default for
_NEOTOMA_BASE_URL — verify import-time fail-fast when NEOTOMA_BASE_URL is
unset, and that the configured value is used unmodified.

Run with: pytest execution/daemons/cyphorhinus/test_watch.py -v
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import os as _os  # noqa: E402

_os.environ.setdefault("NEOTOMA_BASE_URL", "https://neotoma.example.com:9180")

import watch  # noqa: E402


class TestWatchModuleImportFailsLoudWithoutNeotomaBaseUrl:
    def test_import_raises_neotoma_config_error_when_unset(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from execution.lib.neotoma_config import NeotomaConfigError

        # watch.py's own bootstrap reloads NEOTOMA_BASE_URL from
        # ~/.config/neotoma/.env (unconditionally, not setdefault) before
        # resolve_neotoma_base_url() runs — re-home HOME so this test
        # observes a genuinely unset var, independent of the operator's real
        # ~/.config/neotoma/.env on this machine.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("NEOTOMA_BASE_URL", raising=False)
        sys.modules.pop("watch", None)
        try:
            with pytest.raises(NeotomaConfigError):
                importlib.import_module("watch")
        finally:
            monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.com:9180")
            sys.modules.pop("watch", None)
            importlib.import_module("watch")


class TestWatchUsesConfiguredBaseUrlNoHardcodedDefault:
    def test_module_constant_matches_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("NEOTOMA_BASE_URL", "https://neotoma.example.com:9180")
        sys.modules.pop("watch", None)
        try:
            fresh_watch = importlib.import_module("watch")
            assert fresh_watch._NEOTOMA_BASE_URL == "https://neotoma.example.com:9180"
            assert "3180" not in fresh_watch._NEOTOMA_BASE_URL
        finally:
            sys.modules.pop("watch", None)
            importlib.import_module("watch")


class TestNeotomaUnavailableErrorAlreadyCorrect:
    """Lock in the pre-existing (already correct) runtime fail-loud behavior
    so a future refactor of _neotoma_query can't silently regress it while
    touching this file for the :3180 default swap."""

    def test_missing_bearer_token_raises_neotoma_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NEOTOMA_BEARER_TOKEN", raising=False)
        with pytest.raises(watch.NeotomaUnavailableError):
            watch._neotoma_query("transcription", limit=1)
