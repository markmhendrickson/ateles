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
    """A stateful stand-in for the Machines API."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.machines: dict[str, dict] = {}
        self.wait_status = 200
        self.create_status = 201
        self.listed: list[dict] = []
        self.next_id = 0
        self.start_lost = False  # the start takes effect, the response is a 500
        self.create_lost = False  # the create takes effect, the response is a 500
        self.stop_effective = True  # a stop request actually stops the machine
        self.sticky_delete = False  # a delete is acknowledged but nothing happens

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        method = request.method
        assert path.startswith("/v1/apps/runner-app/"), path
        rest = path[len("/v1/apps/runner-app"):]
        if method == "POST" and rest == "/machines":
            if self.create_status >= 400:
                return httpx.Response(self.create_status, json={"error": "no capacity"})
            self.next_id += 1
            mid = f"m{self.next_id}"
            self.machines[mid] = {"id": mid, "state": "started", **json.loads(request.content)}
            if self.create_lost:
                return httpx.Response(500, json={"error": "response lost"})
            return httpx.Response(201, json={"id": mid, "instance_id": f"i{self.next_id}"})
        if method == "GET" and rest == "/machines":
            return httpx.Response(200, json=self.listed or list(self.machines.values()))
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
            if mid not in self.machines:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json=self.machines[mid])
        if method == "POST" and len(parts) == 3:  # update
            body = json.loads(request.content)
            self.machines.setdefault(mid, {"id": mid, "login": True})["config"] = body["config"]
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
            if not self.sticky_delete:
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


def test_launch_failure_revokes_and_leaves_nothing_in_flight(fly_env):
    fake = FakeFly()
    fake.create_status = 503
    executor, _, revoked = make_executor(fake, record=None)
    with pytest.raises(fly_runner.FlyLaunchError):
        asyncio.run(executor.run(_job()))
    assert revoked == [MINTED]
    assert executor.ledger.snapshot()["in_flight"] == {}


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
        ledger.reserve(config, run_id="r", max_run_seconds=max_run)
    config.budget_usd = storage + worst + 0.001
    ledger.reserve(config, run_id="r", max_run_seconds=max_run)


def test_daily_and_concurrency_caps(fly_env, tmp_path):
    config = fly_runner.FlyConfig.from_env()
    config.max_per_day = 2
    config.max_concurrent = 10
    ledger = fly_runner.FlyLedger(tmp_path / "l.json")
    ledger.reserve(config, run_id="a", max_run_seconds=60, now=1_800_000_000)
    ledger.reserve(config, run_id="b", max_run_seconds=60, now=1_800_000_000)
    with pytest.raises(fly_runner.FlyRefusal, match="daily"):
        ledger.reserve(config, run_id="c", max_run_seconds=60, now=1_800_000_000)
    # Next UTC day resets the count.
    config.max_concurrent = 2
    with pytest.raises(fly_runner.FlyRefusal, match="concurrency"):
        ledger.reserve(config, run_id="d", max_run_seconds=60, now=1_800_000_000 + 86400)


def test_spend_accrues_from_machine_seconds(fly_env, tmp_path):
    config = fly_runner.FlyConfig.from_env()
    ledger = fly_runner.FlyLedger(tmp_path / "l.json")
    ledger.reserve(config, run_id="a", max_run_seconds=60)
    ledger.bind("a", "m1", login=False)
    ledger.release("m1", seconds=3600, config=config)
    assert ledger.snapshot()["spend_usd"] == pytest.approx(13.90 / 720)


# ── Reaper ───────────────────────────────────────────────────────────────────


def test_reaper_destroys_strays_but_not_login_or_in_flight(fly_env):
    fake = FakeFly()
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock, login_machines={"codex": "m-login"})
    executor.ledger.reserve(executor.config, run_id="live", max_run_seconds=600, now=clock.now)
    executor.ledger.bind("live", "m-live", login=False)
    iso = "2027-01-15T08:00:00Z"
    for machine in (
        {"id": "m-stray", "state": "started", "created_at": iso},
        {"id": "m-login", "state": "stopped", "created_at": iso, "login": True},
        {"id": "m-live", "state": "started", "created_at": iso},
        {"id": "m-unbound", "state": "started", "created_at": iso,
         "config": {"metadata": {"ateles_run_id": "pending-run"}}},
    ):  # fmt: skip
        fake.machines[machine["id"]] = machine
    executor.ledger.reserve(executor.config, run_id="pending-run", max_run_seconds=600,
                            now=clock.now)  # fmt: skip
    clock.now = fly_runner._parse_ts(iso) + 10
    reaped = asyncio.run(executor.reap())
    assert reaped == ["m-stray"]
    deleted = [r.url.path.rsplit("/", 1)[1] for r in fake.calls("DELETE")]
    assert deleted == ["m-stray"]


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


def test_seam_launch_failure_falls_back_or_fails(fly_env, seam, monkeypatch):
    spawned, fake_exec = seam
    fake = FakeFly()
    fake.create_status = 500
    executor, _, _ = make_executor(fake)
    monkeypatch.setattr(fly_runner, "get_executor", lambda: executor)
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "carry_forward_check")
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        opted = _once(action_class="carry_forward_check", model="m", repository="owner/repo",
                      github_token="t-local", include_github_contract=True)  # fmt: skip
        forced = _once(runner="fly", repository="owner/repo", include_github_contract=True)
    assert opted.ok and len(spawned) == 1  # logged, counted local fallback
    assert not forced.ok and forced.error.startswith("fly runner refused: Fly launch failed")


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


# ── Lifecycle ownership on every exit path (review rounds 1 and 2) ───────────


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


def test_wait_disconnect_destroys_per_job_machine_before_release(fly_env):
    fake = FakeFly()
    executor, _, revoked = make_executor(fake, record={"exit_code": 0, "stdout": "x"})
    executor.client.wait = _async(_disconnect)
    rc, out, err = asyncio.run(executor.run(_job()))
    assert rc == 1 and b"error while waiting" in err  # a failed result, not a raise
    assert fake.calls("POST", "/stop") and fake.calls("DELETE")
    assert fake.machines == {}
    # Released only because destruction was confirmed (GET -> 404).
    assert executor.ledger.snapshot()["in_flight"] == {}
    assert revoked == [MINTED]


def test_unconfirmed_termination_keeps_the_slot_for_the_reaper(fly_env):
    fake = FakeFly()
    clock = Clock()
    executor, _, revoked = make_executor(fake, record=None, clock=clock)
    real_destroy = executor.client.destroy

    async def failing_destroy(mid):
        raise fly_runner.FlyApiError(500, "injected destroy failure")

    executor.client.wait = _async(_disconnect)
    executor.client.destroy = failing_destroy
    asyncio.run(executor.run(_job()))
    (mid,) = fake.machines
    entry = executor.ledger.entry(mid)
    assert entry is not None and entry["unconfirmed"] is True
    assert revoked == [MINTED]  # the token is revoked regardless
    assert executor.ledger.snapshot(clock.now)["spend_usd"] == 0.0  # not settled yet

    # The reaper, once the API answers, destroys it, confirms, charges, releases.
    executor.client.destroy = real_destroy
    clock.now += 30
    assert mid in asyncio.run(executor.reap())
    assert executor.ledger.entry(mid) is None
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
    asyncio.run(executor.run(_codex_job()))
    assert fake.calls("POST", "/stop")  # a stop was attempted
    entry = executor.ledger.entry("m-codex")
    assert entry is not None and entry["unconfirmed"] is True
    assert not fly_runner._login_lock("codex").locked()
    assert revoked == [MINTED]

    # The next job may not start on it while the stop is unconfirmed.
    starts_before = len(fake.calls("POST", "/start"))
    with pytest.raises(fly_runner.FlyLaunchError, match="not confirmed stopped"):
        asyncio.run(executor.run(_codex_job()))
    assert len(fake.calls("POST", "/start")) == starts_before
    assert revoked == [MINTED, MINTED]  # the refused job's token too

    # Once the machine stops, the next launch settles the old entry and runs.
    fake.stop_effective = True
    executor.client.wait = FakeFlyWaitOk(executor)
    executor._read_result = _async(lambda run_id: {"run_id": run_id, "exit_code": 0})
    assert asyncio.run(executor.run(_codex_job()))[0] == 0
    assert executor.ledger.snapshot()["in_flight"] == {}


class FakeFlyWaitOk:
    """Restore the client's real wait after a test replaced it."""

    def __init__(self, executor):
        self._real = fly_runner.FlyMachinesClient.wait.__get__(executor.client)

    async def __call__(self, *a, **k):
        return await self._real(*a, **k)


@pytest.mark.parametrize("stop_effective", [True, False])
def test_login_start_response_lost_keeps_ownership_until_stopped(
    fly_env, monkeypatch, stop_effective
):
    fake = _codex_fake(monkeypatch)
    fake.start_lost = True  # the machine starts; the response says 500
    fake.stop_effective = stop_effective
    executor, _, revoked = make_executor(fake, record=None)
    with pytest.raises(fly_runner.FlyLaunchError):
        asyncio.run(executor.run(_codex_job()))
    assert fake.calls("POST", "/stop")  # the uncertain start is reconciled
    assert revoked == [MINTED]
    assert not fly_runner._login_lock("codex").locked()
    entry = executor.ledger.entry("m-codex")
    if stop_effective:
        assert fake.machines["m-codex"]["state"] == "stopped"
        assert entry is None  # settled only after the stop was confirmed
        assert fake.machines["m-codex"]["config"]["files"] == []  # job files cleared
    else:
        assert fake.machines["m-codex"]["state"] == "started"
        assert entry is not None and entry["unconfirmed"] is True
        fake.start_lost = False
        with pytest.raises(fly_runner.FlyLaunchError, match="not confirmed stopped"):
            asyncio.run(executor.run(_codex_job()))  # no reuse while unconfirmed


def test_create_response_lost_destroys_the_machine_found_by_run_id(fly_env):
    fake = FakeFly()
    fake.create_lost = True  # the machine exists; the response says 500
    executor, _, revoked = make_executor(fake, record=None)
    with pytest.raises(fly_runner.FlyLaunchError):
        asyncio.run(executor.run(_job()))
    assert fake.calls("DELETE")
    assert fake.machines == {}
    assert executor.ledger.snapshot()["in_flight"] == {}
    assert revoked == [MINTED]


def test_create_response_lost_and_unlisted_keeps_the_pending_slot(fly_env):
    fake = FakeFly()
    fake.create_lost = True
    executor, _, _ = make_executor(fake, record=None)

    async def no_list():
        raise httpx.ConnectError("injected list failure")

    executor.client.list = no_list
    with pytest.raises(fly_runner.FlyLaunchError):
        asyncio.run(executor.run(_job()))
    (key,) = executor.ledger.snapshot()["in_flight"]
    assert key.startswith("pending:")
    assert executor.ledger.entry(key)["unconfirmed"] is True


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


# ── Recovery: one settlement path, cost charged exactly once ─────────────────


def _seed_bound(executor, clock, machine_id="m-old", run_id="old-run", max_run=600):
    executor.ledger.reserve(executor.config, run_id=run_id, max_run_seconds=max_run,
                            now=clock.now)  # fmt: skip
    executor.ledger.bind(run_id, machine_id, login=False)


@pytest.mark.parametrize("listed", [True, False])
def test_reaper_settles_an_overdue_reservation_and_charges_it(fly_env, listed):
    """A dispatcher restart: the collector is gone and the reservation is
    overdue. Whether its machine is still listed or already gone, the reaper
    confirms termination, charges the capped runtime once, and releases."""
    fake = FakeFly()
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock)
    _seed_bound(executor, clock)
    if listed:
        fake.machines["m-old"] = {"id": "m-old", "state": "started"}
    clock.now += 5000  # well past the 600 s window
    assert "m-old" in asyncio.run(executor.reap())
    assert fake.machines == {}
    snap = executor.ledger.snapshot(clock.now)
    assert snap["in_flight"] == {}
    assert snap["spend_usd"] == pytest.approx(600 * executor.config.rate_per_second_usd)
    # Exactly once: reaping again charges nothing more.
    asyncio.run(executor.reap())
    assert executor.ledger.snapshot(clock.now)["spend_usd"] == pytest.approx(snap["spend_usd"])


def test_reaper_keeps_the_slot_when_a_delete_ack_is_not_confirmed(fly_env):
    fake = FakeFly()
    fake.sticky_delete = True  # delete acknowledged, machine still started
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock, max_concurrent=1)
    _seed_bound(executor, clock)
    fake.machines["m-old"] = {"id": "m-old", "state": "started"}
    clock.now += 5000
    assert asyncio.run(executor.reap()) == []
    entry = executor.ledger.entry("m-old")
    assert entry is not None and entry["unconfirmed"] is True
    assert executor.ledger.snapshot(clock.now)["spend_usd"] == 0.0
    # Its slot still counts: the next job is refused on concurrency.
    with pytest.raises(fly_runner.FlyRefusal, match="concurrency"):
        executor.ledger.reserve(executor.config, run_id="next", max_run_seconds=60,
                                now=clock.now)  # fmt: skip
    # Once the delete takes effect, the reaper settles and admission reopens.
    fake.sticky_delete = False
    assert "m-old" in asyncio.run(executor.reap())
    executor.ledger.reserve(executor.config, run_id="next", max_run_seconds=60, now=clock.now)


def test_reaped_spend_counts_toward_admission(fly_env):
    fake = FakeFly()
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock)
    _seed_bound(executor, clock, max_run=1920)
    one = 1920 * executor.config.rate_per_second_usd
    data = executor.ledger._load()
    executor.ledger._roll(data, clock.now)
    data["spend_usd"] = 20.0 - one - one / 2
    executor.ledger._save(data)
    clock.now += 5000
    asyncio.run(executor.reap())
    with pytest.raises(fly_runner.FlyRefusal, match="budget"):
        executor.ledger.reserve(executor.config, run_id="n", max_run_seconds=1920,
                                now=clock.now)  # fmt: skip


def test_settle_is_idempotent(fly_env, tmp_path):
    config = fly_runner.FlyConfig.from_env()
    ledger = fly_runner.FlyLedger(tmp_path / "l.json")
    ledger.reserve(config, run_id="a", max_run_seconds=100, now=1000.0)
    ledger.bind("a", "m1", login=False)
    first = ledger.settle("m1", config=config, now=1050.0)
    second = ledger.settle("m1", config=config, now=1050.0)
    assert first == pytest.approx(50 * config.rate_per_second_usd) and second == 0.0


# ── Token lifecycle ──────────────────────────────────────────────────────────


def test_token_minted_during_cancellation_is_still_revoked(fly_env):
    import threading

    release = threading.Event()
    fake = FakeFly()

    def slow_mint(repo, prefix):
        release.wait(5)
        return MINTED

    executor, _, revoked = make_executor(fake, mint=slow_mint)

    async def go():
        task = asyncio.create_task(executor.run(_job()))
        await asyncio.sleep(0.05)  # the mint is in flight in its thread
        task.cancel()
        threading.Timer(0.1, release.set).start()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert revoked == [MINTED]
    assert fake.requests == []  # nothing launched


@pytest.mark.parametrize("failure", ["refusal", "launch_error", "reconcile_refused"])
def test_token_is_revoked_when_the_job_never_starts(fly_env, monkeypatch, failure):
    if failure == "reconcile_refused":
        fake = _codex_fake(monkeypatch)
        fake.stop_effective = False
        fake.machines["m-codex"]["state"] = "started"
        executor, _, revoked = make_executor(fake)
        executor.ledger.reserve(executor.config, run_id="old", max_run_seconds=60)
        executor.ledger.bind("old", "m-codex", login=True)
        job = _codex_job()
        expected = fly_runner.FlyLaunchError
    else:
        fake = FakeFly()
        if failure == "launch_error":
            fake.create_status = 503
        executor, _, revoked = make_executor(
            fake, **({"budget_usd": 0.0} if failure == "refusal" else {})
        )
        job = _job()
        expected = fly_runner.FlyRefusal if failure == "refusal" else fly_runner.FlyLaunchError
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
    """The reviewer's probe: $19.98 spent, three concurrent 1,920 s runs at the
    default rate and concurrency. Without counting outstanding reservations
    all three were admitted ($20.0109 committed against $20)."""
    config = fly_runner.FlyConfig.from_env()
    assert config.max_concurrent == 3 and config.budget_usd == 20.0
    ledger = fly_runner.FlyLedger(tmp_path / "ledger.json")
    now = 1_800_000_000.0
    ledger.snapshot(now)  # roll month keys
    data = ledger._load()
    ledger._roll(data, now)
    data["spend_usd"] = 19.98
    ledger._save(data)
    max_run = 1920
    one = max_run * config.rate_per_second_usd
    admitted = 0
    for i in range(3):
        try:
            ledger.reserve(config, run_id=f"r{i}", max_run_seconds=max_run, now=now)
            admitted += 1
        except fly_runner.FlyRefusal as exc:
            assert "reserved in flight" in str(exc)
    assert admitted == 1
    committed = 19.98 + admitted * one
    assert committed <= config.budget_usd
    # Completing the admitted run releases its reservation and charges actuals.
    ledger.bind("r0", "m0", login=False)
    ledger.release("m0", seconds=60, config=config, now=now)
    ledger.reserve(config, run_id="r3", max_run_seconds=max_run, now=now)
