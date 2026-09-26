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


def contract_errors(slug: str, content: str) -> list[str]:
    """Return human-readable violations for one canonical skill body."""
    if slug not in _SKILL_REQUIREMENTS:
        raise ValueError(f"unsupported planning-spine skill: {slug}")

    folded = " ".join(content.lower().split())
    errors: list[str] = []
    for marker, meaning in {
        **_SHARED_REQUIREMENTS,
        **_SKILL_REQUIREMENTS[slug],
    }.items():
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
