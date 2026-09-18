"""The empty pre-impl sequence must never read as green.

Why this file exists, and what it looked like RED
--------------------------------------------------

`_gates_green` decides whether an agent starts writing code. It asks "is any
pre-impl gate unsigned?" over the sequence the workflow declares. That question
is VACUOUSLY FALSE over an empty sequence — no gate is unsigned when there are
no gates — so the less gate data survived the read, the greener the issue read.
The failure is monotone in the wrong direction: total data loss produced maximum
confidence.

Four distinct inputs collapse to an empty sequence, and each is tested here
against the resolver rather than against a mock of it:

  1. a `gates` field that is missing, empty, or unparseable
  2. an ``impl`` gate stored as ``"Impl"`` — no name equals ``"impl"``, so no
     impl phase is found, so nothing is "before" it
  3. an ``impl`` gate written with unicode lookalikes (fullwidth, mathematical
     Latin) — same mechanism
  4. every gate declared in the same phase as ``impl``, so none is strictly
     earlier

These tests were written against the PRE-FIX code first and confirmed RED. On
`1924191d` (the reviewed commit), with `require_pre_impl_gates` absent and
`_gates_green` calling `resolve_pre_impl_gates`:

  * `test_capitalised_impl_does_not_empty_the_sequence` FAILED — the resolver
    returned ``()`` for a workflow whose gates were ``PM, UX, Impl``, because
    ``"Impl" != "impl"``.
  * `test_unicode_lookalike_impl_does_not_empty_the_sequence` FAILED
    identically for ``"ｉｍｐｌ"`` and ``"𝗂𝗆𝗉𝗅"``.
  * `test_empty_sequence_is_refused_not_returned_green` FAILED with
    `AttributeError: module has no attribute 'require_pre_impl_gates'`; with
    `resolve_pre_impl_gates` substituted it returned ``()`` — the vacuous pass
    itself.
  * `test_same_phase_as_impl_is_not_pre_impl_and_is_refused` FAILED — ``()``
    returned and accepted.
  * `test_release_workflow_may_legitimately_have_none` PASSED before and after:
    it pins the ONE case that must stay permitted, so the fix cannot be "refuse
    everything".

Run: pytest lib/daemon_runtime/test_gates_fail_closed.py -v
"""

from __future__ import annotations

import pytest

from lib.daemon_runtime import workflow_resolver as wr
from lib.daemon_runtime.workflow_resolver import (
    NO_PRE_IMPL_WORKFLOW_TYPES,
    ResolvedWorkflow,
    WorkflowUnresolvedError,
    require_pre_impl_gates,
    validate_gates,
)
from lib.gate_names import IMPL_GATE_NAME, normalize_gate_name

# The two homoglyph families NFKC actually folds. Spelled as escapes so the
# test file itself cannot be "fixed" by an editor silently normalizing it.
FULLWIDTH_IMPL = "ｉｍｐｌ"           # ｉｍｐｌ
MATHEMATICAL_IMPL = "\U0001d422\U0001d426\U0001d429\U0001d425"  # 𝗂𝗆𝗉𝗅


def _gate(phase: int, name: str, owner: str = "someone") -> dict:
    return {"phase": phase, "gate_name": name, "owner_agent": owner, "required": True}


def _wf(workflow_type: str, gates: list[dict]) -> ResolvedWorkflow:
    return ResolvedWorkflow(
        entity_id=f"ent_{workflow_type}",
        project="ateles",
        workflow_type=workflow_type,
        gates=validate_gates(gates, entity_id=f"ent_{workflow_type}"),
    )


def _fetcher(workflows: list[ResolvedWorkflow]):
    """A fetcher returning a fixed list, so no test touches the network."""
    return lambda project: list(workflows)


@pytest.fixture(autouse=True)
def _clean_cache():
    wr.clear_cache()
    yield
    wr.clear_cache()


# ── the normalizer, on the exact inputs the review named ─────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "impl",
        "Impl",
        "IMPL",
        "  impl  ",
        " impl ",          # non-breaking space from a copy-paste
        FULLWIDTH_IMPL,
        MATHEMATICAL_IMPL,
        "Ｉｍｐｌ",   # fullwidth with a capital I
    ],
    ids=[
        "canonical", "capitalised", "upper", "ascii-space",
        "nbsp", "fullwidth", "mathematical", "fullwidth-capital",
    ],
)
def test_every_impl_spelling_reduces_to_the_same_gate(raw):
    """Each of these named a DIFFERENT gate before normalization."""
    assert normalize_gate_name(raw) == IMPL_GATE_NAME


def test_absence_has_exactly_one_spelling():
    """The SENTINEL_ASSIGNEES shape: every absent form reduces to ``""``."""
    assert {normalize_gate_name(v) for v in (None, "", "   ", " ")} == {""}


def test_normalizer_does_not_invent_a_gate():
    """Form only. An unknown name reduces but is never mapped onto a known gate."""
    assert normalize_gate_name("  Copy-Review ") == "copy-review"
    # Cyrillic 'і' is deliberately NOT folded — it stays unmatched and fails
    # closed rather than being guessed into Latin.
    assert normalize_gate_name("іmpl") != IMPL_GATE_NAME


# ── the four inputs that empty the sequence ──────────────────────────────────


def test_capitalised_impl_does_not_empty_the_sequence():
    """RED before the fix: returned ``()``, so pm and ux vanished."""
    wf = _wf("feature", [_gate(1, "pm"), _gate(2, "UX"), _gate(3, "Impl")])
    assert wf.pre_impl_gate_names() == ("pm", "ux")


@pytest.mark.parametrize("spelling", [FULLWIDTH_IMPL, MATHEMATICAL_IMPL])
def test_unicode_lookalike_impl_does_not_empty_the_sequence(spelling):
    """RED before the fix: no name equalled ``impl``, so nothing preceded it."""
    wf = _wf("feature", [_gate(1, "pm"), _gate(2, "arch"), _gate(3, spelling)])
    assert wf.pre_impl_gate_names() == ("pm", "arch")


def test_empty_sequence_is_refused_not_returned_green():
    """A feature workflow resolving to zero pre-impl gates is a FAILED READ.

    RED before the fix: `resolve_pre_impl_gates` returned ``()`` and
    `_gates_green` read it as "nothing unsigned".
    """
    wf = _wf("feature", [_gate(1, "impl"), _gate(2, "pr_review")])
    assert wf.pre_impl_gate_names() == ()
    with pytest.raises(WorkflowUnresolvedError) as exc:
        require_pre_impl_gates(
            "markmhendrickson/ateles", ["feature"], fetcher=_fetcher([wf])
        )
    assert "not one that may have none" in exc.value.reason


def test_same_phase_as_impl_is_not_pre_impl_and_is_refused():
    """Gates sharing impl's phase are not strictly earlier — the review's 4th case."""
    wf = _wf("feature", [_gate(1, "pm"), _gate(1, "impl")])
    assert wf.pre_impl_gate_names() == ()
    with pytest.raises(WorkflowUnresolvedError):
        require_pre_impl_gates(
            "markmhendrickson/ateles", ["feature"], fetcher=_fetcher([wf])
        )


@pytest.mark.parametrize(
    "raw_gates", [[], [{}], [{"gate_name": "", "phase": 1}]],
    ids=["no-gates", "empty-dict", "blank-name"],
)
def test_a_gates_field_that_did_not_survive_the_read_is_refused(raw_gates):
    with pytest.raises(WorkflowUnresolvedError):
        validate_gates(raw_gates, entity_id="ent_x")


# ── the one case that must STAY permitted ────────────────────────────────────


def test_release_workflow_may_legitimately_have_none():
    """PASSED before and after — the fix must not be "refuse everything".

    `ateles|release` declares no `impl` gate, so nothing can precede one. That
    is a real property of the workflow, not a failed read.
    """
    wf = _wf("release", [_gate(1, "pm"), _gate(2, "pr_review")])
    assert wf.permits_no_pre_impl_gates() is True
    assert require_pre_impl_gates(
        "markmhendrickson/ateles", ["release"], fetcher=_fetcher([wf])
    ) == ()


def test_allowlist_does_not_excuse_a_release_that_grew_an_impl_gate():
    """An allowlisted TYPE is not a blanket pass.

    A release workflow carrying an `impl` gate whose pre-impl gates then
    resolve empty is a failed read wearing a permitted name.
    """
    wf = _wf("release", [_gate(1, "impl"), _gate(2, "pr_review")])
    assert wf.permits_no_pre_impl_gates() is False
    with pytest.raises(WorkflowUnresolvedError):
        require_pre_impl_gates(
            "markmhendrickson/ateles", ["release"], fetcher=_fetcher([wf])
        )


def test_a_non_allowlisted_type_is_never_permitted_empty():
    """Per-type, so a failure names WHICH type gained a silent pass (#1049 shape)."""
    for wtype in ("feature", "bug", "security", "copy"):
        assert wtype not in NO_PRE_IMPL_WORKFLOW_TYPES
        wf = _wf(wtype, [_gate(1, "impl")])
        assert wf.permits_no_pre_impl_gates() is False


# ── proof this file is a gate and not a tautology (#1049's move) ─────────────


def test_the_check_can_actually_fail():
    """Simulates the fail-open being reintroduced and asserts we would catch it.

    Without this, every assertion above could be satisfied by a
    `require_pre_impl_gates` that raises unconditionally, or by a normalizer
    that maps everything to ``impl``. This pins the DISCRIMINATION, not just
    the outcome.
    """
    # A normalizer that folded too aggressively would break this:
    assert normalize_gate_name("pm") != normalize_gate_name("impl")

    # A `require_` that raised unconditionally would break this:
    good = _wf("feature", [_gate(1, "pm"), _gate(2, "impl")])
    assert require_pre_impl_gates(
        "markmhendrickson/ateles", ["feature"], fetcher=_fetcher([good])
    ) == ("pm",)

    # The TTL cache is keyed by project and both resolutions above and below
    # use the same one, so without this the second fetcher never runs and the
    # good result is asserted twice. Clearing it is what makes the two halves
    # independent — the cache behaviour itself is correct and covered in
    # test_workflow_resolver.py.
    wr.clear_cache()

    # And the vacuous case must still refuse, in the same run.
    bad = _wf("feature", [_gate(1, "impl")])
    with pytest.raises(WorkflowUnresolvedError):
        require_pre_impl_gates(
            "markmhendrickson/ateles", ["feature"], fetcher=_fetcher([bad])
        )
