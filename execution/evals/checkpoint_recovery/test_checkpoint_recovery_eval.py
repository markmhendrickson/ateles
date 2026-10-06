"""Scenario eval: recovering held tasks into resolvable checkpoints.

Drives the real recovery command (``reissue_held_checkpoints.main``) and the
real signed checkpoint writer against a stateful record stand-in
(``fake_neotoma.py``) that enforces the producer's grant, idempotency-key reuse,
and title-keyed identity.  The scenario file declares the population; every
assertion is about the resulting state of tasks and checkpoints, not about which
functions ran.

Each test names the behavior whose removal turns it red:
* dry run performs zero writes;
* applying leaves each held task held, with exactly one authenticated pending
  checkpoint, and writes no relationship on any identity;
* an exact replay creates nothing;
* a changed task keeps one resolvable current checkpoint;
* a failed persistence retires nothing;
* finished tasks are untouched;
* paging reaches every held task, and read failures are reported distinctly.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent.parent
sys.path.insert(0, str(REPO / "execution" / "daemons" / "apis"))
sys.path.insert(0, str(REPO / "execution" / "mcp" / "ateles"))
sys.path.insert(0, str(HERE))

# The daemon test lane does not install the MCP SDK; the checkpoint authority
# read exercised below is a plain function in server.py.
if importlib.util.find_spec("mcp") is None:
    _shapes = {
        "mcp": types.ModuleType("mcp"),
        "mcp.server": types.ModuleType("mcp.server"),
        "mcp.server.stdio": types.ModuleType("mcp.server.stdio"),
        "mcp.types": types.ModuleType("mcp.types"),
    }

    class _Shape:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    _shapes["mcp.server"].Server = _Shape
    _shapes["mcp.server.stdio"].stdio_server = None
    _shapes["mcp.types"].TextContent = _Shape
    _shapes["mcp.types"].Tool = _Shape
    sys.modules.update(_shapes)

import apis  # noqa: E402
import reissue_held_checkpoints as cli  # noqa: E402
import server  # noqa: E402
from fake_neotoma import CHECKPOINT, FakeNeotoma  # noqa: E402
from lib.daemon_runtime import gating  # noqa: E402
from lib.daemon_runtime.aauth_httpsig import HttpSigSigner  # noqa: E402
from lib.daemon_runtime.gating import ExecutionPolicy  # noqa: E402

SCENARIO = json.loads((HERE / "scenario.json").read_text())
HELD = [t["id"] for t in SCENARIO["tasks"] if t["status"] == "awaiting_approval"]
FINISHED = [t["id"] for t in SCENARIO["tasks"] if t["status"] != "awaiting_approval"]


def _signer() -> HttpSigSigner:
    from cryptography.hazmat.primitives.asymmetric import ec

    private = ec.generate_private_key(ec.SECP256R1()).private_numbers()

    def b64u(value: int) -> str:
        return base64.urlsafe_b64encode(value.to_bytes(32, "big")).rstrip(b"=").decode()

    return HttpSigSigner(
        private_jwk={
            "kty": "EC",
            "crv": "P-256",
            "d": b64u(private.private_value),
            "x": b64u(private.public_numbers.x),
            "y": b64u(private.public_numbers.y),
            "sub": "apis@ateles-swarm",
            "kid": "eval-apis-key",
        },
        sub="apis@ateles-swarm",
        iss="https://markmhendrickson.com",
        kid="eval-apis-key",
    )


@pytest.fixture
def world(monkeypatch):
    signer = _signer()
    fake = FakeNeotoma(SCENARIO, signer.thumbprint)
    monkeypatch.setattr(gating, "NEOTOMA_BASE_URL", "https://neotoma.test")
    monkeypatch.setattr(gating, "NEOTOMA_BEARER_TOKEN", "eval-token")
    monkeypatch.setattr(gating, "CHECKPOINT_REQUIRED_APPROVER_JKT", "A" * 43)
    monkeypatch.setattr(gating, "CHECKPOINT_PRODUCER_JKT", signer.thumbprint)
    monkeypatch.setattr(
        gating, "_checkpoint_producer_http_signer", lambda handler: signer
    )
    monkeypatch.setattr(gating.httpx, "post", fake.post)
    monkeypatch.setattr(gating.httpx, "get", fake.get)
    policy = ExecutionPolicy(
        entity_id="policy",
        low_blast_action_types=frozenset({"local_edit"}),
        high_blast_action_types=frozenset(),
        loaded=True,
    )
    monkeypatch.setattr(apis, "resolve_policy_for_agent", lambda _skill: policy)
    return fake


def run(monkeypatch, capsys, *argv) -> tuple[int, list[dict]]:
    monkeypatch.setattr(sys, "argv", ["reissue_held_checkpoints.py", *argv])
    code = cli.main()
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    return code, lines


def outcomes(lines) -> dict[str, str]:
    return {line["task_id"]: line["outcome"] for line in lines if "task_id" in line}


def resolvable(world, task_id) -> list[str]:
    """Pending checkpoints whose authority the resolver surface will accept."""
    found = []
    for checkpoint_id in world.pending_checkpoints(task_id):
        record = world.record(checkpoint_id)
        if gating.read_authenticated_checkpoint_authorization(checkpoint_id, record):
            found.append(checkpoint_id)
    return found


def test_dry_run_performs_zero_writes(world, monkeypatch, capsys):
    before = {i: len(e["observations"]) for i, e in world.entities.items()}

    code, lines = run(monkeypatch, capsys)

    assert code == 0
    assert world.write_log == []
    assert {i: len(e["observations"]) for i, e in world.entities.items()} == before
    got = outcomes(lines)
    assert all(got[t] == "would_reissue" for t in HELD)
    assert not set(FINISHED) & set(got), "finished tasks are not even enumerated"


def test_apply_gives_each_held_task_exactly_one_authenticated_checkpoint(
    world, monkeypatch, capsys
):
    code, lines = run(monkeypatch, capsys, "--apply")

    assert code == 0, lines
    summary = lines[-1]
    assert summary["examined"] == len(HELD), "paging reached every held task"
    for task_id in HELD:
        assert len(world.pending_checkpoints(task_id)) == 1, task_id
        assert len(resolvable(world, task_id)) == 1, task_id
        assert world.entities[task_id]["fields"]["status"] == "awaiting_approval"
    # The pre-signing brief of a held task is retired, never left beside the new one.
    assert world.entities["ent_legacy_held"]["fields"]["status"] == "superseded"
    # Two tasks that share a title still get two checkpoints.
    assert world.pending_checkpoints("ent_task_twin_a") != world.pending_checkpoints(
        "ent_task_twin_b"
    )
    # The relationship write stays within the producer's grant: none is made.
    assert world.relationship_requests == []


def test_finished_tasks_are_untouched(world, monkeypatch, capsys):
    snapshot = {t: dict(world.entities[t]["fields"]) for t in FINISHED}
    legacy_done = dict(world.entities["ent_legacy_done"]["fields"])

    run(monkeypatch, capsys, "--apply")
    # Even when a finished task is named explicitly, it is skipped and untouched.
    code, lines = run(
        monkeypatch,
        capsys,
        "--apply",
        *[a for t in FINISHED for a in ("--task", t)],
    )
    assert code == 0
    assert all(v == "skipped" for v in outcomes(lines).values())

    for task_id in FINISHED:
        assert world.entities[task_id]["fields"] == snapshot[task_id]
        assert world.pending_checkpoints(task_id) == (
            ["ent_legacy_done"] if task_id == "ent_task_done" else []
        )
    assert world.entities["ent_legacy_done"]["fields"] == legacy_done


def test_exact_replay_creates_nothing(world, monkeypatch, capsys):
    run(monkeypatch, capsys, "--apply")
    entities = set(world.entities)
    writes = list(world.write_log)

    code, lines = run(monkeypatch, capsys, "--apply")

    assert code == 0
    assert set(world.entities) == entities
    assert world.write_log == writes
    assert all(outcomes(lines)[t] == "skipped" for t in HELD)


def test_changed_task_keeps_one_resolvable_current_checkpoint(
    world, monkeypatch, capsys
):
    run(monkeypatch, capsys, "--apply")
    first = world.pending_checkpoints("ent_task_held_plain")
    world.touch_task("ent_task_held_plain", blocked_reason="re-held after a change")

    code, lines = run(monkeypatch, capsys, "--apply")

    assert code == 0, lines
    current = world.pending_checkpoints("ent_task_held_plain")
    assert len(current) == 1 and current != first, "a distinct, current checkpoint"
    assert resolvable(world, "ent_task_held_plain") == current
    assert world.entities[first[0]]["fields"]["status"] == "superseded"


def test_failed_persistence_retires_nothing_and_exits_nonzero(
    world, monkeypatch, capsys
):
    world.fail_store = True

    code, lines = run(monkeypatch, capsys, "--apply")

    assert code == 1
    assert all(outcomes(lines)[t] == "failed" for t in HELD)
    assert world.entities["ent_legacy_held"]["fields"]["status"] == "awaiting_operator"
    assert [
        e
        for e in world.entities.values()
        if e["type"] == CHECKPOINT
        and e["fields"].get("status") == "awaiting_operator"
        and e["observations"][-1]["provenance"]
    ] == []


def test_partial_retirement_is_reported_then_a_rerun_finishes_without_a_second_replacement(
    world, monkeypatch, capsys
):
    world.fail_correct_ids = {"ent_legacy_held"}

    code, lines = run(monkeypatch, capsys, "--apply")

    assert code == 1
    assert outcomes(lines)["ent_task_held_legacy"] == "incomplete"
    replacement = [
        c
        for c in world.pending_checkpoints("ent_task_held_legacy")
        if c != "ent_legacy_held"
    ]
    assert len(replacement) == 1
    world.fail_correct_ids = set()

    code, lines = run(monkeypatch, capsys, "--apply")

    assert code == 0
    assert outcomes(lines)["ent_task_held_legacy"] == "retired_stale"
    assert world.pending_checkpoints("ent_task_held_legacy") == replacement


def test_read_failures_are_reported_distinctly_from_skips(world, monkeypatch, capsys):
    world.fail_query_types = {CHECKPOINT}

    code, lines = run(monkeypatch, capsys, "--apply")

    assert code == 1
    got = outcomes(lines)
    assert all(got[t] == "failed" for t in HELD)
    assert world.write_log == []
    # A confirmed skip stays a skip, distinct from a failed read.
    code, lines = run(monkeypatch, capsys, "--apply", "--task", "ent_task_done")
    assert code == 0 and outcomes(lines) == {"ent_task_done": "skipped"}

    world.fail_query_types = {"task"}
    code, lines = run(monkeypatch, capsys, "--apply")
    assert code == 1
    assert any("enumeration" in line.get("detail", "") for line in lines)


def test_recovered_checkpoint_passes_the_resolver_authority_gate_and_a_legacy_one_does_not(
    world, monkeypatch, capsys
):
    run(monkeypatch, capsys, "--apply")
    new_id = world.pending_checkpoints("ent_task_held_plain")[0]
    monkeypatch.setattr(
        server, "_get", lambda path, params=None: world.record(path.rsplit("/", 1)[-1])
    )
    headers = {
        "signature": "x",
        "signature-input": "x",
        "signature-key": "x",
        "content-digest": "x",
        "content-type": "application/json",
    }

    # The surface the operator uses: the checkpoint's authority is readable ...
    assert (
        server._checkpoint_resolver_authority(new_id, world.record(new_id)) is not None
    )
    # ... whereas the pre-signing brief has none, which is why it could never be resolved.
    legacy = world.record("ent_legacy_done")
    assert server._checkpoint_resolver_authority("ent_legacy_done", legacy) is None
    import asyncio

    refused = asyncio.run(
        server._resolve_checkpoint("ent_legacy_done", "approve", headers)
    )
    assert "no authenticated required-resolver authority" in refused["error"]
    # The recovered checkpoint gets past that gate (and is stopped only by the
    # caller-supplied proof, which this scenario does not have).
    passed = asyncio.run(server._resolve_checkpoint(new_id, "approve", headers))
    assert "no authenticated required-resolver authority" not in passed["error"]
    # Exactly one resolvable checkpoint stands for the task: one possible release.
    assert resolvable(world, "ent_task_held_plain") == [new_id]
