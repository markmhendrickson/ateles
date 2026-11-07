"""The test session never resolves a Notifier to the live held queue.

Daemons build a module-level Notifier at import, during collection and before
any fixture runs, so the per-test fixture alone cannot protect them. The
session-wide paths set in the root conftest's pytest_configure do.
"""

import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.notify import Notifier  # noqa: E402

# Built while this module is imported, i.e. at collection, before any fixture.
_BUILT_AT_IMPORT = Notifier(rubric={})


def _is_live(path: Path) -> bool:
    live_dir = Path(tempfile.gettempdir())
    return path.parent == live_dir or path.name.startswith("ateles-notify")


def test_notifier_built_at_import_time_is_isolated():
    assert not _is_live(_BUILT_AT_IMPORT._digest_path), _BUILT_AT_IMPORT._digest_path
    assert not _is_live(_BUILT_AT_IMPORT._dedupe_path), _BUILT_AT_IMPORT._dedupe_path


def test_notifier_built_in_a_test_is_isolated():
    n = Notifier(rubric={})
    assert not _is_live(n._digest_path) and not _is_live(n._dedupe_path)
