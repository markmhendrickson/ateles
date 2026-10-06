"""Tests for the repo-root ``conftest.py`` credential scrub.

The scrub exists so an assertion that prints ``os.environ`` cannot print a
secret. These tests prove it with synthetic canary values only, in a child
pytest process: one run WITHOUT the scrub (the canaries are visible, so the
instrument has teeth) and one run WITH it (none survive).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.credential_env_names import is_credential_env_name  # noqa: E402

# Load the repo-root conftest by path: a bare ``import conftest`` would resolve
# to whichever conftest module pytest happened to register under that name.
_spec = importlib.util.spec_from_file_location(
    "ateles_root_conftest", _REPO_ROOT / "conftest.py"
)
root_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(root_conftest)

_CANARY_ENV = {
    "ZZ_CANARY_SERVICE_TOKEN": "canary-token-value-0001",
    "ZZ_CANARY_AGENT_PAT": "canary-pat-value-0002",
    "ZZ_CANARY_SIGNING_SECRET": "canary-secret-value-0003",
    "ZZ_CANARY_WALLET_MNEMONIC": "canary-mnemonic-value-0004",
    "ZZ_CANARY_DB_PASSWORD": "canary-password-value-0005",
    "ZZ_CANARY_BEARER": "canary-bearer-value-0006",
    "ZZ_CANARY_API_KEY": "canary-key-value-0007",
    "ZZ_CANARY_PRIVATE_BLOB": "canary-private-value-0008",
}
_KEPT_ENV = {
    "ZZ_CANARY_PLAIN_SETTING": "kept",
    "ZZ_CANARY_KEYS_DIR": "/not/a/secret/dir",
}

_PROBE = """
import os

# A variable set at collection time, after the process-wide scrub has run.
os.environ["ZZ_LATE_CANARY_TOKEN"] = "canary-late-value-0009"


def test_no_credential_named_variable_is_visible():
    visible = sorted(n for n in os.environ if n.startswith("ZZ_CANARY_") and (
        "TOKEN" in n or "PAT" in n or "SECRET" in n or "MNEMONIC" in n
        or "PASSWORD" in n or "BEARER" in n or "KEY" in n or "PRIVATE" in n
    ) and not n.endswith("_DIR"))
    assert visible == []


def test_repr_of_the_environment_cannot_print_a_canary_value():
    assert "canary-" not in repr(dict(os.environ))


def test_collection_time_credential_is_removed_for_each_test():
    assert "ZZ_LATE_CANARY_TOKEN" not in os.environ


def test_non_credential_names_are_left_alone():
    assert os.environ["ZZ_CANARY_PLAIN_SETTING"] == "kept"
    assert os.environ["ZZ_CANARY_KEYS_DIR"] == "/not/a/secret/dir"


def test_a_test_may_set_its_own_synthetic_credential(monkeypatch):
    monkeypatch.setenv("ZZ_OWN_TOKEN", "synthetic")
    assert os.environ["ZZ_OWN_TOKEN"] == "synthetic"
"""


def _run_probe(tmp_path: Path, *, with_scrub: bool) -> subprocess.CompletedProcess:
    probe = tmp_path / "test_probe.py"
    probe.write_text(_PROBE, encoding="utf-8")
    env = {**os.environ, **_CANARY_ENV, **_KEPT_ENV}
    # The child must not pick up the repo's own conftest through its cwd; the
    # scrub under test is loaded explicitly, or not at all.
    env["PYTHONPATH"] = str(_REPO_ROOT)
    command = [
        sys.executable,
        "-m",
        "pytest",
        str(probe),
        "-q",
        "-p",
        "no:cacheprovider",
    ]
    if with_scrub:
        command += ["-p", "conftest"]
    return subprocess.run(
        command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120
    )


def test_probe_goes_red_without_the_scrub(tmp_path):
    """The instrument has teeth: with no scrub the canaries are visible."""
    result = _run_probe(tmp_path, with_scrub=False)
    assert result.returncode != 0, result.stdout[-2000:]
    assert "test_no_credential_named_variable_is_visible" in result.stdout
    assert "test_repr_of_the_environment_cannot_print_a_canary_value" in result.stdout
    assert "test_collection_time_credential_is_removed_for_each_test" in result.stdout


def test_probe_is_green_with_the_scrub(tmp_path):
    result = _run_probe(tmp_path, with_scrub=True)
    assert result.returncode == 0, result.stdout[-2000:]
    assert "5 passed" in result.stdout


def test_failure_output_with_the_scrub_never_contains_a_canary(tmp_path):
    """Make the probe fail on purpose and read what pytest prints."""
    probe = tmp_path / "test_probe_print.py"
    probe.write_text(
        "import os\n\ndef test_print_environment():\n"
        "    assert os.environ == {}, os.environ\n",
        encoding="utf-8",
    )
    env = {**os.environ, **_CANARY_ENV, "PYTHONPATH": str(_REPO_ROOT)}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(probe),
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "conftest",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "canary-" not in combined
    unscrubbed = subprocess.run(
        [sys.executable, "-m", "pytest", str(probe), "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "canary-" in unscrubbed.stdout + unscrubbed.stderr, (
        "without the scrub the failing assertion prints the canaries, so the "
        "check above is capable of failing"
    )


def test_scrub_removes_only_credential_names_in_place():
    environ = {**_CANARY_ENV, **_KEPT_ENV, "HOME": "/h", "PATH": "/bin"}
    removed = root_conftest.scrub_credential_env(environ)
    assert set(removed) == set(_CANARY_ENV)
    assert set(environ) == {*_KEPT_ENV, "HOME", "PATH"}


@pytest.mark.parametrize(
    "name",
    [
        "EXAMPLE_SERVICE_TOKEN",
        "EXAMPLE_AGENT_PAT",
        "GITHUB_TOKEN",
        "EXAMPLE_WALLET_MNEMONIC",
        "EXAMPLE_ENCRYPTION_KEY",
        "EXAMPLE_APPROVAL_SECRET",
        "SOME_SERVICE_PASSWORD",
        "SIGNING_PRIVATE_BLOB",
        "OPENAI_API_KEY",
        "NGROK_AUTHTOKEN",
        "PAT",
    ],
)
def test_credential_names_are_recognized(name):
    assert is_credential_env_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "PATH",
        "PYTHONPATH",
        "HOME",
        "SHELL",
        "TZ",
        "EXAMPLE_BASE_URL",
        "ATELES_REPO_PATH",
        "ATELES_PRIVATE_KEYS_DIR",
        "GOOGLE_APPLICATION_CREDENTIALS_FILE",
        "PATHEXT",
        "EXAMPLE_PROVIDERS",
    ],
)
def test_non_credential_names_are_left_alone(name):
    assert not is_credential_env_name(name)


def test_no_credential_named_variable_is_visible_in_this_test_process():
    """Runs under the repo-root scrub: nothing credential-named remains."""
    assert root_conftest.credential_env_names(os.environ) == []
