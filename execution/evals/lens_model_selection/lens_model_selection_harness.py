"""Offline agent-invoked guarded lens CLI eval, with real dispatch and argv.

Scenarios drive main -> run_one/run_compare -> dispatch_role.dispatch ->
run_skill -> the real provider command builder -> a local synthetic child.
Only external edges are fixtures: worktree provisioning, OS sandbox probes,
Neotoma definitions/events, and provider executables. This is not an OS sandbox
proof; the existing containment effect tests cover that boundary. No model or
network service is contacted, and no GitHub publication path is enabled.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
from pathlib import Path
import sys
import textwrap

import httpx
import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for path in (ROOT, ROOT / "execution/scripts", ROOT / "execution/daemons/apis"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import harness_lens_runner as runner  # noqa: E402
import model_tiering  # noqa: E402
import skill_runner  # noqa: E402
from lib.daemon_runtime.agent_loader import AgentDefinition  # noqa: E402

HEAD = "a" * 40
MUTATIONS = ("none", "no_validation", "drop_model", "drop_action_class")
BINDING = {
    "codex": {"top": "gpt-6-sol", "mid": "gpt-5.6-terra"},
    "cursor": {"top": "gpt-5.6-sol-high", "mid": "gpt-5.6-terra-medium"},
}


def load():
    return json.loads((HERE / "scenarios.json").read_text())


def _executable(path, body):
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    path.chmod(0o700)
    return path


def run_scenario(scenario, tmp_path, *, mutation="none"):
    assert mutation in MUTATIONS
    trace = tmp_path / "provider-calls.jsonl"
    child = _executable(tmp_path / "fixture-provider", f'''
        import json, os, re, sys
        from pathlib import Path
        argv = sys.argv[1:]
        prompt = sys.stdin.read()
        record = {{"argv": argv, "model": argv[argv.index("--model") + 1] if "--model" in argv else None,
                  "local_review": os.environ.get("GIT_CONFIG_NOSYSTEM") == "1" and os.environ.get("GIT_TERMINAL_PROMPT") == "0",
                  "github_credential_present": bool(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))}}
        with Path({str(trace)!r}).open("a") as stream:
            stream.write(json.dumps(record) + "\\n")
        path = re.search(r"It must only write the verdict to (.+?) using", prompt).group(1)
        verdict = "<!-- review:pm commit={HEAD} -->\\n**🤖 Pavo — Ateles swarm, pm review**\\n**SIGNED_OFF**\\n\\nSynthetic eval verdict.\\n"
        Path(path).write_text(verdict)
        print("Verdict: APPROVE\\n" if "--ignore-user-config" in argv else verdict, end="")
    ''')
    wrapper = _executable(tmp_path / "fixture-sandbox", '''
        import os, sys
        assert sys.argv[1] == "-f"
        os.execv(sys.argv[3], sys.argv[3:])
    ''')
    brief = tmp_path / "brief.md"
    brief.write_text("Review this synthetic fixture; write only the requested verdict.\n")
    obs = {"dispatch_calls": [], "events": [], "provider_calls": [], "definition_loads": []}
    stdout, stderr = io.StringIO(), io.StringIO()

    with pytest.MonkeyPatch.context() as mp:
        def no_http(*args, **kwargs):
            raise AssertionError("unexpected external HTTP in an offline eval")

        async def no_async_http(*args, **kwargs):
            raise AssertionError("unexpected external HTTP in an offline eval")

        mp.setattr(httpx.Client, "request", no_http)
        mp.setattr(httpx.AsyncClient, "request", no_async_http)
        # Isolated fixture configuration; never consult operator capacity/files.
        for key, value in {
            "APIS_HARNESS_PROVIDERS": "codex,cursor",
            "APIS_USAGE_GATED_PROVIDERS": "claude",
            "APIS_USAGE_PROBE": "off",
            "APIS_HARNESS_HEADROOM": '{"codex":1.0,"cursor":1.0}',
            "APIS_HARNESS_HEADROOM_FILE": str(tmp_path / "headroom.json"),
            "APIS_HARNESS_USAGE_FILE": str(tmp_path / "usage.json"),
            "APIS_TIER_LEDGER_FILE": str(tmp_path / "tier.jsonl"),
            "APIS_FLY_RUNNER_CLASSES": "",
            "GITHUB_TOKEN": "eval-only-not-a-credential",
            "GH_TOKEN": "eval-only-not-a-credential",
        }.items():
            mp.setenv(key, value)
        mp.setattr(runner.harness_router, "_cooldown_until", {})
        mp.setattr(runner.harness_router, "_current_weights", {})
        mp.setattr(skill_runner, "CODEX_BIN", str(child))
        mp.setattr(skill_runner, "CURSOR_BIN", str(child))
        mp.setattr(skill_runner, "ATELES_REPO", ROOT)
        mp.setattr(runner.dispatch_role, "ATELES_REPO", ROOT)
        mp.setattr(skill_runner, "TRUSTED_MACOS_SANDBOX_EXEC", wrapper)
        mp.setattr(runner, "TRUSTED_MACOS_SANDBOX_EXEC", wrapper)
        def definition(role):
            obs["definition_loads"].append(role)
            return AgentDefinition(name=role, prompt_markdown="Synthetic review role.", tool_allowlist=[])

        mp.setattr(skill_runner, "_load_agent_def", definition)
        mp.setattr(runner.dispatch_role, "_load_agent_def", skill_runner._load_agent_def)
        mp.setattr(skill_runner, "_load_active_policy_prompt", lambda role: "")
        mp.setattr(skill_runner, "_write_harness_event", lambda **kw: obs["events"].append(kw))
        mp.setattr(model_tiering, "configured_action_policy", lambda: {"lens_review:arch": "top"})
        mp.setattr(model_tiering, "configured_vendor_binding", lambda: BINDING)

        def create(worktree, *, head):
            assert head == HEAD
            worktree.path.mkdir(parents=True)
            agents = worktree.path / "docs/agents"
            agents.mkdir(parents=True)
            (agents / "pavo.md").write_text("Synthetic canonical prompt fixture.\n")
            worktree._created = True

        def sandbox(cls, provider, scratch, **kw):
            home = tmp_path / (provider + "-home")
            home.mkdir(exist_ok=True)
            profile = home / "profile.sb"
            profile.write_text("synthetic OS-probe fixture\n")
            ready = not scenario.get("guard_refuses", False)
            return runner.HarnessSandbox(
                provider=provider, root=home,
                env_extra={"CODEX_HOME": str(home), "ATELES_LOCAL_REVIEW_HOME": str(home)},
                command_wrapper=[str(wrapper), "-f", str(profile)],
                credential_read_denied=ready, credential_binding_protected=ready,
                user_config_write_denied=ready, git_stash_denied=ready,
                authentication_ready=ready, review_write_confined=ready,
                unavailable_guards=() if ready else ("synthetic guard refusal",),
            )

        mp.setattr(runner, "current_pr_head", lambda **kw: HEAD)
        # --post is absent; any accidental publication must fail the eval.
        def no_publication(**kw):
            raise AssertionError("unexpected publication attempt")

        mp.setattr(runner, "post_verdict", no_publication)
        mp.setattr(runner.Worktree, "create", create)
        mp.setattr(runner.Worktree, "remove", lambda worktree: None)
        mp.setattr(runner.HarnessSandbox, "build", classmethod(sandbox))
        mp.setattr(runner, "capture_stash_ref_state", lambda path: runner.StashRefState(None, None))
        real_dispatch = runner.dispatch_role.dispatch

        async def dispatch(role, task, **kw):
            if mutation == "drop_model":
                kw.pop("model", None)
            elif mutation == "drop_action_class":
                kw.pop("action_class", None)
            obs["dispatch_calls"].append({k: kw.get(k) for k in (
                "provider", "model", "action_class", "local_review", "seated_reviewer", "task_entity_id"
            )})
            return await real_dispatch(role, task, **kw)

        mp.setattr(runner.dispatch_role, "dispatch", dispatch)
        if mutation == "no_validation":
            mp.setattr(runner, "validate_model_selector", lambda model: model)
            mp.setattr(model_tiering, "validate_model_selector", lambda model: model)
        argv = [
            "--repo", "eval-owner/eval-repo", "--pr", "1", "--head", HEAD,
            "--lens", "pm", "--brief", str(brief), "--timeout", "5", "--json",
            "--task-entity-id", "ent_eval_task",
            *scenario["args"],
        ]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                entry = scenario.get("entrypoint", "lens_cli")
                if entry == "lens_cli":
                    obs["exit_code"] = runner.main(argv)
                else:
                    provider = scenario.get("provider", "codex")
                    task = runner.final_child_instruction(tmp_path / "verdict.md")
                    model = scenario.get("model")
                    action_class = scenario.get("action_class", "lens_review:arch")
                    if entry == "dispatch_cli":
                        direct_args = ["--role", "pavo", "--task", task,
                                       "--provider", provider, "--cwd", str(tmp_path),
                                       "--action-class", action_class, "--task-entity-id", "ent_eval_task", "--json"]
                        if "model" in scenario:
                            direct_args += ["--model=" + model]
                        obs["exit_code"] = runner.dispatch_role.main(direct_args)
                    elif entry == "review_prompt":
                        mp.setenv("APIS_REVIEW_MODELS", json.dumps({provider: model}))
                        result = asyncio.run(skill_runner.run_review_prompt(role="pavo", prompt=task, timeout=5))
                        obs["exit_code"] = 0 if result.ok else 1
                        print(json.dumps({"ok": result.ok, "reason": result.error}))
                    elif entry == "provider_command":
                        async def sink():
                            cmd, payload = skill_runner._provider_command(
                                provider, str(child), "Synthetic role", task,
                                cwd=str(tmp_path), model=model)
                            proc = await asyncio.create_subprocess_exec(
                                *cmd, stdin=asyncio.subprocess.PIPE,
                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                            await proc.communicate(payload or task.encode())
                            return proc.returncode
                        rc = asyncio.run(sink())
                        obs["exit_code"] = rc
                        print(json.dumps({"ok": rc == 0}))
                    else:
                        ready = sandbox(runner.HarnessSandbox, provider, tmp_path)
                        kwargs = dict(provider=provider, model=model, action_class=action_class,
                                      cwd=str(tmp_path), timeout=5, task_entity_id="ent_eval_task",
                                      local_review=True, env_extra=ready.env_extra,
                                      command_wrapper=ready.command_wrapper,
                                      codex_outer_sandboxed=provider == "codex")
                        if entry == "dispatch":
                            call = runner.dispatch_role.dispatch("pavo", task, **kwargs)
                        elif entry in {"run_skill", "run_once"}:
                            fn = skill_runner.run_skill if entry == "run_skill" else skill_runner._run_skill_once
                            call = fn("pavo", task, role="pavo", **kwargs)
                        else:
                            raise AssertionError(f"unknown eval entrypoint: {entry}")
                        result = asyncio.run(call)
                        obs["exit_code"] = 0 if result.ok else 1
                        print(json.dumps({"ok": result.ok, "reason": result.error}))
            except SystemExit as exc:
                obs["exit_code"] = exc.code
            except ValueError as exc:
                obs["exit_code"] = 2
                print(str(exc), file=sys.stderr)
                print(json.dumps({"ok": False, "reason": str(exc)}))
        if trace.exists():
            obs["provider_calls"] = [json.loads(line) for line in trace.read_text().splitlines()]
    obs["stderr"] = stderr.getvalue()
    output = stdout.getvalue().strip()
    obs["report"] = json.loads(output) if output else None
    return obs


def check(scenario, obs):
    exp = scenario["expect"]
    failures = []
    for key in ("exit_code",):
        if obs[key] != exp[key]:
            failures.append(f"{key} {obs[key]!r}, expected {exp[key]!r}")
    if len(obs["provider_calls"]) != exp["launches"]:
        failures.append(f"provider launches {len(obs['provider_calls'])}, expected {exp['launches']}")
    if "dispatches" in exp and len(obs["dispatch_calls"]) != exp["dispatches"]:
        failures.append(f"dispatches {len(obs['dispatch_calls'])}, expected {exp['dispatches']}")
    if "models" in exp and [c["model"] for c in obs["provider_calls"]] != exp["models"]:
        failures.append("actual provider model argv differs")
    for call in obs["dispatch_calls"]:
        if scenario.get("entrypoint", "lens_cli") == "lens_cli" and (not call["local_review"] or call["seated_reviewer"]):
            failures.append("dispatch lost its local review boundary")
        if scenario.get("entrypoint", "lens_cli") == "lens_cli" and call["task_entity_id"] != "ent_eval_task":
            failures.append("dispatch lost task attribution")
    for event in obs["events"]:
        if scenario.get("entrypoint") != "review_prompt" and event["task_entity_id"] != "ent_eval_task":
            failures.append("event lost task attribution")
    for call in obs["provider_calls"]:
        if scenario.get("entrypoint", "lens_cli") not in {"dispatch_cli", "provider_command", "review_prompt"} and (not call["local_review"] or call["github_credential_present"]):
            failures.append("review child lost its publication boundary")
    report = obs["report"]
    reports = list(report["results"].values()) if report and report.get("compare") else [report]
    for result in reports:
        if exp["exit_code"] == 0 and (not result or not result.get("ok")):
            failures.append("native CLI result did not pass its guards/validation")
        if result and result.get("posted"):
            failures.append("eval attempted publication")
    if "dry_model" in exp:
        result = reports[0]
        if not result or result.get("model") != exp["dry_model"]:
            failures.append("dry-run model differs")
        elif exp["dry_model"] and result["example_command"][result["example_command"].index("--model") + 1] != exp["dry_model"]:
            failures.append("dry-run command model differs")
        elif exp["dry_model"] is None and "--model" in result["example_command"]:
            failures.append("dry-run introduced a model flag for omission")
    if exp.get("refusal") and (obs["definition_loads"] or obs["events"]):
        failures.append("invalid model reached definition loading or dispatch events")
    if exp.get("refusal") and "model must be" not in obs["stderr"]:
        failures.append("missing actionable model refusal")
    return failures
