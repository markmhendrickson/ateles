"""
Make this package's modules importable by bare name in tests (e.g.
`from drift import ...`), matching the repo's in-dir test convention, whether
pytest is invoked from here or from the repo root.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_load_backoff(monkeypatch):
    """The agent_definition loader sleeps between retries; tests must not wait.

    The loader is importable under two names in this suite (bare and
    package-qualified), so both module objects are patched when present.
    """
    for name in ("agent_loader", "lib.daemon_runtime.agent_loader"):
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "_sleep"):
            monkeypatch.setattr(mod, "_sleep", lambda seconds: None)
