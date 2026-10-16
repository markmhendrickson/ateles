"""Agentic dispatcher eval: the real dispatch entrances deliver via the real
job entrypoint, a stand-in agent making real git commits, a local bare remote,
and a stand-in PR API. Then a fresh dispatcher replays both entrances.

No network/model or live credentials. The source job eval judges remote refs,
PR API effects, secret isolation, and cleanup; this judge adds routing, receipt,
operator artifact and replay behavior. Collected by the existing eval CI lane.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for path in (ROOT / "execution/daemons/apis", HERE.parent / "fly_runner_delivery"):
    sys.path.insert(0, str(path))

import apis
import build_delivery as delivery
import fly_delivery_eval as job_eval
import skill_runner
from test_swarm_dispatch import (
    SwarmDispatcher,
    _StubNotifier,
    _config,
    _empty_spec_state,
    _issue_trigger,
)

SCENARIOS = json.loads((HERE / "fixtures/scenarios.json").read_text())["scenarios"]
JOBS = {s["id"]: s for s in job_eval.load_scenarios()}


@dataclasses.dataclass
class Trace:
    launches: list
    local_launches: int
    first_url: str
    first_stdout: str
    receipt_status: str
    task_replay_ok: bool
    pipeline_replay_refused: bool
    job_trace: object


def run_scenario(scenario, tmp_path, monkeypatch, *, mutation=None):
    monkeypatch.setenv(
        delivery.FLY_DELIVERY_REGISTRY_ENV, str(tmp_path / "receipts.json")
    )
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")
    monkeypatch.setattr(
        delivery, "_new_branch", lambda label: (job_eval.BRANCH, "eval", "eval")
    )
    launches = []
    local = []
    traces = []
    trig = dataclasses.replace(_issue_trigger(), repository=job_eval.REPO, number=123)
    snap = dict(
        title="Build the approved change",
        assigned_to="cicada",
        description="Implement and commit",
        repository=job_eval.REPO,
        issue_number=123,
        action_type="open_or_merge_pr",
        status="pending",
    )

    async def run(skill, prompt, **kw):
        launches.append((prompt, kw))
        root = tmp_path / f"job-{len(launches)}"
        root.mkdir()
        jt = job_eval.run_scenario(JOBS[scenario["job"]], root, monkeypatch)
        traces.append(jt)
        fields = skill_runner._fly_delivery_fields(
            type(
                "Job",
                (),
                dict(delivery={"branch": job_eval.BRANCH}, delivery_result=jt.record),
            )(),
            unknown=scenario["status"] == "unknown",
        )
        return skill_runner.SkillResult(
            skill,
            jt.rc == 0,
            jt.rc,
            jt.record.get("stdout", ""),
            "",
            remote_run_id=jt.record["run_id"],
            **fields,
        )

    async def task():
        return await apis._spawn_harness_skill(
            "cicada", "ent_eval_task", snap, "created", _StubNotifier(), role="cicada"
        )

    async def never(*a, **kw):
        local.append(True)
        raise delivery.BuildWorktreeError("eval observed local fallback")

    monkeypatch.setattr(apis, "run_skill", run)
    monkeypatch.setattr("swarm_dispatch.run_skill", run)
    monkeypatch.setattr(delivery, "prepare_build_worktree", never)
    first_url = first_stdout = ""
    if scenario["entry"] == "task":
        first = asyncio.run(task())
        first_stdout = first.stdout
        first_url = first.fly_pr_url
    else:
        try:
            first_url = (
                asyncio.run(
                    SwarmDispatcher(_StubNotifier(), _config())._open_implementation_pr(
                        trig, _empty_spec_state()
                    )
                )
                or ""
            )
        except delivery.FlyDeliveryEnded:
            pass
    receipt = json.loads(delivery._registry_path().read_text())["deliveries"][
        "owner/repo#123"
    ]
    # Restart: discard all in-memory registry state, keep disk receipt, and
    # remove opt-in. A lost/unknown result may already have pushed its branch.
    importlib.reload(delivery)
    monkeypatch.delenv("APIS_FLY_RUNNER_CLASSES")
    if mutation == "forget_receipt":
        monkeypatch.setattr(delivery, "fly_delivery_blocker", lambda key: "")
    if mutation == "duplicate_remote":
        traces[0].api_calls.append(
            {"method": "POST", "path": "/duplicate", "body": {}, "token_ok": True}
        )
    task_replay_ok = False
    pipeline_refused = False
    # Preserve the observer after reload; either entrance reaching provisioning
    # is an observable failure even though the test doubles deny the effect.
    monkeypatch.setattr(delivery, "prepare_build_worktree", never)
    replay = asyncio.run(task())
    task_replay_ok = replay.ok
    try:
        asyncio.run(
            SwarmDispatcher(_StubNotifier(), _config())._open_implementation_pr(
                trig, _empty_spec_state()
            )
        )
    except delivery.FlyDeliveryEnded:
        pipeline_refused = True
    except delivery.BuildNotStarted:
        pass
    return Trace(
        launches,
        len(local),
        first_url,
        first_stdout,
        receipt["status"],
        task_replay_ok,
        pipeline_refused,
        traces[0],
    )


def judge(scenario, trace):
    problems = job_eval.judge(JOBS[scenario["job"]], trace.job_trace)
    if len(trace.launches) != 1 or trace.local_launches:
        problems.append("a replay/fallback ran another builder")
    if trace.task_replay_ok or not trace.pipeline_replay_refused:
        problems.append("a restart forgot the delivery receipt")
    if trace.receipt_status != scenario["status"]:
        problems.append("receipt lost the delivery outcome")
    for prompt, kw in trace.launches:
        if kw.get("runner") != "fly" or not kw.get("fly_delivery"):
            problems.append("dispatch did not carry a Fly delivery")
        if any(k in kw for k in ("github_token", "include_github_contract", "cwd")):
            problems.append("Fly dispatch widened local grants")
        if "TOKEN" in prompt or job_eval.TOKEN in prompt:
            problems.append("credential mentioned in prompt")
    if scenario["status"] == "opened":
        if not trace.first_url:
            problems.append("runner PR missing from dispatcher result")
        if (
            scenario["entry"] == "task"
            and "[cicada] pull_request_link:" not in trace.first_stdout
        ):
            problems.append("task has no runner artifact header")
    return problems


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_dispatch_scenario(scenario, tmp_path, monkeypatch):
    trace = run_scenario(scenario, tmp_path, monkeypatch)
    assert judge(scenario, trace) == []


@pytest.mark.parametrize("mutation", ["forget_receipt", "duplicate_remote"])
def test_the_judge_goes_red_when_a_delivery_can_repeat(tmp_path, monkeypatch, mutation):
    trace = run_scenario(SCENARIOS[0], tmp_path, monkeypatch, mutation=mutation)
    assert judge(SCENARIOS[0], trace)
