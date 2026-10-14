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
import stat
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
    def __init__(self, wait_mode: str):
        self.wait_mode = wait_mode
        self.requests: list[httpx.Request] = []
        self.machines: dict[str, dict] = {}
        self.created: list[str] = []
        self.deleted: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        rest = request.url.path.split("/apps/eval-runner-app", 1)[1]
        if request.method == "POST" and rest == "/machines":
            mid = f"m{len(self.created) + 1}"
            self.created.append(mid)
            self.machines[mid] = {"id": mid, "state": "started"}
            return httpx.Response(201, json={"id": mid, "instance_id": f"i-{mid}"})
        if request.method == "GET" and rest == "/machines":
            return httpx.Response(200, json=list(self.machines.values()))
        parts = rest.split("/")
        mid = parts[2]
        if request.method == "GET" and parts[3:] == ["wait"]:
            if self.wait_mode == "disconnect":
                raise httpx.ConnectError("eval: connection dropped while waiting")
            if self.wait_mode == "timeout":
                return httpx.Response(408, json={"error": "deadline exceeded"})
            self.machines.pop(mid, None)  # auto_destroy after a clean exit
            return httpx.Response(200, json={"ok": True})
        if request.method == "GET":
            if mid in self.machines:
                return httpx.Response(200, json=self.machines[mid])
            return httpx.Response(404, json={"error": "not found"})
        if request.method == "POST" and parts[3:] == ["stop"]:
            if mid in self.machines:
                self.machines[mid]["state"] = "stopped"
            return httpx.Response(200, json={"ok": True})
        if request.method == "DELETE":
            self.deleted.append(mid)
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
        **scenario.get("settings", {}),
    }
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
    monkeypatch.setattr(skill_runner, "_load_agent_def", lambda role: agent_def)
    monkeypatch.setattr(dispatch_role, "_load_agent_def", lambda role: agent_def)
    monkeypatch.setattr(skill_runner, "_load_active_policy_prompt", lambda role: "")
    monkeypatch.setattr(skill_runner, "_write_harness_event", lambda **kw: None)
    monkeypatch.setattr(skill_runner, "DISPATCH_FAILURE_LOG_DIR", tmp_path / "failures")
    harness_router.reset_state()

    # ── The Fly edge ─────────────────────────────────────────────────────────
    fake = FakeFly(scenario["fly"]["wait"])
    config = fly_runner.FlyConfig.from_env()
    minted: list[str] = []
    revoked: list[str] = []

    def recording_mint(repo, prefix):
        minted.append(repo)
        return MINTED

    async def read_result(run_id):
        record = scenario["fly"]["record"]
        return None if record is None else {"run_id": run_id, **record}

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
    executor._last_reap = float("inf")
    monkeypatch.setattr(fly_runner, "get_executor", lambda: executor)
    fly_runner._LOGIN_LOCKS.clear()

    # ── Drive the agent-facing CLI ───────────────────────────────────────────
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        exit_code = dispatch_role.main(
            ["--role", ROLE, "--task", "Check the PR's gate inheritance.", "--provider",
             "claude", "--json", *scenario["args"]]
        )  # fmt: skip
    stdout_text = out.getvalue()
    try:
        envelope = json.loads(stdout_text)
    except ValueError:
        envelope = {}
    sent = "\n".join(_decoded(r) for r in fake.requests)
    return Trace(
        exit_code=exit_code,
        envelope=envelope,
        stdout_text=stdout_text,
        stderr_text=err.getvalue(),
        local_cli_ran=marker.exists(),
        fly_requests=len(fake.requests),
        machines_created=len(fake.created),
        machines_remaining=sorted(fake.machines),
        minted=minted,
        revoked=revoked,
        in_flight_after=len(executor.ledger.snapshot()["in_flight"]),
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
