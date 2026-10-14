"""The Fly executor (ateles#1456): Machines API calls, the per-job credential
set, caps, the reaper, login machines, and the skill_runner seam.

The Fly API is an ``httpx.MockTransport``; GitHub minting/revocation and the
Neotoma result read are injected fakes. No test touches the network.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fly_runner  # noqa: E402
import skill_runner  # noqa: E402
from lib.daemon_runtime import AgentDefinition  # noqa: E402

FLY_TOKEN = "fly-deploy-token-canary-0001"
SHARED_BEARER = "shared-daemon-bearer-canary-0002"
SHARED_PAT = "shared-ateles-pat-canary-0003"
DAEMON_GH = "daemon-github-token-canary-0004"
MINTED = "ghs_minted_installation_token_0005"
CLAUDE_OAUTH = "claude-oauth-canary-0006"
CANARY = "operator-canary-value-0007"
FORBIDDEN_VALUES = (FLY_TOKEN, SHARED_BEARER, SHARED_PAT, DAEMON_GH, CANARY)


class FakeFly:
    """A stateful stand-in for the Machines API, with failure knobs."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.machines: dict[str, dict] = {}
        self.wait_status = 200
        self.create_status = 201
        self.next_id = 0
        # "ok" | "lost" (machine created, response 500) | "refused" (422, nothing
        # created) | "connect" (connection never made, nothing created)
        self.create_mode = "ok"
        self.start_lost = False  # the start takes effect, the response is a 500
        self.stop_effective = True  # a stop request actually stops the machine
        self.sticky: set[str] = set()  # delete acknowledged, machine keeps running
        self.sticky_all = False
        # Malformed answers: {"list": "malformed"|"object"|"null", "get": "null"|"other-id"}
        self.bad: dict[str, str] = {}

    @property
    def create_lost(self) -> bool:
        return self.create_mode == "lost"

    @create_lost.setter
    def create_lost(self, value: bool) -> None:
        self.create_mode = "lost" if value else "ok"

    @property
    def sticky_delete(self) -> bool:
        return self.sticky_all

    @sticky_delete.setter
    def sticky_delete(self, value: bool) -> None:
        self.sticky_all = value

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        method = request.method
        assert path.startswith("/v1/apps/runner-app/"), path
        rest = path[len("/v1/apps/runner-app"):]
        if method == "POST" and rest == "/machines":
            if self.create_mode == "connect":
                raise httpx.ConnectError("injected: connection refused")
            if self.create_mode == "refused":
                return httpx.Response(422, json={"error": "invalid config"})
            if self.create_status >= 400:
                return httpx.Response(self.create_status, json={"error": "no capacity"})
            self.next_id += 1
            mid = f"m{self.next_id}"
            self.machines[mid] = {"id": mid, "state": "started", **json.loads(request.content)}
            if self.create_mode == "lost":
                return httpx.Response(500, json={"error": "response lost"})
            return httpx.Response(201, json={"id": mid, "instance_id": f"i{self.next_id}"})
        if method == "GET" and rest == "/machines":
            mode = self.bad.get("list")
            if mode == "malformed":
                return httpx.Response(200, content=b"{not json")
            if mode == "object":
                return httpx.Response(200, json={})
            if mode == "null":
                return httpx.Response(200, json=None)
            return httpx.Response(200, json=list(self.machines.values()))
        parts = rest.split("/")  # ['', 'machines', id, ...]
        mid = parts[2]
        if method == "GET" and len(parts) == 4 and parts[3] == "wait":
            if not self.stop_effective and self.machines.get(mid, {}).get("state") == "started":
                return httpx.Response(408, json={"error": "still running"})  # never stops
            if self.wait_status != 200:
                return httpx.Response(self.wait_status, json={"error": "timeout"})
            if mid in self.machines and not self.machines[mid].get("login"):
                del self.machines[mid]  # auto_destroy
            elif mid in self.machines:
                self.machines[mid]["state"] = "stopped"  # run_job exited
            return httpx.Response(200, json={"ok": True})
        if method == "GET" and len(parts) == 3:
            if self.bad.get("get") == "null":
                return httpx.Response(200, json=None)
            if self.bad.get("get") == "other-id" and mid in self.machines:
                return httpx.Response(200, json={**self.machines[mid], "id": "someone-else"})
            if mid not in self.machines:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json=self.machines[mid])
        if method == "POST" and len(parts) == 3:  # update
            body = json.loads(request.content)
            self.machines.setdefault(mid, {"id": mid, "login": True, "state": "stopped"})[
                "config"
            ] = body["config"]
            return httpx.Response(200, json={"id": mid, "instance_id": "i-login"})
        if method == "POST" and parts[3:] == ["start"]:
            if mid in self.machines:
                self.machines[mid]["state"] = "started"
            if self.start_lost:
                return httpx.Response(500, json={"error": "response lost"})
            return httpx.Response(200, json={"ok": True})
        if method == "POST" and parts[3:] == ["stop"]:
            if mid in self.machines and self.stop_effective:
                self.machines[mid]["state"] = "stopped"
            return httpx.Response(200, json={"ok": True})
        if method == "DELETE":
            if not (self.sticky_all or mid in self.sticky):
                self.machines.pop(mid, None)
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(500, json={"error": f"unhandled {method} {rest}"})

    def calls(self, method: str, suffix: str = "") -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path.endswith(suffix)]


@pytest.fixture
def fly_env(monkeypatch, tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "lanius.jwk.json").write_text(json.dumps({
        "kty": "EC", "crv": "P-256", "x": "x", "y": "y", "d": "private-scalar-canary",
        "sub": "lanius@ateles-swarm",
    }))  # fmt: skip
    for name, value in {
        "APIS_FLY_APP": "runner-app",
        "APIS_FLY_API_TOKEN": FLY_TOKEN,
        "APIS_FLY_IMAGE": "registry.fly.io/runner-app:abc123",
        "APIS_FLY_REGION": "ams",
        "NEOTOMA_BASE_URL": "https://neotoma.example.test",
        "NEOTOMA_BEARER_TOKEN": SHARED_BEARER,
        "ATELES_AGENT_PAT": SHARED_PAT,
        "PAVO_AGENT_PAT": "pavo-pat-canary-0008",
        "GITHUB_TOKEN": DAEMON_GH,
        "GH_TOKEN": DAEMON_GH,
        "CLAUDE_CODE_OAUTH_TOKEN": CLAUDE_OAUTH,
        "ATELES_FLY_CANARY_SENTINEL": CANARY,
        "APIS_FLY_PROBE_ENV_NAMES": "ATELES_FLY_CANARY_SENTINEL",
        "ATELES_PRIVATE_KEYS_DIR": str(keys),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("LANIUS_NEOTOMA_TOKEN", raising=False)
    return tmp_path


class Clock:
    def __init__(self, start: float = 1_800_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


def make_executor(fake: FakeFly, *, record=None, clock=None, mint=None, **overrides):
    config = fly_runner.FlyConfig.from_env()
    for key, value in overrides.items():
        setattr(config, key, value)
    minted: list[str] = []
    revoked: list[str] = []

    def _mint(repo, prefix):
        minted.append(repo)
        return MINTED

    def _revoke(token):
        revoked.append(token)

    async def _read(run_id):
        if record is None:
            return None
        return {"run_id": run_id, **record}

    executor = fly_runner.FlyExecutor(
        config,
        client=fly_runner.FlyMachinesClient(config, transport=httpx.MockTransport(fake.handler)),
        mint=mint or _mint,
        revoke=_revoke,
        read_result=_read,
        clock=clock or Clock(),
    )
    executor._last_reap = float("inf")  # reaper tested on its own
    return executor, minted, revoked


def _job(provider="claude", **kw):
    return fly_runner.build_job(
        provider=provider,
        role="lanius",
        cmd=["/opt/homebrew/bin/claude", "--print", "--mcp-config", "/tmp/apis_mcp_x.json"],
        stdin=b"the task",
        timeout=60,
        repository="owner/repo",
        github_requested=True,
        mcp_config_path="/tmp/apis_mcp_x.json",
        **kw,
    )


def _files(body: dict) -> dict[str, bytes]:
    return {
        f["guest_path"]: base64.b64decode(f["raw_value"]) for f in body["config"]["files"]
    }


def _all_request_text(fake: FakeFly) -> str:
    return "\n".join(
        (r.content or b"").decode() + json.dumps(_decoded_files(r)) for r in fake.requests
    )


def _decoded_files(request: httpx.Request) -> list[str]:
    try:
        body = json.loads(request.content or b"{}")
    except ValueError:
        return []
    files = (body.get("config") or {}).get("files") or []
    return [base64.b64decode(f["raw_value"]).decode(errors="replace") for f in files]


# ── Default off ──────────────────────────────────────────────────────────────


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("APIS_FLY_RUNNER_CLASSES", raising=False)
    assert fly_runner.wants_fly(None, "carry_forward_check") is False
    assert fly_runner.wants_fly(None, None) is False
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "carry_forward_check")
    assert fly_runner.wants_fly(None, "carry_forward_check") is True
    assert fly_runner.wants_fly(None, "build") is False
    assert fly_runner.wants_fly("local", "carry_forward_check") is False
    assert fly_runner.wants_fly("fly", None) is True


# ── Per-job machine ──────────────────────────────────────────────────────────


def test_per_job_machine_config_and_credential_set(fly_env, caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeFly()
    executor, minted, revoked = make_executor(
        fake, record={"exit_code": 0, "stdout": "GATE_INHERITANCE: clear\n", "stderr_tail": ""}
    )
    job = _job()

    rc, out, err = asyncio.run(executor.run(job))
    assert (rc, out) == (0, b"GATE_INHERITANCE: clear\n")

    (create,) = fake.calls("POST", "/machines")
    body = json.loads(create.content)
    cfg = body["config"]
    assert cfg["auto_destroy"] is True
    assert cfg["restart"] == {"policy": "no"}
    assert cfg["guest"] == {"cpu_kind": "shared", "cpus": 2, "memory_mb": 2048}
    assert cfg["image"] == "registry.fly.io/runner-app:abc123"
    assert body["region"] == "ams"
    # PID 1 env: non-secret settings only.
    assert set(cfg["env"]) == {
        "ATELES_RUN_ID", "ATELES_RUN_ROLE", "ATELES_RUN_PROVIDER",
        "ATELES_RUN_REPOSITORY", "ATELES_RUN_DEADLINE",
    }  # fmt: skip

    files = _files(body)
    secret_names = {p.rsplit("/", 1)[1] for p in files if "/secrets/" in p}
    assert secret_names == {
        "GITHUB_TOKEN", "GH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "NEOTOMA_AAUTH_PRIVATE_JWK",
    }  # fmt: skip
    assert files["/run/job/secrets/GITHUB_TOKEN"].decode() == MINTED
    spec = json.loads(files["/run/job/job.json"])
    assert spec["neotoma_jwk_name"] == "NEOTOMA_AAUTH_PRIVATE_JWK"
    assert spec["neotoma_token_name"] == ""
    assert "NEOTOMA_BEARER_TOKEN" in spec["probe_env_names"]
    assert "ATELES_AGENT_PAT" in spec["probe_env_names"]
    assert "PAVO_AGENT_PAT" in spec["probe_env_names"]
    assert "ATELES_FLY_CANARY_SENTINEL" in spec["probe_env_names"]
    argv = json.loads(files["/run/job/argv.json"])
    assert argv[0] == "claude"
    assert fly_runner.MCP_CONFIG_PLACEHOLDER in argv
    assert "/tmp/apis_mcp_x.json" not in argv

    # Never the PAT, the daemon token, the shared bearer, the deploy token or
    # the canary, in any request body or file.
    text = _all_request_text(fake)
    for value in FORBIDDEN_VALUES:
        assert value not in text
    # The deploy token is only ever an Authorization header, and never logged.
    assert all(r.headers["authorization"] == f"Bearer {FLY_TOKEN}" for r in fake.requests)
    assert FLY_TOKEN not in caplog.text and MINTED not in caplog.text

    assert minted == ["owner/repo"]
    assert revoked == [MINTED]
    assert executor.ledger.snapshot()["in_flight"] == {}
    assert executor.ledger.snapshot()["spend_usd"] >= 0


def test_mcp_and_shared_token_never_ship_when_role_has_only_a_key(fly_env):
    job = _job()
    assert SHARED_BEARER not in json.dumps(job.secrets)
    assert "NEOTOMA_BEARER_TOKEN" not in job.secrets


@pytest.mark.parametrize("outcome", ["success", "failure", "no_record", "timeout"])
def test_the_job_token_is_revoked_on_every_exit_path(fly_env, outcome):
    fake = FakeFly()
    clock = Clock()
    record = {
        "success": {"exit_code": 0, "stdout": "ok"},
        "failure": {"exit_code": 1, "stdout": "", "stderr_tail": "boom"},
        "no_record": None,
        "timeout": None,
    }[outcome]
    if outcome == "timeout":
        fake.wait_status = 408
    executor, _, revoked = make_executor(fake, record=record, clock=clock)
    job = _job()

    real_wait = executor.client.wait

    async def advancing_wait(machine_id, **k):
        if k.get("instance_id") is None and k.get("timeout_s") == 30:
            return await real_wait(machine_id, **k)  # the stop confirmation
        clock.now += 120
        return "timeout"

    if outcome == "timeout":
        executor.client.wait = advancing_wait

    async def go():
        return await executor.run(job)

    if outcome == "timeout":
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(go())
        assert fake.calls("POST", "/stop")
        assert fake.calls("DELETE")
    else:
        rc, out, err = asyncio.run(go())
        if outcome == "success":
            assert rc == 0
        elif outcome == "failure":
            assert rc == 1 and err == b"boom"
        else:
            assert rc == 1 and b"no result recorded" in err
    assert revoked == [MINTED]
    assert executor.ledger.snapshot()["in_flight"] == {}


@pytest.mark.parametrize("mode", ["refused", "connect"])
def test_launch_that_provably_never_ran_is_a_plain_launch_failure(fly_env, mode):
    fake = FakeFly()
    fake.create_mode = mode
    executor, _, revoked = make_executor(fake, record=None)
    with pytest.raises(fly_runner.FlyLaunchError):
        asyncio.run(executor.run(_job()))
    assert fake.machines == {}
    assert revoked == [MINTED]
    assert executor.ledger.snapshot()["in_flight"] == {}
    assert executor.ledger.snapshot()["spend_usd"] == 0.0  # nothing ran, nothing charged


def test_mint_failure_refuses_fly_and_creates_nothing(fly_env):
    fake = FakeFly()

    def _fail(repo, prefix):
        raise fly_runner.FlyIneligible("GitHub App not configured")

    executor, _, revoked = make_executor(fake, mint=_fail)
    with pytest.raises(fly_runner.FlyIneligible, match="GitHub App"):
        asyncio.run(executor.run(_job()))
    assert fake.requests == []
    assert revoked == []


def test_mint_never_falls_back_to_a_pat(fly_env, monkeypatch):
    from lib import github_app_token as gat

    monkeypatch.setattr(gat, "app_configured", lambda prefix: False)
    with pytest.raises(fly_runner.FlyIneligible, match="never falls back to a PAT"):
        fly_runner.mint_job_github_token("owner/repo", "ATELES_REVIEWER_APP")


def test_mint_is_fresh_scoped_and_uncached(fly_env, monkeypatch):
    from lib import github_app_token as gat

    calls = []
    monkeypatch.setattr(gat, "app_configured", lambda prefix: True)
    monkeypatch.setattr(gat, "mint_app_jwt", lambda prefix: "app-jwt")
    monkeypatch.setattr(gat, "_resolve_installation_id", lambda prefix, repo, f: "42")

    def fake_request(method, path, bearer, body=None):
        calls.append((method, path, bearer, body))
        return {"token": f"tok{len(calls)}", "expires_at": "2030-01-01T00:00:00Z"}

    monkeypatch.setattr(fly_runner, "_github_request", fake_request)
    a = fly_runner.mint_job_github_token("owner/repo", "P")
    b = fly_runner.mint_job_github_token("owner/repo", "P")
    assert a != b  # one token per job, never a shared cached one
    assert calls[0][:3] == ("POST", "/app/installations/42/access_tokens", "app-jwt")
    assert calls[0][3] == {"repositories": ["repo"]}
    fly_runner.revoke_job_github_token(a)
    assert calls[-1][:3] == ("DELETE", "/installation/token", a)


# ── Eligibility ──────────────────────────────────────────────────────────────


def test_role_without_its_own_identity_is_not_eligible(fly_env, monkeypatch):
    monkeypatch.delenv("ATELES_PRIVATE_KEYS_DIR")
    with pytest.raises(fly_runner.FlyIneligible, match="shared daemon bearer never leaves"):
        _job()


def test_role_token_equal_to_the_shared_bearer_is_refused(fly_env, monkeypatch):
    monkeypatch.setenv("LANIUS_NEOTOMA_TOKEN", SHARED_BEARER)
    with pytest.raises(fly_runner.FlyIneligible, match="shared daemon bearer"):
        _job()


def test_own_token_is_shipped_by_name(fly_env, monkeypatch):
    monkeypatch.setenv("LANIUS_NEOTOMA_TOKEN", "lanius-own-token-0009")
    job = _job()
    assert job.neotoma_token_name == "LANIUS_NEOTOMA_TOKEN"
    assert job.secrets["LANIUS_NEOTOMA_TOKEN"] == "lanius-own-token-0009"
    assert "LANIUS_NEOTOMA_TOKEN" not in job.child_credential_names


@pytest.mark.parametrize("kw,match", [
    ({"cwd": "/tmp/wt"}, "working directory"),
    ({"local_review": True}, "local_review"),
    ({"owns_pending_gate": True}, "gate-owning"),
    ({"command_wrapper": ["/usr/bin/sandbox-exec", "-f", "/p"]}, "command_wrapper"),
])  # fmt: skip
def test_unsupported_shapes_stay_local(fly_env, kw, match):
    with pytest.raises(fly_runner.FlyIneligible, match=match):
        _job(**kw)


def test_unconfigured_fly_is_ineligible(fly_env, monkeypatch):
    monkeypatch.delenv("APIS_FLY_API_TOKEN")
    with pytest.raises(fly_runner.FlyIneligible, match="APIS_FLY_API_TOKEN"):
        _job()


def test_claude_without_setup_token_is_ineligible(fly_env, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    with pytest.raises(fly_runner.FlyIneligible, match="CLAUDE_CODE_OAUTH_TOKEN"):
        _job()


# ── Caps ─────────────────────────────────────────────────────────────────────


def test_budget_refusal_carries_cooled_until_next_month(fly_env):
    executor, _, revoked = make_executor(FakeFly(), budget_usd=0.0001)
    with pytest.raises(fly_runner.FlyRefusal) as exc:
        asyncio.run(executor.run(_job()))
    assert "budget" in str(exc.value)
    assert exc.value.cooled_until
    assert revoked == [MINTED]


def test_budget_counts_storage_and_one_max_run(fly_env, tmp_path):
    config = fly_runner.FlyConfig.from_env()
    config.login_machines = {"codex": "mc", "cursor": "mu"}
    ledger = fly_runner.FlyLedger(tmp_path / "l.json")
    storage = config.monthly_storage_usd()
    assert storage == pytest.approx(2 * 3 * 0.15)
    max_run = 1920
    worst = max_run * config.rate_per_second_usd
    config.budget_usd = storage + worst - 0.001
    with pytest.raises(fly_runner.FlyRefusal):
        ledger.reserve(config, run_id="r", owner="t", max_run_seconds=max_run)
    config.budget_usd = storage + worst + 0.001
    ledger.reserve(config, run_id="r", owner="t", max_run_seconds=max_run)


def test_daily_and_concurrency_caps(fly_env, tmp_path):
    config = fly_runner.FlyConfig.from_env()
    config.max_per_day = 2
    config.max_concurrent = 10
    ledger = fly_runner.FlyLedger(tmp_path / "l.json")
    ledger.reserve(config, run_id="a", owner="t", max_run_seconds=60, now=1_800_000_000)
    ledger.reserve(config, run_id="b", owner="t", max_run_seconds=60, now=1_800_000_000)
    with pytest.raises(fly_runner.FlyRefusal, match="daily"):
        ledger.reserve(config, run_id="c", owner="t", max_run_seconds=60, now=1_800_000_000)
    # Next UTC day resets the count.
    config.max_concurrent = 2
    with pytest.raises(fly_runner.FlyRefusal, match="concurrency"):
        ledger.reserve(config, run_id="d", owner="t", max_run_seconds=60,
                       now=1_800_000_000 + 86400)  # fmt: skip


def test_an_unreadable_ledger_fails_closed(fly_env, tmp_path):
    config = fly_runner.FlyConfig.from_env()
    path = tmp_path / "l.json"
    path.write_text("{not json")
    with pytest.raises(fly_runner.FlyRefusal, match="unreadable"):
        fly_runner.FlyLedger(path).reserve(config, run_id="a", owner="t", max_run_seconds=60)


def test_spend_accrues_from_machine_seconds(fly_env, tmp_path):
    config = fly_runner.FlyConfig.from_env()
    ledger = fly_runner.FlyLedger(tmp_path / "l.json")
    ledger.reserve(config, run_id="a", owner="t", max_run_seconds=7200, now=1000.0)
    ledger.set_machine("a", "m1")
    ledger.settle("a", config=config, now=1000.0 + 3600)
    assert ledger.snapshot(4600.0)["spend_usd"] == pytest.approx(13.90 / 720)


# ── Reaper ───────────────────────────────────────────────────────────────────


def test_reaper_destroys_strays_but_not_login_or_in_flight(fly_env):
    fake = FakeFly()
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock, login_machines={"codex": "m-login"})
    executor.ledger.reserve(executor.config, run_id="live", owner=executor.instance_id,
                            max_run_seconds=600, now=clock.now)  # fmt: skip
    executor.ledger.set_machine("live", "m-live")
    executor.ledger.reserve(executor.config, run_id="pending-run", owner=executor.instance_id,
                            max_run_seconds=600, now=clock.now)  # fmt: skip
    iso = "2027-01-15T08:00:00Z"
    for machine in (
        {"id": "m-stray", "state": "started", "created_at": iso},
        {"id": "m-login", "state": "stopped", "created_at": iso, "login": True},
        {"id": "m-live", "state": "started", "created_at": iso},
        {"id": "m-unbound", "state": "started", "created_at": iso,
         "config": {"metadata": {"ateles_run_id": "pending-run"}}},
    ):  # fmt: skip
        fake.machines[machine["id"]] = machine
    clock.now = fly_runner._parse_ts(iso) + 10
    reaped = asyncio.run(executor.reap())
    assert reaped == ["untracked-m-stray"]
    deleted = [r.url.path.rsplit("/", 1)[1] for r in fake.calls("DELETE")]
    assert deleted == ["m-stray"]
    assert set(fake.machines) == {"m-login", "m-live", "m-unbound"}


# ── Login machines (Codex, Cursor) ────────────────────────────────────────────


def test_codex_login_machine_is_updated_started_and_scrubbed(fly_env, monkeypatch):
    monkeypatch.setenv("APIS_FLY_CODEX_MACHINE_ID", "m-codex")
    fake = FakeFly()
    fake.machines["m-codex"] = {
        "id": "m-codex", "state": "stopped", "login": True,
        "config": {"image": "old", "mounts": [{"volume": "vol_x", "path": "/data"}]},
    }  # fmt: skip
    executor, _, revoked = make_executor(fake, record={"exit_code": 0, "stdout": "done"})
    job = fly_runner.build_job(
        provider="codex", role="lanius", cmd=["/x/codex", "exec", "-"], stdin=b"t",
        timeout=60, repository="owner/repo", github_requested=True, mcp_config_path=None,
    )  # fmt: skip
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in job.secrets
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY"):
        assert name not in job.secrets

    held = []

    async def read_and_check(run_id):
        held.append(fly_runner._login_lock("codex").locked())
        return {"run_id": run_id, "exit_code": 0, "stdout": "done"}

    executor._read_result = read_and_check
    assert asyncio.run(executor.run(job))[0] == 0
    assert held == [True]  # one job at a time while it ran
    assert not fake.calls("POST", "/machines")  # no new machine
    updates = [json.loads(r.content) for r in fake.requests
               if r.method == "POST" and r.url.path.endswith("/machines/m-codex")]  # fmt: skip
    assert len(updates) == 2
    first, scrub = updates
    assert first["config"]["mounts"] == [{"volume": "vol_x", "path": "/data"}]
    assert first["config"]["auto_destroy"] is False
    spec = json.loads(base64.b64decode(next(
        f["raw_value"] for f in first["config"]["files"] if f["guest_path"].endswith("job.json")
    )))  # fmt: skip
    assert spec["codex_home"] == "/data/codex"
    assert scrub["config"]["files"] == [] and scrub["config"]["env"] == {}
    assert fake.calls("POST", "/start")
    assert not fake.calls("DELETE")
    assert revoked == [MINTED]
    assert not fly_runner._login_lock("codex").locked()


def test_login_queue_wait_limit_returns_cooled_until(fly_env, monkeypatch):
    monkeypatch.setenv("APIS_FLY_CURSOR_MACHINE_ID", "m-cursor")
    executor, _, _ = make_executor(FakeFly(), queue_max_wait_seconds=0.01)
    job = fly_runner.build_job(
        provider="cursor", role="lanius", cmd=["/x/cursor-agent", "--print", "p"], stdin=b"",
        timeout=60, repository="owner/repo", github_requested=False, mcp_config_path=None,
    )  # fmt: skip

    async def go():
        await fly_runner._login_lock("cursor").acquire()
        await executor.run(job)

    with pytest.raises(fly_runner.FlyRefusal) as exc:
        asyncio.run(go())
    assert exc.value.cooled_until


def test_codex_without_a_login_machine_is_ineligible(fly_env):
    with pytest.raises(fly_runner.FlyIneligible, match="login machine"):
        fly_runner.build_job(
            provider="codex", role="lanius", cmd=["codex"], stdin=b"", timeout=60,
            repository="owner/repo", github_requested=True, mcp_config_path=None,
        )  # fmt: skip


# ── The skill_runner seam ─────────────────────────────────────────────────────


@pytest.fixture
def seam(monkeypatch, tmp_path):
    skill_dir = tmp_path / ".claude" / "skills" / "lanius"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# lanius\n")
    monkeypatch.setattr(skill_runner, "ATELES_REPO", tmp_path)
    monkeypatch.setattr(skill_runner, "CLAUDE_BIN", "/usr/bin/claude-test")
    monkeypatch.setattr(
        skill_runner, "_load_agent_def",
        lambda role: AgentDefinition(entity_id="e", name=role, prompt_markdown="You are lanius.",
                                     tool_allowlist="*", aauth_sub=f"{role}@ateles-swarm"),
    )  # fmt: skip
    monkeypatch.setattr(skill_runner, "_load_active_policy_prompt", lambda role: "")
    monkeypatch.setattr(skill_runner, "_write_harness_event", lambda **kw: None)
    spawned = []

    async def fake_exec(*cmd, **kwargs):
        spawned.append(list(cmd))

        class P:
            returncode = 0
            pid = 1

            async def communicate(self, input=None):
                return b"local output", b""

        return P()

    return spawned, fake_exec


def _once(**kw):
    return asyncio.run(skill_runner._run_skill_once(
        "lanius", "check the gates", provider="claude", role="lanius", **kw,
    ))  # fmt: skip


def test_seam_default_never_touches_fly(fly_env, seam, monkeypatch):
    spawned, fake_exec = seam
    monkeypatch.setattr(fly_runner, "get_executor", lambda: pytest.fail("Fly reached"))
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = _once(action_class=None)
    assert result.ok and result.stdout == "local output"
    assert len(spawned) == 1


def test_seam_forced_fly_refuses_an_ineligible_role(fly_env, seam, monkeypatch):
    spawned, fake_exec = seam
    monkeypatch.delenv("ATELES_PRIVATE_KEYS_DIR")
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = _once(runner="fly", repository="owner/repo",
                       github_token=None, include_github_contract=True)  # fmt: skip
    assert not result.ok
    assert result.error.startswith("fly runner refused: not eligible for Fly")
    assert spawned == []


def test_seam_opted_in_class_falls_back_locally_when_ineligible(fly_env, seam, monkeypatch):
    spawned, fake_exec = seam
    monkeypatch.delenv("ATELES_PRIVATE_KEYS_DIR")
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "carry_forward_check")
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = _once(action_class="carry_forward_check", model="m", repository="owner/repo",
                       github_token="t-local", include_github_contract=True)  # fmt: skip
    assert result.ok and result.stdout == "local output"
    assert len(spawned) == 1
    assert fly_runner.get_executor().ledger.snapshot()["local_fallbacks"] == 1


def test_seam_forced_fly_returns_the_machine_result(fly_env, seam, monkeypatch):
    spawned, fake_exec = seam
    fake = FakeFly()
    executor, minted, revoked = make_executor(
        fake, record={"exit_code": 0, "stdout": "GATE_INHERITANCE: clear\n"}
    )
    monkeypatch.setattr(fly_runner, "get_executor", lambda: executor)
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = _once(runner="fly", repository="owner/repo",
                       github_token=None, include_github_contract=True)  # fmt: skip
    assert result.ok, result.error
    assert result.stdout == "GATE_INHERITANCE: clear\n"
    assert spawned == []
    assert minted == ["owner/repo"] and revoked == [MINTED]
    files = _files(json.loads(fake.calls("POST", "/machines")[0].content))
    argv = json.loads(files["/run/job/argv.json"])
    assert argv[0] == "claude" and fly_runner.MCP_CONFIG_PLACEHOLDER in argv
    assert SHARED_BEARER not in _all_request_text(fake)


def test_seam_cap_refusal_returns_cooled_until(fly_env, seam, monkeypatch):
    spawned, fake_exec = seam
    executor, _, _ = make_executor(FakeFly(), budget_usd=0.0)
    monkeypatch.setattr(fly_runner, "get_executor", lambda: executor)
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = _once(runner="fly", repository="owner/repo", include_github_contract=True)
    assert not result.ok and result.cooled_until
    assert spawned == []


def test_seam_definite_launch_failure_falls_back_or_fails(fly_env, seam, monkeypatch):
    spawned, fake_exec = seam
    fake = FakeFly()
    fake.create_mode = "refused"  # provably nothing was created
    executor, _, _ = make_executor(fake)
    monkeypatch.setattr(fly_runner, "get_executor", lambda: executor)
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "carry_forward_check")
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        opted = _once(action_class="carry_forward_check", model="m", repository="owner/repo",
                      github_token="t-local", include_github_contract=True)  # fmt: skip
        forced = _once(runner="fly", repository="owner/repo", include_github_contract=True)
    assert opted.ok and len(spawned) == 1  # logged, counted local fallback
    assert not forced.ok and forced.error.startswith("fly runner refused: Fly launch failed")


@pytest.mark.parametrize("terminates", [True, False])
def test_seam_uncertain_launch_is_never_replayed_locally(fly_env, seam, monkeypatch, terminates):
    """A lost create response on an opted-in class with local fallback on:
    zero local executions, a non-success result naming the remote run and
    its machine state, and (when termination is unconfirmed) the reservation
    kept for the reaper."""
    spawned, fake_exec = seam
    events = []
    monkeypatch.setattr(skill_runner, "_write_harness_event", lambda **kw: events.append(kw))
    fake = FakeFly()
    fake.create_mode = "lost"
    fake.sticky_all = not terminates
    executor, _, revoked = make_executor(fake)
    assert executor.config.local_fallback is True
    monkeypatch.setattr(fly_runner, "get_executor", lambda: executor)
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "carry_forward_check")
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = _once(action_class="carry_forward_check", model="m", repository="owner/repo",
                       github_token="t-local", include_github_contract=True)  # fmt: skip
    assert spawned == []
    assert not result.ok and result.remote_outcome_unknown
    assert result.remote_run_id and "UNKNOWN outcome" in result.error
    assert "Not retried locally" in result.error
    state = "terminated" if terminates else "unconfirmed"
    assert result.remote_machine_state == state
    assert any("fly_outcome=unknown" in e.get("output_summary", "")
               and f"fly_machine_state={state}" in e.get("output_summary", "")
               for e in events)  # fmt: skip
    held = executor.ledger.snapshot()["in_flight"]
    assert (held == {}) is terminates
    if not terminates:
        assert held[result.remote_run_id]["unconfirmed"] is True
    assert revoked == [MINTED]


# ── dispatch_role --runner ────────────────────────────────────────────────────


def test_dispatch_role_runner_flag_reaches_run_skill(monkeypatch, tmp_path):
    import dispatch_role
    from skill_runner import SkillResult

    skills = tmp_path / ".claude" / "skills" / "lanius"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# lanius\n")
    monkeypatch.setattr(dispatch_role, "ATELES_REPO", tmp_path)
    seen = {}

    async def _capture(skill, prompt, **kwargs):
        seen.update(kwargs)
        return SkillResult(skill, True, 0, "out", "", provider="claude")

    monkeypatch.setattr(dispatch_role, "run_skill", _capture)
    monkeypatch.setattr(dispatch_role, "_load_agent_def", lambda role: AgentDefinition(name=role))
    rc = dispatch_role.main([
        "--role", "lanius", "--task", "t", "--runner", "fly",
        "--github-delivery", "--repository", "owner/repo",
    ])  # fmt: skip
    assert rc == 0
    assert seen["runner"] == "fly" and seen["repository"] == "owner/repo"
    assert seen["github_token"] is None

    rc = dispatch_role.main(["--role", "lanius", "--task", "t", "--runner", "fly",
                             "--github-delivery"])  # fmt: skip
    assert rc != 0


# ── Lifecycle ownership on every exit path ───────────────────────────────────


def _disconnect(*a, **k):
    raise httpx.ConnectError("injected disconnect")


def _async(fn):
    async def wrapper(*a, **k):
        return fn(*a, **k)

    return wrapper


def _codex_job():
    return fly_runner.build_job(
        provider="codex", role="lanius", cmd=["/x/codex", "exec", "-"], stdin=b"t",
        timeout=60, repository="owner/repo", github_requested=True, mcp_config_path=None,
    )  # fmt: skip


def _codex_fake(monkeypatch) -> FakeFly:
    monkeypatch.setenv("APIS_FLY_CODEX_MACHINE_ID", "m-codex")
    fake = FakeFly()
    fake.machines["m-codex"] = {"id": "m-codex", "state": "stopped", "login": True,
                                "config": {"mounts": []}}  # fmt: skip
    return fake


class RealWait:
    """Restore the client's real wait after a test replaced it."""

    def __init__(self, executor):
        self._real = fly_runner.FlyMachinesClient.wait.__get__(executor.client)

    async def __call__(self, *a, **k):
        return await self._real(*a, **k)


def test_wait_disconnect_destroys_per_job_machine_before_release(fly_env):
    fake = FakeFly()
    executor, _, revoked = make_executor(fake, record={"exit_code": 0, "stdout": "x"})
    executor.client.wait = _async(_disconnect)
    job = _job()
    rc, out, err = asyncio.run(executor.run(job))
    assert rc == 1 and b"error while waiting" in err  # a failed result, not a raise
    assert job.outcome_unknown is True and job.remote_state == "terminated"
    assert fake.calls("POST", "/stop") and fake.calls("DELETE")
    assert fake.machines == {}
    assert executor.ledger.snapshot()["in_flight"] == {}
    assert revoked == [MINTED]


def test_unconfirmed_termination_keeps_the_slot_for_the_reaper(fly_env):
    fake = FakeFly()
    clock = Clock()
    executor, _, revoked = make_executor(fake, record=None, clock=clock)
    executor.client.wait = _async(_disconnect)
    fake.sticky_all = True
    job = _job()
    asyncio.run(executor.run(job))
    assert job.remote_state == "unconfirmed"
    entry = executor.ledger.entry(job.run_id)
    assert entry is not None and entry["unconfirmed"] is True
    assert revoked == [MINTED]
    assert executor.ledger.snapshot(clock.now)["spend_usd"] == 0.0  # not settled yet

    fake.sticky_all = False
    clock.now += 30
    assert job.run_id in asyncio.run(executor.reap())
    assert executor.ledger.entry(job.run_id) is None
    assert executor.ledger.snapshot(clock.now)["spend_usd"] == pytest.approx(
        30 * executor.config.rate_per_second_usd
    )


def test_cancellation_still_terminates_before_release(fly_env):
    fake = FakeFly()
    executor, _, revoked = make_executor(fake, record=None)

    async def go():
        started = asyncio.Event()

        async def hanging_wait(machine_id, **k):
            started.set()
            await asyncio.Event().wait()

        executor.client.wait = hanging_wait
        task = asyncio.create_task(executor.run(_job()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert fake.calls("DELETE") and fake.machines == {}
    assert executor.ledger.snapshot()["in_flight"] == {}
    assert revoked == [MINTED]


def test_login_machine_disconnect_stops_and_unconfirmed_blocks_reuse(fly_env, monkeypatch):
    fake = _codex_fake(monkeypatch)
    fake.stop_effective = False  # the stop is never confirmed
    executor, _, revoked = make_executor(fake, record=None)
    executor.client.wait = _async(_disconnect)
    first = _codex_job()
    asyncio.run(executor.run(first))
    assert fake.calls("POST", "/stop")
    entry = executor.ledger.entry(first.run_id)
    assert entry is not None and entry["unconfirmed"] is True
    assert not fly_runner._login_lock("codex").locked()

    # The next job may not start on it while the stop is unconfirmed; nothing
    # was sent for it, so that is a plain (definite) launch failure.
    starts_before = len(fake.calls("POST", "/start"))
    with pytest.raises(fly_runner.FlyLaunchError, match="not confirmed stopped"):
        asyncio.run(executor.run(_codex_job()))
    assert len(fake.calls("POST", "/start")) == starts_before
    assert revoked == [MINTED, MINTED]

    # Once the machine stops, the next launch settles the old entry and runs.
    fake.stop_effective = True
    executor.client.wait = RealWait(executor)
    executor._read_result = _async(lambda run_id: {"run_id": run_id, "exit_code": 0})
    assert asyncio.run(executor.run(_codex_job()))[0] == 0
    assert executor.ledger.snapshot()["in_flight"] == {}


@pytest.mark.parametrize("stop_effective", [True, False])
def test_login_start_response_lost_is_uncertain_and_owned(fly_env, monkeypatch, stop_effective):
    fake = _codex_fake(monkeypatch)
    fake.start_lost = True  # the machine starts; the response says 500
    fake.stop_effective = stop_effective
    executor, _, revoked = make_executor(fake, record=None)
    job = _codex_job()
    with pytest.raises(fly_runner.FlyUncertainLaunch):
        asyncio.run(executor.run(job))
    assert fake.calls("POST", "/stop")
    assert revoked == [MINTED]
    assert not fly_runner._login_lock("codex").locked()
    entry = executor.ledger.entry(job.run_id)
    if stop_effective:
        assert job.remote_state == "terminated"
        assert fake.machines["m-codex"]["state"] == "stopped"
        assert entry is None
        assert fake.machines["m-codex"]["config"]["files"] == []
    else:
        assert job.remote_state == "unconfirmed"
        assert fake.machines["m-codex"]["state"] == "started"
        assert entry is not None and entry["unconfirmed"] is True
        fake.start_lost = False
        with pytest.raises(fly_runner.FlyLaunchError, match="not confirmed stopped"):
            asyncio.run(executor.run(_codex_job()))


def test_create_response_lost_is_uncertain_and_the_machine_is_found_by_run_id(fly_env):
    fake = FakeFly()
    fake.create_mode = "lost"
    executor, _, revoked = make_executor(fake, record=None)
    job = _job()
    with pytest.raises(fly_runner.FlyUncertainLaunch):
        asyncio.run(executor.run(job))
    assert job.remote_state == "terminated"
    assert fake.calls("DELETE") and fake.machines == {}
    assert executor.ledger.snapshot()["in_flight"] == {}
    assert revoked == [MINTED]


@pytest.mark.parametrize("bad", [{"list": "malformed"}, {"list": "object"}, {"list": "null"}])
def test_an_invalid_listing_never_confirms_termination(fly_env, bad):
    fake = FakeFly()
    fake.create_mode = "lost"
    fake.bad = bad
    executor, _, _ = make_executor(fake, record=None)
    job = _job()
    with pytest.raises(fly_runner.FlyUncertainLaunch):
        asyncio.run(executor.run(job))
    assert job.remote_state == "unconfirmed"
    assert executor.ledger.entry(job.run_id)["unconfirmed"] is True
    assert fake.machines  # still there, still owned


@pytest.mark.parametrize("bad", [{"get": "null"}, {"get": "other-id"}])
def test_an_invalid_machine_read_never_confirms_termination(fly_env, bad):
    fake = FakeFly()
    executor, _, _ = make_executor(fake, record=None)
    executor.client.wait = _async(_disconnect)
    fake.sticky_all = True
    fake.bad = bad
    job = _job()
    asyncio.run(executor.run(job))
    assert job.remote_state == "unconfirmed"
    assert executor.ledger.entry(job.run_id)["unconfirmed"] is True


def test_create_response_lost_and_unlisted_keeps_the_slot(fly_env):
    fake = FakeFly()
    fake.create_mode = "lost"
    executor, _, _ = make_executor(fake, record=None)

    async def no_list():
        raise httpx.ConnectError("injected list failure")

    executor.client.list = no_list
    job = _job()
    with pytest.raises(fly_runner.FlyUncertainLaunch):
        asyncio.run(executor.run(job))
    assert executor.ledger.entry(job.run_id)["unconfirmed"] is True


def test_cancellation_during_login_start_settles_the_machine(fly_env, monkeypatch):
    fake = _codex_fake(monkeypatch)
    executor, _, revoked = make_executor(fake, record=None)

    async def go():
        entered = asyncio.Event()

        async def hanging_start(machine_id):
            fake.machines[machine_id]["state"] = "started"
            entered.set()
            await asyncio.Event().wait()

        executor.client.start = hanging_start
        task = asyncio.create_task(executor.run(_codex_job()))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert fake.machines["m-codex"]["state"] == "stopped"
    assert executor.ledger.snapshot()["in_flight"] == {}
    assert not fly_runner._login_lock("codex").locked()
    assert revoked == [MINTED]


# ── Recovery: explicit ownership, one settlement path ────────────────────────


def _seed(executor, clock, *, run_id="old-run", machine_id="m-old", max_run=600, owner=None,
          started=None):  # fmt: skip
    executor.ledger.reserve(executor.config, run_id=run_id,
                            owner=owner or executor.instance_id, max_run_seconds=max_run,
                            now=clock.now if started is None else started)  # fmt: skip
    executor.ledger.set_machine(run_id, machine_id)


@pytest.mark.parametrize("listed", [True, False])
def test_reaper_settles_an_overdue_reservation_and_charges_it(fly_env, listed):
    fake = FakeFly()
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock)
    _seed(executor, clock)
    if listed:
        fake.machines["m-old"] = {"id": "m-old", "state": "started"}
    clock.now += 600 + fly_runner.REAP_GRACE_SECONDS + 1
    assert "old-run" in asyncio.run(executor.reap())
    assert fake.machines == {}
    snap = executor.ledger.snapshot(clock.now)
    assert snap["in_flight"] == {}
    assert snap["spend_usd"] == pytest.approx(600 * executor.config.rate_per_second_usd)
    asyncio.run(executor.reap())  # exactly once
    assert executor.ledger.snapshot(clock.now)["spend_usd"] == pytest.approx(snap["spend_usd"])


def test_reaper_waits_out_the_grace_period_for_this_process(fly_env):
    fake = FakeFly()
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock)
    _seed(executor, clock)
    fake.machines["m-old"] = {"id": "m-old", "state": "started"}
    clock.now += 600 + fly_runner.REAP_GRACE_SECONDS - 1  # overdue, inside the grace
    assert asyncio.run(executor.reap()) == []
    assert "m-old" in fake.machines


def test_reaper_settles_a_previous_process_reservation_at_once(fly_env):
    fake = FakeFly()
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock)
    _seed(executor, clock, owner="apis-previous-process")
    fake.machines["m-old"] = {"id": "m-old", "state": "started"}
    assert "old-run" in asyncio.run(executor.reap())  # not overdue, but its owner is gone
    assert fake.machines == {}


def test_reaper_never_touches_a_live_run_even_past_its_deadline(fly_env):
    fake = FakeFly()
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock, record={"exit_code": 0, "stdout": "ok"})
    job = _job()

    async def go():
        in_wait = asyncio.Event()
        release = asyncio.Event()
        real = RealWait(executor)

        async def gated_wait(machine_id, **k):
            in_wait.set()
            await release.wait()
            return await real(machine_id, **k)

        executor.client.wait = gated_wait
        task = asyncio.create_task(executor.run(job))
        await in_wait.wait()
        clock.now += 100_000  # far past deadline + grace
        reaped = await executor.reap()
        assert reaped == [] and job.machine_id in fake.machines
        clock.now -= 100_000
        release.set()
        return await task

    assert asyncio.run(go())[0] == 0


def test_reaper_racing_a_concurrent_launch_does_not_touch_its_machine(fly_env):
    """The arch probe: a launch reserves and creates while the reaper runs.
    The reaper must neither destroy the new machine nor charge it."""
    fake = FakeFly()
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock, record={"exit_code": 0, "stdout": "ok"})
    job = _job()

    async def go():
        creating = asyncio.Event()
        finish_create = asyncio.Event()
        in_wait = asyncio.Event()
        release = asyncio.Event()
        real_create = executor.client.create
        real_wait = RealWait(executor)

        async def slow_create(body):
            creating.set()
            await finish_create.wait()
            return await real_create(body)

        async def gated_wait(machine_id, **k):
            in_wait.set()
            await release.wait()
            return await real_wait(machine_id, **k)

        executor.client.create = slow_create
        executor.client.wait = gated_wait
        run_task = asyncio.create_task(executor.run(job))
        await creating.wait()  # reserved, create in flight
        reap_task = asyncio.create_task(executor.reap())
        await asyncio.sleep(0.01)
        assert not reap_task.done()  # admission is held by the launch
        finish_create.set()
        await in_wait.wait()
        reaped = await reap_task
        assert reaped == []
        assert job.machine_id in fake.machines
        assert executor.ledger.entry(job.run_id) is not None
        assert not fake.calls("DELETE")
        release.set()
        return await run_task

    assert asyncio.run(go())[0] == 0
    assert executor.ledger.snapshot()["in_flight"] == {}


def test_reaper_keeps_the_slot_when_a_delete_ack_is_not_confirmed(fly_env):
    fake = FakeFly()
    fake.sticky_all = True
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock, max_concurrent=1)
    _seed(executor, clock)
    fake.machines["m-old"] = {"id": "m-old", "state": "started"}
    clock.now += 600 + fly_runner.REAP_GRACE_SECONDS + 1
    assert asyncio.run(executor.reap()) == []
    entry = executor.ledger.entry("old-run")
    assert entry is not None and entry["unconfirmed"] is True
    assert executor.ledger.snapshot(clock.now)["spend_usd"] == 0.0
    with pytest.raises(fly_runner.FlyRefusal, match="concurrency"):
        executor.ledger.reserve(executor.config, run_id="next", owner="t", max_run_seconds=60,
                                now=clock.now)  # fmt: skip
    fake.sticky_all = False
    assert "old-run" in asyncio.run(executor.reap())
    executor.ledger.reserve(executor.config, run_id="next", owner="t", max_run_seconds=60,
                            now=clock.now)  # fmt: skip


def test_an_untracked_machine_that_will_not_die_holds_capacity(fly_env):
    """The qa probe: an empty ledger, one running machine nobody accounts for
    that survives stop and delete, and max_concurrent=1. The reaper must own
    it (unconfirmed, counted) so the next job is refused, then settle it once
    it is confirmed gone."""
    fake = FakeFly()
    fake.sticky_all = True
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock, max_concurrent=1)
    fake.machines["m-ghost"] = {"id": "m-ghost", "state": "started",
                                "created_at": "2027-01-15T07:59:00Z"}  # fmt: skip
    assert asyncio.run(executor.reap()) == []
    held = executor.ledger.snapshot(clock.now)["in_flight"]
    assert held["untracked-m-ghost"]["unconfirmed"] is True
    with pytest.raises(fly_runner.FlyRefusal, match="concurrency"):
        executor.ledger.reserve(executor.config, run_id="next", owner="t", max_run_seconds=60,
                                now=clock.now)  # fmt: skip
    fake.sticky_all = False
    assert "untracked-m-ghost" in asyncio.run(executor.reap())
    snap = executor.ledger.snapshot(clock.now)
    assert snap["in_flight"] == {}
    assert snap["spend_usd"] == pytest.approx(60 * executor.config.rate_per_second_usd)


def test_reaped_spend_counts_toward_admission(fly_env):
    fake = FakeFly()
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock)
    _seed(executor, clock, max_run=1920)
    one = 1920 * executor.config.rate_per_second_usd
    data = executor.ledger._load()
    executor.ledger._roll(data, clock.now)
    data["spend_usd"] = 20.0 - one - one / 2
    executor.ledger._save(data)
    clock.now += 1920 + fly_runner.REAP_GRACE_SECONDS + 1
    asyncio.run(executor.reap())
    with pytest.raises(fly_runner.FlyRefusal, match="budget"):
        executor.ledger.reserve(executor.config, run_id="n", owner="t", max_run_seconds=1920,
                                now=clock.now)  # fmt: skip


def test_settle_is_idempotent(fly_env, tmp_path):
    config = fly_runner.FlyConfig.from_env()
    ledger = fly_runner.FlyLedger(tmp_path / "l.json")
    ledger.reserve(config, run_id="a", owner="t", max_run_seconds=100, now=1000.0)
    first = ledger.settle("a", config=config, now=1050.0)
    second = ledger.settle("a", config=config, now=1050.0)
    assert first == pytest.approx(50 * config.rate_per_second_usd) and second == 0.0


# ── Token lifecycle ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("cancels", [1, 2])
def test_token_minted_after_cancellation_is_still_revoked(fly_env, cancels):
    """The mint completes only after the dispatch was cancelled (once or
    repeatedly) and after run() finished: the minting thread revokes it."""
    import threading

    release = threading.Event()
    done = threading.Event()
    fake = FakeFly()

    def slow_mint(repo, prefix):
        release.wait(5)
        return MINTED

    executor, _, revoked = make_executor(fake, mint=slow_mint)
    real_revoke = executor._revoke

    def revoke_and_flag(token):
        real_revoke(token)
        done.set()

    executor._revoke = revoke_and_flag

    async def go():
        task = asyncio.create_task(executor.run(_job()))
        await asyncio.sleep(0.05)  # the mint is in flight in its thread
        for _ in range(cancels):
            task.cancel()
            await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError):
            await task

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(go())
        assert revoked == []  # run() is over; the mint has not returned yet
        release.set()
        assert done.wait(5)
    finally:
        loop.close()
    assert revoked == [MINTED]
    assert fake.requests == []


@pytest.mark.parametrize("failure", ["refusal", "launch_error", "reconcile_refused", "uncertain"])
def test_token_is_revoked_when_the_job_never_completes(fly_env, monkeypatch, failure):
    if failure == "reconcile_refused":
        fake = _codex_fake(monkeypatch)
        fake.stop_effective = False
        fake.machines["m-codex"]["state"] = "started"
        executor, _, revoked = make_executor(fake)
        executor.ledger.reserve(executor.config, run_id="old", owner="apis-other",
                                max_run_seconds=60, login=True, machine_id="m-codex")  # fmt: skip
        job = _codex_job()
        expected = fly_runner.FlyLaunchError
    else:
        fake = FakeFly()
        fake.create_mode = {"launch_error": "refused", "uncertain": "lost"}.get(failure, "ok")
        executor, _, revoked = make_executor(
            fake, **({"budget_usd": 0.0} if failure == "refusal" else {})
        )
        job = _job()
        expected = {
            "refusal": fly_runner.FlyRefusal,
            "launch_error": fly_runner.FlyLaunchError,
            "uncertain": fly_runner.FlyUncertainLaunch,
        }[failure]
    with pytest.raises(expected):
        asyncio.run(executor.run(job))
    assert revoked == [MINTED]
    assert job.secrets == {}


# ── Oversized output is never silent ──────────────────────────────────────────


def test_truncated_output_is_flagged_and_marked(fly_env, caplog):
    fake = FakeFly()
    executor, _, _ = make_executor(fake, record={
        "exit_code": 0, "stdout": "tail of report\nGATE_INHERITANCE: clear\n",
        "stdout_truncated": True,
    })  # fmt: skip
    job = _job()
    with caplog.at_level(logging.WARNING):
        rc, out, err = asyncio.run(executor.run(job))
    assert rc == 0
    assert job.stdout_truncated is True
    first_line = out.decode().splitlines()[0]
    assert "output truncated" in first_line and job.run_id in first_line
    assert out.decode().rstrip().endswith("GATE_INHERITANCE: clear")  # verdict still last
    assert b"output truncated" in err
    assert any("output truncated" in r.getMessage() for r in caplog.records
               if r.levelno == logging.WARNING)  # fmt: skip


def test_complete_output_is_not_flagged(fly_env):
    executor, _, _ = make_executor(FakeFly(), record={"exit_code": 0, "stdout": "all\n",
                                                      "stdout_truncated": False})  # fmt: skip
    job = _job()
    rc, out, err = asyncio.run(executor.run(job))
    assert out == b"all\n" and job.stdout_truncated is False


def test_seam_surfaces_truncation_on_the_result_and_harness_event(fly_env, seam, monkeypatch):
    spawned, fake_exec = seam
    events = []
    monkeypatch.setattr(skill_runner, "_write_harness_event", lambda **kw: events.append(kw))
    executor, _, _ = make_executor(FakeFly(), record={
        "exit_code": 0, "stdout": "GATE_INHERITANCE: clear\n", "stdout_truncated": True,
    })  # fmt: skip
    monkeypatch.setattr(fly_runner, "get_executor", lambda: executor)
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = _once(runner="fly", repository="owner/repo", include_github_contract=True)
    assert result.ok and result.output_truncated is True
    assert result.stdout.startswith("[fly runner: output truncated")
    final = [e for e in events if e.get("success") == "true"]
    assert final and "stdout_truncated=true" in final[-1]["output_summary"]


# ── Review round 1: in-flight reservations count against the budget ──────────


def test_budget_counts_every_in_flight_reservation(fly_env, tmp_path):
    """The round-1 probe: $19.98 spent, three concurrent 1,920 s runs at the
    default rate and concurrency. Only one may be admitted."""
    config = fly_runner.FlyConfig.from_env()
    assert config.max_concurrent == 3 and config.budget_usd == 20.0
    ledger = fly_runner.FlyLedger(tmp_path / "ledger.json")
    now = 1_800_000_000.0
    data = ledger._load()
    ledger._roll(data, now)
    data["spend_usd"] = 19.98
    ledger._save(data)
    max_run = 1920
    admitted = 0
    for i in range(3):
        try:
            ledger.reserve(config, run_id=f"r{i}", owner="t", max_run_seconds=max_run, now=now)
            admitted += 1
        except fly_runner.FlyRefusal as exc:
            assert "reserved in flight" in str(exc)
    assert admitted == 1
    ledger.settle("r0", config=config, now=now + 60)
    ledger.reserve(config, run_id="r3", owner="t", max_run_seconds=max_run, now=now + 60)
