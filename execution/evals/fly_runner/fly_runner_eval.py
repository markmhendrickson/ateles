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

import asyncio
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
        self.deleted: list[str] = []
        self.sticky: set[str] = set()  # delete acknowledged, machine keeps running

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        rest = request.url.path.split("/apps/eval-runner-app", 1)[1]
        if request.method == "POST" and rest == "/machines":
            if self.create == "refused":
                return httpx.Response(422, json={"error": "eval: invalid machine config"})
            mid = f"m{len(self.created) + 1}"
            self.created.append(mid)
            self.machines[mid] = {"id": mid, "state": "started",
                                  "config": json.loads(request.content)["config"]}  # fmt: skip
            if self.sticky_all:
                self.sticky.add(mid)
            if self.create == "lost":
                return httpx.Response(500, json={"error": self.lost_error})
            return httpx.Response(201, json={"id": mid, "instance_id": f"i-{mid}"})
        if request.method == "GET" and rest == "/machines":
            return httpx.Response(200, json=list(self.machines.values()))
        parts = rest.split("/")
        mid = parts[2]
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
                return httpx.Response(200, json=self.machines[mid])
            return httpx.Response(404, json={"error": "not found"})
        if request.method == "POST" and len(parts) == 3:  # update a login machine
            body = json.loads(request.content)
            clearing = body["config"].get("files") == []
            if not (clearing and self.scrub_ignored):
                self.machines.setdefault(mid, {"id": mid, "login": True, "state": "stopped"})[
                    "config"
                ] = body["config"]
            return httpx.Response(200, json={"id": mid, "instance_id": f"i-{mid}"})
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
    agent_def = AgentDefinition(entity_id="eval", name=ROLE, prompt_markdown="You are the eval role.",
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
    if scenario["fly"].get("lost_error"):
        fake.lost_error = scenario["fly"]["lost_error"]
        fake.start_lost = True
    fake.machines["m-codex-login"] = {"id": "m-codex-login", "state": "stopped", "login": True,
                                      "config": {"mounts": []}}  # fmt: skip
    config = fly_runner.FlyConfig.from_env()
    minted: list[str] = []
    revoked: list[str] = []

    def recording_mint(repo, prefix):
        minted.append(repo)
        return MINTED

    async def read_result(run_id):
        record = scenario["fly"]["record"]
        if record is None:
            return None
        return {"run_id": run_id, **record,
                "stdout": str(record.get("stdout", "")).replace("{run_id}", run_id)}  # fmt: skip

    clock = AdvancingClock()
    client = fly_runner.FlyMachinesClient(config, transport=httpx.MockTransport(fake.handler))
    real_wait = client.wait

    async def wait_and_advance(*a, **k):
        clock.now += 60
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
        data = executor.ledger._load()
        executor.ledger._roll(data, clock.now)
        data["in_flight"][f"other-{mid}"] = {
            "run_id": f"other-{mid}", "owner": owner["id"], "machine_id": mid, "login": False,
            "started_at": clock.now, "deadline": clock.now + 1920,
            "max_cost_usd": 1920 * config.rate_per_second_usd,
        }  # fmt: skip
        data.setdefault("owners", {})[owner["id"]] = owner
        executor.ledger._save(data)
        fake.machines[mid] = {"id": mid, "state": "started"}
        seed_keys.append(f"other-{mid}")
    # Machines running that no reservation accounts for (a lost ledger).
    for ghost in scenario.get("untracked", []):
        fake.machines[ghost["machine"]] = {"id": ghost["machine"], "state": "started"}
        if ghost.get("delete_ineffective"):
            fake.sticky.add(ghost["machine"])
        seed_keys.append(f"untracked-{ghost['machine']}")
    # The reaper runs on a launch only when one is due; recovery scenarios are.
    executor._last_reap = float("-inf") if seed_keys else float("inf")
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
        in_flight_after=len(executor.ledger.snapshot(clock.now)["in_flight"]),
        spend_usd=float(executor.ledger.snapshot(clock.now)["spend_usd"]),
        seeded_cost_usd=seeded_cost,
        seed_entries_left=[k for k in seed_keys if executor.ledger.entry(k) is not None],
        login_state=str(fake.machines.get("m-codex-login", {}).get("state", "")),
        login_lock_held=fly_runner._login_lock("codex").locked(),
        envelopes=envelopes,
        exit_codes=exit_codes,
        starts=list(fake.starts),
        everything_sent=sent,
    )


def judge(scenario: dict, trace: Trace) -> list[str]:
    """Every way *trace* departs from the scenario's expectations."""
    e = scenario["expect"]
    problems: list[str] = []
    env = trace.envelope
    if trace.exit_code != e["exit"]:
        problems.append(f"exit {trace.exit_code} != {e['exit']}")
    if bool(env.get("ok")) != e["ok"]:
        problems.append(f"envelope ok={env.get('ok')} != {e['ok']}")
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
