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
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for path in (ROOT / "execution/daemons/apis", HERE.parent / "fly_runner_delivery"):
    sys.path.insert(0, str(path))

import apis  # noqa: E402
import build_delivery as delivery  # noqa: E402
import fly_delivery_eval as job_eval  # noqa: E402
import skill_runner  # noqa: E402
import fly_runner  # noqa: E402
from test_fly_runner import FakeFly, make_executor, FLY_TOKEN  # noqa: E402
from test_swarm_dispatch import (  # noqa: E402
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
        # Exercise executor launch, per-repository token mint ownership, result
        # validation and revocation around the real job-entrypoint artifact.
        monkeypatch.setenv("APIS_FLY_APP", "runner-app")
        monkeypatch.setenv("APIS_FLY_API_TOKEN", FLY_TOKEN)
        monkeypatch.setenv("APIS_FLY_IMAGE", "eval-image")
        monkeypatch.setenv("APIS_FLY_REGION", "ams")
        monkeypatch.setattr(
            fly_runner, "LOGIN_PROVIDERS", frozenset({"codex", "cursor"})
        )
        executor, read_mints, revoked = make_executor(FakeFly(), clock=time.time)
        minted = []

        def mint(repo, **kw):
            minted.append(repo)
            return fly_runner.DeliveryToken(job_eval.TOKEN, time.time() + 3600, repo)

        executor._mint_delivery = mint

        async def read(run_id):
            # The stand-in job runs only after the real executor has minted,
            # reserved and launched; a refused mint cannot create effects.
            with monkeypatch.context() as machine_env:
                # The in-process stand-in machine must not inherit the host's
                # Fly API credential, just like the real job boundary.
                machine_env.delenv("APIS_FLY_API_TOKEN", raising=False)
                jt = job_eval.run_scenario(JOBS[scenario["job"]], root, machine_env)
            traces.append(jt)
            assert jt.record["run_id"] == run_id
            if scenario["status"] == "unknown":
                raise fly_runner.FlyResultUnreadable(
                    "eval lost the result after the job ran"
                )
            return jt.record

        executor._read_result = read
        job = fly_runner.FlyJob(
            run_id="fly-eval-delivery",
            role=skill,
            provider="claude",
            repository=job_eval.REPO,
            argv=["claude", "--print"],
            stdin=prompt.encode(),
            timeout=60,
            needs_github=False,
            delivery=kw["fly_delivery"],
        )
        rc, out, err = await executor.run(job)
        assert minted == [job_eval.REPO] and read_mints == []
        assert revoked == [job_eval.TOKEN] and job.secrets == {}
        fields = skill_runner._fly_delivery_fields(job, unknown=job.outcome_unknown)
        return skill_runner.SkillResult(
            skill,
            rc == 0,
            rc,
            out.decode(),
            err.decode(),
            remote_run_id=job.run_id,
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


@pytest.mark.parametrize("entry", ["task", "pipeline"])
@pytest.mark.parametrize("mode", ["default_off", "ineligible", "refused_mint"])
def test_prelaunch_modes_keep_the_existing_local_delivery(
    tmp_path, monkeypatch, entry, mode
):
    monkeypatch.setenv("APIS_FLY_LEDGER_FILE", str(tmp_path / "ledger.json"))
    monkeypatch.delenv("APIS_FLY_RUNNER_CLASSES", raising=False)
    if mode != "default_off":
        monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")
    local = []
    calls = []
    mint_calls = []
    remote = FakeFly()
    monkeypatch.setattr(fly_runner, "LOGIN_PROVIDERS", frozenset({"codex", "cursor"}))

    async def prepare(repo, label):
        local.append(repo)
        return delivery.BuildWorktree(
            str(tmp_path / "checkout"),
            "cicada/local",
            "a" * 40,
            str(tmp_path / "clone"),
            True,
        )

    def mint(repo, **kw):
        mint_calls.append(repo)
        raise fly_runner.FlyIneligible("stand-in App refused the requested scope")

    async def run(skill, prompt, **kw):
        calls.append(kw)
        if kw.get("fly_delivery"):
            if mode == "refused_mint":
                executor, _, _ = make_executor(remote, clock=time.time)
                executor._mint_delivery = mint
                job = fly_runner.FlyJob(
                    "eval-prelaunch",
                    skill,
                    "claude",
                    job_eval.REPO,
                    [],
                    b"",
                    60,
                    False,
                    delivery=kw["fly_delivery"],
                )
                try:
                    await executor.run(job)
                except fly_runner.FlyIneligible:
                    pass
            return skill_runner.SkillResult(
                skill, False, None, "", "", fly_ineligible_before_launch=True
            )
        assert kw["include_github_contract"] and kw["cwd"]
        assert "YOUR WORKTREE" in prompt
        return skill_runner.SkillResult(
            skill, True, 0, f"https://github.com/{job_eval.REPO}/pull/7", ""
        )

    monkeypatch.setattr(delivery, "prepare_build_worktree", prepare)
    monkeypatch.setattr(apis, "run_skill", run)
    monkeypatch.setattr("swarm_dispatch.run_skill", run)
    monkeypatch.setenv("CICADA_AGENT_PAT", "eval-local-placeholder-token")
    if entry == "task":
        snap = dict(
            title="Build",
            assigned_to="cicada",
            repository=job_eval.REPO,
            issue_number=123,
            action_type="open_or_merge_pr",
        )
        assert asyncio.run(
            apis._spawn_harness_skill(
                "cicada", "ent_eval_prelaunch", snap, "created", _StubNotifier()
            )
        ).ok
    else:
        trig = dataclasses.replace(
            _issue_trigger(), repository=job_eval.REPO, number=123
        )
        assert (
            asyncio.run(
                SwarmDispatcher(_StubNotifier(), _config())._open_implementation_pr(
                    trig, _empty_spec_state()
                )
            )
            == f"https://github.com/{job_eval.REPO}/pull/7"
        )
    assert local == [job_eval.REPO]
    assert len(calls) == (1 if mode == "default_off" else 2)
    assert mint_calls == ([job_eval.REPO] if mode == "refused_mint" else [])
    assert not remote.requests
    assert delivery.fly_delivery_blocker("owner/repo#123") == ""
