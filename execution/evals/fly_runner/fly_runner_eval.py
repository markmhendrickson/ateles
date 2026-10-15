"""Fly runner eval harness (ateles#1456): drive the agent-facing dispatch
surface and judge what it did.

Each scenario in ``fixtures/scenarios.json`` runs the REAL ``dispatch_role``
CLI (``main([...])`` with ``--json``), which goes through the real
``skill_runner.run_skill`` routing and the real ``fly_runner`` eligibility,
caps and lifecycle. The stand-ins are only at the edges this process cannot
reach offline:

* the Fly Machines API: an ``httpx.MockTransport`` with machine state;
* GitHub App mint and revoke: a recorder, or the REAL mint with the App left
  unconfigured (the "never falls back to a PAT" scenario);
* the Neotoma result read: the scenario's ``record``;
* the local provider CLI: a stand-in executable that leaves a marker file.

Every credential is a synthetic value. The trace records only what an
operator or caller could observe: exit code, the JSON envelope, whether the
local CLI ran, the Fly requests made, and the tokens minted and revoked.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import socket
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
_APIS = HERE.parents[1] / "daemons" / "apis"
_REPO = HERE.parents[2]
for _path in (str(_REPO), str(_APIS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import dispatch_role  # noqa: E402
import fly_runner  # noqa: E402
import harness_router  # noqa: E402
import skill_runner  # noqa: E402
from lib.daemon_runtime import AgentDefinition  # noqa: E402

SHARED_BEARER = "eval-shared-daemon-bearer-0001"
SHARED_PAT = "eval-shared-ateles-pat-0002"
FLY_TOKEN = "eval-fly-deploy-token-0003"
MINTED = "eval-minted-installation-token-0004"
CLAUDE_OAUTH = "eval-claude-oauth-0005"
LOCAL_SCOPED_GH = "eval-local-scoped-gh-0006"
ROLE = "lanius"


def _present(machine: dict) -> dict:
    """A machine as the Machines API answers for it: always with a version
    and a config object (the double fills them in) unless a test marks the
    machine ``_raw`` to serve an incomplete answer on purpose."""
    out = {k: v for k, v in machine.items() if k != "_raw"}
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


def load_scenarios() -> list[dict]:
    return json.loads((HERE / "fixtures" / "scenarios.json").read_text())["scenarios"]


class FakeFly:
    def __init__(self, wait_mode: str, *, start_lost: bool = False, create: str = "ok"):
        self.wait_mode = wait_mode
        self.start_lost = start_lost
        self.lost_error = "eval: response lost"
        self.scrub_ignored = False  # a config clear is acknowledged but not applied
        self.starts: list[str] = []  # the run id each start found in the config
        # "ok" | "lost" (created, response 500) | "refused" (422, nothing created)
        self.create = create
        self.sticky_all = False
        self.requests: list[httpx.Request] = []
        self.machines: dict[str, dict] = {}
        self.created: list[str] = []
        self.created_configs: list[dict] = []  # every per-job machine's config, as sent
        self.deleted: list[str] = []
        self.sticky: set[str] = set()  # delete acknowledged, machine keeps running
        self.leases: dict[str, dict] = {}
        self.lease_log: list[tuple[str, str]] = []
        self.lease_mode = "ok"
        self.release_fails = False
        self.corrupt_after_scrub = ""  # "absent"|"null": the config read after a clean-up
        self.scrubbed: dict[str, bool] = {}
        self.lease_view_override = None
        # After a config update the login machine reports "created" for this
        # many reads before "stopped" again; or never settles at all.
        self.settle_reads = 0
        self.never_settles = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        rest = request.url.path.split("/apps/eval-runner-app", 1)[1]
        if request.method == "POST" and rest == "/machines":
            if self.create == "refused":
                return httpx.Response(422, json={"error": "eval: invalid machine config"})
            mid = f"m{len(self.created) + 1}"
            self.created.append(mid)
            self.created_configs.append(json.loads(request.content)["config"])
            self.machines[mid] = {"id": mid, "state": "started",
                                  "config": json.loads(request.content)["config"]}  # fmt: skip
            if self.sticky_all:
                self.sticky.add(mid)
            if self.create == "lost":
                return httpx.Response(500, json={"error": self.lost_error})
            return httpx.Response(201, json={"id": mid, "instance_id": f"i-{mid}"})
        if request.method == "GET" and rest == "/machines":
            return httpx.Response(200, json=[_present(m) for m in self.machines.values()])
        parts = rest.split("/")
        mid = parts[2]
        method = request.method
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
                return httpx.Response(200, json={"status": "success", "data": {
                    "nonce": nonce, "expires_at": 4_000_000_000,
                    "description": body.get("description", "")}})  # fmt: skip
            if method == "GET":
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
        settling = mid in self.machines and "_settle" in self.machines[mid]
        if request.method == "GET" and parts[3:] == ["wait"] and settling:
            if self.never_settles:
                return httpx.Response(408, json={"error": "deadline exceeded"})
            self.machines[mid]["state"] = "stopped"
            del self.machines[mid]["_settle"]
            return httpx.Response(200, json={"ok": True})
        if request.method == "GET" and len(parts) == 3 and settling and not self.never_settles:
            if self.machines[mid]["_settle"] <= 0:
                self.machines[mid]["state"] = "stopped"
                del self.machines[mid]["_settle"]
            else:
                self.machines[mid]["_settle"] -= 1
        if request.method == "GET" and parts[3:] == ["wait"]:
            if self.machines.get(mid, {}).get("login"):
                # The runner exits when its one job is done; the machine stops.
                self.machines[mid]["state"] = "stopped"
                return httpx.Response(200, json={"state": "stopped"})
            if self.wait_mode == "disconnect":
                raise httpx.ConnectError("eval: connection dropped while waiting")
            if self.wait_mode == "timeout":
                return httpx.Response(408, json={"error": "deadline exceeded"})
            if mid not in self.sticky:
                self.machines.pop(mid, None)  # auto_destroy after a clean exit
            return httpx.Response(200, json={"ok": True})
        if request.method == "GET":
            if mid in self.machines:
                answer = _present(self.machines[mid])
                if self.corrupt_after_scrub and self.scrubbed.get(mid):
                    answer = _corrupt(answer, "config", self.corrupt_after_scrub)
                return httpx.Response(200, json=answer)
            return httpx.Response(404, json={"error": "not found"})
        if request.method == "POST" and len(parts) == 3:  # update a login machine
            body = json.loads(request.content)
            machine = self.machines.setdefault(mid, {"id": mid, "login": True, "state": "stopped"})
            current = machine.get("instance_id")
            if "current_version" in body and current and body["current_version"] != current:
                return httpx.Response(412, json={"error": "machine version changed"})
            clearing = body["config"].get("files") == []
            if clearing:
                self.scrubbed[mid] = True
            if not (clearing and self.scrub_ignored):
                machine["config"] = body["config"]
                if current:
                    machine["instance_id"] = f"{current}+"
            if self.settle_reads or self.never_settles:
                machine["state"] = "created"  # transitional, right after the update
                machine["_settle"] = self.settle_reads
            return httpx.Response(200, json={"id": mid, "instance_id": machine.get("instance_id")
                                             or f"i-{mid}"})  # fmt: skip
        if request.method == "POST" and parts[3:] == ["start"]:
            self.machines[mid]["state"] = "started"
            cfg = self.machines[mid].get("config") or {}
            self.starts.append(str((cfg.get("env") or {}).get("ATELES_RUN_ID") or ""))
            if self.start_lost:
                return httpx.Response(500, json={"error": self.lost_error})
            return httpx.Response(200, json={"ok": True})
        if request.method == "POST" and parts[3:] == ["stop"]:
            if mid in self.machines:
                self.machines[mid]["state"] = "stopped"
            return httpx.Response(200, json={"ok": True})
        if request.method == "DELETE":
            self.deleted.append(mid)
            if mid not in self.sticky:
                self.machines.pop(mid, None)
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(500, json={"error": "eval: unhandled"})


class AdvancingClock:
    """Wall clock for the executor that jumps a minute on every wait call, so
    a timeout scenario reaches its deadline without sleeping."""

    def __init__(self, start: float = 1_900_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


@dataclass
class Trace:
    exit_code: int
    envelope: dict
    stdout_text: str
    stderr_text: str
    local_cli_ran: bool
    fly_requests: int
    machines_created: int
    machines_remaining: list[str]
    minted: list[str]
    revoked: list[str]
    in_flight_after: int
    spend_usd: float = 0.0
    seeded_cost_usd: float = 0.0
    seed_entries_left: list[str] = field(default_factory=list)
    login_state: str = ""
    login_lock_held: bool = False
    login_config_run_id: str = ""
    login_job_files_after: bool = False
    argv_max_bytes: int = 0  # the longest argument any machine was given
    system_prompt_file_bytes: int = 0
    login_stop_requests: int = 0
    login_mutations: int = 0
    ledger_after: object = None
    ledger_text_after: object = None
    login_lease_holder: str = ""
    lease_released: bool = False
    envelopes: list = field(default_factory=list)
    exit_codes: list = field(default_factory=list)
    starts: list = field(default_factory=list)
    everything_sent: str = field(repr=False, default="")


def _stand_in_cli(tmp_path: Path) -> tuple[str, Path]:
    marker = tmp_path / "local-cli-ran"
    cli = tmp_path / "claude-stand-in"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text('ran')\n"
        "sys.stdin.read()\n"
        "print('LOCAL GATE_INHERITANCE: clear')\n"
    )
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    return str(cli), marker


def _decoded(request: httpx.Request) -> str:
    text = (request.content or b"").decode(errors="replace")
    try:
        body = json.loads(request.content or b"{}")
        for f in (body.get("config") or {}).get("files") or []:
            text += base64.b64decode(f["raw_value"]).decode(errors="replace")
    except (ValueError, AttributeError):
        pass
    return text


def run_scenario(scenario: dict, tmp_path: Path, monkeypatch) -> Trace:
    if scenario.get("kind") == "entrypoint":
        # The in-machine half: the real run_job entrypoint, as a subprocess.
        import entrypoint_probe

        return entrypoint_probe.run_entrypoint(
            scenario["mode"], tmp_path / "entrypoint", budget=float(scenario["budget_seconds"]))
    # ── Isolation: nothing reaches the operator's real state or the network ──
    for name in ("APIS_FLY_RUNNER_CLASSES", "LANIUS_NEOTOMA_TOKEN",
                 "ATELES_REVIEWER_APP_ID", "ATELES_REVIEWER_APP_PRIVATE_KEY",
                 "ATELES_REVIEWER_APP_PRIVATE_KEY_PATH", "ATELES_REVIEWER_APP_INSTALLATION_ID",
                 "APIS_HARNESS_HEADROOM"):  # fmt: skip
        monkeypatch.delenv(name, raising=False)
    settings = {
        "APIS_HARNESS_PROVIDERS": "claude",
        "APIS_HARNESS_HEADROOM_FILE": str(tmp_path / "no-headroom.json"),
        "APIS_HARNESS_USAGE_FILE": str(tmp_path / "usage.json"),
        "APIS_USAGE_GATE": "off",
        "APIS_USAGE_PROBE": "off",
        "APIS_TIER_LEDGER_FILE": str(tmp_path / "tier-ledger.jsonl"),
        "APIS_ACTION_POLICY_FILE": str(tmp_path / "policy.json"),
        "APIS_VENDOR_BINDING_FILE": str(tmp_path / "binding.json"),
        "APIS_FLY_LEDGER_FILE": str(tmp_path / "fly-ledger.json"),
        "APIS_FLY_APP": "eval-runner-app",
        "APIS_FLY_API_TOKEN": FLY_TOKEN,
        "APIS_FLY_IMAGE": "registry.fly.io/eval-runner-app:eval",
        "APIS_FLY_REGION": "ams",
        "NEOTOMA_BASE_URL": "https://neotoma.eval.invalid",
        "NEOTOMA_BEARER_TOKEN": SHARED_BEARER,
        "ATELES_AGENT_PAT": SHARED_PAT,
        "GITHUB_TOKEN": SHARED_PAT,
        "CLAUDE_CODE_OAUTH_TOKEN": CLAUDE_OAUTH,
        "EVAL_LOCAL_SCOPED_GH": LOCAL_SCOPED_GH,
        "APIS_FLY_CODEX_MACHINE_ID": "m-codex-login",
        **scenario.get("settings", {}),
    }
    if scenario.get("provider") in ("codex", "router"):
        settings["APIS_HARNESS_PROVIDERS"] = "claude,codex"
        settings["APIS_HARNESS_HEADROOM"] = '{"claude": 1.0, "codex": 1.0}'
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    if scenario["identity"] == "key":
        keys = tmp_path / "keys"
        keys.mkdir()
        (keys / f"{ROLE}.jwk.json").write_text(json.dumps({
            "kty": "EC", "crv": "P-256", "x": "eval-x", "y": "eval-y",
            "d": "eval-private-scalar", "sub": f"{ROLE}@ateles-swarm",
        }))  # fmt: skip
        monkeypatch.setenv("ATELES_PRIVATE_KEYS_DIR", str(keys))
    else:
        monkeypatch.delenv("ATELES_PRIVATE_KEYS_DIR", raising=False)

    skills = tmp_path / ".claude" / "skills" / ROLE
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(f"# {ROLE}\n")
    cli, marker = _stand_in_cli(tmp_path)
    role_prompt = "You are the eval role."
    if scenario.get("agent_prompt_bytes"):
        # A large agent definition, as real ones with their rule index are.
        role_prompt += "\n" + "Follow the rule. " * (int(scenario["agent_prompt_bytes"]) // 17)
    agent_def = AgentDefinition(entity_id="eval", name=ROLE, prompt_markdown=role_prompt,
                                tool_allowlist="*", aauth_sub=f"{ROLE}@ateles-swarm")  # fmt: skip
    monkeypatch.setattr(dispatch_role, "ATELES_REPO", tmp_path)
    monkeypatch.setattr(skill_runner, "ATELES_REPO", tmp_path)
    monkeypatch.setattr(skill_runner, "CLAUDE_BIN", cli)
    monkeypatch.setattr(skill_runner, "CODEX_BIN", cli)
    monkeypatch.setattr(skill_runner, "_load_agent_def", lambda role: agent_def)
    monkeypatch.setattr(dispatch_role, "_load_agent_def", lambda role: agent_def)
    monkeypatch.setattr(skill_runner, "_load_active_policy_prompt", lambda role: "")
    monkeypatch.setattr(skill_runner, "_write_harness_event", lambda **kw: None)
    monkeypatch.setattr(skill_runner, "DISPATCH_FAILURE_LOG_DIR", tmp_path / "failures")
    harness_router.reset_state()

    # ── The Fly edge ─────────────────────────────────────────────────────────
    fake = FakeFly(scenario["fly"]["wait"], start_lost=scenario["fly"].get("start") == "lost",
                   create=scenario["fly"].get("create", "ok"))  # fmt: skip
    fake.sticky_all = bool(scenario["fly"].get("delete_ineffective"))
    fake.scrub_ignored = bool(scenario["fly"].get("scrub_ignored"))
    fake.corrupt_after_scrub = scenario["fly"].get("corrupt_after_scrub", "")
    fake.release_fails = bool(scenario["fly"].get("release_fails"))
    fake.lease_view_override = scenario["fly"].get("lease_view_override")
    fake.settle_reads = int(scenario["fly"].get("settle_reads", 0))
    fake.never_settles = bool(scenario["fly"].get("never_settles"))
    monkeypatch.setattr(fly_runner, "LOGIN_SETTLE_POLL_SECONDS", 0.0, raising=False)
    if scenario.get("mac_usage") == "over_ceiling":
        # The Mac's own Claude login is far over its weekly ceiling; the gate is on.
        monkeypatch.setenv("APIS_USAGE_GATE", "on")
        monkeypatch.setenv("APIS_USAGE_GATED_PROVIDERS", "claude")
        moment = __import__("time").time()
        harness_router.record_usage("claude", [
            {"name": "weekly_all", "used_percent": 95.0,
             "resets_at": harness_router._iso_from_wall(moment + 3 * 86400)}],
            observed_at=moment)  # fmt: skip
    if scenario["fly"].get("lost_error"):
        fake.lost_error = scenario["fly"]["lost_error"]
        fake.start_lost = True
    fake.machines["m-codex-login"] = {"id": "m-codex-login", "state": "stopped", "login": True,
                                      "instance_id": "v1", "config": {"mounts": []}}  # fmt: skip
    foreign_lease = scenario["fly"].get("login_lease")
    if foreign_lease:
        fake.leases["m-codex-login"] = {"nonce": "foreign-holder-nonce",
                                        "description": foreign_lease, "expires_at": 4_000_000_000}
    if "login_version" in scenario["fly"]:
        if scenario["fly"]["login_version"] is None:
            fake.machines["m-codex-login"].pop("instance_id")
            fake.machines["m-codex-login"]["_raw"] = True
        else:
            fake.machines["m-codex-login"]["instance_id"] = scenario["fly"]["login_version"]
    busy = scenario["fly"].get("login_machine")
    if busy:
        # The machine's own live state, with NO ledger entry behind it.
        rid = busy["config_run_id"]
        fake.machines["m-codex-login"].update({"state": busy["state"], "config": {
            "files": [{"guest_path": "/run/job/job.json", "raw_value": "eA=="}],
            "env": {"ATELES_RUN_ID": rid}, "metadata": {"ateles_run_id": rid},
        }})  # fmt: skip
    config = fly_runner.FlyConfig.from_env()
    minted: list[str] = []
    revoked: list[str] = []

    def recording_mint(repo, prefix):
        minted.append(repo)
        return MINTED

    async def read_result(run_id):
        record = scenario["fly"]["record"]
        if "record_raw" in scenario["fly"]:
            # Exactly what the store holds, however incomplete.
            return {"run_id": run_id, **scenario["fly"]["record_raw"]}
        if record == "malformed":
            # The REAL reader, against a result store that answers nonsense.
            transport = httpx.MockTransport(lambda request: httpx.Response(200, json=[1]))
            return await fly_runner.read_result_record(run_id, transport=transport,
                                                       attempts=1, delay=0)  # fmt: skip
        if record is None:
            return None
        # Every field, as run_job.build_record writes it.
        full = {"exit_code": 0, "stdout": "", "stdout_truncated": False, "stderr_tail": "",
                "wall_ms": 1, "timed_out": False, "refused": "", "probe_ok": True, **record}
        return {"run_id": run_id, **full,
                "stdout": str(full["stdout"]).replace("{run_id}", run_id)}  # fmt: skip

    clock = AdvancingClock()
    client = fly_runner.FlyMachinesClient(config, transport=httpx.MockTransport(fake.handler))
    real_wait = client.wait

    async def wait_and_advance(*a, **k):
        clock.now += 60
        if "ledger_breaks_on_wait" in scenario["fly"]:
            # The ledger becomes unusable while the machine is running.
            executor.ledger.path.write_text(scenario["fly"]["ledger_breaks_on_wait"])
        return await real_wait(*a, **k)

    client.wait = wait_and_advance
    executor = fly_runner.FlyExecutor(
        config,
        client=client,
        mint=recording_mint if scenario["github_app"] == "configured"
        else fly_runner.mint_job_github_token,
        revoke=revoked.append,
        read_result=read_result,
        clock=clock,
    )
    # A dispatcher restart: reservations left by a collector that is gone.
    seeded_cost = 0.0
    seed_keys: list[str] = []
    for seed in scenario.get("seed", []):
        mid = seed["machine"]
        started = clock.now - seed["age"]
        executor.ledger.reserve(config, run_id=f"seed-{mid}", owner=executor.instance_id,
                                max_run_seconds=seed["window"], now=started)  # fmt: skip
        executor.ledger.set_machine(f"seed-{mid}", mid)
        seeded_cost += min(seed["age"], seed["window"]) * config.rate_per_second_usd
        seed_keys.append(f"seed-{mid}")
        if seed["state"] in ("listed", "sticky"):
            fake.machines[mid] = {"id": mid, "state": "started"}
        if seed["state"] == "sticky":
            fake.sticky.add(mid)
    # Another dispatcher (a separate process) holding a reservation of its own.
    others: list = []
    for other in scenario.get("other_dispatcher", []):
        mid = other["machine"]
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        others.append(proc)
        if not other.get("alive", True):
            proc.kill()
            proc.wait()
        owner = {"id": f"apis-other-{mid}", "pid": proc.pid,
                 "start": fly_runner.process_start_token(proc.pid) or "gone",
                 "host": socket.gethostname(), "heartbeat": clock.now}  # fmt: skip
        login = bool(other.get("login"))
        rid = f"other-{mid}-{'live' if other.get('alive', True) else 'dead'}"
        owner["id"] = f"apis-{rid}"
        data = executor.ledger._load()
        executor.ledger._roll(data, clock.now)
        data["in_flight"][rid] = {
            "run_id": rid, "owner": owner["id"], "machine_id": mid, "login": login,
            "started_at": clock.now, "deadline": clock.now + 1920,
            "max_cost_usd": 1920 * config.rate_per_second_usd,
        }  # fmt: skip
        data["owners"][owner["id"]] = owner
        data["launches_today"] += 1  # as that dispatcher's admission counted it
        executor.ledger._save(data)
        if other.get("job_on_machine"):
            # This dispatcher's job is the one running on the machine now.
            fake.machines.setdefault(mid, {"id": mid, "login": login})
            fake.machines[mid].update({"state": "started", "config": {
                "files": [{"guest_path": "/run/job/job.json", "raw_value": "eA=="}],
                "env": {"ATELES_RUN_ID": rid}, "metadata": {"ateles_run_id": rid},
            }})  # fmt: skip
        elif not login:
            fake.machines[mid] = {"id": mid, "state": "started"}
        seed_keys.append(rid)
    # Machines running that no reservation accounts for (a lost ledger).
    for ghost in scenario.get("untracked", []):
        fake.machines[ghost["machine"]] = {"id": ghost["machine"], "state": "started"}
        if ghost.get("delete_ineffective"):
            fake.sticky.add(ghost["machine"])
        seed_keys.append(f"untracked-{ghost['machine']}")
    # The reaper runs on a launch only when one is due; recovery scenarios are.
    executor._last_reap = float("-inf") if seed_keys else float("inf")
    ledger_raw = scenario.get("ledger_raw")
    if ledger_raw is not None:
        # A ledger left on disk as is (e.g. damaged), before the dispatch.
        executor.ledger.path.parent.mkdir(parents=True, exist_ok=True)
        executor.ledger.path.write_text(json.dumps(ledger_raw))
    monkeypatch.setattr(fly_runner, "get_executor", lambda: executor)
    fly_runner._LOGIN_LOCKS.clear()

    # ── Drive the agent-facing CLI ───────────────────────────────────────────
    out, err = io.StringIO(), io.StringIO()
    provider = scenario.get("provider", "claude")
    provider_args = [] if provider == "router" else ["--provider", provider]
    envelopes: list[dict] = []
    exit_codes: list[int] = []
    try:
        for _ in range(int(scenario.get("dispatches", 1))):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                exit_codes.append(dispatch_role.main(
                    ["--role", ROLE, "--task", "Check the PR's gate inheritance.",
                     *provider_args, "--json", *scenario["args"]]
                ))  # fmt: skip
            try:
                envelopes.append(json.loads(out.getvalue()))
            except ValueError:
                envelopes.append({})
    finally:
        for proc in others:
            proc.kill()
            proc.wait()
    exit_code = exit_codes[0]
    stdout_text = out.getvalue()
    envelope = envelopes[0]
    sent = "\n".join(_decoded(r) for r in fake.requests)
    try:
        ledger_view = executor.ledger.snapshot(clock.now)
    except fly_runner.FlyLedgerError:
        ledger_view = None  # a damaged ledger is what the scenario is about
    return Trace(
        exit_code=exit_code,
        envelope=envelope,
        stdout_text=stdout_text,
        stderr_text=err.getvalue(),
        local_cli_ran=marker.exists(),
        fly_requests=len(fake.requests),
        machines_created=len(fake.created),
        machines_remaining=sorted(m for m, d in fake.machines.items() if not d.get("login")),
        minted=minted,
        revoked=revoked,
        in_flight_after=len(ledger_view["in_flight"]) if ledger_view else -1,
        spend_usd=float(ledger_view["spend_usd"]) if ledger_view else -1.0,
        seeded_cost_usd=seeded_cost,
        seed_entries_left=[k for k in seed_keys
                           if ledger_view and k in ledger_view["in_flight"]],
        login_state=str(fake.machines.get("m-codex-login", {}).get("state", "")),
        login_lock_held=fly_runner._login_lock("codex").locked(),
        login_config_run_id=str(((fake.machines.get("m-codex-login", {}).get("config") or {})
                                 .get("metadata") or {}).get("ateles_run_id") or ""),
        login_stop_requests=sum(1 for r in fake.requests if r.method == "POST"
                                and r.url.path.endswith("/m-codex-login/stop")),
        login_mutations=sum(1 for r in fake.requests if r.method in ("POST", "DELETE")
                            and "/machines/m-codex-login" in r.url.path
                            and not r.url.path.endswith("/lease")),
        login_lease_holder=str((fake.leases.get("m-codex-login") or {}).get("description", "")),
        lease_released=("release", "m-codex-login") in fake.lease_log,
        envelopes=envelopes,
        exit_codes=exit_codes,
        starts=list(fake.starts),
        argv_max_bytes=_longest_argument(fake.created_configs),
        system_prompt_file_bytes=_job_file_bytes(fake.created_configs, "system_prompt.md"),
        login_job_files_after=fly_runner._has_job_config(
            fake.machines["m-codex-login"].get("config") or {}),
        everything_sent=sent,
        ledger_after=_ledger_file_json(executor.ledger.path),
        ledger_text_after=(executor.ledger.path.read_text()
                           if executor.ledger.path.exists() else None),
    )


def _job_files(config: dict) -> dict[str, bytes]:
    return {f["guest_path"]: base64.b64decode(f["raw_value"]) for f in config.get("files") or []}


def _longest_argument(configs: list[dict]) -> int:
    """The longest single argument in any machine's argv job file, in bytes."""
    longest = 0
    for config in configs:
        raw = _job_files(config).get("/run/job/argv.json")
        if raw:
            longest = max([longest, *(len(a.encode("utf-8")) for a in json.loads(raw))])
    return longest


def _job_file_bytes(configs: list[dict], name: str) -> int:
    return max([0, *(len(_job_files(c).get(f"/run/job/{name}", b"")) for c in configs)])


def _ledger_file_json(path: Path) -> object:
    """The ledger file as parsed JSON (None when absent or not JSON)."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except ValueError:
        return None


def judge_entrypoint(scenario: dict, trace) -> list[str]:
    """Every way a real entrypoint run departs from its expectations."""
    e = scenario["expect"]
    problems: list[str] = []
    if trace.exit_code != e["exit"]:
        problems.append(f"exit {trace.exit_code} != {e['exit']}")
    if not trace.condition_reached:
        problems.append("the condition under test was never reached")
    if "within_seconds" in e and not trace.elapsed <= e["within_seconds"]:
        problems.append(f"exited after {trace.elapsed:.2f}s, not within {e['within_seconds']}s")
    if "after_condition_within" in e and not (
            trace.exit_after_condition is not None
            and trace.exit_after_condition <= e["after_condition_within"]):  # fmt: skip
        problems.append(
            f"exited {trace.exit_after_condition}s after the condition, not within "
            f"{e['after_condition_within']}s")
    if e.get("cleanup_reached") and not trace.cleanup_reached:
        problems.append("the job's processes were never cleaned up")
    if e.get("descendant_checked"):
        if trace.descendant_alive_before is not True:
            problems.append("the left-behind process was not running before the exit")
        if trace.descendant_gone_after is False:
            problems.append("the left-behind process survived the exit")
    return problems


def judge(scenario: dict, trace: Trace) -> list[str]:
    """Every way *trace* departs from the scenario's expectations."""
    if scenario.get("kind") == "entrypoint":
        return judge_entrypoint(scenario, trace)
    e = scenario["expect"]
    problems: list[str] = []
    env = trace.envelope
    if trace.exit_code != e["exit"]:
        problems.append(f"exit {trace.exit_code} != {e['exit']}")
    if bool(env.get("ok")) != e["ok"]:
        problems.append(f"envelope ok={env.get('ok')} != {e['ok']}")
    if "usage_gate_exempted_contains" in e and (
            e["usage_gate_exempted_contains"] not in str(env.get("usage_gate_exempted", ""))):
        problems.append("the envelope does not say which Mac usage reading was set aside")
    if "stdout_contains" in e and e["stdout_contains"] not in str(env.get("stdout", "")):
        problems.append(f"result stdout lacks {e['stdout_contains']!r}")
    if "reason_contains" in e:
        reason = f"{env.get('reason', '')} {env.get('error', '')} {env.get('stderr', '')}"
        if e["reason_contains"] not in reason:
            problems.append(f"reason lacks {e['reason_contains']!r}: {reason[:300]!r}")
    if trace.local_cli_ran != e["local_cli_ran"]:
        problems.append(f"local CLI ran={trace.local_cli_ran}, expected {e['local_cli_ran']}")
    if "fly_requests" in e and trace.fly_requests != e["fly_requests"]:
        problems.append(f"{trace.fly_requests} Fly requests, expected {e['fly_requests']}")
    if "machines_created" in e and trace.machines_created != e["machines_created"]:
        problems.append(f"{trace.machines_created} machines created, expected {e['machines_created']}")
    if e.get("machine_gone") and trace.machines_remaining:
        problems.append(f"machines left running: {trace.machines_remaining}")
    if "minted" in e and len(trace.minted) != e["minted"]:
        problems.append(f"{len(trace.minted)} tokens minted, expected {e['minted']}")
    if e.get("revoked_all_minted") and trace.revoked != [MINTED] * len(trace.minted):
        problems.append(f"minted {len(trace.minted)} but revoked {len(trace.revoked)}")
    if e.get("revoked_all_minted") and not trace.minted:
        problems.append("expected a minted token to revoke, none was minted")
    if "in_flight_after" in e and trace.in_flight_after != e["in_flight_after"]:
        problems.append(f"{trace.in_flight_after} ledger slots still held")
    if e.get("cooled_until") and not env.get("cooled_until"):
        problems.append("no cooled_until on a deferred dispatch")
    if e.get("cooled_until") is False and env.get("cooled_until"):
        problems.append("a retry time on a refusal that waiting does not fix")
    if "output_truncated" in e:
        flagged = bool(env.get("output_truncated"))
        marked = str(env.get("stdout", "")).startswith("[fly runner: output truncated")
        if flagged != e["output_truncated"] or marked != e["output_truncated"]:
            problems.append(f"output_truncated flag={flagged} marker={marked}, "
                            f"expected {e['output_truncated']}")  # fmt: skip
    if "in_flight_at_least" in e and trace.in_flight_after < e["in_flight_at_least"]:
        problems.append(f"{trace.in_flight_after} ledger slots held, expected a kept slot")
    if e.get("seed_settled") is True:
        if trace.seed_entries_left:
            problems.append(f"seeded reservations not settled: {trace.seed_entries_left}")
        if trace.spend_usd + 1e-12 < trace.seeded_cost_usd:
            problems.append(f"spend ${trace.spend_usd:.6f} < incurred ${trace.seeded_cost_usd:.6f}")
    if e.get("seed_settled") is False:
        if not trace.seed_entries_left:
            problems.append("an unconfirmed seeded machine's reservation was released")
        if trace.spend_usd > 0:
            problems.append("an unconfirmed seeded machine was charged as if settled")
    if "remote_outcome_unknown" in e:
        if bool(env.get("remote_outcome_unknown")) != e["remote_outcome_unknown"]:
            problems.append(f"remote_outcome_unknown={env.get('remote_outcome_unknown')}, "
                            f"expected {e['remote_outcome_unknown']}")  # fmt: skip
        if e["remote_outcome_unknown"] and not env.get("remote_run_id"):
            problems.append("an unknown remote outcome does not name its run")
    if "remote_machine_state" in e and env.get("remote_machine_state") != e["remote_machine_state"]:
        problems.append(f"remote machine state {env.get('remote_machine_state')!r}, "
                        f"expected {e['remote_machine_state']!r}")  # fmt: skip
    if "attempts" in e and len(env.get("attempted_providers") or []) != e["attempts"]:
        problems.append(f"attempted {env.get('attempted_providers')}, expected "
                        f"{e['attempts']} attempt(s)")  # fmt: skip
    if "stderr_contains" in e and e["stderr_contains"] not in trace.stderr_text:
        problems.append(f"stderr lacks {e['stderr_contains']!r}")
    if "error_contains_run_id" in e and env.get("remote_run_id") and (
        env["remote_run_id"] not in str(env.get("error", ""))
    ):
        problems.append("the error does not name the remote run")
    if e.get("stdout_has_own_run_id") and (
        not env.get("remote_run_id") or env["remote_run_id"] not in str(env.get("stdout", ""))
    ):
        problems.append("the result does not belong to its own run")
    if e.get("starts_with_own_config"):
        own = [str(x.get("remote_run_id") or "") for x in trace.envelopes]
        if trace.starts != [r for r in own if r] or len(trace.starts) != len(own):
            problems.append(f"starts found configs {trace.starts}, runs were {own}")
    second = e.get("second")
    if second is not None:
        if len(trace.envelopes) < 2:
            problems.append("the second dispatch did not run")
        else:
            env2, rc2 = trace.envelopes[1], trace.exit_codes[1]
            if rc2 != second["exit"] or bool(env2.get("ok")) != second["ok"]:
                problems.append(f"second dispatch exit={rc2} ok={env2.get('ok')}, "
                                f"expected {second['exit']}/{second['ok']}")  # fmt: skip
            want = second.get("reason_contains")
            got = f"{env2.get('reason', '')} {env2.get('error', '')} {env2.get('stderr', '')}"
            if want and want not in got:
                problems.append(f"second dispatch reason lacks {want!r}: {got[:200]!r}")
            if second.get("stdout_has_own_run_id") and (
                not env2.get("remote_run_id")
                or env2["remote_run_id"] not in str(env2.get("stdout", ""))
            ):
                problems.append("the second dispatch's result is not its own")
        if "starts" in second and len(trace.starts) != second["starts"]:
            problems.append(f"{len(trace.starts)} machine starts, expected {second['starts']}")
    if "login_job_files_after" in e and trace.login_job_files_after != e["login_job_files_after"]:
        problems.append("the login machine's stored config still holds job files"
                        if trace.login_job_files_after else "expected job files on the machine")
    if "argv_max_bytes_below" in e and not trace.argv_max_bytes < e["argv_max_bytes_below"]:
        problems.append(f"a machine was given a {trace.argv_max_bytes}-byte argument")
    if "system_prompt_file_bytes_over" in e and not (
            trace.system_prompt_file_bytes > e["system_prompt_file_bytes_over"]):  # fmt: skip
        problems.append(f"the system prompt file holds {trace.system_prompt_file_bytes} bytes")
    if "login_starts" in e and len(trace.starts) != e["login_starts"]:
        problems.append(f"{len(trace.starts)} login machine starts, expected {e['login_starts']}")
    if "login_config_run_id" in e and trace.login_config_run_id != e["login_config_run_id"]:
        problems.append(f"login machine config belongs to {trace.login_config_run_id!r}, "
                        f"expected {e['login_config_run_id']!r}")  # fmt: skip
    if "login_lease_holder" in e and trace.login_lease_holder != e["login_lease_holder"]:
        problems.append(f"login lease held by {trace.login_lease_holder!r}, "
                        f"expected {e['login_lease_holder']!r}")  # fmt: skip
    if e.get("lease_released") and not trace.lease_released:
        problems.append("the run's lease was never released")
    if "ledger_text_after" in e and trace.ledger_text_after != e["ledger_text_after"]:
        problems.append("the unusable ledger was rewritten")
    if e.get("ledger_untouched") and trace.ledger_after != scenario.get("ledger_raw"):
        problems.append("the damaged ledger was rewritten (reset) instead of refused")
    if "login_mutations" in e and trace.login_mutations != e["login_mutations"]:
        problems.append(f"{trace.login_mutations} write(s) to the login machine")
    if "login_stop_requests" in e and trace.login_stop_requests != e["login_stop_requests"]:
        problems.append(f"{trace.login_stop_requests} stop request(s) on the login machine")
    if e.get("seed_kept") and not trace.seed_entries_left:
        problems.append("another dispatcher's live reservation was released")
    for mid in e.get("machines_kept", []):
        if mid not in trace.machines_remaining:
            problems.append(f"another dispatcher's machine {mid} was terminated")
    if "login_state" in e and trace.login_state != e["login_state"]:
        problems.append(f"login machine is {trace.login_state}, expected {e['login_state']}")
    if e.get("login_lock_free") and trace.login_lock_held:
        problems.append("the login lock is still held")
    # Always: no shared credential ever reaches Fly or the caller's output.
    for label, value in (("shared PAT", SHARED_PAT), ("shared bearer", SHARED_BEARER),
                         ("deploy token", FLY_TOKEN)):  # fmt: skip
        if value in trace.everything_sent:
            problems.append(f"the {label} was sent to Fly")
        if value in trace.stdout_text or value in trace.stderr_text:
            problems.append(f"the {label} appeared in the CLI output")
    if e.get("pat_absent") and SHARED_PAT in json.dumps(trace.envelope):
        problems.append("the shared PAT appeared in the envelope")
    return problems
