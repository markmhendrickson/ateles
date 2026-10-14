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
# Declared on the shared allowlist (lib/environment_allowlist.py), so kept.
_KEPT_ENV = {
    "APIS_HARNESS_PROVIDERS": "kept",
    "ATELES_PRIVATE_KEYS_DIR": "/not/a/secret/dir",
}
# Not declared: removed even though nothing about the name looks secret.
_UNDECLARED_ENV = {
    "ZZ_CANARY_PLAIN_SETTING": "canary-undeclared-value-0010",
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


def test_declared_names_are_left_alone_and_undeclared_ones_are_removed():
    assert os.environ["APIS_HARNESS_PROVIDERS"] == "kept"
    assert os.environ["ATELES_PRIVATE_KEYS_DIR"] == "/not/a/secret/dir"
    assert "ZZ_CANARY_PLAIN_SETTING" not in os.environ


def test_a_test_may_set_its_own_synthetic_credential(monkeypatch):
    monkeypatch.setenv("ZZ_OWN_TOKEN", "synthetic")
    assert os.environ["ZZ_OWN_TOKEN"] == "synthetic"
"""


def _run_probe(tmp_path: Path, *, with_scrub: bool) -> subprocess.CompletedProcess:
    probe = tmp_path / "test_probe.py"
    probe.write_text(_PROBE, encoding="utf-8")
    env = {**os.environ, **_CANARY_ENV, **_KEPT_ENV, **_UNDECLARED_ENV}
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


_FAILING_PROBE = """
import json
import os


def test_print_environment():
    # A failure message built here, not by pytest's assertion rewriting: the
    # rewritten form truncates long reprs differently by pytest version, CI
    # setting and environment size, so a check that depended on what pytest
    # chose to print could not tell "scrub works" from "canary was elided".
    seen = {
        k: v for k, v in os.environ.items()
        if k.startswith("ZZ_") or k == "APIS_HARNESS_PROVIDERS"
    }
    raise AssertionError("SEEN=" + json.dumps(seen, sort_keys=True))
"""


def _deterministic_child_env(extra: dict, *, path_extra: str = "") -> dict:
    """A child environment that does not depend on the caller's pytest setup."""
    pythonpath = os.pathsep.join(p for p in (str(_REPO_ROOT), path_extra) if p)
    child = {**os.environ, **extra, "PYTHONPATH": pythonpath}
    for name in ("CI", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "COLUMNS", "LINES"):
        child.pop(name, None)
    child["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    child["COLUMNS"] = "400"
    return child


def _run_failing_probe(
    tmp_path: Path, *, with_scrub: bool, plugin: str | None = None
) -> str:
    probe = tmp_path / "test_probe_print.py"
    probe.write_text(_FAILING_PROBE, encoding="utf-8")
    command = [
        sys.executable, "-m", "pytest", str(probe),
        "-q", "-vv", "--tb=long", "-p", "no:cacheprovider", "-o", "addopts=",
    ]  # fmt: skip
    if with_scrub:
        command += ["-p", "conftest"]
    if plugin:
        command += ["-p", plugin]
    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=_deterministic_child_env(
            {**_CANARY_ENV, **_KEPT_ENV, **_UNDECLARED_ENV}, path_extra=str(tmp_path)
        ),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0
    return result.stdout + result.stderr


def test_failure_output_with_the_scrub_never_contains_a_canary(tmp_path):
    """Make the probe fail on purpose with a message we build, and read it."""
    scrubbed = _run_failing_probe(tmp_path, with_scrub=True)
    assert "SEEN=" in scrubbed  # the message was printed, so absence means something
    assert "canary-" not in scrubbed
    assert "APIS_HARNESS_PROVIDERS" in scrubbed  # declared names survive
    assert "ZZ_CANARY_PLAIN_SETTING" not in scrubbed  # undeclared ones do not
    control = _run_failing_probe(tmp_path, with_scrub=False)
    for value in _CANARY_ENV.values():
        assert value in control, (
            "without the scrub the failure message carries every canary, so the "
            "check above is capable of failing"
        )


def test_the_scrubbed_mapping_never_renders_a_canary_whatever_prints_it():
    """The same property with no pytest in the loop: build the message from the
    mapping and look for the canaries, scrubbed and not."""
    import json

    def message(environ):
        return json.dumps(dict(environ), sort_keys=True) + repr(environ)

    leaking = {**_CANARY_ENV, **_KEPT_ENV}
    assert all(value in message(leaking) for value in _CANARY_ENV.values())
    scrubbed = dict(leaking)
    root_conftest.scrub_credential_env(scrubbed)
    rendered = message(scrubbed)
    assert not any(value in rendered for value in _CANARY_ENV.values())
    assert not any(name in rendered for name in _CANARY_ENV)


def test_a_scrub_that_leaks_one_credential_turns_the_failure_check_red(tmp_path):
    """Red control: with a scrub that spares one credential, the very condition
    the main check asserts (no canary in the failure output) is violated."""
    (tmp_path / "leaky_scrub_plugin.py").write_text(
        "import os\n\n\ndef pytest_configure(config):\n"
        "    for name in list(os.environ):\n"
        "        if name.startswith('ZZ_CANARY_') and name != 'ZZ_CANARY_API_KEY':\n"
        "            os.environ.pop(name)\n",
        encoding="utf-8",
    )
    output = _run_failing_probe(tmp_path, with_scrub=False, plugin="leaky_scrub_plugin")
    assert "canary-" in output  # the main check's assertion would fail here
    assert _CANARY_ENV["ZZ_CANARY_API_KEY"] in output
    assert _CANARY_ENV["ZZ_CANARY_SERVICE_TOKEN"] not in output  # the others were removed


def test_scrub_keeps_only_declared_names_in_place():
    environ = {**_CANARY_ENV, **_KEPT_ENV, **_UNDECLARED_ENV, "HOME": "/h", "PATH": "/bin"}
    removed = root_conftest.scrub_credential_env(environ)
    assert set(removed) == set(_CANARY_ENV) | set(_UNDECLARED_ENV)
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


# ── Carriers of authentication are removed without being recognised ──────────
#
# The scrub is an allowlist (lib/environment_allowlist.py): anything not
# declared there is removed. Git configuration in all its forms, proxy
# settings, agent sockets and configuration-directory pointers are not
# declared, so they are removed whatever they hold, and git keeps working
# because no half of a group is left behind. Synthetic values only.

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



_SAFE_ENTRIES = [("core.quotePath", "false"), ("advice.detachedHead", "false")]

_CARRIER_ENV = {
    "GIT_CONFIG_PARAMETERS": f"'http.extraHeader'='X-Custom: {_GIT_CANARY}'",
    "GIT_CONFIG": f"/synthetic/{_GIT_CANARY}",
    "GIT_CONFIG_GLOBAL": f"/synthetic/{_GIT_CANARY}",
    "GIT_CONFIG_SYSTEM": f"/synthetic/{_GIT_CANARY}",
    "GIT_ASKPASS": f"/synthetic/{_GIT_CANARY}",
    "SSH_ASKPASS": f"/synthetic/{_GIT_CANARY}",
    "GIT_SSH": f"/synthetic/{_GIT_CANARY}",
    "GIT_SSH_COMMAND": f"ssh -i /synthetic/{_GIT_CANARY}",
    "GIT_PROXY_COMMAND": f"/synthetic/{_GIT_CANARY}",
    "GIT_SSL_CERT": f"/synthetic/{_GIT_CANARY}.pem",
    "GIT_EXEC_PATH": f"/synthetic/{_GIT_CANARY}",
    "http_proxy": f"http://user:{_GIT_CANARY}@proxy.example.test",
    "https_proxy": f"http://user:{_GIT_CANARY}@proxy.example.test",
    "HTTPS_PROXY": f"http://user:{_GIT_CANARY}@proxy.example.test",
    "ALL_PROXY": f"socks5://user:{_GIT_CANARY}@proxy.example.test",
    "all_proxy": f"socks5://user:{_GIT_CANARY}@proxy.example.test",
    "GCM_HTTP_PROXY": f"http://user:{_GIT_CANARY}@proxy.example.test",
    "SSH_AUTH_SOCK": f"/synthetic/{_GIT_CANARY}.sock",
    "KRB5CCNAME": f"FILE:/synthetic/{_GIT_CANARY}",
    "XDG_CONFIG_HOME": f"/synthetic/{_GIT_CANARY}",
    "BASH_ENV": f"/synthetic/{_GIT_CANARY}",
    "NODE_OPTIONS": f"--require /synthetic/{_GIT_CANARY}.js",
}


@pytest.mark.parametrize("name", sorted(_CARRIER_ENV))
def test_every_undeclared_carrier_is_removed(name):
    environ = {"PATH": "/bin", name: _CARRIER_ENV[name]}
    removed = root_conftest.scrub_credential_env(environ)
    assert removed == [name]
    assert environ == {"PATH": "/bin"}


@pytest.mark.parametrize("count", [0, 1, 2])
def test_an_injected_git_group_is_removed_whole_and_git_keeps_working(count):
    entries = [*_SAFE_ENTRIES[:count], ("http.extraHeader", f"X-Custom: {_GIT_CANARY}")]
    environ = _git_env(_git_group(entries))
    environ.update({k: v for k, v in _CARRIER_ENV.items() if not k.startswith("GIT_CONFIG")})
    root_conftest.scrub_credential_env(environ)
    assert _GIT_CANARY not in " ".join(environ.values())
    assert not [n for n in environ if n.startswith("GIT_CONFIG_") and n != "GIT_CONFIG_GLOBAL"]
    probe = _git(["rev-parse", "--is-inside-work-tree"], {**environ, **_git_env({})})
    assert probe.returncode == 0, probe.stderr


def test_the_partly_scrubbed_group_fails_git_so_the_check_has_teeth():
    """Control: removing only the credential-named member (the old behavior) breaks git."""
    environ = _git_env(_git_group(_SAFE_ENTRIES[:1]))
    environ.pop("GIT_CONFIG_KEY_0")
    broken = _git(["rev-parse", "--is-inside-work-tree"], environ)
    assert broken.returncode == 128
    assert "GIT_CONFIG_KEY_0" in broken.stderr


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




# ── The allowlist is declared once and kept in step with the code ───────────

from lib import environment_allowlist as allow  # noqa: E402


def test_the_allowlist_declares_no_credential_or_carrier_name():
    for name in allow.ALLOWED_ENVIRONMENT_NAMES:
        assert not is_credential_env_name(name), name
        assert not name.startswith("GIT_"), name
        assert "PROXY" not in name.upper(), name
        assert name not in {"XDG_CONFIG_HOME", "SSH_AUTH_SOCK", "BASH_ENV", "ENV", "ZDOTDIR"}


def test_every_setting_the_daemons_read_is_declared():
    """Derived from the daemons' own source: a non-credential setting they read
    that is not declared would be removed from tests and refused in plists."""
    import re

    pattern = re.compile(
        r"""(?:os\.environ\.get|os\.getenv|os\.environ\[|environ\.get|env\.get|_env\(|getenv\()"""
        r"""\s*\(?\s*["']([A-Z][A-Z0-9_]+)["']"""
    )
    listing = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "ls-files", "execution/daemons/*.py", "lib/*.py"],
        capture_output=True, text=True, check=True,
    ).stdout.split()  # fmt: skip
    read: set[str] = set()
    for path in listing:
        name = Path(path).name
        if name.startswith("test_") or name == "conftest.py":
            continue
        read.update(pattern.findall((_REPO_ROOT / path).read_text(errors="replace")))
    carriers = {n for n in read if n.startswith("GIT_") or "PROXY" in n or n == "XDG_CONFIG_HOME"}
    missing = sorted(
        n for n in read - carriers
        if not is_credential_env_name(n) and n not in allow.ALLOWED_ENVIRONMENT_NAMES
    )
    assert missing == [], f"declare these in lib/environment_allowlist.py: {missing}"


def test_the_scrub_and_the_renderer_use_the_same_list():
    import render_daemon_plist as rdp

    assert root_conftest.plan_env_scrub is allow.plan_env_scrub
    assert rdp.is_allowed_environment_name is allow.is_allowed_environment_name
