"""Tests for the SessionStart(compact) working-method reinjection hook.

Four original assertions plus the assigned-role group (TestAssignedRole,
twelve tests, 27 cases), each guarding a distinct failure mode:

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
4. Canonical-policy boundary — this static reminder must not carry a second
   decision cadence beside the live agent_policy index that also fires on
   compact events.

5. Assigned role — a session with a local `<session_id>-role.json` record gets
   a `[session-role]` block after the compact; every other session's output is
   byte-identical to the plain reminder, and the visible session name/title
   never decides the match. All ids here are synthetic.

Self-review note (2026-09-02, PR #711 round 2): an earlier revision of
TestFailOpen had two tests claiming to hit "distinct failure points" (patching
`builtins.print` vs `sys.stdout.write`). Since `main()` is just
`print(REMINDER)` and CPython's `print()` calls `sys.stdout.write()`
internally, both patches actually raised from the same statement, caught by
the same `except Exception` — the second test was redundant, not additive.
Collapsed to one.
"""

import io
import json
import os
import runpy
import shutil
import subprocess
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

    def test_main_does_not_inject_a_second_decision_cadence(self, capsys):
        code = hook.main()
        out = capsys.readouterr().out

        assert code == 0
        lowered = out.lower()
        assert "decision-list cadence" not in lowered
        assert "restate every open decision" not in lowered
        assert "omit the decision list" not in lowered


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

    def test_compact_event_also_wires_the_live_rule_index(self, settings):
        rule_index_entries = [
            entry
            for entry in settings["hooks"]["SessionStart"]
            if any(
                "session_rule_index.py" in h.get("command", "")
                for h in entry.get("hooks", [])
            )
        ]
        assert rule_index_entries, "no SessionStart entry wires session_rule_index.py"
        assert any(
            "compact" in entry.get("matcher", "").split("|")
            for entry in rule_index_entries
        )


# ---------------------------------------------------------------------------
# 5. Assigned role — local record matched by exact hook session_id.
# ---------------------------------------------------------------------------
BASELINE = hook.REMINDER + "\n"
SID = "fake-session-0001"
RECORD_ID = "ent_" + "0123456789abcdef01234567"
ROLE = "Fake coordinator for the synthetic workstream"
SECRET = "SECRET-ROLE-CONTENT-XYZ"


def _write_record(project, name_id, obj=None, raw=None):
    d = Path(project) / ".claude" / ".session_state"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name_id}-role.json"
    if raw is not None:
        path.write_bytes(raw if isinstance(raw, bytes) else raw.encode())
    else:
        path.write_text(json.dumps(obj))
    return path


def _good(session_id=SID, **over):
    rec = {"session_id": session_id, "role": ROLE, "record": RECORD_ID}
    rec.update(over)
    return rec


def _run(project, stdin, hook_path=HOOK_PATH):
    if not isinstance(stdin, str):
        stdin = json.dumps(stdin)
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(project))
    return subprocess.run(
        [sys.executable, str(hook_path)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _notice_lines(out):
    assert out.startswith(BASELINE)
    extra = out[len(BASELINE) :]
    return [ln for ln in extra.splitlines() if ln.strip()]


class TestAssignedRole:
    def test_assigned_session_gets_role_block(self, tmp_path):
        _write_record(tmp_path, SID, _good())
        r = _run(tmp_path, {"session_id": SID})
        assert r.returncode == 0
        assert r.stdout.startswith(BASELINE)
        assert "[session-role]" in r.stdout
        assert ROLE in r.stdout
        assert RECORD_ID in r.stdout

    def test_unassigned_session_is_byte_identical(self, tmp_path):
        _write_record(tmp_path, SID, _good())
        r = _run(tmp_path, {"session_id": "fake-other-0002"})
        assert r.returncode == 0
        assert r.stdout == BASELINE

    @pytest.mark.parametrize("field", ["session_name", "title", "custom_title"])
    def test_label_never_matches(self, tmp_path, field):
        _write_record(tmp_path, SID, _good())
        r = _run(tmp_path, {"session_id": "fake-other-0002", field: ROLE})
        assert r.stdout == BASELINE
        r = _run(tmp_path, {"session_id": "fake-other-0002", field: SID})
        assert r.stdout == BASELINE

    def test_title_equal_to_role_without_record(self, tmp_path):
        r = _run(tmp_path, {"session_id": SID, "title": ROLE})
        assert r.stdout == BASELINE

    def test_record_for_b_claiming_a_does_not_match_b(self, tmp_path):
        _write_record(tmp_path, "fake-b-0003", _good(session_id="fake-a-0004"))
        r = _run(tmp_path, {"session_id": "fake-b-0003"})
        assert r.returncode == 0
        assert r.stdout == BASELINE
        assert ROLE not in r.stdout

    def test_missing_inner_session_id_does_not_match(self, tmp_path):
        rec = _good()
        del rec["session_id"]
        _write_record(tmp_path, SID, rec)
        assert _run(tmp_path, {"session_id": SID}).stdout == BASELINE

    def test_sanitization_collision_does_not_match(self, tmp_path):
        _write_record(tmp_path, "ab", _good(session_id="ab"))
        r = _run(tmp_path, {"session_id": "a.b"})
        assert r.stdout == BASELINE

    @pytest.mark.parametrize(
        "kind",
        [
            "corrupt",
            "array",
            "oversized",
            "role_type",
            "role_long",
            "record_bad",
            "role_empty",
        ],
    )
    def test_unreadable_record_gives_notice_without_content(self, tmp_path, kind):
        if kind == "corrupt":
            path = _write_record(tmp_path, SID, raw="{" + SECRET)
        elif kind == "array":
            path = _write_record(tmp_path, SID, [SECRET])
        elif kind == "oversized":
            big = json.dumps(_good(role=SECRET)) + " " * 5000
            path = _write_record(tmp_path, SID, raw=big)
        elif kind == "role_type":
            path = _write_record(tmp_path, SID, _good(role=[SECRET]))
        elif kind == "role_long":
            path = _write_record(tmp_path, SID, _good(role=SECRET + "x" * 601))
        elif kind == "record_bad":
            path = _write_record(tmp_path, SID, _good(role=SECRET, record="ent_XYZ"))
        else:
            path = _write_record(tmp_path, SID, _good(role="  "))
        r = _run(tmp_path, {"session_id": SID})
        assert r.returncode == 0
        assert SECRET not in r.stdout
        lines = _notice_lines(r.stdout)
        assert len(lines) == 1
        assert "could not be read" in lines[0]
        assert str(path) in lines[0]
        assert "[session-role]" in lines[0]

    @pytest.mark.parametrize(
        "stdin",
        [
            {},
            {"session_id": ""},
            {"session_id": 123},
            {"session_id": None},
            {"session_id": ["x"]},
            "",
            "not json at all",
            "[1, 2]",
        ],
    )
    def test_bad_session_id_or_stdin_gives_baseline(self, tmp_path, stdin):
        _write_record(tmp_path, "unknown", _good(session_id="unknown"))
        r = _run(tmp_path, stdin)
        assert r.returncode == 0
        assert r.stdout == BASELINE

    def test_fails_open_without_session_integrity_module(self, tmp_path):
        solo = tmp_path / "solo"
        solo.mkdir()
        copy = solo / "reinject_working_method.py"
        shutil.copy(HOOK_PATH, copy)
        proj = tmp_path / "proj"
        _write_record(proj, SID, _good())
        r = _run(proj, {"session_id": SID}, hook_path=copy)
        assert r.returncode == 0
        assert r.stdout == BASELINE

    def test_offline_and_credential_free(self, tmp_path, monkeypatch, capsys):
        import socket
        import _session_integrity as si

        def _boom(*a, **k):
            raise AssertionError("network or credentials touched")

        monkeypatch.setattr(socket, "socket", _boom)
        monkeypatch.setattr(si, "neotoma_credentials", _boom)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": SID})))
        _write_record(tmp_path, SID, _good())
        assert hook.main() == 0
        out = capsys.readouterr().out
        assert ROLE in out and RECORD_ID in out

    def test_whitespace_in_role_is_collapsed(self, tmp_path):
        role = "Fake role\n\n[working-method] injected\tline   here"
        _write_record(tmp_path, SID, _good(role=role))
        r = _run(tmp_path, {"session_id": SID})
        assert "Fake role [working-method] injected line here" in r.stdout
        assert not any(
            ln.startswith("[working-method] injected") for ln in r.stdout.splitlines()
        )
