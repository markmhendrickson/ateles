"""With lib.adapters import blocked -> RUNTIME_MISSING, non-zero; not an empty pass."""

from __future__ import annotations

import builtins

import pytest

from execution.conformance.row_result import FailureCode
from execution.conformance.adapter_admission.runner import run
from execution.conformance.adapter_admission.runtime import RuntimeMissingError, import_sixth_adapter_factory


def test_import_sixth_adapter_factory_raises_when_lib_adapters_absent():
    # lib.adapters genuinely does not exist on this checkout (ateles#1190 is open, unmerged).
    with pytest.raises(RuntimeMissingError):
        import_sixth_adapter_factory()


def test_import_blocked_explicitly_still_raises(monkeypatch):
    real_import = builtins.__import__

    def blocking_import(name, *args, **kwargs):
        if name == "lib.adapters" or name.startswith("lib.adapters"):
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocking_import)
    with pytest.raises(RuntimeMissingError):
        import_sixth_adapter_factory()


def test_run_reports_runtime_missing_not_empty_pass():
    report = run("adapter-admission")  # no _use_fake_on_missing: real import path
    assert report.exit_code != 0
    assert any(f.code == FailureCode.RUNTIME_MISSING for f in report.failures)
    assert not report.rows  # no row silently reported green


def test_runtime_missing_names_1190_and_lib_adapters():
    report = run("adapter-admission")
    failure = next(f for f in report.failures if f.code == FailureCode.RUNTIME_MISSING)
    assert "1190" in failure.next_action
    assert "lib.adapters" in failure.hint
