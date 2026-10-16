"""Agent-invoked CLI policy contracts through a real synthetic provider child.

Only canonical role metadata, audit persistence and the provider executable are
synthetic externals. CLI parsing, dispatch, policy reads, routing and adapter
argv construction use production code in a fresh process per scenario.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SCENARIOS = json.loads((HERE / "fixtures/policy_cli_scenarios.json").read_text())

_DRIVER = '''import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import dispatch_role, skill_runner
from lib.daemon_runtime import AgentDefinition
import json
context = json.loads(Path(sys.argv[2]).read_text())
repo = Path(context["repo"])
role = AgentDefinition(name="pavo", aauth_sub="pavo@ateles-swarm", tier="T4",
                       prompt_markdown="# Synthetic offline role", tool_allowlist=["Bash", "Read"])
dispatch_role.ATELES_REPO = skill_runner.ATELES_REPO = repo
dispatch_role._load_agent_def = skill_runner._load_agent_def = lambda name: role
skill_runner._provider_binaries = lambda: {"codex": context["binary"], "claude": None, "cursor": None}
skill_runner._write_harness_event = lambda **kwargs: None
if context["failure"] == "unreadable":
    real_read = Path.read_text
    def read(path, *args, **kwargs):
        if path == Path(context["policy"]):
            raise PermissionError("synthetic private configuration data")
        return real_read(path, *args, **kwargs)
    Path.read_text = read
raise SystemExit(dispatch_role.main(context["argv"]))
'''


@pytest.mark.parametrize("case", SCENARIOS, ids=lambda case: case["id"])
def test_agent_policy_cli_scenario(tmp_path, case):
    skill = tmp_path / ".claude/skills/pavo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Synthetic offline role\n")
    invocation = tmp_path / "child-argv.json"
    binary = tmp_path / "codex"
    binary.write_text(f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
                      f"Path({str(invocation)!r}).write_text(json.dumps(sys.argv[1:]))\n"
                      "sys.stdin.read()\nprint('done')\n")
    binary.chmod(0o700)
    example = ROOT / "docs/examples/model-tiering"
    policy = example / "action-policy.json"
    binding = example / "vendor-binding.json"
    failure = case.get("failure", "")
    if "policy_raw" in case:
        policy = tmp_path / "policy.json"
        policy.write_text(case["policy_raw"])
    elif failure and "binding_raw" not in case:
        policy = tmp_path / "policy.json"
        policy.write_text("{invalid" if failure == "json" else "{}")
    if "binding_raw" in case:
        binding = tmp_path / "binding.json"
        binding.write_text(case["binding_raw"])
    args = ["--role", "pavo", "--task", "offline policy eval", "--provider", "codex", "--json"]
    if case.get("action"):
        args += ["--action-class", case["action"]]
    if case.get("model_override"):
        args += ["--model", case["model_override"]]
    if case.get("escalate"):
        args += ["--review-round", "3", "--new-blocking-finding", "--diff-lines", "900"]
    context = tmp_path / "context.json"
    context.write_text(json.dumps({"repo": str(tmp_path), "binary": str(binary),
                                   "policy": str(policy), "failure": failure, "argv": args}))
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path),
           "PYTHONPATH": str(ROOT), "APIS_HARNESS_PROVIDERS": "codex",
           "APIS_HARNESS_HEADROOM": '{"codex":1.0}', "APIS_USAGE_GATE": "off",
           "APIS_USAGE_PROBE": "off", "APIS_ACTION_POLICY_FILE": str(policy),
           "APIS_VENDOR_BINDING_FILE": str(binding),
           "APIS_TIER_LEDGER_FILE": str(tmp_path / "ledger.jsonl")}
    done = subprocess.run([sys.executable, str(driver), str(ROOT / "execution/daemons/apis"), str(context)],
                          env=env, capture_output=True, text=True, timeout=30)
    envelope = json.loads(done.stdout)
    if failure:
        assert done.returncode == 1 and envelope["ok"] is False
        assert "model tiering configuration" in envelope["reason"]
        assert not invocation.exists(), "refused policy reached provider child"
    else:
        assert done.returncode == 0 and envelope["ok"] is True, envelope
        argv = json.loads(invocation.read_text())
        assert argv[argv.index("--model") + 1] == case["model"]
