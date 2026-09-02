"""Tests for the SessionStart(compact) working-method reinjection hook.

Three assertions, each guarding a distinct failure mode:

1. Happy path — the hook prints the reminder, including the five numbered
   rules, and exits 0. Substrings from REMINDER, not full-string equality,
   so the test survives future wording tweaks to the rule text.
2. Fail-open — the `__main__` guard (lines 68-72) must swallow ANY exception
   from main()/print() and still exit 0, since this hook must never block a
   session resume. Importing and calling main() directly skips that guard
   entirely, so this drives the real `__main__` block via runpy.
3. Settings contract — the bug this hook fixes was `session_start.py`
   registered against a SessionStart matcher that excluded `compact`. Pin
   both halves: the matcher covering `session_start.py` includes `compact`,
   and a dedicated `compact` entry wires `reinject_working_method.py`.

Self-review note (2026-09-02, PR #711 round 2): an earlier revision of
TestFailOpen had two tests claiming to hit "distinct failure points" (patching
`builtins.print` vs `sys.stdout.write`). Since `main()` is just
`print(REMINDER)` and CPython's `print()` calls `sys.stdout.write()`
internally, both patches actually raised from the same statement, caught by
the same `except Exception` — the second test was redundant, not additive.
Collapsed to one.
"""

import json
import runpy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import reinject_working_method as hook

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_PATH = Path(__file__).resolve().parent / "reinject_working_method.py"


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------
class TestHappyPath:
    def test_main_prints_reminder_and_exits_zero(self, capsys):
        code = hook.main()
        out = capsys.readouterr().out
        assert code == 0
        assert "[working-method]" in out
        assert "1. DISPATCH" in out
        assert "5. PROCEED" in out


# ---------------------------------------------------------------------------
# 2. Fail-open — drive the actual `__main__` block, not main() in isolation.
# ---------------------------------------------------------------------------
class TestFailOpen:
    # runpy.run_path executes the module in a FRESH namespace, so patching
    # the already-imported `hook` module's attributes (e.g. `hook.main`)
    # has no effect on the code runpy actually runs — that variant would
    # pass even if the `except Exception: sys.exit(0)` guard were deleted.
    # Force the raise via `builtins`, which every fresh namespace shares,
    # so the exception genuinely originates inside the executed __main__
    # block. Mutation-tested: removing the try/except from the hook's
    # `__main__` block makes this test fail, confirming it exercises the
    # real guard rather than passing vacuously.
    def test_main_block_exits_zero_when_print_raises(self, monkeypatch):
        def _raise(*args, **kwargs):
            raise OSError("stdout broke")

        monkeypatch.setattr("builtins.print", _raise)
        with pytest.raises(SystemExit) as exc_info:
            runpy.run_path(str(HOOK_PATH), run_name="__main__")
        assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# 3. Settings contract — the matcher gap this hook exists to close.
# ---------------------------------------------------------------------------
class TestSettingsContract:
    @pytest.fixture
    def settings(self):
        settings_path = REPO_ROOT / ".claude" / "settings.json"
        return json.loads(settings_path.read_text())

    def test_session_start_matcher_includes_compact(self, settings):
        session_start_entries = [
            entry
            for entry in settings["hooks"]["SessionStart"]
            if any(
                "session_start.py" in h.get("command", "")
                for h in entry.get("hooks", [])
            )
        ]
        assert session_start_entries, "no SessionStart entry wires session_start.py"
        for entry in session_start_entries:
            matcher = entry.get("matcher", "")
            assert "compact" in matcher.split("|")

    def test_compact_entry_wires_reinject_working_method(self, settings):
        compact_entries = [
            entry
            for entry in settings["hooks"]["SessionStart"]
            if entry.get("matcher") == "compact"
        ]
        assert compact_entries, "no SessionStart entry matches 'compact'"
        assert any(
            "reinject_working_method.py" in h.get("command", "")
            for entry in compact_entries
            for h in entry.get("hooks", [])
        )
