"""Agentic eval: local Codex and Cursor swarm children run isolated.

Each scenario drives the agent-facing CLI (``dispatch_role.main --json``)
through the real ``run_skill`` path to a stand-in ``codex`` or
``cursor-agent`` binary, run as a real process. The stand-in reports the MCP
servers its configuration actually declares, so the trace shows what a child
would load. A stand-in operator home carries personal MCP servers (a dev
instance, a client's hosted instance, personal tools); none may reach a
child. Offline: no model, no network, synthetic tokens only.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
_REPO = HERE.parents[2]
_APIS = _REPO / "execution" / "daemons" / "apis"
for _path in (str(_REPO), str(_APIS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import dispatch_role  # noqa: E402
import harness_router  # noqa: E402
import skill_runner  # noqa: E402
from lib.daemon_runtime import AgentDefinition  # noqa: E402

ROLE = "lanius"
ROLE_TOKEN = "eval-role-own-token-0001"
SHARED_BEARER = "eval-operator-shared-bearer-0002"
PERSONAL_SERVERS = ("neotoma-dev", "neotoma", "client-hosted", "typefully", "node_repl",
                    "computer-use", "ateles")  # fmt: skip


def load_scenarios() -> list[dict]:
    return json.loads((HERE / "fixtures" / "scenarios.json").read_text())["scenarios"]


@dataclass
class Trace:
    exit_code: int
    envelope: dict
    spawned: bool
    child: dict | None
    homes_built: list = field(default_factory=list)
    homes_left: list = field(default_factory=list)
    output_text: str = ""
    checked_workspaces: list = field(default_factory=list)
    notifications: list = field(default_factory=list)


# The stand-in CLI: reports the MCP servers its generated config declares.
_STAND_IN = r'''#!{python}
import json, os, pathlib, sys, tomllib
pathlib.Path({marker!r}).write_text("1")
sys.stdin.read()
report = {{"argv": sys.argv[1:], "cwd": os.path.realpath(os.getcwd())}}
import hashlib
_bearer = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
report["bearer_digest"] = hashlib.sha256(_bearer.encode()).hexdigest() if _bearer else ""
# Resolve the workspace flag as a real CLI does (against the child's own cwd)
# and note whether a project MCP file sits at the root it would use.
import subprocess
argv = sys.argv[1:]
flag = "--workspace" if "--workspace" in argv else "--cd" if "--cd" in argv else None
ws = os.path.realpath(argv[argv.index(flag) + 1]) if flag else os.path.realpath(os.getcwd())
top = subprocess.run(["git", "-C", ws, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
root = os.path.realpath(top.stdout.strip()) if top.returncode == 0 and top.stdout.strip() else ws
report["workspace_flag_value"] = argv[argv.index(flag) + 1] if flag else None
report["project_mcp"] = os.path.lexists(os.path.join(root, ".cursor", "mcp.json"))
codex_home = os.environ.get("CODEX_HOME", "")
if codex_home and pathlib.Path(codex_home, "config.toml").is_file():
    cfg = tomllib.loads(pathlib.Path(codex_home, "config.toml").read_text())
    servers = cfg.get("mcp_servers", {{}})
    report["servers"] = sorted(servers)
    report["enabled_tools"] = {{k: v.get("enabled_tools") for k, v in servers.items()}}
    report["home"] = codex_home
else:
    cfg = json.loads(pathlib.Path(os.environ["HOME"], ".cursor", "mcp.json").read_text())
    report["servers"] = sorted(cfg.get("mcpServers", {{}}))
    report["home"] = os.environ["HOME"]
print("CHILD " + json.dumps(report))
'''


def _operator_home(root: Path) -> Path:
    home = root / "operator"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    (codex / "config.toml").write_text(
        'model = "gpt-operator-default"\n' + "".join(
            f'[mcp_servers.{n}]\nurl = "https://{n}.personal.example.invalid/mcp"\n'
            for n in PERSONAL_SERVERS))  # fmt: skip
    (codex / "auth.json").write_text("{}")
    (codex / "auth.json").chmod(0o600)
    (home / ".cursor").mkdir()
    (home / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {
        n: {"url": f"https://{n}.personal.example.invalid/mcp"} for n in PERSONAL_SERVERS}}))
    (home / "Library" / "Keychains").mkdir(parents=True)
    return home


def run_scenario(scenario: dict, tmp_path: Path, monkeypatch) -> Trace:
    provider = scenario["provider"]
    home = _operator_home(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    settings = {
        "APIS_HARNESS_PROVIDERS": provider,
        "APIS_HARNESS_HEADROOM_FILE": str(tmp_path / "no-headroom.json"),
        "APIS_HARNESS_USAGE_FILE": str(tmp_path / "usage.json"),
        "APIS_USAGE_GATE": "off",
        "APIS_USAGE_PROBE": "off",
        "APIS_TIER_LEDGER_FILE": str(tmp_path / "tier-ledger.jsonl"),
        "APIS_ACTION_POLICY_FILE": str(tmp_path / "policy.json"),
        "APIS_VENDOR_BINDING_FILE": str(tmp_path / "binding.json"),
        "APIS_FLY_LEDGER_FILE": str(tmp_path / "fly-ledger.json"),
        "NEOTOMA_BASE_URL": "https://neotoma.eval.invalid",
        "NEOTOMA_BEARER_TOKEN": SHARED_BEARER,
        f"{ROLE.upper()}_NEOTOMA_TOKEN": ROLE_TOKEN,
        **scenario.get("settings", {}),
    }
    for name in ("APIS_FLY_RUNNER_CLASSES", "APIS_FLY_APP", "APIS_FLY_API_TOKEN",
                 "APIS_FLY_IMAGE", "APIS_FLY_REGION", "APIS_HARNESS_HEADROOM"):  # fmt: skip
        monkeypatch.delenv(name, raising=False)
    for name, value in settings.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    if scenario.get("relative_workspace"):
        # The daemon runs in one directory and is handed a RELATIVE workspace
        # below it (not a git checkout). A Cursor MCP file sits one level
        # further down, outside the workspace's own project root.
        daemon_cwd = tmp_path / "daemon-cwd"
        (daemon_cwd / "ws" / "ws" / ".cursor").mkdir(parents=True)
        (daemon_cwd / "ws" / "ws" / ".cursor" / "mcp.json").write_text("{}")
        monkeypatch.chdir(daemon_cwd)
        cwd_arg = "ws"
    else:
        workspace = tmp_path / "repo"
        workspace.mkdir()
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
        if scenario.get("project_cursor_config"):
            (workspace / ".cursor").mkdir()
            (workspace / ".cursor" / "mcp.json").write_text(scenario["project_cursor_config"])
        cwd = workspace / "sub"
        cwd.mkdir()
        cwd_arg = str(cwd)

    marker = tmp_path / "child-spawned"
    stand_in = tmp_path / f"{provider}-stand-in"
    stand_in.write_text(_STAND_IN.format(python=sys.executable, marker=str(marker)))
    stand_in.chmod(0o755)
    monkeypatch.setattr(skill_runner, "CODEX_BIN", str(stand_in))
    monkeypatch.setattr(skill_runner, "CURSOR_BIN", str(stand_in))
    skills = tmp_path / ".claude" / "skills" / ROLE
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(f"# {ROLE}\n")
    agent_def = AgentDefinition(entity_id="eval", name=ROLE, prompt_markdown="You are the role.",
                                tool_allowlist=scenario.get("tools", "*"),
                                aauth_sub=f"{ROLE}@ateles-swarm")  # fmt: skip
    monkeypatch.setattr(dispatch_role, "ATELES_REPO", tmp_path)
    monkeypatch.setattr(skill_runner, "ATELES_REPO", tmp_path)
    monkeypatch.setattr(skill_runner, "_load_agent_def", lambda role: agent_def)
    monkeypatch.setattr(dispatch_role, "_load_agent_def", lambda role: agent_def)
    monkeypatch.setattr(skill_runner, "_load_active_policy_prompt", lambda role: "")
    monkeypatch.setattr(skill_runner, "_write_harness_event", lambda **kw: None)
    monkeypatch.setattr(skill_runner, "DISPATCH_FAILURE_LOG_DIR", tmp_path / "failures")
    harness_router.reset_state()

    built: list[Path] = []
    checked: list[str] = []
    real_prepare = skill_runner.prepare_isolated_harness_home

    def recording_prepare(*args, **kwargs):  # observes; never changes the result
        checked.append(str(Path(kwargs["cwd"]).resolve()) if kwargs.get("cwd") else "")
        isolated = real_prepare(*args, **kwargs)
        built.append(isolated.home)
        return isolated

    notifications: list[dict] = []
    real_notify = skill_runner.notify_dispatch_failure

    def recording_notify(notifier, **kwargs):  # observes; never changes the result
        notifications.append(kwargs)
        return real_notify(notifier, **kwargs)

    monkeypatch.setattr(skill_runner, "notify_dispatch_failure", recording_notify)

    monkeypatch.setattr(skill_runner, "prepare_isolated_harness_home", recording_prepare)

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        if scenario.get("seated"):
            # A seated review (gate-owning deny of `correct`) through the real
            # dispatch entry point that the review lanes use.
            import asyncio

            result = asyncio.run(dispatch_role.dispatch(
                ROLE, "Check the gates.", provider=provider, cwd=cwd_arg,
                seated_reviewer=True))  # fmt: skip
            code = 0 if result.ok else 1
            seated_envelope = {
                "ok": result.ok, "stdout": result.stdout, "error": result.error,
                "attempted_providers": list(result.attempted_providers),
                **({"cooled_until": result.cooled_until} if result.cooled_until else {}),
                **({"configuration_repair": result.configuration_repair}
                   if result.configuration_repair else {}),
            }  # fmt: skip
        else:
            pin = ["--provider", provider] if scenario.get("pin", True) else []
            code = dispatch_role.main(["--role", ROLE, "--task", "Check the gates.", *pin,
                                       "--cwd", cwd_arg, "--json",
                                       *scenario.get("args", [])])  # fmt: skip
    if scenario.get("seated"):
        envelope = seated_envelope
    else:
        try:
            envelope = json.loads(out.getvalue())
        except ValueError:
            envelope = {}
    child = None
    for line in str(envelope.get("stdout", "")).splitlines():
        if line.startswith("CHILD "):
            child = json.loads(line[len("CHILD "):])
    return Trace(
        exit_code=code, envelope=envelope, spawned=marker.exists(), child=child,
        homes_built=[str(h) for h in built], homes_left=[str(h) for h in built if h.exists()],
        output_text=out.getvalue() + err.getvalue(),
        checked_workspaces=checked, notifications=notifications,
    )


def judge(scenario: dict, trace: Trace) -> list[str]:
    e = scenario["expect"]
    env = trace.envelope
    problems: list[str] = []
    if trace.exit_code != e["exit"]:
        problems.append(f"exit {trace.exit_code} != {e['exit']}")
    if "spawned" in e and trace.spawned != e["spawned"]:
        problems.append("a child started" if trace.spawned else "no child started")
    if "child_servers" in e:
        seen = (trace.child or {}).get("servers")
        if seen != e["child_servers"]:
            problems.append(f"the child's MCP servers were {seen}, expected {e['child_servers']}")
    if "child_enabled_tools" in e:
        tools = (trace.child or {}).get("enabled_tools")
        if tools != e["child_enabled_tools"]:
            problems.append(f"the child's tool filter was {tools}")
    if "repair_contains" in e:
        if e["repair_contains"] not in str(env.get("configuration_repair", "")):
            problems.append("the envelope does not name the configuration to repair")
        if not any(e["repair_contains"] in str(n.get("stderr", "")) for n in trace.notifications):
            problems.append("the notification does not name the configuration to repair")
        if env.get("cooled_until"):
            problems.append("a configuration repair was reported as a cooldown")
    if "repairs_for" in e:
        repair = str(env.get("configuration_repair", ""))
        missing = [p for p in e["repairs_for"] if f"{p}: " not in repair]
        if missing or sorted(env.get("attempted_providers", [])) != sorted(e["repairs_for"]):
            problems.append(f"not every provider was attempted and kept its repair: {missing}")
    if e.get("value_absent"):
        everywhere = trace.output_text + json.dumps(trace.notifications)
        if e["value_absent"] in everywhere:
            problems.append("the refused setting's value appeared in the output")
    if "child_bearer" in e and trace.child is not None:
        import hashlib

        wanted = {"role": hashlib.sha256(ROLE_TOKEN.encode()).hexdigest(),
                  "shared": hashlib.sha256(SHARED_BEARER.encode()).hexdigest()}[e["child_bearer"]]
        if trace.child.get("bearer_digest") != wanted:
            problems.append(f"the child's Neotoma bearer is not the {e['child_bearer']} token")
    if trace.child is not None and trace.child.get("bearer_digest") == __import__(
            "hashlib").sha256(SHARED_BEARER.encode()).hexdigest():  # fmt: skip
        problems.append("the child was handed the shared bearer")
    if e.get("one_workspace"):
        child = trace.child or {}
        flag_value = child.get("workspace_flag_value")
        if not (trace.checked_workspaces and flag_value == trace.checked_workspaces[0]
                == child.get("cwd")):  # fmt: skip
            problems.append("the child's workspace is not the one the guard checked")
    if "homes_built" in e and len(trace.homes_built) != e["homes_built"]:
        problems.append(f"{len(trace.homes_built)} isolated homes built, expected {e['homes_built']}")
    if "workspace_flag" in e and trace.child is not None:
        argv = trace.child.get("argv", [])
        if "--workspace" not in argv:
            problems.append("the Cursor child was not given an explicit --workspace")
    # Always: no personal server, no leftover home, no credential in the output.
    for name in PERSONAL_SERVERS:
        if name in ((trace.child or {}).get("servers") or []):
            problems.append(f"the child saw the personal server {name!r}")
    if (trace.child or {}).get("project_mcp") and scenario["provider"] == "cursor":
        problems.append("the child's project root holds an MCP file the guard did not refuse")
    if trace.homes_left:
        problems.append("an isolated home was left behind")
    for label, value in (("role token", ROLE_TOKEN), ("shared bearer", SHARED_BEARER)):
        if value in json.dumps(env) or value in trace.output_text:
            problems.append(f"the {label} appeared in the dispatch output")
    return problems
