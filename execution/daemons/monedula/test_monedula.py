"""
Effect tests for monedula.py — ateles#243 (NEOTOMA_BASE_URL fail-fast / fail-loud).

Run with: pytest execution/daemons/monedula/test_monedula.py -v
"""

from __future__ import annotations

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

import monedula  # noqa: E402


class _FakeProfile:
    def __init__(self, task_id: str):
        self.neotoma_task_id = task_id


class _FakeHandler:
    def __init__(self, task_id: str):
        self.profile = _FakeProfile(task_id)


# ── fetch_due_payment_tasks: abort the whole scan, don't partially return ───


class TestFetchDuePaymentTasksAbortsScanOnNeotomaUnavailable:
    def test_raises_and_does_not_return_partial_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(monedula, "NEOTOMA_BASE_URL", "https://neotoma.example.com:9180")

        def _boom(entity_id: str) -> dict | None:
            raise monedula.NeotomaUnavailableError("connection refused")

        monkeypatch.setattr(monedula, "_fetch_entity_by_id", _boom)
        handlers = [_FakeHandler("ent_task_1"), _FakeHandler("ent_task_2")]

        with pytest.raises(monedula.NeotomaUnavailableError):
            monedula.fetch_due_payment_tasks(handlers)

    def test_one_failing_task_aborts_even_if_others_would_succeed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(monedula, "NEOTOMA_BASE_URL", "https://neotoma.example.com:9180")
        calls: list[str] = []

        def _mixed(entity_id: str) -> dict | None:
            calls.append(entity_id)
            if entity_id == "ent_task_2":
                raise monedula.NeotomaUnavailableError("timeout")
            return {"snapshot": {"due_date": "2020-01-01", "title": "t"}}

        monkeypatch.setattr(monedula, "_fetch_entity_by_id", _mixed)
        handlers = [_FakeHandler("ent_task_1"), _FakeHandler("ent_task_2")]

        with pytest.raises(monedula.NeotomaUnavailableError):
            monedula.fetch_due_payment_tasks(handlers)

    def test_success_returns_only_due_tasks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(monedula, "NEOTOMA_BASE_URL", "https://neotoma.example.com:9180")

        def _fake(entity_id: str) -> dict | None:
            due = "2020-01-01" if entity_id == "ent_due" else "2999-01-01"
            return {"snapshot": {"due_date": due, "title": entity_id}}

        monkeypatch.setattr(monedula, "_fetch_entity_by_id", _fake)
        handlers = [_FakeHandler("ent_due"), _FakeHandler("ent_future")]

        result = monedula.fetch_due_payment_tasks(handlers)
        assert len(result) == 1
        assert result[0]["snapshot"]["title"] == "ent_due"


# ── _fetch_entity_by_id: connection failure raises, doesn't return None ─────


class TestFetchEntityByIdRaisesOnConnectionFailure:
    def test_url_error_raises_neotoma_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(monedula, "NEOTOMA_BASE_URL", "https://neotoma.example.com:9180")

        def _boom(*args: object, **kwargs: object) -> None:
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(monedula.urllib.request, "urlopen", _boom)

        with pytest.raises(monedula.NeotomaUnavailableError):
            monedula._fetch_entity_by_id("ent_x")


# ── main() startup: resolve_neotoma_base_url() called before dispatch loop ──


class TestMainResolvesNeotomaBaseUrlAtStartup:
    def test_main_raises_neotoma_config_error_before_any_polling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> str:
            from execution.lib.neotoma_config import NeotomaConfigError

            raise NeotomaConfigError("NEOTOMA_BASE_URL is not set")

        monkeypatch.setattr(monedula, "resolve_neotoma_base_url", _boom)
        already_ran_mock = MagicMock()
        monkeypatch.setattr(monedula, "_check_already_ran_today", already_ran_mock)

        from execution.lib.neotoma_config import NeotomaConfigError

        with pytest.raises(NeotomaConfigError):
            monedula.main()

        already_ran_mock.assert_not_called()


# ── main(): a Neotoma outage mid-run must clear the ran-today marker ───────
# (code-review finding: idempotency guard was set via _mark_ran_today() before
# the now-raising load_handlers()/fetch_due_payment_tasks() calls; without
# clearing on failure, a transient Neotoma outage would silently skip the rest
# of that day's runs — the exact "unknown treated as absent" failure class
# ateles#243 exists to close, just at the daemon-idempotency layer instead of
# the Neotoma-query layer.)


class TestMainClearsRanTodayMarkerOnNeotomaOutage:
    def test_neotoma_unavailable_during_handler_load_clears_marker(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(monedula, "resolve_neotoma_base_url", lambda: "https://neotoma.example.com:9180")
        monkeypatch.setattr(monedula, "_check_already_ran_today", lambda: False)

        state_file = tmp_path / ".monedula_last_run"
        monkeypatch.setattr(monedula, "STATE_FILE", state_file)

        def _boom_load_handlers():
            raise monedula.NeotomaUnavailableError("connection refused")

        fake_handlers_module = MagicMock()
        fake_handlers_module.load_handlers = _boom_load_handlers
        monkeypatch.setitem(sys.modules, "handlers", fake_handlers_module)

        with pytest.raises(monedula.NeotomaUnavailableError):
            monedula.main()

        assert not state_file.exists(), (
            "ran-today marker must be cleared when a Neotoma outage aborts "
            "the run, so the next launchd trigger retries instead of "
            "silently skipping the rest of the day"
        )

    def test_neotoma_unavailable_during_task_scan_clears_marker(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(monedula, "resolve_neotoma_base_url", lambda: "https://neotoma.example.com:9180")
        monkeypatch.setattr(monedula, "_check_already_ran_today", lambda: False)

        state_file = tmp_path / ".monedula_last_run"
        monkeypatch.setattr(monedula, "STATE_FILE", state_file)

        fake_handlers_module = MagicMock()
        fake_handlers_module.load_handlers = lambda: []
        monkeypatch.setitem(sys.modules, "handlers", fake_handlers_module)
        monkeypatch.setattr(monedula, "fetch_yesterday_events", lambda: [])

        def _boom_fetch_tasks(handlers):
            raise monedula.NeotomaUnavailableError("timeout")

        monkeypatch.setattr(monedula, "fetch_due_payment_tasks", _boom_fetch_tasks)

        with pytest.raises(monedula.NeotomaUnavailableError):
            monedula.main()

        assert not state_file.exists(), (
            "ran-today marker must be cleared when the due-task scan aborts, "
            "not just when handler-loading fails"
        )

    def test_successful_run_keeps_marker_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(monedula, "resolve_neotoma_base_url", lambda: "https://neotoma.example.com:9180")
        monkeypatch.setattr(monedula, "_check_already_ran_today", lambda: False)

        state_file = tmp_path / ".monedula_last_run"
        monkeypatch.setattr(monedula, "STATE_FILE", state_file)

        fake_handlers_module = MagicMock()
        fake_handlers_module.load_handlers = lambda: []
        monkeypatch.setitem(sys.modules, "handlers", fake_handlers_module)
        monkeypatch.setattr(monedula, "fetch_yesterday_events", lambda: [])
        monkeypatch.setattr(monedula, "fetch_due_payment_tasks", lambda handlers: [])

        monedula.main()

        assert state_file.exists(), (
            "a clean run with nothing to do must still keep the ran-today "
            "marker set (no regression to the pre-existing no-op-day path)"
        )
