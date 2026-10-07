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


# ── Git's grouped configuration must stay valid after the scrub ----------------

_GIT_CANARY = "canary-git-config-value-0042"


def _git_env(extra: dict) -> dict:
    base = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(_REPO_ROOT),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    base.update(extra)
    return base


def _git_group(entries) -> dict:
    group = {"GIT_CONFIG_COUNT": str(len(entries))}
    for index, (key, value) in enumerate(entries):
        group[f"GIT_CONFIG_KEY_{index}"] = key
        group[f"GIT_CONFIG_VALUE_{index}"] = value
    return group


def _git(args, environ):
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        env=environ,
        cwd=str(_REPO_ROOT),
    )


_SAFE_ENTRIES = [
    ("core.quotePath", "false"),
    ("advice.detachedHead", "false"),
    ("core.hooksPath", os.devnull),
]
_SENSITIVE_ENTRIES = [
    ("http.extraHeader", f"Authorization: Bearer {_GIT_CANARY}"),
    (
        "url.https://user:" + _GIT_CANARY + "@example.test/.insteadOf",
        "https://example.test/",
    ),
    ("credential.helper", f"!f() {{ echo password={_GIT_CANARY}; }}; f"),
    ("example.apiToken", "plain"),
    ("example.setting", _GIT_CANARY * 2),
]


@pytest.mark.parametrize("count", [0, 1, 3])
def test_scrub_preserves_git_config_validity(count):
    """Git still runs after the scrub for zero, one and several injected entries."""
    entries = _SAFE_ENTRIES[:count]
    environ = _git_env(_git_group(entries) if entries else {})
    root_conftest.scrub_credential_env(environ)

    probe = _git(["rev-parse", "--is-inside-work-tree"], environ)
    assert probe.returncode == 0, probe.stderr
    for key, value in entries:
        got = _git(["config", "--get", key], environ)
        assert got.returncode == 0 and got.stdout.strip() == value, key


def test_the_partly_scrubbed_group_fails_git_so_the_check_has_teeth():
    """Control: removing only the credential-named member (the old behavior) breaks git."""
    environ = _git_env(_git_group(_SAFE_ENTRIES[:1]))
    environ.pop("GIT_CONFIG_KEY_0")
    broken = _git(["rev-parse", "--is-inside-work-tree"], environ)
    assert broken.returncode == 128
    assert "GIT_CONFIG_KEY_0" in broken.stderr


@pytest.mark.parametrize("position", ["first", "middle", "last", "only"])
def test_scrub_removes_sensitive_git_config_values_and_keeps_git_working(position):
    safe = _SAFE_ENTRIES[:2]
    sensitive = _SENSITIVE_ENTRIES[0]
    entries = {
        "first": [sensitive, *safe],
        "middle": [safe[0], sensitive, safe[1]],
        "last": [*safe, sensitive],
        "only": [sensitive],
    }[position]
    environ = _git_env(_git_group(entries))
    root_conftest.scrub_credential_env(environ)

    assert _GIT_CANARY not in " ".join(environ.values())
    probe = _git(["rev-parse", "--is-inside-work-tree"], environ)
    assert probe.returncode == 0, probe.stderr
    seen = _git(["config", "--list"], environ)
    assert _GIT_CANARY not in seen.stdout
    if position != "only":
        for key, value in safe:
            got = _git(["config", "--get", key], environ)
            assert got.returncode == 0 and got.stdout.strip() == value, key


@pytest.mark.parametrize("entry", _SENSITIVE_ENTRIES, ids=lambda e: e[0][:24])
def test_every_kind_of_sensitive_git_config_entry_is_removed(entry):
    environ = _git_env(_git_group([_SAFE_ENTRIES[0], entry]))
    root_conftest.scrub_credential_env(environ)
    assert _GIT_CANARY not in " ".join(environ.values())
    assert environ["GIT_CONFIG_COUNT"] == "1"
    assert _git(["rev-parse", "--is-inside-work-tree"], environ).returncode == 0


def test_scrub_drops_the_whole_group_when_nothing_safe_remains():
    environ = _git_env(_git_group([_SENSITIVE_ENTRIES[0]]))
    root_conftest.scrub_credential_env(environ)
    assert "GIT_CONFIG_COUNT" not in environ
    assert not [n for n in environ if n.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))]
    assert _git(["rev-parse", "--is-inside-work-tree"], environ).returncode == 0


@pytest.mark.parametrize("bad", ["", "abc", "-1"])
def test_scrub_handles_an_unusable_git_config_count(bad):
    environ = _git_env(
        {"GIT_CONFIG_COUNT": bad, "GIT_CONFIG_KEY_0": "a.b", "GIT_CONFIG_VALUE_0": "c"}
    )
    root_conftest.scrub_credential_env(environ)
    assert _git(["rev-parse", "--is-inside-work-tree"], environ).returncode == 0


def test_scrub_drops_credential_bearing_git_config_parameters_only():
    bearing = _git_env(
        {"GIT_CONFIG_PARAMETERS": f"'http.extraheader'='Authorization: Bearer {_GIT_CANARY}'"}
    )
    root_conftest.scrub_credential_env(bearing)
    assert "GIT_CONFIG_PARAMETERS" not in bearing
    benign = _git_env({"GIT_CONFIG_PARAMETERS": "'core.quotepath'='false'"})
    root_conftest.scrub_credential_env(benign)
    assert benign["GIT_CONFIG_PARAMETERS"] == "'core.quotepath'='false'"


def test_group_names_are_not_judged_one_by_one():
    for name in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_12"):
        assert not is_credential_env_name(name)
    assert is_credential_env_name("GIT_CONFIG_KEYS_SECRET")


def test_the_autouse_fixture_plan_keeps_a_valid_group_in_the_live_process(monkeypatch):
    """The per-test fixture applies plan_env_scrub through monkeypatch; exercise it live."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.quotePath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "http.extraHeader")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", f"Authorization: Bearer {_GIT_CANARY}")
    remove, updates = root_conftest.plan_env_scrub(os.environ)
    for name in remove:
        monkeypatch.delenv(name, raising=False)
    for name, value in updates.items():
        monkeypatch.setenv(name, value)
    assert _GIT_CANARY not in " ".join(os.environ.values())
    assert _git(["rev-parse", "--is-inside-work-tree"], dict(os.environ)).returncode == 0


def test_collection_that_runs_git_survives_injected_git_config():
    """The plist suite runs `git ls-files` at collection; an injected grouped
    configuration must not stop it (the scrub runs before collection)."""
    child_env = dict(os.environ)
    child_env.update(_git_group([("core.quotePath", "false")]))
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
            "execution/scripts/test_render_daemon_plist.py",
        ],
        capture_output=True,
        text=True,
        env=child_env,
        cwd=str(_REPO_ROOT),
    )
    assert result.returncode == 0, result.stdout[-400:] + result.stderr[-400:]
