"""
Auto-build must not dispatch onto unsigned gates (ateles#460).

## The failure

`_gates_green` read only Lanius's stdout: "green" meant Lanius did not print
`GATE_INHERITANCE: blocked`. That is a report ABOUT gate state, not gate state.
The two diverge whenever an agent turn ends without an explicit block while a
gate is still `pending`.

That was tolerable while `ATELES_SWARM_AUTO_BUILD` was off — the only
consequence was a notification. With it on, it decides whether an agent starts
writing code. On the first two handoffs after enabling it, Cicada was dispatched
onto issues whose live `gate_status` showed `arch: pending` (and once `ux` too).
Cicada refused both times:

    Apis auto-build dispatch claimed green pre-impl gates while live
    gate_status still shows arch/ux pending and owner waxwing

Two occurrences, two repos, two different gate combinations — so not a race.
The dispatcher was measuring something else and calling it gate state.

Run: pytest execution/daemons/apis/test_gates_green_reads_entity.py -v
"""

from __future__ import annotations

import pytest

import swarm_dispatch as sd


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, msg: str, priority=None, handler=None) -> None:  # noqa: ANN001
        self.sent.append(msg)


class _Ok:
    ok = True
    stdout = "Triage complete."
    error = None
    returncode = 0


class _Blocked:
    ok = True
    stdout = "GATE_INHERITANCE: blocked"
    error = None
    returncode = 0


class _Failed:
    ok = False
    stdout = ""
    error = "boom"
    returncode = 1


def _dispatcher() -> sd.SwarmDispatcher:
    return sd.SwarmDispatcher(notifier=_Notifier())


def _stub_gate_status(monkeypatch, gate_status: dict | None, found: bool = True):
    class _State:
        def __init__(self) -> None:
            self.found = found
            self.gate_status = gate_status or {}

    async def fake_load(self, repo, issue_number):  # noqa: ANN001
        return _State()

    monkeypatch.setattr(sd.IssueGateStore, "load", fake_load)


@pytest.mark.asyncio
async def test_the_production_failure_pending_arch_with_silent_lanius(monkeypatch):
    """THE test — the exact shape observed on both real handoffs.

    Lanius reports no block; the record says arch is pending. Old code returned
    True here and dispatched Cicada onto an unsigned gate.
    """
    d = _dispatcher()
    _stub_gate_status(monkeypatch, {"pm": "signed_off", "ux": "not_required",
                                    "arch": "pending"})

    assert await d._gates_green(_Ok(), "o/r", 2186) is False


@pytest.mark.asyncio
async def test_two_pending_gates_also_blocked(monkeypatch):
    """The second occurrence had arch AND ux pending."""
    d = _dispatcher()
    _stub_gate_status(monkeypatch, {"pm": "signed_off", "ux": "pending",
                                    "arch": "pending"})

    assert await d._gates_green(_Ok(), "o/r", 459) is False


@pytest.mark.asyncio
async def test_all_cleared_is_green(monkeypatch):
    d = _dispatcher()
    _stub_gate_status(monkeypatch, {"pm": "signed_off", "ux": "not_required",
                                    "arch": "waived"})

    assert await d._gates_green(_Ok(), "o/r", 1) is True


@pytest.mark.asyncio
async def test_not_applicable_counts_as_cleared(monkeypatch):
    """`not_applicable` appears in live data alongside `not_required`.

    It was absent from CLEARED_GATE_STATES, so a legitimately-cleared gate read
    as uncleared — a false negative that would stall a ready issue forever.
    """
    d = _dispatcher()
    _stub_gate_status(monkeypatch, {"pm": "signed_off", "ux": "not_applicable",
                                    "arch": "not_applicable"})

    assert await d._gates_green(_Ok(), "o/r", 430) is True


@pytest.mark.asyncio
async def test_missing_gate_key_is_treated_as_pending(monkeypatch):
    """An absent key is not evidence of clearance."""
    d = _dispatcher()
    _stub_gate_status(monkeypatch, {"pm": "signed_off"})  # ux/arch absent

    assert await d._gates_green(_Ok(), "o/r", 1) is False


@pytest.mark.asyncio
async def test_explicit_lanius_block_still_vetoes(monkeypatch):
    """The stdout verdict is kept as a veto, even when the record looks clear."""
    d = _dispatcher()
    _stub_gate_status(monkeypatch, {"pm": "signed_off", "ux": "not_required",
                                    "arch": "signed_off"})

    assert await d._gates_green(_Blocked(), "o/r", 1) is False


@pytest.mark.asyncio
async def test_lanius_failure_is_not_green(monkeypatch):
    d = _dispatcher()
    _stub_gate_status(monkeypatch, {"pm": "signed_off", "ux": "not_required",
                                    "arch": "signed_off"})

    assert await d._gates_green(_Failed(), "o/r", 1) is False


@pytest.mark.asyncio
async def test_missing_entity_fails_closed(monkeypatch):
    """No entity means no gate_status to check — do not build."""
    d = _dispatcher()
    _stub_gate_status(monkeypatch, None, found=False)

    assert await d._gates_green(_Ok(), "o/r", 1) is False


@pytest.mark.asyncio
async def test_unreadable_entity_fails_closed(monkeypatch):
    """Declining costs a delay; building on unsigned gates costs a stray PR."""
    d = _dispatcher()

    async def boom(self, repo, issue_number):  # noqa: ANN001
        raise RuntimeError("neotoma unreachable")

    monkeypatch.setattr(sd.IssueGateStore, "load", boom)

    assert await d._gates_green(_Ok(), "o/r", 1) is False


# ── the vacuous-empty fail-open (PR #719 security review) ────────────────────
#
# Everything above asserts that a NAMED pre-impl gate left pending blocks the
# handoff. None of it could catch the sequence being EMPTY, because "no gate is
# unsigned" is vacuously true over an empty list — the check passed hardest
# precisely when the least gate data survived the read.
#
# These drive `_gates_green` end to end, through the real resolver, with a
# workflow_definition shaped like each of the four inputs the review named.


def _serve_workflow(monkeypatch, workflow_type: str, gates: list[dict]):
    """Replace the conftest `ateles|feature` default with a specific workflow."""
    from lib.daemon_runtime import workflow_resolver as _wr

    def _fetch(project: str):
        return [
            _wr.ResolvedWorkflow(
                entity_id="ent_vacuity",
                project=project,
                workflow_type=workflow_type,
                gates=_wr.validate_gates(gates, entity_id="ent_vacuity"),
            )
        ]

    monkeypatch.setattr(_wr, "_fetch_definitions", _fetch)
    _wr.clear_cache()


def _g(phase: int, name: str) -> dict:
    return {"phase": phase, "gate_name": name, "owner_agent": "x", "required": True}


@pytest.mark.asyncio
async def test_capitalised_impl_does_not_green_the_handoff(monkeypatch):
    """`Impl` matched no impl phase, so pm/ux vanished and this returned True."""
    _serve_workflow(monkeypatch, "feature",
                    [_g(1, "pm"), _g(2, "ux"), _g(3, "Impl")])
    _stub_gate_status(monkeypatch, {"pm": "pending", "ux": "pending"})
    d = _dispatcher()

    assert await d._gates_green(_Ok(), "o/r", 719) is False


@pytest.mark.asyncio
async def test_unicode_lookalike_impl_does_not_green_the_handoff(monkeypatch):
    """Fullwidth `ｉｍｐｌ` — same mechanism, different character range."""
    _serve_workflow(monkeypatch, "feature",
                    [_g(1, "pm"), _g(2, "arch"), _g(3, "ｉｍｐｌ")])
    _stub_gate_status(monkeypatch, {"pm": "pending", "arch": "pending"})
    d = _dispatcher()

    assert await d._gates_green(_Ok(), "o/r", 719) is False


@pytest.mark.asyncio
async def test_no_pre_impl_gates_on_a_feature_workflow_fails_closed(monkeypatch):
    """Zero pre-impl gates on a workflow allowed none is a FAILED READ."""
    _serve_workflow(monkeypatch, "feature", [_g(1, "impl"), _g(2, "pr_review")])
    _stub_gate_status(monkeypatch, {})
    d = _dispatcher()

    assert await d._gates_green(_Ok(), "o/r", 719) is False


@pytest.mark.asyncio
async def test_gate_in_the_same_phase_as_impl_fails_closed(monkeypatch):
    """Nothing is strictly earlier, so the sequence empties — the 4th input."""
    _serve_workflow(monkeypatch, "feature", [_g(1, "pm"), _g(1, "impl")])
    _stub_gate_status(monkeypatch, {"pm": "pending"})
    d = _dispatcher()

    assert await d._gates_green(_Ok(), "o/r", 719) is False


@pytest.mark.asyncio
async def test_capitalised_gate_status_key_still_counts_as_signed(monkeypatch):
    """Normalizing must not invent a BLOCK either.

    A gate recorded as "PM"/"UX"/"ARCH" is the signature the workflow's
    lowercase names asked for. Without normalizing the lookup this returned
    False forever on an issue that was fully signed — the opposite error, and
    the reason the fix normalizes both sides rather than only the declaration.
    """
    _serve_workflow(monkeypatch, "feature",
                    [_g(1, "pm"), _g(2, "ux"), _g(3, "impl")])
    _stub_gate_status(monkeypatch, {"PM": "signed_off", "UX": "waived"})
    d = _dispatcher()

    assert await d._gates_green(_Ok(), "o/r", 719) is True


@pytest.mark.asyncio
async def test_a_release_workflow_may_have_no_pre_impl_gates(monkeypatch):
    """The one permitted empty — proves the fix is not "refuse everything"."""
    _serve_workflow(monkeypatch, "release", [_g(1, "pm"), _g(2, "pr_review")])
    _stub_gate_status(monkeypatch, {})
    d = _dispatcher()

    # The `release` label is required: `select_workflow` falls through to the
    # `feature` default otherwise, and a release definition under a feature
    # request is itself an unresolved workflow.
    assert await d._gates_green(_Ok(), "o/r", 719, labels=["release"]) is True
