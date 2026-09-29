"""Repo-root pytest configuration shared by every suite.

Loaded whenever pytest's rootdir is the repo root, which is the case for CI
and for local runs started from the repo root or from any subdirectory
(pyproject.toml anchors the rootdir), so it is the guard's primary wiring.
``execution/daemons/apis/conftest.py`` imports the same hooks as well; the
other suite conftests rely on this file. Suites with their own ``pytest.ini``
(``mcp-servers/*``) do not load it.

The guard redacts host environment values from pytest output so an
env-capturing test can never print a real credential into a terminal or an
agent transcript (2026-09-29 incident). See ``lib/pytest_env_guard.py`` for
what is and is not covered. ``lib/test_pytest_env_guard.py`` asserts this file
imports every hook the guard provides.
"""

import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from lib.pytest_env_guard import (  # noqa: E402,F401
    pytest_collection_finish,
    pytest_configure,
    pytest_make_collect_report,
    pytest_runtest_makereport,
    pytest_warning_recorded,
)
