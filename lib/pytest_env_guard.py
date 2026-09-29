"""Keep the host environment out of pytest output.

Incident, 2026-09-29: a review agent ran a test that captures a child-process
environment. The test failed, pytest's assertion introspection printed the
captured environment (a copy of the host's), and a real host secret landed in
an agent transcript. Two defects combined:

1. The tests were not hermetic: the child env was built from the host's, so
   host variables changed outcomes (and got captured).
2. Nothing stopped a failure message from carrying an environment value.

This module fixes (2) for every suite that loads it, and offers the building
blocks for (1).

What is scrubbed, and where (see ``HOOK_NAMES``):

- ``pytest_runtest_makereport``: setup/call/teardown reports (assertion
  explanation, source lines, locals under ``-l``, chained exceptions, captured
  stdout/stderr/log sections).
- ``pytest_make_collect_report``: collection errors (a module-level assertion
  or failed import that touches the environment).
- ``pytest_warning_recorded``: the warnings summary.
- ``pytest_configure``: installs a log-record factory so every log record's
  message (and pre-formatted exception text) is scrubbed before any handler,
  including live logging (``--log-cli-level``) and ``caplog``, sees it.

What is redacted: every value that was in the process environment at import
time and is longer than ``MIN_SECRET_LEN``, in its raw form AND in its escaped
forms (``repr``, JSON, ``unicode_escape``, each applied up to twice so a repr
inside a repr is caught), and, for a multi-line value (PEM, JWK), each line
longer than ``MIN_SECRET_LEN``. Matches become ``<redacted:NAME>``. A value
loaded after startup is caught by key name when it appears as
``'SOME_TOKEN': 'value'`` or ``SOME_TOKEN=value`` and the name looks like a
credential. If the scrubber errors, meets an object it cannot walk, or is
nested deeper than ``MAX_DEPTH``, the whole body is withheld rather than
printed: fail closed on the field that carries the safety meaning.

Not covered (stated so the documented coverage matches the behaviour): output
a test or subprocess writes straight to the terminal with ``-s``;
``INTERNALERROR`` tracebacks; other encodings of a value (base64, URL
encoding, a hash); values of ``MIN_SECRET_LEN`` characters or fewer; values
read from a file during a test and not held under a credential-looking name.

Also here: ``hermetic_env`` / ``clear_host_env`` (start a test from a synthetic
environment), ``assert_env_keys_absent`` / ``assert_env_keys_present`` (compare
key NAMES only, never render the mapping) and ``changed_during_collection``.

Why output redaction rather than "fail any test whose child env contains a
host value": a check on one captured dict covers only tests that remember to
capture it that way, and the check itself must not print the offender. The
redaction sits on the channels every leak passes through, so it also covers
locals, ``-l``, captured output, and tests nobody has written yet.

Wire it by importing every name in ``HOOK_NAMES`` into a ``conftest.py``
(``lib/test_pytest_env_guard.py`` asserts the repo's conftests do)::

    from lib.pytest_env_guard import (  # noqa: F401
        pytest_collection_finish,
        pytest_configure,
        pytest_make_collect_report,
        pytest_runtest_makereport,
        pytest_warning_recorded,
    )

Names starting ``pytest_`` imported into a conftest namespace register as
hooks. Importing the same hook from several conftests is harmless: scrubbing
is idempotent.
"""

from __future__ import annotations

import json
import logging
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

_CRED_TOKENS = frozenset(
    {
        "TOKEN",
        "TOKENS",
        "SECRET",
        "SECRETS",
        "PASSWORD",
        "PASSWD",
        "PASSPHRASE",
        "JWK",
        "MNEMONIC",
        "CREDENTIAL",
        "CREDENTIALS",
        "APIKEY",
        "BEARER",
        "PAT",
        "AUTHORIZATION",
        "COOKIE",
    }
)
_CRED_PAIRS = frozenset(
    {
        ("API", "KEY"),
        ("PRIVATE", "KEY"),
        ("ACCESS", "KEY"),
        ("SECRET", "KEY"),
        ("AUTH", "KEY"),
    }
)


def looks_like_credential_name(name: str) -> bool:
    """True for names such as GITHUB_TOKEN, apiKey, client-secret, X_PRIVATE_KEY.

    Matches whole words, not substrings, so ``author``, ``oauth_state``,
    ``private_repo`` and ``keyword`` are not credentials."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    toks = [t for t in re.split(r"[^A-Za-z0-9]+", spaced.upper()) if t]
    if any(t in _CRED_TOKENS for t in toks):
        return True
    return any(pair in _CRED_PAIRS for pair in zip(toks, toks[1:]))


# 'SOME_KEY': 'value'  /  "SOME_KEY": "value"
_PAIR = re.compile(
    r"""(?P<k>(['"])(?P<name>[A-Za-z0-9_.\-]+)\2\s*:\s*)(?P<q>['"])(?P<v>.*?)(?P=q)"""
)
# SOME_KEY=value  /  SOME_KEY='value'  (dotenv or ``env`` shape)
_ASSIGN = re.compile(
    r"""(?<![\w.\-])(?P<name>[A-Za-z][A-Za-z0-9_.\-]*)=(?P<q>['"]?)(?P<v>[^\s'",;)\]}]+)(?P=q)"""
)


def _escaped_forms(value: str) -> set:
    """Renderings of ``value`` a failure message can carry: repr body, JSON
    bodies, unicode_escape (with and without escaped single quotes)."""
    forms = {
        repr(value)[1:-1],
        json.dumps(value)[1:-1],
        json.dumps(value, ensure_ascii=False)[1:-1],
        value.encode("unicode_escape").decode("ascii"),
    }
    forms.add(value.encode("unicode_escape").decode("ascii").replace("'", "\\'"))
    return forms


def _variants(value: str) -> set:
    """``value`` plus its escaped forms, each escaped once more (a repr inside
    a repr, JSON inside a repr), plus each long line of a multi-line value."""
    seeds = {value}
    for line in value.splitlines():
        line = line.strip()
        if len(line) > MIN_SECRET_LEN:
            seeds.add(line)
    out = set(seeds)
    for seed in seeds:
        first = _escaped_forms(seed)
        out |= first
        for f in first:
            out |= _escaped_forms(f)
    return {v for v in out if len(v) > MIN_SECRET_LEN}


def snapshot_secret_values(environ: Mapping[str, str]) -> dict:
    """Map every redactable rendering -> variable name.

    Includes the raw value, its escaped forms and, for a multi-line value, each
    long line (see ``_variants``)."""
    out: dict = {}
    for name, value in environ.items():
        if name in SAFE_HOST_NAMES or name.startswith(("PYTEST_", "LC_")):
            continue
        if isinstance(value, str) and len(value) > MIN_SECRET_LEN:
            for form in _variants(value):
                out.setdefault(form, name)
    return out


# Taken at import (conftest load), before any test can monkeypatch the env.
_HOST_SECRETS: dict = snapshot_secret_values(os.environ)
# Longest first so a rendering that contains another is replaced whole.
_HOST_SECRETS_ORDER: tuple = tuple(sorted(_HOST_SECRETS, key=len, reverse=True))

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
    """Redact host env renderings and credential-named pairs from ``text``."""
    if secrets is None or secrets is _HOST_SECRETS:
        secrets, order = _HOST_SECRETS, _HOST_SECRETS_ORDER
    else:
        order = sorted(secrets, key=len, reverse=True)
    for value in order:
        if value in text:
            text = text.replace(value, "<redacted:%s>" % secrets[value])

    def _pair(m: "re.Match[str]") -> str:
        if looks_like_credential_name(m.group("name")):
            return "%s%s<redacted:%s>%s" % (
                m.group("k"),
                m.group("q"),
                m.group("name"),
                m.group("q"),
            )
        return m.group(0)

    def _assign(m: "re.Match[str]") -> str:
        if looks_like_credential_name(m.group("name")) and "<redacted:" not in m.group(
            "v"
        ):
            return "%s=%s<redacted:%s>%s" % (
                m.group("name"),
                m.group("q"),
                m.group("name"),
                m.group("q"),
            )
        return m.group(0)

    return _ASSIGN.sub(_assign, _PAIR.sub(_pair, text))


class Unscrubbable(Exception):
    """A report contained something the scrubber cannot walk; withhold it."""


def _scrub_obj(obj: Any, secrets: Mapping[str, str], depth: int = 0) -> Any:
    """Scrub strings inside a pytest report body, returning the cleaned object.

    Fails closed: anything it cannot positively walk (deeper than MAX_DEPTH, or
    an object type that is not pytest's own) raises ``Unscrubbable``."""
    if depth > MAX_DEPTH:
        raise Unscrubbable("nesting deeper than %d" % MAX_DEPTH)
    if isinstance(obj, str):
        return scrub_text(obj, secrets)
    if isinstance(obj, list):
        return [_scrub_obj(x, secrets, depth + 1) for x in obj]
    if isinstance(obj, tuple):
        return tuple(_scrub_obj(x, secrets, depth + 1) for x in obj)
    if isinstance(obj, dict):
        return {
            _scrub_obj(k, secrets, depth + 1): _scrub_obj(v, secrets, depth + 1)
            for k, v in obj.items()
        }
    if isinstance(obj, (int, float, bool, bytes)) or obj is None:
        return obj
    attrs = getattr(obj, "__dict__", None)
    if attrs is not None and type(obj).__module__.startswith("_pytest"):
        for k, v in list(attrs.items()):
            object.__setattr__(obj, k, _scrub_obj(v, secrets, depth + 1))
        return obj
    raise Unscrubbable("object of type %s" % type(obj).__name__)


def scrub_report(report: Any, secrets: Mapping[str, str] | None = None) -> None:
    """In-place scrub of a TestReport or CollectReport. On any error, or on an
    object it cannot walk, withhold the body (the exception TYPE is named, never
    a value)."""
    secrets = _HOST_SECRETS if secrets is None else secrets
    try:
        if getattr(report, "longrepr", None) is not None:
            report.longrepr = _scrub_obj(report.longrepr, secrets)
        if getattr(report, "sections", None):
            report.sections = _scrub_obj(list(report.sections), secrets)
    except Exception as exc:  # noqa: BLE001 - fail closed, never print unscrubbed output
        report.longrepr = (
            "<failure output withheld: env-redaction guard errored (%s)>"
            % type(exc).__name__
        )
        report.sections = []


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    scrub_report(report)
    return report


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector):
    """Collection errors (import-time failures) carry their own report."""
    report = yield
    scrub_report(report)
    return report


@pytest.hookimpl(tryfirst=True)
def pytest_warning_recorded(warning_message, when, nodeid, location):
    """Scrub the warning before the terminal reporter formats it."""
    try:
        text = str(warning_message.message)
        scrubbed = scrub_text(text)
        if scrubbed != text:
            warning_message.message = UserWarning(scrubbed)
        line = getattr(warning_message, "line", None)
        if isinstance(line, str):
            warning_message.line = scrub_text(line)
    except Exception:  # noqa: BLE001 - fail closed
        warning_message.message = UserWarning(
            "<warning text withheld: env-redaction guard errored>"
        )


_LOG_FACTORY_INSTALLED = False


def pytest_configure(config):
    """Scrub every log record's message before any handler (live logging,
    caplog, captured-log sections) can render it. Idempotent."""
    global _LOG_FACTORY_INSTALLED
    if _LOG_FACTORY_INSTALLED:
        return
    _LOG_FACTORY_INSTALLED = True
    previous = logging.getLogRecordFactory()
    plain = logging.Formatter()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        try:
            message = record.getMessage()
            scrubbed = scrub_text(message)
            if scrubbed != message:
                record.msg, record.args = scrubbed, None
            if record.exc_info and not record.exc_text:
                record.exc_text = scrub_text(plain.formatException(record.exc_info))
            if isinstance(record.stack_info, str):
                record.stack_info = scrub_text(record.stack_info)
        except Exception:  # noqa: BLE001 - fail closed
            record.msg, record.args = (
                "<log record withheld: env-redaction guard errored>",
                None,
            )
            record.exc_info = record.exc_text = record.stack_info = None
        return record

    logging.setLogRecordFactory(factory)


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
HOOK_NAMES = (
    "pytest_collection_finish",
    "pytest_configure",
    "pytest_make_collect_report",
    "pytest_runtest_makereport",
    "pytest_warning_recorded",
)
