"""Tests for lib/pytest_env_guard.py.

Every secret-shaped value here is a DUMMY. The subprocess tests build a child
pytest run whose environment carries a dummy credential, make a test fail in
the most leak-prone ways, and assert the dummy never reaches the output. A
control run without the guard proves the instrument can see a leak at all
(a zero from a broken probe would look the same as a pass).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
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


# ── round 2: escaped forms, other channels, binding ──────────────────────────

MULTI_NAME = "DUMMY_MULTILINE_BLOB"  # deliberately NOT credential-shaped
MULTI_LINES = (
    "-----BEGIN DUMMY KEY-----",
    "DUMMYKEYLINEONE_aaaaaaaaaaaaaaaa",
    "DUMMYKEYLINETWO_bbbbbbbbbbbbbbbb",
    "-----END DUMMY KEY-----",
)
MULTI_VALUE = "\n".join(MULTI_LINES)


def _forms(value: str) -> dict:
    """The renderings a failure message can carry for ``value``."""
    return {
        "raw": value,
        "repr_body": repr(value)[1:-1],
        "json_body": json.dumps(value)[1:-1],
        "unicode_escape": value.encode("unicode_escape").decode(),
        "repr_of_repr_body": repr(repr(value))[1:-1],
    }


def test_snapshot_redacts_repr_json_and_per_line_forms_of_a_multiline_value():
    snap = guard.snapshot_secret_values({MULTI_NAME: MULTI_VALUE})
    for label, form in _forms(MULTI_VALUE).items():
        out = guard.scrub_text("before %s after" % form, snap)
        assert MULTI_LINES[1] not in out and MULTI_LINES[2] not in out, label
        assert "<redacted:%s>" % MULTI_NAME in out, label
    for line in MULTI_LINES:
        if len(line) > guard.MIN_SECRET_LEN:
            assert line not in guard.scrub_text("x %s y" % line, snap)


def test_snapshot_does_not_redact_short_lines_of_a_multiline_value():
    snap = guard.snapshot_secret_values({MULTI_NAME: "short\n" + MULTI_LINES[1]})
    assert "short" not in snap and "short\n" not in snap


_ESCAPE_LEAKS = textwrap.dedent(
    """
    import json, os

    V = os.environ["DUMMY_MULTILINE_BLOB"]

    def test_repr_in_explanation():
        assert "nope" == repr(V)

    def test_json_on_stdout():
        print(json.dumps(V))
        assert False

    def test_repr_body_in_message():
        raise RuntimeError("bad value " + repr(V)[1:-1])
    """
)

_COLLECTION_LEAK = "import os\nassert 'nope' == os.environ['DUMMY_SECRET_TOKEN']\n"

_WARNING_AND_LOG_LEAKS = textwrap.dedent(
    """
    import logging, os, warnings

    def test_warning_text():
        warnings.warn("leak " + os.environ["DUMMY_SECRET_TOKEN"])

    def test_live_log_line():
        logging.getLogger("probe").warning("leak %s", os.environ["DUMMY_SECRET_TOKEN"])
    """
)


def _child(tmp_path: Path, files: dict, *, with_guard: bool, args=()) -> str:
    for name, src in files.items():
        (tmp_path / name).write_text(src)
    conftest = ""
    if with_guard:
        conftest = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "from lib.pytest_env_guard import (  # noqa: F401\n    %s,\n)\n"
            % (str(_REPO_ROOT), ",\n    ".join(guard.HOOK_NAMES))
        )
    (tmp_path / "conftest.py").write_text(conftest)
    env = {
        "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        DUMMY_NAME: DUMMY_VALUE,
        MULTI_NAME: MULTI_VALUE,
    }
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--rootdir", str(tmp_path),
         "-q", *args, *[str(tmp_path / n) for n in files if n.startswith("test_")]],
        env=env, capture_output=True, text=True, cwd=str(tmp_path), timeout=120,
    )
    return done.stdout + done.stderr


def _assert_no_multiline_forms(out: str) -> None:
    for label, form in _forms(MULTI_VALUE).items():
        assert form not in out, label
    for line in MULTI_LINES[1:3]:
        assert line not in out


def test_control_escaped_forms_do_leak_without_the_guard(tmp_path):
    out = _child(tmp_path, {"test_esc.py": _ESCAPE_LEAKS}, with_guard=False, args=("--tb=short", "-vv"))
    assert MULTI_LINES[1] in out or _forms(MULTI_VALUE)["repr_body"] in out


def test_guard_redacts_escaped_forms_of_a_multiline_value(tmp_path):
    out = _child(tmp_path, {"test_esc.py": _ESCAPE_LEAKS}, with_guard=True, args=("--tb=long", "-l", "-vv"))
    _assert_no_multiline_forms(out)
    assert "3 failed" in out


def test_control_collection_errors_do_leak_without_the_guard(tmp_path):
    out = _child(tmp_path, {"test_collect.py": _COLLECTION_LEAK}, with_guard=False, args=("--tb=short", "-vv"))
    assert DUMMY_VALUE in out


def test_guard_redacts_collection_errors(tmp_path):
    out = _child(tmp_path, {"test_collect.py": _COLLECTION_LEAK}, with_guard=True, args=("--tb=long", "-l", "-vv"))
    assert DUMMY_VALUE not in out
    assert "error" in out.lower()


def test_control_warnings_and_live_logs_do_leak_without_the_guard(tmp_path):
    out = _child(tmp_path, {"test_wl.py": _WARNING_AND_LOG_LEAKS}, with_guard=False, args=("--log-cli-level=INFO",))
    assert out.count(DUMMY_VALUE) >= 2


def test_guard_redacts_warnings_summary_and_live_logging(tmp_path):
    out = _child(tmp_path, {"test_wl.py": _WARNING_AND_LOG_LEAKS}, with_guard=True, args=("--log-cli-level=INFO",))
    assert DUMMY_VALUE not in out
    assert "2 passed" in out


def test_scrub_report_withholds_when_it_meets_an_object_it_cannot_walk():
    class Foreign:  # e.g. a plugin's own TerminalRepr
        text = DUMMY_VALUE

    class Report:
        longrepr = Foreign()
        sections = []

    report = Report()
    guard.scrub_report(report, {DUMMY_VALUE: DUMMY_NAME})
    assert isinstance(report.longrepr, str) and "withheld" in report.longrepr


def test_scrub_report_withholds_past_the_depth_limit():
    deep = DUMMY_VALUE
    for _ in range(guard.MAX_DEPTH + 3):
        deep = [deep]

    class Report:
        longrepr = deep
        sections = []

    report = Report()
    guard.scrub_report(report, {DUMMY_VALUE: DUMMY_NAME})
    assert isinstance(report.longrepr, str) and "withheld" in report.longrepr


def test_withheld_message_names_the_exception_type_but_no_value(monkeypatch):
    def boom(*_a, **_k):
        raise KeyError(DUMMY_VALUE)

    monkeypatch.setattr(guard, "_scrub_obj", boom)

    class Report:
        longrepr = "x"
        sections = []

    report = Report()
    guard.scrub_report(report, {})
    assert "KeyError" in report.longrepr and DUMMY_VALUE not in report.longrepr


@pytest.mark.parametrize("key", ["author", "authored_by", "oauth_state", "private_repo", "keyword"])
def test_non_credential_keys_are_not_redacted(key):
    text = "{'%s': 'plain-value'}" % key
    assert guard.scrub_text(text, {}) == text


@pytest.mark.parametrize("key", ["apiKey", "accessToken", "client_secret", "GH_TOKEN", "api-key", "PAT"])
def test_credential_keys_are_redacted_in_quoted_pairs(key):
    out = guard.scrub_text("{'%s': 'plain-value'}" % key, {})
    assert "plain-value" not in out


@pytest.mark.parametrize("name", ["GITHUB_TOKEN", "SOME_API_KEY", "X_PRIVATE_KEY"])
def test_credential_names_are_redacted_in_unquoted_name_equals_value_form(name):
    out = guard.scrub_text("%s=loaded-after-startup-value rest" % name, {})
    assert "loaded-after-startup-value" not in out
    assert "<redacted:%s>" % name in out


def test_unquoted_form_leaves_ordinary_assignments_alone():
    text = "retries=3 mode=fast"
    assert guard.scrub_text(text, {}) == text


def _module_from_path(path: Path):
    spec = importlib.util.spec_from_file_location("_conftest_under_test_" + path.parent.name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("rel", ["conftest.py", "execution/daemons/apis/conftest.py"])
def test_conftests_import_every_hook_the_guard_provides(rel):
    mod = _module_from_path(_REPO_ROOT / rel)
    missing = [n for n in guard.HOOK_NAMES if getattr(mod, n, None) is not getattr(guard, n)]
    assert missing == []


def test_the_repo_root_conftest_really_redacts_in_a_child_run():
    """Binding check for the path CI takes: a probe test placed inside the repo
    tree is redacted by the root conftest alone."""
    with tempfile.TemporaryDirectory(dir=str(_REPO_ROOT / "lib"), prefix="_probe_guard_") as d:
        probe = Path(d) / "test_probe.py"
        probe.write_text("import os\n\ndef test_leaky():\n    assert 'nope' in dict(os.environ)\n")
        env = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": d, "TMPDIR": d, DUMMY_NAME: DUMMY_VALUE}
        done = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--tb=long", "-vv", "-q", str(probe)],
            env=env, capture_output=True, text=True, cwd=str(_REPO_ROOT), timeout=120,
        )
    out = done.stdout + done.stderr
    assert done.returncode == 1 and "1 failed" in out
    assert DUMMY_VALUE not in out
