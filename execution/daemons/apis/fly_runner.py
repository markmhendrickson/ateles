"""The ``fly`` executor: run one dispatch on a Fly Machine (ateles#1456).

Apis keeps every decision it makes today (pace and usage gates, provider
choice, tiering, prompt and argv). Only the step that runs the CLI moves: the
same argv, stdin and timeout are handed to a Fly Machine that receives ONLY
this job's credentials, and the same ``(returncode, stdout, stderr)`` comes
back. The local executor is untouched; this module is reached only when a
dispatch class opts in (``APIS_FLY_RUNNER_CLASSES``) or a caller forces it
(``runner="fly"``, ``dispatch_role.py --runner fly``). With neither, nothing
here runs.

The per-job credential set, and nothing else:

* **GitHub**: a GitHub App installation token minted for THIS job, scoped to
  the one repository, and revoked when the job ends on every exit path. Never
  a PAT: the daemon's shared PAT and ``GITHUB_TOKEN`` never leave the Mac. If
  the App is unconfigured or the mint fails, the job does not go to Fly.
* **Neotoma**: the role's OWN identity. Its AAuth signing key
  (``<ATELES_PRIVATE_KEYS_DIR>/<role>.jwk.json``) and/or its own bearer
  (``<ROLE>_NEOTOMA_TOKEN``). A role with neither is not eligible. The shared
  daemon bearer never leaves the Mac.
* **Provider**: NO provider credential leaves the Mac. Claude, Codex and
  Cursor each run on their own long-lived login machine, stopped between
  jobs, whose volume holds the provider's login. For Claude that is the
  ``claude setup-token`` OAuth token in a file on the volume; the job carries
  only that file's fixed path, and ``run_job.py`` reads it on the machine and
  hands it to the CLI child alone.

Secrets travel as machine-level files (Machines API ``config.files``), never
Fly app secrets. The Fly deploy token (``APIS_FLY_API_TOKEN``) stays on the
Mac and is never logged.

Caps are enforced here, before any launch, in a local ledger: a monthly dollar
budget, a per-day machine count and a concurrency limit. A refusal raises
``FlyRefusal`` carrying ``cooled_until`` so the caller returns the same shape
as a gate refusal.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import fcntl
import json
import logging
import math
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

log = logging.getLogger("apis.fly_runner")

FLY_API_BASE_DEFAULT = "https://api.machines.dev/v1"
GITHUB_API = "https://api.github.com"
JOB_DIR = "/run/job"
RESULT_ENTITY_TYPE = "fly_runner_result"
# run_job.py keeps the LAST this-many bytes of stdout (STDOUT_CAP_BYTES there).
RUNNER_STDOUT_CAP_BYTES = 256 * 1024
MCP_CONFIG_PLACEHOLDER = "@@ATELES_JOB_MCP_CONFIG@@"
KEY_ONLY_NEOTOMA_TOOLS_NOTE = (
    "Ran on Fly with the role's AAuth key only: this run had no Neotoma MCP tools."
)
# Where Claude's system prompt file goes in argv; run_job.py writes the file
# (shipped as a job file) and puts its path here.
SYSTEM_PROMPT_PLACEHOLDER = "@@ATELES_JOB_SYSTEM_PROMPT@@"
SYSTEM_PROMPT_JOB_FILE = "system_prompt.md"
# The largest single argument the Fly runner passes to a CLI. Linux refuses
# any one argument over 131072 bytes (MAX_ARG_STRLEN) with E2BIG, after the
# machine has started; this bound refuses before any launch, with room left.
MAX_ARGV_ELEMENT_BYTES = 100_000
AAUTH_JWK_JOB_NAME = "NEOTOMA_AAUTH_PRIVATE_JWK"
APIS_DEADLINE_GRACE_SECONDS = 120
REAPER_INTERVAL_SECONDS = 600
# Binary names on the runner image (argv[0] on the Mac is a local path).
RUNNER_BINARIES: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "cursor": "cursor-agent",
}
LOGIN_PROVIDERS: frozenset[str] = frozenset({"claude", "codex", "cursor"})
# Claude's OAuth token lives ONLY on its login machine's volume, in this file.
# The job names the path (non-secret); run_job.py reads it on the machine.
CLAUDE_TOKEN_FILE = "/data/claude/oauth_token"
CLAUDE_TOKEN_NAME = "CLAUDE_CODE_OAUTH_TOKEN"
# shared-cpu-2x / 2 GB, Amsterdam list price (ateles#1456 cost table): about
# $13.90 per 30 days.
DEFAULT_RATE_PER_HOUR_USD = 13.90 / (30 * 24)
DEFAULT_STORAGE_GB_MONTH_USD = 0.15
DEFAULT_LOGIN_VOLUME_GB = 1.0
DEFAULT_ROOTFS_GB = 2.0
# The documented defaults of the cost and capacity settings, used ONLY when
# the setting is unset (absent from the environment). Each is bound-checked
# like a supplied value (see LIMITS).
DEFAULT_MONTHLY_BUDGET_USD = 20.0
DEFAULT_MAX_MACHINES_PER_DAY = 50
DEFAULT_MAX_CONCURRENT = 3
DEFAULT_QUEUE_MAX_WAIT_SECONDS = 1800
# The longest dispatch timeout a Fly job may carry.
MAX_JOB_TIMEOUT_SECONDS = 6 * 3600
# The largest number any accounting record may hold (dollars, timestamps,
# seconds): far above every real value, far below float overflow.
MAX_RECORD_NUMBER = 1e12
MAX_RECORD_COUNT = 10**9


class FlyIneligible(Exception):
    """This job cannot run on Fly (no own identity, no App, unsupported
    shape). The reason names what is missing, never a value."""


def _command_too_large(provider: str, size: int) -> "FlyConfigInvalid":
    return FlyConfigInvalid(
        f"the {provider} command has an argument of {size} bytes, over the Fly runner's "
        f"{MAX_ARGV_ELEMENT_BYTES}-byte limit for one argument (Linux refuses one over "
        "131072 bytes); nothing was launched"
    )


class FlyRefusal(Exception):
    """A cost or queue cap refused the launch; ``cooled_until`` says when."""

    def __init__(self, reason: str, cooled_until: str):
        super().__init__(reason)
        self.cooled_until = cooled_until


class FlyConfigInvalid(FlyRefusal):
    """A cost, capacity or time setting is unset (where required),
    unparseable, non-finite or out of range: Fly is disabled until it is
    fixed. Never defaulted, never clamped; no retry time (waiting does not
    fix it). The message names the setting, never its value."""

    def __init__(self, reason: str):
        super().__init__(reason, "")


class FlyLaunchError(Exception):
    """The Fly API refused or failed to start the machine."""


_OPAQUE_RUN = re.compile(r"[A-Za-z0-9+/=_\-]{40,}")

# Every Machines API lease nonce this process has held. A nonce is a secret
# (it authorises changes to a leased machine): it is never logged, recorded,
# put in a harness_event or a result, and any text that could carry one is
# passed through ``redact_nonces`` first.
_LEASE_NONCES: set[str] = set()
LEASE_NONCE_HEADER = "fly-machine-lease-nonce"


def redact_nonces(text: str) -> str:
    for nonce in list(_LEASE_NONCES):
        if nonce:
            text = text.replace(nonce, "[lease-nonce]")
    return text


class FlyApiError(Exception):
    """A Machines API failure. Its text reaches results, logs and operator
    notices, so any long opaque run (which could be an echoed, encoded job
    file) and any lease nonce is redacted, and the message is capped."""

    def __init__(self, status: int, message: str):
        safe = redact_nonces(_OPAQUE_RUN.sub("[redacted]", str(message)))[:200]
        super().__init__(f"Fly API HTTP {status}: {safe}")
        self.status = status


class FlyLeaseBusy(FlyApiError):
    """Another holder has the machine's lease."""


# ── Strict validation, one validator per response or record type ──────────────
#
# Each validator returns the object only when EVERY required field is present,
# non-null and of the right type; otherwise it raises ``FlyInvalid``. Nothing
# downstream turns absence into a value: an INVALID answer means "unknown",
# and in settlement, cleanup, lease release, reaping and recovery "unknown"
# keeps the reservation (unconfirmed, nonce retained). The field lists below
# are what the tests parametrise over, so a field added here is tested.

NONEMPTY_STR = "non-empty string"
STRING = "string"
OBJECT = "object"
NUMBER = "number"
INTEGER = "integer"
COUNT = "count (integer, 0 or more)"
POSITIVE_INT = "process id (integer above 1)"
BOOLEAN = "boolean"
MONTH = "month marker (YYYY-MM)"
DAY = "day marker (YYYY-MM-DD)"
NULLABLE_NUMBER = "number or null"

REQUIRED_FIELDS: dict[str, dict[str, str]] = {
    # GET /machines/{id}: everything a decision reads.
    "machine": {"id": NONEMPTY_STR, "state": NONEMPTY_STR, "instance_id": NONEMPTY_STR,
                "config": OBJECT},
    # Each entry of GET /machines.
    "list_entry": {"id": NONEMPTY_STR, "state": NONEMPTY_STR, "instance_id": NONEMPTY_STR,
                   "config": OBJECT},
    # The data of GET /machines/{id}/lease: the holder identity is required.
    "lease_view": {"description": NONEMPTY_STR, "expires_at": NUMBER},
    # The data of POST /machines/{id}/lease.
    "lease_grant": {"nonce": NONEMPTY_STR, "expires_at": NUMBER},
    # A reservation in the ledger.
    "ledger_entry": {"run_id": NONEMPTY_STR, "owner": NONEMPTY_STR, "started_at": NUMBER,
                     "deadline": NUMBER, "max_cost_usd": NUMBER, "login": BOOLEAN},
    # The ledger itself: the accounting period markers and counters.
    "ledger": {"month": MONTH, "day": DAY, "spend_usd": NUMBER, "launches_today": COUNT,
               "local_fallbacks": COUNT, "in_flight": OBJECT, "owners": OBJECT},
    # An owner's liveness proof in the ledger.
    "owner_record": {"id": NONEMPTY_STR, "pid": POSITIVE_INT, "host": NONEMPTY_STR,
                     "start": STRING, "heartbeat": NUMBER},
    # The runner's result record (run_job.build_record writes every field).
    "result_record": {"run_id": NONEMPTY_STR, "exit_code": INTEGER, "stdout": STRING,
                      "stdout_truncated": BOOLEAN, "stderr_tail": STRING,
                      "wall_ms": COUNT, "timed_out": BOOLEAN, "refused": STRING,
                      "probe_ok": BOOLEAN},
}  # fmt: skip

# Optional fields: may be absent, but when present must have this type.
OPTIONAL_FIELDS: dict[str, dict[str, type | tuple | str]] = {
    "machine_config": {"files": list, "env": dict, "metadata": dict},
    "config_metadata": {"ateles_run_id": str},
    "config_env": {"ATELES_RUN_ID": str},
    "ledger_entry": {"machine_id": str, "lease_nonce": str, "unconfirmed": bool,
                     "lease_expires": NULLABLE_NUMBER, "untracked": bool,
                     "unconfirmed_reason": str},
    # Every other kind declares that it has no optional fields.
    "machine": {}, "list_entry": {}, "lease_view": {}, "lease_grant": {}, "ledger": {},
    "owner_record": {}, "result_record": {},
}  # fmt: skip


class FlyInvalid(FlyApiError):
    """A response or record that does not pass its validator: unknown."""

    def __init__(self, kind: str, why: str):
        super().__init__(0, f"invalid {kind}: {why}")
        self.kind = kind


# Canonical markers only: ASCII digits, a real calendar month or day, and
# exactly the text that date formats back to.
_MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])", re.ASCII)
_DAY_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])", re.ASCII)


def _calendar_marker(value: Any, pattern: "re.Pattern[str]", fmt: str) -> bool:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        return False
    try:
        parsed = datetime.strptime(value, fmt)
    except ValueError:
        return False  # e.g. February 31
    return parsed.strftime(fmt) == value


def _finite_number(value: Any, low: float, high: float) -> bool:
    """A real int or float with low <= value <= high. Written so that NaN
    fails (every comparison with NaN is false); infinities fail the bound."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return low <= value <= high


def _type_ok(value: Any, expected: str) -> bool:
    if expected == NONEMPTY_STR:
        return isinstance(value, str) and bool(value)
    if expected == STRING:
        return isinstance(value, str)
    if expected == OBJECT:
        return isinstance(value, dict)
    if expected == NUMBER:
        return _finite_number(value, 0, MAX_RECORD_NUMBER)
    if expected == NULLABLE_NUMBER:
        return value is None or _finite_number(value, 0, MAX_RECORD_NUMBER)
    if expected == INTEGER:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == COUNT:
        return isinstance(value, int) and _finite_number(value, 0, MAX_RECORD_COUNT)
    if expected == POSITIVE_INT:
        return isinstance(value, int) and not isinstance(value, bool) and value > 1
    if expected == BOOLEAN:
        return isinstance(value, bool)
    if expected == MONTH:
        return _calendar_marker(value, _MONTH_RE, "%Y-%m")
    if expected == DAY:
        return _calendar_marker(value, _DAY_RE, "%Y-%m-%d")
    raise ValueError(expected)


def validate(kind: str, data: Any) -> dict:
    """The validated object for *kind*, or raise ``FlyInvalid``."""
    if not isinstance(data, dict):
        raise FlyInvalid(kind, "not an object")
    for name, expected in REQUIRED_FIELDS[kind].items():
        if name not in data or data[name] is None:
            raise FlyInvalid(kind, f"{name} is missing")
        if not _type_ok(data[name], expected):
            raise FlyInvalid(kind, f"{name} is not a {expected}")
    for name, expected in OPTIONAL_FIELDS[kind].items():
        if name not in data:
            continue
        ok = (_type_ok(data[name], expected) if isinstance(expected, str)
              else isinstance(data[name], expected))  # fmt: skip
        if not ok:
            raise FlyInvalid(kind, f"{name} has the wrong type")
    if kind in ("machine", "list_entry"):
        _validate_config(kind, data["config"])
    if kind == "ledger_entry" and not data["deadline"] >= data["started_at"]:
        # A window that ends before it starts would charge nothing.
        raise FlyInvalid(kind, "deadline is before started_at")
    return data


def _validate_config(kind: str, config: dict) -> None:
    for name, expected in OPTIONAL_FIELDS["machine_config"].items():
        if name in config and not isinstance(config[name], expected):
            raise FlyInvalid(kind, f"config.{name} has the wrong type")
    for section, spec in (("metadata", "config_metadata"), ("env", "config_env")):
        values = config.get(section) if section in config else None
        if isinstance(values, dict):
            for name, expected in OPTIONAL_FIELDS[spec].items():
                if name in values and not isinstance(values[name], expected):
                    raise FlyInvalid(kind, f"config.{section}.{name} has the wrong type")


def _render_wall(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="minutes")


@dataclass(frozen=True)
class Limit:
    """One setting that controls money, capacity or time.

    ``low`` and ``high`` are finite inclusive bounds in the setting's own
    unit, and ``low`` is above zero: a zero, negative, non-finite or
    out-of-range value disables Fly. ``default`` is the documented value for
    an UNSET setting, or None when the setting is required. ``scale`` turns
    the setting's unit into the config field's (the hourly rate is kept per
    second)."""

    env: str
    attr: str
    kind: type
    low: float
    high: float
    default: float | None
    scale: float = 1.0


LIMITS: tuple[Limit, ...] = (
    Limit("APIS_FLY_MONTHLY_BUDGET_USD", "budget_usd", float, 0.01, 1000.0,
          DEFAULT_MONTHLY_BUDGET_USD),
    Limit("APIS_FLY_MAX_MACHINES_PER_DAY", "max_per_day", int, 1, 1000,
          DEFAULT_MAX_MACHINES_PER_DAY),
    Limit("APIS_FLY_MAX_CONCURRENT", "max_concurrent", int, 1, 50, DEFAULT_MAX_CONCURRENT),
    Limit("APIS_FLY_QUEUE_MAX_WAIT_SECONDS", "queue_max_wait_seconds", int, 1, 86400,
          DEFAULT_QUEUE_MAX_WAIT_SECONDS),
    Limit("APIS_FLY_RATE_PER_HOUR_USD", "rate_per_second_usd", float, 0.0001, 100.0,
          DEFAULT_RATE_PER_HOUR_USD, scale=3600),
    Limit("APIS_FLY_STORAGE_GB_MONTH_USD", "storage_gb_month_usd", float, 0.001, 10.0,
          DEFAULT_STORAGE_GB_MONTH_USD),
)  # fmt: skip
# Fields with no setting of their own, checked the same way on every config
# (including one constructed directly).
FIXED_LIMITS: tuple[Limit, ...] = (
    Limit("login_volume_gb", "login_volume_gb", float, 0.1, 500.0, DEFAULT_LOGIN_VOLUME_GB),
    Limit("rootfs_gb", "rootfs_gb", float, 0.1, 500.0, DEFAULT_ROOTFS_GB),
)

_SETTING_PATTERN = {int: re.compile(r"[0-9]{1,12}", re.ASCII),
                    float: re.compile(r"[0-9]{1,12}(\.[0-9]{1,12})?", re.ASCII)}  # fmt: skip


def _limit_range(limit: Limit) -> str:
    return f"{limit.kind.__name__} from {limit.low:g} to {limit.high:g}"


def parse_setting(limit: Limit, env: Any) -> float:
    """The setting's value in the config field's unit, or raise
    ``FlyConfigInvalid`` naming it. Unset uses the documented default only
    when one is declared; a supplied value must be a plain ASCII decimal
    (no sign, exponent, NaN or infinity) within the bounds. Never clamped."""
    if limit.env not in env:
        if limit.default is None:
            raise FlyConfigInvalid(f"{limit.env} is required and unset")
        value = limit.default
    else:
        text = str(env[limit.env]).strip()
        if not _SETTING_PATTERN[limit.kind].fullmatch(text):
            raise FlyConfigInvalid(
                f"{limit.env} is not a plain decimal {_limit_range(limit)}")
        value = limit.kind(text)
    if not _finite_number(value, limit.low, limit.high):
        raise FlyConfigInvalid(f"{limit.env} is outside {_limit_range(limit)}")
    return value if limit.scale == 1 else value / limit.scale


def _csv(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


# Dispatch classes whose every caller has been checked to treat an unknown
# remote outcome as terminal (no retry, no re-dispatch, no failover). Only
# these can be opted in through APIS_FLY_RUNNER_CLASSES; adding one means
# checking every retry lane that can re-dispatch it.
FLY_VETTED_CLASSES: frozenset[str] = frozenset({"carry_forward_check", "open_or_merge_pr"})
# Classes that may be listed (the operator's opt-in) but are never routed by
# class alone: only a builder's delivery path, which forces ``runner="fly"``
# with a delivery spec, sends them to Fly. Every retry lane that can re-run a
# builder treats a launched delivery's outcome as terminal (tested in
# test_fly_delivery_dispatch.py), which is what vetting this class required.
DELIVERY_ONLY_CLASSES: frozenset[str] = frozenset({"open_or_merge_pr"})
# The job-only credential name of a delivery's GitHub token, and the stdin
# and argv placeholders the job runner fills in (deploy/fly-runner/run_job.py
# defines the same names; a test pins them together).
DELIVERY_TOKEN_NAME = "ATELES_DELIVERY_GITHUB_TOKEN"
WORKSPACE_PLACEHOLDER = "@@ATELES_JOB_WORKSPACE@@"
PR_FILE_PLACEHOLDER = "@@ATELES_JOB_PR_FILE@@"
DELIVERY_SPEC_KEYS = ("branch", "base_branch", "author_name", "author_email")
# The fly_runner_result fields a delivering job's record carries.
DELIVERY_RESULT_FIELDS = ("delivery_status", "delivery_reason", "delivery_branch",
                          "delivery_commit", "pr_number", "pr_url")  # fmt: skip


def runner_classes() -> frozenset[str]:
    """The dispatch classes (``action_class`` values) opted onto Fly: those
    listed in ``APIS_FLY_RUNNER_CLASSES`` that are also vetted. An unvetted
    class is ignored, with a warning."""
    listed = frozenset(_csv(os.environ.get("APIS_FLY_RUNNER_CLASSES", "")))
    unvetted = listed - FLY_VETTED_CLASSES
    if unvetted:
        log.warning(
            "[fly] ignoring unvetted classes in APIS_FLY_RUNNER_CLASSES: "
            f"{sorted(unvetted)}; vetted: {sorted(FLY_VETTED_CLASSES)}"
        )
    return listed & FLY_VETTED_CLASSES


def delivery_opted_in() -> bool:
    """Whether the operator opted builder deliveries onto Fly: the delivery
    class is listed in ``APIS_FLY_RUNNER_CLASSES``. Unset, it is False."""
    return bool(DELIVERY_ONLY_CLASSES & runner_classes())


def wants_fly(runner: str | None, action_class: str | None) -> bool:
    """Whether this dispatch should try the fly executor at all.

    ``runner="local"`` always stays local; ``runner="fly"`` forces Fly; with
    no explicit runner, only an opted-in ``action_class`` goes to Fly. With
    ``APIS_FLY_RUNNER_CLASSES`` unset this is False for every dispatch.
    """
    if runner == "local":
        return False
    if runner == "fly":
        return True
    return (bool(action_class) and action_class in runner_classes()
            and action_class not in DELIVERY_ONLY_CLASSES)  # fmt: skip


@dataclass
class FlyConfig:
    app: str
    api_token: str
    image: str
    region: str
    api_base: str = FLY_API_BASE_DEFAULT
    budget_usd: float = DEFAULT_MONTHLY_BUDGET_USD
    max_per_day: int = DEFAULT_MAX_MACHINES_PER_DAY
    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    ledger_path: Path = Path.home() / ".config" / "ateles" / "fly-runner-ledger.json"
    login_machines: dict[str, str] = field(default_factory=dict)
    queue_max_wait_seconds: int = DEFAULT_QUEUE_MAX_WAIT_SECONDS
    local_fallback: bool = True
    github_app_prefix: str = "ATELES_REVIEWER_APP"
    extra_probe_env_names: tuple[str, ...] = ()
    rate_per_second_usd: float = DEFAULT_RATE_PER_HOUR_USD / 3600
    storage_gb_month_usd: float = DEFAULT_STORAGE_GB_MONTH_USD
    login_volume_gb: float = DEFAULT_LOGIN_VOLUME_GB
    rootfs_gb: float = DEFAULT_ROOTFS_GB
    cursor_login_dirs: tuple[str, ...] = (".cursor", ".config/cursor")
    # Problems found while loading the limit settings (names, never values).
    setting_problems: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "FlyConfig":
        env = os.environ
        limits: dict[str, float] = {}
        problems: list[str] = []
        for limit in LIMITS:
            try:
                limits[limit.attr] = parse_setting(limit, env)
            except FlyConfigInvalid as exc:
                problems.append(str(exc))
                # Never a default: a NaN fails every limit check, and
                # admission refuses on ``problems`` before any check runs.
                limits[limit.attr] = math.nan
        login = {}
        for provider in sorted(LOGIN_PROVIDERS):
            name = login_machine_setting(provider)
            if name in env and env[name].strip():
                login[provider] = env[name].strip()
        ledger = env.get("APIS_FLY_LEDGER_FILE", "").strip()
        return cls(
            app=env.get("APIS_FLY_APP", "").strip(),
            api_token=env.get("APIS_FLY_API_TOKEN", "").strip(),
            image=env.get("APIS_FLY_IMAGE", "").strip(),
            region=env.get("APIS_FLY_REGION", "").strip(),
            api_base=(env.get("APIS_FLY_API_BASE", "").strip() or FLY_API_BASE_DEFAULT).rstrip("/"),
            **limits,
            setting_problems=tuple(problems),
            ledger_path=Path(ledger).expanduser() if ledger else cls.ledger_path,
            login_machines=login,
            local_fallback=env.get("APIS_FLY_LOCAL_FALLBACK", "1").strip() != "0",
            github_app_prefix=(
                env.get("APIS_FLY_GITHUB_APP_PREFIX", "").strip() or "ATELES_REVIEWER_APP"
            ),
            extra_probe_env_names=tuple(_csv(env.get("APIS_FLY_PROBE_ENV_NAMES", ""))),
            cursor_login_dirs=tuple(
                _csv(env.get("APIS_FLY_CURSOR_LOGIN_DIRS", "")) or (".cursor", ".config/cursor")
            ),
        )

    def missing(self) -> list[str]:
        """Names of required settings that are unset (names only)."""
        out = []
        for name, value in (
            ("APIS_FLY_APP", self.app),
            ("APIS_FLY_API_TOKEN", self.api_token),
            ("APIS_FLY_IMAGE", self.image),
            ("APIS_FLY_REGION", self.region),
        ):
            if not value:
                out.append(name)
        return out

    def limit_problems(self) -> list[str]:
        """Every limit that is not a valid finite value within its bounds:
        the problems found when loading, plus a check of the fields
        themselves (a config may be constructed directly)."""
        out = list(self.setting_problems)
        for limit in (*LIMITS, *FIXED_LIMITS):
            if any(p.startswith(limit.env + " ") for p in out):
                continue
            value = getattr(self, limit.attr)
            if limit.kind is int and (isinstance(value, bool) or not isinstance(value, int)):
                out.append(f"{limit.env} is not an int")
            elif not _finite_number(value, limit.low / limit.scale, limit.high / limit.scale):
                out.append(f"{limit.env} is outside {_limit_range(limit)}")
        return out

    def require_valid_limits(self) -> None:
        """Refuse every Fly launch while any limit is invalid."""
        problems = self.limit_problems()
        if problems:
            raise FlyConfigInvalid(
                "Fly is disabled: " + "; ".join(problems)
                + ". Fix the setting; no Fly launches until then."
            )

    def monthly_storage_usd(self) -> float:
        """Volume plus stopped-rootfs storage of the long-lived login machines."""
        n = len(self.login_machines)
        return n * (self.login_volume_gb + self.rootfs_gb) * self.storage_gb_month_usd


# ── Ledger: dollars, daily count, concurrency, in-flight machines ─────────────


def _month_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")


def _day_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _next_month_start(ts: float) -> float:
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    year, month = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return datetime(year, month, 1, tzinfo=timezone.utc).timestamp()


def _next_day_start(ts: float) -> float:
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    return (datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(days=1)).timestamp()


def process_start_token(pid: int) -> str | None:
    """When *pid* started, as an opaque token; None when it is not running.

    "" means running with an unreadable start time. Comparing tokens detects
    a pid that was reused by a different process after its owner died.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        # 0 and negatives address process GROUPS, 1 is init: never an owner.
        return ""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None  # the one positive answer: no such process
    except PermissionError:
        pass  # exists, owned by someone else
    except OSError:
        return ""
    # From here the process EXISTS: every failure below is "start unknown"
    # (""), never "not running".
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        try:
            # Field 22 (starttime); fields after the parenthesised name start at 3.
            return stat.read_text().rsplit(")", 1)[1].split()[19]
        except (OSError, IndexError):
            return ""
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout.strip()  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return ""
    # An empty answer from ps is not proof the process ended (kill just said
    # it exists): its start time is unknown.
    return out if out else ""


def this_process_owner(instance_id: str) -> dict:
    """The liveness proof recorded with every reservation this process makes."""
    pid = os.getpid()
    start = process_start_token(pid)
    return {
        "id": instance_id,
        "pid": pid,
        "start": start if isinstance(start, str) else "",  # unknown start: ""
        "host": socket.gethostname(),
    }


def owner_provably_dead(record: dict | None) -> bool:
    """True only when the owner's process is provably gone: same host, and its
    pid is not running or now belongs to a process that started at another
    time. Anything unknown (another host, no record, unreadable start time)
    is NOT proof of death; staleness rules decide those."""
    try:
        record = validate("owner_record", record)
    except FlyInvalid:
        return False  # an unreadable record proves nothing
    if record["host"] != socket.gethostname():
        return False
    token = process_start_token(record["pid"])
    if token is None:
        return True  # positively: no such process
    # A different, KNOWN start time on the same pid: the pid was reused.
    return bool(token) and bool(record["start"]) and token != record["start"]


class FlyLedgerError(Exception):
    """The ledger file exists but cannot be read: accounting is unknown."""


def _new_entry(entry: dict) -> dict:
    """A reservation about to be written must pass the same validator every
    read applies, or nothing is written."""
    try:
        return validate("ledger_entry", entry)
    except FlyInvalid as exc:
        raise FlyLedgerError(f"refusing to write a reservation: {exc}") from None


@dataclass(frozen=True)
class LedgerSnapshot:
    """The ledger as ONE call of the validator read it, under the lock.

    Every field is validated (period markers, counters, every reservation,
    every owner record) and the period is rolled. Decisions read these
    fields only, never the raw file; writes start from ``document()``.
    """

    month: str
    day: str
    spend_usd: float
    launches_today: int
    local_fallbacks: int
    entries: dict[str, dict]
    owners: dict[str, dict]
    deliveries: dict[str, dict] = field(default_factory=dict)

    def outstanding_usd(self) -> float:
        """The reserved maximum cost of every reservation still held,
        recomputed from the validated reservations each time."""
        return sum(float(e["max_cost_usd"]) for e in self.entries.values())

    def check_totals(self, config: FlyConfig, name: str) -> None:
        """Admission's consistency checks: each stored total must agree with
        what the validated reservations say, or the ledger is INVALID."""
        rate = config.rate_per_second_usd
        for key, e in self.entries.items():
            # A reservation's stored cost must cover its whole window at the
            # current rate (a lower cost would under-reserve the budget).
            needed = (e["deadline"] - e["started_at"]) * rate
            if not e["max_cost_usd"] >= needed * (1 - 1e-9):
                raise FlyLedgerError(
                    f"Fly ledger {name}: reservation {key}'s reserved cost does not cover "
                    "its window at the current rate")
        admitted_today = sum(
            1 for e in self.entries.values()
            if not ("untracked" in e and e["untracked"]) and _day_key(e["started_at"]) == self.day
        )  # fmt: skip
        if not self.launches_today >= admitted_today:
            raise FlyLedgerError(
                f"Fly ledger {name}: the day's launch count is below the reservations "
                "admitted today")
        if not _finite_number(self.outstanding_usd(), 0, MAX_RECORD_NUMBER):
            raise FlyLedgerError(f"Fly ledger {name}: the reserved total is not a finite amount")

    def document(self) -> dict:
        """A fresh document built from the validated fields, for writing."""
        return {
            "month": self.month, "day": self.day, "spend_usd": self.spend_usd,
            "launches_today": self.launches_today, "local_fallbacks": self.local_fallbacks,
            "in_flight": copy.deepcopy(self.entries), "owners": copy.deepcopy(self.owners),
            **({"deliveries": copy.deepcopy(self.deliveries)} if self.deliveries else {}),
        }  # fmt: skip


class FlyLedger:
    """A local JSON ledger, like the tier ledger.

    One reservation per run, keyed by run id and written BEFORE any Fly call.
    Each carries its owner (the Apis process instance that made it), the
    machine once known, its window and its reserved maximum cost. A
    reservation counts against the concurrency cap and the budget until it is
    settled (termination confirmed, runtime charged once) or released
    (provably nothing ran). A lock serialises this process's threads.

    An existing ledger that cannot be parsed fails CLOSED: admission refuses,
    because resetting it would forget spend and running machines.
    """

    def __init__(self, path: Path, *, owner: dict | None = None,
                 clock: Callable[[], float] = time.time):  # fmt: skip
        self.path = Path(path)
        self._lock = threading.Lock()
        # This process's liveness proof; refreshed (heartbeat) on every write.
        self.owner = owner
        self._clock = clock

    @contextlib.contextmanager
    def _locked(self):
        """This process's threads AND every other process sharing the file
        (Apis and any ``dispatch_role --runner fly`` CLI) serialise here."""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path.with_name(self.path.name + ".lock"), "a+") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def heartbeat(self) -> None:
        """Refresh this process's owner heartbeat."""
        if self.owner is None:
            return
        with self._locked():
            try:
                snap = self._snapshot(self._clock())
            except FlyLedgerError:
                return
            self._save(snap.document())

    # ── the one validator ────────────────────────────────────────────────

    def _read_document(self) -> tuple[bool, Any]:
        """``(exists, parsed)``: the raw file, parsed but NOT validated. Only
        ``_snapshot`` reads it. Absence is decided ONLY by the file not
        existing; an existing file is never "no ledger", whatever it holds."""
        if not self.path.exists():
            return False, None
        try:
            # Strict UTF-8 (a byte-order mark or bad bytes are unreadable).
            return True, json.loads(self.path.read_bytes().decode("utf-8", errors="strict"))
        except (OSError, ValueError):
            raise FlyLedgerError(f"Fly ledger {self.path.name} is unreadable") from None

    def _snapshot(self, now: float, config: FlyConfig | None = None, *,
                  admission: bool = False) -> LedgerSnapshot:  # fmt: skip
        """THE validated ledger, or ``FlyLedgerError`` (INVALID).

        Every number any decision reads comes from this object, produced in
        one call, under the lock, immediately before that decision. It
        validates every top-level field, every reservation and every owner
        record with the same validators, and rolls the period. With
        ``admission`` it also recomputes the derived totals from the
        validated reservations and refuses on any mismatch with what is
        stored beside them. The caller must hold ``_locked()``.
        """
        name = self.path.name
        exists, raw = self._read_document()
        if not exists:
            # The only "new period" without a marker: no ledger file at all.
            raw = {"month": None, "spend_usd": 0.0, "day": None, "launches_today": 0,
                   "local_fallbacks": 0, "in_flight": {}, "owners": {}}  # fmt: skip
        else:
            try:
                # Any existing document, whatever its top level (null, a list,
                # a string, an empty object...), must pass the validator.
                validate("ledger", raw)
            except FlyInvalid as exc:
                raise FlyLedgerError(f"Fly ledger {name}: {exc}") from None
        entries: dict[str, dict] = {}
        for key, entry in raw["in_flight"].items():
            try:
                entry = validate("ledger_entry", entry)
            except FlyInvalid as exc:
                raise FlyLedgerError(f"Fly ledger {name}: reservation {key}: {exc}") from None
            if entry["run_id"] != key:
                raise FlyLedgerError(f"Fly ledger {name}: reservation {key} names another run")
            entries[key] = copy.deepcopy(entry)
        owners: dict[str, dict] = {}
        for key, record in raw["owners"].items():
            try:
                record = validate("owner_record", record)
            except FlyInvalid as exc:
                raise FlyLedgerError(f"Fly ledger {name}: owner {key}: {exc}") from None
            if record["id"] != key:
                raise FlyLedgerError(f"Fly ledger {name}: owner {key} names another owner")
            owners[key] = copy.deepcopy(record)
        deliveries = raw.get("deliveries", {})
        if not isinstance(deliveries, dict) or not all(
            isinstance(key, str) and isinstance(value, dict)
            and isinstance(value.get("status"), str)
            for key, value in deliveries.items()
        ):
            raise FlyLedgerError(f"Fly ledger {name}: delivery receipts are unreadable")
        period = {"month": raw["month"], "day": raw["day"], "spend_usd": raw["spend_usd"],
                  "launches_today": raw["launches_today"]}  # fmt: skip
        self._roll(period, now)  # refuses a later period; resets only on a rollover
        snap = LedgerSnapshot(
            month=period["month"], day=period["day"], spend_usd=float(period["spend_usd"]),
            launches_today=period["launches_today"], local_fallbacks=raw["local_fallbacks"],
            entries=entries, owners=owners, deliveries=copy.deepcopy(deliveries),
        )
        if admission:
            if config is None:
                raise FlyLedgerError("admission needs the configuration it is checked against")
            snap.check_totals(config, name)
        return snap

    def _load(self) -> dict:
        """The validated ledger as a document (for writing back)."""
        return self._snapshot(self._clock()).document()

    def _save(self, data: dict) -> None:
        if data["month"] is None or data["day"] is None:
            # A brand-new ledger gets its period markers before its first
            # write, so a saved ledger always carries valid ones.
            self._roll(data, self._clock())
        if self.owner is not None:
            owners = data["owners"]
            owners[self.owner["id"]] = {**self.owner, "heartbeat": self._clock()}
            # Keep the map small: drop owners that hold nothing any more.
            holding = {e["owner"] for e in data["in_flight"].values()
                       if isinstance(e, dict) and isinstance(e.get("owner"), str)}
            for oid in [o for o in owners if o not in holding and o != self.owner["id"]]:
                del owners[oid]
        try:
            # A non-finite number is never written: the file stays valid JSON
            # and the loader would refuse it anyway.
            text = json.dumps(data, sort_keys=True, indent=1, allow_nan=False)
        except ValueError:
            raise FlyLedgerError(f"Fly ledger {self.path.name}: refusing to write a "
                                 "non-finite number") from None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def delivery_receipts(self) -> dict[str, dict]:
        """Durable issue/task → run bindings, kept when accounting settles.

        Retained across period rollovers: spending resets do not prove that a
        previous job pushed nothing. The receipt contains only the executor's
        observed result fields and never any credential.
        """
        if not self.path.exists():
            return {}
        with self._locked():
            return self._snapshot(self._clock()).deliveries

    def _write_delivery(self, key: str, write) -> bool:
        """Apply *write(receipts)* to the delivery receipts under the ledger lock
        and read the result back. *write* returns False to change nothing."""
        with self._locked():
            data = self._load()
            if "deliveries" not in data:
                data["deliveries"] = {}
            receipts = data["deliveries"]
            if not write(receipts):
                return False
            self._save(data)
            if self._snapshot(self._clock()).deliveries.get(key) != receipts.get(key):
                raise FlyLedgerError("delivery receipt did not read back")
            return True

    def reserve_delivery(self, key: str, entry: dict) -> bool:
        """Create the receipt for *key* if there is none (False if one exists,
        whatever its status). A read-back is required before the caller may send
        anything to Fly. Accounting and reaper writes preserve receipts through
        ``LedgerSnapshot.document``."""
        if not isinstance(entry.get("status"), str):
            raise FlyLedgerError("delivery receipt has no status")

        def write(receipts: dict) -> bool:
            if key in receipts:
                return False
            receipts[key] = entry
            return True

        return self._write_delivery(key, write)

    def settle_delivery(self, key: str, token: str, entry: dict) -> bool:
        """Replace the receipt for *key* with *entry* only if the stored receipt
        is still the reservation *token* made (compare-and-set); otherwise leave
        it untouched and return False. *entry* carries the same token."""
        if not isinstance(entry.get("status"), str) or entry.get("token") != token:
            raise FlyLedgerError("delivery receipt does not carry its reservation token")

        def write(receipts: dict) -> bool:
            if key not in receipts or receipts[key].get("token") != token:
                return False
            receipts[key] = entry
            return True

        return self._write_delivery(key, write)

    def release_delivery(self, key: str, token: str) -> bool:
        """Remove the receipt for *key* only if it is still the reservation
        *token* made and still ``launching`` (compare-and-remove); otherwise
        leave it untouched and return False. For a dispatch that provably
        launched nothing."""

        def write(receipts: dict) -> bool:
            held = receipts.get(key)
            if held is None or held.get("token") != token or held.get("status") != "launching":
                return False
            receipts.pop(key)
            return True

        return self._write_delivery(key, write)

    def clear_delivery(self, key: str, expected: dict, *, before=None) -> bool:
        """Remove one delivery receipt, only if it still equals *expected*.

        The operator's reconcile step (``fly_delivery_receipt.py``): under the
        ledger's own lock and atomic write, nothing else is touched, and a
        receipt that changed since it was shown is left alone (False). *before*
        is called under the same lock once the receipt has been compared and
        matched, and before anything is written: if it raises, nothing is
        removed. A malformed ledger raises ``FlyLedgerError``. An exception
        raised from the write may come after the file was replaced, so a caller
        decides what happened by reading the ledger again. No code path calls
        this automatically.
        """
        with self._locked():
            data = self._load()
            if "deliveries" not in data:
                return False
            receipts = data["deliveries"]
            if receipts.get(key) != expected:
                return False
            if before is not None:
                before()
            receipts.pop(key)
            self._save(data)
            if key in self._snapshot(self._clock()).deliveries:
                raise FlyLedgerError("delivery receipt was not removed")
            return True

    def _roll(self, data: dict, now: float) -> None:
        """Start a new period only on a genuine rollover: a VALID marker
        EARLIER than the current period (or no ledger at all, ``None``). A
        marker later than the current period is refused (canonical markers
        order as text), never read as a new period."""
        month, day = _month_key(now), _day_key(now)
        for marker, current in ((data["month"], month), (data["day"], day)):
            if marker is not None and not marker <= current:
                raise FlyLedgerError(
                    f"Fly ledger {self.path.name}: its period marker is later than "
                    "the current period")
        if data["month"] is None or (_type_ok(data["month"], MONTH) and data["month"] != month):
            data["month"] = month
            data["spend_usd"] = 0.0
        if data["day"] is None or (_type_ok(data["day"], DAY) and data["day"] != day):
            data["day"] = day
            data["launches_today"] = 0

    def snapshot(self, now: float | None = None) -> dict:
        """A copy of the validated ledger (raises ``FlyLedgerError``)."""
        with self._locked():
            return self._snapshot(self._clock() if now is None else now).document()

    # ── decisions: each reads ONE snapshot taken under the lock ──────────

    def reserve(self, config: FlyConfig, *, run_id: str, owner: str, max_run_seconds: int,
                login: bool = False, machine_id: str = "",
                now: float | None = None) -> None:  # fmt: skip
        """Refuse, or reserve one launch slot for *run_id*, owned by *owner*.

        Admission is atomic under the ledger lock and decided ONLY from one
        validated snapshot: against the budget it counts the month's
        completed spend, the login machines' storage, the reserved maximum
        cost of EVERY reservation still held (including unconfirmed and
        untracked machines), and the incoming job's own maximum-length cost.
        Refuses when that total would exceed the budget (until the next
        calendar month), when the day's launch count is at its cap (until
        the next UTC day), or when the concurrency limit is reached (until
        the earliest held deadline). An invalid or inconsistent ledger
        refuses every launch until it is repaired.
        """
        now = self._clock() if now is None else now
        config.require_valid_limits()  # also for a directly constructed config
        if not _finite_number(max_run_seconds, 1, MAX_JOB_TIMEOUT_SECONDS
                              + APIS_DEADLINE_GRACE_SECONDS):  # fmt: skip
            raise FlyConfigInvalid("the job's maximum run time is outside its bounds")
        with self._locked():
            try:
                snap = self._snapshot(now, config, admission=True)
            except FlyLedgerError as exc:
                # No retry time: this does not clear by waiting.
                raise FlyRefusal(
                    f"{exc}. The Fly ledger needs operator repair; no Fly launches "
                    "until it is fixed (spend and caps are unknown).", "",
                ) from None
            if run_id in snap.entries:
                raise FlyRefusal(f"Fly run {run_id} already holds a reservation", "")
            worst = max_run_seconds * config.rate_per_second_usd
            committed = snap.spend_usd + config.monthly_storage_usd() + snap.outstanding_usd()
            if not (worst > 0 and committed + worst <= config.budget_usd):
                raise FlyRefusal(
                    f"Fly monthly budget: ${committed:.4f} spent, stored or reserved "
                    f"in flight plus ${worst:.4f} for this run's maximum length would "
                    f"exceed APIS_FLY_MONTHLY_BUDGET_USD=${config.budget_usd:.2f}",
                    _render_wall(_next_month_start(now)),
                )
            if not snap.launches_today < config.max_per_day:
                raise FlyRefusal(
                    f"Fly daily machine cap reached (APIS_FLY_MAX_MACHINES_PER_DAY="
                    f"{config.max_per_day})",
                    _render_wall(_next_day_start(now)),
                )
            if not len(snap.entries) < config.max_concurrent:
                deadlines = [e["deadline"] for e in snap.entries.values()]
                earliest = min(deadlines) if deadlines else now + 60  # a retry hint only
                raise FlyRefusal(
                    f"Fly concurrency cap reached (APIS_FLY_MAX_CONCURRENT="
                    f"{config.max_concurrent})",
                    _render_wall(max(earliest, now + 60)),
                )
            doc = snap.document()
            doc["launches_today"] = snap.launches_today + 1
            doc["in_flight"][run_id] = _new_entry(
                {"run_id": run_id, "owner": owner, "machine_id": machine_id, "login": login,
                 "started_at": now, "deadline": now + max_run_seconds, "max_cost_usd": worst})
            self._save(doc)

    def record_untracked(self, *, machine_id: str, run_id: str, owner: str, config: FlyConfig,
                         started_at: float, max_run_seconds: int) -> str:  # fmt: skip
        """Own a machine found running that no reservation accounts for. It is
        recorded unconfirmed with a full reserved cost, so it counts against
        concurrency and budget until its termination is confirmed. Bypasses
        the caps: it records a fact, it is not an admission."""
        key = run_id or f"untracked-{machine_id}"
        with self._locked():
            snap = self._snapshot(self._clock())
            if key not in snap.entries:
                doc = snap.document()
                doc["in_flight"][key] = _new_entry(
                    {"run_id": key, "owner": owner, "machine_id": machine_id, "login": False,
                     "started_at": started_at, "deadline": started_at + max_run_seconds,
                     "max_cost_usd": max_run_seconds * config.rate_per_second_usd,
                     "unconfirmed": True, "untracked": True})  # fmt: skip
                self._save(doc)
        return key

    def _update_entry(self, run_id: str, change: Callable[[dict], bool]) -> None:
        """Apply *change* to *run_id*'s reservation in a fresh snapshot and
        write it back if *change* says it changed something."""
        with self._locked():
            doc = self._snapshot(self._clock()).document()
            if run_id in doc["in_flight"] and change(doc["in_flight"][run_id]):
                _new_entry(doc["in_flight"][run_id])  # still a valid reservation
                self._save(doc)

    def set_lease(self, run_id: str, nonce: str, expires_at: float | None) -> None:
        """Keep the lease nonce WITH the reservation, so a later settlement or
        reaper from the same owner can release it. The ledger file is private
        to the operator (mode 0600)."""
        def change(entry: dict) -> bool:
            entry["lease_nonce"] = nonce
            entry["lease_expires"] = expires_at
            return True

        self._update_entry(run_id, change)

    def clear_lease(self, run_id: str) -> None:
        def change(entry: dict) -> bool:
            if "lease_nonce" not in entry:
                return False
            del entry["lease_nonce"]
            if "lease_expires" in entry:
                del entry["lease_expires"]
            return True

        self._update_entry(run_id, change)

    def set_machine(self, run_id: str, machine_id: str) -> None:
        def change(entry: dict) -> bool:
            entry["machine_id"] = machine_id
            return True

        self._update_entry(run_id, change)

    def mark_unconfirmed(self, run_id: str, *, reason: str) -> None:
        """Keep *run_id*'s slot and reservation, flagged for the reaper: its
        machine's termination could not be confirmed, so it is still owned."""
        def change(entry: dict) -> bool:
            entry["unconfirmed"] = True
            entry["unconfirmed_reason"] = reason[:200]
            return True

        self._update_entry(run_id, change)

    def entry(self, run_id: str) -> dict | None:
        """A validated copy of *run_id*'s reservation, or None if absent."""
        with self._locked():
            entries = self._snapshot(self._clock()).entries
            return copy.deepcopy(entries[run_id]) if run_id in entries else None

    def find_by_machine(self, machine_id: str) -> dict[str, dict]:
        with self._locked():
            entries = self._snapshot(self._clock()).entries
            return {rid: copy.deepcopy(e) for rid, e in entries.items()
                    if "machine_id" in e and e["machine_id"] == machine_id}  # fmt: skip

    def release(self, run_id: str) -> None:
        """Drop a reservation that provably never ran anything (no charge)."""
        with self._locked():
            snap = self._snapshot(self._clock())
            if run_id in snap.entries:
                doc = snap.document()
                del doc["in_flight"][run_id]
                self._save(doc)

    def settle(self, run_id: str, *, config: FlyConfig, now: float | None = None) -> float:
        """Release *run_id* and charge its incurred runtime, EXACTLY ONCE.

        The charge is the entry's elapsed time capped at its reserved window,
        read from the validated snapshot under the lock. An entry already
        settled is absent, so a second settlement charges nothing. Callers
        settle only after termination is confirmed. An invalid ledger, or a
        charge that is not a finite amount, raises ``FlyLedgerError`` and
        keeps the reservation. Returns the dollars charged.
        """
        now = self._clock() if now is None else now
        with self._locked():
            snap = self._snapshot(now)
            if run_id not in snap.entries:
                return 0.0
            cost = _charged_seconds(snap.entries[run_id], now) * config.rate_per_second_usd
            if not _finite_number(cost, 0, MAX_RECORD_NUMBER):
                # Keep the reservation (and its full reserved cost) rather
                # than charge an unreadable amount.
                raise FlyLedgerError(f"Fly charge for {run_id} is not a finite amount")
            doc = snap.document()
            del doc["in_flight"][run_id]
            doc["spend_usd"] = snap.spend_usd + cost
            self._save(doc)
            return cost

    def count_local_fallback(self) -> int:
        with self._locked():
            try:
                snap = self._snapshot(self._clock())
            except FlyLedgerError:
                return -1
            doc = snap.document()
            doc["local_fallbacks"] = snap.local_fallbacks + 1
            self._save(doc)
            return doc["local_fallbacks"]

# ── Machines API client ─────────────────────────────────────────────────────


def _invalid(what: str) -> "FlyApiError":
    # Status 0: not an HTTP rejection. Never treated as proof of anything.
    return FlyApiError(0, f"invalid Machines API response: {what}")


class FlyMachinesClient:
    """Thin async client for ``/v1/apps/<app>/machines``. Never logs headers;
    error messages carry the HTTP status and Fly's own error text only.

    Responses are validated per endpoint. A malformed or unexpected body
    raises ``FlyApiError`` rather than reading as "nothing there": only an
    explicit 404 means a machine is absent, and only a well-formed list is a
    machine listing. Callers treat any error as "state unknown".
    """

    def __init__(self, config: FlyConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=f"{self.config.api_base}/apps/{self.config.app}",
            headers={"Authorization": f"Bearer {self.config.api_token}"},
            timeout=httpx.Timeout(90.0),
            transport=self._transport,
        )

    async def _call(self, method: str, path: str, *, params=None, body=None,
                    parse: bool = True, nonce: str = "") -> Any:  # fmt: skip
        headers = {LEASE_NONCE_HEADER: nonce} if nonce else None
        async with self._client() as client:
            resp = await client.request(method, path, params=params, json=body, headers=headers)
        if not 200 <= resp.status_code < 300:
            try:
                message = str(resp.json().get("error") or resp.text)
            except (ValueError, AttributeError):
                message = resp.text
            raise FlyApiError(resp.status_code, message)
        if not parse:
            return None
        try:
            return resp.json()
        except ValueError:
            raise _invalid(f"{method} {path.split('?')[0]} returned malformed JSON") from None

    @staticmethod
    def _machine(data: Any, *, expect_id: str = "", require_state: bool = True) -> dict:
        """A machine object, or raise. Identity, state and the ownership
        metadata are all checked, because each one drives a decision."""
        if not isinstance(data, dict):
            raise _invalid("machine is not an object")
        mid = data.get("id")
        if not isinstance(mid, str) or not mid:
            raise _invalid("machine has no id")
        if expect_id and mid != expect_id:
            raise _invalid("machine id does not match the request")
        state = data.get("state")
        if (require_state or state is not None) and (not isinstance(state, str) or not state):
            raise _invalid("machine state is missing or not a string")
        config = data.get("config")
        if config is not None:
            if not isinstance(config, dict):
                raise _invalid("machine config is not an object")
            metadata = config.get("metadata")
            if metadata is not None:
                if not isinstance(metadata, dict):
                    raise _invalid("machine metadata is not an object")
                run_id = metadata.get("ateles_run_id")
                if run_id is not None and not isinstance(run_id, str):
                    raise _invalid("machine run id is not a string")
        return data

    async def create(self, body: dict) -> dict:
        return self._machine(await self._call("POST", "/machines", body=body),
                             require_state=False)  # fmt: skip

    async def get(self, machine_id: str) -> dict | None:
        """The machine, fully validated, or None ONLY for an explicit 404."""
        try:
            data = await self._call("GET", f"/machines/{machine_id}")
        except FlyApiError as exc:
            if exc.status == 404:
                return None
            raise
        machine = validate("machine", data)
        if machine["id"] != machine_id:
            raise FlyInvalid("machine", "id does not match the request")
        return machine

    async def update(self, machine_id: str, body: dict, *, nonce: str = "") -> dict:
        """Update a machine's config; the answer must be THAT machine."""
        data = await self._call("POST", f"/machines/{machine_id}", body=body, nonce=nonce)
        return self._machine(data, expect_id=machine_id, require_state=False)

    async def start(self, machine_id: str, *, nonce: str = "") -> None:
        await self._call("POST", f"/machines/{machine_id}/start", parse=False, nonce=nonce)

    async def stop(self, machine_id: str, *, nonce: str = "") -> None:
        try:
            await self._call("POST", f"/machines/{machine_id}/stop", parse=False, nonce=nonce)
        except FlyApiError as exc:
            if exc.status != 404:
                raise

    async def acquire_lease(self, machine_id: str, *, ttl: int,
                            description: str) -> tuple[str, float | None]:  # fmt: skip
        """Take the machine's lease. Returns ``(nonce, expires_at)``.

        Documented: a lease is "an exclusive lock on modifying a Machine",
        taken with a ``ttl``; later calls send the nonce header. NOT
        documented: the status of a conflict. Only a 2xx carrying a nonce is
        success; a 409 is ``FlyLeaseBusy``; anything else raises."""
        try:
            data = await self._call(
                "POST", f"/machines/{machine_id}/lease",
                body={"ttl": int(ttl), "description": description},
            )  # fmt: skip
        except FlyApiError as exc:
            if exc.status == 409:
                raise FlyLeaseBusy(409, "machine is leased by another holder") from None
            raise
        if not isinstance(data, dict) or "data" not in data:
            raise FlyInvalid("lease_grant", "no data object")
        grant = validate("lease_grant", data["data"])
        _LEASE_NONCES.add(grant["nonce"])
        return grant["nonce"], float(grant["expires_at"])

    async def release_lease(self, machine_id: str, nonce: str) -> None:
        await self._call("DELETE", f"/machines/{machine_id}/lease", parse=False, nonce=nonce)

    async def get_lease(self, machine_id: str) -> dict | None:
        """The current lease's public fields, or None ONLY for an explicit 404."""
        try:
            data = await self._call("GET", f"/machines/{machine_id}/lease")
        except FlyApiError as exc:
            if exc.status == 404:
                return None
            raise
        if not isinstance(data, dict) or "data" not in data:
            raise FlyInvalid("lease_view", "no data object")
        return validate("lease_view", data["data"])

    async def destroy(self, machine_id: str) -> None:
        try:
            await self._call("DELETE", f"/machines/{machine_id}", params={"force": "true"},
                             parse=False)  # fmt: skip
        except FlyApiError as exc:
            if exc.status != 404:
                raise

    async def list(self) -> list[dict]:
        data = await self._call("GET", "/machines")
        if not isinstance(data, list):
            raise _invalid("machine listing is not a list")
        machines = [validate("list_entry", m) for m in data]
        ids = [m["id"] for m in machines]
        if len(ids) != len(set(ids)):
            raise _invalid("machine listing repeats an id")
        return machines

    async def wait(self, machine_id: str, *, state: str, instance_id: str = "",
                   timeout_s: int = 60) -> str:  # fmt: skip
        """One wait call. Returns ``"reached"``, ``"gone"`` or ``"timeout"``.
        Callers never treat this as confirmation; they read the state back."""
        params: dict[str, Any] = {"state": state, "timeout": max(1, min(60, timeout_s))}
        if instance_id:
            params["instance_id"] = instance_id
        try:
            await self._call("GET", f"/machines/{machine_id}/wait", params=params, parse=False)
            return "reached"
        except FlyApiError as exc:
            if exc.status == 404:
                return "gone"
            if exc.status == 408:
                return "timeout"
            machine = await self.get(machine_id)
            if machine is None or machine.get("state") == "destroyed":
                return "gone"
            if machine.get("state") == state:
                return "reached"
            return "timeout"


# ── GitHub App installation token, minted and revoked per job ─────────────────


def _github_request(method: str, path: str, bearer: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{GITHUB_API}{path}",
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {bearer}",
            "User-Agent": "ateles-fly-runner/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{method} {path} -> HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {path} -> {exc.reason}") from None
    return json.loads(raw or b"{}")


def mint_job_github_token(repository: str, prefix: str) -> str:
    """A FRESH installation token for this one job, scoped to *repository*.

    Deliberately not ``github_app_token.installation_token``: that one is
    cached and shared by the whole process, and this token is revoked when
    the job ends. Raises ``FlyIneligible`` when the App is unconfigured or the
    mint fails; never falls back to a PAT.
    """
    from lib import github_app_token as gat

    if not repository or "/" not in repository:
        raise FlyIneligible("no owner/name repository to scope the job's GitHub token to")
    if not gat.app_configured(prefix):
        raise FlyIneligible(
            f"GitHub App not configured ({prefix}_ID and a private key); a Fly job "
            "never falls back to a PAT"
        )
    try:
        jwt = gat.mint_app_jwt(prefix)
        installation = gat._resolve_installation_id(prefix, repository, lambda: jwt)
        payload = _github_request(
            "POST",
            f"/app/installations/{installation}/access_tokens",
            jwt,
            {"repositories": [repository.split("/", 1)[1]]},
        )
    except Exception as exc:  # noqa: BLE001 — the message carries no credential
        raise FlyIneligible(f"GitHub App token mint failed: {exc}") from None
    token = str(payload.get("token") or "")
    if not token:
        raise FlyIneligible("GitHub App token mint returned no token")
    return token


def revoke_job_github_token(token: str) -> None:
    """``DELETE /installation/token``, authenticated with the token itself."""
    _github_request("DELETE", "/installation/token", token)


# ── Delivery token: one repository, contents and pull requests only ─────────
#
# A Fly job that delivers a branch and a pull request gets its own token from
# the PR-opener App, requested with exactly these permissions for exactly one
# repository, and checked against what GitHub actually granted. It is never
# the read-class token above, never the swarm App's and never a PAT. GitHub
# fixes an installation token's life at one hour; Apis revokes it when the job
# ends, and a delivery whose run could outlive it stays on the Mac.

DELIVERY_TOKEN_PERMISSIONS: dict[str, str] = {"contents": "write", "pull_requests": "write"}
# GitHub adds read access to repository metadata to every installation token.
DELIVERY_TOKEN_IMPLICIT_PERMISSIONS: dict[str, str] = {"metadata": "read"}
# Time after the CLI for the runner's push and pull request steps.
DELIVERY_RESERVE_SECONDS = 300
# GitHub's fixed installation token life, and a margin for clock skew.
GITHUB_TOKEN_LIFETIME_SECONDS = 3600
TOKEN_EXPIRY_MARGIN_SECONDS = 60
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}", re.ASCII)


@dataclass(frozen=True)
class DeliveryToken:
    token: str = field(repr=False)
    expires_at: float
    repository: str


def delivery_run_seconds(timeout: float) -> float:
    """How long a delivering job may run, start to hard deadline: the CLI's
    timeout, the delivery steps after it and the usual deadline grace."""
    return float(timeout) + DELIVERY_RESERVE_SECONDS + APIS_DEADLINE_GRACE_SECONDS


def delivery_fits_token_life(timeout: float, expires_at: float, now: float) -> bool:
    """True when the token outlives the job's hard deadline, with margin."""
    return expires_at - now >= delivery_run_seconds(timeout) + TOKEN_EXPIRY_MARGIN_SECONDS


def _delivery_repository(repository: str) -> str:
    if not isinstance(repository, str) or not _REPOSITORY_RE.fullmatch(repository):
        raise FlyIneligible("a delivery needs exactly one owner/name repository")
    if repository.split("/", 1)[1] in (".", ".."):
        raise FlyIneligible("a delivery needs exactly one owner/name repository")
    return repository


def check_delivery_token_response(payload: Any, repository: str, now: float) -> tuple[str, float]:
    """``(token, expires_at)`` when GitHub granted exactly what a delivery asked
    for; ``FlyIneligible`` naming the mismatch (never a value) otherwise."""
    if not isinstance(payload, dict):
        raise FlyIneligible("the delivery token response is not an object")
    token = payload.get("token")
    if not isinstance(token, str) or not token.strip():
        raise FlyIneligible("the delivery token response carries no token")
    granted = payload.get("permissions")
    if not isinstance(granted, dict):
        raise FlyIneligible("the delivery token response does not list its permissions")
    expected = dict(DELIVERY_TOKEN_PERMISSIONS)
    for name, level in DELIVERY_TOKEN_IMPLICIT_PERMISSIONS.items():
        if name in granted:
            expected[name] = level
    if granted != expected:
        extra = sorted(set(granted) - set(expected))
        wrong = sorted(n for n in expected if granted.get(n) != expected[n])
        raise FlyIneligible(
            "the delivery token's permissions differ from the request"
            + (f"; not requested: {extra}" if extra else "")
            + (f"; missing or different: {wrong}" if wrong else ""))
    if payload.get("repository_selection") != "selected":
        raise FlyIneligible("the delivery token is not limited to selected repositories")
    repos = payload.get("repositories")
    names = [r.get("full_name") for r in repos if isinstance(r, dict)] if isinstance(repos, list) else []
    if not isinstance(repos, list) or len(repos) != 1 or len(names) != 1 \
            or not isinstance(names[0], str) or names[0].lower() != repository.lower():  # fmt: skip
        raise FlyIneligible("the delivery token does not cover exactly the one repository")
    raw = payload.get("expires_at")
    try:
        expires = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if expires.tzinfo is None:
            raise ValueError
        expires_at = expires.timestamp()
    except (TypeError, ValueError):
        raise FlyIneligible("the delivery token has no readable expiry") from None
    if not math.isfinite(expires_at) or expires_at <= now:
        raise FlyIneligible("the delivery token is already expired")
    if expires_at - now > GITHUB_TOKEN_LIFETIME_SECONDS + TOKEN_EXPIRY_MARGIN_SECONDS:
        raise FlyIneligible("the delivery token's expiry is later than an installation "
                            "token can live")  # fmt: skip
    return token, expires_at


def _revoke_refused_delivery_token(revoke: Callable[[str], None], token: str,
                                   validated_expiry: float | None) -> None:
    """Revoke a token that was minted and then refused, and say what is known:
    revoked (GitHub accepted the revocation), or attempted and failed. A
    failed revocation states an expiry only when one was validated."""
    try:
        revoke(token)
    except Exception:  # noqa: BLE001 — the exception may carry request detail
        if validated_expiry is None:
            log.warning("[fly] revoking a refused delivery token was attempted and failed; "
                        "revoke it manually if it is still live; its expiry was not "
                        "confirmed")  # fmt: skip
        else:
            log.warning("[fly] revoking a refused delivery token was attempted and failed; "
                        "revoke it manually if it is still live; GitHub reported its expiry "
                        f"as {_render_wall(validated_expiry)}")  # fmt: skip
    else:
        log.info("[fly] a refused delivery token was revoked")


def mint_delivery_github_token(
    repository: str,
    *,
    timeout: float,
    clock: Callable[[], float] = time.time,
    revoke: Callable[[str], None] | None = None,
) -> DeliveryToken:
    """A fresh PR-opener App token for one delivering job, or ``FlyIneligible``.

    Refused before minting when the job could outlive any installation token;
    after minting, the response is checked (permissions, one repository,
    expiry) and a token that does not match is revoked before the refusal.
    Never the read-class App, never a cached token, never a PAT."""
    from lib import github_app_token as gat

    repository = _delivery_repository(repository)
    if not _finite_number(timeout, 1, MAX_JOB_TIMEOUT_SECONDS):
        raise FlyIneligible("the delivery timeout is not a valid number of seconds")
    if not delivery_fits_token_life(timeout, clock() + GITHUB_TOKEN_LIFETIME_SECONDS, clock()):
        raise FlyIneligible(
            f"a delivery with a {int(timeout)}s timeout could outlive its GitHub token "
            f"({GITHUB_TOKEN_LIFETIME_SECONDS}s); it stays on the Mac")
    prefix = gat.pr_opener_prefix()
    if not gat.app_configured(prefix):
        raise FlyIneligible(
            f"the PR-opener App is not configured ({prefix}_ID and a private key); a "
            "delivery never uses another App or a PAT")
    try:
        jwt = gat.mint_app_jwt(prefix)
        installation = gat._resolve_installation_id(prefix, repository, lambda: jwt)
        payload = _github_request(
            "POST", f"/app/installations/{installation}/access_tokens", jwt,
            {"repositories": [repository.split("/", 1)[1]],
             "permissions": dict(DELIVERY_TOKEN_PERMISSIONS)},
        )  # fmt: skip
    except Exception as exc:  # noqa: BLE001 — the message carries no credential
        raise FlyIneligible(f"the delivery token mint failed: {exc}") from None
    revoke = revoke or revoke_job_github_token
    # Set only once the expiry has been read and checked to be within an
    # installation token's life; only then may a log line state it.
    validated_expiry: float | None = None
    try:
        now = clock()
        token, expires_at = check_delivery_token_response(payload, repository, now)
        validated_expiry = expires_at
        if not delivery_fits_token_life(timeout, expires_at, now):
            raise FlyIneligible(
                f"the delivery token expires before a {int(timeout)}s job would end; "
                "it stays on the Mac")
    except FlyIneligible:
        minted = payload.get("token") if isinstance(payload, dict) else None
        if isinstance(minted, str) and minted.strip():
            _revoke_refused_delivery_token(revoke, minted, validated_expiry)
        raise
    return DeliveryToken(token=token, expires_at=expires_at, repository=repository)


# ── Neotoma result record (read on the Mac with Apis's own access) ───────────


class FlyResultUnreadable(Exception):
    """The result record could not be read or was malformed: the run's
    outcome is unknown, which is different from a record that is absent."""


async def read_result_record(run_id: str, *, transport: httpx.AsyncBaseTransport | None = None,
                             attempts: int = 3, delay: float = 2.0) -> dict | None:  # fmt: skip
    """The run's result record; None when a well-formed answer says there is
    none. Raises ``FlyResultUnreadable`` when every attempt failed or came
    back malformed. Either way the caller treats the outcome as unknown."""
    base = os.environ.get("NEOTOMA_BASE_URL", "").strip().rstrip("/")
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    if not base or not token:
        log.warning("[fly] cannot read the result record: Neotoma access unset on Apis")
        return None
    body = {
        "entity_type": RESULT_ENTITY_TYPE,
        "limit": 5,
        "include_snapshots": True,
        "snapshot_filters": {"run_id": {"op": "eq", "value": run_id}},
    }
    well_formed_empty = False
    for attempt in range(attempts):
        try:
            async with httpx.AsyncClient(
                timeout=30.0, transport=transport,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            ) as client:  # fmt: skip
                resp = await client.post(f"{base}/entities/query", json=body)
            if resp.status_code != 200:
                raise ValueError(f"HTTP {resp.status_code}")
            data = resp.json()
            rows = data.get("entities") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                raise ValueError("result query answer is not an entity page")
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("result query row is not an object")
                snap = row.get("snapshot")
                if isinstance(snap, dict) and isinstance(snap.get("snapshot"), dict):
                    snap = snap["snapshot"]
                if isinstance(snap, dict) and snap.get("run_id") == run_id:
                    return snap
            well_formed_empty = True
        except (httpx.HTTPError, ValueError) as exc:
            log.debug(f"[fly] result read attempt {attempt + 1} failed: {type(exc).__name__}")
        if attempt + 1 < attempts:
            await asyncio.sleep(delay)
    if well_formed_empty:
        return None
    raise FlyResultUnreadable(f"result record for run {run_id} could not be read")


DELIVERY_STATUSES = frozenset({"opened", "updated", "no_changes", "clone_failed",
                               "not_delivered", "push_failed", "pr_failed", "no_time"})


def delivery_result_from_record(record: dict) -> dict:
    """A delivering job's six result fields, type-checked. A record without a
    recognised ``delivery_status`` (for example, fields the record store does
    not hold yet) reads as ``unreported``: never as a delivery outcome."""
    status = record.get("delivery_status")
    out = {"delivery_status": status if isinstance(status, str) and status in DELIVERY_STATUSES else "unreported"}
    for name in ("delivery_reason", "delivery_branch", "delivery_commit", "pr_url"):
        value = record.get(name)
        out[name] = value if isinstance(value, str) else ""
    number = record.get("pr_number")
    out["pr_number"] = number if isinstance(number, int) and not isinstance(number, bool) \
        and number > 0 else None  # fmt: skip
    return out


# ── Job assembly ─────────────────────────────────────────────────────────────


@dataclass
class FlyJob:
    run_id: str
    role: str
    provider: str
    repository: str
    argv: list[str]
    stdin: bytes
    timeout: int
    needs_github: bool
    # name -> value. Never logged; only written into machine config files.
    secrets: dict[str, str] = field(repr=False, default_factory=dict)
    child_credential_names: list[str] = field(default_factory=list)
    neotoma_token_name: str = ""
    neotoma_jwk_name: str = ""
    neotoma_base_url: str = ""
    neotoma_tools_unavailable: bool = False
    probe_env_names: list[str] = field(default_factory=list)
    # Claude's system prompt, shipped as a job file instead of an argument.
    system_prompt: str = field(repr=False, default="")
    # Set by the executor: the machine the job ran on, and whether the
    # runner kept only the tail of an oversized stdout.
    machine_id: str = ""
    stdout_truncated: bool = False
    # "terminated" or "unconfirmed" once the executor tried to settle a
    # machine for this job; and whether the job's outcome is unknown (a
    # lost wait or a missing result record).
    remote_state: str = ""
    outcome_unknown: bool = False
    # Set the moment a create/start request is sent: from then on the job
    # may have run, so any failure surfaces as an unknown outcome.
    launch_requested: bool = False
    # A builder's delivery (branch, base_branch, author_name, author_email):
    # the job runner clones, runs the CLI in the checkout, then pushes and
    # opens the pull request with a delivery token minted at launch.
    delivery: dict | None = None
    # What the record said about the delivery (DELIVERY_RESULT_FIELDS), once read.
    delivery_result: dict | None = None

    @property
    def login(self) -> bool:
        return self.provider in LOGIN_PROVIDERS


def login_machine_setting(provider: str) -> str:
    """The setting that names *provider*'s login machine."""
    return f"APIS_FLY_{provider.upper()}_MACHINE_ID"


def runner_argv(provider: str, cmd: list[str], mcp_config_path: str | None) -> list[str]:
    """The local argv rewritten for the runner image: the binary by name, and
    the Mac's temporary MCP config path (which may name the shared bearer)
    replaced by a placeholder ``run_job.py`` fills from the role's own token."""
    argv = [RUNNER_BINARIES[provider], *cmd[1:]]
    if mcp_config_path:
        argv = [MCP_CONFIG_PLACEHOLDER if a == mcp_config_path else a for a in argv]
    return argv


def externalize_system_prompt(provider: str, argv: list[str]) -> tuple[list[str], str]:
    """Claude's ``--append-system-prompt <text>`` rewritten to
    ``--append-system-prompt-file <placeholder>``, and the text, which is
    shipped as a job file: the prompt is often over 100 KB and must not be a
    single argument on Linux. Other providers are returned unchanged."""
    if provider != "claude" or "--append-system-prompt" not in argv:
        return argv, ""
    i = argv.index("--append-system-prompt")
    if i + 1 >= len(argv) or "--append-system-prompt" in argv[i + 1:]:
        raise FlyIneligible("the claude command's system prompt flag is malformed")
    text = argv[i + 1]
    return [*argv[:i], "--append-system-prompt-file", SYSTEM_PROMPT_PLACEHOLDER,
            *argv[i + 2:]], text  # fmt: skip


def role_jwk_path(role: str) -> Path | None:
    keys_dir = os.environ.get("ATELES_PRIVATE_KEYS_DIR", "").strip()
    if not keys_dir:
        return None
    path = Path(keys_dir) / f"{role}.jwk.json"
    return path if path.is_file() else None


def _validated_role_jwk(role: str, jwk: Any) -> bool:
    """Check the role's P-256 private key before it can be sent to a runner."""
    if not isinstance(jwk, dict) or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        return False
    if jwk.get("sub") != f"{role}@ateles-swarm":
        return False
    coordinates = []
    for name in ("x", "y", "d"):
        value = jwk.get(name)
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            return False
        try:
            raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_",
                                   validate=True)  # fmt: skip
        except ValueError:
            return False
        if not raw:
            return False
        coordinates.append(int.from_bytes(raw, "big"))
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError:
        raise FlyIneligible("cannot verify the role key: the cryptography package is unavailable") from None
    try:
        public = ec.derive_private_key(coordinates[2], ec.SECP256R1()).public_key().public_numbers()
    except ValueError:
        return False
    return (public.x, public.y) == tuple(coordinates[:2])


def build_job(
    *,
    provider: str,
    role: str,
    cmd: list[str],
    stdin: bytes,
    timeout: int,
    repository: str,
    github_requested: bool,
    mcp_config_path: str | None,
    local_review: bool = False,
    inference_only: bool = False,
    command_wrapper: list[str] | None = None,
    cwd: str | None = None,
    owns_pending_gate: bool = False,
    codex_outer_sandboxed: bool = False,
    config: FlyConfig | None = None,
    delivery: dict | None = None,
    neotoma_tools_granted: bool = False,
) -> FlyJob:
    """Check eligibility and assemble the job, minus the GitHub token (which
    is minted at launch). Raises ``FlyIneligible`` with the reason.

    ``delivery``: a builder's delivery spec (``DELIVERY_SPEC_KEYS``). The job
    then carries no read-class token; a delivery token for the one repository
    is minted at launch instead, and never reaches the CLI."""
    from skill_runner import neotoma_token_env_name

    config = config or FlyConfig.from_env()
    missing = config.missing()
    if missing:
        raise FlyIneligible("Fly runner not configured: " + ", ".join(missing) + " unset")
    # Configured but with an invalid limit: Fly is disabled, and this is a
    # refusal (no local fallback), not an ineligibility.
    config.require_valid_limits()
    if not _finite_number(timeout, 1, MAX_JOB_TIMEOUT_SECONDS):
        raise FlyConfigInvalid(
            f"the dispatch timeout is outside 1 to {MAX_JOB_TIMEOUT_SECONDS} seconds")
    if provider not in RUNNER_BINARIES:
        raise FlyIneligible(f"provider {provider!r} has no Fly runner")
    for flag, why in (
        (local_review, "local_review runs stay on the Mac"),
        (inference_only, "inference_only runs stay on the Mac"),
        (bool(command_wrapper), "a command_wrapper (macOS sandbox) cannot run on Fly"),
        (codex_outer_sandboxed, "an outer-sandboxed Codex run cannot run on Fly"),
        (bool(cwd), "a dispatch with a working directory needs a local worktree"),
        (owns_pending_gate, "seated and gate-owning runs stay on the Mac"),
    ):
        if flag:
            raise FlyIneligible(why)
    if provider in LOGIN_PROVIDERS and provider not in config.login_machines:
        raise FlyIneligible(
            f"no {provider} login machine configured ({login_machine_setting(provider)})"
        )

    secrets: dict[str, str] = {}
    shared_bearer = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    token_name = neotoma_token_env_name(role)
    own_token = os.environ.get(token_name, "").strip()
    token_is_shared = bool(own_token and shared_bearer and own_token == shared_bearer.strip())
    if token_is_shared:
        own_token = ""  # The shared daemon bearer must never enter a Fly job.
    shared_token_without_key = (
        f"{token_name} equals the shared daemon bearer and no valid AAuth key exists for {role!r}"
    )
    jwk_path = role_jwk_path(role)
    jwk_text = ""
    if jwk_path is not None:
        try:
            jwk_text = jwk_path.read_text(encoding="utf-8").strip()
            jwk = json.loads(jwk_text)
        except (OSError, ValueError):
            if token_is_shared:
                raise FlyIneligible(shared_token_without_key) from None
            raise FlyIneligible(f"the AAuth key file for {role!r} is unreadable") from None
        if not isinstance(jwk, dict):
            if token_is_shared:
                raise FlyIneligible(shared_token_without_key)
            raise FlyIneligible(f"the AAuth key file for {role!r} is unreadable")
        if not _validated_role_jwk(role, jwk):
            if token_is_shared:
                raise FlyIneligible(shared_token_without_key)
            raise FlyIneligible(f"the AAuth key file for {role!r} is not that role's private key")
    if not own_token and not jwk_text:
        if token_is_shared:
            raise FlyIneligible(shared_token_without_key)
        raise FlyIneligible(
            f"role {role!r} has no Neotoma identity of its own ({token_name} or an AAuth "
            "key); the shared daemon bearer never leaves the Mac"
        )
    if jwk_text:
        secrets[AAUTH_JWK_JOB_NAME] = jwk_text
    if own_token:
        secrets[token_name] = own_token

    child_names: list[str] = []
    if provider == "claude" and provider in LOGIN_PROVIDERS:
        # The token is NOT read here and never shipped: it is on the login
        # machine's volume. The child is declared to receive it; run_job.py
        # supplies it from CLAUDE_TOKEN_FILE.
        child_names.append(CLAUDE_TOKEN_NAME)

    base_url = os.environ.get("NEOTOMA_BASE_URL", "").strip().rstrip("/")
    if not base_url.startswith("https://"):
        raise FlyIneligible("NEOTOMA_BASE_URL must be an https URL for a Fly run")

    probe = list(dict.fromkeys([
        "NEOTOMA_BEARER_TOKEN", "TELEGRAM_BOT_TOKEN", "APIS_GITHUB_WEBHOOK_SECRET",
        "ATELES_AGENT_PAT", "NEOTOMA_AGENT_PAT",
        # Every other role's PAT by name, plus the operator's canary name(s).
        *sorted(n for n in os.environ if n.endswith("_AGENT_PAT")),
        *sorted(n for n in os.environ if n.endswith("_NEOTOMA_TOKEN") and n != token_name),
        *([token_name] if not own_token else []),
        *config.extra_probe_env_names,
    ]))  # fmt: skip

    argv = runner_argv(provider, cmd, mcp_config_path)
    argv, system_prompt = externalize_system_prompt(provider, argv)
    if delivery is not None:
        delivery = _check_delivery(delivery, repository=repository, stdin=stdin,
                                   github_requested=github_requested, timeout=timeout)  # fmt: skip
        argv = with_workspace(provider, argv)
    largest = max((len(a.encode("utf-8")) for a in argv), default=0)
    if largest > MAX_ARGV_ELEMENT_BYTES:
        # Refused before any token, reservation or machine.
        raise _command_too_large(provider, largest)
    return FlyJob(
        run_id=f"fly-{uuid.uuid4().hex[:16]}",
        role=role,
        provider=provider,
        repository=repository,
        argv=argv,
        stdin=stdin,
        timeout=int(timeout),
        needs_github=github_requested,
        secrets=secrets,
        child_credential_names=child_names,
        neotoma_token_name=token_name if own_token else "",
        neotoma_jwk_name=AAUTH_JWK_JOB_NAME if jwk_text else "",
        neotoma_base_url=base_url,
        neotoma_tools_unavailable=bool(neotoma_tools_granted and not own_token),
        probe_env_names=probe,
        system_prompt=system_prompt,
        delivery=delivery,
    )


def _check_delivery(delivery: Any, *, repository: str, stdin: bytes, github_requested: bool,
                    timeout: float) -> dict:  # fmt: skip
    """A delivery spec the job runner will accept, or ``FlyIneligible`` (the
    builder then runs locally, as today)."""
    if not isinstance(delivery, dict) or tuple(sorted(delivery)) != tuple(sorted(DELIVERY_SPEC_KEYS)):
        raise FlyIneligible("the delivery spec is malformed")
    if not all(isinstance(delivery[k], str) and delivery[k] for k in DELIVERY_SPEC_KEYS):
        raise FlyIneligible("the delivery spec has an empty field")
    _delivery_repository(repository)
    if github_requested:
        raise FlyIneligible("a delivering job carries no read-class GitHub token")
    if not isinstance(stdin, bytes) or PR_FILE_PLACEHOLDER.encode() not in stdin:
        raise FlyIneligible("the delivery prompt is not the Fly variant")
    now = time.time()
    if not delivery_fits_token_life(timeout, now + GITHUB_TOKEN_LIFETIME_SECONDS, now):
        raise FlyIneligible(
            f"a delivery with a {int(timeout)}s timeout could outlive its GitHub token")
    return dict(delivery)


def with_workspace(provider: str, argv: list[str]) -> list[str]:
    """The CLI's workspace flag naming the job's checkout (filled in on the
    machine): Codex ``--cd`` before its prompt-from-stdin marker, Cursor
    ``--workspace``. Claude takes its working directory, which the runner sets."""
    if provider == "codex":
        if argv[-1:] != ["-"] or "--cd" in argv:
            raise FlyIneligible("the codex command is not the expected shape for a delivery")
        return [*argv[:-1], "--cd", WORKSPACE_PLACEHOLDER, "-"]
    if provider == "cursor":
        if "--workspace" in argv:
            raise FlyIneligible("the cursor command already names a workspace")
        return [*argv, "--workspace", WORKSPACE_PLACEHOLDER]
    return argv


def _b64(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.b64encode(data).decode("ascii")


def job_files(job: FlyJob, config: FlyConfig) -> list[dict]:
    """The machine-level files: ``/run/job/{job.json,argv.json,stdin,secrets/*}``."""
    spec = {
        "run_id": job.run_id,
        "role": job.role,
        "provider": job.provider,
        "repository": job.repository,
        "timeout_seconds": job.timeout,
        "credential_names": sorted(job.secrets),
        "child_credential_names": list(job.child_credential_names),
        "neotoma_token_name": job.neotoma_token_name,
        "neotoma_jwk_name": job.neotoma_jwk_name,
        "neotoma_base_url": job.neotoma_base_url,
        "probe_env_names": job.probe_env_names,
        **({"delivery": job.delivery} if job.delivery is not None else {}),
    }
    if job.provider == "claude" and job.login:
        spec["claude_token_file"] = CLAUDE_TOKEN_FILE
    if job.provider == "codex":
        spec["codex_home"] = "/data/codex"
    if job.provider == "cursor":
        spec["login_home_links"] = {
            rel: f"/data/cursor/{rel}" for rel in config.cursor_login_dirs
        }
    files = [
        {"guest_path": f"{JOB_DIR}/job.json", "raw_value": _b64(json.dumps(spec))},
        {"guest_path": f"{JOB_DIR}/argv.json", "raw_value": _b64(json.dumps(job.argv))},
        {"guest_path": f"{JOB_DIR}/stdin", "raw_value": _b64(job.stdin or b"")},
    ]
    if job.system_prompt:
        files.append({"guest_path": f"{JOB_DIR}/{SYSTEM_PROMPT_JOB_FILE}",
                      "raw_value": _b64(job.system_prompt.encode("utf-8"))})  # fmt: skip
    for name, value in sorted(job.secrets.items()):
        files.append({"guest_path": f"{JOB_DIR}/secrets/{name}", "raw_value": _b64(value)})
    return files


def machine_env(job: FlyJob, deadline: float) -> dict[str, str]:
    """PID 1's environment: non-secret settings only."""
    return {
        "ATELES_RUN_ID": job.run_id,
        "ATELES_RUN_ROLE": job.role,
        "ATELES_RUN_PROVIDER": job.provider,
        "ATELES_RUN_REPOSITORY": job.repository,
        "ATELES_RUN_DEADLINE": datetime.fromtimestamp(deadline, tz=timezone.utc).isoformat(),
    }


def per_job_machine_body(job: FlyJob, config: FlyConfig, deadline: float) -> dict:
    return {
        "region": config.region,
        "config": {
            "image": config.image,
            "auto_destroy": True,
            "restart": {"policy": "no"},
            "guest": {"cpu_kind": "shared", "cpus": 2, "memory_mb": 2048},
            "env": machine_env(job, deadline),
            "files": job_files(job, config),
            "metadata": {"ateles_run_id": job.run_id, "ateles_role": job.role},
        },
    }


# ── Executor ─────────────────────────────────────────────────────────────────

_LOGIN_LOCKS: dict[str, asyncio.Lock] = {}


def _login_lock(provider: str) -> asyncio.Lock:
    lock = _LOGIN_LOCKS.get(provider)
    if lock is None:
        lock = _LOGIN_LOCKS[provider] = asyncio.Lock()
    return lock


TRUNCATION_MARKER = (
    "[fly runner: output truncated; only the last {cap} bytes of stdout were kept, "
    "the beginning of the output is missing (run {run_id})]"
)
# How long past its deadline a reservation must be before the reaper may
# treat it as abandoned (a live run settles its own well before that).
REAP_GRACE_SECONDS = 300
# A live executor refreshes its owner heartbeat this often while it runs.
HEARTBEAT_INTERVAL_SECONDS = 30


class FlyUncertainLaunch(Exception):
    """A create or start request was sent and its outcome is unknown: the
    job may have run. Never retried locally. ``job.remote_state`` says whether
    the machine was then confirmed terminated."""


def _definitely_not_executed(exc: BaseException) -> bool:
    """True only when a create/start request provably did not take effect:
    the connection was never made, or Fly rejected the request outright (a
    4xx other than a timeout or conflict). Everything else is uncertain."""
    if isinstance(exc, httpx.ConnectError):
        return True
    if isinstance(exc, FlyApiError):
        return 400 <= exc.status < 500 and exc.status not in (408, 409)
    return False


class _TokenOwner:
    """Owns every token minted for one job, across cancellation.

    The minting thread registers its token itself, so a cancelled dispatch
    cannot lose it. While the job is live, tokens are collected for ``run()``
    to revoke; once ``run()`` has closed the owner, a late token is revoked
    immediately in the thread that minted it.
    """

    def __init__(self, revoke: Callable[[str], None]):
        self._revoke = revoke
        self._lock = threading.Lock()
        self._tokens: list[str] = []
        self._closed = False

    def add(self, token: str) -> None:
        with self._lock:
            if not self._closed:
                self._tokens.append(token)
                return
        try:
            self._revoke(token)
        except Exception as exc:  # noqa: BLE001 — best-effort; the token expires anyway
            log.warning(f"[fly] late GitHub job token revocation failed: {type(exc).__name__}")

    def close(self) -> list[str]:
        with self._lock:
            self._closed = True
            out, self._tokens = self._tokens, []
            return out


@dataclass
class _LaunchState:
    reserved: bool = False
    requested: bool = False  # a create/start was sent; its effect is not disproven
    config_written: bool = False  # a login machine's stored config holds job files
    machine_id: str = ""
    lock: asyncio.Lock | None = None
    lease_nonce: str = field(default="", repr=False)  # a secret: never logged


@dataclass
class FlyHandle:
    job: FlyJob
    machine_id: str
    instance_id: str
    started: float
    deadline: float


class FlyExecutor:
    """Runs one job on Fly. ``run()`` is the only entry point a dispatcher
    uses, and it owns the job's whole lifetime.

    Ownership model:

    * Every reservation is written, keyed by run id and owned by this
      process instance (``instance_id``), BEFORE any Fly call, under the
      admission lock that the reaper also holds. A live ``run()`` lists its
      run id in ``_active``; nothing else in this process touches it.
    * The reaper acts only on reservations no live run holds: one from a
      previous Apis process, one marked unconfirmed, or one past its deadline
      plus ``REAP_GRACE_SECONDS``. A machine running that no reservation
      accounts for is first recorded as an owned, unconfirmed reservation, so
      it counts against capacity until its termination is confirmed.
    * Every release goes through ``_settle()``: confirm termination by
      reading state back, then charge runtime once and release; otherwise
      keep the reservation, marked unconfirmed.
    * Every minted GitHub token is owned by a ``_TokenOwner`` and revoked,
      including one whose mint completes after the dispatch was cancelled.
    * A launch whose outcome is uncertain raises ``FlyUncertainLaunch`` and is
      never replayed locally; only a launch that provably did not run raises
      ``FlyLaunchError``.
    """

    def __init__(
        self,
        config: FlyConfig,
        *,
        client: FlyMachinesClient | None = None,
        ledger: FlyLedger | None = None,
        mint: Callable[[str, str], str] = mint_job_github_token,
        revoke: Callable[[str], None] = revoke_job_github_token,
        read_result: Callable[[str], Any] = read_result_record,
        clock: Callable[[], float] = time.time,
        mint_delivery: Callable[..., "DeliveryToken"] = mint_delivery_github_token,
    ):
        self.config = config
        self._mint_delivery = mint_delivery
        self.client = client or FlyMachinesClient(config)
        self.ledger = ledger or FlyLedger(config.ledger_path)
        self._mint = mint
        self._revoke = revoke
        self._read_result = read_result
        self._clock = clock
        self._last_reap = 0.0
        self.instance_id = f"apis-{uuid.uuid4().hex[:16]}"
        if self.ledger.owner is None:
            self.ledger.owner = this_process_owner(self.instance_id)
            self.ledger._clock = clock
        else:
            self.instance_id = self.ledger.owner["id"]
        self._active: set[str] = set()
        # Machines THIS process launched, by run id: the machine id from the
        # validated create answer (or the configured login machine) and the
        # lease nonce this process holds. Recorded before any ledger write,
        # so stopping them never depends on the ledger being readable.
        self._launched: dict[str, dict] = {}
        self._admission_async: asyncio.Lock | None = None
        self._admission_loop: Any = None

    def _admission_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._admission_async is None or self._admission_loop is not loop:
            self._admission_async, self._admission_loop = asyncio.Lock(), loop
        return self._admission_async

    @contextlib.asynccontextmanager
    async def _admission(self):
        """Admission and reaping are serialised across coroutines in this
        process AND across processes sharing the ledger (an flock on a
        dedicated file, polled without blocking so the event loop never
        waits behind another process)."""
        async with self._admission_lock():
            path = self.ledger.path.with_name(self.ledger.path.name + ".admission.lock")
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(path, "a+")
            try:
                # Non-blocking attempts with a short sleep: a cancelled waiter
                # leaves no thread stuck inside flock().
                while True:
                    try:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        await asyncio.sleep(0.05)
                try:
                    yield
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            try:
                await asyncio.to_thread(self.ledger.heartbeat)
            except Exception as exc:  # noqa: BLE001 — a missed beat is not fatal
                log.warning(f"[fly] heartbeat failed: {type(exc).__name__}")

    # ── The job lifetime ─────────────────────────────────────────────────────

    async def run(self, job: FlyJob) -> tuple[int, bytes, bytes]:
        """Launch, wait, collect and settle one job.

        Raises ``FlyIneligible`` / ``FlyRefusal`` / ``FlyLaunchError`` when no
        job ran (the caller may run it locally), ``FlyUncertainLaunch`` when
        it may have run (the caller must not replay it), and
        ``asyncio.TimeoutError`` at the Apis deadline. Otherwise returns
        ``(returncode, stdout, stderr)``; ``job.remote_state`` then says
        whether the machine was confirmed terminated.
        """
        # Before any credential, ledger write or machine call.
        self.config.require_valid_limits()
        tokens = _TokenOwner(self._revoke)
        launch = _LaunchState()
        self._active.add(job.run_id)
        beat = asyncio.ensure_future(self._heartbeat_loop())
        try:
            try:
                handle = await self._launch(job, tokens, launch)
                return await self._collect(handle)
            finally:
                try:
                    if launch.reserved and launch.requested:
                        confirmed = await self._settle(job.run_id)
                        job.remote_state = "terminated" if confirmed else "unconfirmed"
                    elif launch.reserved:
                        await self._release_unstarted(job.run_id, launch)
                        self._launched.pop(job.run_id, None)  # provably never started
                finally:
                    self._active.discard(job.run_id)
                    beat.cancel()
                    if launch.lock is not None and launch.lock.locked():
                        launch.lock.release()
        finally:
            for token in tokens.close():
                await self._revoke_shielded(token)
            job.secrets.clear()

    async def _mint_owned(self, job: FlyJob, tokens: _TokenOwner) -> str:
        def mint_and_register() -> str:
            token = self._mint(job.repository, self.config.github_app_prefix)
            tokens.add(token)  # registered by the minting thread itself
            return token

        return await asyncio.to_thread(mint_and_register)

    async def _mint_delivery_owned(self, job: FlyJob, tokens: _TokenOwner) -> str:
        def mint_and_register() -> str:
            minted = self._mint_delivery(job.repository, timeout=job.timeout,
                                         revoke=self._revoke)  # fmt: skip
            tokens.add(minted.token)  # revoked by run() on every exit path
            return minted.token

        return await asyncio.to_thread(mint_and_register)

    async def _revoke_shielded(self, token: str) -> None:
        """Revoke one token; a cancellation does not stop the revocation."""
        if not token:
            return
        fut = asyncio.ensure_future(asyncio.to_thread(self._revoke, token))
        try:
            await asyncio.shield(fut)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — best-effort; the token expires anyway
            log.warning(f"[fly] GitHub job token revocation failed: {type(exc).__name__}")

    async def _request(self, launch: _LaunchState, coro, job: "FlyJob | None" = None) -> Any:
        """Send a create/start. Its failure is a definite launch failure only
        when the request provably did not take effect; otherwise uncertain."""
        launch.requested = True
        if job is not None:
            job.launch_requested = True
        try:
            return await coro
        except (FlyApiError, httpx.HTTPError) as exc:
            if _definitely_not_executed(exc):
                launch.requested = False
                raise FlyLaunchError(str(exc) or type(exc).__name__) from None
            raise FlyUncertainLaunch(str(exc) or type(exc).__name__) from None

    async def _launch(self, job: FlyJob, tokens: _TokenOwner, launch: _LaunchState) -> FlyHandle:
        await self.maybe_reap()
        max_run = job.timeout + APIS_DEADLINE_GRACE_SECONDS
        if job.delivery is not None:
            # The deadline the token's life was checked against (#1469).
            max_run = int(delivery_run_seconds(job.timeout))
        if job.login:
            lock = _login_lock(job.provider)
            try:
                # FIFO: asyncio.Lock wakes waiters in arrival order. Queue time
                # does not count against the dispatch timeout.
                await asyncio.wait_for(lock.acquire(), timeout=self.config.queue_max_wait_seconds)
            except asyncio.TimeoutError:
                raise FlyRefusal(
                    f"{job.provider} login machine busy for longer than "
                    f"{self.config.queue_max_wait_seconds}s",
                    _render_wall(self._clock() + 300),
                ) from None
            launch.lock = lock
        if job.delivery is not None:
            # A refused or mismatched mint raises FlyIneligible here, before
            # any reservation or machine: the builder runs locally instead.
            job.secrets[DELIVERY_TOKEN_NAME] = await self._mint_delivery_owned(job, tokens)
        elif job.needs_github:
            token = await self._mint_owned(job, tokens)
            job.secrets["GITHUB_TOKEN"] = token
            job.secrets["GH_TOKEN"] = token
            for name in ("GITHUB_TOKEN", "GH_TOKEN"):
                if name not in job.child_credential_names:
                    job.child_credential_names.append(name)
        async with self._admission():
            # build_job refused a login provider with no machine configured.
            login_id = self.config.login_machines[job.provider] if job.login else ""
            self.ledger.reserve(self.config, run_id=job.run_id, owner=self.instance_id,
                                max_run_seconds=max_run, login=job.login, machine_id=login_id,
                                now=self._clock())  # fmt: skip
            launch.reserved = True
            launch.machine_id = login_id
            started = self._clock()
            deadline = started + max_run
            try:
                if job.login:
                    await self._reconcile_login(login_id, job.run_id)
                    # Cheap pre-check: an incomplete or busy machine is refused
                    # before even a lease is asked for.
                    pre = await self._read_login_machine(login_id, job)
                    if pre is not None:
                        pre = await self._settle_login_machine(pre)
                    if pre is None:
                        raise FlyApiError(404, f"{job.provider} login machine not found")
                    self._refuse_unless_free(pre, job)
                    nonce, expires = await self._acquire_lease(login_id, job, deadline)
                    launch.lease_nonce = nonce
                    self._launched[job.run_id] = {"machine_id": login_id, "login": True,
                                                  "nonce": nonce}  # fmt: skip
                    self.ledger.set_lease(job.run_id, nonce, expires)
                    if expires is not None:
                        # A run must never outlive its lease: its deadline is
                        # cut short of the lease's expiry, and at the deadline
                        # the machine is stopped and the run settled.
                        deadline = min(deadline, expires - LEASE_SAFETY_SECONDS)
                    await self._write_login_config(login_id, job, deadline, launch, nonce)
                    await self._request(launch, self.client.start(login_id, nonce=nonce), job)
                    machine_id, instance_id = login_id, ""
                else:
                    created = await self._request(
                        launch, self.client.create(per_job_machine_body(job, self.config, deadline)),
                        job,
                    )
                    machine_id = created["id"]
                    instance_id = str(created.get("instance_id") or "")
                    self._launched[job.run_id] = {"machine_id": machine_id, "login": False,
                                                  "nonce": ""}  # fmt: skip
                    self.ledger.set_machine(job.run_id, machine_id)
                    launch.machine_id = machine_id
            except (FlyApiError, httpx.HTTPError) as exc:
                # Before any create/start was sent: provably nothing ran.
                raise FlyLaunchError(str(exc) or type(exc).__name__) from None
        job.machine_id = machine_id
        log.info(
            f"[fly] run={job.run_id} role={job.role} provider={job.provider} "
            f"machine={machine_id} launched"
        )
        return FlyHandle(job, machine_id, instance_id, started, deadline)

    async def _read_login_machine(self, machine_id: str, job: FlyJob) -> dict | None:
        """The login machine for admission. An incomplete answer is never
        availability: it is a retry-later refusal, with nothing written."""
        try:
            return await self.client.get(machine_id)
        except FlyInvalid as exc:
            raise FlyRefusal(
                f"{job.provider} login machine is not free (its answer is incomplete: "
                f"{exc}); nothing was changed",
                _render_wall(self._clock() + 300),
            ) from None

    def _refuse_unless_free(self, machine: dict, job: FlyJob) -> None:
        refusal = _login_admission_refusal(machine, job.run_id)
        if refusal:
            raise FlyRefusal(
                f"{job.provider} login machine is not free ({refusal}); nothing was changed",
                _render_wall(self._clock() + 300),
            )

    async def _acquire_lease(self, machine_id: str, job: FlyJob,
                             deadline: float) -> tuple[str, float | None]:  # fmt: skip
        """The login machine's lease, the ownership token for this run. Busy,
        or any answer that is not a clean grant, is a retry-later refusal."""
        ttl = max(60, int(deadline - self._clock()) + REAP_GRACE_SECONDS)
        try:
            return await self.client.acquire_lease(
                machine_id, ttl=ttl, description=_lease_description(job.run_id)
            )
        except FlyLeaseBusy:
            reason = "its lease is held by another dispatcher"
        except (FlyApiError, httpx.HTTPError) as exc:
            reason = f"its lease could not be taken ({type(exc).__name__})"
        raise FlyRefusal(
            f"{job.provider} login machine is not free ({reason}); nothing was changed",
            _render_wall(self._clock() + 300),
        )

    async def _release_lease(self, machine_id: str, nonce: str, run_id: str) -> bool:
        """Release this run's lease. True only on POSITIVE evidence that this
        run no longer holds it, read back after the release attempt: a
        validated answer showing no lease (an explicit 404), or a validated
        lease whose holder identity is present and is not this run's. A
        missing or malformed holder is unknown, never "someone else"."""
        try:
            await self.client.release_lease(machine_id, nonce)
        except Exception as exc:  # noqa: BLE001 — the read-back decides
            log.warning(f"[fly] lease release on {machine_id} failed: {type(exc).__name__}")
        try:
            lease = await self.client.get_lease(machine_id)
        except Exception as exc:  # noqa: BLE001 — unknown: the run keeps its reservation
            log.warning(f"[fly] lease read-back on {machine_id} failed: {type(exc).__name__}")
            return False
        if lease is None:
            return True
        return lease["description"] != _lease_description(run_id)

    async def _settle_login_machine(self, machine: dict) -> dict | None:
        """*machine* (a validated read) once it is no longer in a transitional
        state after a config update: one bounded wait on Fly's wait endpoint
        for ``stopped`` (with the new version's instance id), then a few
        short reads. Returns the last validated read (None when the machine
        is gone); if it is STILL transitional at the bound, the caller fails
        closed on that state."""
        if machine["state"] not in LOGIN_SETTLING_STATES:
            return machine
        machine_id = machine["id"]
        try:
            await self.client.wait(machine_id, state="stopped",
                                   instance_id=machine["instance_id"],
                                   timeout_s=LOGIN_SETTLE_WAIT_SECONDS)  # fmt: skip
        except Exception as exc:  # noqa: BLE001 — the reads below decide
            log.warning(f"[fly] waiting for {machine_id} to settle failed: {type(exc).__name__}")
        for attempt in range(LOGIN_SETTLE_POLLS + 1):
            machine = await self.client.get(machine_id)
            if machine is None or machine["state"] not in LOGIN_SETTLING_STATES:
                return machine
            if attempt < LOGIN_SETTLE_POLLS:
                await asyncio.sleep(LOGIN_SETTLE_POLL_SECONDS)
        log.warning(f"[fly] login machine {machine_id} is still {machine['state']} after the "
                    "settle bound")  # fmt: skip
        return machine

    async def _write_login_config(self, machine_id: str, job: FlyJob, deadline: float,
                                  launch: "_LaunchState", nonce: str) -> None:  # fmt: skip
        """Install this run's job on a login machine, compare-and-set against
        the machine's LIVE state (the ledger is not the source of truth for
        what a machine is running: it can be lost, unshared, or on another
        host).

        Immediately before writing, under the admission lock: the machine
        must be stopped and carry no job config, or carry only this run's
        own. Otherwise (another run's id, an unattributable job, running, or
        unreadable) the launch is refused with a retry time and nothing is
        written. The update carries the machine version read here as a
        precondition, and the stored config is read back afterwards: unless
        it is this run's, the launch fails closed before any start.
        """
        machine = await self._read_login_machine(machine_id, job)  # re-read under the lease
        if machine is not None:
            machine = await self._settle_login_machine(machine)
        if machine is None:
            raise FlyApiError(404, f"{job.provider} login machine not found")
        self._refuse_unless_free(machine, job)
        cfg = copy.deepcopy(machine["config"])
        cfg["image"] = self.config.image
        cfg["auto_destroy"] = False
        cfg["restart"] = {"policy": "no"}
        cfg["env"] = machine_env(job, deadline)
        cfg["files"] = job_files(job, self.config)
        metadata = dict(cfg["metadata"]) if "metadata" in cfg else {}
        cfg["metadata"] = {**metadata, "ateles_run_id": job.run_id}
        # The version precondition is mandatory: _refuse_unless_free has
        # already refused a machine whose answer carried no usable version.
        body: dict = {"config": cfg, "skip_launch": True,
                      "current_version": machine["instance_id"]}  # fmt: skip
        launch.config_written = True  # from here the stored config may hold this job
        try:
            await self.client.update(machine_id, body, nonce=nonce)
        except FlyApiError as exc:
            if exc.status in (409, 412):
                # The machine changed between the check and the write.
                raise FlyRefusal(
                    f"{job.provider} login machine changed while being claimed; retry later",
                    _render_wall(self._clock() + 300),
                ) from None
            raise
        # Read back once the update has settled (a transitional state right
        # after the write is not a refusal); one that never settles fails
        # closed before any start.
        after = await self.client.get(machine_id)
        if after is not None:
            after = await self._settle_login_machine(after)
        if after is None or after["state"] in LOGIN_SETTLING_STATES:
            raise FlyApiError(409, "login machine did not settle to stopped after the write")
        if after["state"] != "stopped" or _config_run_id(after["config"]) != job.run_id:
            raise FlyApiError(409, "login machine config is not this run's after the write")

    async def _scrub_login_machine(self, machine_id: str, run_id: str, nonce: str = "") -> bool:
        """Remove THIS run's files from a stopped login machine's stored
        config, then read it back. True only when the config verifiably no
        longer carries this run's job.

        Compare-and-set: the config is changed only while it still carries
        this run's id. A config that already belongs to another run is never
        touched (and, holding no trace of this run, counts as clean). A
        config with job files but no run marker cannot be attributed, so it
        is left alone and the result is False. Callers hold the admission lock
        and still own the reservation, so no other dispatcher can be writing
        this machine meanwhile."""
        # Positive evidence only: every read below is fully validated (a missing
        # or null config is INVALID, never "empty"), and only an explicit 404
        # means the machine is gone.
        try:
            machine = await self.client.get(machine_id)
            if machine is not None:
                machine = await self._settle_login_machine(machine)
            if machine is None:
                return True
            cfg = copy.deepcopy(machine["config"])
            owner = _config_run_id(cfg)
            if owner and owner != run_id:
                return True  # positively another run's config: this run's job is not stored
            if not owner and _has_job_config(cfg):
                log.warning(f"[fly] login machine {machine_id} holds an unattributed job config")
                return False
            if owner == run_id:
                if machine["state"] not in LOGIN_NOT_RUNNING_STATES:
                    # Never rewrite a machine that may be running.
                    log.warning(f"[fly] login machine {machine_id} is {machine['state']}; "
                                "this run's job files were not cleared")  # fmt: skip
                    return False
                cfg["files"] = []
                cfg["env"] = {}
                if "metadata" in cfg:
                    cfg["metadata"] = {k: v for k, v in cfg["metadata"].items()
                                       if k != "ateles_run_id"}  # fmt: skip
                body: dict = {"config": cfg, "skip_launch": True,
                              "current_version": machine["instance_id"]}  # fmt: skip
                await self.client.update(machine_id, body, nonce=nonce)
            after = await self.client.get(machine_id)
            if after is not None:
                after = await self._settle_login_machine(after)
            if after is None:
                return True
            # Cleared means: a validated, present config carrying no job at all.
            if _has_job_config(after["config"]):
                log.warning(f"[fly] login machine {machine_id} still holds job files after "
                            "the clear")  # fmt: skip
                return False
            return True
        except Exception as exc:  # noqa: BLE001 — unknown state keeps the reservation
            log.warning(f"[fly] could not clear job files from login machine: {type(exc).__name__}")
            return False

    async def _release_unstarted(self, run_id: str, launch: "_LaunchState") -> None:
        """A launch that provably started nothing. If a login machine's config
        was written, clear it (verified) BEFORE releasing, under the admission
        lock; if that cannot be verified, keep the reservation unconfirmed so
        no other dispatcher can take the machine while it holds this job."""
        async with self._admission():
            if launch.config_written and launch.machine_id:
                if not await self._scrub_login_machine(launch.machine_id, run_id,
                                                       launch.lease_nonce):  # fmt: skip
                    self.ledger.mark_unconfirmed(run_id, reason="job config not cleared")
                    return
            if launch.lease_nonce and launch.machine_id:
                if not await self._release_lease(launch.machine_id, launch.lease_nonce, run_id):
                    self.ledger.mark_unconfirmed(run_id, reason="lease not released")
                    return
            self.ledger.release(run_id)

    async def _collect(self, handle: FlyHandle) -> tuple[int, bytes, bytes]:
        """Wait for the machine and read the result record. Settlement is
        ``run()``'s. A wait error is a failed result with an unknown outcome."""
        job = handle.job
        try:
            outcome = await self._wait_until_done(handle)
        except (httpx.HTTPError, FlyApiError) as exc:
            log.error(f"[fly] run={job.run_id} lost the wait on {handle.machine_id}: {exc}")
            job.outcome_unknown = True
            return 1, b"", (
                f"fly runner: error while waiting on machine {handle.machine_id} "
                f"({type(exc).__name__}); the run's outcome is unknown"
            ).encode()
        if outcome == "deadline":
            log.error(f"[fly] run={job.run_id} machine={handle.machine_id} passed its deadline")
            job.outcome_unknown = True  # the task's own result was never observed
            raise asyncio.TimeoutError()
        try:
            record = await self._read_result(job.run_id)
            if not record:
                job.outcome_unknown = True
                return 1, b"", f"fly runner: no result recorded for run {job.run_id}".encode()
            return self._result_from_record(job, record)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — unreadable or malformed: outcome unknown
            log.error(f"[fly] run={job.run_id}: result unreadable ({type(exc).__name__})")
            job.outcome_unknown = True
            return 1, b"", (
                f"fly runner: the result of run {job.run_id} could not be read "
                f"({type(exc).__name__}); the run's outcome is unknown"
            ).encode()

    def _result_from_record(self, job: FlyJob, record: dict) -> tuple[int, bytes, bytes]:
        """The CLI's result, from a VALIDATED record. A record that is not
        this run's, or whose fields have the wrong types, is unreadable."""
        try:
            record = validate("result_record", record)
        except FlyInvalid as exc:
            raise FlyResultUnreadable(str(exc)) from None
        if record["run_id"] != job.run_id:
            raise FlyResultUnreadable("result record does not belong to this run")
        rc = record["exit_code"]
        if job.delivery is not None:
            got = delivery_result_from_record(record)
            if got["delivery_status"] in {"opened", "updated"}:
                expected_url = f"https://github.com/{job.repository}/pull/{got['pr_number']}"
                if (got["pr_url"] != expected_url or got["pr_number"] is None
                        or got["delivery_branch"] != job.delivery["branch"]
                        or re.fullmatch(r"[0-9a-f]{40}", got["delivery_commit"]) is None):
                    raise FlyResultUnreadable("delivery artifact does not match its job")
            job.delivery_result = got
        if record["probe_ok"] is False:
            log.error(f"[fly] run={job.run_id} credential probe failed: {record['refused']}")
        stdout = redact_nonces(record["stdout"])
        stderr = redact_nonces(record["stderr_tail"])
        if record["stdout_truncated"]:
            # Never silent: the caller's stdout says so on its first line (a
            # verdict parsed from the LAST line is unaffected), stderr says
            # so, the job is flagged for the SkillResult and harness_event,
            # and the operator log carries a WARNING naming the run.
            job.stdout_truncated = True
            marker = TRUNCATION_MARKER.format(cap=RUNNER_STDOUT_CAP_BYTES, run_id=job.run_id)
            log.warning(f"[fly] run={job.run_id}: {marker}")
            stdout = f"{marker}\n{stdout}"
            stderr = f"{stderr}\n{marker}" if stderr else marker
        return rc, stdout.encode(), stderr.encode()

    # ── Settlement: the one release path ─────────────────────────────────────

    async def _settle(self, run_id: str) -> bool:
        """``_settle_locked`` under the admission lock (the lock a run's own
        completion, its launch failure, the reaper and login reconciliation
        all share)."""
        async with self._admission():
            return await self._settle_locked(run_id)

    async def _settle_locked(self, run_id: str) -> bool:
        """``_settle_steps``; an unusable ledger keeps the reservation as it
        is (nothing is charged or released) and reports it unconfirmed."""
        try:
            confirmed = await self._settle_steps(run_id)
        except FlyLedgerError as exc:
            log.error(f"[fly] run={run_id}: ledger unusable ({exc}); reservation kept")
            # Accounting stays frozen, but a machine this process launched is
            # still stopped (or destroyed): termination must not wait for
            # the operator's repair.
            await self._terminate_launched(run_id)
            return False
        if confirmed:
            self._launched.pop(run_id, None)
        return confirmed

    async def _terminate_launched(self, run_id: str) -> bool:
        """Stop (login) or destroy (per-job) the machine THIS process launched
        for *run_id*, without reading or writing the ledger.

        Identification is the narrowest this process can trust: the machine
        id recorded in memory at launch (from this app's validated create
        answer, or the configured login machine whose lease this process
        took), confirmed by the machine's live config still carrying this
        run's id. Nothing is charged, released, scrubbed or rewritten; the
        reservation stays held until the ledger is repaired. A run whose
        create answer was lost has no recorded machine and is left to its
        own in-machine deadline.
        """
        if run_id not in self._launched:
            return False
        rec = self._launched[run_id]
        machine_id, login, nonce = rec["machine_id"], rec["login"], rec["nonce"]
        try:
            verdict = await self._ownership(machine_id, run_id, login=login)
            if verdict in ("gone", "idle"):
                confirmed = True
            elif verdict == "ours":
                confirmed = await self._terminate(machine_id, login=login, nonce=nonce)
            else:
                confirmed = False  # not provably this run's machine: untouched
        except Exception as exc:  # noqa: BLE001 — reported; retried by the reaper
            log.error(f"[fly] run={run_id}: termination without the ledger failed: {exc}")
            return False
        if confirmed:
            self._launched.pop(run_id, None)
            done = "already gone" if verdict == "gone" else (
                "stopped" if login or verdict == "idle" else "destroyed")
            log.error(
                f"[fly] run={run_id} machine={machine_id}: {done} while the ledger is "
                "unusable; its reservation stays held until the ledger is repaired"
            )
        return confirmed

    async def _terminate_launched_inactive(self) -> list[str]:
        """``_terminate_launched`` for every recorded run no live run() holds."""
        done = []
        for run_id in [r for r in list(self._launched) if r not in self._active]:
            if await self._terminate_launched(run_id):
                done.append(run_id)
        return done

    async def _settle_steps(self, run_id: str) -> bool:
        """The one release path, in a strict order, with the admission lock
        held and the reservation still owned throughout:

        1. stop (login) or destroy (per-job) the machine and confirm it;
        2. for a login machine, clear this run's job config (compare-and-set)
           and verify the clear by reading it back;
        3. only then charge runtime once and release the reservation.

        If any step fails the reservation is kept, marked unconfirmed. When
        the machine is unknown (an uncertain create) it is found by run id.
        """
        entry = self.ledger.entry(run_id)
        if entry is None:
            return True  # already settled
        try:
            entry = validate("ledger_entry", entry)
        except FlyInvalid as exc:
            # A reservation we cannot read is one we cannot safely act on.
            self.ledger.mark_unconfirmed(run_id, reason=str(exc))
            log.error(f"[fly] run={run_id}: reservation unreadable ({exc}); kept")
            return False
        machine_id = entry["machine_id"] if "machine_id" in entry else ""
        # A machine configured as a login machine is one, whatever the entry
        # says: a login machine is never destroyed.
        login = entry["login"] or machine_id in set(self.config.login_machines.values())
        nonce = entry["lease_nonce"] if "lease_nonce" in entry else ""
        if machine_id:
            verdict = await self._ownership(machine_id, run_id, login=login)
            if verdict == "gone":
                confirmed = True
            elif verdict == "ours":
                confirmed = True
                if login and run_id not in self._active:
                    # Recovery of a run no live run() holds (the reaper, or
                    # a later launch reconciling the machine): its lease may
                    # have expired with the dispatcher that took it. Modify
                    # the machine only under a lease this run holds NOW.
                    held = await self._lease_for_cleanup(machine_id, run_id, nonce)
                    confirmed = held is not None
                    nonce = held if held is not None else nonce
                if confirmed:
                    confirmed = await self._terminate(machine_id, login=login, nonce=nonce)
                if confirmed and login:
                    confirmed = await self._scrub_login_machine(machine_id, run_id, nonce)
            elif verdict == "idle":
                confirmed = True  # a stopped login machine holding no job at all
            else:
                # Another run's job, or one nobody can attribute: this
                # reservation has no right to touch the machine. Nothing is
                # stopped or scrubbed; the reservation is kept and reported.
                confirmed = False
                log.error(
                    f"[fly] run={run_id}: machine {machine_id} is not this run's "
                    f"({verdict}); left untouched, reservation kept unconfirmed"
                )
        else:
            confirmed = await self._terminate_by_run_id(run_id)
        if confirmed and login and machine_id and nonce:
            # After the confirmed stop and clean-up, before the reservation
            # goes: the lease. A failed release keeps the reservation.
            confirmed = await self._release_lease(machine_id, nonce, run_id)
            if confirmed:
                self.ledger.clear_lease(run_id)
        if confirmed:
            self.ledger.settle(run_id, config=self.config, now=self._clock())
        else:
            self.ledger.mark_unconfirmed(run_id, reason="termination not confirmed")
            log.error(
                f"[fly] run={run_id} machine={machine_id or '(unknown)'}: termination not "
                "confirmed; reservation kept, marked for the reaper"
            )
        return confirmed

    async def _lease_for_cleanup(self, machine_id: str, run_id: str, nonce: str) -> str | None:
        """The nonce under which this run may clear its login machine: its
        own lease while it still holds it; if NO lease is held at all (the
        run's lease expired, e.g. after the dispatcher that took it died), a
        fresh short lease taken for this run. None when another dispatcher
        holds the lease, or the lease state cannot be read: the machine is
        then left alone and the reservation kept."""
        try:
            lease = await self.client.get_lease(machine_id)
        except Exception as exc:  # noqa: BLE001 — unknown: not ours to modify
            log.warning(f"[fly] lease read on {machine_id} failed: {type(exc).__name__}")
            return None
        if lease is not None:
            if lease["description"] == _lease_description(run_id) and nonce:
                return nonce
            log.warning(f"[fly] run={run_id}: login machine {machine_id} is leased by another "
                        "dispatcher; left untouched")  # fmt: skip
            return None
        try:
            fresh, expires = await self.client.acquire_lease(
                machine_id, ttl=LOGIN_CLEANUP_LEASE_TTL, description=_lease_description(run_id))
        except Exception as exc:  # noqa: BLE001 — someone else may have taken it
            log.warning(f"[fly] cleanup lease on {machine_id} not taken: {type(exc).__name__}")
            return None
        self.ledger.set_lease(run_id, fresh, expires)  # kept with the reservation
        return fresh

    async def _ownership(self, machine_id: str, run_id: str, *, login: bool) -> str:
        """Whose is this machine RIGHT NOW, read from its current config?

        ``"ours"``: its config carries this run's id (or, for a per-job
        machine, carries none; a per-job machine is created for one run).
        ``"gone"``: it no longer exists. ``"idle"``: a stopped login machine
        with no job config at all. ``"other"``: another run's id, an
        unattributed job config, a running login machine with no job, or an
        unreadable state. Only ``"ours"`` may be stopped or scrubbed."""
        try:
            machine = await self.client.get(machine_id)
        except Exception as exc:  # noqa: BLE001 — unknown state: not ours to touch
            log.warning(f"[fly] could not read machine {machine_id}: {exc}")
            return "other"
        if machine is None or machine["state"] == "destroyed":
            return "gone"
        cfg = machine["config"]  # validated: present, an object
        owner = _config_run_id(cfg)
        if owner == run_id:
            return "ours"
        if owner:
            return "other"
        if not login:
            return "ours"
        if _has_job_config(cfg):
            return "other"  # a job config nobody can attribute
        return "idle" if machine["state"] in LOGIN_NOT_RUNNING_STATES else "other"

    async def _terminate_by_run_id(self, run_id: str) -> bool:
        """Terminate every machine carrying *run_id*. True only when a valid
        listing was read and each such machine was confirmed terminated."""
        try:
            machines = await self.client.list()
        except Exception as exc:  # noqa: BLE001 — unknown state keeps the reservation
            log.warning(f"[fly] could not list machines to find run {run_id}: {exc}")
            return False
        login_ids = set(self.config.login_machines.values())
        found = [
            m["id"] for m in machines
            if _machine_run_id(m) == run_id and m["state"] != "destroyed"
            and m["id"] not in login_ids  # a login machine is never destroyed
        ]  # fmt: skip
        if not found:
            return True  # a valid listing shows no machine for this run
        if not all([await self._terminate(mid, login=False) for mid in found]):
            return False
        # Confirm against a second valid listing, not only per-machine reads.
        try:
            after = await self.client.list()
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[fly] could not re-list machines for run {run_id}: {exc}")
            return False
        return not any(
            _machine_run_id(m) == run_id and m["state"] != "destroyed"
            and m["id"] not in login_ids
            for m in after
        )

    async def _terminate(self, machine_id: str, *, login: bool, nonce: str = "") -> bool:
        """Stop a login machine, or destroy a per-job one, and CONFIRM it by
        reading its state back (validated; only an explicit 404 is absence).
        An acknowledged delete is not confirmation. Never raises an ordinary
        error: anything unexpected returns False."""
        try:
            machine = await self.client.get(machine_id)
            if machine is None or machine["state"] == "destroyed":
                return True
            if login:
                if machine["state"] in LOGIN_NOT_RUNNING_STATES:
                    return True
                await self.client.stop(machine_id, nonce=nonce)
                await self.client.wait(machine_id, state="stopped", timeout_s=30)
                after = await self.client.get(machine_id)
                return after is None or after["state"] in ("stopped", "destroyed")
            try:
                await self.client.stop(machine_id)
            except Exception as exc:  # noqa: BLE001 — destroy is forced regardless
                log.warning(f"[fly] stop before destroy failed: {type(exc).__name__}")
            await self.client.destroy(machine_id)
            after = await self.client.get(machine_id)
            return after is None or after["state"] == "destroyed"
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[fly] could not confirm machine {machine_id} terminated: {exc}")
            return False

    async def _reconcile_login(self, machine_id: str, own_run_id: str) -> None:
        """Before a login machine takes a new job, account for every other
        reservation on it. One held by a live run (here or in another process)
        means the machine is busy: refuse with a retry time. One that is
        provably abandoned is settled; if that cannot be confirmed, refuse.
        Nothing has been sent to the machine yet in either case."""
        now = self._clock()
        owners = self.ledger.snapshot(now)["owners"]
        others = {
            rid: e for rid, e in self.ledger.find_by_machine(machine_id).items()
            if rid != own_run_id
        }  # fmt: skip
        # A live run anywhere means busy: say so first, touching nothing.
        if any(not self._reapable(rid, e, now, owners) for rid, e in others.items()):
            raise FlyRefusal(
                "login machine is busy with another dispatcher's run",
                _render_wall(now + 300),
            )
        for run_id in others:
            if not await self._settle_locked(run_id):  # admission is held
                raise FlyApiError(409, "login machine from a previous job is not confirmed stopped")

    async def _wait_until_done(self, handle: FlyHandle) -> str:
        while True:
            remaining = handle.deadline - self._clock()
            if remaining <= 0:
                return "deadline"
            result = await self.client.wait(
                handle.machine_id, state="stopped", instance_id=handle.instance_id,
                timeout_s=int(min(60, max(1, remaining))),
            )  # fmt: skip
            if result in ("reached", "gone"):
                return result

    # ── Recovery ─────────────────────────────────────────────────────────────

    async def maybe_reap(self) -> list[str]:
        if self._clock() - self._last_reap < REAPER_INTERVAL_SECONDS:
            return []
        self._last_reap = self._clock()
        try:
            return await self.reap()
        except Exception as exc:  # noqa: BLE001 — the reaper never blocks a launch
            log.warning(f"[fly] reaper failed: {exc}")
            return []

    def _reapable(self, run_id: str, entry: dict, now: float, owners: dict) -> bool:
        """Whether no live run holds this reservation.

        A different owner id is NOT proof of a dead owner: Apis and a
        ``dispatch_role --runner fly`` CLI run concurrently on one ledger.
        Another owner's reservation is reapable only when that process is
        provably dead (same host; pid gone, or reused by a process with a
        different start time) or when it is past its deadline plus the grace
        period AND its owner's heartbeat is stale by more than the grace.
        """
        if run_id in self._active:
            return False  # a live run in this process owns it
        try:
            entry = validate("ledger_entry", entry)
        except FlyInvalid as exc:
            log.error(f"[fly] reservation {run_id} is unreadable ({exc}); never auto-settled")
            return False
        past = now > entry["deadline"] + REAP_GRACE_SECONDS
        owner = entry["owner"]
        if owner == self.instance_id:
            # Not active here, so its run() has ended: an unconfirmed
            # machine is ours to finish; otherwise only once overdue.
            return entry.get("unconfirmed") is True or past
        record = owners[owner] if owner in owners else None
        if owner_provably_dead(record):
            return True
        heartbeat = _owner_heartbeat(record)
        if heartbeat is None:
            return False  # no readable heartbeat: liveness unknown, never stale
        return past and now - heartbeat > REAP_GRACE_SECONDS

    async def reap(self) -> list[str]:
        """Settle reservations no live run holds, and own untracked machines.

        Holds the admission lock throughout, so no launch can reserve or
        create while it decides, and it re-reads the ledger after every
        await before acting. Login machines held by a running job are
        skipped.
        """
        settled: list[str] = []
        async with self._admission():
            try:
                return await self._reap_locked(settled)
            except FlyLedgerError:
                # No accounting on an unusable ledger, but the machines this
                # process launched are still stopped; then report it.
                await self._terminate_launched_inactive()
                raise

    async def _reap_locked(self, settled: list[str]) -> list[str]:
        """``reap`` with the admission lock held."""
        now = self._clock()
        for run_id in list(self.ledger.snapshot(now)["in_flight"]):
            fresh = self.ledger.snapshot(now)  # settlements may have run
            if run_id not in fresh["in_flight"]:
                continue
            entry = fresh["in_flight"][run_id]
            if not self._reapable(run_id, entry, now, fresh["owners"]):
                continue
            provider = next((p for p, m in self.config.login_machines.items()
                             if m == entry.get("machine_id")), "")  # fmt: skip
            if provider and _login_lock(provider).locked():
                continue  # a job holds that machine; its launch reconciles first
            if await self._settle_locked(run_id):  # admission is held
                settled.append(run_id)
        machines = await self.client.list()
        current = self.ledger.snapshot(now)["in_flight"]  # after the await
        # Every reservation naming a machine claims it (the snapshot is
        # validated as a whole, so every reservation is readable here).
        tracked_machines = {e["machine_id"] for e in current.values() if "machine_id" in e}
        login_ids = set(self.config.login_machines.values())
        for machine in machines:
            mid = machine["id"]
            run_id = _machine_run_id(machine)
            if (mid in login_ids or mid in tracked_machines or machine.get("state") == "destroyed"
                    or (run_id and (run_id in current or run_id in self._active))):  # fmt: skip
                continue
            cap = DISPATCH_TIMEOUT_DEFAULT + APIS_DEADLINE_GRACE_SECONDS
            created = _parse_ts(machine["created_at"]) if "created_at" in machine else None
            key = self.ledger.record_untracked(
                machine_id=mid, run_id=run_id, owner=self.instance_id, config=self.config,
                # An unreadable creation time, or one later than now (which
                # would charge nothing), is charged the whole window.
                started_at=created if created is not None and created <= now else now - cap,
                max_run_seconds=cap,
            )  # fmt: skip
            if await self._settle_locked(key):  # admission is held
                settled.append(key)
        if settled:
            log.warning(f"[fly] reaper settled {settled}")
        return settled


def _machine_run_id(machine: dict) -> str:
    """The run id of a VALIDATED machine or list entry."""
    return _config_run_id(machine["config"])


def _owner_heartbeat(record: dict | None) -> float | None:
    """A readable owner heartbeat, or None (unknown)."""
    try:
        return float(validate("owner_record", record)["heartbeat"])
    except FlyInvalid:
        return None


LEASE_SAFETY_SECONDS = 60
# Right after a config update Fly reports a login machine in a transitional
# state for a moment before it is back to "stopped". These are "not settled
# yet", never a refusal on their own.
LOGIN_SETTLING_STATES = frozenset({"created", "updating", "replacing"})
# States in which a login machine runs nothing: its stored config may be
# rewritten, and it counts as stopped for settlement.
LOGIN_NOT_RUNNING_STATES = frozenset({"stopped", "created"})
# The bound on waiting for a login machine to settle after an update: one
# wait call (server side), then a few short reads.
LOGIN_SETTLE_WAIT_SECONDS = 20
LOGIN_SETTLE_POLLS = 5
LOGIN_SETTLE_POLL_SECONDS = 1.0
# A short lease taken only to clear a stopped login machine whose run's own
# lease has expired (recovery of an unconfirmed reservation).
LOGIN_CLEANUP_LEASE_TTL = 120


def _lease_description(run_id: str) -> str:
    return f"ateles-fly-runner {run_id}"


def _login_machine_problem(machine: dict) -> str:
    """What is missing for a login machine's state to be trusted, or "".
    Absence is never availability: the config must be an object that is
    present, and the version a non-empty string."""
    if not isinstance(machine.get("config"), dict):
        return "its configuration is missing"
    version = machine.get("instance_id")
    if not isinstance(version, str) or not version:
        return "its version is missing"
    state = machine.get("state")
    if not isinstance(state, str) or not state:
        return "its state is missing"
    return ""


def _login_admission_refusal(machine: dict, run_id: str) -> str:
    """Why a login machine's live state forbids installing *run_id*'s job,
    or "" when it is free: completely known, stopped, with no job config or
    only this run's."""
    problem = _login_machine_problem(machine)
    if problem:
        return problem
    state = machine["state"]
    cfg = machine["config"]
    owner = _config_run_id(cfg)
    if state != "stopped":
        return f"it is {state}"
    if owner and owner != run_id:
        return "it holds another run's job"
    if not owner and _has_job_config(cfg):
        return "it holds a job nobody can attribute"
    return ""


def _config_run_id(config: dict) -> str:
    """The run a VALIDATED machine config belongs to: its metadata marker,
    else the non-secret run id in its environment, else "" (the config is
    present and carries no marker; that is a reading, not a default)."""
    if "metadata" in config and "ateles_run_id" in config["metadata"]:
        if config["metadata"]["ateles_run_id"]:
            return config["metadata"]["ateles_run_id"]
    if "env" in config and "ATELES_RUN_ID" in config["env"]:
        return config["env"]["ATELES_RUN_ID"]
    return ""


def _has_job_config(config: dict) -> bool:
    """Whether a VALIDATED machine config carries any job at all: job files,
    job environment, or a run marker."""
    return bool(
        ("files" in config and config["files"])
        or ("env" in config and config["env"])
        or _config_run_id(config)
    )


DISPATCH_TIMEOUT_DEFAULT = 1800


def _charged_seconds(entry: dict, now: float) -> float:
    """Machine-seconds to charge for an entry released after the fact: its
    elapsed time, capped at its reserved window. An entry whose times cannot
    be read is charged the whole default window, never nothing."""
    try:
        entry = validate("ledger_entry", entry)
    except FlyInvalid:
        return float(DISPATCH_TIMEOUT_DEFAULT + APIS_DEADLINE_GRACE_SECONDS)
    window = entry["deadline"] - entry["started_at"]  # not negative: validated
    if not now >= entry["started_at"]:
        # The clock reads earlier than the start: the elapsed time is
        # unknown, so the whole reserved window is charged, never nothing.
        return float(window)
    return min(now, entry["deadline"]) - entry["started_at"]


def _parse_ts(raw: Any) -> float | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


_EXECUTOR: FlyExecutor | None = None


def get_executor() -> FlyExecutor:
    """The process-wide executor, built from the environment on first use."""
    global _EXECUTOR
    if _EXECUTOR is None:
        config = FlyConfig.from_env()
        log_limit_check(config)
        _EXECUTOR = FlyExecutor(config)
    return _EXECUTOR


def log_limit_check(config: FlyConfig | None = None) -> list[str]:
    """Validate the cost, capacity and time settings once and log the result
    (at startup). Returns the problems; every launch refuses while any
    remain, so this is a report, not the guard."""
    config = config if config is not None else FlyConfig.from_env()
    problems = config.limit_problems()
    if problems:
        log.error("[fly] Fly is disabled until fixed: " + "; ".join(problems))
    else:
        log.info(
            f"[fly] limits ok: budget ${config.budget_usd:.2f}/month, "
            f"{config.max_per_day} machines/day, {config.max_concurrent} concurrent"
        )
    return problems
