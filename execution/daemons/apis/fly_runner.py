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
* **Provider**: Claude gets ``CLAUDE_CODE_OAUTH_TOKEN`` (a ``claude
  setup-token`` OAuth token). Codex and Cursor get NO per-job credential: each
  runs on its own long-lived login machine, stopped between jobs.

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
import copy
import json
import logging
import os
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
AAUTH_JWK_JOB_NAME = "NEOTOMA_AAUTH_PRIVATE_JWK"
APIS_DEADLINE_GRACE_SECONDS = 120
REAPER_INTERVAL_SECONDS = 600
# Binary names on the runner image (argv[0] on the Mac is a local path).
RUNNER_BINARIES: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "cursor": "cursor-agent",
}
LOGIN_PROVIDERS: frozenset[str] = frozenset({"codex", "cursor"})
# shared-cpu-2x / 2 GB, Amsterdam list price (ateles#1456 cost table): about
# $13.90 per 30 days.
DEFAULT_RATE_PER_HOUR_USD = 13.90 / (30 * 24)
DEFAULT_STORAGE_GB_MONTH_USD = 0.15
DEFAULT_LOGIN_VOLUME_GB = 1.0
DEFAULT_ROOTFS_GB = 2.0


class FlyIneligible(Exception):
    """This job cannot run on Fly (no own identity, no App, unsupported
    shape). The reason names what is missing, never a value."""


class FlyRefusal(Exception):
    """A cost or queue cap refused the launch; ``cooled_until`` says when."""

    def __init__(self, reason: str, cooled_until: str):
        super().__init__(reason)
        self.cooled_until = cooled_until


class FlyLaunchError(Exception):
    """The Fly API refused or failed to start the machine."""


class FlyApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Fly API HTTP {status}: {message[:200]}")
        self.status = status


def _render_wall(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="minutes")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _csv(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def runner_classes() -> frozenset[str]:
    """The dispatch classes (``action_class`` values) opted onto Fly."""
    return frozenset(_csv(os.environ.get("APIS_FLY_RUNNER_CLASSES", "")))


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
    return bool(action_class) and action_class in runner_classes()


@dataclass
class FlyConfig:
    app: str
    api_token: str
    image: str
    region: str
    api_base: str = FLY_API_BASE_DEFAULT
    budget_usd: float = 20.0
    max_per_day: int = 50
    max_concurrent: int = 3
    ledger_path: Path = Path.home() / ".config" / "ateles" / "fly-runner-ledger.json"
    login_machines: dict[str, str] = field(default_factory=dict)
    queue_max_wait_seconds: int = 1800
    local_fallback: bool = True
    github_app_prefix: str = "ATELES_REVIEWER_APP"
    extra_probe_env_names: tuple[str, ...] = ()
    rate_per_second_usd: float = DEFAULT_RATE_PER_HOUR_USD / 3600
    storage_gb_month_usd: float = DEFAULT_STORAGE_GB_MONTH_USD
    login_volume_gb: float = DEFAULT_LOGIN_VOLUME_GB
    rootfs_gb: float = DEFAULT_ROOTFS_GB
    cursor_login_dirs: tuple[str, ...] = (".cursor", ".config/cursor")

    @classmethod
    def from_env(cls) -> "FlyConfig":
        env = os.environ
        login = {}
        if env.get("APIS_FLY_CODEX_MACHINE_ID", "").strip():
            login["codex"] = env["APIS_FLY_CODEX_MACHINE_ID"].strip()
        if env.get("APIS_FLY_CURSOR_MACHINE_ID", "").strip():
            login["cursor"] = env["APIS_FLY_CURSOR_MACHINE_ID"].strip()
        ledger = env.get("APIS_FLY_LEDGER_FILE", "").strip()
        return cls(
            app=env.get("APIS_FLY_APP", "").strip(),
            api_token=env.get("APIS_FLY_API_TOKEN", "").strip(),
            image=env.get("APIS_FLY_IMAGE", "").strip(),
            region=env.get("APIS_FLY_REGION", "").strip(),
            api_base=(env.get("APIS_FLY_API_BASE", "").strip() or FLY_API_BASE_DEFAULT).rstrip("/"),
            budget_usd=_env_float("APIS_FLY_MONTHLY_BUDGET_USD", 20.0),
            max_per_day=_env_int("APIS_FLY_MAX_MACHINES_PER_DAY", 50),
            max_concurrent=_env_int("APIS_FLY_MAX_CONCURRENT", 3),
            ledger_path=Path(ledger).expanduser() if ledger else cls.ledger_path,
            login_machines=login,
            queue_max_wait_seconds=_env_int("APIS_FLY_QUEUE_MAX_WAIT_SECONDS", 1800),
            local_fallback=env.get("APIS_FLY_LOCAL_FALLBACK", "1").strip() != "0",
            github_app_prefix=(
                env.get("APIS_FLY_GITHUB_APP_PREFIX", "").strip() or "ATELES_REVIEWER_APP"
            ),
            extra_probe_env_names=tuple(_csv(env.get("APIS_FLY_PROBE_ENV_NAMES", ""))),
            rate_per_second_usd=_env_float(
                "APIS_FLY_RATE_PER_HOUR_USD", DEFAULT_RATE_PER_HOUR_USD
            )
            / 3600,
            storage_gb_month_usd=_env_float(
                "APIS_FLY_STORAGE_GB_MONTH_USD", DEFAULT_STORAGE_GB_MONTH_USD
            ),
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


def _reserved_cost(entry: dict, config: FlyConfig) -> float:
    """An in-flight entry's reserved maximum cost (derived from its window for
    an entry written before costs were stored)."""
    try:
        return float(entry["max_cost_usd"])
    except (KeyError, TypeError, ValueError):
        span = float(entry.get("deadline") or 0) - float(entry.get("started_at") or 0)
        return max(0.0, span) * config.rate_per_second_usd


class FlyLedgerError(Exception):
    """The ledger file exists but cannot be read: accounting is unknown."""


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

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"month": "", "spend_usd": 0.0, "day": "", "launches_today": 0,
                    "local_fallbacks": 0, "in_flight": {}}  # fmt: skip
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise FlyLedgerError(f"Fly ledger {self.path.name} is unreadable") from None
        if not isinstance(data, dict) or not isinstance(data.get("in_flight", {}), dict):
            raise FlyLedgerError(f"Fly ledger {self.path.name} has an unexpected shape")
        data.setdefault("in_flight", {})
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def _roll(self, data: dict, now: float) -> None:
        if data.get("month") != _month_key(now):
            data["month"] = _month_key(now)
            data["spend_usd"] = 0.0
        if data.get("day") != _day_key(now):
            data["day"] = _day_key(now)
            data["launches_today"] = 0

    def snapshot(self, now: float | None = None) -> dict:
        with self._lock:
            data = self._load()
            self._roll(data, now or time.time())
            return copy.deepcopy(data)

    def reserve(self, config: FlyConfig, *, run_id: str, owner: str, max_run_seconds: int,
                login: bool = False, machine_id: str = "",
                now: float | None = None) -> None:  # fmt: skip
        """Refuse, or reserve one launch slot for *run_id*, owned by *owner*.

        Admission is atomic under the ledger lock and counts, against the
        budget, the month's completed spend, the login machines' storage,
        the maximum-length cost of EVERY reservation still held (including
        unconfirmed and untracked machines), and the incoming job's own
        maximum-length cost. Refuses when that total would exceed the budget
        (until the next calendar month), when the day's launch count is at
        its cap (until the next UTC day), or when the concurrency limit is
        reached (until the earliest held deadline).
        """
        now = time.time() if now is None else now
        with self._lock:
            try:
                data = self._load()
            except FlyLedgerError as exc:
                raise FlyRefusal(f"{exc}; refusing Fly launches until it is repaired",
                                 _render_wall(now + 3600)) from None  # fmt: skip
            self._roll(data, now)
            worst = max_run_seconds * config.rate_per_second_usd
            outstanding = sum(
                _reserved_cost(entry, config) for entry in data["in_flight"].values()
            )
            committed = (
                float(data.get("spend_usd") or 0.0)
                + config.monthly_storage_usd()
                + outstanding
            )
            if committed + worst > config.budget_usd:
                raise FlyRefusal(
                    f"Fly monthly budget: ${committed:.4f} spent, stored or reserved "
                    f"in flight plus ${worst:.4f} for this run's maximum length would "
                    f"exceed APIS_FLY_MONTHLY_BUDGET_USD=${config.budget_usd:.2f}",
                    _render_wall(_next_month_start(now)),
                )
            if int(data.get("launches_today") or 0) >= config.max_per_day:
                raise FlyRefusal(
                    f"Fly daily machine cap reached (APIS_FLY_MAX_MACHINES_PER_DAY="
                    f"{config.max_per_day})",
                    _render_wall(_next_day_start(now)),
                )
            in_flight = data["in_flight"]
            if len(in_flight) >= config.max_concurrent:
                earliest = min(float(v.get("deadline") or now + 60) for v in in_flight.values())
                raise FlyRefusal(
                    f"Fly concurrency cap reached (APIS_FLY_MAX_CONCURRENT="
                    f"{config.max_concurrent})",
                    _render_wall(max(earliest, now + 60)),
                )
            data["launches_today"] = int(data.get("launches_today") or 0) + 1
            in_flight[run_id] = {
                "run_id": run_id, "owner": owner, "machine_id": machine_id, "login": login,
                "started_at": now, "deadline": now + max_run_seconds, "max_cost_usd": worst,
            }  # fmt: skip
            self._save(data)

    def record_untracked(self, *, machine_id: str, run_id: str, owner: str, config: FlyConfig,
                         started_at: float, max_run_seconds: int) -> str:  # fmt: skip
        """Own a machine found running that no reservation accounts for. It is
        recorded unconfirmed with a full reserved cost, so it counts against
        concurrency and budget until its termination is confirmed. Bypasses
        the caps: it records a fact, it is not an admission."""
        key = run_id or f"untracked-{machine_id}"
        with self._lock:
            data = self._load()
            data["in_flight"].setdefault(key, {
                "run_id": key, "owner": owner, "machine_id": machine_id, "login": False,
                "started_at": started_at, "deadline": started_at + max_run_seconds,
                "max_cost_usd": max_run_seconds * config.rate_per_second_usd,
                "unconfirmed": True, "untracked": True,
            })  # fmt: skip
            self._save(data)
        return key

    def set_machine(self, run_id: str, machine_id: str) -> None:
        with self._lock:
            data = self._load()
            entry = data["in_flight"].get(run_id)
            if entry is not None:
                entry["machine_id"] = machine_id
                self._save(data)

    def mark_unconfirmed(self, run_id: str, *, reason: str) -> None:
        """Keep *run_id*'s slot and reservation, flagged for the reaper: its
        machine's termination could not be confirmed, so it is still owned."""
        with self._lock:
            data = self._load()
            entry = data["in_flight"].get(run_id)
            if entry is None:
                return
            entry["unconfirmed"] = True
            entry["unconfirmed_reason"] = reason[:200]
            self._save(data)

    def entry(self, run_id: str) -> dict | None:
        with self._lock:
            return copy.deepcopy(self._load()["in_flight"].get(run_id))

    def find_by_machine(self, machine_id: str) -> dict[str, dict]:
        with self._lock:
            return {
                rid: copy.deepcopy(e) for rid, e in self._load()["in_flight"].items()
                if e.get("machine_id") == machine_id
            }  # fmt: skip

    def release(self, run_id: str) -> None:
        """Drop a reservation that provably never ran anything (no charge)."""
        with self._lock:
            data = self._load()
            if data["in_flight"].pop(run_id, None) is not None:
                self._save(data)

    def settle(self, run_id: str, *, config: FlyConfig, now: float | None = None) -> float:
        """Release *run_id* and charge its incurred runtime, EXACTLY ONCE.

        The charge is the entry's elapsed time capped at its reserved window,
        read from the entry itself under the lock. An entry already settled
        is absent, so a second settlement charges nothing. Callers settle
        only after termination is confirmed. Returns the dollars charged.
        """
        now = time.time() if now is None else now
        with self._lock:
            data = self._load()
            self._roll(data, now)
            entry = data["in_flight"].pop(run_id, None)
            if entry is None:
                return 0.0
            cost = _charged_seconds(entry, now) * config.rate_per_second_usd
            data["spend_usd"] = float(data.get("spend_usd") or 0.0) + cost
            self._save(data)
            return cost

    def count_local_fallback(self) -> int:
        with self._lock:
            try:
                data = self._load()
            except FlyLedgerError:
                return -1
            data["local_fallbacks"] = int(data.get("local_fallbacks") or 0) + 1
            self._save(data)
            return data["local_fallbacks"]


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
                    parse: bool = True) -> Any:  # fmt: skip
        async with self._client() as client:
            resp = await client.request(method, path, params=params, json=body)
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
    def _machine(data: Any, *, expect_id: str = "") -> dict:
        if not isinstance(data, dict):
            raise _invalid("machine is not an object")
        mid = data.get("id")
        if not isinstance(mid, str) or not mid:
            raise _invalid("machine has no id")
        if expect_id and mid != expect_id:
            raise _invalid("machine id does not match the request")
        if "state" in data and not isinstance(data["state"], str):
            raise _invalid("machine state is not a string")
        return data

    async def create(self, body: dict) -> dict:
        return self._machine(await self._call("POST", "/machines", body=body))

    async def get(self, machine_id: str) -> dict | None:
        """The machine, or None ONLY for an explicit 404."""
        try:
            data = await self._call("GET", f"/machines/{machine_id}")
        except FlyApiError as exc:
            if exc.status == 404:
                return None
            raise
        machine = self._machine(data, expect_id=machine_id)
        if not isinstance(machine.get("state"), str):
            raise _invalid("machine has no state")
        return machine

    async def update(self, machine_id: str, body: dict) -> dict:
        data = await self._call("POST", f"/machines/{machine_id}", body=body)
        if not isinstance(data, dict):
            raise _invalid("update result is not an object")
        return data

    async def start(self, machine_id: str) -> None:
        await self._call("POST", f"/machines/{machine_id}/start", parse=False)

    async def stop(self, machine_id: str) -> None:
        try:
            await self._call("POST", f"/machines/{machine_id}/stop", parse=False)
        except FlyApiError as exc:
            if exc.status != 404:
                raise

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
        return [self._machine(m) for m in data]

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


# ── Neotoma result record (read on the Mac with Apis's own access) ───────────


async def read_result_record(run_id: str, *, transport: httpx.AsyncBaseTransport | None = None,
                             attempts: int = 3, delay: float = 2.0) -> dict | None:  # fmt: skip
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
    for attempt in range(attempts):
        try:
            async with httpx.AsyncClient(
                timeout=30.0, transport=transport,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            ) as client:  # fmt: skip
                resp = await client.post(f"{base}/entities/query", json=body)
            if resp.status_code == 200:
                for row in (resp.json() or {}).get("entities") or []:
                    snap = row.get("snapshot") if isinstance(row, dict) else None
                    if isinstance(snap, dict) and isinstance(snap.get("snapshot"), dict):
                        snap = snap["snapshot"]
                    if isinstance(snap, dict) and snap.get("run_id") == run_id:
                        return snap
        except (httpx.HTTPError, ValueError) as exc:
            log.debug(f"[fly] result read attempt {attempt + 1} failed: {type(exc).__name__}")
        if attempt + 1 < attempts:
            await asyncio.sleep(delay)
    return None


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
    probe_env_names: list[str] = field(default_factory=list)
    # Set by the executor: the machine the job ran on, and whether the
    # runner kept only the tail of an oversized stdout.
    machine_id: str = ""
    stdout_truncated: bool = False
    # "terminated" or "unconfirmed" once the executor tried to settle a
    # machine for this job; and whether the job's outcome is unknown (a
    # lost wait or a missing result record).
    remote_state: str = ""
    outcome_unknown: bool = False

    @property
    def login(self) -> bool:
        return self.provider in LOGIN_PROVIDERS


def runner_argv(provider: str, cmd: list[str], mcp_config_path: str | None) -> list[str]:
    """The local argv rewritten for the runner image: the binary by name, and
    the Mac's temporary MCP config path (which may name the shared bearer)
    replaced by a placeholder ``run_job.py`` fills from the role's own token."""
    argv = [RUNNER_BINARIES[provider], *cmd[1:]]
    if mcp_config_path:
        argv = [MCP_CONFIG_PLACEHOLDER if a == mcp_config_path else a for a in argv]
    return argv


def role_jwk_path(role: str) -> Path | None:
    keys_dir = os.environ.get("ATELES_PRIVATE_KEYS_DIR", "").strip()
    if not keys_dir:
        return None
    path = Path(keys_dir) / f"{role}.jwk.json"
    return path if path.is_file() else None


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
) -> FlyJob:
    """Check eligibility and assemble the job, minus the GitHub token (which
    is minted at launch). Raises ``FlyIneligible`` with the reason."""
    from skill_runner import neotoma_token_env_name

    config = config or FlyConfig.from_env()
    missing = config.missing()
    if missing:
        raise FlyIneligible("Fly runner not configured: " + ", ".join(missing) + " unset")
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
    if provider in LOGIN_PROVIDERS and not config.login_machines.get(provider):
        raise FlyIneligible(
            f"no {provider} login machine configured (APIS_FLY_{provider.upper()}_MACHINE_ID)"
        )

    secrets: dict[str, str] = {}
    shared_bearer = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    token_name = neotoma_token_env_name(role)
    own_token = os.environ.get(token_name, "").strip()
    if own_token and shared_bearer and own_token == shared_bearer.strip():
        raise FlyIneligible(f"{token_name} is the shared daemon bearer, not the role's own")
    jwk_path = role_jwk_path(role)
    jwk_text = ""
    if jwk_path is not None:
        try:
            jwk_text = jwk_path.read_text(encoding="utf-8").strip()
            jwk = json.loads(jwk_text)
        except (OSError, ValueError):
            raise FlyIneligible(f"the AAuth key file for {role!r} is unreadable") from None
        if str(jwk.get("sub") or "") != f"{role}@ateles-swarm" or not jwk.get("d"):
            raise FlyIneligible(f"the AAuth key file for {role!r} is not that role's private key")
    if not own_token and not jwk_text:
        raise FlyIneligible(
            f"role {role!r} has no Neotoma identity of its own ({token_name} or an AAuth "
            "key); the shared daemon bearer never leaves the Mac"
        )
    if jwk_text:
        secrets[AAUTH_JWK_JOB_NAME] = jwk_text
    if own_token:
        secrets[token_name] = own_token

    child_names: list[str] = []
    if provider == "claude":
        oauth = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
        if not oauth:
            raise FlyIneligible("CLAUDE_CODE_OAUTH_TOKEN (claude setup-token) is unset")
        secrets["CLAUDE_CODE_OAUTH_TOKEN"] = oauth
        child_names.append("CLAUDE_CODE_OAUTH_TOKEN")

    base_url = os.environ.get("NEOTOMA_BASE_URL", "").strip().rstrip("/")
    if not base_url.startswith("https://"):
        raise FlyIneligible("NEOTOMA_BASE_URL must be an https URL for a Fly run")

    probe = list(dict.fromkeys([
        "NEOTOMA_BEARER_TOKEN", "TELEGRAM_BOT_TOKEN", "APIS_GITHUB_WEBHOOK_SECRET",
        "ATELES_AGENT_PAT", "NEOTOMA_AGENT_PAT",
        # Every other role's PAT by name, plus the operator's canary name(s).
        *sorted(n for n in os.environ if n.endswith("_AGENT_PAT")),
        *sorted(n for n in os.environ if n.endswith("_NEOTOMA_TOKEN") and n != token_name),
        *config.extra_probe_env_names,
    ]))  # fmt: skip

    argv = runner_argv(provider, cmd, mcp_config_path)
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
        probe_env_names=probe,
    )


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
    }
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
# How long past its deadline a reservation of THIS process must be before the
# reaper may treat it as abandoned (a live run settles its own well before).
REAP_GRACE_SECONDS = 300


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
    ):
        self.config = config
        self.client = client or FlyMachinesClient(config)
        self.ledger = ledger or FlyLedger(config.ledger_path)
        self._mint = mint
        self._revoke = revoke
        self._read_result = read_result
        self._clock = clock
        self._last_reap = 0.0
        self.instance_id = f"apis-{uuid.uuid4().hex[:16]}"
        self._active: set[str] = set()
        self._admission: asyncio.Lock | None = None
        self._admission_loop: Any = None

    def _admission_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._admission is None or self._admission_loop is not loop:
            self._admission, self._admission_loop = asyncio.Lock(), loop
        return self._admission

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
        tokens = _TokenOwner(self._revoke)
        launch = _LaunchState()
        self._active.add(job.run_id)
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
                        self.ledger.release(job.run_id)
                        if launch.config_written:
                            await self._scrub_login_machine(launch.machine_id)
                finally:
                    self._active.discard(job.run_id)
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

    async def _request(self, launch: _LaunchState, coro) -> Any:
        """Send a create/start. Its failure is a definite launch failure only
        when the request provably did not take effect; otherwise uncertain."""
        launch.requested = True
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
        if job.needs_github:
            token = await self._mint_owned(job, tokens)
            job.secrets["GITHUB_TOKEN"] = token
            job.secrets["GH_TOKEN"] = token
            for name in ("GITHUB_TOKEN", "GH_TOKEN"):
                if name not in job.child_credential_names:
                    job.child_credential_names.append(name)
        async with self._admission_lock():
            login_id = self.config.login_machines.get(job.provider, "") if job.login else ""
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
                    launch.config_written = True
                    await self._write_login_config(login_id, job, deadline)
                    await self._request(launch, self.client.start(login_id))
                    machine_id, instance_id = login_id, ""
                else:
                    created = await self._request(
                        launch, self.client.create(per_job_machine_body(job, self.config, deadline))
                    )
                    machine_id = created["id"]
                    instance_id = str(created.get("instance_id") or "")
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

    async def _write_login_config(self, machine_id: str, job: FlyJob, deadline: float) -> None:
        machine = await self.client.get(machine_id)
        if machine is None:
            raise FlyApiError(404, f"{job.provider} login machine not found")
        cfg = copy.deepcopy(machine.get("config") or {})
        cfg["image"] = self.config.image
        cfg["auto_destroy"] = False
        cfg["restart"] = {"policy": "no"}
        cfg["env"] = machine_env(job, deadline)
        cfg["files"] = job_files(job, self.config)
        await self.client.update(machine_id, {"config": cfg, "skip_launch": True})

    async def _scrub_login_machine(self, machine_id: str) -> None:
        """Remove the job's files from the stopped login machine's stored
        config, so no credential outlives the job in Fly's config store."""
        try:
            machine = await self.client.get(machine_id)
            if machine is None:
                return
            cfg = copy.deepcopy(machine.get("config") or {})
            cfg["files"] = []
            cfg["env"] = {}
            await self.client.update(machine_id, {"config": cfg, "skip_launch": True})
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[fly] could not clear job files from login machine: {type(exc).__name__}")

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
            raise asyncio.TimeoutError()
        record = await self._read_result(job.run_id)
        if not record:
            job.outcome_unknown = True
            return 1, b"", f"fly runner: no result recorded for run {job.run_id}".encode()
        return self._result_from_record(job, record)

    def _result_from_record(self, job: FlyJob, record: dict) -> tuple[int, bytes, bytes]:
        if record.get("probe_ok") is False:
            log.error(f"[fly] run={job.run_id} credential probe failed: {record.get('refused')}")
        try:
            rc = int(record.get("exit_code"))
        except (TypeError, ValueError):
            rc = 1
        stdout = str(record.get("stdout") or "")
        stderr = str(record.get("stderr_tail") or "")
        if record.get("stdout_truncated"):
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
        """Confirm the reservation's machine is terminated, then charge its
        runtime once and release it. When the machine is unknown (an
        uncertain create), it is found by run id. When termination cannot be
        confirmed, the reservation is kept, marked unconfirmed."""
        entry = self.ledger.entry(run_id)
        if entry is None:
            return True  # already settled
        machine_id = str(entry.get("machine_id") or "")
        login = bool(entry.get("login"))
        if machine_id:
            confirmed = await self._terminate(machine_id, login=login)
        else:
            confirmed = await self._terminate_by_run_id(run_id)
        if confirmed:
            self.ledger.settle(run_id, config=self.config, now=self._clock())
            if login and machine_id:
                await self._scrub_login_machine(machine_id)
        else:
            self.ledger.mark_unconfirmed(run_id, reason="termination not confirmed")
            log.error(
                f"[fly] run={run_id} machine={machine_id or '(unknown)'}: termination not "
                "confirmed; reservation kept, marked for the reaper"
            )
        return confirmed

    async def _terminate_by_run_id(self, run_id: str) -> bool:
        """Terminate every machine carrying *run_id*. True only when a valid
        listing was read and each such machine was confirmed terminated."""
        try:
            machines = await self.client.list()
        except Exception as exc:  # noqa: BLE001 — unknown state keeps the reservation
            log.warning(f"[fly] could not list machines to find run {run_id}: {exc}")
            return False
        found = [
            m["id"] for m in machines
            if _machine_run_id(m) == run_id and m.get("state") != "destroyed"
        ]  # fmt: skip
        results = [await self._terminate(mid, login=False) for mid in found]
        return all(results)

    async def _terminate(self, machine_id: str, *, login: bool) -> bool:
        """Stop a login machine, or destroy a per-job one, and CONFIRM it by
        reading its state back (validated; only an explicit 404 is absence).
        An acknowledged delete is not confirmation. Never raises an ordinary
        error: anything unexpected returns False."""
        try:
            machine = await self.client.get(machine_id)
            if machine is None or machine["state"] == "destroyed":
                return True
            if login:
                if machine["state"] == "stopped":
                    return True
                await self.client.stop(machine_id)
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
        """Before a login machine takes a new job, settle every other
        reservation on it that no live run holds. Refuse the launch (nothing
        has been sent yet) if any cannot be confirmed stopped."""
        for run_id in self.ledger.find_by_machine(machine_id):
            if run_id == own_run_id or run_id in self._active:
                continue
            if not await self._settle(run_id):
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

    def _reapable(self, run_id: str, entry: dict, now: float) -> bool:
        if run_id in self._active:
            return False  # a live run in this process owns it
        if entry.get("owner") != self.instance_id:
            return True  # left by a previous Apis process
        if entry.get("unconfirmed"):
            return True  # its run ended without confirming termination
        return now > float(entry.get("deadline") or 0) + REAP_GRACE_SECONDS

    async def reap(self) -> list[str]:
        """Settle reservations no live run holds, and own untracked machines.

        Holds the admission lock throughout, so no launch can reserve or
        create while it decides, and it re-reads the ledger after every
        await before acting. Login machines held by a running job are
        skipped.
        """
        settled: list[str] = []
        async with self._admission_lock():
            now = self._clock()
            for run_id in list(self.ledger.snapshot(now)["in_flight"]):
                entry = self.ledger.entry(run_id)  # fresh: settlements may have run
                if entry is None or not self._reapable(run_id, entry, now):
                    continue
                if entry.get("login"):
                    provider = next((p for p, m in self.config.login_machines.items()
                                     if m == entry.get("machine_id")), "")  # fmt: skip
                    if provider and _login_lock(provider).locked():
                        continue  # a job holds that machine; its launch reconciles first
                if await self._settle(run_id):
                    settled.append(run_id)
            machines = await self.client.list()
            current = self.ledger.snapshot(now)["in_flight"]  # after the await
            tracked_machines = {str(e.get("machine_id") or "") for e in current.values()}
            login_ids = set(self.config.login_machines.values())
            for machine in machines:
                mid = machine["id"]
                run_id = _machine_run_id(machine)
                if (mid in login_ids or mid in tracked_machines or machine.get("state") == "destroyed"
                        or (run_id and (run_id in current or run_id in self._active))):  # fmt: skip
                    continue
                created = _parse_ts(machine.get("created_at"))
                key = self.ledger.record_untracked(
                    machine_id=mid, run_id=run_id, owner=self.instance_id, config=self.config,
                    started_at=created if created is not None else now,
                    max_run_seconds=DISPATCH_TIMEOUT_DEFAULT + APIS_DEADLINE_GRACE_SECONDS,
                )  # fmt: skip
                if await self._settle(key):
                    settled.append(key)
        if settled:
            log.warning(f"[fly] reaper settled {settled}")
        return settled


def _machine_run_id(machine: dict) -> str:
    return str(((machine.get("config") or {}).get("metadata") or {}).get("ateles_run_id") or "")


DISPATCH_TIMEOUT_DEFAULT = 1800


def _charged_seconds(entry: dict, now: float) -> float:
    """Machine-seconds to charge for an entry released after the fact: its
    elapsed time, capped at its reserved window."""
    started = float(entry.get("started_at") or now)
    deadline = float(entry.get("deadline") or now)
    return max(0.0, min(now, deadline) - started)


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
        _EXECUTOR = FlyExecutor(FlyConfig.from_env())
    return _EXECUTOR
