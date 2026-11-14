"""Bounded, read-only status of the configured Fly login machines.

    ATELES_SKIP_DOTENV=1 .venv/bin/python execution/daemons/apis/fly_machine_status.py \
        --provider all

Prints exactly one closed JSON envelope (at most 4 KiB) and nothing else:

    {"schema_version": 1, "complete": bool,
     "machines": [{"provider", "machine_state", "run_marker_present",
                   "job_config_present"}],
     "errors": [{"provider", "code", "http_status"}]}

It reads ``APIS_FLY_APP``, ``APIS_FLY_API_TOKEN`` and the per-provider
``APIS_FLY_<PROVIDER>_MACHINE_ID`` bindings from the process environment
through ``FlyConfig.from_env`` and loads no env or key file. Every requested
binding is checked before any request: if one is missing, malformed or shared
with another provider, no machine is read. Otherwise it makes at most one
``GET /machines/<id>`` per requested provider through the existing
``FlyMachinesClient``. The whole run, including cancelling unfinished reads and
closing the client, finishes inside one 10-second deadline: no retries, no
lease, no start/stop/update/destroy, no executor, ledger or Neotoma.

Nothing read from the API, the environment or the command line is echoed:
not job files, env, metadata, names, machine or run ids, versions, error
text, URLs or argument values. ``complete`` means every requested machine was
read (an explicit 404 reads as ``absent``); it does not mean a machine is
free, clean or released, and nothing here authorizes a release. Exit 0 only
when complete; otherwise exit 2.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import sys
import time
from typing import Any

import fly_runner
import httpx

SCHEMA_VERSION = 1
DEADLINE_SECONDS = 10.0
# Held back from the reads, inside the deadline, for cancelling unfinished
# reads and letting their clients close (capped at a quarter of the deadline).
CLEANUP_SECONDS = 0.5
MAX_OUTPUT_BYTES = 4096
PROVIDERS: tuple[str, ...] = ("claude", "codex", "cursor")
EXIT_COMPLETE = 0
EXIT_INCOMPLETE = 2

# Fixed error codes. Nothing else ever appears in ``code``.
MISSING_CONFIGURATION = "missing_configuration"
INVALID_CONFIGURATION = "invalid_configuration"
MISSING_BINDING = "missing_binding"
INVALID_BINDING = "invalid_binding"
DUPLICATE_BINDING = "duplicate_binding"
REQUEST_FAILED = "request_failed"
MALFORMED_RESPONSE = "malformed_response"
UNRECOGNIZED_STATE = "unrecognized_state"
TIMED_OUT = "timed_out"
INVALID_ARGUMENTS = "invalid_arguments"
INTERRUPTED = "interrupted"
UNEXPECTED_ERROR = "unexpected_error"
ERROR_CODES: frozenset[str] = frozenset({
    MISSING_CONFIGURATION, INVALID_CONFIGURATION, MISSING_BINDING, INVALID_BINDING,
    DUPLICATE_BINDING, REQUEST_FAILED, MALFORMED_RESPONSE, UNRECOGNIZED_STATE, TIMED_OUT,
    INVALID_ARGUMENTS, INTERRUPTED, UNEXPECTED_ERROR,
})  # fmt: skip

USAGE = "usage: fly_machine_status.py [--provider {all,claude,codex,cursor}]\n"
HELP = USAGE + (
    "\nBounded read-only status of the configured Fly login machines.\n"
    "Prints one fixed JSON envelope: per provider the machine state and whether\n"
    "a run marker or job config is present; no ids, names, files or values.\n"
    "With no option, --provider defaults to all.\n"
    "Reads APIS_FLY_APP, APIS_FLY_API_TOKEN and APIS_FLY_<PROVIDER>_MACHINE_ID\n"
    "from the environment; see docs/cloud_hosting.md.\n"
    "Exit 0 only when every requested machine was read; otherwise 2.\n"
    "A complete status does not mean a machine is free or may be released.\n"
)
INVALID_ARGUMENTS_TEXT = "fly_machine_status: invalid arguments\n" + USAGE

# Fly app names and machine ids as the Machines API issues them. Anything
# else would change the request path, so it is refused before any call.
_APP_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}", re.ASCII)
_MACHINE_ID_RE = re.compile(r"[A-Za-z0-9]{1,64}", re.ASCII)
# A deploy token is either one printable no-space token (``fo1_...``,
# ``fm2_...``) or a Fly macaroon, which is one scheme word, a single space and
# a no-space token (``FlyV1 fm2_a,fm2_b``; commas are inside the token).
# fly_runner sends the value as ``Authorization: Bearer <token>``, so nothing
# else may pass: no CR, LF, tab or other control character, no leading or
# trailing space and no second space (header injection). This is stricter than
# "any printable ASCII including space" and still accepts both real forms.
_TOKEN_RE = re.compile(r"(?:[A-Za-z][A-Za-z0-9]{0,15} )?[\x21-\x7e]+", re.ASCII)
# Loggers that could carry a request URL, header or response text. Each is
# silenced together with every child logger (httpcore traces through
# ``httpcore.http11``, ``httpcore.connection`` and so on, and a parent's
# ``disabled`` flag does not stop a child's records from propagating).
_QUIET_LOGGERS = ("httpx", "httpcore", "h2", "hpack", "asyncio", "apis.fly_runner")
_KNOWN_CHILD_LOGGERS = (
    "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy",
    "httpcore.socks",
)  # fmt: skip


class _Rejected(Exception):
    """A configuration refusal carrying only a fixed code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def parse_args(argv: list[str]) -> tuple[str, tuple[str, ...]] | None:
    """``("help", ())``, ``("status", providers)``, or None when invalid.

    Hand-written on purpose: argparse echoes unknown arguments and values in
    its diagnostics (and accepts abbreviations), which could print a
    credential pasted onto the command line.
    """
    if argv in (["--help"], ["-h"]):
        return "help", ()
    if not argv:
        choice = "all"
    elif len(argv) == 2 and argv[0] == "--provider":
        choice = argv[1]
    elif len(argv) == 1 and argv[0].startswith("--provider="):
        choice = argv[0][len("--provider=") :]
    else:
        return None
    if choice == "all":
        return "status", PROVIDERS
    for provider in PROVIDERS:
        if choice == provider:
            return "status", (provider,)
    return None


def _error(provider: str | None, code: str, http_status: int | None = None) -> dict:
    if code not in ERROR_CODES:
        raise ValueError("error code outside the fixed vocabulary")
    literal = None if provider is None else fly_runner._STATUS_PROVIDERS[provider]
    status = None
    if (isinstance(http_status, int) and not isinstance(http_status, bool)
            and 100 <= http_status <= 599):  # fmt: skip
        status = int(http_status)
    return {"provider": literal, "code": code, "http_status": status}


def _unknown(provider: str) -> dict:
    return fly_runner.status_row(provider, "unknown", None, None)


def _envelope(machines: list[dict], errors: list[dict]) -> dict:
    complete = (
        not errors
        and len(machines) > 0
        and all(m["machine_state"] != "unknown" for m in machines)
    )
    return {"schema_version": SCHEMA_VERSION, "complete": complete,
            "machines": machines[:len(PROVIDERS)], "errors": errors[:len(PROVIDERS)]}  # fmt: skip


_ROW_KEYS = ("provider", "machine_state", "run_marker_present", "job_config_present")
_ERROR_KEYS = ("provider", "code", "http_status")
_STATES = fly_runner.MACHINE_STATES | {"absent", "unknown"}


def _check_envelope(envelope: Any) -> None:
    """Refuse anything outside the closed envelope: exact key sets and value
    domains, so a regression upstream cannot pass an API string through."""

    def flag(value: Any) -> bool:
        return value is None or isinstance(value, bool)

    ok = (
        type(envelope) is dict
        and tuple(envelope) == ("schema_version", "complete", "machines", "errors")
        and envelope["schema_version"] == SCHEMA_VERSION
        and isinstance(envelope["complete"], bool)
        and type(envelope["machines"]) is list
        and len(envelope["machines"]) <= len(PROVIDERS)
        and type(envelope["errors"]) is list
        and len(envelope["errors"]) <= len(PROVIDERS)
    )
    if ok:
        for row in envelope["machines"]:
            ok = ok and (
                type(row) is dict
                and tuple(row) == _ROW_KEYS
                and row["provider"] in PROVIDERS
                and row["machine_state"] in _STATES
                and flag(row["run_marker_present"])
                and flag(row["job_config_present"])
            )
        for err in envelope["errors"]:
            status = err.get("http_status") if type(err) is dict else None
            ok = ok and (
                type(err) is dict
                and tuple(err) == _ERROR_KEYS
                and (err["provider"] is None or err["provider"] in PROVIDERS)
                and err["code"] in ERROR_CODES
                and (status is None or (type(status) is int and 100 <= status <= 599))
            )
    if not ok:
        raise ValueError("status envelope outside its closed shape")


def _encode(envelope: dict) -> str:
    _check_envelope(envelope)
    text = json.dumps(envelope, ensure_ascii=True, separators=(",", ":"))
    if len(text.encode("ascii")) + 1 > MAX_OUTPUT_BYTES:
        raise ValueError("status envelope exceeds its bound")
    return text


FALLBACK_OUTPUT = _encode(_envelope([], [_error(None, UNEXPECTED_ERROR)]))
INTERRUPTED_OUTPUT = _encode(_envelope([], [_error(None, INTERRUPTED)]))


def _load_config() -> fly_runner.FlyConfig:
    """The configured client settings, or ``_Rejected`` with a fixed code.
    Only the official Machines API base is accepted, so the deploy token is
    never sent to a different destination."""
    config = fly_runner.FlyConfig.from_env()
    if not config.app or not config.api_token:
        raise _Rejected(MISSING_CONFIGURATION)
    if config.api_base != fly_runner.FLY_API_BASE_DEFAULT:
        raise _Rejected(INVALID_CONFIGURATION)
    if not _APP_RE.fullmatch(config.app) or not _TOKEN_RE.fullmatch(config.api_token):
        raise _Rejected(INVALID_CONFIGURATION)
    return config


def _binding_problem(config: fly_runner.FlyConfig, provider: str) -> str:
    machine_id = config.login_machines.get(provider)
    if not machine_id:
        return MISSING_BINDING
    if not _MACHINE_ID_RE.fullmatch(machine_id):
        return INVALID_BINDING
    others = [
        p
        for p, mid in config.login_machines.items()
        if p != provider and mid == machine_id
    ]
    return DUPLICATE_BINDING if others else ""


def _failure_code(exc: BaseException) -> tuple[str, int | None]:
    if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)):
        return TIMED_OUT, None
    if isinstance(exc, fly_runner.FlyApiError):
        if 100 <= exc.status <= 599:
            return REQUEST_FAILED, exc.status
        return MALFORMED_RESPONSE, None
    if isinstance(exc, httpx.HTTPError):
        return REQUEST_FAILED, None
    return UNEXPECTED_ERROR, None


async def _read(client: fly_runner.FlyMachinesClient, provider: str,
                machine_id: str) -> dict:  # fmt: skip
    machine = await client.get(machine_id)
    if machine is None:
        return fly_runner.project_machine_status(provider, None, absent=True)
    return fly_runner.project_machine_status(provider, machine)


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _cleanup_seconds() -> float:
    return min(CLEANUP_SECONDS, DEADLINE_SECONDS / 4)


async def collect(providers: tuple[str, ...], *,
                  transport: httpx.AsyncBaseTransport | None = None,
                  deadline: float | None = None) -> dict:  # fmt: skip
    """The status envelope for *providers*. Every requested binding is checked
    before any request; then at most one GET per provider, all inside the
    aggregate *deadline* (a ``time.monotonic()`` value)."""
    if deadline is None:
        deadline = time.monotonic() + DEADLINE_SECONDS
    try:
        config = _load_config()
    except _Rejected as rejected:
        return _envelope([], [_error(None, rejected.code)])
    # One bad binding stops the whole command before any client call, so a
    # partial read never stands in for a full one.
    binding_errors = [
        _error(provider, problem)
        for provider in providers
        if (problem := _binding_problem(config, provider))
    ]
    if binding_errors:
        return _envelope([_unknown(p) for p in providers], binding_errors)
    rows: dict[str, dict] = {}
    errors: list[dict] = []
    client = fly_runner.FlyMachinesClient(config, transport=transport)
    reads: dict[str, asyncio.Task] = {
        provider: asyncio.ensure_future(
            _read(client, provider, config.login_machines[provider])
        )
        for provider in providers
    }
    read_budget = max(0.0, deadline - _cleanup_seconds() - time.monotonic())
    _done, pending = await asyncio.wait(reads.values(), timeout=read_budget)
    for task in pending:
        task.cancel()
    if pending:
        # Bounded: a read that ignores cancellation or hangs while closing its
        # client is abandoned at the deadline, not waited on.
        await asyncio.wait(pending, timeout=_remaining(deadline))
    for provider, task in reads.items():
        if task in pending:
            if task.done() and not task.cancelled():
                task.exception()  # retrieved so it is never logged; not printed
            rows[provider] = _unknown(provider)
            errors.append(_error(provider, TIMED_OUT))
            continue
        exc = task.exception() if not task.cancelled() else TimeoutError()
        if exc is None:
            rows[provider] = task.result()
            if rows[provider]["machine_state"] == "unknown":
                # ``client.get`` already validated the machine, so the only
                # way the projector answers unknown here is a state outside
                # its vocabulary.
                errors.append(_error(provider, UNRECOGNIZED_STATE))
            continue
        rows[provider] = _unknown(provider)
        code, status = _failure_code(exc)
        errors.append(_error(provider, code, status))
    machines = [rows[p] for p in providers]
    return _envelope(machines, errors)


def _drop_loop_error(_loop: asyncio.AbstractEventLoop, _context: dict) -> None:
    """The event loop's exception handler: an abandoned or failed task is
    reported only through the fixed envelope, never as log or stderr text."""


def _close_loop(loop: asyncio.AbstractEventLoop, deadline: float) -> None:
    """Cancel what is left, wait for it only until *deadline*, then close the
    loop. Anything still running is abandoned: its coroutine is closed here,
    with finalizer reports suppressed, so nothing surfaces later."""
    try:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.wait(pending, timeout=_remaining(deadline)))
        shutdown = loop.create_task(loop.shutdown_asyncgens())
        loop.run_until_complete(asyncio.wait({shutdown}, timeout=_remaining(deadline)))
        stuck = [t for t in asyncio.all_tasks(loop) if not t.done()]
        saved_hook = sys.unraisablehook
        sys.unraisablehook = lambda _unraisable: None
        try:
            for task in stuck:
                with contextlib.suppress(BaseException):
                    task.get_coro().close()
        finally:
            sys.unraisablehook = saved_hook
    finally:
        loop.close()


def run_bounded(providers: tuple[str, ...], *,
                transport: httpx.AsyncBaseTransport | None = None) -> dict:  # fmt: skip
    """``collect`` on a private event loop; the whole run, including its
    teardown, ends by one ``DEADLINE_SECONDS`` deadline. ``asyncio.run`` is
    not used because its teardown waits without limit for cancelled tasks."""
    deadline = time.monotonic() + DEADLINE_SECONDS
    loop = asyncio.new_event_loop()
    loop.set_exception_handler(_drop_loop_error)
    try:
        task = loop.create_task(
            collect(providers, transport=transport, deadline=deadline)
        )
        loop.run_until_complete(asyncio.wait({task}, timeout=_remaining(deadline)))
        if task.done():
            return task.result()
        task.cancel()
        return _envelope([_unknown(p) for p in providers],
                         [_error(p, TIMED_OUT) for p in providers])  # fmt: skip
    finally:
        _close_loop(loop, deadline)


@contextlib.contextmanager
def _quiet_logging():
    """Silence every logger that could carry a URL, header or response text,
    and all of their child loggers, for the duration of the read; restored
    afterwards.

    Each is disabled (its own records are dropped), stops propagating, and
    holds only a ``NullHandler``. Stopping propagation at the parent is what
    catches a child logger, including one first created during the run: its
    records reach that parent's ``NullHandler`` and go no further, neither to
    the root handlers nor to logging's last-resort stderr handler.
    """
    names = set(_QUIET_LOGGERS) | set(_KNOWN_CHILD_LOGGERS)
    names |= {
        name
        for name in list(logging.Logger.manager.loggerDict)
        if any(name.startswith(parent + ".") for parent in _QUIET_LOGGERS)
    }
    saved = []
    for name in sorted(names):
        logger = logging.getLogger(name)
        saved.append((logger, logger.disabled, logger.propagate, logger.handlers))
    try:
        for logger, *_ in saved:
            logger.disabled = True
            logger.propagate = False
            logger.handlers = [logging.NullHandler()]
        yield
    finally:
        for logger, disabled, propagate, handlers in saved:
            logger.disabled = disabled
            logger.propagate = propagate
            logger.handlers = handlers


def main(argv: list[str] | None = None, *,
         transport: httpx.AsyncBaseTransport | None = None) -> int:  # fmt: skip
    """Print the status envelope; return the exit code. Never raises, never
    prints a traceback, an exception message or an input value."""
    try:
        args = list(sys.argv[1:] if argv is None else argv)
        parsed = parse_args(args)
        if parsed is None:
            sys.stderr.write(INVALID_ARGUMENTS_TEXT)
            sys.stdout.write(
                _encode(_envelope([], [_error(None, INVALID_ARGUMENTS)])) + "\n"
            )
            return EXIT_INCOMPLETE
        mode, providers = parsed
        if mode == "help":
            sys.stdout.write(HELP)
            return EXIT_COMPLETE
        with _quiet_logging():
            envelope = run_bounded(providers, transport=transport)
        sys.stdout.write(_encode(envelope) + "\n")
        return EXIT_COMPLETE if envelope["complete"] else EXIT_INCOMPLETE
    except (KeyboardInterrupt, asyncio.CancelledError):
        output = INTERRUPTED_OUTPUT
    except BaseException:  # noqa: BLE001 - any failure prints the fixed envelope only
        output = FALLBACK_OUTPUT
    with contextlib.suppress(BaseException):
        sys.stdout.write(output + "\n")
    return EXIT_INCOMPLETE


if __name__ == "__main__":
    raise SystemExit(main())
