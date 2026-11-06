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


@pytest.fixture(autouse=True)
def _fresh_load_workers(monkeypatch):
    """Each test gets its own request workers, so one test's stuck request cannot
    hold a permit another test needs. Both module objects are patched when present."""
    import concurrent.futures
    import threading

    pools = []
    for name in ("agent_loader", "lib.daemon_runtime.agent_loader"):
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "_load_pool"):
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=mod.LOAD_WORKERS)
            pools.append(pool)
            monkeypatch.setattr(mod, "_load_pool", pool)
            monkeypatch.setattr(mod, "_load_permits", threading.BoundedSemaphore(mod.LOAD_WORKERS))
    yield
    for pool in pools:
        pool.shutdown(wait=False)
