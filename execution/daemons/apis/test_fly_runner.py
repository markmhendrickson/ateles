"""The Fly executor (ateles#1456): Machines API calls, the per-job credential
set, caps, the reaper, login machines, and the skill_runner seam.

The Fly API is an ``httpx.MockTransport``; GitHub minting/revocation and the
Neotoma result read are injected fakes. No test touches the network.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import base64
import contextlib
import copy
import json
import logging
import math
import re
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


def complete_record(run_id: str, **fields) -> dict:
    """A result record as run_job.build_record writes it: every field."""
    record = {"run_id": run_id, "exit_code": 0, "stdout": "", "stdout_truncated": False,
              "stderr_tail": "", "wall_ms": 1, "timed_out": False, "refused": "",
              "probe_ok": True}  # fmt: skip
    record.update(fields)
    return record


def _present(machine: dict) -> dict:
    """A machine as the Machines API answers for it: always with a version
    and a config object (the double fills them in) unless a test marks the
    machine ``_raw`` to serve an incomplete answer on purpose."""
    out = {k: v for k, v in machine.items() if k not in ("_raw", "_settle")}
    if not machine.get("_raw"):
        out.setdefault("instance_id", "v0")
        out.setdefault("config", {})
    return out


def _corrupt(data, field: str, mode: str):
    """Make one field of an answer absent, null or the wrong type."""
    data = dict(data)
    if mode == "absent":
        data.pop(field, None)
    elif mode == "null":
        data[field] = None
    else:
        data[field] = 12345 if isinstance(data.get(field), str) else "wrong-type"
    return data


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
        self.stored: list[str] = []  # the run id of every config actually stored
        self.leases: dict[str, dict] = {}
        self.lease_log: list[tuple[str, str]] = []
        self.lease_mode = "ok"  # "ok" | "error" | "no_nonce"
        self.enforce_version = True  # Fly documents the field, not its mismatch status
        # {"kind": "machine"|"list_entry"|"lease_view"|"lease_grant", "field",
        #  "mode": "absent"|"null"|"wrong", "when": callable(request) -> bool}
        self.corrupt: dict | None = None
        self.lease_view_override = None  # a raw GET /lease answer, e.g. {"data": {}}
        self.release_fails = False
        # After a config update a login machine reports "created" for this many
        # reads before it is "stopped" again, as Fly does for a moment.
        self.settle_reads = 0
        self.never_settles = False  # it stays "created"; the wait times out
        self.wait_settles = True  # the wait endpoint returns once it settled
        # A nonce sent for a machine that holds no lease (an expired one) is
        # refused, the strict reading of Fly's lease rules.
        self.reject_stale_nonce = False
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
            listing = [_present(m) for m in self.machines.values()]
            if self.corrupt and self.corrupt["kind"] == "list_entry" and self._when(request):
                listing = [_corrupt(m, self.corrupt["field"], self.corrupt["mode"])
                           for m in listing]  # fmt: skip
            return httpx.Response(200, json=listing)
        parts = rest.split("/")  # ['', 'machines', id, ...]
        mid = parts[2]
        # ── Leases, as Fly documents them: an exclusive lock on modifying a
        # machine; later calls carry the nonce header; an update without it
        # on a leased machine fails. This double also refuses start, stop
        # and delete without it (the docs are silent; the code never relies
        # on either answer).
        if len(parts) == 4 and parts[3] == "lease":
            held = self.leases.get(mid)
            given = request.headers.get("fly-machine-lease-nonce", "")
            if method == "POST":
                if self.lease_mode == "error":
                    return httpx.Response(500, json={"error": "lease service unavailable"})
                if self.lease_mode == "no_nonce":
                    return httpx.Response(200, json={"status": "success", "data": {}})
                if held and given != held["nonce"]:
                    return httpx.Response(409, json={"error": "machine already leased"})
                body = json.loads(request.content or b"{}")
                nonce = held["nonce"] if held else f"nonce-{len(self.lease_log)}-{mid}-secret"
                self.leases[mid] = {"nonce": nonce, "description": body.get("description", ""),
                                    "expires_at": 4_000_000_000}  # fmt: skip
                self.lease_log.append(("acquire", mid))
                grant = {"nonce": nonce, "expires_at": 4_000_000_000,
                         "description": body.get("description", "")}
                if self.corrupt and self.corrupt["kind"] == "lease_grant" and self._when(request):
                    grant = _corrupt(grant, self.corrupt["field"], self.corrupt["mode"])
                return httpx.Response(200, json={"status": "success", "data": grant})
            if method == "GET":
                if self.corrupt and self.corrupt["kind"] == "lease_view" and self._when(request):
                    view = {"description": (held or {}).get("description", "x"),
                            "expires_at": (held or {}).get("expires_at", 4_000_000_000)}
                    view = _corrupt(view, self.corrupt["field"], self.corrupt["mode"])
                    return httpx.Response(200, json={"status": "success", "data": view})
                if self.lease_view_override is not None:
                    return httpx.Response(200, json=self.lease_view_override)
                if not held:
                    return httpx.Response(404, json={"error": "no lease"})
                return httpx.Response(200, json={"status": "success", "data": {
                    "description": held["description"], "expires_at": held["expires_at"]}})
            if method == "DELETE":
                if self.release_fails:
                    return httpx.Response(500, json={"error": "release failed"})
                if not held:
                    return httpx.Response(404, json={"error": "no lease"})
                if given != held["nonce"]:
                    return httpx.Response(409, json={"error": "wrong nonce"})
                del self.leases[mid]
                self.lease_log.append(("release", mid))
                return httpx.Response(200, json={"ok": True})
        held = self.leases.get(mid)
        if held and method in ("POST", "DELETE") and (
            request.headers.get("fly-machine-lease-nonce", "") != held["nonce"]
        ):
            return httpx.Response(409, json={"error": "machine is leased; nonce required"})
        if (self.reject_stale_nonce and not held and method in ("POST", "DELETE")
                and request.headers.get("fly-machine-lease-nonce", "")):  # fmt: skip
            return httpx.Response(409, json={"error": "lease nonce is not current"})
        settling = mid in self.machines and "_settle" in self.machines[mid]
        if method == "GET" and len(parts) == 4 and parts[3] == "wait" and settling:
            if self.never_settles or not self.wait_settles:
                return httpx.Response(408, json={"error": "deadline exceeded"})
            self.machines[mid]["state"] = "stopped"
            del self.machines[mid]["_settle"]
            return httpx.Response(200, json={"ok": True})
        if method == "GET" and len(parts) == 3 and settling and not self.never_settles:
            machine = self.machines[mid]
            if machine["_settle"] <= 0:
                machine["state"] = "stopped"
                del machine["_settle"]
            else:
                machine["_settle"] -= 1
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
            answer = _present(self.machines[mid])
            if self.corrupt and self.corrupt["kind"] == "machine" and self._when(request):
                answer = _corrupt(answer, self.corrupt["field"], self.corrupt["mode"])
            return httpx.Response(200, json=answer)
        if method == "POST" and len(parts) == 3:  # update
            body = json.loads(request.content)
            machine = self.machines.setdefault(mid, {"id": mid, "login": True, "state": "stopped"})
            current = machine.get("instance_id")
            if (self.enforce_version and "current_version" in body and current
                    and body["current_version"] != current):  # fmt: skip
                return httpx.Response(412, json={"error": "machine version changed"})
            machine["config"] = body["config"]
            self.stored.append(fly_runner._config_run_id(body["config"]))
            if machine.get("login") and (self.settle_reads or self.never_settles):
                machine["state"] = "created"  # transitional, right after the update
                machine["_settle"] = self.settle_reads
            if current:
                machine["instance_id"] = f"{current}+"
            return httpx.Response(200, json={"id": mid, "instance_id": machine.get("instance_id")
                                             or "i-login"})  # fmt: skip
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

    def _when(self, request) -> bool:
        when = (self.corrupt or {}).get("when")
        return True if when is None else bool(when(request))

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


# Valid limits that one maximum-length run cannot fit: the smallest budget
# and the highest hourly rate the settings allow.
EXHAUSTING = {"budget_usd": 0.01, "rate_per_second_usd": 100.0 / 3600}


def _admitted(data: dict) -> dict:
    """Seeded reservations as admission would have counted them: the day's
    launch count covers every reservation started that day."""
    n = sum(1 for e in data["in_flight"].values()
            if isinstance(e, dict) and not e.get("untracked")
            and isinstance(e.get("started_at"), (int, float))
            and fly_runner._day_key(e["started_at"]) == data["day"])  # fmt: skip
    data["launches_today"] = max(data["launches_today"], n)
    return data


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
        return complete_record(run_id, **record)

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
        timeout=kw.pop("timeout", 60),
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
    executor, _, revoked = make_executor(FakeFly(), **EXHAUSTING)
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
    ledger = fly_runner.FlyLedger(tmp_path / "l.json", clock=lambda: 1000.0)
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
        "id": "m-codex", "state": "stopped", "login": True, "instance_id": "v1",
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
        return complete_record(run_id, stdout="done")

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
    assert not [r for r in fake.calls("DELETE") if not r.url.path.endswith("/lease")]
    assert fake.lease_log == [("acquire", "m-codex"), ("release", "m-codex")]
    assert fake.leases == {}
    assert revoked == [MINTED]
    assert not fly_runner._login_lock("codex").locked()


def test_login_queue_wait_limit_returns_cooled_until(fly_env, monkeypatch):
    monkeypatch.setenv("APIS_FLY_CURSOR_MACHINE_ID", "m-cursor")
    executor, _, _ = make_executor(FakeFly(), queue_max_wait_seconds=1)
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
    executor, _, _ = make_executor(FakeFly(), **EXHAUSTING)
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
                                "instance_id": "v1", "config": {"mounts": []}}  # fmt: skip
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
    executor._read_result = _async(lambda run_id: complete_record(run_id))
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

        async def hanging_start(machine_id, **kw):
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
    executor.ledger._save(_admitted(data))
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
        # Its owner left no liveness record: unknown, never presumed dead, so
        # the machine is busy (a retry-later refusal) rather than reclaimed.
        expected = fly_runner.FlyRefusal
    else:
        fake = FakeFly()
        fake.create_mode = {"launch_error": "refused", "uncertain": "lost"}.get(failure, "ok")
        executor, _, revoked = make_executor(
            fake, **(EXHAUSTING if failure == "refusal" else {})
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


# ── Owner liveness and cross-process coordination (round 4) ─────────────────


def _dead_pid() -> int:
    import subprocess as sp

    proc = sp.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _seed_foreign(executor, clock, *, owner: dict, run_id="their-run", machine_id="m-theirs",
                  max_run=600, heartbeat=None):  # fmt: skip
    """A reservation made by ANOTHER executor, with its liveness record."""
    data = executor.ledger._load()
    executor.ledger._roll(data, clock.now)
    data["in_flight"][run_id] = {
        "run_id": run_id, "owner": owner["id"], "machine_id": machine_id, "login": False,
        "started_at": clock.now, "deadline": clock.now + max_run,
        "max_cost_usd": max_run * executor.config.rate_per_second_usd,
    }  # fmt: skip
    data.setdefault("owners", {})[owner["id"]] = {
        **owner, "heartbeat": clock.now if heartbeat is None else heartbeat,
    }
    executor.ledger._save(_admitted(data))


def _live_owner(oid="apis-live-cli"):
    import os as _os

    record = fly_runner.this_process_owner(oid)
    assert record["pid"] == _os.getpid()
    return record


@pytest.mark.parametrize("case,reaped", [
    ("live_fresh", False),          # a concurrent dispatcher, alive and beating
    ("live_overdue_fresh", False),  # overdue, but its heartbeat is fresh
    ("live_overdue_stale", True),   # overdue AND heartbeat stale beyond the grace
    ("dead_pid", True),             # its process is gone
    ("pid_reused", True),           # its pid now belongs to another process
    ("other_host_fresh", False),    # cannot check another host's pid
    ("other_host_overdue_stale", True),
])  # fmt: skip
def test_another_owner_is_reaped_only_when_provably_dead_or_stale(fly_env, case, reaped):
    fake = FakeFly()
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock)
    owner = _live_owner()
    heartbeat = clock.now
    if case == "dead_pid":
        owner = {**owner, "pid": _dead_pid()}
    elif case == "pid_reused":
        owner = {**owner, "start": "a-different-start-time"}
    elif case.startswith("other_host"):
        owner = {**owner, "host": "another-host.invalid"}
    _seed_foreign(executor, clock, owner=owner, heartbeat=heartbeat)
    fake.machines["m-theirs"] = {"id": "m-theirs", "state": "started"}
    if "overdue" in case:
        clock.now += 600 + fly_runner.REAP_GRACE_SECONDS + 1
        if case.endswith("fresh"):
            data = executor.ledger._load()
            data["owners"][owner["id"]]["heartbeat"] = clock.now
            executor.ledger._save(_admitted(data))
    result = asyncio.run(executor.reap())
    assert ("their-run" in result) is reaped
    assert ("m-theirs" in fake.machines) is (not reaped)


_HOLDER = r"""
import json, sys, time
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import fly_runner
from pathlib import Path
mode, ledger_path = sys.argv[3], Path(sys.argv[4])
config = fly_runner.FlyConfig(app="x", api_token="", image="", region="")
owner = fly_runner.this_process_owner("apis-other-process")
ledger = fly_runner.FlyLedger(ledger_path, owner=owner)
if mode == "reserve":
    ledger.reserve(config, run_id="their-live-run", owner=owner["id"], max_run_seconds=600)
    ledger.set_machine("their-live-run", "m-their-live")
    print("ready", flush=True)
    sys.stdin.readline()
elif mode == "admission":
    import fcntl
    fh = open(ledger_path.with_name(ledger_path.name + ".admission.lock"), "a+")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    print("ready", flush=True)
    sys.stdin.readline()
"""


def _holder(tmp_path, mode, ledger_path):
    import subprocess as sp

    script = tmp_path / "holder.py"
    script.write_text(_HOLDER)
    proc = sp.Popen(
        [sys.executable, str(script), str(_REPO_ROOT), str(_DAEMON_DIR), mode, str(ledger_path)],
        stdin=sp.PIPE, stdout=sp.PIPE, text=True,
    )  # fmt: skip
    assert proc.stdout.readline().strip() == "ready"
    return proc


def test_two_processes_sharing_a_ledger_never_reap_each_others_live_run(fly_env, tmp_path):
    """A second process (a CLI dispatcher) holds a live reservation in the
    shared ledger. This executor's reaper must leave it alone, and settle it
    only once that process has exited."""
    fake = FakeFly()
    import time as _time

    executor, _, _ = make_executor(fake, clock=_time.time)  # real time: liveness is real
    other = _holder(tmp_path, "reserve", executor.ledger.path)
    try:
        fake.machines["m-their-live"] = {"id": "m-their-live", "state": "started"}
        assert asyncio.run(executor.reap()) == []
        assert "m-their-live" in fake.machines
        assert executor.ledger.entry("their-live-run") is not None
    finally:
        other.stdin.write("done\n")
        other.stdin.flush()
        other.wait(10)
    assert "their-live-run" in asyncio.run(executor.reap())  # its owner is gone now
    assert "m-their-live" not in fake.machines


def test_admission_is_exclusive_across_processes(fly_env, tmp_path):
    executor, _, _ = make_executor(FakeFly())
    other = _holder(tmp_path, "admission", executor.ledger.path)

    async def enter():
        async with executor._admission():
            return True

    try:
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(asyncio.wait_for(enter(), 0.5))
    finally:
        other.stdin.write("done\n")
        other.stdin.flush()
        other.wait(10)
    assert asyncio.run(asyncio.wait_for(enter(), 5)) is True


def test_login_machine_held_by_a_live_dispatcher_elsewhere_is_busy(fly_env, monkeypatch):
    fake = _codex_fake(monkeypatch)
    clock = Clock()
    executor, _, revoked = make_executor(fake, clock=clock)
    _seed_foreign(executor, clock, owner=_live_owner(), run_id="their-codex-run",
                  machine_id="m-codex")  # fmt: skip
    data = executor.ledger._load()
    data["in_flight"]["their-codex-run"]["login"] = True
    executor.ledger._save(_admitted(data))
    fake.machines["m-codex"]["state"] = "started"
    with pytest.raises(fly_runner.FlyRefusal, match="busy") as exc:
        asyncio.run(executor.run(_codex_job()))
    assert exc.value.cooled_until
    assert not fake.calls("POST", "/start") and not fake.calls("POST", "/stop")
    assert executor.ledger.entry("their-codex-run") is not None
    assert revoked == [MINTED]


def test_this_process_heartbeat_is_recorded_on_every_write(fly_env):
    clock = Clock()
    executor, _, _ = make_executor(FakeFly(), clock=clock)
    executor.ledger.reserve(executor.config, run_id="r", owner=executor.instance_id,
                            max_run_seconds=60, now=clock.now)  # fmt: skip
    rec = executor.ledger._load()["owners"][executor.instance_id]
    assert rec["pid"] and rec["host"] and rec["heartbeat"] == clock.now
    clock.now += 45
    executor.ledger.heartbeat()
    assert executor.ledger._load()["owners"][executor.instance_id]["heartbeat"] == clock.now


# ── Response validation on update, list and run-id termination (round 4) ────


def test_an_update_answered_for_another_machine_aborts_before_start(fly_env, monkeypatch):
    fake = _codex_fake(monkeypatch)
    executor, _, _ = make_executor(fake)
    real = fake.handler

    def wrong_update(request):
        response = real(request)
        if request.method == "POST" and request.url.path.endswith("/machines/m-codex"):
            return httpx.Response(200, json={"id": "someone-else", "state": "stopped"})
        return response

    executor.client = fly_runner.FlyMachinesClient(
        executor.config, transport=httpx.MockTransport(wrong_update)
    )
    with pytest.raises(fly_runner.FlyLaunchError, match="invalid Machines API response"):
        asyncio.run(executor.run(_codex_job()))
    assert not fake.calls("POST", "/start")


@pytest.mark.parametrize("bad_listing", [
    [{"id": "m1", "state": "started"}, {"id": "m1", "state": "started"}],  # repeated id
    [{"id": "m1"}],  # no state
    [{"id": "m1", "state": "started", "config": {"metadata": {"ateles_run_id": 7}}}],
    [{"id": "m1", "state": "started", "config": {"metadata": []}}],
])  # fmt: skip
def test_an_invalid_listing_entry_is_rejected(fly_env, bad_listing):
    def handler(request):
        return httpx.Response(200, json=bad_listing)

    config = fly_runner.FlyConfig.from_env()
    client = fly_runner.FlyMachinesClient(config, transport=httpx.MockTransport(handler))
    with pytest.raises(fly_runner.FlyApiError, match="invalid"):
        asyncio.run(client.list())


def test_run_id_termination_is_confirmed_against_a_second_listing(fly_env):
    """A machine that reads back as gone but is still LISTED for the run is
    not confirmed terminated."""
    fake = FakeFly()
    fake.create_mode = "lost"
    executor, _, _ = make_executor(fake, record=None)
    real = fake.handler
    listed_ghost = {}

    def handler(request):
        if request.method == "DELETE":
            mid = request.url.path.rsplit("/", 1)[1]
            listed_ghost[mid] = fake.machines.get(mid)
        response = real(request)
        if request.method == "GET" and request.url.path.endswith("/machines") and listed_ghost:
            ghosts = [m for m in listed_ghost.values() if m]
            return httpx.Response(200, json=list(fake.machines.values()) + ghosts)
        return response

    executor.client = fly_runner.FlyMachinesClient(
        executor.config, transport=httpx.MockTransport(handler)
    )
    job = _job()
    with pytest.raises(fly_runner.FlyUncertainLaunch):
        asyncio.run(executor.run(job))
    assert job.remote_state == "unconfirmed"
    assert executor.ledger.entry(job.run_id)["unconfirmed"] is True


# ── The dispatch layer above the executor (round 4) ──────────────────────────


def test_unknown_outcome_never_fails_over_to_another_provider(fly_env, seam, monkeypatch):
    """Unpinned, two providers, an uncertain launch whose infrastructure
    error text resembles a provider rate limit: exactly one attempt, and the
    original remote identifiers come back."""
    spawned, fake_exec = seam
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "claude,codex")
    monkeypatch.setenv("APIS_HARNESS_HEADROOM", '{"claude": 1.0, "codex": 1.0}')
    monkeypatch.setattr(skill_runner, "CODEX_BIN", "/usr/bin/codex-test")
    monkeypatch.setenv("APIS_FLY_CODEX_MACHINE_ID", "m-codex")
    fake = FakeFly()
    fake.machines["m-codex"] = {"id": "m-codex", "state": "stopped", "login": True,
                                "instance_id": "v1", "config": {"mounts": []}}  # fmt: skip
    real = fake.handler

    def handler(request):
        response = real(request)
        if request.method == "POST" and request.url.path.endswith("/machines"):
            return httpx.Response(500, json={"error": "rate_limit_error: usage limit reached"})
        if request.method == "POST" and request.url.path.endswith("/start"):
            return httpx.Response(500, json={"error": "rate_limit_error: usage limit reached"})
        return response

    executor, _, _ = make_executor(fake)
    executor.client = fly_runner.FlyMachinesClient(
        executor.config, transport=httpx.MockTransport(handler)
    )
    monkeypatch.setattr(fly_runner, "get_executor", lambda: executor)
    import harness_router

    harness_router.reset_state()
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = asyncio.run(skill_runner.run_skill(
            "lanius", "check the gates", role="lanius", runner="fly",
            repository="owner/repo", include_github_contract=True,
        ))  # fmt: skip
    assert len(result.attempted_providers) == 1, result.attempted_providers
    assert result.remote_outcome_unknown and result.remote_run_id
    assert not result.ok and spawned == []


@pytest.mark.parametrize("terminates", [True, False])
def test_timeout_keeps_the_remote_run_identity_and_state(fly_env, seam, monkeypatch, terminates):
    spawned, fake_exec = seam
    events = []
    monkeypatch.setattr(skill_runner, "_write_harness_event", lambda **kw: events.append(kw))
    fake = FakeFly()
    fake.wait_status = 408
    fake.sticky_all = not terminates
    clock = Clock()
    executor, _, revoked = make_executor(fake, clock=clock)
    real_wait = executor.client.wait

    async def advancing_wait(machine_id, **k):
        clock.now += 120
        return await real_wait(machine_id, **k)

    executor.client.wait = advancing_wait
    monkeypatch.setattr(fly_runner, "get_executor", lambda: executor)
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = _once(runner="fly", repository="owner/repo", include_github_contract=True,
                       timeout=60)  # fmt: skip
    state = "terminated" if terminates else "unconfirmed"
    assert not result.ok and "timed out" in result.error
    assert result.remote_outcome_unknown is True
    assert result.remote_run_id and result.remote_machine_state == state
    assert result.remote_run_id in result.error and "Not retried" in result.error
    assert any("fly_outcome=unknown" in e.get("output_summary", "")
               and f"fly_machine_state={state}" in e.get("output_summary", "")
               for e in events)  # fmt: skip
    assert (executor.ledger.snapshot(clock.now)["in_flight"] == {}) is terminates
    assert spawned == [] and revoked == [MINTED]


def test_a_recorded_result_with_unconfirmed_cleanup_keeps_its_remote_fields(
    fly_env, seam, monkeypatch
):
    spawned, fake_exec = seam
    fake = FakeFly()
    fake.sticky_all = True
    executor, _, _ = make_executor(fake, record={"exit_code": 0, "stdout": "GATE: clear\n"})
    executor.client.wait = _async(lambda *a, **k: "reached")
    monkeypatch.setattr(fly_runner, "get_executor", lambda: executor)
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = _once(runner="fly", repository="owner/repo", include_github_contract=True)
    assert result.ok  # the task's result is known
    assert result.remote_outcome_unknown is False
    assert result.remote_run_id and result.remote_machine_state == "unconfirmed"


# ── Login-machine release order (round 5) ────────────────────────────────────


def _second_executor(fake, executor, *, owner_id="apis-second-dispatcher", record=None):
    """Another dispatcher on the same ledger file and the same machines, as a
    separate process would have it: its own instance, ledger handle and
    in-process locks."""
    import time as _time

    config = executor.config
    revoked: list[str] = []

    async def _read(run_id):
        return complete_record(run_id, **(record if record is not None
                                          else {"stdout": f"B {run_id}"}))  # fmt: skip

    other = fly_runner.FlyExecutor(
        config,
        client=fly_runner.FlyMachinesClient(config, transport=httpx.MockTransport(fake.handler)),
        ledger=fly_runner.FlyLedger(executor.ledger.path,
                                    owner=fly_runner.this_process_owner(owner_id),
                                    clock=_time.time),  # fmt: skip
        mint=lambda repo, prefix: MINTED,
        revoke=revoked.append,
        read_result=_read,
        clock=_time.time,
    )
    other._last_reap = float("inf")
    return other


def test_old_cleanup_cannot_erase_the_next_dispatchers_login_job(fly_env, monkeypatch):
    """The reviewers' interleaving: A's cleanup is paused at its read while B
    tries to take the same login machine. B must not get in until A has
    stopped, scrubbed, verified and released; B's job config survives and B
    completes with its own result."""
    import time as _time

    fake = _codex_fake(monkeypatch)
    # Separate processes do not share the in-process login lock.
    monkeypatch.setattr(fly_runner, "_login_lock", lambda provider: asyncio.Lock())
    a, _, _ = make_executor(fake, clock=_time.time,
                            record={"exit_code": 0, "stdout": "A done"})  # fmt: skip
    b = _second_executor(fake, a)
    starts: list[str] = []
    real = fake.handler

    def handler(request):
        if request.method == "POST" and request.url.path.endswith("/start"):
            cfg = fake.machines["m-codex"].get("config") or {}
            starts.append(fly_runner._config_run_id(cfg))
        return real(request)

    a.client = fly_runner.FlyMachinesClient(a.config, transport=httpx.MockTransport(handler))
    b.client = fly_runner.FlyMachinesClient(b.config, transport=httpx.MockTransport(handler))
    job_a, job_b = _codex_job(), _codex_job()

    async def go():
        paused = asyncio.Event()
        resume = asyncio.Event()
        real_get = a.client.get
        calls = {"n": 0}

        async def get_paused_in_cleanup(machine_id):
            # A's reads during settlement: pause the scrub's read.
            if job_a.run_id in a._active and fake.machines["m-codex"]["state"] == "stopped":
                calls["n"] += 1
                if calls["n"] == 2:  # terminate's read, then the scrub's read
                    paused.set()
                    await resume.wait()
            return await real_get(machine_id)

        a.client.get = get_paused_in_cleanup
        task_a = asyncio.create_task(a.run(job_a))
        await paused.wait()
        task_b = asyncio.create_task(b.run(job_b))
        await asyncio.sleep(0.3)
        assert b.ledger.entry(job_b.run_id) is None  # B is held at admission
        resume.set()
        return await task_a, await task_b

    (rc_a, out_a, _), (rc_b, out_b, _) = asyncio.run(go())
    assert rc_a == 0 and rc_b == 0
    assert out_b.decode() == f"B {job_b.run_id}"  # B's result is its own run's
    assert starts == [job_a.run_id, job_b.run_id]  # each started with its own job
    assert fake.machines["m-codex"]["config"]["files"] == []  # B cleaned its own up
    assert a.ledger.snapshot()["in_flight"] == {}


_RESERVER = r"""
import fcntl, json, sys, time
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import fly_runner
from pathlib import Path
ledger_path, machine = Path(sys.argv[3]), sys.argv[4]
config = fly_runner.FlyConfig(app="x", api_token="", image="", region="", max_concurrent=10)
owner = fly_runner.this_process_owner("apis-process-b")
ledger = fly_runner.FlyLedger(ledger_path, owner=owner)
lock = ledger_path.with_name(ledger_path.name + ".admission.lock")
seen_a = False
print("ready", flush=True)
deadline = time.time() + 30
while time.time() < deadline:
    if ledger.find_by_machine(machine):
        seen_a = True  # observed without the admission lock
    with open(lock, "a+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            time.sleep(0.005)
            continue
        try:
            held = ledger.find_by_machine(machine)
            if held:
                seen_a = True
            elif seen_a:
                ledger.reserve(config, run_id="b-run", owner=owner["id"], max_run_seconds=60,
                               login=True, machine_id=machine)
                print(json.dumps({"reserved_at": time.time()}), flush=True)
                break
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    time.sleep(0.005)
"""


def test_a_second_process_reserving_the_instant_a_releases_finds_cleanup_done(
    fly_env, tmp_path, monkeypatch
):
    """Process B polls the shared ledger and reserves the login machine the
    moment A's reservation disappears. Every one of A's cleanup calls must
    already have happened by then."""
    import subprocess as sp
    import time as _time

    fake = _codex_fake(monkeypatch)
    a, _, _ = make_executor(fake, clock=_time.time, record={"exit_code": 0, "stdout": "A"})
    cleanup_times: list[float] = []
    real = fake.handler

    def handler(request):
        is_scrub = (
            request.method == "POST" and request.url.path.endswith("/machines/m-codex")
            and json.loads(request.content)["config"].get("files") == []
        )
        if is_scrub:
            _time.sleep(0.2)  # widen the window an early release would open
        response = real(request)
        if is_scrub or (request.method == "GET" and cleanup_times):
            cleanup_times.append(_time.time())
        return response

    a.client = fly_runner.FlyMachinesClient(a.config, transport=httpx.MockTransport(handler))
    script = tmp_path / "reserver.py"
    script.write_text(_RESERVER)
    proc = sp.Popen([sys.executable, str(script), str(_REPO_ROOT), str(_DAEMON_DIR),
                     str(a.ledger.path), "m-codex"], stdout=sp.PIPE, stderr=sp.PIPE,
                    text=True)  # fmt: skip
    try:
        assert proc.stdout.readline().strip() == "ready"
        assert asyncio.run(a.run(_codex_job()))[0] == 0
        line = proc.stdout.readline().strip()
    finally:
        proc.kill()
        _, b_err = proc.communicate()
    assert line, f"process B never reserved the machine: {b_err[-800:]}"
    reserved_at = json.loads(line)["reserved_at"]
    assert cleanup_times and max(cleanup_times) < reserved_at


def test_cleanup_never_touches_a_config_that_belongs_to_another_run(fly_env, monkeypatch):
    fake = _codex_fake(monkeypatch)
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock)
    executor.ledger.reserve(executor.config, run_id="old-run", owner=executor.instance_id,
                            max_run_seconds=60, login=True, machine_id="m-codex",
                            now=clock.now)  # fmt: skip
    executor.ledger.mark_unconfirmed("old-run", reason="test")
    newer = {"files": [{"guest_path": "/run/job/job.json", "raw_value": "eA=="}],
             "env": {"ATELES_RUN_ID": "newer-run"},
             "metadata": {"ateles_run_id": "newer-run"}}  # fmt: skip
    fake.machines["m-codex"]["config"] = newer
    fake.machines["m-codex"]["state"] = "started"  # the newer run is live on it
    # Not this run's machine any more: nothing is touched, and the old
    # reservation stays unconfirmed (reported) rather than claiming success.
    assert asyncio.run(executor._settle("old-run")) is False
    assert fake.machines["m-codex"]["config"] == newer
    assert fake.machines["m-codex"]["state"] == "started"
    assert not [r for r in fake.requests if r.method == "POST"]
    assert executor.ledger.entry("old-run")["unconfirmed"] is True


def test_an_unverified_scrub_keeps_the_login_reservation(fly_env, monkeypatch):
    fake = _codex_fake(monkeypatch)
    real = fake.handler

    def scrub_ignored(request):
        if request.method == "POST" and request.url.path.endswith("/machines/m-codex"):
            if json.loads(request.content)["config"].get("files") == []:
                return httpx.Response(200, json={"id": "m-codex", "state": "stopped"})
        return real(request)

    executor, _, _ = make_executor(fake, record={"exit_code": 0, "stdout": "ok"})
    executor.client = fly_runner.FlyMachinesClient(
        executor.config, transport=httpx.MockTransport(scrub_ignored)
    )
    job = _codex_job()
    rc, _, _ = asyncio.run(executor.run(job))
    assert rc == 0 and job.remote_state == "unconfirmed"
    assert executor.ledger.entry(job.run_id)["unconfirmed"] is True
    assert fake.machines["m-codex"]["config"]["files"]  # still there, still owned
    with pytest.raises(fly_runner.FlyLaunchError, match="not confirmed stopped"):
        asyncio.run(executor.run(_codex_job()))


def test_a_definite_start_failure_scrubs_before_it_releases(fly_env, monkeypatch):
    fake = _codex_fake(monkeypatch)
    order: list[str] = []
    real = fake.handler

    def handler(request):
        if request.method == "POST" and request.url.path.endswith("/start"):
            return httpx.Response(422, json={"error": "rejected"})
        if request.method == "POST" and request.url.path.endswith("/machines/m-codex"):
            if json.loads(request.content)["config"].get("files") == []:
                order.append("scrub")
        return real(request)

    executor, _, _ = make_executor(fake)
    executor.client = fly_runner.FlyMachinesClient(
        executor.config, transport=httpx.MockTransport(handler)
    )
    real_release = executor.ledger.release

    def release(run_id):
        order.append("release")
        real_release(run_id)

    executor.ledger.release = release
    with pytest.raises(fly_runner.FlyLaunchError):
        asyncio.run(executor.run(_codex_job()))
    assert order == ["scrub", "release"]
    assert fake.machines["m-codex"]["config"]["files"] == []


def test_only_vetted_classes_can_be_opted_in(monkeypatch):
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "carry_forward_check,build")
    assert fly_runner.runner_classes() == frozenset({"carry_forward_check"})
    assert fly_runner.wants_fly(None, "build") is False
    assert fly_runner.wants_fly(None, "carry_forward_check") is True


def test_api_error_text_never_carries_an_encoded_blob():
    import base64

    blob = base64.b64encode(b"a job file that must never be echoed into a log line").decode()
    err = fly_runner.FlyApiError(400, f"invalid config: files[0].raw_value={blob}")
    assert blob not in str(err) and "[redacted]" in str(err)
    assert err.status == 400


# ── Ownership before any stop (round 6) ──────────────────────────────────────


_LOGIN_HOLDER = r"""
import sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import fly_runner
from pathlib import Path
ledger_path, machine = Path(sys.argv[3]), sys.argv[4]
config = fly_runner.FlyConfig(app="x", api_token="", image="", region="", max_concurrent=10)
owner = fly_runner.this_process_owner("apis-process-b-live")
ledger = fly_runner.FlyLedger(ledger_path, owner=owner)
ledger.reserve(config, run_id="b-live-run", owner=owner["id"], max_run_seconds=600,
               login=True, machine_id=machine)
print("ready", flush=True)
sys.stdin.readline()
"""


def test_settling_an_abandoned_login_reservation_never_stops_another_live_run(
    fly_env, tmp_path, monkeypatch
):
    """Process B (real, live) is running its job on the login machine. A
    holds an abandoned reservation on the same machine (its owner died before
    reconciling). A's reaper must leave B's machine running, its config and
    its reservation untouched, and keep its own reservation unconfirmed."""
    import subprocess as sp
    import time as _time

    fake = _codex_fake(monkeypatch)
    executor, _, _ = make_executor(fake, clock=_time.time)
    b_config = {"files": [{"guest_path": "/run/job/job.json", "raw_value": "eA=="}],
                "env": {"ATELES_RUN_ID": "b-live-run"},
                "metadata": {"ateles_run_id": "b-live-run"}}  # fmt: skip
    fake.machines["m-codex"].update({"state": "started", "config": dict(b_config)})
    script = tmp_path / "login_holder.py"
    script.write_text(_LOGIN_HOLDER)
    proc = sp.Popen([sys.executable, str(script), str(_REPO_ROOT), str(_DAEMON_DIR),
                     str(executor.ledger.path), "m-codex"], stdin=sp.PIPE, stdout=sp.PIPE,
                    text=True)  # fmt: skip
    try:
        assert proc.stdout.readline().strip() == "ready"
        dead = {**fly_runner.this_process_owner("apis-dead-a"), "pid": _dead_pid()}
        data = executor.ledger._load()
        now = _time.time()
        data["in_flight"]["a-abandoned-run"] = {
            "run_id": "a-abandoned-run", "owner": dead["id"], "machine_id": "m-codex",
            "login": True, "started_at": now, "deadline": now + 600, "max_cost_usd": 0.01,
        }  # fmt: skip
        data.setdefault("owners", {})[dead["id"]] = {**dead, "heartbeat": now}
        executor.ledger._save(_admitted(data))
        reaped = asyncio.run(executor.reap())
        assert reaped == []
        assert fake.machines["m-codex"]["state"] == "started"
        assert fake.machines["m-codex"]["config"] == b_config
        assert not [r for r in fake.requests if r.method in ("POST", "DELETE")]
        assert executor.ledger.entry("b-live-run") is not None
        assert executor.ledger.entry("a-abandoned-run")["unconfirmed"] is True
    finally:
        proc.stdin.write("done\n")
        proc.stdin.flush()
        proc.wait(10)


def test_a_per_job_machine_carrying_another_runs_id_is_never_destroyed(fly_env):
    fake = FakeFly()
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock)
    _seed(executor, clock)  # this run's reservation names m-old
    fake.machines["m-old"] = {"id": "m-old", "state": "started",
                              "config": {"metadata": {"ateles_run_id": "someone-else"}}}  # fmt: skip
    clock.now += 600 + fly_runner.REAP_GRACE_SECONDS + 1
    assert asyncio.run(executor.reap()) == []
    assert fake.machines["m-old"]["state"] == "started"
    assert not fake.calls("DELETE") and not fake.calls("POST", "/stop")
    assert executor.ledger.entry("old-run")["unconfirmed"] is True


# ── Keep an unknown remote outcome intact end to end (round 6) ──────────────


@pytest.mark.parametrize("answer", [
    httpx.Response(200, json=[1]),
    httpx.Response(200, json={"entities": 1}),
    httpx.Response(200, json={"entities": [1]}),
    httpx.Response(200, content=b"{not json"),
    httpx.Response(503, json={"error": "down"}),
])  # fmt: skip
def test_an_unreadable_result_read_raises_instead_of_reading_as_absent(fly_env, answer):
    transport = httpx.MockTransport(lambda request: answer)
    with pytest.raises(fly_runner.FlyResultUnreadable):
        asyncio.run(fly_runner.read_result_record("r1", transport=transport, attempts=2, delay=0))


def test_a_well_formed_empty_answer_reads_as_absent(fly_env):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"entities": []}))
    assert asyncio.run(fly_runner.read_result_record("r1", transport=transport, attempts=1)) is None


@pytest.mark.parametrize("record", [
    "raise",
    {"exit_code": "0", "stdout": "x"},       # wrong type
    {"exit_code": True},                     # bool is not an exit code
    {"exit_code": 0, "stdout": 7},           # non-text output
    {"exit_code": 0, "run_id": "another"},   # not this run's record
])  # fmt: skip
def test_a_malformed_or_unreadable_record_is_an_unknown_outcome(fly_env, record):
    fake = FakeFly()
    executor, _, _ = make_executor(fake)

    async def read(run_id):
        if record == "raise":
            raise fly_runner.FlyResultUnreadable("synthetic")
        return {"run_id": run_id, **record}

    executor._read_result = read
    job = _job()
    rc, out, err = asyncio.run(executor.run(job))
    assert rc == 1 and job.outcome_unknown is True
    assert b"outcome is unknown" in err


def test_seam_turns_a_malformed_result_into_an_unknown_outcome(fly_env, seam, monkeypatch):
    spawned, fake_exec = seam
    executor, _, _ = make_executor(FakeFly())

    async def read(run_id):
        raise TypeError("synthetic malformed answer")

    executor._read_result = read
    monkeypatch.setattr(fly_runner, "get_executor", lambda: executor)
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = _once(runner="fly", repository="owner/repo", include_github_contract=True)
    assert not result.ok and result.remote_outcome_unknown is True
    assert result.remote_run_id and spawned == []


@pytest.mark.parametrize("after_launch", [True, False])
def test_seam_unexpected_errors_after_a_launch_stay_unknown(fly_env, seam, monkeypatch,
                                                            after_launch):  # fmt: skip
    spawned, fake_exec = seam
    executor, _, _ = make_executor(FakeFly())

    async def exploding_run(job):
        job.launch_requested = after_launch
        raise RuntimeError("synthetic failure")

    executor.run = exploding_run
    monkeypatch.setattr(fly_runner, "get_executor", lambda: executor)
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        if after_launch:
            result = _once(runner="fly", repository="owner/repo", include_github_contract=True)
            assert result.remote_outcome_unknown is True and not result.ok
            assert "UNKNOWN outcome" in result.error
        else:
            with pytest.raises(RuntimeError):
                _once(runner="fly", repository="owner/repo", include_github_contract=True)
    assert spawned == []


def test_run_id_termination_never_destroys_a_login_machine(fly_env, monkeypatch):
    fake = _codex_fake(monkeypatch)
    fake.create_mode = "lost"
    executor, _, _ = make_executor(fake, record=None)
    job = _job()
    fake.machines["m-codex"]["config"] = {"metadata": {"ateles_run_id": job.run_id}}
    with pytest.raises(fly_runner.FlyUncertainLaunch):
        asyncio.run(executor.run(job))
    assert "m-codex" in fake.machines
    deleted = [r.url.path.rsplit("/", 1)[1] for r in fake.calls("DELETE")]
    assert "m-codex" not in deleted


import time  # noqa: E402

# ── Login admission is compare-and-set on the machine's LIVE state (round 7) ─


def _mutations(fake, machine="m-codex"):
    return [r for r in fake.requests if r.method in ("POST", "DELETE")
            and f"/machines/{machine}" in r.url.path]  # fmt: skip


@pytest.mark.parametrize("state,config,why", [
    ("stopped", {"files": [{"guest_path": "/run/job/job.json", "raw_value": "eA=="}],
                 "env": {"ATELES_RUN_ID": "someone-else"},
                 "metadata": {"ateles_run_id": "someone-else"}}, "another run"),
    ("stopped", {"files": [{"guest_path": "/run/job/job.json", "raw_value": "eA=="}]},
     "nobody can attribute"),
    ("started", {"mounts": []}, "started"),
    ("started", {"env": {"ATELES_RUN_ID": "someone-else"},
                 "metadata": {"ateles_run_id": "someone-else"}}, "started"),
])  # fmt: skip
def test_login_admission_refuses_a_busy_machine_with_no_ledger_entry(
    fly_env, monkeypatch, state, config, why
):
    """The ledger knows nothing about the machine (lost, unshared, another
    host), but the machine itself is busy: refused, nothing written."""
    fake = _codex_fake(monkeypatch)
    fake.machines["m-codex"].update({"state": state, "config": dict(config)})
    executor, _, revoked = make_executor(fake)
    assert executor.ledger.snapshot()["in_flight"] == {}
    with pytest.raises(fly_runner.FlyRefusal, match="not free") as exc:
        asyncio.run(executor.run(_codex_job()))
    assert why in str(exc.value) and exc.value.cooled_until
    assert _mutations(fake) == []  # no update, start, stop or delete
    assert fake.machines["m-codex"]["config"] == config
    assert fake.machines["m-codex"]["state"] == state
    assert executor.ledger.snapshot()["in_flight"] == {}
    assert revoked == [MINTED]


def test_a_lost_ledger_with_a_busy_machine_is_refused(fly_env, monkeypatch):
    fake = _codex_fake(monkeypatch)
    other = {"files": [{"guest_path": "/run/job/job.json", "raw_value": "eA=="}],
             "env": {"ATELES_RUN_ID": "pre-loss-run"},
             "metadata": {"ateles_run_id": "pre-loss-run"}}  # fmt: skip
    fake.machines["m-codex"].update({"state": "started", "config": dict(other)})
    executor, _, _ = make_executor(fake)
    executor.ledger.path.unlink(missing_ok=True)  # the ledger is gone
    with pytest.raises(fly_runner.FlyRefusal):
        asyncio.run(executor.run(_codex_job()))
    assert _mutations(fake) == [] and fake.machines["m-codex"]["config"] == other


def test_an_unreadable_ledger_writes_nothing_to_the_machine(fly_env, monkeypatch):
    fake = _codex_fake(monkeypatch)
    executor, _, _ = make_executor(fake)
    executor.ledger.path.parent.mkdir(parents=True, exist_ok=True)
    executor.ledger.path.write_text("{not json")
    with pytest.raises(fly_runner.FlyRefusal, match="unreadable"):
        asyncio.run(executor.run(_codex_job()))
    assert _mutations(fake) == []


def test_a_clean_stopped_machine_is_admitted_with_a_version_precondition(fly_env, monkeypatch):
    fake = _codex_fake(monkeypatch)
    fake.machines["m-codex"]["instance_id"] = "v-before-write"
    executor, _, _ = make_executor(fake, record={"exit_code": 0, "stdout": "ok"})
    job = _codex_job()
    assert asyncio.run(executor.run(job))[0] == 0
    first_update = json.loads(next(
        r for r in _mutations(fake) if r.url.path.endswith("/machines/m-codex")
    ).content)
    assert first_update["current_version"] == "v-before-write"
    assert fly_runner._config_run_id(first_update["config"]) == job.run_id


def test_admission_fails_closed_when_the_stored_config_is_not_ours(fly_env, monkeypatch):
    fake = _codex_fake(monkeypatch)
    real = fake.handler

    def someone_else_wins(request):
        response = real(request)
        if (request.method == "POST" and request.url.path.endswith("/machines/m-codex")
                and json.loads(request.content)["config"].get("files")):  # fmt: skip
            fake.machines["m-codex"]["config"] = {
                "env": {"ATELES_RUN_ID": "the-winner"}, "metadata": {"ateles_run_id": "the-winner"},
                "files": [{"guest_path": "/run/job/job.json", "raw_value": "eA=="}],
            }  # fmt: skip
        return response

    executor, _, _ = make_executor(fake)
    executor.client = fly_runner.FlyMachinesClient(
        executor.config, transport=httpx.MockTransport(someone_else_wins)
    )
    with pytest.raises(fly_runner.FlyLaunchError, match="not this run's"):
        asyncio.run(executor.run(_codex_job()))
    assert not fake.calls("POST", "/start")
    assert fly_runner._config_run_id(fake.machines["m-codex"]["config"]) == "the-winner"


class _SharedFly:
    """The FakeFly Machines API served over local HTTP, so separate
    processes act on ONE set of machines."""

    def __init__(self, fake: FakeFly, slow_login_wait: float = 0.0):
        import http.server
        import threading

        self.fake = fake
        lock = threading.Lock()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                request = httpx.Request(self.command, f"http://fly.invalid{self.path}",
                                        headers=dict(self.headers), content=body)  # fmt: skip
                if self.command == "GET" and self.path.split("?")[0].endswith("/wait"):
                    time.sleep(slow_login_wait)  # the winner's job keeps the machine busy
                with lock:
                    response = outer.fake.handler(request)
                payload = response.content
                self.send_response(response.status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_DELETE = _serve

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()


_CONTENDER = r"""
import asyncio, json, sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import fly_runner
from pathlib import Path
api_base, ledger_path, run_id = sys.argv[3], Path(sys.argv[4]), sys.argv[5]
config = fly_runner.FlyConfig(app="runner-app", api_token="t", image="img", region="ams",
                              api_base=api_base, login_machines={"codex": "m-codex"},
                              ledger_path=ledger_path)
job = fly_runner.FlyJob(run_id=run_id, role="lanius", provider="codex", repository="o/r",
                        argv=["codex"], stdin=b"", timeout=60, needs_github=False,
                        neotoma_base_url="https://neotoma.invalid")
async def read(rid):
    return {"run_id": rid, "exit_code": 0, "stdout": rid, "stdout_truncated": False,
            "stderr_tail": "", "wall_ms": 1, "timed_out": False, "refused": "",
            "probe_ok": True}
executor = fly_runner.FlyExecutor(config, read_result=read, mint=lambda r, p: "t",
                                  revoke=lambda t: None)
executor._last_reap = float("inf")
print("ready", flush=True)
sys.stdin.readline()
try:
    rc, out, err = asyncio.run(executor.run(job))
    print(json.dumps({"outcome": "ran", "rc": rc}), flush=True)
except fly_runner.FlyRefusal as exc:
    print(json.dumps({"outcome": "refused", "why": str(exc)}), flush=True)
except Exception as exc:
    print(json.dumps({"outcome": type(exc).__name__, "why": str(exc)}), flush=True)
"""


def test_two_processes_without_a_shared_ledger_only_one_wins_the_machine(fly_env, tmp_path):
    """Two dispatchers in separate processes, each with its OWN ledger (no
    shared reservation, no shared lock), race for one login machine. Both
    pass their ledger checks; the machine's live state lets exactly one in,
    and the other is refused having written nothing."""
    import subprocess as sp

    fake = FakeFly()
    fake.machines["m-codex"] = {"id": "m-codex", "state": "stopped", "login": True,
                                "instance_id": "v1", "config": {"mounts": []}}  # fmt: skip
    # The LEASE alone must decide the race: the double ignores current_version
    # here, since Fly does not document what a mismatch returns.
    fake.enforce_version = False
    shared = _SharedFly(fake, slow_login_wait=1.0)
    script = tmp_path / "contender.py"
    script.write_text(_CONTENDER)
    procs = []
    try:
        for name in ("run-a", "run-b"):
            procs.append(sp.Popen(
                [sys.executable, str(script), str(_REPO_ROOT), str(_DAEMON_DIR), shared.base,
                 str(tmp_path / f"ledger-{name}.json"), name],
                stdin=sp.PIPE, stdout=sp.PIPE, text=True,
            ))  # fmt: skip
        for proc in procs:
            assert proc.stdout.readline().strip() == "ready"
        for proc in procs:
            proc.stdin.write("go\n")
            proc.stdin.flush()
        results = {}
        for name, proc in zip(("run-a", "run-b"), procs):
            results[name] = json.loads(proc.stdout.readline())
            proc.wait(20)
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
        shared.close()
    outcomes = sorted(r["outcome"] for r in results.values())
    assert outcomes == ["ran", "refused"], results
    assert sum(1 for kind, _ in fake.lease_log if kind == "acquire") == 1  # one lease holder
    assert fake.leases == {}
    loser = next(n for n, r in results.items() if r["outcome"] == "refused")
    winner = next(n for n, r in results.items() if r["outcome"] == "ran")
    assert loser not in fake.stored  # the loser's job config was never stored
    assert winner in fake.stored
    starts = [r for r in fake.requests if r.method == "POST" and r.url.path.endswith("/start")]
    assert len(starts) == 1


# ── Leases and mandatory versions (round 8) ──────────────────────────────────


def _machine_writes(fake, machine="m-codex"):
    """update, start, stop and delete calls on the machine (lease calls apart)."""
    return [r for r in fake.requests if r.method in ("POST", "DELETE")
            and f"/machines/{machine}" in r.url.path
            and not r.url.path.endswith("/lease")]  # fmt: skip


@pytest.mark.parametrize("damage", ["no_version", "empty_version", "no_config", "null_config"])
def test_an_incompletely_known_login_machine_is_refused_with_no_writes(
    fly_env, monkeypatch, damage
):
    fake = _codex_fake(monkeypatch)
    machine = fake.machines["m-codex"]
    machine["_raw"] = True  # serve the incomplete answer as is
    if damage == "no_version":
        machine.pop("instance_id")
    elif damage == "empty_version":
        machine["instance_id"] = ""
    elif damage == "no_config":
        machine.pop("config")
    else:
        machine["config"] = None
    executor, _, revoked = make_executor(fake)
    with pytest.raises(fly_runner.FlyRefusal, match="not free") as exc:
        asyncio.run(executor.run(_codex_job()))
    assert exc.value.cooled_until
    assert _machine_writes(fake) == [] and fake.lease_log == []
    assert executor.ledger.snapshot()["in_flight"] == {}
    assert revoked == [MINTED]


def test_a_lease_held_elsewhere_means_busy_with_no_writes(fly_env, monkeypatch):
    fake = _codex_fake(monkeypatch)
    fake.leases["m-codex"] = {"nonce": "someone-elses-nonce", "description": "other",
                              "expires_at": 4_000_000_000}  # fmt: skip
    executor, _, _ = make_executor(fake)
    with pytest.raises(fly_runner.FlyRefusal, match="lease is held by another") as exc:
        asyncio.run(executor.run(_codex_job()))
    assert exc.value.cooled_until
    assert _machine_writes(fake) == []
    assert fake.leases["m-codex"]["nonce"] == "someone-elses-nonce"  # untouched


@pytest.mark.parametrize("mode", ["error", "no_nonce"])
def test_an_unexpected_lease_answer_is_refused(fly_env, monkeypatch, mode):
    fake = _codex_fake(monkeypatch)
    fake.lease_mode = mode
    executor, _, _ = make_executor(fake)
    with pytest.raises(fly_runner.FlyRefusal, match="lease could not be taken"):
        asyncio.run(executor.run(_codex_job()))
    assert _machine_writes(fake) == []


def test_the_nonce_is_sent_on_every_mutating_call(fly_env, monkeypatch):
    fake = _codex_fake(monkeypatch)
    executor, _, _ = make_executor(fake, record={"exit_code": 0, "stdout": "ok"})
    real_wait = executor.client.wait

    async def wait_leaves_running(machine_id, **k):
        if k.get("timeout_s") == 30:
            return await real_wait(machine_id, **k)
        return "reached"  # the job finished, but the machine still reads started

    executor.client.wait = wait_leaves_running
    asyncio.run(executor.run(_codex_job()))
    writes = _machine_writes(fake)
    kinds = {r.url.path.rsplit("/", 1)[1] for r in writes}
    assert {"m-codex", "start", "stop"} <= kinds  # update, start, stop all happened
    acquire = [r for r in fake.requests if r.method == "POST" and r.url.path.endswith("/lease")]
    assert len(acquire) == 1
    for request in writes + [r for r in fake.requests if r.method == "DELETE"
                             and r.url.path.endswith("/lease")]:  # fmt: skip
        assert request.headers.get("fly-machine-lease-nonce"), request.url.path
    assert fake.leases == {}


def test_an_update_without_the_nonce_on_a_leased_machine_is_rejected(fly_env, monkeypatch):
    """The double behaves as Fly documents."""
    fake = _codex_fake(monkeypatch)
    config = fly_runner.FlyConfig.from_env()
    client = fly_runner.FlyMachinesClient(config, transport=httpx.MockTransport(fake.handler))
    nonce, _ = asyncio.run(client.acquire_lease("m-codex", ttl=60, description="t"))
    with pytest.raises(fly_runner.FlyApiError) as exc:
        asyncio.run(client.update("m-codex", {"config": {}, "skip_launch": True}))
    assert exc.value.status == 409
    asyncio.run(client.update("m-codex", {"config": {}, "skip_launch": True}, nonce=nonce))


def test_the_nonce_never_appears_in_logs_results_events_or_json(
    fly_env, seam, monkeypatch, caplog
):
    spawned, fake_exec = seam
    events = []
    monkeypatch.setattr(skill_runner, "_write_harness_event", lambda **kw: events.append(kw))
    fake = _codex_fake(monkeypatch)
    monkeypatch.setattr(skill_runner, "CODEX_BIN", "/usr/bin/codex-test")
    monkeypatch.setenv("APIS_HARNESS_PROVIDERS", "claude,codex")
    executor, _, _ = make_executor(fake, record={"exit_code": 0, "stdout": "fine"})
    monkeypatch.setattr(fly_runner, "get_executor", lambda: executor)
    caplog.set_level(logging.DEBUG)
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = asyncio.run(skill_runner._run_skill_once(
            "lanius", "check", provider="codex", role="lanius", runner="fly",
            repository="owner/repo", include_github_contract=True,
        ))  # fmt: skip
    nonces = set(fly_runner._LEASE_NONCES)
    assert nonces, "no lease was taken"
    surfaces = [caplog.text, json.dumps(events, default=str), result.stdout, result.stderr,
                result.error, json.dumps(fly_runner.redact_nonces(str(result)))]  # fmt: skip
    for request in fake.requests:  # the job files sent to the machine
        surfaces.extend(_decoded_files(request))
    for nonce in nonces:
        for text in surfaces:
            assert nonce not in text
    assert fly_runner.redact_nonces(f"x {next(iter(nonces))} y") == "x [lease-nonce] y"


@pytest.mark.parametrize("path", ["success", "failure", "timeout", "cancellation"])
def test_the_lease_is_released_on_every_exit_path(fly_env, monkeypatch, path):
    fake = _codex_fake(monkeypatch)
    clock = Clock()
    record = {"exit_code": 0 if path == "success" else 1, "stdout": "x"}
    executor, _, _ = make_executor(fake, clock=clock, record=record)
    job = _codex_job()

    async def go():
        if path == "timeout":
            real_wait = executor.client.wait

            async def slow(machine_id, **k):
                if k.get("timeout_s") == 30:
                    return await real_wait(machine_id, **k)
                clock.now += 600
                return "timeout"

            executor.client.wait = slow
            with pytest.raises(asyncio.TimeoutError):
                await executor.run(job)
        elif path == "cancellation":
            started = asyncio.Event()

            async def hang(machine_id, **k):
                if k.get("timeout_s") == 30:
                    return "reached"
                started.set()
                await asyncio.Event().wait()

            executor.client.wait = hang
            task = asyncio.create_task(executor.run(job))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await executor.run(job)

    asyncio.run(go())
    assert fake.leases == {}, path
    assert ("release", "m-codex") in fake.lease_log
    assert executor.ledger.snapshot(clock.now)["in_flight"] == {}


def test_a_lease_whose_release_fails_keeps_the_reservation(fly_env, monkeypatch):
    fake = _codex_fake(monkeypatch)
    fake.release_fails = True
    executor, _, _ = make_executor(fake, record={"exit_code": 0, "stdout": "ok"})
    job = _codex_job()
    asyncio.run(executor.run(job))
    assert job.remote_state == "unconfirmed"
    entry = executor.ledger.entry(job.run_id)
    assert entry is not None and entry["unconfirmed"] is True
    assert entry["lease_nonce"]  # kept, so a later settlement can release it
    fake.release_fails = False
    assert job.run_id in asyncio.run(executor.reap())
    assert fake.leases == {} and executor.ledger.entry(job.run_id) is None


def test_the_run_deadline_never_outlives_the_lease(fly_env, monkeypatch):
    fake = _codex_fake(monkeypatch)
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock)
    real = fake.handler

    def short_lease(request):
        response = real(request)
        if request.method == "POST" and request.url.path.endswith("/lease"):
            data = json.loads(response.content)
            data["data"]["expires_at"] = clock.now + 100  # shorter than the run
            return httpx.Response(200, json=data)
        return response

    executor.client = fly_runner.FlyMachinesClient(
        executor.config, transport=httpx.MockTransport(short_lease)
    )
    seen = {}
    real_wait = executor.client.wait

    async def wait(machine_id, **k):
        if k.get("timeout_s") == 30:
            return await real_wait(machine_id, **k)
        seen.setdefault("first_budget", k.get("timeout_s"))
        clock.now += 50
        return "timeout"

    executor.client.wait = wait
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(executor.run(_codex_job()))
    # Deadline = lease expiry - LEASE_SAFETY_SECONDS = 40 s from start.
    assert seen["first_budget"] <= 100 - fly_runner.LEASE_SAFETY_SECONDS
    assert fake.leases == {}  # stopped, cleaned and released at that deadline


# ── Settlement accepts only positive evidence (round 9) ──────────────────────
#
# Parametrised from the validators' own field lists: a field added to
# fly_runner.REQUIRED_FIELDS is tested here without anyone editing this file.

MODES = ("absent", "null", "wrong")


def _field_cases(kind):
    return [(field, mode) for field in fly_runner.REQUIRED_FIELDS[kind] for mode in MODES]


def _settling_run(fake, executor, monkeypatch):
    """A codex login run that completes; ``phase["settling"]`` turns true
    once its result is read, i.e. for settlement's reads only."""
    phase = {"settling": False}

    async def read(run_id):
        phase["settling"] = True
        return complete_record(run_id, stdout="done")

    executor._read_result = read
    return phase


def _assert_kept_with_nonce(executor, job):
    assert job.remote_state == "unconfirmed"
    entry = executor.ledger.entry(job.run_id)
    assert entry is not None, "the reservation was released on incomplete evidence"
    assert entry["unconfirmed"] is True
    assert entry["lease_nonce"], "the nonce must be kept for the reaper"


@pytest.mark.parametrize("field,mode", _field_cases("machine"))
def test_an_incomplete_machine_answer_never_settles(fly_env, monkeypatch, field, mode):
    fake = _codex_fake(monkeypatch)
    executor, _, _ = make_executor(fake)
    phase = _settling_run(fake, executor, monkeypatch)
    fake.corrupt = {"kind": "machine", "field": field, "mode": mode,
                    "when": lambda request: phase["settling"]}  # fmt: skip
    job = _codex_job()
    asyncio.run(executor.run(job))
    _assert_kept_with_nonce(executor, job)
    assert "m-codex" in fake.leases  # the lease is not released either


@pytest.mark.parametrize("mode", ["absent", "null"])
def test_a_missing_config_on_the_cleanup_read_back_never_settles(fly_env, monkeypatch, mode):
    """Finding: the read after the clean-up update had no config."""
    fake = _codex_fake(monkeypatch)
    executor, _, _ = make_executor(fake, record={"exit_code": 0, "stdout": "ok"})

    def after_cleanup(request):
        return (request.method == "GET" and request.url.path.endswith("/machines/m-codex")
                and bool(fake.stored) and fake.stored[-1] == "")  # fmt: skip

    fake.corrupt = {"kind": "machine", "field": "config", "mode": mode, "when": after_cleanup}
    job = _codex_job()
    asyncio.run(executor.run(job))
    _assert_kept_with_nonce(executor, job)


@pytest.mark.parametrize("field,mode", _field_cases("lease_view"))
def test_an_incomplete_lease_answer_after_a_failed_release_never_settles(
    fly_env, monkeypatch, field, mode
):
    fake = _codex_fake(monkeypatch)
    fake.release_fails = True
    executor, _, _ = make_executor(fake, record={"exit_code": 0, "stdout": "ok"})
    fake.corrupt = {"kind": "lease_view", "field": field, "mode": mode}
    job = _codex_job()
    asyncio.run(executor.run(job))
    _assert_kept_with_nonce(executor, job)


def test_an_empty_lease_answer_after_a_failed_release_never_settles(fly_env, monkeypatch):
    """Finding: ``{"data": {}}`` must not read as "another holder"."""
    fake = _codex_fake(monkeypatch)
    fake.release_fails = True
    fake.lease_view_override = {"status": "success", "data": {}}
    executor, _, _ = make_executor(fake, record={"exit_code": 0, "stdout": "ok"})
    job = _codex_job()
    asyncio.run(executor.run(job))
    _assert_kept_with_nonce(executor, job)


@pytest.mark.parametrize("view,settles", [
    (None, True),                                                      # explicit 404
    ({"status": "success", "data": {"description": "someone else",
                                    "expires_at": 4_000_000_000}}, True),  # named other
])  # fmt: skip
def test_positive_evidence_after_a_failed_release_settles(fly_env, monkeypatch, view, settles):
    fake = _codex_fake(monkeypatch)
    fake.release_fails = True
    executor, _, _ = make_executor(fake, record={"exit_code": 0, "stdout": "ok"})
    job = _codex_job()
    real = fake.handler

    def handler(request):
        if request.method == "GET" and request.url.path.endswith("/lease"):
            if view is None:
                return httpx.Response(404, json={"error": "no lease"})
            return httpx.Response(200, json=view)
        return real(request)

    executor.client = fly_runner.FlyMachinesClient(
        executor.config, transport=httpx.MockTransport(handler)
    )
    asyncio.run(executor.run(job))
    assert (executor.ledger.entry(job.run_id) is None) is settles


@pytest.mark.parametrize("field,mode", _field_cases("list_entry"))
def test_an_incomplete_listing_never_settles_an_uncertain_create(fly_env, field, mode):
    fake = FakeFly()
    fake.create_mode = "lost"
    fake.corrupt = {"kind": "list_entry", "field": field, "mode": mode}
    executor, _, _ = make_executor(fake, record=None)
    job = _job()
    with pytest.raises(fly_runner.FlyUncertainLaunch):
        asyncio.run(executor.run(job))
    assert job.remote_state == "unconfirmed"
    assert executor.ledger.entry(job.run_id)["unconfirmed"] is True
    assert fake.machines  # the machine was not presumed gone


@pytest.mark.parametrize("field,mode", _field_cases("lease_grant"))
def test_an_incomplete_lease_grant_is_refused_with_no_writes(fly_env, monkeypatch, field, mode):
    fake = _codex_fake(monkeypatch)
    fake.corrupt = {"kind": "lease_grant", "field": field, "mode": mode}
    executor, _, _ = make_executor(fake)
    with pytest.raises(fly_runner.FlyRefusal, match="lease could not be taken"):
        asyncio.run(executor.run(_codex_job()))
    assert _machine_writes(fake) == []
    assert executor.ledger.snapshot()["in_flight"] == {}


@pytest.mark.parametrize("field,mode", _field_cases("ledger_entry"))
def test_an_unreadable_reservation_is_never_reaped(fly_env, field, mode):
    fake = FakeFly()
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock)
    dead = {**fly_runner.this_process_owner("apis-dead"), "pid": _dead_pid(), "heartbeat": 0}
    entry = {"run_id": "r-bad", "owner": dead["id"], "machine_id": "m-old", "login": False,
             "started_at": clock.now - 10_000, "deadline": clock.now - 9_000,
             "max_cost_usd": 0.01}  # fmt: skip
    entry = _corrupt(entry, field, mode)
    data = executor.ledger._load()
    executor.ledger._roll(data, clock.now)
    data["in_flight"]["r-bad"] = entry
    data["owners"][dead["id"]] = dead
    executor.ledger._save(_admitted(data))
    fake.machines["m-old"] = {"id": "m-old", "state": "started"}
    # One unreadable reservation makes the whole ledger INVALID: the reaper
    # does no accounting at all, and admission refuses until it is repaired.
    with pytest.raises(fly_runner.FlyLedgerError, match="r-bad"):
        asyncio.run(executor.reap())
    assert "m-old" in fake.machines and not fake.calls("DELETE")
    assert "r-bad" in json.loads(executor.ledger.path.read_text())["in_flight"]
    with pytest.raises(fly_runner.FlyRefusal, match="needs operator repair"):
        executor.ledger.reserve(executor.config, run_id="next", owner="t", max_run_seconds=60,
                                now=clock.now)  # fmt: skip


def test_the_reaper_finishes_once_complete_answers_return(fly_env, monkeypatch):
    """Recovery after a later valid answer: the kept reservation, with its
    nonce, is settled and the lease released by the reaper."""
    fake = _codex_fake(monkeypatch)
    fake.release_fails = True
    fake.lease_view_override = {"status": "success", "data": {}}
    executor, _, _ = make_executor(fake, record={"exit_code": 0, "stdout": "ok"})
    job = _codex_job()
    asyncio.run(executor.run(job))
    assert executor.ledger.entry(job.run_id)["lease_nonce"]
    fake.release_fails = False
    fake.lease_view_override = None
    assert job.run_id in asyncio.run(executor.reap())
    assert fake.leases == {} and executor.ledger.entry(job.run_id) is None


def test_validator_field_lists_cover_every_decision_field():
    assert set(fly_runner.REQUIRED_FIELDS) == {
        "machine", "list_entry", "lease_view", "lease_grant", "ledger_entry", "ledger",
        "owner_record", "result_record"}
    assert "config" in fly_runner.REQUIRED_FIELDS["machine"]
    assert "description" in fly_runner.REQUIRED_FIELDS["lease_view"]
    for kind, fields in fly_runner.REQUIRED_FIELDS.items():
        for field in fields:
            for mode in MODES:
                sample = {name: _sample(spec) for name, spec in fields.items()}
                with pytest.raises(fly_runner.FlyInvalid):
                    fly_runner.validate(kind, _corrupt(sample, field, mode))
        fly_runner.validate(kind, {name: _sample(spec) for name, spec in fields.items()})


def _sample(spec):
    return {fly_runner.NONEMPTY_STR: "x", fly_runner.STRING: "x", fly_runner.OBJECT: {},
            fly_runner.NUMBER: 1.0, fly_runner.INTEGER: 1, fly_runner.COUNT: 1, fly_runner.POSITIVE_INT: 4242,
            fly_runner.BOOLEAN: False, fly_runner.MONTH: "2026-10",
            fly_runner.DAY: "2026-10-14"}[spec]  # fmt: skip


def test_runner_probe_treats_an_unreadable_path_as_reachable(monkeypatch, tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "fly_run_job_r9", _REPO_ROOT / "deploy" / "fly-runner" / "run_job.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["fly_run_job_r9"] = module
    spec.loader.exec_module(module)

    def broken(*a, **k):
        raise OSError("cannot tell")

    monkeypatch.setattr(module.os, "access", broken)
    (tmp_path / "f").write_text("x")
    assert module._readable(tmp_path / "f") is True
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    import pwd

    monkeypatch.setattr(pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name)))
    with pytest.raises(RuntimeError, match="unprivileged"):
        module.child_identity("agent")


# ── Accounting, results and liveness: every field validated (round 10) ───────


@pytest.mark.parametrize("field,mode", _field_cases("ledger"))
def test_an_incomplete_ledger_refuses_every_launch_and_never_resets(fly_env, field, mode):
    """An exhausted ledger whose period marker or counter is absent, null or
    the wrong type must not read as a new period: no launch, no reset."""
    fake = FakeFly()
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock)
    month = datetime.fromtimestamp(clock.now, tz=timezone.utc).strftime("%Y-%m")
    day = datetime.fromtimestamp(clock.now, tz=timezone.utc).strftime("%Y-%m-%d")
    exhausted = {"month": month, "day": day, "spend_usd": 19.999, "launches_today": 50,
                 "local_fallbacks": 0, "in_flight": {}, "owners": {}}  # fmt: skip
    damaged = _corrupt(exhausted, field, mode)
    executor.ledger.path.parent.mkdir(parents=True, exist_ok=True)
    executor.ledger.path.write_text(json.dumps(damaged))
    with pytest.raises(fly_runner.FlyRefusal, match="needs operator repair") as exc:
        asyncio.run(executor.run(_job()))
    assert exc.value.cooled_until == ""  # waiting does not fix it
    assert fake.requests == []
    assert json.loads(executor.ledger.path.read_text()) == damaged  # never reset


@pytest.mark.parametrize("marker", ["month", "day"])
@pytest.mark.parametrize("value", ["", "2026-13", "not-a-date", "2026-10-14T00:00"])
def test_a_malformed_period_marker_is_refused(fly_env, tmp_path, marker, value):
    config = fly_runner.FlyConfig.from_env()
    path = tmp_path / "l.json"
    data = {"month": "2027-01", "day": "2027-01-15", "spend_usd": 19.9, "launches_today": 49,
            "local_fallbacks": 0, "in_flight": {}, "owners": {}, marker: value}  # fmt: skip
    path.write_text(json.dumps(data))
    with pytest.raises(fly_runner.FlyRefusal, match="needs operator repair"):
        fly_runner.FlyLedger(path).reserve(config, run_id="r", owner="t", max_run_seconds=60,
                                           now=1_800_000_000)  # fmt: skip


def test_a_genuine_rollover_resets_the_period(fly_env, tmp_path):
    config = fly_runner.FlyConfig.from_env()
    path = tmp_path / "l.json"
    path.write_text(json.dumps({"month": "2026-12", "day": "2026-12-31", "spend_usd": 19.99,
                                "launches_today": 50, "local_fallbacks": 0, "in_flight": {},
                                "owners": {}}))  # fmt: skip
    ledger = fly_runner.FlyLedger(path)
    ledger.reserve(config, run_id="r", owner="t", max_run_seconds=60, now=1_800_000_000)
    data = json.loads(path.read_text())
    assert data["month"] == "2027-01" and data["launches_today"] == 1
    assert data["spend_usd"] == 0.0


@pytest.mark.parametrize("field,mode", _field_cases("result_record"))
def test_an_incomplete_result_record_is_an_unknown_outcome(fly_env, field, mode):
    fake = FakeFly()
    executor, _, _ = make_executor(fake)
    record = _corrupt(complete_record("placeholder", stdout="looks fine"), field, mode)

    async def read(run_id):
        if "run_id" in record and record["run_id"] == "placeholder":
            return {**record, "run_id": run_id}
        return record

    executor._read_result = read
    job = _job()
    rc, out, err = asyncio.run(executor.run(job))
    assert rc == 1 and job.outcome_unknown is True
    assert b"outcome is unknown" in err and b"looks fine" not in out


@pytest.mark.parametrize("field,mode", _field_cases("owner_record"))
def test_an_incomplete_owner_record_never_proves_death(fly_env, field, mode):
    """A dead owner whose liveness record is incomplete: nothing about it is
    known, so its reservation is not reclaimed (it stays reserved)."""
    fake = FakeFly()
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock)
    dead = {**fly_runner.this_process_owner("apis-dead-owner"), "pid": _dead_pid(),
            "heartbeat": clock.now}  # fmt: skip
    record = _corrupt(dead, field, mode)
    data = executor.ledger._load()
    executor.ledger._roll(data, clock.now)
    data["in_flight"]["their-run"] = {
        "run_id": "their-run", "owner": "apis-dead-owner", "machine_id": "m-theirs",
        "login": False, "started_at": clock.now, "deadline": clock.now + 600,
        "max_cost_usd": 0.01,
    }  # fmt: skip
    data["owners"]["apis-dead-owner"] = record
    executor.ledger._save(_admitted(data))
    fake.machines["m-theirs"] = {"id": "m-theirs", "state": "started"}
    with pytest.raises(fly_runner.FlyLedgerError, match="apis-dead-owner"):
        asyncio.run(executor.reap())  # an unreadable owner record: no accounting
    assert "m-theirs" in fake.machines
    assert "their-run" in json.loads(executor.ledger.path.read_text())["in_flight"]


@pytest.mark.parametrize("pid", [0, -1, 1, True, 12.5, "4242"])
def test_a_pid_that_is_not_a_real_process_id_proves_nothing(fly_env, pid):
    record = {"id": "x", "pid": pid, "host": __import__("socket").gethostname(),
              "start": "some start", "heartbeat": 1.0}  # fmt: skip
    assert fly_runner.owner_provably_dead(record) is False


def test_a_running_process_is_never_reported_dead_when_its_start_is_unreadable(monkeypatch):
    import os as _os
    import subprocess as _sp

    monkeypatch.setattr(fly_runner.Path, "exists", lambda self: False)  # no /proc

    def empty_ps(*a, **k):
        return _sp.CompletedProcess(a, 0, stdout="", stderr="")

    monkeypatch.setattr(fly_runner.subprocess, "run", empty_ps)
    assert fly_runner.process_start_token(_os.getpid()) == ""  # alive, start unknown
    record = {"id": "x", "pid": _os.getpid(), "host": __import__("socket").gethostname(),
              "start": "recorded", "heartbeat": 1.0}  # fmt: skip
    assert fly_runner.owner_provably_dead(record) is False


@pytest.mark.parametrize("field", ["spend_usd", "launches_today", "local_fallbacks"])
def test_a_negative_ledger_counter_is_refused_not_trusted(fly_env, tmp_path, field):
    """A negative spend or launch count would widen the caps: refused."""
    config = fly_runner.FlyConfig.from_env()
    path = tmp_path / "l.json"
    month = datetime.fromtimestamp(1_800_000_000, tz=timezone.utc).strftime("%Y-%m")
    day = datetime.fromtimestamp(1_800_000_000, tz=timezone.utc).strftime("%Y-%m-%d")
    data = {"month": month, "day": day, "spend_usd": 19.9, "launches_today": 49,
            "local_fallbacks": 0, "in_flight": {}, "owners": {}, field: -100}  # fmt: skip
    path.write_text(json.dumps(data))
    with pytest.raises(fly_runner.FlyRefusal, match="needs operator repair"):
        fly_runner.FlyLedger(path).reserve(config, run_id="r", owner="t", max_run_seconds=60,
                                           now=1_800_000_000)  # fmt: skip
    assert json.loads(path.read_text()) == data



# ── Cost, capacity and time limits: one strict loader (round 11) ─────────────

_LIMIT_ENVS = [lim.env for lim in fly_runner.LIMITS]


def _above(limit):
    return str(int(limit.high) * 10 + 1)


_BAD_SETTING_VALUES = {
    "empty": lambda lim: "",
    "blank": lambda lim: "   ",
    "text": lambda lim: "twenty",
    "negative": lambda lim: "-5",
    "zero": lambda lim: "0",
    "nan": lambda lim: "NaN",
    "nan-spaced": lambda lim: " nan ",
    "infinity": lambda lim: "inf",
    "infinity-word": lambda lim: "Infinity",
    "exponent": lambda lim: "1e3",
    "underscore": lambda lim: "1_0",
    "non-ascii-digits": lambda lim: "\uff12\uff10",
    "above-upper-bound": _above,
}


@pytest.mark.parametrize("bad", sorted(_BAD_SETTING_VALUES))
@pytest.mark.parametrize("name", _LIMIT_ENVS)
def test_an_invalid_limit_setting_refuses_every_launch(fly_env, monkeypatch, name, bad):
    limit = next(lim for lim in fly_runner.LIMITS if lim.env == name)
    job = _job()  # built while the settings were valid
    monkeypatch.setenv(name, _BAD_SETTING_VALUES[bad](limit))
    fake = FakeFly()
    executor, minted, _ = make_executor(fake)
    assert any(p.startswith(name + " ") for p in executor.config.limit_problems())
    with pytest.raises(fly_runner.FlyConfigInvalid, match=name) as exc:
        _job()  # build_job refuses: Fly is disabled
    assert exc.value.cooled_until == ""  # waiting does not fix it
    with pytest.raises(fly_runner.FlyConfigInvalid, match=name):
        asyncio.run(executor.run(job))  # and so does admission
    assert fake.requests == [] and minted == []
    assert not executor.ledger.path.exists()  # nothing reserved or written


@pytest.mark.parametrize("name", _LIMIT_ENVS)
def test_an_unset_limit_uses_its_documented_default(fly_env, monkeypatch, name):
    monkeypatch.delenv(name, raising=False)
    limit = next(lim for lim in fly_runner.LIMITS if lim.env == name)
    config = fly_runner.FlyConfig.from_env()
    assert config.limit_problems() == []
    assert getattr(config, limit.attr) == pytest.approx(limit.default / limit.scale)
    assert limit.low <= limit.default <= limit.high  # the default is bound-checked


def test_every_limit_is_finite_bounded_above_zero_and_has_a_default_or_is_required():
    for limit in (*fly_runner.LIMITS, *fly_runner.FIXED_LIMITS):
        assert 0 < limit.low < limit.high < float("inf"), limit
        assert limit.default is None or limit.low <= limit.default <= limit.high, limit


def test_valid_limit_settings_are_admitted(fly_env, monkeypatch):
    for name, value in {"APIS_FLY_MONTHLY_BUDGET_USD": "25", "APIS_FLY_MAX_MACHINES_PER_DAY": "40",
                        "APIS_FLY_MAX_CONCURRENT": "2", "APIS_FLY_QUEUE_MAX_WAIT_SECONDS": "600",
                        "APIS_FLY_RATE_PER_HOUR_USD": "0.05",
                        "APIS_FLY_STORAGE_GB_MONTH_USD": " 0.2 "}.items():  # fmt: skip
        monkeypatch.setenv(name, value)
    fake = FakeFly()
    executor, _, _ = make_executor(fake, record={"stdout": "ok"})
    assert executor.config.limit_problems() == []
    assert executor.config.budget_usd == 25.0 and executor.config.max_per_day == 40
    assert executor.config.rate_per_second_usd == pytest.approx(0.05 / 3600)
    rc, out, _ = asyncio.run(executor.run(_job()))
    assert rc == 0 and b"ok" in out and fake.requests


_BAD_FIELD_VALUES = [math.nan, math.inf, -math.inf, -1, 0, 10**9, True, "20", None]


@pytest.mark.parametrize("value", _BAD_FIELD_VALUES, ids=repr)
@pytest.mark.parametrize("limit", [*fly_runner.LIMITS, *fly_runner.FIXED_LIMITS],
                         ids=lambda lim: lim.attr)  # fmt: skip
def test_a_directly_constructed_config_with_an_invalid_limit_refuses(fly_env, tmp_path,
                                                                     limit, value):  # fmt: skip
    job = _job()
    fake = FakeFly()
    executor, minted, _ = make_executor(fake, **{limit.attr: value})
    with pytest.raises(fly_runner.FlyConfigInvalid, match=limit.env):
        asyncio.run(executor.run(job))
    with pytest.raises(fly_runner.FlyConfigInvalid, match=limit.env):
        fly_runner.FlyLedger(tmp_path / "l.json").reserve(
            executor.config, run_id="r", owner="t", max_run_seconds=60, now=1_800_000_000)
    assert fake.requests == [] and minted == [] and not (tmp_path / "l.json").exists()


@pytest.mark.parametrize("attr", ["budget_usd", "rate_per_second_usd", "storage_gb_month_usd",
                                  "max_per_day", "max_concurrent"])  # fmt: skip
def test_cap_comparisons_refuse_on_nan_even_without_the_settings_check(fly_env, tmp_path,
                                                                       monkeypatch, attr):  # fmt: skip
    """Defence in depth: every cap comparison is written so NaN fails it."""
    monkeypatch.setattr(fly_runner.FlyConfig, "require_valid_limits", lambda self: None)
    config = fly_runner.FlyConfig.from_env()
    config.login_machines = {"codex": "mc"}  # storage counts
    setattr(config, attr, math.nan)
    with pytest.raises(fly_runner.FlyRefusal):
        fly_runner.FlyLedger(tmp_path / "l.json").reserve(
            config, run_id="r", owner="t", max_run_seconds=60, now=1_800_000_000)
    assert not (tmp_path / "l.json").exists()


@pytest.mark.parametrize("timeout", [0, -60, math.nan, math.inf, 10**9, True, "60"], ids=repr)
def test_a_dispatch_timeout_out_of_bounds_is_refused(fly_env, timeout):
    with pytest.raises(fly_runner.FlyConfigInvalid, match="timeout"):
        _job(timeout=timeout)


def test_an_invalid_limit_is_a_refusal_never_a_local_fallback(fly_env, seam, monkeypatch):
    spawned, fake_exec = seam
    monkeypatch.setenv("APIS_FLY_MONTHLY_BUDGET_USD", "NaN")
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "carry_forward_check")
    fake = FakeFly()
    executor, _, _ = make_executor(fake)
    assert executor.config.local_fallback  # fallback is on, and still not used
    monkeypatch.setattr(fly_runner, "get_executor", lambda: executor)
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        opted = _once(action_class="carry_forward_check", model="m", repository="owner/repo",
                      github_token="t-local", include_github_contract=True)  # fmt: skip
        forced = _once(runner="fly", repository="owner/repo", include_github_contract=True)
    for result in (opted, forced):
        assert not result.ok and "APIS_FLY_MONTHLY_BUDGET_USD" in result.error
    assert spawned == [] and fake.requests == []


def test_the_startup_check_logs_an_invalid_limit(fly_env, monkeypatch, caplog):
    monkeypatch.setenv("APIS_FLY_MAX_CONCURRENT", "-1")
    with caplog.at_level("ERROR", logger="apis.fly_runner"):
        problems = fly_runner.log_limit_check()
    assert problems and "APIS_FLY_MAX_CONCURRENT" in caplog.text


def test_the_watchdog_never_crashes_on_an_invalid_queue_wait():
    from task_watchdog import stall_seconds

    base = {"APIS_DISPATCH_TIMEOUT": "1800", "APIS_STALL_SECONDS": "60",
            "APIS_FLY_RUNNER_CLASSES": "carry_forward_check"}  # fmt: skip
    valid = stall_seconds({**base, "APIS_FLY_QUEUE_MAX_WAIT_SECONDS": "900"})
    for bad in ("NaN", "-5", "abc", "1e9", ""):
        assert stall_seconds({**base, "APIS_FLY_QUEUE_MAX_WAIT_SECONDS": bad}) == valid - 900 - 120


# ── Ledger values arriving from disk ─────────────────────────────────────────


def _ledger_now(clock_now=1_800_000_000):
    when = datetime.fromtimestamp(clock_now, tz=timezone.utc)
    return when.strftime("%Y-%m"), when.strftime("%Y-%m-%d")


@pytest.mark.parametrize("marker,value", [
    ("month", "\uff12\uff10\uff12\uff17-01"),
    ("month", "\u0662\u0660\u0662\u0667-01"),
    ("day", "2027-02-31"),                         # not a calendar day
    ("day", "2027-01-15 "),                        # trailing space
    ("month", "2099-01"),                          # a later period than now
    ("day", "2099-01-01"),
], ids=["non-ascii-1", "non-ascii-2", "feb-31", "trailing-space", "future-month", "future-day"])
def test_a_non_canonical_or_future_period_marker_is_refused_not_reset(fly_env, tmp_path,
                                                                       marker, value):  # fmt: skip
    config = fly_runner.FlyConfig.from_env()
    month, day = _ledger_now()
    data = {"month": month, "day": day, "spend_usd": 100.0, "launches_today": 0,
            "local_fallbacks": 0, "in_flight": {}, "owners": {}, marker: value}  # fmt: skip
    path = tmp_path / "l.json"
    path.write_text(json.dumps(data))
    with pytest.raises(fly_runner.FlyRefusal, match="needs operator repair"):
        fly_runner.FlyLedger(path).reserve(config, run_id="r", owner="t", max_run_seconds=60,
                                           now=1_800_000_000)  # fmt: skip
    assert json.loads(path.read_text()) == data


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity", "1e400", "1" + "0" * 400],
                         ids=["nan", "inf", "-inf", "float-overflow", "huge-int"])
def test_a_non_finite_or_huge_ledger_number_is_refused(fly_env, tmp_path, raw):
    config = fly_runner.FlyConfig.from_env()
    month, day = _ledger_now()
    text = ('{"month": "%s", "day": "%s", "spend_usd": %s, "launches_today": 0, '
            '"local_fallbacks": 0, "in_flight": {}, "owners": {}}') % (month, day, raw)
    path = tmp_path / "l.json"
    path.write_text(text)
    with pytest.raises(fly_runner.FlyRefusal, match="needs operator repair"):
        fly_runner.FlyLedger(path).reserve(config, run_id="r", owner="t", max_run_seconds=60,
                                           now=1_800_000_000)  # fmt: skip
    assert path.read_text() == text


def test_a_reservation_whose_window_ends_before_it_starts_is_charged_in_full():
    entry = {"run_id": "r", "owner": "o", "started_at": 1000.0, "deadline": 10.0,
             "max_cost_usd": 0.0, "login": False}  # fmt: skip
    charged = fly_runner._charged_seconds(entry, 5000.0)
    assert charged == fly_runner.DISPATCH_TIMEOUT_DEFAULT + fly_runner.APIS_DEADLINE_GRACE_SECONDS


@pytest.mark.parametrize("value", [math.nan, math.inf, -1.0, True, "soon"], ids=repr)
def test_a_reservation_with_an_unreadable_lease_expiry_is_invalid(value):
    entry = {"run_id": "r", "owner": "o", "started_at": 1.0, "deadline": 2.0,
             "max_cost_usd": 0.1, "login": True, "lease_expires": value}  # fmt: skip
    with pytest.raises(fly_runner.FlyInvalid):
        fly_runner.validate("ledger_entry", entry)


@pytest.mark.parametrize("value", [math.nan, math.inf, -5.0, 10**400], ids=repr)
@pytest.mark.parametrize("kind", ["lease_grant", "lease_view"])
def test_a_machines_api_time_that_is_not_finite_is_invalid(kind, value):
    data = {"nonce": "n", "description": "d", "expires_at": value}
    with pytest.raises(fly_runner.FlyInvalid):
        fly_runner.validate(kind, {k: data[k] for k in fly_runner.REQUIRED_FIELDS[kind]})


def test_a_charge_that_is_not_finite_keeps_the_reservation(fly_env, tmp_path):
    config = fly_runner.FlyConfig.from_env()
    ledger = fly_runner.FlyLedger(tmp_path / "l.json")
    ledger.reserve(config, run_id="r", owner="t", max_run_seconds=60, now=1_800_000_000)
    before = (tmp_path / "l.json").read_text()
    config.rate_per_second_usd = math.nan
    with pytest.raises(fly_runner.FlyLedgerError):
        ledger.settle("r", config=config, now=1_800_000_030)
    assert (tmp_path / "l.json").read_text() == before  # still reserved, nothing charged


def test_the_ledger_never_writes_a_non_finite_number(tmp_path):
    ledger = fly_runner.FlyLedger(tmp_path / "l.json")
    data = ledger._load()
    ledger._roll(data, 1_800_000_000)
    data["spend_usd"] = math.nan
    with pytest.raises(fly_runner.FlyLedgerError, match="non-finite"):
        ledger._save(_admitted(data))
    assert not (tmp_path / "l.json").exists()


def test_an_untracked_machine_created_in_the_future_is_charged_the_whole_window(fly_env):
    """A creation time later than now would charge nothing: it is unreadable."""
    fake = FakeFly()
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock)
    fake.machines["m-ghost"] = {"id": "m-ghost", "state": "started",
                                "created_at": "2099-01-01T00:00:00Z"}  # fmt: skip
    assert "untracked-m-ghost" in asyncio.run(executor.reap())
    window = fly_runner.DISPATCH_TIMEOUT_DEFAULT + fly_runner.APIS_DEADLINE_GRACE_SECONDS
    spend = executor.ledger.snapshot(clock.now)["spend_usd"]
    assert spend == pytest.approx(window * executor.config.rate_per_second_usd)


def test_a_clock_that_reads_before_the_start_charges_the_whole_window():
    entry = {"run_id": "r", "owner": "o", "started_at": 1000.0, "deadline": 1300.0,
             "max_cost_usd": 0.1, "login": False}  # fmt: skip
    assert fly_runner._charged_seconds(entry, 900.0) == 300.0
    assert fly_runner._charged_seconds(entry, 1100.0) == 100.0
    assert fly_runner._charged_seconds(entry, math.nan) == 300.0



# ── Admission reads ONE validated snapshot (round 12) ────────────────────────

_NOW = 1_800_000_000  # 2027-01-15 UTC


def _valid_ledger(config, now=_NOW):
    """A ledger admission accepts: two held reservations, owners, counters."""
    month, day = _ledger_now(now)
    rate = config.rate_per_second_usd
    entries = {
        rid: {"run_id": rid, "owner": "o1", "machine_id": f"m-{rid}", "login": False,
              "started_at": now - 100.0, "deadline": now - 100.0 + window,
              "max_cost_usd": window * rate}
        for rid, window in (("r1", 600.0), ("r2", 1920.0))
    }  # fmt: skip
    owner = {"id": "o1", "pid": 4242, "host": "h", "start": "s", "heartbeat": float(now)}
    return {"month": month, "day": day, "spend_usd": 1.0, "launches_today": 2,
            "local_fallbacks": 0, "in_flight": entries, "owners": {"o1": owner}}  # fmt: skip


@pytest.mark.parametrize("cost", [-1, 10**13, "unknown", None, 0.0, 1e-9], ids=repr)
def test_admission_refuses_a_held_reservation_whose_cost_it_cannot_trust(fly_env, tmp_path,
                                                                         cost):  # fmt: skip
    """The same untrustworthy reservation refuses admission; a correct one is
    counted in full (control). An unreadable one also stops recovery from
    settling or releasing anything; one that is readable but too low for
    its window (e.g. after a rate rise) still lets recovery free capacity,
    and only admission waits."""
    config = fly_runner.FlyConfig.from_env()
    config.budget_usd = 0.1
    rate = config.rate_per_second_usd
    data = _valid_ledger(config)
    data["spend_usd"] = 0.0
    long = data["in_flight"]["r2"]
    long["deadline"] = long["started_at"] + 21_720
    long["max_cost_usd"] = 21_720 * rate
    path = tmp_path / "l.json"
    ledger = fly_runner.FlyLedger(path, clock=lambda: _NOW)
    # Control: the correct cost is counted and the run does not fit.
    path.write_text(json.dumps(data))
    with pytest.raises(fly_runner.FlyRefusal, match="budget"):
        ledger.reserve(config, run_id="new", owner="t", max_run_seconds=1920, now=_NOW)
    # The same state with a cost that cannot be trusted: refused as invalid.
    long["max_cost_usd"] = cost
    path.write_text(json.dumps(data))
    with pytest.raises(fly_runner.FlyRefusal, match="needs operator repair"):
        ledger.reserve(config, run_id="new", owner="t", max_run_seconds=1920, now=_NOW)
    assert json.loads(path.read_text()) == data
    if not isinstance(cost, float):
        # Recovery on the same state: nothing is settled or released.
        with pytest.raises(fly_runner.FlyLedgerError):
            ledger.settle("r1", config=config, now=_NOW)
        with pytest.raises(fly_runner.FlyLedgerError):
            ledger.release("r1")
        assert json.loads(path.read_text()) == data
    else:
        ledger.release("r1")  # frees capacity; the low reservation stays held
        assert set(json.loads(path.read_text())["in_flight"]) == {"r2"}


def test_a_valid_ledger_admits_within_its_caps(fly_env, tmp_path):
    config = fly_runner.FlyConfig.from_env()
    path = tmp_path / "l.json"
    path.write_text(json.dumps(_valid_ledger(config)))
    ledger = fly_runner.FlyLedger(path, clock=lambda: _NOW)
    ledger.reserve(config, run_id="new", owner="t", max_run_seconds=600, now=_NOW)
    after = json.loads(path.read_text())
    assert set(after["in_flight"]) == {"r1", "r2", "new"} and after["launches_today"] == 3


@pytest.mark.parametrize("change", ["launch-count-below-today", "cost-below-window",
                                    "key-names-another-run", "owner-key-mismatch"])  # fmt: skip
def test_a_stored_total_that_disagrees_with_the_reservations_refuses(fly_env, tmp_path, change):
    config = fly_runner.FlyConfig.from_env()
    data = _valid_ledger(config)
    if change == "launch-count-below-today":
        data["launches_today"] = 1
    elif change == "cost-below-window":
        data["in_flight"]["r1"]["max_cost_usd"] *= 0.5
    elif change == "key-names-another-run":
        data["in_flight"]["r1"]["run_id"] = "r9"
    else:
        data["owners"]["o1"]["id"] = "o9"
    path = tmp_path / "l.json"
    path.write_text(json.dumps(data))
    with pytest.raises(fly_runner.FlyRefusal, match="needs operator repair"):
        fly_runner.FlyLedger(path, clock=lambda: _NOW).reserve(
            config, run_id="new", owner="t", max_run_seconds=600, now=_NOW)
    assert json.loads(path.read_text()) == data


def test_a_run_id_already_reserved_is_never_overwritten(fly_env, tmp_path):
    config = fly_runner.FlyConfig.from_env()
    path = tmp_path / "l.json"
    path.write_text(json.dumps(_valid_ledger(config)))
    with pytest.raises(fly_runner.FlyRefusal, match="already holds"):
        fly_runner.FlyLedger(path, clock=lambda: _NOW).reserve(
            config, run_id="r1", owner="t", max_run_seconds=600, now=_NOW)


def test_a_settlement_on_an_unusable_ledger_keeps_the_result_and_the_reservation(fly_env):
    """A completed run whose ledger became unusable: its result still comes
    back, its machine state says unconfirmed, and nothing is released."""
    fake = FakeFly()
    executor, _, _ = make_executor(fake, record={"stdout": "done"})
    real = executor.ledger.settle

    def broken(*a, **k):
        raise fly_runner.FlyLedgerError("unusable")

    executor.ledger.settle = broken
    job = _job()
    rc, out, _ = asyncio.run(executor.run(job))
    assert rc == 0 and b"done" in out and job.remote_state == "unconfirmed"
    assert job.run_id in executor.ledger.snapshot()["in_flight"]
    executor.ledger.settle = real


# Property: reserve admits ONLY from a fully valid ledger, and then exactly
# when the caps allow; every other generated ledger is refused unchanged.

_CORRUPTIONS = {
    "absent": None, "null": None, "text": "x", "list": [], "object": {}, "true": True,
    "negative": -1, "negative-float": -0.5, "zero": 0, "huge": 1e13, "huge-int": 10**400,
    "nan": math.nan, "inf": math.inf, "bad-month": "2027-13", "bad-day": "2027-02-30",
    "future-month": "2099-01", "future-day": "2099-01-01", "past-month": "2026-12",
    "past-day": "2027-01-14", "renamed": "other",
}
_LEDGER_FIELDS = ["month", "day", "spend_usd", "launches_today", "local_fallbacks",
                  "in_flight", "owners"]  # fmt: skip
_ENTRY_FIELDS = ["run_id", "owner", "started_at", "deadline", "max_cost_usd", "login",
                 "machine_id", "lease_expires", "unconfirmed", "untracked"]  # fmt: skip
_OWNER_FIELDS = ["id", "pid", "host", "start", "heartbeat"]


def _apply(target: dict, field: str, mode: str) -> None:
    if mode == "absent":
        target.pop(field, None)
    elif mode == "renamed":
        target[field] = "other"
    else:
        target[field] = copy.deepcopy(_CORRUPTIONS[mode])


def _num(x, low=0, high=1e12):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and low <= x <= high


def _marker(x, fmt, current):
    if not isinstance(x, str) or not re.fullmatch(r"[0-9-]+", x, re.ASCII):
        return False
    try:
        ok = datetime.strptime(x, fmt).strftime(fmt) == x
    except ValueError:
        return False
    return ok and x <= current


def _oracle(doc, config, max_run, now=_NOW):
    """An independent reading of the ledger: (valid, admitted-if-valid)."""
    month, day = _ledger_now(now)
    if not isinstance(doc, dict):
        return False, False
    try:
        if not (_marker(doc["month"], "%Y-%m", month) and _marker(doc["day"], "%Y-%m-%d", day)):
            return False, False
        spend, launches, falls = doc["spend_usd"], doc["launches_today"], doc["local_fallbacks"]
        entries, owners = doc["in_flight"], doc["owners"]
    except KeyError:
        return False, False
    if not (_num(spend) and isinstance(launches, int) and _num(launches, 0, 10**9)
            and isinstance(falls, int) and _num(falls, 0, 10**9)
            and isinstance(entries, dict) and isinstance(owners, dict)):  # fmt: skip
        return False, False
    rate = config.rate_per_second_usd
    for key, e in entries.items():
        if not isinstance(e, dict):
            return False, False
        need = {"run_id", "owner", "started_at", "deadline", "max_cost_usd", "login"}
        if not need <= set(e) or any(e[k] is None for k in need):
            return False, False
        if not (e["run_id"] == key and isinstance(e["owner"], str) and e["owner"]
                and _num(e["started_at"]) and _num(e["deadline"]) and _num(e["max_cost_usd"])
                and isinstance(e["login"], bool) and e["deadline"] >= e["started_at"]):  # fmt: skip
            return False, False
        for name, ok in (("machine_id", lambda v: isinstance(v, str)),
                         ("unconfirmed", lambda v: isinstance(v, bool)),
                         ("untracked", lambda v: isinstance(v, bool)),
                         ("lease_expires", lambda v: v is None or _num(v))):  # fmt: skip
            if name in e and not ok(e[name]):
                return False, False
        if not e["max_cost_usd"] >= (e["deadline"] - e["started_at"]) * rate * (1 - 1e-9):
            return False, False
    for key, o in owners.items():
        if not (isinstance(o, dict) and set(_OWNER_FIELDS) <= set(o)
                and o["id"] == key and isinstance(o["host"], str) and o["host"]
                and isinstance(o["start"], str) and _num(o["heartbeat"])
                and isinstance(o["pid"], int) and not isinstance(o["pid"], bool)
                and o["pid"] > 1):  # fmt: skip
            return False, False
    spend = 0.0 if doc["month"] < month else float(spend)
    launches = 0 if doc["day"] < day else launches
    today = sum(1 for e in entries.values()
                if not e.get("untracked") and fly_runner._day_key(e["started_at"]) == day)
    if not launches >= today:
        return False, False
    committed = spend + config.monthly_storage_usd() + sum(e["max_cost_usd"] for e in entries.values())
    admitted = (committed + max_run * rate <= config.budget_usd
                and launches < config.max_per_day and len(entries) < config.max_concurrent)  # fmt: skip
    return True, admitted


def test_reserve_never_admits_from_an_unvalidated_ledger(fly_env, tmp_path):
    import random

    config = fly_runner.FlyConfig.from_env()
    config.max_concurrent = 5
    rng = random.Random(1456)
    outcomes = {"admitted": 0, "refused-invalid": 0, "refused-cap": 0}
    for case in range(600):
        doc = _valid_ledger(config)
        if case % 4 == 0:
            doc["spend_usd"] = 19.99  # valid ledgers that are over budget
        elif case % 4 == 1:
            doc["launches_today"] = config.max_per_day  # valid, at the daily cap
        applied = []
        for _ in range(rng.randint(0, 3)):
            mode = rng.choice(sorted(_CORRUPTIONS))
            where = rng.choice(["ledger", "entry", "owner"])
            if where == "ledger":
                field = rng.choice(_LEDGER_FIELDS)
                _apply(doc, field, mode)
            elif where == "entry" and isinstance(doc.get("in_flight"), dict) and doc["in_flight"]:
                key = rng.choice(sorted(doc["in_flight"]))
                field = rng.choice(_ENTRY_FIELDS)
                if isinstance(doc["in_flight"][key], dict):
                    _apply(doc["in_flight"][key], field, mode)
            elif where == "owner" and isinstance(doc.get("owners"), dict) and doc["owners"]:
                field = rng.choice(_OWNER_FIELDS)
                if isinstance(doc["owners"]["o1"], dict):
                    _apply(doc["owners"]["o1"], field, mode)
            else:
                continue
            applied.append((where, field, mode))
        path = tmp_path / f"l{case}.json"
        text = json.dumps(doc)  # NaN and Infinity are written as JSON allows
        if case % 10 == 9:
            # The whole document replaced: never "no ledger", always refused.
            text = rng.choice(sorted(_WHOLE_DOCUMENTS.values()))
            applied.append(("document", "", text[:20]))
        path.write_text(text)
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        valid, expected = _oracle(parsed, config, 600) if text.strip() else (False, False)
        ledger = fly_runner.FlyLedger(path, clock=lambda: _NOW)
        try:
            ledger.reserve(config, run_id="new", owner="t", max_run_seconds=600, now=_NOW)
            admitted = True
        except fly_runner.FlyRefusal as exc:
            admitted = False
            reason = str(exc)
        if case % 10 == 9:
            valid = expected = False
        assert admitted == (valid and expected), (case, applied, admitted, valid, expected)
        if admitted:
            outcomes["admitted"] += 1
            after = json.loads(path.read_text())
            assert _oracle(after, config, 0)[0], (case, applied)
            assert after["in_flight"]["new"]["max_cost_usd"] == 600 * config.rate_per_second_usd
        else:
            outcomes["refused-invalid" if "needs operator repair" in reason else "refused-cap"] += 1
            assert path.read_text() == text, (case, applied)  # never rewritten
    # Every outcome is exercised, many times.
    assert min(outcomes.values()) >= 20, outcomes



# ── An existing ledger file is never "no ledger" (round 14) ─────────────────

_WHOLE_DOCUMENTS = {
    "null": "null", "list": "[]", "list-of-ledger": "[{}]", "string": '"ledger"',
    "number": "0", "true": "true", "false": "false", "empty-object": "{}",
    "empty-file": "", "whitespace": "  \n\t ", "bom-prefixed": "\ufeff{}",
    "truncated": '{"month": "2027-01", "day": "2027-01-15", "spend_usd": 1.',
}


@pytest.mark.parametrize("name", sorted(_WHOLE_DOCUMENTS))
def test_an_existing_ledger_that_is_not_a_ledger_document_is_refused(fly_env, tmp_path, name):
    config = fly_runner.FlyConfig.from_env()
    path = tmp_path / "l.json"
    raw = _WHOLE_DOCUMENTS[name].encode("utf-8")
    path.write_bytes(raw)
    ledger = fly_runner.FlyLedger(path, clock=lambda: _NOW)
    with pytest.raises(fly_runner.FlyRefusal, match="needs operator repair"):
        ledger.reserve(config, run_id="new", owner="t", max_run_seconds=600, now=_NOW)
    for read in (ledger.snapshot, lambda: ledger.release("r1"), ledger.count_local_fallback):
        try:
            read()
        except fly_runner.FlyLedgerError:
            pass
    assert path.read_bytes() == raw  # byte-identical: never initialised over


def test_only_an_absent_ledger_file_initialises(fly_env, tmp_path):
    config = fly_runner.FlyConfig.from_env()
    path = tmp_path / "l.json"
    ledger = fly_runner.FlyLedger(path, clock=lambda: _NOW)
    ledger.reserve(config, run_id="new", owner="t", max_run_seconds=600, now=_NOW)
    data = json.loads(path.read_text())
    assert set(data["in_flight"]) == {"new"} and data["launches_today"] == 1


def test_the_ledger_validator_rejects_every_non_object_document():
    for value in (None, [], [{}], "x", 0, 1.5, True, False):
        with pytest.raises(fly_runner.FlyInvalid):
            fly_runner.validate("ledger", value)


# ── Termination does not wait for the ledger (round 14) ─────────────────────


def _break_ledger_while_running(executor, contents=b"null"):
    """The ledger becomes unusable while the machine is still running, and
    the connection to the run is lost: the machine is still up when the run
    is settled."""
    async def wait(machine_id, **k):
        executor.ledger.path.write_bytes(contents)
        raise httpx.ConnectError("injected disconnect")

    executor.client.wait = wait


def test_a_per_job_machine_is_destroyed_while_the_ledger_is_invalid(fly_env):
    fake = FakeFly()
    executor, _, _ = make_executor(fake, record=None)
    _break_ledger_while_running(executor)
    job = _job()
    with contextlib.suppress(Exception):
        asyncio.run(executor.run(job))
    assert job.machine_id
    (delete,) = fake.calls("DELETE")
    assert delete.url.path.endswith(f"/machines/{job.machine_id}")
    assert job.machine_id not in fake.machines
    assert executor.ledger.path.read_bytes() == b"null"  # nothing rewritten


def test_a_login_machine_is_stopped_while_the_ledger_is_invalid(fly_env, monkeypatch):
    fake = _codex_fake(monkeypatch)
    executor, _, _ = make_executor(fake, record=None)
    _break_ledger_while_running(executor, b"[]")
    job = _codex_job()
    with contextlib.suppress(Exception):
        asyncio.run(executor.run(job))
    stops = fake.calls("POST", "/stop")
    assert stops and all(r.headers.get("fly-machine-lease-nonce") for r in stops)
    assert fake.machines["m-codex"]["state"] == "stopped"
    assert not fake.calls("DELETE")  # a login machine is never destroyed
    assert executor.ledger.path.read_bytes() == b"[]"


def test_the_reaper_stops_only_machines_this_process_launched_while_invalid(fly_env):
    fake = FakeFly()
    fake.sticky_all = True  # the run's own destroy is not confirmed
    executor, _, _ = make_executor(fake, record=None)
    executor.client.wait = _async(_disconnect)
    job = _job()
    with contextlib.suppress(Exception):
        asyncio.run(executor.run(job))
    assert job.machine_id in fake.machines
    fake.sticky_all = False
    fake.machines["m-other"] = {"id": "m-other", "state": "started"}  # not ours
    executor.ledger.path.write_bytes(b"{}")
    deletes_before = len(fake.calls("DELETE"))
    with pytest.raises(fly_runner.FlyLedgerError):
        asyncio.run(executor.reap())
    new = fake.calls("DELETE")[deletes_before:]
    assert {r.url.path.rsplit("/", 1)[-1] for r in new} == {job.machine_id}
    assert job.machine_id not in fake.machines and "m-other" in fake.machines
    assert executor.ledger.path.read_bytes() == b"{}"



# ── Login machines settle after a config update (live pilot finding) ───────


@pytest.fixture
def fast_settle(monkeypatch):
    monkeypatch.setattr(fly_runner, "LOGIN_SETTLE_POLL_SECONDS", 0.0, raising=False)


@pytest.mark.parametrize("wait_settles", [True, False], ids=["wait", "poll"])
def test_a_login_claim_waits_for_the_machine_to_settle_after_its_update(
        fly_env, monkeypatch, fast_settle, wait_settles):  # fmt: skip
    fake = _codex_fake(monkeypatch)
    fake.settle_reads = 2  # "created" for two reads after every update
    fake.wait_settles = wait_settles
    executor, _, _ = make_executor(fake, record={"exit_code": 0, "stdout": "ok"})
    job = _codex_job()
    rc, out, _ = asyncio.run(executor.run(job))
    assert rc == 0 and b"ok" in out
    assert len(fake.calls("POST", "/start")) == 1
    settles = [r for r in fake.calls("GET", "/wait") if r.url.params.get("state") == "stopped"
               and r.url.params.get("instance_id")]  # fmt: skip
    assert settles  # waited on the new version, not read once and refused
    cfg = fake.machines["m-codex"]["config"]
    assert not fly_runner._has_job_config(cfg)  # the clear after the run settled too
    assert executor.ledger.snapshot()["in_flight"] == {}


def test_a_login_machine_that_never_settles_fails_closed_and_keeps_no_job_files(
        fly_env, monkeypatch, fast_settle):  # fmt: skip
    fake = _codex_fake(monkeypatch)
    fake.never_settles = True
    executor, _, revoked = make_executor(fake, record=None)
    job = _codex_job()
    with pytest.raises(fly_runner.FlyLaunchError, match="did not settle"):
        asyncio.run(executor.run(job))
    assert not fake.calls("POST", "/start")  # nothing ran
    cfg = fake.machines["m-codex"]["config"]  # the stored config, as the fake holds it
    assert cfg["files"] == [] and cfg["env"] == {}
    assert not fly_runner._config_run_id(cfg)
    assert executor.ledger.entry(job.run_id) is None  # cleared, so released
    assert "m-codex" not in fake.leases and revoked == [MINTED]


def test_a_running_login_machine_is_never_rewritten_and_the_reason_is_logged(
        fly_env, monkeypatch, fast_settle, caplog):  # fmt: skip
    fake = _codex_fake(monkeypatch)
    executor, _, _ = make_executor(fake, record=None)
    fake.machines["m-codex"].update({"state": "started", "config": {
        "files": [{"guest_path": "/run/job/job.json", "raw_value": "e30="}],
        "env": {"ATELES_RUN_ID": "fly-r"}, "metadata": {"ateles_run_id": "fly-r"}}})
    with caplog.at_level("WARNING", logger="apis.fly_runner"):
        ok = asyncio.run(executor._scrub_login_machine("m-codex", "fly-r"))
    assert ok is False and not fake.calls("POST", "/machines/m-codex")
    assert "this run's job files were not cleared" in caplog.text


def _seed_abandoned_login_run(executor, fake, clock, *, files: bool) -> str:
    """The live pilot's leftover: an unconfirmed reservation for the login
    machine, owned by a dispatcher that has exited, whose lease expired."""
    run_id = "fly-abandoned-login"
    dead = {**fly_runner.this_process_owner("apis-exited-dispatcher"), "pid": _dead_pid(),
            "heartbeat": clock.now - 10_000}  # fmt: skip
    rate = executor.config.rate_per_second_usd
    data = executor.ledger._load()
    data["in_flight"][run_id] = {
        "run_id": run_id, "owner": dead["id"], "machine_id": "m-codex", "login": True,
        "started_at": clock.now - 5_000, "deadline": clock.now - 3_080,
        "max_cost_usd": 1_920 * rate, "unconfirmed": True,
        "unconfirmed_reason": "job config not cleared",
        "lease_nonce": "expired-lease-nonce-secret", "lease_expires": clock.now - 2_700,
    }  # fmt: skip
    data["owners"][dead["id"]] = dead
    executor.ledger._save(_admitted(data))
    fake.leases.pop("m-codex", None)  # the run's lease has expired
    if files:
        fake.machines["m-codex"]["config"] = {
            "files": [{"guest_path": "/run/job/job.json", "raw_value": "e30="}],
            "env": {"ATELES_RUN_ID": run_id}, "metadata": {"ateles_run_id": run_id},
        }  # fmt: skip
    return run_id


@pytest.mark.parametrize("files", [True, False], ids=["job-files-left", "already-clear"])
def test_an_abandoned_login_reservation_heals_without_operator_action(
        fly_env, monkeypatch, fast_settle, files):  # fmt: skip
    fake = _codex_fake(monkeypatch)
    fake.reject_stale_nonce = True  # an expired lease's nonce is refused
    fake.settle_reads = 1
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock, record=None)
    run_id = _seed_abandoned_login_run(executor, fake, clock, files=files)
    assert run_id in asyncio.run(executor.reap())
    assert not fly_runner._has_job_config(fake.machines["m-codex"]["config"])
    assert executor.ledger.entry(run_id) is None  # settled
    assert "m-codex" not in fake.leases  # any cleanup lease was released


def test_an_abandoned_login_reservation_heals_on_the_next_launch(fly_env, monkeypatch,
                                                                 fast_settle):  # fmt: skip
    fake = _codex_fake(monkeypatch)
    fake.reject_stale_nonce = True
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock,
                                   record={"exit_code": 0, "stdout": "ok"})  # fmt: skip
    run_id = _seed_abandoned_login_run(executor, fake, clock, files=True)
    rc, _, _ = asyncio.run(executor.run(_codex_job()))
    assert rc == 0 and executor.ledger.snapshot(clock.now)["in_flight"] == {}
    assert run_id not in executor.ledger.snapshot(clock.now)["in_flight"]


def test_a_login_machine_leased_by_another_dispatcher_is_left_alone(fly_env, monkeypatch,
                                                                    fast_settle):  # fmt: skip
    fake = _codex_fake(monkeypatch)
    clock = Clock()
    executor, _, _ = make_executor(fake, clock=clock, record=None)
    run_id = _seed_abandoned_login_run(executor, fake, clock, files=True)
    fake.leases["m-codex"] = {"nonce": "theirs", "description": "another dispatcher",
                              "expires_at": 4_000_000_000}  # fmt: skip
    before = copy.deepcopy(fake.machines["m-codex"]["config"])
    assert run_id not in asyncio.run(executor.reap())
    assert fake.machines["m-codex"]["config"] == before
    assert executor.ledger.entry(run_id)["unconfirmed"] is True



# ── Large prompts never become one argument on Fly ──────────────────────────


def _cursor_job(**kw):
    return fly_runner.build_job(
        provider="cursor", role="lanius", cmd=kw.pop("cmd"), stdin=kw.pop("stdin", b""),
        timeout=60, repository="owner/repo", github_requested=False, mcp_config_path=None,
        **kw,
    )  # fmt: skip


def test_a_cursor_job_carries_its_prompt_on_stdin(fly_env, monkeypatch):
    monkeypatch.setenv("APIS_FLY_CURSOR_MACHINE_ID", "m-cursor")
    prompt = "p" * 250_000
    cmd, stdin = skill_runner._provider_command("cursor", "/x/cursor-agent", prompt, "WORK",
                                               cwd=None)  # fmt: skip
    job = _cursor_job(cmd=cmd, stdin=stdin)
    assert all(len(a) < 1_000 for a in job.argv)
    files = {f["guest_path"]: base64.b64decode(f["raw_value"])
             for f in fly_runner.job_files(job, fly_runner.FlyConfig.from_env())}  # fmt: skip
    assert files["/run/job/stdin"].decode().startswith(prompt)
    assert json.loads(files["/run/job/argv.json"]) == job.argv


def test_a_claude_job_ships_its_system_prompt_as_a_file(fly_env):
    system = "s" * 250_000
    cmd, stdin = skill_runner._provider_command("claude", "/x/claude", system, "WORK",
                                               cwd=None)  # fmt: skip
    job = fly_runner.build_job(
        provider="claude", role="lanius", cmd=cmd, stdin=b"WORK", timeout=60,
        repository="owner/repo", github_requested=False, mcp_config_path=None,
    )  # fmt: skip
    assert "--append-system-prompt" not in job.argv
    i = job.argv.index("--append-system-prompt-file")
    assert job.argv[i + 1] == fly_runner.SYSTEM_PROMPT_PLACEHOLDER
    assert all(len(a) < 1_000 for a in job.argv)
    files = {f["guest_path"]: base64.b64decode(f["raw_value"])
             for f in fly_runner.job_files(job, fly_runner.FlyConfig.from_env())}  # fmt: skip
    assert files["/run/job/system_prompt.md"].decode() == system


@pytest.mark.parametrize("size", [100_001, 140_000])
def test_an_oversized_argument_is_refused_before_launch(fly_env, monkeypatch, size):
    monkeypatch.setenv("APIS_FLY_CURSOR_MACHINE_ID", "m-cursor")
    cmd = ["/x/cursor-agent", "--print", "q" * size]
    with pytest.raises(fly_runner.FlyConfigInvalid, match=f"cursor command has an argument "
                                                          f"of {size} bytes"):  # fmt: skip
        _cursor_job(cmd=cmd)


def test_an_argument_at_the_bound_is_accepted(fly_env, monkeypatch):
    monkeypatch.setenv("APIS_FLY_CURSOR_MACHINE_ID", "m-cursor")
    job = _cursor_job(cmd=["/x/cursor-agent", "--print", "q" * 100_000])
    assert len(job.argv[-1]) == 100_000


def test_an_oversized_argument_never_starts_a_machine_or_falls_back(fly_env, seam, monkeypatch):
    spawned, fake_exec = seam
    fake = FakeFly()
    executor, minted, _ = make_executor(fake)
    monkeypatch.setattr(fly_runner, "get_executor", lambda: executor)
    monkeypatch.setattr(fly_runner, "MAX_ARGV_ELEMENT_BYTES", 10)  # any real argument exceeds it
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = _once(runner="fly", repository="owner/repo", include_github_contract=True)
    assert not result.ok and "nothing was launched" in result.error
    assert fake.requests == [] and minted == [] and spawned == []
