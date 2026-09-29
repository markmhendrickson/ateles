"""Keep the host environment out of pytest failure output.

Incident, 2026-09-29: a review agent ran a test that captures a child-process
environment. The test failed, pytest's assertion introspection printed the
captured environment (a copy of the host's), and a real host secret landed in
an agent transcript. Two defects combined:

1. The tests were not hermetic: the child env was built from the host's, so
   host variables changed outcomes (and got captured).
2. Nothing stopped a failure message from carrying an environment value.

This module fixes (2) for every suite that loads it, and offers the building
blocks for (1):

- ``pytest_runtest_makereport`` (hookwrapper): after the report is built,
  every string in it (assertion explanation, source lines, locals, chained
  exceptions, captured stdout/stderr/log sections) is scrubbed. A value that
  was in the process environment at import time and is longer than
  ``MIN_SECRET_LEN`` becomes ``<redacted:NAME>``; a dict-shaped
  ``'SOME_TOKEN': 'value'`` pair whose key looks credential-like is redacted
  by key name as well, to cover values the process loaded after startup. If
  the scrubber itself errors, the whole failure body is withheld rather than
  printed: fail closed on the field that carries the safety meaning.
- ``hermetic_env``: a fixture factory that removes every host variable except
  a small non-secret allowlist, for tests that build or inspect a child env.

Why output redaction rather than "fail any test whose child env contains a
host value": a check on one captured dict covers only tests that remember to
capture it that way, and the check itself must not print the offender. The
redaction hook sits on the one channel every leak passes through (the report),
so it also covers locals, ``-l``, captured output, and tests nobody has
written yet.

Wire it by importing the hook names into a ``conftest.py``::

    from lib.pytest_env_guard import (  # noqa: F401
        pytest_runtest_makereport,
    )

Names starting ``pytest_`` imported into a conftest namespace register as
hooks. Importing the same hook from several conftests is harmless: scrubbing
is idempotent.
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable, Mapping

import pytest

MIN_SECRET_LEN = 12
MAX_DEPTH = 12

# Host variables whose values are never credentials. Kept out of the redaction
# set so ordinary paths in a failure message stay readable, and kept in the
# hermetic allowlist because tools genuinely need them.
SAFE_HOST_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "PWD",
        "OLDPWD",
        "SHELL",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TERM_PROGRAM",
        "TERM_PROGRAM_VERSION",
        "TERM_SESSION_ID",
        "USER",
        "LOGNAME",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "SHLVL",
        "XPC_SERVICE_NAME",
        "COLORTERM",
        "SSH_AUTH_SOCK",
        "PYTEST_CURRENT_TEST",
        "PYTEST_VERSION",
    }
)

# Hermetic tests keep these (plus anything starting PYTEST_/LC_) and drop the rest.
HERMETIC_KEEP = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "TERM",
        "USER",
        "LOGNAME",
        "SHELL",
        "VIRTUAL_ENV",
    }
)

_CREDENTIAL_KEY = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE|JWK|MNEMONIC|CREDENTIAL|API_?KEY|BEARER|AUTH)",
    re.IGNORECASE,
)
# 'SOME_KEY': 'value'  /  "SOME_KEY": "value"  /  SOME_KEY=value inside a repr.
_PAIR = re.compile(
    r"""(?P<k>(['"])(?P<name>[A-Za-z0-9_.\-]+)\2\s*:\s*)(?P<q>['"])(?P<v>.*?)(?P=q)"""
)


def snapshot_secret_values(environ: Mapping[str, str]) -> dict:
    """Map value -> variable name for every env value that is worth redacting."""
    out: dict = {}
    for name, value in environ.items():
        if name in SAFE_HOST_NAMES or name.startswith(("PYTEST_", "LC_")):
            continue
        if isinstance(value, str) and len(value) > MIN_SECRET_LEN:
            out[value] = name
    return out


# Taken at import (conftest load), before any test can monkeypatch the env.
_HOST_SECRETS: dict = snapshot_secret_values(os.environ)

# Private full snapshot, used only to answer "did importing test modules
# change the process environment?" (see ``changed_during_collection``). Never
# exposed, never rendered.
_STARTUP_ENVIRON: dict = dict(os.environ)

# NAMES whose value changed between conftest load and the end of collection,
# i.e. changes caused by importing the code under test. Set by
# ``pytest_collection_finish``; safe to render (names only).
ENV_CHANGED_DURING_COLLECTION: frozenset = frozenset()


def changed_during_collection(name: str) -> bool:
    """True if importing the code under test added, removed or altered ``name``
    in the process environment. This is what "did the module-level dotenv
    bootstrap run under pytest?" detectors need: a host that already exports
    the variable is not a leak, so the comparison is against startup rather
    than against "absent".

    Hermetic per-test fixtures clear the environment at test time, which is
    after collection, so they cannot mask an import-time leak: this answer is
    fixed before the first test runs.
    """
    return name in ENV_CHANGED_DURING_COLLECTION


def pytest_collection_finish(session):
    global ENV_CHANGED_DURING_COLLECTION
    names = set(_STARTUP_ENVIRON) | set(os.environ)
    ENV_CHANGED_DURING_COLLECTION = frozenset(
        n for n in names if _STARTUP_ENVIRON.get(n) != os.environ.get(n)
    )


def scrub_text(text: str, secrets: Mapping[str, str] | None = None) -> str:
    """Redact host env values and credential-keyed dict entries from ``text``."""
    secrets = _HOST_SECRETS if secrets is None else secrets
    # Longest first so a value that contains another is replaced whole.
    for value in sorted(secrets, key=len, reverse=True):
        if value in text:
            text = text.replace(value, "<redacted:%s>" % secrets[value])

    def _pair(m: "re.Match[str]") -> str:
        if _CREDENTIAL_KEY.search(m.group("name")):
            return "%s%s<redacted:%s>%s" % (
                m.group("k"),
                m.group("q"),
                m.group("name"),
                m.group("q"),
            )
        return m.group(0)

    return _PAIR.sub(_pair, text)


def _scrub_obj(obj: Any, secrets: Mapping[str, str], depth: int = 0) -> Any:
    """Scrub strings inside a pytest report body, returning the cleaned object."""
    if depth > MAX_DEPTH:
        return obj
    if isinstance(obj, str):
        return scrub_text(obj, secrets)
    if isinstance(obj, list):
        return [_scrub_obj(x, secrets, depth + 1) for x in obj]
    if isinstance(obj, tuple):
        return tuple(_scrub_obj(x, secrets, depth + 1) for x in obj)
    if isinstance(obj, (int, float, bool, bytes)) or obj is None:
        return obj
    attrs = getattr(obj, "__dict__", None)
    if attrs is not None and type(obj).__module__.startswith("_pytest"):
        for k, v in list(attrs.items()):
            object.__setattr__(obj, k, _scrub_obj(v, secrets, depth + 1))
        return obj
    return obj


def scrub_report(report: Any, secrets: Mapping[str, str] | None = None) -> None:
    """In-place scrub of a TestReport. On any error, withhold the body."""
    secrets = _HOST_SECRETS if secrets is None else secrets
    try:
        if getattr(report, "longrepr", None) is not None:
            report.longrepr = _scrub_obj(report.longrepr, secrets)
        if getattr(report, "sections", None):
            report.sections = _scrub_obj(list(report.sections), secrets)
    except Exception:  # noqa: BLE001 - fail closed, never print unscrubbed output
        report.longrepr = "<failure output withheld: env-redaction guard errored>"
        report.sections = []


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    scrub_report(report)
    return report


def env_keys_present(env: Mapping[str, Any], *names: str) -> list:
    """Sorted subset of ``names`` that are keys of ``env`` (NAMES only)."""
    return sorted(n for n in names if n in (env or {}))


def assert_env_keys_absent(env: Mapping[str, Any], *names: str, why: str = "") -> None:
    """Assert none of ``names`` is a key of ``env`` without ever rendering
    ``env``: a bare ``assert "X" not in env`` makes pytest print the whole
    mapping, which for a child-process env is the host's environment."""
    found = env_keys_present(env, *names)
    if found:
        raise AssertionError(
            "env keys must be absent but are present: %s%s"
            % (found, (" (%s)" % why) if why else "")
        )


def assert_env_keys_present(env: Mapping[str, Any], *names: str, why: str = "") -> None:
    """Counterpart of ``assert_env_keys_absent``; reports missing NAMES only."""
    missing = sorted(n for n in names if n not in (env or {}))
    if missing:
        raise AssertionError(
            "env keys must be present but are missing: %s%s"
            % (missing, (" (%s)" % why) if why else "")
        )


def hermetic_environ(
    environ: Mapping[str, str], keep: Iterable[str] = HERMETIC_KEEP
) -> list:
    """Names in ``environ`` that a hermetic test must remove (NAMES only)."""
    keep = set(keep)
    return [
        name
        for name in environ
        if name not in keep and not name.startswith(("PYTEST_", "LC_"))
    ]


def clear_host_env(monkeypatch: "pytest.MonkeyPatch") -> None:
    """Remove every non-allowlisted host variable for the duration of a test."""
    for name in hermetic_environ(os.environ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def hermetic_env(monkeypatch):
    """Start the test from a synthetic environment: host vars cannot change
    the outcome, and cannot be captured by a child-process stub."""
    clear_host_env(monkeypatch)
    return monkeypatch


# Every hook this module provides. Conftests import exactly these names; a test
# asserts they do, so a hook added here cannot silently go unwired.
HOOK_NAMES = ("pytest_runtest_makereport", "pytest_collection_finish")
