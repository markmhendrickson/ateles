"""
The label gate binds every automatic entry into issue/PR software work.

## The failure these cover

ateles#1269 added `ATELES_SWARM_REQUIRE_LABEL` so that, in bootstrap mode, the
swarm pipeline acts only on labelled (canary) work. Its first two rounds gated
`handle_trigger`'s `issue_opened` and PR branches, and nothing else. Review
round 3 (security and pm lenses) traced the other live callers of
`_handle_issue_opened` / `_handle_pr`, each reachable with no operator action:

* `resume_interrupted_pipelines` (run at every daemon boot);
* `resume_deferred_reviews` and `resume_stalled_reviews` (the 600s sweep);
* `_handle_ci_status_for_current_head` (CI-green auto-merge re-panel).

The same sweep loop also dispatches software work WITHOUT going through those
two functions, so they are covered here too:

* `resume_unactioned_revisions` (Cicada pushes a revision);
* `resume_missing_lens_reviews` (one lens re-runs);
* `_handle_ci_status_for_current_head` (a red build routed to Cicada).

Each path gets the same three cases: gate unset -> runs (today's behaviour),
gate set and the work unlabelled -> skipped, gate set and labelled -> runs.
`test_label_gate_binds_every_automatic_entry` then pins the set of direct
callers structurally, so a NEW caller of either function fails until someone
decides whether it is automatic (gate it) or an operator override (list it).

Run: pytest execution/daemons/apis/test_label_gate_entry_paths.py -v
"""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import swarm_dispatch as sd
from github_gateway import SwarmTrigger

CANARY = "swarm-canary"

# (require_label, labels on the work, should the automatic path run?)
GATE_CASES = [
    pytest.param("", [], True, id="gate-unset-unlabelled-runs"),
    pytest.param(CANARY, ["bug"], False, id="gate-set-unlabelled-skipped"),
    pytest.param(CANARY, [CANARY], True, id="gate-set-labelled-runs"),
]


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, msg, priority=None, handler=None, **kwargs):  # noqa: ANN001
        self.sent.append(msg)

    def clear_dedupe(self, key):  # noqa: ANN001
        return None


def _dispatcher(require_label: str, **overrides) -> sd.SwarmDispatcher:
    config = sd.DispatchConfig(
        **{
            "neotoma_token": "",
            "github_token": "",
            "auto_merge": False,
            "auto_rereview_on_push": False,
            "require_label": require_label,
            **overrides,
        }
    )
    return sd.SwarmDispatcher(_Notifier(), config)


def _pr(number: int, labels: list[str]) -> dict:
    # No `Closes #N` in the body: the gate decides on the PR's own labels and
    # never needs the parent-issue fetch, which `_no_fetch` turns into a
    # failure if it is reached.
    return {
        "number": number,
        "title": f"PR {number}",
        "body": "no parent reference",
        "user": {"login": "someone"},
        "html_url": f"https://github.com/o/r/pull/{number}",
        "head": {"ref": "feature", "sha": "c" * 40},
        "base": {"ref": "main"},
        "labels": [{"name": n} for n in labels],
    }


@pytest.fixture(autouse=True)
def _no_fetch(monkeypatch):
    async def fail(self, repository, issue_number):  # noqa: ANN001
        raise AssertionError("parent-issue fetch must not be needed here")

    monkeypatch.setattr(sd.SwarmDispatcher, "_fetch_issue_fields", fail)


# ── boot: resume_interrupted_pipelines -> _handle_issue_opened ─────────────


@pytest.mark.parametrize("require_label,labels,should_run", GATE_CASES)
def test_resume_interrupted_pipelines(monkeypatch, require_label, labels, should_run):
    d = _dispatcher(require_label)
    ran: list[int] = []

    async def scan(self, repository):  # noqa: ANN001
        return [
            {
                "number": 230,
                "title": "stranded",
                "body": "b",
                "user": {"login": "x"},
                "html_url": "u",
                "labels": [{"name": n} for n in labels],
            }
        ]

    async def handle(self, trigger):  # noqa: ANN001
        ran.append(trigger.number)

    async def advanced(self, repository, number, started):  # noqa: ANN001
        return True

    monkeypatch.setattr(sd.SwarmDispatcher, "_issues_with_inflight_marker", scan)
    monkeypatch.setattr(sd.SwarmDispatcher, "_handle_issue_opened", handle)
    monkeypatch.setattr(sd.SwarmDispatcher, "_pipeline_advanced_past_triage", advanced)

    summary = asyncio.run(d.resume_interrupted_pipelines(["o/r"]))

    if should_run:
        assert ran == [230]
        assert summary["resumed"] == 1
        assert "label_gated" not in summary
    else:
        assert ran == [], "an unlabelled issue must not resume past the gate"
        assert summary["resumed"] == 0 and summary["label_gated"] == 1
        assert d.notifier.sent == [], "a gated resume must page nobody"


# ── 600s loop: resume_deferred_reviews -> _handle_pr ───────────────────────


@pytest.mark.parametrize("require_label,labels,should_run", GATE_CASES)
def test_resume_deferred_reviews(monkeypatch, require_label, labels, should_run):
    d = _dispatcher(require_label)
    ran: list[int] = []

    async def scan(self, repository, now):  # noqa: ANN001
        return {"due": [_pr(254, labels)], "scanned": 1, "not_yet": 0}

    async def handle(self, trigger):  # noqa: ANN001
        ran.append(trigger.number)

    monkeypatch.setattr(sd.SwarmDispatcher, "_prs_with_matured_deferral", scan)
    monkeypatch.setattr(sd.SwarmDispatcher, "_handle_pr", handle)

    summary = asyncio.run(d.resume_deferred_reviews(["o/r"]))

    if should_run:
        assert ran == [254] and summary["resumed"] == 1
    else:
        assert ran == [], "an unlabelled PR must not be re-paneled past the gate"
        assert summary["resumed"] == 0 and summary["label_gated"] == 1


# ── 600s loop: resume_stalled_reviews -> _handle_pr ────────────────────────


@pytest.mark.parametrize("require_label,labels,should_run", GATE_CASES)
def test_resume_stalled_reviews(monkeypatch, require_label, labels, should_run):
    d = _dispatcher(require_label)
    ran: list[int] = []

    async def scan(self, repository, now, stall_after):  # noqa: ANN001
        return [_pr(408, labels)]

    async def handle(self, trigger):  # noqa: ANN001
        ran.append(trigger.number)

    monkeypatch.setattr(sd.SwarmDispatcher, "_prs_with_stalled_review", scan)
    monkeypatch.setattr(sd.SwarmDispatcher, "_handle_pr", handle)

    summary = asyncio.run(d.resume_stalled_reviews(["o/r"]))

    if should_run:
        assert ran == [408] and summary["resumed"] == 1
    else:
        assert ran == [], "an unlabelled PR must not be re-dispatched past the gate"
        assert summary["label_gated"] == 1
        # The PR has no review BECAUSE of the gate: it must not burn the
        # retry budget and later escalate to the operator as a stuck review.
        assert d._stall_retries == {}


# ── 600s loop: resume_unactioned_revisions -> Cicada ───────────────────────


@pytest.mark.parametrize("require_label,labels,should_run", GATE_CASES)
def test_resume_unactioned_revisions(monkeypatch, require_label, labels, should_run):
    d = _dispatcher(require_label)
    dispatched: list[str] = []

    async def scan(self, repository, now, stale_after):  # noqa: ANN001
        pr = _pr(163, labels)
        pr.update(
            _blocking_review_at="x",
            _revision_pushed_at="y",
            _blocking_review_body="[NON-BLOCKING] nit",
        )
        return [pr]

    async def fake_run_skill(skill, prompt, **kw):  # noqa: ANN001
        dispatched.append(skill)
        return SimpleNamespace(ok=True, error="", stdout="", stderr="")

    monkeypatch.setattr(sd.SwarmDispatcher, "_prs_with_unactioned_revisions", scan)
    monkeypatch.setattr(sd, "run_skill", fake_run_skill)

    summary = asyncio.run(d.resume_unactioned_revisions(["o/r"]))

    if should_run:
        assert dispatched == ["cicada"] and summary["resumed"] == 1
    else:
        assert dispatched == [], "Cicada must not revise an unlabelled PR"
        assert summary["label_gated"] == 1
        assert d._revision_retries == {}


# ── 600s loop: resume_missing_lens_reviews -> one lens ─────────────────────


@pytest.mark.parametrize("require_label,labels,should_run", GATE_CASES)
def test_resume_missing_lens_reviews(monkeypatch, require_label, labels, should_run):
    d = _dispatcher(require_label)
    dispatched: list[tuple[int, str]] = []

    async def scan(self, repository):  # noqa: ANN001
        return [(_pr(2153, labels), ["security"])]

    async def redispatch(self, repository, pr, lens):  # noqa: ANN001
        dispatched.append((pr["number"], lens))

    monkeypatch.setattr(sd.SwarmDispatcher, "_prs_with_missing_lens", scan)
    monkeypatch.setattr(sd.SwarmDispatcher, "_redispatch_missing_lens", redispatch)

    summary = asyncio.run(d.resume_missing_lens_reviews(["o/r"]))

    if should_run:
        assert dispatched == [(2153, "security")] and summary["resumed"] == 1
    else:
        assert dispatched == [], "no lens may re-run on an unlabelled PR"
        assert summary["label_gated"] == 1
        assert d._missing_lens_retries == {}


# ── delayed CI: _handle_ci_status_for_current_head ─────────────────────────


def _ci_trigger() -> SwarmTrigger:
    return SwarmTrigger(
        kind="ci_status",
        repository="o/r",
        number=87,
        title="",
        body="",
        author="",
        html_url="",
        delivery_id="ci-1",
        action="completed",
        ci_head_sha="c" * 40,
        ci_conclusion="success",
    )


def _patch_ci(monkeypatch, *, ci_state: str, calls: list[str]) -> None:
    async def required_ci(self, trigger):  # noqa: ANN001
        return ci_state

    async def review_clear(self, *a, **k):  # noqa: ANN001
        return True

    async def handle_pr(self, trigger):  # noqa: ANN001
        calls.append("handle_pr")

    async def route_ci_failure(self, trigger, parent):  # noqa: ANN001
        calls.append("route_ci_failure")

    async def gate_readiness(self, *a, **k):  # noqa: ANN001
        calls.append("readiness")

    monkeypatch.setattr(sd.SwarmDispatcher, "_required_ci_state", required_ci)
    monkeypatch.setattr(sd.SwarmDispatcher, "_pr_review_is_clear", review_clear)
    monkeypatch.setattr(sd.SwarmDispatcher, "_handle_pr", handle_pr)
    monkeypatch.setattr(sd.SwarmDispatcher, "_route_ci_failure", route_ci_failure)
    monkeypatch.setattr(sd.SwarmDispatcher, "_gate_merge_readiness", gate_readiness)


@pytest.mark.parametrize("require_label,labels,should_run", GATE_CASES)
def test_ci_green_auto_merge_repanel(monkeypatch, require_label, labels, should_run):
    d = _dispatcher(require_label, auto_merge=True)
    calls: list[str] = []
    _patch_ci(monkeypatch, ci_state="green", calls=calls)

    handled = asyncio.run(
        d._handle_ci_status_for_current_head(_ci_trigger(), _pr(87, labels), "c" * 40)
    )

    assert handled is True
    if should_run:
        assert calls == ["handle_pr"]
    else:
        assert calls == [], "the CI-green re-panel must not review an unlabelled PR"


@pytest.mark.parametrize("require_label,labels,should_run", GATE_CASES)
def test_ci_failing_route_to_cicada(monkeypatch, require_label, labels, should_run):
    d = _dispatcher(require_label)
    calls: list[str] = []
    _patch_ci(monkeypatch, ci_state="failing", calls=calls)

    handled = asyncio.run(
        d._handle_ci_status_for_current_head(_ci_trigger(), _pr(87, labels), "c" * 40)
    )

    assert handled is True
    if should_run:
        assert calls == ["route_ci_failure"]
    else:
        assert calls == [], "a red build on an unlabelled PR must not go to Cicada"


def test_ci_green_operator_gated_readiness_is_not_gated(monkeypatch):
    """The operator-gated merge-ready signal starts no software work, so it
    stays outside the gate — the operator still hears that a PR is ready."""
    d = _dispatcher(CANARY, auto_merge=False)
    calls: list[str] = []
    _patch_ci(monkeypatch, ci_state="green", calls=calls)

    asyncio.run(
        d._handle_ci_status_for_current_head(_ci_trigger(), _pr(87, ["bug"]), "c" * 40)
    )

    assert calls == ["readiness"]


# ── sweep skip logging: loud once, then quiet ──────────────────────────────


def test_sweep_skip_warns_once_per_pr_then_debug(monkeypatch, caplog):
    d = _dispatcher(CANARY)

    async def scan(self, repository, now):  # noqa: ANN001
        return {"due": [_pr(254, ["bug"])], "scanned": 1, "not_yet": 0}

    async def handle(self, trigger):  # noqa: ANN001
        raise AssertionError("an unlabelled PR must not reach the panel")

    monkeypatch.setattr(sd.SwarmDispatcher, "_prs_with_matured_deferral", scan)
    monkeypatch.setattr(sd.SwarmDispatcher, "_handle_pr", handle)

    with caplog.at_level(logging.DEBUG, logger=sd.log.name):
        asyncio.run(d.resume_deferred_reviews(["o/r"]))
        asyncio.run(d.resume_deferred_reviews(["o/r"]))

    skips = [r for r in caplog.records if "label gate active" in r.message]
    assert [r.levelno for r in skips] == [logging.WARNING, logging.DEBUG]
    assert "resume_deferred_reviews" in skips[0].message


# ── operator overrides bypass, and no caller goes unclassified ─────────────

_OPERATOR_OVERRIDES = {"_handle_swarm_run", "_handle_confirm_gates_clear"}
_GUARDED = {"_handle_issue_opened", "_handle_pr"}


def _direct_callers() -> dict[str, set[str]]:
    """Map each function in swarm_dispatch.py to the guarded functions it calls
    directly, read from the source rather than from anyone's memory of it."""
    tree = ast.parse(Path(sd.__file__).read_text())
    out: dict[str, set[str]] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _GUARDED
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
            ):
                out.setdefault(fn.name, set()).add(node.func.attr)
    return out


def _calls_gate(fn_name: str) -> bool:
    tree = ast.parse(Path(sd.__file__).read_text())
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == fn_name:
            return any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "_label_gate_allows"
                for n in ast.walk(fn)
            )
    raise AssertionError(f"{fn_name} not found")


def test_label_gate_binds_every_automatic_entry():
    """Every direct caller of `_handle_issue_opened` / `_handle_pr` is either a
    named operator override or consults `_label_gate_allows`. A new caller
    fails here until it is classified — a gate that binds some entrances is
    not a control."""
    callers = _direct_callers()
    assert callers, "the AST scan found no callers — the instrument is broken"
    assert _OPERATOR_OVERRIDES <= set(callers), (
        "an operator override stopped calling the guarded function directly; "
        "re-check the override tests"
    )
    ungated = sorted(
        name
        for name in callers
        if name not in _OPERATOR_OVERRIDES and not _calls_gate(name)
    )
    assert ungated == [], (
        f"automatic entries into {sorted(_GUARDED)} with no label-gate check: "
        f"{ungated}"
    )


def test_operator_overrides_do_not_consult_the_gate():
    for name in _OPERATOR_OVERRIDES:
        assert not _calls_gate(name), (
            f"{name} is an operator override and must bypass the label gate"
        )


# ── parent-issue labels: malformed data denies instead of raising ──────────


@pytest.mark.parametrize(
    "parent,allowed",
    [
        pytest.param({"labels": [{"name": CANARY}]}, True, id="well-formed"),
        pytest.param({"labels": [CANARY]}, False, id="strings-not-dicts"),
        pytest.param({"labels": None}, False, id="null"),
        pytest.param({"labels": CANARY}, False, id="bare-string"),
        pytest.param({"labels": [{"name": None}, 7]}, False, id="junk-entries"),
        pytest.param({}, False, id="missing"),
    ],
)
def test_parent_issue_label_parse_denies_on_bad_data(monkeypatch, parent, allowed):
    """Falco #1269 round 3 (non-blocking): the parent-label read used to sit
    outside the fetch's try and index `lbl.get` directly, so a malformed
    parent `labels` raised out of the gate and ended a sweep pass early. It
    now reuses `_label_names`, so bad data is a clean deny."""
    d = _dispatcher(CANARY)

    async def fetch(self, repository, issue_number):  # noqa: ANN001
        return parent

    monkeypatch.setattr(sd.SwarmDispatcher, "_fetch_issue_fields", fetch)
    trigger = SwarmTrigger(
        kind="pr_opened",
        repository="o/r",
        number=5,
        title="t",
        body="Closes #4",
        author="someone",
        html_url="u",
        delivery_id="d",
        action="opened",
        labels=[],
    )
    assert asyncio.run(d._label_gate_allows(trigger, source="webhook")) is allowed


# ── one gate, one definition: Apis and Anthus share lib/daemon_runtime ─────


def test_apis_gate_helpers_come_from_the_shared_module():
    """Apis and Anthus must not keep two notions of "labelled". The label
    parser and the PR -> parent link parser are the shared module's own
    objects, not copies."""
    from lib.daemon_runtime import label_gate

    import github_gateway

    assert sd._label_names is label_gate.label_names
    assert sd._PARENT_LINK is label_gate.PARENT_LINK
    assert label_gate.REQUIRE_LABEL_ENV == "ATELES_SWARM_REQUIRE_LABEL"
    gw_src = Path(github_gateway.__file__).read_text()
    assert "_label_gate.required_label()" in gw_src
    sd_src = Path(sd.__file__).read_text()
    assert "_label_gate.required_label()" in sd_src
    assert 'os.environ.get("ATELES_SWARM_REQUIRE_LABEL"' not in gw_src + sd_src


# ── Anthus: the second automatic entry, outside this process ──────────────
#
# Falco #1269 round 3: the AST scan above only reads swarm_dispatch.py, so it
# could not see Anthus (execution/daemons/anthus/), which starts gate-owner
# agent runs from Neotoma issue/pull_request events on its own. These pin
# that daemon's launch surface the same way: every agent spawn sits behind
# `_label_gate_allows`, and nothing else in the daemon starts a process.

_ANTHUS_DIR = Path(sd.__file__).resolve().parent.parent / "anthus"
_ANTHUS = _ANTHUS_DIR / "anthus.py"
_SPAWN_CALLS = {"create_subprocess_exec", "create_subprocess_shell", "Popen"}


def _anthus_functions(path: Path):
    tree = ast.parse(path.read_text())
    return [
        fn
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _call_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _calls(fn, names: set[str]) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and _call_name(n) in names
    ]


def test_anthus_only_spawn_agent_starts_a_process():
    sources = [
        p for p in sorted(_ANTHUS_DIR.glob("*.py")) if not p.name.startswith("test_")
    ]
    assert _ANTHUS in sources, "the instrument is pointed at the wrong directory"
    spawners = sorted(
        f"{p.name}:{fn.name}"
        for p in sources
        for fn in _anthus_functions(p)
        if _calls(fn, _SPAWN_CALLS)
    )
    assert spawners == ["anthus.py:_spawn_agent"], (
        "a new Anthus function starts a process; decide whether it launches "
        f"issue/PR work and gate it: {spawners}"
    )


def test_anthus_every_agent_spawn_is_behind_the_label_gate():
    """Every direct caller of `_spawn_agent` consults `_label_gate_allows`,
    and does so BEFORE it selects a workflow or spawns anything — a gate
    evaluated after the `feature` fallback has already dispatched is not a
    gate."""
    fns = _anthus_functions(_ANTHUS)
    callers = [fn for fn in fns if _calls(fn, {"_spawn_agent"})]
    assert [fn.name for fn in callers] == ["_orchestrate_workflow_for"], (
        "the set of Anthus functions that spawn agents changed; classify the "
        f"new one: {[fn.name for fn in callers]}"
    )
    for fn in callers:
        gate = _calls(fn, {"_label_gate_allows"})
        assert gate, f"{fn.name} spawns agents with no label-gate check"
        first_gate = min(n.lineno for n in gate)
        for sink in ("select_workflow", "fetch_workflow_definitions", "_spawn_agent"):
            lines = [n.lineno for n in _calls(fn, {sink})]
            assert lines and first_gate < min(lines), (
                f"{fn.name}: the label gate must run before {sink}"
            )


def test_anthus_orchestration_has_one_door():
    """`_orchestrate_workflow_for` is reached only from the SSE handler, so the
    gate inside it covers every issue/PR event Anthus receives."""
    fns = _anthus_functions(_ANTHUS)
    callers = sorted(fn.name for fn in fns if _calls(fn, {"_orchestrate_workflow_for"}))
    assert callers == ["handle_event"], callers


def test_anthus_gate_reads_the_shared_env_helper():
    fns = {fn.name: fn for fn in _anthus_functions(_ANTHUS)}
    assert "_label_gate_allows" in fns, "Anthus has no label gate"
    gate = fns["_label_gate_allows"]
    assert _calls(gate, {"required_label"}), (
        "Anthus must read ATELES_SWARM_REQUIRE_LABEL through "
        "lib/daemon_runtime/label_gate.required_label, like Apis"
    )
    assert _calls(gate, {"carries_label"})


# ── Formica and neotoma-agent: the triage-label lanes ─────────────────────
#
# Security review on #1269: two more daemons start Cicada/Vanellus from Neotoma
# issue/pull_request events keyed only on their own triage label
# (`triage:formica`, `triage:neotoma-agent`), outside both scans above. These
# pin each daemon's launch surface: `_spawn_claude_skill` is its only process
# launch, it consults the gate before launching, and every `handle_event`
# branch that notifies or dispatches consults the gate first. Behaviour for the
# same lanes: execution/daemons/test_triage_lane_label_gate.py.

_DAEMONS_DIR = Path(sd.__file__).resolve().parent.parent
_TRIAGE_LANES = [
    pytest.param(_DAEMONS_DIR / "formica" / "formica.py", id="formica"),
    pytest.param(
        _DAEMONS_DIR / "neotoma-agent" / "neotoma_agent.py", id="neotoma-agent"
    ),
]
_TRIAGE_SINKS = {"dispatch_cicada", "dispatch_vanellus", "_spawn_claude_skill"}


@pytest.mark.parametrize("path", _TRIAGE_LANES)
def test_triage_lane_only_spawn_skill_starts_a_process(path):
    assert path.exists(), "the instrument is pointed at the wrong file"
    sources = [
        p for p in sorted(path.parent.glob("*.py")) if not p.name.startswith("test_")
    ]
    spawners = sorted(
        f"{p.name}:{fn.name}"
        for p in sources
        for fn in _anthus_functions(p)
        if _calls(fn, _SPAWN_CALLS)
    )
    assert spawners == [f"{path.name}:_spawn_claude_skill"], (
        f"a new {path.name} function starts a process; decide whether it "
        f"launches issue/PR work and gate it: {spawners}"
    )


@pytest.mark.parametrize("path", _TRIAGE_LANES)
def test_triage_lane_spawn_checks_gate_before_launch(path):
    fns = {fn.name: fn for fn in _anthus_functions(path)}
    spawn = fns["_spawn_claude_skill"]
    gate = _calls(spawn, {"_label_gate_allows"})
    assert gate, f"{path.name}: _spawn_claude_skill launches with no label gate"
    launches = _calls(spawn, _SPAWN_CALLS)
    assert launches and min(n.lineno for n in gate) < min(
        n.lineno for n in launches
    ), f"{path.name}: the label gate must run before the process launch"


@pytest.mark.parametrize("path", _TRIAGE_LANES)
def test_triage_lane_handle_event_gates_every_dispatch_branch(path):
    """Each `handle_event` branch that dispatches work consults the gate before
    it notifies or dispatches — so a gated item neither pages nor launches."""
    fns = {fn.name: fn for fn in _anthus_functions(path)}
    handler = fns["handle_event"]
    branches = 0
    for node in ast.walk(handler):
        if not isinstance(node, ast.If):
            continue
        body_calls = [
            n
            for stmt in node.body
            for n in ast.walk(stmt)
            if isinstance(n, ast.Call)
        ]
        sinks = [n for n in body_calls if _call_name(n) in _TRIAGE_SINKS]
        if not sinks:
            continue
        branches += 1
        gate = [n for n in body_calls if _call_name(n) == "_label_gate_allows"]
        assert gate, f"{path.name}: a dispatch branch at line {node.lineno} is ungated"
        first_gate = min(n.lineno for n in gate)
        guarded = sinks + [n for n in body_calls if _call_name(n) == "send"]
        assert first_gate < min(n.lineno for n in guarded), (
            f"{path.name}: line {node.lineno}: gate must run before notify/dispatch"
        )
    assert branches == 2, (
        f"{path.name}: expected the issue and pull_request dispatch branches, "
        f"found {branches}; classify any new one"
    )
    # Only handle_event (and, in Formica, its dispatch_* helpers) reach a sink.
    callers = sorted(
        fn.name
        for fn in fns.values()
        if _calls(fn, _TRIAGE_SINKS) and fn.name not in _TRIAGE_SINKS
    )
    assert callers == ["handle_event"], callers


@pytest.mark.parametrize("path", _TRIAGE_LANES)
def test_triage_lane_gate_reads_the_shared_module(path):
    fns = {fn.name: fn for fn in _anthus_functions(path)}
    assert "_label_gate_allows" in fns, f"{path.name} has no label gate"
    gate = fns["_label_gate_allows"]
    assert _calls(gate, {"required_label"}), (
        f"{path.name} must read ATELES_SWARM_REQUIRE_LABEL through "
        "lib/daemon_runtime/label_gate.required_label, like Apis and Anthus"
    )
    assert _calls(gate, {"carries_label"})
    src = path.read_text()
    assert "from lib.daemon_runtime import label_gate" in src
    assert 'os.environ.get("ATELES_SWARM_REQUIRE_LABEL"' not in src
