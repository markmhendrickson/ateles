"""
An absent gate key is not `pending` — it is the workflow's own declaration
(ateles#1213).

## The failure

`_gates_green` decided build handoff with:

    (state.gate_status.get(gate) or "pending").strip().lower()
    not in CLEARED_GATE_STATES

over `PRE_IMPL_GATES = ("pm", "ux", "arch")`. When Lanius fast-paths a
`workflow_type: bug` issue it writes no `ux` or `arch` key at all, because the
`ateles|bug` workflow does not declare those gates. `.get(gate) or "pending"`
turned that ABSENCE into `pending`, which is not in `CLEARED_GATE_STATES`, so
handoff was refused forever: nothing can sign a gate that does not exist.

Verified live on ateles#1209 (`ent_410f446b8daac84da333836c`), whose
`gate_status` is exactly

    {"pm": "signed_off", "impl": "pending", "pr_review": "pending",
     "qa": "pending", "release": "pending"}

with `workflow_type: bug` and `workflow_definition_id:
ent_1b6d0acbdc436d3f0dad5a0d` — the `ateles|bug` workflow, whose declared
gates are `pm, impl, pr_review, qa, release`, key-for-key what the issue holds.
18 distinct ateles issues logged "not handing off to build" on 2026-09-23.

## What must NOT be fixed by this

Treating every absence as cleared turns a stuck pipeline into an unguarded one.
The two cases are distinguished by the issue's own workflow, and the
`test_declared_but_absent_gate_still_refuses` / `test_unknown_workflow_*` cases
below are what hold that line: they FAIL against a blanket "absent ⇒ cleared".

Run: pytest execution/daemons/apis/test_absent_gate_is_not_pending.py -v
"""

from __future__ import annotations

import pytest

import swarm_dispatch as sd
from gate_waive import gates_needing_waive, uncleared_gates


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, msg: str, priority=None, handler=None, **kwargs) -> None:  # noqa: ANN001
        self.sent.append(msg)

    def clear_dedupe(self, key: str) -> None:
        return None


class _Ok:
    ok = True
    stdout = "Triage complete."
    error = None
    returncode = 0


# The `ateles|bug` workflow as it lives in prod today
# (ent_1b6d0acbdc436d3f0dad5a0d): no `ux`, no `arch`. `gates` is stored as a
# JSON-encoded STRING on this row, which is the shape the resolver must decode.
_BUG_WORKFLOW_GATES_JSON = (
    '[{"phase":1,"gate_name":"pm","owner_agent":"pavo","required":true},'
    '{"phase":3,"gate_name":"impl","owner_agent":"cicada","required":true},'
    '{"phase":4,"gate_name":"pr_review","owner_agent":"vanellus","required":true},'
    '{"phase":4,"gate_name":"qa","owner_agent":"phoenicurus","required":true},'
    '{"phase":5,"gate_name":"release","owner_agent":"struthio","required":true}]'
)

# The `ateles|feature` workflow: declares `ux` AND `arch`.
_FEATURE_WORKFLOW_GATES = [
    {"phase": 1, "gate_name": "pm", "owner_agent": "pavo", "required": True},
    {"phase": 2, "gate_name": "ux", "owner_agent": "accipiter", "required": True},
    {"phase": 2, "gate_name": "arch", "owner_agent": "waxwing", "required": True},
    {"phase": 3, "gate_name": "impl", "owner_agent": "cicada", "required": True},
]


def _dispatcher() -> sd.SwarmDispatcher:
    return sd.SwarmDispatcher(notifier=_Notifier())


def _stub(
    monkeypatch,
    gate_status: dict,
    *,
    workflow_type: str = "",
    workflow_definition_id: str = "",
    workflow_rows: list | None = None,
    workflow_read_raises: bool = False,
):
    """Stub the issue read AND the workflow read the resolver depends on."""

    class _State:
        def __init__(self) -> None:
            self.found = True
            self.read_failed = False
            self.gate_status = dict(gate_status)
            self.repo = "markmhendrickson/ateles"
            self.workflow_type = workflow_type
            self.workflow_definition_id = workflow_definition_id

    async def fake_load(self, repo, issue_number):  # noqa: ANN001
        return _State()

    async def fake_all_proven(self, state, owners):  # noqa: ANN001
        # Every `signed_off` here is backed by its owner's own signed write;
        # the provenance re-proof is covered in test_gate_sign_off_residuals.
        return set()

    async def fake_post(self, path, payload):  # noqa: ANN001
        if workflow_read_raises:
            raise RuntimeError("neotoma unreachable")
        return {"entities": list(workflow_rows or [])}

    monkeypatch.setattr(sd.IssueGateStore, "load", fake_load)
    monkeypatch.setattr(
        sd.IssueGateStore,
        "unverified_signed_off_gates",
        fake_all_proven,
        raising=False,
    )
    monkeypatch.setattr(sd.IssueSpecStore, "_post", fake_post)


# ── THE test: the exact live shape of ateles#1209 ────────────────────────────


@pytest.mark.asyncio
async def test_bug_workflow_with_absent_ux_and_arch_hands_off(monkeypatch):
    """THE test — ateles#1209's gate_status, byte for byte.

    RED against the old predicate: `.get("ux") or "pending"` → `"pending"`,
    which is not cleared, so this returned False and the issue stalled.
    """
    d = _dispatcher()
    _stub(
        monkeypatch,
        {
            "pm": "signed_off",
            "impl": "pending",
            "pr_review": "pending",
            "qa": "pending",
            "release": "pending",
        },
        workflow_type="bug",
        workflow_definition_id="ent_1b6d0acbdc436d3f0dad5a0d",
        workflow_rows=[
            {
                "entity_id": "ent_1b6d0acbdc436d3f0dad5a0d",
                "canonical_name": "workflow_definition:ateles|bug",
                "snapshot": {"gates": _BUG_WORKFLOW_GATES_JSON},
            }
        ],
    )

    assert await d._gates_green(_Ok(), "markmhendrickson/ateles", 1209) is True


@pytest.mark.asyncio
async def test_bug_workflow_resolved_by_type_when_id_is_absent(monkeypatch):
    """Older issue rows carry `workflow_type` but no `workflow_definition_id`."""
    d = _dispatcher()
    _stub(
        monkeypatch,
        {"pm": "signed_off"},
        workflow_type="bug",
        workflow_rows=[
            {
                "entity_id": "ent_1b6d0acbdc436d3f0dad5a0d",
                "canonical_name": "workflow_definition:ateles|bug",
                "snapshot": {"gates": _BUG_WORKFLOW_GATES_JSON},
            }
        ],
    )

    assert await d._gates_green(_Ok(), "markmhendrickson/ateles", 460) is True


# ── The guard: absence must NOT clear a gate the workflow DOES declare ───────


@pytest.mark.asyncio
async def test_declared_but_absent_gate_still_refuses(monkeypatch):
    """`arch` applies to this workflow and is unwritten — that still blocks.

    This is the case a blanket "absent ⇒ cleared" fix would break, turning a
    stalled pipeline into an unguarded one.
    """
    d = _dispatcher()
    _stub(
        monkeypatch,
        {"pm": "signed_off", "ux": "signed_off"},  # `arch` declared but absent
        workflow_type="feature",
        workflow_definition_id="ent_1d20d557828ecd080b654367",
        workflow_rows=[
            {
                "entity_id": "ent_1d20d557828ecd080b654367",
                "canonical_name": "workflow_definition:ateles|feature",
                "snapshot": {"gates": _FEATURE_WORKFLOW_GATES},
            }
        ],
    )

    assert await d._gates_green(_Ok(), "markmhendrickson/ateles", 1) is False


@pytest.mark.asyncio
async def test_declared_and_pending_gate_still_refuses(monkeypatch):
    """The ateles#460 behaviour is untouched: an explicit `pending` blocks."""
    d = _dispatcher()
    _stub(
        monkeypatch,
        {"pm": "signed_off", "ux": "signed_off", "arch": "pending"},
        workflow_type="feature",
        workflow_definition_id="ent_1d20d557828ecd080b654367",
        workflow_rows=[
            {
                "entity_id": "ent_1d20d557828ecd080b654367",
                "canonical_name": "workflow_definition:ateles|feature",
                "snapshot": {"gates": _FEATURE_WORKFLOW_GATES},
            }
        ],
    )

    assert await d._gates_green(_Ok(), "markmhendrickson/ateles", 2) is False


# ── Unknown workflow ⇒ absence stays blocking (fail closed) ──────────────────


@pytest.mark.asyncio
async def test_unknown_workflow_keeps_absent_gate_blocking(monkeypatch):
    """No workflow binding on the issue at all — absence is UNKNOWN, so block."""
    d = _dispatcher()
    _stub(monkeypatch, {"pm": "signed_off"})  # no workflow_type, no id

    assert await d._gates_green(_Ok(), "markmhendrickson/ateles", 3) is False


@pytest.mark.asyncio
async def test_unresolvable_workflow_keeps_absent_gate_blocking(monkeypatch):
    """The issue names a workflow that the query returns no row for."""
    d = _dispatcher()
    _stub(
        monkeypatch,
        {"pm": "signed_off"},
        workflow_type="bug",
        workflow_definition_id="ent_does_not_exist",
        workflow_rows=[],
    )

    assert await d._gates_green(_Ok(), "markmhendrickson/ateles", 4) is False


@pytest.mark.asyncio
async def test_workflow_read_failure_keeps_absent_gate_blocking(monkeypatch):
    """A transport failure reading the workflow must never read as 'clear'."""
    d = _dispatcher()
    _stub(
        monkeypatch,
        {"pm": "signed_off"},
        workflow_type="bug",
        workflow_definition_id="ent_1b6d0acbdc436d3f0dad5a0d",
        workflow_read_raises=True,
    )

    assert await d._gates_green(_Ok(), "markmhendrickson/ateles", 5) is False


@pytest.mark.asyncio
async def test_workflow_declaring_no_readable_gates_blocks(monkeypatch):
    """A malformed `gates` value must not read as 'this workflow has no gates'.

    `frozenset()` would clear EVERY pre-impl gate at once — strictly worse than
    the stall being fixed — so `declared_gate_names` returns None instead.
    """
    d = _dispatcher()
    _stub(
        monkeypatch,
        {"pm": "signed_off"},
        workflow_type="bug",
        workflow_definition_id="ent_broken",
        workflow_rows=[
            {
                "entity_id": "ent_broken",
                "canonical_name": "workflow_definition:ateles|bug",
                "snapshot": {"gates": "not valid json {{{"},
            }
        ],
    )

    assert await d._gates_green(_Ok(), "markmhendrickson/ateles", 6) is False


# ── `declared_gate_names` directly ───────────────────────────────────────────


def test_declared_gate_names_decodes_json_string_rows():
    assert sd.declared_gate_names({"gates": _BUG_WORKFLOW_GATES_JSON}) == frozenset(
        {"pm", "impl", "pr_review", "qa", "release"}
    )


def test_declared_gate_names_decodes_list_rows():
    assert sd.declared_gate_names({"gates": _FEATURE_WORKFLOW_GATES}) == frozenset(
        {"pm", "ux", "arch", "impl"}
    )


@pytest.mark.parametrize(
    "snapshot",
    [
        {},
        {"gates": None},
        {"gates": "not valid json {{{"},
        {"gates": '"a string, not a list"'},
        {"gates": "[]"},
        {"gates": [{"owner_agent": "pavo"}]},  # no gate_name
    ],
)
def test_declared_gate_names_returns_none_not_empty_set(snapshot):
    """UNKNOWN must never be spelled `frozenset()` — that clears every gate."""
    assert sd.declared_gate_names(snapshot) is None


# ── The two predicates derive from ONE source ────────────────────────────────


def test_waive_sweep_and_dispatch_share_one_predicate():
    """`gates_needing_waive` IS `uncleared_gates` with no declared-gate set.

    The drift this asserts against is the defect itself: `gate_waive` compared
    an absent gate as `""` while `swarm_dispatch` compared it as `"pending"`.
    """
    for gate_status in (
        {},
        {"pm": "signed_off"},
        {"pm": "signed_off", "ux": "pending"},
        {"pm": "waived", "ux": "not_required", "arch": "not_applicable"},
        {"pm": "SIGNED_OFF ", "ux": "skipped"},
    ):
        assert gates_needing_waive(gate_status, sd.PRE_IMPL_GATES) == uncleared_gates(
            gate_status, sd.PRE_IMPL_GATES, declared_gates=None
        )


def test_uncleared_gates_absent_key_blocks_without_a_declaration():
    """No declaration ⇒ the waive sweep still targets an absent gate."""
    assert uncleared_gates({"pm": "signed_off"}, ("pm", "ux", "arch")) == ["ux", "arch"]


def test_uncleared_gates_undeclared_gate_clears():
    assert (
        uncleared_gates(
            {"pm": "signed_off"},
            ("pm", "ux", "arch"),
            declared_gates=frozenset({"pm", "impl"}),
        )
        == []
    )


def test_uncleared_gates_declared_absent_gate_blocks():
    assert uncleared_gates(
        {"pm": "signed_off"},
        ("pm", "ux", "arch"),
        declared_gates=frozenset({"pm", "ux", "arch"}),
    ) == ["ux", "arch"]


def test_uncleared_gates_written_pending_blocks_even_when_undeclared():
    """An explicitly WRITTEN state is honoured whatever the workflow says.

    Absence is the only thing the declaration resolves. A gate someone wrote
    `pending` on is a real, live gate state and outranks the declaration —
    otherwise a stale workflow row could clear a gate a lens is mid-review on.
    """
    assert uncleared_gates(
        {"pm": "signed_off", "ux": "pending"},
        ("pm", "ux", "arch"),
        declared_gates=frozenset({"pm"}),
    ) == ["ux"]
