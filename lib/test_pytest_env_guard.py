"""Tests for lib/pytest_env_guard.py.

Every secret-shaped value here is a DUMMY. The subprocess tests build a child
pytest run whose environment carries a dummy credential, make a test fail in
the most leak-prone ways, and assert the dummy never reaches the output. A
control run without the guard proves the instrument can see a leak at all
(a zero from a broken probe would look the same as a pass).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from lib import pytest_env_guard as guard

_REPO_ROOT = Path(__file__).resolve().parent.parent

DUMMY_NAME = "DUMMY_SECRET_TOKEN"
DUMMY_VALUE = "DUMMYSECRETVALUE_abcdef123456"
DUMMY_PATH_NAME = "NEOTOMA_AAUTH_PRIVATE_JWK_PATH"
DUMMY_PATH_VALUE = "/dummy/host/keys/DUMMYHOST.jwk.json"


# ── unit level ────────────────────────────────────────────────────────────────


def test_snapshot_keeps_long_values_and_drops_short_and_safe_names():
    snap = guard.snapshot_secret_values(
        {
            "LONG_ONE": "x" * 13,
            "SHORT_ONE": "x" * 12,
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "PYTEST_CURRENT_TEST": "a" * 40,
            "LC_ALL": "en_US.UTF-8-and-more-chars",
        }
    )
    assert snap == {"x" * 13: "LONG_ONE"}


def test_scrub_text_redacts_value_and_names_the_variable():
    out = guard.scrub_text(
        "assert 'X' not in {'K': '%s'}" % DUMMY_VALUE, {DUMMY_VALUE: DUMMY_NAME}
    )
    assert DUMMY_VALUE not in out
    assert "<redacted:%s>" % DUMMY_NAME in out


def test_scrub_text_leaves_short_or_unlisted_values_alone():
    text = "assert 'a' == 'b' and 'PATH': '/bin'"
    assert guard.scrub_text(text, {}) == text


@pytest.mark.parametrize(
    "name", ["GITHUB_TOKEN", "SOME_API_KEY", "NEOTOMA_MNEMONIC", "X_PRIVATE_KEY"]
)
def test_scrub_text_redacts_credential_keyed_pairs_not_in_the_snapshot(name):
    out = guard.scrub_text("{'%s': 'value-loaded-after-startup'}" % name, {})
    assert "value-loaded-after-startup" not in out
    assert "<redacted:%s>" % name in out


def test_scrub_text_keeps_non_credential_keyed_pairs():
    text = "{'APIS_HARNESS_PROVIDERS': 'claude'}"
    assert guard.scrub_text(text, {}) == text


def test_scrub_is_idempotent():
    once = guard.scrub_text("v=%s" % DUMMY_VALUE, {DUMMY_VALUE: DUMMY_NAME})
    assert guard.scrub_text(once, {DUMMY_VALUE: DUMMY_NAME}) == once


def test_scrub_report_fails_closed_when_the_scrubber_errors(monkeypatch):
    class Report:
        longrepr = "body containing %s" % DUMMY_VALUE
        sections = [("Captured stdout call", DUMMY_VALUE)]

    def boom(*_a, **_k):
        raise RuntimeError("scrubber bug")

    monkeypatch.setattr(guard, "_scrub_obj", boom)
    report = Report()
    guard.scrub_report(report, {DUMMY_VALUE: DUMMY_NAME})
    assert DUMMY_VALUE not in str(report.longrepr)
    assert "withheld" in report.longrepr
    assert report.sections == []


def test_assert_env_keys_absent_reports_names_only():
    env = {"KEEP": "v", DUMMY_NAME: DUMMY_VALUE}
    with pytest.raises(AssertionError) as exc:
        guard.assert_env_keys_absent(env, DUMMY_NAME, "OTHER", why="must not leak")
    message = str(exc.value)
    assert DUMMY_NAME in message and "must not leak" in message
    assert DUMMY_VALUE not in message and "KEEP" not in message


def test_assert_env_keys_absent_passes_when_absent_and_for_none():
    guard.assert_env_keys_absent({"A": "1"}, "B")
    guard.assert_env_keys_absent(None, "B")


def test_hermetic_environ_lists_names_to_remove_and_keeps_the_allowlist():
    env = {
        "PATH": "/bin",
        "HOME": "/h",
        "PYTEST_X": "1",
        "LC_ALL": "C",
        DUMMY_NAME: DUMMY_VALUE,
    }
    assert guard.hermetic_environ(env) == [DUMMY_NAME]


def test_clear_host_env_removes_a_simulated_host_var(monkeypatch):
    monkeypatch.setenv(DUMMY_PATH_NAME, DUMMY_PATH_VALUE)
    monkeypatch.setenv(DUMMY_NAME, DUMMY_VALUE)
    guard.clear_host_env(monkeypatch)
    import os

    assert guard.env_keys_present(os.environ, DUMMY_PATH_NAME, DUMMY_NAME) == []


# ── end to end: a child pytest whose environment carries a dummy secret ──────

_LEAKY_TESTS = textwrap.dedent(
    """
    import os

    def _child_env():
        return dict(os.environ)          # what daemon code copies into a subprocess

    def test_not_in_renders_the_whole_mapping():
        captured = _child_env()
        assert "NOT_A_REAL_VAR" in captured

    def test_dict_equality_renders_both_sides():
        assert _child_env() == {"only": "this"}

    def test_locals_are_shown_under_dash_l():
        child_env = _child_env()
        raise RuntimeError("boom")

    def test_captured_stdout_is_shown():
        print(os.environ["DUMMY_SECRET_TOKEN"])
        assert False
    """
)


def _run_child_pytest(tmp_path: Path, *, with_guard: bool) -> str:
    (tmp_path / "test_leaky.py").write_text(_LEAKY_TESTS)
    conftest = ""
    if with_guard:
        conftest = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "from lib.pytest_env_guard import pytest_runtest_makereport  # noqa: F401\n"
            % str(_REPO_ROOT)
        )
    (tmp_path / "conftest.py").write_text(conftest)
    child_env = {
        "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        DUMMY_NAME: DUMMY_VALUE,
        DUMMY_PATH_NAME: DUMMY_PATH_VALUE,
    }
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--rootdir",
            str(tmp_path),
            "--tb=long",
            "-l",
            "-q",
            str(tmp_path / "test_leaky.py"),
        ],
        env=child_env,
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=120,
    )
    assert done.returncode == 1, "the four deliberately leaky tests must fail"
    return done.stdout + done.stderr


def test_control_without_guard_the_leaky_tests_do_print_the_dummy(tmp_path):
    """Instrument check: with no guard the dummy IS visible, so a clean run
    below is evidence and not an artifact of a probe that cannot see leaks."""
    out = _run_child_pytest(tmp_path, with_guard=False)
    assert DUMMY_VALUE in out


def test_guard_keeps_the_dummy_out_of_every_failure_rendering(tmp_path):
    out = _run_child_pytest(tmp_path, with_guard=True)
    assert DUMMY_VALUE not in out
    assert DUMMY_PATH_VALUE not in out
    assert "<redacted:%s>" % DUMMY_NAME in out
    assert "4 failed" in out


def test_changed_during_collection_detects_an_import_time_mutation(tmp_path):
    """The detector must be able to fail: a module that mutates the process
    environment at import time is reported, a variable the host already
    exported is not, and per-test clearing cannot hide the import-time change."""
    (tmp_path / "conftest.py").write_text(
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "from lib.pytest_env_guard import pytest_collection_finish  # noqa: F401\n"
        % str(_REPO_ROOT)
    )
    (tmp_path / "test_probe.py").write_text(
        textwrap.dedent(
            """
            import os
            import pytest
            from lib.pytest_env_guard import changed_during_collection

            os.environ["SET_AT_IMPORT"] = "changed-by-import"   # simulated bootstrap

            @pytest.fixture(autouse=True)
            def _clear(monkeypatch):
                monkeypatch.delenv("SET_AT_IMPORT", raising=False)
                monkeypatch.delenv("EXPORTED_BY_HOST", raising=False)

            def test_import_time_change_is_seen_despite_clearing():
                assert changed_during_collection("SET_AT_IMPORT")

            def test_host_exported_variable_is_not_a_change():
                assert not changed_during_collection("EXPORTED_BY_HOST")
            """
        )
    )
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--rootdir",
            str(tmp_path),
            "--tb=line",
            "-q",
            str(tmp_path / "test_probe.py"),
        ],
        env={
            "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "EXPORTED_BY_HOST": "exported-before-pytest-started",
        },
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=120,
    )
    assert done.returncode == 0 and "2 passed" in done.stdout
