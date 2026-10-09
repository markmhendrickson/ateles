"""Repo-root pytest configuration: keep ambient credentials out of every test.

A test process inherits the environment of whatever started it, and in this
repository that is often a session or daemon that carries live credentials.
Pytest prints local variables and ``assert`` operands on failure, so a failing
assertion that mentions ``os.environ`` (or anything holding a copy of it) can
write every one of those values into a transcript or log. Removing the
credential-named variables before any test runs makes that impossible by
construction, rather than by each test remembering not to compare the whole
environment.

Two layers, both applied here:

* ``pytest_configure`` drops the ambient credential-named variables for the
  whole process, so collection-time imports and session fixtures cannot read
  them either.
* An autouse fixture drops any credential-named variable present when a test
  starts (restored afterwards by ``monkeypatch``), covering a variable that a
  collection-time import or an earlier test left behind.

A test that needs a credential-shaped variable sets its own synthetic value
(``monkeypatch.setenv``) after the fixture has run; that value belongs to the
test and is not touched. There is deliberately no switch that restores the
ambient values: a test that needs a real credential is an integration run and
belongs outside the unit-test lanes.

Which names count as credentials is defined once, in
``lib/credential_env_names.py``, shared with the launchd plist renderer.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = str(Path(__file__).resolve().parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from lib.credential_env_names import is_credential_env_name, plan_env_scrub  # noqa: E402


def credential_env_names(environ) -> list[str]:
    """The credential-named keys currently in *environ* (names only, never values)."""
    return sorted(name for name in list(environ) if is_credential_env_name(name))


def scrub_credential_env(environ) -> list[str]:
    """Scrub *environ* in place; return the names removed.

    Credential-named variables are removed. Git's grouped configuration
    (``GIT_CONFIG_COUNT`` / ``GIT_CONFIG_KEY_<n>`` / ``GIT_CONFIG_VALUE_<n>``)
    is rewritten as one unit, never member by member, so it stays valid and
    ``git`` keeps working while no credential-bearing entry survives.
    """
    remove, updates = plan_env_scrub(environ)
    for name in remove:
        environ.pop(name, None)
    environ.update(updates)
    return remove


def pytest_configure(config):  # noqa: ARG001 — pytest hook signature
    scrub_credential_env(os.environ)


@pytest.fixture(autouse=True)
def _scrub_credential_environment(monkeypatch):
    """Start every test with no credential-bearing variable in ``os.environ``."""
    remove, updates = plan_env_scrub(os.environ)
    for name in remove:
        monkeypatch.delenv(name, raising=False)
    for name, value in updates.items():
        monkeypatch.setenv(name, value)
