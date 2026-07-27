"""Tests for the gate/phase -> GitHub label projection (ateles#259 item 4).

The projection is a pure function precisely so it can be tested without any
GitHub or Neotoma I/O; the reconciler that consumes it stays a thin transport
shim. These tests pin the contract that shim depends on.
"""

from __future__ import annotations

from issue_labels import (
    BLOCKED_GATES_LABEL,
    GateLabel,
    PhaseLabel,
    is_managed_label,
    labels_for_gate_status,
    reconcile_labels,
)

# ── labels_for_gate_status: cleared gates -> cumulative trail ────────────────


def test_signed_off_gate_yields_its_signed_label():
    out = labels_for_gate_status({"pm": "signed_off"})
    assert GateLabel.PM.value in out


def test_waived_gate_counts_as_cleared():
    """A waived gate is no longer holding the issue up, so it earns the same
    label as a signed_off one — HOW it cleared lives in owner_history."""
    out = labels_for_gate_status({"arch": "waived"})
    assert GateLabel.ARCH.value in out


def test_pending_gate_yields_no_signed_label():
    out = labels_for_gate_status({"qa": "pending"})
    assert GateLabel.QA.value not in out


def test_not_required_yields_no_label_at_all():
    """Absence is the signal — emitting a label per gate per state would put
    ~28 labels on an issue."""
    out = labels_for_gate_status({"legal": "not_required"})
    assert GateLabel.LEGAL.value not in out
    assert out == set()


def test_multiple_cleared_gates_accumulate():
    out = labels_for_gate_status(
        {"pm": "signed_off", "arch": "waived", "qa": "pending"}
    )
    assert GateLabel.PM.value in out
    assert GateLabel.ARCH.value in out
    assert GateLabel.QA.value not in out


def test_gate_state_is_case_and_whitespace_insensitive():
    out = labels_for_gate_status({"pm": "  Signed_Off "})
    assert GateLabel.PM.value in out


def test_non_string_state_is_ignored_not_crashed():
    """A malformed state must not crash the projection, must not earn a
    `-signed` label, and must FAIL SAFE: an unparseable pre-impl gate counts as
    'not cleared' (blocked), never as silently clear."""
    out = labels_for_gate_status({"pm": None, "arch": 3, "qa": "signed_off"})  # type: ignore[dict-item]
    assert GateLabel.QA.value in out
    assert GateLabel.PM.value not in out
    assert GateLabel.ARCH.value not in out
    # pm is unparseable -> treated as still holding the issue up.
    assert BLOCKED_GATES_LABEL in out


# ── blocked/gates: derived from PRE-IMPL gates only ─────────────────────────


def test_pending_pre_impl_gate_sets_blocked_flag():
    assert BLOCKED_GATES_LABEL in labels_for_gate_status({"pm": "pending"})


def test_blocked_state_also_sets_blocked_flag():
    assert BLOCKED_GATES_LABEL in labels_for_gate_status({"arch": "blocked"})


def test_all_pre_impl_gates_cleared_drops_blocked_flag():
    out = labels_for_gate_status({"pm": "signed_off", "arch": "waived"})
    assert BLOCKED_GATES_LABEL not in out


def test_pending_NON_pre_impl_gate_does_not_block():
    """qa/legal pending must not flag the issue as gate-blocked — only the
    pre-impl gates (pm, arch) gate implementation."""
    out = labels_for_gate_status(
        {"pm": "signed_off", "arch": "signed_off", "qa": "pending"}
    )
    assert BLOCKED_GATES_LABEL not in out


def test_not_required_pre_impl_gate_does_not_block():
    out = labels_for_gate_status({"pm": "not_required", "arch": "signed_off"})
    assert BLOCKED_GATES_LABEL not in out


# ── phase/: exactly one, from current_owner ─────────────────────────────────


def test_current_owner_by_gate_name_yields_phase():
    out = labels_for_gate_status({}, current_owner="arch")
    assert PhaseLabel.ARCH.value in out


def test_current_owner_by_AGENT_name_resolves_to_same_phase():
    """The swarm writes current_owner as either the gate ("arch") or the agent
    that owns it ("waxwing"); both must project identically."""
    assert labels_for_gate_status({}, current_owner="waxwing") == labels_for_gate_status(
        {}, current_owner="arch"
    )


def test_phase_labels_are_mutually_exclusive():
    out = labels_for_gate_status({"pm": "signed_off"}, current_owner="cicada")
    phases = {lbl for lbl in out if lbl.startswith("phase/")}
    assert phases == {PhaseLabel.IMPL.value}, f"expected exactly one phase, got {phases}"


def test_unknown_owner_yields_no_phase_label():
    out = labels_for_gate_status({}, current_owner="nobody")
    assert not any(lbl.startswith("phase/") for lbl in out)


def test_absent_owner_yields_no_phase_label():
    out = labels_for_gate_status({"pm": "signed_off"}, current_owner=None)
    assert not any(lbl.startswith("phase/") for lbl in out)


# ── degenerate input ────────────────────────────────────────────────────────


def test_none_gate_status_is_safe():
    assert labels_for_gate_status(None) == set()


def test_empty_everything_is_empty():
    assert labels_for_gate_status({}, current_owner=None) == set()


# ── reconcile_labels: never clobber unmanaged labels ────────────────────────


def test_reconcile_preserves_unmanaged_labels():
    """type/, priority/, lanius-triage and human labels must survive a sync."""
    out = reconcile_labels(
        current=["type/bug", "priority/p1", "lanius-triage", "phase/pm"],
        desired={PhaseLabel.ARCH.value},
    )
    assert "type/bug" in out
    assert "priority/p1" in out
    assert "lanius-triage" in out


def test_reconcile_drops_stale_managed_labels():
    """The old phase must go when the issue advances, or every phase it ever
    passed through accumulates."""
    out = reconcile_labels(
        current=["phase/pm", "gate/pm-signed"],
        desired={PhaseLabel.ARCH.value, GateLabel.PM.value},
    )
    assert PhaseLabel.PM.value not in out
    assert PhaseLabel.ARCH.value in out
    assert GateLabel.PM.value in out


def test_reconcile_is_idempotent():
    """A sync that runs twice must be a no-op the second time — this is what
    keeps the reconciler from churning labels on every gate transition."""
    desired = {PhaseLabel.ARCH.value, GateLabel.PM.value}
    once = reconcile_labels(["type/bug"], desired)
    twice = reconcile_labels(list(once), desired)
    assert once == twice


def test_reconcile_removes_blocked_flag_when_cleared():
    out = reconcile_labels(
        current=[BLOCKED_GATES_LABEL, "type/feature"],
        desired={GateLabel.PM.value},
    )
    assert BLOCKED_GATES_LABEL not in out
    assert "type/feature" in out


# ── is_managed_label ────────────────────────────────────────────────────────


def test_managed_prefixes_recognised():
    assert is_managed_label("phase/qa")
    assert is_managed_label("gate/pm-signed")
    assert is_managed_label(BLOCKED_GATES_LABEL)


def test_unmanaged_labels_not_claimed():
    for lbl in ("type/bug", "priority/p0", "lanius-triage", "good first issue"):
        assert not is_managed_label(lbl), f"{lbl} must not be managed"


# ── end-to-end shape on a realistic issue ───────────────────────────────────


def test_realistic_mid_pipeline_issue_projects_expected_set():
    """ateles#241-shaped: pm signed, arch waived by operator, impl next."""
    out = labels_for_gate_status(
        {
            "pm": "signed_off",
            "ux": "pending",
            "arch": "waived",
            "impl": "pending",
            "pr_review": "pending",
            "qa": "pending",
            "legal": "not_required",
        },
        current_owner="cicada",
    )
    assert out == {
        GateLabel.PM.value,
        GateLabel.ARCH.value,
        PhaseLabel.IMPL.value,
    }, out
