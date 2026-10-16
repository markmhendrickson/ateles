"""Offline real-preflight eval; no provider, repository or credential access.

Mutation controls intercept prohibited plans before any subprocess execution.
"""
from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "execution/scripts"))
import harness_lens_runner as runner  # noqa: E402

MUTATIONS = ("none", "remove_ref_deny", "bypass_plan_validation", "force_probe_success")


def load():
    return json.loads((HERE / "scenarios.json").read_text())


def run_scenario(scenario, tmp_path, *, mutation="none"):
    assert mutation in MUTATIONS
    fixture_auth = tmp_path / "fixture-auth.json"
    fixture_auth.write_text("synthetic subscription capability", encoding="utf-8")
    real_run = subprocess.run
    observed = {"effect_calls": 0, "prohibited_plans": 0}

    def checked_run(command, *args, **kwargs):
        if "stash" in command:
            observed["prohibited_plans"] += 1
            raise RuntimeError("prohibited plan intercepted before execution")
        if runner._REF_EFFECT_PROGRAM in command:
            observed["effect_calls"] += 1
        return real_run(command, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runner, "_codex_auth_source", lambda: fixture_auth)
        mp.setattr(runner.subprocess, "run", checked_run)
        if scenario["fault"] == "remove_ref_deny" or mutation == "remove_ref_deny":
            mp.setattr(runner, "_STASH_REF_DENY_REGEXES", ())
        if scenario["fault"] == "inject_command":
            mp.setattr(runner, "_ref_effect_command", lambda *a: ["git", "stash", "push"])
        if mutation == "bypass_plan_validation":
            source = textwrap.dedent(inspect.getsource(runner._run_ref_effect_probe))
            needle = "    if command != expected:\n        return None\n"
            assert needle in source
            namespace = dict(vars(runner))
            exec(compile(source.replace(needle, ""), "<planted-probe-mutation>", "exec"), namespace)
            mp.setattr(runner, "_run_ref_effect_probe", namespace["_run_ref_effect_probe"])
        if mutation == "force_probe_success":
            mp.setattr(runner, "probe_stash_ref_effects_denied", lambda *a: True)
        sandbox = runner.HarnessSandbox.build("codex", tmp_path / "sandbox")
        observed.update(
            ready=sandbox.ready_to_dispatch,
            fully_guarded=sandbox.fully_guarded,
            ref_guard=sandbox.git_stash_denied,
            refusal=runner.refuse_if_guard_required(sandbox),
            other_guards=all((sandbox.credential_read_denied,
                              sandbox.credential_binding_protected,
                              sandbox.user_config_write_denied,
                              sandbox.review_write_confined)),
        )
    observed["ok"] = (
        observed["ready"] == scenario["ready"]
        and observed["other_guards"]
        and observed["prohibited_plans"] == 0
        and (observed["effect_calls"] == 40 if scenario["ready"] else True)
    )
    return observed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, choices=[s["id"] for s in load()])
    parser.add_argument("--mutation", choices=MUTATIONS, default="none")
    args = parser.parse_args()
    if runner.trusted_sandbox_exec_path() is None:
        parser.error("this effect eval requires the trusted macOS sandbox")
    scenario = next(s for s in load() if s["id"] == args.scenario)
    with tempfile.TemporaryDirectory(prefix="guard-policy-eval-") as raw:
        result = run_scenario(scenario, Path(raw), mutation=args.mutation)
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
