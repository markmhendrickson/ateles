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


@pytest.mark.parametrize(
    ("slug", "extra"),
    [
        (
            "continue-session",
            "Show a planning resume ledger. Queue a newly introduced "
            "workstream until current work is captured, workboarded, and dispatched.",
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
    assert contract_errors(slug, BASE + extra) == []


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
        body
        + "Show a planning resume ledger. Queue a newly introduced workstream "
        "until current work is captured, workboarded, and dispatched.",
    )
    assert any(expected_fragment in error for error in errors)


def test_resume_requires_the_planning_records_resumed() -> None:
    errors = contract_errors(
        "continue-session",
        BASE
        + "Queue a newly introduced workstream until current work is captured, "
        "workboarded, and dispatched.",
    )
    assert any("resume ledger" in error for error in errors)


def test_digest_requires_planning_spine_summary() -> None:
    errors = contract_errors(
        "digest",
        BASE
        + "Queue a newly introduced workstream until current work is captured, "
        "workboarded, and dispatched.",
    )
    assert any("planning spine summary" in error for error in errors)


def test_interactive_skills_require_sequential_admission_gate() -> None:
    for slug in ("continue-session", "digest"):
        errors = contract_errors(slug, BASE)
        assert any("captured, workboarded, and dispatched" in error for error in errors)


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
