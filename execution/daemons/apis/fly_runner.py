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


class FlyLedger:
    """A local JSON ledger, like the tier ledger. One Apis process writes it;
    a lock serialises this process's threads and coroutines."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("in_flight", {})
                return data
        except (OSError, ValueError):
            pass
        return {"month": "", "spend_usd": 0.0, "day": "", "launches_today": 0,
                "local_fallbacks": 0, "in_flight": {}}  # fmt: skip

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

    def reserve(self, config: FlyConfig, *, run_id: str, max_run_seconds: int,
                now: float | None = None) -> None:  # fmt: skip
        """Refuse, or reserve one launch slot for *run_id*.

        Admission is atomic under the ledger lock and counts, against the
        budget, the month's completed spend, the login machines' storage,
        the maximum-length cost of EVERY reservation still in flight
        (including machines whose termination is unconfirmed), and the
        incoming job's own maximum-length cost. Refuses when that total
        would exceed the budget (until the next calendar month), when the
        day's launch count is at its cap (until the next UTC day), or when
        the concurrency limit is reached (until the earliest in-flight
        deadline).
        """
        now = time.time() if now is None else now
        with self._lock:
            data = self._load()
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
            in_flight[f"pending:{run_id}"] = {
                "run_id": run_id, "started_at": now, "deadline": now + max_run_seconds,
                "max_cost_usd": worst,
            }  # fmt: skip
            self._save(data)

    def mark_unconfirmed(self, key: str, *, reason: str) -> None:
        """Keep *key*'s slot and reservation, flagged for the reaper: the
        machine's termination could not be confirmed, so it is still owned."""
        with self._lock:
            data = self._load()
            entry = data["in_flight"].get(key) or {"run_id": "", "started_at": time.time()}
            entry["unconfirmed"] = True
            entry["unconfirmed_reason"] = reason[:200]
            data["in_flight"][key] = entry
            self._save(data)

    def entry(self, key: str) -> dict | None:
        with self._lock:
            return copy.deepcopy(self._load()["in_flight"].get(key))

    def bind(self, run_id: str, machine_id: str, *, login: bool) -> None:
        with self._lock:
            data = self._load()
            entry = data["in_flight"].pop(f"pending:{run_id}", None) or {"run_id": run_id}
            entry["login"] = login
            data["in_flight"][machine_id] = entry
            self._save(data)

    def release(self, key: str, *, seconds: float = 0.0, config: FlyConfig | None = None,
                now: float | None = None) -> None:  # fmt: skip
        """Drop an in-flight entry and charge its machine-seconds."""
        now = time.time() if now is None else now
        with self._lock:
            data = self._load()
            self._roll(data, now)
            data["in_flight"].pop(key, None)
            if config is not None and seconds > 0:
                data["spend_usd"] = float(data.get("spend_usd") or 0.0) + (
                    seconds * config.rate_per_second_usd
                )
            self._save(data)

    def count_local_fallback(self) -> int:
        with self._lock:
            data = self._load()
            data["local_fallbacks"] = int(data.get("local_fallbacks") or 0) + 1
            self._save(data)
            return data["local_fallbacks"]


# ── Machines API client ─────────────────────────────────────────────────────


class FlyMachinesClient:
    """Thin async client for ``/v1/apps/<app>/machines``. Never logs headers;
    error messages carry the HTTP status and Fly's own error text only."""

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
                    ok: tuple[int, ...] = (200, 201, 202, 204)) -> Any:  # fmt: skip
        async with self._client() as client:
            resp = await client.request(method, path, params=params, json=body)
        if resp.status_code not in ok:
            try:
                message = str(resp.json().get("error") or resp.text)
            except ValueError:
                message = resp.text
            raise FlyApiError(resp.status_code, message)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    async def create(self, body: dict) -> dict:
        return await self._call("POST", "/machines", body=body)

    async def get(self, machine_id: str) -> dict | None:
        try:
            return await self._call("GET", f"/machines/{machine_id}")
        except FlyApiError as exc:
            if exc.status == 404:
                return None
            raise

    async def update(self, machine_id: str, body: dict) -> dict:
        return await self._call("POST", f"/machines/{machine_id}", body=body)

    async def start(self, machine_id: str) -> None:
        await self._call("POST", f"/machines/{machine_id}/start")

    async def stop(self, machine_id: str) -> None:
        try:
            await self._call("POST", f"/machines/{machine_id}/stop")
        except FlyApiError as exc:
            if exc.status != 404:
                raise

    async def destroy(self, machine_id: str) -> None:
        try:
            await self._call("DELETE", f"/machines/{machine_id}", params={"force": "true"})
        except FlyApiError as exc:
            if exc.status != 404:
                raise

    async def list(self) -> list[dict]:
        data = await self._call("GET", "/machines")
        return data if isinstance(data, list) else []

    async def wait(self, machine_id: str, *, state: str, instance_id: str = "",
                   timeout_s: int = 60) -> str:  # fmt: skip
        """One wait call. Returns ``"reached"``, ``"gone"`` or ``"timeout"``."""
        params: dict[str, Any] = {"state": state, "timeout": max(1, min(60, timeout_s))}
        if instance_id:
            params["instance_id"] = instance_id
        try:
            await self._call("GET", f"/machines/{machine_id}/wait", params=params)
            return "reached"
        except FlyApiError as exc:
            if exc.status == 404:
                return "gone"
            if exc.status == 408:
                return "timeout"
            # Anything else: read the machine's own state rather than guess.
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


@dataclass
class FlyHandle:
    job: FlyJob
    machine_id: str
    instance_id: str
    started: float
    deadline: float
    github_token: str = field(repr=False, default="")
    lock: asyncio.Lock | None = None


class FlyExecutor:
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

    async def _revoke_quietly(self, token: str) -> None:
        if not token:
            return
        try:
            await asyncio.to_thread(self._revoke, token)
        except Exception as exc:  # noqa: BLE001 — revocation is best-effort; it expires anyway
            log.warning(f"[fly] GitHub job token revocation failed: {type(exc).__name__}")

    async def launch(self, job: FlyJob) -> FlyHandle:
        """Mint, check caps, start the machine. Raises ``FlyIneligible``,
        ``FlyRefusal`` or ``FlyLaunchError``; on any of them nothing is left
        running and the minted token is revoked."""
        await self.maybe_reap()
        max_run = job.timeout + APIS_DEADLINE_GRACE_SECONDS
        lock: asyncio.Lock | None = None
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
        token = ""
        reserved = False
        try:
            if job.needs_github:
                token = await asyncio.to_thread(
                    self._mint, job.repository, self.config.github_app_prefix
                )
                job.secrets["GITHUB_TOKEN"] = token
                job.secrets["GH_TOKEN"] = token
                for name in ("GITHUB_TOKEN", "GH_TOKEN"):
                    if name not in job.child_credential_names:
                        job.child_credential_names.append(name)
            self.ledger.reserve(self.config, run_id=job.run_id, max_run_seconds=max_run,
                                now=self._clock())  # fmt: skip
            reserved = True
            started = self._clock()
            deadline = started + max_run
            if job.login:
                machine_id = self.config.login_machines[job.provider]
                await self._reconcile_login(machine_id)
                instance_id = await self._start_login_machine(machine_id, job, deadline)
            else:
                created = await self.client.create(per_job_machine_body(job, self.config, deadline))
                machine_id = str(created.get("id") or "")
                instance_id = str(created.get("instance_id") or "")
                if not machine_id:
                    raise FlyApiError(0, "create returned no machine id")
            self.ledger.bind(job.run_id, machine_id, login=job.login)
            log.info(
                f"[fly] run={job.run_id} role={job.role} provider={job.provider} "
                f"machine={machine_id} launched"
            )
            return FlyHandle(job, machine_id, instance_id, started, deadline, token, lock)
        except BaseException as exc:
            if reserved:
                self.ledger.release(f"pending:{job.run_id}")
            await self._revoke_quietly(token)
            if lock is not None and lock.locked():
                lock.release()
            if isinstance(exc, FlyApiError):
                raise FlyLaunchError(str(exc)) from None
            raise

    async def _start_login_machine(self, machine_id: str, job: FlyJob, deadline: float) -> str:
        machine = await self.client.get(machine_id)
        if machine is None:
            raise FlyApiError(404, f"{job.provider} login machine not found")
        cfg = copy.deepcopy(machine.get("config") or {})
        cfg["image"] = self.config.image
        cfg["auto_destroy"] = False
        cfg["restart"] = {"policy": "no"}
        cfg["env"] = machine_env(job, deadline)
        cfg["files"] = job_files(job, self.config)
        updated = await self.client.update(machine_id, {"config": cfg, "skip_launch": True})
        await self.client.start(machine_id)
        return str((updated or {}).get("instance_id") or "")

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

    async def collect(self, handle: FlyHandle) -> tuple[int, bytes, bytes]:
        """Wait for the machine to finish, confirm it is terminated, read the
        result record.

        Ownership rule: the ledger slot (and its reserved cost) and the login
        lock are released only after the machine is CONFIRMED terminated (a
        per-job machine destroyed, a login machine stopped). That holds on
        every exit path: normal exit, deadline, a wait that raises (a dropped
        connection), and cancellation. If termination cannot be confirmed,
        the slot stays reserved and is marked ``unconfirmed`` for the reaper,
        and a login machine is reconciled (stopped and confirmed) before its
        next job may use it. The job's GitHub token is revoked on every path.

        Raises ``asyncio.TimeoutError`` at the Apis deadline.
        """
        job = handle.job
        terminated = False
        try:
            try:
                outcome = await self._wait_until_done(handle)
            except (httpx.HTTPError, FlyApiError) as exc:
                # A dropped connection or API error while waiting is a FAILED
                # dispatch, not an exception for the caller: the failover loop
                # expects a result. The machine is terminated (or kept owned
                # and marked) in the finally below before anything is released.
                log.error(f"[fly] run={job.run_id} lost the wait on {handle.machine_id}: {exc}")
                return 1, b"", (
                    f"fly runner: error while waiting on machine {handle.machine_id} "
                    f"({type(exc).__name__}); the run's outcome is unknown"
                ).encode()
            if outcome == "deadline":
                log.error(
                    f"[fly] run={job.run_id} machine={handle.machine_id} passed its deadline"
                )
                terminated = await self._terminate(handle.machine_id, login=job.login)
                raise asyncio.TimeoutError()
            terminated = await self._terminate(handle.machine_id, login=job.login)
            record = await self._read_result(job.run_id)
            if not record:
                return 1, b"", f"fly runner: no result recorded for run {job.run_id}".encode()
            if record.get("probe_ok") is False:
                log.error(f"[fly] run={job.run_id} credential probe failed: {record.get('refused')}")
            try:
                rc = int(record.get("exit_code"))
            except (TypeError, ValueError):
                rc = 1
            stdout = str(record.get("stdout") or "").encode()
            stderr = str(record.get("stderr_tail") or "").encode()
            return rc, stdout, stderr
        finally:
            try:
                if not terminated:
                    terminated = await self._terminate(handle.machine_id, login=job.login)
                if terminated:
                    elapsed = max(0.0, self._clock() - handle.started)
                    self.ledger.release(handle.machine_id, seconds=elapsed, config=self.config)
                    if job.login:
                        await self._scrub_login_machine(handle.machine_id)
                else:
                    self.ledger.mark_unconfirmed(
                        handle.machine_id, reason="termination not confirmed after the job"
                    )
                    log.error(
                        f"[fly] run={job.run_id} machine={handle.machine_id}: termination "
                        "not confirmed; slot kept reserved and marked for the reaper"
                    )
                await self._revoke_quietly(handle.github_token)
            finally:
                # The in-process lock is always released: an unconfirmed login
                # machine stays owned through its ledger entry, and the next
                # launch reconciles it before reuse (``_reconcile_login``).
                if handle.lock is not None and handle.lock.locked():
                    handle.lock.release()
                job.secrets.clear()

    async def _terminate(self, machine_id: str, *, login: bool) -> bool:
        """Stop a login machine, or destroy a per-job one, and CONFIRM it.
        Returns False (never raises an ordinary error) when it cannot be
        confirmed."""
        try:
            machine = await self.client.get(machine_id)
            if machine is None or machine.get("state") == "destroyed":
                return True
            if login:
                if machine.get("state") == "stopped":
                    return True
                await self.client.stop(machine_id)
                result = await self.client.wait(machine_id, state="stopped", timeout_s=30)
                return result in ("reached", "gone")
            try:
                await self.client.stop(machine_id)
            except Exception as exc:  # noqa: BLE001 — destroy is forced regardless
                log.warning(f"[fly] stop before destroy failed: {type(exc).__name__}")
            await self.client.destroy(machine_id)
            after = await self.client.get(machine_id)
            return after is None or after.get("state") == "destroyed"
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[fly] could not confirm machine {machine_id} terminated: {exc}")
            return False

    async def _reconcile_login(self, machine_id: str) -> None:
        """Before a login machine takes a new job: any ledger entry for it is
        a previous job whose termination was never confirmed. Stop and
        confirm it, clear its job files, then release that entry; refuse the
        launch if it cannot be confirmed."""
        prior = self.ledger.entry(machine_id)
        if prior is None:
            return
        if not await self._terminate(machine_id, login=True):
            raise FlyApiError(409, "login machine from a previous job is not confirmed stopped")
        self.ledger.release(machine_id, seconds=_charged_seconds(prior, self._clock()),
                            config=self.config)  # fmt: skip
        await self._scrub_login_machine(machine_id)

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

    async def maybe_reap(self) -> list[str]:
        if self._clock() - self._last_reap < REAPER_INTERVAL_SECONDS:
            return []
        self._last_reap = self._clock()
        try:
            return await self.reap()
        except Exception as exc:  # noqa: BLE001 — the reaper never blocks a launch
            log.warning(f"[fly] reaper failed: {exc}")
            return []

    async def reap(self) -> list[str]:
        """Destroy machines older than the deadline or not in flight.

        Login machines are never reaped. Ledger entries whose machine no
        longer exists and whose deadline has passed are dropped.
        """
        now = self._clock()
        login_ids = set(self.config.login_machines.values())
        in_flight = self.ledger.snapshot(now)["in_flight"]
        max_age = (
            max((int(v.get("deadline", 0) - v.get("started_at", 0)) for v in in_flight.values()),
                default=0)  # fmt: skip
            or 1800 + APIS_DEADLINE_GRACE_SECONDS
        )
        reaped: list[str] = []
        # First, machines whose termination a previous job could not confirm.
        login_provider = {mid: prov for prov, mid in self.config.login_machines.items()}
        for key, entry in in_flight.items():
            if not entry.get("unconfirmed") or key.startswith("pending:"):
                continue
            login = bool(entry.get("login")) or key in login_provider
            if login and _login_lock(login_provider.get(key, "")).locked():
                continue  # a job holds that machine now; it reconciles first
            if await self._terminate(key, login=login):
                self.ledger.release(key, seconds=_charged_seconds(entry, now), config=self.config)
                if login:
                    await self._scrub_login_machine(key)
                reaped.append(key)
        in_flight = {k: v for k, v in in_flight.items() if k not in reaped}
        listed = await self.client.list()
        listed_ids = set()
        for machine in listed:
            mid = str(machine.get("id") or "")
            listed_ids.add(mid)
            if not mid or mid in login_ids or machine.get("state") == "destroyed":
                continue
            entry = in_flight.get(mid)
            run_id = str(((machine.get("config") or {}).get("metadata") or {}).get("ateles_run_id") or "")
            if entry is None and run_id and f"pending:{run_id}" in in_flight:
                # Created by a launch that has not bound it yet: in flight.
                entry = in_flight[f"pending:{run_id}"]
            created = _parse_ts(machine.get("created_at"))
            too_old = created is not None and now - created > max_age
            past_deadline = entry is not None and now > float(entry.get("deadline") or 0)
            if entry is None or too_old or past_deadline:
                await self.client.destroy(mid)
                self.ledger.release(mid)
                reaped.append(mid)
        for key, entry in in_flight.items():
            if key not in listed_ids and not key.startswith("pending:") and not entry.get("login"):
                if now > float(entry.get("deadline") or 0):
                    self.ledger.release(key)
        if reaped:
            log.warning(f"[fly] reaper destroyed {len(reaped)} stray machine(s): {reaped}")
        return reaped


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
