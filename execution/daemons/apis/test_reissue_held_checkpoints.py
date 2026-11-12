"""Re-issuing a signed checkpoint for tasks still held at the gate.

Background: from 2026-09-28 every signed checkpoint write was refused, leaving
tasks at ``awaiting_approval`` with no checkpoint the operator could see, while
the checkpoints from before signing carry no authority envelope and can never
be resolved.  ``reissue_held_task_checkpoint`` is the recovery.  These tests
drive it against a stateful stand-in for the record, so they assert the EFFECT
on tasks and checkpoints, not merely which functions were called.

What they looked like RED: with the re-issue guards removed, the done-task,
already-resolvable, and idempotency tests fail with a second checkpoint filed or
a finished task re-issued; with the signed write unfixed, nothing persists and
the first test fails on ``failed``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apis  # noqa: E402
import reissue_held_checkpoints as cli  # noqa: E402
from lib.daemon_runtime import gating  # noqa: E402
from lib.daemon_runtime.gating import ExecutionPolicy  # noqa: E402
from test_checkpoint_release import CHECKPOINT_TYPE  # noqa: E402


class _Record:
    """Tasks and checkpoints as the record holds them."""

    def __init__(self, monkeypatch):
        self.tasks: dict[str, dict] = {}
        self.checkpoints: dict[str, dict] = {}
        self.by_key: dict[str, str] = {}
        self.writes = 0
        self.persist = True
        self.fail_supersede: set[str] = set()
        self.unreadable: set[str] = set()
        self.policy = ExecutionPolicy(
            entity_id="policy", loaded=True, authorization_revision="policy-v1"
        )
        self.events: list[str] = []
        self.strict_reads: list[bool] = []
        monkeypatch.setattr(apis, "fetch_task_record", self.fetch_task)
        monkeypatch.setattr(apis, "fetch_entity_user_id", lambda _id: "tenant-a")
        monkeypatch.setattr(
            apis, "resolve_policy_for_agent", lambda _skill: self.policy
        )
        monkeypatch.setattr(apis, "checkpoints_for_task", self.for_task)
        monkeypatch.setattr(apis, "fetch_checkpoint_record", self.fetch_checkpoint)
        monkeypatch.setattr(apis, "_checkpoint_authority_state", self.authority_state)
        monkeypatch.setattr(apis, "write_checkpoint_brief", self.write_brief)
        monkeypatch.setattr(apis, "supersede_checkpoint", self.supersede)

    def add_task(self, task_id, status="awaiting_approval", **fields):
        self.tasks[task_id] = {
            "entity_id": task_id,
            "entity_type": "task",
            "observation_count": 3,
            "snapshot": {
                "title": f"Task {task_id}",
                "status": status,
                "assigned_to": "cicada",
                **fields,
            },
        }

    def add_checkpoint(
        self, checkpoint_id, task_id, *, signed, status="awaiting_operator"
    ):
        self.checkpoints[checkpoint_id] = {
            "entity_id": checkpoint_id,
            "entity_type": "checkpoint_" + "brief",
            "signed": signed,
            # The task and policy revisions the envelope was signed against.
            "revision": gating.entity_record_digest(self.tasks[task_id]),
            "policy_revision": gating.execution_policy_revision(self.policy),
            "snapshot": {"task_entity_id": task_id, "status": status},
        }

    def fetch_task(self, task_id):
        return self.tasks.get(task_id)

    def for_task(self, task_id, *, strict=False):
        self.strict_reads.append(strict)
        return [
            c
            for c in self.checkpoints.values()
            if c["snapshot"]["task_entity_id"] == task_id
        ]

    def fetch_checkpoint(self, checkpoint_id):
        return self.checkpoints.get(checkpoint_id)

    def authority_state(self, checkpoint_id, task_record):
        if checkpoint_id in self.unreadable:
            return "unreadable"
        record = self.checkpoints[checkpoint_id]
        if not record.get("signed"):
            return "stale"  # a pre-signing brief has no authority envelope
        if not self.policy.loaded:
            return "unreadable"
        current = record["revision"] == gating.entity_record_digest(
            task_record
        ) and record["policy_revision"] == gating.execution_policy_revision(
            self.policy
        )
        return "current" if current else "stale"

    def write_brief(self, *, task_entity_id, idempotency_context, task_record, **_kw):
        self.writes += 1
        if not self.persist:
            return None
        self.events.append("store")
        key = f"{task_entity_id}-{idempotency_context}"
        if key not in self.by_key:
            checkpoint_id = f"ent_new_{len(self.by_key)}"
            self.by_key[key] = checkpoint_id
            self.add_checkpoint(checkpoint_id, task_entity_id, signed=True)
        return self.by_key[key]

    def supersede(self, checkpoint_id, *, handler, reason):
        if checkpoint_id in self.fail_supersede:
            return False
        self.events.append(f"retire {checkpoint_id}")
        self.checkpoints[checkpoint_id]["snapshot"]["status"] = "superseded"
        return True

    def set_policy(self, **changes):
        import dataclasses

        self.policy = dataclasses.replace(self.policy, **changes)


@pytest.fixture
def record(monkeypatch):
    return _Record(monkeypatch)


def _awaiting(record):
    return [
        c["entity_id"]
        for c in record.checkpoints.values()
        if c["snapshot"]["status"] == "awaiting_operator" and c["signed"]
    ]


def test_held_task_with_no_checkpoint_gets_a_signed_resolvable_one(record):
    record.add_task("ent_t1")

    result = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert result.outcome == "reissued"
    assert _awaiting(record) == [result.checkpoint_id]
    # The task itself is untouched: nothing is released or dispatched.
    assert record.tasks["ent_t1"]["snapshot"]["status"] == "awaiting_approval"


def test_legacy_pending_checkpoint_is_replaced_and_retired(record):
    record.add_task("ent_t1")
    record.add_checkpoint("ent_legacy", "ent_t1", signed=False)

    result = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert result.outcome == "reissued"
    assert record.checkpoints["ent_legacy"]["snapshot"]["status"] == "superseded"
    assert result.superseded == ["ent_legacy"]
    assert _awaiting(record) == [result.checkpoint_id]


@pytest.mark.parametrize(
    "status", ["done", "completed", "declined", "superseded", "blocked", "executing"]
)
def test_a_task_that_is_not_held_is_never_reissued(record, status):
    record.add_task("ent_t1", status=status)
    record.add_checkpoint("ent_legacy", "ent_t1", signed=False)

    result = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert result.outcome == "skipped"
    assert record.writes == 0
    assert record.checkpoints["ent_legacy"]["snapshot"]["status"] == "awaiting_operator"


def test_rerun_is_idempotent_one_checkpoint_per_task(record):
    record.add_task("ent_t1")

    first = apis.reissue_held_task_checkpoint("ent_t1", apply=True)
    again = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert first.outcome == "reissued"
    assert again.outcome == "skipped"
    assert again.checkpoint_id == first.checkpoint_id
    assert len(_awaiting(record)) == 1
    assert record.writes == 1


def test_changed_task_gets_a_new_checkpoint_and_the_stale_one_is_retired(record):
    record.add_task("ent_t1")
    first = apis.reissue_held_task_checkpoint("ent_t1", apply=True)
    record.tasks["ent_t1"]["observation_count"] = 9  # task moved on since

    second = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert second.outcome == "reissued"
    assert second.checkpoint_id != first.checkpoint_id
    assert second.superseded == [first.checkpoint_id]
    assert _awaiting(record) == [second.checkpoint_id]


def test_dry_run_writes_nothing(record):
    record.add_task("ent_t1")
    record.add_checkpoint("ent_legacy", "ent_t1", signed=False)

    result = apis.reissue_held_task_checkpoint("ent_t1", apply=False)

    assert result.outcome == "would_reissue"
    assert record.writes == 0
    assert record.checkpoints["ent_legacy"]["snapshot"]["status"] == "awaiting_operator"


def test_unpersisted_replacement_fails_closed_and_retires_nothing(record):
    record.add_task("ent_t1")
    record.add_checkpoint("ent_legacy", "ent_t1", signed=False)
    record.persist = False

    result = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert result.outcome == "failed"
    assert record.checkpoints["ent_legacy"]["snapshot"]["status"] == "awaiting_operator"
    assert record.tasks["ent_t1"]["snapshot"]["status"] == "awaiting_approval"


def test_unreadable_checkpoint_list_is_a_failure_not_a_skip(record, monkeypatch):
    record.add_task("ent_t1")
    monkeypatch.setattr(apis, "checkpoints_for_task", lambda _id, **_kw: None)

    result = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert result.outcome == "failed"
    assert record.writes == 0


def test_unreadable_task_is_a_failure_not_a_skip(record):
    # ent_missing was never added: the read returns nothing.
    result = apis.reissue_held_task_checkpoint("ent_missing", apply=True)

    assert result.outcome == "failed"
    assert record.writes == 0


def test_failed_retirement_is_incomplete_and_a_rerun_finishes_it(record):
    record.add_task("ent_t1")
    record.add_checkpoint("ent_legacy", "ent_t1", signed=False)
    record.fail_supersede = {"ent_legacy"}

    first = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert first.outcome == "incomplete"
    assert first.remaining == ["ent_legacy"]
    assert first.checkpoint_id is not None
    writes_after_first = record.writes

    record.fail_supersede = set()  # the transient failure clears
    second = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert second.outcome == "retired_stale"
    assert second.checkpoint_id == first.checkpoint_id, "the replacement is reused"
    assert record.writes == writes_after_first, "no second replacement is created"
    assert record.checkpoints["ent_legacy"]["snapshot"]["status"] == "superseded"
    assert _awaiting(record) == [first.checkpoint_id]
    assert record.tasks["ent_t1"]["snapshot"]["status"] == "awaiting_approval"

    third = apis.reissue_held_task_checkpoint("ent_t1", apply=True)
    assert third.outcome == "skipped"


def test_replacement_is_never_retired_even_if_the_record_returns_its_id_as_stale(
    record, monkeypatch
):
    """A replacement that comes back under an id already pending must survive."""
    record.add_task("ent_t1")
    record.add_checkpoint("ent_same", "ent_t1", signed=False)
    retired: list[str] = []
    original = record.supersede

    def spy(checkpoint_id, **kwargs):
        retired.append(checkpoint_id)
        return original(checkpoint_id, **kwargs)

    monkeypatch.setattr(apis, "supersede_checkpoint", spy)
    # The writer hands back the already-pending id (identity collision).
    monkeypatch.setattr(apis, "write_checkpoint_brief", lambda **_kw: "ent_same")

    result = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert "ent_same" not in retired
    assert record.checkpoints["ent_same"]["snapshot"]["status"] == "awaiting_operator"
    assert result.checkpoint_id == "ent_same"


def test_an_approved_unreleased_checkpoint_is_left_to_the_release_path(record):
    record.add_task("ent_t1")
    record.add_checkpoint("ent_ok", "ent_t1", signed=True, status="approved")

    result = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert result.outcome == "skipped"
    assert record.writes == 0


def test_enumeration_failure_is_an_error_not_an_empty_list(monkeypatch):
    monkeypatch.setattr(gating, "query_entities", lambda *a, **k: None)

    with pytest.raises(RuntimeError):
        list(apis.iter_held_task_ids())


def test_enumeration_pages_by_offset_to_the_end_and_never_sends_a_cursor(monkeypatch):
    monkeypatch.setattr(gating, "QUERY_PAGE_SIZE", 2)
    ids = ["ent_a", "ent_b", "ent_c", "ent_d", "ent_e"]
    seen: list[dict] = []

    def page(_type, *, snapshot_filters, offset=0, limit=None):
        seen.append({"offset": offset, "limit": limit})
        chunk = ids[offset : offset + limit]
        # The server offers a cursor on a full page; it must not be used.
        return {
            "entities": [{"entity_id": i} for i in chunk],
            "next_cursor": "unusable" if len(chunk) == limit else None,
        }

    monkeypatch.setattr(gating, "query_entities", page)

    assert list(apis.iter_held_task_ids()) == ids
    assert [p["offset"] for p in seen] == [0, 2, 4]


def test_a_dropped_row_on_a_full_page_still_reads_the_next_page(monkeypatch):
    """Paging counts the rows the server returned, not the ones kept: one
    non-object row on a full page must not end paging and truncate the set for
    a non-strict caller."""
    monkeypatch.setattr(gating, "QUERY_PAGE_SIZE", 2)
    pages = [
        {"entities": [{"entity_id": "ent_a"}, "not-an-object"]},
        {"entities": [{"entity_id": "ent_c"}]},
    ]
    seen: list[int] = []

    def page(_type, *, snapshot_filters, offset=0, limit=None):
        seen.append(offset)
        return pages[len(seen) - 1]

    monkeypatch.setattr(gating, "query_entities", page)

    found = gating.query_all_entities("task", snapshot_filters={})
    assert [e["entity_id"] for e in found] == ["ent_a", "ent_c"]
    assert seen == [0, 2]


def _run_cli(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["reissue_held_checkpoints.py", *argv])
    return cli.main()


def test_cli_dry_run_by_default_and_nonzero_on_failure(record, monkeypatch, capsys):
    record.add_task("ent_t1")
    record.persist = False
    assert _run_cli(monkeypatch, "--task", "ent_t1") == 0  # dry run never fails a write
    assert record.writes == 0

    assert _run_cli(monkeypatch, "--task", "ent_t1", "--apply") == 1
    out = capsys.readouterr().out
    assert '"outcome": "failed"' in out


def test_cli_read_failures_exit_nonzero_and_confirmed_skips_exit_zero(
    record, monkeypatch, capsys
):
    record.add_task("ent_done", status="done")
    assert _run_cli(monkeypatch, "--task", "ent_done", "--apply") == 0  # confirmed skip

    assert _run_cli(monkeypatch, "--task", "ent_missing") == 1  # dry run too
    out = capsys.readouterr().out
    assert '"task_id": "ent_missing"' in out and '"outcome": "failed"' in out

    record.add_task("ent_t1")
    monkeypatch.setattr(apis, "checkpoints_for_task", lambda _id, **_kw: None)
    assert _run_cli(monkeypatch, "--task", "ent_t1", "--apply") == 1


def test_cli_partial_retirement_exits_nonzero_then_zero_after_rerun(
    record, monkeypatch, capsys
):
    record.add_task("ent_t1")
    record.add_checkpoint("ent_legacy", "ent_t1", signed=False)
    record.fail_supersede = {"ent_legacy"}

    assert _run_cli(monkeypatch, "--task", "ent_t1", "--apply") == 1
    first = capsys.readouterr().out
    assert '"outcome": "incomplete"' in first and "ent_legacy" in first

    record.fail_supersede = set()
    assert _run_cli(monkeypatch, "--task", "ent_t1", "--apply") == 0
    assert '"outcome": "retired_stale"' in capsys.readouterr().out
    assert (
        _awaiting(record)
        and record.checkpoints["ent_legacy"]["snapshot"]["status"] == "superseded"
    )


def test_cli_enumeration_failure_is_reported_and_nonzero_not_an_empty_run(
    monkeypatch, capsys
):
    monkeypatch.setattr(gating, "query_entities", lambda *a, **k: None)

    assert _run_cli(monkeypatch) == 1
    out = capsys.readouterr().out
    assert "enumeration" in out and '"failed": 1' in out


def test_cli_empty_successful_enumeration_is_distinguishable(monkeypatch, capsys):
    monkeypatch.setattr(
        gating, "query_entities", lambda *a, **k: {"entities": [], "next_cursor": None}
    )

    assert _run_cli(monkeypatch) == 0
    assert '"examined": 0' in capsys.readouterr().out


# ── replacement retirement and release consistency ─────────────────────────


def test_fresh_release_authority_never_retires_the_replacement_itself(monkeypatch):
    retired: list[str] = []
    monkeypatch.setattr(apis, "_file_fresh_checkpoint", lambda **_kw: "ent_same")
    monkeypatch.setattr(
        apis,
        "require_fresh_checkpoint_approval",
        lambda checkpoint_id, **_kw: retired.append(checkpoint_id) or True,
    )

    class _N:
        sent: list = []

        def send(self, *a, **k):
            self.sent.append(a)

    result = apis._require_fresh_release_authority(
        "ent_same", task_id="ent_t1", task_snapshot={}, notifier=_N(), reason="x"
    )

    assert result is None
    assert retired == [], "the replacement must not be retired as the brief it replaces"


def test_a_distinct_replacement_still_retires_the_prior_brief(monkeypatch):
    retired: list[str] = []
    # The prior brief is a readable ordinary one: its original scope qualifies.
    monkeypatch.setattr(
        apis,
        "fetch_checkpoint_record",
        lambda checkpoint_id: {"entity_id": checkpoint_id, "snapshot": {}},
    )
    monkeypatch.setattr(apis, "_file_fresh_checkpoint", lambda **_kw: "ent_new")
    monkeypatch.setattr(
        apis,
        "require_fresh_checkpoint_approval",
        lambda checkpoint_id, **_kw: retired.append(checkpoint_id) or True,
    )

    class _N:
        def send(self, *a, **k):
            pass

    result = apis._require_fresh_release_authority(
        "ent_old", task_id="ent_t1", task_snapshot={}, notifier=_N(), reason="x"
    )

    assert result == "ent_new"
    assert retired == ["ent_old"]


@pytest.mark.parametrize(
    "now_action,now_blast,approved_action,approved_blast,expected",
    [
        # Same decision: consistent.
        ("checkpoint_plan_approval", "low", "checkpoint_plan_approval", "low", True),
        # The gate would now let it through: an explicit approval still stands.
        ("auto_execute", "low", "checkpoint_plan_approval", "low", True),
        # Stricter now, or a different blast radius: never consistent.
        ("checkpoint_plan_approval", "never", "checkpoint_plan_approval", "low", False),
        ("auto_execute", "high", "checkpoint_plan_approval", "low", False),
        ("checkpoint_plan_approval", "high", "checkpoint_plan_approval", "low", False),
    ],
)
def test_release_consistency_with_the_creation_decision(
    now_action, now_blast, approved_action, approved_blast, expected
):
    from lib.daemon_runtime.gating import BlastRadius, GateAction, GateDecision

    decision = GateDecision(
        action=GateAction(now_action),
        blast_radius=BlastRadius(now_blast),
        confidence=0.9,
        threshold=0.85,
        policy_id="p",
        reason="r",
    )

    assert (
        apis._decision_consistent_with_approval(
            decision, gate_action=approved_action, blast_radius=approved_blast
        )
        is expected
    )


@pytest.mark.parametrize("bad", ["0", "-1", "-5", "abc", "1.5"])
@pytest.mark.parametrize("apply_flag", [[], ["--apply"]])
@pytest.mark.parametrize("selection", [[], ["--task", "ent_t1"]])
def test_cli_limit_must_be_a_positive_integer_before_any_recovery_call(
    record, monkeypatch, capsys, bad, apply_flag, selection
):
    record.add_task("ent_t1")
    called: list[str] = []
    monkeypatch.setattr(
        cli,
        "reissue_held_task_checkpoint",
        lambda task_id, **_kw: called.append(task_id),
    )
    monkeypatch.setattr(
        cli, "iter_held_task_ids", lambda **_kw: called.append("enumerated") or iter([])
    )
    monkeypatch.setattr(sys, "argv", ["x", "--limit", bad, *apply_flag, *selection])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
    assert called == [], "no recovery call is made for an invalid --limit"
    assert record.writes == 0
    err = capsys.readouterr().err
    assert "positive integer" in err or "not an integer" in err


@pytest.mark.parametrize("apply_flag", [[], ["--apply"]])
def test_cli_limit_means_the_first_n_tasks_in_both_selection_modes(
    record, monkeypatch, capsys, apply_flag
):
    for task_id in ("ent_a", "ent_b", "ent_c"):
        record.add_task(task_id)
    monkeypatch.setattr(
        gating,
        "query_entities",
        lambda *_a, **_k: {
            "entities": [{"entity_id": i} for i in ("ent_a", "ent_b", "ent_c")],
            "next_cursor": None,
        },
    )

    assert _run_cli(monkeypatch, "--limit", "2", *apply_flag) == 0
    enumerated = [
        json.loads(line)["task_id"]
        for line in capsys.readouterr().out.splitlines()
        if '"task_id"' in line
    ]
    assert enumerated == ["ent_a", "ent_b"]

    assert (
        _run_cli(
            monkeypatch,
            "--limit",
            "2",
            *apply_flag,
            *[a for t in ("ent_a", "ent_b", "ent_c") for a in ("--task", t)],
        )
        == 0
    )
    named = [
        json.loads(line)["task_id"]
        for line in capsys.readouterr().out.splitlines()
        if '"task_id"' in line
    ]
    assert named == ["ent_a", "ent_b"]


def test_iter_held_task_ids_rejects_a_non_positive_limit():
    for bad in (0, -1):
        with pytest.raises(ValueError):
            list(apis.iter_held_task_ids(limit=bad))


# ── a failed read is not evidence that authority is stale ───────────────────


class TestAuthorityState:
    TASK = {"entity_id": "ent_t1", "observation_count": 3}

    def _patch(self, monkeypatch, *, record, observations, authority):
        monkeypatch.setattr(apis, "fetch_checkpoint_record", lambda _id: record)
        monkeypatch.setattr(
            apis, "fetch_entity_observations_strict", lambda _id: observations
        )
        monkeypatch.setattr(
            apis,
            "read_authenticated_checkpoint_authorization",
            lambda _id, _record, *, observations=None: authority,
        )

    def test_unreadable_record_is_unreadable_not_stale(self, monkeypatch):
        self._patch(monkeypatch, record=None, observations=[], authority=None)
        assert apis._checkpoint_authority_state("ent_cp", self.TASK) == "unreadable"

    def test_unreadable_observations_are_unreadable_not_stale(self, monkeypatch):
        self._patch(
            monkeypatch,
            record={"snapshot": {"body": "{}"}},
            observations=None,
            authority=None,
        )
        assert apis._checkpoint_authority_state("ent_cp", self.TASK) == "unreadable"

    def test_a_brief_with_no_envelope_is_positively_stale(self, monkeypatch):
        self._patch(
            monkeypatch, record={"snapshot": {}}, observations=None, authority=None
        )
        assert apis._checkpoint_authority_state("ent_cp", self.TASK) == "stale"

    def test_readable_but_unauthenticated_envelope_is_stale(self, monkeypatch):
        self._patch(
            monkeypatch,
            record={"snapshot": {"body": "{}"}},
            observations=[],
            authority=None,
        )
        assert apis._checkpoint_authority_state("ent_cp", self.TASK) == "stale"

    POLICY = ExecutionPolicy(
        entity_id="policy",
        loaded=True,
        authorization_revision="policy-v1",
        low_blast_action_types=frozenset({"local_edit"}),
    )

    def _authority(self, **overrides):
        envelope = {
            "task_entity_id": "ent_t1",
            "task_revision": gating.entity_record_digest(self.TASK),
            "task_observation_count": 3,
            "task_last_observation_at": None,
            "action_type": "local_edit",
            "policy_entity_id": "policy",
            "policy_revision": "policy-v1",
            "gate_action": "checkpoint_plan_approval",
            "blast_radius": "low",
        }
        return {**envelope, **overrides}

    def _state(self, monkeypatch, *, authority, policy=None, action_type="local_edit"):
        policy = policy or self.POLICY
        decision = gating.evaluate_gate(
            confidence=0.3, action_type=action_type, policy=policy
        )
        self._patch(
            monkeypatch,
            record={"snapshot": {"body": "{}"}},
            observations=[],
            authority=authority,
        )
        monkeypatch.setattr(
            apis,
            "_task_gate_context",
            lambda _snapshot, **_kw: ("cicada", action_type, policy, decision),
        )
        return apis._checkpoint_authority_state("ent_cp", self.TASK)

    def test_envelope_bound_to_another_task_revision_is_stale(self, monkeypatch):
        authority = self._authority(task_revision="someone-else")
        assert self._state(monkeypatch, authority=authority) == "stale"

    def test_envelope_bound_to_the_task_and_policy_as_they_are_is_current(
        self, monkeypatch
    ):
        assert self._state(monkeypatch, authority=self._authority()) == "current"

    def test_unchanged_task_under_a_changed_policy_revision_is_stale(
        self, monkeypatch
    ):
        authority = self._authority(policy_revision="policy-v0")
        assert self._state(monkeypatch, authority=authority) == "stale"

    def test_unchanged_task_under_a_different_policy_entity_is_stale(
        self, monkeypatch
    ):
        authority = self._authority(policy_entity_id="another-policy")
        assert self._state(monkeypatch, authority=authority) == "stale"

    def test_a_different_action_type_is_stale(self, monkeypatch):
        authority = self._authority(action_type="send_email")
        assert self._state(monkeypatch, authority=authority) == "stale"

    def test_a_blast_radius_the_policy_no_longer_gives_is_stale(self, monkeypatch):
        # Same policy revision on paper, but the classification now differs.
        authority = self._authority(
            blast_radius="never", gate_action="checkpoint_plan_approval"
        )
        assert self._state(monkeypatch, authority=authority) == "stale"

    def test_a_policy_that_cannot_be_established_is_unreadable_not_stale(
        self, monkeypatch
    ):
        unloaded = ExecutionPolicy(entity_id="policy", loaded=False)
        assert (
            self._state(monkeypatch, authority=self._authority(), policy=unloaded)
            == "unreadable"
        )


def test_unreadable_pending_checkpoint_fails_without_replacing_or_retiring(record):
    record.add_task("ent_t1")
    record.add_checkpoint("ent_signed", "ent_t1", signed=True)
    record.add_checkpoint("ent_legacy", "ent_t1", signed=False)
    record.unreadable = {"ent_signed"}

    result = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert result.outcome == "failed"
    assert record.writes == 0
    assert record.checkpoints["ent_signed"]["snapshot"]["status"] == "awaiting_operator"
    assert record.checkpoints["ent_legacy"]["snapshot"]["status"] == "awaiting_operator"


def test_failed_task_reread_after_replacement_is_incomplete_and_retires_nothing(
    record, monkeypatch
):
    record.add_task("ent_t1")
    record.add_checkpoint("ent_legacy", "ent_t1", signed=False)
    original = record.fetch_task

    def reread_fails(task_id):
        return None if record.writes > 0 else original(task_id)

    monkeypatch.setattr(apis, "fetch_task_record", reread_fails)

    result = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert result.outcome == "incomplete"
    assert result.checkpoint_id and result.remaining == ["ent_legacy"]
    assert record.checkpoints["ent_legacy"]["snapshot"]["status"] == "awaiting_operator"

    monkeypatch.setattr(apis, "fetch_task_record", original)
    writes = record.writes
    second = apis.reissue_held_task_checkpoint("ent_t1", apply=True)
    assert second.outcome == "retired_stale"
    assert second.checkpoint_id == result.checkpoint_id
    assert record.writes == writes
    assert record.checkpoints["ent_legacy"]["snapshot"]["status"] == "superseded"


@pytest.mark.parametrize(
    "fields,outcome",
    [
        ({"action_type": "operator_only"}, "needs_operator"),
        ({"action_type": "not_in_any_policy_set"}, "needs_classification"),
    ],
)
def test_task_approval_cannot_release_is_a_distinct_outcome(record, fields, outcome):
    record.add_task("ent_t1", **fields)

    result = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert result.outcome == outcome
    assert "will NOT release" in result.detail
    assert result.checkpoint_id


def test_cli_exits_zero_for_needs_operator_and_nonzero_for_incomplete(
    record, monkeypatch, capsys
):
    record.add_task("ent_op", action_type="operator_only")
    assert _run_cli(monkeypatch, "--task", "ent_op", "--apply") == 0
    assert '"outcome": "needs_operator"' in capsys.readouterr().out

    record.add_task("ent_t2")
    record.add_checkpoint("ent_legacy2", "ent_t2", signed=False)
    record.fail_supersede = {"ent_legacy2"}
    assert _run_cli(monkeypatch, "--task", "ent_t2", "--apply") == 1


def test_policy_only_change_replaces_a_checkpoint_the_task_did_not_change(record):
    """The task is untouched; only the policy it was filed under changed.  The
    obsolete checkpoint is replaced by one bound to the current policy, and
    retired only after the replacement exists."""
    record.add_task("ent_t1", action_type="local_edit")
    first = apis.reissue_held_task_checkpoint("ent_t1", apply=True)
    assert first.outcome == "reissued"
    record.events.clear()

    again = apis.reissue_held_task_checkpoint("ent_t1", apply=True)
    assert again.outcome == "skipped", "same task, same policy: nothing to do"

    record.set_policy(authorization_revision="policy-v2")
    dry = apis.reissue_held_task_checkpoint("ent_t1", apply=False)
    assert dry.outcome == "would_reissue" and record.events == []

    result = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert result.outcome == "reissued"
    assert result.checkpoint_id != first.checkpoint_id
    assert result.superseded == [first.checkpoint_id]
    assert record.events == ["store", f"retire {first.checkpoint_id}"]
    assert _awaiting(record) == [result.checkpoint_id]


def test_an_unloaded_policy_neither_replaces_nor_retires(record):
    record.add_task("ent_t1", action_type="local_edit")
    first = apis.reissue_held_task_checkpoint("ent_t1", apply=True)
    record.events.clear()
    writes = record.writes
    record.set_policy(loaded=False)

    result = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert result.outcome == "failed"
    assert record.writes == writes and record.events == []
    assert _awaiting(record) == [first.checkpoint_id]


# ── a deliberately retired hold stays retired in a blanket run (ateles#1600) ──


def _retired_hold(record):
    """A held task whose only checkpoint was superseded on purpose."""
    record.add_task("ent_t1")
    record.add_checkpoint("ent_old", "ent_t1", signed=True, status="superseded")
    record.checkpoints["ent_old"]["created_at"] = "2026-11-01T10:00:00Z"


def _blanket(monkeypatch, *task_ids):
    monkeypatch.setattr(cli, "iter_held_task_ids", lambda limit=None: iter(task_ids))


def _records(out):
    return [json.loads(line) for line in out.splitlines() if line.strip()]


@pytest.mark.parametrize("apply_flag", [[], ["--apply"]])
def test_blanket_run_leaves_a_superseded_hold_retired_and_reports_it(
    record, monkeypatch, capsys, apply_flag
):
    _retired_hold(record)
    _blanket(monkeypatch, "ent_t1")

    assert _run_cli(monkeypatch, *apply_flag) == 0
    rows = _records(capsys.readouterr().out)

    assert rows[0]["task_id"] == "ent_t1"
    assert rows[0]["outcome"] == "retired"
    assert rows[0]["checkpoint_id"] == "ent_old"
    assert rows[-1]["summary"] == {"retired": 1}
    assert record.writes == 0
    assert _awaiting(record) == []


def test_naming_a_retired_hold_with_task_refiles_it(record, monkeypatch, capsys):
    _retired_hold(record)
    _blanket(monkeypatch, "ent_t1")
    assert _run_cli(monkeypatch, "--apply") == 0
    assert _records(capsys.readouterr().out)[0]["outcome"] == "retired"
    assert record.writes == 0

    assert _run_cli(monkeypatch, "--task", "ent_t1", "--apply") == 0
    rows = _records(capsys.readouterr().out)

    assert rows[0]["outcome"] == "reissued"
    assert record.writes == 1
    assert _awaiting(record) == [rows[0]["checkpoint_id"]]


def test_blanket_run_still_refiles_a_lost_hold(record, monkeypatch, capsys):
    # No checkpoint at all, and one whose newest checkpoint is neither live nor
    # superseded (an older superseded brief does not make it retired).
    record.add_task("ent_none")
    record.add_task("ent_rejected")
    record.add_checkpoint("ent_a", "ent_rejected", signed=True, status="superseded")
    record.checkpoints["ent_a"]["created_at"] = "2026-11-01T10:00:00Z"
    record.add_checkpoint("ent_b", "ent_rejected", signed=True, status="rejected")
    record.checkpoints["ent_b"]["created_at"] = "2026-11-02T10:00:00Z"
    # A retired hold in the same run is the only one held back.
    record.add_task("ent_retired")
    record.add_checkpoint("ent_c", "ent_retired", signed=True, status="superseded")
    record.checkpoints["ent_c"]["created_at"] = "2026-11-03T10:00:00Z"
    _blanket(monkeypatch, "ent_none", "ent_rejected", "ent_retired")

    assert _run_cli(monkeypatch, "--apply") == 0
    rows = _records(capsys.readouterr().out)

    assert {r["task_id"]: r["outcome"] for r in rows[:3]} == {
        "ent_none": "reissued",
        "ent_rejected": "reissued",
        "ent_retired": "retired",
    }
    assert record.writes == 2


def test_blanket_run_does_not_refile_when_checkpoints_cannot_be_read(
    record, monkeypatch, capsys
):
    record.add_task("ent_t1")
    monkeypatch.setattr(apis, "checkpoints_for_task", lambda _id, **_kw: None)
    _blanket(monkeypatch, "ent_t1")

    assert _run_cli(monkeypatch, "--apply") == 1
    rows = _records(capsys.readouterr().out)

    assert rows[0]["task_id"] == "ent_t1"
    assert rows[0]["outcome"] == "unknown"
    assert record.writes == 0


# ── fail closed when retirement evidence is malformed or unordered (#1704) ──


def _production_reader(monkeypatch, rows):
    """Serve ``rows`` through the real collection reader, one consistent page."""
    monkeypatch.setattr(apis, "checkpoints_for_task", gating.checkpoints_for_task)

    def page(_type, *, snapshot_filters, offset=0, limit=None):
        if offset:
            return {"entities": [], "total": len(rows)}
        return {"entities": list(rows), "total": len(rows)}

    monkeypatch.setattr(gating, "query_entities", page)


def test_blanket_apply_with_a_malformed_row_reports_unknown_and_files_nothing(
    record, monkeypatch, capsys
):
    # The superseded newest brief comes back without an entity_id.  A lenient
    # read keeps it and the reissue path drops it, leaving "no checkpoint", so
    # the blanket run would refile a retired hold.
    record.add_task("ent_t1")
    _production_reader(
        monkeypatch,
        [
            {
                "entity_type": CHECKPOINT_TYPE,
                "created_at": "2026-11-01T10:00:00Z",
                "snapshot": {"task_entity_id": "ent_t1", "status": "superseded"},
            }
        ],
    )
    _blanket(monkeypatch, "ent_t1")

    assert _run_cli(monkeypatch, "--apply") == 1
    rows = _records(capsys.readouterr().out)

    assert rows[0]["task_id"] == "ent_t1"
    assert rows[0]["outcome"] == "unknown"
    assert record.writes == 0
    assert _awaiting(record) == []


@pytest.mark.parametrize(
    "bad_row",
    [
        "not-a-record",
        {"entity_id": "", "snapshot": {"status": "superseded"}},
        {"entity_id": "ent_x", "snapshot": {"task_entity_id": "ent_t1"}},
    ],
    ids=["not_a_mapping", "empty_id", "no_status"],
)
def test_blanket_run_treats_every_malformed_row_shape_as_unknown(
    record, monkeypatch, capsys, bad_row
):
    record.add_task("ent_t1")
    monkeypatch.setattr(apis, "checkpoints_for_task", lambda _id, **_kw: [bad_row])
    _blanket(monkeypatch, "ent_t1")

    assert _run_cli(monkeypatch, "--apply") == 1
    assert _records(capsys.readouterr().out)[0]["outcome"] == "unknown"
    assert record.writes == 0


def test_blanket_run_reads_checkpoints_strictly_and_named_run_does_not(
    record, monkeypatch, capsys
):
    _retired_hold(record)
    _blanket(monkeypatch, "ent_t1")
    _run_cli(monkeypatch, "--apply")
    _run_cli(monkeypatch, "--task", "ent_t1")
    capsys.readouterr()

    assert record.strict_reads == [True, False]


def test_malformed_stamp_on_an_older_live_row_makes_order_unknown(
    record, monkeypatch, capsys
):
    # "zzz" sorts after the real stamp as a string, so string comparison would
    # call the rejected brief newest and refile the retired hold.
    record.add_task("ent_t1")
    record.add_checkpoint("ent_old", "ent_t1", signed=True, status="rejected")
    record.checkpoints["ent_old"]["created_at"] = "zzz"
    record.add_checkpoint("ent_new", "ent_t1", signed=True, status="superseded")
    record.checkpoints["ent_new"]["created_at"] = "2026-11-02T10:00:00Z"
    _blanket(monkeypatch, "ent_t1")

    assert _run_cli(monkeypatch, "--apply") == 1
    rows = _records(capsys.readouterr().out)

    assert rows[0]["outcome"] == "unknown"
    assert record.writes == 0


@pytest.mark.parametrize(
    "stamp",
    [None, "", "not-a-date", "2026-11-03T10:00:00"],
    ids=["missing", "empty", "unparseable", "naive"],
)
def test_unknown_order_with_a_superseded_brief_is_unknown(
    record, monkeypatch, capsys, stamp
):
    record.add_task("ent_t1")
    record.add_checkpoint("ent_a", "ent_t1", signed=True, status="superseded")
    record.checkpoints["ent_a"]["created_at"] = "2026-11-02T10:00:00Z"
    record.add_checkpoint("ent_b", "ent_t1", signed=True, status="rejected")
    if stamp is not None:
        record.checkpoints["ent_b"]["created_at"] = stamp
    _blanket(monkeypatch, "ent_t1")

    assert _run_cli(monkeypatch, "--apply") == 1
    assert _records(capsys.readouterr().out)[0]["outcome"] == "unknown"
    assert record.writes == 0


def test_unknown_order_with_no_superseded_brief_is_refiled(
    record, monkeypatch, capsys
):
    # No superseded brief means no retirement evidence: refile as before.
    record.add_task("ent_t1")
    record.add_checkpoint("ent_a", "ent_t1", signed=True, status="rejected")
    record.checkpoints["ent_a"]["created_at"] = "zzz"
    _blanket(monkeypatch, "ent_t1")

    assert _run_cli(monkeypatch, "--apply") == 0
    assert _records(capsys.readouterr().out)[0]["outcome"] == "reissued"
    assert record.writes == 1


@pytest.mark.parametrize(
    "superseded_at, other_at, expected",
    [
        # The pair from the review: 11:30+01:00 is 10:30Z, so the rejected
        # brief is newer and the hold is refiled.
        ("2026-11-01T10:00:00Z", "2026-11-01T11:30:00+01:00", "reissued"),
        # 09:30-02:00 is 11:30Z, newer than 10:00Z, although as a string it
        # sorts first: string order would refile a retired hold.
        ("2026-11-01T09:30:00-02:00", "2026-11-01T10:00:00Z", "retired"),
        # 10:30+01:00 is 09:30Z, older than 10:00Z, although as a string it
        # sorts last: string order would keep a lost hold retired.
        ("2026-11-01T10:30:00+01:00", "2026-11-01T10:00:00Z", "reissued"),
    ],
)
def test_stamps_in_different_offsets_are_ordered_as_datetimes(
    record, monkeypatch, capsys, superseded_at, other_at, expected
):
    record.add_task("ent_t1")
    record.add_checkpoint("ent_sup", "ent_t1", signed=True, status="superseded")
    record.checkpoints["ent_sup"]["created_at"] = superseded_at
    record.add_checkpoint("ent_rej", "ent_t1", signed=True, status="rejected")
    record.checkpoints["ent_rej"]["created_at"] = other_at
    _blanket(monkeypatch, "ent_t1")

    assert _run_cli(monkeypatch, "--apply") == 0
    assert _records(capsys.readouterr().out)[0]["outcome"] == expected


def test_named_task_run_ignores_retirement_evidence_and_refiles(
    record, monkeypatch, capsys
):
    # Unknown order and a superseded brief: --task still refiles, unchanged.
    record.add_task("ent_t1")
    record.add_checkpoint("ent_a", "ent_t1", signed=True, status="superseded")
    record.checkpoints["ent_a"]["created_at"] = "zzz"

    assert _run_cli(monkeypatch, "--task", "ent_t1", "--apply") == 0
    rows = _records(capsys.readouterr().out)

    assert rows[0]["outcome"] == "reissued"
    assert record.writes == 1


# ── present but unrecognized values are unknown, never evidence (#1705) ──


@pytest.mark.parametrize(
    "status",
    ["unexpected", 7, ["superseded"], {"value": "superseded"}],
    ids=["unrecognized_string", "integer", "list", "mapping"],
)
def test_blanket_run_treats_an_unrecognized_status_as_unknown(
    record, monkeypatch, capsys, status
):
    # A lone checkpoint whose status is not one the code writes or reads is not
    # evidence the hold was not retired: file nothing.
    record.add_task("ent_t1")
    record.add_checkpoint("ent_x", "ent_t1", signed=True, status=status)
    record.checkpoints["ent_x"]["created_at"] = "2026-11-02T10:00:00Z"
    _blanket(monkeypatch, "ent_t1")

    assert _run_cli(monkeypatch, "--apply") == 1
    rows = _records(capsys.readouterr().out)

    assert rows[0]["task_id"] == "ent_t1"
    assert rows[0]["outcome"] == "unknown"
    assert record.writes == 0


@pytest.mark.parametrize(
    "status",
    [
        gating.CHECKPOINT_AWAITING_OPERATOR,
        gating.CHECKPOINT_APPROVED_NO_RELEASE,
        gating.CHECKPOINT_REQUIRES_FRESH_APPROVAL,
        " Rejected ",
    ],
)
def test_blanket_run_still_refiles_on_every_recognized_non_retired_status(
    record, monkeypatch, capsys, status
):
    record.add_task("ent_t1")
    record.add_checkpoint("ent_x", "ent_t1", signed=False, status=status)
    record.checkpoints["ent_x"]["created_at"] = "2026-11-02T10:00:00Z"
    _blanket(monkeypatch, "ent_t1")

    assert _run_cli(monkeypatch, "--apply") == 0
    assert _records(capsys.readouterr().out)[0]["outcome"] == "reissued"
    assert record.writes == 1


@pytest.mark.parametrize(
    "linked_task",
    ["ent_other", None, 42],
    ids=["another_task", "missing", "not_a_string"],
)
def test_blanket_run_treats_a_row_linked_to_another_task_as_unknown(
    record, monkeypatch, capsys, linked_task
):
    # The read filters on the task; a row naming another task (or none) means
    # the listing cannot be trusted as this task's retirement evidence.
    record.add_task("ent_t1")
    row = {
        "entity_id": "ent_x",
        "entity_type": CHECKPOINT_TYPE,
        "created_at": "2026-11-02T10:00:00Z",
        "snapshot": {"task_entity_id": linked_task, "status": "rejected"},
    }
    monkeypatch.setattr(apis, "checkpoints_for_task", lambda _id, **_kw: [row])
    _blanket(monkeypatch, "ent_t1")

    assert _run_cli(monkeypatch, "--apply") == 1
    assert _records(capsys.readouterr().out)[0]["outcome"] == "unknown"
    assert record.writes == 0


def test_named_task_run_still_refiles_with_an_unrecognized_status(
    record, monkeypatch, capsys
):
    record.add_task("ent_t1")
    record.add_checkpoint("ent_x", "ent_t1", signed=True, status="unexpected")

    assert _run_cli(monkeypatch, "--task", "ent_t1", "--apply") == 0
    assert _records(capsys.readouterr().out)[0]["outcome"] == "reissued"
    assert record.writes == 1


# ── Original restrictive scope survives reissue ───────────────────────────────

def _restrict(record, monkeypatch, checkpoint_id, extension=None):
    """Give a pending checkpoint an authenticated restrictive envelope."""
    import json as _json

    authority = {
        **{k: "x" for k in gating.CHECKPOINT_ORDINARY_ENVELOPE_FIELDS},
        **(extension or {"effect_scope": "review_request_only"}),
    }
    record.checkpoints[checkpoint_id]["snapshot"]["body"] = _json.dumps(authority)
    original = apis.read_authenticated_checkpoint_authorization
    monkeypatch.setattr(
        apis,
        "read_authenticated_checkpoint_authorization",
        lambda cid, rec: dict(authority) if cid == checkpoint_id else original(cid, rec),
    )


def test_restricted_pending_checkpoint_is_never_replaced_or_retired(
    record, monkeypatch
):
    record.add_task("ent_t1")
    record.add_checkpoint("ent_restricted", "ent_t1", signed=True)
    _restrict(record, monkeypatch, "ent_restricted")
    # The task changed since: ordinary reasoning would call this brief stale.
    record.tasks["ent_t1"]["observation_count"] = 9

    result = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert result.outcome == "skipped"
    assert result.checkpoint_id == "ent_restricted"
    assert record.writes == 0
    assert record.events == []
    assert record.checkpoints["ent_restricted"]["snapshot"]["status"] == (
        "awaiting_operator"
    )


def test_restricted_pending_checkpoint_dry_run_has_zero_effects(record, monkeypatch):
    record.add_task("ent_t1")
    record.add_checkpoint("ent_restricted", "ent_t1", signed=True)
    _restrict(record, monkeypatch, "ent_restricted")

    result = apis.reissue_held_task_checkpoint("ent_t1", apply=False)

    assert result.outcome == "skipped"
    assert "restricted original scope" in result.detail
    assert record.writes == 0 and record.events == []


def test_unqualifiable_scope_fails_without_replacing_or_retiring(record, monkeypatch):
    import json as _json

    record.add_task("ent_t1")
    record.add_checkpoint("ent_odd", "ent_t1", signed=False)
    # An envelope claiming an extension that never authenticated.
    record.checkpoints["ent_odd"]["snapshot"]["body"] = _json.dumps(
        {"version": 2, "effect_scope": "review_request_only"}
    )

    result = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert result.outcome == "failed"
    assert record.writes == 0 and record.events == []
    assert record.checkpoints["ent_odd"]["snapshot"]["status"] == "awaiting_operator"


def test_later_ordinary_checkpoint_stays_and_stale_legacy_is_retired_beside_restricted(
    record, monkeypatch
):
    record.add_task("ent_t1")
    record.add_checkpoint("ent_restricted", "ent_t1", signed=True)
    _restrict(record, monkeypatch, "ent_restricted")
    record.add_checkpoint("ent_legacy", "ent_t1", signed=False)
    record.add_checkpoint("ent_ordinary", "ent_t1", signed=True)

    result = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert result.outcome == "retired_stale"
    assert result.checkpoint_id == "ent_ordinary"
    assert result.superseded == ["ent_legacy"]
    assert record.checkpoints["ent_restricted"]["snapshot"]["status"] == (
        "awaiting_operator"
    )
    assert record.checkpoints["ent_ordinary"]["snapshot"]["status"] == (
        "awaiting_operator"
    )
