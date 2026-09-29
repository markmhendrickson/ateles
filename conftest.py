"""Repo-root pytest configuration shared by every suite.

Only loaded when pytest's rootdir is the repo root (CI and the usual local
invocations); suites that are run from inside their own directory get the same
guard through their own conftest.py, which import the same hook.
"""

import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Redact host environment values from every failure report so an
# env-capturing test can never print a real credential into a terminal or an
# agent transcript (2026-09-29 incident). See lib/pytest_env_guard.py.
from lib.pytest_env_guard import (  # noqa: E402,F401
    pytest_collection_finish,
    pytest_runtest_makereport,
)
