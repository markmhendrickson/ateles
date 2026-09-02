"""Pytest path bootstrap: daemons import repo-root packages and sibling
modules as top-level (same as the standalone-script runtime path setup)."""

import sys
from pathlib import Path

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent

for p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture(autouse=True)
def _isolate_dispatch_failure_logs(monkeypatch, tmp_path):
    """Never let a test write diagnostics into the operator's real log directory.

    `write_dispatch_failure_log` resolves `DISPATCH_FAILURE_LOG_DIR` at call time
    and defaults to ~/Library/Logs/ateles/dispatch-failures/ — the same directory
    a live daemon writes to. Any test that reaches a dispatch-failure path (now
    including the exit-0-but-no-PR post-condition path) would deposit fixture
    output like `owner/repo#100` there, polluting real diagnostic evidence.
    Autouse so a future test cannot reintroduce the leak by forgetting to patch.
    """
    import skill_runner

    monkeypatch.setattr(
        skill_runner, "DISPATCH_FAILURE_LOG_DIR", tmp_path / "dispatch-failures"
    )


@pytest.fixture(autouse=True)
def _default_feature_workflow(monkeypatch):
    """Serve the `ateles|feature` workflow_definition to every dispatch test.

    Gate sequences now come from `workflow_definition` entities rather than a
    hardcoded tuple, and an unresolvable workflow fails CLOSED — so without this
    every legacy waive / gates_green test would fail on a refusal rather than on
    the behaviour it is actually asserting. This fixture supplies the workflow
    those tests were written against (`ateles|feature`: pm, then ux+arch), for
    ANY repo, so their intent is preserved unchanged.

    It is deliberately a plain default, not a bypass: a test that cares which
    workflow is in force overrides `httpx.post` itself (see
    `test_workflow_definition_drives_dispatch.py`), and the resolver cache is
    cleared around every test so one test's stored entity can never decide
    another's outcome.
    """
    from lib.daemon_runtime import workflow_resolver as _wr

    _gates = [
        {"phase": 1, "gate_name": "pm", "owner_agent": "pavo", "required": True},
        {"phase": 2, "gate_name": "ux", "owner_agent": "accipiter", "required": True},
        {"phase": 2, "gate_name": "arch", "owner_agent": "waxwing", "required": True},
        {"phase": 3, "gate_name": "impl", "owner_agent": "cicada", "required": True},
        {"phase": 4, "gate_name": "pr_review", "owner_agent": "vanellus",
         "required": True},
    ]

    def _fetch(project: str):
        return [
            _wr.ResolvedWorkflow(
                entity_id="ent_test_feature",
                project=project,
                workflow_type="feature",
                gates=_wr.validate_gates(_gates, entity_id="ent_test_feature"),
            )
        ]

    monkeypatch.setattr(_wr, "_fetch_definitions", _fetch)
    _wr.clear_cache()
    yield
    _wr.clear_cache()
