"""Contract tests for task-ascent reporting in session-facing skills.

The live skills are Neotoma entities, not files in this repository.  The
companion checker validates those entities when it has a Neotoma connection;
these tests keep the semantic checks deterministic and credential-free.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "execution" / "scripts"))

from check_planning_spine_skills import contract_errors  # noqa: E402
import sync_skills  # noqa: E402


BASE = """
## Planning spine

Derive the workstream from each task's upward `PART_OF` ascent through its
plan, project, and strategy. Report the planning records being resumed. Treat
zero parents as missing ascent and two parents as duplicate ascent; never hide
either result. A task ascent is the source of truth.
"""

FOUNDATION_MASTER_PLAN_SHAPE = """
Foundation rollout: A, A2, B, B2, B2-prime, C, D, D2, E, F, G, H, I, J,
K, L, M, N, O. Each entry has its own exit gate in the master-plan record.
"""

BOOTSTRAP_TASK = """
The active bootstrap/rules-delivery workstream uses the local label E2. Its
tasks are repair, review, merge, and deploy, with serial and parallel edges.
"""

PHASE_REPORTING_CONTRACT = """
Resolve and display the selected master plan first. Render its canonical phase
names in record order with each phase's exit-gate state. Map every active
workstream to a canonical phase through a structural phase binding. If there is
no phase binding, mark it as a cross-phase prerequisite whose canonical phase
is not structurally derivable; never guess from prose or a task title. A
subordinate workstream label such as E2 is not canonical Phase E. Put
serial/parallel task execution underneath the phase-level view.
"""

WHOLE_SESSION_CONTRACT = """
For a whole-session continuation, bind the exact source session first. Build
the terminal resumable population as a union of the transcript inventory, the
session_digest or workboard, linked tasks and plans, and the terminal handoff.
Complete a source-session coverage ledger with imported, explicitly excluded,
and unresolved dispositions. Report the balance as audited = imported +
excluded + unresolved. Never claim the session is comprehensively resumed
while an omitted row exists.
If exact resolution is not found, stop before plan binding, name checked
sources, offer a stable id or exact path, and use
[COPY: not-found message and recovery hint]. If it is ambiguous, present two
or three stable candidates with timestamp, harness, or repository metadata,
stop before plan binding, and use [COPY: ambiguity prompt]. An exact empty
session emits audited: 0; imported: 0; excluded: 0; unresolved: 0, states no
work was resumed, and uses [COPY: empty-session outcome]. Partly unreadable
evidence remains unresolved: an unreadable source is unknown, never empty;
name it, prohibit domain action, and make a bounded retry before escalation.
"""


@pytest.mark.parametrize(
    ("slug", "extra"),
    [
        (
            "continue-session",
            "Show a planning resume ledger. Queue a newly introduced "
            "workstream until current work is captured, workboarded, and dispatched."
            + WHOLE_SESSION_CONTRACT,
        ),
        (
            "digest",
            "Show a planning spine summary. Queue a newly introduced workstream "
            "until current work is captured, workboarded, and dispatched.",
        ),
        (
            "reconcile-planning",
            "This is retrospective repair, not the real-time source of truth.",
        ),
    ],
)
def test_complete_contract_passes(slug: str, extra: str) -> None:
    content = BASE + extra
    if slug in {"continue-session", "digest"}:
        content += PHASE_REPORTING_CONTRACT
    assert contract_errors(slug, content) == []


@pytest.mark.parametrize("slug", ["continue-session", "digest"])
def test_task_stages_cannot_masquerade_as_master_plan_phases(slug: str) -> None:
    misleading_task_stage_report = """
    ## Phases
    1. Repair
    2. Review
    3. Merge
    4. Deploy
    """
    skill_specific = (
        "Show a planning resume ledger. Queue a newly introduced workstream "
        "until current work is captured, workboarded, and dispatched."
        + WHOLE_SESSION_CONTRACT
        if slug == "continue-session"
        else "Show a planning spine summary. Queue a newly introduced workstream "
        "until current work is captured, workboarded, and dispatched."
    )

    errors = contract_errors(
        slug,
        BASE
        + FOUNDATION_MASTER_PLAN_SHAPE
        + BOOTSTRAP_TASK
        + misleading_task_stage_report
        + skill_specific,
    )

    for required_fragment in (
        "selected master plan first",
        "canonical phase",
        "exit-gate state",
        "structural phase binding",
        "cross-phase prerequisite",
        "serial/parallel",
        "subordinate workstream label",
        "not structurally derivable",
        "task title",
    ):
        assert any(required_fragment in error for error in errors)


@pytest.mark.parametrize(
    "missing_text,expected_fragment",
    [
        ("PART_OF", "PART_OF"),
        ("plan", "plan"),
        ("project", "project"),
        ("strategy", "strategy"),
        ("missing ascent", "missing ascent"),
        ("duplicate ascent", "duplicate ascent"),
    ],
)
def test_shared_contract_fails_on_each_missing_clause(
    missing_text: str, expected_fragment: str
) -> None:
    body = BASE.replace(missing_text, "omitted")
    errors = contract_errors(
        "continue-session",
        body + "Show a planning resume ledger. Queue a newly introduced workstream "
        "until current work is captured, workboarded, and dispatched."
        + WHOLE_SESSION_CONTRACT,
    )
    assert any(expected_fragment in error for error in errors)


def test_resume_requires_the_planning_records_resumed() -> None:
    errors = contract_errors(
        "continue-session",
        BASE + "Queue a newly introduced workstream until current work is captured, "
        "workboarded, and dispatched." + WHOLE_SESSION_CONTRACT,
    )
    assert any("resume ledger" in error for error in errors)


def test_digest_requires_planning_spine_summary() -> None:
    errors = contract_errors(
        "digest",
        BASE + "Queue a newly introduced workstream until current work is captured, "
        "workboarded, and dispatched.",
    )
    assert any("planning spine summary" in error for error in errors)


def test_interactive_skills_require_sequential_admission_gate() -> None:
    for slug in ("continue-session", "digest"):
        errors = contract_errors(slug, BASE)
        assert any("captured, workboarded, and dispatched" in error for error in errors)


def test_pre_fix_single_plan_skill_fails_whole_session_contract() -> None:
    pre_fix = (
        BASE
        + PHASE_REPORTING_CONTRACT
        + "Show a planning resume ledger. Bind exactly one plan before doing anything "
        "else. Queue a newly introduced workstream until current work is captured, "
        "workboarded, and dispatched."
    )

    errors = contract_errors("continue-session", pre_fix)

    assert any("exact source session" in error for error in errors)
    assert any("source-session coverage ledger" in error for error in errors)
    assert any(
        "audited = imported + excluded + unresolved" in error for error in errors
    )


def test_corrected_continue_session_fixture_passes_whole_session_contract() -> None:
    fixture = (
        _REPO_ROOT
        / "execution/evals/planning_spine/fixtures/skills/continue-session/SKILL.md"
    ).read_text()

    assert contract_errors("continue-session", fixture) == []


@pytest.mark.parametrize(
    ("marker", "expected_fragment"),
    [
        ("[COPY: not-found message and recovery hint]", "not-found"),
        ("[COPY: ambiguity prompt]", "ambiguity prompt"),
        ("[COPY: empty-session outcome]", "empty-session"),
        ("Partly unreadable", "partly unreadable"),
        ("bounded retry", "bounded retry"),
        ("unreadable source is unknown, never empty", "unreadable source"),
    ],
)
def test_each_whole_session_recovery_outcome_is_required(
    marker: str, expected_fragment: str
) -> None:
    content = (
        BASE
        + PHASE_REPORTING_CONTRACT
        + "Show a planning resume ledger. Queue a newly introduced workstream until "
        "current work is captured, workboarded, and dispatched."
        + WHOLE_SESSION_CONTRACT
    )
    assert contract_errors("continue-session", content) == []
    errors = contract_errors("continue-session", content.replace(marker, "omitted"))

    assert any(expected_fragment in error for error in errors)


def test_reconcile_is_explicitly_retrospective() -> None:
    errors = contract_errors("reconcile-planning", BASE)
    assert any("retrospective repair" in error for error in errors)
    assert any("not the real-time source of truth" in error for error in errors)


def test_unknown_skill_is_refused() -> None:
    with pytest.raises(ValueError, match="unsupported planning-spine skill"):
        contract_errors("some-other-skill", BASE)


def test_sync_finds_a_renamed_skill_by_frontmatter_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A historical directory name must not orphan the canonical entity."""
    repo_root = tmp_path / "repo-skills"
    user_root = tmp_path / "user-skills"
    mirror = user_root / "status" / "SKILL.md"
    mirror.parent.mkdir(parents=True)
    mirror.write_text("---\nname: digest\n---\n\n# digest\n")
    monkeypatch.setattr(sync_skills, "REPO_SKILLS_DIR", repo_root)
    monkeypatch.setattr(sync_skills, "USER_SKILLS_DIR", user_root)

    _, aliases = sync_skills._index_disk_mirrors()

    assert aliases["status"] == mirror.resolve()
    assert aliases["digest"] == mirror.resolve()
