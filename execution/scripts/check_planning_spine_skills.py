#!/usr/bin/env python3
"""Check the live session skills for the task-ascent planning contract.

Neotoma ``skill`` entities are canonical; user-root ``SKILL.md`` files are
generated mirrors.  This check therefore reads the entities directly and exits
non-zero when one is missing or no longer tells the harness how to report the
planning spine.
"""

from __future__ import annotations

import argparse
import re
import sys

import sync_skills


SKILLS = ("continue-session", "digest", "reconcile-planning")

_SHARED_REQUIREMENTS = {
    "planning spine": "name the planning spine",
    "part_of": "derive through PART_OF",
    "plan": "report the plan level",
    "project": "report the project level",
    "strategy": "report the strategy level",
    "missing ascent": "identify missing ascent",
    "duplicate ascent": "identify duplicate ascent",
    "source of truth": "state which record is the source of truth",
}

_SKILL_REQUIREMENTS = {
    "continue-session": {
        "planning resume ledger": "show the planning records being resumed",
        "captured, workboarded, and dispatched": (
            "queue a new workstream until current work is captured and dispatched"
        ),
        "exact source session": "bind a whole-session continuation before a plan",
        "terminal resumable population as a union": (
            "combine every terminal source instead of accepting the first hit"
        ),
        "session_digest or workboard": "include the source session's status inventory",
        "terminal handoff": "include the source session's terminal handoff",
        "source-session coverage ledger": "account for every distinct source lane",
        "explicitly excluded": "record a reasoned exclusion disposition",
        "unresolved": "preserve lanes whose canonical identity is not proven",
        "audited = imported + excluded + unresolved": (
            "balance all four source-session coverage counts"
        ),
        "comprehensively resumed while an omitted row exists": (
            "forbid completeness claims while a source lane is omitted"
        ),
    },
    "digest": {
        "planning spine summary": "include a planning-spine status summary",
        "captured, workboarded, and dispatched": (
            "queue a new workstream until current work is captured and dispatched"
        ),
    },
    "reconcile-planning": {
        "retrospective repair": "define reconciliation as retrospective repair",
        "not the real-time source of truth": (
            "deny reconciliation status as the real-time source of truth"
        ),
    },
}

_PHASE_REPORTING_REQUIREMENTS = {
    "selected master plan first": "display the selected master plan before workstreams",
    "canonical phase": "use the master plan's own phase names",
    "exit-gate state": "show each canonical phase's exit-gate state",
    "structural phase binding": "derive workstream-to-phase placement structurally",
    "cross-phase prerequisite": "label work with no phase binding explicitly",
    "serial/parallel task execution underneath": (
        "place task mechanics below the phase-level view"
    ),
    "subordinate workstream label": (
        "distinguish a local workstream label from a canonical master phase"
    ),
    "not structurally derivable": "state when no canonical phase can be derived",
    "task title": "forbid phase inference from task titles",
}


def contract_errors(slug: str, content: str) -> list[str]:
    """Return human-readable violations for one canonical skill body."""
    if slug not in _SKILL_REQUIREMENTS:
        raise ValueError(f"unsupported planning-spine skill: {slug}")

    folded = " ".join(content.lower().split())
    errors: list[str] = []
    requirements = {
        **_SHARED_REQUIREMENTS,
        **_SKILL_REQUIREMENTS[slug],
    }
    if slug in {"continue-session", "digest"}:
        requirements.update(_PHASE_REPORTING_REQUIREMENTS)

    for marker, meaning in requirements.items():
        present = (
            re.search(rf"\b{re.escape(marker)}\b", folded) is not None
            if marker in {"plan", "project", "strategy"}
            else marker in folded
        )
        if not present:
            errors.append(f"{slug}: missing {marker!r} ({meaning})")
    return errors


def check_live_skills(only: tuple[str, ...] = SKILLS) -> list[str]:
    """Fetch canonical entities and validate exactly the requested skills."""
    base_url, token = sync_skills._load_env()
    entities = sync_skills.fetch_skills(base_url, token)
    by_slug = {skill["_slug"]: skill for skill in entities}
    by_name = {skill["name"]: skill for skill in entities}

    errors: list[str] = []
    for slug in only:
        skill = by_slug.get(slug) or by_name.get(slug)
        if skill is None:
            errors.append(f"{slug}: canonical skill entity is missing")
            continue
        errors.extend(contract_errors(slug, skill.get("content") or ""))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        action="append",
        choices=SKILLS,
        help="check only this skill (repeatable)",
    )
    args = parser.parse_args(argv)
    selected = tuple(args.only or SKILLS)

    errors = check_live_skills(selected)
    if errors:
        for error in errors:
            print(f"ERROR {error}", file=sys.stderr)
        return 1
    print(f"OK planning-spine contract holds for {', '.join(selected)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
