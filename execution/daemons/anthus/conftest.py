"""Pytest setup for the anthus suite."""

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.pytest_env_guard import clear_host_env  # noqa: E402


@pytest.fixture(autouse=True)
def _hermetic_host_env(monkeypatch):
    """Start every test from a synthetic environment.

    participation.py reads the Neotoma bearer token from the process
    environment at call time, so a host shell that exports it changed which
    branch `test_known_satisfied_gate_is_not_redispatched` took. Tests that
    need a variable set it themselves with ``monkeypatch.setenv``.
    """
    clear_host_env(monkeypatch)
